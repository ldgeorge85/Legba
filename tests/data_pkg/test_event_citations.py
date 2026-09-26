# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3/P2 — the ``event:<uuid>`` citation ref kind (spec §2.5).

Covers the contract end to end, DB-free:

* ``expand_event_citation`` — the provenance leaf: ranked expansion over
  ``signal_event_links`` JOIN ``signals``, per-signal entries carrying
  ``signal_id`` + real RAW ``source_text`` (the same precedence + 3,200-char
  cap as ``_citation_entry``), ``ref_kind="event"`` + ``event_id``, and the
  ``event_expansion_truncated`` stamp when the event outgrows the cap. The
  event's own ``summary`` is NEVER emitted — the expansion never touches
  the ``events`` row.
* The unit splice — ``_expand_event_refs``: body ``event:<uuid>`` tokens
  (bare or bracketed) rewrite to fresh ``[K]`` markers; the deterministic
  floor then scores a resolving event ``supported`` and an unresolvable
  one ``unresolved_citation``. ``derived_from`` carries the event id AND
  the expanded signal ids.
* The composition splice — ``_expand_event_markers``: ``[[event:<uuid>]]``
  markers rewrite into the sub-claim ``[[ref:K]]`` grammar with
  ``evidence_text`` the composition judge's evidence map reads.
* Flag-off byte-identity, the GROUNDING_REF_KINDS exclusion, and the
  lineage substrate-table entry.
"""
from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import inline_target as it
from legba.data.analysts.composition_event_citations import (
    _event_entry_builder,
    _expand_event_markers,
)
from legba.data.provenance import event_citations as ec
from legba.data.provenance.kinds import GROUNDING_REF_KINDS
from legba.data.provenance.models import FindingPayload
from legba.data.provenance.verify import _deterministic_floor
from legba.data.registry import lineage_api


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConn:
    """Duck-typed asyncpg pool/conn — ``.fetch`` records and returns."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append((sql, args))
        return self.rows


def _link_row(
    *, signal_id: UUID, relevance: float = 1.0,
    linked_at: str = "2026-09-22T00:00:00+00:00",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": signal_id,
        "payload": payload or {
            "title": "Signal headline",
            "body": "the raw signal body evidence",
        },
        "canonical_url": "https://example.com/article",
        "source_id": "src.test",
        "relevance": relevance,
        "linked_at": linked_at,
    }


def _finding(body: str, evidence: list[str] | None = None) -> FindingPayload:
    return FindingPayload(
        title="t", body=body, confidence=0.7,
        evidence=list(evidence or []),
    )


EVENT_ID = uuid4()


# ---------------------------------------------------------------------------
# The leaf — expand_event_citation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_emits_per_signal_entries_with_real_source_text():
    """N linked signals → N ordinary entries: signal_id + source_text +
    ref_kind='event' + event_id + fresh ordinals from start_ordinal."""
    sids = [uuid4() for _ in range(3)]
    conn = _FakeConn([_link_row(signal_id=s) for s in sids])
    entries = await ec.expand_event_citation(
        conn, EVENT_ID, start_ordinal=5, max_signals=4,
    )
    assert len(entries) == 3
    assert [e["signal_id"] for e in entries] == [str(s) for s in sids]
    assert [e["ordinal"] for e in entries] == [5, 6, 7]
    for e in entries:
        assert e["ref_kind"] == "event"
        assert e["event_id"] == str(EVENT_ID)
        assert e["source_text"] == "the raw signal body evidence"
        assert "event_expansion_truncated" not in e
    sql, args = conn.queries[0]
    # The expansion reads the LINK table + signals — never the events row.
    assert "signal_event_links" in sql and "JOIN signals" in sql
    assert "FROM events" not in sql
    # Deterministic member order: relevance DESC, then linked_at DESC.
    assert re.search(r"relevance\s+DESC", sql)
    assert re.search(r"linked_at\s+DESC", sql)
    # Cap+1 probe — the +1 row is how truncation is detected.
    assert args == (EVENT_ID, 5)


@pytest.mark.asyncio
async def test_expand_preserves_link_rank_and_truncation_stamp():
    """Cap=2 over 3 ranked links → the first 2 in order, every emitted
    entry stamped event_expansion_truncated."""
    sids = [uuid4() for _ in range(3)]
    rows = [
        _link_row(signal_id=sids[0], relevance=0.9),
        _link_row(signal_id=sids[1], relevance=0.7),
        _link_row(signal_id=sids[2], relevance=0.4),
    ]
    entries = await ec.expand_event_citation(
        _FakeConn(rows), EVENT_ID, start_ordinal=1, max_signals=2,
    )
    assert [e["signal_id"] for e in entries] == [
        str(sids[0]), str(sids[1]),
    ]
    assert all(e["event_expansion_truncated"] is True for e in entries)


@pytest.mark.asyncio
async def test_expand_empty_event_returns_no_entries():
    """Unknown event / no linked signals → [] — the caller owns the
    unresolved shape; the leaf never fabricates an entry."""
    entries = await ec.expand_event_citation(
        _FakeConn([]), EVENT_ID, start_ordinal=1, max_signals=4,
    )
    assert entries == []


@pytest.mark.asyncio
async def test_expand_default_entry_precedence_and_cap():
    """The mirrored RAW-source precedence (archived first, distilled_body
    NEVER — a summarizer output must not become judge evidence) and the
    same 3,200-char cap with source_truncated."""
    sid = uuid4()
    rows = [_link_row(
        signal_id=sid,
        payload={
            "title": "t",
            "distilled_body": "LLM summary — must never reach the grader",
            "archived_text": "archived article body",
            "raw_body": "raw http body",
        },
    )]
    entries = await ec.expand_event_citation(
        _FakeConn(rows), EVENT_ID, start_ordinal=1, max_signals=4,
    )
    assert entries[0]["source_text"] == "archived article body"
    assert "distilled" not in (entries[0]["source_text"] or "")

    big = "x " * 4000
    rows = [_link_row(signal_id=uuid4(), payload={"body": big})]
    entries = await ec.expand_event_citation(
        _FakeConn(rows), EVENT_ID, start_ordinal=1, max_signals=4,
    )
    assert len(entries[0]["source_text"]) == 3200
    assert entries[0]["source_truncated"] is True


@pytest.mark.asyncio
async def test_event_summary_never_in_source_text():
    """The sentinel: an events-row summary string placed NOWHERE in the
    signal payload must not surface anywhere in the emitted envelope."""
    sentinel = "EVENT SUMMARY PROSE — never evidence"
    rows = [_link_row(signal_id=uuid4(), payload={
        "title": "t", "body": "signal body",
        # Even if a signal payload carried the string, source_text stays
        # on the signal's own raw body (precedence picks it first anyway).
    })]
    conn = _FakeConn(rows)
    entries = await ec.expand_event_citation(
        conn, EVENT_ID, start_ordinal=1, max_signals=4,
    )
    assert sentinel not in json.dumps(entries)
    # And the expansion's SQL can never have read it — no events table.
    assert "FROM events" not in conn.queries[0][0]


def test_flag_and_cap_env_reads():
    """Both env knobs default safe: flag off, cap 4, floor 1."""
    import os
    os.environ.pop(ec.EVENT_CITATIONS_ENV, None)
    os.environ.pop(ec.EVENT_EXPANSION_MAX_ENV, None)
    assert ec.event_citations_enabled() is False
    assert ec.event_expansion_max_signals() == 4


# ---------------------------------------------------------------------------
# The unit splice — _expand_event_refs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unit_splice_rewrites_body_and_scores_supported():
    """``event:<uuid>`` in the body → fresh [K] markers + per-signal
    entries; the deterministic floor scores the clause SUPPORTED."""
    sid = uuid4()
    conn = _FakeConn([_link_row(signal_id=sid)])
    finding = _finding(f"Itaipu upgrade was completed event:{EVENT_ID}.")
    finding, entries, derived = await it._expand_event_refs(
        conn, finding, start_ordinal=4,
    )
    assert finding.body == f"Itaipu upgrade was completed [4]."
    assert len(entries) == 1
    e = entries[0]
    assert e["marker"] == "[4]"
    assert e["signal_id"] == str(sid)
    assert e["ref_kind"] == "event"
    assert e["event_id"] == str(EVENT_ID)
    assert e["source_text"]
    # derived carries the event id AND the expanded signal id.
    assert EVENT_ID in derived and sid in derived
    # The floor sees an ordinary [4] → signal_id bridge → supported.
    report = _deterministic_floor(finding.body, entries)
    assert report.supported_claims == 1
    assert report.faithfulness_score == 1.0


@pytest.mark.asyncio
async def test_unit_splice_bracketed_token_and_multiple_signals():
    """``[event:<uuid>]`` spelling expands identically; two signals → two
    markers appended adjacent, ordinals consecutive."""
    sids = [uuid4(), uuid4()]
    conn = _FakeConn([_link_row(signal_id=s) for s in sids])
    finding = _finding(f"Capacity rose [event:{EVENT_ID}].")
    finding, entries, _ = await it._expand_event_refs(
        conn, finding, start_ordinal=9,
    )
    assert finding.body == "Capacity rose [9][10]."
    assert [e["marker"] for e in entries] == ["[9]", "[10]"]


@pytest.mark.asyncio
async def test_unit_splice_unresolved_event_scores_unresolved_citation():
    """A cited event with no resolvable members → the token becomes a
    marker with no backing signal id → unresolved_citation (honest), and
    a bare ref_kind='event' entry records WHICH event was meant."""
    conn = _FakeConn([])
    finding = _finding(f"Itaipu upgrade was completed event:{EVENT_ID}.")
    finding, entries, derived = await it._expand_event_refs(
        conn, finding, start_ordinal=2,
    )
    assert finding.body == "Itaipu upgrade was completed [2]."
    assert entries == [{
        "marker": "[2]", "ref_kind": "event", "event_id": str(EVENT_ID),
    }]
    assert derived == [EVENT_ID]
    report = _deterministic_floor(finding.body, entries)
    assert report.supported_claims == 0
    assert report.unsupported_spans[0].reason == "unresolved_citation"


@pytest.mark.asyncio
async def test_unit_splice_evidence_list_token_expands_too():
    """``event:<uuid>`` in the ``evidence`` id-list (where the prompt tells
    the model to list source ids) expands the same way — entries emitted,
    no body rewrite needed."""
    sid = uuid4()
    conn = _FakeConn([_link_row(signal_id=sid)])
    finding = _finding("Wind capacity rose.", evidence=[f"event:{EVENT_ID}"])
    finding, entries, derived = await it._expand_event_refs(
        conn, finding, start_ordinal=1,
    )
    assert finding.body == "Wind capacity rose."  # untouched
    assert len(entries) == 1 and entries[0]["signal_id"] == str(sid)
    assert EVENT_ID in derived and sid in derived


@pytest.mark.asyncio
async def test_unit_splice_no_tokens_is_noop():
    finding = _finding("Wind capacity rose [1].")
    conn = _FakeConn([_link_row(signal_id=uuid4())])
    out, entries, derived = await it._expand_event_refs(
        conn, finding, start_ordinal=1,
    )
    assert out is finding and entries == [] and derived == []
    assert conn.queries == []  # no tokens → no substrate read at all


# ---------------------------------------------------------------------------
# The composition splice — _expand_event_markers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composition_event_marker_expands_to_subclaim_ordinals():
    """``[[event:<uuid>]]`` → ``[[ref:K]]`` ordinals past the slice space;
    entries carry signal_id + evidence_text (the composition judge's
    field) + ref_kind='event'."""
    sid = uuid4()
    finding = _finding(f"Escalation persisted [[event:{EVENT_ID}]].")
    citations: list[dict[str, Any]] = []
    emitted = await _expand_event_markers(
        _FakeConn([_link_row(signal_id=sid)]), finding, citations,
        start_ordinal=7,
    )
    assert emitted == 1
    assert finding.body == "Escalation persisted [[ref:7]]."
    e = citations[0]
    assert e["marker"] == "[[ref:7]]" and e["ordinal"] == 7
    assert e["signal_id"] == str(sid)
    assert e["ref_kind"] == "event" and e["event_id"] == str(EVENT_ID)
    # evidence_text mirrors source_text — the field the composition
    # judge's _ordinal_evidence_map reads.
    assert e["evidence_text"] == e["source_text"]
    assert e["evidence_text"]


@pytest.mark.asyncio
async def test_composition_unresolved_event_keeps_unresolvable_marker():
    """Empty expansion → body keeps ONE [[ref:K]] marker with NO resolvable
    ordinal behind it (unresolved_citation shape), plus a bare
    non-ordinal [[event:...]] entry recording the cited event."""
    finding = _finding(f"Escalation persisted [[event:{EVENT_ID}]].")
    citations: list[dict[str, Any]] = []
    emitted = await _expand_event_markers(
        _FakeConn([]), finding, citations, start_ordinal=3,
    )
    assert emitted == 1
    assert finding.body == "Escalation persisted [[ref:3]]."
    assert citations == [{
        "marker": f"[[event:{EVENT_ID}]]",
        "ref_kind": "event", "event_id": str(EVENT_ID),
    }]
    # The bare entry must NOT resolve ordinal 3 — no 'ordinal' key, and
    # its marker is not in [[ref:N]] grammar.
    assert "ordinal" not in citations[0]


# ---------------------------------------------------------------------------
# run_method end-to-end — flag-off byte-identity vs flag-on expansion
# ---------------------------------------------------------------------------


class _StubLLM:
    """Canned finding-JSON handler (same shape as the suite's stub)."""

    subprovider = "openai"

    def __init__(self, body: str, evidence: list[str]) -> None:
        self._content = json.dumps({
            "title": "t", "body": body, "confidence": 0.7,
            "evidence": evidence,
        })

    async def chat_complete(self, messages, **kwargs):
        class _R:
            usage = None
        r = _R()
        r.content = self._content
        return r


def _signal_row(id_: UUID) -> dict[str, Any]:
    return {
        "id": id_,
        "title": "Itaipu hydro upgrade",
        "produced_at": "2026-09-22T14:00:00+00:00",
        "source_url": "https://example.com/news",
        "data": {"summary": "snippet"},
    }


@pytest.mark.asyncio
async def test_run_method_flag_off_leaves_token_ordinary_text(monkeypatch):
    """Flag unset: deps.pg wired but the token stays in the body verbatim,
    no substrate read happens, no ref_kind='event' entry is emitted."""
    monkeypatch.delenv("LEGBA_EVENT_CITATIONS", raising=False)
    sid = uuid4()
    conn = _FakeConn([_link_row(signal_id=uuid4())])
    deps = it.InlineTargetDeps(
        llm=_StubLLM(
            body=f"Itaipu upgrade was completed event:{EVENT_ID}.",
            evidence=[f"event:{EVENT_ID}"],
        ),
        pg=conn,
    )
    result = await it.run_method(
        [_signal_row(sid)],
        {"target_id": "t1", "analyst_id": "a.test"},
        deps,
    )
    body = result.finding.body
    assert f"event:{EVENT_ID}" in body
    citations = (result.finding.data or {}).get("citations") or []
    assert not any(c.get("ref_kind") == "event" for c in citations)
    assert conn.queries == []
    assert EVENT_ID not in result.derived_from


@pytest.mark.asyncio
async def test_run_method_flag_on_expands_and_derives(monkeypatch):
    """Flag on + deps.pg: the token rewrites to [K] markers, the citations
    carry the expanded signal entries, and derived_from carries BOTH the
    event id and the expanded signal ids."""
    monkeypatch.setenv("LEGBA_EVENT_CITATIONS", "1")
    slice_id, member_id = uuid4(), uuid4()
    conn = _FakeConn([_link_row(signal_id=member_id)])
    deps = it.InlineTargetDeps(
        llm=_StubLLM(
            body=f"Itaipu upgrade was completed event:{EVENT_ID}.",
            evidence=[f"event:{EVENT_ID}"],
        ),
        pg=conn,
    )
    result = await it.run_method(
        [_signal_row(slice_id)],
        {"target_id": "t1", "analyst_id": "a.test"},
        deps,
    )
    body = result.finding.body
    assert f"event:{EVENT_ID}" not in body
    assert "[2]" in body  # one input → event entries start at ordinal 2
    citations = (result.finding.data or {}).get("citations") or []
    event_entries = [c for c in citations if c.get("ref_kind") == "event"]
    assert len(event_entries) == 1
    e = event_entries[0]
    assert e["signal_id"] == str(member_id)
    assert e["event_id"] == str(EVENT_ID)
    assert e["source_text"]
    assert EVENT_ID in result.derived_from
    assert member_id in result.derived_from
    # The floor sees the ordinary bridge.
    report = _deterministic_floor(body, citations)
    assert report.supported_claims == 1


# ---------------------------------------------------------------------------
# Flag-off byte-identity + the kinds/lineage invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_default_and_no_expansion(monkeypatch):
    """LEGBA_EVENT_CITATIONS unset → enabled() False; the run_method splice
    is gated on it so a finding citing event:<uuid> stays byte-identical."""
    monkeypatch.delenv("LEGBA_EVENT_CITATIONS", raising=False)
    assert ec.event_citations_enabled() is False
    monkeypatch.setenv("LEGBA_EVENT_CITATIONS", "1")
    assert ec.event_citations_enabled() is True


# ---------------------------------------------------------------------------
# THE OFFER meets THE EXPANSION — the grammar round-trip (wave I, p2_offer)
# ---------------------------------------------------------------------------
#
# Everything above tests the expansion of a token the test itself typed. The
# live gap P2 left is that nothing in the tree ever RENDERED an event id into a
# prompt, so no model had ever typed one: 59 receipts after
# ``LEGBA_EVENT_CITATIONS=1`` all read ``event_citations: 0``. These tests
# close the loop at the seam — the token is lifted OUT of the rendered OPEN
# EVENTS block, exactly as a model copies it, and fed to the splice.


def _rendered_open_events_block(event_id, ordinal: int = 44) -> str:
    """The OPEN EVENTS block as a granted unit's prompt actually carries it."""
    from legba.data.analysts import unit_grounding as ug

    row = {
        ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_OPEN_EVENTS,
        ug.GROUNDING_PAYLOAD_KEY: [
            {
                "event_id": str(event_id),
                "title": "Ukrainian drones set fire to the Kapotnya refinery",
                "time_start": "2026-09-20T12:15:17+00:00",
                "time_end": None,
                "report_count": 168,
                "source_count": 34,
                "lifecycle_state": "active",
                "updated_at": "2026-09-24T03:15:38+00:00",
                "updated_age_days": 0.9,
            }
        ],
    }
    text, stamped = ug.render_grounding_section([row], start_ordinal=ordinal)
    assert stamped, "the offer must render or there is nothing to round-trip"
    return text


@pytest.mark.asyncio
async def test_the_offered_token_is_exactly_what_the_expansion_accepts():
    """The round-trip: the token the OPEN EVENTS block PRINTS, copied verbatim
    into a finding body, is expanded by ``_expand_event_refs`` into [K] markers
    over the event's member signals, with ref_kind='event' entries and the
    event id on derived_from.

    Lifted from the RENDERED TEXT rather than re-typed, because re-typing the
    token would test the test's own spelling — the whole defect class here is
    an offer whose grammar the expansion does not accept.
    """
    sid = uuid4()
    rendered = _rendered_open_events_block(EVENT_ID)
    found = ec.EVENT_ID_RE.findall(rendered)
    assert found == [str(EVENT_ID)], (
        "the block must print exactly one resolvable token per event, in the "
        f"grammar the expansion parses — got {found!r}"
    )
    token = f"event:{found[0]}"
    assert token in rendered

    conn = _FakeConn([_link_row(signal_id=sid)])
    finding = _finding(f"The refinery strike is the window's driver {token}.")
    finding, entries, derived = await it._expand_event_refs(
        conn, finding, start_ordinal=45,
    )
    assert finding.body == "The refinery strike is the window's driver [45]."
    assert [e["ref_kind"] for e in entries] == ["event"]
    assert entries[0]["signal_id"] == str(sid)
    assert entries[0]["event_id"] == str(EVENT_ID)
    assert entries[0]["source_text"]
    # Rule 4 — the lineage carries BOTH the event and its expanded signal.
    assert EVENT_ID in derived and sid in derived
    # ...and the floor reads the rewritten body as an ordinary cited clause.
    report = _deterministic_floor(finding.body, entries)
    assert report.supported_claims == 1


@pytest.mark.asyncio
async def test_the_offered_token_round_trips_from_the_evidence_list_too():
    """The block's closing rule names BOTH positions ("inline in the body or as
    an evidence entry"). A rule that names a position the splice does not read
    would send a desk's citation nowhere."""
    sid = uuid4()
    rendered = _rendered_open_events_block(EVENT_ID)
    token = f"event:{ec.EVENT_ID_RE.findall(rendered)[0]}"
    conn = _FakeConn([_link_row(signal_id=sid)])
    finding = _finding("The refinery strike is the window's driver.", [token])
    _finding_out, entries, derived = await it._expand_event_refs(
        conn, finding, start_ordinal=7,
    )
    assert [e["marker"] for e in entries] == ["[7]"]
    assert entries[0]["signal_id"] == str(sid)
    assert EVENT_ID in derived


@pytest.mark.asyncio
async def test_the_blocks_own_instruction_placeholder_never_expands():
    """The closing rule spells the shape as the literal ``event:<uuid>``. A
    desk that copies the RULE rather than a LINE must produce nothing — not an
    expansion, not an unresolved marker against a fabricated id."""
    from legba.data.analysts.unit_grounding import OPEN_EVENTS_CITE_RULE

    assert "event:<uuid>" in OPEN_EVENTS_CITE_RULE
    assert ec.EVENT_ID_RE.search(OPEN_EVENTS_CITE_RULE) is None
    conn = _FakeConn([_link_row(signal_id=uuid4())])
    finding = _finding(f"A desk that copied the rule: {OPEN_EVENTS_CITE_RULE}")
    out, entries, derived = await it._expand_event_refs(
        conn, finding, start_ordinal=3,
    )
    assert entries == [] and derived == []
    assert out.body == finding.body
    assert conn.queries == []


def test_the_offer_block_never_carries_an_event_summary():
    """§2.5 rules 1-2: an event's own prose is never evidence. The block must
    not print it even when a caller hands one down in the payload."""
    from legba.data.analysts import unit_grounding as ug

    leak = "SUMMARY-PROSE-THAT-MUST-NEVER-REACH-A-PROMPT"
    row = {
        ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_OPEN_EVENTS,
        ug.GROUNDING_PAYLOAD_KEY: [
            {
                "event_id": str(EVENT_ID),
                "title": "A bounded occurrence",
                "summary": leak,
                "time_start": "2026-09-20T12:15:17+00:00",
                "time_end": None,
                "report_count": 4,
                "source_count": 3,
                "lifecycle_state": "active",
                "updated_at": "2026-09-24T03:15:38+00:00",
                "updated_age_days": 0.9,
            }
        ],
    }
    text, stamped = ug.render_grounding_section([row], start_ordinal=1)
    assert leak not in text
    citation = ug.citation_for_block(row, 1)
    assert citation is not None and leak not in citation["evidence_text"]
    # The reader does not even SELECT the column (belt and braces, one level up).
    assert "summary" not in ug._OPEN_EVENTS_SQL


def test_the_offer_block_is_not_the_event_ref_kind():
    """Two different things: the BLOCK is a grounding block graded on its own
    rendered text; an EXPANDED entry is a per-signal citation graded on the
    report. Collapsing them is how an event ends up graded on its own
    summary — so the block's kind is registered and 'event' still is not."""
    from legba.data.analysts import unit_grounding as ug

    assert ug.GROUNDING_OPEN_EVENTS == "open_events"
    assert ug.GROUNDING_OPEN_EVENTS in GROUNDING_REF_KINDS
    assert "event" not in GROUNDING_REF_KINDS


def test_event_not_in_grounding_ref_kinds():
    """'event' must never be a synthetic grounding block — expanded
    entries carry signal_id so is_grounding_citation already returns
    False; registering the kind would let an event cite its own summary."""
    assert "event" not in GROUNDING_REF_KINDS


def test_events_table_in_lineage_substrate():
    """The walk resolves event ids on derived_from (spec §2.5 rule 4);
    journal_entries stays excluded."""
    tables = {t.table for t in lineage_api._SUBSTRATE_TABLES}
    assert "events" in tables
    assert "journal_entries" not in tables


@pytest.mark.asyncio
async def test_event_entry_builder_uses_citation_entry_precedence():
    """The composition adapter is the REAL _citation_entry under the hood —
    archived_text beats raw_body; distilled_body is never consulted; and
    evidence_text mirrors source_text."""
    sid = uuid4()
    entry = _event_entry_builder(
        signal_id=str(sid), title="t", source="https://x",
        source_id="src", fields={
            "distilled_body": "never",
            "archived_text": "archived article",
            "raw_body": "raw",
        },
    )
    assert entry["source_text"] == "archived article"
    assert entry["evidence_text"] == "archived article"


# ---------------------------------------------------------------------------
# The degrade paths are COUNTED (09-23 review item 1, the flag's precondition)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unit_splice_counts_a_raised_expansion_and_keeps_the_bare_entry(monkeypatch):
    """Before 2026-09-24 a raising ``expand_event_citation`` reached only a
    warning line; the receipt said ``event_citations: 0`` as if nothing had
    been cited. The bare entry still lands (honest), and the receipt tally
    names the degrade."""
    async def _boom(*_a, **_k):
        raise RuntimeError("pool blip")
    monkeypatch.setattr(it, "expand_event_citation", _boom)
    conn = _FakeConn([])
    finding = _finding(f"Itaipu upgrade was completed event:{EVENT_ID}.")
    stats: dict[str, int] = {}
    finding, entries, derived = await it._expand_event_refs(
        conn, finding, start_ordinal=4, stats=stats,
    )
    assert stats == {"event_expand_failed": 1}
    assert [e for e in entries if e.get("ref_kind") == "event" and not e.get("signal_id")]
    assert EVENT_ID in derived


@pytest.mark.asyncio
async def test_unit_splice_counts_an_unresolved_event_separately():
    conn = _FakeConn([])  # the event has no member links
    finding = _finding(f"Itaipu upgrade was completed event:{EVENT_ID}.")
    stats: dict[str, int] = {}
    await it._expand_event_refs(conn, finding, start_ordinal=4, stats=stats)
    assert stats == {"event_unresolved": 1}


@pytest.mark.asyncio
async def test_composition_splice_counts_a_raised_expansion(monkeypatch):
    from legba.data.analysts import composition_event_citations as cec

    async def _boom(*_a, **_k):
        raise RuntimeError("pool blip")
    monkeypatch.setattr(cec, "expand_event_citation", _boom)
    conn = _FakeConn([])
    finding = _finding(f"Itaipu upgrade was completed [[event:{EVENT_ID}]].")
    citations: list[dict] = []
    stats: dict[str, int] = {}
    emitted = await _expand_event_markers(
        conn, finding, citations, start_ordinal=3, stats=stats,
    )
    assert stats == {"event_expand_failed": 1}
    assert emitted == 1  # the bare, traceable entry
    assert citations[-1]["event_id"] == str(EVENT_ID) and "signal_id" not in citations[-1]

