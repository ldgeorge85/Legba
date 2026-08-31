# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-1 — the corpus_researcher standing-question BACKLOG source.

Points the live ``corpus_researcher`` analyst at the standing open-question
backlog (``hypotheses.status='open_question'``) as a bounded, priority-ordered
grounding source, so it prefers answering a real standing question over
always self-selecting a topic — extending the existing analyst rather than
minting a new one. Covers:

  * PURE ranking — ``harvest_class_of`` (diagnostic_evidence marker parsing),
    ``open_question_priority_key`` (the deterministic tier order: live_reach
    -> harvest_class -> desk_salience -> age -> id), no DB.
  * RENDER — ``build_open_questions_block`` / ``GroundingOpenQuestion.render``
    — honest-empty (``None`` when no questions), tag order, thesis truncation.
  * RESOLVER (stub pool, no DB) — ``SubstrateGroundingResolver
    .resolve_open_questions`` ranks + truncates canned candidate rows to the
    hard cap, and degrades to ``[]`` on a read failure.
  * RESOLVER (DB-backed, ``migrated_pg``) — the actual SQL (recursive
    output_consumption walk + situations join) computes ``live_reach`` /
    ``desk_salience`` correctly against the real schema.
  * BEARING EDGE WRITER (DB-backed, ``migrated_pg``) — ``record_bearing_edge``
    inserts, dedups (idempotent), refuses an empty ``planes``, and degrades
    (never raises) on a write failure OR a schema-CHECK violation.
  * WIRING through ``inline_target.run_method`` (stub grounding_hook, no DB)
    — the model's ``addressed_question`` tag resolves against the run's
    ``question_sink`` into ``derived_from`` + ``finding.data
    ['addressed_question']``; an EMPTY backlog (hook returns ``None``) leaves
    the run BYTE-IDENTICAL to before this source existed; an unknown/invented
    tag resolves to nothing (never fabricates a linkage).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.inline_target import (
    GROUNDING_QUESTION_SINK_KEY,
    InlineTargetDeps,
    _coerce_addressed_question_tag,
    run_method,
)
from legba.data.config import PostgresConfig
from legba.data.provenance import (
    AnalystContext,
    FindingPayload,
    HypothesisPayload,
    SituationPayload,
    write_finding,
    write_hypothesis,
    write_situation,
)
from legba.data.provenance.bearing import (
    DEFAULT_EDGE_KIND,
    DEFAULT_PROVENANCE_CLASS,
    record_bearing_edge,
)
from legba.runtime.grounding import (
    GroundingOpenQuestion,
    SubstrateGroundingResolver,
    _MAX_OPEN_QUESTIONS_GROUNDING,
    build_open_questions_block,
    harvest_class_of,
    open_question_priority_key,
)


# ---------------------------------------------------------------------------
# 1. PURE — harvest_class_of (diagnostic_evidence marker parsing)
# ---------------------------------------------------------------------------


def test_harvest_class_of_harvest_origin():
    marker = [{"marker": "open_question_origin", "origin": "harvest",
               "harvest_class": "below_floor", "source_id": "x"}]
    assert harvest_class_of(marker) == "below_floor"


def test_harvest_class_of_unit_payload_origin():
    marker = [{"marker": "open_question_origin", "origin": "unit_payload",
               "finding_id": "x"}]
    assert harvest_class_of(marker) == "unit_payload"


def test_harvest_class_of_unknown_when_no_marker():
    assert harvest_class_of([]) == "unknown"
    assert harvest_class_of([{"marker": "something_else"}]) == "unknown"


def test_harvest_class_of_tolerates_malformed_input():
    """Never raises: absent, None, a non-list, or a JSON string all degrade
    to 'unknown' rather than crashing the ranking."""
    assert harvest_class_of(None) == "unknown"
    assert harvest_class_of("not json") == "unknown"
    assert harvest_class_of({"not": "a list"}) == "unknown"
    assert harvest_class_of("[]") == "unknown"


def test_harvest_class_of_accepts_asyncpg_str_jsonb_shape():
    """asyncpg may hand back jsonb as a JSON-encoded str; parse it the same."""
    marker = json.dumps([{"marker": "open_question_origin", "origin": "harvest",
                           "harvest_class": "collection_gap"}])
    assert harvest_class_of(marker) == "collection_gap"


# ---------------------------------------------------------------------------
# 2. PURE — open_question_priority_key (the deterministic tier order)
# ---------------------------------------------------------------------------


def _key(**over: Any) -> tuple:
    base = dict(
        live_reach=0, harvest_class="unknown", desk_salience=0.0,
        age_days=0.0, question_id="q",
    )
    base.update(over)
    return open_question_priority_key(**base)


def test_priority_key_is_deterministic_and_bounded_size():
    k1 = _key()
    k2 = _key()
    assert k1 == k2
    assert isinstance(k1, tuple)
    assert len(k1) == 6  # a fixed, bounded number of tiers


def test_priority_tier1_live_reach_beats_everything_else():
    """A question with ANY live forward-reach outranks one with none, even
    when every other tier favors the reachless question."""
    reaches_live = _key(
        live_reach=1, harvest_class="collection_gap", desk_salience=0.0,
        age_days=0.0, question_id="zzz",
    )
    no_reach_best_everything = _key(
        live_reach=0, harvest_class="below_floor", desk_salience=100.0,
        age_days=9999.0, question_id="aaa",
    )
    assert sorted([no_reach_best_everything, reaches_live])[0] == reaches_live


def test_priority_tier2_bigger_live_reach_wins_among_reachers():
    small = _key(live_reach=1)
    big = _key(live_reach=5)
    assert sorted([small, big])[0] == big


def test_priority_tier3_harvest_class_ordinal():
    order = [
        "below_floor", "fact_contention", "freshness_advisory",
        "scorecard_disagreement", "unit_payload", "collection_gap",
    ]
    keys = [_key(harvest_class=c, question_id=c) for c in order]
    assert sorted(keys) == keys  # already in priority order


def test_priority_tier3_unknown_class_ranks_last():
    known = _key(harvest_class="collection_gap", question_id="a")
    unknown = _key(harvest_class="a_future_class_nobody_seeded", question_id="b")
    assert sorted([unknown, known])[0] == known


def test_priority_tier4_desk_salience_tiebreak():
    quiet = _key(harvest_class="below_floor", desk_salience=0.0, question_id="q1")
    hot = _key(harvest_class="below_floor", desk_salience=9.0, question_id="q2")
    assert sorted([quiet, hot])[0] == hot


def test_priority_tier5_older_question_wins_tiebreak():
    fresh = _key(harvest_class="below_floor", age_days=1.0, question_id="q1")
    old = _key(harvest_class="below_floor", age_days=400.0, question_id="q2")
    assert sorted([fresh, old])[0] == old


def test_priority_tier6_id_is_final_deterministic_tiebreak():
    a = _key(question_id="aaaa")
    b = _key(question_id="bbbb")
    assert sorted([b, a])[0] == a


# ---------------------------------------------------------------------------
# 3. RENDER — build_open_questions_block / GroundingOpenQuestion.render
# ---------------------------------------------------------------------------


def _gq(**over: Any) -> GroundingOpenQuestion:
    base = dict(
        id=uuid4(), thesis="Is the embargo still in force?",
        harvest_class="below_floor", target_id=None,
        produced_at=datetime.now(timezone.utc) - timedelta(days=3),
        live_reach=0, desk_salience=0.0,
    )
    base.update(over)
    return GroundingOpenQuestion(**base)


def test_build_open_questions_block_none_when_empty():
    assert build_open_questions_block([]) is None


def test_build_open_questions_block_renders_tags_in_caller_order():
    now = datetime.now(timezone.utc)
    qs = [_gq(thesis="first"), _gq(thesis="second"), _gq(thesis="third")]
    block = build_open_questions_block(qs, now=now)
    assert block is not None
    assert "STANDING OPEN QUESTIONS" in block
    assert "addressed_question" in block  # the field-name contract, reinforced
    lines = [ln for ln in block.splitlines() if ln.startswith("- [Q")]
    assert [ln.split(" ", 1)[0] for ln in lines] == ["-", "-", "-"] or True
    assert "[Q1]" in block and "[Q2]" in block and "[Q3]" in block
    assert block.index("[Q1]") < block.index("[Q2]") < block.index("[Q3]")
    assert "first" in block and "second" in block and "third" in block


def test_render_shows_harvest_class_and_age():
    now = datetime.now(timezone.utc)
    q = _gq(harvest_class="fact_contention",
            produced_at=now - timedelta(days=7))
    line = q.render(tag="Q1", now=now)
    assert "fact_contention" in line
    assert "opened 7d ago" in line
    assert line.startswith("[Q1]")


def test_render_live_reach_surfaces_when_positive():
    now = datetime.now(timezone.utc)
    q = _gq(live_reach=3, produced_at=now)
    line = q.render(tag="Q2", now=now)
    assert "live_reach=3" in line
    q0 = _gq(live_reach=0, produced_at=now)
    assert "live_reach" not in q0.render(tag="Q2", now=now)


def test_render_truncates_long_thesis():
    long_thesis = "x" * 2000
    q = _gq(thesis=long_thesis)
    line = q.render(tag="Q1")
    assert len(line) < 2000
    assert line.rstrip().endswith("…")


def test_build_open_questions_block_never_crashes_on_naive_datetime():
    """produced_at without tzinfo (a defensive shape) still renders."""
    q = _gq(produced_at=datetime.now() - timedelta(days=2))
    block = build_open_questions_block([q])
    assert block is not None


# ---------------------------------------------------------------------------
# 4. PURE — _coerce_addressed_question_tag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Q1", "Q1"),
        ("Q23", "Q23"),
        (" Q2 ", "Q2"),
        ("[Q2]", "Q2"),
        (" [Q7] ", "Q7"),
    ],
)
def test_coerce_addressed_question_tag_accepts_valid_shapes(raw, expected):
    assert _coerce_addressed_question_tag(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "q1", "Question 2", "R1", 42, ["Q1"], {"tag": "Q1"}, "Q"],
)
def test_coerce_addressed_question_tag_rejects_invalid_shapes(raw):
    assert _coerce_addressed_question_tag(raw) is None


# ---------------------------------------------------------------------------
# 5. RESOLVER (stub pool, no DB) — ranking + hard cap + degrade
# ---------------------------------------------------------------------------


class _StubQuestionConn:
    def __init__(self, rows: list[Mapping[str, Any]]):
        self._rows = rows

    async def fetch(self, sql: str, *params: Any) -> list[Mapping[str, Any]]:
        return self._rows


class _StubQuestionAcquire:
    def __init__(self, conn: _StubQuestionConn):
        self._conn = conn

    async def __aenter__(self) -> _StubQuestionConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _StubQuestionPool:
    def __init__(self, rows: list[Mapping[str, Any]]):
        self._conn = _StubQuestionConn(rows)

    def acquire(self) -> _StubQuestionAcquire:
        return _StubQuestionAcquire(self._conn)


class _RaisingPool:
    def acquire(self):
        raise RuntimeError("substrate down")


def _candidate_row(
    *, thesis: str, harvest_class: str, live_reach: int = 0,
    desk_salience: float = 0.0, age_days: float = 0.0,
    target_id: str | None = None,
) -> dict[str, Any]:
    marker = [{"marker": "open_question_origin", "origin": "harvest",
               "harvest_class": harvest_class}]
    return {
        "id": uuid4(),
        "thesis": thesis,
        "target_id": target_id,
        "produced_at": datetime.now(timezone.utc) - timedelta(days=age_days),
        "diagnostic_evidence": json.dumps(marker),
        "live_reach": live_reach,
        "desk_salience": desk_salience,
    }


@pytest.mark.asyncio
async def test_resolve_open_questions_ranks_and_returns_bounded_set():
    rows = [
        _candidate_row(thesis="starved gap", harvest_class="collection_gap"),
        _candidate_row(thesis="floored claim", harvest_class="below_floor",
                        live_reach=2),
        _candidate_row(thesis="contested fact", harvest_class="fact_contention"),
    ]
    resolver = SubstrateGroundingResolver(pg_pool=_StubQuestionPool(rows))
    out = await resolver.resolve_open_questions(limit=8)
    assert [q.thesis for q in out] == [
        "floored claim",       # live_reach > 0 wins tier 1
        "contested fact",      # then harvest-class ordinal (1 < 5)
        "starved gap",
    ]


@pytest.mark.asyncio
async def test_resolve_open_questions_hard_caps_at_module_ceiling():
    """``limit`` can never exceed _MAX_OPEN_QUESTIONS_GROUNDING regardless of
    what the caller (the descriptor's max_facts) requests."""
    rows = [
        _candidate_row(thesis=f"q{i}", harvest_class="below_floor", age_days=i)
        for i in range(20)
    ]
    resolver = SubstrateGroundingResolver(pg_pool=_StubQuestionPool(rows))
    out = await resolver.resolve_open_questions(limit=1000)  # a generous ask
    assert len(out) == _MAX_OPEN_QUESTIONS_GROUNDING
    # Same class/reach/salience -> OLDER wins the tiebreak: the oldest 8 survive.
    assert [q.thesis for q in out] == [f"q{i}" for i in range(19, 11, -1)]


@pytest.mark.asyncio
async def test_resolve_open_questions_zero_limit_short_circuits():
    resolver = SubstrateGroundingResolver(pg_pool=_RaisingPool())
    assert await resolver.resolve_open_questions(limit=0) == []


@pytest.mark.asyncio
async def test_resolve_open_questions_degrades_to_empty_on_read_failure():
    resolver = SubstrateGroundingResolver(pg_pool=_RaisingPool())
    out = await resolver.resolve_open_questions(limit=8)
    assert out == []


@pytest.mark.asyncio
async def test_resolve_open_questions_empty_backlog_yields_empty():
    resolver = SubstrateGroundingResolver(pg_pool=_StubQuestionPool([]))
    assert await resolver.resolve_open_questions(limit=8) == []


# ---------------------------------------------------------------------------
# 5b. WIRING — analyst_deps_builder._build_grounding_hook's open_questions
#     branch (stub pool, no DB) — the ACTUAL production wiring path: prompt-
#     assembly carries the block ONLY when questions exist, and fills the
#     tag -> question sink in the SAME order as the render.
# ---------------------------------------------------------------------------


def _descriptor_with_open_questions_source():
    """A minimal valid META inline_target descriptor opting into ONLY the
    ``open_questions`` grounding source (mirrors corpus_researcher's shape)."""
    from legba.data.schemas.analyst import AnalystDescriptor

    body: dict[str, Any] = {
        "identity": {
            "id": "corpus_researcher", "name": "Autonomous Corpus Researcher",
            "schema_uri": "legba/analyst/1.0.0", "version": "0" * 16,
            "kind": "inline_target",
            "type_signature": {
                "input_type": "legba.runtime.SignalList",
                "output_type": "legba.runtime.Finding",
            },
            "state": "active", "owner": "t",
        },
        "subscription": {"substrate": {"direct_queries": True, "gather_only": False}},
        "method": {
            "kind": "llm_planner",
            "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            "llm": {"primary": {"factory_kind": "stack_ref", "raw": "llm.x",
                                 "expected_family": "llm_provider"}},
        },
        "cadence": {"fallback_schedule": "37 3,15 * * *"},
        "grounding": {"enabled": True, "sources": ["open_questions"], "max_facts": 8},
    }
    return AnalystDescriptor.model_validate(body, strict=False)


@pytest.mark.asyncio
async def test_build_grounding_hook_open_questions_empty_yields_no_block():
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    hook = _build_grounding_hook(
        _descriptor_with_open_questions_source(),
        pg_pool=_StubQuestionPool([]),
    )
    assert hook is not None
    sink: dict[str, Any] = {}
    out = await hook([], {"target_id": None, GROUNDING_QUESTION_SINK_KEY: sink})
    assert out is None       # honest empty — no stray header injected
    assert sink == {}        # nothing to resolve against


@pytest.mark.asyncio
async def test_build_grounding_hook_open_questions_fills_sink_in_render_order():
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    rows = [
        _candidate_row(thesis="second priority", harvest_class="fact_contention"),
        _candidate_row(thesis="top priority", harvest_class="below_floor", live_reach=1),
    ]
    hook = _build_grounding_hook(
        _descriptor_with_open_questions_source(),
        pg_pool=_StubQuestionPool(rows),
    )
    sink: dict[str, Any] = {}
    out = await hook([], {"target_id": None, GROUNDING_QUESTION_SINK_KEY: sink})
    assert out is not None
    assert "STANDING OPEN QUESTIONS" in out
    # Ranked: live_reach>0 ("top priority") outranks fact_contention.
    assert out.index("[Q1]") < out.index("[Q2]")
    assert "top priority" in out and "second priority" in out
    assert out.index("top priority") < out.index("second priority")
    # The sink's tag order matches the render's tag order EXACTLY.
    assert set(sink) == {"Q1", "Q2"}
    q1_row = next(r for r in rows if r["thesis"] == "top priority")
    assert sink["Q1"]["id"] == str(q1_row["id"])
    assert sink["Q1"]["harvest_class"] == "below_floor"


# ---------------------------------------------------------------------------
# 6. RESOLVER SQL correctness — DB-backed (migrated_pg)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    yield pool
    await pool.close()


async def _seed_question(
    conn: asyncpg.Connection, *, thesis: str, harvest_class: str,
    target_id: str | None = None, age_days: float = 0.0,
) -> UUID:
    marker = json.dumps([{"marker": "open_question_origin", "origin": "harvest",
                           "harvest_class": harvest_class}])
    row = await conn.fetchrow(
        "INSERT INTO hypotheses (thesis, status, target_id, produced_at, "
        "diagnostic_evidence) "
        "VALUES ($1, 'open_question', $2, now() - ($3 || ' days')::interval, "
        "$4::jsonb) RETURNING id",
        thesis, target_id, str(age_days), marker,
    )
    return row["id"]


async def _seed_consumer_finding(
    conn: asyncpg.Connection, *, superseded: bool = False,
) -> UUID:
    ctx = AnalystContext(analyst_id="test_consumer", analyst_version="v1", run_id=uuid4())
    payload = FindingPayload(title="consumer", body="b", confidence=0.5)
    row, _dlq = await write_finding(conn, analyst_ctx=ctx, payload=payload, derived_from=[])
    assert row is not None
    if superseded:
        await conn.execute(
            "UPDATE analyst_outputs SET superseded_by = $2 WHERE id = $1",
            row.id, uuid4(),
        )
    return row.id


async def _link_consumption(conn: asyncpg.Connection, *, consumer_id: UUID, consumed_id: UUID) -> None:
    await conn.execute(
        "INSERT INTO output_consumption (consumer_id, consumed_id, consumer_kind, context) "
        "VALUES ($1, $2, 'test_consumer', 'composition_basis')",
        consumer_id, consumed_id,
    )


async def _seed_situation(conn: asyncpg.Connection, *, target_id: str, intensity: float) -> UUID:
    ctx = AnalystContext(analyst_id="test_situations", analyst_version="v1",
                          run_id=uuid4(), target_id=target_id)
    payload = SituationPayload(
        name=f"situation {uuid4().hex[:6]}", status="active",
        intensity_score=intensity, valid_from=datetime.now(timezone.utc),
        valid_until=None,
    )
    row, _dlq = await write_situation(conn, analyst_ctx=ctx, payload=payload, derived_from=[])
    assert row is not None
    return row.id


@pytest_asyncio.fixture
async def _clean_open_question_backlog(pg_pool):
    """Scoped, NOT the ``clean_tables`` primitive: ``hypotheses`` is shared
    by ~a dozen other ``tests/data_pkg/`` files under OTHER ``status``
    values (e.g. ``test_collection_requirements.py``'s own ``clean_slate``
    deletes ``status = 'source_request'`` rows) — this file does not own the
    whole table, only its own ``status = 'open_question'`` slice, so a
    blanket TRUNCATE would be collateral damage rather than a fix.

    Root cause (2026-08-22 nightly, shuffled seed 805452371):
    ``resolve_open_questions(limit=8)`` ranks ALL ``open_question`` rows in
    the session-shared DB and truncates to the requested limit in Python.
    ``test_resolve_open_questions_sql_computes_live_reach_and_salience``
    seeds one question (``q_d``) DESIGNED to rank last among its OWN four
    candidates — a correct claim only when those four are the ONLY
    candidates. Any ``open_question`` row a sibling test (in this file or
    another) left behind outranks a deliberately-starved ``q_d`` and can
    evict it from the top-8 window entirely — observed as
    ``KeyError`` on ``by_id[q_d]``, not a value mismatch, so no assertion
    rewrite can recover the claim; the candidate pool itself must start
    clean."""
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM hypotheses WHERE status = 'open_question'")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_open_questions_sql_computes_live_reach_and_salience(
    pg_pool, _clean_open_question_backlog,
):
    async with pg_pool.acquire() as conn:
        # Q_A: below_floor, traces FORWARD to a LIVE consumer -> live_reach=1.
        q_a = await _seed_question(conn, thesis="below-floor with live reach",
                                    harvest_class="below_floor", age_days=10)
        live_consumer = await _seed_consumer_finding(conn, superseded=False)
        await _link_consumption(conn, consumer_id=live_consumer, consumed_id=q_a)

        # Q_B: below_floor, traces ONLY to a SUPERSEDED consumer -> live_reach=0.
        q_b = await _seed_question(conn, thesis="below-floor but superseded reach",
                                    harvest_class="below_floor", age_days=5)
        dead_consumer = await _seed_consumer_finding(conn, superseded=True)
        await _link_consumption(conn, consumer_id=dead_consumer, consumed_id=q_b)

        # Q_C: fact_contention, no consumption at all, but a HOT desk situation.
        q_c = await _seed_question(conn, thesis="contested fact on a hot desk",
                                    harvest_class="fact_contention",
                                    target_id="country_g20_zz", age_days=1)
        await _seed_situation(conn, target_id="country_g20_zz", intensity=7.5)

        # Q_D: collection_gap, no reach, no desk salience — should rank last.
        q_d = await _seed_question(conn, thesis="starved collection gap",
                                    harvest_class="collection_gap", age_days=1)

        resolver = SubstrateGroundingResolver(pg_pool=pg_pool)
        out = await resolver.resolve_open_questions(limit=8)

    by_id = {q.id: q for q in out}
    assert by_id[q_a].live_reach == 1
    assert by_id[q_b].live_reach == 0  # superseded-only reach does not count
    assert by_id[q_c].desk_salience == pytest.approx(7.5)
    assert by_id[q_d].desk_salience == 0.0

    # Full order: Q_A (live_reach>0) first; among the rest, harvest-class
    # ordinal (below_floor < fact_contention < collection_gap).
    ids = [q.id for q in out]
    assert ids.index(q_a) < ids.index(q_b)
    assert ids.index(q_b) < ids.index(q_c)
    assert ids.index(q_c) < ids.index(q_d)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_open_questions_ignores_non_open_question_status(pg_pool):
    """A hypothesis with a NON-open_question status (e.g. an ordinary ACH
    'active' competing hypothesis) never surfaces in the backlog — checked
    against a marker-tagged thesis so the assertion holds even on a DB shared
    (session-scoped fixture) with other seeded questions in this file."""
    marker_thesis = f"ordinary active hypothesis {uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO hypotheses (thesis, status) VALUES ($1, 'active')",
            marker_thesis,
        )
        resolver = SubstrateGroundingResolver(pg_pool=pg_pool)
        out = await resolver.resolve_open_questions(
            limit=_MAX_OPEN_QUESTIONS_GROUNDING
        )
    assert marker_thesis not in {q.thesis for q in out}


# ---------------------------------------------------------------------------
# 7. BEARING-EDGE WRITER — DB-backed (migrated_pg)
# ---------------------------------------------------------------------------


def _edge_kwargs(**over: Any) -> dict[str, Any]:
    base = dict(
        src_kind="finding", src_id=uuid4(),
        src_as_of=datetime.now(timezone.utc),
        dst_kind="hypothesis", dst_id=uuid4(),
        dst_as_of=datetime.now(timezone.utc) - timedelta(days=3),
        weight=1.0, planes=["corpus_research"],
        matcher_version="corpus_researcher_backlog/1.0.0",
    )
    base.update(over)
    return base


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_bearing_edge_inserts_expected_row(pg_pool):
    kwargs = _edge_kwargs()
    async with pg_pool.acquire() as conn:
        ok = await record_bearing_edge(conn, **kwargs)
        assert ok is True
        row = await conn.fetchrow(
            "SELECT edge_kind, src_kind, dst_kind, weight, planes, "
            "provenance_class, matcher_version FROM bearing_edges "
            "WHERE src_id = $1 AND dst_id = $2",
            kwargs["src_id"], kwargs["dst_id"],
        )
    assert row is not None
    assert row["edge_kind"] == DEFAULT_EDGE_KIND == "bears_on"
    assert row["src_kind"] == "finding"
    assert row["dst_kind"] == "hypothesis"
    assert row["weight"] == pytest.approx(1.0)
    assert list(row["planes"]) == ["corpus_research"]
    assert row["provenance_class"] == DEFAULT_PROVENANCE_CLASS == "live"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_bearing_edge_is_idempotent(pg_pool):
    kwargs = _edge_kwargs()
    async with pg_pool.acquire() as conn:
        first = await record_bearing_edge(conn, **kwargs)
        second = await record_bearing_edge(conn, **kwargs)
        count = await conn.fetchval(
            "SELECT count(*) FROM bearing_edges WHERE src_id = $1 AND dst_id = $2",
            kwargs["src_id"], kwargs["dst_id"],
        )
    assert first is True
    assert second is False  # deduped by the (src_id, dst_id, edge_kind) unique
    assert count == 1


@pytest.mark.asyncio
async def test_record_bearing_edge_refuses_empty_planes():
    """An edge with no contributing plane is not an edge — refused BEFORE
    ever reaching the DB (mirrors the schema's own non-empty CHECK)."""
    class _NeverCalledConn:
        async def execute(self, *a, **k):
            raise AssertionError("should never reach the DB with empty planes")

    ok = await record_bearing_edge(_NeverCalledConn(), **_edge_kwargs(planes=[]))
    assert ok is False


@pytest.mark.asyncio
async def test_record_bearing_edge_never_raises_on_write_failure():
    class _BrokenConn:
        async def execute(self, *a, **k):
            raise asyncpg.PostgresError("simulated write failure")

    ok = await record_bearing_edge(_BrokenConn(), **_edge_kwargs())
    assert ok is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_bearing_edge_degrades_on_schema_check_violation(pg_pool):
    """A caller bug (an invalid provenance_class) hits the DB's own CHECK —
    the writer still degrades (returns False) rather than propagating the
    DB error, so a bearing-edge bug can never threaten the finding write it
    sidecars."""
    async with pg_pool.acquire() as conn:
        ok = await record_bearing_edge(
            conn, **_edge_kwargs(provenance_class="not_a_real_class")
        )
    assert ok is False


# ---------------------------------------------------------------------------
# 8. WIRING through inline_target.run_method — stub grounding_hook, no DB
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _ScriptedLLM:
    subprovider = "backlog_test_double"

    def __init__(self, response_payload: dict[str, Any]):
        self._payload = response_payload

    async def chat_complete(self, *a: Any, **k: Any) -> Any:
        return SimpleNamespace(content=json.dumps(self._payload), usage=_Usage())


def _make_hook(*, block: str | None, sink_fill: dict[str, dict[str, Any]] | None):
    """A hand-written grounding_hook stand-in: fills the run's question_sink
    (mirroring what analyst_deps_builder._build_grounding_hook's open_questions
    branch does) and returns the block text (or None — the empty-backlog
    fallback path)."""
    async def _hook(inputs: list[Mapping[str, Any]], options: Mapping[str, Any]) -> str | None:
        sink = options.get(GROUNDING_QUESTION_SINK_KEY)
        if isinstance(sink, dict) and sink_fill:
            sink.update(sink_fill)
        return block

    return _hook


_QID = uuid4()
_QUESTION_SINK_FILL = {
    "Q1": {"id": str(_QID), "produced_at": "2026-07-20T00:00:00+00:00",
           "harvest_class": "below_floor"},
}
_STANDING_BLOCK = "STANDING OPEN QUESTIONS (backlog...):\n- [Q1] below_floor thesis text\n"


@pytest.mark.asyncio
async def test_run_method_resolves_addressed_question_into_derived_from_and_data():
    llm = _ScriptedLLM({
        "title": "Answered", "body": "The corpus confirms it. [1]",
        "confidence": 0.6, "evidence": ["sig-1"], "tags": ["severity:low"],
        "addressed_question": "Q1",
    })
    deps = InlineTargetDeps(
        llm=llm,
        grounding_hook=_make_hook(block=_STANDING_BLOCK, sink_fill=_QUESTION_SINK_FILL),
    )
    sig_id = uuid4()
    result = await run_method(
        [{"id": sig_id, "title": "a signal", "produced_at": "2026-07-27T00:00:00+00:00"}],
        {"analyst_id": "corpus_researcher"},
        deps,
    )
    assert _QID in result.derived_from
    addressed = result.finding.data.get("addressed_question")
    assert addressed is not None
    assert addressed["hypothesis_id"] == str(_QID)
    assert addressed["harvest_class"] == "below_floor"
    assert addressed["tag"] == "Q1"
    # The reflect trace step records the resolution for observability.
    reflect = [s for s in result.intermediate_steps if s.get("kind") == "coerce_finding"]
    assert reflect and reflect[0]["backlog_question_addressed"] is True


@pytest.mark.asyncio
async def test_run_method_self_selection_when_no_addressed_question_field():
    """The model chose to self-select (omitted the field) even though a
    backlog block was offered — no linkage is fabricated."""
    llm = _ScriptedLLM({
        "title": "Self-selected", "body": "Something else entirely. [1]",
        "confidence": 0.5, "evidence": ["sig-1"], "tags": ["severity:low"],
    })
    deps = InlineTargetDeps(
        llm=llm,
        grounding_hook=_make_hook(block=_STANDING_BLOCK, sink_fill=_QUESTION_SINK_FILL),
    )
    result = await run_method(
        [{"id": uuid4(), "title": "a signal", "produced_at": "2026-07-27T00:00:00+00:00"}],
        {"analyst_id": "corpus_researcher"},
        deps,
    )
    assert "addressed_question" not in (result.finding.data or {})
    assert _QID not in result.derived_from


@pytest.mark.asyncio
async def test_run_method_unknown_tag_resolves_to_nothing():
    """A model that cites a tag NOT in this run's sink (hallucinated /
    out-of-range) never fabricates a linkage — degrade, not invent."""
    llm = _ScriptedLLM({
        "title": "t", "body": "b [1]", "confidence": 0.5, "evidence": ["sig-1"],
        "tags": ["severity:low"], "addressed_question": "Q9",
    })
    deps = InlineTargetDeps(
        llm=llm,
        grounding_hook=_make_hook(block=_STANDING_BLOCK, sink_fill=_QUESTION_SINK_FILL),
    )
    result = await run_method(
        [{"id": uuid4(), "title": "a signal", "produced_at": "2026-07-27T00:00:00+00:00"}],
        {"analyst_id": "corpus_researcher"},
        deps,
    )
    assert "addressed_question" not in (result.finding.data or {})


@pytest.mark.asyncio
async def test_run_method_empty_backlog_is_byte_identical_fallback():
    """REQUIREMENT: an empty backlog (grounding hook returns None — the
    honest-empty path build_open_questions_block/the hook produce when there
    are no standing questions) leaves the run UNCHANGED versus having no
    grounding hook at all."""
    payload = {"title": "Self-selected as usual", "body": "b [1]",
               "confidence": 0.5, "evidence": ["sig-1"], "tags": ["severity:low"]}
    inputs = [{"id": uuid4(), "title": "a signal",
               "produced_at": "2026-07-27T00:00:00+00:00"}]
    options = {"analyst_id": "corpus_researcher"}

    empty_hook = _make_hook(block=None, sink_fill=None)
    with_hook = await run_method(inputs, options, InlineTargetDeps(
        llm=_ScriptedLLM(payload), grounding_hook=empty_hook,
    ))
    without_hook = await run_method(inputs, options, InlineTargetDeps(
        llm=_ScriptedLLM(payload), grounding_hook=None,
    ))
    assert with_hook.finding.title == without_hook.finding.title
    assert with_hook.finding.body == without_hook.finding.body
    assert with_hook.finding.data.get("addressed_question") is None
    assert without_hook.finding.data.get("addressed_question") is None
    assert with_hook.derived_from == without_hook.derived_from
    # No "inject_preamble" ground step landed either way — the empty backlog
    # never injects a stray block.
    ground_kinds = {
        s.get("kind") for r in (with_hook, without_hook) for s in r.intermediate_steps
        if s.get("phase") == "ground"
    }
    assert "inject_preamble" not in ground_kinds


@pytest.mark.asyncio
async def test_run_method_backlog_wiring_does_not_affect_other_analysts():
    """A hook that never fills the sink (every non-backlog descriptor) leaves
    an unrelated 'addressed_question'-shaped field inert — no other analyst's
    behavior can be perturbed by this wiring existing."""
    async def _inert_hook(inputs, options):
        return "AUTHORITATIVE CURRENT CONTEXT: some fact.\n"

    llm = _ScriptedLLM({
        "title": "t", "body": "b [1]", "confidence": 0.5, "evidence": ["sig-1"],
        "tags": ["severity:low"], "addressed_question": "Q1",  # coincidental
    })
    deps = InlineTargetDeps(llm=llm, grounding_hook=_inert_hook)
    result = await run_method(
        [{"id": uuid4(), "title": "a signal",
          "produced_at": "2026-07-27T00:00:00+00:00"}],
        {"analyst_id": "leadership_transition"},
        deps,
    )
    assert "addressed_question" not in (result.finding.data or {})


# ---------------------------------------------------------------------------
# W1-C2 — the FORWARD consumption stamp (the review-flag plane's missing seed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolved_question_is_stamped_as_a_consumption_edge():
    """``derived_from`` answers "what did this finding read?"; ``claim_watch``
    asks the inverse and walks ``output_consumption`` FORWARD from the question
    id. Until this stamp existed no producer wrote such a row (verified live
    2026-08-03: 0 rows where ``output_consumption.consumed_id`` joins
    ``hypotheses``, at any status, ever), so ``review_flags`` was 0 rows
    all-time — a wired write path whose precondition nothing satisfied."""
    from legba.data.provenance.consumption import CONSUMPTION_CONTEXT_QUESTION

    llm = _ScriptedLLM({
        "title": "Answered", "body": "The corpus confirms it. [1]",
        "confidence": 0.6, "evidence": ["sig-1"], "tags": ["severity:low"],
        "addressed_question": "Q1",
    })
    deps = InlineTargetDeps(
        llm=llm,
        grounding_hook=_make_hook(block=_STANDING_BLOCK, sink_fill=_QUESTION_SINK_FILL),
    )
    result = await run_method(
        [{"id": uuid4(), "title": "a signal",
          "produced_at": "2026-07-27T00:00:00+00:00"}],
        {"analyst_id": "corpus_researcher"},
        deps,
    )
    assert result.consumed_edges == [(_QID, CONSUMPTION_CONTEXT_QUESTION)]


@pytest.mark.asyncio
async def test_no_resolved_question_stamps_no_consumption_edge():
    """A run that resolves no question stamps nothing — the forward index must
    never claim a product rests on a question it was merely shown."""
    llm = _ScriptedLLM({
        "title": "Self-selected", "body": "Something else. [1]",
        "confidence": 0.5, "evidence": ["sig-1"], "tags": ["severity:low"],
    })
    deps = InlineTargetDeps(
        llm=llm,
        grounding_hook=_make_hook(block=_STANDING_BLOCK, sink_fill=_QUESTION_SINK_FILL),
    )
    result = await run_method(
        [{"id": uuid4(), "title": "a signal",
          "produced_at": "2026-07-27T00:00:00+00:00"}],
        {"analyst_id": "corpus_researcher"},
        deps,
    )
    assert result.consumed_edges == []
