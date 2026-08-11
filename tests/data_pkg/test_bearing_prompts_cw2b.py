# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CW-2b — the consequence-specificity prompt line (K-4 R4 §11.3), pinned.

R4's F1 class: 3 of the round's 6 residual failures were the same shape —
"the question names a country's imports and the signal never mentions that
country's imports." The report's remedy is ONE line added to both bearing
prompts, verbatim: "if the thesis names a specific consequence for a specific
actor, the signal must speak to *that* consequence, not only to the upstream
event" (emphasis rendered in the prompts' own caps convention). Per the
replay's rule, a prompt change is measured by the NEXT K-4 round, never by
replay — these pins only hold the strings and their version stamps to what
this commit shipped, so a drift is a deliberate edit with a new version.

NEW FILE deliberately: the sibling claim_watch suites are under concurrent
test-hygiene work.
"""
from __future__ import annotations

from legba.data.analysts.deterministic_handlers import bearing_gate as bg
from legba.data.analysts.deterministic_handlers import claim_watch as cw

#: The R4 §11.3 line, in the prompts' caps-for-emphasis convention. A LITERAL
#: here — the prompt-version contract says an edit must be a deliberate act.
_CW2B_LINE = (
    "If the thesis names a specific consequence for a specific actor, the "
    "signal must speak to THAT consequence, not only to the upstream event."
)


def test_the_gate_system_prompt_carries_the_cw2b_line():
    assert _CW2B_LINE in bg.GATE_SYSTEM_PROMPT


def test_the_confirm_prompt_carries_the_cw2b_line():
    # The template's backslash line-continuations resolve at compile time,
    # so the pin holds on the exact text the model is sent.
    assert _CW2B_LINE in bg.CONFIRM_PROMPT


def test_both_prompt_versions_bumped_with_the_prompt():
    """The stamps ride every edge and are the only thing that tells an edge
    judged by one prompt apart from an edge judged by another — bump WITH the
    prompt, in the same commit, pinned to literals here for the same reason
    the matcher version is."""
    assert bg.GATE_PROMPT_VERSION == "fewshot+desk+consequence/3"
    assert bg.CONFIRM_PROMPT_VERSION == "desk+consequence/3"


def test_the_measured_exemplars_are_untouched():
    """The eight few-shot turns ARE the 0.842 measurement (seven) plus the
    CW-2 worked failure (the eighth); CW-2b is a system-message clause ONLY.
    Editing an exemplar would silently invalidate the measurement."""
    assert len(bg.GATE_FEWSHOT_TURNS) == 8
    assert all(a in ("YES", "NO") for _, a in bg.GATE_FEWSHOT_TURNS)
    # The CW-2 eighth turn still carries its desk line and its NO.
    eighth_user, eighth_answer = bg.GATE_FEWSHOT_TURNS[-1]
    assert eighth_user.startswith("Desk: G20 — Türkiye [country_g20_tr]")
    assert eighth_answer == "NO"
    # And no exemplar was reworded to carry the CW-2b line.
    assert all(_CW2B_LINE not in user for user, _ in bg.GATE_FEWSHOT_TURNS)


def test_matcher_version_bumped_for_the_r4_followups():
    """A 4.1.0 row survived prompts a 4.0.0 row never faced — the population-
    meaning argument that bumped 3.3.0. K-4 R6 cuts on this stamp."""
    assert cw.MATCHER_VERSION == "claim_watch/4.1.0"
