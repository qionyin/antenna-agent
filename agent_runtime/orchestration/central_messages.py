from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..utils import now_iso, stable_hash


class CentralMessageServiceMixin:
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
