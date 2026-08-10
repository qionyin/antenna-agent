from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

from .embedding import cosine_similarity, embed_text
from .utils import stable_hash


BM25_RECALL_TOP_K = 50
EMBEDDING_RECALL_TOP_K = 15
FINAL_SEMANTIC_TOP_K = 5
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
    """BM25/embedding coarse recall followed by original-query embedding rerank."""

    def __init__(self, memory_manager: Any, minimum_relevance_scores: dict[str, float] | None = None):
        self.memory = memory_manager
        configured = {**DEFAULT_MIN_RELEVANCE_SCORES, **(minimum_relevance_scores or {})}
        if any(not 0.0 <= float(value) <= 1.0 for value in configured.values()):
            raise ValueError("minimum relevance scores must be between 0 and 1")
        self.minimum_relevance_scores = {layer: float(configured[layer]) for layer in ("l2", "l3")}
        self._embedding_cache: dict[str, list[float]] = {}

    def retrieve_l2(self, query: str, final_top_k: int = FINAL_SEMANTIC_TOP_K) -> list[dict[str, Any]]:
        return self._retrieve_layer(query, "l2", min(final_top_k, FINAL_SEMANTIC_TOP_K))

    def retrieve_l3(self, query: str, top_k: int = FINAL_SEMANTIC_TOP_K) -> list[dict[str, Any]]:
        return self._retrieve_layer(query, "l3", min(top_k, FINAL_SEMANTIC_TOP_K))

    def build_context(
        self,
        query: str,
        l2_top_k: int = FINAL_SEMANTIC_TOP_K,
        l3_top_k: int = FINAL_SEMANTIC_TOP_K,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "query_hash": stable_hash(query),
            "top_k": {"l2": min(l2_top_k, FINAL_SEMANTIC_TOP_K), "l3": min(l3_top_k, FINAL_SEMANTIC_TOP_K)},
            "l2": self.retrieve_l2(query, l2_top_k),
            "l3": self.retrieve_l3(query, l3_top_k),
        }

    def _retrieve_layer(self, query: str, layer: str, top_k: int) -> list[dict[str, Any]]:
        records = [record for record in self._iter_layer_records(layer) if record.get("status", "active") == "active"]
        if not records or top_k <= 0:
            return []
        query_vector = self._embed_text(query)
        embedding_scores = self._embedding_scores(layer, records, query_vector)
        bm25_scores = self._bm25_scores(query, layer, records)

        bm25_ids = [
            self._record_id(records[index])
            for index in sorted(
                range(len(records)),
                key=lambda index: (-bm25_scores[index], self._record_id(records[index])),
            )[:BM25_RECALL_TOP_K]
        ]
        embedding_ids = [
            record_id
            for record_id, _ in sorted(embedding_scores.items(), key=lambda item: (-item[1], item[0]))[:EMBEDDING_RECALL_TOP_K]
        ]
        candidate_ids = set([*bm25_ids, *embedding_ids])
        records_by_id = {self._record_id(record): record for record in records}
        bm25_by_id = {self._record_id(record): float(score) for record, score in zip(records, bm25_scores)}
        threshold = self.minimum_relevance_scores[layer]

        ranked = []
        for record_id in candidate_ids:
            embedding_score = float(embedding_scores.get(record_id, 0.0))
            if embedding_score < threshold:
                continue
            coarse_sources = []
            if record_id in bm25_ids:
                coarse_sources.append("bm25_top50")
            if record_id in embedding_ids:
                coarse_sources.append("embedding_top15")
            record = dict(records_by_id[record_id])
            if layer == "l2":
                record.setdefault("provenance", dict(DEFAULT_L2_PROVENANCE))
            ranked.append({
                **record,
                "source": f"{layer}_memory",
                "hit_reason": "bm25_top50+embedding_top15->original_embedding_rerank",
                "retrieval_score": embedding_score,
                "retrieval_components": {"embedding": embedding_score},
                "bm25_score": bm25_by_id.get(record_id, 0.0),
                "coarse_sources": coarse_sources,
                "retrieval_pipeline": {
                    "bm25_top_k": BM25_RECALL_TOP_K,
                    "embedding_top_k": EMBEDDING_RECALL_TOP_K,
                    "candidate_count_before_dedupe": len(bm25_ids) + len(embedding_ids),
                    "candidate_count_after_dedupe": len(candidate_ids),
                    "semantic_threshold": threshold,
                    "final_top_k": top_k,
                },
            })
        return sorted(ranked, key=lambda item: (-item["retrieval_score"], self._record_id(item)))[:top_k]

    def _bm25_scores(self, query: str, layer: str, records: list[dict[str, Any]]) -> list[float]:
        model = BM25Okapi([_bm25_tokens(self._record_text(layer, record)) for record in records])
        return [float(score) for score in model.get_scores(_bm25_tokens(query))]

    def _embedding_scores(
        self,
        layer: str,
        records: list[dict[str, Any]],
        query_vector: list[float],
    ) -> dict[str, float]:
        if self.memory.store is not None:
            hits = self.memory.store.vector_search(layer, query_vector, max(len(records), EMBEDDING_RECALL_TOP_K))
            scores = {
                self._record_id(item): float(item.get("vector_score", 0.0))
                for item in hits
                if item.get("status", "active") == "active"
            }
            for record in records:
                record_id = self._record_id(record)
                if record_id in scores:
                    continue
                text = self._record_text(layer, record)
                record_vector = self._embed_text(text)
                scores[record_id] = cosine_similarity(query_vector, record_vector)
                if hasattr(self.memory.store, "upsert_vector"):
                    self.memory.store.upsert_vector(layer, record_id, text, record_vector, record)
            return scores
        return {
            self._record_id(record): cosine_similarity(
                query_vector,
                self._embed_text(self._record_text(layer, record)),
            )
            for record in records
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
