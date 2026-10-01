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


PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
