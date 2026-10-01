from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.types import interrupt

from ...utils import now_iso
from ..retry import retry_transient_runtime_error
from ..state import DynamicGraphState


class ExecutionGraphNodesMixin:
    def _prepare_execution(self, state: DynamicGraphState) -> dict[str, Any]:
        plan = dict(state.get("active_plan") or {})
        metadata = self.scheduler.blackboard.get(state["task_id"]).task_metadata
        plan_steps = [dict(step) for step in plan.get("steps") or []]
        return {
            "plan": plan,
            "plan_metadata_key": str(state.get("plan_metadata_key") or "dynamic_plan"),
            "plan_steps": plan_steps,
            "step_index": 0,
            "executed_steps": [dict(step) for step in plan_steps if step.get("status") == "passed"],
            "global_review_records": list(metadata.get("global_review_records") or []),
            "module_review_records": list(metadata.get("module_review_records") or []),
            "subagent_records": list(metadata.get("subagent_records") or []),
            "central_decisions": list(metadata.get("central_decisions") or []),
            "step_skill_contexts": list(metadata.get("step_skill_contexts") or []),
            "plan_version": int(plan.get("plan_version") or state.get("plan_version") or 1),
            "done": False,
            "needs_repair": False,
            "replay_ready": False,
        }

    def _select_step(self, state: DynamicGraphState) -> dict[str, Any]:
        steps = list(state.get("plan_steps") or [])
        index = int(state.get("step_index") or 0)
        while index < len(steps) and steps[index].get("status") == "passed":
            index += 1
        if index >= len(steps):
            return {
                "step_index": index,
                "done": True,
                "final_decision": "complete_task",
                "final_stage": "completed",
                "reason": "all dynamic steps passed",
            }
        return {
            "step_index": index,
            "current_step": dict(steps[index]),
            "step_route_plan": {},
            "step_skill_context": {},
            "task_result": {},
            "local_module_review": None,
            "module_result": {},
            "global_result": None,
            "central_decision": "",
            "done": False,
            "needs_repair": False,
            "replay_ready": False,
        }

    def _route_skill(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = dict(state["current_step"])
        module_records = list(state.get("module_review_records") or [])
        global_records = list(state.get("global_review_records") or [])
        context = self.scheduler.skill_router.build_step_context(
            task_id=task_id,
            user_input=state["user_input"],
            step=step,
            mode=state["mode"],
            plan_version=state["plan_version"],
            require_paperwise=state["require_paperwise"],
            capabilities=state["capabilities"],
            context=self.scheduler._step_route_context(task_id, step, module_records, global_records),
        )
        route_plan = context.get("route_plan") or {}
        route_history = list(self.scheduler.blackboard.get(task_id).task_metadata.get("skill_route_plan_history") or [])
        route_history.append(
            {
                "plan": route_plan,
                "review": {"decision": "pass", "review_agent": "central_agent", "reason": "step_route_context_generated"},
                "reason": context.get("route_reason") or "step_route",
                "step_id": step.get("step_id"),
                "dynamic_plan_version": state["plan_version"],
                "created_at": now_iso(),
            }
        )
        step["step_skill_context"] = context
        step["callable_skills"] = context.get("callable_skills", [])
        step["candidate_skills"] = context.get("candidate_skills", [])
        step["blocked_skills"] = context.get("excluded_skills", [])
        contexts = [*list(state.get("step_skill_contexts") or []), context]
        self.scheduler._set_v2_metadata(
            task_id,
            current_stage=step.get("step_id"),
            current_task_subagent=step.get("task_agent"),
            current_review_subagent=step.get("module_review_agent") or step.get("review_agent"),
            step_skill_contexts=contexts,
            active_skill_route_plan=route_plan,
            skill_route_plan_history=route_history,
        )
        return {
            "current_step": step,
            "step_route_plan": route_plan,
            "step_skill_context": context,
            "step_skill_contexts": contexts,
        }

    def _task_agent(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = dict(state["current_step"])
        execution_context: dict[str, Any] = {}

        def execute_step() -> dict[str, Any]:
            output = self.scheduler._execute_dynamic_step_handler(
                task_id=task_id,
                step=step,
                route_plan=state["step_route_plan"],
                plan_version=state["plan_version"],
                user_input=state["user_input"],
            )
            execution_context["module_review"] = output.pop("_module_review", None)
            return output

        task_agent = str(step.get("task_agent") or "dynamic_step_task_agent")
        task_node = f"{step.get('step_id')}.{task_agent}"
        task_context = self.io.build_input(
            "agent.task",
            dict(state),
            overlays={"current_step": step},
        )
        self.scheduler.blackboard.update_node(task_id, task_node, "running", agent=task_agent)
        try:
            result = self.scheduler.subagents.run_task(
                task_id=task_id,
                step=step,
                context=task_context,
                allow_external_llm=self.scheduler._step_external_llm_allowed(task_id, step),
                executor=execute_step,
            )
        except Exception as exc:
            self.scheduler.blackboard.update_node(task_id, task_node, "failed", agent=task_agent, error=str(exc))
            if retry_transient_runtime_error(exc):
                raise
            result = self.scheduler.subagents.run_task(
                task_id=task_id,
                step=step,
                context=task_context,
                allow_external_llm=False,
                local_output={
                    "status": "failed",
                    "execution_failure": {
                        "error_type": exc.__class__.__name__,
                        "reason": str(exc),
                    },
                    "missing_required_input": f"{exc.__class__.__name__}: {exc}",
                    "evidence_refs": [],
                },
            )
        self.scheduler.blackboard.update_node(task_id, task_node, "done", agent=task_agent, output_summary=str(result)[:500])
        self.scheduler.audit.emit(task_id, {"type": "dynamic_step_task_result", "agent": task_agent, "step_id": step.get("step_id"), "result": result})
        return {
            "task_result": result,
            "local_module_review": execution_context.get("module_review"),
            "subagent_records": [*list(state.get("subagent_records") or []), result],
        }

    def _module_review(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = state["current_step"]
        agent = str(step.get("module_review_agent") or step.get("review_agent") or "module_review_agent")
        node = f"{step.get('step_id')}.{agent}"
        module_context = self.io.build_input(
            "agent.module_review",
            dict(state),
            overlays={
                "current_step": step,
                "real_cst_approved": self.scheduler._real_cst_approval_valid(task_id),
            },
        )
        self.scheduler.blackboard.update_node(task_id, node, "running", agent=agent)
        result = self.scheduler.subagents.run_module_review(
            task_id=task_id,
            step=step,
            task_result=state["task_result"],
            context=module_context,
            allow_external_llm=self.scheduler._step_external_llm_allowed(task_id, step),
            local_review=state.get("local_module_review"),
        )
        status = "done" if result.get("decision") == "pass" else result.get("decision", "reviewed")
        self.scheduler.blackboard.update_node(task_id, node, status, agent=agent, output_summary=str(result)[:500])
        self.scheduler.audit.emit(task_id, {"type": "dynamic_step_module_review_result", "agent": agent, "step_id": step.get("step_id"), "result": result})
        return {
            "module_result": result,
            "module_review_records": [*list(state.get("module_review_records") or []), result],
            "subagent_records": [*list(state.get("subagent_records") or []), result],
        }

    def _global_review(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = state["current_step"]
        node = f"{step.get('step_id')}.global_review_agent"
        global_context = self.io.build_input(
            "agent.global_review",
            dict(state),
            overlays={"current_step": step},
        )
        self.scheduler._set_v2_metadata(task_id, current_review_subagent="global_review_agent")
        self.scheduler.blackboard.update_node(task_id, node, "running", agent="global_review_agent")
        result = self.scheduler.subagents.run_global_review(
            task_id=task_id,
            step=step,
            task_result=state["task_result"],
            module_review=state["module_result"].get("output") or state["module_result"],
            history=state.get("executed_steps") or [],
            context=global_context,
            allow_external_llm=self.scheduler._step_external_llm_allowed(task_id, step),
        )
        status = "done" if result.get("decision") == "pass" else result.get("decision", "reviewed")
        self.scheduler.blackboard.update_node(task_id, node, status, agent="global_review_agent", output_summary=str(result)[:500])
        self.scheduler.audit.emit(task_id, {"type": "dynamic_step_global_review_result", "agent": "global_review_agent", "step_id": step.get("step_id"), "result": result})
        return {
            "global_result": result,
            "global_review_records": [*list(state.get("global_review_records") or []), result],
            "subagent_records": [*list(state.get("subagent_records") or []), result],
        }

    def _central_decision(self, state: DynamicGraphState) -> dict[str, Any]:
        decision = self.scheduler._central_step_decision(state["module_result"], state.get("global_result"))
        global_result = state.get("global_result") or {}
        record = {
            "schema_version": "1.0",
            "step_id": state["current_step"].get("step_id"),
            "decision": decision,
            "module_review_decision": state["module_result"].get("decision"),
            "global_review_decision": global_result.get("decision") or None,
            "plan_version": state["plan_version"],
            "skill_route_plan_id": state["step_route_plan"].get("plan_id"),
            "step_skill_context_id": state["step_skill_context"].get("route_plan_id"),
            "created_at": now_iso(),
        }
        self.scheduler.audit.emit(state["task_id"], {"type": "central_dynamic_step_decision", **record})
        return {
            "central_decision": decision,
            "central_decisions": [*list(state.get("central_decisions") or []), record],
        }

    def _commit_step(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = dict(state["current_step"])
        global_result = state.get("global_result") or {}
        global_output = global_result.get("output") or {}
        if global_output.get("reflection"):
            reflections = list(self.scheduler.blackboard.get(task_id).task_metadata.get("reflection_records") or [])
            reflections.append(global_output["reflection"])
            self.scheduler._set_v2_metadata(task_id, reflection_records=reflections)

        step["task_subagent_result_id"] = state["task_result"].get("result_id")
        step["module_review_result_id"] = state["module_result"].get("result_id")
        if global_result:
            step["global_review_result_id"] = global_result.get("result_id")
        decision = state["central_decision"]
        terminal_failure = False
        terminal_reason = ""
        if decision == "wait_user":
            paused_step = dict(step)
            paused_step["central_decision"] = "wait_user"
            paused_step["status"] = "wait_user"
            paused_steps = list(state["plan_steps"])
            paused_steps[int(state["step_index"])] = paused_step
            paused_plan = dict(state["plan"])
            paused_plan["steps"] = paused_steps
            self.scheduler._set_v2_metadata(
                task_id,
                **{state["plan_metadata_key"]: paused_plan},
                current_stage=str(step.get("step_id") or "waiting_approval"),
            )
            resumed = interrupt(
                {
                    "stage": str(step.get("step_id") or "waiting_approval"),
                    "reason": "central_decision:wait_user",
                    "step_id": step.get("step_id"),
                }
            )
            resume_payload = dict(resumed) if isinstance(resumed, dict) else {"approved": bool(resumed)}
            approved = bool(resume_payload.get("approved"))
            review_agent = str(step.get("module_review_agent") or step.get("review_agent") or "module_review_agent")
            approval_valid = approved and (state.get("mode") != "real" or self.scheduler._real_cst_approval_valid(task_id))
            self.scheduler.blackboard.update_node(
                task_id,
                f"{step.get('step_id')}.{review_agent}",
                "done" if approval_valid else "block",
                agent=review_agent,
                output_summary="approval resumed from LangGraph checkpoint" if approval_valid else "approval rejected or stale",
            )
            decision = "pass_next_step" if approval_valid else "block_task"
            if not approved:
                terminal_failure = True
                terminal_reason = str(resume_payload.get("reason") or "user_rejected_approval")
        step["central_decision"] = decision
        step["status"] = "passed" if decision == "pass_next_step" else decision

        steps = list(state["plan_steps"])
        index = int(state["step_index"])
        steps[index] = step
        executed = list(state.get("executed_steps") or [])
        if decision == "pass_next_step":
            executed.append(step)
        plan = dict(state["plan"])
        plan["steps"] = steps

        updates: dict[str, Any] = {
            "current_step": step,
            "plan_steps": steps,
            "plan": plan,
            "executed_steps": executed,
        }
        if decision in {"refresh_skill_route", "block_task", "reroute_plan", "revise_current_step"}:
            reason = terminal_reason or self._step_failure_reason(step, decision)
            if terminal_failure:
                updates.update(
                    {
                        "done": True,
                        "needs_repair": False,
                        "terminal_failure": True,
                        "replay_ready": False,
                        "final_decision": "block_task",
                        "final_stage": str(step.get("step_id") or "approval_rejected"),
                        "reason": reason,
                    }
                )
                self.scheduler._set_v2_metadata(task_id, failure_reason=reason, cst_status="not_reached")
            else:
                task_output = dict(state.get("task_result", {}).get("output") or {})
                module_output = dict(state.get("module_result", {}).get("output") or {})
                if decision == "refresh_skill_route":
                    failure_error: Any = "missing_skill in skill_route_plan; refresh route required"
                elif decision == "reroute_plan":
                    failure_error = "invalid dynamic_plan; plan regeneration required"
                else:
                    failure_error = (
                        task_output.get("repairable_failure")
                        or task_output.get("failure")
                        or task_output.get("execution_failure")
                        or task_output.get("missing_required_input")
                        or module_output.get("blocking_findings")
                        or module_output.get("required_fixes")
                        or reason
                    )
                updates.update(
                    {
                        "done": True,
                        "needs_repair": True,
                        "terminal_failure": False,
                        "replay_ready": False,
                        "current_failure": {
                            "error": failure_error,
                            "step_id": str(step.get("step_id") or "execution.unknown"),
                            "resume_target": "execution",
                            "central_decision": decision,
                        },
                        "final_decision": "revise_current_step",
                        "final_stage": str(step.get("step_id") or ""),
                        "reason": reason,
                    }
                )
        else:
            updates.update({"step_index": index + 1, "done": False, "needs_repair": False})

        self.scheduler._set_v2_metadata(
            task_id,
            **{state["plan_metadata_key"]: plan},
            global_review_records=state.get("global_review_records") or [],
            module_review_records=state.get("module_review_records") or [],
            subagent_records=state.get("subagent_records") or [],
            central_decisions=state.get("central_decisions") or [],
            step_skill_contexts=updates.get("step_skill_contexts", state.get("step_skill_contexts") or []),
        )
        return updates

    @staticmethod
    def _step_failure_reason(step: dict[str, Any], decision: str) -> str:
        return {
            "step_000_evidence": "paperwise_evidence_review_failed",
            "step_001_preflight": "preflight_failed",
            "step_003_cst_run": "cst_run_review_failed",
            "step_004_parse": "result_parse_review_failed",
            "step_005_report": "report_review_failed",
        }.get(str(step.get("step_id") or ""), f"central_decision:{decision}")

    def _after_execution_commit(self, state: DynamicGraphState) -> str:
        if state.get("needs_repair"):
            return "finish"
        if not state.get("done"):
            return "select_step"
        return "finish"
