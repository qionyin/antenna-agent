from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict


class PlanningGraphInput(TypedDict, total=False):
    operation: str
    task_id: str
    user_input: str
    mode: str
    require_paperwise: bool
    capability_snapshot: dict[str, Any]
    capabilities: dict[str, Any]
    plan_version: int
    reroute_instruction: dict[str, Any] | None
    plan_metadata_key: str
    execute_after_plan: bool
    done: bool
    needs_repair: bool
    replay_ready: bool
    repair_attempts: int


class ExecutionGraphInput(TypedDict, total=False):
    task_id: str
    user_input: str
    mode: str
    require_paperwise: bool
    capabilities: dict[str, Any]
    plan: dict[str, Any]
    plan_metadata_key: str
    plan_steps: list[dict[str, Any]]
    step_index: int
    executed_steps: list[dict[str, Any]]
    global_review_records: list[dict[str, Any]]
    module_review_records: list[dict[str, Any]]
    subagent_records: list[dict[str, Any]]
    central_decisions: list[dict[str, Any]]
    step_skill_contexts: list[dict[str, Any]]
    plan_version: int
    done: bool
    needs_repair: bool
    replay_ready: bool


class RepairGraphInput(TypedDict, total=False):
    task_id: str
    user_input: str
    mode: str
    require_paperwise: bool
    capabilities: dict[str, Any]
    plan: dict[str, Any]
    plan_metadata_key: str
    plan_steps: list[dict[str, Any]]
    step_index: int
    current_step: dict[str, Any]
    executed_steps: list[dict[str, Any]]
    global_review_records: list[dict[str, Any]]
    module_review_records: list[dict[str, Any]]
    subagent_records: list[dict[str, Any]]
    central_decisions: list[dict[str, Any]]
    step_skill_contexts: list[dict[str, Any]]
    plan_version: int
    current_failure: dict[str, Any]
    failure_record: dict[str, Any]
    repair_decision: dict[str, Any]
    repair_plan: dict[str, Any]
    repair_resume_target: str
    final_stage: str
    reason: str
    done: bool
    needs_repair: bool
    replay_ready: bool
    repair_attempts: int


@dataclass(frozen=True)
class FieldRule:
    path: str
    alias: str | None = None
    required: bool = False
    default: Any = None


IO_CONTRACTS: dict[str, list[FieldRule]] = {
    "agent.task": [
        FieldRule("task_id", required=True),
        FieldRule("current_step.step_id", alias="step_id", required=True),
        FieldRule("current_step.step_goal", alias="goal"),
        FieldRule("current_step.inputs", alias="inputs", default=[]),
        FieldRule("current_step.outputs", alias="outputs", default=[]),
        FieldRule("current_step.required_skills", alias="required_skills", default=[]),
        FieldRule("step_skill_context.callable_skills", alias="allowed_skills", default=[]),
        FieldRule("step_skill_context.candidate_skills", alias="candidate_skills", default=[]),
        FieldRule("step_skill_context.excluded_skills", alias="excluded_skills", default=[]),
        FieldRule("executed_steps", alias="recent_history", default=[]),
    ],
    "agent.module_review": [
        FieldRule("task_id", required=True),
        FieldRule("current_step.step_id", alias="step_id", required=True),
        FieldRule("current_step.step_goal", alias="goal"),
        FieldRule("current_step.outputs", alias="expected_outputs", default=[]),
        FieldRule("task_result.output", alias="task_output", default={}),
        FieldRule("real_cst_approved", default=False),
        FieldRule("step_skill_context", default={}),
    ],
    "agent.global_review": [
        FieldRule("task_id", required=True),
        FieldRule("current_step.step_id", alias="step_id", required=True),
        FieldRule("current_step.step_goal", alias="goal"),
        FieldRule("task_result.output", alias="task_output", default={}),
        FieldRule("module_result.output", alias="module_review", default={}),
        FieldRule("executed_steps", alias="recent_history", default=[]),
        FieldRule("step_skill_context", default={}),
    ],
    "agent.repair_task": [
        FieldRule("task_id", required=True),
        FieldRule("repair_plan", required=True),
        FieldRule("failure_record", default={}),
    ],
    "agent.repair_review": [
        FieldRule("task_id", required=True),
        FieldRule("repair_plan", required=True),
        FieldRule("repair_task_result.output", alias="repair_output", default={}),
    ],
}


class IOContractError(ValueError):
    pass


class IOContractResolver:
    """Build small handoff payloads from graph state using explicit contracts."""

    def build_input(
        self,
        contract_id: str,
        source: dict[str, Any],
        *,
        overlays: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if contract_id not in IO_CONTRACTS:
            raise IOContractError(f"unknown IO contract: {contract_id}")
        merged = {**source, **(overlays or {})}
        payload: dict[str, Any] = {"contract_id": contract_id}
        missing: list[str] = []
        for rule in IO_CONTRACTS[contract_id]:
            found, value = self._lookup(merged, rule.path)
            if not found:
                if rule.required:
                    missing.append(rule.path)
                    continue
                value = rule.default
            payload[rule.alias or rule.path.split(".")[-1]] = value
        if missing:
            raise IOContractError(f"{contract_id} missing required fields: {', '.join(missing)}")
        return payload

    @staticmethod
    def _lookup(source: dict[str, Any], path: str) -> tuple[bool, Any]:
        current: Any = source
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return False, None
        return True, current
