from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from agent_runtime.mcp_client import SyncMCPClient


class _MCPAdapterMixin:
    def _client(self, timeout_seconds: int | None = None) -> SyncMCPClient:
        return SyncMCPClient(
            server_args=self._server_args(),
            timeout_seconds=timeout_seconds,
        )


class MCPAntennaSkillsAdapter(_MCPAdapterMixin):
    def __init__(self, root: str):
        self.root = Path(root)

    def _server_args(self) -> list[str]:
        return [
            "--antenna-skills-root",
            str(self.root),
            "--modeling-skill-root",
            str(self.root / "antenna-research-ideation"),
        ]

    def available(self) -> bool:
        return bool(self._client().call_tool("antenna_available"))

    def protocol_summary(self) -> dict[str, object]:
        return dict(self._client().call_tool("antenna_protocol_summary"))

    def discover_packet_adapters(self) -> list[dict[str, object]]:
        return list(self._client().call_tool("antenna_discover_packet_adapters"))

    def adapt_packet_stage(
        self,
        packet_type: str,
        payload: dict[str, Any],
        artifacts: list[dict[str, Any]],
        output_dir: str | Path,
        source_packet_path: str | Path | None = None,
        timeout_seconds: int = 20,
    ) -> dict[str, Any] | None:
        return self._client(timeout_seconds + 5).call_tool(
            "antenna_adapt_packet_stage",
            {
                "packet_type": packet_type,
                "payload": payload,
                "artifacts": artifacts,
                "output_dir": str(output_dir),
                "source_packet_path": str(source_packet_path) if source_packet_path else None,
                "timeout_seconds": timeout_seconds,
            },
        )

    def adapt_baseline_ablation_plan(
        self,
        plan: dict[str, Any],
        output_dir: str | Path,
        timeout_seconds: int = 20,
    ) -> dict[str, Any] | None:
        return self._client(timeout_seconds + 5).call_tool(
            "antenna_adapt_baseline_ablation_plan",
            {"plan": plan, "output_dir": str(output_dir), "timeout_seconds": timeout_seconds},
        )

    def adapt_claim_assessment(
        self,
        assessment: dict[str, Any],
        output_dir: str | Path,
        timeout_seconds: int = 20,
    ) -> dict[str, Any] | None:
        return self._client(timeout_seconds + 5).call_tool(
            "antenna_adapt_claim_assessment",
            {"assessment": assessment, "output_dir": str(output_dir), "timeout_seconds": timeout_seconds},
        )


class MCPModelingPreparationAdapter(_MCPAdapterMixin):
    def __init__(
        self,
        skill_root: str | Path,
        python_executable: str = sys.executable,
        timeout_seconds: int = 30,
    ) -> None:
        self.skill_root = Path(skill_root)
        self.python_executable = python_executable
        self.timeout_seconds = timeout_seconds

    def _server_args(self) -> list[str]:
        root = self.skill_root.parent
        return [
            "--antenna-skills-root",
            str(root),
            "--modeling-skill-root",
            str(self.skill_root),
            "--timeout-seconds",
            str(self.timeout_seconds),
        ]

    def prepare(self, request: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
        return dict(
            self._client(self.timeout_seconds + 10).call_tool(
                "modeling_prepare",
                {"request": request, "output_dir": str(output_dir)},
            )
        )

    def validate_cst_model_spec_artifacts(
        self,
        spec_path: str | Path,
        manifest_path: str | Path,
    ) -> dict[str, Any]:
        return dict(
            self._client().call_tool(
                "modeling_validate_cst_model_spec_artifacts",
                {"spec_path": str(spec_path), "manifest_path": str(manifest_path)},
            )
        )

    def recover_missing_evidence(
        self,
        request: dict[str, Any],
        *,
        missing_inputs: Any = (),
        blockers: Any = (),
    ) -> list[dict[str, Any]]:
        return list(
            self._client(self.timeout_seconds + 10).call_tool(
                "modeling_recover_missing_evidence",
                {"request": request, "missing_inputs": list(missing_inputs), "blockers": list(blockers)},
            )
        )

    def repair_non_protected_modeling_request(
        self,
        request: dict[str, Any],
        *,
        missing_inputs: Any = (),
        blockers: Any = (),
    ) -> dict[str, Any]:
        return dict(
            self._client(self.timeout_seconds + 10).call_tool(
                "modeling_repair_non_protected_modeling_request",
                {"request": request, "missing_inputs": list(missing_inputs), "blockers": list(blockers)},
            )
        )


class MCPCstRealRunAdapter(_MCPAdapterMixin):
    DEFAULT_CST_ROOT = Path(r"D:\Program Files (x86)\CST Studio Suite 2022")
    CST_CONTROL_SCRIPT = Path(r"C:\Users\30626\.codex\skills\Antenna Skills\cst-control\scripts\cst_control.py")

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

    def _server_args(self) -> list[str]:
        return [
            "--e-platform-root",
            str(self.e_platform_root),
            "--e-results-root",
            str(self.e_results_root),
            "--cst-root",
            str(self.cst_root),
        ]

    def preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        return dict(self._client().call_tool("cst_preflight", {"request": request}))

    def review_preflight(self, preflight: dict[str, Any]) -> dict[str, Any]:
        return dict(self._client().call_tool("cst_review_preflight", {"preflight": preflight}))

    def run_single(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        return dict(
            self._client(int(request.get("cst_timeout_seconds", 7200)) + 30).call_tool(
                "cst_run_single",
                {"task_id": task_id, "request": request},
            )
        )

    def review_run(self, run_manifest: dict[str, Any]) -> dict[str, Any]:
        return dict(self._client().call_tool("cst_review_run", {"run_manifest": run_manifest}))

    def parse_results(self, run_manifest: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        return dict(self._client().call_tool("cst_parse_results", {"run_manifest": run_manifest, "request": request}))

    def review_parsed_results(self, parsed_result: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        return dict(self._client().call_tool("cst_review_parsed_results", {"parsed_result": parsed_result, "request": request}))

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
        return dict(
            self._client().call_tool(
                "cst_write_report",
                {
                    "task_id": task_id,
                    "task_title": task_title,
                    "user_input": user_input,
                    "status": status,
                    "run_manifest": run_manifest,
                    "parsed_result": parsed_result,
                    "reviews": reviews,
                    "artifacts": artifacts,
                    "evidence_pool_summary": evidence_pool_summary,
                    "failure_reason": failure_reason,
                    "workspace_root": str(workspace_root),
                },
            )
        )

    def review_report(self, report: dict[str, Any]) -> dict[str, Any]:
        return dict(self._client().call_tool("cst_review_report", {"report": report}))
