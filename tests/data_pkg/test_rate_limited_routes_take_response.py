# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every rate-limited registry route declares ``response: Response``.

The registry's limiter is slowapi with ``headers_enabled=True``. When a
decorated endpoint returns a model rather than a ``Response``, slowapi writes
the rate-limit headers onto the ``response`` keyword argument — and raises
when the endpoint has none, so every successful call 500s. The gate image
does not ship slowapi (the no-op limiter runs), so only this static guard can
catch it: POST /consult/runs/{id}/synthesize shipped without the parameter
(found in a review of the rate-limited routes).
"""
from __future__ import annotations

import ast
from pathlib import Path

_REGISTRY = Path(__file__).resolve().parents[2] / "src" / "legba" / "data" / "registry"


def _limited_routes():
    for path in sorted(_REGISTRY.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                text = ast.unparse(dec)
                if "limiter" in text.lower() and ".limit(" in text:
                    yield path, node
                    break


def test_there_are_rate_limited_routes_to_check():
    assert any(True for _ in _limited_routes())


def test_every_rate_limited_route_takes_a_response_parameter():
    missing = [
        f"{path.relative_to(_REGISTRY)}:{node.lineno} {node.name}"
        for path, node in _limited_routes()
        if "response" not in [a.arg for a in node.args.args + node.args.kwonlyargs]
    ]
    assert missing == [], (
        "rate-limited routes without `response: Response` (slowapi raises on "
        f"every successful call): {missing}"
    )
