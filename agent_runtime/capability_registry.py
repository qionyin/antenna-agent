from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import RISK_LEVELS
from .mcp_client import SyncMCPClient
from .utils import atomic_write_json, ensure_dir, now_iso, stable_hash
from adapters.mcp_wrappers import MCPAntennaSkillsAdapter


@dataclass
class Capability:
    name: str
    version: str = "1.0"
    input_schema_version: str = "1.0"
    output_schema_version: str = "1.0"
    risk_level: str = "low"
    may_call_external: bool = False
    may_write_files: bool = False
    may_delete_files: bool = False
    requires_approval: bool = False
    source: str = "manual"
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """将当前对象转换为可序列化字典。"""
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"invalid risk_level: {self.risk_level}")
        return {
            "name": self.name,
            "version": self.version,
            "input_schema_version": self.input_schema_version,
            "output_schema_version": self.output_schema_version,
            "risk_level": self.risk_level,
            "may_call_external": self.may_call_external,
            "may_write_files": self.may_write_files,
            "may_delete_files": self.may_delete_files,
            "requires_approval": self.requires_approval,
            "source": self.source,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "metadata": self.metadata,
        }


class CapabilityRegistry:
    def __init__(self) -> None:
        """初始化当前对象依赖和运行参数。"""
        self.capabilities: dict[str, Capability] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}

    def register(self, capability: Capability) -> None:
        """注册一个运行能力定义。"""
        self.capabilities[capability.name] = capability

    def scan(self, roots: dict[str, str]) -> dict[str, Capability]:
        """扫描本地根目录并注册可用能力。"""
        antenna_root = roots.get("antenna_skills_root", "manual")
        antenna_adapter = MCPAntennaSkillsAdapter(antenna_root)
        self.register(Capability(
            "skill_packet_protocol",
            source=antenna_root,
            metadata=antenna_adapter.protocol_summary(),
        ))
        for tool in self._discover_mcp_tools(roots):
            self.register(Capability(
                name=f"mcp_tool:{tool['name']}",
                source="adapters/mcp_server.py",
                risk_level=self._infer_risk(str(tool["name"]), "mcp_tool"),
                may_write_files=str(tool["name"]).startswith(("antenna_adapt_", "modeling_prepare", "cst_")),
                may_call_external=False,
                metadata=tool,
            ))
        for adapter in antenna_adapter.discover_packet_adapters():
            packet_type = str(adapter["packet_type"])
            self.register(Capability(
                name=f"antenna_packet_adapter:{packet_type}",
                source=str(adapter.get("path") or antenna_root),
                risk_level="low",
                input_schema={"type": "object", "properties": {"payload": {"type": "object"}}, "required": ["payload"]},
                output_schema={"type": "object", "properties": {"packet_type": {"type": "string"}}},
                metadata=adapter,
            ))
        paperwise_root = roots.get("paperwise_root", "manual")
        self.register(Capability("paperwise_report_reader", source=paperwise_root, may_call_external=False))
        self.register(Capability(
            "paperwise_vector_library_reader",
            source=str(Path(paperwise_root) / "outputs" / ".kb" / "chroma.sqlite3"),
            may_call_external=False,
            metadata={"read_only": True, "roles": ["reproduction", "evidence"]},
        ))
        self.register(Capability(
            "paperwise_graph_library_reader",
            source=str(Path(paperwise_root) / "outputs" / "graph" / "graph.json"),
            may_call_external=False,
            metadata={
                "read_only": True,
                "roles": ["innovation", "relation"],
                "fallback_glob": str(Path(paperwise_root) / "outputs" / "*" / "graph_info.json"),
            },
        ))
        self.register(Capability("ieee_harvester_status", source=roots.get("ieee_harvester_root", "manual"), may_call_external=True, risk_level="medium"))
        self.register(Capability("mock_run_manifest", source="agent_runtime", may_write_files=True))
        e_root = Path(roots.get("e_platform_root", ""))
        if e_root.exists():
            for meta in e_root.rglob("meta.json"):
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                except Exception:
                    continue
                name = data.get("name") or meta.parent.name
                risk = self._infer_risk(name, str(meta.parent))
                schemas = self._extract_schemas(data, meta.parent)
                self.register(Capability(
                    name=name,
                    version=str(data.get("version", "1.0")),
                    input_schema_version=str(schemas.get("input_schema_version", data.get("input_schema_version", "1.0"))),
                    output_schema_version=str(schemas.get("output_schema_version", data.get("output_schema_version", "1.0"))),
                    source=str(meta),
                    risk_level=risk,
                    requires_approval=risk in {"high", "destructive"},
                    may_write_files=risk in {"medium", "high", "destructive"},
                    input_schema=schemas.get("input_schema"),
                    output_schema=schemas.get("output_schema"),
                    metadata={
                        "path": str(meta.parent),
                        "entrypoint": data.get("entrypoint") or data.get("module") or data.get("script"),
                        "schema_sources": schemas.get("schema_sources", []),
                    },
                ))
        self._register_workflows(e_root)
        return self.capabilities

    def _discover_mcp_tools(self, roots: dict[str, str]) -> list[dict[str, Any]]:
        antenna_root = roots.get("antenna_skills_root", "manual")
        server_args = [
            "--antenna-skills-root",
            str(antenna_root),
            "--modeling-skill-root",
            str(Path(antenna_root) / "antenna-research-ideation"),
            "--e-platform-root",
            str(roots.get("e_platform_root", ".")),
            "--e-results-root",
            str(roots.get("e_results_root", ".")),
        ]
        try:
            return SyncMCPClient(server_args=server_args, timeout_seconds=10).list_tools()
        except Exception as exc:
            return [{
                "name": "mcp_discovery_unavailable",
                "description": f"{exc.__class__.__name__}: {exc}",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
                "available": False,
            }]

    def snapshot(self) -> dict[str, Any]:
        """生成当前内存或任务状态快照。"""
        data = {
            "schema_version": "1.0",
            "created_at": now_iso(),
            "capabilities": {name: cap.to_dict() for name, cap in sorted(self.capabilities.items())},
        }
        snapshot_id = stable_hash(data)[:16]
        data["snapshot_id"] = snapshot_id
        self.snapshots[snapshot_id] = data
        return data

    def save_locked_snapshot(self, workspace_root: str | Path) -> dict[str, Any]:
        """保存带锁定信息的能力快照。"""
        snapshot = self.snapshot()
        snapshots_root = ensure_dir(Path(workspace_root) / "capability_snapshots")
        write_lock = snapshots_root / ".snapshot_write.lock"
        lock_fd = os.open(str(write_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
        try:
            snapshot_path = snapshots_root / f"{snapshot['snapshot_id']}.json"
            lock_path = snapshots_root / f"{snapshot['snapshot_id']}.lock.json"
            snapshot = {
                **snapshot,
                "locked": True,
                "snapshot_path": str(snapshot_path),
                "lock_path": str(lock_path),
            }
            if snapshot_path.exists():
                existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
                if existing.get("snapshot_id") != snapshot["snapshot_id"]:
                    raise RuntimeError(f"capability snapshot collision: {snapshot_path}")
            else:
                atomic_write_json(snapshot_path, snapshot)
            atomic_write_json(
                lock_path,
                {
                    "schema_version": "1.0",
                    "snapshot_id": snapshot["snapshot_id"],
                    "locked": True,
                    "snapshot_path": str(snapshot_path),
                    "created_at": now_iso(),
                },
            )
            self.snapshots[snapshot["snapshot_id"]] = snapshot
            return snapshot
        finally:
            if write_lock.exists():
                write_lock.unlink()

    def _infer_risk(self, name: str, path: str) -> str:
        """根据能力名称和路径推断风险等级。"""
        text = f"{name} {path}".lower()
        if "cst" in text or "runner" in text or "collect" in text:
            return "high"
        if "optimizer" in text or "gwo" in text or "train" in text or "dataset" in text:
            return "medium"
        return "low"

    def _extract_schemas(self, meta_data: dict[str, Any], skill_dir: Path) -> dict[str, Any]:
        """从技能元数据和目录中提取输入输出 schema。"""
        result: dict[str, Any] = {"schema_sources": []}
        input_schema = self._first_schema(meta_data, ["input_schema", "inputs", "parameters", "args"])
        output_schema = self._first_schema(meta_data, ["output_schema", "outputs", "returns", "artifacts"])
        if input_schema is not None:
            result["input_schema"] = self._normalize_schema(input_schema)
            result["schema_sources"].append("meta.json:input")
        if output_schema is not None:
            result["output_schema"] = self._normalize_schema(output_schema)
            result["schema_sources"].append("meta.json:output")

        for schema_file in list(skill_dir.glob("*schema*.json")) + list(skill_dir.glob("*schemas*.json")):
            try:
                data = json.loads(schema_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            file_input = self._first_schema(data, ["input_schema", "inputs", "parameters"])
            file_output = self._first_schema(data, ["output_schema", "outputs", "returns"])
            if result.get("input_schema") is None and file_input is not None:
                result["input_schema"] = self._normalize_schema(file_input)
                result["schema_sources"].append(str(schema_file))
            if result.get("output_schema") is None and file_output is not None:
                result["output_schema"] = self._normalize_schema(file_output)
                result["schema_sources"].append(str(schema_file))
        return result

    def _register_workflows(self, e_root: Path) -> None:
        """注册 E 平台中的工作流能力。"""
        workflows_dir = e_root / "workflows"
        if not workflows_dir.exists():
            return
        for workflow in workflows_dir.glob("*.json"):
            try:
                data = json.loads(workflow.read_text(encoding="utf-8"))
            except Exception:
                continue
            name = data.get("name") or workflow.stem
            steps = data.get("steps") or data.get("workflow") or data.get("nodes") or []
            risk = self._infer_risk(name, str(workflow))
            self.register(Capability(
                name=f"workflow:{name}",
                version=str(data.get("version", "1.0")),
                source=str(workflow),
                risk_level=risk,
                requires_approval=risk in {"high", "destructive"},
                input_schema=self._normalize_schema(data.get("input_schema") or data.get("inputs")),
                output_schema=self._normalize_schema(data.get("output_schema") or data.get("outputs")),
                metadata={
                    "path": str(workflow),
                    "step_count": len(steps) if isinstance(steps, list) else 0,
                    "schema_sources": ["workflow"] if data.get("input_schema") or data.get("outputs") else [],
                },
            ))

    def _first_schema(self, data: dict[str, Any], keys: list[str]) -> Any:
        """按优先级返回第一个可用 schema。"""
        for key in keys:
            value = data.get(key)
            if isinstance(value, (dict, list)):
                return value
        return None

    def _normalize_schema(self, schema: Any) -> dict[str, Any] | None:
        """把 schema 数据规范化为字典结构。"""
        if schema is None:
            return None
        if isinstance(schema, dict) and schema.get("type") == "object":
            return schema
        if isinstance(schema, dict):
            properties = {}
            required = []
            for key, value in schema.items():
                if isinstance(value, dict):
                    properties[str(key)] = {
                        "type": value.get("type", "string"),
                        "description": value.get("description") or value.get("help") or "",
                    }
                    if value.get("required"):
                        required.append(str(key))
                else:
                    properties[str(key)] = {"type": type(value).__name__}
            return {"type": "object", "properties": properties, "required": required}
        if isinstance(schema, list):
            properties = {}
            required = []
            for item in schema:
                if not isinstance(item, dict):
                    continue
                key = item.get("name") or item.get("key") or item.get("id")
                if not key:
                    continue
                properties[str(key)] = {
                    "type": item.get("type", "string"),
                    "description": item.get("description") or item.get("help") or "",
                }
                if item.get("required"):
                    required.append(str(key))
            return {"type": "object", "properties": properties, "required": required}
        return None
