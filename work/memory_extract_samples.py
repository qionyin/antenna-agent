from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_runtime.config import load_settings  # noqa: E402
from agent_runtime.memory import MemoryManager  # noqa: E402
from agent_runtime.redis_store import RedisStore  # noqa: E402


def _key_text(key: Any) -> str:
    return key.decode("utf-8") if isinstance(key, bytes) else str(key)


def _scan_keys(store: RedisStore, pattern: str) -> list[str]:
    if hasattr(store, "iter_keys"):
        return sorted(_key_text(key) for key in store.iter_keys(pattern))
    return sorted(_key_text(key) for key in store.keys(pattern))


def _session_id_from_l1_key(key: str) -> str:
    prefix = "mem:l1:session:"
    return key[len(prefix):] if key.startswith(prefix) else key


def extract_samples(store: RedisStore, memory: MemoryManager, limit: int = 1) -> dict[str, Any]:
    l1_keys = _scan_keys(store, "mem:l1:session:*")
    l2_keys = _scan_keys(store, "mem:l2:*")
    l3_keys = _scan_keys(store, "mem:l3:workflow:*")

    result: dict[str, Any] = {
        "counts": {
            "l1_sessions": len(l1_keys),
            "l2_items": len(l2_keys),
            "l3_items": len(l3_keys),
        },
        "l1": [],
        "l2": [],
        "l3": [],
    }

    for key in l1_keys[:limit]:
        session_id = _session_id_from_l1_key(key)
        turns = memory.get_l1(session_id)
        result["l1"].append({
            "key": key,
            "session_id": session_id,
            "count": len(turns),
            "turns": turns,
        })

    for key in l2_keys[:limit]:
        result["l2"].append({
            "key": key,
            "data": store.get_json(key),
        })

    for key in l3_keys[:limit]:
        result["l3"].append({
            "key": key,
            "data": store.get_json(key),
        })

    if not any(result["counts"].values()):
        result["hint"] = "No memory keys found. Run: python work\\memory_flow_check.py --keep-keys"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract sample records from L1/L2/L3 memory layers.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--redis-url", default=os.environ.get("MEMORY_TEST_REDIS_URL"))
    parser.add_argument("--limit", type=int, default=1)
    args = parser.parse_args()

    settings = load_settings(args.config)
    store_url = args.redis_url or settings.redis_url
    store = RedisStore(store_url)
    if not store.available:
        print(json.dumps({
            "ok": False,
            "error": f"Redis not available at {store_url}",
            "store": store.health(),
        }, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    memory = MemoryManager(l1_recent_turns=settings.l1_recent_turns, store=store)
    output = {
        "ok": True,
        "store": store.health(),
        "samples": extract_samples(store, memory, max(1, args.limit)),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
