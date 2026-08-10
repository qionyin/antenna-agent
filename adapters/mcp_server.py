from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from adapters.antenna_skills_adapter import AntennaSkillsAdapter
from adapters.cst_real_run_adapter import CstRealRunAdapter
from adapters.modeling_preparation_adapter import ModelingPreparationAdapter


def _object_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


TOOLS: list[types.Tool] = [
    types.Tool(name="antenna_available", description="Check legacy antenna skills adapter availability.", inputSchema=_object_schema()),
    types.Tool(name="antenna_protocol_summary", description="Return legacy antenna packet protocol summary.", inputSchema=_object_schema()),
    types.Tool(name="antenna_discover_packet_adapters", description="Discover legacy packet adapters.", inputSchema=_object_schema()),
    types.Tool(name="antenna_adapt_packet_stage", description="Adapt one packet stage through legacy antenna skills adapter.", inputSchema=_object_schema()),
    types.Tool(name="antenna_adapt_baseline_ablation_plan", description="Adapt a baseline/ablation plan through legacy antenna skills adapter.", inputSchema=_object_schema()),
    types.Tool(name="antenna_adapt_claim_assessment", description="Adapt a claim assessment through legacy antenna skills adapter.", inputSchema=_object_schema()),
    types.Tool(name="modeling_prepare", description="Prepare traceable modeling artifacts through legacy modeling adapter.", inputSchema=_object_schema()),
    types.Tool(name="modeling_validate_cst_model_spec_artifacts", description="Validate persisted CST model spec artifacts.", inputSchema=_object_schema()),
    types.Tool(name="modeling_recover_missing_evidence", description="Recover missing evidence through legacy modeling adapter.", inputSchema=_object_schema()),
    types.Tool(name="modeling_repair_non_protected_modeling_request", description="Repair non-protected modeling request fields.", inputSchema=_object_schema()),
    types.Tool(name="cst_preflight", description="Run legacy CST preflight.", inputSchema=_object_schema()),
    types.Tool(name="cst_review_preflight", description="Review legacy CST preflight.", inputSchema=_object_schema()),
    types.Tool(name="cst_run_single", description="Run one legacy CST task.", inputSchema=_object_schema()),
    types.Tool(name="cst_review_run", description="Review legacy CST run manifest.", inputSchema=_object_schema()),
    types.Tool(name="cst_parse_results", description="Parse legacy CST run results.", inputSchema=_object_schema()),
    types.Tool(name="cst_review_parsed_results", description="Review parsed CST results.", inputSchema=_object_schema()),
    types.Tool(name="cst_write_report", description="Write legacy CST task report.", inputSchema=_object_schema()),
    types.Tool(name="cst_review_report", description="Review legacy CST report.", inputSchema=_object_schema()),
]


class LegacyAdapterTools:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "antenna_available":
            return self._antenna().available()
        if name == "antenna_protocol_summary":
            return self._antenna().protocol_summary()
        if name == "antenna_discover_packet_adapters":
            return self._antenna().discover_packet_adapters()
        if name == "antenna_adapt_packet_stage":
            return self._antenna().adapt_packet_stage(
                str(arguments["packet_type"]),
                dict(arguments.get("payload") or {}),
                list(arguments.get("artifacts") or []),
                arguments["output_dir"],
                source_packet_path=arguments.get("source_packet_path"),
                timeout_seconds=int(arguments.get("timeout_seconds") or 20),
            )
        if name == "antenna_adapt_baseline_ablation_plan":
            return self._antenna().adapt_baseline_ablation_plan(
                dict(arguments.get("plan") or {}),
                arguments["output_dir"],
                timeout_seconds=int(arguments.get("timeout_seconds") or 20),
            )
        if name == "antenna_adapt_claim_assessment":
            return self._antenna().adapt_claim_assessment(
                dict(arguments.get("assessment") or {}),
                arguments["output_dir"],
                timeout_seconds=int(arguments.get("timeout_seconds") or 20),
            )
        if name == "modeling_prepare":
            return self._modeling().prepare(dict(arguments.get("request") or {}), arguments["output_dir"])
        if name == "modeling_validate_cst_model_spec_artifacts":
            return self._modeling().validate_cst_model_spec_artifacts(arguments["spec_path"], arguments["manifest_path"])
        if name == "modeling_recover_missing_evidence":
            return self._modeling().recover_missing_evidence(
                dict(arguments.get("request") or {}),
                missing_inputs=list(arguments.get("missing_inputs") or []),
                blockers=list(arguments.get("blockers") or []),
            )
        if name == "modeling_repair_non_protected_modeling_request":
            return self._modeling().repair_non_protected_modeling_request(
                dict(arguments.get("request") or {}),
                missing_inputs=list(arguments.get("missing_inputs") or []),
                blockers=list(arguments.get("blockers") or []),
            )
        if name == "cst_preflight":
            return self._cst().preflight(dict(arguments.get("request") or {}))
        if name == "cst_review_preflight":
            return self._cst().review_preflight(dict(arguments.get("preflight") or {}))
        if name == "cst_run_single":
            return self._cst().run_single(str(arguments["task_id"]), dict(arguments.get("request") or {}))
        if name == "cst_review_run":
            return self._cst().review_run(dict(arguments.get("run_manifest") or {}))
        if name == "cst_parse_results":
            return self._cst().parse_results(dict(arguments.get("run_manifest") or {}), dict(arguments.get("request") or {}))
        if name == "cst_review_parsed_results":
            return self._cst().review_parsed_results(dict(arguments.get("parsed_result") or {}), dict(arguments.get("request") or {}))
        if name == "cst_write_report":
            return self._cst().write_report(**dict(arguments))
        if name == "cst_review_report":
            return self._cst().review_report(dict(arguments.get("report") or {}))
        raise KeyError(f"unknown MCP tool: {name}")

    def _antenna(self) -> AntennaSkillsAdapter:
        return AntennaSkillsAdapter(self.args.antenna_skills_root)

    def _modeling(self) -> ModelingPreparationAdapter:
        return ModelingPreparationAdapter(
            skill_root=Path(self.args.modeling_skill_root),
            timeout_seconds=int(self.args.timeout_seconds),
        )

    def _cst(self) -> CstRealRunAdapter:
        return CstRealRunAdapter(
            e_platform_root=self.args.e_platform_root,
            e_results_root=self.args.e_results_root,
            cst_root=self.args.cst_root,
        )


def _result(value: Any, *, is_error: bool = False) -> types.CallToolResult:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    structured = value if isinstance(value, dict) else {"result": value}
    return types.CallToolResult(
        content=[types.TextContent(text=text)],
        structuredContent=structured,
        isError=is_error,
    )


async def serve(args: argparse.Namespace) -> None:
    tools = LegacyAdapterTools(args)

    async def list_tools(_context: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=TOOLS)

    async def call_tool(_context: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            return _result(tools.call(params.name, dict(params.arguments or {})))
        except Exception as exc:
            return _result({"error": f"{exc.__class__.__name__}: {exc}"}, is_error=True)

    server = Server("antenna-agent-lab-legacy-adapters", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="stdio MCP server for antenna_agent_lab legacy adapters")
    parser.add_argument("--antenna-skills-root", default=r"C:\Users\30626\.codex\skills\Antenna Skills")
    parser.add_argument("--modeling-skill-root", default=r"C:\Users\30626\.codex\skills\Antenna Skills\antenna-research-ideation")
    parser.add_argument("--e-platform-root", default=".")
    parser.add_argument("--e-results-root", default=".")
    parser.add_argument("--cst-root", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    return parser.parse_args(argv)


def main() -> int:
    asyncio.run(serve(parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
