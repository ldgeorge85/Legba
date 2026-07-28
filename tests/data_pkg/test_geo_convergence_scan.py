# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A7 — the ``geo_convergence_scan`` deterministic geographic convergence
detector.

Pure tests (no DB): cell/country bin keys, the source-family fold, the score,
the two-tier binning (country-centroid geocodes NEVER cell-binned), and the
formation/dissolution edge core. Ephemeral-DB tests (``migrated_pg``): the
seed-silently → fire-once-on-formation → never-refire → fire-once-on-
dissolution watermark lifecycle against live SQL, and the diversity rule
(a same-family pile-on never fires).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import (
    geo_convergence_scan as gcs,
)
from legba.data.config import PostgresConfig
from legba.data.provenance.kinds import (
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
    OutputKind,
)
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registered_as_finding_sub_handler_and_structural_exempt():
    """The summary is a genuine FINDING (suppressed to trace-only on quiet
    sweeps), so the handler sits in the STRUCTURAL_VERIFY_EXEMPT registry and
    the drift guard's FINDING-set equality holds."""
    assert SUB_HANDLERS["geo_convergence_scan"] is gcs.handle
    assert (
        OUTPUT_KIND_BY_SUB_HANDLER["geo_convergence_scan"]
        is OutputKind.FINDING
    )
    assert "geo_convergence_scan" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


async def test_refuses_loud_without_pool():
    with pytest.raises(RuntimeError, match="pg_pool"):
        await gcs.handle([], {"sub_handler": "geo_convergence_scan"}, None)


# ---------------------------------------------------------------------------
# Pure — bin keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lat,lon,expected",
    [
        (33.31, 44.36, "cell:33:44"),          # Baghdad-ish
        (-23.55, -46.63, "cell:-24:-47"),      # floor, not trunc, on negatives
        (0.0, 0.0, "cell:0:0"),
        (90.0, 180.0, "cell:89:179"),          # top edges fold into last cell
        (-90.0, -180.0, "cell:-90:-180"),
        ("33.5", "44.5", "cell:33:44"),        # numeric strings (JSONB text)
        (91.0, 0.0, None),                     # out of range
        (0.0, -180.1, None),
        (float("nan"), 10.0, None),
        (None, 10.0, None),
        ("junk", 10.0, None),
    ],
)
def test_cell_key(lat, lon, expected):
    assert gcs.cell_key(lat, lon) == expected


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("US", "country:US"),
        ("us", "country:US"),
        (" iq ", "country:IQ"),
        ("USA", None),       # not ISO2
        ("U1", None),
        ("", None),
        (None, None),
        (123, None),
    ],
)
def test_country_key(tag, expected):
    assert gcs.country_key(tag) == expected


# ---------------------------------------------------------------------------
# Pure — source family + score
# ---------------------------------------------------------------------------


def test_source_family_first_tag_wins_with_honest_fallback():
    assert gcs.source_family(["gis", "geospatial", "hazard"], "s") == "gis"
    assert gcs.source_family(["", "news"], "s") == "news"  # skip empties
    # No tags / junk tags → per-source fallback (counts once, never inflates
    # diversity across the source's own signals).
    assert gcs.source_family([], "source.x") == "src:source.x"
    assert gcs.source_family(None, "source.x") == "src:source.x"
    assert gcs.source_family("notalist", "source.x") == "src:source.x"


def test_convergence_score_families_plus_capped_volume_bonus():
    assert gcs.convergence_score(3, 5) == 3      # below the first volume step
    assert gcs.convergence_score(3, 10) == 4     # +1 at 10 signals
    assert gcs.convergence_score(4, 25) == 6     # +2 at 20+
    assert gcs.convergence_score(3, 1000) == 5   # bonus capped at +2
    assert gcs.convergence_score(5, 0) == 5


# ---------------------------------------------------------------------------
# Pure — two-tier binning
# ---------------------------------------------------------------------------

_FAMS = {
    "src.quake": ["gis"],
    "src.news_a": ["news"],
    "src.news_b": ["news"],
    "src.tg": ["social"],
}


def test_build_bins_cells_and_countries():
    point_rows = [
        {"id": "p1", "source_id": "src.quake", "lat": 33.2, "lon": 44.1,
         "iso2": "IQ"},
        {"id": "p2", "source_id": "src.news_a", "lat": 33.9, "lon": 44.9,
         "iso2": "IQ"},
        {"id": "p3", "source_id": "src.tg", "lat": 33.5, "lon": 44.5,
         "iso2": "SY"},
        {"id": "p4", "source_id": "src.news_b", "lat": 10.0, "lon": 10.0,
         "iso2": None},
        {"id": "p5", "source_id": "src.news_b", "lat": "junk", "lon": 44.0,
         "iso2": "IQ"},  # unusable point mints NO bin
    ]
    country_rows = [
        {"id": "p1", "source_id": "src.quake", "country": "IQ"},
        {"id": "c1", "source_id": "src.news_a", "country": "IQ"},
        {"id": "c2", "source_id": "src.tg", "country": "iq"},
        {"id": "c3", "source_id": "src.news_b", "country": "XXL"},  # skipped
    ]
    bins = gcs.build_bins(point_rows, country_rows, _FAMS)

    cell = bins["cell:33:44"]
    assert cell.bin_kind == "cell"
    assert {sid for sid, _, _ in cell.contributors} == {"p1", "p2", "p3"}
    assert cell.families == {"gis", "news", "social"}
    assert cell.country_iso2 == "IQ"  # modal contributor country

    iq = bins["country:IQ"]
    assert iq.bin_kind == "country"
    assert iq.country_iso2 == "IQ"
    assert iq.families == {"gis", "news", "social"}
    assert iq.signal_count == 3

    # The lone p4 point mints its own cell; the junk XXL tag minted nothing.
    assert "cell:10:10" in bins
    assert not any(k.startswith("country:XX") for k in bins)


def test_build_bins_same_family_pileon_stays_one_family():
    """Two different NEWS sources are still ONE family — same-family pile-ons
    can never manufacture diversity."""
    country_rows = [
        {"id": f"c{i}", "source_id": src, "country": "SY"}
        for i, src in enumerate(["src.news_a", "src.news_b"] * 10)
    ]
    bins = gcs.build_bins([], country_rows, _FAMS)
    assert bins["country:SY"].families == {"news"}
    assert bins["country:SY"].signal_count == 20


# ---------------------------------------------------------------------------
# Pure — formation/dissolution edge core
# ---------------------------------------------------------------------------


def test_edge_actions_first_scan_seeds_silently():
    formed, dissolved, seed = gcs.edge_actions(
        False, {}, ["country:US", "cell:33:44"]
    )
    assert formed == [] and dissolved == []
    assert seed == ["cell:33:44", "country:US"]


def test_edge_actions_formation_dissolution_and_steady_state():
    prev = {
        "country:US": {"active": True, "families": ["gis", "news", "social"]},
        "country:SY": {"active": True, "families": ["gis", "news", "osint"]},
        "cell:10:10": {"active": False, "families": []},
    }
    formed, dissolved, seed = gcs.edge_actions(
        True, prev, ["country:US", "cell:10:10", "cell:33:44"]
    )
    # US persists (no refire); cell:10:10 REFORMS from inactive; cell:33:44 is
    # brand new; SY dropped below the bar → one dissolution.
    assert formed == ["cell:10:10", "cell:33:44"]
    assert dissolved == ["country:SY"]
    assert seed == []


def test_bin_label_states_cell_extent_never_a_point():
    assert gcs.bin_label("country:IQ", "IQ") == "IQ"
    assert gcs.bin_label("cell:33:44", "IQ") == "cell(33..34°, 44..45°) IQ"
    assert gcs.bin_label("cell:-24:-47", None) == "cell(-24..-23°, -47..-46°)"


# ---------------------------------------------------------------------------
# Ephemeral-DB rig
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    """Fresh geo_convergence watermarks + no leftover fixture rows, so every
    test gets its own seed → fire cycle."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM alert_trigger_watermarks WHERE trigger_class = $1",
            gcs.TRIGGER_CLASS,
        )
        await conn.execute(
            "DELETE FROM analyst_outputs "
            "WHERE analyst_id = 'geo_convergence_scan'"
        )
        await conn.execute(
            "DELETE FROM signals WHERE source_id LIKE 'src.geotest.%'"
        )
        await conn.execute(
            "DELETE FROM source_descriptors "
            "WHERE descriptor_id LIKE 'src.geotest.%'"
        )
    yield


class _FakeDispatcher:
    def __init__(self) -> None:
        self.payloads: list[Any] = []

    async def fan_out(self, payload: Any) -> list[Any]:
        self.payloads.append(payload)
        return []


class _Deps:
    def __init__(self, pool: Any, dispatcher: Any) -> None:
        self.pg_pool = pool
        self.extras = {"alert_sink_dispatcher": dispatcher}


async def _run(pool: Any, dispatcher: Any | None = None, **opts: Any):
    deps = _Deps(pool, dispatcher if dispatcher is not None else _FakeDispatcher())
    options = {
        "sub_handler": "geo_convergence_scan",
        "analyst_id": "geo_convergence_scan",
        "run_id": str(uuid4()),
        **opts,
    }
    result = await gcs.handle([], options, deps)
    assert isinstance(result, AnalystMethodResult)
    return result


async def _alert_rows(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT id, title, severity, target_id, data "
        "FROM analyst_outputs "
        "WHERE kind = 'alert' AND analyst_id = 'geo_convergence_scan' "
        "ORDER BY produced_at, id"
    )


def _row_data(row: Any) -> dict[str, Any]:
    d = row["data"]
    full = json.loads(d) if isinstance(d, str) else dict(d)
    inner = full.get("data")
    return inner if isinstance(inner, dict) else full


async def _insert_source(conn: Any, source_id: str, family: str) -> None:
    await conn.execute(
        "INSERT INTO source_descriptors (descriptor_id, version, schema_uri, "
        "is_head, kind, state, owner, name, body) "
        "VALUES ($1, 'v0', 'legba/source/1.0.0', TRUE, 'rss', 'active', "
        "'test', $1, $2::jsonb) "
        "ON CONFLICT DO NOTHING",
        source_id,
        json.dumps({"scope": {"tags": [family]}}),
    )


async def _insert_signal(
    conn: Any,
    source_id: str,
    *,
    geo_tags: list[str] | None = None,
    lat: float | None = None,
    lon: float | None = None,
    precision: str | None = None,
    geo_source: str | None = None,
    hours_ago: float = 1.0,
) -> str:
    sid = uuid4()
    payload: dict[str, Any] = {"title": "t"}
    geo_block: dict[str, Any] = {}
    if lat is not None:
        geo_block["lat"] = lat
        geo_block["lon"] = lon
    if precision is not None:
        geo_block["precision"] = precision
    if geo_source is not None:
        geo_block["source"] = geo_source
    if geo_block:
        payload["geo"] = geo_block
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    await conn.execute(
        "INSERT INTO signals (id, source_id, payload, geo, fetched_at) "
        "VALUES ($1, $2, $3::jsonb, $4::text[], $5)",
        sid,
        source_id,
        json.dumps(payload),
        geo_tags or [],
        ts,
    )
    return str(sid)


async def _seed_three_family_sources(conn: Any) -> None:
    await _insert_source(conn, "src.geotest.quake", "gis")
    await _insert_source(conn, "src.geotest.news", "news")
    await _insert_source(conn, "src.geotest.tg", "social")
    await _insert_source(conn, "src.geotest.news2", "news")
    await _insert_source(conn, "src.geotest.health", "health")


# ---------------------------------------------------------------------------
# DB — watermark lifecycle
# ---------------------------------------------------------------------------


async def test_first_scan_seeds_silently_then_steady_state_is_quiet(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        await _seed_three_family_sources(conn)
        for src in ("src.geotest.quake", "src.geotest.news", "src.geotest.tg"):
            await _insert_signal(conn, src, geo_tags=["IQ"])

    r1 = await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _alert_rows(conn) == []  # history never pages
        wm = await conn.fetchrow(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = 'country:IQ'",
            gcs.TRIGGER_CLASS,
        )
    assert wm is not None
    state = json.loads(wm["state"]) if isinstance(wm["state"], str) else wm["state"]
    assert state["active"] is True
    assert state["families"] == ["gis", "news", "social"]
    # The seed run is a REAL finding (it says what was seeded)…
    assert r1.force_trace_only is False
    assert r1.finding.data["seeded"] is True

    # …and the unchanged second scan is trace-only quiet (no refire).
    r2 = await _run(pg_pool)
    assert r2.force_trace_only is True
    assert r2.finding.data["formed_fired"] == 0
    async with pg_pool.acquire() as conn:
        assert await _alert_rows(conn) == []


async def test_formation_fires_once_with_contributors_then_never_refires(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        await _seed_three_family_sources(conn)
        # Below the bar at seed time: two families only.
        await _insert_signal(conn, "src.geotest.quake", geo_tags=["SY"])
        await _insert_signal(conn, "src.geotest.news", geo_tags=["SY"])
    await _run(pg_pool)  # seeds (nothing formed)

    async with pg_pool.acquire() as conn:
        # Third DISTINCT family arrives → formation edge.
        await _insert_signal(conn, "src.geotest.tg", geo_tags=["SY"])
    dispatcher = _FakeDispatcher()
    r2 = await _run(pg_pool, dispatcher)
    assert r2.force_trace_only is False
    assert r2.finding.data["formed_fired"] == 1

    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == "medium"
    data = _row_data(row)
    assert data["event"] == "formed"
    assert data["bin_key"] == "country:SY"
    assert data["distinct_family_count"] == 3
    assert sorted(data["families"]) == ["gis", "news", "social"]
    # Payload lists the contributing signals: ids + sources + families.
    contribs = data["contributing_signals"]
    assert len(contribs) == 3
    assert {c["source_id"] for c in contribs} == {
        "src.geotest.quake", "src.geotest.news", "src.geotest.tg",
    }
    assert all(c["id"] and c["family"] for c in contribs)
    assert len(dispatcher.payloads) == 1  # fanned outward once

    # Persisting convergence: the next scan fires NOTHING.
    r3 = await _run(pg_pool)
    assert r3.force_trace_only is True
    async with pg_pool.acquire() as conn:
        assert len(await _alert_rows(conn)) == 1


async def test_same_family_pileon_never_fires(pg_pool, clean_slate):
    await _run(pg_pool)  # seed on an empty window
    async with pg_pool.acquire() as conn:
        await _seed_three_family_sources(conn)
        # 12 signals, TWO sources, ONE family (news) → diversity bar unmet.
        for _ in range(6):
            await _insert_signal(conn, "src.geotest.news", geo_tags=["LY"])
            await _insert_signal(conn, "src.geotest.news2", geo_tags=["LY"])
    r2 = await _run(pg_pool)
    assert r2.finding.data["formed_fired"] == 0
    async with pg_pool.acquire() as conn:
        assert await _alert_rows(conn) == []

    # Two more DISTINCT families flip it.
    async with pg_pool.acquire() as conn:
        await _insert_signal(conn, "src.geotest.quake", geo_tags=["LY"])
        await _insert_signal(conn, "src.geotest.health", geo_tags=["LY"])
    r3 = await _run(pg_pool)
    assert r3.finding.data["formed_fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    assert _row_data(rows[0])["distinct_family_count"] == 3


async def test_cell_tier_excludes_country_centroid_geocodes(
    pg_pool, clean_slate
):
    await _run(pg_pool)  # seed empty
    async with pg_pool.acquire() as conn:
        await _seed_three_family_sources(conn)
        # Three families with point-trustworthy coordinates in one 1° cell
        # (no geo country tags → the country tier stays out of the picture).
        await _insert_signal(
            conn, "src.geotest.quake",
            lat=33.2, lon=44.1, geo_source="geometry", precision="country",
        )
        await _insert_signal(
            conn, "src.geotest.news",
            lat=33.8, lon=44.9, precision="municipality",
        )
        await _insert_signal(
            conn, "src.geotest.tg",
            lat=33.5, lon=44.5, precision="region",
        )
        # A country-precision nominatim point (country CENTROID) in the same
        # cell MUST NOT enter the cell tier.
        await _insert_signal(
            conn, "src.geotest.health",
            lat=33.5, lon=44.2, precision="country", geo_source="nominatim",
        )
    r2 = await _run(pg_pool)
    assert r2.finding.data["formed_fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["bin_kind"] == "cell"
    assert data["bin_key"] == "cell:33:44"
    # The centroid signal is excluded: 3 contributors, no 'health' family.
    assert data["signal_count"] == 3
    assert sorted(data["families"]) == ["gis", "news", "social"]


async def test_dissolution_fires_once_then_quiet(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        await _seed_three_family_sources(conn)
        for src in ("src.geotest.quake", "src.geotest.news", "src.geotest.tg"):
            await _insert_signal(conn, src, geo_tags=["SO"])
    await _run(pg_pool)  # seeds country:SO as active

    async with pg_pool.acquire() as conn:
        # The window empties out (signals age past 24h).
        await conn.execute(
            "UPDATE signals SET fetched_at = now() - interval '3 days' "
            "WHERE source_id LIKE 'src.geotest.%'"
        )
    r2 = await _run(pg_pool)
    assert r2.force_trace_only is False
    assert r2.finding.data["dissolved_fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == "info"
    data = _row_data(row)
    assert data["event"] == "dissolved"
    assert data["bin_key"] == "country:SO"
    assert sorted(data["previous_families"]) == ["gis", "news", "social"]

    # Dissolution fired ONCE — the next scan is quiet.
    r3 = await _run(pg_pool)
    assert r3.force_trace_only is True
    async with pg_pool.acquire() as conn:
        assert len(await _alert_rows(conn)) == 1
