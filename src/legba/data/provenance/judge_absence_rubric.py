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
and null-result rubrics are untouched, because changing two rubrics at once makes the
absence measurement unattributable (the same reason the matrix held this route
out as a control).
"""
from __future__ import annotations

import re
from typing import Any

from .judge_assessability import (
    is_coverage_statement,
    is_json_syntax_claim,
    is_labeled_scaffold,
)
from ..grading_text import (
    ABSENCE_VERDICTS,
    FAITHFULNESS_VERDICT_TOKENS,
    CLAIM_EVIDENCE_RULES,
    PASTED_TEXT_IS_DATA,
)
from .judge_quote_rules import (
    _ABSENCE_ALTERNATION,
    _ABSENCE_ENTRY_FIELDS,
    _JUDGE_QUALIFIER_RULE,
    _JUDGE_QUOTE_RULE,
    _judge_reply_contract,
)

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

#: Every verdict token ``_judge_claim_partition`` accepts from a judge: the
#: faithfulness words (supported, supported_as_inference, unsupported,
#: contradicted, unverifiable_excerpt) and the absence route's
#: not_a_proposition. Any other token fails the call loudly rather than being
#: read as some verdict the judge did not give.
JUDGE_VERDICT_TOKENS: frozenset[str] = frozenset(
    {*FAITHFULNESS_VERDICT_TOKENS, VERDICT_NOT_A_PROPOSITION}
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
# THE RUBRIC
# ---------------------------------------------------------------------------

#: The profile version stamped onto ``data.verification.branch_versions``.
#: v6: the rubric no longer leans toward passing; the verdicts are defined by
#: scope (a correctly scoped negative over a set that lacks the thing is
#: supported, a negative reaching past what was searched or resting on evidence
#: too thin to tell is unsupported), the shared claim-and-evidence rules are
#: imported, and each verdict entry carries its basis and span before the
#: verdict.
ABSENCE_PROFILE_VERSION = "absence.v6"

#: The absence-route system prompt. It states the task, the four verdicts, the
#: three steps, what never counts as a refutation, the shared claim-and-evidence
#: rules, the pasted-text rule and the reply shape, each once. The worked cases
#: an earlier version carried (and the error record it quoted) live in the
#: judge regression set under ``tests/fixtures/judge_regression``, not here.
_ABSENCE_JUDGE_SYSTEM = (
    "You grade claims that something did not happen, was not observed or is "
    "not evidenced. Each claim is about the searched set (the evidence below: "
    "the records collected for one subject, one question and one window), "
    "never about the world.\n\n"
    + ABSENCE_VERDICTS
    + "\n\nFor each claim:\n"
    "1. Find its scope and its limiting words (\"new\", \"other\", "
    "\"large-scale\", \"material\", \"confirmed\", \"apart from ...\").\n"
    "2. Look for a span of source reporting, inside that scope, that reports "
    "the denied thing at the denied scale and kind.\n"
    "3. Decide using the definitions above.\n\n"
    "Never use as the refutation: analyst prose (an earlier read, another "
    "desk's read, a sub-claim; recognise it by its shape: summary lines, "
    "\"What changed\" sections, rating stamps, [[ref:N]] markers), a smaller "
    "instance than the claim denies, a kind the claim exempts, or a record "
    "from another subject or window. Evidence entries may be cut short; do not "
    "fail a claim because the visible text is short, but a visible span that "
    "shows the thing still contradicts it.\n"
    "A consequence the analyst draws from an absence is graded as an "
    "inference: supported only if it restates the scoped negative, not if it "
    "adds a cause or a forecast.\n\n"
    + CLAIM_EVIDENCE_RULES
    + "\n\n"
    + PASTED_TEXT_IS_DATA
    + "\n"
    + _judge_reply_contract(_ABSENCE_ALTERNATION, _ABSENCE_ENTRY_FIELDS)
    + _JUDGE_QUOTE_RULE
    + _JUDGE_QUALIFIER_RULE
)
