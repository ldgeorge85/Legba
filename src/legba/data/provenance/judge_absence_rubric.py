# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE ABSENCE RUBRIC + THE FOURTH VERDICT — judge-subsystem brick 6.

Two changes that arrived together on the 2026-08-21 train, and they are one
cohesive unit: the rewritten ABSENCE system prompt (RUST-2) is the first prompt
in the tree that can emit the fourth verdict (RUST-3), so the rubric text and
the contract token that only it advertises live beside each other rather than
straddling the ``verify`` seam.

RUST-2 — WHY THE ABSENCE ROUTE GOT ITS OWN DOCTRINE PROMPT.
``PANEL_2026-08-16/lens4_groundtruth_attribution.md`` measured the adjudicated
judge errors per PROMPT BRANCH and found the negative route was the worst
surface in the system by a wide margin:

    branch        n    wrong   rate
    ABSENCE       7      6     86%
    GENERIC      30      8     27%
    NULL-RESULT   5      1     20%

n is small and is stated as small everywhere it is used. It is corroborated by
MECHANISM rather than by rate alone, which is what makes it actionable: 8 of 13
preserved ABSENCE payloads omit the ``PRIOR READ`` label that the shared quote
rule's anti-update sentence keys on, while 27 of 27 GENERIC payloads carry it.
The rule was real, the route just could not satisfy its precondition — so the
rewrite below judges analyst prose by SHAPE and never by label. The second
measured mechanism is the same shape: every wrong item carrying carve-out or
scale language in its own wording had NO rendered ``QUALIFIERS`` line, while
all 7 claims that DID get one were judged correctly — so the rewrite tells the
seat to read the limiting words off the claim itself.

A CONTINUITY fence was drafted and then REMOVED, and the removal is the more
useful record. It said what V-G2/V-I5 already enforce in code — a continuity read
("no material change since the prior read") denies a difference between two
assessments, so a row describing the current state cannot refute it — and it was
added mid-train on ONE suppression observation. The catch arm then priced it: on
the adjudicated true fails it cost R5-H1 (v3 2/6 -> v4 0/6), a prior-read
continuity claim, and the gate ("v4 catch >= v3") did not clear with it in.
Ablating it is the whole content of the second measurement pass.

The lesson is worth more than the sentence was: a fence that merely RESTATES a
deterministic rule the pipeline already applies buys nothing on the pass side and
can only cost on the catch side, because the code path has the claim either way
while the prompt version also reaches claims the router never excluded. V-I5
already demotes the hard class for exactly this shape; the judge did not need
telling, and telling it turned a demotion into a pass.

The prior rubric (``absence.v3``, 2026-07-16) was a verdict definition and
nothing else: ~1.4k chars of supported/contradicted/unsupported, inheriting the
generic judge's framing for everything else. It was written to kill the
0.0/0.2/1.0 variance on identical absence prose and it did that. What it never
carried is the four doctrine dimensions the 2026-08-11 prompt doctrine names —
who the seat is, what each error costs, what it is reading, and the adjudicated
record of what goes wrong in this chair — and D5 Q4 deliberately held it out of
the generic-prompt matrix as an internal control so this rewrite could be
attributed on its own. ``absence.v4`` is that rewrite.

REGISTER. The prompt DEFINES THE INSTITUTION IN TEXT and never names the
platform or assumes the model knows what it is (the D6 ``SHARED_PREAMBLE_NOTE``
P1 rule, applied to a judge seat instead of an analyst seat). A judge that does
not know it is the last reader before publication cannot weigh what its errors
cost, and a model asked about "Legba" knows nothing at all.

RUST-3 — THE FOURTH VERDICT.
``JUDGE_PROMPT_V2_DRAFT_2026-08-16.md`` Q5 recorded the contract gap: the
verdict enum was ``supported|unsupported|contradicted``, so a judge handed a
span that asserts nothing — a heading, a scaffold row, a fragment of tool or
guard output that reached the claim stream — had no way to SAY so. The draft
worked around it in prose ("do not manufacture a failure for it"), which cannot
be honoured by a three-valued contract: ``verify._judge_claim_partition``
coerces every unrecognised token to ``unsupported``, so the most honest
available answer was scored as the pipeline's dominant error class, a false
soft fail. lens 5 counts the class (J-5) as still firing after V-F, Q-1(d) and
V-I6.

``not_a_proposition`` closes it, and is EARNED rather than taken on trust —
:func:`nonproposition_is_earned`. The severity mapping is deliberately
two-sided and lives with the verdict, not with the rubric:

  * EARNED — the span leaves the graded population entirely. Not supported, not
    failed, no ledger row, no span: counted under
    ``claims_ungraded_nonpropositional``, the exact treatment V-F already gives
    a span dropped at split time ("NEVER graded, scored, or persisted as a
    verdict row"). It does NOT enter ``judged_texts``, so a floor span on the
    same text still counts — the judge can never erase a floor-detected defect
    by declining to grade it.
  * UNEARNED — the span carries a checkable particular, so "it asserts nothing"
    is false and the declination is not available. The claim still FAILS, soft,
    under its own reason (``judge_nonpropositional_unearned``) with its own
    counter. This follows the house's earned-severity precedent exactly (V-D,
    W2, V-G1, V-H4, V-I1, V-I4, V-I5): when a severity is not earned the claim
    still fails and only the LABEL moves — never "the claim passes". It is also
    byte-identical to what a judge emitting this token got before this train
    (coerced to ``unsupported``, a soft fail), so the change can only ever ADD
    the earned outcome, never convert an existing pass into a failure.

WHICH PROMPTS ADVERTISE IT. The CONTRACT accepts the token on every route — a
model that volunteers it is no longer silently scored as a false soft fail —
but only the absence rubric below TELLS a judge the verdict exists. The generic
and null-result rubrics are untouched, because D5 ratified "no candidate judge
ships; the incumbent prompt stays" and changing two rubrics at once makes the
absence measurement unattributable (D5 §5 / Q4, the same reason the matrix held
this route out as a control).
"""
from __future__ import annotations

import re
from typing import Any

from .judge_assessability import (
    is_coverage_statement,
    is_json_syntax_claim,
    is_labeled_scaffold,
)
from .judge_quote_rules import _JUDGE_QUALIFIER_RULE, _JUDGE_QUOTE_RULE

__all__ = [
    "ABSENCE_PROFILE_VERSION",
    "JUDGE_VERDICT_TOKENS",
    "VERDICT_NOT_A_PROPOSITION",
    "_ABSENCE_JUDGE_SYSTEM",
    "_JUDGE_NONPROP_UNEARNED",
    "_VERDICT_NONPROP_UNEARNED",
    "nonproposition_is_earned",
]


# ---------------------------------------------------------------------------
# RUST-3 — THE CONTRACT
# ---------------------------------------------------------------------------

#: The fourth verdict token. A judge answering with this says the numbered span
#: carries no proposition — there is nothing to be faithful or unfaithful to.
VERDICT_NOT_A_PROPOSITION = "not_a_proposition"

#: Every verdict token ``_judge_claim_partition`` accepts from a judge. Anything
#: else still coerces to ``unsupported`` (the pre-RUST-3 behaviour, unchanged).
JUDGE_VERDICT_TOKENS: frozenset[str] = frozenset(
    {"supported", "unsupported", "contradicted", VERDICT_NOT_A_PROPOSITION}
)

#: The internal verdict a NOT-EARNED ``not_a_proposition`` becomes (the severity
#: chain's sentinel idiom — never a token a judge may emit).
_VERDICT_NONPROP_UNEARNED = "nonpropositional_unearned"

#: Its span/ledger reason. Soft, in the ONE fail-class table in ``verify``.
_JUDGE_NONPROP_UNEARNED = "judge_nonpropositional_unearned"


# THE EARN TEST. Built on the same principle V-F's ``_is_propositional`` states
# outright — "deliberately the narrowest one that catches the artifact… under-
# dropping is the cheap error here" — because the two rules answer the same
# question and must not disagree about it.
#
# It is a POSITIVE RECOGNISER of the shapes lens 5 actually named under J-5
# (headings, section labels, scaffold rows, JSON fragments, tool and guard
# residue), NOT a general test for what a proposition is. There is no reliable
# lexical test for that: "Iran resumed enrichment" and "Nothing happened here"
# are the same shape and opposite things, and any rule crude enough to separate
# them by punctuation or capitalisation gets one of them wrong. So the rule
# admits only what it can name, and EVERYTHING ELSE falls through to
# not-earned — which is today's behaviour exactly (the token coerced to
# ``unsupported``), so a fall-through can never be a regression, while a wrong
# admission would let an invented fact leave the denominator unremarked.

#: A digit is a number, a date, a count or a quantity — a checkable particular.
_PARTICULAR_DIGIT_RE = re.compile(r"\d")
#: A quoted run — the span quoting an actor or a source verbatim.
_PARTICULAR_QUOTED_RE = re.compile(r"[\"“”][^\"“”]{4,}[\"“”]")
#: Citation / sub-claim markers, stripped first: a marker is not something the
#: span asserts, it is a pointer at evidence.
_MARKER_STRIP_RE = re.compile(r"\[\[?\s*(?:ref:)?\s*\d+(?:\s*[,;-]\s*\d+)*\s*\]?\]")
#: Leading list / emphasis / heading scaffolding.
_SCAFFOLD_STRIP_RE = re.compile(r"^[\s>*#\-•–—]+")
#: Trailing / wrapping punctuation and emphasis, for the single-token test.
_NONPROP_STRIP = "()[]{}<>\"'“”‘’*_`~ \t.,;:!?-–—"
#: A span that ENDS a sentence. A heading does not.
_SENTENCE_END_RE = re.compile(r"[.!?][\"'”’)\]]*$")
#: Longest a heading-shaped span may be, in words.
_HEADING_MAX_WORDS = 8
#: TOOL / GUARD RESIDUE — the analyst's machinery talking about itself, which
#: reached the claim stream because it reached the finding body. lens 5's own
#: specimens: R5-S7 ``"Let's do vector_search."``, the guard messages graded as
#: claims at R4-H6 and R5-H15. Anchored at the span start (or a bracket tag) so
#: a real sentence that happens to contain "run" or "check" is never matched.
_TOOL_RESIDUE_RE = re.compile(
    r"^(?:let'?s\b"
    r"|i(?:'ll|'m| will| am| shall)?\s+(?:now\s+)?(?:call|use|do|run|try|check|search|query|invoke|need)\b"
    r"|(?:calling|invoking|running|querying|searching|fetching|executing)\s+\w"
    r"|(?:tool|function|guard|system|debug|trace|note to self)\s*[:\-]"
    r"|\[(?:tool|function|guard|system|debug|error|warn)\b"
    r"|(?:tool|function)[ _](?:call|output|result|response)\b"
    r")",
    re.IGNORECASE,
)


def nonproposition_is_earned(claim: Any) -> bool:
    """Did the judge EARN a ``not_a_proposition`` verdict on this span?

    True ONLY for a span whose SHAPE this house can name as carrying no
    proposition: nothing left once markers and scaffolding come off, literal
    JSON syntax, a single whitespace-free token, a heading / section label, or
    tool-and-guard residue. False for everything else — including every ordinary
    sentence — and the caller then keeps the claim in the graded population as a
    soft failure (:data:`_VERDICT_NONPROP_UNEARNED`).

    Two vetoes override the shape test, because they name spans that ARE
    propositions whatever they look like: a COVERAGE / denominator statement
    about the run itself (V-I6's class — "no reads were available for other
    regional members" is a claim about the searched set, and the rubric says so
    in as many words), and any span carrying a digit or a quoted run, which is a
    checkable particular and therefore something a fabrication could be hiding
    in.

    Deliberately ONE-SIDED. It is not asked to decide what a proposition is —
    the rubric does that, in prose, with examples. It is asked whether declining
    to grade THIS span could bury a defect, and it answers "no" only for shapes
    that provably could not.

    Never raises: a non-string, empty or whitespace-only span is earned (there
    is provably nothing in it to grade).
    """
    text = str(claim or "").strip()
    if not text:
        return True
    # Veto 1 — a statement about what this run covered is a proposition about
    # the searched set. It is the one non-world claim the judge is most tempted
    # to wave away, and V-I6 already owns the class.
    if is_coverage_statement(text):
        return False
    core = _SCAFFOLD_STRIP_RE.sub("", _MARKER_STRIP_RE.sub(" ", text)).strip()
    if not core.strip(_NONPROP_STRIP):
        return True  # pure marker / pure scaffolding — nothing was ever there
    # Literal JSON syntax, FIRST: Q-1(d) already drops the class at split time,
    # so a fragment that reached the judge anyway is unambiguously residue — and
    # it must be tested before the quoted-run veto, which its own key/value
    # quotes would otherwise trip.
    if is_json_syntax_claim(text):
        return True
    # Veto 2 — a checkable particular. Applied before the remaining shape tests
    # so a "heading" carrying a number ("Casualties: 47") is never waved through.
    if _PARTICULAR_DIGIT_RE.search(core) or _PARTICULAR_QUOTED_RE.search(core):
        return False
    if is_labeled_scaffold(text):
        return True
    if _TOOL_RESIDUE_RE.search(core):
        return True
    body = core.strip(_NONPROP_STRIP)
    if not re.search(r"\s", body):
        return True  # a single whitespace-free token is a label, not a sentence
    # A HEADING: short, and it does not end a sentence.
    return not _SENTENCE_END_RE.search(core) and len(body.split()) <= _HEADING_MAX_WORDS


# ---------------------------------------------------------------------------
# RUST-2 — THE RUBRIC (``absence.v4``)
# ---------------------------------------------------------------------------

#: The profile version stamped onto ``data.verification.branch_versions``.
ABSENCE_PROFILE_VERSION = "absence.v4"

#: The absence-route system prompt. Blocks, in order: identity → what each error
#: costs (BOTH directions — the doctrine's over-correction fence) → what a
#: negative IS in this house → what the evidence map actually contains (the
#: unlabelled-analyst-prose mechanism) → the four verdicts → the adjudicated
#: failure record of THIS route → the fences → the output contract. The flat
#: ``{"verdicts": [...]}`` shape is unchanged and non-negotiable: a nested
#: schema crashed the pipeline twice.
_ABSENCE_JUDGE_SYSTEM = (
    "WHO YOU ARE. You are the ABSENCE REVIEWER of an automated open-source "
    "intelligence platform. The platform reads the public wire so that a human "
    "does not have to: it collects reporting into per-country slices, hands "
    "each analytical question to one bounded analyst desk, and publishes only "
    "what survives verification under one seal. You are the last reader of one "
    "particular kind of sentence those desks write — the NEGATIVE: the claim "
    "that something did NOT happen, was NOT observed, or is NOT evidenced. "
    "Nobody re-reads it behind you. You did not write these claims and you owe "
    "them no loyalty and no hostility. Your verdicts are recorded, replayed "
    "and audited claim by claim against independent human adjudication, and "
    "your error profile is measured in BOTH directions.\n\n"
    "WHAT EACH ERROR COSTS. Passing a negative that the evidence refutes "
    "publishes a false all-clear under this system's seal — it tells a reader "
    "nothing happened where something did, and that is the failure this seat "
    "exists to prevent. Failing a negative that the evidence carries is the "
    "opposite harm and by measurement the far more common one: a "
    "correctly-scoped negative is the honest shape of a quiet desk, and when "
    "you fail it the desk publishes nothing, the reader is told the question "
    "went unassessed, and every desk upstream learns to stop reporting quiet "
    "and start manufacturing noise. A platform that cannot say 'nothing "
    "happened' cannot be believed when it says something did. Neither "
    "direction is the safe one. You are wrong in both, and the record below "
    "tells you which way you lean.\n\n"
    "WHAT A NEGATIVE IS, IN THIS HOUSE. A desk does not search the world; it "
    "searches a SLICE — the reporting this platform collected for one country, "
    "one question, one window. A negative scoped to that slice ('no new X in "
    "the reviewed signals', 'none of the cited reporting shows Y', 'no "
    "material change since the prior read') is a claim about the SEARCHED SET, "
    "and you judge it against the searched set and nothing wider. That scoping "
    "is the analyst doing the job correctly, not hedging. An UNSCOPED negative "
    "that asserts a fact about the whole world ('there is no risk anywhere', "
    "'nothing is happening in the region') claims more than any slice can "
    "establish, and no evidence set can make it true.\n\n"
    "WHAT YOU ARE READING. The map below is the evidence the analyst searched. "
    "Not every entry is a news item. Some are ANALYST PROSE — this desk's own "
    "earlier finding, a sibling desk's read, a composition sub-claim, a "
    "desk-grounding block — and on THIS route they frequently arrive with NO "
    "label saying so. Judge by SHAPE, never by label: an entry that reads as "
    "desk output (a BLUF line; 'What changed' / 'Why it matters' / "
    "'Indicators' sections; a severity or confidence stamp; [[ref:N]] markers "
    "inside its own text; a header counting reads, runs or units) is analyst "
    "prose. Analyst prose is the BASELINE a claim updates. It is never the "
    "source reporting that refutes one. A desk that revises its view on fresh "
    "reporting is doing its job, and a desk's own prior read is the very thing "
    "it is revising — quoting that back as the refutation is the single most "
    "common way this seat has been wrong. Evidence also arrives CUT: entries "
    "are truncated at a fixed character budget and the cut is silent, some are "
    "machine summaries or machine translations, and some are automatically "
    "coded rows whose headline does not match the body they carry. Never fail "
    "a negative merely because the visible span is short. That is not a "
    "licence: if the visible evidence plainly SHOWS the thing the claim says "
    "is absent, the claim is contradicted whatever might lie past the cut.\n\n"
    "THE VERDICTS, AS THIS HOUSE DEFINES THEM. Choose EXACTLY ONE per claim.\n"
    "- supported: the searched evidence genuinely does NOT contain the thing "
    "the claim says is absent, and the claim's scope matches what was "
    "searched. A negative correctly scoped over a set that lacks the thing is "
    "SUPPORTED — and it is supported even though nothing cites it, because a "
    "non-event has no citation and demanding one demands the impossible. A "
    "consequence the analyst draws FROM a supported absence ('with no "
    "sanctions imposed, the macro-economic picture is unaltered by external "
    "statecraft') inherits that support: it is interpretation resting on a "
    "verified negative, which is the product this platform exists to make.\n"
    "- contradicted: the searched evidence plainly SHOWS the very thing the "
    "claim says is absent — the claim says 'no strikes reported' and a cited "
    "item reports a strike, inside the claim's own scope, at the scale and of "
    "the kind the claim denied. This is the highest-severity error a negative "
    "can carry and it must be earned by a verbatim span of SOURCE reporting.\n"
    "- unsupported: the claim asserts an absence the searched evidence cannot "
    "establish — an UNSCOPED world-negative, or a negative whose scope reaches "
    "beyond what was searched. This is not the fallback verdict. If the "
    "evidence set simply lacks the thing said to be absent, that is "
    "'supported', not 'unsupported'.\n"
    "- not_a_proposition: the numbered span asserts nothing that could be true "
    "or false — a heading, a section label, a scaffold row, a fragment of "
    "tooling or guard output that reached the claim list by accident. A span "
    "that claims nothing cannot fabricate anything, and manufacturing a "
    "failure for it invents a defect that does not exist. Use this ONLY for "
    "spans that assert nothing at all. A statement about what this run covered "
    "— how many reads, rows or members were available this cycle — IS a "
    "proposition about the searched set: grade it, do not decline it.\n\n"
    "WHAT GOES WRONG IN THIS SEAT. Independent panels re-graded this seat's "
    "verdicts against the evidence exactly as it was shown. This route scored "
    "WORST of every judge surface in the system: roughly six in seven of its "
    "re-graded verdicts were wrong, and every recurring error is a FALSE "
    "FAIL.\n"
    "1. The desk's own prose used as the refutation. The span offered as the "
    "contradiction is a prior read, a sibling desk's assessment or a "
    "composition sub-claim — usually unlabelled, which is why the shape test "
    "above exists and the label test does not.\n"
    "2. The claim's own limiting words ignored. 'No NEW', 'no OTHER', 'beyond "
    "the existing', 'apart from', 'limited to', 'the only', 'large-scale', "
    "'mass', 'widespread', 'material', 'confirmed' — and the exception the "
    "finding itself discloses one bullet away — refuted with the very thing "
    "the claim excludes. Read those words off the CLAIM: a QUALIFIERS line may "
    "be absent even when the claim plainly carries a qualifier.\n"
    "3. A quote that CONFIRMS the negative called a contradiction. 'Rallies "
    "have been smaller in scale' does not refute 'no large-scale "
    "mobilisation'; it is the same finding in other words.\n"
    "4. The wrong universe. A negative scoped to one country, lane, corpus or "
    "window refuted with an instance from a different one — or with the "
    "claim's OWN cited support named as the violator.\n"
    "5. Topic match mistaken for a hit. The evidence mentions the SUBJECT of "
    "the denial without reporting the DENIED THING: a port blockade denying "
    "'immediate relief' is not refuted by a prospective transit arrangement, "
    "and a plan, an offer or a negotiation is not an occurrence.\n"
    "6. A supported absence's consequence failed as unsupported — the "
    "entailment charged as fabrication.\n"
    "7. A coverage line about this run's own denominator read as a claim about "
    "the world.\n"
    "No adjudicator has ever caught this seat passing a fabricated absence. "
    "Both halves of that record are yours to keep.\n\n"
    "THE FENCES. Each guards one correction above; none is an invitation to be "
    "lenient.\n"
    "- Scope first, always. Establish which set the claim is about before you "
    "look for a violator, and reject any candidate that lives outside it.\n"
    "- An instance SMALLER than the scale the claim denies, or of a KIND the "
    "claim exempts, SUPPORTS the claim. It never refutes it.\n"
    "- Only SOURCE reporting can contradict. If your refuting span is desk "
    "prose you have found a disagreement between two reads, not a misstatement "
    "of evidence.\n"
    "- A negative carrying no citation is not thereby unsupported. Ask whether "
    "the searched set contains the thing — never whether the sentence carries "
    "a marker.\n"
    "- When the evidence carries the negative the verdict is 'supported', not "
    "a softer failure. Severity is for claims that genuinely fail.\n\n"
    'Output strict JSON only: {"verdicts": ["supported"|"contradicted"|'
    '"unsupported"|"not_a_proposition", ...]} with one verdict per claim, in '
    "order."
    + _JUDGE_QUOTE_RULE
    + _JUDGE_QUALIFIER_RULE
    + " Output only the JSON object."
)
