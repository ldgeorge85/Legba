# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The arbiter pass must not hold its actor's turn — measured, then pinned.

WHAT THIS EXISTS TO STOP COMING BACK (live read, 2026-09-20)
------------------------------------------------------------
``fact_contention_arbiter`` recomputed all 22 208 contention groups from the
open facts every hour and re-wrote every one of them, whether or not anything
about them had moved. One live pass: 107 s of Python clustering, 247 s of
per-group earned-track-record queries, ~339 000 write round trips. Over the 24
passes in the log window the turn was held a median of 651 s (min 443, max 994)
against a 180 s actor invoke timeout. Dapr actors are turn-based with
reentrancy disabled, so the reconciler's ``ENSURE_ACTIVE`` heal blew its 20 s
deadline 124 times in 24 h, the heal breaker opened 144 times, and all 24
cadence fires landed on *actor is closed*. Activation measures ~17 ms — it was
never slow, it was queued behind the run.

The tests here pin the three properties the fix rests on:

  * an unchanged group is skipped WHOLE — no clustering, no tie-break, and in
    particular ZERO writes, including no ``facts.updated_at`` bump;
  * changed evidence (or a changed tunable) invalidates the fingerprint, so a
    stored answer can never coast past evidence that moved — which is the
    recompute-from-open-rows contract the skip must not weaken;
  * the per-pass wall-clock budget truncates the pass and, crucially, SUPPRESSES
    the stale-collapse sweep, because a truncated pass's live-key set is partial
    and collapsing against it would tear down live groups.

The ``facts.updated_at`` assertion is not a performance detail. The
unconditional hourly restamp meant a contested fact's ``updated_at`` was always
minutes old, and ``fact_decay`` selects staleness on ``updated_at < now() - 30
days`` — so the decay sweep had been silently excluded from ~78 % of the open
corpus (103 192 of 131 255 open rows measured). Re-introducing the unconditional
stamp would re-break decay without breaking anything decay-shaped, so the guard
is pinned here, next to its cause.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb
from legba.data.analysts.deterministic_handlers import fact_contention_pass as fcp
from legba.data.config import PostgresConfig

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

#: This file's own subject namespace — the arbiter is a META analyst that scans
#: the WHOLE facts table, so every assertion here is scoped to rows this file
#: minted and cleanup touches nothing else (the discipline
#: test_fact_contention_surfacing_db.py established after an unscoped
#: ``DELETE FROM facts`` destroyed sibling baselines mid-suite).
_SUBJECT_TAG = "fcpb"


def _subject() -> str:
    return f"{_SUBJECT_TAG} {uuid4().hex[:10]}"


def _skey(subject: str) -> str:
    return " ".join(subject.split()).strip().lower()


# ===========================================================================
# Pure unit — the fingerprint and the budget, no database
# ===========================================================================


def _raw(value: str, *, conf: float = 0.8, cred: float | None = 0.9,
         fid: UUID | None = None, age_days: float = 1.0,
         lineage: list[UUID] | None = None) -> dict[str, Any]:
    """One raw scanned fact row, shaped as ``_open_triples`` returns it."""
    return {
        "id": fid or uuid4(),
        "subject": "atlantis",
        "predicate": "located in",
        "value": value,
        "confidence": conf,
        "source_type": "rss",
        "source_credibility": cred,
        "produced_at": NOW - timedelta(days=age_days),
        "derived_from": list(lineage or [uuid4()]),
    }


def _fp(rows: list[dict[str, Any]], *, now: datetime = NOW,
        past_soak: bool = True, tunables: str = "t0") -> str:
    return fcp.group_fingerprint(
        rows, tunables=tunables, arbiter_version=arb.ARBITER_VERSION,
        now=now, past_soak=past_soak,
    )


def test_fingerprint_is_stable_and_order_insensitive():
    rows = [_raw("the sea"), _raw("the sky")]
    assert _fp(rows) == _fp(rows)
    assert _fp(rows) == _fp(list(reversed(rows))), (
        "the scan's ORDER BY is stable today; the fingerprint must not rely on it"
    )


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda r: r.__setitem__("value", "the deep"), id="value"),
    pytest.param(lambda r: r.__setitem__("confidence", 0.1), id="confidence"),
    pytest.param(lambda r: r.__setitem__("source_credibility", 0.1), id="credibility"),
    pytest.param(lambda r: r.__setitem__("source_type", "agent"), id="source_type"),
    pytest.param(lambda r: r.__setitem__("produced_at", NOW - timedelta(days=9)),
                 id="produced_at"),
    pytest.param(lambda r: r.__setitem__("derived_from", [uuid4(), uuid4()]),
                 id="lineage"),
])
def test_every_scored_input_moves_the_fingerprint(mutate):
    """Each field feeds Q, C, R or F. A change that the score can see must be a
    change the fingerprint can see, or a stale answer would stand."""
    rows = [_raw("the sea"), _raw("the sky")]
    before = _fp(rows)
    mutate(rows[0])
    assert _fp(rows) != before


def test_a_new_member_row_moves_the_fingerprint():
    rows = [_raw("the sea"), _raw("the sky")]
    before = _fp(rows)
    rows.append(_raw("the sea"))
    assert _fp(rows) != before


def test_tunable_change_and_soak_crossing_move_the_fingerprint():
    rows = [_raw("the sea"), _raw("the sky")]
    assert _fp(rows, tunables="t1") != _fp(rows, tunables="t0")
    assert _fp(rows, past_soak=False) != _fp(rows, past_soak=True)


def test_age_bucket_forces_one_recompute_per_refresh_window(monkeypatch):
    """Recency decays; the absolute MIN_SURFACE_SCORE floor is the one
    comparison wall-clock alone can flip. The bucket is how that crossing gets
    noticed within a day."""
    monkeypatch.setenv(fcp.REFRESH_HOURS_ENV, "24")
    rows = [_raw("the sea", age_days=1.0), _raw("the sky", age_days=1.0)]
    same_day = _fp(rows, now=NOW + timedelta(hours=11))
    assert same_day == _fp(rows, now=NOW)
    assert _fp(rows, now=NOW + timedelta(hours=25)) != same_day


def test_refresh_hours_zero_disables_the_skip(monkeypatch):
    """The operator escape hatch: 0 restores every-group-every-pass."""
    monkeypatch.setenv(fcp.REFRESH_HOURS_ENV, "0")
    assert fcp.skip_enabled() is False
    prior = {"input_fingerprint": "abc", "arbiter_version": arb.ARBITER_VERSION,
             "status": "contested"}
    assert fcp.unchanged(prior, "abc", arbiter_version=arb.ARBITER_VERSION) is False


@pytest.mark.parametrize("prior,fingerprint,why", [
    (None, "abc", "no stored row at all"),
    ({"input_fingerprint": None, "arbiter_version": arb.ARBITER_VERSION,
      "status": "contested"}, "abc", "never fingerprinted (pre-migration row)"),
    ({"input_fingerprint": "zzz", "arbiter_version": arb.ARBITER_VERSION,
      "status": "contested"}, "abc", "fingerprint differs"),
    ({"input_fingerprint": "abc", "arbiter_version": "other/9.9.9",
      "status": "contested"}, "abc", "a different arbiter build decided it"),
    ({"input_fingerprint": "abc", "arbiter_version": arb.ARBITER_VERSION,
      "status": "collapsed"}, "abc", "collapsed groups must be re-examined"),
    ({"status": "contested"}, "abc", "degraded row missing the columns"),
])
def test_unchanged_refuses_every_reason_to_doubt_the_stored_answer(
    prior, fingerprint, why, monkeypatch,
):
    monkeypatch.delenv(fcp.REFRESH_HOURS_ENV, raising=False)
    assert fcp.unchanged(
        prior, fingerprint, arbiter_version=arb.ARBITER_VERSION
    ) is False, why


def test_unchanged_accepts_a_matching_live_group(monkeypatch):
    monkeypatch.delenv(fcp.REFRESH_HOURS_ENV, raising=False)
    for status in ("contested", "surfaced"):
        prior = {"input_fingerprint": "abc",
                 "arbiter_version": arb.ARBITER_VERSION, "status": status}
        assert fcp.unchanged(prior, "abc", arbiter_version=arb.ARBITER_VERSION)


def test_budget_gates_work_that_would_overrun_it():
    spent = fcp.PassBudget(1e-6)
    time.sleep(0.001)
    assert spent.exhausted()
    assert not spent.allows(arb.LLM_TIEBREAK_TIMEOUT_SECONDS)
    roomy = fcp.PassBudget(120.0)
    assert not roomy.exhausted()
    assert roomy.allows(arb.LLM_TIEBREAK_TIMEOUT_SECONDS)
    assert not roomy.allows(1_000.0), (
        "a call that cannot finish inside the budget must never be started"
    )
    # <= 0 is the documented OFF switch, not a zero-length budget: an operator
    # disabling the bound must get an unbounded pass, never one that refuses to
    # process a single group.
    for off in (0.0, -1.0):
        unbounded = fcp.PassBudget(off)
        assert unbounded.remaining == float("inf")
        assert not unbounded.exhausted()
        assert unbounded.allows(1e9)


def test_prior_load_failure_fails_open_to_recompute_everything():
    """A degraded read must mean 'nothing is known unchanged', never 'nothing
    changed' — the skip may only ever be granted on positive evidence."""

    class _Broken:
        async def fetch(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("connection reset")

    assert asyncio.run(fcp.load_prior_groups(_Broken())) == {}


# ===========================================================================
# Real Postgres — the pass itself
# ===========================================================================


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean(pg_pool):
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


async def _insert_fact(conn: Any, subject: str, value: str, *, seq: int,
                       cred: float = 0.9, conf: float = 0.8) -> UUID:
    fid = uuid4()
    await conn.execute(
        """
        INSERT INTO facts (id, subject, predicate, value, confidence,
                           source_type, source_credibility, produced_at,
                           valid_from, derived_from, data)
        VALUES ($1, $2, 'border status', $3, $4, 'rss', $5, now(), $6,
                $7::uuid[], '{}'::jsonb)
        """,
        fid, subject, value, conf, cred,
        datetime.now(tz=timezone.utc) - timedelta(minutes=seq), [uuid4()],
    )
    return fid


async def _dispute(conn: Any, subject: str) -> None:
    """A genuine 3-vs-3 dispute this file owns."""
    for i in range(3):
        await _insert_fact(conn, subject, "de-escalating", seq=i)
    for i in range(3):
        await _insert_fact(conn, subject, "clashes ongoing", seq=10 + i)


async def _stamps(conn: Any, subject: str) -> tuple[Any, list[Any]]:
    grp = await conn.fetchrow(
        "SELECT updated_at, resolved_at, input_fingerprint, status "
        "  FROM fact_contention WHERE subject_key = $1", _skey(subject),
    )
    facts = await conn.fetch(
        "SELECT id, updated_at, contested FROM facts WHERE subject = $1 "
        " ORDER BY id", subject,
    )
    return grp, list(facts)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unchanged_group_is_skipped_whole_and_writes_nothing(
    pg_pool, clean, monkeypatch,
):
    """The fix, end to end: pass 2 over untouched evidence must leave the group
    row AND every member fact byte-identical — same ``updated_at``, same
    ``resolved_at``. Before the fix both were ``now()`` on every pass."""
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    monkeypatch.delenv(fcp.REFRESH_HOURS_ENV, raising=False)
    subject = _subject()
    async with pg_pool.acquire() as conn:
        await _dispute(conn, subject)

    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        grp1, facts1 = await _stamps(conn, subject)
    assert grp1 is not None and grp1["input_fingerprint"], (
        "a recomputed group must record the fingerprint it was decided from"
    )
    assert all(f["contested"] for f in facts1)

    counts2 = await arb._run_arbiter(pg_pool, None)
    assert counts2["groups_unchanged"] >= 1
    async with pg_pool.acquire() as conn:
        grp2, facts2 = await _stamps(conn, subject)

    assert grp2["updated_at"] == grp1["updated_at"], "group row re-written"
    assert grp2["resolved_at"] == grp1["resolved_at"]
    assert grp2["input_fingerprint"] == grp1["input_fingerprint"]
    assert [f["updated_at"] for f in facts2] == [f["updated_at"] for f in facts1], (
        "facts.updated_at was bumped by an unchanged pass — this is exactly what "
        "hid ~78% of the open corpus from fact_decay's 30-day staleness sweep"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_evidence_invalidates_the_fingerprint_and_re_decides(
    pg_pool, clean, monkeypatch,
):
    """The skip must never outlive the evidence. One more supporting row and the
    group is recomputed — the reversibility guarantee, unweakened."""
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    monkeypatch.delenv(fcp.REFRESH_HOURS_ENV, raising=False)
    subject = _subject()
    async with pg_pool.acquire() as conn:
        await _dispute(conn, subject)
    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        grp1, _ = await _stamps(conn, subject)
        await _insert_fact(conn, subject, "de-escalating", seq=99)

    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        grp2, _ = await _stamps(conn, subject)
    assert grp2["input_fingerprint"] != grp1["input_fingerprint"]
    assert grp2["updated_at"] > grp1["updated_at"], "changed group was not re-written"
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM fact_contention_values v "
            "  JOIN fact_contention c ON c.id = v.contention_id "
            " WHERE c.subject_key = $1 AND NOT v.is_junk", _skey(subject),
        )
    assert n == 2, "the sidecar must still be recomputed from the open rows"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exhausted_budget_defers_groups_and_never_collapses(
    pg_pool, clean, monkeypatch,
):
    """A truncated pass has a PARTIAL live-key set. Running the stale-collapse
    sweep against it would collapse every group the pass never reached — a
    live dispute torn down because the arbiter ran out of clock."""
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    subject = _subject()
    async with pg_pool.acquire() as conn:
        await _dispute(conn, subject)
    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        grp1, _ = await _stamps(conn, subject)
    assert grp1["status"] in ("contested", "surfaced")

    monkeypatch.setenv(fcp.PASS_BUDGET_ENV, "0.000001")
    counts = await arb._run_arbiter(pg_pool, None)
    assert counts["groups_deferred"] >= 1, "budget must report what it deferred"
    assert counts["groups_collapsed"] == 0

    async with pg_pool.acquire() as conn:
        grp2, _ = await _stamps(conn, subject)
    assert grp2["status"] == grp1["status"], "a deferred group was collapsed"
    assert grp2["updated_at"] == grp1["updated_at"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_complete_pass_still_collapses_a_vanished_group(
    pg_pool, clean, monkeypatch,
):
    """The other half of the budget guard: suppressing the sweep on truncation
    must not suppress it on a pass that genuinely finished."""
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    monkeypatch.delenv(fcp.PASS_BUDGET_ENV, raising=False)
    subject = _subject()
    async with pg_pool.acquire() as conn:
        await _dispute(conn, subject)
    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        # The dispute converges: one side is retired, so the group drops below
        # two clusters and must collapse.
        await conn.execute(
            "DELETE FROM facts WHERE subject = $1 AND value = 'clashes ongoing'",
            subject,
        )
    await arb._run_arbiter(pg_pool, None)
    async with pg_pool.acquire() as conn:
        grp, facts = await _stamps(conn, subject)
    assert grp["status"] == "collapsed"
    assert not any(f["contested"] for f in facts), "collapse must clear the markers"
