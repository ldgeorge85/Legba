# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""7d — THE INDEPENDENCE GATE and the circular-reporting flag.

What this file proves, in the brief's own order:

  * A claim cited to two signals from ONE source is marked — on the citation
    (``single_source: true``) and in the record (``[single-source]`` beside the
    ordinal).
  * Two sources → not marked, and the attribution line is byte-for-byte the one
    that shipped before this piece existed.
  * Two sources whose items are near-verbatim → folded to one, and therefore
    marked; the fold is stated (``wire-folded``) rather than silently
    subtracted from the sources count.
  * ``descriptors/wire_map.yaml`` validates and agrees with the map that
    actually runs.
  * ``claims_single_source`` / ``claims_wire_folded`` ride the ``cite`` receipt.
  * There is exactly ONE near-duplicate implementation and ONE cited-signal
    reader behind all of it.

The fixture pairs are the LIVE ones, measured read-only over 30 days on
2026-09-23 — Asharq Al-Awsat and CNA running one Reuters dispatch in Title Case
and sentence case respectively (46 such days in 30), which is precisely the
pair ``signals.content_hash`` cannot see because the publishers' URLs differ.
"""

from __future__ import annotations

import pathlib
from uuid import uuid5, NAMESPACE_URL

import pytest
import yaml

from legba.data import _url_canon
from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assembly_render as ar
from legba.data.analysts import composition_citations as cc
from legba.data.analysts import source_independence as si
from legba.data.filters import dedupe as dd

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WIRE_MAP_PATH = REPO_ROOT / "descriptors" / "wire_map.yaml"

#: The measured pair, verbatim from the live substrate.
REUTERS_HEADLINE_TITLE_CASE = (
    "Antonelli Takes Sensational Home Italian GP Win from Back of Grid"
)
REUTERS_HEADLINE_SENTENCE_CASE = (
    "Antonelli takes sensational home Italian GP win from back of grid"
)

LEDE_A = (
    "MONZA, Italy (Reuters) - Kimi Antonelli held off a late charge to take a "
    "sensational home victory at the Italian Grand Prix on Sunday, climbing "
    "from the back of the grid after a gearbox penalty."
)
LEDE_B = (
    "Kimi Antonelli held off a late charge to take a sensational home victory "
    "at the Italian Grand Prix on Sunday, climbing from the back of the grid "
    "after a gearbox penalty."
)
OTHER_LEDE = (
    "Rail freight across the southern corridor halted on Tuesday as signalling "
    "crews walked out over a pay dispute that unions say has run for eleven "
    "months without a serious offer from the operator."
)


def _cit(
    signal_id: str,
    source_id: str,
    *,
    title: str = "A headline with at least five tokens in it",
    body: str = "",
) -> dict:
    out = {
        "marker": f"[{signal_id}]",
        "signal_id": signal_id,
        "source_id": source_id,
        "title": title,
        "source": f"https://example.test/{signal_id}",
    }
    if body:
        out["source_text"] = body
    return out


#: The origin head's own body. Quotable: the assembler refuses to construct a
#: block it cannot cut a span from, which is F-12 doing its job.
DESK_BODY = (
    "**BLUF:** Two separate deployments were confirmed along the northern "
    "corridor this week, and the second has not been publicly acknowledged.\n"
    "\n"
    "## What changed\n"
    "- The second convoy crossed on 2 September [1].\n"
)


def _head(uid: str, citations: list[dict], *, body: str = DESK_BODY) -> dict:
    """An origin head row in the shape the slice and the assembler both read.

    ``id`` is a real uuid because ``_build_composition_citation`` refuses a row
    with no resolvable drill target — never a fabricated ref.
    """
    return {
        "id": str(uuid5(NAMESPACE_URL, f"legba.test.head.{uid}")),
        "analyst_id": "escalation",
        "target_id": "country_watch_ir",
        "title": f"head {uid}",
        "body": body,
        "severity": "high",
        "produced_at": "2026-09-03T10:01:00+00:00",
        "faithfulness_score": 0.90,
        "effective_confidence": 0.80,
        "derived_from": [
            c["signal_id"] for c in citations if c.get("signal_id")
        ],
        "data": {"data": {"citations": citations}},
    }


# ---------------------------------------------------------------------------
# THE COUNT — one source, two sources, and the fold
# ---------------------------------------------------------------------------


def test_two_signals_from_one_source_is_single_source() -> None:
    ind = si.independence_of([
        _cit("s1", "source.bbc.world"), _cit("s2", "source.bbc.world"),
    ])
    assert (ind.cited, ind.sources, ind.independent) == (2, 1, 1)
    assert ind.known and ind.single_source and ind.folded == 0


def test_two_distinct_sources_are_not_single_source() -> None:
    ind = si.independence_of([
        _cit("s1", "source.bbc.world", title="Flooding displaces thousands in "
             "northern Japan", body=OTHER_LEDE),
        _cit("s2", "source.dw.world", title=REUTERS_HEADLINE_TITLE_CASE,
             body=LEDE_A),
    ])
    assert (ind.sources, ind.independent, ind.folded) == (2, 2, 0)
    assert not ind.single_source


def test_no_cited_outlet_at_all_is_unknown_and_never_single_source() -> None:
    """A composition origin cites FINDINGS. That is UNKNOWN, not one source."""
    ind = si.independence_of([
        {"marker": "[[ref:1]]", "ref_id": "x", "ref_kind": "finding",
         "evidence_text": LEDE_A},
    ])
    assert not ind.known and not ind.single_source
    assert (ind.cited, ind.sources, ind.independent, ind.folded) == (0, 0, 0, 0)
    assert si.independence_of([]) == si.independence_of(None) == si.Independence()


def test_a_source_id_with_no_signal_id_is_not_a_cited_signal() -> None:
    """The denominator is the SIGNALS the block renders — the same filter
    ``assembly_payload._signals_block`` applies, so the two cannot disagree."""
    assert not si.independence_of([
        {"source_id": "source.bbc.world", "title": "x"},
    ]).known


def test_near_verbatim_headlines_from_two_outlets_fold_to_one() -> None:
    """THE MEASURED PAIR. Title Case vs sentence case, two publishers, one
    Reuters dispatch — invisible to the content hash, folded here."""
    ind = si.independence_of([
        _cit("s1", "source.aawsat.english", title=REUTERS_HEADLINE_TITLE_CASE),
        _cit("s2", "source.cna.all", title=REUTERS_HEADLINE_SENTENCE_CASE),
    ])
    assert (ind.sources, ind.independent, ind.folded) == (2, 1, 1)
    assert ind.single_source


def test_near_verbatim_bodies_fold_even_when_the_headlines_were_rewritten() -> None:
    """The brief's own case: the bodies decide when the mastheads re-headline.

    One copy carries a dateline the other dropped, so the two lede windows sit
    24 characters out of phase over 160 — a distance inside the DECLARED bar
    and outside the undeclared one, which is the map doing exactly the job it
    exists for and nothing more.
    """
    rewritten = [
        _cit("s1", "source.dawn.home",
             title="Antonelli storms from the back to win at Monza",
             body=LEDE_A),
        _cit("s2", "source.jpost.frontpage",
             title="Italian Grand Prix goes to the teenage home favourite",
             body=LEDE_B),
    ]
    ind = si.independence_of(rewritten)
    assert (ind.sources, ind.independent, ind.folded) == (2, 1, 1)
    assert ind.single_source
    # The same two bodies under outlets the map does not relate: two sources.
    rewritten[0]["source_id"] = "source.bbc.world"
    rewritten[1]["source_id"] = "source.dw.world"
    assert si.independence_of(rewritten).independent == 2


def test_different_stories_from_two_outlets_never_fold() -> None:
    """The safety direction: a false fold erases a real second source."""
    ind = si.independence_of([
        _cit("s1", "source.dawn.home", title=REUTERS_HEADLINE_TITLE_CASE,
             body=LEDE_A),
        _cit("s2", "source.jpost.frontpage",
             title="Rail strike halts freight across the southern corridor",
             body=OTHER_LEDE),
    ])
    assert (ind.sources, ind.independent, ind.folded) == (2, 2, 0)


def test_a_generic_headline_under_the_token_floor_never_keys_a_fold() -> None:
    ind = si.independence_of([
        _cit("s1", "source.dawn.home", title="Morning briefing"),
        _cit("s2", "source.jpost.frontpage", title="Morning briefing"),
    ])
    assert ind.independent == 2


def test_a_stub_body_under_the_character_floor_never_keys_a_fold() -> None:
    stub = "Breaking news."
    ind = si.independence_of([
        _cit("s1", "source.dawn.home", title="One headline of five tokens here",
             body=stub),
        _cit("s2", "source.jpost.frontpage",
             title="Another headline of five tokens", body=stub),
    ])
    assert ind.independent == 2


def test_same_publisher_feeds_fold_with_no_content_test_at_all() -> None:
    """An identity statement, not a content judgement: two UN News feeds are
    the UN News desk, once, whatever they happened to publish."""
    ind = si.independence_of([
        _cit("s1", "source.un_news.africa", title="Displacement on the rise in "
             "Yemen as fighting continues"),
        _cit("s2", "source.un_news.peace_security",
             title="Something else entirely about a different continent"),
    ])
    assert (ind.sources, ind.independent, ind.folded) == (2, 1, 1)
    assert ind.single_source


def test_the_map_relaxes_the_bar_and_never_folds_on_its_own() -> None:
    """A declared relationship lowers the content bar; it is never the reason.

    The SAME two headlines, one pair declared and one not: only the declared
    pair clears the trimmed-headline distance, and neither folds without the
    content agreeing at all.
    """
    # An eight-character trim off a 65-character normalized headline: 0.123,
    # inside the declared bar (0.15) and outside the undeclared one (0.0).
    trimmed = "Antonelli takes sensational home Italian GP win from back"
    declared = si.independence_of([
        _cit("s1", "source.aawsat.english", title=REUTERS_HEADLINE_TITLE_CASE),
        _cit("s2", "source.cna.all", title=trimmed),
    ])
    assert declared.independent == 1 and si.shared_wire(
        "source.aawsat.english", "source.cna.all"
    )
    undeclared = si.independence_of([
        _cit("s1", "source.bbc.world", title=REUTERS_HEADLINE_TITLE_CASE),
        _cit("s2", "source.dw.world", title=trimmed),
    ])
    assert undeclared.independent == 2
    assert not si.shared_wire("source.bbc.world", "source.dw.world")
    # And a declared pair with NOTHING in common still counts two.
    assert si.independence_of([
        _cit("s1", "source.aawsat.english", title=REUTERS_HEADLINE_TITLE_CASE,
             body=LEDE_A),
        _cit("s2", "source.cna.all",
             title="Rail strike halts freight across the southern corridor",
             body=OTHER_LEDE),
    ]).independent == 2


def test_the_fold_is_transitive_across_three_mastheads() -> None:
    ind = si.independence_of([
        _cit("s1", "source.aawsat.english", title=REUTERS_HEADLINE_TITLE_CASE),
        _cit("s2", "source.cna.all", title=REUTERS_HEADLINE_SENTENCE_CASE),
        _cit("s3", "source.dawn.home", title=REUTERS_HEADLINE_SENTENCE_CASE),
    ])
    assert (ind.sources, ind.independent, ind.folded) == (3, 1, 2)


# ---------------------------------------------------------------------------
# THE CITATION — ``single_source`` / ``wire_folded`` on the quoted claim
# ---------------------------------------------------------------------------


def test_the_citation_carries_the_mark_for_a_single_sourced_head() -> None:
    row = _head("h1", [
        _cit("s1", "source.bbc.world"), _cit("s2", "source.bbc.world"),
    ])
    citation = cc._build_composition_citation(1, row)
    assert citation["single_source"] is True
    assert "wire_folded" not in citation


def test_the_citation_is_byte_identical_when_two_sources_stand_behind_it() -> None:
    """Byte-identity where it matters: nothing marked, nothing added."""
    row = _head("h1", [
        _cit("s1", "source.bbc.world", title="Flooding displaces thousands in "
             "northern Japan", body=OTHER_LEDE),
        _cit("s2", "source.dw.world", title=REUTERS_HEADLINE_TITLE_CASE,
             body=LEDE_A),
    ])
    citation = cc._build_composition_citation(1, row)
    assert "single_source" not in citation and "wire_folded" not in citation
    assert list(citation) == [
        "marker", "ordinal", "ref_id", "ref_kind", "source", "target_id",
        "title", "produced_at", "evidence_text", "effective_confidence",
        "derived_from",
    ]


def test_the_citation_records_the_fold_and_the_mark_together() -> None:
    row = _head("h1", [
        _cit("s1", "source.aawsat.english", title=REUTERS_HEADLINE_TITLE_CASE),
        _cit("s2", "source.cna.all", title=REUTERS_HEADLINE_SENTENCE_CASE),
    ])
    citation = cc._build_composition_citation(1, row)
    assert citation["single_source"] is True
    assert citation["wire_folded"] is True


def test_a_composition_origin_is_unmarked_rather_than_falsely_cleared() -> None:
    row = _head("h1", [
        {"marker": "[[ref:1]]", "ref_id": "x", "ref_kind": "finding",
         "evidence_text": LEDE_A},
    ])
    citation = cc._build_composition_citation(1, row)
    assert "single_source" not in citation and "wire_folded" not in citation


# ---------------------------------------------------------------------------
# THE RECEIPT — ``claims_single_source`` / ``claims_wire_folded``
# ---------------------------------------------------------------------------


class _Finding:
    """The two attributes the CITE phase touches."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.data: dict = {}


@pytest.mark.asyncio
async def test_the_counters_ride_the_cite_receipt() -> None:
    sliced = [
        _head("h1", [
            _cit("s1", "source.bbc.world"), _cit("s2", "source.bbc.world"),
        ]),
        _head("h2", [
            _cit("s3", "source.aawsat.english",
                 title=REUTERS_HEADLINE_TITLE_CASE),
            _cit("s4", "source.cna.all",
                 title=REUTERS_HEADLINE_SENTENCE_CASE),
        ]),
        _head("h3", [
            _cit("s5", "source.bbc.world", title="Flooding displaces thousands "
                 "in northern Japan", body=OTHER_LEDE),
            _cit("s6", "source.dw.world", title=REUTERS_HEADLINE_TITLE_CASE,
                 body=LEDE_A),
        ]),
    ]
    finding = _Finding("A [[ref:1]] B [[ref:2]] C [[ref:3]]")
    steps: list[dict] = []
    citations, ords = await cc.resolve_composition_citations(
        finding=finding,
        steps=steps,
        sliced=sliced,
        periphery_sel=[],
        prior_row=None,
        ledger_row=None,
        ledger_entries=[],
        register_row=None,
        register_situations=[],
        rollup_payload=None,
        render_situation_register_lines=lambda situations, n: [],
    )
    assert ords == [1, 2, 3] and len(citations) == 3
    cite = next(s for s in steps if s["phase"] == "cite")
    # h1 (one outlet) and h2 (one dispatch, two mastheads) are single-sourced;
    # h2 alone was folded; h3 rests on two independent outlets.
    assert cite["claims_single_source"] == 2
    assert cite["claims_wire_folded"] == 1


@pytest.mark.asyncio
async def test_the_receipt_publishes_a_checked_zero() -> None:
    sliced = [_head("h1", [
        _cit("s1", "source.bbc.world", title="Flooding displaces thousands in "
             "northern Japan", body=OTHER_LEDE),
        _cit("s2", "source.dw.world", title=REUTERS_HEADLINE_TITLE_CASE,
             body=LEDE_A),
    ])]
    steps: list[dict] = []
    await cc.resolve_composition_citations(
        finding=_Finding("A [[ref:1]]"),
        steps=steps,
        sliced=sliced,
        periphery_sel=[],
        prior_row=None,
        ledger_row=None,
        ledger_entries=[],
        register_row=None,
        register_situations=[],
        rollup_payload=None,
        render_situation_register_lines=lambda situations, n: [],
    )
    cite = next(s for s in steps if s["phase"] == "cite")
    assert cite["claims_single_source"] == 0 and cite["claims_wire_folded"] == 0


# ---------------------------------------------------------------------------
# THE RECORD — the mark beside the ordinal, and the byte-identity floor
# ---------------------------------------------------------------------------


def _block(corroboration: dict, *, source: str = "cited_signals") -> dict:
    return {
        "ordinal": 3,
        "desk": "escalation",
        "target_name": "Iran",
        "target_id": "country_watch_ir",
        "produced_at": "2026-09-03T10:01:00+00:00",
        "severity": "high",
        "verify": {"overall_score": 0.90},
        "salience": {"cited_mass": 0.44, "source": source},
        "corroboration": corroboration,
    }


HISTORICAL_LINE = (
    "[[ref:3]] · escalation · Iran (country_watch_ir) · 2026-09-03T10:01:00 · "
    "severity high · verify 0.90 · cited mass 0.44 · 2 sources"
)


def test_an_unmarked_block_renders_the_line_it_always_rendered() -> None:
    """THE BYTE-IDENTITY FLOOR. Nothing marked ⇒ nothing changed."""
    line = ar._attribution(_block({"n_sources": 2, "n_desks_sharing": 0}))
    assert line == HISTORICAL_LINE


def test_the_mark_renders_beside_the_ordinal() -> None:
    line = ar._attribution(_block(
        {"n_sources": 1, "n_desks_sharing": 0, "single_source": True}
    ))
    assert line.startswith(f"[[ref:3]] {si.SINGLE_SOURCE_MARKER} · escalation")
    assert line.endswith("1 sources")


def test_the_fold_is_stated_and_never_silently_subtracted() -> None:
    line = ar._attribution(_block({
        "n_sources": 3, "n_desks_sharing": 0,
        "n_independent_sources": 1, "wire_folded": 2, "single_source": True,
    }))
    # The outlet count does not move; the explanation arrives beside it.
    assert "3 sources · 2 wire-folded" in line
    assert si.SINGLE_SOURCE_MARKER in line


def test_a_composition_origin_gets_no_mark_and_no_sources_tail() -> None:
    line = ar._attribution(
        _block({"n_sources": 0, "n_desks_sharing": 0}, source="cited_findings")
    )
    assert si.SINGLE_SOURCE_MARKER not in line and "sources" not in line


def test_both_marks_are_members_of_the_closed_connective_vocabulary() -> None:
    assert si.SINGLE_SOURCE_MARKER in ar.CONNECTIVES
    assert si.WIRE_FOLDED_LABEL in ar.CONNECTIVES
    for phrase in (si.SINGLE_SOURCE_MARKER, si.WIRE_FOLDED_LABEL):
        low = phrase.lower()
        for verb in (" rises", " rising", " escalat", " threat", " risk is",
                     " worsen", " improv", " likely", " suggests"):
            assert verb not in low


# ---------------------------------------------------------------------------
# THE PAYLOAD — the assembler stamps what the render reads
# ---------------------------------------------------------------------------


def _assembly(rows: list[dict]) -> dict:
    ap.attach_cited_salience(rows, {f"s{i}": 0.9 - i / 100 for i in range(1, 9)})
    ordered = sorted(rows, key=ap.order_key)
    return ap.build_assembly(
        tier=ap.TIER_COUNTRY,
        as_of="2026-09-03T12:00:00+00:00",
        candidates=ordered,
        carried=ordered[: ap.BLOCK_CAP],
    )


def test_the_corroboration_block_keeps_its_historical_shape_unmarked() -> None:
    payload = _assembly([_head("h1", [
        _cit("s1", "source.bbc.world", title="Flooding displaces thousands in "
             "northern Japan", body=OTHER_LEDE),
        _cit("s2", "source.dw.world", title=REUTERS_HEADLINE_TITLE_CASE,
             body=LEDE_A),
    ])])
    corr = payload["blocks"][0]["corroboration"]
    assert list(corr) == ["n_sources", "n_desks_sharing"]
    assert corr["n_sources"] == 2
    assert si.SINGLE_SOURCE_MARKER not in ar.render_assembly_body(payload)
    assert si.WIRE_FOLDED_LABEL not in ar.render_assembly_body(payload)


def test_the_assembled_record_marks_a_single_sourced_block() -> None:
    payload = _assembly([_head("h1", [
        _cit("s1", "source.bbc.world"), _cit("s2", "source.bbc.world"),
    ])])
    corr = payload["blocks"][0]["corroboration"]
    assert corr["n_sources"] == 1 and corr["single_source"] is True
    assert "n_independent_sources" not in corr  # nothing folded
    body = ar.render_assembly_body(payload)
    assert f"[[ref:1]] {si.SINGLE_SOURCE_MARKER} · escalation" in body


def test_the_assembled_record_states_the_wire_fold() -> None:
    payload = _assembly([_head("h1", [
        _cit("s1", "source.aawsat.english", title=REUTERS_HEADLINE_TITLE_CASE),
        _cit("s2", "source.cna.all", title=REUTERS_HEADLINE_SENTENCE_CASE),
    ])])
    corr = payload["blocks"][0]["corroboration"]
    assert corr == {
        "n_sources": 2,
        "n_desks_sharing": 0,
        "n_independent_sources": 1,
        "wire_folded": 1,
        "single_source": True,
    }
    body = ar.render_assembly_body(payload)
    assert "2 sources · 1 wire-folded" in body
    assert si.SINGLE_SOURCE_MARKER in body


# ---------------------------------------------------------------------------
# THE DESCRIPTOR — it validates, and it is the map that runs
# ---------------------------------------------------------------------------


def test_the_wire_map_descriptor_validates() -> None:
    body = yaml.safe_load(WIRE_MAP_PATH.read_text(encoding="utf-8"))
    assert body["identity"]["id"] == "source.wire_map"
    assert body["identity"]["kind"] == "static_map"
    # It declares no lifecycle: nothing registers it, so a `state` would be a
    # claim about a registry row that does not exist.
    assert "state" not in body["identity"]
    wire_map = body["wire_map"]
    assert wire_map["version"] == si.WIRE_MAP_VERSION
    assert set(wire_map) == {"version", "syndication", "same_publisher"}
    for sid, wires in wire_map["syndication"].items():
        assert sid.startswith("source."), sid
        assert wires and len(set(wires)) == len(wires), sid
    seen: set[str] = set()
    for publisher, members in wire_map["same_publisher"].items():
        assert len(members) >= 2, publisher
        for sid in members:
            assert sid.startswith("source."), sid
            assert sid not in seen, f"{sid} is in two publisher groups"
            seen.add(sid)


def test_the_descriptor_is_the_map_that_runs() -> None:
    """The pin. ``descriptors/`` is in neither container image, so what RUNS is
    the module; editing one side without the other turns this red."""
    wire_map = yaml.safe_load(WIRE_MAP_PATH.read_text(encoding="utf-8"))["wire_map"]
    assert {k: tuple(v) for k, v in wire_map["syndication"].items()} == dict(
        si.SYNDICATION
    )
    assert {k: tuple(v) for k, v in wire_map["same_publisher"].items()} == dict(
        si.SAME_PUBLISHER
    )
    assert si.WIRE_MAP_DESCRIPTOR == str(
        WIRE_MAP_PATH.relative_to(REPO_ROOT)
    ).replace("\\", "/")


def test_every_declared_outlet_is_a_source_id_the_tree_knows() -> None:
    """A row naming an outlet that does not exist folds nothing and misleads
    whoever reads the map next."""
    known = {
        yaml.safe_load(p.read_text(encoding="utf-8"))["identity"]["id"]
        for p in (REPO_ROOT / "descriptors").glob("source_*.yaml")
    }
    catalog = (
        REPO_ROOT / "scripts" / "bringup_register_source_catalog.py"
    ).read_text(encoding="utf-8")
    declared = set(si.SYNDICATION) | {
        sid for members in si.SAME_PUBLISHER.values() for sid in members
    }
    missing = sorted(s for s in declared if s not in known and f'"{s}"' not in catalog)
    assert not missing, f"wire map names unknown source ids: {missing}"


# ---------------------------------------------------------------------------
# ONE IMPLEMENTATION — the pins that stop a second opinion appearing
# ---------------------------------------------------------------------------


def test_there_is_one_near_duplicate_distance() -> None:
    assert dd._normalized_levenshtein is _url_canon.normalized_levenshtein
    assert _url_canon.normalized_levenshtein("hello world", "hello world") == 0.0
    assert _url_canon.normalized_levenshtein("hello", "") == 1.0


def test_there_is_one_cited_signal_reader() -> None:
    assert ap._head_citations is si.head_citations


def test_the_cited_signal_reader_is_total() -> None:
    for bad in (None, {}, {"data": None}, {"data": {}}, {"data": {"data": 3}},
                {"data": {"data": {"citations": "x"}}}):
        assert si.head_citations(bad) == []
    assert si.head_citations(
        {"data": {"data": {"citations": [{"signal_id": "s1"}, 7]}}}
    ) == [{"signal_id": "s1"}]


def test_the_token_screen_keeps_a_wide_slice_off_the_quadratic_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost control, measured rather than asserted about.

    Forty unrelated cited items, every normalized lede truncated to the SAME
    160 characters — so the exact length bound prunes nothing and, without the
    token screen, all 780 pairs would each take a 160x160 walk. The screen has
    to keep the DP count small, and it must not change the answer.
    """
    calls = {"n": 0}
    real = si.normalized_levenshtein

    def _counting(a: str, b: str) -> float:
        calls["n"] += 1
        return real(a, b)

    monkeypatch.setattr(si, "normalized_levenshtein", _counting)
    citations = [
        _cit(
            f"s{i}",
            f"source.outlet{i:02d}.news",
            # Disjoint vocabularies, identical lengths: nothing in common for
            # the screen to find and nothing for the length bound to prune.
            title=" ".join(f"h{i:02d}w{j:02d}" for j in range(9)),
            body=" ".join(f"b{i:02d}w{j:03d}" for j in range(30)),
        )
        for i in range(40)
    ]
    ind = si.independence_of(citations)
    assert (ind.sources, ind.independent, ind.folded) == (40, 40, 0)
    assert calls["n"] < 100, f"{calls['n']} distance walks over 780 pairs"


def test_the_length_prefilter_does_not_change_any_answer() -> None:
    """The prefilter is an exact bound, so it may only ever save work."""
    pairs = [
        ("", ""), ("a", ""), ("abc", "abd"), ("hello world", "hello worlds"),
        (LEDE_A[:160], LEDE_B[:160]), (LEDE_A[:160], OTHER_LEDE[:160]),
    ]
    for bar in (0.0, 0.1, 0.25, 0.5):
        for a, b in pairs:
            expected = (
                a == b
                or (bool(a) and bool(b)
                    and _url_canon.normalized_levenshtein(a, b) <= bar)
                or (not a and not b)
            )
            assert si._within(a, b, bar) is bool(expected), (a[:20], b[:20], bar)
