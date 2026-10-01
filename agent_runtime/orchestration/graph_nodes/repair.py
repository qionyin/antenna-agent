from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.types import Command

from ...utils import now_iso, stable_hash
from ..state import DynamicGraphState


class RepairGraphNodesMixin:
    def _failure_triage(self, state: DynamicGraphState) -> dict[str, Any]:
        failure = dict(state.get("current_failure") or {})
        task_id = state["task_id"]
        step_id = str(failure.get("step_id") or state.get("final_stage") or "unknown")
        error = failure.get("error") or state.get("reason") or "unknown graph failure"
        record = self.scheduler.failure_store.create(
            task_id=task_id,
            step_id=step_id,
            error=error,
            context={
                "central_decision": failure.get("central_decision"),
                "resume_target": failure.get("resume_target"),
            },
        )
        if record.get("status") == "repaired":
            record = self.scheduler.failure_store.reopen_after_replay_failure(
                record["failure_id"], reason=str(error)
            )
        decision = self.scheduler.failure_store.policy.decide(
            self.scheduler.failure_store.classifier.classify(error, step_id=step_id),
            repair_rounds=int(record.get("repair_rounds") or 0),
        )
        if record.get("status") == "exhausted" or decision.exhausted:
            decision = self.scheduler.failure_store.policy.decide(
                record["category"], repair_rounds=self.scheduler.failure_store.max_repair_rounds
            )
        self.scheduler._set_v2_metadata(
            task_id,
            current_stage="failure_triage",
            active_failure=record,
            repair_decision=decision.to_dict(),
        )
        self.scheduler.audit.emit(
            task_id,
            {
                "type": "failure_triaged",
                "failure_id": record["failure_id"],
                "category": record["category"],
                "repairability": decision.repairability,
                "action": decision.action,
            },
        )
        return {
            "failure_record": record,
            "repair_decision": decision.to_dict(),
            "repair_resume_target": str(failure.get("resume_target") or "execution"),
            "replay_ready": False,
        }

    @staticmethod
    def _after_failure_triage(state: DynamicGraphState) -> str:
        decision = state.get("repair_decision") or {}
        return "repair" if decision.get("repairability") == "automatic" else "terminal"

    def _repair_plan(self, state: DynamicGraphState) -> dict[str, Any]:
        failure = dict(state["failure_record"])
        decision = dict(state["repair_decision"])
        task_id = state["task_id"]
        try:
            started = self.scheduler.failure_store.begin_repair(
                failure["failure_id"], action=str(decision["action"])
            )
        except Exception as exc:
            blocked = self.scheduler.failure_store.get(failure["failure_id"])
            return {
                "failure_record": blocked,
                "repair_plan": {},
                "repair_task_result": {"changed": False, "reason": str(exc)},
                "repair_review_result": {"decision": "block", "reason": str(exc)},
                "replay_ready": False,
            }
        repair_plan = {
            "schema_version": "1.0",
            "repair_id": f"{failure['failure_id']}:round-{started['repair_rounds']}",
            "failure_id": failure["failure_id"],
            "task_id": task_id,
            "failed_step_id": failure["step_id"],
            "category": failure["category"],
            "action": decision["action"],
            "round": started["repair_rounds"],
            "max_rounds": started["max_repair_rounds"],
            "required_change": "route, plan, input, or artifact fingerprint must change before replay",
            "task_agent": "repair_task_agent",
            "module_review_agent": "repair_review_agent",
            "global_review_required": failure["category"] in {"route", "plan", "evidence_gap", "modeling_parameter"},
            "created_at": now_iso(),
        }
        self.scheduler._set_v2_metadata(
            task_id,
            current_stage="repair_plan",
            current_task_subagent="repair_plan_task_agent",
            active_failure=started,
            active_repair_plan=repair_plan,
        )
        return {
            "failure_record": started,
            "repair_plan": repair_plan,
            "repair_attempts": int(state.get("repair_attempts") or 0) + 1,
        }

    def _repair_task(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        plan = dict(state.get("repair_plan") or {})
        if not plan:
            return {"repair_task_result": {"changed": False, "reason": "repair plan unavailable"}}
        repair_step = {
            "step_id": f"repair_{plan['failed_step_id']}_{plan['round']}",
            "step_goal": f"Apply {plan['action']} and produce verifiable change references",
            "task_agent": "repair_task_agent",
            "module_review_agent": "repair_review_agent",
            "inputs": [plan["failure_id"]],
            "outputs": ["repair_change_manifest"],
            "required_skills": [],
        }
        node = f"{repair_step['step_id']}.repair_task_agent"
        self.scheduler._set_v2_metadata(task_id, current_stage="repair_task", current_task_subagent="repair_task_agent")
        self.scheduler.blackboard.update_node(task_id, node, "running", agent="repair_task_agent")

        def execute_repair() -> dict[str, Any]:
            return self.scheduler._execute_repair_action(
                task_id=task_id,
                action=str(plan["action"]),
                failure=dict(state["failure_record"]),
                failed_step=dict(state.get("current_step") or {}),
                plan_version=int(state.get("plan_version") or 1),
            )

        repair_context = self.io.build_input("agent.repair_task", dict(state))
        result = self.scheduler.subagents.run_task(
            task_id=task_id,
            step=repair_step,
            context=repair_context,
            allow_external_llm=False,
            executor=execute_repair,
        )
        self.scheduler.blackboard.update_node(task_id, node, "done", agent="repair_task_agent", output_summary=str(result)[:500])
        records = [*list(state.get("subagent_records") or []), result]
        self.scheduler.audit.emit(task_id, {"type": "repair_task_result", "repair_plan": plan, "result": result})
        return {"repair_task_result": result, "subagent_records": records}

    @staticmethod
    def _validate_repair_change(output: dict[str, Any]) -> tuple[bool, str]:
        refs = [Path(str(item)) for item in output.get("change_refs") or [] if str(item).strip()]
        manifest_path = Path(str(output.get("repair_manifest") or ""))
        if not output.get("changed") or not refs or not str(output.get("repair_manifest") or ""):
            return False, "repair did not declare a changed manifest and change references"
        if any(not path.exists() for path in refs) or not manifest_path.is_file():
            return False, "repair references must exist before replay"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"repair manifest is unreadable: {exc}"
        before_hash = str(output.get("before_hash") or "")
        after_hash = str(output.get("after_hash") or "")
        if (
            manifest.get("schema_version") != "1.0"
            or not manifest.get("changed")
            or not before_hash
            or not after_hash
            or before_hash == after_hash
            or manifest.get("before_hash") != before_hash
            or manifest.get("after_hash") != after_hash
        ):
            return False, "repair manifest does not prove a before/after state change"
        return True, "verified repair manifest and persisted change references"

    def _repair_review(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        plan = dict(state.get("repair_plan") or {})
        task_result = dict(state.get("repair_task_result") or {})
        output = dict(task_result.get("output") or {})
        changed, validation_reason = self._validate_repair_change(output)
        local_review = {
            "schema_version": "1.0",
            "decision": "pass" if changed else "revise",
            "status": "pass" if changed else "revise",
            "evidence_level": "B" if changed else "D",
            "blocking_findings": [] if changed else [{"type": "repair_no_change", "reason": validation_reason}],
            "required_fixes": [] if changed else ["produce a changed route/plan/input/artifact fingerprint"],
            "reflection": {
                "status": "no_issue" if changed else "blocked",
                "error_type": "none" if changed else "repair_no_change",
                "confidence": 0.99,
            },
        }
        repair_step = {
            "step_id": f"repair_{plan.get('failed_step_id')}_{plan.get('round')}",
            "step_goal": "Verify the repair changed executable state before replay",
            "module_review_agent": "repair_review_agent",
            "inputs": [plan.get("failure_id")],
            "outputs": ["repair_review"],
        }
        repair_review_context = self.io.build_input("agent.repair_review", dict(state))
        review = self.scheduler.subagents.run_module_review(
            task_id=task_id,
            step=repair_step,
            task_result=task_result,
            context=repair_review_context,
            allow_external_llm=False,
            local_review=local_review,
        )
        if review.get("decision") == "pass":
            stored = self.scheduler.failure_store.complete_repair(
                plan["failure_id"],
                changed=True,
                change_refs=[str(item) for item in output.get("change_refs") or []],
                review=review,
            )
        else:
            stored = self.scheduler.failure_store.fail_repair(
                plan["failure_id"], reason=str(local_review["blocking_findings"])
            )
        records = [*list(state.get("subagent_records") or []), review]
        module_records = [*list(state.get("module_review_records") or []), review]
        self.scheduler._set_v2_metadata(
            task_id,
            current_stage="repair_review",
            current_review_subagent="repair_review_agent",
            active_failure=stored,
        )
        return {
            "failure_record": stored,
            "repair_review_result": review,
            "subagent_records": records,
            "module_review_records": module_records,
        }

    @staticmethod
    def _after_repair_review(state: DynamicGraphState) -> str:
        review = state.get("repair_review_result") or {}
        record = state.get("failure_record") or {}
        if review.get("decision") == "pass":
            return "global_review" if (state.get("repair_plan") or {}).get("global_review_required") else "replay"
        return "retry_repair" if record.get("status") == "open" else "terminal"

    def _repair_global_review(self, state: DynamicGraphState) -> dict[str, Any]:
        plan = dict(state.get("repair_plan") or {})
        step = {
            "step_id": f"repair_global_{plan.get('failed_step_id')}_{plan.get('round')}",
            "step_goal": "Check repaired input against the task evidence and downstream dependencies",
            "required_skills": [],
            "callable_skills": [],
            "candidate_skills": [],
        }
        repair_global_context = self.io.build_input(
            "agent.global_review",
            dict(state),
            overlays={
                "current_step": step,
                "task_result": state.get("repair_task_result") or {},
                "module_result": state.get("repair_review_result") or {},
            },
        )
        result = self.scheduler.subagents.run_global_review(
            task_id=state["task_id"],
            step=step,
            task_result=state.get("repair_task_result") or {},
            module_review=(state.get("repair_review_result") or {}).get("output") or state.get("repair_review_result") or {},
            history=state.get("executed_steps") or [],
            context={**repair_global_context, "repair_plan": plan},
            allow_external_llm=False,
        )
        stored = dict(state.get("failure_record") or {})
        if result.get("decision") != "pass" and stored.get("status") == "repaired":
            stored = self.scheduler.failure_store.reopen_after_replay_failure(
                stored["failure_id"], reason="global repair review rejected the change"
            )
        records = [*list(state.get("subagent_records") or []), result]
        global_records = [*list(state.get("global_review_records") or []), result]
        return {
            "repair_global_review_result": result,
            "failure_record": stored,
            "subagent_records": records,
            "global_review_records": global_records,
        }

    @staticmethod
    def _after_repair_global_review(state: DynamicGraphState) -> str:
        result = state.get("repair_global_review_result") or {}
        if result.get("decision") == "pass":
            return "replay"
        return "retry_repair" if (state.get("failure_record") or {}).get("status") == "open" else "terminal"

    def _replay_failed_step(self, state: DynamicGraphState) -> dict[str, Any]:
        target = str(state.get("repair_resume_target") or "execution")
        updates: dict[str, Any] = {
            "needs_repair": False,
            "replay_ready": True,
            "done": False,
            "final_decision": "pass_next_step",
            "reason": "repair reviewed; replay failed step only",
        }
        if target == "planning":
            new_version = int(state.get("plan_version") or 1) + 1
            updates.update(
                {
                    "operation": "replan",
                    "plan_version": new_version,
                    "execute_after_plan": True,
                    "reroute_instruction": {
                        "reason": "automatic_repair",
                        "repair_plan": state.get("repair_plan") or {},
                    },
                }
            )
        else:
            index = int(state.get("step_index") or 0)
            steps = [dict(item) for item in state.get("plan_steps") or []]
            if 0 <= index < len(steps):
                steps[index]["status"] = "pending"
                steps[index].pop("central_decision", None)
            updates.update(
                {
                    "operation": "execute",
                    "plan_steps": steps,
                    "current_step": dict(steps[index]) if 0 <= index < len(steps) else {},
                    "task_result": {},
                    "module_result": {},
                    "global_result": None,
                    "central_decision": "",
                }
            )
        self.scheduler.audit.emit(
            state["task_id"],
            {
                "type": "failed_step_replay_scheduled",
                "failure_id": (state.get("failure_record") or {}).get("failure_id"),
                "resume_target": target,
                "step_index": state.get("step_index"),
            },
        )
        return updates

    def _repair_terminal(self, state: DynamicGraphState) -> dict[str, Any]:
        record = dict(state.get("failure_record") or {})
        decision = dict(state.get("repair_decision") or {})
        wait_user = record.get("status") == "waiting_user" or decision.get("repairability") == "approval_required"
        final_decision = "wait_user" if wait_user else "block_task"
        reason = str(decision.get("reason") or record.get("status") or "repair stopped")
        if wait_user and record.get("failure_id"):
            record = self.scheduler.failure_store.mark_waiting_user(record["failure_id"], reason=reason)
        elif record.get("failure_id") and record.get("status") not in {"blocked", "exhausted"}:
            record = self.scheduler.failure_store.block(record["failure_id"], reason=reason)
        blackboard = self.scheduler.blackboard.get(state["task_id"])
        blockers = [*blackboard.blockers]
        blockers.append(
            {
                "type": "repair_waiting_user" if wait_user else "repair_blocked",
                "reason": reason,
                "failure_id": record.get("failure_id"),
                "category": record.get("category"),
            }
        )
        self.scheduler.blackboard.update(state["task_id"], blockers=blockers)
        self.scheduler._set_v2_metadata(
            state["task_id"], active_failure=record, current_stage="waiting_approval" if wait_user else "failed"
        )
        return {
            "failure_record": record,
            "needs_repair": False,
            "replay_ready": False,
            "done": True,
            "final_decision": final_decision,
            "final_stage": str(record.get("step_id") or state.get("final_stage") or "failure"),
            "reason": reason,
        }

    def _terminal_report(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = {
            "step_id": "terminal_report",
            "step_goal": "Generate a traceable failed report for the stopped task",
            "task_agent": "report_task_agent",
            "handler": "report_task_agent",
            "action": "report",
            "module_review_agent": "report_review_agent",
            "global_review_required": True,
            "inputs": ["failure_reason", "available_artifacts"],
            "outputs": ["failed_report"],
            "required_skills": [],
            "status": "running",
        }
        task_node = "terminal_report.report_task_agent"
        review_node = "terminal_report.report_review_agent"
        global_node = "terminal_report.global_review_agent"
        execution_context: dict[str, Any] = {}

        def execute_report() -> dict[str, Any]:
            output = self.scheduler._execute_dynamic_step_handler(
                task_id=task_id,
                step=step,
                route_plan={},
                plan_version=int(state.get("plan_version") or 1),
                user_input=state["user_input"],
            )
            execution_context["module_review"] = output.pop("_module_review", None)
            return output

        self.scheduler.blackboard.update_node(task_id, task_node, "running", agent="report_task_agent")
        report_task_context = self.io.build_input(
            "agent.task",
            dict(state),
            overlays={"current_step": step},
        )
        task_result = self.scheduler.subagents.run_task(
            task_id=task_id,
            step=step,
            context=report_task_context,
            allow_external_llm=False,
            executor=execute_report,
        )
        self.scheduler.blackboard.update_node(task_id, task_node, "done", agent="report_task_agent", output_summary=str(task_result)[:500])
        self.scheduler.blackboard.update_node(task_id, review_node, "running", agent="report_review_agent")
        report_module_context = self.io.build_input(
            "agent.module_review",
            dict(state),
            overlays={
                "current_step": step,
                "task_result": task_result,
                "real_cst_approved": False,
            },
        )
        module_result = self.scheduler.subagents.run_module_review(
            task_id=task_id,
            step=step,
            task_result=task_result,
            context=report_module_context,
            allow_external_llm=False,
            local_review=execution_context.get("module_review"),
        )
        self.scheduler.blackboard.update_node(task_id, review_node, "done", agent="report_review_agent", output_summary=str(module_result)[:500])
        self.scheduler.blackboard.update_node(task_id, global_node, "running", agent="global_review_agent")
        report_global_context = self.io.build_input(
            "agent.global_review",
            dict(state),
            overlays={
                "current_step": step,
                "task_result": task_result,
                "module_result": module_result,
            },
        )
        global_result = self.scheduler.subagents.run_global_review(
            task_id=task_id,
            step=step,
            task_result=task_result,
            module_review=module_result.get("output") or module_result,
            history=state.get("executed_steps") or [],
            context=report_global_context,
            allow_external_llm=False,
        )
        self.scheduler.blackboard.update_node(task_id, global_node, "done", agent="global_review_agent", output_summary=str(global_result)[:500])
        subagent_records = [*list(state.get("subagent_records") or []), task_result, module_result, global_result]
        module_records = [*list(state.get("module_review_records") or []), module_result]
        global_records = [*list(state.get("global_review_records") or []), global_result]
        self.scheduler._set_v2_metadata(
            task_id,
            subagent_records=subagent_records,
            module_review_records=module_records,
            global_review_records=global_records,
        )
        return {
            "subagent_records": subagent_records,
            "module_review_records": module_records,
            "global_review_records": global_records,
        }

    def _failure_report(self, state: DynamicGraphState) -> dict[str, Any]:
        reason = str(state.get("reason") or "task_failed")
        self.scheduler._set_v2_metadata(
            state["task_id"],
            current_stage="failure_report",
            failure_reason=reason,
        )
        report_updates = self._terminal_report(state)
        return {
            **report_updates,
            "done": True,
            "needs_repair": False,
            "terminal_failure": True,
            "final_decision": "block_task",
            "final_stage": "failure_report",
            "reason": reason,
        }

    def _finish(self, state: DynamicGraphState) -> dict[str, Any]:
        plan = dict(state.get("plan") or {})
        changes: dict[str, Any] = {
            "global_review_records": state.get("global_review_records") or [],
            "module_review_records": state.get("module_review_records") or [],
            "subagent_records": state.get("subagent_records") or [],
            "central_decisions": state.get("central_decisions") or [],
            "step_skill_contexts": state.get("step_skill_contexts") or [],
        }
        if plan:
            plan["steps"] = list(state.get("plan_steps") or [])
            changes[str(state.get("plan_metadata_key") or "dynamic_plan")] = plan
        self.scheduler._set_v2_metadata(
            state["task_id"],
            **changes,
        )
        return {}
