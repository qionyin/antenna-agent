from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .skill_disclosure import SkillDisclosure
from .skill_registry import SkillCard, SkillRegistry


@dataclass(frozen=True)
class SkillMatch:
    skill: SkillCard
    confidence: float
    final_score: float
    domain_score: float
    task_type_score: float
    artifact_score: float
    evidence_score: float
    review_feedback_score: float
    context_score: float
    negative_score: float
    matched_terms: tuple[str, ...]
    negative_matches: tuple[str, ...]
    reason: str


class SkillMatcher:
    """Score skill cards using project-local routing metadata only."""

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self.registry = registry or SkillRegistry()
        self.disclosure = SkillDisclosure()

    def match(
        self,
        user_input: str,
        *,
        mode: str,
        require_paperwise: bool = False,
        context: dict[str, Any] | None = None,
    ) -> tuple[list[SkillMatch], list[dict[str, Any]], dict[str, Any]]:
        text = str(user_input or "")
        lowered = text.lower()
        context = context or {}
        has_antenna_context = bool(context.get("is_antenna_context")) or self._has_antenna_context(lowered)
        matches: list[SkillMatch] = []
        excluded: list[dict[str, Any]] = []

        for skill in self.registry.list_cards():
            self.disclosure.card(skill, 2)
            matched = self._matched_terms(lowered, skill.trigger_terms)
            domain_matched = self._matched_terms(lowered, skill.domain_terms)
            weak_matched = self._matched_terms(lowered, skill.weak_context_terms)
            negative = self._matched_terms(lowered, skill.negative_terms)

            domain_score = min(0.45, 0.15 * len(domain_matched))
            task_type_score = min(0.42, 0.14 * len(matched))
            artifact_score = self._artifact_score(lowered, skill)
            evidence_score = self._evidence_score(skill, context)
            review_feedback_score = self._review_feedback_score(skill, context)
            context_score = 0.50 if weak_matched and (has_antenna_context or skill.category.startswith("antenna")) else 0.0
            if skill.category.startswith("antenna") and has_antenna_context and (matched or domain_matched or weak_matched):
                context_score += 0.18
            negative_score = min(0.75, 0.30 * len(negative))

            if skill.id == "paperwise" and require_paperwise:
                context_score += 0.45
            if skill.id == "cst-control" and mode == "real" and has_antenna_context:
                context_score += 0.15
            if skill.category.startswith("antenna") and not has_antenna_context and not domain_matched:
                negative_score += 0.25
            if skill.category.startswith("antenna") and self._non_antenna_collision(lowered):
                negative_score += 0.65

            final_score = domain_score + task_type_score + artifact_score + evidence_score + review_feedback_score + context_score - negative_score
            is_antenna_support = skill.id in {"paperwise", "pdf"} and (has_antenna_context or require_paperwise)
            selected = final_score >= 0.45 and (skill.category.startswith("antenna") or is_antenna_support)
            if selected:
                matches.append(
                    SkillMatch(
                        skill=skill,
                        confidence=round(max(0.0, min(1.0, final_score)), 3),
                        final_score=round(final_score, 3),
                        domain_score=round(domain_score, 3),
                        task_type_score=round(task_type_score, 3),
                        artifact_score=round(artifact_score, 3),
                        evidence_score=round(evidence_score, 3),
                        review_feedback_score=round(review_feedback_score, 3),
                        context_score=round(context_score, 3),
                        negative_score=round(negative_score, 3),
                        matched_terms=tuple(sorted(set(matched + domain_matched + weak_matched))),
                        negative_matches=tuple(sorted(set(negative))),
                        reason="matched route metadata",
                    )
                )
            elif negative or matched or domain_matched:
                excluded.append(
                    {
                        "owner_skill": skill.id,
                        "reason": "score_below_threshold" if not negative else "negative_domain_match",
                        "matched_terms": sorted(set(matched + domain_matched + weak_matched)),
                        "negative_matches": sorted(set(negative)),
                        "score": round(final_score, 3),
                    }
                )

        matches = self._postprocess_matches(matches, lowered, mode, require_paperwise, has_antenna_context)
        primary_domain = self._primary_domain(matches, lowered)
        domain_decision = {
            "primary_domain": primary_domain,
            "domain_score": round(max((m.domain_score + m.context_score for m in matches), default=0.0), 3),
            "negative_score": round(max((m.negative_score for m in matches), default=0.0), 3),
            "decision": "route_selected" if matches else "no_specialized_skill_needed",
        }
        return matches, excluded, domain_decision

    def _postprocess_matches(
        self,
        matches: list[SkillMatch],
        lowered: str,
        mode: str,
        require_paperwise: bool,
        has_antenna_context: bool,
    ) -> list[SkillMatch]:
        by_id = {match.skill.id: match for match in matches}
        cards = self.registry.load()

        def add(skill_id: str, confidence: float, reason: str) -> None:
            if skill_id not in cards:
                return
            skill = cards[skill_id]
            existing = by_id.get(skill_id)
            if existing is not None and existing.confidence >= confidence:
                return
            by_id[skill_id] = SkillMatch(
                skill=skill,
                confidence=confidence,
                final_score=confidence,
                domain_score=0.0,
                task_type_score=0.0,
                artifact_score=0.0,
                evidence_score=0.0,
                review_feedback_score=0.0,
                context_score=confidence,
                negative_score=0.0,
                matched_terms=(),
                negative_matches=(),
                reason=reason,
            )

        if any(skill_id in by_id for skill_id in {"antenna-research-ideation", "antenna-research-idea-advisor", "antenna-result-to-claim"}):
            add("paperwise", 0.72, "paper evidence support")
        if require_paperwise:
            add("paperwise", 0.9, "paperwise required by task type")
            add("antenna-research-ideation", 0.86, "paper planning requires geometry evidence route")
            add("antenna-research-idea-advisor", 0.82, "paper planning needs an upstream idea skill packet")
            add("antenna-claim-experiment-planner", 0.72, "paper planning needs an experiment contract route")
            add("antenna-result-to-claim", 0.72, "paper planning keeps claim assessment route available")
        if any(skill_id in by_id for skill_id in {"antenna-research-ideation", "antenna-result-to-claim", "cst-control"}):
            add("antenna-research-reviewer", 0.7, "phase review support")
        if any(term in lowered for term in ["gwo", "pso", "ga", "小预算", "budget", "参数扫描", "parameter bounds", "消融"]):
            add("antenna-baseline-ablation-planner", 0.72, "baseline or budget needed")
        result_claim_terms = ["支撑", "supports", "support", "结果", "result", "results", "csv", "return loss", "efficiency", "farfield"]
        if any(term in lowered for term in result_claim_terms) and has_antenna_context:
            add("antenna-result-to-claim", 0.76, "result-to-claim assessment")
        if any(term in lowered for term in ["claim", "声明", "成功标准", "可验证", "指标", "优化一下参数", "带宽不够", "parameter bounds", "idea变成实验"]) and not any(term in lowered for term in result_claim_terms):
            add("antenna-claim-experiment-planner", 0.72, "claim or experiment planning")
        if any(term in lowered for term in ["cst", ".cst", "仿真", "solver", "preflight", "port", "端口", "monitors"]) and not self._non_antenna_collision(lowered):
            add("cst-control", 0.75, "CST control support")
        if mode == "real" and has_antenna_context:
            add("cst-control", 0.75, "real mode requires CST control before E platform execution")
            add("e-platform-cst", 0.76, "real mode uses E platform CST adapter route")
        if any(term in lowered for term in ["pdf", "图", "figure"]) and has_antenna_context:
            add("pdf", 0.72, "PDF or figure extraction support")
        if any(term in lowered for term in ["复现", "建模", "尺寸", "geometry", "case", "导入", "模型图", "cut_body", "建cst模型"]) and has_antenna_context:
            add("antenna-research-ideation", 0.78, "geometry or reproduction route")
        if any(term in lowered for term in ["想法", "idea", "新颖", "值得", "增益太低"]):
            add("antenna-research-idea-advisor", 0.74, "idea or novelty route")
        if any(term in lowered for term in ["审查", "review", "简化", "cut_body", "不要修复", "下一阶段", "object tree", "missing monitors"]):
            add("antenna-research-reviewer", 0.74, "review route")
        if any(term in lowered for term in ["e:\\antenna skills", "e:\\antenna skills-resault", "resault", "代理模型", "surrogate", "优化一下参数", "带宽不够", "s11不行", "增益太低"]):
            add("antenna-skills", 0.74, "top-level antenna route")
        if "antenna-claim-experiment-planner" in by_id:
            add("antenna-research-idea-advisor", 0.82, "experiment adapter needs an upstream idea skill packet")
        return sorted(by_id.values(), key=lambda item: (-item.confidence, item.skill.id))

    def _matched_terms(self, lowered: str, terms: tuple[str, ...]) -> list[str]:
        return [term for term in terms if term and term.lower() in lowered]

    def _artifact_score(self, lowered: str, skill: SkillCard) -> float:
        score = 0.0
        binding = skill.adapter_binding or {}
        packet_type = str(binding.get("packet_type") or "")
        if packet_type and packet_type in lowered:
            score += 0.25
        for token in ["geometry_sketch", "model_spec", "run_manifest", "result manifest", "csv", ".cst"]:
            if token in lowered and token in " ".join(skill.trigger_terms).lower():
                score += 0.15
        return min(0.35, score)

    def _evidence_score(self, skill: SkillCard, context: dict[str, Any]) -> float:
        evidence_terms = " ".join(str(item).lower() for item in context.get("evidence_terms", []) if item)
        if not evidence_terms:
            return 0.0
        score = 0.0
        for term in skill.trigger_terms + skill.domain_terms + skill.weak_context_terms:
            if term and term.lower() in evidence_terms:
                score += 0.08
        if skill.id in set(context.get("evidence_suggested_skills") or []):
            score += 0.18
        return min(0.24, score)

    def _review_feedback_score(self, skill: SkillCard, context: dict[str, Any]) -> float:
        suggested = set(context.get("review_suggested_skills") or [])
        missing = set(context.get("missing_dependency_skills") or [])
        if skill.id in missing:
            return 0.26
        if skill.id in suggested:
            return 0.18
        return 0.0

    def _has_antenna_context(self, lowered: str) -> bool:
        antenna_terms = [
            "antenna", "天线", "cst", ".cst", "s11", "vswr", "gain", "增益", "arbw", "轴比",
            "hfss", "farfield", "return loss", "s参数", "-10 db", "patch", "slot", "cpw", "mimo", "siw", "gwo",
            "psO".lower(), "论文图", "馈电", "端口", "仿真", "resault",
        ]
        return any(term in lowered for term in antenna_terms)

    def _non_antenna_collision(self, lowered: str) -> bool:
        collisions = [
            "react", "frontend", "component", "组件", "dashboard", "card layout", "cst-style",
            "vscode", "端口占用", "股票", "邮件", "word", "git", "矩形的面积", "房间模型",
            "机器学习论文", "论文摘要", "综述", "cnn准确率",
        ]
        return any(term in lowered for term in collisions)

    def _primary_domain(self, matches: list[SkillMatch], lowered: str) -> str:
        if matches:
            top = matches[0].skill.category.split(".", 1)[0]
            return top
        if self._non_antenna_collision(lowered):
            if any(term in lowered for term in ["react", "frontend", "dashboard", "组件", "layout"]):
                return "frontend"
            if any(term in lowered for term in ["word", "docx"]):
                return "document"
            if any(term in lowered for term in ["csv", "xlsx"]):
                return "data"
        return "general"
