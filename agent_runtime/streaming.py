from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .utils import now_iso, redact_text


@dataclass
class StreamEvent:
    sequence_id: str
    task_id: str
    node_id: str
    type: str
    status: str
    ui_safe_message: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """将当前对象转换为可序列化字典。"""
        return {
            "sequence_id": self.sequence_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "type": self.type,
            "status": self.status,
            "ui_safe_message": redact_text(self.ui_safe_message),
            "reason": redact_text(self.reason or "") or None,
            "created_at": now_iso(),
        }


class StreamPublisher:
    def __init__(self) -> None:
        """初始化当前对象依赖和运行参数。"""
        self.events: dict[str, list[StreamEvent]] = {}

    def publish(self, task_id: str, node_id: str, event_type: str, status: str, message: str, reason: str | None = None) -> StreamEvent:
        """发布任务流事件。"""
        seq = f"{len(self.events.get(task_id, [])) + 1:08d}"
        event = StreamEvent(seq, task_id, node_id, event_type, status, message, reason)
        self.events.setdefault(task_id, []).append(event)
        return event

    def replay(self, task_id: str, after_sequence_id: str | None = None) -> list[dict[str, Any]]:
        """按任务和序号重放审计事件。"""
        events = self.events.get(task_id, [])
        if after_sequence_id:
            events = [event for event in events if event.sequence_id > after_sequence_id]
        return [event.to_dict() for event in events]
