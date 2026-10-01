from __future__ import annotations

from statistics import mean
from typing import Any

from ..utils import now_iso, stable_hash


DEFAULT_THRESHOLDS = {
    "recall_warn": 0.8,
    "correctness_fail": 0.5,
    "faithfulness_fail": 0.5,
    "coverage_warn": 0.5,
    "latency_p95_warn_seconds": 300.0,
    "extraction_precision_warn": 0.9,
    "extraction_recall_warn": 0.9,
}


class EvolutionService:
    """Turn evaluation results into reviewable proposals, never direct mutations."""

    def diagnose(self, run: dict[str, Any], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
        limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        findings: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in self._cases(run):
            case_id = str(case.get("id") or case.get("case_id") or "")
            question = str(case.get("question") or "")
            recall = self._number(case.get("retrieval_recall"))
            if recall is not None and recall < limits["recall_warn"]:
                findings.append(self._finding("retrieval_miss", "high", case_id, question, {"recall": recall}))
            correctness = self._number(case.get("judge_correctness"))
            if correctness is not None and correctness < limits["correctness_fail"]:
                findings.append(self._finding("answer_weak", "high", case_id, question, {"correctness": correctness}))
            faithfulness = self._number(case.get("judge_faithfulness"))
            if faithfulness is not None and faithfulness < limits["faithfulness_fail"]:
                findings.append(self._finding("faithfulness", "high", case_id, question, {"faithfulness": faithfulness}))
            coverage = self._number(case.get("fact_coverage"))
            if coverage is not None and coverage < limits["coverage_warn"]:
                findings.append(self._finding("coverage_low", "medium", case_id, question, {"coverage": coverage}))
            if case.get("citation_validity") is False:
                findings.append(self._finding("citation_missing", "medium", case_id, question, {}))
            extraction_precision = self._number(case.get("extraction_precision"))
            if extraction_precision is not None and extraction_precision < limits["extraction_precision_warn"]:
                findings.append(self._finding("extraction_false_positive", "high", case_id, question, {"precision": extraction_precision}))
            extraction_recall = self._number(case.get("extraction_recall"))
            if extraction_recall is not None and extraction_recall < limits["extraction_recall_warn"]:
                findings.append(self._finding(
                    "extraction_false_negative",
                    "high",
                    case_id,
                    question,
                    {"recall": extraction_recall},
                    details={
                        "false_negative_entities": list(case.get("false_negative_entities") or []),
                        "false_negative_relations": list(case.get("false_negative_relations") or []),
                        "suggested_aliases": dict(case.get("suggested_aliases") or {}),
                    },
                ))
            duplicate_rate = self._number(case.get("duplicate_relation_rate"))
            if duplicate_rate is not None and duplicate_rate > 0:
                findings.append(self._finding("duplicate_relation", "high", case_id, question, {"rate": duplicate_rate}))
            unsupported_count = self._number(case.get("unsupported_relation_count"))
            if unsupported_count is not None and unsupported_count > 0:
                findings.append(self._finding("unsupported_relation", "high", case_id, question, {"count": unsupported_count}))
            latency = self._number(case.get("elapsed_s") or case.get("latency_s"))
            if latency is not None:
                latencies.append(latency)
        p95 = self._percentile(latencies, 0.95)
        if p95 is not None and p95 > limits["latency_p95_warn_seconds"]:
            findings.append(self._finding("latency", "medium", "", "", {"p95_seconds": p95}))
        return {
            "schema_version": "1.0",
            "run_id": run.get("run_id"),
            "finding_count": len(findings),
            "findings": findings,
            "thresholds": limits,
            "created_at": now_iso(),
        }

    def propose(self, run: dict[str, Any], *, current_config_hash: str | None = None) -> dict[str, Any]:
        diagnosis = self.diagnose(run)
        proposals = []
        for finding in diagnosis["findings"]:
            kind = finding["type"]
            target = {
                "retrieval_miss": "retrieval",
                "answer_weak": "evidence_review",
                "faithfulness": "evidence_review",
                "coverage_low": "retrieval",
                "citation_missing": "evidence_review",
                "latency": "retrieval",
                "extraction_false_positive": "evidence_extraction",
                "extraction_false_negative": "evidence_extraction",
                "duplicate_relation": "evidence_extraction",
                "unsupported_relation": "evidence_extraction",
            }.get(kind, "review")
            change = self._suggested_change(kind, finding)
            proposals.append({
                "proposal_id": stable_hash({"run_id": run.get("run_id"), "finding": finding})[:20],
                "schema_version": "1.0",
                "target": target,
                "diagnosis": finding,
                "change": change,
                "current_config_hash": current_config_hash,
                "requires_approval": not bool(change.get("automatic")),
                "status": "proposed",
                "verification_required": True,
                "created_at": now_iso(),
            })
        return {
            "schema_version": "1.0",
            "run_id": run.get("run_id"),
            "diagnosis": diagnosis,
            "proposals": proposals,
            "mutation_applied": False,
            "created_at": now_iso(),
        }

    def verify(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        before_metrics = self._metrics(before)
        after_metrics = self._metrics(after)
        deltas = {
            key: round(after_metrics[key] - before_metrics[key], 6)
            for key in before_metrics
            if before_metrics[key] is not None and after_metrics[key] is not None
        }
        quality_keys = {
            "recall",
            "correctness",
            "faithfulness",
            "coverage",
            "extraction_precision",
            "extraction_recall",
            "extraction_f1",
        }
        comparable_quality = quality_keys.intersection(deltas)
        regressions = [key for key in comparable_quality if deltas[key] < 0]
        if "latency_p95" in deltas and deltas["latency_p95"] > 0:
            regressions.append("latency_p95")
        improvements = [key for key in comparable_quality if deltas[key] > 0]
        if "latency_p95" in deltas and deltas["latency_p95"] < 0:
            improvements.append("latency_p95")
        if not comparable_quality:
            decision = "insufficient_evidence"
        elif regressions:
            decision = "reject"
        elif improvements:
            decision = "keep"
        else:
            decision = "inconclusive"
        return {
            "schema_version": "1.0",
            "before_run_id": before.get("run_id"),
            "after_run_id": after.get("run_id"),
            "before": before_metrics,
            "after": after_metrics,
            "deltas": deltas,
            "decision": decision,
            "regressions": regressions,
            "improvements": improvements,
            "verified_at": now_iso(),
        }

    @staticmethod
    def _cases(run: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(run.get("cases"), list):
            return [item for item in run["cases"] if isinstance(item, dict)]
        cases = []
        for workspace in (run.get("workspaces") or {}).values():
            cases.extend(item for item in workspace.get("cases", []) if isinstance(item, dict))
        return cases

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))]

    @staticmethod
    def _finding(
        kind: str,
        severity: str,
        case_id: str,
        question: str,
        metrics: dict[str, float],
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": kind,
            "severity": severity,
            "case_id": case_id,
            "question": question,
            "metrics": metrics,
            "details": details or {},
        }

    @staticmethod
    def _suggested_change(kind: str, finding: dict[str, Any]) -> dict[str, Any]:
        if kind == "extraction_false_negative":
            aliases = dict((finding.get("details") or {}).get("suggested_aliases") or {})
            missing = set((finding.get("details") or {}).get("false_negative_entities") or [])
            aliases = {entity: list(values) for entity, values in aliases.items() if entity in missing and isinstance(values, list)}
            if aliases:
                return {"action": "add_entity_aliases", "aliases": aliases, "automatic": True}
        suggestions = {
            "retrieval_miss": {"action": "review_query_expansion_or_source_index", "automatic": False},
            "coverage_low": {"action": "review_top_k_or_evidence_selection", "automatic": False},
            "answer_weak": {"action": "review_plan_and_evidence_context", "automatic": False},
            "faithfulness": {"action": "tighten_evidence_gate", "automatic": False},
            "citation_missing": {"action": "require_traceable_evidence_refs", "automatic": False},
            "latency": {"action": "review_candidate_count_and_embedding_cache", "automatic": False},
            "extraction_false_positive": {"action": "review_entity_boundaries_and_relation_support", "automatic": False},
            "extraction_false_negative": {"action": "review_extraction_patterns_or_llm_extractor", "automatic": False},
            "duplicate_relation": {"action": "deduplicate_extracted_relations", "automatic": False},
            "unsupported_relation": {"action": "reject_relations_without_source_entities", "automatic": False},
        }
        return suggestions.get(kind, {"action": "manual_review", "automatic": False})

    def _metrics(self, run: dict[str, Any]) -> dict[str, float | None]:
        cases = self._cases(run)
        return {
            "recall": self._average(cases, "retrieval_recall"),
            "correctness": self._average(cases, "judge_correctness"),
            "faithfulness": self._average(cases, "judge_faithfulness"),
            "coverage": self._average(cases, "fact_coverage"),
            "extraction_precision": self._average(cases, "extraction_precision"),
            "extraction_recall": self._average(cases, "extraction_recall"),
            "extraction_f1": self._average(cases, "extraction_f1"),
            "latency_p95": self._percentile([value for value in (self._number(item.get("elapsed_s") or item.get("latency_s")) for item in cases) if value is not None], 0.95),
        }

    def _average(self, cases: list[dict[str, Any]], key: str) -> float | None:
        values = [value for value in (self._number(item.get(key)) for item in cases) if value is not None]
        return round(mean(values), 6) if values else None
