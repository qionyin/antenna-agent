from __future__ import annotations

import fnmatch
import json
import time
from array import array
from collections import defaultdict
from typing import Any

from .embedding import cosine_similarity


DEFAULT_RETENTION_DAYS = 360
SECONDS_PER_DAY = 24 * 60 * 60


class RedisStore:
    """Redis facade with an in-memory fallback for local/offline development."""

    def __init__(
        self,
        url: str,
        json_retention_days: int = DEFAULT_RETENTION_DAYS,
        stream_retention_days: int = DEFAULT_RETENTION_DAYS,
        vector_retention_days: int = DEFAULT_RETENTION_DAYS,
    ):
        """初始化当前对象依赖和运行参数。"""
        self.url = url
        self.json_ttl_seconds = self._retention_days_to_seconds(json_retention_days)
        self.stream_ttl_seconds = self._retention_days_to_seconds(stream_retention_days)
        self.vector_ttl_seconds = self._retention_days_to_seconds(vector_retention_days)
        self.redis = None
        self._binary_redis = None
        self.available = False
        self.redis_stack_available = False
        self._kv: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}
        self._streams: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._vector_docs: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._vector_expires_at: dict[str, dict[str, float]] = defaultdict(dict)
        self._vector_indexes: set[str] = set()
        try:
            import redis

            client = redis.Redis.from_url(url, decode_responses=True)
            client.ping()
            self.redis = client
            self._binary_redis = redis.Redis.from_url(url, decode_responses=False)
            self.available = True
            try:
                client.execute_command("FT._LIST")
                self.redis_stack_available = True
            except Exception:
                self.redis_stack_available = False
        except Exception:
            self.redis = None
            self.available = False

    def health(self) -> dict[str, Any]:
        """返回当前组件健康状态。"""
        return {
            "url": self.url,
            "available": self.available,
            "redis_stack_available": self.redis_stack_available,
            "backend": "redis" if self.available else "memory_fallback",
        }

    def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """把 JSON 数据写入热存储。"""
        payload = json.dumps(value, ensure_ascii=False)
        effective_ttl = self._effective_json_ttl_seconds(ttl_seconds)
        if self.redis is not None:
            try:
                self.redis.set(key, payload, ex=effective_ttl)
                return
            except Exception:
                self.redis = None
                self.available = False
        self._kv[key] = payload
        if effective_ttl is None:
            self._expires_at.pop(key, None)
        else:
            self._expires_at[key] = time.time() + effective_ttl

    def get_json(self, key: str, default: Any = None) -> Any:
        """从热存储读取 JSON 数据。"""
        if self.redis is not None:
            try:
                raw = self.redis.get(key)
            except Exception:
                self.redis = None
                self.available = False
                raw = self._kv.get(key)
        else:
            if self._is_expired(key):
                return default
            raw = self._kv.get(key)
        if raw is None:
            return default
        return json.loads(raw)

    def delete_pattern(self, pattern: str) -> int:
        """按模式删除匹配的热存储键。"""
        count = 0
        if self.redis is not None:
            try:
                for key in self.redis.scan_iter(match=pattern):
                    count += int(self.redis.delete(key))
                return count
            except Exception:
                self.redis = None
                self.available = False
        for key in list(self._kv):
            if fnmatch.fnmatch(key, pattern):
                del self._kv[key]
                self._expires_at.pop(key, None)
                count += 1
        for key in list(self._streams):
            if fnmatch.fnmatch(key, pattern):
                del self._streams[key]
                self._expires_at.pop(key, None)
                count += 1
        return count

    def expire(self, key: str, ttl_seconds: int) -> bool:
        """设置热存储键的过期时间。"""
        if self.redis is not None:
            try:
                return bool(self.redis.expire(key, ttl_seconds))
            except Exception:
                self.redis = None
                self.available = False
        if key in self._kv or key in self._streams:
            self._expires_at[key] = time.time() + ttl_seconds
            return True
        return False

    def keys(self, pattern: str) -> list[str]:
        """返回匹配模式的热存储键列表。"""
        if self.redis is not None:
            try:
                return [str(key) for key in self.redis.scan_iter(match=pattern)]
            except Exception:
                self.redis = None
                self.available = False
        keys = {key for key in self._kv if not self._is_expired(key) and fnmatch.fnmatch(key, pattern)}
        keys.update(key for key in self._streams if not self._is_expired(key) and fnmatch.fnmatch(key, pattern))
        return sorted(keys)

    def iter_keys(self, pattern: str):
        """Yield matching keys without materializing the full key set."""
        if self.redis is not None:
            try:
                yield from (str(key) for key in self.redis.scan_iter(match=pattern))
                return
            except Exception:
                self.redis = None
                self.available = False
        for key in sorted(self._kv):
            if not self._is_expired(key) and fnmatch.fnmatch(key, pattern):
                yield key
        for key in sorted(self._streams):
            if not self._is_expired(key) and fnmatch.fnmatch(key, pattern):
                yield key

    def count_keys(self, pattern: str) -> int:
        """Count matching keys without returning the key names."""
        return sum(1 for _ in self.iter_keys(pattern))

    def xadd(self, stream: str, event: dict[str, Any]) -> str:
        """向事件流追加一条事件。"""
        if self.redis is not None:
            try:
                sequence_id = str(self.redis.xadd(stream, {"payload": json.dumps(event, ensure_ascii=False)}))
                if self.stream_ttl_seconds > 0:
                    self.redis.expire(stream, self.stream_ttl_seconds)
                return sequence_id
            except Exception:
                self.redis = None
                self.available = False
        sequence_id = f"{len(self._streams[stream]) + 1:08d}"
        self._streams[stream].append({"sequence_id": sequence_id, "payload": event})
        if self.stream_ttl_seconds > 0:
            self._expires_at[stream] = time.time() + self.stream_ttl_seconds
        return sequence_id

    def xrange(self, stream: str, after_sequence_id: str | None = None) -> list[dict[str, Any]]:
        """读取事件流中指定序号之后的事件。"""
        if self.redis is not None:
            try:
                min_id = f"({after_sequence_id}" if after_sequence_id else "-"
                rows = self.redis.xrange(stream, min=min_id, max="+")
                events = []
                for row_id, fields in rows:
                    event = json.loads(fields.get("payload", "{}"))
                    event.setdefault("sequence_id", str(row_id))
                    events.append(event)
                return events
            except Exception:
                self.redis = None
                self.available = False
        if self._is_expired(stream):
            return []
        rows = self._streams.get(stream, [])
        return [
            dict(row["payload"], sequence_id=row["sequence_id"])
            for row in rows
            if after_sequence_id is None or row["sequence_id"] > after_sequence_id
        ]

    def xtrim(self, stream: str, maxlen: int) -> int:
        """按最大长度裁剪事件流。"""
        if self.redis is not None:
            try:
                return int(self.redis.xtrim(stream, maxlen=maxlen, approximate=True))
            except Exception:
                self.redis = None
                self.available = False
        if self._is_expired(stream):
            return 0
        rows = self._streams.get(stream, [])
        if len(rows) <= maxlen:
            return 0
        removed = len(rows) - maxlen
        self._streams[stream] = rows[-maxlen:]
        return removed

    def upsert_vector(
        self,
        layer: str,
        record_id: str,
        text: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None:
        doc = {"id": record_id, "layer": layer, "text": text, "embedding": list(embedding), "metadata": metadata}
        self._vector_docs[layer][record_id] = doc
        if self.vector_ttl_seconds > 0:
            self._vector_expires_at[layer][record_id] = time.time() + self.vector_ttl_seconds
        else:
            self._vector_expires_at[layer].pop(record_id, None)
        if not self.redis_stack_available or self._binary_redis is None:
            return
        try:
            self._ensure_vector_index(layer, len(embedding))
            key = f"memvec:{layer}:{record_id}"
            self._binary_redis.hset(
                key,
                mapping={
                    "record_id": record_id,
                    "layer": layer,
                    "text": text,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                    "embedding": array("f", embedding).tobytes(),
                },
            )
            if self.vector_ttl_seconds > 0:
                self._binary_redis.expire(key, self.vector_ttl_seconds)
        except Exception:
            self.redis_stack_available = False

    def vector_search(self, layer: str, query_embedding: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        self._prune_expired_vectors(layer)
        if self.redis_stack_available and self._binary_redis is not None:
            try:
                self._ensure_vector_index(layer, len(query_embedding))
                rows = self._binary_redis.execute_command(
                    "FT.SEARCH", f"idx:memvec:{layer}", f"*=>[KNN {top_k} @embedding $vec AS vector_score]",
                    "PARAMS", 2, "vec", array("f", query_embedding).tobytes(), "SORTBY", "vector_score",
                    "RETURN", 3, "record_id", "metadata", "vector_score", "DIALECT", 2,
                )
                parsed = self._parse_vector_rows(rows)
                if parsed:
                    return parsed[:top_k]
            except Exception:
                self.redis_stack_available = False
        scored = []
        for doc in self._vector_docs.get(layer, {}).values():
            similarity = cosine_similarity(query_embedding, doc["embedding"])
            if similarity > 0:
                scored.append({**doc["metadata"], "id": doc["id"], "vector_score": similarity})
        return sorted(scored, key=lambda item: item["vector_score"], reverse=True)[:top_k]

    @staticmethod
    def _retention_days_to_seconds(days: int) -> int:
        return max(0, int(days)) * SECONDS_PER_DAY

    def _effective_json_ttl_seconds(self, ttl_seconds: int | None = None) -> int | None:
        ttl = self.json_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        return ttl if ttl > 0 else None

    def _is_expired(self, key: str) -> bool:
        """检查内存兜底存储中的键是否过期。"""
        expires_at = self._expires_at.get(key)
        if expires_at is None or expires_at > time.time():
            return False
        self._expires_at.pop(key, None)
        self._kv.pop(key, None)
        self._streams.pop(key, None)
        return True

    def _prune_expired_vectors(self, layer: str) -> None:
        expires = self._vector_expires_at.get(layer)
        if not expires:
            return
        now = time.time()
        for record_id, expires_at in list(expires.items()):
            if expires_at <= now:
                expires.pop(record_id, None)
                self._vector_docs.get(layer, {}).pop(record_id, None)

    def _ensure_vector_index(self, layer: str, dim: int) -> None:
        if layer in self._vector_indexes or self._binary_redis is None:
            return
        index_name = f"idx:memvec:{layer}"
        try:
            self._binary_redis.execute_command("FT.INFO", index_name)
        except Exception:
            self._binary_redis.execute_command(
                "FT.CREATE", index_name, "ON", "HASH", "PREFIX", 1, f"memvec:{layer}:", "SCHEMA",
                "embedding", "VECTOR", "FLAT", 6, "TYPE", "FLOAT32", "DIM", dim,
                "DISTANCE_METRIC", "COSINE", "record_id", "TAG", "text", "TEXT", "metadata", "TEXT",
            )
        self._vector_indexes.add(layer)

    def _parse_vector_rows(self, rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list) or len(rows) < 2:
            return []
        parsed = []
        for index in range(2, len(rows), 2):
            fields = rows[index]
            if not isinstance(fields, list):
                continue
            row = {}
            for field_index in range(0, len(fields), 2):
                row[self._decode(fields[field_index])] = self._decode(fields[field_index + 1])
            metadata = json.loads(row.get("metadata", "{}"))
            try:
                distance = float(row.get("vector_score", 1.0))
            except (TypeError, ValueError):
                distance = 1.0
            metadata["id"] = row.get("record_id", metadata.get("id"))
            metadata["vector_score"] = 1.0 - distance
            parsed.append(metadata)
        return parsed

    def _decode(self, value: Any) -> Any:
        return value.decode("utf-8") if isinstance(value, bytes) else value
