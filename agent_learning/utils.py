from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def dedupe_query_variants(query: str, query_variants: list[str] | None = None) -> list[str]:
    """原句在前，拓展 query 去重后追加。"""
    variants = [str(query or "")]
    for item in query_variants or []:
        text = str(item or "").strip()
        if text and text not in variants:
            variants.append(text)
    return variants


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)
