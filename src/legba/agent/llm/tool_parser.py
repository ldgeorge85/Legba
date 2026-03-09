"""
Tool Call Parser — JSON Format

Parses tool invocations from LLM output. The model produces tool calls
wrapped in a single JSON object:

    {"actions": [{"tool": "name", "args": {"p": "v"}}, ...]}

Also supports bare {"tool": ...} objects for backward compatibility.
"""

from __future__ import annotations

import ast
import json
import re

from ...shared.schemas.tools import ToolCall


# Pattern: beginning of a JSON tool call — used to find candidates
_TOOL_START_PATTERN = re.compile(r'\{\s*"tool"\s*:')

# Pattern: actions wrapper — the primary expected format
_ACTIONS_PATTERN = re.compile(r'\{\s*"actions"\s*:')

# Fallback: legacy to=functions.NAME pattern (for transition period)
_LEGACY_ROUTE_PATTERN = re.compile(r'to=functions\.(\w+)\s*(?:json\s*)?(\{.*)', re.DOTALL)


def _extract_tool_call(parsed: dict, raw: str) -> ToolCall | None:
    """Convert a parsed dict with 'tool' key to a ToolCall."""
    if not parsed or "tool" not in parsed:
        return None
    tool_name = str(parsed["tool"])
    arguments = parsed.get("args", {})
    if not isinstance(arguments, dict):
        arguments = {"value": arguments}
    return ToolCall(tool_name=tool_name, arguments=arguments, raw_text=raw)


def parse_tool_calls(text: str) -> list[ToolCall]:
    """
    Parse tool calls from LLM output.

    Primary format: {"actions": [{"tool": "name", "args": {...}}, ...]}
    Fallback: bare {"tool": "name", "args": {...}} objects (backward compat).
    """
    calls = []

    # Strategy 1: {"actions": [...]} wrapper (preferred — single JSON object)
    for match in _ACTIONS_PATTERN.finditer(text):
        start = match.start()
        raw = _extract_balanced_braces(text[start:])
        if not raw or not raw.endswith("}"):
            continue
        parsed = _parse_json_safe(raw)
        if parsed and "actions" in parsed and isinstance(parsed["actions"], list):
            for action in parsed["actions"]:
                if isinstance(action, dict):
                    tc = _extract_tool_call(action, json.dumps(action))
                    if tc:
                        calls.append(tc)
            if calls:
                return calls

    # Strategy 2: Bare {"tool": ...} objects (backward compatibility)
    for match in _TOOL_START_PATTERN.finditer(text):
        start = match.start()
        raw = _extract_balanced_braces(text[start:])
        if not raw or not raw.endswith("}"):
            continue
        parsed = _parse_json_safe(raw)
        tc = _extract_tool_call(parsed, raw)
        if tc:
            calls.append(tc)

    if calls:
        return calls

    # Strategy 3: Legacy to=functions.NAME json{...} format
    for match in _LEGACY_ROUTE_PATTERN.finditer(text):
        tool_name = _clean_tool_name(match.group(1))
        json_text = match.group(2).strip()
        arguments = _parse_json_safe(_extract_balanced_braces(json_text))
        calls.append(ToolCall(
            tool_name=tool_name,
            arguments=arguments,
            raw_text=match.group(0),
        ))

    return calls


def has_tool_call(text: str) -> bool:
    """Quick check if text likely contains a tool call."""
    return '"actions"' in text or '"tool"' in text or "to=functions." in text


def _clean_tool_name(name: str) -> str:
    """Clean a tool name that may have 'json' merged into it."""
    if name.endswith("json") and len(name) > 4:
        return name[:-4]
    return name


def _parse_json_safe(text: str) -> dict:
    """Parse JSON, returning empty dict on failure."""
    if not text:
        return {}

    # Clean up common issues
    for token in ["<|end|>", "<|call|>", "<|return|>"]:
        text = text.replace(token, "")
    text = text.strip()

    # Try JSON
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {"value": result}
    except (json.JSONDecodeError, ValueError):
        pass

    # Try Python dict literal (single quotes)
    try:
        result = ast.literal_eval(text)
        if isinstance(result, dict):
            return {k: ("..." if v is ... else v) for k, v in result.items()}
    except (ValueError, SyntaxError):
        pass

    return {"_raw": text}


def _extract_balanced_braces(text: str) -> str:
    """Extract the first balanced {...} from text."""
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    in_string = False
    escape_next = False
    quote_char = None
    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if in_string:
            if ch == quote_char:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote_char = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text
