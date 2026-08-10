from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimeLLMConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""
    embedding_model_name: str = "text-embedding-v3"
    source: str = "runtime"

    def load_from_settings(self, settings: Any) -> None:
        """Use config/env as the default LLM source until the frontend overrides it."""
        if self.base_url or self.model_name or self.api_key:
            return
        api_key = os.getenv(getattr(settings, "llm_api_key_env", "OPENAI_API_KEY"), "")
        self.base_url = getattr(settings, "llm_base_url", "") or ""
        self.model_name = getattr(settings, "llm_model_name", "") or ""
        self.embedding_model_name = getattr(settings, "embedding_model_name", "") or self.embedding_model_name
        self.api_key = api_key
        self.enabled = bool(getattr(settings, "llm_enabled", False) or (self.base_url and self.model_name and self.api_key))
        self.source = "config_env"

    def public_state(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "embedding_model_name": self.embedding_model_name,
            "api_key_configured": bool(self.api_key),
            "source": self.source,
        }


runtime_llm_config = RuntimeLLMConfig()
