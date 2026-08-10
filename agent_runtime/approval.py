from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .utils import now_iso, stable_hash


@dataclass
class ApprovalRequest:
    approval_id: str
    task_id: str
    reason: str
    status: str
    created_at: str
    expires_at: str
    timeout_count: int = 0


class ApprovalManager:
    def __init__(self, ttl_hours: int = 24, abandoned_after: int = 3):
        """初始化当前对象依赖和运行参数。"""
        self.ttl_hours = ttl_hours
        self.abandoned_after = abandoned_after
        self.requests: dict[str, ApprovalRequest] = {}

    def create(self, task_id: str, reason: str) -> ApprovalRequest:
        """创建新的领域对象或请求记录。"""
        created = datetime.now(timezone.utc)
        approval_id = stable_hash({"task_id": task_id, "reason": reason, "created_at": created.isoformat()})[:16]
        request = ApprovalRequest(
            approval_id=approval_id,
            task_id=task_id,
            reason=reason,
            status="waiting_approval",
            created_at=created.isoformat(),
            expires_at=(created + timedelta(hours=self.ttl_hours)).isoformat(),
        )
        self.requests[approval_id] = request
        return request

    def expire_due(self, now: datetime | None = None) -> list[ApprovalRequest]:
        """过期超过有效期的审批请求。"""
        now = now or datetime.now(timezone.utc)
        expired = []
        for request in self.requests.values():
            if request.status != "waiting_approval":
                continue
            if datetime.fromisoformat(request.expires_at) <= now:
                request.timeout_count += 1
                request.status = "abandoned" if request.timeout_count >= self.abandoned_after else "blocked"
                expired.append(request)
        return expired
