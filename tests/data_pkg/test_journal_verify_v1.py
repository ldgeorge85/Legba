# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V1 — the journal verify profile (chronicle gate).

Locks the two pure-ish pieces:

  * ``journal_assessor.build_journal_verify_inputs`` — the CITED-FACT-ONLY
    document: perspective claims are EXEMPT by construction (§10 flag-never-
    strip), uncited fact claims (already ``[needs_citation]``-flagged by
    REFLECT) are excluded, ``[[ref:<uuid>]]`` markers rewrite to the ordinal
    ``[N]`` form with a stable first-seen ordinal map shared across claims.
  * ``actor_critic._resolve_journal_citation_bridge`` — uuid → bridge entries
    (analyst_outputs first, signals fallback, unresolved kept in-place so
    ordinals never skew), via a routing fake connection.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

from legba.data.analysts.journal_assessor import build_journal_verify_inputs
from legba.data.provenance.models import JournalClaim
from legba.runtime.actor_critic import _resolve_journal_citation_bridge


class _P:  # minimal JournalPayload stand-in (duck-typed: .claims)
    def __init__(self, claims):
        self.claims = claims


def _fact(text, refs):
    return JournalClaim(text_span=text, refs=refs, kind="fact")


def _persp(text, refs=()):
    return JournalClaim(text_span=text, refs=list(refs), kind="perspective")


# ---------------------------------------------------------------------------
# the transform
# ---------------------------------------------------------------------------


def test_cited_fact_claims_become_ordinal_doc() -> None:
    a, b = uuid4(), uuid4()
    doc, refs = build_journal_verify_inputs(_P([
        _fact(f"Strikes hit the port [[ref:{a}]].", [a]),
        _fact(f"The blockade resumed [[ref:{b}]] overnight.", [b]),
    ]))
    assert refs == [str(a).lower(), str(b).lower()]
    assert "[1]" in doc and "[2]" in doc
    assert "[[ref:" not in doc
    assert doc.count("\n\n") == 1  # two paragraphs


def test_shared_ref_gets_one_ordinal_across_claims() -> None:
    a = uuid4()
    doc, refs = build_journal_verify_inputs(_P([
        _fact(f"First mention [[ref:{a}]].", [a]),
        _fact(f"Second mention [[ref:{a}]].", [a]),
    ]))
    assert refs == [str(a).lower()]
    assert doc.count("[1]") == 2 and "[2]" not in doc


def test_perspective_exempt_and_uncited_fact_excluded() -> None:
    a = uuid4()
    doc, refs = build_journal_verify_inputs(_P([
        _persp("I wonder whether the quiet is the storm."),
        _fact("[needs_citation] Something asserted without a ref.", []),
        _fact(f"A real cited claim [[ref:{a}]].", [a]),
    ]))
    assert refs == [str(a).lower()]
    assert "wonder" not in doc and "needs_citation" not in doc
    assert "[1]" in doc


def test_listed_ref_missing_from_span_is_appended() -> None:
    a = uuid4()
    # refs carries a but the span lost its inline marker — the floor must
    # still see the support, so a trailing [1] is appended.
    doc, refs = build_journal_verify_inputs(_P([_fact("Marker-less span.", [a])]))
    assert refs == [str(a).lower()]
    assert doc.endswith("[1]")


def test_all_perspective_entry_yields_empty_doc() -> None:
    doc, refs = build_journal_verify_inputs(_P([_persp("Pure reflection.")]))
    assert doc == "" and refs == []
    doc2, refs2 = build_journal_verify_inputs(_P([]))
    assert doc2 == "" and refs2 == []


def test_claim_cap_is_a_backstop() -> None:
    claims = [_fact(f"Claim {i} [[ref:{uuid4()}]].", [uuid4()]) for i in range(60)]
    # NOTE each claim above has a span-marker uuid AND a different listed ref —
    # both get ordinals; the point here is only the 40-claim cap.
    doc, refs = build_journal_verify_inputs(_P(claims))
    assert doc.count("\n\n") == 39  # 40 paragraphs kept


# ---------------------------------------------------------------------------
# the bridge resolver (routing fake conn)
# ---------------------------------------------------------------------------


class _Row(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


class _RoutingConn:
    """Routes fetches by table name in the SQL (the H-2 fake-conn pattern)."""

    def __init__(self, outputs_rows=(), signal_rows=(), fact_rows=(),
                 situation_rows=(), nexus_rows=(), hypothesis_rows=()):
        self._by_table = {
            "FROM analyst_outputs": list(outputs_rows),
            "FROM signals": list(signal_rows),
            "FROM facts": list(fact_rows),
            "FROM situations": list(situation_rows),
            "FROM nexuses": list(nexus_rows),
            "FROM hypotheses": list(hypothesis_rows),
        }

    async def fetch(self, sql, *args):
        for frag, rows in self._by_table.items():
            if frag in sql:
                return rows
        raise AssertionError(f"unexpected sql: {sql}")


def test_bridge_resolves_outputs_then_signals_then_unresolved() -> None:
    f_id, s_id, missing = uuid4(), uuid4(), uuid4()
    conn = _RoutingConn(
        outputs_rows=[_Row(id=f_id, title="Calibration snapshot",
                           body="Brier 0.21 over 34 resolved.", analyst_id="calibration")],
        signal_rows=[_Row(id=s_id, canonical_url="https://example.org/x",
                          payload={"title": "Port strike", "summary": "Two vessels hit."})],
    )
    ordered = [str(f_id), str(s_id), str(missing)]
    entries = asyncio.run(_resolve_journal_citation_bridge(conn, ordered))
    assert [e["marker"] for e in entries] == ["[1]", "[2]", "[3]"]
    assert entries[0]["source"] == "calibration"
    assert "Brier" in entries[0]["source_text"]
    assert entries[1]["title"] == "Port strike"
    assert "vessels" in entries[1]["source_text"]
    # unresolved keeps its slot (ordinals never skew) with empty evidence
    assert entries[2]["source"] == "unresolved" and entries[2]["source_text"] == ""
    # C-1 regression: a resolved ref carries signal_id (floor support); an
    # UNRESOLVED (possibly fabricated) ref must NOT — else fabrication passes
    # the deterministic floor whenever the judge soft-fails.
    assert entries[0]["signal_id"] == str(f_id)
    assert entries[1]["signal_id"] == str(s_id)
    assert "signal_id" not in entries[2]


def test_bridge_resolves_fact_situation_nexus_hypothesis_kinds() -> None:
    # C-2 regression: the journal legitimately cites facts / situations /
    # nexuses / hypotheses (the journal_read pack returns their uuids); the
    # bridge must resolve them, not demote them as unresolved.
    fa, si, nx, hy = uuid4(), uuid4(), uuid4(), uuid4()
    conn = _RoutingConn(
        fact_rows=[_Row(id=fa, label="iran LeaderOf khamenei", text="iran LeaderOf khamenei")],
        situation_rows=[_Row(id=si, label="Hormuz shipping crisis", text="Hormuz shipping crisis")],
        nexus_rows=[_Row(id=nx, label="US -HostileTo-> IR", text="US -HostileTo-> IR")],
        hypothesis_rows=[_Row(id=hy, label="Escalation persists", text="Escalation persists through Q3.")],
    )
    ordered = [str(fa), str(si), str(nx), str(hy)]
    entries = asyncio.run(_resolve_journal_citation_bridge(conn, ordered))
    assert all("signal_id" in e for e in entries)
    assert entries[0]["title"] == "iran LeaderOf khamenei"
    assert entries[1]["title"] == "Hormuz shipping crisis"
    assert entries[2]["title"] == "US -HostileTo-> IR"
    assert "Q3" in entries[3]["source_text"]


def test_bridge_empty_refs_is_empty() -> None:
    entries = asyncio.run(_resolve_journal_citation_bridge(_RoutingConn(), []))
    assert entries == []


# ---------------------------------------------------------------------------
# V2 — the [[instrument]] marker + deterministic denominator line
# ---------------------------------------------------------------------------


def test_instrument_marker_makes_span_perspective() -> None:
    from legba.data.analysts.journal_assessor import _reflect_claims
    claims, refs, flags = _reflect_claims(
        "US betweenness centrality is 0.135 this window [[instrument]].\n\n"
        "Kuwait was struck overnight [[ref:%s]]." % ("a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12)
    )
    inst = next(c for c in claims if "[[instrument]]" in c.text_span)
    assert inst.kind == "perspective" and inst.refs == []
    # the marker is KEPT (flag-never-strip)
    assert "[[instrument]]" in inst.text_span


def test_denominator_line_rendered_from_slice() -> None:
    from legba.data.analysts.journal_assessor import _render_user_prompt
    rows = [
        {"id": "x1", "title": "a", "source_id": "s1", "produced_at": "2026-07-17T00:00:00+00:00"},
        {"id": "x2", "title": "b", "source_id": "s2", "produced_at": "2026-07-17T00:00:00+00:00"},
        {"id": "x3", "title": "c", "source_id": "s1", "produced_at": "2026-07-17T00:00:00+00:00"},
    ]
    out = _render_user_prompt(rows)
    assert "from at least 2 DISTINCT wired sources" in out
    assert "INSTRUMENT CITATIONS" in out
    assert "the apparatus is your POSTSCRIPT" in out


def test_instrument_marker_on_world_span_downgrades_to_fact() -> None:
    # Review-A guard: [[instrument]] wearing world proper nouns is a citation
    # dodge — the exemption is stripped (marker + text kept) and the span falls
    # through as an uncited fact ([needs_citation]-flagged).
    from legba.data.analysts.journal_assessor import _reflect_claims
    claims, refs, flags = _reflect_claims(
        "Kuwait's Mutla Ridge site was struck overnight; 40 dead [[instrument]]."
    )
    assert "instrument_marker_on_world_span" in flags
    dodge = next(c for c in claims if "Mutla" in c.text_span)
    assert dodge.kind == "fact" and dodge.refs == []
    assert "[needs_citation]" in dodge.text_span
    # a GENUINE self-metric span keeps the exemption
    claims2, _, flags2 = _reflect_claims(
        "US betweenness centrality sits at 0.135 this window [[instrument]]."
    )
    inst = next(c for c in claims2 if "betweenness" in c.text_span)
    assert inst.kind == "perspective"


# ---------------------------------------------------------------------------
# T-3 — a judge_contradicted verdict stamps a durable honesty flag on the entry.
# ---------------------------------------------------------------------------


class _StampConn:
    """Fake conn that records the honesty_flags UPDATE (T-3)."""

    def __init__(self, raise_on_execute: bool = False):
        self.executed: list[tuple[str, tuple]] = []
        self.raise_on_execute = raise_on_execute

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if self.raise_on_execute:
            raise RuntimeError("boom")


def test_stamp_contradicted_flag_updates_journal_entry() -> None:
    from legba.runtime.actor_critic import (
        _JOURNAL_CONTRADICTED_FLAG,
        _stamp_journal_contradicted_flag,
    )

    entry_id = uuid4()
    conn = _StampConn()
    asyncio.run(_stamp_journal_contradicted_flag(conn, entry_id))
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    # Durable, idempotent append onto the journal_entries.honesty_flags text[].
    assert "UPDATE journal_entries" in sql
    assert "honesty_flags" in sql
    assert "array_append" in sql
    assert args[0] == entry_id
    assert args[1] == _JOURNAL_CONTRADICTED_FLAG == "contradicted_claims"
    # Idempotency guard present (never duplicates the flag).
    assert "@>" in sql


def test_stamp_contradicted_flag_is_fail_safe() -> None:
    # A write error must NEVER crash the run (the entry + critique already landed).
    from legba.runtime.actor_critic import _stamp_journal_contradicted_flag

    conn = _StampConn(raise_on_execute=True)
    # must not raise
    asyncio.run(_stamp_journal_contradicted_flag(conn, uuid4()))
    assert len(conn.executed) == 1


def test_contradicted_detection_predicate_matches_report_span() -> None:
    # The actor_critic branch keys on UnsupportedSpan.reason == 'judge_contradicted'
    # in report.unsupported_spans — verify that exact predicate over the real type.
    from legba.data.provenance.verify import FaithfulnessReport, UnsupportedSpan

    contradicted = FaithfulnessReport(
        faithfulness_score=0.5, checkable_claims=2, supported_claims=1,
        unsupported_spans=[UnsupportedSpan(text="Rubio is Iran's FM", reason="judge_contradicted")],
        judge_status="llm",
    )
    clean = FaithfulnessReport(
        faithfulness_score=0.9, checkable_claims=2, supported_claims=2,
        unsupported_spans=[UnsupportedSpan(text="x", reason="judge_unsupported")],
        judge_status="llm",
    )

    def _has_contradiction(report):
        return any(
            getattr(s, "reason", None) == "judge_contradicted"
            for s in report.unsupported_spans
        )

    assert _has_contradiction(contradicted) is True
    assert _has_contradiction(clean) is False
