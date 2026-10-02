from __future__ import annotations

from copy import deepcopy
from typing import Any

from .utils import now_iso, stable_hash
from .clustering import KnowledgeClusterer
from .common import ClusterConfig, _unique
from .extraction import EvidenceCompiler
from .evolution import (
    ControlledEvolutionExecutor,
    EvolutionService,
    ExtractionQualityEvaluator,
    LLMEvolutionAdvisor,
    SelfEvolvingEvaluationAdapter,
)
from .innovation import InnovationEvaluator
from .review import KnowledgeReviewer
from .wiki import WikiProjection

class LearningService:
    """Single controlled entrypoint for source, knowledge, experience, and innovation learning."""

    def __init__(
        self,
        memory: Any,
        *,
        enabled: bool = True,
        evolution_enabled: bool = True,
        domain_knowledge_enabled: bool = True,
        llm_client: Any | None = None,
        runtime_llm_config: Any | None = None,
    ):
        self.memory = memory
        self.enabled = bool(enabled)
        self.evolution_enabled = bool(evolution_enabled)
        self.domain_knowledge_enabled = bool(domain_knowledge_enabled)
        alias_record = memory.get_evolution_config("entity_alias_overrides")
        alias_overrides = alias_record.get("value") if isinstance(alias_record.get("value"), dict) else alias_record
        self.compiler = EvidenceCompiler(alias_overrides)
        self.reviewer = KnowledgeReviewer()
        self.clusterer = KnowledgeClusterer(memory.embed_text)
        self.innovation = InnovationEvaluator()
        self.projection = WikiProjection()
        self.evolution = EvolutionService()
        self.evaluation_adapter = SelfEvolvingEvaluationAdapter()
        self.extraction_quality = ExtractionQualityEvaluator()
        self.evolution_executor = ControlledEvolutionExecutor(memory, self.compiler)
        self.llm_advisor = LLMEvolutionAdvisor()
        self.llm_client = llm_client
        self.runtime_llm_config = runtime_llm_config

    def compile_source(self, source_id: str) -> dict[str, Any]:
        self._require_domain_knowledge_enabled()
        source = self.memory.get_long_term_source(source_id)
        if not source:
            raise KeyError(f"long-term source not found: {source_id}")
        compiled = self.compiler.compile(source)
        quality_audit = self.extraction_quality.audit_compilation(compiled)
        compiled["quality_audit"] = quality_audit
        for evidence in compiled["evidence_units"]:
            self.memory.store_evidence(evidence)
        for candidate in compiled["knowledge_candidates"]:
            self.memory.store_domain_knowledge(candidate)
        self.memory.update_long_term_source_status(source_id, "extracted", extraction={
            "evidence_ids": [item["evidence_id"] for item in compiled["evidence_units"]],
            "knowledge_ids": [item["knowledge_id"] for item in compiled["knowledge_candidates"]],
            "compiler": compiled["compiler"],
            "quality_audit": quality_audit,
        })
        compiled["evolution_proposal_ids"] = []
        if self.evolution_enabled:
            audit_run = {
                "schema_version": "1.0",
                "run_id": stable_hash({"source_id": source_id, "audit": quality_audit})[:20],
                "evaluation_type": "extraction_structural_audit",
                "cases": [{"id": source_id, "question": "source compilation", **quality_audit}],
                "created_at": now_iso(),
            }
            self.memory.store_evolution_run(audit_run)
            audit_proposals = self.evolution.propose(audit_run)
            if audit_proposals["proposals"]:
                self.memory.store_evolution_proposals(audit_proposals)
            compiled["evolution_proposal_ids"] = [item["proposal_id"] for item in audit_proposals["proposals"]]
        return compiled

    def review_knowledge(self, knowledge_id: str) -> dict[str, Any]:
        self._require_domain_knowledge_enabled()
        candidate = self.memory.get_domain_knowledge(knowledge_id)
        if not candidate:
            raise KeyError(f"knowledge not found: {knowledge_id}")
        peers = self.memory.list_domain_knowledge(statuses={"candidate", "validated", "promoted", "contradicted"})
        review = self.reviewer.review(candidate, self.memory.get_evidence, peers)
        next_status = "validated" if review["promotable"] else "rejected" if review["decision"] == "reject" else "contradicted" if review["decision"] == "conflict" else "candidate"
        self.memory.update_domain_knowledge(knowledge_id, lifecycle_status=next_status, review=review)
        return review

    def promote_knowledge(self, knowledge_id: str, *, authority: str, evidence_refs: list[str] | None = None) -> dict[str, Any]:
        self._require_domain_knowledge_enabled()
        record = self.memory.get_domain_knowledge(knowledge_id)
        if not record:
            raise KeyError(f"knowledge not found: {knowledge_id}")
        if record.get("lifecycle_status") != "validated":
            raise ValueError("knowledge must be validated before promotion")
        if record.get("evidence_type") not in {"paper_fact", "source_paper_fact"}:
            raise ValueError("only paper-backed domain knowledge can be promoted")
        if authority not in {"user", "cst", "measurement", "reviewer_replay"}:
            raise ValueError("promotion authority must be user, cst, measurement, or reviewer_replay")
        refs = _unique([*(record.get("evidence_refs") or []), *(evidence_refs or [])])
        if not refs or any(self.memory.get_evidence(ref) is None for ref in refs):
            raise ValueError("promotion requires resolvable evidence_refs")
        promoted = self.memory.update_domain_knowledge(knowledge_id, lifecycle_status="promoted", promotion={"authority": authority, "evidence_refs": refs, "promoted_at": now_iso()})
        promoted["wiki_projection"] = self.rebuild_wiki()
        return promoted

    def record_episode(self, episode: dict[str, Any]) -> dict[str, Any]:
        stored = self.memory.store_episode(episode)
        candidate = self._experience_from_episode(stored)
        if candidate:
            self.memory.store_experience(candidate)
            stored = self.memory.update_episode(stored["episode_id"], experience_candidate_id=candidate["experience_id"])
        return stored

    def _experience_from_episode(self, episode: dict[str, Any]) -> dict[str, Any] | None:
        if episode.get("final_status") not in {"completed", "failed"}:
            return None
        feedback_refs = self._record_refs(episode.get("user_feedback") or [], "feedback")
        cst_refs = self._record_refs(episode.get("cst_results") or [], "cst")
        review_refs = [str(item.get("result_id") or item.get("review_id") or "") for item in episode.get("global_reviews") or [] if isinstance(item, dict)]
        attribution = self._attribute_episode_error(episode)
        goal = str(episode.get("task_goal") or "")
        if episode.get("final_status") == "completed":
            lesson = f"The reviewed plan completed for: {goal}. Reuse only when objective, mode, evidence, and dependencies remain comparable."
        else:
            lesson = f"Do not repeat the {attribution} path for: {goal}. Resolve the recorded blocker and re-review dependencies before retry."
        experience_id = stable_hash({"episode_id": episode["episode_id"], "goal": episode.get("task_goal")})[:20]
        return {
            "schema_version": "1.0",
            "experience_id": experience_id,
            "memory_type": "workflow_experience",
            "trigger": {
                "task_goal": episode.get("task_goal"),
                "mode": episode.get("mode"),
                "input_context": episode.get("input_context") or {},
            },
            "action": "follow reviewed dynamic plan" if episode.get("final_status") == "completed" else "repair before replay",
            "result": {"status": episode.get("final_status"), "failure_reasons": episode.get("failure_reasons") or []},
            "error_attribution": attribution,
            "lesson": lesson,
            "when_not_to_use": ["different objective", "different execution mode", "changed evidence or dependencies without review"],
            "evidence_refs": _unique([episode["episode_id"], *feedback_refs, *cst_refs, *review_refs]),
            "source_episode_id": episode["episode_id"],
            "lifecycle_status": "candidate",
            "status": "candidate",
            "reuse_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "replay_validation": {"status": "not_run", "case_count": 0},
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }

    @staticmethod
    def _record_refs(records: list[Any], prefix: str) -> list[str]:
        refs = []
        for record in records:
            if isinstance(record, dict):
                value = next(
                    (
                        record.get(key)
                        for key in ("result_id", "review_id", "message_id", "task_id", "id")
                        if record.get(key)
                    ),
                    None,
                )
                refs.append(str(value or f"{prefix}:{stable_hash(record)[:16]}"))
            elif record is not None:
                refs.append(str(record))
        return refs

    @staticmethod
    def _attribute_episode_error(episode: dict[str, Any]) -> str:
        text = " ".join(str(item) for item in episode.get("failure_reasons") or []).lower()
        reviews = [*(episode.get("module_reviews") or []), *(episode.get("global_reviews") or [])]
        for review in reviews:
            output = review.get("output") if isinstance(review, dict) else None
            value = output if isinstance(output, dict) else review if isinstance(review, dict) else {}
            error_type = str((value.get("reflection") or {}).get("error_type") or "")
            if error_type in {"missing_dependency", "weak_evidence", "unsafe_execution", "wrong_primary", "false_positive", "false_negative"}:
                return {
                    "missing_dependency": "planning_error",
                    "weak_evidence": "retrieval_error",
                    "unsafe_execution": "execution_error",
                    "wrong_primary": "reasoning_error",
                    "false_positive": "reasoning_error",
                    "false_negative": "reasoning_error",
                }[error_type]
        mapping = (
            ("paperwise", "retrieval_error"),
            ("evidence", "extraction_error"),
            ("route", "planning_error"),
            ("plan", "planning_error"),
            ("cst", "execution_error"),
            ("solver", "execution_error"),
            ("review", "review_error"),
        )
        return next((kind for token, kind in mapping if token in text), "none" if episode.get("final_status") == "completed" else "unknown_error")

    def record_experience_feedback(self, experience_id: str, *, task_id: str, outcome: str, evidence_refs: list[str] | None = None) -> dict[str, Any]:
        if outcome not in {"helpful", "neutral", "harmful"}:
            raise ValueError("reuse outcome must be helpful, neutral, or harmful")
        if not str(task_id or "").strip():
            raise ValueError("experience feedback requires task_id")
        record = self.memory.get_experience(experience_id)
        if not record:
            raise KeyError(f"experience not found: {experience_id}")
        uses = list(record.get("usage_records") or [])
        uses.append({"task_id": task_id, "outcome": outcome, "evidence_refs": evidence_refs or [], "created_at": now_iso()})
        success = int(record.get("success_count") or 0) + int(outcome == "helpful")
        failure = int(record.get("failure_count") or 0) + int(outcome == "harmful")
        lifecycle = str(record.get("lifecycle_status") or "candidate")
        if outcome == "harmful":
            lifecycle = "contradicted"
        elif lifecycle == "candidate" and success >= 1 and (evidence_refs or []):
            lifecycle = "validated"
        return self.memory.update_experience(experience_id, usage_records=uses, reuse_count=len(uses), success_count=success, failure_count=failure, lifecycle_status=lifecycle)

    def validate_experience_replay(self, experience_id: str, replay_cases: list[dict[str, Any]]) -> dict[str, Any]:
        record = self.memory.get_experience(experience_id)
        if not record:
            raise KeyError(f"experience not found: {experience_id}")
        if len(replay_cases) < 3:
            raise ValueError("experience replay requires at least 3 cases")
        outcomes = [str(item.get("outcome") or "") for item in replay_cases]
        if any(outcome not in {"helpful", "neutral", "harmful"} for outcome in outcomes):
            raise ValueError("replay outcome must be helpful, neutral, or harmful")
        helpful = outcomes.count("helpful")
        harmful = outcomes.count("harmful")
        helpful_rate = helpful / len(outcomes)
        passed = helpful_rate >= 0.8 and harmful == 0
        validation = {
            "status": "passed" if passed else "failed",
            "case_count": len(outcomes),
            "helpful": helpful,
            "neutral": outcomes.count("neutral"),
            "harmful": harmful,
            "helpful_rate": round(helpful_rate, 4),
            "case_ids": [str(item.get("case_id") or "") for item in replay_cases],
            "validated_at": now_iso(),
        }
        lifecycle = "validated" if passed else "contradicted" if harmful else str(record.get("lifecycle_status") or "candidate")
        return self.memory.update_experience(experience_id, replay_validation=validation, lifecycle_status=lifecycle)

    def promote_experience(self, experience_id: str, *, authority: str, evidence_refs: list[str] | None = None) -> dict[str, Any]:
        record = self.memory.get_experience(experience_id)
        if not record:
            raise KeyError(f"experience not found: {experience_id}")
        if record.get("lifecycle_status") != "validated":
            raise ValueError("experience must be validated before promotion")
        if authority not in {"user", "cst", "measurement", "reviewer_replay"}:
            raise ValueError("unsupported experience promotion authority")
        refs = _unique([*(record.get("evidence_refs") or []), *(evidence_refs or [])])
        replay_passed = (record.get("replay_validation") or {}).get("status") == "passed"
        if authority == "reviewer_replay" and not replay_passed:
            raise ValueError("reviewer_replay promotion requires passed replay validation")
        if authority in {"user", "cst", "measurement"} and not (evidence_refs or []):
            raise ValueError("authoritative experience promotion requires evidence_refs")
        return self.memory.update_experience(
            experience_id,
            lifecycle_status="promoted",
            evidence_refs=refs,
            promotion={"authority": authority, "evidence_refs": evidence_refs or [], "promoted_at": now_iso()},
        )

    def cluster_candidates(self, config: ClusterConfig) -> dict[str, Any]:
        self._require_domain_knowledge_enabled()
        candidates = self.memory.list_domain_knowledge(statuses={"candidate", "validated"})
        result = self.clusterer.cluster(candidates, config)
        self.memory.store_cluster_run(result)
        return result

    def evaluate_innovation(self, idea: dict[str, Any]) -> dict[str, Any]:
        self._require_domain_knowledge_enabled()
        result = self.innovation.evaluate(idea, self.memory.get_evidence)
        self.memory.store_innovation(result, idea=idea)
        return result

    def rebuild_wiki(self) -> dict[str, Any]:
        self._require_domain_knowledge_enabled()
        projection = self.projection.build(
            self.memory.list_domain_knowledge(statuses={"promoted"}, limit=10000),
            self.memory.get_evidence,
            self.memory.list_experiences(statuses={"promoted"}, limit=10000),
        )
        self.memory.store_wiki_projection(projection["wiki_pages"])
        return {
            "schema_version": "1.0",
            "source_version": projection["source_version"],
            "wiki_page_count": len(projection["wiki_pages"]),
            "experience_note_count": sum(
                len(page.get("statements") or [])
                for page in projection["wiki_pages"]
                if page.get("page_type") == "experience_wiki"
            ),
            "paper_source": "promoted_paper_evidence",
            "experience_source": "promoted_workflow_experience",
            "knowledge_graph": "not_created; use existing PaperWise or antenna research graph",
        }

    def snapshot(self) -> dict[str, Any]:
        snapshot = self.memory.learning_snapshot()
        snapshot["learning_module"] = {
            "enabled": self.enabled,
            "evolution_enabled": self.evolution_enabled,
            "domain_knowledge_enabled": self.domain_knowledge_enabled,
            "package": "agent_learning",
            "evolution_mode": "controlled_low_risk_apply_then_verify" if self.evolution_enabled else "disabled",
            "automatic_actions": ["add_entity_aliases"] if self.evolution_enabled else [],
        }
        if not self.domain_knowledge_enabled:
            snapshot.update({
                "raw_sources": 0,
                "evidence_units": 0,
                "domain_knowledge": {"total": 0, "by_status": {}},
                "cluster_runs": 0,
                "innovations": 0,
                "wiki_projection": {
                    "wiki_pages": 0,
                    "graph_source": "disabled",
                },
            })
        return snapshot

    def recall_promoted(
        self,
        query: str,
        *,
        knowledge_top_k: int = 2,
        experience_top_k: int = 1,
        query_variants: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {
                "schema_version": "1.0",
                "policy": "learning_module_disabled",
                "query_hash": stable_hash(query),
                "query_variants": [query],
                "knowledge": [],
                "experiences": [],
            }
        context = self.memory.recall_learning_context(
            query,
            knowledge_top_k=knowledge_top_k if self.domain_knowledge_enabled else 0,
            experience_top_k=experience_top_k,
            query_variants=query_variants,
        )
        if not self.domain_knowledge_enabled:
            context["knowledge"] = []
        return context

    def diagnose_evaluation(self, run: dict[str, Any]) -> dict[str, Any]:
        self._require_evolution_enabled()
        normalized = self.evaluation_adapter.normalize(run)
        self.memory.store_evolution_run(normalized)
        return self.evolution.diagnose(normalized)

    def propose_evolution(self, run: dict[str, Any], *, current_config_hash: str | None = None) -> dict[str, Any]:
        self._require_evolution_enabled()
        normalized = self.evaluation_adapter.normalize(run)
        self.memory.store_evolution_run(normalized)
        result = self.evolution.propose(normalized, current_config_hash=current_config_hash)
        result["stored_proposal_ids"] = self.memory.store_evolution_proposals(result)
        return result

    def verify_evolution(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        self._require_evolution_enabled()
        result = self.evolution.verify(
            self.evaluation_adapter.normalize(before),
            self.evaluation_adapter.normalize(after),
        )
        result["verification_id"] = self.memory.store_evolution_verification(result)
        return result

    def evaluate_extraction(self, cases: list[dict[str, Any]]) -> dict[str, Any]:
        self._require_evolution_enabled()
        result = self.extraction_quality.evaluate(cases, self.compiler)
        self.memory.store_evolution_run(result)
        diagnosis = self.evolution.diagnose(result)
        proposals = self.evolution.propose(result)
        proposal_ids = self.memory.store_evolution_proposals(proposals)
        return {**result, "diagnosis": diagnosis, "proposal_ids": proposal_ids}

    def run_extraction_evolution(
        self,
        cases: list[dict[str, Any]],
        *,
        verification_cases: list[dict[str, Any]] | None = None,
        auto_apply_low_risk: bool = True,
        use_external_llm: bool = False,
    ) -> dict[str, Any]:
        self._require_evolution_enabled()
        working_cases = deepcopy(cases)
        holdout_cases = deepcopy(verification_cases or [])
        baseline = self.extraction_quality.evaluate(working_cases, self.compiler)
        diagnosis = self.evolution.diagnose(baseline)
        llm_advice = None
        egress_plan = None
        if use_external_llm:
            if hasattr(self.llm_client, "egress_plan"):
                egress_plan = self.llm_client.egress_plan(
                    "learning_evolution_advice",
                    str(working_cases),
                    "suggest reviewable extraction aliases",
                )
            llm_advice = self.llm_advisor.suggest(
                working_cases,
                diagnosis,
                self.llm_client,
                self.runtime_llm_config,
            )
            self._merge_llm_alias_suggestions(working_cases, llm_advice)
            baseline = self.extraction_quality.evaluate(working_cases, self.compiler)
            diagnosis = self.evolution.diagnose(baseline)
        self.memory.store_evolution_run(baseline)
        proposal_result = self.evolution.propose(baseline)
        proposal_ids = self.memory.store_evolution_proposals(proposal_result)
        automatic = [item for item in proposal_result["proposals"] if item.get("change", {}).get("automatic")]
        if not auto_apply_low_risk or not automatic:
            return {
                "schema_version": "1.0",
                "status": "proposals_only",
                "baseline": baseline,
                "diagnosis": diagnosis,
                "proposal_ids": proposal_ids,
                "llm_advice": llm_advice,
                "egress_plan": egress_plan,
                "mutation_applied": False,
            }
        if not holdout_cases:
            return {
                "schema_version": "1.0",
                "status": "verification_cases_required",
                "baseline": baseline,
                "diagnosis": diagnosis,
                "proposal_ids": proposal_ids,
                "llm_advice": llm_advice,
                "egress_plan": egress_plan,
                "mutation_applied": False,
            }
        holdout_ids = {str(item.get("case_id") or item.get("id") or "") for item in holdout_cases}
        training_ids = {str(item.get("case_id") or item.get("id") or "") for item in working_cases}
        if holdout_ids.intersection(training_ids):
            raise ValueError("verification cases must use case IDs distinct from training cases")
        verification_baseline = self.extraction_quality.evaluate(holdout_cases, self.compiler)
        original_aliases = self.evolution_executor.current_aliases()
        applied = []
        errors = []
        for proposal in automatic:
            try:
                change = self.evolution_executor.apply(proposal)
                applied.append({"proposal_id": proposal["proposal_id"], **change})
                self.memory.update_evolution_proposal(proposal["proposal_id"], status="applied_pending_verification")
            except (TypeError, ValueError) as exc:
                errors.append({"proposal_id": proposal["proposal_id"], "error": str(exc)})
                self.memory.update_evolution_proposal(proposal["proposal_id"], status="rejected_by_policy", error=str(exc))
        if not applied:
            return {
                "schema_version": "1.0",
                "status": "no_safe_change",
                "baseline": baseline,
                "diagnosis": diagnosis,
                "proposal_ids": proposal_ids,
                "llm_advice": llm_advice,
                "egress_plan": egress_plan,
                "policy_errors": errors,
                "mutation_applied": False,
            }
        after_training = self.extraction_quality.evaluate(working_cases, self.compiler)
        after = self.extraction_quality.evaluate(holdout_cases, self.compiler)
        verification = self.evolution.verify(verification_baseline, after)
        keep = verification["decision"] == "keep"
        if not keep:
            self.evolution_executor.rollback(original_aliases)
        verification["verification_id"] = self.memory.store_evolution_verification(verification)
        final_status = "verified_kept" if keep else "rolled_back"
        for item in applied:
            self.memory.update_evolution_proposal(
                item["proposal_id"],
                status=final_status,
                verification_id=verification["verification_id"],
            )
        return {
            "schema_version": "1.0",
            "status": final_status,
            "baseline": baseline,
            "verification_baseline": verification_baseline,
            "after_training": after_training,
            "after": after,
            "diagnosis": diagnosis,
            "proposal_ids": proposal_ids,
            "applied": applied,
            "policy_errors": errors,
            "verification": verification,
            "llm_advice": llm_advice,
            "egress_plan": egress_plan,
            "mutation_applied": keep,
            "rolled_back": not keep,
        }

    @staticmethod
    def _merge_llm_alias_suggestions(cases: list[dict[str, Any]], advice: dict[str, Any]) -> None:
        by_id = {str(case.get("case_id") or case.get("id") or ""): case for case in cases}
        for item in advice.get("aliases") or []:
            if not isinstance(item, dict):
                continue
            case = by_id.get(str(item.get("case_id") or ""))
            entity = str(item.get("entity") or "")
            alias = str(item.get("alias") or "").strip()
            if case is None or entity not in set(case.get("expected_entities") or []) or not alias:
                continue
            suggestions = case.setdefault("suggested_aliases", {})
            suggestions.setdefault(entity, []).append(alias)

    def _require_evolution_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError("learning_module_disabled")
        if not self.evolution_enabled:
            raise RuntimeError("learning_evolution_disabled")

    def _require_domain_knowledge_enabled(self) -> None:
        if not self.domain_knowledge_enabled:
            raise ValueError("domain_knowledge_disabled")
