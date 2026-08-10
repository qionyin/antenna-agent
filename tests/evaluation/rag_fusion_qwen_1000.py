from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from rank_bm25 import BM25Okapi

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
PRF_DOCUMENTS = 2
SEED_EMBEDDING_CACHE = Path(
    r"D:\pythoncode\antenna_agent_lab\docs\evaluations\hybrid_retrieval_1000_bm25_coarse_qwen_rerank_20260722\qwen_embedding_cache.json"
)


class RagFusionPrototype:
    def __init__(self, records_by_layer: dict[str, list[dict[str, Any]]], embeddings: dict[str, list[float]]):
        self.records_by_layer = records_by_layer
        self.embeddings = embeddings
        self.bm25_by_layer = {
            layer: BM25Okapi([bm25_tokens(record["text"]) for record in records])
            for layer, records in records_by_layer.items()
        }

    def retrieve(self, variants: list[str], layer: str, threshold: float | None) -> tuple[list[str], dict[str, Any]]:
        records = self.records_by_layer[layer]
        record_vectors = {record["id"]: self.embeddings[record["text"]] for record in records}
        rank_lists: list[list[str]] = []
        semantic_by_variant: list[dict[str, float]] = []
        for query in variants:
            lexical_scores = self.bm25_by_layer[layer].get_scores(bm25_tokens(query))
            lexical_order = sorted(range(len(records)), key=lambda index: (-float(lexical_scores[index]), records[index]["id"]))
            rank_lists.append([records[index]["id"] for index in lexical_order[:BM25_TOP_K]])
            query_vector = self.embeddings[query]
            semantic = {record["id"]: cosine_similarity(query_vector, record_vectors[record["id"]]) for record in records}
            semantic_by_variant.append(semantic)
            rank_lists.append([record_id for record_id, _ in sorted(semantic.items(), key=lambda item: (-item[1], item[0]))[:EMBEDDING_TOP_K]])

        rrf_scores: dict[str, float] = {}
        for ranking in rank_lists:
            for rank, record_id in enumerate(ranking, start=1):
                rrf_scores[record_id] = rrf_scores.get(record_id, 0.0) + 1.0 / (RRF_K + rank)
        fused_ids = [record_id for record_id, _ in sorted(rrf_scores.items(), key=lambda item: (-item[1], item[0]))[:RRF_CANDIDATE_TOP_K]]
        semantic_scores = {
            record_id: max(scores[record_id] for scores in semantic_by_variant)
            for record_id in fused_ids
        }
        reranked = sorted(semantic_scores.items(), key=lambda item: (-item[1], item[0]))
        if threshold is not None:
            reranked = [item for item in reranked if item[1] >= threshold]
        returned = [record_id for record_id, _ in reranked[:FINAL_TOP_K]]
        return returned, {
            "query_variant_count": len(variants),
            "rrf_rank_list_count": len(rank_lists),
            "rrf_candidate_count": len(fused_ids),
            "returned_count": len(returned),
            "top_score": reranked[0][1] if reranked else None,
        }


def build_prf_variants(
    cases: list[dict[str, Any]],
    records_by_layer: dict[str, list[dict[str, Any]]],
    base_embeddings: dict[str, list[float]],
) -> dict[str, list[str]]:
    variants_by_case: dict[str, list[str]] = {}
    for case in cases:
        query = case["query"]
        query_vector = base_embeddings[query]
        records = records_by_layer[case["layer"]]
        nearest = sorted(
            records,
            key=lambda record: (-cosine_similarity(query_vector, base_embeddings[record["text"]]), record["id"]),
        )[:PRF_DOCUMENTS]
        variants_by_case[case["case_id"]] = [query, *[f"{query} {record['text']}" for record in nearest]]
    return variants_by_case


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
    embedding_cache = QwenEmbeddingCache(output_dir, seed_cache_path=SEED_EMBEDDING_CACHE)
    base_texts = [record["text"] for records in records_by_layer.values() for record in records]
    base_texts.extend(case["query"] for case in cases)
    base_embeddings = embedding_cache.embed_many(base_texts)
    variants_by_case = build_prf_variants(cases, records_by_layer, base_embeddings)
    all_variant_texts = [query for variants in variants_by_case.values() for query in variants]
    embeddings = embedding_cache.embed_many([*base_texts, *all_variant_texts])
    engine = RagFusionPrototype(records_by_layer, embeddings)

    modes = {
        "rag_fusion_qwen_no_threshold": None,
        "rag_fusion_qwen_threshold_040": 0.40,
        "rag_fusion_qwen_threshold_051": 0.51,
    }
    results = []
    started_all = time.perf_counter()
    for mode, threshold in modes.items():
        for case in cases:
            started = time.perf_counter()
            ids, trace = engine.retrieve(variants_by_case[case["case_id"]], case["layer"], threshold)
            row = case_result(case, mode, ids, (time.perf_counter() - started) * 1000)
            row["pipeline_trace"] = trace
            results.append(row)
    summary = summarize(results)
    summary["pipeline"] = {
        "query_variant_method": "qwen_embedding_pseudo_relevance_feedback",
        "query_variants": 3,
        "prf_documents": PRF_DOCUMENTS,
        "retrievers_per_variant": ["bm25_top50", "qwen_embedding_top15"],
        "rrf_k": RRF_K,
        "rrf_candidate_top_k": RRF_CANDIDATE_TOP_K,
        "final_rerank": "max_qwen_embedding_cosine_across_variants",
        "final_top_k": FINAL_TOP_K,
        "embedding_model": embedding_cache.model,
        "embedding_external_calls": embedding_cache.external_calls,
        "wall_time_ms": round((time.perf_counter() - started_all) * 1000, 4),
    }
    atomic_write_json(output_dir / "summary.json", summary)
    atomic_write_json(output_dir / "query_variants.json", {"schema_version": "1.0", "variants": variants_by_case})
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
