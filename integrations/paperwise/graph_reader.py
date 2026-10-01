from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import (
    ALGORITHM_GROUPS,
    CRITICAL_MODELING_TERMS,
    DISALLOWED_TOPIC_TERMS,
    MATCH_TIER_QUOTAS,
    OBJECTIVE_GROUPS,
    STRUCTURE_GROUPS,
)


class PaperWiseGraphReaderMixin:
    def graph_library_summary(self, query: str = "", limit: int = 20) -> dict[str, Any]:
        """Return a read-only view of the existing paper-derived PaperWise graph."""
        return self._graph_library_summary(
            query=query,
            limit=limit,
            profile=self._query_profile(query),
        )

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
