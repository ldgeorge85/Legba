# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Competing explanations as a TYPED block (lane Q-1, rule B) — pure, no I/O.

THE DEFECT. The reviewed consult (2026-11-14) gave its competing explanations
probabilities about PROXY events — "0.75 that no US agency states a malicious
cause for either A or B by 2026-12-31" — where the reader needed the
probability that each explanation is TRUE. Those are different quantities: an
agency can stay silent about a malicious cause that is real.

THE CONTRACT. The model does not write competing explanations as prose. It
writes them in the turn's ``contract`` block (a fenced JSON object at the end
of the answer; the system prompt states the shape) and this module renders the
prose from the structure:

    {"hypotheses": [
        {"explanation": "...",                # what would be TRUE
         "p": 0.55,                           # P(explanation is true)
         "for": ["<ref>", ...], "against": ["<ref>", ...],   # cited rows
         "test": {"observable": "...",         # would confirm or refute it
                  "by": "YYYY-MM-DD",
                  "p_observable": 0.4}}],      # OPTIONAL, a different quantity
     "something_else_p": 0.1}

CHECKED IN CODE (:func:`check_hypotheses`): each explanation is non-empty and
does not itself read as a dated observable; ``p`` is a probability; the
explanations' ``p`` plus the explicit ``something_else_p`` sum to 1 (± 0.02);
each item cites at least one row the turn holds, for or against; each carries a
``test`` with an observable and a future date; ``p_observable`` when present is
a probability. On failure the runner makes ONE bounded repair request
(:func:`repair_messages`) and, if the repaired block still fails, renders it
marked NON-CONFORMING with the problems — it is never silently accepted.

A SEALABLE FORECAST is derived from each test line (:func:`forecast_proposals`)
— the observable, the date, the consult's ``p_observable`` — shaped as a
``forecast_ledger.ForecastQuestion`` with its seal problems. It is a proposal on
the turn's data, never written to the ledger here (sealing needs the operator's
number), and it never stands in for the cause probability.

PROBABILITY WORDS. The tree defines two vocabularies. The forecast ledger is
numeric (``forecast_ledger.clamp_p``: 0.01–0.99, three decimals) and the
console renders numbers through the ICD-203 seven-band scale
(``legba-ui-v3/src/lib/verdictModel.ts`` ``LIKELIHOOD_CUTS``); this module uses
that scale (:func:`likelihood_band`) so a rendered word and the console's badge
can never disagree. The other vocabulary — the analytic preamble's three tiers
(``_tradecraft`` clause 4: assess / likely / possibly) — is a confidence
register for prose, not a probability scale, and is not used here.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

__all__ = [
    "CONTRACT_FENCE",
    "HYPOTHESES_PLACEHOLDER",
    "SUM_TOLERANCE",
    "check_hypotheses",
    "extract_contract_block",
    "forecast_proposals",
    "likelihood_band",
    "render_hypotheses",
    "repair_messages",
]

CONTRACT_FENCE = "contract"
#: Where the model wants the rendered section; absent ⇒ the end of the prose.
HYPOTHESES_PLACEHOLDER = "[[hypotheses]]"
SUM_TOLERANCE = 0.02

_BLOCK_RE = re.compile(r"```\s*" + CONTRACT_FENCE + r"\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_DATED_OBSERVABLE_RE = re.compile(
    r"\bby\s+(?:\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Z][a-z]+(?:\s+\d{4})?)\b"
    # "no US agency states a malicious cause …" — a statement about what will
    # be SAID, which is an observable, not a cause.
    r"|^\s*(?:that\s+)?no\b.{0,60}\b(?:states?|announces?|publishes?|attributes?|claims?)\b",
    re.IGNORECASE,
)

#: ICD-203 likelihood bands — the console's ``LIKELIHOOD_CUTS`` (upper bounds, exclusive).
_BANDS: tuple[tuple[float, str], ...] = (
    (0.05, "almost no chance"), (0.2, "very unlikely"), (0.45, "unlikely"),
    (0.55, "roughly even chance"), (0.8, "likely"), (0.95, "very likely"),
    (1.01, "almost certain"),
)


def likelihood_band(p: Any) -> str:
    """The ICD-203 word for ``p`` (the console's own cut), ``unstated`` otherwise."""
    try:
        value = float(p)
    except (TypeError, ValueError):
        return "unstated"
    if not 0.0 <= value <= 1.0:
        return "unstated"
    for cut, band in _BANDS:
        if value < cut:
            return band
    return "almost certain"


def extract_contract_block(answer: str) -> tuple[str, dict[str, Any] | None, str | None]:
    """``(answer_without_block, parsed, error)``.

    The LAST ```contract fenced block is the turn's contract; it is removed
    from the prose whether or not it parses (a reader never sees raw JSON).
    ``parsed`` is ``None`` with ``error`` set when it was present but not a
    JSON object, and ``None`` with ``error`` ``None`` when it was absent.
    """
    matches = list(_BLOCK_RE.finditer(answer or ""))
    if not matches:
        return answer, None, None
    m = matches[-1]
    stripped = (answer[: m.start()].rstrip() + "\n" + answer[m.end():].lstrip()).rstrip()
    # A model that opens the block with a bare fence before the tagged one
    # (seen on the core route, 2026-11-14) leaves a lone ``` behind: drop it.
    lines = stripped.split("\n")
    while lines and lines[-1].strip() in ("```", ""):
        lines.pop()
    stripped = "\n".join(lines) + "\n"
    try:
        parsed = json.loads(m.group(1))
    except ValueError as exc:
        return stripped, None, f"contract block is not JSON ({exc.msg})"
    if not isinstance(parsed, dict):
        return stripped, None, "contract block is not a JSON object"
    return stripped, parsed, None


def _prob(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return p if 0.0 <= p <= 1.0 else None


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _ref_ok(token: Any, held: Sequence[str]) -> bool:
    t = str(token or "").strip().strip("[]").lower()
    if len(t) < 8:
        return False
    return any(h.startswith(t) for h in held)


def check_hypotheses(
    block: Mapping[str, Any] | None, *, held_refs: Sequence[str], now: datetime | None = None,
) -> list[str]:
    """Every way the block breaks the contract (empty = conforming).

    ``held_refs`` are the turn's citable refs (lower-case uuids); an item's
    for/against tokens may be a full id or an 8+ hex prefix of one.
    """
    if block is None:
        return []
    items = block.get("hypotheses")
    if items in (None, []):
        return []
    if not isinstance(items, list):
        return ["hypotheses is not a list"]
    now = now or datetime.now(timezone.utc)
    held = [str(h).lower() for h in held_refs]
    problems: list[str] = []
    total = 0.0
    for i, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            problems.append(f"item {i} is not an object")
            continue
        text = str(item.get("explanation") or "").strip()
        if not text:
            problems.append(f"item {i} has no explanation")
        elif _DATED_OBSERVABLE_RE.search(text):
            problems.append(
                f"item {i}'s explanation reads as a dated observable, not a cause — "
                "the probability must be about what is true; put the observable under test"
            )
        p = _prob(item.get("p"))
        if p is None:
            problems.append(f"item {i} has no probability p in [0, 1] that the explanation is true")
        else:
            total += p
        refs = [*(item.get("for") or []), *(item.get("against") or [])]
        if not isinstance(item.get("for") or [], list) or not isinstance(item.get("against") or [], list):
            problems.append(f"item {i}: for/against must be lists of cited rows")
        elif not any(_ref_ok(r, held) for r in refs):
            problems.append(f"item {i} cites no row this turn holds, for or against")
        test = item.get("test")
        if not isinstance(test, Mapping):
            problems.append(f"item {i} has no 'would be confirmed or refuted by' test")
            continue
        if not str(test.get("observable") or "").strip():
            problems.append(f"item {i}'s test names no observable")
        by = _date(test.get("by"))
        if by is None:
            problems.append(f"item {i}'s test has no date (by: YYYY-MM-DD)")
        elif by < now.date():
            problems.append(f"item {i}'s test date {by.isoformat()} is already past")
        if test.get("p_observable") is not None and _prob(test.get("p_observable")) is None:
            problems.append(f"item {i}'s p_observable is not a probability")
    other = _prob(block.get("something_else_p"))
    if other is None:
        problems.append("something_else_p is missing — the explanations plus 'something else' must sum to 1")
    else:
        total += other
        if abs(total - 1.0) > SUM_TOLERANCE:
            problems.append(f"the probabilities sum to {total:.3f}, not 1 (explanations + something else)")
    return problems


def repair_messages(block: Mapping[str, Any] | None, raw_error: str | None,
                    problems: Sequence[str]) -> tuple[str, list[dict[str, str]]]:
    """``(system, messages)`` for the ONE bounded repair request."""
    system = (
        "You repair a consult answer's typed contract block. Reply with ONLY the corrected "
        "```contract fenced JSON block and nothing else. Keep every explanation's meaning and "
        "every cited ref; change only what the problems name. Each explanation's p is the "
        "probability that the EXPLANATION IS TRUE; the explanations' p plus something_else_p "
        "sum to 1; each item carries test {observable, by: YYYY-MM-DD, optional p_observable} "
        "— the observable that would confirm or refute it, and (optionally) the probability "
        "the observable occurs by that date, which is a different quantity from p."
    )
    shown = json.dumps(block, ensure_ascii=False, indent=1) if block is not None else "(unparseable)"
    body = (
        "The block below failed the contract.\n"
        + (f"Parse error: {raw_error}\n" if raw_error else "")
        + "Problems:\n- " + "\n- ".join(problems or ["(none listed)"])
        + f"\n\nBlock:\n```contract\n{shown}\n```"
    )
    return system, [{"role": "user", "content": body}]


def _refs_text(tokens: Sequence[Any]) -> str:
    shown = [f"[{str(t).strip().strip('[]')}]" for t in tokens if str(t).strip()]
    return "".join(shown) if shown else "no row"


def render_hypotheses(block: Mapping[str, Any], *, problems: Sequence[str] = ()) -> str:
    """The prose section, rendered from the typed block (never from scraped prose)."""
    items = [i for i in (block.get("hypotheses") or []) if isinstance(i, Mapping)]
    lines = ["## Competing explanations (P = probability the explanation is true)", ""]
    for n, item in enumerate(items, start=1):
        p = _prob(item.get("p"))
        p_txt = f"P = {p:.2f} ({likelihood_band(p)})" if p is not None else "P not stated"
        lines.append(f"{n}. **{str(item.get('explanation') or '').strip()}** — {p_txt}.")
        lines.append(
            f"   For: {_refs_text(item.get('for') or [])}. "
            f"Against: {_refs_text(item.get('against') or [])}."
        )
        test = item.get("test") if isinstance(item.get("test"), Mapping) else {}
        obs = str(test.get("observable") or "").strip() or "(no observable given)"
        by = str(test.get("by") or "").strip() or "(no date)"
        line = f"   Would be confirmed or refuted by: {obs}, by {by}."
        po = _prob(test.get("p_observable"))
        if po is not None:
            line += (
                f" Chance this observable occurs by then: {po:.2f} — a different quantity "
                "from P above."
            )
        lines.append(line)
    other = _prob(block.get("something_else_p"))
    lines.append("")
    lines.append(
        f"Something else: P = {other:.2f} ({likelihood_band(other)})."
        if other is not None else "Something else: not stated."
    )
    if problems:
        lines.append("")
        lines.append(
            "**Non-conforming:** this block failed the hypothesis contract after one repair "
            "— " + "; ".join(problems) + "."
        )
    return "\n".join(lines)


def forecast_proposals(
    block: Mapping[str, Any] | None, *, turn_ref: str, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Sealable-forecast PROPOSALS derived from each test line (never written here)."""
    from ..forecast_ledger import ForecastQuestion, clamp_p

    if not block:
        return []
    now = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for n, item in enumerate(block.get("hypotheses") or [], start=1):
        if not isinstance(item, Mapping) or not isinstance(item.get("test"), Mapping):
            continue
        test = item["test"]
        obs = str(test.get("observable") or "").strip()
        by = _date(test.get("by"))
        if not obs or by is None:
            continue
        due = datetime(by.year, by.month, by.day, tzinfo=timezone.utc) + timedelta(days=1)
        question = ForecastQuestion(
            slug=f"consult-{turn_ref[:8]}-h{n}",
            question=f"{obs} (by {by.isoformat()})",
            resolver_kind="event_reported",
            resolver_spec={"class": "consult_observable", "observable": obs,
                           "window": {"start": now.isoformat(), "end": due.isoformat()}},
            due_at=due,
            window_start=now,
            window_end=due,
            created_by="consult",
        )
        po = _prob(test.get("p_observable"))
        out.append({
            "hypothesis": n,
            "question": question.content(),
            "question_problems": question.problems(),
            "consult_number": (
                {"issuer_kind": "consult", "p": clamp_p(po)} if po is not None
                else {"issuer_kind": "consult", "p": None, "absent_reason": "no p_observable given"}
            ),
            "seal_owed": ["operator number"],
            "note": "derived from the observable line; not the explanation's probability",
        })
    return out
