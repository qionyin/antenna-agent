from __future__ import annotations

import tempfile
import unittest

from agent_learning import ClusterConfig, EvidenceCompiler, EvolutionService, LearningService, SelfEvolvingEvaluationAdapter
from agent_runtime.knowledge import EvidenceCompiler as CompatibilityCompiler
from agent_runtime.audit import AuditLog
from agent_runtime.blackboard import Blackboard
from agent_runtime.capability_registry import CapabilityRegistry
from agent_runtime.config import Settings
from agent_runtime.memory import MemoryManager
from agent_runtime.scheduler import Scheduler
from agent_runtime.streaming import StreamPublisher


class AgentLearningModuleTests(unittest.TestCase):
    def test_scheduler_loads_configured_project_fact_into_legacy_l2(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            memory = MemoryManager()
            Scheduler(
                Settings(
                    workspace_root=root,
                    logs_root=root,
                    llm_enabled=False,
                    learning_l2_project_facts=[{
                        "namespace": "project",
                        "fact": "默认使用 CST 仿真，实测数据优先",
                    }],
                ),
                Blackboard(root),
                memory,
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )
            self.assertEqual(len(memory.l2), 1)
            record = next(iter(memory.l2.values()))
            self.assertEqual(record["namespace"], "project")
            self.assertEqual(record["fact"]["statement"], "默认使用 CST 仿真，实测数据优先")
            self.assertEqual(record["provenance"]["source_ref"], "config.yaml")

    def test_v23_implementation_is_owned_by_standalone_package(self) -> None:
        self.assertIs(CompatibilityCompiler, __import__("agent_learning", fromlist=["EvidenceCompiler"]).EvidenceCompiler)

    def test_evolution_produces_read_only_proposals(self) -> None:
        service = EvolutionService()
        result = service.propose({
            "run_id": "eval-1",
            "cases": [{"id": "case-1", "question": "q", "retrieval_recall": 0.4}],
        })
        self.assertEqual(result["mutation_applied"], False)
        self.assertEqual(result["proposals"][0]["status"], "proposed")
        self.assertTrue(result["proposals"][0]["requires_approval"])

    def test_learning_switch_disables_runtime_recall(self) -> None:
        service = LearningService(MemoryManager(), enabled=False)
        result = service.recall_promoted("bandwidth")
        self.assertEqual(result["policy"], "learning_module_disabled")
        self.assertEqual(result["knowledge"], [])
        self.assertFalse(service.snapshot()["learning_module"]["enabled"])

    def test_evolution_switch_keeps_learning_but_blocks_evolution_chain(self) -> None:
        memory = MemoryManager()
        service = LearningService(memory, evolution_enabled=False)
        source_id = memory.store_long_term_source(
            "switch-paper",
            "A U-slot patch antenna uses GWO to improve bandwidth.",
            metadata={"source_type": "paper", "evidence_type": "paper_fact"},
        )
        compiled = service.compile_source(source_id)
        self.assertTrue(compiled["knowledge_candidates"])
        self.assertEqual(compiled["evolution_proposal_ids"], [])
        self.assertEqual(memory.learning_snapshot()["evolution"]["runs"], 0)
        self.assertFalse(service.snapshot()["learning_module"]["evolution_enabled"])
        with self.assertRaisesRegex(RuntimeError, "learning_evolution_disabled"):
            service.run_extraction_evolution([])

    def test_domain_switch_blocks_path_a_but_keeps_experience_recall(self) -> None:
        memory = MemoryManager()
        memory.store_domain_knowledge({
            "knowledge_id": "domain-hidden",
            "memory_type": "domain_knowledge",
            "subject": "gwo",
            "relation": "optimizes",
            "object": "bandwidth",
            "evidence_refs": ["paper-evidence"],
            "conditions": {},
            "evidence_type": "paper_fact",
            "lifecycle_status": "promoted",
        })
        memory.store_experience({
            "experience_id": "experience-visible",
            "memory_type": "workflow_experience",
            "trigger": {"task_goal": "improve bandwidth"},
            "action": "review evidence",
            "result": {"status": "completed"},
            "lesson": "reuse reviewed evidence",
            "when_not_to_use": [],
            "evidence_refs": ["episode-1"],
            "lifecycle_status": "promoted",
        })
        service = LearningService(memory, domain_knowledge_enabled=False)
        blocked_calls = [
            lambda: service.compile_source("missing"),
            lambda: service.review_knowledge("missing"),
            lambda: service.promote_knowledge("missing", authority="user"),
            lambda: service.cluster_candidates(ClusterConfig()),
            lambda: service.evaluate_innovation({}),
            service.rebuild_wiki,
        ]
        for call in blocked_calls:
            with self.assertRaisesRegex(ValueError, "domain_knowledge_disabled"):
                call()
        recalled = service.recall_promoted("improve bandwidth")
        self.assertEqual(recalled["knowledge"], [])
        self.assertEqual(recalled["experiences"][0]["experience_id"], "experience-visible")
        snapshot = service.snapshot()
        self.assertEqual(snapshot["domain_knowledge"]["total"], 0)
        self.assertEqual(snapshot["experiences"]["total"], 1)

    def test_evolution_verification_rejects_regression(self) -> None:
        service = EvolutionService()
        result = service.verify(
            {"run_id": "before", "cases": [{"retrieval_recall": 0.9, "elapsed_s": 1}]},
            {"run_id": "after", "cases": [{"retrieval_recall": 0.8, "elapsed_s": 2}]},
        )
        self.assertEqual(result["decision"], "reject")

    def test_self_evolving_workspace_output_is_normalized(self) -> None:
        result = SelfEvolvingEvaluationAdapter().normalize({
            "run_id": "external-1",
            "workspaces": {"vault-wikis": {"cases": [{"id": "case-1"}]}},
        })
        self.assertEqual(result["source"], "self-evolving-kb")
        self.assertEqual(result["cases"][0]["workspace"], "vault-wikis")

    def test_five_point_scores_are_normalized_before_diagnosis(self) -> None:
        service = LearningService(MemoryManager())
        diagnosis = service.diagnose_evaluation({
            "run_id": "external-worst",
            "workspaces": {
                "vault-wikis": {
                    "cases": [{"id": "case-1", "judge_correctness": 1.0, "judge_faithfulness": 1.0}]
                }
            },
        })
        self.assertEqual(
            {item["type"] for item in diagnosis["findings"]},
            {"answer_weak", "faithfulness"},
        )

    def test_extraction_uses_boundaries_proximity_and_deduplication(self) -> None:
        compiled = EvidenceCompiler().compile({
            "source_id": "boundary-case",
            "status": "pending_l2_extraction",
            "content": "A decoupling structure uses GWO to decrease S11 and improve bandwidth for a microstrip patch antenna.",
            "content_hash": "test",
            "metadata": {"source_type": "paper", "evidence_type": "paper_fact"},
        })
        self.assertNotIn("de", compiled["evidence_units"][0]["entities"])
        relations = {
            (item["subject"], item["relation"], item["object"])
            for item in compiled["knowledge_candidates"]
        }
        self.assertEqual(relations, {
            ("microstrip_patch_antenna", "uses_algorithm", "gwo"),
            ("gwo", "decreases", "s11"),
            ("gwo", "increases", "bandwidth"),
        })
        self.assertEqual(len(relations), len(compiled["knowledge_candidates"]))

    def test_overlapping_antenna_alias_prefers_specific_subtype(self) -> None:
        compiled = EvidenceCompiler().compile({
            "source_id": "overlap-case",
            "status": "pending_l2_extraction",
            "content": "A U-slot patch antenna uses GWO to improve bandwidth.",
            "content_hash": "test",
            "metadata": {"source_type": "paper", "evidence_type": "paper_fact"},
        })
        entities = compiled["evidence_units"][0]["entities"]
        self.assertIn("u_slot_patch_antenna", entities)
        self.assertNotIn("microstrip_patch_antenna", entities)
        relations = {
            (item["subject"], item["relation"], item["object"])
            for item in compiled["knowledge_candidates"]
        }
        self.assertIn(("u_slot_patch_antenna", "uses_algorithm", "gwo"), relations)

    def test_relation_target_never_falls_back_across_sentences(self) -> None:
        compiled = EvidenceCompiler().compile({
            "source_id": "sentence-boundary-case",
            "status": "pending_l2_extraction",
            "content": (
                "A U-slot patch antenna uses GWO. "
                "GWO improves impedance bandwidth. "
                "The decoupling structure reduces mutual coupling."
            ),
            "content_hash": "test",
            "metadata": {"source_type": "paper", "evidence_type": "paper_fact"},
        })
        relations = {
            (item["subject"], item["relation"], item["object"])
            for item in compiled["knowledge_candidates"]
        }
        self.assertIn(("gwo", "increases", "bandwidth"), relations)
        self.assertNotIn(("gwo", "decreases", "bandwidth"), relations)

    def test_extraction_evaluation_feeds_persisted_proposals(self) -> None:
        memory = MemoryManager()
        service = LearningService(memory)
        result = service.evaluate_extraction([{
            "case_id": "missing-relation",
            "text": "A patch antenna operates at 3.5 GHz.",
            "expected_entities": ["microstrip_patch_antenna"],
            "expected_relations": [["microstrip_patch_antenna", "affects", "bandwidth"]],
        }])
        self.assertLess(result["summary"]["recall"], 1.0)
        self.assertIn("extraction_false_negative", {item["type"] for item in result["diagnosis"]["findings"]})
        self.assertTrue(result["proposal_ids"])
        self.assertEqual(len(memory.list_evolution_proposals()), len(result["proposal_ids"]))

    def test_verification_without_quality_metrics_is_not_kept(self) -> None:
        result = EvolutionService().verify({"run_id": "before", "cases": []}, {"run_id": "after", "cases": []})
        self.assertEqual(result["decision"], "insufficient_evidence")

    def test_controlled_alias_evolution_applies_retests_and_keeps_improvement(self) -> None:
        memory = MemoryManager()
        service = LearningService(memory)
        result = service.run_extraction_evolution([{
            "case_id": "new-u-slot-alias",
            "text": "A U shaped slot patch uses GWO to improve bandwidth.",
            "expected_entities": ["u_slot_patch_antenna", "gwo", "bandwidth"],
            "expected_relations": [
                ["u_slot_patch_antenna", "uses_algorithm", "gwo"],
                ["gwo", "increases", "bandwidth"],
            ],
            "suggested_aliases": {"u_slot_patch_antenna": ["u shaped slot patch"]},
        }], verification_cases=[{
            "case_id": "new-u-slot-alias-holdout",
            "text": "The U shaped slot patch adopts GWO and improves bandwidth.",
            "expected_entities": ["u_slot_patch_antenna", "gwo", "bandwidth"],
            "expected_relations": [
                ["u_slot_patch_antenna", "uses_algorithm", "gwo"],
                ["gwo", "increases", "bandwidth"],
            ],
        }])
        self.assertEqual(result["status"], "verified_kept")
        self.assertTrue(result["mutation_applied"])
        self.assertGreater(result["after"]["summary"]["f1"], result["baseline"]["summary"]["f1"])
        config = memory.get_evolution_config("entity_alias_overrides")
        self.assertIn("u shaped slot patch", config["value"]["u_slot_patch_antenna"])

    def test_external_evolution_advisor_reuses_supplied_central_llm(self) -> None:
        class FakeCentralLLM:
            def __init__(self):
                self.calls = []

            def generate_json(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "aliases": [{
                        "entity": "u_slot_patch_antenna",
                        "alias": "u shaped slot patch",
                        "case_id": "llm-alias",
                        "reason": "verbatim phrase",
                    }],
                    "llm_metadata": {"model": "same-as-central"},
                }

        llm = FakeCentralLLM()
        runtime_config = object()
        service = LearningService(MemoryManager(), llm_client=llm, runtime_llm_config=runtime_config)
        result = service.run_extraction_evolution(
            [{
                "case_id": "llm-alias",
                "text": "A U shaped slot patch uses GWO to improve bandwidth.",
                "expected_entities": ["u_slot_patch_antenna", "gwo", "bandwidth"],
                "expected_relations": [
                    ["u_slot_patch_antenna", "uses_algorithm", "gwo"],
                    ["gwo", "increases", "bandwidth"],
                ],
            }],
            verification_cases=[{
                "case_id": "llm-alias-holdout",
                "text": "The U shaped slot patch adopts GWO and improves bandwidth.",
                "expected_entities": ["u_slot_patch_antenna", "gwo", "bandwidth"],
                "expected_relations": [
                    ["u_slot_patch_antenna", "uses_algorithm", "gwo"],
                    ["gwo", "increases", "bandwidth"],
                ],
            }],
            use_external_llm=True,
        )
        self.assertEqual(result["status"], "verified_kept")
        self.assertEqual(len(llm.calls), 1)
        self.assertIs(llm.calls[0]["runtime_config"], runtime_config)

    def test_controlled_evolution_rolls_back_when_retest_does_not_improve(self) -> None:
        memory = MemoryManager()
        service = LearningService(memory)
        result = service.run_extraction_evolution([{
            "case_id": "rollback-alias",
            "text": "A U shaped slot patch uses GWO.",
            "expected_entities": ["u_slot_patch_antenna", "gwo"],
            "expected_relations": [["u_slot_patch_antenna", "increases", "bandwidth"]],
            "suggested_aliases": {"u_slot_patch_antenna": ["u shaped slot patch"]},
        }], verification_cases=[{
            "case_id": "rollback-alias-holdout",
            "text": "The U shaped slot patch adopts GWO.",
            "expected_entities": ["u_slot_patch_antenna", "gwo"],
            "expected_relations": [["u_slot_patch_antenna", "increases", "bandwidth"]],
        }])
        self.assertEqual(result["status"], "rolled_back")
        self.assertFalse(result["mutation_applied"])
        config = memory.get_evolution_config("entity_alias_overrides")
        self.assertEqual(config["value"], {})

    def test_auto_evolution_requires_distinct_verification_cases(self) -> None:
        service = LearningService(MemoryManager())
        result = service.run_extraction_evolution([{
            "case_id": "training-only",
            "text": "A U shaped slot patch uses GWO.",
            "expected_entities": ["u_slot_patch_antenna", "gwo"],
            "expected_relations": [["u_slot_patch_antenna", "uses_algorithm", "gwo"]],
            "suggested_aliases": {"u_slot_patch_antenna": ["u shaped slot patch"]},
        }])
        self.assertEqual(result["status"], "verification_cases_required")
        self.assertFalse(result["mutation_applied"])


if __name__ == "__main__":
    unittest.main()
