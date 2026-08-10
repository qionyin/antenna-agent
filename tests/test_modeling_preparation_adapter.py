from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_CLI = PROJECT_ROOT / "adapters" / "modeling_preparation_adapter.py"
SKILL_ROOT = Path(r"C:\Users\30626\.codex\skills\Antenna Skills\antenna-research-ideation")


class ModelingPreparationAdapterAdversarialTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="antenna-modeling-prep-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def fixture_request(
        self,
        name: str = "english",
        *,
        target_type: str = "paper_reproduction",
        include_port: bool = True,
    ) -> dict[str, Any]:
        fixtures = {
            "english": {
                "page": 4,
                "frequency": {"min": 2.0, "max": 4.0, "unit": "GHz"},
                "parameter_source": "sub_w=40mm sub_l=30mm h=1.6mm patch_w=16mm patch_l=12mm feed_w=1.5mm feed_l=9mm metal_thickness=0.035mm",
                "parameter_summary": "sub_w=40mm sub_l=30mm h=1.6mm patch_w=16mm patch_l=12mm feed_w=1.5mm feed_l=9mm metal_thickness=0.035mm",
                "frequency_text": "The modeled frequency range is 2 GHz to 4 GHz.",
                "port_text": "The waveguide port is specified by port_orientation=ymin, port_xmin=-3.75mm, port_xmax=3.75mm, port_y=-15mm, port_zmin=0mm, and port_zmax=8mm.",
            },
            "reordered": {
                "page": 7,
                "frequency": {"min": 3.1, "max": 5.8, "unit": "GHz"},
                "parameter_source": "feed_l is 11 mm; patch_l: 14 mm; sub_l = 36 mm; metal_thickness is 0.018 mm; feed_w: 1.2 mm; h = 0.8 mm; patch_w is 18 mm; sub_w: 48 mm",
                "parameter_summary": "patch_w is 18 mm, sub_w: 48 mm, feed_w = 1.2 mm, sub_l is 36 mm, h: 0.8 mm, feed_l=11 mm, patch_l = 14 mm, metal_thickness: 0.018 mm",
                "frequency_text": "Simulation coverage extends from 3.1GHz through 5.8 GHz.",
                "port_text": "The waveguide port has port_zmax: 4mm; port_xmax=3mm; port_orientation is ymin; port_y=-18mm; port_zmin=0mm; port_xmin=-3mm.",
            },
            "converted_units": {
                "page": 12,
                "frequency": {"min": 1.8, "max": 2.6, "unit": "GHz"},
                "parameter_source": "metal_thickness=35um; feed_l=0.9cm; patch_l=1.2cm; patch_w=1.6cm; h=1600um; sub_l=3cm; feed_w=0.15cm; sub_w=4cm",
                "parameter_summary": "sub_w:4cm feed_w:0.15cm sub_l:3cm h:1600um patch_w:1.6cm patch_l:1.2cm feed_l:0.9cm metal_thickness:35um",
                "frequency_text": "The sweep begins at 1800 MHz and ends at 2600MHz.",
                "port_text": "For the waveguide port, port_orientation:ymin port_xmin:-0.375cm port_xmax:0.375cm port_y:-1.5cm port_zmin:0cm port_zmax:0.8cm.",
            },
        }
        fixture = fixtures[name]
        source = self.root / f"{name}_paper_extract.md"
        source.write_text(
            "\n".join(
                [
                    "# Paper geometry extract",
                    f"## Page {fixture['page']}",
                    "### Antenna Geometry",
                    "The reported antenna is a microstrip patch.",
                    "The layer stack uses a Rogers RT/duroid 5880 substrate and Copper top patch, feed, and continuous full ground plane on the underside.",
                    "A centered rectangular patch is excited by a microstrip feed line starting at the lower board edge.",
                    f"Dimensions: {fixture['parameter_source']}.",
                    fixture["frequency_text"],
                    fixture["port_text"],
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        common = {
            "source_path": str(source),
            "source_kind": "original_extract",
            "evidence_label": "paper_fact",
            "status": "usable",
            "page": fixture["page"],
            "section": "Antenna Geometry",
        }
        evidence = [
            {**common, "id": f"{name}-antenna", "fact_type": "antenna_type", "summary": "microstrip_patch antenna"},
            {**common, "id": f"{name}-layer", "fact_type": "layer_stack", "summary": "Rogers 5880 substrate with Copper conductors"},
            {**common, "id": f"{name}-feed", "fact_type": "feed", "summary": "microstrip feed line from the lower board edge"},
            {**common, "id": f"{name}-ground", "fact_type": "ground", "summary": "continuous full ground plane covers the underside"},
            {**common, "id": f"{name}-patch", "fact_type": "patch", "summary": "centered rectangular patch"},
            {**common, "id": f"{name}-parameters", "fact_type": "parameter", "summary": fixture["parameter_summary"]},
            {**common, "id": f"{name}-frequency", "fact_type": "frequency_range", "summary": fixture["frequency_text"]},
        ]
        if include_port:
            evidence.append({**common, "id": f"{name}-port", "fact_type": "port", "summary": fixture["port_text"]})
        return {
            "schema_version": "1.0",
            "task_id": f"task-{name}",
            "paper_id": f"paper-{name}",
            "objective": "Prepare a traceable microstrip patch model and stop before CST execution.",
            "target_type": target_type,
            "antenna_family": "microstrip_patch",
            "frequency_range": fixture["frequency"],
            "evidence": evidence,
        }

    def run_cli(
        self,
        request: dict[str, Any],
        *,
        skill_root: Path = SKILL_ROOT,
        output_dir: Path | None = None,
        expect_persisted_result: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], Path]:
        case_id = hashlib.sha1(json.dumps(request, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
        case_root = self.root / f"case-{case_id}-{uuid.uuid4().hex[:6]}"
        case_root.mkdir(parents=True, exist_ok=True)
        request_path = case_root / "request.json"
        requested_output = output_dir or (case_root / "outputs")
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(ADAPTER_CLI),
                "--request-json", str(request_path),
                "--output-dir", str(requested_output),
                "--skill-root", str(skill_root),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
        result_path = requested_output / "modeling_preparation_result.json"
        if expect_persisted_result:
            self.assertTrue(result_path.is_file(), msg=f"stdout={completed.stdout}\nstderr={completed.stderr}")
            result = json.loads(result_path.read_text(encoding="utf-8"))
        else:
            result = json.loads(completed.stdout)
            self.assertFalse(result_path.exists())
        return completed, result, requested_output

    def test_three_diverse_real_subprocess_fixtures_reach_model_spec(self) -> None:
        for fixture_name in ("english", "reordered", "converted_units"):
            with self.subTest(fixture=fixture_name):
                before = self._skill_hashes()
                completed, result, _ = self.run_cli(self.fixture_request(fixture_name))
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertTrue(result["success"])
                self.assertEqual(result["completed_stage"], "cst_model_spec")
                self.assertFalse(result["cst_executed"])
                validation = self._artifact(result, "geometry_sketch_validation")
                spec_manifest = self._artifact(result, "cst_model_spec_manifest")
                self.assertTrue(validation["success"])
                self.assertTrue(spec_manifest["success"])
                self.assertEqual(spec_manifest["summary"]["objects_in_cst_spec"], 4)
                self.assertEqual(spec_manifest["summary"]["ports_in_cst_spec"], 1)
                self.assertEqual(before, self._skill_hashes())

    def test_derived_geometry_has_explicit_inference_and_nonempty_assumptions(self) -> None:
        completed, result, _ = self.run_cli(self.fixture_request("english"))
        self.assertEqual(completed.returncode, 0)
        sketch = self._artifact(result, "geometry_sketch")
        self.assertTrue(sketch["assumptions"])
        self.assertTrue(sketch["derivations"])
        for derivation in sketch["derivations"]:
            self.assertEqual(derivation["provenance"], "engineering_inference")
            self.assertEqual(derivation["derivation_type"], "derived")
            self.assertTrue([ref for ref in derivation["derivation_refs"] if ref])
        for obj in sketch["objects"]:
            self.assertEqual(obj["coordinates_provenance"], "engineering_inference")
            self.assertEqual(obj["derivation_type"], "derived")
            self.assertTrue(obj["derivation_ref"])
        for layer in sketch["layers"]:
            self.assertEqual(layer["zrange_provenance"], "engineering_inference")
        self.assertEqual(sketch["ports"][0]["provenance"]["derivation_type"], "direct")

    def test_candidate_without_port_uses_inference_not_paper_fact(self) -> None:
        request = self.fixture_request("english", target_type="candidate_geometry", include_port=False)
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        sketch = self._artifact(result, "geometry_sketch")
        port = sketch["ports"][0]
        self.assertEqual(port["evidence"], "engineering_assumption")
        self.assertEqual(port["provenance"]["provenance"], "engineering_inference")
        self.assertEqual(port["provenance"]["derivation_type"], "derived")
        self.assertTrue(any("Port face" in assumption for assumption in sketch["assumptions"]))

    def test_candidate_copper_default_is_marked_as_inference_and_assumption(self) -> None:
        request = self.fixture_request("english", target_type="candidate_geometry", include_port=False)
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "and Copper top patch, feed, and continuous full ground plane",
                "and an unspecified conductor for the top patch, feed, and continuous full ground plane",
            ),
            encoding="utf-8",
        )
        layer = next(item for item in request["evidence"] if item["fact_type"] == "layer_stack")
        layer["summary"] = "Rogers 5880 substrate with unspecified conductors"
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        sketch = self._artifact(result, "geometry_sketch")
        metal_layers = [layer for layer in sketch["layers"] if layer["type"] in {"ground", "metal"}]
        self.assertTrue(metal_layers)
        self.assertTrue(all(layer["material_provenance"] == "engineering_inference" for layer in metal_layers))
        self.assertTrue(any("Copper patch/feed/ground" in assumption for assumption in sketch["assumptions"]))

    def test_paper_reproduction_without_direct_port_evidence_is_blocked(self) -> None:
        completed, result, _ = self.run_cli(self.fixture_request("english", include_port=False))
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "missing_direct_port_evidence")
        self.assertIn("evidence:port", result["failure"]["missing_inputs"])

    def test_unrelated_source_with_fabricated_facts_is_rejected(self) -> None:
        request = self.fixture_request("english")
        unrelated = self.root / "unrelated.md"
        unrelated.write_text("# Cooking notes\n## Page 4\n### Antenna Geometry\nBoil water and add rice.\n", encoding="utf-8")
        for item in request["evidence"]:
            item["source_path"] = str(unrelated)
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "fact_not_supported_by_source")
        self.assertEqual(result["subprocess_trace"], [])

    def test_material_fact_conflicting_with_source_is_rejected(self) -> None:
        request = self.fixture_request("english")
        layer = next(item for item in request["evidence"] if item["fact_type"] == "layer_stack")
        layer["summary"] = "FR-4 substrate with Copper conductors"
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "fact_not_supported_by_source")

    def test_negated_copper_is_not_positive_material_evidence(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "The layer stack uses a Rogers RT/duroid 5880 substrate and Copper top patch, feed, and continuous full ground plane on the underside.",
                "The layer stack uses a Rogers RT/duroid 5880 substrate but does not use Copper for the top patch, feed, or continuous full ground plane.",
            ),
            encoding="utf-8",
        )
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "fact_not_supported_by_source")

    def test_negated_dielectric_is_not_positive_material_evidence(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "The layer stack uses a Rogers RT/duroid 5880 substrate and Copper top patch, feed, and continuous full ground plane on the underside.",
                "The layer stack does not use Rogers RT/duroid 5880; it uses Copper conductors and an unspecified dielectric.",
            ),
            encoding="utf-8",
        )
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "fact_not_supported_by_source")

    def test_negated_feed_is_not_positive_structure_evidence(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "A centered rectangular patch is excited by a microstrip feed line starting at the lower board edge.",
                "A centered rectangular patch does not use a microstrip feed line; its excitation is unspecified.",
            ),
            encoding="utf-8",
        )
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "fact_not_supported_by_source")

    def test_chinese_negation_is_not_positive_material_evidence(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "The layer stack uses a Rogers RT/duroid 5880 substrate and Copper top patch, feed, and continuous full ground plane on the underside.",
                "The layer stack uses a Rogers RT/duroid 5880 substrate，未采用 Copper 作为贴片、馈线和地板材料。",
            ),
            encoding="utf-8",
        )
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "fact_not_supported_by_source")

    def test_fabricated_page_locator_is_rejected(self) -> None:
        request = self.fixture_request("english")
        request["evidence"][0]["page"] = 99
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "locator_not_found")
        self.assertEqual(result["subprocess_trace"], [])

    def test_fact_on_same_page_but_outside_declared_section_is_rejected(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        original = source.read_text(encoding="utf-8")
        body = original.split("### Antenna Geometry", 1)[1]
        source.write_text(
            "# Paper geometry extract\n## Page 4\n### Antenna Geometry\nNo antenna facts are stated here.\n### Unrelated Appendix\n" + body,
            encoding="utf-8",
        )
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "fact_not_supported_by_source")

    def test_similar_section_heading_is_not_an_exact_match(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(
            source.read_text(encoding="utf-8").replace("### Antenna Geometry", "### Antenna Geometry Details"),
            encoding="utf-8",
        )
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "locator_not_found")

    def test_duplicate_exact_section_heading_on_same_page_is_ambiguous(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(
            source.read_text(encoding="utf-8") + "\n### Antenna Geometry\nDuplicate section without authoritative facts.\n",
            encoding="utf-8",
        )
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "ambiguous_section_locator")

    def test_same_exact_heading_on_different_page_is_disambiguated_by_page(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(
            source.read_text(encoding="utf-8")
            + "\n## Page 5\n### Antenna Geometry\nThis different page does not use the reported geometry.\n",
            encoding="utf-8",
        )
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertTrue(result["success"])
        self.assertEqual(result["completed_stage"], "cst_model_spec")

    def test_conflicting_parameter_values_in_same_located_source_are_blocked(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(source.read_text(encoding="utf-8") + "A second table reports feed_w=2.0mm.\n", encoding="utf-8")
        common = dict(request["evidence"][0])
        common.update({"id": "conflict", "fact_type": "parameter", "summary": "feed_w=2.0mm"})
        request["evidence"].append(common)
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "conflicting_source_values")
        self.assertIn("parameter:feed_w", result["failure"]["missing_inputs"])

    def test_missing_parameter_produces_repairable_partial_artifacts(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(source.read_text(encoding="utf-8").replace(" metal_thickness=0.035mm", ""), encoding="utf-8")
        parameter = next(item for item in request["evidence"] if item["fact_type"] == "parameter")
        parameter["summary"] = parameter["summary"].replace(" metal_thickness=0.035mm", "")
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "unresolved_parameters")
        self.assertTrue(Path(result["artifacts"]["parameters"]).is_file())
        self.assertIn("parameter:metal_thickness", result["failure"]["missing_inputs"])

    def test_patch_outside_substrate_is_blocked_after_real_gates(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(source.read_text(encoding="utf-8").replace("patch_w=16mm", "patch_w=42mm"), encoding="utf-8")
        parameter = next(item for item in request["evidence"] if item["fact_type"] == "parameter")
        parameter["summary"] = parameter["summary"].replace("patch_w=16mm", "patch_w=42mm")
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "patch_outside_substrate")
        self.assertTrue(Path(result["artifacts"]["geometry_sketch"]).is_file())
        self.assertNotIn("cst_model_spec", result["artifacts"])

    def test_missing_dielectric_is_not_replaced_by_copper_default(self) -> None:
        request = self.fixture_request("english")
        source = Path(request["evidence"][0]["source_path"])
        source.write_text(source.read_text(encoding="utf-8").replace("Rogers RT/duroid 5880 substrate and ", "an unspecified substrate and "), encoding="utf-8")
        layer = next(item for item in request["evidence"] if item["fact_type"] == "layer_stack")
        layer["summary"] = "Copper conductors with an unspecified substrate"
        completed, result, _ = self.run_cli(request)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["failure"]["code"], "missing_dielectric_material")

    def test_existing_malicious_skill_clone_is_rejected_before_script_execution(self) -> None:
        malicious = self.root / "antenna-research-ideation"
        (malicious / "scripts").mkdir(parents=True)
        (malicious / "SKILL.md").write_text("malicious", encoding="utf-8")
        for name in (
            "init_objective_contract.py", "evidence_ledger.py", "modeling_artifact_seed.py",
            "parameter_resolution.py", "geometry_artifact_gate.py", "fill_geometry_template.py",
            "validate_geometry_sketch.py", "sketch_to_cst_spec.py",
        ):
            (malicious / "scripts" / name).write_text("raise SystemExit('malicious')\n", encoding="utf-8")
        completed, result, _ = self.run_cli(
            self.fixture_request("english"),
            skill_root=malicious,
            expect_persisted_result=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(result["failure"]["code"], "untrusted_skill_root")
        self.assertEqual(result["subprocess_trace"], [])

    def test_output_inside_original_skill_root_is_rejected_without_writing(self) -> None:
        forbidden_output = SKILL_ROOT / f"forbidden-test-{uuid.uuid4().hex}"
        self.assertFalse(forbidden_output.exists())
        completed, result, _ = self.run_cli(
            self.fixture_request("english"),
            output_dir=forbidden_output,
            expect_persisted_result=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(result["failure"]["code"], "unsafe_output_dir")
        self.assertEqual(result["subprocess_trace"], [])
        self.assertFalse(forbidden_output.exists())

    def test_output_inside_e_antenna_skills_is_rejected_without_writing(self) -> None:
        forbidden_output = Path(r"E:\antenna skills") / f"forbidden-test-{uuid.uuid4().hex}"
        self.assertFalse(forbidden_output.exists())
        completed, result, _ = self.run_cli(
            self.fixture_request("english"),
            output_dir=forbidden_output,
            expect_persisted_result=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(result["failure"]["code"], "unsafe_output_dir")
        self.assertEqual(result["subprocess_trace"], [])
        self.assertFalse(forbidden_output.exists())

    def _artifact(self, result: dict[str, Any], name: str) -> dict[str, Any]:
        return json.loads(Path(result["artifacts"][name]).read_text(encoding="utf-8"))

    def _skill_hashes(self) -> dict[str, str]:
        paths = [SKILL_ROOT / "SKILL.md", SKILL_ROOT / "assets" / "templates" / "microstrip_patch.geometry_sketch.json"]
        paths.extend(
            SKILL_ROOT / "scripts" / name
            for name in (
                "init_objective_contract.py", "evidence_ledger.py", "modeling_artifact_seed.py",
                "parameter_resolution.py", "geometry_artifact_gate.py", "fill_geometry_template.py",
                "validate_geometry_sketch.py", "sketch_to_cst_spec.py",
            )
        )
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


if __name__ == "__main__":
    unittest.main()
