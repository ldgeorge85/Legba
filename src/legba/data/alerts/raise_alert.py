# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``raise_alert`` — THE one function every producer calls to raise an alert
(L-1, the single alert path).

What one call does, in order:

1. **Refuses an unknown kind** (:class:`~.kinds.UnknownAlertKind`) before any
   write. That is the only exception it raises; everything after this point is
   fail-safe (a broken database or sink degrades to a log line, never into the
   producer — and never toward silence: an episode it cannot read is treated as
   new, so the page goes out).
2. **Maps the severity** onto THE ladder (:mod:`.ladder`; the units' moderate /
   elevated map explicitly).
3. **Records the episode** in ``alerts``: the first raise for a ``dedupe_key``
   opens it; a raise while it is open updates it (occurrences, last raised,
   severity, summary); ``close=True`` closes it (a recovery). An open episode
   nothing has re-raised for its kind's quiet window is closed first, so a
   raise after the window opens a fresh one.
4. **Decides paging**, in one place:

   * ``held`` — the producer measured this raise as not-a-page and said why
     (``hold=``: the trigger scan's steady-state guard or page budget, a band
     move carried by its tick's batch push, a level-only escalation). Held
     raises are logged with the reason; they never page.
   * ``logged`` — no page-rule entry (:mod:`.page_rule`) matches the kind,
     severity and subject; or a recovery of an episode that never paged.
   * ``deduped`` — the episode already paged at this severity or above (a
     critical episode re-pages after ``LEGBA_ALERT_CRITICAL_REPAGE_HOURS``).
   * ``paged`` — a rule entry matched; the entry is stored with the decision.

5. **Writes one ``alert_events`` row** with the decision, its reason and the
   rule entry — every raise, whatever the decision.
6. **Pages** through the shared :class:`~.sinks.AlertSinkDispatcher` (one per
   pool per process, so the transport cooldown is shared by every producer),
   which writes one ``alert_sink_deliveries`` row per sink outcome naming the
   episode and the raise. A level-only hold writes its ``skipped_level_only``
   ledger rows here too, exactly as before.

The dispatcher's ``fan_out`` is called from this module and nowhere else
(``tests/data_pkg/alerts/test_single_alert_path_guard.py`` scans the tree).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from .kinds import KINDS, SUBJECT_KINDS, AlertKind, UnknownAlertKind, require_kind
from .ladder import SEVERITIES, SEVERITY_ORDER, map_alert_severity
from .page_rule import PageRule, current_rule

logger = logging.getLogger(__name__)

ENV_CRITICAL_REPAGE_HOURS = "LEGBA_ALERT_CRITICAL_REPAGE_HOURS"
DEFAULT_CRITICAL_REPAGE_HOURS = 6.0

#: How often (per process) the quiet-window sweep closes stale open episodes.
_SWEEP_EVERY_S = 300.0

_SUMMARY_MAX = 512
_DETAIL_MAX = 4000
_EVIDENCE_MAX = 50

#: ``array_position`` over this array is the ladder rank in SQL (1-based).
_LADDER_SQL = "ARRAY['" + "','".join(SEVERITIES) + "']::text[]"

_CLOSE_STALE_ONE_SQL = """
    UPDATE alerts
       SET state = 'closed', closed_at = $2::timestamptz,
           close_reason = 'quiet: no raise for ' || $3::text || ' h'
     WHERE dedupe_key = $1 AND state = 'open'
       AND last_raised_at < $2::timestamptz - make_interval(secs => $4::float8)
"""

_SWEEP_SQL = """
    UPDATE alerts a
       SET state = 'closed', closed_at = $3::timestamptz,
           close_reason = 'quiet: no raise for ' || q.h::text || ' h'
      FROM (SELECT unnest($1::text[]) AS k, unnest($2::float8[]) AS h) q
     WHERE a.state = 'open' AND a.kind = q.k
       AND a.last_raised_at < $3::timestamptz - make_interval(secs => q.h * 3600)
"""

_UPSERT_SQL = f"""
    INSERT INTO alerts (
        kind, subject_kind, subject_id, dedupe_key, severity, max_severity,
        summary, detail, evidence_refs, raised_by, opened_at, last_raised_at
    ) VALUES ($1, $2, $3, $4, $5, $5, $6, $7, $8::jsonb, $9, $10, $10)
    ON CONFLICT (dedupe_key) WHERE state = 'open' DO UPDATE SET
        occurrences    = alerts.occurrences + 1,
        last_raised_at = EXCLUDED.last_raised_at,
        severity       = EXCLUDED.severity,
        max_severity   = CASE
            WHEN array_position({_LADDER_SQL}, EXCLUDED.severity)
               > array_position({_LADDER_SQL}, alerts.max_severity)
            THEN EXCLUDED.severity ELSE alerts.max_severity END,
        summary        = EXCLUDED.summary,
        detail         = EXCLUDED.detail,
        evidence_refs  = EXCLUDED.evidence_refs
    RETURNING id, (xmax = 0) AS inserted, paged_severity, last_paged_at
"""

_CLOSE_SQL = """
    UPDATE alerts
       SET state = 'closed', closed_at = $2, close_reason = $3,
           last_raised_at = $2, occurrences = occurrences + 1
     WHERE dedupe_key = $1 AND state = 'open'
    RETURNING id, paged_severity, last_paged_at
"""

_EVENT_SQL = """
    INSERT INTO alert_events (
        alert_id, raised_at, kind, severity, subject_kind, subject_id,
        dedupe_key, action, summary, evidence_refs, raised_by, decision,
        decision_reason, rule_entry, rule_version
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, $14, $15)
    RETURNING id
"""

_MARK_PAGED_SQL = f"""
    UPDATE alerts
       SET paged_count = paged_count + 1, last_paged_at = $2,
           paged_severity = CASE
               WHEN paged_severity IS NULL
                 OR array_position({_LADDER_SQL}, $3::text)
                  > array_position({_LADDER_SQL}, paged_severity)
               THEN $3::text ELSE paged_severity END
     WHERE id = $1
"""

_OUTCOMES_SQL = "UPDATE alert_events SET outcomes = $2::jsonb WHERE id = $1"


@dataclass
class RaiseResult:
    """What one raise did. ``paged`` is True when a sink delivered (or the
    transport coalesced it onto its next send)."""

    kind: str
    severity: str
    dedupe_key: str
    action: str
    decision: str
    reason: str
    rule_entry: Optional[str] = None
    alert_id: Optional[str] = None
    event_id: Optional[str] = None
    paged: bool = False
    outcomes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The shared dispatcher (one per pool per process)
# ---------------------------------------------------------------------------

_DISPATCHERS: dict[int, Any] = {}


def register_dispatcher(pool: Any, dispatcher: Any) -> None:
    """Make ``dispatcher`` THE dispatcher for ``pool`` in this process (the
    runtime registers its boot-time instance, so every producer shares one
    transport cooldown and one idempotency set)."""
    _DISPATCHERS[id(pool)] = dispatcher


def dispatcher_for(pool: Any) -> Any:
    disp = _DISPATCHERS.get(id(pool))
    if disp is None:
        from .sinks import AlertSinkDispatcher

        disp = AlertSinkDispatcher(pg_pool=pool)
        _DISPATCHERS[id(pool)] = disp
    return disp


def reset_dispatchers() -> None:
    """Forget the shared dispatchers (tests)."""
    _DISPATCHERS.clear()


# ---------------------------------------------------------------------------
# Pure decision (also the offline replay's decision)
# ---------------------------------------------------------------------------


def critical_repage_hours() -> float:
    raw = (os.environ.get(ENV_CRITICAL_REPAGE_HOURS) or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_CRITICAL_REPAGE_HOURS
    except ValueError:
        return DEFAULT_CRITICAL_REPAGE_HOURS
    return value if value > 0 else DEFAULT_CRITICAL_REPAGE_HOURS


def decide(
    *,
    rule: PageRule,
    kind: str,
    severity: str,
    subject_id: str,
    action: str,
    hold: Optional[str],
    prior_paged_severity: Optional[str],
    prior_last_paged_at: Optional[datetime],
    now: datetime,
) -> tuple[str, str, Optional[str]]:
    """``(decision, reason, rule_entry)`` for one raise. Pure.

    ``action`` is ``opened`` / ``updated`` / ``closed`` /
    ``close_without_episode``; the ``prior_*`` values are the episode's paging
    state BEFORE this raise.
    """
    if hold:
        return "held", f"held by the producer: {hold}"[:500], None
    entry = rule.match(kind, severity, subject_id)
    if entry is None:
        return "logged", f"no page-rule entry pages {kind} at {severity}", None
    described = entry.describe()
    if action in ("closed", "close_without_episode"):
        if prior_paged_severity is None:
            return "logged", "a recovery of an episode that never paged", None
        return "paged", "recovery of an episode that paged", described
    if action == "updated" and prior_paged_severity is not None:
        rank = SEVERITY_ORDER.get(severity, 0)
        prior = SEVERITY_ORDER.get(prior_paged_severity, 0)
        if rank > prior:
            return "paged", f"severity rose from {prior_paged_severity} to {severity}", described
        if severity == "critical" and prior_last_paged_at is not None:
            hours = critical_repage_hours()
            if now - prior_last_paged_at >= timedelta(hours=hours):
                return "paged", f"critical and still open after {hours:g} h", described
        return (
            "deduped",
            f"the open episode already paged at {prior_paged_severity}",
            None,
        )
    if action == "updated":
        return "paged", "the open episode had not paged yet", described
    return "paged", "a new episode", described


# ---------------------------------------------------------------------------
# The one function
# ---------------------------------------------------------------------------


async def raise_alert(
    pool: Any,
    *,
    kind: str,
    severity: Any,
    summary: str,
    subject_id: Any,
    raised_by: str,
    subject_kind: Optional[str] = None,
    detail: str = "",
    dedupe_key: Optional[str] = None,
    evidence_refs: Iterable[Any] = (),
    close: bool = False,
    hold: Optional[str] = None,
    payload: Any = None,
    dispatcher: Any = None,
    rule: Optional[PageRule] = None,
    now: Optional[datetime] = None,
) -> RaiseResult:
    """Raise (or, with ``close=True``, close) one alert. See the module doc.

    ``payload`` is the producer's own :class:`~.sinks.AlertSinkPayload` when it
    shapes one (the escalation edge's enriched payload, the trigger scan's
    batched band push); otherwise one is built from the summary and detail.
    ``dispatcher`` overrides the shared one (tests and rigs).
    """
    spec = require_kind(kind)  # the one exception: refused before any write
    sk = subject_kind or spec.subject_kind
    if sk not in SUBJECT_KINDS:
        raise UnknownAlertKind(f"subject kind {sk!r} is not one of {SUBJECT_KINDS}")
    sev = map_alert_severity(severity, context=f"raise:{kind}")
    subject = str(subject_id if subject_id is not None else "")[:256]
    key = (dedupe_key or f"{kind}:{subject}")[:512]
    now = now or datetime.now(timezone.utc)
    text = str(summary or "").strip()[:_SUMMARY_MAX] or kind
    body = str(detail or "")[:_DETAIL_MAX]
    refs = [str(r) for r in list(evidence_refs)[:_EVIDENCE_MAX] if r not in (None, "")]
    if payload is not None and getattr(payload, "level_only", False) and not hold:
        from .sinks import suppress_level_only_enabled

        if suppress_level_only_enabled():
            hold = f"level-only: {payload.transition}" if payload.transition else "level-only"
    try:
        rule = rule or current_rule()
    except Exception as exc:  # noqa: BLE001 — a broken rule file must not drop alerts
        logger.error("alert_raise.rule_unavailable err=%s — paging every raise", exc)
        rule = _PAGE_EVERYTHING

    # 3. the episode
    action, alert_id, prior_sev, prior_paged_at = (
        "close_without_episode" if close else "opened", None, None, None,
    )
    db = pool is not None and hasattr(pool, "fetchrow")
    if db:
        try:
            action, alert_id, prior_sev, prior_paged_at = await _record_episode(
                pool, spec=spec, subject_kind=sk, subject=subject, key=key,
                severity=sev, summary=text, detail=body, refs=refs,
                raised_by=raised_by, close=close, now=now,
            )
        except Exception as exc:  # noqa: BLE001 — fail toward paging
            logger.warning(
                "alert_raise.episode_write_failed kind=%s key=%s err=%s "
                "(treated as a new episode)", kind, key, exc,
            )

    # 4. the decision
    decision, reason, entry = decide(
        rule=rule, kind=kind, severity=sev, subject_id=subject, action=action,
        hold=hold, prior_paged_severity=prior_sev,
        prior_last_paged_at=prior_paged_at, now=now,
    )
    result = RaiseResult(
        kind=kind, severity=sev, dedupe_key=key, action=action,
        decision=decision, reason=reason, rule_entry=entry, alert_id=alert_id,
    )

    # 5. the raise row
    if db:
        try:
            row = await pool.fetchrow(
                _EVENT_SQL, _uuid(alert_id), now, kind, sev, sk, subject, key,
                action, text, json.dumps(refs), str(raised_by)[:200], decision,
                reason, entry, rule.version or None,
            )
            result.event_id = str(row["id"]) if row is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "alert_raise.event_write_failed kind=%s key=%s decision=%s err=%s",
                kind, key, decision, exc,
            )
    log = logger.info if decision == "paged" else logger.debug
    log(
        "alert_raise kind=%s severity=%s subject=%s action=%s decision=%s reason=%s",
        kind, sev, subject, action, decision, reason,
    )

    # 6. delivery (or the level-only ledger rows)
    if decision == "paged" or (decision == "held" and hold and hold.startswith("level-only")):
        disp = dispatcher if dispatcher is not None else (
            dispatcher_for(pool) if pool is not None else None
        )
        if disp is not None:
            outward = _shape_payload(payload, spec, kind, sev, text, body, subject, sk,
                                     alert_id, result.event_id, paged=decision == "paged")
            # A level-only hold still goes to the dispatcher: it writes the
            # skipped_level_only ledger rows the escalation panel reads, and
            # never posts (the payload says level_only).
            results = await disp.fan_out(outward)
            result.outcomes = [getattr(r, "outcome", str(r)) for r in results or []]
            result.paged = decision == "paged" and any(
                o in ("delivered", "skipped_cooldown") for o in result.outcomes
            )
            if db:
                await _after_delivery(pool, result, sev, now)
    return result


#: The fallback when the rule file cannot be loaded: page every raise at or
#: above medium (a broken rule fails toward paging, loudly).
def _page_everything() -> PageRule:
    from .page_rule import PageRuleEntry

    return PageRule(
        version="fallback:rule-unavailable",
        entries=tuple(PageRuleEntry(kind=k, min_severity="medium") for k in KINDS),
    )


_PAGE_EVERYTHING = _page_everything()


async def _record_episode(
    pool: Any, *, spec: AlertKind, subject_kind: str, subject: str, key: str,
    severity: str, summary: str, detail: str, refs: list[str], raised_by: str,
    close: bool, now: datetime,
) -> tuple[str, Optional[str], Optional[str], Optional[datetime]]:
    await _maybe_sweep(pool, now)
    if spec.quiet_close_hours is not None:
        await pool.execute(
            _CLOSE_STALE_ONE_SQL, key, now, f"{spec.quiet_close_hours:g}",
            float(spec.quiet_close_hours) * 3600.0,
        )
    if close:
        row = await pool.fetchrow(_CLOSE_SQL, key, now, f"recovered: {summary}"[:500])
        if row is None:
            return "close_without_episode", None, None, None
        return "closed", str(row["id"]), row["paged_severity"], row["last_paged_at"]
    row = await pool.fetchrow(
        _UPSERT_SQL, spec.name, subject_kind, subject, key, severity, summary,
        detail, json.dumps(refs), str(raised_by)[:200], now,
    )
    action = "opened" if row["inserted"] else "updated"
    return action, str(row["id"]), row["paged_severity"], row["last_paged_at"]


_LAST_SWEEP: dict[int, float] = {}


async def _maybe_sweep(pool: Any, now: datetime) -> None:
    """Close open episodes nothing has re-raised for their kind's quiet window
    (at most every five minutes per pool). Best-effort."""
    mono = time.monotonic()
    last = _LAST_SWEEP.get(id(pool))
    if last is not None and mono - last < _SWEEP_EVERY_S:
        return
    _LAST_SWEEP[id(pool)] = mono
    kinds = [k for k in KINDS.values() if k.quiet_close_hours is not None]
    try:
        await pool.execute(
            _SWEEP_SQL, [k.name for k in kinds],
            [float(k.quiet_close_hours) for k in kinds], now,  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert_raise.sweep_failed err=%s", exc)


def _shape_payload(
    payload: Any, spec: AlertKind, kind: str, severity: str, summary: str,
    detail: str, subject: str, subject_kind: str, alert_id: Optional[str],
    event_id: Optional[str], *, paged: bool,
) -> Any:
    from .sinks import runtime_alert_payload

    if payload is None:
        payload = runtime_alert_payload(
            channel_name=kind, summary=summary, detail=detail, severity=severity,
            target_id=subject or None, topic=spec.topic,
        )
    update: dict[str, Any] = {
        "kind": kind,
        "severity": severity,
        "alert_id": alert_id,
        "alert_event_id": event_id,
        "rule_paged": paged,
    }
    if not getattr(payload, "topic", ""):
        update["topic"] = spec.topic
    return payload.model_copy(update=update)


async def _after_delivery(pool: Any, result: RaiseResult, severity: str, now: datetime) -> None:
    try:
        if result.paged and result.alert_id:
            await pool.execute(_MARK_PAGED_SQL, _uuid(result.alert_id), now, severity)
        if result.event_id:
            await pool.execute(_OUTCOMES_SQL, _uuid(result.event_id), json.dumps(result.outcomes))
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert_raise.after_delivery_failed alert=%s err=%s", result.alert_id, exc)


def _uuid(raw: Any) -> Any:
    from uuid import UUID

    try:
        return UUID(str(raw)) if raw else None
    except (ValueError, TypeError):
        return None


async def raise_many(pool: Any, raises: Sequence[dict[str, Any]], **common: Any) -> list[RaiseResult]:
    """Raise several alerts in order (the trigger scan's held rows)."""
    out: list[RaiseResult] = []
    for kwargs in raises:
        out.append(await raise_alert(pool, **{**common, **kwargs}))
    return out


__all__ = [
    "DEFAULT_CRITICAL_REPAGE_HOURS",
    "ENV_CRITICAL_REPAGE_HOURS",
    "RaiseResult",
    "UnknownAlertKind",
    "critical_repage_hours",
    "decide",
    "dispatcher_for",
    "raise_alert",
    "raise_many",
    "register_dispatcher",
    "reset_dispatchers",
]
