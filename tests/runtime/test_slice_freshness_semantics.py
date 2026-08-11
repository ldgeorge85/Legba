# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B-2 — the substrate slice's freshness contract, pinned after B-1.

THE QUESTION B-2 ASKS. The 08-03 incident put a frozen 07-28 AP snapshot at the
top of five country desks' 72h slices for six days. Two things could have been
at fault: the timestamp (``fetched_at`` was being advanced by unchanged
re-serves) or the slice (which both WINDOWS and ORDERS on that timestamp,
``actor_substrate_slice.py:330,377``). B-1 fixed the timestamp. B-2 asks whether
the slice ALSO needs changing — and the answer, measured against the live
substrate and pinned by the tests below, is NO.

WHAT THE MEASUREMENT SHOWED (live substrate, 2026-08-03, read-only):

  * The incident, at the desk: the North Korea desk's 72h slice held 34 rows, and
    21 of them — 62% — were the frozen AP feed. Every one of those 21 carried a
    ``fetched_at`` advanced by 5d16h-5d18h past its real fetch. B-1 plus the 0141
    backfill rewinds them to 07-28, at which point they age out of the window on
    their own. Nothing about the slice needed to change for that to happen.

  * The residue, bounded: the AP feeds were not the only rows carrying an
    advanced timestamp — NWS (1,206 rows), EONET (260), telegram (244), GDACS
    (168) and 20 smaller sources hold ~2,000 more. Those are the DESIGNED case
    (hazard feeds re-serving active events), they are deliberately out of 0141's
    scope, and they self-clear: nothing re-bumps them after B-1, and their
    advanced timestamps are all in the past, so every one of them falls out of a
    72h window within 72h of deploy. A slice change would not clear them faster.

  * The flood, capped: the "legit-update flood pins the slice" hypothesis was
    tested on the live US desk, the worst case in the fleet. Pre-cap, the top 360
    rows by ``fetched_at`` were 50% NWS and 27% GDELT. Post-cap the same slice is
    28 distinct sources with NWS at 24/120 (20%) and a top-2 share of 32%.
    ``_diversify_by_source`` holds. It is not a pin, and B-1 makes it strictly
    better: an unchanged NWS re-serve no longer refreshes its own position.

WHAT B-1 DID COST, STATED PLAINLY. A hazard alert that stays in force for a week
while the source re-serves it unchanged now ages out of a 72h slice after three
days, where before it stayed pinned. That reading — "still in force" — was real,
but it was riding on a field that means "when we fetched this", and every other
consumer of that field paid for the overload. The reading now has its own field,
``last_seen_at``, so a consumer that genuinely wants currently-active hazards can
ask for them; today none does. That is the correct layering, not a regression.

CONCLUSION: no change to slice semantics. The remaining slice question — that the
120-row recency cap is a RECENCY bound where a relevance bound belongs, and that
``_diversify_by_source``'s overflow back-fill lets a capped source exceed its own
cap (NWS 24 against a cap of 15, above) — is real, pre-existing, unaffected by
B-1, and already scoped as C-2 in the remediation roadmap. It is not a freshness
defect and is deliberately not fixed here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.runtime.actor_substrate_slice import _read_substrate_slice


#: Every row this module writes carries this source id, and the fixture cleans up
#: by source id rather than truncating ``signals``. ``migrated_pg`` is SESSION
#: scoped — a table-wide DELETE here would silently pull the rug out from under
#: any other test in the same run that seeded signals in a broader-scoped fixture.
_SRC = "source.b2.freshness_fixture"


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
    )
    try:
        await c.execute("DELETE FROM signals WHERE source_id = $1", _SRC)
        yield c
    finally:
        await c.execute("DELETE FROM signals WHERE source_id = $1", _SRC)
        await c.close()


def _descriptor(window_hours: int = 72) -> SimpleNamespace:
    """A non-``inline_target`` kind, so the slice's desk-grounding leg (which is
    gated on that kind) stays out of the way of the freshness assertion."""
    return SimpleNamespace(
        identity=SimpleNamespace(id="predictor", kind="predictor"),
        subscription=SimpleNamespace(
            substrate={},
            targets=SimpleNamespace(time_window=f"{window_hours}h"),
        ),
    )


async def _seed(conn, *, title: str, fetched_at, last_seen_at=None) -> None:
    await conn.execute(
        "INSERT INTO signals (id, source_id, owner_tenant, modality, payload, "
        "content_hash, fetched_at, created_at, last_seen_at) "
        "VALUES ($1,$2,'default','text',$3::jsonb,$4,$5,$5,$6)",
        uuid4(), _SRC, json.dumps({"title": title}),
        f"h_{uuid4().hex}", fetched_at, last_seen_at,
    )


async def _titles(conn) -> list[str]:
    """Titles from the REAL slice reader, narrowed to this module's rows.

    The narrowing is what lets the fixture clean up by source id instead of
    truncating the table: whatever else the session has in ``signals``, these
    assertions only speak about rows this module wrote — and relative order among
    them is preserved, which is all the ordering tests need."""
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor(), target_filter=None,
    )
    return [
        r["title"] for r in rows
        if r.get("title") and r.get("source_id") == _SRC
    ]


@pytest.mark.asyncio
async def test_a_re_served_frozen_row_ages_out_of_the_window(conn):
    """The incident's mechanism, inverted.

    A row fetched six days ago and re-served unchanged ever since carries an OLD
    ``fetched_at`` and a NOW ``last_seen_at``. It must fall outside a 72h window
    — being re-served is not being fresh. Before B-1 this row's ``fetched_at``
    was the re-serve time, so it not only passed the window, it led the order."""
    now = datetime.now(timezone.utc)
    await _seed(conn, title="frozen 07-28 story",
                fetched_at=now - timedelta(days=6), last_seen_at=now)
    await _seed(conn, title="genuinely new story", fetched_at=now)

    titles = await _titles(conn)
    assert "genuinely new story" in titles
    assert "frozen 07-28 story" not in titles


@pytest.mark.asyncio
async def test_a_genuine_update_enters_the_window_and_leads(conn):
    """The converse the fix must not break.

    Changed content hashes differently, so it lands a NEW row with its own fresh
    ``fetched_at`` — it enters the window and, being the newest fetch, leads the
    ordering. This is what keeps EONET/GDACS event updates surfacing."""
    now = datetime.now(timezone.utc)
    await _seed(conn, title="event v1", fetched_at=now - timedelta(hours=10))
    await _seed(conn, title="event v2 (updated)", fetched_at=now)

    titles = await _titles(conn)
    assert titles[0] == "event v2 (updated)"
    assert "event v1" in titles


@pytest.mark.asyncio
async def test_recent_re_serve_does_not_outrank_a_newer_fetch(conn):
    """Ordering, not just windowing.

    A stale row re-served one second ago must still sort BELOW a genuinely newer
    fetch. This is the ordering half of ``actor_substrate_slice.py:377`` — the
    half that decided which story topped five desks for six days."""
    now = datetime.now(timezone.utc)
    await _seed(conn, title="stale but re-served just now",
                fetched_at=now - timedelta(hours=60), last_seen_at=now)
    await _seed(conn, title="fetched an hour ago",
                fetched_at=now - timedelta(hours=1))

    titles = await _titles(conn)
    assert titles.index("fetched an hour ago") < titles.index(
        "stale but re-served just now")


@pytest.mark.asyncio
async def test_the_window_is_the_descriptors_and_nothing_shifts_it(conn):
    """B-1 changes which rows are honest, not how wide the window is. A row just
    inside the descriptor's window is in; one just outside is out; ``last_seen_at``
    has no vote either way."""
    now = datetime.now(timezone.utc)
    await _seed(conn, title="inside 72h", fetched_at=now - timedelta(hours=71))
    await _seed(conn, title="outside 72h", fetched_at=now - timedelta(hours=73),
                last_seen_at=now)

    titles = await _titles(conn)
    assert "inside 72h" in titles
    assert "outside 72h" not in titles
