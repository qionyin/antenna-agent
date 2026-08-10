import inspect
import errno
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.audit import AuditLog
from agent_runtime.blackboard import Blackboard
from agent_runtime.capability_registry import CapabilityRegistry
from agent_runtime.config import Settings
from agent_runtime.memory import MemoryManager
from agent_runtime.llm import LLMClient
from agent_runtime.langgraph_runtime import DynamicLangGraphRuntime, retry_transient_runtime_error
from agent_runtime.io_contracts import IOContractResolver
from agent_runtime.runtime_llm_config import runtime_llm_config
from agent_runtime.scheduler import Scheduler
from agent_runtime.streaming import StreamPublisher
from langgraph.checkpoint.sqlite import SqliteSaver
from tests.plan_fixtures import fake_llm_plan


class LangGraphRuntimeTests(unittest.TestCase):
    def setUp(self):
        previous_runtime_llm_enabled = runtime_llm_config.enabled
        runtime_llm_config.enabled = False
        self.addCleanup(setattr, runtime_llm_config, "enabled", previous_runtime_llm_enabled)
        patcher = patch.object(
            LLMClient,
            "generate_json",
            autospec=True,
            side_effect=lambda _client, **kwargs: fake_llm_plan(kwargs["payload"]),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _scheduler(self, root: str) -> Scheduler:
        return Scheduler(
            Settings(workspace_root=root, logs_root=root, llm_enabled=False),
            Blackboard(root),
            MemoryManager(),
            CapabilityRegistry(),
            AuditLog(root),
            StreamPublisher(),
        )

    def test_graph_contains_the_multi_agent_execution_nodes(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = self._scheduler(root)
            runtime = scheduler.langgraph_runtime
            parent_nodes = set(runtime.graph.get_graph().nodes)
            planning_nodes = set(runtime.planning_subgraph.get_graph().nodes)
            execution_nodes = set(runtime.execution_subgraph.get_graph().nodes)
            repair_nodes = set(runtime.repair_subgraph.get_graph().nodes)

        self.assertEqual(
            parent_nodes - {"__start__", "__end__"},
            {"planning_subgraph", "execution_subgraph", "repair_subgraph", "failure_report", "finalize"},
        )
        self.assertTrue(
            {
                "initial_skill_route",
                "skill_route_review",
                "persist_skill_route",
                "block_skill_route",
                "paperwise_gate",
                "external_llm_gate",
                "plan_generate",
                "plan_normalize",
                "plan_validate",
                "persist_plan",
                "block_plan",
                "prepare_execution",
            }.issubset(planning_nodes)
        )
        self.assertTrue(
            {
                "select_step",
                "route_skill",
                "task_agent",
                "module_review",
                "global_review",
                "central_decision",
                "commit_step",
            }.issubset(execution_nodes)
        )
        self.assertTrue(
            {
                "failure_triage",
                "repair_plan",
                "repair_task",
                "repair_review",
                "repair_global_review",
                "replay_failed_step",
                "repair_terminal",
            }.issubset(repair_nodes)
        )

    def test_parent_graph_uses_sliced_subgraph_inputs(self):
        source = inspect.getsource(DynamicLangGraphRuntime._build_parent_graph)
        self.assertIn("input_schema=PlanningGraphInput", source)
        self.assertIn("input_schema=ExecutionGraphInput", source)
        self.assertIn("input_schema=RepairGraphInput", source)

    def test_io_contract_builds_minimal_task_agent_payload(self):
        payload = IOContractResolver().build_input(
            "agent.task",
            {
                "task_id": "task-1",
                "current_step": {
                    "step_id": "step_001",
                    "step_goal": "parse S11",
                    "inputs": ["run_manifest"],
                    "outputs": ["s11"],
                    "required_skills": ["cst-result-parser"],
                },
                "step_skill_context": {
                    "callable_skills": [{"owner_skill": "cst-result-parser"}],
                    "candidate_skills": [{"owner_skill": "paperwise"}],
                },
                "executed_steps": [{"step_id": "step_000", "status": "passed"}],
                "current_failure": {"error": "should not leak"},
                "repair_plan": {"action": "should not leak"},
            },
        )
        self.assertEqual(payload["step_id"], "step_001")
        self.assertEqual(payload["goal"], "parse S11")
        self.assertEqual(payload["allowed_skills"], [{"owner_skill": "cst-result-parser"}])
        self.assertNotIn("current_failure", payload)
        self.assertNotIn("repair_plan", payload)

    def test_initial_skill_route_retries_inside_langgraph(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = self._scheduler(root)
            original = scheduler.skill_router.build_plan
            calls = 0
            calls_at_plan = 0

            def flaky_route(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise ConnectionError("temporary route failure")
                return original(*args, **kwargs)

            original_plan = scheduler._generate_dynamic_plan_candidate

            def capture_plan(planner_input):
                nonlocal calls_at_plan
                calls_at_plan = calls
                return original_plan(planner_input)

            with patch.object(scheduler.skill_router, "build_plan", side_effect=flaky_route), patch.object(
                scheduler, "_generate_dynamic_plan_candidate", side_effect=capture_plan
            ):
                state = scheduler.create_task_from_request("patch antenna S11")

            self.assertEqual(calls_at_plan, 3)
            self.assertEqual(state["state"], "completed")
            self.assertEqual(state["nodes"]["skill_router.task_agent"]["status"], "done")

    def test_plan_generation_retries_inside_langgraph(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = self._scheduler(root)
            calls = 0

            def flaky_plan(planner_input):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise ConnectionError("temporary planner failure")
                return fake_llm_plan(planner_input)

            with patch.object(scheduler, "_generate_dynamic_plan_candidate", side_effect=flaky_plan):
                state = scheduler.create_task_from_request("general status summary")

            self.assertEqual(calls, 3)
            self.assertEqual(state["state"], "completed")
            self.assertEqual(state["nodes"]["central_scheduler.llm_plan_generation"]["status"], "done")

    def test_new_task_uses_one_end_to_end_graph_invoke(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = self._scheduler(root)
            with patch.object(scheduler.langgraph_runtime.graph, "invoke", wraps=scheduler.langgraph_runtime.graph.invoke) as invoke:
                state = scheduler.create_task_from_request("general status summary")

            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(state["state"], "completed")
            self.assertIn("skill_router.task_agent", state["nodes"])
            self.assertIn("central_scheduler.plan_validation", state["nodes"])
            self.assertIn("step_001_general.general_task_agent", state["nodes"])

    def test_route_review_block_stops_plan_and_execution_in_same_graph(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = self._scheduler(root)
            blocked = {
                "status": "blocked",
                "decision": "block",
                "blocking_findings": [{"reason": "invalid route binding"}],
            }
            with patch.object(scheduler.skill_router, "review_plan", return_value=blocked), patch.object(
                scheduler, "_generate_dynamic_plan_candidate"
            ) as generate:
                state = scheduler.create_task_from_request("general status summary")

            self.assertEqual(state["state"], "failed")
            self.assertEqual(state["task_metadata"]["langgraph_final_decision"], "block_task")
            self.assertEqual(state["task_metadata"]["langgraph_final_stage"], "failure_report")
            self.assertTrue(any(item["type"] == "repair_blocked" for item in state["blockers"]))
            self.assertEqual(state["task_metadata"]["active_failure"]["category"], "route")
            self.assertEqual(generate.call_count, 0)
            self.assertNotIn("central_scheduler.llm_plan_generation", state["nodes"])
            self.assertFalse(any(node_id.endswith(".general_task_agent") for node_id in state["nodes"]))

    def test_non_transient_route_errors_do_not_retry(self):
        for error in (PermissionError("denied"), ValueError("invalid schema")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as root:
                scheduler = self._scheduler(root)
                calls = 0

                def fail_route(*_args, **_kwargs):
                    nonlocal calls
                    calls += 1
                    raise error

                with patch.object(scheduler.skill_router, "build_plan", side_effect=fail_route):
                    state = scheduler.create_task_from_request("general status summary")

                expected_calls = 1 if isinstance(error, PermissionError) else 4
                self.assertEqual(calls, expected_calls)
                expected = "waiting_approval" if isinstance(error, PermissionError) else "failed"
                self.assertEqual(state["state"], expected)

    def test_non_transient_plan_error_does_not_retry(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = self._scheduler(root)
            calls = 0

            def fail_plan(_planner_input):
                nonlocal calls
                calls += 1
                raise ValueError("candidate schema invalid")

            with patch.object(scheduler, "_generate_dynamic_plan_candidate", side_effect=fail_plan):
                state = scheduler.create_task_from_request("general status summary")

            self.assertEqual(calls, 4)
            self.assertEqual(state["state"], "failed")
            failures = scheduler.failure_store.list(task_id=state["task_id"])
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["category"], "plan")
            self.assertEqual(failures[0]["repair_rounds"], 3)
            self.assertTrue(state["task_metadata"].get("reports"))

    def test_invoke_exception_reenters_parent_repair_without_out_of_graph_failure_write(self):
        invoke_source = inspect.getsource(DynamicLangGraphRuntime._invoke_parent_state)
        failure_source = inspect.getsource(DynamicLangGraphRuntime._graph_failure_input)

        self.assertNotIn("failure_store", invoke_source)
        self.assertNotIn("failure_store", failure_source)
        self.assertIn('"operation": "failure"', failure_source)

    def test_retry_predicate_excludes_cst_timeout(self):
        self.assertTrue(retry_transient_runtime_error(TimeoutError("HTTP request timed out")))
        self.assertTrue(retry_transient_runtime_error(OSError(errno.EBUSY, "resource busy")))
        self.assertFalse(retry_transient_runtime_error(TimeoutError("CST command timed out after 10s")))
        self.assertFalse(retry_transient_runtime_error(PermissionError("denied")))
        self.assertFalse(retry_transient_runtime_error(ValueError("schema invalid")))

    def test_retry_policy_is_not_attached_to_task_or_cst_execution_nodes(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = self._scheduler(root).langgraph_runtime
            planning_nodes = runtime.planning_subgraph_builder.nodes
            execution_nodes = runtime.execution_subgraph_builder.nodes

        self.assertIsNotNone(planning_nodes["initial_skill_route"].retry_policy)
        self.assertIsNotNone(planning_nodes["plan_generate"].retry_policy)
        self.assertIsNotNone(execution_nodes["task_agent"].retry_policy)
        self.assertIsNone(execution_nodes["commit_step"].retry_policy)

    def test_plan_schema_review_repair_converges_after_three_rounds(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = self._scheduler(root)
            scheduler.llm.generate_json = lambda **_kwargs: {"steps": [{"step_goal": "invalid", "action": "unknown"}]}
            original = scheduler._validate_dynamic_plan
            calls = 0

            def validate_once(plan):
                nonlocal calls
                calls += 1
                return original(plan)

            with patch.object(scheduler, "_validate_dynamic_plan", side_effect=validate_once):
                state = scheduler.create_task_from_request("general planning request")

            self.assertEqual(calls, 4)
            self.assertEqual(state["state"], "failed")
            failures = scheduler.failure_store.list(task_id=state["task_id"])
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["repair_rounds"], 3)
            self.assertEqual(failures[0]["status"], "exhausted")
            self.assertFalse(any(node_id.endswith(".general_task_agent") for node_id in state["nodes"]))

    def test_artifact_projection_uses_metadata_and_reviews_without_legacy_packets(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = self._scheduler(root)
            record = scheduler.blackboard.create_task(
                "artifact projection",
                task_metadata={
                    "artifacts": [
                        {"id": "skill-packet", "path": "skill_packet.json", "artifact_type": "antenna_skill_packet"}
                    ],
                    "reviews": [
                        {
                            "artifacts": [
                                {"id": "skill-packet", "path": "skill_packet.json", "artifact_type": "antenna_skill_packet"},
                                {"id": "review-log", "path": "review.json", "artifact_type": "review"},
                            ]
                        }
                    ],
                },
            )

            artifacts = scheduler.task_artifacts(record.task_id)

            self.assertEqual([item["id"] for item in artifacts], ["skill-packet", "review-log"])
            self.assertEqual(artifacts[0]["artifact_type"], "antenna_skill_packet")

    def test_task_contract_records_langgraph_as_the_orchestrator(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._scheduler(root).create_task_from_request("summarize current task status")

        self.assertEqual(state["state"], "completed")
        self.assertEqual(state["task_metadata"]["orchestration_runtime"], "langgraph")
        self.assertEqual(
            state["task_metadata"]["langgraph_thread"]["checkpoint_backend"],
            "langgraph_in_memory_non_durable",
        )
        self.assertFalse(state["task_metadata"]["langgraph_thread"]["restart_resume_supported"])
        self.assertEqual(state["task_metadata"]["langgraph_thread"]["durable_mirror"], "blackboard_json_task_snapshot")

    def test_durable_checkpoint_resumes_approval_after_scheduler_restart(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = str(Path(root) / "langgraph-test-checkpoints.sqlite")
            settings = Settings(workspace_root=root, logs_root=root, llm_enabled=True)
            connection_one = sqlite3.connect(db_path, check_same_thread=False)
            scheduler_one = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
                langgraph_checkpointer=SqliteSaver(connection_one),
            )
            created = scheduler_one.create_task_from_request("summarize current task status")
            self.assertEqual(created["state"], "waiting_approval")
            thread_before = created["task_metadata"]["langgraph_thread"]["thread_id"]
            approval_id = f"external_llm:{created['task_id']}"
            approvals = dict(scheduler_one.blackboard.get(created["task_id"]).approvals)
            approvals[approval_id] = {**approvals[approval_id], "status": "approved"}
            scheduler_one.blackboard.update(created["task_id"], approvals=approvals)
            connection_one.close()

            connection_two = sqlite3.connect(db_path, check_same_thread=False)
            self.addCleanup(connection_two.close)
            scheduler_two = Scheduler(
                settings,
                Blackboard(root),
                MemoryManager(),
                CapabilityRegistry(),
                AuditLog(root),
                StreamPublisher(),
                langgraph_checkpointer=SqliteSaver(connection_two),
            )
            resumed = scheduler_two.resume_approved_task(created["task_id"])

            self.assertEqual(resumed["state"], "completed")
            thread_after = resumed["task_metadata"]["langgraph_thread"]
            self.assertEqual(thread_after["thread_id"], thread_before)
            self.assertEqual(thread_after["checkpoint_backend"], "langgraph_sqlite_test")
            self.assertTrue(thread_after["restart_resume_supported"])
            connection_two.close()


if __name__ == "__main__":
    unittest.main()
