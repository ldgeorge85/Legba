# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult route health -- read off ``consult_turns``, the table consults DO write.

THE GAP (2026-10-29 -> 11-11). A successful consult writes ``consult_turns`` and
never ``analyst_traces``; only a FAILED run leaves a trace (the actor's death
receipt). So ``get_run_health`` saw one failed trace for ``consult_default``
(run bd1a8e8a, 10-29) as its "last run" forever -- 15 later successful consults
were invisible -- and the journal reported "failed 303 hours ago and sits
quiet". Nothing alarmed on the failure either (see :mod:`.hard_fail_alert`).

THE CHOICE: read ``consult_turns`` rather than start writing a trace per
consult. A trace is a hash-chained receipt written inside the actor run; the
consult route's success path has its own persistence and is not the actor's
run body, so a trace-per-consult would add a second writer to a hot path.
``consult_turns`` already says everything health needs.

THE OUTCOME OF A CONSULT, per session mode (``chat`` -> ``consult_default``,
``deep`` -> ``deep_consult``):

  * ``succeeded`` -- an assistant turn with ``synthesis_status`` complete or
    partial (a partial answer was delivered; it is degraded, not failed);
  * ``failed``    -- an assistant turn with ``synthesis_status='none'`` (the
    failure path writes this row -- the 10-29 run did, at 21:23:23Z);
  * ``unfinished``-- a user turn with no later assistant turn in its session
    after a grace period (the run hung or the process died mid-consult).

Three things use that one read:

  * :func:`overlay_run_health` -- re-states the on-demand analysts' rows in
    ``get_run_health`` so an idle route reads ``idle_since <date>, last run
    succeeded`` (``run_state='idle_last_succeeded'``, not quiet) and a broken
    one reads ``failing``;
  * :func:`evaluate` / :func:`transition_kwargs` -- the alarm: N consecutive
    failed-or-unfinished turns raise one transition-edge alert on the
    watchdog's durable channel (``consult_unhealthy``), one ``recovered`` row
    when a turn succeeds.
  * the declared knobs: ``LEGBA_CONSULT_HEALTH_CONSECUTIVE_BAD`` (default 3) and
    ``LEGBA_CONSULT_HEALTH_UNFINISHED_GRACE_MIN`` (default 45, longer than the
    ~22 min the longest observed deep consult ran).

A leaf: stdlib only.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

ALERT_CHANNEL_CONSULT = "consult_unhealthy"

CONSECUTIVE_BAD_ENV = "LEGBA_CONSULT_HEALTH_CONSECUTIVE_BAD"
DEFAULT_CONSECUTIVE_BAD = 3
UNFINISHED_GRACE_ENV = "LEGBA_CONSULT_HEALTH_UNFINISHED_GRACE_MIN"
DEFAULT_UNFINISHED_GRACE_MIN = 45

#: session mode -> the analyst id whose run health it carries.
MODE_TO_ANALYST = {"chat": "consult_default", "deep": "deep_consult"}
ON_DEMAND_ANALYSTS = frozenset(MODE_TO_ANALYST.values())

LOOKBACK_DAYS = 30
OUTCOME_LIMIT = 60

OUTCOMES_SQL = f"""
    SELECT mode, at, outcome FROM (
      SELECT s.mode AS mode, t.created_at AS at,
             CASE WHEN t.synthesis_status = 'none' THEN 'failed'
                  ELSE 'succeeded' END AS outcome
        FROM public.consult_turns t
        JOIN public.consult_sessions s ON s.id = t.session_id
       WHERE t.role = 'assistant'
         AND t.created_at > now() - interval '{LOOKBACK_DAYS} days'
      UNION ALL
      SELECT s.mode, t.created_at, 'unfinished'
        FROM public.consult_turns t
        JOIN public.consult_sessions s ON s.id = t.session_id
       WHERE t.role = 'user'
         AND t.created_at > now() - interval '{LOOKBACK_DAYS} days'
         AND t.created_at < now() - ($1::int * interval '1 minute')
         AND NOT EXISTS (
               SELECT 1 FROM public.consult_turns a
                WHERE a.session_id = t.session_id AND a.role = 'assistant'
                  AND a.created_at > t.created_at)
    ) o
    ORDER BY at DESC
    LIMIT {OUTCOME_LIMIT}
"""


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def consecutive_bad_threshold() -> int:
    return _env_int(CONSECUTIVE_BAD_ENV, DEFAULT_CONSECUTIVE_BAD)


def unfinished_grace_minutes() -> int:
    return _env_int(UNFINISHED_GRACE_ENV, DEFAULT_UNFINISHED_GRACE_MIN)


async def fetch_outcomes(pg: Any) -> list[dict[str, Any]]:
    """Recent consult outcomes, newest first. Fail-safe: a read failure is no rows."""
    if pg is None:
        return []
    try:
        async with pg.acquire() as conn:
            rows = await conn.fetch(OUTCOMES_SQL, unfinished_grace_minutes())
    except Exception as exc:  # noqa: BLE001 -- health must not become a fault
        logger.warning("consult_health.lookup_failed err=%s", exc)
        return []
    return [dict(r) for r in rows]


def summarize(outcomes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pure: per analyst id -> newest outcome, its time, the consecutive-bad run
    length and ``run_state`` (``ok`` | ``failing`` | ``idle_last_succeeded``;
    the last two split on whether the newest outcome was bad)."""
    out: dict[str, dict[str, Any]] = {}
    for row in sorted(outcomes, key=lambda r: r["at"], reverse=True):
        analyst = MODE_TO_ANALYST.get(str(row.get("mode")))
        if analyst is None:
            continue
        entry = out.get(analyst)
        if entry is None:
            entry = out[analyst] = {
                "analyst_id": analyst,
                "last_outcome": row["outcome"],
                "last_at": row["at"],
                "consecutive_bad": 0,
                "_run_open": True,
            }
        if entry["_run_open"]:
            if row["outcome"] == "succeeded":
                entry["_run_open"] = False
            else:
                entry["consecutive_bad"] += 1
    for entry in out.values():
        entry.pop("_run_open", None)
        entry["run_state"] = (
            "idle_last_succeeded" if entry["last_outcome"] == "succeeded"
            else "failing"
        )
    return out


def evaluate(
    outcomes: list[dict[str, Any]], *, threshold: int | None = None
) -> list[dict[str, Any]]:
    """Pure: the analysts whose newest ``threshold`` outcomes are ALL bad."""
    need = max(1, threshold if threshold is not None else consecutive_bad_threshold())
    bad = [
        dict(e) for e in summarize(outcomes).values()
        if e["consecutive_bad"] >= need
    ]
    bad.sort(key=lambda e: e["analyst_id"])
    return bad


def alert_text(entry: dict[str, Any]) -> tuple[str, str]:
    analyst = entry["analyst_id"]
    n = int(entry["consecutive_bad"])
    last = entry["last_at"]
    title = f"Consult unhealthy: {n} consecutive failed or unfinished turns on {analyst}"
    body = (
        f"The last {n} consult turns on '{analyst}' ended failed (no synthesis) "
        f"or never finished; the newest at {last}. An operator asking a "
        "question is getting nothing back. Check the consult runtime's logs "
        "and the stack component the consult is routed to (a context-window "
        "refusal killed run bd1a8e8a on 2026-10-29). The alert clears when a "
        "consult turn completes."
    )
    return title, body


def transition_kwargs(entry: dict[str, Any]) -> dict[str, Any]:
    title, body = alert_text(entry)
    return {
        "channel": ALERT_CHANNEL_CONSULT,
        "entity_id": entry["analyst_id"],
        "state": "entered",
        "severity": "high",
        "kind": "consult_unhealthy",
        "title": title,
        "body": body,
        "extra": {
            "consecutive_bad": int(entry["consecutive_bad"]),
            "threshold": consecutive_bad_threshold(),
            "last_outcome": entry["last_outcome"],
            "page_budget_exempt": True,
        },
    }


async def overlay_run_health(
    pg: Any, result: dict[str, Any], *, analyst_id: str | None = None
) -> None:
    """Fold consult health into a ``get_run_health`` result, in place.

    Fleet read: each on-demand analyst's head row is restated from the newest
    consult outcome when that is newer than its trace (or appended when it has
    no trace). Every restated row carries ``on_demand=True``, ``run_state``
    and ``consecutive_bad``; an idle-but-last-succeeded route is NOT quiet and
    reads ``idle_since``. A per-analyst read just gets ``on_demand_health``.
    """
    summary = summarize(await fetch_outcomes(pg))
    if analyst_id is not None:
        if analyst_id in summary:
            result["on_demand_health"] = _public(summary[analyst_id])
        return
    now = datetime.now(timezone.utc)
    quiet = set(result.get("quiet_analysts") or [])
    rows = result.setdefault("rows", [])
    by_id = {r.get("analyst_id"): r for r in rows}
    for aid, entry in summary.items():
        at = entry["last_at"]
        hours = round((now - at).total_seconds() / 3600.0, 1)
        row = by_id.get(aid)
        if row is None:
            row = {"analyst_id": aid, "run_id": None, "cadence_trigger": "on_demand"}
            rows.append(row)
            result["count"] = len(rows)
        prev = row.get("last_run_at")
        if prev and datetime.fromisoformat(prev) > at:
            pass  # a newer trace wins; still stamp the state below
        else:
            row.update(
                status="failed" if entry["last_outcome"] != "succeeded" else "succeeded",
                had_error=entry["last_outcome"] != "succeeded",
                last_run_at=at.isoformat(),
                hours_ago=hours,
            )
        row.update(_public(entry), on_demand=True)
        if entry["run_state"] == "idle_last_succeeded":
            row["quiet"] = False
            row["idle_since"] = at.isoformat()
            quiet.discard(aid)
        else:
            row["quiet"] = False  # failing is its own state, not mere quiet
            quiet.discard(aid)
    result["quiet_analysts"] = sorted(quiet)


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_state": entry["run_state"],
        "last_outcome": entry["last_outcome"],
        "consecutive_bad": entry["consecutive_bad"],
    }


async def check_once(wd: Any, now_monotonic: float) -> list[str]:
    """The watchdog leg: alert on entry, one ``recovered`` row on exit, silence
    between. ``wd`` is the :class:`~.liveness_watchdog.LivenessWatchdog`; it owns
    the leader gate, the boot grace and the transition ledger. Returns the
    alerted analyst ids."""
    if wd._pg is None:
        return []
    if wd._is_leader is not None and not wd._is_leader():
        return []
    if (now_monotonic - wd._started_at) < wd._cfg.stall_after_s:
        return []
    bad = evaluate(await fetch_outcomes(wd._pg))
    states = await wd._get_alert_states(ALERT_CHANNEL_CONSULT)
    alerted: list[str] = []
    for entry in bad:
        if states.get(entry["analyst_id"]) == "entered":
            continue
        logger.error(
            "liveness_watchdog.consult_unhealthy analyst=%s consecutive=%d",
            entry["analyst_id"], entry["consecutive_bad"],
        )
        await wd._record_transition(**transition_kwargs(entry))
        alerted.append(entry["analyst_id"])
    await wd._emit_recoveries(
        channel=ALERT_CHANNEL_CONSULT,
        kind="consult_recovered",
        noun="Consult route",
        still_bad={e["analyst_id"] for e in bad},
    )
    return alerted
