# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``production_deficit`` trigger-class internals — S-1, the production gauge.

The seventh trigger class of :mod:`.alert_trigger_scan`, and the only one that
alerts on the engine watching ITSELF rather than on the world. Every other
class fires when the substrate says something happened; this one fires when a
producing loop that should have said something has said nothing.

The judgment is NOT here. :mod:`legba.data.registry.production_gauge` owns the
whole expectation model — descriptor cadence, trailing production baselines,
quiet-by-design exemptions, the severity ramp — and the v3 route reads the
exact same function. This module is the thin adapter that turns gauge rows
into ``AlertCandidate``s under the alert plane's existing anti-noise
machinery. One implementation, two readers (the ``source_freshness``
precedent); a threshold can never mean one thing on the route and another on
the phone.

What pages, and what deliberately does not
------------------------------------------
* **Only ``medium`` and worse** (``production_gauge.ALERT_MIN_SEVERITY``).
  ``info``/``low`` deficits are numbers for the table, not interruptions.
* **Escalation-only refire.** A deficit fires once when it appears, then stays
  silent while it persists at the same severity, then fires AGAIN when it
  escalates a rung (medium -> high -> critical). The watermark state carries
  the severity rank, so the ledger reads as a story ("this got worse") rather
  than a heartbeat. This is stricter than the plain fire-once contract the
  other classes use, and it is the right shape for a condition that by
  definition persists: a frozen feed does not "happen" once.
* **Recovery is silent.** When a loop returns to ``ok`` its watermark is
  cleared (so the NEXT deficit fires cleanly) without an all-clear page.
  Recovery is visible on the route; the operator's phone is for the bad news.
  Deliberate divergence from the liveness watchdog's entered/recovered edges —
  that plane pages per entity on a 60s sweep and needs the symmetry; this one
  pages rarely and should stay that way.
* **First scan seeds silently**, per the 0091 watermark contract: bringing the
  class up must never page the whole standing backlog of deficits. The seeding
  scan is NOT quiet about it though — it logs at WARNING and the receipt's
  ``seeded_deficits`` counter names how many standing deficits were adopted
  without paging, so "the gauge went live and found seven" is a fact in
  ``analyst_traces`` rather than a silence. Read the route after deploy.

No new table. ``alert_trigger_watermarks.trigger_class`` is an open
vocabulary, so this class rides the migration-0091 table exactly as
``geo_convergence`` did when it folded in.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

#: Bound on alert candidates one scan may raise, before the shared per-desk
#: cap folds the remainder. A fleet-wide fault (the substrate down, every
#: source silent) must not turn into a hundred candidate objects.
_MAX_CANDIDATES = 40

#: Watermark keys not seen in a gauge read for this long are dropped — a
#: retired analyst or deleted source should not keep state forever. Pruning by
#: ABSENCE from the current read (not by age) would be wrong: a loop can be
#: legitimately missing from one read (a transient query failure) and pruning
#: it would let its deficit re-page. Age gives that a wide margin.
_WATERMARK_PRUNE_DAYS = 30


#: Descriptor-option prefix for the gauge thresholds. Prefixed rather than
#: flat so ``gauge_window_days`` can never be confused with the handler's own
#: ``finding_window_hours`` / ``baseline_days`` knobs, which measure different
#: things over different tables.
OPTION_PREFIX = "gauge_"


def config_from_options(options: Mapping[str, Any]) -> Any:
    """Build a ``GaugeConfig`` from this handler's ``gauge_*`` options.

    Imported at call time (not module scope) so the other six trigger classes
    never pay for the registry import.
    """
    from ....data.registry.production_gauge import GaugeConfig

    return GaugeConfig.from_options(
        {
            k[len(OPTION_PREFIX):]: v
            for k, v in options.items()
            if k.startswith(OPTION_PREFIX)
        }
    )


def _fmt_evidence(evidence: Mapping[str, Any]) -> str:
    """Evidence dict -> stable ``key=value`` lines for the alert body."""
    return "\n".join(
        f"{k}={v}" for k, v in sorted(evidence.items()) if v is not None
    )


def build_body(gauge: Any) -> str:
    """The alert body for one gauge row: expectation, observation, evidence.

    Every deficit body states the EXPECTATION first and where it came from.
    An operator reading a 3am page must be able to tell "the engine promised X
    and delivered Y" from the notification alone, without opening the route —
    the whole failure class this gauge exists for is one nobody looked for.
    """
    head = [f"EXPECTED: {gauge.expected}", f"ACTUAL:   {gauge.actual}"]
    if gauge.ratio is not None:
        head.append(f"RATIO:    {gauge.ratio:.2f}x its own bar")
    blocks = [
        "\n".join(head),
        (
            "Derived from this loop's own declared cadence and trailing "
            "production history — not a global threshold. "
            "GET /api/v1/v3/system/production-gauge for the whole-engine table."
        ),
        _fmt_evidence(gauge.evidence),
    ]
    return "\n\n".join(b for b in blocks if b).strip()


def _watermark_state(gauge: Any, now: datetime) -> dict[str, Any]:
    return {
        "state": gauge.state,
        "severity": gauge.severity,
        "rank": gauge.rank,
        "ratio": gauge.ratio,
        "observed_at": now.isoformat(),
    }


async def scan_production_deficits(
    conn: Any,
    *,
    config: Any = None,
    now: Optional[datetime] = None,
) -> tuple[list[Any], list[tuple[str, str, dict[str, Any]]], bool, dict[str, int]]:
    """One ``production_deficit`` scan pass.

    Returns ``(candidates, silent_watermarks, was_seeded, stats)`` — the same
    4-tuple shape ``_watchlist_scan.scan_watchlist`` and
    ``geo_convergence_scan.scan_geo_convergence`` use, so
    :func:`.alert_trigger_scan.handle` folds it in with no special-casing.

    ``stats`` rides the receipt's ``counts_by_class`` entry:
    ``loops`` / ``gauged`` / ``deficits`` / ``paging`` / ``escalations`` /
    ``recoveries`` / ``seeded_deficits`` / ``candidate_bound_hit`` /
    ``unavailable``.
    """
    # Local imports — alert_trigger_scan imports this module; deferring the
    # reverse import to call time keeps the cycle harmless (the
    # _watchlist_scan precedent). The gauge lives in the registry package and
    # is imported here rather than at module scope so a handler import never
    # drags the registry in for the other six classes.
    from ....data.registry import production_gauge as gauge_mod
    from .alert_trigger_scan import (
        SEED_KEY,
        TRIGGER_PRODUCTION_DEFICIT,
        AlertCandidate,
        _load_class_watermarks,
    )

    stats = {
        "loops": 0,
        "gauged": 0,
        "deficits": 0,
        "paging": 0,
        "escalations": 0,
        "recoveries": 0,
        "seeded_deficits": 0,
        "candidate_bound_hit": 0,
        "unavailable": 0,
    }

    cfg = config or gauge_mod.GaugeConfig()
    try:
        report = await gauge_mod.read_gauge(conn, now=now, cfg=cfg)
    except Exception as exc:  # noqa: BLE001 — a broken gauge must SAY so
        # Degrade LOUD and empty rather than killing the other six classes:
        # a not-yet-migrated substrate or a schema drift takes the production
        # class offline, never the whole alert scan.
        if type(exc).__name__ != "UndefinedTableError":
            raise
        logger.warning(
            "production_deficit_scan.unavailable — the gauge could not read "
            "the substrate (%s); the class scanned nothing",
            exc,
        )
        stats["unavailable"] = 1
        return [], [], True, stats

    now_dt = report.generated_at
    seeded, marked = await _load_class_watermarks(conn, TRIGGER_PRODUCTION_DEFICIT)

    totals = report.totals()
    stats["loops"] = int(totals["loops"])
    stats["gauged"] = int(totals["gauged"])
    stats["deficits"] = int(totals["deficit"])
    stats["paging"] = int(totals["paging"])

    candidates: list[Any] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    seeded_deficit_labels: list[str] = []

    for g in report.loops:
        key = g.key
        prior = marked.get(key) or {}
        was_deficit = prior.get("state") == "deficit"
        prior_rank = int(prior.get("rank") or 0) if was_deficit else -1

        if not g.pages:
            # Not a paging condition. Clear any prior deficit so the NEXT one
            # fires cleanly; otherwise leave no trace (a watermark row per
            # healthy loop would be pure table growth).
            if prior_rank >= 0:
                stats["recoveries"] += 1
                silent.append(
                    (TRIGGER_PRODUCTION_DEFICIT, key, _watermark_state(g, now_dt))
                )
            continue

        state = _watermark_state(g, now_dt)

        if not seeded:
            # First-ever scan: adopt every standing deficit WITHOUT paging.
            seeded_deficit_labels.append(f"{key}({g.severity})")
            silent.append((TRIGGER_PRODUCTION_DEFICIT, key, state))
            continue

        if g.rank <= prior_rank:
            # Ongoing at the same or lower severity — silence is the point.
            silent.append((TRIGGER_PRODUCTION_DEFICIT, key, state))
            continue

        if len(candidates) >= _MAX_CANDIDATES:
            # Over the defensive bound: DO NOT watermark. An un-advanced
            # watermark means this deficit is a candidate again next scan
            # rather than silently adopted as reported — the same "a failed
            # write retries" ordering the handler uses for alert-row writes.
            stats["candidate_bound_hit"] = 1
            continue

        if prior_rank >= 0:
            stats["escalations"] += 1
        headline = "escalated to" if prior_rank >= 0 else "detected —"
        candidates.append(
            AlertCandidate(
                trigger_class=TRIGGER_PRODUCTION_DEFICIT,
                severity=g.severity,
                title=(
                    f"Production deficit {headline} {g.severity}: "
                    f"{g.label} — {g.actual}"
                ),
                body=build_body(g),
                target_id=None,
                data={
                    "trigger_class": TRIGGER_PRODUCTION_DEFICIT,
                    "loop_class": g.loop_class,
                    "loop_id": g.loop_id,
                    "gauge_state": g.state,
                    "ratio": g.ratio,
                    "expected": g.expected,
                    "observed": g.actual,
                    "previous_severity": prior.get("severity"),
                    "window_days": report.window_days,
                    "evidence": g.evidence,
                },
                watermarks=[(TRIGGER_PRODUCTION_DEFICIT, key, state)],
                event_at=g.last_production_at,
            )
        )

    stats["seeded_deficits"] = len(seeded_deficit_labels)
    if seeded_deficit_labels:
        # NEVER silent about seeding real deficits — this is the one moment a
        # fire-once contract could be mistaken for "the engine is fine".
        logger.warning(
            "production_deficit_scan.seeded_standing_deficits n=%d loops=%s "
            "— adopted WITHOUT paging (0091 seed contract); read "
            "/api/v1/v3/system/production-gauge for the standing table",
            len(seeded_deficit_labels),
            sorted(seeded_deficit_labels)[:20],
        )

    await _prune_watermarks(conn, TRIGGER_PRODUCTION_DEFICIT, SEED_KEY)
    return candidates, silent, seeded, stats


async def _prune_watermarks(conn: Any, trigger_class: str, seed_key: str) -> None:
    """Drop watermark rows for loops that stopped existing.

    Aged by ``updated_at`` rather than ``first_seen``: an ongoing deficit is
    re-upserted every scan, so its ``updated_at`` stays fresh no matter how
    old the condition is. Only a key nothing has touched for
    :data:`_WATERMARK_PRUNE_DAYS` — a retired analyst, a deleted source — ages
    out.
    """
    await conn.execute(
        """
        DELETE FROM alert_trigger_watermarks
         WHERE trigger_class = $1
           AND watermark_key <> $2
           AND updated_at < now() - make_interval(days => $3)
        """,
        trigger_class,
        seed_key,
        _WATERMARK_PRUNE_DAYS,
    )


__all__ = [
    "OPTION_PREFIX",
    "build_body",
    "config_from_options",
    "scan_production_deficits",
]
