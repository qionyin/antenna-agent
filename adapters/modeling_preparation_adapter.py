from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SKILL_ROOT = Path(
    r"C:\Users\30626\.codex\skills\Antenna Skills\antenna-research-ideation"
)
CONFIGURED_SKILL_ROOT = Path(
    os.environ.get("ANTENNA_RESEARCH_IDEATION_SKILL_ROOT", str(DEFAULT_SKILL_ROOT))
)
PROTECTED_SKILL_ROOTS = (
    Path(r"C:\Users\30626\.codex\skills"),
    Path(r"E:\antenna skills"),
    Path(r"E:\antenna skill"),
)
SUPPORTED_TARGETS = {
    "paper_reproduction",
    "candidate_geometry",
    "cst_skeleton",
}
SUPPORTED_FAMILIES = {"microstrip_patch"}
DIRECT_EVIDENCE = {"paper_fact", "source_paper_fact"}
REQUIRED_MICROSTRIP_PARAMETERS = (
    "sub_w",
    "sub_l",
    "h",
    "patch_w",
    "patch_l",
    "feed_w",
    "feed_l",
    "metal_thickness",
)
PARAMETER_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{0,32})\s*(?:=|:|\bis\b|为)\s*"
    r"(-?\d+(?:\.\d+)?)\s*(mm|cm|um)?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
FREQUENCY_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(GHz|MHz|kHz|Hz)", re.IGNORECASE)
PAGE_MARKER_RE = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(?:page\s*[:#]?\s*(\d+)|p\.\s*(\d+)|第\s*(\d+)\s*页)\s*$"
)
MATERIAL_ALIASES = {
    "rogers_5880": ("rogers 5880", "rt/duroid 5880", "duroid 5880"),
    "ro4003c": ("ro4003c", "rogers ro4003c"),
    "fr4": ("fr-4", "fr4"),
    "copper": ("copper", "铜"),
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stable_id(*parts: Any) -> str:
    value = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def _as_cli(value: Any) -> str:
    return str(value)


@dataclass
class SkillCommandRecord:
    stage: str
    script: str
    arguments: list[str]
    returncode: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "script": self.script,
            "arguments": self.arguments,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class ModelingPreparationError(RuntimeError):
    def __init__(
        self,
        code: str,
        stage: str,
        blockers: Iterable[str],
        *,
        missing_inputs: Iterable[str] = (),
        repair_actions: Iterable[str] = (),
        repairable: bool = True,
        partial_artifacts: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.blockers = [str(item) for item in blockers if str(item)]
        self.missing_inputs = [str(item) for item in missing_inputs if str(item)]
        self.repair_actions = [str(item) for item in repair_actions if str(item)]
        self.repairable = repairable
        self.partial_artifacts = dict(partial_artifacts or {})
        super().__init__("; ".join(self.blockers) or code)


@dataclass
class ModelingPreparationAdapter:
    skill_root: Path = CONFIGURED_SKILL_ROOT
    python_executable: str = sys.executable
    timeout_seconds: int = 30
    command_records: list[SkillCommandRecord] = field(default_factory=list, init=False)

    def prepare(self, request: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
        self.command_records = []
        artifacts: dict[str, str] = {}
        output_root: Path | None = None
        output_safe = False
        try:
            self._preflight_skill()
            output_root = self._validate_output_dir(output_dir)
            output_safe = True
            output_root.mkdir(parents=True, exist_ok=True)
            normalized = self._validate_request(request)
            artifacts.update(self._create_objective(normalized, output_root))
            artifacts.update(self._create_evidence(normalized, output_root, artifacts))

            sufficiency = _read_json(Path(artifacts["evidence_sufficiency"]))
            if not sufficiency.get("success"):
                missing = sufficiency.get("missing_fact_types") or []
                raise ModelingPreparationError(
                    "insufficient_evidence",
                    "evidence_sufficiency",
                    [sufficiency.get("reason") or "evidence is insufficient for the requested target"],
                    missing_inputs=[f"evidence:{item}" for item in missing],
                    repair_actions=["add traceable source evidence for each missing fact type"],
                )

            artifacts.update(self._extract_and_resolve_parameters(normalized, output_root, artifacts))
            artifacts.update(self._generate_modeling_artifacts(normalized, output_root, artifacts))
            artifacts.update(self._run_geometry_artifact_gate(output_root, artifacts))
            artifacts.update(self._build_and_validate_sketch(normalized, output_root, artifacts))
            artifacts.update(self._create_cst_model_spec(output_root, artifacts, normalized))
            result = self._result(
                success=True,
                decision="ready_for_cst_modeling",
                stage="cst_model_spec",
                artifacts=artifacts,
                failure=None,
            )
        except ModelingPreparationError as exc:
            artifacts.update(exc.partial_artifacts)
            result = self._result(
                success=False,
                decision="repairable_failure" if exc.repairable else "failed",
                stage=exc.stage,
                artifacts=artifacts,
                failure={
                    "code": exc.code,
                    "repairable": exc.repairable,
                    "blockers": exc.blockers,
                    "missing_inputs": exc.missing_inputs,
                    "repair_actions": exc.repair_actions,
                },
            )
        except Exception as exc:
            result = self._result(
                success=False,
                decision="failed",
                stage="adapter_internal",
                artifacts=artifacts,
                failure={
                    "code": "adapter_internal_error",
                    "repairable": False,
                    "blockers": [f"{exc.__class__.__name__}: {exc}"],
                    "missing_inputs": [],
                    "repair_actions": ["inspect the adapter subprocess trace and correct the integration error"],
                },
            )
        if output_safe and output_root is not None:
            result_path = output_root / "modeling_preparation_result.json"
            result["result_path"] = str(result_path)
            _atomic_write_json(result_path, result)
        return result

    def validate_cst_model_spec_artifacts(
        self,
        spec_path: str | Path,
        manifest_path: str | Path,
    ) -> dict[str, Any]:
        """Revalidate persisted model artifacts before downstream acceptance or replay."""
        findings: list[str] = []
        try:
            spec = _read_json(Path(spec_path))
            manifest = _read_json(Path(manifest_path))
        except Exception as exc:
            return {"valid": False, "findings": [f"artifact JSON unreadable: {exc}"]}
        if spec.get("schema_version") != "1.0":
            findings.append("cst_model_spec.schema_version must be 1.0")
        required_objects = ("units", "frequency_range", "boundaries")
        for field in required_objects:
            if not isinstance(spec.get(field), dict) or not spec.get(field):
                findings.append(f"cst_model_spec.{field} must be a non-empty object")
        objects = spec.get("objects")
        ports = spec.get("ports")
        booleans = spec.get("booleans")
        if not isinstance(objects, list) or not objects:
            findings.append("cst_model_spec.objects must be a non-empty list")
        if not isinstance(ports, list) or not ports:
            findings.append("cst_model_spec.ports must be a non-empty list")
        if not isinstance(booleans, list):
            findings.append("cst_model_spec.booleans must be a list")
        frequency = spec.get("frequency_range") or {}
        try:
            if float(frequency.get("min")) >= float(frequency.get("max")):
                findings.append("cst_model_spec frequency_range must be increasing")
        except (TypeError, ValueError):
            findings.append("cst_model_spec frequency_range must be numeric")
        if manifest.get("schema_version") != "1.0" or not manifest.get("success"):
            findings.append("cst_model_spec manifest must be schema 1.0 and successful")
        summary = manifest.get("summary") or {}
        if isinstance(objects, list) and summary.get("objects_in_cst_spec") != len(objects):
            findings.append("manifest object count does not match cst_model_spec")
        if isinstance(ports, list) and summary.get("ports_in_cst_spec") != len(ports):
            findings.append("manifest port count does not match cst_model_spec")
        return {"valid": not findings, "findings": findings}

    def recover_missing_evidence(
        self,
        request: dict[str, Any],
        *,
        missing_inputs: Iterable[str] = (),
        blockers: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Recover facts only from an existing exact, traceable source locator."""
        evidence = [dict(item) for item in request.get("evidence") or [] if isinstance(item, dict)]
        missing_types = self._missing_fact_types(missing_inputs, blockers)
        present_types = {str(item.get("fact_type") or "") for item in evidence}
        missing_types.update(self._required_fact_types(request) - present_types)
        if not missing_types:
            return []

        locators: list[dict[str, Any]] = []
        locator_keys: set[tuple[str, int, str]] = set()
        for item in evidence:
            if not self._is_exact_traceable_locator(item):
                continue
            source_path = str(item.get("source_path") or "").strip()
            section = str(item.get("section") or "").strip()
            key = (str(Path(source_path).expanduser().resolve(strict=False)).lower(), int(item["page"]), section)
            if key not in locator_keys:
                locator_keys.add(key)
                locators.append(item)

        recovered: list[dict[str, Any]] = []
        for fact_type in sorted(missing_types):
            if fact_type in present_types:
                continue
            for locator_index, locator in enumerate(locators):
                candidate = self._fact_from_traceable_locator(
                    request,
                    locator,
                    fact_type,
                    index=len(evidence) + locator_index,
                )
                if candidate is None:
                    continue
                recovered.append(candidate)
                present_types.add(fact_type)
                break
        return recovered

    def repair_non_protected_modeling_request(
        self,
        request: dict[str, Any],
        *,
        missing_inputs: Iterable[str] = (),
        blockers: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Repair safe request values from verified evidence; never alter protected geometry."""
        repaired = json.loads(json.dumps(request, ensure_ascii=False, default=str))
        signal = " ".join([*(str(item) for item in missing_inputs), *(str(item) for item in blockers)]).lower()
        evidence = [item for item in repaired.get("evidence") or [] if isinstance(item, dict)]
        changed_fields: list[str] = []

        for index, item in enumerate(evidence):
            fact_type = str(item.get("fact_type") or "")
            if fact_type not in {"frequency_range", "parameter"}:
                continue
            if not self._is_exact_traceable_locator(item):
                continue
            try:
                source_path = Path(str(item.get("source_path") or "")).expanduser()
                localized = self._localized_source_text(source_path, item, index)
            except (ModelingPreparationError, OSError, TypeError, ValueError):
                continue

            if fact_type == "frequency_range":
                try:
                    validated = self._validate_evidence_item(item, index)
                except ModelingPreparationError:
                    continue
                values = self._frequency_values_ghz(str(validated["summary"]))
                if len(values) < 2:
                    continue
                lower, upper = sorted(values[:2])
                candidate = {"min": lower, "max": upper, "unit": "GHz"}
                if repaired.get("frequency_range") != candidate:
                    repaired["frequency_range"] = candidate
                    changed_fields.append("frequency_range")
                continue

            if not re.search(r"\bparameter(?:_evidence)?\b|\bdimension\b|参数|尺寸", signal, re.IGNORECASE):
                continue
            source_values = {
                name: value
                for name, value in self._parameter_values(localized).items()
                if name in REQUIRED_MICROSTRIP_PARAMETERS
            }
            current_values = self._parameter_values(str(item.get("summary") or ""))
            if not source_values:
                continue
            requested_names = {
                name for name in source_values
                if re.search(rf"(?:parameter:|parameter_evidence:|\b){re.escape(name)}\b", signal, re.IGNORECASE)
            }
            if not requested_names:
                requested_names = set(source_values)
            merged = dict(current_values)
            for name in requested_names:
                if self._protected_parameter_name(name):
                    continue
                merged[name] = source_values[name]
            summary = " ".join(
                f"{name}={self._format_number(self._dimension_mm(value))}mm"
                for name, value in merged.items()
            )
            candidate = dict(item)
            candidate["summary"] = summary
            try:
                validated = self._validate_evidence_item(candidate, index)
            except ModelingPreparationError:
                continue
            normalized_summary = str(validated["summary"])
            if normalized_summary != str(item.get("summary") or ""):
                item["summary"] = normalized_summary
                changed_fields.append(f"evidence[{index}].summary")

        repaired["evidence"] = evidence
        return {"request": repaired, "changed_fields": changed_fields}

    def _fact_from_traceable_locator(
        self,
        request: dict[str, Any],
        locator: dict[str, Any],
        fact_type: str,
        *,
        index: int,
    ) -> dict[str, Any] | None:
        source_path = Path(str(locator.get("source_path") or "")).expanduser()
        if not self._is_exact_traceable_locator(locator):
            return None
        try:
            localized = self._localized_source_text(source_path, locator, index)
            summary = self._summary_for_fact(fact_type, localized, request)
        except (ModelingPreparationError, OSError, TypeError, ValueError):
            return None
        if not summary:
            return None
        candidate = {
            "id": f"repair-{fact_type}-{_stable_id(source_path.resolve(strict=False), locator.get('page'), locator.get('section'), fact_type)}",
            "fact_type": fact_type,
            "summary": summary,
            "source_path": str(source_path),
            "source_kind": "original_extract",
            "source_tool": str(locator.get("source_tool") or "paperwise_retrieval"),
            "evidence_label": "paper_fact",
            "status": "usable",
            "page": int(locator["page"]),
            "section": str(locator["section"]),
        }
        try:
            validated = self._validate_evidence_item(candidate, index)
        except ModelingPreparationError:
            return None
        candidate["summary"] = str(validated["summary"])
        candidate["quote_or_note"] = str(validated["quote_or_note"])
        return candidate

    def _summary_for_fact(self, fact_type: str, localized: str, request: dict[str, Any]) -> str:
        if fact_type == "parameter":
            values = {
                name: value
                for name, value in self._parameter_values(localized).items()
                if name in REQUIRED_MICROSTRIP_PARAMETERS
            }
            if not values:
                return ""
            return " ".join(
                f"{name}={self._format_number(self._dimension_mm(value))}mm"
                for name, value in values.items()
            )
        if fact_type == "frequency_range":
            values = self._explicit_frequency_range(localized)
            if values is None:
                return ""
            lower, upper = values
            return f"{self._format_number(lower)} GHz to {self._format_number(upper)} GHz"
        aliases = {
            "antenna_type": ("microstrip patch", "microstrip_patch", "微带贴片"),
            "layer_stack": tuple(alias for values in MATERIAL_ALIASES.values() for alias in values),
            "feed": ("microstrip feed", "feed line", "馈线", "微带馈电"),
            "ground": ("ground plane", "ground", "接地板", "地板"),
            "patch": ("rectangular patch", "patch", "矩形贴片", "贴片"),
            "port": ("waveguide port", "port", "波导端口", "端口"),
        }.get(fact_type, ())
        if fact_type == "antenna_type" and not self._family_mentioned(str(request.get("antenna_family") or ""), localized):
            return ""
        return self._supporting_clause(localized, aliases)

    def _supporting_clause(self, text: str, aliases: Iterable[str]) -> str:
        for clause in re.split(r"[\n\r。.!！？]+", text):
            value = clause.strip()
            if value and self._has_any(value, aliases):
                return value
        return ""

    def _explicit_frequency_range(self, text: str) -> tuple[float, float] | None:
        clauses = [value.strip() for value in re.split(r"[\n\r。.!！？]+", text) if value.strip()]
        range_terms = ("frequency range", "modeled range", "simulation range", "频率范围", "仿真频段", "频段")
        for clause in clauses:
            if not self._has_any(clause, range_terms):
                continue
            values = self._frequency_values_ghz(clause)
            if len(values) == 2:
                lower, upper = sorted(values)
                return lower, upper
        values = sorted(set(self._frequency_values_ghz(text)))
        return (values[0], values[1]) if len(values) == 2 else None

    @staticmethod
    def _protected_parameter_name(name: str) -> bool:
        return bool(re.search(r"(?:^|_)(?:port|feed|boundary|topology)(?:_|$)", str(name), re.IGNORECASE))

    @staticmethod
    def _is_exact_traceable_locator(item: dict[str, Any]) -> bool:
        source_path = str(item.get("source_path") or "").strip()
        return bool(
            source_path
            and Path(source_path).expanduser().is_file()
            and item.get("page") is not None
            and str(item.get("section") or "").strip()
            and str(item.get("source_kind") or "") == "original_extract"
            and str(item.get("evidence_label") or "") in DIRECT_EVIDENCE
            and str(item.get("status") or "usable") == "usable"
        )

    @staticmethod
    def _required_fact_types(request: dict[str, Any]) -> set[str]:
        required = {"antenna_type", "layer_stack", "feed", "ground", "patch", "parameter", "frequency_range"}
        if str(request.get("target_type") or "paper_reproduction") == "paper_reproduction":
            required.add("port")
        return required

    @staticmethod
    def _missing_fact_types(missing_inputs: Iterable[str], blockers: Iterable[str]) -> set[str]:
        text = " ".join([*(str(item) for item in missing_inputs), *(str(item) for item in blockers)]).lower()
        aliases = {
            "antenna_type": ("antenna_type", "antenna type", "天线类型"),
            "layer_stack": ("layer_stack", "layer stack", "material", "substrate", "dielectric", "材料", "介质"),
            "feed": ("feed", "馈电", "馈线"),
            "ground": ("ground", "接地", "地板"),
            "patch": ("patch", "贴片"),
            "port": ("port", "端口"),
            "parameter": ("parameter", "dimension", "参数", "尺寸"),
            "frequency_range": ("frequency_range", "frequency range", "frequency", "频段", "频率"),
        }
        return {fact_type for fact_type, terms in aliases.items() if any(term in text for term in terms)}

    def _validate_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ModelingPreparationError("invalid_request", "input_validation", ["request must be a JSON object"])
        objective = str(request.get("objective") or "").strip()
        if not objective:
            raise ModelingPreparationError(
                "missing_objective",
                "input_validation",
                ["objective is required"],
                missing_inputs=["objective"],
                repair_actions=["provide the modeling objective"],
            )
        paper_id = str(request.get("paper_id") or request.get("task_id") or "").strip()
        if not paper_id:
            raise ModelingPreparationError(
                "missing_paper_id",
                "input_validation",
                ["paper_id or task_id is required"],
                missing_inputs=["paper_id"],
                repair_actions=["provide a stable paper or case identifier"],
            )
        target_type = str(request.get("target_type") or "paper_reproduction")
        if target_type not in SUPPORTED_TARGETS:
            raise ModelingPreparationError(
                "unsupported_target_type",
                "input_validation",
                [f"unsupported target_type: {target_type}"],
                repair_actions=[f"choose one of: {', '.join(sorted(SUPPORTED_TARGETS))}"],
            )
        family = str(request.get("antenna_family") or "").strip()
        if not family:
            raise ModelingPreparationError(
                "missing_antenna_family",
                "input_validation",
                ["antenna_family is required"],
                missing_inputs=["antenna_family"],
                repair_actions=["provide the normalized antenna family"],
            )
        evidence = request.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ModelingPreparationError(
                "missing_evidence",
                "input_validation",
                ["evidence must be a non-empty list"],
                missing_inputs=["evidence"],
                repair_actions=["supply recalled evidence records with source paths and locators"],
            )
        normalized = dict(request)
        normalized.update(
            {
                "objective": objective,
                "paper_id": paper_id,
                "target_type": target_type,
                "antenna_family": family,
                "evidence": [self._validate_evidence_item(item, index) for index, item in enumerate(evidence)],
            }
        )
        frequency = request.get("frequency_range") or {}
        if not isinstance(frequency, dict) or frequency.get("min") is None or frequency.get("max") is None:
            raise ModelingPreparationError(
                "missing_frequency_range",
                "input_validation",
                ["frequency_range.min and frequency_range.max are required"],
                missing_inputs=["frequency_range.min", "frequency_range.max"],
                repair_actions=["provide the evidence-backed modeling frequency range"],
            )
        try:
            f_min = float(frequency["min"])
            f_max = float(frequency["max"])
        except (TypeError, ValueError) as exc:
            raise ModelingPreparationError(
                "invalid_frequency_range",
                "input_validation",
                ["frequency range values must be numeric"],
            ) from exc
        if f_min <= 0 or f_max <= f_min:
            raise ModelingPreparationError(
                "invalid_frequency_range",
                "input_validation",
                ["frequency range must satisfy 0 < min < max"],
                repair_actions=["correct the target frequency range"],
            )
        normalized["frequency_range"] = {
            "min": f_min,
            "max": f_max,
            "unit": str(frequency.get("unit") or "GHz"),
        }
        antenna_facts = [item for item in normalized["evidence"] if item["fact_type"] == "antenna_type"]
        if not antenna_facts or not self._family_mentioned(family, " ".join(item["source_excerpt"] for item in antenna_facts)):
            raise ModelingPreparationError(
                "antenna_family_not_supported_by_source",
                "evidence_content_validation",
                [f"localized antenna evidence does not support requested family {family}"],
                repair_actions=["correct antenna_family or bind it to the source passage that names the antenna type"],
            )
        frequency_facts = [item for item in normalized["evidence"] if item["fact_type"] == "frequency_range"]
        expected_frequency = sorted((self._frequency_to_ghz(f_min, normalized["frequency_range"]["unit"]), self._frequency_to_ghz(f_max, normalized["frequency_range"]["unit"])))
        if not frequency_facts or not any(self._contains_frequency_pair(item["source_excerpt"], expected_frequency) for item in frequency_facts):
            raise ModelingPreparationError(
                "frequency_range_not_supported_by_source",
                "evidence_content_validation",
                ["requested frequency_range does not match the localized source evidence"],
                repair_actions=["correct the frequency range or locator using the original source"],
            )
        return normalized

    def _validate_evidence_item(self, item: Any, index: int) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ModelingPreparationError("invalid_evidence", "input_validation", [f"evidence[{index}] must be an object"])
        fact_type = str(item.get("fact_type") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not fact_type or not summary:
            raise ModelingPreparationError(
                "invalid_evidence",
                "input_validation",
                [f"evidence[{index}] requires fact_type and summary"],
                missing_inputs=[f"evidence[{index}].fact_type", f"evidence[{index}].summary"],
            )
        value = dict(item)
        value["fact_type"] = fact_type
        value["summary"] = summary
        value["original_summary"] = summary
        value["evidence_label"] = str(item.get("evidence_label") or "unknown")
        value["status"] = str(item.get("status") or "usable")
        value["source_kind"] = str(item.get("source_kind") or "retrieval_chunk")
        value["source_tool"] = str(item.get("source_tool") or "paperwise_retrieval")
        source_path = Path(str(item.get("source_path") or "")).expanduser() if item.get("source_path") else None
        value["source_path"] = source_path
        has_locator = item.get("page") is not None or any(item.get(key) for key in ("figure", "table", "caption", "section"))
        if value["evidence_label"] in DIRECT_EVIDENCE:
            if value["source_kind"] != "original_extract" or source_path is None or not source_path.is_file() or not has_locator:
                raise ModelingPreparationError(
                    "untraceable_direct_evidence",
                    "input_validation",
                    [f"evidence[{index}] labels a fact as direct without an original source file and locator"],
                    missing_inputs=[f"evidence[{index}].source_path", f"evidence[{index}].page_or_locator"],
                    repair_actions=["trace the retrieval hit to an original PDF extract/table/figure and provide its locator"],
                )
        elif source_path is not None and not source_path.is_file():
            raise ModelingPreparationError(
                "missing_evidence_source",
                "input_validation",
                [f"evidence source does not exist: {source_path}"],
                repair_actions=["restore the source artifact or remove the stale evidence record"],
            )
        if source_path is not None:
            localized = self._localized_source_text(source_path, item, index)
            value["source_excerpt"] = localized[:4000]
            supplied_quote = str(item.get("quote_or_note") or "").strip()
            if supplied_quote and self._normalize_text(supplied_quote) not in self._normalize_text(localized):
                raise ModelingPreparationError(
                    "quote_not_found_at_locator",
                    "evidence_content_validation",
                    [f"evidence[{index}] quote_or_note is not present at the declared locator"],
                    repair_actions=["use an exact source quote from the located page/section"],
                )
            value["quote_or_note"] = supplied_quote or self._source_excerpt(localized)
            value["summary"] = self._verify_fact_support(value, localized, index)
        else:
            value["source_excerpt"] = ""
            value["quote_or_note"] = str(item.get("quote_or_note") or summary)
        return value

    def _localized_source_text(self, source_path: Path, item: dict[str, Any], index: int) -> str:
        if source_path.suffix.lower() not in {".md", ".txt", ".json"}:
            raise ModelingPreparationError(
                "unsupported_evidence_source_format",
                "evidence_content_validation",
                [f"evidence[{index}] requires a readable text/JSON extract, not {source_path.suffix}"],
                repair_actions=["trace the source through the PDF skill to a locator-preserving .md extract"],
            )
        try:
            text = source_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ModelingPreparationError(
                "unreadable_evidence_source",
                "evidence_content_validation",
                [f"cannot read evidence source as UTF-8: {source_path}: {exc}"],
            ) from exc
        if not text.strip():
            raise ModelingPreparationError("empty_evidence_source", "evidence_content_validation", [f"evidence source is empty: {source_path}"])
        scope = text
        if item.get("page") is not None:
            page = int(item["page"])
            markers = list(PAGE_MARKER_RE.finditer(text))
            selected = None
            for marker_index, marker in enumerate(markers):
                number = next((int(group) for group in marker.groups() if group is not None), None)
                if number == page:
                    end = markers[marker_index + 1].start() if marker_index + 1 < len(markers) else len(text)
                    selected = text[marker.start():end]
                    break
            if selected is None:
                raise ModelingPreparationError(
                    "locator_not_found",
                    "evidence_content_validation",
                    [f"evidence[{index}] page locator {page} is not present in {source_path.name}"],
                    repair_actions=["correct the page locator or regenerate the page-preserving source extract"],
                )
            scope = selected
        section = str(item.get("section") or "").strip()
        if section:
            scope = self._section_scope(scope, section, index)
        for key in ("figure", "table", "caption"):
            locator = str(item.get(key) or "").strip()
            if locator and self._normalize_text(locator) not in self._normalize_text(scope):
                raise ModelingPreparationError(
                    "locator_not_found",
                    "evidence_content_validation",
                    [f"evidence[{index}] {key} locator '{locator}' is not present in the localized source"],
                    repair_actions=["correct the locator using the original extract"],
                )
        return scope

    def _section_scope(self, text: str, section: str, index: int) -> str:
        headings = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", text))
        matches: list[tuple[int, re.Match[str]]] = []
        for heading_index, heading in enumerate(headings):
            if self._normalize_heading(section) == self._normalize_heading(heading.group(2)):
                matches.append((heading_index, heading))
        if not matches:
            raise ModelingPreparationError(
                "locator_not_found",
                "evidence_content_validation",
                [f"evidence[{index}] exact section heading '{section}' is not present in the localized page"],
                repair_actions=["use the normalized exact Markdown heading together with its page locator"],
            )
        if len(matches) != 1:
            raise ModelingPreparationError(
                "ambiguous_section_locator",
                "evidence_content_validation",
                [f"evidence[{index}] section heading '{section}' occurs {len(matches)} times on the localized page"],
                repair_actions=["regenerate the extract with unique headings or add a more specific exact heading"],
            )
        heading_index, heading = matches[0]
        level = len(heading.group(1))
        end = len(text)
        for later in headings[heading_index + 1:]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        return text[heading.start():end]

    def _normalize_heading(self, value: str) -> str:
        normalized = self._normalize_text(value)
        return re.sub(r"[\s\-_:：]+", " ", normalized).strip()

    def _verify_fact_support(self, item: dict[str, Any], localized: str, index: int) -> str:
        fact_type = item["fact_type"]
        summary = item["summary"]
        if fact_type == "parameter":
            conflicts = self._parameter_conflicts(localized)
            if conflicts:
                raise ModelingPreparationError(
                    "conflicting_source_values",
                    "evidence_content_validation",
                    [f"localized source contains conflicting values for: {', '.join(conflicts)}"],
                    missing_inputs=[f"parameter:{name}" for name in conflicts],
                    repair_actions=["resolve the source-table/figure conflict before parameter extraction"],
                )
            summary_parameters = self._parameter_values(summary)
            source_parameters = self._parameter_values(localized)
            if not summary_parameters:
                raise ModelingPreparationError(
                    "parameter_fact_unparseable",
                    "evidence_content_validation",
                    [f"evidence[{index}] parameter fact contains no parseable name/value pairs"],
                )
            mismatches = [
                name for name, value in summary_parameters.items()
                if name not in source_parameters or not self._same_dimension(value, source_parameters[name])
            ]
            if mismatches:
                raise ModelingPreparationError(
                    "fact_not_supported_by_source",
                    "evidence_content_validation",
                    [f"localized source does not support parameter values: {', '.join(mismatches)}"],
                    repair_actions=["correct the parameter fact or bind it to the source passage containing those exact values"],
                )
            return " ".join(
                f"{name}={self._format_number(self._dimension_mm(value))}mm"
                for name, value in summary_parameters.items()
            )
        if fact_type == "frequency_range":
            summary_values = self._frequency_values_ghz(summary)
            if len(summary_values) < 2 or not self._contains_frequency_pair(localized, sorted(summary_values[:2])):
                raise ModelingPreparationError(
                    "fact_not_supported_by_source",
                    "evidence_content_validation",
                    ["localized source does not support the declared frequency range"],
                    repair_actions=["correct the frequency fact or its page/section locator"],
                )
            return summary
        if fact_type == "layer_stack":
            summary_materials = self._materials_in(summary)
            source_materials = self._materials_in(localized)
            if not summary_materials or not summary_materials.issubset(source_materials):
                raise ModelingPreparationError(
                    "fact_not_supported_by_source",
                    "evidence_content_validation",
                    ["localized source does not support the declared layer materials"],
                    repair_actions=["bind the layer fact to text/table content that names the materials"],
                )
            return summary
        if fact_type == "antenna_type":
            families = [family for family in ("microstrip_patch", "printed_monopole", "slot_antenna") if self._family_mentioned(family, summary)]
            if not families or not any(self._family_mentioned(family, localized) for family in families):
                raise ModelingPreparationError("fact_not_supported_by_source", "evidence_content_validation", ["localized source does not support the antenna type"])
            return summary
        anchors = {
            "feed": (("microstrip feed", "feed line", "馈线", "微带馈电"),),
            "ground": (("ground plane", "ground", "接地板", "地板"),),
            "patch": (("rectangular patch", "patch", "矩形贴片", "贴片"),),
            "port": (("waveguide port", "port", "波导端口", "端口"),),
        }
        groups = anchors.get(fact_type)
        if groups and not all(self._has_any(summary, group) and self._has_any(localized, group) for group in groups):
            raise ModelingPreparationError(
                "fact_not_supported_by_source",
                "evidence_content_validation",
                [f"localized source does not support {fact_type} fact semantics"],
                repair_actions=["correct the fact or bind it to a source passage with the required structure terms"],
            )
        if fact_type == "port":
            summary_parameters = self._parameter_values(summary)
            source_parameters = self._parameter_values(localized)
            required = {"port_xmin", "port_xmax", "port_y", "port_zmin", "port_zmax"}
            mismatches = [
                name for name in required
                if name not in summary_parameters or name not in source_parameters or not self._same_dimension(summary_parameters[name], source_parameters[name])
            ]
            orientation = self._port_orientation(summary)
            if mismatches or not orientation or orientation != self._port_orientation(localized):
                raise ModelingPreparationError(
                    "fact_not_supported_by_source",
                    "evidence_content_validation",
                    ["localized source does not support the port orientation and exact face ranges"],
                    missing_inputs=[*sorted(mismatches), "port_orientation" if not orientation else ""],
                    repair_actions=["provide an exact source-backed port orientation and x/z face ranges"],
                )
        return summary

    def _parameter_values(self, text: str) -> dict[str, tuple[float, str]]:
        values: dict[str, tuple[float, str]] = {}
        for match in PARAMETER_TEXT_RE.finditer(text):
            values[match.group(1).lower()] = (float(match.group(2)), (match.group(3) or "mm").lower())
        return values

    def _parameter_conflicts(self, text: str) -> list[str]:
        occurrences: dict[str, list[tuple[float, str]]] = {}
        for match in PARAMETER_TEXT_RE.finditer(text):
            occurrences.setdefault(match.group(1).lower(), []).append(
                (float(match.group(2)), (match.group(3) or "mm").lower())
            )
        return sorted(
            name for name, values in occurrences.items()
            if any(not self._same_dimension(values[0], value) for value in values[1:])
        )

    def _same_dimension(self, left: tuple[float, str], right: tuple[float, str]) -> bool:
        scale = {"um": 0.001, "mm": 1.0, "cm": 10.0}
        return abs(left[0] * scale[left[1]] - right[0] * scale[right[1]]) <= 1e-6

    def _dimension_mm(self, value: tuple[float, str]) -> float:
        return value[0] * {"um": 0.001, "mm": 1.0, "cm": 10.0}[value[1]]

    def _frequency_values_ghz(self, text: str) -> list[float]:
        return [self._frequency_to_ghz(float(match.group(1)), match.group(2)) for match in FREQUENCY_RE.finditer(text)]

    def _frequency_to_ghz(self, value: float, unit: str) -> float:
        scale = {"ghz": 1.0, "mhz": 1e-3, "khz": 1e-6, "hz": 1e-9}
        key = str(unit).lower()
        if key not in scale:
            raise ModelingPreparationError("unsupported_frequency_unit", "evidence_content_validation", [f"unsupported frequency unit: {unit}"])
        return value * scale[key]

    def _contains_frequency_pair(self, text: str, expected: list[float]) -> bool:
        actual = self._frequency_values_ghz(text)
        return all(any(abs(value - candidate) <= 1e-9 for candidate in actual) for value in expected)

    def _materials_in(self, text: str) -> set[str]:
        return {name for name, aliases in MATERIAL_ALIASES.items() if self._has_any(text, aliases)}

    def _family_mentioned(self, family: str, text: str) -> bool:
        aliases = {
            "microstrip_patch": ("microstrip patch", "microstrip_patch", "微带贴片"),
            "printed_monopole": ("printed monopole", "printed_monopole", "印刷单极子"),
            "slot_antenna": ("slot antenna", "slot_antenna", "缝隙天线"),
        }.get(family, (family.replace("_", " "), family))
        return self._has_any(text, aliases)

    def _port_orientation(self, text: str) -> str:
        match = re.search(r"port_orientation\s*(?:=|:|\bis\b|为)\s*(xmin|xmax|ymin|ymax|zmin|zmax)", text, re.IGNORECASE)
        return match.group(1).lower() if match else ""

    def _has_any(self, text: str, values: Iterable[str]) -> bool:
        return any(self._has_positive_mention(text, value) for value in values)

    def _has_positive_mention(self, text: str, value: str) -> bool:
        alias = self._normalize_text(value)
        if not alias:
            return False
        clauses = re.split(
            r"[\n\r,，;；。.!！?？]+|\bbut\b|\binstead\b|\bhowever\b|但是|但|而是|然而",
            str(text),
            flags=re.IGNORECASE,
        )
        for clause in clauses:
            normalized = self._normalize_text(clause)
            start = 0
            while True:
                position = normalized.find(alias, start)
                if position < 0:
                    break
                if not self._mention_is_negated(normalized, position, position + len(alias)):
                    return True
                start = position + len(alias)
        return False

    def _mention_is_negated(self, clause: str, start: int, end: int) -> bool:
        before = clause[max(0, start - 90):start]
        after = clause[end:end + 60]
        before_patterns = (
            r"(?:does|do|did)\s+not\s+(?:use|employ|adopt|contain|include)\s+(?:(?:a|an|the|any)\s+)?(?:[a-z0-9_/-]+\s+){0,5}$",
            r"(?:is|are|was|were)\s+not\s+(?:using|employing|adopting|containing|including)\s+(?:(?:a|an|the|any)\s+)?(?:[a-z0-9_/-]+\s+){0,4}$",
            r"(?:without|no)\s+(?:(?:a|an|the|any)\s+)?(?:[a-z0-9_/-]+\s+){0,5}$",
            r"(?<!not\s)\bnot\s+(?!only\b)(?:(?:a|an|the|any)\s+)?(?:[a-z0-9_/-]+\s+){0,4}$",
            r"(?:未采用|不采用|不使用|没有使用|未使用|无|没有)(?:任何)?[^，,；;。.!！？?]{0,24}$",
        )
        after_patterns = (
            r"^\s*(?:is|are|was|were)?\s*not\s+(?:used|employed|adopted|present|included)",
            r"^\s*(?:未采用|不采用|不使用|未使用|不存在|没有使用)",
        )
        return any(re.search(pattern, before, re.IGNORECASE) for pattern in before_patterns) or any(
            re.search(pattern, after, re.IGNORECASE) for pattern in after_patterns
        )

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value).strip().lower())

    def _source_excerpt(self, value: str) -> str:
        return " ".join(line.strip() for line in value.splitlines() if line.strip())[:1000]

    def _format_number(self, value: float) -> str:
        return str(int(value)) if value.is_integer() else format(value, ".12g")

    def _preflight_skill(self) -> None:
        try:
            configured_root = CONFIGURED_SKILL_ROOT.resolve(strict=True)
            requested_root = self.skill_root.resolve(strict=True)
        except OSError as exc:
            raise ModelingPreparationError(
                "skill_preflight_failed",
                "preflight",
                [f"configured or requested skill root is unavailable: {exc}"],
                repair_actions=["restore the configured read-only antenna-research-ideation skill root"],
                repairable=False,
            ) from exc
        if requested_root != configured_root:
            raise ModelingPreparationError(
                "untrusted_skill_root",
                "preflight",
                [f"requested skill root is not the configured trusted root: {requested_root}"],
                repair_actions=["use ANTENNA_RESEARCH_IDEATION_SKILL_ROOT or the configured default root"],
                repairable=False,
            )
        self.skill_root = requested_root
        if self.skill_root.name != "antenna-research-ideation" or not (self.skill_root / "SKILL.md").is_file():
            raise ModelingPreparationError(
                "invalid_trusted_skill_root",
                "preflight",
                ["trusted root does not contain the antenna-research-ideation skill contract"],
                repairable=False,
            )
        required = (
            "init_objective_contract.py",
            "evidence_ledger.py",
            "modeling_artifact_seed.py",
            "parameter_resolution.py",
            "geometry_artifact_gate.py",
            "fill_geometry_template.py",
            "validate_geometry_sketch.py",
            "sketch_to_cst_spec.py",
        )
        missing = [name for name in required if not (self.skill_root / "scripts" / name).is_file()]
        if missing:
            raise ModelingPreparationError(
                "skill_preflight_failed",
                "preflight",
                [f"missing original skill scripts: {', '.join(missing)}"],
                repair_actions=["restore or configure the read-only antenna-research-ideation skill root"],
                repairable=False,
            )

    def _validate_output_dir(self, output_dir: str | Path) -> Path:
        output_root = Path(output_dir).expanduser().resolve(strict=False)
        protected = [root.resolve(strict=False) for root in PROTECTED_SKILL_ROOTS]
        protected.append(self.skill_root.resolve(strict=True))
        for root in protected:
            if output_root == root or output_root.is_relative_to(root):
                raise ModelingPreparationError(
                    "unsafe_output_dir",
                    "output_validation",
                    [f"output_dir is inside a protected skill root: {root}"],
                    repair_actions=["choose a project work/task output directory outside all skill roots"],
                    repairable=False,
                )
        return output_root

    def _run_skill(self, stage: str, script_name: str, arguments: list[Any], allowed_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
        script = self.skill_root / "scripts" / script_name
        command = [self.python_executable, str(script), *[_as_cli(value) for value in arguments]]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
        )
        record = SkillCommandRecord(
            stage=stage,
            script=str(script),
            arguments=command[2:],
            returncode=completed.returncode,
            stdout=completed.stdout.strip(),
            stderr=completed.stderr.strip(),
        )
        self.command_records.append(record)
        if completed.returncode not in allowed_codes:
            reason = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise ModelingPreparationError(
                "skill_subprocess_failed",
                stage,
                [f"{script_name}: {reason}"],
                repair_actions=["inspect the subprocess trace and correct its input artifact"],
            )
        return completed

    def _create_objective(self, request: dict[str, Any], output_root: Path) -> dict[str, str]:
        path = output_root / "objective_contract.json"
        self._run_skill(
            "objective_contract",
            "init_objective_contract.py",
            [
                "--out", path,
                "--request", request["objective"],
                "--target-type", request["target_type"],
                "--paper-or-case-id", request["paper_id"],
                "--primary-goal", request["objective"],
                "--required-evidence", "traceable geometry, feed, layer, parameter, and frequency evidence",
                "--required-output", "validated cst_model_spec.json without CST execution",
                "--success-definition", "all local evidence, parameter, geometry, and conversion gates pass",
                "--stop-condition", "stop before any CST process or solver invocation",
                "--next-phase", "cst_modeling_preflight",
            ],
        )
        return {"objective_contract": str(path)}

    def _create_evidence(self, request: dict[str, Any], output_root: Path, artifacts: dict[str, str]) -> dict[str, str]:
        ledger = output_root / "evidence_ledger.json"
        packet = output_root / "evidence_packet.json"
        sufficiency = output_root / "evidence_sufficiency.json"
        self._run_skill(
            "evidence_ledger_init",
            "evidence_ledger.py",
            ["init", "--ledger", ledger, "--paper-id", request["paper_id"], "--objective-contract", artifacts["objective_contract"]],
        )
        artifact_ids: dict[str, str] = {}
        for item in request["evidence"]:
            source_path: Path | None = item.get("source_path")
            if source_path is None:
                continue
            key = str(source_path.resolve()).lower()
            if key in artifact_ids:
                continue
            artifact_id = _stable_id("evidence-artifact", source_path.resolve())
            artifact_ids[key] = artifact_id
            source_tool = "pdf_skill" if item.get("source_kind") == "original_extract" and source_path.suffix.lower() == ".md" else item["source_tool"]
            args: list[Any] = [
                "add-artifact", "--ledger", ledger,
                "--id", artifact_id,
                "--artifact-type", str(item.get("artifact_type") or "source_extract"),
                "--path", source_path,
                "--source-tool", source_tool,
                "--description", str(item.get("source_description") or "retrieved evidence source"),
            ]
            if item.get("page") is not None:
                args.extend(["--page", item["page"]])
            self._run_skill("evidence_add_artifact", "evidence_ledger.py", args)

        for index, item in enumerate(request["evidence"]):
            source_path: Path | None = item.get("source_path")
            artifact_id = artifact_ids.get(str(source_path.resolve()).lower(), "") if source_path else ""
            fact_id = str(item.get("id") or _stable_id("evidence-fact", request["paper_id"], index, item["fact_type"], item["summary"]))
            item["resolved_fact_id"] = fact_id
            args = [
                "add-fact", "--ledger", ledger,
                "--id", fact_id,
                "--fact-type", item["fact_type"],
                "--summary", item["summary"],
                "--evidence-label", item["evidence_label"],
                "--status", item["status"],
                "--quote-or-note", item["quote_or_note"],
            ]
            if artifact_id:
                args.extend(["--artifact-id", artifact_id])
                item["resolved_artifact_id"] = artifact_id
            for key in ("page", "figure", "table", "caption", "section"):
                if item.get(key) not in (None, ""):
                    args.extend([f"--{key.replace('_', '-')}", item[key]])
            self._run_skill("evidence_add_fact", "evidence_ledger.py", args)

        self._run_skill(
            "evidence_sufficiency",
            "evidence_ledger.py",
            ["check-sufficiency", "--ledger", ledger, "--target-type", request["target_type"], "--out", sufficiency],
            allowed_codes=(0, 2),
        )
        self._run_skill(
            "evidence_packet",
            "evidence_ledger.py",
            ["export-packet", "--ledger", ledger, "--out", packet, "--target-type", request["target_type"]],
        )
        return {
            "evidence_ledger": str(ledger),
            "evidence_sufficiency": str(sufficiency),
            "evidence_packet": str(packet),
        }

    def _extract_and_resolve_parameters(self, request: dict[str, Any], output_root: Path, artifacts: dict[str, str]) -> dict[str, str]:
        seed_dir = output_root / "modeling_seed"
        self._run_skill(
            "parameter_extract",
            "modeling_artifact_seed.py",
            [
                "--evidence-packet", artifacts["evidence_packet"],
                "--out-dir", seed_dir,
                "--antenna-family", request["antenna_family"],
                "--stop-after-parameters",
            ],
            allowed_codes=(0, 2),
        )
        parameters_path = seed_dir / "parameters.json"
        seed_manifest = seed_dir / "paper_modeling_manifest.json"
        if not parameters_path.is_file() or not seed_manifest.is_file():
            raise ModelingPreparationError(
                "parameter_extract_missing_output",
                "parameter_extract",
                ["original skill did not write parameters.json and its manifest"],
            )
        parameter_artifacts = {
            "parameters": str(parameters_path),
            "parameter_seed_manifest": str(seed_manifest),
        }
        parameters = _read_json(parameters_path)
        missing = [str(item.get("name")) for item in parameters.get("items", []) if item.get("value") is None]
        ambiguous = [str(item.get("name")) for item in parameters.get("items", []) if item.get("status") == "ambiguous_candidate"]
        if missing or ambiguous:
            protected_missing = [name for name in missing + ambiguous if self._protected_parameter_name(name)]
            protected_notice = (
                f"protected feed/port/boundary/topology parameters require approval: {', '.join(protected_missing)}"
                if protected_missing
                else ""
            )
            raise ModelingPreparationError(
                "unresolved_parameters",
                "parameter_extract",
                [
                    f"missing parameters: {', '.join(missing)}" if missing else "",
                    f"ambiguous parameters: {', '.join(ambiguous)}" if ambiguous else "",
                    protected_notice,
                ],
                missing_inputs=[f"parameter:{name}" for name in sorted(set(missing + ambiguous))],
                repair_actions=["supply one traceable numeric paper fact for each missing or conflicting parameter"],
                partial_artifacts=parameter_artifacts,
            )
        if request["antenna_family"] not in SUPPORTED_FAMILIES:
            raise ModelingPreparationError(
                "unsupported_family",
                "parameter_extract",
                [f"strict geometry construction is not implemented for {request['antenna_family']}"],
                repair_actions=["add a family-specific evidence-to-geometry mapping before enabling this family"],
                partial_artifacts=parameter_artifacts,
            )

        packet = _read_json(Path(artifacts["evidence_packet"]))
        facts = [item for item in packet.get("facts", []) if isinstance(item, dict)]
        resolution = output_root / "parameter_resolution.json"
        completeness = output_root / "parameter_completeness.json"
        review = output_root / "parameter_review.md"
        self._run_skill(
            "parameter_resolution_init",
            "parameter_resolution.py",
            [
                "init", "--resolution", resolution,
                "--paper-id", request["paper_id"],
                "--target-type", request["target_type"],
                "--evidence-packet", artifacts["evidence_packet"],
            ],
        )
        for item in parameters.get("items", []):
            name = str(item.get("name") or "")
            fact = self._parameter_fact(name, facts)
            if not fact:
                raise ModelingPreparationError(
                    "unbound_parameter_evidence",
                    "parameter_resolution",
                    [f"parameter {name} has a value but no unique evidence fact"],
                    missing_inputs=[f"parameter_evidence:{name}"],
                    repair_actions=[f"add a parameter fact containing {name}=<value><unit> with a source locator"],
                    partial_artifacts={**parameter_artifacts, "parameter_resolution": str(resolution)},
                )
            source = fact.get("source") or {}
            args: list[Any] = [
                "add-parameter", "--resolution", resolution,
                "--name", name,
                "--value", item["value"],
                "--unit", item.get("unit") or "mm",
                "--status", "resolved",
                "--evidence-fact-id", fact["id"],
                "--artifact-id", fact.get("artifact_id") or "",
                "--next-action", "none",
                "--critical",
                "--notes", "Resolved from a traceable evidence fact by the modeling preparation adapter.",
            ]
            if fact.get("page") is not None:
                args.extend(["--page", fact["page"]])
            locator = fact.get("locator") or {}
            for key in ("figure", "table", "section"):
                if locator.get(key):
                    args.extend([f"--{key}", locator[key]])
            self._run_skill("parameter_resolution_add", "parameter_resolution.py", args)
            mapping = self._parameter_mapping(name)
            map_args: list[Any] = [
                "map-parameter", "--resolution", resolution,
                "--parameter", name,
                "--target-object", mapping["target_object"],
                "--operation", mapping["operation"],
                "--axis-or-edge", mapping["axis_or_edge"],
                "--evidence-fact-id", fact["id"],
                "--confidence", "direct",
                "--critical",
                "--notes", "Deterministic microstrip-patch parameter ownership.",
            ]
            for control in mapping["controls"]:
                map_args.extend(["--control", control])
            self._run_skill("parameter_resolution_map", "parameter_resolution.py", map_args)

        self._run_skill(
            "parameter_completeness",
            "parameter_resolution.py",
            ["check-completeness", "--resolution", resolution, "--manual-review", "auto", "--out", completeness],
            allowed_codes=(0, 2),
        )
        completeness_data = _read_json(completeness)
        if not completeness_data.get("success"):
            raise ModelingPreparationError(
                "parameter_completeness_failed",
                "parameter_completeness",
                ["parameter resolution did not pass the original skill completeness gate"],
                missing_inputs=(completeness_data.get("missing_parameters") or []) + (completeness_data.get("missing_parameter_object_maps") or []),
                repair_actions=["resolve missing parameters and parameter-to-object mappings, then rerun from parameter resolution"],
                partial_artifacts={
                    **parameter_artifacts,
                    "parameter_resolution": str(resolution),
                    "parameter_completeness": str(completeness),
                },
            )
        self._run_skill(
            "parameter_review",
            "parameter_resolution.py",
            ["export-review", "--resolution", resolution, "--out", review],
        )
        return {
            "parameters": str(parameters_path),
            "parameter_seed_manifest": str(seed_manifest),
            "parameter_resolution": str(resolution),
            "parameter_completeness": str(completeness),
            "parameter_review": str(review),
        }

    def _generate_modeling_artifacts(self, request: dict[str, Any], output_root: Path, artifacts: dict[str, str]) -> dict[str, str]:
        seed_dir = output_root / "modeling_artifacts"
        self._run_skill(
            "modeling_artifact_generation",
            "modeling_artifact_seed.py",
            [
                "--evidence-packet", artifacts["evidence_packet"],
                "--out-dir", seed_dir,
                "--antenna-family", request["antenna_family"],
            ],
            allowed_codes=(0, 2),
        )
        manifest = _read_json(seed_dir / "paper_modeling_manifest.json")
        if manifest.get("status") == "blocked":
            raise ModelingPreparationError(
                "modeling_artifact_generation_blocked",
                "modeling_artifact_generation",
                manifest.get("blockers") or ["modeling artifact generation was blocked"],
                repair_actions=["repair the parameter evidence and regenerate all dependent modeling artifacts"],
                partial_artifacts={"modeling_artifact_manifest": str(seed_dir / "paper_modeling_manifest.json")},
            )
        return {
            "materials": str(seed_dir / "materials.json"),
            "solids": str(seed_dir / "solids.json"),
            "dimensions": str(seed_dir / "dimensions.json"),
            "boolean_plan": str(seed_dir / "boolean_plan.json"),
            "modeling_artifact_manifest": str(seed_dir / "paper_modeling_manifest.json"),
        }

    def _run_geometry_artifact_gate(self, output_root: Path, artifacts: dict[str, str]) -> dict[str, str]:
        gate = output_root / "geometry_artifact_gate.json"
        self._run_skill(
            "geometry_artifact_gate",
            "geometry_artifact_gate.py",
            [
                "--parameters-json", artifacts["parameters"],
                "--materials-json", artifacts["materials"],
                "--solids-json", artifacts["solids"],
                "--dimensions-json", artifacts["dimensions"],
                "--out", gate,
            ],
            allowed_codes=(0, 2),
        )
        gate_data = _read_json(gate)
        if gate_data.get("status") != "ok":
            blockers = [str(item.get("issue") or item) for item in gate_data.get("issues", []) if item.get("severity") != "warning"]
            raise ModelingPreparationError(
                "geometry_artifact_gate_failed",
                "geometry_artifact_gate",
                blockers or ["geometry artifact gate failed"],
                repair_actions=["repair the routed parameters/materials/solids stage and rerun the gate"],
                partial_artifacts={"geometry_artifact_gate": str(gate)},
            )
        return {"geometry_artifact_gate": str(gate)}

    def _build_and_validate_sketch(self, request: dict[str, Any], output_root: Path, artifacts: dict[str, str]) -> dict[str, str]:
        template = self.skill_root / "assets" / "templates" / "microstrip_patch.geometry_sketch.json"
        parameter_values = {
            item["name"]: item["value"]
            for item in _read_json(Path(artifacts["parameters"])).get("items", [])
            if item.get("name")
        }
        values_path = output_root / "geometry_parameter_values.json"
        _atomic_write_json(values_path, parameter_values)
        sketch = output_root / "geometry_sketch.json"
        fill_manifest = output_root / "geometry_template_fill_manifest.json"
        self._run_skill(
            "geometry_sketch_seed",
            "fill_geometry_template.py",
            ["--template", template, "--params-json", values_path, "--out", sketch, "--manifest", fill_manifest],
        )
        sketch_artifacts = {
            "geometry_parameter_values": str(values_path),
            "geometry_template_fill_manifest": str(fill_manifest),
            "geometry_sketch": str(sketch),
        }
        try:
            sketch_data = self._materialize_microstrip_sketch(request, artifacts, _read_json(sketch), parameter_values)
        except ModelingPreparationError as exc:
            exc.partial_artifacts.update(sketch_artifacts)
            raise
        _atomic_write_json(sketch, sketch_data)
        validation = output_root / "geometry_sketch_validation.json"
        args: list[Any] = [
            "--sketch", sketch,
            "--target-type", request["target_type"],
            "--evidence-packet", artifacts["evidence_packet"],
            "--parameter-resolution", artifacts["parameter_resolution"],
            "--manifest", validation,
        ]
        if request["target_type"] == "candidate_geometry":
            args.append("--allow-partial")
        self._run_skill("geometry_sketch_validation", "validate_geometry_sketch.py", args, allowed_codes=(0, 2))
        validation_data = _read_json(validation)
        if not validation_data.get("success") or not validation_data.get("allowed_for_cst_spec"):
            blockers = [str(item.get("message") or item.get("reason") or item) for item in validation_data.get("errors", [])]
            raise ModelingPreparationError(
                "geometry_sketch_validation_failed",
                "geometry_sketch_validation",
                blockers or ["geometry sketch validation did not authorize CST spec generation"],
                repair_actions=["repair the geometry sketch fields identified by the original skill validator"],
                partial_artifacts={**sketch_artifacts, "geometry_sketch_validation": str(validation)},
            )
        return {
            "geometry_parameter_values": str(values_path),
            "geometry_template_fill_manifest": str(fill_manifest),
            "geometry_sketch": str(sketch),
            "geometry_sketch_validation": str(validation),
        }

    def _create_cst_model_spec(self, output_root: Path, artifacts: dict[str, str], request: dict[str, Any]) -> dict[str, str]:
        spec = output_root / "cst_model_spec.json"
        manifest = output_root / "cst_model_spec_manifest.json"
        args: list[Any] = ["--sketch", artifacts["geometry_sketch"], "--out", spec, "--manifest", manifest]
        if request["target_type"] == "candidate_geometry":
            args.append("--allow-partial")
        self._run_skill("cst_model_spec", "sketch_to_cst_spec.py", args, allowed_codes=(0, 2))
        manifest_data = _read_json(manifest)
        if not manifest_data.get("success"):
            blockers = [str(item.get("reason") or item) for item in manifest_data.get("failed", [])]
            blockers.extend(str(item.get("reason") or item) for item in manifest_data.get("unsupported", []))
            raise ModelingPreparationError(
                "cst_model_spec_failed",
                "cst_model_spec",
                blockers or [manifest_data.get("reason") or "CST model spec conversion failed"],
                repair_actions=["repair unsupported or unresolved geometry; do not launch CST"],
                partial_artifacts={"cst_model_spec": str(spec), "cst_model_spec_manifest": str(manifest)},
            )
        return {"cst_model_spec": str(spec), "cst_model_spec_manifest": str(manifest)}

    def _parameter_fact(self, name: str, facts: list[dict[str, Any]]) -> dict[str, Any] | None:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}\s*=\s*-?\d+(?:\.\d+)?(?:mm|cm|um)?(?![A-Za-z0-9_])", re.IGNORECASE)
        matches = [fact for fact in facts if fact.get("fact_type") == "parameter" and pattern.search(str(fact.get("summary") or ""))]
        return matches[0] if len(matches) == 1 else None

    def _parameter_mapping(self, name: str) -> dict[str, Any]:
        mappings = {
            "sub_w": ("substrate", "x", ["substrate.xrange", "ground.xrange"]),
            "sub_l": ("substrate", "y", ["substrate.yrange", "ground.yrange"]),
            "h": ("substrate", "z", ["substrate.zrange"]),
            "patch_w": ("main_patch", "x", ["main_patch.xrange"]),
            "patch_l": ("main_patch", "y", ["main_patch.yrange"]),
            "feed_w": ("feed_line", "x", ["feed_line.xrange"]),
            "feed_l": ("feed_line", "y", ["feed_line.yrange"]),
            "metal_thickness": ("main_patch", "z", ["ground.zrange", "main_patch.zrange", "feed_line.zrange"]),
        }
        if name not in mappings:
            raise ModelingPreparationError(
                "unsupported_parameter_mapping",
                "parameter_resolution",
                [f"no deterministic microstrip mapping exists for parameter {name}"],
                repair_actions=["add an explicit parameter-to-object mapping before proceeding"],
            )
        target, axis, controls = mappings[name]
        return {"target_object": target, "operation": "feed" if target == "feed_line" else "add_body", "axis_or_edge": axis, "controls": controls}

    def _materialize_microstrip_sketch(
        self,
        request: dict[str, Any],
        artifacts: dict[str, str],
        sketch: dict[str, Any],
        values: dict[str, Any],
    ) -> dict[str, Any]:
        missing = [name for name in REQUIRED_MICROSTRIP_PARAMETERS if name not in values]
        if missing:
            raise ModelingPreparationError(
                "missing_geometry_parameters",
                "geometry_sketch",
                [f"missing microstrip parameters: {', '.join(missing)}"],
                missing_inputs=[f"parameter:{name}" for name in missing],
            )
        numeric = {name: float(values[name]) for name in REQUIRED_MICROSTRIP_PARAMETERS}
        if any(value <= 0 for value in numeric.values()):
            raise ModelingPreparationError(
                "non_positive_geometry_parameter",
                "geometry_sketch",
                ["all microstrip dimensions must be positive"],
                repair_actions=["correct non-positive dimensions in the source evidence"],
            )
        if numeric["patch_w"] >= numeric["sub_w"] or numeric["patch_l"] >= numeric["sub_l"]:
            raise ModelingPreparationError(
                "patch_outside_substrate",
                "geometry_sketch",
                ["patch dimensions must be smaller than substrate dimensions"],
                repair_actions=["verify sub_w/sub_l and patch_w/patch_l against the original source"],
            )
        feed_start = -numeric["sub_l"] / 2
        feed_end = feed_start + numeric["feed_l"]
        patch_bottom = -numeric["patch_l"] / 2
        if feed_end + 1e-9 < patch_bottom or feed_end > numeric["patch_l"] / 2:
            raise ModelingPreparationError(
                "feed_patch_connection_invalid",
                "geometry_sketch",
                ["feed_l does not place the feed line in contact with the patch"],
                repair_actions=["correct feed_l or provide an explicit inset/overlap geometry mapping"],
            )

        packet = _read_json(Path(artifacts["evidence_packet"]))
        facts = [item for item in packet.get("facts", []) if isinstance(item, dict)]
        fact_by_type: dict[str, dict[str, Any]] = {}
        for fact in facts:
            fact_by_type.setdefault(str(fact.get("fact_type") or ""), fact)
        required_types = ("antenna_type", "layer_stack", "feed", "ground", "patch", "frequency_range")
        if request["target_type"] == "paper_reproduction":
            weak = [kind for kind in required_types if (fact_by_type.get(kind) or {}).get("evidence_label") not in DIRECT_EVIDENCE]
            if weak:
                raise ModelingPreparationError(
                    "weak_geometry_evidence",
                    "geometry_sketch",
                    [f"paper reproduction requires direct evidence for: {', '.join(weak)}"],
                    repair_actions=["bind each critical geometry fact to an original source locator"],
                )
            if (fact_by_type.get("port") or {}).get("evidence_label") not in DIRECT_EVIDENCE:
                raise ModelingPreparationError(
                    "missing_direct_port_evidence",
                    "geometry_sketch",
                    ["paper reproduction cannot use an automatically inferred port face or orientation"],
                    missing_inputs=["evidence:port"],
                    repair_actions=["provide source-backed port_orientation, port_xmin/xmax, and port_zmin/zmax"],
                )
            placement_text = {
                kind: " ".join(str((fact_by_type.get(kind) or {}).get(key) or "") for key in ("summary", "quote_or_note"))
                for kind in ("patch", "feed", "ground")
            }
            missing_placement = []
            if not self._has_any(placement_text["patch"], ("centered", "centred", "居中")):
                missing_placement.append("centered_patch_location")
            if not self._has_any(placement_text["feed"], ("lower board edge", "board edge", "下边缘", "底边")):
                missing_placement.append("feed_edge_location")
            if not self._has_any(placement_text["ground"], ("continuous", "full", "covers", "完整", "覆盖")):
                missing_placement.append("ground_extent")
            if missing_placement:
                raise ModelingPreparationError(
                    "insufficient_placement_evidence",
                    "geometry_sketch",
                    [f"source does not support placement rules: {', '.join(missing_placement)}"],
                    missing_inputs=missing_placement,
                    repair_actions=["bind patch/feed/ground placement to explicit source text or figure measurements"],
                )
        evidence_label = "paper_fact" if request["target_type"] == "paper_reproduction" else "engineering_assumption"
        substrate_material = self._substrate_material(_read_json(Path(artifacts["materials"])))
        layer_fact_text = " ".join(str((fact_by_type.get("layer_stack") or {}).get(key) or "") for key in ("summary", "quote_or_note"))
        copper_is_direct = "copper" in self._materials_in(layer_fact_text)
        if request["target_type"] == "paper_reproduction" and not copper_is_direct:
            raise ModelingPreparationError(
                "missing_conductor_material_evidence",
                "geometry_sketch",
                ["Copper conductors would be a default, not a paper fact"],
                missing_inputs=["evidence:layer_stack.conductor_material"],
                repair_actions=["provide source-backed patch/feed/ground conductor material"],
            )
        h = numeric["h"]
        mt = numeric["metal_thickness"]
        sw = numeric["sub_w"]
        sl = numeric["sub_l"]
        pw = numeric["patch_w"]
        pl = numeric["patch_l"]
        fw = numeric["feed_w"]
        parameter_resolution = _read_json(Path(artifacts["parameter_resolution"]))
        parameter_entries = []
        mapping_by_name = {item["parameter"]: item for item in parameter_resolution.get("parameter_object_map", [])}
        for item in parameter_resolution.get("parameters", []):
            mapping = mapping_by_name.get(item["name"], {})
            parameter_entries.append(
                {
                    "name": item["name"],
                    "status": item["status"],
                    "value": item["value"],
                    "bounds": item.get("bounds"),
                    "unit": item["unit"],
                    "controls": mapping.get("controls", []),
                    "source_evidence": item["evidence_fact_id"],
                    "evidence_refs": [item["evidence_fact_id"]],
                    "next_action": item["next_action"],
                }
            )
        geometry_refs = [fact["id"] for fact in facts if fact.get("id")]
        parameter_ref = next((fact["id"] for fact in facts if fact.get("fact_type") == "parameter"), "")
        derivations = [
            {
                "id": "derive_layer_zranges",
                "provenance": "engineering_inference",
                "derivation_type": "derived",
                "outputs": ["layers[*].zrange"],
                "derivation_refs": [parameter_ref, (fact_by_type.get("layer_stack") or {}).get("id")],
                "rule": "Stack z ranges are calculated from h and metal_thickness with ground below z=0.",
            },
            {
                "id": "derive_centered_rectangles",
                "provenance": "engineering_inference",
                "derivation_type": "derived",
                "outputs": ["substrate.coordinates", "ground.coordinates", "main_patch.coordinates"],
                "derivation_refs": [parameter_ref, (fact_by_type.get("patch") or {}).get("id"), (fact_by_type.get("ground") or {}).get("id")],
                "rule": "Centered rectangle endpoints are calculated as +/- dimension/2.",
            },
            {
                "id": "derive_feed_coordinates",
                "provenance": "engineering_inference",
                "derivation_type": "derived",
                "outputs": ["feed_line.coordinates"],
                "derivation_refs": [parameter_ref, (fact_by_type.get("feed") or {}).get("id")],
                "rule": "Feed starts at the lower board edge and extends by feed_l toward the patch.",
            },
        ]
        port_fact = fact_by_type.get("port")
        if port_fact and port_fact.get("evidence_label") in DIRECT_EVIDENCE:
            port_values = self._parameter_values(str(port_fact.get("summary") or ""))
            port_coordinates = {
                "xrange": [self._dimension_mm(port_values["port_xmin"]), self._dimension_mm(port_values["port_xmax"])],
                "yrange": [self._dimension_mm(port_values["port_y"]), self._dimension_mm(port_values["port_y"])],
                "zrange": [self._dimension_mm(port_values["port_zmin"]), self._dimension_mm(port_values["port_zmax"])],
            }
            port_orientation = self._port_orientation(str(port_fact.get("summary") or ""))
            port_evidence = "paper_fact"
            port_provenance = {"provenance": "paper_fact", "derivation_type": "direct", "derivation_refs": [port_fact["id"]]}
        else:
            port_coordinates = {
                "xrange": [-5 * fw / 2, 5 * fw / 2],
                "yrange": [feed_start, feed_start],
                "zrange": [0.0, max(h + mt, 5 * h)],
            }
            port_orientation = "ymin"
            port_evidence = "engineering_assumption"
            port_provenance = {"provenance": "engineering_inference", "derivation_type": "derived", "derivation_refs": [parameter_ref, (fact_by_type.get("feed") or {}).get("id")]}
            derivations.append(
                {
                    "id": "derive_candidate_port_face",
                    **port_provenance,
                    "outputs": ["feed_port.orientation", "feed_port.coordinates"],
                    "rule": "Candidate microstrip port uses 5x feed width and at least 5x substrate height at ymin.",
                }
            )
        assumptions = [
            "Coordinate endpoints and layer z ranges are engineering_inference values derived from source-backed dimensions; they are not paper_fact values.",
            "The coordinate origin is placed at the substrate center to materialize the source-backed centered geometry.",
        ]
        if port_evidence != "paper_fact":
            assumptions.append("Port face dimensions and orientation are engineering_inference values for candidate geometry only.")
        conductor_provenance = "paper_fact" if copper_is_direct else "engineering_inference"
        if not copper_is_direct:
            assumptions.append("Copper patch/feed/ground conductors are an engineering_inference default because the source does not name the conductor material.")
        sketch.update(
            {
                "paper_id": request["paper_id"],
                "objective": request["target_type"],
                "decision": "go" if request["target_type"] == "paper_reproduction" else "partial",
                "antenna": {
                    "type": "microstrip_patch",
                    "coordinate_system": {"unit": "mm", "origin": "substrate center", "x_axis": "patch width", "y_axis": "feed to patch", "z_axis": "up"},
                    "frequency_range": request["frequency_range"],
                },
                "evidence_sources": geometry_refs,
                "layers": [
                    {"id": "ground_layer", "type": "ground", "material": "Copper", "zrange": [-mt, 0.0], "evidence": evidence_label, "evidence_scope": "layer_and_material_only", "material_provenance": conductor_provenance, "zrange_provenance": "engineering_inference", "derivation_ref": "derive_layer_zranges"},
                    {"id": "substrate_layer", "type": "substrate", "material": substrate_material, "zrange": [0.0, h], "evidence": evidence_label, "evidence_scope": "layer_and_material_only", "material_provenance": evidence_label, "zrange_provenance": "engineering_inference", "derivation_ref": "derive_layer_zranges"},
                    {"id": "top_metal", "type": "metal", "material": "Copper", "zrange": [h, h + mt], "evidence": evidence_label, "evidence_scope": "layer_and_material_only", "material_provenance": conductor_provenance, "zrange_provenance": "engineering_inference", "derivation_ref": "derive_layer_zranges"},
                ],
                "objects": [
                    self._object("substrate", "substrate", "substrate_layer", "add_body", [-sw / 2, sw / 2], [-sl / 2, sl / 2], [0.0, h], ["sub_w", "sub_l", "h"], evidence_label, fact_by_type.get("layer_stack"), "derive_centered_rectangles"),
                    self._object("ground", "ground", "ground_layer", "add_body", [-sw / 2, sw / 2], [-sl / 2, sl / 2], [-mt, 0.0], ["sub_w", "sub_l", "metal_thickness"], evidence_label, fact_by_type.get("ground"), "derive_centered_rectangles"),
                    self._object("main_patch", "patch", "top_metal", "add_body", [-pw / 2, pw / 2], [-pl / 2, pl / 2], [h, h + mt], ["patch_w", "patch_l", "metal_thickness"], evidence_label, fact_by_type.get("patch"), "derive_centered_rectangles"),
                    self._object("feed_line", "feed", "top_metal", "feed", [-fw / 2, fw / 2], [feed_start, feed_end], [h, h + mt], ["feed_w", "feed_l", "metal_thickness"], evidence_label, fact_by_type.get("feed"), "derive_feed_coordinates"),
                ],
                "parameters": parameter_entries,
                "ports": [
                    {
                        "id": "feed_port",
                        "number": 1,
                        "type": "waveguide_port",
                        "feed_object": "feed_line",
                        "reference_object": "ground",
                        "return_ground_or_ground_plane": "ground",
                        "orientation": port_orientation,
                        "coordinates": port_coordinates,
                        "evidence": port_evidence,
                        "provenance": port_provenance,
                        "evidence_refs": [port_fact["id"]] if port_fact else [(fact_by_type.get("feed") or {}).get("id")],
                        "critical": True,
                    }
                ],
                "assumptions": assumptions,
                "derivations": derivations,
                "mismatches": [],
                "blockers": [],
                "cst_readiness": {"allowed": True, "reason": "Evidence, parameter, LEAM artifact, and geometry gates passed.", "required_before_cst": ["human approval and CST environment preflight"]},
            }
        )
        return sketch

    def _object(
        self,
        object_id: str,
        object_type: str,
        layer: str,
        operation: str,
        xrange: list[float],
        yrange: list[float],
        zrange: list[float],
        controls: list[str],
        evidence_label: str,
        fact: dict[str, Any] | None,
        derivation_ref: str,
    ) -> dict[str, Any]:
        return {
            "id": object_id,
            "type": object_type,
            "layer": layer,
            "operation": operation,
            "shape": "rectangle",
            "coordinates": {"xrange": xrange, "yrange": yrange, "zrange": zrange, "points": []},
            "controlled_by": controls,
            "evidence": evidence_label,
            "evidence_scope": "object_existence_and_topology_only",
            "evidence_refs": [fact["id"]] if fact and fact.get("id") else [],
            "coordinates_provenance": "engineering_inference",
            "derivation_type": "derived",
            "derivation_ref": derivation_ref,
            "critical": True,
            "notes": "",
        }

    def _substrate_material(self, materials: dict[str, Any]) -> str:
        candidates = [
            str(item.get("name") or "")
            for item in materials.get("items", [])
            if str(item.get("role") or "").lower() == "substrate"
        ]
        conductive = {"copper", "pec", "copperpure", "vacuum", ""}
        for candidate in candidates:
            if candidate.lower().replace(" ", "") not in conductive:
                return candidate
        raise ModelingPreparationError(
            "missing_dielectric_material",
            "geometry_sketch",
            ["no non-conductive substrate material was extracted from layer-stack evidence"],
            missing_inputs=["evidence:layer_stack.substrate_material"],
            repair_actions=["add the substrate material to the traceable layer_stack evidence"],
        )

    def _result(
        self,
        *,
        success: bool,
        decision: str,
        stage: str,
        artifacts: dict[str, str],
        failure: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "operation": "antenna_modeling_preparation",
            "created_at": _now(),
            "success": success,
            "decision": decision,
            "completed_stage": stage,
            "cst_executed": False,
            "artifacts": artifacts,
            "failure": failure,
            "subprocess_trace": [record.as_dict() for record in self.command_records],
            "safety": {
                "original_skill_modified": False,
                "cst_process_allowed": False,
                "last_allowed_output": "cst_model_spec.json",
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare traceable antenna geometry through the read-only antenna research skill")
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--skill-root", default=str(CONFIGURED_SKILL_ROOT))
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()
    try:
        request = _read_json(Path(args.request_json))
        adapter = ModelingPreparationAdapter(skill_root=Path(args.skill_root), timeout_seconds=args.timeout_seconds)
        result = adapter.prepare(request, args.output_dir)
    except Exception as exc:
        result = {
            "schema_version": "1.0",
            "operation": "antenna_modeling_preparation",
            "success": False,
            "decision": "failed",
            "completed_stage": "cli_input",
            "cst_executed": False,
            "failure": {"code": "cli_input_error", "repairable": False, "blockers": [f"{exc.__class__.__name__}: {exc}"]},
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("success"):
        return 0
    return 2 if (result.get("failure") or {}).get("repairable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
