from __future__ import annotations

import unittest
import tempfile

from agent_runtime.audit import AuditLog
from agent_runtime.blackboard import Blackboard
from agent_runtime.capability_registry import CapabilityRegistry
from agent_runtime.config import Settings
from agent_runtime.learning import ClusterConfig, LearningService
from agent_runtime.memory import MemoryManager
from agent_runtime.redis_store import RedisStore
from agent_runtime.scheduler import Scheduler
from agent_runtime.runtime_llm_config import runtime_llm_config
from agent_runtime.streaming import StreamPublisher


class V23LearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RedisStore("redis://127.0.0.1:1/0")
        self.memory = MemoryManager(store=self.store)
        self.learning = LearningService(self.memory)

    def _source(self, source_id: str = "paper-001") -> str:
        return self.memory.store_long_term_source(
            source_id,
            "A U-slot patch antenna at 3.5 GHz uses GWO to optimize slot length and improve impedance bandwidth in simulation.",
            metadata={
                "source_type": "paper",
                "source_ref": f"paperwise/{source_id}/report.md",
                "locator": "page 4 paragraph 2",
                "evidence_type": "paper_fact",
            },
        )

    def test_source_compiles_to_traceable_candidate_not_active_memory(self) -> None:
        source_id = self._source()
        compiled = self.learning.compile_source(source_id)

        self.assertGreaterEqual(len(compiled["evidence_units"]), 1)
        self.assertGreaterEqual(len(compiled["knowledge_candidates"]), 1)
        evidence = compiled["evidence_units"][0]
        self.assertEqual(evidence["source_id"], source_id)
        self.assertEqual(evidence["locator"], "page 4 paragraph 2")
        self.assertEqual(self.memory.get_long_term_source(source_id)["status"], "extracted")
        self.assertEqual(self.learning.recall_promoted("GWO bandwidth")["knowledge"], [])

    def test_non_paper_source_cannot_create_domain_knowledge(self) -> None:
        source_id = self.memory.store_long_term_source(
            "cst-source-1",
            "CST export shows a wider bandwidth after changing slot length.",
            metadata={"source_type": "cst_result", "source_ref": "runs/cst-1/result.csv"},
        )
        compiled = self.learning.compile_source(source_id)
        self.assertTrue(compiled["evidence_units"])
        self.assertEqual(compiled["knowledge_candidates"], [])
        self.assertEqual(self.memory.list_domain_knowledge(), [])

    def test_review_and_explicit_promotion_make_knowledge_recallable(self) -> None:
        compiled = self.learning.compile_source(self._source())
        candidate = compiled["knowledge_candidates"][0]
        review = self.learning.review_knowledge(candidate["knowledge_id"])
        self.assertTrue(review["promotable"])
        self.assertEqual(self.memory.get_domain_knowledge(candidate["knowledge_id"])["lifecycle_status"], "validated")

        promoted = self.learning.promote_knowledge(candidate["knowledge_id"], authority="reviewer_replay")
        self.assertEqual(promoted["lifecycle_status"], "promoted")
        recalled = self.learning.recall_promoted("GWO optimizes bandwidth", knowledge_top_k=5)
        self.assertIn(candidate["knowledge_id"], {item["knowledge_id"] for item in recalled["knowledge"]})

    def test_conflicting_effects_are_preserved_and_not_promoted(self) -> None:
        for source_id, effect in (("paper-up", "increase"), ("paper-down", "decrease")):
            self.memory.store_long_term_source(
                source_id,
                f"At 3.5 GHz slot length can {effect} bandwidth.",
                metadata={"source_type": "paper", "source_ref": source_id, "evidence_type": "paper_fact"},
            )
            compiled = self.learning.compile_source(source_id)
            candidate = next(item for item in compiled["knowledge_candidates"] if item["object"] == "bandwidth")
            candidate["subject"] = "slot_length"
            candidate["relation"] = "affects"
            candidate["effect"] = effect
            self.memory.store_domain_knowledge(candidate)
            if source_id == "paper-up":
                self.learning.review_knowledge(candidate["knowledge_id"])
            else:
                review = self.learning.review_knowledge(candidate["knowledge_id"])
                self.assertEqual(review["decision"], "conflict")
                self.assertFalse(review["promotable"])
                self.assertTrue(review["conflict_with"])
        self.assertEqual(len(self.memory.list_domain_knowledge()), 2)

    def test_cluster_is_reproducible_and_never_formal(self) -> None:
        records = [
            {"knowledge_id": "k1", "subject": "GWO", "relation": "optimizes", "object": "S11"},
            {"knowledge_id": "k2", "subject": "Grey Wolf Optimizer", "relation": "optimizes", "object": "return loss"},
            {"knowledge_id": "k3", "subject": "slot_length", "relation": "affects", "object": "bandwidth"},
        ]
        for record in records:
            self.memory.store_domain_knowledge({
                **record,
                "memory_type": "domain_knowledge",
                "evidence_refs": ["e"],
                "conditions": {},
                "evidence_type": "paper_fact",
                "lifecycle_status": "candidate",
            })
        config = ClusterConfig(method="random", temperature=0.2, random_seed=17)
        first = self.learning.cluster_candidates(config)
        second = self.learning.cluster_candidates(config)
        self.assertEqual(first["clusters"], second["clusters"])
        self.assertTrue(all(not cluster["formal_knowledge"] for cluster in first["clusters"]))
        self.assertTrue(all(cluster["cluster_type"] == "hypothesis_cluster" for cluster in first["clusters"]))

    def test_episode_creates_candidate_experience_and_feedback_controls_status(self) -> None:
        episode = self.learning.record_episode({
            "task_id": "task-001",
            "task_goal": "optimize U-slot bandwidth",
            "mode": "mock",
            "final_status": "completed",
            "global_reviews": [{"result_id": "review-001", "decision": "pass"}],
            "user_feedback": [],
            "cst_results": [],
            "failure_reasons": [],
        })
        experience_id = episode["experience_candidate_id"]
        self.assertEqual(self.memory.get_experience(experience_id)["lifecycle_status"], "candidate")
        validated = self.learning.record_experience_feedback(experience_id, task_id="task-002", outcome="helpful", evidence_refs=["user-feedback-1"])
        self.assertEqual(validated["lifecycle_status"], "validated")
        still_validated = self.learning.record_experience_feedback(experience_id, task_id="task-003", outcome="helpful", evidence_refs=["cst-result-1"])
        self.assertEqual(still_validated["lifecycle_status"], "validated")
        promoted = self.learning.promote_experience(experience_id, authority="cst", evidence_refs=["cst-result-1"])
        self.assertEqual(promoted["lifecycle_status"], "promoted")
        contradicted = self.learning.record_experience_feedback(experience_id, task_id="task-004", outcome="harmful", evidence_refs=["review-4"])
        self.assertEqual(contradicted["lifecycle_status"], "contradicted")

    def test_episode_normalizes_dict_feedback_and_cst_results_to_stable_refs(self) -> None:
        episode = self.learning.record_episode({
            "task_id": "task-real-shape",
            "task_goal": "verify simulated CST result",
            "mode": "real",
            "final_status": "completed",
            "global_reviews": [],
            "user_feedback": [{"message_id": "message-1", "message": "accepted"}],
            "cst_results": [{"task_id": "task-real-shape", "results": {"s11_min_db": -23.0}}],
            "failure_reasons": [],
        })
        experience = self.memory.get_experience(episode["experience_candidate_id"])
        self.assertIn("message-1", experience["evidence_refs"])
        self.assertIn("task-real-shape", experience["evidence_refs"])

    def test_experience_replay_requires_three_cases_and_explicit_promotion(self) -> None:
        episode = self.learning.record_episode({
            "task_id": "task-replay",
            "task_goal": "review antenna evidence",
            "mode": "mock",
            "final_status": "completed",
            "global_reviews": [],
            "user_feedback": [],
            "cst_results": [],
            "failure_reasons": [],
        })
        experience_id = episode["experience_candidate_id"]
        with self.assertRaises(ValueError):
            self.learning.validate_experience_replay(experience_id, [{"case_id": "1", "outcome": "helpful"}])
        validated = self.learning.validate_experience_replay(
            experience_id,
            [
                {"case_id": "1", "outcome": "helpful"},
                {"case_id": "2", "outcome": "helpful"},
                {"case_id": "3", "outcome": "helpful"},
            ],
        )
        self.assertEqual(validated["lifecycle_status"], "validated")
        promoted = self.learning.promote_experience(experience_id, authority="reviewer_replay")
        self.assertEqual(promoted["lifecycle_status"], "promoted")

    def test_innovation_requires_all_gates_and_real_validation(self) -> None:
        self.memory.store_evidence({"evidence_id": "e1", "source_id": "p1", "text": "prior work gap", "status": "candidate"})
        idea = {
            "baseline_refs": ["p1"],
            "known_gap": "feed-offset robustness is not evaluated",
            "proposed_delta": "add feed-offset robustness objective",
            "mechanism_hypothesis": "input impedance sensitivity can be reduced",
            "modelable_parameters": ["feed_offset", "slot_length"],
            "metrics": ["S11", "bandwidth", "sensitivity"],
            "baselines": ["single-objective baseline"],
            "ablations": ["remove robustness objective"],
            "falsification_condition": "no improvement under feed-offset sweep",
            "evidence_refs": ["e1"],
        }
        ready = self.learning.evaluate_innovation(idea)
        self.assertEqual(ready["status"], "experiment_ready")
        self.assertFalse(ready["novelty_claim_allowed"])
        validated = self.learning.evaluate_innovation({**idea, "validation_refs": ["cst-run-1"], "validated_by_cst_or_measurement": True})
        self.assertEqual(validated["status"], "validated_innovation")
        self.assertTrue(validated["novelty_claim_allowed"])

    def test_wiki_combines_promoted_paper_knowledge_and_experience_without_graph(self) -> None:
        compiled = self.learning.compile_source(self._source("paper-projection"))
        paper_candidate = compiled["knowledge_candidates"][0]
        self.learning.review_knowledge(paper_candidate["knowledge_id"])
        self.learning.promote_knowledge(paper_candidate["knowledge_id"], authority="reviewer_replay")

        self.memory.store_evidence({
            "schema_version": "1.0",
            "evidence_id": "cst-evidence",
            "source_id": "cst-run-1",
            "source_ref": "runs/cst-run-1/result.csv",
            "locator": "row:1",
            "text": "CST result suggests bandwidth improvement.",
            "evidence_type": "cst_verified",
            "status": "candidate",
        })
        self.memory.store_experience({
            "schema_version": "1.0",
            "experience_id": "exp-cst-1",
            "memory_type": "workflow_experience",
            "source_episode_id": "episode-cst-1",
            "trigger": {"task_goal": "validate CST bandwidth result"},
            "action": "check monitor configuration before interpreting bandwidth",
            "lesson": "A constant curve is an export problem until the monitor is checked.",
            "result": {"status": "success", "cst_status": "completed"},
            "when_not_to_use": ["different monitor type without review"],
            "evidence_refs": ["cst-evidence"],
            "lifecycle_status": "promoted",
        })

        result = self.learning.rebuild_wiki()
        self.assertEqual(result["paper_source"], "promoted_paper_evidence")
        self.assertEqual(result["knowledge_graph"], "not_created; use existing PaperWise or antenna research graph")
        self.assertEqual(self.store.count_keys("mem:knowledge:graph:*"), 0)
        wiki_pages = self.memory.list_wiki_pages()
        paper_statements = [statement for page in wiki_pages if page.get("source_of_truth") == "promoted_paper_evidence" for statement in page.get("statements") or []]
        self.assertIn(paper_candidate["knowledge_id"], {statement["knowledge_id"] for statement in paper_statements})
        self.assertTrue(any(page.get("page_type") == "experience_wiki" for page in wiki_pages))
        experience_page = next(page for page in wiki_pages if page.get("page_type") == "experience_wiki")
        self.assertEqual(experience_page["statements"][0]["experience_id"], "exp-cst-1")
        self.assertEqual(experience_page["statements"][0]["source_scope"], "experience_memory_only; not a graph relation or universal domain fact")
        self.assertTrue(all(page["source_of_truth"] in {"promoted_paper_evidence", "promoted_workflow_experience"} for page in wiki_pages))

    def test_scheduler_terminal_task_records_episode_and_candidate_experience(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            previous = (runtime_llm_config.enabled, runtime_llm_config.base_url, runtime_llm_config.api_key, runtime_llm_config.model_name)
            runtime_llm_config.enabled = False
            runtime_llm_config.base_url = ""
            runtime_llm_config.api_key = ""
            runtime_llm_config.model_name = ""
            try:
                scheduler = Scheduler(
                    Settings(workspace_root=root, logs_root=root, llm_enabled=False),
                    Blackboard(root),
                    MemoryManager(),
                    CapabilityRegistry(),
                    AuditLog(root),
                    StreamPublisher(),
                )
                state = scheduler.create_task_from_request("summarize current task state")
                metadata = state["task_metadata"]
                self.assertIn(state["state"], {"completed", "failed"})
                self.assertTrue(metadata.get("learning_episode_id"))
                self.assertTrue(metadata.get("experience_candidate_id"))
                experience = scheduler.memory.get_experience(metadata["experience_candidate_id"])
                self.assertEqual(experience["lifecycle_status"], "candidate")
                self.assertEqual(metadata["validated_learning_context"]["policy"], "promoted_only")
                self.assertEqual(metadata["validated_learning_context"]["experiences"], [])
                self.assertEqual(len(scheduler.memory.l3), 1)
            finally:
                runtime_llm_config.enabled, runtime_llm_config.base_url, runtime_llm_config.api_key, runtime_llm_config.model_name = previous

    def test_waiting_task_does_not_create_episode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = Scheduler(
                Settings(workspace_root=root, logs_root=root, llm_enabled=False),
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )
            record = scheduler.blackboard.create_task("approval needed")
            scheduler.blackboard.update(record.task_id, state="waiting_approval")
            scheduler._record_terminal_learning(record.task_id)
            self.assertEqual(scheduler.memory.learning_snapshot()["episodes"], 0)


if __name__ == "__main__":
    unittest.main()
