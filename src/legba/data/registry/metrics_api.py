# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prometheus ``/metrics`` exposition for the registry (resilience P2).

A single unauthenticated GET endpoint that scrapes the live substrate and
renders the Prometheus **text exposition format** (0.0.4) directly — no
``prometheus_client`` dependency. The values are REAL counters/gauges
already tracked in Postgres, not synthesized:

  * ``legba_signals_total``            — rows in ``signals`` (canonical only;
                                         dedup'd snapshot rows excluded), the
                                         ingest throughput counter.
  * ``legba_findings_total``           — rows in ``analyst_outputs`` grouped
                                         by ``kind`` (finding / situation /
                                         critique / …) — analyst output
                                         throughput.
  * ``legba_dlq_open``                 — open (``resolution IS NULL``) rows in
                                         ``descriptor_dead_letter`` and
                                         ``output_dead_letter``, labelled by
                                         ``queue``. The DLQ-depth gauge the
                                         alert rules watch.
  * ``legba_signal_ingest_age_seconds`` — age of the most recent
                                         ``signals.fetched_at`` — the
                                         cursor-frozen gauge (climbs without
                                         bound when ingestion stalls).
  * ``legba_analyst_tokens_used``      — today's ``budget_ledger.tokens_used``
                                         per analyst (per-analyst budget spend).
  * ``legba_analyst_cost_estimate_usd`` — today's
                                         ``budget_ledger.cost_estimate_usd``
                                         per analyst.
  * ``legba_budget_envelope_tokens_used`` /
    ``legba_budget_envelope_tokens_cap`` — today's global envelope rollup +
                                         operator cap (the budget-exhaustion
                                         signal; cap omitted when unset).
  * ``legba_metrics_scrape_ok``        — 1 when the scrape collected cleanly,
                                         0 when a query failed (so a broken
                                         substrate read is itself alertable
                                         rather than a silent empty scrape).

Consumer lag: the durable JetStream consumer depth lives on the runtime
worker (``JobQueue.consumer_pending`` in
:mod:`legba.runtime.jobs.queue`), not on the registry's substrate — the
registry process holds no JetStream consumer binding. Exposing it here
would require fabricating a number this process doesn't observe, which the
no-stubs rule forbids; the in-Postgres DLQ-depth + ingest-age gauges cover
the registry-observable backpressure. When a runtime-side ``/metrics`` is
added it owns the consumer-lag gauge from the live ``ConsumerInfo``.

Mount (app-level, NOT bearer-gated — Prometheus scrapers don't carry the
operator token, matching the unauthenticated ``/healthz`` convention)::

    from .metrics_api import build_metrics_router
    app.include_router(build_metrics_router(deps))

The companion alert-rules file is ``deploy/prometheus/legba_alerts.yml``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .api import RegistryAPIDeps

logger = logging.getLogger(__name__)

# Prometheus text exposition content type (version pinned per the spec).
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

__all__ = ["build_metrics_router", "render_exposition"]


# ---------------------------------------------------------------------------
# Exposition rendering (pure — unit-testable without a DB)
# ---------------------------------------------------------------------------


def _escape_label(value: str) -> str:
    """Escape a label value per the exposition format (\\, \" and newline)."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _fmt_value(value: float | int) -> str:
    """Render a metric value — ints stay int-shaped, floats get a decimal."""
    if isinstance(value, bool):  # bool is an int subclass; normalise to 0/1
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def render_exposition(
    *,
    families: list[dict[str, Any]],
) -> str:
    """Render metric families into the Prometheus text exposition format.

    Each family is ``{"name", "type", "help", "samples"}`` where ``samples``
    is a list of ``{"labels": {k: v}, "value": number}``. ``HELP`` / ``TYPE``
    header lines are emitted once per family per the spec; a family with no
    samples still emits its header (so a scraper sees the series exists).
    """
    lines: list[str] = []
    for fam in families:
        name = fam["name"]
        lines.append(f"# HELP {name} {fam['help']}")
        lines.append(f"# TYPE {name} {fam['type']}")
        for sample in fam.get("samples", []):
            labels = sample.get("labels") or {}
            if labels:
                rendered = ",".join(
                    f'{k}="{_escape_label(str(v))}"'
                    for k, v in labels.items()
                )
                series = f"{name}{{{rendered}}}"
            else:
                series = name
            lines.append(f"{series} {_fmt_value(sample['value'])}")
    # Exposition format requires a trailing newline.
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Substrate scrape
# ---------------------------------------------------------------------------


async def _collect_families(pool: Any) -> list[dict[str, Any]]:
    """Query the substrate and assemble the metric families.

    Raises on the first failed query so the caller can flip
    ``legba_metrics_scrape_ok`` to 0 rather than emit a partial scrape that
    silently reads as "all gauges zero".
    """
    async with pool.acquire() as conn:
        # Ingest throughput — canonical signals only (dedup'd snapshot dups
        # carry a non-self canonical_signal_id; exclude them so the counter
        # tracks distinct ingested items, mirroring the read API's filter).
        signals_total = await conn.fetchval(
            "SELECT COUNT(*) FROM signals "
            "WHERE canonical_signal_id IS NULL OR canonical_signal_id = id"
        )

        # Analyst output throughput, by kind.
        output_rows = await conn.fetch(
            "SELECT kind, COUNT(*) AS n FROM analyst_outputs GROUP BY kind"
        )

        # DLQ depth — open rows per queue.
        descriptor_dlq_open = await conn.fetchval(
            "SELECT COUNT(*) FROM descriptor_dead_letter "
            "WHERE resolution IS NULL"
        )
        output_dlq_open = await conn.fetchval(
            "SELECT COUNT(*) FROM output_dead_letter WHERE resolution IS NULL"
        )

        # Cursor-frozen gauge — age of the freshest ingested signal.
        latest_fetched = await conn.fetchval(
            "SELECT MAX(fetched_at) FROM signals"
        )

        # Per-analyst budget spend for today's bucket.
        today = datetime.now(tz=timezone.utc).date()
        ledger_rows = await conn.fetch(
            "SELECT analyst_id, "
            "       SUM(tokens_used)::BIGINT AS tokens, "
            "       SUM(cost_estimate_usd)::NUMERIC AS cost "
            "FROM budget_ledger WHERE bucket = $1 GROUP BY analyst_id",
            today,
        )

        # Global envelope rollup + cap for today.
        envelope_rollup = await conn.fetchval(
            "SELECT COALESCE(SUM(tokens_used), 0)::BIGINT "
            "FROM budget_ledger WHERE bucket = $1",
            today,
        )
        envelope_cap = await conn.fetchval(
            "SELECT tokens_cap FROM global_budget_envelope WHERE bucket = $1",
            today,
        )

    now = datetime.now(tz=timezone.utc)
    ingest_age = (
        max(0.0, (now - latest_fetched).total_seconds())
        if latest_fetched is not None
        else None
    )

    families: list[dict[str, Any]] = [
        {
            "name": "legba_signals_total",
            "type": "counter",
            "help": "Canonical signals ingested into the substrate.",
            "samples": [{"labels": {}, "value": int(signals_total or 0)}],
        },
        {
            "name": "legba_findings_total",
            "type": "counter",
            "help": "Analyst outputs in the substrate, labelled by kind.",
            "samples": [
                {"labels": {"kind": r["kind"]}, "value": int(r["n"])}
                for r in output_rows
            ],
        },
        {
            "name": "legba_dlq_open",
            "type": "gauge",
            "help": "Open (unresolved) dead-letter rows, by queue.",
            "samples": [
                {
                    "labels": {"queue": "descriptor"},
                    "value": int(descriptor_dlq_open or 0),
                },
                {
                    "labels": {"queue": "output"},
                    "value": int(output_dlq_open or 0),
                },
            ],
        },
        {
            "name": "legba_analyst_tokens_used",
            "type": "gauge",
            "help": "Tokens used today per analyst (budget_ledger bucket=today).",
            "samples": [
                {
                    "labels": {"analyst_id": r["analyst_id"]},
                    "value": int(r["tokens"] or 0),
                }
                for r in ledger_rows
            ],
        },
        {
            "name": "legba_analyst_cost_estimate_usd",
            "type": "gauge",
            "help": "Estimated USD cost today per analyst (budget_ledger).",
            "samples": [
                {
                    "labels": {"analyst_id": r["analyst_id"]},
                    "value": float(r["cost"] or 0),
                }
                for r in ledger_rows
            ],
        },
        {
            "name": "legba_budget_envelope_tokens_used",
            "type": "gauge",
            "help": "Global token spend today across all analysts.",
            "samples": [
                {"labels": {}, "value": int(envelope_rollup or 0)},
            ],
        },
    ]

    # Cursor-frozen gauge — only emit a value when we have an observation
    # (an empty signals table has no ingest age; emitting 0 would falsely
    # read as "ingested a moment ago").
    families.append(
        {
            "name": "legba_signal_ingest_age_seconds",
            "type": "gauge",
            "help": (
                "Seconds since the most recent signal was ingested "
                "(cursor-frozen indicator)."
            ),
            "samples": (
                [{"labels": {}, "value": float(ingest_age)}]
                if ingest_age is not None
                else []
            ),
        }
    )

    # Envelope cap — only when an operator has configured one for today.
    if envelope_cap is not None:
        families.append(
            {
                "name": "legba_budget_envelope_tokens_cap",
                "type": "gauge",
                "help": "Operator-set global token cap for today (if any).",
                "samples": [{"labels": {}, "value": int(envelope_cap)}],
            }
        )

    return families


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_metrics_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the Prometheus ``/metrics`` router bound to the registry deps.

    Mount at app level (no prefix, no bearer gate)::

        app.include_router(build_metrics_router(deps))
    """
    router = APIRouter(tags=["metrics"])

    @router.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        pool = deps.descriptor_registry.pg
        scrape_ok = True
        families: list[dict[str, Any]]
        try:
            families = await _collect_families(pool)
        except Exception as exc:  # noqa: BLE001 — a broken scrape is alertable
            logger.warning("metrics.scrape.failed err=%s", exc)
            scrape_ok = False
            families = []

        families.append(
            {
                "name": "legba_metrics_scrape_ok",
                "type": "gauge",
                "help": "1 when the last substrate scrape succeeded, else 0.",
                "samples": [{"labels": {}, "value": 1 if scrape_ok else 0}],
            }
        )

        body = render_exposition(families=families)
        return PlainTextResponse(content=body, media_type=_CONTENT_TYPE)

    return router
