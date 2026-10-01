from __future__ import annotations

from statistics import mean
from typing import Any

from ..utils import now_iso, stable_hash


class ExtractionQualityEvaluator:
    """Evaluate evidence extraction with structural audits or labeled cases."""

    def audit_compilation(self, compiled: dict[str, Any]) -> dict[str, Any]:
        relations = [
            (item.get("subject"), item.get("relation"), item.get("object"))
            for item in compiled.get("knowledge_candidates") or []
        ]
        unique = set(relations)
        evidence_entities = {
            entity
            for unit in compiled.get("evidence_units") or []
            for entity in unit.get("entities") or []
        }
        unsupported = [
            relation
            for relation in unique
            if relation[0] not in evidence_entities | {"antenna_design"} or relation[2] not in evidence_entities
        ]
        duplicate_count = len(relations) - len(unique)
        return {
            "schema_version": "1.0",
            "audit_type": "extraction_structural_audit",
            "source_id": compiled.get("source_id"),
            "candidate_count": len(relations),
            "unique_relation_count": len(unique),
            "duplicate_relation_count": duplicate_count,
            "duplicate_relation_rate": round(duplicate_count / len(relations), 6) if relations else 0.0,
            "unsupported_relation_count": len(unsupported),
            "unsupported_relations": [list(item) for item in sorted(unsupported)],
            "passed": duplicate_count == 0 and not unsupported,
            "created_at": now_iso(),
        }

    def evaluate(self, cases: list[dict[str, Any]], compiler: Any) -> dict[str, Any]:
        if not cases:
            raise ValueError("extraction evaluation requires at least one case")
        results = []
        for index, case in enumerate(cases, 1):
            case_id = str(case.get("case_id") or case.get("id") or f"case-{index}")
            text = str(case.get("text") or "").strip()
            if not text:
                raise ValueError(f"extraction case {case_id} requires text")
            compiled = compiler.compile({
                "source_id": f"eval:{case_id}",
                "status": "pending_l2_extraction",
                "content": text,
                "content_hash": stable_hash(text),
                "metadata": {"source_type": "paper", "evidence_type": "paper_fact"},
            })
            predicted_entities = set(compiled["evidence_units"][0].get("entities") or [])
            expected_entities = {str(item) for item in case.get("expected_entities") or []}
            predicted_relations = {
                (str(item.get("subject")), str(item.get("relation")), str(item.get("object")))
                for item in compiled.get("knowledge_candidates") or []
            }
            expected_relations = {
                tuple(str(value) for value in item)
                for item in case.get("expected_relations") or []
                if isinstance(item, (list, tuple)) and len(item) == 3
            }
            relation_scores = self._scores(predicted_relations, expected_relations)
            entity_scores = self._scores(predicted_entities, expected_entities)
            audit = self.audit_compilation(compiled)
            results.append({
                "id": case_id,
                "question": text,
                "extraction_precision": relation_scores["precision"],
                "extraction_recall": relation_scores["recall"],
                "extraction_f1": relation_scores["f1"],
                "entity_precision": entity_scores["precision"],
                "entity_recall": entity_scores["recall"],
                "predicted_entities": sorted(predicted_entities),
                "expected_entities": sorted(expected_entities),
                "false_negative_entities": sorted(expected_entities - predicted_entities),
                "suggested_aliases": dict(case.get("suggested_aliases") or {}),
                "predicted_relations": [list(item) for item in sorted(predicted_relations)],
                "expected_relations": [list(item) for item in sorted(expected_relations)],
                "false_positive_relations": [list(item) for item in sorted(predicted_relations - expected_relations)],
                "false_negative_relations": [list(item) for item in sorted(expected_relations - predicted_relations)],
                **{key: audit[key] for key in ("duplicate_relation_count", "duplicate_relation_rate", "unsupported_relation_count")},
            })
        return {
            "schema_version": "1.0",
            "run_id": stable_hash({"type": "extraction", "cases": cases})[:20],
            "evaluation_type": "evidence_extraction",
            "cases": results,
            "summary": {
                "case_count": len(results),
                "precision": round(mean(item["extraction_precision"] for item in results), 6),
                "recall": round(mean(item["extraction_recall"] for item in results), 6),
                "f1": round(mean(item["extraction_f1"] for item in results), 6),
            },
            "created_at": now_iso(),
        }

    @staticmethod
    def _scores(predicted: set[Any], expected: set[Any]) -> dict[str, float]:
        true_positive = len(predicted & expected)
        precision = true_positive / len(predicted) if predicted else float(not expected)
        recall = true_positive / len(expected) if expected else float(not predicted)
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {"precision": round(precision, 6), "recall": round(recall, 6), "f1": round(f1, 6)}
