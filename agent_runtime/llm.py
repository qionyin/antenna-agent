from __future__ import annotations

import json
import os
from typing import Any

from .utils import stable_hash


class LLMGenerationError(RuntimeError):
    """Raised when the configured LLM cannot return a valid JSON object."""


class LLMClient:
    """Small OpenAI-compatible client used by the central Scheduler."""

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        model_name: str,
        timeout_seconds: int = 30,
        enabled: bool = False,
        client_factory: Any | None = None,
    ):
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled
        self.client_factory = client_factory

    def egress_plan(self, operation: str, text: str, purpose: str) -> dict[str, Any]:
        return {
            "data_egress": True,
            "egress_target": self.base_url,
            "operation": operation,
            "model": self.model_name,
            "purpose": purpose,
            "input_hash": stable_hash(text),
            "input_chars": len(text),
        }

    def generate_json(
        self,
        *,
        system_prompt: str,
        payload: dict[str, Any],
        runtime_config: Any | None = None,
    ) -> dict[str, Any]:
        """Call the configured external LLM and require one JSON object."""
        config = self._resolved_config(runtime_config)
        if not config["enabled"]:
            raise LLMGenerationError("central_planner_llm_disabled")
        if not config["base_url"] or not config["api_key"] or not config["model_name"]:
            raise LLMGenerationError("central_planner_llm_not_configured")
        try:
            factory = self.client_factory
            if factory is None:
                from openai import OpenAI

                factory = OpenAI
            client = factory(
                api_key=config["api_key"],
                base_url=config["base_url"],
                timeout=self.timeout_seconds,
            )
            response = client.chat.completions.create(
                model=config["model_name"],
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
                ],
            )
            raw = response.choices[0].message.content or ""
            parsed = self._parse_json_object(raw)
            parsed.setdefault(
                "llm_metadata",
                {
                    "model": config["model_name"],
                    "base_url": config["base_url"],
                    "data_egress": True,
                },
            )
            return parsed
        except LLMGenerationError:
            raise
        except Exception as exc:
            raise LLMGenerationError(f"central_planner_llm_failed:{exc}") from exc

    def _resolved_config(self, runtime_config: Any | None) -> dict[str, Any]:
        if runtime_config is not None and bool(getattr(runtime_config, "enabled", False)):
            return {
                "enabled": True,
                "base_url": str(getattr(runtime_config, "base_url", "") or ""),
                "api_key": str(getattr(runtime_config, "api_key", "") or ""),
                "model_name": str(getattr(runtime_config, "model_name", "") or ""),
            }
        return {
            "enabled": bool(self.enabled),
            "base_url": self.base_url,
            "api_key": os.getenv(self.api_key_env, ""),
            "model_name": self.model_name,
        }

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMGenerationError(f"central_planner_invalid_json:{exc.msg}") from exc
        if not isinstance(value, dict):
            raise LLMGenerationError("central_planner_json_must_be_object")
        return value
