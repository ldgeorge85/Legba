# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PIECE C — competing_hypotheses (ACH) meta-analyst kind.

Covers (per the locked D3 spec):
  * the ACH kind with a STUBBED LLM produces >= 2 COMPETING hypotheses each with
    a non-empty counter-thesis;
  * a populated evidence x hypothesis MATRIX with per-item DIAGNOSTICITY;
  * an INTEGER evidence balance per hypothesis;
  * an over-threshold hypothesis TRANSITIONS (active -> confirmed / refuted);
  * current-fact reads use ``superseded_by IS NULL`` (a superseded fact never
    enters the evidence base);
  * degrade-not-drop: a raising LLM still builds the matrix off the deterministic
    fallback hypothesis set (never an exception, never 0 rows).

The matrix math (consistency + diagnosticity + integer balance + transitions) is
covered as pure units; the side-write + current-fact read are covered live over
the migrated test pg_pool. NO live model — the generation LLM is a canned stub.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.competing_hypotheses import (
    ACHDeps,
    CONFIRM_K,
    MIN_HYPOTHESES,
    REFUTE_K,
    RESOLUTION_MIN_AGE_DAYS,
    _coerce_cell_scores,
    _coerce_hypotheses,
    _deterministic_hypotheses,
    _diagnosticity,
    _read_evidence_for_topic,
    _resolve_hypotheses_against_subsequent_facts,
    _resolve_topic_entities,
    _score_consistency,
    _score_consistency_matrix_llm,
    _thesis_direction,
    _thesis_is_status_quo,
    build_ach_matrix,
    run_method,
)
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext, FactPayload, write_fact


# ---------------------------------------------------------------------------
# Stub LLM (canned competing-hypotheses JSON) — NO live model in unit tests.
# ---------------------------------------------------------------------------


class _CannedHypothesesLLM:
    subprovider = "stub"

    def __init__(self, hypotheses: list[dict[str, str]], *, pt: int = 17, ct: int = 23):
        self._obj = {"hypotheses": hypotheses}
        self._pt = pt
        self._ct = ct
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"system": system, "max_tokens": max_tokens})
        obj, pt, ct = self._obj, self._pt, self._ct

        class _Usage:
            prompt_tokens = pt
            completion_tokens = ct
            reasoning_tokens = 0

        class _Response:
            content = json.dumps(obj)
            usage = _Usage()

        return _Response()


class _RaisingLLM:
    subprovider = "raising"

    async def chat_complete(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("synthetic generation failure")


# ---------------------------------------------------------------------------
# Pure-unit: ACH matrix (consistency + diagnosticity + integer balance)
# ---------------------------------------------------------------------------


def test_deterministic_fallback_yields_competing_set_with_counter_theses():
    hyps = _deterministic_hypotheses("Strait of Hormuz crisis")
    assert len(hyps) >= MIN_HYPOTHESES
    for h in hyps:
        assert h["thesis"].strip()
        assert h["counter_thesis"].strip()  # MANDATORY counter-thesis
    # The hypotheses are genuinely DIVERGENT (escalate vs de-escalate vs status).
    theses = " ".join(h["thesis"].lower() for h in hyps)
    assert "escalate" in theses and "de-escalate" in theses


def test_coerce_hypotheses_enforces_min_and_mandatory_counter():
    # < MIN_HYPOTHESES → rejected (caller falls back to the deterministic set).
    assert _coerce_hypotheses({"hypotheses": [{"thesis": "only one"}]}, topic_name="T") == []
    # Missing counter_thesis → synthesized, never left empty (the ACH invariant).
    out = _coerce_hypotheses(
        {"hypotheses": [{"thesis": "A happens"}, {"thesis": "B happens"}]},
        topic_name="T",
    )
    assert len(out) == 2
    assert all(h["counter_thesis"].strip() for h in out)


def test_diagnosticity_zero_when_evidence_fits_all_hypotheses():
    # Identical consistency across hypotheses → non-diagnostic → weight 0.
    assert _diagnosticity([1, 1, 1]) == 0.0
    # Maximal spread (+2 vs -2) → fully diagnostic (1.0 over the 4-wide scale).
    assert _diagnosticity([2, -2]) == 1.0
    # Partial spread → between.
    assert 0.0 < _diagnosticity([2, 0]) < 1.0


def test_score_consistency_directional():
    # Escalation evidence supports an "escalate" thesis, contradicts "de-escalate".
    esc_text = "missile strike and clash, sanctions and threat"
    assert _score_consistency(esc_text, 0, "X will escalate") > 0
    assert _score_consistency(esc_text, 0, "X will de-escalate") < 0
    # A hostile signed nexus (polarity -1) is escalation evidence.
    assert _score_consistency("A supplies B", -1, "X will escalate") > 0


def test_thesis_direction_and_status_quo_classification():
    # DQ-H2b: direction sign + the status-quo vs undirected split the resolver
    # uses to decide grade-vs-abstain.
    assert _thesis_direction("Iran will escalate over 14 days") == 1
    assert _thesis_direction("the conflict will de-escalate and calm") == -1
    # "de-escalate" contains "escalate" — must classify as de-escalation.
    assert _thesis_direction("a de-escalation is likely") == -1
    # Status-quo and genuinely-undirected theses BOTH have direction 0 …
    assert _thesis_direction("X remains at its current intensity (status quo)") == 0
    assert _thesis_direction("the situation is complicated and notable") == 0
    # … but only the status-quo one is a GRADEABLE claim.
    assert _thesis_is_status_quo("X remains at its current intensity (status quo)") is True
    assert _thesis_is_status_quo("the border stays unchanged") is True
    assert _thesis_is_status_quo("the situation is complicated and notable") is False
    assert _thesis_is_status_quo("Iran will escalate") is False


def test_build_ach_matrix_shape_and_integer_balance():
    hyps = [
        {"thesis": "Conflict will escalate", "counter_thesis": "it won't"},
        {"thesis": "Conflict will de-escalate", "counter_thesis": "it won't"},
    ]
    eid1, eid2 = uuid4(), uuid4()
    evidence = [
        {"id": eid1, "kind": "fact", "text": "missile strike, clash, threat", "polarity": -1},
        {"id": eid2, "kind": "nexus", "text": "ceasefire talks, peace deal", "polarity": 1},
    ]
    ach = build_ach_matrix(hyps, evidence)
    # The matrix has one row per evidence item, one cell per hypothesis.
    assert len(ach["matrix"]) == 2
    assert all(len(row["cells"]) == 2 for row in ach["matrix"])
    # Diagnosticity present per item; the divergent evidence is diagnostic (>0).
    assert all("diagnosticity" in row for row in ach["matrix"])
    assert any(row["diagnosticity"] > 0 for row in ach["matrix"])
    # The balance is a list of INTEGERS, one per hypothesis.
    assert len(ach["balance"]) == 2
    assert all(isinstance(b, int) for b in ach["balance"])
    # Escalation evidence + de-escalation evidence should pull the two hypotheses
    # in opposite directions → a lead index exists.
    assert ach["lead_index"] in (0, 1)


# ---------------------------------------------------------------------------
# Pure-unit: LLM-per-cell consistency scorer (part a)
# ---------------------------------------------------------------------------


class _CannedCellLLM:
    """Stub LLM that returns a canned ACH consistency-matrix JSON."""

    subprovider = "stub"

    def __init__(self, cells: list[dict[str, Any]] | None, *, raw: str | None = None):
        self._obj = {"cells": cells} if cells is not None else None
        self._raw = raw
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):
        self.calls.append({"system": system, "temperature": temperature})
        content = self._raw if self._raw is not None else json.dumps(self._obj)

        class _Usage:
            prompt_tokens = 11
            completion_tokens = 13
            reasoning_tokens = 0

        class _Response:
            pass

        r = _Response()
        r.content = content
        r.usage = _Usage()
        return r


class _PausedBudget:
    async def check_envelope(self) -> str:
        return "exhausted"


def test_coerce_cell_scores_maps_labels_and_drops_out_of_range():
    obj = {"cells": [
        {"e": 0, "h": 0, "label": "CC"},   # +2
        {"e": 0, "h": 1, "label": "II"},   # -2
        {"e": 1, "h": 0, "label": "n"},    # 0 (case-insensitive)
        {"e": 9, "h": 0, "label": "C"},    # out of range → dropped
        {"e": 0, "h": 0, "label": "ZZZ"},  # unknown label → dropped
    ]}
    out = _coerce_cell_scores(obj, n_evidence=2, n_hyp=2)
    assert out[(0, 0)] == 2
    assert out[(0, 1)] == -2
    assert out[(1, 0)] == 0
    assert (9, 0) not in out


@pytest.mark.asyncio
async def test_llm_matrix_scorer_overrides_lexical_in_build():
    """The LLM cell scores OVERRIDE the lexical scorer at the build call site;
    omitted cells fall back to lexical."""
    hyps = [{"thesis": "A", "counter_thesis": "x"}, {"thesis": "B", "counter_thesis": "y"}]
    evidence = [{"id": uuid4(), "kind": "fact", "text": "neutral text", "polarity": 0}]
    # The lexical scorer would return 0 for both (undirected theses); the LLM
    # forces a divergent, diagnostic row.
    llm = _CannedCellLLM([
        {"e": 0, "h": 0, "label": "CC"},
        {"e": 0, "h": 1, "label": "II"},
    ])
    deps = ACHDeps(llm=llm)
    cells, usage = await _score_consistency_matrix_llm(deps, hypotheses=hyps, evidence=evidence)
    assert cells == {(0, 0): 2, (0, 1): -2}
    assert usage["prompt_tokens"] == 11
    ach = build_ach_matrix(hyps, evidence, cell_scores=cells)
    # The LLM forced a fully-diagnostic divergent row (lexical would be flat 0).
    assert ach["matrix"][0]["cells"][0]["consistency"] == 2
    assert ach["matrix"][0]["cells"][1]["consistency"] == -2
    assert ach["matrix"][0]["diagnosticity"] == 1.0


@pytest.mark.asyncio
async def test_llm_matrix_scorer_falls_back_when_budget_exhausted():
    """A non-ok budget envelope must SKIP the LLM call and return None so the
    caller scores lexically (the budget-exhausted fallback)."""
    hyps = [{"thesis": "A", "counter_thesis": "x"}, {"thesis": "B", "counter_thesis": "y"}]
    evidence = [{"id": uuid4(), "kind": "fact", "text": "missile strike", "polarity": 0}]
    llm = _CannedCellLLM([{"e": 0, "h": 0, "label": "CC"}])
    deps = ACHDeps(llm=llm, budget=_PausedBudget())
    cells, usage = await _score_consistency_matrix_llm(deps, hypotheses=hyps, evidence=evidence)
    assert cells is None
    assert not llm.calls, "the LLM must not be called when the budget is exhausted"
    # build_ach_matrix with cell_scores=None scores purely lexically (no crash).
    ach = build_ach_matrix(hyps, evidence, cell_scores=None)
    assert len(ach["matrix"]) == 1


@pytest.mark.asyncio
async def test_llm_matrix_scorer_falls_back_on_unparsable_output():
    hyps = [{"thesis": "A", "counter_thesis": "x"}, {"thesis": "B", "counter_thesis": "y"}]
    evidence = [{"id": uuid4(), "kind": "fact", "text": "x", "polarity": 0}]
    llm = _CannedCellLLM(None, raw="not json at all")
    deps = ACHDeps(llm=llm)
    cells, _ = await _score_consistency_matrix_llm(deps, hypotheses=hyps, evidence=evidence)
    assert cells is None


# ---------------------------------------------------------------------------
# Live-DB fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


class _Deps:
    def __init__(self, pool, *, llm=None, budget=None):
        self.pg_pool = pool
        self.nats_publish = None
        self.llm = llm
        self.budget = budget


async def _seed_situation(conn, *, name: str, intensity: float, derived_from=None, status="active"):
    sid = uuid4()
    await conn.execute(
        """
        INSERT INTO situations
            (id, data, name, status, category, last_event_at, event_count,
             intensity_score, analyst_id, produced_at, derived_from, schema_uri)
        VALUES ($1, '{}'::jsonb, $2, $3, 'conflict', NOW(), 5, $4,
                'situation_clustering', NOW(), $5,
                'iglu:legba/situation/jsonschema/2-0-0')
        """,
        sid, name, status, intensity, list(derived_from or []),
    )
    return sid


async def _seed_finding(conn, *, title: str, body: str = "b"):
    fid = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, confidence, analyst_id, analyst_version,
             produced_at, derived_from, schema_uri)
        VALUES ($1, 'finding', $2, $3, 0.8, 'country_assessor', 'v1', NOW(),
                '{}'::uuid[], 'iglu:legba/finding/jsonschema/1-0-0')
        """,
        fid, title, body,
    )
    return fid


# ---------------------------------------------------------------------------
# The ACH kind with a STUBBED LLM: >= 2 competing hypotheses + counter-theses +
# a populated matrix + integer balance, side-written via write_hypothesis.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ach_writes_competing_hypotheses_with_matrix(pg_pool):
    analyst_id = f"ach_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        f1 = await _seed_finding(conn, title="missile strike near port, clash escalates")
        f2 = await _seed_finding(conn, title="ceasefire talks announced, peace deal floated")
        sid = await _seed_situation(
            conn, name=f"ACHtopic{uuid4().hex[:6]}", intensity=5.0,
            derived_from=[f1, f2],
        )

    llm = _CannedHypothesesLLM([
        {"thesis": "The conflict will escalate over the next 14 days",
         "counter_thesis": "Diplomatic pressure de-escalates it"},
        {"thesis": "The conflict will de-escalate over the next 14 days",
         "counter_thesis": "Hardliners keep escalating it"},
    ])
    deps = ACHDeps(llm=llm, pg_pool=pg_pool, max_topics=20)

    result = await run_method(
        inputs=[], options={"analyst_id": analyst_id, "run_id": str(uuid4())}, deps=deps,
    )
    data = result.finding.data
    assert data["hypotheses_written"] >= MIN_HYPOTHESES, data
    assert result.usage["prompt_tokens"] > 0  # the LLM was actually called
    assert llm.calls

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT thesis, counter_thesis, evidence_balance, status, "
            "diagnostic_evidence FROM hypotheses "
            "WHERE analyst_id = $1 AND situation_id = $2 ORDER BY thesis",
            analyst_id, sid,
        )
    # >= 2 COMPETING hypotheses for the topic.
    assert len(rows) >= MIN_HYPOTHESES
    for r in rows:
        assert r["thesis"].strip()
        assert r["counter_thesis"].strip(), "every hypothesis carries a counter-thesis"
        assert isinstance(r["evidence_balance"], int)
        # The full ACH structure rides in diagnostic_evidence.
        de = json.loads(r["diagnostic_evidence"]) if isinstance(r["diagnostic_evidence"], str) else r["diagnostic_evidence"]
        entry = de[0]
        assert entry["ach"] is True
        assert "matrix" in entry and entry["matrix"], "the matrix is populated"
        # Each matrix row carries diagnosticity.
        assert all("diagnosticity" in m for m in entry["matrix"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ach_transitions_one_hypothesis_past_threshold(pg_pool):
    """An evidence base that strongly + diagnostically favours ONE direction must
    push that hypothesis past ±K so it auto-transitions (confirmed or refuted)."""
    analyst_id = f"acht_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        # Three findings, all unambiguous ESCALATION evidence — diagnostic for the
        # escalate-vs-de-escalate competition.
        fids = []
        for txt in (
            "missile strike kills dozens, clash escalates",
            "new sanctions and military threat, deploy forces",
            "armed conflict spreads, invasion raid reported",
        ):
            fids.append(await _seed_finding(conn, title=txt))
        sid = await _seed_situation(
            conn, name=f"Escalate{uuid4().hex[:6]}", intensity=8.0, derived_from=fids,
        )

    llm = _CannedHypothesesLLM([
        {"thesis": "It will escalate over the next 14 days", "counter_thesis": "it won't"},
        {"thesis": "It will de-escalate over the next 14 days", "counter_thesis": "it won't"},
    ])
    deps = ACHDeps(llm=llm, pg_pool=pg_pool, max_topics=20)
    result = await run_method(
        inputs=[], options={"analyst_id": analyst_id, "run_id": str(uuid4())}, deps=deps,
    )
    # At least one hypothesis transitioned past the threshold.
    transitions = result.finding.data["confirmed"] + result.finding.data["refuted"]
    assert transitions >= 1, result.finding.data

    async with pg_pool.acquire() as conn:
        statuses = await conn.fetch(
            "SELECT status, evidence_balance FROM hypotheses "
            "WHERE analyst_id = $1 AND situation_id = $2",
            analyst_id, sid,
        )
    non_active = [r for r in statuses if r["status"] != "active"]
    assert non_active, "a strongly-favoured hypothesis must transition"
    for r in non_active:
        if r["status"] == "confirmed":
            assert r["evidence_balance"] >= CONFIRM_K
        elif r["status"] == "refuted":
            assert r["evidence_balance"] <= -REFUTE_K


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ach_evidence_uses_current_facts_only(pg_pool):
    """Current-fact read discipline: a SUPERSEDED fact (superseded_by set) must
    NOT enter the evidence base; only the current (open) fact does."""
    analyst_id = f"achf_{uuid4().hex[:8]}"
    topic = f"Hormuz{uuid4().hex[:6]}"
    async with pg_pool.acquire() as conn:
        ctx = AnalystContext(analyst_id=analyst_id, analyst_version="v1", run_id=uuid4())
        # An OPEN (current) fact about the topic (distinct predicate so the
        # write-time supersession on (subject,predicate) doesn't touch it).
        cur, _ = await write_fact(
            conn, analyst_ctx=ctx,
            payload=FactPayload(
                subject=topic, predicate="current_status", value="missile strike escalates",
            ),
            derived_from=[],
        )
        # A second fact under a DIFFERENT predicate, then manually CLOSE it
        # (superseded_by + valid_until) — it must be ignored by the current read.
        old, _ = await write_fact(
            conn, analyst_ctx=ctx,
            payload=FactPayload(
                subject=topic, predicate="stale_status", value="ceasefire holds (stale)",
            ),
            derived_from=[],
        )
        await conn.execute(
            "UPDATE facts SET superseded_by = $2, valid_until = NOW() WHERE id = $1",
            old.id, cur.id,
        )
        sid = await _seed_situation(conn, name=topic, intensity=6.0)

    llm = _CannedHypothesesLLM([
        {"thesis": "It will escalate", "counter_thesis": "it won't"},
        {"thesis": "It will de-escalate", "counter_thesis": "it won't"},
    ])
    deps = ACHDeps(llm=llm, pg_pool=pg_pool, max_topics=20)
    result = await run_method(
        inputs=[], options={"analyst_id": analyst_id, "run_id": str(uuid4())}, deps=deps,
    )
    assert result.finding.data["hypotheses_written"] >= MIN_HYPOTHESES

    # The matrix must reference the CURRENT fact id, never the superseded one.
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT diagnostic_evidence FROM hypotheses "
            "WHERE analyst_id = $1 AND situation_id = $2 LIMIT 1",
            analyst_id, sid,
        )
    de = json.loads(row["diagnostic_evidence"]) if isinstance(row["diagnostic_evidence"], str) else row["diagnostic_evidence"]
    ev_ids = {e.get("id") for e in de[0]["evidence"]}
    assert str(cur.id) in ev_ids, "the current fact must be in the evidence base"
    assert str(old.id) not in ev_ids, "a superseded fact must be excluded (superseded_by IS NULL)"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ach_degrades_not_drops_on_llm_failure(pg_pool):
    """A raising LLM must NOT raise + must NOT yield 0 rows — the matrix is built
    off the deterministic fallback hypothesis set and the rows still land."""
    analyst_id = f"achd_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        f1 = await _seed_finding(conn, title="clash and strike escalate")
        sid = await _seed_situation(
            conn, name=f"Degrade{uuid4().hex[:6]}", intensity=5.0, derived_from=[f1],
        )
    deps = ACHDeps(llm=_RaisingLLM(), pg_pool=pg_pool, max_topics=20)
    result = await run_method(
        inputs=[], options={"analyst_id": analyst_id, "run_id": str(uuid4())}, deps=deps,
    )
    data = result.finding.data
    assert data["degraded"] >= 1, "a raising LLM must degrade, not raise"
    assert data["hypotheses_written"] >= MIN_HYPOTHESES, "the fallback set still lands rows"

    async with pg_pool.acquire() as conn:
        cnt = await conn.fetchval(
            "SELECT count(*) FROM hypotheses WHERE analyst_id = $1 AND situation_id = $2",
            analyst_id, sid,
        )
    assert cnt >= MIN_HYPOTHESES


# ---------------------------------------------------------------------------
# Part (b): RESOLVED-entity-scoped evidence reads (not LIKE '%name%')
# ---------------------------------------------------------------------------


async def _seed_entity(conn, *, canonical_name: str, entity_class: str = "country"):
    eid = uuid4()
    await conn.execute(
        """
        INSERT INTO entity_profiles
            (id, data, canonical_name, entity_type, entity_class, version,
             completeness_score, analyst_id, produced_at, derived_from, schema_uri)
        VALUES ($1, '{}'::jsonb, $2, 'entity', $3, 1, 0.9,
                'entity_resolution', NOW(), '{}'::uuid[],
                'iglu:legba/entity_profile/jsonschema/2-0-0')
        ON CONFLICT DO NOTHING
        """,
        eid, canonical_name, entity_class,
    )
    return eid


async def _seed_fact(conn, *, subject: str, predicate: str, value: str,
                     produced_at_sql: str = "NOW()"):
    fid = uuid4()
    await conn.execute(
        f"""
        INSERT INTO facts
            (id, subject, predicate, value, confidence, source_type,
             analyst_id, produced_at, derived_from, schema_uri)
        VALUES ($1, $2, $3, $4, 0.9, 'agent', 'fact_extractor', {produced_at_sql},
                '{{}}'::uuid[], 'iglu:legba/fact/jsonschema/2-0-0')
        """,
        fid, subject, predicate, value,
    )
    return fid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_topic_entities_returns_canonical_set(pg_pool):
    """A topic name resolves to the entity_profiles canonical set + itself;
    the lookup distinguishes by the composite key (no false substring merge)."""
    topic = f"Iran{uuid4().hex[:6]}"
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, canonical_name=topic, entity_class="country")
        names = await _resolve_topic_entities(conn, name=topic)
    assert topic.lower() in names  # the topic itself is always in the set
    assert all(n == n.lower() for n in names)  # lower-cased canonical set


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ach_evidence_scoped_by_resolved_entity_not_substring(pg_pool):
    """A fact whose subject merely CONTAINS the topic as a substring (but is a
    DIFFERENT entity) must NOT enter the evidence base — the old LIKE '%name%'
    would have wrongly pulled it; exact resolved-entity membership excludes it."""
    topic = f"Iran{uuid4().hex[:6]}"
    decoy_subject = f"{topic} American Society of Ohio"  # substring match, wrong entity
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, canonical_name=topic, entity_class="country")
        # An ON-topic fact (subject == the resolved entity).
        on_topic = await _seed_fact(
            conn, subject=topic, predicate="status", value="missile strike escalates",
        )
        # A DECOY fact whose subject contains the topic as a substring only.
        decoy = await _seed_fact(
            conn, subject=decoy_subject, predicate="status", value="hosts a cultural gala",
        )
        sid = await _seed_situation(conn, name=topic, intensity=6.0)

    llm = _CannedHypothesesLLM([
        {"thesis": "It will escalate", "counter_thesis": "it won't"},
        {"thesis": "It will de-escalate", "counter_thesis": "it won't"},
    ])
    deps = ACHDeps(llm=llm, pg_pool=pg_pool, max_topics=20)
    result = await run_method(
        inputs=[], options={"analyst_id": f"achs_{uuid4().hex[:8]}", "run_id": str(uuid4())},
        deps=deps,
    )
    assert result.finding.data["hypotheses_written"] >= MIN_HYPOTHESES

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT diagnostic_evidence FROM hypotheses WHERE situation_id = $1 LIMIT 1",
            sid,
        )
    de = json.loads(row["diagnostic_evidence"]) if isinstance(row["diagnostic_evidence"], str) else row["diagnostic_evidence"]
    ev_ids = {e.get("id") for e in de[0]["evidence"]}
    assert str(on_topic) in ev_ids, "the resolved-entity fact must be in the evidence base"
    assert str(decoy) not in ev_ids, "a substring-only decoy fact must be excluded"


# ---------------------------------------------------------------------------
# Part (c): EXOGENOUS resolution — resolved_outcome vs SUBSEQUENT facts
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exogenous_resolution_grades_against_subsequent_facts(pg_pool):
    """An old escalation hypothesis is auto-resolved against facts produced AFTER
    it: subsequent escalation facts => resolved_outcome=1 (came true); the
    resolution is EXOGENOUS (stamped resolved_by='subsequent_facts'), never the
    hypothesis's own evidence_balance."""
    analyst_id = f"achx_{uuid4().hex[:8]}"
    topic = f"Strait{uuid4().hex[:6]}"
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, canonical_name=topic, entity_class="location")
        sid = await _seed_situation(conn, name=topic, intensity=7.0)
        # An OLD, unresolved escalation hypothesis (older than the min age).
        ctx = AnalystContext(analyst_id=analyst_id, analyst_version="v1", run_id=uuid4())
        from legba.data.provenance import HypothesisPayload, write_hypothesis
        out, _ = await write_hypothesis(
            conn, analyst_ctx=ctx,
            payload=HypothesisPayload(
                thesis=f"{topic} will escalate over the next 14 days",
                counter_thesis="it will de-escalate",
                situation_id=sid, evidence_balance=3, status="confirmed",
            ),
            derived_from=[],
        )
        hyp_id = out.id
        # Backdate it so it is older than RESOLUTION_MIN_AGE_DAYS.
        await conn.execute(
            "UPDATE hypotheses SET produced_at = NOW() - make_interval(days => $2) "
            "WHERE id = $1",
            hyp_id, RESOLUTION_MIN_AGE_DAYS + 3,
        )
        # SUBSEQUENT facts (produced AFTER the hypothesis) — strong escalation.
        for v in ("missile strike kills dozens", "armed clash and sanctions deploy"):
            await _seed_fact(conn, subject=topic, predicate="status", value=v)

        resolved, scanned = await _resolve_hypotheses_against_subsequent_facts(
            conn, now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        assert resolved >= 1, "the old hypothesis with subsequent facts must resolve"

        row = await conn.fetchrow(
            "SELECT resolved_outcome, resolved_by, resolved_at FROM hypotheses WHERE id = $1",
            hyp_id,
        )
    assert row["resolved_outcome"] == 1, "subsequent escalation => escalation thesis came true"
    assert row["resolved_by"] == "subsequent_facts", "resolution is EXOGENOUS, not evidence_balance"
    assert row["resolved_at"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exogenous_resolution_abstains_on_undirected_thesis(pg_pool):
    """DQ-H2b: an UNDIRECTED thesis (no escalation/de-escalation/status-quo
    claim) is ABSTAINED, never auto-graded. It is fully resolvable in every
    OTHER respect (situation + entities + subsequent facts all present), so the
    OLD code would have minted resolved_outcome=1 from quiet facts — proving the
    NULL is the abstain, not a missing-gate skip."""
    from legba.data.provenance import HypothesisPayload, write_hypothesis

    topic = f"Region{uuid4().hex[:6]}"
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, canonical_name=topic, entity_class="location")
        sid = await _seed_situation(conn, name=topic, intensity=5.0)
        ctx = AnalystContext(
            analyst_id=f"achund_{uuid4().hex[:8]}", analyst_version="v1", run_id=uuid4()
        )
        out, _ = await write_hypothesis(
            conn, analyst_ctx=ctx,
            payload=HypothesisPayload(
                thesis=f"dynamics around {topic} are complex and noteworthy",
                counter_thesis="they are simple", situation_id=sid,
                evidence_balance=0, status="active",
            ),
            derived_from=[],
        )
        hyp_id = out.id
        await conn.execute(
            "UPDATE hypotheses SET produced_at = NOW() - make_interval(days => $2) "
            "WHERE id = $1",
            hyp_id, RESOLUTION_MIN_AGE_DAYS + 3,
        )
        # A QUIET subsequent fact (net 0) — the OLD code's `abs(net)<=1` branch
        # would have graded the undirected thesis TRUE off this.
        await _seed_fact(conn, subject=topic, predicate="status",
                         value="officials held a routine briefing")

        await _resolve_hypotheses_against_subsequent_facts(
            conn, now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        row = await conn.fetchrow(
            "SELECT resolved_outcome, resolved_by FROM hypotheses WHERE id = $1", hyp_id,
        )
    assert row["resolved_outcome"] is None, "undirected thesis must ABSTAIN (stay unresolved)"
    assert row["resolved_by"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exogenous_resolution_grades_status_quo_thesis(pg_pool):
    """DQ-H2b: a genuine STATUS-QUO claim IS still gradeable — quiet subsequent
    facts => it held (resolved_outcome=1). Only the truly undirected case
    abstains; status-quo is a real directional claim."""
    from legba.data.provenance import HypothesisPayload, write_hypothesis

    topic = f"Border{uuid4().hex[:6]}"
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, canonical_name=topic, entity_class="location")
        sid = await _seed_situation(conn, name=topic, intensity=4.0)
        ctx = AnalystContext(
            analyst_id=f"achsq_{uuid4().hex[:8]}", analyst_version="v1", run_id=uuid4()
        )
        out, _ = await write_hypothesis(
            conn, analyst_ctx=ctx,
            payload=HypothesisPayload(
                thesis=f"{topic} will remain at its current intensity (status quo)",
                counter_thesis="it breaks the status quo", situation_id=sid,
                evidence_balance=0, status="active",
            ),
            derived_from=[],
        )
        hyp_id = out.id
        await conn.execute(
            "UPDATE hypotheses SET produced_at = NOW() - make_interval(days => $2) "
            "WHERE id = $1",
            hyp_id, RESOLUTION_MIN_AGE_DAYS + 3,
        )
        await _seed_fact(conn, subject=topic, predicate="status",
                         value="officials held a routine briefing")

        await _resolve_hypotheses_against_subsequent_facts(
            conn, now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        row = await conn.fetchrow(
            "SELECT resolved_outcome, resolved_by FROM hypotheses WHERE id = $1", hyp_id,
        )
    assert row["resolved_outcome"] == 1, "status-quo thesis + quiet facts => it held"
    assert row["resolved_by"] == "subsequent_facts"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_calibration_reads_exogenous_resolved_outcome_only(pg_pool):
    """calibration_tracking must pull hypotheses by resolved_outcome (exogenous),
    NOT by status — a confirmed-but-UNRESOLVED hypothesis is absent from the
    Brier sample (closing the circular Brier)."""
    from legba.data.analysts.deterministic_handlers import calibration_tracking as cal
    from legba.data.provenance import HypothesisPayload, write_hypothesis

    analyst_id = f"calx_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        ctx = AnalystContext(analyst_id=analyst_id, analyst_version="v1", run_id=uuid4())
        # (1) confirmed BUT unresolved → must NOT be in the sample (no circular read).
        out_unresolved, _ = await write_hypothesis(
            conn, analyst_ctx=ctx,
            payload=HypothesisPayload(thesis="A", counter_thesis="x",
                                      evidence_balance=3, status="confirmed"),
            derived_from=[],
        )
        # (2) a row with an EXOGENOUS resolved_outcome stamped (e.g. by an operator).
        out_resolved, _ = await write_hypothesis(
            conn, analyst_ctx=ctx,
            payload=HypothesisPayload(thesis="B", counter_thesis="y",
                                      evidence_balance=2, status="active"),
            derived_from=[],
        )
        await conn.execute(
            "UPDATE hypotheses SET resolved_outcome = 0, resolved_at = NOW(), "
            "resolved_by = 'operator:test' WHERE id = $1",
            out_resolved.id,
        )

    pulled = await cal._pull_resolved_claims(_Deps(pg_pool), {"lookback_days": 365})
    mine = [r for r in pulled if r["analyst_id"] == analyst_id]
    ids = {r["claim_id"] for r in mine}
    assert str(out_resolved.id) in ids, "an exogenously-resolved row is in the sample"
    assert str(out_unresolved.id) not in ids, (
        "a confirmed-but-UNRESOLVED row must NOT be pulled (no circular status read)"
    )
    # The exogenous outcome (0) is what the Brier sees, derived confidence > 0.5.
    resolved_row = next(r for r in mine if r["claim_id"] == str(out_resolved.id))
    assert resolved_row["outcome"] == 0
    assert resolved_row["claimed_confidence"] > 0.5


# ---------------------------------------------------------------------------
# Contested-fact-value ACH evidence (Holes-B Wave 5, #101) — a stub-conn unit
# test (no live DB): an OPEN fact_contention group whose subject is a resolved
# topic entity becomes a ``contested_fact_value`` diagnostic-evidence item.
# ---------------------------------------------------------------------------


class _RoutingConn:
    """Routes ``fetch`` by table keyword so one stub serves every read leg of
    ``_read_evidence_for_topic`` (entity_profiles / analyst_outputs / facts /
    nexuses / fact_contention). Records the SQL it ran."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]]):
        self._rows = rows
        self.log: list[str] = []

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.log.append(sql)
        if "FROM entity_profiles" in sql:
            return self._rows.get("entity_profiles", [])
        if "FROM fact_contention" in sql:
            return self._rows.get("fact_contention", [])
        if "FROM analyst_outputs" in sql:
            return self._rows.get("analyst_outputs", [])
        if "FROM facts" in sql:
            return self._rows.get("facts", [])
        if "FROM nexuses" in sql:
            return self._rows.get("nexuses", [])
        return []


@pytest.mark.asyncio
async def test_read_evidence_surfaces_open_contention_as_diagnostic():
    cid = uuid4()
    conn = _RoutingConn({
        "entity_profiles": [{"canonical_name": "Country X"}],
        "fact_contention": [
            {
                "id": cid,
                "subject_key": "country x",
                "predicate_key": "capital",
                "status": "surfaced",
                "surfaced_value": "Alpha",
                "value_count": 2,
                "updated_at": None,
                "competing_values": ["Alpha", "Beta"],
            }
        ],
    })
    evidence = await _read_evidence_for_topic(
        conn, situation={"id": uuid4(), "name": "Country X", "derived_from": []},
        limit=30,
    )
    contested = [e for e in evidence if e["kind"] == "contested_fact_value"]
    assert len(contested) == 1
    item = contested[0]
    assert item["id"] == cid
    assert item["polarity"] == 0  # neutral — does not pre-bias a hypothesis
    assert "CONTESTED CLAIM" in item["text"]
    assert "Alpha vs Beta" in item["text"]
    assert "surfaced winner='Alpha'" in item["text"]
    # The query scoped by subject_key against the resolved entity set + only
    # live groups (contested/surfaced).
    fc_sql = next(s for s in conn.log if "FROM fact_contention" in s)
    assert "fc.subject_key = ANY($1::text[])" in fc_sql
    assert "fc.status IN ('contested', 'surfaced')" in fc_sql


@pytest.mark.asyncio
async def test_read_evidence_abstained_contention_reads_unresolved():
    conn = _RoutingConn({
        "entity_profiles": [{"canonical_name": "Country X"}],
        "fact_contention": [
            {
                "id": uuid4(),
                "subject_key": "country x",
                "predicate_key": "capital",
                "status": "contested",
                "surfaced_value": None,  # arbiter abstained
                "value_count": 2,
                "updated_at": None,
                "competing_values": ["Alpha", "Beta"],
            }
        ],
    })
    evidence = await _read_evidence_for_topic(
        conn, situation={"id": uuid4(), "name": "Country X", "derived_from": []},
        limit=30,
    )
    item = next(e for e in evidence if e["kind"] == "contested_fact_value")
    assert "NO surfaced winner" in item["text"]
    assert "abstained" in item["text"]


@pytest.mark.asyncio
async def test_read_evidence_no_contention_when_no_entities():
    """No resolved entity set → the contention leg never queries (no spurious
    diagnostic) — mirrors the facts/nexuses entity-scoping guard."""
    conn = _RoutingConn({"entity_profiles": []})
    evidence = await _read_evidence_for_topic(
        conn, situation={"id": uuid4(), "name": "", "derived_from": []},
        limit=30,
    )
    assert [e for e in evidence if e["kind"] == "contested_fact_value"] == []
    assert not any("FROM fact_contention" in s for s in conn.log)
