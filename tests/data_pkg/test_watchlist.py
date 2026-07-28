# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P5-6 — Watchlist v2 (operator-defined standing watches).

Pure tests: pattern validation per kind (closed key sets, honest rejections),
severity-floor semantics, the per-watch cap fold. Ephemeral-DB tests (the
``migrated_pg`` fixture — migration 0105 applies via the real runner, never
the live DB): per-kind matching through the REAL ``alert_trigger_scan`` run —
entity resolution incl. the alias + identity-fold paths, text (plainto
'simple' over title+body), geo country tags + point-radius incl. the
point-trustworthy tier honesty — plus verified gating (critique bar +
structural-verified counts), the class seed, the new-watch no-history gate,
watermark no-refire, per-watch cap + rollup, and the CRUD routes (validation,
soft delete, hits_7d) over httpx ASGITransport.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from legba.data.analysts.deterministic_handlers import (
    _watchlist_scan as wls,
    alert_trigger_scan as ats,
)
from legba.data.config import PostgresConfig
from legba.data.provenance.kinds import STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
from legba.data.registry.watchlist_api import build_watchlist_router, validate_pattern

STRUCTURAL_ANALYST = sorted(STRUCTURAL_VERIFY_EXEMPT_ANALYSTS)[0]


# ---------------------------------------------------------------------------
# Pure — pattern validation
# ---------------------------------------------------------------------------


def test_validate_pattern_entity():
    assert validate_pattern("entity", {"name": "  Wagner Group "}) == {
        "name": "Wagner Group"
    }
    eid = str(uuid4())
    assert validate_pattern("entity", {"entity_id": eid}) == {"entity_id": eid}
    with pytest.raises(ValueError, match="requires 'name'"):
        validate_pattern("entity", {})
    with pytest.raises(ValueError, match="must be a UUID"):
        validate_pattern("entity", {"entity_id": "nope"})
    with pytest.raises(ValueError, match="extras"):
        validate_pattern("entity", {"name": "x", "query": "y"})


def test_validate_pattern_text():
    assert validate_pattern("text", {"query": " Strait of Hormuz "}) == {
        "query": "Strait of Hormuz"
    }
    with pytest.raises(ValueError, match=">= 2"):
        validate_pattern("text", {"query": "x"})
    with pytest.raises(ValueError, match="extras"):
        validate_pattern("text", {"query": "ok", "name": "no"})


def test_validate_pattern_geo():
    assert validate_pattern("geo", {"countries": ["ir", "IQ", "ir"]}) == {
        "countries": ["IR", "IQ"]
    }
    out = validate_pattern("geo", {"lat": 36.3, "lon": 43.1, "radius_km": 50})
    assert out == {"lat": 36.3, "lon": 43.1, "radius_km": 50.0}
    with pytest.raises(ValueError, match="not both"):
        validate_pattern("geo", {"countries": ["IR"], "lat": 1, "lon": 1, "radius_km": 1})
    with pytest.raises(ValueError, match="ISO2"):
        validate_pattern("geo", {"countries": ["Iran"]})
    with pytest.raises(ValueError, match="radius_km"):
        validate_pattern("geo", {"lat": 0, "lon": 0, "radius_km": 0})
    with pytest.raises(ValueError, match="lat"):
        validate_pattern("geo", {"lat": 91, "lon": 0, "radius_km": 5})
    with pytest.raises(ValueError, match="requires"):
        validate_pattern("geo", {})
    with pytest.raises(ValueError, match="unknown watch kind"):
        validate_pattern("place", {"countries": ["IR"]})


# ---------------------------------------------------------------------------
# Pure — severity floor + per-watch cap fold
# ---------------------------------------------------------------------------


def test_severity_floor_semantics():
    # No floor → everything, including an unresolved severity.
    assert wls.severity_meets_floor(None, None) is True
    assert wls.severity_meets_floor("info", None) is True
    # A floor + unresolved severity → honest refusal.
    assert wls.severity_meets_floor(None, "low") is False
    # Ladder comparisons across BOTH vocabularies (the shared rank table).
    assert wls.severity_meets_floor("high", "high") is True
    assert wls.severity_meets_floor("critical", "high") is True
    assert wls.severity_meets_floor("medium", "high") is False
    assert wls.severity_meets_floor("moderate", "medium") is True


def test_alert_severity_mapping():
    assert wls.alert_severity_for("elevated") == "high"
    assert wls.alert_severity_for("moderate") == "medium"
    assert wls.alert_severity_for("critical") == "critical"
    assert wls.alert_severity_for(None) == "medium"
    assert wls.alert_severity_for("garbage") == "medium"


def _hit(watch_id: str, sev: str, title: str, key: str) -> ats.AlertCandidate:
    return ats.AlertCandidate(
        trigger_class=ats.TRIGGER_WATCHLIST,
        severity=sev,
        title=title,
        body="",
        target_id=None,
        data={"watch_id": watch_id, "watch_label": f"label-{watch_id}"},
        watermarks=[(ats.TRIGGER_WATCHLIST, key, {})],
    )


def test_fold_watch_rollups_caps_per_watch_and_carries_watermarks():
    cands = [
        _hit("w1", "critical", "a", "w1|f1"),
        _hit("w1", "medium", "b", "w1|f2"),
        _hit("w1", "high", "c", "w1|f3"),
        _hit("w1", "medium", "d", "w1|f4"),
        _hit("w2", "low", "other", "w2|f5"),
    ]
    kept, rollups = wls.fold_watch_rollups(cands, 2, ats.AlertCandidate)
    w1_kept = [c for c in kept if c.data["watch_id"] == "w1"]
    assert [c.severity for c in w1_kept] == ["critical", "high"]
    # w2 is under cap — untouched, no rollup for it.
    assert [c.data["watch_id"] for c in kept if c.data["watch_id"] == "w2"] == ["w2"]
    assert len(rollups) == 1
    roll = rollups[0]
    assert roll.data["suppressed_count"] == 2
    assert roll.data["watch_id"] == "w1"
    assert roll.severity == "medium"  # worst of the suppressed
    rolled = {k for _, k, _ in roll.watermarks}
    assert rolled == {"w1|f2", "w1|f4"}


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
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE alert_trigger_watermarks")
        await conn.execute("TRUNCATE watchlist")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = 'alert_trigger_scan'"
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
        "sub_handler": "alert_trigger_scan",
        "analyst_id": "alert_trigger_scan",
        "run_id": str(uuid4()),
        # Keep the shared desk cap out of the way — these tests exercise the
        # per-WATCH cap; co-firing v1 classes must not fold our rows away.
        "per_desk_cap": 50,
        **opts,
    }
    return await ats.handle([], options, deps)


def _row_data(row: Any) -> dict[str, Any]:
    d = row["data"]
    full = json.loads(d) if isinstance(d, str) else dict(d)
    inner = full.get("data")
    return inner if isinstance(inner, dict) else full


async def _watch_alerts(conn: Any) -> list[Any]:
    """Fired watchlist_hit alert rows + per-watch rollups, oldest first."""
    rows = await conn.fetch(
        "SELECT id, title, severity, target_id, data, derived_from "
        "FROM analyst_outputs "
        "WHERE kind = 'alert' AND analyst_id = 'alert_trigger_scan' "
        "ORDER BY produced_at, id"
    )
    out = []
    for r in rows:
        data = _row_data(r)
        if data.get("trigger_class") == ats.TRIGGER_WATCHLIST or (
            data.get("rollup_of") == ats.TRIGGER_WATCHLIST
        ):
            out.append(r)
    return out


# -- insert helpers ---------------------------------------------------------


async def _insert_watch(
    conn: Any,
    *,
    kind: str,
    pattern: dict[str, Any],
    label: str = "test watch",
    min_severity: str | None = None,
    created_hours_ago: float = 0.0,
    active: bool = True,
) -> str:
    row = await conn.fetchrow(
        "INSERT INTO watchlist (kind, pattern, label, min_severity, active, "
        "                       created_at) "
        "VALUES ($1, $2::jsonb, $3, $4, $5, "
        "        now() - make_interval(secs => $6)) "
        "RETURNING id::text AS id",
        kind,
        json.dumps(pattern),
        label,
        min_severity,
        active,
        created_hours_ago * 3600.0,
    )
    return row["id"]


async def _insert_finding(
    conn: Any,
    *,
    title: str,
    body: str = "",
    analyst_id: str = "country_assessor",
    target: str | None = "country_g20_us",
    confidence: float = 0.9,
    severity: str | None = "medium",
    derived: list[UUID] | None = None,
) -> UUID:
    fid = uuid4()
    tags = [f"severity:{severity}"] if severity else []
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, severity, data, target_id, "
        "   analyst_id, derived_from, schema_uri) "
        "VALUES ($1, 'finding', $2, $3, $4, $5, $6::jsonb, $7, $8, "
        "        $9::uuid[], 'iglu:legba/finding/jsonschema/1-0-0')",
        fid,
        title,
        body,
        confidence,
        severity,
        json.dumps({"tags": tags}),
        target,
        analyst_id,
        derived or [],
    )
    return fid


async def _insert_faith_critique(conn: Any, finding_id: UUID, score: float) -> None:
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, body, confidence, data, "
        "                             schema_uri) "
        "VALUES ($1, 'critique', $2, '', $3, $4::jsonb, "
        "        'iglu:legba/critique/jsonschema/1-0-0')",
        uuid4(),
        "Faithfulness verify — test",
        score,
        json.dumps({"analyzed_output_id": str(finding_id), "overall_score": score}),
    )


async def _insert_signal(
    conn: Any,
    *,
    geo: list[str] | None = None,
    payload: dict[str, Any] | None = None,
) -> UUID:
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, geo, payload, content_hash) "
        "VALUES ($1, 'test_p5_6_source', $2::text[], $3::jsonb, $4)",
        sid,
        geo or [],
        json.dumps(payload or {}),
        uuid4().hex,
    )
    return sid


async def _insert_entity(
    conn: Any,
    *,
    canonical_name: str,
    aliases: list[str] | None = None,
    merged_into: UUID | None = None,
) -> UUID:
    eid = uuid4()
    await conn.execute(
        "INSERT INTO entity_profiles (id, data, canonical_name, merged_into) "
        "VALUES ($1, $2::jsonb, $3, $4)",
        eid,
        json.dumps({"merged_aliases": aliases or []}),
        canonical_name,
        merged_into,
    )
    return eid


async def _link(conn: Any, signal_id: UUID, entity_id: UUID) -> None:
    await conn.execute(
        "INSERT INTO signal_entity_links (signal_id, entity_id) VALUES ($1, $2)",
        signal_id,
        entity_id,
    )


async def _insert_fact(
    conn: Any, *, subject: str, derived: list[UUID] | None = None
) -> UUID:
    fid = uuid4()
    await conn.execute(
        "INSERT INTO facts (id, subject, predicate, value, derived_from) "
        "VALUES ($1, $2, 'observed', 'test', $3::uuid[])",
        fid,
        subject,
        derived or [],
    )
    return fid


# ---------------------------------------------------------------------------
# DB — entity kind (alias resolution + lineage links + fact subject)
# ---------------------------------------------------------------------------


async def test_entity_watch_alias_hit_and_no_refire(pg_pool, clean_slate):
    await _run(pg_pool)  # seed all classes

    async with pg_pool.acquire() as conn:
        # Canonical entity that FOLDED the "SNSC" alias in (the resolver's
        # merged_aliases machinery) — the watch names the ALIAS.
        ent = await _insert_entity(
            conn,
            canonical_name="Supreme National Security Council",
            aliases=["SNSC"],
        )
        watch_id = await _insert_watch(
            conn, kind="entity", pattern={"name": "SNSC"}, label="SNSC watch"
        )
        sig = await _insert_signal(conn, geo=["IR"])
        await _link(conn, sig, ent)
        fact = await _insert_fact(conn, subject="something else", derived=[sig])
        finding = await _insert_finding(
            conn,
            title="Council convenes emergency session",
            derived=[fact],
        )
        await _insert_faith_critique(conn, finding, 0.8)

    dispatcher = _FakeDispatcher()
    await _run(pg_pool, dispatcher)
    async with pg_pool.acquire() as conn:
        rows = await _watch_alerts(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["watch_id"] == watch_id
    assert data["matched_finding_id"] == str(finding)
    assert "entity-resolution" in data["matched_via"]
    assert "signal_entity_links" in data["matched_via"]
    assert finding in list(rows[0]["derived_from"])
    # Verified watch hit carries its REAL faithfulness on the sink payload.
    wl_payloads = [
        p for p in dispatcher.payloads if p.channel_name == "trigger_scan"
        and "Watch hit" in p.summary
    ]
    assert len(wl_payloads) == 1
    assert wl_payloads[0].verify_state == "faithfulness=0.80"

    # No refire.
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert len(await _watch_alerts(conn)) == 1


async def test_entity_watch_fold_variant_and_fact_subject(pg_pool, clean_slate):
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await _insert_entity(conn, canonical_name="Wagner Group")
        # Article variant resolves via identity_fold; the finding's lineage
        # carries NO signal link — only a fact whose SUBJECT names the entity.
        await _insert_watch(
            conn,
            kind="entity",
            pattern={"name": "The Wagner Group"},
            label="wagner",
        )
        fact = await _insert_fact(conn, subject="Wagner Group")
        finding = await _insert_finding(
            conn, title="Convoy movement reported", derived=[fact]
        )
        await _insert_faith_critique(conn, finding, 0.7)

    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _watch_alerts(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["matched_finding_id"] == str(finding)
    assert "fact subject" in data["matched_via"]


async def test_entity_watch_unresolvable_matches_nothing(pg_pool, clean_slate):
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await _insert_watch(
            conn,
            kind="entity",
            pattern={"name": "No Such Entity Anywhere"},
            label="ghost",
        )
        # A verified finding whose TEXT contains the watched words — an
        # entity watch must NOT degrade into a text search.
        finding = await _insert_finding(
            conn, title="No Such Entity Anywhere in the record"
        )
        await _insert_faith_critique(conn, finding, 0.9)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _watch_alerts(conn) == []


# ---------------------------------------------------------------------------
# DB — text kind + verified gating (critique bar + structural-verified)
# ---------------------------------------------------------------------------


async def test_text_watch_verified_bar_and_structural_counts(pg_pool, clean_slate):
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await _insert_watch(
            conn,
            kind="text",
            pattern={"query": "Strait of Hormuz"},
            label="hormuz",
        )
        # 1) Verified → fires.
        verified = await _insert_finding(
            conn, title="Tanker seized near the Strait of Hormuz"
        )
        await _insert_faith_critique(conn, verified, 0.8)
        # 2) Matching text but NO critique + non-structural → never a hit.
        await _insert_finding(
            conn, title="Unverified rumor about the Strait of Hormuz"
        )
        # 3) Below the effective-confidence floor → never a hit.
        low = await _insert_finding(
            conn, title="Weak read on the Strait of Hormuz"
        )
        await _insert_faith_critique(conn, low, 0.30)
        # 4) Structural verify-exempt analyst, no critique → COUNTS.
        structural = await _insert_finding(
            conn,
            title="Deterministic Strait of Hormuz cluster",
            analyst_id=STRUCTURAL_ANALYST,
        )

    dispatcher = _FakeDispatcher()
    await _run(pg_pool, dispatcher)
    async with pg_pool.acquire() as conn:
        rows = await _watch_alerts(conn)
    matched = {_row_data(r)["matched_finding_id"] for r in rows}
    assert matched == {str(verified), str(structural)}
    for r in rows:
        assert "plainto_tsquery" in _row_data(r)["matched_via"]
    # The structural hit's verify posture is stated, not faked.
    structural_payloads = [
        p
        for p in dispatcher.payloads
        if "Watch hit" in p.summary and "Deterministic" in p.summary
    ]
    assert len(structural_payloads) == 1
    assert structural_payloads[0].verify_state.startswith("unverified")
    assert "structural" in structural_payloads[0].verify_state


# ---------------------------------------------------------------------------
# DB — geo kind (country tags + point-trustworthy tier)
# ---------------------------------------------------------------------------


async def test_geo_watch_country_tags(pg_pool, clean_slate):
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await _insert_watch(
            conn, kind="geo", pattern={"countries": ["IR"]}, label="iran"
        )
        sig = await _insert_signal(conn, geo=["IR", "IQ"])
        fact = await _insert_fact(conn, subject="border", derived=[sig])
        hit = await _insert_finding(conn, title="Border activity", derived=[fact])
        await _insert_faith_critique(conn, hit, 0.8)
        # Other-country lineage → no hit.
        sig2 = await _insert_signal(conn, geo=["FR"])
        miss = await _insert_finding(conn, title="Elsewhere", derived=[sig2])
        await _insert_faith_critique(conn, miss, 0.8)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _watch_alerts(conn)
    assert {_row_data(r)["matched_finding_id"] for r in rows} == {str(hit)}


async def test_geo_watch_point_radius_point_trustworthy_only(pg_pool, clean_slate):
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await _insert_watch(
            conn,
            kind="geo",
            pattern={"lat": 36.34, "lon": 43.13, "radius_km": 50},
            label="mosul 50km",
        )
        # Point-trustworthy geocode ~ Mosul → hit (signal directly in
        # derived_from — the fact hop is exercised by the country test).
        good = await _insert_signal(
            conn,
            payload={"geo": {"lat": "36.32", "lon": "43.12", "precision": "municipality"}},
        )
        hit = await _insert_finding(conn, title="Point event", derived=[good])
        await _insert_faith_critique(conn, hit, 0.8)
        # Same coordinates but COUNTRY-precision geocode → honesty gate: no hit.
        centroid = await _insert_signal(
            conn,
            payload={"geo": {"lat": "36.32", "lon": "43.12", "precision": "country"}},
        )
        miss = await _insert_finding(conn, title="Centroid event", derived=[centroid])
        await _insert_faith_critique(conn, miss, 0.8)
        # Point-trustworthy but far away (~Baghdad, >300km) → no hit.
        far = await _insert_signal(
            conn,
            payload={"geo": {"lat": "33.31", "lon": "44.36", "precision": "municipality"}},
        )
        miss2 = await _insert_finding(conn, title="Far event", derived=[far])
        await _insert_faith_critique(conn, miss2, 0.8)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _watch_alerts(conn)
    assert {_row_data(r)["matched_finding_id"] for r in rows} == {str(hit)}
    assert "point-trustworthy" in _row_data(rows[0])["matched_via"]


# ---------------------------------------------------------------------------
# DB — min_severity, seed, new-watch history gate, cap + rollup
# ---------------------------------------------------------------------------


async def test_min_severity_floor(pg_pool, clean_slate):
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await _insert_watch(
            conn,
            kind="text",
            pattern={"query": "sevfloor topic"},
            label="floored",
            min_severity="high",
        )
        below = await _insert_finding(
            conn, title="sevfloor topic minor", severity="medium"
        )
        await _insert_faith_critique(conn, below, 0.8)
        above = await _insert_finding(
            conn, title="sevfloor topic major", severity="high"
        )
        await _insert_faith_critique(conn, above, 0.8)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _watch_alerts(conn)
    assert {_row_data(r)["matched_finding_id"] for r in rows} == {str(above)}
    assert rows[0]["severity"] == "high"


async def test_first_scan_seeds_silently(pg_pool, clean_slate):
    """Bring-up honesty: a pre-existing watch + matching finding seed the
    class silently; only NEW findings fire afterwards."""
    async with pg_pool.acquire() as conn:
        await _insert_watch(
            conn, kind="text", pattern={"query": "seedcase topic"}, label="seed"
        )
        pre = await _insert_finding(conn, title="seedcase topic pre-existing")
        await _insert_faith_critique(conn, pre, 0.8)

    r1 = await _run(pg_pool)
    assert ats.TRIGGER_WATCHLIST in r1.finding.data["seeded_classes"]
    async with pg_pool.acquire() as conn:
        assert await _watch_alerts(conn) == []
        seeded = await conn.fetchval(
            "SELECT count(*) FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key <> $2",
            ats.TRIGGER_WATCHLIST,
            ats.SEED_KEY,
        )
        assert seeded == 1  # the pre-existing match, watermarked silently

        post = await _insert_finding(conn, title="seedcase topic fresh")
        await _insert_faith_critique(conn, post, 0.8)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _watch_alerts(conn)
    assert {_row_data(r)["matched_finding_id"] for r in rows} == {str(post)}


async def test_new_watch_never_pages_history(pg_pool, clean_slate):
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        old = await _insert_finding(conn, title="history topic event")
        await _insert_faith_critique(conn, old, 0.8)
        # Watch created strictly AFTER the finding was produced (each
        # autocommit statement gets its own now()).
        await _insert_watch(
            conn,
            kind="text",
            pattern={"query": "history topic"},
            label="late watch",
        )
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _watch_alerts(conn) == []

        # A finding produced AFTER the watch fires normally.
        fresh = await _insert_finding(conn, title="history topic again")
        await _insert_faith_critique(conn, fresh, 0.8)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _watch_alerts(conn)
    assert {_row_data(r)["matched_finding_id"] for r in rows} == {str(fresh)}


async def test_per_watch_cap_folds_honest_rollup(pg_pool, clean_slate):
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await _insert_watch(
            conn, kind="text", pattern={"query": "capfold topic"}, label="capfold"
        )
        fids = []
        for i in range(5):
            f = await _insert_finding(conn, title=f"capfold topic event {i}")
            await _insert_faith_critique(conn, f, 0.8)
            fids.append(f)

    r = await _run(pg_pool, per_watch_cap=2)
    async with pg_pool.acquire() as conn:
        rows = await _watch_alerts(conn)
    hits = [r_ for r_ in rows if _row_data(r_).get("trigger_class") == ats.TRIGGER_WATCHLIST]
    rollups = [r_ for r_ in rows if _row_data(r_).get("rollup_of") == ats.TRIGGER_WATCHLIST]
    assert len(hits) == 2
    assert len(rollups) == 1
    roll_data = _row_data(rollups[0])
    assert roll_data["suppressed_count"] == 3
    assert roll_data["per_watch_cap"] == 2
    assert "3 further hit(s)" in rollups[0]["title"]
    # ALL five (watch, finding) watermarks advanced — nothing refires.
    await _run(pg_pool, per_watch_cap=2)
    async with pg_pool.acquire() as conn:
        assert len(await _watch_alerts(conn)) == 3
    # Receipt honesty: the suppression is counted per class.
    assert r.finding.data["counts_by_class"][ats.TRIGGER_WATCHLIST][
        "suppressed_into_watch_rollups"
    ] == 3


async def test_inactive_watch_never_fires(pg_pool, clean_slate):
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await _insert_watch(
            conn,
            kind="text",
            pattern={"query": "inactive topic"},
            label="off",
            active=False,
        )
        f = await _insert_finding(conn, title="inactive topic event")
        await _insert_faith_critique(conn, f, 0.8)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _watch_alerts(conn) == []


# ---------------------------------------------------------------------------
# Routes — CRUD validation + soft delete + hits_7d
# ---------------------------------------------------------------------------


class _Reg:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg = pool


class _RouteDeps:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.descriptor_registry = _Reg(pool)


@pytest_asyncio.fixture
async def wl_client(pg_pool, monkeypatch):
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")
    app = FastAPI()
    app.include_router(
        build_watchlist_router(_RouteDeps(pg_pool)), prefix="/api/v1/v3"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_watchlist_crud_roundtrip(wl_client, pg_pool, clean_slate):
    # Create one of each kind.
    r = await wl_client.post(
        "/api/v1/v3/watchlist",
        json={
            "kind": "entity",
            "pattern": {"name": "Wagner Group"},
            "label": "wagner",
        },
    )
    assert r.status_code == 201, r.text
    ent = r.json()
    assert ent["kind"] == "entity" and ent["active"] is True
    assert ent["hits_7d"] == 0
    assert ent["min_severity"] is None

    r = await wl_client.post(
        "/api/v1/v3/watchlist",
        json={
            "kind": "geo",
            "pattern": {"countries": ["ir"]},
            "label": "iran",
            "min_severity": "high",
        },
    )
    assert r.status_code == 201
    geo = r.json()
    assert geo["pattern"] == {"countries": ["IR"]}  # normalized

    # Validation honesty: junk patterns are 422 with a reason.
    r = await wl_client.post(
        "/api/v1/v3/watchlist",
        json={"kind": "text", "pattern": {"query": "x"}, "label": "short"},
    )
    assert r.status_code == 422
    r = await wl_client.post(
        "/api/v1/v3/watchlist",
        json={"kind": "geo", "pattern": {"countires": ["IR"]}, "label": "typo"},
    )
    assert r.status_code == 422
    r = await wl_client.post(
        "/api/v1/v3/watchlist",
        json={"kind": "place", "pattern": {"countries": ["IR"]}, "label": "bad"},
    )
    assert r.status_code == 422  # pydantic Literal rejects the kind

    # List: both actives.
    r = await wl_client.get("/api/v1/v3/watchlist")
    assert r.status_code == 200
    ids = {w["id"] for w in r.json()}
    assert {ent["id"], geo["id"]} <= ids

    # Update: label + clearing min_severity (explicit null) + pattern
    # revalidated against the ROW's kind.
    r = await wl_client.put(
        f"/api/v1/v3/watchlist/{geo['id']}",
        json={"label": "iran+iraq", "pattern": {"countries": ["IR", "IQ"]},
              "min_severity": None},
    )
    assert r.status_code == 200
    upd = r.json()
    assert upd["label"] == "iran+iraq"
    assert upd["pattern"] == {"countries": ["IR", "IQ"]}
    assert upd["min_severity"] is None
    # Wrong-shape pattern for the row's kind → 422 (kind immutable).
    r = await wl_client.put(
        f"/api/v1/v3/watchlist/{geo['id']}", json={"pattern": {"query": "nope"}}
    )
    assert r.status_code == 422

    # Soft delete: active=false, gone from the default list, still visible
    # with include_inactive, row survives in the table.
    r = await wl_client.delete(f"/api/v1/v3/watchlist/{ent['id']}")
    assert r.status_code == 200
    assert r.json()["active"] is False
    r = await wl_client.get("/api/v1/v3/watchlist")
    assert ent["id"] not in {w["id"] for w in r.json()}
    r = await wl_client.get("/api/v1/v3/watchlist?include_inactive=true")
    assert ent["id"] in {w["id"] for w in r.json()}
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM watchlist WHERE id = $1", UUID(ent["id"])
        ) == 1

    # 404 / 400 honesty.
    r = await wl_client.delete(f"/api/v1/v3/watchlist/{uuid4()}")
    assert r.status_code == 404
    r = await wl_client.delete("/api/v1/v3/watchlist/not-a-uuid")
    assert r.status_code == 400


async def test_watchlist_hits_7d_counts_alert_rows(wl_client, pg_pool, clean_slate):
    await _run(pg_pool)  # seed the trigger classes
    r = await wl_client.post(
        "/api/v1/v3/watchlist",
        json={"kind": "text", "pattern": {"query": "hitcount topic"}, "label": "hc"},
    )
    watch_id = r.json()["id"]
    async with pg_pool.acquire() as conn:
        f = await _insert_finding(conn, title="hitcount topic event")
        await _insert_faith_critique(conn, f, 0.8)
    await _run(pg_pool)
    r = await wl_client.get("/api/v1/v3/watchlist")
    row = next(w for w in r.json() if w["id"] == watch_id)
    assert row["hits_7d"] == 1
