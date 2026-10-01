from __future__ import annotations

from typing import Any

from .utils import now_iso, stable_hash

class WikiProjection:
    """Build a readable wiki without creating a second knowledge graph."""

    def build(
        self,
        records: list[dict[str, Any]],
        evidence_lookup: Any,
        experiences: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        promoted = [
            record
            for record in records
            if record.get("lifecycle_status") == "promoted"
            and record.get("evidence_type") in {"paper_fact", "source_paper_fact"}
            and self._paper_evidence_complete(record, evidence_lookup)
        ]
        source_version = stable_hash({"knowledge_ids": [record["knowledge_id"] for record in promoted]})[:20]
        pages = []
        for subject in sorted({str(record.get("subject")) for record in promoted if record.get("subject")}):
            statements = [{
                "knowledge_id": record["knowledge_id"],
                "statement": f"{record['subject']} {record['relation']} {record['object']}",
                "effect": record.get("effect"),
                "conditions": record.get("conditions") or {},
                "limits": record.get("limits") or [],
                "evidence_refs": record.get("evidence_refs") or [],
            } for record in promoted if str(record.get("subject")) == subject]
            pages.append({
                "schema_version": "1.0",
                "page_id": stable_hash({"subject": subject})[:20],
                "title": subject,
                "source_of_truth": "promoted_paper_evidence",
                "source_version": source_version,
                "graph_source": "existing_paperwise_or_antenna_research_graph",
                "statements": statements,
                "generated_at": now_iso(),
            })
        promoted_experiences = [
            experience
            for experience in experiences or []
            if experience.get("lifecycle_status") == "promoted"
        ]
        if promoted_experiences:
            pages.append({
                "schema_version": "1.0",
                "page_id": stable_hash({"subject": "workflow_experiences"})[:20],
                "title": "实践经验",
                "page_type": "experience_wiki",
                "source_of_truth": "promoted_workflow_experience",
                "source_version": source_version,
                "graph_source": "none; experience notes never create graph relations",
                "statements": [
                    {
                        "experience_id": experience.get("experience_id"),
                        "episode_id": experience.get("source_episode_id"),
                        "statement_type": "experience_note",
                        "trigger": experience.get("trigger") or {},
                        "action": experience.get("action") or "",
                        "lesson": experience.get("lesson") or "",
                        "result": experience.get("result") or {},
                        "when_not_to_use": experience.get("when_not_to_use") or [],
                        "evidence_refs": experience.get("evidence_refs") or [],
                        "source_scope": "experience_memory_only; not a graph relation or universal domain fact",
                    }
                    for experience in promoted_experiences
                ],
                "generated_at": now_iso(),
            })
        return {"source_version": source_version, "wiki_pages": pages}

    @staticmethod
    def _paper_evidence_complete(record: dict[str, Any], evidence_lookup: Any) -> bool:
        refs = list(record.get("evidence_refs") or [])
        if not refs:
            return False
        evidence = [evidence_lookup(ref) for ref in refs]
        return all(
            isinstance(item, dict)
            and item.get("evidence_type") in {"paper_fact", "source_paper_fact"}
            and bool(item.get("source_id"))
            and bool(item.get("source_ref"))
            and bool(item.get("locator"))
            and bool(item.get("text"))
            for item in evidence
        )
