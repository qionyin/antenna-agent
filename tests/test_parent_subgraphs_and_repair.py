from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.audit import AuditLog
from agent_runtime.blackboard import Blackboard
from agent_runtime.capability_registry import CapabilityRegistry
from agent_runtime.config import Settings
from agent_runtime.llm import LLMGenerationError
from agent_runtime.memory import MemoryManager
from agent_runtime.runtime_llm_config import runtime_llm_config
from agent_runtime.scheduler import Scheduler
from agent_runtime.streaming import StreamPublisher


def modeling_request(root: Path, *, include_port: bool = True) -> dict:
    source = root / "paper_extract.md"
    source.write_text(
        "\n".join(
            [
                "# Paper geometry extract",
                "## Page 4",
                "### Antenna Geometry",
                "The reported antenna is a microstrip patch.",
                "The layer stack uses a Rogers RT/duroid 5880 substrate and Copper top patch, feed, and continuous full ground plane on the underside.",
                "A centered rectangular patch is excited by a microstrip feed line starting at the lower board edge.",
                "Dimensions: sub_w=40mm sub_l=30mm h=1.6mm patch_w=16mm patch_l=12mm feed_w=1.5mm feed_l=9mm metal_thickness=0.035mm.",
                "The modeled frequency range is 2 GHz to 4 GHz.",
                "The waveguide port is specified by port_orientation=ymin, port_xmin=-3.75mm, port_xmax=3.75mm, port_y=-15mm, port_zmin=0mm, and port_zmax=8mm.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    common = {
        "source_path": str(source),
        "source_kind": "original_extract",
        "evidence_label": "paper_fact",
        "status": "usable",
        "page": 4,
        "section": "Antenna Geometry",
    }
    port = {
        **common,
        "id": "paper-port",
        "fact_type": "port",
        "summary": "port_orientation=ymin, port_xmin=-3.75mm, port_xmax=3.75mm, port_y=-15mm, port_zmin=0mm, port_zmax=8mm",
    }
    evidence = [
        {**common, "id": "paper-antenna", "fact_type": "antenna_type", "summary": "microstrip_patch antenna"},
        {**common, "id": "paper-layer", "fact_type": "layer_stack", "summary": "Rogers 5880 substrate with Copper conductors"},
        {**common, "id": "paper-feed", "fact_type": "feed", "summary": "microstrip feed line from the lower board edge"},
        {**common, "id": "paper-ground", "fact_type": "ground", "summary": "continuous full ground plane covers the underside"},
        {**common, "id": "paper-patch", "fact_type": "patch", "summary": "centered rectangular patch"},
        {
            **common,
            "id": "paper-parameters",
            "fact_type": "parameter",
            "summary": "sub_w=40mm sub_l=30mm h=1.6mm patch_w=16mm patch_l=12mm feed_w=1.5mm feed_l=9mm metal_thickness=0.035mm",
        },
        {**common, "id": "paper-frequency", "fact_type": "frequency_range", "summary": "2 GHz to 4 GHz"},
    ]
    if include_port:
        evidence.append(port)
    request = {
        "schema_version": "1.0",
        "paper_id": "paper-integration",
        "objective": "Prepare a traceable microstrip patch model and stop before CST execution.",
        "target_type": "paper_reproduction",
        "antenna_family": "microstrip_patch",
        "frequency_range": {"min": 2.0, "max": 4.0, "unit": "GHz"},
        "evidence": evidence,
    }
    return request


class ParentSubgraphIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        previous = (
            runtime_llm_config.enabled,
            runtime_llm_config.base_url,
            runtime_llm_config.api_key,
            runtime_llm_config.model_name,
        )
        runtime_llm_config.enabled = False
        self.addCleanup(
            self._restore_runtime_llm,
            previous,
        )

    @staticmethod
    def _restore_runtime_llm(previous: tuple[bool, str, str, str]) -> None:
        (
            runtime_llm_config.enabled,
            runtime_llm_config.base_url,
            runtime_llm_config.api_key,
            runtime_llm_config.model_name,
        ) = previous

    @staticmethod
    def scheduler(root: str) -> Scheduler:
        return Scheduler(
            Settings(workspace_root=root, logs_root=root, llm_enabled=False),
            Blackboard(root),
            MemoryManager(),
            CapabilityRegistry(),
            AuditLog(root),
            StreamPublisher(),
        )

    def test_parent_has_three_compiled_child_graphs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = self.scheduler(root).langgraph_runtime
            parent = set(runtime.graph.get_graph().nodes)
            planning = set(runtime.planning_subgraph.get_graph().nodes)
            execution = set(runtime.execution_subgraph.get_graph().nodes)
            repair = set(runtime.repair_subgraph.get_graph().nodes)

        self.assertEqual(
            parent - {"__start__", "__end__"},
            {"planning_subgraph", "execution_subgraph", "repair_subgraph", "failure_report", "finalize"},
        )
        self.assertTrue({"initial_skill_route", "plan_generate", "plan_validate"}.issubset(planning))
        self.assertTrue({"task_agent", "module_review", "global_review", "central_decision"}.issubset(execution))
        self.assertTrue(
            {"failure_triage", "repair_plan", "repair_task", "repair_review", "replay_failed_step"}.issubset(repair)
        )

    def test_single_parent_invoke_runs_real_skill_subprocess_to_model_spec(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            request = modeling_request(Path(root))
            with patch.object(
                scheduler.langgraph_runtime.graph,
                "invoke",
                wraps=scheduler.langgraph_runtime.graph.invoke,
            ) as parent_invoke, patch.object(
                scheduler.modeling_preparation,
                "prepare",
                wraps=scheduler.modeling_preparation.prepare,
            ) as real_prepare:
                state = scheduler.create_task_from_request(
                    "复现微带贴片天线并生成可验证 CST 建模规格",
                    modeling_request=request,
                )

            self.assertEqual(parent_invoke.call_count, 1)
            self.assertEqual(real_prepare.call_count, 1)
            self.assertEqual(state["state"], "completed")
            self.assertEqual(state["task_metadata"]["dynamic_plan"]["source"], "scheduler_fallback")
            result = state["task_metadata"]["modeling_preparation_result"]
            self.assertTrue(result["success"])
            self.assertFalse(result["cst_executed"])
            self.assertTrue(Path(result["artifacts"]["cst_model_spec"]).is_file())
            self.assertGreaterEqual(len(result["subprocess_trace"]), 8)
            roles = {item.get("agent_role") for item in state["task_metadata"]["subagent_records"]}
            self.assertIn("task", roles)
            self.assertIn("module_review", roles)
            self.assertIn("global_review", roles)

    def test_missing_evidence_is_repaired_then_only_failed_step_replays(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            request = modeling_request(Path(root))
            request["evidence"] = [item for item in request["evidence"] if item["id"] != "paper-ground"]
            state = scheduler.create_task_from_request(
                "复现微带贴片天线，证据不足时自动补充后继续建模",
                modeling_request=request,
            )

            self.assertEqual(state["state"], "completed")
            self.assertEqual(state["task_metadata"]["modeling_preparation_attempt"], 2)
            self.assertTrue(state["task_metadata"]["modeling_preparation_result"]["success"])
            recovered = next(
                item
                for item in state["task_metadata"]["modeling_request"]["evidence"]
                if item["fact_type"] == "ground"
            )
            self.assertTrue(recovered["id"].startswith("repair-ground-"))
            self.assertEqual(recovered["page"], 4)
            self.assertEqual(recovered["section"], "Antenna Geometry")
            self.assertTrue(Path(recovered["source_path"]).is_file())
            failures = scheduler.failure_store.list(task_id=state["task_id"])
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["category"], "evidence_gap")
            self.assertEqual(failures[0]["status"], "repaired")
            self.assertEqual(failures[0]["repair_rounds"], 1)
            self.assertTrue(failures[0]["repair_records"][0]["changed"])

    def test_route_review_failure_refreshes_route_then_replans(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            original = scheduler.skill_router.review_plan
            calls = 0

            def review(plan):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {
                        "decision": "block",
                        "status": "blocked",
                        "blocking_findings": [{"reason": "missing_skill in skill_route_plan"}],
                    }
                return original(plan)

            with patch.object(scheduler.skill_router, "review_plan", side_effect=review):
                state = scheduler.create_task_from_request("general status summary")

            self.assertEqual(state["state"], "completed")
            self.assertEqual(calls, 2)
            failures = scheduler.failure_store.list(task_id=state["task_id"])
            self.assertEqual(failures[0]["category"], "route")
            self.assertEqual(failures[0]["status"], "repaired")
            self.assertIn("route_repair_context", state["task_metadata"])

    def test_invalid_plan_is_regenerated_and_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            original = scheduler._validate_dynamic_plan
            calls = 0

            def validate(plan):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return scheduler._plan_validation_result(
                        [{"type": "schema_error", "reason": "invalid dynamic_plan missing step_goal"}],
                        source="test",
                    )
                return original(plan)

            with patch.object(scheduler, "_validate_dynamic_plan", side_effect=validate):
                state = scheduler.create_task_from_request("general status summary")

            self.assertEqual(state["state"], "completed")
            self.assertEqual(calls, 2)
            self.assertEqual(state["task_metadata"]["dynamic_plan"]["plan_version"], 2)
            failures = scheduler.failure_store.list(task_id=state["task_id"])
            self.assertEqual(failures[0]["category"], "plan")
            self.assertEqual(failures[0]["status"], "repaired")

    def test_transient_task_retry_succeeds_and_exhaustion_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            original = scheduler._execute_dynamic_step_handler
            calls = 0

            def flaky(**kwargs):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise ConnectionError("connection reset by peer")
                return original(**kwargs)

            with patch.object(scheduler, "_execute_dynamic_step_handler", side_effect=flaky):
                state = scheduler.create_task_from_request("general status summary")
            self.assertEqual(state["state"], "completed")
            self.assertEqual(calls, 3)

        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            original = scheduler._execute_dynamic_step_handler
            calls = 0

            def exhausted(**kwargs):
                nonlocal calls
                if kwargs["step"].get("action") == "general":
                    calls += 1
                    raise ConnectionError("connection reset by peer")
                return original(**kwargs)

            with patch.object(scheduler, "_execute_dynamic_step_handler", side_effect=exhausted):
                state = scheduler.create_task_from_request("general status summary")
            self.assertEqual(calls, 3)
            self.assertEqual(state["state"], "failed")
            failure = scheduler.failure_store.list(task_id=state["task_id"])[0]
            self.assertEqual(failure["category"], "transient")
            self.assertEqual(failure["status"], "blocked")

    def test_transient_planner_retries_then_uses_validated_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            calls = 0

            def unavailable(**_kwargs):
                nonlocal calls
                calls += 1
                raise LLMGenerationError("central_planner_llm_failed: error code: 503 service temporarily unavailable")

            scheduler.llm.generate_json = unavailable
            state = scheduler.create_task_from_request("general status summary")

            self.assertEqual(calls, scheduler.settings.max_node_retries)
            self.assertEqual(state["state"], "completed")
            self.assertEqual(state["task_metadata"]["dynamic_plan"]["source"], "scheduler_fallback")
            self.assertEqual(
                state["task_metadata"]["planner_generation_attempts"]["1"],
                scheduler.settings.max_node_retries,
            )

    def test_artifact_repair_replays_only_failed_step(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            request = modeling_request(Path(root))
            original_prepare = scheduler.modeling_preparation.prepare
            attempts = 0

            def corrupt_first_spec(modeling_input, output_dir):
                nonlocal attempts
                attempts += 1
                result = original_prepare(modeling_input, output_dir)
                if attempts == 1 and result.get("success"):
                    Path(result["artifacts"]["cst_model_spec"]).write_text("{}\n", encoding="utf-8")
                return result

            with patch.object(scheduler.modeling_preparation, "prepare", side_effect=corrupt_first_spec):
                state = scheduler.create_task_from_request(
                    "Regenerate a malformed CST model specification from validated antenna evidence.",
                    modeling_request=request,
                )

            self.assertEqual(state["state"], "completed")
            self.assertEqual(attempts, 2)
            final_result = state["task_metadata"]["modeling_preparation_result"]
            validation = scheduler.modeling_preparation.validate_cst_model_spec_artifacts(
                final_result["artifacts"]["cst_model_spec"],
                final_result["artifacts"]["cst_model_spec_manifest"],
            )
            self.assertTrue(validation["valid"], validation)
            failure = scheduler.failure_store.list(task_id=state["task_id"])[0]
            self.assertEqual(failure["category"], "artifact_schema")
            self.assertEqual(failure["status"], "repaired")

    def test_frequency_conflict_is_repaired_from_traceable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            request = modeling_request(Path(root))
            request["frequency_range"] = {"min": 2.0, "max": 5.0, "unit": "GHz"}
            state = scheduler.create_task_from_request(
                "纠正普通频段参数后生成贴片天线建模规格",
                modeling_request=request,
            )
            self.assertEqual(state["state"], "completed")
            self.assertEqual(state["task_metadata"]["modeling_preparation_attempt"], 2)
            failure = scheduler.failure_store.list(task_id=state["task_id"])[0]
            self.assertEqual(failure["category"], "modeling_parameter")
            self.assertEqual(failure["status"], "repaired")

    def test_missing_safe_parameter_is_restored_from_parameter_fact_source(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            request = modeling_request(Path(root))
            parameter_fact = next(item for item in request["evidence"] if item["fact_type"] == "parameter")
            parameter_fact["summary"] = parameter_fact["summary"].replace(" patch_l=12mm", "")

            state = scheduler.create_task_from_request(
                "从可追溯参数原文恢复缺失的普通贴片尺寸",
                modeling_request=request,
            )

            self.assertEqual(state["state"], "completed")
            self.assertEqual(state["task_metadata"]["modeling_preparation_attempt"], 2)
            repaired_fact = next(
                item
                for item in state["task_metadata"]["modeling_request"]["evidence"]
                if item["fact_type"] == "parameter"
            )
            self.assertIn("patch_l=12mm", repaired_fact["summary"])
            failure = scheduler.failure_store.list(task_id=state["task_id"])[0]
            self.assertEqual(failure["category"], "modeling_parameter")
            self.assertEqual(failure["status"], "repaired")

    def test_missing_feed_parameter_is_never_auto_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            request = modeling_request(Path(root))
            parameter_fact = next(item for item in request["evidence"] if item["fact_type"] == "parameter")
            parameter_fact["summary"] = parameter_fact["summary"].replace(" feed_w=1.5mm", "")

            state = scheduler.create_task_from_request(
                "馈电参数缺失时必须等待人工，不允许自动改馈电",
                modeling_request=request,
            )

            self.assertEqual(state["state"], "waiting_approval")
            self.assertEqual(state["task_metadata"]["modeling_preparation_attempt"], 1)
            persisted_fact = next(
                item
                for item in state["task_metadata"]["modeling_request"]["evidence"]
                if item["fact_type"] == "parameter"
            )
            self.assertNotIn("feed_w=1.5mm", persisted_fact["summary"])
            failure = scheduler.failure_store.list(task_id=state["task_id"])[0]
            self.assertEqual(failure["repairability"], "approval_required")
            self.assertEqual(failure["repair_rounds"], 0)

    def test_protected_manual_and_unknown_failures_stop_before_repair_task(self) -> None:
        cases = {
            "CST solver timeout": "waiting_approval",
            "invalid port placement": "waiting_approval",
            "feed topology invalid": "waiting_approval",
            "boundary invalid": "waiting_approval",
            "topology change required": "waiting_approval",
            "permission denied": "waiting_approval",
            "license unavailable": "waiting_approval",
            "user input required for target frequency": "waiting_approval",
            "unexpected invariant zeta-431": "failed",
        }
        for message, expected_state in cases.items():
            with self.subTest(message=message), tempfile.TemporaryDirectory() as root:
                scheduler = self.scheduler(root)
                original = scheduler._execute_dynamic_step_handler

                def fail_current_step(**kwargs):
                    if str((kwargs.get("step") or {}).get("action") or "") == "report":
                        return original(**kwargs)
                    return self._repairable_output(message)

                with patch.object(
                    scheduler,
                    "_execute_dynamic_step_handler",
                    side_effect=fail_current_step,
                ):
                    state = scheduler.create_task_from_request("general status summary")
                self.assertEqual(state["state"], expected_state)
                self.assertFalse(any("repair_task_agent" in node for node in state["nodes"]))
                if expected_state == "failed":
                    self.assertTrue(state["task_metadata"].get("reports"))
                    self.assertEqual(state["task_metadata"]["reports"][-1]["report_type"], "failed_report")

    def test_source_without_missing_fact_exhausts_three_rounds_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            request = modeling_request(Path(root))
            request["evidence"] = [item for item in request["evidence"] if item["id"] != "paper-ground"]
            source = Path(request["evidence"][0]["source_path"])
            text = source.read_text(encoding="utf-8")
            text = text.replace(" and continuous full ground plane on the underside", "")
            source.write_text(text, encoding="utf-8")
            state = scheduler.create_task_from_request(
                "原论文确实没有接地证据时停止自动修复",
                modeling_request=request,
            )
            self.assertEqual(state["state"], "failed")
            self.assertEqual(state["task_metadata"]["modeling_preparation_attempt"], 1)
            failure = scheduler.failure_store.list(task_id=state["task_id"])[0]
            self.assertEqual(failure["status"], "exhausted")
            self.assertEqual(failure["repair_rounds"], 3)
            self.assertTrue(state["task_metadata"].get("reports"))
            self.assertEqual(state["task_metadata"]["reports"][-1]["report_type"], "failed_report")
            self.assertTrue(all(not item["changed"] for item in failure["repair_records"]))

    def test_non_direct_or_inexact_locator_is_not_used_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            request = modeling_request(Path(root))
            request["evidence"] = [item for item in request["evidence"] if item["fact_type"] != "ground"]
            for item in request["evidence"]:
                item["source_kind"] = "retrieval_chunk"
                item["evidence_label"] = "retrieved_summary"

            recovered = scheduler.modeling_preparation.recover_missing_evidence(
                request,
                missing_inputs=["evidence:ground"],
                blockers=["insufficient evidence: ground"],
            )

            self.assertEqual(recovered, [])

    def test_frequency_recovery_uses_explicit_range_not_other_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scheduler = self.scheduler(root)
            request = modeling_request(Path(root))
            request["evidence"] = [item for item in request["evidence"] if item["fact_type"] != "frequency_range"]
            source = Path(request["evidence"][0]["source_path"])
            text = source.read_text(encoding="utf-8")
            text = text.replace(
                "The modeled frequency range is 2 GHz to 4 GHz.",
                "The resonance is 2.45 GHz.\nThe modeled frequency range is 2 GHz to 4 GHz.",
            )
            source.write_text(text, encoding="utf-8")

            recovered = scheduler.modeling_preparation.recover_missing_evidence(
                request,
                missing_inputs=["evidence:frequency_range"],
                blockers=["insufficient evidence: frequency range"],
            )

            frequency = next(item for item in recovered if item["fact_type"] == "frequency_range")
            self.assertEqual(frequency["summary"], "2 GHz to 4 GHz")

    @staticmethod
    def _repairable_output(reason: str) -> dict:
        review = {
            "schema_version": "1.0",
            "decision": "revise",
            "status": "revise",
            "evidence_level": "D",
            "blocking_findings": [{"type": "fixture_failure", "reason": reason}],
            "required_fixes": [reason],
            "reflection": {"status": "blocked", "error_type": "fixture_failure", "confidence": 0.99},
        }
        return {
            "status": "failed",
            "repairable_failure": {"code": reason, "blockers": [reason]},
            "evidence_refs": [],
            "_module_review": review,
        }


if __name__ == "__main__":
    unittest.main()
