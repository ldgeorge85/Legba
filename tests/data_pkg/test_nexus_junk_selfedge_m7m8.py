# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ M7/M8 (nexus-write audit 2026-07-06) — junk / self-edge nexus gates.

Pure (no-network) unit tests for the two nexus producers'
endpoint hygiene. Every junk / self-edge input string is taken VERBATIM from the
live open-nexus catalogue that leaked into the signed / hostility graph:

  * M7 — a VAGUE bloc/adjective/role singleton ("West", "Islamic", "Leader"),
    a frequency adjective ("annual"), a bare quantifier plural ("Hundreds",
    "Millions"), or a relative-time phrase ("this week") reaching an endpoint;
  * M8 — a SELF-EDGE whose two endpoints are the same referent under the canon
    ("Africa"/"African", "Iran"/"Iranians", "Israel"/"Israeli", and the plain
    singular/plural "Houthi"/"Houthis" the canon does not lemma-map).

The shared gate (:func:`is_junk_entity` + :func:`same_referent`) is exercised
directly, then BOTH producers are exercised: the reifier via its pure
``_coerce_typing`` and the governance promoter via ``_promote_candidates`` over a
fake connection (junk / self rows are rejected, a clean dyad promotes).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data._entity_canon import is_junk_entity, same_referent
from legba.data.analysts.relationship_reifier import _coerce_typing
from legba.data.analysts.deterministic_handlers import proposed_edge_governance as peg
from legba.data.provenance import NexusPayload


# ---------------------------------------------------------------------------
# M7 — the shared is_junk_entity gate BOTH producers call on every endpoint.
# ---------------------------------------------------------------------------

# VERBATIM live junk endpoints — every one must be rejected.
_M7_JUNK = [
    # vague bloc / directional adjective
    "West", "Western", "Eastern", "Northern", "Southern",
    # ideological adjective
    "Islamic", "Islamist",
    # generic leadership role
    "Leader", "Leaders", "Leadership",
    # frequency adjective
    "annual", "monthly", "weekly", "daily", "quarterly",
    # bare quantifier plural
    "Hundreds", "Thousands", "Millions", "Billions", "Dozens",
    # relative-time phrase (already covered by _is_temporal_surface)
    "this week", "last week", "past day", "morning", "next month",
]

# Real single-token actors that must NEVER be junk-dropped (regression guard on
# the "do not reject a real country/org" contract).
_M7_REAL = [
    "China", "Iran", "Russia", "Israel", "United States", "Ukraine",
    "NATO", "OPEC", "ASEAN", "Hamas", "Houthi", "Hezbollah", "IRGC",
    "IAEA", "Yonhap", "Tehran", "Gaza",
]


@pytest.mark.parametrize("name", _M7_JUNK)
def test_m7_vague_and_relative_time_endpoints_are_junk(name):
    assert is_junk_entity(name) is True


@pytest.mark.parametrize("name", _M7_REAL)
def test_m7_real_actors_are_not_junk(name):
    assert is_junk_entity(name) is False


# ---------------------------------------------------------------------------
# M8 — same_referent: the self-edge gate BOTH producers call.
# ---------------------------------------------------------------------------

# VERBATIM live self-loops — subject/object name the SAME referent.
_M8_SELF = [
    ("Africa", "African"),
    ("Asia", "Asian"),
    ("Iran", "Iranians"),
    ("Iran", "Iranian"),
    ("Israel", "Israeli"),
    ("Russia", "Russians"),
    ("Colombia", "Colombians"),
    ("Somali", "Somalia"),
    ("US", "United States"),
    ("European Union", "the European Union"),
    # plain singular/plural the canon does NOT lemma-map (needs _singularize)
    ("Houthi", "Houthis"),
]

# Genuinely DISTINCT dyads that must NEVER fold to a self-edge.
_M8_DISTINCT = [
    ("Russia", "Ukraine"),
    ("Iran", "Israel"),
    ("India", "Pakistan"),
    ("North Korea", "South Korea"),
    ("United States", "United Kingdom"),
    ("Iran", "Iraq"),
    ("Hama", "Hamas"),  # 4-char stem guard: not a plural of each other
]


@pytest.mark.parametrize("a,b", _M8_SELF)
def test_m8_self_edges_are_same_referent(a, b):
    assert same_referent(a, b) is True
    assert same_referent(b, a) is True  # symmetric


@pytest.mark.parametrize("a,b", _M8_DISTINCT)
def test_m8_distinct_dyads_are_not_same_referent(a, b):
    assert same_referent(a, b) is False


# ---------------------------------------------------------------------------
# Producer 2 (relationship_reifier) — the pure _coerce_typing gate.
# ---------------------------------------------------------------------------


def _typed(subject, object_, rel_type="HostileTo"):
    return _coerce_typing(
        {"related": True, "rel_type": rel_type, "subject": subject,
         "object": object_, "intent": "hostile"},
        fallback_subject=subject, fallback_object=object_,
    )


@pytest.mark.parametrize("subject,object_", [
    ("West", "Yemen"),            # M7 vague SUBJECT
    ("United States", "Leader"),  # M7 role OBJECT
    ("IRNA", "Islamic"),          # M7 adjective OBJECT
    ("United States", "this week"),  # M7 relative-time OBJECT
    ("Hundreds", "Israel"),       # M7 quantifier-plural SUBJECT
])
def test_reifier_drops_m7_junk_endpoint(subject, object_):
    assert _typed(subject, object_) is None


@pytest.mark.parametrize("subject,object_", [
    ("Africa", "African"),
    ("Iran", "Iranians"),
    ("Houthi", "Houthis"),
    ("Israel", "Israeli"),
])
def test_reifier_drops_m8_self_edge(subject, object_):
    assert _typed(subject, object_) is None


def test_reifier_keeps_a_real_dyad():
    # positive control — a genuine hostile dyad still types (polarity -1).
    p = _typed("Iran", "Israel")
    assert isinstance(p, NexusPayload)
    assert p.subject == "Iran" and p.object == "Israel"
    assert p.polarity == -1


# ---------------------------------------------------------------------------
# Producer 1 (proposed_edge_governance) — _promote_candidates over a fake conn.
# A junk / self-edge co-occurrence is REJECTED (status='rejected', never
# written); a clean dyad PROMOTES (write_nexus stubbed).
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal async stand-in for an asyncpg connection.

    ``fetch`` returns the seeded candidate rows; ``execute`` records every
    ``UPDATE proposed_edges`` (so we can read back the status transition);
    ``fetchval`` (the "already reified?" probe) returns None so a clean row
    proceeds to the (stubbed) write path."""

    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, _q, *_args):
        return self._rows

    async def execute(self, q, *args):
        self.executed.append((q, args))
        return "UPDATE 1"

    async def fetchval(self, _q, *_args):
        return None


def _edge(source, target, conf=0.9):
    return {
        "id": uuid4(),
        "source_entity": source,
        "target_entity": target,
        "evidence_text": "",
        "confidence": conf,
        "derived_from": [],
        "produced_at": None,
    }


async def _run_promote(conn):
    return await peg._promote_candidates(
        conn,
        analyst_id="proposed_edge_governance",
        analyst_version="",
        run_id=None,
        target_id=None,
        target_version=None,
        min_confidence=0.6,
        limit=200,
    )


async def test_governance_rejects_m7_and_m8_endpoints(monkeypatch):
    # Any promotion write would be a bug for these rows; make write_nexus explode
    # so the test fails loudly if the gate ever lets one through.
    async def _boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("write_nexus called for a junk / self edge")

    monkeypatch.setattr(peg, "write_nexus", _boom)

    rows = [
        _edge("West", "Yemen"),            # M7 vague subject
        _edge("United States", "this week"),  # M7 relative-time object
        _edge("Hundreds", "Israel"),       # M7 quantifier plural
        _edge("Houthi", "Houthis"),        # M8 plain singular/plural self-loop
        _edge("Africa", "African"),        # M8 continent/adjective self-loop
        _edge("Iran", "Iranians"),         # M8 demonym-plural self-loop
    ]
    conn = _FakeConn(rows)
    promoted = await _run_promote(conn)

    assert promoted == 0
    # every candidate was UPDATEd to 'rejected' (and none 'promoted').
    assert len(conn.executed) == len(rows)
    for q, _args in conn.executed:
        assert "rejected" in q
        assert "promoted" not in q


async def test_governance_promotes_a_clean_dyad(monkeypatch):
    written: list = []

    async def _stub_write(_conn, *, analyst_ctx, payload, derived_from, source_signal_ids):
        written.append(payload)
        return object(), None  # (out, dlq) — success

    monkeypatch.setattr(peg, "write_nexus", _stub_write)

    conn = _FakeConn([_edge("Iran", "Israel")])
    promoted = await _run_promote(conn)

    assert promoted == 1
    assert len(written) == 1
    assert written[0].subject == "Iran" and written[0].object == "Israel"
    # the pending row was flipped to 'promoted', never 'rejected'.
    assert conn.executed and all("promoted" in q for q, _ in conn.executed)
