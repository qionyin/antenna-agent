from __future__ import annotations

import json
from typing import Any

from ..runtime_llm_config import runtime_llm_config
from ..utils import now_iso, stable_hash


class EvidenceReviewServiceMixin:
    def _review_paperwise_evidence(
        self,
        query: str,
        evidence_pool: dict[str, Any],
        allow_runtime_llm: bool = False,
    ) -> dict[str, Any]:
        reviewed = dict(evidence_pool)
        profile = reviewed.get("relevance_profile") or self.paperwise._query_profile(query)
        if allow_runtime_llm:
            llm_result = self._runtime_llm_review_paperwise_evidence(query, profile, evidence_pool)
        else:
            llm_result = {
                "reviews": {},
                "expanded_candidates": [],
                "llm_review": {
                    "attempted": False,
                    "success": False,
                    "error": "external_llm_not_approved",
                    "model": runtime_llm_config.model_name,
                    "base_url": runtime_llm_config.base_url,
                },
            }
        llm_reviews = llm_result.get("reviews", {})
        llm_review_status = llm_result.get("llm_review", {})
        llm_expanded_candidates = list(llm_result.get("expanded_candidates") or [])
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        uncertain: list[dict[str, Any]] = []
        reviewed_sources: dict[str, Any] = {}
        gate_items: list[dict[str, Any]] = []

        for source_name, source in (reviewed.get("sources") or {}).items():
            next_source = dict(source)
            items = list(source.get("items") or [])
            next_items = []
            for item in items:
                item_key = str(item.get("path") or item.get("title") or item.get("display_title") or "")
                review = llm_reviews.get(item_key)
                if review is None and source_name == "graph_library":
                    review = self._llm_required_graph_evidence_review(query, item, llm_review_status)
                if review is None:
                    review = self._fallback_react_evidence_review(query, profile, source_name, item)
                next_item = dict(item)
                next_item["react_review"] = review
                gate = self._evidence_modeling_gate_review(query, profile, source_name, next_item, review, llm_review_status)
                next_item["evidence_gate"] = gate
                next_items.append(next_item)
                gate_items.append(gate)
                decision = self._gate_bucket_decision(review, gate)
                if decision == "accept":
                    accepted.append(next_item)
                elif decision == "reject":
                    rejected.append(next_item)
                else:
                    uncertain.append(next_item)
            next_source["items"] = next_items
            next_source["accepted_count"] = sum(1 for item in next_items if item.get("evidence_gate", {}).get("adoption_decision") == "adopt")
            next_source["rejected_count"] = sum(1 for item in next_items if item.get("evidence_gate", {}).get("adoption_decision") == "block")
            next_source["uncertain_count"] = len(next_items) - next_source["accepted_count"] - next_source["rejected_count"]
            reviewed_sources[source_name] = next_source

        if llm_expanded_candidates:
            expansion_items = []
            for item in llm_expanded_candidates:
                review = {
                    "schema_version": "1.0",
                    "review_style": "react",
                    "thought": f"外接 LLM 为任务补充候选：{query}",
                    "action": "runtime_llm_semantic_expand_candidate",
                    "observation": {"source": "llm_semantic_expansion", "matched": {}},
                    "decision": "uncertain",
                    "match_tier": "match_60",
                    "match_percent": 60,
                    "reason": item.get("description") or "LLM 语义扩展候选，尚未绑定到 PaperWise 论文证据。",
                    "roles": ["semantic_expansion"],
                    "data_egress": True,
                }
                next_item = dict(item)
                next_item["react_review"] = review
                gate = self._evidence_modeling_gate_review(query, profile, "llm_semantic_expansion", next_item, review, llm_review_status)
                next_item["evidence_gate"] = gate
                expansion_items.append(next_item)
                gate_items.append(gate)
                uncertain.append(next_item)
            reviewed_sources["llm_semantic_expansion"] = {
                "status": "candidate_only",
                "roles": ["semantic_expansion", "reason_explanation"],
                "description": "外接 LLM 只做语义补充、候选扩展和理由解释；没有绑定 PaperWise 论文证据前不能被采用。",
                "support_level": "blocked_until_paperwise_evidence",
                "review_requirement": "must_bind_to_paperwise_report_vector_or_graph_before_adoption",
                "read_only": True,
                "items": expansion_items,
                "count": len(expansion_items),
                "accepted_count": 0,
                "rejected_count": sum(1 for item in expansion_items if item.get("evidence_gate", {}).get("adoption_decision") == "block"),
                "uncertain_count": sum(1 for item in expansion_items if item.get("evidence_gate", {}).get("adoption_decision") != "block"),
            }

        evidence_gaps = self._evidence_review_gaps(profile, accepted)
        reviewed["sources"] = reviewed_sources
        reviewed["review_agent"] = "evidence_relevance_review_agent"
        has_graph_items = bool((reviewed.get("sources") or {}).get("graph_library", {}).get("items"))
        if llm_reviews:
            reviewed["review_mode"] = "runtime_llm_react"
        elif has_graph_items:
            reviewed["review_mode"] = "local_react_fallback_with_graph_llm_required"
        else:
            reviewed["review_mode"] = "local_react_fallback"
        reviewed["accelerator_policy"] = {
            "schema_version": "1.0",
            "reports_and_deep_reading": "local hard constraints plus reproducible ranking; runtime LLM may supplement reasons and expansion.",
            "deep_read_papers": "local hard constraints plus reproducible ranking; vector chunks are traced back to source papers before review.",
            "graph_library": "candidate relation pool for innovation; runtime LLM semantic review is required before adoption.",
            "final_decider": "evidence_modeling_gate_agent and reviewer, not local matching or LLM alone.",
            "gate_checks": [
                "has_paper_evidence",
                "em_logic_ok",
                "parameter_modelable",
                "optimization_range_reasonable",
                "feed_port_boundary_safe",
            ],
        }
        reviewed["llm_review"] = llm_review_status
        reviewed["gate_summary"] = self._evidence_gate_summary(gate_items)
        reviewed["react_review"] = {
            "accepted": [self._compact_evidence_review_item(item) for item in accepted],
            "rejected": [self._compact_evidence_review_item(item) for item in rejected],
            "uncertain": [self._compact_evidence_review_item(item) for item in uncertain],
            "evidence_gaps": evidence_gaps,
        }
        reviewed["status"] = "available" if accepted else "insufficient_evidence"
        reviewed["support_level"] = "reviewed_candidate_evidence" if accepted else "insufficient_evidence"
        reviewed["evidence_level"] = "paperwise_reviewed" if accepted else "insufficient_evidence"
        reviewed["insufficiencies"] = evidence_gaps
        return reviewed

    def _gate_bucket_decision(self, review: dict[str, Any], gate: dict[str, Any]) -> str:
        if gate.get("adoption_decision") == "adopt":
            return "accept"
        if gate.get("adoption_decision") == "block":
            return "reject"
        return "uncertain"

    def _evidence_gate_summary(self, gates: list[dict[str, Any]]) -> dict[str, Any]:
        decisions = [str(gate.get("adoption_decision") or "unknown") for gate in gates]
        blocker_counts: dict[str, int] = {}
        warning_counts: dict[str, int] = {}
        for gate in gates:
            for blocker in gate.get("blockers") or []:
                blocker_counts[str(blocker)] = blocker_counts.get(str(blocker), 0) + 1
            for warning in gate.get("warnings") or []:
                warning_counts[str(warning)] = warning_counts.get(str(warning), 0) + 1
        return {
            "schema_version": "1.0",
            "total": len(gates),
            "adopted": decisions.count("adopt"),
            "blocked": decisions.count("block"),
            "needs_more_evidence": decisions.count("needs_more_evidence"),
            "blockers": blocker_counts,
            "warnings": warning_counts,
            "applies_to_sources": ["reports", "deep_read_papers", "graph_library", "llm_semantic_expansion"],
            "gate_agent": "evidence_modeling_gate_agent",
        }

    def _evidence_modeling_gate_review(
        self,
        query: str,
        profile: dict[str, Any],
        source_name: str,
        item: dict[str, Any],
        review: dict[str, Any],
        llm_status: dict[str, Any],
    ) -> dict[str, Any]:
        text = " ".join(
            str(item.get(key) or "")
            for key in ("display_title", "title", "original_title", "description", "snippet", "path", "relation")
        ).lower()
        matched = (item.get("relevance") or {}).get("matched") or (review.get("observation") or {}).get("matched") or {}
        roles = set(review.get("roles") or [])
        review_decision = str(review.get("decision") or "uncertain").lower()
        has_paper_evidence = bool(item.get("path") and (item.get("description") or item.get("snippet") or item.get("title")))
        relevance_review_ok = review_decision != "reject"
        em_logic_ok = bool(matched.get("structures") or "structure" in roles or profile.get("structures"))
        parameter_modelable = bool(matched.get("modeling_terms") or source_name in {"reports", "deep_read_papers"})
        optimization_range_reasonable = bool(matched.get("objectives") or "metric" in roles or profile.get("objectives"))
        breaks_feed_port_boundary = any(
            token in text
            for token in (
                "break feed",
                "broken feed",
                "port mismatch",
                "boundary error",
                "invalid boundary",
                "floating ground",
            )
        )
        if source_name == "graph_library":
            semantic_review_ready = bool(llm_status.get("success") and review.get("decision") == "accept")
        elif source_name == "llm_semantic_expansion":
            semantic_review_ready = bool(llm_status.get("success"))
        else:
            semantic_review_ready = True
        checks = {
            "has_paper_evidence": has_paper_evidence,
            "relevance_review_ok": relevance_review_ok,
            "em_logic_ok": em_logic_ok,
            "parameter_modelable": parameter_modelable,
            "optimization_range_reasonable": optimization_range_reasonable,
            "feed_port_boundary_safe": not breaks_feed_port_boundary,
            "semantic_review_ready": semantic_review_ready,
        }
        blockers = [name for name, ok in checks.items() if not ok and name in {"has_paper_evidence", "relevance_review_ok", "em_logic_ok", "feed_port_boundary_safe", "semantic_review_ready"}]
        warnings = [name for name, ok in checks.items() if not ok and name not in blockers]
        if blockers:
            adoption = "block"
        elif warnings:
            adoption = "needs_more_evidence"
        else:
            adoption = "adopt"
        return {
            "schema_version": "1.0",
            "gate_agent": "evidence_modeling_gate_agent",
            "source_skill": r"C:\Users\30626\.codex\skills\Antenna Skills\antenna-research-ideation",
            "basis": "Applies antenna-research-ideation evidence gates to PaperWise reports, deep-read papers traced from vector chunks, graph relations, and LLM semantic expansions: paper evidence, EM logic, modelable parameters, reasonable optimization range, feed/port/boundary safety.",
            "adoption_decision": adoption,
            "checks": checks,
            "blockers": blockers,
            "warnings": warnings,
            "reason": self._evidence_modeling_gate_reason(query, source_name, adoption, blockers, warnings),
        }

    def _evidence_modeling_gate_reason(self, query: str, source_name: str, adoption: str, blockers: list[str], warnings: list[str]) -> str:
        if adoption == "adopt":
            return f"{source_name} candidate can be adopted for {query}: paper-backed evidence and modeling-safety gates are satisfied."
        if blockers:
            return f"{source_name} candidate blocked before adoption: {', '.join(blockers)}."
        return f"{source_name} candidate remains usable as evidence but needs confirmation before adoption: {', '.join(warnings)}."

    def _llm_required_graph_evidence_review(self, query: str, item: dict[str, Any], llm_status: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "review_style": "react",
            "thought": f"graph relation evidence for {query} requires semantic LLM review before adoption",
            "action": "require_runtime_llm_for_graph_evidence_review",
            "observation": {
                "source": "graph_library",
                "llm_attempted": bool(llm_status.get("attempted")),
                "llm_success": bool(llm_status.get("success")),
                "llm_error": llm_status.get("error"),
            },
            "decision": "uncertain",
            "match_tier": "llm_required",
            "match_percent": 0,
            "reason": "图谱关系只能作为候选池；当前 LLM 未成功返回，所以不采用图谱候选，只保留为待语义审查证据。",
            "roles": ["innovation", "relation"],
            "data_egress": False,
        }

    def _runtime_llm_review_paperwise_evidence(
        self,
        query: str,
        profile: dict[str, Any],
        evidence_pool: dict[str, Any],
    ) -> dict[str, Any]:
        if not runtime_llm_config.enabled or not runtime_llm_config.base_url or not runtime_llm_config.api_key or not runtime_llm_config.model_name:
            return {
                "reviews": {},
                "llm_review": {
                    "attempted": False,
                    "success": False,
                    "error": "runtime_llm_not_configured",
                    "model": runtime_llm_config.model_name,
                },
            }
        candidates = []
        for source_name, source in (evidence_pool.get("sources") or {}).items():
            for item in list(source.get("items") or [])[:10]:
                key = str(item.get("path") or item.get("title") or item.get("display_title") or "")
                candidates.append(
                    {
                        "key": key,
                        "source": source_name,
                        "title": item.get("display_title") or item.get("title"),
                        "original_title": item.get("original_title"),
                        "description": item.get("description") or item.get("snippet") or "",
                        "relevance": item.get("relevance") or {},
                        "match_tier": (item.get("relevance") or {}).get("match_tier"),
                        "match_percent": (item.get("relevance") or {}).get("match_percent"),
                    }
                )
        if not candidates:
            return {
                "reviews": {},
                "llm_review": {
                    "attempted": False,
                    "success": False,
                    "error": "no_paperwise_candidates",
                    "model": runtime_llm_config.model_name,
                },
            }
        prompt = {
            "task": query,
            "profile": profile,
            "instruction": (
                "Review each PaperWise candidate for antenna-task relevance. "
                "Sources include PaperWise deep-reading reports, deep-read papers traced from vector-library chunks, graph-library relations, and LLM semantic expansion candidates. "
                "The LLM may provide semantic supplement, candidate expansion, and Chinese reasons, but final adoption is decided later by evidence_modeling_gate_agent. "
                "Return strict JSON: {\"reviews\":[{\"key\":\"...\",\"decision\":\"accept|reject|uncertain\","
                "\"match_tier\":\"exact|match_80|match_60|reject\","
                "\"reason\":\"Chinese reason\",\"roles\":[\"structure|metric|algorithm|reproduction|innovation\"]}],"
                "\"expanded_candidates\":[{\"title\":\"...\",\"reason\":\"Chinese reason\",\"evidence_hint\":\"source title or concept\"}]}. "
                "Use exact only when all requested antenna type, metric, and algorithm match. "
                "Use match_80 or match_60 for partial-but-useful candidates, keeping the requested antenna type as a hard gate. "
                "Do not treat semantic guesses as adopted evidence unless they are backed by PaperWise report/vector/graph sources."
            ),
            "candidates": candidates,
        }
        try:
            from openai import OpenAI

            client = OpenAI(api_key=runtime_llm_config.api_key, base_url=runtime_llm_config.base_url, timeout=8)
            response = client.chat.completions.create(
                model=runtime_llm_config.model_name,
                temperature=0,
                messages=[
                    {"role": "system", "content": "You are an antenna evidence relevance reviewer. Return strict JSON only."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
            )
            raw = response.choices[0].message.content or "{}"
            parsed = json.loads(raw)
        except Exception as exc:
            return {
                "reviews": {},
                "llm_review": {
                    "attempted": True,
                    "success": False,
                    "error": str(exc),
                    "model": runtime_llm_config.model_name,
                    "base_url": runtime_llm_config.base_url,
                },
            }
        reviews = {}
        for review in parsed.get("reviews") or []:
            key = str(review.get("key") or "")
            decision = str(review.get("decision") or "uncertain").lower()
            if decision not in {"accept", "reject", "uncertain"}:
                decision = "uncertain"
            match_tier = str(review.get("match_tier") or "match_60").lower()
            if match_tier not in {"exact", "match_80", "match_60", "reject"}:
                match_tier = "match_60"
            if match_tier == "reject":
                decision = "reject"
            reviews[key] = {
                "schema_version": "1.0",
                "review_style": "react",
                "thought": f"目标是判断候选是否服务于任务：{query}",
                "action": "runtime_llm_inspect_candidate",
                "observation": {"source": "runtime_frontend_llm", "model": runtime_llm_config.model_name},
                "decision": decision,
                "match_tier": match_tier,
                "match_percent": {"exact": 100, "match_80": 80, "match_60": 60, "reject": 0}[match_tier],
                "reason": str(review.get("reason") or "LLM 未给出明确原因。"),
                "roles": list(review.get("roles") or []),
                "data_egress": True,
            }
        expanded_candidates = []
        for candidate in parsed.get("expanded_candidates") or []:
            expanded_candidates.append(
                {
                    "schema_version": "1.0",
                    "source": "llm_semantic_expansion",
                    "title": str(candidate.get("title") or "LLM semantic expansion"),
                    "description": str(candidate.get("reason") or ""),
                    "evidence_hint": str(candidate.get("evidence_hint") or ""),
                    "status": "candidate_only",
                    "adoption_decision": "blocked_until_paperwise_evidence",
                }
            )
        return {
            "reviews": reviews,
            "llm_review": {
                "attempted": True,
                "success": True,
                "error": None,
                "model": runtime_llm_config.model_name,
                "base_url": runtime_llm_config.base_url,
                "candidate_count": len(candidates),
                "review_count": len(reviews),
                "expanded_candidate_count": len(expanded_candidates),
            },
            "expanded_candidates": expanded_candidates,
        }

    def _fallback_react_evidence_review(self, query: str, profile: dict[str, Any], source_name: str, item: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(
            str(item.get(key) or "")
            for key in ("display_title", "title", "original_title", "description", "snippet", "path", "relation")
        ).lower()
        relevance = item.get("relevance") or self.paperwise._relevance(text, profile)
        matched = relevance.get("matched") or {}
        has_structure = bool(matched.get("structures"))
        has_objective = bool(matched.get("objectives"))
        has_algorithm = bool(matched.get("algorithms"))
        has_parameter_count = bool(matched.get("parameter_count_match"))
        match_tier = relevance.get("match_tier")
        rejected_reason = ""
        if relevance.get("reason") == "excluded_non_antenna_topic":
            decision = "reject"
            rejected_reason = "明显属于非目标天线任务或医学/成像噪声。"
        elif match_tier == "reject" or not relevance.get("accepted"):
            decision = "reject"
            rejected_reason = "四项匹配（天线类型、指标、算法、参数数量）命中不足。"
        elif source_name == "graph_library" and (has_structure or has_objective or has_algorithm or has_parameter_count):
            decision = "accept"
            rejected_reason = "图谱关系可作为创新/关系候选证据，但需和论文/向量证据交叉确认。"
        elif match_tier == "exact":
            decision = "accept"
            rejected_reason = "四项匹配完全命中或已满足全部请求项。"
        elif match_tier in {"match_80", "match_60"}:
            decision = "uncertain"
            rejected_reason = "只命中部分任务要素，需要人工或 LLM 进一步判断。"
        else:
            decision = "reject"
            rejected_reason = "与当前任务缺少可解释关联。"
        roles = []
        if has_structure:
            roles.append("structure")
        if has_objective:
            roles.append("metric")
        if has_algorithm:
            roles.append("algorithm")
        if has_parameter_count:
            roles.append("parameter_count")
        if source_name == "reports":
            roles.append("reproduction")
        if source_name == "graph_library":
            roles.append("innovation")
        return {
            "schema_version": "1.0",
            "review_style": "react",
            "thought": f"目标是判断候选是否服务于任务：{query}",
            "action": "inspect_candidate_title_snippet_relation_and_matched_terms",
            "observation": {
                "source": source_name,
                "matched": matched,
                "score": relevance.get("score", 0),
                "reason": relevance.get("reason"),
            },
            "decision": decision,
            "match_tier": relevance.get("match_tier"),
            "match_percent": relevance.get("match_percent"),
            "reason": rejected_reason,
            "roles": roles,
            "data_egress": False,
        }

    def _compact_evidence_review_item(self, item: dict[str, Any]) -> dict[str, Any]:
        review = item.get("react_review") or {}
        return {
            "source": item.get("source"),
            "title": item.get("title"),
            "display_title": item.get("display_title"),
            "path": item.get("path"),
            "decision": review.get("decision"),
            "reason": review.get("reason"),
            "roles": review.get("roles", []),
            "score": (review.get("observation") or {}).get("score"),
            "match_tier": review.get("match_tier"),
            "match_percent": review.get("match_percent"),
        }

    def _evidence_review_gaps(self, profile: dict[str, Any], accepted: list[dict[str, Any]]) -> list[str]:
        text = json.dumps([item.get("react_review", {}).get("observation", {}).get("matched", {}) for item in accepted], ensure_ascii=False).lower()
        gaps = []
        for name in profile.get("structures") or []:
            if name not in text:
                gaps.append(f"structure:{name}")
        for name in profile.get("objectives") or []:
            if name not in text:
                gaps.append(f"objective:{name}")
        for name in profile.get("algorithms") or []:
            if name not in text:
                gaps.append(f"algorithm:{name}")
        return gaps
