from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .embedding import embed_text
from .redis_store import RedisStore
from .utils import now_iso, stable_hash


@dataclass
class MemoryManager:
    l1_recent_turns: int = 20
    "none为临时存储，在config里设置redis配置"
    store: RedisStore | None = None
    l1: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    l2: dict[str, dict[str, Any]] = field(default_factory=dict)
    long_term_sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    l3: dict[str, dict[str, Any]] = field(default_factory=dict)
    invalidated_cache_keys: list[str] = field(default_factory=list)
    embedder: Callable[[str], list[float]] = embed_text

    def add_l1_turn(self, session_id: str, role: str, content: str) -> None:
        "追加短期对话"
        "turn存储最近对话"
        turns = self.l1.setdefault(session_id, [])
        turns.append({"role": role, "content": content, "created_at": now_iso()})
        del turns[:-self.l1_recent_turns]
        if self.store is not None:
            self.store.set_json(f"mem:l1:session:{session_id}", turns)

    "读取cession的短期记忆"
    def get_l1(self, session_id: str) -> list[dict[str, Any]]:
        if self.store is not None:
            return list(self.store.get_json(f"mem:l1:session:{session_id}", self.l1.get(session_id, [])))
        return list(self.l1.get(session_id, []))

    def store_long_term_source(
        self,
        source_id: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store raw source material pending extraction into a structured L2 memory."""
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("long-term source_id must be a non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("long-term source content must be a non-empty string")
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("long-term source metadata must be a dictionary or None")
        record = {
            "schema_version": "1.0",
            "source_id": source_id,
            "status": "pending_l2_extraction",
            "content": content,
            "content_hash": stable_hash(content),
            "metadata": dict(metadata or {}),
            "updated_at": now_iso(),
        }
        self.long_term_sources[source_id] = record
        if self.store is not None:
            self.store.set_json(f"mem:source:long_term:{source_id}", record)
        return source_id

    def get_long_term_source(self, source_id: str) -> dict[str, Any] | None:
        if self.store is not None:
            return self.store.get_json(f"mem:source:long_term:{source_id}")
        record = self.long_term_sources.get(source_id)
        return dict(record) if record is not None else None

    "保存长期l2记忆，负责找回用户偏好、项目事实、长期约束。"
    def store_l2(
        self,
        namespace: str,
        fact: dict[str, Any],
        *,
        provenance: dict[str, Any] | None = None,
    ) -> str:
        memory_id = fact.get("memory_id") or stable_hash({"namespace": namespace, "fact": fact})[:16]
        if provenance is not None and not isinstance(provenance, dict):
            raise TypeError("L2 provenance must be a dictionary or None")
        provenance_record = dict(provenance or {})
        provenance_record["trace_policy"] = "llm_decide"
        provenance_record.setdefault(
            "available",
            bool(
                provenance_record.get("source_ref")
                or provenance_record.get("source_id")
                or provenance_record.get("source_excerpt")
            ),
        )
        record = {
            "memory_id": memory_id,
            "namespace": namespace,
            "status": "active",
            "confidence": fact.get("confidence", 0.7),
            "updated_at": now_iso(),
            "fact": fact,
            "provenance": provenance_record,
        }
        self.l2[memory_id] = record
        "相近缓存覆盖，以最新版为主"
        self.invalidated_cache_keys.extend([f"cache:hot:preference:{namespace}:*", "cache:hot:*preference*"])
        if self.store is not None:
            text = self._l2_text(record)
            self.store.set_json(f"mem:l2:{namespace}:{memory_id}", record)
            self.store.upsert_vector("l2", memory_id, text, self.embed_text(text), record)
            self.store.delete_pattern(f"cache:hot:preference:{namespace}:*")
            self.store.delete_pattern("cache:hot:*preference*")
        return memory_id

    def maybe_store_l3_workflow(self, workflow: dict[str, Any]) -> bool:
        if workflow.get("task_status") != "completed" or workflow.get("unresolved_failures", 0) != 0:
            return False
        if workflow.get("graph_step", 999) > 30 or workflow.get("workflow_quality", 0.0) < 0.8:
            return False
        key = f"{workflow.get('domain', 'unknown')}:{workflow.get('goal_pattern', 'unknown')}"
        existing = self.l3.get(key)
        if existing and workflow.get("workflow_quality", 0) <= existing.get("workflow_quality", 0):
            return False
        record = {**workflow, "status": "active", "updated_at": now_iso()}
        self.l3[key] = record
        if self.store is not None:
            text = self._l3_text(key, record)
            self.store.set_json(f"mem:l3:workflow:{key}", record)
            self.store.upsert_vector("l3", key, text, self.embed_text(text), {"workflow_key": key, **record})
        return True

    def snapshot(self) -> dict[str, Any]:
        l2_count = len(self.l2)
        l3_count = len(self.l3)
        if self.store is not None:
            l2_count = self.store.count_keys("mem:l2:*") if hasattr(self.store, "count_keys") else len(self.store.keys("mem:l2:*"))
            l3_count = self.store.count_keys("mem:l3:workflow:*") if hasattr(self.store, "count_keys") else len(self.store.keys("mem:l3:workflow:*"))
        return {
            "l1_sessions": len(self.l1),
            "l2_count": l2_count,
            "l3_count": l3_count,
            "l2_cached": len(self.l2),
            "l3_cached": len(self.l3),
            "redis": self.store.health() if self.store is not None else {"available": False, "backend": "none"},
            "invalidated_cache_keys": list(self.invalidated_cache_keys),
        }

    def hydrate_from_store(self, limit: int | None = None) -> None:
        if limit is None:
            raise ValueError("hydrate_from_store requires an explicit limit")
        if self.store is None:
            return
        loaded = 0
        keys = self.store.iter_keys("mem:l2:*") if hasattr(self.store, "iter_keys") else iter(self.store.keys("mem:l2:*"))
        for key in keys:
            record = self.store.get_json(key)
            if isinstance(record, dict) and record.get("memory_id"):
                self.l2[record["memory_id"]] = record
                loaded += 1
                if limit is not None and loaded >= limit:
                    return
        keys = self.store.iter_keys("mem:l3:workflow:*") if hasattr(self.store, "iter_keys") else iter(self.store.keys("mem:l3:workflow:*"))
        for key in keys:
            record = self.store.get_json(key)
            if isinstance(record, dict):
                self.l3[key.split("mem:l3:workflow:", 1)[-1]] = record
                loaded += 1
                if limit is not None and loaded >= limit:
                    return

    def embed_text(self, text: str) -> list[float]:
        return self.embedder(text)

    def _l2_text(self, record: dict[str, Any]) -> str:
        return f"{record.get('namespace', '')} {record.get('fact', '')}"

    def _l3_text(self, workflow_key: str, record: dict[str, Any]) -> str:
        return f"{workflow_key} {record}"
