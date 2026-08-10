from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt

from .io_contracts import ExecutionGraphInput, IOContractResolver, PlanningGraphInput, RepairGraphInput
from .llm import LLMGenerationError
from .utils import now_iso, stable_hash


def retry_transient_runtime_error(error: Exception) -> bool:
    """Retry only transient planning/routing infrastructure failures."""
    if isinstance(error, (PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError, NotADirectoryError)):
        return False
    message = str(error).lower()
    if isinstance(error, TimeoutError):
        return "cst" not in message
    if isinstance(error, ConnectionError):
        return True
    if isinstance(error, OSError):
        return error.errno in {
            errno.EAGAIN,
            errno.EBUSY,
            errno.ECONNABORTED,
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.EINTR,
            errno.ENETDOWN,
            errno.ENETUNREACH,
            errno.ETIMEDOUT,
        }
    if "cst" in message or "solver" in message:
        return False
    if any(
        term in message
        for term in (
            "temporarily unavailable",
            "service unavailable",
            "connection reset",
            "connection refused",
            "network unavailable",
            "rate limit",
            "too many requests",
            "error code: 429",
            "error code: 502",
            "error code: 503",
            "error code: 504",
        )
    ):
        return True
    return error.__class__.__name__ in {"APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout"}


class DynamicGraphState(TypedDict, total=False):
    operation: str
    task_id: str
    plan: dict[str, Any]
    plan_metadata_key: str
    plan_steps: list[dict[str, Any]]
    step_index: int
    executed_steps: list[dict[str, Any]]
    global_review_records: list[dict[str, Any]]
    module_review_records: list[dict[str, Any]]
    subagent_records: list[dict[str, Any]]
    central_decisions: list[dict[str, Any]]
    step_skill_contexts: list[dict[str, Any]]
    capabilities: dict[str, Any]
    user_input: str
    require_paperwise: bool
    mode: str
    plan_version: int
    current_step: dict[str, Any]
    step_route_plan: dict[str, Any]
    step_skill_context: dict[str, Any]
    task_result: dict[str, Any]
    local_module_review: dict[str, Any] | None
    module_result: dict[str, Any]
    global_result: dict[str, Any] | None
    central_decision: str
    final_decision: str
    final_stage: str
    reason: str
    done: bool
    capability_snapshot: dict[str, Any]
    reroute_instruction: dict[str, Any] | None
    route_plan: dict[str, Any]
    route_review: dict[str, Any]
    planner_input: dict[str, Any]
    plan_candidate: dict[str, Any]
    active_plan: dict[str, Any]
    active_review: dict[str, Any]
    plan_history: list[dict[str, Any]]
    execute_after_plan: bool
    precondition_error: str
    needs_repair: bool
    current_failure: dict[str, Any]
    failure_record: dict[str, Any]
    repair_decision: dict[str, Any]
    repair_plan: dict[str, Any]
    repair_task_result: dict[str, Any]
    repair_review_result: dict[str, Any]
    repair_global_review_result: dict[str, Any]
    repair_resume_target: str
    replay_ready: bool
    repair_attempts: int
    terminal_failure: bool


class DynamicLangGraphRuntime:
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

    def _initial_skill_route(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        node_id = "skill_router.task_agent"
        agent = "skill_route_task_agent" if state.get("mode") == "real" else node_id
        self.scheduler._set_v2_metadata(task_id, current_stage="skill_router", current_task_subagent=agent)
        self.scheduler.blackboard.update_node(task_id, node_id, "running", agent=agent)
        self.scheduler._event(task_id, node_id, "status", "pending", f"{node_id} running")
        try:
            metadata = self.scheduler.blackboard.get(task_id).task_metadata
            route_repair_context = metadata.get("route_repair_context") or {}
            route_input = state["user_input"]
            if route_repair_context:
                route_input = (
                    f"{route_input}\nRouting review feedback: "
                    f"{route_repair_context.get('review_feedback') or route_repair_context}"
                )
            plan = self.scheduler.skill_router.build_plan(
                route_input,
                mode=state["mode"],
                require_paperwise=state["require_paperwise"],
                capabilities=state["capabilities"],
                task_id=task_id,
            )
        except Exception as exc:
            self.scheduler.blackboard.update_node(task_id, node_id, "failed", error=str(exc), agent=agent)
            raise
        self.scheduler.blackboard.update_node(task_id, node_id, "done", agent=agent, output_summary=str(plan)[:500])
        self.scheduler._event(task_id, node_id, "final", "finalized", f"{node_id} done")
        return {"route_plan": plan}

    def _skill_route_review(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        node_id = "skill_router.review_agent"
        agent = "skill_route_review_agent" if state.get("mode") == "real" else node_id
        self.scheduler._set_v2_metadata(task_id, current_stage="skill_router", current_review_subagent=agent)
        self.scheduler.blackboard.update_node(task_id, node_id, "running", agent=agent)
        self.scheduler._event(task_id, node_id, "status", "pending", f"{node_id} running")
        try:
            review = self.scheduler.skill_router.review_plan(state["route_plan"])
        except Exception as exc:
            self.scheduler.blackboard.update_node(task_id, node_id, "failed", error=str(exc), agent=agent)
            raise
        self.scheduler.blackboard.update_node(task_id, node_id, "done", agent=agent, output_summary=str(review)[:500])
        self.scheduler._event(task_id, node_id, "final", "finalized", f"{node_id} done")
        return {"route_review": review}

    def _persist_skill_route(self, state: DynamicGraphState) -> dict[str, Any]:
        self.scheduler._persist_skill_route_result(
            task_id=state["task_id"],
            mode=state["mode"],
            plan=state["route_plan"],
            review=state["route_review"],
        )
        return {}

    @staticmethod
    def _review_blocks(review: dict[str, Any]) -> bool:
        return str(review.get("decision") or "").lower() in {"block", "blocked"} or str(review.get("status") or "").lower() in {"block", "blocked"}

    def _block_skill_route(self, state: DynamicGraphState) -> dict[str, Any]:
        review = dict(state.get("route_review") or {})
        findings = list(review.get("blocking_findings") or [])
        self.scheduler.audit.emit(state["task_id"], {"type": "skill_route_review_blocked", "findings": findings})
        return {
            "done": True,
            "needs_repair": True,
            "current_failure": {
                "error": {"type": "route", "reason": "skill_route_plan review blocked", "findings": findings},
                "step_id": "planning.skill_route_review",
                "resume_target": "planning",
            },
            "final_decision": "revise_current_step",
            "final_stage": "skill_route_review",
            "reason": "skill_route_review_blocked",
        }

    def _paperwise_gate(self, state: DynamicGraphState) -> dict[str, Any]:
        if state.get("mode") == "real":
            return {"precondition_error": ""}
        metadata = self.scheduler.blackboard.get(state["task_id"]).task_metadata
        error = self.scheduler._paperwise_required_but_unavailable(
            metadata.get("paper_report_path"),
            bool(state.get("require_paperwise")),
        )
        if error and self.scheduler.settings.llm_enabled:
            error = None
        return {"precondition_error": str(error or "")}

    def _block_precondition(self, state: DynamicGraphState) -> dict[str, Any]:
        reason = str(state.get("precondition_error") or "precondition_failed")
        return {
            "done": True,
            "needs_repair": True,
            "current_failure": {
                "error": f"insufficient evidence: {reason}",
                "step_id": "planning.paperwise_gate",
                "resume_target": "planning",
            },
            "final_decision": "revise_current_step",
            "final_stage": "precondition",
            "reason": reason,
        }

    def _external_llm_gate(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        if (
            state.get("operation") == "paperwise_rerun"
            or state.get("mode") == "real"
            or not self.scheduler._external_llm_enabled()
        ):
            return {}
        if self.scheduler._has_approved_external_llm(task_id):
            self.scheduler._external_llm_approved_tasks.add(task_id)
            record = self.scheduler.blackboard.get(task_id)
            blockers = [item for item in record.blockers if item.get("type") != "external_llm_approval_required"]
            self.scheduler.blackboard.update(task_id, state="running", blockers=blockers)
            return {}
        egress = self.scheduler._external_llm_egress_plan("dynamic_plan_task", state["user_input"], "dynamic_planning_and_review")
        approval = {
            "approval_id": f"external_llm:{task_id}",
            "task_id": task_id,
            "status": "waiting_approval",
            "reason": "External LLM data egress requires approval",
            "data_egress": egress,
        }
        record = self.scheduler.blackboard.get(task_id)
        approvals = dict(record.approvals)
        approvals[approval["approval_id"]] = approval
        blockers = [item for item in record.blockers if item.get("type") != "external_llm_approval_required"]
        blockers.append({"type": "external_llm_approval_required", "reason": approval["reason"], "data_egress": egress})
        self.scheduler.blackboard.update(task_id, state="waiting_approval", approvals=approvals, blockers=blockers)
        self.scheduler.audit.emit(task_id, {"type": "data_egress_requested", "data_egress": egress})
        interrupt({"stage": "external_llm_approval", "reason": approval["reason"], "approval_id": approval["approval_id"]})
        return {}

    def _plan_generate(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        metadata = dict(self.scheduler.blackboard.get(task_id).task_metadata)
        planner_input = self.scheduler._dynamic_planner_input(
            task_id=task_id,
            user_input=state["user_input"],
            mode=state["mode"],
            require_paperwise=state["require_paperwise"],
            capability_snapshot=state.get("capability_snapshot") or {},
            route_plan=metadata.get("skill_route_plan") or {},
            route_review=metadata.get("skill_route_review") or {},
            metadata=metadata,
            plan_version=int(state.get("plan_version") or 1),
            reroute_instruction=state.get("reroute_instruction"),
        )
        node_id = "central_scheduler.llm_plan_generation"
        self.scheduler._set_v2_metadata(task_id, current_stage="central_scheduler", current_task_subagent=node_id)
        self.scheduler.blackboard.update_node(task_id, node_id, "running", agent=node_id)
        self.scheduler._event(task_id, node_id, "status", "pending", f"{node_id} running")
        try:
            if state.get("operation") == "paperwise_rerun":
                candidate = {
                    "source": "scheduler_fallback",
                    "rerun_kind": "paperwise_evidence_rerun",
                    "steps": [
                        {
                            "step_goal": "Refresh traceable PaperWise evidence and review relevance",
                            "action": "evidence_retrieval",
                            "inputs": ["user_goal", "paperwise_read_only_sources"],
                            "outputs": ["evidence_pool"],
                            "required_skills": ["paperwise"],
                            "gate_condition": "PaperWise remains read-only",
                        }
                    ]
                }
            else:
                candidate = self.scheduler._generate_dynamic_plan_candidate(planner_input)
        except Exception as exc:
            attempts = dict(metadata.get("planner_generation_attempts") or {})
            version_key = str(int(state.get("plan_version") or 1))
            attempt = int(attempts.get(version_key) or 0) + 1
            attempts[version_key] = attempt
            self.scheduler._set_v2_metadata(task_id, planner_generation_attempts=attempts)
            transient = retry_transient_runtime_error(exc)
            if transient and attempt < max(1, int(self.scheduler.settings.max_node_retries)):
                self.scheduler.blackboard.update_node(task_id, node_id, "failed", error=str(exc), agent=node_id)
                raise
            if transient or isinstance(exc, LLMGenerationError):
                candidate = self.scheduler._fallback_dynamic_plan_candidate(planner_input, reason=str(exc))
                self.scheduler.audit.emit(
                    task_id,
                    {
                        "type": "central_plan_fallback_selected",
                        "attempts": attempt,
                        "reason": str(exc),
                        "plan_version": int(state.get("plan_version") or 1),
                    },
                )
            else:
                candidate = self.scheduler._failed_dynamic_plan(
                    task_id,
                    str(state.get("mode") or "mock"),
                    int(state.get("plan_version") or 1),
                    state["user_input"],
                    str(exc),
                )
                self.scheduler.audit.emit(
                    task_id,
                    {
                        "type": "central_plan_generation_failed",
                        "attempts": attempt,
                        "reason": str(exc),
                        "plan_version": int(state.get("plan_version") or 1),
                    },
                )
        self.scheduler.blackboard.update_node(task_id, node_id, "done", agent=node_id, output_summary=str(candidate)[:500])
        self.scheduler._event(task_id, node_id, "final", "finalized", f"{node_id} done")
        return {"planner_input": planner_input, "plan_candidate": candidate}

    def _plan_normalize(self, state: DynamicGraphState) -> dict[str, Any]:
        plan = self.scheduler._normalize_dynamic_plan_candidate(
            state["plan_candidate"],
            task_id=state["task_id"],
            mode=state["mode"],
            plan_version=int(state.get("plan_version") or 1),
            user_input=state["user_input"],
        )
        return {"active_plan": plan}

    def _plan_validate(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        node_id = "central_scheduler.plan_validation"
        self.scheduler._set_v2_metadata(task_id, current_stage="central_scheduler", current_review_subagent=node_id)
        self.scheduler.blackboard.update_node(task_id, node_id, "running", agent=node_id)
        self.scheduler._event(task_id, node_id, "status", "pending", f"{node_id} running")
        try:
            review = self.scheduler._validate_dynamic_plan(state["active_plan"])
        except Exception as exc:
            self.scheduler.blackboard.update_node(task_id, node_id, "failed", error=str(exc), agent=node_id)
            raise
        self.scheduler.blackboard.update_node(task_id, node_id, "done", agent=node_id, output_summary=str(review)[:500])
        self.scheduler._event(task_id, node_id, "final", "finalized", f"{node_id} done")
        return {"active_review": review}

    def _persist_plan(self, state: DynamicGraphState) -> dict[str, Any]:
        result = self.scheduler._persist_dynamic_plan_result(
            task_id=state["task_id"],
            mode=state["mode"],
            plan=state["active_plan"],
            review=state["active_review"],
            plan_version=int(state.get("plan_version") or 1),
            reroute_instruction=state.get("reroute_instruction"),
            generation_error="",
            plan_metadata_key=str(state.get("plan_metadata_key") or "dynamic_plan"),
        )
        return {
            "active_plan": result["active_plan"],
            "active_review": result["active_review"],
            "plan_history": result["history"],
        }

    def _after_plan(self, state: DynamicGraphState) -> str:
        if self._review_blocks(state.get("active_review") or {}):
            return "block"
        return "execute" if state.get("execute_after_plan", True) else "finish"

    def _block_plan(self, state: DynamicGraphState) -> dict[str, Any]:
        review = dict(state.get("active_review") or {})
        findings = list(review.get("blocking_findings") or [])
        return {
            "done": True,
            "needs_repair": True,
            "current_failure": {
                "error": {"type": "plan", "reason": "dynamic_plan validation failed", "findings": findings},
                "step_id": "planning.plan_validate",
                "resume_target": "planning",
            },
            "final_decision": "revise_current_step",
            "final_stage": "plan_validation",
            "reason": "dynamic_plan_review_failed",
        }

    def _prepare_execution(self, state: DynamicGraphState) -> dict[str, Any]:
        plan = dict(state.get("active_plan") or {})
        metadata = self.scheduler.blackboard.get(state["task_id"]).task_metadata
        plan_steps = [dict(step) for step in plan.get("steps") or []]
        return {
            "plan": plan,
            "plan_metadata_key": str(state.get("plan_metadata_key") or "dynamic_plan"),
            "plan_steps": plan_steps,
            "step_index": 0,
            "executed_steps": [dict(step) for step in plan_steps if step.get("status") == "passed"],
            "global_review_records": list(metadata.get("global_review_records") or []),
            "module_review_records": list(metadata.get("module_review_records") or []),
            "subagent_records": list(metadata.get("subagent_records") or []),
            "central_decisions": list(metadata.get("central_decisions") or []),
            "step_skill_contexts": list(metadata.get("step_skill_contexts") or []),
            "plan_version": int(plan.get("plan_version") or state.get("plan_version") or 1),
            "done": False,
            "needs_repair": False,
            "replay_ready": False,
        }

    def _select_step(self, state: DynamicGraphState) -> dict[str, Any]:
        steps = list(state.get("plan_steps") or [])
        index = int(state.get("step_index") or 0)
        while index < len(steps) and steps[index].get("status") == "passed":
            index += 1
        if index >= len(steps):
            return {
                "step_index": index,
                "done": True,
                "final_decision": "complete_task",
                "final_stage": "completed",
                "reason": "all dynamic steps passed",
            }
        return {
            "step_index": index,
            "current_step": dict(steps[index]),
            "step_route_plan": {},
            "step_skill_context": {},
            "task_result": {},
            "local_module_review": None,
            "module_result": {},
            "global_result": None,
            "central_decision": "",
            "done": False,
            "needs_repair": False,
            "replay_ready": False,
        }

    def _route_skill(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = dict(state["current_step"])
        module_records = list(state.get("module_review_records") or [])
        global_records = list(state.get("global_review_records") or [])
        context = self.scheduler.skill_router.build_step_context(
            task_id=task_id,
            user_input=state["user_input"],
            step=step,
            mode=state["mode"],
            plan_version=state["plan_version"],
            require_paperwise=state["require_paperwise"],
            capabilities=state["capabilities"],
            context=self.scheduler._step_route_context(task_id, step, module_records, global_records),
        )
        route_plan = context.get("route_plan") or {}
        route_history = list(self.scheduler.blackboard.get(task_id).task_metadata.get("skill_route_plan_history") or [])
        route_history.append(
            {
                "plan": route_plan,
                "review": {"decision": "pass", "review_agent": "central_agent", "reason": "step_route_context_generated"},
                "reason": context.get("route_reason") or "step_route",
                "step_id": step.get("step_id"),
                "dynamic_plan_version": state["plan_version"],
                "created_at": now_iso(),
            }
        )
        step["step_skill_context"] = context
        step["callable_skills"] = context.get("callable_skills", [])
        step["candidate_skills"] = context.get("candidate_skills", [])
        step["blocked_skills"] = context.get("excluded_skills", [])
        contexts = [*list(state.get("step_skill_contexts") or []), context]
        self.scheduler._set_v2_metadata(
            task_id,
            current_stage=step.get("step_id"),
            current_task_subagent=step.get("task_agent"),
            current_review_subagent=step.get("module_review_agent") or step.get("review_agent"),
            step_skill_contexts=contexts,
            active_skill_route_plan=route_plan,
            skill_route_plan_history=route_history,
        )
        return {
            "current_step": step,
            "step_route_plan": route_plan,
            "step_skill_context": context,
            "step_skill_contexts": contexts,
        }

    def _task_agent(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = dict(state["current_step"])
        execution_context: dict[str, Any] = {}

        def execute_step() -> dict[str, Any]:
            output = self.scheduler._execute_dynamic_step_handler(
                task_id=task_id,
                step=step,
                route_plan=state["step_route_plan"],
                plan_version=state["plan_version"],
                user_input=state["user_input"],
            )
            execution_context["module_review"] = output.pop("_module_review", None)
            return output

        task_agent = str(step.get("task_agent") or "dynamic_step_task_agent")
        task_node = f"{step.get('step_id')}.{task_agent}"
        task_context = self.io.build_input(
            "agent.task",
            dict(state),
            overlays={"current_step": step},
        )
        self.scheduler.blackboard.update_node(task_id, task_node, "running", agent=task_agent)
        try:
            result = self.scheduler.subagents.run_task(
                task_id=task_id,
                step=step,
                context=task_context,
                allow_external_llm=self.scheduler._step_external_llm_allowed(task_id, step),
                executor=execute_step,
            )
        except Exception as exc:
            self.scheduler.blackboard.update_node(task_id, task_node, "failed", agent=task_agent, error=str(exc))
            if retry_transient_runtime_error(exc):
                raise
            result = self.scheduler.subagents.run_task(
                task_id=task_id,
                step=step,
                context=task_context,
                allow_external_llm=False,
                local_output={
                    "status": "failed",
                    "execution_failure": {
                        "error_type": exc.__class__.__name__,
                        "reason": str(exc),
                    },
                    "missing_required_input": f"{exc.__class__.__name__}: {exc}",
                    "evidence_refs": [],
                },
            )
        self.scheduler.blackboard.update_node(task_id, task_node, "done", agent=task_agent, output_summary=str(result)[:500])
        self.scheduler.audit.emit(task_id, {"type": "dynamic_step_task_result", "agent": task_agent, "step_id": step.get("step_id"), "result": result})
        return {
            "task_result": result,
            "local_module_review": execution_context.get("module_review"),
            "subagent_records": [*list(state.get("subagent_records") or []), result],
        }

    def _module_review(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = state["current_step"]
        agent = str(step.get("module_review_agent") or step.get("review_agent") or "module_review_agent")
        node = f"{step.get('step_id')}.{agent}"
        module_context = self.io.build_input(
            "agent.module_review",
            dict(state),
            overlays={
                "current_step": step,
                "real_cst_approved": self.scheduler._real_cst_approval_valid(task_id),
            },
        )
        self.scheduler.blackboard.update_node(task_id, node, "running", agent=agent)
        result = self.scheduler.subagents.run_module_review(
            task_id=task_id,
            step=step,
            task_result=state["task_result"],
            context=module_context,
            allow_external_llm=self.scheduler._step_external_llm_allowed(task_id, step),
            local_review=state.get("local_module_review"),
        )
        status = "done" if result.get("decision") == "pass" else result.get("decision", "reviewed")
        self.scheduler.blackboard.update_node(task_id, node, status, agent=agent, output_summary=str(result)[:500])
        self.scheduler.audit.emit(task_id, {"type": "dynamic_step_module_review_result", "agent": agent, "step_id": step.get("step_id"), "result": result})
        return {
            "module_result": result,
            "module_review_records": [*list(state.get("module_review_records") or []), result],
            "subagent_records": [*list(state.get("subagent_records") or []), result],
        }

    def _global_review(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = state["current_step"]
        node = f"{step.get('step_id')}.global_review_agent"
        global_context = self.io.build_input(
            "agent.global_review",
            dict(state),
            overlays={"current_step": step},
        )
        self.scheduler._set_v2_metadata(task_id, current_review_subagent="global_review_agent")
        self.scheduler.blackboard.update_node(task_id, node, "running", agent="global_review_agent")
        result = self.scheduler.subagents.run_global_review(
            task_id=task_id,
            step=step,
            task_result=state["task_result"],
            module_review=state["module_result"].get("output") or state["module_result"],
            history=state.get("executed_steps") or [],
            context=global_context,
            allow_external_llm=self.scheduler._step_external_llm_allowed(task_id, step),
        )
        status = "done" if result.get("decision") == "pass" else result.get("decision", "reviewed")
        self.scheduler.blackboard.update_node(task_id, node, status, agent="global_review_agent", output_summary=str(result)[:500])
        self.scheduler.audit.emit(task_id, {"type": "dynamic_step_global_review_result", "agent": "global_review_agent", "step_id": step.get("step_id"), "result": result})
        return {
            "global_result": result,
            "global_review_records": [*list(state.get("global_review_records") or []), result],
            "subagent_records": [*list(state.get("subagent_records") or []), result],
        }

    def _central_decision(self, state: DynamicGraphState) -> dict[str, Any]:
        decision = self.scheduler._central_step_decision(state["module_result"], state.get("global_result"))
        global_result = state.get("global_result") or {}
        record = {
            "schema_version": "1.0",
            "step_id": state["current_step"].get("step_id"),
            "decision": decision,
            "module_review_decision": state["module_result"].get("decision"),
            "global_review_decision": global_result.get("decision") or None,
            "plan_version": state["plan_version"],
            "skill_route_plan_id": state["step_route_plan"].get("plan_id"),
            "step_skill_context_id": state["step_skill_context"].get("route_plan_id"),
            "created_at": now_iso(),
        }
        self.scheduler.audit.emit(state["task_id"], {"type": "central_dynamic_step_decision", **record})
        return {
            "central_decision": decision,
            "central_decisions": [*list(state.get("central_decisions") or []), record],
        }

    def _commit_step(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = dict(state["current_step"])
        global_result = state.get("global_result") or {}
        global_output = global_result.get("output") or {}
        if global_output.get("reflection"):
            reflections = list(self.scheduler.blackboard.get(task_id).task_metadata.get("reflection_records") or [])
            reflections.append(global_output["reflection"])
            self.scheduler._set_v2_metadata(task_id, reflection_records=reflections)

        step["task_subagent_result_id"] = state["task_result"].get("result_id")
        step["module_review_result_id"] = state["module_result"].get("result_id")
        if global_result:
            step["global_review_result_id"] = global_result.get("result_id")
        decision = state["central_decision"]
        terminal_failure = False
        terminal_reason = ""
        if decision == "wait_user":
            paused_step = dict(step)
            paused_step["central_decision"] = "wait_user"
            paused_step["status"] = "wait_user"
            paused_steps = list(state["plan_steps"])
            paused_steps[int(state["step_index"])] = paused_step
            paused_plan = dict(state["plan"])
            paused_plan["steps"] = paused_steps
            self.scheduler._set_v2_metadata(
                task_id,
                **{state["plan_metadata_key"]: paused_plan},
                current_stage=str(step.get("step_id") or "waiting_approval"),
            )
            resumed = interrupt(
                {
                    "stage": str(step.get("step_id") or "waiting_approval"),
                    "reason": "central_decision:wait_user",
                    "step_id": step.get("step_id"),
                }
            )
            resume_payload = dict(resumed) if isinstance(resumed, dict) else {"approved": bool(resumed)}
            approved = bool(resume_payload.get("approved"))
            review_agent = str(step.get("module_review_agent") or step.get("review_agent") or "module_review_agent")
            approval_valid = approved and (state.get("mode") != "real" or self.scheduler._real_cst_approval_valid(task_id))
            self.scheduler.blackboard.update_node(
                task_id,
                f"{step.get('step_id')}.{review_agent}",
                "done" if approval_valid else "block",
                agent=review_agent,
                output_summary="approval resumed from LangGraph checkpoint" if approval_valid else "approval rejected or stale",
            )
            decision = "pass_next_step" if approval_valid else "block_task"
            if not approved:
                terminal_failure = True
                terminal_reason = str(resume_payload.get("reason") or "user_rejected_approval")
        step["central_decision"] = decision
        step["status"] = "passed" if decision == "pass_next_step" else decision

        steps = list(state["plan_steps"])
        index = int(state["step_index"])
        steps[index] = step
        executed = list(state.get("executed_steps") or [])
        if decision == "pass_next_step":
            executed.append(step)
        plan = dict(state["plan"])
        plan["steps"] = steps

        updates: dict[str, Any] = {
            "current_step": step,
            "plan_steps": steps,
            "plan": plan,
            "executed_steps": executed,
        }
        if decision in {"refresh_skill_route", "block_task", "reroute_plan", "revise_current_step"}:
            reason = terminal_reason or self._step_failure_reason(step, decision)
            if terminal_failure:
                updates.update(
                    {
                        "done": True,
                        "needs_repair": False,
                        "terminal_failure": True,
                        "replay_ready": False,
                        "final_decision": "block_task",
                        "final_stage": str(step.get("step_id") or "approval_rejected"),
                        "reason": reason,
                    }
                )
                self.scheduler._set_v2_metadata(task_id, failure_reason=reason, cst_status="not_reached")
            else:
                task_output = dict(state.get("task_result", {}).get("output") or {})
                module_output = dict(state.get("module_result", {}).get("output") or {})
                if decision == "refresh_skill_route":
                    failure_error: Any = "missing_skill in skill_route_plan; refresh route required"
                elif decision == "reroute_plan":
                    failure_error = "invalid dynamic_plan; plan regeneration required"
                else:
                    failure_error = (
                        task_output.get("repairable_failure")
                        or task_output.get("failure")
                        or task_output.get("execution_failure")
                        or task_output.get("missing_required_input")
                        or module_output.get("blocking_findings")
                        or module_output.get("required_fixes")
                        or reason
                    )
                updates.update(
                    {
                        "done": True,
                        "needs_repair": True,
                        "terminal_failure": False,
                        "replay_ready": False,
                        "current_failure": {
                            "error": failure_error,
                            "step_id": str(step.get("step_id") or "execution.unknown"),
                            "resume_target": "execution",
                            "central_decision": decision,
                        },
                        "final_decision": "revise_current_step",
                        "final_stage": str(step.get("step_id") or ""),
                        "reason": reason,
                    }
                )
        else:
            updates.update({"step_index": index + 1, "done": False, "needs_repair": False})

        self.scheduler._set_v2_metadata(
            task_id,
            **{state["plan_metadata_key"]: plan},
            global_review_records=state.get("global_review_records") or [],
            module_review_records=state.get("module_review_records") or [],
            subagent_records=state.get("subagent_records") or [],
            central_decisions=state.get("central_decisions") or [],
            step_skill_contexts=updates.get("step_skill_contexts", state.get("step_skill_contexts") or []),
        )
        return updates

    @staticmethod
    def _step_failure_reason(step: dict[str, Any], decision: str) -> str:
        return {
            "step_000_evidence": "paperwise_evidence_review_failed",
            "step_001_preflight": "preflight_failed",
            "step_003_cst_run": "cst_run_review_failed",
            "step_004_parse": "result_parse_review_failed",
            "step_005_report": "report_review_failed",
        }.get(str(step.get("step_id") or ""), f"central_decision:{decision}")

    def _after_execution_commit(self, state: DynamicGraphState) -> str:
        if state.get("needs_repair"):
            return "finish"
        if not state.get("done"):
            return "select_step"
        return "finish"

    def _failure_triage(self, state: DynamicGraphState) -> dict[str, Any]:
        failure = dict(state.get("current_failure") or {})
        task_id = state["task_id"]
        step_id = str(failure.get("step_id") or state.get("final_stage") or "unknown")
        error = failure.get("error") or state.get("reason") or "unknown graph failure"
        record = self.scheduler.failure_store.create(
            task_id=task_id,
            step_id=step_id,
            error=error,
            context={
                "central_decision": failure.get("central_decision"),
                "resume_target": failure.get("resume_target"),
            },
        )
        if record.get("status") == "repaired":
            record = self.scheduler.failure_store.reopen_after_replay_failure(
                record["failure_id"], reason=str(error)
            )
        decision = self.scheduler.failure_store.policy.decide(
            self.scheduler.failure_store.classifier.classify(error, step_id=step_id),
            repair_rounds=int(record.get("repair_rounds") or 0),
        )
        if record.get("status") == "exhausted" or decision.exhausted:
            decision = self.scheduler.failure_store.policy.decide(
                record["category"], repair_rounds=self.scheduler.failure_store.max_repair_rounds
            )
        self.scheduler._set_v2_metadata(
            task_id,
            current_stage="failure_triage",
            active_failure=record,
            repair_decision=decision.to_dict(),
        )
        self.scheduler.audit.emit(
            task_id,
            {
                "type": "failure_triaged",
                "failure_id": record["failure_id"],
                "category": record["category"],
                "repairability": decision.repairability,
                "action": decision.action,
            },
        )
        return {
            "failure_record": record,
            "repair_decision": decision.to_dict(),
            "repair_resume_target": str(failure.get("resume_target") or "execution"),
            "replay_ready": False,
        }

    @staticmethod
    def _after_failure_triage(state: DynamicGraphState) -> str:
        decision = state.get("repair_decision") or {}
        return "repair" if decision.get("repairability") == "automatic" else "terminal"

    def _repair_plan(self, state: DynamicGraphState) -> dict[str, Any]:
        failure = dict(state["failure_record"])
        decision = dict(state["repair_decision"])
        task_id = state["task_id"]
        try:
            started = self.scheduler.failure_store.begin_repair(
                failure["failure_id"], action=str(decision["action"])
            )
        except Exception as exc:
            blocked = self.scheduler.failure_store.get(failure["failure_id"])
            return {
                "failure_record": blocked,
                "repair_plan": {},
                "repair_task_result": {"changed": False, "reason": str(exc)},
                "repair_review_result": {"decision": "block", "reason": str(exc)},
                "replay_ready": False,
            }
        repair_plan = {
            "schema_version": "1.0",
            "repair_id": f"{failure['failure_id']}:round-{started['repair_rounds']}",
            "failure_id": failure["failure_id"],
            "task_id": task_id,
            "failed_step_id": failure["step_id"],
            "category": failure["category"],
            "action": decision["action"],
            "round": started["repair_rounds"],
            "max_rounds": started["max_repair_rounds"],
            "required_change": "route, plan, input, or artifact fingerprint must change before replay",
            "task_agent": "repair_task_agent",
            "module_review_agent": "repair_review_agent",
            "global_review_required": failure["category"] in {"route", "plan", "evidence_gap", "modeling_parameter"},
            "created_at": now_iso(),
        }
        self.scheduler._set_v2_metadata(
            task_id,
            current_stage="repair_plan",
            current_task_subagent="repair_plan_task_agent",
            active_failure=started,
            active_repair_plan=repair_plan,
        )
        return {
            "failure_record": started,
            "repair_plan": repair_plan,
            "repair_attempts": int(state.get("repair_attempts") or 0) + 1,
        }

    def _repair_task(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        plan = dict(state.get("repair_plan") or {})
        if not plan:
            return {"repair_task_result": {"changed": False, "reason": "repair plan unavailable"}}
        repair_step = {
            "step_id": f"repair_{plan['failed_step_id']}_{plan['round']}",
            "step_goal": f"Apply {plan['action']} and produce verifiable change references",
            "task_agent": "repair_task_agent",
            "module_review_agent": "repair_review_agent",
            "inputs": [plan["failure_id"]],
            "outputs": ["repair_change_manifest"],
            "required_skills": [],
        }
        node = f"{repair_step['step_id']}.repair_task_agent"
        self.scheduler._set_v2_metadata(task_id, current_stage="repair_task", current_task_subagent="repair_task_agent")
        self.scheduler.blackboard.update_node(task_id, node, "running", agent="repair_task_agent")

        def execute_repair() -> dict[str, Any]:
            return self.scheduler._execute_repair_action(
                task_id=task_id,
                action=str(plan["action"]),
                failure=dict(state["failure_record"]),
                failed_step=dict(state.get("current_step") or {}),
                plan_version=int(state.get("plan_version") or 1),
            )

        repair_context = self.io.build_input("agent.repair_task", dict(state))
        result = self.scheduler.subagents.run_task(
            task_id=task_id,
            step=repair_step,
            context=repair_context,
            allow_external_llm=False,
            executor=execute_repair,
        )
        self.scheduler.blackboard.update_node(task_id, node, "done", agent="repair_task_agent", output_summary=str(result)[:500])
        records = [*list(state.get("subagent_records") or []), result]
        self.scheduler.audit.emit(task_id, {"type": "repair_task_result", "repair_plan": plan, "result": result})
        return {"repair_task_result": result, "subagent_records": records}

    @staticmethod
    def _validate_repair_change(output: dict[str, Any]) -> tuple[bool, str]:
        refs = [Path(str(item)) for item in output.get("change_refs") or [] if str(item).strip()]
        manifest_path = Path(str(output.get("repair_manifest") or ""))
        if not output.get("changed") or not refs or not str(output.get("repair_manifest") or ""):
            return False, "repair did not declare a changed manifest and change references"
        if any(not path.exists() for path in refs) or not manifest_path.is_file():
            return False, "repair references must exist before replay"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"repair manifest is unreadable: {exc}"
        before_hash = str(output.get("before_hash") or "")
        after_hash = str(output.get("after_hash") or "")
        if (
            manifest.get("schema_version") != "1.0"
            or not manifest.get("changed")
            or not before_hash
            or not after_hash
            or before_hash == after_hash
            or manifest.get("before_hash") != before_hash
            or manifest.get("after_hash") != after_hash
        ):
            return False, "repair manifest does not prove a before/after state change"
        return True, "verified repair manifest and persisted change references"

    def _repair_review(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        plan = dict(state.get("repair_plan") or {})
        task_result = dict(state.get("repair_task_result") or {})
        output = dict(task_result.get("output") or {})
        changed, validation_reason = self._validate_repair_change(output)
        local_review = {
            "schema_version": "1.0",
            "decision": "pass" if changed else "revise",
            "status": "pass" if changed else "revise",
            "evidence_level": "B" if changed else "D",
            "blocking_findings": [] if changed else [{"type": "repair_no_change", "reason": validation_reason}],
            "required_fixes": [] if changed else ["produce a changed route/plan/input/artifact fingerprint"],
            "reflection": {
                "status": "no_issue" if changed else "blocked",
                "error_type": "none" if changed else "repair_no_change",
                "confidence": 0.99,
            },
        }
        repair_step = {
            "step_id": f"repair_{plan.get('failed_step_id')}_{plan.get('round')}",
            "step_goal": "Verify the repair changed executable state before replay",
            "module_review_agent": "repair_review_agent",
            "inputs": [plan.get("failure_id")],
            "outputs": ["repair_review"],
        }
        repair_review_context = self.io.build_input("agent.repair_review", dict(state))
        review = self.scheduler.subagents.run_module_review(
            task_id=task_id,
            step=repair_step,
            task_result=task_result,
            context=repair_review_context,
            allow_external_llm=False,
            local_review=local_review,
        )
        if review.get("decision") == "pass":
            stored = self.scheduler.failure_store.complete_repair(
                plan["failure_id"],
                changed=True,
                change_refs=[str(item) for item in output.get("change_refs") or []],
                review=review,
            )
        else:
            stored = self.scheduler.failure_store.fail_repair(
                plan["failure_id"], reason=str(local_review["blocking_findings"])
            )
        records = [*list(state.get("subagent_records") or []), review]
        module_records = [*list(state.get("module_review_records") or []), review]
        self.scheduler._set_v2_metadata(
            task_id,
            current_stage="repair_review",
            current_review_subagent="repair_review_agent",
            active_failure=stored,
        )
        return {
            "failure_record": stored,
            "repair_review_result": review,
            "subagent_records": records,
            "module_review_records": module_records,
        }

    @staticmethod
    def _after_repair_review(state: DynamicGraphState) -> str:
        review = state.get("repair_review_result") or {}
        record = state.get("failure_record") or {}
        if review.get("decision") == "pass":
            return "global_review" if (state.get("repair_plan") or {}).get("global_review_required") else "replay"
        return "retry_repair" if record.get("status") == "open" else "terminal"

    def _repair_global_review(self, state: DynamicGraphState) -> dict[str, Any]:
        plan = dict(state.get("repair_plan") or {})
        step = {
            "step_id": f"repair_global_{plan.get('failed_step_id')}_{plan.get('round')}",
            "step_goal": "Check repaired input against the task evidence and downstream dependencies",
            "required_skills": [],
            "callable_skills": [],
            "candidate_skills": [],
        }
        repair_global_context = self.io.build_input(
            "agent.global_review",
            dict(state),
            overlays={
                "current_step": step,
                "task_result": state.get("repair_task_result") or {},
                "module_result": state.get("repair_review_result") or {},
            },
        )
        result = self.scheduler.subagents.run_global_review(
            task_id=state["task_id"],
            step=step,
            task_result=state.get("repair_task_result") or {},
            module_review=(state.get("repair_review_result") or {}).get("output") or state.get("repair_review_result") or {},
            history=state.get("executed_steps") or [],
            context={**repair_global_context, "repair_plan": plan},
            allow_external_llm=False,
        )
        stored = dict(state.get("failure_record") or {})
        if result.get("decision") != "pass" and stored.get("status") == "repaired":
            stored = self.scheduler.failure_store.reopen_after_replay_failure(
                stored["failure_id"], reason="global repair review rejected the change"
            )
        records = [*list(state.get("subagent_records") or []), result]
        global_records = [*list(state.get("global_review_records") or []), result]
        return {
            "repair_global_review_result": result,
            "failure_record": stored,
            "subagent_records": records,
            "global_review_records": global_records,
        }

    @staticmethod
    def _after_repair_global_review(state: DynamicGraphState) -> str:
        result = state.get("repair_global_review_result") or {}
        if result.get("decision") == "pass":
            return "replay"
        return "retry_repair" if (state.get("failure_record") or {}).get("status") == "open" else "terminal"

    def _replay_failed_step(self, state: DynamicGraphState) -> dict[str, Any]:
        target = str(state.get("repair_resume_target") or "execution")
        updates: dict[str, Any] = {
            "needs_repair": False,
            "replay_ready": True,
            "done": False,
            "final_decision": "pass_next_step",
            "reason": "repair reviewed; replay failed step only",
        }
        if target == "planning":
            new_version = int(state.get("plan_version") or 1) + 1
            updates.update(
                {
                    "operation": "replan",
                    "plan_version": new_version,
                    "execute_after_plan": True,
                    "reroute_instruction": {
                        "reason": "automatic_repair",
                        "repair_plan": state.get("repair_plan") or {},
                    },
                }
            )
        else:
            index = int(state.get("step_index") or 0)
            steps = [dict(item) for item in state.get("plan_steps") or []]
            if 0 <= index < len(steps):
                steps[index]["status"] = "pending"
                steps[index].pop("central_decision", None)
            updates.update(
                {
                    "operation": "execute",
                    "plan_steps": steps,
                    "current_step": dict(steps[index]) if 0 <= index < len(steps) else {},
                    "task_result": {},
                    "module_result": {},
                    "global_result": None,
                    "central_decision": "",
                }
            )
        self.scheduler.audit.emit(
            state["task_id"],
            {
                "type": "failed_step_replay_scheduled",
                "failure_id": (state.get("failure_record") or {}).get("failure_id"),
                "resume_target": target,
                "step_index": state.get("step_index"),
            },
        )
        return updates

    def _repair_terminal(self, state: DynamicGraphState) -> dict[str, Any]:
        record = dict(state.get("failure_record") or {})
        decision = dict(state.get("repair_decision") or {})
        wait_user = record.get("status") == "waiting_user" or decision.get("repairability") == "approval_required"
        final_decision = "wait_user" if wait_user else "block_task"
        reason = str(decision.get("reason") or record.get("status") or "repair stopped")
        if wait_user and record.get("failure_id"):
            record = self.scheduler.failure_store.mark_waiting_user(record["failure_id"], reason=reason)
        elif record.get("failure_id") and record.get("status") not in {"blocked", "exhausted"}:
            record = self.scheduler.failure_store.block(record["failure_id"], reason=reason)
        blackboard = self.scheduler.blackboard.get(state["task_id"])
        blockers = [*blackboard.blockers]
        blockers.append(
            {
                "type": "repair_waiting_user" if wait_user else "repair_blocked",
                "reason": reason,
                "failure_id": record.get("failure_id"),
                "category": record.get("category"),
            }
        )
        self.scheduler.blackboard.update(state["task_id"], blockers=blockers)
        self.scheduler._set_v2_metadata(
            state["task_id"], active_failure=record, current_stage="waiting_approval" if wait_user else "failed"
        )
        return {
            "failure_record": record,
            "needs_repair": False,
            "replay_ready": False,
            "done": True,
            "final_decision": final_decision,
            "final_stage": str(record.get("step_id") or state.get("final_stage") or "failure"),
            "reason": reason,
        }

    def _terminal_report(self, state: DynamicGraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        step = {
            "step_id": "terminal_report",
            "step_goal": "Generate a traceable failed report for the stopped task",
            "task_agent": "report_task_agent",
            "handler": "report_task_agent",
            "action": "report",
            "module_review_agent": "report_review_agent",
            "global_review_required": True,
            "inputs": ["failure_reason", "available_artifacts"],
            "outputs": ["failed_report"],
            "required_skills": [],
            "status": "running",
        }
        task_node = "terminal_report.report_task_agent"
        review_node = "terminal_report.report_review_agent"
        global_node = "terminal_report.global_review_agent"
        execution_context: dict[str, Any] = {}

        def execute_report() -> dict[str, Any]:
            output = self.scheduler._execute_dynamic_step_handler(
                task_id=task_id,
                step=step,
                route_plan={},
                plan_version=int(state.get("plan_version") or 1),
                user_input=state["user_input"],
            )
            execution_context["module_review"] = output.pop("_module_review", None)
            return output

        self.scheduler.blackboard.update_node(task_id, task_node, "running", agent="report_task_agent")
        report_task_context = self.io.build_input(
            "agent.task",
            dict(state),
            overlays={"current_step": step},
        )
        task_result = self.scheduler.subagents.run_task(
            task_id=task_id,
            step=step,
            context=report_task_context,
            allow_external_llm=False,
            executor=execute_report,
        )
        self.scheduler.blackboard.update_node(task_id, task_node, "done", agent="report_task_agent", output_summary=str(task_result)[:500])
        self.scheduler.blackboard.update_node(task_id, review_node, "running", agent="report_review_agent")
        report_module_context = self.io.build_input(
            "agent.module_review",
            dict(state),
            overlays={
                "current_step": step,
                "task_result": task_result,
                "real_cst_approved": False,
            },
        )
        module_result = self.scheduler.subagents.run_module_review(
            task_id=task_id,
            step=step,
            task_result=task_result,
            context=report_module_context,
            allow_external_llm=False,
            local_review=execution_context.get("module_review"),
        )
        self.scheduler.blackboard.update_node(task_id, review_node, "done", agent="report_review_agent", output_summary=str(module_result)[:500])
        self.scheduler.blackboard.update_node(task_id, global_node, "running", agent="global_review_agent")
        report_global_context = self.io.build_input(
            "agent.global_review",
            dict(state),
            overlays={
                "current_step": step,
                "task_result": task_result,
                "module_result": module_result,
            },
        )
        global_result = self.scheduler.subagents.run_global_review(
            task_id=task_id,
            step=step,
            task_result=task_result,
            module_review=module_result.get("output") or module_result,
            history=state.get("executed_steps") or [],
            context=report_global_context,
            allow_external_llm=False,
        )
        self.scheduler.blackboard.update_node(task_id, global_node, "done", agent="global_review_agent", output_summary=str(global_result)[:500])
        subagent_records = [*list(state.get("subagent_records") or []), task_result, module_result, global_result]
        module_records = [*list(state.get("module_review_records") or []), module_result]
        global_records = [*list(state.get("global_review_records") or []), global_result]
        self.scheduler._set_v2_metadata(
            task_id,
            subagent_records=subagent_records,
            module_review_records=module_records,
            global_review_records=global_records,
        )
        return {
            "subagent_records": subagent_records,
            "module_review_records": module_records,
            "global_review_records": global_records,
        }

    def _failure_report(self, state: DynamicGraphState) -> dict[str, Any]:
        reason = str(state.get("reason") or "task_failed")
        self.scheduler._set_v2_metadata(
            state["task_id"],
            current_stage="failure_report",
            failure_reason=reason,
        )
        report_updates = self._terminal_report(state)
        return {
            **report_updates,
            "done": True,
            "needs_repair": False,
            "terminal_failure": True,
            "final_decision": "block_task",
            "final_stage": "failure_report",
            "reason": reason,
        }

    def _finish(self, state: DynamicGraphState) -> dict[str, Any]:
        plan = dict(state.get("plan") or {})
        changes: dict[str, Any] = {
            "global_review_records": state.get("global_review_records") or [],
            "module_review_records": state.get("module_review_records") or [],
            "subagent_records": state.get("subagent_records") or [],
            "central_decisions": state.get("central_decisions") or [],
            "step_skill_contexts": state.get("step_skill_contexts") or [],
        }
        if plan:
            plan["steps"] = list(state.get("plan_steps") or [])
            changes[str(state.get("plan_metadata_key") or "dynamic_plan")] = plan
        self.scheduler._set_v2_metadata(
            state["task_id"],
            **changes,
        )
        return {}
