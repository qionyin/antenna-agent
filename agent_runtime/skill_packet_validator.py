from __future__ import annotations

from typing import Any

from .constants import PACKET_TYPES


class SkillPacketValidationError(ValueError):
    pass


REQUIRED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "idea_card": ("problem_anchor", "improved_idea", "research_question", "hypothesis"),
    "experiment_contract": ("claim", "metrics", "success_criteria", "claim_ceiling"),
    "geometry_contract": ("cst_readiness",),
    "run_manifest": ("run_id",),
    "result_packet": ("run_id", "result_status"),
    "claim_assessment": ("claim", "support", "claim_ceiling", "limitations"),
    "next_iteration_plan": ("review_packet", "next_actions", "owner_skills"),
}


class SkillPacketValidator:
    """Validate skill adapter output without reintroducing the V1 packet chain."""

    def validate(self, packet: dict[str, Any], expected_packet_type: str | None = None) -> dict[str, Any]:
        if not isinstance(packet, dict):
            raise SkillPacketValidationError("skill packet must be an object")
        required = {"schema_version", "packet_type", "id", "created_by", "created_at", "status", "payload", "artifacts"}
        missing = sorted(required - packet.keys())
        if missing:
            raise SkillPacketValidationError(f"missing skill packet fields: {missing}")
        packet_type = str(packet.get("packet_type") or "")
        if packet_type not in PACKET_TYPES:
            raise SkillPacketValidationError(f"unsupported skill packet type: {packet_type}")
        if expected_packet_type and packet_type != expected_packet_type:
            raise SkillPacketValidationError(f"skill packet type mismatch: expected {expected_packet_type}, got {packet_type}")
        if str(packet.get("schema_version")) != "1.0":
            raise SkillPacketValidationError(f"unsupported skill packet schema_version: {packet.get('schema_version')}")
        if not isinstance(packet.get("payload"), dict):
            raise SkillPacketValidationError("skill packet payload must be an object")
        if not isinstance(packet.get("artifacts"), list):
            raise SkillPacketValidationError("skill packet artifacts must be a list")
        missing_payload = [field for field in REQUIRED_PAYLOAD_FIELDS.get(packet_type, ()) if field not in packet["payload"]]
        if missing_payload:
            raise SkillPacketValidationError(f"missing required payload fields for {packet_type}: {missing_payload}")
        if packet["payload"].get("data_egress") and not packet["payload"].get("data_egress_declared"):
            raise SkillPacketValidationError("skill packet data egress must be declared")
        if packet_type == "next_iteration_plan":
            review = packet["payload"].get("review_packet")
            if not isinstance(review, dict):
                raise SkillPacketValidationError("review_packet must be an object")
            review_missing = [field for field in ("decision", "blocking_findings", "required_fixes", "evidence_gaps") if field not in review]
            if review_missing:
                raise SkillPacketValidationError(f"missing review_packet fields: {review_missing}")
            if review.get("decision") not in {"pass", "revise", "block"}:
                raise SkillPacketValidationError(f"invalid review_packet decision: {review.get('decision')}")
        return packet
