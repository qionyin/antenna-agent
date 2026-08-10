from __future__ import annotations

import json
from typing import Any, Callable

from .runtime_llm_config import runtime_llm_config
from .utils import now_iso, stable_hash


class SubagentRuntime:
    """Run dynamic-plan task, module-review, and global-review subagents."""

    def run_task(
        self,
        *,
        task_id: str,
        step: dict[str, Any],
        context: dict[str, Any],
        allow_external_llm: bool,
        local_output: dict[str, Any] | None = None,
        executor: Callable[[], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        prepared_output = {
            "schema_version": "1.0",
            "step_id": step.get("step_id"),
            "status": "prepared",
            "outputs": step.get("outputs") or [],
            "evidence_refs": step.get("evidence_refs") or [],
            "required_skills": step.get("required_skills") or [],
            "callable_skills": step.get("callable_skills") or [],
            "candidate_skills": step.get("candidate_skills") or [],
            "no_cst_execution": True,
        }
        executed_output = executor() if executor is not None else local_output
        if executed_output is not None and not isinstance(executed_output, dict):
            raise TypeError("task subagent executor must return a dictionary")
        if executed_output:
            prepared_output.update(executed_output)
        llm_payload = self._try_external_llm(
            agent_role="task",
            agent_id=str(step.get("task_agent") or "dynamic_task_agent"),
            step=step,
            context=context,
            allow_external_llm=allow_external_llm,
        )
        if llm_payload.get("llm_success") and isinstance(llm_payload.get("output"), dict):
            prepared_output.update(llm_payload["output"])
            prepared_output.setdefault("schema_version", "1.0")
            prepared_output.setdefault("step_id", step.get("step_id"))
        return self._result(
            task_id=task_id,
            step=step,
            agent_id=str(step.get("task_agent") or "dynamic_task_agent"),
            agent_role="task",
            decision="pass",
            output=prepared_output,
            llm=llm_payload,
            confidence=0.78 if llm_payload.get("llm_success") else 0.68,
            confidence_basis=["task output prepared", llm_payload.get("llm_mode", "local_fallback")],
        )

    def run_module_review(
        self,
        *,
        task_id: str,
        step: dict[str, Any],
        task_result: dict[str, Any],
        context: dict[str, Any],
        allow_external_llm: bool,
        local_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output = task_result.get("output") or {}
        review = dict(local_review) if local_review is not None else self._review_step_output(
            step=step,
            output=output,
            context={"real_cst_approved": bool(context.get("real_cst_approved"))},
        )
        review.setdefault("schema_version", "1.0")
        review.setdefault("decision", "pass" if review.get("status") == "pass" else str(review.get("status") or "pass"))
        review.setdefault("status", "pass" if review.get("decision") == "pass" else review.get("decision"))
        review.setdefault("blocking_findings", review.get("structured_blockers") or [])
        review.setdefault("required_fixes", [])
        review.setdefault("reflection", {"status": "no_issue", "error_type": "none", "confidence": 0.90})
        review["review_agent"] = str(step.get("module_review_agent") or "module_review_agent")
        review["review_scope"] = "module"
        llm_payload = self._try_external_llm(
            agent_role="module_review",
            agent_id=str(step.get("module_review_agent") or "module_review_agent"),
            step=step,
            context={**context, "task_output": output, "local_review": review},
            allow_external_llm=allow_external_llm,
        )
        if llm_payload.get("llm_success") and isinstance(llm_payload.get("output"), dict):
            review.update({k: v for k, v in llm_payload["output"].items() if k in {"decision", "required_fixes", "blocking_findings"}})
        return self._result(
            task_id=task_id,
            step=step,
            agent_id=str(step.get("module_review_agent") or "module_review_agent"),
            agent_role="module_review",
            decision=str(review.get("decision") or "pass"),
            output=review,
            llm=llm_payload,
            confidence=float((review.get("reflection") or {}).get("confidence") or 0.86),
            confidence_basis=["current step artifact/schema review", llm_payload.get("llm_mode", "local_fallback")],
        )

    def run_global_review(
        self,
        *,
        task_id: str,
        step: dict[str, Any],
        task_result: dict[str, Any],
        module_review: dict[str, Any],
        history: list[dict[str, Any]],
        context: dict[str, Any],
        allow_external_llm: bool,
    ) -> dict[str, Any]:
        module_decision = str(module_review.get("decision") or "pass")
        step_skill_context = context.get("step_skill_context") or step.get("step_skill_context") or {}
        callable_skills = list(step_skill_context.get("callable_skills") or step.get("callable_skills") or [])
        candidate_skills = list(step_skill_context.get("candidate_skills") or step.get("candidate_skills") or [])
        required_skills = set(step.get("required_skills") or [])
        callable_owners = {str(item.get("owner_skill")) for item in callable_skills if isinstance(item, dict)}
        candidate_owners = {str(item.get("owner_skill")) for item in candidate_skills if isinstance(item, dict)}
        missing_required = sorted(required_skills - callable_owners)
        output = {
            "schema_version": "1.0",
            "review_agent": "global_review_agent",
            "review_scope": "global",
            "decision": module_decision,
            "status": "pass" if module_decision == "pass" else module_decision,
            "evidence_level": "B" if module_decision == "pass" else "C",
            "blocking_findings": [],
            "required_fixes": [],
            "reflection": {
                "status": "no_issue" if module_decision == "pass" else "advisory",
                "error_type": "none" if module_decision == "pass" else "weak_evidence",
                "why_central_failed": "",
                "what_should_change": "",
                "reroute_required": False,
                "confidence": 0.90 if module_decision == "pass" else 0.84,
                "confidence_basis": {
                    "source": "global_rule_review",
                    "module_decision": module_decision,
                    "history_steps": len(history),
                    "callable_skill_count": len(callable_skills),
                    "candidate_skill_count": len(candidate_skills),
                    "missing_required_skills": missing_required,
                },
            },
            "reroute_suggestion": {"action": "none", "reason": "", "suggested_steps": []},
        }
        if module_decision in {"block", "blocked"}:
            output["decision"] = "block"
            output["status"] = "block"
            output["evidence_level"] = "D"
            output["blocking_findings"] = module_review.get("blocking_findings") or []
            output["reflection"]["status"] = "blocked"
            output["reflection"]["error_type"] = "missing_dependency"
            output["reflection"]["why_central_failed"] = "module review blocked the current step"
            output["reflection"]["what_should_change"] = "repair current step output before downstream execution"
        elif missing_required:
            output["decision"] = "reroute" if candidate_owners.intersection(missing_required) else "revise"
            output["status"] = output["decision"]
            output["evidence_level"] = "C"
            output["required_fixes"] = [f"refresh route or supply evidence for {skill}" for skill in missing_required]
            output["reflection"]["status"] = "needs_reroute" if output["decision"] == "reroute" else "advisory"
            output["reflection"]["error_type"] = "missing_dependency"
            output["reflection"]["why_central_failed"] = "required step skill is not currently callable"
            output["reflection"]["what_should_change"] = "refresh step skill route after new evidence or review feedback"
            output["reflection"]["reroute_required"] = output["decision"] == "reroute"
            output["reflection"]["confidence"] = 0.88
            output["reroute_suggestion"] = {
                "action": "refresh_route",
                "reason": "required skill is candidate or missing for this step",
                "suggested_steps": [],
                "suggested_skills": missing_required,
            }
        llm_payload = self._try_external_llm(
            agent_role="global_review",
            agent_id="global_review_agent",
            step=step,
            context={**context, "task_result": task_result, "module_review": module_review, "history": history},
            allow_external_llm=allow_external_llm,
        )
        if llm_payload.get("llm_success") and isinstance(llm_payload.get("output"), dict):
            output.update({k: v for k, v in llm_payload["output"].items() if k in {"decision", "required_fixes", "blocking_findings", "reroute_suggestion"}})
        return self._result(
            task_id=task_id,
            step=step,
            agent_id="global_review_agent",
            agent_role="global_review",
            decision=str(output.get("decision") or "pass"),
            output=output,
            llm=llm_payload,
            confidence=float((output.get("reflection") or {}).get("confidence") or 0.88),
            confidence_basis=["cross-step dependency and evidence-chain review", llm_payload.get("llm_mode", "local_fallback")],
        )

    def _review_step_output(
        self,
        *,
        step: dict[str, Any],
        output: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply module-level artifact, evidence, dependency, and safety checks."""
        blockers: list[dict[str, str]] = []
        fixes: list[str] = []
        goal = str(step.get("step_goal") or "").lower()
        evidence_refs = list(output.get("evidence_refs") or step.get("evidence_refs") or [])
        if any(term in goal for term in ("claim", "report", "cst", "geometry")) and not evidence_refs:
            fixes.append("attach traceable evidence_refs before claiming pass")
        if output.get("contains_live_cst") and not context.get("real_cst_approved"):
            blockers.append({"type": "unsafe_execution", "reason": "live_cst_without_approval"})
        if output.get("missing_required_input"):
            blockers.append({"type": "missing_dependency", "reason": str(output["missing_required_input"])})
        if blockers:
            return self._review_result(
                decision="block",
                evidence_level="D",
                blocking_findings=blockers,
                required_fixes=fixes,
                reflection_status="blocked",
                error_type="unsafe_execution" if any(item["type"] == "unsafe_execution" for item in blockers) else "missing_dependency",
                why="step output cannot safely feed the next step",
                change="repair dependencies or approvals, then rerun the step",
            )
        if fixes:
            return self._review_result(
                decision="revise",
                evidence_level="C",
                required_fixes=fixes,
                reflection_status="advisory",
                error_type="weak_evidence",
                why="step output has weak traceability",
                change="add evidence references or downgrade conclusion strength",
            )
        return self._review_result(decision="pass", evidence_level="B")

    @staticmethod
    def _review_result(
        *,
        decision: str,
        evidence_level: str,
        blocking_findings: list[dict[str, str]] | None = None,
        required_fixes: list[str] | None = None,
        reflection_status: str = "no_issue",
        error_type: str = "none",
        why: str = "",
        change: str = "",
    ) -> dict[str, Any]:
        confidence = {"A": 0.98, "B": 0.92, "C": 0.82, "D": 0.72}.get(evidence_level, 0.75)
        return {
            "schema_version": "1.0",
            "review_agent": "module_review_agent",
            "decision": decision,
            "status": "pass" if decision == "pass" else decision,
            "evidence_level": evidence_level,
            "blocking_findings": blocking_findings or [],
            "required_fixes": required_fixes or [],
            "reflection": {
                "status": reflection_status,
                "error_type": error_type,
                "why_central_failed": why,
                "what_should_change": change,
                "reroute_required": False,
                "confidence": confidence,
                "confidence_basis": {"source": "module_rule_review", "evidence_level": evidence_level},
            },
            "reroute_suggestion": {"action": "none", "reason": "", "suggested_steps": []},
        }

    def _result(
        self,
        *,
        task_id: str,
        step: dict[str, Any],
        agent_id: str,
        agent_role: str,
        decision: str,
        output: dict[str, Any],
        llm: dict[str, Any],
        confidence: float,
        confidence_basis: list[str],
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "result_id": stable_hash({"task_id": task_id, "step_id": step.get("step_id"), "agent_id": agent_id, "role": agent_role, "output": output})[:16],
            "task_id": task_id,
            "step_id": step.get("step_id"),
            "agent_id": agent_id,
            "agent_role": agent_role,
            "decision": decision,
            "status": "pass" if decision == "pass" else decision,
            "input_refs": step.get("inputs") or [],
            "output_refs": step.get("outputs") or [],
            "evidence_refs": output.get("evidence_refs") or step.get("evidence_refs") or [],
            "output": output,
            "llm_mode": llm.get("llm_mode", "local_fallback"),
            "llm_attempted": bool(llm.get("llm_attempted")),
            "llm_success": bool(llm.get("llm_success")),
            "data_egress": bool(llm.get("data_egress")),
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "confidence_basis": confidence_basis,
            "created_at": now_iso(),
        }

    def _try_external_llm(
        self,
        *,
        agent_role: str,
        agent_id: str,
        step: dict[str, Any],
        context: dict[str, Any],
        allow_external_llm: bool,
    ) -> dict[str, Any]:
        if not runtime_llm_config.enabled:
            return {"llm_mode": "local_fallback", "llm_attempted": False, "llm_success": False, "data_egress": False, "reason": "runtime_llm_disabled"}
        if not allow_external_llm:
            return {"llm_mode": "local_fallback", "llm_attempted": False, "llm_success": False, "data_egress": True, "reason": "external_llm_not_approved"}
        if not (runtime_llm_config.base_url and runtime_llm_config.api_key and runtime_llm_config.model_name):
            return {"llm_mode": "local_fallback", "llm_attempted": False, "llm_success": False, "data_egress": False, "reason": "runtime_llm_incomplete"}
        try:
            from openai import OpenAI

            client = OpenAI(api_key=runtime_llm_config.api_key, base_url=runtime_llm_config.base_url, timeout=30)
            prompt = {
                "agent_role": agent_role,
                "agent_id": agent_id,
                "step": {
                    "step_id": step.get("step_id"),
                    "step_goal": step.get("step_goal"),
                    "inputs": step.get("inputs"),
                    "outputs": step.get("outputs"),
                    "callable_skills": [item.get("owner_skill") for item in step.get("callable_skills") or []],
                    "candidate_skills": [item.get("owner_skill") for item in step.get("candidate_skills") or []],
                },
                "context": context,
            }
            response = client.chat.completions.create(
                model=runtime_llm_config.model_name,
                temperature=0,
                messages=[
                    {"role": "system", "content": "Return strict JSON. Keep outputs traceable and conservative."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)[:12000]},
                ],
            )
            raw = response.choices[0].message.content or "{}"
            parsed = json.loads(raw)
            return {
                "llm_mode": "external_llm",
                "llm_attempted": True,
                "llm_success": True,
                "data_egress": True,
                "output": parsed if isinstance(parsed, dict) else {"value": parsed},
            }
        except Exception as exc:
            return {
                "llm_mode": "local_fallback",
                "llm_attempted": True,
                "llm_success": False,
                "data_egress": True,
                "reason": f"external_llm_failed:{exc}",
            }
