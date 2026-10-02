from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

from .embedding import cosine_similarity, embed_text
from .utils import stable_hash


def dedupe_query_variants(query: str, query_variants: list[str] | None = None) -> list[str]:
    """原句在前，拓展 query 去重后追加。"""
    variants = [str(query or "")]
    for item in query_variants or []:
        text = str(item or "").strip()
        if text and text not in variants:
            variants.append(text)
    return variants


BM25_RECALL_TOP_K = 50
EMBEDDING_RECALL_TOP_K = 15
FINAL_SEMANTIC_TOP_K = 5
RRF_K = 60
DEFAULT_MIN_RELEVANCE_SCORES = {"l2": 0.51, "l3": 0.51}
EMBEDDING_CACHE_SIZE = 2048
ANTENNA_DICTIONARY_PATH = Path(__file__).with_name("dictionaries") / "antenna_terms.txt"
DEFAULT_L2_PROVENANCE = {"trace_policy": "llm_decide", "available": False}


@lru_cache(maxsize=1)
def _jieba_tokenizer() -> jieba.Tokenizer:
    tokenizer = jieba.Tokenizer()
    if ANTENNA_DICTIONARY_PATH.exists():
        with ANTENNA_DICTIONARY_PATH.open("r", encoding="utf-8") as dictionary:
            tokenizer.load_userdict(dictionary)
    return tokenizer


def _bm25_tokens(text: str) -> list[str]:
    normalized = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens: list[str] = []
    for part in _jieba_tokenizer().cut_for_search(normalized, HMM=False):
        tokens.extend(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", str(part).lower()))
    return tokens


class RetrievalEngine:
    """Run BM25 and embedding rankings per query, then fuse them with RRF."""

    def __init__(self, memory_manager: Any, minimum_relevance_scores: dict[str, float] | None = None):
        self.memory = memory_manager
        configured = {**DEFAULT_MIN_RELEVANCE_SCORES, **(minimum_relevance_scores or {})}
        if any(not 0.0 <= float(value) <= 1.0 for value in configured.values()):
            raise ValueError("minimum relevance scores must be between 0 and 1")
        self.minimum_relevance_scores = {layer: float(configured[layer]) for layer in ("l2", "l3")}
        self._embedding_cache: dict[str, list[float]] = {}

    def retrieve_l2(
        self,
        query: str,
        final_top_k: int = FINAL_SEMANTIC_TOP_K,
        query_variants: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._retrieve_layer(query, "l2", min(final_top_k, FINAL_SEMANTIC_TOP_K), query_variants)

    def retrieve_l3(
        self,
        query: str,
        top_k: int = FINAL_SEMANTIC_TOP_K,
        query_variants: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._retrieve_layer(query, "l3", min(top_k, FINAL_SEMANTIC_TOP_K), query_variants)

    def build_context(
        self,
        query: str,
        l2_top_k: int = FINAL_SEMANTIC_TOP_K,
        l3_top_k: int = FINAL_SEMANTIC_TOP_K,
        query_variants: list[str] | None = None,
    ) -> dict[str, Any]:
        variants = dedupe_query_variants(query, query_variants)
        return {
            "schema_version": "1.0",
            "query_hash": stable_hash(query),
            "query_variants": variants,
            "top_k": {"l2": min(l2_top_k, FINAL_SEMANTIC_TOP_K), "l3": min(l3_top_k, FINAL_SEMANTIC_TOP_K)},
            "l2": self.retrieve_l2(query, l2_top_k, query_variants=variants),
            "l3": self.retrieve_l3(query, l3_top_k, query_variants=variants),
        }

    def _retrieve_layer(
        self,
        query: str,
        layer: str,
        top_k: int,
        query_variants: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        records = [record for record in self._iter_layer_records(layer) if record.get("status", "active") == "active"]
        if not records or top_k <= 0:
            return []
        variants = dedupe_query_variants(query, query_variants)
        ranking_lists: list[list[str]] = []
        semantic_scores_by_variant: list[dict[str, float]] = []
        bm25_scores_by_variant: list[list[float]] = []
        for variant in variants:
            bm25_scores = self._bm25_scores([variant], layer, records)
            bm25_scores_by_variant.append(bm25_scores)
            bm25_order = sorted(
                range(len(records)),
                key=lambda index: (-bm25_scores[index], self._record_id(records[index])),
            )[:BM25_RECALL_TOP_K]
            ranking_lists.append([self._record_id(records[index]) for index in bm25_order])

            query_vector = self._embed_text(variant)
            embedding_scores = self._embedding_scores(layer, records, [query_vector])
            semantic_scores_by_variant.append(embedding_scores)
            ranking_lists.append([
                record_id
                for record_id, _ in sorted(embedding_scores.items(), key=lambda item: (-item[1], item[0]))[:EMBEDDING_RECALL_TOP_K]
            ])

        rrf_scores: dict[str, float] = {}
        for ranking in ranking_lists:
            for rank, record_id in enumerate(ranking, start=1):
                rrf_scores[record_id] = rrf_scores.get(record_id, 0.0) + 1.0 / (RRF_K + rank)

        candidate_ids = set(rrf_scores)
        records_by_id = {self._record_id(record): record for record in records}
        original_embedding_scores = semantic_scores_by_variant[0]
        max_bm25_scores = {
            self._record_id(record): max(scores[index] for scores in bm25_scores_by_variant)
            for index, record in enumerate(records)
        }
        threshold = self.minimum_relevance_scores[layer]

        ranked = []
        for record_id in candidate_ids:
            original_embedding_score = float(original_embedding_scores.get(record_id, 0.0))
            if original_embedding_score < threshold:
                continue
            ranking_memberships = sum(record_id in ranking for ranking in ranking_lists)
            coarse_sources = []
            if any(record_id in ranking_lists[index] for index in range(0, len(ranking_lists), 2)):
                coarse_sources.append("bm25_top50")
            if any(record_id in ranking_lists[index] for index in range(1, len(ranking_lists), 2)):
                coarse_sources.append("embedding_top15")
            record = dict(records_by_id[record_id])
            if layer == "l2":
                record.setdefault("provenance", dict(DEFAULT_L2_PROVENANCE))
            ranked.append({
                **record,
                "source": f"{layer}_memory",
                "hit_reason": "per_query_bm25+embedding->rrf_fusion",
                "retrieval_score": round(rrf_scores[record_id], 8),
                "retrieval_components": {
                    "rrf": round(rrf_scores[record_id], 8),
                    "original_embedding": round(original_embedding_score, 8),
                },
                "semantic_score": round(original_embedding_score, 8),
                "rrf_score": round(rrf_scores[record_id], 8),
                "bm25_score": max_bm25_scores.get(record_id, 0.0),
                "coarse_sources": coarse_sources,
                "ranking_memberships": ranking_memberships,
                "retrieval_pipeline": {
                    "query_variant_count": len(variants),
                    "bm25_top_k": BM25_RECALL_TOP_K,
                    "embedding_top_k": EMBEDDING_RECALL_TOP_K,
                    "ranking_list_count": len(ranking_lists),
                    "fusion": "rrf",
                    "rrf_k": RRF_K,
                    "candidate_count_before_dedupe": sum(len(ranking) for ranking in ranking_lists),
                    "candidate_count_after_dedupe": len(candidate_ids),
                    "semantic_threshold": threshold,
                    "threshold_basis": "original_query_embedding",
                    "final_top_k": top_k,
                },
            })
        return sorted(ranked, key=lambda item: (-item["rrf_score"], -item["semantic_score"], self._record_id(item)))[:top_k]

    def _bm25_scores(self, queries: str | list[str], layer: str, records: list[dict[str, Any]]) -> list[float]:
        """多个 query 的 BM25 分数按记录取最大值。"""
        variants = queries if isinstance(queries, list) else [queries]
        model = BM25Okapi([_bm25_tokens(self._record_text(layer, record)) for record in records])
        merged = [0.0] * len(records)
        for variant in variants:
            for index, score in enumerate(model.get_scores(_bm25_tokens(variant))):
                merged[index] = max(merged[index], float(score))
        return merged

    def _embedding_scores(
        self,
        layer: str,
        records: list[dict[str, Any]],
        query_vectors: list[list[float]],
    ) -> dict[str, float]:
        """多个 query 向量的语义分数按记录取最大值。"""
        if self.memory.store is not None:
            scores: dict[str, float] = {}
            for query_vector in query_vectors:
                hits = self.memory.store.vector_search(layer, query_vector, max(len(records), EMBEDDING_RECALL_TOP_K))
                for item in hits:
                    if item.get("status", "active") != "active":
                        continue
                    record_id = self._record_id(item)
                    score = float(item.get("vector_score", 0.0))
                    if score > scores.get(record_id, 0.0):
                        scores[record_id] = score
            for record in records:
                record_id = self._record_id(record)
                if record_id in scores:
                    continue
                text = self._record_text(layer, record)
                record_vector = self._embed_text(text)
                scores[record_id] = max(cosine_similarity(query_vector, record_vector) for query_vector in query_vectors)
                if hasattr(self.memory.store, "upsert_vector"):
                    self.memory.store.upsert_vector(layer, record_id, text, record_vector, record)
            return scores
        texts = {self._record_id(record): self._record_text(layer, record) for record in records}
        return {
            record_id: max(
                cosine_similarity(query_vector, self._embed_text(text))
                for query_vector in query_vectors
            )
            for record_id, text in texts.items()
        }

    def _iter_layer_records(self, layer: str):
        if self.memory.store is not None:
            pattern = "mem:l2:*" if layer == "l2" else "mem:l3:workflow:*"
            keys = self.memory.store.iter_keys(pattern) if hasattr(self.memory.store, "iter_keys") else iter(self.memory.store.keys(pattern))
            for key in keys:
                record = self.memory.store.get_json(key)
                if isinstance(record, dict):
                    if layer == "l3":
                        workflow_key = key.split("mem:l3:workflow:", 1)[-1]
                        yield {"workflow_key": workflow_key, "memory_id": workflow_key, **record}
                    else:
                        yield record
            return
        if layer == "l2":
            yield from self.memory.l2.values()
            return
        for workflow_key, record in self.memory.l3.items():
            yield {"workflow_key": workflow_key, "memory_id": workflow_key, **record}

    @staticmethod
    def _record_id(record: dict[str, Any]) -> str:
        return str(record.get("memory_id") or record.get("workflow_key") or record.get("id"))

    @staticmethod
    def _record_text(layer: str, record: dict[str, Any]) -> str:
        if layer == "l2":
            return f"{record.get('namespace', '')} {record.get('fact', '')}"
        return f"{record.get('workflow_key', '')} {record}"

    def _embed_text(self, text: str) -> list[float]:
        cache_key = stable_hash(text)
        cached = self._embedding_cache.get(cache_key)
        if cached is not None:
            return cached
        vector = self.memory.embed_text(text) if hasattr(self.memory, "embed_text") else embed_text(text)
        if len(self._embedding_cache) >= EMBEDDING_CACHE_SIZE:
            self._embedding_cache.pop(next(iter(self._embedding_cache)))
        self._embedding_cache[cache_key] = vector
        return vector
