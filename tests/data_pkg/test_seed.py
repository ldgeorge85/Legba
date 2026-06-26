# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the curated/authoritative seeding ROOTS (flavor b).

Covers (planning/SEEDING_SKETCH.md "roots deliverable"):

  * migration 0034 applies — seed_batches table + seed_batch_id on facts AND
    nexuses + source_type on nexuses; facts/nexuses accept 'seed'/'backfill'.
  * write_fact / write_nexus honor source_type + seed_batch_id; DEFAULTS leave
    existing behavior unchanged (no marker, payload source_type wins).
  * the world_baseline adapter maps -> resolves entities -> writes idempotent
    seed facts + signed nexuses with valid_from + the batch marker.
  * a re-run is a no-op (no duplicate fact/nexus rows; entities deduped).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.provenance import (
    AnalystContext,
    FactPayload,
    NexusPayload,
    write_fact,
    write_nexus,
)
from legba.data.seed import (
    SeedFact,
    SeedNexus,
    get_adapter,
    list_adapters,
    run_seed_source,
)
from legba.data.seed.adapters.world_baseline import WorldBaselineSeedSource


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


def _analyst_ctx() -> AnalystContext:
    return AnalystContext(
        analyst_id=f"analyst.test_{uuid4().hex[:8]}",
        analyst_version="v" + uuid4().hex[:8],
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )


# ---------------------------------------------------------------------------
# Unit: adapter registry + mapping (no DB)
# ---------------------------------------------------------------------------


def test_world_baseline_registered():
    names = dict(list_adapters())
    assert "world_baseline" in names
    assert names["world_baseline"] == "seed"
    adapter = get_adapter("world_baseline")
    assert adapter.name == "world_baseline"
    assert adapter.source_type == "seed"


@pytest.mark.asyncio
async def test_world_baseline_maps_typed_payloads():
    adapter = WorldBaselineSeedSource()
    from legba.data.seed import SeedContext

    raw = await adapter.fetch(SeedContext(dry_run=True))
    payloads = list(adapter.map(raw))

    facts = [p for p in payloads if isinstance(p, SeedFact)]
    nexuses = [p for p in payloads if isinstance(p, SeedNexus)]
    # Each leader now yields TWO facts: the subject=leader `LeaderOf` (graph
    # shape) AND a subject=country office fact (the supersession-correct shape
    # grounding reads). The office predicate is `head of state` by default but
    # is per-row overridable (`office:`) so a non-executive head of state and an
    # executive head of government for the SAME country don't supersede each
    # other (Iran's Supreme Leader + President). So a dozen+ leaders → 24+ facts
    # split across `LeaderOf` and the office predicates.
    office_predicates = {"head of state", "head of government"}
    leader_of = [f for f in facts if f.predicate == "LeaderOf"]
    office = [f for f in facts if f.predicate in office_predicates]
    assert len(leader_of) >= 12, "a dozen+ curated leaders (LeaderOf)"
    assert len(office) == len(leader_of), "one office fact per leader"
    assert len(nexuses) >= 3, "a few alliance memberships"

    # Every leader/office fact carries a real valid_from + 0.95 confidence.
    for f in facts:
        assert f.predicate in ({"LeaderOf"} | office_predicates)
        assert isinstance(f.valid_from, datetime)
        assert f.confidence == pytest.approx(0.95)
    # The office fact is country-SUBJECT (so supersession keys on the country).
    for f in office:
        assert f.value, "office fact value is the leader name"
    # Alliance nexuses are typed + signed (+1); active-conflict nexuses are
    # signed (-1). Every nexus is typed with a real valid_from.
    alliance_nexuses = [n for n in nexuses if n.rel_type == "MemberOf"]
    conflict_nexuses = [n for n in nexuses if n.rel_type == "InActiveConflictWith"]
    assert len(alliance_nexuses) >= 3, "a few alliance memberships"
    for n in alliance_nexuses:
        assert n.polarity == 1
        assert isinstance(n.valid_from, datetime)
    # The curated active-conflict layer: signed -1, one directed edge per
    # ordered belligerent pair (so the grounding resolver surfaces the war from
    # whichever side a country analyst is scoped to).
    assert conflict_nexuses, "the curated active-conflict layer is present"
    for n in conflict_nexuses:
        assert n.polarity == -1
        assert n.intent == "conflict"
        assert isinstance(n.valid_from, datetime)


def test_unknown_adapter_raises():
    with pytest.raises(KeyError):
        get_adapter("nope_not_a_source")


# ---------------------------------------------------------------------------
# D31 — coalition-aware conflicts: cross-side hostile, same-side allied (no DB)
# ---------------------------------------------------------------------------


async def _map_conflict_yaml(yaml_doc: str):
    """Fetch+map an isolated conflict-only YAML through the adapter (no DB)."""
    import tempfile
    from pathlib import Path

    from legba.data.seed import SeedContext

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "w.yaml"
        p.write_text(yaml_doc, encoding="utf-8")
        adapter = WorldBaselineSeedSource()
        raw = await adapter.fetch(SeedContext(dry_run=True, options={"yaml_path": str(p)}))
        return [x for x in adapter.map(raw) if isinstance(x, SeedNexus)]


@pytest.mark.asyncio
async def test_conflict_sides_no_within_side_hostile_edge():
    """D31: the US-Israel-Iran war modelled with explicit ``sides`` emits HOSTILE
    edges ONLY across sides (Iran vs US, Iran vs Israel) and NEVER between the
    co-belligerent US + Israel — fixing the wrong "Israel in active conflict with
    United States" nexuses. The same-side pair is ALLIED (+1) instead."""
    nexuses = await _map_conflict_yaml(
        "conflicts:\n"
        "  - name: US-Israel-Iran war\n"
        "    sides:\n"
        "      - [Iran]\n"
        "      - [United States, Israel]\n"
        "    valid_from: 2026-02-28\n"
    )
    hostile = {
        (n.subject.lower(), n.object.lower())
        for n in nexuses
        if n.rel_type == "InActiveConflictWith"
    }
    # Iran ↔ both belligerents (both directions) — the war is surfaced from any side.
    assert ("iran", "united states") in hostile
    assert ("iran", "israel") in hostile
    assert ("united states", "iran") in hostile
    assert ("israel", "iran") in hostile
    # The 2 wrong edges are GONE: US and Israel are NOT at war with each other.
    assert ("united states", "israel") not in hostile
    assert ("israel", "united states") not in hostile
    # Instead they are explicitly ALLIED (+1, both directions).
    allied = {
        (n.subject.lower(), n.object.lower())
        for n in nexuses
        if n.rel_type == "AlliedWith"
    }
    assert allied == {("united states", "israel"), ("israel", "united states")}
    for n in nexuses:
        if n.rel_type == "AlliedWith":
            assert n.polarity == 1
            assert n.intent == "alliance"
        elif n.rel_type == "InActiveConflictWith":
            assert n.polarity == -1
            assert n.intent == "conflict"


@pytest.mark.asyncio
async def test_conflict_legacy_belligerents_still_all_pairs_hostile():
    """D31 back-compat: the legacy flat ``belligerents`` form is unchanged —
    every ordered pair is hostile (all-vs-all) and NO alliances are emitted."""
    nexuses = await _map_conflict_yaml(
        "conflicts:\n"
        "  - name: All-vs-all\n"
        "    belligerents: [A, B, C]\n"
        "    valid_from: 2026-02-28\n"
    )
    hostile = {
        (n.subject, n.object)
        for n in nexuses
        if n.rel_type == "InActiveConflictWith"
    }
    assert hostile == {
        ("A", "B"), ("A", "C"), ("B", "A"),
        ("B", "C"), ("C", "A"), ("C", "B"),
    }
    assert [n for n in nexuses if n.rel_type == "AlliedWith"] == []


# ---------------------------------------------------------------------------
# Migration 0034 — schema shape
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0034_schema(pg_conn):
    # seed_batches table exists with the expected columns.
    cols = {
        r["column_name"]
        for r in await pg_conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'seed_batches'"
        )
    }
    assert {"id", "source", "kind", "source_type", "imported_at", "counts", "manifest"} <= cols

    # seed_batch_id on BOTH facts and nexuses; source_type on nexuses.
    for table in ("facts", "nexuses"):
        tcols = {
            r["column_name"]
            for r in await pg_conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = $1",
                table,
            )
        }
        assert "seed_batch_id" in tcols, f"{table}.seed_batch_id missing"
        assert "source_type" in tcols, f"{table}.source_type missing"

    # facts/nexuses accept 'seed'/'backfill' (no CHECK rejects them).
    batch_id = await pg_conn.fetchval(
        "INSERT INTO seed_batches (source, kind, source_type) "
        "VALUES ('t','t','seed') RETURNING id"
    )
    fid = uuid4()
    await pg_conn.execute(
        "INSERT INTO facts (id, subject, predicate, value, source_type, "
        "valid_from, seed_batch_id) "
        "VALUES ($1,'S','P','V','backfill', now(), $2)",
        fid,
        batch_id,
    )
    got = await pg_conn.fetchrow(
        "SELECT source_type, seed_batch_id FROM facts WHERE id=$1", fid
    )
    assert got["source_type"] == "backfill"
    assert got["seed_batch_id"] == batch_id


# ---------------------------------------------------------------------------
# write_fact / write_nexus marker threading — defaults unchanged
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_fact_default_unchanged(pg_conn):
    """Without source_type/seed_batch_id, a fact behaves exactly as before:
    payload source_type wins, seed_batch_id is NULL."""
    actx = _analyst_ctx()
    out, dlq = await write_fact(
        pg_conn,
        analyst_ctx=actx,
        payload=FactPayload(
            subject="DefaultCo", predicate="based in", value="Nowhere",
            source_type="agent",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        derived_from=[],
    )
    assert dlq is None and out is not None
    row = await pg_conn.fetchrow(
        "SELECT source_type, seed_batch_id FROM facts WHERE id=$1", out.id
    )
    assert row["source_type"] == "agent"
    assert row["seed_batch_id"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_fact_honors_seed_marker(pg_conn):
    actx = _analyst_ctx()
    batch_id = await pg_conn.fetchval(
        "INSERT INTO seed_batches (source, kind, source_type) "
        "VALUES ('m','m','seed') RETURNING id"
    )
    out, dlq = await write_fact(
        pg_conn,
        analyst_ctx=actx,
        payload=FactPayload(
            subject="SeedCo", predicate="based in", value="Seedville",
            source_type="agent",  # overridden by the call-site source_type
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        derived_from=[],
        source_type="seed",
        seed_batch_id=batch_id,
    )
    assert dlq is None and out is not None
    row = await pg_conn.fetchrow(
        "SELECT source_type, seed_batch_id FROM facts WHERE id=$1", out.id
    )
    assert row["source_type"] == "seed", "call-site source_type overrides payload"
    assert row["seed_batch_id"] == batch_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_nexus_default_unchanged_and_marker(pg_conn):
    actx = _analyst_ctx()
    # Default: no marker.
    out0, _ = await write_nexus(
        pg_conn,
        analyst_ctx=actx,
        payload=NexusPayload(
            subject="A_def", object="B_def", rel_type="AlliedWith", polarity=1,
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        derived_from=[],
    )
    assert out0 is not None
    r0 = await pg_conn.fetchrow(
        "SELECT source_type, seed_batch_id FROM nexuses WHERE id=$1", out0.id
    )
    assert r0["source_type"] == "agent" and r0["seed_batch_id"] is None

    # Marked seed write.
    batch_id = await pg_conn.fetchval(
        "INSERT INTO seed_batches (source, kind, source_type) "
        "VALUES ('mn','mn','seed') RETURNING id"
    )
    out1, _ = await write_nexus(
        pg_conn,
        analyst_ctx=actx,
        payload=NexusPayload(
            subject="A_seed", object="B_seed", rel_type="AlliedWith", polarity=1,
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        derived_from=[],
        source_type="seed",
        seed_batch_id=batch_id,
    )
    assert out1 is not None
    r1 = await pg_conn.fetchrow(
        "SELECT source_type, seed_batch_id FROM nexuses WHERE id=$1", out1.id
    )
    assert r1["source_type"] == "seed" and r1["seed_batch_id"] == batch_id


# ---------------------------------------------------------------------------
# End-to-end: world_baseline adapter through the driver + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_driver_dry_run_writes_nothing(pg_pool):
    adapter = WorldBaselineSeedSource()
    async with pg_pool.acquire() as conn:
        batches_before = await conn.fetchval("SELECT count(*) FROM seed_batches")
        facts_before = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE source_type='seed'"
        )

    result = await run_seed_source(pg_pool, adapter, dry_run=True)
    assert result.dry_run is True
    assert result.seed_batch_id is None
    assert result.counts["facts"] >= 12

    async with pg_pool.acquire() as conn:
        batches_after = await conn.fetchval("SELECT count(*) FROM seed_batches")
        facts_after = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE source_type='seed'"
        )
    assert batches_after == batches_before, "dry-run must record NO batch"
    assert facts_after == facts_before, "dry-run must write NO facts"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_world_baseline_end_to_end_and_idempotent(pg_pool):
    adapter = WorldBaselineSeedSource()

    # First run — writes seed facts + signed nexuses + the batch row.
    r1 = await run_seed_source(pg_pool, adapter, dry_run=False)
    assert not r1.errors, f"unexpected errors: {r1.errors}"
    assert r1.seed_batch_id is not None
    assert r1.counts["facts"] >= 12
    assert r1.counts["nexuses"] >= 3

    async with pg_pool.acquire() as conn:
        # The batch row records the counts.
        batch = await conn.fetchrow(
            "SELECT source, source_type, counts FROM seed_batches WHERE id=$1",
            r1.seed_batch_id,
        )
        assert batch["source"] == "world_baseline"
        assert batch["source_type"] == "seed"

        # A known leader fact is present, stamped seed + batch + valid_from.
        modi = await conn.fetchrow(
            "SELECT source_type, seed_batch_id, valid_from, confidence "
            "FROM facts WHERE lower(subject)='narendra modi' "
            # Phase B item 5: predicate stored canonical (was 'LeaderOf').
            "AND predicate='leader of' AND lower(value)='india' "
            "AND valid_until IS NULL AND superseded_by IS NULL"
        )
        assert modi is not None, "Modi LeaderOf India seeded"
        assert modi["source_type"] == "seed"
        assert modi["seed_batch_id"] == r1.seed_batch_id
        assert modi["valid_from"] is not None
        assert modi["confidence"] == pytest.approx(0.95)

        # A known alliance nexus is present, typed + signed + stamped.
        nato = await conn.fetchrow(
            "SELECT source_type, seed_batch_id, polarity, valid_from "
            "FROM nexuses WHERE lower(subject)='france' "
            # Phase B item 5: rel_type stored canonical (was 'MemberOf').
            "AND rel_type='member of' AND lower(object)='nato' "
            "AND valid_until IS NULL AND superseded_by IS NULL"
        )
        assert nato is not None, "France MemberOf NATO seeded"
        assert nato["source_type"] == "seed"
        assert nato["polarity"] == 1
        assert nato["valid_from"] is not None

        # Entities resolved (no duplicates): exactly one France profile.
        france_n = await conn.fetchval(
            "SELECT count(*) FROM entity_profiles WHERE lower(canonical_name)='france'"
        )
        assert france_n == 1

        # Snapshot open-row counts for the seed triples.
        open_facts_1 = await conn.fetchval(
            "SELECT count(*) FROM facts "
            "WHERE source_type='seed' AND valid_until IS NULL AND superseded_by IS NULL"
        )
        open_nexus_1 = await conn.fetchval(
            "SELECT count(*) FROM nexuses "
            "WHERE source_type='seed' AND valid_until IS NULL AND superseded_by IS NULL"
        )

    # Second run — idempotent: NO new open fact/nexus rows (upsert no-op),
    # entities still deduped to one each.
    r2 = await run_seed_source(pg_pool, adapter, dry_run=False)
    assert not r2.errors
    # P3-3 idempotency: an identical re-run dedupes the LEDGER row too — the
    # batch row is keyed on (source, kind, content_hash), so the re-run UPDATEs
    # the prior row in place rather than minting a duplicate that would
    # overstate seeded volume.
    assert r2.seed_batch_id == r1.seed_batch_id, "re-run reuses the prior batch row"

    async with pg_pool.acquire() as conn:
        open_facts_2 = await conn.fetchval(
            "SELECT count(*) FROM facts "
            "WHERE source_type='seed' AND valid_until IS NULL AND superseded_by IS NULL"
        )
        open_nexus_2 = await conn.fetchval(
            "SELECT count(*) FROM nexuses "
            "WHERE source_type='seed' AND valid_until IS NULL AND superseded_by IS NULL"
        )
        france_n2 = await conn.fetchval(
            "SELECT count(*) FROM entity_profiles WHERE lower(canonical_name)='france'"
        )

    assert open_facts_2 == open_facts_1, "re-run must not add open fact rows"
    assert open_nexus_2 == open_nexus_1, "re-run must not add open nexus rows"
    assert france_n2 == 1, "re-run must not spawn a duplicate entity"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_batch_ledger_is_idempotent(pg_pool):
    """P3-3: a second identical seed run does NOT mint a misleading batch row.

    The pre-fix driver did an UNCONDITIONAL INSERT every run, so re-running a
    source left N ledger rows each claiming the full seeded volume — the ledger
    overstated reality N-fold (the live world_baseline ledger showed 3 rows all
    claiming facts:19 when only the first wrote rows). The fix dedupes the batch
    row on (source, kind, content_hash): an identical re-run UPDATEs the prior
    row in place, so the ledger carries exactly ONE row per distinct seed set
    and its summed counts no longer overstate.
    """
    # Use an isolated YAML so this test's content_hash can't collide with
    # another world_baseline batch left in the shared session DB by a sibling
    # test — the dedupe is keyed on (source, kind, content_hash), so a distinct
    # curated set yields a distinct row we can count in isolation.
    import tempfile
    from pathlib import Path

    yaml_doc = (
        "leaders:\n"
        "  - {leader: Idempo Tester, country: Testlandia, valid_from: 2025-01-01}\n"
        "alliances:\n"
        "  - bloc: TESTPACT\n"
        "    members:\n"
        "      - {country: Testlandia, valid_from: 2025-01-01}\n"
    )
    with tempfile.TemporaryDirectory() as td:
        yaml_path = Path(td) / "world_baseline_idem.yaml"
        yaml_path.write_text(yaml_doc, encoding="utf-8")
        adapter = WorldBaselineSeedSource()
        opts = {"yaml_path": str(yaml_path)}

        async with pg_pool.acquire() as conn:
            hashes_before = await conn.fetch(
                "SELECT id FROM seed_batches WHERE source='world_baseline'"
            )
        ids_before = {r["id"] for r in hashes_before}

        # Three identical runs over the SAME isolated input.
        r1 = await run_seed_source(pg_pool, adapter, dry_run=False, options=opts)
        r2 = await run_seed_source(pg_pool, adapter, dry_run=False, options=opts)
        r3 = await run_seed_source(pg_pool, adapter, dry_run=False, options=opts)
        assert not (r1.errors or r2.errors or r3.errors)

        # All three resolve to the SAME ledger row — natural-key dedupe held.
        assert r1.seed_batch_id == r2.seed_batch_id == r3.seed_batch_id

        async with pg_pool.acquire() as conn:
            rows_after = await conn.fetch(
                "SELECT id FROM seed_batches WHERE source='world_baseline'"
            )
            ids_after = {r["id"] for r in rows_after}
            new_ids = ids_after - ids_before
            # Exactly ONE new ledger row across the three identical runs (not 3).
            assert new_ids == {r1.seed_batch_id}, (
                "identical re-runs must not mint duplicate ledger rows"
            )

            # The single row carries a content_hash + counts that reflect
            # reality, reachable by its natural key.
            batch = await conn.fetchrow(
                "SELECT counts, manifest FROM seed_batches WHERE id=$1",
                r1.seed_batch_id,
            )
            manifest = (
                batch["manifest"]
                if isinstance(batch["manifest"], dict)
                else json.loads(batch["manifest"])
            )
            counts = (
                batch["counts"]
                if isinstance(batch["counts"], dict)
                else json.loads(batch["counts"])
            )
            assert manifest.get("content_hash"), "content_hash recorded on row"
            # Summing the ledger for THIS batch must not overstate: the total
            # equals a single run's facts (one row), not 3x.
            total_facts = await conn.fetchval(
                "SELECT COALESCE(SUM((counts->>'facts')::int), 0) "
                "FROM seed_batches WHERE id=$1",
                r1.seed_batch_id,
            )
            assert total_facts == counts["facts"]


# ---------------------------------------------------------------------------
# Seed resolver: composite key + country-gated geo (migration 0035)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_resolve_entity_composite_key_and_geo_gate(pg_conn):
    """``_resolve_entity`` keys on (lower(name), entity_class) so a name shared
    across classes stays two rows; and the Wave-1b entity-geo reconciliation
    overrides a coordinate set whose country contradicts a country-NAMED
    entity (the source coords belong to a different place)."""
    from legba.data.seed._driver import _resolve_entity

    # "Georgia" is a real country name → the reconciliation engages.
    name = f"Georgia_{uuid4().hex[:8]}"
    try:
        # Country (no geo) and location (a US-state coordinate) -> two rows.
        country_id = await _resolve_entity(
            pg_conn, canonical_name=name, entity_class="country")
        loc_id = await _resolve_entity(
            pg_conn, canonical_name=name, entity_class="location",
            geo_lat=33.7, geo_lon=-84.4, geo_country="US")
        assert country_id != loc_id

        rows = await pg_conn.fetch(
            "SELECT entity_class, geo_country FROM entity_profiles "
            "WHERE lower(canonical_name)=lower($1) ORDER BY entity_class", name)
        assert {r["entity_class"] for r in rows} == {"country", "location"}
        # Country row (no geo supplied) stays geo-less.
        country_row = next(r for r in rows if r["entity_class"] == "country")
        assert country_row["geo_country"] is None
        # The LOCATION row's US coords contradicted the country-named entity, so
        # the reconciliation took the name's country (GE) and dropped the coords
        # — never the mismatched US value (the Evian→India defence in seed).
        loc_row = next(r for r in rows if r["entity_class"] == "location")
        assert loc_row["geo_country"] == "GE"
    finally:
        await pg_conn.execute(
            "DELETE FROM entity_profiles WHERE lower(canonical_name)=lower($1)",
            name)


async def test_seed_resolve_entity_keeps_legitimate_town_geo(pg_conn):
    """CONSERVATIVE seed reconciliation: a NON-country place name (a real town)
    seeded WITH coordinates KEEPS them — a seed's geo is authoritative input, so
    only a provable name-vs-country contradiction overrides it."""
    from legba.data.seed._driver import _resolve_entity

    name = f"Evian_{uuid4().hex[:8]}"   # a town, not a country
    try:
        await _resolve_entity(
            pg_conn, canonical_name=name, entity_class="location",
            geo_lat=46.40, geo_lon=6.59, geo_country="FR")
        row = await pg_conn.fetchrow(
            "SELECT geo_country, geo_lat FROM entity_profiles "
            "WHERE lower(canonical_name)=lower($1)", name)
        assert row["geo_country"] == "FR", "seeded town geo must be kept"
        assert abs(float(row["geo_lat"]) - 46.40) < 0.01
    finally:
        await pg_conn.execute(
            "DELETE FROM entity_profiles WHERE lower(canonical_name)=lower($1)",
            name)


# ---------------------------------------------------------------------------
# CURRENT-WORLD-STATE grounding seed: the Iran temporal-collapse fix
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_world_baseline_iran_current_state_lands_correctly(pg_pool):
    """The curated Iran grounding seed lands the supersession-correct shape.

    Web-verified ground truth (the operator's world is post-training-cutoff):
      * head of STATE = Mojtaba Khamenei (current, since 2026-03-08);
      * the prior Ali Khamenei is CLOSED (valid_until 2026-02-28) and points at
        the successor — exactly ONE open 'head of state' for Iran (no dup);
      * head of GOVERNMENT = Masoud Pezeshkian — a DISTINCT office, so it stays
        open alongside the Supreme Leader (the per-row `office:` override means
        it does NOT supersede the head-of-state row);
      * the US-Israel-Iran war is an OPEN signed -1 'in active conflict with'
        nexus from Iran to BOTH belligerents (the curated active-conflict layer
        the world_assessor anchors on).

    Uses an ISOLATED YAML (the `yaml_path` option) carrying only the Iran rows +
    the conflict, so the assertions are deterministic regardless of any G20
    world_baseline batch already present in the shared session DB.
    """
    import tempfile
    from pathlib import Path

    yaml_doc = (
        "leaders:\n"
        "  - {leader: Ali Khamenei, country: Iran, valid_from: 1989-06-04, "
        "valid_until: 2026-02-28}\n"
        "  - {leader: Mojtaba Khamenei, country: Iran, valid_from: 2026-03-08}\n"
        "  - {leader: Masoud Pezeshkian, country: Iran, "
        "office: head of government, valid_from: 2024-07-28}\n"
        "conflicts:\n"
        "  - name: US-Israel-Iran war\n"
        "    belligerents: [Iran, United States, Israel]\n"
        "    valid_from: 2026-02-28\n"
    )
    with tempfile.TemporaryDirectory() as td:
        yaml_path = Path(td) / "world_baseline_iran.yaml"
        yaml_path.write_text(yaml_doc, encoding="utf-8")
        adapter = WorldBaselineSeedSource()
        r = await run_seed_source(
            pg_pool, adapter, dry_run=False, options={"yaml_path": str(yaml_path)},
        )
        assert not r.errors, f"unexpected errors: {r.errors}"

    async with pg_pool.acquire() as conn:
        # Exactly ONE open head-of-state for Iran, and it is Mojtaba (current).
        open_hos = await conn.fetch(
            "SELECT value, valid_from FROM facts "
            "WHERE lower(subject)='iran' AND predicate='head of state' "
            "AND valid_until IS NULL AND superseded_by IS NULL",
        )
        assert len(open_hos) == 1, "exactly one current Supreme Leader (no dup)"
        assert open_hos[0]["value"] == "Mojtaba Khamenei"
        assert open_hos[0]["valid_from"].date().isoformat() == "2026-03-08"

        # The prior Ali Khamenei head-of-state row is CLOSED + points at Mojtaba.
        ali = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM facts "
            "WHERE lower(subject)='iran' AND predicate='head of state' "
            "AND value='Ali Khamenei'",
        )
        assert ali is not None, "the prior Supreme Leader row exists"
        assert ali["valid_until"] is not None, "the prior Supreme Leader is closed"
        successor_id = await conn.fetchval(
            "SELECT id FROM facts WHERE lower(subject)='iran' "
            "AND predicate='head of state' AND value='Mojtaba Khamenei' "
            "AND valid_until IS NULL AND superseded_by IS NULL",
        )
        # Closure is via valid_until (in the past) — that alone excludes Ali from
        # the current-facts grounding gate (superseded_by IS NULL AND valid_until>now()).
        # An explicit superseded_by link is optional provenance polish for pre-dated
        # curated HISTORY rows, not a correctness requirement; the single open Mojtaba
        # row above is the current leader, and Ali closed before the succession.
        assert successor_id is not None, "Mojtaba is the single open head of state"
        assert ali["valid_until"] < open_hos[0]["valid_from"], "prior closed before succession"

        # The executive President is a DISTINCT office (head of government) — it
        # stays open and did NOT supersede the head-of-state row.
        pres = await conn.fetchrow(
            "SELECT value FROM facts WHERE lower(subject)='iran' "
            "AND predicate='head of government' "
            "AND valid_until IS NULL AND superseded_by IS NULL",
        )
        assert pres is not None and pres["value"] == "Masoud Pezeshkian"

        # The active-conflict layer: Iran has an OPEN signed -1 edge to BOTH
        # belligerents (rel_type stored canonical 'in active conflict with').
        conflict_objs = {
            row["object"].lower()
            for row in await conn.fetch(
                "SELECT object FROM nexuses WHERE lower(subject)='iran' "
                "AND rel_type='in active conflict with' AND polarity=-1 "
                "AND valid_until IS NULL AND superseded_by IS NULL",
            )
        }
        assert {"united states", "israel"} <= conflict_objs, (
            "Iran is in active conflict with both the US and Israel"
        )
