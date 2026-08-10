from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from openai import OpenAI
from rank_bm25 import BM25Okapi

from agent_runtime.config import load_settings
from agent_runtime.embedding import cosine_similarity
from agent_runtime.utils import atomic_write_json
from tests.evaluation.bm25_coarse_embedding_rerank_1000 import (
    BM25_TOP_K,
    EMBEDDING_TOP_K,
    FINAL_TOP_K,
    QwenEmbeddingCache,
    bm25_tokens,
)
from tests.evaluation.hybrid_retrieval_1000 import build_cases, case_result, concept_record, record_text, summarize


RRF_K = 60
RRF_CANDIDATE_TOP_K = 65
REWRITES_PER_QUERY = 2
REWRITE_BATCH_SIZE = 50
SEED_EMBEDDING_CACHE = Path(
    r"D:\pythoncode\antenna_agent_lab\docs\evaluations\hybrid_retrieval_1000_bm25_coarse_qwen_rerank_20260722\qwen_embedding_cache.json"
)


class RelatedQueryCache:
    def __init__(self, output_dir: Path):
        settings = load_settings()
        api_key = os.getenv(settings.llm_api_key_env)
        if not api_key:
            raise RuntimeError(f"missing related-query API key: {settings.llm_api_key_env}")
        self.model = settings.llm_model_name
        self.client = OpenAI(api_key=api_key, base_url=settings.llm_base_url, timeout=90)
        self.path = output_dir / "related_queries.json"
        self.rewrites: dict[str, list[str]] = {}
        self.external_calls = 0
        if self.path.exists():
            cached = json.loads(self.path.read_text(encoding="utf-8"))
            if cached.get("model") == self.model:
                self.rewrites = {str(key): list(value) for key, value in (cached.get("rewrites") or {}).items()}

    def generate(self, cases: list[dict[str, Any]]) -> dict[str, list[str]]:
        pending = [case for case in cases if case["case_id"] not in self.rewrites]
        for start in range(0, len(pending), REWRITE_BATCH_SIZE):
            batch = pending[start:start + REWRITE_BATCH_SIZE]
            items = [{"id": case["case_id"], "query": case["query"]} for case in batch]
            prompt = (
                "Generate exactly two short alternative retrieval questions for every input query. "
                "Keep the same meaning, technical domain, entities, and any negation. Do not answer the query, "
                "infer hidden intent, or introduce facts. Return strict JSON only in this shape: "
                '{"items":[{"id":"ID","queries":["rewrite 1","rewrite 2"]}]}. Inputs: '
                + json.dumps(items, ensure_ascii=False)
            )
            parsed = None
            last_error = None
            for attempt, delay in enumerate((5, 15, 60), start=1):
                try:
                    response = self.client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        response_format={"type": "json_object"},
                    )
                    self.external_calls += 1
                    content = response.choices[0].message.content or ""
                    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
                    parsed = json.loads(content)
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 3:
                        time.sleep(delay)
            if parsed is None:
                raise RuntimeError(f"related-query generation failed after 3 attempts: {last_error}") from last_error
            returned = {
                str(item["id"]): [str(query).strip() for query in item.get("queries") or [] if str(query).strip()]
                for item in parsed.get("items") or []
            }
            for case in batch:
                queries = returned.get(case["case_id"], [])
                if len(queries) != REWRITES_PER_QUERY:
                    raise RuntimeError(f"related-query count mismatch for {case['case_id']}: {queries}")
                self.rewrites[case["case_id"]] = queries
            atomic_write_json(
                self.path,
                {"schema_version": "1.0", "model": self.model, "rewrites": self.rewrites},
            )
        return self.rewrites


class RagFusionOriginalQueryReranker:
    def __init__(self, records_by_layer: dict[str, list[dict[str, Any]]], embeddings: dict[str, list[float]]):
        self.records_by_layer = records_by_layer
        self.embeddings = embeddings
        self.bm25_by_layer = {
            layer: BM25Okapi([bm25_tokens(record["text"]) for record in records])
            for layer, records in records_by_layer.items()
        }

    def retrieve(self, original_query: str, related_queries: list[str], layer: str, threshold: float | None):
        records = self.records_by_layer[layer]
        record_vectors = {record["id"]: self.embeddings[record["text"]] for record in records}
        variants = [original_query, *related_queries]
        rank_lists: list[list[str]] = []
        for query in variants:
            lexical_scores = self.bm25_by_layer[layer].get_scores(bm25_tokens(query))
            lexical_order = sorted(range(len(records)), key=lambda index: (-float(lexical_scores[index]), records[index]["id"]))
            rank_lists.append([records[index]["id"] for index in lexical_order[:BM25_TOP_K]])
            query_vector = self.embeddings[query]
            semantic = {
                record["id"]: cosine_similarity(query_vector, record_vectors[record["id"]])
                for record in records
            }
            rank_lists.append([
                record_id
                for record_id, _ in sorted(semantic.items(), key=lambda item: (-item[1], item[0]))[:EMBEDDING_TOP_K]
            ])

        rrf_scores: dict[str, float] = {}
        for ranking in rank_lists:
            for rank, record_id in enumerate(ranking, start=1):
                rrf_scores[record_id] = rrf_scores.get(record_id, 0.0) + 1.0 / (RRF_K + rank)
        fused_ids = [
            record_id
            for record_id, _ in sorted(rrf_scores.items(), key=lambda item: (-item[1], item[0]))[:RRF_CANDIDATE_TOP_K]
        ]

        original_vector = self.embeddings[original_query]
        original_scores = {
            record_id: cosine_similarity(original_vector, record_vectors[record_id])
            for record_id in fused_ids
        }
        reranked = sorted(original_scores.items(), key=lambda item: (-item[1], item[0]))
        if threshold is not None:
            reranked = [item for item in reranked if item[1] >= threshold]
        returned = [record_id for record_id, _ in reranked[:FINAL_TOP_K]]
        return returned, {
            "query_variant_count": len(variants),
            "rrf_rank_list_count": len(rank_lists),
            "rrf_candidate_count": len(fused_ids),
            "returned_count": len(returned),
            "top_original_query_score": reranked[0][1] if reranked else None,
            "threshold_source": "original_query_qwen_embedding_only",
        }


def evaluate(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cases, corpora = build_cases()
    records_by_layer = {
        layer: [
            {**(record := concept_record(item)), "id": record.get("memory_id"), "text": record_text(layer, record)}
            for item in concepts
            if item.active
        ]
        for layer, concepts in corpora.items()
    }
    related_cache = RelatedQueryCache(output_dir)
    related_queries = related_cache.generate(cases)
    embedding_cache = QwenEmbeddingCache(output_dir, seed_cache_path=SEED_EMBEDDING_CACHE)
    texts = [record["text"] for records in records_by_layer.values() for record in records]
    texts.extend(case["query"] for case in cases)
    texts.extend(query for queries in related_queries.values() for query in queries)
    embeddings = embedding_cache.embed_many(texts)
    engine = RagFusionOriginalQueryReranker(records_by_layer, embeddings)

    modes = {
        "rag_fusion_original_qwen_no_threshold": None,
        "rag_fusion_original_qwen_threshold_040": 0.40,
        "rag_fusion_original_qwen_threshold_051": 0.51,
    }
    results = []
    started_all = time.perf_counter()
    for mode, threshold in modes.items():
        for case in cases:
            started = time.perf_counter()
            ids, trace = engine.retrieve(
                case["query"], related_queries[case["case_id"]], case["layer"], threshold
            )
            row = case_result(case, mode, ids, (time.perf_counter() - started) * 1000)
            row["pipeline_trace"] = trace
            results.append(row)
    summary = summarize(results)
    summary["pipeline"] = {
        "related_query_model": related_cache.model,
        "related_query_external_calls": related_cache.external_calls,
        "query_variants": 3,
        "retrievers_per_variant": ["bm25_top50", "qwen_embedding_top15"],
        "rrf_k": RRF_K,
        "rrf_candidate_top_k": RRF_CANDIDATE_TOP_K,
        "final_rerank": "original_query_qwen_embedding_cosine_only",
        "threshold_source": "original_query_qwen_embedding_cosine_only",
        "related_query_scores_in_final_rank": False,
        "related_query_scores_in_threshold": False,
        "final_top_k": FINAL_TOP_K,
        "embedding_model": embedding_cache.model,
        "embedding_external_calls": embedding_cache.external_calls,
        "wall_time_ms": round((time.perf_counter() - started_all) * 1000, 4),
    }
    atomic_write_json(output_dir / "summary.json", summary)
    (output_dir / "results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    evaluate(Path(args.output_dir))


if __name__ == "__main__":
    main()
