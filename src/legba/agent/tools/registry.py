"""
Tool Registry

Manages available tools: built-in tools + dynamically registered tools
from /agent/tools/. Provides tool definitions in TypeScript format
for the LLM context.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Awaitable

from ...shared.schemas.tools import ToolDefinition, ToolParameter


# Type for tool implementation functions
ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class ToolRegistry:
    """
    Registry of all available tools.

    Loads built-in tools at init, scans /agent/tools/ for dynamic tools each cycle.
    """

    def __init__(self, dynamic_tools_path: str = "/agent/tools"):
        self._tools: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._dynamic_path = Path(dynamic_tools_path)

    def register(
        self,
        definition: ToolDefinition,
        handler: ToolHandler,
    ) -> None:
        """Register a tool with its definition and handler."""
        self._tools[definition.name] = definition
        self._handlers[definition.name] = handler

    def get_definition(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def get_handler(self, name: str) -> ToolHandler | None:
        return self._handlers.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def to_tool_data(self) -> list[dict]:
        """Return raw tool data dicts (name, description, parameters)."""
        tools_data = []
        for defn in self._tools.values():
            tools_data.append({
                "name": defn.name,
                "description": defn.description,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "required": p.required,
                    }
                    for p in defn.parameters
                ],
                "return_type": defn.return_type,
            })
        return tools_data

    def to_tool_definitions(self, only: set[str] | None = None) -> str:
        """
        Render tools as a formatted block for the LLM context.

        If *only* is provided, only those tools get full parameter details;
        the rest are listed as name + description with a pointer to explain_tool.
        """
        from ..llm.format import format_tool_definitions
        return format_tool_definitions(self.to_tool_data(), only=only)

    def to_tool_summary(self) -> str:
        """Compact tool list (name + description only) for the PLAN phase."""
        from ..llm.format import format_tool_summary
        return format_tool_summary(self.to_tool_data())

    def load_dynamic_tools(self) -> int:
        """
        Scan /agent/tools/ for dynamic tool definitions and load them.

        Each tool file is a JSON file with:
        {
            "name": "tool_name",
            "description": "...",
            "parameters": [...],
            "implementation": "shell" | "python",
            "command": "..." (for shell) or "code": "..." (for python)
        }

        Returns the number of tools loaded.
        """
        if not self._dynamic_path.exists():
            return 0

        loaded = 0
        for tool_file in self._dynamic_path.glob("*.json"):
            try:
                data = json.loads(tool_file.read_text())
                defn = ToolDefinition(
                    name=data["name"],
                    description=data.get("description", ""),
                    parameters=[
                        ToolParameter(**p) for p in data.get("parameters", [])
                    ],
                    return_type=data.get("return_type", "any"),
                    builtin=False,
                    source_file=str(tool_file),
                )

                # Create handler based on implementation type
                impl_type = data.get("implementation", "shell")
                if impl_type == "shell":
                    handler = _make_shell_handler(data.get("command", "echo 'no command'"))
                elif impl_type == "python":
                    handler = _make_python_handler(data.get("code", "pass"))
                else:
                    continue

                self.register(defn, handler)
                loaded += 1

            except Exception:
                continue  # Skip malformed tool files

        return loaded


async def _shell_execute(command: str, args: dict) -> str:
    """Execute a shell command with argument substitution."""
    import asyncio

    # Substitute {arg_name} in command with actual values
    cmd = command
    for key, value in args.items():
        cmd = cmd.replace(f"{{{key}}}", str(value))

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        output += f"\n[exit code: {proc.returncode}]\n{err}"
    return output


def _make_shell_handler(command: str) -> ToolHandler:
    async def handler(args: dict) -> Any:
        return await _shell_execute(command, args)
    return handler


def _make_python_handler(code: str) -> ToolHandler:
    async def handler(args: dict) -> Any:
        # Execute Python code with args available in local scope
        local_vars: dict[str, Any] = {"args": args, "result": None}
        exec(code, {}, local_vars)
        return local_vars.get("result")
    return handler
