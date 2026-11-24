# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult front door's two turn WRITERS — split out of ``consult_api``.

X2 C1 moved them here unchanged in behaviour except for what C1 adds: every
assistant row now carries its run id, synthesis status, usage receipt and
verification stamp (W1-F1). ``consult_api`` re-exports both under their old
private names, so every caller and test is unchanged. ``consult_api.py`` sat 42
lines under the module-size gate's new-entrant threshold; the writers are the
cohesive unit C1 owns.
"""

from __future__ import annotations

from typing import Any

from . import consult_case_front as _case_front
from . import consult_persistence
from . import consult_turn_stamps as _stamps

__all__ = ["persist_assistant_turn", "persist_partial_turn"]


async def persist_assistant_turn(
    pg: Any,
    session_id: str | None,
    response: Any,
    *,
    steps: Any = None,
) -> None:
    """Append the assistant turn to the audit trail (0038), best-effort.

    Projects the response's typed tool_calls / cited_refs back to plain dicts
    for the jsonb columns and threads the FULL ReAct ``steps`` trace into the
    ``consult_turns.steps`` column so a turn is inspectable after the fact
    (previously that column was never populated). The response bag (0223) —
    uncertainty, census, hold, floor stamp, watch candidates, model — rides
    the ``response`` column so a reloaded turn renders what a live one did.
    A no-op when there's no session (the open failed) — the consult answer is
    unaffected either way.
    """
    if not session_id:
        return
    await consult_persistence.append_turn(
        pg,
        session_id=session_id,
        role="assistant",
        content=response.answer,
        steps=steps if steps is not None else [],
        tool_calls=[tc.model_dump() for tc in response.tool_calls],
        cited_refs=[cr.model_dump() for cr in response.cited_refs],
        finding_id=response.finding_id,
        response=consult_persistence.response_bag(response),
        # X2 C1 (W1-F1) — the run, its synthesis status, its usage receipt and
        # its verification stamp: all four were on the response and none
        # reached the row (every live turn stored ``usage = {}``).
        request_id=response.request_id,
        synthesis_status=response.synthesis_status,
        usage=response.usage,
        verification=response.verification or _stamps.verification_stamp(
            None, reason_if_absent="no_stamp_from_run",
        ),
    )


async def persist_partial_turn(
    pg: Any, run: Any, *, plane: str | None = None, route: str | None = None,
) -> None:
    """Record a failed / cancelled run as an assistant turn carrying its partial.

    Best-effort like every other audit write (``append_turn`` swallows its own
    errors). The content is :meth:`ConsultRun.partial_summary` — prose, not a
    status code — and ``steps`` carries whatever the run watched go by, so a
    reload renders the same partial the live panel showed.
    """
    if not run.session_id:
        return
    await consult_persistence.append_turn(
        pg,
        session_id=run.session_id,
        role="assistant",
        content=run.partial_summary(),
        steps=list(run.steps),
        # C-1b — a stopped / failed CASE turn persists its record (measurement
        # keys None = not measured); a plain turn still writes no bag.
        response=_case_front.partial_bag(run, consult_persistence.RESPONSE_BAG_KEYS),
        # X2 C1 — what the cut run spent (its last meter frame) and the label.
        request_id=run.request_id,
        synthesis_status="none",
        usage=_stamps.usage_receipt(
            _stamps.usage_from_steps(run.steps), plane=plane,
            route=route,
        ),
        verification=_stamps.verification_stamp(
            None, reason_if_absent="synthesis_incomplete",
        ),
    )
