# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PARTIAL VERDICTS + THE EVIDENCE ENVELOPE (``2026-09-20/1``).

Two defects, measured live on the paid Nemotron judge route, and one stamp.

**A — the verdict-count mismatch failed the whole row.** ~1.5% of judge calls
end at ``verify.faithfulness.judge_failed err=judge returned 22 verdicts for 23
claims`` (15 on 2026-09-19, 4 on 2026-09-20; the same shape at 73 for 74). One
dropped verdict out of twenty-odd dropped the row to the deterministic floor,
``judge-unavailable:judge_error``, capped at the PROVISIONAL 0.85 ceiling.

The repair salvages such a response IFF it says WHICH claim each verdict is
for, and the tests below are built around that line:

  * a response that NAMES its claims (``{"claim": 3, "verdict": ...}``, or a
    parallel ``claim_indices`` array) aligns by id, and the claims nobody named
    come back UNCHECKED — out of the numerator, out of the judged denominator,
    never handed to the severity chain, and carrying no JUDGE ledger row (the
    deterministic floor's own row for that claim carries over, as it already
    does for a floored partition's claims);
  * a short response of BARE tokens is unreadable, not partial — every verdict
    after the drop point may belong to the claim before it — so it keeps
    today's hard failure, deliberately;
  * a gap wider than ``partial_verdict_budget`` keeps it too.

**B — the evidence envelope was too short.** ``_EVIDENCE_TOTAL_CHARS`` 4,000 ->
8,000, ``_EVIDENCE_SOURCE_CHARS`` 3,000 -> 6,000, ``_EVIDENCE_GROUNDING_CHARS``
2,400 -> 4,800, so a claim that resolves deep in a long evidence string stops
reading as absent from it.

The end-to-end tests drive the REAL verify path — ``verify_finding_faithfulness``
→ ``_maybe_llm_judge`` → ``_run_judge`` → ``_judge_claim_partition`` — and stub
only the handler's ``chat_complete``, the same discipline
``test_verify_judge_transport`` holds itself to.

**H3 (stamp ``2026-09-24/1``) built on this file's machinery:** the reply
contract now ASKS every verdict entry to name its claim (``claim_index``) —
the id arm below used to fire only when a model volunteered ids. The A1b
block pins the contract's own key, the positional fallback, and the prompt
itself.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from legba.data.provenance.judge_assessability import (
    PROVISIONAL_SCORE_CEILING,
    gate_score,
    is_provisional,
)
from legba.data.provenance.judge_pipeline_version import (
    JUDGE_PIPELINE_VERSION,
    METRIC_FAMILIES,
    SHIFT_MOVES,
    STAMP_EXPECTED_SHIFTS,
    STAMP_LINEAGE,
    expected_shift,
    poolable_stamps,
)
from legba.data.provenance.judge_transport import (
    JUDGE_PARTIAL_VERDICTS,
    JUDGE_STATUS_PARTIAL,
    split_unchecked,
    resolve_partition_outcome,
)
from legba.data.provenance.judge_verdict_parsing import (
    ALIGNED_BY_CLAIM_INDEX,
    ALIGNED_BY_POSITIONAL,
    _JudgeVerdictError,
    _VERDICT_UNCHECKED,
    align_verdicts,
    partial_verdict_budget,
)
from legba.data.provenance.verify import (
    _ABSENCE_JUDGE_SYSTEM,
    _EVIDENCE_GROUNDING_CHARS,
    _EVIDENCE_SOURCE_CHARS,
    _EVIDENCE_TOTAL_CHARS,
    verify_finding_faithfulness,
)


# ---------------------------------------------------------------------------
# Stub handler — copied in shape from test_verify_judge_transport._ScriptedJudge
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0
    total_tokens = 0
    cost_estimate_usd = 0.0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.finish_reason = "stop" if content else "error"
        self.raw_response = {"provider": "Nvidia", "choices": [{}]}
        self.usage = _Usage()


class _ScriptedJudge:
    """Replays one response per judge ROUTE, keyed on prompt IDENTITY."""

    subprovider = "stub"

    def __init__(self, *, shared: str, absence: str) -> None:
        self._scripts = {"shared": shared, "absence": absence}
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}

    async def chat_complete(self, messages, *, system=None, **kw):
        route = "absence" if (system or "") == _ABSENCE_JUDGE_SYSTEM else "shared"
        self.calls.append(route)
        self.prompts[route] = messages[0]["content"] if messages else ""
        return _Response(self._scripts[route])


@pytest.fixture(autouse=True)
def _judge_on(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")


def _mixed_body(sid: str) -> tuple[str, list[dict[str, Any]]]:
    """Two cited positive claims + one embedded absence → the V3 two-partition
    route (shared claims at span positions 1 and 2, the absence at 3)."""
    body = (
        "The lira fell three percent today [1].\n"
        "The central bank spent two billion dollars defending the peg [1].\n"
        "No evidence of capital-flight controls appears in the reviewed signals.\n"
    )
    citations = [
        {"marker": "[1]", "signal_id": sid, "title": "Lira drops 3% on the day"}
    ]
    return body, citations


# ---------------------------------------------------------------------------
# A1. The alignment contract, directly
# ---------------------------------------------------------------------------


def test_a_full_positional_response_aligns_exactly_as_it_always_did():
    """The healthy case and the overwhelming majority: bare tokens, right
    length, quotes parallel. Nothing about this path may move."""
    raw = ["supported", "contradicted"]
    parsed = {"verdicts": raw, "quotes": ["", "the refinery kept running"]}
    slots, missing, aligned_by = align_verdicts(raw, parsed, 2)
    assert missing == []
    assert slots == [("supported", ""), ("contradicted", "the refinery kept running")]
    assert aligned_by == ALIGNED_BY_POSITIONAL


def test_a_short_response_that_names_its_claims_leaves_the_rest_unchecked():
    """THE REPAIR, at the grain it happens: 2 verdicts for 3 claims, each one
    naming the claim it grades, so claim 2 is UNCHECKED rather than guessed."""
    raw = [
        {"claim": 1, "verdict": "supported"},
        {"claim": 3, "verdict": "contradicted", "quote": "capital controls imposed"},
    ]
    slots, missing, aligned_by = align_verdicts(raw, {"verdicts": raw}, 3)
    assert missing == [1]  # 0-based; claim 2 in the judge's own numbering
    assert slots[0] == ("supported", "")
    assert slots[1] is None
    assert slots[2] == ("contradicted", "capital controls imposed")
    assert aligned_by == ALIGNED_BY_CLAIM_INDEX


def test_a_short_response_of_bare_tokens_is_unreadable_and_still_fails():
    """THE LINE. Positions are ambiguous the moment one is missing — every
    verdict after the drop point may belong to the claim before it — so this
    keeps the #116d hard failure rather than fabricating an alignment."""
    with pytest.raises(_JudgeVerdictError) as exc:
        align_verdicts(["supported", "supported"], {}, 3)
    assert "2 verdicts for 3 claims" in str(exc.value)


def test_a_gap_wider_than_the_budget_fails_even_when_it_names_its_claims():
    """A response missing more than a tenth of its answers is not a judge that
    skipped a line; identity does not buy it a salvage."""
    raw = [{"claim": 1, "verdict": "supported"}]
    with pytest.raises(_JudgeVerdictError) as exc:
        align_verdicts(raw, {"verdicts": raw}, 6)
    assert "over the partial budget" in str(exc.value)
    # ...and the measured live shapes are comfortably INSIDE it.
    assert partial_verdict_budget(23) == 3 and partial_verdict_budget(74) == 8
    assert partial_verdict_budget(2) == 2 and partial_verdict_budget(30) == 3


def test_a_duplicate_claim_id_is_not_an_alignment():
    """Two verdicts for one claim is a contradiction, not an identity map — the
    whole id arm is refused and the strict length contract decides."""
    raw = [
        {"claim": 1, "verdict": "supported"},
        {"claim": 1, "verdict": "contradicted"},
    ]
    with pytest.raises(_JudgeVerdictError):
        align_verdicts(raw, {"verdicts": raw}, 3)


def test_a_half_labelled_response_is_ambiguous_exactly_where_it_matters():
    """All-or-nothing by design: one unlabelled entry and the list is back to
    being positional, which a short list cannot be read as."""
    raw = [{"claim": 1, "verdict": "supported"}, "supported"]
    with pytest.raises(_JudgeVerdictError):
        align_verdicts(raw, {"verdicts": raw}, 3)


def test_a_parallel_claim_index_array_is_read_as_identity_too():
    """The other shape a judge reaches for: flat verdicts, a sidecar naming the
    claims. Ordinals are 1-based in the prompt and stay 1-based here."""
    raw = ["supported", "contradicted"]
    parsed = {"verdicts": raw, "claim_indices": [2, 4], "quotes": ["", "q"]}
    slots, missing, aligned_by = align_verdicts(raw, parsed, 4)
    assert missing == [0, 2]
    assert slots[1] == ("supported", "") and slots[3] == ("contradicted", "q")
    assert aligned_by == ALIGNED_BY_CLAIM_INDEX


def test_a_claim_key_holding_claim_TEXT_is_skipped_not_mistaken_for_an_index():
    """``{"claim": "<the claim>"}`` is the other thing that key means. It is not
    an ordinal, so the entry names no claim and the response is positional."""
    raw = [
        {"claim": "The lira fell three percent today", "verdict": "supported"},
        {"claim": "The bank spent two billion", "verdict": "unsupported"},
    ]
    slots, missing, aligned_by = align_verdicts(raw, {"verdicts": raw}, 2)
    assert missing == []
    assert slots == [("supported", ""), ("unsupported", "")]
    assert aligned_by == ALIGNED_BY_POSITIONAL


def test_an_ordinal_written_as_a_string_or_with_punctuation_still_reads():
    raw = [{"n": "1.", "verdict": "supported"}, {"n": "#3", "verdict": "supported"}]
    slots, missing, aligned_by = align_verdicts(raw, {"verdicts": raw}, 3)
    assert missing == [1] and slots[0] and slots[2]
    assert aligned_by == ALIGNED_BY_CLAIM_INDEX


def test_an_out_of_range_ordinal_never_mis_attributes_a_verdict():
    """A claim number the prompt never issued buys no salvage — the id arm is
    refused wholesale rather than dropping the entry and aligning the rest."""
    raw = [{"claim": 1, "verdict": "supported"}, {"claim": 9, "verdict": "supported"}]
    with pytest.raises(_JudgeVerdictError):
        align_verdicts(raw, {"verdicts": raw}, 3)


def test_split_unchecked_drops_the_slot_and_names_the_claim():
    """The sentinel never reaches the severity chain; the caller gets the
    surviving verdicts against their ORIGINAL span positions and the 1-based
    numbers of the claims that went ungraded."""
    graded, unchecked = split_unchecked(
        [0, 2, 5],
        [
            ("supported", "", ALIGNED_BY_CLAIM_INDEX),
            (_VERDICT_UNCHECKED, "", ALIGNED_BY_CLAIM_INDEX),
            ("contradicted", "q", ALIGNED_BY_CLAIM_INDEX),
        ],
    )
    assert graded == [
        (0, ("supported", "", ALIGNED_BY_CLAIM_INDEX)),
        (5, ("contradicted", "q", ALIGNED_BY_CLAIM_INDEX)),
    ]
    assert unchecked == [3]


# ---------------------------------------------------------------------------
# A1b. H3 (2026-09-24/1) — the reply contract ASKS for the id
# ---------------------------------------------------------------------------


def test_claim_index_entries_out_of_order_align_by_id():
    """The contract's own key, deliberately scrambled: identity decides, never
    position — claim 3's verdict lands on claim 3 however it is ordered."""
    raw = [
        {"claim_index": 3, "verdict": "contradicted", "quote": "the port stayed open"},
        {"claim_index": 1, "verdict": "supported"},
    ]
    slots, missing, aligned_by = align_verdicts(raw, {"verdicts": raw}, 3)
    assert missing == [1]
    assert slots[0] == ("supported", "")
    assert slots[2] == ("contradicted", "the port stayed open")
    assert aligned_by == ALIGNED_BY_CLAIM_INDEX


def test_entries_that_name_nothing_stay_positional():
    """The fallback the contract keeps: a full-length reply whose object entries
    carry no id key aligns exactly as bare tokens always did."""
    raw = [{"verdict": "supported"}, {"verdict": "unsupported"}]
    slots, missing, aligned_by = align_verdicts(raw, {"verdicts": raw}, 2)
    assert missing == []
    assert slots == [("supported", ""), ("unsupported", "")]
    assert aligned_by == ALIGNED_BY_POSITIONAL


async def test_the_reply_contract_asks_every_route_to_name_its_claims():
    """H3's actual mechanism: the PROMPT carries ``claim_index`` — the id arm
    existed since 09-20 but nothing asked the model for the ids. The shared
    lead states it in the user prompt; the absence rubric states it in the
    system prompt; the M14 survey rides the shared lead verbatim."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=(
            '{"verdicts": [{"claim_index": 1, "verdict": "supported"}, '
            '{"claim_index": 2, "verdict": "supported"}]}'
        ),
        absence='{"verdicts": [{"claim_index": 1, "verdict": "supported"}]}',
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "llm"
    assert '"claim_index"' in judge.prompts["shared"]
    assert '"claim_index"' in _ABSENCE_JUDGE_SYSTEM

    # H3-MEASURE: a full, gap-free id-named reply — every row aligned by id,
    # nothing unmatched, no reply-count mismatch.
    assert all(cv.aligned_by == ALIGNED_BY_CLAIM_INDEX for cv in rep.claim_verdicts)
    out = rep.as_dict()
    assert out["aligned_by_id"] == len(rep.claim_verdicts) == 3
    assert out["aligned_positionally"] == 0
    assert out["unmatched_claims"] == 0
    assert out["miscount_claims"] == 0


async def test_a_short_reply_keyed_by_claim_index_marks_the_gap_unchecked():
    """The lane's proof shape end to end on the contract's OWN key: the shared
    partition answers 1 of 2 claims naming them by claim_index, so the ungraded
    claim leaves the judged population and the row reads partial — not the
    pre-H3 ``judge_error`` floor."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared='{"verdicts": [{"claim_index": 2, "verdict": "supported"}]}',
        absence='{"verdicts": [{"claim_index": 1, "verdict": "supported"}]}',
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == JUDGE_STATUS_PARTIAL
    assert rep.judge_unavailable_reason == (
        f"{JUDGE_PARTIAL_VERDICTS}:citation_support:1"
    )
    assert rep.judge_partial_claims == [1]
    assert rep.supported_claims == 2

    # H3-MEASURE (2026-09-25/1): both surviving verdicts were salvaged by id, so
    # the ledger says so on their rows; the THIRD row is the unmatched claim's
    # CARRIED floor row (never touched by alignment — aligned_by stays None).
    aligned_vals = [cv.aligned_by for cv in rep.claim_verdicts]
    assert aligned_vals.count(ALIGNED_BY_CLAIM_INDEX) == 2
    assert aligned_vals.count(None) == 1
    out = rep.as_dict()
    assert out["aligned_by_id"] == 2
    assert out["aligned_positionally"] == 0
    # unmatched: the ONE shared claim (claim_index 1) the reply never named —
    # the same population ``judge_partial_claims`` already lists.
    assert out["unmatched_claims"] == 1
    # miscount: shared returned 1 verdict for 2 claims sent (-1); absence
    # returned 1 for 1 (0). Summed across partitions: -1.
    assert out["miscount_claims"] == -1


# ---------------------------------------------------------------------------
# A2. The state, the reason and the ceiling rule
# ---------------------------------------------------------------------------


def test_a_partial_verdict_set_reports_under_its_own_reason_prefix():
    """An empty partition and a short answer are different provider behaviours.
    The row must be able to say which, so they never share a prefix."""
    status, reason = resolve_partition_outcome(
        judged=True, floored=[], unchecked={"citation_support": 1}
    )
    assert status == JUDGE_STATUS_PARTIAL
    assert reason == f"{JUDGE_PARTIAL_VERDICTS}:citation_support:1"


def test_both_kinds_of_gap_ride_one_reason_string_in_order():
    status, reason = resolve_partition_outcome(
        judged=True, floored=["absence"], unchecked={"citation_support": 2}
    )
    assert status == JUDGE_STATUS_PARTIAL
    assert reason == (
        "judge_empty_partition:absence;judge_partial_verdicts:citation_support:2"
    )


def test_no_gap_is_still_plain_llm_and_byte_identical():
    assert resolve_partition_outcome(judged=True, floored=[], unchecked={}) == (
        "llm",
        None,
    )
    assert resolve_partition_outcome(judged=True, floored=[]) == ("llm", None)
    assert resolve_partition_outcome(judged=False, floored=[]) == (
        "deterministic",
        None,
    )


# ---------------------------------------------------------------------------
# A3. The REAL path, end to end
# ---------------------------------------------------------------------------


async def test_a_dropped_verdict_no_longer_discards_the_whole_row():
    """THE LIVE DEFECT, in miniature. The shared partition grades 1 of its 2
    claims and NAMES it; before this train the ``_JudgeVerdictError`` took the
    absence verdict down with it and published the floor under ``judge_error``
    at the 0.85 provisional ceiling."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared='{"verdicts": [{"claim": 1, "verdict": "supported"}]}',
        absence='{"verdicts": ["supported"]}',
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == JUDGE_STATUS_PARTIAL
    assert rep.judge_unavailable_reason == (
        f"{JUDGE_PARTIAL_VERDICTS}:citation_support:1"
    )
    # The verdicts the judge DID produce survived, on both partitions.
    assert rep.supported_claims == 2
    assert rep.branch_scores["citation_support"]["checkable"] == 1
    assert rep.branch_scores["absence"]["checkable"] == 1
    # The unchecked claim is named on the row, in span order, 1-based.
    assert rep.judge_partial == 1 and rep.judge_partial_claims == [2]
    block = rep.as_dict()
    assert block["judge_partial"] == 1 and block["judge_partial_claims"] == [2]
    # NOT provisional: a grader adjudicated the claims that survive, so the
    # 0.85 cap does not apply (judge_transport.resolve_partition_outcome).
    assert not is_provisional(rep.judge_status)
    assert block["provisional"] is False


async def test_the_unchecked_claim_is_never_scored_either_way():
    """No FABRICATED verdict, in either direction. The ungraded claim is in no
    branch denominator and carries no judge span; the row it does carry in the
    ledger is the DETERMINISTIC floor's own, carried over by the
    ``carried_ledger`` rule that has always handled claims the judge could not
    grade. That is provenance, not a verdict the judge did not give."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared='{"verdicts": [{"claim": 2, "verdict": "contradicted"}]}',
        absence='{"verdicts": ["supported"]}',
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    ungraded = "The lira fell three percent today [1]."
    assert rep.judge_partial_claims == [1]
    assert rep.branch_scores["citation_support"]["checkable"] == 1
    assert not any(ungraded in s.text for s in rep.unsupported_spans)
    carried = [cv for cv in rep.claim_verdicts if ungraded in cv.text]
    assert len(carried) == 1 and carried[0].reason is None
    # ...and it is out of the judged tallies: one shared verdict + one absence.
    assert rep.checkable_claims == 2


async def test_a_bare_short_response_still_lands_on_the_floor_as_judge_error():
    """UNCHANGED, deliberately. Nothing names the claims, so nothing is
    salvageable, and the row keeps the behaviour it has today — including the
    provisional ceiling, which is what makes it visible."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared='{"verdicts": ["supported"]}',
        absence='{"verdicts": ["supported"]}',
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "deterministic"
    assert rep.judge_unavailable_reason == "judge_error"
    assert rep.judge_partial is None
    assert is_provisional(rep.judge_status)
    assert gate_score(
        score=1.0,
        ceiling=None,
        score_state=rep.score_state,
        provisional=rep.provisional,
    ) == pytest.approx(PROVISIONAL_SCORE_CEILING)


async def test_a_labelled_response_at_the_right_length_is_read_as_the_verdict():
    """The silent defect the id arm also repairs: an object-form entry used to
    be stringified whole, miss the four-token vocabulary and coerce to
    ``unsupported`` — a clean pass published as two failures."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared=(
            '{"verdicts": [{"claim": 1, "verdict": "supported"}, '
            '{"claim": 2, "verdict": "supported"}]}'
        ),
        absence='{"verdicts": ["supported"]}',
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "llm"
    assert rep.judge_unavailable_reason is None
    assert rep.judge_partial is None
    assert rep.supported_claims == 3
    assert not any(s.reason.startswith("judge_") for s in rep.unsupported_spans)


async def test_a_complete_pass_carries_no_partial_receipt_at_all():
    """SPARSE. Every complete pass keeps a byte-identical verification block —
    the new keys are absent, not zero."""
    sid = str(uuid4())
    body, citations = _mixed_body(sid)
    judge = _ScriptedJudge(
        shared='{"verdicts": ["supported", "supported"]}',
        absence='{"verdicts": ["supported"]}',
    )
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge
    )
    assert rep.judge_status == "llm"
    block = rep.as_dict()
    assert "judge_partial" not in block
    assert "judge_partial_claims" not in block
    assert block["judge_attempts"] == 2  # the receipt pair is unchanged


# ---------------------------------------------------------------------------
# B. The evidence envelope
# ---------------------------------------------------------------------------


def test_the_judge_evidence_envelope_is_the_widened_one():
    """The three caps, pinned so a silent narrowing is a test failure and not a
    live verdict. 8,000 total is the load-bearing one: the unit evidence string
    is ``OUTLET + title + SOURCE + Analyst summary`` and the source alone is
    stored at 3,200, so 4,000 left the summary ~700 chars and cut it."""
    assert _EVIDENCE_TOTAL_CHARS == 8000
    assert _EVIDENCE_SOURCE_CHARS == 6000
    assert _EVIDENCE_GROUNDING_CHARS == 4800


def test_the_grader_is_never_narrower_than_what_the_producer_captured():
    """THE INVARIANT B is really about, in both directions it has a producer.

    A cap the GRADER applies below what the PRODUCER captured is a window the
    model was shown and the judge cannot read — the P0c defect, and the shape
    that false-demotes a faithful claim about the tail of its own evidence.
    """
    from legba.data.analysts.composition_citations import MAX_EVIDENCE_TEXT_CHARS
    from legba.data.analysts.inline_target import _SOURCE_TEXT_CHARS
    from legba.data.analysts.unit_grounding import EVIDENCE_TEXT_CHARS

    assert _EVIDENCE_TOTAL_CHARS >= MAX_EVIDENCE_TEXT_CHARS
    assert _EVIDENCE_SOURCE_CHARS >= _SOURCE_TEXT_CHARS
    assert _EVIDENCE_GROUNDING_CHARS >= EVIDENCE_TEXT_CHARS


def test_a_long_source_reaches_the_judge_whole_instead_of_being_re_cut():
    """The judge-side re-truncation is what also mislabelled a COMPLETE article
    as an 'authoritative excerpt' (F1), softening 'absent => unsupported'. At
    6,000 the store cap binds first, so a stored source is shown whole and
    announced as what it is."""
    from legba.data.provenance.verify import _marker_to_evidence

    src = ("The refinery resumed throughput on Tuesday. " * 90)[:3200].strip()
    ev = _marker_to_evidence(
        [
            {
                "marker": "[1]",
                "signal_id": str(uuid4()),
                "title": "Ryazan refinery",
                "source_text": src,
                "snippet": "Throughput resumed.",
            }
        ]
    )
    shown = ev[1]
    assert src in shown, "the stored source no longer survives the judge's cap"
    assert "authoritative excerpt" not in shown
    assert len(shown) <= _EVIDENCE_TOTAL_CHARS


# ---------------------------------------------------------------------------
# The STAMP — both changes move verdicts, so the population splits
# ---------------------------------------------------------------------------


def test_the_train_ships_behind_one_new_stamp():
    """H3's OWN stamp, pinned by literal string: a later lane
    (``2026-09-25/1``, H3-MEASURE — persisting HOW H3 aligned) has since
    become the current head, so this no longer reads off ``JUDGE_PIPELINE_
    VERSION`` (see that stamp's own test for the current-head assertions)."""
    assert "2026-09-24/1" in STAMP_LINEAGE
    assert STAMP_LINEAGE[STAMP_LINEAGE.index("2026-09-24/1") - 1] == "2026-09-20/1"
    assert STAMP_LINEAGE[-1] == JUDGE_PIPELINE_VERSION == "2026-09-25/1"


def test_the_stamp_declares_all_three_families_moving():
    """Neither arm is score-neutral. A moves rows out of the floor and into
    adjudication; B changes what the grader can SEE on every unit finding, which
    is the one effect that cannot be predicted from here. H3's own stamp,
    pinned by literal string (see the docstring above)."""
    stamp = "2026-09-24/1"
    entry = STAMP_EXPECTED_SHIFTS[stamp]
    assert set(entry) == set(METRIC_FAMILIES)
    for family in METRIC_FAMILIES:
        assert expected_shift(stamp, family) == SHIFT_MOVES
        # H3-MEASURE (2026-09-25/1) declares SHIFT_NONE entering it, so it now
        # pools FORWARD with this stamp — the walk is correct, not stale.
        assert poolable_stamps(stamp, family) == (stamp, "2026-09-25/1")


def test_no_reason_code_was_added_removed_or_re_classed():
    """The census claim in the lineage entry, checkable: ``judge_partial_verdicts``
    is a REASON PREFIX on ``judge_unavailable_reason``, never a fail-class
    reason, so the severity table is byte-identical across this bump."""
    from legba.data.provenance.verify import _FAIL_CLASS_BY_REASON

    assert JUDGE_PARTIAL_VERDICTS not in _FAIL_CLASS_BY_REASON
    assert _VERDICT_UNCHECKED not in _FAIL_CLASS_BY_REASON
    from legba.data.provenance.judge_absence_rubric import JUDGE_VERDICT_TOKENS

    assert _VERDICT_UNCHECKED not in JUDGE_VERDICT_TOKENS
