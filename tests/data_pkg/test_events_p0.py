# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3/P0 tests — the event surface (migrations 0202-0205 + the Python leaves).

Pure unit tests cover the lifecycle FSM (``events.lifecycle``), the signature
builder (``events.signature``), the payload/registry wiring, and the
LEGBA_EVENTS default-off contract. Integration tests use ``migrated_pg``:
the Python/Postgres signature twin row-for-row, ``write_event`` upsert +
ledger-open behavior, the ledger's append-only triggers, and
``fold_event_entity_links`` inside ``entity_researcher.merge_pair``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.events import lifecycle as el
from legba.data.events import signature as es
from legba.data.provenance import (
    AnalystContext,
    EventEntityLinkPayload,
    EventPayload,
    EventSignalLinkPayload,
    OutputKind,
    events_enabled,
    spec_for_kind,
    write_event,
)
from legba.data.provenance.models import EventPayload as _EventPayloadCheck


# ---------------------------------------------------------------------------
# Lifecycle FSM — vocabulary, the transition table, validation
# ---------------------------------------------------------------------------


def test_lifecycle_vocab_mirrors_migration_0204() -> None:
    """The Python frozensets are the DB CHECKs' mirror — same strings."""
    assert el.LIFECYCLE_STATES == frozenset(
        {"emerging", "developing", "active", "evolving", "resolved"}
    )
    assert el.LIFECYCLE_TRANSITIONS == frozenset(
        {"opened", "advanced", "accelerated", "stabilised",
         "resolved", "reactivated"}
    )
    assert el.INITIAL_STATE == "emerging"
    assert el.EVENTS_LIFECYCLE_VERSION == "2026-09/p0"


@pytest.mark.parametrize(
    "state,transition,expected",
    [
        ("emerging", "opened", "emerging"),
        ("emerging", "advanced", "developing"),
        ("developing", "advanced", "active"),
        ("active", "accelerated", "evolving"),
        ("evolving", "stabilised", "active"),
        ("emerging", "resolved", "resolved"),
        ("developing", "resolved", "resolved"),
        ("active", "resolved", "resolved"),
        ("evolving", "resolved", "resolved"),
        ("resolved", "reactivated", "developing"),
    ],
)
def test_next_state_legal_table(state, transition, expected) -> None:
    assert el.next_state(state, transition) == expected


@pytest.mark.parametrize(
    "state,transition",
    [
        ("emerging", "accelerated"),
        ("developing", "accelerated"),
        ("developing", "stabilised"),
        ("active", "advanced"),
        ("evolving", "advanced"),
        ("resolved", "resolved"),          # silence never closes twice
        ("resolved", "advanced"),
        ("resolved", "opened"),
        ("emerging", "reactivated"),       # only resolved reactivates
        ("active", "opened"),
    ],
)
def test_next_state_refuses_illegal_pairs(state, transition) -> None:
    """Raised, not coerced — a silently-corrected transition is a lie in an
    append-only ledger."""
    with pytest.raises(el.EventTransitionError):
        el.next_state(state, transition)


def test_next_state_unknown_values_raise() -> None:
    with pytest.raises(el.EventTransitionError):
        el.next_state("bogus", "advanced")
    with pytest.raises(el.EventTransitionError):
        el.next_state("emerging", "bogus")


def test_evaluate_transition_triggers() -> None:
    """The measured triggers fire at the spec §2.3 thresholds, and silence is
    evaluated first (it asserts an absence)."""
    m = el.EventMeasurement
    # emerging: 3+ signals advance; 48h silence resolves.
    assert el.evaluate_transition("emerging", m(signal_count=2)) is None
    assert el.evaluate_transition("emerging", m(signal_count=3)) == "advanced"
    assert el.evaluate_transition(
        "emerging", m(signal_count=3, silence_hours=49)) == "resolved"
    # developing: needs 5 signals AND confidence >= 0.6.
    assert el.evaluate_transition(
        "developing", m(signal_count=5, confidence=0.59)) is None
    assert el.evaluate_transition(
        "developing", m(signal_count=5, confidence=0.6)) == "advanced"
    assert el.evaluate_transition(
        "developing", m(silence_hours=73)) == "resolved"
    # active: 2x the 7d baseline, or a new actor/ISO2, accelerates.
    assert el.evaluate_transition(
        "active", m(link_rate_24h=1.9, baseline_rate_7d=1.0)) is None
    assert el.evaluate_transition(
        "active", m(link_rate_24h=2.0, baseline_rate_7d=1.0)) == "accelerated"
    assert el.evaluate_transition(
        "active", m(new_actor_or_geo=True)) == "accelerated"
    # evolving: the rate back inside [0.5x, 2x] stabilises.
    assert el.evaluate_transition(
        "evolving", m(link_rate_24h=1.0, baseline_rate_7d=1.0)) == "stabilised"
    assert el.evaluate_transition(
        "evolving", m(link_rate_24h=3.0, baseline_rate_7d=1.0)) is None
    # resolved: a new link reactivates; silence writes nothing.
    assert el.evaluate_transition(
        "resolved", m(new_link_arrived=True)) == "reactivated"
    assert el.evaluate_transition(
        "resolved", m(silence_hours=9999)) is None


def _ok_row(**kw):
    base = dict(
        event_id=uuid4(),
        occurred_at=datetime.now(tz=timezone.utc),
        transition="advanced",
        why="signal_count reached 3",
        state_from="emerging",
        state_to="developing",
        derived_from=(uuid4(),),
    )
    base.update(kw)
    return el.LifecycleEvent(**base)


def test_lifecycle_event_validates() -> None:
    _ok_row()  # a well-formed row constructs
    with pytest.raises(el.EventTransitionError):
        _ok_row(transition="bogus")
    with pytest.raises(el.EventTransitionError):
        _ok_row(state_from="bogus")
    with pytest.raises(el.EventTransitionError):
        _ok_row(why="   ")
    with pytest.raises(el.EventTransitionError):
        # evidence-bearing transition with no derived_from
        _ok_row(derived_from=())
    with pytest.raises(el.EventTransitionError):
        # state_to disagrees with the FSM
        _ok_row(state_to="active")


def test_lifecycle_event_allows_evidence_free_resolved() -> None:
    row = _ok_row(transition="resolved", state_from="emerging",
                  state_to="resolved", derived_from=())
    assert row.state_to == "resolved"


# ---------------------------------------------------------------------------
# Signature builder + the Postgres twin
# ---------------------------------------------------------------------------


def test_event_marker_matches_reserved_slot() -> None:
    """#evt: must equal the marker finding_supersession already reserves."""
    from legba.data.analysts.deterministic_handlers.finding_supersession import (
        _SIGNATURE_EVENT_MARKER,
    )
    assert es.EVENT_MARKER == _SIGNATURE_EVENT_MARKER == "#evt:"


def test_event_signature_basic() -> None:
    sig = es.event_signature(
        "Oil Price Spike", ["Iran", "the United States"], "IR"
    )
    assert sig == "evt:oil price spike|iran,unitedstates#evt:ir"


def test_event_signature_entity_tail_k_and_sort() -> None:
    # K caps the tail; folds sort (order-insensitive signature).
    sig = es.event_signature(
        "t", ["Mossad", "Tehran", "Iran", "IAEA"], "ir", entity_k=2
    )
    assert sig == "evt:t|iaea,iran#evt:ir"
    # K=0 gives the topic-only shape (the {0,2,3,4} sweep's floor).
    assert es.event_signature("t", ["Iran"], "ir", entity_k=0) == (
        "evt:t#evt:ir"
    )


def test_event_signature_junk_and_fold() -> None:
    # Junk literals drop ('Leader' -> 'leader' -> junk; 'Two' likewise), folds
    # collapse ('the United States' -> 'unitedstates'). NB 'the Leader' is NOT
    # junk under is_junk_entity (the check runs on the unstripped surface) —
    # it folds to 'leader' and lands in the tail, on both twin sides alike.
    sig = es.event_signature(
        "strike", ["Leader", "Two", "Fordow", "the United States"], "ir"
    )
    assert sig == "evt:strike|fordow,unitedstates#evt:ir"


def test_event_signature_fallbacks() -> None:
    # empty topic falls back to the first entity token; neither -> None.
    assert es.event_signature("", ["Iran"], "ir") == "evt:iran|iran#evt:ir"
    assert es.event_signature("", [], "ir") is None
    assert es.event_signature(None, None, None) is None
    # unresolvable polity mints the _domestic residue, never an empty slot.
    assert es.event_signature("t", [], None) == "evt:t#evt:_domestic"


# ---------------------------------------------------------------------------
# Registry + payload + flag
# ---------------------------------------------------------------------------


def test_event_kind_registered() -> None:
    assert OutputKind.EVENT.value == "event"
    spec = spec_for_kind("event")
    assert spec.table == "events"
    assert spec.payload_model is _EventPayloadCheck
    assert spec.schema_uri == "iglu:legba/event/jsonschema/1-0-0"


def test_event_payload_validation() -> None:
    p = EventPayload(event_signature="evt:x#evt:ir", title="t")
    assert p.source_method == "clustering" and p.kind_marker == "event"
    # lifecycle_state is NOT a payload field — a write-path event is born
    # 'emerging' and the ledger owns every transition thereafter.
    assert "lifecycle_state" not in EventPayload.model_fields
    for bad in (
        dict(event_signature="e", title="t", event_type="bogus"),
        dict(event_signature="e", title="t", severity="bogus"),
        dict(event_signature="e", title="t", source_method="bogus"),
    ):
        with pytest.raises(Exception):
            EventPayload(**bad)
    with pytest.raises(Exception):
        EventSignalLinkPayload(
            signal_id=uuid4(), linked_at=datetime.now(tz=timezone.utc),
            source_class="bogus")
    # linked_at is REQUIRED (evidence time is never invented).
    with pytest.raises(Exception):
        EventSignalLinkPayload(signal_id=uuid4())


def test_events_flag_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_EVENTS", raising=False)
    assert events_enabled() is False
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    assert events_enabled() is True
    monkeypatch.setenv("LEGBA_EVENTS", "off")
    assert events_enabled() is False


# ---------------------------------------------------------------------------
# Integration — migrated_pg
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


def _actx(**kw) -> AnalystContext:
    # Unique producer id per write — the session-scoped migrated_pg DB is
    # shared and this suite runs shuffled, so a fixed id would pollute
    # per-producer assertions across tests.
    base = dict(
        analyst_id=f"analyst.test_events_{uuid4().hex[:8]}",
        analyst_version="p0test",
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )
    base.update(kw)
    return AnalystContext(**base)


async def _seed_signal(conn, *, fetched_at=None) -> "asyncpg.Record":
    return await conn.fetchval(
        "INSERT INTO signals (source_id, modality, payload, content_hash,"
        " fetched_at, geo) VALUES ('rss_main', 'text', '{}'::jsonb,"
        " $2, $1, '{ar}'::text[]) RETURNING id",
        fetched_at or datetime.now(tz=timezone.utc),
        f"h-{uuid4().hex}",
    )


# Canonical-name-shaped surfaces — the twin's declared input domain. Includes
# the boundary cases: leading articles (guarded strip), junk literals, the
# length<=2 gate, punctuation, mixed case. NOT included by contract: raw
# alias surfaces like 'US' — identity_fold routes those through the alias
# map (Python-only); a stored canonical_name is post-collapse by the write
# path, so the divergence is unreachable on the declared domain.
_TWIN_ENTITY_CORPUS = [
    "Iran", "United States", "the United States", "Vladimir Putin",
    "IAEA", "Fordow", "Israel Defense Forces", "NATO", "European Union",
    "Ali Khamenei", "OPEC+", "O'Neill", "20th Brigade",
    "the Leader", "Two", "Xi", "West", "Parl", "Fed", "hundreds", "",
]

_TWIN_CASES = [
    ("oil price", ["Iran", "the United States"], "ir", 3),
    ("", ["Iran"], None, 3),
    ("", [], None, 3),
    ("ceasefire talks", ["Mossad", "Tehran", "Iran"], "IR", 3),
    ("strike", ["the Leader", "IAEA", "Fordow"], "ir", 2),
    ("t", ["Iran"], "ir", 0),
    ("t", ["Iran"], "ir", 4),
    ("", _TWIN_ENTITY_CORPUS, "United States", 3),
    ("x", _TWIN_ENTITY_CORPUS, "  ", 3),
    (None, _TWIN_ENTITY_CORPUS, None, 3),
]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_signature_python_and_sql_agree(pg_conn) -> None:
    """Row-for-row twin parity over the canonical-surface domain."""
    for topic, ents, polity, k in _TWIN_CASES:
        sql = await pg_conn.fetchval(
            "SELECT public.event_signature($1,$2,$3,$4)",
            topic, ents, polity, k,
        )
        py = es.event_signature(topic, ents, polity, entity_k=k)
        assert sql == py, f"twin diverged: {topic!r} {ents} {polity!r} k={k}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_signature_twin_over_live_canonical_names(pg_conn) -> None:
    """The twin over every canonical_name actually stored — the real guard."""
    names = [
        r[0] for r in await pg_conn.fetch(
            "SELECT canonical_name FROM entity_profiles"
        )
    ]
    sql = await pg_conn.fetchval(
        "SELECT public.event_signature($1,$2,$3,$4)",
        "audit", names, "ar", 3,
    )
    assert sql == es.event_signature("audit", names, "ar", entity_k=3)


def _event_payload(sig: str = "evt:test occurrence|fordow,iaea#evt:ir",
                   **kw) -> EventPayload:
    base = dict(
        event_signature=sig,
        title="Fordow strike",
        category="conflict",
        geo=["ir"],
        confidence=0.7,
        signal_count=2,
        distinct_source_count=1,
        source_method="tower",
    )
    base.update(kw)
    return EventPayload(**base)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_event_flag_off_refuses(pg_conn, monkeypatch) -> None:
    """LEGBA_EVENTS unset -> the write path refuses (structural off)."""
    monkeypatch.delenv("LEGBA_EVENTS", raising=False)
    actx = _actx()
    with pytest.raises(RuntimeError, match="LEGBA_EVENTS"):
        await write_event(
            pg_conn,
            analyst_ctx=actx,
            payload=_event_payload(),
            derived_from=[uuid4()],
        )
    # The refusal landed nothing for THIS producer. The session-scoped
    # migrated_pg DB is shared and the suite runs shuffled, so sibling tests
    # may already have landed their own rows — the flag-off invariant is that
    # this write landed nothing, not that the table is empty.
    assert await pg_conn.fetchval(
        "SELECT count(*) FROM events WHERE analyst_id = $1", actx.analyst_id
    ) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_event_requires_analyst_id(pg_conn, monkeypatch) -> None:
    """NULL analyst_id would duplicate instead of upserting — refused."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    with pytest.raises(ValueError, match="analyst_id"):
        await write_event(
            pg_conn,
            analyst_ctx=_actx(analyst_id=None),
            payload=_event_payload(),
            derived_from=[uuid4()],
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_event_requires_evidence(pg_conn, monkeypatch) -> None:
    """An event cannot open evidence-free — 'resolved' is the only
    evidence-free transition and 'opened' is not it."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    with pytest.raises(ValueError, match="evidence"):
        await write_event(
            pg_conn,
            analyst_ctx=_actx(),
            payload=_event_payload(signals=[]),
            derived_from=[],
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_event_insert_and_upsert(pg_conn, monkeypatch) -> None:
    """Insert writes the row + links + the 'opened' ledger row; a re-emit of
    the same (signature, analyst_id) upserts in place — no duplicate, no
    second 'opened'."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    s1, s2 = await _seed_signal(pg_conn), await _seed_signal(pg_conn)
    e1 = await pg_conn.fetchval(
        "INSERT INTO entity_profiles (data, canonical_name) "
        "VALUES ('{}'::jsonb, 'Fordow') RETURNING id")
    payload = _event_payload(
        signals=[
            EventSignalLinkPayload(signal_id=s1, linked_at=datetime.now(
                tz=timezone.utc), source_class="reporting"),
            EventSignalLinkPayload(signal_id=s2, linked_at=datetime.now(
                tz=timezone.utc), source_class="analysis",
                source_kind="rss", source_id="rss_main"),
        ],
        entities=[EventEntityLinkPayload(
            entity_id=e1, role="target", derived_from=[s1])],
    )
    actx = _actx()
    out, dlq = await write_event(
        pg_conn, analyst_ctx=actx, payload=payload,
        derived_from=[uuid4()],
    )
    assert dlq is None and out is not None and out.table == "events"

    row = await pg_conn.fetchrow(
        "SELECT * FROM events WHERE id = $1", out.id)
    assert row["event_signature"] == payload.event_signature
    assert row["analyst_id"] == actx.analyst_id
    assert row["lifecycle_state"] == "emerging"
    assert row["source_method"] == "tower"
    assert row["geo"] == ["ir"]

    n_links = await pg_conn.fetchval(
        "SELECT count(*) FROM signal_event_links WHERE event_id = $1", out.id)
    assert n_links == 2
    n_ents = await pg_conn.fetchval(
        "SELECT count(*) FROM event_entity_links WHERE event_id = $1", out.id)
    assert n_ents == 1
    opened = await pg_conn.fetch(
        "SELECT transition, state_from, state_to FROM event_lifecycle_events "
        "WHERE event_id = $1", out.id)
    assert len(opened) == 1 and opened[0]["transition"] == "opened"

    # Re-emit: same key -> UPDATE in place. OutputRow.id is the write's
    # freshly-minted attempt id (the situations upsert contract), so the
    # landed-row check goes through the signature: still ONE row, still the
    # ORIGINAL id, no second 'opened' row, links deduped.
    out2, _ = await write_event(
        pg_conn, analyst_ctx=actx, payload=payload,
        derived_from=[uuid4()],
    )
    assert out2 is not None
    rows = await pg_conn.fetch(
        "SELECT id FROM events WHERE event_signature = $1 AND"
        " analyst_id = $2",
        payload.event_signature, actx.analyst_id)
    assert [r["id"] for r in rows] == [out.id]
    assert await pg_conn.fetchval(
        "SELECT count(*) FROM event_lifecycle_events WHERE event_id = $1",
        out.id) == 1
    assert await pg_conn.fetchval(
        "SELECT count(*) FROM signal_event_links WHERE event_id = $1",
        out.id) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_event_rollups_are_the_link_tables_truth(
    pg_conn, monkeypatch
) -> None:
    """``signal_count``/``distinct_source_count`` come off the LINK TABLE.

    The payload's own numbers are deliberately absurd here (999/999) and the
    re-emit carries an overlapping member set, so nothing but a read of
    ``signal_event_links`` can produce the asserted values. Read back through
    the persisted columns on the real driver — three members, then two of
    which one is already linked, is FOUR links and four is what the row has
    to say. Additive or payload-trusting behavior lands 999, 3, 5 or 1,002.
    """
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    now = datetime.now(tz=timezone.utc)
    s1, s2, s3 = [await _seed_signal(pg_conn) for _ in range(3)]
    s4 = await _seed_signal(pg_conn)

    def _link(signal_id, source_id):
        return EventSignalLinkPayload(
            signal_id=signal_id, linked_at=now, relevance=1.0,
            source_class="reporting", source_kind="rss", source_id=source_id,
        )

    actx = _actx()
    sig = f"evt:rollup-truth-{uuid4().hex[:8]}#evt:ir"
    out, dlq = await write_event(
        pg_conn, analyst_ctx=actx,
        payload=_event_payload(
            sig=sig, signal_count=999, distinct_source_count=999,
            signals=[_link(s1, "rss_a"), _link(s2, "rss_b"),
                     _link(s3, "rss_a")],
        ),
        derived_from=[uuid4()],
    )
    assert dlq is None and out is not None
    row = await pg_conn.fetchrow(
        "SELECT signal_count, distinct_source_count FROM events WHERE id = $1",
        out.id,
    )
    assert (row["signal_count"], row["distinct_source_count"]) == (3, 2)

    # Re-emit the SAME identity with two members, one of them already linked.
    out2, dlq2 = await write_event(
        pg_conn, analyst_ctx=actx,
        payload=_event_payload(
            sig=sig, signal_count=999, distinct_source_count=999,
            signals=[_link(s3, "rss_a"), _link(s4, "rss_c")],
        ),
        derived_from=[uuid4()],
    )
    assert dlq2 is None and out2 is not None

    truth = await pg_conn.fetchrow(
        "SELECT count(*)::int AS n,"
        "       count(DISTINCT NULLIF(source_id, ''))::int AS d"
        "  FROM signal_event_links WHERE event_id = $1",
        out.id,
    )
    assert (truth["n"], truth["d"]) == (4, 3)
    row2 = await pg_conn.fetchrow(
        "SELECT signal_count, distinct_source_count FROM events WHERE id = $1",
        out.id,
    )
    assert (row2["signal_count"], row2["distinct_source_count"]) == (4, 3)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_event_rollups_ignore_actor_links(
    pg_conn, monkeypatch
) -> None:
    """Actor links do not enter the member count.

    The live drift was a (member x actor) product; an event with two members
    and three actors reads two, not six, whatever the payload claimed.
    """
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    now = datetime.now(tz=timezone.utc)
    s1, s2 = await _seed_signal(pg_conn), await _seed_signal(pg_conn)
    entities = [
        await pg_conn.fetchval(
            "INSERT INTO entity_profiles (data, canonical_name) "
            "VALUES ('{}'::jsonb, $1) RETURNING id", f"Rollup {uuid4().hex[:8]}")
        for _ in range(3)
    ]
    out, _ = await write_event(
        pg_conn, analyst_ctx=_actx(),
        payload=_event_payload(
            sig=f"evt:rollup-actors-{uuid4().hex[:8]}#evt:ir",
            signal_count=6, distinct_source_count=6,
            signals=[
                EventSignalLinkPayload(
                    signal_id=s, linked_at=now, source_class="reporting",
                    source_kind="rss", source_id="rss_a")
                for s in (s1, s2)
            ],
            entities=[
                EventEntityLinkPayload(entity_id=e, role=role, derived_from=[s1])
                for e, role in zip(entities, ("actor", "target", "observer"))
            ],
        ),
        derived_from=[uuid4()],
    )
    assert out is not None
    assert await pg_conn.fetchval(
        "SELECT count(*) FROM event_entity_links WHERE event_id = $1", out.id
    ) == 3
    row = await pg_conn.fetchrow(
        "SELECT signal_count, distinct_source_count FROM events WHERE id = $1",
        out.id,
    )
    assert (row["signal_count"], row["distinct_source_count"]) == (2, 1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_lifecycle_ledger_is_append_only(pg_conn, monkeypatch) -> None:
    """UPDATE and DELETE both fail at the trigger (the 0184 shape)."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    out, _ = await write_event(
        pg_conn, analyst_ctx=_actx(), payload=_event_payload(),
        derived_from=[uuid4()],
    )
    with pytest.raises(Exception, match="append-only"):
        await pg_conn.execute(
            "UPDATE event_lifecycle_events SET why = 'x' WHERE event_id = $1",
            out.id)
    with pytest.raises(Exception, match="append-only"):
        await pg_conn.execute(
            "DELETE FROM event_lifecycle_events WHERE event_id = $1", out.id)
    # The row survived both refusals.
    assert await pg_conn.fetchval(
        "SELECT count(*) FROM event_lifecycle_events WHERE event_id = $1",
        out.id) == 1


async def _mk_entity(conn, name: str):
    return await conn.fetchval(
        "INSERT INTO entity_profiles (data, canonical_name) "
        "VALUES ('{}'::jsonb, $1) RETURNING id", name)


async def _mk_event_with_entity_link(conn, entity_id) -> "asyncpg.Record":
    sig = await _seed_signal(conn)
    ev = await conn.fetchval(
        "INSERT INTO events (event_signature, analyst_id, title) "
        "VALUES ($1, 'tower_backfill', 't') RETURNING id",
        f"evt:fold-test-{uuid4().hex[:8]}#evt:ir")
    await conn.execute(
        "INSERT INTO event_entity_links (event_id, entity_id, role,"
        " derived_from) VALUES ($1, $2, 'actor', $3::uuid[])",
        ev, entity_id, [sig])
    return ev


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_pair_folds_event_entity_links(pg_conn) -> None:
    """A loser that is an event actor has its links repointed to the keeper,
    inside the merge transaction; the receipt counts them."""
    from legba.data.analysts.entity_researcher import merge_pair

    keeper = await _mk_entity(pg_conn, f"Keeper {uuid4().hex[:6]}")
    loser = await _mk_entity(pg_conn, f"Loser {uuid4().hex[:6]}")
    ev = await _mk_event_with_entity_link(pg_conn, loser)

    fold: dict[str, int] = {}
    ok = await merge_pair(pg_conn, str(keeper), str(loser), edge_fold=fold)
    assert ok is True
    assert fold.get("event_links") == 1
    now = await pg_conn.fetchval(
        "SELECT entity_id FROM event_entity_links WHERE event_id = $1", ev)
    assert now == keeper


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_pair_rollback_leaves_event_links(pg_conn) -> None:
    """If the merge transaction rolls back, the event links are untouched —
    the fold is inside the same transaction, not a side effect."""
    from legba.data.analysts.entity_researcher import merge_pair

    keeper = await _mk_entity(pg_conn, f"Keeper {uuid4().hex[:6]}")
    loser = await _mk_entity(pg_conn, f"Loser {uuid4().hex[:6]}")
    ev = await _mk_event_with_entity_link(pg_conn, loser)

    with pytest.raises(RuntimeError):
        async with pg_conn.transaction():
            await merge_pair(pg_conn, str(keeper), str(loser))
            raise RuntimeError("force rollback")

    assert await pg_conn.fetchval(
        "SELECT merged_into FROM entity_profiles WHERE id = $1", loser) is None
    assert await pg_conn.fetchval(
        "SELECT entity_id FROM event_entity_links WHERE event_id = $1",
        ev) == loser


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_edges_canonical_check(pg_conn) -> None:
    """correlated_with / contradicts store canonically (src < dst); directed
    edge types are exempt, and self edges refuse."""
    evs = sorted(
        [await pg_conn.fetchval(
            "INSERT INTO events (event_signature, analyst_id, title) "
            "VALUES ($1, 'tower_backfill', 't') RETURNING id",
            f"evt:edge-{uuid4().hex[:8]}-{i}#evt:ir")
            for i in range(2)])
    lo, hi = evs
    # Directed: any order.
    await pg_conn.execute(
        "INSERT INTO event_edges (src_event_id, dst_event_id, edge_type) "
        "VALUES ($1, $2, 'caused_by')", hi, lo)
    # Symmetric reversed: refused.
    with pytest.raises(Exception, match="symmetric_canonical"):
        await pg_conn.execute(
            "INSERT INTO event_edges (src_event_id, dst_event_id, edge_type) "
            "VALUES ($1, $2, 'correlated_with')", hi, lo)
    await pg_conn.execute(
        "INSERT INTO event_edges (src_event_id, dst_event_id, edge_type) "
        "VALUES ($1, $2, 'correlated_with')", lo, hi)
    with pytest.raises(Exception, match="no_self"):
        await pg_conn.execute(
            "INSERT INTO event_edges (src_event_id, dst_event_id, edge_type) "
            "VALUES ($1, $1, 'part_of')", lo)
