from __future__ import annotations

from typing import Any

from .utils import now_iso, stable_hash
from .common import _normalize_text

class KnowledgeReviewer:
    """Reject unsupported merges and preserve conditional or contradictory claims."""

    def review(
        self,
        candidate: dict[str, Any],
        evidence_lookup: Any,
        peers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        findings = []
        for field in ("knowledge_id", "subject", "relation", "object", "evidence_refs", "conditions", "evidence_type"):
            if not candidate.get(field):
                findings.append(f"missing:{field}")
        evidence = [evidence_lookup(ref) for ref in candidate.get("evidence_refs") or []]
        evidence = [item for item in evidence if isinstance(item, dict)]
        if len(evidence) != len(candidate.get("evidence_refs") or []):
            findings.append("unresolved_evidence_ref")
        if candidate.get("subject") == candidate.get("object"):
            findings.append("self_relation")
        if candidate.get("evidence_type") == "llm_inference":
            findings.append("llm_inference_requires_external_support")
        conflict_ids = [
            str(peer.get("knowledge_id"))
            for peer in peers or []
            if peer.get("knowledge_id") != candidate.get("knowledge_id")
            and self._same_relation(candidate, peer)
            and self._opposing_effects(str(candidate.get("effect") or ""), str(peer.get("effect") or ""))
        ]
        decision = "reject" if any(item.startswith("missing:") or item in {"unresolved_evidence_ref", "self_relation"} for item in findings) else "keep_separate"
        if conflict_ids:
            decision = "conflict"
            findings.append("conditional_conflict")
        elif not findings:
            decision = "merge_with_conditions" if any((candidate.get("conditions") or {}).values()) else "keep_separate"
        return {
            "schema_version": "1.0",
            "review_id": stable_hash({"candidate": candidate.get("knowledge_id"), "findings": findings, "decision": decision})[:20],
            "knowledge_id": candidate.get("knowledge_id"),
            "decision": decision,
            "evidence_level": "B" if evidence and not findings else "C" if evidence else "D",
            "findings": findings,
            "conflict_with": conflict_ids,
            "promotable": bool(evidence and decision in {"keep_separate", "merge_with_conditions"} and candidate.get("evidence_type") != "llm_inference"),
            "reviewed_at": now_iso(),
        }

    @staticmethod
    def _same_relation(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return all(_normalize_text(str(left.get(key) or "")) == _normalize_text(str(right.get(key) or "")) for key in ("subject", "relation", "object"))

    @staticmethod
    def _opposing_effects(left: str, right: str) -> bool:
        positive = {"increase", "increases", "improve", "improves", "positive", "增加", "提高", "改善"}
        negative = {"decrease", "decreases", "reduce", "reduces", "negative", "降低", "减小", "恶化"}
        left_norm = _normalize_text(left)
        right_norm = _normalize_text(right)
        return (any(term in left_norm for term in positive) and any(term in right_norm for term in negative)) or (any(term in left_norm for term in negative) and any(term in right_norm for term in positive))
