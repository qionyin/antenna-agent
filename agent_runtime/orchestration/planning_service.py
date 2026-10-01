from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..runtime_llm_config import runtime_llm_config
from ..utils import atomic_write_json, now_iso, stable_hash


class PlanningServiceMixin:
    """Dynamic-plan generation, normalization, validation, and persistence."""

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
            "validated_learning_context": metadata.get("validated_learning_context") or {
                "policy": "promoted_only",
                "knowledge": [],
                "experiences": [],
            },
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
                    "Use only validated_learning_context records; never treat candidate or contradicted memory as established knowledge.",
                    "When a learning record influences a step, preserve its knowledge_id or experience_id in the step evidence_refs.",
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
