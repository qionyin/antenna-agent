from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .blackboard import Blackboard
from .capability_registry import CapabilityRegistry
from .checkpoint import CheckpointManager
from .config import Settings
from .llm import LLMClient
from .langgraph_runtime import DynamicLangGraphRuntime
from .failure_store import FailureStore
from .memory import MemoryManager
from .retrieval import RetrievalEngine
from .runtime_llm_config import runtime_llm_config
from .skill_router import SkillRouter
from .skill_executor_gate import SkillExecutorGate
from .streaming import StreamPublisher
from .subagents import SubagentRuntime
from .utils import atomic_write_json, now_iso, stable_hash
from adapters.mcp_wrappers import MCPAntennaSkillsAdapter, MCPCstRealRunAdapter, MCPModelingPreparationAdapter
from adapters.paperwise_adapter import PaperWiseAdapter


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        blackboard: Blackboard,
        memory: MemoryManager,
        registry: CapabilityRegistry,
        audit: AuditLog,
        stream: StreamPublisher,
        checkpoint: CheckpointManager | None = None,
        langgraph_checkpointer: Any | None = None,
    ):
        """初始化当前对象依赖和运行参数。"""
        self.settings = settings
        self.blackboard = blackboard
        self.memory = memory
        self.registry = registry
        self.audit = audit
        self.stream = stream
        self.checkpoint = checkpoint
        self.retrieval = RetrievalEngine(
            memory,
            minimum_relevance_scores={
                "l2": settings.l2_min_relevance_score,
                "l3": settings.l3_min_relevance_score,
            },
        )
        self.skill_router = SkillRouter()
        self.skill_executor_gate = SkillExecutorGate()
        self.antenna_skills = MCPAntennaSkillsAdapter(settings.antenna_skills_root)
        self.modeling_preparation = MCPModelingPreparationAdapter(
            skill_root=Path(settings.antenna_skills_root) / "antenna-research-ideation",
            timeout_seconds=settings.agent_timeout_seconds,
        )
        self.failure_store = FailureStore(
            Path(settings.workspace_root) / "failures",
            max_repair_rounds=settings.max_auto_fix_rounds,
        )
        self.subagents = SubagentRuntime()
        self.llm = LLMClient(settings.llm_base_url, settings.llm_api_key_env, settings.llm_model_name, enabled=settings.llm_enabled)
        self.paperwise = PaperWiseAdapter(settings.paperwise_root)
        self.cst_real = MCPCstRealRunAdapter(
            e_platform_root=settings.e_platform_root,
            e_results_root=settings.e_results_root,
        )
        self._external_llm_approved_tasks: set[str] = set()
        self._real_cst_lock = threading.Lock()
        self._real_cst_running: set[str] = set()
        self.langgraph_runtime = DynamicLangGraphRuntime(
            self,
            settings.workspace_root,
            checkpointer=langgraph_checkpointer,
        )

    def create_paper_plan_task(
        self,
        user_input: str,
        paper_report_path: str | None = None,
        external_llm_approved: bool = False,
    ) -> dict[str, Any]:
        """创建论文计划类任务。"""
        return self._create_dynamic_task(user_input, paper_report_path, external_llm_approved, require_paperwise=True)

    def create_task_from_request(
        self,
        user_input: str,
        external_llm_approved: bool = False,
        modeling_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """根据自然语言请求创建通用任务。"""
        return self._create_dynamic_task(
            user_input,
            None,
            external_llm_approved,
            require_paperwise=bool(modeling_request),
            modeling_request=modeling_request,
        )

    def _create_dynamic_task(
        self,
        user_input: str,
        paper_report_path: str | None,
        external_llm_approved: bool,
        require_paperwise: bool,
        modeling_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a V2.2.3 dynamic-plan task without forcing seven packets."""
        snapshot = self.registry.save_locked_snapshot(self.settings.workspace_root)
        mode = "real" if modeling_request else "mock"
        record = self.blackboard.create_task(
            user_input,
            capability_snapshot_id=snapshot["snapshot_id"],
            task_metadata={
                "user_input": user_input,
                "mode": mode,
                "runtime_kind": "dynamic",
                "task_title": self._default_dynamic_title(user_input),
                "current_stage": "dynamic_plan",
                "current_task_subagent": None,
                "current_review_subagent": None,
                "paper_report_path": paper_report_path,
                "modeling_request": dict(modeling_request or {}),
                "require_paperwise": require_paperwise,
                "retrieval_context": self._retrieval_context(user_input),
                "capability_snapshot_path": snapshot["snapshot_path"],
                "capability_snapshot_lock_path": snapshot["lock_path"],
                "dynamic_plan_history": [],
                "skill_route_plan_history": [],
                "step_skill_contexts": [],
                "subagent_records": [],
                "central_decisions": [],
                "global_review_records": [],
                "module_review_records": [],
                "reflection_records": [],
                "artifacts": [],
            },
        )
        task_id = record.task_id
        self._write_capability_snapshot(task_id, snapshot)
        self.memory.add_l1_turn(task_id, "user", user_input)
        self._event(task_id, "central_agent", "status", "pending", "V2.2.3 dynamic task created")
        if external_llm_approved and not self._has_approved_external_llm(task_id):
            self._event(task_id, "central_agent", "status", "pending", "Ignored untrusted external LLM approval flag")
        execution_result = self.langgraph_runtime.start(
            task_id=task_id,
            user_input=user_input,
            mode=mode,
            require_paperwise=require_paperwise,
            capability_snapshot=snapshot,
        )
        return self._apply_dynamic_execution_result(task_id, execution_result)

    def _apply_dynamic_execution_result(self, task_id: str, execution_result: dict[str, Any]) -> dict[str, Any]:
        """Project one end-to-end graph result onto the public task state."""
        if execution_result["final_decision"] in {"block_task", "reroute_plan", "revise_current_step"}:
            blockers = list(self.blackboard.get(task_id).blockers)
            if not any(item.get("reason") == execution_result["reason"] for item in blockers):
                blockers.append({"type": execution_result["final_decision"], "reason": execution_result["reason"]})
            self.blackboard.update(task_id, state="failed", blockers=blockers)
            self._set_v2_metadata(task_id, current_stage=execution_result["final_stage"], failure_reason=execution_result["reason"])
            self._event(task_id, "central_agent", "review", "revoked", "V2.2.4 dynamic task blocked", reason=execution_result["reason"])
        elif execution_result["final_decision"] == "wait_user":
            self.blackboard.update(task_id, state="waiting_approval")
            self._set_v2_metadata(task_id, current_stage=execution_result["final_stage"])
            self._event(task_id, "central_agent", "status", "pending", "V2.2.4 dynamic task waits for user")
        elif execution_result["final_decision"] == "refresh_skill_route":
            self.blackboard.update(task_id, state="failed", blockers=list(self.blackboard.get(task_id).blockers) + [{"type": "skill_route_refreshed", "reason": execution_result["reason"]}])
            self._set_v2_metadata(task_id, current_stage=execution_result["final_stage"], failure_reason=execution_result["reason"])
            self._event(task_id, "central_agent", "review", "pending", "V2.2.4 refreshed route; task needs rerun")
        else:
            self.blackboard.update(task_id, state="completed")
            self._set_v2_metadata(task_id, current_stage="completed")
            self._event(task_id, "central_agent", "final", "finalized", "V2.2.4 dynamic task completed")
        return self.blackboard.get(task_id).to_dict()

    def resume_approved_task(self, task_id: str) -> dict[str, Any]:
        """在审批通过后恢复阻塞任务。"""
        record = self.blackboard.get(task_id)
        if record.state != "waiting_approval":
            raise RuntimeError(f"task is not waiting for approval: {record.state}")
        if not self._has_approved_external_llm(task_id):
            raise PermissionError("external LLM approval is not approved on the blackboard")
        self._external_llm_approved_tasks.add(task_id)
        blockers = [blocker for blocker in record.blockers if blocker.get("type") != "external_llm_approval_required"]
        self.blackboard.update(task_id, state="running", blockers=blockers)
        execution_result = self.langgraph_runtime.resume(task_id, {"approved": True, "approval_type": "external_llm"})
        return self._apply_dynamic_execution_result(task_id, execution_result)

    def create_real_cst_single_run_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a V2.0 real-CST task and stop before the solver approval gate."""
        user_input = str(payload.get("user_input") or payload.get("task_goal") or "真实 CST 单次运行")
        task_title = str(payload.get("task_title") or self._default_v2_title(user_input))
        request = {
            "user_input": user_input,
            "task_title": task_title,
            "project_path": payload.get("project_path"),
            "model_json_path": payload.get("model_json_path"),
            "fixture_result_dir": payload.get("fixture_result_dir"),
            "parameters": payload.get("parameters") or {},
            "target_metric": payload.get("target_metric", "S11"),
            "target_freq_range": payload.get("target_freq_range") or payload.get("target_band"),
            "s11_threshold_db": payload.get("s11_threshold_db", -10.0),
            "cst_timeout_seconds": payload.get("cst_timeout_seconds", 7200),
            "cst_s11_tree_item": payload.get("cst_s11_tree_item", r"1D Results\S-Parameters\S1,1"),
            "cst_use_active_project": payload.get("cst_use_active_project", True),
            "cst_save_project": payload.get("cst_save_project", False),
            "simulate_cst": bool(payload.get("simulate_cst", False)),
        }
        snapshot = self.registry.save_locked_snapshot(self.settings.workspace_root)
        record = self.blackboard.create_task(
            user_input,
            capability_snapshot_id=snapshot["snapshot_id"],
            task_metadata={
                "mode": "real",
                "runtime_kind": "dynamic",
                "task_title": task_title,
                "current_stage": "created",
                "current_task_subagent": None,
                "current_review_subagent": None,
                "cst_status": "not_reached",
                "request": request,
                "paper_report_path": payload.get("paper_report_path"),
                "require_paperwise": True,
                "artifacts": [],
                "reports": [],
                "reviews": [],
                "results": {},
                "subagent_records": [],
                "module_review_records": [],
                "global_review_records": [],
                "central_decisions": [],
                "step_skill_contexts": [],
                "evidence_pool_summary": {},
                "capability_snapshot_path": snapshot["snapshot_path"],
                "capability_snapshot_lock_path": snapshot["lock_path"],
            },
        )
        task_id = record.task_id
        self._write_capability_snapshot(task_id, snapshot)
        self.memory.add_l1_turn(task_id, "user", user_input)
        self._event(task_id, "central_agent", "status", "pending", "V2.0 real CST task created")
        self.blackboard.update(task_id, state="preflight_running")
        execution_result = self.langgraph_runtime.start(
            task_id=task_id,
            user_input=user_input,
            mode="real",
            require_paperwise=True,
            capability_snapshot=snapshot,
        )
        return self._apply_real_execution_result(task_id, execution_result)

    def approve_real_cst_task(self, task_id: str) -> dict[str, Any]:
        """Approve one bound dynamic CST step and continue the dynamic chain once."""
        record = self.blackboard.get(task_id)
        if record.task_metadata.get("mode") != "real":
            raise RuntimeError("task is not a real CST task")
        if record.state == "completed":
            return record.to_dict()
        if record.state != "waiting_approval":
            raise RuntimeError(f"task is not waiting for real CST approval: {record.state}")
        approval_id = f"real_cst:{task_id}"
        approvals = dict(record.approvals)
        approval = dict(approvals.get(approval_id) or {})
        request = dict(self.blackboard.get(task_id).task_metadata.get("request") or {})
        plan = dict(self.blackboard.get(task_id).task_metadata.get("dynamic_plan") or {})
        expected = {
            "task_id": task_id,
            "request_hash": stable_hash(request),
            "plan_id": plan.get("plan_id"),
            "plan_version": int(plan.get("plan_version") or 1),
            "step_id": "step_003_cst_run",
        }
        for key, value in expected.items():
            if approval.get(key) != value:
                raise RuntimeError(f"real CST approval is stale: {key}")
        with self._real_cst_lock:
            if task_id in self._real_cst_running:
                raise RuntimeError("real CST step is already running")
            if int(approval.get("run_count") or 0) > 0:
                return self.blackboard.get(task_id).to_dict()
            self._real_cst_running.add(task_id)
            approval.update(
                {
                    "status": "approved",
                    "approved_at": now_iso(),
                    "attempt_count": int(approval.get("attempt_count") or 0) + 1,
                    "run_count": 0,
                }
            )
            approvals[approval_id] = approval
            self.blackboard.update(task_id, state="running", approvals=approvals)
        self._set_v2_metadata(task_id, current_stage="step_002_approval", cst_status="running")
        self._event(task_id, "central_agent", "status", "running", "User approved bound real CST step")
        try:
            execution_result = self.langgraph_runtime.resume(task_id, {"approved": True, "approval_type": "real_cst"})
            return self._apply_real_execution_result(task_id, execution_result)
        except Exception as exc:
            blockers = [*self.blackboard.get(task_id).blockers, {"type": "langgraph_resume_failed", "reason": str(exc)}]
            self.blackboard.update(task_id, state="failed", blockers=blockers)
            self._set_v2_metadata(task_id, current_stage="failed", cst_status="failed", failure_reason=str(exc))
            return self.blackboard.get(task_id).to_dict()
        finally:
            with self._real_cst_lock:
                self._real_cst_running.discard(task_id)
            final_record = self.blackboard.get(task_id)
            if final_record.state in {"completed", "failed"}:
                final_approvals = dict(final_record.approvals)
                final_approval = dict(final_approvals.get(approval_id) or {})
                final_approval.update({"status": final_record.state, "run_count": 1, "finished_at": now_iso()})
                final_approvals[approval_id] = final_approval
                self.blackboard.update(task_id, approvals=final_approvals)

    def reject_real_cst_task(self, task_id: str, reason: str = "user_rejected_real_cst") -> dict[str, Any]:
        """Reject the V2.0 solver gate and generate a failed report."""
        record = self.blackboard.get(task_id)
        if record.task_metadata.get("mode") != "real":
            raise RuntimeError("task is not a V2.0 real CST task")
        if record.state not in {"waiting_approval", "failed"}:
            raise RuntimeError(f"task is not waiting for real CST rejection: {record.state}")
        approval_id = f"real_cst:{task_id}"
        approvals = dict(record.approvals)
        approval = dict(approvals.get(approval_id) or {})
        approval.update({"approval_id": approval_id, "task_id": task_id, "status": "rejected", "reason": reason})
        approvals[approval_id] = approval
        self.blackboard.update(task_id, state="running", approvals=approvals)
        execution_result = self.langgraph_runtime.resume(task_id, {"approved": False, "approval_type": "real_cst", "reason": reason})
        return self._apply_real_execution_result(task_id, execution_result)

    def _apply_real_execution_result(self, task_id: str, execution_result: dict[str, Any]) -> dict[str, Any]:
        decision = str(execution_result.get("final_decision") or "block_task")
        stage = str(execution_result.get("final_stage") or "failed")
        if decision == "wait_user":
            self.blackboard.update(task_id, state="waiting_approval")
            self._set_v2_metadata(task_id, current_stage=stage, cst_status="waiting_user_approval")
            self._event(task_id, "central_agent", "status", "pending", "Waiting for user approval before real CST solver")
            return self.blackboard.get(task_id).to_dict()
        if decision == "complete_task":
            self.blackboard.update(task_id, state="completed")
            self._set_v2_metadata(task_id, current_stage="completed", cst_status="completed")
            self._event(task_id, "central_agent", "final", "finalized", "Unified dynamic CST task completed")
            return self.blackboard.get(task_id).to_dict()
        reason_by_stage = {
            "step_000_evidence": "paperwise_evidence_review_failed",
            "step_001_preflight": "preflight_failed",
            "step_003_cst_run": "cst_run_review_failed",
            "step_004_parse": "result_parse_review_failed",
            "step_005_report": "report_review_failed",
        }
        reason = reason_by_stage.get(stage, str(execution_result.get("reason") or decision))
        blockers = list(self.blackboard.get(task_id).blockers)
        if not any(item.get("reason") == reason for item in blockers):
            blockers.append({"type": decision, "reason": reason, "stage": stage})
        self.blackboard.update(task_id, state="failed", blockers=blockers)
        self._set_v2_metadata(
            task_id,
            current_stage="failed",
            cst_status="not_reached" if stage in {"skill_route_review", "plan_validation", "precondition"} else "failed",
            failure_reason=reason,
        )
        return self.blackboard.get(task_id).to_dict()

    def available_actions(self, task_id: str) -> dict[str, Any]:
        """Return front-end actions for the current task."""
        record = self.blackboard.get(task_id)
        actions: list[str] = ["view_logs", "send_central_message"]
        if record.task_metadata.get("mode") == "real" and record.state == "waiting_approval":
            actions.extend(["approve_real_cst", "reject"])
        if record.state in {"failed", "completed"}:
            actions.extend(["view_report", "view_artifacts"])
        return {"task_id": task_id, "state": record.state, "actions": actions, "available_actions": actions}

    def send_central_message(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Record and handle a user message for the central agent without executing risky work."""
        record = self.blackboard.get(task_id)
        message = str(payload.get("message") or "").strip()
        if not message:
            raise RuntimeError("central agent message is empty")
        intent = str(payload.get("intent") or "comment").strip() or "comment"
        allowed_intents = {"comment", "request_revision", "request_reroute", "approve_risk", "ask_question"}
        if intent not in allowed_intents:
            raise RuntimeError(f"unsupported central agent message intent: {intent}")
        target_step_id = str(payload.get("target_step_id") or record.task_metadata.get("current_stage") or "").strip() or None
        created_at = now_iso()
        message_record = {
            "schema_version": "1.0",
            "message_id": stable_hash(
                {
                    "task_id": task_id,
                    "intent": intent,
                    "target_step_id": target_step_id,
                    "message": message,
                    "created_at": created_at,
                }
            )[:16],
            "task_id": task_id,
            "sender": "user",
            "target": "central_agent",
            "intent": intent,
            "target_step_id": target_step_id,
            "message": message,
            "created_at": created_at,
            "status": "received",
        }
        messages = list(record.task_metadata.get("central_agent_messages") or [])
        messages.append(message_record)
        central_decisions = list(record.task_metadata.get("central_decisions") or [])
        central_decisions.append(
            {
                "schema_version": "1.0",
                "step_id": target_step_id,
                "decision": "user_message_received",
                "intent": intent,
                "message_id": message_record["message_id"],
                "reason": "Frontend user message recorded for central agent review.",
                "created_at": created_at,
            }
        )
        self._set_v2_metadata(
            task_id,
            central_agent_messages=messages,
            central_decisions=central_decisions,
            last_user_central_message=message_record,
        )
        self.memory.add_l1_turn(task_id, "user", f"[central_agent:{intent}] {message}")
        reply = self._handle_central_message(task_id, message_record)
        self.memory.add_l1_turn(task_id, "assistant", f"[central_agent:{reply['action']}] {reply['reply_text']}")
        self._event(
            task_id,
            "central_agent",
            "central_reply",
            "pending",
            f"Central agent handled message: {reply['action']}",
            reason=reply["reply_text"],
        )
        return self.blackboard.get(task_id).to_dict()

    def _handle_central_message(self, task_id: str, message_record: dict[str, Any]) -> dict[str, Any]:
        """Apply deterministic central-message actions and append the immediate reply."""
        intent = str(message_record.get("intent") or "comment")
        if intent in {"comment", "ask_question"}:
            reply = self._build_central_reply(
                task_id,
                message_record,
                action="reply_only",
                plan_mutation_allowed=False,
                skill_route_mutation_allowed=False,
                changed={},
            )
        elif intent == "request_revision":
            changed = self._revise_current_step_for_message(task_id, message_record)
            reply = self._build_central_reply(
                task_id,
                message_record,
                action="revise_current_step",
                plan_mutation_allowed=True,
                skill_route_mutation_allowed=False,
                changed=changed,
            )
        elif intent == "request_reroute":
            changed = self._reroute_plan_for_message(task_id, message_record)
            reply = self._build_central_reply(
                task_id,
                message_record,
                action="reroute_plan",
                plan_mutation_allowed=True,
                skill_route_mutation_allowed=True,
                changed=changed,
            )
        elif intent == "approve_risk":
            changed = self._acknowledge_risk_for_message(task_id, message_record)
            reply = self._build_central_reply(
                task_id,
                message_record,
                action="risk_acknowledged",
                plan_mutation_allowed=False,
                skill_route_mutation_allowed=False,
                changed=changed,
            )
        else:
            raise RuntimeError(f"unsupported central agent message intent: {intent}")

        record = self.blackboard.get(task_id)
        replies = list(record.task_metadata.get("central_agent_replies") or [])
        replies.append(reply)
        action_records = list(record.task_metadata.get("central_action_records") or [])
        action_records.append(
            {
                "schema_version": "1.0",
                "action_record_id": stable_hash({"reply_id": reply["reply_id"], "action": reply["action"]})[:16],
                "message_id": reply["message_id"],
                "reply_id": reply["reply_id"],
                "task_id": task_id,
                "intent": intent,
                "action": reply["action"],
                "changed": reply["changed"],
                "created_at": reply["created_at"],
                "status": "recorded",
            }
        )
        self._set_v2_metadata(
            task_id,
            central_agent_replies=replies,
            central_action_records=action_records,
            last_central_agent_reply=reply,
        )
        self.audit.emit(task_id, {"type": "central_agent_reply", **reply})
        return reply

    def _revise_current_step_for_message(self, task_id: str, message_record: dict[str, Any]) -> dict[str, Any]:
        plan = dict(self.blackboard.get(task_id).task_metadata.get("dynamic_plan") or {})
        steps = [dict(step) for step in plan.get("steps") or []]
        target_step_id = self._central_target_step_id(task_id, message_record, steps)
        changed_steps: list[dict[str, Any]] = []
        for step in steps:
            if step.get("step_id") == target_step_id:
                before = step.get("status")
                step["status"] = "revise"
                changed_steps.append({"step_id": target_step_id, "before_status": before, "after_status": "revise"})
                break
        if changed_steps:
            plan["steps"] = steps
            self._set_v2_metadata(task_id, dynamic_plan=plan)
        return {"changed_steps": changed_steps}

    def _reroute_plan_for_message(self, task_id: str, message_record: dict[str, Any]) -> dict[str, Any]:
        """Ask the LLM for a new candidate plan; never mutate the old plan in place."""
        record = self.blackboard.get(task_id)
        metadata = dict(record.task_metadata)
        old_plan = dict(metadata.get("dynamic_plan") or {})
        old_steps = [dict(step) for step in old_plan.get("steps") or []]
        old_version = int(old_plan.get("plan_version") or 1)
        new_version = old_version + 1
        target_step_id = self._central_target_step_id(task_id, message_record, old_steps)
        user_input = str(metadata.get("user_input") or record.user_input or "")
        mode = str(metadata.get("mode") or old_plan.get("mode") or "mock")
        require_paperwise = bool(metadata.get("require_paperwise", False))
        snapshot: dict[str, Any] = {}
        snapshot_path = metadata.get("capability_snapshot_path")
        if snapshot_path and Path(snapshot_path).exists():
            snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
        result = self.langgraph_runtime.replan(
            task_id=task_id,
            user_input=f"{user_input}\n{message_record.get('message') or ''}",
            mode=mode,
            require_paperwise=require_paperwise,
            capability_snapshot=snapshot,
            plan_version=new_version,
            reroute_instruction={
                "message_id": message_record.get("message_id"),
                "target_step_id": target_step_id,
                "user_instruction": message_record.get("message"),
                "previous_plan": old_plan,
            },
        )
        if not self._dynamic_plan_review_passed(result):
            findings = (result.get("active_review") or {}).get("blocking_findings") or []
            raise RuntimeError(f"LLM reroute candidate rejected: {findings}")
        new_plan = dict(result["active_plan"])
        new_steps = [dict(step) for step in new_plan.get("steps") or []]
        old_by_id = {str(step.get("step_id")): step for step in old_steps}
        new_by_id = {str(step.get("step_id")): step for step in new_steps}
        changed_steps = [
            {
                "step_id": step_id,
                "before_action": old_by_id.get(step_id, {}).get("action"),
                "after_action": new_by_id.get(step_id, {}).get("action"),
            }
            for step_id in sorted(set(old_by_id) | set(new_by_id))
            if old_by_id.get(step_id) != new_by_id.get(step_id)
        ]
        changed_skills = [
            {
                "step_id": step_id,
                "before": old_by_id.get(step_id, {}).get("required_skills") or [],
                "after": new_by_id.get(step_id, {}).get("required_skills") or [],
            }
            for step_id in sorted(set(old_by_id) | set(new_by_id))
            if (old_by_id.get(step_id, {}).get("required_skills") or []) != (new_by_id.get(step_id, {}).get("required_skills") or [])
        ]
        if mode == "real":
            current = self.blackboard.get(task_id)
            approvals = dict(current.approvals)
            approval_id = f"real_cst:{task_id}"
            old_approval = dict(approvals.get(approval_id) or {})
            old_approval.update(
                {
                    "approval_id": approval_id,
                    "task_id": task_id,
                    "status": "waiting_approval",
                    "plan_id": new_plan.get("plan_id"),
                    "plan_version": new_version,
                    "step_id": "step_003_cst_run",
                    "request_hash": stable_hash(metadata.get("request") or {}),
                    "run_count": 0,
                    "invalidated_by_reroute": True,
                }
            )
            approvals[approval_id] = old_approval
            self.blackboard.update(task_id, state="waiting_approval", approvals=approvals)
        return {"changed_steps": changed_steps, "changed_skills": changed_skills, "new_plan_version": new_version}

    def _acknowledge_risk_for_message(self, task_id: str, message_record: dict[str, Any]) -> dict[str, Any]:
        record = self.blackboard.get(task_id)
        acknowledgements = list(record.task_metadata.get("risk_acknowledgements") or [])
        acknowledgement = {
            "schema_version": "1.0",
            "ack_id": stable_hash({"task_id": task_id, "message_id": message_record.get("message_id"), "risk": message_record.get("message")})[:16],
            "message_id": message_record.get("message_id"),
            "task_id": task_id,
            "target_step_id": message_record.get("target_step_id"),
            "status": "acknowledged",
            "created_at": now_iso(),
            "note": message_record.get("message"),
        }
        acknowledgements.append(acknowledgement)
        self._set_v2_metadata(task_id, risk_acknowledgements=acknowledgements)
        return {"risk_acknowledgements": [acknowledgement]}

    def _build_central_reply(
        self,
        task_id: str,
        message_record: dict[str, Any],
        *,
        action: str,
        plan_mutation_allowed: bool,
        skill_route_mutation_allowed: bool,
        changed: dict[str, Any],
    ) -> dict[str, Any]:
        created_at = now_iso()
        target_step = self._central_target_step(task_id, message_record)
        return {
            "schema_version": "1.0",
            "reply_id": stable_hash({"task_id": task_id, "message_id": message_record.get("message_id"), "action": action, "created_at": created_at})[:16],
            "message_id": message_record.get("message_id"),
            "task_id": task_id,
            "target_step_id": target_step.get("step_id") if target_step else message_record.get("target_step_id"),
            "action": action,
            "plan_mutation_allowed": plan_mutation_allowed,
            "skill_route_mutation_allowed": skill_route_mutation_allowed,
            "reply_text": self._central_reply_text(task_id, message_record, action, changed, target_step),
            "changed": changed,
            "created_at": created_at,
        }

    def _central_reply_text(
        self,
        task_id: str,
        message_record: dict[str, Any],
        action: str,
        changed: dict[str, Any],
        target_step: dict[str, Any] | None,
    ) -> str:
        metadata = self.blackboard.get(task_id).task_metadata
        step_label = target_step.get("step_id") if target_step else (message_record.get("target_step_id") or "当前任务")
        step_goal = target_step.get("step_goal") if target_step else "未定位到具体步骤"
        latest_review = self._latest_step_record(metadata.get("global_review_records") or [], step_label)
        latest_reflection = self._latest_reflection(metadata, latest_review)
        skill_context = (target_step or {}).get("step_skill_context") or {}
        callable_skills = [route.get("owner_skill") for route in skill_context.get("callable_skills") or [] if route.get("owner_skill")]
        changed_steps = changed.get("changed_steps") or []
        changed_skills = changed.get("changed_skills") or []
        lines = [
            f"中枢回复：{action}",
            f"目标步骤：{step_label}",
            f"步骤目标：{step_goal}",
        ]
        if latest_review:
            lines.append(f"最近审查：{latest_review.get('decision') or latest_review.get('status') or '已记录'}")
        if latest_reflection:
            lines.append(f"反思状态：{latest_reflection.get('status') or '已记录'}；建议：{latest_reflection.get('what_should_change') or '无新增'}")
        if callable_skills:
            lines.append(f"当前技能上下文：{', '.join(callable_skills[:6])}")
        if action == "reply_only":
            lines.append("处理结果：已记录你的问题/评论；未修改计划、步骤状态或技能路由。")
        elif action == "revise_current_step":
            lines.append(f"处理结果：已将目标步骤标记为 revise；变更步骤数 {len(changed_steps)}。")
        elif action == "reroute_plan":
            lines.append(f"处理结果：已生成 dynamic_plan v{changed.get('new_plan_version')}；刷新步骤数 {len(changed_steps)}，技能记录数 {len(changed_skills)}。未执行 CST 或绕过审批。")
        elif action == "risk_acknowledged":
            lines.append("处理结果：已记录风险确认；这不是 pass，也不会直接推进执行。")
        return "\n".join(lines)

    def _central_target_step(self, task_id: str, message_record: dict[str, Any]) -> dict[str, Any] | None:
        steps = list((self.blackboard.get(task_id).task_metadata.get("dynamic_plan") or {}).get("steps") or [])
        target_step_id = self._central_target_step_id(task_id, message_record, steps)
        for step in steps:
            if step.get("step_id") == target_step_id:
                return dict(step)
        return None

    def _central_target_step_id(self, task_id: str, message_record: dict[str, Any], steps: list[dict[str, Any]]) -> str | None:
        requested = str(message_record.get("target_step_id") or "").strip()
        if requested and any(step.get("step_id") == requested for step in steps):
            return requested
        current = str(self.blackboard.get(task_id).task_metadata.get("current_stage") or "").strip()
        if current and any(step.get("step_id") == current for step in steps):
            return current
        return str(steps[0].get("step_id")) if steps else (requested or None)

    def _latest_step_record(self, records: list[dict[str, Any]], step_id: str | None) -> dict[str, Any]:
        for record in reversed(records):
            if not step_id or record.get("step_id") == step_id:
                return record.get("output") or record
        return {}

    def _latest_reflection(self, metadata: dict[str, Any], latest_review: dict[str, Any]) -> dict[str, Any]:
        if latest_review.get("reflection"):
            return latest_review["reflection"]
        reflections = list(metadata.get("reflection_records") or [])
        return dict(reflections[-1]) if reflections else {}

    def task_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        """Return artifact refs from the current metadata and review records."""
        metadata = self.blackboard.get(task_id).task_metadata
        candidates = list(metadata.get("artifacts") or [])
        for review in metadata.get("reviews") or []:
            if not isinstance(review, dict):
                continue
            candidates.extend(review.get("artifacts") or [])
            output = review.get("output") or {}
            if isinstance(output, dict):
                candidates.extend(output.get("artifacts") or [])
        artifacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for artifact in candidates:
            if not isinstance(artifact, dict):
                continue
            key = str(artifact.get("id") or artifact.get("path") or "")
            if key and key not in seen:
                seen.add(key)
                artifacts.append(artifact)
        return artifacts

    def task_reports(self, task_id: str) -> list[dict[str, Any]]:
        """Return report refs for the task."""
        return list(self.blackboard.get(task_id).task_metadata.get("reports") or [])

    def task_logs(self, task_id: str) -> list[dict[str, Any]]:
        """Return audit events, falling back to in-memory stream events."""
        events = self.audit.replay(task_id)
        return events if events else self.stream.replay(task_id)

    def rerun_paperwise_evidence_review(self, task_id: str) -> dict[str, Any]:
        """Re-plan and execute a read-only PaperWise evidence refresh through the Parent graph."""
        record = self.blackboard.get(task_id)
        metadata = dict(record.task_metadata)
        request = dict(metadata.get("request") or {})
        query = str(request.get("user_input") or metadata.get("user_input") or "")
        if not query:
            raise RuntimeError("task has no user_input for PaperWise review")
        active_plan = dict(metadata.get("dynamic_plan") or {})
        result = self.langgraph_runtime.rerun_paperwise(
            task_id=task_id,
            user_input=query,
            mode=str(metadata.get("mode") or "mock"),
            capability_snapshot=self.registry.snapshot(),
            plan_version=int(active_plan.get("plan_version") or 1) + 1,
        )
        if result.get("final_decision") != "complete_task":
            raise RuntimeError(str(result.get("reason") or "PaperWise evidence rerun was blocked"))
        reviewed = dict(self.blackboard.get(task_id).task_metadata.get("evidence_pool_summary") or {})
        reviewed["reviewed_at"] = now_iso()
        self._set_v2_metadata(task_id, evidence_pool_summary=reviewed)
        self._event(task_id, "central_agent", "final", "finalized", "PaperWise evidence review rerun")
        return self.blackboard.get(task_id).to_dict()

    def _persist_dynamic_plan_result(
        self,
        *,
        task_id: str,
        mode: str,
        plan: dict[str, Any],
        review: dict[str, Any],
        plan_version: int,
        reroute_instruction: dict[str, Any] | None,
        generation_error: str,
        plan_metadata_key: str = "dynamic_plan",
    ) -> dict[str, Any]:
        """Persist one graph-produced plan and its validation result."""
        metadata = dict(self.blackboard.get(task_id).task_metadata)
        history_key = f"{plan_metadata_key}_history"
        if plan_metadata_key == "dynamic_plan":
            history = list(metadata.get(history_key) or []) if plan_version > 1 else []
        else:
            history = list(metadata.get(history_key) or [])
        history.append({"plan": plan, "review": review, "reroute_instruction": reroute_instruction})
        active_plan = plan
        active_review = review

        plan_dir = Path(self.settings.workspace_root) / "tasks" / task_id / plan_metadata_key
        plan_path = plan_dir / f"{plan_metadata_key}.json"
        review_path = plan_dir / f"{plan_metadata_key}_review.json"
        history_path = plan_dir / f"{history_key}.json"
        atomic_write_json(plan_path, active_plan)
        atomic_write_json(review_path, active_review)
        atomic_write_json(history_path, history)
        reflections = [item.get("review", {}).get("reflection") for item in history if item.get("review", {}).get("reflection")]

        self._append_v2_artifacts(
            task_id,
            [
                self._artifact_ref(plan_metadata_key, str(plan_path), "dynamic_plan", "central_agent", metadata={"mode": mode}),
                self._artifact_ref(f"{plan_metadata_key}_review", str(review_path), "dynamic_plan_validation", "central_scheduler", metadata={"mode": mode}),
                self._artifact_ref(history_key, str(history_path), "dynamic_plan_history", "central_agent", metadata={"mode": mode}),
            ],
        )
        updates = {
            plan_metadata_key: active_plan,
            history_key: history,
            "global_review_records": list(metadata.get("global_review_records") or []),
            "plan_review_records": [*(metadata.get("plan_review_records") or []), active_review],
            "reflection_records": reflections,
            "current_stage": plan_metadata_key,
        }
        if plan_metadata_key == "dynamic_plan":
            updates["active_dynamic_plan_version"] = active_plan.get("plan_version", 1)
        self._set_v2_metadata(task_id, **updates)
        self.audit.emit(
            task_id,
            {
                "type": "dynamic_plan_selected",
                "plan_id": active_plan.get("plan_id"),
                "plan_version": active_plan.get("plan_version"),
                "step_count": len(active_plan.get("steps") or []),
                "source": active_plan.get("source"),
                "validation_status": active_review.get("status"),
                "generation_error": generation_error or None,
            },
        )
        return {"active_plan": active_plan, "active_review": active_review, "history": history}

    def _dynamic_planner_input(
        self,
        *,
        task_id: str,
        user_input: str,
        mode: str,
        require_paperwise: bool,
        capability_snapshot: dict[str, Any],
        route_plan: dict[str, Any],
        route_review: dict[str, Any],
        metadata: dict[str, Any],
        plan_version: int,
        reroute_instruction: dict[str, Any] | None,
    ) -> dict[str, Any]:
        capabilities = capability_snapshot.get("capabilities") or {}
        capability_summary = [
            {
                "name": name,
                "risk_level": value.get("risk_level"),
                "requires_approval": value.get("requires_approval"),
            }
            for name, value in list(capabilities.items())[:100]
            if isinstance(value, dict)
        ]
        route_summary = [
            {
                "owner_skill": route.get("owner_skill"),
                "capability": route.get("capability"),
                "status": route.get("status"),
                "confidence": route.get("confidence"),
                "adapter_binding": route.get("adapter_binding"),
            }
            for route in route_plan.get("routes") or []
        ]
        artifacts = [
            {"artifact_id": item.get("artifact_id"), "artifact_type": item.get("artifact_type"), "path": item.get("path")}
            for item in (metadata.get("artifacts") or [])[-30:]
        ]
        return {
            "schema_version": "1.0",
            "operation": "generate_dynamic_plan_candidate",
            "task_id": task_id,
            "plan_version": plan_version,
            "user_goal": user_input,
            "mode": mode,
            "require_paperwise": require_paperwise,
            "available_actions": sorted(self._dynamic_action_specs()),
            "registered_skills": sorted(self.skill_router.registry.load()),
            "callable_skill_routes": route_summary,
            "route_review": {
                "decision": route_review.get("decision"),
                "reflection": route_review.get("reflection"),
                "reroute_suggestion": route_review.get("reroute_suggestion"),
            },
            "capability_snapshot_id": capability_snapshot.get("snapshot_id"),
            "capabilities": capability_summary,
            "evidence_pool_summary": metadata.get("evidence_pool_summary") or {},
            "modeling_request": metadata.get("modeling_request") or {},
            "artifacts": artifacts,
            "reroute_instruction": reroute_instruction,
            "output_contract": {
                "type": "object",
                "required": ["steps"],
                "step_fields": [
                    "step_goal",
                    "action",
                    "inputs",
                    "outputs",
                    "required_skills",
                    "gate_condition",
                ],
                "rules": [
                    "Use only available_actions and registered_skills.",
                    "Do not claim task completion; Scheduler owns final state.",
                    "Each step must be directly executable by one task subagent and reviewable by one module reviewer.",
                    "If modeling_request is present, use modeling_preparation and stop after validated cst_model_spec.",
                    "For real CST mode without modeling_request include preflight, approval, cst_run, result_parse, report in that order.",
                    "Never bypass CST approval or a skill gate.",
                ],
            },
        }

    def _generate_dynamic_plan_candidate(self, planner_input: dict[str, Any]) -> dict[str, Any]:
        system_prompt = (
            "You are the planning capability of one central Scheduler. Return strict JSON only. "
            "Generate a concise executable candidate plan from the supplied goal, evidence, routes, and capabilities. "
            "You propose steps but never decide final task state. Use only declared actions and skills. "
            "Do not bypass approval, skill gates, PaperWise read-only rules, or CST safety constraints."
        )
        return self.llm.generate_json(
            system_prompt=system_prompt,
            payload=planner_input,
            runtime_config=runtime_llm_config,
        )

    @staticmethod
    def _fallback_dynamic_plan_candidate(planner_input: dict[str, Any], *, reason: str) -> dict[str, Any]:
        """Build the smallest safe plan when the preferred external planner is unavailable."""

        def step(
            goal: str,
            action: str,
            skills: list[str],
            inputs: list[str],
            outputs: list[str],
            gate: str,
        ) -> dict[str, Any]:
            return {
                "step_goal": goal,
                "action": action,
                "inputs": inputs,
                "outputs": outputs,
                "required_skills": skills,
                "gate_condition": gate,
            }

        if planner_input.get("modeling_request"):
            steps = [
                step(
                    "Prepare and validate traceable antenna geometry up to cst_model_spec",
                    "modeling_preparation",
                    ["antenna-research-ideation"],
                    ["modeling_request"],
                    ["cst_model_spec"],
                    "stop before CST execution",
                )
            ]
        elif str(planner_input.get("mode") or "mock") == "real":
            steps = [
                step("Collect traceable PaperWise evidence", "evidence_retrieval", ["paperwise"], ["user_goal"], ["evidence_pool"], "PaperWise read-only"),
                step("Check CST environment and request inputs", "preflight", ["cst-control"], ["request"], ["preflight_result"], "read-only preflight"),
                step("Wait for bound user approval", "approval", [], ["preflight_result"], ["approval_record"], "approval.status == approved"),
                step("Run one approved real CST job", "cst_run", ["cst-control", "e-platform-cst"], ["approval_record", "request"], ["cst_run_manifest"], "real CST approval required"),
                step("Parse S11, return loss, and bandwidth", "result_parse", [], ["cst_run_manifest"], ["parsed_result"], "run review passed"),
                step("Generate the terminal report", "report", [], ["parsed_result"], ["task_report"], "parsed result review passed"),
            ]
        else:
            steps = [
                step(
                    "Handle the current request",
                    "general",
                    [],
                    ["user_goal"],
                    ["general_task_summary"],
                    "no specialized execution required",
                )
            ]
        return {
            "source": "scheduler_fallback",
            "steps": steps,
            "generation_error": str(reason),
            "llm_metadata": {"fallback": True, "data_egress": False},
        }

    def _normalize_dynamic_plan_candidate(
        self,
        candidate: dict[str, Any],
        *,
        task_id: str,
        mode: str,
        plan_version: int,
        user_input: str,
    ) -> dict[str, Any]:
        raw_steps = candidate.get("steps")
        if not isinstance(raw_steps, list):
            raw_steps = []
        specs = self._dynamic_action_specs()
        canonical_real_ids = {
            "evidence_retrieval": "step_000_evidence",
            "preflight": "step_001_preflight",
            "approval": "step_002_approval",
            "cst_run": "step_003_cst_run",
            "result_parse": "step_004_parse",
            "report": "step_005_report",
        }
        steps: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, dict):
                raw = {}
            action = str(raw.get("action") or "").strip()
            spec = specs.get(action, {})
            requested_id = str(raw.get("step_id") or "").strip()
            safe_id = requested_id if re.fullmatch(r"[A-Za-z0-9_-]{3,80}", requested_id) else f"step_{index:03}_{action or 'invalid'}"
            if mode == "real" and action in canonical_real_ids:
                safe_id = canonical_real_ids[action]
            required_skills = raw.get("required_skills") if isinstance(raw.get("required_skills"), list) else []
            steps.append(
                {
                    "step_id": safe_id,
                    "step_goal": str(raw.get("step_goal") or "").strip(),
                    "task_agent": spec.get("task_agent", "invalid_task_agent"),
                    "handler": spec.get("task_agent", "invalid_task_agent"),
                    "action": action,
                    "module_review_agent": spec.get("review_agent", "invalid_review_agent"),
                    "global_review_required": self._action_requires_global_review(action),
                    "inputs": [str(item) for item in raw.get("inputs") or [] if str(item).strip()],
                    "outputs": [str(item) for item in raw.get("outputs") or [] if str(item).strip()],
                    "evidence_refs": [],
                    "required_skills": [str(item) for item in required_skills if str(item).strip()],
                    "callable_skills": [],
                    "candidate_skills": [],
                    "blocked_skills": [],
                    "step_skill_context": {},
                    "gate_condition": str(raw.get("gate_condition") or "").strip(),
                    "depends_on": [str(item) for item in raw.get("depends_on") or [] if str(item).strip()],
                    "external_llm_required": bool(raw.get("external_llm_required", False)),
                    "status": "pending",
                }
            )
        seed = {
            "task_id": task_id,
            "mode": mode,
            "plan_version": plan_version,
            "steps": steps,
        }
        planner_source = str(candidate.get("source") or "external_llm")
        return {
            "schema_version": "1.0",
            "plan_type": "dynamic_plan",
            "plan_id": stable_hash(seed)[:16],
            "plan_version": plan_version,
            "task_id": task_id,
            "mode": mode,
            "source": planner_source,
            "reference_template": "none",
            "inputs": {"user_goal": user_input},
            "steps": steps,
            "summary": {"step_count": len(steps), "planner": planner_source, "scheduler_validated": False},
            "llm_metadata": candidate.get("llm_metadata") or {},
            "generation_error": candidate.get("generation_error"),
            "rerun_kind": candidate.get("rerun_kind"),
        }

    def _validate_dynamic_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        findings: list[dict[str, str]] = []
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        specs = self._dynamic_action_specs()
        registered_skills = set(self.skill_router.registry.load())
        if plan.get("plan_type") != "dynamic_plan":
            findings.append({"type": "schema_error", "reason": "plan_type_not_dynamic_plan"})
        if plan.get("source") not in {"external_llm", "scheduler_fallback", "external_llm_failed"}:
            findings.append({"type": "planner_source_error", "reason": "plan_source_not_allowed"})
        if plan.get("source") == "external_llm_failed":
            findings.append({"type": "planner_generation_error", "reason": str(plan.get("generation_error") or "external_llm_failed")})
        if not steps:
            findings.append({"type": "schema_error", "reason": "dynamic_plan_has_no_steps"})
        if len(steps) > 25:
            findings.append({"type": "schema_error", "reason": "dynamic_plan_exceeds_25_steps"})
        seen_ids: set[str] = set()
        action_positions: dict[str, list[int]] = {}
        for index, step in enumerate(steps):
            step_id = str(step.get("step_id") or "")
            action = str(step.get("action") or "")
            if not step_id or step_id in seen_ids:
                findings.append({"type": "schema_error", "reason": f"duplicate_or_missing_step_id:{step_id}"})
            seen_ids.add(step_id)
            if not str(step.get("step_goal") or "").strip():
                findings.append({"type": "schema_error", "reason": f"{step_id}:missing_step_goal"})
            if action not in specs:
                findings.append({"type": "unknown_action", "reason": f"{step_id}:{action}"})
            action_positions.setdefault(action, []).append(index)
            unknown_skills = sorted(set(step.get("required_skills") or []) - registered_skills)
            for skill in unknown_skills:
                findings.append({"type": "unknown_skill", "reason": f"{step_id}:{skill}"})
            for dependency in step.get("depends_on") or []:
                if dependency not in seen_ids:
                    findings.append({"type": "invalid_dependency", "reason": f"{step_id}:{dependency}"})

        mode = str(plan.get("mode") or "mock")
        is_paperwise_rerun = plan.get("rerun_kind") == "paperwise_evidence_rerun"
        if mode != "real" and any(action in action_positions for action in ("preflight", "approval", "cst_run")):
            findings.append({"type": "unsafe_execution", "reason": "non_real_plan_contains_real_cst_actions"})
        modeling_only = False
        task_id = str(plan.get("task_id") or "")
        if task_id:
            try:
                modeling_only = bool(self.blackboard.get(task_id).task_metadata.get("modeling_request"))
            except FileNotFoundError:
                # Candidate validation is also used before a task snapshot exists.
                modeling_only = False
        if mode == "real" and modeling_only:
            if len(action_positions.get("modeling_preparation", [])) != 1:
                findings.append({"type": "modeling_contract", "reason": "modeling_plan_requires_exactly_one:modeling_preparation"})
            if any(action in action_positions for action in ("approval", "cst_run", "result_parse")):
                findings.append({"type": "unsafe_execution", "reason": "modeling_preparation_plan_must_stop_before_cst"})
        elif mode == "real" and is_paperwise_rerun:
            if list(action_positions) != ["evidence_retrieval"] or len(action_positions.get("evidence_retrieval", [])) != 1:
                findings.append({"type": "paperwise_rerun_contract", "reason": "paperwise_rerun_requires_exactly_one:evidence_retrieval"})
        elif mode == "real":
            required = ["preflight", "approval", "cst_run", "result_parse", "report"]
            for action in required:
                if len(action_positions.get(action, [])) != 1:
                    findings.append({"type": "real_cst_contract", "reason": f"real_plan_requires_exactly_one:{action}"})
            if all(len(action_positions.get(action, [])) == 1 for action in required):
                positions = [action_positions[action][0] for action in required]
                if positions != sorted(positions):
                    findings.append({"type": "unsafe_execution", "reason": "real_cst_actions_out_of_order"})
                cst_step = steps[action_positions["cst_run"][0]]
                if "approval" not in str(cst_step.get("gate_condition") or "").lower():
                    findings.append({"type": "unsafe_execution", "reason": "real_cst_step_missing_approval_gate"})
        if not findings:
            plan["summary"]["scheduler_validated"] = True
        return self._plan_validation_result(findings, source="central_scheduler")

    @staticmethod
    def _plan_validation_result(findings: list[dict[str, str]], *, source: str) -> dict[str, Any]:
        passed = not findings
        return {
            "schema_version": "1.0",
            "review_agent": "central_scheduler_validator",
            "decision": "pass" if passed else "block",
            "status": "pass" if passed else "block",
            "evidence_level": "B" if passed else "D",
            "blocking_findings": findings,
            "required_fixes": [item["reason"] for item in findings],
            "reflection": {
                "status": "no_issue" if passed else "blocked",
                "error_type": "none" if passed else "invalid_candidate_plan",
                "why_central_failed": "" if passed else "LLM candidate failed deterministic Scheduler validation",
                "what_should_change": "" if passed else "Regenerate a candidate using only declared actions, skills, dependencies, and gates",
                "reroute_required": False,
                "confidence": 0.99,
                "confidence_basis": {"source": source, "deterministic": True},
            },
            "reroute_suggestion": {"action": "none", "reason": "", "suggested_steps": []},
        }

    @staticmethod
    def _failed_dynamic_plan(task_id: str, mode: str, plan_version: int, user_input: str, reason: str) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "plan_type": "dynamic_plan",
            "plan_id": stable_hash({"task_id": task_id, "version": plan_version, "error": reason})[:16],
            "plan_version": plan_version,
            "task_id": task_id,
            "mode": mode,
            "source": "external_llm_failed",
            "reference_template": "none",
            "inputs": {"user_goal": user_input},
            "steps": [],
            "summary": {"step_count": 0, "planner": "external_llm", "scheduler_validated": False},
            "generation_error": reason,
        }

    @staticmethod
    def _dynamic_action_specs() -> dict[str, dict[str, str]]:
        actions = [
            "evidence_retrieval",
            "evidence_check",
            "general",
            "goal_contract",
            "geometry_evidence",
            "baseline_ablation",
            "claim_assessment",
            "antenna_result_to_claim",
            "report",
            "preflight",
            "approval",
            "cst_run",
            "result_parse",
            "modeling_preparation",
        ]
        specs = {
            action: {"task_agent": f"{action}_task_agent", "review_agent": f"{action}_review_agent"}
            for action in actions
        }
        return specs

    @staticmethod
    def _action_requires_global_review(action: str) -> bool:
        return action in {
            "evidence_check",
            "goal_contract",
            "geometry_evidence",
            "baseline_ablation",
            "claim_assessment",
            "antenna_result_to_claim",
            "result_parse",
            "report",
            "modeling_preparation",
        }

    def _dynamic_plan_review_passed(self, plan_result: dict[str, Any]) -> bool:
        """Return true only when Scheduler accepted the LLM candidate plan."""
        review = plan_result.get("active_review") or {}
        return review.get("decision") == "pass" and review.get("status") == "pass"

    @staticmethod
    def _central_step_decision(module_review: dict[str, Any], global_review: dict[str, Any] | None = None) -> str:
        """Let Scheduler deterministically arbitrate reviews for the current step."""
        module_decision = str(module_review.get("decision") or "pass")
        global_review = global_review or {}
        global_decision = str(global_review.get("decision") or "pass")
        if module_decision in {"block", "blocked"} or global_decision in {"block", "blocked"}:
            return "block_task"
        if module_decision == "wait_user":
            return "wait_user"
        if module_decision == "reroute":
            return "reroute_plan"
        suggestion = global_review.get("reroute_suggestion") or {}
        if suggestion.get("action") == "refresh_route":
            return "refresh_skill_route"
        if global_decision == "reroute":
            return "reroute_plan"
        if global_decision == "wait_user":
            return "wait_user"
        if module_decision == "revise" or global_decision == "revise":
            return "revise_current_step"
        return "pass_next_step"

    def _execute_dynamic_step_handler(
        self,
        *,
        task_id: str,
        step: dict[str, Any],
        route_plan: dict[str, Any],
        plan_version: int,
        user_input: str,
    ) -> dict[str, Any]:
        """Execute a real local handler or a gate-authorized Antenna Skill adapter."""
        action = str(step.get("action") or "")
        step_id = str(step.get("step_id") or "")
        metadata = self.blackboard.get(task_id).task_metadata
        artifacts = list(metadata.get("artifacts") or [])
        skill_artifacts = self._skill_artifact_refs(artifacts)
        evidence_refs = self._dynamic_evidence_refs(metadata)

        if action == "modeling_preparation":
            modeling_request = dict(metadata.get("modeling_request") or {})
            modeling_request.setdefault("task_id", task_id)
            modeling_request.setdefault("paper_id", task_id)
            attempt = int(metadata.get("modeling_preparation_attempt") or 0) + 1
            output_dir = (
                Path(self.settings.workspace_root)
                / "tasks"
                / task_id
                / "modeling_preparation"
                / f"attempt_{attempt:02d}"
            )
            result = self.modeling_preparation.prepare(modeling_request, output_dir)
            registered: list[dict[str, Any]] = []
            for name, path in sorted((result.get("artifacts") or {}).items()):
                if not path:
                    continue
                registered.append(
                    self._v2_artifact(
                        str(name),
                        str(path),
                        str(name),
                        metadata={
                            "created_by": "modeling_preparation_task_agent",
                            "attempt": attempt,
                            "read_only_source": True,
                            "no_cst_execution": True,
                        },
                    )
                )
            result_path = str(result.get("result_path") or "")
            if result_path:
                registered.append(
                    self._v2_artifact(
                        "modeling_preparation_result",
                        result_path,
                        "modeling_preparation_result",
                        metadata={"attempt": attempt, "no_cst_execution": True},
                    )
                )
            self._append_v2_artifacts(task_id, registered)
            self._set_v2_metadata(
                task_id,
                modeling_preparation_attempt=attempt,
                modeling_preparation_result=result,
                cst_status="not_reached",
            )
            source_refs = [
                str(item.get("source_path"))
                for item in modeling_request.get("evidence") or []
                if isinstance(item, dict) and item.get("source_path")
            ]
            artifact_refs = [str(item.get("path")) for item in registered if item.get("path")]
            success = bool(result.get("success"))
            spec_path = str((result.get("artifacts") or {}).get("cst_model_spec") or "")
            failure = dict(result.get("failure") or {})
            manifest_path = str((result.get("artifacts") or {}).get("cst_model_spec_manifest") or "")
            persisted_validation = self.modeling_preparation.validate_cst_model_spec_artifacts(
                spec_path,
                manifest_path,
            ) if success and spec_path and manifest_path else {"valid": False, "findings": ["cst_model_spec artifacts are missing"]}
            success = (
                success
                and bool(spec_path)
                and Path(spec_path).is_file()
                and not bool(result.get("cst_executed"))
                and bool(persisted_validation.get("valid"))
            )
            if not success and not failure and persisted_validation.get("findings"):
                failure = {
                    "code": "artifact_schema_invalid",
                    "repairable": True,
                    "blockers": list(persisted_validation["findings"]),
                    "missing_inputs": [],
                    "repair_actions": ["archive the invalid artifact set and regenerate it from validated inputs"],
                }
            review = {
                "schema_version": "1.0",
                "decision": "pass" if success else ("revise" if failure.get("repairable") else "block"),
                "status": "pass" if success else ("revise" if failure.get("repairable") else "block"),
                "evidence_level": "A" if success else "D",
                "blocking_findings": [] if success else [
                    {
                        "type": str(failure.get("code") or "modeling_preparation_failed"),
                        "reason": "; ".join(str(item) for item in failure.get("blockers") or []) or "modeling preparation failed",
                    }
                ],
                "required_fixes": list(failure.get("repair_actions") or []),
                "reflection": {
                    "status": "no_issue" if success else "blocked",
                    "error_type": "none" if success else str(failure.get("code") or "modeling_preparation_failed"),
                    "confidence": 0.99,
                },
            }
            self._record_v2_review(task_id, review)
            return {
                "status": "executed" if success else "failed",
                "modeling_preparation": result,
                "validated_cst_model_spec": spec_path if success else "",
                "cst_executed": False,
                "evidence_refs": [*source_refs, *artifact_refs],
                "repairable_failure": None if success else {
                    "code": failure.get("code"),
                    "stage": result.get("completed_stage"),
                    "blockers": failure.get("blockers") or [],
                    "missing_inputs": failure.get("missing_inputs") or [],
                    "repair_actions": failure.get("repair_actions") or [],
                },
                "_module_review": review,
            }

        if action == "preflight":
            request = dict(metadata.get("request") or {})
            preflight = self.cst_real.preflight(request)
            review = self.cst_real.review_preflight(preflight)
            path = self._write_v2_json(task_id, "preflight_result.json", preflight)
            self._set_v2_metadata(task_id, preflight=preflight, cst_status="preflight_done")
            self._append_v2_artifacts(task_id, [self._v2_artifact("preflight_result", path, "preflight")])
            self._record_v2_review(task_id, review)
            return {
                "status": "executed",
                "preflight_result": preflight,
                "evidence_refs": [path],
                "_module_review": review,
            }
        if action == "approval":
            approval = self._ensure_real_cst_approval(task_id)
            approved = self._real_cst_approval_valid(task_id)
            review = {
                "schema_version": "1.0",
                "status": "pass" if approved else "wait_user",
                "decision": "pass" if approved else "wait_user",
                "conclusion": "bound approval is valid" if approved else "waiting for bound real CST approval",
                "blocking_findings": [],
                "required_fixes": [] if approved else ["user approval required before CST solver"],
            }
            return {
                "status": "approved" if approved else "waiting_approval",
                "approval_record": approval,
                "evidence_refs": [],
                "_module_review": review,
            }
        if action == "cst_run":
            if not self._real_cst_approval_valid(task_id):
                return {
                    "status": "blocked",
                    "missing_required_input": "bound real CST approval is missing or stale",
                    "contains_live_cst": True,
                    "evidence_refs": evidence_refs,
                }
            request = dict(metadata.get("request") or {})
            run_manifest = self.cst_real.run_single(task_id, request)
            review = self.cst_real.review_run(run_manifest)
            self._set_v2_metadata(task_id, run_manifest=run_manifest, cst_status="run_done")
            self._append_v2_artifacts(
                task_id,
                [self._v2_artifact("cst_run_manifest", run_manifest.get("manifest_path", ""), "run_manifest"), *(review.get("artifacts") or [])],
            )
            self._record_v2_review(task_id, review)
            return {
                "status": "executed",
                "cst_run_manifest": run_manifest,
                "contains_live_cst": not bool(request.get("simulate_cst")),
                "evidence_refs": [str(run_manifest.get("manifest_path") or "")],
                "_module_review": review,
            }
        if action == "result_parse":
            request = dict(metadata.get("request") or {})
            run_manifest = dict(metadata.get("run_manifest") or {})
            if not run_manifest:
                return {"status": "blocked", "missing_required_input": "cst_run_manifest", "evidence_refs": evidence_refs}
            parsed_result = self.cst_real.parse_results(run_manifest, request)
            review = self.cst_real.review_parsed_results(parsed_result, request)
            self._set_v2_metadata(task_id, results=parsed_result, parse_review=review, cst_status="parsed")
            self._append_v2_artifacts(task_id, [self._v2_artifact("parsed_results", parsed_result.get("path", ""), "parsed_result")])
            self._record_v2_review(task_id, review)
            return {
                "status": "executed",
                "parsed_result": parsed_result,
                "evidence_refs": [str(parsed_result.get("path") or "")],
                "_module_review": review,
            }
        if action == "report" and str(metadata.get("mode")) == "real":
            failure_reason = str(metadata.get("failure_reason") or "") or None
            report_status = "failed" if failure_reason else "completed"
            report = self.cst_real.write_report(
                task_id=task_id,
                task_title=str(metadata.get("task_title") or task_id),
                user_input=str(metadata.get("user_input") or user_input),
                status=report_status,
                run_manifest=metadata.get("run_manifest") or None,
                parsed_result=metadata.get("results") or None,
                reviews=list(metadata.get("reviews") or []),
                artifacts=self.task_artifacts(task_id),
                evidence_pool_summary=metadata.get("evidence_pool_summary") or {},
                failure_reason=failure_reason,
                workspace_root=self.settings.workspace_root,
            )
            review = self.cst_real.review_report(report)
            self._append_v2_reports(task_id, [report])
            self._append_v2_artifacts(
                task_id,
                [
                    self._v2_artifact(f"{report_status}_report_md", report.get("markdown_path", ""), "report"),
                    self._v2_artifact(f"{report_status}_report_html", report.get("html_path", ""), "report"),
                ],
            )
            self._record_v2_review(task_id, review)
            return {
                "status": "executed",
                "task_report": report,
                "evidence_refs": [str(report.get("markdown_path") or ""), str(report.get("html_path") or "")],
                "_module_review": review,
            }

        if action == "evidence_retrieval":
            candidates = self.paperwise.evidence_pool_summary(
                user_input,
                metadata.get("paper_report_path"),
                allow_external_embedding=self._has_approved_external_llm(task_id),
            )
            evidence = self._review_paperwise_evidence(
                user_input,
                candidates,
                allow_runtime_llm=self._has_approved_external_llm(task_id),
            )
            self._set_v2_metadata(task_id, evidence_pool_summary=evidence)
            path = self._write_v2_json(task_id, "paperwise_evidence_pool_summary.json", evidence)
            self._append_v2_artifacts(
                task_id,
                [
                    self._v2_artifact(
                        "paperwise_evidence_pool_summary",
                        path,
                        "paperwise_evidence_pool",
                        metadata={"source": "PaperWise", "read_only": True, "data_egress": False, "no_cst_execution": True},
                    )
                ],
            )
            refs = [
                str(item.get("path"))
                for source in (evidence.get("sources") or {}).values()
                for item in (source.get("items") or [])
                if item.get("path")
            ]
            return {"status": "executed", "evidence_pool": evidence, "evidence_refs": [path, *refs], "read_only": True}
        if action == "evidence_check":
            evidence = metadata.get("evidence_pool_summary") or self.paperwise.evidence_pool_summary(
                user_input,
                metadata.get("paper_report_path"),
                allow_external_embedding=self._has_approved_external_llm(task_id),
            )
            refs = [
                str(item.get("path"))
                for source in (evidence.get("sources") or {}).values()
                for item in (source.get("items") or [])
                if item.get("path")
            ]
            if not refs:
                return {"status": "blocked", "missing_required_input": "no traceable PaperWise evidence", "evidence_refs": []}
            return {"status": "executed", "claim_evidence_review": {"status": "traceable", "count": len(refs)}, "evidence_refs": refs}
        if action == "general" or step.get("task_agent") == "general_task_agent":
            return {"status": "executed", "general_task_summary": user_input, "evidence_refs": []}
        if action == "report":
            failure_reason = str(metadata.get("failure_reason") or "") or None
            report_status = "failed" if failure_reason else "completed"
            report = {
                "schema_version": "1.0",
                "report_type": f"{report_status}_report",
                "status": report_status,
                "task_id": task_id,
                "task_goal": user_input,
                "plan_version": plan_version,
                "artifacts": artifacts,
                "failure_reason": failure_reason,
                "limitations": ["planning path does not execute a live CST solver"],
                "created_at": now_iso(),
            }
            path = self._write_v2_json(task_id, f"{report_status}_report.json", report)
            report["json_path"] = path
            self._append_v2_reports(task_id, [report])
            self._append_v2_artifacts(task_id, [self._v2_artifact(f"{report_status}_report", path, "task_report")])
            return {"status": "executed", "task_report": report, "evidence_refs": [path]}
        if action == "baseline_ablation":
            decision = self.skill_executor_gate.check(
                task_id,
                "run_manifest",
                route_plan,
                step_id=step_id,
                plan_version=plan_version,
            )
            if not decision.allowed or decision.token is None:
                return {
                    "status": "blocked",
                    "missing_required_input": f"skill gate rejected baseline planner: {decision.reason}",
                    "evidence_refs": evidence_refs,
                }
            self.skill_executor_gate.validate_token(
                task_id,
                "run_manifest",
                route_plan,
                decision.token,
                step_id=step_id,
                plan_version=plan_version,
            )
            geometry_ref = self._latest_skill_packet_path(artifacts, {"geometry_contract"})
            if not geometry_ref:
                return {"status": "blocked", "missing_required_input": "baseline planning requires geometry_contract", "evidence_refs": evidence_refs}
            plan = self._baseline_ablation_plan(user_input, artifacts, geometry_ref)
            output_dir = Path(self.settings.workspace_root) / "tasks" / task_id / "dynamic_steps" / step_id
            try:
                adapted = self.antenna_skills.adapt_baseline_ablation_plan(
                    plan,
                    output_dir,
                    timeout_seconds=self.settings.agent_timeout_seconds,
                )
            except Exception as exc:
                return {"status": "blocked", "missing_required_input": f"baseline adapter failed: {exc}", "evidence_refs": evidence_refs}
            if not adapted:
                return {"status": "blocked", "missing_required_input": "baseline adapter unavailable", "evidence_refs": evidence_refs}
            path = str(adapted.get("path") or "")
            self._append_v2_artifacts(task_id, [self._v2_artifact("skill_packet", path, "run_manifest")])
            return {
                "status": "executed",
                "baseline_ablation_plan": plan,
                "skill_packet": adapted.get("packet"),
                "skill_adapter": adapted.get("adapter"),
                "gate_token_id": decision.token.get("token_id"),
                "evidence_refs": [*evidence_refs, path],
            }

        packet_by_action = {
            "goal_contract": "experiment_contract",
            "geometry_evidence": "geometry_contract",
            "claim_assessment": "claim_assessment",
            "antenna_result_to_claim": "claim_assessment",
        }
        packet_type = packet_by_action.get(action)
        if packet_type is None:
            if step.get("required_skills"):
                return {
                    "status": "blocked",
                    "missing_required_input": f"no executable handler for {action}",
                    "evidence_refs": evidence_refs,
                }
            return {"status": "executed", "action": action, "evidence_refs": evidence_refs}

        decision = self.skill_executor_gate.check(
            task_id,
            packet_type,
            route_plan,
            step_id=step_id,
            plan_version=plan_version,
        )
        if not decision.allowed or decision.token is None:
            if action == "goal_contract" and not step.get("required_skills"):
                return {
                    "status": "executed",
                    "objective_contract": self.paperwise._query_profile(user_input),
                    "evidence_refs": evidence_refs,
                }
            return {
                "status": "blocked",
                "missing_required_input": f"skill gate rejected {packet_type}: {decision.reason}",
                "evidence_refs": evidence_refs,
            }
        self.skill_executor_gate.validate_token(
            task_id,
            packet_type,
            route_plan,
            decision.token,
            step_id=step_id,
            plan_version=plan_version,
        )
        output_dir = Path(self.settings.workspace_root) / "tasks" / task_id / "dynamic_steps" / step_id
        source_packet_path = None
        if packet_type == "claim_assessment":
            source_packet_path = self._latest_skill_packet_path(artifacts, {"result_packet", "run_manifest", "experiment_contract"})
        elif packet_type == "next_iteration_plan":
            source_packet_path = self._latest_skill_packet_path(
                artifacts,
                {"idea_card", "experiment_contract", "geometry_contract", "run_manifest", "result_packet", "claim_assessment"},
            )
        try:
            if packet_type == "claim_assessment" and not source_packet_path:
                adapted = self.antenna_skills.adapt_claim_assessment(
                    {
                        "claim_id": stable_hash(user_input)[:16],
                        "claim": user_input,
                        "support": "insufficient",
                        "claim_ceiling": "insufficient until validated CST/result exports exist",
                        "evidence_summary": "PaperWise context exists, but no validated result_packet is available.",
                        "limitations": ["missing validated result_packet"],
                        "next_required_experiments": ["run CST and export the target metrics"],
                    },
                    output_dir,
                    timeout_seconds=self.settings.agent_timeout_seconds,
                )
            else:
                adapted = self.antenna_skills.adapt_packet_stage(
                    packet_type,
                    {"goal": user_input, "parsed_goal": self.paperwise._query_profile(user_input)},
                    skill_artifacts,
                    output_dir,
                    source_packet_path=source_packet_path,
                    timeout_seconds=self.settings.agent_timeout_seconds,
                )
        except Exception as exc:
            return {
                "status": "blocked",
                "missing_required_input": f"adapter execution failed for {packet_type}: {exc}",
                "evidence_refs": evidence_refs,
            }
        if not adapted:
            return {
                "status": "blocked",
                "missing_required_input": f"adapter unavailable for {packet_type}",
                "evidence_refs": evidence_refs,
            }
        path = str(adapted.get("path") or "")
        if path:
            self._append_v2_artifacts(task_id, [self._v2_artifact("skill_packet", path, packet_type)])
        return {
            "status": "executed",
            "skill_packet": adapted.get("packet"),
            "skill_adapter": adapted.get("adapter"),
            "gate_token_id": decision.token.get("token_id"),
            "evidence_refs": [*evidence_refs, *([path] if path else [])],
        }

    def _execute_repair_action(
        self,
        *,
        task_id: str,
        action: str,
        failure: dict[str, Any],
        failed_step: dict[str, Any],
        plan_version: int,
    ) -> dict[str, Any]:
        """Apply one bounded repair that must change executable state."""
        record = self.blackboard.get(task_id)
        metadata = dict(record.task_metadata)
        before: Any
        after: Any
        change_refs: list[str] = []
        repair_round = int(failure.get("repair_rounds") or 0)

        if action == "refresh_skill_route":
            before = metadata.get("route_repair_context") or {}
            after = {
                "failure_id": failure.get("failure_id"),
                "failed_step_id": failure.get("step_id"),
                "repair_round": repair_round,
                "review_feedback": failure.get("error"),
            }
            self._set_v2_metadata(task_id, route_repair_context=after)
            path = self._write_v2_json(task_id, "route_repair_context.json", after)
            change_refs.append(path)
        elif action == "regenerate_plan":
            before = metadata.get("plan_repair_context") or {}
            after = {
                "failure_id": failure.get("failure_id"),
                "failed_step_id": failure.get("step_id"),
                "repair_round": repair_round,
                "previous_plan_version": plan_version,
                "next_plan_version": plan_version + 1,
                "validation_feedback": failure.get("error"),
            }
            self._set_v2_metadata(task_id, plan_repair_context=after)
            path = self._write_v2_json(task_id, "plan_repair_context.json", after)
            change_refs.append(path)
        elif action in {"supplement_read_only_evidence", "revise_modeling_input"}:
            request = dict(metadata.get("modeling_request") or {})
            before = json.loads(json.dumps(request, ensure_ascii=False, default=str))
            failure_details = dict((metadata.get("modeling_preparation_result") or {}).get("failure") or {})
            missing_inputs = [str(item) for item in failure_details.get("missing_inputs") or []]
            blockers = [str(item) for item in failure_details.get("blockers") or []]
            if failure.get("error"):
                blockers.append(str(failure["error"]))
            if action == "supplement_read_only_evidence":
                additions = self.modeling_preparation.recover_missing_evidence(
                    request,
                    missing_inputs=missing_inputs,
                    blockers=blockers,
                )
                existing = list(request.get("evidence") or [])
                existing_ids = {str(item.get("id")) for item in existing if isinstance(item, dict)}
                evidence_changed = False
                for item in additions:
                    if isinstance(item, dict) and str(item.get("id")) not in existing_ids:
                        existing.append(dict(item))
                        existing_ids.add(str(item.get("id")))
                        evidence_changed = True
                if evidence_changed:
                    request["evidence"] = existing
            else:
                repair = self.modeling_preparation.repair_non_protected_modeling_request(
                    request,
                    missing_inputs=missing_inputs,
                    blockers=blockers,
                )
                request = dict(repair["request"])
            after = request
            if stable_hash(before) != stable_hash(after):
                self._set_v2_metadata(task_id, modeling_request=after)
                path = self._write_v2_json(task_id, f"modeling_request_repair_{repair_round}.json", after)
                change_refs.append(path)
        elif action == "regenerate_artifact":
            before = metadata.get("modeling_preparation_result") or {}
            task_root = Path(self.settings.workspace_root) / "tasks" / task_id
            attempt = int(metadata.get("modeling_preparation_attempt") or 0)
            source = task_root / "modeling_preparation" / f"attempt_{attempt:02d}"
            archive = task_root / "repair_archive" / f"artifact_round_{repair_round}"
            if source.exists():
                archive.parent.mkdir(parents=True, exist_ok=True)
                if archive.exists():
                    shutil.rmtree(archive)
                shutil.move(str(source), str(archive))
                change_refs.append(str(archive))
            after = {
                "regenerate": True,
                "failure_id": failure.get("failure_id"),
                "repair_round": repair_round,
                "archived_previous_attempt": str(archive) if archive.exists() else None,
            }
            path = self._write_v2_json(task_id, f"artifact_regeneration_{repair_round}.json", after)
            change_refs.append(path)
            self._set_v2_metadata(task_id, modeling_preparation_result={})
        else:
            return {
                "status": "blocked",
                "changed": False,
                "change_refs": [],
                "reason": f"unsupported automatic repair action: {action}",
            }

        changed = stable_hash(before) != stable_hash(after) and bool(change_refs)
        manifest = {
            "schema_version": "1.0",
            "task_id": task_id,
            "failure_id": failure.get("failure_id"),
            "action": action,
            "repair_round": repair_round,
            "before_hash": stable_hash(before),
            "after_hash": stable_hash(after),
            "changed": changed,
            "change_refs": change_refs,
            "created_at": now_iso(),
        }
        manifest_path = self._write_v2_json(task_id, f"repair_change_{repair_round}.json", manifest)
        if manifest_path not in change_refs:
            change_refs.append(manifest_path)
        self._append_v2_artifacts(
            task_id,
            [
                self._v2_artifact(
                    f"repair_change_round_{repair_round}",
                    manifest_path,
                    "repair_change_manifest",
                    metadata={"no_cst_execution": True, "failure_id": failure.get("failure_id")},
                )
            ],
        )
        return {
            "status": "executed" if changed else "no_change",
            "changed": changed,
            "change_refs": change_refs,
            "before_hash": manifest["before_hash"],
            "after_hash": manifest["after_hash"],
            "repair_manifest": manifest_path,
            "reason": "repair changed executable state" if changed else "repair input did not change",
        }

    def _dynamic_evidence_refs(self, metadata: dict[str, Any]) -> list[str]:
        return [str(item.get("path")) for item in (metadata.get("artifacts") or []) if item.get("path")]

    def _step_external_llm_allowed(self, task_id: str, step: dict[str, Any]) -> bool:
        return task_id in self._external_llm_approved_tasks and bool(step.get("external_llm_required", False))

    def _skill_artifact_refs(self, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for index, artifact in enumerate(artifacts):
            path = str(artifact.get("path") or "")
            artifact_type = str(artifact.get("artifact_type") or artifact.get("type") or "artifact")
            name = str(artifact.get("name") or (Path(path).name if path else f"artifact_{index + 1}"))
            refs.append(
                {
                    "schema_version": "1.0",
                    "id": str(artifact.get("id") or stable_hash({"path": path, "type": artifact_type})[:16]),
                    "name": name,
                    "artifact_type": artifact_type,
                    "source_skill": str(artifact.get("source_skill") or artifact.get("created_by") or "antenna_agent_lab"),
                    "status": str(artifact.get("status") or "ready"),
                    "path": path,
                    "metadata": dict(artifact.get("metadata") or {}),
                }
            )
        return refs

    def _latest_skill_packet_path(self, artifacts: list[dict[str, Any]], allowed_types: set[str]) -> str | None:
        for artifact in reversed(artifacts):
            if artifact.get("artifact_type") not in allowed_types:
                continue
            path = str(artifact.get("path") or "")
            if path and Path(path).is_file():
                return path
        return None

    def _baseline_ablation_plan(
        self,
        user_input: str,
        artifacts: list[dict[str, Any]],
        geometry_ref: str,
    ) -> dict[str, Any]:
        profile = self.paperwise._query_profile(user_input)
        algorithms = list(profile.get("algorithms") or ["candidate"])
        metrics = [str(item).upper() for item in (profile.get("objectives") or ["s11"])]
        jobs = []
        for index, algorithm in enumerate(algorithms):
            jobs.append(
                {
                    "job_id": f"{algorithm}_{index + 1}",
                    "role": "candidate" if index == 0 else "baseline",
                    "algorithm": algorithm,
                    "geometry_ref": geometry_ref,
                    "metrics": metrics,
                }
            )
        return {
            "schema_version": "1.0",
            "run_id": stable_hash({"goal": user_input, "geometry_ref": geometry_ref})[:16],
            "experiment_ref": self._latest_skill_packet_path(artifacts, {"experiment_contract"}) or "",
            "geometry_ref": geometry_ref,
            "metrics": metrics,
            "jobs": jobs,
            "baselines": [job for job in jobs if job["role"] == "baseline"],
            "ablations": [{"name": "candidate_without_adaptive_component", "source_job": jobs[0]["job_id"]}],
            "export_requirements": ["S11", "bandwidth"],
            "created_at": now_iso(),
        }

    def _paperwise_required_but_unavailable(self, paper_report_path: str | None, require_paperwise: bool) -> str | None:
        if not require_paperwise:
            return None
        if paper_report_path:
            report = self.paperwise.read_report(paper_report_path)
            return None if report.get("available") else str(report.get("error") or "paper_report_unavailable")
        inventory = self.paperwise.inventory()
        if inventory.get("reports") or inventory.get("vector_library_exists") or inventory.get("graph_library_exists"):
            return None
        return "paperwise_evidence_required_but_no_report_vector_or_graph_available"

    def _default_dynamic_title(self, user_input: str) -> str:
        text = str(user_input or "").lower()
        if any(term in text for term in ("antenna", "s11", "cst", "gain", "arbw", "天线", "贴片")):
            if any(term in text for term in ("paper", "论文", "复现")):
                return "动态计划 - 天线论文复现"
            if "cst" in text:
                return "动态计划 - CST 天线任务"
            return "动态计划 - 天线研究任务"
        return "动态计划 - 通用任务"

    def _persist_skill_route_result(
        self,
        *,
        task_id: str,
        mode: str,
        plan: dict[str, Any],
        review: dict[str, Any],
    ) -> None:
        """Persist one graph-produced initial skill route and review."""
        route_dir = Path(self.settings.workspace_root) / "tasks" / task_id / "skill_routes"
        plan_path = route_dir / "skill_route_plan.json"
        review_path = route_dir / "skill_route_review.json"
        atomic_write_json(plan_path, plan)
        atomic_write_json(review_path, review)

        record = self.blackboard.get(task_id)
        metadata = dict(record.task_metadata)
        artifacts = list(metadata.get("artifacts") or [])
        route_artifacts = [
            self._artifact_ref(
                "skill_route_plan",
                str(plan_path),
                "skill_route_plan",
                "central_skill_router",
                metadata={"mode": mode, "data_egress": False},
            ),
            self._artifact_ref(
                "skill_route_review",
                str(review_path),
                "skill_route_review",
                "skill_route_review_agent",
                metadata={"mode": mode, "data_egress": False},
            ),
        ]
        seen = {item.get("id") or item.get("path") for item in artifacts}
        for artifact in route_artifacts:
            key = artifact.get("id") or artifact.get("path")
            if key not in seen:
                artifacts.append(artifact)
                seen.add(key)
        metadata.update(
            {
                "initial_skill_route_plan": metadata.get("initial_skill_route_plan") or plan,
                "active_skill_route_plan": plan,
                "skill_route_plan": plan,
                "skill_route_review": review,
                "skill_route_plan_history": [
                    *(metadata.get("skill_route_plan_history") or []),
                    {
                        "plan": plan,
                        "review": review,
                        "reason": "initial_route" if not metadata.get("skill_route_plan_history") else "route_refresh",
                    },
                ],
                "artifacts": artifacts,
            }
        )
        self.blackboard.update(task_id, task_metadata=metadata)
        self.audit.emit(
            task_id,
            {
                "type": "skill_route_selected",
                "mode": mode,
                "decision": plan.get("decision"),
                "selected_owner_skills": plan.get("selected_owner_skills", []),
                "review_status": review.get("status"),
                "plan_path": str(plan_path),
                "review_path": str(review_path),
            },
        )
    def _has_approved_external_llm(self, task_id: str) -> bool:
        """检查任务是否已有外部 LLM 审批。"""
        approval = self.blackboard.get(task_id).approvals.get(f"external_llm:{task_id}")
        return isinstance(approval, dict) and approval.get("status") == "approved"

    def _external_llm_enabled(self) -> bool:
        return bool(self.settings.llm_enabled or runtime_llm_config.enabled)

    def _external_llm_egress_plan(self, operation: str, text: str, purpose: str) -> dict[str, Any]:
        if runtime_llm_config.enabled:
            return {
                "data_egress": True,
                "egress_target": runtime_llm_config.base_url,
                "operation": operation,
                "model": runtime_llm_config.model_name,
                "purpose": purpose,
                "input_hash": stable_hash(text),
                "input_chars": len(text),
            }
        return self.llm.egress_plan(operation, text, purpose)

    def _step_route_context(
        self,
        task_id: str,
        step: dict[str, Any],
        module_review_records: list[dict[str, Any]],
        global_review_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        metadata = self.blackboard.get(task_id).task_metadata
        evidence_terms: list[str] = []
        evidence = metadata.get("evidence_pool_summary") or {}
        for source in (evidence.get("sources") or {}).values():
            for item in source.get("items") or source.get("candidates") or []:
                evidence_terms.extend([item.get("title"), item.get("display_title"), item.get("description"), item.get("snippet")])
        memory_context = metadata.get("retrieval_context") or {}
        for item in [*(memory_context.get("l2") or []), *(memory_context.get("l3") or [])]:
            evidence_terms.extend([item.get("fact"), item.get("workflow_key"), item.get("goal_pattern"), item.get("domain")])
        review_suggested: list[str] = []
        missing_dependency: list[str] = []
        for review in [*module_review_records, *global_review_records]:
            output = review.get("output") or review
            suggestion = output.get("reroute_suggestion") or {}
            for suggested_step in suggestion.get("suggested_steps") or []:
                review_suggested.extend(suggested_step.get("required_skills") or [])
            reflection = output.get("reflection") or {}
            if reflection.get("error_type") == "missing_dependency":
                missing_dependency.extend(step.get("required_skills") or [])
        return {
            "context_task_id": task_id,
            "is_antenna_context": bool((metadata.get("skill_route_plan") or {}).get("selected_owner_skills")) or bool(step.get("required_skills")),
            "evidence_terms": [str(item) for item in evidence_terms if item],
            "evidence_suggested_skills": step.get("required_skills") or [],
            "review_suggested_skills": sorted(set(review_suggested)),
            "missing_dependency_skills": sorted(set(missing_dependency)),
        }

    def recover_tasks(self) -> list[dict[str, Any]]:
        """恢复工作区中未完成的任务。"""
        recovered = []
        for record in self.blackboard.load_existing():
            if record.state == "running":
                if record.task_metadata.get("mode") != "real":
                    blockers = list(record.blockers)
                    blockers.append({"type": "interrupted_dynamic_execution", "reason": "non-CST dynamic steps must be restarted from a new task"})
                    self.blackboard.update(record.task_id, state="failed", blockers=blockers)
                    self._set_v2_metadata(record.task_id, current_stage="failed", failure_reason="interrupted_dynamic_execution")
                    continue
                approvals = dict(record.approvals)
                approval_id = f"real_cst:{record.task_id}"
                if approval_id in approvals:
                    approval = dict(approvals[approval_id])
                    approval.update({"status": "waiting_approval", "run_count": 0, "recovered_at": now_iso()})
                    approvals[approval_id] = approval
                self.blackboard.update(record.task_id, state="waiting_approval", approvals=approvals)
                self._set_v2_metadata(record.task_id, cst_status="interrupted_waiting_reapproval")
                self._event(record.task_id, "scheduler", "status", "pending", "Recovered interrupted running task; waiting for approval")
            if record.state in {"running", "waiting_approval"}:
                recovered.append(self.blackboard.get(record.task_id).to_dict())
        return recovered

    def _ensure_real_cst_approval(self, task_id: str) -> dict[str, Any]:
        record = self.blackboard.get(task_id)
        plan = dict(record.task_metadata.get("dynamic_plan") or {})
        request = dict(record.task_metadata.get("request") or {})
        approval_id = f"real_cst:{task_id}"
        expected = {
            "approval_id": approval_id,
            "task_id": task_id,
            "mode": "real",
            "required_before": "cst_single_run",
            "plan_id": plan.get("plan_id"),
            "plan_version": int(plan.get("plan_version") or 1),
            "step_id": "step_003_cst_run",
            "request_hash": stable_hash(request),
        }
        approvals = dict(record.approvals)
        current = dict(approvals.get(approval_id) or {})
        binding_changed = any(current.get(key) != value for key, value in expected.items() if key != "approval_id")
        if not current or binding_changed:
            current = {
                **expected,
                "status": "waiting_approval",
                "reason": "真实 CST solver 会启动本机 CST/license，需要人工批准",
                "run_count": 0,
                "attempt_count": 0,
            }
            approvals[approval_id] = current
            self.blackboard.update(task_id, approvals=approvals)
        self._set_v2_metadata(task_id, request_hash=expected["request_hash"])
        return current

    def _real_cst_approval_valid(self, task_id: str) -> bool:
        record = self.blackboard.get(task_id)
        approval = record.approvals.get(f"real_cst:{task_id}") or {}
        plan = record.task_metadata.get("dynamic_plan") or {}
        request = record.task_metadata.get("request") or {}
        return bool(
            approval.get("status") == "approved"
            and approval.get("task_id") == task_id
            and approval.get("request_hash") == stable_hash(request)
            and approval.get("plan_id") == plan.get("plan_id")
            and approval.get("plan_version") == int(plan.get("plan_version") or 1)
            and approval.get("step_id") == "step_003_cst_run"
        )

    def _review_paperwise_evidence(
        self,
        query: str,
        evidence_pool: dict[str, Any],
        allow_runtime_llm: bool = False,
    ) -> dict[str, Any]:
        reviewed = dict(evidence_pool)
        profile = reviewed.get("relevance_profile") or self.paperwise._query_profile(query)
        if allow_runtime_llm:
            llm_result = self._runtime_llm_review_paperwise_evidence(query, profile, evidence_pool)
        else:
            llm_result = {
                "reviews": {},
                "expanded_candidates": [],
                "llm_review": {
                    "attempted": False,
                    "success": False,
                    "error": "external_llm_not_approved",
                    "model": runtime_llm_config.model_name,
                    "base_url": runtime_llm_config.base_url,
                },
            }
        llm_reviews = llm_result.get("reviews", {})
        llm_review_status = llm_result.get("llm_review", {})
        llm_expanded_candidates = list(llm_result.get("expanded_candidates") or [])
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        uncertain: list[dict[str, Any]] = []
        reviewed_sources: dict[str, Any] = {}
        gate_items: list[dict[str, Any]] = []

        for source_name, source in (reviewed.get("sources") or {}).items():
            next_source = dict(source)
            items = list(source.get("items") or [])
            next_items = []
            for item in items:
                item_key = str(item.get("path") or item.get("title") or item.get("display_title") or "")
                review = llm_reviews.get(item_key)
                if review is None and source_name == "graph_library":
                    review = self._llm_required_graph_evidence_review(query, item, llm_review_status)
                if review is None:
                    review = self._fallback_react_evidence_review(query, profile, source_name, item)
                next_item = dict(item)
                next_item["react_review"] = review
                gate = self._evidence_modeling_gate_review(query, profile, source_name, next_item, review, llm_review_status)
                next_item["evidence_gate"] = gate
                next_items.append(next_item)
                gate_items.append(gate)
                decision = self._gate_bucket_decision(review, gate)
                if decision == "accept":
                    accepted.append(next_item)
                elif decision == "reject":
                    rejected.append(next_item)
                else:
                    uncertain.append(next_item)
            next_source["items"] = next_items
            next_source["accepted_count"] = sum(1 for item in next_items if item.get("evidence_gate", {}).get("adoption_decision") == "adopt")
            next_source["rejected_count"] = sum(1 for item in next_items if item.get("evidence_gate", {}).get("adoption_decision") == "block")
            next_source["uncertain_count"] = len(next_items) - next_source["accepted_count"] - next_source["rejected_count"]
            reviewed_sources[source_name] = next_source

        if llm_expanded_candidates:
            expansion_items = []
            for item in llm_expanded_candidates:
                review = {
                    "schema_version": "1.0",
                    "review_style": "react",
                    "thought": f"外接 LLM 为任务补充候选：{query}",
                    "action": "runtime_llm_semantic_expand_candidate",
                    "observation": {"source": "llm_semantic_expansion", "matched": {}},
                    "decision": "uncertain",
                    "match_tier": "match_60",
                    "match_percent": 60,
                    "reason": item.get("description") or "LLM 语义扩展候选，尚未绑定到 PaperWise 论文证据。",
                    "roles": ["semantic_expansion"],
                    "data_egress": True,
                }
                next_item = dict(item)
                next_item["react_review"] = review
                gate = self._evidence_modeling_gate_review(query, profile, "llm_semantic_expansion", next_item, review, llm_review_status)
                next_item["evidence_gate"] = gate
                expansion_items.append(next_item)
                gate_items.append(gate)
                uncertain.append(next_item)
            reviewed_sources["llm_semantic_expansion"] = {
                "status": "candidate_only",
                "roles": ["semantic_expansion", "reason_explanation"],
                "description": "外接 LLM 只做语义补充、候选扩展和理由解释；没有绑定 PaperWise 论文证据前不能被采用。",
                "support_level": "blocked_until_paperwise_evidence",
                "review_requirement": "must_bind_to_paperwise_report_vector_or_graph_before_adoption",
                "read_only": True,
                "items": expansion_items,
                "count": len(expansion_items),
                "accepted_count": 0,
                "rejected_count": sum(1 for item in expansion_items if item.get("evidence_gate", {}).get("adoption_decision") == "block"),
                "uncertain_count": sum(1 for item in expansion_items if item.get("evidence_gate", {}).get("adoption_decision") != "block"),
            }

        evidence_gaps = self._evidence_review_gaps(profile, accepted)
        reviewed["sources"] = reviewed_sources
        reviewed["review_agent"] = "evidence_relevance_review_agent"
        has_graph_items = bool((reviewed.get("sources") or {}).get("graph_library", {}).get("items"))
        if llm_reviews:
            reviewed["review_mode"] = "runtime_llm_react"
        elif has_graph_items:
            reviewed["review_mode"] = "local_react_fallback_with_graph_llm_required"
        else:
            reviewed["review_mode"] = "local_react_fallback"
        reviewed["accelerator_policy"] = {
            "schema_version": "1.0",
            "reports_and_deep_reading": "local hard constraints plus reproducible ranking; runtime LLM may supplement reasons and expansion.",
            "deep_read_papers": "local hard constraints plus reproducible ranking; vector chunks are traced back to source papers before review.",
            "graph_library": "candidate relation pool for innovation; runtime LLM semantic review is required before adoption.",
            "final_decider": "evidence_modeling_gate_agent and reviewer, not local matching or LLM alone.",
            "gate_checks": [
                "has_paper_evidence",
                "em_logic_ok",
                "parameter_modelable",
                "optimization_range_reasonable",
                "feed_port_boundary_safe",
            ],
        }
        reviewed["llm_review"] = llm_review_status
        reviewed["gate_summary"] = self._evidence_gate_summary(gate_items)
        reviewed["react_review"] = {
            "accepted": [self._compact_evidence_review_item(item) for item in accepted],
            "rejected": [self._compact_evidence_review_item(item) for item in rejected],
            "uncertain": [self._compact_evidence_review_item(item) for item in uncertain],
            "evidence_gaps": evidence_gaps,
        }
        reviewed["status"] = "available" if accepted else "insufficient_evidence"
        reviewed["support_level"] = "reviewed_candidate_evidence" if accepted else "insufficient_evidence"
        reviewed["evidence_level"] = "paperwise_reviewed" if accepted else "insufficient_evidence"
        reviewed["insufficiencies"] = evidence_gaps
        return reviewed

    def _gate_bucket_decision(self, review: dict[str, Any], gate: dict[str, Any]) -> str:
        if gate.get("adoption_decision") == "adopt":
            return "accept"
        if gate.get("adoption_decision") == "block":
            return "reject"
        return "uncertain"

    def _evidence_gate_summary(self, gates: list[dict[str, Any]]) -> dict[str, Any]:
        decisions = [str(gate.get("adoption_decision") or "unknown") for gate in gates]
        blocker_counts: dict[str, int] = {}
        warning_counts: dict[str, int] = {}
        for gate in gates:
            for blocker in gate.get("blockers") or []:
                blocker_counts[str(blocker)] = blocker_counts.get(str(blocker), 0) + 1
            for warning in gate.get("warnings") or []:
                warning_counts[str(warning)] = warning_counts.get(str(warning), 0) + 1
        return {
            "schema_version": "1.0",
            "total": len(gates),
            "adopted": decisions.count("adopt"),
            "blocked": decisions.count("block"),
            "needs_more_evidence": decisions.count("needs_more_evidence"),
            "blockers": blocker_counts,
            "warnings": warning_counts,
            "applies_to_sources": ["reports", "deep_read_papers", "graph_library", "llm_semantic_expansion"],
            "gate_agent": "evidence_modeling_gate_agent",
        }

    def _evidence_modeling_gate_review(
        self,
        query: str,
        profile: dict[str, Any],
        source_name: str,
        item: dict[str, Any],
        review: dict[str, Any],
        llm_status: dict[str, Any],
    ) -> dict[str, Any]:
        text = " ".join(
            str(item.get(key) or "")
            for key in ("display_title", "title", "original_title", "description", "snippet", "path", "relation")
        ).lower()
        matched = (item.get("relevance") or {}).get("matched") or (review.get("observation") or {}).get("matched") or {}
        roles = set(review.get("roles") or [])
        review_decision = str(review.get("decision") or "uncertain").lower()
        has_paper_evidence = bool(item.get("path") and (item.get("description") or item.get("snippet") or item.get("title")))
        relevance_review_ok = review_decision != "reject"
        em_logic_ok = bool(matched.get("structures") or "structure" in roles or profile.get("structures"))
        parameter_modelable = bool(matched.get("modeling_terms") or source_name in {"reports", "deep_read_papers"})
        optimization_range_reasonable = bool(matched.get("objectives") or "metric" in roles or profile.get("objectives"))
        breaks_feed_port_boundary = any(
            token in text
            for token in (
                "break feed",
                "broken feed",
                "port mismatch",
                "boundary error",
                "invalid boundary",
                "floating ground",
            )
        )
        if source_name == "graph_library":
            semantic_review_ready = bool(llm_status.get("success") and review.get("decision") == "accept")
        elif source_name == "llm_semantic_expansion":
            semantic_review_ready = bool(llm_status.get("success"))
        else:
            semantic_review_ready = True
        checks = {
            "has_paper_evidence": has_paper_evidence,
            "relevance_review_ok": relevance_review_ok,
            "em_logic_ok": em_logic_ok,
            "parameter_modelable": parameter_modelable,
            "optimization_range_reasonable": optimization_range_reasonable,
            "feed_port_boundary_safe": not breaks_feed_port_boundary,
            "semantic_review_ready": semantic_review_ready,
        }
        blockers = [name for name, ok in checks.items() if not ok and name in {"has_paper_evidence", "relevance_review_ok", "em_logic_ok", "feed_port_boundary_safe", "semantic_review_ready"}]
        warnings = [name for name, ok in checks.items() if not ok and name not in blockers]
        if blockers:
            adoption = "block"
        elif warnings:
            adoption = "needs_more_evidence"
        else:
            adoption = "adopt"
        return {
            "schema_version": "1.0",
            "gate_agent": "evidence_modeling_gate_agent",
            "source_skill": r"C:\Users\30626\.codex\skills\Antenna Skills\antenna-research-ideation",
            "basis": "Applies antenna-research-ideation evidence gates to PaperWise reports, deep-read papers traced from vector chunks, graph relations, and LLM semantic expansions: paper evidence, EM logic, modelable parameters, reasonable optimization range, feed/port/boundary safety.",
            "adoption_decision": adoption,
            "checks": checks,
            "blockers": blockers,
            "warnings": warnings,
            "reason": self._evidence_modeling_gate_reason(query, source_name, adoption, blockers, warnings),
        }

    def _evidence_modeling_gate_reason(self, query: str, source_name: str, adoption: str, blockers: list[str], warnings: list[str]) -> str:
        if adoption == "adopt":
            return f"{source_name} candidate can be adopted for {query}: paper-backed evidence and modeling-safety gates are satisfied."
        if blockers:
            return f"{source_name} candidate blocked before adoption: {', '.join(blockers)}."
        return f"{source_name} candidate remains usable as evidence but needs confirmation before adoption: {', '.join(warnings)}."

    def _llm_required_graph_evidence_review(self, query: str, item: dict[str, Any], llm_status: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "review_style": "react",
            "thought": f"graph relation evidence for {query} requires semantic LLM review before adoption",
            "action": "require_runtime_llm_for_graph_evidence_review",
            "observation": {
                "source": "graph_library",
                "llm_attempted": bool(llm_status.get("attempted")),
                "llm_success": bool(llm_status.get("success")),
                "llm_error": llm_status.get("error"),
            },
            "decision": "uncertain",
            "match_tier": "llm_required",
            "match_percent": 0,
            "reason": "图谱关系只能作为候选池；当前 LLM 未成功返回，所以不采用图谱候选，只保留为待语义审查证据。",
            "roles": ["innovation", "relation"],
            "data_egress": False,
        }

    def _runtime_llm_review_paperwise_evidence(
        self,
        query: str,
        profile: dict[str, Any],
        evidence_pool: dict[str, Any],
    ) -> dict[str, Any]:
        if not runtime_llm_config.enabled or not runtime_llm_config.base_url or not runtime_llm_config.api_key or not runtime_llm_config.model_name:
            return {
                "reviews": {},
                "llm_review": {
                    "attempted": False,
                    "success": False,
                    "error": "runtime_llm_not_configured",
                    "model": runtime_llm_config.model_name,
                },
            }
        candidates = []
        for source_name, source in (evidence_pool.get("sources") or {}).items():
            for item in list(source.get("items") or [])[:10]:
                key = str(item.get("path") or item.get("title") or item.get("display_title") or "")
                candidates.append(
                    {
                        "key": key,
                        "source": source_name,
                        "title": item.get("display_title") or item.get("title"),
                        "original_title": item.get("original_title"),
                        "description": item.get("description") or item.get("snippet") or "",
                        "relevance": item.get("relevance") or {},
                        "match_tier": (item.get("relevance") or {}).get("match_tier"),
                        "match_percent": (item.get("relevance") or {}).get("match_percent"),
                    }
                )
        if not candidates:
            return {
                "reviews": {},
                "llm_review": {
                    "attempted": False,
                    "success": False,
                    "error": "no_paperwise_candidates",
                    "model": runtime_llm_config.model_name,
                },
            }
        prompt = {
            "task": query,
            "profile": profile,
            "instruction": (
                "Review each PaperWise candidate for antenna-task relevance. "
                "Sources include PaperWise deep-reading reports, deep-read papers traced from vector-library chunks, graph-library relations, and LLM semantic expansion candidates. "
                "The LLM may provide semantic supplement, candidate expansion, and Chinese reasons, but final adoption is decided later by evidence_modeling_gate_agent. "
                "Return strict JSON: {\"reviews\":[{\"key\":\"...\",\"decision\":\"accept|reject|uncertain\","
                "\"match_tier\":\"exact|match_80|match_60|reject\","
                "\"reason\":\"Chinese reason\",\"roles\":[\"structure|metric|algorithm|reproduction|innovation\"]}],"
                "\"expanded_candidates\":[{\"title\":\"...\",\"reason\":\"Chinese reason\",\"evidence_hint\":\"source title or concept\"}]}. "
                "Use exact only when all requested antenna type, metric, and algorithm match. "
                "Use match_80 or match_60 for partial-but-useful candidates, keeping the requested antenna type as a hard gate. "
                "Do not treat semantic guesses as adopted evidence unless they are backed by PaperWise report/vector/graph sources."
            ),
            "candidates": candidates,
        }
        try:
            from openai import OpenAI

            client = OpenAI(api_key=runtime_llm_config.api_key, base_url=runtime_llm_config.base_url, timeout=8)
            response = client.chat.completions.create(
                model=runtime_llm_config.model_name,
                temperature=0,
                messages=[
                    {"role": "system", "content": "You are an antenna evidence relevance reviewer. Return strict JSON only."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
            )
            raw = response.choices[0].message.content or "{}"
            parsed = json.loads(raw)
        except Exception as exc:
            return {
                "reviews": {},
                "llm_review": {
                    "attempted": True,
                    "success": False,
                    "error": str(exc),
                    "model": runtime_llm_config.model_name,
                    "base_url": runtime_llm_config.base_url,
                },
            }
        reviews = {}
        for review in parsed.get("reviews") or []:
            key = str(review.get("key") or "")
            decision = str(review.get("decision") or "uncertain").lower()
            if decision not in {"accept", "reject", "uncertain"}:
                decision = "uncertain"
            match_tier = str(review.get("match_tier") or "match_60").lower()
            if match_tier not in {"exact", "match_80", "match_60", "reject"}:
                match_tier = "match_60"
            if match_tier == "reject":
                decision = "reject"
            reviews[key] = {
                "schema_version": "1.0",
                "review_style": "react",
                "thought": f"目标是判断候选是否服务于任务：{query}",
                "action": "runtime_llm_inspect_candidate",
                "observation": {"source": "runtime_frontend_llm", "model": runtime_llm_config.model_name},
                "decision": decision,
                "match_tier": match_tier,
                "match_percent": {"exact": 100, "match_80": 80, "match_60": 60, "reject": 0}[match_tier],
                "reason": str(review.get("reason") or "LLM 未给出明确原因。"),
                "roles": list(review.get("roles") or []),
                "data_egress": True,
            }
        expanded_candidates = []
        for candidate in parsed.get("expanded_candidates") or []:
            expanded_candidates.append(
                {
                    "schema_version": "1.0",
                    "source": "llm_semantic_expansion",
                    "title": str(candidate.get("title") or "LLM semantic expansion"),
                    "description": str(candidate.get("reason") or ""),
                    "evidence_hint": str(candidate.get("evidence_hint") or ""),
                    "status": "candidate_only",
                    "adoption_decision": "blocked_until_paperwise_evidence",
                }
            )
        return {
            "reviews": reviews,
            "llm_review": {
                "attempted": True,
                "success": True,
                "error": None,
                "model": runtime_llm_config.model_name,
                "base_url": runtime_llm_config.base_url,
                "candidate_count": len(candidates),
                "review_count": len(reviews),
                "expanded_candidate_count": len(expanded_candidates),
            },
            "expanded_candidates": expanded_candidates,
        }

    def _fallback_react_evidence_review(self, query: str, profile: dict[str, Any], source_name: str, item: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(
            str(item.get(key) or "")
            for key in ("display_title", "title", "original_title", "description", "snippet", "path", "relation")
        ).lower()
        relevance = item.get("relevance") or self.paperwise._relevance(text, profile)
        matched = relevance.get("matched") or {}
        has_structure = bool(matched.get("structures"))
        has_objective = bool(matched.get("objectives"))
        has_algorithm = bool(matched.get("algorithms"))
        has_parameter_count = bool(matched.get("parameter_count_match"))
        match_tier = relevance.get("match_tier")
        rejected_reason = ""
        if relevance.get("reason") == "excluded_non_antenna_topic":
            decision = "reject"
            rejected_reason = "明显属于非目标天线任务或医学/成像噪声。"
        elif match_tier == "reject" or not relevance.get("accepted"):
            decision = "reject"
            rejected_reason = "四项匹配（天线类型、指标、算法、参数数量）命中不足。"
        elif source_name == "graph_library" and (has_structure or has_objective or has_algorithm or has_parameter_count):
            decision = "accept"
            rejected_reason = "图谱关系可作为创新/关系候选证据，但需和论文/向量证据交叉确认。"
        elif match_tier == "exact":
            decision = "accept"
            rejected_reason = "四项匹配完全命中或已满足全部请求项。"
        elif match_tier in {"match_80", "match_60"}:
            decision = "uncertain"
            rejected_reason = "只命中部分任务要素，需要人工或 LLM 进一步判断。"
        else:
            decision = "reject"
            rejected_reason = "与当前任务缺少可解释关联。"
        roles = []
        if has_structure:
            roles.append("structure")
        if has_objective:
            roles.append("metric")
        if has_algorithm:
            roles.append("algorithm")
        if has_parameter_count:
            roles.append("parameter_count")
        if source_name == "reports":
            roles.append("reproduction")
        if source_name == "graph_library":
            roles.append("innovation")
        return {
            "schema_version": "1.0",
            "review_style": "react",
            "thought": f"目标是判断候选是否服务于任务：{query}",
            "action": "inspect_candidate_title_snippet_relation_and_matched_terms",
            "observation": {
                "source": source_name,
                "matched": matched,
                "score": relevance.get("score", 0),
                "reason": relevance.get("reason"),
            },
            "decision": decision,
            "match_tier": relevance.get("match_tier"),
            "match_percent": relevance.get("match_percent"),
            "reason": rejected_reason,
            "roles": roles,
            "data_egress": False,
        }

    def _compact_evidence_review_item(self, item: dict[str, Any]) -> dict[str, Any]:
        review = item.get("react_review") or {}
        return {
            "source": item.get("source"),
            "title": item.get("title"),
            "display_title": item.get("display_title"),
            "path": item.get("path"),
            "decision": review.get("decision"),
            "reason": review.get("reason"),
            "roles": review.get("roles", []),
            "score": (review.get("observation") or {}).get("score"),
            "match_tier": review.get("match_tier"),
            "match_percent": review.get("match_percent"),
        }

    def _evidence_review_gaps(self, profile: dict[str, Any], accepted: list[dict[str, Any]]) -> list[str]:
        text = json.dumps([item.get("react_review", {}).get("observation", {}).get("matched", {}) for item in accepted], ensure_ascii=False).lower()
        gaps = []
        for name in profile.get("structures") or []:
            if name not in text:
                gaps.append(f"structure:{name}")
        for name in profile.get("objectives") or []:
            if name not in text:
                gaps.append(f"objective:{name}")
        for name in profile.get("algorithms") or []:
            if name not in text:
                gaps.append(f"algorithm:{name}")
        return gaps

    def _set_v2_metadata(self, task_id: str, **changes: Any) -> None:
        record = self.blackboard.get(task_id)
        metadata = dict(record.task_metadata)
        metadata.update(changes)
        self.blackboard.update(task_id, task_metadata=metadata)

    def _record_v2_review(self, task_id: str, review: dict[str, Any]) -> None:
        record = self.blackboard.get(task_id)
        reviews = list(record.task_metadata.get("reviews") or [])
        reviews.append(review)
        self._set_v2_metadata(task_id, reviews=reviews)

    def _append_v2_artifacts(self, task_id: str, artifacts: list[dict[str, Any]]) -> None:
        if not artifacts:
            return
        record = self.blackboard.get(task_id)
        existing = list(record.task_metadata.get("artifacts") or [])
        seen = {item.get("id") or item.get("path") for item in existing}
        for artifact in artifacts:
            key = artifact.get("id") or artifact.get("path")
            if key and key not in seen:
                seen.add(key)
                existing.append(artifact)
        self._set_v2_metadata(task_id, artifacts=existing)

    def _append_v2_reports(self, task_id: str, reports: list[dict[str, Any]]) -> None:
        record = self.blackboard.get(task_id)
        existing = list(record.task_metadata.get("reports") or [])
        existing.extend(reports)
        self._set_v2_metadata(task_id, reports=existing)

    def _write_v2_json(self, task_id: str, name: str, data: dict[str, Any]) -> str:
        path = Path(self.settings.workspace_root) / "tasks" / task_id / "v2_0" / name
        atomic_write_json(path, data)
        return str(path)

    def _v2_artifact(
        self,
        name: str,
        path: str,
        artifact_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": stable_hash({"name": name, "path": path})[:16],
            "name": name,
            "path": path,
            "artifact_type": artifact_type,
            "source": "antenna_agent_lab_v2_0",
            "schema_version": "1.0",
            "status": "ready" if path else "not_available",
            "metadata": {"mode": "real", "no_cst_execution": False, **(metadata or {})},
        }

    def _default_v2_title(self, user_input: str) -> str:
        text = user_input.lower()
        antenna_type = "贴片天线" if ("patch" in text or "贴片" in user_input) else "天线"
        metric = "S11 验证" if "s11" in text else "结果验证"
        return f"真实 CST 单次运行 - {antenna_type} - {metric}"

    def _artifact_ref(
        self,
        name: str,
        path: str,
        artifact_type: str,
        source_skill: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构造任务产物引用对象。"""
        return {
            "id": stable_hash({"name": name, "path": path})[:16],
            "name": name,
            "path": path,
            "artifact_type": artifact_type,
            "source_skill": source_skill,
            "schema_version": "1.0",
            "status": "ready",
            "metadata": {"no_cst_execution": True, **(metadata or {})},
        }

    def _retrieval_context(self, user_input: str) -> dict[str, Any]:
        """Retrieve L2 facts and L3 workflows for planning and step skill routing."""
        return self.retrieval.build_context(
            user_input,
            l2_top_k=self.settings.l2_final_top_k,
            l3_top_k=self.settings.l3_workflow_top_k,
        )

    def _write_capability_snapshot(self, task_id: str, snapshot: dict[str, Any]) -> None:
        """写入任务关联的能力快照。"""
        path = Path(self.settings.workspace_root) / "tasks" / task_id / "capability_snapshot.json"
        atomic_write_json(path, snapshot)

    def _event(self, task_id: str, node_id: str, event_type: str, status: str, message: str, reason: str | None = None) -> None:
        """发布调度节点事件。"""
        stream_event = self.stream.publish(task_id, node_id, event_type, status, message, reason=reason)
        audit_event = self.audit.emit(task_id, stream_event.to_dict())
        self.blackboard.append_event(task_id, audit_event)
