from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt

from .io_contracts import ExecutionGraphInput, IOContractResolver, PlanningGraphInput, RepairGraphInput
from .orchestration.graph_nodes import ExecutionGraphNodesMixin, PlanningGraphNodesMixin, RepairGraphNodesMixin
from .orchestration.retry import retry_transient_runtime_error
from .orchestration.state import DynamicGraphState
from .llm import LLMGenerationError
from .utils import now_iso, stable_hash






class DynamicLangGraphRuntime(PlanningGraphNodesMixin, ExecutionGraphNodesMixin, RepairGraphNodesMixin):
    """The single orchestration runtime for dynamic task execution."""

    def __init__(self, scheduler: Any, workspace_root: str | Path, *, checkpointer: Any | None = None):
        self.scheduler = scheduler
        self.workspace_root = Path(workspace_root)
        self.checkpointer, self.checkpoint_backend, self.checkpoint_durable, self.checkpoint_error = (
            self._select_checkpointer(checkpointer)
        )
        self.planning_subgraph_builder = self._build_planning_subgraph()
        self.execution_subgraph_builder = self._build_execution_subgraph()
        self.repair_subgraph_builder = self._build_repair_subgraph()
        self.planning_subgraph = self.planning_subgraph_builder.compile()
        self.execution_subgraph = self.execution_subgraph_builder.compile()
        self.repair_subgraph = self.repair_subgraph_builder.compile()
        self.io = IOContractResolver()
        self.parent_graph_builder = self._build_parent_graph()
        self.graph_builder = self.parent_graph_builder
        self.graph = self.parent_graph_builder.compile(checkpointer=self.checkpointer)

    def _select_checkpointer(self, explicit: Any | None) -> tuple[Any, str, bool, str | None]:
        if explicit is not None:
            module = explicit.__class__.__module__.lower()
            if "sqlite" in module:
                return explicit, "langgraph_sqlite_test", True, None
            if "redis" in module:
                return explicit, "langgraph_redis", True, None
            if isinstance(explicit, InMemorySaver):
                return explicit, "langgraph_in_memory_non_durable", False, "restart_resume_unavailable"
            return explicit, f"langgraph_{explicit.__class__.__name__.lower()}", True, None

        checkpoint = getattr(self.scheduler, "checkpoint", None)
        if checkpoint is not None and bool(getattr(checkpoint, "available", False)):
            try:
                from langgraph.checkpoint.redis import RedisSaver

                saver = RedisSaver(redis_url=self.scheduler.settings.redis_url)
                saver.setup()
                return saver, "langgraph_redis", True, None
            except Exception as exc:
                error = f"redis_saver_unavailable:{exc}"
                return InMemorySaver(), "langgraph_in_memory_non_durable", False, error
        return InMemorySaver(), "langgraph_in_memory_non_durable", False, "redis_unavailable"

    def _retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            initial_interval=0.05,
            backoff_factor=2.0,
            max_interval=1.0,
            max_attempts=max(1, int(self.scheduler.settings.max_node_retries)),
            jitter=False,
            retry_on=retry_transient_runtime_error,
        )

    def _build_planning_subgraph(self) -> StateGraph:
        graph = StateGraph(DynamicGraphState)
        retry_policy = self._retry_policy()
        graph.add_node("initial_skill_route", self._initial_skill_route, retry_policy=retry_policy)
        graph.add_node("skill_route_review", self._skill_route_review, retry_policy=retry_policy)
        graph.add_node("persist_skill_route", self._persist_skill_route)
        graph.add_node("block_skill_route", self._block_skill_route)
        graph.add_node("paperwise_gate", self._paperwise_gate)
        graph.add_node("block_precondition", self._block_precondition)
        graph.add_node("external_llm_gate", self._external_llm_gate)
        graph.add_node("plan_generate", self._plan_generate, retry_policy=retry_policy)
        graph.add_node("plan_normalize", self._plan_normalize)
        graph.add_node("plan_validate", self._plan_validate, retry_policy=retry_policy)
        graph.add_node("persist_plan", self._persist_plan)
        graph.add_node("block_plan", self._block_plan)
        graph.add_node("prepare_execution", self._prepare_execution)
        graph.add_edge(START, "initial_skill_route")
        graph.add_edge("initial_skill_route", "skill_route_review")
        graph.add_edge("skill_route_review", "persist_skill_route")
        graph.add_conditional_edges(
            "persist_skill_route",
            lambda state: "block" if self._review_blocks(state.get("route_review") or {}) else "continue",
            {"block": "block_skill_route", "continue": "paperwise_gate"},
        )
        graph.add_edge("block_skill_route", END)
        graph.add_conditional_edges(
            "paperwise_gate",
            lambda state: "block" if state.get("precondition_error") else "continue",
            {"block": "block_precondition", "continue": "external_llm_gate"},
        )
        graph.add_edge("block_precondition", END)
        graph.add_edge("external_llm_gate", "plan_generate")
        graph.add_edge("plan_generate", "plan_normalize")
        graph.add_edge("plan_normalize", "plan_validate")
        graph.add_edge("plan_validate", "persist_plan")
        graph.add_conditional_edges(
            "persist_plan",
            self._after_plan,
            {"block": "block_plan", "execute": "prepare_execution", "finish": END},
        )
        graph.add_edge("block_plan", END)
        graph.add_edge("prepare_execution", END)
        return graph

    def _build_execution_subgraph(self) -> StateGraph:
        graph = StateGraph(DynamicGraphState)
        graph.add_node("select_step", self._select_step)
        graph.add_node("route_skill", self._route_skill)
        graph.add_node("task_agent", self._task_agent, retry_policy=self._retry_policy())
        graph.add_node("module_review", self._module_review)
        graph.add_node("global_review", self._global_review)
        graph.add_node("central_decision", self._central_decision)
        graph.add_node("commit_step", self._commit_step)
        graph.add_edge(START, "select_step")
        graph.add_conditional_edges(
            "select_step",
            lambda state: "finish" if state.get("done") else "route_skill",
            {"route_skill": "route_skill", "finish": END},
        )
        graph.add_edge("route_skill", "task_agent")
        graph.add_edge("task_agent", "module_review")
        graph.add_conditional_edges(
            "module_review",
            lambda state: "global_review" if state.get("current_step", {}).get("global_review_required") else "central_decision",
            {"global_review": "global_review", "central_decision": "central_decision"},
        )
        graph.add_edge("global_review", "central_decision")
        graph.add_edge("central_decision", "commit_step")
        graph.add_conditional_edges(
            "commit_step",
            self._after_execution_commit,
            {"select_step": "select_step", "finish": END},
        )
        return graph

    def _build_repair_subgraph(self) -> StateGraph:
        graph = StateGraph(DynamicGraphState)
        graph.add_node("failure_triage", self._failure_triage)
        graph.add_node("repair_plan", self._repair_plan)
        graph.add_node("repair_task", self._repair_task)
        graph.add_node("repair_review", self._repair_review)
        graph.add_node("repair_global_review", self._repair_global_review)
        graph.add_node("replay_failed_step", self._replay_failed_step)
        graph.add_node("repair_terminal", self._repair_terminal)
        graph.add_edge(START, "failure_triage")
        graph.add_conditional_edges(
            "failure_triage",
            self._after_failure_triage,
            {"repair": "repair_plan", "terminal": "repair_terminal"},
        )
        graph.add_edge("repair_plan", "repair_task")
        graph.add_edge("repair_task", "repair_review")
        graph.add_conditional_edges(
            "repair_review",
            self._after_repair_review,
            {
                "global_review": "repair_global_review",
                "replay": "replay_failed_step",
                "retry_repair": "repair_plan",
                "terminal": "repair_terminal",
            },
        )
        graph.add_conditional_edges(
            "repair_global_review",
            self._after_repair_global_review,
            {"replay": "replay_failed_step", "retry_repair": "repair_plan", "terminal": "repair_terminal"},
        )
        graph.add_edge("replay_failed_step", END)
        graph.add_edge("repair_terminal", END)
        return graph

    def _build_parent_graph(self) -> StateGraph:
        graph = StateGraph(DynamicGraphState)
        graph.add_node("planning_subgraph", self.planning_subgraph, input_schema=PlanningGraphInput)
        graph.add_node("execution_subgraph", self.execution_subgraph, input_schema=ExecutionGraphInput)
        graph.add_node("repair_subgraph", self.repair_subgraph, input_schema=RepairGraphInput)
        graph.add_node("failure_report", self._failure_report)
        graph.add_node("finalize", self._finish)
        graph.add_conditional_edges(
            START,
            self._parent_entry,
            {"planning": "planning_subgraph", "repair": "repair_subgraph"},
        )
        graph.add_conditional_edges(
            "planning_subgraph",
            self._after_parent_planning,
            {"execution": "execution_subgraph", "repair": "repair_subgraph", "finish": "finalize"},
        )
        graph.add_conditional_edges(
            "execution_subgraph",
            self._after_parent_execution,
            {"repair": "repair_subgraph", "report": "failure_report", "finish": "finalize"},
        )
        graph.add_conditional_edges(
            "repair_subgraph",
            self._after_parent_repair,
            {
                "planning": "planning_subgraph",
                "execution": "execution_subgraph",
                "report": "failure_report",
                "finish": "finalize",
            },
        )
        graph.add_edge("failure_report", "finalize")
        graph.add_edge("finalize", END)
        return graph

    @staticmethod
    def _parent_entry(state: DynamicGraphState) -> str:
        return "repair" if state.get("operation") == "failure" else "planning"

    @staticmethod
    def _after_parent_planning(state: DynamicGraphState) -> str:
        if state.get("needs_repair"):
            return "repair"
        if state.get("done") or not state.get("execute_after_plan", True):
            return "finish"
        return "execution"

    @staticmethod
    def _after_parent_execution(state: DynamicGraphState) -> str:
        if state.get("needs_repair"):
            return "repair"
        if state.get("terminal_failure") or state.get("final_decision") == "block_task":
            return "report"
        return "finish"

    @staticmethod
    def _after_parent_repair(state: DynamicGraphState) -> str:
        if not state.get("replay_ready"):
            return "finish" if state.get("final_decision") == "wait_user" else "report"
        return "planning" if state.get("repair_resume_target") == "planning" else "execution"

    def start(
        self,
        *,
        task_id: str,
        user_input: str,
        mode: str,
        require_paperwise: bool,
        capability_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        state: DynamicGraphState = {
            "operation": "start",
            "task_id": task_id,
            "user_input": user_input,
            "mode": mode,
            "require_paperwise": require_paperwise,
            "capability_snapshot": capability_snapshot,
            "capabilities": {name: cap.to_dict() for name, cap in sorted(self.scheduler.registry.capabilities.items())},
            "plan_version": 1,
            "plan_metadata_key": "dynamic_plan",
            "execute_after_plan": True,
            "done": False,
            "needs_repair": False,
            "replay_ready": False,
            "repair_attempts": 0,
        }
        return self._invoke_graph(task_id, state, self._main_config(task_id))

    def replan(
        self,
        *,
        task_id: str,
        user_input: str,
        mode: str,
        require_paperwise: bool,
        capability_snapshot: dict[str, Any],
        plan_version: int = 1,
        reroute_instruction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = self.scheduler.blackboard.get(task_id).task_metadata
        state: DynamicGraphState = {
            "operation": "replan",
            "task_id": task_id,
            "user_input": user_input,
            "mode": mode,
            "require_paperwise": require_paperwise,
            "capability_snapshot": capability_snapshot,
            "plan_version": plan_version,
            "reroute_instruction": reroute_instruction,
            "plan_metadata_key": "dynamic_plan",
            "execute_after_plan": False,
            "capabilities": {name: cap.to_dict() for name, cap in sorted(self.scheduler.registry.capabilities.items())},
            "done": False,
            "needs_repair": False,
            "replay_ready": False,
            "repair_attempts": 0,
            "global_review_records": list(metadata.get("global_review_records") or []),
            "module_review_records": list(metadata.get("module_review_records") or []),
            "subagent_records": list(metadata.get("subagent_records") or []),
            "central_decisions": list(metadata.get("central_decisions") or []),
            "step_skill_contexts": list(metadata.get("step_skill_contexts") or []),
        }
        config = self._phase_config(task_id, f"dynamic-plan-v{plan_version}")
        result, effective_config = self._invoke_parent_state(task_id, state, config)
        self._record_runtime_state(task_id, effective_config, result)
        return {
            "active_plan": dict(result.get("active_plan") or {}),
            "active_review": dict(result.get("active_review") or {}),
            "history": list(result.get("plan_history") or []),
            "final_decision": str(result.get("final_decision") or "complete_task"),
        }

    def rerun_paperwise(
        self,
        *,
        task_id: str,
        user_input: str,
        mode: str,
        capability_snapshot: dict[str, Any],
        plan_version: int,
    ) -> dict[str, Any]:
        state: DynamicGraphState = {
            "operation": "paperwise_rerun",
            "task_id": task_id,
            "user_input": user_input,
            "mode": mode,
            "require_paperwise": True,
            "capability_snapshot": capability_snapshot,
            "capabilities": {name: cap.to_dict() for name, cap in sorted(self.scheduler.registry.capabilities.items())},
            "plan_version": plan_version,
            "plan_metadata_key": "paperwise_rerun_plan",
            "execute_after_plan": True,
            "reroute_instruction": {
                "action": "paperwise_evidence_rerun",
                "allowed_actions": ["evidence_retrieval"],
            },
            "done": False,
            "needs_repair": False,
            "replay_ready": False,
            "repair_attempts": 0,
        }
        return self._invoke_graph(task_id, state, self._phase_config(task_id, f"paperwise-rerun-v{plan_version}"))

    def resume(self, task_id: str, resume_value: dict[str, Any]) -> dict[str, Any]:
        return self._invoke_graph(task_id, Command(resume=resume_value), self._main_config(task_id))

    def _invoke_graph(self, task_id: str, graph_input: Any, config: dict[str, Any]) -> dict[str, Any]:
        result, effective_config = self._invoke_parent_state(task_id, graph_input, config)
        return self._record_runtime_state(task_id, effective_config, result)

    def _invoke_parent_state(
        self,
        task_id: str,
        graph_input: Any,
        config: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        current_input = graph_input
        current_config = config
        max_handoffs = int(self.scheduler.settings.max_auto_fix_rounds) + 2
        for handoff in range(max_handoffs):
            try:
                return self.graph.invoke(current_input, config=current_config), current_config
            except Exception as exc:
                if handoff + 1 >= max_handoffs:
                    raise
                failure_namespace = f"failure-{stable_hash({'task_id': task_id, 'error': str(exc), 'at': now_iso()})[:12]}"
                current_config = self._phase_config(task_id, failure_namespace)
                current_input = self._graph_failure_input(task_id, exc)
        raise RuntimeError("unreachable parent graph failure handoff")

    def _record_runtime_state(self, task_id: str, config: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        interrupts = list(result.get("__interrupt__") or [])
        if interrupts:
            value = interrupts[0].value if hasattr(interrupts[0], "value") else {}
            execution = {
                "final_decision": "wait_user",
                "final_stage": str((value or {}).get("stage") or "waiting_approval"),
                "reason": str((value or {}).get("reason") or "approval_required"),
            }
        else:
            execution = {
                "final_decision": str(result.get("final_decision") or "complete_task"),
                "final_stage": str(result.get("final_stage") or "completed"),
                "reason": str(result.get("reason") or "all dynamic steps passed"),
            }
        thread = {
                "thread_id": config["configurable"]["thread_id"],
                "paused": bool(interrupts),
                "checkpoint_backend": self.checkpoint_backend,
                "checkpoint_durable": self.checkpoint_durable,
                "restart_resume_supported": self.checkpoint_durable,
                "checkpoint_error": self.checkpoint_error,
                "durable_mirror": "blackboard_json_task_snapshot",
                "updated_at": now_iso(),
            }
        thread_key = "langgraph_thread" if thread["thread_id"].endswith(":main") else "last_auxiliary_langgraph_thread"
        self.scheduler._set_v2_metadata(
            task_id,
            orchestration_runtime="langgraph",
            langgraph_final_decision=execution["final_decision"],
            langgraph_final_stage=execution["final_stage"],
            **{thread_key: thread},
        )
        return execution

    def _graph_failure_input(self, task_id: str, error: Exception) -> DynamicGraphState:
        record = self.scheduler.blackboard.get(task_id)
        failed_nodes = [
            (node_id, node)
            for node_id, node in record.nodes.items()
            if isinstance(node, dict) and node.get("status") == "failed"
        ]
        failed_stage = max(
            failed_nodes,
            key=lambda item: str(item[1].get("updated_at") or ""),
            default=("langgraph", {}),
        )[0]
        metadata = dict(record.task_metadata)
        if failed_stage.startswith("skill_router"):
            failure_type = "skill_route"
        elif failed_stage.startswith("central_scheduler"):
            failure_type = "dynamic_plan"
        else:
            failure_type = error.__class__.__name__
        return {
            "operation": "failure",
            "task_id": task_id,
            "user_input": str(metadata.get("user_input") or record.user_input or ""),
            "mode": str(metadata.get("mode") or "mock"),
            "require_paperwise": bool(metadata.get("require_paperwise", False)),
            "capability_snapshot": {},
            "capabilities": {name: cap.to_dict() for name, cap in sorted(self.scheduler.registry.capabilities.items())},
            "plan": dict(metadata.get("dynamic_plan") or {}),
            "plan_steps": [dict(step) for step in (metadata.get("dynamic_plan") or {}).get("steps") or []],
            "plan_version": int((metadata.get("dynamic_plan") or {}).get("plan_version") or 1),
            "plan_metadata_key": "dynamic_plan",
            "global_review_records": list(metadata.get("global_review_records") or []),
            "module_review_records": list(metadata.get("module_review_records") or []),
            "subagent_records": list(metadata.get("subagent_records") or []),
            "central_decisions": list(metadata.get("central_decisions") or []),
            "step_skill_contexts": list(metadata.get("step_skill_contexts") or []),
            "done": True,
            "needs_repair": True,
            "replay_ready": False,
            "current_failure": {
                "error": {"type": failure_type, "exception_type": error.__class__.__name__, "reason": str(error)},
                "step_id": failed_stage,
                "resume_target": "planning" if failed_stage.startswith(("skill_router", "central_scheduler")) else "execution",
                "source": "langgraph_retry_exhausted_or_unhandled",
            },
            "final_decision": "revise_current_step",
            "final_stage": failed_stage,
            "reason": str(error),
        }

    def _main_config(self, task_id: str) -> dict[str, Any]:
        return self._phase_config(task_id, "main")

    def _phase_config(self, task_id: str, namespace: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": f"{task_id}:{namespace}"},
            "recursion_limit": max(100, int(self.scheduler.settings.max_graph_steps) * 8),
        }
