# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""forecast_resolver — the forecast ledger's scheduled resolution job.

A ``deterministic`` META analyst (sub_handler=forecast_resolver), every 6 h.
One tick = :func:`legba.data.forecast_resolution.run_resolution_job`: the
sealed open questions at or past their due date get their resolver run over
the record (no model), a PROPOSAL is written to
``forecast_resolution_records`` when the reading is new (idempotent on its
fingerprint), ``series_threshold`` is settled by rule, and every new proposal
— and every question that waits on a human judgement — raises one alert
through the ONE alert path (``raise_alert``, kind ``forecast_resolution``).
Every other kind closes only on the operator's confirmation or override
(``scripts/resolve_forecasts.py``).

The returned summary is a TRACE_ONLY receipt (counts + per-question outcome):
never a finding on a trust surface.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from ...forecast_resolution import RESOLUTION_JOB_VERSION, run_resolution_job
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "forecast_resolver"
ALERT_CHANNEL = "forecast_resolution"
_ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}

def _alert_fn(pool: Any) -> Any:
    async def _alert(summary: str, detail: str, severity: str) -> None:
        # L-1: the ONE alert path. Subject = the question (its slug leads the
        # summary, "Forecast <slug>: ..."); one episode per question and
        # reading, so the same proposal raised again on a later tick updates
        # its episode instead of paging again.
        try:
            from ...alerts.raise_alert import raise_alert

            head = str(summary or "")
            slug = head[len("Forecast "):].split(":", 1)[0] if head.startswith("Forecast ") else ""
            await raise_alert(
                pool, kind=ALERT_CHANNEL, severity=severity, summary=head, detail=detail,
                subject_id=slug, dedupe_key=f"{ALERT_CHANNEL}:{head[:300]}",
                raised_by=SUB_HANDLER_NAME,
            )
        except Exception as exc:  # noqa: BLE001 — an alert never fails the tick
            logger.warning("forecast_resolver.alert_failed err=%s", exc)

    return _alert


def build_receipt(report: Mapping[str, Any], warnings: list[str]) -> FindingPayload:
    qs = report.get("questions") or []
    head = (f"Forecast resolver: {len(qs)} due, {report.get('written', 0)} new proposals, "
            f"{report.get('settled', 0)} settled by rule, {report.get('errors', 0)} errors")
    body = "\n".join(
        f"{q.get('slug')}: {q.get('outcome')} ({q.get('record')}); judgements="
        f"{[j.get('key') for j in q.get('judgements') or []]}" + (f" error={q['error']}" if q.get("error") else "")
        for q in qs
    )
    return FindingPayload(
        title=head[:2048], body=(body or "no question due")[:65536], confidence=1.0, evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME, "job_version": RESOLUTION_JOB_VERSION,
            "resolver_version": report.get("resolver_version"), "as_of": report.get("as_of"),
            "due": len(qs), "written": report.get("written", 0), "unchanged": report.get("unchanged", 0),
            "settled": report.get("settled", 0), "alerts": report.get("alerts", 0),
            "errors": report.get("errors", 0),
            "questions": [{k: q.get(k) for k in ("slug", "outcome", "record", "record_id", "error")} for q in qs],
            "warnings": warnings,
        },
    )


async def handle(inputs: list[dict[str, Any]], options: Mapping[str, Any], deps: Any | None) -> AnalystMethodResult:
    """Sub-handler entry point — a single global run; no pool ⇒ an honest empty receipt."""
    warnings: list[str] = []
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        warnings.append("forecast_resolver.no_pool")
        return AnalystMethodResult(finding=build_receipt({}, warnings), usage=dict(_ZERO_USAGE))
    async with pool.acquire() as conn:
        report = await run_resolution_job(conn, alert=_alert_fn(pool))
    logger.info("forecast_resolver.tick due=%d written=%d settled=%d errors=%d",
                len(report["questions"]), report["written"], report["settled"], report["errors"])
    return AnalystMethodResult(finding=build_receipt(report, warnings), usage=dict(_ZERO_USAGE))


__all__ = ["ALERT_CHANNEL", "SUB_HANDLER_NAME", "build_receipt", "handle"]
