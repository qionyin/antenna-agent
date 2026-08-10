from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal


FailureCategory = Literal[
    "transient",
    "plan",
    "route",
    "artifact_schema",
    "evidence_gap",
    "modeling_parameter",
    "cst_process",
    "license_permission",
    "user_input",
    "code_unknown",
]
Repairability = Literal["automatic", "approval_required", "not_repairable"]

FAILURE_CATEGORIES = frozenset(
    {
        "transient",
        "plan",
        "route",
        "artifact_schema",
        "evidence_gap",
        "modeling_parameter",
        "cst_process",
        "license_permission",
        "user_input",
        "code_unknown",
    }
)


@dataclass(frozen=True)
class FailureClassification:
    category: FailureCategory
    matched_rule: str
    normalized_error: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepairDecision:
    category: FailureCategory
    repairability: Repairability
    action: str
    reason: str
    repair_rounds: int
    max_repair_rounds: int
    exhausted: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FailureClassifier:
    """Classify graph failures with protected operations taking precedence."""

    _PATTERNS: tuple[tuple[FailureCategory, str, tuple[str, ...]], ...] = (
        (
            "plan",
            "dynamic_plan",
            (
                r"dynamic[_ ]plan",
                r"plan validation",
                r"missing step_goal",
                r"invalid dependency",
                r"unknown action",
                r"计划.*(?:无效|失败|缺失)",
            ),
        ),
        (
            "route",
            "skill_route",
            (
                r"skill[_ ]route",
                r"missing[_ ]skill",
                r"skill gate rejected",
                r"route refresh",
                r"技能.*(?:路由|缺失|未调用)",
            ),
        ),
        (
            "artifact_schema",
            "artifact_schema",
            (
                r"artifact.*(?:schema|invalid|malformed|missing required)",
                r"json.*(?:decode|invalid|malformed)",
                r"schema.*(?:invalid|missing|required)",
                r"产物.*(?:格式|字段|损坏)",
            ),
        ),
        (
            "evidence_gap",
            "evidence_gap",
            (
                r"insufficient[_ ]evidence",
                r"missing[_ ]evidence",
                r"no traceable.*evidence",
                r"evidence.*(?:missing|insufficient|not found)",
                r"证据.*(?:不足|缺失|不可追溯)",
            ),
        ),
        (
            "modeling_parameter",
            "modeling_parameter",
            (
                r"parameter.*(?:out of range|invalid|conflict|missing)",
                r"frequency[_ ]range.*(?:not[_ ]supported|mismatch|invalid)",
                r"geometry.*(?:invalid|not modelable|exceeds|conflict)",
                r"unresolved[_ ]parameters",
                r"missing[_ ]geometry[_ ]parameters",
                r"参数.*(?:越界|无效|冲突|缺失)",
                r"几何.*(?:无效|不可建模|越界|冲突)",
            ),
        ),
        (
            "user_input",
            "user_input",
            (
                r"user input required",
                r"missing objective",
                r"missing antenna family",
                r"request user input",
                r"需要用户.*(?:输入|确认|提供)",
            ),
        ),
        (
            "transient",
            "transient_io",
            (
                r"connection (?:reset|refused|aborted)",
                r"network.*unavailable",
                r"rate limit",
                r"too many requests",
                r"file.*(?:locked|busy)",
                r"temporar(?:y|ily)",
                r"timeout|timed out",
                r"连接重置|网络暂时|文件被占用|临时故障",
            ),
        ),
    )

    def classify(
        self,
        error: BaseException | str | dict[str, Any],
        *,
        step_id: str = "",
        context: dict[str, Any] | None = None,
    ) -> FailureClassification:
        primary = self._normalize(error, step_id=step_id, context=None)
        exception_type = str(error.get("exception_type") or "") if isinstance(error, dict) else ""
        if isinstance(error, PermissionError) or exception_type == "PermissionError" or re.search(
            r"\blicen[cs]e\b|access denied|permission denied|许可证|权限不足",
            primary,
            re.IGNORECASE,
        ):
            return FailureClassification("license_permission", "protected_permission", primary)
        cst_artifact_only = "cst_model_spec" in primary or "cst model spec" in primary
        if re.search(r"\bsolver\b|求解器", primary, re.IGNORECASE) or (
            not cst_artifact_only and re.search(r"\bcst(?:\b|_)", primary, re.IGNORECASE)
        ):
            return FailureClassification("cst_process", "protected_cst", primary)
        if re.search(
            r"\b(port|feed|boundary|topology)\b|端口|馈电|边界|拓扑",
            primary,
            re.IGNORECASE,
        ):
            return FailureClassification("modeling_parameter", "protected_modeling", primary)

        normalized = self._normalize(error, step_id=step_id, context=context)
        if isinstance(error, (TimeoutError, ConnectionError)) or exception_type in {"TimeoutError", "ConnectionError"}:
            return FailureClassification("transient", "transient_exception", normalized)
        if isinstance(error, json.JSONDecodeError) or exception_type == "JSONDecodeError":
            return FailureClassification("artifact_schema", "json_decode", normalized)
        if isinstance(error, dict):
            failure_type = str(error.get("type") or "")
            if failure_type in {"dynamic_plan", "plan"}:
                return FailureClassification("plan", f"{failure_type}_type", normalized)
            if failure_type in {"skill_route", "route"}:
                return FailureClassification("route", f"{failure_type}_type", normalized)
        if re.search(
            r"artifact[_ ]schema|schema[_ ]invalid|malformed[_ ]artifact|json[_ ](?:decode|invalid|malformed)",
            normalized,
            re.IGNORECASE,
        ):
            return FailureClassification("artifact_schema", "artifact_schema", normalized)
        if re.search(
            r"frequency[_ ]range.*(?:not[_ ]supported|mismatch|invalid)",
            normalized,
            re.IGNORECASE,
        ):
            return FailureClassification("modeling_parameter", "modeling_frequency_range", normalized)
        for category, rule, patterns in self._PATTERNS:
            if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns):
                return FailureClassification(category, rule, normalized)
        return FailureClassification("code_unknown", "no_rule_matched", normalized)

    @staticmethod
    def _normalize(
        error: BaseException | str | dict[str, Any],
        *,
        step_id: str,
        context: dict[str, Any] | None,
    ) -> str:
        if isinstance(error, dict):
            error_text = json.dumps(error, ensure_ascii=False, sort_keys=True, default=str)
        else:
            error_text = f"{error.__class__.__name__}: {error}" if isinstance(error, BaseException) else str(error)
        context_text = json.dumps(context or {}, ensure_ascii=False, sort_keys=True, default=str)
        return " ".join(f"{step_id} {error_text} {context_text}".lower().split())


class RepairPolicy:
    """Choose one bounded action; execution belongs to the LangGraph repair subgraph."""

    _AUTOMATIC_ACTIONS: dict[str, str] = {
        "route": "refresh_skill_route",
        "plan": "regenerate_plan",
        "artifact_schema": "regenerate_artifact",
        "evidence_gap": "supplement_read_only_evidence",
        "modeling_parameter": "revise_modeling_input",
    }

    def __init__(self, max_repair_rounds: int = 3):
        if max_repair_rounds < 1:
            raise ValueError("max_repair_rounds must be at least 1")
        self.max_repair_rounds = max_repair_rounds

    def decide(
        self,
        classification: FailureClassification | FailureCategory | str,
        *,
        repair_rounds: int = 0,
        context: dict[str, Any] | None = None,
    ) -> RepairDecision:
        category = classification.category if isinstance(classification, FailureClassification) else str(classification)
        if category not in FAILURE_CATEGORIES:
            raise ValueError(f"unsupported failure category: {category}")
        if repair_rounds < 0:
            raise ValueError("repair_rounds cannot be negative")
        if repair_rounds >= self.max_repair_rounds:
            return self._decision(category, "not_repairable", "exhaust_failure", repair_rounds, exhausted=True)

        normalized = classification.normalized_error if isinstance(classification, FailureClassification) else ""
        protected_modeling = bool(
            re.search(r"\b(port|feed|boundary|topology)\b|端口|馈电|边界|拓扑", normalized, re.IGNORECASE)
        ) or bool((context or {}).get("protected_change"))
        if category in {"license_permission", "user_input", "cst_process"}:
            action = {
                "license_permission": "wait_for_license_or_permission",
                "user_input": "request_user_input",
                "cst_process": "wait_for_cst_approval",
            }[category]
            return self._decision(category, "approval_required", action, repair_rounds)
        if category == "modeling_parameter" and protected_modeling:
            return self._decision(category, "approval_required", "wait_for_protected_modeling_approval", repair_rounds)
        if category == "code_unknown":
            return self._decision(category, "not_repairable", "stop_and_report_code_failure", repair_rounds)
        if category == "transient":
            return self._decision(category, "not_repairable", "langgraph_retry_exhausted", repair_rounds)
        return self._decision(category, "automatic", self._AUTOMATIC_ACTIONS[category], repair_rounds)

    def _decision(
        self,
        category: str,
        repairability: Repairability,
        action: str,
        repair_rounds: int,
        *,
        exhausted: bool = False,
    ) -> RepairDecision:
        return RepairDecision(
            category=category,  # type: ignore[arg-type]
            repairability=repairability,
            action=action,
            reason=f"{category} -> {action}",
            repair_rounds=repair_rounds,
            max_repair_rounds=self.max_repair_rounds,
            exhausted=exhausted,
        )
