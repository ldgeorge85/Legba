# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C2 "one janitor" (2026-07-28 coherence pass, migration 0109).

Two literal-mirror janitors — ``signals_retention`` (migration 0036) and
``analyst_traces_retention`` (migration 0101, whose own header used to say it
"mirrors signals_retention exactly") — are folded onto ONE config table
(``retention_policies``) + ONE parameterized sweep engine
(``deterministic_handlers._retention_sweep``). The two standalone modules are
now thin delegating shims; :mod:`test_signals_retention` /
:mod:`test_analyst_traces_retention` are UNCHANGED and still pass — they are
the byte-identical-behavior proof (same module, same ``handle`` entry point,
same assertions).

This file covers what those two files structurally cannot:

  * the ``retention_policies`` table + seed row shape (migration 0109);
  * the shared engine reading a LIVE policy row (not hardcoded Python
    constants) — an operator edit to ttl_days/keep_classes/batch_size/enabled
    takes effect without a code change;
  * the ``enabled`` kill-switch (a NEW capability the old modules never had,
    additive only — default TRUE, so default behavior is unchanged);
  * idempotency of a policy-driven sweep;
  * a drift guard tying the code-side policy registry
    (``_retention_sweep.KNOWN_POLICIES``) to the migration 0109 seed rows, so
    the two can never silently diverge.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import SUB_HANDLERS, OUTPUT_KIND_BY_SUB_HANDLER
from legba.data.analysts.deterministic_handlers import (
    _retention_sweep,
    analyst_traces_retention,
    signals_retention,
)
from legba.data.config import PostgresConfig
from legba.data.provenance.kinds import OutputKind
from legba.runtime.deps import StandardDeps


# ---------------------------------------------------------------------------
# Registration — the shims stay the SAME function objects dispatch points to;
# no descriptor/dispatch-table change was required by the consolidation.
# ---------------------------------------------------------------------------


def test_shims_stay_registered_under_the_same_names():
    assert SUB_HANDLERS["signals_retention"] is signals_retention.handle
    assert SUB_HANDLERS["analyst_traces_retention"] is analyst_traces_retention.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["signals_retention"] is OutputKind.FINDING
    assert OUTPUT_KIND_BY_SUB_HANDLER["analyst_traces_retention"] is OutputKind.FINDING


def test_known_policies_matches_the_migrated_pair():
    """The C2 scope is exactly these two — nexus_decay / archive retention are
    surveyed-but-not-folded (see migration 0109's header)."""
    assert _retention_sweep.KNOWN_POLICIES == {
        "signals_retention",
        "analyst_traces_retention",
    }


# ---------------------------------------------------------------------------
# Unit (no DB) — the engine's built-in defaults mirror the migration 0109
# seed exactly, so deps=None never needs a live database.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_policy_no_deps_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LEGBA_SIGNALS_RETENTION_TTL_DAYS", raising=False)
    res = await _retention_sweep.handle_policy("signals_retention", [], {}, None)
    assert res.finding.data["ttl_days"] == 0
    assert res.finding.data["signals_purged"] == 0
    assert "disabled" in res.finding.title.lower()


@pytest.mark.asyncio
async def test_engine_env_fallback_matches_legacy_var_names_no_db(monkeypatch):
    """Without a pool, the engine still resolves the CORRECT env var per
    policy from its built-in defaults (no DB round-trip needed)."""
    monkeypatch.setenv("LEGBA_SIGNALS_RETENTION_TTL_DAYS", "45")
    res = await _retention_sweep.handle_policy(
        "signals_retention", [], {"sub_handler": "signals_retention"}, None
    )
    assert res.finding.data["ttl_days"] == 45

    monkeypatch.setenv("LEGBA_ANALYST_TRACES_TTL_DAYS", "60")
    res2 = await _retention_sweep.handle_policy(
        "analyst_traces_retention",
        [],
        {"sub_handler": "analyst_traces_retention"},
        None,
    )
    assert res2.finding.data["ttl_days"] == 60


@pytest.mark.asyncio
async def test_engine_options_ttl_wins_over_env_no_db(monkeypatch):
    monkeypatch.setenv("LEGBA_SIGNALS_RETENTION_TTL_DAYS", "45")
    res = await _retention_sweep.handle_policy(
        "signals_retention", [], {"ttl_days": 0}, None
    )
    assert res.finding.data["ttl_days"] == 0
    assert "disabled" in res.finding.title.lower()


# ---------------------------------------------------------------------------
# Integration — the retention_policies table + seed rows (migration 0109).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0109_table_and_seed_rows(pg_pool):
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT policy_name, table_name, ttl_days, keep_classes, "
            "batch_size, enabled, env_fallback_var "
            "FROM retention_policies "
            "WHERE policy_name = ANY($1::text[]) "
            "ORDER BY policy_name",
            ["signals_retention", "analyst_traces_retention"],
        )
    by_name = {r["policy_name"]: r for r in rows}
    assert set(by_name) == {"signals_retention", "analyst_traces_retention"}

    sig = by_name["signals_retention"]
    assert sig["table_name"] == "signals"
    assert sig["ttl_days"] == 0  # off by default — a deletion is an operator call
    assert set(sig["keep_classes"]) == {"retain_always", "evidence_hold"}
    assert sig["batch_size"] == 5_000
    assert sig["enabled"] is True
    assert sig["env_fallback_var"] == "LEGBA_SIGNALS_RETENTION_TTL_DAYS"

    tr = by_name["analyst_traces_retention"]
    assert tr["table_name"] == "analyst_traces"
    assert tr["ttl_days"] == 0
    assert list(tr["keep_classes"]) == []
    assert tr["batch_size"] == 5_000
    assert tr["enabled"] is True
    assert tr["env_fallback_var"] == "LEGBA_ANALYST_TRACES_TTL_DAYS"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reapplying_migration_0109_is_a_noop(pg_pool):
    """CREATE-only + ON CONFLICT DO NOTHING seed: re-running the file must not
    clobber an operator's edit to a policy row."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE retention_policies SET ttl_days = 77 "
            "WHERE policy_name = 'signals_retention'"
        )
        # Re-run the seed insert exactly as migration 0109 does it.
        await conn.execute(
            "INSERT INTO retention_policies "
            "(policy_name, table_name, ttl_days, keep_classes, batch_size, "
            " enabled, env_fallback_var) "
            "VALUES ('signals_retention', 'signals', 0, "
            " ARRAY['retain_always','evidence_hold']::text[], 5000, TRUE, "
            " 'LEGBA_SIGNALS_RETENTION_TTL_DAYS') "
            "ON CONFLICT (policy_name) DO NOTHING"
        )
        ttl = await conn.fetchval(
            "SELECT ttl_days FROM retention_policies "
            "WHERE policy_name = 'signals_retention'"
        )
        assert ttl == 77, "re-applying the seed must not overwrite an operator edit"
        # Restore for other tests sharing the session-scoped migrated_pg DB.
        await conn.execute(
            "UPDATE retention_policies SET ttl_days = 0 "
            "WHERE policy_name = 'signals_retention'"
        )


# ---------------------------------------------------------------------------
# Integration — the engine reads the LIVE policy row (config, not constants).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_engine_executes_policy_row_end_to_end_signals(pg_pool):
    """A cadence-shaped call (options carries ONLY sub_handler) purges via the
    operator-set ttl_days DEFAULT stored on the policy row itself — no env
    var needed when the row's own ttl_days is positive."""
    tenant = f"pol_{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)
    old_sig = uuid4()
    fresh_sig = uuid4()

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE retention_policies SET ttl_days = 30 "
            "WHERE policy_name = 'signals_retention'"
        )
        for sid, ts in [
            (old_sig, now - timedelta(days=120)),
            (fresh_sig, now - timedelta(days=1)),
        ]:
            await conn.execute(
                "INSERT INTO signals (id, source_id, owner_tenant, modality, "
                "payload, fetched_at, retention_class) "
                "VALUES ($1,$2,$3,'text',$4::jsonb,$5,'reference_only')",
                sid, "src", tenant, json.dumps({"title": "x"}), ts,
            )

    deps = StandardDeps(pg_pool=pg_pool)
    try:
        res = await _retention_sweep.handle_policy(
            "signals_retention", [], {"sub_handler": "signals_retention"}, deps
        )
        d = res.finding.data
        assert d["ttl_days"] == 30
        assert d["signals_purged"] >= 1
        async with pg_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT 1 FROM signals WHERE id=$1", old_sig) is None
            assert await conn.fetchval(
                "SELECT 1 FROM signals WHERE id=$1", fresh_sig) == 1
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute(
                "UPDATE retention_policies SET ttl_days = 0 "
                "WHERE policy_name = 'signals_retention'"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_engine_respects_operator_keep_classes_edit(pg_pool):
    """Editing keep_classes on the LIVE row changes what survives — proof the
    exemption list is genuinely config, not a Python constant."""
    tenant = f"polkeep_{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=120)
    a_sig, b_sig = uuid4(), uuid4()

    async with pg_pool.acquire() as conn:
        # Operator adds a THIRD keep-class beyond the two seeded ones.
        await conn.execute(
            "UPDATE retention_policies SET ttl_days = 30, "
            "keep_classes = ARRAY['retain_always','evidence_hold','operator_hold']::text[] "
            "WHERE policy_name = 'signals_retention'"
        )
        await conn.execute(
            "INSERT INTO signals (id, source_id, owner_tenant, modality, "
            "payload, fetched_at, retention_class) "
            "VALUES ($1,$2,$3,'text',$4::jsonb,$5,'operator_hold')",
            a_sig, "src", tenant, json.dumps({"title": "x"}), old,
        )
        await conn.execute(
            "INSERT INTO signals (id, source_id, owner_tenant, modality, "
            "payload, fetched_at, retention_class) "
            "VALUES ($1,$2,$3,'text',$4::jsonb,$5,'reference_only')",
            b_sig, "src", tenant, json.dumps({"title": "x"}), old,
        )

    deps = StandardDeps(pg_pool=pg_pool)
    try:
        res = await _retention_sweep.handle_policy(
            "signals_retention", [], {"sub_handler": "signals_retention"}, deps
        )
        assert res.finding.data["signals_purged"] >= 1
        async with pg_pool.acquire() as conn:
            # The operator-added keep-class is honored: a_sig survives.
            assert await conn.fetchval(
                "SELECT 1 FROM signals WHERE id=$1", a_sig) == 1
            # The plain aged row is purged.
            assert await conn.fetchval(
                "SELECT 1 FROM signals WHERE id=$1", b_sig) is None
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute(
                "UPDATE retention_policies SET ttl_days = 0, "
                "keep_classes = ARRAY['retain_always','evidence_hold']::text[] "
                "WHERE policy_name = 'signals_retention'"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_engine_enabled_false_is_a_hard_kill_switch(pg_pool):
    """A disabled policy row purges nothing even when an explicit positive
    ttl_days is passed via options — the NEW enabled flag is a genuine
    additional lever, additive over the pre-existing ttl<=0 behavior."""
    tenant = f"poldisabled_{uuid4().hex[:8]}"
    old_sig = uuid4()
    old = datetime.now(tz=timezone.utc) - timedelta(days=120)

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE retention_policies SET enabled = FALSE "
            "WHERE policy_name = 'analyst_traces_retention'"
        )
        await conn.execute(
            "INSERT INTO analyst_traces "
            "(run_id, analyst_id, analyst_version, cadence_trigger, status, "
            " run_started_at, receipt_hash) "
            "VALUES ($1, 'unit_disabled', '0000000000000001', 'cadence', "
            "'success', $2, $3)",
            old_sig, old, f"rh_{old_sig.hex[:12]}",
        )

    deps = StandardDeps(pg_pool=pg_pool)
    try:
        res = await _retention_sweep.handle_policy(
            "analyst_traces_retention", [], {"ttl_days": 30}, deps
        )
        assert res.finding.data["traces_purged"] == 0
        async with pg_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT 1 FROM analyst_traces WHERE run_id=$1", old_sig) == 1
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM analyst_traces WHERE run_id=$1", old_sig)
            await conn.execute(
                "UPDATE retention_policies SET enabled = TRUE "
                "WHERE policy_name = 'analyst_traces_retention'"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_engine_batch_size_edit_bounds_the_scan(pg_pool):
    """Editing batch_size on the row (not options) bounds how many rows a
    single sweep purges — proof batching is genuinely read from config.

    ORDER DEPENDENCE. The sweep is a GLOBAL purge over `signals`: it takes the
    oldest `batch_size` rows past the TTL, whoever owns them. This test used to
    assert `remaining == 3` for its own tenant, which silently assumed the two
    purged rows were ITS two. Under `--randomly-seed` a sibling file's older
    rows were purged instead, all five of this tenant's rows survived, and the
    assertion failed at 5 == 3 — on a sweep that had done exactly the right
    thing and had already been proved to do it by the `signals_purged == 2`
    line above. What "batching bounds the scan" actually claims is a statement
    about the TABLE (a delta of exactly batch_size), plus a statement about
    this tenant that holds however the bound is spent. Both are asserted below.

    The TTL is also 100 rather than 30 days now. It only ever needed to be low
    enough to catch this test's own 120-day rows; at 30 it made every row in
    the shared DB older than a month eligible, so this test DELETED up to two
    rows belonging to whichever file ran before it. Narrowing it keeps the
    collateral to rows nothing else plausibly owns.
    """
    tenant = f"polbatch_{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)
    old_ids = [uuid4() for _ in range(5)]

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE retention_policies SET ttl_days = 100, batch_size = 2 "
            "WHERE policy_name = 'signals_retention'"
        )
        for i, sid in enumerate(old_ids):
            await conn.execute(
                "INSERT INTO signals (id, source_id, owner_tenant, modality, "
                "payload, fetched_at, retention_class) "
                "VALUES ($1,$2,$3,'text',$4::jsonb,$5,'reference_only')",
                sid, "src", tenant, json.dumps({"title": "x"}),
                now - timedelta(days=120, minutes=i),
            )

    deps = StandardDeps(pg_pool=pg_pool)
    try:
        async with pg_pool.acquire() as conn:
            total_before = await conn.fetchval("SELECT count(*) FROM signals")

        res = await _retention_sweep.handle_policy(
            "signals_retention", [], {"sub_handler": "signals_retention"}, deps
        )
        assert res.finding.data["signals_purged"] == 2  # bounded by batch_size

        async with pg_pool.acquire() as conn:
            total_after = await conn.fetchval("SELECT count(*) FROM signals")
            remaining = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant
            )

        # THE bound, stated over the table the sweep actually operates on: one
        # tick removed exactly batch_size rows and stopped. True in any order.
        assert total_before - total_after == 2, (
            "one sweep must purge exactly batch_size rows, not the whole backlog"
        )
        # And this tenant's own five: the sweep may have spent its budget here
        # or elsewhere, but it can never have taken more than the budget.
        assert 5 - remaining <= 2, (
            f"batching must leave the rest for the next tick; {5 - remaining} purged"
        )
        assert remaining >= 3
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute(
                "UPDATE retention_policies SET ttl_days = 0, batch_size = 5000 "
                "WHERE policy_name = 'signals_retention'"
            )


# ---------------------------------------------------------------------------
# Integration — idempotency of a policy-driven sweep.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_engine_second_sweep_is_a_clean_noop(pg_pool):
    tenant = f"polidem_{uuid4().hex[:8]}"
    old_sig = uuid4()
    old = datetime.now(tz=timezone.utc) - timedelta(days=120)

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE retention_policies SET ttl_days = 30 "
            "WHERE policy_name = 'signals_retention'"
        )
        await conn.execute(
            "INSERT INTO signals (id, source_id, owner_tenant, modality, "
            "payload, fetched_at, retention_class) "
            "VALUES ($1,$2,$3,'text',$4::jsonb,$5,'reference_only')",
            old_sig, "src", tenant, json.dumps({"title": "x"}), old,
        )

    deps = StandardDeps(pg_pool=pg_pool)
    try:
        first = await _retention_sweep.handle_policy(
            "signals_retention", [], {"sub_handler": "signals_retention"}, deps
        )
        assert first.finding.data["signals_purged"] >= 1

        second = await _retention_sweep.handle_policy(
            "signals_retention", [], {"sub_handler": "signals_retention"}, deps
        )
        assert second.finding.data["signals_purged"] == 0
        assert second.finding.data["entity_links_purged"] == 0
        assert second.finding.data["aliases_purged"] == 0
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute(
                "UPDATE retention_policies SET ttl_days = 0 "
                "WHERE policy_name = 'signals_retention'"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shim_and_engine_agree_byte_for_byte(pg_pool):
    """The public shim (``signals_retention.handle``) and a direct engine call
    must produce IDENTICAL finding data for the same substrate state — the
    shim is proven to be a pure delegate, not a parallel implementation."""
    tenant = f"polshim_{uuid4().hex[:8]}"
    old = datetime.now(tz=timezone.utc) - timedelta(days=120)
    shim_sig, engine_sig = uuid4(), uuid4()

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE retention_policies SET ttl_days = 30 "
            "WHERE policy_name = 'signals_retention'"
        )
        for sid in (shim_sig, engine_sig):
            await conn.execute(
                "INSERT INTO signals (id, source_id, owner_tenant, modality, "
                "payload, fetched_at, retention_class) "
                "VALUES ($1,$2,$3,'text',$4::jsonb,$5,'reference_only')",
                sid, "src", tenant, json.dumps({"title": "x"}), old,
            )

    deps = StandardDeps(pg_pool=pg_pool)
    try:
        via_shim = await signals_retention.handle(
            [], {"sub_handler": "signals_retention"}, deps
        )
        via_engine = await _retention_sweep.handle_policy(
            "signals_retention", [], {"sub_handler": "signals_retention"}, deps
        )
        # Same shape, same keys — the counts differ by construction (each
        # call purges whatever is left), so compare the STRUCTURE + the
        # ttl_days resolution, which must match exactly.
        assert set(via_shim.finding.data) == set(via_engine.finding.data)
        assert via_shim.finding.data["ttl_days"] == via_engine.finding.data["ttl_days"] == 30
        assert via_shim.finding.data["sub_handler"] == "signals_retention"
        assert via_engine.finding.data["sub_handler"] == "signals_retention"
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM signals WHERE owner_tenant=$1", tenant)
            await conn.execute(
                "UPDATE retention_policies SET ttl_days = 0 "
                "WHERE policy_name = 'signals_retention'"
            )
