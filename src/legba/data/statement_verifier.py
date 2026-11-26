# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The VERIFIER seat — gate G4, the clean-context support check.

CODE FIRST. Before any model sees a statement, code runs the checks it can:
every evidence quote is in the stored text it cites (G1), every number and
date the statement states is in a quote (G2), and at least one evidence row
exists (G8). A statement that fails any of these is ``does_not_support``,
``decided_by="code"``, and no model call is spent on it. The fabricated claim
of the project's history stops here, before any model judged it.

THEN A CLEAN CONTEXT. The model sees the statement (its text, kind and valid
time) and the quoted support — nothing else: not the desk's prose, not its
reasoning, not the source titles, not the typing seat's reply. It answers one
of three verdicts with a closed reason code. Only ``supports`` establishes; a
reply that cannot be read is ``unclear`` and establishes nothing.

S-5 — ONE LABELLED FACT ABOUT EACH QUOTE. Each quote is shown with its row's
PUBLICATION DATE as metadata, so a quote's "on Saturday" or "today" is read
against the same day the gate's date rule reads it against
(``statement_dates`` rule (a)) and the two cannot disagree. Titles, sources and
the desk's prose stay hidden.

THE QUOTE, NOT ITS SENTENCE. Code also records each quote's whole sentence
(``SupportRow.quote_context``), and ``show_context=True`` shows the verifier
that instead of the quote. It is OFF by default because it was measured to
loosen the check (S-5, 66 planted items): the surrounding sentence can carry
a frame ("officials confirmed … that X claimed …") that talks the verifier out
of an attribution the quote alone makes plain; the planted catch rate fell
below the 0.90 floor with it and stayed above without it. A short quote is
fixed by the SEAT quoting the whole sentence (G1 checks it verbatim), not by
code widening what the verifier reads.

NOT FOR JUDGEMENTS, ABSENCES OR COVERAGE. An ``assessment`` (a desk
judgement), an ``absence`` or a ``coverage`` statement (both about the
collection, S-6) is never established by a quote: the seat returns
``not_applicable`` decided by code, with no call.

MEASURED, BECAUSE A JUDGE THAT CANNOT FAIL HAS BEEN TRUSTED BEFORE. The seat's
catch rate on planted wrong statements (:mod:`legba.data.statement_planted`)
is published beside every figure it feeds, with its sample size; below the
floor, establishment pauses (candidates accumulate; nothing is lost).

``code_first=False`` sends a statement to the model even when code would have
refused it — the measurement mode, so the MODEL's own catch rate can be
reported apart from the seat's end-to-end rate. The code verdict is computed
and returned either way.

No I/O beyond the injected ``llm``; nothing is written.
"""
from __future__ import annotations

import json

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .statement_content import strip_markers
from .statement_gate import check_dates, check_numbers, check_quote, check_statement_shape
from .statement_seats import (
    VERIFIER_SEAT_ID,
    CallBudget,
    SeatCall,
    SeatLLM,
    as_text,
    call_seat,
    first_of,
    parse_reply_object,
)
from .statement_shapes import (
    CheckResult,
    EvidenceTexts,
    StatementLike,
    SupportLike,
    SupportRole,
    enum_value,
)

SUPPORTS = "supports"
PARTLY = "partly"
DOES_NOT_SUPPORT = "does_not_support"
#: A reply the seat could not read, or no call made. Establishes nothing.
UNCLEAR = "unclear"
#: An assessment or absence: not a quoted report of the world (S-5).
NOT_APPLICABLE = "not_applicable"
_NOT_QUOTED_KINDS = frozenset({"assessment", "absence", "coverage"})
VERDICTS = frozenset({SUPPORTS, PARTLY, DOES_NOT_SUPPORT})

#: The closed reason codes the model may give (anything else → ``other``).
REASON_CODES = frozenset({
    "number_mismatch", "date_mismatch", "subject_mismatch", "not_stated",
    "beyond_quote", "attribution_dropped", "fully_stated", "other",
})


@dataclass(frozen=True)
class VerifierReport:
    """One verdict. ``establishes`` is the only thing the lifecycle reads."""

    verdict: str
    decided_by: str                       # code | model | unparsed | no_call
    reason_code: str | None
    code_failures: tuple[str, ...]
    call: SeatCall | None = None
    seat: str = VERIFIER_SEAT_ID

    @property
    def establishes(self) -> bool:
        return self.verdict == SUPPORTS and self.decided_by == "model" and not self.code_failures

    def as_dict(self) -> dict[str, Any]:
        """The ``statement_support.verifier`` jsonb (no model text)."""
        out: dict[str, Any] = {"seat": self.seat, "verdict": self.verdict,
                               "decided_by": self.decided_by, "reason_code": self.reason_code,
                               "code_failures": list(self.code_failures)}
        if self.call is not None:
            out["model"] = self.call.model
        return out


def _evidence(support: Sequence[SupportLike]) -> list[SupportLike]:
    return [r for r in support if enum_value(r.role) == SupportRole.EVIDENCE.value]


def code_precheck(
    statement: StatementLike, support: Sequence[SupportLike], texts: EvidenceTexts,
    *, read_at: datetime | None = None,
) -> list[CheckResult]:
    """The checks code runs before the model: G8 evidence present, G1 per
    evidence row, G2 numbers and dates — the dates under the ONE date rule
    the gate applies (``statement_dates``; S-4), so a draft the gate passed
    on its row's publication day is not refused here by a stricter copy.
    ``read_at`` resolves the statement's own relative words. Returns every
    check; failures are those not passed."""
    out = [c for c in check_statement_shape(statement, support) if c.check == "evidence_present"]
    for i, row in enumerate(support):
        if enum_value(row.role) == SupportRole.EVIDENCE.value:
            out.append(check_quote(row, texts, row_index=i))
    out.append(check_numbers(statement, support, texts))
    out.append(check_dates(statement, support, read_at=read_at))
    return out


def _valid_label(statement: StatementLike) -> str:
    vf = statement.valid_from
    precision = enum_value(statement.valid_from_precision) or "unknown"
    if vf is None or precision == "unknown":
        return "unknown"
    if vf.tzinfo is not None:
        vf = vf.astimezone(timezone.utc)
    return vf.strftime("%Y-%m") if precision == "month" else vf.date().isoformat()


def _published_label(row: SupportLike) -> str:
    pub = getattr(row, "published_at", None)
    if not isinstance(pub, datetime):
        return "unknown"
    if pub.tzinfo is not None:
        pub = pub.astimezone(timezone.utc)
    return pub.date().isoformat()


def build_verifier_prompt(
    statement: StatementLike, support: Sequence[SupportLike], *, show_context: bool = False,
) -> str:
    """The clean context: the statement and its quotes, each with its
    publication date (metadata); nothing else. ``show_context`` shows each
    quote's recorded whole sentence instead (measured off by default — see
    the module doc)."""
    lines = [
        "STATEMENT:",
        strip_markers(statement.text),
        f"KIND: {enum_value(statement.kind)}",
        f"VALID FROM: {_valid_label(statement)}",
        "",
        "QUOTED SUPPORT:",
    ]
    for i, row in enumerate(_evidence(support), start=1):
        quote = ((getattr(row, "quote_context", None) if show_context else None) or row.quote or "").strip()
        # JSON-encoded: a quote mark or a line break inside the quote cannot
        # end the quote early or start a new line of the frame
        lines.append(f"  Q{i} (published {_published_label(row)}): {json.dumps(quote, ensure_ascii=False)}")
    return "\n".join(lines)


def read_verdict(reply: dict[str, Any] | None) -> tuple[str, str | None]:
    """``(verdict, reason_code)`` from a parsed reply; ``unclear`` if absent."""
    if reply is None:
        return UNCLEAR, None
    raw = as_text(reply.get("verdict")).lower().replace(" ", "_").replace("-", "_")
    raw = {"does_not": DOES_NOT_SUPPORT, "not_supported": DOES_NOT_SUPPORT,
           "partial": PARTLY, "partly_supports": PARTLY, "supported": SUPPORTS}.get(raw, raw)
    verdict = first_of([raw], VERDICTS) or UNCLEAR
    reason = as_text(reply.get("reason_code")).lower() or None
    if reason is not None and reason not in REASON_CODES:
        reason = "other"
    return verdict, reason


async def verify_support(
    llm: SeatLLM | None,
    statement: StatementLike,
    support: Sequence[SupportLike],
    *,
    texts: EvidenceTexts,
    system_prompt: str,
    code_first: bool = True,
    budget: CallBudget | None = None,
    read_at: datetime | None = None,
    show_context: bool = False,
    thinking: bool | None = None,
) -> VerifierReport:
    """Check one statement's support: code, then (if code passed, or
    ``code_first`` is off) one clean-context model call. An assessment or an
    absence (or, S-6, a coverage statement) is ``not_applicable`` — never
    sent, never established. ``thinking`` goes to the model call as
    :func:`statement_seats.call_seat` says (``None`` = unchanged)."""
    if enum_value(statement.kind) in _NOT_QUOTED_KINDS:
        return VerifierReport(NOT_APPLICABLE, "code", None, ())
    failures = tuple(f"{c.gate}.{c.check}:{c.reason}"
                     for c in code_precheck(statement, support, texts, read_at=read_at) if not c.passed)
    if failures and code_first:
        return VerifierReport(DOES_NOT_SUPPORT, "code", None, failures)
    if llm is None:
        return VerifierReport(UNCLEAR, "no_call", None, failures)
    content, call = await call_seat(
        llm, seat=VERIFIER_SEAT_ID, system_prompt=system_prompt,
        user_content=build_verifier_prompt(statement, support, show_context=show_context), budget=budget,
        thinking=thinking,
    )
    if content is None:
        return VerifierReport(UNCLEAR, "no_call", None, failures, call)
    verdict, reason = read_verdict(parse_reply_object(content))
    return VerifierReport(verdict, "model" if verdict != UNCLEAR else "unparsed",
                          reason, failures, call)


__all__ = [
    "DOES_NOT_SUPPORT",
    "NOT_APPLICABLE",
    "PARTLY",
    "REASON_CODES",
    "SUPPORTS",
    "UNCLEAR",
    "VERDICTS",
    "VerifierReport",
    "build_verifier_prompt",
    "code_precheck",
    "read_verdict",
    "verify_support",
]
