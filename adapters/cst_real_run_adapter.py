from __future__ import annotations

import csv
import html
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_runtime.utils import atomic_write_json, now_iso, stable_hash


class CstRealRunAdapter:
    """V2.0 adapter for one approved CST run and local result parsing."""

    DEFAULT_CST_ROOT = Path(r"D:\Program Files (x86)\CST Studio Suite 2022")
    CST_CONTROL_SCRIPT = Path(r"C:\Users\30626\.codex\skills\Antenna Skills\cst-control\scripts\cst_control.py")
    PLACEHOLDER_PATHS = {
        r"d:\path\model.cst",
        "d:/path/model.cst",
        r"d:\path\model.json",
        "d:/path/model.json",
    }

    def __init__(
        self,
        *,
        e_platform_root: str | Path,
        e_results_root: str | Path,
        cst_root: str | Path | None = None,
    ) -> None:
        self.e_platform_root = Path(e_platform_root)
        self.e_results_root = Path(e_results_root)
        self.cst_root = Path(cst_root) if cst_root else self.DEFAULT_CST_ROOT
        self.runs_root = self.e_results_root / "runs"

    def preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        simulate = bool(request.get("simulate_cst"))
        project_path = self._request_path(request, "project_path")
        model_json_path = self._request_path(request, "model_json_path")
        fixture_result_dir = Path(str(request["fixture_result_dir"])).expanduser() if request.get("fixture_result_dir") else None
        parameters = self._parameters_from_request(request)

        checks = [
            self._exists_check("e_platform_root", self.e_platform_root),
            self._exists_check("e_cst_local_adapter", self.e_platform_root / "simulator_skills" / "cst_local" / "adapter.py"),
            self._exists_check("e_cst_local_official_runner", self.e_platform_root / "simulator_skills" / "cst_local" / "official.py"),
            self._exists_check("cst_control_script", self.CST_CONTROL_SCRIPT),
            self._exists_check("cst_root", self.cst_root),
            self._exists_check("cst_python", self.cst_root / "AMD64" / "python" / "python.exe"),
            self._exists_check("python_cst_libraries", self.cst_root / "AMD64" / "python_cst_libraries"),
        ]
        if project_path is not None:
            checks.append(self._exists_check("project_path", project_path))
        if model_json_path is not None:
            checks.append(self._exists_check("model_json_path", model_json_path))
        if fixture_result_dir is not None:
            checks.append(self._exists_check("fixture_result_dir", fixture_result_dir))

        output_writable = self._writable_check("runs_root", self.runs_root)
        checks.append(output_writable)
        checks.append(self._parameter_check(parameters, required=not simulate and fixture_result_dir is None))

        input_blockers = []
        blocker_details = []
        for field_name in ("project_path", "model_json_path"):
            raw_value = request.get(field_name)
            if raw_value and self._is_placeholder_path(str(raw_value)):
                reason = f"{field_name} is a placeholder, provide a real input path"
                input_blockers.append(reason)
                blocker_details.append(
                    self._blocker(
                        "placeholder_path",
                        reason,
                        field=field_name,
                        hint="不要使用 D:\\path\\model.cst 示例路径；请填写真实 .cst 或 model_json_path。",
                    )
                )
            if raw_value and not self._has_expected_extension(str(raw_value), field_name):
                reason = f"{field_name} has an unsupported file extension"
                input_blockers.append(reason)
                blocker_details.append(
                    self._blocker(
                        "invalid_path_extension",
                        reason,
                        field=field_name,
                        hint="project_path must end with .cst; model_json_path must end with .json.",
                    )
                )
        if not simulate and fixture_result_dir is None and project_path is None and model_json_path is None:
            reason = "real mode requires project_path or model_json_path"
            input_blockers.append(reason)
            blocker_details.append(
                self._blocker(
                    "missing_real_input",
                    reason,
                    field="project_path|model_json_path",
                    hint="真实模式至少需要一个真实 .cst project_path，或一个可创建 CST 项目的 model_json_path。",
                )
            )
        if not simulate and fixture_result_dir is None and not parameters:
            reason = "real CST mode requires non-empty parameters"
            input_blockers.append(reason)
            blocker_details.append(
                self._blocker(
                    "missing_parameters",
                    reason,
                    field="parameters",
                    hint="请填写参数 JSON，或在 model_json_path 指向的 JSON 中提供 parameters 对象。",
                )
            )

        success = all(item["ok"] for item in checks) and not input_blockers
        return {
            "schema_version": "1.0",
            "operation": "v2_0_real_cst_preflight",
            "success": success,
            "mode": "real",
            "simulate_cst": simulate,
            "checks": checks,
            "blockers": input_blockers,
            "blocker_details": blocker_details,
            "runs_root": str(self.runs_root),
            "parameters_count": len(parameters),
            "created_at": now_iso(),
        }

    def review_preflight(self, preflight: dict[str, Any]) -> dict[str, Any]:
        blockers = list(preflight.get("blockers") or [])
        blockers.extend(item["reason"] for item in preflight.get("checks", []) if not item.get("ok"))
        structured_blockers = list(preflight.get("blocker_details") or [])
        seen_reasons = {item.get("reason") for item in structured_blockers}
        for item in preflight.get("checks", []):
            if item.get("ok"):
                continue
            reason = item.get("reason") or f"{item.get('name')} failed"
            if reason in seen_reasons:
                continue
            seen_reasons.add(reason)
            structured_blockers.append(self._check_blocker(item))
        return {
            "schema_version": "1.0",
            "agent": "preflight_review_agent",
            "status": "pass" if preflight.get("success") and not blockers else "block",
            "blockers": blockers,
            "structured_blockers": structured_blockers,
            "checks": preflight.get("checks", []),
            "conclusion": "允许进入真实 CST 审批" if not blockers else "预检未通过，不能进入真实 CST 审批",
            "created_at": now_iso(),
        }

    def run_single(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        run_dir = self.runs_root / task_id
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        if request.get("fixture_result_dir"):
            return self._register_fixture_run(task_id, request, run_dir)
        if request.get("simulate_cst"):
            return self._simulate_run(task_id, request, run_dir)

        project_path = Path(str(request["project_path"])).expanduser() if request.get("project_path") else None
        model_json_path = Path(str(request["model_json_path"])).expanduser() if request.get("model_json_path") else None
        if project_path is None and model_json_path is not None:
            project_path = run_dir / "model.cst"
            create_manifest = run_dir / "create_project_manifest.json"
            command = [
                str(self.cst_root / "AMD64" / "python" / "python.exe"),
                str(self.CST_CONTROL_SCRIPT),
                "--cst-root",
                str(self.cst_root),
                "create-project",
                "--project",
                str(project_path),
                "--model-json",
                str(model_json_path),
                "--manifest",
                str(create_manifest),
                "--execute",
                "--force",
            ]
            self._run_subprocess(
                command,
                run_dir / "create_project_stdout.log",
                run_dir / "create_project_stderr.log",
                timeout_seconds=int(request.get("cst_timeout_seconds", 7200)),
            )

        if project_path is None:
            raise ValueError("project_path is required for real CST execution")

        parameters = self._parameters_from_request(request)
        if not parameters:
            raise ValueError("real CST execution requires non-empty parameters")

        sys.path.insert(0, str(self.e_platform_root))
        try:
            from simulator_skills.cst_local.adapter import CstLocalSkill
        finally:
            try:
                sys.path.remove(str(self.e_platform_root))
            except ValueError:
                pass

        ctx = {
            "mode": "cst",
            "run_dir": str(run_dir),
            "config": {
                "paths": {
                    "results_dir": str(self.e_results_root),
                    "cst_project": str(project_path),
                    "cst_python_exe": str(self.cst_root / "AMD64" / "python" / "python.exe"),
                    "cst_python_lib": str(self.cst_root / "AMD64" / "python_cst_libraries"),
                    "cst_run_solver": True,
                    "cst_use_active_project": bool(request.get("cst_use_active_project", True)),
                    "cst_save_project": bool(request.get("cst_save_project", False)),
                    "cst_timeout_seconds": int(request.get("cst_timeout_seconds", 7200)),
                    "cst_s11_tree_item": request.get("cst_s11_tree_item", r"1D Results\S-Parameters\S1,1"),
                    "cst_backup_root": str(self.e_results_root / "cst_backups"),
                }
            },
        }
        result = CstLocalSkill.run(ctx, {"optimizer_step": {"proposed_params": parameters}})
        manifest = {
            "schema_version": "1.0",
            "operation": "v2_0_real_cst_single_run",
            "task_id": task_id,
            "run_dir": str(run_dir),
            "project_path": str(project_path),
            "model_json_path": str(model_json_path) if model_json_path else None,
            "parameters": parameters,
            "result": result,
            "success": result.get("status") == "ok",
            "created_at": now_iso(),
        }
        manifest_path = run_dir / "run_manifest.json"
        atomic_write_json(manifest_path, manifest)
        return {**manifest, "manifest_path": str(manifest_path)}

    def review_run(self, run_manifest: dict[str, Any]) -> dict[str, Any]:
        blockers = []
        run_dir = Path(str(run_manifest.get("run_dir", "")))
        if not run_dir.is_dir():
            blockers.append("CST run directory does not exist")
        manifest_path = Path(str(run_manifest.get("manifest_path", "")))
        if manifest_path and not manifest_path.is_file():
            blockers.append("run manifest is missing")
        if run_manifest.get("success") is not True:
            error = run_manifest.get("result", {}).get("error") or run_manifest.get("result", {}).get("message")
            blockers.append(str(error or "CST run did not return ok status"))
        artifacts = self._collect_artifacts(run_dir) if run_dir.is_dir() else []
        if not artifacts:
            blockers.append("run directory has no artifacts")
        return {
            "schema_version": "1.0",
            "agent": "cst_run_review_agent",
            "status": "pass" if not blockers else "block",
            "blockers": blockers,
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
            "created_at": now_iso(),
        }

    def parse_results(self, run_manifest: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        result = run_manifest.get("result") or {}
        output = result.get("output") if isinstance(result.get("output"), dict) else {}
        freq = output.get("freq") or result.get("freq") or []
        s11 = output.get("s11") or result.get("s11") or []
        source = "skill_output"
        source_path = None
        if not freq or not s11:
            parsed = self._parse_s11_file(Path(str(run_manifest["run_dir"])))
            freq = parsed["freq"]
            s11 = parsed["s11"]
            source = "file_scan"
            source_path = parsed["source_path"]

        pairs = self._numeric_pairs(freq, s11)
        metrics = self._compute_s11_metrics(pairs, request)
        parsed_result = {
            "schema_version": "1.0",
            "operation": "v2_0_result_parse",
            "status": "done" if pairs else "failed",
            "source": source,
            "source_path": source_path,
            "point_count": len(pairs),
            "freq": [row[0] for row in pairs],
            "s11": [row[1] for row in pairs],
            "metrics": metrics,
            "not_available": ["gain", "efficiency", "axial_ratio", "pattern"],
            "created_at": now_iso(),
        }
        output_path = Path(str(run_manifest["run_dir"])) / "parsed_results.json"
        atomic_write_json(output_path, parsed_result)
        parsed_result["path"] = str(output_path)
        return parsed_result

    def review_parsed_results(self, parsed_result: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        freq = parsed_result.get("freq") or []
        s11 = parsed_result.get("s11") or []
        blockers = []
        if not freq or not s11:
            blockers.append("result file exists but no S11 data was parsed")
        if len(freq) != len(s11):
            blockers.append("frequency and S11 lengths do not match")
        if any(not math.isfinite(float(value)) for value in s11):
            blockers.append("S11 curve contains NaN or infinite values")
        if s11 and all(abs(float(value)) < 1e-12 for value in s11):
            blockers.append("S11 curve is all zero")
        if len({round(float(value), 12) for value in s11}) <= 1 and len(s11) > 1:
            blockers.append("S11 curve is constant")
        band = request.get("target_freq_range") or request.get("target_band")
        if band and len(band) == 2:
            lo, hi = float(band[0]), float(band[1])
            if not any(lo <= float(freq_value) <= hi for freq_value in freq):
                blockers.append("target frequency band has no data")
        metrics = parsed_result.get("metrics") or {}
        if metrics.get("s11_min_db") is None:
            blockers.append("target metric S11 is not available")
        return {
            "schema_version": "1.0",
            "agent": "result_parse_review_agent",
            "status": "pass" if not blockers else "block",
            "blockers": blockers,
            "metrics": metrics,
            "conclusion": "S11 / return loss / bandwidth 解析结果可用于报告" if not blockers else "解析结果不可信，不能生成完成报告",
            "created_at": now_iso(),
        }

    def write_report(
        self,
        *,
        task_id: str,
        task_title: str,
        user_input: str,
        status: str,
        run_manifest: dict[str, Any] | None,
        parsed_result: dict[str, Any] | None,
        reviews: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        evidence_pool_summary: dict[str, Any] | None,
        failure_reason: str | None,
        workspace_root: str | Path,
    ) -> dict[str, Any]:
        report_kind = "completed_report" if status == "completed" else "failed_report"
        report_dir = Path(workspace_root) / "tasks" / task_id / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        md_path = report_dir / f"{report_kind}.md"
        html_path = report_dir / f"{report_kind}.html"
        metrics = (parsed_result or {}).get("metrics") or {}
        run_dir = (run_manifest or {}).get("run_dir") or "not_reached"
        failure_stage = self._failure_stage(failure_reason)
        cst_status = "not_reached" if run_manifest is None else ("completed" if run_manifest.get("success") else "failed")
        result_status = "not_available" if parsed_result is None else parsed_result.get("status", "available")
        lines = [
            f"# {task_title}",
            "",
            f"- 报告类型: {report_kind}",
            f"- 任务 ID: {task_id}",
            "- 运行模式: real",
            f"- 任务目标: {user_input}",
            f"- CST run 路径: {run_dir}",
            f"- 状态: {status}",
            f"- 失败原因: {failure_reason or 'none'}",
            f"- Failure stage: {failure_stage}",
            f"- Preflight: {'failed' if failure_reason == 'preflight_failed' else 'not_failed'}",
            f"- CST: {cst_status}",
            f"- Result: {result_status}",
            "",
            "## 解析结果",
            "",
            f"- S11 最小值: {metrics.get('s11_min_db', 'not_available')}",
            f"- S11 最小值频点: {metrics.get('s11_min_freq', 'not_available')}",
            f"- Return loss 最大值: {metrics.get('return_loss_max_db', 'not_available')}",
            f"- -10 dB 带宽: {metrics.get('bandwidth_10db', 'not_available')}",
            "",
            "## Artifact 列表",
            "",
        ]
        lines[18:18] = [
            "## PaperWise Evidence Pool",
            "",
            *self._paperwise_evidence_lines(evidence_pool_summary or {}),
            "",
        ]
        if artifacts:
            lines.extend(f"- {item.get('name')}: {item.get('path')}" for item in artifacts)
        else:
            lines.append("- CST: not_reached")
            lines.append("- Result: not_available")
            lines.append("- Reason: failed_before_cst")
        lines.extend(["", "## 审查结论", ""])
        for review in reviews:
            lines.append(f"- {review.get('agent')}: {review.get('status')} | {review.get('conclusion') or review.get('blockers')}")
            for blocker in review.get("structured_blockers") or []:
                lines.append(f"  - {blocker.get('type')}: {blocker.get('reason')} | 建议: {blocker.get('hint')}")
        lines.extend(["", "## 下一步建议", "", self._next_step(status, failure_reason)])
        markdown = "\n".join(lines) + "\n"
        self._atomic_write_text(md_path, markdown)
        self._atomic_write_text(
            html_path,
            "<!doctype html><meta charset=\"utf-8\"><title>"
            + html.escape(task_title)
            + "</title><body><pre>"
            + html.escape(markdown)
            + "</pre></body>",
        )
        return {
            "schema_version": "1.0",
            "report_type": report_kind,
            "task_id": task_id,
            "status": status,
            "markdown_path": str(md_path),
            "html_path": str(html_path),
            "created_at": now_iso(),
        }

    def review_report(self, report: dict[str, Any]) -> dict[str, Any]:
        blockers = []
        for key in ("markdown_path", "html_path"):
            path = Path(str(report.get(key, "")))
            if not path.is_file() or path.stat().st_size == 0:
                blockers.append(f"{key} missing or empty")
        return {
            "schema_version": "1.0",
            "agent": "report_review_agent",
            "status": "pass" if not blockers else "block",
            "blockers": blockers,
            "conclusion": "报告已包含任务目标、路径、artifact、结果、审查结论和下一步建议" if not blockers else "报告不完整",
            "created_at": now_iso(),
        }

    def _parameters_from_request(self, request: dict[str, Any]) -> dict[str, Any]:
        parameters = request.get("parameters")
        if isinstance(parameters, dict) and parameters:
            return dict(parameters)
        model_json_path = request.get("model_json_path")
        if model_json_path and Path(str(model_json_path)).is_file():
            try:
                model = json.loads(Path(str(model_json_path)).read_text(encoding="utf-8-sig"))
            except Exception:
                return {}
            params = model.get("parameters")
            return dict(params) if isinstance(params, dict) else {}
        return {}

    def _request_path(self, request: dict[str, Any], key: str) -> Path | None:
        value = request.get(key)
        if not value or self._is_placeholder_path(str(value)):
            return None
        return Path(str(value)).expanduser()

    def _is_placeholder_path(self, value: str) -> bool:
        normalized = re.sub(r"\\+", r"\\", value.strip().replace("/", "\\").lower())
        placeholders = {re.sub(r"\\+", r"\\", item.replace("/", "\\").lower()) for item in self.PLACEHOLDER_PATHS}
        return normalized in placeholders

    def _has_expected_extension(self, value: str, field_name: str) -> bool:
        suffix = Path(value.strip().strip('"')).suffix.lower()
        if field_name == "project_path":
            return suffix == ".cst"
        if field_name == "model_json_path":
            return suffix == ".json"
        return True

    def _exists_check(self, name: str, path: Path) -> dict[str, Any]:
        exists = path.exists()
        return {
            "name": name,
            "path": str(path),
            "ok": exists,
            "status": "pass" if exists else "fail",
            "severity": "info" if exists else "error",
            "category": "path_exists",
            "reason": "" if exists else f"{name} not found: {path}",
            "hint": "" if exists else self._check_hint(name),
        }

    def _writable_check(self, name: str, path: Path) -> dict[str, Any]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".write-check-{stable_hash({'path': str(path), 'time': now_iso()})[:12]}.tmp"
            probe.write_bytes(b"")
            probe.unlink()
            return {
                "name": name,
                "path": str(path),
                "ok": True,
                "status": "pass",
                "severity": "info",
                "category": "path_writable",
                "reason": "",
                "hint": "",
            }
        except Exception as exc:
            return {
                "name": name,
                "path": str(path),
                "ok": False,
                "status": "fail",
                "severity": "error",
                "category": "path_writable",
                "reason": f"{name} not writable: {exc}",
                "hint": "请确认输出目录可创建、可写，且没有被权限或占用问题阻塞。",
            }

    def _parameter_check(self, parameters: dict[str, Any], *, required: bool) -> dict[str, Any]:
        ok = bool(parameters) or not required
        return {
            "name": "parameters",
            "path": "",
            "ok": ok,
            "status": "pass" if ok else "fail",
            "severity": "info" if ok else "error",
            "category": "input_parameters",
            "reason": "" if ok else "real CST mode requires non-empty parameters",
            "hint": "" if ok else "请填写参数 JSON，或在 model_json_path 指向的 JSON 中提供 parameters 对象。",
            "count": len(parameters),
        }

    def _blocker(self, blocker_type: str, reason: str, *, field: str, hint: str) -> dict[str, Any]:
        return {
            "type": blocker_type,
            "field": field,
            "severity": "error",
            "status": "block",
            "reason": reason,
            "hint": hint,
        }

    def _check_blocker(self, check: dict[str, Any]) -> dict[str, Any]:
        return self._blocker(
            str(check.get("category") or "preflight_check_failed"),
            str(check.get("reason") or f"{check.get('name')} failed"),
            field=str(check.get("name") or "preflight"),
            hint=str(check.get("hint") or "请修复该预检项后重新创建任务。"),
        )

    def _check_hint(self, name: str) -> str:
        hints = {
            "project_path": "请填写存在的 .cst 文件路径，或改用 model_json_path 创建项目。",
            "model_json_path": "请填写存在的模型 JSON 路径，或改用现有 .cst project_path。",
            "fixture_result_dir": "请确认 fixture_result_dir 指向已有结果目录。",
            "cst_root": "请确认本机 CST Studio Suite 安装路径。",
            "cst_python": "请确认 CST 自带 Python 可执行文件存在。",
            "python_cst_libraries": "请确认 CST Python 库目录存在。",
            "e_platform_root": "请确认 E 平台根目录配置正确。",
            "e_cst_local_adapter": "请确认 E 平台 cst_local adapter.py 存在。",
            "e_cst_local_official_runner": "请确认 E 平台 cst_local official.py 存在。",
            "cst_control_script": "请确认 cst-control 脚本路径存在。",
        }
        return hints.get(name, "请修复该路径或配置后重新创建任务。")

    def _run_subprocess(
        self,
        command: list[str],
        stdout_path: Path,
        stderr_path: Path,
        *,
        timeout_seconds: int,
    ) -> None:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(1, timeout_seconds),
            )
        except subprocess.TimeoutExpired as exc:
            self._atomic_write_text(stdout_path, exc.stdout or "")
            self._atomic_write_text(stderr_path, exc.stderr or "")
            raise TimeoutError(f"CST command timed out after {timeout_seconds}s: {stderr_path}") from exc
        self._atomic_write_text(stdout_path, completed.stdout or "")
        self._atomic_write_text(stderr_path, completed.stderr or "")
        if completed.returncode != 0:
            raise RuntimeError(f"CST command failed with exit code {completed.returncode}: {stderr_path}")

    def _register_fixture_run(self, task_id: str, request: dict[str, Any], run_dir: Path) -> dict[str, Any]:
        source = Path(str(request["fixture_result_dir"]))
        manifest = {
            "schema_version": "1.0",
            "operation": "v2_0_real_cst_fixture_result",
            "task_id": task_id,
            "run_dir": str(source),
            "registered_run_dir": str(run_dir),
            "success": True,
            "fixture_result_dir": str(source),
            "created_at": now_iso(),
            "result": {"status": "ok", "output": {}},
        }
        path = run_dir / "run_manifest.json"
        atomic_write_json(path, manifest)
        return {**manifest, "manifest_path": str(path)}

    def _simulate_run(self, task_id: str, request: dict[str, Any], run_dir: Path) -> dict[str, Any]:
        artifact_dir = run_dir / "artifacts" / "simulated_cst"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        freq = [2.0 + index * 0.02 for index in range(151)]
        s11 = [-5.0 - 18.0 * math.exp(-((value - 3.2) / 0.22) ** 2) for value in freq]
        csv_path = artifact_dir / "s11.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Frequency", "S11"])
            writer.writerows(zip(freq, s11))
        manifest = {
            "schema_version": "1.0",
            "operation": "v2_0_simulated_cst_single_run",
            "task_id": task_id,
            "run_dir": str(run_dir),
            "success": True,
            "simulated_execution": True,
            "result": {"status": "ok", "output": {"freq": freq, "s11": s11, "source": "v2_0_simulated_fixture"}},
            "created_at": now_iso(),
        }
        path = run_dir / "run_manifest.json"
        atomic_write_json(path, manifest)
        return {**manifest, "manifest_path": str(path)}

    def _parse_s11_file(self, root: Path) -> dict[str, Any]:
        candidates = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".csv", ".txt", ".dat"}
            and any(token in path.name.lower() for token in ("s11", "s1,1", "return", "loss"))
        ]
        if not candidates:
            raise FileNotFoundError(f"no S11-like result file found under {root}")
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
            pairs = self._read_numeric_pairs(path)
            if pairs:
                return {"source_path": str(path), "freq": [row[0] for row in pairs], "s11": [row[1] for row in pairs]}
        raise ValueError(f"S11-like files exist but no numeric two-column data was found under {root}")

    def _read_numeric_pairs(self, path: Path) -> list[tuple[float, float]]:
        pairs = []
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            row = raw.strip()
            if not row or row[0] in "#!%":
                continue
            parts = row.replace(";", ",").replace("\t", ",").split(",")
            if len(parts) == 1:
                parts = row.split()
            values = []
            for part in parts:
                if not part:
                    continue
                try:
                    values.append(float(part))
                except ValueError:
                    values = []
                    break
            if len(values) >= 2 and math.isfinite(values[0]) and math.isfinite(values[1]):
                pairs.append((float(values[0]), float(values[1])))
        return pairs

    def _numeric_pairs(self, freq: Any, s11: Any) -> list[tuple[float, float]]:
        pairs = []
        for raw_freq, raw_value in zip(freq or [], s11 or []):
            try:
                freq_value = float(raw_freq)
                s11_value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(freq_value) and math.isfinite(s11_value):
                pairs.append((freq_value, s11_value))
        return sorted(pairs, key=lambda item: item[0])

    def _compute_s11_metrics(self, pairs: list[tuple[float, float]], request: dict[str, Any]) -> dict[str, Any]:
        if not pairs:
            return {
                "s11_min_db": None,
                "s11_min_freq": None,
                "return_loss_max_db": None,
                "bandwidth_10db": 0.0,
                "bands_below_10db": [],
            }
        min_freq, min_s11 = min(pairs, key=lambda item: item[1])
        threshold = float(request.get("s11_threshold_db", -10.0))
        bands = self._bands_below_threshold(pairs, threshold)
        return {
            "s11_min_db": float(min_s11),
            "s11_min_freq": float(min_freq),
            "return_loss_max_db": float(-min_s11),
            "bandwidth_10db": float(max((band["width"] for band in bands), default=0.0)),
            "threshold_db": threshold,
            "bands_below_10db": bands,
        }

    def _bands_below_threshold(self, pairs: list[tuple[float, float]], threshold: float) -> list[dict[str, Any]]:
        bands = []
        current: list[tuple[float, float]] = []

        def flush() -> None:
            if not current:
                return
            min_freq, min_s11 = min(current, key=lambda item: item[1])
            start = current[0][0]
            end = current[-1][0]
            bands.append(
                {
                    "start_freq": float(start),
                    "end_freq": float(end),
                    "width": float(max(0.0, end - start)),
                    "min_s11_db": float(min_s11),
                    "min_s11_freq": float(min_freq),
                    "point_count": len(current),
                }
            )
            current.clear()

        for pair in pairs:
            if pair[1] <= threshold:
                current.append(pair)
            else:
                flush()
        flush()
        return bands

    def _collect_artifacts(self, root: Path) -> list[dict[str, Any]]:
        artifacts = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            artifacts.append(
                {
                    "id": stable_hash(str(path))[:16],
                    "name": path.name,
                    "path": str(path),
                    "artifact_type": self._artifact_type(path),
                    "source": "v2_0_real_cst",
                    "size": path.stat().st_size,
                }
            )
        return artifacts

    def _artifact_type(self, path: Path) -> str:
        lower = path.name.lower()
        if lower.endswith(".json"):
            return "json"
        if lower.endswith((".csv", ".txt", ".dat")):
            return "result_export"
        if lower.endswith(".log"):
            return "log"
        if lower.endswith(".cst"):
            return "cst_project"
        return "artifact"

    def _next_step(self, status: str, failure_reason: str | None) -> str:
        if status == "completed":
            return "可以在前端查看 S11、return loss、bandwidth，并决定是否进入后续优化或论文对照。"
        if failure_reason == "preflight_failed":
            return "preflight failed：CST not_reached，Result not_available。请补充真实 .cst project_path 或 model_json_path，并确认参数 JSON 后重新创建任务。"
        if failure_reason:
            return f"先处理失败原因：{failure_reason}。修复后重新创建 V2.0 真实 CST 单次任务。"
        return "检查 CST 路径、项目文件、参数和 S11 导出配置后重新运行。"

    def _paperwise_evidence_lines(self, summary: dict[str, Any]) -> list[str]:
        if not summary:
            return ["- support: insufficient_evidence", "- reason: PaperWise evidence summary is missing"]
        lines = [
            f"- status: {summary.get('status', 'insufficient_evidence')}",
            f"- support_level: {summary.get('support_level', 'insufficient_evidence')}",
            f"- read_only: {summary.get('read_only') is True}",
            "- deep_read_papers purpose: source papers traced from PaperWise vector chunks and deep-reading reports",
            "- graph_library purpose: relation library for innovation judgment and paper/concept/structure/metric relations",
        ]
        sources = summary.get("sources") if isinstance(summary.get("sources"), dict) else {}
        for key in ("deep_read_papers", "graph_library"):
            source = sources.get(key) if isinstance(sources.get(key), dict) else {}
            status = source.get("status", "missing")
            support = source.get("support_level", "insufficient_evidence")
            roles = ", ".join(source.get("roles", []))
            count = source.get("count", source.get("chunk_count", source.get("relation_count", source.get("node_count", 0))))
            lines.append(f"- {key}: status={status}; support={support}; roles={roles}; count={count}")
            if status != "available":
                lines.append(f"  - {key} insufficient_evidence: {source.get('reason', 'source unavailable')}")
            for item in (source.get("items") or [])[:3]:
                lines.append(
                    f"  - {item.get('title') or item.get('source')}: {item.get('path')} "
                    f"| level={item.get('level', 'unknown')} | status={item.get('status', 'unknown')}"
                )
                if item.get("snippet") or item.get("description"):
                    lines.append(f"    - evidence_text: {item.get('snippet') or item.get('description')}")
        return lines

    def _failure_stage(self, failure_reason: str | None) -> str:
        if failure_reason == "preflight_failed":
            return "preflight failed"
        if failure_reason and "result" in failure_reason:
            return "result failed"
        if failure_reason:
            return "cst failed"
        return "none"

    def _atomic_write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            f".{path.name}.{stable_hash({'path': str(path), 'time': now_iso()})[:12]}.tmp"
        )
        try:
            with temp_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
