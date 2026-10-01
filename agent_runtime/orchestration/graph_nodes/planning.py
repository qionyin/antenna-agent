from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from ...llm import LLMGenerationError
from ...utils import now_iso
from ..retry import retry_transient_runtime_error
from ..state import DynamicGraphState


class PlanningGraphNodesMixin:
    def _initial_skill_route(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        node_id = "skill_router.task_agent"
        agent = "skill_route_task_agent" if state.get("mode") == "real" else node_id
        self.scheduler._set_v2_metadata(task_id, current_stage="skill_router", current_task_subagent=agent)
        self.scheduler.blackboard.update_node(task_id, node_id, "running", agent=agent)
        self.scheduler._event(task_id, node_id, "status", "pending", f"{node_id} running")
        try:
            metadata = self.scheduler.blackboard.get(task_id).task_metadata
            route_repair_context = metadata.get("route_repair_context") or {}
            route_input = state["user_input"]
            if route_repair_context:
                route_input = (
                    f"{route_input}\nRouting review feedback: "
                    f"{route_repair_context.get('review_feedback') or route_repair_context}"
                )
            plan = self.scheduler.skill_router.build_plan(
                route_input,
                mode=state["mode"],
                require_paperwise=state["require_paperwise"],
                capabilities=state["capabilities"],
                task_id=task_id,
            )
        except Exception as exc:
            self.scheduler.blackboard.update_node(task_id, node_id, "failed", error=str(exc), agent=agent)
            raise
        self.scheduler.blackboard.update_node(task_id, node_id, "done", agent=agent, output_summary=str(plan)[:500])
        self.scheduler._event(task_id, node_id, "final", "finalized", f"{node_id} done")
        return {"route_plan": plan}

    def _skill_route_review(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        node_id = "skill_router.review_agent"
        agent = "skill_route_review_agent" if state.get("mode") == "real" else node_id
        self.scheduler._set_v2_metadata(task_id, current_stage="skill_router", current_review_subagent=agent)
        self.scheduler.blackboard.update_node(task_id, node_id, "running", agent=agent)
        self.scheduler._event(task_id, node_id, "status", "pending", f"{node_id} running")
        try:
            review = self.scheduler.skill_router.review_plan(state["route_plan"])
        except Exception as exc:
            self.scheduler.blackboard.update_node(task_id, node_id, "failed", error=str(exc), agent=agent)
            raise
        self.scheduler.blackboard.update_node(task_id, node_id, "done", agent=agent, output_summary=str(review)[:500])
        self.scheduler._event(task_id, node_id, "final", "finalized", f"{node_id} done")
        return {"route_review": review}

    def _persist_skill_route(self, state: DynamicGraphState) -> dict[str, Any]:
        self.scheduler._persist_skill_route_result(
            task_id=state["task_id"],
            mode=state["mode"],
            plan=state["route_plan"],
            review=state["route_review"],
        )
        return {}

    @staticmethod
    def _review_blocks(review: dict[str, Any]) -> bool:
        return str(review.get("decision") or "").lower() in {"block", "blocked"} or str(review.get("status") or "").lower() in {"block", "blocked"}

    def _block_skill_route(self, state: DynamicGraphState) -> dict[str, Any]:
        review = dict(state.get("route_review") or {})
        findings = list(review.get("blocking_findings") or [])
        self.scheduler.audit.emit(state["task_id"], {"type": "skill_route_review_blocked", "findings": findings})
        return {
            "done": True,
            "needs_repair": True,
            "current_failure": {
                "error": {"type": "route", "reason": "skill_route_plan review blocked", "findings": findings},
                "step_id": "planning.skill_route_review",
                "resume_target": "planning",
            },
            "final_decision": "revise_current_step",
            "final_stage": "skill_route_review",
            "reason": "skill_route_review_blocked",
        }

    def _paperwise_gate(self, state: DynamicGraphState) -> dict[str, Any]:
        if state.get("mode") == "real":
            return {"precondition_error": ""}
        metadata = self.scheduler.blackboard.get(state["task_id"]).task_metadata
        error = self.scheduler._paperwise_required_but_unavailable(
            metadata.get("paper_report_path"),
            bool(state.get("require_paperwise")),
        )
        if error and self.scheduler.settings.llm_enabled:
            error = None
        return {"precondition_error": str(error or "")}

    def _block_precondition(self, state: DynamicGraphState) -> dict[str, Any]:
        reason = str(state.get("precondition_error") or "precondition_failed")
        return {
            "done": True,
            "needs_repair": True,
            "current_failure": {
                "error": f"insufficient evidence: {reason}",
                "step_id": "planning.paperwise_gate",
                "resume_target": "planning",
            },
            "final_decision": "revise_current_step",
            "final_stage": "precondition",
            "reason": reason,
        }

    def _external_llm_gate(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        if (
            state.get("operation") == "paperwise_rerun"
            or state.get("mode") == "real"
            or not self.scheduler._external_llm_enabled()
        ):
            return {}
        if self.scheduler._has_approved_external_llm(task_id):
            self.scheduler._external_llm_approved_tasks.add(task_id)
            record = self.scheduler.blackboard.get(task_id)
            blockers = [item for item in record.blockers if item.get("type") != "external_llm_approval_required"]
            self.scheduler.blackboard.update(task_id, state="running", blockers=blockers)
            return {}
        egress = self.scheduler._external_llm_egress_plan("dynamic_plan_task", state["user_input"], "dynamic_planning_and_review")
        approval = {
            "approval_id": f"external_llm:{task_id}",
            "task_id": task_id,
            "status": "waiting_approval",
            "reason": "External LLM data egress requires approval",
            "data_egress": egress,
        }
        record = self.scheduler.blackboard.get(task_id)
        approvals = dict(record.approvals)
        approvals[approval["approval_id"]] = approval
        blockers = [item for item in record.blockers if item.get("type") != "external_llm_approval_required"]
        blockers.append({"type": "external_llm_approval_required", "reason": approval["reason"], "data_egress": egress})
        self.scheduler.blackboard.update(task_id, state="waiting_approval", approvals=approvals, blockers=blockers)
        self.scheduler.audit.emit(task_id, {"type": "data_egress_requested", "data_egress": egress})
        interrupt({"stage": "external_llm_approval", "reason": approval["reason"], "approval_id": approval["approval_id"]})
        return {}

    def _plan_generate(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        metadata = dict(self.scheduler.blackboard.get(task_id).task_metadata)
        planner_input = self.scheduler._dynamic_planner_input(
            task_id=task_id,
            user_input=state["user_input"],
            mode=state["mode"],
            require_paperwise=state["require_paperwise"],
            capability_snapshot=state.get("capability_snapshot") or {},
            route_plan=metadata.get("skill_route_plan") or {},
            route_review=metadata.get("skill_route_review") or {},
            metadata=metadata,
            plan_version=int(state.get("plan_version") or 1),
            reroute_instruction=state.get("reroute_instruction"),
        )
        node_id = "central_scheduler.llm_plan_generation"
        self.scheduler._set_v2_metadata(task_id, current_stage="central_scheduler", current_task_subagent=node_id)
        self.scheduler.blackboard.update_node(task_id, node_id, "running", agent=node_id)
        self.scheduler._event(task_id, node_id, "status", "pending", f"{node_id} running")
        try:
            if state.get("operation") == "paperwise_rerun":
                candidate = {
                    "source": "scheduler_fallback",
                    "rerun_kind": "paperwise_evidence_rerun",
                    "steps": [
                        {
                            "step_goal": "Refresh traceable PaperWise evidence and review relevance",
                            "action": "evidence_retrieval",
                            "inputs": ["user_goal", "paperwise_read_only_sources"],
                            "outputs": ["evidence_pool"],
                            "required_skills": ["paperwise"],
                            "gate_condition": "PaperWise remains read-only",
                        }
                    ]
                }
            else:
                candidate = self.scheduler._generate_dynamic_plan_candidate(planner_input)
        except Exception as exc:
            attempts = dict(metadata.get("planner_generation_attempts") or {})
            version_key = str(int(state.get("plan_version") or 1))
            attempt = int(attempts.get(version_key) or 0) + 1
            attempts[version_key] = attempt
            self.scheduler._set_v2_metadata(task_id, planner_generation_attempts=attempts)
            transient = retry_transient_runtime_error(exc)
            if transient and attempt < max(1, int(self.scheduler.settings.max_node_retries)):
                self.scheduler.blackboard.update_node(task_id, node_id, "failed", error=str(exc), agent=node_id)
                raise
            if transient or isinstance(exc, LLMGenerationError):
                candidate = self.scheduler._fallback_dynamic_plan_candidate(planner_input, reason=str(exc))
                self.scheduler.audit.emit(
                    task_id,
                    {
                        "type": "central_plan_fallback_selected",
                        "attempts": attempt,
                        "reason": str(exc),
                        "plan_version": int(state.get("plan_version") or 1),
                    },
                )
            else:
                candidate = self.scheduler._failed_dynamic_plan(
                    task_id,
                    str(state.get("mode") or "mock"),
                    int(state.get("plan_version") or 1),
                    state["user_input"],
                    str(exc),
                )
                self.scheduler.audit.emit(
                    task_id,
                    {
                        "type": "central_plan_generation_failed",
                        "attempts": attempt,
                        "reason": str(exc),
                        "plan_version": int(state.get("plan_version") or 1),
                    },
                )
        self.scheduler.blackboard.update_node(task_id, node_id, "done", agent=node_id, output_summary=str(candidate)[:500])
        self.scheduler._event(task_id, node_id, "final", "finalized", f"{node_id} done")
        return {"planner_input": planner_input, "plan_candidate": candidate}

    def _plan_normalize(self, state: DynamicGraphState) -> dict[str, Any]:
        plan = self.scheduler._normalize_dynamic_plan_candidate(
            state["plan_candidate"],
            task_id=state["task_id"],
            mode=state["mode"],
            plan_version=int(state.get("plan_version") or 1),
            user_input=state["user_input"],
        )
        return {"active_plan": plan}

    def _plan_validate(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        node_id = "central_scheduler.plan_validation"
        self.scheduler._set_v2_metadata(task_id, current_stage="central_scheduler", current_review_subagent=node_id)
        self.scheduler.blackboard.update_node(task_id, node_id, "running", agent=node_id)
        self.scheduler._event(task_id, node_id, "status", "pending", f"{node_id} running")
        try:
            review = self.scheduler._validate_dynamic_plan(state["active_plan"])
        except Exception as exc:
            self.scheduler.blackboard.update_node(task_id, node_id, "failed", error=str(exc), agent=node_id)
            raise
        self.scheduler.blackboard.update_node(task_id, node_id, "done", agent=node_id, output_summary=str(review)[:500])
        self.scheduler._event(task_id, node_id, "final", "finalized", f"{node_id} done")
        return {"active_review": review}

    def _persist_plan(self, state: DynamicGraphState) -> dict[str, Any]:
        result = self.scheduler._persist_dynamic_plan_result(
            task_id=state["task_id"],
            mode=state["mode"],
            plan=state["active_plan"],
            review=state["active_review"],
            plan_version=int(state.get("plan_version") or 1),
            reroute_instruction=state.get("reroute_instruction"),
            generation_error="",
            plan_metadata_key=str(state.get("plan_metadata_key") or "dynamic_plan"),
        )
        return {
            "active_plan": result["active_plan"],
            "active_review": result["active_review"],
            "plan_history": result["history"],
        }

    def _after_plan(self, state: DynamicGraphState) -> str:
        if self._review_blocks(state.get("active_review") or {}):
            return "block"
        return "execute" if state.get("execute_after_plan", True) else "finish"

    def _block_plan(self, state: DynamicGraphState) -> dict[str, Any]:
        review = dict(state.get("active_review") or {})
        findings = list(review.get("blocking_findings") or [])
        return {
            "done": True,
            "needs_repair": True,
            "current_failure": {
                "error": {"type": "plan", "reason": "dynamic_plan validation failed", "findings": findings},
                "step_id": "planning.plan_validate",
                "resume_target": "planning",
            },
            "final_decision": "revise_current_step",
            "final_stage": "plan_validation",
            "reason": "dynamic_plan_review_failed",
        }
