# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The faithfulness judge's prompt texts: system prompts and message leads.

``verify`` assembles each judge call from these pieces and imports them one
way. They live here rather than in ``verify`` so the prompt text can be read
in one place, apart from the scoring code around it.

The judges do not lean either way. Each verdict is defined by what the
evidence shows (see :mod:`legba.data.grading_text`): a claim passes when the
evidence states what it asserts, an inference passes when the facts it rests
on do, an excerpt that cannot settle a claim says so with its own verdict, and
a summary line with no citation must restate the cited claims at the same
strength.

Pieces:

* :data:`GENERIC_JUDGE_SYSTEM` — the system prompt for every claim that is not
  an absence claim (unit and composition findings alike).
* :data:`INDEPENDENT_JUDGE_SYSTEM` — the same prompt with one sentence saying
  the claims were written by someone else, kept for the prompt-profile switch.
* :data:`UNIT_LEAD` / :data:`COMPOSITION_LEAD` — the first part of the user
  message: how to read the evidence map of each kind.
* :data:`SURVEY_JUDGE_SYSTEM` / :data:`SURVEY_PROMPT_PREFIX` — the whole-finding
  grade for a finding whose core claim is that something was not observed.
"""

from __future__ import annotations

from ..grading_text import (
    CLAIM_EVIDENCE_BLOCK,
    CLAIM_EVIDENCE_RULES,
    FAITHFULNESS_VERDICT_TOKENS,
    PASTED_TEXT_IS_DATA,
)

#: The reply contract's verdict alternation for the faithfulness routes.
FAITHFULNESS_VERDICT_ALTERNATION = "|".join(
    f'"{token}"' for token in FAITHFULNESS_VERDICT_TOKENS
)

#: The survey route grades with three verdicts: a whole-finding negative is
#: either a faithful account of the listed evidence, an overreach, or refuted.
SURVEY_VERDICT_ALTERNATION = '"supported"|"unsupported"|"contradicted"'

GENERIC_JUDGE_SYSTEM = (
    "You check each numbered claim against only the evidence shown with it. "
    "Do not use what you know about the world, for or against, and do not "
    "assume the writer must have had a reason for a claim the evidence does "
    "not carry.\n"
    + CLAIM_EVIDENCE_BLOCK
    + "\n"
    + PASTED_TEXT_IS_DATA
    + "\nReply with the JSON object described in the message and nothing else."
)

INDEPENDENT_JUDGE_SYSTEM = (
    "The claims below were written by another analyst.\n" + GENERIC_JUDGE_SYSTEM
)

UNIT_LEAD = (
    "For each numbered claim, decide whether the evidence for its [N] markers "
    "supports it. An evidence entry may carry: OUTLET (who published; use it "
    "for claims about who reported), SOURCE (the article text) and Analyst "
    "summary (what the analyst worked from; it is not evidence and never "
    "supplies missing text).\n"
    "- SOURCE (authoritative) is the complete article. A specific the claim "
    "asserts that is not in it is unsupported.\n"
    "- SOURCE (authoritative excerpt) is the start of a longer article. If the "
    "claim's specifics are not in the excerpt, the verdict is "
    "unverifiable_excerpt; if the excerpt states the opposite, contradicted.\n"
    "- A claim with no [N] marker (a summary line, a lead sentence, a rating) "
    "must restate marked claims of this finding at the same strength. If it "
    "adds anything, or states it more firmly than they do, it is unsupported."
)

COMPOSITION_LEAD = (
    "For each numbered claim, decide whether the cited sub-claims support it "
    "(the N -> sub-claim map below). A claim citing [[ref:N]] must follow from "
    "that sub-claim at no greater strength than the sub-claim states. A claim "
    "with no marker (a summary line, a lead sentence, a rating) must restate "
    "marked claims at the same strength; if it adds anything or states it more "
    "firmly, it is unsupported. The rubric below lists the shapes to check."
)

#: The survey route's opening sentence, shared by its system prompt and the
#: head of its user message.
_SURVEY_FIRST_SENTENCE = (
    "You grade a finding whose core claim is that something was not observed, "
    "together with a description of what the listed signals do contain. Grade "
    "it as a survey of the listed evidence, not clause by clause."
)

SURVEY_JUDGE_SYSTEM = (
    _SURVEY_FIRST_SENTENCE
    + "\nAn absence is supported when the finding names the scope it searched "
    '("in the 40 signals reviewed") and the listed evidence does not contain '
    "the thing. It is unsupported if it claims more than that scope, and "
    "contradicted if the evidence contains the thing at the scale and of the "
    "kind denied (quote it).\n"
    "A description of what the signals contain is supported only if you can "
    "check it against the listed items. If the list is a sample, do not "
    "support claims about the whole.\n"
    + CLAIM_EVIDENCE_RULES
    + "\n"
    + PASTED_TEXT_IS_DATA
    + "\nReply with the JSON object described in the message and nothing else."
)

SURVEY_PROMPT_PREFIX = _SURVEY_FIRST_SENTENCE + "\n"

__all__ = [
    "COMPOSITION_LEAD",
    "FAITHFULNESS_VERDICT_ALTERNATION",
    "GENERIC_JUDGE_SYSTEM",
    "INDEPENDENT_JUDGE_SYSTEM",
    "SURVEY_JUDGE_SYSTEM",
    "SURVEY_PROMPT_PREFIX",
    "SURVEY_VERDICT_ALTERNATION",
    "UNIT_LEAD",
]
