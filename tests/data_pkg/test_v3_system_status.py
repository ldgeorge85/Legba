# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the System Status routes on the v3 telemetry API.

Covers the two read routes added to :mod:`legba.data.registry.v3_api`:

  * ``GET /api/v1/v3/system/analyst-cadence`` -> ``AnalystCadenceRow``
  * ``GET /api/v1/v3/system/source-firing``   -> ``SourceFiringRow``

These are pure registration + model-shape tests: ``build_v3_router`` only
touches ``deps`` inside the async handlers (lazily), so the router can be
constructed against a trivial stub and its registered paths introspected
without a live substrate. The status-derivation thresholds (the load-bearing
contract) are asserted directly on the pydantic models.
"""

from __future__ import annotations

from datetime import datetime, timezone

from legba.data.registry.v3_api import (
    AnalystCadenceRow,
    SourceFiringRow,
    build_v3_router,
)


def test_system_routes_registered() -> None:
    """Both /system/* routes register on the v3 router (resolve under the
    /api/v1/v3 mount prefix the panel polls)."""
    router = build_v3_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/system/analyst-cadence" in paths
    assert "/system/source-firing" in paths


def test_analyst_cadence_row_status_values() -> None:
    """never (no run) / stale (>6h) / healthy (<=6h) per the contract."""
    now = datetime.now(timezone.utc)

    never = AnalystCadenceRow(analyst_id="a", age_seconds=None, status="never")
    assert never.status == "never"
    assert never.last_run_at is None

    healthy = AnalystCadenceRow(
        analyst_id="b",
        last_run_at=now,
        age_seconds=120,
        runs_1h=3,
        runs_24h=40,
        last_outcome="success",
        status="healthy",
    )
    assert healthy.status == "healthy"

    # 21600s == 6h is the boundary; > 21600 is stale.
    stale = AnalystCadenceRow(
        analyst_id="c", age_seconds=21601, status="stale",
    )
    assert stale.status == "stale"


def test_source_firing_row_status_values() -> None:
    """firing / silent / error / paused per the contract."""
    now = datetime.now(timezone.utc)

    firing = SourceFiringRow(
        source_id="source.x",
        state="active",
        signals_24h=100,
        signals_7d=700,
        last_seen_at=now,
        age_seconds=60,
        status="firing",
    )
    assert firing.status == "firing"

    silent = SourceFiringRow(
        source_id="source.y", state="active", signals_24h=0, status="silent",
    )
    assert silent.status == "silent"

    errored = SourceFiringRow(
        source_id="source.z",
        state="active",
        signals_24h=5,
        recent_error_count=3,
        last_poll_outcome="error",
        status="error",
    )
    assert errored.status == "error"

    paused = SourceFiringRow(
        source_id="source.w", state="paused", status="paused",
    )
    assert paused.status == "paused"


def test_row_defaults_are_panel_safe() -> None:
    """Sparse rows (analyst with a row but no recent runs / source with only
    a descriptor head) construct with sane zero/None defaults so the panel
    never sees missing fields."""
    cad = AnalystCadenceRow(analyst_id="lonely")
    assert cad.runs_1h == 0 and cad.runs_24h == 0
    assert cad.last_run_at is None and cad.age_seconds is None
    assert cad.status == "never"

    src = SourceFiringRow(source_id="source.bare")
    assert src.signals_24h == 0 and src.signals_7d == 0
    assert src.recent_error_count == 0
    assert src.state is None and src.last_seen_at is None
    assert src.status == "silent"
