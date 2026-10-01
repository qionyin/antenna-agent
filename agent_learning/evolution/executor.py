from __future__ import annotations

from copy import deepcopy
from typing import Any

from .policy import EvolutionPolicy


class ControlledEvolutionExecutor:
    """Apply and roll back allow-listed learning configuration changes."""

    CONFIG_NAME = "entity_alias_overrides"

    def __init__(self, memory: Any, compiler: Any):
        self.memory = memory
        self.compiler = compiler
        self.policy = EvolutionPolicy()

    def current_aliases(self) -> dict[str, list[str]]:
        record = self.memory.get_evolution_config(self.CONFIG_NAME)
        value = record.get("value") if isinstance(record.get("value"), dict) else record
        return {str(key): list(values) for key, values in (value or {}).items() if isinstance(values, list)}

    def apply(self, proposal: dict[str, Any]) -> dict[str, Any]:
        additions = self.policy.validate_auto_change(proposal)
        before = self.current_aliases()
        after = deepcopy(before)
        for entity, aliases in additions.items():
            after[entity] = list(dict.fromkeys([*(after.get(entity) or []), *aliases]))
        self._write(after)
        return {"before": before, "after": after, "applied": additions}

    def rollback(self, snapshot: dict[str, list[str]]) -> None:
        self._write(snapshot)

    def _write(self, aliases: dict[str, list[str]]) -> None:
        self.memory.store_evolution_config(self.CONFIG_NAME, aliases)
        self.compiler.set_alias_overrides(aliases)
