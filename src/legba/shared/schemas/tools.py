"""
Tool system schemas.

Defines tool definitions (for LLM context), tool calls (from LLM output),
and tool results (fed back to LLM).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ToolParameter(BaseModel):
    """A single parameter in a tool's signature."""

    name: str
    type: str  # "string", "number", "boolean", "object", "array"
    description: str = ""
    required: bool = True
    default: Any = None


class ToolDefinition(BaseModel):
    """Definition of a tool available to the agent."""

    name: str
    description: str
    parameters: list[ToolParameter] = Field(default_factory=list)
    return_type: str = "any"

    # Metadata
    builtin: bool = True  # False for dynamically registered tools
    source_file: str | None = None  # For dynamic tools: path to definition file

    def to_typescript(self) -> str:
        """Render this tool as TypeScript-style definition for the developer message."""
        params = []
        for p in self.parameters:
            optional = "?" if not p.required else ""
            params.append(f"  {p.name}{optional}: {p.type},")

        params_block = "\n".join(params)
        return (
            f"// {self.description}\n"
            f"type {self.name} = (_: {{\n"
            f"{params_block}\n"
            f"}}) => {self.return_type};"
        )


class ToolCall(BaseModel):
    """A tool invocation parsed from LLM output."""

    id: UUID = Field(default_factory=uuid4)
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_text: str = ""  # The raw LLM output that produced this call


class ToolResult(BaseModel):
    """Result of executing a tool call."""

    call_id: UUID
    tool_name: str
    success: bool = True
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
