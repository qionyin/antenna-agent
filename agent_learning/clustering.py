from __future__ import annotations

import random
from typing import Any, Callable, Iterable

from .utils import cosine_similarity, now_iso, stable_hash
from .common import ClusterConfig, ENTITY_ALIASES, _normalize_text

class KnowledgeClusterer:
    """Discover candidate groups; never promote or merge formal knowledge."""

    def __init__(self, embed_text: Callable[[str], list[float]] | Any):
        self.embed_text = embed_text if callable(embed_text) else embed_text.embed_text

    def cluster(self, records: list[dict[str, Any]], config: ClusterConfig) -> dict[str, Any]:
        config.validate()
        ordered = sorted(records, key=lambda item: str(item.get("knowledge_id") or ""))
        rng = random.Random(config.random_seed)
        temperature = config.temperature
        previous_signature: tuple[tuple[str, ...], ...] | None = None
        groups: list[list[dict[str, Any]]] = []
        iteration_history = []
        stop_reason = "iteration_limit"
        ambiguous_pairs: list[dict[str, Any]] = []
        for iteration in range(1, config.iteration_limit + 1):
            groups = []
            ambiguous_pairs = []
            pass_records = list(ordered)
            if config.method == "random":
                rng.shuffle(pass_records)
            for record in pass_records:
                best_group: list[dict[str, Any]] | None = None
                best_score = -1.0
                for group in groups:
                    score = self._similarity(record, group[0], config.method, rng, temperature)
                    if score > best_score:
                        best_group, best_score = group, score
                if best_group is not None and best_score >= config.merge_threshold:
                    best_group.append(record)
                else:
                    if best_group is not None and best_score >= config.split_threshold:
                        ambiguous_pairs.append({
                            "left": str(record.get("knowledge_id")),
                            "right": str(best_group[0].get("knowledge_id")),
                            "score": round(best_score, 6),
                            "decision": "keep_separate_pending_review",
                        })
                    groups.append([record])
            signature = tuple(sorted(tuple(sorted(str(item.get("knowledge_id")) for item in group)) for group in groups))
            iteration_history.append({
                "iteration": iteration,
                "temperature": round(temperature, 6),
                "cluster_count": len(groups),
                "ambiguous_pair_count": len(ambiguous_pairs),
            })
            if signature == previous_signature:
                stop_reason = "stable_clusters"
                break
            previous_signature = signature
            temperature *= config.cooling_rate
        cluster_records = []
        for members in groups:
            member_ids = [str(item.get("knowledge_id")) for item in members]
            cluster_records.append({
                "cluster_id": stable_hash({"method": config.method, "members": member_ids, "seed": config.random_seed})[:20],
                "cluster_type": "candidate_cluster" if config.method != "random" else "hypothesis_cluster",
                "method": config.method,
                "member_ids": member_ids,
                "canonical_labels": self._labels(members),
                "formal_knowledge": False,
            })
        return {
            "schema_version": "1.0",
            "cluster_version": stable_hash({"records": [r.get("knowledge_id") for r in ordered], "config": config.__dict__})[:20],
            "config": config.__dict__,
            "input_count": len(ordered),
            "cluster_count": len(cluster_records),
            "clusters": cluster_records,
            "ambiguous_pairs": ambiguous_pairs,
            "iteration_history": iteration_history,
            "stop_reason": stop_reason,
            "created_at": now_iso(),
        }

    def _similarity(self, left: dict[str, Any], right: dict[str, Any], method: str, rng: random.Random, temperature: float) -> float:
        left_key = self._canonical_tuple(left)
        right_key = self._canonical_tuple(right)
        natural = sum(a == b for a, b in zip(left_key, right_key)) / 3.0
        if method == "natural":
            return natural
        left_text = " ".join(left_key)
        right_text = " ".join(right_key)
        semantic = cosine_similarity(self.embed_text(left_text), self.embed_text(right_text))
        if method == "semantic":
            return max(natural, semantic)
        return max(0.0, min(1.0, (natural + semantic) / 2 + rng.uniform(-temperature, temperature)))

    @staticmethod
    def _canonical(value: str) -> str:
        normalized = _normalize_text(value)
        for canonical, aliases in ENTITY_ALIASES.items():
            if normalized == _normalize_text(canonical) or any(normalized == _normalize_text(alias) for alias in aliases):
                return canonical
        return normalized

    def _canonical_tuple(self, record: dict[str, Any]) -> tuple[str, str, str]:
        return (self._canonical(str(record.get("subject") or "")), self._canonical(str(record.get("relation") or "")), self._canonical(str(record.get("object") or "")))

    def _labels(self, members: list[dict[str, Any]]) -> dict[str, str]:
        tuples = [self._canonical_tuple(item) for item in members]
        return {"subject": CounterLike.mode(item[0] for item in tuples), "relation": CounterLike.mode(item[1] for item in tuples), "object": CounterLike.mode(item[2] for item in tuples)}


class CounterLike:
    @staticmethod
    def mode(values: Iterable[str]) -> str:
        counts: dict[str, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        return sorted(counts, key=lambda value: (-counts[value], value))[0] if counts else ""
