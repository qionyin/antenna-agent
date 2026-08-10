from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_runtime.config import load_settings  # noqa: E402
from agent_runtime.embedding import cosine_similarity  # noqa: E402
from agent_runtime.memory import MemoryManager  # noqa: E402
from agent_runtime.redis_store import RedisStore  # noqa: E402
from agent_runtime.retrieval import RetrievalEngine  # noqa: E402


DEFAULT_QUERY = "wideband patch antenna S11 evidence workflow"


def _key_text(key: Any) -> str:
    return key.decode("utf-8") if isinstance(key, bytes) else str(key)


def _scan_keys(store: RedisStore, pattern: str) -> list[str]:
    if hasattr(store, "iter_keys"):
        return sorted(_key_text(key) for key in store.iter_keys(pattern))
    return sorted(_key_text(key) for key in store.keys(pattern))


def _session_id_from_l1_key(key: str) -> str:
    prefix = "mem:l1:session:"
    return key[len(prefix):] if key.startswith(prefix) else key


def _recall_l1(memory: MemoryManager, store: RedisStore, query: str) -> dict[str, Any] | None:
    session_keys = _scan_keys(store, "mem:l1:session:*")
    if not session_keys:
        return None
    query_vector = memory.embed_text(query)
    scored = []
    for key in session_keys:
        session_id = _session_id_from_l1_key(key)
        turns = memory.get_l1(session_id)
        text = " ".join(str(turn.get("content") or "") for turn in turns)
        score = cosine_similarity(query_vector, memory.embed_text(text))
        scored.append((score, key, session_id, turns))
    scored.sort(key=lambda item: (-item[0], item[1]))
    score, key, session_id, turns = scored[0]
    return {
        "source": "l1_recent_turns",
        "key": key,
        "session_id": session_id,
        "retrieval_score": round(float(score), 4),
        "turn_count": len(turns),
        "turns": turns,
    }


def _seed_demo(memory: MemoryManager) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:10]
    session_id = f"manual-memory-demo-{run_id}"
    for role, content in [
        ("user", "I prefer wideband patch antenna reports with S11 evidence."),
        ("assistant", "Recorded preference for traceable S11 evidence and patch antenna workflows."),
    ]:
        memory.add_l1_turn(session_id, role, content)
    l2_id = memory.store_l2(
        f"manual-user-{run_id}",
        {"preference": "prefer wideband patch antenna reports with S11 evidence", "confidence": 0.93},
        provenance={
            "source_type": "manual_seed",
            "source_id": f"manual-memory-demo-{run_id}",
            "source_ref": f"manual-memory-demo-{run_id}/l2",
            "source_excerpt": "Prefer wideband patch antenna reports with S11 evidence.",
        },
    )
    memory.maybe_store_l3_workflow(
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
    return {"run_id": run_id, "session_id": session_id, "l2_id": l2_id}


def main() -> None:
    parser = argparse.ArgumentParser(description="Recall one record from L1, L2, and L3 memory.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--redis-url", default=os.environ.get("MEMORY_TEST_REDIS_URL"))
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Text used to recall memory.")
    parser.add_argument("--min-score", type=float, default=0.0, help="Semantic threshold for L2/L3 recall.")
    parser.add_argument("--seed-demo", action="store_true", help="Create demo L1/L2/L3 records before recall.")
    args = parser.parse_args()

    settings = load_settings(args.config)
    store_url = args.redis_url or settings.redis_url
    store = RedisStore(store_url)
    if not store.available:
        print(json.dumps({
            "ok": False,
            "error": f"Redis not available at {store_url}",
            "store": store.health(),
        }, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    memory = MemoryManager(l1_recent_turns=settings.l1_recent_turns, store=store)
    seeded = _seed_demo(memory) if args.seed_demo else None
    engine = RetrievalEngine(memory, minimum_relevance_scores={"l2": args.min_score, "l3": args.min_score})

    l2_hits = engine.retrieve_l2(args.query, final_top_k=1)
    l3_hits = engine.retrieve_l3(args.query, top_k=1)
    output = {
        "ok": True,
        "query": args.query,
        "min_score": args.min_score,
        "store": store.health(),
        "seeded": seeded,
        "counts": {
            "l1_sessions": len(_scan_keys(store, "mem:l1:session:*")),
            "l2_items": len(_scan_keys(store, "mem:l2:*")),
            "l3_items": len(_scan_keys(store, "mem:l3:workflow:*")),
        },
        "recall": {
            "l1": _recall_l1(memory, store, args.query),
            "l2": l2_hits[0] if l2_hits else None,
            "l3": l3_hits[0] if l3_hits else None,
        },
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
