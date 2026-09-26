# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D-3 — THE ASSEMBLY ARMS: four deterministic auditors over ``assembly.v1``.

EVERY FIXTURE BELOW IS BUILT FROM REAL ROWS. The three origin bodies are
verbatim excerpts of live desk heads read out of ``analyst_outputs`` on
2026-09-03 (ids in the constants), truncated only in length. That matters for
three separate reasons and none of them is decoration:

  * they carry the TYPOGRAPHY the wire actually writes — U+2019 in
    ``desk’s collection``, U+2011 in ``energy‑security``, U+202F between a
    numeral and its month — which is exactly the MECH-6 class the shared fold
    exists for. A synthetic ASCII fixture would pass an arm that is broken;
  * one of them is the JP MECHANISM VERBATIM (*"No coordinated narrative
    appears in Myanmar coverage in this desk's collection through
    3 September"*), so ARM 2 is tested against the sentence the class was named
    for rather than a paraphrase of it;
  * the CLEAN fixture is built by CONSTRUCTION — offsets, sha and length are
    computed from the body — so every violation fixture below is exactly ONE
    mutation away from a passing read. That is what makes "this arm fires on
    this defect and nowhere else" a checkable claim instead of a hope.

THE ARM-FIRE MATRIX is the point of the middle section: one fixture per
violation class, each asserting BOTH halves — the arm fires on its own defect,
and the OTHER THREE ARMS STAY SILENT. An arm that fires on everything catches
nothing.

BOTH ERROR DIRECTIONS, for ARM 2 especially. That class (``M-8`` /
``absence_scope_laundered``) has false-positived three times in this plane's
history, so the replay covers the way it FAILS as carefully as the way it
fires: a span that keeps its qualifier passes, a sentence with no collection
bound never fires, a time bound ("in the latest 72-hour slice") is NOT scope,
and a payload declaring a scope its source sentence lacks fires the OTHER class.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from typing import Any
from uuid import uuid4

import pytest

import legba.data.provenance.assembly_arms as AA
import legba.data.provenance.composition_integrity as ci
import legba.data.provenance.verify as V
from legba.data.provenance.text_fold import (
    PUNCT_FOLD,
    fold_contains,
    fold_equal,
    normalize_for_match,
)
from legba.data.provenance.verify import (
    build_faithfulness_critique_payload,
    verify_finding_faithfulness,
)

# ---------------------------------------------------------------------------
# REAL ORIGIN BODIES (verbatim excerpts, 2026-09-03)
# ---------------------------------------------------------------------------

#: ``narrative_coordination`` / ``country_watch_mm``, head bb3fb6d3. THE JP
#: MECHANISM'S OWN SHAPE: a collection-scoped negative, in a desk's BLUF.
BODY_MM = (
    "*As of 2026-09-03; slice covers the trailing 72h to that date; 41 signals.*\n\n"
    "**BLUF:** No coordinated narrative appears in Myanmar coverage in this "
    "desk’s collection through 3 September.\n\n"
    "## What changed\n\n"
    "- No material change; the assessment that no coordinated narrative is "
    "present remains consistent with the prior read [42].\n"
)

#: ``energy_security`` / ``country_watch_cd``, head 1a6d0cc8. Carries U+2011
#: inside ``energy‑security`` and U+202F inside ``2 September 2026``.
BODY_CD_ENERGY = (
    "*As of 2026-09-02; slice covers the trailing 72h to that date; 12 signals.*\n\n"
    "**BLUF:** No new energy‑security incidents are reported in this "
    "desk’s collection through 2 September 2026, and the pressure "
    "remains at a moderate level.\n\n"
    "## What changed\n\n"
    "No material change; the 12 signals reviewed contain no reports of outages, "
    "fuel price spikes, or infrastructure attacks, so the moderate pressure "
    "rating from the prior read persists [13].\n"
)

#: ``military_posture`` / ``country_watch_cd``, head 8d1113f3. Its BLUF carries
#: NO collection bound — it is a positive claim about the world — which is what
#: makes it the honest control for ARM 2.
BODY_CD_MIL = (
    "*As of 2026-09-02; slice covers the trailing 72h to that date; 13 signals.*\n\n"
    "**BLUF:** DR Congo's standing military posture shows no material change "
    "this window, with the UN peace‑monitoring deployment persisting "
    "unchanged [14][15].\n\n"
    "## What changed\n\n"
    "- The prior read confirmed the posture was unchanged and the UN "
    "peace‑monitoring deployment persisted [14].\n"
    "- No new exercise activity, major arms acquisition, or readiness‑level "
    "change appears in this desk's collection of signals [1][2][3].\n"
)

HEAD_MM = "bb3fb6d3-6cf4-4216-9c73-d1d3bf31097d"
HEAD_CD_ENERGY = "1a6d0cc8-b1d1-4e70-a24c-cb32764743b8"
HEAD_CD_MIL = "8d1113f3-45ed-40cf-b773-ebfde99026fb"

_ORIGINS: dict[str, dict[str, str]] = {
    HEAD_MM: {
        "body": BODY_MM,
        "desk": "narrative_coordination",
        "target_id": "country_watch_mm",
        "produced_at": "2026-09-03T09:14:00Z",
    },
    HEAD_CD_ENERGY: {
        "body": BODY_CD_ENERGY,
        "desk": "energy_security",
        "target_id": "country_watch_cd",
        "produced_at": "2026-09-02T22:41:00Z",
    },
    HEAD_CD_MIL: {
        "body": BODY_CD_MIL,
        "desk": "military_posture",
        "target_id": "country_watch_cd",
        "produced_at": "2026-09-02T21:03:00Z",
    },
}


# ---------------------------------------------------------------------------
# THE FIXTURE BUILDER — a CLEAN payload by construction
# ---------------------------------------------------------------------------


def _span(head_id: str, needle: str, *, role: str = "bluf", scope_tokens=None):
    """A span cut from a REAL body at REAL byte offsets.

    The offsets, the sha and the length are all COMPUTED, never written by hand,
    so a clean fixture cannot drift into a passing-by-accident one and every
    violation below is exactly one mutation from here.
    """
    body = _ORIGINS[head_id]["body"]
    raw = body.encode("utf-8")
    c_at = body.index(needle)
    start = len(body[:c_at].encode("utf-8"))
    end = start + len(needle.encode("utf-8"))
    assert raw[start:end].decode("utf-8") == needle
    return {
        "role": role,
        "text": needle,
        "origin": {
            "head_id": head_id,
            "start": start,
            "end": end,
            "body_sha256": hashlib.sha256(raw).hexdigest(),
            "body_len": len(raw),
        },
        "markers": re.findall(r"\[\d+\]", needle),
        "scope_tokens": list(scope_tokens or []),
    }


#: The three spans the clean read carries — one per block, each a real sentence.
SPAN_MM = (
    "No coordinated narrative appears in Myanmar coverage in this "
    "desk’s collection through 3 September."
)
SPAN_CD_ENERGY = (
    "No new energy‑security incidents are reported in this desk’s "
    "collection through 2 September 2026, and the pressure remains at "
    "a moderate level."
)
SPAN_CD_MIL = (
    "DR Congo's standing military posture shows no material change this window, "
    "with the UN peace‑monitoring deployment persisting unchanged [14][15]."
)

_ROSTER = (
    "narrative_coordination",
    "energy_security",
    "military_posture",
)

#: THE TOKEN THE ASSEMBLER ACTUALLY WRITES, taken from the arm module rather
#: than spelled here, and this line is the fix for the defect the first live
#: cycle found. D-3's fixtures declared ``scope_tokens=["this desk"]`` — a
#: PHRASE — and D-2 writes ``["collection_denominator"]``, an IDENTIFIER
#: (``assembly_spans.py:286``, pinned by ``test_composition_assembly_d2.py:189``).
#: Each side passed its own tests and the pair was wrong: SCOPE_WIDENED tested
#: the token with a literal substring match, which is true of a phrase copied
#: out of the sentence and false of every identifier, so the arm fired on all 26
#: collection-scoped spans in the fleet's first assembled cycle. The fixture now
#: carries the live shape, so a future drift of the same kind fails here.
_SCOPE_MARK = AA.SCOPE_TOKEN_COLLECTION_DENOMINATOR


def _block(ordinal: int, head_id: str, span: dict[str, Any]) -> dict[str, Any]:
    o = _ORIGINS[head_id]
    return {
        "ordinal": ordinal,
        "finding_id": head_id,
        "desk": o["desk"],
        "target_id": o["target_id"],
        "produced_at": o["produced_at"],
        "tier": "basis",
        "severity": "elevated",
        "spans": [span],
    }


def clean_assembly() -> dict[str, Any]:
    """The CLEAN ``assembly.v1`` read. Every arm must be silent on it."""
    blocks = [
        _block(
            1,
            HEAD_MM,
            _span(HEAD_MM, SPAN_MM, scope_tokens=[_SCOPE_MARK]),
        ),
        _block(
            2,
            HEAD_CD_ENERGY,
            _span(HEAD_CD_ENERGY, SPAN_CD_ENERGY, scope_tokens=[_SCOPE_MARK]),
        ),
        _block(3, HEAD_CD_MIL, _span(HEAD_CD_MIL, SPAN_CD_MIL)),
    ]
    return {
        "schema": AA.ASSEMBLY_SCHEMA,
        "regime": "assembly",
        "tier": "country",
        "as_of": "2026-09-03T12:00:00Z",
        "blocks": blocks,
        "coverage_roster": list(_ROSTER),
        "coverage": [
            {"unit": "narrative_coordination", "status": "in_basis", "age_h": 2.8},
            {"unit": "energy_security", "status": "in_basis", "age_h": 13.3},
            {"unit": "military_posture", "status": "below_floor", "age_h": 14.9},
        ],
        "drops": {
            "shown_not_carried": [
                {
                    "finding_id": str(uuid4()),
                    "desk": "internal_stability",
                    "rank": 4,
                    "why": "shown_not_selected",
                }
            ],
            "not_selected": [
                {"finding_id": str(uuid4()), "rank": 4, "why": "not_selected"},
                {"finding_id": str(uuid4()), "rank": 5, "why": "not_selected"},
            ],
            "trimmed": [],
            "below_floor": [],
            "no_head": [],
            "counts": {
                "shown": 4,
                "carried": 3,
                "shown_not_carried": 1,
                "candidates": 5,
                "not_selected": 2,
                "trimmed": 0,
                "below_floor": 0,
                "no_head": 0,
                "invisible_heads": 228,
            },
            "why_classes": list(AA.DROP_WHY_CLASSES),
        },
    }


def clean_citations() -> list[dict[str, Any]]:
    """The CITATION BRIDGE for the clean read — the real key set a live
    composition citation carries, plus the ``produced_at`` D-2 must add."""
    return [
        {
            "marker": f"[[ref:{i}]]",
            "ordinal": i,
            "ref_id": head,
            "ref_kind": "finding",
            "source": _ORIGINS[head]["desk"],
            "target_id": _ORIGINS[head]["target_id"],
            "produced_at": _ORIGINS[head]["produced_at"],
            "title": f"{_ORIGINS[head]['desk']} read",
            "evidence_text": _ORIGINS[head]["body"],
            "effective_confidence": 0.62,
        }
        for i, head in enumerate((HEAD_MM, HEAD_CD_ENERGY, HEAD_CD_MIL), start=1)
    ]


def _reasons(result: AA.ArmResult) -> list[str]:
    return sorted(f.reason for f in result.findings)


def _audit(assembly, citations=None) -> AA.ArmResult:
    return AA.audit(assembly, citations if citations is not None else clean_citations())


# ---------------------------------------------------------------------------
# 1. THE CLEAN READ — every arm silent, quote fidelity 1.000
# ---------------------------------------------------------------------------


def test_the_clean_assembly_fires_nothing() -> None:
    """THE BASELINE the whole matrix rests on. If this ever goes red, every
    'fires exactly on its violation' assertion below is meaningless."""
    res = _audit(clean_assembly())
    assert res.findings == [], _reasons(res)
    assert res.counters["assembly_arms_reads_audited"] == 1
    assert res.counters["assembly_quote_spans_checked"] == 3
    assert res.counters["assembly_quote_spans_ok"] == 3
    assert res.counters["assembly_attribution_blocks_checked"] == 3
    assert res.counters["assembly_coverage_units_checked"] == 3


def test_quote_fidelity_is_an_invariant_not_a_threshold() -> None:
    """G2: the bar is 1.000 over ALL spans, and it is stated as an invariant so
    nobody reports 0.98 as a pass. The 0.75 analog is the PROSE tier's bar."""
    assert AA.quote_fidelity_score(_audit(clean_assembly())) == 1.0


def test_the_clean_read_publishes_its_drop_numbers() -> None:
    """§3.4(b) COUNTED, NOT GATED — ``drops.counts.*`` are published per read.
    These are the numbers R4 reports and R5 moves, and the thematic tier's
    0.678 drop rate has never been published at all."""
    c = _audit(clean_assembly()).counters
    assert c["assembly_drops_shown"] == 4
    assert c["assembly_drops_carried"] == 3
    assert c["assembly_drops_shown_not_carried"] == 1
    assert c["assembly_drops_not_selected"] == 2
    assert c["assembly_drops_invisible_heads"] == 228


# ---------------------------------------------------------------------------
# 2. THE ARM-FIRE MATRIX — one mutation, one class, three silent arms
# ---------------------------------------------------------------------------

_ARM_PREFIX = {
    "quote": "ARM 1",
    "scope": "ARM 2",
    "attribution": "ARM 3",
    "coverage": "ARM 4a",
    "drop": "ARM 4b",
}


def _arm_of(reason: str) -> str:
    for prefix, arm in _ARM_PREFIX.items():
        if reason.startswith(prefix):
            return arm
    raise AssertionError(f"unclassified reason {reason!r}")


def _assert_fires_only(result: AA.ArmResult, reason: str) -> None:
    """The BOTH-HALVES assertion: the expected class fired, and nothing from any
    other arm did. An arm that fires on everything catches nothing."""
    got = _reasons(result)
    assert reason in got, got
    strays = [r for r in got if _arm_of(r) != _arm_of(reason)]
    assert not strays, f"{reason} leaked into other arms: {strays}"


# -- ARM 1 ------------------------------------------------------------------


def test_arm1_quote_not_contained() -> None:
    """The assembler claimed a quotation and the origin body has none. This is
    the class ``attribution_ungrounded_quote`` was written for and never got a
    population to fire on."""
    a = clean_assembly()
    a["blocks"][0]["spans"][0]["text"] = (
        "A coordinated anti-Myanmar narrative is being driven from three "
        "state-aligned outlets."
    )
    res = _audit(a)
    _assert_fires_only(res, AA.QUOTE_NOT_CONTAINED)
    assert AA.quote_fidelity_score(res) == pytest.approx(2 / 3)


def test_arm1_quote_offset_mismatch() -> None:
    """The quote is REAL and its provenance is wrong — the worse defect of the
    two, because spans exist to point at bytes."""
    a = clean_assembly()
    a["blocks"][1]["spans"][0]["origin"]["start"] += 40
    a["blocks"][1]["spans"][0]["origin"]["end"] += 40
    res = _audit(a)
    _assert_fires_only(res, AA.QUOTE_OFFSET_MISMATCH)


def test_arm1_quote_origin_drift_on_sha() -> None:
    """The body this span was cut from is not the body on the row."""
    a = clean_assembly()
    a["blocks"][2]["spans"][0]["origin"]["body_sha256"] = "0" * 64
    _assert_fires_only(_audit(a), AA.QUOTE_ORIGIN_DRIFT)


def test_arm1_quote_origin_drift_on_depth() -> None:
    """DEPTH-1 IS THE CONTRACT (D-1 §0.3: "never a path"). A span pointing
    through some other head is an origin drift even when its bytes resolve."""
    a = clean_assembly()
    a["blocks"][0]["spans"][0]["origin"]["head_id"] = HEAD_CD_MIL
    _assert_fires_only(_audit(a), AA.QUOTE_ORIGIN_DRIFT)


def test_arm1_quote_origin_truncated_is_p0c_made_visible() -> None:
    """P0c: ``evidence_text`` is captured at 3600 chars while the renderer shows
    4000, so a span from that gap would be a FALSE hard reject. It is not
    silently mis-verified — it is a NAMED, COUNTED class, and the sha check over
    an incomplete capture is DECLINED rather than passed."""
    a = clean_assembly()
    cits = clean_citations()
    full = _ORIGINS[HEAD_CD_MIL]["body"]
    # Capture stops 20 chars into the span the block quotes — exactly the P0c
    # shape, where the renderer showed bytes the citation bridge never kept.
    cut = full[: full.index(SPAN_CD_MIL) + 20]
    for c in cits:
        if c["ref_id"] == HEAD_CD_MIL:
            c["evidence_text"] = cut
    res = AA.audit(a, cits)
    _assert_fires_only(res, AA.QUOTE_ORIGIN_TRUNCATED)
    # The sha over an INCOMPLETE capture is declined, never passed and never
    # convicted on — undecided is its own state and it is counted.
    assert res.counters["assembly_quote_origin_truncated"] == 1


def test_arm1_declines_a_span_whose_origin_is_unresolvable() -> None:
    """DECLINED AND COUNTED, never charged twice: ARM 3 owns the unresolvable
    head, so ARM 1 costs the read nothing for the same defect."""
    a = clean_assembly()
    cits = [c for c in clean_citations() if c["ref_id"] != HEAD_MM]
    res = AA.audit(a, cits)
    assert _reasons(res) == [AA.ATTRIBUTION_HEAD_UNRESOLVED]
    assert res.counters["assembly_quote_origin_unresolved"] == 1


# -- ARM 2, BOTH DIRECTIONS -------------------------------------------------


def test_arm2_scope_truncated_on_the_jp_mechanism() -> None:
    """THE CLASS, on the sentence it was named for. The desk wrote a
    COLLECTION-scoped negative; the span cuts the qualifier away and leaves a
    claim about Myanmar coverage full stop."""
    a = clean_assembly()
    span = a["blocks"][0]["spans"][0]
    body = _ORIGINS[HEAD_MM]["body"]
    short = "No coordinated narrative appears in Myanmar coverage"
    c_at = body.index(short)
    span["text"] = short
    span["origin"]["start"] = len(body[:c_at].encode("utf-8"))
    span["origin"]["end"] = span["origin"]["start"] + len(short.encode("utf-8"))
    span["scope_tokens"] = []
    _assert_fires_only(_audit(a), AA.SCOPE_TRUNCATED)


def test_arm2_scope_widened_is_the_other_direction() -> None:
    """The payload DECLARES a bound its own source sentence never had. A reader
    trusting ``scope_tokens`` would read a scoped negative that is not one.

    Block 3 is the military-posture BLUF: a positive claim about the world whose
    sentence carries NO collection bound (``has_collection_denominator_scope`` is
    False on it — asserted below so this fixture cannot rot into a passing one).
    Declaring the assembler's own token over it is the widening."""
    a = clean_assembly()
    a["blocks"][2]["spans"][0]["scope_tokens"] = [_SCOPE_MARK]
    sentence = AA.source_sentence(
        BODY_CD_MIL,
        a["blocks"][2]["spans"][0]["origin"]["start"],
        a["blocks"][2]["spans"][0]["origin"]["end"],
    )
    assert not ci.has_collection_denominator_scope(sentence)
    _assert_fires_only(_audit(a), AA.SCOPE_WIDENED)


def test_arm2_the_declared_token_is_verified_by_the_predicate_not_a_substring(
) -> None:
    """THE LIVE REGRESSION (2026-09-05), and it is the whole reason this train
    exists.

    ``scope_tokens`` is an IDENTIFIER list. The shipped SCOPE_WIDENED tested it
    with ``fold_contains(sentence, tok)`` — a literal substring match — so it
    looked for the word ``collection_denominator`` inside desk prose, where it
    never appears, and fired on EVERY collection-scoped span: 26 ERROR lines and
    an assembly branch of ~0.9 across half the fleet's first assembled cycle, on
    reads whose quote fidelity was 1.000.

    The live sentence, verbatim from ``narrative_coordination`` — a scoped
    negative carrying the assembler's own token — must cost NOTHING."""
    a = clean_assembly()
    span = a["blocks"][0]["spans"][0]
    assert span["scope_tokens"] == [_SCOPE_MARK], "the live payload shape"
    sentence = AA.source_sentence(
        BODY_MM, span["origin"]["start"], span["origin"]["end"]
    )
    assert ci.has_collection_denominator_scope(sentence)
    assert _SCOPE_MARK not in sentence, (
        "the identifier is NOT in the prose — which is exactly why a substring "
        "test could only ever fire"
    )
    res = _audit(a)
    assert not [f for f in res.findings if f.reason == AA.SCOPE_WIDENED]
    assert "assembly_scope_widened" not in res.counters


def test_arm2_an_unknown_scope_token_is_counted_and_never_charged() -> None:
    """A token this arm does not know is the ARM's ignorance, not the payload's
    defect. Inventing a violation out of it is how the class false-positived a
    fourth time, so it gets a counter and no finding — the same
    ``assembly_drops_why_absent`` posture ARM 4(b) takes for the same reason."""
    a = clean_assembly()
    a["blocks"][2]["spans"][0]["scope_tokens"] = ["in this desk's collection"]
    res = _audit(a)
    assert res.counters["assembly_scope_token_unknown"] == 1
    assert not res.findings, "an unrecognised identifier charges nothing"


def test_arm2_a_span_that_keeps_its_qualifier_passes() -> None:
    """THE FALSE-POSITIVE DIRECTION, and this class has false-positived three
    times. The clean read's first two spans are collection-scoped and carry the
    bound; neither may cost anything."""
    res = _audit(clean_assembly())
    assert res.counters["assembly_scope_bounded_sentences"] >= 2
    assert not [f for f in res.findings if f.reason.startswith("scope")]


def test_arm2_a_time_bound_is_not_a_collection_bound() -> None:
    """The predicate is ``has_collection_denominator_scope``, reused VERBATIM —
    "in the latest 72-hour slice" says WHEN a desk looked, not WHAT it searched,
    so it neither creates a violation nor cures one. Asserted against the shared
    predicate directly so this test fails if the reuse ever becomes a rewrite."""
    assert ci.has_collection_denominator_scope("in this desk’s collection")
    assert not ci.has_collection_denominator_scope("in the latest 72-hour slice")
    assert not ci.has_collection_denominator_scope("in this window")


def test_arm2_never_fires_on_an_unbounded_source_sentence() -> None:
    """The military-posture BLUF is a positive claim about the world with no
    collection bound anywhere in its sentence. ARM 2 has nothing to preserve and
    must say nothing."""
    a = clean_assembly()
    a["blocks"] = [a["blocks"][2]]
    a["drops"]["counts"].update({"shown": 2, "carried": 1})
    res = _audit(a)
    assert not [f for f in res.findings if f.reason.startswith("scope")]


# -- ARM 3 ------------------------------------------------------------------


def test_arm3_desk_mismatch_kills_the_r3_specimen() -> None:
    """R3 §4.2: an assessment attributed to a "proliferation-watch desk that
    does not exist among the seven unit heads". Under the assembly the desk name
    is GENERATED, so this arm is a regression test on the generator."""
    a = clean_assembly()
    a["blocks"][0]["desk"] = "proliferation_watch"
    _assert_fires_only(_audit(a), AA.ATTRIBUTION_DESK_MISMATCH)


def test_arm3_target_mismatch() -> None:
    a = clean_assembly()
    a["blocks"][1]["target_id"] = "country_watch_mm"
    _assert_fires_only(_audit(a), AA.ATTRIBUTION_TARGET_MISMATCH)


def test_arm3_date_mismatch() -> None:
    a = clean_assembly()
    a["blocks"][2]["produced_at"] = "2026-08-30T21:03:00Z"
    _assert_fires_only(_audit(a), AA.ATTRIBUTION_DATE_MISMATCH)


def test_arm3_date_is_declined_when_the_bridge_does_not_carry_one() -> None:
    """TODAY'S REAL SHAPE. The live composition citation carries ``marker ·
    ordinal · ref_id · ref_kind · source · target_id · title · evidence_text ·
    effective_confidence · derived_from`` and NO timestamp, so this arm cannot
    decide the date until D-2 adds it. UNDECIDED IS NOT PASSED — it is counted,
    and the counter is what makes the gap visible instead of silent."""
    cits = clean_citations()
    for c in cits:
        c.pop("produced_at")
    a = clean_assembly()
    a["blocks"][2]["produced_at"] = "1999-01-01T00:00:00Z"
    res = AA.audit(a, cits)
    assert res.findings == [], _reasons(res)
    assert res.counters["assembly_attribution_date_unverifiable"] == 3


def test_arm3_timestamp_forms_that_mean_the_same_instant_agree() -> None:
    """The two capture paths write ISO differently (``T`` vs space, with and
    without sub-second precision). An equality test that convicted on that would
    be a false hard fail on every block in the fleet."""
    a = clean_assembly()
    cits = clean_citations()
    for c in cits:
        if c["ref_id"] == HEAD_CD_MIL:
            c["produced_at"] = "2026-09-02 21:03:00.815+00:00"
    assert AA.audit(a, cits).findings == []


def test_arm3_head_unresolved_is_counted_never_guessed() -> None:
    a = clean_assembly()
    a["blocks"][1]["finding_id"] = str(uuid4())
    res = _audit(a)
    assert AA.ATTRIBUTION_HEAD_UNRESOLVED in _reasons(res)


# -- ARM 4 ------------------------------------------------------------------


def test_arm4a_coverage_unit_missing_needs_the_declared_roster() -> None:
    """The ROSTER is the only honest denominator: deriving it from the rows that
    ARRIVED makes a missing unit invisible, which is the original defect."""
    a = clean_assembly()
    a["coverage"] = [c for c in a["coverage"] if c["unit"] != "military_posture"]
    _assert_fires_only(_audit(a), AA.COVERAGE_UNIT_MISSING)


def test_arm4a_declines_completeness_without_a_roster() -> None:
    """No roster on the payload ⇒ the arm CANNOT check completeness and refuses
    to reconstruct one. It still checks duplication and status validity, which
    are self-contained, and it counts the decline."""
    a = clean_assembly()
    a.pop("coverage_roster")
    a["coverage"] = [c for c in a["coverage"] if c["unit"] != "military_posture"]
    res = _audit(a)
    assert res.findings == [], _reasons(res)
    assert res.counters["assembly_coverage_roster_absent"] == 1


def test_arm4a_coverage_unit_duplicated() -> None:
    a = clean_assembly()
    a["coverage"].append({"unit": "energy_security", "status": "unverified"})
    _assert_fires_only(_audit(a), AA.COVERAGE_UNIT_DUPLICATED)


def test_arm4a_coverage_status_invalid() -> None:
    a = clean_assembly()
    a["coverage"][1]["status"] = "periphery"
    _assert_fires_only(_audit(a), AA.COVERAGE_STATUS_INVALID)


def test_arm4b_drop_count_mismatch_is_the_identity_not_closing() -> None:
    """``|shown| - |blocks| == |shown_not_carried| + |trimmed|`` (D-1 §1.3):
    what was shown and not carried IS the drop count."""
    a = clean_assembly()
    a["drops"]["counts"]["shown"] = 9
    _assert_fires_only(_audit(a), AA.DROP_COUNT_MISMATCH)


def test_arm4b_one_broken_ledger_costs_one_claim_however_many_identities_fail() -> None:
    """A broken ledger is a SINGLE construction defect. Charging it once per
    failing identity would let one bug cost a read seven claims; the detail
    carries them all, so nothing is hidden by the cap."""
    a = clean_assembly()
    a["drops"]["counts"].update({"shown": 9, "carried": 7, "not_selected": 11})
    res = _audit(a)
    assert _reasons(res) == [AA.DROP_COUNT_MISMATCH]
    assert "counts.carried=7" in (res.findings[0].detail or "")
    assert "counts.not_selected=11" in (res.findings[0].detail or "")


def test_arm4b_drop_why_unknown() -> None:
    """A fabricated reason is worse than a published count — the same doctrine
    that keeps ``invisible_heads`` a number and never a list."""
    a = clean_assembly()
    a["drops"]["not_selected"][0]["why"] = "model_did_not_like_it"
    _assert_fires_only(_audit(a), AA.DROP_WHY_UNKNOWN)


def test_arm4b_a_missing_why_is_counted_not_charged() -> None:
    """THE CONSERVATIVE HALF, and it is deliberate. Three of the five grains
    (`trimmed` / `below_floor` / `no_head`) have their class implied by the grain
    they sit in, and the producer's schema is the authority on which grains stamp
    a ``why`` at all. A HARD arm that fired on every read at merge would be
    withdrawn within a day — so an absent ``why`` is COUNTED, and the counter is
    what keeps the silence measurable rather than silent."""
    a = clean_assembly()
    a["drops"]["not_selected"][0].pop("why")
    a["drops"]["shown_not_carried"][0].pop("why")
    res = _audit(a)
    assert res.findings == [], _reasons(res)
    assert res.counters["assembly_drops_why_absent"] == 2


def test_arm4b_drop_order_violation() -> None:
    """Selection is a STRICT PREFIX of the order by construction, so a
    not-selected candidate ranked at or above the carried blocks cannot happen
    unless the constructor is broken."""
    a = clean_assembly()
    a["drops"]["not_selected"][0]["rank"] = 2
    _assert_fires_only(_audit(a), AA.DROP_ORDER_VIOLATION)


def test_arm4b_a_non_total_order_is_a_violation_too() -> None:
    """The ordering rule is TOTAL — severity, cited_mass, produced_at,
    finding_id — precisely so no run is decided by a five-second ``produced_at``
    gap. Duplicate ranks mean the tiebreak came back."""
    a = clean_assembly()
    a["drops"]["not_selected"][1]["rank"] = 4
    _assert_fires_only(_audit(a), AA.DROP_ORDER_VIOLATION)


# ---------------------------------------------------------------------------
# 3. INERTNESS — the property the whole fleet rests on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        [],
        "not-a-mapping",
        {"regime": "legacy"},
        {"blocks": [{"ordinal": 1}]},
    ],
)
def test_the_arms_are_inert_without_an_assembly_v1_block(payload) -> None:
    """A unit finding and a malformed payload produce NOTHING — no findings, no
    counters."""
    res = AA.audit(payload, clean_citations())
    assert res.findings == [] and res.counters == {}


def test_a_legacy_regime_row_is_declined_silently() -> None:
    """THE FLAG-OFF SHAPE, and it is the one a ``schema``-only gate would get
    wrong. §5.2 requires ``regime`` on EVERY composition row from D-2's merge,
    so a flag-off row carries a REAL ``assembly.v1`` key with nothing behind it.
    Auditing it would put a ``reads_audited`` counter, a ``branch_scores`` entry
    and six ledger check units on every legacy composition in the fleet — noise
    on a read nobody assembled, and a breach of §5.2's other requirement that
    flag-off restores byte-identical behaviour.

    Declined SILENTLY: a counter here would be a receipt for a non-event."""
    res = AA.audit({"schema": AA.ASSEMBLY_SCHEMA, "regime": "legacy"}, clean_citations())
    assert res.findings == [] and res.counters == {}
    # ...and a fully-populated payload flipped back to legacy is inert too, so
    # the gate is the REGIME and not the emptiness.
    legacy = clean_assembly()
    legacy["regime"] = AA.REGIME_LEGACY
    assert AA.audit(legacy, clean_citations()).counters == {}


def test_a_missing_regime_never_switches_the_audit_off() -> None:
    """The other direction, and it is deliberate: only the explicit word
    ``legacy`` declines. A producer that forgets the label must not thereby
    silence its own auditor."""
    a = clean_assembly()
    a.pop("regime")
    assert AA.audit(a, clean_citations()).counters["assembly_arms_reads_audited"] == 1


def test_the_arms_never_read_the_rendered_body() -> None:
    """D-2's renderer defuses child ``[[ref:N]]`` markers (``[[ref:3]]`` →
    ``(child ref 3)``) so a span cut from a lower COMPOSITION tier cannot collide
    with this tier's ordinal space. The canonical text is ``spans[].text`` in the
    PAYLOAD, byte-identical to the origin; the body is a render of it and is
    not. An ARM 1 that byte-matched the body would false-fire on every defused
    marker in the fleet.

    ``fold`` takes no ``body`` argument at all — the ABSENCE of that parameter is
    the guard, and this is the pin that keeps a later edit from adding one."""
    assert "body" not in inspect.signature(AA.fold).parameters
    assert "body" not in inspect.signature(AA.audit).parameters
    # And the property that absence protects: a span carrying a raw child marker
    # matches its origin, whatever the render would have done to it.
    body = "**BLUF:** The corridor reopened [[ref:3]] under monitoring."
    origins = {
        "h1": {"evidence_text": body, "has_evidence_text": True, "source": "d",
               "target_id": "t", "produced_at": "", "ordinal": 1}
    }
    raw = body.encode("utf-8")
    needle = "The corridor reopened [[ref:3]] under monitoring."
    start = body.index(needle)
    a = {
        "schema": AA.ASSEMBLY_SCHEMA,
        "regime": "assembly",
        "blocks": [
            {
                "ordinal": 1,
                "finding_id": "h1",
                "desk": "d",
                "target_id": "t",
                "spans": [
                    {
                        "text": needle,
                        "origin": {
                            "head_id": "h1",
                            "start": start,
                            "end": start + len(needle.encode("utf-8")),
                            "body_sha256": hashlib.sha256(raw).hexdigest(),
                            "body_len": len(raw),
                        },
                    }
                ],
            }
        ],
    }
    res = AA.quote_fidelity(a, origins)
    assert res.findings == []
    assert AA.quote_fidelity_score(res) == 1.0


def test_a_future_schema_is_declined_loudly_not_graded() -> None:
    """``assembly.v2`` is not graded by v1 arms. It counts, so the miss is
    visible, and it decides nothing — the ``branch_versions`` discipline applied
    to the payload it grades."""
    res = AA.audit({"schema": "assembly.v2", "blocks": []}, clean_citations())
    assert res.findings == []
    assert res.counters == {"assembly_arms_schema_unknown": 1}


def test_a_malformed_payload_never_raises() -> None:
    """Degrade-not-drop: an arm never breaks the verify path."""
    for junk in (
        {"schema": AA.ASSEMBLY_SCHEMA, "blocks": "nope", "coverage": 7, "drops": 3},
        {"schema": AA.ASSEMBLY_SCHEMA, "blocks": [None, 5, {"spans": [None]}]},
        {"schema": AA.ASSEMBLY_SCHEMA, "blocks": [{"spans": [{"origin": []}]}]},
    ):
        for cits in (None, "not-a-list", [None, 4, {"no_ref": 1}]):
            assert AA.audit(junk, cits) is not None


# ---------------------------------------------------------------------------
# 4. THE FOLD — through the REAL verify pass
# ---------------------------------------------------------------------------

_COMPOSITION_BODY = (
    "**BLUF:** No coordinated narrative appears in Myanmar coverage in this "
    "desk’s collection through 3 September [[ref:1]].\n"
)


async def test_the_fold_lands_hard_spans_on_a_real_verify_pass(monkeypatch) -> None:
    """END TO END through ``verify_finding_faithfulness``: a corrupted assembly
    lands a HARD span in the ledger and a HARD fail_class on the critique."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    a = clean_assembly()
    a["blocks"][0]["desk"] = "proliferation_watch"
    report = await verify_finding_faithfulness(
        body=_COMPOSITION_BODY,
        citations=clean_citations(),
        assembly=a,
    )
    spans = [s for s in report.unsupported_spans if s.reason.startswith("attribution_")]
    assert [s.reason for s in spans] == [AA.ATTRIBUTION_DESK_MISMATCH]
    assert spans[0].as_dict()["fail_class"] == V.FAIL_CLASS_HARD
    assert report.counters["assembly_attribution_desk_mismatch"] == 1
    # The violation is a CHECKABLE-BUT-UNSUPPORTED claim: denominator +1,
    # numerator +0 — byte-identical arithmetic to every other fold here.
    assert any(
        cv.reason == AA.ATTRIBUTION_DESK_MISMATCH
        and cv.verdict == V.FAIL_CLASS_HARD
        for cv in report.claim_verdicts
    )


async def test_the_fold_stamps_the_assembly_branch_version(monkeypatch) -> None:
    """``branch_versions.assembly == 'assembly_arms.v1'`` lands on the critique —
    the same visible per-kind version contract the five prose kinds carry, for an
    instrument that has no judge at all."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_COMPOSITION_BODY,
        citations=clean_citations(),
        assembly=clean_assembly(),
    )
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    verification = payload["data"]["verification"]
    assert verification["branch_versions"]["assembly"] == "assembly_arms.v1"
    assert verification["branch_scores"]["assembly"]["score"] == 1.0
    assert verification["counters"]["assembly_arms_reads_audited"] == 1


async def test_the_pass_is_byte_identical_without_an_assembly(monkeypatch) -> None:
    """THE INERTNESS PROOF AT THE PASS LEVEL: the same finding graded with
    ``assembly=None`` and with the argument absent produce identical reports,
    and neither carries a single assembly counter."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    kw = {"body": _COMPOSITION_BODY, "citations": clean_citations()}
    a = await verify_finding_faithfulness(**kw)
    b = await verify_finding_faithfulness(**kw, assembly=None)
    assert a.as_dict() == b.as_dict()
    assert not [k for k in a.counters if k.startswith("assembly_")]


# ---------------------------------------------------------------------------
# 2026-09-05/1 — THE ARMS ARE THE GRADER OF AN ASSEMBLY ROW'S HEADLINE
# ---------------------------------------------------------------------------


async def test_the_headline_of_an_assembly_row_is_the_arms_ratio(monkeypatch):
    """THE PRODUCT-BREAKING DEFECT, as an assertion.

    On 2026-09-05 the legacy ``citation_support`` branch graded the
    quote-stitched assembly body on the grain it was written for — authored prose
    against a citation map — and returned 0.18 on rows whose ``assembly`` branch
    read 1.000. ``LEGBA_COMPOSITION_VERIFY_FLOOR`` is 0.50 and the world
    assembler is verify-floored on its inputs, so 31 of 32 byte-correct country
    assemblies were floored OUT of the world read.

    A clean assembly's published headline is now the arms' own ratio."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_COMPOSITION_BODY,
        citations=clean_citations(),
        assembly=clean_assembly(),
    )
    branch = report.branch_scores["assembly"]
    assert branch["score"] == 1.0
    assert report.checkable_claims == branch["checkable"]
    assert report.supported_claims == branch["supported"]
    assert report.faithfulness_score == 1.0
    assert report.score_denominator == branch["checkable"]
    assert report.counters["assembly_headline_regraded_to_arms"] == 1
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    assert payload["data"]["verification"]["overall_score"] >= 0.50


async def test_a_firing_arm_still_costs_the_assembly_row(monkeypatch) -> None:
    """The regrade is not an amnesty. A construction bug lands in the arms'
    OWN numerator, so it moves the published number — which is the whole point
    of grading a generated payload with a deterministic instrument."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    a = clean_assembly()
    a["blocks"][0]["desk"] = "proliferation_watch"
    report = await verify_finding_faithfulness(
        body=_COMPOSITION_BODY, citations=clean_citations(), assembly=a,
    )
    branch = report.branch_scores["assembly"]
    assert branch["score"] < 1.0
    assert report.faithfulness_score == branch["supported"] / branch["checkable"]
    assert report.checkable_claims == branch["checkable"]


async def test_the_regrade_never_touches_a_legacy_row(monkeypatch) -> None:
    """THE INERTNESS HALF. A flag-off composition carries a real ``assembly.v1``
    key with ``regime: legacy`` and nothing behind it; its headline is whatever
    the prose graders said, byte-for-byte, and no counter records a visit."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    kw = {"body": _COMPOSITION_BODY, "citations": clean_citations()}
    plain = await verify_finding_faithfulness(**kw)
    legacy = await verify_finding_faithfulness(
        **kw, assembly={"schema": AA.ASSEMBLY_SCHEMA, "regime": "legacy"},
    )
    assert plain.as_dict() == legacy.as_dict()
    assert "assembly_headline_regraded_to_arms" not in legacy.counters


async def test_a_rollup_is_audited_but_never_regraded(monkeypatch) -> None:
    """D-5's ``regime: "rollup"`` passes the AUDIT gate (it is an ``assembly.v1``
    stamp) and must not pass the GRADER gate: a rollup carries no blocks and no
    spans, so the arms' denominator would be the six ledger units alone and the
    headline would read 1.000 for a row nothing checked.

    (Today a rollup takes the ``structural_claims`` critique instead and never
    reaches this pass at all — verified live on 2026-09-05, where the one
    ``region_composition`` critique carried ``structural_verify: true`` and no
    faithfulness block. This is the guard that keeps that true if it changes.)"""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    rollup = {"schema": AA.ASSEMBLY_SCHEMA, "regime": "rollup", "blocks": []}
    assert AA.is_assembly(rollup) is True
    assert AA.is_grader_of_record(rollup) is False
    report = await verify_finding_faithfulness(
        body=_COMPOSITION_BODY, citations=clean_citations(), assembly=rollup,
    )
    assert "assembly_headline_regraded_to_arms" not in report.counters


def test_the_grader_gate_is_narrower_than_the_audit_gate() -> None:
    """Stated as a property rather than a case list, because the two gates
    disagreeing in the OTHER direction — a row graded by arms that did not audit
    it — is the failure that would publish a fabricated number."""
    for payload in (
        None,
        {},
        {"schema": AA.ASSEMBLY_SCHEMA, "regime": "legacy"},
        {"schema": AA.ASSEMBLY_SCHEMA, "regime": "rollup"},
        {"schema": AA.ASSEMBLY_SCHEMA},
        {"schema": "assembly.v2", "regime": "assembly"},
        {"schema": AA.ASSEMBLY_SCHEMA, "regime": AA.REGIME_ASSEMBLY},
    ):
        if AA.is_grader_of_record(payload):
            assert AA.is_assembly(payload)
    assert AA.is_grader_of_record(
        {"schema": AA.ASSEMBLY_SCHEMA, "regime": AA.REGIME_ASSEMBLY}
    )
    assert not AA.is_grader_of_record({"schema": AA.ASSEMBLY_SCHEMA})


def test_the_regrade_declines_an_empty_branch_rather_than_dividing_by_zero(
) -> None:
    """``regrade_to_arms`` is called with whatever ``branch_score`` returned, and
    that is ``None`` when nothing was audited. It must leave the report alone
    rather than publish ``0/0`` as a score."""
    made = AA.regrade_to_arms(object(), None)
    assert made is not None  # returned unchanged, no attribute touched
    made = AA.regrade_to_arms(object(), {"checkable": 0, "supported": 0})
    assert made is not None


def _buried_lead_eval() -> dict[str, Any]:
    """A ``data.eval`` block whose legacy salience check FAILED — the only shape
    ``fold_salience_lead`` charges for."""
    return {
        "salience_check": {
            "pass": False,
            "lead_ref": 1,
            "gap": 0.42,
            "top_title": "the top input",
            "reason": "lead magnitude 0.51 against top 0.93",
        }
    }


async def test_the_buried_lead_check_still_bites_on_a_legacy_row(monkeypatch) -> None:
    """THE CONTROL. Nothing about the legacy population moves: a flag-off
    composition whose producer stamped a failed salience check is charged
    exactly as it is today."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    legacy = clean_assembly()
    legacy["regime"] = AA.REGIME_LEGACY
    report = await verify_finding_faithfulness(
        body=_COMPOSITION_BODY,
        citations=clean_citations(),
        eval_block=_buried_lead_eval(),
        assembly=legacy,
    )
    assert "buried_lead_salience" in [s.reason for s in report.unsupported_spans]
    assert "salience_lead_suppressed_assembly" not in report.counters


async def test_the_buried_lead_check_is_disabled_on_an_assembly_row(
    monkeypatch,
) -> None:
    """D-3 owns this fold, and it is DISABLED for ``regime == "assembly"``.

    The legacy check compares the lead's magnitude to the top input's on
    ``max_salience()`` — a max-pool at sd 0.024, whose world burial guard is
    58 pass / 0 fail against a 0.300 threshold. On an assembled row every
    assumption under it is false: the key is repaired (``cited_mass.v1``,
    sd 1.673), the lead is PLURAL (``earned_single`` | ``co_leads`` | ``none``),
    and the ordering IS the payload — so the read cannot bury its own lead and
    there is no discretionary act left to charge for. The verdict is already on
    the row, with its arithmetic, in ``lead.test``.

    DISABLED, NOT RE-POINTED: re-aiming it at ``cited_mass.v1`` would mean
    inventing a gap threshold on a key with 70× the variance and no calibration,
    which is a new instrument wearing an old reason code.

    THE SUPPRESSION IS COUNTED, so "how often did the dead key disagree with the
    repaired one" is a number and not a silence.
    """
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_COMPOSITION_BODY,
        citations=clean_citations(),
        eval_block=_buried_lead_eval(),
        assembly=clean_assembly(),
    )
    assert "buried_lead_salience" not in [s.reason for s in report.unsupported_spans]
    assert report.counters["salience_lead_suppressed_assembly"] == 1


async def test_r2_stays_live_on_an_assembly_row(monkeypatch) -> None:
    """Only the SALIENCE half is withdrawn. D-3 does not ship ARM 5, so pulling
    the one detector that reads the producer's own contradiction ledger would
    leave the class with no grader at all until D-6."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_COMPOSITION_BODY,
        citations=clean_citations(),
        eval_block={
            "contradictions": [
                {"a_ref": 1, "b_ref": 2, "group": "corridor", "subject": ["hormuz"]}
            ]
        },
        assembly=clean_assembly(),
    )
    assert "unsurfaced_input_contradiction" in [
        s.reason for s in report.unsupported_spans
    ]


# ---------------------------------------------------------------------------
# 5. THE PREDECESSOR ARM, FIRED FOR THE FIRST TIME IN ITS LIFE
# ---------------------------------------------------------------------------


def test_attribution_ungrounded_quote_fires_on_a_corrupted_fixture() -> None:
    """D-1 §6 acceptance 4. ``composition_integrity.attribution_ungrounded_quote``
    is structurally the same test as ARM 1 and has fired **0 times, ever** — not
    because compositions quote faithfully but because 82% of composition bodies
    contain no quotation mark at all. It is LIVE, not merely present, and this
    proves it on a deliberately corrupted specimen: a coinage put in a real
    desk's mouth that appears nowhere in that desk's real read.

    It stays exactly where it is. It grades the LEGACY prose path; the assembly
    arms grade the assembled one. Do not rewrite it; extend it — and the
    extension is a population, not an edit.
    """
    corrupt = (
        'the narrative-coordination read notes a "manufactured consensus '
        'architecture" across three state-aligned outlets'
    )
    assert ci.ungrounded_quote(corrupt, BODY_MM) is not None
    # ...and the FAITHFUL relay of the same desk's real words costs nothing.
    faithful = (
        'the narrative-coordination read notes "No coordinated narrative appears '
        'in Myanmar coverage"'
    )
    assert ci.ungrounded_quote(faithful, BODY_MM) is None


# ---------------------------------------------------------------------------
# 6. THE SHARED FOLD
# ---------------------------------------------------------------------------


def test_the_fold_covers_what_the_old_dash_tables_missed() -> None:
    """The census found THREE different dash tables and none of them total.
    ``_UNICODE_HYPHENS`` covered U+2010–2013 only: NO em dash, NO minus sign."""
    for dash in "‐‑‒–—―−":
        assert normalize_for_match(f"energy{dash}security") == "energy-security"
    # U+00AD SOFT HYPHEN is DELETED, not folded to '-': it is an invisible
    # line-break hint and rendering it would split a word nobody saw split.
    assert normalize_for_match("soft­hyphen") == "softhyphen"
    # NFKC reaches what no dash table can: full-width forms and the narrow /
    # no-break spaces the wire writes inside dates.
    assert normalize_for_match("ＵＳ") == "us"
    assert normalize_for_match("3 September") == "3 september"
    # ``.casefold()``, not ``.lower()``.
    assert normalize_for_match("STRASSE") == normalize_for_match("straße")


def test_the_fold_is_idempotent_so_a_folded_caller_is_safe() -> None:
    """Load-bearing for the ``_absence_content_terms`` re-point: half its
    callers already folded their text before handing it over."""
    for s in (BODY_MM, BODY_CD_ENERGY, BODY_CD_MIL, "", "  ", "A — B"):
        assert normalize_for_match(normalize_for_match(s)) == normalize_for_match(s)


def test_fold_helpers_never_answer_yes_to_nothing() -> None:
    assert not fold_contains(BODY_MM, "")
    assert fold_contains(BODY_MM, "in this desk's collection")
    assert fold_equal("energy‑security", "Energy-Security")


def test_the_punct_table_covers_the_full_2010_2015_range() -> None:
    for cp in range(0x2010, 0x2016):
        assert PUNCT_FOLD[chr(cp)] == "-"
    assert PUNCT_FOLD["−"] == "-"


# ---------------------------------------------------------------------------
# 7. THE _absence_content_terms RE-POINT — the 56.5% arm, both directions
# ---------------------------------------------------------------------------


def test_absence_content_terms_now_folds_the_compound_the_desk_wrote() -> None:
    """THE MEASURED CHANGE. An ASCII-only tokenizer split ``energy‑security``
    written with U+2011 into ``{energy, security}`` — two terms, neither of them
    the compound — and now yields the one the desk actually wrote. The archived
    audit measured this changing the term set on 4,272 of 7,562 claims (56.5%)."""
    from legba.data.provenance.absence_slice import _absence_content_terms

    folded = _absence_content_terms(
        "No new energy‑security incidents are reported.", target_id=None
    )
    assert "energy-security" in folded
    assert "energy" not in folded and "security" not in folded
    # The ASCII spelling was ALREADY this, which is the whole point: one phrase,
    # one term set, whichever hyphen the producer happened to emit.
    ascii_terms = _absence_content_terms(
        "No new energy-security incidents are reported.", target_id=None
    )
    assert folded == ascii_terms


def test_absence_content_terms_is_now_caller_independent() -> None:
    """THE SHARPER DEFECT THE RE-POINT CLOSES: the function returned DIFFERENT
    TERM SETS DEPENDING ON THE CALLER. V-B and the six ``judge_quote_rules``
    sites passed UNFOLDED text; ``denied_enumeration`` and
    ``composition_integrity`` passed FOLDED text. One screen, two answers,
    decided by who called it."""
    from legba.data.provenance.absence_slice import _absence_content_terms

    raw = "No new energy‑security incidents in this desk’s collection."
    assert _absence_content_terms(raw, target_id=None) == _absence_content_terms(
        normalize_for_match(raw), target_id=None
    )


def test_the_re_point_does_not_widen_the_screen_on_ascii_prose() -> None:
    """THE OTHER DIRECTION, and it is the one that bounds the blast radius.

    The screen's tokenizer is ``[a-z][a-z\\-]{3,}``. On text carrying none of the
    characters the fold touches, ``normalize_for_match`` differs from the old
    ``.lower()`` only by collapsing whitespace runs — which the tokenizer cannot
    see — so the TOKEN SET IS IDENTICAL and no ASCII claim's screen moves. The
    56.5% is exactly the population that carries typography, and this is the
    complement of it, asserted rather than assumed.
    """
    from legba.data.provenance.absence_slice import _absence_content_terms

    tok = re.compile(r"[a-z][a-z\-]{3,}")
    plain = (
        "No coordinated narrative appears in Myanmar coverage in this desk's "
        "collection through 3 September."
    )
    assert set(tok.findall(normalize_for_match(plain))) == set(
        tok.findall(plain.lower())
    )
    # ...and on the SAME sentence written the way the wire writes it, the token
    # sets DIFFER. One string moves, the other does not: that is the whole shape
    # of the change, in two assertions.
    typo = plain.replace("desk's", "desk’s").replace(
        "coordinated narrative", "coordinated‑narrative"
    )
    assert set(tok.findall(normalize_for_match(typo))) != set(
        tok.findall(typo.lower())
    )
    assert "myanmar" in _absence_content_terms(plain, target_id=None)


# ---------------------------------------------------------------------------
# 8. THE REGISTRIES — exhaustive, drift-guarded
# ---------------------------------------------------------------------------


def test_every_counter_this_brick_can_bump_is_declared() -> None:
    """The receipts are enumerable from code (the V-G8 fidelity rule) rather than
    by grepping for ``bump(``. Two emission shapes: the literal counters, and the
    ``assembly_drops_{key}`` family the drop ledger publishes verbatim."""
    src = inspect.getsource(AA)
    bumped = set(re.findall(r'bump\(\s*"([a-z_]+)"', src))
    bumped |= {f"assembly_drops_{k}" for k in AA._DROP_COUNT_KEYS}
    assert bumped == set(AA.COUNTERS), bumped ^ set(AA.COUNTERS)


def test_every_reason_is_hard_and_in_the_one_table() -> None:
    """The severity DECISION lives in ``verify._FAIL_CLASS_BY_REASON`` and this
    module only contributes to it — one lookup, one drift guard."""
    assert set(AA.FAIL_CLASSES) <= set(V._FAIL_CLASS_BY_REASON)
    for reason in AA.FAIL_CLASSES:
        assert V.fail_class_for_reason(reason) == V.FAIL_CLASS_HARD


def test_the_coverage_status_literals_mirror_the_ledger_that_writes_them() -> None:
    """``data.provenance`` sits UNDER ``data.analysts`` in the import graph, so
    the four statuses are DUPLICATED as literals here (the
    ``verify._REGISTER_REF_KIND`` precedent). This is the drift guard that makes
    that safe."""
    from legba.data.analysts import composition_window as cw

    assert set(AA.COVERAGE_STATUSES) == {
        cw.COVERAGE_IN_BASIS,
        cw.COVERAGE_BELOW_FLOOR,
        cw.COVERAGE_UNVERIFIED,
        cw.COVERAGE_NO_HEAD,
    }


def test_the_drop_why_enum_is_the_specced_closed_set() -> None:
    """D-1 §1.7. Pinned here because D-2 has not shipped its producer yet: when
    it does, its own enum must equal this one and this pin is where the two
    meet."""
    assert AA.DROP_WHY_CLASSES == (
        "shown_not_selected",
        "not_selected",
        "cap_trimmed",
        "below_floor",
        "no_head_in_horizon",
        "superseded",
        "correlated_duplicate",
        "not_a_candidate",
    )


def test_the_arms_add_no_undefended_verdict_token_table() -> None:
    """D-1 §6: "Do not add a fourth undefended table".

    Two undefended verdict-token tables already exist —
    ``judge_verdict_parsing._judge_reason`` (token → reason) and
    ``verify._DEMOTION_COUNTERS`` (token → counter, nine entries plus one name
    hard-coded outside the table) — and only ``_FAIL_CLASS_BY_REASON`` is drift-
    guarded. This module contributes EXACTLY ONE mapping, ``FAIL_CLASSES``, and
    that one is guarded: it is spliced into the guarded table and every key is
    asserted present there. Any second dict added here would turn this red.
    """
    tables = {
        name
        for name, value in vars(AA).items()
        if isinstance(value, dict) and not name.startswith("__")
    }
    assert tables == {"FAIL_CLASSES"}, tables
    assert set(AA.FAIL_CLASSES) <= set(V._FAIL_CLASS_BY_REASON)


def test_the_claim_kind_axis_is_untouched() -> None:
    """F-11: D-3 does NOT build the D3/#71 claim-class split (zero code, own
    stamp, sequenced after #69). The ASSEMBLY branch is a new
    ``_JUDGE_PROFILES`` entry and NOT a ``_claim_kind`` return value, so the
    fleet's existing branch telemetry does not move."""
    kinds = {
        V._claim_kind(s)
        for s in (
            "## Heading",
            "**BLUF:** Something happened [1].",
            "No evidence of X appears in this desk's collection.",
            "Watch for a further escalation.",
            "Alpha struck Bravo [1].",
        )
    }
    assert V.CLAIM_KIND_ASSEMBLY not in kinds
    assert V.CLAIM_KIND_ASSEMBLY in V._JUDGE_PROFILES
    assert V._JUDGE_PROFILES[V.CLAIM_KIND_ASSEMBLY].judge_system is None
