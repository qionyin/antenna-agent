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
from agent_runtime.embedding import cosine_similarity, normalize_vector
from agent_runtime.utils import atomic_write_json, stable_hash
from tests.evaluation.hybrid_retrieval_1000 import (
    build_cases,
    case_result,
    concept_record,
    record_text,
    summarize,
)


BM25_TOP_K = 50
EMBEDDING_TOP_K = 15
FINAL_TOP_K = 5
BATCH_SIZE = 10


class QwenEmbeddingCache:
    def __init__(self, output_dir: Path, seed_cache_path: Path | None = None):
        settings = load_settings()
        api_key = os.getenv(settings.embedding_api_key_env)
        if not api_key:
            raise RuntimeError(f"missing Qwen embedding key: {settings.embedding_api_key_env}")
        self.model = settings.embedding_model_name
        self.dimensions = settings.embedding_dimensions
        self.client = OpenAI(
            api_key=api_key,
            base_url=settings.embedding_base_url,
            timeout=settings.embedding_timeout_seconds,
        )
        self.path = output_dir / "qwen_embedding_cache.json"
        self.vectors: dict[str, list[float]] = {}
        self.external_calls = 0
        cache_source = self.path if self.path.exists() else seed_cache_path
        if cache_source is not None and cache_source.exists():
            cached = json.loads(cache_source.read_text(encoding="utf-8"))
            if cached.get("model") == self.model and cached.get("dimensions") == self.dimensions:
                self.vectors = {str(key): list(value) for key, value in (cached.get("vectors") or {}).items()}

    def embed_many(self, texts: list[str]) -> dict[str, list[float]]:
        unique = list(dict.fromkeys(str(text) for text in texts))
        missing = [text for text in unique if stable_hash(text) not in self.vectors]
        for start in range(0, len(missing), BATCH_SIZE):
            batch = missing[start:start + BATCH_SIZE]
            payload: dict[str, Any] = {
                "model": self.model,
                "input": batch,
                "encoding_format": "float",
            }
            if self.dimensions is not None:
                payload["dimensions"] = self.dimensions
            response = self.client.embeddings.create(**payload)
            self.external_calls += 1
            ordered = sorted(response.data, key=lambda item: item.index)
            if len(ordered) != len(batch):
                raise RuntimeError(f"Qwen embedding batch size mismatch: expected {len(batch)}, got {len(ordered)}")
            for text, item in zip(batch, ordered):
                self.vectors[stable_hash(text)] = normalize_vector([float(value) for value in item.embedding])
        atomic_write_json(
            self.path,
            {
                "schema_version": "1.0",
                "provider": "qwen",
                "model": self.model,
                "dimensions": self.dimensions,
                "vectors": self.vectors,
            },
        )
        return {text: self.vectors[stable_hash(text)] for text in unique}


def bm25_tokens(text: str) -> list[str]:
    normalized = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens: list[str] = []
    for part in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            tokens.append(part)
            tokens.extend(part[index:index + 2] for index in range(len(part) - 1))
            tokens.extend(part[index:index + 3] for index in range(len(part) - 2))
        else:
            tokens.append(part)
    return tokens


class Bm25CoarseEmbeddingReranker:
    """Standalone test-only implementation of BM25 coarse recall + embedding rerank."""

    def __init__(self, records_by_layer: dict[str, list[dict[str, Any]]], embeddings: dict[str, list[float]]):
        self.records_by_layer = records_by_layer
        self.embeddings = embeddings
        self.bm25_by_layer = {
            layer: BM25Okapi([bm25_tokens(record["text"]) for record in records])
            for layer, records in records_by_layer.items()
        }
        self.embeddings_by_layer = {
            layer: {record["id"]: embeddings[record["text"]] for record in records}
            for layer, records in records_by_layer.items()
        }

    def retrieve(self, query: str, layer: str) -> tuple[list[str], dict[str, Any]]:
        records = self.records_by_layer[layer]
        query_vector = self.embeddings[query]
        lexical_scores = self.bm25_by_layer[layer].get_scores(bm25_tokens(query))
        lexical_order = sorted(
            range(len(records)),
            key=lambda index: (-float(lexical_scores[index]), records[index]["id"]),
        )
        bm25_ids = [records[index]["id"] for index in lexical_order[:BM25_TOP_K]]

        semantic_scores = [
            (cosine_similarity(query_vector, self.embeddings_by_layer[layer][record["id"]]), record["id"])
            for record in records
        ]
        semantic_order = sorted(semantic_scores, key=lambda item: (-item[0], item[1]))
        embedding_ids = [record_id for _, record_id in semantic_order[:EMBEDDING_TOP_K]]

        candidate_ids = list(dict.fromkeys([*bm25_ids, *embedding_ids]))
        candidate_set = set(candidate_ids)
        reranked = sorted(
            ((score, record_id) for score, record_id in semantic_scores if record_id in candidate_set),
            key=lambda item: (-item[0], item[1]),
        )
        returned_ids = [record_id for _, record_id in reranked[:FINAL_TOP_K]]
        return returned_ids, {
            "bm25_candidate_count": len(bm25_ids),
            "embedding_candidate_count": len(embedding_ids),
            "candidate_count_before_dedupe": len(bm25_ids) + len(embedding_ids),
            "candidate_count_after_dedupe": len(candidate_ids),
            "returned_count": len(returned_ids),
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
    embedding_cache = QwenEmbeddingCache(output_dir)
    texts = [record["text"] for records in records_by_layer.values() for record in records]
    texts.extend(case["query"] for case in cases)
    embeddings = embedding_cache.embed_many(texts)
    engine = Bm25CoarseEmbeddingReranker(records_by_layer, embeddings)
    results: list[dict[str, Any]] = []
    started_all = time.perf_counter()
    for case in cases:
        started = time.perf_counter()
        ids, trace = engine.retrieve(case["query"], case["layer"])
        latency_ms = (time.perf_counter() - started) * 1000
        row = case_result(case, "bm2550_embedding15_rerank5", ids, latency_ms)
        row["pipeline_trace"] = trace
        results.append(row)
    summary = summarize(results)
    summary["pipeline"] = {
        "coarse_bm25_top_k": BM25_TOP_K,
        "coarse_embedding_top_k": EMBEDDING_TOP_K,
        "coarse_candidate_count_max": BM25_TOP_K + EMBEDDING_TOP_K,
        "final_embedding_top_k": FINAL_TOP_K,
        "final_score": "embedding_cosine_similarity_only",
        "bm25_in_final_score": False,
        "fuzzy_in_final_score": False,
        "confidence_in_final_score": False,
        "embedding_provider": "qwen",
        "embedding_model": embedding_cache.model,
        "embedding_dimensions": embedding_cache.dimensions,
        "external_embedding_calls": embedding_cache.external_calls,
        "wall_time_ms": round((time.perf_counter() - started_all) * 1000, 4),
    }
    trace_rows = [row["pipeline_trace"] for row in results]
    summary["pipeline_trace_averages"] = {
        key: round(sum(float(row[key]) for row in trace_rows) / len(trace_rows), 4)
        for key in trace_rows[0]
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results),
        encoding="utf-8",
    )
    (output_dir / "pipeline.json").write_text(json.dumps(summary["pipeline"], ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(Path(args.output_dir)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
