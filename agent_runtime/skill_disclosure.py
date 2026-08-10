from __future__ import annotations

from dataclasses import dataclass

from .skill_registry import SkillCard


@dataclass(frozen=True)
class DisclosureCard:
    level: int
    data: dict


class SkillDisclosure:
    """Expose only registry-level skill data until executor gate permits Level 3."""

    ROUTING_LEVEL = 2

    def card(self, skill: SkillCard, level: int = ROUTING_LEVEL) -> DisclosureCard:
        level = min(level, self.ROUTING_LEVEL)
        data = {
            "id": skill.id,
            "category": skill.category,
            "short_description": skill.short_description,
        }
        if level >= 1:
            data.update(
                {
                    "domain_terms": list(skill.domain_terms),
                    "trigger_terms": list(skill.trigger_terms),
                    "negative_terms": list(skill.negative_terms),
                    "weak_context_terms": list(skill.weak_context_terms),
                    "task_types": list(skill.task_types),
                }
            )
        if level >= 2:
            data.update(
                {
                    "inputs": list(skill.inputs),
                    "outputs": list(skill.outputs),
                    "risk_level": skill.risk_level,
                    "requires_approval": skill.requires_approval,
                    "adapter_binding": skill.adapter_binding,
                    "capability": skill.capability,
                }
            )
        return DisclosureCard(level=level, data=data)
