from __future__ import annotations

from typing import Any, TypedDict

class DynamicGraphState(TypedDict, total=False):
    operation: str
    task_id: str
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
    capabilities: dict[str, Any]
    user_input: str
    require_paperwise: bool
    mode: str
    plan_version: int
    current_step: dict[str, Any]
    step_route_plan: dict[str, Any]
    step_skill_context: dict[str, Any]
    task_result: dict[str, Any]
    local_module_review: dict[str, Any] | None
    module_result: dict[str, Any]
    global_result: dict[str, Any] | None
    central_decision: str
    final_decision: str
    final_stage: str
    reason: str
    done: bool
    capability_snapshot: dict[str, Any]
    reroute_instruction: dict[str, Any] | None
    route_plan: dict[str, Any]
    route_review: dict[str, Any]
    planner_input: dict[str, Any]
    plan_candidate: dict[str, Any]
    active_plan: dict[str, Any]
    active_review: dict[str, Any]
    plan_history: list[dict[str, Any]]
    execute_after_plan: bool
    precondition_error: str
    needs_repair: bool
    current_failure: dict[str, Any]
    failure_record: dict[str, Any]
    repair_decision: dict[str, Any]
    repair_plan: dict[str, Any]
    repair_task_result: dict[str, Any]
    repair_review_result: dict[str, Any]
    repair_global_review_result: dict[str, Any]
    repair_resume_target: str
    replay_ready: bool
    repair_attempts: int
    terminal_failure: bool
