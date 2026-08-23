# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""METERING loops for S-1 — the LLM plane's latency and spend gauges (#21/#22).

The production classes ask *is this loop producing*; the integrity classes ask
*is what it produces still what we think it is*. These two ask the question
neither can: *can the plane it all runs on AFFORD the load* — in seconds
against the client timeout, and in dollars against a per-component ceiling.
Same ``LoopGauge`` contract, same watermark and paging path; ``read_gauge``
picks them up and the route, totals, ``production_deficit`` trigger and ntfy
fan-out work with no further wiring.

**llm_latency** — the leading edge of the timeout cliff. The primary
component's client timeout is 240s. A call that fails at 240s pages as an
analyst failure — AFTER the finding is lost. But the fleet's p95 crossing HALF
that budget is visible hours earlier, and it is the number that must be green
before the ~100K budget raise (#21): a plane already spending 120s per
typical-worst call has no headroom to absorb more load. Measured live
2026-08-15 the primary's 24h p95 was 102.4s — under the bar, and not by much,
which is exactly why this loop exists. The second leg is truncation:
``finish_reason='length'`` on a receipt means the server cut a finding off
mid-sentence and the run carried on as a success — ONE such call is a defect
at any sample size (the vllm handler does not send ``max_tokens`` by default,
so 'length' means the server-side budget silently won).

**llm_daily_burn** — quota death is blind (#22). Every hosted PAYG call's
receipt carried ``cost_estimate_usd: 0.0`` because the vllm handler's price
table is empty (correct for self-hosted, silently wrong for Cerebras). The
2026-08-03 judge outage was a ``402 payment_required`` — the account ran DRY
and nothing had been counting. The handler side now prices per component
(``price_input_per_m`` / ``price_output_per_m`` on the component config);
this loop is the read side: sum today's ``cost_estimate_usd`` per component
from the ``analyst_traces.llm_calls`` receipts and page when a component
crosses its own ``daily_burn_alert_usd``. A component with spend but no
ceiling reads ``ungauged/no_burn_threshold`` — money moving with nobody
watching is a fact worth showing, never a page. A component with a ceiling
and no spend is honestly ``ok`` at $0.00.

Both loops are READ-ONLY over receipts that already exist; no producer grew
any instrumentation for them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from .production_gauge import (
    GaugeConfig,
    LoopGauge,
    _ungauged,
    severity_for_ratio,
)

logger = logging.getLogger(__name__)

LOOP_LLM_LATENCY = "llm_latency"
LOOP_LLM_DAILY_BURN = "llm_daily_burn"

METERING_LOOP_CLASSES: tuple[str, ...] = (
    LOOP_LLM_LATENCY,
    LOOP_LLM_DAILY_BURN,
)

#: The component the latency loop watches — the fleet's primary plane, whose
#: 240s client timeout is the budget the p95 ceiling is calibrated against
#: (half of it). A CONSTANT, not a knob, on the ``BACKLOG_DRAINS`` precedent:
#: the ceiling/window ARE tunable (``gauge_llm_latency_*``), but pointing the
#: loop at a different component means recalibrating the ceiling to THAT
#: component's timeout, which is an edit someone should have to read this
#: comment to make. Other components' latency is visible on the runtime
#: telemetry route; their affordability is this module's OTHER loop.
LATENCY_COMPONENT = "llm.primary.openai_compat"

# Quiet-by-design reasons these loops add to the S-1 vocabulary.
QUIET_NO_CALLS = "no_calls_in_window"
QUIET_FEW_CALLS = "insufficient_history"
QUIET_LATENCY_QUERY_FAILED = "latency_query_failed"
QUIET_NO_BURN_THRESHOLD = "no_burn_threshold"
QUIET_NO_SPEND_DATA = "no_spend_data"
QUIET_BURN_QUERY_FAILED = "burn_query_failed"

#: Defensive bound on components enumerated per burn read.
_MAX_COMPONENTS = 100


# ---------------------------------------------------------------------------
# llm_latency
# ---------------------------------------------------------------------------

#: One pass over the trailing window's receipts for the watched component.
#: ``duration_ms`` is stamped on EVERY receipt entry (success and failure
#: alike), so the p95 honestly includes timeouts — they are the slowest calls,
#: and excluding them would make a drowning plane read faster as it drowns.
#: The window keys on ``run_started_at`` (trace rows are written at run end,
#: so a still-running run's calls are not visible yet — acceptable lag for a
#: gauge that reads every scan tick).
_LATENCY_SQL = """
    SELECT count(*)::int AS calls,
           percentile_cont(0.95) WITHIN GROUP (
               ORDER BY (c->>'duration_ms')::float8
           ) AS p95_ms,
           max((c->>'duration_ms')::float8) AS max_ms,
           count(*) FILTER (
               WHERE c->>'finish_reason' = 'length'
           )::int AS length_hits,
           count(*) FILTER (
               WHERE c->>'status' <> 'success'
           )::int AS failed_calls
      FROM analyst_traces t
     CROSS JOIN LATERAL jsonb_array_elements(
               coalesce(t.llm_calls, '[]'::jsonb)) AS c
     WHERE t.run_started_at > $1
       AND c->>'component_id' = $2
       AND c->>'duration_ms' IS NOT NULL
"""


def llm_latency_gauge(
    row: Mapping[str, Any], *, now: datetime, cfg: GaugeConfig
) -> LoopGauge:
    """Is the primary plane answering inside its headroom budget?

    Two legs, one loop:

    * **p95 over ceiling** — ``ratio`` is p95/ceiling, so crossing the bar
      lands ``medium`` (pages) and a p95 at the full 240s timeout reads 2.0 =
      ``high``. Requires ``llm_latency_min_calls`` in-window.
    * **any truncation** — ``finish_reason='length'`` receipts, each one a
      finding the server cut off while the run reported success. The ratio is
      the raw count (1 = medium, 4+ = critical), and the min-calls floor does
      NOT apply — one truncated finding is a defect at any sample size.

    The reported ratio is the worse leg; the evidence carries both.
    """
    label = f"LLM latency {LATENCY_COMPONENT}"
    calls = int(row.get("calls") or 0)
    length_hits = int(row.get("length_hits") or 0)
    p95_ms = row.get("p95_ms")
    ceiling = max(1.0, float(cfg.llm_latency_p95_ceiling_ms))

    if calls == 0:
        # A quiet hour on the primary plane is the silence alarm's condition
        # (host_llm_heartbeat), not a latency one. One fault, one page.
        return _ungauged(
            LOOP_LLM_LATENCY,
            LATENCY_COMPONENT,
            label,
            QUIET_NO_CALLS,
            window_minutes=cfg.llm_latency_window_minutes,
        )

    p95_ratio = 0.0
    p95_trusted = calls >= max(1, int(cfg.llm_latency_min_calls))
    if p95_trusted and p95_ms is not None:
        p95_ratio = float(p95_ms) / ceiling

    length_ratio = float(length_hits)
    ratio = max(p95_ratio if p95_ratio >= 1.0 else 0.0, length_ratio)

    evidence: dict[str, Any] = {
        "calls": calls,
        "p95_ms": None if p95_ms is None else round(float(p95_ms), 1),
        "max_ms": (
            None if row.get("max_ms") is None else round(float(row["max_ms"]), 1)
        ),
        "ceiling_ms": ceiling,
        "length_hits": length_hits,
        "failed_calls": int(row.get("failed_calls") or 0),
        "window_minutes": cfg.llm_latency_window_minutes,
        "p95_trusted": p95_trusted,
    }

    if ratio < 1.0:
        if not p95_trusted:
            # Too few calls for the percentile AND no truncations: nothing
            # honest to say yet. Never folded into ok.
            return _ungauged(
                LOOP_LLM_LATENCY,
                LATENCY_COMPONENT,
                label,
                QUIET_FEW_CALLS,
                calls=calls,
                required=int(cfg.llm_latency_min_calls),
                length_hits=length_hits,
                window_minutes=cfg.llm_latency_window_minutes,
            )
        return LoopGauge(
            loop_class=LOOP_LLM_LATENCY,
            loop_id=LATENCY_COMPONENT,
            label=label,
            state="ok",
            ratio=round(max(p95_ratio, 0.0), 3),
            expected=(
                f"p95 call duration under {ceiling / 1000.0:.0f}s (half the "
                f"client timeout) and zero truncated completions"
            ),
            actual=(
                f"p95 {float(p95_ms) / 1000.0:.1f}s over {calls} calls, "
                f"0 truncations"
            ),
            evidence=evidence,
        )

    parts: list[str] = []
    if p95_ratio >= 1.0:
        parts.append(
            f"p95 {float(p95_ms) / 1000.0:.1f}s > {ceiling / 1000.0:.0f}s "
            f"over {calls} calls"
        )
    if length_hits:
        parts.append(
            f"{length_hits} completion(s) truncated by the server "
            f"(finish_reason='length') — findings cut off mid-sentence "
            f"inside runs that reported success"
        )
    return LoopGauge(
        loop_class=LOOP_LLM_LATENCY,
        loop_id=LATENCY_COMPONENT,
        label=label,
        state="deficit",
        severity=severity_for_ratio(ratio),
        ratio=round(ratio, 3),
        expected=(
            f"p95 call duration under {ceiling / 1000.0:.0f}s (half the "
            f"client timeout) and zero truncated completions"
        ),
        actual="; ".join(parts),
        evidence=evidence,
    )


async def read_llm_latency_loops(
    conn: Any, *, now: datetime, cfg: GaugeConfig
) -> list[LoopGauge]:
    """Degrade LOUD: a failed read is ``ungauged`` carrying the error, never a
    silent zero (which would read as a fast plane)."""
    since = now - timedelta(minutes=max(1, int(cfg.llm_latency_window_minutes)))
    try:
        row = await conn.fetchrow(_LATENCY_SQL, since, LATENCY_COMPONENT)
    except Exception as exc:  # pragma: no cover — degrade-not-drop
        logger.warning("production_gauge.latency_query_failed err=%s", exc)
        return [
            _ungauged(
                LOOP_LLM_LATENCY,
                LATENCY_COMPONENT,
                f"LLM latency {LATENCY_COMPONENT}",
                QUIET_LATENCY_QUERY_FAILED,
                error=str(exc)[:300],
            )
        ]
    return [llm_latency_gauge(dict(row or {}), now=now, cfg=cfg)]


# ---------------------------------------------------------------------------
# llm_daily_burn
# ---------------------------------------------------------------------------

#: Today's spend per component, from the same receipts. ``$1`` is the UTC day
#: start; grouping keys on the receipt's ``component_id`` so a component the
#: registry no longer heads still shows its money moving.
_BURN_SQL = """
    SELECT c->>'component_id' AS component_id,
           sum(coalesce((c->>'cost_estimate_usd')::float8, 0.0)) AS burn_usd,
           count(*)::int AS calls,
           sum(coalesce((c->>'total_tokens')::bigint, 0))::bigint AS tokens
      FROM analyst_traces t
     CROSS JOIN LATERAL jsonb_array_elements(
               coalesce(t.llm_calls, '[]'::jsonb)) AS c
     WHERE t.run_started_at >= $1
       AND c->>'component_id' IS NOT NULL
     GROUP BY 1
     LIMIT $2
"""

#: Every live LLM component head and its declared ceiling. ``NULL`` ceiling =
#: never pages (the $0 self-hosted / free-lane posture).
_THRESHOLD_SQL = """
    SELECT component_id,
           state,
           (body->'config'->'daily_burn_alert_usd'->>'raw')::float8
               AS daily_burn_alert_usd,
           ((body->'config'->'price_input_per_m'->>'raw') IS NOT NULL
            OR (body->'config'->'price_output_per_m'->>'raw') IS NOT NULL)
               AS priced
      FROM stack_components
     WHERE is_head
       AND kind = 'llm_provider'
     LIMIT $1
"""


def llm_burn_gauges(
    spend_rows: list[Mapping[str, Any]],
    threshold_rows: list[Mapping[str, Any]],
    *,
    now: datetime,
    cfg: GaugeConfig,
) -> list[LoopGauge]:
    """One gauge per component that either declares a ceiling or moved money.

    * ceiling declared → gauged: ``ok`` under it, ``deficit`` at/over it,
      ``ratio`` = burn/ceiling on the shared ramp (crossing = medium = pages;
      4x the ceiling = critical).
    * spend with NO ceiling → ``ungauged/no_burn_threshold``, burn in the
      evidence. Visible on the route, never a page — the operator's cue to
      set ``daily_burn_alert_usd`` on that component.
    * neither spend nor ceiling → no row at all: an unpriced self-hosted
      component's non-burn is not a fact worth a line per read.
    """
    spend: dict[str, Mapping[str, Any]] = {
        str(r.get("component_id") or ""): r
        for r in spend_rows
        if r.get("component_id")
    }
    thresholds: dict[str, Mapping[str, Any]] = {
        str(r.get("component_id") or ""): r
        for r in threshold_rows
        if r.get("component_id")
    }

    out: list[LoopGauge] = []
    for component_id in sorted(set(spend) | set(thresholds)):
        srow = spend.get(component_id) or {}
        trow = thresholds.get(component_id) or {}
        burn = float(srow.get("burn_usd") or 0.0)
        calls = int(srow.get("calls") or 0)
        tokens = int(srow.get("tokens") or 0)
        ceiling_raw = trow.get("daily_burn_alert_usd")
        label = f"LLM daily burn {component_id}"

        if ceiling_raw is None:
            if burn <= 0.0:
                continue  # nothing declared, nothing moving — not a loop
            out.append(
                _ungauged(
                    LOOP_LLM_DAILY_BURN,
                    component_id,
                    label,
                    QUIET_NO_BURN_THRESHOLD,
                    burn_usd_today=round(burn, 4),
                    calls_today=calls,
                    tokens_today=tokens,
                    note=(
                        "money is moving on this component and no "
                        "daily_burn_alert_usd is configured — set it on the "
                        "component config to gauge this loop"
                    ),
                )
            )
            continue

        ceiling = float(ceiling_raw)
        if ceiling <= 0.0:
            # A zero/negative ceiling cannot be a bar; treat as undeclared
            # (loud in the evidence rather than a divide-by-zero critical).
            if burn > 0.0:
                out.append(
                    _ungauged(
                        LOOP_LLM_DAILY_BURN,
                        component_id,
                        label,
                        QUIET_NO_BURN_THRESHOLD,
                        burn_usd_today=round(burn, 4),
                        calls_today=calls,
                        tokens_today=tokens,
                        declared_ceiling=ceiling,
                        note="daily_burn_alert_usd must be > 0 to gauge",
                    )
                )
            continue

        ratio = burn / ceiling
        evidence = {
            "burn_usd_today": round(burn, 4),
            "ceiling_usd": ceiling,
            "calls_today": calls,
            "tokens_today": tokens,
            "priced": bool(trow.get("priced")),
            "utc_day": now.date().isoformat(),
        }
        if not trow.get("priced") and burn == 0.0:
            # A ceiling on an UNPRICED component gauges a number that is $0
            # by construction — say so rather than reading healthy.
            evidence["note"] = (
                "component has a burn ceiling but no price_input_per_m/"
                "price_output_per_m — receipts cost 0.0, so this gauge "
                "cannot see real spend until pricing is set"
            )
        out.append(
            LoopGauge(
                loop_class=LOOP_LLM_DAILY_BURN,
                loop_id=component_id,
                label=label,
                state="deficit" if ratio >= 1.0 else "ok",
                severity=severity_for_ratio(ratio) if ratio >= 1.0 else "info",
                ratio=round(ratio, 3),
                expected=f"under ${ceiling:.2f} spend this UTC day",
                actual=(
                    f"${burn:.2f} across {calls} call(s) today"
                    + (f" ({tokens:,} tokens)" if tokens else "")
                ),
                evidence=evidence,
            )
        )
    return out


async def read_llm_burn_loops(
    conn: Any, *, now: datetime, cfg: GaugeConfig
) -> list[LoopGauge]:
    """READ-ONLY. Degrades loud, as one ungauged row (the loop set is dynamic,
    so there is no per-component identity to degrade under)."""
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    try:
        spend_rows = await conn.fetch(_BURN_SQL, day_start, _MAX_COMPONENTS)
        threshold_rows = await conn.fetch(_THRESHOLD_SQL, _MAX_COMPONENTS)
    except Exception as exc:  # pragma: no cover — degrade-not-drop
        logger.warning("production_gauge.burn_query_failed err=%s", exc)
        return [
            _ungauged(
                LOOP_LLM_DAILY_BURN,
                LOOP_LLM_DAILY_BURN,
                "LLM daily burn",
                QUIET_BURN_QUERY_FAILED,
                error=str(exc)[:300],
            )
        ]
    return llm_burn_gauges(
        [dict(r) for r in (spend_rows or [])],
        [dict(r) for r in (threshold_rows or [])],
        now=now,
        cfg=cfg,
    )


async def read_metering_loops(
    conn: Any, *, now: Optional[datetime] = None, cfg: Optional[GaugeConfig] = None
) -> list[LoopGauge]:
    """Both metering loops, for ``read_gauge``."""
    now = now or datetime.now(tz=timezone.utc)
    cfg = cfg or GaugeConfig()
    loops = await read_llm_latency_loops(conn, now=now, cfg=cfg)
    loops.extend(await read_llm_burn_loops(conn, now=now, cfg=cfg))
    return loops


__all__ = [
    "LATENCY_COMPONENT",
    "LOOP_LLM_DAILY_BURN",
    "LOOP_LLM_LATENCY",
    "METERING_LOOP_CLASSES",
    "llm_burn_gauges",
    "llm_latency_gauge",
    "read_llm_burn_loops",
    "read_llm_latency_loops",
    "read_metering_loops",
]
