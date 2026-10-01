from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from integrations.paperwise import (
    PaperWiseGraphReaderMixin,
    PaperWiseQueryProfileMixin,
    PaperWiseReportsMixin,
    PaperWiseVectorStoreMixin,
)


class PaperWiseAdapter(
    PaperWiseReportsMixin,
    PaperWiseVectorStoreMixin,
    PaperWiseGraphReaderMixin,
    PaperWiseQueryProfileMixin,
):
    """Read-only facade over modular PaperWise report, vector, graph, and query components."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

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
