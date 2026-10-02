from __future__ import annotations

from typing import Any, Callable

from .utils import cosine_similarity, dedupe_query_variants, now_iso, stable_hash


class LearningMemoryRepositoryMixin:
    """Redis-backed storage operations for V2.3 learning records."""

    def store_long_term_source(
        self,
        source_id: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store raw source material pending extraction into a structured L2 memory."""
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("long-term source_id must be a non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("long-term source content must be a non-empty string")
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("long-term source metadata must be a dictionary or None")
        record = {
            "schema_version": "1.0",
            "source_id": source_id,
            "status": "pending_l2_extraction",
            "content": content,
            "content_hash": stable_hash(content),
            "metadata": dict(metadata or {}),
            "updated_at": now_iso(),
        }
        self.long_term_sources[source_id] = record
        if self.store is not None:
            self.store.set_json(f"mem:source:long_term:{source_id}", record)
        return source_id

    def get_long_term_source(self, source_id: str) -> dict[str, Any] | None:
        if self.store is not None:
            return self.store.get_json(f"mem:source:long_term:{source_id}")
        record = self.long_term_sources.get(source_id)
        return dict(record) if record is not None else None

    def update_long_term_source_status(
        self,
        source_id: str,
        status: str,
        *,
        extraction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"pending_l2_extraction", "extracted", "rejected"}:
            raise ValueError("unsupported long-term source status")
        record = self.get_long_term_source(source_id)
        if record is None:
            raise KeyError(f"long-term source not found: {source_id}")
        updated = {**record, "status": status, "updated_at": now_iso()}
        if extraction is not None:
            updated["extraction"] = dict(extraction)
        self.long_term_sources[source_id] = updated
        if self.store is not None:
            self.store.set_json(f"mem:source:long_term:{source_id}", updated)
        return updated

    def store_evidence(self, evidence: dict[str, Any]) -> str:
        evidence_id = str(evidence.get("evidence_id") or "").strip()
        if not evidence_id or not str(evidence.get("source_id") or "").strip() or not str(evidence.get("text") or "").strip():
            raise ValueError("evidence requires evidence_id, source_id, and text")
        record = {**evidence, "schema_version": str(evidence.get("schema_version") or "1.0")}
        self.evidence[evidence_id] = record
        if self.store is not None:
            self.store.set_json(f"mem:evidence:{evidence_id}", record)
        return evidence_id

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        if self.store is not None:
            return self.store.get_json(f"mem:evidence:{evidence_id}")
        record = self.evidence.get(evidence_id)
        return dict(record) if record is not None else None

    def store_domain_knowledge(self, knowledge: dict[str, Any]) -> str:
        knowledge_id = str(knowledge.get("knowledge_id") or "").strip()
        if not knowledge_id or knowledge.get("memory_type") != "domain_knowledge":
            raise ValueError("domain knowledge requires knowledge_id and memory_type=domain_knowledge")
        lifecycle = str(knowledge.get("lifecycle_status") or "candidate")
        if lifecycle not in {"candidate", "validated", "promoted", "superseded", "contradicted", "rejected", "expired"}:
            raise ValueError("unsupported domain knowledge lifecycle_status")
        evidence_type = str(knowledge.get("evidence_type") or "")
        if evidence_type not in {"paper_fact", "source_paper_fact"}:
            raise ValueError("domain knowledge must be backed by paper_fact or source_paper_fact")
        record = {
            **knowledge,
            "schema_version": str(knowledge.get("schema_version") or "1.0"),
            "status": "active" if lifecycle == "promoted" else lifecycle,
            "lifecycle_status": lifecycle,
            "updated_at": now_iso(),
        }
        self.l2[knowledge_id] = record
        if self.store is not None:
            self.store.set_json(f"mem:l2:domain:{knowledge_id}", record)
            if lifecycle == "promoted":
                text = self._l2_text(record)
                self.store.upsert_vector("l2", knowledge_id, text, self.embed_text(text), record)
        return knowledge_id

    def get_domain_knowledge(self, knowledge_id: str) -> dict[str, Any] | None:
        if self.store is not None:
            record = self.store.get_json(f"mem:l2:domain:{knowledge_id}")
            return dict(record) if isinstance(record, dict) else None
        record = self.l2.get(knowledge_id)
        return dict(record) if isinstance(record, dict) and record.get("memory_type") == "domain_knowledge" else None

    def update_domain_knowledge(self, knowledge_id: str, **changes: Any) -> dict[str, Any]:
        record = self.get_domain_knowledge(knowledge_id)
        if record is None:
            raise KeyError(f"domain knowledge not found: {knowledge_id}")
        updated = {**record, **changes, "knowledge_id": knowledge_id, "updated_at": now_iso()}
        self.store_domain_knowledge(updated)
        return self.get_domain_knowledge(knowledge_id) or updated

    def list_domain_knowledge(self, *, statuses: set[str] | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if self.store is not None:
            for key in self.store.iter_keys("mem:l2:domain:*"):
                record = self.store.get_json(key)
                if isinstance(record, dict):
                    records.append(record)
                    if len(records) >= limit:
                        break
        else:
            records = [record for record in self.l2.values() if record.get("memory_type") == "domain_knowledge"][:limit]
        if statuses is not None:
            records = [record for record in records if record.get("lifecycle_status") in statuses]
        return sorted(records, key=lambda item: str(item.get("knowledge_id") or ""))

    def store_episode(self, episode: dict[str, Any]) -> dict[str, Any]:
        task_id = str(episode.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("episode requires task_id")
        episode_id = str(episode.get("episode_id") or stable_hash({"task_id": task_id, "goal": episode.get("task_goal")})[:20])
        record = {
            **episode,
            "schema_version": str(episode.get("schema_version") or "1.0"),
            "episode_id": episode_id,
            "created_at": str(episode.get("created_at") or now_iso()),
            "updated_at": now_iso(),
        }
        self.episodes[episode_id] = record
        if self.store is not None:
            self.store.set_json(f"mem:episode:{episode_id}", record)
        return record

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        if self.store is not None:
            return self.store.get_json(f"mem:episode:{episode_id}")
        record = self.episodes.get(episode_id)
        return dict(record) if record is not None else None

    def update_episode(self, episode_id: str, **changes: Any) -> dict[str, Any]:
        record = self.get_episode(episode_id)
        if record is None:
            raise KeyError(f"episode not found: {episode_id}")
        return self.store_episode({**record, **changes, "episode_id": episode_id})

    def store_experience(self, experience: dict[str, Any]) -> str:
        experience_id = str(experience.get("experience_id") or "").strip()
        lifecycle = str(experience.get("lifecycle_status") or "candidate")
        if not experience_id or experience.get("memory_type") != "workflow_experience":
            raise ValueError("experience requires experience_id and memory_type=workflow_experience")
        if lifecycle not in {"candidate", "validated", "promoted", "superseded", "contradicted", "rejected", "expired"}:
            raise ValueError("unsupported experience lifecycle_status")
        record = {
            **experience,
            "schema_version": str(experience.get("schema_version") or "1.0"),
            "status": "active" if lifecycle == "promoted" else lifecycle,
            "lifecycle_status": lifecycle,
            "updated_at": now_iso(),
        }
        self.experiences[experience_id] = record
        if self.store is not None:
            self.store.set_json(f"mem:l3:experience:{experience_id}", record)
            if lifecycle == "promoted":
                text = self._experience_text(record)
                self.store.upsert_vector("l3", experience_id, text, self.embed_text(text), record)
        return experience_id

    def get_experience(self, experience_id: str) -> dict[str, Any] | None:
        if self.store is not None:
            record = self.store.get_json(f"mem:l3:experience:{experience_id}")
            return dict(record) if isinstance(record, dict) else None
        record = self.experiences.get(experience_id)
        return dict(record) if record is not None else None

    def update_experience(self, experience_id: str, **changes: Any) -> dict[str, Any]:
        record = self.get_experience(experience_id)
        if record is None:
            raise KeyError(f"experience not found: {experience_id}")
        updated = {**record, **changes, "experience_id": experience_id, "updated_at": now_iso()}
        self.store_experience(updated)
        return self.get_experience(experience_id) or updated

    def list_experiences(self, *, statuses: set[str] | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if self.store is not None:
            for key in self.store.iter_keys("mem:l3:experience:*"):
                record = self.store.get_json(key)
                if isinstance(record, dict):
                    records.append(record)
                    if len(records) >= limit:
                        break
        else:
            records = list(self.experiences.values())[:limit]
        if statuses is not None:
            records = [record for record in records if record.get("lifecycle_status") in statuses]
        return sorted(records, key=lambda item: str(item.get("experience_id") or ""))

    def store_cluster_run(self, cluster_run: dict[str, Any]) -> str:
        cluster_version = str(cluster_run.get("cluster_version") or "").strip()
        if not cluster_version:
            raise ValueError("cluster run requires cluster_version")
        if self.store is not None:
            self.store.set_json(f"mem:cluster:{cluster_version}", cluster_run)
        return cluster_version

    def store_innovation(self, result: dict[str, Any], *, idea: dict[str, Any]) -> str:
        innovation_id = str(result.get("innovation_id") or "").strip()
        if not innovation_id:
            raise ValueError("innovation result requires innovation_id")
        record = {"schema_version": "1.0", "innovation_id": innovation_id, "idea": idea, "evaluation": result, "updated_at": now_iso()}
        if self.store is not None:
            self.store.set_json(f"mem:innovation:{innovation_id}", record)
        return innovation_id

    def store_wiki_projection(self, wiki_pages: list[dict[str, Any]]) -> None:
        self.wiki_pages = {str(page["page_id"]): dict(page) for page in wiki_pages}
        if self.store is not None:
            self.store.delete_pattern("mem:wiki:*")
            for page in wiki_pages:
                self.store.set_json(f"mem:wiki:{page['page_id']}", page)

    def list_wiki_pages(self, limit: int = 1000) -> list[dict[str, Any]]:
        if self.store is not None:
            pages = []
            for key in self.store.iter_keys("mem:wiki:*"):
                page = self.store.get_json(key)
                if isinstance(page, dict):
                    pages.append(page)
                    if len(pages) >= limit:
                        break
            return sorted(pages, key=lambda item: str(item.get("title") or ""))
        return sorted(self.wiki_pages.values(), key=lambda item: str(item.get("title") or ""))[:limit]

    def get_wiki_page(self, page_id: str) -> dict[str, Any] | None:
        if self.store is not None:
            page = self.store.get_json(f"mem:wiki:{page_id}")
            return dict(page) if isinstance(page, dict) else None
        page = self.wiki_pages.get(page_id)
        return dict(page) if page is not None else None

    def store_evolution_run(self, run: dict[str, Any]) -> str:
        run_id = str(run.get("run_id") or stable_hash(run)[:20])
        record = {**run, "schema_version": str(run.get("schema_version") or "1.0"), "run_id": run_id}
        self.evolution_runs[run_id] = record
        if self.store is not None:
            self.store.set_json(f"mem:evolution:run:{run_id}", record)
        return run_id

    def store_evolution_proposals(self, result: dict[str, Any]) -> list[str]:
        proposal_ids = []
        for proposal in result.get("proposals") or []:
            proposal_id = str(proposal.get("proposal_id") or stable_hash(proposal)[:20])
            record = {**proposal, "proposal_id": proposal_id, "run_id": result.get("run_id")}
            self.evolution_proposals[proposal_id] = record
            if self.store is not None:
                self.store.set_json(f"mem:evolution:proposal:{proposal_id}", record)
            proposal_ids.append(proposal_id)
        return proposal_ids

    def update_evolution_proposal(self, proposal_id: str, **changes: Any) -> dict[str, Any]:
        if self.store is not None:
            current = self.store.get_json(f"mem:evolution:proposal:{proposal_id}")
        else:
            current = self.evolution_proposals.get(proposal_id)
        if not isinstance(current, dict):
            raise KeyError(f"evolution proposal not found: {proposal_id}")
        updated = {**current, **changes, "proposal_id": proposal_id, "updated_at": now_iso()}
        self.evolution_proposals[proposal_id] = updated
        if self.store is not None:
            self.store.set_json(f"mem:evolution:proposal:{proposal_id}", updated)
        return updated

    def store_evolution_verification(self, verification: dict[str, Any]) -> str:
        verification_id = stable_hash(verification)[:20]
        record = {**verification, "verification_id": verification_id}
        self.evolution_verifications[verification_id] = record
        if self.store is not None:
            self.store.set_json(f"mem:evolution:verification:{verification_id}", record)
        return verification_id

    def list_evolution_proposals(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.store is not None:
            records = []
            for key in self.store.iter_keys("mem:evolution:proposal:*"):
                record = self.store.get_json(key)
                if isinstance(record, dict):
                    records.append(record)
                    if len(records) >= limit:
                        break
            return sorted(records, key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return sorted(self.evolution_proposals.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)[:limit]

    def get_evolution_config(self, name: str) -> dict[str, Any]:
        if self.store is not None:
            record = self.store.get_json(f"mem:evolution:config:{name}")
            return dict(record) if isinstance(record, dict) else {}
        return dict(self.evolution_configs.get(name) or {})

    def store_evolution_config(self, name: str, value: dict[str, Any]) -> None:
        record = {"schema_version": "1.0", "name": name, "value": value, "updated_at": now_iso()}
        self.evolution_configs[name] = record
        if self.store is not None:
            self.store.set_json(f"mem:evolution:config:{name}", record)

    def recall_learning_context(
        self,
        query: str,
        *,
        knowledge_top_k: int = 2,
        experience_top_k: int = 1,
        query_variants: list[str] | None = None,
    ) -> dict[str, Any]:
        variants = dedupe_query_variants(query, query_variants)
        query_vectors = [self.embed_text(variant) for variant in variants]

        def ranked(records: list[dict[str, Any]], text_builder: Callable[[dict[str, Any]], str], top_k: int) -> list[dict[str, Any]]:
            scored = []
            for record in records:
                record_vector = self.embed_text(text_builder(record))
                score = max(cosine_similarity(query_vector, record_vector) for query_vector in query_vectors)
                scored.append({**record, "retrieval_score": round(float(score), 6)})
            return sorted(scored, key=lambda item: (-item["retrieval_score"], str(item.get("knowledge_id") or item.get("experience_id") or "")))[:top_k]

        knowledge = ranked(self.list_domain_knowledge(statuses={"promoted"}), self._l2_text, knowledge_top_k)
        experiences = ranked(self.list_experiences(statuses={"promoted"}), self._experience_text, experience_top_k)
        return {
            "schema_version": "1.0",
            "policy": "promoted_only",
            "query_hash": stable_hash(query),
            "query_variants": variants,
            "knowledge": knowledge,
            "experiences": experiences,
        }

    def learning_snapshot(self) -> dict[str, Any]:
        def count(pattern: str, fallback: int = 0) -> int:
            return self.store.count_keys(pattern) if self.store is not None else fallback

        knowledge = self.list_domain_knowledge(limit=10000)
        experiences = self.list_experiences(limit=10000)
        return {
            "schema_version": "1.0",
            "raw_sources": count("mem:source:long_term:*", len(self.long_term_sources)),
            "evidence_units": count("mem:evidence:*", len(self.evidence)),
            "episodes": count("mem:episode:*", len(self.episodes)),
            "domain_knowledge": {
                "total": len(knowledge),
                "by_status": self._status_counts(knowledge),
            },
            "experiences": {
                "total": len(experiences),
                "by_status": self._status_counts(experiences),
            },
            "cluster_runs": count("mem:cluster:*"),
            "innovations": count("mem:innovation:*"),
            "evolution": {
                "runs": count("mem:evolution:run:*", len(self.evolution_runs)),
                "proposals": count("mem:evolution:proposal:*", len(self.evolution_proposals)),
                "verifications": count("mem:evolution:verification:*", len(self.evolution_verifications)),
                "configs": count("mem:evolution:config:*", len(self.evolution_configs)),
            },
            "wiki_projection": {
                "wiki_pages": len(self.list_wiki_pages(limit=10000)),
                "graph_source": "existing_paperwise_or_antenna_research_graph",
            },
        }

    @staticmethod
    def _experience_text(record: dict[str, Any]) -> str:
        return " ".join(str(record.get(key) or "") for key in ("trigger", "action", "result", "lesson", "when_not_to_use"))

    @staticmethod
    def _status_counts(records: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            status = str(record.get("lifecycle_status") or record.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))
