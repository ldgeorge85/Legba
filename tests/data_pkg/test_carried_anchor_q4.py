# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Q4 — THE CARRIED DATED ANCHOR, pinned against the case that produced it.

THE GOLDEN. ``leadership_transition`` on ``country_g20_fr``, finding aa4a8991
(2026-09-27 19:37, confidence 0.9, a 120-row slice), wrote:

    "the presidential hand-over from Emmanuel Macron to his successor is
     virtually certain on 28 September 2026"

Nothing in that slice names the date or the event — it held Senate elections,
a remark about a term-limited departure, and the Pope. The date was
nevertheless already in the RENDERED EVIDENCE, sitting inside the PRIOR READ
block, which is a citable ref carrying real ``evidence_text``. The
faithfulness judge — asked whether a claim FOLLOWS FROM the evidence its
markers name — answered yes, twice, at 1.00 (5/5 supported); the WINDOW LEDGER
re-fed the sentence every cycle (24 findings since 07-01, "No material change:
the scheduled 28 September handover remains"); the countdown ticked and the
confidence rose. A fabricated dated anchor survived three months through the
carry and the verify gate.

The fixtures below are that case and its controls, in BOTH citation
conventions, plus the positive control the brief asks for — the same date
present in a cited SIGNAL, which must stay supported. The second half pins the
PROMPT side: the rule reaches every unit through the code-appended grounding
clause, the one-sentence fence renders ON both memory blocks, and the
DESK-scoped ledger render (the composition floor's, i.e. region and world) is
byte-identical.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

import legba.data.analysts.unit_grounding as ug
import legba.data.analysts.window_ledger as wl
import legba.data.provenance.grounding_backstops as gb
import legba.data.provenance.verify as verify
from legba.data.analysts._tradecraft import (
    CARRIED_ANCHOR_BLOCK_NOTE,
    CARRIED_ANCHOR_DISCLOSURE,
    CARRIED_ANCHOR_RULE,
)
from legba.data.provenance.verify import verify_finding_faithfulness

# ---------------------------------------------------------------------------
# The France fixture
# ---------------------------------------------------------------------------

#: The prior read's ordinal in the unit's flat ``[N]`` space — past the slice
#: count, exactly as the live block sat.
PRIOR_ORD = 121
LEDGER_ORD = 122
SIGNAL_ORD = 7

#: The prior read was produced on 27 September; the anchor it carries is the
#: 28th. The two dates differ by one day ON PURPOSE: the block's own STAMP is
#: exempt and the claim inside it is not, and a fixture where they coincided
#: would prove nothing about which of the two the guard reads.
PRIOR_PRODUCED_AT = "2026-09-27T19:37:11.482901+00:00"

#: The prior read's rendered block, as ``unit_grounding`` captures it — header
#: lines, the Q4 fence, then the previous cycle's prose carrying the anchor.
PRIOR_BODY = (
    "*As of 26 September 2026; slice covers the trailing 72 hours to that "
    "date; 118 signals.*\n"
    "**BLUF:** No material change: the scheduled 28 September 2026 hand-over "
    "from Emmanuel Macron to his successor remains virtually certain "
    "[prior:3].\n"
)


def _prior_row() -> dict[str, Any]:
    return {
        ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_PRIOR_READ,
        "id": str(uuid4()),
        "analyst_id": "leadership_transition",
        "title": "Macron succession: hand-over remains on track",
        "produced_at": PRIOR_PRODUCED_AT,
        "age_hours": 23.4,
        "body": PRIOR_BODY,
    }


def _ledger_entries() -> list[dict[str, Any]]:
    return [
        {
            "finding_id": str(uuid4()),
            "analyst_id": "leadership_transition",
            "severity": "elevated",
            "severity_rank": 3,
            "title": "Senate elections held without incident",
            "read_date": "2026-09-21",
            "day": "2026-09-21",
            "produced_at": "2026-09-21T06:00:00+00:00",
        },
        {
            "finding_id": str(uuid4()),
            "analyst_id": "leadership_transition",
            "severity": "elevated",
            "severity_rank": 3,
            "title": "Term-limited departure restated by a minister",
            "read_date": "2026-09-24",
            "day": "2026-09-24",
            "produced_at": "2026-09-24T06:00:00+00:00",
        },
    ]


def _unit_citations(*, with_signal: bool = True) -> list[dict[str, Any]]:
    """The unit ``[N]`` convention: a real signal, the prior read, the ledger."""
    cits: list[dict[str, Any]] = []
    if with_signal:
        cits.append(
            {
                "marker": f"[{SIGNAL_ORD}]",
                "ordinal": SIGNAL_ORD,
                "signal_id": str(uuid4()),
                "source_text": (
                    "Senate elections were held on 27 September; the Elysee "
                    "made no announcement about a hand-over."
                ),
            }
        )
    cits.append(ug.citation_for_block(_prior_row(), PRIOR_ORD))
    cits.append(
        ug.citation_for_block(
            {
                ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_WINDOW_LEDGER,
                ug.GROUNDING_PAYLOAD_KEY: _ledger_entries(),
            },
            LEDGER_ORD,
        )
    )
    return cits


#: THE SENTENCE. The live BLUF, with its marker pointing at the prior read —
#: which is exactly how it was cited and exactly why it scored 1.00.
FRANCE_BLUF = (
    "The presidential hand-over from Emmanuel Macron to his successor is "
    f"virtually certain on 28 September 2026 [{PRIOR_ORD}]."
)


# ---------------------------------------------------------------------------
# 1. The golden: the France sentence is marked, the control is not
# ---------------------------------------------------------------------------


def test_the_france_bluf_is_marked_carried_unsupported() -> None:
    """A dated claim whose only citation resolves to the PRIOR READ."""
    spans = gb.carried_anchor_spans(FRANCE_BLUF + "\n", _unit_citations())
    assert [s.reason for s in spans] == ["carried_unsupported"]
    assert spans[0].markers == [PRIOR_ORD]
    assert spans[0].text == FRANCE_BLUF
    assert verify._FAIL_CLASS_BY_REASON[spans[0].reason] == verify.FAIL_CLASS_SOFT


def test_the_positive_control_the_same_date_in_a_cited_signal_is_supported() -> None:
    """THE CONTROL the rule turns on: the identical sentence, cited to a SIGNAL.

    The guard never second-guesses what a cited signal says — that is the
    judge's question. All it asks is whether the sentence rests on reporting
    at all, and here it does.
    """
    body = (
        "The presidential hand-over from Emmanuel Macron to his successor is "
        f"virtually certain on 28 September 2026 [{SIGNAL_ORD}].\n"
    )
    assert gb.carried_anchor_spans(body, _unit_citations()) == []


def test_a_signal_beside_the_memory_block_passes() -> None:
    """ALL resolved markers must be memory. One real citation alongside and the
    sentence is grounded in that citation."""
    body = (
        "The hand-over is expected on 28 September 2026 "
        f"[{SIGNAL_ORD}][{PRIOR_ORD}].\n"
    )
    assert gb.carried_anchor_spans(body, _unit_citations()) == []


def test_the_live_no_change_sentence_is_marked() -> None:
    """THE SENTENCE THAT ACTUALLY CARRIED IT, verbatim from head aa4a8991 (only
    the ordinals re-pointed at this fixture's blocks).

    It matters that this one is in the file, because it is the shape a guard
    gated on ``_is_fact_asserting`` would MISS: "No material change: … remains
    unchanged" is a no-change idiom, the floor exempts that class on purpose,
    and the exemption is exactly why the carry was free for three months.
    """
    body = (
        "- No material change: the constitutionally scheduled hand\u2011over on "
        f"28 September 2026 remains unchanged (see prior read [{PRIOR_ORD}] and "
        f"the window\u2011ledger entry [{LEDGER_ORD}]).\n"
    )
    assert not verify._is_fact_asserting(body.strip())
    spans = gb.carried_anchor_spans(body, _unit_citations())
    assert [s.reason for s in spans] == ["carried_unsupported"]
    assert spans[0].markers == [PRIOR_ORD, LEDGER_ORD]


def test_the_window_ledger_carries_the_same_class() -> None:
    """§A's other half: the fortnight ledger re-fed the anchor every cycle, and
    a ledger-only citation warrants a date no more than a prior read does."""
    body = (
        "The scheduled 28 September 2026 hand-over remains the desk's central "
        f"expectation [{LEDGER_ORD}].\n"
    )
    spans = gb.carried_anchor_spans(body, _unit_citations())
    assert [s.reason for s in spans] == ["carried_unsupported"]
    assert spans[0].markers == [LEDGER_ORD]


# ---------------------------------------------------------------------------
# 2. The exemptions — each one is a sentence the house PROMPT asks for
# ---------------------------------------------------------------------------


def test_the_licensed_continuity_idiom_is_never_flagged() -> None:
    """The prior read's OWN produced_at is a fact about the record, and the
    grounding clause's obligation (3) asks for it in these words."""
    body = (
        "No material change since the 27 September morning read "
        f"[{PRIOR_ORD}].\n"
    )
    assert gb.carried_anchor_spans(body, _unit_citations()) == []


def test_a_date_printed_on_a_ledger_line_is_never_flagged() -> None:
    """``window_ledger_rule``'s obligation (1) — carry a ledger line WITH THE
    DATE PRINTED ON THAT LINE — must stay followable."""
    body = (
        "Senate elections recorded on 21 September have since passed without "
        f"incident [{LEDGER_ORD}].\n"
    )
    assert gb.carried_anchor_spans(body, _unit_citations()) == []


def test_the_licensed_disclosure_is_never_flagged() -> None:
    """The honest move the rule offers. Flagging the sentence the prompt asks
    for would make the rule unfollowable."""
    body = (
        "A 28 September 2026 hand-over is carried from the prior read, not "
        f"re-sourced this window [{PRIOR_ORD}].\n"
    )
    assert gb.carried_anchor_spans(body, _unit_citations()) == []
    assert CARRIED_ANCHOR_DISCLOSURE in body


def test_a_qualitative_carry_asserts_no_anchor() -> None:
    """No date, no numeral: a purely qualitative continuity sentence is exactly
    what the prior-read block is FOR."""
    body = (
        "The succession picture is unchanged against the prior read "
        f"[{PRIOR_ORD}].\n"
    )
    assert gb.carried_anchor_spans(body, _unit_citations()) == []


def test_an_ordinal_is_a_handle_and_never_a_figure() -> None:
    """The markers are masked before a single digit is read — otherwise every
    cited sentence would carry a 'figure'."""
    body = f"The succession picture is unchanged [{PRIOR_ORD}][{LEDGER_ORD}].\n"
    assert gb.carried_anchor_spans(body, _unit_citations()) == []


def test_a_figure_the_ledger_itself_prints_is_its_own_stamp() -> None:
    """The ledger header prints its window length; a duration read off the
    block's own lines is licensed by ``window_ledger_rule`` obligation (2)."""
    body = (
        "Nothing has moved on this dimension across the trailing 14 days "
        f"[{LEDGER_ORD}].\n"
    )
    assert gb.carried_anchor_spans(body, _unit_citations()) == []


def test_a_figure_no_memory_block_prints_is_marked() -> None:
    """The FIGURE leg: a count asserted on memory alone is the same defect as
    a date asserted on memory alone."""
    body = f"Forty-two prefectures, 42 of them, have reported [{PRIOR_ORD}].\n"
    spans = gb.carried_anchor_spans(body, _unit_citations())
    assert [s.reason for s in spans] == ["carried_unsupported"]


# ---------------------------------------------------------------------------
# 3. Inertness — the property that keeps the fleet byte-identical
# ---------------------------------------------------------------------------


def test_the_guard_is_inert_without_a_memory_citation() -> None:
    """No prior-read and no window-ledger ref ⇒ no spans, whatever the prose
    says. Every finding that never sees a memory block keeps its pre-Q4
    verdict."""
    plain = [{"ordinal": 1, "ref_kind": "finding", "marker": "[[ref:1]]"}]
    body = "The hand-over is certain on 28 September 2026 [[ref:1]].\n"
    assert gb.carried_anchor_spans(body, plain) == []
    assert gb.carried_anchor_spans(body, None) == []
    assert gb.carried_anchor_spans(body, "not a list") == []
    assert gb.carried_anchor_spans("", _unit_citations()) == []


def test_a_malformed_citation_list_never_raises() -> None:
    """A guard never breaks the verify path."""
    assert gb.carried_anchor_spans(FRANCE_BLUF, [None, 3, {"ordinal": "x"}]) == []


def test_the_composition_spelling_is_covered_too() -> None:
    """A COMPOSITION's prior-read citation carries ``ref_kind='finding'`` (its
    drill target really is a finding row) and says so on ``continuity``.
    Reading only ``ref_kind`` would leave unguarded the tier the France claim
    was carried UP through (country_composition 8c54f65c)."""
    cits = [
        {
            "ordinal": 2,
            "marker": "[[ref:2]]",
            "ref_kind": "finding",
            "continuity": wl.CONTINUITY_PRIOR,
            "produced_at": PRIOR_PRODUCED_AT,
            "evidence_text": PRIOR_BODY,
        }
    ]
    body = (
        "The French hand-over remains scheduled for 28 September 2026 "
        "[[ref:2]].\n"
    )
    spans = gb.carried_anchor_spans(body, cits)
    assert [s.reason for s in spans] == ["carried_unsupported"]
    # ...and the composition's own continuity idiom still passes.
    ok = "No material change since the 27 September read [[ref:2]].\n"
    assert gb.carried_anchor_spans(ok, cits) == []


# ---------------------------------------------------------------------------
# 4. The fold — the mark has to COUNT, not merely exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_mark_demotes_faithfulness_exactly_like_an_uncited_fact() -> None:
    """THE POINT OF THE LANE. The live head scored 1.00 twice. With the guard
    folded, the same body cannot: the carried sentence is one more
    checkable-but-unsupported claim in the denominator."""
    body = (
        f"{FRANCE_BLUF}\n\n"
        "## What changed\n"
        f"- Senate elections were held without incident [{SIGNAL_ORD}].\n"
    )
    report = await verify_finding_faithfulness(
        body=body, citations=_unit_citations()
    )
    reasons = [s.reason for s in report.unsupported_spans]
    assert "carried_unsupported" in reasons
    assert report.faithfulness_score < 1.0
    assert report.checkable_claims > report.supported_claims


@pytest.mark.asyncio
async def test_the_control_body_is_not_demoted_by_this_guard() -> None:
    """The same shape, cited to the signal: the guard contributes nothing."""
    body = (
        "The presidential hand-over is expected on 28 September 2026 "
        f"[{SIGNAL_ORD}].\n\n"
        "## What changed\n"
        f"- Senate elections were held without incident [{SIGNAL_ORD}].\n"
    )
    report = await verify_finding_faithfulness(
        body=body, citations=_unit_citations()
    )
    assert "carried_unsupported" not in [
        s.reason for s in report.unsupported_spans
    ]


# ---------------------------------------------------------------------------
# 5. The PROMPT half — the rule the guard is the backstop for
# ---------------------------------------------------------------------------


def test_the_rule_reaches_every_unit_through_the_code_appended_clause() -> None:
    """Appended in CODE rather than pasted into nine descriptors: one
    definition, nine units, no drift — and no descriptor bytes move, which is
    what keeps the VOICE-4 flip digests intact."""
    assert CARRIED_ANCHOR_RULE in ug.UNIT_GROUNDING_CLAUSE
    once = ug.with_grounding_clause("SYSTEM PROMPT")
    assert once.count(CARRIED_ANCHOR_RULE) == 1
    # Idempotent by the clause fingerprint, like every other leg of the append.
    assert ug.with_grounding_clause(once) == once


def test_the_rule_names_the_signal_block_as_the_only_warrant() -> None:
    """The three load-bearing clauses, so a reword cannot quietly drop one."""
    assert "MEMORY, NOT EVIDENCE" in CARRIED_ANCHOR_RULE
    assert "ONLY where a SIGNAL block you cite IN THAT SAME SENTENCE" in (
        CARRIED_ANCHOR_RULE
    )
    assert "THE ONE EXCEPTION IS THE BLOCK'S OWN STAMP" in CARRIED_ANCHOR_RULE
    assert "not re-sourced this window" in CARRIED_ANCHOR_RULE


def test_the_fence_renders_on_the_prior_read_block() -> None:
    """The clause is read once at the top of a long prompt; the temptation
    arrives here, with the carried date on the next line."""
    lines = ug._render_prior_read(_prior_row(), PRIOR_ORD)
    assert f"    {CARRIED_ANCHOR_BLOCK_NOTE}" in lines
    # IMMEDIATELY above the body it fences.
    assert lines.index(f"    {CARRIED_ANCHOR_BLOCK_NOTE}") == len(lines) - 2
    assert lines[-1].startswith("    body: ")


def test_the_fence_renders_on_the_unit_ledger_block_only() -> None:
    """UNIT scope only: the desk-scoped render is the composition floor's, and
    region/world prompt bytes do not move for a defect measured at the unit
    tier."""
    unit = wl.ledger_block_lines(_ledger_entries(), f"[{LEDGER_ORD}]", scope="unit")
    desk = wl.ledger_block_lines(_ledger_entries(), "[[ref:4]]")
    assert f"    {CARRIED_ANCHOR_BLOCK_NOTE}" in unit
    assert not any(CARRIED_ANCHOR_BLOCK_NOTE in ln for ln in desk)


def test_the_unit_ledger_block_still_fits_inside_its_own_capture() -> None:
    """The derived bound, re-checked WITH the fence line: a block that renders
    longer than its capture is one whose tail the model reads and the judge
    cannot. 14 chars of slack remain — the next header edit re-derives
    ``LEDGER_UNIT_TOTAL_CAP`` rather than shaving this further."""
    worst = [
        {
            "finding_id": str(uuid4()),
            "analyst_id": "composition_lineage_sweep",
            "severity": "critical",
            "severity_rank": 4,
            "title": "X" * wl.LEDGER_TITLE_CHARS,
            "read_date": "27 September 2026",
            "day": f"2026-09-{(i % 28) + 1:02d}",
            "produced_at": f"2026-09-{(i % 28) + 1:02d}T12:00:00+00:00",
        }
        for i in range(wl.LEDGER_UNIT_TOTAL_CAP)
    ]
    rendered = "\n".join(wl.ledger_block_lines(worst, "[19]", scope="unit"))
    assert len(rendered) <= ug.EVIDENCE_TEXT_CHARS


# ---------------------------------------------------------------------------
# 6. Drift guards — the literals ``data.provenance`` may not import
# ---------------------------------------------------------------------------


def test_the_memory_ref_kind_literals_match_their_owners() -> None:
    """``data.provenance`` must not import ``data.analysts`` (the edge runs the
    other way), so these are restated. This is what makes that safe."""
    assert ug.GROUNDING_PRIOR_READ in gb._MEMORY_REF_KINDS
    assert wl.WINDOW_LEDGER_REF_KIND in gb._MEMORY_REF_KINDS
    assert gb._MEMORY_REF_KINDS == frozenset(
        {ug.GROUNDING_PRIOR_READ, wl.WINDOW_LEDGER_REF_KIND}
    )
    assert gb._CONTINUITY_KEY == wl.CONTINUITY_CITATION_KEY


def test_the_disclosure_literal_matches_the_prompt() -> None:
    """The guard exempts the exact words the prompt asks for. If one moves and
    the other does not, the rule becomes unfollowable in silence."""
    assert gb._CARRY_DISCLOSURE == CARRIED_ANCHOR_DISCLOSURE
    assert CARRIED_ANCHOR_DISCLOSURE in CARRIED_ANCHOR_BLOCK_NOTE


def test_both_memory_kinds_are_registered_grounding_refs() -> None:
    """A kind outside ``GROUNDING_REF_KINDS`` would never reach a citation at
    all, and the guard would be inert against a block nobody can cite."""
    from legba.data.provenance.kinds import GROUNDING_REF_KINDS

    assert gb._MEMORY_REF_KINDS <= GROUNDING_REF_KINDS
