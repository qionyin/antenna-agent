from __future__ import annotations

import re
from typing import Any

from .utils import now_iso, stable_hash
from .common import ENTITY_ALIASES, ENTITY_SPECIFICITY, EVIDENCE_TYPES, RELATION_PATTERNS, _unique

class EvidenceCompiler:
    """Compile raw source text into traceable candidate evidence and knowledge."""

    def __init__(self, aliases: dict[str, list[str] | tuple[str, ...]] | None = None):
        self.aliases = {
            canonical: tuple(dict.fromkeys([*base_aliases, *((aliases or {}).get(canonical) or [])]))
            for canonical, base_aliases in ENTITY_ALIASES.items()
        }

    def set_alias_overrides(self, aliases: dict[str, list[str] | tuple[str, ...]]) -> None:
        self.__init__(aliases)

    def compile(self, source: dict[str, Any]) -> dict[str, Any]:
        if source.get("status") not in {"pending_l2_extraction", "extracted"}:
            raise ValueError("source is not available for extraction")
        content = str(source.get("content") or "").strip()
        if not content:
            raise ValueError("source content is empty")
        source_id = str(source["source_id"])
        metadata = dict(source.get("metadata") or {})
        evidence_type = str(metadata.get("evidence_type") or self._evidence_type(metadata))
        if evidence_type not in EVIDENCE_TYPES:
            raise ValueError(f"unsupported evidence_type: {evidence_type}")
        units = []
        candidates = []
        for index, text in enumerate(self._split(content), 1):
            entities = self._entities(text)
            relations = self._relations(text, entities)
            conditions = self._conditions(text, metadata)
            evidence_id = stable_hash({"source_id": source_id, "index": index, "text": text})[:20]
            unit = {
                "schema_version": "1.0",
                "evidence_id": evidence_id,
                "source_id": source_id,
                "source_ref": metadata.get("source_ref") or source_id,
                "locator": metadata.get("locator") or f"segment:{index}",
                "text": text,
                "text_hash": stable_hash(text),
                "entities": entities,
                "candidate_relations": relations,
                "conditions": conditions,
                "evidence_type": evidence_type,
                "status": "candidate",
                "extraction_method": "deterministic_rule_v1",
                "extraction_confidence": self._confidence(entities, relations, conditions),
                "created_at": now_iso(),
            }
            units.append(unit)
            if evidence_type not in {"paper_fact", "source_paper_fact"}:
                continue
            for relation in relations:
                knowledge_id = stable_hash({"relation": relation, "conditions": conditions, "evidence_id": evidence_id})[:20]
                candidates.append({
                    "schema_version": "1.0",
                    "knowledge_id": knowledge_id,
                    "memory_type": "domain_knowledge",
                    "subject": relation["subject"],
                    "relation": relation["relation"],
                    "object": relation["object"],
                    "mechanism": relation.get("mechanism") or "",
                    "conditions": conditions,
                    "effect": relation.get("effect") or "unspecified",
                    "parameters": [entity for entity in entities if entity in {"feed_position", "slot_length", "patch_length", "patch_width"}],
                    "evidence_refs": [evidence_id],
                    "source_refs": [source_id],
                    "evidence_type": evidence_type,
                    "limits": self._limits(text, evidence_type),
                    "counterexample_refs": [],
                    "lifecycle_status": "candidate",
                    "status": "candidate",
                    "confidence": unit["extraction_confidence"],
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                })
        return {
            "schema_version": "1.0",
            "source_id": source_id,
            "source_hash": source.get("content_hash"),
            "evidence_units": units,
            "knowledge_candidates": candidates,
            "compiler": "EvidenceCompiler",
            "created_at": now_iso(),
        }

    @staticmethod
    def _split(content: str) -> list[str]:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", content) if part.strip()]
        units: list[str] = []
        for paragraph in paragraphs or [content]:
            sentences = [part.strip() for part in re.split(r"(?<=[。！？.!?;；])\s+|\n+", paragraph) if part.strip()]
            buffer = ""
            for sentence in sentences:
                if buffer and len(buffer) + len(sentence) > 900:
                    units.append(buffer)
                    buffer = sentence
                else:
                    buffer = f"{buffer} {sentence}".strip()
            if buffer:
                units.append(buffer)
        return units[:200]

    def _entities(self, text: str) -> list[str]:
        resolved = self._resolved_entity_matches(text)
        present = {item["canonical"] for item in resolved}
        return [canonical for canonical in self.aliases if canonical in present]

    def _relations(self, text: str, entities: list[str]) -> list[dict[str, str]]:
        relations = []
        for sentence in self._sentences(text):
            sentence_entities = self._entities(sentence)
            relations.extend(self._relations_in_sentence(sentence, sentence_entities))
        unique: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for relation in relations:
            key = (relation["subject"], relation["relation"], relation["object"])
            if key not in seen:
                seen.add(key)
                unique.append(relation)
        return unique[:20]

    def _relations_in_sentence(self, text: str, entities: list[str]) -> list[dict[str, str]]:
        parameters = [item for item in entities if item in {"feed_position", "slot_length", "patch_length", "patch_width"}]
        metrics = [item for item in entities if item in {"s11", "bandwidth", "gain", "efficiency", "axial_ratio"}]
        algorithms = [item for item in entities if item in {"gwo", "pso", "ga", "de", "ann"}]
        antennas = [item for item in entities if item.endswith("antenna") or item == "antenna_array"]
        entity_spans = {
            entity: self._canonical_spans(text, entity)
            for entity in [*parameters, *metrics, *algorithms, *antennas]
        }
        triggers = EvidenceCompiler._relation_triggers(text)
        relations: list[dict[str, str]] = []
        for index, (start, end, relation) in enumerate(triggers):
            next_start = triggers[index + 1][0] if index + 1 < len(triggers) else len(text)
            if relation == "uses_algorithm":
                algorithm = EvidenceCompiler._nearest_entity(algorithms, entity_spans, end, next_start, prefer_after=True)
                if algorithm:
                    relations.append({
                        "subject": EvidenceCompiler._nearest_entity(antennas, entity_spans, start, start) or "antenna_design",
                        "relation": relation,
                        "object": algorithm,
                    })
                continue
            targets = metrics if relation in {"increases", "decreases", "affects"} else [*metrics, *parameters]
            target = EvidenceCompiler._nearest_entity(targets, entity_spans, end, next_start, prefer_after=True)
            if not target:
                continue
            subject = EvidenceCompiler._nearest_entity([*algorithms, *parameters], entity_spans, start, start)
            if not subject or subject == target:
                continue
            relations.append({"subject": subject, "relation": relation, "object": target})
        return relations

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [
            sentence.strip()
            for sentence in re.split(r"(?<!\d)\.(?!\d)\s*|[!?。！？;；]\s*|\n+", str(text))
            if sentence.strip()
        ]

    def _resolved_entity_matches(self, text: str) -> list[dict[str, Any]]:
        matches = []
        for canonical, aliases in self.aliases.items():
            for alias in aliases:
                for start, end in EvidenceCompiler._term_spans(text, alias):
                    matches.append({
                        "canonical": canonical,
                        "alias": alias,
                        "start": start,
                        "end": end,
                        "specificity": ENTITY_SPECIFICITY.get(canonical, 0),
                    })
        antenna_matches = [item for item in matches if item["canonical"] in ENTITY_SPECIFICITY]
        other_matches = [item for item in matches if item["canonical"] not in ENTITY_SPECIFICITY]
        selected = []
        for candidate in sorted(
            antenna_matches,
            key=lambda item: (-item["specificity"], -(item["end"] - item["start"]), item["start"]),
        ):
            if any(
                candidate["start"] < existing["end"] and existing["start"] < candidate["end"]
                for existing in selected
            ):
                continue
            selected.append(candidate)
        return sorted([*selected, *other_matches], key=lambda item: (item["start"], item["end"], item["canonical"]))

    @staticmethod
    def _term_spans(text: str, term: str) -> list[tuple[int, int]]:
        escaped = re.escape(str(term).lower())
        if re.fullmatch(r"[a-z0-9][a-z0-9 _-]*", str(term).lower()):
            pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
        else:
            pattern = escaped
        return [(match.start(), match.end()) for match in re.finditer(pattern, str(text).lower())]

    def _canonical_spans(self, text: str, canonical: str) -> list[tuple[int, int]]:
        spans = []
        for alias in self.aliases.get(canonical, (canonical,)):
            spans.extend(EvidenceCompiler._term_spans(text, alias))
        return sorted(set(spans))

    @staticmethod
    def _relation_triggers(text: str) -> list[tuple[int, int, str]]:
        candidates = []
        for relation, terms in RELATION_PATTERNS:
            for term in terms:
                candidates.extend((*span, relation) for span in EvidenceCompiler._term_spans(text, term))
        candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        selected = []
        for candidate in candidates:
            if any(candidate[0] < existing[1] and existing[0] < candidate[1] for existing in selected):
                continue
            selected.append(candidate)
        return sorted(selected)

    @staticmethod
    def _nearest_entity(
        entities: list[str],
        spans: dict[str, list[tuple[int, int]]],
        anchor: int,
        window_end: int,
        *,
        prefer_after: bool = False,
    ) -> str | None:
        candidates = []
        for entity in entities:
            for start, end in spans.get(entity, []):
                if prefer_after and anchor <= start < window_end:
                    candidates.append((0, start - anchor, entity))
                elif end <= anchor:
                    candidates.append((1, anchor - end, entity))
                elif not prefer_after:
                    candidates.append((2, start - anchor, entity))
        return min(candidates)[2] if candidates else None

    def _conditions(self, text: str, metadata: dict[str, Any]) -> dict[str, Any]:
        frequencies = re.findall(r"\b\d+(?:\.\d+)?\s*(?:GHz|MHz|kHz|Hz)\b", text, flags=re.IGNORECASE)
        lowered = text.lower()
        validation = "measurement" if any(term in lowered for term in ("measured", "measurement", "实测", "测量")) else "simulation" if any(term in lowered for term in ("simulation", "simulated", "cst", "hfss", "仿真")) else "unspecified"
        return {
            "frequency_bands": _unique(frequencies),
            "antenna_types": [entity for entity in self._entities(text) if entity.endswith("antenna") or entity == "antenna_array"],
            "substrate": metadata.get("substrate"),
            "feed_type": metadata.get("feed_type"),
            "validation_type": validation,
        }

    @staticmethod
    def _evidence_type(metadata: dict[str, Any]) -> str:
        source_type = str(metadata.get("source_type") or "").lower()
        if source_type in {"cst_result", "cst_output"}:
            return "cst_verified"
        if source_type in {"user_message", "user_feedback"}:
            return "user_confirmed"
        if source_type in {"paper", "paperwise_report", "paper_chunk"}:
            return "paper_fact"
        return "llm_inference"

    @staticmethod
    def _confidence(entities: list[str], relations: list[dict[str, str]], conditions: dict[str, Any]) -> float:
        score = 0.35 + min(0.25, len(entities) * 0.04) + min(0.25, len(relations) * 0.08)
        if conditions.get("frequency_bands"):
            score += 0.08
        if conditions.get("validation_type") != "unspecified":
            score += 0.07
        return round(min(score, 0.95), 3)

    @staticmethod
    def _limits(text: str, evidence_type: str) -> list[str]:
        limits = []
        lowered = text.lower()
        if evidence_type in {"llm_inference", "engineering_assumption", "figure_inferred"}:
            limits.append("not a directly verified paper fact")
        if not re.search(r"\b\d+(?:\.\d+)?\s*(?:GHz|MHz)\b", text, flags=re.IGNORECASE):
            limits.append("frequency scope not stated")
        if not any(term in lowered for term in ("measured", "measurement", "实测", "测量")):
            limits.append("measurement validation not established")
        return limits
