# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The UNIT GROUNDING prompt contract — one clause, appended to every unit.

Extracted from :mod:`legba.data.analysts.unit_grounding` (7g-2), verbatim,
when the HISTORICAL SERIES block pushed that module at the module-size gate's
1,500-line entry threshold. The gate's own instruction is to extract a
cohesive unit rather than pin a new ceiling, and the prompt contract is
exactly that: three generated clause constants, their fingerprint and the
idempotent append, depending on nothing in ``unit_grounding`` at all — only
on ``._tradecraft`` and ``.window_ledger``, which is why it is the clean cut.

``unit_grounding`` imports these names back ONE WAY and re-exports them, so
``from .unit_grounding import UNIT_GROUNDING_CLAUSE, with_grounding_clause``
and every existing caller and test is unchanged.
"""

from __future__ import annotations

from ._tradecraft import RETRIEVED_CONTEXT_RULE, as_of_rule
from .window_ledger import window_ledger_rule

__all__ = [
    "UNIT_GROUNDING_CLAUSE",
    "with_grounding_clause",
]



# ---------------------------------------------------------------------------
# The prompt contract — ONE clause, appended to every unit system prompt
# ---------------------------------------------------------------------------
#
# Generated as a single constant rather than pasted into nine descriptors: a copy
# per unit would drift the moment one is edited, and the whole point of a
# grounding contract is that every unit states it identically. It encodes exactly
# the four obligations the composition clause encodes, in the order a reader needs
# them — SAY WHAT CHANGED, ANCHOR ON THE BLOCK'S OWN DATES, NO-CHANGE IS AN
# ANSWER, and NEVER assert continuity that is not grounded in a shown block —
# plus the two obligations the extra unit blocks create: argue against the
# baseline number instead of a vibe, and treat a standing question as standing
# unless this run's own evidence answers it.
#
# PHASE-V D1 — the AS-OF clause rides in FRONT of it, on the same append. This
# is the right seam for two reasons. (a) The clause below is where the temporal
# discipline already lives, and it is exactly the clause the diagnostic found
# causing the damage: it forbids 'today'/'now'/'as of this run' (correctly — it
# is the temporal-collapse guard) and supplies NO replacement, so the model
# resolves the conflict by dropping temporal reference altogether. Stating the
# prohibition and the replacement in ONE breath is what makes the replacement
# reachable. (b) ``with_grounding_clause`` is the single unconditional append
# every inline_target analyst passes through, so the as-of obligation reaches
# the four NON-unit inline_target analysts too — which is correct: an undated
# read is no better from country_assessor than from escalation.

#: D1 anchored for the UNIT layer. The three values come off the SLICE HEADER
#: ``inline_target._render_user_prompt`` stamps at the top of the evidence —
#: rendered text, not run state, so an as-of line is a COPY and never an
#: assertion the judge cannot check.
_AS_OF_CLAUSE: str = as_of_rule(
    "'*As of <run date>; slice covers the trailing <window> to that date; <N> "
    "signals.*'. Take all three values VERBATIM from the SLICE HEADER at the "
    "top of the evidence (its 'Run date (as-of)', 'Slice window' and 'Number "
    "of signals' lines); when an AUTHORITATIVE CURRENT CONTEXT block is also "
    "shown, its 'as of' date is the same date and is your ground truth for "
    "what 'current' means. Never substitute your own sense of the time. If NO "
    "slice header is shown — a run that gathers its own evidence rather than "
    "reading a cadence slice — take the date from the AUTHORITATIVE CURRENT "
    "CONTEXT block instead and name the evidence you gathered in place of a "
    "window; if neither is shown, OMIT the as-of line rather than inventing a "
    "date. An absent anchor is an honest absence; a guessed one is a "
    "fabrication."
)

#: V-N2 — the RETRIEVED-CONTEXT rule rides the SAME append, immediately after
#: the as-of rule it completes. This is the right seam for the same reason D1
#: chose it: ``with_grounding_clause`` is the ONE unconditional append every
#: inline_target analyst passes through, and the four GATHERING analysts
#: (cross_doc_corroborator, corpus_researcher, country_assessor and the unit
#: disruption_status) are exactly the ones that run a GATHER loop and therefore
#: the only ones that can be shown a RETRIEVED block at all. Gathering is a
#: separate axis from unit-hood — DS-1 — and disruption_status is on both.
#:
#: Appended unconditionally rather than only when a gathered block is present,
#: and that is deliberate: the GATHER phase and the SYNTHESIS phase are separate
#: LLM calls, and the synthesis call is where the finding gets written. Making
#: the clause conditional on this run having gathered would mean deciding, at
#: prompt-assembly time, something only the tool loop knows — and would leave
#: the rule unstated on precisely the runs that retrieved something.
#:
#: A unit that never gathers reads one paragraph about blocks it will not see.
#: That is the same cost the "no DESK GROUNDING shown ⇒ this is a FIRST read"
#: leg already pays, for the same reason: one definition, no drift.
_RETRIEVED_CLAUSE: str = RETRIEVED_CONTEXT_RULE

UNIT_GROUNDING_CLAUSE: str = (
    _AS_OF_CLAUSE
    + "\n\n"
    + _RETRIEVED_CLAUSE
    + "\n\n"
    + "DESK GROUNDING (what this desk already knew). AFTER the numbered signals you "
    "may be shown a DESK GROUNDING section carrying up to five blocks, each with "
    "its own [N] handle in the SAME numbering as the signals: a PRIOR READ (this "
    "unit's own previous verified read of this target, with its produced_at and "
    "age), a WINDOW LEDGER (this unit's own dated, verified reads of the trailing "
    "14 days — see the WINDOW LEDGER rules below), an OPEN SITUATION REGISTER "
    "(the desk's open frames with their status, "
    "intensity, event count and last_event_at), a DESK BASELINE (the trailing "
    "normal band for this desk with the current observed value), and STANDING "
    "OPEN QUESTIONS (questions this desk raised that nobody has answered). When a "
    "block is shown you MUST: (1) state EXPLICITLY what CHANGED versus the cited "
    "PRIOR READ — name the change and cite the block by its [N] handle exactly "
    "like a signal; (2) anchor EVERY temporal statement on the dates printed IN "
    "those blocks (the prior read's produced_at, a situation's last_event_at, the "
    "baseline's computed_at, a question's asked_at) and on the SLICE HEADER's "
    "run date — NEVER on 'today', 'now', 'as of this run', or the time you are "
    "running; (3) if nothing material "
    "changed, SAY SO plainly and briefly (e.g. 'no material change since the "
    "3 August morning read [N]' — a HUMAN calendar date taken from the block's "
    "produced_at, never the raw ISO/microsecond timestamp) rather than "
    "re-deriving the same "
    "picture in different words. When the block you are citing IS the PRIOR "
    "READ, you may write '(prior read ref N)' in place of the bare '[N]' — it "
    "resolves identically, and it lets a reader of the finished prose see that "
    "you cited your own previous read rather than a signal; "
    "(4) describe a situation ONLY as the register "
    "states it — its own name and status — and never "
    "upgrade, downgrade, or re-date it beyond what the register shows. The "
    "register's intensity score and event_count are internal instrument "
    "readings: USE them to decide, never PRINT them; (5) when "
    "you call this window unusual (or normal), say so AGAINST the DESK BASELINE "
    "band and cite it — do not assert 'elevated' or 'a spike' when the baseline "
    "block shows the current value inside its normal band, and never restate the "
    "baseline as a forecast; (6) treat every STANDING OPEN QUESTION as still "
    "open unless THIS run's cited evidence answers it — if it does, say which "
    "question and cite the signal that answers it; if it does not, do not "
    "re-ask the same question as if it were new. NEVER assert continuity of ANY "
    "kind — an escalation, a de-escalation, a trend, an 'ongoing'/'longstanding' "
    "framing, or that something has 'been building' — unless it is grounded in a "
    "cited DESK GROUNDING block. If NO DESK GROUNDING section is shown this is a "
    "FIRST read of this target: make NO claim about what came before and use no "
    "'ongoing' / 'continuing' / 'still' framing."
    # FRAME-2 — the ledger's own three rules, generated by the SAME function the
    # composition clause calls (``window_ledger_rule``) so both layers state the
    # contract identically and neither can drift on an edit. Appended AFTER the
    # six numbered obligations rather than folded into them: those govern a
    # SINGLE-STEP diff against last cycle, these govern the FORTNIGHT, and the
    # rule the round actually needs — "never write 'not observed' about a window
    # your own ledger contradicts" — deserves to be readable on its own.
    + "\n\n"
    + window_ledger_rule("[N]")
)

_CLAUSE_FINGERPRINT = "DESK GROUNDING (what this desk already knew)."


def with_grounding_clause(system_prompt: str) -> str:
    """Append the grounding clause to a unit system prompt, exactly once.

    Idempotent by fingerprint so a re-resolution (or a GEPA-promoted candidate
    that already carries the clause) can never double it — the same posture
    ``_tradecraft.with_preamble_if_absent`` takes for the house preamble.

    The clause is appended in CODE rather than pasted into nine descriptors: one
    definition, nine units, no drift. It is appended UNCONDITIONALLY — the
    "no blocks shown ⇒ this is a first read, claim nothing about before" leg is
    exactly the obligation a unit with no memory needs, and making the clause
    conditional on the blocks resolving would leave that leg unstated on the one
    run where it bites hardest.
    """
    if not system_prompt:
        return system_prompt
    if _CLAUSE_FINGERPRINT in system_prompt:
        return system_prompt
    return f"{system_prompt.rstrip()}\n\n{UNIT_GROUNDING_CLAUSE}\n"


# ---------------------------------------------------------------------------
# Small coercions — each returns None rather than a fabricated zero/date
# ---------------------------------------------------------------------------
