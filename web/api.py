from __future__ import annotations

import json

from agent_runtime.audit import AuditLog
from agent_runtime.approval import ApprovalManager
from agent_runtime.blackboard import Blackboard
from agent_runtime.capability_registry import CapabilityRegistry
from agent_runtime.checkpoint import CheckpointManager
from agent_runtime.config import load_settings
from agent_runtime.embedding import EmbeddingClient
from agent_runtime.maintenance import MaintenanceManager
from agent_runtime.memory import MemoryManager
from agent_runtime.runtime_llm_config import runtime_llm_config
from agent_runtime.redis_store import RedisStore
from agent_runtime.scheduler import Scheduler
from agent_runtime.streaming import StreamPublisher

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse
except Exception:  # pragma: no cover
    FastAPI = None
    JSONResponse = None


settings = load_settings()
runtime_llm_config.load_from_settings(settings)
store = RedisStore(
    settings.redis_url,
    json_retention_days=settings.redis_json_retention_days,
    stream_retention_days=settings.redis_stream_retention_days,
    vector_retention_days=settings.redis_vector_retention_days,
)
checkpoint = CheckpointManager(settings.redis_url, store)
embedder = EmbeddingClient(
    provider=settings.embedding_provider,
    base_url=settings.embedding_base_url,
    api_key_env=settings.embedding_api_key_env,
    model_name=settings.embedding_model_name,
    dimensions=settings.embedding_dimensions,
    timeout_seconds=settings.embedding_timeout_seconds,
).embed_text
registry = CapabilityRegistry()
registry.scan({
    "antenna_skills_root": settings.antenna_skills_root,
    "e_platform_root": settings.e_platform_root,
    "paperwise_root": settings.paperwise_root,
    "ieee_harvester_root": settings.ieee_harvester_root,
})
blackboard = Blackboard(settings.workspace_root)
memory = MemoryManager(l1_recent_turns=settings.l1_recent_turns, store=store, embedder=embedder)
audit = AuditLog(settings.logs_root, store=store)
stream = StreamPublisher()
approval_manager = ApprovalManager(settings.approval_ttl_hours, settings.approval_abandoned_after_timeouts)
scheduler = Scheduler(settings, blackboard, memory, registry, audit, stream, checkpoint)


if FastAPI is not None:
    app = FastAPI(title="antenna_agent_lab")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(FileNotFoundError)
    def file_not_found_handler(_request, exc: FileNotFoundError):
        """Return a clear 404 for missing task snapshots or requested files."""
        return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)

    @app.get("/health")
    def health():
        """返回当前组件健康状态。"""
        redis_health = store.health()
        return {"status": "ok", "mode": "dynamic_runtime", "redis": redis_health, "checkpoint": checkpoint.health()}

    @app.get("/capabilities")
    def capabilities():
        """执行当前函数对应的业务逻辑。"""
        return registry.snapshot()

    @app.get("/settings/llm-runtime")
    def get_runtime_llm_settings():
        """Return runtime LLM settings without exposing the API key."""
        return runtime_llm_config.public_state()

    @app.post("/settings/llm-runtime")
    def set_runtime_llm_settings(payload: dict):
        """Set process-local LLM settings from the frontend."""
        enabled = bool(payload.get("enabled", True))
        base_url = str(payload.get("base_url") or "").strip()
        model_name = str(payload.get("model_name") or "").strip()
        embedding_model_name = str(payload.get("embedding_model_name") or runtime_llm_config.embedding_model_name or "text-embedding-v3").strip()
        api_key = str(payload.get("api_key") or "").strip()
        if enabled:
            if not base_url.startswith(("http://", "https://")):
                return JSONResponse({"error": "invalid_base_url", "message": "base_url must start with http:// or https://"}, status_code=400)
            if not model_name:
                return JSONResponse({"error": "missing_model_name", "message": "model_name is required"}, status_code=400)
            if not api_key and not runtime_llm_config.api_key:
                return JSONResponse({"error": "missing_api_key", "message": "api_key is required the first time runtime LLM is enabled"}, status_code=400)
        runtime_llm_config.enabled = enabled
        runtime_llm_config.base_url = base_url
        runtime_llm_config.model_name = model_name
        runtime_llm_config.embedding_model_name = embedding_model_name
        runtime_llm_config.source = "frontend_runtime"
        if api_key:
            runtime_llm_config.api_key = api_key
        return runtime_llm_config.public_state()

    @app.get("/tasks")
    def list_tasks():
        """列出当前工作区中的任务摘要。"""
        if hasattr(blackboard, "list_tasks"):
            return blackboard.list_tasks()
        return [record.to_dict() for record in blackboard.records.values()]

    @app.post("/tasks/recover")
    def recover_tasks():
        """恢复工作区中未完成的任务。"""
        return scheduler.recover_tasks()

    @app.post("/tasks")
    def create_general_task(payload: dict):
        """执行当前函数对应的业务逻辑。"""
        result = scheduler.create_task_from_request(
            payload["user_input"],
            external_llm_approved=False,
            modeling_request=payload.get("modeling_request"),
        )
        return JSONResponse(result)

    @app.post("/tasks/real-cst-single-run")
    def create_real_cst_single_run(payload: dict):
        """Create a V2.0 real CST single-run task and stop at approval."""
        try:
            return JSONResponse(scheduler.create_real_cst_single_run_task(payload))
        except Exception as exc:
            return JSONResponse({"error": "real_cst_task_create_failed", "message": str(exc)}, status_code=500)

    @app.post("/tasks/{task_id}/approve")
    def approve_task(task_id: str):
        """Approve a V2.0 real CST task."""
        try:
            return scheduler.approve_real_cst_task(task_id)
        except RuntimeError as exc:
            return JSONResponse({"error": "task_not_approvable", "message": str(exc)}, status_code=409)
        except Exception as exc:
            return JSONResponse({"error": "approval_failed", "message": str(exc)}, status_code=500)

    @app.post("/tasks/{task_id}/reject")
    def reject_task(task_id: str):
        """Reject a V2.0 real CST task."""
        try:
            return scheduler.reject_real_cst_task(task_id, "user_rejected_real_cst")
        except RuntimeError as exc:
            return JSONResponse({"error": "task_not_rejectable", "message": str(exc)}, status_code=409)
        except Exception as exc:
            return JSONResponse({"error": "rejection_failed", "message": str(exc)}, status_code=500)

    @app.get("/tasks/{task_id}/available-actions")
    def task_available_actions(task_id: str):
        """Return front-end actions for a task."""
        return scheduler.available_actions(task_id)

    @app.post("/tasks/{task_id}/central-message")
    def task_central_message(task_id: str, payload: dict):
        """Record a frontend message for the central agent."""
        try:
            return scheduler.send_central_message(task_id, payload)
        except FileNotFoundError as exc:
            return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)
        except RuntimeError as exc:
            return JSONResponse({"error": "central_message_rejected", "message": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": "central_message_failed", "message": str(exc)}, status_code=500)

    @app.get("/tasks/{task_id}/artifacts")
    def task_artifacts(task_id: str):
        """Return artifact references for a task."""
        return scheduler.task_artifacts(task_id)

    @app.get("/tasks/{task_id}/logs")
    def task_logs(task_id: str):
        """Return task audit/log events."""
        return scheduler.task_logs(task_id)

    @app.get("/tasks/{task_id}/reports")
    def task_reports(task_id: str):
        """Return report references for a task."""
        return scheduler.task_reports(task_id)

    @app.post("/tasks/{task_id}/paperwise/review")
    def rerun_paperwise_review(task_id: str):
        """Re-run PaperWise evidence review for an existing task."""
        try:
            return scheduler.rerun_paperwise_evidence_review(task_id)
        except RuntimeError as exc:
            return JSONResponse({"error": "paperwise_review_not_available", "message": str(exc)}, status_code=409)
        except Exception as exc:
            return JSONResponse({"error": "paperwise_review_failed", "message": str(exc)}, status_code=500)

    @app.post("/tasks/{task_id}/resume")
    def resume_task(task_id: str):
        """执行当前函数对应的业务逻辑。"""
        try:
            return scheduler.resume_approved_task(task_id)
        except PermissionError as exc:
            return JSONResponse({"error": "approval_required", "message": str(exc)}, status_code=403)
        except RuntimeError as exc:
            return JSONResponse({"error": "task_not_resumable", "message": str(exc)}, status_code=409)

    @app.get("/approvals")
    def list_approvals():
        """执行当前函数对应的业务逻辑。"""
        approvals = {request.approval_id: request.__dict__ for request in approval_manager.requests.values()}
        for record in blackboard.list_tasks():
            for approval_id, approval in record.get("approvals", {}).items():
                approvals.setdefault(approval_id, _approval_card(record, approval))
        return list(approvals.values())

    @app.get("/tasks/{task_id}/approval-card")
    def approval_card(task_id: str):
        """执行当前函数对应的业务逻辑。"""
        record = blackboard.get(task_id).to_dict()
        approval = next(iter(record.get("approvals", {}).values()), {})
        return _approval_card(record, approval)

    @app.post("/tasks/{task_id}/approvals")
    def create_approval(task_id: str, payload: dict):
        """执行当前函数对应的业务逻辑。"""
        request = approval_manager.create(task_id, payload.get("reason", "manual approval requested"))
        record = blackboard.get(task_id)
        record.approvals[request.approval_id] = request.__dict__
        blackboard.update(task_id, state="waiting_approval", approvals=record.approvals)
        return request.__dict__

    @app.post("/approvals/{approval_id}/{decision}")
    def decide_approval(approval_id: str, decision: str):
        """执行当前函数对应的业务逻辑。"""
        decision_map = {
            "approve": "approved",
            "approved": "approved",
            "reject": "rejected",
            "rejected": "rejected",
            "request_repair": "request_repair",
            "downgrade_to_mock": "downgrade_to_mock",
            "terminate": "terminated",
        }
        if decision not in decision_map:
            return JSONResponse({"error": "invalid_decision"}, status_code=400)
        status = decision_map[decision]
        request = approval_manager.requests.get(approval_id)
        if request is None:
            for record_data in blackboard.list_tasks():
                approvals = record_data.get("approvals", {})
                if approval_id not in approvals:
                    continue
                record = blackboard.get(record_data["task_id"])
                approval = dict(approvals[approval_id])
                approval["status"] = status
                record.approvals[approval_id] = approval
                next_state = _approval_next_state(status)
                blackboard.update(record.task_id, state=next_state, approvals=record.approvals)
                if approval_id.startswith("real_cst:"):
                    try:
                        if status == "approved":
                            return scheduler.approve_real_cst_task(record.task_id)
                        if status in {"rejected", "terminated"}:
                            return scheduler.reject_real_cst_task(record.task_id, f"approval_{status}")
                    except Exception as exc:
                        return JSONResponse({"error": "real_cst_approval_failed", "message": str(exc)}, status_code=409)
                if approval_id.startswith("external_llm:") and status == "approved":
                    try:
                        return scheduler.resume_approved_task(record.task_id)
                    except Exception as exc:
                        return blackboard.update(
                            record.task_id,
                            state="failed",
                            blockers=record.blockers + [{"type": "resume_failed", "reason": str(exc)}],
                        ).to_dict()
                return approval
            return JSONResponse({"error": "not_found"}, status_code=404)
        request.status = status
        record = blackboard.get(request.task_id)
        record.approvals[approval_id] = request.__dict__
        next_state = _approval_next_state(status)
        blackboard.update(request.task_id, state=next_state, approvals=record.approvals)
        if approval_id.startswith("real_cst:"):
            try:
                if status == "approved":
                    return scheduler.approve_real_cst_task(request.task_id)
                if status in {"rejected", "terminated"}:
                    return scheduler.reject_real_cst_task(request.task_id, f"approval_{status}")
            except Exception as exc:
                return JSONResponse({"error": "real_cst_approval_failed", "message": str(exc)}, status_code=409)
        if approval_id.startswith("external_llm:") and status == "approved":
            try:
                return scheduler.resume_approved_task(request.task_id)
            except Exception as exc:
                return blackboard.update(
                    request.task_id,
                    state="failed",
                    blockers=record.blockers + [{"type": "resume_failed", "reason": str(exc)}],
                ).to_dict()
        return request.__dict__

    @app.post("/approvals/expire")
    def expire_approvals():
        """执行当前函数对应的业务逻辑。"""
        expired = approval_manager.expire_due()
        for request in expired:
            try:
                record = blackboard.get(request.task_id)
                record.approvals[request.approval_id] = request.__dict__
                blackboard.update(request.task_id, state="failed", approvals=record.approvals)
            except Exception:
                pass
        return [request.__dict__ for request in expired]

    @app.post("/tasks/paper-plan")
    def create_paper_plan(payload: dict):
        """执行当前函数对应的业务逻辑。"""
        result = scheduler.create_paper_plan_task(
            payload["user_input"],
            payload.get("paper_report_path"),
            external_llm_approved=False,
        )
        return JSONResponse(result)

    @app.get("/tasks/{task_id}/state")
    def task_state(task_id: str):
        """执行当前函数对应的业务逻辑。"""
        return blackboard.get(task_id).to_dict()

    @app.get("/tasks/{task_id}/events")
    def task_events(task_id: str, after_sequence_id: str | None = None):
        """执行当前函数对应的业务逻辑。"""
        events = audit.replay(task_id, after_sequence_id)
        return events if events else stream.replay(task_id, after_sequence_id)

    @app.get("/tasks/{task_id}/events/stream")
    def task_events_stream(task_id: str, after_sequence_id: str | None = None):
        """执行当前函数对应的业务逻辑。"""
        import json
        import time

        def event_source():
            """执行当前函数对应的业务逻辑。"""
            last_id = after_sequence_id
            while True:
                events = stream.replay(task_id, last_id)
                if not events:
                    events = audit.replay(task_id, last_id)
                for event in events:
                    last_id = event.get("sequence_id", last_id)
                    yield f"id: {last_id}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                heartbeat = {"type": "heartbeat", "task_id": task_id}
                yield f"event: heartbeat\ndata: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
                time.sleep(15)

        return StreamingResponse(event_source(), media_type="text/event-stream")

    @app.get("/memory")
    def memory_snapshot():
        """执行当前函数对应的业务逻辑。"""
        if hasattr(memory, "snapshot"):
            return memory.snapshot()
        return {
            "l1_sessions": len(memory.l1),
        }

    @app.post("/maintenance/audit")
    def run_audit_maintenance(payload: dict | None = None):
        """执行审计日志维护并返回结果摘要。"""
        manager = MaintenanceManager(settings.logs_root, store=store)
        body = payload or {}
        dry_run = bool(body.get("dry_run", True))
        if not dry_run and body.get("confirm") != "run_audit_maintenance":
            return JSONResponse({"error": "confirmation_required", "dry_run": True}, status_code=409)
        return manager.run_audit_maintenance(dry_run=dry_run)

    @app.get("/paperwise/reports")
    def paperwise_reports(limit: int = 50):
        """执行当前函数对应的业务逻辑。"""
        from adapters.paperwise_adapter import PaperWiseAdapter

        adapter = PaperWiseAdapter(settings.paperwise_root)
        if hasattr(adapter, "list_reports"):
            return adapter.list_reports(limit=limit)
        return []

    @app.get("/paperwise/report")
    def paperwise_report(path: str | None = None):
        """执行当前函数对应的业务逻辑。"""
        from adapters.paperwise_adapter import PaperWiseAdapter

        adapter = PaperWiseAdapter(settings.paperwise_root)
        if hasattr(adapter, "read_report"):
            return adapter.read_report(path)
        return {"available": False, "error": "read_report_not_supported"}
else:
    app = None


def _approval_next_state(status: str) -> str:
    """执行当前模块的内部辅助逻辑。"""
    if status == "approved":
        return "waiting_approval"
    if status == "request_repair":
        return "failed"
    if status == "downgrade_to_mock":
        return "waiting_approval"
    if status == "terminated":
        return "failed"
    return "failed"


def _approval_card(record: dict, approval: dict) -> dict:
    """执行当前模块的内部辅助逻辑。"""
    task_metadata = record.get("task_metadata", {})
    artifacts = list(task_metadata.get("artifacts") or [])
    reviewer_findings = []
    reviews = [
        *(task_metadata.get("module_review_records") or []),
        *(task_metadata.get("global_review_records") or []),
    ]
    for review in reviews:
        output = review.get("output") if isinstance(review, dict) else None
        review_data = output if isinstance(output, dict) else review
        if isinstance(review_data, dict):
            reviewer_findings.extend(review_data.get("blocking_findings") or [])
    data_egress_flags = []
    for blocker in record.get("blockers", []):
        if blocker.get("data_egress"):
            data_egress_flags.append(blocker["data_egress"])
    live_cst_flags = []
    for artifact in artifacts:
        metadata = artifact.get("metadata", {})
        if metadata.get("real_cst_execution") is True:
            live_cst_flags.append({"artifact": artifact})
    return {
        **approval,
        "task_id": record.get("task_id"),
        "current_phase": task_metadata.get("current_stage") or record.get("state") or "created",
        "proposed_next_phase": "human_review_or_v2_execution" if record.get("state") == "completed" else "repair_or_resume",
        "artifacts": artifacts,
        "reviewer_findings": reviewer_findings,
        "blockers": record.get("blockers", []),
        "data_egress_flags": data_egress_flags,
        "live_cst_flags": live_cst_flags,
        "allowed_actions": ["approve", "reject", "request_repair", "downgrade_to_mock", "terminate"],
    }
