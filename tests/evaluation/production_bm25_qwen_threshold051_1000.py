from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from agent_runtime.memory import MemoryManager
from agent_runtime.redis_store import RedisStore
from agent_runtime.retrieval import RetrievalEngine
from agent_runtime.utils import atomic_write_json
from tests.evaluation.bm25_coarse_embedding_rerank_1000 import QwenEmbeddingCache
from tests.evaluation.hybrid_retrieval_1000 import build_cases, case_result, concept_record, populate, record_text, summarize


SEED_EMBEDDING_CACHE = Path(
    r"D:\pythoncode\antenna_agent_lab\docs\evaluations\hybrid_retrieval_1000_bm25_coarse_qwen_rerank_20260722\qwen_embedding_cache.json"
)


def evaluate(output_dir: Path, redis_url: str) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    cases, corpora = build_cases()
    records = {
        layer: [concept_record(item) for item in concepts]
        for layer, concepts in corpora.items()
    }
    texts = [record_text(layer, record) for layer, layer_records in records.items() for record in layer_records]
    texts.extend(case["query"] for case in cases)
    embedding_cache = QwenEmbeddingCache(output_dir, seed_cache_path=SEED_EMBEDDING_CACHE)
    embeddings = embedding_cache.embed_many(texts)

    store = RedisStore(redis_url)
    if not store.available or store.count_keys("*"):
        raise RuntimeError(f"evaluation Redis database must be available and empty: {store.health()}")
    memory = MemoryManager(store=store, embedder=lambda text: embeddings[text])
    results = []
    started_all = time.perf_counter()
    try:
        populate(memory, corpora)
        persisted_texts = []
        for layer, pattern in (("l2", "mem:l2:*"), ("l3", "mem:l3:workflow:*")):
            for key in store.iter_keys(pattern):
                record = store.get_json(key)
                if not isinstance(record, dict):
                    continue
                if layer == "l3":
                    workflow_key = key.split("mem:l3:workflow:", 1)[-1]
                    record = {"workflow_key": workflow_key, "memory_id": workflow_key, **record}
                persisted_texts.append(RetrievalEngine._record_text(layer, record))
        embeddings.update(embedding_cache.embed_many(persisted_texts))
        engine = RetrievalEngine(memory)
        for case in cases:
            started = time.perf_counter()
            hits = engine.retrieve_l2(case["query"], 5) if case["layer"] == "l2" else engine.retrieve_l3(case["query"], 5)
            ids = [str(item.get("memory_id") or item.get("workflow_key") or item.get("id")) for item in hits]
            results.append(case_result(case, "production_bm25_qwen_threshold051", ids, (time.perf_counter() - started) * 1000))
        summary = summarize(results)
        summary["pipeline"] = {
            "implementation": "agent_runtime.retrieval.RetrievalEngine",
            "bm25_top_k": 50,
            "embedding_top_k": 15,
            "final_top_k": 5,
            "semantic_threshold": 0.51,
            "final_score": "original_query_qwen_embedding_cosine_only",
            "embedding_model": embedding_cache.model,
            "embedding_dimensions": embedding_cache.dimensions,
            "external_embedding_calls": embedding_cache.external_calls,
            "wall_time_ms": round((time.perf_counter() - started_all) * 1000, 4),
        }
        atomic_write_json(output_dir / "summary.json", summary)
        (output_dir / "results.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), encoding="utf-8"
        )
        return summary
    finally:
        store.delete_pattern("mem:l2:*")
        store.delete_pattern("mem:l3:workflow:*")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/15")
    args = parser.parse_args()
    print(json.dumps(evaluate(Path(args.output_dir), args.redis_url), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
