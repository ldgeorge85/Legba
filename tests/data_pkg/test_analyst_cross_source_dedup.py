# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-09 tests for the ``cross_source_dedup`` deterministic sub-handler.

Two layers:

  * **Synthetic** (``deps=None``) — content-hash grouping + deterministic
    canonical selection over pre-shaped input rows. No substrate needed; runs
    in every CI lane.
  * **Live pivot DB** (env-gated, ``LEGBA_PIVOT_PG_DSN`` or the dev-rig default)
    — the P-09 acceptance: insert the same content via 2 ``source_id``s into the
    ``legba_pivot_test`` ``signals`` table, run the handler, and assert it links
    1 canonical + 1 alias with BOTH raw rows preserved and a ``canonical_only``
    subscription seeing exactly 1. Skips cleanly when the dev rig is down.

The dispatcher contract (registered in
:data:`legba.data.analysts.deterministic.SUB_HANDLERS`) is asserted too — P-09
requires this be a *first-class* deterministic sub-handler, not hidden magic.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    run_method,
)
from legba.data.analysts.deterministic_handlers import cross_source_dedup
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "cross_source_dedup"


# ---------------------------------------------------------------------------
# Registration — P-09 demands a real registered deterministic sub-handler
# ---------------------------------------------------------------------------


def test_cross_source_dedup_registered():
    assert SUB in SUB_HANDLERS, "cross_source_dedup missing from SUB_HANDLERS"
    assert SUB in OUTPUT_KIND_BY_SUB_HANDLER
    assert SUB_HANDLERS[SUB] is cross_source_dedup.handle


# ---------------------------------------------------------------------------
# R2 — the qdrant_collection default drift guard + qdrant_errors hardening
#
# Root cause (2026-07): this handler's ``qdrant_collection`` default was the
# literal ``"signals"``, a collection nothing ever creates — the live Qdrant
# only ever holds ``legba_signals`` (signal_embedder / QdrantConfig). The
# descriptor never overrode it, so every semantic-dedup pass queried a
# nonexistent collection, raised, and was swallowed by the best-effort
# except-log in ``_resolve_semantic_pool`` — zero ``signal_aliases`` rows with
# ``reason='semantic_qdrant'`` in all of history, with NOTHING in the receipt
# to say so.
# ---------------------------------------------------------------------------


def test_qdrant_collection_default_matches_the_shared_canonical_name():
    """Cross-module drift guard: import both the handler and the shared
    Qdrant config (the source of truth signal_embedder writes through via
    ``store.cfg.signals_collection``) and assert they name the SAME
    collection. This is the exact class of bug that shipped: a hardcoded
    literal ("signals") silently diverged from the real collection name
    ("legba_signals") and nobody noticed for the handler's entire history."""
    from legba.data.analysts.deterministic_handlers import signal_embedder  # noqa: F401 — drift guard: prove the module imports clean alongside the config it relies on
    from legba.data.config import QdrantConfig

    assert cross_source_dedup._DEFAULT_QDRANT_COLLECTION == QdrantConfig().signals_collection
    assert cross_source_dedup._DEFAULT_QDRANT_COLLECTION == "legba_signals"


async def test_semantic_pool_qdrant_error_returns_counter_not_silent():
    """A Qdrant/transport failure during the semantic pass must surface as
    ``qdrant_errors=1`` in the return tuple — not just the WARNING log line
    (which nothing consumes as a signal). Before this hardening a dead/
    misnamed collection degraded with ZERO observable trace in the receipt."""

    class _RaisingPool:
        def acquire(self):
            raise RuntimeError("qdrant/pg transport boom")

    aliases_linked, sets, qdrant_errors = await cross_source_dedup._resolve_semantic_pool(
        _RaisingPool(),
        qdrant=object(),
        threshold=0.95,
        collection="legba_signals",
        produced_by="test_dedup",
        owner_tenant=None,
    )
    assert aliases_linked == 0
    assert sets == []
    assert qdrant_errors == 1


async def test_synthetic_path_reports_zero_qdrant_errors():
    """The synthetic (deps=None) path never touches Qdrant — the receipt must
    say so honestly (``qdrant_errors=0``), not omit the key."""
    inputs = [
        {"id": str(uuid4()), "source_id": "a", "content_hash": "x", "fetched_at": 1},
    ]
    result = await run_method(inputs, {"sub_handler": SUB}, None)
    assert result.finding.data["qdrant_errors"] == 0


# ---------------------------------------------------------------------------
# Synthetic path — content-hash grouping, deterministic canonical
# ---------------------------------------------------------------------------


async def test_synthetic_links_cross_source_duplicate():
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    sig_a, sig_b, sig_c = uuid4(), uuid4(), uuid4()
    inputs = [
        # same content via 2 sources — A is earlier => canonical
        {"id": str(sig_a), "source_id": "src_A", "content_hash": "H1", "fetched_at": t0},
        {"id": str(sig_b), "source_id": "src_B", "content_hash": "H1",
         "fetched_at": t0 + timedelta(minutes=5)},
        # unique content — no link
        {"id": str(sig_c), "source_id": "src_A", "content_hash": "H2", "fetched_at": t0},
    ]
    result = await run_method(
        inputs, {"sub_handler": SUB, "analyst_id": "dedup", "run_id": uuid4()}, None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["canonical_count"] == 1
    assert data["aliases_linked"] == 1
    assert data["exact_aliases"] == 1
    # deterministic canonical = earliest fetched_at
    one_set = data["sets"][0]
    assert one_set["canonical_signal_id"] == str(sig_a)
    assert one_set["alias_signal_ids"] == [str(sig_b)]
    assert one_set["reason"] == "content_hash"
    # never spends tokens
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


async def test_synthetic_no_duplicates_links_nothing():
    inputs = [
        {"id": str(uuid4()), "source_id": "a", "content_hash": "x", "fetched_at": 1},
        {"id": str(uuid4()), "source_id": "b", "content_hash": "y", "fetched_at": 2},
    ]
    result = await run_method(inputs, {"sub_handler": SUB}, None)
    assert result.finding.data["canonical_count"] == 0
    assert result.finding.data["aliases_linked"] == 0


async def test_synthetic_ignores_empty_content_hash():
    # Empty content_hash is the pre-enrichment / raw shape — never deduped.
    inputs = [
        {"id": str(uuid4()), "source_id": "a", "content_hash": "", "fetched_at": 1},
        {"id": str(uuid4()), "source_id": "b", "content_hash": "", "fetched_at": 2},
    ]
    result = await run_method(inputs, {"sub_handler": SUB}, None)
    assert result.finding.data["aliases_linked"] == 0


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
    """asyncpg pool against the pivot substrate DB; skip if unreachable."""
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(min_size=1, max_size=4, **_PIVOT_DB)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"legba_pivot_test unreachable: {exc}")
    # Confirm the pivot substrate (not the legacy schema) is present.
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT to_regclass('signal_aliases')")
        has_canon = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='signals' AND column_name='canonical_signal_id'"
        )
    if not ok or not has_canon:
        await pool.close()
        pytest.skip("pivot substrate (signal_aliases / canonical_signal_id) not present")
    yield pool
    await pool.close()


async def test_live_pivot_acceptance(pivot_pool):
    """P-09 acceptance — same content via 2 sources => 1 canonical + 1 alias,
    both raw rows preserved, canonical_only sees 1, rerun idempotent."""
    import json

    from legba.runtime.deps import StandardDeps

    tenant = f"p09_test_{uuid4().hex[:8]}"
    produced_by = "test_dedup_p09"
    content_hash = f"p09_{uuid4().hex}"
    sig_a, sig_b = uuid4(), uuid4()
    t0 = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)

    async with pivot_pool.acquire() as conn:
        for sid, ts, payload, sigid in [
            ("source_reuters", t0, {"title": "Quake hits region"}, sig_a),
            ("source_ap", t0 + timedelta(minutes=3), {"title": "Quake hits region"}, sig_b),
        ]:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload, content_hash, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5,$6)""",
                sigid, sid, tenant, json.dumps(payload), content_hash, ts,
            )

    deps = StandardDeps(pg_pool=pivot_pool)
    try:
        result = await run_method(
            [], {"sub_handler": SUB, "analyst_id": produced_by,
                 "run_id": uuid4(), "owner_tenant": tenant}, deps,
        )
        data = result.finding.data
        assert data["canonical_count"] == 1, data
        assert data["aliases_linked"] == 1, data
        assert data["exact_aliases"] == 1, data

        async with pivot_pool.acquire() as conn:
            # BOTH raw rows survive — never destructive collapse.
            raw = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant)
            assert raw == 2

            aliases = await conn.fetch(
                "SELECT alias_signal_id, canonical_signal_id, reason, score "
                "FROM signal_aliases WHERE produced_by=$1", produced_by)
            assert len(aliases) == 1
            assert str(aliases[0]["canonical_signal_id"]) == str(sig_a)  # earliest
            assert str(aliases[0]["alias_signal_id"]) == str(sig_b)
            assert aliases[0]["reason"] == "content_hash"
            assert abs(aliases[0]["score"] - 1.0) < 1e-6

            # canonical points at itself; alias points at canonical
            ca = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", sig_a)
            cb = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", sig_b)
            assert str(ca) == str(sig_a)
            assert str(cb) == str(sig_a)

            # a canonical_only subscription sees exactly 1.
            canon_only = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1 "
                "AND (canonical_signal_id = id OR canonical_signal_id IS NULL)", tenant)
            assert canon_only == 1

        # Rerun is idempotent — links 0 new aliases, never collapses.
        rerun = await run_method(
            [], {"sub_handler": SUB, "analyst_id": produced_by,
                 "run_id": uuid4(), "owner_tenant": tenant}, deps,
        )
        assert rerun.finding.data["aliases_linked"] == 0
        async with pivot_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM signal_aliases WHERE produced_by=$1",
                produced_by) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant) == 2
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)


# ---------------------------------------------------------------------------
# Bounded + incremental per-run work (the actor-invoke timeout fix)
# ---------------------------------------------------------------------------


async def _seed_dup_groups(conn, tenant: str, n_groups: int) -> dict[str, tuple]:
    """Seed ``n_groups`` 2-row content_hash duplicate groups for ``tenant``.

    Returns ``{content_hash: (canonical_id, alias_id)}`` where the canonical is
    the earlier-fetched row (so dedupe should pick it).
    """
    import json

    t0 = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    # Per-call batch nonce so repeated calls for the same tenant never reuse a
    # content_hash (each call yields a fresh, independent set of groups).
    batch = uuid4().hex[:8]
    groups: dict[str, tuple] = {}
    for i in range(n_groups):
        ch = f"bnd_{tenant}_{batch}_{i:05d}"
        canon, alias = uuid4(), uuid4()
        for sid, ts, sigid in [
            ("source_a", t0, canon),
            ("source_b", t0 + timedelta(minutes=1), alias),
        ]:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload, content_hash, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5,$6)""",
                sigid, sid, tenant, json.dumps({"title": ch}), ch, ts,
            )
        groups[ch] = (canon, alias)
    return groups


async def _unresolved_group_count(conn, tenant: str) -> int:
    """Number of content_hash groups still holding an unresolved member."""
    return await conn.fetchval(
        """
        SELECT count(*) FROM (
            SELECT content_hash
            FROM signals
            WHERE owner_tenant = $1 AND content_hash <> ''
            GROUP BY content_hash
            HAVING COUNT(*) > 1
               AND COUNT(*) FILTER (WHERE canonical_signal_id IS NULL) > 0
        ) s
        """,
        tenant,
    )


async def test_bounded_cap_processes_only_max_groups_per_run(pivot_pool):
    """N > cap unresolved groups → one run resolves exactly ``cap`` groups and
    leaves the rest for the next run; successive idempotent runs drain the
    backlog until every group is resolved (eventual consistency)."""
    from legba.runtime.deps import StandardDeps

    tenant = f"bnd_cap_{uuid4().hex[:8]}"
    produced_by = "test_dedup_bounded"
    n_groups = 7
    cap = 3
    deps = StandardDeps(pg_pool=pivot_pool)

    try:
        async with pivot_pool.acquire() as conn:
            await _seed_dup_groups(conn, tenant, n_groups)
            assert await _unresolved_group_count(conn, tenant) == n_groups

        opts = {
            "sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
            "owner_tenant": tenant, "max_groups_per_run": cap,
        }

        # Run 1 — bounded to exactly `cap` groups.
        r1 = await run_method([], dict(opts, run_id=uuid4()), deps)
        assert r1.finding.data["canonical_count"] == cap, r1.finding.data
        assert r1.finding.data["aliases_linked"] == cap, r1.finding.data
        async with pivot_pool.acquire() as conn:
            assert await _unresolved_group_count(conn, tenant) == n_groups - cap

        # Run 2 — next `cap` groups.
        r2 = await run_method([], dict(opts, run_id=uuid4()), deps)
        assert r2.finding.data["canonical_count"] == cap, r2.finding.data
        async with pivot_pool.acquire() as conn:
            assert await _unresolved_group_count(conn, tenant) == n_groups - 2 * cap

        # Run 3 — the final group (< cap remaining).
        r3 = await run_method([], dict(opts, run_id=uuid4()), deps)
        assert r3.finding.data["canonical_count"] == n_groups - 2 * cap, r3.finding.data
        async with pivot_pool.acquire() as conn:
            assert await _unresolved_group_count(conn, tenant) == 0
            # All N groups now fully resolved: N canonicals + N aliases.
            n_aliases = await conn.fetchval(
                "SELECT count(*) FROM signal_aliases WHERE produced_by=$1", produced_by)
            assert n_aliases == n_groups
            # No raw rows lost (2 per group).
            assert await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant) == 2 * n_groups

        # Run 4 — fully drained → idempotent no-op.
        r4 = await run_method([], dict(opts, run_id=uuid4()), deps)
        assert r4.finding.data["canonical_count"] == 0
        assert r4.finding.data["aliases_linked"] == 0
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)


async def test_already_canonicalised_group_not_reprocessed(pivot_pool):
    """A fully-canonicalised group is skipped in SQL — never re-resolved — so a
    cap-sized run spends its whole budget on *unresolved* groups only."""
    from legba.runtime.deps import StandardDeps

    tenant = f"bnd_skip_{uuid4().hex[:8]}"
    produced_by = "test_dedup_skip"
    cap = 2
    deps = StandardDeps(pg_pool=pivot_pool)

    try:
        async with pivot_pool.acquire() as conn:
            groups = await _seed_dup_groups(conn, tenant, 5)

        opts = {
            "sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
            "owner_tenant": tenant, "max_groups_per_run": cap,
        }

        # Drain all 5 groups (cap=2 → 2,2,1).
        for _ in range(3):
            await run_method([], dict(opts, run_id=uuid4()), deps)
        async with pivot_pool.acquire() as conn:
            assert await _unresolved_group_count(conn, tenant) == 0
            resolved_aliases = await conn.fetchval(
                "SELECT count(*) FROM signal_aliases WHERE produced_by=$1", produced_by)
        assert resolved_aliases == 5

        # Insert ONE brand-new unresolved group. With every old group already
        # canonicalised, the next run must spend its (capped) budget resolving
        # exactly the new group — proving settled groups are skipped, not
        # re-walked.
        async with pivot_pool.acquire() as conn:
            new = await _seed_dup_groups(conn, tenant, 1)
            assert await _unresolved_group_count(conn, tenant) == 1
        new_ch, (new_canon, new_alias) = next(iter(new.items()))

        r = await run_method([], dict(opts, run_id=uuid4()), deps)
        assert r.finding.data["canonical_count"] == 1, r.finding.data
        assert r.finding.data["aliases_linked"] == 1, r.finding.data
        async with pivot_pool.acquire() as conn:
            assert await _unresolved_group_count(conn, tenant) == 0
            # The new group is correctly linked; total aliases = 5 old + 1 new.
            assert await conn.fetchval(
                "SELECT count(*) FROM signal_aliases WHERE produced_by=$1",
                produced_by) == 6
            cb = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", new_alias)
            assert str(cb) == str(new_canon)
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)


async def test_processed_group_outcome_unchanged_under_cap(pivot_pool):
    """The dedupe result for a group the bounded pass *does* process is
    identical to the old unbounded behaviour: earliest-fetched canonical, one
    alias linked, both raw rows preserved, canonical_only sees exactly one."""
    from legba.runtime.deps import StandardDeps

    tenant = f"bnd_eq_{uuid4().hex[:8]}"
    produced_by = "test_dedup_equiv"
    deps = StandardDeps(pg_pool=pivot_pool)

    try:
        async with pivot_pool.acquire() as conn:
            groups = await _seed_dup_groups(conn, tenant, 1)
        ch, (canon, alias) = next(iter(groups.items()))

        r = await run_method(
            [],
            {"sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
             "owner_tenant": tenant, "max_groups_per_run": 500},
            deps,
        )
        data = r.finding.data
        assert data["canonical_count"] == 1
        assert data["aliases_linked"] == 1
        assert data["exact_aliases"] == 1

        async with pivot_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT alias_signal_id, canonical_signal_id, reason, score "
                "FROM signal_aliases WHERE produced_by=$1", produced_by)
            assert str(row["canonical_signal_id"]) == str(canon)  # earliest fetched
            assert str(row["alias_signal_id"]) == str(alias)
            assert row["reason"] == "content_hash"
            assert abs(row["score"] - 1.0) < 1e-6

            ca = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", canon)
            cb = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", alias)
            assert str(ca) == str(canon)
            assert str(cb) == str(canon)

            assert await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant) == 2
            canon_only = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1 "
                "AND (canonical_signal_id = id OR canonical_signal_id IS NULL)", tenant)
            assert canon_only == 1
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)
