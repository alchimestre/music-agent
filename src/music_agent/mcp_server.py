"""Stdio MCP tools over the pipeline's structured operations."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .agent_api import AgentAPI


def create_server(api: AgentAPI) -> FastMCP:
    server = FastMCP('music-agent')

    @server.tool()
    def run_playlist(source: str) -> dict[str, Any]:
        """Start one Spotify playlist pass; return its job ID without waiting. Never remove source tracks."""
        return api.run_playlist(source)

    @server.tool()
    def pass_status(job_id: str) -> dict[str, Any]:
        """Read current progress, acquisition, review reasons, blank genres and verified collection outcomes."""
        return api.pass_status(job_id)

    @server.tool()
    def deliver_pass(job_id: str, apply: bool = False) -> dict[str, Any]:
        """Preview collection delivery, or apply its prior preview and refresh receipts. Never acquire or process audio."""
        return api.deliver_pass(job_id, apply=apply)

    @server.tool()
    def cleanup_playlist(job_id: str, scope: Literal['acquired', 'delivered'] = 'acquired',
                         apply: bool = False) -> dict[str, Any]:
        """Preview or remove tracks verified in every collection. Default: this pass's acquisitions only; delivered includes held tracks."""
        return api.cleanup_playlist(job_id, scope=scope, apply=apply)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='pipeline configuration file')
    args = parser.parse_args()
    api = AgentAPI(config_path=args.config)
    try:
        create_server(api).run(transport='stdio')
    finally:
        api.close()


if __name__ == '__main__':
    main()
