# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L1 — DEAD LLM ROUTE detection, for the liveness watchdog.

THE INCIDENT THIS IS (2026-09-18 -> 09-20). OpenRouter removed
``mistralai/mistral-large-2512``. The standing external auditor's grader
component pointed at it, so every grader call 404'd. Nobody was paged for
**2.7 days**, and the reason is the whole design problem: a dead LLM route
does not look like silence.

  * The analyst kept running on its cadence, so the per-analyst cadence check
    in :mod:`.liveness_watchdog` saw a perfectly healthy analyst.
  * The fleet kept publishing, so the global stall check saw a healthy
    pipeline.
  * The auditor kept WRITING ROWS — verdicts of ``UNCHECKED`` with
    ``unchecked_reason='grader_unavailable'``, attributed to a family label
    ("grader: mistral") that was still technically true of a model that no
    longer existed.
  * The only trace of the actual fault was a ``WARNING`` log line per claim.

So the audit's own accuracy instrument produced 2.7 days of output that
measured nothing, and every observability surface in the tree reported green.

WHY THIS IS A LIVENESS CLASS AND NOT A QUALITY GAUGE. A quality gauge asks
"are the numbers good?"; this asks "is the instrument connected?". That makes
it exempt from the daily page budget (see
``_daily_page_budget.LIVENESS_EXEMPT_CLASSES``) — the budget exists to stop an
operator drowning in findings about the WORLD, and deferring "the thing
producing your findings is broken" is strictly worse than deferring any
finding, because the deferral is silent while the broken thing keeps writing.

WHAT IT MEASURES, AND WHY NOTHING NEW HAD TO BE RECORDED. The provider plane
already writes a per-call receipt: ``LLMProviderHandler._account_call`` appends
one entry per call to ``analyst_traces.llm_calls`` (JSONB array) carrying
``component_id`` — the stack ref the call was routed to — plus ``model``,
``status`` (``'success'``, else the mapped exception class) and, on an HTTP
failure, ``http_status``. Grouping that by component over a trailing window IS
the route's health; no new table, no new counter, no migration. The same
lateral-unnest shape :mod:`legba.data.registry.judge_stats_api` already uses.

EXTRACTED HERE rather than left inline in :mod:`.liveness_watchdog` because
that module sits under a pinned LOC ceiling (``tests/test_module_size_gate.py``)
and the gate's rule is to extract a cohesive unit at a real seam rather than
raise the number. This is that unit: the thresholds, the read, the pure
verdict and the alert prose. The watchdog keeps the parts that are ITS
business — the leader gate, the boot grace and the transition-edge ledger.

A leaf: stdlib only, no imports from the rest of the runtime package, so the
watchdog -> here edge never runs back.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Durable alert channel (``alert_sink_deliveries.channel_name``), paired with
#: ``sink_kind='liveness_watchdog'`` like every other watchdog channel.
ALERT_CHANNEL_LLM_ROUTE = "llm_route_dead"

# --- thresholds ------------------------------------------------------------
# A route must be BUSY before it can be called dead, and it must be failing
# ALMOST COMPLETELY. Both gates exist to keep this channel worth reading:
#
#   * ``min_calls`` — a component with three failed calls is noise. An alert
#     that fires on noise is an alert an operator learns to ignore, and the
#     value of this channel is that it has never cried wolf.
#   * ``success_floor`` — a floor, not "any failure". A hosted router throws
#     429s and transient 5xx all day; none of that is worth waking somebody.
#     Under 1 in 20 answering is not a bad afternoon, it is a route that does
#     not exist any more. The incident's own rate was 0.000.
MIN_CALLS_ENV = "LEGBA_LLM_ROUTE_MIN_CALLS"
DEFAULT_MIN_CALLS = 20
SUCCESS_FLOOR_ENV = "LEGBA_LLM_ROUTE_SUCCESS_FLOOR"
DEFAULT_SUCCESS_FLOOR = 0.05

#: Trailing window the check reads. 24 h matches the incident's own shape (an
#: upstream removal is permanent from the minute it lands) and keeps the
#: unindexed lateral over ``analyst_traces.llm_calls`` bounded — the same
#: bound every other receipt gauge in the tree takes.
WINDOW_HOURS = 24

#: Per-component call counts over the window. ``max(model)`` names the dead
#: model, which is the one fact that turns a page into a fix; the non-success
#: ``status`` and ``http_status`` name HOW it is dead (a 404 is a retired
#: model id; a 401 is a revoked key; a 429 at this rate is a throttle).
ROUTE_HEALTH_SQL = f"""
    SELECT c->>'component_id'                         AS component_id,
           count(*)::int                              AS calls,
           count(*) FILTER (WHERE c->>'status' = 'success')::int
                                                      AS successes,
           max(c->>'model')                           AS model,
           max(c->>'status') FILTER (WHERE c->>'status' <> 'success')
                                                      AS last_error,
           max(c->>'http_status') FILTER (WHERE c->>'status' <> 'success')
                                                      AS http_status
      FROM public.analyst_traces t
      CROSS JOIN LATERAL jsonb_array_elements(
             coalesce(t.llm_calls, '[]'::jsonb)) AS c
     WHERE t.run_started_at > now() - interval '{WINDOW_HOURS} hours'
       AND coalesce(c->>'component_id', '') <> ''
     GROUP BY 1
"""


async def fetch_route_health(pg: Any) -> list[dict[str, Any]]:
    """Per-component call counts over the trailing window, off the receipts.

    Fail-safe: a read failure returns NO rows (and logs), because the watchdog
    exists to observe faults, not to become one. The failure direction is
    deliberate — no verdict this pass beats a wrong verdict, and the next pass
    is 60 seconds away.
    """
    if pg is None:
        return []
    try:
        async with pg.acquire() as conn:
            rows = await conn.fetch(ROUTE_HEALTH_SQL)
    except Exception as exc:  # noqa: BLE001 — never kill the watchdog loop
        logger.warning(
            "llm_route_liveness.lookup_failed err=%s — no route liveness "
            "verdict this pass", exc,
        )
        return []
    return [dict(r) for r in rows]


def evaluate_dead_routes(
    rows: list[dict[str, Any]],
    *,
    min_calls: int,
    success_floor: float,
) -> list[dict[str, Any]]:
    """Pure: which components are BUSY and answering nothing. Sorted by id.

    ``success_rate`` is added to each returned row so the caller renders the
    same number the decision was made on rather than recomputing it — the
    alert body and the ledger row can never disagree about why it fired.
    """
    dead: list[dict[str, Any]] = []
    floor = float(success_floor)
    needed = max(1, int(min_calls))
    for row in rows:
        component = str(row.get("component_id") or "").strip()
        if not component:
            continue
        calls = int(row.get("calls") or 0)
        if calls < needed:
            continue
        successes = int(row.get("successes") or 0)
        rate = successes / float(calls)
        if rate >= floor:
            continue
        out = dict(row)
        out.update(
            component_id=component,
            calls=calls,
            successes=successes,
            success_rate=rate,
        )
        dead.append(out)
    dead.sort(key=lambda r: str(r["component_id"]))
    return dead


def alert_text(route: dict[str, Any]) -> tuple[str, str]:
    """``(title, body)`` for one dead route.

    The body names the component, the model, the arithmetic that fired, and —
    load-bearing — what an operator actually does about it, because the fix is
    a two-minute config change that is impossible to guess from "route dead".
    The last paragraph is the part the 09-18 incident earned: output graded
    through a dead route is not merely late, it is UNGRADED, and somebody has
    to know that before they trust the numbers.
    """
    component = str(route.get("component_id") or "")
    model = route.get("model") or "(model unrecorded)"
    calls = int(route.get("calls") or 0)
    successes = int(route.get("successes") or 0)
    rate = float(route.get("success_rate") or 0.0)
    last_error = route.get("last_error") or "(unclassified)"
    http = route.get("http_status")
    title = (
        f"LLM route dead: {component} answered {successes}/{calls} "
        f"in {WINDOW_HOURS}h"
    )
    body = (
        f"Stack component '{component}' (model '{model}') made {calls} calls "
        f"in the trailing {WINDOW_HOURS}h and only {successes} succeeded — a "
        f"{rate:.1%} success rate, under the floor. Newest non-success "
        f"status: {last_error}" + (f" (http {http})" if http else "") + ".\n\n"
        "This is a ROUTE fault, not an analyst fault: every analyst on this "
        "component is still running on cadence and still writing rows, so "
        "neither the global stall check nor the per-analyst cadence check can "
        "see it. The usual cause is a model id retired or renamed upstream — "
        "OpenRouter removed mistralai/mistral-large-2512 on 2026-09-18 and "
        "the standing external audit graded with nothing for 2.7 days before "
        "anyone noticed. Check the model id against the provider's live "
        "model list, repoint the stack component's config.model_name, and "
        "re-register it.\n\n"
        "Anything graded through this component since the window opened "
        "should be treated as UNGRADED, not as graded-and-inconclusive."
    )
    return title, body


def _env_number(name: str, default: float) -> float:
    """A tolerant env read — an unparsable or non-positive value keeps the
    default rather than silently disarming or over-arming the check."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "llm_route_liveness.bad_env %s=%r — using %s", name, raw, default
        )
        return default
    return value if value > 0 else default


async def dead_routes(pg: Any) -> list[dict[str, Any]]:
    """The whole verdict in one call: read the receipts, apply the live
    thresholds, return the routes that are busy and answering nothing."""
    return evaluate_dead_routes(
        await fetch_route_health(pg),
        min_calls=int(_env_number(MIN_CALLS_ENV, DEFAULT_MIN_CALLS)),
        success_floor=_env_number(SUCCESS_FLOOR_ENV, DEFAULT_SUCCESS_FLOOR),
    )


def log_dead_route(route: dict[str, Any]) -> None:
    """The LOUD log leg, at ERROR. The durable row is the operator surface;
    this is the one a log search finds, and it carries the component and the
    model because the 09-18 outage's only trace carried neither."""
    logger.error(
        "liveness_watchdog.llm_route_dead component=%s model=%s calls=%d "
        "successes=%d success_rate=%.3f last_error=%s — emitting alert",
        route["component_id"], route.get("model") or "?", route["calls"],
        route["successes"], route["success_rate"],
        route.get("last_error") or "?",
    )


def transition_kwargs(route: dict[str, Any]) -> dict[str, Any]:
    """The entry-edge transition for one dead route, ready to record.

    Severity ``high`` on the watchdog's own durable channel — the house's LOUD
    path (``alert_sink_deliveries``, ``sink_kind='liveness_watchdog'``), which
    both the escalations panel and the non-delivery canary already read.
    """
    title, body = alert_text(route)
    return {
        "channel": ALERT_CHANNEL_LLM_ROUTE,
        "entity_id": route["component_id"],
        "state": "entered",
        "severity": "high",
        "kind": "llm_route_dead",
        "title": title,
        "body": body,
        "extra": ledger_extra(route),
    }


def ledger_extra(route: dict[str, Any]) -> dict[str, Any]:
    """The ``payload_summary`` fields the durable ledger row carries.

    The arithmetic that fired, so a row read months later re-derives the
    decision without the log. ``page_budget_exempt`` is the assertion that
    this is a LIVENESS class — see
    ``_daily_page_budget.LIVENESS_EXEMPT_CLASSES``: the daily page budget caps
    how much of the WORLD an operator is asked to read, and must never defer
    the alert that says the instrument reading it is broken.
    """
    return {
        "component_id": route["component_id"],
        "model": route.get("model") or "",
        "calls": route["calls"],
        "successes": route["successes"],
        "success_rate": round(float(route["success_rate"]), 4),
        "window_hours": WINDOW_HOURS,
        "page_budget_exempt": True,
    }


__all__ = [
    "ALERT_CHANNEL_LLM_ROUTE",
    "DEFAULT_MIN_CALLS",
    "DEFAULT_SUCCESS_FLOOR",
    "MIN_CALLS_ENV",
    "ROUTE_HEALTH_SQL",
    "SUCCESS_FLOOR_ENV",
    "WINDOW_HOURS",
    "alert_text",
    "dead_routes",
    "evaluate_dead_routes",
    "ledger_extra",
    "log_dead_route",
    "transition_kwargs",
    "fetch_route_health",
]
