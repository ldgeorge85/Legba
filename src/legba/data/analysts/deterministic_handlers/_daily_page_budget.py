# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D2 — the 90-day product wager (2026-08-29): the fleet-wide daily page
budget. Extracted from :mod:`.alert_trigger_scan` (module-size gate) the same
way triggers 1 and 5-8 already are. Re-exported into
:mod:`.alert_trigger_scan`'s own namespace at import time so
``ats.apply_daily_page_budget`` / ``ats.budget_magnitude_tier`` /
``ats.DEFAULT_DAILY_PAGE_BUDGET`` / ``ats._bool_env`` /
``ats._daily_page_budget_from_env`` keep working exactly as before.

Applied AFTER the FRAME-3 steady-state guard and the per-desk cap, on
whatever survives both: at most :data:`DEFAULT_DAILY_PAGE_BUDGET` alerts (env
``LEGBA_ALERT_DAILY_PAGE_BUDGET`` / descriptor option ``daily_page_budget``)
actually PAGE per UTC calendar day, fleet-wide, across every trigger class.
Survivors are ranked worst-first by severity, then by
:func:`budget_magnitude_tier` (an actual crossing beats a verified_finding's
own rise/fall tag, which beats a steady-tag heartbeat) and only the day's
remaining slots are fanned out; the rest still write their ``kind='alert'``
row, tagged ``data.budget_deferred=true`` — the SAME observable-suppression
idiom the FRAME-3 guard uses, never a silent drop.

The trigger-class name STRINGS below are literal copies of
:mod:`.alert_trigger_scan`'s own ``TRIGGER_*`` constants, not a reverse
import — these are treated as a stable, documented "open vocabulary" (see
migration 0091's own comment) that has not changed across five prior
extraction refactors (geo_convergence's 2026-07-29 fold deliberately KEPT its
identical trigger_class string specifically because changing it would break
watermark continuity), so duplicating them here as literals avoids paying a
reverse-import for values that are, in practice, frozen identifiers.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Mapping, Optional

if TYPE_CHECKING:  # pragma: no cover — type-checking only, no runtime import
    from .alert_trigger_scan import AlertCandidate

logger = logging.getLogger(__name__)

#: Fleet-wide hard cap on ACTUAL pages (fan-outs) per UTC calendar day, across
#: every trigger class, applied AFTER every other filter (the FRAME-3 guard,
#: the per-desk cap). Env ``LEGBA_ALERT_DAILY_PAGE_BUDGET`` / descriptor
#: option ``daily_page_budget``.
DEFAULT_DAILY_PAGE_BUDGET = 5
DAILY_PAGE_BUDGET_ENV = "LEGBA_ALERT_DAILY_PAGE_BUDGET"

#: KIND-DIVERSITY CAP (addendum, 2026-08-29) — no single trigger_class may
#: take more than this many of the day's budget slots, CUMULATIVE across the
#: whole UTC day (not per scan — see :func:`count_paged_today_by_kind`).
#: Answers the fleet-wide-budget starvation the replay found: with the plain
#: budget alone, ``situation_escalation`` (100% ``severity=critical`` in the
#: soak window) wins every slot every day and verified_finding/band_crossing
#: never page. Env ``LEGBA_ALERT_BUDGET_PER_KIND_CAP`` / descriptor option
#: ``budget_per_kind_cap``.
DEFAULT_BUDGET_PER_KIND_CAP = 3
BUDGET_PER_KIND_CAP_ENV = "LEGBA_ALERT_BUDGET_PER_KIND_CAP"

#: Kill-list env vars — both default OFF (``false``); the classes' scans keep
#: running and their watermarks keep advancing (see ``advance_watermarks_only``
#: below), only the write+page is withheld.
CONTENTION_FLIP_ENABLED_ENV = "LEGBA_ALERT_CONTENTION_FLIP_ENABLED"
GEO_CONVERGENCE_ENABLED_ENV = "LEGBA_ALERT_GEO_CONVERGENCE_ENABLED"

# Literal mirrors of alert_trigger_scan's TRIGGER_* constants — see the
# module docstring above for why these are copies rather than a reverse
# import.
_TRIGGER_BAND = "band_crossing"
_TRIGGER_FINDING = "verified_finding"
_TRIGGER_CONTENTION = "contention_flip"
_TRIGGER_BASELINE = "baseline_deviation"
_TRIGGER_WATCHLIST = "watchlist_hit"
_TRIGGER_GEO_CONVERGENCE = "geo_convergence"
_TRIGGER_PRODUCTION_DEFICIT = "production_deficit"
_TRIGGER_SITUATION_ESCALATION = "situation_escalation"

_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

#: Every trigger class besides ``verified_finding`` fires ONLY on a detected
#: transition (each already carries its own no-refire watermark; none of them
#: has a "steady" repeat mode), so all of them rank as an ACTUAL crossing
#: alongside band_crossing's own ladder crossing. See
#: :func:`budget_magnitude_tier`.
_BUDGET_CROSSING_CLASSES = frozenset(
    {
        _TRIGGER_BAND,
        _TRIGGER_CONTENTION,
        _TRIGGER_BASELINE,
        _TRIGGER_WATCHLIST,
        _TRIGGER_GEO_CONVERGENCE,
        _TRIGGER_PRODUCTION_DEFICIT,
        _TRIGGER_SITUATION_ESCALATION,
    }
)


def bool_env(name: str, default: bool) -> bool:
    """A tolerant boolean env-var read — never raises, unknown/missing values
    fall back to ``default`` rather than being guessed."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def daily_page_budget_from_env() -> int:
    raw = os.environ.get(DAILY_PAGE_BUDGET_ENV)
    if raw is None:
        return DEFAULT_DAILY_PAGE_BUDGET
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_PAGE_BUDGET
    return val if val >= 0 else DEFAULT_DAILY_PAGE_BUDGET


def budget_per_kind_cap_from_env() -> int:
    raw = os.environ.get(BUDGET_PER_KIND_CAP_ENV)
    if raw is None:
        return DEFAULT_BUDGET_PER_KIND_CAP
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_PER_KIND_CAP
    return val if val >= 0 else DEFAULT_BUDGET_PER_KIND_CAP


def budget_magnitude_tier(cand: "AlertCandidate") -> int:
    """Secondary D2 budget-ranking key (higher = more urgent), applied AFTER
    severity band. A direct reading of the wager's two named comparisons —
    "band crossing beats within-band" and "severity_delta rise/fall beats
    steady":

      * **2 — an ACTUAL crossing.** ``band_crossing``'s own ladder crossing,
        plus every other trigger class besides ``verified_finding`` (all six
        fire only on a detected transition — see
        :data:`_BUDGET_CROSSING_CLASSES`).
      * **1 — within-band movement, or a rollup.** A ``verified_finding``
        candidate whose OWN ``severity_delta`` tag claims explicit movement
        (``rose`` / ``fell`` / ``new``) without its coarse band crossing; a
        per-desk rollup (a synthetic aggregate — ranked in the middle rather
        than assumed either extreme).
      * **0 — a heartbeat.** A ``verified_finding`` candidate tagged
        ``steady`` or carrying no ``severity_delta`` tag at all, paging only
        because it is the desk's first-ever page or its cooldown elapsed.
    """
    if cand.trigger_class in _BUDGET_CROSSING_CLASSES:
        return 2
    if cand.trigger_class == "rollup":
        return 1
    if cand.trigger_class == _TRIGGER_FINDING:
        delta = cand.data.get("severity_delta_tag")
        return 1 if delta in ("rose", "fell", "new") else 0
    return 1  # defensive default for any future trigger class


def budget_sort_key(cand: "AlertCandidate") -> tuple[int, int, str]:
    """Worst-first D2 ranking: severity, then :func:`budget_magnitude_tier`,
    then title (deterministic tie-break — no candidate is silently
    reordered run to run for equal-priority items)."""
    return (
        -_SEVERITY_RANK.get(cand.severity, 0),
        -budget_magnitude_tier(cand),
        cand.title,
    )


def apply_daily_page_budget(
    candidates: "list[AlertCandidate]",
    *,
    already_paged_today: int,
    budget: int,
    per_kind_cap: int = DEFAULT_BUDGET_PER_KIND_CAP,
    already_paged_today_by_kind: Optional[Mapping[str, int]] = None,
) -> int:
    """Marks ``data['budget_deferred']`` True/False on every candidate IN
    PLACE (mutates ``cand.data``; always sets the key explicitly, mirroring
    the FRAME-3 guard's always-present ``guard_suppressed`` field) and
    returns the count deferred.

    Ranks worst-first via :func:`budget_sort_key`, then walks the ranked list
    ONCE, taking a candidate only while BOTH hold: the day's remaining budget
    (``max(0, budget - already_paged_today)``) is not exhausted, AND its own
    ``trigger_class`` has not yet reached ``per_kind_cap`` slots today
    (``already_paged_today_by_kind`` seeds each kind's running count, so the
    cap is a DAY total across scans, not a per-call total).

    KIND-DIVERSITY CAP, single pass, deliberately no second pass: once a kind
    hits its cap it is skipped for the REST of the ranked list, but a
    LOWER-ranked candidate of a DIFFERENT kind can still take the slot that
    skip freed up — diversity is the point. If no other kind has a candidate
    left, that slot goes UNUSED rather than backfilled with more of the
    capped kind — the cap would mean nothing if it did.
    """
    remaining = max(0, budget - already_paged_today)
    kind_counts: dict[str, int] = dict(already_paged_today_by_kind or {})
    ranked = sorted(candidates, key=budget_sort_key)
    deferred = 0
    taken = 0
    for cand in ranked:
        kind = cand.trigger_class
        if taken < remaining and kind_counts.get(kind, 0) < per_kind_cap:
            cand.data["budget_deferred"] = False
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            taken += 1
        else:
            cand.data["budget_deferred"] = True
            deferred += 1
    return deferred


async def advance_watermarks_only(conn: Any, candidates: "list[AlertCandidate]") -> None:
    """D2 kill list — advance a killed class's watermarks EXACTLY as if its
    candidates had been written and fired (``fired=True``), without writing
    any alert row or paging anybody. Keeps the SAME transition from being
    re-detected as "still needs to fire" on every subsequent scan while the
    class is disabled — the no-refire contract holds even though nothing is
    ever delivered — so a later resurrection does not replay a backlog."""
    from .alert_trigger_scan import _upsert_watermark

    for cand in candidates:
        for wm_class, wm_key, wm_state in cand.watermarks:
            await _upsert_watermark(conn, wm_class, wm_key, wm_state, fired=True)


async def handle_kill_switch(
    conn: Any,
    cls: str,
    candidates: "list[AlertCandidate]",
    *,
    enabled: bool,
) -> dict[str, Any]:
    """One shared path for both kill-listed classes (contention_flip,
    geo_convergence). Enabled: a no-op, returns ``{"killed": False}`` and the
    caller extends its pageable list as usual. Disabled: advances every
    candidate's watermark as if fired (see :func:`advance_watermarks_only`),
    logs the count, and returns ``{"killed": True,
    "killed_would_have_fired": n}`` — the caller must NOT extend its pageable
    list, so nothing is written and nothing pages.
    """
    if enabled:
        return {"killed": False}
    if candidates:
        await advance_watermarks_only(conn, candidates)
        logger.info(
            "alert_trigger_scan.killed_class_would_have_fired class=%s n=%d",
            cls,
            len(candidates),
        )
    return {"killed": True, "killed_would_have_fired": len(candidates)}


#: D2 daily page budget — how many alerts this handler has ALREADY paged
#: (fanned out, not suppressed/killed/deferred) since the start of the
#: current UTC calendar day. Reads the nested payload-dump shape
#: (``data->'data'->>...`` — the ``AlertPayload`` model_dump wraps
#: ``cand.data`` one level in, see ``_row_data`` in the test suite for the
#: same unwrap) so an operator's live `psql` query and this count agree.
_COUNT_PAGED_TODAY_SQL = """
    SELECT count(*)::int AS n
      FROM analyst_outputs
     WHERE kind = 'alert'
       AND analyst_id = $1
       AND produced_at >= $2
       -- 'guard_suppressed', never bare 'suppressed' — that key is ALREADY
       -- a per-desk-cap rollup's list-of-summaries field (a different type
       -- for a different mechanism; see the AlertCandidate.data comment in
       -- _scan_verified_findings) and casting IT to boolean is exactly the
       -- crash this query used to hit on any rollup row.
       AND NOT COALESCE((data -> 'data' ->> 'guard_suppressed')::boolean, false)
       AND NOT COALESCE((data -> 'data' ->> 'killed')::boolean, false)
       AND NOT COALESCE((data -> 'data' ->> 'budget_deferred')::boolean, false)
"""


async def count_paged_today(conn: Any, analyst_id: str, day_start_utc: Any) -> int:
    row = await conn.fetchrow(_COUNT_PAGED_TODAY_SQL, analyst_id, day_start_utc)
    return int(row["n"]) if row and row["n"] is not None else 0


#: Same population as :data:`_COUNT_PAGED_TODAY_SQL`, broken out per
#: trigger_class — feeds the kind-diversity cap's DAY-cumulative count (a
#: kind capped by an EARLIER scan this UTC day must stay capped in a LATER
#: scan, not just within one scan's own candidate batch). geo_convergence
#: rows carry no explicit ``trigger_class`` key in their own payload (byte-
#: identical to the pre-fold standalone analyst — see
#: geo_convergence_scan._formation_candidate); COALESCEd the same way the
#: fleet-wide replay script reads it.
_COUNT_PAGED_TODAY_BY_KIND_SQL = """
    SELECT COALESCE(data -> 'data' ->> 'trigger_class', 'geo_convergence') AS trigger_class,
           count(*)::int AS n
      FROM analyst_outputs
     WHERE kind = 'alert'
       AND analyst_id = $1
       AND produced_at >= $2
       AND NOT COALESCE((data -> 'data' ->> 'guard_suppressed')::boolean, false)
       AND NOT COALESCE((data -> 'data' ->> 'killed')::boolean, false)
       AND NOT COALESCE((data -> 'data' ->> 'budget_deferred')::boolean, false)
     GROUP BY 1
"""


async def count_paged_today_by_kind(
    conn: Any, analyst_id: str, day_start_utc: Any
) -> dict[str, int]:
    rows = await conn.fetch(_COUNT_PAGED_TODAY_BY_KIND_SQL, analyst_id, day_start_utc)
    return {str(r["trigger_class"]): int(r["n"]) for r in rows}


__all__ = [
    "BUDGET_PER_KIND_CAP_ENV",
    "CONTENTION_FLIP_ENABLED_ENV",
    "DAILY_PAGE_BUDGET_ENV",
    "DEFAULT_BUDGET_PER_KIND_CAP",
    "DEFAULT_DAILY_PAGE_BUDGET",
    "GEO_CONVERGENCE_ENABLED_ENV",
    "advance_watermarks_only",
    "apply_daily_page_budget",
    "bool_env",
    "budget_magnitude_tier",
    "budget_per_kind_cap_from_env",
    "budget_sort_key",
    "count_paged_today",
    "count_paged_today_by_kind",
    "daily_page_budget_from_env",
    "handle_kill_switch",
]
