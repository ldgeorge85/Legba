# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Legba MCP Server — Model Context Protocol exposure of analytical tools.

Exposes analytical capabilities as MCP tools for Claude Code and other MCP
clients. The catalog is sourced entirely from the descriptor-driven
:data:`legba.data.outputs.mcp_tool.MCP_TOOL_REGISTRY` (L-194) — every analyst
descriptor whose body declares ``outputs.mcp_tool`` is auto-surfaced here.

History
-------
Pre-reshape this module also carried a hand-wired catalog (``consult``,
``search_entities``, ``search_signals``, ``search_events``, ``graph_query``,
``get_situation``, ``get_world_assessment``, ``system_status``) backed by a
``StoreHolder`` that reached into the legacy ``legba.agent.memory.*`` stores.
L-205 retired the embedded stores along with the rest of the legacy
``agent/`` tree; the analytical surface those tools provided is rebuilt as
descriptor-driven analyst kinds — the ``consult`` tool returns as a
``consult_on_demand`` analyst (L-178), and the read-only query surface
returns as descriptor-driven tools backed by the new substrate package.

Transport: stdio (for Claude Code integration).
Protocol: MCP 2025-11-25 via the `mcp` Python SDK.

Usage:
    python -m legba.ui.mcp_server

Or via the entry point:
    legba-mcp
"""

from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
)

# L-194 — pull descriptor-registered MCP tool bindings from the shared
# registry so analyst descriptors that declare `outputs.mcp_tool` are
# auto-surfaced here. The registry is process-wide; the runtime populates
# it at descriptor activate time. Importing it here does not by itself
# trigger any registration — it's just the catalog the legba-mcp stdio
# server reads.
from ..data.outputs.mcp_tool import MCP_TOOL_REGISTRY

log = logging.getLogger(__name__)


# ======================================================================
# MCP server setup
# ======================================================================

def create_server() -> Server:
    """Create and configure the MCP server.

    The tool catalog is sourced from :data:`MCP_TOOL_REGISTRY` — every
    analyst descriptor whose body declares ``outputs.mcp_tool`` is
    surfaced here. The registry is populated at descriptor activate time
    by the runtime; this factory only reads the live catalog.
    """
    server = Server("legba")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return MCP_TOOL_REGISTRY.list_mcp_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
        args = arguments or {}

        if MCP_TOOL_REGISTRY.get(name) is None:
            available = sorted(MCP_TOOL_REGISTRY.names())
            return [TextContent(
                type="text",
                text=f"Unknown tool: '{name}'. Available: {', '.join(available)}",
            )]

        try:
            text = await MCP_TOOL_REGISTRY.handle(name, args)
        except Exception as exc:  # noqa: BLE001 — bubble as MCP text error
            log.exception("Registry tool %s failed", name)
            text = f"Error executing {name}: {exc}"
        return [TextContent(type="text", text=text)]

    return server


async def run_stdio() -> None:
    """Run the MCP server over stdio transport."""
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Entry point for the MCP server."""
    # Configure logging to stderr (stdout is for MCP protocol)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
