# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The source-drift pass: classify new drift rows, flag their
dependants, raise ``evidence_drift`` — on a loop, OFF by default.

Every ``LEGBA_EVIDENCE_DRIFT_INTERVAL_S`` (900) the loop runs
:func:`legba.data.evidence_drift_store.classify_pending` over at most
``LEGBA_EVIDENCE_DRIFT_PASS_LIMIT`` (50) unclassified drift rows, walking
dependants to ``LEGBA_EVIDENCE_DRIFT_WALK_DEPTH`` (2) and at most
``LEGBA_EVIDENCE_DRIFT_MAX_DEPENDANTS`` (200) per drift row.

ALERTING — through the one function (``raise_alert``), kind
``evidence_drift``:

* ONE EPISODE PER SOURCE (the host when the drift row has no source), not per
  article: a news source that updates a developing story re-publishes under
  the same address several times a day (the live sample: Dawn re-wrote one
  Swat bombing page four times in 30 hours), so a page per article would be a
  page per edit. The episode is an ``event`` kind with a 24-hour quiet close,
  so a source that keeps editing holds one open episode whose
  ``occurrences`` count the passes that found something; each raise names
  that pass's counts by class.
* Raised only for a subject where something RELIED on a changed page (a flag
  was written). A cosmetic or minor change with no dependant is counted on the
  classification row and the panel, never raised.
* Severity ``low``; ``medium`` when a relied-on page was retracted, replaced
  or is gone. The page rule has no ``evidence_drift`` entry, so these are
  logged and visible on the Watch data and never pushed — an operator who
  wants pages adds an entry.
* A DAILY ROLL-UP (subject ``platform`` / ``evidence_drift_rollup``, severity
  ``info``): the last 24 hours' classifications by source and class, once per
  UTC day.

``LEGBA_EVIDENCE_DRIFT`` off (the default): nothing starts, nothing is
classified, nothing raised. The classification needs no switch of its own
beyond this one; the drift rows it reads are written whether or not it runs.

D-2 — THE ASSESSMENT, right after the classify step of every pass:

* code-only assessments of every new flag-writing change (no extra switch:
  they run whenever the pass runs, ``assessor = code_only``), bounded by
  ``LEGBA_DRIFT_ASSESS_PASS_LIMIT`` (50);
* ``LEGBA_DRIFT_ASSESS_MODEL`` (default OFF): the model step on the free core
  plane (``LEGBA_DRIFT_ASSESS_MODEL_REF``, default ``llm.primary.openai_compat``),
  at most ``LEGBA_DRIFT_ASSESS_MODEL_PER_PASS`` (5) changes per pass;
* every pass records the flags the auto-close rule WOULD close (the shadow
  count, in the daily roll-up); ``LEGBA_DRIFT_AUTO_CLOSE`` (default OFF)
  actually closes them.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "DRIFT_ENV",
    "EvidenceDriftLoop",
    "evidence_drift_enabled",
    "raise_drift_alerts",
    "raise_daily_rollup",
    "assess_llm_factory",
    "run_assessment",
    "run_pass",
    "start_evidence_drift_if_enabled",
]

DRIFT_ENV = "LEGBA_EVIDENCE_DRIFT"
RAISED_BY = "evidence_drift_loop"
_ON = frozenset({"1", "on", "true", "yes"})
_GRAVE = frozenset({"retracted_or_replaced", "gone"})


def evidence_drift_enabled() -> bool:
    return os.environ.get(DRIFT_ENV, "").strip().lower() in _ON


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _fmt_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items(), key=lambda x: -x[1]))


async def raise_drift_alerts(pool: Any, report: Any, *, now: Optional[datetime] = None) -> int:
    """One ``evidence_drift`` raise per subject whose changed pages had
    dependants in this pass. Returns how many were raised."""
    from ..data.alerts.raise_alert import raise_alert  # noqa: PLC0415

    raised = 0
    for subject, flags in sorted(report.flags_by_subject.items()):
        if flags <= 0:
            continue
        counts = report.by_subject.get(subject, {})
        severity = "medium" if any(counts.get(k) for k in _GRAVE) else "low"
        await raise_alert(
            pool, kind="evidence_drift", severity=severity, subject_id=subject,
            summary=f"{subject}: pages changed after they were read ({_fmt_counts(counts)}); "
                    f"{flags} dependant(s) flagged",
            detail="Open flags: GET /api/v1/v3/evidence/drift/flags?source_id=" + subject,
            dedupe_key=f"evidence_drift:{subject}", raised_by=RAISED_BY, now=now,
        )
        raised += 1
    return raised


async def raise_daily_rollup(pool: Any, *, now: Optional[datetime] = None) -> bool:
    """The once-a-UTC-day roll-up. False when today's already exists or the
    last 24 hours classified nothing."""
    from ..data.alerts.raise_alert import raise_alert  # noqa: PLC0415
    from ..data.evidence_drift_reads import drift_counts  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    key = f"evidence_drift:rollup:{now.date().isoformat()}"
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM alert_events WHERE dedupe_key = $1 LIMIT 1", key):
            return False
        counts = await drift_counts(conn, window_hours=24)
    if not counts or not counts.get("classified"):
        return False
    lines = [f"{src}: {_fmt_counts(c)}" for src, c in sorted(counts["by_source"].items())]
    shadow = counts.get("would_auto_close") or {}
    lines.append(f"auto-close shadow ({shadow.get('rule_version', '-')}): {shadow.get('window', 0)} flag(s) "
                 f"would have closed in 24 h, {shadow.get('total', 0)} in all; switch "
                 f"{'on' if shadow.get('switch_on') else 'off'}")
    await raise_alert(
        pool, kind="evidence_drift", severity="info", subject_kind="platform",
        subject_id="evidence_drift_rollup",
        summary=f"Source drift, last 24 h: {counts['classified']} changed page(s) "
                f"({_fmt_counts(counts['by_class'])}); {counts['open_flags_total']} open flag(s); "
                f"{(counts.get('would_auto_close') or {}).get('window', 0)} would auto-close",
        detail="\n".join(lines), dedupe_key=key, raised_by=RAISED_BY, now=now,
    )
    return True


async def run_pass(pool: Any, *, now: Optional[datetime] = None, llm_factory: Any = None) -> Any:
    """One classify + flag + alert pass, then the D-2 assessment (also the
    loop's body). ``llm_factory``: an async callable returning ``(handler,
    model_id)`` for the model step; called only when its switch is on."""
    from ..data.evidence_drift_store import classify_pending  # noqa: PLC0415

    report = await classify_pending(
        pool, limit=int(_num("LEGBA_EVIDENCE_DRIFT_PASS_LIMIT", 50)),
        max_depth=max(0, min(2, int(_num("LEGBA_EVIDENCE_DRIFT_WALK_DEPTH", 2)))),
        max_dependants=max(1, int(_num("LEGBA_EVIDENCE_DRIFT_MAX_DEPENDANTS", 200))),
    )
    if report.classified:
        logger.info("evidence_drift.pass classified=%d flags=%d by_class=%s errors=%d",
                    report.classified, report.flags_written, report.by_class, report.errors)
        await raise_drift_alerts(pool, report, now=now)
    try:
        await run_assessment(pool, llm_factory=llm_factory)
    except Exception as exc:  # noqa: BLE001 — the assessment never stops the pass
        logger.warning("evidence_drift.assess_error err=%s", exc, exc_info=True)
    return report


async def run_assessment(pool: Any, *, llm_factory: Any = None) -> Any:
    """D-2: code-only assessments; the model step when its switch is on and a
    handler is wired; the tracked auto-close when its switch is on."""
    from ..data.evidence_drift_assess_store import (  # noqa: PLC0415
        assess_pending,
        auto_close_enabled,
        auto_close_pending,
        model_cap,
        model_enabled,
    )

    llm, model_id = None, None
    if model_enabled() and llm_factory is not None:
        try:
            llm, model_id = await llm_factory()
        except Exception as exc:  # noqa: BLE001 — model step off this pass, loudly
            logger.warning("evidence_drift.assess_model_unavailable err=%s", exc)
    elif model_enabled():
        logger.warning("evidence_drift.assess_model_on_but_no_handler: the model step is skipped")
    rep = await assess_pending(pool, limit=int(_num("LEGBA_DRIFT_ASSESS_PASS_LIMIT", 50)),
                               llm=llm, model_id=model_id, model_cap_n=model_cap())
    if rep.code_only or rep.with_model or rep.errors:
        logger.info("evidence_drift.assessed code_only=%d with_model=%d model_failed=%d "
                    "history_rewrites=%d late_unexplained=%d model_doubts=%d by_type=%s errors=%d",
                    rep.code_only, rep.with_model, rep.model_failed, rep.history_rewrites,
                    rep.late_unexplained, rep.model_doubts, rep.by_type, rep.errors)
    # The shadow count runs every pass; flags close only with the switch on.
    await auto_close_pending(pool, close=auto_close_enabled())
    return rep


class EvidenceDriftLoop:
    def __init__(self, pool: Any, *, interval_s: Optional[float] = None, llm_factory: Any = None) -> None:
        self._pool = pool
        self._llm_factory = llm_factory
        self._interval = float(interval_s if interval_s is not None
                               else _num("LEGBA_EVIDENCE_DRIFT_INTERVAL_S", 900.0))
        self._task: Optional[asyncio.Task] = None
        self._stopped = False

    async def run_forever(self) -> None:
        while not self._stopped:
            try:
                await run_pass(self._pool, llm_factory=self._llm_factory)
                await raise_daily_rollup(self._pool)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a DB blip: log, keep going
                logger.warning("evidence_drift.pass_error err=%s", exc, exc_info=True)
            await asyncio.sleep(self._interval)

    async def start(self) -> "EvidenceDriftLoop":
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self.run_forever(), name="legba-evidence-drift")
        return self

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


#: The core-plane stack component the model step uses (free, self-hosted).
ASSESS_MODEL_REF_ENV = "LEGBA_DRIFT_ASSESS_MODEL_REF"
DEFAULT_ASSESS_MODEL_REF = "llm.primary.openai_compat"


def assess_llm_factory(registry_client: Any, secrets_resolve: Any) -> Any:
    """An async ``() -> (handler, model_id)`` for the model step, built once
    and cached; the component is ``LEGBA_DRIFT_ASSESS_MODEL_REF``."""
    cache: dict[str, Any] = {}

    async def factory() -> tuple[Any, str]:
        ref = os.environ.get(ASSESS_MODEL_REF_ENV, "").strip() or DEFAULT_ASSESS_MODEL_REF
        if ref not in cache:
            from .analyst_deps_builder import build_llm_handler_from_stack_component  # noqa: PLC0415

            handler = await build_llm_handler_from_stack_component(
                ref, registry_client=registry_client, secrets_resolve=secrets_resolve)
            cfg = getattr(handler, "_cfg", None)
            model = getattr(getattr(cfg, "model_name", None), "raw", None) or "unknown"
            cache[ref] = (handler, f"{ref}:{model}")
        return cache[ref]

    return factory


async def start_evidence_drift_if_enabled(pool: Any, *, llm_factory: Any = None) -> Optional[EvidenceDriftLoop]:
    if not evidence_drift_enabled():
        logger.info("evidence_drift.disabled %s is off", DRIFT_ENV)
        return None
    loop = await EvidenceDriftLoop(pool, llm_factory=llm_factory).start()
    logger.info("evidence_drift.started interval_s=%s", loop._interval)
    return loop
