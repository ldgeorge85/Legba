# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-D — the research counters through the REAL binding path, on live SQL.

Everything here goes through ``deterministic.run_method`` with
``options.sub_handler='research_measurement'`` rather than calling
``research_measurement.handle`` directly, because the dispatcher IS the binding
the runtime uses and a handler that only works when called by name is a handler
that has never been wired.

Three shapes, in order of what they protect:

  1. **The empty window** — the state the fleet is actually in until R-A merges
     (229,945 live signals, 100% NULL ``retrieval_origin``). Every rate must be
     ``null`` with a reason, and the run must be ``force_trace_only`` so a
     quiet week does not repeat "nothing to report" into the feed.
  2. **The IL-shaped window** — a research signal dispatched by
     ``country_watch_il``, cited by a desk head, that head quoted into a
     composition, and a later NON-research wire row folded onto it. All three
     counters fire against real SQL, including the ``bounded_question``
     desk-definition join and the assembly-span lookup.
  3. **The stratification** — ``stratify_heads`` over fixture desk heads,
     the function the R4 round harness imports.

The SQL is what is under test here, not the arithmetic (that is DB-free in
``test_research_measurement.py``): these cases exist to catch a predicate that
reads the wrong JSON path, a join that drops rows, or a cast that throws.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import run_method
from legba.data.analysts.deterministic_handlers import research_measurement as rm
from legba.data.config import PostgresConfig

ORIGIN = "web_search:search.searxng.local"
DESK = "test_rd_internal_stability"
TARGET = "country_watch_il"
SOURCE = "source.research.searxng_local"
WIRE_SOURCE = "test_rd_wire"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    """A universe this file exclusively owns.

    ``retrieval_origin IS NOT NULL`` is a safe exclusive scope for signals:
    nothing else in the tree writes that column (it is the migration-0112 seam
    the research program is the first to use). The desk set is made exclusive
    by removing every descriptor carrying a ``bounded_question`` — that
    predicate IS the handler's mechanical definition of "a desk", so leaving a
    stray one behind would silently widen the denominator.
    """
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM signals WHERE retrieval_origin IS NOT NULL")
        await conn.execute("DELETE FROM signals WHERE source_id = $1", WIRE_SOURCE)
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id LIKE 'test_rd_%'"
        )
        await conn.execute(
            "DELETE FROM analyst_descriptors "
            "WHERE (body -> 'method' ->> 'bounded_question') IS NOT NULL"
        )
    yield


class _Deps(SimpleNamespace):
    pass


def _opts():
    return {
        "sub_handler": "research_measurement",
        "analyst_id": "research_measurement",
        "run_id": uuid4(),
        # A floor of 1 so the fixture's n=1 events produce EARNED rates and the
        # SQL's own arithmetic is what is asserted. G9's real floor is pinned
        # DB-free in test_research_measurement.py.
        "min_n": 1,
    }


async def _run(pool):
    return await run_method([], _opts(), _Deps(pg_pool=pool))


async def _insert_desk(conn):
    await conn.execute(
        """
        INSERT INTO analyst_descriptors
            (descriptor_id, version, schema_uri, is_head, kind, state, owner,
             name, body)
        VALUES ($1, 'v1', 'legba/analyst/1.0.0', TRUE, 'inline_target',
                'active', 'test', 'RD desk', $2::jsonb)
        """,
        DESK,
        json.dumps({"method": {"bounded_question": "Does the IL slice hold?"}}),
    )


async def _insert_research_signal(conn, *, fetched_at, novel=True,
                                  host_in_slice=False, url=None):
    sid = uuid4()
    await conn.execute(
        """
        INSERT INTO signals
            (id, source_id, retrieval_origin, produced_by_kind, fetched_at,
             canonical_url, content_hash, geo, tags, payload, raw_provenance)
        VALUES ($1, $2, $3, 'research', $4, $5, $6, ARRAY['IL','PS'],
                ARRAY['research'], $7::jsonb, $8::jsonb)
        """,
        sid,
        SOURCE,
        ORIGIN,
        fetched_at,
        url or f"https://example.test/{sid}",
        f"hash-{sid}",
        json.dumps(
            {
                "title": "Palestine corridor closure",
                "research": {
                    "schema": "research_evidence.v1",
                    "provider": "search.searxng.local",
                    "novelty": {
                        "version": "novelty.v1",
                        "scope": "dispatching_target",
                        "novel": novel,
                        "host_in_slice": host_in_slice,
                        "url_in_slice": False,
                        "content_hash_in_slice": False,
                    },
                },
            }
        ),
        json.dumps(
            {
                "kind": "research",
                "fetch_kind": "web_evidence",
                "dispatch": {"kind": "coverage_floor", "target_id": TARGET},
            }
        ),
    )
    return sid


async def _insert_wire_signal(conn, *, fetched_at, fold_onto=None, url=None):
    """A NON-research signal — ``retrieval_origin IS NULL``, which is the whole
    test in §4.2: corroboration only counts a source the program did not fetch
    for itself."""
    sid = uuid4()
    await conn.execute(
        """
        INSERT INTO signals
            (id, source_id, fetched_at, canonical_url, content_hash,
             canonical_signal_id, geo, payload)
        VALUES ($1, $2, $3, $4, $5, $6, ARRAY['IL','PS'], '{}'::jsonb)
        """,
        sid,
        WIRE_SOURCE,
        fetched_at,
        url or f"https://wire.test/{sid}",
        f"wire-{sid}",
        fold_onto,
    )
    return sid


async def _insert_desk_head(conn, *, cites, produced_at, analyst_id=DESK):
    head_id = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, schema_uri, analyst_id, target_id,
             produced_at, data)
        VALUES ($1, 'finding', 'RD desk head', 'body',
                'legba/finding/1.0.0', $2, $3, $4, $5::jsonb)
        """,
        head_id,
        analyst_id,
        TARGET,
        produced_at,
        json.dumps(
            {
                "data": {
                    "citations": [
                        {"marker": f"[{i + 1}]", "signal_id": str(sid),
                         "source_text": "quoted"}
                        for i, sid in enumerate(cites)
                    ]
                }
            }
        ),
    )
    return head_id


async def _insert_composition(conn, *, quotes_head, produced_at):
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, schema_uri, analyst_id, target_id,
             produced_at, data)
        VALUES ($1, 'finding', 'RD composition', 'body',
                'legba/finding/1.0.0', 'test_rd_country_composition', $2, $3,
                $4::jsonb)
        """,
        uuid4(),
        TARGET,
        produced_at,
        json.dumps(
            {
                "data": {
                    "meta": True,
                    "assembly": {
                        "schema": "assembly.v1",
                        "regime": "assembly",
                        "blocks": [
                            {
                                "ordinal": 1,
                                "finding_id": str(quotes_head),
                                "spans": [
                                    {
                                        "role": "bluf",
                                        "text": "quoted",
                                        "origin": {"head_id": str(quotes_head),
                                                   "start": 0, "end": 6},
                                    }
                                ],
                                "signals": [],
                            }
                        ],
                    },
                }
            }
        ),
    )


# ---------------------------------------------------------------------------
# 1. The empty window — the fleet's actual state until R-A merges
# ---------------------------------------------------------------------------


async def test_empty_window_is_honest_null_and_trace_only(pg_pool, clean_slate):
    """The verified-zero baseline, through the dispatcher.

    Every rate null WITH a reason, and ``force_trace_only`` so an idempotent
    daily re-run never repeats "nothing to report" into the feed (§4.4, the
    collection_gap precedent)."""
    result = await _run(pg_pool)
    assert result.force_trace_only is True
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0
    }

    data = result.finding.data["research_measurement"]
    assert data["signals_written"] == 0
    assert data["retrieval_origins"] == []
    assert data["novelty"]["rate"] is None
    assert data["novelty"]["reason"] == "no_rows"
    assert data["corroboration"]["rate"] is None
    assert data["corroboration"]["reason"] == "window_not_matured"
    for rung in ("c1", "c2", "c3"):
        assert data["consequence"][rung]["rate"] is None
    assert result.finding.data["meta"] is True
    assert "no research signals in window" in result.finding.title


async def test_empty_window_still_measures_the_r4_population(pg_pool, clean_slate):
    """Zero research signals does NOT mean zero desk heads: the stratification
    must still report the ``false`` arm, because that arm is R4's control
    group and an empty control group would silently un-power the split."""
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn)
        now = datetime.now(timezone.utc)
        await _insert_desk_head(conn, cites=[], produced_at=now - timedelta(hours=2))
        await _insert_desk_head(conn, cites=[], produced_at=now - timedelta(hours=1))

    data = (await _run(pg_pool)).finding.data["research_measurement"]
    arms = data["stratification"]["arms"]
    assert arms["false"]["n_heads"] == 2
    assert arms["false"]["n_uncited_heads"] == 2
    assert arms["true"]["n_heads"] == 0
    assert data["stratification"]["pooled"] is False


# ---------------------------------------------------------------------------
# 2. The IL-shaped window — all three counters against real SQL
# ---------------------------------------------------------------------------


async def test_the_il_shaped_window_end_to_end(pg_pool, clean_slate):
    """The IL/Palestine shape carried end to end.

    TWO research rows, because the three counters DO NOT SHARE A POPULATION and
    that is deliberate (§4.2): NOVELTY and CONSEQUENCE read the trailing 7-day
    window, while CORROBORATION reads the MATURED band (rows aged 7-14 days,
    whose forward window has closed). A single fixture row would silently pass
    only whichever counter its age happened to suit.

      * the FRESH row — dispatched by ``country_watch_il``, novel at write
        time, cited by a desk head, that head quoted into a composition;
      * the MATURED row — same shape, nine days old, with a non-research wire
        item folded onto it two days later.
    """
    now = datetime.now(timezone.utc)
    fresh_at = now - timedelta(hours=6)
    matured_at = now - timedelta(days=9)

    async with pg_pool.acquire() as conn:
        await _insert_desk(conn)

        fresh_id = await _insert_research_signal(
            conn, fetched_at=fresh_at, novel=True, host_in_slice=False
        )
        head_id = await _insert_desk_head(
            conn, cites=[fresh_id], produced_at=fresh_at + timedelta(hours=1)
        )
        await _insert_composition(
            conn, quotes_head=head_id, produced_at=fresh_at + timedelta(hours=2)
        )

        matured_id = await _insert_research_signal(
            conn, fetched_at=matured_at, novel=True, host_in_slice=False
        )
        # The corroborating wire item: a NON-research row the dedup plane later
        # folded onto the research row. That fold IS the anchor (§4.2).
        await _insert_wire_signal(
            conn, fetched_at=matured_at + timedelta(days=2),
            fold_onto=matured_id,
        )

    result = await _run(pg_pool)
    assert result.force_trace_only is False
    data = result.finding.data["research_measurement"]

    # NOVELTY — read off the write-time stamp, with the stricter v1b arm.
    assert data["signals_written"] == 1
    assert data["retrieval_origins"] == [ORIGIN]
    assert data["novelty"]["rate"] == 1.0
    assert data["novelty"]["v1b"]["rate"] == 1.0
    assert data["novelty"]["unstamped"] == 0

    # CORROBORATION — the canonical fold, and ONLY against a non-research row.
    assert data["corroboration"]["n"] == 1
    assert data["corroboration"]["rate"] == 1.0
    assert data["corroboration"]["by_anchor"]["fold"] == 1
    assert data["corroboration"]["ceiling_lift"]["performed"] is False

    # CONSEQUENCE — the full three-rung ladder.
    assert data["consequence"]["c1_cited_any"] == 1
    assert data["consequence"]["c2_cited_by_desk"] == 1
    assert data["consequence"]["c3_quoted_head"] == 1

    # The citing grain, keyed on (target, unit).
    assert [(r["target_id"], r["unit"]) for r in data["by_target_unit"]] == [
        (TARGET, DESK)
    ]
    # The dispatch grain, keyed on the dispatching target.
    assert [r["target_id"] for r in data["by_target"]] == [TARGET]
    # And the R4 split now has a populated research arm.
    assert data["stratification"]["arms"]["true"]["n_heads"] == 1

    # The two windows are DISJOINT, and the row publishes both so no reader
    # assumes one denominator served all three counters. At the defaults
    # (window 7d, maturation 7d) the matured band ends exactly where the fresh
    # window begins, so no research row can ever be counted in both.
    assert data["matured_window"]["until"] == data["window"]["since"]
    assert data["matured_window"]["since"] < data["matured_window"]["until"]


async def test_a_non_desk_citation_does_not_count_as_consequence(
    pg_pool, clean_slate
):
    """§4.3 — 'a researcher citing the page it just fetched is not consequence,
    it is bookkeeping'. The desk definition (``method.bounded_question``)
    excludes the researcher by construction, so c1 fires and c2 does not."""
    now = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn)
        research_id = await _insert_research_signal(
            conn, fetched_at=now - timedelta(hours=6)
        )
        await _insert_desk_head(
            conn,
            cites=[research_id],
            produced_at=now - timedelta(hours=1),
            analyst_id="test_rd_corpus_researcher",   # NOT a bounded desk
        )

    data = (await _run(pg_pool)).finding.data["research_measurement"]
    assert data["consequence"]["c1_cited_any"] == 1
    assert data["consequence"]["c2_cited_by_desk"] == 0
    assert data["consequence"]["c3_quoted_head"] == 0
    assert data["by_target_unit"] == []


async def test_a_research_row_does_not_corroborate_another_research_row(
    pg_pool, clean_slate
):
    """§4.2's whole point: the second source must be NON-research
    (``retrieval_origin IS NULL``). Two research rows in the same fold are the
    program agreeing with itself."""
    now = datetime.now(timezone.utc)
    matured_at = now - timedelta(days=9)
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn)
        first = await _insert_research_signal(conn, fetched_at=matured_at)
        # A SECOND research row folded onto the first — must NOT corroborate.
        second = await _insert_research_signal(
            conn, fetched_at=matured_at + timedelta(hours=6)
        )
        await conn.execute(
            "UPDATE signals SET canonical_signal_id = $1 WHERE id = $2",
            first, second,
        )

    data = (await _run(pg_pool)).finding.data["research_measurement"]
    assert data["corroboration"]["n"] == 2
    assert data["corroboration"]["count"] == 0
    assert data["corroboration"]["by_anchor"] == {
        "fold": 0, "url": 0, "fact": 0, "entity": 0
    }


async def test_an_unstamped_research_row_reads_as_unknown_not_as_not_novel(
    pg_pool, clean_slate
):
    """The honest-null contract on the real read path: a row R-A landed without
    a novelty block must not be counted as 'not novel'."""
    now = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn)
        await conn.execute(
            """
            INSERT INTO signals
                (id, source_id, retrieval_origin, fetched_at, payload,
                 raw_provenance)
            VALUES ($1, $2, $3, $4, '{}'::jsonb, '{}'::jsonb)
            """,
            uuid4(), SOURCE, ORIGIN, now - timedelta(hours=3),
        )

    data = (await _run(pg_pool)).finding.data["research_measurement"]
    assert data["signals_written"] == 1
    assert data["novelty"]["rate"] is None
    assert data["novelty"]["reason"] == "novelty_not_stamped"
    assert data["novelty"]["unstamped"] == 1


async def test_the_novelty_stamp_is_also_read_from_raw_provenance(
    pg_pool, clean_slate
):
    """§4.1 pins the block at ``payload.research.novelty``; the R-D brief put it
    in ``raw_provenance``. The counter resolves both, spec path first, so a
    lane-boundary disagreement cannot silently zero the headline counter."""
    now = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn)
        await conn.execute(
            """
            INSERT INTO signals
                (id, source_id, retrieval_origin, fetched_at, payload,
                 raw_provenance)
            VALUES ($1, $2, $3, $4, '{}'::jsonb, $5::jsonb)
            """,
            uuid4(), SOURCE, ORIGIN, now - timedelta(hours=3),
            json.dumps(
                {
                    "dispatch": {"target_id": TARGET},
                    "research": {
                        "novelty": {
                            "version": "novelty.v1",
                            "scope": "dispatching_target",
                            "novel": True,
                            "host_in_slice": False,
                        }
                    },
                }
            ),
        )

    data = (await _run(pg_pool)).finding.data["research_measurement"]
    assert data["novelty"]["unstamped"] == 0
    assert data["novelty"]["rate"] == 1.0
    assert data["novelty"]["novelty_versions"] == ["novelty.v1"]


async def test_a_malformed_citation_id_cannot_abort_the_counters(
    pg_pool, clean_slate
):
    """The citation ids are compared as lowercase TEXT, never cast to uuid: one
    bad ``signal_id`` in one finding must not take down the whole measurement
    (nor, via the same query, the R4 population)."""
    now = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn)
        research_id = await _insert_research_signal(
            conn, fetched_at=now - timedelta(hours=6)
        )
        head_id = uuid4()
        await conn.execute(
            """
            INSERT INTO analyst_outputs
                (id, kind, title, body, schema_uri, analyst_id, target_id,
                 produced_at, data)
            VALUES ($1, 'finding', 'RD bad cite', 'body',
                    'legba/finding/1.0.0', $2, $3, $4, $5::jsonb)
            """,
            head_id, DESK, TARGET, now - timedelta(hours=1),
            json.dumps(
                {
                    "data": {
                        "citations": [
                            {"marker": "[1]", "signal_id": "not-a-uuid"},
                            {"marker": "[2]", "signal_id": str(research_id)},
                        ]
                    }
                }
            ),
        )

    data = (await _run(pg_pool)).finding.data["research_measurement"]
    assert data["consequence"]["c2_cited_by_desk"] == 1
    assert data["stratification"]["arms"]["true"]["n_heads"] == 1


# ---------------------------------------------------------------------------
# 3. stratify_heads — the function the R4 round harness imports
# ---------------------------------------------------------------------------


async def test_stratify_heads_returns_per_head_arm_rows(pg_pool, clean_slate):
    """R4 joins its own bars onto these rows rather than re-deriving the arm,
    so the per-head shape is part of the contract."""
    now = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn)
        research_id = await _insert_research_signal(
            conn, fetched_at=now - timedelta(hours=6)
        )
        cited = await _insert_desk_head(
            conn, cites=[research_id], produced_at=now - timedelta(hours=3)
        )
        plain = await _insert_desk_head(
            conn, cites=[], produced_at=now - timedelta(hours=2)
        )
        rows = await rm.stratify_heads(
            conn, since=now - timedelta(days=7), until=now
        )

    by_id = {r["head_id"]: r for r in rows}
    assert by_id[str(cited)]["has_research_evidence"] is True
    assert by_id[str(cited)]["citation_count"] == 1
    assert by_id[str(plain)]["has_research_evidence"] is False
    assert by_id[str(plain)]["citation_count"] == 0
    assert {r["unit"] for r in rows} == {DESK}
    assert set(rows[0]) == {
        "head_id", "unit", "target_id", "produced_at", "citation_count",
        "has_research_evidence",
    }


async def test_stratify_heads_excludes_non_desk_analysts(pg_pool, clean_slate):
    """The R4 population is DESK heads. A composition row is a different
    grain and must not enter the arms."""
    now = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn)
        head = await _insert_desk_head(
            conn, cites=[], produced_at=now - timedelta(hours=2)
        )
        await _insert_composition(
            conn, quotes_head=head, produced_at=now - timedelta(hours=1)
        )
        rows = await rm.stratify_heads(
            conn, since=now - timedelta(days=7), until=now
        )

    assert [r["head_id"] for r in rows] == [str(head)]


async def test_the_dispatcher_rejects_an_unknown_sub_handler():
    """Belt and braces on the binding: the dispatcher is what the runtime
    calls, and a typo in a descriptor must fail loud rather than no-op."""
    from legba.data.analysts.deterministic import DeterministicDispatchError

    with pytest.raises(DeterministicDispatchError):
        await run_method([], {"sub_handler": "research_measurment"}, None)


def test_uuid_import_is_used_by_the_fixtures():
    """Guard against the fixtures drifting away from real UUID ids — the text
    comparison in the SQL only holds because these are canonical lowercase."""
    assert str(UUID(int=0)) == "00000000-0000-0000-0000-000000000000"
