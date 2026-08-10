from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_runtime.constants import PACKET_TYPES
from agent_runtime.skill_packet_validator import SkillPacketValidator
from agent_runtime.utils import atomic_write_json, now_iso, stable_hash


ADAPTER_BY_PACKET_TYPE = {
    "idea_card": (
        ("antenna-research-idea-advisor", "scripts", "packet_adapter.py"),
        "from-card",
    ),
    "experiment_contract": (
        ("antenna-claim-experiment-planner", "scripts", "packet_adapter.py"),
        "from-idea-packet",
    ),
    "geometry_contract": (
        ("antenna-research-ideation", "scripts", "packet_adapter.py"),
        "export-geometry",
    ),
    "run_manifest": (
        ("cst-control", "scripts", "packet_adapter.py"),
        "from-packet",
    ),
    "claim_assessment": (
        ("antenna-result-to-claim", "scripts", "packet_adapter.py"),
        "from-result-packet",
    ),
    "next_iteration_plan": (
        ("antenna-research-reviewer", "scripts", "packet_adapter.py"),
        "from-input-packet",
    ),
}


class AntennaSkillsAdapter:
    def __init__(self, root: str):
        """初始化当前对象依赖和运行参数。"""
        self.root = Path(root)
        self.packet_validator = SkillPacketValidator()

    def available(self) -> bool:
        """检查本地适配目标是否可用。"""
        return self.root.exists()

    def protocol_summary(self) -> dict[str, object]:
        """返回适配器支持的协议和能力摘要。"""
        return {
            "available": self.available(),
            "root": str(self.root),
            "packet_types": list(PACKET_TYPES),
            "packet_adapters": self.discover_packet_adapters(),
        }

    def discover_packet_adapters(self) -> list[dict[str, object]]:
        """扫描并返回可用的数据包适配器。"""
        adapters = []
        if self.root.exists():
            for packet_type, (adapter_parts, _command) in ADAPTER_BY_PACKET_TYPE.items():
                adapter = self.root.joinpath(*adapter_parts)
                if adapter.is_file():
                    adapters.append({
                        "packet_type": packet_type,
                        "path": str(adapter),
                        "skill_dir": str(adapter.parent),
                        "available": True,
                    })
        if self.root.exists():
            for adapter in self.root.rglob("packet_adapter.py"):
                packet_type = self._infer_packet_type(adapter)
                adapters.append({
                    "packet_type": packet_type,
                    "path": str(adapter),
                    "skill_dir": str(adapter.parent),
                    "available": True,
                })

        by_type = {str(item["packet_type"]): item for item in adapters if item.get("packet_type")}
        for packet_type in PACKET_TYPES:
            by_type.setdefault(packet_type, {
                "packet_type": packet_type,
                "path": None,
                "skill_dir": None,
                "available": False,
            })
        return [by_type[packet_type] for packet_type in PACKET_TYPES]

    def adapt_packet_stage(
        self,
        packet_type: str,
        payload: dict[str, Any],
        artifacts: list[dict[str, Any]],
        output_dir: str | Path,
        source_packet_path: str | Path | None = None,
        timeout_seconds: int = 20,
    ) -> dict[str, Any] | None:
        """把内部数据包阶段转换为天线技能可消费的输入和输出。"""
        output_root = Path(output_dir)
        output_root.mkdir(parents=True, exist_ok=True)
        if packet_type == "result_packet":
            output_path = output_root / f"{packet_type}.skill_packet.json"
            packet = self._make_skill_packet(packet_type, "agent_runtime", payload, artifacts, status="ready")
            self.packet_validator.validate(packet, packet_type)
            atomic_write_json(output_path, packet)
            return {"path": str(output_path), "packet": packet, "adapter": "agent_runtime"}

        adapter_info = ADAPTER_BY_PACKET_TYPE.get(packet_type)
        if adapter_info is None:
            return None
        adapter_parts, command = adapter_info
        adapter_path = self.root.joinpath(*adapter_parts)
        if not adapter_path.is_file():
            return None

        output_path = output_root / f"{packet_type}.skill_packet.json"
        input_path = output_root / f"{packet_type}.adapter_input.json"
        if packet_type == "idea_card":
            atomic_write_json(input_path, self._idea_card_adapter_input(payload))
            args = [sys.executable, str(adapter_path), command, "--idea-card-json", str(input_path), "--status", "ready", "--out", str(output_path)]
        elif packet_type == "geometry_contract":
            args = [
                sys.executable,
                str(adapter_path),
                command,
                "--target-type",
                str(payload.get("target_type") or "paper_reproduction"),
                "--out",
                str(output_path),
            ]
        elif packet_type == "run_manifest":
            source = source_packet_path or self._write_fallback_input(packet_type, payload, artifacts, input_path)
            args = [sys.executable, str(adapter_path), command, "--input-packet", str(source), "--operation", "audit_model", "--out", str(output_path)]
        elif packet_type == "experiment_contract":
            if source_packet_path:
                source = source_packet_path
            else:
                idea_payload = self._idea_card_adapter_input(payload)
                idea_packet = self._make_skill_packet("idea_card", "agent_runtime", idea_payload, artifacts, status="ready")
                atomic_write_json(input_path, idea_packet)
                source = input_path
            args = [sys.executable, str(adapter_path), command, "--input-packet", str(source), "--out", str(output_path)]
        else:
            source = source_packet_path or self._write_fallback_input(packet_type, payload, artifacts, input_path)
            args = [sys.executable, str(adapter_path), command, "--input-packet", str(source), "--out", str(output_path)]

        completed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds, check=False)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "antenna skill adapter failed").strip())
        packet = json.loads(output_path.read_text(encoding="utf-8"))
        self.packet_validator.validate(packet, packet_type)
        self._assert_no_live_cst(packet)
        return {"path": str(output_path), "packet": packet, "adapter": str(adapter_path)}

    def adapt_baseline_ablation_plan(
        self,
        plan: dict[str, Any],
        output_dir: str | Path,
        timeout_seconds: int = 20,
    ) -> dict[str, Any] | None:
        """Adapt a generated baseline/ablation plan through the original skill adapter."""
        adapter_path = self.root / "antenna-baseline-ablation-planner" / "scripts" / "packet_adapter.py"
        if not adapter_path.is_file():
            return None
        output_root = Path(output_dir)
        output_root.mkdir(parents=True, exist_ok=True)
        plan_path = output_root / "baseline_ablation_plan.json"
        output_path = output_root / "run_manifest.skill_packet.json"
        atomic_write_json(plan_path, plan)
        args = [
            sys.executable,
            str(adapter_path),
            "from-plan",
            "--baseline-ablation-plan",
            str(plan_path),
            "--out",
            str(output_path),
        ]
        completed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds, check=False)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "baseline adapter failed").strip())
        packet = json.loads(output_path.read_text(encoding="utf-8"))
        self.packet_validator.validate(packet, "run_manifest")
        self._assert_no_live_cst(packet)
        return {"path": str(output_path), "packet": packet, "adapter": str(adapter_path), "plan_path": str(plan_path)}

    def adapt_claim_assessment(
        self,
        assessment: dict[str, Any],
        output_dir: str | Path,
        timeout_seconds: int = 20,
    ) -> dict[str, Any] | None:
        """Adapt an explicit insufficient/supported assessment through the original skill adapter."""
        adapter_path = self.root / "antenna-result-to-claim" / "scripts" / "packet_adapter.py"
        if not adapter_path.is_file():
            return None
        output_root = Path(output_dir)
        output_root.mkdir(parents=True, exist_ok=True)
        assessment_path = output_root / "claim_assessment.json"
        output_path = output_root / "claim_assessment.skill_packet.json"
        atomic_write_json(assessment_path, assessment)
        args = [
            sys.executable,
            str(adapter_path),
            "from-assessment",
            "--assessment-json",
            str(assessment_path),
            "--out",
            str(output_path),
        ]
        completed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds, check=False)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "claim adapter failed").strip())
        packet = json.loads(output_path.read_text(encoding="utf-8"))
        self.packet_validator.validate(packet, "claim_assessment")
        self._assert_no_live_cst(packet)
        return {"path": str(output_path), "packet": packet, "adapter": str(adapter_path), "assessment_path": str(assessment_path)}

    def _infer_packet_type(self, adapter_path: Path) -> str:
        """根据适配器路径推断数据包类型。"""
        text = str(adapter_path.parent).replace("-", "_").lower()
        for packet_type in PACKET_TYPES:
            if packet_type in text:
                return packet_type
        stem = adapter_path.parent.name.replace("-", "_").lower()
        aliases = {
            "idea": "idea_card",
            "experiment": "experiment_contract",
            "geometry": "geometry_contract",
            "run": "run_manifest",
            "result": "result_packet",
            "claim": "claim_assessment",
            "review": "next_iteration_plan",
        }
        for key, packet_type in aliases.items():
            if key in stem:
                return packet_type
        return stem

    def _write_fallback_input(
        self,
        packet_type: str,
        payload: dict[str, Any],
        artifacts: list[dict[str, Any]],
        input_path: Path,
    ) -> Path:
        """在没有真实适配器时写入兜底输入文件。"""
        packet = self._make_skill_packet(packet_type, "agent_runtime", payload, artifacts)
        atomic_write_json(input_path, packet)
        return input_path

    def _make_skill_packet(
        self,
        packet_type: str,
        created_by: str,
        payload: dict[str, Any],
        artifacts: list[dict[str, Any]],
        status: str = "draft",
    ) -> dict[str, Any]:
        """构造天线技能适配器返回的数据包。"""
        packet = {
            "schema_version": "1.0",
            "packet_type": packet_type,
            "id": stable_hash({"created_by": created_by, "packet_type": packet_type, "payload": payload, "artifacts": artifacts})[:16],
            "created_by": created_by,
            "created_at": now_iso(),
            "status": status,
            "payload": payload,
            "artifacts": artifacts,
            "metadata": {"adapted_by": "antenna_agent_lab", "no_cst_execution": True},
        }
        return packet

    def _idea_card_adapter_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        """生成 idea_card 阶段所需的适配器输入。"""
        parsed_goal = payload.get("parsed_goal", {}) if isinstance(payload.get("parsed_goal"), dict) else {}
        goal = str(payload.get("goal") or "")
        antenna_type = parsed_goal.get("antenna_type") or "antenna"
        target_metric = parsed_goal.get("target_metric") or "S11"
        method = parsed_goal.get("method_hint") or parsed_goal.get("algorithm_family") or "planning"
        return {
            "problem_anchor": goal,
            "improved_idea": goal,
            "research_question": f"How should {antenna_type} be planned for {target_metric} improvement?",
            "hypothesis": f"{method} can be evaluated after evidence and geometry gates are satisfied.",
            "novelty_claim": goal,
            "evidence_status": "partial" if payload.get("paper_report_path") else "weak",
            "candidate_ideas": [goal] if goal else [],
            "risks": ["planning_only_no_cst_execution"],
            "next_best_step": "create experiment_contract",
            "selected_claim": goal,
            "antenna_name": antenna_type,
            "recommended_algorithm": method,
            "parameter_space": {},
            "metrics": [target_metric] if target_metric else [],
            "success_criteria": {target_metric: "requires validated exports before claim support"} if target_metric else {},
            "claim_ceiling": "planning only; no CST execution",
        }

    def _assert_no_live_cst(self, value: Any, path: str = "packet") -> None:
        """递归检查数据中是否包含真实 CST 执行标记。"""
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower()
                child_path = f"{path}.{key}"
                if lowered in {"real_cst_execution", "live_cst", "run_real_cst", "cst_solver_enabled"} and item is True:
                    raise RuntimeError(f"antenna adapter attempted live CST flag: {child_path}")
                if lowered in {"execution_mode", "mode"} and isinstance(item, str) and item.lower() in {"cst", "real_cst", "live_cst"}:
                    raise RuntimeError(f"antenna adapter attempted live CST mode: {child_path}")
                self._assert_no_live_cst(item, child_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._assert_no_live_cst(item, f"{path}[{index}]")
