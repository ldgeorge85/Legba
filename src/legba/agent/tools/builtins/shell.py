"""
Shell execution tool: exec
"""

from __future__ import annotations

import asyncio
from typing import Any

from ....shared.schemas.tools import ToolDefinition, ToolParameter

# Module-level default, overridden by register() with config value
_max_timeout: int = 60


def get_definitions() -> list[tuple[ToolDefinition, Any]]:
    return [(EXEC_DEF, exec_command)]


def register(registry, *, agent_config=None, **_deps) -> None:
    """Register shell tools. Accepts agent_config for timeout ceiling."""
    global _max_timeout
    if agent_config is not None:
        _max_timeout = agent_config.shell_timeout
        EXEC_DEF.parameters[2].description = (
            f"Timeout in seconds (default {_max_timeout}, max {_max_timeout})"
        )
    registry.register(EXEC_DEF, exec_command)


EXEC_DEF = ToolDefinition(
    name="exec",
    description="Execute a shell command in the agent container",
    parameters=[
        ToolParameter(name="command", type="string", description="The shell command to execute"),
        ToolParameter(name="working_dir", type="string", description="Working directory", required=False),
        ToolParameter(name="timeout", type="number", description=f"Timeout in seconds (default {_max_timeout}, max {_max_timeout})", required=False),
    ],
)


async def exec_command(args: dict) -> str:
    command = args.get("command", "")
    if not command:
        return "Error: No command provided"

    working_dir = args.get("working_dir")
    requested = int(args.get("timeout", _max_timeout))
    timeout = min(requested, _max_timeout)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: Command timed out after {timeout}s"

        output = stdout.decode("utf-8", errors="replace")
        err_output = stderr.decode("utf-8", errors="replace")

        result = ""
        if output:
            result += output
        if err_output:
            result += f"\n[stderr]\n{err_output}"
        if proc.returncode != 0:
            result += f"\n[exit code: {proc.returncode}]"

        return result.strip() or "(no output)"

    except Exception as e:
        return f"Error executing command: {e}"
