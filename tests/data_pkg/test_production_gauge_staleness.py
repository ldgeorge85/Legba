# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CADENCE-STALENESS gauge (FRAME-1 §6, 2026-08-20).

This loop exists because on 2026-08-20 every instrument in the engine was
green while the Burkina Faso country read was composed over 42-hour-old unit
heads. The composition ran on time (``analyst_cadence`` green), it wrote a row
(``analyst_production`` green), the units were alive and simply had nothing new
to analyse — the trigger kernel's cadence gate fires only on a dirty window,
which is correct economics and is NOT changed here. The one thing missing was
that the layer consuming those heads had no idea how old they were.

The load-bearing assertions: 42h of head silence PAGES; a normal overnight
quiet does NOT; a composition that published no head-age stamp is UNGAUGED and
never green; and the loop is dynamic, so an engine whose compositions predate
FRAME-1 contributes no rows at all rather than a fleet of false reassurances.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from legba.data.analysts.handler_options import HANDLER_OPTIONS
from legba.data.registry.production_gauge import (
    LOOP_CLASSES,
    LOOP_DESK_HEAD_STALENESS,
    GaugeConfig,
)
from legba.data.registry.production_gauge_staleness import (
    QUIET_NO_STAMP,
    QUIET_STALENESS_QUERY_FAILED,
    desk_head_staleness_gauge,
    read_staleness_loops,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
CFG = GaugeConfig()


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "analyst_id": "country_composition",
        "target_id": "country_watch_bf",
        "produced_at": NOW,
        "max_head_age_h": 6.0,
        "min_head_age_h": 2.0,
        "horizon_h": 336.0,
        "head_count": 7,
    }
    row.update(over)
    return row


class _FetchConn:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return list(self._rows)


class _BrokenConn:
    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        raise RuntimeError("relation analyst_outputs does not exist")


# ---------------------------------------------------------------------------
# The judgment
# ---------------------------------------------------------------------------


def test_the_bf_stall_pages():
    """THE §6 assertion: 42h of desk silence, consumed by a composition that
    ran perfectly on time, must reach the operator."""
    gauge = desk_head_staleness_gauge(_row(max_head_age_h=42.0), now=NOW, cfg=CFG)
    assert gauge.state == "deficit"
    assert gauge.pages
    assert gauge.ratio == pytest.approx(42.0 / 34.0, rel=1e-3)
    assert gauge.loop_id == "country_composition:country_watch_bf"
    assert "42h old" in gauge.actual
    assert gauge.evidence["heads_consumed"] == 7
    assert gauge.evidence["horizon_h"] == pytest.approx(336.0)


def test_a_normal_overnight_quiet_does_not_page():
    """The precision knob. A unit that last fired 13h ago (inside its own 11h
    cooldown at the previous compose) is ORDINARY, not a page."""
    gauge = desk_head_staleness_gauge(_row(max_head_age_h=13.0), now=NOW, cfg=CFG)
    assert gauge.state == "ok"
    assert not gauge.pages
    assert gauge.severity == "info"


def test_the_bar_is_two_unit_cooldowns_plus_fallback_slack():
    assert CFG.staleness_max_head_age_hours == 34.0
    assert desk_head_staleness_gauge(
        _row(max_head_age_h=33.9), now=NOW, cfg=CFG
    ).state == "ok"
    assert desk_head_staleness_gauge(
        _row(max_head_age_h=34.0), now=NOW, cfg=CFG
    ).state == "deficit"


def test_severity_rides_the_shared_ramp():
    assert desk_head_staleness_gauge(
        _row(max_head_age_h=40.0), now=NOW, cfg=CFG
    ).severity == "medium"
    assert desk_head_staleness_gauge(
        _row(max_head_age_h=80.0), now=NOW, cfg=CFG
    ).severity == "high"
    assert desk_head_staleness_gauge(
        _row(max_head_age_h=200.0), now=NOW, cfg=CFG
    ).severity == "critical"


def test_a_composition_without_the_stamp_is_ungauged_never_green():
    """"We cannot say" and "it is fine" are different statements — blurring
    them is how the twelve dead limbs went unnoticed."""
    gauge = desk_head_staleness_gauge(_row(max_head_age_h=None), now=NOW, cfg=CFG)
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == QUIET_NO_STAMP
    assert gauge.ratio is None
    assert not gauge.pages


def test_a_malformed_stamp_is_ungauged_not_a_zero():
    gauge = desk_head_staleness_gauge(
        _row(max_head_age_h="not-a-number"), now=NOW, cfg=CFG
    )
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == QUIET_NO_STAMP


def test_a_world_head_with_no_target_reads_as_world():
    gauge = desk_head_staleness_gauge(
        _row(analyst_id="world_assessor", target_id=None, max_head_age_h=5.0),
        now=NOW,
        cfg=CFG,
    )
    assert gauge.loop_id == "world_assessor:world"
    assert "world" in gauge.label


def test_the_operator_can_retune_the_bar():
    cfg = GaugeConfig(staleness_max_head_age_hours=72.0)
    assert desk_head_staleness_gauge(
        _row(max_head_age_h=42.0), now=NOW, cfg=cfg
    ).state == "ok"


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_query_asks_only_for_stamped_live_heads():
    conn = _FetchConn(rows=[])
    await read_staleness_loops(conn, now=NOW, cfg=CFG)
    query, params = conn.calls[0]
    assert "head_ages" in query
    assert "f.superseded_by IS NULL" in query
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in query
    # The window is the SHORT acute one, not the composition's 336h horizon.
    assert params[0] == NOW - __import__("datetime").timedelta(
        hours=CFG.staleness_window_hours
    )


@pytest.mark.asyncio
async def test_an_engine_with_no_stamped_heads_contributes_no_rows():
    """DYNAMIC like ``llm_daily_burn``: silence about what cannot be measured,
    never a fleet of green rows that predate the stamp."""
    assert await read_staleness_loops(_FetchConn(rows=[]), now=NOW, cfg=CFG) == []


@pytest.mark.asyncio
async def test_one_row_per_stamped_composition_head():
    conn = _FetchConn(
        rows=[
            _row(target_id="country_watch_bf", max_head_age_h=42.0),
            _row(target_id="country_g20_us", max_head_age_h=4.0),
        ]
    )
    loops = await read_staleness_loops(conn, now=NOW, cfg=CFG)
    assert [g.state for g in loops] == ["deficit", "ok"]
    assert all(g.loop_class == LOOP_DESK_HEAD_STALENESS for g in loops)


@pytest.mark.asyncio
async def test_a_failed_read_degrades_loud_never_as_a_silent_zero():
    loops = await read_staleness_loops(_BrokenConn(), now=NOW, cfg=CFG)
    assert len(loops) == 1
    assert loops[0].state == "ungauged"
    assert loops[0].quiet_reason == QUIET_STALENESS_QUERY_FAILED
    assert "analyst_outputs" in loops[0].evidence["error"]


# ---------------------------------------------------------------------------
# Registration — the contract every S-1 loop family joins on
# ---------------------------------------------------------------------------


def test_the_loop_is_registered_in_the_one_enumeration():
    assert LOOP_DESK_HEAD_STALENESS in LOOP_CLASSES


@pytest.mark.asyncio
async def test_read_gauge_picks_the_family_up():
    """No new wiring: the route, the production_deficit trigger and the ntfy
    fan-out all read ``read_gauge``."""
    from legba.data.registry import production_gauge as pg

    calls: list[str] = []

    async def _spy(conn: Any, *, now: Any = None, cfg: Any = None) -> list[Any]:
        calls.append("staleness")
        return []

    import legba.data.registry.production_gauge_staleness as staleness_mod

    original = staleness_mod.read_staleness_loops
    staleness_mod.read_staleness_loops = _spy  # type: ignore[assignment]
    try:
        class _EmptyConn:
            async def fetch(self, *a: Any, **k: Any) -> list[Any]:
                return []

            async def fetchrow(self, *a: Any, **k: Any) -> None:
                return None

        await pg.read_gauge(_EmptyConn(), now=NOW, cfg=CFG)
    finally:
        staleness_mod.read_staleness_loops = original  # type: ignore[assignment]
    assert calls == ["staleness"]


@pytest.mark.parametrize(
    "field",
    ["staleness_max_head_age_hours", "staleness_window_hours"],
)
def test_every_new_threshold_is_an_operator_knob(field):
    """A GaugeConfig field with no matching ``gauge_``-prefixed OptionSpec is
    silently un-tunable: a descriptor setting it is REJECTED."""
    assert hasattr(GaugeConfig(), field)
    names = {spec.name for spec in HANDLER_OPTIONS["alert_trigger_scan"]}
    assert f"gauge_{field}" in names
