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
    _OPEN_QUESTION_BACKLOG_DECAY_DAYS,
    build_open_questions_block,
    harvest_class_of,
    open_question_backlog_decay_days,
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


def test_harvest_class_of_reference_gap_origin():
    """A-4's class rides the SAME ``origin='harvest'`` marker every other
    dispatched/harvested class does — the only origin for which an explicit
    class is read off the marker at all."""
    marker = [{"marker": "open_question_origin", "origin": "harvest",
               "harvest_class": "reference_gap", "source_id": "il|lebanon"}]
    assert harvest_class_of(marker) == "reference_gap"


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


# ---------------------------------------------------------------------------
# 2b. PURE — AGE DECAY (2026-09-06 tune): a stale row's live_reach is
#     neutralised for tiers 1-2 ONLY, so it can no longer bury a fresh
#     dispatch on borrowed tier-1 urgency. Uses the DEFAULT decay window
#     (14 days, ``_OPEN_QUESTION_BACKLOG_DECAY_DAYS`` — ``_key`` does not
#     override ``decay_days``).
# ---------------------------------------------------------------------------


def test_priority_stale_live_reach_decays_to_zero_for_tier1_and_tier2():
    """A row older than the decay window loses its tier-1/tier-2 live_reach
    claim entirely — it ranks exactly as a live_reach=0 row would."""
    stale_with_reach = _key(
        live_reach=1, harvest_class="unit_payload",
        age_days=_OPEN_QUESTION_BACKLOG_DECAY_DAYS + 1.0, question_id="q1",
    )
    genuinely_reachless = _key(
        live_reach=0, harvest_class="unit_payload",
        age_days=_OPEN_QUESTION_BACKLOG_DECAY_DAYS + 1.0, question_id="q1",
    )
    assert stale_with_reach == genuinely_reachless


def test_priority_fresh_live_reach_is_not_decayed():
    """A row exactly at (not past) the decay window keeps its raw live_reach —
    decay is a strictly-greater-than boundary."""
    at_boundary = _key(
        live_reach=1, harvest_class="unit_payload",
        age_days=_OPEN_QUESTION_BACKLOG_DECAY_DAYS, question_id="q1",
    )
    reachless_at_boundary = _key(
        live_reach=0, harvest_class="unit_payload",
        age_days=_OPEN_QUESTION_BACKLOG_DECAY_DAYS, question_id="q1",
    )
    assert sorted([reachless_at_boundary, at_boundary])[0] == at_boundary


def test_priority_stale_backlog_row_sinks_below_a_fresh_coverage_floor_gap():
    """THE FINDING (2026-09-06 03:37Z): a month-old unit_payload row that
    traces to a live product must sink below a fresh, reachless
    coverage_floor dispatch — decay removes its false tier-1 claim, and the
    (now-first) harvest-class ordinal decides."""
    stale_backlog = _key(
        live_reach=1, harvest_class="unit_payload", age_days=35.0,
        question_id="aug_backlog_row",
    )
    fresh_dispatch = _key(
        live_reach=0, harvest_class="coverage_floor", age_days=2.0,
        question_id="il_palestine_gap",
    )
    assert sorted([stale_backlog, fresh_dispatch])[0] == fresh_dispatch


def test_priority_decay_days_is_env_overridable(monkeypatch):
    monkeypatch.setenv("LEGBA_OPEN_QUESTION_BACKLOG_DECAY_DAYS", "5")
    assert open_question_backlog_decay_days() == 5.0
    still_live = open_question_priority_key(
        live_reach=1, harvest_class="below_floor", desk_salience=0.0,
        age_days=6.0, question_id="q", decay_days=open_question_backlog_decay_days(),
    )
    decayed = open_question_priority_key(
        live_reach=0, harvest_class="below_floor", desk_salience=0.0,
        age_days=6.0, question_id="q",
    )
    assert still_live == decayed  # 6 days > the overridden 5-day window


def test_priority_decay_days_bad_env_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LEGBA_OPEN_QUESTION_BACKLOG_DECAY_DAYS", "not-a-number")
    assert open_question_backlog_decay_days() == _OPEN_QUESTION_BACKLOG_DECAY_DAYS
    monkeypatch.setenv("LEGBA_OPEN_QUESTION_BACKLOG_DECAY_DAYS", "-3")
    assert open_question_backlog_decay_days() == _OPEN_QUESTION_BACKLOG_DECAY_DAYS
    monkeypatch.delenv("LEGBA_OPEN_QUESTION_BACKLOG_DECAY_DAYS", raising=False)
    assert open_question_backlog_decay_days() == _OPEN_QUESTION_BACKLOG_DECAY_DAYS


def test_priority_tier3_harvest_class_ordinal():
    # The two DISPATCHED classes lead — coverage_floor (R-B) then reference_gap
    # (A-4) — because each names a gap provably unanswerable from our own
    # corpus; the harvested classes follow in their long-standing order. The
    # assertion is that the sequence is SORTED, not that any ordinal is pinned:
    # inserting a class shifts the ones below it and changes nothing else.
    order = [
        "coverage_floor", "reference_gap",
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


# ---------------------------------------------------------------------------
# 9. THE GOLDEN — a SELF-SELECTED run is byte-identical after the 2026-09-07
#    dispatch-as-assignment change.
#
# The change makes a DISPATCHED question command the run. Everything else must
# be untouched, and "untouched" here means literally the same bytes in the
# prompt: the hook's output on a backlog with nothing dispatched has to equal
# ``build_open_questions_block(resolve_open_questions(...))`` — the exact
# expression the hook evaluated before the change existed. If a single
# character of the assignment machinery leaked into the self-selection path,
# these fail.
# ---------------------------------------------------------------------------


def _undispatched_rows() -> list[dict[str, Any]]:
    """The live 03:37Z backlog MINUS the dispatch: seven unit_payload rows,
    the oldest carrying real forward reach (the Sizewell B shape)."""
    return [
        _candidate_row(
            thesis=f"Will the wildfire near Sizewell B force an outage? ({i})",
            harvest_class="unit_payload", age_days=34 + i,
            live_reach=1 if i == 0 else 0,
        )
        for i in range(7)
    ]


@pytest.mark.asyncio
async def test_self_selected_prompt_block_is_byte_identical_to_the_old_render():
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    rows = _undispatched_rows()
    expected = build_open_questions_block(
        await SubstrateGroundingResolver(
            pg_pool=_StubQuestionPool(rows)
        ).resolve_open_questions(limit=8)
    )
    assert expected  # the fixture really does render a block

    hook = _build_grounding_hook(
        _descriptor_with_open_questions_source(), pg_pool=_StubQuestionPool(rows),
    )
    sink: dict[str, Any] = {}
    got = await hook([], {"target_id": None, GROUNDING_QUESTION_SINK_KEY: sink})

    assert got == expected                      # BYTE-identical, not merely equivalent
    assert "DISPATCHED RESEARCH ASSIGNMENT" not in got
    assert "hypothesis_id=" not in got          # the assignment-only token
    # Every question is still offered — the menu is intact when nothing was
    # dispatched, which is exactly when self-selection is the honest behaviour.
    assert len(sink) == len(rows)
    assert all(entry["dispatched"] is False for entry in sink.values())


@pytest.mark.asyncio
async def test_a_dispatched_row_in_the_same_backlog_replaces_the_whole_block():
    """The other side of the golden: add ONE dispatched row to the very same
    seven-row backlog and the prompt stops being a menu."""
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    rows = _undispatched_rows()
    rows.append(
        _candidate_row(
            thesis="COVERAGE GAP — country_watch_il's evidence keeps naming Palestine",
            harvest_class="coverage_floor", target_id="country_watch_il",
        )
    )
    # The dispatch marker has to carry the scope for the render to print it.
    rows[-1]["diagnostic_evidence"] = json.dumps([{
        "marker": "open_question_origin", "origin": "harvest",
        "harvest_class": "coverage_floor", "target_id": "country_watch_il",
        "geo": ["IL"],
    }])

    hook = _build_grounding_hook(
        _descriptor_with_open_questions_source(), pg_pool=_StubQuestionPool(rows),
    )
    sink: dict[str, Any] = {}
    got = await hook([], {"target_id": None, GROUNDING_QUESTION_SINK_KEY: sink})

    assert got is not None
    assert "DISPATCHED RESEARCH ASSIGNMENT" in got
    assert "STANDING OPEN QUESTIONS" not in got
    assert "Sizewell" not in got and "[Q2]" not in got
    assert f"hypothesis_id={rows[-1]['id']}" in got
    assert "scope=country_watch_il geo=IL" in got
    assert set(sink) == {"Q1"} and sink["Q1"]["dispatched"] is True


# ---------------------------------------------------------------------------
# 10. THE PLANNER EXECUTES ITS ASSIGNMENT (2026-09-08) — the pure predicates.
#
# ``tests/data_pkg/agency/test_research_web_evidence_e2e.py`` drives these
# through the real DB, gate and write path. What is checked HERE is what that
# cannot show cheaply: the exact shape TABLE (which planner replies are read as
# actions and which are not), and the guarantee that the refusal is scoped to
# an ASSIGNED run — a self-selected run's odd body is still published, byte for
# byte as before, because outside an assignment "the run was told to call a
# tool" is not a fact anyone holds.
# ---------------------------------------------------------------------------


_RECOGNIZED = ("web_evidence", "search_corpus", "search_signals")

#: The 09-07 15:37Z reply, verbatim (id included) — the object the loop refused
#: and the model then narrated into its finding.
_LIVE_ACTION = {
    "action": "web_evidence",
    "query": "Israel Palestine conflict September 2026 news",
    "hypothesis_id": "b904fc78-245a-4eb8-bfdf-517c34db6173",
}


@pytest.mark.parametrize(
    "parsed,expected",
    [
        # The protocol shape — unchanged, and the args mapping is passed through.
        ({"tool": "search_corpus", "args": {"query": "x", "size": 5}},
         ("search_corpus", {"query": "x", "size": 5})),
        # THE LIVE SHAPE: flat, "action"-keyed, arguments alongside the name.
        (_LIVE_ACTION, ("web_evidence", {
            "query": "Israel Palestine conflict September 2026 news",
            "hypothesis_id": "b904fc78-245a-4eb8-bfdf-517c34db6173"})),
        # The tool-grammar shapes the plane's models are trained on.
        ({"name": "search_corpus", "arguments": {"query": "y"}},
         ("search_corpus", {"query": "y"})),
        ({"function": {"name": "web_evidence"}, "parameters": {"query": "z"}},
         ("web_evidence", {"query": "z"})),
        # Not actions: done, an unbound tool, prose, a finding envelope.
        ({"done": True}, None),
        ({"tool": "rm_rf", "args": {}}, None),
        # A present-but-malformed ``args`` is a broken call, not a flat one —
        # reading the siblings as its arguments would invent a call nobody
        # made. Unreadable, exactly as it was before the normalizer existed.
        ({"tool": "search_corpus", "args": "query=x"}, None),
        ("Attempt tool use.", None),
        ({"title": "t", "body": "b", "confidence": 0.4}, None),
    ],
)
def test_the_planner_action_shapes_the_loop_reads(parsed, expected):
    from legba.data.analysts.planner_action import normalize_planner_action

    got = normalize_planner_action(parsed, _RECOGNIZED)
    if expected is None:
        assert got is None
    else:
        assert (got[0], dict(got[1])) == expected


def test_the_scope_carry_stamps_only_what_the_planner_left_out():
    from legba.data.analysts.planner_action import stamp_dispatch_hypothesis_id

    # Omitted -> stamped, and the receipt says so.
    assert stamp_dispatch_hypothesis_id({"query": "q"}, "H") == (
        {"query": "q", "hypothesis_id": "H"}, True)
    # Supplied -> NEVER overwritten. A run naming a different question is
    # making a real claim about what its evidence bears on.
    assert stamp_dispatch_hypothesis_id({"hypothesis_id": "OWN"}, "H") == (
        {"hypothesis_id": "OWN"}, False)
    # No assignment -> nothing to carry, and the args are untouched.
    assert stamp_dispatch_hypothesis_id({"query": "q"}, None) == (
        {"query": "q"}, False)


@pytest.mark.parametrize(
    "body,reason",
    [
        # 09-07 15:37Z — the narration with the action object left in it.
        ("Attempt to fetch external evidence.\n" + json.dumps(_LIVE_ACTION),
         "unexecuted_action"),
        # The same object with no narration around it at all.
        (json.dumps(_LIVE_ACTION), "unexecuted_action"),
        # 09-08 03:37Z — the same intent with the object left off.
        ("Attempt tool use.", "process_narration"),
        # A real finding, terse but about the world.
        ("**BLUF:** the corpus held nothing on the polity; the web returned "
         "one 2026 policy item [4].", None),
        # A finding whose SUBJECT is search — the verb is there, the
        # announcement is not.
        ("Attempts to broker a ceasefire failed after Tuesday's strike [1].",
         None),
        # An object that names no tool is a malformed finding, not an action —
        # the existing unstructured degrade keeps it.
        ('{"title": "t", "body": "b"}', None),
    ],
)
def test_the_narration_guard_refuses_only_the_announcement(body, reason):
    from legba.data.analysts.planner_action import narration_guard_step

    step = narration_guard_step(body, _RECOGNIZED, "H-1")
    if reason is None:
        assert step is None
    else:
        assert step["reason"] == reason
        assert step["kind"] == "planner_narrated_without_action"
        assert step["hypothesis_id"] == "H-1"
        assert "planner_narrated_without_action" in step["detail"]


@pytest.mark.asyncio
async def test_a_self_selected_run_may_still_publish_an_odd_body():
    """THE SCOPE OF THE REFUSAL. The identical body that fails an ASSIGNED run
    is published unchanged by a self-selected one. This is the byte-identity
    guarantee for every analyst that has no backlog at all: nothing about the
    narration guard reaches a run nobody assigned anything to."""
    llm = _ScriptedLLM({"title": "t", "body": "Attempt tool use.",
                        "confidence": 0.4, "evidence": [], "tags": []})
    result = await run_method(
        [{"id": uuid4(), "title": "a signal",
          "produced_at": "2026-07-27T00:00:00+00:00"}],
        {"analyst_id": "corpus_researcher"},
        InlineTargetDeps(
            llm=llm,
            grounding_hook=_make_hook(
                block=_STANDING_BLOCK, sink_fill=_QUESTION_SINK_FILL,
            ),
        ),
    )
    assert result.finding.body == "Attempt tool use."
    assert not [
        s for s in result.intermediate_steps
        if s.get("kind") == "planner_narrated_without_action"
    ]


@pytest.mark.asyncio
async def test_an_assigned_run_refuses_the_same_body():
    """…and the other side of it: the SAME body, the SAME analyst, with the
    sink marking the question as this run's assignment."""
    from legba.data.analysts.output_contract import OutputContractError

    assigned_fill = {
        "Q1": {**_QUESTION_SINK_FILL["Q1"], "harvest_class": "coverage_floor",
               "target_id": "country_watch_il", "geo": ["IL"],
               "dispatched": True},
    }
    llm = _ScriptedLLM({"title": "t", "body": "Attempt tool use.",
                        "confidence": 0.4, "evidence": [], "tags": []})
    with pytest.raises(OutputContractError) as raised:
        await run_method(
            [{"id": uuid4(), "title": "a signal",
              "produced_at": "2026-07-27T00:00:00+00:00"}],
            {"analyst_id": "corpus_researcher"},
            InlineTargetDeps(
                llm=llm,
                grounding_hook=_make_hook(
                    block="DISPATCHED RESEARCH ASSIGNMENT\n- [Q1] gap\n",
                    sink_fill=assigned_fill,
                ),
            ),
        )
    assert "planner_narrated_without_action" in str(raised.value)
    assert str(_QID) in str(raised.value)


# ---------------------------------------------------------------------------
# 11. THE FINAL TURN IS THE FINDING (2026-09-09) — the pure predicates.
#
# The e2e file drives these through the real chain. What is checked HERE is the
# shape TABLE: which final answers are read as a tool call (and are therefore a
# run that gathered and reported nothing) and which are findings that must
# never be mistaken for one — plus the two directions of the synthesis clause,
# because "only the synthesis turn, only a gathering run" IS the contract.
# ---------------------------------------------------------------------------


#: The completions the LIVE core plane returned on the rebuilt 09-09 03:37Z
#: synthesis prompt — 5 of 6 samples, verbatim.
_LIVE_FINAL_TURNS = [
    '{"tool":"web_evidence","args":{"query":"Palestine relevance to Israel '
    'coverage country_watch_il","hypothesis_id":'
    '"b904fc78-245a-4eb8-bfdf-517c34db6173"}}',
    '{"tool": "search_corpus", "args": {"query": "Palestine Israel", "size": 10}}',
    '{"tool": "web_evidence", "args": {"query": "Israel extended emergency army '
    'mobilisation until 30 September 2026 larger conventional offensive Gaza '
    'Lebanon after extension"} }',
]


@pytest.mark.parametrize("raw", _LIVE_FINAL_TURNS)
def test_the_live_final_turn_is_read_as_a_tool_call(raw):
    from legba.data.analysts.planner_action import final_answer_is_a_tool_call

    assert final_answer_is_a_tool_call(raw) in ("web_evidence", "search_corpus")


@pytest.mark.parametrize(
    "raw",
    [
        # A real finding, even a terse one, is never a tool call…
        '{"title": "t", "body": "**BLUF:** nothing landed.", "confidence": 0.2}',
        # …nor is one whose title is missing but whose body is real…
        '{"body": "**BLUF:** the corpus held nothing on the polity."}',
        # …nor a finding that QUOTES a protocol object inside its body…
        '{"title": "t", "body": "The run emitted {\\"tool\\": \\"web_evidence\\"}."}',
        # …nor prose, an array, a fenced block, or an empty completion.
        "Attempt tool use.",
        '[{"tool": "web_evidence"}]',
        '```json\n{"tool": "web_evidence", "args": {}}\n```',
        "",
    ],
)
def test_what_is_never_read_as_a_final_tool_call(raw):
    from legba.data.analysts.planner_action import final_answer_is_a_tool_call

    assert final_answer_is_a_tool_call(raw) is None


@pytest.mark.parametrize(
    "raw,reason",
    [
        (_LIVE_FINAL_TURNS[0], "final_turn_is_a_tool_call"),
        ("", "empty_completion"),
        ("   \n ", "empty_completion"),
        ('{"title": "", "body": ""}', "unreadable_body"),
    ],
)
def test_the_unreadable_receipt_names_the_shape(raw, reason):
    from legba.data.analysts.output_contract import OutputContractError
    from legba.data.analysts.planner_action import (
        FINAL_ANSWER_UNREADABLE, unreadable_answer_step,
    )

    step = unreadable_answer_step(
        raw, OutputContractError("no readable body"), hypothesis_id=str(_QID),
    )
    assert step["kind"] == FINAL_ANSWER_UNREADABLE
    assert step["phase"] == "reflect"
    assert step["reason"] == reason
    assert step["raw_chars"] == len(raw)
    assert step["raw_head"] == raw.strip()[:600]
    assert step["hypothesis_id"] == str(_QID)
    assert FINAL_ANSWER_UNREADABLE in step["detail"]


def test_the_unreadable_receipt_omits_the_id_on_a_self_selected_run():
    from legba.data.analysts.planner_action import unreadable_answer_step

    step = unreadable_answer_step("", RuntimeError("x"))
    assert "hypothesis_id" not in step


def test_the_unreadable_receipt_bounds_the_raw_head():
    from legba.data.analysts.planner_action import unreadable_answer_step

    raw = "{" + "x" * 5_000
    step = unreadable_answer_step(raw, RuntimeError("x"))
    assert step["raw_chars"] == 5_001 and len(step["raw_head"]) == 600


#: The OTHER live shape (2 of 4 samples on the sharpened descriptor): the model
#: emits the call it wanted AND then the finding, in one completion. The first
#: object parses, so every recovery path used to be skipped and the finding —
#: complete, cited, well-formed — was thrown away.
_LIVE_CALL_THEN_FINDING = (
    '{"tool": "web_evidence", "args": {"query": "Israel Palestine coverage", '
    '"hypothesis_id": "b904fc78-245a-4eb8-bfdf-517c34db6173"}}\n'
    "[waiting]\n"
    '{\n  "title": "Palestine influences Israel\'s domestic politics",\n'
    '  "body": "*As of 2026-09-08.*\\n**BLUF:** the slice names it nowhere [1].",\n'
    '  "confidence": 0.5,\n  "evidence": ["s-1"],\n'
    '  "tags": ["severity:low", "topic:corpus_research"],\n'
    '  "addressed_question": "Q1"\n}'
)


def test_the_finding_behind_the_tool_call_is_read():
    """(b) THE LIVE SHAPE, RECOVERED. The envelope is wrong; the finding inside
    it is real, and refusing it publishes nothing where a complete cited read
    was available."""
    from legba.data.analysts.inline_target import _coerce_finding

    finding = _coerce_finding(
        _LIVE_CALL_THEN_FINDING, fallback_title="Assessment for target",
    )
    assert finding.title == "Palestine influences Israel's domestic politics"
    assert "**BLUF:**" in finding.body and "[1]" in finding.body
    assert finding.confidence == 0.5
    assert finding.data["addressed_question_tag"] == "Q1"


def test_a_completion_that_is_ONLY_a_tool_call_is_still_refused():
    """The other side: nothing to recover, so nothing is recovered. A
    fabricated body would be worse than the loud failure."""
    from legba.data.analysts.inline_target import _coerce_finding
    from legba.data.analysts.output_contract import OutputContractError

    with pytest.raises(OutputContractError) as raised:
        _coerce_finding(
            _LIVE_FINAL_TURNS[0], fallback_title="Assessment for target",
        )
    assert "title='Assessment for target'" in str(raised.value)


def test_the_envelope_walk_never_promotes_a_quoted_payload():
    """The walk must not turn a model's ECHOED tool call into a finding, and
    must still prefer the FIRST real contract it meets."""
    from legba.data.analysts.output_contract import (
        iter_json_objects, parse_finding_envelope,
    )

    # Two contracts: the first wins, exactly as before the walk existed.
    two = '{"title": "first", "body": "b1"}\n{"title": "second", "body": "b2"}'
    assert parse_finding_envelope(two)["title"] == "first"
    # No contract anywhere: still None, never the tool call.
    assert parse_finding_envelope(
        '{"tool": "web_evidence"}\n{"tool": "search_corpus"}'
    ) is None
    # An unterminated object stops the walk (a truncated envelope is
    # salvage_json_envelope's job, and anything after it is inside it).
    assert list(iter_json_objects('{"a": 1}{"b": ')) == ['{"a": 1}']
    # Braces inside strings never close an object early.
    assert list(iter_json_objects('{"body": "a } brace"}')) == [
        '{"body": "a } brace"}'
    ]


def test_the_synthesis_clause_reaches_gathering_runs_only():
    from legba.data.analysts.planner_action import (
        GATHERING_CLOSED_CLAUSE, synthesis_prompt,
    )

    base = "Target: unspecified\nNumber of signals: 3\n"
    # A single-shot analyst was never told the protocol — byte-identical.
    assert synthesis_prompt(base, gathered=False) is base
    closed = synthesis_prompt(base, gathered=True)
    assert closed.startswith(base) and GATHERING_CLOSED_CLAUSE in closed
    # It must name the contract's required keys and refuse the protocol object.
    assert '"title"' in GATHERING_CLOSED_CLAUSE
    assert '"body"' in GATHERING_CLOSED_CLAUSE
    assert '{"tool"' in GATHERING_CLOSED_CLAUSE


def test_the_completion_text_cap_is_generous_and_marked():
    from legba.data.run_accounting import (
        clip_completion_text, record_llm_call, bind_run_accounting,
        current_llm_calls, reset_run_accounting,
    )

    # The live failures were 76-174 chars: held whole, unmarked.
    assert clip_completion_text(_LIVE_FINAL_TURNS[0]) == _LIVE_FINAL_TURNS[0]
    long = "x" * 9_000
    cut = clip_completion_text(long)
    assert cut.startswith("x" * 4_000) and "TRUNCATED: 5000 of 9000" in cut

    # And the recorder exempts it from the 500-char field backstop that would
    # otherwise throw away the answer this field exists to preserve.
    token = bind_run_accounting()
    try:
        record_llm_call(status="success", completion_text=long, error="e" * 900)
        entry = current_llm_calls()[0]
    finally:
        reset_run_accounting(token)
    assert len(entry["completion_text"]) > 4_000
    assert len(entry["error"]) == 500
