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
from agent_learning import LearningService
from .failure_store import FailureStore
from .memory import MemoryManager
from .orchestration import (
    CentralMessageServiceMixin,
    DynamicExecutionServiceMixin,
    EvidenceReviewServiceMixin,
    LearningLifecycleMixin,
    PlanningServiceMixin,
)
from .query_expansion import QueryExpander
from .retrieval import RetrievalEngine
from .runtime_llm_config import runtime_llm_config
from .skill_router import SkillRouter
from .skill_executor_gate import SkillExecutorGate
from .streaming import StreamPublisher
from .subagents import SubagentRuntime
from .utils import atomic_write_json, now_iso, stable_hash
from adapters.mcp_wrappers import MCPAntennaSkillsAdapter, MCPCstRealRunAdapter, MCPModelingPreparationAdapter
from adapters.paperwise_adapter import PaperWiseAdapter


class Scheduler(
    CentralMessageServiceMixin,
    PlanningServiceMixin,
    DynamicExecutionServiceMixin,
    EvidenceReviewServiceMixin,
    LearningLifecycleMixin,
):
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
        self.llm = LLMClient(settings.llm_base_url, settings.llm_api_key_env, settings.llm_model_name, enabled=settings.llm_enabled)
        self.query_expander = QueryExpander(
            enabled=settings.query_expansion_enabled,
            max_variants=settings.query_expansion_max_variants,
            use_llm=settings.query_expansion_use_llm,
            llm_client=self.llm,
            runtime_llm_config=runtime_llm_config,
        )
        self.learning = LearningService(
            memory,
            enabled=settings.learning_enabled,
            evolution_enabled=settings.learning_evolution_enabled,
            domain_knowledge_enabled=settings.learning_domain_knowledge_enabled,
            llm_client=self.llm,
            runtime_llm_config=runtime_llm_config,
        )
        for configured_fact in settings.learning_l2_project_facts:
            namespace = str(configured_fact.get("namespace") or "").strip()
            fact = configured_fact.get("fact")
            if namespace and fact is not None and fact != "":
                fact_record = dict(fact) if isinstance(fact, dict) else {"statement": str(fact), "confidence": 1.0}
                self.memory.store_l2(namespace, fact_record, provenance={"source_ref": "config.yaml"})
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
        # 收到用户问题后立即拓展，后续向量化/检索统一使用拓展后的 query 列表。
        query_expansion = self.query_expander.expand_report(user_input)
        query_variants = list(query_expansion["variants"])
        record = self.blackboard.create_task(
            user_input,
            capability_snapshot_id=snapshot["snapshot_id"],
            task_metadata={
                "user_input": user_input,
                "query_expansion": query_expansion,
                "mode": mode,
                "runtime_kind": "dynamic",
                "task_title": self._default_dynamic_title(user_input),
                "current_stage": "dynamic_plan",
                "current_task_subagent": None,
                "current_review_subagent": None,
                "paper_report_path": paper_report_path,
                "modeling_request": dict(modeling_request or {}),
                "require_paperwise": require_paperwise,
                "retrieval_context": self._retrieval_context(user_input, query_variants),
                "validated_learning_context": self.learning.recall_promoted(user_input, query_variants=query_variants),
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
        self._record_terminal_learning(task_id)
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
                "validated_learning_context": self.learning.recall_promoted(user_input),
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
            self._record_terminal_learning(task_id)
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
        self._record_terminal_learning(task_id)
        return self.blackboard.get(task_id).to_dict()


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

    def _retrieval_context(self, user_input: str, query_variants: list[str] | None = None) -> dict[str, Any]:
        """Retrieve L2 facts and L3 workflows for planning and step skill routing."""
        return self.retrieval.build_context(
            user_input,
            l2_top_k=self.settings.l2_final_top_k,
            l3_top_k=self.settings.l3_workflow_top_k,
            query_variants=query_variants,
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
