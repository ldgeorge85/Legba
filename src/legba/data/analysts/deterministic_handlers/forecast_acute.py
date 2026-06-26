# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``forecast_acute`` — the pre-registered binary-forecast pilot (R1-T1.3 / #92).

ONE narrow, falsifiable, forward-looking call so the project can EARN BACK the
word "forecast" with an exogenously-resolved Brier / Brier-skill score:

    For each G20 country C, weekly, emit  p = P(>=1 ACUTE event of class K in C
    during the forward 7-day window [window_start, window_end)).  Resolve the
    outcome o EXOGENOUSLY by counting class-K events in that exact window by the
    UPSTREAM source's own event timestamp.  Score (p - o)^2; report it against
    the per-country climatological base rate p_base.

Design: planning/FORECAST_METRIC_DESIGN_2026-06-24.md. The pilot lives in its
own ``acute_forecasts`` table (migration 0047) — fully isolated from the live
findings feed and from the existing CI-coverage prediction resolver, so its
number is never pooled into the honest headline calibration Brier.

Why this earns the word where the legacy producers do not:
  * **Exogenous by construction.** The outcome is a count over a FROZEN class of
    events whose severity the UPSTREAM catalog stamps (USGS decides a quake is
    significant; NWS decides an alert is severe) — zero coupling to anything the
    forecaster says. An outside observer, given only (C, week, K), can recompute
    o independently. That is the falsifiability bar the ``subsequent_facts``
    keyword resolver fails.
  * **p and o are the SAME quantity.** p = P(>=1 event); o = (>=1 event
    occurred). A Brier over them scores a calibrated probability, not a
    CI-width heuristic graded against a coverage indicator.
  * **Skill is falsifiable.** p_base (climatology) gives a Brier skill score
    BSS = 1 - brier_model/brier_base. The project earns "forecast" only when
    BSS > 0 on the pilot.

Pure SQL + math; no LLM, no new model. λ over the window comes from the recent
class-K event rate (a Poisson rate forecast); ARIMA is a future swap-in.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# --- The frozen class-K definition. The ONE place the event class is defined. ---
EVENT_CLASS = "hazard_severe"
"""Class-K id. The hazard catalogs the ingest curation layer already
severity-filters. The forecaster does NOT get to define its own outcome
vocabulary — these source authorities stamp severity, not Legba.

The class is restricted to GLOBAL catalogs (USGS significant quakes + NASA EONET
major natural events). NWS active-alerts is deliberately EXCLUDED: it covers the
US only, so it cannot produce a cross-country-comparable G20 call, and its sheer
volume makes "at least one severe alert this week" trivially certain for the US
(the first live seeding of this pilot exposed exactly that — US λ saturated the
probability to 1.0). EMSC is retired upstream."""

HAZARD_SEVERE_SOURCES: tuple[str, ...] = (
    "source.usgs.earthquakes_m45",
    "source.nasa.eonet_events",
)

# Exogenous resolution label — NOT in calibration_tracking._SELF_CONSISTENCY_SOURCES
# (so it counts as exogenous) and NOT 'forecast_vs_actual' (so the pilot Brier is
# segregable from the legacy CI-coverage rows).
RESOLVED_BY = "forecast_acute_exogenous"

# --- Tuning knobs (module-level so tests/options can override). ---
RESOLUTION_GRACE_DAYS = 1
"""Grade only windows closed AND settled — a window [t0,t1) is resolvable at
t1 + grace, so late-ingested events for the window are already in."""

RECENT_RATE_LOOKBACK_DAYS = 28
"""Trailing window the model's Poisson rate is estimated over (the 'recent'
signal). Deliberately shorter than the climatology window so the model can
differ from — and potentially beat — climatology."""

CLIMATOLOGY_WEEKS = 52
"""Trailing weeks the per-country base rate p_base is computed over (capped to
the observed record so we never divide by weeks before ingestion began)."""

# --- D9: forecast-honesty knobs. ---
P_EPSILON = 0.01
"""Epsilon-clamp bound for the issued probability. A proper-scoring forecast
NEVER asserts 0.0 or 1.0 certainty — a single counter-example yields an infinite
penalty under log-loss and the worst possible Brier (1.0). We clamp every issued
p into [P_EPSILON, 1 - P_EPSILON] so the producer cannot collapse to the
degenerate {0, 1} certainties that made the live pilot a non-forecaster."""

CLIMATOLOGY_SHRINK_W = 0.5
"""Climatology-shrink weight for the de-rated rate. The raw recent-rate Poisson
λ saturates to 1.0 for high-exposure countries (a large, seismically-active
country accumulates events trivially). We shrink the model rate toward the
per-country climatology expectation: λ_eff = (1-w)·λ_recent + w·λ_clim, so the
issued p is pulled back from saturation toward the long-run base rate. w=0.5
weights recent signal and climatology equally."""

DEGENERACY_ABSTAIN_SHARE = 0.2
"""Pre-issue ABSTAIN threshold. If the cross-country p-vector for a window is
degenerate — i.e. the share of GENUINELY UNCERTAIN calls (p strictly inside
(P_EPSILON, 1-P_EPSILON)) is below this — the batch is geography-dominated, not
probabilistic forecasting, so the producer ABSTAINS (issues nothing) rather than
minting a certainty vector that would earn unearned skill against climatology."""

# The UPSTREAM event timestamp per source — quake origin time (epoch ms), NWS
# alert onset/effective, NASA EONET event date — NEVER fetched_at. This is what
# makes the resolution exogenous and removes the ingest-rate artifact.
_EVENT_TIME_SQL = """
CASE source_id
  WHEN 'source.usgs.earthquakes_m45'
    THEN to_timestamp((payload->'geojson'->'properties'->>'time')::double precision / 1000.0)
  WHEN 'source.nasa.eonet_events'
    THEN COALESCE(
      NULLIF(payload->'geojson'->'properties'->>'date', '')::timestamptz,
      fetched_at)
  ELSE fetched_at
END
"""


def poisson_tail_p(lmbda: float) -> float:
    """P(>=1 event) for a Poisson rate λ over the window = 1 - exp(-λ). Clamped
    to [0, 1]. NOTE: this is the RAW tail probability; the issued forecast is the
    epsilon-clamped :func:`clamp_p` of this value (D9) so a saturated λ never
    yields a degenerate 0/1 certainty."""
    if lmbda <= 0.0:
        return 0.0
    return float(min(1.0, max(0.0, 1.0 - math.exp(-lmbda))))


def clamp_p(p: float, *, epsilon: float = P_EPSILON) -> float:
    """D9 epsilon-clamp: pull an issued probability into [epsilon, 1-epsilon] so
    the producer can never assert a degenerate 0.0 / 1.0 certainty. A proper
    forecast keeps a hedge — a single surprise against a 1.0 call is the worst
    possible Brier (1.0); against a clamped 0.99 it is a bounded 0.0001 penalty."""
    return float(min(1.0 - epsilon, max(epsilon, p)))


def derate_lambda(
    lam_recent: float, lam_clim: float, *, w: float = CLIMATOLOGY_SHRINK_W,
) -> float:
    """D9 climatology-shrink: blend the recent-rate Poisson λ toward the
    per-country climatology rate so a high-exposure country's recent λ cannot
    saturate the issued p to 1.0. λ_eff = (1-w)·λ_recent + w·λ_clim.

    ``lam_clim`` is the climatology EXPECTED COUNT over the same 7-day window
    (NOT the base-rate probability) — derived from p_base via the Poisson
    inverse (see :func:`_lambda_from_p`). w defaults to ``CLIMATOLOGY_SHRINK_W``
    (0.5 — equal weight). Negative inputs are floored at 0."""
    lam_recent = max(0.0, float(lam_recent))
    lam_clim = max(0.0, float(lam_clim))
    w = min(1.0, max(0.0, float(w)))
    return (1.0 - w) * lam_recent + w * lam_clim


def _lambda_from_p(p: float) -> float:
    """Inverse Poisson tail: the λ whose P(>=1 event) = p, i.e. λ = -ln(1-p).
    Used to convert a climatology PROBABILITY (p_base) back into an expected
    COUNT so it can be blended with the recent-rate λ on the same scale."""
    p = min(1.0 - 1e-9, max(0.0, float(p)))
    if p <= 0.0:
        return 0.0
    return float(-math.log(1.0 - p))


def p_vector_is_degenerate(
    ps: list[float], *, epsilon: float = P_EPSILON,
    min_uncertain_share: float = DEGENERACY_ABSTAIN_SHARE,
) -> bool:
    """D9 pre-issue degeneracy check: True when the cross-country issued-p vector
    collapses to (near-){0, 1} — i.e. the share of GENUINELY UNCERTAIN calls
    (p strictly inside (epsilon, 1-epsilon)) is below ``min_uncertain_share``.
    A degenerate batch is geography-dominated, not probabilistic forecasting, so
    the producer ABSTAINS (issues nothing) rather than minting certainties."""
    if not ps:
        return True
    uncertain = sum(1 for p in ps if epsilon < p < (1.0 - epsilon))
    return (uncertain / len(ps)) < min_uncertain_share


def _next_window(now: datetime) -> tuple[datetime, datetime]:
    """The next FULLY-FORWARD weekly window — the Monday strictly after ``now``,
    00:00 UTC, for 7 days. Issuing for a window entirely in the future means p is
    computed only from data BEFORE the window (no look-ahead leakage)."""
    n = now.astimezone(timezone.utc)
    days_ahead = (7 - n.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    start = (n + timedelta(days=days_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(days=7)


async def _g20_regions(conn: Any) -> list[tuple[str, list[str]]]:
    """Enumerate active G20 country targets as ``(descriptor_id, geo_codes)``.

    G20 targets are ``country_g20_<iso2>`` with ``scope.geo`` = ISO2 codes that
    overlap ``signals.geo`` (same join the existing exogenous resolver uses)."""
    rows = await conn.fetch(
        """
        SELECT descriptor_id, body->'scope'->'geo' AS geo
        FROM target_descriptors
        WHERE is_head = TRUE
          AND descriptor_id LIKE 'country_g20_%'
          AND COALESCE(state, 'active') <> 'retired'
        ORDER BY descriptor_id
        """
    )
    out: list[tuple[str, list[str]]] = []
    for r in rows:
        geo_raw = r["geo"]
        geo: list[str] = []
        if isinstance(geo_raw, list):
            geo = [str(g) for g in geo_raw if g]
        elif isinstance(geo_raw, str):
            import json
            try:
                parsed = json.loads(geo_raw)
                geo = [str(g) for g in parsed if g] if isinstance(parsed, list) else []
            except Exception:  # noqa: BLE001
                geo = []
        if not geo:
            suffix = r["descriptor_id"].rsplit("_", 1)[-1].upper()
            if len(suffix) == 2 and suffix.isalpha():
                geo = [suffix]
        if geo:
            out.append((r["descriptor_id"], geo))
    return out


async def _count_class_k(
    conn: Any, geo: list[str], t0: datetime, t1: datetime,
) -> int:
    """Count class-K events in ``geo`` whose UPSTREAM event time is in [t0, t1)."""
    row = await conn.fetchrow(
        f"""
        SELECT count(*)::int AS cnt
        FROM signals
        WHERE source_id = ANY($1::text[])
          AND geo && $2::text[]
          AND ({_EVENT_TIME_SQL}) >= $3::timestamptz
          AND ({_EVENT_TIME_SQL}) <  $4::timestamptz
        """,
        list(HAZARD_SEVERE_SOURCES), geo, t0, t1,
    )
    return int(row["cnt"]) if row else 0


async def _recent_lambda(conn: Any, geo: list[str], now: datetime) -> float:
    """Expected class-K count over a 7-day window = recent daily rate * 7."""
    t0 = now - timedelta(days=RECENT_RATE_LOOKBACK_DAYS)
    cnt = await _count_class_k(conn, geo, t0, now)
    daily_rate = cnt / float(RECENT_RATE_LOOKBACK_DAYS)
    return daily_rate * 7.0


async def _climatology_base(
    conn: Any, geo: list[str], now: datetime, total_observed_weeks: int,
) -> float:
    """Per-country base rate p_base = (weeks with >=1 class-K event) /
    (observed weeks). Denominator is the GLOBAL observed-week count so we never
    credit weeks before the catalog began ingesting."""
    if total_observed_weeks <= 0:
        return 0.0
    since = now - timedelta(weeks=CLIMATOLOGY_WEEKS)
    row = await conn.fetchrow(
        f"""
        SELECT count(DISTINCT date_trunc('week', ({_EVENT_TIME_SQL}))) AS wk
        FROM signals
        WHERE source_id = ANY($1::text[])
          AND geo && $2::text[]
          AND ({_EVENT_TIME_SQL}) >= $3::timestamptz
          AND ({_EVENT_TIME_SQL}) <  $4::timestamptz
        """,
        list(HAZARD_SEVERE_SOURCES), geo, since, now,
    )
    weeks_with_event = int(row["wk"]) if row and row["wk"] is not None else 0
    return float(min(1.0, weeks_with_event / float(total_observed_weeks)))


async def _total_observed_weeks(conn: Any, now: datetime) -> int:
    """Distinct ISO-weeks in the climatology window for which the hazard catalog
    produced ANY event globally — the honest climatology denominator."""
    since = now - timedelta(weeks=CLIMATOLOGY_WEEKS)
    row = await conn.fetchrow(
        f"""
        SELECT count(DISTINCT date_trunc('week', ({_EVENT_TIME_SQL}))) AS wk
        FROM signals
        WHERE source_id = ANY($1::text[])
          AND ({_EVENT_TIME_SQL}) >= $2::timestamptz
          AND ({_EVENT_TIME_SQL}) <  $3::timestamptz
        """,
        list(HAZARD_SEVERE_SOURCES), since, now,
    )
    return int(row["wk"]) if row and row["wk"] is not None else 0


async def issue_weekly_forecasts(deps: Any, options: Mapping[str, Any]) -> int:
    """Issue one acute-binary forecast per G20 country for the NEXT weekly window.

    Idempotent: ``ON CONFLICT (region, event_class, window_start) DO NOTHING`` so
    the first daily tick of the week pins p (from pre-window data) and later ticks
    are no-ops. Best-effort; returns the count issued."""
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return 0
    now = datetime.now(timezone.utc)
    window_start, window_end = _next_window(now)
    issued = 0
    shrink_w = float(options.get("climatology_shrink_w", CLIMATOLOGY_SHRINK_W))
    epsilon = float(options.get("p_epsilon", P_EPSILON))
    min_uncertain_share = float(
        options.get("degeneracy_abstain_share", DEGENERACY_ABSTAIN_SHARE)
    )
    try:
        async with pool.acquire() as conn:
            regions = await _g20_regions(conn)
            if not regions:
                return 0
            total_weeks = await _total_observed_weeks(conn, now)
            # PHASE 1 — compute the full cross-country forecast vector WITHOUT
            # writing, so a degenerate (geography-dominated) batch can ABSTAIN as
            # a whole rather than emitting a {0,1} certainty vector (D9).
            staged: list[dict[str, Any]] = []
            for region, geo in regions:
                try:
                    lam_recent = await _recent_lambda(conn, geo, now)
                    p_base = await _climatology_base(conn, geo, now, total_weeks)
                    # D9 climatology-shrink: blend the recent-rate λ toward the
                    # climatology expected COUNT (p_base → λ via the Poisson
                    # inverse) so high-exposure countries cannot saturate p to 1.
                    lam_clim = _lambda_from_p(p_base)
                    lam_eff = derate_lambda(lam_recent, lam_clim, w=shrink_w)
                    # D9 epsilon-clamp: a forecast never asserts 0/1 certainty.
                    p = clamp_p(poisson_tail_p(lam_eff), epsilon=epsilon)
                    staged.append({
                        "region": region, "p": float(p),
                        "p_base": float(p_base), "lam_eff": float(lam_eff),
                    })
                except Exception as exc:  # noqa: BLE001 — one bad country never blocks the rest
                    logger.debug("forecast_acute.compute_one_failed region=%s err=%s", region, exc)

            # PHASE 2 — pre-issue degeneracy ABSTAIN (D9). If the cross-country
            # p-vector collapses to near-{0,1}, this batch is geography, not a
            # forecast; issue NOTHING this window.
            p_vec = [s["p"] for s in staged]
            if not staged or p_vector_is_degenerate(
                p_vec, epsilon=epsilon, min_uncertain_share=min_uncertain_share
            ):
                uncertain = sum(1 for p in p_vec if epsilon < p < (1.0 - epsilon))
                logger.info(
                    "forecast_acute.abstain_degenerate n=%d uncertain=%d "
                    "window=%s..%s class=%s",
                    len(p_vec), uncertain,
                    window_start.date().isoformat(),
                    window_end.date().isoformat(), EVENT_CLASS,
                )
                return 0

            for s in staged:
                try:
                    res = await conn.execute(
                        """
                        INSERT INTO acute_forecasts
                            (region, event_class, window_start, window_end,
                             p, p_base, method, lambda_model)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (region, event_class, window_start)
                            DO NOTHING
                        """,
                        s["region"], EVENT_CLASS, window_start, window_end,
                        s["p"], s["p_base"], "recent_rate_poisson_shrunk",
                        s["lam_eff"],
                    )
                    if res and res.endswith("1"):
                        issued += 1
                except Exception as exc:  # noqa: BLE001 — one bad country never blocks the rest
                    logger.debug("forecast_acute.issue_one_failed region=%s err=%s", s["region"], exc)
    except Exception as exc:  # noqa: BLE001 — never break the calibration tick
        logger.warning("forecast_acute.issue_failed err=%s", exc)
    if issued:
        logger.info(
            "forecast_acute.issued n=%d window=%s..%s class=%s",
            issued, window_start.date().isoformat(), window_end.date().isoformat(),
            EVENT_CLASS,
        )
    return issued


async def resolve_open_acute_forecasts(deps: Any, options: Mapping[str, Any]) -> int:
    """Grade every acute forecast whose window has CLOSED and SETTLED.

    o = 1 if >=1 class-K event occurred in [window_start, window_end) by the
    UPSTREAM event time, else 0. Stamps resolved_by='forecast_acute_exogenous'.
    Never overwrites an already-resolved row (operator labels win). Best-effort;
    returns the count resolved."""
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return 0
    grace = int(options.get("acute_grace_days", RESOLUTION_GRACE_DAYS))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=grace)
    resolved = 0
    try:
        async with pool.acquire() as conn:
            open_rows = await conn.fetch(
                """
                SELECT id::text AS id, region, window_start, window_end
                FROM acute_forecasts
                WHERE resolved_outcome IS NULL
                  AND window_end < $1::timestamptz
                ORDER BY window_end
                LIMIT 500
                """,
                cutoff,
            )
            if not open_rows:
                return 0
            geo_cache: dict[str, list[str]] = {}
            for row in open_rows:
                region = row["region"]
                geo = geo_cache.get(region)
                if geo is None:
                    geo = await _region_geo(conn, region)
                    geo_cache[region] = geo
                if not geo:
                    continue
                try:
                    cnt = await _count_class_k(
                        conn, geo, row["window_start"], row["window_end"],
                    )
                except Exception as exc:  # noqa: BLE001 — leave unresolved, retry next tick
                    logger.debug("forecast_acute.count_failed id=%s err=%s", row["id"], exc)
                    continue
                outcome = 1 if cnt >= 1 else 0
                await conn.execute(
                    """
                    UPDATE acute_forecasts
                    SET resolved_outcome = $2,
                        actual_value = $3,
                        resolved_by = $4,
                        resolved_at = $5::timestamptz
                    WHERE id = $1::uuid AND resolved_outcome IS NULL
                    """,
                    row["id"], outcome, int(cnt), RESOLVED_BY, now,
                )
                resolved += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("forecast_acute.resolve_failed err=%s", exc)
    if resolved:
        logger.info("forecast_acute.resolved n=%d source=%s", resolved, RESOLVED_BY)
    return resolved


async def _region_geo(conn: Any, region: str) -> list[str]:
    """Resolve a region's geo codes — prefer scope.geo, fall back to the ISO2
    suffix (mirrors calibration_tracking._resolve_region_geo)."""
    try:
        trow = await conn.fetchrow(
            "SELECT body->'scope'->'geo' AS geo FROM target_descriptors "
            "WHERE descriptor_id = $1 AND is_head = TRUE",
            region,
        )
        if trow and trow["geo"]:
            geo_raw = trow["geo"]
            if isinstance(geo_raw, list):
                geo = [str(g) for g in geo_raw if g]
                if geo:
                    return geo
            elif isinstance(geo_raw, str):
                import json
                try:
                    parsed = json.loads(geo_raw)
                    if isinstance(parsed, list):
                        geo = [str(g) for g in parsed if g]
                        if geo:
                            return geo
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    suffix = region.rsplit("_", 1)[-1].upper() if region else ""
    return [suffix] if len(suffix) == 2 and suffix.isalpha() else []


async def pull_resolved_acute_forecasts(
    deps: Any, options: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return resolved pilot forecasts as calibration claim rows. Each carries
    ``claim_kind='forecast'`` and ``p_base`` so the handler can compute a
    SEGREGATED Brier + Brier-skill score, never pooled into the headline."""
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return []
    lookback_days = int(options.get("lookback_days", 365))
    out: list[dict[str, Any]] = []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text AS claim_id, region, p, p_base,
                       resolved_outcome, resolved_by, resolved_at
                FROM acute_forecasts
                WHERE resolved_outcome IS NOT NULL
                  AND issued_at > NOW() - make_interval(days => $1)
                """,
                lookback_days,
            )
        for r in rows:
            out.append({
                "analyst_id": "forecast_acute",
                "claim_id": r["claim_id"],
                "claim_kind": "forecast",
                "claimed_confidence": float(r["p"]),
                "p_base": float(r["p_base"]),
                "outcome": int(r["resolved_outcome"]),
                "resolved_at": (
                    r["resolved_at"].isoformat() if r["resolved_at"] else None
                ),
                "resolved_by": r["resolved_by"] or RESOLVED_BY,
                "region": r["region"],
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("forecast_acute.pull_failed err=%s", exc)
    return out


__all__ = [
    "EVENT_CLASS",
    "HAZARD_SEVERE_SOURCES",
    "RESOLVED_BY",
    "P_EPSILON",
    "CLIMATOLOGY_SHRINK_W",
    "DEGENERACY_ABSTAIN_SHARE",
    "poisson_tail_p",
    "clamp_p",
    "derate_lambda",
    "p_vector_is_degenerate",
    "issue_weekly_forecasts",
    "resolve_open_acute_forecasts",
    "pull_resolved_acute_forecasts",
]
