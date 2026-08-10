from __future__ import annotations

import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from agent_runtime.failure_store import (
    FailureStore,
    InvalidFailureTransitionError,
    RepairLimitExceededError,
    RepairNotAllowedError,
)
from agent_runtime.repair_policy import FailureClassifier, RepairPolicy


def _concurrent_repair(root: str, failure_id: str, queue) -> None:
    store = FailureStore(root, max_repair_rounds=3)
    for _ in range(100):
        try:
            started = store.begin_repair(failure_id, action="regenerate_artifact")
            store.fail_repair(failure_id, reason="concurrency boundary fixture")
            queue.put(("started", started["repair_rounds"]))
            return
        except InvalidFailureTransitionError:
            time.sleep(0.005)
        except (RepairLimitExceededError, RepairNotAllowedError):
            queue.put(("stopped", None))
            return
    queue.put(("stopped", None))


class FailurePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = FailureClassifier()
        self.policy = RepairPolicy(max_repair_rounds=3)

    def test_supported_categories_and_actions(self) -> None:
        cases = {
            "route": ("missing_skill in skill_route_plan", "refresh_skill_route"),
            "plan": ("invalid dynamic_plan missing step_goal", "regenerate_plan"),
            "artifact_schema": ("artifact schema invalid", "regenerate_artifact"),
            "evidence_gap": ("insufficient_evidence no traceable evidence", "supplement_read_only_evidence"),
            "modeling_parameter": ("parameter width out of range", "revise_modeling_input"),
        }
        for category, (message, action) in cases.items():
            with self.subTest(category=category):
                classification = self.classifier.classify(message)
                decision = self.policy.decide(classification)
                self.assertEqual(classification.category, category)
                self.assertEqual(decision.repairability, "automatic")
                self.assertEqual(decision.action, action)

    def test_retry_and_repair_are_separate(self) -> None:
        decision = self.policy.decide(self.classifier.classify(ConnectionError("connection reset")))
        self.assertEqual(decision.category, "transient")
        self.assertEqual(decision.repairability, "not_repairable")
        self.assertEqual(decision.action, "langgraph_retry_exhausted")

    def test_protected_and_manual_failures_never_auto_repair(self) -> None:
        waiting = (
            "CST solver timeout",
            "invalid port placement",
            "feed topology invalid",
            "boundary invalid",
            "topology change required",
            "permission denied",
            "license unavailable",
            "user input required for target frequency",
        )
        for message in waiting:
            with self.subTest(message=message):
                decision = self.policy.decide(self.classifier.classify(message))
                self.assertEqual(decision.repairability, "approval_required")
        unknown = self.policy.decide(self.classifier.classify("unexpected invariant zeta-431"))
        self.assertEqual(unknown.category, "code_unknown")
        self.assertEqual(unknown.repairability, "not_repairable")


class FailureStoreTests(unittest.TestCase):
    def test_stable_identity_and_explicit_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = FailureStore(root)
            first = store.create(task_id="task-1", step_id="step-2", error="artifact schema invalid")
            same = store.create(task_id="task-1", step_id="step-2", error="artifact schema invalid")
            other = store.create(task_id="task-1", step_id="step-2", error="artifact missing required field x")
            self.assertEqual(first["failure_id"], same["failure_id"])
            self.assertNotEqual(first["failure_id"], other["failure_id"])
            started = store.begin_repair(first["failure_id"], action="regenerate_artifact")
            self.assertEqual(started["status"], "repairing")
            repaired = store.complete_repair(
                first["failure_id"], changed=True, change_refs=["artifact-v2.json"], review={"decision": "pass"}
            )
            self.assertEqual(repaired["status"], "repaired")
            self.assertTrue(repaired["repair_records"][0]["changed"])

    def test_waiting_user_cannot_enter_repair(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = FailureStore(root)
            for message in ("CST timeout", "invalid port placement", "permission denied"):
                record = store.create(task_id="task", step_id=message.replace(" ", "-"), error=message)
                self.assertEqual(record["status"], "waiting_user")
                with self.assertRaises(RepairNotAllowedError):
                    store.begin_repair(record["failure_id"], action=record["recommended_action"])

    def test_cross_process_lock_enforces_three_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = FailureStore(root, max_repair_rounds=3)
            record = store.create(task_id="task", step_id="artifact", error="artifact schema invalid")
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            workers = [
                context.Process(target=_concurrent_repair, args=(root, record["failure_id"], queue))
                for _ in range(8)
            ]
            for worker in workers:
                worker.start()
            results = [queue.get(timeout=20) for _ in workers]
            for worker in workers:
                worker.join(timeout=20)
                self.assertEqual(worker.exitcode, 0)
            stored = store.get(record["failure_id"])
            started_rounds = sorted(item[1] for item in results if item[0] == "started")
            self.assertEqual(started_rounds, [1, 2, 3])
            self.assertEqual(stored["status"], "exhausted")
            self.assertEqual(stored["repair_rounds"], 3)
            self.assertEqual(len(stored["repair_records"]), 3)

    def test_store_is_minimal_and_has_no_second_approval_system(self) -> None:
        import agent_runtime.failure_store as module

        self.assertFalse(hasattr(module, "ApprovalTokenIssuer"))
        self.assertFalse(hasattr(module, "ApprovalVerifier"))
        self.assertFalse(hasattr(module, "ApprovalGrantService"))
        self.assertFalse(hasattr(module, "MissingIntegrityKeyError"))
        self.assertFalse(any(Path(__file__).parents[1].glob("**/.failure_store_key.json")))


if __name__ == "__main__":
    unittest.main()
