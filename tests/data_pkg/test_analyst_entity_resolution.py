# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``entity_resolution`` deterministic sub-handler.

Wires ``scripts/backfill_entity_graph.py`` as an ongoing analyst
(PIVOT_BUILD_PLAN §9 fast-follow). Three layers:

  * **Registration** — it must be a first-class registered deterministic
    sub-handler (in :data:`SUB_HANDLERS`), not hidden magic.
  * **Synthetic** (``deps=None``) — no substrate → a zeroed, well-formed run
    (the unit-test path the dispatcher contract guarantees). Runs everywhere.
  * **Live pivot DB** (env-gated) — the acceptance: seed signals carrying
    ``payload.entities`` into ``legba_pivot_test``, run the handler, and assert
    it stands up ``entity_profiles`` + ``signal_entity_links`` + co-occurrence
    ``proposed_edges``, stamps ``signals.entities_resolved_at`` (forward
    progress), and is idempotent on re-run. Skips cleanly when the dev rig is
    down or migration 0029 isn't present.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    run_method,
)
from legba.data.analysts.deterministic_handlers import entity_resolution
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "entity_resolution"


# ---------------------------------------------------------------------------
# Registration — must be a real registered deterministic sub-handler
# ---------------------------------------------------------------------------


def test_entity_resolution_registered():
    assert SUB in SUB_HANDLERS, "entity_resolution missing from SUB_HANDLERS"
    assert SUB in OUTPUT_KIND_BY_SUB_HANDLER
    assert SUB_HANDLERS[SUB] is entity_resolution.handle


# ---------------------------------------------------------------------------
# Synthetic path — no substrate, zeroed run, never spends tokens
# ---------------------------------------------------------------------------


async def test_synthetic_no_deps_zeroed_run():
    result = await run_method(
        [], {"sub_handler": SUB, "analyst_id": "er", "run_id": uuid4()}, None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["signals_processed"] == 0
    assert data["links_created"] == 0
    assert data["edges_upserted"] == 0
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


# ---------------------------------------------------------------------------
# Live pivot-DB acceptance (env-gated)
# ---------------------------------------------------------------------------


_PIVOT_DB = {
    "host": os.environ.get("LEGBA_PIVOT_PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("LEGBA_PIVOT_PG_PORT", "5432")),
    "user": os.environ.get("LEGBA_PIVOT_PG_USER", "legba"),
    "password": os.environ.get("LEGBA_PIVOT_PG_PASSWORD", "legba"),
    "database": os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test"),
}


@pytest.fixture
async def pivot_pool():
    """asyncpg pool against the pivot substrate DB; skip if unreachable or
    migration 0029 (entities_resolved_at + uq_proposed_edges_triple) absent."""
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(min_size=1, max_size=4, **_PIVOT_DB)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"legba_pivot_test unreachable: {exc}")
    async with pool.acquire() as conn:
        has_marker = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='signals' AND column_name='entities_resolved_at'"
        )
        has_profiles = await conn.fetchval("SELECT to_regclass('entity_profiles')")
    if not has_marker or not has_profiles:
        await pool.close()
        pytest.skip("migration 0029 (entity-resolution substrate) not present")
    yield pool
    await pool.close()


async def test_live_entity_resolution_acceptance(pivot_pool):
    """Seed 2 signals with overlapping NER mentions → entities resolved, links +
    co-occurrence edges created, signals stamped, re-run idempotent.

    Uses UNIQUE synthetic entity names (suffixed with a run nonce) so the
    acceptance is deterministic even against a shared dev DB where real-world
    names like "Brazil" already exist (and may pre-date this run without geo).
    """
    from datetime import datetime, timedelta, timezone

    from legba.runtime.deps import StandardDeps

    tenant = f"er_test_{uuid4().hex[:8]}"
    nonce = uuid4().hex[:8]
    hub = f"Hub_{nonce}"        # location, geo-bearing; co-occurs in both signals
    partner_a = f"PartnerA_{nonce}"
    partner_b = f"PartnerB_{nonce}"
    short = "x"                  # below MIN_NAME_LEN → never a profile
    sig_a, sig_b = uuid4(), uuid4()
    t0 = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    # sig_a is processed before sig_b (older fetched_at) so the geo-bearing first
    # sighting of `hub` lands its geo.
    payload_a = {
        "title": f"{hub} signs deal with {partner_a}",
        "entities": [
            {"text": hub, "class": "location"},
            {"text": partner_a, "class": "location"},
        ],
        "geo": {"lat": -15.7, "lon": -47.9, "country": "BR"},
    }
    payload_b = {
        "title": f"{hub} and {partner_b} expand trade",
        "entities": [
            {"text": hub, "class": "location"},
            {"text": partner_b, "class": "location"},
            {"text": short, "class": "entity"},  # below MIN_NAME_LEN → ignored
        ],
    }

    async with pivot_pool.acquire() as conn:
        for sid, pl, sigid, ts in [
            ("source_a", payload_a, sig_a, t0),
            ("source_b", payload_b, sig_b, t0 + timedelta(minutes=1)),
        ]:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5)""",
                sigid, sid, tenant, json.dumps(pl), ts,
            )

    deps = StandardDeps(pg_pool=pivot_pool)
    # Big batch so the sweep drains any pre-existing un-resolved backlog on a
    # shared dev DB and reaches our seed rows regardless of their fetched_at
    # position (the default 500 processes oldest-first and could stop short).
    big = {"sub_handler": SUB, "analyst_id": "entity_resolution",
           "run_id": uuid4(), "batch_limit": 1_000_000}
    try:
        result = await run_method([], big, deps)
        data = result.finding.data
        # Both seeded signals folded (other un-resolved rows may exist on the
        # shared dev DB, so assert >= the 2 we control rather than ==).
        assert data["signals_processed"] >= 2, data
        assert result.usage["prompt_tokens"] == 0

        async with pivot_pool.acquire() as conn:
            # Our 2 signals are now stamped resolved.
            unresolved = await conn.fetchval(
                "SELECT count(*) FROM signals "
                "WHERE owner_tenant=$1 AND entities_resolved_at IS NULL", tenant)
            assert unresolved == 0

            # hub / partner_a / partner_b profiles exist (deduped by name).
            for name in (hub, partner_a, partner_b):
                got = await conn.fetchval(
                    "SELECT count(*) FROM entity_profiles "
                    "WHERE lower(canonical_name)=lower($1)", name)
                assert got == 1, f"{name} profile missing/duplicated ({got})"
            # The below-min mention never became a profile (sig_b's link count
            # below also proves it was dropped).
            assert await conn.fetchval(
                "SELECT count(*) FROM entity_profiles WHERE canonical_name=$1",
                short) == 0

            # hub is a synthetic NON-country place name. Per the Wave-1b
            # entity-geo fix it must NOT inherit the mentioning signal's country
            # (BR) — that signal-location≠entity-location bleed is the Evian→India
            # bug. Absent a geocoder it stays geo-NULL (never BR).
            geo = await conn.fetchrow(
                "SELECT geo_lat, geo_country FROM entity_profiles "
                "WHERE lower(canonical_name)=lower($1)", hub)
            assert geo["geo_country"] is None, geo
            assert geo["geo_lat"] is None, geo

            # signal→entity links: 2 each (the short mention in sig_b dropped).
            la = await conn.fetchval(
                "SELECT count(*) FROM signal_entity_links WHERE signal_id=$1", sig_a)
            lb = await conn.fetchval(
                "SELECT count(*) FROM signal_entity_links WHERE signal_id=$1", sig_b)
            assert la == 2 and lb == 2, (la, lb)

            # Co-occurrence edges (case-insensitive triple): hub–partner_a and
            # hub–partner_b each exist exactly once.
            for a, b in ((hub, partner_a), (hub, partner_b)):
                e = await conn.fetchval(
                    "SELECT count(*) FROM proposed_edges "
                    "WHERE lower(source_entity)=lower($1) "
                    "AND lower(target_entity)=lower($2) "
                    "AND relationship_type='co_occurs'", *sorted((a, b)))
                assert e == 1, (a, b, e)

            link_total_before = await conn.fetchval(
                "SELECT count(*) FROM signal_entity_links "
                "WHERE signal_id = ANY($1::uuid[])", [sig_a, sig_b])

        # Re-run is idempotent: our signals are stamped, so they're skipped; no
        # new links for them.
        await run_method(
            [], {"sub_handler": SUB, "analyst_id": "entity_resolution",
                 "run_id": uuid4(), "batch_limit": 1_000_000}, deps,
        )
        async with pivot_pool.acquire() as conn:
            link_total_after = await conn.fetchval(
                "SELECT count(*) FROM signal_entity_links "
                "WHERE signal_id = ANY($1::uuid[])", [sig_a, sig_b])
        assert link_total_after == link_total_before

    finally:
        # Full cleanup — names are unique to this run, so nothing shared is hit.
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_entity_links WHERE signal_id = ANY($1::uuid[])",
                [sig_a, sig_b])
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute(
                "DELETE FROM proposed_edges WHERE source_entity = ANY($1) "
                "OR target_entity = ANY($1)", [hub, partner_a, partner_b])
            await conn.execute(
                "DELETE FROM entity_profiles WHERE canonical_name = ANY($1)",
                [hub, partner_a, partner_b])


# ---------------------------------------------------------------------------
# Composite-key false-merge fix (migration 0035) — the entity-resolution W1
# acceptance: same name + different class = two rows; cross-class geo not bled.
# ---------------------------------------------------------------------------


@pytest.fixture
async def composite_pool(pivot_pool):
    """``pivot_pool`` that additionally requires migration 0035's composite
    unique index to be present; skips cleanly otherwise (so a rig that hasn't
    re-applied migrations doesn't false-pass)."""
    async with pivot_pool.acquire() as conn:
        has_composite = await conn.fetchval(
            "SELECT 1 FROM pg_indexes "
            "WHERE indexname = 'idx_entity_profiles_name_class'"
        )
    if not has_composite:
        pytest.skip(
            "migration 0035 (entity_profiles composite key) not applied — "
            "apply it then re-run"
        )
    yield pivot_pool


async def test_georgia_country_and_state_do_not_merge(composite_pool):
    """The headline bug: "Georgia" the country and "Georgia" the US state are
    DISTINCT entities. The single-key dedup collapsed them to one node geocoded
    to Azerbaijan. The composite key (lower(name), entity_class) keeps two rows,
    and the US-state mention's geo never bleeds into the country row.
    """
    from datetime import datetime, timedelta, timezone

    from legba.runtime.deps import StandardDeps

    tenant = f"er_geo_{uuid4().hex[:8]}"
    nonce = uuid4().hex[:8]
    # Same surface name, two classes. Unique-suffixed so a shared dev DB's real
    # "Georgia" rows don't perturb the assertion.
    name = f"Georgia_{nonce}"
    sig_country, sig_state = uuid4(), uuid4()
    t0 = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    # Country mention WITHOUT geo (the country row should stay geo-less, not
    # inherit the US-state coordinates).
    payload_country = {
        "title": f"{name} signs trade pact",
        "entities": [{"text": name, "class": "country"}],
    }
    # US-state mention WITH US geo (Atlanta-ish) under class 'location'.
    payload_state = {
        "title": f"Floods hit {name} counties",
        "entities": [{"text": name, "class": "location"}],
        "geo": {"lat": 33.7, "lon": -84.4, "country": "US"},
    }

    async with composite_pool.acquire() as conn:
        for sid, pl, sigid, ts in [
            ("src_country", payload_country, sig_country, t0),
            ("src_state", payload_state, sig_state, t0 + timedelta(minutes=1)),
        ]:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5)""",
                sigid, sid, tenant, json.dumps(pl), ts,
            )

    deps = StandardDeps(pg_pool=composite_pool)
    big = {"sub_handler": SUB, "analyst_id": "entity_resolution",
           "run_id": uuid4(), "batch_limit": 1_000_000}
    try:
        await run_method([], big, deps)

        async with composite_pool.acquire() as conn:
            # TWO distinct rows for the same name, one per class.
            total = await conn.fetchval(
                "SELECT count(*) FROM entity_profiles "
                "WHERE lower(canonical_name)=lower($1)", name)
            assert total == 2, f"expected 2 Georgia rows (country+location), got {total}"

            classes = {
                r["entity_class"]
                for r in await conn.fetch(
                    "SELECT entity_class FROM entity_profiles "
                    "WHERE lower(canonical_name)=lower($1)", name)
            }
            assert classes == {"country", "location"}, classes

            # Entity-geo now resolves by NAME. The name carries "Georgia" (a
            # real country → ISO 'GE'), so BOTH rows take country='GE' from the
            # name — NEVER the LOCATION mention's US signal-geo. The Wave-1b fix
            # drops that mismatched US coordinate set (signal-location ≠
            # entity-location), which is exactly what stops the Georgia→
            # Azerbaijan cross-bleed: there is no foreign coordinate left to
            # bleed. Coords are NULL (no gazetteer wired in this unit run).
            for cls in ("country", "location"):
                row = await conn.fetchrow(
                    "SELECT geo_lat, geo_lon, geo_country FROM entity_profiles "
                    "WHERE lower(canonical_name)=lower($1) AND entity_class=$2",
                    name, cls)
                assert row["geo_lat"] is None, (cls, row)
                # Name-derived country (GE), never the US signal country.
                assert row["geo_country"] == "GE", (cls, row)
    finally:
        async with composite_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_entity_links WHERE signal_id = ANY($1::uuid[])",
                [sig_country, sig_state])
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute(
                "DELETE FROM entity_profiles WHERE lower(canonical_name)=lower($1)",
                name)


async def test_any_class_prelookup_reuses_and_upgrades_upward_not_downward(
    composite_pool,
):
    """DQ P4: the any-class PRE-LOOKUP reuses the highest-priority existing row
    for a name typed differently across articles, promoting the row's class UP
    the priority ladder (entity -> person) but NEVER down (organization stays
    organization when a later 'person' mention of the same name arrives)."""
    from datetime import datetime, timezone

    from legba.runtime.deps import StandardDeps

    tenant = f"er_p4_{uuid4().hex[:8]}"
    nonce = uuid4().hex[:8]
    up_name = f"Acmewidget_{nonce}"     # pre-seeded as entity, mention as person
    down_name = f"Globex_{nonce}"       # pre-seeded as organization, mention as person
    t0 = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    async with composite_pool.acquire() as conn:
        up_id = await conn.fetchval(
            "INSERT INTO entity_profiles (canonical_name, entity_type, entity_class, "
            "data, completeness_score) VALUES ($1,'entity','entity','{}'::jsonb,0.5) "
            "RETURNING id",
            up_name,
        )
        down_id = await conn.fetchval(
            "INSERT INTO entity_profiles (canonical_name, entity_type, entity_class, "
            "data, completeness_score) VALUES ($1,'organization','organization','{}'::jsonb,0.5) "
            "RETURNING id",
            down_name,
        )
        sig = uuid4()
        await conn.execute(
            "INSERT INTO signals (id, source_id, owner_tenant, modality, payload, fetched_at) "
            "VALUES ($1,'src',$2,'text',$3::jsonb,$4)",
            sig, tenant,
            json.dumps({
                "title": f"{up_name} and {down_name} in the news",
                # Both mentions typed 'person' by NER — the pre-lookup decides.
                "entities": [
                    {"text": up_name, "class": "person"},
                    {"text": down_name, "class": "person"},
                ],
            }),
            t0,
        )

    deps = StandardDeps(pg_pool=composite_pool)
    big = {"sub_handler": SUB, "analyst_id": "entity_resolution",
           "run_id": uuid4(), "batch_limit": 1_000_000}
    try:
        await run_method([], big, deps)
        async with composite_pool.acquire() as conn:
            # UPGRADE upward: entity -> person, SAME row reused (no new row).
            up_rows = await conn.fetch(
                "SELECT id, entity_class, entity_type FROM entity_profiles "
                "WHERE lower(canonical_name)=lower($1)", up_name)
            assert len(up_rows) == 1, f"upgrade forked a row: {up_rows}"
            assert str(up_rows[0]["id"]) == str(up_id), "did not reuse the existing id"
            assert up_rows[0]["entity_class"] == "person"
            assert up_rows[0]["entity_type"] == "person"  # kept in lockstep

            # NO downgrade: organization stays organization, SAME row reused.
            down_rows = await conn.fetch(
                "SELECT id, entity_class FROM entity_profiles "
                "WHERE lower(canonical_name)=lower($1)", down_name)
            assert len(down_rows) == 1, f"downgrade forked a row: {down_rows}"
            assert str(down_rows[0]["id"]) == str(down_id)
            assert down_rows[0]["entity_class"] == "organization", "class was downgraded"

            # Both mentions linked onto the reused rows.
            n_links = await conn.fetchval(
                "SELECT count(*) FROM signal_entity_links WHERE signal_id=$1", sig)
            assert n_links == 2, n_links
    finally:
        async with composite_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_entity_links WHERE entity_id = ANY($1::uuid[])",
                [up_id, down_id])
            await conn.execute("DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute(
                "DELETE FROM entity_profiles WHERE id = ANY($1::uuid[])",
                [up_id, down_id])


async def test_canonicalization_merges_fragments_with_derived_from(composite_pool):
    """Phase C acceptance: two fragmented surface forms of the SAME entity
    ({US-variant-1} as a *person*, {US-variant-2} as a *country*) converge to
    ONE ``("United States", "country")`` profile, the mistype is corrected (the
    'person' fragment is NOT a separate row), the ORIGINAL surface forms are
    recorded into ``derived_from`` + ``data.merged_aliases`` (merge provenance),
    and an ``entity_profile_versions`` row is written (the dead table is alive).

    Runs against the live pivot DB only (env-gated via ``composite_pool``);
    cleans up after itself. Uses the real "United States" canonical row, so the
    cleanup is scoped to that exact (name, class) AND only rows this test seeded
    are deleted by re-pointing through the signals it created.
    """
    from datetime import datetime, timedelta, timezone

    from legba.data.analysts.deterministic_handlers.entity_resolution import (
        _alias_marker,
    )
    from legba.runtime.deps import StandardDeps

    tenant = f"er_canon_{uuid4().hex[:8]}"
    sig_a, sig_b = uuid4(), uuid4()
    t0 = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    # Two DISTINCT raw surface forms of the United States, each MIS-classed,
    # in two signals. Canonicalization must fold both to one country row.
    payload_a = {
        "title": "USA raises tariffs",
        "entities": [{"text": "USA", "class": "person"}],     # mistyped person
    }
    payload_b = {
        "title": "The U.S. responds",
        "entities": [{"text": "U.S.", "class": "location"}],  # mistyped location
    }

    async with composite_pool.acquire() as conn:
        for sid, pl, sigid, ts in [
            ("src_us_a", payload_a, sig_a, t0),
            ("src_us_b", payload_b, sig_b, t0 + timedelta(minutes=1)),
        ]:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5)""",
                sigid, sid, tenant, json.dumps(pl), ts,
            )

    deps = StandardDeps(pg_pool=composite_pool)
    big = {"sub_handler": SUB, "analyst_id": "entity_resolution",
           "analyst_version": "phaseC", "run_id": uuid4(),
           "batch_limit": 1_000_000}
    try:
        await run_method([], big, deps)

        async with composite_pool.acquire() as conn:
            # Both signals link to the SAME single profile (the merge).
            rows = await conn.fetch(
                """SELECT DISTINCT l.entity_id
                     FROM signal_entity_links l
                    WHERE l.signal_id = ANY($1::uuid[])""",
                [sig_a, sig_b],
            )
            assert len(rows) == 1, f"fragments did not converge: {rows}"
            entity_id = rows[0]["entity_id"]

            prof = await conn.fetchrow(
                "SELECT canonical_name, entity_class, derived_from, data, version "
                "FROM entity_profiles WHERE id = $1", entity_id)
            # Canonical name + corrected class (country, never person/location).
            assert prof["canonical_name"] == "United States", prof["canonical_name"]
            assert prof["entity_class"] == "country", prof["entity_class"]

            # derived_from carries the content-addressed markers for BOTH raw
            # surface forms (provenance) — and the readable forms in data.
            df = set(prof["derived_from"])
            assert _alias_marker("USA") in df, df
            assert _alias_marker("U.S.") in df, df
            data = prof["data"]
            if isinstance(data, str):
                data = json.loads(data)
            merged = set(data.get("merged_aliases") or [])
            assert {"USA", "U.S."} <= merged, merged

            # entity_profile_versions has at least one row for this entity
            # (the table was previously dead/0-row).
            nver = await conn.fetchval(
                "SELECT count(*) FROM entity_profile_versions WHERE entity_id = $1",
                entity_id)
            assert nver >= 1, nver
            events = {
                r["e"] for r in await conn.fetch(
                    "SELECT data->>'event' AS e FROM entity_profile_versions "
                    "WHERE entity_id = $1", entity_id)
            }
            assert "alias_folded" in events or "created" in events, events

            ver_count_before = nver

        # Re-run is idempotent: the signals are stamped resolved, so no new
        # version rows / no derived_from re-growth.
        await run_method(
            [], {"sub_handler": SUB, "analyst_id": "entity_resolution",
                 "analyst_version": "phaseC", "run_id": uuid4(),
                 "batch_limit": 1_000_000}, deps,
        )
        async with composite_pool.acquire() as conn:
            ver_count_after = await conn.fetchval(
                "SELECT count(*) FROM entity_profile_versions WHERE entity_id = $1",
                entity_id)
        assert ver_count_after == ver_count_before, (
            ver_count_before, ver_count_after)
    finally:
        async with composite_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_entity_links WHERE signal_id = ANY($1::uuid[])",
                [sig_a, sig_b])
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute(
                "DELETE FROM entity_profile_versions WHERE entity_id = $1",
                entity_id)
            # Only delete the United States profile if THIS test created it
            # (no other signal links remain). On a shared dev DB a pre-existing
            # row may legitimately survive — leave it.
            remaining = await conn.fetchval(
                "SELECT count(*) FROM signal_entity_links WHERE entity_id = $1",
                entity_id)
            if remaining == 0:
                await conn.execute(
                    "DELETE FROM entity_profiles WHERE id = $1", entity_id)


async def test_same_class_geo_not_cross_country_inherited(composite_pool):
    """Within ONE class, an incoming geo whose COUNTRY disagrees with the stored
    one is not inherited — the ON-CONFLICT COALESCE is country-gated. A stub
    geocoder gives the entity its (correct, JO) geo by NAME on the first
    sighting; a later mention carrying a stray IR geo must NOT overwrite it.
    """
    from datetime import datetime, timedelta, timezone

    from legba.runtime.deps import StandardDeps

    tenant = f"er_jor_{uuid4().hex[:8]}"
    nonce = uuid4().hex[:8]
    name = f"Jordan_{nonce}"
    sig1, sig2 = uuid4(), uuid4()
    t0 = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    # First sighting: Jordan (location) WITH a JO signal geo.
    payload1 = {
        "title": f"{name} announces budget",
        "entities": [{"text": name, "class": "location"}],
        "geo": {"lat": 31.9, "lon": 35.9, "country": "JO"},
    }
    # Second sighting: same name+class but a stray IR-country geo (the kind of
    # disambiguation error that mislocated entities pre-fix).
    payload2 = {
        "title": f"{name} mentioned near the border",
        "entities": [{"text": name, "class": "location"}],
        "geo": {"lat": 35.7, "lon": 51.4, "country": "IR"},
    }

    async with composite_pool.acquire() as conn:
        for sid, pl, sigid, ts in [
            ("src_j1", payload1, sig1, t0),
            ("src_j2", payload2, sig2, t0 + timedelta(minutes=1)),
        ]:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5)""",
                sigid, sid, tenant, json.dumps(pl), ts,
            )

    # Stub geocoder: the entity geocodes by NAME to JO (its real location) on
    # BOTH sightings, so the stray IR signal-geo never even reaches the row —
    # the entity-name resolution is itself the defence (a strictly stronger
    # guarantee than the SQL ON-CONFLICT guard, which the 0035 schema test
    # covers directly). The stored geo stays JO.
    class _Res:
        lat, lon, country = 31.9, 35.9, "JO"

    class _StubGeocoder:
        async def geocode(self, query):
            return _Res()

    deps = StandardDeps(
        pg_pool=composite_pool, extras={"geocoder": _StubGeocoder()}
    )
    big = {"sub_handler": SUB, "analyst_id": "entity_resolution",
           "run_id": uuid4(), "batch_limit": 1_000_000}
    try:
        await run_method([], big, deps)

        async with composite_pool.acquire() as conn:
            # One row (same name + same class folds), retaining the FIRST
            # (correct, JO) geo — the disagreeing IR geo was rejected.
            rows = await conn.fetch(
                "SELECT geo_country, geo_lat FROM entity_profiles "
                "WHERE lower(canonical_name)=lower($1)", name)
            assert len(rows) == 1, rows
            assert rows[0]["geo_country"] == "JO", rows[0]
            assert abs(float(rows[0]["geo_lat"]) - 31.9) < 0.01, rows[0]
    finally:
        async with composite_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_entity_links WHERE signal_id = ANY($1::uuid[])",
                [sig1, sig2])
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute(
                "DELETE FROM entity_profiles WHERE lower(canonical_name)=lower($1)",
                name)
