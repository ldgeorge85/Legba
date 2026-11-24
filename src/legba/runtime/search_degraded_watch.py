# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``search_degraded`` page — the search plane answering degraded (X1/B5).

WHY A PAGE. A search that loses engines still returns HTTP 200 with results, so
every check on the watchdog loop reads it as health. On this deployment one
SearXNG engine (reuters) failed on EVERY query in the 10-05 → 10-08 runtime log
(3,030 ``web_search.degraded`` lines over ~3,029 searches), which made every
external-audit NOT_FOUND since 10-02 a degraded search — 70 of 70 sampled
``searched_found_nothing`` absences on three desks were really
``search_failed`` (W11-F15). Nothing paged; a reviewer found it.

THE MEASURE. Over the trailing :data:`WINDOW_HOURS`, per search provider: the
share of the external audit's primary NOT_FOUND grades whose search the ONE
rule (:func:`legba.data.registry.search_health.read_search_health`) calls
degraded. The page fires when that share is ABOVE
:data:`PAGE_SHARE_ENV` (default 0.20) on at least :data:`PAGE_MIN_NOT_FOUND_ENV`
(default 20) NOT_FOUND grades — a thin window is no verdict.

THE EDGE. The watchdog's transition-edge ledger idiom (B0-12): ONE durable
``alert_sink_deliveries`` row (``sink_kind='liveness_watchdog'``, channel
``search_degraded``, ``sink_target`` = the provider component id) plus the
outward fan-out when a provider ENTERS the state, one ``recovered`` row when it
leaves, silence in between, restart-safe through the ledger seed. A provider
whose window falls below the minimum while it is ``entered`` is held, not
recovered: too few grades is no verdict either way.

Fail-safe: a read failure is no verdict this pass, never a crash — this module
catches its own errors so the watchdog loop spends one line on it.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from ..data.registry.search_health import (
    SEARCH_HEALTH_WINDOW_SQL,
    degraded_min_unresponsive,
    summarize,
)

logger = logging.getLogger(__name__)

ALERT_CHANNEL_SEARCH_DEGRADED = "search_degraded"
WINDOW_HOURS = 24

#: THE TUNABLES (docs/TUNABLES.md §5).
PAGE_SHARE_ENV = "LEGBA_SEARCH_DEGRADED_PAGE_SHARE"
DEFAULT_PAGE_SHARE = 0.20
PAGE_MIN_NOT_FOUND_ENV = "LEGBA_SEARCH_DEGRADED_PAGE_MIN_NOT_FOUND"
DEFAULT_PAGE_MIN_NOT_FOUND = 20


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("search_degraded_watch.bad_env %s=%r — using %s", name, raw, default)
        return default
    return value if value > 0 else default


def evaluate(
    summary: Mapping[str, Any], *, share_floor: float, min_not_found: int,
) -> tuple[list[dict[str, Any]], set[str]]:
    """``(paging, thin)`` — the providers over the floor on enough NOT_FOUND
    grades, and the providers whose window held too few to judge."""
    paging: list[dict[str, Any]] = []
    thin: set[str] = set()
    for p in summary.get("providers") or []:
        if int(p.get("not_found") or 0) < min_not_found:
            thin.add(str(p["provider"]))
            continue
        share = p.get("not_found_degraded_share")
        if share is not None and float(share) > share_floor:
            paging.append(dict(p))
    return paging, thin


def alert_text(
    provider: Mapping[str, Any], summary: Mapping[str, Any], *, share_floor: float,
) -> tuple[str, str]:
    share = float(provider.get("not_found_degraded_share") or 0.0)
    name = provider["provider"]
    title = (
        f"Search plane degraded: {name} — {share:.0%} of NOT_FOUND grades in "
        f"{WINDOW_HOURS} h ran on a degraded search"
    )
    engines = [
        e for e in (summary.get("engines") or []) if int(e.get("errored") or 0) > 0
    ][:4]
    engine_line = "; ".join(
        f"{e['engine']} {float(e.get('error_share') or 0.0):.0%} "
        f"({', '.join(sorted(e.get('errors') or {})) or 'reason unstated'})"
        for e in engines
    ) or "the engine list is not recorded on these rows (pre-0235 grades)"
    body = (
        f"{provider['not_found_degraded']} of {provider['not_found']} external-audit "
        f"NOT_FOUND grades on '{name}' in the last {WINDOW_HOURS} h ran on a "
        f"search the search-health rule calls DEGRADED (threshold: "
        f"{summary.get('threshold')} unresponsive engine(s); page floor "
        f"{share_floor:.0%}). Those absences now read as search_failed, not "
        "'searched, found nothing' — the audit is not able to rule claims out "
        f"while this holds. Failing engines (error share over recorded "
        f"searches): {engine_line}. Read /v3/system/search-health; the fix is "
        "the engine set (docs/TUNABLES.md §1, SW-3: drop or re-weight the "
        "failing engine in docker/searxng/settings.yml)."
    )
    return title, body


async def check_search_degraded_once(
    wd: Any, *, now: Optional[datetime] = None,
) -> list[str]:
    """One pass. Returns the providers whose ENTRY edge fired this pass.

    Leader-gated like its siblings; no boot grace (the window is read from the
    ledger, not from what this process has seen). Never raises.
    """
    try:
        if wd._pg is None:
            return []
        if wd._is_leader is not None and not wd._is_leader():
            return []
        now = now or datetime.now(timezone.utc)
        share_floor = _env_float(PAGE_SHARE_ENV, DEFAULT_PAGE_SHARE)
        min_nf = int(_env_float(PAGE_MIN_NOT_FOUND_ENV, DEFAULT_PAGE_MIN_NOT_FOUND))
        async with wd._pg.acquire() as conn:
            rows = await conn.fetch(
                SEARCH_HEALTH_WINDOW_SQL, now - timedelta(hours=WINDOW_HOURS)
            )
        summary = summarize(
            [dict(r) for r in rows], threshold=degraded_min_unresponsive()
        )
        paging, thin = evaluate(summary, share_floor=share_floor, min_not_found=min_nf)
        states = await wd._get_alert_states(ALERT_CHANNEL_SEARCH_DEGRADED)
        fired: list[str] = []
        for p in paging:
            name = str(p["provider"])
            if states.get(name) == "entered":
                continue  # ongoing — the entry edge already fired
            title, body = alert_text(p, summary, share_floor=share_floor)
            logger.error(
                "liveness_watchdog.search_degraded provider=%s not_found=%d "
                "degraded=%d share=%s", name, p["not_found"],
                p["not_found_degraded"], p["not_found_degraded_share"],
            )
            await wd._record_transition(
                channel=ALERT_CHANNEL_SEARCH_DEGRADED, entity_id=name,
                state="entered", severity="high", kind="search_degraded",
                title=title, body=body,
                extra={
                    "provider": name,
                    "not_found": p["not_found"],
                    "not_found_degraded": p["not_found_degraded"],
                    "not_found_degraded_share": p["not_found_degraded_share"],
                    "threshold": summary.get("threshold"),
                    "page_share_floor": share_floor,
                    "window_hours": WINDOW_HOURS,
                    "as_of": summary.get("as_of"),
                    "page_budget_exempt": True,
                },
            )
            fired.append(name)
        held = {n for n, st in states.items() if st == "entered" and n in thin}
        await wd._emit_recoveries(
            channel=ALERT_CHANNEL_SEARCH_DEGRADED, kind="search_degraded_recovered",
            noun="Search provider",
            still_bad={str(p["provider"]) for p in paging} | held,
        )
        return fired
    except Exception as exc:  # noqa: BLE001 — never kill the watchdog loop
        logger.warning("liveness_watchdog.search_degraded_check_error err=%s", exc)
        return []


__all__ = [
    "ALERT_CHANNEL_SEARCH_DEGRADED",
    "DEFAULT_PAGE_MIN_NOT_FOUND",
    "DEFAULT_PAGE_SHARE",
    "PAGE_MIN_NOT_FOUND_ENV",
    "PAGE_SHARE_ENV",
    "WINDOW_HOURS",
    "alert_text",
    "check_search_degraded_once",
    "evaluate",
]
