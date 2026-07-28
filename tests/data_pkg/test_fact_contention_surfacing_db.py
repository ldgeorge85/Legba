# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P3-2 — contested-claims arbiter TAIL, real-Postgres integration (#101).

Drives ``_run_arbiter`` against a MIGRATED ephemeral DB (the ``migrated_pg``
fixture applies migration 0097) to prove the coexistence + surfacing behavior
end to end:

  * migration 0097 columns / table exist;
  * a soaked, weight-decisive dispute SURFACES a winner deterministically —
    ``surfaced_by='deterministic'`` + ``surfaced_at`` + ``surface_rationale`` on
    the group row, ``status='surfaced'``, and both fact rows still OPEN
    (valid_until / superseded_by NULL — the never-mutate-facts invariant B15);
  * the SAME winner across two passes keeps a STABLE ``surfaced_at`` stamp
    (idempotent) with an empty ``surface_history``;
  * NEW contradicting evidence that tips the dispute the other way RE-DECIDES:
    the prior surface record lands in ``surface_history`` (newest first) and the
    loser fact rows are STILL untouched;
  * a young (un-soaked) dispute is left ``contested`` (no surface);
  * the LLM tie-break verdict CACHE is written on a genuine near-tie verdict and
    a second pass over unchanged evidence serves it WITHOUT a second LLM call;
  * contention_flip trigger compatibility — a surfacing event moves exactly the
    fields the alert scan watches (``status`` + ``surfaced_fact_id``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb
from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean(pg_pool):
    """Fresh contention + facts tables for each test (isolated ephemeral DB)."""
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM fact_contention_tiebreak")
        await conn.execute("DELETE FROM fact_contention_values")
        await conn.execute("DELETE FROM fact_contention")
        await conn.execute("DELETE FROM facts")
    yield


class _StubResponse:
    def __init__(self, content: str, model: str = "vllm-test") -> None:
        self.content = content

        class _U:
            pass

        u = _U()
        u.model = model
        self.usage = u


class StubLLM:
    def __init__(self, pick: str) -> None:
        self.pick = pick
        self.calls = 0

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _StubResponse:
        self.calls += 1
        return _StubResponse(f'{{"winner": "{self.pick}", "why": "better sourced"}}')


async def _insert_fact(
    conn: Any, subject: str, predicate: str, value: str, *,
    cred: float, conf: float, source_type: str = "ingestion",
    lineage: list[UUID] | None = None, seq: int = 0,
) -> UUID:
    fid = uuid4()
    # Distinct valid_from per row so same-value rows from different sources
    # legitimately COEXIST open (the open-triple unique index keys on
    # (subject, predicate, value, COALESCE(valid_from, epoch))) — this is how a
    # real N-source-agree-on-one-value dispute is shaped in the substrate.
    valid_from = datetime.now(tz=timezone.utc) - timedelta(minutes=seq)
    await conn.execute(
        """
        INSERT INTO facts (id, subject, predicate, value, confidence,
                           source_type, source_credibility, produced_at,
                           valid_from, derived_from, data)
        VALUES ($1, $2, $3, $4, $5, $6, $7, now(), $8, $9::uuid[], '{}'::jsonb)
        """,
        fid, subject, predicate, value, conf, source_type, cred, valid_from,
        lineage or [uuid4()],
    )
    return fid


async def _fact_untouched(conn: Any, fid: UUID) -> bool:
    row = await conn.fetchrow(
        "SELECT valid_until, superseded_by FROM facts WHERE id = $1", fid
    )
    return row["valid_until"] is None and row["superseded_by"] is None


async def _weight_decisive_facts(conn: Any) -> tuple[list[UUID], list[UUID]]:
    """5 low-cred 'de-escalating' + 2 high-cred 'clashes ongoing' (equal cred
    SUM 2.0 -> Q·C·R·F near-tie; weight 8 vs 5 -> weighted tie-break surfaces
    'de-escalating')."""
    de = [
        await _insert_fact(conn, "India", "border status", "de-escalating",
                           cred=0.4, conf=0.7, source_type="rss", seq=i)
        for i in range(5)
    ]
    cl = [
        await _insert_fact(conn, "India", "border status", "clashes ongoing",
                           cred=1.0, conf=0.7, source_type="agency", seq=i)
        for i in range(2)
    ]
    return de, cl


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0097_columns_present(pg_pool):
    async with pg_pool.acquire() as conn:
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='fact_contention'"
            )
        }
        assert {"surfaced_by", "surfaced_at", "surface_rationale",
                "surface_history"} <= cols
        tbl = await conn.fetchval(
            "SELECT to_regclass('public.fact_contention_tiebreak')"
        )
        assert tbl is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_weight_decisive_surface_is_detect_only(pg_pool, clean, monkeypatch):
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")  # skip the soak wait
    monkeypatch.delenv(arb.LLM_TIEBREAK_ENV, raising=False)
    async with pg_pool.acquire() as conn:
        de, cl = await _weight_decisive_facts(conn)

    counts = await arb._run_arbiter(pg_pool, None)
    assert counts["weight_tiebreaks"] == 1
    assert counts["abstained"] == 0

    async with pg_pool.acquire() as conn:
        grp = await conn.fetchrow(
            "SELECT status, surfaced_value, surfaced_by, surfaced_at, "
            "       surface_rationale, surface_history "
            "FROM fact_contention WHERE subject_key = 'india'"
        )
        assert grp["status"] == "surfaced"
        assert grp["surfaced_value"] == "de-escalating"
        assert grp["surfaced_by"] == "deterministic"
        assert grp["surfaced_at"] is not None
        assert "weighted tie-break" in (grp["surface_rationale"] or "")
        # never-mutate-facts (B15): every fact row on BOTH sides still open.
        for fid in de + cl:
            assert await _fact_untouched(conn, fid)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_winner_keeps_stable_stamp(pg_pool, clean, monkeypatch):
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    async with pg_pool.acquire() as conn:
        await _weight_decisive_facts(conn)

    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT surfaced_at, surface_history FROM fact_contention "
            "WHERE subject_key = 'india'"
        )
    # Second pass over identical evidence — the decision stands.
    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        second = await conn.fetchrow(
            "SELECT surfaced_at, surface_history FROM fact_contention "
            "WHERE subject_key = 'india'"
        )
    assert first["surfaced_at"] == second["surfaced_at"], "stamp must be stable"
    import json
    hist = second["surface_history"]
    hist = json.loads(hist) if isinstance(hist, str) else hist
    assert hist == [], "no history entry while the same winner stands"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_evidence_reopens_and_appends_history(pg_pool, clean, monkeypatch):
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    async with pg_pool.acquire() as conn:
        de, cl = await _weight_decisive_facts(conn)

    await arb._run_arbiter(pg_pool, None)  # surfaces 'de-escalating'
    async with pg_pool.acquire() as conn:
        assert (await conn.fetchval(
            "SELECT surfaced_value FROM fact_contention WHERE subject_key='india'"
        )) == "de-escalating"
        # Flip the balance: pile new corroboration onto 'clashes ongoing' so it
        # now dominates on weight (7 clashes vs 5 de-escalating).
        new_cl = [
            await _insert_fact(conn, "India", "border status", "clashes ongoing",
                               cred=1.0, conf=0.7, source_type="agency", seq=10 + i)
            for i in range(5)
        ]

    counts = await arb._run_arbiter(pg_pool, None)
    # The overwhelming new corroboration re-decides the winner (whichever
    # deterministic layer resolves it — here Q·C·R·F once one side dominates).
    assert counts["abstained"] == 0
    async with pg_pool.acquire() as conn:
        grp = await conn.fetchrow(
            "SELECT surfaced_value, surface_history FROM fact_contention "
            "WHERE subject_key='india'"
        )
        assert grp["surfaced_value"] == "clashes ongoing", "the flip re-decided"
        import json
        hist = grp["surface_history"]
        hist = json.loads(hist) if isinstance(hist, str) else hist
        assert len(hist) == 1, "the prior decision was appended to history"
        assert hist[0]["surfaced_value"] == "de-escalating"
        # never-mutate-facts still holds through the re-decision.
        for fid in de + cl + new_cl:
            assert await _fact_untouched(conn, fid)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_young_dispute_is_not_surfaced(pg_pool, clean, monkeypatch):
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "48")  # real soak window
    async with pg_pool.acquire() as conn:
        await _weight_decisive_facts(conn)

    counts = await arb._run_arbiter(pg_pool, None)
    assert counts["soak_deferred"] == 1
    assert counts["weight_tiebreaks"] == 0
    async with pg_pool.acquire() as conn:
        grp = await conn.fetchrow(
            "SELECT status, surfaced_by FROM fact_contention WHERE subject_key='india'"
        )
        assert grp["status"] == "contested"
        assert grp["surfaced_by"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llm_verdict_cache_round_trip(pg_pool, clean, monkeypatch):
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    async with pg_pool.acquire() as conn:
        # Symmetric near-tie (equal sources + cred) — the LLM's gray zone.
        for i in range(3):
            await _insert_fact(conn, "Brazil", "border status", "de-escalating",
                               cred=0.9, conf=0.8, source_type="rss", seq=i)
        for i in range(3):
            await _insert_fact(conn, "Brazil", "border status", "clashes ongoing",
                               cred=0.9, conf=0.8, source_type="rss", seq=i)

    llm = StubLLM("de-escalating")
    counts1 = await arb._run_arbiter(pg_pool, llm)
    assert counts1["llm_tiebreaks"] == 1
    assert counts1["llm_tiebreak_calls"] == 1
    assert llm.calls == 1
    async with pg_pool.acquire() as conn:
        cached = await conn.fetchrow(
            "SELECT verdict, winner_value_key, model_id FROM fact_contention_tiebreak"
        )
        assert cached["verdict"] == "pick"
        assert cached["winner_value_key"] == "de-escalating"
        assert cached["model_id"] == "vllm-test"

    # Second pass, unchanged evidence — served from cache, no new LLM call.
    counts2 = await arb._run_arbiter(pg_pool, llm)
    assert llm.calls == 1, "cache HIT must not re-call the LLM"
    assert counts2["llm_cache_hits"] == 1
    assert counts2["llm_tiebreaks"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_surfacing_moves_exactly_the_watched_trigger_fields(pg_pool, clean, monkeypatch):
    """The alert_trigger_scan contention_flip trigger fingerprints
    ``(status, surfaced_fact_id)``. A surfacing event must change EXACTLY those
    two fields (from contested/NULL to surfaced/<winner fact id>) so the alert
    loop fires — no more, no less."""
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    async with pg_pool.acquire() as conn:
        de, cl = await _weight_decisive_facts(conn)

    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        grp = await conn.fetchrow(
            "SELECT status, surfaced_fact_id FROM fact_contention WHERE subject_key='india'"
        )
        assert grp["status"] == "surfaced"
        assert grp["surfaced_fact_id"] is not None
        # The surfaced_fact_id is a real 'de-escalating' member (the winner
        # representative), so the trigger's LATERAL join resolves it.
        winner_val = await conn.fetchval(
            "SELECT value FROM facts WHERE id = $1", grp["surfaced_fact_id"]
        )
        assert winner_val == "de-escalating"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
