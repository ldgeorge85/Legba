# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B-8 — the journal's ``[[instrument]]`` empty read, against a POPULATED engine.

THE ENTRY. On 2026-08-03 at 00:01 the journal wrote:

    "the assessment engine produced no country-level rows and identified no
     disagreements between scorecard and composition products in the last 48
     hours [[instrument]]"

on a day with 1,562 successful analyst runs and 131 fresh ``country_composition``
findings. Nothing lied to it. It called ``get_assessments`` once, and the read
was CORRECT under its own semantics — it just asked for ``country_assessor``, a
``state: draft`` analyst that has written nothing for months, and then reported
that zero as a fact about the engine.

THREE THINGS HAD TO LINE UP, and each is closed here:

  1. The GATHER tool catalog described ``get_assessments`` as "recent
     country_assessor/world_assessor reads" — a hand-typed roster naming a dead
     analyst. The persona's fleet map said the same thing fifteen lines after
     correctly describing the live set. The planner did as it was told.
  2. A narrowed read for a non-producer returned a bare ``rows: []``, which is
     indistinguishable from a quiet engine.
  3. The ``disagreements`` scan derives its targets FROM those rows, so one empty
     read short-circuited it to ``[]`` — and ``[]`` reads as "measured, they
     agree" rather than "nothing was compared". One bad argument, both halves of
     the false claim.

The tests below run against a REAL substrate holding real ``country_composition``
findings, because the whole failure was about what an empty answer means when the
engine is in fact busy — a fake pool that returns ``[]`` cannot express that.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.runtime.substrate_query_port import (
    _ASSESSMENT_PRODUCER_ANALYSTS,
    PostgresQdrantSubstrateQueryPort,
)


_SCHEMA = "iglu:legba/finding/jsonschema/1-0-0"
#: Stamped on every row this module writes so cleanup can be scoped to them, and
#: so the assertions speak about THIS module's rows rather than assuming it owns
#: a session-scoped table it shares with the rest of the suite.
_RUN_ID = uuid4()
_TAG = _RUN_ID.hex[:8]
_CLEANUP = "DELETE FROM analyst_outputs WHERE run_id = $1"
_TARGETS = tuple(
    f"country_b8_{_TAG}_{c}" for c in ("de", "cn", "kp", "br", "in", "tw")
)


def _mine(rows: list[dict]) -> list[dict]:
    """This module's rows, by target-id tag."""
    prefix = f"country_b8_{_TAG}"
    return [r for r in rows if str(r.get("target_id") or "").startswith(prefix)]


@pytest_asyncio.fixture
async def populated_port(migrated_pg: PostgresConfig):
    """A substrate that is BUSY — the 08-03 condition. Six live country
    compositions plus a bounded-unit finding, exactly the rows the journal
    claimed did not exist."""
    pool = await asyncpg.create_pool(
        min_size=1, max_size=3,
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
    )
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        # Scoped cleanup, not a truncate: ``migrated_pg`` is SESSION scoped, so
        # wiping analyst_outputs here would silently break any other test in the
        # same run that seeded findings in a broader-scoped fixture.
        await conn.execute(_CLEANUP, _RUN_ID)
        for i, target in enumerate(_TARGETS):
            await conn.execute(
                "INSERT INTO analyst_outputs (id, kind, title, body, confidence, "
                "target_id, analyst_id, produced_at, schema_uri, run_id) "
                "VALUES ($1,'finding',$2,'composed body',0.7,$3,"
                "'country_composition',$4,$5,$6)",
                uuid4(), f"{target} composition", target,
                now - timedelta(hours=i + 1), _SCHEMA, _RUN_ID,
            )
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, body, confidence, "
            "target_id, analyst_id, produced_at, schema_uri, run_id) "
            "VALUES ($1,'finding','escalation read','b',0.6,$2,"
            "'escalation',$3,$4,$5)",
            uuid4(), _TARGETS[2], now - timedelta(hours=2), _SCHEMA, _RUN_ID,
        )
    try:
        yield PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(_CLEANUP, _RUN_ID)
        await pool.close()


@pytest.mark.asyncio
async def test_the_default_read_sees_the_busy_engine(populated_port):
    """The control. With no narrowing argument the journal sees all seven rows —
    so any claim that the engine produced nothing is refutable in one call."""
    out = await populated_port.get_assessments(since_hours=48, limit=200)

    mine = _mine(out["rows"])
    assert len(mine) == 7
    assert {r["analyst_id"] for r in mine} == {"country_composition", "escalation"}
    assert "unavailable" not in out


@pytest.mark.asyncio
async def test_a_retired_analyst_id_returns_empty_WITH_an_explanation(
    populated_port,
):
    """The exact 08-03 call. It still returns zero rows — that is honest, the
    analyst really does produce nothing — but it can no longer be mistaken for a
    quiet engine, and it names what to ask instead."""
    out = await populated_port.get_assessments(
        analyst_id="country_assessor", since_hours=48,
    )

    assert out["count"] == 0
    assert out["rows"] == []
    note = out.get("unavailable")
    assert note, "an empty narrowed read must explain itself"
    assert "country_assessor" in note
    assert "not a live assessment producer" in note
    # It must point at the live set, derived — not a second hand-typed roster.
    for producer in _ASSESSMENT_PRODUCER_ANALYSTS:
        assert producer in note


@pytest.mark.asyncio
async def test_an_unmeasured_disagreement_block_is_null_not_empty(populated_port):
    """The second half of the false claim. When no country rows came back there
    was nothing to reconcile — that must read as NOT MEASURED, never as
    'the two products agree'."""
    out = await populated_port.get_assessments(
        analyst_id="country_assessor", since_hours=48,
    )
    assert out["disagreements"] is None
    assert out["disagreements"] != []   # the distinction is the entire point


@pytest.mark.asyncio
async def test_a_measured_reconciliation_is_an_empty_list_not_null(populated_port):
    """The converse, so ``null`` keeps its meaning. Real country rows came back
    and were reconciled against their scorecards; none diverge (no scorecards
    seeded), so the answer is a measured, honest ``[]``."""
    out = await populated_port.get_assessments(since_hours=48, limit=200)

    assert _mine(out["rows"]), "the fixture's rows must be in the read"
    assert isinstance(out["disagreements"], list)   # MEASURED — the whole point
    assert out["disagreements"] is not None
    # None of THIS module's targets diverge (no scorecards seeded for them).
    prefix = f"country_b8_{_TAG}"
    assert not [d for d in out["disagreements"]
                if str(d.get("target_id") or "").startswith(prefix)]


@pytest.mark.asyncio
async def test_every_live_producer_is_readable_by_name(populated_port):
    """A named read for a producer that IS live must behave normally — the guard
    must reject only non-producers, never narrow the tool's real surface."""
    out = await populated_port.get_assessments(
        analyst_id="country_composition", since_hours=48, limit=200,
    )
    assert len(_mine(out["rows"])) == 6
    assert "unavailable" not in out


# ---------------------------------------------------------------------------
# The prompt surfaces that produced the bad argument
# ---------------------------------------------------------------------------


def test_the_tool_catalog_cannot_name_a_dead_analyst():
    """The catalog line is DERIVED from the producer tuple. A hand-typed roster
    in a prompt is a claim about the fleet with nothing keeping it true — this is
    the mechanism that keeps it true."""
    from legba.data.analysts.journal_assessor import _JOURNAL_READ_TOOL_SCHEMAS

    line = _JOURNAL_READ_TOOL_SCHEMAS["get_assessments"]
    assert "country_assessor" not in line
    for producer in _ASSESSMENT_PRODUCER_ANALYSTS:
        assert producer in line, producer


def test_the_persona_does_not_advertise_the_retired_monolith_as_the_producer():
    """The persona's fleet map listed 'country_assessor — one per G20 target'
    fifteen lines after correctly describing the live set. Two contradictory
    rosters in one prompt is how the planner picked the wrong one."""
    from legba.prompts.journal_assessor import JOURNAL_SYSTEM

    assert "country_assessor — one per G20 target" not in JOURNAL_SYSTEM
    assert "country_composition" in JOURNAL_SYSTEM
    # If it is mentioned at all, it must be marked retired in the same breath.
    if "country_assessor" in JOURNAL_SYSTEM:
        idx = JOURNAL_SYSTEM.index("country_assessor")
        assert "retired" in JOURNAL_SYSTEM[idx:idx + 260]


def test_the_narrator_is_told_an_empty_read_is_about_its_own_question():
    """The discipline that makes the 08-03 sentence unwritable. Asserted on the
    RENDERED entry-tier prompt, not on a constant — the disciplines are tier-
    scoped, and one that never reaches the entry tier is not a discipline."""
    from legba.data.analysts.journal_assessor import _render_user_prompt

    prompt = _render_user_prompt([], tier="entry")
    assert "NOT that the engine produced" in prompt
    assert "unavailable" in prompt
    assert "NOT MEASURED" in prompt


def test_that_discipline_does_not_leak_into_the_public_record_tiers():
    """The diary disciplines are the first-person tiers' contract; the chronicle
    and lens tiers must not inherit them (the module's own stated invariant)."""
    from legba.data.analysts.journal_assessor import _render_user_prompt

    for tier in ("chronicle", "lens"):
        assert "NOT that the engine produced" not in _render_user_prompt(
            [], tier=tier)
