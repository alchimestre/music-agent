from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from music_agent import config, state


def test_stdio_tools_return_structured_results_without_live_collections(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[paths]\nstate = "state"\nstaging = "staging"\nlossless = "lossless"\n'
        'lossy = "lossy"\npeers = "peers"\n[export]\ntargets = []\n',
        encoding="utf-8",
    )
    job = state.create(config.load(config_path), "mcp-test", records=[])
    asyncio.run(_check_stdio_tools(config_path, job.id))


async def _check_stdio_tools(config_path: Path, job_id: str) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "music_agent.mcp_server", "--config", str(config_path)],
        env={**os.environ, "PATH": str(Path(sys.executable).parent)},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert {tool.name for tool in tools} == {
                "run_playlist", "pass_status", "deliver_pass", "cleanup_playlist",
            }
            assert all(tool.outputSchema for tool in tools)
            for name in ("pass_status", "deliver_pass", "cleanup_playlist"):
                result = await session.call_tool(name, {"job_id": job_id})
                assert not result.isError
                assert result.structuredContent["job_id"] == job_id
            invalid = await session.call_tool("run_playlist", {"source": "https://invalid.example/list"})
            assert invalid.isError
