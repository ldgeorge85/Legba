# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G2 — "these two names are one entity" is a merge candidate, not an edge.

The bake-off's floor stratum accepted verdicts like
``IRGC AffiliatedWith Revolutionary Guards Corps`` — true, and worthless (§6.5).
Neither existing defence catches the class: ``same_referent`` folds only lexical
relations (demonym, singular/plural), and the N4 keeper gate needs the merge to
have already happened. Two surfaces with no shared lexical form and no shared
keeper sail through both and mint an edge saying an organisation is affiliated
with itself.

So the typer is asked one extra question, and a ``same_entity`` answer is routed
to ``entity_judgement`` — the tree's purpose-built merge surface — rather than
minted. The delicate part, pinned hardest below: that row must never become a
DECISION. ``entity_judgement`` doubles as the LLM re-adjudication cache, so an
untagged proposal would suppress the very adjudication it exists to invite.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.reifier_alias_pairs import (
    ALIAS_PAIR_DECIDED_BY,
    ALIAS_PAIR_MODEL_ID,
    ALIAS_PAIR_VERDICT,
    pair_key_for,
    record_alias_pair,
)
from legba.data.analysts.relationship_reifier import ReifierDeps, run_method
from legba.data.analysts.relationship_typing_batch import (
    BATCH_SYSTEM_PROMPT,
    BatchCandidate,
    parse_batch_response,
)
from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


# ---------------------------------------------------------------------------
# the verdict class
# ---------------------------------------------------------------------------


def _cand(idx: int = 0, source: str = "IRGC",
          target: str = "Revolutionary Guards Corps") -> BatchCandidate:
    return BatchCandidate(idx=idx, source=source, target=target,
                          evidence_text="The IRGC, or Revolutionary Guards Corps")


def test_same_entity_is_never_an_edge():
    raw = json.dumps([{
        "idx": 0, "same_entity": True, "related": False, "confidence": 0.9,
        "rationale": "acronym and expansion",
    }])
    result = parse_batch_response(raw, [_cand()])
    v = result.verdicts[0]
    assert v.same_entity is True
    assert v.accepted is False
    assert v.payload is None
    assert v.reject_reason == "alias_pair"


def test_same_entity_wins_even_when_the_model_also_says_related():
    """An acronym IS 'related' to its expansion in every ordinary sense, so the
    model will often answer both. Identity must short-circuit."""
    raw = json.dumps([{
        "idx": 0, "same_entity": True, "related": True,
        "rel_type": "AffiliatedWith", "intent": "supportive",
        "channel": "direct", "confidence": 0.8,
    }])
    v = parse_batch_response(raw, [_cand()]).verdicts[0]
    assert v.same_entity is True
    assert v.accepted is False
    assert v.payload is None


def test_an_ordinary_verdict_is_untouched():
    raw = json.dumps([{
        "idx": 0, "related": True, "rel_type": "AlliedWith",
        "intent": "supportive", "channel": "direct", "confidence": 0.7,
    }])
    v = parse_batch_response(raw, [_cand(source="France", target="Germany")]).verdicts[0]
    assert v.same_entity is False
    assert v.accepted is True


def test_part_whole_is_not_an_alias_pair():
    """A subsidiary, subcommittee, province or member state is a DIFFERENT
    entity from its parent — the prompt says so, and the parser must not
    second-guess a model that answered same_entity=false."""
    raw = json.dumps([{
        "idx": 0, "same_entity": False, "related": True, "rel_type": "PartOf",
        "intent": "neutral", "channel": "institutional", "confidence": 0.7,
    }])
    v = parse_batch_response(
        raw, [_cand(source="Council of the IMO", target="IMO")]
    ).verdicts[0]
    assert v.same_entity is False
    assert v.accepted is True


def test_the_prompt_asks_the_question_and_gives_the_worked_example():
    assert '"same_entity"' in BATCH_SYSTEM_PROMPT
    assert "SAME-ENTITY CHECK FIRST" in BATCH_SYSTEM_PROMPT
    assert "IRGC" in BATCH_SYSTEM_PROMPT
    # ... and states the part-whole carve-out, which is the failure mode of
    # asking the question at all
    assert "subsidiary" in BATCH_SYSTEM_PROMPT


def test_a_missing_same_entity_key_defaults_to_false():
    """Every verdict ever emitted before this field existed must still parse."""
    raw = json.dumps([{
        "idx": 0, "related": True, "rel_type": "AlliedWith",
        "intent": "supportive", "channel": "direct", "confidence": 0.7,
    }])
    assert parse_batch_response(raw, [_cand()]).verdicts[0].same_entity is False


# ---------------------------------------------------------------------------
# routing to the merge surface
# ---------------------------------------------------------------------------


async def _seed_entity(conn, name: str, cls: str = "organization") -> str:
    return await conn.fetchval(
        "INSERT INTO entity_profiles (canonical_name, entity_class, data) "
        "VALUES ($1, $2, '{}'::jsonb) RETURNING id",
        name, cls,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_pair_lands_in_entity_judgement(pg_pool):
    tag = uuid4().hex[:8]
    a, b = f"IRGC{tag}", f"Revolutionary Guards Corps{tag}"
    async with pg_pool.acquire() as conn:
        id_a = await _seed_entity(conn, a)
        id_b = await _seed_entity(conn, b)
        outcome = await record_alias_pair(conn, a, b, confidence=0.9)
        row = await conn.fetchrow(
            "SELECT * FROM entity_judgement WHERE pair_key = $1",
            pair_key_for(id_a, id_b),
        )
    assert outcome == "recorded"
    assert row is not None
    assert row["verdict"] == ALIAS_PAIR_VERDICT == "unsure"
    assert row["decided_by"] == ALIAS_PAIR_DECIDED_BY == "rule"
    assert row["model_id"] == ALIAS_PAIR_MODEL_ID
    assert str(row["entity_a"]) in {str(id_a), str(id_b)}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_verdict_is_unsure_not_same(pg_pool):
    """The typer was asked to type a relationship and answered a side question.
    Recording 'same' would over-claim — and would let execute_merges act on it."""
    assert ALIAS_PAIR_VERDICT == "unsure"
    tag = uuid4().hex[:8]
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, f"A{tag}")
        await _seed_entity(conn, f"B{tag}")
        await record_alias_pair(conn, f"A{tag}", f"B{tag}")
        verdicts = await conn.fetch(
            "SELECT verdict FROM entity_judgement WHERE model_id = $1",
            ALIAS_PAIR_MODEL_ID,
        )
    assert {r["verdict"] for r in verdicts} == {"unsure"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_pair_key_is_order_independent(pg_pool):
    tag = uuid4().hex[:8]
    a, b = f"X{tag}", f"Y{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, a)
        await _seed_entity(conn, b)
        first = await record_alias_pair(conn, a, b)
        reverse = await record_alias_pair(conn, b, a)
        n = await conn.fetchval(
            "SELECT count(*) FROM entity_judgement WHERE model_id = $1 "
            "AND justification LIKE $2",
            ALIAS_PAIR_MODEL_ID, f"%{tag}%",
        )
    assert first == "recorded"
    assert reverse == "duplicate", "(A,B) and (B,A) must be one candidate"
    assert n == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_existing_real_verdict_is_never_clobbered(pg_pool):
    tag = uuid4().hex[:8]
    a, b = f"P{tag}", f"Q{tag}"
    async with pg_pool.acquire() as conn:
        id_a = await _seed_entity(conn, a)
        id_b = await _seed_entity(conn, b)
        key = pair_key_for(id_a, id_b)
        await conn.execute(
            "INSERT INTO entity_judgement (pair_key, entity_a, entity_b, "
            "verdict, decided_by, justification) "
            "VALUES ($1,$2,$3,'not_same','human','operator adjudicated')",
            key, id_a, id_b,
        )
        outcome = await record_alias_pair(conn, a, b)
        row = await conn.fetchrow(
            "SELECT verdict, decided_by FROM entity_judgement WHERE pair_key=$1",
            key,
        )
    assert outcome == "duplicate"
    assert row["verdict"] == "not_same"
    assert row["decided_by"] == "human"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unresolvable_side_is_counted_not_written(pg_pool):
    """A merge candidate naming something that is not an entity is not a
    candidate. It still gets counted by the caller — the count never silently
    drops to zero."""
    tag = uuid4().hex[:8]
    async with pg_pool.acquire() as conn:
        await _seed_entity(conn, f"Real{tag}")
        outcome = await record_alias_pair(
            conn, f"Real{tag}", f"NotAnEntity{tag}"
        )
    assert outcome == "unresolved"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recording_never_raises(pg_pool):
    """Degrade-not-break: an alias-pair write must never sink a typing run."""
    async with pg_pool.acquire() as conn:
        assert await record_alias_pair(conn, "", "") in {"unresolved", "failed"}


# ---------------------------------------------------------------------------
# THE GUARD: a proposal must never become a decision
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_proposal_does_not_suppress_llm_adjudication(pg_pool):
    """THE delicate one. `entity_judgement` doubles as entity_researcher's
    re-adjudication cache: a pair with a row is never re-sent to the LLM. An
    untagged proposal would therefore suppress the adjudication of exactly the
    pair it was raised to surface."""
    from legba.data.analysts.entity_researcher import _load_cached

    tag = uuid4().hex[:8]
    a, b = f"M{tag}", f"N{tag}"
    async with pg_pool.acquire() as conn:
        id_a = await _seed_entity(conn, a)
        id_b = await _seed_entity(conn, b)
        await record_alias_pair(conn, a, b)
        key = pair_key_for(id_a, id_b)
        cached = await _load_cached(conn, [key])
    assert cached == {}, "an alias-pair proposal leaked into the adjudication cache"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_real_verdict_is_still_cached(pg_pool):
    """The exclusion must be surgical — every pre-existing row still caches."""
    from legba.data.analysts.entity_researcher import _load_cached

    tag = uuid4().hex[:8]
    async with pg_pool.acquire() as conn:
        id_a = await _seed_entity(conn, f"R{tag}")
        id_b = await _seed_entity(conn, f"S{tag}")
        key = pair_key_for(id_a, id_b)
        await conn.execute(
            "INSERT INTO entity_judgement (pair_key, entity_a, entity_b, "
            "verdict, decided_by, model_id) VALUES ($1,$2,$3,'same','llm','gpt')",
            key, id_a, id_b,
        )
        cached = await _load_cached(conn, [key])
    assert key in cached
    assert cached[key].verdict == "same"


def test_nothing_else_writes_the_alias_pair_tag():
    """What makes the cache exclusion provably a no-op for existing rows."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "legba"
    literal = {
        p.name for p in src.rglob("*.py") if ALIAS_PAIR_MODEL_ID in p.read_text()
    }
    # The tag string exists in exactly ONE place. A second occurrence means a
    # second writer appeared, and the "provably a no-op for every pre-existing
    # row" claim in _load_cached is no longer true.
    assert literal == {"reifier_alias_pairs.py"}, literal
    # ... and the cache excludes it by IMPORTING that one definition, never by
    # restating the string.
    researcher = (src / "data" / "analysts" / "entity_researcher.py").read_text()
    assert "ALIAS_PAIR_MODEL_ID" in researcher
    assert ALIAS_PAIR_MODEL_ID not in researcher


# ---------------------------------------------------------------------------
# end to end through the run loop
# ---------------------------------------------------------------------------


class _AliasLLM:
    subprovider = "stub"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def chat_complete(self, messages, **kw):
        self.calls.append(messages)
        prompt = messages[0]["content"]
        idxs = [
            int(line.split()[-2])
            for line in prompt.splitlines()
            if line.startswith("--- CANDIDATE ")
        ]
        out = [
            {"idx": i, "same_entity": True, "related": False, "confidence": 0.9}
            for i in idxs
        ]

        class _U:
            prompt_tokens = 10
            completion_tokens = 5
            reasoning_tokens = 0

        class _R:
            content = json.dumps(out)
            usage = _U()

        return _R()


@pytest.mark.asyncio
async def test_the_run_counts_alias_pairs_and_writes_no_edge():
    llm = _AliasLLM()
    rows = [
        {"source_entity": "IRGC", "target_entity": "Revolutionary Guards Corps",
         "evidence_text": "the IRGC, or Revolutionary Guards Corps,"},
        {"source_entity": "IMO", "target_entity": "Intl Maritime Organization",
         "evidence_text": "the IMO"},
    ]
    res = await run_method(rows, {}, ReifierDeps(llm=llm))
    data = res.finding.data
    assert data["alias_pairs"] == 2
    assert data["accepted"] == 0, "an alias pair must never mint an edge"
    assert data["rejected"] == 0, "an alias pair is not a plain rejection"
    assert data["written"] == 0
