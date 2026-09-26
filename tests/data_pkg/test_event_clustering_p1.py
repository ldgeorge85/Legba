# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3/P1 tests — event clustering, lifecycle maintenance and reconciliation.

Pure tests pin the matcher contract (weights live in the sibling module), the
B1 guards, the additive embedding/fact gates and the descriptor shape. The
``migrated_pg`` tests exercise the real path end-to-end: bounded signal slice →
cluster → event upsert + links → lifecycle transition → reactivation, plus the
signature-collapse edge the tower lane produces deliberately.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    TRACE_ONLY,
    run_method,
)
from legba.data.analysts.deterministic_handlers import (
    _event_lifecycle,
    _event_matcher,
    event_clustering,
    event_reconciler,
)
from legba.data.analysts.deterministic_handlers._event_candidates import (
    EventCandidate,
    _resolve_candidate_signals,
    cluster_candidate,
    normalize_signal,
    resolve_candidate_signals_batch,
    tower_producer_id,
)
from legba.data.analysts.deterministic_handlers._event_matcher import (
    EVENT_CLUSTERING_VERSION,
    EventCluster,
    EventEvidence,
    cluster_aggregate,
    cluster_evidence,
    match_cluster_to_event,
    match_prepared_pair,
    match_prepared_to_event,
    prepare_aggregate,
    prepare_open_events,
    prepared_cache_stats,
    reset_prepared_cache,
    score_pair,
)
from legba.data.config import PostgresConfig
from legba.data.registry.descriptor import Family

DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"


def _ev(
    *,
    title: str,
    entity_names: tuple[str, ...] = ("Iran",),
    source_id: str = "src_a",
    fetched_at: datetime | None = None,
    embedding_ref: str = "",
    facts: frozenset[str] = frozenset(),
    source_class: str = "reporting",
    geo: tuple[str, ...] = ("ir",),
    category: str = "conflict",
) -> EventEvidence:
    """One normalized evidence row for matcher tests."""
    return EventEvidence(
        id=uuid4(),
        title=title,
        category=category,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        source_id=source_id,
        source_class=source_class,
        geo=geo,
        entity_names=entity_names,
        embedding_ref=embedding_ref,
        fact_subjects=facts,
    )


def _descriptor(name: str):
    """Parse a descriptor through the same family model registry writes use."""
    return Family.ANALYST.model.model_validate(
        yaml.safe_load((DESCRIPTORS / name).read_text()), strict=False
    )


# ---------------------------------------------------------------------------
# Matcher / promotion guards — pure, no substrate
# ---------------------------------------------------------------------------


def test_registration_is_trace_only_and_options_are_declared() -> None:
    assert SUB_HANDLERS["event_clustering"] is event_clustering.handle
    assert SUB_HANDLERS["event_reconciler"] is event_reconciler.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["event_clustering"] is TRACE_ONLY
    assert OUTPUT_KIND_BY_SUB_HANDLER["event_reconciler"] is TRACE_ONLY
    from legba.data.analysts.handler_options import HANDLER_OPTIONS

    assert "lookback_hours" in {
        spec.name for spec in HANDLER_OPTIONS["event_clustering"]
    }
    assert "max_events" in {
        spec.name for spec in HANDLER_OPTIONS["event_reconciler"]
    }
    # P1b — the pass-budget mirror option is declared on both handlers.
    assert "pass_budget_seconds" in {
        spec.name for spec in HANDLER_OPTIONS["event_clustering"]
    }
    assert "pass_budget_seconds" in {
        spec.name for spec in HANDLER_OPTIONS["event_reconciler"]
    }


def test_descriptors_are_draft_zero_budget_meta_analysts() -> None:
    clustering = _descriptor("analyst_event_clustering.yaml")
    reconciler = _descriptor("analyst_event_reconciler.yaml")
    assert clustering.identity.state == "draft"
    assert reconciler.identity.state == "draft"
    assert clustering.method.budget_tokens_per_day == 0
    assert reconciler.method.budget_tokens_per_day == 0
    assert clustering.method.sub_handler == "event_clustering"
    assert reconciler.method.sub_handler == "event_reconciler"
    assert clustering.cadence.fallback_schedule != reconciler.cadence.fallback_schedule


def test_score_pair_uses_the_four_measured_features() -> None:
    left = _ev(title="Strike hits Fordow")
    right = _ev(title="Strike hits Fordow", source_id="src_b")
    match = score_pair(left, right)
    assert match.entity_overlap == 1.0
    assert match.title_similarity == 1.0
    assert match.temporal_proximity == 1.0
    assert match.category_match == 1.0
    assert match.score == 1.0 and match.linked


def test_compass_gate_refuses_opposed_direction_stems() -> None:
    left = _ev(title="Korea missile test", entity_names=("North Korea",))
    right = _ev(title="Korea missile test", entity_names=("South Korea",))
    match = score_pair(left, right)
    assert match.refused_by_direction
    assert not match.linked


def test_embedding_gate_is_additive_and_structurally_gated() -> None:
    ref_a, ref_b = uuid4(), uuid4()
    left = _ev(
        title="Completely different report",
        entity_names=("Alpha",),
        embedding_ref=str(ref_a),
        category="other",
    )
    right = _ev(
        title="Unrelated story",
        entity_names=("Beta",),
        source_id="src_b",
        embedding_ref=str(ref_b),
        category="other",
    )
    weak = score_pair(left, right, embedding_cosine=0.96)
    strong = score_pair(left, right, embedding_cosine=0.98)
    assert not weak.linked
    assert strong.via_embedding and strong.linked
    no_uuid = score_pair(
        left,
        EventEvidence(**{**right.__dict__, "embedding_ref": "no_body"}),
        embedding_cosine=0.99,
    )
    assert not no_uuid.via_embedding and not no_uuid.linked


def test_shared_fact_subjects_are_a_second_gate() -> None:
    left = _ev(title="A", entity_names=("Alpha",), facts=frozenset({"f1"}))
    right = _ev(
        title="B", entity_names=("Beta",), source_id="src_b", facts=frozenset({"f1"})
    )
    match = score_pair(left, right)
    assert match.via_shared_facts and match.shared_fact_subjects == 1


def test_cluster_evidence_bounds_mega_buckets() -> None:
    members = [
        _ev(
            title=f"Report {i}",
            entity_names=("Iran",),
            source_id=f"src_{i}",
        )
        for i in range(30)
    ]
    clusters, counts = cluster_evidence(members)
    assert len(clusters) == 1
    assert clusters[0].oversized
    assert counts["oversized"] == 1
    assert not cluster_candidate(clusters[0]).promoted


def test_cluster_promotion_needs_cross_source_or_official() -> None:
    one = _ev(title="Fordow strike confirmed", source_id="rss_one")
    clusters, _ = cluster_evidence([one])
    cand = cluster_candidate(clusters[0])
    assert not cand.promoted and cand.decline_reason == "single_source"
    official = _ev(
        title="Official confirms Fordow strike",
        source_id="gov_one",
        source_class="official",
    )
    cand2 = cluster_candidate(cluster_evidence([official])[0][0])
    assert cand2.promoted


# ---------------------------------------------------------------------------
# Integration — migrated_pg
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig, clean_tables):
    """A clean event substrate and a short-lived pool."""
    await clean_tables(
        "events",
        "signals",
        "entity_profiles",
        "analyst_outputs",
        "situations",
        "source_descriptors",
        "output_dead_letter",
        # P1b: the handler's resume cursors live on the shared watermark
        # plane — an uncleaned row would silently skip the next test's slice.
        "alert_trigger_watermarks",
    )
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=3)
    yield pool
    await pool.close()


async def _signal(
    conn,
    *,
    title: str,
    source_id: str,
    fetched_at: datetime | None = None,
    geo: tuple[str, ...] = ("ir",),
    credibility: float = 0.7,
    category: str = "conflict",
    signal_id: UUID | None = None,
) -> UUID:
    """Insert one canonical text signal in the shape the handler consumes."""
    return await conn.fetchval(
        """
        INSERT INTO signals
            (id, source_id, modality, payload, content_hash, fetched_at, geo,
             source_credibility)
        VALUES (COALESCE($7::uuid, gen_random_uuid()),
                $1, 'text', $2::jsonb, $3, $4, $5::text[], $6)
        RETURNING id
        """,
        source_id,
        json.dumps({"title": title, "category": category}),
        f"p1-{uuid4().hex}",
        fetched_at or datetime.now(timezone.utc),
        list(geo),
        credibility,
        signal_id,
    )


async def _entity(conn, name: str) -> UUID:
    """Insert one canonical entity profile."""
    return await conn.fetchval(
        "INSERT INTO entity_profiles (data, canonical_name) "
        "VALUES ('{}'::jsonb, $1) RETURNING id",
        name,
    )


async def _link_signal_entity(
    conn, signal_id: UUID, entity_id: UUID, *, role: str = "mentioned"
) -> None:
    """Link a signal to a resolved canonical entity."""
    await conn.execute(
        "INSERT INTO signal_entity_links (signal_id, entity_id, role,"
        " confidence, analyst_id) VALUES ($1,$2,$3,0.9,'p1_test')",
        signal_id,
        entity_id,
        role,
    )


def _options(**kw):
    """Descriptor-shaped run options for one isolated analyst producer."""
    base = {
        "sub_handler": "event_clustering",
        "analyst_id": f"analyst.event_clustering_test_{uuid4().hex[:8]}",
        "analyst_version": "p1-test",
        "run_id": uuid4(),
        "lookback_hours": 2,
        "max_signals": 100,
        "include_tower": False,
        "max_lifecycle_events": 100,
    }
    base.update(kw)
    return base


@pytest.mark.integration
@pytest.mark.asyncio
async def test_handle_flag_off_is_structurally_inert(pg_pool, monkeypatch) -> None:
    """Even with a pool injected, LEGBA_EVENTS off returns before reading."""
    monkeypatch.delenv("LEGBA_EVENTS", raising=False)
    result = await event_clustering.handle(
        [], _options(), SimpleNamespace(pg_pool=pg_pool)
    )
    assert result.finding.data["events_enabled"] is False
    assert await pg_pool.fetchval("SELECT count(*) FROM events") == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clustering_writes_event_links_and_is_idempotent(
    pg_pool, monkeypatch
) -> None:
    """Two same-occurrence signals from different sources open ONE event."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    async with pg_pool.acquire() as conn:
        entity = await _entity(conn, f"P1 Fordow {uuid4().hex[:8]}")
        s1 = await _signal(conn, title="Strike hits Fordow", source_id="rss_a")
        later = datetime.now(timezone.utc) + timedelta(minutes=1)
        s2 = await _signal(
            conn,
            title="Strike hits Fordow",
            source_id="rss_b",
            fetched_at=later,
        )
        await _link_signal_entity(conn, s1, entity)
        await _link_signal_entity(conn, s2, entity)

    result = await event_clustering.handle(
        [], _options(), SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    assert result.finding.data["funnel"]["promoted"] == 1
    row = await pg_pool.fetchrow(
        "SELECT id, lifecycle_state, signal_count, distinct_source_count,"
        " source_method, lifecycle_changed_at FROM events"
    )
    assert row["lifecycle_state"] == "emerging"
    assert row["signal_count"] == 2
    assert row["distinct_source_count"] == 2
    assert row["source_method"] == "clustering"
    assert row["lifecycle_changed_at"] == later
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM signal_event_links WHERE event_id=$1", row["id"]
    ) == 2
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM event_entity_links WHERE event_id=$1", row["id"]
    ) == 1
    ledger = await pg_pool.fetch(
        "SELECT transition, occurred_at, derived_from FROM event_lifecycle_events"
        " WHERE event_id=$1", row["id"]
    )
    assert [r["transition"] for r in ledger] == ["opened"]
    assert len(ledger[0]["derived_from"]) == 2

    # Re-emitting the same candidate under the same producer id is an upsert:
    # one event, no duplicated links, no second 'opened' row.
    opts = _options(analyst_id="analyst.p1_idempotent")
    await event_clustering.handle([], opts, SimpleNamespace(pg_pool=pg_pool, extras={}))
    before = await pg_pool.fetchval("SELECT count(*) FROM events")
    await event_clustering.handle([], opts, SimpleNamespace(pg_pool=pg_pool, extras={}))
    assert await pg_pool.fetchval("SELECT count(*) FROM events") == before
    assert result.finding.kind_marker == "finding"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifecycle_signal_count_is_members_not_member_actor_pairs(
    pg_pool, monkeypatch
) -> None:
    """The lifecycle scan's ``signal_count`` counts MEMBERS, not pairs.

    ``_OPEN_EVENT_STATS_SQL`` LEFT JOINs both link tables onto ``events`` in
    one FROM, so its rows are (member x actor) pairs. Counting those rows is
    what wrote the live drift — 1,099 of 1,246 rows wrong on 2026-09-25, 1,110
    of them carrying exactly ``n_members * n_actors``. Four members and five
    actors here: the ledger's own reason line and the persisted column both
    have to say 4, never 20.
    """
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    now = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        event_id = await conn.fetchval(
            "INSERT INTO events (event_signature, analyst_id, title,"
            " lifecycle_state, lifecycle_changed_at, confidence)"
            " VALUES ($1, 'analyst.p1_fanout', 'fan-out', 'emerging', $2, 0.9)"
            " RETURNING id",
            f"evt:fanout-{uuid4().hex[:8]}#evt:ir", now,
        )
        for i in range(4):
            await conn.execute(
                "INSERT INTO signal_event_links (signal_id, event_id,"
                " linked_at, source_id) VALUES ($1, $2, $3, $4)",
                await _signal(conn, title=f"m{i}", source_id=f"rss_{i % 2}"),
                event_id, now - timedelta(minutes=i), f"rss_{i % 2}",
            )
        for j in range(5):
            await conn.execute(
                "INSERT INTO event_entity_links (event_id, entity_id, role)"
                " VALUES ($1, $2, 'actor')",
                event_id, await _entity(conn, f"Fanout {j} {uuid4().hex[:8]}"),
            )
        # 4 members x 5 actors = 20 pair rows for this one event.
        assert await conn.fetchval(
            "SELECT count(*) FROM signal_event_links sel"
            " JOIN event_entity_links eel ON eel.event_id = sel.event_id"
            " WHERE sel.event_id = $1", event_id
        ) == 20

        out = await _event_lifecycle.maintain_event_lifecycle(
            conn, now=now, analyst_ctx=SimpleNamespace(
                analyst_id="analyst.p1_fanout", analyst_version="p1-test",
                run_id=uuid4(),
            ),
        )
        assert out["transitions"] == {"advanced": 1}

        row = await conn.fetchrow(
            "SELECT lifecycle_state, signal_count, distinct_source_count"
            "  FROM events WHERE id = $1", event_id,
        )
        assert row["lifecycle_state"] == "developing"
        assert row["signal_count"] == 4
        assert row["distinct_source_count"] == 2
        why = await conn.fetchval(
            "SELECT why FROM event_lifecycle_events WHERE event_id = $1"
            " AND transition = 'advanced'", event_id,
        )
        assert "signal_count=4" in why


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifecycle_advances_resolves_and_reactivates(
    pg_pool, monkeypatch
) -> None:
    """The open-event scan drives ledger transitions on evidence clocks.

    P1c: lifecycle runs FIRST, so a link written by tick N is transitioned by
    tick N+1. The writer still mints 'opened' inside its own write, and the
    one-tick lag is exactly what the P1b ``link_since_ledger`` machinery was
    built to absorb — nothing is lost, it lands one cadence later.
    """
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    opts = _options(analyst_id="analyst.p1_lifecycle")
    async with pg_pool.acquire() as conn:
        entity = await _entity(conn, f"P1 Fordow {uuid4().hex[:8]}")
        for i in range(5):
            sid = await _signal(
                conn,
                title="Strike hits Fordow",
                source_id=f"rss_{i}",
                credibility=0.8,
            )
            await _link_signal_entity(conn, sid, entity)
    await event_clustering.handle([], opts, SimpleNamespace(pg_pool=pg_pool, extras={}))
    event = await pg_pool.fetchrow(
        "SELECT id, lifecycle_state FROM events WHERE analyst_id=$1",
        opts["analyst_id"],
    )
    # The writing tick mints 'opened' and stops there: its lifecycle phase ran
    # before the event existed.
    assert event["lifecycle_state"] == "emerging"
    assert [r["transition"] for r in await pg_pool.fetch(
        "SELECT transition FROM event_lifecycle_events WHERE event_id=$1"
        " ORDER BY occurred_at, created_at", event["id"]
    )] == ["opened"]

    # The NEXT tick's lifecycle phase reads the links the last one wrote. Its
    # slice is empty (the watermark advanced), so this is lifecycle alone.
    receipt = await event_clustering.handle(
        [], opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    assert receipt.finding.data["funnel"]["examined"] == 0
    assert await pg_pool.fetchval(
        "SELECT lifecycle_state FROM events WHERE id=$1", event["id"]
    ) == "developing"
    kinds = await pg_pool.fetch(
        "SELECT transition FROM event_lifecycle_events WHERE event_id=$1"
        " ORDER BY occurred_at, created_at", event["id"]
    )
    assert [r["transition"] for r in kinds] == ["opened", "advanced"]

    # Resolve on the EVIDENCE clock: a developing event crosses at newest
    # linked_at + 72h, not at the run's wall clock.
    silent_at = datetime.now(timezone.utc) - timedelta(days=8)
    await pg_pool.execute(
        "UPDATE signal_event_links SET linked_at=$2 WHERE event_id=$1",
        event["id"], silent_at,
    )
    await event_clustering.handle(
        [], {**opts, "lookback_hours": 1},
        SimpleNamespace(pg_pool=pg_pool, extras={}),
    )
    state = await pg_pool.fetchval(
        "SELECT lifecycle_state FROM events WHERE id=$1", event["id"]
    )
    assert state == "resolved"
    resolved = await pg_pool.fetchrow(
        "SELECT transition, occurred_at, derived_from FROM event_lifecycle_events"
        " WHERE event_id=$1 AND transition='resolved'", event["id"]
    )
    assert resolved["occurred_at"] == silent_at + timedelta(hours=72)
    assert resolved["derived_from"] == []

    # A single late link cannot open a NEW event, but it can reattach to the
    # resolved occurrence and mint exactly one 'reactivated' ledger row. Two
    # ticks under P1c: the first attaches the link, the second transitions on
    # it — found through ``link_since_ledger``, not an in-memory hand-off.
    async with pg_pool.acquire() as conn:
        sid = await _signal(
            conn, title="Strike hits Fordow", source_id="late_wire"
        )
        await _link_signal_entity(conn, sid, entity)
    await event_clustering.handle(
        [], opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    assert await pg_pool.fetchval(
        "SELECT lifecycle_state FROM events WHERE id=$1", event["id"]
    ) == "resolved"
    await event_clustering.handle(
        [], opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    state = await pg_pool.fetchval(
        "SELECT lifecycle_state FROM events WHERE id=$1", event["id"]
    )
    assert state == "developing"
    reactivated = await pg_pool.fetch(
        "SELECT transition, derived_from FROM event_lifecycle_events"
        " WHERE event_id=$1 AND transition='reactivated'", event["id"]
    )
    assert len(reactivated) == 1
    assert sid in list(reactivated[0]["derived_from"])

    # Silence cannot reactivate and silence cannot resolve twice.
    await pg_pool.execute(
        "UPDATE signal_event_links SET linked_at=$2 WHERE event_id=$1",
        event["id"], silent_at,
    )
    before = await pg_pool.fetchval(
        "SELECT count(*) FROM event_lifecycle_events WHERE event_id=$1",
        event["id"],
    )
    await event_clustering.handle(
        [], {**opts, "lookback_hours": 1},
        SimpleNamespace(pg_pool=pg_pool, extras={}),
    )
    after = await pg_pool.fetchval(
        "SELECT count(*) FROM event_lifecycle_events WHERE event_id=$1",
        event["id"],
    )
    assert after > before  # resolved again from developing, evidence-free
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM event_lifecycle_events WHERE event_id=$1"
        " AND transition='resolved'", event["id"]
    ) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tower_candidate_and_signature_collapse_correlate(
    pg_pool, monkeypatch
) -> None:
    """Clustering and tower mint separate rows for one signature; the
    reconciler lands the canonical correlated_with edge."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    analyst_id = "analyst.p1_tower"
    async with pg_pool.acquire() as conn:
        entity = await _entity(conn, f"P1 Fordow {uuid4().hex[:8]}")
        s1 = await _signal(conn, title="Strike hits Fordow", source_id="rss_a")
        s2 = await _signal(
            conn, title="Strike hits Fordow", source_id="rss_b"
        )
        await _link_signal_entity(conn, s1, entity)
        await _link_signal_entity(conn, s2, entity)
        finding_id = await conn.fetchval(
            """
            INSERT INTO analyst_outputs
                (kind, title, confidence, data, derived_from, schema_uri,
                 analyst_id, analyst_version)
            VALUES ('finding', 'Strike hits Fordow', 0.9,
                    $1::jsonb, $2::uuid[],
                    'iglu:legba/finding/jsonschema/1-0-0',
                    'finding_writer', 'test')
            RETURNING id
            """,
            json.dumps({"category": "conflict"}),
            [s1, s2],
        )
        await conn.execute(
            """
            INSERT INTO analyst_outputs
                (kind, title, data, schema_uri, analyst_id, analyst_version)
            VALUES ('critique', 'Faithfulness verify — pass',
                    $1::jsonb,
                    'iglu:legba/critique/jsonschema/1-0-0',
                    'verifier', 'test')
            """,
            json.dumps({"analyzed_output_id": str(finding_id),
                        "overall_score": 0.9}),
        )
    opts = _options(
        analyst_id=analyst_id,
        include_tower=True,
        tower_window_days=30,
        tower_floor=0.5,
    )
    await event_clustering.handle([], opts, SimpleNamespace(pg_pool=pg_pool, extras={}))
    rows = await pg_pool.fetch(
        "SELECT analyst_id, source_method, event_signature FROM events"
        " ORDER BY analyst_id"
    )
    assert {r["analyst_id"] for r in rows} == {analyst_id, "tower_backfill"}
    assert len({r["event_signature"] for r in rows}) == 1
    assert {r["source_method"] for r in rows} == {"clustering", "tower"}

    rec_opts = {
        "sub_handler": "event_reconciler",
        "analyst_id": "analyst.event_reconciler_test",
        "analyst_version": "p1-test",
        "run_id": uuid4(),
        "max_events": 100,
    }
    await event_reconciler.handle(
        [], rec_opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    edge = await pg_pool.fetchrow(
        "SELECT src_event_id, dst_event_id, edge_type FROM event_edges"
        " WHERE edge_type='correlated_with'"
    )
    assert edge is not None
    assert str(edge["src_event_id"]) < str(edge["dst_event_id"])
    # Idempotent upsert: one open edge, not two.
    await event_reconciler.handle(
        [], rec_opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM event_edges WHERE edge_type='correlated_with'"
    ) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciler_evolves_and_unsupported_edges_refuse(
    pg_pool, monkeypatch
) -> None:
    """Same actors + temporal adjacency mint evolves_from; seam types raise."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    async with pg_pool.acquire() as conn:
        actors = [await _entity(conn, f"Actor {i}") for i in range(2)]
        t0 = datetime.now(timezone.utc) - timedelta(hours=3)
        earlier = await conn.fetchval(
            "INSERT INTO events (event_signature, analyst_id, title,"
            " time_start, time_end) VALUES ($1,'a','old',$2,$3) RETURNING id",
            f"evt:old-{uuid4().hex[:6]}#evt:ir", t0, t0,
        )
        later = await conn.fetchval(
            "INSERT INTO events (event_signature, analyst_id, title,"
            " time_start, time_end) VALUES ($1,'a','new',$2,$3) RETURNING id",
            f"evt:new-{uuid4().hex[:6]}#evt:ir",
            t0 + timedelta(hours=1), t0 + timedelta(hours=1),
        )
        for event_id in (earlier, later):
            for actor in actors:
                await conn.execute(
                    "INSERT INTO event_entity_links"
                    " (event_id, entity_id, role, derived_from)"
                    " VALUES ($1,$2,'actor','{}'::uuid[])",
                    event_id, actor,
                )
        opts = {
            "sub_handler": "event_reconciler",
            "analyst_id": "analyst.event_reconciler_test",
            "analyst_version": "p1-test",
            "run_id": uuid4(),
            "max_events": 100,
            "evolves_min_actors": 2,
        }
        result = await event_reconciler.handle(
            [], opts, SimpleNamespace(pg_pool=pg_pool)
        )
        assert result.finding.data["evolves_written"] == 1
        edge = await conn.fetchrow(
            "SELECT * FROM event_edges WHERE edge_type='evolves_from'"
        )
        assert edge["src_event_id"] == later
        assert edge["dst_event_id"] == earlier
        with pytest.raises(event_reconciler.EventEdgeUnsupportedError,
                           match="SEAMS #56"):
            await event_reconciler.write_event_edge(
                conn,
                src_event_id=earlier,
                dst_event_id=later,
                edge_type="caused_by",
                why="unsupported",
                analyst_ctx=event_reconciler._ctx(opts),
            )


@pytest.mark.asyncio
async def test_dispatcher_routes_event_subhandlers() -> None:
    """The real dispatch path resolves both names and returns result objects."""
    for name in ("event_clustering", "event_reconciler"):
        result = await run_method(
            [], {"sub_handler": name}, deps=None
        )
        assert result.finding.data["sub_handler"] == name


# ---------------------------------------------------------------------------
# P1b — the pass budget, the resume cursors and the per-phase receipt timings
# ---------------------------------------------------------------------------


class _FiniteBudget:
    """A ``PassBudget`` stand-in that reports spent after ``checks`` probes.

    Each ``exhausted()`` call is one probe: the first ``checks`` answer False
    and every probe after that answers True, so a test can place the
    exhaustion point exactly instead of racing the wall clock.
    """

    def __init__(self, checks: int) -> None:
        self._checks_left = checks

    def exhausted(self) -> bool:
        self._checks_left -= 1
        return self._checks_left < 0


def _watermark_state(row) -> dict:
    """Decode one ``alert_trigger_watermarks.state`` jsonb cell."""
    state = row["state"]
    return json.loads(state) if isinstance(state, str) else dict(state)


_PHASES = {"slice", "cluster", "match", "write", "tower", "lifecycle"}
_NIL = "00000000-0000-0000-0000-000000000000"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_budget_truncated_pass_saves_cursor_and_resumes(
    pg_pool, monkeypatch
) -> None:
    """A pass that spends its budget mid-write ends inside the turn with a
    partial receipt and an inclusive resume cursor; the next tick picks the
    slice up there instead of re-examining it."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    # Deterministic candidate order: cluster order follows each cluster's
    # smallest member uuid, so cluster A is written and cluster B deferred.
    id_a1 = UUID("00000000-0000-4000-8000-0000000000a1")
    id_a2 = UUID("00000000-0000-4000-8000-0000000000a2")
    id_b1 = UUID("00000000-0000-4000-8000-0000000000b1")
    id_b2 = UUID("00000000-0000-4000-8000-0000000000b2")
    async with pg_pool.acquire() as conn:
        ent_a = await _entity(conn, f"P1b Alpha {uuid4().hex[:8]}")
        ent_b = await _entity(conn, f"P1b Beta {uuid4().hex[:8]}")
        a1 = await _signal(
            conn, title="Alpha plant blast", source_id="rss_a1",
            fetched_at=base, signal_id=id_a1,
        )
        a2 = await _signal(
            conn, title="Alpha plant blast", source_id="rss_a2",
            fetched_at=base + timedelta(minutes=1), signal_id=id_a2,
        )
        b1 = await _signal(
            conn, title="Beta convoy strike", source_id="rss_b1",
            fetched_at=base + timedelta(minutes=10), signal_id=id_b1,
        )
        b2 = await _signal(
            conn, title="Beta convoy strike", source_id="rss_b2",
            fetched_at=base + timedelta(minutes=11), signal_id=id_b2,
        )
        for sid, ent in ((a1, ent_a), (a2, ent_a), (b1, ent_b), (b2, ent_b)):
            await _link_signal_entity(conn, sid, ent)

    # Probes: semantic gate, pre-cluster, post-cluster, then one per write
    # candidate — four pass, the fifth (candidate B's turn check) spends it.
    budgets = iter([_FiniteBudget(4), _FiniteBudget(10 ** 6)])
    monkeypatch.setattr(
        event_clustering, "PassBudget", lambda seconds=None: next(budgets)
    )
    opts = _options(analyst_id="analyst.p1b_budget")
    first = await event_clustering.handle(
        [], opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    data = first.finding.data
    assert data["budget_exceeded"] is True
    assert data["stopped_in_phase"] == "write"
    assert set(data["phase_seconds"]) == _PHASES
    assert data["funnel"]["examined"] == 4
    assert data["funnel"]["promoted"] == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM events") == 1

    # The deferred candidate's oldest member is the INCLUSIVE resume bound.
    cursor = data["cursor"]["signal_slice"]
    assert cursor["id"] == _NIL
    assert cursor["ts"] == (base + timedelta(minutes=10)).isoformat()
    wm = await pg_pool.fetchrow(
        "SELECT state FROM alert_trigger_watermarks"
        " WHERE trigger_class='event_clustering'"
        " AND watermark_key='signal_slice'"
    )
    assert _watermark_state(wm)["id"] == _NIL

    second = await event_clustering.handle(
        [], opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    data2 = second.finding.data
    assert data2["budget_exceeded"] is False
    # Only cluster B's two members sit at-or-after the cursor — the run
    # resumes instead of re-examining the whole slice.
    assert data2["funnel"]["examined"] == 2
    assert data2["funnel"]["promoted"] == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM events") == 2
    tail = data2["cursor"]["signal_slice"]
    assert tail["id"] == str(id_b2)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tiny_pass_budget_returns_inside_the_turn(
    pg_pool, monkeypatch
) -> None:
    """The descriptor-side ``pass_budget_seconds`` mirror bounds the real
    wall-clock budget; nothing consumed means nothing advances."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    async with pg_pool.acquire() as conn:
        ent = await _entity(conn, f"P1b Fordow {uuid4().hex[:8]}")
        for src in ("rss_x1", "rss_x2"):
            sid = await _signal(
                conn, title="Strike hits Fordow", source_id=src
            )
            await _link_signal_entity(conn, sid, ent)
    result = await event_clustering.handle(
        [],
        _options(pass_budget_seconds=0.000001),
        SimpleNamespace(pg_pool=pg_pool, extras={}),
    )
    data = result.finding.data
    assert data["budget_exceeded"] is True
    assert data["stopped_in_phase"] == "cluster"
    assert data["funnel"]["examined"] == 2
    assert set(data["phase_seconds"]) == _PHASES
    assert data["phase_seconds"]["slice"] > 0
    assert await pg_pool.fetchval("SELECT count(*) FROM events") == 0
    # Nothing was consumed, so no cursor was written — the slice is re-offered.
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM alert_trigger_watermarks"
        " WHERE trigger_class='event_clustering'"
    ) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clean_pass_receipt_carries_phase_timings_and_cursors(
    pg_pool, monkeypatch
) -> None:
    """Every receipt carries the six phase timings, the budget state and all
    three cursor keys — clean, synthetic, truncated or flag-off alike."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    result = await event_clustering.handle(
        [], _options(), SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    data = result.finding.data
    assert data["budget_exceeded"] is False
    assert data["stopped_in_phase"] is None
    assert set(data["phase_seconds"]) == _PHASES
    assert set(data["cursor"]) == {
        "signal_slice", "tower_findings", "tower_situations"
    }
    flag_off = await event_clustering.handle(
        [], _options(), SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    monkeypatch.delenv("LEGBA_EVENTS")
    flag_off = await event_clustering.handle(
        [], _options(), SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    assert flag_off.finding.data["events_enabled"] is False
    assert set(flag_off.finding.data["phase_seconds"]) == _PHASES


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciler_budget_releases_the_turn(pg_pool, monkeypatch) -> None:
    """The reconciler honors the same discipline: with the env ceiling spent
    it scans, writes nothing, and names the phase it stopped in."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    monkeypatch.setenv("LEGBA_EVENT_RECONCILER_PASS_BUDGET_SECONDS", "0.000001")
    async with pg_pool.acquire() as conn:
        actors = [await _entity(conn, f"P1b Actor {i}") for i in range(2)]
        t0 = datetime.now(timezone.utc) - timedelta(hours=3)
        earlier = await conn.fetchval(
            "INSERT INTO events (event_signature, analyst_id, title,"
            " time_start, time_end) VALUES ($1,'a','old',$2,$3) RETURNING id",
            f"evt:old-{uuid4().hex[:6]}#evt:ir", t0, t0,
        )
        later = await conn.fetchval(
            "INSERT INTO events (event_signature, analyst_id, title,"
            " time_start, time_end) VALUES ($1,'a','new',$2,$3) RETURNING id",
            f"evt:new-{uuid4().hex[:6]}#evt:ir",
            t0 + timedelta(hours=1), t0 + timedelta(hours=1),
        )
        for event_id in (earlier, later):
            for actor in actors:
                await conn.execute(
                    "INSERT INTO event_entity_links"
                    " (event_id, entity_id, role, derived_from)"
                    " VALUES ($1,$2,'actor','{}'::uuid[])",
                    event_id, actor,
                )
    result = await event_reconciler.handle(
        [],
        {
            "sub_handler": "event_reconciler",
            "analyst_id": "analyst.event_reconciler_test",
            "analyst_version": "p1b-test",
            "run_id": uuid4(),
            "max_events": 100,
        },
        SimpleNamespace(pg_pool=pg_pool),
    )
    data = result.finding.data
    assert data["budget_exceeded"] is True
    assert data["stopped_in_phase"] == "correlated"
    assert data["events_examined"] == 2
    assert data["correlated_written"] == 0
    assert data["evolves_written"] == 0
    assert set(data["phase_seconds"]) == {"scan", "correlated", "evolves"}
    assert data["phase_seconds"]["scan"] > 0
    assert await pg_pool.fetchval("SELECT count(*) FROM event_edges") == 0


# ---------------------------------------------------------------------------
# P1c — where the turn's seconds go: blocking, lifecycle-first, the tower wall
# ---------------------------------------------------------------------------


def _slice_of(count: int, *, span_hours: float, actors: int) -> list:
    """A deterministic synthetic signal slice, shaped like the live one.

    ``actors`` sets how thinly entities are spread: the live newest-300 slice
    has ~2.3% of its pairs sharing a resolved entity, which a wide roster
    reproduces and a narrow one (every signal naming the same ten countries)
    does not.
    """
    words = (
        "strike convoy plant blast talks sanctions drone border missile "
        "port raid"
    ).split()
    roster = [f"Actor {i}" for i in range(actors)]
    rnd = random.Random(7)
    base = datetime.now(timezone.utc) - timedelta(hours=span_hours)
    rows = []
    for i in range(count):
        names = tuple(rnd.sample(roster, rnd.randint(1, 3)))
        rows.append(EventEvidence(
            id=UUID(int=i + 1),
            title=" ".join(rnd.sample(words, 6)) + f" {names[0]}",
            category=rnd.choice(["conflict", "diplomacy", "economy"]),
            tags=(f"severity:{rnd.choice(['high', 'medium'])}",),
            fetched_at=base + timedelta(
                seconds=rnd.randint(0, int(span_hours * 3600))
            ),
            source_id=f"rss_{rnd.randint(0, 40)}",
            geo=("ir",),
            entity_names=names,
        ))
    return rows


def test_blocking_holds_a_300_signal_slice_inside_the_turn() -> None:
    """The descriptor's steady-state page — 300 signals over the 24 h
    lookback — clusters well inside the 120 s turn at the shipped window.

    Measured in the container at the pinned SHA: 31.8 s unblocked (the whole
    44,850-pair triangle) against 16.2 s at the 6 h default. The bound here is
    the lane's 20 s target, not the measurement, so ordinary jitter on a box
    shared by six lanes does not turn it red.
    """
    rows = _slice_of(300, span_hours=24.0, actors=240)
    started = time.monotonic()
    clusters, counts = cluster_evidence(rows)
    elapsed = time.monotonic() - started

    assert counts["pairs_blocked"] > 0
    # scored + blocked partition the triangle exactly.
    assert counts["pairs_examined"] + counts["pairs_blocked"] == 300 * 299 // 2
    assert counts["pairs_examined"] < 300 * 299 // 2
    assert clusters
    assert elapsed < 20.0, f"300-signal slice took {elapsed:.1f}s"


def test_blocking_admits_shared_entities_and_facts_at_any_age() -> None:
    """A pair outside the window still scores when it shares an entity or a
    fact subject — the window is a safety net for pairs with neither, never a
    ceiling on corroboration."""
    old = datetime.now(timezone.utc) - timedelta(days=9)
    new = datetime.now(timezone.utc)

    shared_entity = [
        _ev(title="Fordow site struck", fetched_at=old,
            entity_names=("Fordow",)),
        _ev(title="Fordow site struck", fetched_at=new, source_id="src_b",
            entity_names=("Fordow",)),
    ]
    _clusters, counts = cluster_evidence(shared_entity, block_window_hours=0.0)
    assert counts["pairs_examined"] == 1
    assert counts["pairs_blocked"] == 0

    shared_fact = [
        _ev(title="Alpha", fetched_at=old, facts=frozenset({"uranium stock"})),
        _ev(title="Beta", fetched_at=new, source_id="src_b",
            facts=frozenset({"uranium stock"})),
    ]
    _clusters, counts = cluster_evidence(shared_fact, block_window_hours=0.0)
    assert counts["pairs_examined"] == 1
    assert counts["shared_fact_linked"] == 1

    # Nothing in common and outside the window: defined unrelated, not scored.
    strangers = [
        _ev(title="Alpha", fetched_at=old, entity_names=("Alpha Co",)),
        _ev(title="Beta", fetched_at=new, source_id="src_b",
            entity_names=("Beta Co",)),
    ]
    _clusters, counts = cluster_evidence(strangers, block_window_hours=0.0)
    assert counts["pairs_examined"] == 0
    assert counts["pairs_blocked"] == 1


def test_blocking_admits_a_supplied_embedding_pair() -> None:
    """The cosine gate is ADDITIVE, so a high-cosine pair must survive
    blocking even with no shared entity and no temporal overlap."""
    left = _ev(
        title="Alpha",
        fetched_at=datetime.now(timezone.utc) - timedelta(days=9),
        entity_names=("Alpha Co",), embedding_ref=str(uuid4()),
    )
    right = _ev(
        title="Beta", fetched_at=datetime.now(timezone.utc), source_id="src_b",
        entity_names=("Beta Co",), embedding_ref=str(uuid4()),
    )
    pair = (
        (left.id, right.id) if str(left.id) <= str(right.id)
        else (right.id, left.id)
    )
    _clusters, counts = cluster_evidence(
        [left, right], block_window_hours=0.0, embedding_cosines={pair: 0.99},
    )
    assert counts["pairs_examined"] == 1
    assert counts["embedding_linked"] == 1


def test_prepared_split_and_aggregate_hoist_change_no_verdict() -> None:
    """``cluster_aggregate`` passed in must score identically to letting
    ``match_cluster_to_event`` derive it."""
    members = [
        _ev(title="Strike hits Fordow", entity_names=("Fordow", "Iran")),
        _ev(title="Strike hits Fordow site", source_id="src_b",
            entity_names=("Fordow",)),
    ]
    row = {
        "id": uuid4(),
        "title": "Strike hits Fordow",
        "category": "conflict",
        "entity_names": ["Fordow", "Iran"],
        "geo": ["ir"],
        "latest_linked_at": datetime.now(timezone.utc),
    }
    derived = match_cluster_to_event(members, row)
    hoisted = match_cluster_to_event(
        members, row, aggregate=cluster_aggregate(members)
    )
    assert derived == hoisted
    assert derived.linked

    # A member with no evidence time used to raise TypeError here
    # (``datetime.replace(tz=...)`` is not a keyword) — the aggregate is the
    # one place that fallback is reached.
    assert cluster_aggregate([_ev(title="No clock", fetched_at=None)]) is not None
    assert cluster_aggregate([]) is None


def test_prepared_aggregate_scores_identically_to_the_derived_path() -> None:
    """P1d: ``prepare_aggregate`` + ``match_prepared_to_event`` must score
    BYTE-IDENTICAL to ``match_cluster_to_event(..., aggregate=...)`` — the
    whole point is eliminating redundant work, never changing a verdict."""
    members = [
        _ev(title="Strike hits Fordow", entity_names=("Fordow", "Iran")),
        _ev(title="Strike hits Fordow site", source_id="src_b",
            entity_names=("Fordow",)),
    ]
    row = {
        "id": uuid4(),
        "title": "Strike hits Fordow",
        "category": "conflict",
        "entity_names": ["Fordow", "Iran"],
        "geo": ["ir"],
        "latest_linked_at": datetime.now(timezone.utc),
    }
    aggregate = cluster_aggregate(members)
    via_derive = match_cluster_to_event(members, row, aggregate=aggregate)
    prepared = prepare_aggregate(aggregate)
    via_prepared = match_prepared_to_event(prepared, row)
    assert via_derive == via_prepared
    assert via_prepared.linked

    # A non-matching row scores identically too, not just the linked case.
    other = {**row, "entity_names": ["North Korea"], "geo": ["kp"],
              "title": "Talks continue in Pyongyang"}
    assert (
        match_cluster_to_event(members, other, aggregate=aggregate)
        == match_prepared_to_event(prepared, other)
    )


def test_prepared_events_path_scores_identically_to_the_per_call_path() -> None:
    """P1e: ``prepare_open_events`` + ``match_prepared_pair`` — the EVENT side
    folded ONCE, ahead of the walk — must score BYTE-IDENTICAL to
    ``match_prepared_to_event``, which re-``_prepare``s the event side on
    every call. Folding the event side once must never change a verdict,
    only when the fold happens."""
    members = [
        _ev(title="Strike hits Fordow", entity_names=("Fordow", "Iran")),
        _ev(title="Strike hits Fordow site", source_id="src_b",
            entity_names=("Fordow",)),
    ]
    linked_row = {
        "id": uuid4(),
        "title": "Strike hits Fordow",
        "category": "conflict",
        "entity_names": ["Fordow", "Iran"],
        "geo": ["ir"],
        "latest_linked_at": datetime.now(timezone.utc),
    }
    unlinked_row = {
        "id": uuid4(),
        "title": "Talks continue in Pyongyang",
        "category": "diplomacy",
        "entity_names": ["North Korea"],
        "geo": ["kp"],
        "latest_linked_at": datetime.now(timezone.utc),
    }
    rows = [linked_row, unlinked_row]
    aggregate = cluster_aggregate(members)
    prepared_aggregate = prepare_aggregate(aggregate)

    prepared_events = prepare_open_events(rows)
    assert [item.row for item in prepared_events] == rows

    for row, item in zip(rows, prepared_events):
        via_old = match_prepared_to_event(prepared_aggregate, row)
        via_new = match_prepared_pair(prepared_aggregate, item.prepared)
        assert via_old == via_new
    assert match_prepared_pair(prepared_aggregate, prepared_events[0].prepared).linked
    assert not match_prepared_pair(prepared_aggregate, prepared_events[1].prepared).linked


def test_opposed_compass_stem_restricted_walk_matches_old_cross_product() -> None:
    """P1f: ``_opposed_compass_stem`` now walks only (left direction-bearing
    x right ALL) union (left ALL x right direction-bearing) over each row's
    precomputed ``_NameProbe`` list, instead of the full name x name cross
    product re-tokenizing on every pairwise call. This must return EXACTLY
    the same boolean as the old cross product on every pair — checked over
    thousands of random surface-list pairs that draw both the POSITIONAL
    branch of ``differs_by_direction`` (equal token counts) and its
    MULTISET branch (unequal counts), opposed-direction pairs, same-
    direction pairs, and pairs whose stems differ even though both carry a
    direction token."""
    from legba.data._entity_canon import differs_by_direction
    from legba.data.analysts.deterministic_handlers._event_matcher import (
        _non_direction_stem,
        _opposed_compass_stem,
        _prepare,
    )

    def _old_opposed_compass_stem(
        left_names: tuple[str, ...], right_names: tuple[str, ...]
    ) -> bool:
        """The pre-P1f reference: the O(n x m) cross product over the raw
        string predicate — exactly what ``_opposed_compass_stem`` did
        before this lane hoisted the per-row probes."""
        for a in left_names:
            for b in right_names:
                if not differs_by_direction(a, b):
                    continue
                stem_a = _non_direction_stem(a)
                stem_b = _non_direction_stem(b)
                if stem_a and stem_a == stem_b:
                    return True
        return False

    directions = [
        "North", "South", "East", "West", "Upper", "Lower", "Central",
        "Nord", "Sur", "Norte",
    ]
    stems_one_token = ["Korea", "Sudan", "Yemen", "Darfur"]
    stems_multi_token = ["South Ossetia Province", "Kivu Border Region"]
    plain = [
        "Iran", "Russia", "the UN", "NATO", "Hezbollah", "Fordow",
        "Oliver North", "Veronica Lake",
    ]

    rng = random.Random(202609241)

    def _random_name() -> str:
        roll = rng.random()
        if roll < 0.30:
            # Direction-bearing, single-token stem — equal-length-prone,
            # exercises the POSITIONAL branch on a like-for-like pair.
            return f"{rng.choice(directions)} {rng.choice(stems_one_token)}"
        if roll < 0.45:
            # Direction-bearing, multi-token stem — unequal-length-prone
            # against a single-token surface, exercises the MULTISET branch.
            return f"{rng.choice(directions)} {rng.choice(stems_multi_token)}"
        if roll < 0.55:
            # A direction token present but NOT leading (surname-shaped,
            # e.g. "Oliver North") — carries a direction token without
            # being a compass place name; must never falsely match.
            return f"{rng.choice(plain)} {rng.choice(directions)}"
        return rng.choice(plain)

    def _random_names(n: int) -> tuple[str, ...]:
        return tuple(_random_name() for _ in range(n))

    checked_true = 0
    for _ in range(3000):
        left_names = _random_names(rng.randint(1, 4))
        right_names = _random_names(rng.randint(1, 4))
        left = _prepare(_ev(title="l", entity_names=left_names))
        right = _prepare(_ev(title="r", entity_names=right_names))
        expected = _old_opposed_compass_stem(left_names, right_names)
        actual = _opposed_compass_stem(left, right)
        assert actual == expected, (left_names, right_names, expected, actual)
        checked_true += expected
    # The fixture must actually exercise the True branch, not merely prove a
    # vacuous "always False" restriction.
    assert checked_true > 100, checked_true


def test_match_event_reuses_a_prepared_event_list_across_candidates() -> None:
    """P1e: ``_match_event(..., prepared_events=...)`` must return the exact
    same outcome as the un-prepared call over the same events — the prepared
    list is an amortization of the SAME fold, not a different feature set."""
    from legba.data.analysts.deterministic_handlers.event_clustering import (
        _match_event,
    )

    members = (_ev(title="Strike hits Fordow", entity_names=("Fordow", "Iran")),)
    candidate = cluster_candidate(EventCluster(members=members, oversized=False))
    events = [
        {
            "id": uuid4(),
            "event_signature": f"evt:sig-{i}",
            "title": "Strike hits Fordow" if i == 3 else f"Unrelated report {i}",
            "category": "conflict" if i == 3 else "trade",
            "entity_names": ["Fordow", "Iran"] if i == 3 else ["Unrelated Entity"],
            "geo": ["ir"] if i == 3 else ["us"],
            "latest_linked_at": datetime.now(timezone.utc),
        }
        for i in range(10)
    ]
    prepared_events = prepare_open_events(events)

    baseline = _match_event(candidate, events, threshold=0.5, prefilter=True)
    via_prepared = _match_event(
        candidate, events, threshold=0.5, prefilter=True,
        prepared_events=prepared_events,
    )
    assert baseline.matched is not None
    assert baseline.matched == via_prepared.matched
    assert baseline.capped == via_prepared.capped
    assert baseline.cut == via_prepared.cut


def test_representative_members_caps_by_recency_then_distinct_source() -> None:
    """P1d member cap: newest first, then filled out by distinct source — the
    COMPARE shrinks, the candidate's own member tuple never does."""
    from legba.data.analysts.deterministic_handlers.event_clustering import (
        _representative_members,
    )

    now = datetime.now(timezone.utc)
    # Three sources, two members each, newest first within each source.
    members = tuple(
        _ev(
            title=f"m{i}",
            source_id=f"src_{i % 3}",
            fetched_at=now - timedelta(minutes=i),
        )
        for i in range(6)
    )
    # No cap needed: the full tuple comes back unchanged.
    assert _representative_members(members, 10) == members
    assert _representative_members(members, 0) == members

    capped = _representative_members(members, 3)
    assert len(capped) == 3
    # The three newest members already cover all three distinct sources
    # (m0/src_0, m1/src_1, m2/src_2) — the distinct-source pass has nothing
    # left to add, so the newest-first slice IS the answer here.
    assert {m.source_id for m in capped} == {"src_0", "src_1", "src_2"}
    assert {m.title for m in capped} == {"m0", "m1", "m2"}


def test_prefilter_open_events_by_category_or_geo_with_fallback() -> None:
    """P1d pre-filter: category-or-geo overlap with the candidate's OWN
    fields, and a safety valve that never drops every row."""
    from legba.data.analysts.deterministic_handlers.event_clustering import (
        _prefilter_open_events,
    )

    candidate = cluster_candidate(EventCluster(
        members=(_ev(title="x", category="conflict", geo=("ir",)),),
        oversized=False,
    ))
    same_category = {"id": uuid4(), "category": "conflict", "geo": ["kp"]}
    same_geo = {"id": uuid4(), "category": "diplomacy", "geo": ["ir"]}
    unrelated = {"id": uuid4(), "category": "trade", "geo": ["us"]}
    events = [same_category, same_geo, unrelated]
    filtered = _prefilter_open_events(candidate, events)
    assert same_category in filtered
    assert same_geo in filtered
    assert unrelated not in filtered

    # Nothing plausibly matches: fall back to the full set rather than ever
    # silently refuse a reattachment the heuristic cannot see.
    none_match = [
        {"id": uuid4(), "category": "trade", "geo": ["us"]},
        {"id": uuid4(), "category": "diplomacy", "geo": ["cn"]},
    ]
    assert _prefilter_open_events(candidate, none_match) == none_match


def test_match_event_bounds_a_120_member_candidate_under_the_wall() -> None:
    """P1d, the incident this lane fixes: a 120-member tower candidate
    matched against a large open-event set used to run 254.3 s against a
    10 s wall (finding 128dcb9c, 2026-09-24) because
    ``asyncio.wait_for`` cannot cancel the synchronous ``_match_event`` walk.
    With the prepared-aggregate path, the member cap and the category/geo
    pre-filter engaged (the shape ``_write_tower_candidate`` now uses), the
    same shape completes in a small fraction of the wall."""
    geo_pool = ["ir", "us", "ru", "cn", "il", "ua", "sa", "tr", "kp", "eu"]
    entity_pool = [
        "Iran", "United States", "Russia", "China", "Israel", "Ukraine",
        "European Union", "Saudi Arabia", "North Korea", "Turkey",
        "Islamic Revolutionary Guard Corps", "United Nations Security "
        "Council", "Hezbollah", "Hamas", "NATO", "Pentagon", "White House",
        "Kremlin", "Central Bank of Iran", "OPEC", "World Bank",
        "Gulf Cooperation Council", "Strait of Hormuz", "Persian Gulf",
        "Tehran", "Moscow", "Beijing", "Washington", "Brussels", "Ankara",
        "Riyadh", "South Korea", "West Bank",
    ] + [f"Entity {i}" for i in range(120)]
    rng = random.Random(2026)

    members = tuple(
        _ev(
            title=f"Sanctions tighten on economic partners round {i}",
            category="economic_coercion",
            source_id=f"src_{i % 7}",
            geo=("ir",),
            fetched_at=datetime.now(timezone.utc) - timedelta(hours=rng.uniform(0, 720)),
            entity_names=tuple(rng.sample(entity_pool, rng.randint(1, 5))),
        )
        for i in range(120)
    )
    candidate = cluster_candidate(EventCluster(members=members, oversized=False))
    events = [
        {
            "id": uuid4(),
            "event_signature": f"evt:sig-{i}",
            "title": f"Existing event {i} about sanctions and coercion",
            "category": rng.choice(
                ["economic_coercion", "conflict", "diplomacy", "sanctions", "trade"]
            ),
            "geo": [rng.choice(geo_pool)],
            "time_start": datetime.now(timezone.utc) - timedelta(days=rng.uniform(0, 30)),
            "time_end": datetime.now(timezone.utc) - timedelta(hours=rng.uniform(0, 48)),
            "latest_linked_at": datetime.now(timezone.utc) - timedelta(hours=rng.uniform(0, 48)),
            "entity_names": rng.sample(entity_pool, rng.randint(2, 12)),
        }
        for i in range(2000)
    ]

    started = time.monotonic()
    outcome = event_clustering._match_event(
        candidate, events, threshold=0.5, max_members=20, prefilter=True,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"took {elapsed:.3f}s against a 10s wall"
    assert outcome.capped is True
    assert outcome.cut is False


def test_prepare_open_events_amortizes_across_many_tower_candidates() -> None:
    """P1e — the fix's realistic shape and the incident it closes. Live
    2026-09-24, with the P1d cap and pre-filter already engaged,
    ``_match_event`` still cost 90.5 s / 106.4 s / 13.8 s for the three
    candidates past the cursor, because the open-event set — unchanged for
    the whole tick — was re-``_prepare``d fresh for EVERY candidate. Here:
    1,000 open events, each carrying 50-300 entity mentions and a long
    title (the heavy fold this lane amortizes), matched against 3 different
    120-member candidates. ONE ``prepare_open_events`` call folds the whole
    set; every candidate's match must then land well under the
    ``tower_candidate_max_seconds`` wall — this is the shape that used to
    blow it. No directional-compass entity names (``North Korea``, ``West
    Bank``, ...) in the pool: at this density they would make almost every
    pair trip the pre-existing O(members x mentions) opposed-direction scan
    (``_opposed_compass_stem``) — a real, separate cost this lane does not
    touch, not the redundant-preparation cost it fixes."""
    entity_pool = [f"Entity {i}" for i in range(400)] + [
        "Iran", "United States", "Russia", "China", "Israel", "Ukraine",
        "European Union", "Saudi Arabia", "Republic of Korea", "Turkey",
        "Islamic Revolutionary Guard Corps", "Hezbollah", "Hamas", "NATO",
        "Pentagon", "White House", "Kremlin", "OPEC", "World Bank",
        "Strait of Hormuz", "Persian Gulf", "Tehran", "Moscow", "Beijing",
    ]
    geo_pool = ["ir", "us", "ru", "cn", "il", "ua", "sa", "tr", "kp", "eu"]
    categories = ["economic_coercion", "conflict", "diplomacy", "sanctions", "trade"]
    title_words = [f"word{i}" for i in range(60)]
    rng = random.Random(20260924)

    events = [
        {
            "id": uuid4(),
            "event_signature": f"evt:sig-{i}",
            "title": (
                f"Existing event {i} about sanctions and coercion — "
                + " ".join(rng.sample(title_words, 12))
            ),
            "category": rng.choice(categories),
            "geo": [rng.choice(geo_pool)],
            "time_start": datetime.now(timezone.utc) - timedelta(days=rng.uniform(0, 30)),
            "time_end": datetime.now(timezone.utc) - timedelta(hours=rng.uniform(0, 48)),
            "latest_linked_at": datetime.now(timezone.utc) - timedelta(hours=rng.uniform(0, 48)),
            "entity_names": rng.choices(entity_pool, k=rng.randint(50, 300)),
        }
        for i in range(1000)
    ]

    started = time.monotonic()
    prepared_events = prepare_open_events(events)
    prepare_elapsed = time.monotonic() - started
    assert len(prepared_events) == 1000
    assert prepare_elapsed < 10.0, f"one-time preparation took {prepare_elapsed:.3f}s"

    candidates = [
        cluster_candidate(EventCluster(
            members=tuple(
                _ev(
                    title=f"Sanctions tighten on economic partners round {c}-{i}",
                    category="economic_coercion",
                    source_id=f"src_{i % 7}",
                    geo=("ir",),
                    fetched_at=datetime.now(timezone.utc) - timedelta(hours=rng.uniform(0, 720)),
                    entity_names=tuple(rng.sample(entity_pool, rng.randint(1, 5))),
                )
                for i in range(120)
            ),
            oversized=False,
        ))
        for c in range(3)
    ]

    per_candidate_seconds = []
    for candidate in candidates:
        started = time.monotonic()
        outcome = event_clustering._match_event(
            candidate, events, threshold=0.5, max_members=20, prefilter=True,
            prepared_events=prepared_events,
        )
        elapsed = time.monotonic() - started
        per_candidate_seconds.append(elapsed)
        assert outcome.capped is True
        # The brief's target is ~1 s; the bound here carries headroom for the
        # shared test box (six wave-F lanes contend for the same CPU) without
        # hiding a regression — this is still two orders of magnitude under
        # the 90.5 s / 106.4 s / 13.8 s live-profiled incident and a fifth of
        # the 10 s tower_candidate_max_seconds wall.
        assert elapsed < 2.0, (
            f"per-candidate match took {elapsed:.3f}s against the 2s bound "
            f"(one-time preparation took {prepare_elapsed:.3f}s)"
        )


def test_prepare_open_events_amortizes_with_direction_bearing_names() -> None:
    """P1f — the shape ``test_prepare_open_events_amortizes_across_many_
    tower_candidates`` (P1e) explicitly declined to cover: 'No directional-
    compass entity names ... in the pool: at this density they would make
    almost every pair trip the pre-existing O(members x mentions) opposed-
    direction scan (_opposed_compass_stem) — a real, separate cost this lane
    does not touch.' This is that cost, closed. Roughly a THIRD of the
    entity pool here is direction-bearing (North Region7, South Region12,
    ...) — the live density the brief measured (North Korea, South Sudan,
    West Bank, Eastern Europe, ... on almost every real row) — so
    ``has_direction`` is True for nearly every candidate and nearly every
    open event, and it is the RESTRICTED pair walk over precomputed probes,
    not the row-level gate alone, standing between this test and the
    pre-fix profile (37.9 s total / 36.3 s in ``_opposed_compass_stem`` /
    2,507,128 calls to ``differs_by_direction`` for ONE 125-member candidate
    against 150 open events, 2026-09-24)."""
    direction_words = ["North", "South", "East", "West", "Upper", "Lower", "Central"]
    stem_words = [f"Region{i}" for i in range(29)]
    directional_pool = [f"{d} {s}" for d in direction_words for s in stem_words]
    plain_pool = [f"Entity {i}" for i in range(400)] + [
        "Iran", "United States", "Russia", "China", "Israel", "Ukraine",
        "European Union", "Saudi Arabia", "Republic of Korea", "Turkey",
        "Islamic Revolutionary Guard Corps", "Hezbollah", "Hamas", "NATO",
        "Pentagon", "White House", "Kremlin", "OPEC", "World Bank",
        "Strait of Hormuz", "Persian Gulf", "Tehran", "Moscow", "Beijing",
    ]
    entity_pool = plain_pool + directional_pool
    direction_share = len(directional_pool) / len(entity_pool)
    assert 0.30 <= direction_share <= 0.40, direction_share

    geo_pool = ["ir", "us", "ru", "cn", "il", "ua", "sa", "tr", "kp", "eu"]
    categories = ["economic_coercion", "conflict", "diplomacy", "sanctions", "trade"]
    title_words = [f"word{i}" for i in range(60)]
    rng = random.Random(202609242)

    events = [
        {
            "id": uuid4(),
            "event_signature": f"evt:sig-{i}",
            "title": (
                f"Existing event {i} about sanctions and coercion — "
                + " ".join(rng.sample(title_words, 12))
            ),
            "category": rng.choice(categories),
            "geo": [rng.choice(geo_pool)],
            "time_start": datetime.now(timezone.utc) - timedelta(days=rng.uniform(0, 30)),
            "time_end": datetime.now(timezone.utc) - timedelta(hours=rng.uniform(0, 48)),
            "latest_linked_at": datetime.now(timezone.utc) - timedelta(hours=rng.uniform(0, 48)),
            "entity_names": rng.choices(entity_pool, k=rng.randint(50, 300)),
        }
        for i in range(1000)
    ]

    started = time.monotonic()
    prepared_events = prepare_open_events(events)
    prepare_elapsed = time.monotonic() - started
    assert len(prepared_events) == 1000
    assert prepare_elapsed < 10.0, f"one-time preparation took {prepare_elapsed:.3f}s"
    # Live density: with a third of the pool direction-bearing and 50-300
    # mentions per event, virtually every open event carries at least one —
    # the scenario that makes the row-level ``has_direction`` gate alone
    # worthless and the restricted pair walk load-bearing.
    direction_bearing_events = sum(
        1 for item in prepared_events if item.prepared.has_direction
    )
    assert direction_bearing_events > 900, direction_bearing_events

    candidates = [
        cluster_candidate(EventCluster(
            members=tuple(
                _ev(
                    title=f"Sanctions tighten on economic partners round {c}-{i}",
                    category="economic_coercion",
                    source_id=f"src_{i % 7}",
                    geo=("ir",),
                    fetched_at=datetime.now(timezone.utc) - timedelta(hours=rng.uniform(0, 720)),
                    entity_names=tuple(rng.sample(entity_pool, rng.randint(1, 5))),
                )
                for i in range(120)
            ),
            oversized=False,
        ))
        for c in range(3)
    ]

    for candidate in candidates:
        started = time.monotonic()
        outcome = event_clustering._match_event(
            candidate, events, threshold=0.5, max_members=20, prefilter=True,
            prepared_events=prepared_events,
        )
        elapsed = time.monotonic() - started
        assert outcome.capped is True
        # The brief's bound: well under 1 s per candidate against 1,000
        # open events — still two orders of magnitude under the pre-fix
        # 36.3 s ``_opposed_compass_stem`` profile.
        assert elapsed < 1.0, (
            f"per-candidate match with direction-bearing names took "
            f"{elapsed:.3f}s against the 1s bound "
            f"(one-time preparation took {prepare_elapsed:.3f}s)"
        )


def _open_event_row(**overrides: object) -> dict:
    """One open-event row in ``_open_event_read``'s projected shape."""
    base: dict = {
        "id": UUID("11111111-1111-1111-1111-111111111111"),
        "event_signature": "evt:sig-1",
        "title": "Sanctions tighten on Iranian energy exports",
        "category": "economic_coercion",
        "geo": ["ir"],
        "time_start": datetime(2026, 9, 20, tzinfo=timezone.utc),
        "time_end": datetime(2026, 9, 21, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 22, tzinfo=timezone.utc),
        "signal_ids": [UUID("aaaaaaaa-0000-0000-0000-000000000001")],
        "source_ids": ["src_a"],
        "latest_linked_at": datetime(2026, 9, 22, 6, tzinfo=timezone.utc),
        "entity_ids": [UUID("bbbbbbbb-0000-0000-0000-000000000001")],
        "entity_names": ["Iran", "United States"],
    }
    base.update(overrides)
    return base


def test_prepared_cache_hits_an_identical_row_across_calls() -> None:
    """P1g — the cross-tick cache's whole point. An open event unchanged in
    every column the fold reads is folded ONCE; the next tick's call reuses
    that bundle instead of re-deriving its folds, name probes and normalized
    title. Live, that fold was 4.1-4.7 s per tick for ~1,000 events
    (``tower.prepare_seconds``), of a 60 s tower reserve."""
    reset_prepared_cache()
    first = prepare_open_events([_open_event_row()])
    second = prepare_open_events([_open_event_row()])
    assert prepared_cache_stats() == {"hits": 1, "misses": 1}
    # The SAME bundle object, not merely an equal one.
    assert second[0].prepared is first[0].prepared


def test_prepared_cache_misses_when_updated_at_moves() -> None:
    """P1g — ``updated_at`` is the stamp the event row itself carries, and the
    fold's last evidence-time fallback; a rewritten event folds again."""
    reset_prepared_cache()
    prepare_open_events([_open_event_row()])
    prepare_open_events(
        [_open_event_row(updated_at=datetime(2026, 9, 23, tzinfo=timezone.utc))]
    )
    assert prepared_cache_stats() == {"hits": 0, "misses": 2}


def test_prepared_cache_misses_when_latest_linked_at_moves() -> None:
    """P1g — a new member link moves the aggregate's ``latest_linked_at``
    without rewriting the event row at all, and that timestamp IS the fold's
    primary evidence time, so ``updated_at`` alone would be a stale key."""
    reset_prepared_cache()
    first = prepare_open_events([_open_event_row()])
    second = prepare_open_events(
        [
            _open_event_row(
                latest_linked_at=datetime(2026, 9, 23, 7, tzinfo=timezone.utc)
            )
        ]
    )
    assert prepared_cache_stats() == {"hits": 0, "misses": 2}
    assert second[0].prepared.when != first[0].prepared.when


@pytest.mark.parametrize(
    "column,value",
    [
        ("id", UUID("22222222-2222-2222-2222-222222222222")),
        ("title", "Sanctions ease on Iranian energy exports"),
        ("category", "diplomacy"),
        ("geo", ["ir", "il"]),
        ("time_start", datetime(2026, 9, 19, tzinfo=timezone.utc)),
        ("time_end", datetime(2026, 9, 21, 12, tzinfo=timezone.utc)),
        ("entity_names", ["Iran", "United States", "Israel"]),
    ],
)
def test_prepared_cache_misses_on_every_column_the_fold_reads(
    column: str, value: object
) -> None:
    """P1g — the key is exactly the fold's input set. Each of these columns
    changes at least one folded value (id rides the bundle; title the
    normalized title; category and geo the category terms; the two timestamps
    the evidence time; entity_names the folds, probes and direction names), so
    each on its own must force a re-fold."""
    reset_prepared_cache()
    prepare_open_events([_open_event_row()])
    prepare_open_events([_open_event_row(**{column: value})])
    assert prepared_cache_stats() == {"hits": 0, "misses": 2}


def test_prepared_cache_ignores_columns_the_fold_never_reads() -> None:
    """P1g — the converse. ``event_signature`` / ``signal_ids`` /
    ``source_ids`` / ``entity_ids`` are projected and read by the write path,
    never by the fold, so changing them is still a hit — and the caller reads
    this tick's values off ``.row`` regardless."""
    reset_prepared_cache()
    prepare_open_events([_open_event_row()])
    changed = _open_event_row(
        event_signature="evt:sig-1-rewritten",
        signal_ids=[UUID("aaaaaaaa-0000-0000-0000-000000000002")],
        source_ids=["src_a", "src_b"],
        entity_ids=[UUID("bbbbbbbb-0000-0000-0000-000000000002")],
    )
    second = prepare_open_events([changed])
    assert prepared_cache_stats() == {"hits": 1, "misses": 1}
    assert second[0].row["event_signature"] == "evt:sig-1-rewritten"
    assert second[0].row["source_ids"] == ["src_a", "src_b"]


def test_prepared_cache_hit_returns_this_ticks_row_object() -> None:
    """P1g — a hit reuses the BUNDLE, never the row it was folded from. The
    write path and the category/geo pre-filter read ``category`` / ``geo`` /
    ``id`` off ``PreparedEvent.row``, so that mapping has to be the one the
    caller just handed in."""
    reset_prepared_cache()
    prepare_open_events([_open_event_row()])
    this_tick = _open_event_row()
    second = prepare_open_events([this_tick])
    assert prepared_cache_stats()["hits"] == 1
    assert second[0].row is this_tick


def test_prepared_cache_evicts_least_recently_used_at_the_bound() -> None:
    """P1g — the cache is bounded by the caller's own ``max_open_events`` and
    evicts least-recently-used, so a process cannot grow one entry per event
    the producer has ever opened."""
    reset_prepared_cache()
    rows = [
        _open_event_row(id=UUID(int=i), event_signature=f"evt:sig-{i}")
        for i in range(1, 6)
    ]
    prepare_open_events(rows, cache_limit=3)
    assert prepared_cache_stats() == {"hits": 0, "misses": 5}
    # The three most recent survive; the two oldest were evicted.
    prepare_open_events(rows[2:], cache_limit=3)
    assert prepared_cache_stats() == {"hits": 3, "misses": 5}
    prepare_open_events(rows[:2], cache_limit=3)
    assert prepared_cache_stats() == {"hits": 3, "misses": 7}


def test_prepared_cache_refolds_the_in_tick_reattach_row() -> None:
    """P1g — the handler's reattach refresh rewrites ``entity_names``,
    ``geo`` and sometimes ``latest_linked_at`` on a LOCAL copy of the matched
    row and leaves ``updated_at`` alone (the DB row's stamp has not been
    re-read). Those three are key columns, so the refreshed row folds again
    rather than reading back the pre-reattach bundle."""
    reset_prepared_cache()
    row = _open_event_row()
    prepare_open_events([row])
    reattached = dict(row)
    reattached["entity_names"] = sorted(
        set(row["entity_names"]) | {"Islamic Revolutionary Guard Corps"}
    )
    reattached["geo"] = sorted(set(row["geo"]) | {"il"})
    refreshed = prepare_open_events([reattached])
    assert prepared_cache_stats() == {"hits": 0, "misses": 2}
    assert reattached["updated_at"] == row["updated_at"]
    assert "islamic revolutionary guard corps" in " ".join(
        n.name for n in refreshed[0].prepared.name_probes
    ).lower()


def test_prepared_cache_folds_a_settled_thousand_event_set_only_once(
    monkeypatch,
) -> None:
    """P1g — the measured shape. 1,000 open events of the live density
    (50-300 entity mentions each) are folded on the first tick and NOT
    re-folded on the second, counted at the fold itself rather than inferred
    from a clock."""
    entity_pool = [f"Entity {i}" for i in range(400)] + [
        "Iran", "United States", "Russia", "China", "Israel", "Ukraine",
        "European Union", "Saudi Arabia", "Turkey", "Hezbollah", "NATO",
    ]
    geo_pool = ["ir", "us", "ru", "cn", "il", "ua", "sa", "tr", "eu"]
    categories = ["economic_coercion", "conflict", "diplomacy", "sanctions"]
    rng = random.Random(20260924)
    base = datetime(2026, 9, 24, tzinfo=timezone.utc)
    events = [
        {
            "id": UUID(int=i + 1),
            "event_signature": f"evt:sig-{i}",
            "title": f"Existing event {i} about sanctions and coercion",
            "category": rng.choice(categories),
            "geo": [rng.choice(geo_pool)],
            "time_start": base - timedelta(days=rng.uniform(0, 30)),
            "time_end": base - timedelta(hours=rng.uniform(0, 48)),
            "updated_at": base - timedelta(hours=rng.uniform(0, 48)),
            "latest_linked_at": base - timedelta(hours=rng.uniform(0, 48)),
            "entity_names": rng.choices(entity_pool, k=rng.randint(50, 300)),
        }
        for i in range(1000)
    ]

    folds = {"calls": 0}
    real_fold = _event_matcher._fold_open_event

    def _counting_fold(row, **kwargs):
        folds["calls"] += 1
        return real_fold(row, **kwargs)

    reset_prepared_cache()
    monkeypatch.setattr(_event_matcher, "_fold_open_event", _counting_fold)
    first = prepare_open_events(events, cache_limit=5000)
    assert folds["calls"] == 1000
    second = prepare_open_events(
        [dict(row) for row in events], cache_limit=5000
    )
    assert folds["calls"] == 1000, "the settled second tick re-folded rows"
    assert prepared_cache_stats() == {"hits": 1000, "misses": 1000}
    # The same bundles, not merely equal ones.
    assert all(a.prepared is b.prepared for a, b in zip(second, first))


def test_prepared_cache_key_never_raises_on_an_unhashable_row() -> None:
    """P1g — the cache is an optimization, never a correctness dependency: a
    row carrying an unhashable value in a fold-input column folds every time
    rather than raising."""
    reset_prepared_cache()
    row = _open_event_row(entity_names=[["Iran"], ["United States"]])
    prepare_open_events([row])
    prepare_open_events([dict(row)])
    assert prepared_cache_stats() == {"hits": 0, "misses": 2}


def test_match_event_deadline_cuts_mid_walk_and_reports_best_so_far(
    monkeypatch,
) -> None:
    """P1d cooperative deadline: the walk yields the best match found BEFORE
    the deadline passed, and reports the cut — this is the mechanism that
    makes the wall real when the compare itself is the cost, not an await."""
    from legba.data.analysts.deterministic_handlers import event_clustering as ec

    members = (_ev(title="Strike hits Fordow", entity_names=("Fordow", "Iran")),)
    candidate = cluster_candidate(EventCluster(members=members, oversized=False))
    matching_row = {
        "id": uuid4(),
        "event_signature": "evt:unrelated-signature",
        "title": "Strike hits Fordow",
        "category": "conflict",
        "entity_names": ["Fordow", "Iran"],
        "geo": ["ir"],
        "latest_linked_at": datetime.now(timezone.utc),
    }
    filler_rows = [
        {
            "id": uuid4(),
            "event_signature": f"evt:filler-{i}",
            "title": "Something else entirely",
            "category": "trade",
            "entity_names": ["Unrelated Entity"],
            "geo": ["us"],
            "latest_linked_at": datetime.now(timezone.utc),
        }
        for i in range(50)
    ]
    events = [matching_row] + filler_rows

    # Check the deadline every row so the fake clock's second call (the
    # second event) reads as already-past.
    monkeypatch.setattr(ec, "_DEADLINE_CHECK_EVERY", 1)
    clock = iter([0.0, 100.0] + [100.0] * 100)
    monkeypatch.setattr(ec.time, "monotonic", lambda: next(clock))

    outcome = ec._match_event(
        candidate, events, threshold=0.5, deadline_monotonic=50.0,
    )
    assert outcome.cut is True
    assert outcome.matched is not None
    assert outcome.matched["id"] == matching_row["id"]


def test_p1c_knobs_are_declared_in_the_catalog() -> None:
    """The three P1c knobs are operator-settable through the X-1 catalog."""
    from legba.data.analysts.handler_options import HANDLER_OPTIONS

    specs = {s.name: s for s in HANDLER_OPTIONS["event_clustering"]}
    for name in (
        "pair_block_window_hours",
        "tower_budget_share",
        "tower_candidate_max_seconds",
    ):
        assert name in specs, name
        assert specs[name].kind == "float"
        assert specs[name].minimum == 0.0
    assert specs["tower_budget_share"].maximum == 1.0
    # A negative window is refused rather than silently clamped at the gate.
    assert specs["pair_block_window_hours"].validate(-1.0)[0] is False
    assert specs["pair_block_window_hours"].validate(6.0)[0] is True


def test_max_tower_members_knob_is_declared_in_the_catalog() -> None:
    """P1d's member cap is operator-settable through the X-1 catalog."""
    from legba.data.analysts.handler_options import HANDLER_OPTIONS

    specs = {s.name: s for s in HANDLER_OPTIONS["event_clustering"]}
    assert "max_tower_members" in specs
    spec = specs["max_tower_members"]
    assert spec.kind == "int"
    assert spec.minimum == 1
    assert spec.validate(0)[0] is False
    assert spec.validate(20)[0] is True


def test_version_marker_is_the_p1c_stamp() -> None:
    """The deploy marker the roll greps for."""
    assert EVENT_CLUSTERING_VERSION == "2026-09/p1c"


async def _tower_finding(conn, *, prefix: str = "tower") -> UUID:
    """One verify-passed finding over two fresh signals — a tower candidate."""
    entity = await _entity(conn, f"P1c {prefix} {uuid4().hex[:8]}")
    signals = []
    for tag in ("a", "b"):
        sid = await _signal(
            conn, title=f"{prefix} strike hits Fordow",
            source_id=f"rss_{prefix}_{tag}",
        )
        await _link_signal_entity(conn, sid, entity)
        signals.append(sid)
    finding_id = await conn.fetchval(
        """
        INSERT INTO analyst_outputs
            (kind, title, confidence, data, derived_from, schema_uri,
             analyst_id, analyst_version)
        VALUES ('finding', $3, 0.9, $1::jsonb, $2::uuid[],
                'iglu:legba/finding/jsonschema/1-0-0',
                'finding_writer', 'test')
        RETURNING id
        """,
        json.dumps({"category": "conflict"}),
        signals,
        f"{prefix} strike hits Fordow",
    )
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (kind, title, data, schema_uri, analyst_id, analyst_version)
        VALUES ('critique', 'Faithfulness verify — pass', $1::jsonb,
                'iglu:legba/critique/jsonschema/1-0-0', 'verifier', 'test')
        """,
        json.dumps({"analyzed_output_id": str(finding_id),
                    "overall_score": 0.9}),
    )
    return finding_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifecycle_runs_even_when_the_slice_is_cut(
    pg_pool, monkeypatch
) -> None:
    """P1c's reason for existing: with a budget too small to cluster anything,
    the transitions — the product — still land."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    opts = _options(analyst_id="analyst.p1c_lifecycle_first")
    async with pg_pool.acquire() as conn:
        entity = await _entity(conn, f"P1c Fordow {uuid4().hex[:8]}")
        for i in range(5):
            sid = await _signal(
                conn, title="Strike hits Fordow", source_id=f"rss_{i}",
                credibility=0.8,
            )
            await _link_signal_entity(conn, sid, entity)
    # Tick one opens the event on a full budget.
    await event_clustering.handle(
        [], opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    event_id = await pg_pool.fetchval(
        "SELECT id FROM events WHERE analyst_id=$1", opts["analyst_id"]
    )
    assert event_id is not None
    # Fresh signals so tick two has a slice to be cut out of.
    async with pg_pool.acquire() as conn:
        for i in range(5, 8):
            sid = await _signal(
                conn, title="Strike hits Fordow", source_id=f"rss_{i}",
                credibility=0.8,
            )
            await _link_signal_entity(conn, sid, entity)

    # Tick two spends its budget the moment clustering starts. Probes: one per
    # lifecycle row (the single open event), the slice's semantic gate, then
    # the pre-cluster check that spends it. Pre-P1c the same budget stopped
    # the pass before lifecycle and wrote no transition at all.
    budgets = iter([_FiniteBudget(2)])
    monkeypatch.setattr(
        event_clustering, "PassBudget", lambda seconds=None: next(budgets)
    )
    result = await event_clustering.handle(
        [], _options(analyst_id=opts["analyst_id"]),
        SimpleNamespace(pg_pool=pg_pool, extras={}),
    )
    data = result.finding.data
    assert data["budget_exceeded"] is True
    assert data["stopped_in_phase"] == "cluster"
    assert data["phase_seconds"]["lifecycle"] > 0
    assert data["funnel"]["transitions_by_kind"] == {"advanced": 1}
    assert await pg_pool.fetchval(
        "SELECT lifecycle_state FROM events WHERE id=$1", event_id
    ) == "developing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tower_candidate_over_the_wall_is_skipped_and_counted(
    pg_pool, monkeypatch
) -> None:
    """A tower candidate that runs past ``tower_candidate_max_seconds`` is
    abandoned, counted, and named on the receipt — the pass keeps its turn."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    async with pg_pool.acquire() as conn:
        finding_id = await _tower_finding(conn)

    # Stall ONLY the tower producer's writes: the same two signals also form a
    # cluster candidate, and the cluster write loop is not behind the wall.
    # The stall is a REAL in-flight query on the pass's own connection, so
    # this also proves the wall's cancellation leaves that connection usable —
    # the cursor writes below run on it after the cut.
    real_write = event_clustering._write_candidate

    async def _stall_the_tower(conn, candidate, *, ctx, **kwargs):
        if ctx.analyst_id == tower_producer_id():
            await conn.execute("SELECT pg_sleep(30)")
            raise AssertionError("unreachable")
        return await real_write(conn, candidate, ctx=ctx, **kwargs)

    monkeypatch.setattr(event_clustering, "_write_candidate", _stall_the_tower)
    started = time.monotonic()
    result = await event_clustering.handle(
        [],
        _options(
            analyst_id="analyst.p1c_wall",
            include_tower=True,
            tower_candidate_max_seconds=0.25,
        ),
        SimpleNamespace(pg_pool=pg_pool, extras={}),
    )
    elapsed = time.monotonic() - started
    data = result.finding.data
    assert data["funnel"]["tower_candidates_over_wall"] == 1
    assert data["tower"]["candidates_over_wall"] == [str(finding_id)]
    assert data["tower"]["candidate_max_seconds"] == 0.25
    assert len(data["tower"]["per_candidate_seconds"]) == 1
    assert data["funnel"]["tower_promoted"] == 0
    # The wall, not the 120 s pass budget or the 30 s query, released the turn.
    assert elapsed < 20.0
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM events WHERE analyst_id='tower_backfill'"
    ) == 0
    # The pass kept writing on the connection whose query the wall cancelled.
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM alert_trigger_watermarks"
        " WHERE trigger_class='event_clustering'"
    ) > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tower_member_cap_still_links_every_signal_on_the_write(
    pg_pool, monkeypatch
) -> None:
    """P1d: ``max_tower_members`` bounds the COMPARE, never the provenance —
    a 5-signal finding capped to 2 comparison members still lands all 5
    ``signal_event_links`` rows on the written event, and the funnel counts
    the candidate as capped."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    async with pg_pool.acquire() as conn:
        entity = await _entity(conn, f"P1d cap {uuid4().hex[:8]}")
        signals = []
        for i in range(5):
            sid = await _signal(
                conn, title="P1d cap strike hits Fordow",
                source_id=f"rss_cap_{i}",
            )
            await _link_signal_entity(conn, sid, entity)
            signals.append(sid)
        finding_id = await conn.fetchval(
            """
            INSERT INTO analyst_outputs
                (kind, title, confidence, data, derived_from, schema_uri,
                 analyst_id, analyst_version)
            VALUES ('finding', 'P1d cap strike hits Fordow', 0.9,
                    $1::jsonb, $2::uuid[],
                    'iglu:legba/finding/jsonschema/1-0-0',
                    'finding_writer', 'test')
            RETURNING id
            """,
            json.dumps({"category": "conflict"}),
            signals,
        )
        await conn.execute(
            """
            INSERT INTO analyst_outputs
                (kind, title, data, schema_uri, analyst_id, analyst_version)
            VALUES ('critique', 'Faithfulness verify — pass', $1::jsonb,
                    'iglu:legba/critique/jsonschema/1-0-0', 'verifier', 'test')
            """,
            json.dumps({"analyzed_output_id": str(finding_id),
                        "overall_score": 0.9}),
        )
    opts = _options(
        analyst_id="analyst.p1d_cap",
        include_tower=True,
        tower_window_days=30,
        tower_floor=0.5,
        max_tower_members=2,
    )
    result = await event_clustering.handle(
        [], opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    data = result.finding.data
    assert data["funnel"]["tower_candidates_capped"] == 1
    # P1e receipt: the one-time fold's size and cost ride the tower block —
    # zero here (no pre-existing tower event before this tick's own fetch),
    # but present and typed, not a stub the write path never touches.
    assert data["tower"]["events_prepared"] == 0
    assert isinstance(data["tower"]["prepare_seconds"], float)
    assert data["tower"]["prepare_seconds"] >= 0.0
    event_id = await pg_pool.fetchval(
        "SELECT id FROM events WHERE analyst_id='tower_backfill'"
    )
    assert event_id is not None
    linked = await pg_pool.fetchval(
        "SELECT count(*) FROM signal_event_links WHERE event_id=$1", event_id
    )
    assert linked == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tower_prepare_receipt_counts_the_actual_open_event_fold(
    pg_pool, monkeypatch
) -> None:
    """P1e receipt: ``tower.events_prepared`` is the SIZE of the fold this
    tick actually ran, read back through the finding data — zero on a first
    tick with no prior tower event to fold, and the real prior count on a
    later tick that has one, proving the number tracks
    ``prepare_open_events``'s own output rather than a candidate count or a
    constant."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    async with pg_pool.acquire() as conn:
        await _tower_finding(conn, prefix="p1e-a")
    first = await event_clustering.handle(
        [],
        _options(include_tower=True, tower_window_days=30, tower_floor=0.5),
        SimpleNamespace(pg_pool=pg_pool, extras={}),
    )
    first_data = first.finding.data
    assert first_data["tower"]["ran"] is True
    assert first_data["tower"]["events_prepared"] == 0
    event_id = await pg_pool.fetchval(
        "SELECT id FROM events WHERE analyst_id='tower_backfill'"
    )
    assert event_id is not None

    async with pg_pool.acquire() as conn:
        await _tower_finding(conn, prefix="p1e-b")
    second = await event_clustering.handle(
        [],
        _options(include_tower=True, tower_window_days=30, tower_floor=0.5),
        SimpleNamespace(pg_pool=pg_pool, extras={}),
    )
    second_data = second.finding.data
    assert second_data["tower"]["ran"] is True
    # This tick's fold sees the event the FIRST tick actually wrote.
    assert second_data["tower"]["events_prepared"] >= 1
    assert second_data["tower"]["prepare_seconds"] >= 0.0
    # P1g receipt: the cross-tick cache's own accounting rides the same block
    # and PARTITIONS the fold — every event handed to ``prepare_open_events``
    # was either a hit or a fold, on both ticks.
    for data in (first_data, second_data):
        hits = data["tower"]["prepare_cache_hits"]
        misses = data["tower"]["prepare_cache_misses"]
        assert isinstance(hits, int) and isinstance(misses, int)
        assert hits + misses == data["tower"]["events_prepared"]
    # The event this tick folded was minted by the first tick, so it had never
    # been through the cache before: a miss, not a hit.
    assert second_data["tower"]["prepare_cache_misses"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batched_resolution_matches_the_per_candidate_path(
    pg_pool,
) -> None:
    """The page-batched resolver returns each candidate exactly the members
    the one-at-a-time path returns, in the same order."""
    async with pg_pool.acquire() as conn:
        first = await _tower_finding(conn, prefix="batcha")
        second = await _tower_finding(conn, prefix="batchb")
        lineages = {("finding", first): [first], ("finding", second): [second]}
        batched = await resolve_candidate_signals_batch(conn, lineages)
        one_at_a_time = {
            key: await _resolve_candidate_signals(conn, lineage)
            for key, lineage in lineages.items()
        }

    assert set(batched) == set(one_at_a_time)
    for key, members in batched.items():
        assert members == one_at_a_time[key], key
        assert len(members) == 2
    # The two candidates own different signals — the batch attributes each
    # expansion back to the candidate that asked, it does not pool them.
    assert not (
        {m.id for m in batched[("finding", first)]}
        & {m.id for m in batched[("finding", second)]}
    )


async def _seed_one_cluster(pg_pool, stem: str) -> None:
    """Two same-story signals from two sources sharing one entity, an hour
    old — enough for the front phases to run their budget probes."""
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    async with pg_pool.acquire() as conn:
        ent = await _entity(conn, f"{stem} {uuid4().hex[:8]}")
        s1 = await _signal(
            conn, title=f"{stem} depot blast", source_id="rss_r1",
            fetched_at=base, signal_id=uuid4(),
        )
        s2 = await _signal(
            conn, title=f"{stem} depot blast", source_id="rss_r2",
            fetched_at=base + timedelta(minutes=1), signal_id=uuid4(),
        )
        for sid in (s1, s2):
            await _link_signal_entity(conn, sid, ent)


class _ReserveBudget:
    """A ``PassBudget`` stand-in with the RESERVE arithmetic: ``remaining`` is
    a fixed number of seconds that never runs out. A front check that holds
    the tower's reserve back (``remaining <= reserve``) cuts; the tower's own
    checks (reserve 0, ``exhausted``/``allows``) do not."""

    def __init__(self, seconds_left: float) -> None:
        self.remaining = seconds_left

    def exhausted(self) -> bool:
        return self.remaining <= 0.0

    def allows(self, cost_seconds: float) -> bool:
        return self.remaining > cost_seconds


@pytest.mark.integration
async def test_a_front_cut_at_the_reserve_still_runs_the_tower_leg(pg_pool, monkeypatch):
    """P1c review (2026-09-24): three live ticks were cut in write exactly at
    the reserve boundary and the tower leg ran 0.0 s every time — the gate
    asked "did nothing stop?" where it had to ask "is my reserve still
    there?". 25 s left against a 30 s reserve (120 s x 0.25): the first
    reserve-aware front check cuts, the tower still runs, and the receipt
    names the FRONT phase that was cut and says the tower ran uncut."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    monkeypatch.delenv(event_clustering.EVENT_CLUSTERING_PASS_BUDGET, raising=False)
    await _seed_one_cluster(pg_pool, "P1c reserve")
    budgets = iter([_ReserveBudget(25.0)])
    monkeypatch.setattr(
        event_clustering, "PassBudget", lambda seconds=None: next(budgets)
    )
    opts = _options(
        analyst_id="analyst.p1c_reserve", include_tower=True,
        max_tower_candidates=1,
    )
    out = await event_clustering.handle(
        [], opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    data = out.finding.data
    assert data["budget_exceeded"] is True
    assert data["stopped_in_phase"] in ("cluster", "match", "write")
    assert data["tower"]["ran"] is True
    assert data["tower"]["cut"] is False
    assert "tower" in data["phase_seconds"]
    assert data["tower"]["budget_share"] == pytest.approx(0.25)
    # P1e — the tower ran, so the fold ran too, once, and its cost rides
    # the same receipt block (see test_tower_prepare_receipt_counts_the_
    # actual_open_event_fold for a non-zero events_prepared).
    assert data["tower"]["events_prepared"] == 0
    assert data["tower"]["prepare_seconds"] >= 0.0


@pytest.mark.integration
async def test_a_spent_budget_still_skips_the_tower_leg(pg_pool, monkeypatch):
    """The other side of the same gate: when the WHOLE budget is gone (not a
    front cut at the reserve), the tower does not start — the pre-review
    behaviour for a spent pass is kept exactly."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    monkeypatch.delenv(event_clustering.EVENT_CLUSTERING_PASS_BUDGET, raising=False)
    await _seed_one_cluster(pg_pool, "P1c spent")
    budgets = iter([_ReserveBudget(0.0)])
    monkeypatch.setattr(
        event_clustering, "PassBudget", lambda seconds=None: next(budgets)
    )
    opts = _options(analyst_id="analyst.p1c_spent", include_tower=True)
    out = await event_clustering.handle(
        [], opts, SimpleNamespace(pg_pool=pg_pool, extras={})
    )
    data = out.finding.data
    assert data["budget_exceeded"] is True
    assert data["tower"]["ran"] is False
    assert data["phase_seconds"]["tower"] == 0.0
    # P1e — the fold never ran either; the receipt says so rather than
    # carrying a stale count from a phase that never started.
    assert data["tower"]["events_prepared"] == 0
    assert data["tower"]["prepare_seconds"] == 0.0
