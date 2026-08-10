from __future__ import annotations

from typing import Any


def fake_llm_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic external-LLM fixture; never imported by production code."""
    mode = str(payload.get("mode") or "mock")
    text = str(payload.get("user_goal") or "").lower()
    selected = {str(item.get("owner_skill")) for item in payload.get("callable_skill_routes") or []}
    if payload.get("modeling_request"):
        return {"steps": [
            _step(
                "Prepare and validate traceable antenna geometry up to cst_model_spec",
                "modeling_preparation",
                ["antenna-research-ideation"],
                ["modeling_request"],
                ["cst_model_spec"],
                "stop before CST execution",
            )
        ]}
    if mode == "real":
        return {"steps": [
            _step("Collect traceable PaperWise evidence", "evidence_retrieval", ["paperwise"], ["user_goal"], ["evidence_pool"], "PaperWise read-only"),
            _step("Check CST environment and request inputs", "preflight", ["cst-control"], ["request"], ["preflight_result"], "read-only preflight"),
            _step("Wait for bound user approval", "approval", [], ["preflight_result"], ["approval_record"], "approval.status == approved"),
            _step("Run one approved real CST job", "cst_run", ["cst-control", "e-platform-cst"], ["approval_record", "request"], ["cst_run_manifest"], "real CST approval required"),
            _step("Parse S11, return loss, and bandwidth", "result_parse", [], ["cst_run_manifest"], ["parsed_result"], "run review passed"),
            _step("Generate the terminal report", "report", [], ["parsed_result"], ["task_report"], "parsed result review passed"),
        ]}

    antenna = any(term in text for term in ("antenna", "s11", "gain", "arbw", "cst", "patch", "slot", "gwo", "claim"))
    if not antenna:
        return {"steps": [
            _step("Handle the general request", "general", [], ["user_goal"], ["general_task_summary"], "no specialized skill required"),
        ]}

    steps = [
        _step("Collect traceable evidence", "evidence_retrieval", ["paperwise"] if "paperwise" in selected else [], ["user_goal"], ["evidence_pool"], "PaperWise read-only"),
        _step("Normalize antenna goals and constraints", "goal_contract", ["antenna-claim-experiment-planner"] if "antenna-claim-experiment-planner" in selected else [], ["user_goal", "evidence_pool"], ["objective_contract"], "evidence limitations recorded"),
    ]
    baseline_requested = any(term in text for term in ("baseline", "ablation", "gwo and pso"))
    if baseline_requested or any(term in text for term in ("geometry", "model", "cst", "reproduce", "paper")):
        steps.append(_step("Prepare geometry and modeling evidence", "geometry_evidence", ["antenna-research-ideation"] if "antenna-research-ideation" in selected else [], ["evidence_pool", "objective_contract"], ["geometry_contract"], "traceable geometry evidence required"))
    if baseline_requested:
        steps.append(_step("Plan baseline and ablation checks", "baseline_ablation", ["antenna-baseline-ablation-planner"] if "antenna-baseline-ablation-planner" in selected else [], ["objective_contract"], ["baseline_ablation_plan"], "objective contract exists"))
    if "claim_assessment" in text and "evidence" in text:
        steps.append(_step("Check traceable paper evidence", "evidence_check", ["paperwise"] if "paperwise" in selected else [], ["evidence_pool"], ["claim_evidence_review"], "PaperWise evidence refs required"))
    if "arbw" in text and any(term in text for term in ("three", "3")):
        steps.append(_step("Assess ARBW sample sufficiency", "antenna_result_to_claim", ["antenna-result-to-claim"] if "antenna-result-to-claim" in selected else [], ["evidence_pool"], ["claim_assessment"], "result evidence required"))
    elif any(term in text for term in ("claim", "conclusion", "support", "arbw")):
        steps.append(_step("Assess evidence support for the claim", "claim_assessment", ["antenna-result-to-claim"] if "antenna-result-to-claim" in selected else [], ["evidence_pool"], ["claim_assessment"], "evidence refs required"))
    steps.append(_step("Write the task report", "report", [], ["evidence_pool"], ["task_report"], "reviewed outputs available"))
    return {"steps": steps}


def _step(
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
