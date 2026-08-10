from __future__ import annotations

from pathlib import Path
from typing import Any

from .redis_store import RedisStore
from .utils import ensure_dir, now_iso, redact_text


class AuditLog:
    def __init__(self, logs_root: str | Path, store: RedisStore | None = None):
        """初始化当前对象依赖和运行参数。"""
        self.logs_root = Path(logs_root)
        self.hot_events: dict[str, list[dict[str, Any]]] = {}
        self.store = store

    def emit(self, task_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """写入审计事件并返回带序号的事件对象。"""
        event = dict(event)
        event.setdefault("created_at", now_iso())
        event["message"] = redact_text(str(event.get("message", "")))
        events = self.hot_events.setdefault(task_id, [])
        event["sequence_id"] = f"{len(events) + 1:08d}"
        events.append(event)
        if self.store is not None:
            event["redis_sequence_id"] = self.store.xadd(f"audit:event:{task_id}", event)
        self._write_cold(event)
        return event

    def replay(self, task_id: str, after_sequence_id: str | None = None) -> list[dict[str, Any]]:
        """按任务和序号重放审计事件。"""
        if self.store is not None and self.store.available:
            return self.store.xrange(f"audit:event:{task_id}", after_sequence_id)
        events = self.hot_events.get(task_id, [])
        if not after_sequence_id:
            return list(events)
        return [event for event in events if event["sequence_id"] > after_sequence_id]

    def _write_cold(self, event: dict[str, Any]) -> None:
        """把审计事件追加写入冷日志文件。"""
        date = now_iso()[:10]
        target_dir = ensure_dir(self.logs_root / "audit")
        with (target_dir / f"{date}.jsonl").open("a", encoding="utf-8") as handle:
            import json

            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
