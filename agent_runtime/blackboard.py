from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import atomic_write_json, now_iso, stable_hash


class OptimisticLockError(RuntimeError):
    pass


@dataclass
class BlackboardRecord:
    task_id: str
    state: str = "created"
    version: int = 0
    capability_snapshot_id: str | None = None
    task_metadata: dict[str, Any] = field(default_factory=dict)
    nodes: dict[str, Any] = field(default_factory=dict)
    approvals: dict[str, Any] = field(default_factory=dict)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Return the durable blackboard record."""
        return {
            "schema_version": "1.0",
            "task_id": self.task_id,
            "state": self.state,
            "version": self.version,
            "capability_snapshot_id": self.capability_snapshot_id,
            "task_metadata": self.task_metadata,
            "nodes": self.nodes,
            "approvals": self.approvals,
            "blockers": self.blockers,
            "events": self.events,
            "updated_at": self.updated_at,
        }


class Blackboard:
    def __init__(self, workspace_root: str | Path):
        self.workspace_root = Path(workspace_root)
        self.records: dict[str, BlackboardRecord] = {}

    def create_task(
        self,
        user_input: str,
        capability_snapshot_id: str | None = None,
        task_metadata: dict[str, Any] | None = None,
    ) -> BlackboardRecord:
        """Create and persist a task record."""
        task_id = stable_hash({"user_input": user_input, "created_at": now_iso()})[:16]
        metadata = {"user_input": user_input, **(task_metadata or {})}
        record = BlackboardRecord(task_id=task_id, capability_snapshot_id=capability_snapshot_id, task_metadata=metadata)
        record.events.append({"type": "task_created", "message": "task created", "created_at": now_iso()})
        self.records[task_id] = record
        self.snapshot(task_id)
        return record

    def get(self, task_id: str) -> BlackboardRecord:
        """Load a task from memory or disk."""
        if task_id not in self.records:
            return self.load_task(task_id)
        return self.records[task_id]

    def update(self, task_id: str, expected_version: int | None = None, **changes: Any) -> BlackboardRecord:
        """Update and persist a task record with optional optimistic locking."""
        record = self.get(task_id)
        if expected_version is not None and expected_version != record.version:
            raise OptimisticLockError(f"expected version {expected_version}, got {record.version}")
        for key, value in changes.items():
            setattr(record, key, value)
        record.version += 1
        record.updated_at = now_iso()
        self.snapshot(task_id)
        return record

    def append_event(self, task_id: str, event: dict[str, Any]) -> None:
        """Append an audit event pointer to the task record."""
        record = self.get(task_id)
        pointer = {
            "sequence_id": event.get("sequence_id"),
            "redis_sequence_id": event.get("redis_sequence_id"),
            "node_id": event.get("node_id"),
            "type": event.get("type"),
            "status": event.get("status"),
            "created_at": event.get("created_at", now_iso()),
        }
        record.events.append(pointer)
        record.events = record.events[-300:]
        record.version += 1
        self.snapshot(task_id)

    def update_node(self, task_id: str, node_id: str, status: str, **metadata: Any) -> None:
        """Update one node status on the task record."""
        record = self.get(task_id)
        record.nodes[node_id] = {
            **record.nodes.get(node_id, {}),
            "status": status,
            "updated_at": now_iso(),
            **metadata,
        }
        record.version += 1
        self.snapshot(task_id)

    def snapshot(self, task_id: str) -> Path:
        """Persist the latest blackboard state."""
        record = self.get(task_id)
        target = self.workspace_root / "tasks" / task_id / "blackboard.json"
        atomic_write_json(target, record.to_dict())
        return target

    def load_task(self, task_id: str) -> BlackboardRecord:
        """Load one task from its durable blackboard file."""
        target = self.workspace_root / "tasks" / task_id / "blackboard.json"
        data = json.loads(target.read_text(encoding="utf-8"))
        record = BlackboardRecord(
            task_id=data["task_id"],
            state=data.get("state", "created"),
            version=data.get("version", 0),
            capability_snapshot_id=data.get("capability_snapshot_id"),
            task_metadata=data.get("task_metadata", {}),
            nodes=data.get("nodes", {}),
            approvals=data.get("approvals", {}),
            blockers=data.get("blockers", []),
            events=data.get("events", []),
            updated_at=data.get("updated_at", now_iso()),
        )
        self.records[task_id] = record
        return record

    def load_existing(self) -> list[BlackboardRecord]:
        """Load all task records from the workspace."""
        tasks_root = self.workspace_root / "tasks"
        if not tasks_root.exists():
            return []
        return [self.load_task(path.parent.name) for path in tasks_root.glob("*/blackboard.json")]

    def list_tasks(self) -> list[dict[str, Any]]:
        """List task records for the frontend/API."""
        records = list(self.records.values()) or self.load_existing()
        return [record.to_dict() for record in sorted(records, key=lambda item: item.updated_at, reverse=True)]
