from __future__ import annotations

from typing import Any


class LLMEvolutionAdvisor:
    """Use the central LLM to suggest candidates; never apply them directly."""

    SYSTEM_PROMPT = """You diagnose evidence-extraction failures.
Return one JSON object with an `aliases` array. Each item must contain:
`entity`, `alias`, `case_id`, and `reason`.
Only suggest an alias when it appears verbatim in the case text and maps to an expected canonical entity.
Do not suggest prompt, threshold, code, knowledge-promotion, or model changes.
Do not claim that a suggestion has been applied."""

    def suggest(self, cases: list[dict[str, Any]], diagnosis: dict[str, Any], llm_client: Any, runtime_config: Any | None = None) -> dict[str, Any]:
        if llm_client is None:
            raise ValueError("central LLM client is unavailable")
        return llm_client.generate_json(
            system_prompt=self.SYSTEM_PROMPT,
            payload={"cases": cases, "diagnosis": diagnosis},
            runtime_config=runtime_config,
        )
