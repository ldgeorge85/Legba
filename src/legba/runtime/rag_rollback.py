# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""rag_rollback — the opportunistic-RAG (``vector:world_context``) auto-rollback guard.

The staggered flip that turns opportunistic RAG on for a bounded assessment unit
is a MEASURED experiment (``scripts/rag_watch.py`` + the pre-registered rule in
``planning/RAG_EXPANSION_WATCH_2026-07-03.md``). Before M22 the "rollback" was
COMMENTS ONLY — ``rag_watch`` printed a verdict, but a triggered rule required a
human to hand-edit a descriptor + PUT it live. This module is the REAL code guard:

  * :func:`evaluate_rollback` — the pure, unit-testable rule. Fires on any of
      (a) trailing-mean FAITHFULNESS drop >= ``faith_drop_trigger`` (0.08),
      (b) LOW-FAITH rate more than ``low_faith_ratio_trigger``× the baseline, OR
      (c) TOKEN cost rise >= ``token_rise_frac`` (0.35) — the cost trigger the
          pre-M22 rule only PRINTED. Set to 0.35 so it CATCHES the motivating
          leadership_transition case (a +42% token rise alongside the faith drop);
          a 0.50 default would have missed it. (c) makes cost a first-class trigger,
          not just an annotation.
  * :func:`world_context_disabled_units` / :func:`is_world_context_enabled` — the
      runtime KILL-SWITCH the grounding hook honors. A unit in the disabled set
      gets NO ``vector:world_context`` block even though its descriptor still lists
      the source — i.e. the flip is REVERTED in code, with no live PUT / redeploy.
  * :func:`record_rollback` — the ACTUATOR: when ``rag_watch --enforce`` sees the
      rule trigger, it writes the unit into the persisted rollback state, which the
      runtime reads on its next grounding build. Auto-revert, end to end.

The kill-switch is sourced from BOTH an env var (``LEGBA_WORLD_CONTEXT_DISABLED_UNITS``,
comma-separated analyst ids) and an optional JSON state file
(``LEGBA_RAG_ROLLBACK_STATE``) so an operator can pin a unit off by env OR the
guard can persist a rollback durably. This does NOT flip any unit ON — it is a
one-way safety brake; re-enabling a unit is a deliberate operator action (clear the
env / state entry).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_FAITH_DROP_TRIGGER",
    "DEFAULT_LOW_FAITH_RATIO_TRIGGER",
    "DEFAULT_TOKEN_RISE_FRAC",
    "RollbackDecision",
    "RollbackWindow",
    "evaluate_rollback",
    "is_world_context_enabled",
    "record_rollback",
    "world_context_disabled_units",
]


# Pre-registered thresholds (planning/RAG_EXPANSION_WATCH_2026-07-03.md + M22).
DEFAULT_FAITH_DROP_TRIGGER = 0.08      # (a) absolute trailing-mean faithfulness drop
DEFAULT_LOW_FAITH_RATIO_TRIGGER = 2.0  # (b) low-faith rate multiple over baseline
DEFAULT_TOKEN_RISE_FRAC = 0.35         # (c) fractional avg-tokens/run rise (M22-new;
#                                        0.35 CATCHES the motivating +42% leadership
#                                        case that a 0.50 default would have missed)

_ENV_DISABLED = "LEGBA_WORLD_CONTEXT_DISABLED_UNITS"
_ENV_STATE_PATH = "LEGBA_RAG_ROLLBACK_STATE"


# ---------------------------------------------------------------------------
# The rule (pure — no DB, no env, fully unit-testable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollbackWindow:
    """A before- or after-flip measurement window (the fields the rule reads).

    All optional so an empty / under-filled window degrades safely (a rule that
    needs a value it doesn't have simply doesn't fire that leg). Mirrors the
    ``WindowStats`` ``rag_watch`` already computes from the substrate."""

    n: int = 0
    mean_faith: float | None = None
    low_faith_rate: float | None = None
    low_faith_count: int = 0
    tokens_mean: float | None = None


@dataclass(frozen=True)
class RollbackDecision:
    """The verdict + WHY. ``triggered`` is the actionable bit; ``reasons`` lists
    each fired leg (human-readable); ``provisional`` marks an under-filled window
    (the rule fired but on < ``window`` samples, so the operator should confirm)."""

    triggered: bool
    reasons: list[str] = field(default_factory=list)
    provisional: bool = False
    faith_delta: float | None = None
    low_faith_ratio: float | None = None
    token_rise_frac: float | None = None


def evaluate_rollback(
    before: RollbackWindow,
    after: RollbackWindow,
    *,
    window: int,
    faith_drop_trigger: float = DEFAULT_FAITH_DROP_TRIGGER,
    low_faith_ratio_trigger: float = DEFAULT_LOW_FAITH_RATIO_TRIGGER,
    token_rise_frac: float = DEFAULT_TOKEN_RISE_FRAC,
) -> RollbackDecision:
    """Evaluate the pre-registered RAG rollback rule over two windows.

    Fires (``triggered=True``) on ANY of:
      (a) ``before.mean_faith - after.mean_faith >= faith_drop_trigger``;
      (b) ``after.low_faith_rate > low_faith_ratio_trigger * before.low_faith_rate``
          (zero-baseline guard: a clean baseline fires only with >= 2 post-flip
          low-faith rows, matching ``rag_watch``);
      (c) ``(after.tokens_mean - before.tokens_mean) / before.tokens_mean
          >= token_rise_frac`` — the cost trigger.

    Any leg whose inputs are missing simply doesn't fire. ``provisional`` is set
    when either window has fewer than ``window`` samples (the verdict stands, but
    on thin evidence).
    """
    reasons: list[str] = []

    faith_delta = None
    if before.mean_faith is not None and after.mean_faith is not None:
        faith_delta = before.mean_faith - after.mean_faith
        if faith_delta >= faith_drop_trigger:
            reasons.append(
                f"faithfulness dropped {faith_delta:+.3f} "
                f"(>= {faith_drop_trigger:.2f} trigger)"
            )

    low_ratio = None
    if before.low_faith_rate is not None and after.low_faith_rate is not None:
        if before.low_faith_rate > 0:
            low_ratio = after.low_faith_rate / before.low_faith_rate
            if after.low_faith_rate > low_faith_ratio_trigger * before.low_faith_rate:
                reasons.append(
                    f"low-faith rate x{low_ratio:.2f} "
                    f"(> {low_faith_ratio_trigger:.1f}x baseline)"
                )
        elif after.low_faith_rate > 0 and after.low_faith_count >= 2:
            reasons.append(
                "low-faith rate rose from a clean (0) baseline "
                f"({after.low_faith_count} post-flip low-faith runs)"
            )

    token_frac = None
    if (
        before.tokens_mean is not None
        and after.tokens_mean is not None
        and before.tokens_mean > 0
    ):
        token_frac = (after.tokens_mean - before.tokens_mean) / before.tokens_mean
        if token_frac >= token_rise_frac:
            reasons.append(
                f"avg tokens/run rose {token_frac * 100:+.0f}% "
                f"(>= {token_rise_frac * 100:.0f}% trigger)"
            )

    provisional = before.n < window or after.n < window
    return RollbackDecision(
        triggered=bool(reasons),
        reasons=reasons,
        provisional=provisional,
        faith_delta=faith_delta,
        low_faith_ratio=low_ratio,
        token_rise_frac=token_frac,
    )


# ---------------------------------------------------------------------------
# The kill-switch (env + persisted state) the runtime + actuator share
# ---------------------------------------------------------------------------


def _parse_units(raw: str | None) -> set[str]:
    if not raw or not raw.strip():
        return set()
    return {u.strip().casefold() for u in raw.split(",") if u.strip()}


def _state_path(path: str | None = None) -> str | None:
    return path or (os.getenv(_ENV_STATE_PATH) or "").strip() or None


def _load_state(path: str | None = None) -> dict:
    p = _state_path(path)
    if not p or not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:  # degrade — a bad state file never crashes
        logger.warning("rag_rollback.state_read_failed path=%s err=%s", p, exc)
        return {}


def world_context_disabled_units(*, state_path: str | None = None) -> frozenset[str]:
    """The set of analyst ids whose ``vector:world_context`` RAG is REVERTED off.

    Union of the ``LEGBA_WORLD_CONTEXT_DISABLED_UNITS`` env list and the persisted
    rollback state's ``disabled_units`` (both casefolded). Never raises — a missing
    env / unreadable state degrades to an empty set (RAG stays as the descriptor
    declares)."""
    units = _parse_units(os.getenv(_ENV_DISABLED))
    state = _load_state(state_path)
    for u in state.get("disabled_units") or []:
        if isinstance(u, str) and u.strip():
            units.add(u.strip().casefold())
    return frozenset(units)


def is_world_context_enabled(analyst_id: str, *, state_path: str | None = None) -> bool:
    """True unless ``analyst_id`` has been rolled back off (env or persisted state).

    The grounding hook calls this to decide whether to honor a descriptor's
    ``vector:world_context`` source — so an auto-rollback (or an operator env pin)
    disables the RAG block in code, with no live descriptor PUT / redeploy."""
    if not analyst_id:
        return True
    return analyst_id.casefold() not in world_context_disabled_units(state_path=state_path)


def record_rollback(
    analyst_id: str,
    *,
    state_path: str | None = None,
    reasons: Sequence[str] = (),
) -> str | None:
    """Persist ``analyst_id`` into the rollback state (the auto-rollback ACTUATOR).

    Merges the unit into ``disabled_units`` and appends an audit entry
    (timestamp + reasons) so the next runtime grounding build reverts its RAG
    flip. Returns the state path written, or ``None`` when no state path is
    configured (env-only deployments must set ``LEGBA_RAG_ROLLBACK_STATE`` — or
    the operator pins the unit via ``LEGBA_WORLD_CONTEXT_DISABLED_UNITS``). Never
    raises on an I/O failure — it logs + returns ``None`` (the guard's report is
    still printed by the caller)."""
    p = _state_path(state_path)
    if not p:
        logger.warning(
            "rag_rollback.record.no_state_path analyst=%s — set %s to persist an "
            "auto-rollback, or pin the unit via %s",
            analyst_id, _ENV_STATE_PATH, _ENV_DISABLED,
        )
        return None
    state = _load_state(p)
    disabled = list(state.get("disabled_units") or [])
    key = analyst_id.strip()
    if key and key.casefold() not in {d.casefold() for d in disabled if isinstance(d, str)}:
        disabled.append(key)
    state["disabled_units"] = disabled
    log = list(state.get("rollback_log") or [])
    log.append({
        "analyst_id": key,
        "at": datetime.now(tz=timezone.utc).isoformat(),
        "reasons": list(reasons),
    })
    state["rollback_log"] = log
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
    except OSError as exc:
        logger.warning("rag_rollback.record.write_failed path=%s err=%s", p, exc)
        return None
    logger.info("rag_rollback.recorded analyst=%s path=%s reasons=%r", key, p, list(reasons))
    return p
