# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The series tools are reachable through the consult ROUTER.

``series_history`` / ``series_compare`` were on the consult whitelist
(``_KNOWN_TOOLS``) and in the substrate_read pack, but
``consult_tool_routing._dispatch_tool`` had no branch for them: every call
through the router (the harness binding the bake-off drives, any hand-built
deps) fell through to ``{"error": "unreachable"}`` — deviation D22, 6 of 6
calls failed. The governed path (``AgencyToolBinding`` -> the pack handler)
always reached them; ``tests/data_pkg/agency/test_series_tools.py`` proves that
path against a real database, including through ``_run_one_call``.

Pinned here, no database:

* every whitelisted name has a router branch (the class of defect, not the
  instance — a name on ``_KNOWN_TOOLS`` with no branch is caught by name);
* the router and the pack handler call the port with IDENTICAL kwargs for the
  same planner args, so the two dispatch paths cannot drift;
* the consult's own per-call entry point (``_run_one_call``) answers through a
  binding of the harness's shape (a ``run_tool`` that delegates to the router).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from legba.data.analysts.agency.substrate_read import (
    SUBSTRATE_READ_PACK_ID,
    SUBSTRATE_READ_TOOLS,
    series_compare_tool,
    series_history_tool,
)
from legba.data.analysts.agency.tools import ToolCall, ToolContext, ToolResult
from legba.data.analysts.consult_tool_routing import (
    _KNOWN_TOOLS,
    _dispatch_tool,
    _run_one_call,
)

_ROW = {
    "observation_id": "11111111-1111-4111-8111-111111111111",
    "ref": "observation:11111111-1111-4111-8111-111111111111",
    "valid_from": "2016-01-01",
    "value_display": "2.8",
}


class _AnyPort:
    """A port whose every method records its call and answers with one row."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def _method(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, dict(kwargs)))
            return {"rows": [dict(_ROW)], "refs": [_ROW["observation_id"]], "count": 1}

        return _method


# ---------------------------------------------------------------------------
# The class of defect: a whitelisted name with no branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_KNOWN_TOOLS))
async def test_every_whitelisted_tool_has_a_router_branch(name: str) -> None:
    port = _AnyPort()
    out = await _dispatch_tool(port, name=name, args={}, scope_predicate=None)
    assert out.get("error") != "unreachable", name
    assert not str(out.get("error", "")).startswith("unknown_tool"), name
    assert port.calls, f"{name} never reached the port"


def test_the_series_tools_are_pack_tools() -> None:
    """Consult tools must be pack tools: the governed consult loop blocks any
    name the substrate_read pack does not carry."""
    assert {"series_history", "series_compare"} <= set(SUBSTRATE_READ_TOOLS)
    assert {"series_history", "series_compare"} <= set(_KNOWN_TOOLS)


# ---------------------------------------------------------------------------
# Parity: the router and the pack handler call the port the same way
# ---------------------------------------------------------------------------

_HISTORY_ARGS = [
    {"series_id": "bis.cbpol.us", "subject": "US", "from": "2016", "to": "2025",
     "as_of": "2024-01-01", "limit": 50},
    {"series_id": "s", "subject": "US", "since": "2016-01-01", "until": "2025-12-31"},
    {"series_id": "s", "subject": "US", "from": "2016", "to": "2025",
     "collection_id": "wb_wdi"},
    {},
]
_COMPARE_ARGS = [
    {"series_id": "s", "subjects": ["US", "IL"], "from": "2016", "to": "2025"},
    {"series_id": "s", "subjects": "US, IL", "since": "2016", "until": "2025",
     "as_of": "2024-01-01", "limit": 10},
    {"series_id": "s", "subjects": {"nope": 1}, "from": "2016", "to": "2025"},
    {},
]


async def _via_pack(handler, name: str, args: dict[str, Any]) -> tuple[ToolResult, _AnyPort]:
    port = _AnyPort()
    call = ToolCall(pack_id=SUBSTRATE_READ_PACK_ID, tool_name=name, args=dict(args))
    result = await handler(call, None, ToolContext(substrate=port))
    return result, port


async def _via_router(name: str, args: dict[str, Any]) -> tuple[dict[str, Any], _AnyPort]:
    port = _AnyPort()
    out = await _dispatch_tool(port, name=name, args=dict(args), scope_predicate=None)
    return out, port


@pytest.mark.asyncio
@pytest.mark.parametrize("args", _HISTORY_ARGS)
async def test_series_history_router_matches_the_pack_handler(args: dict[str, Any]) -> None:
    result, pack_port = await _via_pack(series_history_tool, "series_history", args)
    out, router_port = await _via_router("series_history", args)
    assert result.status == "completed"
    assert "error" not in out
    assert router_port.calls == pack_port.calls
    assert out == dict(result.output)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", _COMPARE_ARGS)
async def test_series_compare_router_matches_the_pack_handler(args: dict[str, Any]) -> None:
    result, pack_port = await _via_pack(series_compare_tool, "series_compare", args)
    out, router_port = await _via_router("series_compare", args)
    assert result.status == "completed"
    assert "error" not in out
    assert router_port.calls == pack_port.calls


@pytest.mark.asyncio
async def test_the_router_keeps_a_malformed_subjects_value_empty() -> None:
    out, port = await _via_router(
        "series_compare", {"series_id": "s", "subjects": 7, "from": "2016", "to": "2025"},
    )
    assert port.calls[0][1]["subjects"] == []


# ---------------------------------------------------------------------------
# Through the consult's own per-call entry, with a harness-shaped binding
# ---------------------------------------------------------------------------


class _RouterBinding:
    """The shape the bake-off harness binds (D15): ``run_tool`` delegates to
    the production router and wraps the result as an admitted outcome."""

    def __init__(self, port: Any) -> None:
        self.port = port

    async def run_tool(self, name: str, args: dict[str, Any]) -> Any:
        scope = args.pop("scope_predicate", None)
        out = await _dispatch_tool(self.port, name=name, args=args, scope_predicate=scope)
        status = "failed" if "error" in out else "completed"
        return SimpleNamespace(
            admitted=True, block_cause=None, detail=None,
            tool_result=SimpleNamespace(status=status, output=out, error=out.get("error")),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("series_history", {"series_id": "bis.cbpol.us", "subject": "US",
                        "from": "2024-01-01", "to": "2026-11-01"}),
    ("series_compare", {"series_id": "bis.cbpol.us", "subjects": ["US", "GB"],
                        "from": "2024-01-01", "to": "2026-11-01"}),
])
async def test_run_one_call_answers_series_reads_through_a_router_binding(
    name: str, args: dict[str, Any],
) -> None:
    port = _AnyPort()
    deps = SimpleNamespace(agency_binding=_RouterBinding(port), substrate=port)
    result, meta = await _run_one_call(
        deps, tool_name=name, tool_args=args, scope_predicate=None, analyst_id="consult",
    )
    assert "error" not in result, result
    assert result["count"] == 1
    assert meta == {"governed": True, "admitted": True}
    assert [c[0] for c in port.calls] == [name]


@pytest.mark.asyncio
async def test_run_one_call_ungoverned_reaches_series_history() -> None:
    """Hand-built deps with no binding dispatch straight at the port through
    the router — the path that returned ``unreachable`` before."""
    port = _AnyPort()
    deps = SimpleNamespace(agency_binding=None, substrate=port)
    result, meta = await _run_one_call(
        deps, tool_name="series_history",
        tool_args={"series_id": "s", "subject": "US", "from": "2016", "to": "2025"},
        scope_predicate=None, analyst_id="consult",
    )
    assert result["count"] == 1
    assert meta == {"governed": False, "admitted": None}
