# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B3 (T1.5, JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §3 P7 / §6) —
the register-nudge clauses in JOURNAL_NARRATE_PREAMBLE (the journal's
"narrating clause" the descriptor's ``prompt_module`` resolves; the
descriptor itself carries no inline prompt text — confirmed via
``scripts/gen_descriptor_prompt_manifest.py``'s own rule, "a prompt_module-
backed unit's prompt IS tracked code," so ``descriptor_prompts.json`` never
mirrors this descriptor's prompt and needs no update here).

A pure content check — no LLM call, no DB. Every clause added is additive;
this also proves no existing clause was dropped.
"""

from __future__ import annotations

from legba.prompts.journal_assessor import JOURNAL_NARRATE_PREAMBLE, JOURNAL_SYSTEM

# The three pre-existing clause anchors (voice-preservation, citation
# discipline, self-instrument marking) — proves nothing was removed.
_PRE_EXISTING_ANCHORS = (
    "FIRST PERSON, as a running notebook",
    "OPENS on the WORLD",
    "[[instrument]] beats a wrong ref",
    "Ground in specifics",
)


def test_register_nudge_keeps_every_existing_clause() -> None:
    for anchor in _PRE_EXISTING_ANCHORS:
        assert anchor in JOURNAL_NARRATE_PREAMBLE


def test_register_nudge_no_evaluative_adjectives_on_outcomes() -> None:
    assert "evaluative adjectives" in JOURNAL_NARRATE_PREAMBLE
    assert '"welcome"' in JOURNAL_NARRATE_PREAMBLE
    assert '"grim,"' in JOURNAL_NARRATE_PREAMBLE


def test_register_nudge_named_war_never_scare_quoted() -> None:
    assert "a named war is named" in JOURNAL_NARRATE_PREAMBLE
    assert '"narrative"' in JOURNAL_NARRATE_PREAMBLE


def test_register_nudge_one_inference_per_signal_marked() -> None:
    assert "one inference per cited signal" in JOURNAL_NARRATE_PREAMBLE
    assert "[[inference]]" in JOURNAL_NARRATE_PREAMBLE


def test_register_nudge_reaches_the_composed_system_prompt() -> None:
    # JOURNAL_SYSTEM is the persona + MAP + this preamble, joined — the
    # descriptor's run_method threads JOURNAL_SYSTEM on every LLM call.
    assert "one inference per cited signal" in JOURNAL_SYSTEM
