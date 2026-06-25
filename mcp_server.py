#!/usr/bin/env python3
"""
Legba MCP Server — standalone entry point.

Run this directly or via: python -m legba.ui.mcp_server
For Claude Code, configure in .claude/settings.json (see docs/mcp_setup.md).

The stdio server itself needs no environment variables: the tool catalog
is descriptor-driven (``legba.data.outputs.mcp_tool.MCP_TOOL_REGISTRY``)
and populated by the runtime at descriptor activate time.
"""
import sys
import os

# Ensure src is on the path when running from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from legba.ui.mcp_server import main

if __name__ == "__main__":
    main()
