# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G3 — THE WEIGHTED-COMPARISON LICENCE (``provenance.assessment_weighting``).

What this file proves, in the order the licence's own conditions run:

  * THE FENCE. Without the record's arithmetic in the evidence map there is no
    licence and no counter, and a composition finding is BYTE-IDENTICAL through
    the real verify pass with the fold in the chain. That is the population
    statement the ``2026-09-06/1`` stamp makes, proven rather than asserted.
  * THE THREE LICENSED SHAPES — ``order``, ``earned_lead``, ``refusal`` — each
    tied to a fact the record PUBLISHES, and each replayed from the N=5 prose
    that produced the residual.
  * THE FOUR DENIALS, and the one that matters most: the live 2026-09-06
    specimen, which is a weighted comparison AND a real failure, still fails.
  * THE ARITHMETIC. Numbers are checked against the arithmetic block and not
    against the voice's own good intentions.
  * THE VOCABULARY IS RESTATED, NOT IMPORTED (``data.provenance`` must not
    import ``data.analysts``), so each restatement is pinned against the module
    that owns it — the ``assembly_arms`` idiom.
  * THE BRANCH MOVES WITH THE VERDICT. ``branch_scores['citation_support']`` is
    the Assessment's published ``fidelity_to_spine``; a headline that moves over
    a stale branch is the defect the stamp exists to prevent.
"""

from __future__ import annotations

import json

import pytest

from legba.data.analysts import assessment_channel as ac
from legba.data.analysts import assessment_prompts as apr
from legba.data.analysts import assessment_unsupported as au
from legba.data.provenance import assessment_weighting as aw
from legba.data.provenance import verify as vf

# ---------------------------------------------------------------------------
# Fixtures — the arithmetic block in the shape ``render_lead_test`` prints it.
# ---------------------------------------------------------------------------

ARITH_NOT_EARNED = (
    "THE RECORD'S OWN ARITHMETIC — these are facts about the record.\n"
    "- concentration NOT earned this cycle: top-share 0.667, ratio 2.00 against "
    "bars 0.15 / 1.5, over 6 candidates (minimum 8). Lead state: none; "
    "lead ordinals: none.\n"
    "- 6 reads carried; 0 shown and not carried; 6 candidates ranked; 0 below "
    "the verification floor.\n"
)

ARITH_EARNED = (
    "THE RECORD'S OWN ARITHMETIC — these are facts about the record.\n"
    "- concentration EARNED this cycle: top-share 0.812, ratio 3.10 against "
    "bars 0.15 / 1.5, over 9 candidates (minimum 8). Lead state: earned_single; "
    "lead ordinals: 2.\n"
    "- 8 reads carried; 2 shown and not carried; 9 candidates ranked; 1 below "
    "the verification floor.\n"
)

DESK_SPAN = (
    "Tanker attacks in the Strait of Hormuz continue to disrupt the export "
    "chain."
)


def _evidence(ordinals, *, arithmetic=ARITH_NOT_EARNED, span=DESK_SPAN):
    return {
        int(n): ac.assessment_evidence_text(f"{span} (block {n})", arithmetic)
        for n in ordinals
    }


def _decide(claim, ordinals, *, arithmetic=ARITH_NOT_EARNED, span=DESK_SPAN):
    evidence = _evidence(ordinals, arithmetic=arithmetic, span=span)
    return aw.licence_for(
        claim,
        ordinals=ordinals,
        evidence_text="\n".join(evidence.values()),
        arithmetic=aw.arithmetic_from_evidence(evidence),
    )


# ---------------------------------------------------------------------------
# 1. THE FENCE
# ---------------------------------------------------------------------------


def test_without_the_arithmetic_ref_there_is_no_licence_and_no_counter() -> None:
    """CONDITION 1, and it is the whole population statement.

    A comparison whose cited entries carry no arithmetic gets ``None`` — not a
    denial. The distinction is deliberate: a denial is a MEASUREMENT about an
    Assessment claim, and this module has no opinion about the rest of the
    fleet's prose. Returning a denial here would put a counter on every
    composition in the tree.
    """
    evidence = {1: "a composition sub-claim", 2: "another sub-claim"}
    assert aw.arithmetic_from_evidence(evidence).present is False
    assert aw.licence_for(
        "Alpha outweighs Bravo [[ref:1]] [[ref:2]].",
        ordinals=[1, 2],
        evidence_text="a composition sub-claim",
        arithmetic=aw.arithmetic_from_evidence(evidence),
    ) is None


def test_only_the_assessment_channel_writes_the_rule_the_fence_keys_on() -> None:
    """The fence is a byte string, and exactly one producer emits it."""
    assert aw.ARITHMETIC_RULE == ac.EVIDENCE_ARITHMETIC_RULE.strip()
    assert aw.ARITHMETIC_RULE in ac.assessment_evidence_text("span", "arith")
    # …and an empty arithmetic block does NOT mint one, so a payload with no
    # counters declines the same way a composition does.
    assert aw.ARITHMETIC_RULE not in ac.assessment_evidence_text("span", "")


class _AllUnsupportedJudge:
    """Every prose claim UNSUPPORTED — the harshest arm, so anything that comes
    back supported came back through the licence and nowhere else."""

    subprovider = "g3_stub"

    def __init__(self) -> None:
        self.calls = 0

    async def chat_complete(self, messages, **kw):  # noqa: ANN001
        import re

        self.calls += 1
        n = len(re.findall(r"^\s*\d+\.\s", messages[-1]["content"], re.M)) or 1

        class _R:
            content = json.dumps({"verdicts": ["unsupported"] * n})

        return _R()


COMPOSITION_BODY = (
    "**BLUF:** The energy desk's disruption dominates this window [[ref:1]].\n"
    "\n"
    "The export chain is under pressure [[ref:1]]. The escalation read "
    "[[ref:2]] outweighs the trade thread [[ref:1]] on this cycle's evidence.\n"
)


def _composition_citations():
    return [
        {
            "marker": f"[[ref:{n}]]", "ordinal": n, "ref_kind": "finding",
            "ref_id": "22222222-2222-2222-2222-222222222222",
            "source": "world_composition", "title": f"sub-claim {n}",
            "evidence_text": f"A composition sub-claim {n}. {DESK_SPAN}",
        }
        for n in (1, 2)
    ]


@pytest.mark.asyncio
async def test_a_composition_is_byte_identical_through_the_real_pass(
    monkeypatch,
) -> None:
    """THE INERTNESS CONTRACT, held for a third time (H2, then D-3, now this).

    The same finding through the same pass, once with the fold in the chain and
    once with it replaced by the identity, produces the same verification dict
    field for field. An arm that cannot route is byte-identical for every caller
    — and this is the proof the ``2026-09-06/1`` lineage entry's "ASSEMBLY and
    DESK cannot move at all" rests on, at unit scale.
    """
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    citations = _composition_citations()

    with_fold = await vf.verify_finding_faithfulness(
        body=COMPOSITION_BODY, citations=citations,
        judge_llm=_AllUnsupportedJudge(), title="t",
    )
    monkeypatch.setattr(
        vf.assessment_weighting, "fold", lambda report, **kw: report
    )
    without = await vf.verify_finding_faithfulness(
        body=COMPOSITION_BODY, citations=citations,
        judge_llm=_AllUnsupportedJudge(), title="t",
    )
    assert json.dumps(with_fold.as_dict(), sort_keys=True, default=str) == (
        json.dumps(without.as_dict(), sort_keys=True, default=str)
    )
    assert not [k for k in with_fold.counters if k.startswith("weighted_")]


# ---------------------------------------------------------------------------
# 2. THE THREE LICENSED SHAPES
# ---------------------------------------------------------------------------


def test_the_order_shape_licenses_a_weighing_that_names_both_blocks() -> None:
    """``blocks[].ordinal`` follows the ``cited_mass.v1`` ranking BY
    CONSTRUCTION (``judge_input_checks``: "the ordering IS the payload"), so a
    sentence weighing two cited blocks is relaying a comparison the record
    performed over the whole day's candidate pool."""
    decision = _decide(
        "The export disruption [[ref:1]] outweighs the quiet escalation window "
        "[[ref:4]] in what it costs the reader this cycle.",
        [1, 4],
    )
    assert decision is not None and decision.granted
    assert decision.shape == aw.SHAPE_ORDER


def test_the_earned_lead_shape_relays_the_records_own_crowning() -> None:
    """``assessment_unsupported``'s third ``rank`` exemption, widened from the
    marker surface to the GRADER — which is exactly what D-6 §6.1 asked for.
    The arithmetic names lead ordinal 2; the sentence crowns ordinal 2."""
    decision = _decide(
        "Iran's export capacity is the primary driver this cycle [[ref:2]].",
        [2], arithmetic=ARITH_EARNED,
    )
    assert decision is not None and decision.granted
    assert decision.shape == aw.SHAPE_EARNED_LEAD


def test_crowning_the_records_top_ranked_block_is_also_the_order() -> None:
    """Ordinal 1 is the top-weight carried block by construction, and that is a
    different published fact from the earned-lead verdict: a record can decline
    to crown a SINGLE DRIVER (the separation bar) while still having ranked one
    block above the rest. Both are relays; neither is a mint."""
    decision = _decide(
        "The tanker attacks are the most consequential thread here [[ref:1]].",
        [1],
    )
    assert decision is not None and decision.granted
    assert decision.shape == aw.SHAPE_EARNED_LEAD


def test_the_refusal_shape_is_the_replays_own_contradicted_claim() -> None:
    """THE SPECIMEN. Verbatim from the N=5 replay's 09-04 12:00Z arm-B body,
    where it graded ``judge_contradicted`` — a HARD fail on a sentence relaying
    the record's own printed verdict. Marking it taxed the honest sentence and
    rewarded the crowned one, which is the failure this program reverses."""
    decision = _decide(
        "Together they push the day’s overall risk profile upward, with no "
        "single driver eclipsing the others [[ref:1]].",
        [1],
    )
    assert decision is not None and decision.granted
    assert decision.shape == aw.SHAPE_REFUSAL


# ---------------------------------------------------------------------------
# 3. THE FOUR DENIALS
# ---------------------------------------------------------------------------


def test_the_live_specimen_is_a_weighted_comparison_and_still_fails() -> None:
    """THE TEST OF WHETHER THE LICENCE IS HONEST, and it is the reason the
    number condition exists at all.

    The live 2026-09-06 00:15Z claim that dragged ``citation_support`` to 0.4286
    IS a weighted comparison — it says one read outweighs another — and it is
    also a real failure, because 9.10 / 0.85 / 5.05 / 0.60 are printed on a
    BLOCK'S ATTRIBUTION LINE, which ``spine_span_text`` does not carry and the
    evidence map therefore does not hold. A licence that pardoned this would be
    a rubric concession wearing a proof's clothes.
    """
    decision = _decide(
        "This single, high-mass (9.10) and well-verified (0.85) read outweighs "
        "other country signals, including the U.S. domestic diesel-price "
        "pressure (cited mass 5.05, verify 0.60) [[ref:1]] [[ref:2]].",
        [1, 2],
    )
    assert decision is not None and decision.granted is False
    assert decision.counter == aw.WEIGHTED_COMPARISON_DENIED_INSTRUMENT


def test_a_number_the_arithmetic_does_not_carry_denies_the_licence() -> None:
    """CONDITION 3 on its own, with no instrument vocabulary to decide it first:
    a bare quantity the record never published is a claim with no truthmaker in
    the map, whatever shape the comparison takes."""
    decision = _decide(
        "The first thread [[ref:1]] outweighs the second [[ref:2]] by a factor "
        "of 3.47 on this record.",
        [1, 2],
    )
    assert decision is not None and decision.granted is False
    assert decision.counter == aw.WEIGHTED_COMPARISON_DENIED_NUMBER


def test_a_number_the_arithmetic_does_carry_survives_the_check() -> None:
    """The other direction, and it is what keeps condition 3 from being a ban on
    arithmetic: the counters the record publishes about itself ARE in the map,
    which is P1's whole finding."""
    decision = _decide(
        "The six carried reads [[ref:1]] outweigh the nothing that was shown "
        "and not carried [[ref:2]].",
        [1, 2],
    )
    assert decision is not None and decision.granted


def test_ordinal_references_are_handles_and_never_quantities() -> None:
    """``[[ref:4]]`` and "Blocks 3-8" name blocks. Charging a sentence for
    naming the blocks it compares would deny the licence to exactly the
    sentences that earned it."""
    decision = _decide(
        "Blocks 3 and 4 weigh against block 1 [[ref:3]] [[ref:4]].",
        [3, 4],
    )
    assert decision is not None and decision.granted


def test_a_weighing_naming_one_side_stays_as_graded() -> None:
    """THE SPECIMEN, verbatim from the replay's 09-04 12:00Z arm-B body. The
    comparison is real and the reader cannot check it: one ordinal is named and
    the other side is prose. The brief's own wording is "cites the arithmetic
    ref AND both compared blocks", and this is the half that is missing."""
    decision = _decide(
        "Block 5 notes that Iran’s naval‑mine deployment further "
        "tightens the Hormuz blockade, though it treats this as a secondary "
        "factor compared with the tanker attacks [[ref:5]].",
        [5],
    )
    assert decision is not None and decision.granted is False
    assert decision.counter == aw.WEIGHTED_COMPARISON_DENIED_UNPAIRED


def test_crowning_against_the_records_own_order_stays_as_graded() -> None:
    """THE SPECIMEN, verbatim from the replay's 09-04 00:00Z arm-B body. It
    crowns a thread the record's order puts fifth and sixth of six, on a cycle
    whose arithmetic says concentration was NOT earned. The prompt forbids
    exactly this ("YOU MAY NOT CROWN AGAINST IT"), so it is a real failure and
    the licence has nothing to say for it."""
    decision = _decide(
        "Consequently the assessment treats Myanmar’s campaign as the "
        "highest‑weight thread, with US‑MENA pressure and Sudan’s "
        "buildup as secondary but still significant contributors "
        "[[ref:6]][[ref:5]].",
        [6, 5],
    )
    assert decision is not None and decision.granted is False
    assert decision.counter == aw.WEIGHTED_COMPARISON_DENIED_CROWNED


def test_a_refusal_on_a_record_that_DID_crown_is_denied() -> None:
    """The refusal shape is a RELAY, so it needs something to relay. On a record
    whose arithmetic earned its concentration, "no single driver dominates"
    contradicts a number printed on the same page — the voice may disagree with
    the arithmetic, but not by claiming to carry it."""
    decision = _decide(
        "No single driver dominates this cycle [[ref:2]].",
        [2], arithmetic=ARITH_EARNED,
    )
    assert decision is not None and decision.granted is False
    assert decision.counter == aw.WEIGHTED_COMPARISON_DENIED_CROWNED


def test_prose_that_is_not_a_comparison_is_not_in_the_class_at_all() -> None:
    """``None``, not a denial: the licence is silent about ordinary world prose
    and must never appear in its counters."""
    assert _decide(
        "Kharg Island terminals were struck on 3 September [[ref:1]].", [1],
    ) is None


# ---------------------------------------------------------------------------
# 4. THE RESTATED VOCABULARY, PINNED AGAINST ITS OWNER
# ---------------------------------------------------------------------------


def test_the_restated_lexicons_match_the_modules_that_own_them() -> None:
    """``data.provenance`` must not import ``data.analysts`` — the edge runs the
    other way, which is why ``assembly_arms`` restates ``assembly_spans``' scope
    tokens and holds them with a test. Same discipline, same shape: an edit on
    either side fails here rather than drifting silently."""
    assert aw._INSTRUMENT_TERMS == tuple(au._INSTRUMENT_TERMS)
    assert aw._FLEX_SEP == au._FLEX_SEP
    assert aw._NEGATOR_RE.pattern == au._NEGATOR_RE.pattern
    # The crowning set is the RANK lexicon's single-winner half plus the shapes
    # the N=5 replay itself wrote (which is why it is not a strict subset).
    shared = set(aw._CROWNING_TERMS) & set(au._RANK_TERMS)
    assert len(shared) >= 25, sorted(set(au._RANK_TERMS) - set(aw._CROWNING_TERMS))


def test_the_lead_verdict_is_read_back_from_the_bytes_the_prompt_prints() -> None:
    """``arithmetic_from_evidence`` parses ``render_lead_test``'s OWN wording, so
    a change to how the record states its verdict fails here rather than
    silently turning every licence into a decline."""
    payload = {
        "lead": {
            "kind": "earned_single",
            "block_ordinals": [1],
            "test": {
                "earned": True, "ratio_12": 3.1, "top_share": 0.812,
                "bar_share": 0.15, "bar_ratio": 1.5, "n_candidates": 9,
                "min_candidates": 8,
            },
        },
    }
    rendered = apr.render_lead_test(payload)
    read_back = aw.arithmetic_from_evidence(
        {1: ac.assessment_evidence_text("span", f"- {rendered}")}
    )
    assert read_back.present
    assert read_back.earned is True
    assert read_back.lead_ordinals == frozenset({1})


# ---------------------------------------------------------------------------
# 5. THE FOLD, THROUGH THE REAL PASS
# ---------------------------------------------------------------------------


ASSESSMENT_BODY = (
    "**Two threads, weighed against each other**\n"
    "\n"
    "**BLUF:** Export disruption in the strait leads this record [[ref:1]].\n"
    "\n"
    "## The reading\n"
    "The export chain is under pressure in the desk's own words [[ref:1]]. "
    "Together they push the day's risk profile upward, with no single driver "
    "eclipsing the others [[ref:1]]. The export disruption [[ref:1]] outweighs "
    "the quiet escalation window [[ref:2]] in what it costs the reader. This "
    "high-mass (9.10) read outweighs the escalation window (cited mass 5.05) "
    "[[ref:1]] [[ref:2]].\n"
)


def _assessment_citations(arithmetic=ARITH_NOT_EARNED):
    return [
        {
            "marker": f"[[ref:{n}]]", "ordinal": n, "ref_kind": "finding",
            "ref_id": "11111111-1111-1111-1111-111111111111",
            "source": "world_assessor", "title": f"block {n}",
            "evidence_text": ac.assessment_evidence_text(
                f"{DESK_SPAN} (block {n})", arithmetic
            ),
        }
        for n in (1, 2)
    ]


@pytest.mark.asyncio
async def test_the_licence_lifts_only_what_it_licenses_and_counts_the_rest(
    monkeypatch,
) -> None:
    """The whole fold through ``verify_finding_faithfulness``, against a judge
    that marks EVERYTHING unsupported — so every supported verdict in the result
    came through the licence, and the ones that did not are the denials."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await vf.verify_finding_faithfulness(
        body=ASSESSMENT_BODY, citations=_assessment_citations(),
        judge_llm=_AllUnsupportedJudge(), title="t",
    )
    counters = report.counters
    assert counters[aw.WEIGHTED_COMPARISON_LICENSED] == 2
    assert counters[aw.WEIGHTED_COMPARISON_DENIED_INSTRUMENT] == 1
    # The counters PARTITION: every comparison seen is licensed or denied, once.
    denied = sum(
        v for k, v in counters.items()
        if k.startswith("weighted_comparison_denied_")
    )
    assert counters[aw.WEIGHTED_COMPARISON_SEEN] == (
        counters[aw.WEIGHTED_COMPARISON_LICENSED] + denied
    )
    lifted = [
        cv for cv in report.claim_verdicts
        if cv.verdict == "supported" and (cv.detail or "").startswith("[")
    ]
    assert {d.split("]")[0].lstrip("[") for d in (cv.detail for cv in lifted)} == {
        aw.SHAPE_REFUSAL, aw.SHAPE_ORDER,
    }
    # The instrument specimen is still a failure, and still in the spans.
    assert any(
        "high-mass" in s.text for s in report.unsupported_spans
    ), "the denied comparison must remain an unsupported span"


@pytest.mark.asyncio
async def test_the_branch_moves_with_the_verdicts_it_lifted(monkeypatch) -> None:
    """``branch_scores['citation_support']`` IS the Assessment's published
    ``fidelity_to_spine`` — the number the G3 bar is about. Every prior override
    stage carries ``branch_scores`` through unchanged, which is correct for them
    and would be a defect here: a row whose headline moved over a stale branch
    tells two stories about one grading."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    citations = _assessment_citations()

    licensed = await vf.verify_finding_faithfulness(
        body=ASSESSMENT_BODY, citations=citations,
        judge_llm=_AllUnsupportedJudge(), title="t",
    )
    monkeypatch.setattr(
        vf.assessment_weighting, "fold", lambda report, **kw: report
    )
    base = await vf.verify_finding_faithfulness(
        body=ASSESSMENT_BODY, citations=citations,
        judge_llm=_AllUnsupportedJudge(), title="t",
    )
    kind = vf.CLAIM_KIND_CITATION_SUPPORT
    assert base.branch_scores[kind]["score"] == 0.0
    assert licensed.branch_scores[kind]["score"] > base.branch_scores[kind]["score"]
    # The DENOMINATOR never moves: a licence lifts a verdict, it never withdraws
    # a claim from the count.
    assert (
        licensed.branch_scores[kind]["checkable"]
        == base.branch_scores[kind]["checkable"]
    )
    # …and no other branch is touched.
    for other in set(base.branch_scores) - {kind}:
        assert licensed.branch_scores[other] == base.branch_scores[other]


@pytest.mark.asyncio
async def test_the_licence_can_only_move_a_score_upward(monkeypatch) -> None:
    """It lifts verdicts and never creates one. Stated as an inequality over the
    same body on both trees, because "upward only" is the property the lineage
    entry declares and a reader has to be able to check it."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    citations = _assessment_citations()
    licensed = await vf.verify_finding_faithfulness(
        body=ASSESSMENT_BODY, citations=citations,
        judge_llm=_AllUnsupportedJudge(), title="t",
    )
    monkeypatch.setattr(
        vf.assessment_weighting, "fold", lambda report, **kw: report
    )
    base = await vf.verify_finding_faithfulness(
        body=ASSESSMENT_BODY, citations=citations,
        judge_llm=_AllUnsupportedJudge(), title="t",
    )
    assert licensed.faithfulness_score >= base.faithfulness_score
    assert licensed.checkable_claims == base.checkable_claims
    assert licensed.supported_claims >= base.supported_claims


def test_a_licence_over_a_hard_verdict_is_counted_apart() -> None:
    """A relayed refusal to crown arrived as ``judge_contradicted`` — HARD — in
    the live 09-04 12:00Z replay, and that is the ONLY way this train can move
    the SEVERITY family. It gets its own counter precisely so the move is
    sizeable on its own rather than inferred from the score.

    Driven against the fold directly with a hand-built ledger, because the hard
    class is EARNED (``judge_contradicted`` demotes to
    ``judge_contradicted_unquoted`` without a resolving quote) and a stub judge
    that produced one would be testing the quote rules, not this.
    """
    claim = (
        "Together they push the day's risk profile upward, with no single "
        "driver eclipsing the others [[ref:1]]."
    )
    report = vf.FaithfulnessReport(
        faithfulness_score=0.0,
        checkable_claims=1,
        supported_claims=0,
        unsupported_spans=[
            vf.UnsupportedSpan(text=claim, reason="judge_contradicted")
        ],
        judge_status="llm",
        branch_scores={
            vf.CLAIM_KIND_CITATION_SUPPORT: {
                "checkable": 1, "supported": 0, "score": 0.0,
            }
        },
        claim_verdicts=[
            vf.ClaimVerdict.failed(claim, "judge_contradicted", [1])
        ],
        score_denominator=1,
    )
    out = aw.fold(
        report, body=f"{claim}\n", citations=_assessment_citations()
    )
    assert out.counters[aw.WEIGHTED_COMPARISON_LICENSED] == 1
    assert out.counters[aw.WEIGHTED_COMPARISON_LICENSED_HARD] == 1
    assert out.faithfulness_score == 1.0
    assert out.branch_scores[vf.CLAIM_KIND_CITATION_SUPPORT]["score"] == 1.0
    # The ERASE receipt the override contract requires, so an attempt counter
    # and the surviving ledger rows still reconcile.
    assert out.counters["override_erased_judge_contradicted"] == 1


def test_no_reason_code_moves_across_this_bump() -> None:
    """The stamp's own claim, held mechanically: the licence adds COUNTERS and
    the fail-class table is byte-identical. A new reason would have to be
    declared in ``_FAIL_CLASS_BY_REASON`` and pinned twice, and none is."""
    for name in dir(aw):
        if not name.startswith("WEIGHTED_COMPARISON"):
            continue
        assert getattr(aw, name) not in vf._FAIL_CLASS_BY_REASON, name
