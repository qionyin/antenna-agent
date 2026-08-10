from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .redis_store import RedisStore
from .utils import ensure_dir, now_iso, redact_text


@dataclass
class MaintenanceResult:
    operation: str
    started_at: str
    ended_at: str
    success: bool
    updated: list[str]
    skipped: list[str]
    failed: list[dict[str, Any]]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """将当前对象转换为可序列化字典。"""
        return {
            "schema_version": "1.0",
            "operation": self.operation,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "success": self.success,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "summary": self.summary,
        }


class MaintenanceManager:
    """Safe local maintenance for audit streams and JSONL cold logs."""

    def __init__(
        self,
        logs_root: str | Path,
        store: RedisStore | None = None,
        hot_ttl_days: int = 30,
        cold_keep_days: int = 365,
        max_stream_events: int = 1000,
    ) -> None:
        """初始化当前对象依赖和运行参数。"""
        self.logs_root = Path(logs_root)
        self.store = store
        self.hot_ttl_days = hot_ttl_days
        self.cold_keep_days = cold_keep_days
        self.max_stream_events = max_stream_events

    def run_audit_maintenance(self, dry_run: bool = False) -> dict[str, Any]:
        """执行审计日志维护并返回结果摘要。"""
        started = now_iso()
        updated: list[str] = []
        skipped: list[str] = []
        failed: list[dict[str, Any]] = []

        if self.store is None:
            skipped.append("redis_store_unavailable")
        else:
            ttl_seconds = self.hot_ttl_days * 24 * 60 * 60
            for key in self.store.keys("audit:event:*"):
                try:
                    if dry_run:
                        skipped.append(f"would_expire_trim:{key}")
                    else:
                        self.store.expire(key, ttl_seconds)
                        self.store.xtrim(key, self.max_stream_events)
                        updated.append(key)
                except Exception as exc:
                    failed.append({"path": key, "error_type": "unknown_error", "error": str(exc)})

        cold = self._archive_old_cold_logs(dry_run)
        updated.extend(cold["updated"])
        skipped.extend(cold["skipped"])
        failed.extend(cold["failed"])

        result = MaintenanceResult(
            operation="audit_maintenance",
            started_at=started,
            ended_at=now_iso(),
            success=not failed,
            updated=updated,
            skipped=skipped,
            failed=failed,
            summary={
                "dry_run": dry_run,
                "redis_streams_touched": len([item for item in updated if item.startswith("audit:event:")]),
                "cold_logs_touched": len([item for item in updated if item.endswith(".jsonl")]),
                "failed": len(failed),
            },
        ).to_dict()
        if not dry_run:
            self._write_manifest(result)
        return result

    def _archive_old_cold_logs(self, dry_run: bool) -> dict[str, Any]:
        """归档超过保留期的冷审计日志。"""
        audit_dir = self.logs_root / "audit"
        updated: list[str] = []
        skipped: list[str] = []
        failed: list[dict[str, Any]] = []
        if not audit_dir.exists():
            skipped.append(str(audit_dir))
            return {"updated": updated, "skipped": skipped, "failed": failed}

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cold_keep_days)
        archive_dir = self.logs_root / "audit_archive"
        for path in audit_dir.glob("*.jsonl"):
            try:
                log_date = datetime.fromisoformat(path.stem).replace(tzinfo=timezone.utc)
            except ValueError:
                skipped.append(str(path))
                continue
            if log_date >= cutoff:
                skipped.append(str(path))
                continue
            if dry_run:
                skipped.append(f"would_archive:{path}")
                continue
            ensure_dir(archive_dir)
            target = archive_dir / path.name
            path.replace(target)
            updated.append(str(target))
        return {"updated": updated, "skipped": skipped, "failed": failed}

    def _write_manifest(self, result: dict[str, Any]) -> None:
        """写入维护操作的结果清单。"""
        safe_time = now_iso().replace(":", "-")
        target = ensure_dir(self.logs_root / "maintenance") / f"{safe_time}.json"
        with target.open("w", encoding="utf-8") as handle:
            json.dump(_redact_obj(result), handle, ensure_ascii=False, indent=2)
            handle.write("\n")


def _redact_obj(value: Any) -> Any:
    """递归脱敏对象中的敏感字段。"""
    if isinstance(value, dict):
        return {key: _redact_obj(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_obj(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
