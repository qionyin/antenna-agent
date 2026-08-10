from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import atomic_write_json


@dataclass(frozen=True)
class SkillCard:
    id: str
    category: str
    short_description: str
    source_path: str
    capability: str
    domain_terms: tuple[str, ...]
    trigger_terms: tuple[str, ...]
    weak_context_terms: tuple[str, ...]
    negative_terms: tuple[str, ...]
    task_types: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    risk_level: str
    requires_approval: bool
    adapter_binding: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillCard":
        return cls(
            id=str(data["id"]),
            category=str(data.get("category", "general")),
            short_description=str(data.get("short_description", "")),
            source_path=str(data.get("source_path", "")),
            capability=str(data.get("capability", data["id"].replace("-", "_"))),
            domain_terms=tuple(str(item) for item in data.get("domain_terms", [])),
            trigger_terms=tuple(str(item) for item in data.get("trigger_terms", [])),
            weak_context_terms=tuple(str(item) for item in data.get("weak_context_terms", [])),
            negative_terms=tuple(str(item) for item in data.get("negative_terms", [])),
            task_types=tuple(str(item) for item in data.get("task_types", [])),
            inputs=tuple(str(item) for item in data.get("inputs", [])),
            outputs=tuple(str(item) for item in data.get("outputs", [])),
            risk_level=str(data.get("risk_level", "low")),
            requires_approval=bool(data.get("requires_approval", False)),
            adapter_binding=dict(data.get("adapter_binding") or {}),
        )

    def to_index_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "short_description": self.short_description,
            "capability": self.capability,
            "source_path": self.source_path,
            "risk_level": self.risk_level,
            "requires_approval": self.requires_approval,
            "adapter_binding": self.adapter_binding,
        }


class SkillRegistry:
    """Load project-local skill cards without reading source SKILL.md files."""

    def __init__(self, root: str | Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.root = Path(root) if root is not None else project_root / "skill_system"
        self.categories_root = self.root / "categories"
        self.registry_root = self.root / "registry"
        self._cards: dict[str, SkillCard] | None = None

    def load(self) -> dict[str, SkillCard]:
        if self._cards is not None:
            return self._cards
        cards: dict[str, SkillCard] = {}
        if self.categories_root.exists():
            for path in sorted(self.categories_root.rglob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                card = SkillCard.from_dict(data)
                cards[card.id] = card
        self._cards = cards
        return cards

    def list_cards(self) -> list[SkillCard]:
        return list(self.load().values())

    def get(self, skill_id: str) -> SkillCard | None:
        return self.load().get(skill_id)

    def write_indexes(self) -> dict[str, str]:
        cards = self.load()
        skill_index = {
            "schema_version": "1.0",
            "generated_from": "skill_system/categories/**/*.json",
            "skills": {skill_id: card.to_index_dict() for skill_id, card in sorted(cards.items())},
        }
        category_map: dict[str, list[str]] = {}
        for skill_id, card in sorted(cards.items()):
            category_map.setdefault(card.category, []).append(skill_id)
        category_index = {
            "schema_version": "1.0",
            "generated_from": "skill_system/categories/**/*.json",
            "categories": category_map,
        }
        self.registry_root.mkdir(parents=True, exist_ok=True)
        skill_path = self.registry_root / "skill_index.json"
        category_path = self.registry_root / "category_index.json"
        atomic_write_json(skill_path, skill_index)
        atomic_write_json(category_path, category_index)
        return {"skill_index": str(skill_path), "category_index": str(category_path)}

    def validate_aliases(self) -> dict[str, Any]:
        aliases_path = self.registry_root / "routing_aliases.json"
        if not aliases_path.exists():
            return {"success": True, "missing": [], "path": str(aliases_path)}
        aliases = json.loads(aliases_path.read_text(encoding="utf-8")).get("aliases", {})
        missing = [skill_id for skill_id in aliases if skill_id not in self.load()]
        return {"success": not missing, "missing": missing, "path": str(aliases_path)}
