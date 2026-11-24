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

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

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


# ===========================================================================
# JUDGE-2 (W11b) — a HOSTED route's auth/credit failure pages AT ONCE
# ===========================================================================
#
# THE INCIDENT. OpenRouter answered 402 (out of credits) from 2026-10-02 23:08Z
# to 10-05 02:03Z. The L1 check above fired at 10-03 13:07Z — 14 h in — because
# it is built for a different question ("is a BUSY route answering almost
# nothing over a day?") and must wait for volume and a 24 h window before it
# can say so without crying wolf. A 401/402/403/404 from a HOSTED provider does
# not need volume to mean something: ONE of them says the account, the key or
# the model id is gone, and no retry will bring it back. So this check reads the
# receipts written SINCE THE LAST TICK and pages on the first such call.
#
# HOSTED vs IN-HOUSE. An in-house host's 4xx is a deploy problem the operator is
# already looking at, not a bill. A component is IN-HOUSE when its endpoint host
# ends with one of the suffixes in ``LEGBA_LLM_INHOUSE_HOSTS`` (comma-separated).
# Unset, the default is DERIVED, never hardcoded: the core plane component's
# (``llm.primary.openai_compat``) endpoint host with its first label dropped —
# the in-house host family. A component with no readable endpoint is HOSTED
# (fail loud: an unknown route returning 402 is worth a page).
#
# THE EDGE. One durable ``state='entered'`` row per component (channel
# ``llm_route_auth_failed``, severity high, ``page_budget_exempt``), through the
# watchdog's own transition ledger and ntfy fan-out — at most once per component
# per :data:`AUTH_PAGE_DEDUP_HOURS` (a still-failing route re-pages after the
# window; a flapping one does not). The exit edge is the first SUCCESS on that
# component newer than its last auth failure.

#: Durable channel + kind for the at-once page.
ALERT_CHANNEL_LLM_ROUTE_AUTH = "llm_route_auth_failed"
#: Comma-separated host suffixes counted IN-HOUSE (never paged on this channel).
INHOUSE_HOSTS_ENV = "LEGBA_LLM_INHOUSE_HOSTS"
#: The component whose host family is the in-house default when the env is unset.
INHOUSE_ANCHOR_COMPONENT = "llm.primary.openai_compat"
#: One page per component per this many hours (a reminder if it persists).
AUTH_PAGE_DEDUP_ENV = "LEGBA_LLM_AUTH_PAGE_DEDUP_HOURS"
DEFAULT_AUTH_PAGE_DEDUP_HOURS = 6.0
#: Receipts land when a RUN ends, not when the call is made, so "since the last
#: tick" reads calls made up to this many seconds before it (re-reading is
#: harmless — the ledger de-duplicates). Matches the longest analyst timeout.
AUTH_RECEIPT_LAG_ENV = "LEGBA_LLM_AUTH_RECEIPT_LAG_S"
DEFAULT_AUTH_RECEIPT_LAG_S = 3600.0

#: Per component, over calls made after ``$1``: the newest success, the newest
#: route-gone failure (http 401/402/403/404 on a non-success receipt), and the
#: FIRST route-gone failure after the newest success (the episode's start).
#: ``run_started_at`` bounds the unindexed lateral (a run that made a call after
#: ``$1`` started at most an hour of runtime before it).
AUTH_FAILED_SQL = """
WITH win AS (
    SELECT c->>'component_id'                         AS component_id,
           c->>'model'                                AS model,
           c->>'status'                               AS status,
           c->>'http_status'                          AS http_status,
           (c->>'at')::timestamptz                    AS at
      FROM public.analyst_traces t
      CROSS JOIN LATERAL jsonb_array_elements(
             coalesce(t.llm_calls, '[]'::jsonb)) AS c
     WHERE t.run_started_at > $1::timestamptz - interval '1 hour'
       AND coalesce(c->>'component_id', '') <> ''
       AND c->>'at' IS NOT NULL
       AND (c->>'at')::timestamptz > $1::timestamptz
),
gone AS (
    SELECT *, (status IS DISTINCT FROM 'success'
               AND http_status IN ('401', '402', '403', '404')) AS is_gone
      FROM win
),
ok AS (
    SELECT component_id, max(at) FILTER (WHERE status = 'success') AS last_success_at
      FROM gone GROUP BY 1
)
SELECT g.component_id,
       ok.last_success_at,
       max(g.model) FILTER (WHERE g.is_gone)              AS model,
       max(g.at) FILTER (WHERE g.is_gone)                 AS last_failure_at,
       min(g.at) FILTER (WHERE g.is_gone AND (ok.last_success_at IS NULL
                                 OR g.at > ok.last_success_at)) AS first_failure_at,
       count(*) FILTER (WHERE g.is_gone AND (ok.last_success_at IS NULL
                                 OR g.at > ok.last_success_at))::int AS failures,
       max(g.http_status) FILTER (WHERE g.is_gone)        AS http_status
  FROM gone g JOIN ok USING (component_id)
 GROUP BY g.component_id, ok.last_success_at
"""

#: The endpoint config of the components in play (heads only).
COMPONENT_ENDPOINTS_SQL = """
    SELECT component_id, body->'config' AS config
      FROM public.stack_components
     WHERE is_head AND component_id = ANY($1::text[])
"""

#: The newest 'entered' page per component inside the de-dup window — the
#: restart-safe half of "once per component per 6 h".
RECENT_AUTH_PAGES_SQL = """
    SELECT sink_target, max(attempted_at) AS entered_at
      FROM public.alert_sink_deliveries
     WHERE sink_kind = 'liveness_watchdog'
       AND channel_name = $1
       AND payload_summary->>'state' = 'entered'
       AND attempted_at > $2::timestamptz
     GROUP BY 1
"""


def endpoint_host(config: Any) -> str | None:
    """The lower-cased host of a component's ``api_endpoint`` / ``base_url``."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            return None
    if not isinstance(config, dict):
        return None
    for key in ("api_endpoint", "base_url", "endpoint"):
        value = config.get(key)
        if isinstance(value, dict):
            value = value.get("raw")
        if isinstance(value, str) and value.strip():
            host = urlparse(value.strip()).hostname
            if host:
                return host.lower()
    return None


def inhouse_suffixes(anchor_host: str | None) -> tuple[str, ...]:
    """The in-house host suffixes: the env list, else the anchor's host family
    (its host with the first label dropped; a bare two-label host stays whole)."""
    raw = os.getenv(INHOUSE_HOSTS_ENV)
    if raw is not None:
        return tuple(s.strip().lower().lstrip(".") for s in raw.split(",") if s.strip())
    if not anchor_host:
        return ()
    labels = anchor_host.split(".")
    return (".".join(labels[1:]) if len(labels) > 2 else anchor_host,)


def is_hosted(host: str | None, suffixes: tuple[str, ...]) -> bool:
    """False iff ``host`` is (or is under) an in-house suffix. Unknown → hosted."""
    if not host:
        return True
    return not any(host == s or host.endswith("." + s) for s in suffixes)


def evaluate_auth_failures(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Pure: ``(failing, cleared)``. FAILING = the newest route-gone failure is
    newer than the newest success; CLEARED = a success is the newest word."""
    failing: list[dict[str, Any]] = []
    cleared: set[str] = set()
    for row in rows:
        component = str(row.get("component_id") or "").strip()
        if not component:
            continue
        ok_at = row.get("last_success_at")
        bad_at = row.get("last_failure_at")
        if bad_at is not None and (ok_at is None or bad_at > ok_at):
            out = dict(row)
            out["component_id"] = component
            failing.append(out)
        elif ok_at is not None:
            cleared.add(component)
    failing.sort(key=lambda r: r["component_id"])
    return failing, cleared


def auth_alert_text(row: dict[str, Any], *, reminder: bool = False) -> tuple[str, str]:
    """``(title, body)`` naming the component, the model, the status and the
    first failing call's time — and what an operator does about each status."""
    component = row["component_id"]
    model = row.get("model") or "(model unrecorded)"
    http = str(row.get("http_status") or "?")
    first = row.get("first_failure_at") or row.get("last_failure_at")
    first_s = first.isoformat() if hasattr(first, "isoformat") else str(first)
    n = int(row.get("failures") or 0)
    meaning = {
        "401": "the API key was refused (revoked, rotated or missing)",
        "402": "the account is OUT OF CREDIT",
        "403": "the provider refused the request (policy, region or key scope)",
        "404": "the model id is no longer served (retired or renamed upstream)",
    }.get(http, "the route refused the call")
    title = (
        f"LLM route {'STILL ' if reminder else ''}refusing: {component} "
        f"http {http} ({model})"
    )
    body = (
        f"Stack component '{component}' (model '{model}', a HOSTED route) "
        f"answered HTTP {http} — {meaning}. First failing call in this "
        f"episode: {first_s}; {n} refused call(s) seen since the last success.\n\n"
        "This is paged at ONCE because no retry fixes it: a 402 needs a top-up, "
        "a 401/403 a key, a 404 a repoint of config.model_name. While it lasts, "
        "every check routed here falls down its judge fallback chain (if one is "
        "declared) or to the deterministic floor — critiques written in the "
        "meantime carry data.verification.unverified_reason and are NOT "
        "re-judged automatically (scripts/rejudge_floor_critiques.py --dry-run "
        "lists them; --apply --limit N re-judges them once a rung answers)."
        "\n\nThe episode clears on the first successful call on this component."
    )
    return title, body


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


async def _endpoints(pg: Any, components: list[str]) -> dict[str, str | None]:
    ids = sorted(set(components) | {INHOUSE_ANCHOR_COMPONENT})
    async with pg.acquire() as conn:
        rows = await conn.fetch(COMPONENT_ENDPOINTS_SQL, ids)
    return {str(r["component_id"]): endpoint_host(r["config"]) for r in rows}


async def _recent_pages(wd: Any, now: Any, dedup_s: float) -> dict[str, Any]:
    """In-memory map of component → newest 'entered' page time, seeded once per
    process from the ledger (restart-safe de-dup)."""
    seen =getattr(wd, "_llm_auth_paged_at", None)
    if seen is not None:
        return seen
    seen = {}
    try:
        async with wd._pg.acquire() as conn:
            rows = await conn.fetch(
                RECENT_AUTH_PAGES_SQL, ALERT_CHANNEL_LLM_ROUTE_AUTH,
                now - timedelta(seconds=dedup_s),
            )
        seen = {str(r["sink_target"]): r["entered_at"] for r in rows}
    except Exception as exc:  # noqa: BLE001 — a seed failure may re-page once
        logger.warning("llm_route_liveness.auth_seed_failed err=%s", exc)
    wd._llm_auth_paged_at = seen
    return seen


async def check_auth_failed_once(wd: Any, *, now: Any = None) -> list[str]:
    """One pass of the at-once page. Returns the components paged this pass.

    Leader-gated like its siblings; NO boot grace (one refused call is a
    verdict, and a restart mid-outage is held by the ledger). Fail-safe: a read
    failure is no verdict this pass, never a crash.
    """
    if wd._pg is None:
        return []
    if wd._is_leader is not None and not wd._is_leader():
        return []
    now = now or datetime.now(timezone.utc)
    lag = _env_number(AUTH_RECEIPT_LAG_ENV, DEFAULT_AUTH_RECEIPT_LAG_S)
    dedup_s = _env_number(AUTH_PAGE_DEDUP_ENV, DEFAULT_AUTH_PAGE_DEDUP_HOURS) * 3600.0
    last_tick = getattr(wd, "_llm_auth_checked_at", None) or now
    since = last_tick - timedelta(seconds=lag)
    try:
        async with wd._pg.acquire() as conn:
            rows = [dict(r) for r in await conn.fetch(AUTH_FAILED_SQL, since)]
        failing, cleared = evaluate_auth_failures(rows)
        hosts = await _endpoints(wd._pg, [r["component_id"] for r in failing]) if failing else {}
    except Exception as exc:  # noqa: BLE001 — never kill the watchdog loop
        logger.warning(
            "llm_route_liveness.auth_lookup_failed err=%s — no auth verdict this pass", exc,
        )
        return []
    wd._llm_auth_checked_at = now
    suffixes = inhouse_suffixes(hosts.get(INHOUSE_ANCHOR_COMPONENT))
    states = await wd._get_alert_states(ALERT_CHANNEL_LLM_ROUTE_AUTH)
    paged_at = await _recent_pages(wd, now, dedup_s)
    paged: list[str] = []
    for row in failing:
        component = row["component_id"]
        if not is_hosted(hosts.get(component), suffixes):
            continue
        last = paged_at.get(component)
        if last is not None and (now - last).total_seconds() < dedup_s:
            continue
        reminder = states.get(component) == "entered"
        title, body = auth_alert_text(row, reminder=reminder)
        logger.error(
            "liveness_watchdog.llm_route_auth_failed component=%s model=%s "
            "http_status=%s first_failure_at=%s — paging at once",
            component, row.get("model") or "?", row.get("http_status"),
            _iso(row.get("first_failure_at")),
        )
        await wd._record_transition(
            channel=ALERT_CHANNEL_LLM_ROUTE_AUTH, entity_id=component,
            state="entered", severity="high", kind="llm_route_auth_failed",
            title=title, body=body,
            extra={
                "component_id": component,
                "model": row.get("model") or "",
                "http_status": str(row.get("http_status") or ""),
                "first_failure_at": _iso(row.get("first_failure_at")),
                "failures": int(row.get("failures") or 0),
                "reminder": reminder,
                "page_budget_exempt": True,
            },
        )
        paged_at[component] = now
        paged.append(component)
    for component in sorted(cleared):
        if states.get(component) != "entered":
            continue
        await wd._record_transition(
            channel=ALERT_CHANNEL_LLM_ROUTE_AUTH, entity_id=component,
            state="recovered", severity="info", kind="llm_route_auth_recovered",
            title=f"LLM route answering again: {component}",
            body=(
                f"Stack component '{component}' answered a call successfully after "
                "its auth/credit failure — the episode is closed. Critiques written "
                "during it keep their unverified_reason."
            ),
        )
    return paged


__all__ = [
    "ALERT_CHANNEL_LLM_ROUTE",
    "ALERT_CHANNEL_LLM_ROUTE_AUTH",
    "AUTH_FAILED_SQL",
    "AUTH_PAGE_DEDUP_ENV",
    "AUTH_RECEIPT_LAG_ENV",
    "COMPONENT_ENDPOINTS_SQL",
    "DEFAULT_AUTH_PAGE_DEDUP_HOURS",
    "DEFAULT_AUTH_RECEIPT_LAG_S",
    "INHOUSE_ANCHOR_COMPONENT",
    "INHOUSE_HOSTS_ENV",
    "RECENT_AUTH_PAGES_SQL",
    "auth_alert_text",
    "check_auth_failed_once",
    "endpoint_host",
    "evaluate_auth_failures",
    "inhouse_suffixes",
    "is_hosted",
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
