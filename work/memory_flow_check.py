from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_runtime.config import load_settings  # noqa: E402
from agent_runtime.memory import MemoryManager  # noqa: E402
from agent_runtime.redis_store import RedisStore  # noqa: E402
from agent_runtime.retrieval import RetrievalEngine  # noqa: E402


def check(name: str, condition: bool, details: dict | None = None) -> dict:
    if not condition:
        raise AssertionError(f"{name} failed: {details or {}}")
    return {"name": name, "ok": True, "details": details or {}}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run L1/L2/L3 memory flow checks.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--redis-url", default=os.environ.get("MEMORY_TEST_REDIS_URL"))
    parser.add_argument("--fallback", action="store_true", help="Use an unreachable Redis URL to force memory fallback.")
    parser.add_argument("--keep-keys", action="store_true", help="Do not delete generated test keys after the run.")
    args = parser.parse_args()

    started = time.perf_counter()
    settings = load_settings(args.config)
    configured_redis = RedisStore(settings.redis_url)

    run_id = uuid.uuid4().hex[:10]
    store_url = "redis://127.0.0.1:1/0" if args.fallback else (args.redis_url or settings.redis_url)
    store = RedisStore(store_url)
    memory = MemoryManager(l1_recent_turns=6, store=store)
    engine = RetrievalEngine(memory, minimum_relevance_scores={"l2": 0.0, "l3": 0.0})
    checks: list[dict] = []

    session_id = f"task-memory-flow-{run_id}"
    for index in range(10):
        role = "user" if index % 2 == 0 else "assistant"
        memory.add_l1_turn(session_id, role, f"turn-{index}")

    l1 = memory.get_l1(session_id)
    l1_store = store.get_json(f"mem:l1:session:{session_id}")
    checks.append(check("l1_retention_keeps_latest_6", len(l1) == 6 and l1[0]["content"] == "turn-4" and l1[-1]["content"] == "turn-9"))
    checks.append(check("l1_persisted_to_store", isinstance(l1_store, list) and len(l1_store) == 6))

    l2_namespace = f"user-{run_id}"
    l2_project_namespace = f"project-{run_id}"
    l2_id = memory.store_l2(
        l2_namespace,
        {"preference": "prefer wideband patch antenna reports with S11 evidence", "confidence": 0.93},
        provenance={
            "source_type": "conversation",
            "source_id": "message-memory-flow-001",
            "source_ref": f"task-memory-flow-{run_id}/messages/001",
            "source_excerpt": "Prefer wideband patch antenna reports with S11 evidence.",
        },
    )
    untraceable_l2_id = memory.store_l2(
        l2_project_namespace,
        {"constraint": "do not run real CST without approval", "confidence": 0.88},
    )
    checks.append(check("l2_json_persisted", store.get_json(f"mem:l2:{l2_namespace}:{l2_id}")["memory_id"] == l2_id))
    checks.append(check("l2_vector_persisted", l2_id in store._vector_docs["l2"]))

    saved_l3 = memory.maybe_store_l3_workflow(
        {
            "task_status": "completed",
            "unresolved_failures": 0,
            "graph_step": 5,
            "workflow_quality": 0.92,
            "domain": "antenna",
            "goal_pattern": f"wideband_patch_reproduction_{run_id}",
            "steps": ["evidence", "geometry", "review", "report"],
        }
    )
    rejected_l3 = memory.maybe_store_l3_workflow(
        {
            "task_status": "failed",
            "unresolved_failures": 1,
            "graph_step": 5,
            "workflow_quality": 0.95,
            "domain": "antenna",
            "goal_pattern": f"failed_flow_{run_id}",
        }
    )
    checks.append(check("l3_completed_workflow_saved", saved_l3 is True))
    checks.append(check("l3_failed_workflow_rejected", rejected_l3 is False))

    l2_hits = engine.retrieve_l2("wideband patch antenna S11 evidence", final_top_k=3)
    l3_hits = engine.retrieve_l3("reuse successful wideband patch reproduction workflow", top_k=2)
    context = engine.build_context("wideband patch antenna workflow with S11 evidence", l2_top_k=2, l3_top_k=1)
    strict_hits = RetrievalEngine(memory, minimum_relevance_scores={"l2": 1.0, "l3": 1.0}).retrieve_l2(
        "wideband patch antenna S11 evidence",
        final_top_k=5,
    )

    checks.append(check("l2_retrieval_hits_traceable_memory", any(hit["memory_id"] == l2_id for hit in l2_hits), {"hit_ids": [hit["memory_id"] for hit in l2_hits]}))
    traceable_hit = next(hit for hit in l2_hits if hit["memory_id"] == l2_id)
    checks.append(check("l2_trace_policy_and_source_available", traceable_hit["provenance"]["trace_policy"] == "llm_decide" and traceable_hit["provenance"]["available"] is True))
    untraceable_hit = RetrievalEngine(memory, minimum_relevance_scores={"l2": 0.0, "l3": 0.0}).retrieve_l2(
        "real CST approval",
        final_top_k=5,
    )
    checks.append(check("l2_untraceable_memory_marks_source_unavailable", any(hit["memory_id"] == untraceable_l2_id and hit["provenance"]["available"] is False for hit in untraceable_hit)))
    checks.append(check("l3_retrieval_hits_workflow", len(l3_hits) >= 1 and l3_hits[0]["source"] == "l3_memory"))
    checks.append(check("context_contains_l2_and_l3", len(context["l2"]) >= 1 and len(context["l3"]) >= 1))
    checks.append(check("strict_threshold_filters_results", strict_hits == []))

    snapshot = memory.snapshot()
    checks.append(check("snapshot_counts_layers", snapshot["l1_sessions"] == 1 and snapshot["l2_count"] == 2 and snapshot["l3_count"] == 1, snapshot))

    cross_client = {}
    if store.available:
        reader_store = RedisStore(store_url)
        reader = MemoryManager(l1_recent_turns=6, store=reader_store)
        reader_engine = RetrievalEngine(reader, minimum_relevance_scores={"l2": 0.0, "l3": 0.0})
        reader_l1 = reader.get_l1(session_id)
        reader_context = reader_engine.build_context(
            "wideband patch antenna workflow with S11 evidence",
            l2_top_k=2,
            l3_top_k=1,
        )
        cross_client = {
            "l1_turns": len(reader_l1),
            "l2_count": reader.snapshot()["l2_count"],
            "l3_count": reader.snapshot()["l3_count"],
            "l2_hits": len(reader_context["l2"]),
            "l3_hits": len(reader_context["l3"]),
        }
        checks.append(check(
            "redis_cross_client_readback",
            reader_l1 == l1 and len(reader_context["l2"]) >= 1 and len(reader_context["l3"]) >= 1,
            cross_client,
        ))

    cleanup_patterns = [
        f"mem:l1:session:{session_id}",
        f"mem:l2:{l2_namespace}:*",
        f"mem:l2:{l2_project_namespace}:*",
        f"mem:l3:workflow:antenna:wideband_patch_reproduction_{run_id}",
    ]
    result = {
        "ok": True,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "configured_redis": configured_redis.health(),
        "test_store": store.health(),
        "checks": checks,
        "sample_l1": l1,
        "sample_l2_hit": {
            "memory_id": traceable_hit["memory_id"],
            "source": traceable_hit["source"],
            "retrieval_score": round(traceable_hit["retrieval_score"], 4),
            "coarse_sources": traceable_hit["coarse_sources"],
            "provenance": traceable_hit["provenance"],
        },
        "sample_l3_hit": {
            "memory_id": l3_hits[0]["memory_id"],
            "source": l3_hits[0]["source"],
            "retrieval_score": round(l3_hits[0]["retrieval_score"], 4),
            "workflow_quality": l3_hits[0]["workflow_quality"],
        },
        "snapshot": snapshot,
        "cross_client": cross_client,
        "cleanup_patterns": cleanup_patterns,
        "cleaned_up": not args.keep_keys,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not args.keep_keys:
        for pattern in cleanup_patterns:
            store.delete_pattern(pattern)


if __name__ == "__main__":
    main()
