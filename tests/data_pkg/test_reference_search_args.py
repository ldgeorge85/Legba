# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reference builder's web_search never forwards ``time_range``.

The pack's ``web_search`` takes ``query`` and ``limit`` only. The builder's
model-facing schema used to offer an optional ``time_range`` and the wrapper
forwarded it; the agency gate refused every such call as ``invalid_arg``
before any search ran (217 of 267 builder searches over the seven days to
2026-10-05), each refusal counted as a degraded-search strike, and the lane
committed early with nothing — the sixteen "unbuildable" desks and the empty
first build of lane_hormuz. Two guards: the schema carries no ``time_range``,
and a call that still carries one reaches the binding with ``query`` + ``limit``
alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.data.analysts.deterministic_handlers import _reference_loop as L


def test_the_model_facing_search_schema_offers_no_time_range() -> None:
    spec = next(s for s in L.TOOL_SPECS if s.name == "web_search")
    props = spec.json_schema["properties"]
    assert "time_range" not in props
    assert set(props) == {"query"}
    assert spec.json_schema["required"] == ["query"]


class _Binding:
    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(self, tool_name: str, args: dict[str, Any], **_: Any):
        self.invocations.append((tool_name, dict(args)))

        class _R:
            status = "completed"
            result = {"results": []}
            error = None
            admitted = True
            block_cause = None

        return _R()


def _loop_with(binding: _Binding):
    cls = next(
        c for c in vars(L).values()
        if isinstance(c, type) and "web_search" in vars(c)
    )
    loop = object.__new__(cls)
    loop.binding = binding
    loop.queries = None
    loop.searches = []
    loop.search_timeout = 5
    return loop


@pytest.mark.asyncio
async def test_a_time_range_the_model_still_emits_is_dropped_not_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        L.MANIFEST, "apply_query_ledger", lambda _queries, q: (q, []), raising=True,
    )
    b = _Binding()
    loop = _loop_with(b)
    try:
        await loop.web_search("Strait of Hormuz shipping traffic", time_range="month")
    except Exception as exc:  # noqa: BLE001 — the result-shaping after the call is not under test
        if not b.invocations:
            raise AssertionError(f"the binding was never reached: {exc!r}") from exc
    assert b.invocations, "the search never reached the pack"
    name, args = b.invocations[0]
    assert name == "web_search"
    assert set(args) == {"query", "limit"}, args
    assert args["query"] == "Strait of Hormuz shipping traffic"
    # The ask is still on the ledger, so a trace shows what the model wanted.
    assert any("time_range='month' dropped" in str(e.get("note", "")) for e in loop.searches)


@pytest.mark.asyncio
async def test_the_binding_is_reached_without_a_time_range_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        L.MANIFEST, "apply_query_ledger", lambda _queries, q: (q, []), raising=True,
    )
    b = _Binding()
    loop = _loop_with(b)
    try:
        await loop.web_search("Hormuz")
    except Exception:  # noqa: BLE001 — the result-shaping after the call is not under test
        pass
    assert b.invocations and set(b.invocations[0][1]) == {"query", "limit"}
