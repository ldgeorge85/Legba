# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``verify_backfill`` sub-handler — the sweep over findings whose verify leg never ran.

Every rolling deploy recreates the runtime actor host. An analyst run in flight
at that moment has already WRITTEN its finding (``analyst_outputs``
``kind='finding'``), but the faithfulness verify leg that writes its critique
(``kind='critique'``, title ``Faithfulness verify (score …)``, ``derived_from =
[finding id]``) never runs — so the finding stands published with no critique,
unverified forever. ``scripts/rejudge_floor_critiques.py --missing --apply`` is
the hand tool; this sweep runs the SAME selection and the SAME verify-and-write
(``legba.runtime.rejudge_floor``) on cadence so nobody has to.

Per tick (a cadence-driven deterministic META analyst, like the sibling sweeps):

  * SELECT the next ``backfill_batch`` (default 50) critique-less cited findings
    of the last :data:`LOOKBACK` older than ``min_age_minutes`` (default 30 —
    a verify leg still running is never raced), oldest first
    (:func:`~legba.runtime.rejudge_floor.select_missing_findings`: cited, of an
    analyst the critic covers, not a deterministic rollup, not superseded).
  * For each, the critic's verify pass over the STORED finding against the
    analyzed analyst's CURRENT judge chain (resolved per analyst as
    ``dapr_host`` wires it — :func:`~legba.runtime.rejudge_floor.live_judge_resolver`),
    and the critique the critic would have written, stamped
    ``verification.judge_trigger = 'verify_backfill'`` and
    ``verification.backfill_of_run = <the finding's run_id>``. Each row runs on
    its own pooled connection.
  * A judge that does not answer writes NOTHING (never a floor): the finding
    stays missing and comes back next tick. Five such failures in a row stop the
    tick (the chain is down). A finding that fails on a tick where another row
    was verified (the chain is healthy, so this row is the problem) counts an
    attempt in process memory; at :data:`MAX_ATTEMPTS` it is PARKED — left out
    of the selection until the process restarts — so a few poison rows never
    wedge the oldest-first queue. A row the critic's kind guard would not grade
    is parked at once. The tick stops starting rows after :data:`TICK_BUDGET_S`.
  * Skipped without a read: ``LEGBA_VERIFY_LLM_JUDGE`` off (every re-run would be
    a floor, and a floor is never written) or no registry/secrets wiring.

Output ``data`` keys (the cadence receipt):
    examined          int — findings selected this run
    verified          int — critiques written this run
    failed            int — findings the judge did not adjudicate / the write
                            refused (nothing written; retried, or parked)
    still_missing     int — critique-less findings in scope after this run (the
                            whole backlog over the lookback, parked included)
    skipped           int — findings the kind guard declined, or that gained a
                            critique between the selection and the write
    parked            int — findings currently parked in this process
    skipped_no_judge  int — 1 when the tick did nothing (judge flag off, or the
                            registry / secrets resolver not wired)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "verify_backfill"

#: ``deps.extras`` key for the registry client, threaded by
#: ``runtime/analyst_deps_reenrich.py`` only when the bound sub-handler is
#: ``verify_backfill``. The secrets resolver is ``deps.secrets_resolve``.
REGISTRY_DEPS_EXTRA_KEY = "verify_backfill_registry"

#: Findings verified per tick — the SELECT ``LIMIT``; the ``backfill_batch`` option.
_DEFAULT_BACKFILL_BATCH = 50
#: The ``min_age_minutes`` option's default (the script's ``--min-age-minutes``).
_DEFAULT_MIN_AGE_MINUTES = 30

#: How far back the sweep looks. Older debt is the script's (``--since``).
LOOKBACK = timedelta(days=7)
#: Failed attempts (counted on healthy ticks only) after which a finding is
#: parked for the life of the process.
MAX_ATTEMPTS = 3
#: A tick starts no new row after this many seconds (the cadence is 30 min).
TICK_BUDGET_S = 20 * 60.0

#: Per-process attempt counts, keyed by finding id. A restart forgets them —
#: every parked finding gets one more try after each deploy, which is the point.
_ATTEMPTS: dict[UUID, int] = {}


def _zero_counters() -> dict[str, int]:
    return {
        "examined": 0,
        "verified": 0,
        "failed": 0,
        "still_missing": 0,
        "skipped": 0,
        "parked": 0,
        "skipped_no_judge": 0,
    }


def _extra(deps: Any | None, key: str) -> Any | None:
    extras = getattr(deps, "extras", None) if deps is not None else None
    return extras.get(key) if isinstance(extras, Mapping) else None


def _parked() -> list[UUID]:
    return [fid for fid, n in _ATTEMPTS.items() if n >= MAX_ATTEMPTS]


def _record_attempts(outcomes: list[Any]) -> None:
    """Count the failed rows' attempts — only when the chain verified another
    row this tick — and park kind-guard skips at once. A verified row's count is
    dropped."""
    from ....runtime import rejudge_floor as RF

    healthy = any(o.outcome == RF.OUTCOME_BACKFILLED for o in outcomes)
    for o in outcomes:
        if o.finding_id is None:
            continue
        if o.outcome == RF.OUTCOME_BACKFILLED:
            _ATTEMPTS.pop(o.finding_id, None)
        elif o.outcome == RF.OUTCOME_SKIPPED:
            _ATTEMPTS[o.finding_id] = MAX_ATTEMPTS
        elif healthy and o.outcome in (RF.OUTCOME_JUDGE_FAILED, RF.OUTCOME_DLQ):
            _ATTEMPTS[o.finding_id] = _ATTEMPTS.get(o.finding_id, 0) + 1


async def _sweep(
    pool: Any,
    *,
    resolve: Any,
    backfill_batch: int,
    min_age_minutes: int,
    now: datetime,
    verify_fn: Any = None,
) -> dict[str, int]:
    from ....runtime import rejudge_floor as RF

    counters = _zero_counters()
    since = now - LOOKBACK
    async with pool.acquire() as conn:
        rows = await RF.select_missing_findings(
            conn, since=since, until=now, limit=backfill_batch,
            min_age_minutes=min_age_minutes, exclude=_parked(),
        )
    counters["examined"] = len(rows)
    if rows:
        summary = await RF.apply(
            None, rows, resolve, verify_fn=verify_fn, acquire=pool.acquire,
            max_seconds=TICK_BUDGET_S,
        )
        counts = summary.counts()
        counters["verified"] = counts.get(RF.OUTCOME_BACKFILLED, 0)
        counters["failed"] = (
            counts.get(RF.OUTCOME_JUDGE_FAILED, 0) + counts.get(RF.OUTCOME_DLQ, 0)
            + counts.get(RF.OUTCOME_WEAK_SKIPPED, 0)
        )
        counters["skipped"] = counts.get(RF.OUTCOME_SKIPPED, 0) + counts.get(RF.OUTCOME_RACED, 0)
        _record_attempts(summary.outcomes)
        if summary.stopped_early:
            logger.warning("verify_backfill.stopped_early %s", summary.stopped_early)
    async with pool.acquire() as conn:
        counters["still_missing"] = await RF.count_missing(
            conn, since=since, until=now, min_age_minutes=min_age_minutes,
        )
    counters["parked"] = len(_parked())
    return counters


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    title = (
        f"Verify backfill: verified {counters.get('verified', 0)} of "
        f"{counters.get('examined', 0)} examined, {counters.get('failed', 0)} failed, "
        f"{counters.get('still_missing', 0)} still missing"
    )
    tags = ["deterministic", SUB_HANDLER_NAME]
    if counters.get("verified", 0):
        tags.append("verified")
    return FindingPayload(
        title=title[:2048],
        body="\n".join(f"{k}={v}" for k, v in counters.items())[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see the module docstring. ``deps is None``
    (unit-test path) yields a zeroed run. Usage is reported zero, like the
    sibling sweeps: the judge calls a tick makes are NOT counted in it (each
    critique it writes carries its judge stamps — ``judge_llm_ref`` and the
    chain block — as the critic's do)."""
    counters = _zero_counters()
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        from ...provenance.verify import _llm_judge_enabled

        registry = _extra(deps, REGISTRY_DEPS_EXTRA_KEY)
        secrets_resolve = getattr(deps, "secrets_resolve", None)
        if not _llm_judge_enabled():
            counters["skipped_no_judge"] = 1
            logger.warning(
                "verify_backfill.judge_off — LEGBA_VERIFY_LLM_JUDGE is off; every "
                "re-run would be a floor, and a floor is never written; skipping",
            )
        elif registry is None or secrets_resolve is None:
            counters["skipped_no_judge"] = 1
            logger.warning(
                "verify_backfill.no_judge_wiring — the registry client or the "
                "secrets resolver is not on deps; findings left missing this tick",
            )
        else:
            from ....runtime import rejudge_floor as RF

            resolve, close = RF.live_judge_resolver(
                registry_client=registry, secrets_resolve=secrets_resolve,
            )
            try:
                counters = await _sweep(
                    pool, resolve=resolve,
                    backfill_batch=int(options.get("backfill_batch", _DEFAULT_BACKFILL_BATCH)),
                    min_age_minutes=int(
                        options.get("min_age_minutes", _DEFAULT_MIN_AGE_MINUTES)
                    ),
                    now=datetime.now(timezone.utc),
                )
            except Exception as exc:
                logger.warning("verify_backfill.failed err=%s", exc)
            finally:
                await close()
    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "LOOKBACK",
    "MAX_ATTEMPTS",
    "REGISTRY_DEPS_EXTRA_KEY",
    "SUB_HANDLER_NAME",
    "TICK_BUDGET_S",
    "handle",
]
