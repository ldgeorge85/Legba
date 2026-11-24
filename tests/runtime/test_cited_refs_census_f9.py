# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W4b F-9 - the provenance census read, on the REAL driver.

Three things, each proven against the migrated test database through asyncpg:

* ``events`` fold into ``by_origin_class`` (their own ``origin_class`` column,
  read off the row) - a golden over signals + observations + events;
* the read is BOUNDED at ``CITED_REFS_CLASSIFY_CAP`` and the remainder is
  reported as ``unclassified_over_cap``, never dropped and never filed as
  fabricated;
* the recovery route's ``unavailable`` branch runs the census - a persisted
  turn that cites events has a ``provenance_census`` after the recovery.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.analysts.consult_provenance_census import build_provenance_census
from legba.runtime import substrate_cited_refs as cited
from legba.runtime.substrate_query_port import (
    CITED_REFS_CLASSIFY_CAP,
    PostgresQdrantSubstrateQueryPort,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_TAG = f"f9census_{uuid4().hex[:8]}"
_COLLECTION = f"collection.{_TAG}"
_SOURCE = f"source.{_TAG}"
_EVENT_SIG = f"sig.{_TAG}"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    yield pool
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM signals WHERE source_id = $1", _SOURCE)
        await conn.execute(
            "DELETE FROM observations WHERE collection_id = $1", _COLLECTION)
        await conn.execute(
            "DELETE FROM events WHERE event_signature LIKE $1", f"{_EVENT_SIG}%")
    await pool.close()


async def _seed(conn) -> dict[str, list[str]]:
    """3 signals (2 live, 1 web_retrieval), 2 observations (archive), 4 events
    (3 live, 1 web_retrieval; the table CHECK only admits live/web_retrieval/seed)."""
    ids: dict[str, list[str]] = {"signal": [], "observation": [], "event": []}
    for klass in ("live", "live", "web_retrieval"):
        sid = uuid4()
        await conn.execute(
            "INSERT INTO signals (id, source_id, source_version, payload, "
            "content_hash, fetched_at, created_at, updated_at, schema_uri, "
            "origin_class) VALUES ($1, $2, 'v1', '{}'::jsonb, $3, now(), now(), "
            "now(), 'iglu:legba/signal/jsonschema/1-0-0', $4)",
            sid, _SOURCE, f"h-{sid}", klass,
        )
        ids["signal"].append(str(sid))
    for i in range(2):
        await conn.execute(
            "INSERT INTO observations (collection_id, series_id, subject_kind, "
            "subject, valid_from, valid_to, record_time, value, unit, "
            "source_url, sha256, origin_class, provenance) VALUES ($1, $2, "
            "'country', 'IR', '2016-01-01', '2017-01-01', now(), $3, 'x', "
            "'https://example.invalid/s.json', 'deadbeef', 'archive', '{}'::jsonb)",
            _COLLECTION, f"series-{i}", 1.0 + i,
        )
    ids["observation"] = [
        str(r["id"]) for r in await conn.fetch(
            "SELECT id FROM observations WHERE collection_id = $1", _COLLECTION)
    ]
    for i, klass in enumerate(("live", "live", "live", "web_retrieval")):
        eid = uuid4()
        await conn.execute(
            "INSERT INTO events (id, event_signature, analyst_id, title, "
            "origin_class) VALUES ($1, $2, 'test', $3, $4)",
            eid, f"{_EVENT_SIG}.{i}", f"event {i} {_TAG}", klass,
        )
        ids["event"].append(str(eid))
    return ids


async def test_events_fold_into_the_class_buckets_golden(pg_pool):
    async with pg_pool.acquire() as conn:
        ids = await _seed(conn)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)
    refs = ids["signal"] + ids["observation"] + ids["event"]
    out = await port.classify_cited_refs(refs=refs)
    assert out["by_origin_class"] == {
        "live": 5, "web_retrieval": 2, "archive": 2,
    }
    assert out["by_kind"] == {"signal": 3, "observation": 2, "event": 4}
    # An event now RESOLVES to a class-bearing row; it is no longer filed as
    # "matched a row whose class this census does not read".
    assert out["resolved"] == 9
    assert out["unresolved"] == 0
    assert out["matched"] == 9
    assert out["fabricated"] == 0
    assert out["asked"] == 9
    assert out["unclassified_over_cap"] == 0

    census = build_provenance_census(
        answer="A claim with nothing behind it.",
        cited_refs=refs,
        classification=out,
    )
    assert (census["live"], census["web_retrieval"], census["history"]) == (5, 2, 2)
    assert census["cited_total"] == 9
    assert census["by_kind"] == {"signal": 3, "observation": 2, "event": 4}


async def test_the_read_is_bounded_and_the_remainder_is_reported(pg_pool):
    assert CITED_REFS_CLASSIFY_CAP == 400
    async with pg_pool.acquire() as conn:
        ids = await _seed(conn)
    real = ids["signal"] + ids["event"]  # 7 real refs, cited FIRST
    extra_fake = [str(uuid4()) for _ in range(CITED_REFS_CLASSIFY_CAP - len(real))]
    # Past the cap: one REAL ref (must not be looked up), 4 fakes, a malformed
    # token (reported as malformed, not as over-cap), and a duplicate.
    past = [ids["observation"][0]] + [str(uuid4()) for _ in range(4)]
    refs = real + extra_fake + past + ["not-a-uuid", real[0]]
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)
    out = await port.classify_cited_refs(refs=refs)
    assert out["asked"] == CITED_REFS_CLASSIFY_CAP
    assert out["unclassified_over_cap"] == 5
    assert out["malformed"] == ["not-a-uuid"]
    assert out["matched"] == 7
    # Over-cap refs were never looked up, so they are NOT fabricated and the
    # real observation past the cap is NOT counted.
    assert out["fabricated"] == CITED_REFS_CLASSIFY_CAP - 7
    assert "observation" not in out["by_kind"]
    assert out["by_kind"] == {"signal": 3, "event": 4}

    census = build_provenance_census(
        answer="x", cited_refs=refs, classification=out)
    assert census["unclassified_over_cap"] == 5
    assert census["cited_total"] == CITED_REFS_CLASSIFY_CAP + 5


async def test_each_probe_sees_at_most_the_cap():
    """However many refs are cited, each probe is handed at most CAP ids."""
    seen: list[int] = []

    class _Spy:
        def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch(self, _sql, wanted):
            seen.append(len(wanted))
            return []

    out = await cited.classify_cited_refs(
        _Spy(), refs=[str(uuid4()) for _ in range(1500)])
    assert out["asked"] == 400 and out["unclassified_over_cap"] == 1100
    assert seen and max(seen) == 400
    # signals, observations, events + every kind table (W11-F155 added four)
    assert len(seen) == len(cited._CLASS_TABLES) + len(cited.CITED_REF_TABLES) == 11


class _NoCallLLM:
    subprovider = "anthropic"
    PRICE_TABLE: dict = {}

    async def chat_complete(self, *a, **k):  # pragma: no cover
        raise AssertionError("an unavailable recovery must cost nothing")


async def test_the_unavailable_recovery_runs_the_census_on_the_real_driver(pg_pool):
    """A persisted turn with cited events and neither a transcript nor any
    re-executable call: the recovery refuses to invent an answer, and the
    refusal STILL carries the provenance line for the refs the turn cited."""
    async with pg_pool.acquire() as conn:
        ids = await _seed(conn)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)
    steps: list[dict] = []

    async def _publish(step):
        steps.append(step)

    deps = ConsultOnDemandDeps(llm=_NoCallLLM(), substrate=port, step_publish=_publish)
    result = await run_method(
        [{
            "question": "what happened?",
            "synthesize_from": {
                "question": "what happened?", "steps": [],
                "cited_refs": ids["event"],
            },
        }],
        {}, deps,
    )
    data = result.consult_response.data
    assert data["replay_fidelity"] == "unavailable"
    census = data["provenance_census"]
    assert census["live"] == 3 and census["web_retrieval"] == 1
    assert census["history"] == 0
    assert census["by_kind"] == {"event": 4}
    assert census["cited_total"] == 4
    # Grounded in real rows, so the hold does not fire.
    assert "hold_reason" not in data
    frame = next(s for s in steps if s.get("kind") == "resynthesis_uncertainty")
    assert frame["resolved_refs"] == 4


async def test_the_unavailable_recovery_holds_when_its_citations_resolve_nowhere(pg_pool):
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)

    async def _publish(step):
        return None

    deps = ConsultOnDemandDeps(llm=_NoCallLLM(), substrate=port, step_publish=_publish)
    result = await run_method(
        [{
            "question": "q",
            "synthesize_from": {
                "question": "q", "steps": [],
                "cited_refs": [str(uuid4()), str(uuid4())],
            },
        }],
        {}, deps,
    )
    data = result.consult_response.data
    assert data["provenance_census"]["fabricated"] == 2
    assert data["hold_reason"]
    assert result.consult_response.answer.startswith("> **Held:")
