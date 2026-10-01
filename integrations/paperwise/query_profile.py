from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import ALGORITHM_GROUPS, CRITICAL_MODELING_TERMS, DISALLOWED_TOPIC_TERMS, MATCH_TIER_QUOTAS, OBJECTIVE_GROUPS, STRUCTURE_GROUPS


class PaperWiseQueryProfileMixin:
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
