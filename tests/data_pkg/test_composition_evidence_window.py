# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""F-D (2026-08-03) — the composition tier read its inputs through a keyhole.

Two halves of one defect, in one module because they are one module and one
deploy.

**THE INPUT WINDOW.** ``LEGBA_LLM_INPUT_TOKEN_BUDGET`` has bounded the UNIT
path's signal slice since the LLM-planes work — 32,000 estimated input tokens by
default. ``meta_findings_synthesizer`` referenced it NOWHERE: it packed against
``MAX_INPUT_FINDINGS`` x ``MAX_BODY_CHARS`` = 15 x 600 chars, roughly 2,250
estimated tokens, about **7%** of what every leaf desk gets. The tier whose whole
job is seeing across desks saw less of its inputs than any leaf saw of its
signals.

**THE EVIDENCE WINDOW.** The citation each composed clause is graded against
captured 600 chars of the cited sub-claim's body. Measured read-only on the live
substrate for the stamped day: country_composition citations averaged **567 of
600** chars against cited bodies averaging **2,352** — i.e. the cap BOUND on
essentially every citation in production, and the judge graded the whole tower
against roughly the first quarter of each sub-claim. A composed clause resting on
anything the cited finding said after its BLUF read as ungrounded.

(The 08-03 panel reported this as "0 of 551 composition citations carry resolvable
text". That measurement read ``source_text``/``snippet``/``body`` — the UNIT
citation shape. Composition citations carry ``evidence_text``, and 542 of 542 had
it. The defect was never absence; it was the width of the window. Recorded here
because the next panel will re-measure and should not re-derive the wrong
conclusion from the same query.)
"""

from __future__ import annotations

from uuid import uuid4

import legba.data.analysts.meta_findings_synthesizer as M
from legba.data.analysts._llm_budget import (
    CHARS_PER_TOKEN,
    DEFAULT_INPUT_TOKEN_BUDGET,
    LLM_INPUT_TOKEN_BUDGET_ENV,
    budget_chars,
    estimate_tokens,
    input_token_budget,
)
from legba.data.provenance import verify as V

# ---------------------------------------------------------------------------
# The shared budget — ONE definition, both producers
# ---------------------------------------------------------------------------


def test_the_unit_path_and_the_composition_path_read_the_same_budget(
    monkeypatch,
) -> None:
    """A copied constant drifts; a shared one cannot."""
    import legba.data.analysts.inline_target as I

    monkeypatch.setenv(LLM_INPUT_TOKEN_BUDGET_ENV, "12345")
    assert I._input_token_budget() == 12345
    assert input_token_budget() == 12345
    assert budget_chars() == 12345 * CHARS_PER_TOKEN
    assert I._estimate_tokens("abcd" * 10) == estimate_tokens("abcd" * 10)
    assert M._PROMPT_CHARS_PER_TOKEN == CHARS_PER_TOKEN


def test_a_malformed_budget_env_falls_back_rather_than_raising(monkeypatch) -> None:
    """A bad env must not take an analyst down."""
    for bad in ("", "   ", "not-a-number", "-5", "0"):
        monkeypatch.setenv(LLM_INPUT_TOKEN_BUDGET_ENV, bad)
        got = input_token_budget()
        assert got >= 1
        if bad in ("", "   ", "not-a-number"):
            assert got == DEFAULT_INPUT_TOKEN_BUDGET


# ---------------------------------------------------------------------------
# The per-input body excerpt is now sized from that budget
# ---------------------------------------------------------------------------


def test_the_body_excerpt_is_sized_from_the_budget_not_a_fixed_constant(
    monkeypatch,
) -> None:
    monkeypatch.setenv(LLM_INPUT_TOKEN_BUDGET_ENV, str(DEFAULT_INPUT_TOKEN_BUDGET))
    # A per-country read fuses ~7 unit heads: every one renders WHOLE.
    assert M.composition_body_cap(7) == M.MAX_FULL_BODY_CHARS
    # The world read's degrade path (a headless region falling back to member
    # country heads) still clears the old ceiling by a wide margin.
    assert M.composition_body_cap(64) > M.MAX_BODY_CHARS
    # …and it is monotonic in the input count, because the budget is shared out.
    assert M.composition_body_cap(64) <= M.composition_body_cap(15)


def test_the_historical_excerpt_is_a_floor_that_cannot_be_undercut(
    monkeypatch,
) -> None:
    """However small the budget, no render narrows below what shipped before.

    Nothing is ever DROPPED for budget either: the slice is already count-capped
    upstream, and a dropped input on the world path is a country the world read
    cannot see — a worse failure than a wide turn.
    """
    monkeypatch.setenv(LLM_INPUT_TOKEN_BUDGET_ENV, "100")
    assert M.composition_body_cap(64) == M.MAX_BODY_CHARS
    assert M.composition_body_cap(1) >= M.MAX_BODY_CHARS


def test_a_long_input_body_now_reaches_the_prompt_whole(monkeypatch) -> None:
    monkeypatch.setenv(LLM_INPUT_TOKEN_BUDGET_ENV, str(DEFAULT_INPUT_TOKEN_BUDGET))
    tail = "The interdiction risk shifted to DEGRADING on four fresh signals."
    body = ("Filler sentence about the desk's window. " * 40) + tail
    assert len(body) > M.MAX_BODY_CHARS
    rendered = M._render_user_prompt(
        [
            {
                "id": uuid4(),
                "title": "Hormuz Strait – Interdiction risk",
                "analyst_id": "disruption_status",
                "confidence": 0.8,
                "body": body,
            }
        ],
        ["disruption_status"],
        include_source_ids=True,
    )
    assert tail in rendered, "the verdict sat past the old 600-char cut"


def test_the_findings_block_stays_inside_its_share_of_the_budget(
    monkeypatch,
) -> None:
    """Wider is the point; unbounded is not."""
    monkeypatch.setenv(LLM_INPUT_TOKEN_BUDGET_ENV, str(DEFAULT_INPUT_TOKEN_BUDGET))
    rows = [
        {
            "id": uuid4(),
            "title": f"desk {i}",
            "analyst_id": f"unit_{i}",
            "confidence": 0.7,
            "body": "x" * 20_000,
        }
        for i in range(M.MAX_INPUT_FINDINGS)
    ]
    rendered = M._render_user_prompt(
        rows, [r["analyst_id"] for r in rows], include_source_ids=True
    )
    ceiling = int(
        DEFAULT_INPUT_TOKEN_BUDGET * M.COMPOSITION_SLICE_BUDGET_SHARE * 1.25
    )
    assert estimate_tokens(rendered) <= ceiling


# ---------------------------------------------------------------------------
# The EVIDENCE window the judge grades the tower against
# ---------------------------------------------------------------------------


def test_the_composition_citation_carries_the_unit_evidence_window() -> None:
    """The composed clause is graded against the same width a unit clause is."""
    assert M.MAX_EVIDENCE_TEXT_CHARS == V._EVIDENCE_TOTAL_CHARS


def test_a_cited_sub_claims_body_survives_past_its_bluf(monkeypatch) -> None:
    """The measured shape: bodies average ~2,352 chars against a 600-char cap."""
    bluf = "BLUF: energy-security pressure on Russia has risen sharply.\n"
    tail = "Refinery throughput fell 11% week-on-week after the Ryazan strike."
    body = bluf + ("Supporting paragraph carrying the detail. " * 45) + tail
    assert 600 < len(body) < M.MAX_EVIDENCE_TEXT_CHARS

    citation = M._build_composition_citation(
        3, {"id": uuid4(), "title": "Russia – energy security", "body": body}
    )
    assert citation is not None
    assert tail in citation["evidence_text"], "the judge could not see this before"

    # …and it resolves through the verify path's own evidence map, which is what
    # the judge is actually handed.
    evidence = V._ordinal_evidence_map([citation])
    assert tail in evidence[3]


def test_the_evidence_capture_is_still_bounded() -> None:
    citation = M._build_composition_citation(
        1, {"id": uuid4(), "title": "runaway", "body": "y" * 50_000}
    )
    assert citation is not None
    assert len(citation["evidence_text"]) == M.MAX_EVIDENCE_TEXT_CHARS
