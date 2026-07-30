# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E6c — the entity_researcher RECLASSIFY pass (the person-skew fix).

The NER title-case->person default mis-typed ~half the graph, which BLOCKS merges
(persons never auto-merge). This pass LLM-classifies never-examined `person`
rows into the closed class set and rewrites entity_class for a confident
change, reversibly. Since the 2026-07-21 widening the pool is EVERY unexamined
person (quiet mis-types like "West Berlin"/"Dodge" carry no lexical signal);
suspects (article prefix, then org/loc/event keywords) only jump the queue.
Verified here (real migrated DB, canned classifier):

  * select_reclass_candidates returns every unexamined person row, lexical
    suspects FIRST (article, then keyword signal, then the bare name-shaped
    rest), and never an already-seen row;
  * _parse_reclass_batch binds by echoed name; an unmatched / invalid class
    defaults to NO CHANGE (never a silent move);
  * DRY-RUN reports would-change but mutates NOTHING (no class change, no marker);
  * APPLY rewrites the class for a confident non-person verdict, stores
    data.reclass = {from,to,...}, marks reclass_seen_at; a confirmed person is
    left `person` but marked seen; a below-threshold verdict is NOT moved.

#219 (2026-07-23) EXTENSION — the identical pass over the generic `entity`
bucket (DQ M6: ~29.5% of entity_profiles fall here; R-2/`6f270a2` made this
WORSE by demoting article-prefixed person->entity). Verified below:

  * select_reclass_candidates(source_class='entity') queries the entity pool
    ONLY (never touches a person row), ordered by the same institutional /
    geographic / corporate-suffix signal family as the person predicate;
  * reclassify_entities(source_class='entity') uses the entity-framed system
    prompt (a below-gate or unmatched verdict defaults to NO CHANGE = stays
    'entity', never a silent move) but the IDENTICAL response schema/parse;
  * run_entity_research SPLITS reclassify_max across the two pools via
    reclass_entity_share — never adds to it (person_max + entity_max ==
    reclassify_max, always); share=0.0 preserves pre-#219 behavior exactly
    (entity pool never queried, 100% of the budget stays with person).
"""

from __future__ import annotations

import json
import re
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.entity_researcher import (
    ReclassCandidate,
    _build_reclass_prompt,
    _build_reclass_system,
    _parse_reclass_batch,
    reclassify_entities,
    run_entity_research,
    select_reclass_candidates,
)
from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


# name -> (class, confidence) the canned classifier will echo.
_CANNED = {
    "the zzr6c indian ocean": ("location", 0.97),
    "the zzr6c foreign ministry": ("organization", 0.96),
    "the zzr6c world war": ("event", 0.95),
    "zzr6c sea king": ("entity", 0.55),          # below the 0.75 gate -> no move
    "the zzr6c real person": ("person", 0.9),    # confirmed person -> no move
    # #219 — entity-pool canned names (distinct 'zzr6e' prefix; never collides
    # with the person-pool 'zzr6c' names above, same classifier/fixture).
    "zzr6e acme holdings inc": ("corporation", 0.93),   # corp-suffix -> moves
    "the zzr6e west bank": ("location", 0.94),          # region -> moves
    "zzr6e some concept": ("entity", 0.5),              # genuinely unclear -> stays
}


class _CannedClassifier:
    """Extracts the quoted names from the prompt and emits a class per name."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kw):
        self.calls += 1
        prompt = messages[0]["content"]
        names = re.findall(r'"([^"]+)"', prompt)
        out = []
        for nm in names:
            cls, conf = _CANNED.get(re.sub(r"\s+", " ", nm.strip().lower()),
                                    ("person", 0.9))
            out.append({"name": nm, "class": cls, "confidence": conf, "why": "t"})

        class _R:
            content = json.dumps(out)
            usage = None

        return _R()


async def _seed_person(conn, name):
    eid = str(uuid4())
    await conn.execute(
        "INSERT INTO entity_profiles (id, canonical_name, entity_class, entity_type,"
        " data) VALUES ($1::uuid,$2,'person','person','{}'::jsonb)", eid, name)
    return eid


async def _seed_entity(conn, name):
    # #219: the generic-entity-pool counterpart to _seed_person.
    eid = str(uuid4())
    await conn.execute(
        "INSERT INTO entity_profiles (id, canonical_name, entity_class, entity_type,"
        " data) VALUES ($1::uuid,$2,'entity','entity','{}'::jsonb)", eid, name)
    return eid


# ---------------------------------------------------------------------------


def test_parse_binds_by_name_and_defaults_to_no_change():
    batch = [
        ReclassCandidate("id-a", "the Foo Ocean", "person"),
        ReclassCandidate("id-b", "Jane Roe", "person"),
    ]
    # only the first is echoed; the second is unmatched -> no change (person).
    content = json.dumps([
        {"name": "the Foo Ocean", "class": "location", "confidence": 0.9, "why": "x"},
    ])
    verdicts = {v.entity_id: v for v in _parse_reclass_batch(content, batch)}
    assert verdicts["id-a"].to_class == "location"
    assert verdicts["id-b"].to_class == "person"  # unmatched -> keep
    # an invalid class also defaults to no-change.
    v2 = _parse_reclass_batch(
        json.dumps([{"name": "Jane Roe", "class": "banana", "confidence": 0.9}]),
        [batch[1]])[0]
    assert v2.to_class == "person"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_select_candidates_full_pool_suspects_first(pg_pool):
    async with pg_pool.acquire() as conn:
        art = await _seed_person(conn, "the Zzr6csel Indian Ocean")   # article
        sig = await _seed_person(conn, "Zzr6csel Defence Ministry")    # org signal
        plain = await _seed_person(conn, "Zzr6csel Janean Doede")      # neither
        cands = await select_reclass_candidates(conn, 5000)
        order = [c.id for c in cands]
    # 2026-07-21 widening: EVERY unexamined person is in the pool (the quiet
    # "West Berlin"/"Dodge" class carries no lexical signal), suspects first.
    assert art in order and sig in order and plain in order
    assert order.index(art) < order.index(sig) < order.index(plain)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_moves_confident_reclass_reversibly(pg_pool):
    llm = _CannedClassifier()
    async with pg_pool.acquire() as conn:
        ocean = await _seed_person(conn, "the Zzr6c Indian Ocean")
        ministry = await _seed_person(conn, "the Zzr6c Foreign Ministry")
        war = await _seed_person(conn, "the Zzr6c World War")
        seaking = await _seed_person(conn, "Zzr6c Sea King")  # low conf -> no move
        person = await _seed_person(conn, "the Zzr6c Real Person")  # -> person

        # DRY-RUN: reports change, mutates nothing.
        exam, chg, samp = await reclassify_entities(
            conn, llm, apply=False, max_rows=50)
        assert exam >= 5 and chg >= 3
        row = await conn.fetchrow(
            "SELECT entity_class, data FROM entity_profiles WHERE id=$1::uuid", ocean)
        assert row["entity_class"] == "person"  # unchanged by dry-run
        assert "reclass_seen_at" not in (json.loads(row["data"])
                                         if isinstance(row["data"], str) else row["data"])

        # APPLY.
        exam2, chg2, samp2 = await reclassify_entities(
            conn, llm, apply=True, max_rows=50)
        assert chg2 >= 3

        async def cls(eid):
            return await conn.fetchval(
                "SELECT entity_class FROM entity_profiles WHERE id=$1::uuid", eid)

        async def data(eid):
            d = await conn.fetchval(
                "SELECT data FROM entity_profiles WHERE id=$1::uuid", eid)
            return json.loads(d) if isinstance(d, str) else d

        assert await cls(ocean) == "location"
        assert await cls(ministry) == "organization"
        assert await cls(war) == "event"
        assert await cls(seaking) == "person"   # 0.55 < 0.75 gate -> not moved
        assert await cls(person) == "person"     # confirmed person -> not moved

        # reversibility ledger + seen-marker.
        d_ocean = await data(ocean)
        assert d_ocean["reclass"]["from"] == "person"
        assert d_ocean["reclass"]["to"] == "location"
        assert "reclass_seen_at" in d_ocean
        # even the no-move rows are marked seen (drain the pool).
        assert "reclass_seen_at" in (await data(seaking))
        assert "reclass_seen_at" in (await data(person))

        # idempotent: a second apply examines nothing (all marked seen).
        exam3, chg3, _ = await reclassify_entities(conn, llm, apply=True, max_rows=50)
    assert exam3 == 0 and chg3 == 0


# ===========================================================================
# #219 — generic-entity extension. Same shape as the person-pool tests above,
# proving: (a) the entity pool is queried in isolation (never a person row),
# (b) apply/reversibility/gate behavior is IDENTICAL, (c) the shared cap SPLITS
# rather than adds.
# ===========================================================================


def test_reclass_prompt_header_names_actual_current_class():
    # #219: the batch header must not hardcode 'person' when reviewing the
    # entity pool (it would misdescribe the batch to the LLM).
    person_batch = [ReclassCandidate("id-a", "Jane Roe", "person")]
    entity_batch = [ReclassCandidate("id-b", "Some Concept", "entity")]
    assert "'person'" in _build_reclass_prompt(person_batch)
    assert "'entity'" in _build_reclass_prompt(entity_batch)
    assert "'person'" not in _build_reclass_prompt(entity_batch)


def test_reclass_system_shares_schema_diverges_on_framing():
    person_sys = _build_reclass_system("person")
    entity_sys = _build_reclass_system("entity")
    # the response schema + class definitions are shared VERBATIM (#219 must
    # never let the two prompts drift on the part _parse_reclass_batch depends
    # on) — the exact schema line appears identically in both.
    schema_line = (
        '[{"n": 1, "name": "<name verbatim>", "class": '
        '"<one of: country|organization|corporation|location|person|event|entity>"'
    )
    assert schema_line in person_sys and schema_line in entity_sys
    assert 'organization = an institution' in person_sys
    assert 'organization = an institution' in entity_sys
    # the framing + conservative-default rule DIVERGE (this is the whole point).
    assert "CURRENTLY typed `person`" in person_sys
    assert "CURRENTLY typed `entity`" in entity_sys
    assert "CURRENTLY typed `entity`" not in person_sys
    assert "CURRENTLY typed `person`" not in entity_sys
    # unknown source_class fails safe to the person framing (never an unbounded
    # / unrecognized prompt).
    assert _build_reclass_system("bogus") == person_sys


@pytest.mark.integration
@pytest.mark.asyncio
async def test_select_entity_candidates_isolated_from_person_pool(pg_pool):
    async with pg_pool.acquire() as conn:
        # a person row that WOULD look like an entity-pool suspect if the
        # predicate leaked across classes (it must not: the base filter is
        # entity_class = 'entity', never 'person').
        leak_check = await _seed_person(conn, "Zzr6esel Ministry Of Truth")
        org = await _seed_entity(conn, "Zzr6esel Acme Holdings Inc")     # corp signal
        region = await _seed_entity(conn, "the Zzr6esel West Bank")      # geo signal
        plain = await _seed_entity(conn, "Zzr6esel Some Concept")        # neither

        cands = await select_reclass_candidates(conn, 5000, source_class="entity")
        ids = {c.id for c in cands}
        order = [c.id for c in cands]

    assert leak_check not in ids  # the person row never leaks into the entity pool
    assert org in ids and region in ids and plain in ids
    assert all(c.cur_class == "entity" for c in cands if c.id in
               {org, region, plain})
    # suspects (org/geo signal) queue ahead of the bare concept.
    assert order.index(org) < order.index(plain)
    assert order.index(region) < order.index(plain)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_entity_pool_moves_confidently_stays_entity_when_unclear(pg_pool):
    llm = _CannedClassifier()
    async with pg_pool.acquire() as conn:
        org = await _seed_entity(conn, "Zzr6e Acme Holdings Inc")
        region = await _seed_entity(conn, "the Zzr6e West Bank")
        concept = await _seed_entity(conn, "Zzr6e Some Concept")  # stays entity

        exam, chg, sample = await reclassify_entities(
            conn, llm, apply=True, max_rows=50, source_class="entity")
        assert exam >= 3 and chg >= 2

        async def cls(eid):
            return await conn.fetchval(
                "SELECT entity_class FROM entity_profiles WHERE id=$1::uuid", eid)

        async def data(eid):
            d = await conn.fetchval(
                "SELECT data FROM entity_profiles WHERE id=$1::uuid", eid)
            return json.loads(d) if isinstance(d, str) else d

        assert await cls(org) == "corporation"
        assert await cls(region) == "location"
        assert await cls(concept) == "entity"  # genuinely unclear -> stays put

        # SAME reversibility ledger shape as the person pool.
        d_org = await data(org)
        assert d_org["reclass"]["from"] == "entity"
        assert d_org["reclass"]["to"] == "corporation"
        assert "reclass_seen_at" in d_org
        assert "reclass_seen_at" in (await data(concept))  # examined, marked, unmoved

        # idempotent drain, same as the person pool.
        exam2, chg2, _ = await reclassify_entities(
            conn, llm, apply=True, max_rows=50, source_class="entity")
    assert exam2 == 0 and chg2 == 0


class _CountingLLM:
    """Records how many candidate NAMES it was asked to classify, split by
    which pool's framing sentence appears in the system prompt — a proxy for
    "how many rows did each pool actually send to the LLM this run", without
    needing real seeded data (used to test run_entity_research's split-cap
    ARITHMETIC in isolation from candidate-pool contents)."""

    def __init__(self) -> None:
        self.person_names = 0
        self.entity_names = 0

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kw):
        prompt = messages[0]["content"]
        n = len(re.findall(r'"([^"]+)"', prompt))
        if "CURRENTLY typed `entity`" in (system or ""):
            self.entity_names += n
        else:
            self.person_names += n

        class _R:
            content = "[]"
            usage = None

        return _R()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_entity_research_splits_not_adds_the_reclass_cap(pg_pool):
    # #219's core constraint: reclassify_max is SPLIT across the two pools,
    # never added to. Seed MORE rows than the cap in BOTH pools so each pool's
    # actual LLM volume is bounded by its SHARE of reclassify_max, not by
    # candidate-pool exhaustion — isolating the arithmetic under test.
    llm = _CountingLLM()
    async with pg_pool.acquire() as conn:
        for i in range(12):
            await _seed_person(conn, f"Zzr6split Person {i}")
        for i in range(12):
            await _seed_entity(conn, f"Zzr6split Entity {i}")

        # share=0.0 (the shipped default): ALL 10 go to person, entity untouched.
        rep_off = await run_entity_research(
            conn, llm, apply=False, max_pairs=0, reclassify_max=10,
            reclass_entity_share=0.0)
    assert rep_off.reclass_examined == 10
    assert llm.person_names == 10 and llm.entity_names == 0
    assert rep_off.reclass_by_class.get("entity", {}).get("examined", 0) == 0
    assert rep_off.reclass_by_class["person"]["examined"] == 10

    # share=0.3 of a 10-cap => entity gets round(10*0.3)=3, person gets the
    # REMAINDER 7. Total LLM volume across both pools is STILL 10 — the split
    # never grows the combined per-tick call count.
    llm2 = _CountingLLM()
    async with pg_pool.acquire() as conn:
        rep_split = await run_entity_research(
            conn, llm2, apply=False, max_pairs=0, reclassify_max=10,
            reclass_entity_share=0.3)
    assert llm2.entity_names == 3
    assert llm2.person_names == 7
    assert llm2.person_names + llm2.entity_names == 10  # combined cap unchanged
    assert rep_split.reclass_examined == 10
    assert rep_split.reclass_by_class["entity"]["examined"] == 3
    assert rep_split.reclass_by_class["person"]["examined"] == 7

    # share=1.0: ALL 10 go to entity, person pool untouched.
    llm3 = _CountingLLM()
    async with pg_pool.acquire() as conn:
        rep_all_entity = await run_entity_research(
            conn, llm3, apply=False, max_pairs=0, reclassify_max=10,
            reclass_entity_share=1.0)
    assert llm3.entity_names == 10 and llm3.person_names == 0
    assert rep_all_entity.reclass_by_class["entity"]["examined"] == 10
    assert rep_all_entity.reclass_by_class.get("person", {}).get("examined", 0) == 0


@pytest.mark.asyncio
async def test_reclass_entity_share_clamped_out_of_range():
    # A share outside [0,1] must clamp rather than produce a negative pool
    # size or exceed the combined cap (defensive — a misconfigured descriptor
    # PUT must never be able to double the LLM volume). A conn stub that
    # unconditionally returns [] cannot tell "clamped correctly" apart from
    # "clamped wrong" (any positive LIMIT yields the same empty result, and
    # even a NEGATIVE LIMIT would only surface as a real Postgres error this
    # stub never raises) — so this records the ACTUAL LIMIT argument each
    # pool's query was called with and asserts on the captured value, not
    # just on "nothing crashed".
    class _RecordingConn:
        def __init__(self) -> None:
            self.limits: list[int] = []

        async def fetch(self, sql, limit, *a, **k):
            # generate_candidates' country-alias gazetteer probe passes a
            # surface LIST (not a LIMIT); it is not a pool query, so it is
            # excluded from the recorded limits this test asserts on.
            if isinstance(limit, (int, float)):
                self.limits.append(int(limit))
            return []

    llm = _CountingLLM()
    conn = _RecordingConn()
    rep = await run_entity_research(
        conn, llm, apply=False, max_pairs=0, reclassify_max=10,
        reclass_entity_share=5.0,  # way out of range -> clamps to 1.0
    )
    # conn.limits[0] == 1000 is generate_candidates' OWN exact-block-key probe
    # (exact_limit=max(max_pairs*4, 1000); fires unconditionally before the
    # reclassify section, independent of reclassify_max/share — not part of
    # what THIS test is checking). The reclassify section's query is always
    # the LAST recorded call: share=5.0 clamps to 1.0 -> entity_max=
    # round(10*1.0)=10, person_max=0 (skipped entirely — pool_max<=0 never
    # calls select_reclass_candidates). The ONLY reclassify query issued is
    # the entity pool's, with LIMIT=10 exactly (not 50, not -40, not any
    # other value an unclamped 5.0 could produce).
    assert conn.limits[-1] == 10
    assert len(conn.limits) == 2  # the 1000-probe + the one reclassify call
    assert rep.reclass_examined == 0  # no candidates in either pool -> no-op
    assert llm.person_names == 0 and llm.entity_names == 0

    # The symmetric negative-share case: clamps to 0.0 -> entity_max=0
    # (skipped), person_max=10 (the only reclassify query, LIMIT=10 exactly).
    conn2 = _RecordingConn()
    llm2 = _CountingLLM()
    rep2 = await run_entity_research(
        conn2, llm2, apply=False, max_pairs=0, reclassify_max=10,
        reclass_entity_share=-3.0,
    )
    assert conn2.limits[-1] == 10
    assert len(conn2.limits) == 2
    assert rep2.reclass_examined == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reclass_entity_share_nan_fails_safe_to_person_only(pg_pool):
    # A NaN share (reachable via a malformed descriptor value — `.nan` is
    # valid YAML 1.1) must fail safe to share=0.0 (100% person, the
    # well-validated pool), NOT resolve to 1.0 (Python's bare
    # max(0.0, min(1.0, nan)) keeps its first argument on a NaN comparison,
    # which is the WRONG fail-safe direction — it would silently divert the
    # full budget to the newer, less-validated entity pool).
    llm = _CountingLLM()
    async with pg_pool.acquire() as conn:
        for i in range(5):
            await _seed_person(conn, f"Zzr6nan Person {i}")
        for i in range(5):
            await _seed_entity(conn, f"Zzr6nan Entity {i}")

        rep = await run_entity_research(
            conn, llm, apply=False, max_pairs=0, reclassify_max=5,
            reclass_entity_share=float("nan"),
        )
    assert llm.entity_names == 0 and llm.person_names == 5
    assert rep.reclass_by_class.get("entity", {}).get("examined", 0) == 0
    assert rep.reclass_by_class["person"]["examined"] == 5
