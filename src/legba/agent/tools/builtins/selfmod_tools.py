"""
Self-modification tools: code_test

Provides the agent with a way to validate code before committing
self-modifications. Runs syntax check + import validation.
"""

from __future__ import annotations

from typing import Any

from ....shared.schemas.tools import ToolDefinition, ToolParameter


CODE_TEST_DEF = ToolDefinition(
    name="code_test",
    description=(
        "Test a Python file or code snippet for syntax errors and import failures. "
        "Use BEFORE applying self-modifications to catch bugs early. "
        "Returns 'PASS' or a description of errors found."
    ),
    parameters=[
        ToolParameter(name="file_path", type="string",
                      description="Path to the Python file to test (relative to /agent)",
                      required=False),
        ToolParameter(name="code", type="string",
                      description="Raw Python code to syntax-check (if no file_path)",
                      required=False),
    ],
)


async def code_test(args: dict) -> str:
    """
    Validate Python code: compile check + import check.

    This runs in-process so it's fast but limited — it catches syntax
    errors and missing imports, not runtime bugs.
    """
    import ast
    import sys
    import importlib
    from pathlib import Path

    file_path = args.get("file_path")
    code = args.get("code")

    if file_path:
        agent_base = Path("/agent")
        full_path = agent_base / file_path
        if not full_path.exists():
            return f"Error: file not found: {full_path}"
        code = full_path.read_text()

    if not code:
        return "Error: provide either file_path or code."

    errors = []

    # 1. Syntax check via ast.parse
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"FAIL: Syntax error at line {e.lineno}: {e.msg}"

    # 2. Extract imports and check they resolve
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                try:
                    importlib.import_module(alias.name)
                except ImportError:
                    errors.append(f"Import error: '{alias.name}' not found")
                except Exception:
                    pass  # Other errors during import (e.g. side effects) are OK
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # Only check top-level module resolution
                top_module = node.module.split(".")[0]
                if top_module not in sys.modules:
                    try:
                        importlib.import_module(top_module)
                    except ImportError:
                        errors.append(f"Import error: '{node.module}' (top-level '{top_module}' not found)")
                    except Exception:
                        pass

    # 3. Check for basic code quality issues
    # (Very lightweight — just catch obvious problems)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            # Check for unreachable code after return
            for i, stmt in enumerate(node.body):
                if isinstance(stmt, ast.Return) and i < len(node.body) - 1:
                    errors.append(f"Warning: unreachable code after return in '{node.name}' at line {stmt.lineno}")

    if errors:
        return "FAIL:\n" + "\n".join(f"  - {e}" for e in errors)

    line_count = len(code.strip().splitlines())
    return f"PASS: {line_count} lines, syntax OK, imports resolve."


def get_definitions() -> list[tuple[ToolDefinition, Any]]:
    return [(CODE_TEST_DEF, code_test)]


def register(registry, **_deps) -> None:
    """Register self-modification tools."""
    registry.register(CODE_TEST_DEF, code_test)
