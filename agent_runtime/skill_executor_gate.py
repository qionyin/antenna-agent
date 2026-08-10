from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .utils import stable_hash


@dataclass(frozen=True)
class SkillGateDecision:
    allowed: bool
    reason: str
    token: dict[str, Any] | None = None
    route: dict[str, Any] | None = None


class SkillExecutorGate:
    """Authorize old adapter execution from the new skill_route_plan."""

    def check(
        self,
        task_id: str,
        packet_type: str,
        plan: dict[str, Any] | None,
        *,
        step_id: str | None = None,
        plan_version: int | None = None,
    ) -> SkillGateDecision:
        if not isinstance(plan, dict) or plan.get("plan_type") != "skill_route_plan":
            return SkillGateDecision(False, "missing_skill_route_plan")
        plan_version_value = plan_version or plan.get("plan_version", 1)
        if not step_id or plan_version is None:
            return SkillGateDecision(False, "explicit_step_and_plan_version_required")
        step_id_value = step_id
        for route in plan.get("routes") or []:
            if route.get("route_class", "callable") != "callable" or route.get("call_allowed") is False:
                continue
            binding = route.get("adapter_binding") or {}
            if not binding.get("adapter_allowed"):
                continue
            if binding.get("packet_type") != packet_type:
                continue
            token = {
                "schema_version": "1.0",
                "token_type": "skill_executor_gate",
                "task_id": task_id,
                "plan_id": plan.get("plan_id"),
                "plan_version": plan_version_value,
                "step_id": step_id_value,
                "route_id": route.get("route_id"),
                "owner_skill": route.get("owner_skill"),
                "packet_type": packet_type,
                "action": route.get("action"),
                "expires_when_plan_changes": True,
                "token_id": stable_hash(
                    {
                        "task_id": task_id,
                        "plan_id": plan.get("plan_id"),
                        "plan_version": plan_version_value,
                        "step_id": step_id_value,
                        "route_id": route.get("route_id"),
                        "owner_skill": route.get("owner_skill"),
                        "packet_type": packet_type,
                        "action": route.get("action"),
                    }
                )[:16],
            }
            return SkillGateDecision(True, "route_allowed", token, route)
        support_decision = self._check_support_packet(
            task_id,
            packet_type,
            plan,
            step_id=step_id,
            plan_version=plan_version,
        )
        if support_decision.allowed:
            return support_decision
        return SkillGateDecision(False, "packet_type_not_selected_by_route")

    def validate_token(
        self,
        task_id: str,
        packet_type: str,
        plan: dict[str, Any] | None,
        token: dict[str, Any] | None,
        *,
        step_id: str | None = None,
        plan_version: int | None = None,
    ) -> None:
        if not isinstance(token, dict):
            raise PermissionError("missing_skill_executor_gate_token")
        if token.get("task_id") != task_id:
            raise PermissionError("skill_gate_token_task_mismatch")
        if token.get("packet_type") != packet_type:
            raise PermissionError("skill_gate_token_packet_mismatch")
        expected_plan_version = plan_version or (plan or {}).get("plan_version", 1)
        if not step_id or plan_version is None:
            raise PermissionError("explicit_step_and_plan_version_required")
        expected_step_id = step_id
        if token.get("plan_version") != expected_plan_version:
            raise PermissionError("skill_gate_token_plan_version_stale")
        if token.get("step_id") != expected_step_id:
            raise PermissionError("skill_gate_token_step_mismatch")
        decision = self.check(task_id, packet_type, plan, step_id=expected_step_id, plan_version=expected_plan_version)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        route = decision.route or {}
        if token.get("owner_skill") != route.get("owner_skill"):
            raise PermissionError("skill_gate_token_owner_mismatch")
        if token.get("action") != route.get("action"):
            raise PermissionError("skill_gate_token_action_mismatch")
        if decision.token and decision.token.get("token_id") != token.get("token_id"):
            raise PermissionError("skill_gate_token_route_mismatch")

    def _check_support_packet(
        self,
        task_id: str,
        packet_type: str,
        plan: dict[str, Any],
        *,
        step_id: str | None = None,
        plan_version: int | None = None,
    ) -> SkillGateDecision:
        support_owner_by_packet = {
            "result_packet": "antenna-result-to-claim",
        }
        owner_skill = support_owner_by_packet.get(packet_type)
        if owner_skill is None:
            return SkillGateDecision(False, "not_a_support_packet")
        plan_version_value = plan_version or plan.get("plan_version", 1)
        if not step_id or plan_version is None:
            return SkillGateDecision(False, "explicit_step_and_plan_version_required")
        step_id_value = step_id
        for route in plan.get("routes") or []:
            if route.get("route_class", "callable") != "callable" or route.get("call_allowed") is False:
                continue
            if route.get("owner_skill") != owner_skill:
                continue
            action = route.get("action")
            token = {
                "schema_version": "1.0",
                "token_type": "skill_executor_gate",
                "task_id": task_id,
                "plan_id": plan.get("plan_id"),
                "plan_version": plan_version_value,
                "step_id": step_id_value,
                "route_id": f"{route.get('route_id')}:support:{packet_type}",
                "owner_skill": owner_skill,
                "packet_type": packet_type,
                "action": action,
                "expires_when_plan_changes": True,
                "token_id": stable_hash(
                    {
                        "task_id": task_id,
                        "plan_id": plan.get("plan_id"),
                        "plan_version": plan_version_value,
                        "step_id": step_id_value,
                        "route_id": route.get("route_id"),
                        "packet_type": packet_type,
                        "action": action,
                        "support_for": owner_skill,
                    }
                )[:16],
            }
            return SkillGateDecision(True, "support_packet_allowed_by_route", token, route)
        return SkillGateDecision(False, "support_packet_owner_not_selected")
