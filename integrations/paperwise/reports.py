from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import ALGORITHM_GROUPS, DISALLOWED_TOPIC_TERMS, OBJECTIVE_GROUPS, STRUCTURE_GROUPS


class PaperWiseReportsMixin:
    def inventory(self) -> dict[str, int | bool | str]:
        outputs = self.root / "outputs"
        vector_db = outputs / ".kb" / "chroma.sqlite3"
        graph_json = outputs / "graph" / "graph.json"
        graph_info_files = list(outputs.glob("*/graph_info.json")) if outputs.exists() else []
        return {
            "root": str(self.root),
            "available": self.root.exists(),
            "reports": len(list(outputs.rglob("*.md"))) if outputs.exists() else 0,
            "kb_exists": (outputs / ".kb").exists(),
            "vector_library_exists": vector_db.is_file(),
            "vector_library_path": str(vector_db),
            "graph_library_exists": graph_json.is_file() or bool(graph_info_files),
            "graph_library_path": str(graph_json if graph_json.is_file() else outputs),
            "graph_info_files": len(graph_info_files),
        }

    def list_reports(self, limit: int = 50) -> list[dict[str, str | int]]:
        outputs = self.root / "outputs"
        if not outputs.exists():
            return []
        reports = []
        for path in sorted(outputs.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
            reports.append({"path": str(path), "name": path.stem, "size": path.stat().st_size})
        return reports

    def read_report(self, path: str | None = None, max_chars: int = 12000) -> dict[str, object]:
        if path is None:
            reports = self.list_reports(limit=1)
            if not reports:
                return {"available": False, "summary": "", "path": None, "error": "no_reports"}
            path = str(reports[0]["path"])
        report_path = Path(path)
        if not self._is_allowed_report_path(report_path):
            return {"available": False, "summary": "", "path": str(report_path), "error": "path_not_allowed"}
        if not report_path.exists():
            return {"available": False, "summary": "", "path": str(report_path), "error": "not_found"}
        if report_path.suffix.lower() != ".md":
            return {"available": False, "summary": "", "path": str(report_path), "error": "unsupported_report_type"}
        if report_path.stat().st_size > 5_000_000:
            return {"available": False, "summary": "", "path": str(report_path), "error": "report_too_large"}
        text = report_path.read_text(encoding="utf-8", errors="ignore")
        headings = re.findall(r"^#{1,4}\s+(.+)$", text, flags=re.MULTILINE)
        metrics = sorted(set(re.findall(r"\b(?:S11|VSWR|gain|efficiency|bandwidth|axial ratio|ARBW)\b", text, flags=re.IGNORECASE)))
        title = headings[0] if headings else report_path.stem
        return {
            "available": True,
            "path": str(report_path),
            "title": title,
            "display_title": self._display_title_for_report(report_path, title, text),
            "headings": headings[:20],
            "metrics": metrics,
            "summary": " ".join(text.split())[:max_chars],
        }

    def _is_allowed_report_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            outputs = (self.root / "outputs").resolve()
            resolved.relative_to(outputs)
            return True
        except ValueError:
            return False

    def _report_evidence(self, query: str, report_path: str | None, limit: int, profile: dict[str, Any]) -> dict[str, Any]:
        outputs = self.root / "outputs"
        if not outputs.exists():
            return {"status": "missing", "path": str(outputs), "count": 0, "candidates": []}
        if report_path:
            report = self.read_report(report_path, max_chars=2000)
            if not report.get("available"):
                return {"status": "insufficient_evidence", "path": report.get("path"), "count": 0, "candidates": [], "error": report.get("error")}
            report_index = self._build_report_index(report)
            relevance = self._structured_report_relevance(report_index, profile)
            if not relevance["accepted"]:
                return {
                    "status": "insufficient_evidence",
                    "path": report.get("path"),
                    "count": 0,
                    "candidates": [],
                    "error": f"selected report is not relevant enough: {relevance['reason']}",
                }
            return {
                "status": "available",
                "path": report.get("path"),
                "count": 1,
                "candidates": [self._report_candidate(report, score=relevance["score"], relevance=relevance, report_index=report_index)],
            }
        candidates = []
        for item in self.list_reports(limit=1000):
            report = self.read_report(str(item["path"]), max_chars=2000)
            if not report.get("available"):
                continue
            report_index = self._build_report_index(report)
            relevance = self._structured_report_relevance(report_index, profile)
            if relevance["accepted"]:
                candidates.append(self._report_candidate(report, score=relevance["score"], relevance=relevance, report_index=report_index))
        candidates = self._tiered_select(candidates, limit)
        return {
            "status": "available" if candidates else "insufficient_evidence",
            "path": str(outputs),
            "count": len(candidates),
            "candidates": candidates,
        }

    def _report_relevance_text(self, report: dict[str, Any]) -> str:
        parts = [
            str(report.get("title") or ""),
            str(report.get("display_title") or ""),
            " ".join(str(item) for item in report.get("metrics") or []),
        ]
        report_path = Path(str(report.get("path") or ""))
        parts.append(self._report_sidecar_signal_text(report_path))
        summary = str(report.get("summary") or "")
        parts.append(summary[:1500])
        return " ".join(part for part in parts if part).lower()

    def _report_sidecar_signal_text(self, report_path: Path) -> str:
        if not report_path:
            return ""
        values: list[str] = []
        for sidecar_name in ("feature.json", "meta.json", "graph_info.json"):
            sidecar = report_path.with_name(sidecar_name)
            if not sidecar.is_file():
                continue
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue
            self._collect_sidecar_signal_values(data, values, depth=0)
        return " ".join(values[:80])

    def _collect_sidecar_signal_values(self, value: Any, values: list[str], depth: int) -> None:
        if depth > 3 or len(values) >= 80:
            return
        if isinstance(value, dict):
            signal_keys = {
                "title",
                "title_zh",
                "display_title",
                "keywords",
                "concepts",
                "antenna_type",
                "structure",
                "structures",
                "objectives",
                "metrics",
                "algorithm",
                "algorithms",
                "feed",
                "ground",
                "substrate",
                "relations",
                "parameter_count",
                "param_count",
                "parameters_count",
                "parameters",
            }
            for key, item in value.items():
                if str(key).lower() in signal_keys:
                    self._collect_sidecar_signal_values(item, values, depth + 1)
        elif isinstance(value, list):
            for item in value[:30]:
                self._collect_sidecar_signal_values(item, values, depth + 1)
        elif isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                values.append(text[:300])

    def _build_report_index(self, report: dict[str, Any]) -> dict[str, Any]:
        text = self._report_relevance_text(report)
        tokens = sorted(set(self._query_terms(text)))
        structures = self._matched_groups(text, STRUCTURE_GROUPS)
        objectives = self._matched_groups(text, OBJECTIVE_GROUPS)
        algorithms = self._matched_groups(text, ALGORITHM_GROUPS)
        parameter_count = self._extract_parameter_count(text)
        return {
            "title": report.get("title"),
            "display_title": report.get("display_title"),
            "path": report.get("path"),
            "structures": structures,
            "objectives": objectives,
            "algorithms": algorithms,
            "parameter_count": parameter_count,
            "tokens": tokens[:120],
            "evidence_text": text[:1800],
        }

    def _structured_report_relevance(self, report_index: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        text = str(report_index.get("evidence_text") or "")
        if any(term in text for term in DISALLOWED_TOPIC_TERMS):
            return {"accepted": False, "score": 0.0, "reason": "excluded_non_antenna_topic", "matched": {}}
        structures = [name for name in profile.get("structures", []) if name in (report_index.get("structures") or [])]
        objectives = [name for name in profile.get("objectives", []) if name in (report_index.get("objectives") or [])]
        algorithms = [name for name in profile.get("algorithms", []) if name in (report_index.get("algorithms") or [])]
        parameter_count_match = self._parameter_count_matches(profile.get("parameter_count"), report_index.get("parameter_count"))
        task_tokens = set()
        for group_name in [*profile.get("structures", []), *profile.get("objectives", []), *profile.get("algorithms", [])]:
            task_tokens.add(group_name)
        task_tokens.update(profile.get("search_terms") or [])
        report_tokens = set(report_index.get("tokens") or [])
        token_overlap = len(task_tokens & report_tokens)
        family_score = self._algorithm_family_score(profile.get("algorithms") or [], report_index.get("algorithms") or [])
        score = 0.0
        if structures:
            score += 0.45
        if objectives:
            score += 0.25
        if algorithms:
            score += 0.20
        elif family_score:
            score += family_score
        score += min(0.10, token_overlap * 0.02)
        tier = self._match_tier(profile, structures, objectives, algorithms, parameter_count_match, score)
        return {
            "accepted": tier != "reject",
            "score": round(score, 3),
            "reason": tier if tier != "reject" else "below_structured_report_threshold",
            "match_tier": tier,
            "match_percent": self._tier_percent(tier),
            "matched": {
                "structures": structures,
                "objectives": objectives,
                "algorithms": algorithms,
                "parameter_count": report_index.get("parameter_count"),
                "parameter_count_match": parameter_count_match,
                "algorithm_family_score": family_score,
                "token_overlap": token_overlap,
            },
        }

    def _algorithm_family_score(self, requested: list[str], candidate: list[str]) -> float:
        if not requested or not candidate:
            return 0.0
        evolutionary = {"gwo", "pso", "ga", "de"}
        surrogate = {"ann"}
        for req in requested:
            for cand in candidate:
                if req == cand:
                    return 0.20
                if req in evolutionary and cand in evolutionary:
                    return 0.08
                if req in surrogate and cand in surrogate:
                    return 0.08
        return 0.0

    def _report_candidate(
        self,
        report: dict[str, Any],
        score: float,
        relevance: dict[str, Any] | None = None,
        report_index: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        title = str(report.get("title") or "")
        display_title = str(report.get("display_title") or title)
        return {
            "source_type": "paperwise_report",
            "source": "reports",
            "title": title,
            "display_title": display_title,
            "original_title": title,
            "path": str(report.get("path") or ""),
            "metrics": list(report.get("metrics") or []),
            "level": "deep_reading_report",
            "status": "available",
            "evidence_grade": "report_match",
            "score": score,
            "relevance": relevance or {},
            "report_index": report_index or {},
        }

    def _normalize_reports_source(self, reports: dict[str, Any]) -> dict[str, Any]:
        status = reports.get("status", "missing")
        return {
            "source_type": "reports",
            "label": "PaperWise deep-reading reports",
            "roles": ["context", "evidence"],
            "status": status,
            "support_level": "candidate_evidence" if status == "available" else "insufficient_evidence",
            "path": reports.get("path"),
            "count": reports.get("count", 0),
            "items": reports.get("candidates", []),
            "reason": reports.get("error") or (None if status == "available" else "no matching PaperWise report evidence"),
            "read_only": True,
        }

    def _deep_read_papers_source(self, reports: dict[str, Any], vector_library: dict[str, Any], limit: int) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        for item in reports.get("items") or []:
            candidate = dict(item)
            candidate["source"] = "deep_read_papers"
            candidate["source_type"] = "paperwise_deep_read_report"
            candidate["trace_sources"] = [
                {
                    "type": "paperwise_report",
                    "path": item.get("path"),
                    "evidence": item.get("display_title") or item.get("title"),
                }
            ]
            candidate["paper_title"] = item.get("display_title") or item.get("title")
            candidate["paper_path"] = item.get("path")
            candidates.append(candidate)
        for item in vector_library.get("items") or []:
            vector_text = " ".join(str(item.get(key) or "") for key in ("title", "display_title", "paper_title", "snippet", "paper_path")).lower()
            if any(term in vector_text for term in DISALLOWED_TOPIC_TERMS):
                continue
            paper_key = str(item.get("paper_id") or item.get("paper_path") or item.get("paper_title") or item.get("display_title") or item.get("title"))
            existing = next((candidate for candidate in candidates if str(candidate.get("paper_path") or candidate.get("paper_title") or candidate.get("display_title")) == paper_key), None)
            trace = {
                "type": "vector_chunk",
                "path": item.get("path"),
                "evidence": item.get("snippet") or item.get("description") or item.get("title"),
            }
            if existing is not None:
                existing.setdefault("trace_sources", []).append(trace)
                existing["score"] = max(float(existing.get("score") or 0), float(item.get("score") or 0))
                if (item.get("relevance") or {}).get("match_percent", 0) > (existing.get("relevance") or {}).get("match_percent", 0):
                    existing["relevance"] = item.get("relevance") or existing.get("relevance")
                continue
            candidate = {
                "source": "deep_read_papers",
                "source_type": "paperwise_vector_traced_paper",
                "title": item.get("paper_title") or item.get("title"),
                "display_title": item.get("paper_title") or item.get("display_title") or item.get("title"),
                "original_title": item.get("original_title") or item.get("title"),
                "path": item.get("paper_path") or item.get("path"),
                "paper_id": item.get("paper_id"),
                "paper_title": item.get("paper_title") or item.get("display_title") or item.get("title"),
                "paper_path": item.get("paper_path") or item.get("path"),
                "description": item.get("snippet"),
                "level": "deep_reading_paper",
                "status": "available",
                "roles": ["reproduction", "evidence"],
                "score": item.get("score", 0),
                "relevance": item.get("relevance") or {},
                "trace_sources": [trace],
            }
            candidates.append(candidate)
        selected = self._tiered_select(candidates, limit)
        return {
            "source_type": "deep_read_papers",
            "label": "PaperWise deep-read papers",
            "roles": ["reproduction", "evidence"],
            "status": "available" if selected else "insufficient_evidence",
            "support_level": "candidate_paper_evidence" if selected else "insufficient_evidence",
            "path": reports.get("path") or vector_library.get("path"),
            "count": len(selected),
            "items": selected,
            "reason": None if selected else "no matching PaperWise deep-read paper after report/vector trace",
            "read_only": True,
            "trace_policy": "Vector chunks and report hits are traced back to their source paper before review.",
        }
