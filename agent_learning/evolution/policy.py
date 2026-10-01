from __future__ import annotations

import re
from typing import Any

from ..common import ENTITY_ALIASES


class EvolutionPolicy:
    """Allow only reversible, data-level alias additions to run automatically."""

    AUTO_ACTIONS = {"add_entity_aliases"}
    GENERIC_ALIASES = {"antenna", "patch", "design", "structure", "model", "天线", "结构", "模型"}

    def validate_auto_change(self, proposal: dict[str, Any]) -> dict[str, list[str]]:
        change = dict(proposal.get("change") or {})
        if change.get("action") not in self.AUTO_ACTIONS or not change.get("automatic"):
            raise ValueError("proposal is not eligible for automatic execution")
        aliases = change.get("aliases") or {}
        if not isinstance(aliases, dict) or not aliases:
            raise ValueError("automatic alias proposal requires aliases")
        source_text = str((proposal.get("diagnosis") or {}).get("question") or "").lower()
        validated: dict[str, list[str]] = {}
        for entity, values in aliases.items():
            if entity not in ENTITY_ALIASES:
                raise ValueError(f"unknown entity alias target: {entity}")
            clean = []
            for value in values if isinstance(values, list) else []:
                alias = " ".join(str(value).strip().lower().split())
                if not alias or alias in self.GENERIC_ALIASES:
                    continue
                if re.fullmatch(r"[a-z]{1,2}", alias):
                    continue
                if alias not in source_text:
                    continue
                clean.append(alias)
            if clean:
                validated[entity] = list(dict.fromkeys(clean))[:5]
        if not validated:
            raise ValueError("no safe aliases survived policy validation")
        return validated
