from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable



EVIDENCE_TYPES = {
    "paper_fact",
    "source_paper_fact",
    "figure_inferred",
    "engineering_assumption",
    "llm_inference",
    "cst_verified",
    "user_confirmed",
}
MEMORY_LIFECYCLE = {"candidate", "validated", "promoted", "superseded", "contradicted", "rejected", "expired"}

ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "microstrip_patch_antenna": ("microstrip patch", "patch antenna", "微带贴片", "贴片天线"),
    "u_slot_patch_antenna": ("u-slot patch", "u slot patch", "u槽贴片", "u 槽贴片"),
    "slot_antenna": ("slot antenna", "缝隙天线"),
    "mimo_antenna": ("mimo antenna", "mimo天线", "mimo 天线"),
    "antenna_array": ("antenna array", "phased array", "天线阵列", "相控阵"),
    "dielectric_resonator_antenna": ("dielectric resonator antenna", "dra", "介质谐振天线"),
    "gwo": ("grey wolf optimizer", "gray wolf optimizer", "grey wolf optimization", "gwo", "灰狼优化"),
    "pso": ("particle swarm optimization", "particle swarm", "pso", "粒子群优化"),
    "ga": ("genetic algorithm", "ga", "遗传算法"),
    "de": ("differential evolution", "de", "差分进化"),
    "ann": ("artificial neural network", "neural network", "ann", "神经网络"),
    "s11": ("s11", "s 11", "return loss", "reflection coefficient", "回波损耗", "反射系数"),
    "bandwidth": ("impedance bandwidth", "bandwidth", "阻抗带宽", "带宽"),
    "gain": ("realized gain", "gain", "增益"),
    "efficiency": ("radiation efficiency", "efficiency", "辐射效率", "效率"),
    "axial_ratio": ("axial ratio", "arbw", "轴比"),
    "feed_position": ("feed position", "feed offset", "馈电位置", "馈电偏移"),
    "slot_length": ("slot length", "槽长", "缝隙长度"),
    "patch_length": ("patch length", "贴片长度"),
    "patch_width": ("patch width", "贴片宽度"),
}

ENTITY_SPECIFICITY: dict[str, int] = {
    "u_slot_patch_antenna": 100,
    "dielectric_resonator_antenna": 90,
    "mimo_antenna": 90,
    "slot_antenna": 80,
    "antenna_array": 80,
    "microstrip_patch_antenna": 50,
}

RELATION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("optimizes", ("optimize", "optimizes", "optimized", "优化")),
    ("affects", ("affect", "affects", "influence", "impact", "影响")),
    ("increases", ("increase", "increases", "improve", "improves", "提高", "增加", "扩展", "改善")),
    ("decreases", ("decrease", "decreases", "reduce", "reduces", "降低", "减小")),
    ("limited_by", ("limited by", "limitation", "受限", "限制")),
    ("uses_algorithm", ("using", "uses", "adopts", "采用", "使用")),
)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").replace("-", " ").split())


@dataclass(frozen=True)
class ClusterConfig:
    method: str = "natural"
    merge_threshold: float = 0.82
    split_threshold: float = 0.55
    temperature: float = 0.0
    cooling_rate: float = 0.9
    iteration_limit: int = 3
    random_seed: int = 0

    def validate(self) -> None:
        if self.method not in {"natural", "semantic", "random"}:
            raise ValueError("cluster method must be natural, semantic, or random")
        if not 0.0 <= self.split_threshold <= self.merge_threshold <= 1.0:
            raise ValueError("cluster thresholds must satisfy 0 <= split <= merge <= 1")
        if not 0.0 <= self.temperature <= 1.0:
            raise ValueError("cluster temperature must be between 0 and 1")
        if not 0.0 < self.cooling_rate <= 1.0:
            raise ValueError("cluster cooling_rate must be in (0, 1]")
        if not 1 <= self.iteration_limit <= 20:
            raise ValueError("cluster iteration_limit must be between 1 and 20")
