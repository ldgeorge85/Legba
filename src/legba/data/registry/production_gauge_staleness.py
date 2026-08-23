# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CADENCE-STALENESS loop for S-1 — FRAME-1 §6, the BF loop closed.

The production classes ask *is this loop producing*; the integrity classes ask
*is what it produces still what we think it is*; the metering classes ask *can
the plane afford it*. This one asks the question the 2026-08-20 correctness
round found nobody was asking: **is what the composition READ still current?**

THE MECHANISM, and why it was invisible. The trigger kernel's cadence gate
fires only when a desk has at least one pending signal — "a tick over an empty
window is a no-op" (``runtime/triggers/policy.py``, the CADENCE branch). That
is deliberate and CORRECT for units: forcing runs on empty slices would spend
LLM budget manufacturing "no change" heads. But a quiet desk holds NOT_DIRTY
indefinitely, and on 2026-08-20 the Burkina Faso units had last fired 42 hours
earlier. Nothing was broken: the analysts were alive (``analyst_cadence`` green
— the composition itself ran on time), they were producing (
``analyst_production`` green), and the desk was simply quiet. The only thing
wrong was that the layer CONSUMING those heads had no idea how old they were,
so it composed a country read over two-day-old inputs and printed "No source
findings to synthesize."

**This loop does not change the trigger policy and does not force any run.**
§6.4 is explicit about that: a stalled desk is surfaced, never silently
re-fired. What changes is that 42 hours of silence stops being invisible.

*Expected*: the units under a composition fire often enough that its newest
consumed head is younger than :attr:`GaugeConfig.staleness_max_head_age_hours`
— 34h by default = 2x the units' 11h cooldown plus 12h of fallback-cron slack.
BF's 42h trips it; a normal overnight quiet (a unit that fired 13h ago because
its cooldown had not expired at the last compose) does not.
*Actual*: ``data.head_ages.max_h`` — the age the composition ITSELF published
on its envelope at render time (``composition_window.head_ages_stamp``).
*Deficit*: ratio = max age / bar, on the shared severity ramp.

READING THE PRODUCT'S OWN NUMBER is the design decision worth defending. This
loop could re-derive head ages from ``analyst_outputs`` directly, and it would
then be measuring something the product never saw — the gap between "the desk
is quiet" and "the read was composed over stale heads" is exactly where the
defect lived. Keying on the stamp means the operator is paged about the same
number the reader was (or was not) told. It also makes the loop honestly
self-limiting: a composition head with no stamp (pre-FRAME-1, or a run whose
inputs carried no parsable timestamp) is NOT GAUGED, never gauged as fresh.

Consumer, per the no-stubs rule: none is new. ``read_gauge`` picks the rows up,
so the ``/v3/system/production-gauge`` route, the ``production_deficit``
trigger class and the ntfy fan-out all carry them with no further wiring —
the same contract the metering loops joined on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from .production_gauge import (
    GaugeConfig,
    LoopGauge,
    _ungauged,
    severity_for_ratio,
)

logger = logging.getLogger(__name__)

LOOP_DESK_HEAD_STALENESS = "desk_head_staleness"

STALENESS_LOOP_CLASSES: tuple[str, ...] = (LOOP_DESK_HEAD_STALENESS,)

# Quiet-by-design reasons this loop adds to the S-1 vocabulary.
QUIET_NO_STAMP = "no_head_ages_stamp"
QUIET_STALENESS_QUERY_FAILED = "staleness_query_failed"

#: Defensive bound on desks enumerated per read (33 desks live; a region/world
#: composition adds a handful more once those heads carry the stamp).
_MAX_DESKS = 500

#: The newest composition head per (analyst, desk) that carries a FRAME-1
#: ``data.head_ages`` stamp, inside a short trailing window.
#:
#: The window is short ON PURPOSE and is NOT the composition's 336h
#: admissibility horizon: this loop asks "was the read the operator is looking
#: at now composed over stale heads", and a five-day-old composition head is a
#: question for ``analyst_cadence`` (did the composition stop running), not for
#: this one. ``$1`` is the window start, ``$2`` the row bound. Verified against
#: the live substrate read-only on 2026-08-20 (0 rows — no head carries the
#: stamp until FRAME-1 deploys, which is exactly the honest starting state).
_STALENESS_SQL = """
    SELECT DISTINCT ON (f.analyst_id, f.target_id)
           f.analyst_id,
           f.target_id,
           f.produced_at,
           (f.data->'data'->'head_ages'->>'max_h')::float8   AS max_head_age_h,
           (f.data->'data'->'head_ages'->>'min_h')::float8   AS min_head_age_h,
           (f.data->'data'->'head_ages'->>'horizon_h')::float8 AS horizon_h,
           jsonb_array_length(
               coalesce(f.data->'data'->'head_ages'->'heads', '[]'::jsonb)
           )                                                 AS head_count
      FROM analyst_outputs f
     WHERE f.kind = 'finding'
       AND f.produced_at > $1
       AND f.superseded_by IS NULL
       AND (f.data->'data'->'head_ages') IS NOT NULL
     ORDER BY f.analyst_id, f.target_id, f.produced_at DESC, f.id DESC
     LIMIT $2
"""


def _desk_label(analyst_id: str, target_id: str | None) -> str:
    desk = target_id or "world"
    return f"composition head staleness {analyst_id} @ {desk}"


def _loop_id(analyst_id: str, target_id: str | None) -> str:
    return f"{analyst_id}:{target_id or 'world'}"


def desk_head_staleness_gauge(
    row: Mapping[str, Any], *, now: datetime, cfg: GaugeConfig
) -> LoopGauge:
    """Were the heads this composition consumed still current?

    ``row`` carries ``analyst_id``, ``target_id``, ``produced_at`` and the
    ``head_ages`` stamp's ``max_head_age_h`` / ``min_head_age_h`` /
    ``horizon_h`` / ``head_count``.

    A row without ``max_head_age_h`` is ``ungauged``/``no_head_ages_stamp``
    rather than ``ok``: the composition ran but published no age accounting, and
    "we cannot say" must never render as "it is fine" (the S-1 rule the whole
    twelve-limb census turned on).
    """
    analyst_id = str(row.get("analyst_id") or "")
    target_id = row.get("target_id")
    target = str(target_id) if target_id is not None else None
    loop_id = _loop_id(analyst_id, target)
    label = _desk_label(analyst_id, target)

    raw_age = row.get("max_head_age_h")
    if raw_age is None:
        return _ungauged(
            LOOP_DESK_HEAD_STALENESS,
            loop_id,
            label,
            QUIET_NO_STAMP,
            composed_at=(
                row["produced_at"].isoformat()
                if hasattr(row.get("produced_at"), "isoformat")
                else None
            ),
        )
    try:
        max_age = float(raw_age)
    except (TypeError, ValueError):
        return _ungauged(
            LOOP_DESK_HEAD_STALENESS, loop_id, label, QUIET_NO_STAMP,
            malformed_max_h=repr(raw_age),
        )

    bar = max(1.0, float(cfg.staleness_max_head_age_hours))
    ratio = max_age / bar
    heads = int(row.get("head_count") or 0)
    evidence: dict[str, Any] = {
        "max_head_age_h": round(max_age, 2),
        "bar_hours": round(bar, 1),
        "heads_consumed": heads,
    }
    min_age = row.get("min_head_age_h")
    if min_age is not None:
        try:
            evidence["min_head_age_h"] = round(float(min_age), 2)
        except (TypeError, ValueError):
            pass
    horizon = row.get("horizon_h")
    if horizon is not None:
        try:
            evidence["horizon_h"] = round(float(horizon), 1)
        except (TypeError, ValueError):
            pass
    composed = row.get("produced_at")
    if hasattr(composed, "isoformat"):
        evidence["composed_at"] = composed.isoformat()

    expected = (
        f"every unit under this composition read within {bar:.0f}h "
        "(2x the unit cooldown + fallback slack); older means the desk has been "
        "quiet across a whole compose cycle"
    )
    actual = (
        f"oldest consumed head {max_age:.0f}h old across {heads} head(s)"
        if heads
        else f"oldest consumed head {max_age:.0f}h old"
    )

    return LoopGauge(
        loop_class=LOOP_DESK_HEAD_STALENESS,
        loop_id=loop_id,
        label=label,
        state="deficit" if ratio >= 1.0 else "ok",
        severity=severity_for_ratio(ratio) if ratio >= 1.0 else "info",
        ratio=round(ratio, 3),
        expected=expected,
        actual=actual,
        last_production_at=composed if isinstance(composed, datetime) else None,
        evidence=evidence,
    )


async def read_staleness_loops(
    conn: Any,
    *,
    now: Optional[datetime] = None,
    cfg: Optional[GaugeConfig] = None,
) -> list[LoopGauge]:
    """One row per composition head carrying a FRAME-1 ``head_ages`` stamp.

    DYNAMIC, like ``llm_daily_burn``: an engine whose compositions carry no
    stamp (every engine before FRAME-1 ships) contributes ZERO rows rather than
    a fleet of green ones — silence about a thing we cannot measure, not a
    reassurance. Degrades LOUD as a single ungauged row if the query fails.
    """
    now = now or datetime.now(tz=timezone.utc)
    cfg = cfg or GaugeConfig()
    since = now - timedelta(hours=max(1.0, float(cfg.staleness_window_hours)))
    try:
        rows: Sequence[Mapping[str, Any]] = await conn.fetch(
            _STALENESS_SQL, since, _MAX_DESKS
        )
    except Exception as exc:  # pragma: no cover — degrade-not-drop
        logger.warning("production_gauge.staleness_query_failed err=%s", exc)
        return [
            _ungauged(
                LOOP_DESK_HEAD_STALENESS,
                LOOP_DESK_HEAD_STALENESS,
                "composition head staleness",
                QUIET_STALENESS_QUERY_FAILED,
                error=str(exc)[:300],
            )
        ]
    return [
        desk_head_staleness_gauge(dict(r), now=now, cfg=cfg) for r in (rows or [])
    ]


__all__ = [
    "LOOP_DESK_HEAD_STALENESS",
    "QUIET_NO_STAMP",
    "QUIET_STALENESS_QUERY_FAILED",
    "STALENESS_LOOP_CLASSES",
    "desk_head_staleness_gauge",
    "read_staleness_loops",
]
