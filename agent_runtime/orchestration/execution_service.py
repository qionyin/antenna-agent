from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from ..runtime_llm_config import runtime_llm_config
from ..utils import atomic_write_json, now_iso, stable_hash


class DynamicExecutionServiceMixin:
    def _execute_dynamic_step_handler(
        self,
        *,
        task_id: str,
        step: dict[str, Any],
        route_plan: dict[str, Any],
        plan_version: int,
        user_input: str,
    ) -> dict[str, Any]:
        """Execute a real local handler or a gate-authorized Antenna Skill adapter."""
        action = str(step.get("action") or "")
        step_id = str(step.get("step_id") or "")
        metadata = self.blackboard.get(task_id).task_metadata
        artifacts = list(metadata.get("artifacts") or [])
        skill_artifacts = self._skill_artifact_refs(artifacts)
        evidence_refs = self._dynamic_evidence_refs(metadata)

        if action == "modeling_preparation":
            modeling_request = dict(metadata.get("modeling_request") or {})
            modeling_request.setdefault("task_id", task_id)
            modeling_request.setdefault("paper_id", task_id)
            attempt = int(metadata.get("modeling_preparation_attempt") or 0) + 1
            output_dir = (
                Path(self.settings.workspace_root)
                / "tasks"
                / task_id
                / "modeling_preparation"
                / f"attempt_{attempt:02d}"
            )
            result = self.modeling_preparation.prepare(modeling_request, output_dir)
            registered: list[dict[str, Any]] = []
            for name, path in sorted((result.get("artifacts") or {}).items()):
                if not path:
                    continue
                registered.append(
                    self._v2_artifact(
                        str(name),
                        str(path),
                        str(name),
                        metadata={
                            "created_by": "modeling_preparation_task_agent",
                            "attempt": attempt,
                            "read_only_source": True,
                            "no_cst_execution": True,
                        },
                    )
                )
            result_path = str(result.get("result_path") or "")
            if result_path:
                registered.append(
                    self._v2_artifact(
                        "modeling_preparation_result",
                        result_path,
                        "modeling_preparation_result",
                        metadata={"attempt": attempt, "no_cst_execution": True},
                    )
                )
            self._append_v2_artifacts(task_id, registered)
            self._set_v2_metadata(
                task_id,
                modeling_preparation_attempt=attempt,
                modeling_preparation_result=result,
                cst_status="not_reached",
            )
            source_refs = [
                str(item.get("source_path"))
                for item in modeling_request.get("evidence") or []
                if isinstance(item, dict) and item.get("source_path")
            ]
            artifact_refs = [str(item.get("path")) for item in registered if item.get("path")]
            success = bool(result.get("success"))
            spec_path = str((result.get("artifacts") or {}).get("cst_model_spec") or "")
            failure = dict(result.get("failure") or {})
            manifest_path = str((result.get("artifacts") or {}).get("cst_model_spec_manifest") or "")
            persisted_validation = self.modeling_preparation.validate_cst_model_spec_artifacts(
                spec_path,
                manifest_path,
            ) if success and spec_path and manifest_path else {"valid": False, "findings": ["cst_model_spec artifacts are missing"]}
            success = (
                success
                and bool(spec_path)
                and Path(spec_path).is_file()
                and not bool(result.get("cst_executed"))
                and bool(persisted_validation.get("valid"))
            )
            if not success and not failure and persisted_validation.get("findings"):
                failure = {
                    "code": "artifact_schema_invalid",
                    "repairable": True,
                    "blockers": list(persisted_validation["findings"]),
                    "missing_inputs": [],
                    "repair_actions": ["archive the invalid artifact set and regenerate it from validated inputs"],
                }
            review = {
                "schema_version": "1.0",
                "decision": "pass" if success else ("revise" if failure.get("repairable") else "block"),
                "status": "pass" if success else ("revise" if failure.get("repairable") else "block"),
                "evidence_level": "A" if success else "D",
                "blocking_findings": [] if success else [
                    {
                        "type": str(failure.get("code") or "modeling_preparation_failed"),
                        "reason": "; ".join(str(item) for item in failure.get("blockers") or []) or "modeling preparation failed",
                    }
                ],
                "required_fixes": list(failure.get("repair_actions") or []),
                "reflection": {
                    "status": "no_issue" if success else "blocked",
                    "error_type": "none" if success else str(failure.get("code") or "modeling_preparation_failed"),
                    "confidence": 0.99,
                },
            }
            self._record_v2_review(task_id, review)
            return {
                "status": "executed" if success else "failed",
                "modeling_preparation": result,
                "validated_cst_model_spec": spec_path if success else "",
                "cst_executed": False,
                "evidence_refs": [*source_refs, *artifact_refs],
                "repairable_failure": None if success else {
                    "code": failure.get("code"),
                    "stage": result.get("completed_stage"),
                    "blockers": failure.get("blockers") or [],
                    "missing_inputs": failure.get("missing_inputs") or [],
                    "repair_actions": failure.get("repair_actions") or [],
                },
                "_module_review": review,
            }

        if action == "preflight":
            request = dict(metadata.get("request") or {})
            preflight = self.cst_real.preflight(request)
            review = self.cst_real.review_preflight(preflight)
            path = self._write_v2_json(task_id, "preflight_result.json", preflight)
            self._set_v2_metadata(task_id, preflight=preflight, cst_status="preflight_done")
            self._append_v2_artifacts(task_id, [self._v2_artifact("preflight_result", path, "preflight")])
            self._record_v2_review(task_id, review)
            return {
                "status": "executed",
                "preflight_result": preflight,
                "evidence_refs": [path],
                "_module_review": review,
            }
        if action == "approval":
            approval = self._ensure_real_cst_approval(task_id)
            approved = self._real_cst_approval_valid(task_id)
            review = {
                "schema_version": "1.0",
                "status": "pass" if approved else "wait_user",
                "decision": "pass" if approved else "wait_user",
                "conclusion": "bound approval is valid" if approved else "waiting for bound real CST approval",
                "blocking_findings": [],
                "required_fixes": [] if approved else ["user approval required before CST solver"],
            }
            return {
                "status": "approved" if approved else "waiting_approval",
                "approval_record": approval,
                "evidence_refs": [],
                "_module_review": review,
            }
        if action == "cst_run":
            if not self._real_cst_approval_valid(task_id):
                return {
                    "status": "blocked",
                    "missing_required_input": "bound real CST approval is missing or stale",
                    "contains_live_cst": True,
                    "evidence_refs": evidence_refs,
                }
            request = dict(metadata.get("request") or {})
            run_manifest = self.cst_real.run_single(task_id, request)
            review = self.cst_real.review_run(run_manifest)
            self._set_v2_metadata(task_id, run_manifest=run_manifest, cst_status="run_done")
            self._append_v2_artifacts(
                task_id,
                [self._v2_artifact("cst_run_manifest", run_manifest.get("manifest_path", ""), "run_manifest"), *(review.get("artifacts") or [])],
            )
            self._record_v2_review(task_id, review)
            return {
                "status": "executed",
                "cst_run_manifest": run_manifest,
                "contains_live_cst": not bool(request.get("simulate_cst")),
                "evidence_refs": [str(run_manifest.get("manifest_path") or "")],
                "_module_review": review,
            }
        if action == "result_parse":
            request = dict(metadata.get("request") or {})
            run_manifest = dict(metadata.get("run_manifest") or {})
            if not run_manifest:
                return {"status": "blocked", "missing_required_input": "cst_run_manifest", "evidence_refs": evidence_refs}
            parsed_result = self.cst_real.parse_results(run_manifest, request)
            review = self.cst_real.review_parsed_results(parsed_result, request)
            self._set_v2_metadata(task_id, results=parsed_result, parse_review=review, cst_status="parsed")
            self._append_v2_artifacts(task_id, [self._v2_artifact("parsed_results", parsed_result.get("path", ""), "parsed_result")])
            self._record_v2_review(task_id, review)
            return {
                "status": "executed",
                "parsed_result": parsed_result,
                "evidence_refs": [str(parsed_result.get("path") or "")],
                "_module_review": review,
            }
        if action == "report" and str(metadata.get("mode")) == "real":
            failure_reason = str(metadata.get("failure_reason") or "") or None
            report_status = "failed" if failure_reason else "completed"
            report = self.cst_real.write_report(
                task_id=task_id,
                task_title=str(metadata.get("task_title") or task_id),
                user_input=str(metadata.get("user_input") or user_input),
                status=report_status,
                run_manifest=metadata.get("run_manifest") or None,
                parsed_result=metadata.get("results") or None,
                reviews=list(metadata.get("reviews") or []),
                artifacts=self.task_artifacts(task_id),
                evidence_pool_summary=metadata.get("evidence_pool_summary") or {},
                failure_reason=failure_reason,
                workspace_root=self.settings.workspace_root,
            )
            review = self.cst_real.review_report(report)
            self._append_v2_reports(task_id, [report])
            self._append_v2_artifacts(
                task_id,
                [
                    self._v2_artifact(f"{report_status}_report_md", report.get("markdown_path", ""), "report"),
                    self._v2_artifact(f"{report_status}_report_html", report.get("html_path", ""), "report"),
                ],
            )
            self._record_v2_review(task_id, review)
            return {
                "status": "executed",
                "task_report": report,
                "evidence_refs": [str(report.get("markdown_path") or ""), str(report.get("html_path") or "")],
                "_module_review": review,
            }

        if action == "evidence_retrieval":
            candidates = self.paperwise.evidence_pool_summary(
                user_input,
                metadata.get("paper_report_path"),
                allow_external_embedding=self._has_approved_external_llm(task_id),
            )
            evidence = self._review_paperwise_evidence(
                user_input,
                candidates,
                allow_runtime_llm=self._has_approved_external_llm(task_id),
            )
            self._set_v2_metadata(task_id, evidence_pool_summary=evidence)
            path = self._write_v2_json(task_id, "paperwise_evidence_pool_summary.json", evidence)
            self._append_v2_artifacts(
                task_id,
                [
                    self._v2_artifact(
                        "paperwise_evidence_pool_summary",
                        path,
                        "paperwise_evidence_pool",
                        metadata={"source": "PaperWise", "read_only": True, "data_egress": False, "no_cst_execution": True},
                    )
                ],
            )
            refs = [
                str(item.get("path"))
                for source in (evidence.get("sources") or {}).values()
                for item in (source.get("items") or [])
                if item.get("path")
            ]
            return {"status": "executed", "evidence_pool": evidence, "evidence_refs": [path, *refs], "read_only": True}
        if action == "evidence_check":
            evidence = metadata.get("evidence_pool_summary") or self.paperwise.evidence_pool_summary(
                user_input,
                metadata.get("paper_report_path"),
                allow_external_embedding=self._has_approved_external_llm(task_id),
            )
            refs = [
                str(item.get("path"))
                for source in (evidence.get("sources") or {}).values()
                for item in (source.get("items") or [])
                if item.get("path")
            ]
            if not refs:
                return {"status": "blocked", "missing_required_input": "no traceable PaperWise evidence", "evidence_refs": []}
            return {"status": "executed", "claim_evidence_review": {"status": "traceable", "count": len(refs)}, "evidence_refs": refs}
        if action == "general" or step.get("task_agent") == "general_task_agent":
            return {"status": "executed", "general_task_summary": user_input, "evidence_refs": []}
        if action == "report":
            failure_reason = str(metadata.get("failure_reason") or "") or None
            report_status = "failed" if failure_reason else "completed"
            report = {
                "schema_version": "1.0",
                "report_type": f"{report_status}_report",
                "status": report_status,
                "task_id": task_id,
                "task_goal": user_input,
                "plan_version": plan_version,
                "artifacts": artifacts,
                "failure_reason": failure_reason,
                "limitations": ["planning path does not execute a live CST solver"],
                "created_at": now_iso(),
            }
            path = self._write_v2_json(task_id, f"{report_status}_report.json", report)
            report["json_path"] = path
            self._append_v2_reports(task_id, [report])
            self._append_v2_artifacts(task_id, [self._v2_artifact(f"{report_status}_report", path, "task_report")])
            return {"status": "executed", "task_report": report, "evidence_refs": [path]}
        if action == "baseline_ablation":
            decision = self.skill_executor_gate.check(
                task_id,
                "run_manifest",
                route_plan,
                step_id=step_id,
                plan_version=plan_version,
            )
            if not decision.allowed or decision.token is None:
                return {
                    "status": "blocked",
                    "missing_required_input": f"skill gate rejected baseline planner: {decision.reason}",
                    "evidence_refs": evidence_refs,
                }
            self.skill_executor_gate.validate_token(
                task_id,
                "run_manifest",
                route_plan,
                decision.token,
                step_id=step_id,
                plan_version=plan_version,
            )
            geometry_ref = self._latest_skill_packet_path(artifacts, {"geometry_contract"})
            if not geometry_ref:
                return {"status": "blocked", "missing_required_input": "baseline planning requires geometry_contract", "evidence_refs": evidence_refs}
            plan = self._baseline_ablation_plan(user_input, artifacts, geometry_ref)
            output_dir = Path(self.settings.workspace_root) / "tasks" / task_id / "dynamic_steps" / step_id
            try:
                adapted = self.antenna_skills.adapt_baseline_ablation_plan(
                    plan,
                    output_dir,
                    timeout_seconds=self.settings.agent_timeout_seconds,
                )
            except Exception as exc:
                return {"status": "blocked", "missing_required_input": f"baseline adapter failed: {exc}", "evidence_refs": evidence_refs}
            if not adapted:
                return {"status": "blocked", "missing_required_input": "baseline adapter unavailable", "evidence_refs": evidence_refs}
            path = str(adapted.get("path") or "")
            self._append_v2_artifacts(task_id, [self._v2_artifact("skill_packet", path, "run_manifest")])
            return {
                "status": "executed",
                "baseline_ablation_plan": plan,
                "skill_packet": adapted.get("packet"),
                "skill_adapter": adapted.get("adapter"),
                "gate_token_id": decision.token.get("token_id"),
                "evidence_refs": [*evidence_refs, path],
            }

        packet_by_action = {
            "goal_contract": "experiment_contract",
            "geometry_evidence": "geometry_contract",
            "claim_assessment": "claim_assessment",
            "antenna_result_to_claim": "claim_assessment",
        }
        packet_type = packet_by_action.get(action)
        if packet_type is None:
            if step.get("required_skills"):
                return {
                    "status": "blocked",
                    "missing_required_input": f"no executable handler for {action}",
                    "evidence_refs": evidence_refs,
                }
            return {"status": "executed", "action": action, "evidence_refs": evidence_refs}

        decision = self.skill_executor_gate.check(
            task_id,
            packet_type,
            route_plan,
            step_id=step_id,
            plan_version=plan_version,
        )
        if not decision.allowed or decision.token is None:
            if action == "goal_contract" and not step.get("required_skills"):
                return {
                    "status": "executed",
                    "objective_contract": self.paperwise._query_profile(user_input),
                    "evidence_refs": evidence_refs,
                }
            return {
                "status": "blocked",
                "missing_required_input": f"skill gate rejected {packet_type}: {decision.reason}",
                "evidence_refs": evidence_refs,
            }
        self.skill_executor_gate.validate_token(
            task_id,
            packet_type,
            route_plan,
            decision.token,
            step_id=step_id,
            plan_version=plan_version,
        )
        output_dir = Path(self.settings.workspace_root) / "tasks" / task_id / "dynamic_steps" / step_id
        source_packet_path = None
        if packet_type == "claim_assessment":
            source_packet_path = self._latest_skill_packet_path(artifacts, {"result_packet", "run_manifest", "experiment_contract"})
        elif packet_type == "next_iteration_plan":
            source_packet_path = self._latest_skill_packet_path(
                artifacts,
                {"idea_card", "experiment_contract", "geometry_contract", "run_manifest", "result_packet", "claim_assessment"},
            )
        try:
            if packet_type == "claim_assessment" and not source_packet_path:
                adapted = self.antenna_skills.adapt_claim_assessment(
                    {
                        "claim_id": stable_hash(user_input)[:16],
                        "claim": user_input,
                        "support": "insufficient",
                        "claim_ceiling": "insufficient until validated CST/result exports exist",
                        "evidence_summary": "PaperWise context exists, but no validated result_packet is available.",
                        "limitations": ["missing validated result_packet"],
                        "next_required_experiments": ["run CST and export the target metrics"],
                    },
                    output_dir,
                    timeout_seconds=self.settings.agent_timeout_seconds,
                )
            else:
                adapted = self.antenna_skills.adapt_packet_stage(
                    packet_type,
                    {"goal": user_input, "parsed_goal": self.paperwise._query_profile(user_input)},
                    skill_artifacts,
                    output_dir,
                    source_packet_path=source_packet_path,
                    timeout_seconds=self.settings.agent_timeout_seconds,
                )
        except Exception as exc:
            return {
                "status": "blocked",
                "missing_required_input": f"adapter execution failed for {packet_type}: {exc}",
                "evidence_refs": evidence_refs,
            }
        if not adapted:
            return {
                "status": "blocked",
                "missing_required_input": f"adapter unavailable for {packet_type}",
                "evidence_refs": evidence_refs,
            }
        path = str(adapted.get("path") or "")
        if path:
            self._append_v2_artifacts(task_id, [self._v2_artifact("skill_packet", path, packet_type)])
        return {
            "status": "executed",
            "skill_packet": adapted.get("packet"),
            "skill_adapter": adapted.get("adapter"),
            "gate_token_id": decision.token.get("token_id"),
            "evidence_refs": [*evidence_refs, *([path] if path else [])],
        }

    def _execute_repair_action(
        self,
        *,
        task_id: str,
        action: str,
        failure: dict[str, Any],
        failed_step: dict[str, Any],
        plan_version: int,
    ) -> dict[str, Any]:
        """Apply one bounded repair that must change executable state."""
        record = self.blackboard.get(task_id)
        metadata = dict(record.task_metadata)
        before: Any
        after: Any
        change_refs: list[str] = []
        repair_round = int(failure.get("repair_rounds") or 0)

        if action == "refresh_skill_route":
            before = metadata.get("route_repair_context") or {}
            after = {
                "failure_id": failure.get("failure_id"),
                "failed_step_id": failure.get("step_id"),
                "repair_round": repair_round,
                "review_feedback": failure.get("error"),
            }
            self._set_v2_metadata(task_id, route_repair_context=after)
            path = self._write_v2_json(task_id, "route_repair_context.json", after)
            change_refs.append(path)
        elif action == "regenerate_plan":
            before = metadata.get("plan_repair_context") or {}
            after = {
                "failure_id": failure.get("failure_id"),
                "failed_step_id": failure.get("step_id"),
                "repair_round": repair_round,
                "previous_plan_version": plan_version,
                "next_plan_version": plan_version + 1,
                "validation_feedback": failure.get("error"),
            }
            self._set_v2_metadata(task_id, plan_repair_context=after)
            path = self._write_v2_json(task_id, "plan_repair_context.json", after)
            change_refs.append(path)
        elif action in {"supplement_read_only_evidence", "revise_modeling_input"}:
            request = dict(metadata.get("modeling_request") or {})
            before = json.loads(json.dumps(request, ensure_ascii=False, default=str))
            failure_details = dict((metadata.get("modeling_preparation_result") or {}).get("failure") or {})
            missing_inputs = [str(item) for item in failure_details.get("missing_inputs") or []]
            blockers = [str(item) for item in failure_details.get("blockers") or []]
            if failure.get("error"):
                blockers.append(str(failure["error"]))
            if action == "supplement_read_only_evidence":
                additions = self.modeling_preparation.recover_missing_evidence(
                    request,
                    missing_inputs=missing_inputs,
                    blockers=blockers,
                )
                existing = list(request.get("evidence") or [])
                existing_ids = {str(item.get("id")) for item in existing if isinstance(item, dict)}
                evidence_changed = False
                for item in additions:
                    if isinstance(item, dict) and str(item.get("id")) not in existing_ids:
                        existing.append(dict(item))
                        existing_ids.add(str(item.get("id")))
                        evidence_changed = True
                if evidence_changed:
                    request["evidence"] = existing
            else:
                repair = self.modeling_preparation.repair_non_protected_modeling_request(
                    request,
                    missing_inputs=missing_inputs,
                    blockers=blockers,
                )
                request = dict(repair["request"])
            after = request
            if stable_hash(before) != stable_hash(after):
                self._set_v2_metadata(task_id, modeling_request=after)
                path = self._write_v2_json(task_id, f"modeling_request_repair_{repair_round}.json", after)
                change_refs.append(path)
        elif action == "regenerate_artifact":
            before = metadata.get("modeling_preparation_result") or {}
            task_root = Path(self.settings.workspace_root) / "tasks" / task_id
            attempt = int(metadata.get("modeling_preparation_attempt") or 0)
            source = task_root / "modeling_preparation" / f"attempt_{attempt:02d}"
            archive = task_root / "repair_archive" / f"artifact_round_{repair_round}"
            if source.exists():
                archive.parent.mkdir(parents=True, exist_ok=True)
                if archive.exists():
                    shutil.rmtree(archive)
                shutil.move(str(source), str(archive))
                change_refs.append(str(archive))
            after = {
                "regenerate": True,
                "failure_id": failure.get("failure_id"),
                "repair_round": repair_round,
                "archived_previous_attempt": str(archive) if archive.exists() else None,
            }
            path = self._write_v2_json(task_id, f"artifact_regeneration_{repair_round}.json", after)
            change_refs.append(path)
            self._set_v2_metadata(task_id, modeling_preparation_result={})
        else:
            return {
                "status": "blocked",
                "changed": False,
                "change_refs": [],
                "reason": f"unsupported automatic repair action: {action}",
            }

        changed = stable_hash(before) != stable_hash(after) and bool(change_refs)
        manifest = {
            "schema_version": "1.0",
            "task_id": task_id,
            "failure_id": failure.get("failure_id"),
            "action": action,
            "repair_round": repair_round,
            "before_hash": stable_hash(before),
            "after_hash": stable_hash(after),
            "changed": changed,
            "change_refs": change_refs,
            "created_at": now_iso(),
        }
        manifest_path = self._write_v2_json(task_id, f"repair_change_{repair_round}.json", manifest)
        if manifest_path not in change_refs:
            change_refs.append(manifest_path)
        self._append_v2_artifacts(
            task_id,
            [
                self._v2_artifact(
                    f"repair_change_round_{repair_round}",
                    manifest_path,
                    "repair_change_manifest",
                    metadata={"no_cst_execution": True, "failure_id": failure.get("failure_id")},
                )
            ],
        )
        return {
            "status": "executed" if changed else "no_change",
            "changed": changed,
            "change_refs": change_refs,
            "before_hash": manifest["before_hash"],
            "after_hash": manifest["after_hash"],
            "repair_manifest": manifest_path,
            "reason": "repair changed executable state" if changed else "repair input did not change",
        }

    def _dynamic_evidence_refs(self, metadata: dict[str, Any]) -> list[str]:
        return [str(item.get("path")) for item in (metadata.get("artifacts") or []) if item.get("path")]

    def _step_external_llm_allowed(self, task_id: str, step: dict[str, Any]) -> bool:
        return task_id in self._external_llm_approved_tasks and bool(step.get("external_llm_required", False))

    def _skill_artifact_refs(self, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for index, artifact in enumerate(artifacts):
            path = str(artifact.get("path") or "")
            artifact_type = str(artifact.get("artifact_type") or artifact.get("type") or "artifact")
            name = str(artifact.get("name") or (Path(path).name if path else f"artifact_{index + 1}"))
            refs.append(
                {
                    "schema_version": "1.0",
                    "id": str(artifact.get("id") or stable_hash({"path": path, "type": artifact_type})[:16]),
                    "name": name,
                    "artifact_type": artifact_type,
                    "source_skill": str(artifact.get("source_skill") or artifact.get("created_by") or "antenna_agent_lab"),
                    "status": str(artifact.get("status") or "ready"),
                    "path": path,
                    "metadata": dict(artifact.get("metadata") or {}),
                }
            )
        return refs

    def _latest_skill_packet_path(self, artifacts: list[dict[str, Any]], allowed_types: set[str]) -> str | None:
        for artifact in reversed(artifacts):
            if artifact.get("artifact_type") not in allowed_types:
                continue
            path = str(artifact.get("path") or "")
            if path and Path(path).is_file():
                return path
        return None

    def _baseline_ablation_plan(
        self,
        user_input: str,
        artifacts: list[dict[str, Any]],
        geometry_ref: str,
    ) -> dict[str, Any]:
        profile = self.paperwise._query_profile(user_input)
        algorithms = list(profile.get("algorithms") or ["candidate"])
        metrics = [str(item).upper() for item in (profile.get("objectives") or ["s11"])]
        jobs = []
        for index, algorithm in enumerate(algorithms):
            jobs.append(
                {
                    "job_id": f"{algorithm}_{index + 1}",
                    "role": "candidate" if index == 0 else "baseline",
                    "algorithm": algorithm,
                    "geometry_ref": geometry_ref,
                    "metrics": metrics,
                }
            )
        return {
            "schema_version": "1.0",
            "run_id": stable_hash({"goal": user_input, "geometry_ref": geometry_ref})[:16],
            "experiment_ref": self._latest_skill_packet_path(artifacts, {"experiment_contract"}) or "",
            "geometry_ref": geometry_ref,
            "metrics": metrics,
            "jobs": jobs,
            "baselines": [job for job in jobs if job["role"] == "baseline"],
            "ablations": [{"name": "candidate_without_adaptive_component", "source_job": jobs[0]["job_id"]}],
            "export_requirements": ["S11", "bandwidth"],
            "created_at": now_iso(),
        }

    def _paperwise_required_but_unavailable(self, paper_report_path: str | None, require_paperwise: bool) -> str | None:
        if not require_paperwise:
            return None
        if paper_report_path:
            report = self.paperwise.read_report(paper_report_path)
            return None if report.get("available") else str(report.get("error") or "paper_report_unavailable")
        inventory = self.paperwise.inventory()
        if inventory.get("reports") or inventory.get("vector_library_exists") or inventory.get("graph_library_exists"):
            return None
        return "paperwise_evidence_required_but_no_report_vector_or_graph_available"

    def _default_dynamic_title(self, user_input: str) -> str:
        text = str(user_input or "").lower()
        if any(term in text for term in ("antenna", "s11", "cst", "gain", "arbw", "天线", "贴片")):
            if any(term in text for term in ("paper", "论文", "复现")):
                return "动态计划 - 天线论文复现"
            if "cst" in text:
                return "动态计划 - CST 天线任务"
            return "动态计划 - 天线研究任务"
        return "动态计划 - 通用任务"

    def _persist_skill_route_result(
        self,
        *,
        task_id: str,
        mode: str,
        plan: dict[str, Any],
        review: dict[str, Any],
    ) -> None:
        """Persist one graph-produced initial skill route and review."""
        route_dir = Path(self.settings.workspace_root) / "tasks" / task_id / "skill_routes"
        plan_path = route_dir / "skill_route_plan.json"
        review_path = route_dir / "skill_route_review.json"
        atomic_write_json(plan_path, plan)
        atomic_write_json(review_path, review)

        record = self.blackboard.get(task_id)
        metadata = dict(record.task_metadata)
        artifacts = list(metadata.get("artifacts") or [])
        route_artifacts = [
            self._artifact_ref(
                "skill_route_plan",
                str(plan_path),
                "skill_route_plan",
                "central_skill_router",
                metadata={"mode": mode, "data_egress": False},
            ),
            self._artifact_ref(
                "skill_route_review",
                str(review_path),
                "skill_route_review",
                "skill_route_review_agent",
                metadata={"mode": mode, "data_egress": False},
            ),
        ]
        seen = {item.get("id") or item.get("path") for item in artifacts}
        for artifact in route_artifacts:
            key = artifact.get("id") or artifact.get("path")
            if key not in seen:
                artifacts.append(artifact)
                seen.add(key)
        metadata.update(
            {
                "initial_skill_route_plan": metadata.get("initial_skill_route_plan") or plan,
                "active_skill_route_plan": plan,
                "skill_route_plan": plan,
                "skill_route_review": review,
                "skill_route_plan_history": [
                    *(metadata.get("skill_route_plan_history") or []),
                    {
                        "plan": plan,
                        "review": review,
                        "reason": "initial_route" if not metadata.get("skill_route_plan_history") else "route_refresh",
                    },
                ],
                "artifacts": artifacts,
            }
        )
        self.blackboard.update(task_id, task_metadata=metadata)
        self.audit.emit(
            task_id,
            {
                "type": "skill_route_selected",
                "mode": mode,
                "decision": plan.get("decision"),
                "selected_owner_skills": plan.get("selected_owner_skills", []),
                "review_status": review.get("status"),
                "plan_path": str(plan_path),
                "review_path": str(review_path),
            },
        )
    def _has_approved_external_llm(self, task_id: str) -> bool:
        """检查任务是否已有外部 LLM 审批。"""
        approval = self.blackboard.get(task_id).approvals.get(f"external_llm:{task_id}")
        return isinstance(approval, dict) and approval.get("status") == "approved"

    def _external_llm_enabled(self) -> bool:
        return bool(self.settings.llm_enabled or runtime_llm_config.enabled)

    def _external_llm_egress_plan(self, operation: str, text: str, purpose: str) -> dict[str, Any]:
        if runtime_llm_config.enabled:
            return {
                "data_egress": True,
                "egress_target": runtime_llm_config.base_url,
                "operation": operation,
                "model": runtime_llm_config.model_name,
                "purpose": purpose,
                "input_hash": stable_hash(text),
                "input_chars": len(text),
            }
        return self.llm.egress_plan(operation, text, purpose)

    def _step_route_context(
        self,
        task_id: str,
        step: dict[str, Any],
        module_review_records: list[dict[str, Any]],
        global_review_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        metadata = self.blackboard.get(task_id).task_metadata
        evidence_terms: list[str] = []
        evidence = metadata.get("evidence_pool_summary") or {}
        for source in (evidence.get("sources") or {}).values():
            for item in source.get("items") or source.get("candidates") or []:
                evidence_terms.extend([item.get("title"), item.get("display_title"), item.get("description"), item.get("snippet")])
        memory_context = metadata.get("retrieval_context") or {}
        for item in [*(memory_context.get("l2") or []), *(memory_context.get("l3") or [])]:
            evidence_terms.extend([item.get("fact"), item.get("workflow_key"), item.get("goal_pattern"), item.get("domain")])
        learning_context = metadata.get("validated_learning_context") or {}
        for item in [*(learning_context.get("knowledge") or []), *(learning_context.get("experiences") or [])]:
            evidence_terms.extend([
                item.get("subject"),
                item.get("relation"),
                item.get("object"),
                item.get("mechanism"),
                item.get("lesson"),
                item.get("action"),
            ])
        review_suggested: list[str] = []
        missing_dependency: list[str] = []
        for review in [*module_review_records, *global_review_records]:
            output = review.get("output") or review
            suggestion = output.get("reroute_suggestion") or {}
            for suggested_step in suggestion.get("suggested_steps") or []:
                review_suggested.extend(suggested_step.get("required_skills") or [])
            reflection = output.get("reflection") or {}
            if reflection.get("error_type") == "missing_dependency":
                missing_dependency.extend(step.get("required_skills") or [])
        return {
            "context_task_id": task_id,
            "is_antenna_context": bool((metadata.get("skill_route_plan") or {}).get("selected_owner_skills")) or bool(step.get("required_skills")),
            "evidence_terms": [str(item) for item in evidence_terms if item],
            "evidence_suggested_skills": step.get("required_skills") or [],
            "review_suggested_skills": sorted(set(review_suggested)),
            "missing_dependency_skills": sorted(set(missing_dependency)),
        }
