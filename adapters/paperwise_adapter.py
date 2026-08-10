from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

from agent_runtime.config import load_settings
from agent_runtime.embedding import normalize_vector
from agent_runtime.utils import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BM25_RECALL_TOP_K = 50
EMBEDDING_RECALL_TOP_K = 15
PAPERWISE_HYBRID_MIN_SCORE = 0.51
ANTENNA_DICTIONARY_PATH = PROJECT_ROOT / "agent_runtime" / "dictionaries" / "antenna_terms.txt"

STRUCTURE_GROUPS = {
    "patch": [
        "u-slot microstrip antenna",
        "patch antenna",
        "patch antennas",
        "microstrip patch",
        "u-slot patch",
        "u-slot microstrip",
        "printed patch",
        "贴片",
        "微带贴片",
        "微带天线",
        "u槽微带",
    ],
    "filtering": ["filtering antenna", "filtering patch", "滤波天线", "滤波贴片"],
    "monopole": ["monopole", "printed monopole", "uwb antenna", "单极子", "印刷单极子"],
    "dipole": ["dipole", "偶极子"],
    "array": ["antenna array", "phased array", "linear antenna array", "planar array", "阵列", "相控阵"],
    "transmitarray": ["transmitarray", "reflectarray", "透射阵", "反射阵"],
    "mimo": ["mimo antenna", "mimo antennas", "multi-input multi-output antenna", "多输入多输出天线", "mimo天线"],
    "slot": ["slot antenna", "slot", "cpw-fed", "缝隙天线", "开槽", "共面波导馈电"],
    "horn": ["horn antenna", "喇叭天线"],
    "leaky_wave": ["leaky wave", "漏波"],
    "circular_polarization": ["circularly polarized", "circular polarization", "圆极化"],
    "dielectric_resonator": ["dielectric resonator", "dra", "介质谐振器"],
    "wearable": ["wearable antenna", "可穿戴天线"],
    "pixelated": ["pixelated antenna", "像素化天线"],
    "metasurface": ["metasurface", "ris", "reconfigurable intelligent surface", "超表面", "可重构智能表面"],
    "subarray": ["subarray", "cross-grid array", "子阵", "交叉网格阵列"],
}

OBJECTIVE_GROUPS = {
    "s11": ["s11", "s 11", "s-parameter", "s-parameters", "return loss", "reflection coefficient", "回波损耗", "反射系数"],
    "bandwidth": ["bandwidth", "wideband", "broadband", "带宽", "宽带"],
    "gain": ["gain", "realized gain", "增益"],
    "efficiency": ["efficiency", "效率"],
    "axial_ratio": ["axial ratio", "arbw", "circular polarization", "圆极化", "轴比"],
}

ALGORITHM_GROUPS = {
    "gwo": ["gwo", "grey wolf", "gray wolf", "grey wolf optimizer", "gray wolf optimizer", "灰狼"],
    "pso": ["pso", "particle swarm", "particle swarm optimization", "粒子群"],
    "ga": ["ga", "genetic algorithm", "遗传算法"],
    "ann": ["ann", "artificial neural network", "neural network", "神经网络", "代理模型", "surrogate"],
    "de": ["de", "differential evolution", "差分进化"],
}

DISALLOWED_TOPIC_TERMS = [
    "computerized tomography",
    "tomography diagnosis",
    "heart disease",
    "medical image",
    "medical imaging",
    "mri",
    "magnetic resonance",
    "catheter",
    "intravascular",
    "imaging system",
    "b1 field",
    "sar distribution",
    "医学影像",
    "心脏病",
    "ct颅内",
    "图像诊断",
    "磁共振",
    "导管",
    "血管内",
    "成像系统",
]

MATCH_TIER_QUOTAS = [
    ("exact", 5),
    ("match_80", 3),
    ("match_60", 2),
]

CRITICAL_MODELING_TERMS = [
    "feed",
    "cpw",
    "microstrip",
    "coax",
    "probe",
    "excitation",
    "port",
    "waveguide",
    "lumped",
    "discrete",
    "ground",
    "gnd",
    "ground plane",
    "slot",
    "slit",
    "cut",
    "stub",
    "via",
    "substrate",
    "layer",
    "stack",
    "dielectric",
    "馈电",
    "端口",
    "地板",
    "接地",
    "开槽",
    "枝节",
    "通孔",
    "介质板",
    "层叠",
]


@lru_cache(maxsize=1)
def _paperwise_tokenizer() -> jieba.Tokenizer:
    tokenizer = jieba.Tokenizer()
    if ANTENNA_DICTIONARY_PATH.exists():
        with ANTENNA_DICTIONARY_PATH.open("r", encoding="utf-8") as dictionary:
            tokenizer.load_userdict(dictionary)
    return tokenizer


def _bm25_tokens(text: str) -> list[str]:
    normalized = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens: list[str] = []
    for part in _paperwise_tokenizer().cut_for_search(normalized, HMM=False):
        tokens.extend(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", str(part).lower()))
    stop = {"the", "and", "for", "with", "using", "based", "paper", "research", "antenna"}
    return [token for token in tokens if token not in stop]


def _cache_safe_model_name(model_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", model_name or "embedding")


def _embedding_cache_key(record_id: str, text: str) -> str:
    digest = hashlib.sha256(f"{record_id}\n{text[:4000]}".encode("utf-8")).hexdigest()[:24]
    return f"{record_id}:{digest}"


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return float(sum(a * b for a, b in zip(left, right)))


class PaperWiseAdapter:
    def __init__(self, root: str | Path):
        self.root = Path(root)

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

    def evidence_pool_summary(
        self,
        query: str = "",
        report_path: str | None = None,
        limit: int = 10,
        allow_external_embedding: bool = False,
    ) -> dict[str, Any]:
        profile = self._query_profile(query)
        reports = self._normalize_reports_source(self._report_evidence(query, report_path=report_path, limit=limit, profile=profile))
        vector_library = self._vector_library_summary(
            query,
            limit=limit,
            profile=profile,
            allow_external_embedding=allow_external_embedding,
        )
        deep_read_papers = self._deep_read_papers_source(reports, vector_library, limit)
        graph_library = self._graph_library_summary(query=query, limit=limit, profile=profile)
        insufficiencies = []
        for name, source in (
            ("reports", reports),
            ("deep_read_papers", deep_read_papers),
            ("graph_library", graph_library),
        ):
            if source.get("status") != "available":
                insufficiencies.append(f"{name}:{source.get('status')}")
        evidence_count = (
            len(reports.get("items") or [])
            + len(deep_read_papers.get("items") or [])
            + len(graph_library.get("items") or [])
        )
        status = "available" if evidence_count > 0 and not insufficiencies else "insufficient_evidence"
        return {
            "schema_version": "1.0",
            "summary_type": "paperwise_evidence_pool_summary",
            "source": "PaperWise",
            "retrieval_agent": "evidence_retrieval_task_agent",
            "review_agent": None,
            "review_mode": "not_reviewed",
            "root": str(self.root),
            "outputs_path": str(self.root / "outputs"),
            "read_only": True,
            "data_egress": False,
            "query": query,
            "relevance_profile": profile,
            "status": status,
            "support_level": "candidate_evidence" if status == "available" else "insufficient_evidence",
            "evidence_level": "paperwise_backed" if status == "available" else "insufficient_evidence",
            "insufficiencies": insufficiencies,
            "react_review": {"accepted": [], "rejected": [], "uncertain": [], "evidence_gaps": []},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sources": {
                "reports": reports,
                "deep_read_papers": deep_read_papers,
                "graph_library": graph_library,
            },
            "internal_sources": {
                "vector_library": vector_library,
            },
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

    def _vector_library_summary(
        self,
        query: str,
        limit: int,
        profile: dict[str, Any],
        allow_external_embedding: bool = False,
    ) -> dict[str, Any]:
        db_path = self.root / "outputs" / ".kb" / "chroma.sqlite3"
        result: dict[str, Any] = {
            "status": "missing",
            "path": str(db_path),
            "roles": ["reproduction", "evidence"],
            "description": "PaperWise vector library stores deep-read paper chunks for reproduction and evidence lookup.",
            "support_level": "insufficient_evidence",
            "items": [],
            "chunk_count": 0,
            "paper_count": 0,
            "read_only": True,
            "retrieval_backend": "not_loaded",
            "vector_query_error": None,
        }
        if not db_path.is_file():
            return result
        vector_items: list[dict[str, Any]] = []
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            try:
                result["chunk_count"] = self._sqlite_count(conn, "embeddings")
                result["metadata_count"] = self._sqlite_count(conn, "embedding_metadata")
                result["paper_count"] = self._vector_paper_count(conn)
                try:
                    vector_items = self._project_hybrid_vector_query_items(
                        conn,
                        query,
                        limit,
                        profile,
                        allow_external_embedding=allow_external_embedding,
                    )
                    result["retrieval_backend"] = (
                        "project_hybrid_bm25_50_qwen_15_semantic_rerank"
                        if allow_external_embedding
                        else "project_bm25_sqlite_fallback"
                    )
                except Exception as exc:
                    result["vector_query_error"] = self._vector_query_error(exc)
                    vector_items = self._vector_query_items(conn, query, limit, profile)
                    result["retrieval_backend"] = "project_sqlite_like_fallback_after_hybrid_error"
                result["items"] = vector_items
                result["status"] = "available" if result["items"] else "insufficient_evidence"
                result["support_level"] = "candidate_evidence" if result["status"] == "available" else "insufficient_evidence"
                if not result["items"] and result["chunk_count"]:
                    result["reason"] = "vector library exists but no query-related deep-reading chunk was found"
            finally:
                conn.close()
        except Exception as exc:
            result["status"] = "unreadable"
            result["error"] = str(exc)
        return result

    def _project_hybrid_vector_query_items(
        self,
        conn: sqlite3.Connection,
        query: str,
        limit: int,
        profile: dict[str, Any],
        allow_external_embedding: bool,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        records = self._paperwise_vector_records(conn)
        if not records:
            return []
        corpus_tokens = [_bm25_tokens(record["search_text"]) for record in records]
        bm25 = BM25Okapi(corpus_tokens)
        query_tokens = _bm25_tokens(query)
        bm25_scores = [float(score) for score in bm25.get_scores(query_tokens)]
        bm25_indices = sorted(
            range(len(records)),
            key=lambda index: (-bm25_scores[index], records[index]["embedding_id"]),
        )[:BM25_RECALL_TOP_K]

        embedding_scores: dict[int, float] = {}
        embedding_indices: list[int] = []
        query_vector: list[float] | None = None
        if allow_external_embedding:
            settings = load_settings(PROJECT_ROOT / "config.yaml")
            model_name = os.getenv("PAPERWISE_HYBRID_EMBEDDING_MODEL") or "text-embedding-v3"
            base_url = os.getenv("PAPERWISE_HYBRID_EMBEDDING_BASE_URL") or settings.embedding_base_url
            api_key_env = os.getenv("PAPERWISE_HYBRID_EMBEDDING_API_KEY_ENV") or settings.embedding_api_key_env
            query_vector = self._embed_external_texts(
                [query],
                model_name=model_name,
                base_url=base_url,
                api_key_env=api_key_env,
                dimensions=settings.embedding_dimensions,
            )[0]
            record_vectors = self._paperwise_record_embeddings(
                records,
                model_name=model_name,
                base_url=base_url,
                api_key_env=api_key_env,
                dimensions=settings.embedding_dimensions,
            )
            embedding_scores = {
                index: _cosine(query_vector, record_vectors.get(records[index]["cache_key"], []))
                for index in range(len(records))
            }
            embedding_indices = sorted(
                range(len(records)),
                key=lambda index: (-embedding_scores.get(index, 0.0), records[index]["embedding_id"]),
            )[:EMBEDDING_RECALL_TOP_K]

        candidate_indices = sorted(set([*bm25_indices, *embedding_indices]))
        candidates: list[dict[str, Any]] = []
        for index in candidate_indices:
            record = records[index]
            clean = record["text"]
            relevance = self._relevance(clean, profile)
            semantic_score = embedding_scores.get(index)
            if semantic_score is not None and semantic_score < PAPERWISE_HYBRID_MIN_SCORE:
                continue
            score = semantic_score if semantic_score is not None else self._normalize_bm25_score(bm25_scores[index], bm25_scores)
            item = self._paperwise_vector_item_from_record(
                record,
                score=score,
                relevance=relevance,
                retrieval_backend=(
                    "project_hybrid_bm25_50_qwen_15_semantic_rerank"
                    if allow_external_embedding
                    else "project_bm25_sqlite_fallback"
                ),
                bm25_score=bm25_scores[index],
                coarse_sources=[
                    *("bm25_top50" for _ in [0] if index in bm25_indices),
                    *("embedding_top15" for _ in [0] if index in embedding_indices),
                ],
            )
            candidates.append(item)
        candidates.sort(key=lambda item: (-float(item.get("score", 0.0)), str(item.get("paper_id") or ""), str(item.get("path") or "")))
        return self._dedupe_papers(candidates, limit)

    def _paperwise_vector_records(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT rowid, c0 FROM embedding_fulltext_search_content "
            "WHERE length(c0) >= 120 ORDER BY rowid"
        ).fetchall()
        records: list[dict[str, Any]] = []
        for rowid, text in rows:
            clean = " ".join(str(text).split())
            metadata = self._vector_metadata_for_embedding(conn, int(rowid))
            embedding_id = metadata.get("embedding_id") or f"row:{rowid}"
            paper_id = metadata.get("paper_id") or metadata.get("arxiv_id") or embedding_id.split("::", 1)[0]
            paper_title = metadata.get("title") or clean.lstrip("# ").split("**", 1)[0][:120] or f"PaperWise chunk {rowid}"
            search_text = f"{paper_title} {metadata.get('source', '')} {clean[:1600]}"
            records.append(
                {
                    "rowid": int(rowid),
                    "text": clean,
                    "search_text": search_text,
                    "paper_id": paper_id,
                    "paper_title": paper_title,
                    "source": metadata.get("source"),
                    "embedding_id": embedding_id,
                    "cache_key": _embedding_cache_key(embedding_id, search_text),
                }
            )
        return records

    def _paperwise_record_embeddings(
        self,
        records: list[dict[str, Any]],
        model_name: str,
        base_url: str,
        api_key_env: str,
        dimensions: int | None,
    ) -> dict[str, list[float]]:
        cache_path = self._paperwise_embedding_cache_path(model_name)
        cached = self._read_embedding_cache(cache_path)
        missing = [record for record in records if record["cache_key"] not in cached]
        if missing:
            vectors = self._embed_external_texts(
                [record["search_text"] for record in missing],
                model_name=model_name,
                base_url=base_url,
                api_key_env=api_key_env,
                dimensions=dimensions,
            )
            for record, vector in zip(missing, vectors):
                cached[record["cache_key"]] = vector
            self._write_embedding_cache(cache_path, cached)
        return cached

    def _paperwise_embedding_cache_path(self, model_name: str) -> Path:
        return PROJECT_ROOT / "workspace" / "cache" / f"paperwise_hybrid_embeddings_{_cache_safe_model_name(model_name)}.json"

    def _read_embedding_cache(self, path: Path) -> dict[str, list[float]]:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict) or data.get("schema_version") != "1.0":
            return {}
        vectors = data.get("embeddings")
        if not isinstance(vectors, dict):
            return {}
        return {
            str(key): [float(value) for value in vector]
            for key, vector in vectors.items()
            if isinstance(vector, list)
        }

    def _write_embedding_cache(self, path: Path, embeddings: dict[str, list[float]]) -> None:
        atomic_write_json(
            path,
            {
                "schema_version": "1.0",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "source": "project_paperwise_hybrid_retrieval",
                "embedding_count": len(embeddings),
                "embeddings": embeddings,
            },
        )

    def _embed_external_texts(
        self,
        texts: list[str],
        model_name: str,
        base_url: str,
        api_key_env: str,
        dimensions: int | None,
    ) -> list[list[float]]:
        api_key = os.getenv(api_key_env)
        if not api_key and api_key_env != "QWEN_API_KEY":
            api_key = os.getenv("QWEN_API_KEY")
        if not api_key:
            raise RuntimeError(f"missing embedding API key: set {api_key_env}")
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)
        vectors: list[list[float]] = []
        for index in range(0, len(texts), 10):
            batch = texts[index : index + 10]
            payload: dict[str, Any] = {
                "model": model_name,
                "input": batch,
                "encoding_format": "float",
            }
            if dimensions is not None:
                payload["dimensions"] = dimensions
            response = client.embeddings.create(**payload)
            vectors.extend(normalize_vector([float(value) for value in item.embedding]) for item in response.data)
        return vectors

    def _normalize_bm25_score(self, score: float, all_scores: list[float]) -> float:
        max_score = max(all_scores) if all_scores else 0.0
        if max_score <= 0:
            return 0.0
        return float(score) / float(max_score)

    def _paperwise_vector_item_from_record(
        self,
        record: dict[str, Any],
        score: float,
        relevance: dict[str, Any],
        retrieval_backend: str,
        bm25_score: float,
        coarse_sources: list[str],
    ) -> dict[str, Any]:
        paper_id = str(record.get("paper_id") or "")
        paper_title = str(record.get("paper_title") or paper_id or "PaperWise paper")
        text = str(record.get("text") or "")
        return {
            "source": "paperwise_vector_library_project_hybrid",
            "title": paper_title,
            "display_title": self._display_title_from_text(text, fallback=paper_title),
            "original_title": paper_title,
            "path": f"embedding_fulltext_search_content:{record.get('rowid')}",
            "paper_id": paper_id or None,
            "paper_title": paper_title,
            "paper_path": self._report_path_for_kb_entry(paper_id, paper_title),
            "chunk_source": record.get("source"),
            "embedding_id": record.get("embedding_id"),
            "snippet": text[:500],
            "level": "semantic_evidence",
            "status": "available",
            "roles": ["reproduction", "evidence"],
            "score": round(float(score), 6),
            "bm25_score": round(float(bm25_score), 6),
            "coarse_sources": coarse_sources,
            "retrieval_backend": retrieval_backend,
            "relevance": relevance,
        }

    def _dedupe_papers(self, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            key = str(item.get("paper_id") or item.get("paper_path") or item.get("path"))
            if key in seen:
                continue
            seen.add(key)
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected

    def _vector_query_error(self, exc: Exception) -> str:
        message = str(exc).strip()
        lowered = message.lower()
        if "api key" in lowered or "apikey" in lowered or "unauthorized" in lowered:
            return f"qwen_embedding_api_key_error:{message}"
        if "collection" in lowered:
            return f"paperwise_chroma_query_error:{message}"
        return f"paperwise_kb_store_query_failed:{message}"

    def _report_path_for_kb_entry(self, paper_id: str, title: str) -> str:
        outputs = self.root / "outputs"
        if paper_id:
            matches = sorted(outputs.glob(f"*{paper_id}*/report.md"))
            if matches:
                return str(matches[0])
        title_terms = [term for term in re.findall(r"[A-Za-z0-9]+", str(title or "").lower()) if len(term) >= 4][:4]
        if title_terms:
            for path in outputs.glob("*/report.md"):
                lower = str(path.parent.name).lower()
                if all(term in lower for term in title_terms[:2]):
                    return str(path)
        return "paperwise_kb"

    def _graph_library_summary(self, query: str, limit: int, profile: dict[str, Any]) -> dict[str, Any]:
        graph_path = self.root / "outputs" / "graph" / "graph.json"
        result: dict[str, Any] = {
            "status": "missing",
            "path": str(graph_path),
            "roles": ["innovation", "relation"],
            "description": "PaperWise graph library stores paper, concept, structure, metric, and relation candidates. It is a candidate pool for innovation and still needs LLM semantic review plus gate/reviewer adoption.",
            "support_level": "insufficient_evidence",
            "items": [],
            "node_count": 0,
            "relation_count": 0,
            "sample_relations": [],
            "read_only": True,
        }
        if not graph_path.is_file():
            graph_files = sorted((self.root / "outputs").glob("*/graph_info.json"))
            if graph_files:
                result.update(self._graph_info_summary(graph_files, query, limit, profile))
            return result
        try:
            data = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception as exc:
            result["status"] = "unreadable"
            result["error"] = str(exc)
            return result
        nodes = data.get("nodes") if isinstance(data, dict) else []
        edges = data.get("edges") if isinstance(data, dict) else []
        result["node_count"] = len(nodes) if isinstance(nodes, list) else 0
        result["relation_count"] = len(edges) if isinstance(edges, list) else 0
        candidate_edges = self._collect_graph_edges_with_node_labels(nodes if isinstance(nodes, list) else [], edges if isinstance(edges, list) else [], profile)
        result["matched_relation_count"] = len(candidate_edges)
        result["sample_relations"] = candidate_edges[:limit]
        result["status"] = "available" if result["matched_relation_count"] else "insufficient_evidence"
        result["support_level"] = "candidate_relation_pool_llm_required" if result["status"] == "available" else "insufficient_evidence"
        result["review_requirement"] = "llm_semantic_review_required_before_adoption"
        result["items"] = self._relation_items(result["sample_relations"], str(graph_path), limit)
        if result["relation_count"] and not result["items"]:
            result["status"] = "insufficient_evidence"
            result["support_level"] = "insufficient_evidence"
            result["reason"] = "graph library has relations but none match the task query"
        elif result["status"] != "available":
            result["reason"] = "graph library has no relation edges"
        return result

    def _graph_info_summary(self, graph_files: list[Path], query: str, limit: int, profile: dict[str, Any]) -> dict[str, Any]:
        matched_relation_count = 0
        total_relation_count = 0
        concepts: set[str] = set()
        samples = []
        for path in graph_files[:200]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for concept in data.get("concepts") or []:
                concepts.add(str(concept))
            raw_relations = data.get("relations") or []
            total_relation_count += len(raw_relations)
            concept_text = " ".join(str(concept) for concept in data.get("concepts") or [])
            relations = self._filter_relations(raw_relations, query, profile, context=concept_text)
            matched_relation_count += len(relations)
            for relation in relations:
                if len(samples) < limit:
                    samples.append({"path": str(path), **relation})
        return {
            "status": "available" if matched_relation_count else "insufficient_evidence",
            "support_level": "candidate_relation_pool_llm_required" if matched_relation_count else "insufficient_evidence",
            "path": str(graph_files[0].parent.parent),
            "node_count": len(concepts),
            "relation_count": total_relation_count,
            "matched_relation_count": matched_relation_count,
            "sample_relations": samples,
            "items": self._relation_items(samples, str(graph_files[0].parent.parent), limit),
            "reason": None if matched_relation_count else "graph library has no relation evidence matching the task query",
            "graph_info_files": len(graph_files),
            "review_requirement": "llm_semantic_review_required_before_adoption",
        }

    def _sqlite_count(self, conn: sqlite3.Connection, table: str) -> int:
        try:
            return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        except Exception:
            return 0

    def _vector_paper_count(self, conn: sqlite3.Connection) -> int:
        try:
            rows = conn.execute(
                "SELECT DISTINCT embedding_id FROM embeddings WHERE embedding_id IS NOT NULL"
            ).fetchall()
        except Exception:
            return 0
        values = {str(row[0]).split("::", 1)[0] for row in rows if row and row[0]}
        return len(values)

    def _vector_query_items(self, conn: sqlite3.Connection, query: str, limit: int, profile: dict[str, Any]) -> list[dict[str, Any]]:
        terms = self._query_terms(query)
        search_terms = list(dict.fromkeys([*profile.get("search_terms", []), *terms]))
        if not search_terms:
            return []
        clauses = " OR ".join("c0 LIKE ?" for _ in search_terms[:10])
        params = [f"%{term}%" for term in search_terms[:10]]
        try:
            rows = conn.execute(
                f"SELECT rowid, c0 FROM embedding_fulltext_search_content WHERE {clauses} LIMIT ?",
                [*params, max(limit * 8, 80)],
            ).fetchall()
        except Exception:
            return []
        items = []
        for rowid, text in rows:
            clean = " ".join(str(text).split())
            relevance = self._relevance(clean, profile)
            if not relevance["accepted"]:
                continue
            title = clean.lstrip("# ").split("**", 1)[0][:120] or f"PaperWise chunk {rowid}"
            display_title = self._display_title_from_text(clean, fallback=title)
            metadata = self._vector_metadata_for_embedding(conn, int(rowid))
            paper_id = metadata.get("paper_id") or metadata.get("arxiv_id") or metadata.get("embedding_id", "").split("::", 1)[0]
            paper_title = metadata.get("title") or self._display_title_from_text(clean, fallback=title)
            paper_path = self._report_path_for_kb_entry(paper_id, paper_title)
            items.append(
                {
                    "source": "vector_library",
                    "title": title,
                    "display_title": display_title,
                    "original_title": title,
                    "path": f"embedding_fulltext_search_content:{rowid}",
                    "paper_id": paper_id or None,
                    "paper_title": paper_title,
                    "paper_path": paper_path,
                    "chunk_source": metadata.get("source"),
                    "embedding_id": metadata.get("embedding_id"),
                    "snippet": clean[:500],
                    "level": "semantic_evidence",
                    "status": "available",
                    "roles": ["reproduction", "evidence"],
                    "score": relevance["score"],
                    "relevance": relevance,
                }
            )
        return self._tiered_select(items, limit)

    def _vector_metadata_for_embedding(self, conn: sqlite3.Connection, embedding_row_id: int) -> dict[str, str]:
        metadata: dict[str, str] = {}
        try:
            rows = conn.execute(
                "SELECT key, string_value FROM embedding_metadata "
                "WHERE id = ? AND key IN ('paper_id', 'arxiv_id', 'title', 'source', 'path')",
                (embedding_row_id,),
            ).fetchall()
            embedding = conn.execute(
                "SELECT embedding_id FROM embeddings WHERE id = ?",
                (embedding_row_id,),
            ).fetchone()
        except Exception:
            return metadata
        for key, value in rows:
            if key and value:
                metadata[str(key)] = str(value)
        if embedding and embedding[0]:
            metadata["embedding_id"] = str(embedding[0])
        return metadata

    def _relation_items(self, relations: list[Any], default_path: str, limit: int) -> list[dict[str, Any]]:
        items = []
        for relation in relations[:limit]:
            if not isinstance(relation, dict):
                continue
            title = relation.get("target_title") or relation.get("relation") or relation.get("target") or "relation evidence"
            description = relation.get("description") or f"{relation.get('source', '')} {relation.get('relation', '')} {relation.get('target', '')}".strip()
            display_title = (
                relation.get("display_title")
                or relation.get("target_title_zh")
                or relation.get("title_zh")
                or self._display_title_from_text(str(description), fallback=str(title))
            )
            items.append(
                {
                    "source": "graph_library",
                    "title": str(title),
                    "display_title": str(display_title),
                    "original_title": str(title),
                    "path": str(relation.get("path") or default_path),
                    "description": description,
                    "level": "relation_graph",
                    "status": "available",
                    "relation": relation.get("relation"),
                    "roles": ["innovation", "relation"],
                }
            )
        return items

    def _filter_relations(self, relations: list[Any], query: str, profile: dict[str, Any], context: str = "") -> list[dict[str, Any]]:
        terms = self._query_terms(query)
        if not terms and not profile.get("has_constraints"):
            return [relation for relation in relations if isinstance(relation, dict)]
        matched = []
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            text = f"{context} {' '.join(str(value) for value in relation.values())}".lower()
            relevance = self._relevance(text, profile)
            if relevance["accepted"]:
                next_relation = dict(relation)
                next_relation["relevance"] = relevance
                matched.append(next_relation)
        return self._tiered_select(
            [
                {
                    **item,
                    "path": str(item.get("path") or item.get("target_title") or item.get("description") or item),
                    "score": (item.get("relevance") or {}).get("score", 0),
                }
                for item in matched
            ],
            sum(quota for _, quota in MATCH_TIER_QUOTAS),
        )

    def _collect_graph_edges_with_node_labels(self, nodes: list[Any], edges: list[Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
        labels = {}
        for node in nodes:
            if isinstance(node, dict) and node.get("id"):
                labels[str(node.get("id"))] = str(node.get("label") or node.get("id") or "")
        candidates = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            text = " ".join(
                [
                    labels.get(source, source),
                    labels.get(target, target),
                    str(edge.get("type") or edge.get("relation") or ""),
                    str(edge.get("description") or ""),
                ]
            ).lower()
            if any(term in text for term in DISALLOWED_TOPIC_TERMS):
                continue
            relevance = self._partial_relevance(text, profile)
            next_edge = dict(edge)
            next_edge["source_label"] = labels.get(source, source)
            next_edge["target_label"] = labels.get(target, target)
            next_edge["target_title"] = labels.get(target, target)
            next_edge["relation"] = edge.get("type") or edge.get("relation")
            next_edge["description"] = edge.get("description") or f"{labels.get(source, source)} -> {labels.get(target, target)}"
            next_edge["relevance"] = relevance
            next_edge["score"] = relevance.get("score", 0)
            candidates.append(next_edge)
        candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
        return candidates[: sum(quota for _, quota in MATCH_TIER_QUOTAS)]

    def _partial_relevance(self, text: str, profile: dict[str, Any]) -> dict[str, Any]:
        lower = str(text or "").lower()
        if any(term in lower for term in DISALLOWED_TOPIC_TERMS):
            return {"accepted": False, "score": 0.0, "reason": "excluded_non_antenna_topic", "matched": {}}
        _, structures = self._contains_any_alias(lower, profile.get("structures", []), STRUCTURE_GROUPS)
        _, objectives = self._contains_any_alias(lower, profile.get("objectives", []), OBJECTIVE_GROUPS)
        _, algorithms = self._contains_any_alias(lower, profile.get("algorithms", []), ALGORITHM_GROUPS)
        parameter_count = self._extract_parameter_count(lower)
        parameter_count_match = self._parameter_count_matches(profile.get("parameter_count"), parameter_count)
        modeling_terms = [term for term in CRITICAL_MODELING_TERMS if term in lower]
        score = 0.0
        if structures:
            score += 0.55
        if objectives:
            score += 0.30
        if algorithms:
            score += 0.15
        if modeling_terms:
            score += min(0.10, len(modeling_terms) * 0.025)
        tier = self._match_tier(profile, structures, objectives, algorithms, parameter_count_match, score)
        accepted = tier != "reject"
        return {
            "accepted": accepted,
            "score": round(score, 3),
            "reason": tier if accepted else "no_requested_graph_term",
            "match_tier": tier,
            "match_percent": self._tier_percent(tier),
            "matched": {
                "structures": structures,
                "objectives": objectives,
                "algorithms": algorithms,
                "parameter_count": parameter_count,
                "parameter_count_match": parameter_count_match,
                "modeling_terms": modeling_terms[:8],
            },
        }

    def _query_terms(self, query: str) -> list[str]:
        raw = re.findall(r"[A-Za-z0-9_+\-]+|[\u4e00-\u9fff]+", query.lower())
        stop = {"real", "single", "run", "test", "verify", "验证", "真实", "单次", "运行", "任务"}
        return [term for term in raw if len(term) >= 2 and term not in stop]

    def _query_profile(self, query: str) -> dict[str, Any]:
        text = str(query or "").lower()
        structures = self._matched_groups(text, STRUCTURE_GROUPS)
        if "mimo" not in structures and self._query_mentions_mimo_antenna(text):
            structures.append("mimo")
        objectives = self._matched_groups(text, OBJECTIVE_GROUPS)
        algorithms = self._matched_groups(text, ALGORITHM_GROUPS)
        parameter_count = self._extract_parameter_count(text)
        search_terms: list[str] = []
        for group_name in [*structures, *objectives, *algorithms]:
            for groups in (STRUCTURE_GROUPS, OBJECTIVE_GROUPS, ALGORITHM_GROUPS):
                search_terms.extend(groups.get(group_name, [])[:3])
        if "patch" in structures:
            search_terms.extend(["patch antenna", "microstrip patch", "贴片", "微带贴片"])
        if "mimo" in structures:
            search_terms.extend(["mimo antenna", "mimo antennas", "多输入多输出天线", "mimo天线"])
        return {
            "schema_version": "1.0",
            "source": "antenna_skills_geometry_and_selector_terms",
            "structures": structures,
            "objectives": objectives,
            "algorithms": algorithms,
            "parameter_count": parameter_count,
            "search_terms": sorted(set(term.lower() for term in search_terms if term)),
            "has_constraints": bool(structures or objectives or algorithms or parameter_count is not None),
            "requires_structure_match": False,
            "requires_objective_match": bool(objectives),
            "requires_algorithm_match": bool(algorithms),
            "requires_parameter_count_match": parameter_count is not None,
            "minimum_score": 0.45 if structures else 0.25,
        }

    def _matched_groups(self, text: str, groups: dict[str, list[str]]) -> list[str]:
        return [name for name, aliases in groups.items() if any(self._alias_in_text(text, alias) for alias in aliases)]

    def _contains_any_alias(self, text: str, group_names: list[str], groups: dict[str, list[str]]) -> tuple[bool, list[str]]:
        matched = []
        for name in group_names:
            if any(self._alias_in_text(text, alias) for alias in groups.get(name, [])):
                matched.append(name)
        return bool(matched), matched

    def _alias_in_text(self, text: str, alias: str) -> bool:
        candidate = str(alias or "").lower()
        if not candidate:
            return False
        if re.fullmatch(r"[a-z0-9]{1,3}", candidate):
            return re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", text) is not None
        return candidate in text

    def _query_mentions_mimo_antenna(self, text: str) -> bool:
        return re.search(r"(?<![a-z0-9])mimo(?![a-z0-9]).{0,24}(antenna|天线)", text) is not None

    def _relevance(self, text: str, profile: dict[str, Any]) -> dict[str, Any]:
        lower = str(text or "").lower()
        if any(term in lower for term in DISALLOWED_TOPIC_TERMS):
            return {"accepted": False, "score": 0.0, "reason": "excluded_non_antenna_topic", "matched": {}}
        structure_ok, structures = self._contains_any_alias(lower, profile.get("structures", []), STRUCTURE_GROUPS)
        objective_ok, objectives = self._contains_any_alias(lower, profile.get("objectives", []), OBJECTIVE_GROUPS)
        algorithm_ok, algorithms = self._contains_any_alias(lower, profile.get("algorithms", []), ALGORITHM_GROUPS)
        parameter_count = self._extract_parameter_count(lower)
        parameter_count_match = self._parameter_count_matches(profile.get("parameter_count"), parameter_count)
        modeling_terms = [term for term in CRITICAL_MODELING_TERMS if term in lower]
        score = 0.0
        if structures:
            score += 0.55
        if objectives:
            score += 0.30
        if algorithms:
            score += 0.15
        if modeling_terms:
            score += min(0.10, len(modeling_terms) * 0.025)
        if not profile.get("has_constraints"):
            return {"accepted": True, "score": 0.0, "reason": "unconstrained_query", "matched": {}}
        tier = self._match_tier(profile, structures, objectives, algorithms, parameter_count_match, score)
        accepted = tier != "reject"
        return {
            "accepted": accepted,
            "score": round(score, 3),
            "reason": tier if accepted else "below_relevance_threshold",
            "match_tier": tier,
            "match_percent": self._tier_percent(tier),
            "matched": {
                "structures": structures,
                "objectives": objectives,
                "algorithms": algorithms,
                "parameter_count": parameter_count,
                "parameter_count_match": parameter_count_match,
                "modeling_terms": modeling_terms[:8],
            },
        }

    def _match_tier(
        self,
        profile: dict[str, Any],
        structures: list[str],
        objectives: list[str],
        algorithms: list[str],
        parameter_count_match: bool,
        score: float,
    ) -> str:
        required = {
            "structures": bool(profile.get("structures")),
            "objectives": bool(profile.get("objectives")),
            "algorithms": bool(profile.get("algorithms")),
            "parameter_count": bool(profile.get("parameter_count") is not None),
        }
        matched = {
            "structures": bool(structures),
            "objectives": bool(objectives),
            "algorithms": bool(algorithms),
            "parameter_count": bool(parameter_count_match),
        }
        requested_count = sum(1 for is_required in required.values() if is_required)
        matched_required_count = sum(1 for name, is_required in required.items() if is_required and matched[name])
        if requested_count and matched_required_count == requested_count:
            return "exact"
        if requested_count >= 2 and matched_required_count >= requested_count - 1:
            return "match_80"
        if requested_count >= 3 and matched_required_count >= requested_count - 2:
            return "match_60"
        if requested_count <= 2 and matched_required_count >= 1:
            return "match_60"
        return "reject"

    def _extract_parameter_count(self, text: str) -> int | None:
        lower = str(text or "").lower()
        patterns = [
            r"(?:parameter|parameters|param|params)\s*(?:count|number|num|数量|个数)?\s*[:=：]?\s*(\d{1,2})",
            r"(\d{1,2})\s*(?:parameter|parameters|param|params)\b",
            r"(\d{1,2})\s*个?\s*(?:参数|变量|优化变量)",
            r"(?:参数|变量|优化变量)\s*(?:数量|个数)?\s*[:=：]?\s*(\d{1,2})",
        ]
        for pattern in patterns:
            match = re.search(pattern, lower)
            if match:
                value = int(match.group(1))
                if 1 <= value <= 99:
                    return value
        return None

    def _parameter_count_matches(self, requested: Any, candidate: Any) -> bool:
        if requested is None or candidate is None:
            return False
        try:
            return abs(int(requested) - int(candidate)) <= 2
        except Exception:
            return False

    def _tier_percent(self, tier: str) -> int:
        return {"exact": 100, "match_80": 80, "match_60": 60}.get(tier, 0)

    def _tiered_select(self, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        target = min(limit, sum(quota for _, quota in MATCH_TIER_QUOTAS))
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tier, quota in MATCH_TIER_QUOTAS:
            bucket = [
                item
                for item in candidates
                if (item.get("relevance") or {}).get("match_tier") == tier
            ]
            bucket.sort(key=lambda item: item.get("score", 0), reverse=True)
            for item in bucket[:quota]:
                key = str(item.get("path") or item.get("title") or item.get("display_title"))
                if key in seen:
                    continue
                seen.add(key)
                selected.append(item)
        if len(selected) < target:
            overflow = [
                item
                for item in sorted(candidates, key=lambda value: value.get("score", 0), reverse=True)
                if str(item.get("path") or item.get("title") or item.get("display_title")) not in seen
            ]
            for item in overflow:
                key = str(item.get("path") or item.get("title") or item.get("display_title"))
                if key in seen:
                    continue
                seen.add(key)
                selected.append(item)
                if len(selected) >= target:
                    break
        return selected[:target]

    def _display_title_for_report(self, report_path: Path, original_title: str, text: str) -> str:
        for sidecar_name in ("feature.json", "meta.json"):
            sidecar = report_path.with_name(sidecar_name)
            if not sidecar.is_file():
                continue
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue
            for key in ("display_title", "title_zh", "chinese_title", "translated_title", "中文标题"):
                value = data.get(key)
                if self._looks_chinese_title(value):
                    return str(value).strip()
        return self._display_title_from_text(text, fallback=original_title)

    def _display_title_from_text(self, text: str, fallback: str) -> str:
        if self._looks_chinese_title(fallback):
            return fallback.strip()
        quoted = re.findall(r"《([^》]{4,120})》", text)
        for value in quoted:
            if self._looks_chinese_title(value):
                return value.strip()
        alias = self._chinese_alias_from_report_text(text)
        if alias:
            return alias
        return self._translate_title(fallback)

    def _clean_chinese_display_title(self, value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" ：:，,。.；;*-")
        text = re.sub(r"[$`#]+", "", text)
        text = re.sub(r"^\d+[\.\、]\s*", "", text)
        text = re.sub(r"^(领域背景|核心痛点|研究动机|论文目标|核心方法|关键创新|重要细节)[：:]\s*", "", text)
        text = text.replace("幅度-only", "仅幅度").replace("幅度-Only", "仅幅度")
        return text[:90].strip()

    def _looks_chinese_title(self, value: Any) -> bool:
        text = self._clean_chinese_display_title(value)
        return bool(text) and len(re.findall(r"[\u4e00-\u9fff]", text)) >= 4

    def _chinese_alias_from_report_text(self, text: str) -> str:
        head = text[:4000]
        candidates = []
        for value in re.findall(r"\*\*([^*\n]{4,80})\*\*", head):
            cleaned = self._clean_chinese_display_title(value)
            if self._is_good_chinese_alias(cleaned):
                candidates.append(cleaned)
        for pattern in (
            r"属于\*\*([^*]{4,60})\*\*",
            r"聚焦于\*\*([^*]{4,60})\*\*",
            r"具体问题是([^。；\n]{6,50})",
            r"具体聚焦于([^。；\n]{6,50})",
        ):
            for value in re.findall(pattern, head):
                cleaned = self._clean_chinese_display_title(value)
                if self._is_good_chinese_alias(cleaned):
                    candidates.append(cleaned)
        if not candidates:
            return ""
        candidates.sort(key=lambda item: (self._alias_score(item), -len(item)), reverse=True)
        best = candidates[0]
        suffix = "相关论文"
        return best if best.endswith(suffix) else f"{best}{suffix}"

    def _is_good_chinese_alias(self, value: str) -> bool:
        if not self._looks_chinese_title(value):
            return False
        if len(value) > 42:
            return False
        blocked = {"领域背景", "核心痛点", "研究动机", "论文目标", "核心方法", "关键创新", "重要细节", "实现要点"}
        if value in blocked:
            return False
        return any(
            keyword in value
            for keyword in (
                "天线",
                "阵列",
                "微带",
                "贴片",
                "优化",
                "代理",
                "神经网络",
                "机器学习",
                "强化学习",
                "波束",
                "感知",
                "信道",
                "相控阵",
                "回波损耗",
                "参数",
            )
        )

    def _alias_score(self, value: str) -> int:
        score = 0
        for keyword in ("天线", "微带", "贴片", "阵列", "S11", "回波损耗", "优化", "代理模型", "CST"):
            if keyword in value:
                score += 2
        for keyword in ("方法", "框架", "系统", "设计"):
            if keyword in value:
                score += 1
        return score

    def _translate_title(self, title: Any) -> str:
        text = re.sub(r"\s+", " ", str(title or "")).strip()
        if not text:
            return ""
        if self._looks_chinese_title(text):
            return text
        concept = re.match(r"^concept:(.+)$", text, flags=re.IGNORECASE)
        if concept:
            return f"概念：{self._translate_title(concept.group(1))}"
        relation = re.match(r"^relation[:\s-]+(.+)$", text, flags=re.IGNORECASE)
        if relation:
            return f"关系：{self._translate_title(relation.group(1))}"

        normalized = text
        phrase_map = [
            ("Artificial Neural Network Surrogate", "人工神经网络代理模型"),
            ("Artificial Neural Network", "人工神经网络"),
            ("Convolutional Neural Network", "卷积神经网络"),
            ("Neural Network", "神经网络"),
            ("A Novel", "新型"),
            ("Novel", "新型"),
            ("Angle-of-Arrival", "到达角"),
            ("Amplitude-Only", "仅幅度"),
            ("Amplitude-only", "仅幅度"),
            ("Beetle Antennae Search", "天牛须搜索"),
            ("Computerized Tomog", "计算机断层扫描"),
            ("Conditional GANs", "条件生成对抗网络"),
            ("Conditional GAN", "条件生成对抗网络"),
            ("Deep Reinforcement Learning", "深度强化学习"),
            ("Reinforcement Learning", "强化学习"),
            ("Machine Learning", "机器学习"),
            ("CMA-Guided", "CMA引导的"),
            ("Miniaturized", "小型化"),
            ("Terminal Applications", "终端应用"),
            ("Base Station Deployment", "基站部署"),
            ("EMF Aware", "电磁场感知"),
            ("Microstrip Patch Antenna Arrays", "微带贴片天线阵列"),
            ("Microstrip Patch Antenna Array", "微带贴片天线阵列"),
            ("Microstrip Patch Antenna", "微带贴片天线"),
            ("Patch Antennas", "贴片天线"),
            ("Patch Antenna", "贴片天线"),
            ("Antenna Structures", "天线结构"),
            ("Conformal Antenna Array", "共形天线阵列"),
            ("Antenna Arrays", "天线阵列"),
            ("Antenna Array", "天线阵列"),
            ("Antenna Selection", "天线选择"),
            ("U-Slot Microstrip Antenna", "U槽微带天线"),
            ("Response Features", "响应特征"),
            ("Principal Directions", "主方向"),
            ("Parameter Tuning", "参数调优"),
            ("Return Loss", "回波损耗"),
            ("Bandwidth", "带宽"),
            ("Optimization Method", "优化方法"),
            ("Optimization Framework", "优化框架"),
            ("Optimization", "优化"),
            ("Design", "设计"),
            ("Synthesis", "综合"),
            ("Prediction", "预测"),
            ("Sensing", "感知"),
            ("Calibration", "校准"),
            ("Fault Diagnosis", "故障诊断"),
            ("Beamforming", "波束成形"),
            ("Base Station", "基站"),
            ("Fluid Antenna System", "流体天线系统"),
            ("Reconfigurable Intelligent Surface", "可重构智能表面"),
            ("Intelligent Reflecting Surface", "智能反射表面"),
            ("Phased Array", "相控阵"),
            ("Wireless Power Transmission", "无线能量传输"),
            ("by Means of", "通过"),
            ("Based on", "基于"),
            ("Using", "使用"),
            ("Enhanced", "增强的"),
            ("Driven", "驱动"),
            ("AI-Driven", "AI驱动的"),
            ("Accelerated", "加速的"),
            ("Efficient", "高效的"),
            ("Compact", "紧凑型"),
            ("Broadband", "宽带"),
            ("Dual Band", "双频"),
            ("Multi-Stream", "多流"),
            ("Multi-Agent", "多智能体"),
            ("Surrogate", "代理模型"),
            ("Framework", "框架"),
            ("Method", "方法"),
            ("Algorithm", "算法"),
            ("Model", "模型"),
            ("System", "系统"),
            ("Systems", "系统"),
        ]
        for source, target in phrase_map:
            normalized = re.sub(re.escape(source), target, normalized, flags=re.IGNORECASE)
        word_map = {
            "for": "用于",
            "of": "的",
            "and": "与",
            "with": "结合",
            "via": "通过",
            "in": "中的",
            "to": "到",
            "from": "从",
            "a": "",
            "an": "",
            "the": "",
        }
        tokens = re.split(r"(\W+)", normalized)
        normalized = "".join(word_map.get(token.lower(), token) for token in tokens)
        normalized = re.sub(r"\s+", " ", normalized).strip(" ：:-,，")
        normalized = normalized.replace(" 的 ", "的").replace(" 与 ", "与").replace(" 用于 ", "用于")
        if self._looks_chinese_title(normalized):
            return normalized[:120]
        return f"未译名论文：{text[:100]}"
