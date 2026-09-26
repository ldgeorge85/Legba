# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""rag_rollback — the opportunistic-RAG (``vector:world_context``) auto-rollback guard.

**Generalized to any RAG source** (added alongside the ``exemplar`` corpus,
EXEMPLAR_SHELF_DRAFT_2026-07-31.md §4 step 4): the kill-switch was
``world_context``-only by construction (function names, env var, and the
persisted state's ``disabled_units`` key all baked the one source in).
:func:`disabled_units_for` / :func:`is_source_enabled` / :func:`record_rollback_for`
are the SAME mechanism parameterized by a ``source`` token (``"world_context"``,
``"tradecraft"``, ``"exemplar"``, ...), so a newly-provisioned corpus gets the
identical rollback treatment on day one rather than a bespoke guard per corpus.
``world_context_disabled_units`` / ``is_world_context_enabled`` /
``record_rollback`` are now thin wrappers over the generic functions with
``source="world_context"`` — byte-identical behaviour, same env var
(``LEGBA_WORLD_CONTEXT_DISABLED_UNITS``), same state key (``disabled_units``,
NOT ``world_context_disabled_units`` — the legacy state file shape is
preserved). ``exemplar_disabled_units`` / ``is_exemplar_enabled`` are the new
corpus's own named convenience wrappers, "like the others".

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
    "disabled_units_for",
    "evaluate_rollback",
    "exemplar_disabled_units",
    "is_exemplar_enabled",
    "is_source_enabled",
    "is_world_context_enabled",
    "record_rollback",
    "record_rollback_for",
    "world_context_disabled_units",
]


# Pre-registered thresholds (planning/RAG_EXPANSION_WATCH_2026-07-03.md + M22).
DEFAULT_FAITH_DROP_TRIGGER = 0.08      # (a) absolute trailing-mean faithfulness drop
DEFAULT_LOW_FAITH_RATIO_TRIGGER = 2.0  # (b) low-faith rate multiple over baseline
DEFAULT_TOKEN_RISE_FRAC = 0.35         # (c) fractional avg-tokens/run rise (M22-new;
#                                        0.35 CATCHES the motivating +42% leadership
#                                        case that a 0.50 default would have missed)

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


#: The historical source — its env var / state key stay EXACTLY as they were
#: before generalization (below), so no existing deployment / state file /
#: test needs to change.
_LEGACY_SOURCE = "world_context"


def _env_var_for(source: str) -> str:
    """The ``LEGBA_<SOURCE>_DISABLED_UNITS`` env var for one RAG source.

    ``world_context`` reproduces the pre-generalization env var exactly
    (``LEGBA_WORLD_CONTEXT_DISABLED_UNITS``) — same formula, so it is not a
    special case, just documented as one for clarity."""
    return f"LEGBA_{source.upper()}_DISABLED_UNITS"


def _state_key_for(source: str) -> str:
    """The persisted-state key holding one source's disabled-unit list.

    ``world_context`` keeps the LEGACY bare ``disabled_units`` key (predates
    this generalization — state files in the wild use it); every other source
    gets its own ``<source>_disabled_units`` key so multiple sources can share
    one state file without colliding."""
    return "disabled_units" if source == _LEGACY_SOURCE else f"{source}_disabled_units"


def disabled_units_for(source: str, *, state_path: str | None = None) -> frozenset[str]:
    """The set of analyst ids whose ``vector:<source>`` RAG is REVERTED off.

    Union of the ``LEGBA_<SOURCE>_DISABLED_UNITS`` env list and the persisted
    rollback state's per-source key (both casefolded). Never raises — a
    missing env / unreadable state degrades to an empty set (RAG stays as the
    descriptor declares). ``world_context_disabled_units`` is the
    ``source="world_context"`` convenience wrapper (unchanged behaviour)."""
    units = _parse_units(os.getenv(_env_var_for(source)))
    state = _load_state(state_path)
    for u in state.get(_state_key_for(source)) or []:
        if isinstance(u, str) and u.strip():
            units.add(u.strip().casefold())
    return frozenset(units)


def is_source_enabled(source: str, analyst_id: str, *, state_path: str | None = None) -> bool:
    """True unless ``analyst_id`` has been rolled back off ``source`` (env or state).

    A grounding hook calls this to decide whether to honor a descriptor's
    ``vector:<source>`` source — so an auto-rollback (or an operator env pin)
    disables that RAG block in code, with no live descriptor PUT / redeploy.
    ``is_world_context_enabled`` is the ``source="world_context"`` wrapper."""
    if not analyst_id:
        return True
    return analyst_id.casefold() not in disabled_units_for(source, state_path=state_path)


def record_rollback_for(
    source: str,
    analyst_id: str,
    *,
    state_path: str | None = None,
    reasons: Sequence[str] = (),
) -> str | None:
    """Persist ``analyst_id`` into ``source``'s rollback state (the ACTUATOR).

    Merges the unit into that source's disabled-unit key and appends an audit
    entry (timestamp + source + reasons) to the SHARED ``rollback_log`` list —
    one audit trail per state file, entries tagged by source — so the next
    runtime grounding build reverts the flip. Returns the state path written,
    or ``None`` when no state path is configured (env-only deployments must
    set ``LEGBA_RAG_ROLLBACK_STATE`` — or the operator pins the unit via
    ``LEGBA_<SOURCE>_DISABLED_UNITS``). Never raises on an I/O failure — it
    logs + returns ``None`` (the guard's report is still printed by the
    caller). ``record_rollback`` is the ``source="world_context"`` wrapper
    (same state shape it always wrote — ``disabled_units`` / ``rollback_log``,
    no ``source`` key required on old entries)."""
    p = _state_path(state_path)
    if not p:
        logger.warning(
            "rag_rollback.record.no_state_path source=%s analyst=%s — set %s to "
            "persist an auto-rollback, or pin the unit via %s",
            source, analyst_id, _ENV_STATE_PATH, _env_var_for(source),
        )
        return None
    state = _load_state(p)
    state_key = _state_key_for(source)
    disabled = list(state.get(state_key) or [])
    key = analyst_id.strip()
    if key and key.casefold() not in {d.casefold() for d in disabled if isinstance(d, str)}:
        disabled.append(key)
    state[state_key] = disabled
    log = list(state.get("rollback_log") or [])
    log.append({
        "source": source,
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
    logger.info(
        "rag_rollback.recorded source=%s analyst=%s path=%s reasons=%r",
        source, key, p, list(reasons),
    )
    return p


# ---------------------------------------------------------------------------
# world_context — the legacy, byte-identical-behaviour wrappers
# ---------------------------------------------------------------------------


def world_context_disabled_units(*, state_path: str | None = None) -> frozenset[str]:
    """The set of analyst ids whose ``vector:world_context`` RAG is REVERTED off.

    ``source="world_context"`` wrapper over :func:`disabled_units_for` —
    unchanged env var (``LEGBA_WORLD_CONTEXT_DISABLED_UNITS``) and state key
    (``disabled_units``)."""
    return disabled_units_for(_LEGACY_SOURCE, state_path=state_path)


def is_world_context_enabled(analyst_id: str, *, state_path: str | None = None) -> bool:
    """True unless ``analyst_id`` has been rolled back off (env or persisted state).

    The grounding hook calls this to decide whether to honor a descriptor's
    ``vector:world_context`` source — so an auto-rollback (or an operator env pin)
    disables the RAG block in code, with no live descriptor PUT / redeploy."""
    return is_source_enabled(_LEGACY_SOURCE, analyst_id, state_path=state_path)


def record_rollback(
    analyst_id: str,
    *,
    state_path: str | None = None,
    reasons: Sequence[str] = (),
) -> str | None:
    """Persist ``analyst_id`` into the rollback state (the auto-rollback ACTUATOR).

    ``source="world_context"`` wrapper over :func:`record_rollback_for` — same
    state shape it always wrote (``disabled_units`` / ``rollback_log``)."""
    return record_rollback_for(
        _LEGACY_SOURCE, analyst_id, state_path=state_path, reasons=reasons
    )


# ---------------------------------------------------------------------------
# exemplar — the new corpus's own named wrappers ("like the others")
# ---------------------------------------------------------------------------

_EXEMPLAR_SOURCE = "exemplar"


def exemplar_disabled_units(*, state_path: str | None = None) -> frozenset[str]:
    """The set of analyst ids whose ``vector:exemplar`` RAG is REVERTED off."""
    return disabled_units_for(_EXEMPLAR_SOURCE, state_path=state_path)


def is_exemplar_enabled(analyst_id: str, *, state_path: str | None = None) -> bool:
    """True unless ``analyst_id`` has been rolled back off ``vector:exemplar``."""
    return is_source_enabled(_EXEMPLAR_SOURCE, analyst_id, state_path=state_path)
