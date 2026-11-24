# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The honesty state of a finding that carries NO verification block (X4 READPATH, W11-F53).

``GET /findings`` folds each finding's latest faithfulness critique (else its
latest structural one) into the row's ``verification`` block. A finding with
neither used to come back with ``verification: null`` and nothing else, so every
client had to guess whether "no block" meant *the judge has not run yet* or
*the judge will never run*, and most surfaces guessed neither: the row rendered
as a plain current read.

WHY A ROW CAN HAVE NO BLOCK (measured live 2026-10-13, read-only):

  * The verify pass runs INLINE, after the finding is written, on an autocommit
    connection (``dapr_actors`` → ``actor_critic.verify_inline_target_finding``).
    The finding is visible from its INSERT; its critique lands when the pass
    finishes — over 7 days of faithfulness critiques the lag from the finding's
    ``created_at`` was p50 66–69 s, p95 146–171 s, p99 219–254 s, max 549 s
    (``partial`` judge runs: p50 285 s, max 513 s). That window is *pending*.
  * The pass never runs for a verify-EXEMPT analyst
    (``provenance.kinds.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS``) — by design.
  * The pass no-ops for an honest-empty composition (no ``citations`` key).
  * The pass is LOST when the runtime container is recreated while it is in
    flight: the finding committed, the critique never will (the 2026-10-09
    01:35:46Z Brazil and 04:41:26Z Japan reads, inside rolls 44 and 46).

So the route states which of the two a block-less row is, and the UI renders it
instead of inferring it:

  * ``pending`` — younger than :data:`JUDGE_PENDING_WINDOW_S` and not exempt:
    the verify pass may still land.
  * ``absent`` — older than the window, or verify-exempt: no verdict is coming
    on this row as it stands.

The window is 15 minutes: above the 7-day maximum observed lag (549 s) with
margin, short enough that a lost pass stops reading as "pending" within one
read cadence. It is a constant, not a TUNABLE — it describes the verify pass's
measured behaviour, it does not change it.

Slim: imports only ``provenance.kinds`` (already imported by the route module).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..provenance.kinds import verify_exempt_reason

__all__ = [
    "JUDGE_PENDING_WINDOW_S",
    "SCORE_STATE_ABSENT",
    "SCORE_STATE_PENDING",
    "unverified_state",
]

#: The verify pass may still land within this many seconds of the finding.
JUDGE_PENDING_WINDOW_S = 900

SCORE_STATE_PENDING = "pending"
SCORE_STATE_ABSENT = "absent"


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def unverified_state(
    produced_at: datetime,
    analyst_id: str | None,
    *,
    exempt: bool | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The state block for a finding with NO verification block.

    ``{"score_state": "pending"|"absent", "since": <produced_at ISO>,
    "judge_window_s": 900, "reason": ...}`` — ``reason`` is
    ``awaiting_verify`` (pending), ``verify_exempt`` (the pass never runs for
    this analyst) or ``no_verdict`` (the window closed with nothing written).
    Never carries a score: there is none, and a ``0`` would be a fabricated one.

    ``exempt`` — the caller's own exemption verdict when it has one (the full
    row's ``verify_exempt`` stamp, which also knows a deterministic ROLLUP by
    its payload); ``None`` falls back to the analyst registry alone.
    """
    produced = _aware(produced_at)
    current = _aware(now) if now is not None else datetime.now(timezone.utc)
    if exempt is None:
        exempt = verify_exempt_reason(analyst_id) is not None
    age_s = (current - produced).total_seconds()
    if exempt:
        state, reason = SCORE_STATE_ABSENT, "verify_exempt"
    elif age_s < JUDGE_PENDING_WINDOW_S:
        state, reason = SCORE_STATE_PENDING, "awaiting_verify"
    else:
        state, reason = SCORE_STATE_ABSENT, "no_verdict"
    return {
        "score_state": state,
        "since": produced.isoformat(),
        "judge_window_s": JUDGE_PENDING_WINDOW_S,
        "reason": reason,
    }
