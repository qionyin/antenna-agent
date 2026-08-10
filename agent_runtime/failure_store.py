from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .repair_policy import FailureClassifier, RepairPolicy
from .utils import atomic_write_json, now_iso, stable_hash


class FailureStoreError(RuntimeError):
    pass


class FailureNotFoundError(FailureStoreError):
    pass


class InvalidFailureTransitionError(FailureStoreError):
    pass


class RepairLimitExceededError(FailureStoreError):
    pass


class RepairNotAllowedError(FailureStoreError):
    pass


class FailureStore:
    """Small, process-safe store for repairable graph failures.

    The Blackboard remains the task source of truth. This store only owns the
    lifecycle and repair-round count of one failed step. Protected operations
    are represented as ``waiting_user`` and can never enter ``begin_repair``.
    """

    VALID_STATES = {
        "open",
        "repairing",
        "repaired",
        "waiting_user",
        "blocked",
        "exhausted",
    }

    def __init__(self, root: str | Path, *, max_repair_rounds: int = 3):
        if max_repair_rounds < 1:
            raise ValueError("max_repair_rounds must be at least 1")
        self.root = Path(root)
        self.records_dir = self.root / "records"
        self.locks_dir = self.root / "locks"
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.max_repair_rounds = max_repair_rounds
        self.classifier = FailureClassifier()
        self.policy = RepairPolicy(max_repair_rounds=max_repair_rounds)

    def create(
        self,
        *,
        task_id: str,
        step_id: str,
        error: BaseException | str | dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        classification = self.classifier.classify(error, step_id=step_id, context=context)
        fingerprint = stable_hash(classification.normalized_error)[:20]
        failure_id = stable_hash(
            {
                "task_id": task_id,
                "step_id": step_id,
                "category": classification.category,
                "fingerprint": fingerprint,
            }
        )[:24]
        with self._locked(failure_id):
            path = self._path(failure_id)
            if path.is_file():
                return self._read(path)
            decision = self.policy.decide(classification)
            status = "open"
            if decision.repairability == "approval_required":
                status = "waiting_user"
            elif decision.repairability == "not_repairable":
                status = "blocked"
            timestamp = now_iso()
            record = {
                "schema_version": "1.0",
                "failure_id": failure_id,
                "task_id": task_id,
                "step_id": step_id,
                "category": classification.category,
                "matched_rule": classification.matched_rule,
                "error": classification.normalized_error,
                "error_fingerprint": fingerprint,
                "status": status,
                "repairability": decision.repairability,
                "recommended_action": decision.action,
                "repair_rounds": 0,
                "max_repair_rounds": self.max_repair_rounds,
                "repair_records": [],
                "context": dict(context or {}),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            self._write(record)
            return dict(record)

    def get(self, failure_id: str) -> dict[str, Any]:
        with self._locked(failure_id):
            path = self._path(failure_id)
            if not path.is_file():
                raise FailureNotFoundError(f"failure not found: {failure_id}")
            return self._read(path)

    def list(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        records = [self._read(path) for path in sorted(self.records_dir.glob("*.json"))]
        if task_id is not None:
            records = [record for record in records if record.get("task_id") == task_id]
        return records

    def begin_repair(self, failure_id: str, *, action: str) -> dict[str, Any]:
        with self._locked(failure_id):
            record = self._load_locked(failure_id)
            if record["repairability"] != "automatic":
                raise RepairNotAllowedError(
                    f"failure {failure_id} is {record['repairability']}; protected work must wait for Scheduler approval"
                )
            if action != record["recommended_action"]:
                raise RepairNotAllowedError(
                    f"repair action {action} does not match {record['recommended_action']}"
                )
            if record["status"] not in {"open"}:
                raise InvalidFailureTransitionError(
                    f"cannot begin repair from {record['status']}"
                )
            if int(record["repair_rounds"]) >= self.max_repair_rounds:
                record["status"] = "exhausted"
                record["updated_at"] = now_iso()
                self._write(record)
                raise RepairLimitExceededError(
                    f"repair limit reached ({record['repair_rounds']}/{self.max_repair_rounds})"
                )
            round_number = int(record["repair_rounds"]) + 1
            repair_record = {
                "round": round_number,
                "action": action,
                "status": "running",
                "changed": False,
                "change_refs": [],
                "started_at": now_iso(),
                "finished_at": None,
                "review": None,
                "reason": "",
            }
            record["repair_rounds"] = round_number
            record["repair_records"].append(repair_record)
            record["status"] = "repairing"
            record["updated_at"] = now_iso()
            self._write(record)
            return dict(record)

    def complete_repair(
        self,
        failure_id: str,
        *,
        changed: bool,
        change_refs: list[str] | None = None,
        review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._locked(failure_id):
            record = self._load_locked(failure_id)
            if record["status"] != "repairing":
                raise InvalidFailureTransitionError(
                    f"cannot complete repair from {record['status']}"
                )
            current = record["repair_records"][-1]
            current.update(
                {
                    "status": "passed" if changed else "failed",
                    "changed": bool(changed),
                    "change_refs": [str(item) for item in (change_refs or [])],
                    "finished_at": now_iso(),
                    "review": dict(review or {}),
                    "reason": "" if changed else "repair made no verifiable change",
                }
            )
            if changed:
                record["status"] = "repaired"
            elif int(record["repair_rounds"]) >= self.max_repair_rounds:
                record["status"] = "exhausted"
            else:
                record["status"] = "open"
            record["updated_at"] = now_iso()
            self._write(record)
            return dict(record)

    def fail_repair(self, failure_id: str, *, reason: str) -> dict[str, Any]:
        with self._locked(failure_id):
            record = self._load_locked(failure_id)
            if record["status"] != "repairing":
                raise InvalidFailureTransitionError(
                    f"cannot fail repair from {record['status']}"
                )
            current = record["repair_records"][-1]
            current.update(
                {
                    "status": "failed",
                    "finished_at": now_iso(),
                    "reason": str(reason),
                }
            )
            record["status"] = (
                "exhausted"
                if int(record["repair_rounds"]) >= self.max_repair_rounds
                else "open"
            )
            record["updated_at"] = now_iso()
            self._write(record)
            return dict(record)

    def mark_waiting_user(self, failure_id: str, *, reason: str) -> dict[str, Any]:
        return self._terminal_transition(failure_id, "waiting_user", reason)

    def block(self, failure_id: str, *, reason: str) -> dict[str, Any]:
        return self._terminal_transition(failure_id, "blocked", reason)

    def reopen_after_replay_failure(self, failure_id: str, *, reason: str) -> dict[str, Any]:
        with self._locked(failure_id):
            record = self._load_locked(failure_id)
            if record["status"] != "repaired":
                raise InvalidFailureTransitionError(
                    f"cannot reopen failure from {record['status']}"
                )
            record["status"] = (
                "exhausted"
                if int(record["repair_rounds"]) >= self.max_repair_rounds
                else "open"
            )
            record["last_replay_failure"] = str(reason)
            record["updated_at"] = now_iso()
            self._write(record)
            return dict(record)

    def _terminal_transition(self, failure_id: str, status: str, reason: str) -> dict[str, Any]:
        if status not in {"waiting_user", "blocked"}:
            raise ValueError(status)
        with self._locked(failure_id):
            record = self._load_locked(failure_id)
            if record["status"] in {"repairing", "repaired", "exhausted"}:
                raise InvalidFailureTransitionError(
                    f"cannot set {status} from {record['status']}"
                )
            record["status"] = status
            record["terminal_reason"] = str(reason)
            record["updated_at"] = now_iso()
            self._write(record)
            return dict(record)

    def _path(self, failure_id: str) -> Path:
        if not failure_id or any(char not in "0123456789abcdef" for char in failure_id):
            raise ValueError("failure_id must be a lowercase hexadecimal stable ID")
        return self.records_dir / f"{failure_id}.json"

    def _load_locked(self, failure_id: str) -> dict[str, Any]:
        path = self._path(failure_id)
        if not path.is_file():
            raise FailureNotFoundError(f"failure not found: {failure_id}")
        return self._read(path)

    def _read(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            record = json.load(handle)
        if not isinstance(record, dict):
            raise FailureStoreError(f"failure record must be an object: {path}")
        if record.get("schema_version") != "1.0":
            raise FailureStoreError(f"unsupported failure schema: {path}")
        if record.get("status") not in self.VALID_STATES:
            raise FailureStoreError(f"invalid failure status: {path}")
        if int(record.get("max_repair_rounds", -1)) != self.max_repair_rounds:
            raise FailureStoreError(f"failure max_repair_rounds mismatch: {path}")
        return record

    def _write(self, record: dict[str, Any]) -> None:
        atomic_write_json(self._path(str(record["failure_id"])), record)

    @contextmanager
    def _locked(self, failure_id: str) -> Iterator[None]:
        lock_path = self.locks_dir / f"{failure_id}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows is the production host
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
