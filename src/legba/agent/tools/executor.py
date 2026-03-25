"""
Tool Executor

Dispatches tool calls to their handlers, collects results, handles errors.
"""

from __future__ import annotations

import time
from typing import Any

from ..log import CycleLogger
from .registry import ToolRegistry


class ToolExecutor:
    """
    Executes tool calls by dispatching to registered handlers.

    Provides the `execute` callable that the LLM client's reason_with_tools
    loop uses.
    """

    def __init__(self, registry: ToolRegistry, logger: CycleLogger):
        self.registry = registry
        self.logger = logger

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Execute a tool call by name.

        This is the function passed to LLMClient.reason_with_tools() as
        the tool_executor callback.

        Returns the tool result (any type — will be stringified for the LLM).
        Raises on unknown tool or execution error.
        """
        handler = self.registry.get_handler(tool_name)
        if handler is None:
            error_msg = f"Unknown tool: {tool_name}. Available: {[t.name for t in self.registry.list_tools()]}"
            self.logger.log_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                error=error_msg,
            )
            return error_msg

        start = time.monotonic()
        try:
            result = await handler(arguments)
            duration = (time.monotonic() - start) * 1000

            self.logger.log_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                duration_ms=duration,
            )
            return result

        except Exception as e:
            duration = (time.monotonic() - start) * 1000
            error_msg = f"Tool execution error ({tool_name}): {e}"

            self.logger.log_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                error=error_msg,
                duration_ms=duration,
            )
            return error_msg
