from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from functools import cached_property
from typing import Iterable


EMBEDDING_DIM = 64


def _features(text: str) -> Iterable[str]:
    normalized = " ".join(str(text or "").lower().split())
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", normalized)
    for token in tokens:
        yield f"token:{token}"
    compact = "".join(tokens)
    for index in range(max(0, len(compact) - 2)):
        yield f"tri:{compact[index:index + 3]}"


def embed_text(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    vector = [0.0] * dim
    for feature in _features(text):
        digest = hashlib.sha256(feature.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


@dataclass(frozen=True)
class EmbeddingClient:
    provider: str = "local"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_env: str = "API_KEY"
    model_name: str = "text-embedding-v4"
    dimensions: int | None = 1024
    timeout_seconds: int = 30

    @cached_property
    def _openai_client(self):
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"missing embedding API key: set {self.api_key_env}")
        try:
            from openai import OpenAI
        except Exception as exc:  # pragma: no cover - dependency is optional in offline mode
            raise RuntimeError("openai package is required for qwen embeddings") from exc
        return OpenAI(api_key=api_key, base_url=self.base_url, timeout=self.timeout_seconds)

    def embed_text(self, text: str) -> list[float]:
        if self.provider == "local":
            return embed_text(text)
        if self.provider == "qwen":
            return self._embed_qwen(text)
        raise ValueError(f"unsupported embedding provider: {self.provider}")

    def _embed_qwen(self, text: str) -> list[float]:
        payload: dict[str, object] = {
            "model": self.model_name,
            "input": text,
            "encoding_format": "float",
        }
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        response = self._openai_client.embeddings.create(**payload)
        if not response.data:
            raise RuntimeError("qwen embedding response contained no vectors")
        vector = [float(value) for value in response.data[0].embedding]
        return normalize_vector(vector)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))
