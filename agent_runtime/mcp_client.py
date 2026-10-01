from __future__ import annotations

import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER_SCRIPT = PROJECT_ROOT / "adapters" / "mcp_server.py"
T = TypeVar("T")


class SyncMCPClient:
    """Small synchronous helper for one-shot stdio MCP calls."""

    def __init__(
        self,
        *,
        server_script: str | Path = DEFAULT_SERVER_SCRIPT,
        python_executable: str = sys.executable,
        server_args: list[str] | None = None,
        cwd: str | Path = PROJECT_ROOT,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.server_script = Path(server_script)
        self.python_executable = python_executable
        self.server_args = list(server_args or [])
        self.cwd = Path(cwd)
        self.env = dict(env or {})
        self.timeout_seconds = timeout_seconds

    def list_tools(self) -> list[dict[str, Any]]:
        return self._run_sync(self._list_tools)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return self._run_sync(lambda: self._call_tool(name, dict(arguments or {})))

    @staticmethod
    def _run_sync(factory: Callable[[], Awaitable[T]]) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(factory())
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-sync") as executor:
            return executor.submit(lambda: asyncio.run(factory())).result()

    def _server_parameters(self) -> StdioServerParameters:
        env = os.environ.copy()
        env.update(self.env)
        python_path = str(PROJECT_ROOT)
        if env.get("PYTHONPATH"):
            python_path = python_path + os.pathsep + env["PYTHONPATH"]
        env["PYTHONPATH"] = python_path
        return StdioServerParameters(
            command=self.python_executable,
            args=[str(self.server_script), *self.server_args],
            cwd=self.cwd,
            env=env,
            encoding="utf-8",
            encoding_error_handler="replace",
        )

    async def _list_tools(self) -> list[dict[str, Any]]:
        async with stdio_client(self._server_parameters()) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=self.timeout_seconds) as session:
                await session.initialize()
                result = await session.list_tools()
                return [tool.model_dump(mode="json") for tool in result.tools]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        async with stdio_client(self._server_parameters()) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=self.timeout_seconds) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments=arguments, read_timeout_seconds=self.timeout_seconds)
                is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
                if is_error:
                    text = self._content_text(result)
                    raise RuntimeError(text or f"MCP tool failed: {name}")
                structured = getattr(result, "structured_content", getattr(result, "structuredContent", None))
                if structured is not None:
                    if isinstance(structured, dict) and set(structured) == {"result"}:
                        return structured["result"]
                    return structured
                text = self._content_text(result)
                return json.loads(text) if text else None

    @staticmethod
    def _content_text(result: Any) -> str:
        content = getattr(result, "content", None) or []
        parts = [str(item.text) for item in content if getattr(item, "type", None) == "text"]
        return "\n".join(parts)
