import unittest
from unittest.mock import Mock


class DynamicTaskManager:
    """被测类（原样保留）"""
    def _apply_dynamic_execution_result(self, task_id: str, execution_result: dict) -> dict:
        if execution_result["final_decision"] in {"block_task", "reroute_plan", "revise_current_step"}:
            blockers = list(self.blackboard.get(task_id).blockers)
            if not any(item.get("reason") == execution_result["reason"] for item in blockers):
                blockers.append({"type": execution_result["final_decision"], "reason": execution_result["reason"]})
            self.blackboard.update(task_id, state="failed", blockers=blockers)
            self._set_v2_metadata(task_id, current_stage=execution_result["final_stage"], failure_reason=execution_result["reason"])
            self._event(task_id, "central_agent", "review", "revoked", "V2.2.4 dynamic task blocked", reason=execution_result["reason"])
        elif execution_result["final_decision"] == "wait_user":
            self.blackboard.update(task_id, state="waiting_approval")
            self._set_v2_metadata(task_id, current_stage=execution_result["final_stage"])
            self._event(task_id, "central_agent", "status", "pending", "V2.2.4 dynamic task waits for user")
        elif execution_result["final_decision"] == "refresh_skill_route":
            self.blackboard.update(task_id, state="failed", blockers=list(self.blackboard.get(task_id).blockers) + [{"type": "skill_route_refreshed", "reason": execution_result["reason"]}])
            self._set_v2_metadata(task_id, current_stage=execution_result["final_stage"], failure_reason=execution_result["reason"])
            self._event(task_id, "central_agent", "review", "pending", "V2.2.4 refreshed route; task needs rerun")
        else:
            self.blackboard.update(task_id, state="completed")
            self._set_v2_metadata(task_id, current_stage="completed")
            self._event(task_id, "central_agent", "final", "finalized", "V2.2.4 dynamic task completed")
        return self.blackboard.get(task_id).to_dict()


class TestApplyDynamicExecutionResult(unittest.TestCase):
    """单元测试 - 包含输入输出打印"""

    def setUp(self):
        self.task_id = "task-123"
        # 模拟任务对象
        self.mock_task = Mock()
        self.mock_task.blockers = []
        # 默认 to_dict 返回值（可在各测试中覆盖）
        self.mock_task.to_dict.return_value = {"task_id": self.task_id, "state": "dummy"}

        self.mock_blackboard = Mock()
        self.mock_blackboard.get.return_value = self.mock_task

        self.manager = DynamicTaskManager()
        self.manager.blackboard = self.mock_blackboard
        self.manager._set_v2_metadata = Mock()
        self.manager._event = Mock()

        self.sep = "-" * 60

    def _print_input(self, execution_result):
        print(f"\n{self.sep}")
        print("INPUT execution_result:")
        for k, v in execution_result.items():
            print(f"  {k}: {v}")

    def _print_output(self, result, update_args=None, metadata_args=None, event_args=None):
        print("OUTPUT return value:")
        print(f"  {result}")
        if update_args:
            print("blackboard.update called with:")
            print(f"  {update_args}")
        if metadata_args:
            print("_set_v2_metadata called with:")
            print(f"  {metadata_args}")
        if event_args:
            print("_event called with:")
            print(f"  {event_args}")
        print(self.sep)

    def test_block_task_new_blocker(self):
        # 设置 to_dict 返回值（模拟任务最终状态）
        self.mock_task.to_dict.return_value = {"task_id": self.task_id, "state": "failed"}

        execution_result = {
            "final_decision": "block_task",
            "reason": "New reason",
            "final_stage": "stage_1"
        }
        self._print_input(execution_result)

        result = self.manager._apply_dynamic_execution_result(self.task_id, execution_result)

        update_call = self.mock_blackboard.update.call_args
        metadata_call = self.manager._set_v2_metadata.call_args
        event_call = self.manager._event.call_args

        self._print_output(result, update_call, metadata_call, event_call)

        # 断言
        expected_blockers = [{"type": "block_task", "reason": "New reason"}]
        self.mock_blackboard.update.assert_called_once_with(
            self.task_id, state="failed", blockers=expected_blockers
        )
        self.manager._set_v2_metadata.assert_called_once_with(
            self.task_id, current_stage="stage_1", failure_reason="New reason"
        )
        self.manager._event.assert_called_once_with(
            self.task_id, "central_agent", "review", "revoked",
            "V2.2.4 dynamic task blocked", reason="New reason"
        )
        # 验证返回值
        self.assertEqual(result, {"task_id": self.task_id, "state": "failed"})

    def test_block_task_existing_blocker_same_reason(self):
        existing_blocker = {"type": "block_task", "reason": "Existing reason"}
        self.mock_task.blockers = [existing_blocker]
        self.mock_task.to_dict.return_value = {"task_id": self.task_id, "state": "failed"}

        execution_result = {
            "final_decision": "block_task",
            "reason": "Existing reason",
            "final_stage": "stage_1"
        }
        self._print_input(execution_result)

        result = self.manager._apply_dynamic_execution_result(self.task_id, execution_result)

        update_call = self.mock_blackboard.update.call_args
        metadata_call = self.manager._set_v2_metadata.call_args
        event_call = self.manager._event.call_args

        self._print_output(result, update_call, metadata_call, event_call)

        # 断言：blockers 不应增加
        self.mock_blackboard.update.assert_called_once_with(
            self.task_id, state="failed", blockers=[existing_blocker]
        )
        self.manager._set_v2_metadata.assert_called_once()
        self.manager._event.assert_called_once()
        self.assertEqual(result, {"task_id": self.task_id, "state": "failed"})

    def test_reroute_plan(self):
        self.mock_task.to_dict.return_value = {"task_id": self.task_id, "state": "failed"}

        execution_result = {
            "final_decision": "reroute_plan",
            "reason": "Reroute reason",
            "final_stage": "stage_2"
        }
        self._print_input(execution_result)

        result = self.manager._apply_dynamic_execution_result(self.task_id, execution_result)

        update_call = self.mock_blackboard.update.call_args
        metadata_call = self.manager._set_v2_metadata.call_args
        event_call = self.manager._event.call_args

        self._print_output(result, update_call, metadata_call, event_call)

        expected_blockers = [{"type": "reroute_plan", "reason": "Reroute reason"}]
        self.mock_blackboard.update.assert_called_once_with(
            self.task_id, state="failed", blockers=expected_blockers
        )
        self.manager._set_v2_metadata.assert_called_once_with(
            self.task_id, current_stage="stage_2", failure_reason="Reroute reason"
        )
        self.manager._event.assert_called_once_with(
            self.task_id, "central_agent", "review", "revoked",
            "V2.2.4 dynamic task blocked", reason="Reroute reason"
        )
        self.assertEqual(result, {"task_id": self.task_id, "state": "failed"})

    def test_revise_current_step(self):
        self.mock_task.to_dict.return_value = {"task_id": self.task_id, "state": "failed"}

        execution_result = {
            "final_decision": "revise_current_step",
            "reason": "Revise reason",
            "final_stage": "stage_3"
        }
        self._print_input(execution_result)

        result = self.manager._apply_dynamic_execution_result(self.task_id, execution_result)

        update_call = self.mock_blackboard.update.call_args
        metadata_call = self.manager._set_v2_metadata.call_args
        event_call = self.manager._event.call_args

        self._print_output(result, update_call, metadata_call, event_call)

        expected_blockers = [{"type": "revise_current_step", "reason": "Revise reason"}]
        self.mock_blackboard.update.assert_called_once_with(
            self.task_id, state="failed", blockers=expected_blockers
        )
        self.manager._set_v2_metadata.assert_called_once_with(
            self.task_id, current_stage="stage_3", failure_reason="Revise reason"
        )
        self.manager._event.assert_called_once_with(
            self.task_id, "central_agent", "review", "revoked",
            "V2.2.4 dynamic task blocked", reason="Revise reason"
        )
        self.assertEqual(result, {"task_id": self.task_id, "state": "failed"})

    def test_wait_user(self):
        self.mock_task.to_dict.return_value = {"task_id": self.task_id, "state": "waiting_approval"}

        execution_result = {
            "final_decision": "wait_user",
            "final_stage": "waiting_stage"
        }
        self._print_input(execution_result)

        result = self.manager._apply_dynamic_execution_result(self.task_id, execution_result)

        update_call = self.mock_blackboard.update.call_args
        metadata_call = self.manager._set_v2_metadata.call_args
        event_call = self.manager._event.call_args

        self._print_output(result, update_call, metadata_call, event_call)

        self.mock_blackboard.update.assert_called_once_with(
            self.task_id, state="waiting_approval"
        )
        self.manager._set_v2_metadata.assert_called_once_with(
            self.task_id, current_stage="waiting_stage"
        )
        self.manager._event.assert_called_once_with(
            self.task_id, "central_agent", "status", "pending",
            "V2.2.4 dynamic task waits for user"
        )
        self.assertEqual(result, {"task_id": self.task_id, "state": "waiting_approval"})

    def test_refresh_skill_route(self):
        existing_blocker = {"type": "old", "reason": "old reason"}
        self.mock_task.blockers = [existing_blocker]
        self.mock_task.to_dict.return_value = {"task_id": self.task_id, "state": "failed"}

        execution_result = {
            "final_decision": "refresh_skill_route",
            "reason": "Route refresh reason",
            "final_stage": "refresh_stage"
        }
        self._print_input(execution_result)

        result = self.manager._apply_dynamic_execution_result(self.task_id, execution_result)

        update_call = self.mock_blackboard.update.call_args
        metadata_call = self.manager._set_v2_metadata.call_args
        event_call = self.manager._event.call_args

        self._print_output(result, update_call, metadata_call, event_call)

        expected_blockers = [
            existing_blocker,
            {"type": "skill_route_refreshed", "reason": "Route refresh reason"}
        ]
        self.mock_blackboard.update.assert_called_once_with(
            self.task_id, state="failed", blockers=expected_blockers
        )
        self.manager._set_v2_metadata.assert_called_once_with(
            self.task_id, current_stage="refresh_stage", failure_reason="Route refresh reason"
        )
        self.manager._event.assert_called_once_with(
            self.task_id, "central_agent", "review", "pending",
            "V2.2.4 refreshed route; task needs rerun"
        )
        self.assertEqual(result, {"task_id": self.task_id, "state": "failed"})

    def test_other_decision_completed(self):
        self.mock_task.to_dict.return_value = {"task_id": self.task_id, "state": "completed"}

        execution_result = {
            "final_decision": "proceed",
            "final_stage": "final_stage"
        }
        self._print_input(execution_result)

        result = self.manager._apply_dynamic_execution_result(self.task_id, execution_result)

        update_call = self.mock_blackboard.update.call_args
        metadata_call = self.manager._set_v2_metadata.call_args
        event_call = self.manager._event.call_args

        self._print_output(result, update_call, metadata_call, event_call)

        self.mock_blackboard.update.assert_called_once_with(
            self.task_id, state="completed"
        )
        self.manager._set_v2_metadata.assert_called_once_with(
            self.task_id, current_stage="completed"
        )
        self.manager._event.assert_called_once_with(
            self.task_id, "central_agent", "final", "finalized",
            "V2.2.4 dynamic task completed"
        )
        self.assertEqual(result, {"task_id": self.task_id, "state": "completed"})

    def test_return_value(self):
        # 单独验证返回值一定是 to_dict() 的结果
        expected_dict = {"task_id": self.task_id, "state": "completed"}
        self.mock_task.to_dict.return_value = expected_dict
        execution_result = {"final_decision": "dummy", "final_stage": "x"}
        self._print_input(execution_result)

        result = self.manager._apply_dynamic_execution_result(self.task_id, execution_result)

        update_call = self.mock_blackboard.update.call_args
        metadata_call = self.manager._set_v2_metadata.call_args
        event_call = self.manager._event.call_args
        self._print_output(result, update_call, metadata_call, event_call)

        self.mock_blackboard.update.assert_called_once_with(self.task_id, state="completed")
        self.manager._set_v2_metadata.assert_called_once_with(self.task_id, current_stage="completed")
        self.manager._event.assert_called_once_with(
            self.task_id, "central_agent", "final", "finalized",
            "V2.2.4 dynamic task completed"
        )
        # 关键断言：返回值等于我们预先设置的 to_dict 返回值
        self.assertEqual(result, expected_dict)


if __name__ == '__main__':
    unittest.main()