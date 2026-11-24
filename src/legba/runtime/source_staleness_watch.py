# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The staleness TRIPWIRE — turns :mod:`.source_staleness` into an alert.

The liveness watchdog runs this on its own loop (throttled to
:data:`MIN_INTERVAL_S`). It reads the SAME rows the ``get_source_health`` tool
and the Source Health panel show (``rows[*].staleness``) — one source of
staleness, no parallel evaluation — and alerts on the STATE-TRANSITION EDGE
through the watchdog's existing durable path (``alert_sink_deliveries`` row,
``sink_kind='liveness_watchdog'``, plus the P1-1 outward fan-out and the core
NATS subject), exactly as the source cadence and degraded channels do.

TWO channels, so each tier fires ONCE per transition and a source that goes
stale and later dead alerts twice in its life (once per tier), never per poll:

* ``source_staleness_stale`` — enters on state ``stale`` (unless the feed is
  honestly quiet — :func:`.source_staleness.should_alert`), recovers when the
  source is back to ``fresh`` / ``quiet``.
* ``source_staleness_dead`` — enters on state ``dead``, recovers when it is
  back below ``dead``.

A source that jumps straight to ``dead`` fires only the dead alert. Absence is
not recovery: a source that turns ``unknown`` or leaves the active roster
keeps its open episode (no "recovered" is claimed on no evidence).

This module takes the watchdog as a duck-typed ``wd`` (it must not import
:mod:`.liveness_watchdog`, which imports THIS module's channel names).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import source_staleness as _st
from .substrate_source_health import get_source_health

logger = logging.getLogger(__name__)

ALERT_CHANNEL_STALE = "source_staleness_stale"
ALERT_CHANNEL_DEAD = "source_staleness_dead"
CHANNELS = (ALERT_CHANNEL_STALE, ALERT_CHANNEL_DEAD)

#: The watchdog loop ticks every ~60 s; staleness moves in hours.
MIN_INTERVAL_S = 300.0

_NOUN = {"stale": "Source stale", "dead": "Source dead"}


def _envelope(*, source_id: str, tier: str, title: str, body: str, factor: float) -> dict[str, Any]:
    """Same shape as the watchdog's other alert envelopes (the UI live feed and
    any operator sink render it identically)."""
    return {
        "kind": "alert",
        "severity": "high",
        "title": title,
        "body": body,
        "confidence": 1.0,
        "tags": ["watchdog", "liveness", "source_staleness", tier, source_id],
        "evidence": [],
        "routing_hint": "ops",
        "analyst_id": "system.liveness_watchdog",
        "analyst_version": None,
        "target_id": None,
        "target_version": None,
        "run_id": None,
        "stale_source_id": source_id,
        "staleness_factor": factor,
        "emitted_at": datetime.now(tz=timezone.utc).isoformat(),
    }


async def check_source_staleness_once(
    wd: Any, now_monotonic: float, *, min_interval_s: float = 0.0
) -> list[str]:
    """One tripwire pass. Returns ``["<source_id>:<tier>", ...]`` for every
    alert that fired this pass (entry edges only).

    Leader-gated and boot-graced like its siblings; ``min_interval_s`` throttles
    against ``wd._staleness_checked_at`` (the loop passes
    :data:`MIN_INTERVAL_S`; a direct call passes nothing and always runs).
    """
    if wd._pg is None:
        return []
    if wd._is_leader is not None and not wd._is_leader():
        return []
    if (now_monotonic - wd._started_at) < wd._cfg.stall_after_s:
        return []
    last = getattr(wd, "_staleness_checked_at", None)
    if last is not None and min_interval_s and (now_monotonic - last) < min_interval_s:
        return []
    wd._staleness_checked_at = now_monotonic

    health = await get_source_health(
        wd._pg, silent_only=False, include_body_extraction=False,
    )
    rows = health.get("rows") or []
    by_id = {r["source_id"]: r for r in rows}
    stale_states = await wd._get_alert_states(ALERT_CHANNEL_STALE)
    dead_states = await wd._get_alert_states(ALERT_CHANNEL_DEAD)

    alerted: list[str] = []
    for source_id, row in by_id.items():
        staleness = row.get("staleness") or {}
        tier = _st.should_alert(staleness)
        if tier is None:
            continue
        states = dead_states if tier == "dead" else stale_states
        if states.get(source_id) == "entered":
            continue  # ongoing — the entry edge already fired
        channel = ALERT_CHANNEL_DEAD if tier == "dead" else ALERT_CHANNEL_STALE
        factor = float(staleness.get("factor") or 0.0)
        title = f"{_NOUN[tier]}: {source_id} — {factor:.1f}× its expected interval"
        body = (
            f"Source '{source_id}' is ACTIVE but has delivered no new item for "
            f"{factor:.1f}× its expected interval "
            f"(stale ≥ {_st.STALE_FACTOR:g}×, dead ≥ {_st.DEAD_FACTOR:g}×). "
            f"{staleness.get('reason') or ''}"
        )
        logger.error(
            "liveness_watchdog.source_staleness source=%s tier=%s factor=%.1f "
            "— emitting alert", source_id, tier, factor,
        )
        await wd._publish_alert(
            _envelope(source_id=source_id, tier=tier, title=title, body=body, factor=factor)
        )
        await wd._record_transition(
            channel=channel,
            entity_id=source_id,
            state="entered",
            severity="high",
            kind=f"source_staleness_{tier}",
            title=title,
            body=body,
            extra={
                "staleness_state": staleness.get("state"),
                "factor": staleness.get("factor"),
                "last_new_item_at": staleness.get("last_new_item_at"),
                "upstream_quiet": staleness.get("upstream_quiet"),
            },
        )
        alerted.append(f"{source_id}:{tier}")

    # Recoveries. A source is still "bad" for a tier unless the evidence says
    # otherwise: unknown / absent-from-roster is NOT recovery.
    def _state(sid: str) -> str | None:
        r = by_id.get(sid)
        return None if r is None else (r.get("staleness") or {}).get("state")

    still_bad_stale = {
        sid for sid in set(stale_states) | set(by_id)
        if _state(sid) not in ("fresh", "quiet")
    }
    still_bad_dead = {
        sid for sid in set(dead_states) | set(by_id)
        if _state(sid) not in ("fresh", "quiet", "stale")
    }
    await wd._emit_recoveries(
        channel=ALERT_CHANNEL_STALE, kind="source_staleness_recovered",
        noun="Source", still_bad=still_bad_stale,
    )
    await wd._emit_recoveries(
        channel=ALERT_CHANNEL_DEAD, kind="source_staleness_recovered",
        noun="Source", still_bad=still_bad_dead,
    )
    return alerted


__all__ = ["ALERT_CHANNEL_STALE", "ALERT_CHANNEL_DEAD", "CHANNELS", "MIN_INTERVAL_S",
           "check_source_staleness_once"]
