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


#: This file's subject namespace. Every fact this file inserts carries a
#: subject minted by :func:`_subject`, so its contention groups can never
#: collide with another suite's and its cleanup can never touch another
#: suite's rows.
_SUBJECT_TAG = "fcsdb"


def _subject() -> str:
    """A subject no other test (in this file or any other) can carry."""
    return f"{_SUBJECT_TAG} {uuid4().hex[:10]}"


def _skey(subject: str) -> str:
    """The arbiter's canonical subject_key for ``subject`` (whitespace-folded
    lowercase — mirrors ``_group_keys``)."""
    return " ".join(subject.split()).strip().lower()


@pytest_asyncio.fixture
async def clean(pg_pool):
    """This FILE's own contention groups + fact rows, cleared around each test.

    WHAT WAS HERE: ``DELETE FROM facts`` — an unscoped wipe of the whole
    suite's fact substrate, run before every test in this file. It destroyed
    sibling baselines mid-suite (test_seed's world-baseline ledger kept the
    batch row while the wipe took its facts, so a re-seed after the wipe
    corroborated a ghost) and, worse, it MASKED every fact-table polluter
    that ran before this file in ordered runs. The arbiter under test scans
    the WHOLE table by design (META), so this file now takes the other half
    of the bargain: unique subjects per test (the ``fcsdb`` namespace), every
    assertion scoped to the test's own group, and cleanup that deletes only
    rows this file minted. Teardown cleans as well as setup so the file's
    LAST test leaves no standing disputes for later arbiter-driving suites.

    ``fact_contention_values`` / ``_tiebreak`` cascade from the group delete
    (both carry ``ON DELETE CASCADE``)."""

    async def _clean_own(conn):
        await conn.execute(
            "DELETE FROM fact_contention WHERE subject_key LIKE $1",
            f"{_SUBJECT_TAG} %",
        )
        await conn.execute(
            "DELETE FROM facts WHERE subject LIKE $1", f"{_SUBJECT_TAG} %"
        )

    async with pg_pool.acquire() as conn:
        await _clean_own(conn)
    yield
    async with pg_pool.acquire() as conn:
        await _clean_own(conn)


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
        # The per-call prompts, so a test can count how often ITS OWN group
        # (subject_key appears in the tie-break prompt) reached the model —
        # `self.calls` alone is a substrate statement once foreign near-ties
        # can share the sweep.
        self.prompts: list[str] = []

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _StubResponse:
        self.calls += 1
        self.prompts.append(str(messages[0].get("content", "")))
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


async def _weight_decisive_facts(
    conn: Any, subject: str
) -> tuple[list[UUID], list[UUID]]:
    """5 low-cred 'de-escalating' + 2 high-cred 'clashes ongoing' (equal cred
    SUM 2.0 -> Q·C·R·F near-tie; weight 8 vs 5 -> weighted tie-break surfaces
    'de-escalating'). ``subject`` comes from :func:`_subject` so the dispute
    is this test's own group on the shared substrate."""
    de = [
        await _insert_fact(conn, subject, "border status", "de-escalating",
                           cred=0.4, conf=0.7, source_type="rss", seq=i)
        for i in range(5)
    ]
    cl = [
        await _insert_fact(conn, subject, "border status", "clashes ongoing",
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
    subject = _subject()
    async with pg_pool.acquire() as conn:
        de, cl = await _weight_decisive_facts(conn, subject)

    counts = await arb._run_arbiter(pg_pool, None)
    # The arbiter is a META sweep over the WHOLE shared table, so the run
    # counters are substrate statements (a foreign dispute left by another
    # suite is counted alongside mine). MY dispute's outcome is the row below;
    # the counter proves the deterministic layer ran at least for it.
    assert counts["weight_tiebreaks"] >= 1

    async with pg_pool.acquire() as conn:
        grp = await conn.fetchrow(
            "SELECT status, surfaced_value, surfaced_by, surfaced_at, "
            "       surface_rationale, surface_history "
            "FROM fact_contention WHERE subject_key = $1", _skey(subject)
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
    subject = _subject()
    async with pg_pool.acquire() as conn:
        await _weight_decisive_facts(conn, subject)

    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT surfaced_at, surface_history FROM fact_contention "
            "WHERE subject_key = $1", _skey(subject)
        )
    # Second pass over identical evidence — the decision stands.
    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        second = await conn.fetchrow(
            "SELECT surfaced_at, surface_history FROM fact_contention "
            "WHERE subject_key = $1", _skey(subject)
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
    subject = _subject()
    async with pg_pool.acquire() as conn:
        de, cl = await _weight_decisive_facts(conn, subject)

    await arb._run_arbiter(pg_pool, None)  # surfaces 'de-escalating'
    async with pg_pool.acquire() as conn:
        assert (await conn.fetchval(
            "SELECT surfaced_value FROM fact_contention WHERE subject_key=$1",
            _skey(subject),
        )) == "de-escalating"
        # Flip the balance: pile new corroboration onto 'clashes ongoing' so it
        # now dominates on weight (7 clashes vs 5 de-escalating).
        new_cl = [
            await _insert_fact(conn, subject, "border status", "clashes ongoing",
                               cred=1.0, conf=0.7, source_type="agency", seq=10 + i)
            for i in range(5)
        ]

    await arb._run_arbiter(pg_pool, None)
    # The overwhelming new corroboration re-decides the winner (whichever
    # deterministic layer resolves it — here Q·C·R·F once one side dominates).
    # The old `counts["abstained"] == 0` was a statement about every dispute in
    # the shared table; the re-decision on MY group below is the real claim.
    async with pg_pool.acquire() as conn:
        grp = await conn.fetchrow(
            "SELECT surfaced_value, surface_history FROM fact_contention "
            "WHERE subject_key=$1", _skey(subject)
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
    subject = _subject()
    async with pg_pool.acquire() as conn:
        await _weight_decisive_facts(conn, subject)

    counts = await arb._run_arbiter(pg_pool, None)
    # At least MY freshly-opened group deferred; the proof it was MINE (and
    # that no tie-break touched it) is the group row itself, below.
    assert counts["soak_deferred"] >= 1
    async with pg_pool.acquire() as conn:
        grp = await conn.fetchrow(
            "SELECT status, surfaced_by FROM fact_contention WHERE subject_key=$1",
            _skey(subject),
        )
        assert grp["status"] == "contested"
        assert grp["surfaced_by"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llm_verdict_cache_round_trip(pg_pool, clean, monkeypatch):
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    subject = _subject()
    async with pg_pool.acquire() as conn:
        # Symmetric near-tie (equal sources + cred) — the LLM's gray zone.
        for i in range(3):
            await _insert_fact(conn, subject, "border status", "de-escalating",
                               cred=0.9, conf=0.8, source_type="rss", seq=i)
        for i in range(3):
            await _insert_fact(conn, subject, "border status", "clashes ongoing",
                               cred=0.9, conf=0.8, source_type="rss", seq=i)

    llm = StubLLM("de-escalating")
    counts1 = await arb._run_arbiter(pg_pool, llm)
    assert counts1["llm_tiebreaks"] >= 1
    assert counts1["llm_tiebreak_calls"] >= 1
    # MY group reached the model exactly once (the tie-break prompt carries the
    # subject_key). A foreign near-tie left on the shared substrate may add
    # calls of its own — those are its business, not this cache's.
    assert sum(_skey(subject) in p for p in llm.prompts) == 1
    async with pg_pool.acquire() as conn:
        cached = await conn.fetchrow(
            "SELECT t.verdict, t.winner_value_key, t.model_id "
            "  FROM fact_contention_tiebreak t "
            "  JOIN fact_contention c ON c.id = t.contention_id "
            " WHERE c.subject_key = $1", _skey(subject)
        )
        assert cached["verdict"] == "pick"
        assert cached["winner_value_key"] == "de-escalating"
        assert cached["model_id"] == "vllm-test"

    # Second pass, unchanged evidence — MY verdict is served from cache: the
    # subject's prompt count must not grow.
    counts2 = await arb._run_arbiter(pg_pool, llm)
    assert sum(_skey(subject) in p for p in llm.prompts) == 1, (
        "cache HIT must not re-call the LLM for this group"
    )
    assert counts2["llm_cache_hits"] >= 1
    assert counts2["llm_tiebreaks"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_surfacing_moves_exactly_the_watched_trigger_fields(pg_pool, clean, monkeypatch):
    """The alert_trigger_scan contention_flip trigger fingerprints
    ``(status, surfaced_fact_id)``. A surfacing event must change EXACTLY those
    two fields (from contested/NULL to surfaced/<winner fact id>) so the alert
    loop fires — no more, no less."""
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    subject = _subject()
    async with pg_pool.acquire() as conn:
        de, cl = await _weight_decisive_facts(conn, subject)

    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        grp = await conn.fetchrow(
            "SELECT status, surfaced_fact_id FROM fact_contention WHERE subject_key=$1",
            _skey(subject),
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
