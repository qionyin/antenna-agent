import copy
import csv
import json
import gc
import inspect
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_runtime.approval import ApprovalManager
from agent_runtime.blackboard import Blackboard, OptimisticLockError
from agent_runtime.capability_registry import Capability, CapabilityRegistry
from agent_runtime.checkpoint import CheckpointManager
from agent_runtime.config import Settings, load_settings
from agent_runtime.constants import PACKET_TYPES
from agent_runtime.embedding import EMBEDDING_DIM, embed_text
from agent_runtime.memory import MemoryManager
from agent_runtime.llm import LLMClient
from agent_runtime.maintenance import MaintenanceManager
from agent_runtime.redis_store import RedisStore
from agent_runtime.retrieval import RetrievalEngine, _bm25_tokens
from agent_runtime.runtime_llm_config import runtime_llm_config
from agent_runtime.scheduler import Scheduler
from agent_runtime.skill_executor_gate import SkillExecutorGate
from agent_runtime.skill_packet_validator import SkillPacketValidationError, SkillPacketValidator
from agent_runtime.skill_router import SkillRouter
from agent_runtime.subagents import SubagentRuntime
from agent_runtime.audit import AuditLog
from agent_runtime.streaming import StreamPublisher
from adapters.paperwise_adapter import PaperWiseAdapter
from adapters.cst_real_run_adapter import CstRealRunAdapter
from tests.plan_fixtures import fake_llm_plan


def _contains_live_cst(value):
    """检查测试对象中是否包含真实 CST 标记。"""
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"real_cst_execution", "live_cst", "run_real_cst", "cst_solver_enabled"} and item is True:
                return True
            if lowered in {"execution_mode", "mode"} and isinstance(item, str) and item.lower() in {"cst", "real_cst", "live_cst"}:
                return True
            if _contains_live_cst(item):
                return True
    if isinstance(value, list):
        return any(_contains_live_cst(item) for item in value)
    return False


class RuntimeCoreTests(unittest.TestCase):
    def setUp(self):
        runtime_llm_config.enabled = False
        patcher = patch.object(
            LLMClient,
            "generate_json",
            autospec=True,
            side_effect=lambda _client, **kwargs: fake_llm_plan(kwargs["payload"]),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bm25_tokenizer_uses_jieba_antenna_dictionary(self):
        tokens = _bm25_tokens("优化贴片天线的轴比带宽和S11")

        self.assertIn("贴片天线", tokens)
        self.assertIn("轴比带宽", tokens)
        self.assertIn("s11", tokens)

    def test_bm25_tokenizer_preserves_english_and_numeric_identifiers(self):
        tokens = _bm25_tokens("GWO optimize patch_antenna at 2.45GHz")

        self.assertEqual(tokens, ["gwo", "optimize", "patch", "antenna", "at", "2", "45ghz"])

    def test_skill_router_selects_antenna_skills_for_real_cst_request(self):
        """Verify central routing selects evidence, geometry, CST, optimizer, and reviewer skills."""
        plan = SkillRouter().build_plan(
            "patch antenna GWO optimize S11 with CST",
            mode="real",
            require_paperwise=True,
            capabilities={
                "paperwise_vector_library_reader": {},
                "skill_packet_protocol": {},
                "antenna_packet_adapter:geometry_contract": {},
                "antenna_packet_adapter:next_iteration_plan": {},
                "optimizer_selector": {},
                "cst_local": {},
            },
        )

        self.assertEqual(plan["decision"], "route_selected")
        self.assertIn("paperwise", plan["selected_owner_skills"])
        self.assertIn("antenna-research-ideation", plan["selected_owner_skills"])
        self.assertIn("cst-control", plan["selected_owner_skills"])
        self.assertIn("antenna-research-reviewer", plan["selected_owner_skills"])
        self.assertIn("e-platform-cst", plan["selected_owner_skills"])
        self.assertEqual(plan["disclosure_level_used"], 2)
        self.assertTrue(all("route_id" in route for route in plan["routes"]))
        self.assertTrue(
            all(route.get("adapter_binding", {}).get("packet_type") != "" for route in plan["routes"])
        )

    def test_skill_router_matches_csv_ground_truth(self):
        """Verify V2.2.2 routing against the 100-row trigger ground truth when available."""
        csv_path = Path(r"C:\Users\30626\Documents\Codex\2026-07-09\skill-skill-2\outputs\antenna_skill_trigger_tests.csv")
        if not csv_path.exists():
            self.skipTest("antenna skill trigger CSV is not available")
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        capabilities = {
            "paperwise_vector_library_reader": {},
            "paperwise_report_reader": {},
            "skill_packet_protocol": {},
            "antenna_packet_adapter:idea_card": {},
            "antenna_packet_adapter:experiment_contract": {},
            "antenna_packet_adapter:geometry_contract": {},
            "antenna_packet_adapter:run_manifest": {},
            "antenna_packet_adapter:claim_assessment": {},
            "antenna_packet_adapter:next_iteration_plan": {},
            "cst_local": {},
            "optimizer_selector": {},
        }
        aliases = {
            "antenna-skills": {"antenna-skills", "antenna_skills"},
            "antenna-research-ideation": {"antenna-research-ideation", "antenna_research_ideation"},
            "antenna-research-idea-advisor": {"antenna-research-idea-advisor", "antenna_research_idea_advisor"},
            "antenna-research-reviewer": {"antenna-research-reviewer", "antenna_research_reviewer"},
            "antenna-claim-experiment-planner": {"antenna-claim-experiment-planner", "antenna_claim_experiment_planner"},
            "antenna-baseline-ablation-planner": {"antenna-baseline-ablation-planner", "antenna_baseline_ablation_planner"},
            "antenna-result-to-claim": {"antenna-result-to-claim", "antenna_result_to_claim"},
            "cst-control": {"cst-control", "cst_control"},
            "paperwise": {"paperwise", "paperwise_vector_library_reader", "paperwise_report_reader"},
            "pdf": {"pdf"},
        }

        def normalize(value: str) -> str:
            return str(value or "").strip().lower().replace("\\\\", "\\")

        def matches(expected: str, selected: set[str]) -> bool:
            expected = normalize(expected)
            return True if not expected else bool(selected & aliases.get(expected, {expected}))

        router = SkillRouter()
        trigger_ok = 0
        primary_ok = 0
        for row in rows:
            plan = router.build_plan(row["input"], mode="mock", capabilities=capabilities)
            selected = {normalize(item) for item in plan["selected_owner_skills"]}
            selected.update(normalize(item) for item in plan["selected_capabilities"])
            expected_trigger = row["expected_trigger"].lower() == "true"
            actual_trigger = plan["decision"] == "route_selected"
            trigger_ok += int(expected_trigger == actual_trigger)
            primary_ok += int((not expected_trigger) or matches(row["expected_primary_skill"], selected))
        self.assertGreaterEqual(trigger_ok / len(rows), 0.90)
        self.assertGreaterEqual(primary_ok / len(rows), 0.85)

    def test_route_review_reflects_false_positive_return_loss_finance(self):
        router = SkillRouter()
        plan = router.build_plan("return loss 是金融模型回撤吗", mode="mock")
        review = router.review_plan(plan)
        self.assertEqual(review["decision"], "reroute")
        self.assertEqual(review["reflection"]["error_type"], "false_positive")
        self.assertTrue(review["reflection"]["reroute_required"])

    def test_route_review_reflects_arbw_sample_sufficiency(self):
        router = SkillRouter()
        plan = router.build_plan("ARBW 只有三个采样点，能不能写进结论", mode="mock")
        review = router.review_plan(plan)
        self.assertEqual(review["decision"], "reroute")
        self.assertEqual(review["reflection"]["error_type"], "false_negative")
        suggested = review["reroute_suggestion"]["suggested_steps"]
        self.assertTrue(any("antenna-result-to-claim" in step.get("required_skills", []) for step in suggested))

    def test_l1_keeps_recent_20_turns(self):
        """验证该运行场景的预期行为。"""
        memory = MemoryManager(l1_recent_turns=20)
        for i in range(25):
            memory.add_l1_turn("s1", "user", str(i))
        self.assertEqual(len(memory.get_l1("s1")), 20)
        self.assertEqual(memory.get_l1("s1")[0]["content"], "5")

    def test_memory_embedding_and_redis_vector_retrieval(self):
        store = RedisStore("redis://127.0.0.1:1/0")
        memory = MemoryManager(store=store)
        memory.store_l2(
            "user",
            {"preference": "wideband patch antenna", "confidence": 0.9},
            provenance={
                "source_type": "conversation",
                "source_id": "message-001",
                "source_ref": "task-001/messages/message-001",
                "source_excerpt": "Prefer wideband patch antennas.",
            },
        )
        vector = embed_text("wideband patch antenna")
        self.assertEqual(len(vector), EMBEDDING_DIM)
        hits = RetrievalEngine(
            memory, minimum_relevance_scores={"l2": 0.0, "l3": 0.0}
        ).retrieve_l2("wideband patch", final_top_k=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["source"], "l2_memory")
        self.assertTrue(hits[0]["provenance"]["available"])
        self.assertEqual(hits[0]["provenance"]["trace_policy"], "llm_decide")
        self.assertEqual(hits[0]["provenance"]["source_id"], "message-001")

    def test_l2_without_source_is_returned_as_not_traceable(self):
        memory = MemoryManager(store=None)
        memory.store_l2("user", {"preference": "wideband patch antenna"})

        hit = RetrievalEngine(
            memory, minimum_relevance_scores={"l2": 0.0, "l3": 0.0}
        ).retrieve_l2("wideband patch", final_top_k=1)[0]

        self.assertEqual(hit["provenance"], {"trace_policy": "llm_decide", "available": False})

    def test_long_term_source_is_stored_outside_l2(self):
        store = RedisStore("redis://127.0.0.1:1/0")
        memory = MemoryManager(store=store)
        source_text = "用户要求：真实 CST 运行前必须审批；先验证 2.45 GHz 矩形贴片的 S11。"
        source_id = memory.store_long_term_source(
            "message-001",
            source_text,
            metadata={"source_type": "conversation", "task_id": "task-001"},
        )

        record = memory.get_long_term_source(source_id)
        self.assertEqual(record["content"], source_text)
        self.assertEqual(record["status"], "pending_l2_extraction")
        self.assertEqual(store.count_keys("mem:l2:*"), 0)

    def test_legacy_l2_record_gets_untraceable_provenance_view(self):
        memory = MemoryManager(store=None)
        memory.l2["legacy"] = {
            "memory_id": "legacy",
            "namespace": "user",
            "status": "active",
            "confidence": 0.7,
            "fact": {"preference": "wideband patch antenna"},
        }

        hit = RetrievalEngine(
            memory, minimum_relevance_scores={"l2": 0.0, "l3": 0.0}
        ).retrieve_l2("wideband patch", final_top_k=1)[0]

        self.assertEqual(hit["provenance"], {"trace_policy": "llm_decide", "available": False})

    def test_retrieval_filters_inactive_vector_candidates(self):
        store = RedisStore("redis://127.0.0.1:1/0")
        memory = MemoryManager(store=store)
        inactive = {
            "memory_id": "inactive-exact",
            "namespace": "test",
            "status": "inactive",
            "confidence": 0.99,
            "fact": {"text": "patch antenna target setting"},
        }
        active = {
            "memory_id": "active-related",
            "namespace": "test",
            "status": "active",
            "confidence": 0.7,
            "fact": {"text": "patch antenna review policy"},
        }
        for record in (inactive, active):
            text = memory._l2_text(record)
            store.set_json(f"mem:l2:test:{record['memory_id']}", record)
            store.upsert_vector("l2", record["memory_id"], text, memory.embed_text(text), record)

        hits = RetrievalEngine(memory).retrieve_l2("patch antenna target setting", final_top_k=5)

        self.assertNotIn("inactive-exact", {item["memory_id"] for item in hits})

    def test_retrieval_confidence_does_not_override_embedding_relevance(self):
        memory = MemoryManager(store=None)
        memory.l2["low-confidence"] = {
            "memory_id": "low-confidence",
            "namespace": "test",
            "status": "active",
            "confidence": 0.1,
            "fact": {"text": "patch antenna target setting"},
        }
        memory.l2["high-confidence"] = {
            "memory_id": "high-confidence",
            "namespace": "test",
            "status": "active",
            "confidence": 0.99,
            "fact": {"text": "financial portfolio weather"},
        }

        hits = RetrievalEngine(memory).retrieve_l2("patch antenna target setting", final_top_k=2)

        self.assertEqual(hits[0]["memory_id"], "low-confidence")

    def test_retrieval_score_is_normalized(self):
        memory = MemoryManager(store=None)
        memory.l2["target"] = {
            "memory_id": "target",
            "namespace": "test",
            "status": "active",
            "confidence": 0.7,
            "fact": {"text": "patch antenna target setting"},
        }

        hit = RetrievalEngine(memory).retrieve_l2("patch antenna target setting", final_top_k=1)[0]

        self.assertGreaterEqual(hit["retrieval_score"], 0.0)
        self.assertLessEqual(hit["retrieval_score"], 1.0)
        self.assertEqual(set(hit["retrieval_components"]), {"embedding"})
        self.assertEqual(hit["retrieval_score"], hit["retrieval_components"]["embedding"])

    def test_retrieval_minimum_score_can_return_fewer_than_top_k(self):
        memory = MemoryManager(store=None)
        memory.l2["target"] = {
            "memory_id": "target",
            "namespace": "test",
            "status": "active",
            "confidence": 0.7,
            "fact": {"text": "patch antenna target setting"},
        }

        hits = RetrievalEngine(
            memory,
            minimum_relevance_scores={"l2": 1.0, "l3": 1.0},
        ).retrieve_l2("patch antenna target setting", final_top_k=5)

        self.assertEqual(hits, [])

    def test_retrieval_does_not_hydrate_all_l2_records(self):
        store = RedisStore("redis://127.0.0.1:1/0")
        writer = MemoryManager(store=store)
        writer.store_l2("user", {"preference": "wideband patch antenna", "confidence": 0.9})
        reader = MemoryManager(store=store)
        hits = RetrievalEngine(
            reader, minimum_relevance_scores={"l2": 0.0, "l3": 0.0}
        ).retrieve_l2("wideband patch", final_top_k=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(reader.l2, {})

    def test_retrieval_rebuilds_missing_vector_index_from_persisted_record(self):
        store = RedisStore("redis://127.0.0.1:1/0")
        record = {
            "memory_id": "persisted-only",
            "namespace": "test",
            "status": "active",
            "confidence": 0.8,
            "fact": {"text": "patch antenna persisted preference"},
        }
        store.set_json("mem:l2:test:persisted-only", record)
        memory = MemoryManager(store=store)

        hits = RetrievalEngine(
            memory, minimum_relevance_scores={"l2": 0.0, "l3": 0.0}
        ).retrieve_l2("patch antenna preference", final_top_k=1)

        self.assertEqual(hits[0]["memory_id"], "persisted-only")
        self.assertIn("persisted-only", store._vector_docs["l2"])

    def test_memory_snapshot_counts_store_without_hydrating_l2(self):
        store = RedisStore("redis://127.0.0.1:1/0")
        writer = MemoryManager(store=store)
        writer.store_l2("user", {"preference": "wideband patch antenna", "confidence": 0.9})
        reader = MemoryManager(store=store)
        snapshot = reader.snapshot()
        self.assertEqual(snapshot["l2_count"], 1)
        self.assertEqual(snapshot["l2_cached"], 0)
        self.assertEqual(reader.l2, {})

    def test_retrieval_does_not_hydrate_all_l3_records(self):
        store = RedisStore("redis://127.0.0.1:1/0")
        writer = MemoryManager(store=store)
        saved = writer.maybe_store_l3_workflow(
            {
                "task_status": "completed",
                "unresolved_failures": 0,
                "graph_step": 4,
                "workflow_quality": 0.95,
                "domain": "antenna",
                "goal_pattern": "wideband_patch",
            }
        )
        self.assertTrue(saved)
        reader = MemoryManager(store=store)
        hits = RetrievalEngine(
            reader, minimum_relevance_scores={"l2": 0.0, "l3": 0.0}
        ).retrieve_l3("wideband patch", top_k=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(reader.l3, {})

    def test_retrieval_pipeline_uses_bm25_50_embedding_15_and_final_top5(self):
        memory = MemoryManager(store=None)
        for index in range(70):
            memory_id = f"record-{index:02d}"
            memory.l2[memory_id] = {
                "memory_id": memory_id,
                "namespace": "test",
                "status": "active",
                "confidence": 0.7,
                "fact": {"text": f"patch antenna target setting variant {index}"},
            }

        hits = RetrievalEngine(
            memory, minimum_relevance_scores={"l2": 0.0, "l3": 0.0}
        ).retrieve_l2("patch antenna target setting", final_top_k=10)

        self.assertEqual(len(hits), 5)
        pipeline = hits[0]["retrieval_pipeline"]
        self.assertEqual(pipeline["bm25_top_k"], 50)
        self.assertEqual(pipeline["embedding_top_k"], 15)
        self.assertEqual(pipeline["candidate_count_before_dedupe"], 65)
        self.assertLessEqual(pipeline["candidate_count_after_dedupe"], 65)
        self.assertEqual(pipeline["final_top_k"], 5)

    def test_skill_packet_validator_rejects_missing_payload_contract(self):
        packet = {
            "schema_version": "1.0",
            "packet_type": "claim_assessment",
            "id": "packet-1",
            "created_by": "test",
            "created_at": "2026-07-11T00:00:00Z",
            "status": "ready",
            "payload": {"claim": "S11 is improved"},
            "artifacts": [],
        }
        with self.assertRaisesRegex(SkillPacketValidationError, "missing required payload fields"):
            SkillPacketValidator().validate(packet, "claim_assessment")

    def test_paperwise_vector_query_uses_project_hybrid_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = PaperWiseAdapter(root)
            kb = Path(root) / "outputs" / ".kb"
            kb.mkdir(parents=True)
            db = kb / "chroma.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE collections (name TEXT)")
            conn.execute("INSERT INTO collections VALUES ('papers')")
            conn.commit()
            conn.close()
            before = db.read_bytes()
            seen = {}

            def fake_query(conn, query, limit, profile, allow_external_embedding):
                seen["query"] = query
                seen["allow_external_embedding"] = allow_external_embedding
                return [{"source": "paperwise_vector_library_project_hybrid", "title": "hit", "score": 0.9}]

            with patch.object(adapter, "_project_hybrid_vector_query_items", side_effect=fake_query):
                result = adapter._vector_library_summary("patch S11", 5, adapter._query_profile("patch S11"), True)

            self.assertEqual(result["retrieval_backend"], "project_hybrid_bm25_50_qwen_15_semantic_rerank")
            self.assertEqual(result["items"][0]["source"], "paperwise_vector_library_project_hybrid")
            self.assertTrue(seen["allow_external_embedding"])
            self.assertEqual(db.read_bytes(), before)

    def test_blackboard_optimistic_lock(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            board = Blackboard(root)
            record = board.create_task("hello")
            board.update(record.task_id, expected_version=record.version, state="running")
            with self.assertRaises(OptimisticLockError):
                board.update(record.task_id, expected_version=0, state="failed")

    def test_scheduler_creates_dynamic_plan_by_default(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(workspace_root=root, logs_root=root)
            registry = CapabilityRegistry()
            registry.register(Capability("mock_run_manifest"))
            scheduler = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                registry,
                AuditLog(root),
                StreamPublisher(),
            )
            state = scheduler.create_paper_plan_task("patch antenna GWO S11")
            self.assertEqual(state["state"], "completed")
            self.assertNotIn("packets", state)
            self.assertEqual(state["nodes"]["skill_router.task_agent"]["status"], "done")
            self.assertEqual(state["nodes"]["skill_router.review_agent"]["status"], "done")
            self.assertEqual(state["nodes"]["central_scheduler.llm_plan_generation"]["status"], "done")
            self.assertEqual(state["nodes"]["central_scheduler.plan_validation"]["status"], "done")
            self.assertEqual(state["task_metadata"]["dynamic_plan"]["source"], "external_llm")
            self.assertEqual(state["task_metadata"]["skill_route_plan"]["decision"], "route_selected")
            self.assertIn("antenna-research-ideation", state["task_metadata"]["skill_route_plan"]["selected_owner_skills"])
            self.assertEqual(state["task_metadata"]["dynamic_plan"]["plan_type"], "dynamic_plan")
            self.assertNotEqual(len(state["task_metadata"]["dynamic_plan"]["steps"]), 7)
            self.assertTrue(state["task_metadata"]["reflection_records"])
            self.assertTrue(
                any(item["artifact_type"] == "skill_route_plan" for item in state["task_metadata"]["artifacts"])
            )
            self.assertTrue(
                any(item["artifact_type"] == "dynamic_plan" for item in state["task_metadata"]["artifacts"])
            )

    def test_scheduler_gate_skips_antenna_adapter_for_non_antenna_task(self):
        """Verify V2.2.2 does not call antenna adapters when routing excludes them."""
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(workspace_root=root, logs_root=root, llm_enabled=False)
            scheduler = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )

            state = scheduler.create_task_from_request("优化这个React组件的性能")

            self.assertEqual(state["state"], "completed")
            self.assertEqual(state["task_metadata"]["skill_route_plan"]["decision"], "no_specialized_skill_needed")
            self.assertEqual(state["task_metadata"]["dynamic_plan"]["plan_type"], "dynamic_plan")
            self.assertFalse(
                any(step.get("required_skills") for step in state["task_metadata"]["dynamic_plan"]["steps"])
            )
            antenna_artifacts = [
                artifact
                for artifact in state["task_metadata"].get("artifacts", [])
                if artifact.get("artifact_type") == "antenna_skill_packet"
            ]
            self.assertEqual(antenna_artifacts, [])
            self.assertFalse(any(event.get("type") == "skill_adapter_called" for event in scheduler.audit.replay(state["task_id"])))

    def test_scheduler_persists_locked_capability_snapshot(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(workspace_root=root, logs_root=root)
            registry = CapabilityRegistry()
            registry.register(Capability("mock_run_manifest"))
            scheduler = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                registry,
                AuditLog(root),
                StreamPublisher(),
            )

            state = scheduler.create_paper_plan_task("patch antenna GWO S11")

            snapshot_path = Path(state["task_metadata"]["capability_snapshot_path"])
            lock_path = Path(state["task_metadata"]["capability_snapshot_lock_path"])
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertTrue(snapshot["locked"])
            self.assertTrue(lock["locked"])
            self.assertEqual(state["capability_snapshot_id"], snapshot["snapshot_id"])
            self.assertEqual(lock["snapshot_id"], snapshot["snapshot_id"])

    def test_general_natural_language_task_does_not_require_paperwise(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(workspace_root=root, logs_root=root, paperwise_root=str(Path(root) / "missing"))
            scheduler = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )

            state = scheduler.create_task_from_request("patch antenna GWO optimize S11")

            self.assertEqual(state["state"], "completed")
            self.assertNotIn("packets", state)
            self.assertFalse(state["task_metadata"]["require_paperwise"])
            self.assertEqual(state["task_metadata"]["dynamic_plan"]["plan_type"], "dynamic_plan")
            self.assertTrue(state["task_metadata"]["dynamic_plan"]["steps"])

    def test_approval_expires_to_blocked(self):
        """验证该运行场景的预期行为。"""
        manager = ApprovalManager(ttl_hours=0)
        request = manager.create("t1", "need approval")
        expired = manager.expire_due()
        self.assertEqual(expired[0].approval_id, request.approval_id)
        self.assertIn(expired[0].status, {"blocked", "abandoned"})

    def test_redis_store_memory_fallback(self):
        """验证该运行场景的预期行为。"""
        store = RedisStore("redis://127.0.0.1:1/0")
        self.assertFalse(store.health()["available"])
        store.set_json("mem:test", {"ok": True})
        self.assertEqual(store.get_json("mem:test")["ok"], True)
        seq = store.xadd("audit:event:t1", {"message": "hello"})
        self.assertEqual(seq, "00000001")
        self.assertEqual(len(store.xrange("audit:event:t1")), 1)

    def test_redis_retention_days_load_from_config(self):
        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "redis:",
                        '  url: "redis://127.0.0.1:1/0"',
                        "  json_retention_days: 11",
                        "  stream_retention_days: 22",
                        "  vector_retention_days: 33",
                    ]
                ),
                encoding="utf-8",
            )

            settings = load_settings(config_path)
            store = RedisStore(
                settings.redis_url,
                json_retention_days=settings.redis_json_retention_days,
                stream_retention_days=settings.redis_stream_retention_days,
                vector_retention_days=settings.redis_vector_retention_days,
            )

            self.assertEqual(store.json_ttl_seconds, 11 * 24 * 60 * 60)
            self.assertEqual(store.stream_ttl_seconds, 22 * 24 * 60 * 60)
            self.assertEqual(store.vector_ttl_seconds, 33 * 24 * 60 * 60)

    def test_checkpoint_manager_fallback(self):
        """验证该运行场景的预期行为。"""
        store = RedisStore("redis://127.0.0.1:1/0")
        checkpoint = CheckpointManager("redis://127.0.0.1:1/0", store)
        health = checkpoint.health()
        self.assertIn("dynamic_checkpoint_available", health)
        self.assertIn("dynamic_checkpoint_error", health)

    def test_paperwise_adapter_reads_report(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            report_dir = Path(root) / "outputs"
            report_dir.mkdir()
            report = report_dir / "paper.md"
            report.write_text("# Patch Antenna\n\nS11 bandwidth gain", encoding="utf-8")
            adapter = PaperWiseAdapter(root)
            self.assertEqual(adapter.inventory()["reports"], 1)
            parsed = adapter.read_report(str(report))
            self.assertTrue(parsed["available"])
            self.assertIn("S11", parsed["metrics"])

    def test_paperwise_adapter_rejects_path_outside_outputs(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            outside = Path(root) / "secret.md"
            outside.write_text("# secret", encoding="utf-8")
            adapter = PaperWiseAdapter(root)
            parsed = adapter.read_report(str(outside))
            self.assertFalse(parsed["available"])
            self.assertEqual(parsed["error"], "path_not_allowed")

    def test_paperwise_evidence_pool_summary_covers_three_sources(self):
        with tempfile.TemporaryDirectory() as root:
            outputs = Path(root) / "outputs"
            paper_dir = outputs / "paper_a"
            paper_dir.mkdir(parents=True)
            (paper_dir / "report.md").write_text("# Patch Antenna Evidence\n\npatch antenna S11 bandwidth gain", encoding="utf-8")
            (paper_dir / "graph_info.json").write_text(
                json.dumps(
                    {
                        "concepts": ["patch antenna", "S11"],
                        "relations": [{"target_title": "Patch antenna S11 evidence", "relation": "supports", "description": "patch antenna S11 evidence relation"}],
                    }
                ),
                encoding="utf-8",
            )
            kb_dir = outputs / ".kb"
            kb_dir.mkdir()
            db_path = kb_dir / "chroma.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("create table embeddings (id text)")
                conn.execute("insert into embeddings values ('chunk-1')")
                conn.execute("create table embedding_metadata (key text, string_value text)")
                conn.execute("insert into embedding_metadata values ('paper_id', 'paper-a')")
                conn.execute("create table embedding_fulltext_search_content (rowid integer, c0 text)")
                conn.execute("insert into embedding_fulltext_search_content values (1, 'patch antenna S11 reproduction evidence chunk')")
                conn.commit()
            finally:
                conn.close()
            gc.collect()

            before = {path: path.stat().st_mtime_ns for path in outputs.rglob("*") if path.is_file()}
            summary = PaperWiseAdapter(root).evidence_pool_summary("patch antenna S11")
            after = {path: path.stat().st_mtime_ns for path in outputs.rglob("*") if path.is_file()}

            self.assertEqual(before, after)
            self.assertTrue(summary["read_only"])
            self.assertEqual(set(summary["sources"]), {"reports", "deep_read_papers", "graph_library"})
            self.assertIn("reproduction", summary["sources"]["deep_read_papers"]["roles"])
            self.assertIn("evidence", summary["sources"]["deep_read_papers"]["roles"])
            self.assertIn("innovation", summary["sources"]["graph_library"]["roles"])
            self.assertIn("relation", summary["sources"]["graph_library"]["roles"])
            for source in summary["sources"].values():
                self.assertEqual(source["status"], "available")
            self.assertTrue(summary["sources"]["deep_read_papers"]["items"])
            self.assertIn("vector_library", summary["internal_sources"])
            self.assertTrue(summary["sources"]["graph_library"]["items"])
            report_item = summary["sources"]["reports"]["items"][0]
            self.assertIn("display_title", report_item)
            self.assertIn("original_title", report_item)
            self.assertRegex(report_item["display_title"], r"[\u4e00-\u9fff]")

    def test_paperwise_evidence_pool_summary_marks_missing_libraries(self):
        with tempfile.TemporaryDirectory() as root:
            outputs = Path(root) / "outputs"
            outputs.mkdir()
            (outputs / "paper.md").write_text("# Report Only", encoding="utf-8")

            summary = PaperWiseAdapter(root).evidence_pool_summary("patch antenna S11")

            self.assertEqual(summary["status"], "insufficient_evidence")
            self.assertEqual(summary["sources"]["deep_read_papers"]["status"], "insufficient_evidence")
            self.assertEqual(summary["internal_sources"]["vector_library"]["status"], "missing")
            self.assertEqual(summary["sources"]["graph_library"]["status"], "missing")
            self.assertEqual(summary["support_level"], "insufficient_evidence")

    def test_paperwise_graph_concepts_without_relations_are_insufficient(self):
        with tempfile.TemporaryDirectory() as root:
            outputs = Path(root) / "outputs"
            paper_dir = outputs / "paper_a"
            paper_dir.mkdir(parents=True)
            (paper_dir / "report.md").write_text("# Patch Antenna Evidence\n\npatch antenna S11 bandwidth gain", encoding="utf-8")
            (paper_dir / "graph_info.json").write_text(json.dumps({"concepts": ["patch antenna"]}), encoding="utf-8")

            summary = PaperWiseAdapter(root).evidence_pool_summary("patch antenna S11")

            graph = summary["sources"]["graph_library"]
            self.assertEqual(graph["status"], "insufficient_evidence")
            self.assertEqual(graph["relation_count"], 0)

    def test_paperwise_graph_relations_must_match_query(self):
        with tempfile.TemporaryDirectory() as root:
            outputs = Path(root) / "outputs"
            paper_dir = outputs / "paper_a"
            paper_dir.mkdir(parents=True)
            (paper_dir / "report.md").write_text("# Patch Antenna Evidence\n\npatch antenna S11 bandwidth gain", encoding="utf-8")
            (paper_dir / "graph_info.json").write_text(
                json.dumps(
                    {
                        "concepts": ["unrelated"],
                        "relations": [{"target_title": "MRI segmentation", "relation": "compares_to", "description": "medical image relation"}],
                    }
                ),
                encoding="utf-8",
            )

            summary = PaperWiseAdapter(root).evidence_pool_summary("patch antenna S11")

            graph = summary["sources"]["graph_library"]
            self.assertEqual(graph["status"], "insufficient_evidence")
            self.assertEqual(graph["relation_count"], 1)
            self.assertEqual(graph["matched_relation_count"], 0)

    def test_paperwise_graph_global_edges_can_match_split_structure_and_metric(self):
        with tempfile.TemporaryDirectory() as root:
            outputs = Path(root) / "outputs" / "graph"
            outputs.mkdir(parents=True)
            (outputs / "graph.json").write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"id": "paper:1", "type": "paper", "label": "Neural Network-Based Optimization of U-Slot Microstrip Antenna"},
                            {"id": "concept:structure", "type": "concept", "label": "U-Slot Microstrip Antenna"},
                            {"id": "concept:metric", "type": "concept", "label": "S11 Bandwidth Optimization"},
                        ],
                        "edges": [
                            {"source": "paper:1", "target": "concept:structure", "type": "uses", "weight": 1.0},
                            {"source": "paper:1", "target": "concept:metric", "type": "uses", "weight": 1.0},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = PaperWiseAdapter(root).evidence_pool_summary("patch antenna S11")

            graph = summary["sources"]["graph_library"]
            self.assertEqual(graph["status"], "available")
            self.assertEqual(graph["matched_relation_count"], 2)
            self.assertEqual(graph["review_requirement"], "llm_semantic_review_required_before_adoption")
            self.assertTrue(graph["items"])

    def test_paperwise_filters_non_patch_medical_hits(self):
        with tempfile.TemporaryDirectory() as root:
            outputs = Path(root) / "outputs"
            paper_dir = outputs / "medical_cnn"
            paper_dir.mkdir(parents=True)
            (paper_dir / "report.md").write_text(
                "# A Novel Convolutional Neural Network Model Based on Beetle Antennae Search Optimization Algorithm for Computerized Tomography Diagnosis\n\n"
                "This paper is about medical image tomography diagnosis. It mentions antennae search optimization but has no patch antenna geometry and no S11 return loss.",
                encoding="utf-8",
            )
            kb_dir = outputs / ".kb"
            kb_dir.mkdir()
            db_path = kb_dir / "chroma.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("create table embeddings (id text)")
                conn.execute("insert into embeddings values ('chunk-1')")
                conn.execute("create table embedding_metadata (key text, string_value text)")
                conn.execute("insert into embedding_metadata values ('paper_id', 'medical')")
                conn.execute("create table embedding_fulltext_search_content (rowid integer, c0 text)")
                conn.execute(
                    "insert into embedding_fulltext_search_content values (1, 'computerized tomography diagnosis neural network antennae search S11')"
                )
                conn.commit()
            finally:
                conn.close()
            gc.collect()

            summary = PaperWiseAdapter(root).evidence_pool_summary("patch antenna S11")

            self.assertEqual(summary["sources"]["reports"]["status"], "insufficient_evidence")
            self.assertEqual(summary["sources"]["deep_read_papers"]["status"], "insufficient_evidence")
            self.assertFalse(summary["sources"]["reports"]["items"])
            self.assertFalse(summary["sources"]["deep_read_papers"]["items"])

    def test_paperwise_profile_extracts_algorithm_structure_and_metric(self):
        adapter = PaperWiseAdapter(".")
        cases = [
            ("GWO patch antenna S11 optimization", ["gwo"], ["patch"], ["s11"]),
            ("PSO antenna array gain", ["pso"], ["array"], ["gain"]),
            ("ANN MIMO antenna bandwidth", ["ann"], ["mimo"], ["bandwidth"]),
            ("GA slot antenna return loss", ["ga"], ["slot"], ["s11"]),
            ("differential evolution metasurface efficiency", ["de"], ["metasurface"], ["efficiency"]),
        ]
        for query, algorithms, structures, objectives in cases:
            with self.subTest(query=query):
                profile = adapter._query_profile(query)
                self.assertEqual(profile["algorithms"], algorithms)
                self.assertEqual(profile["structures"], structures)
                self.assertEqual(profile["objectives"], objectives)
                self.assertTrue(profile["requires_algorithm_match"])
                self.assertFalse(profile["requires_structure_match"])
                self.assertTrue(profile["requires_objective_match"])

    def test_paperwise_relevance_ranks_four_signals_without_structure_veto(self):
        adapter = PaperWiseAdapter(".")
        profile = adapter._query_profile("GWO patch antenna S11 optimization 6 parameters")

        accepted = adapter._relevance("GWO microstrip patch antenna S11 return loss 7 parameters feed ground", profile)
        missing_algorithm = adapter._relevance("microstrip patch antenna S11 return loss 6 parameters feed ground", profile)
        missing_structure = adapter._relevance("GWO monopole antenna S11 return loss 6 parameters feed ground", profile)
        missing_metric = adapter._relevance("GWO microstrip patch antenna bandwidth 6 parameters feed ground", profile)
        parameter_far = adapter._relevance("GWO microstrip patch antenna S11 return loss 12 parameters feed ground", profile)

        self.assertTrue(accepted["accepted"])
        self.assertTrue(accepted["matched"]["parameter_count_match"])
        self.assertEqual(missing_algorithm["reason"], "match_80")
        self.assertEqual(missing_algorithm["match_percent"], 80)
        self.assertEqual(missing_structure["reason"], "match_80")
        self.assertEqual(missing_metric["reason"], "match_80")
        self.assertEqual(missing_metric["match_percent"], 80)
        self.assertEqual(parameter_far["reason"], "match_80")
        self.assertFalse(parameter_far["matched"]["parameter_count_match"])

    def test_paperwise_mimo_query_without_structure_is_second_tier(self):
        adapter = PaperWiseAdapter(".")
        profile = adapter._query_profile("ANN MIMO antenna bandwidth")

        accepted = adapter._relevance("ANN model optimizes a MIMO antenna bandwidth with ports and feed", profile)
        radar_only = adapter._relevance("ANN model for distributed MIMO radar network bandwidth allocation", profile)

        self.assertTrue(accepted["accepted"])
        self.assertTrue(radar_only["accepted"])
        self.assertEqual(radar_only["reason"], "match_80")

    def test_paperwise_short_algorithm_aliases_use_word_boundaries(self):
        adapter = PaperWiseAdapter(".")

        ga_profile = adapter._query_profile("GA slot antenna return loss")
        self.assertEqual(ga_profile["algorithms"], ["ga"])
        weak = adapter._relevance("slot antenna return loss gain design", ga_profile)
        self.assertTrue(weak["accepted"])
        self.assertEqual(weak["match_tier"], "match_80")
        self.assertTrue(adapter._relevance("GA slot antenna return loss", ga_profile)["accepted"])

        de_profile = adapter._query_profile("DE metasurface efficiency")
        self.assertEqual(de_profile["algorithms"], ["de"])
        de_weak = adapter._relevance("metasurface efficiency design", de_profile)
        self.assertTrue(de_weak["accepted"])
        self.assertEqual(de_weak["match_tier"], "match_80")
        self.assertTrue(adapter._relevance("DE metasurface efficiency", de_profile)["accepted"])

    def test_paperwise_combination_query_filters_report_vector_and_graph(self):
        with tempfile.TemporaryDirectory() as root:
            outputs = Path(root) / "outputs"
            good_dir = outputs / "good"
            bad_dir = outputs / "bad"
            good_dir.mkdir(parents=True)
            bad_dir.mkdir(parents=True)
            (good_dir / "report.md").write_text(
                "# GWO Patch Antenna S11 Optimization\n\n"
                "A grey wolf optimizer tunes a microstrip patch antenna for S11 return loss. "
                "The geometry includes feed, substrate, ground plane, and patch dimensions.",
                encoding="utf-8",
            )
            (good_dir / "graph_info.json").write_text(
                json.dumps(
                    {
                        "concepts": ["GWO", "patch antenna", "S11"],
                        "relations": [
                            {
                                "target_title": "GWO patch antenna S11",
                                "relation": "optimizes",
                                "description": "grey wolf optimizer improves S11 return loss of a patch antenna",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (bad_dir / "report.md").write_text(
                "# PSO Monopole Gain Optimization\n\n"
                "Particle swarm optimization tunes a monopole antenna for gain.",
                encoding="utf-8",
            )
            (bad_dir / "graph_info.json").write_text(
                json.dumps(
                    {
                        "concepts": ["PSO", "monopole", "gain"],
                        "relations": [
                            {
                                "target_title": "PSO monopole gain",
                                "relation": "optimizes",
                                "description": "particle swarm optimization improves gain of a monopole antenna",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            kb_dir = outputs / ".kb"
            kb_dir.mkdir()
            db_path = kb_dir / "chroma.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("create table embeddings (id text)")
                conn.execute("insert into embeddings values ('good-chunk')")
                conn.execute("insert into embeddings values ('bad-chunk')")
                conn.execute("create table embedding_metadata (key text, string_value text)")
                conn.execute("insert into embedding_metadata values ('paper_id', 'combo')")
                conn.execute("create table embedding_fulltext_search_content (rowid integer, c0 text)")
                conn.execute(
                    "insert into embedding_fulltext_search_content values (1, 'GWO grey wolf optimizer microstrip patch antenna S11 return loss feed ground')"
                )
                conn.execute(
                    "insert into embedding_fulltext_search_content values (2, 'PSO monopole antenna gain optimization')"
                )
                conn.commit()
            finally:
                conn.close()
            gc.collect()

            summary = PaperWiseAdapter(root).evidence_pool_summary("GWO patch antenna S11 optimization", limit=5)

            self.assertEqual(summary["sources"]["reports"]["status"], "available")
            self.assertEqual(summary["sources"]["deep_read_papers"]["status"], "available")
            self.assertEqual(summary["sources"]["graph_library"]["status"], "available")
            for source in summary["sources"].values():
                joined = json.dumps(source["items"], ensure_ascii=False).lower()
                self.assertIn("gwo", joined)
                self.assertNotIn("pso monopole", joined)

    def test_scheduler_blocks_when_paperwise_missing(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(workspace_root=root, logs_root=root, paperwise_root=str(Path(root) / "missing_paperwise"))
            registry = CapabilityRegistry()
            registry.register(Capability("mock_run_manifest"))
            scheduler = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                registry,
                AuditLog(root),
                StreamPublisher(),
            )
            state = scheduler.create_paper_plan_task("patch antenna GWO S11")
            self.assertEqual(state["state"], "failed")
            self.assertNotIn("packets", state)

    def test_scheduler_llm_enabled_requires_external_approval(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(workspace_root=root, logs_root=root, llm_enabled=True)
            scheduler = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )
            state = scheduler.create_paper_plan_task("patch antenna GWO S11")
            self.assertEqual(state["state"], "waiting_approval")
            self.assertIn("external_llm_approval_required", state["blockers"][0]["type"])
            self.assertNotIn("packets", state)

    def test_scheduler_ignores_untrusted_external_approval_flag(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(workspace_root=root, logs_root=root, llm_enabled=True)
            scheduler = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )
            state = scheduler.create_paper_plan_task("patch antenna GWO S11", external_llm_approved=True)
            self.assertEqual(state["state"], "waiting_approval")
            self.assertNotIn("packets", state)

    def test_blackboard_loads_latest_snapshot(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            board = Blackboard(root)
            record = board.create_task("hello")
            board.update(record.task_id, state="running")
            loaded = Blackboard(root).load_task(record.task_id)
            self.assertEqual(loaded.state, "running")

    def test_audit_replay_uses_store_when_available(self):
        """验证该运行场景的预期行为。"""
        store = RedisStore("redis://127.0.0.1:1/0")
        audit = AuditLog(tempfile.gettempdir(), store=store)
        audit.emit("t1", {"message": "hello"})
        self.assertEqual(audit.replay("t1")[0]["message"], "hello")

    def test_capability_registry_marks_cst_high_risk(self):
        """验证该运行场景的预期行为。"""
        registry = CapabilityRegistry()
        self.assertEqual(registry._infer_risk("cst_local", r"E:\antenna skills\simulator_skills\cst_local"), "high")
        self.assertEqual(registry._infer_risk("gwo_search", r"E:\antenna skills\optimizer_skills\gwo_search"), "medium")

    def test_capability_registry_extracts_meta_schema_and_workflow(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            skill_dir = Path(root) / "optimizer_skills" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "meta.json").write_text(
                '{"name":"demo_optimizer","version":"2.0","inputs":[{"name":"population","type":"integer","required":true}],"outputs":{"best_s11":{"type":"number"}}}',
                encoding="utf-8",
            )
            workflows = Path(root) / "workflows"
            workflows.mkdir()
            (workflows / "default_workflow.json").write_text('{"name":"default","steps":["a","b"]}', encoding="utf-8")

            registry = CapabilityRegistry()
            capabilities = registry.scan({"e_platform_root": root})

            cap = capabilities["demo_optimizer"]
            self.assertEqual(cap.version, "2.0")
            self.assertEqual(cap.risk_level, "medium")
            self.assertIn("population", cap.input_schema["properties"])
            self.assertIn("best_s11", cap.output_schema["properties"])
            self.assertIn("workflow:default", capabilities)

    def test_capability_registry_registers_antenna_packet_adapters(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            adapter_dir = Path(root) / "antenna-idea-card"
            adapter_dir.mkdir()
            (adapter_dir / "packet_adapter.py").write_text("# adapter", encoding="utf-8")

            registry = CapabilityRegistry()
            capabilities = registry.scan({"antenna_skills_root": root})

            for packet_type in PACKET_TYPES:
                self.assertIn(f"antenna_packet_adapter:{packet_type}", capabilities)
            self.assertTrue(capabilities["antenna_packet_adapter:idea_card"].metadata["available"])

    def test_audit_maintenance_sets_stream_ttl_and_writes_manifest(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            store = RedisStore("redis://127.0.0.1:1/0")
            for index in range(5):
                store.xadd("audit:event:t1", {"message": f"event {index}"})
            manager = MaintenanceManager(root, store=store, max_stream_events=2)
            result = manager.run_audit_maintenance()
            self.assertTrue(result["success"])
            self.assertEqual(len(store.xrange("audit:event:t1")), 2)
            self.assertTrue(list((Path(root) / "maintenance").glob("*.json")))


    def test_v224_dynamic_routes_have_initial_active_history_and_step_contexts(self):
        """Verify V2.2.4 dynamic skill routing is step-scoped, not fixed at task creation."""
        with tempfile.TemporaryDirectory() as root:
            old_runtime_llm_enabled = runtime_llm_config.enabled
            runtime_llm_config.enabled = False
            settings = Settings(workspace_root=root, logs_root=root, llm_enabled=False)
            try:
                scheduler = Scheduler(
                    settings,
                    Blackboard(root),
                    MemoryManager(),
                    CapabilityRegistry(),
                    AuditLog(root),
                    StreamPublisher(),
                )

                state = scheduler.create_task_from_request("patch antenna GWO S11 claim support geometry")
                metadata = state["task_metadata"]
                dynamic_plan = metadata["dynamic_plan"]
                steps = dynamic_plan["steps"]
                contexts = metadata["step_skill_contexts"]

                self.assertEqual(state["state"], "completed")
                self.assertEqual(metadata["initial_skill_route_plan"]["plan_type"], "skill_route_plan")
                self.assertEqual(metadata["active_skill_route_plan"]["plan_type"], "skill_route_plan")
                self.assertGreaterEqual(len(metadata["skill_route_plan_history"]), 1 + len(steps))
                self.assertEqual(len(contexts), len(steps))

                context_by_step = {item["step_id"]: item for item in contexts}
                global_review_steps = [step for step in steps if step["global_review_required"]]
                self.assertTrue(global_review_steps)
                self.assertLess(len(global_review_steps), len(steps))
                for step in steps:
                    self.assertIn("module_review_agent", step)
                    self.assertIn("callable_skills", step)
                    self.assertIn("candidate_skills", step)
                    self.assertIn("step_skill_context", step)
                    self.assertIn(step["step_id"], context_by_step)
                    context = context_by_step[step["step_id"]]
                    self.assertEqual(context["context_type"], "step_skill_context")
                    self.assertEqual(context["plan_version"], dynamic_plan["plan_version"])
                    self.assertIn("route_plan", context)
                    self.assertEqual(context["confidence_policy"]["callable_min"], 0.70)
                    self.assertEqual(context["confidence_policy"]["candidate_min"], 0.45)
                    self.assertIn("review_feedback_score", context["score_formula"])
            finally:
                runtime_llm_config.enabled = old_runtime_llm_enabled

    def test_v224_dynamic_steps_record_task_module_global_subagents(self):
        """Verify each dynamic step records task, module-review, and global-review subagent results."""
        with tempfile.TemporaryDirectory() as root:
            old_runtime_llm_enabled = runtime_llm_config.enabled
            runtime_llm_config.enabled = False
            settings = Settings(workspace_root=root, logs_root=root, llm_enabled=False)
            try:
                scheduler = Scheduler(
                    settings,
                    Blackboard(root),
                    MemoryManager(),
                    CapabilityRegistry(),
                    AuditLog(root),
                    StreamPublisher(),
                )

                state = scheduler.create_task_from_request("patch antenna GWO S11 claim support geometry")
                metadata = state["task_metadata"]
                steps = metadata["dynamic_plan"]["steps"]
                subagent_records = metadata["subagent_records"]
                roles = {record["agent_role"] for record in subagent_records}

                self.assertEqual(roles, {"task", "module_review", "global_review"})
                self.assertEqual(sum(1 for record in subagent_records if record["agent_role"] == "task"), len(steps))
                self.assertEqual(sum(1 for record in subagent_records if record["agent_role"] == "module_review"), len(steps))
                expected_global_reviews = sum(bool(step["global_review_required"]) for step in steps)
                self.assertEqual(sum(1 for record in subagent_records if record["agent_role"] == "global_review"), expected_global_reviews)
                self.assertEqual(len(metadata["module_review_records"]), len(steps))
                self.assertEqual(len(metadata["global_review_records"]), expected_global_reviews)
                self.assertEqual(len(metadata["central_decisions"]), len(steps))
                for step in steps:
                    self.assertTrue(step.get("task_subagent_result_id"))
                    self.assertTrue(step.get("module_review_result_id"))
                    self.assertEqual(bool(step.get("global_review_result_id")), bool(step["global_review_required"]))
                    self.assertIn(
                        step.get("central_decision"),
                        {"pass_next_step", "revise_current_step", "refresh_skill_route", "reroute_plan", "block_task", "wait_user"},
                    )
            finally:
                runtime_llm_config.enabled = old_runtime_llm_enabled

    def test_v224_candidate_48_percent_skill_is_not_callable_and_does_not_open_gate(self):
        """Verify weak 45%-70% matches remain candidates and cannot call adapters."""
        plan = SkillRouter().build_plan("patch antenna", mode="mock")
        candidates = {route["owner_skill"]: route for route in plan["candidate_skills"]}

        self.assertIn("antenna-research-ideation", candidates)
        candidate = candidates["antenna-research-ideation"]
        self.assertGreaterEqual(candidate["confidence"], 0.45)
        self.assertLess(candidate["confidence"], 0.70)
        self.assertEqual(candidate["route_class"], "candidate")
        self.assertFalse(candidate["call_allowed"])
        self.assertNotIn("antenna-research-ideation", plan["selected_owner_skills"])

        decision = SkillExecutorGate().check("task-1", "geometry_contract", plan, step_id="step_003_geometry", plan_version=1)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "packet_type_not_selected_by_route")

    def test_v224_gate_token_expires_on_plan_version_or_step_change(self):
        """Verify gate tokens are bound to task, plan_version, step_id, and packet_type."""
        plan = SkillRouter().build_plan("patch antenna geometry", mode="mock")
        gate = SkillExecutorGate()
        decision = gate.check("task-1", "geometry_contract", plan, step_id="step_003_geometry", plan_version=1)

        self.assertTrue(decision.allowed)
        token = decision.token
        self.assertEqual(token["plan_version"], 1)
        self.assertEqual(token["step_id"], "step_003_geometry")
        self.assertTrue(token["expires_when_plan_changes"])
        gate.validate_token("task-1", "geometry_contract", plan, token, step_id="step_003_geometry", plan_version=1)
        with self.assertRaisesRegex(PermissionError, "skill_gate_token_plan_version_stale"):
            gate.validate_token("task-1", "geometry_contract", plan, token, step_id="step_003_geometry", plan_version=2)
        with self.assertRaisesRegex(PermissionError, "skill_gate_token_step_mismatch"):
            gate.validate_token("task-1", "geometry_contract", plan, token, step_id="step_004_parse", plan_version=1)

    def test_v224_module_review_block_cannot_be_passed_by_central_decision(self):
        """Verify module-review block has veto power over central pass decisions."""
        central_decision = Scheduler._central_step_decision(
            {"decision": "block", "blocking_findings": [{"reason": "artifact_missing"}]},
            {"decision": "pass"},
        )

        self.assertEqual(central_decision, "block_task")

    def test_v224_task_state_exposes_callable_candidate_and_step_contexts(self):
        """Verify task metadata exposes V2.2.4 route visibility for API/frontend use."""
        with tempfile.TemporaryDirectory() as root:
            scheduler = Scheduler(
                Settings(workspace_root=root, logs_root=root),
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )

            state = scheduler.create_task_from_request("patch antenna")
            metadata = state["task_metadata"]
            step = metadata["dynamic_plan"]["steps"][0]

            self.assertIn("initial_skill_route_plan", metadata)
            self.assertIn("active_skill_route_plan", metadata)
            self.assertIn("skill_route_plan_history", metadata)
            self.assertIn("step_skill_contexts", metadata)
            self.assertIn("callable_skills", step)
            self.assertIn("candidate_skills", step)
            self.assertTrue(metadata["step_skill_contexts"])
            self.assertTrue(metadata["step_skill_contexts"][0]["candidate_skills"])
            self.assertTrue(
                all(item["confidence"] < 0.70 for item in metadata["step_skill_contexts"][0]["candidate_skills"])
            )

    def test_central_comment_replies_without_plan_or_route_mutation(self):
        """Verify central comments are handled immediately without changing plan or route state."""
        with tempfile.TemporaryDirectory() as root:
            old_runtime_llm_enabled = runtime_llm_config.enabled
            runtime_llm_config.enabled = False
            try:
                memory = MemoryManager()
                scheduler = Scheduler(
                    Settings(workspace_root=root, logs_root=root, llm_enabled=False),
                    Blackboard(root),
                    memory,
                    CapabilityRegistry(),
                    AuditLog(root),
                    StreamPublisher(),
                )
                state = scheduler.create_task_from_request("patch antenna S11 geometry")
                task_id = state["task_id"]
                before = scheduler.blackboard.get(task_id).task_metadata
                before_plan = copy.deepcopy(before["dynamic_plan"])
                before_route_history = copy.deepcopy(before["skill_route_plan_history"])
                before_statuses = {step["step_id"]: step.get("status") for step in before_plan["steps"]}

                after = scheduler.send_central_message(
                    task_id,
                    {
                        "intent": "ask_question",
                        "target_step_id": before_plan["steps"][0]["step_id"],
                        "message": "请解释当前证据是否足够。",
                    },
                )
                metadata = after["task_metadata"]
                reply = metadata["last_central_agent_reply"]

                self.assertEqual(reply["action"], "reply_only")
                self.assertFalse(reply["plan_mutation_allowed"])
                self.assertFalse(reply["skill_route_mutation_allowed"])
                self.assertEqual(metadata["dynamic_plan"], before_plan)
                self.assertEqual(metadata["skill_route_plan_history"], before_route_history)
                self.assertEqual({step["step_id"]: step.get("status") for step in metadata["dynamic_plan"]["steps"]}, before_statuses)
                self.assertIn("central_agent_replies", metadata)
                self.assertIn("central_action_records", metadata)
                self.assertEqual(memory.get_l1(task_id)[-1]["role"], "assistant")
            finally:
                runtime_llm_config.enabled = old_runtime_llm_enabled

    def test_central_reroute_increments_plan_and_refreshes_step_routes_only(self):
        """Verify central reroute mutates plan/routing metadata without executing steps or CST."""
        with tempfile.TemporaryDirectory() as root:
            old_runtime_llm_enabled = runtime_llm_config.enabled
            runtime_llm_config.enabled = False
            try:
                scheduler = Scheduler(
                    Settings(workspace_root=root, logs_root=root, llm_enabled=False),
                    Blackboard(root),
                    MemoryManager(),
                    CapabilityRegistry(),
                    AuditLog(root),
                    StreamPublisher(),
                )
                state = scheduler.create_task_from_request("patch antenna S11 geometry")
                task_id = state["task_id"]
                before_record = scheduler.blackboard.get(task_id)
                before_metadata = before_record.task_metadata
                before_version = before_metadata["dynamic_plan"]["plan_version"]
                before_route_count = len(before_metadata["skill_route_plan_history"])
                before_history_count = len(before_metadata["dynamic_plan_history"])
                before_nodes = set(before_record.nodes)
                target_step_id = before_metadata["dynamic_plan"]["steps"][0]["step_id"]

                after = scheduler.send_central_message(
                    task_id,
                    {
                        "intent": "request_reroute",
                        "target_step_id": target_step_id,
                        "message": "证据链需要重新路由当前步骤，但不要执行 CST。",
                    },
                )
                metadata = after["task_metadata"]
                reply = metadata["last_central_agent_reply"]

                self.assertEqual(reply["action"], "reroute_plan")
                self.assertTrue(reply["plan_mutation_allowed"])
                self.assertTrue(reply["skill_route_mutation_allowed"])
                self.assertEqual(metadata["dynamic_plan"]["plan_version"], before_version + 1)
                self.assertGreater(len(metadata["dynamic_plan_history"]), before_history_count)
                self.assertGreater(len(metadata["skill_route_plan_history"]), before_route_count)
                self.assertTrue(reply["changed"]["changed_steps"])
                self.assertEqual(set(after["nodes"]), before_nodes)
                self.assertFalse(any("cst_run" in node.lower() for node in set(after["nodes"]) - before_nodes))
            finally:
                runtime_llm_config.enabled = old_runtime_llm_enabled

    def test_central_revision_only_marks_target_step_revise(self):
        """Verify request_revision only changes the target step status."""
        with tempfile.TemporaryDirectory() as root:
            old_runtime_llm_enabled = runtime_llm_config.enabled
            runtime_llm_config.enabled = False
            try:
                scheduler = Scheduler(
                    Settings(workspace_root=root, logs_root=root, llm_enabled=False),
                    Blackboard(root),
                    MemoryManager(),
                    CapabilityRegistry(),
                    AuditLog(root),
                    StreamPublisher(),
                )
                state = scheduler.create_task_from_request("patch antenna S11 geometry")
                task_id = state["task_id"]
                before_record = scheduler.blackboard.get(task_id)
                before_metadata = before_record.task_metadata
                before_version = before_metadata["dynamic_plan"]["plan_version"]
                before_route_history = copy.deepcopy(before_metadata["skill_route_plan_history"])
                before_nodes = copy.deepcopy(before_record.nodes)
                target_step_id = before_metadata["dynamic_plan"]["steps"][0]["step_id"]

                after = scheduler.send_central_message(
                    task_id,
                    {
                        "intent": "request_revision",
                        "target_step_id": target_step_id,
                        "message": "这个步骤证据不足，请标记为需要修改。",
                    },
                )
                metadata = after["task_metadata"]
                reply = metadata["last_central_agent_reply"]
                statuses = {step["step_id"]: step.get("status") for step in metadata["dynamic_plan"]["steps"]}

                self.assertEqual(reply["action"], "revise_current_step")
                self.assertTrue(reply["plan_mutation_allowed"])
                self.assertFalse(reply["skill_route_mutation_allowed"])
                self.assertEqual(statuses[target_step_id], "revise")
                self.assertEqual(metadata["dynamic_plan"]["plan_version"], before_version)
                self.assertEqual(metadata["skill_route_plan_history"], before_route_history)
                self.assertEqual(after["nodes"], before_nodes)
                self.assertTrue(reply["changed"]["changed_steps"])
            finally:
                runtime_llm_config.enabled = old_runtime_llm_enabled

    def test_v2_real_cst_waits_for_approval_and_completes_with_simulated_run(self):
        """Verify the V2.0 real-mode state machine and subagent trace."""
        with tempfile.TemporaryDirectory() as root:
            old_runtime_llm_enabled = runtime_llm_config.enabled
            runtime_llm_config.enabled = False
            e_root = Path(root) / "e_platform"
            cst_root = Path(root) / "cst"
            results_root = Path(root) / "results"
            (e_root / "simulator_skills" / "cst_local").mkdir(parents=True)
            (e_root / "simulator_skills" / "cst_local" / "adapter.py").write_text("# fixture", encoding="utf-8")
            (e_root / "simulator_skills" / "cst_local" / "official.py").write_text("# fixture", encoding="utf-8")
            (cst_root / "AMD64" / "python").mkdir(parents=True)
            (cst_root / "AMD64" / "python" / "python.exe").write_text("", encoding="utf-8")
            (cst_root / "AMD64" / "python_cst_libraries").mkdir(parents=True)
            cst_control = Path(root) / "cst_control.py"
            cst_control.write_text("# fixture", encoding="utf-8")
            settings = Settings(workspace_root=root, logs_root=root, e_platform_root=str(e_root), e_results_root=str(results_root), llm_enabled=False)
            scheduler = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )
            scheduler.cst_real = CstRealRunAdapter(e_platform_root=e_root, e_results_root=results_root, cst_root=cst_root)
            scheduler.cst_real.CST_CONTROL_SCRIPT = cst_control

            try:
                created = scheduler.create_real_cst_single_run_task(
                    {"user_input": "patch antenna S11", "simulate_cst": True, "parameters": {"w": 10}}
                )
            finally:
                runtime_llm_config.enabled = old_runtime_llm_enabled
            self.assertEqual(created["state"], "waiting_approval")
            self.assertIn(f"real_cst:{created['task_id']}", created["approvals"])
            approval = created["approvals"][f"real_cst:{created['task_id']}"]
            self.assertEqual(approval["task_id"], created["task_id"])
            self.assertEqual(approval["step_id"], "step_003_cst_run")
            self.assertEqual(approval["plan_id"], created["task_metadata"]["dynamic_plan"]["plan_id"])
            self.assertEqual(approval["plan_version"], created["task_metadata"]["dynamic_plan"]["plan_version"])
            self.assertEqual(approval["request_hash"], created["task_metadata"]["request_hash"])
            self.assertEqual(created["task_metadata"]["current_stage"], "step_002_approval")
            dynamic_plan = created["task_metadata"]["dynamic_plan"]
            self.assertEqual(dynamic_plan["mode"], "real")
            step_ids = {step["step_id"] for step in dynamic_plan["steps"]}
            self.assertTrue(
                {
                    "step_001_preflight",
                    "step_002_approval",
                    "step_003_cst_run",
                    "step_004_parse",
                    "step_005_report",
                }
                <= step_ids
            )
            cst_step = next(step for step in dynamic_plan["steps"] if step["step_id"] == "step_003_cst_run")
            self.assertIn("approval", cst_step["gate_condition"])
            self.assertEqual(cst_step["action"], "cst_run")
            self.assertEqual(cst_step["handler"], "cst_run_task_agent")
            route_plan = created["task_metadata"]["skill_route_plan"]
            self.assertIn("cst-control", route_plan["selected_owner_skills"])
            self.assertIn("e-platform-cst", route_plan["selected_owner_skills"])
            self.assertIn("antenna-research-reviewer", route_plan["selected_owner_skills"])
            self.assertEqual(created["nodes"]["skill_router.task_agent"]["status"], "done")
            self.assertEqual(created["nodes"]["skill_router.review_agent"]["status"], "done")

            runtime_llm_config.enabled = False
            try:
                completed = scheduler.approve_real_cst_task(created["task_id"])
            finally:
                runtime_llm_config.enabled = old_runtime_llm_enabled

            self.assertEqual(completed["state"], "completed")
            repeated = scheduler.approve_real_cst_task(created["task_id"])
            self.assertEqual(repeated["state"], "completed")
            self.assertEqual(repeated["approvals"][f"real_cst:{created['task_id']}"]["run_count"], 1)
            self.assertEqual(completed["task_metadata"]["mode"], "real")
            self.assertEqual(completed["task_metadata"]["current_stage"], "completed")
            evidence_pool = completed["task_metadata"]["evidence_pool_summary"]
            self.assertEqual(set(evidence_pool["sources"]), {"reports", "deep_read_papers", "graph_library"})
            self.assertIn("reproduction", evidence_pool["sources"]["deep_read_papers"]["roles"])
            self.assertIn("innovation", evidence_pool["sources"]["graph_library"]["roles"])
            self.assertTrue(evidence_pool["read_only"])
            self.assertEqual(evidence_pool["retrieval_agent"], "evidence_retrieval_task_agent")
            self.assertEqual(evidence_pool["review_agent"], "evidence_relevance_review_agent")
            self.assertEqual(evidence_pool["review_mode"], "local_react_fallback_with_graph_llm_required")
            self.assertIn("gate_summary", evidence_pool)
            self.assertIn("accelerator_policy", evidence_pool)
            self.assertIn("react_review", evidence_pool)
            self.assertTrue(
                any(artifact["artifact_type"] == "paperwise_evidence_pool" for artifact in completed["task_metadata"]["artifacts"])
            )
            self.assertGreater(completed["task_metadata"]["results"]["metrics"]["bandwidth_10db"], 0)
            for node_id in [
                "skill_router.task_agent",
                "skill_router.review_agent",
                "step_000_evidence.evidence_retrieval_task_agent",
                "step_000_evidence.evidence_retrieval_review_agent",
                "step_001_preflight.preflight_task_agent",
                "step_001_preflight.preflight_review_agent",
                "step_002_approval.approval_task_agent",
                "step_002_approval.approval_review_agent",
                "step_003_cst_run.cst_run_task_agent",
                "step_003_cst_run.cst_run_review_agent",
                "step_004_parse.result_parse_task_agent",
                "step_004_parse.result_parse_review_agent",
                "step_005_report.report_task_agent",
                "step_005_report.report_review_agent",
            ]:
                self.assertEqual(completed["nodes"][node_id]["status"], "done")
            self.assertTrue(completed["task_metadata"]["reports"])
            report_text = Path(completed["task_metadata"]["reports"][0]["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("PaperWise Evidence Pool", report_text)
            self.assertIn("deep_read_papers purpose", report_text)
            self.assertIn("graph_library purpose", report_text)
            self.assertNotIn("packets", completed)
            recorded_step_ids = {
                item.get("step_id") for item in completed["task_metadata"]["module_review_records"]
            }
            self.assertTrue(
                {
                    "step_001_preflight",
                    "step_002_approval",
                    "step_003_cst_run",
                    "step_004_parse",
                    "step_005_report",
                }
                <= recorded_step_ids
            )

    def test_v2_real_cst_creation_does_not_call_runtime_llm_without_approval(self):
        """Creating a real CST task must not block on external LLM before approval."""
        with tempfile.TemporaryDirectory() as root:
            old_enabled = runtime_llm_config.enabled
            old_base_url = runtime_llm_config.base_url
            old_api_key = runtime_llm_config.api_key
            old_model = runtime_llm_config.model_name
            runtime_llm_config.enabled = True
            runtime_llm_config.base_url = "https://example.invalid/v1"
            runtime_llm_config.api_key = "test-key"
            runtime_llm_config.model_name = "test-model"
            try:
                e_root = Path(root) / "e_platform"
                cst_root = Path(root) / "cst"
                results_root = Path(root) / "results"
                (e_root / "simulator_skills" / "cst_local").mkdir(parents=True)
                (e_root / "simulator_skills" / "cst_local" / "adapter.py").write_text("# fixture", encoding="utf-8")
                (e_root / "simulator_skills" / "cst_local" / "official.py").write_text("# fixture", encoding="utf-8")
                (cst_root / "AMD64" / "python").mkdir(parents=True)
                (cst_root / "AMD64" / "python" / "python.exe").write_text("", encoding="utf-8")
                (cst_root / "AMD64" / "python_cst_libraries").mkdir(parents=True)
                cst_control = Path(root) / "cst_control.py"
                cst_control.write_text("# fixture", encoding="utf-8")
                settings = Settings(
                    workspace_root=root,
                    logs_root=root,
                    e_platform_root=str(e_root),
                    e_results_root=str(results_root),
                    llm_enabled=False,
                )
                scheduler = Scheduler(
                    settings,
                    Blackboard(root),
                    MemoryManager(),
                    CapabilityRegistry(),
                    AuditLog(root),
                    StreamPublisher(),
                )
                scheduler.cst_real = CstRealRunAdapter(e_platform_root=e_root, e_results_root=results_root, cst_root=cst_root)
                scheduler.cst_real.CST_CONTROL_SCRIPT = cst_control

                def fail_if_called(*_args, **_kwargs):
                    raise AssertionError("runtime LLM should not be called without task-level approval")

                scheduler._runtime_llm_review_paperwise_evidence = fail_if_called
                created = scheduler.create_real_cst_single_run_task(
                    {"user_input": "patch antenna S11", "simulate_cst": True, "parameters": {"w": 10}}
                )

                self.assertEqual(created["state"], "waiting_approval")
                llm_review = created["task_metadata"]["evidence_pool_summary"]["llm_review"]
                self.assertFalse(llm_review["attempted"])
                self.assertEqual(llm_review["error"], "external_llm_not_approved")
            finally:
                runtime_llm_config.enabled = old_enabled
                runtime_llm_config.base_url = old_base_url
                runtime_llm_config.api_key = old_api_key
                runtime_llm_config.model_name = old_model

    def test_v2_real_cst_preflight_missing_input_waits_for_user(self):
        """Missing protected run inputs pause the parent graph for user action."""
        with tempfile.TemporaryDirectory() as root:
            e_root = Path(root) / "e_platform"
            cst_root = Path(root) / "cst"
            results_root = Path(root) / "results"
            (e_root / "simulator_skills" / "cst_local").mkdir(parents=True)
            (e_root / "simulator_skills" / "cst_local" / "adapter.py").write_text("# fixture", encoding="utf-8")
            (e_root / "simulator_skills" / "cst_local" / "official.py").write_text("# fixture", encoding="utf-8")
            (cst_root / "AMD64" / "python").mkdir(parents=True)
            (cst_root / "AMD64" / "python" / "python.exe").write_text("", encoding="utf-8")
            (cst_root / "AMD64" / "python_cst_libraries").mkdir(parents=True)
            cst_control = Path(root) / "cst_control.py"
            cst_control.write_text("# fixture", encoding="utf-8")
            settings = Settings(workspace_root=root, logs_root=root, e_platform_root=str(e_root), e_results_root=str(results_root))
            scheduler = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )
            scheduler.cst_real = CstRealRunAdapter(e_platform_root=e_root, e_results_root=results_root, cst_root=cst_root)
            scheduler.cst_real.CST_CONTROL_SCRIPT = cst_control

            with patch.object(scheduler.langgraph_runtime.graph, "invoke", wraps=scheduler.langgraph_runtime.graph.invoke) as invoke:
                state = scheduler.create_real_cst_single_run_task({"user_input": "patch antenna S11", "project_path": r"D:\path\model.cst"})

            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(state["state"], "waiting_approval")
            self.assertEqual(state["task_metadata"]["current_stage"], "step_001_preflight")
            self.assertFalse(state["task_metadata"].get("reports"))
            self.assertEqual(state["nodes"]["step_001_preflight.preflight_review_agent"]["status"], "block")
            self.assertEqual(state["blockers"][-1]["type"], "repair_waiting_user")
            preflight = state["task_metadata"]["preflight"]
            self.assertFalse(preflight["success"])
            self.assertTrue(any(item["type"] == "placeholder_path" for item in preflight["blocker_details"]))
            self.assertTrue(any(item["name"] == "parameters" and item["status"] == "fail" for item in preflight["checks"]))
            review = state["task_metadata"]["reviews"][0]
            self.assertTrue(any(item["type"] == "missing_real_input" for item in review["structured_blockers"]))

            bad_extension = scheduler.create_real_cst_single_run_task(
                {"user_input": "patch antenna S11", "project_path": r"D:\real\model.txt", "parameters": {"w": 10}}
            )
            bad_preflight = bad_extension["task_metadata"]["preflight"]
            self.assertTrue(any(item["type"] == "invalid_path_extension" for item in bad_preflight["blocker_details"]))
            self.assertFalse(bad_extension["task_metadata"].get("cst_executed", False))


    def test_dynamic_plan_review_block_prevents_step_execution(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = Scheduler(
                Settings(workspace_root=root, logs_root=root, llm_enabled=False),
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )
            scheduler.llm.generate_json = lambda **_kwargs: {"steps": [{"step_goal": "invalid", "action": "unknown"}]}

            state = scheduler.create_task_from_request("general planning request")

            self.assertEqual(state["state"], "failed")
            self.assertIn("exhaust_failure", state["task_metadata"]["failure_reason"])
            failures = scheduler.failure_store.list(task_id=state["task_id"])
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["status"], "exhausted")
            self.assertEqual(failures[0]["repair_rounds"], 3)
            self.assertFalse(any("step_001" in node_id for node_id in state["nodes"]))

    def test_llm_planner_failure_has_no_rule_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = Scheduler(
                Settings(workspace_root=root, logs_root=root, llm_enabled=False),
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )

            def fail_plan(**_kwargs):
                raise RuntimeError("fixture_llm_unavailable")

            scheduler.llm.generate_json = fail_plan
            state = scheduler.create_task_from_request("general planning request")

            self.assertEqual(state["state"], "failed")
            self.assertEqual(state["task_metadata"]["dynamic_plan"]["source"], "external_llm_failed")
            self.assertIn("fixture_llm_unavailable", state["task_metadata"]["dynamic_plan"]["generation_error"])
            self.assertFalse(any(node_id.startswith("step_") for node_id in state["nodes"]))

    def test_scheduler_rejects_real_candidate_without_approval_step(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = Scheduler(
                Settings(workspace_root=root, logs_root=root),
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )
            candidate = fake_llm_plan({"mode": "real", "user_goal": "patch antenna S11"})
            candidate["steps"] = [step for step in candidate["steps"] if step["action"] != "approval"]
            plan = scheduler._normalize_dynamic_plan_candidate(
                candidate,
                task_id="task-real",
                mode="real",
                plan_version=1,
                user_input="patch antenna S11",
            )

            review = scheduler._validate_dynamic_plan(plan)

            self.assertEqual(review["decision"], "block")
            self.assertIn("real_plan_requires_exactly_one:approval", [item["reason"] for item in review["blocking_findings"]])

    def test_recover_interrupted_real_task_resets_run_guard(self):
        with tempfile.TemporaryDirectory() as root:
            blackboard = Blackboard(root)
            record = blackboard.create_task("real task", task_metadata={"mode": "real"})
            approval_id = f"real_cst:{record.task_id}"
            blackboard.update(
                record.task_id,
                state="running",
                approvals={approval_id: {"status": "running", "run_count": 1, "attempt_count": 1}},
            )
            scheduler = Scheduler(
                Settings(workspace_root=root, logs_root=root),
                blackboard,
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )

            recovered = scheduler.recover_tasks()

            self.assertEqual(recovered[0]["state"], "waiting_approval")
            self.assertEqual(recovered[0]["approvals"][approval_id]["run_count"], 0)
            self.assertEqual(recovered[0]["task_metadata"]["cst_status"], "interrupted_waiting_reapproval")

    def test_cst_project_creation_subprocess_has_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = CstRealRunAdapter(e_platform_root=root, e_results_root=root, cst_root=root)
            stdout_path = Path(root) / "stdout.log"
            stderr_path = Path(root) / "stderr.log"
            timeout = __import__("subprocess").TimeoutExpired(["cst"], 3)
            with patch("adapters.cst_real_run_adapter.subprocess.run", side_effect=timeout):
                with self.assertRaises(TimeoutError):
                    adapter._run_subprocess(["cst"], stdout_path, stderr_path, timeout_seconds=3)
            self.assertTrue(stdout_path.exists())
            self.assertTrue(stderr_path.exists())

    def test_paperwise_vector_metadata_is_bound_to_each_chunk(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = PaperWiseAdapter(root)
            conn = sqlite3.connect(":memory:")
            conn.execute(
                "CREATE TABLE embedding_metadata "
                "(id INTEGER, key TEXT, string_value TEXT, int_value INTEGER, float_value REAL, bool_value INTEGER)"
            )
            conn.execute(
                "CREATE TABLE embeddings "
                "(id INTEGER, segment_id TEXT, embedding_id TEXT, seq_id BLOB, created_at TEXT)"
            )
            conn.executemany(
                "INSERT INTO embedding_metadata(id, key, string_value) VALUES (?, ?, ?)",
                [
                    (1, "arxiv_id", "paper-a"),
                    (1, "title", "Paper A"),
                    (2, "arxiv_id", "paper-b"),
                    (2, "title", "Paper B"),
                ],
            )
            conn.executemany(
                "INSERT INTO embeddings(id, segment_id, embedding_id, seq_id, created_at) VALUES (?, '', ?, X'00', '')",
                [(1, "paper-a::report_0"), (2, "paper-b::report_0")],
            )

            first = adapter._vector_metadata_for_embedding(conn, 1)
            second = adapter._vector_metadata_for_embedding(conn, 2)

            self.assertEqual(first["arxiv_id"], "paper-a")
            self.assertEqual(first["title"], "Paper A")
            self.assertEqual(second["arxiv_id"], "paper-b")
            self.assertEqual(second["title"], "Paper B")

    def test_dynamic_handlers_execute_baseline_and_reflection_steps(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = Scheduler(
                Settings(workspace_root=root, logs_root=root, llm_enabled=False),
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )

            baseline = scheduler.create_task_from_request("GWO and PSO baseline ablation patch antenna S11")
            arbw = scheduler.create_task_from_request("ARBW only three samples, can it support the antenna conclusion")
            evidence = scheduler.create_task_from_request("Does claim_assessment have paper evidence for patch antenna S11")

            self.assertEqual(baseline["state"], "completed")
            self.assertEqual(arbw["state"], "completed")
            self.assertEqual(evidence["state"], "completed")
            self.assertIn("baseline_ablation", [step.get("action") for step in baseline["task_metadata"]["dynamic_plan"]["steps"]])
            self.assertIn("antenna_result_to_claim", [step.get("action") for step in arbw["task_metadata"]["dynamic_plan"]["steps"]])
            self.assertIn("evidence_check", [step.get("action") for step in evidence["task_metadata"]["dynamic_plan"]["steps"]])


    def test_scheduler_has_one_physical_execution_chain(self):
        source = inspect.getsource(Scheduler)
        self.assertEqual(source.count("self.cst_real.run_single("), 1)
        self.assertNotIn("_finish_v2_failure", source)
        self.assertNotIn("_run_v2_agent", source)
        self.assertNotIn("_run_packet_stage", source)
        self.assertNotIn("PacketStore", source)
        self.assertNotIn("DynamicPlanBuilder", source)
        self.assertFalse((Path(__file__).resolve().parents[1] / "agent_runtime" / "dynamic_plan.py").exists())
        self.assertFalse(hasattr(Scheduler, "_execute_dynamic_plan_steps"))
        self.assertFalse(hasattr(Scheduler, "_fail_dynamic_plan_review"))
        self.assertTrue((Path(__file__).resolve().parents[1] / "agent_runtime" / "langgraph_runtime.py").exists())
        self.assertIn("langgraph_runtime.start", inspect.getsource(Scheduler._create_dynamic_task))
        self.assertIn("langgraph_runtime.start", inspect.getsource(Scheduler.create_real_cst_single_run_task))
        self.assertIn("langgraph_runtime.resume", inspect.getsource(Scheduler.approve_real_cst_task))
        self.assertIn("langgraph_runtime.rerun_paperwise", inspect.getsource(Scheduler.rerun_paperwise_evidence_review))
        self.assertNotIn("_finalize_v2_failure", source)

    def test_langgraph_runtime_uses_existing_persistence_without_changing_task_contract(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = Scheduler(
                Settings(workspace_root=root, logs_root=root, llm_enabled=False),
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
            )

            state = scheduler.create_task_from_request("general status summary")

            self.assertEqual(state["state"], "completed")
            self.assertEqual(state["task_metadata"]["orchestration_runtime"], "langgraph")
            thread = state["task_metadata"]["langgraph_thread"]
            self.assertIn(state["task_id"], thread["thread_id"])
            self.assertFalse(thread["paused"])
            self.assertEqual(thread["checkpoint_backend"], "langgraph_in_memory_non_durable")
            self.assertFalse(thread["restart_resume_supported"])
            checkpoint = scheduler.langgraph_runtime.graph.get_state(scheduler.langgraph_runtime._main_config(state["task_id"]))
            self.assertEqual(checkpoint.values["task_id"], state["task_id"])
            self.assertTrue((Path(root) / "tasks" / state["task_id"] / "blackboard.json").is_file())


if __name__ == "__main__":
    unittest.main()
