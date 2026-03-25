"""
Chat Message Formatter

Converts internal Message objects to message dicts for the LLM provider.

Two formatters:
- to_chat_messages():     vLLM/GPT-OSS — combines all into single user message
- to_anthropic_messages(): Anthropic — proper system/user separation
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class Message:
    """A message in a conversation."""

    role: str  # system, user, assistant
    content: str


def safe_response_body(response: httpx.Response) -> str:
    """Extract response body for error logging, truncated."""
    try:
        return response.text[:1000]
    except Exception:
        return f"(could not read body, status={response.status_code})"


def strip_harmony_response(text: str) -> str:
    """
    Strip Harmony channel markers and reasoning tokens from LLM response.

    GPT-OSS (Harmony format) may produce:
      - <|channel|>final<|message|>...content...<|end|>
      - <|channel|>analysis<|message|>...
      - assistantanalysis...assistantfinal{actual content}
      - Raw content with stray <|...|> tokens
    """
    # Try extracting from <|channel|>final<|message|>...<|end|>
    final_pattern = r'<\|channel\|>final<\|message\|>(.*?)(?:<\|end\|>|<\|return\|>|$)'
    final_matches = re.findall(final_pattern, text, re.DOTALL)
    if final_matches:
        return ' '.join(final_matches).strip()

    # Fallback: extract any <|message|>... content
    message_pattern = r'<\|message\|>(.*?)(?:<\|end\|>|<\|return\|>|$)'
    message_matches = re.findall(message_pattern, text, re.DOTALL)
    if message_matches:
        return ' '.join(message_matches).strip()

    # Try assistantfinal marker (raw completions format)
    marker = "assistantfinal"
    idx = text.rfind(marker)
    if idx >= 0:
        return text[idx + len(marker):].strip()

    # Try assistantanalysis marker
    marker2 = "assistantanalysis"
    idx2 = text.rfind(marker2)
    if idx2 >= 0:
        return text[idx2 + len(marker2):].strip()

    # Clean up any remaining stray tokens
    if '<|' in text:
        return re.sub(r'<\|[^|]+\|>', '', text).strip()

    return text


def to_chat_messages(messages: list[Message]) -> list[dict[str, str]]:
    """
    Convert Message objects to message dicts for vLLM/GPT-OSS.

    Combines all messages into a single "user" message — GPT-OSS
    doesn't handle the system role reliably via chat/completions.
    """
    combined = "\n\n".join(m.content for m in messages)
    return [{"role": "user", "content": combined}]


def _strip_reasoning_directive(text: str) -> str:
    """Remove GPT-OSS reasoning level directives (e.g., 'reasoning: high')."""
    return re.sub(r'^reasoning:\s*(high|medium|low)\s*\n*', '', text, flags=re.MULTILINE).lstrip()


def to_anthropic_messages(messages: list[Message]) -> tuple[str, list[dict[str, str]]]:
    """
    Convert Message objects for the Anthropic Messages API.

    Returns (system_text, messages_list) where:
    - system_text: combined system messages (goes to top-level 'system' field)
    - messages_list: user/assistant messages (goes to 'messages' field)

    Strips GPT-OSS specific directives (reasoning: high/medium/low).
    """
    system_parts = []
    chat_messages = []

    for m in messages:
        if m.role == "system":
            system_parts.append(_strip_reasoning_directive(m.content))
        else:
            chat_messages.append({
                "role": m.role,
                "content": _strip_reasoning_directive(m.content),
            })

    # Anthropic requires first message to be user role
    if not chat_messages or chat_messages[0]["role"] != "user":
        chat_messages.insert(0, {"role": "user", "content": "(context follows)"})

    system_text = "\n\n".join(system_parts)
    return system_text, chat_messages


def format_tool_result(tool_name: str, result: str) -> str:
    """Format a tool result as a text block (no longer returns Message)."""
    return f"[Tool Result: {tool_name}]\n{result}"


def format_tool_definitions(
    tools: list[dict[str, Any]],
    only: set[str] | None = None,
) -> str:
    """
    Format tool definitions as a JSON block.

    If *only* is provided, only those tools get full parameter details.
    All other tools are listed as name + description only.
    """
    import json

    full_tools = []
    summary_tools = []

    for tool in tools:
        if only is not None and tool["name"] not in only:
            summary_tools.append(tool)
            continue
        params = []
        for p in tool.get("parameters", []):
            param = {
                "name": p["name"],
                "type": p.get("type", "string"),
                "required": p.get("required", True),
            }
            if p.get("description"):
                param["description"] = p["description"]
            params.append(param)
        full_tools.append({
            "name": tool["name"],
            "description": tool["description"],
            "parameters": params,
        })

    parts = ["# Tools"]

    if full_tools:
        parts.append("```json\n" + json.dumps({"tools": full_tools}, indent=2) + "\n```")

    if summary_tools:
        parts.append("## Other Available Tools")
        parts.append("Use `explain_tool` to get full parameter details for any of these.")
        for t in summary_tools:
            parts.append(f"- **{t['name']}**: {t['description']}")

    return "\n".join(parts)


def format_tool_summary(tools: list[dict[str, Any]]) -> str:
    """Compact tool list: name + description only. Used in PLAN phase."""
    lines = ["## Available Tools"]
    for tool in tools:
        lines.append(f"- **{tool['name']}**: {tool['description']}")
    return "\n".join(lines)
