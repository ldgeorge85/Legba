# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""F5 (K-4 R4 §9) — the question SELF-flag detect surface, on the REAL path.

R4's F5 finding, verified live before this surface was built: the
"flag-only watcher" wrote zero flags EVER because flags fired only for
questions with forward consumers, and ``output_consumption`` held 0 rows for
ALL 112 watched question ids — "``bearing_edges`` is currently the only
artifact ``claim_watch`` produces. If detect-only is the shipped shape, this
is the detect surface, and it is invisible." The fix wires the flag path to
the one thing every watched question has: ITSELF. A matched question the
forward walk finds no consumer for writes one open self-flag
(``output_id = founded_on_id =`` the hypothesis id, reason
``new_evidence_bears_on_unconsumed_question``), bounded to one OPEN flag per
question by the 0107 partial unique index.

REAL BINDING PATH: every run here enters through
``legba.data.analysts.deterministic:run_method`` — the exact ``method.impl``
the registered descriptor names — routed by ``options.sub_handler``, with a
deps object exposing ``pg_pool`` the way ``StandardDeps`` does. No handler is
bound directly and no ``output_consumption`` row is hand-written in a shape
production does not produce: the consumerless scene IS the production scene
(that is the whole finding).

NEW FILE deliberately: the sibling ``test_claim_watch.py`` is under
concurrent test-hygiene work; this suite shares no helper with it and uses
its own analyst/source tags.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest_asyncio

from legba.data.analysts.deterministic import run_method
from legba.data.analysts.deterministic_handlers import claim_watch as cw
from legba.data.analysts.handler_options import HANDLER_OPTIONS
from legba.data.config import PostgresConfig

_ANALYST = "test_kw3_f5"
_SRC = "test_kw3_f5_src"
_SEQ = {"n": 0}

#: Every signal this suite inserts lands at ``now + _STREAM_BASE + n`` — a
#: WEEK out, strictly past anything the sibling suite's cursor-policy tests
#: leave in the shared session database (their controlled stream sits one day
#: out). The matcher seeds its cursor at the NEWEST signal, so a leftover
#: future row from another suite would otherwise park the cursor past this
#: suite's probes and every scene would silently match nothing.
_STREAM_BASE = 7 * 86400.0


# ---------------------------------------------------------------------------
# Rig (self-contained — see the module docstring for why nothing is imported
# from the sibling suite)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    """Fresh matcher state (the sibling suite's reset, on this suite's tags).
    TRUNCATE on review_flags is deliberate — the 0107 forbid-delete trigger
    is row-level (BEFORE DELETE) and does not fire on TRUNCATE."""
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE alert_trigger_watermarks")
        await conn.execute("TRUNCATE bearing_edges")
        await conn.execute("TRUNCATE review_flags")
        await conn.execute("TRUNCATE output_consumption")
        # The scanner is GLOBAL over open questions, so any suite's leftover
        # open questions would ride into this suite's counts.
        await conn.execute("DELETE FROM hypotheses WHERE status = 'open_question'")
        await conn.execute("DELETE FROM facts WHERE analyst_id = $1", _ANALYST)
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = $1", _ANALYST
        )
        await conn.execute(
            "DELETE FROM signal_entity_links WHERE analyst_id = $1", _ANALYST
        )
        await conn.execute(
            "DELETE FROM entity_profiles WHERE analyst_id = $1", _ANALYST
        )
        await conn.execute("DELETE FROM signals WHERE source_id = $1", _SRC)
        await conn.execute(
            "DELETE FROM target_descriptors WHERE owner = $1", _ANALYST
        )
    cw._QUESTION_EMBED_CACHE.clear()
    yield
    # Teardown: this suite's stream sits a WEEK in the future (see
    # _STREAM_BASE) — leaving it behind would park every later newest-signal
    # seeding suite's cursor out there. Remove this suite's rows on the way
    # out; shared tables are reset by each DB suite's own setup.
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM signals WHERE source_id = $1", _SRC)
        await conn.execute(
            "DELETE FROM hypotheses WHERE analyst_id = $1", _ANALYST
        )
        await conn.execute("DELETE FROM facts WHERE analyst_id = $1", _ANALYST)
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = $1", _ANALYST
        )
        await conn.execute(
            "DELETE FROM signal_entity_links WHERE analyst_id = $1", _ANALYST
        )
        await conn.execute(
            "DELETE FROM entity_profiles WHERE analyst_id = $1", _ANALYST
        )
        await conn.execute(
            "DELETE FROM target_descriptors WHERE owner = $1", _ANALYST
        )


class _Deps:
    """The ``pg_pool``-bearing shape ``StandardDeps`` presents to handlers."""

    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool
        self.extras: dict[str, Any] = {}


async def _dispatch(pool: Any, **opts: Any):
    """One matcher pass through the REAL descriptor entry point
    (``deterministic:run_method`` routing on ``options.sub_handler``)."""
    return await run_method(
        [],
        {
            "sub_handler": "claim_watch",
            "analyst_id": "claim_watch",
            "run_id": str(uuid4()),
            **opts,
        },
        _Deps(pool),
    )


def _counters(result: Any) -> dict[str, Any]:
    return dict(result.finding.data)


async def _insert_desk(conn: Any, desk: str) -> None:
    await conn.execute(
        "INSERT INTO target_descriptors (descriptor_id, version, schema_uri, "
        "  is_head, state, owner, name, body) "
        "VALUES ($1, 'v1', 'legba/target/2.0.0', TRUE, 'active', $2, $1, "
        "        $3::jsonb) ON CONFLICT DO NOTHING",
        desk,
        _ANALYST,
        json.dumps({"scope": {"geo": ["IR"], "tags": ["watch"]}}),
    )


async def _insert_entity(conn: Any, name: str) -> UUID:
    eid = uuid4()
    await conn.execute(
        "INSERT INTO entity_profiles "
        "  (id, data, canonical_name, entity_class, analyst_id) "
        "VALUES ($1, '{}'::jsonb, $2, 'organization', $3)",
        eid,
        name,
        _ANALYST,
    )
    return eid


async def _insert_signal(conn: Any, *, geo: tuple[str, ...] = ()) -> UUID:
    _SEQ["n"] += 1
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, geo, fetched_at, payload, "
        " content_hash) "
        "VALUES ($1, $2, $3::text[], now() + make_interval(secs => $4), "
        "        $5::jsonb, $6)",
        sid,
        _SRC,
        list(geo),
        _STREAM_BASE + float(_SEQ["n"]),
        json.dumps({"title": f"kw3-f5 signal {_SEQ['n']}"}),
        uuid4().hex,
    )
    return sid


async def _link_all(conn: Any, signal_id: UUID, entity_ids: Any) -> None:
    for eid in entity_ids:
        await conn.execute(
            "INSERT INTO signal_entity_links (signal_id, entity_id, "
            " analyst_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            signal_id,
            eid,
            _ANALYST,
        )


async def _insert_fact(conn: Any, derived_from: list[UUID]) -> UUID:
    fid = uuid4()
    await conn.execute(
        "INSERT INTO facts (id, subject, predicate, value, analyst_id, "
        " derived_from) VALUES ($1, 'kw3-f5 subject', 'involved in', $2, $3, "
        " $4::uuid[])",
        fid,
        f"kw3-f5-{fid.hex}",
        _ANALYST,
        derived_from,
    )
    return fid


async def _insert_question(
    conn: Any, thesis: str, *, desk: str, derived_from: list[UUID]
) -> UUID:
    qid = uuid4()
    await conn.execute(
        "INSERT INTO hypotheses (id, thesis, status, target_id, analyst_id, "
        "  derived_from, produced_at) "
        "VALUES ($1, $2, 'open_question', $3, $4, $5::uuid[], now())",
        qid,
        thesis,
        desk,
        _ANALYST,
        derived_from,
    )
    return qid


async def _matchable_question(
    conn: Any,
    *,
    thesis: str,
    desk: str = "kw3_f5_desk",
    ents: tuple[UUID, ...] | None = None,
) -> tuple[UUID, tuple[UUID, ...]]:
    """A question whose lineage carries two canonical entities on a geo-IR
    desk — the minimum matchable scene (2 shared entities 0.38 + geo 0.10 =
    0.48 >= 0.45)."""
    await _insert_desk(conn, desk)
    if ents is None:
        ents = (
            await _insert_entity(conn, f"KW3F5 Org {uuid4().hex[:8]}"),
            await _insert_entity(conn, f"KW3F5 Org {uuid4().hex[:8]}"),
        )
    lineage_sig = await _insert_signal(conn)
    await _link_all(conn, lineage_sig, ents)
    fact = await _insert_fact(conn, [lineage_sig])
    qid = await _insert_question(conn, thesis, desk=desk, derived_from=[fact])
    return qid, ents


async def _insert_live_consumer(conn: Any, consumed: UUID) -> UUID:
    cid = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, analyst_id, schema_uri) "
        "VALUES ($1, 'finding', $2, '', 0.9, '{}'::jsonb, $3, "
        "        'iglu:legba/finding/jsonschema/1-0-0')",
        cid,
        f"kw3-f5 consumer {cid}",
        _ANALYST,
    )
    await conn.execute(
        "INSERT INTO output_consumption "
        "  (consumer_id, consumed_id, consumer_kind, context) "
        "VALUES ($1, $2, 'meta_findings_synthesizer', 'composition_basis') "
        "ON CONFLICT DO NOTHING",
        cid,
        consumed,
    )
    return cid


async def _flags(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT output_id, founded_on_id, moved_at, reason, closed_at "
        "  FROM review_flags ORDER BY created_at, id"
    )


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------


async def test_consumerless_question_writes_one_self_flag(pg_pool, clean_slate):
    """The headline: a watched-question hit with NO forward consumer is now a
    queryable ``review_flags`` row — output_id = founded_on_id = the question,
    the F5 reason, open — and the run says so in counters AND title."""
    async with pg_pool.acquire() as conn:
        qid, ents = await _matchable_question(conn, thesis="kw3-f5 lone q")
    await _dispatch(pg_pool)  # first run seeds the cursor silently

    async with pg_pool.acquire() as conn:
        probe = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, probe, ents)

    c = _counters(await _dispatch(pg_pool, question_flags="on"))
    assert c["edges_written"] == 1
    assert c["flags_written"] == 0  # no consumer — the walk found nobody
    assert c["question_flags_written"] == 1
    assert c["question_flags_deduped"] == 0
    # The gauge counts the self-flag: review debt with a live head to review.
    assert c["staleness_debt"] == 1

    async with pg_pool.acquire() as conn:
        flags = await _flags(conn)
    assert len(flags) == 1
    f = flags[0]
    assert f["output_id"] == qid and f["founded_on_id"] == qid
    assert f["reason"] == cw.FLAG_REASON_UNCONSUMED
    assert f["closed_at"] is None
    assert f["moved_at"] is not None


async def test_the_surface_ships_off_byte_identical_to_4_0_0(
    pg_pool, clean_slate
):
    """No ``question_flags`` option -> the 4.0.0 behaviour byte for byte:
    zero flags of any kind for a consumerless hit, and the receipt still
    carries the new counters at their inert zeros (the bearing-counter
    convention — "off" and "refused nothing" must read differently)."""
    async with pg_pool.acquire() as conn:
        _, ents = await _matchable_question(conn, thesis="kw3-f5 off q")
    await _dispatch(pg_pool)

    async with pg_pool.acquire() as conn:
        probe = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, probe, ents)

    c = _counters(await _dispatch(pg_pool))
    assert c["edges_written"] == 1
    assert c["flags_written"] == 0
    assert c["question_flags_written"] == 0
    assert c["question_flags_deduped"] == 0
    async with pg_pool.acquire() as conn:
        assert await _flags(conn) == []


async def test_one_open_flag_per_question_further_evidence_dedupes(
    pg_pool, clean_slate
):
    """The 0107 partial unique index bounds the surface: a second signal
    bearing on the same still-open question dedupes onto the existing OPEN
    flag instead of flooding — counted, never silent."""
    async with pg_pool.acquire() as conn:
        qid, ents = await _matchable_question(conn, thesis="kw3-f5 dedupe q")
    await _dispatch(pg_pool)

    async with pg_pool.acquire() as conn:
        s1 = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s1, ents)
    c1 = _counters(await _dispatch(pg_pool, question_flags="on"))
    assert c1["question_flags_written"] == 1

    async with pg_pool.acquire() as conn:
        s2 = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s2, ents)
    c2 = _counters(await _dispatch(pg_pool, question_flags="on"))
    assert c2["question_flags_written"] == 0
    assert c2["question_flags_deduped"] == 1

    async with pg_pool.acquire() as conn:
        flags = await _flags(conn)
    assert len(flags) == 1
    assert flags[0]["output_id"] == qid


async def test_a_consumed_question_flags_its_consumer_never_itself(
    pg_pool, clean_slate
):
    """The self-flag is the FALLBACK, not a second flag: when the forward
    walk finds a live consumer, the consumer flag is the detect surface and
    no self-flag is written — the two reasons partition the flag population."""
    async with pg_pool.acquire() as conn:
        qid, ents = await _matchable_question(conn, thesis="kw3-f5 consumed q")
        consumer = await _insert_live_consumer(conn, qid)
    await _dispatch(pg_pool)

    async with pg_pool.acquire() as conn:
        probe = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, probe, ents)

    c = _counters(await _dispatch(pg_pool, question_flags="on"))
    assert c["flags_written"] == 1
    assert c["question_flags_written"] == 0

    async with pg_pool.acquire() as conn:
        flags = await _flags(conn)
    assert len(flags) == 1
    assert flags[0]["output_id"] == consumer
    assert flags[0]["founded_on_id"] == qid
    assert flags[0]["reason"] == cw.FLAG_REASON


async def test_self_flags_share_the_flag_cap(pg_pool, clean_slate):
    """One budget for the whole flag plane: with ``flag_cap=1`` and two
    consumerless hits, the second self-flag is DROPPED and counted — the cap
    can never be dodged by the new surface."""
    async with pg_pool.acquire() as conn:
        _, ents_a = await _matchable_question(conn, thesis="kw3-f5 cap q A")
        _, ents_b = await _matchable_question(
            conn, thesis="kw3-f5 cap q B", desk="kw3_f5_desk_b"
        )
    await _dispatch(pg_pool)

    async with pg_pool.acquire() as conn:
        probe = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, probe, ents_a)
        await _link_all(conn, probe, ents_b)

    c = _counters(await _dispatch(pg_pool, question_flags="on", flag_cap=1))
    assert c["edges_written"] == 2
    assert c["question_flags_written"] == 1
    assert c["flags_dropped_cap"] == 1
    async with pg_pool.acquire() as conn:
        assert len(await _flags(conn)) == 1


async def test_duplicate_theses_flag_per_hypothesis_row_not_per_thesis(
    pg_pool, clean_slate
):
    """K-4 R4 §9 recorded — for the DIRECTION SESSION, not for this surface —
    that "112 question ids carry only 89 distinct theses ... The matcher pays
    full price per duplicate." Dedup is deliberately NOT folded in here: the
    flag surface mirrors the hypothesis rows that actually matched, one open
    flag per ROW, because collapsing duplicate theses at flag time would (a)
    hide the harvest defect the direction session needs to see, and (b) save
    nothing — the report's own words put the cost at MATCH time, upstream of
    flags. Two rows, one thesis, two flags; the duplication is queryable
    (GROUP BY thesis over the flag join), never masked."""
    thesis = "Will the Hormuz blockade translate into actual oil-import "
    thesis += "disruptions for the United States? (kw3-f5 twin)"
    async with pg_pool.acquire() as conn:
        q1, ents = await _matchable_question(conn, thesis=thesis)
        q2, _ = await _matchable_question(conn, thesis=thesis, ents=ents)
    await _dispatch(pg_pool)

    async with pg_pool.acquire() as conn:
        probe = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, probe, ents)

    c = _counters(await _dispatch(pg_pool, question_flags="on"))
    assert c["edges_written"] == 2
    assert c["question_flags_written"] == 2

    async with pg_pool.acquire() as conn:
        flags = await _flags(conn)
    assert {f["output_id"] for f in flags} == {q1, q2}
    assert all(f["reason"] == cw.FLAG_REASON_UNCONSUMED for f in flags)


async def test_the_receipt_title_carries_the_question_flags(
    pg_pool, clean_slate
):
    """The surface exists because the flag plane was INVISIBLE — so its
    output rides the receipt TITLE, not only the body counters."""
    async with pg_pool.acquire() as conn:
        _, ents = await _matchable_question(conn, thesis="kw3-f5 title q")
    await _dispatch(pg_pool)

    async with pg_pool.acquire() as conn:
        probe = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, probe, ents)

    result = await _dispatch(pg_pool, question_flags="on")
    assert "1 question flag(s)" in result.finding.title


def test_x1_catalog_declares_the_switch_choice_locked():
    """The knob is a declared X-1 option — 'on'/'off' only, so a typo'd value
    fails the catalog loudly instead of silently keeping the surface dark."""
    spec = {s.name: s for s in HANDLER_OPTIONS["claim_watch"]}["question_flags"]
    assert spec.choices == ("on", "off")
    assert cw.DEFAULT_QUESTION_FLAGS == "off"
