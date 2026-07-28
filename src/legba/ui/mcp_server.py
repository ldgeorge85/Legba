# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Legba MCP Server — Model Context Protocol exposure of analytical tools.

Exposes Legba's read + consult plane as MCP tools for Claude Code and any
other MCP client. The catalog has TWO sources, merged and deduped by tool
name at ``tools/list`` time:

1. **Built-in tools** (:mod:`legba.ui.mcp_builtin_tools`, A8) — a fixed set
   of seven tools, each wrapping a live registry HTTP endpoint through
   :class:`legba.runtime.registry_client.RegistryHTTPClient`:

     * ``substrate_findings`` / ``substrate_situations`` / ``substrate_signals``
       — the substrate read surfaces (``GET /api/v1/findings|situations|signals``);
     * ``lineage_walk`` — the provenance DAG walk
       (``GET /api/v1/lineage/{row_kind}/{row_id}``);
     * ``since`` — the "what changed since" diff (``GET /api/v1/v3/since``);
     * ``export`` — the read-only document composer (``POST /api/v1/v3/export``);
     * ``consult`` — the on-demand ReAct analyst (``POST /api/v1/consult``).

   These are constructed from code, so a **standalone** ``legba-mcp`` process
   serves them regardless of runtime population — this is what fixes the
   historical standalone-empty gap (see below).

2. **Descriptor-driven tools** (:data:`legba.data.outputs.mcp_tool.MCP_TOOL_REGISTRY`,
   L-194) — every analyst descriptor whose body declares ``outputs.mcp_tool``
   is auto-surfaced. This registry is process-wide and populated by the
   RUNTIME at descriptor-activate time; a standalone process sees it empty.
   It remains the SECOND catalog source. On a name collision the built-in
   wins (the descriptor tool is dropped from the merged list).

The standalone-empty fix
------------------------
Pre-A8 the catalog was *entirely* descriptor-driven, so a separately-launched
``legba-mcp`` process — the way an MCP client actually spawns it (one
short-lived ``docker run -i`` per conversation, never sharing a process with
the runtime) — listed **zero** tools. The built-in source removes that: the
seven tools register at server start with no runtime present.

No registry MUTATIONS ride MCP — reads + the sanctioned consult-run only.
:func:`legba.ui.mcp_builtin_tools.assert_reads_and_consult_only` encodes and
enforces that; it is asserted at server construction.

Transport: stdio (for Claude Code integration).
Protocol: MCP 2025-11-25 via the `mcp` Python SDK.

Usage:
    python -m legba.ui.mcp_server

Or via the entry point:
    legba-mcp
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

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
# trigger any registration — it's just the SECOND catalog source the
# legba-mcp stdio server reads (the built-ins are the first).
from ..data.outputs.mcp_tool import MCP_TOOL_REGISTRY
from ..runtime.registry_client import RegistryHTTPClient
from .mcp_builtin_tools import (
    BuiltinTool,
    assert_reads_and_consult_only,
    build_registry_client,
    builtin_tools,
)

log = logging.getLogger(__name__)


# ======================================================================
# Catalog merge (pure — unit-testable)
# ======================================================================


def _builtin_as_tool(t: BuiltinTool) -> Tool:
    """Render one :class:`BuiltinTool` as an ``mcp.types.Tool`` for tools/list."""
    return Tool(
        name=t.name,
        description=t.scoped_description(),
        inputSchema=t.input_schema,
    )


def merge_tool_catalogs(
    builtins: list[BuiltinTool],
    registry_tools: list[Tool],
) -> list[Tool]:
    """Merge the built-in + descriptor catalogs, deduped by tool name.

    Built-ins come first and WIN on a name collision — a descriptor that
    reuses a built-in name is dropped from the merged list (the built-in is
    the canonical wrapper). Returns ``mcp.types.Tool`` instances.
    """
    builtin_names = {t.name for t in builtins}
    merged: list[Tool] = [_builtin_as_tool(t) for t in builtins]
    for tool in registry_tools:
        if tool.name in builtin_names:
            log.debug("descriptor tool %s shadowed by built-in", tool.name)
            continue
        merged.append(tool)
    return merged


def _format_result(result: Any) -> str:
    """Serialize a built-in handler's structured dict result to MCP text."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, indent=2, default=str)
    except (TypeError, ValueError):
        return str(result)


# ======================================================================
# MCP server setup
# ======================================================================

def create_server(
    *,
    registry_client: RegistryHTTPClient | None = None,
    builtins: list[BuiltinTool] | None = None,
) -> Server:
    """Create and configure the MCP server.

    The tool catalog merges the built-in tool set (each wrapping a live
    registry endpoint) with the descriptor-driven
    :data:`MCP_TOOL_REGISTRY`. The built-ins register regardless of runtime
    population, which is what makes a standalone ``legba-mcp`` non-empty.

    ``registry_client`` / ``builtins`` are injectable for tests; production
    builds them from env (:func:`build_registry_client` /
    :func:`builtin_tools`).
    """
    server = Server("legba")

    tools = builtins if builtins is not None else builtin_tools()
    # Defensive: fail loud at construction if a mutating verb ever sneaks in.
    assert_reads_and_consult_only(tools)
    tools_by_name = {t.name: t for t in tools}
    client = registry_client if registry_client is not None else build_registry_client()

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return merge_tool_catalogs(tools, MCP_TOOL_REGISTRY.list_mcp_tools())

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
        args = arguments or {}

        # Built-in tools take precedence (they win the name dedupe too).
        builtin = tools_by_name.get(name)
        if builtin is not None:
            try:
                result = await builtin.handler(client, args)
            except Exception as exc:  # noqa: BLE001 — bubble as MCP text error
                log.exception("Built-in tool %s failed", name)
                result = {"error": f"tool_execution_failed: {exc}", "tool": name}
            return [TextContent(type="text", text=_format_result(result))]

        # Fall through to the descriptor-driven catalog.
        if MCP_TOOL_REGISTRY.get(name) is None:
            available = sorted({*tools_by_name, *MCP_TOOL_REGISTRY.names()})
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
