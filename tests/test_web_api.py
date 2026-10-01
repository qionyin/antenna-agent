import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from unittest.mock import patch

import web.api as api
from agent_runtime.audit import AuditLog
from agent_runtime.approval import ApprovalManager
from agent_runtime.blackboard import Blackboard
from agent_runtime.capability_registry import CapabilityRegistry
from agent_runtime.config import Settings
from agent_runtime.memory import MemoryManager
from agent_runtime.llm import LLMClient
from agent_runtime.runtime_llm_config import runtime_llm_config
from agent_runtime.scheduler import Scheduler
from agent_runtime.streaming import StreamPublisher
from adapters.cst_real_run_adapter import CstRealRunAdapter
from tests.plan_fixtures import fake_llm_plan
from tests.test_parent_subgraphs_and_repair import modeling_request


class WebApiTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(
            LLMClient,
            "generate_json",
            autospec=True,
            side_effect=lambda _client, **kwargs: fake_llm_plan(kwargs["payload"]),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _with_isolated_api_state(self, root: str):
        """切换 Web API 到隔离测试状态。"""
        original = {
            "settings": api.settings,
            "blackboard": api.blackboard,
            "audit": api.audit,
            "stream": api.stream,
            "approval_manager": api.approval_manager,
            "scheduler": api.scheduler,
            "runtime_llm_config": {
                "enabled": runtime_llm_config.enabled,
                "base_url": runtime_llm_config.base_url,
                "api_key": runtime_llm_config.api_key,
                "model_name": runtime_llm_config.model_name,
                "embedding_model_name": runtime_llm_config.embedding_model_name,
                "source": runtime_llm_config.source,
            },
        }
        api.settings = Settings(
            workspace_root=root,
            logs_root=root,
            paperwise_root=str(Path(root) / "paperwise"),
            learning_evolution_enabled=True,
        )
        api.blackboard = Blackboard(root)
        api.audit = AuditLog(root)
        api.stream = StreamPublisher()
        api.approval_manager = ApprovalManager()
        api.scheduler = Scheduler(
            api.settings,
            api.blackboard,
            MemoryManager(),
            CapabilityRegistry(),
            api.audit,
            api.stream,
        )
        runtime_llm_config.enabled = False
        runtime_llm_config.base_url = ""
        runtime_llm_config.api_key = ""
        runtime_llm_config.model_name = ""
        runtime_llm_config.embedding_model_name = "text-embedding-v3"
        runtime_llm_config.source = "test_isolated"
        return original

    def _restore_api_state(self, original: dict):
        """恢复 Web API 的原始模块状态。"""
        for name, value in original.items():
            if name == "runtime_llm_config":
                runtime_llm_config.enabled = value["enabled"]
                runtime_llm_config.base_url = value["base_url"]
                runtime_llm_config.api_key = value["api_key"]
                runtime_llm_config.model_name = value["model_name"]
                runtime_llm_config.embedding_model_name = value["embedding_model_name"]
                runtime_llm_config.source = value["source"]
                continue
            setattr(api, name, value)

    def test_api_modeling_request_runs_parent_graph_to_real_cst_model_spec(self):
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                response = client.post(
                    "/tasks",
                    json={
                        "user_input": "复现微带贴片天线并生成可验证 CST 建模规格",
                        "modeling_request": modeling_request(Path(root)),
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                state = response.json()
                self.assertEqual(state["state"], "completed")
                result = state["task_metadata"]["modeling_preparation_result"]
                self.assertTrue(result["success"])
                self.assertFalse(result["cst_executed"])
                self.assertTrue(Path(result["artifacts"]["cst_model_spec"]).is_file())
                self.assertEqual(
                    state["task_metadata"]["langgraph_thread"]["thread_id"],
                    f"{state['task_id']}:main",
                )
            finally:
                self._restore_api_state(original)

    def test_health_and_capabilities(self):
        """验证该运行场景的预期行为。"""
        client = TestClient(api.app)
        self.assertEqual(client.get("/health").status_code, 200)
        capabilities = client.get("/capabilities").json()["capabilities"]
        self.assertIn("skill_packet_protocol", capabilities)

    def test_v23_learning_source_compile_and_innovation_gate_api(self):
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                created = client.post(
                    "/learning/sources",
                    json={
                        "source_id": "paper-api-1",
                        "content": "At 3.5 GHz, GWO optimizes slot length and improves U-slot patch antenna bandwidth in simulation.",
                        "metadata": {
                            "source_type": "paper",
                            "source_ref": "paperwise/paper-api-1/report.md",
                            "locator": "page 4",
                            "evidence_type": "paper_fact",
                        },
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                compiled = client.post("/learning/sources/paper-api-1/compile")
                self.assertEqual(compiled.status_code, 200, compiled.text)
                body = compiled.json()
                self.assertTrue(body["evidence_units"])
                self.assertTrue(body["knowledge_candidates"])
                knowledge = client.get("/learning/knowledge", params={"status": "candidate"}).json()
                self.assertTrue(knowledge)

                innovation = client.post(
                    "/learning/innovations/evaluate",
                    json={
                        "baseline_refs": ["paper-api-1"],
                        "known_gap": "robustness not evaluated",
                        "proposed_delta": "add robustness objective",
                        "mechanism_hypothesis": "reduce input impedance sensitivity",
                        "modelable_parameters": ["feed_offset"],
                        "metrics": ["S11"],
                        "baselines": ["single objective"],
                        "ablations": ["remove robustness objective"],
                        "falsification_condition": "no improvement",
                        "evidence_refs": [body["evidence_units"][0]["evidence_id"]],
                    },
                )
                self.assertEqual(innovation.status_code, 200, innovation.text)
                self.assertEqual(innovation.json()["status"], "experiment_ready")
                self.assertFalse(innovation.json()["novelty_claim_allowed"])
            finally:
                self._restore_api_state(original)

    def test_domain_knowledge_switch_returns_403_and_keeps_experience_routes(self):
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            api.scheduler.learning.domain_knowledge_enabled = False
            try:
                client = TestClient(api.app)
                requests = [
                    ("post", "/learning/sources", {}),
                    ("post", "/learning/paperwise/ingest", {}),
                    ("post", "/learning/sources/source-1/compile", {}),
                    ("get", "/learning/knowledge", None),
                    ("post", "/learning/knowledge/k1/review", {}),
                    ("post", "/learning/knowledge/k1/promote", {}),
                    ("post", "/learning/clusters", {}),
                    ("post", "/learning/innovations/evaluate", {}),
                    ("post", "/learning/wiki/rebuild", {}),
                    ("get", "/learning/wiki", None),
                    ("get", "/learning/wiki/page-1", None),
                    ("get", "/learning/graph", None),
                ]
                for method, path, payload in requests:
                    response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
                    self.assertEqual(response.status_code, 403, (method, path, response.text))
                    self.assertEqual(response.json(), {"error": "domain_knowledge_disabled"})
                self.assertEqual(client.get("/learning/experiences").status_code, 200)
                snapshot = client.get("/learning").json()
                self.assertEqual(snapshot["domain_knowledge"]["total"], 0)
            finally:
                self._restore_api_state(original)

    def test_learning_evolution_extraction_api_persists_proposals(self):
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                response = client.post(
                    "/learning/evolution/evaluate-extraction",
                    json={
                        "cases": [{
                            "case_id": "api-extraction-gap",
                            "text": "A patch antenna operates at 3.5 GHz.",
                            "expected_entities": ["microstrip_patch_antenna"],
                            "expected_relations": [["microstrip_patch_antenna", "affects", "bandwidth"]],
                        }]
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertTrue(result["proposal_ids"])
                proposals = client.get("/learning/evolution/proposals").json()
                self.assertEqual({item["proposal_id"] for item in proposals}, set(result["proposal_ids"]))
            finally:
                self._restore_api_state(original)

    def test_learning_evolution_run_api_applies_and_verifies_safe_alias(self):
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                response = client.post(
                    "/learning/evolution/run-extraction",
                    json={
                        "cases": [{
                            "case_id": "api-safe-alias",
                            "text": "A U shaped slot patch uses GWO to improve bandwidth.",
                            "expected_entities": ["u_slot_patch_antenna", "gwo", "bandwidth"],
                            "expected_relations": [
                                ["u_slot_patch_antenna", "uses_algorithm", "gwo"],
                                ["gwo", "increases", "bandwidth"],
                            ],
                            "suggested_aliases": {"u_slot_patch_antenna": ["u shaped slot patch"]},
                        }],
                        "verification_cases": [{
                            "case_id": "api-safe-alias-holdout",
                            "text": "The U shaped slot patch adopts GWO and improves bandwidth.",
                            "expected_entities": ["u_slot_patch_antenna", "gwo", "bandwidth"],
                            "expected_relations": [
                                ["u_slot_patch_antenna", "uses_algorithm", "gwo"],
                                ["gwo", "increases", "bandwidth"],
                            ],
                        }],
                        "auto_apply_low_risk": True,
                        "use_external_llm": False,
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(result["status"], "verified_kept")
                self.assertTrue(result["mutation_applied"])
                self.assertGreater(result["after"]["summary"]["f1"], result["baseline"]["summary"]["f1"])
            finally:
                self._restore_api_state(original)

    def test_v23_paperwise_report_ingest_api_preserves_source_and_compiles(self):
        with tempfile.TemporaryDirectory() as root:
            paperwise_root = Path(root) / "paperwise"
            report_dir = paperwise_root / "outputs" / "paper-001"
            report_dir.mkdir(parents=True)
            report_path = report_dir / "report.md"
            source_text = "# U-slot patch antenna\nAt 3.5 GHz, GWO improves impedance bandwidth by changing slot length."
            report_path.write_text(source_text, encoding="utf-8")
            original = self._with_isolated_api_state(root)
            api.settings.paperwise_root = str(paperwise_root)
            api.scheduler.paperwise = api.scheduler.paperwise.__class__(paperwise_root)
            try:
                client = TestClient(api.app)
                response = client.post(
                    "/learning/paperwise/ingest",
                    json={"report_path": str(report_path), "source_id": "paperwise-api-source", "compile": True},
                )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["source"]["status"], "extracted")
                self.assertEqual(body["source"]["content"], source_text)
                self.assertTrue(body["compiled"]["evidence_units"])
                self.assertTrue(body["compiled"]["knowledge_candidates"])
            finally:
                self._restore_api_state(original)

    def test_v23_graph_api_reads_existing_paperwise_graph_without_redis_projection(self):
        with tempfile.TemporaryDirectory() as root:
            paperwise_root = Path(root) / "paperwise"
            graph_dir = paperwise_root / "outputs" / "graph"
            graph_dir.mkdir(parents=True)
            graph_path = graph_dir / "graph.json"
            graph_path.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"id": "p1", "name": "Paper One", "type": "Paper"},
                            {"id": "a1", "name": "GWO", "type": "Algorithm"},
                        ],
                        "edges": [
                            {"source": "p1", "relation": "uses_algorithm", "target": "a1", "evidence_text": "Paper One uses GWO"}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original = self._with_isolated_api_state(root)
            api.settings.paperwise_root = str(paperwise_root)
            api.scheduler.paperwise = api.scheduler.paperwise.__class__(paperwise_root)
            try:
                client = TestClient(api.app)
                response = client.get("/learning/graph")
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["path"], str(graph_path))
                self.assertTrue(body["read_only"])
                self.assertEqual(api.scheduler.memory.store, None)
                rebuilt = client.post("/learning/wiki/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                self.assertEqual(rebuilt.json()["knowledge_graph"], "not_created; use existing PaperWise or antenna research graph")
            finally:
                self._restore_api_state(original)

    def test_runtime_llm_settings_do_not_echo_api_key(self):
        client = TestClient(api.app)
        old_config = {
            "enabled": runtime_llm_config.enabled,
            "base_url": runtime_llm_config.base_url,
            "api_key": runtime_llm_config.api_key,
            "model_name": runtime_llm_config.model_name,
            "embedding_model_name": runtime_llm_config.embedding_model_name,
            "source": runtime_llm_config.source,
        }
        runtime_llm_config.enabled = False
        runtime_llm_config.base_url = ""
        runtime_llm_config.api_key = ""
        runtime_llm_config.model_name = ""
        runtime_llm_config.embedding_model_name = "text-embedding-v3"
        missing_key = client.post(
            "/settings/llm-runtime",
            json={"enabled": True, "base_url": "https://example.test/v1", "model_name": "test-model", "api_key": ""},
        )
        try:
            self.assertEqual(missing_key.status_code, 400)

            saved = client.post(
                "/settings/llm-runtime",
                json={
                    "enabled": True,
                    "base_url": "https://example.test/v1",
                    "model_name": "test-model",
                    "embedding_model_name": "text-embedding-test",
                    "api_key": "secret-key",
                },
            )
            self.assertEqual(saved.status_code, 200)
            body = saved.json()
            self.assertTrue(body["enabled"])
            self.assertTrue(body["api_key_configured"])
            self.assertEqual(body["model_name"], "test-model")
            self.assertEqual(body["embedding_model_name"], "text-embedding-test")
            self.assertNotIn("secret-key", str(body))

            public = client.get("/settings/llm-runtime").json()
            self.assertTrue(public["api_key_configured"])
            self.assertEqual(public["embedding_model_name"], "text-embedding-test")
            self.assertNotIn("secret-key", str(public))
        finally:
            runtime_llm_config.enabled = old_config["enabled"]
            runtime_llm_config.base_url = old_config["base_url"]
            runtime_llm_config.api_key = old_config["api_key"]
            runtime_llm_config.model_name = old_config["model_name"]
            runtime_llm_config.embedding_model_name = old_config["embedding_model_name"]
            runtime_llm_config.source = old_config["source"]

    def test_paperwise_rejects_arbitrary_path(self):
        """验证该运行场景的预期行为。"""
        client = TestClient(api.app)
        response = client.get("/paperwise/report", params={"path": r"C:\Windows\win.ini"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error"], "path_not_allowed")

    def test_scheduler_blocks_without_paperwise_report(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            old_runtime_llm_enabled = runtime_llm_config.enabled
            runtime_llm_config.enabled = False
            settings = Settings(workspace_root=root, logs_root=root, paperwise_root=str(Path(root) / "missing"), llm_enabled=False)
            registry = CapabilityRegistry()
            scheduler = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                registry,
                AuditLog(root),
                StreamPublisher(),
            )
            try:
                result = scheduler.create_paper_plan_task("paper plan")
            finally:
                runtime_llm_config.enabled = old_runtime_llm_enabled
            self.assertEqual(result["state"], "failed")
            self.assertNotIn("packets", result)

    def test_approval_api_happy_path(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                record = api.blackboard.create_task("approval test")

                created = client.post(
                    f"/tasks/{record.task_id}/approvals",
                    json={"reason": "human gate"},
                )
                self.assertEqual(created.status_code, 200)
                approval = created.json()
                self.assertEqual(approval["task_id"], record.task_id)
                self.assertEqual(approval["status"], "waiting_approval")

                listed = client.get("/approvals").json()
                self.assertEqual(len(listed), 1)
                decided = client.post(f"/approvals/{approval['approval_id']}/approved")
                self.assertEqual(decided.status_code, 200)
                self.assertEqual(decided.json()["status"], "approved")
            finally:
                self._restore_api_state(original)

    def test_approvals_include_blackboard_requests(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                record = api.blackboard.create_task("external approval test")
                approval_id = "external_llm:test"
                record.approvals[approval_id] = {
                    "approval_id": approval_id,
                    "task_id": record.task_id,
                    "status": "waiting_approval",
                    "reason": "External LLM data egress requires approval",
                }
                api.blackboard.update(record.task_id, state="waiting_approval", approvals=record.approvals)

                listed = client.get("/approvals").json()
                self.assertIn(approval_id, {item["approval_id"] for item in listed})
                decided = client.post(f"/approvals/{approval_id}/rejected")
                self.assertEqual(decided.status_code, 200)
                self.assertEqual(decided.json()["status"], "rejected")
            finally:
                self._restore_api_state(original)

    def test_api_does_not_trust_external_llm_approved_payload(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                api.settings.llm_enabled = True
                api.scheduler.settings.llm_enabled = True
                api.scheduler.llm.enabled = True
                client = TestClient(api.app)

                response = client.post(
                    "/tasks/paper-plan",
                    json={
                        "user_input": "patch antenna GWO S11",
                        "external_llm_approved": True,
                    },
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["state"], "waiting_approval")
                self.assertNotIn("packets", body)
            finally:
                self._restore_api_state(original)

    def test_external_llm_approval_resumes_same_task(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                paperwise = Path(root) / "paperwise" / "outputs"
                paperwise.mkdir(parents=True)
                report = paperwise / "paper.md"
                report.write_text("# Patch Antenna\n\nS11 bandwidth gain", encoding="utf-8")
                api.settings.llm_enabled = True
                api.scheduler.settings.llm_enabled = True
                api.scheduler.llm.enabled = True
                client = TestClient(api.app)

                created = client.post(
                    "/tasks/paper-plan",
                    json={"user_input": "patch antenna GWO S11", "paper_report_path": str(report)},
                ).json()
                thread_before = created["task_metadata"]["langgraph_thread"]
                self.assertTrue(thread_before["paused"])
                self.assertEqual(thread_before["checkpoint_backend"], "langgraph_in_memory_non_durable")
                self.assertFalse(thread_before["restart_resume_supported"])
                approval_id = f"external_llm:{created['task_id']}"
                resumed = client.post(f"/approvals/{approval_id}/approved")

                self.assertEqual(resumed.status_code, 200)
                body = resumed.json()
                self.assertEqual(body["task_id"], created["task_id"])
                self.assertEqual(body["state"], "completed")
                self.assertNotIn("packets", body)
                self.assertEqual(body["task_metadata"]["dynamic_plan"]["plan_type"], "dynamic_plan")
                self.assertFalse(body["task_metadata"]["langgraph_thread"]["paused"])
                self.assertEqual(body["task_metadata"]["langgraph_thread"]["thread_id"], thread_before["thread_id"])
            finally:
                self._restore_api_state(original)

    def test_general_task_endpoint_does_not_require_paperwise(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                response = client.post("/tasks", json={"user_input": "patch antenna GWO optimize S11"})

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["state"], "completed")
                self.assertNotIn("packets", body)
                self.assertFalse(body["task_metadata"]["require_paperwise"])
                self.assertEqual(body["task_metadata"]["dynamic_plan"]["plan_type"], "dynamic_plan")
            finally:
                self._restore_api_state(original)

    def test_approval_card_contains_gate_fields(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                record = api.blackboard.create_task(
                    "patch antenna GWO optimize S11",
                    task_metadata={
                        "current_stage": "step_003_cst_run",
                        "artifacts": [
                            {
                                "artifact_type": "cst_run_manifest",
                                "path": "run.json",
                                "metadata": {"real_cst_execution": True},
                            }
                        ],
                        "module_review_records": [
                            {"output": {"blocking_findings": ["solver output missing"]}}
                        ],
                    },
                )
                card = client.get(f"/tasks/{record.task_id}/approval-card")

                self.assertEqual(card.status_code, 200)
                data = card.json()
                self.assertNotIn("packet", data)
                self.assertEqual(data["current_phase"], "step_003_cst_run")
                self.assertIn("artifacts", data)
                self.assertEqual(data["reviewer_findings"], ["solver output missing"])
                self.assertIn("data_egress_flags", data)
                self.assertEqual(len(data["live_cst_flags"]), 1)
                self.assertIn("terminate", data["allowed_actions"])
            finally:
                self._restore_api_state(original)

    def test_events_endpoint_available(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                record = api.blackboard.create_task("events test")
                api.stream.publish(record.task_id, "node", "status", "pending", "node running")

                response = client.get(f"/tasks/{record.task_id}/events")
                self.assertEqual(response.status_code, 200)
                events = response.json()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["task_id"], record.task_id)
            finally:
                self._restore_api_state(original)

    def test_placeholder_dead_letter_api_is_not_exposed(self):
        paths = api.app.openapi()["paths"]

        self.assertFalse(any("dead-letter" in path for path in paths))

    def test_v2_real_cst_api_create_approve_and_report(self):
        """Verify V2.0 real CST API endpoints."""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
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
                api.settings.e_platform_root = str(e_root)
                api.settings.e_results_root = str(results_root)
                api.scheduler.cst_real = CstRealRunAdapter(e_platform_root=e_root, e_results_root=results_root, cst_root=cst_root)
                api.scheduler.cst_real.CST_CONTROL_SCRIPT = cst_control
                report_dir = Path(root) / "paperwise" / "outputs" / "paper_a"
                report_dir.mkdir(parents=True)
                (report_dir / "report.md").write_text("# Patch Antenna S11\n\npatch antenna S11 return loss", encoding="utf-8")
                client = TestClient(api.app)

                created = client.post(
                    "/tasks/real-cst-single-run",
                    json={"user_input": "patch antenna S11", "simulate_cst": True, "parameters": {"w": 10}},
                )
                self.assertEqual(created.status_code, 200)
                body = created.json()
                self.assertEqual(body["state"], "waiting_approval")
                actions = client.get(f"/tasks/{body['task_id']}/available-actions").json()
                self.assertIn("approve_real_cst", actions["actions"])
                self.assertIn("reject", actions["actions"])

                approved = client.post(f"/tasks/{body['task_id']}/approve")
                self.assertEqual(approved.status_code, 200)
                done = approved.json()
                self.assertEqual(done["state"], "completed")
                self.assertEqual(done["task_metadata"]["mode"], "real")
                self.assertGreater(done["task_metadata"]["results"]["metrics"]["bandwidth_10db"], 0)
                self.assertTrue(client.get(f"/tasks/{body['task_id']}/artifacts").json())
                self.assertTrue(client.get(f"/tasks/{body['task_id']}/reports").json())
                self.assertTrue(client.get(f"/tasks/{body['task_id']}/logs").json())
                rejected_after_done = client.post(f"/tasks/{body['task_id']}/reject")
                self.assertEqual(rejected_after_done.status_code, 409)
                still_done = client.get(f"/tasks/{body['task_id']}/state").json()
                self.assertEqual(still_done["state"], "completed")
            finally:
                self._restore_api_state(original)

    def test_v2_paperwise_review_can_be_rerun_and_surfaces_llm_error(self):
        """Verify existing tasks can re-run PaperWise review after runtime LLM settings change."""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            old_config = {
                "enabled": runtime_llm_config.enabled,
                "base_url": runtime_llm_config.base_url,
                "api_key": runtime_llm_config.api_key,
                "model_name": runtime_llm_config.model_name,
            }
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
                api.settings.e_platform_root = str(e_root)
                api.settings.e_results_root = str(results_root)
                api.scheduler.cst_real = CstRealRunAdapter(e_platform_root=e_root, e_results_root=results_root, cst_root=cst_root)
                api.scheduler.cst_real.CST_CONTROL_SCRIPT = cst_control
                report_dir = Path(root) / "paperwise" / "outputs" / "paper_a"
                report_dir.mkdir(parents=True)
                (report_dir / "report.md").write_text("# Patch Antenna S11\n\npatch antenna S11 return loss", encoding="utf-8")
                client = TestClient(api.app)

                created = client.post(
                    "/tasks/real-cst-single-run",
                    json={"user_input": "patch antenna S11", "simulate_cst": True, "parameters": {"w": 10}},
                ).json()
                self.assertEqual(created["task_metadata"]["evidence_pool_summary"]["review_mode"], "local_react_fallback")
                original_plan_id = created["task_metadata"]["dynamic_plan"]["plan_id"]
                report_dir_b = Path(root) / "paperwise" / "outputs" / "paper_b"
                report_dir_b.mkdir(parents=True)
                (report_dir_b / "report.md").write_text("# U-Slot Patch Antenna S11\n\nmicrostrip patch antenna S11 bandwidth feed ground", encoding="utf-8")

                runtime_llm_config.enabled = True
                runtime_llm_config.base_url = "https://127.0.0.1:1/v1"
                runtime_llm_config.api_key = "secret-key"
                runtime_llm_config.model_name = "test-model"

                record = api.blackboard.get(created["task_id"])
                approvals = dict(record.approvals)
                approvals[f"external_llm:{created['task_id']}"] = {
                    "approval_id": f"external_llm:{created['task_id']}",
                    "task_id": created["task_id"],
                    "status": "approved",
                    "reason": "test approved runtime LLM rerun",
                }
                api.blackboard.update(created["task_id"], approvals=approvals)

                reviewed = client.post(f"/tasks/{created['task_id']}/paperwise/review")
                self.assertEqual(reviewed.status_code, 200)
                evidence = reviewed.json()["task_metadata"]["evidence_pool_summary"]
                rerun_metadata = reviewed.json()["task_metadata"]
                self.assertEqual(rerun_metadata["dynamic_plan"]["plan_id"], original_plan_id)
                self.assertEqual(
                    [step["action"] for step in rerun_metadata["paperwise_rerun_plan"]["steps"]],
                    ["evidence_retrieval"],
                )
                self.assertIn("paperwise-rerun-v", rerun_metadata["last_auxiliary_langgraph_thread"]["thread_id"])
                self.assertEqual(evidence["review_mode"], "local_react_fallback")
                self.assertTrue(evidence["llm_review"]["attempted"])
                self.assertFalse(evidence["llm_review"]["success"])
                self.assertTrue(evidence["llm_review"]["error"])
                titles = json.dumps(evidence["sources"].get("deep_read_papers", {}).get("items", []), ensure_ascii=False)
                self.assertIn("U-Slot Patch Antenna S11", titles)
            finally:
                runtime_llm_config.enabled = old_config["enabled"]
                runtime_llm_config.base_url = old_config["base_url"]
                runtime_llm_config.api_key = old_config["api_key"]
                runtime_llm_config.model_name = old_config["model_name"]
                self._restore_api_state(original)

    def test_v2_paperwise_create_does_not_call_runtime_llm_without_approval(self):
        """Verify task creation does not call runtime LLM before task-level approval."""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
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
                api.settings.e_platform_root = str(e_root)
                api.settings.e_results_root = str(results_root)
                api.scheduler.cst_real = CstRealRunAdapter(e_platform_root=e_root, e_results_root=results_root, cst_root=cst_root)
                api.scheduler.cst_real.CST_CONTROL_SCRIPT = cst_control
                report_dir = Path(root) / "paperwise" / "outputs" / "paper_a"
                report_dir.mkdir(parents=True)
                (report_dir / "report.md").write_text("# Patch Antenna S11\n\npatch antenna S11 return loss", encoding="utf-8")
                runtime_llm_config.enabled = True
                runtime_llm_config.base_url = "https://127.0.0.1:1/v1"
                runtime_llm_config.api_key = "secret-key"
                runtime_llm_config.model_name = "test-model"
                runtime_llm_config.source = "frontend_runtime"
                client = TestClient(api.app)

                class FakeOpenAI:
                    def __init__(self, **kwargs):
                        raise AssertionError("runtime LLM should not be called without approval")

                with patch("openai.OpenAI", FakeOpenAI):
                    created = client.post(
                        "/tasks/real-cst-single-run",
                        json={"user_input": "patch antenna S11", "simulate_cst": True, "parameters": {"w": 10}},
                    )

                self.assertEqual(created.status_code, 200)
                evidence = created.json()["task_metadata"]["evidence_pool_summary"]
                self.assertFalse(evidence["llm_review"]["attempted"])
                self.assertEqual(evidence["llm_review"]["error"], "external_llm_not_approved")
                self.assertEqual(evidence["review_mode"], "local_react_fallback")
            finally:
                self._restore_api_state(original)

    def test_v2_real_cst_api_rejects_solver_gate(self):
        """Verify V2.0 real CST rejection endpoint."""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
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
                api.settings.e_platform_root = str(e_root)
                api.settings.e_results_root = str(results_root)
                api.scheduler.cst_real = CstRealRunAdapter(
                    e_platform_root=e_root,
                    e_results_root=results_root,
                    cst_root=cst_root,
                )
                api.scheduler.cst_real.CST_CONTROL_SCRIPT = cst_control
                client = TestClient(api.app)

                created = client.post(
                    "/tasks/real-cst-single-run",
                    json={"user_input": "patch antenna S11", "simulate_cst": True, "parameters": {"w": 10}},
                )
                self.assertEqual(created.status_code, 200)
                body = created.json()
                rejected = client.post(f"/tasks/{body['task_id']}/reject")
                self.assertEqual(rejected.status_code, 200)
                failed = rejected.json()
                self.assertEqual(failed["state"], "failed")
                self.assertEqual(failed["approvals"][f"real_cst:{body['task_id']}"]["status"], "rejected")
                reports = client.get(f"/tasks/{body['task_id']}/reports").json()
                self.assertTrue(reports)
                self.assertEqual(reports[-1]["status"], "failed")
                self.assertEqual(api.scheduler.failure_store.list(task_id=body["task_id"]), [])
            finally:
                self._restore_api_state(original)

    def test_unknown_task_endpoints_return_404(self):
        """Unknown task IDs should not surface as 500 errors."""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                self.assertEqual(client.get("/tasks/not-a-task/state").status_code, 404)
                self.assertEqual(client.get("/tasks/not-a-task/reports").status_code, 404)
                self.assertEqual(
                    client.post(
                        "/tasks/not-a-task/central-message",
                        json={"intent": "ask_question", "message": "hello"},
                    ).status_code,
                    404,
                )
            finally:
                self._restore_api_state(original)

    def test_v224_task_state_exposes_callable_candidate_and_step_contexts(self):
        """Verify API state exposes V2.2.4 callable/candidate skill routing fields."""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)

                created = client.post("/tasks", json={"user_input": "patch antenna"})
                self.assertEqual(created.status_code, 200)
                body = created.json()
                task_id = body["task_id"]
                state = client.get(f"/tasks/{task_id}/state")

                self.assertEqual(state.status_code, 200)
                metadata = state.json()["task_metadata"]
                initial_route = metadata["initial_skill_route_plan"]
                self.assertIn("callable_skills", initial_route)
                self.assertIn("candidate_skills", initial_route)
                self.assertTrue(initial_route["candidate_skills"])
                self.assertTrue(all(route["route_class"] == "candidate" for route in initial_route["candidate_skills"]))
                self.assertTrue(all(not route["call_allowed"] for route in initial_route["candidate_skills"]))

                steps = metadata["dynamic_plan"]["steps"]
                self.assertTrue(steps)
                self.assertTrue(all("callable_skills" in step for step in steps))
                self.assertTrue(all("candidate_skills" in step for step in steps))
                self.assertTrue(all("step_skill_context" in step for step in steps))
                self.assertTrue(metadata["step_skill_contexts"])
                self.assertIn("active_skill_route_plan", metadata)
                self.assertIn("skill_route_plan_history", metadata)
            finally:
                self._restore_api_state(original)

    def test_central_agent_message_endpoint_records_user_feedback(self):
        """Verify frontend can send a user instruction back to the central agent."""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)

                created = client.post("/tasks", json={"user_input": "patch antenna S11"})
                self.assertEqual(created.status_code, 200)
                task_id = created.json()["task_id"]

                actions = client.get(f"/tasks/{task_id}/available-actions").json()["actions"]
                self.assertIn("send_central_message", actions)

                response = client.post(
                    f"/tasks/{task_id}/central-message",
                    json={
                        "intent": "request_reroute",
                        "target_step_id": "step_003_geometry",
                        "message": "证据不足，请重新规划几何证据步骤。",
                    },
                )

                self.assertEqual(response.status_code, 200)
                metadata = response.json()["task_metadata"]
                messages = metadata["central_agent_messages"]
                self.assertEqual(messages[-1]["intent"], "request_reroute")
                self.assertEqual(messages[-1]["target_step_id"], "step_003_geometry")
                self.assertIn("重新规划", messages[-1]["message"])
                self.assertEqual(metadata["last_user_central_message"]["message_id"], messages[-1]["message_id"])
                self.assertTrue(
                    any(item.get("decision") == "user_message_received" for item in metadata["central_decisions"])
                )
            finally:
                self._restore_api_state(original)

    def test_maintenance_endpoint_dry_run(self):
        """验证该运行场景的预期行为。"""
        with tempfile.TemporaryDirectory() as root:
            original = self._with_isolated_api_state(root)
            try:
                client = TestClient(api.app)
                response = client.post("/maintenance/audit", json={"dry_run": True})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["summary"]["dry_run"])
            finally:
                self._restore_api_state(original)


if __name__ == "__main__":
    unittest.main()
