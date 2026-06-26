# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D15 (upstream) — the ``proposed_edges`` PRODUCER must STAMP ``derived_from``.

Defect D15: the ``relationship_reifier`` already copies ``pe.derived_from`` into
the nexus it writes (populating BOTH the nexus ``derived_from`` and
``source_signal_ids`` columns), but ALL live ``proposed_edges`` carried an EMPTY
``derived_from``, so nothing propagated and agent nexuses had no real provenance.

The fix is upstream: the co-occurrence edge writer in
``entity_resolution._resolve_batch`` now stamps the ORIGINATING SIGNAL id into
each ``proposed_edges`` row's ``derived_from`` (a ``uuid[]``) at write time, and
UNIONs it (deduped) on the corroboration upsert.

These are PURE unit tests (NO database). A tiny fake ``pool``/``conn`` records
the SQL each call would issue and returns plausible rows, so we can assert the
exact arguments bound to the ``INSERT INTO proposed_edges`` statement — namely
that the ``derived_from`` parameter is non-empty and carries the signal id.

The fake also lets us drive the VERBATIM live-junk catalog through the producer's
``is_junk_entity`` endpoint guard: a junk span (e.g. ``"$3.2bn"``) and a
possessive fragment (``"Abu Dhabi 's"``, which the canon strips to ``Abu Dhabi``)
must never co-occur as a *junk-named* edge endpoint, while the clean edge that IS
written still carries the originating signal id as provenance.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest

from legba.data.analysts.deterministic_handlers import entity_resolution


# ---------------------------------------------------------------------------
# Minimal no-DB fakes — record every statement; return plausible rows.
# ---------------------------------------------------------------------------


class _FakeConn:
    """Records each statement+args; answers the few reads ``_resolve_batch`` does.

    * ``fetch`` (the un-resolved-signals sweep) → the seeded signal rows.
    * ``fetchrow`` (the entity_profiles upsert) → a fresh id / version=1 / inserted.
    * ``fetchval`` (version lookups / merged-aliases read) → a plausible scalar.
    * ``execute`` → records the call (this captures the proposed_edges INSERT).
    """

    def __init__(self, signal_rows: list[dict[str, Any]]):
        self._signal_rows = signal_rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if "FROM signals" in sql and "entities_resolved_at IS NULL" in sql:
            return list(self._signal_rows)
        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        if "INSERT INTO entity_profiles" in sql:
            # A fresh profile: new id, version 1, inserted (xmax=0) → True.
            return {"id": uuid.uuid4(), "version": 1, "inserted": True}
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        if "SELECT version FROM entity_profiles" in sql:
            return 1
        if "merged_aliases" in sql:
            return "[]"
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "OK"

    # async-context-manager support for ``pool.acquire()``
    async def __aenter__(self) -> "_FakeConn":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeAcquire:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _signal_row(sig_id: uuid.UUID, entities: list[dict[str, str]], title: str) -> dict[str, Any]:
    return {
        "id": sig_id,
        "payload": json.dumps({"title": title, "entities": entities}),
    }


def _proposed_edge_inserts(conn: _FakeConn) -> list[tuple[str, tuple[Any, ...]]]:
    """All recorded calls that were an ``INSERT INTO proposed_edges`` (not the
    reifier read / governance paths — this is the producer's write)."""
    return [
        (sql, args)
        for (sql, args) in conn.calls
        if "INSERT INTO proposed_edges" in sql
    ]


# ---------------------------------------------------------------------------
# Core D15 assertion — a written proposed_edge carries non-empty derived_from
# ---------------------------------------------------------------------------


async def test_proposed_edge_carries_nonempty_derived_from():
    """One signal co-mentioning two clean entities → exactly one co_occurs edge,
    and that edge's ``derived_from`` parameter is NON-EMPTY and contains the
    originating signal id (the lineage the reifier copies into the nexus)."""
    sig_id = uuid.uuid4()
    conn = _FakeConn(
        [
            _signal_row(
                sig_id,
                # Synthetic, canon-stable names (no surface/class rewrite, not
                # junk) so the only edge written is the clean pair.
                [
                    {"text": "Zentavia", "class": "organization"},
                    {"text": "Quorvex", "class": "organization"},
                ],
                "Zentavia partners with Quorvex",
            )
        ]
    )
    pool = _FakePool(conn)

    counters = await entity_resolution._resolve_batch(
        pool,
        batch_limit=10,
        geocoder=None,
        run_id=uuid.uuid4(),
        analyst_id="entity_resolution",
        analyst_version="d15-test",
    )

    assert counters["edges_upserted"] == 1, counters
    inserts = _proposed_edge_inserts(conn)
    assert len(inserts) == 1, inserts

    sql, args = inserts[0]
    # The producer binds (source_entity, target_entity, evidence_text,
    # derived_from). derived_from is the LAST positional arg.
    derived_from = args[-1]
    assert isinstance(derived_from, list), derived_from
    assert derived_from, "proposed_edge.derived_from must be NON-EMPTY (D15)"
    assert sig_id in derived_from, (
        "the originating signal id must be stamped as edge lineage",
        derived_from,
    )
    # The DML must actually write the column (regression guard against an
    # INSERT that silently omits derived_from and falls back to the '{}' default).
    assert "derived_from" in sql


async def test_derived_from_unions_on_corroboration_upsert():
    """The upsert path UNIONs the originating signal id (deduped). Two signals
    co-mentioning the SAME pair each stamp their own signal id, so across the two
    INSERTs the producer supplies a derived_from carrying BOTH ids — that is what
    accrues full lineage on a re-corroborated edge."""
    sig_a, sig_b = uuid.uuid4(), uuid.uuid4()
    ents = [
        {"text": "Zentavia", "class": "organization"},
        {"text": "Quorvex", "class": "organization"},
    ]
    conn = _FakeConn(
        [
            _signal_row(sig_a, ents, "A: Zentavia and Quorvex"),
            _signal_row(sig_b, ents, "B: Zentavia and Quorvex again"),
        ]
    )

    await entity_resolution._resolve_batch(
        _FakePool(conn),
        batch_limit=10,
        geocoder=None,
        run_id=uuid.uuid4(),
        analyst_id="entity_resolution",
        analyst_version="d15-test",
    )

    inserts = _proposed_edge_inserts(conn)
    assert len(inserts) == 2, inserts
    stamped = {args[-1][0] for (_sql, args) in inserts if args[-1]}
    # Each signal stamped its own id; the ON CONFLICT UNION (deduped) folds them.
    assert stamped == {sig_a, sig_b}, stamped
    # And the upsert UNION-dedup is present so re-corroboration never re-grows
    # nor drops a prior signal's lineage.
    assert "array_agg(DISTINCT m)" in inserts[0][0]


# ---------------------------------------------------------------------------
# VERBATIM live-junk catalog through the producer's endpoint guard
# ---------------------------------------------------------------------------

# The producer drops ``is_junk_entity(text)`` spans BEFORE they can become an
# edge endpoint (entity_resolution line ~416). These are the verbatim live
# junk tokens; the canon currently DROPS the money token and STRIPS the
# possessive — both must be reflected in the edges the producer writes.
_MONEY_JUNK = ["S$2,500", "US$ 525 million", "$3.2bn"]
_AGE_JUNK = ["51 - year - old", "2,600 - year - old", "24 - year - old", "centuries"]
_POSSESSIVE = "Abu Dhabi 's"  # canon strips → "Abu Dhabi"


async def test_junk_token_never_becomes_an_edge_endpoint():
    """A signal co-mentioning a CLEAN entity and the verbatim money-junk token
    ``"$3.2bn"`` (which ``is_junk_entity`` catches) yields NO edge — the junk
    span is dropped, leaving a single clean entity with no pair to co-occur."""
    sig_id = uuid.uuid4()
    conn = _FakeConn(
        [
            _signal_row(
                sig_id,
                [
                    {"text": "Zentavia", "class": "organization"},
                    {"text": "$3.2bn", "class": "entity"},  # verbatim live junk
                ],
                "Zentavia raises $3.2bn",
            )
        ]
    )

    counters = await entity_resolution._resolve_batch(
        _FakePool(conn), batch_limit=10, geocoder=None, run_id=None,
        analyst_id="er", analyst_version="d15-test",
    )

    # Only one non-junk endpoint survived → no pair → zero edges.
    assert counters["edges_upserted"] == 0, counters
    assert _proposed_edge_inserts(conn) == []


async def test_possessive_fragment_stripped_in_edge_endpoint():
    """The verbatim possessive ``"Abu Dhabi 's"`` is canonicalized to
    ``"Abu Dhabi"`` BEFORE it becomes an edge endpoint, so the edge written
    carries the clean form (never the trailing ``'s``) — and still stamps the
    originating signal id."""
    sig_id = uuid.uuid4()
    conn = _FakeConn(
        [
            _signal_row(
                sig_id,
                [
                    {"text": _POSSESSIVE, "class": "location"},
                    {"text": "Zentavia", "class": "organization"},
                ],
                "Zentavia opens in Abu Dhabi 's free zone",
            )
        ]
    )

    counters = await entity_resolution._resolve_batch(
        _FakePool(conn), batch_limit=10, geocoder=None, run_id=None,
        analyst_id="er", analyst_version="d15-test",
    )

    assert counters["edges_upserted"] == 1, counters
    inserts = _proposed_edge_inserts(conn)
    assert len(inserts) == 1, inserts
    _sql, args = inserts[0]
    # args[0] / args[1] are the (sorted) edge endpoints.
    endpoints = {args[0], args[1]}
    assert "Abu Dhabi" in endpoints, endpoints
    assert "Abu Dhabi 's" not in endpoints, endpoints
    # And lineage is still stamped.
    assert args[-1] and sig_id in args[-1], args[-1]


async def test_money_and_age_tokens_when_they_canon_to_junk_drop_the_pair():
    """Belt-and-braces over the verbatim money/age catalog: for each token the
    canon CURRENTLY classifies as junk, pairing it with a single clean entity
    yields no edge (the junk endpoint is dropped before the pairwise step)."""
    from legba.data._entity_canon import canonicalize_entity, is_junk_entity

    for token in _MONEY_JUNK + _AGE_JUNK:
        text, cls = canonicalize_entity(token, "entity")
        if not is_junk_entity(text):
            # The canon does not (yet) catch this token — that is the canon
            # agent's scope, not the producer's. Skip; the producer only acts
            # on what the canon flags.
            continue
        sig_id = uuid.uuid4()
        conn = _FakeConn(
            [
                _signal_row(
                    sig_id,
                    [
                        {"text": "Zentavia", "class": "organization"},
                        {"text": token, "class": "entity"},
                    ],
                    f"Zentavia and {token}",
                )
            ]
        )
        counters = await entity_resolution._resolve_batch(
            _FakePool(conn), batch_limit=10, geocoder=None, run_id=None,
            analyst_id="er", analyst_version="d15-test",
        )
        assert counters["edges_upserted"] == 0, (token, counters)
        assert _proposed_edge_inserts(conn) == [], token


# ---------------------------------------------------------------------------
# Static guard — the INSERT statement actually names the derived_from column
# ---------------------------------------------------------------------------


def test_producer_insert_statement_names_derived_from():
    """A source-level regression guard: the producer's ``INSERT INTO
    proposed_edges`` column list MUST include ``derived_from`` (so a future edit
    that drops the column falls back to the '{}' default and silently re-breaks
    D15 is caught here without a DB)."""
    import inspect

    src = inspect.getsource(entity_resolution._resolve_batch)
    m = re.search(r"INSERT INTO proposed_edges\s*\((?P<cols>[^)]*)\)", src)
    assert m, "could not locate the proposed_edges INSERT in _resolve_batch"
    cols = m.group("cols")
    assert "derived_from" in cols, cols
