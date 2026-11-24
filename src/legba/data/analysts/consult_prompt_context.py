# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult prompt's RUN CONTEXT block — what is true at THIS run's start.

THE DEFECT THIS CLOSES (CONSULT_REVIEW_2026-09-28 §A, §B.4). ``series_history``
and ``series_compare`` are registered in ``action_pack_substrate_read.yaml``,
granted to ``consult_default`` and ``deep_consult``, present in
``_KNOWN_TOOLS`` and rendered onto the native tool wire — and were **invoked
zero times, ever**. Two reasons, both here:

  * the hand-written tool catalogue in ``consult_on_demand._SYSTEM_PROMPT``
    stopped at ``list_sources``, so the model was never told the two tools
    existed (fixed in the constant itself);
  * nothing told the model there was anything to READ. A tool over an empty
    store and a tool over eleven loaded series are the same tool from inside
    the prompt, and a planner with a question about the past will answer it
    from model knowledge rather than gamble a round on an empty holding.

So the holdings line is rendered per run off the LOADED collections, and it is
the honest absence — *"no curated series loaded"* — when nothing is loaded.
Never a zero dressed as a holding, and never a holding implied when the read
failed: those are three different lines below, because they are three
different facts.

THE PACK FRAGMENTS. The ``substrate_read`` pack carries its own
``prompt_fragments`` and ``rules`` — the valid-period obligation on a series
row among them — and the runtime injected them only on the inline_target
GATHER binding (``dapr_host.py``), never on the consult binding, although the
consult path is the one governed THROUGH that pack. They are read here from
the live pack descriptor the binding already resolved. Deliberately NOT copied
into this file: a rule that exists in two places drifts, and the descriptor is
the one an operator edits.

Everything in this module is best-effort by contract. A run context that
cannot be built is an empty string and the prompt is exactly what it was
before — this block informs an answer, it never costs one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_SCHEDULED_EVENTS_BLOCK_CHARS",
    "MAX_SCHEDULED_EVENTS_RENDERED",
    "MAX_SCHEDULED_EVENT_TITLE_CHARS",
    "MAX_SERIES_IDS_RENDERED",
    "MAX_SUBJECTS_RENDERED",
    "RUN_CONTEXT_HEADER",
    "SCHEDULED_EVENTS_WINDOW",
    "render_run_context",
]

#: The block's own banner. Spelled out so the model reads it as a fact about
#: this run rather than as one more standing instruction.
RUN_CONTEXT_HEADER: str = (
    "RUN CONTEXT — read from the platform at the start of THIS run, not baked "
    "into this prompt."
)

#: Render bounds. The catalogue is a prompt line, not a report: past these the
#: line says how many were not named rather than growing without limit.
MAX_SERIES_IDS_RENDERED: int = 24
MAX_SUBJECTS_RENDERED: int = 24

#: W11-F141 — the scheduled-events calendar's prompt window (now → +14 days)
#: and the most rows it renders. The events are injected into the run context
#: so the dated list lands BEFORE the question, every run, with the line a
#: memory-assertion can never replace.
SCHEDULED_EVENTS_WINDOW: timedelta = timedelta(days=14)
MAX_SCHEDULED_EVENTS_RENDERED: int = 40
#: The block rides EVERY consult's system prompt, so it is bounded in
#: characters as well as rows: an operator-entered title is clipped, and the
#: dated list stops before the block passes the cap (~1.5k tokens), the rows
#: it could not fit counted in the overflow line.
MAX_SCHEDULED_EVENT_TITLE_CHARS: int = 160
MAX_SCHEDULED_EVENTS_BLOCK_CHARS: int = 6000

#: The sentence the whole lane exists for — one wording, so a drift in it is
#: a diff, not a paraphrase.
_NEVER_ASSERT: str = (
    "Events not in this list are not known to the platform — "
    "never assert that nothing is scheduled."
)


async def render_run_context(*, substrate: Any, agency_binding: Any) -> str:
    """The block appended to the consult system prompt for one run.

    ``""`` when there is nothing to say — which is the case for a hand-built
    deps bundle with neither a holdings reader nor a governed binding, so an
    embedder's prompt is byte-identical to what it was before this existed.
    """
    parts: list[str] = []
    holdings = await _holdings_line(substrate)
    if holdings:
        parts.append(holdings)
    events = await _events_block(substrate)
    if events:
        parts.append(events)
    pack = _pack_block(agency_binding)
    if pack:
        parts.append(pack)
    if not parts:
        return ""
    return "\n\n" + RUN_CONTEXT_HEADER + "\n" + "\n".join(parts)


async def _holdings_line(substrate: Any) -> str:
    """One line naming the curated series the platform holds.

    Three outcomes, three different sentences:

      * no reader on this substrate at all — ``""``. Nothing is known and
        nothing is claimed; the prompt keeps its previous shape.
      * the read succeeded and found nothing — the honest absence, so the
        planner does not spend a round discovering it.
      * the read failed — said so in words. "No curated series loaded" would
        be a claim, and a failed read is not evidence of an empty store.
    """
    reader = getattr(substrate, "series_holdings", None)
    if not callable(reader):
        return ""
    try:
        holdings = await reader()
    except Exception as exc:  # noqa: BLE001 — a prompt block never fails a run
        logger.warning("consult.run_context.holdings_read_failed err=%s", exc)
        return (
            "Curated series held: NOT MEASURED for this run (the holdings "
            "catalogue could not be read) — do not conclude from this line "
            "that no history exists; call series_history if a question turns "
            "on a historical number."
        )
    if not isinstance(holdings, dict):
        return ""
    series = holdings.get("series")
    series = series if isinstance(series, list) else []
    count = len(series)
    if not count:
        return (
            "Curated series held: no curated series loaded — there is no "
            "curated historical holding to read on this run, so series_history "
            "and series_compare have nothing to return. Say so rather than "
            "supplying a historical number from memory."
        )
    subjects = holdings.get("subjects")
    subjects = [str(s) for s in subjects if str(s).strip()] if isinstance(
        subjects, list
    ) else []
    ids = [
        str(row.get("series_id"))
        for row in series
        if isinstance(row, dict) and str(row.get("series_id") or "").strip()
    ]
    return (
        f"Curated series held: {count} series for "
        f"{_render_list(subjects, MAX_SUBJECTS_RENDERED)}; ask "
        "series_history(series_id, subject, since, until) for one subject's "
        "history or series_compare(series_id, subjects, since, until) across "
        f"several. The series ids are {_render_list(ids, MAX_SERIES_IDS_RENDERED)}. "
        "These are the ONLY subjects and series the platform holds curated "
        "numbers for; a historical figure for anything else is not in the "
        "substrate."
    )


async def _events_block(substrate: Any) -> str:
    """The next 14 days of scheduled events as a short dated list.

    Three outcomes, three different texts — the same honesty rule as the
    holdings line, and the lane's whole point: "the calendar is empty",
    "nothing is held for this window" and "I could not look" are three
    different facts, and only the first two may ever be said.

      * no reader on this substrate at all — ``""``, the embedder's prompt
        unchanged;
      * the read succeeded — the dated rows, or the honest absence (an
        empty TABLE says so; an empty WINDOW over a non-empty table says
        THAT instead);
      * the read failed — NOT MEASURED, so the model can never take a
        failed read as "nothing scheduled".
    """
    reader = getattr(substrate, "list_scheduled_events", None)
    if not callable(reader):
        return ""
    now = datetime.now(timezone.utc)
    window_end = now + SCHEDULED_EVENTS_WINDOW
    try:
        out = await reader(
            window_start=now.isoformat(),
            window_end=window_end.isoformat(),
            # One past the cap, so an over-full window is visible as such.
            limit=MAX_SCHEDULED_EVENTS_RENDERED + 1,
        )
    except Exception as exc:  # noqa: BLE001 — a prompt block never fails a run
        logger.warning("consult.run_context.events_read_failed err=%s", exc)
        return (
            "Scheduled events (next 14 days): NOT MEASURED for this run "
            "(the calendar could not be read) — the read failing is not "
            "evidence that nothing is scheduled; say the read failed."
        )
    if not isinstance(out, dict) or out.get("error"):
        err = out.get("error") if isinstance(out, dict) else None
        return (
            "Scheduled events (next 14 days): NOT MEASURED for this run "
            f"(the calendar read refused{f': {err}' if err else ''}) — "
            "never assert that nothing is scheduled on the strength of "
            "this line; say the read failed."
        )
    coverage = out.get("coverage") if isinstance(out.get("coverage"), dict) else {}
    total = coverage.get("total_rows")
    rows = out.get("rows")
    rows = rows if isinstance(rows, list) else []
    if not rows:
        if total == 0:
            return (
                "Scheduled events: the platform's scheduled-events table is "
                "EMPTY — no events have been entered at all. "
                + _NEVER_ASSERT
            )
        return (
            "Scheduled events (next 14 days): none held for this window. "
            + _NEVER_ASSERT
        )
    lines = [
        f"Scheduled events (next 14 days, read from the platform calendar "
        f"at {now.date().isoformat()}):"
    ]
    # Reserve room for the overflow line and the closing sentence so the
    # whole block — not just the list — stays under the character cap.
    budget = MAX_SCHEDULED_EVENTS_BLOCK_CHARS - len(lines[0]) - len(_NEVER_ASSERT) - 80
    shown = 0
    for row in rows[:MAX_SCHEDULED_EVENTS_RENDERED]:
        line = f"  - {_event_line(row)}"
        if len(line) + 1 > budget:
            break
        budget -= len(line) + 1
        lines.append(line)
        shown += 1
    if len(rows) > shown:
        more = "more" if len(rows) > MAX_SCHEDULED_EVENTS_RENDERED else str(len(rows) - shown)
        lines.append(f"  (+{more} held for the window not listed here)")
    lines.append(_NEVER_ASSERT)
    return "\n".join(lines)


def _event_line(row: Any) -> str:
    """``2026-11-09: title (global · confirmed)`` — one event, one line."""
    if not isinstance(row, dict):
        return str(row)
    start = str(row.get("starts_at") or "")[:10]
    end = str(row.get("ends_at") or "")[:10]
    span = f"{start} → {end}" if end and end != start else start
    scope = _clip(str(row.get("scope") or "global"), 40)
    confidence = _clip(str(row.get("confidence") or "confirmed"), 20)
    title = _clip(" ".join(str(row.get("title") or "").split()),
                  MAX_SCHEDULED_EVENT_TITLE_CHARS)
    return f"{span}: {title} ({scope} · {confidence})"


def _clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, an ellipsis marking the cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pack_block(agency_binding: Any) -> str:
    """The governed pack's OWN prompt fragments and rules, verbatim.

    Read off ``binding.pack`` — the live descriptor the runtime already
    resolved for this analyst — so a rule an operator edits in
    ``descriptors/action_pack_substrate_read.yaml`` reaches the consult prompt
    on the next activation without a code change. ``""`` when the run is
    ungoverned (no binding), which is the non-production embedder path.
    """
    pack = getattr(agency_binding, "pack", None)
    if pack is None:
        return ""
    fragments = _strings(getattr(pack, "prompt_fragments", None))
    rules = _strings(getattr(pack, "rules", None))
    if not fragments and not rules:
        return ""
    pack_id = getattr(getattr(pack, "identity", None), "id", None) or "action pack"
    lines = [
        f"Tool-pack guidance in force for {pack_id} (from the live pack "
        "descriptor — these bind every tool call this run makes):"
    ]
    lines += [f"  - {text}" for text in fragments]
    if rules:
        lines.append("Pack rules (hard):")
        lines += [f"  - {text}" for text in rules]
    return "\n".join(lines)


def _strings(raw: Any) -> list[str]:
    """Flatten a descriptor list field into single-line strings.

    The YAML folds its blocks across several physical lines; a prompt bullet
    is one line, so the whitespace is collapsed here rather than shipping a
    bullet whose continuation lines read as separate instructions.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        text = " ".join(str(item).split()).strip()
        if text:
            out.append(text)
    return out


def _render_list(values: list[str], cap: int) -> str:
    """``a, b and c`` — with an explicit count of what the cap left out."""
    if not values:
        return "no named subject"
    shown = values[:cap]
    tail = len(values) - len(shown)
    text = ", ".join(shown)
    if tail > 0:
        text += f" (+{tail} more not listed here)"
    return text
