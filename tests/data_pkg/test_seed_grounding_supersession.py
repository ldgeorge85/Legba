# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tier-0 grounding: a leader CHANGE supersedes the prior officeholder.

The grounding injection (Tier 1) is only as good as the seed it reads, and the
seed must be temporally honest: when a new head of state takes office the prior
one must be CLOSED (``valid_until`` + ``superseded_by``), not left as a second
open "current" row. The `LeaderOf` fact (subject=leader) CANNOT auto-supersede
on a leader change — supersession keys on (lower(subject), lower(predicate)),
and the subject is the PERSON, so a new leader is a different subject. The
adapters therefore ALSO emit a country-SUBJECT office fact
(``<country> | head of state | <leader>``); this test proves THAT row
supersedes correctly through the live seed write path.

Scenario (offline — uses the wikidata adapter's ``sparql_json`` fixture seam,
NO network): seed a stale leader for a country, then run a fresh pull naming a
DIFFERENT leader for the SAME country → the stale country-subject office fact
gets ``valid_until`` + ``superseded_by`` set, and the new one is the single
open ("current") row.

NOTE: the current-world leader name in this test is a FIXTURE INPUT (the test
is checking supersession mechanics, not curating live facts) — the production
seed pulls the real value from Wikidata (egress verified reachable, term-start
2025-01-20). The test deliberately uses placeholder names so it asserts
mechanics, not a hand-written world fact.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.seed import run_seed_source
from legba.data.seed.adapters.wikidata_leaders import WikidataLeadersSeedSource


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


def _leader_binding(country: str, leader: str, start: str) -> dict:
    """Shape a single WDQS leaders binding (the SPARQL JSON results form)."""
    return {
        "country": {"value": f"http://www.wikidata.org/entity/{country[:3].upper()}"},
        "countryLabel": {"value": country},
        "leader": {"value": f"http://www.wikidata.org/entity/{leader[:3].upper()}"},
        "leaderLabel": {"value": leader},
        "role": {"value": "head_of_state"},
        "start": {"value": start},
    }


async def _run_pull(pg_pool, country: str, leader: str, start: str):
    """Run wikidata_leaders offline with one leader binding for `country`."""
    adapter = WikidataLeadersSeedSource()
    fixture = {"leaders": [_leader_binding(country, leader, start)], "alliances": []}
    return await run_seed_source(
        pg_pool, adapter, dry_run=False, options={"sparql_json": fixture},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_leader_change_supersedes_prior_office_fact(pg_pool):
    # Use a unique synthetic country so the test is isolated from any seeded
    # G20 data already present in the shared dev DB.
    country = f"Testland_{datetime.now(tz=timezone.utc).timestamp():.0f}"
    old_leader = "Old Officeholder"
    new_leader = "New Officeholder"

    # 1) Seed the prior officeholder.
    r1 = await _run_pull(pg_pool, country, old_leader, "+2017-01-20T00:00:00Z")
    assert not r1.errors, r1.errors

    async with pg_pool.acquire() as conn:
        # The country-subject office fact is OPEN and points at the old leader.
        # (predicate normalizes to canonical lowercase-spaced 'head of state').
        old = await conn.fetchrow(
            "SELECT id, value, valid_until, superseded_by FROM facts "
            "WHERE lower(subject)=lower($1) AND predicate='head of state' "
            "AND valid_until IS NULL AND superseded_by IS NULL",
            country,
        )
        assert old is not None, "prior country-subject office fact must be open"
        assert old["value"] == old_leader
        old_id = old["id"]

    # 2) A fresh pull names a DIFFERENT leader for the SAME country.
    r2 = await _run_pull(pg_pool, country, new_leader, "+2025-01-20T00:00:00Z")
    assert not r2.errors, r2.errors

    async with pg_pool.acquire() as conn:
        # The prior office fact is now CLOSED: valid_until set + superseded_by
        # pointing at the new row (temporal honesty — NOT a duplicate).
        closed = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM facts WHERE id=$1", old_id
        )
        assert closed["valid_until"] is not None, "prior leader must be closed"
        assert closed["superseded_by"] is not None, "prior must point at successor"

        # Exactly ONE open country-subject office fact remains, and it is the
        # new leader — the single "who currently holds office" row grounding
        # reads.
        open_rows = await conn.fetch(
            "SELECT value FROM facts "
            "WHERE lower(subject)=lower($1) AND predicate='head of state' "
            "AND valid_until IS NULL AND superseded_by IS NULL",
            country,
        )
        assert len(open_rows) == 1, "exactly one current officeholder (no dup)"
        assert open_rows[0]["value"] == new_leader
        # And the successor row is the one the prior points at.
        successor = await conn.fetchrow(
            "SELECT id FROM facts WHERE lower(subject)=lower($1) "
            "AND predicate='head of state' AND value=$2 "
            "AND valid_until IS NULL AND superseded_by IS NULL",
            country, new_leader,
        )
        assert successor["id"] == closed["superseded_by"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pull_emits_both_leader_of_and_office_fact(pg_pool):
    """A single leader pull writes BOTH the subject=leader `LeaderOf` fact
    (graph/read shape) AND the subject=country `head of state` office fact
    (supersession shape) — neither replaces the other."""
    country = f"Factland_{datetime.now(tz=timezone.utc).timestamp():.0f}"
    leader = "Some Leader"
    r = await _run_pull(pg_pool, country, leader, "+2024-01-01T00:00:00Z")
    assert not r.errors, r.errors

    async with pg_pool.acquire() as conn:
        leader_of = await conn.fetchrow(
            "SELECT 1 FROM facts WHERE lower(subject)=lower($1) "
            "AND predicate='leader of' AND lower(value)=lower($2) "
            "AND valid_until IS NULL AND superseded_by IS NULL",
            leader, country,
        )
        office = await conn.fetchrow(
            "SELECT 1 FROM facts WHERE lower(subject)=lower($1) "
            "AND predicate='head of state' AND lower(value)=lower($2) "
            "AND valid_until IS NULL AND superseded_by IS NULL",
            country, leader,
        )
        assert leader_of is not None, "subject=leader 'leader of' fact present"
        assert office is not None, "subject=country 'head of state' fact present"
