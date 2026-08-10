from __future__ import annotations

from typing import Any

from .skill_matcher import SkillMatch, SkillMatcher
from .skill_registry import SkillRegistry
from .utils import stable_hash


class SkillRouter:
    """Build schema-stable skill_route_plan objects without reading source SKILL.md."""

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self.registry = registry or SkillRegistry()
        self.matcher = SkillMatcher(self.registry)

    def build_plan(
        self,
        user_input: str,
        *,
        mode: str,
        require_paperwise: bool = False,
        capabilities: dict[str, Any] | None = None,
        task_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        matches, excluded, domain_decision = self.matcher.match(
            user_input,
            mode=mode,
            require_paperwise=require_paperwise,
            context=context,
        )
        capabilities = capabilities or {}
        callable_matches = [match for match in matches if match.confidence >= 0.70]
        candidate_matches = [match for match in matches if 0.45 <= match.confidence < 0.70]
        routes = [self._route_from_match(match, capabilities, route_class="callable") for match in callable_matches]
        candidate_skills = [self._route_from_match(match, capabilities, route_class="candidate") for match in candidate_matches]
        decision = "route_selected" if routes else "no_specialized_skill_needed"
        if domain_decision.get("decision") == "ambiguous":
            decision = "ambiguous"
        plan_seed = {
            "task_id": task_id or "",
            "user_input": user_input,
            "mode": mode,
            "routes": [(route["owner_skill"], route["stage_id"]) for route in routes],
        }
        plan_id = stable_hash(plan_seed)[:16]
        plan = {
            "schema_version": "1.0",
            "plan_type": "skill_route_plan",
            "plan_id": plan_id,
            "plan_version": 1,
            "task_id": task_id or "",
            "mode": mode,
            "disclosure_level_used": 2,
            "input_summary": {
                "user_input": str(user_input or ""),
                "context_task_id": (context or {}).get("context_task_id"),
            },
            "domain_decision": {**domain_decision, "decision": decision},
            "routes": routes,
            "callable_skills": routes,
            "candidate_skills": candidate_skills,
            "excluded_skills": excluded,
            "review": {
                "required": True,
                "review_agent": "skill_route_review_agent",
            },
            "selected_owner_skills": sorted({route["owner_skill"] for route in routes}),
            "selected_capabilities": sorted({route["capability"] for route in routes}),
            "candidate_owner_skills": sorted({route["owner_skill"] for route in candidate_skills}),
            "summary": {
                "route_count": len(routes),
                "candidate_count": len(candidate_skills),
                "required_count": sum(1 for route in routes if route["required"]),
                "high_risk_count": sum(1 for route in routes if route["risk_level"] == "high"),
                "excluded_count": len(excluded),
            },
            "decision": decision,
            "reason": "Central scheduler selected routes from project skill registry." if routes else "No specialized skill route met the threshold.",
        }
        return plan

    def build_step_context(
        self,
        *,
        task_id: str,
        user_input: str,
        step: dict[str, Any],
        mode: str,
        plan_version: int,
        require_paperwise: bool = False,
        capabilities: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Re-score skills for one dynamic step without reading source SKILL.md."""
        step_text = " ".join(
            [
                str(user_input or ""),
                str(step.get("step_goal") or ""),
                " ".join(str(item) for item in step.get("required_skills") or []),
                " ".join(str(item) for item in step.get("outputs") or []),
            ]
        )
        plan = self.build_plan(
            step_text,
            mode=mode,
            require_paperwise=require_paperwise,
            capabilities=capabilities,
            task_id=task_id,
            context=context,
        )
        route_reason = "step_route_refresh" if (context or {}).get("review_suggested_skills") else "step_route"
        return {
            "schema_version": "1.0",
            "context_type": "step_skill_context",
            "task_id": task_id,
            "plan_version": plan_version,
            "step_id": step.get("step_id"),
            "route_plan_id": plan.get("plan_id"),
            "route_plan": plan,
            "route_reason": route_reason,
            "callable_skills": plan.get("callable_skills", []),
            "candidate_skills": plan.get("candidate_skills", []),
            "excluded_skills": plan.get("excluded_skills", []),
            "confidence_policy": {
                "callable_min": 0.70,
                "candidate_min": 0.45,
                "candidate_max": 0.699,
                "meaning": "scores are valid only for this step, plan_version, and evidence context",
            },
            "score_formula": "base_domain_score + task_type_score + artifact_score + evidence_score + review_feedback_score + context_score - negative_score",
        }

    def review_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        blockers: list[str] = []
        routes = list(plan.get("routes") or [])
        if plan.get("disclosure_level_used", 99) > 2:
            blockers.append("routing_disclosure_exceeded_level_2")
        for route in routes:
            if not route.get("owner_skill") or not route.get("route_id"):
                blockers.append("route_missing_identity")
            binding = route.get("adapter_binding") or {}
            if binding.get("adapter_allowed") and not binding.get("packet_type"):
                blockers.append(f"{route.get('owner_skill')}:adapter_missing_packet_type")
        reflection_review = self._reflection_for_request(
            str((plan.get("input_summary") or {}).get("user_input") or ""),
            plan,
        )
        if blockers:
            decision = "block"
            status = "blocked"
            reflection = {
                "status": "blocked",
                "error_type": "missing_dependency",
                "why_central_failed": "skill_route_plan failed schema or adapter-binding review",
                "what_should_change": "repair route identity, disclosure, or adapter binding before execution",
                "reroute_required": False,
                "confidence": 0.83,
            }
            reroute_suggestion = {"action": "none", "reason": "", "suggested_steps": []}
        else:
            decision = reflection_review.get("decision", "pass")
            status = "pass" if decision == "pass" else decision
            reflection = reflection_review.get("reflection") or {}
            reroute_suggestion = reflection_review.get("reroute_suggestion") or {}
        return {
            "schema_version": "1.0",
            "review_agent": "skill_route_review_agent",
            "status": status,
            "decision": decision,
            "evidence_level": "D" if blockers else reflection_review.get("evidence_level", "B"),
            "blockers": blockers,
            "blocking_findings": [{"reason": item} for item in blockers],
            "required_fixes": blockers,
            "reflection": reflection,
            "reroute_suggestion": reroute_suggestion,
            "conclusion": "skill route is executable" if status == "pass" else "skill route needs central reroute or repair",
        }

    def _reflection_for_request(self, user_input: str, route_plan: dict[str, Any]) -> dict[str, Any]:
        """Review routing-domain mistakes without depending on the plan generator."""
        text = str(user_input or "").lower()
        selected = set(route_plan.get("selected_owner_skills") or [])
        if "return loss" in text and any(term in text for term in ("finance", "drawdown", "金融", "回撤")):
            return self._reflection_result(
                decision="reroute",
                evidence_level="D",
                error_type="false_positive",
                why="return loss is used in a finance context, not an antenna S11 context",
                action="rebuild_plan",
                reason="non-antenna return-loss context",
            )
        if "arbw" in text and any(term in text for term in ("three", "3", "三个", "三個")):
            return self._reflection_result(
                decision="reroute",
                evidence_level="C",
                error_type="false_negative",
                why="ARBW sample sufficiency needs result-to-claim review",
                action="add_step",
                reason="ARBW evidence sufficiency must be reviewed",
                suggested_steps=[self._suggested_step(
                    "step_arbw_claim_review",
                    "Review whether ARBW samples are sufficient for the conclusion",
                    "antenna_result_to_claim",
                    ["antenna-result-to-claim"],
                )],
            )
        if "claim_assessment" in text and any(term in text for term in ("论文证据", "paper evidence", "evidence")):
            return self._reflection_result(
                decision="reroute",
                evidence_level="C",
                error_type="missing_dependency",
                why="claim assessment needs an explicit evidence trace step",
                action="add_step",
                reason="claim support needs paper evidence check",
                suggested_steps=[self._suggested_step(
                    "step_claim_evidence_check",
                    "Check whether claim_assessment has traceable paper evidence",
                    "evidence_check",
                    ["paperwise", "antenna-research-reviewer"],
                )],
            )
        if "cst" in text and not any(term in text for term in ("geometry", "model spec", "建模", "几何")):
            if "cst-control" in selected or "e-platform-cst" in selected:
                return self._reflection_result(
                    decision="reroute",
                    evidence_level="C",
                    error_type="missing_dependency",
                    why="CST was selected without geometry/model readiness",
                    action="add_step",
                    reason="CST requires geometry/model readiness",
                    suggested_steps=[self._suggested_step(
                        "step_geometry_readiness",
                        "Prepare and review geometry or model spec before CST",
                        "geometry_evidence",
                        ["antenna-research-ideation"],
                    )],
                )
        return self._reflection_result(decision="pass", evidence_level="B")

    @staticmethod
    def _suggested_step(step_id: str, goal: str, action: str, skills: list[str]) -> dict[str, Any]:
        return {
            "step_id": step_id,
            "step_goal": goal,
            "action": action,
            "task_agent": f"{action}_task_agent",
            "module_review_agent": f"{action}_review_agent",
            "inputs": [],
            "outputs": [],
            "required_skills": skills,
            "gate_condition": "validated by Scheduler before execution",
            "status": "pending",
        }

    @staticmethod
    def _reflection_result(
        *,
        decision: str,
        evidence_level: str,
        error_type: str = "none",
        why: str = "",
        action: str = "none",
        reason: str = "",
        suggested_steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        reroute = decision == "reroute"
        return {
            "decision": decision,
            "status": "pass" if decision == "pass" else decision,
            "evidence_level": evidence_level,
            "blocking_findings": [],
            "required_fixes": [],
            "reflection": {
                "status": "needs_reroute" if reroute else "no_issue",
                "error_type": error_type,
                "why_central_failed": why,
                "what_should_change": reason,
                "reroute_required": reroute,
                "confidence": 0.92 if reroute else 0.90,
            },
            "reroute_suggestion": {
                "action": action,
                "reason": reason,
                "suggested_steps": suggested_steps or [],
            },
        }

    def _route_from_match(self, match: SkillMatch, capabilities: dict[str, Any], route_class: str = "callable") -> dict[str, Any]:
        skill = match.skill
        binding = dict(skill.adapter_binding or {})
        route_seed = {
            "skill": skill.id,
            "category": skill.category,
            "packet_type": binding.get("packet_type"),
            "action": self._action_for_skill(skill.id),
        }
        status = self._status_for_skill(skill.id, skill.capability, binding, capabilities)
        return {
            "route_id": stable_hash(route_seed)[:16],
            "stage_id": self._stage_for_skill(skill.id),
            "owner_skill": skill.id,
            "category": skill.category,
            "capability": skill.capability,
            "action": self._action_for_skill(skill.id),
            "required": skill.category.startswith("antenna") and skill.id not in {"antenna-skills", "e-platform-cst"},
            "risk_level": skill.risk_level,
            "status": status,
            "route_class": route_class,
            "call_allowed": route_class == "callable",
            "confidence": match.confidence,
            "matched_terms": list(match.matched_terms),
            "negative_matches": list(match.negative_matches),
            "reason": match.reason,
            "scores": {
                "final_score": match.final_score,
                "domain_score": match.domain_score,
                "task_type_score": match.task_type_score,
                "artifact_score": match.artifact_score,
                "evidence_score": match.evidence_score,
                "review_feedback_score": match.review_feedback_score,
                "context_score": match.context_score,
                "negative_score": match.negative_score,
            },
            "adapter_binding": binding,
        }

    def _status_for_skill(
        self,
        skill_id: str,
        capability: str,
        binding: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> str:
        if not capabilities:
            return "planned"
        aliases = {
            "paperwise": {"paperwise_vector_library_reader", "paperwise_report_reader"},
            "antenna-research-ideation": {"skill_packet_protocol", "antenna_packet_adapter:geometry_contract"},
            "antenna-research-idea-advisor": {"skill_packet_protocol", "antenna_packet_adapter:idea_card"},
            "antenna-research-reviewer": {"skill_packet_protocol", "antenna_packet_adapter:next_iteration_plan"},
            "antenna-claim-experiment-planner": {"skill_packet_protocol", "antenna_packet_adapter:experiment_contract"},
            "antenna-result-to-claim": {"skill_packet_protocol", "antenna_packet_adapter:claim_assessment"},
            "cst-control": {"skill_packet_protocol", "antenna_packet_adapter:run_manifest"},
            "e-platform-cst": {"cst_local", "workflow:cst_local"},
        }
        names = set(aliases.get(skill_id, {capability}))
        packet_type = binding.get("packet_type")
        if packet_type:
            names.add(f"antenna_packet_adapter:{packet_type}")
        return "available" if any(name in capabilities for name in names) else "missing_capability"

    def _stage_for_skill(self, skill_id: str) -> str:
        return {
            "paperwise": "paperwise_evidence",
            "pdf": "pdf_extract",
            "antenna-research-idea-advisor": "idea_evidence",
            "antenna-claim-experiment-planner": "experiment_contract",
            "antenna-baseline-ablation-planner": "baseline_ablation",
            "antenna-research-ideation": "geometry_evidence",
            "cst-control": "cst_control",
            "e-platform-cst": "e_platform",
            "antenna-result-to-claim": "claim_assessment",
            "antenna-research-reviewer": "phase_review",
            "antenna-skills": "antenna_skill_router",
        }.get(skill_id, skill_id.replace("-", "_"))

    def _action_for_skill(self, skill_id: str) -> str:
        return {
            "paperwise": "retrieve_and_trace_evidence",
            "pdf": "extract_pdf_or_figure_evidence",
            "antenna-research-idea-advisor": "review_graph_and_novelty_candidates",
            "antenna-claim-experiment-planner": "plan_claim_experiment_contract",
            "antenna-baseline-ablation-planner": "plan_baselines_and_ablations",
            "antenna-research-ideation": "prepare_geometry_evidence_and_gate",
            "cst-control": "preflight_or_execute_cst_after_approval",
            "e-platform-cst": "select_e_platform_cst_adapter",
            "antenna-result-to-claim": "assess_results_against_claim",
            "antenna-research-reviewer": "review_selected_skill_outputs_before_next_phase",
            "antenna-skills": "route_top_level_antenna_workflow",
        }.get(skill_id, "route_specialized_skill")
