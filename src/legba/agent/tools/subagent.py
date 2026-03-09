"""
Sub-Agent Execution Engine

Implements the spawn_subagent tool. A sub-agent gets its own fresh LLM
context window, runs a focused REASON→ACT loop, and returns a summarized
result to the head agent.

This is the primary context management strategy: instead of truncating,
delegate to a sub-agent that has its own 128k window.
"""

from __future__ import annotations

from typing import Any

from ..llm.client import LLMClient
from ..llm.format import Message
from ..log import CycleLogger
from .registry import ToolRegistry


SUBAGENT_SYSTEM_PROMPT = """reasoning: high

You are a focused sub-agent working on a specific task delegated by the head agent.

## Rules
- Use your tools to accomplish the task. Be thorough and systematic.
- When done, provide a clear, structured summary of findings in your final response.
- Include specific data, names, numbers, and URLs — not just conclusions.
- If you find nothing useful, say so clearly rather than fabricating results.
- Do NOT attempt to modify agent code, prompts, or configuration.
- Do NOT create new goals or modify existing ones.
- Focus only on the task you were given.

## Parent Context
The head agent is pursuing a larger mission. Your task is one piece of that work. Return results that the head agent can act on.
"""


async def run_subagent(
    task: str,
    context: str,
    allowed_tools: list[str],
    max_steps: int,
    llm_client: LLMClient,
    registry: ToolRegistry,
    logger: CycleLogger,
) -> str:
    """
    Execute a sub-agent with its own LLM context.

    Args:
        task: What the sub-agent should accomplish
        context: Relevant context from the head agent
        allowed_tools: Tool names the sub-agent can use
        max_steps: Maximum tool call iterations
        llm_client: The LLM client (shared, but each call is independent)
        registry: Tool registry for resolving handlers
        logger: Cycle logger for forensics

    Returns:
        The sub-agent's final summary as a string.
    """
    logger.log("subagent_start", task=task, tools=allowed_tools, max_steps=max_steps)

    # Build sub-agent's conversation: system (instructions+tools) + user (task+context)
    # Same two-message pattern as the head agent.
    system_parts = [SUBAGENT_SYSTEM_PROMPT]

    # Add tool definitions for allowed tools only
    tool_defs = []
    for name in allowed_tools:
        defn = registry.get_definition(name)
        if defn:
            tool_defs.append({
                "name": defn.name,
                "description": defn.description,
                "parameters": [
                    {"name": p.name, "type": p.type, "description": p.description, "required": p.required}
                    for p in defn.parameters
                ],
                "return_type": defn.return_type,
            })

    if tool_defs:
        from ..llm.format import format_tool_definitions
        from ..prompt.templates import TOOL_CALLING_INSTRUCTIONS
        system_parts.append(format_tool_definitions(tool_defs))
        system_parts.append(TOOL_CALLING_INSTRUCTIONS)

    # Task message with context
    task_content = f"## Task\n{task}"
    if context:
        task_content += f"\n\n## Context from Head Agent\n{context}"
    task_content += '\n\nComplete this task using your available tools. Output one JSON object: {"actions": [...]}'

    messages = [
        Message(role="system", content="\n\n".join(system_parts)),
        Message(role="user", content=task_content),
    ]

    # Create a filtered executor that only allows the specified tools
    async def filtered_executor(tool_name: str, arguments: dict) -> Any:
        if tool_name not in allowed_tools:
            return f"Tool '{tool_name}' is not available to this sub-agent. Available: {allowed_tools}"

        handler = registry.get_handler(tool_name)
        if handler is None:
            return f"Unknown tool: {tool_name}"

        return await handler(arguments)

    # Run the sub-agent's REASON→ACT loop
    try:
        final_response, _conversation = await llm_client.reason_with_tools(
            messages=messages,
            tool_executor=filtered_executor,
            purpose="subagent",
            max_steps=max_steps,
        )

        logger.log("subagent_complete", task=task, result_length=len(final_response))
        return final_response

    except Exception as e:
        error_msg = f"Sub-agent failed: {type(e).__name__}: {e}"
        logger.log_error(error_msg, task=task)
        return error_msg
