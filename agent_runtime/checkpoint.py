from __future__ import annotations

from .redis_store import RedisStore


class CheckpointManager:
    """Expose checkpoint backend availability for LangGraph."""

    def __init__(self, redis_url: str, store: RedisStore):
        self.redis_url = redis_url
        self.store = store
        self.available = bool(store.available)
        self.error = None if self.available else "redis_unavailable_memory_fallback"

    def health(self) -> dict[str, bool | str | None]:
        return {
            "dynamic_checkpoint_available": self.available,
            "dynamic_checkpoint_error": self.error,
        }
