# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Piece 3, Task A4 — READ_SLICE source-analyst resolution for the meta kinds.

Before Piece 3, both meta kinds resolved their source-analyst set from
``descriptor.subscription.targets.id_list`` — a field that does not exist on
:class:`legba.data.schemas.analyst.SubscriptionTargets`. The result was always
``ids = []``: the synthesizer short-circuited to ``[]`` (permanent NOOP) and the
correlator silently fell back to scanning the WHOLE output pool instead of the
declared set. Task A swaps the resolution to the documented surface,
``subscription.other_analysts[].id``, and honors ``other_analysts[].time_window``.

These tests assert, via a fake ``conn`` that captures the SQL params:
  * other_analysts ids drive ``analyst_id = ANY([...])``;
  * empty other_analysts → the synth short-circuits to ``[]`` (no DB scan);
  * empty other_analysts → the correlator's GLOBAL fallback (whole-pool) runs;
  * non-empty other_analysts → the correlator's scoped query runs (NOT the
    global fallback);
  * ``time_window`` "336h" on other_analysts[] flows through as 336.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from legba.data.analysts import cross_analyst_correlator as corr
from legba.data.analysts import meta_findings_synthesizer as synth


class _CapturingConn:
    """Fake asyncpg.Connection that records the last fetch() call's params."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return list(self._rows)


def _descriptor(others: list[tuple[str, str]] | None) -> SimpleNamespace:
    """Build a descriptor stub with subscription.other_analysts entries.

    ``others`` is a list of (id, time_window) pairs; ``None`` → no subscription.
    """
    if others is None:
        return SimpleNamespace(subscription=None)
    entries = [SimpleNamespace(id=i, time_window=w, data_types=[]) for i, w in others]
    return SimpleNamespace(
        subscription=SimpleNamespace(other_analysts=entries, targets=None)
    )


# ---------------------------------------------------------------------------
# meta_findings_synthesizer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synth_read_slice_resolves_other_analysts_ids():
    desc = _descriptor([("country_assessor", "24h"), ("world_assessor", "24h")])
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)
    # Exactly one query fired; first param is the analyst id list.
    assert len(conn.calls) == 1
    query, params = conn.calls[0]
    assert "analyst_id = ANY($1::TEXT[])" in query
    assert params[0] == ["country_assessor", "world_assessor"]
    # Default 24h window flows through as the second param.
    assert params[1] == 24


@pytest.mark.asyncio
async def test_synth_read_slice_empty_other_analysts_short_circuits():
    """No other_analysts → ids=[] → read_other_analyst_findings refuses the
    query (no full-table scan). The fake conn's fetch must NEVER be hit."""
    desc = _descriptor([])
    conn = _CapturingConn(rows=[{"id": "should-not-appear"}])
    rows = await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)
    assert rows == []
    assert conn.calls == []  # short-circuit: no DB query


@pytest.mark.asyncio
async def test_synth_read_slice_honors_declared_time_window():
    """A 336h window on other_analysts[] flows through as time_window_hours."""
    desc = _descriptor([("country_predictor", "336h")])
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)
    _, params = conn.calls[0]
    assert params[1] == 336


@pytest.mark.asyncio
async def test_synth_read_slice_explicit_analyst_ids_override():
    """The analyst_ids= override (test/direct-caller path) wins over descriptor."""
    desc = _descriptor([("country_assessor", "24h")])
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(
        conn, descriptor=desc, target_filter=None, analyst_ids=["explicit_a", "explicit_b"]
    )
    _, params = conn.calls[0]
    assert params[0] == ["explicit_a", "explicit_b"]


# ---------------------------------------------------------------------------
# cross_analyst_correlator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correlator_read_slice_scopes_to_other_analysts_not_global():
    """Non-empty other_analysts → the SCOPED query runs (analyst_id filter),
    NOT the global whole-pool fallback."""
    desc = _descriptor([("country_assessor", "24h"), ("country_predictor", "24h")])
    conn = _CapturingConn(rows=[])
    await corr.READ_SLICE(conn, descriptor=desc, target_filter=None)
    assert len(conn.calls) == 1
    query, params = conn.calls[0]
    # Scoped: the analyst_id filter is present and carries the declared set.
    assert "analyst_id = ANY($1::TEXT[])" in query
    assert params[0] == ["country_assessor", "country_predictor"]


@pytest.mark.asyncio
async def test_correlator_read_slice_empty_other_analysts_global_fallback():
    """Empty other_analysts → the correlator's GLOBAL fallback runs (it is
    allowed to read across the whole output stream, unlike the synth). The
    fallback query has NO analyst_id filter; the window is the first param."""
    desc = _descriptor([])
    conn = _CapturingConn(rows=[])
    await corr.READ_SLICE(conn, descriptor=desc, target_filter=None)
    assert len(conn.calls) == 1
    query, params = conn.calls[0]
    assert "analyst_id = ANY" not in query
    assert params[0] == 24  # window is the only param in the fallback query


@pytest.mark.asyncio
async def test_correlator_read_slice_honors_declared_time_window():
    desc = _descriptor([("country_assessor", "336h")])
    conn = _CapturingConn(rows=[])
    await corr.READ_SLICE(conn, descriptor=desc, target_filter=None)
    _, params = conn.calls[0]
    # scoped query: window is the SECOND param
    assert params[1] == 336
