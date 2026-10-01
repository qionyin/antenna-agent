from __future__ import annotations

from typing import Any

from .utils import now_iso, stable_hash

class InnovationEvaluator:
    """Evaluate baseline-gap-delta ideas without claiming novelty from LLM prose alone."""

    REQUIRED = ("baseline_refs", "known_gap", "proposed_delta", "mechanism_hypothesis", "modelable_parameters", "metrics", "baselines", "ablations", "falsification_condition", "evidence_refs")

    def evaluate(self, idea: dict[str, Any], evidence_lookup: Any) -> dict[str, Any]:
        missing = [field for field in self.REQUIRED if not idea.get(field)]
        evidence = [evidence_lookup(ref) for ref in idea.get("evidence_refs") or []]
        evidence = [item for item in evidence if isinstance(item, dict)]
        paper_gate = bool(idea.get("baseline_refs") and idea.get("known_gap") and evidence)
        electromagnetic_gate = bool(idea.get("mechanism_hypothesis") and idea.get("metrics"))
        modeling_gate = bool(idea.get("modelable_parameters")) and not bool(idea.get("breaks_feed_port_boundary"))
        experiment_gate = bool(idea.get("baselines") and idea.get("ablations") and idea.get("falsification_condition"))
        gates = {
            "paper_evidence": {"passed": paper_gate, "reason": "traceable evidence and baseline/gap required"},
            "electromagnetic_logic": {"passed": electromagnetic_gate, "reason": "mechanism and metric mapping required"},
            "modeling_feasibility": {"passed": modeling_gate, "reason": "modelable parameters must preserve feed/port/boundary"},
            "falsifiable_experiment": {"passed": experiment_gate, "reason": "baseline, ablation, and failure condition required"},
        }
        if missing or not paper_gate:
            status = "idea_candidate"
        elif not electromagnetic_gate:
            status = "evidence_supported"
        elif not modeling_gate or not experiment_gate:
            status = "engineering_plausible"
        else:
            status = "experiment_ready"
        validation_refs = list(idea.get("validation_refs") or [])
        if status == "experiment_ready" and validation_refs and bool(idea.get("validated_by_cst_or_measurement")):
            status = "validated_innovation"
        return {
            "schema_version": "1.0",
            "innovation_id": str(idea.get("innovation_id") or stable_hash(idea)[:20]),
            "status": status,
            "novelty_claim_allowed": status == "validated_innovation",
            "missing_fields": missing,
            "gates": gates,
            "evidence_refs": list(idea.get("evidence_refs") or []),
            "validation_refs": validation_refs,
            "reviewed_at": now_iso(),
        }
