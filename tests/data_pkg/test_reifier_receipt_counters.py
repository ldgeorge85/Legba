# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3/P5 — the two cheap receipt readings the spec's §5.3 adds.

The typing funnel is already published; these are the readings it was
throwing away:

  * ``scan_limit_binding`` — a NAMED flag on ``SelectionCounters``, true
    exactly when the scoring scan returned its LIMIT's worth of rows and
    every one cleared the bar (the DRAIN state: qualifying supply provably
    extends below the cut). Named so the reading is not inferred from two
    integers that happen to be equal — and deliberately NOT just
    ``qualified == examined``: a pool that ran out inside the window
    (``examined < examine``) is the STEADY state, not a binding limit.
  * ``inserted`` / ``folded`` — the entity_edges upsert split via
    ``RETURNING (xmax = 0) AS inserted``. A write that re-observes an open
    edge is a FOLD (observed_count + 1), not a new edge; the ~15-20 % fold
    rate the spec measured was invisible on the receipt.

The fold counter rides a task-local ContextVar sink
(``bind_edge_write_sink``) rather than the process-wide ``COUNTERS`` delta,
because every nexus producer shares the runtime process — a run receipt must
count exactly ITS writes.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts._reifier_receipt import build_reifier_summary
from legba.data.analysts.relationship_reifier import (
    ReifierDeps,
    run_method,
)
from legba.data.analysts.reifier_selection import (
    SelectionCounters,
    select_candidates,
)
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext, NexusPayload, write_nexus
from legba.data.provenance.entity_edge_writes import (
    COUNTERS,
    bind_edge_write_sink,
)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
def _fresh_counters():
    COUNTERS.reset()
    yield
    COUNTERS.reset()


# ---------------------------------------------------------------------------
# Seeding helpers (the test_reifier_selection shapes)
# ---------------------------------------------------------------------------


async def _seed_signals(conn, *, n: int, tag: str) -> list:
    """``n`` backing signals from DISTINCT publishers (the source floor)."""
    ids = []
    for i in range(n):
        sid = uuid4()
        await conn.execute(
            """
            INSERT INTO signals (id, source_id, payload, content_hash, fetched_at)
            VALUES ($1, $2, $3::jsonb, $4, now())
            """,
            sid,
            f"source.rc{tag}{i}.feed",
            json.dumps({"title": f"rc story {tag} {i}", "summary": ""}),
            f"hash-{sid}",
        )
        ids.append(sid)
    return ids


async def _seed_edge(
    conn, *, src: str, tgt: str, conf: float = 0.7, sources: int = 3,
):
    """A pending candidate whose evidence clears both hard floors."""
    tag = uuid4().hex[:6]
    sig_ids = await _seed_signals(conn, n=sources, tag=tag)
    await conn.execute(
        """
        INSERT INTO proposed_edges
            (source_entity, target_entity, relationship_type, confidence,
             evidence_text, status, derived_from)
        VALUES ($1, $2, 'co_occurs', $3, $4, 'pending', $5::uuid[])
        """,
        src, tgt, conf, f"{src} and {tgt} appeared together", sig_ids,
    )


async def _seed_entity(conn, name: str, *, aliases: list[str] | None = None):
    await conn.execute(
        """
        INSERT INTO entity_profiles (canonical_name, entity_class, data)
        VALUES ($1, 'organization', $2::jsonb)
        """,
        name, json.dumps({"merged_aliases": aliases or []}),
    )


# ---------------------------------------------------------------------------
# scan_limit_binding — the named DRAIN flag
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_limit_binding_flags_the_drain_state(pg_pool):
    """LIMIT reached AND every examined row qualified ⇒ supply extends below
    the cut — the flag must name it rather than leave it to be inferred."""
    tag = uuid4().hex[:8]
    async with pg_pool.acquire() as conn:
        # limit=2 → examine=6; eight qualifying rows force examined == examine
        # even on a shared session DB with ambient pending rows.
        for i in range(8):
            await _seed_edge(conn, src=f"RcDrain S{i} {tag}", tgt=f"RcDrain T{i} {tag}")
        _, counters = await select_candidates(conn, limit=2, bar=0.0)

    assert counters.examined == 6                     # the LIMIT was reached
    assert counters.qualified == counters.examined    # every row cleared
    assert counters.scan_limit_binding is True
    assert counters.as_dict()["scan_limit_binding"] is True  # bool, not 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_limit_binding_is_not_mere_equality(pg_pool):
    """qualified == examined with the pool EXHAUSTED inside the window is the
    STEADY state — the flag must be False even though the integers are equal."""
    async with pg_pool.acquire() as conn:
        # examine = min(8000, 30000) = 8000; the test pool is orders of
        # magnitude smaller, so the scan returns everything — LIMIT never binds.
        _, counters = await select_candidates(conn, limit=10000, bar=0.0)

    assert counters.examined < 8000
    assert counters.qualified == counters.examined    # equality alone → old inference
    assert counters.scan_limit_binding is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_limit_binding_false_when_the_bar_binds(pg_pool):
    """LIMIT reached but some examined rows are below the bar ⇒ the BAR bound
    the window, not the scan — the deeper rows are all weaker still."""
    tag = uuid4().hex[:8]
    async with pg_pool.acquire() as conn:
        for i in range(8):
            await _seed_edge(conn, src=f"RcBar S{i} {tag}", tgt=f"RcBar T{i} {tag}")
        # Default bar: only rows that actually qualify count, so unless the
        # whole examined window clears it the flag stays off.
        _, counters = await select_candidates(conn, limit=2, bar=99.0)

    assert counters.examined == 6
    assert counters.qualified == 0                    # the bar held everything back
    assert counters.scan_limit_binding is False


def test_build_summary_serializes_the_flag_as_jsonb_bool():
    """``->>'scan_limit_binding'`` must read 'true'/'false', never '1'/'0'."""
    finding = build_reifier_summary(
        n_candidates=0, typed=0, written=0, superseded=0, degraded=0,
        budget_paused=False, target_id=None,
        selection=SelectionCounters(
            examined=1800, qualified=1800, scan_limit_binding=True,
        ),
    )
    sel = finding.data["selection"]
    assert sel["scan_limit_binding"] is True
    assert "selection_scan_limit_binding=True" in finding.body


# ---------------------------------------------------------------------------
# inserted / folded — the upsert split
# ---------------------------------------------------------------------------


def _nexus_payload(subject: str, object_: str) -> NexusPayload:
    return NexusPayload(
        subject=subject, object=object_, rel_type="allied with",
        label=f"{subject} allied with {object_}", polarity=1,
        intent="", channel="direct", confidence=0.7,
        valid_from=None, data={},
    )


def _ctx() -> AnalystContext:
    return AnalystContext(
        analyst_id="relationship_reifier", analyst_version="test",
        run_id=uuid4(), target_id=None, target_version=None,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_upsert_split_counts_insert_then_fold(pg_pool):
    """First write mints the row (xmax=0); a second write on the open triple
    is a re-observation — folded, not inserted."""
    tag = uuid4().hex[:8]
    a_name, b_name = f"RcFold Alpha {tag}", f"RcFold Beta {tag}"
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, a_name)
        await _seed_entity(conn, b_name)
        with bind_edge_write_sink() as sink:
            await write_nexus(
                conn, analyst_ctx=_ctx(),
                payload=_nexus_payload(a_name, b_name), derived_from=[],
            )
            await write_nexus(
                conn, analyst_ctx=_ctx(),
                payload=_nexus_payload(a_name, b_name), derived_from=[],
            )

    assert [w["inserted"] for w in sink] == [True, False]
    assert all(w["outcome"] == "written" for w in sink)
    assert COUNTERS.written == 2
    assert COUNTERS.inserted == 1 and COUNTERS.folded == 1
    assert COUNTERS.inserted + COUNTERS.folded == COUNTERS.written
    data = COUNTERS.to_data()
    assert data["entity_edges_inserted"] == 1
    assert data["entity_edges_folded"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unbound_sink_and_out_of_scope_writes_are_invisible(pg_pool):
    """No bind → no overhead, no leak: a write outside the context does not
    appear in another task's receipt."""
    tag = uuid4().hex[:8]
    a_name, b_name = f"RcSolo A {tag}", f"RcSolo B {tag}"
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, a_name)
        await _seed_entity(conn, b_name)
        await write_nexus(
            conn, analyst_ctx=_ctx(),
            payload=_nexus_payload(a_name, b_name), derived_from=[],
        )
        with bind_edge_write_sink() as sink:
            pass
    assert sink == []
    assert COUNTERS.inserted == 1 and COUNTERS.folded == 0


# ---------------------------------------------------------------------------
# The full run receipt — inserted + folded == written through run_method
# ---------------------------------------------------------------------------


class _AcceptAllLLM:
    """Batch stub: accepts every CANDIDATE block with the same verdict."""

    subprovider = "stub"

    async def chat_complete(self, messages, **kw):
        prompt = messages[0]["content"]
        idxs = [
            int(line.split()[-2])
            for line in prompt.splitlines()
            if line.startswith("--- CANDIDATE ")
        ]
        if not idxs:  # the single-candidate retry prompt — one object
            return _resp(json.dumps(self._verdict(None)))
        return _resp(json.dumps([self._verdict(i) for i in idxs]))

    @staticmethod
    def _verdict(idx: int | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "related": True, "rel_type": "AlliedWith", "intent": "",
            "channel": "direct", "confidence": 0.7, "rationale": "stub",
        }
        if idx is not None:
            body["idx"] = idx
        return body


class _RejectAllLLM(_AcceptAllLLM):
    """Batch stub: the model says every candidate is unrelated (a model_reject)."""

    @staticmethod
    def _verdict(idx: int | None) -> dict[str, Any]:
        body: dict[str, Any] = {"related": False, "rationale": "stub reject"}
        if idx is not None:
            body["idx"] = idx
        return body


class _Usage:
    prompt_tokens = 3
    completion_tokens = 2
    reasoning_tokens = 0


def _resp(content: str):
    class _R:
        usage = _Usage()

    r = _R()
    r.content = content
    return r


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_method_receipt_splits_written_into_inserted_and_folded(
    pg_pool, clean_tables,
):
    """Two candidate surfaces resolving to ONE keeper pair: the first write
    inserts the edge, the second folds into it — and the receipt splits the
    ``written`` count accordingly, through the REAL run_method path."""
    await clean_tables("proposed_edges")
    tag = uuid4().hex[:8]
    keeper_a = f"RcRun Alpha {tag}"
    alias_a = f"RcRun Alpha Alias {tag}"
    b_name = f"RcRun Beta {tag}"
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, keeper_a, aliases=[alias_a])
        await _seed_entity(conn, b_name)
        # Two surfaces of the SAME keeper pair — both pass selection (no open
        # nexus exists at scan time), the second write folds.
        await _seed_edge(conn, src=keeper_a, tgt=b_name)
        await _seed_edge(conn, src=alias_a, tgt=b_name)

    res = await run_method(
        [], {"analyst_id": "relationship_reifier"},
        ReifierDeps(llm=_AcceptAllLLM(), pg_pool=pg_pool),
    )
    data = res.finding.data
    assert data["written"] == 2
    assert data["inserted"] == 1
    assert data["folded"] == 1
    assert data["inserted"] + data["folded"] == data["written"]
    # And the named flag rode along inside the selection receipt.
    assert isinstance(data["selection"]["scan_limit_binding"], bool)
    assert "inserted=1 folded=1" in res.finding.body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_method_receipt_reports_the_already_reified_exclusion(
    pg_pool, clean_tables,
):
    """2026-09-23 — the already-reified guard moved INTO the scan SQL. A pair
    with an existing open nexus must never reach the typer, and the receipt's
    ``selection.already_reified`` counter must say so — exercised through the
    REAL ``run_method`` path, not a unit-level call into ``select_candidates``.
    """
    await clean_tables("proposed_edges")
    tag = uuid4().hex[:8]
    dead_a, dead_b = f"ArRun Dead A {tag}", f"ArRun Dead B {tag}"
    live_a, live_b = f"ArRun Live A {tag}", f"ArRun Live B {tag}"
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, dead_a)
        await _seed_entity(conn, dead_b)
        await _seed_entity(conn, live_a)
        await _seed_entity(conn, live_b)
        await conn.execute(
            """
            INSERT INTO nexuses (subject, object, rel_type, label, polarity,
                                 intent, channel, confidence, valid_from)
            VALUES ($1, $2, 'HostileTo', $3, -1, 'hostile', 'direct', 0.7, now())
            """,
            dead_a, dead_b, f"{dead_a} HostileTo {dead_b}",
        )
        await _seed_edge(conn, src=dead_a, tgt=dead_b)
        await _seed_edge(conn, src=live_a, tgt=live_b)

    res = await run_method(
        [], {"analyst_id": "relationship_reifier"},
        ReifierDeps(llm=_AcceptAllLLM(), pg_pool=pg_pool),
    )
    data = res.finding.data
    assert data["typed"] == 1, "the already-reified pair must never reach the typer"
    assert data["selection"]["already_reified"] >= 1
    assert "selection_already_reified=" in res.finding.body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_method_stamps_rejections_and_the_next_run_skips_them(
    pg_pool, clean_tables,
):
    """H9 — a rejected candidate gets ``reviewed_at`` (status stays pending), the
    receipt says how many were stamped, and the NEXT run does not type it again."""
    await clean_tables("proposed_edges")
    tag = uuid4().hex[:8]
    a, b = f"RjRun Alpha {tag}", f"RjRun Beta {tag}"
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, a)
        await _seed_entity(conn, b)
        await _seed_edge(conn, src=a, tgt=b)

    res = await run_method(
        [], {"analyst_id": "relationship_reifier"},
        ReifierDeps(llm=_RejectAllLLM(), pg_pool=pg_pool),
    )
    data = res.finding.data
    assert data["typed"] == 1 and data["rejected"] == 1 and data["accepted"] == 0
    assert data["rejected_marked"] == 1
    assert "rejected_marked=1" in res.finding.body
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, reviewed_at FROM proposed_edges WHERE source_entity = $1", a)
    assert row["status"] == "pending" and row["reviewed_at"] is not None

    res2 = await run_method(
        [], {"analyst_id": "relationship_reifier"},
        ReifierDeps(llm=_RejectAllLLM(), pg_pool=pg_pool),
    )
    assert res2.finding.data["typed"] == 0, "inside the cooldown the scan does not offer it again"
    assert res2.finding.data["rejected_marked"] == 0


# ---------------------------------------------------------------------------
# H14 — the wall-clock TURN budget (distinct from the $/token `budget_paused`
# above): a pass gets a partial receipt + resumes the untouched tail on the
# next tick instead of holding the actor turn for the whole candidate window.
# ---------------------------------------------------------------------------


def test_reifier_pass_budget_seconds_env_parsing(monkeypatch):
    """Env-only, read once per run: unset -> default; a set value wins; a
    malformed value reads as the default (never as an accidental zero, which
    would make every run refuse its first candidate). Also proves
    ``relationship_reifier`` re-exports the same names from
    ``_reifier_pass_budget`` (the size-gate extraction), not a stale copy."""
    from legba.data.analysts import relationship_reifier as reifier_mod
    from legba.data.analysts._reifier_pass_budget import (
        DEFAULT_REIFIER_PASS_BUDGET_SECONDS,
        REIFIER_PASS_BUDGET,
        reifier_pass_budget_seconds,
    )

    assert REIFIER_PASS_BUDGET == "LEGBA_REIFIER_PASS_BUDGET_SECONDS"
    assert reifier_mod.REIFIER_PASS_BUDGET is REIFIER_PASS_BUDGET
    assert (
        reifier_mod.DEFAULT_REIFIER_PASS_BUDGET_SECONDS
        == DEFAULT_REIFIER_PASS_BUDGET_SECONDS
    )
    assert reifier_mod.reifier_pass_budget_seconds is reifier_pass_budget_seconds

    monkeypatch.delenv(REIFIER_PASS_BUDGET, raising=False)
    assert reifier_pass_budget_seconds() == DEFAULT_REIFIER_PASS_BUDGET_SECONDS

    monkeypatch.setenv(REIFIER_PASS_BUDGET, "45")
    assert reifier_pass_budget_seconds() == 45.0

    monkeypatch.setenv(REIFIER_PASS_BUDGET, "not-a-number")
    assert reifier_pass_budget_seconds() == DEFAULT_REIFIER_PASS_BUDGET_SECONDS

    # `<= 0` is the unbounded escape hatch, not a malformed value — 0 must
    # read back as exactly 0.0, never silently promoted to the default.
    monkeypatch.setenv(REIFIER_PASS_BUDGET, "0")
    assert reifier_pass_budget_seconds() == 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pass_budget_unbounded_is_byte_identical_to_pre_h14(
    pg_pool, clean_tables, monkeypatch,
):
    """`<= 0` disables the H14 gate entirely: every admitted candidate is typed
    exactly like the pre-H14 shape, and the receipt says so plainly
    (`pass_budget_exceeded=False`, `stopped_after=None`, `cursor=None`)."""
    await clean_tables("proposed_edges")
    monkeypatch.setenv("LEGBA_REIFIER_PASS_BUDGET_SECONDS", "0")
    tag = uuid4().hex[:8]
    a, b = f"UbA {tag}", f"UbB {tag}"
    async with pg_pool.acquire() as conn:
        await _seed_edge(conn, src=a, tgt=b)

    res = await run_method(
        [], {"analyst_id": "relationship_reifier"},
        ReifierDeps(llm=_AcceptAllLLM(), pg_pool=pg_pool),
    )
    data = res.finding.data
    assert data["typed"] == 1 and data["written"] == 1
    assert data["pass_budget_exceeded"] is False
    assert data["stopped_after"] is None
    assert data["cursor"] is None
    assert data["per_candidate_seconds"]["mean"] >= 0.0
    assert "pass_budget_exceeded=False" in res.finding.body
    assert "stopped_after=None" in res.finding.body


class _FiniteAllowsBudget:
    """A ``PassBudget`` stand-in whose ``allows()`` answers True ``checks``
    times, then False — the ``_FiniteBudget`` idiom
    (test_event_clustering_p1.py), adapted to H14's admission call
    (``allows(estimate)``, called once per candidate) so the exact
    truncation point is placed deterministically instead of racing the wall
    clock with a tiny real budget."""

    def __init__(self, checks: int) -> None:
        self._checks_left = checks

    def allows(self, cost_seconds: float) -> bool:
        self._checks_left -= 1
        return self._checks_left >= 0

    def exhausted(self) -> bool:
        return self._checks_left < 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pass_budget_truncates_and_next_run_resumes_the_tail(
    pg_pool, clean_tables, monkeypatch,
):
    """A pass that runs out of budget mid-run carries a partial receipt
    (``pass_budget_exceeded``, ``stopped_after``, a ``cursor``) naming the last
    candidate it examined — never the untouched one — and the untouched tail
    is not lost: the NEXT (unbounded) run types it, not the two
    already-processed candidates again (they already left the pool via the
    already-reified guard, unchanged this lane)."""
    await clean_tables("proposed_edges")
    tag = uuid4().hex[:8]
    pairs = [(f"FinA{i} {tag}", f"FinB{i} {tag}") for i in range(3)]
    async with pg_pool.acquire() as conn:
        for src, tgt in pairs:
            await _seed_edge(conn, src=src, tgt=tgt)

    import legba.data.analysts.relationship_reifier as reifier_mod
    from legba.data.analysts.deterministic_handlers.fact_contention_pass import (
        PassBudget as RealPassBudget,
    )

    monkeypatch.setattr(
        reifier_mod, "PassBudget", lambda seconds=None: _FiniteAllowsBudget(2)
    )
    deps = ReifierDeps(
        llm=_AcceptAllLLM(), pg_pool=pg_pool, batch_size=1, max_candidates=10,
    )
    first = await run_method([], {"analyst_id": "relationship_reifier"}, deps)
    data = first.finding.data
    assert data["typed"] == 2, data
    assert data["pass_budget_exceeded"] is True
    assert data["stopped_after"] is not None
    assert data["cursor"] is not None
    assert data["cursor"]["stopped_after"] == data["stopped_after"]
    assert data["per_candidate_seconds"]["mean"] >= 0.0
    assert "pass_budget_exceeded=True" in first.finding.body

    async with pg_pool.acquire() as conn:
        written_rows = await conn.fetch(
            "SELECT subject FROM nexuses WHERE valid_until IS NULL"
            " AND superseded_by IS NULL AND subject = ANY($1::text[])",
            [src for src, _ in pairs],
        )
    assert len(written_rows) == 2, "exactly two of the three were admitted"
    written_subjects = {r["subject"] for r in written_rows}
    untouched = [src for src, _ in pairs if src not in written_subjects]
    assert len(untouched) == 1

    async with pg_pool.acquire() as conn:
        untouched_id = await conn.fetchval(
            "SELECT id FROM proposed_edges WHERE source_entity = $1", untouched[0],
        )
    assert data["stopped_after"] != str(untouched_id), (
        "stopped_after names the last EXAMINED candidate, never the one the "
        "run never reached"
    )

    # Next tick, unbounded (the REAL PassBudget restored — plenty of room for
    # one tiny stub candidate): resumes at the untouched tail, not the head.
    monkeypatch.setattr(reifier_mod, "PassBudget", RealPassBudget)
    second = await run_method([], {"analyst_id": "relationship_reifier"}, deps)
    data2 = second.finding.data
    assert data2["typed"] == 1, "only the untouched candidate remains to type"
    assert data2["pass_budget_exceeded"] is False
    assert data2["cursor"] is None

    async with pg_pool.acquire() as conn:
        final_written = await conn.fetchval(
            "SELECT count(*) FROM nexuses WHERE valid_until IS NULL"
            " AND superseded_by IS NULL AND subject = ANY($1::text[])",
            [src for src, _ in pairs],
        )
    assert final_written == 3, "all three candidates are eventually reified, none lost"
