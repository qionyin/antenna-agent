from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from adapters.mcp_wrappers import MCPAntennaSkillsAdapter, MCPCstRealRunAdapter
from agent_runtime.capability_registry import CapabilityRegistry
from agent_runtime.mcp_client import SyncMCPClient


class MCPAdapterBridgeTests(unittest.TestCase):
    def test_sync_client_can_run_inside_an_active_event_loop(self):
        client = SyncMCPClient()

        async def fake_tools():
            return [{"name": "fake"}]

        client._list_tools = fake_tools

        async def invoke():
            return client.list_tools()

        self.assertEqual(asyncio.run(invoke()), [{"name": "fake"}])

    def test_mcp_server_lists_legacy_adapter_tools(self) -> None:
        tools = SyncMCPClient(timeout_seconds=20).list_tools()
        names = {tool["name"] for tool in tools}

        self.assertIn("antenna_discover_packet_adapters", names)
        self.assertIn("modeling_prepare", names)
        self.assertIn("cst_run_single", names)

    def test_antenna_packet_discovery_uses_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            adapter_dir = Path(root) / "antenna-idea-card"
            adapter_dir.mkdir()
            (adapter_dir / "packet_adapter.py").write_text("# adapter", encoding="utf-8")

            adapters = MCPAntennaSkillsAdapter(root).discover_packet_adapters()
            by_type = {item["packet_type"]: item for item in adapters}

            self.assertTrue(by_type["idea_card"]["available"])
            self.assertFalse(by_type["geometry_contract"]["available"])

    def test_capability_registry_exposes_mcp_tools_and_packet_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            adapter_dir = Path(root) / "antenna-idea-card"
            adapter_dir.mkdir()
            (adapter_dir / "packet_adapter.py").write_text("# adapter", encoding="utf-8")

            capabilities = CapabilityRegistry().scan({"antenna_skills_root": root, "e_platform_root": root, "e_results_root": root})

            self.assertIn("mcp_tool:antenna_discover_packet_adapters", capabilities)
            self.assertIn("mcp_tool:cst_run_single", capabilities)
            self.assertTrue(capabilities["antenna_packet_adapter:idea_card"].metadata["available"])

    def test_cst_fixture_run_round_trips_through_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            fixture = Path(root) / "fixture"
            fixture.mkdir()
            (fixture / "s11.csv").write_text("Frequency,S11\n2.0,-5\n2.4,-12\n2.8,-6\n", encoding="utf-8")
            (Path(root) / "simulator_skills" / "cst_local").mkdir(parents=True)
            (Path(root) / "simulator_skills" / "cst_local" / "adapter.py").write_text("# cst adapter", encoding="utf-8")
            (Path(root) / "simulator_skills" / "cst_local" / "official.py").write_text("# official", encoding="utf-8")
            (Path(root) / "AMD64" / "python").mkdir(parents=True)
            (Path(root) / "AMD64" / "python" / "python.exe").write_text("", encoding="utf-8")
            (Path(root) / "AMD64" / "python_cst_libraries").mkdir(parents=True)
            adapter = MCPCstRealRunAdapter(e_platform_root=root, e_results_root=root, cst_root=root)
            request = {"fixture_result_dir": str(fixture), "target_freq_range": [2.0, 3.0], "simulate_cst": False}

            preflight = adapter.preflight(request)
            run_manifest = adapter.run_single("mcp-fixture-task", request)
            parsed = adapter.parse_results(run_manifest, request)

            self.assertTrue(preflight["success"])
            self.assertTrue(run_manifest["success"])
            self.assertEqual(parsed["metrics"]["s11_min_db"], -12.0)


if __name__ == "__main__":
    unittest.main()
