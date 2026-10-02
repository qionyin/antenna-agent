from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .utils import stable_hash


LOGGER = logging.getLogger(__name__)

# 第一批实验：向 LLM 索取 5 个相似 query（不含原句）。
DEFAULT_MAX_VARIANTS = 5

EXPANSION_SYSTEM_PROMPT = (
    "你是天线工程领域的检索查询拓展器。\n"
    "输入是一个用户的原始问题，输出必须是且仅是一个 JSON 对象字典，不要输出任何解释或 Markdown 代码块。\n"
    "字典格式：{\"queries\": [\"相似 query 1\", \"相似 query 2\", ...]}\n"
    "要求：\n"
    "1. queries 是与原问题语义相近、可用于向量检索的改写/扩展问法，数量严格等于 max_variants。\n"
    "2. 每个 query 必须保持原问题意图，不得引入原问题没有的结论、参数或任务。\n"
    "3. 语言与原问题保持一致（中文问题输出中文，英文问题输出英文）。\n"
    "4. 可以补充领域同义词、缩写全称对照（如 S11/回波损耗、GWO/灰狼优化），但不要改变技术指标含义。\n"
    "5. 每个 query 长度控制在 5-60 个字符之间。"
)

# LLM 返回的字典键尚未固定，按优先级兼容常见命名。
VARIANT_KEYS: tuple[str, ...] = (
    "queries",
    "variants",
    "expanded_queries",
    "expanded",
    "similar_queries",
    "rewrites",
    "candidates",
)

# 同义词组：LLM 不可用时的离线兜底拓展（组内成员互为拓展词）。
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("s11", "回波损耗", "反射系数", "return loss"),
    ("s21", "传输系数", "插入损耗", "insertion loss"),
    ("vswr", "驻波比", "电压驻波比"),
    ("arbw", "轴比带宽", "axial ratio bandwidth"),
    ("rhcp", "右旋圆极化"),
    ("lhcp", "左旋圆极化"),
    ("cpw", "共面波导"),
    ("siw", "基片集成波导"),
    ("mimo", "多输入多输出天线"),
    ("gwo", "灰狼优化", "灰狼优化算法"),
    ("pso", "粒子群优化", "粒子群算法"),
    ("ga", "遗传算法"),
    ("cnn", "卷积神经网络"),
    ("cma", "特征模态分析"),
    ("贴片天线", "微带天线", "patch antenna"),
    ("微带天线", "贴片天线", "patch antenna"),
    ("缝隙天线", "开槽天线", "slot antenna"),
    ("圆极化天线", "圆极化"),
    ("天线阵列", "阵列天线"),
    ("超表面天线", "电磁超表面"),
    ("滤波天线", "滤波器和天线一体化"),
    ("去耦结构", "互耦抑制", "去耦"),
    ("轴比带宽", "arbw"),
    ("回波损耗", "s11", "反射系数"),
    ("辐射效率", "效率"),
    ("增益", "gain"),
    ("特征模态分析", "cma"),
    ("代理模型", "surrogate model"),
    ("论文复现", "文献复现", "复现论文"),
    ("几何重建", "模型重建"),
    ("几何建模", "参数化建模"),
    ("仿真预检", "仿真前检查"),
    ("结果审查", "结果复核"),
    ("cst仿真", "cst"),
    ("优化", "参数优化"),
    ("带宽", "工作带宽"),
    ("方向图", "辐射方向图"),
    ("交叉极化", "交叉极化电平"),
)


def _term_matches(query: str, term: str) -> bool:
    """判断术语是否命中问题（ASCII 走词边界，中文走子串）。"""
    normalized = str(query or "").lower()
    term = str(term or "").strip().lower()
    if not term:
        return False
    if re.fullmatch(r"[a-z0-9 .+-]+", term):
        return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", normalized) is not None
    return term in normalized


def collect_synonyms(query: str) -> list[str]:
    """收集命中同义词组后可补充的拓展词，保持首次出现顺序并去重。"""
    synonyms: list[str] = []
    for group in SYNONYM_GROUPS:
        if not any(_term_matches(query, member) for member in group):
            continue
        for member in group:
            if _term_matches(query, member) or member in synonyms:
                continue
            synonyms.append(member)
    return synonyms


def rule_based_variants(query: str, limit: int) -> list[str]:
    """LLM 不可用时的确定性离线拓展：追加式 + 替换式。"""
    if limit <= 0:
        return []
    synonyms = collect_synonyms(query)
    if not synonyms:
        return []
    variants = [f"{query} {' '.join(synonyms[:6])}".strip()]
    pairs = []
    for group in SYNONYM_GROUPS:
        hit = next((member for member in group if _term_matches(str(query), member)), None)
        if hit is None:
            continue
        replacement = next((member for member in group if member != hit and member in synonyms), None)
        if replacement is None:
            continue
        pairs.append((hit, replacement))
    if pairs:
        # 单次扫描替换，避免先替换出的词被后续规则二次改写。
        pattern = re.compile("|".join(re.escape(hit) for hit, _ in sorted(pairs, key=lambda item: -len(item[0]))), re.IGNORECASE)
        mapping = {hit.lower(): replacement for hit, replacement in pairs}
        replaced = pattern.sub(lambda match: mapping[match.group(0).lower()], str(query)).strip()
        if replaced.lower() != str(query).strip().lower():
            variants.append(replaced)
    return variants[:limit]


def extract_variants(payload: Any) -> list[str]:
    """从 LLM 返回的字典中取出 query 列表，兼容键名尚未固定的情况。"""
    if not isinstance(payload, dict):
        return []
    for key in VARIANT_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return _clean_variants(value)
    for value in payload.values():
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return _clean_variants(value)
    return []


def _clean_variants(values: list[Any]) -> list[str]:
    variants: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in variants:
            variants.append(text)
    return variants


@dataclass
class QueryExpander:
    """在向量化之前把原始问题拓展成多个检索 query。

    主路径：复用主 agent 的 LLMClient 生成相似 query；
    兜底路径：LLM 不可用时使用本地同义词表，保证检索链路不中断。
    """

    enabled: bool = True
    max_variants: int = DEFAULT_MAX_VARIANTS
    use_llm: bool = True
    llm_client: Any = None
    runtime_llm_config: Any = None

    def expand(self, query: str) -> list[str]:
        """返回检索用 query 列表，原句始终排在首位。"""
        return self._expand_with_strategy(query)[0]

    def expand_report(self, query: str) -> dict[str, Any]:
        """拓展并返回可写入任务元数据的溯源记录。"""
        original = str(query or "").strip()
        variants, strategy = self._expand_with_strategy(original)
        return {
            "schema_version": "1.0",
            "enabled": bool(self.enabled),
            "strategy": strategy,
            "use_llm": bool(self.use_llm),
            "llm_available": self._llm_available(),
            "requested_variants": max(1, int(self.max_variants)) if self.enabled else 0,
            "query_hash": stable_hash(original),
            "variant_count": len(variants),
            "variants": variants,
        }

    def _expand_with_strategy(self, query: str) -> tuple[list[str], str]:
        """执行一次拓展并返回 (query 列表, 实际使用的策略)。"""
        original = str(query or "").strip()
        if not original:
            return [], "original_only"
        if not self.enabled or self.max_variants <= 0:
            return [original], "disabled"
        limit = max(1, int(self.max_variants))
        expanded = self._llm_variants(original, limit) if self.use_llm else []
        if expanded:
            return [original, *[item for item in expanded if item != original][:limit]], "llm_similar_queries"
        expanded = rule_based_variants(original, limit)
        if expanded:
            return [original, *[item for item in expanded if item != original][:limit]], "rule_synonym"
        return [original], "original_only"

    def _llm_available(self) -> bool:
        if self.llm_client is None or not hasattr(self.llm_client, "generate_json"):
            return False
        config = getattr(self, "runtime_llm_config", None)
        if config is not None and bool(getattr(config, "enabled", False)):
            return True
        return bool(getattr(self.llm_client, "enabled", False))

    def _llm_variants(self, query: str, limit: int) -> list[str]:
        """调用主 agent 的 LLM，失败时返回空列表由调用方兜底。"""
        if self.llm_client is None or not hasattr(self.llm_client, "generate_json"):
            return []
        try:
            parsed = self.llm_client.generate_json(
                system_prompt=EXPANSION_SYSTEM_PROMPT,
                payload={"query": query, "max_variants": limit, "output_format": {"queries": ["..."]}},
                runtime_config=self.runtime_llm_config,
            )
        except Exception as exc:
            LOGGER.warning("query expansion llm call failed, falling back to rule based: %s", exc)
            return []
        return extract_variants(parsed)[:limit]


def variants_payload(variants: list[str]) -> str:
    """调试用：把拓展结果序列化成稳定字符串。"""
    return json.dumps({"variants": variants}, ensure_ascii=False, sort_keys=True)
