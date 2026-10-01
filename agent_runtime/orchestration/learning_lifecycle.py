from __future__ import annotations

from typing import Any

from ..utils import now_iso, stable_hash


class LearningLifecycleMixin:
    """Persist terminal task episodes without owning task execution."""

    def _record_terminal_learning(self, task_id: str) -> None:
        """Persist one idempotent episode after a task truly reaches a terminal state."""
        if not getattr(self.learning, "enabled", True):
            return
        record = self.blackboard.get(task_id)
        if record.state not in {"completed", "failed"}:
            return
        metadata = dict(record.task_metadata)
        if metadata.get("learning_episode_id"):
            return
        evidence_pool = dict(metadata.get("evidence_pool_summary") or {})
        selected_evidence: list[dict[str, Any]] = []
        for source_name, source in (evidence_pool.get("sources") or {}).items():
            if not isinstance(source, dict):
                continue
            for item in source.get("items") or source.get("candidates") or []:
                if not isinstance(item, dict):
                    continue
                selected_evidence.append({
                    "source": source_name,
                    "paper_id": item.get("paper_id"),
                    "path": item.get("paper_path") or item.get("path"),
                    "title": item.get("paper_title") or item.get("display_title") or item.get("title"),
                    "score": item.get("score"),
                })
        cst_results = []
        results = metadata.get("results") or {}
        if results:
            cst_results.append({"task_id": task_id, "results": results, "cst_status": metadata.get("cst_status")})
        episode = {
            "schema_version": "1.0",
            "task_id": task_id,
            "task_goal": metadata.get("user_input") or (metadata.get("request") or {}).get("user_input") or record.user_input,
            "mode": metadata.get("mode") or "mock",
            "input_context": {
                "require_paperwise": bool(metadata.get("require_paperwise")),
                "modeling_request": metadata.get("modeling_request") or {},
            },
            "plan_version": (metadata.get("dynamic_plan") or {}).get("plan_version"),
            "retrieved_evidence": selected_evidence,
            "selected_evidence": selected_evidence,
            "agent_predictions": list(metadata.get("subagent_records") or []),
            "actions": list(metadata.get("central_decisions") or []),
            "module_reviews": list(metadata.get("module_review_records") or []),
            "global_reviews": list(metadata.get("global_review_records") or []),
            "user_feedback": list(metadata.get("central_agent_messages") or []),
            "cst_results": cst_results,
            "final_status": record.state,
            "failure_reasons": [str(item.get("reason") or item.get("type") or item) for item in record.blockers],
            "created_at": now_iso(),
        }
        try:
            stored = self.learning.record_episode(episode)
            self._set_v2_metadata(
                task_id,
                learning_episode_id=stored["episode_id"],
                experience_candidate_id=stored.get("experience_candidate_id"),
            )
            self.audit.emit(task_id, {
                "type": "learning_episode_recorded",
                "episode_id": stored["episode_id"],
                "experience_candidate_id": stored.get("experience_candidate_id"),
            })
            if record.state == "completed" and not record.blockers:
                goal = str(episode.get("task_goal") or "")
                goal_pattern = stable_hash(" ".join(goal.lower().split()))[:16]
                self.memory.maybe_store_l3_workflow({
                    "task_status": record.state,
                    "unresolved_failures": len(record.blockers),
                    "graph_step": 0,
                    "workflow_quality": 1.0,
                    "domain": metadata.get("mode") or "antenna",
                    "goal_pattern": goal_pattern,
                    "workflow": list((metadata.get("dynamic_plan") or {}).get("steps") or []),
                })
        except Exception as exc:
            self.audit.emit(task_id, {"type": "learning_episode_record_failed", "error": str(exc)})
