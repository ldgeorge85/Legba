# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/alerts/log?hours=168`` — the alert log, the data behind the
console's Watch surface (L-1, the single alert path).

One answer, five parts:

* ``open``       — every open episode (kind, severity, subject, when it opened
  and was last raised, how often, whether and at what severity it paged);
* ``closed``     — episodes closed in the window, newest first, with why;
* ``counts``     — raises in the window by kind and severity, each split by
  decision (paged / logged / held / deduped);
* ``paged``      — what paged in the window and why: the raise, the reason, the
  page-rule entry that matched, and the per-sink outcomes the delivery ledger
  recorded;
* ``rule``       — the page rule this process loaded (path, version, entries) and
  the closed list of kinds with their topics and groups.

HONESTY. A failed read is ``measured: false`` with the reason, at HTTP 200 (a
polling surface never 500s). ``as_of`` is the newest raise read.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + the alert package's
leaf modules (``kinds``, ``ladder``, ``page_rule``: stdlib + PyYAML).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..alerts.kinds import KINDS
from ..alerts.ladder import SEVERITIES
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

ALERT_LOG_ROUTE_VERSION = "2026-11/l1-single-alert-path"
_ROUTE = "/system/alerts/log"
DEFAULT_WINDOW_HOURS = 168
MAX_WINDOW_HOURS = 24 * 30
_OPEN_LIMIT = 500
_CLOSED_LIMIT = 200
_PAGED_LIMIT = 300

# ---------------------------------------------------------------------------
# Acknowledge / snooze (S-2, migration 0261) — the episode's ATTENTION state
# ---------------------------------------------------------------------------
#
# An acknowledgement or a snooze hides an open episode from "needs you" for
# EVERYONE (one shared instance). Neither closes it nor changes paging. Each
# carries a BASELINE severity (the higher of the episode's severity and the
# severity it had paged at, when the person acted); the episode comes back
# when it PAGES above that baseline (``paged_severity`` only rises when a page
# goes out). A snooze also lapses when its time passes ('until_closed' lapses
# with the episode). This predicate is the ONE statement of that rule in SQL;
# :func:`attention_state` is the same rule in Python (tested to agree). It
# says nothing about WHICH open episodes need a person (the inclusion rule):
# it only removes the ones a person has dealt with, whatever that rule is.

#: ``array_position`` over this array is the ladder rank (1-based).
LADDER_SQL = "ARRAY['" + "','".join(SEVERITIES) + "']::text[]"

#: The attention columns every episode read carries (0261).
_ATTENTION_COLUMNS = (
    "acked_at, acked_by, ack_note, ack_severity, snoozed_at, snoozed_by, "
    "snooze_option, snoozed_until, snooze_severity"
)

_PAGED_RANK = f"array_position({LADDER_SQL}, COALESCE(paged_severity, 'info'))"

#: TRUE when a person's acknowledgement or snooze currently hides the episode
#: (a row of ``alerts``; ``now()`` is the statement's clock). NULL-safe.
ATTENTION_HIDDEN_SQL = f"""(
        COALESCE(acked_at IS NOT NULL
                 AND {_PAGED_RANK} <= array_position({LADDER_SQL}, ack_severity), false)
     OR COALESCE((snooze_option = 'until_closed' OR snoozed_until > now())
                 AND {_PAGED_RANK} <= array_position({LADDER_SQL}, snooze_severity), false)
    )"""

_RANK = {s: i for i, s in enumerate(SEVERITIES)}


def severity_rank(sev: Optional[str]) -> int:
    """Ladder rank (``info`` = 0); unknown / None = 0."""
    return _RANK.get(str(sev), 0) if sev else 0


def attention_baseline(severity: str, paged_severity: Optional[str]) -> str:
    """The baseline an acknowledgement or snooze records: the higher of the
    episode's current severity and the severity it had paged at."""
    if paged_severity and severity_rank(paged_severity) > severity_rank(severity):
        return paged_severity
    return severity


class AttentionState(BaseModel):
    """An episode's acknowledgement and snooze as they stand (S-2).

    ``ack_active`` / ``snooze_active`` — whether each currently hides the
    episode from "needs you"; ``resurfaced`` — a recorded acknowledgement or
    unexpired snooze no longer hides it because the episode paged above its
    baseline. ``hidden_from_needs_you`` = either is active. The recorded
    fields stay after a lapse (and after the episode closes) so the record of
    who acknowledged it is never lost; the full history is the episode's
    ``/attention`` route."""

    acked_at: Optional[str] = None
    acked_by: Optional[str] = None
    ack_note: Optional[str] = None
    ack_severity: Optional[str] = None
    ack_active: bool = False
    snoozed_at: Optional[str] = None
    snoozed_by: Optional[str] = None
    snooze_option: Optional[str] = None
    snoozed_until: Optional[str] = None
    snooze_severity: Optional[str] = None
    snooze_active: bool = False
    resurfaced: bool = False
    hidden_from_needs_you: bool = False


def attention_state(row: Any, *, now: datetime) -> AttentionState:
    """The Python statement of :data:`ATTENTION_HIDDEN_SQL` over one row. Pure."""
    def g(k: str) -> Any:
        try:
            return row[k]
        except (KeyError, IndexError):
            return None

    paged = severity_rank(g("paged_severity")) if g("paged_severity") else 0
    acked = g("acked_at") is not None and g("ack_severity") is not None
    ack_held = acked and paged <= severity_rank(g("ack_severity"))
    until = g("snoozed_until")
    snooze_running = g("snooze_option") == "until_closed" or (
        isinstance(until, datetime) and until > now
    )
    snooze_held = (
        snooze_running and g("snooze_severity") is not None
        and paged <= severity_rank(g("snooze_severity"))
    )
    if str(g("state") or "open") == "closed":
        # A closed episode needs nobody; its record is kept, nothing is active.
        ack_active = snooze_active = False
        resurfaced = False
    else:
        ack_active, snooze_active = ack_held, bool(snooze_held)
        resurfaced = (acked and not ack_held) or (snooze_running and not snooze_held)
    return AttentionState(
        acked_at=_iso(g("acked_at")), acked_by=g("acked_by"), ack_note=g("ack_note"),
        ack_severity=g("ack_severity"), ack_active=ack_active,
        snoozed_at=_iso(g("snoozed_at")), snoozed_by=g("snoozed_by"),
        snooze_option=g("snooze_option"), snoozed_until=_iso(until),
        snooze_severity=g("snooze_severity"), snooze_active=snooze_active,
        resurfaced=resurfaced, hidden_from_needs_you=ack_active or snooze_active,
    )


_OPEN_SQL = f"""
    SELECT id, kind, subject_kind, subject_id, severity, max_severity, summary,
           opened_at, last_raised_at, occurrences, paged_count, paged_severity,
           last_paged_at, raised_by, {_ATTENTION_COLUMNS}
      FROM alerts
     WHERE state = 'open'
       AND ($2::text[] IS NULL OR kind = ANY($2::text[]))
     ORDER BY last_raised_at DESC
     LIMIT $1
"""

_CLOSED_SQL = f"""
    SELECT id, kind, subject_kind, subject_id, severity, max_severity, summary,
           opened_at, last_raised_at, occurrences, paged_count, paged_severity,
           last_paged_at, raised_by, closed_at, close_reason, {_ATTENTION_COLUMNS}
      FROM alerts
     WHERE state = 'closed' AND closed_at >= $1
       AND ($3::text[] IS NULL OR kind = ANY($3::text[]))
     ORDER BY closed_at DESC
     LIMIT $2
"""

_COUNTS_SQL = """
    SELECT kind, severity, decision, count(*)::int AS n, max(raised_at) AS newest
      FROM alert_events
     WHERE raised_at >= $1
       AND ($2::text[] IS NULL OR kind = ANY($2::text[]))
     GROUP BY 1, 2, 3
"""

_PAGED_SQL = """
    SELECT e.id, e.alert_id, e.raised_at, e.kind, e.severity, e.subject_kind,
           e.subject_id, e.summary, e.action, e.decision_reason, e.rule_entry,
           e.rule_version, e.outcomes, e.raised_by
      FROM alert_events e
     WHERE e.decision = 'paged' AND e.raised_at >= $1
       AND ($3::text[] IS NULL OR e.kind = ANY($3::text[]))
     ORDER BY e.raised_at DESC
     LIMIT $2
"""


# ---------------------------------------------------------------------------
# "Needs you" — the alert log's section on ``GET /v3/since`` (S-1, plan 4.1)
# ---------------------------------------------------------------------------

#: The topic a non-admin caller sees. The alert log route itself is admin
#: ("operator side"); the reader's "needs you" carries the world's kinds
#: only, never the engine's own health (source stalls, host disk, nightly).
NEEDS_YOU_READER_TOPIC = "world"

#: Open episodes that PAGED ("needs you" is paged-only —
#: an episode that only sits at medium or above without paging is on the
#: Watch page's alert log, not here), which OPENED or PAGED after the cursor
#: ($1). ``$2`` = the kinds the caller may see (NULL =
#: every kind). S-2: minus the episodes a person acknowledged or snoozed
#: (:data:`ATTENTION_HIDDEN_SQL` — independent of the inclusion rule above it).
#: Severity-ranked worst first, then newest activity. The
#: predicate rides ``idx_alerts_open_last_raised`` (partial, state='open');
#: the open set is small by construction (one row per open dedupe key).
NEEDS_YOU_SQL = f"""
    SELECT id::text AS id, kind, subject_kind, subject_id, severity, summary,
           opened_at, last_raised_at, last_paged_at, paged_count,
           paged_severity, acked_at, snoozed_until,
           count(*) OVER () AS total
      FROM alerts
     WHERE state = 'open'
       AND paged_count > 0
       AND (opened_at > $1 OR last_paged_at > $1)
       AND ($2::text[] IS NULL OR kind = ANY($2::text[]))
       AND NOT {ATTENTION_HIDDEN_SQL}
     ORDER BY CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                            WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC,
              GREATEST(opened_at, COALESCE(last_paged_at, opened_at)) DESC,
              id DESC
     LIMIT $3
"""


def needs_you_kinds(*, is_admin: bool) -> Optional[list[str]]:
    """The alert kinds a caller's "needs you" may carry: every kind (``None``)
    for an admin-class caller, the world topic's kinds for anyone else."""
    if is_admin:
        return None
    return sorted(k.name for k in KINDS.values() if k.topic == NEEDS_YOU_READER_TOPIC)


class NeedsYouEpisode(BaseModel):
    """One open alert-log episode that needs a person (plan 4.1).

    ``why`` is ``paged``: only episodes that paged are listed (roll 84);
    ``paged_severity`` / ``last_paged_at`` say at what severity and when.
    ``acked_at`` / ``snoozed_until`` are the recorded acknowledgement time and
    snooze end (S-2, migration 0261). An acknowledged or snoozed episode is
    NOT listed, so on a listed item a non-null ``acked_at`` means "was
    acknowledged, came back because it paged at a higher severity", and a
    ``snoozed_until`` means a snooze that has passed or was overridden the
    same way ('until it closes' snoozes record no end: ``None``)."""

    id: str
    kind: str
    group: Optional[str] = None
    topic: Optional[str] = None
    severity: str
    summary: str
    subject_kind: str
    subject_id: str
    opened_at: str
    last_raised_at: str
    last_paged_at: Optional[str] = None
    paged_severity: Optional[str] = None
    why: str
    acked_at: Optional[str] = None
    snoozed_until: Optional[str] = None


class NeedsYouSection(BaseModel):
    items: list[NeedsYouEpisode] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


def needs_you_item(row: Any) -> NeedsYouEpisode:
    """One ``NEEDS_YOU_SQL`` row → its :class:`NeedsYouEpisode`. Pure."""
    spec = KINDS.get(str(row["kind"]))
    return NeedsYouEpisode(
        id=str(row["id"]), kind=str(row["kind"]),
        group=spec.group if spec else None, topic=spec.topic if spec else None,
        severity=row["severity"], summary=row["summary"],
        subject_kind=row["subject_kind"], subject_id=row["subject_id"] or "",
        opened_at=_iso(row["opened_at"]) or "",
        last_raised_at=_iso(row["last_raised_at"]) or "",
        last_paged_at=_iso(row["last_paged_at"]),
        paged_severity=row["paged_severity"],
        # The query lists paged episodes only; a row that never paged (a
        # caller's own rows) says so rather than claiming a page.
        why="paged" if int(row["paged_count"] or 0) > 0 else "not_paged",
        acked_at=_iso(_get(row, "acked_at")),
        snoozed_until=_iso(_get(row, "snoozed_until")),
    )


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


async def fetch_needs_you(
    conn: Any, cursor: datetime, *, is_admin: bool, cap: int,
) -> list[Any]:
    """The capped ``NEEDS_YOU_SQL`` rows (each carries the full ``total``)."""
    return list(await conn.fetch(
        NEEDS_YOU_SQL, cursor, needs_you_kinds(is_admin=is_admin), int(cap),
    ))


class AlertEpisode(BaseModel):
    id: str
    kind: str
    group: Optional[str] = None
    topic: Optional[str] = None
    subject_kind: str
    subject_id: str
    severity: str
    max_severity: str
    summary: str
    opened_at: str
    last_raised_at: str
    occurrences: int
    paged_count: int
    paged_severity: Optional[str] = None
    last_paged_at: Optional[str] = None
    raised_by: str
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None
    #: S-2 — the acknowledgement and snooze as they stand (kept on a closed
    #: episode as its record).
    attention: AttentionState = Field(default_factory=AttentionState)


class AlertCount(BaseModel):
    kind: str
    severity: str
    raised: int = 0
    paged: int = 0
    logged: int = 0
    held: int = 0
    deduped: int = 0


class PagedAlert(BaseModel):
    event_id: str
    alert_id: Optional[str] = None
    raised_at: str
    kind: str
    severity: str
    subject_kind: str
    subject_id: str
    summary: str
    action: str
    why: str
    rule_entry: Optional[str] = None
    rule_version: Optional[str] = None
    outcomes: list[str] = Field(default_factory=list)
    delivered: bool = False
    raised_by: str


class AlertKindOut(BaseModel):
    kind: str
    group: str
    topic: str
    subject_kind: str
    lifecycle: str
    summary: str
    pages_when: list[str] = Field(default_factory=list)


class AlertLogOut(BaseModel):
    version: str = ALERT_LOG_ROUTE_VERSION
    measured: bool
    not_measured_reason: Optional[str] = None
    window_hours: int
    read_at: str
    as_of: Optional[str] = None
    ladder: list[str] = Field(default_factory=lambda: list(SEVERITIES))
    rule_version: Optional[str] = None
    rule_path: Optional[str] = None
    rule_error: Optional[str] = None
    kinds: list[AlertKindOut] = Field(default_factory=list)
    open: list[AlertEpisode] = Field(default_factory=list)
    closed: list[AlertEpisode] = Field(default_factory=list)
    counts: list[AlertCount] = Field(default_factory=list)
    paged: list[PagedAlert] = Field(default_factory=list)


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (str(v) if v is not None else None)


def _episode(row: dict[str, Any], *, now: Optional[datetime] = None) -> AlertEpisode:
    spec = KINDS.get(str(row["kind"]))
    return AlertEpisode(
        id=str(row["id"]), kind=row["kind"],
        group=spec.group if spec else None, topic=spec.topic if spec else None,
        subject_kind=row["subject_kind"], subject_id=row["subject_id"] or "",
        severity=row["severity"], max_severity=row["max_severity"],
        summary=row["summary"], opened_at=_iso(row["opened_at"]) or "",
        last_raised_at=_iso(row["last_raised_at"]) or "",
        occurrences=int(row["occurrences"] or 0), paged_count=int(row["paged_count"] or 0),
        paged_severity=row.get("paged_severity"), last_paged_at=_iso(row.get("last_paged_at")),
        raised_by=row.get("raised_by") or "",
        closed_at=_iso(row.get("closed_at")), close_reason=row.get("close_reason"),
        attention=attention_state(row, now=now or datetime.now(timezone.utc)),
    )


def _outcomes(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [str(x) for x in raw] if isinstance(raw, list) else []


def _rule_block() -> tuple[Optional[Any], Optional[str]]:
    try:
        from ..alerts.page_rule import load_rule

        return load_rule(), None
    except Exception as exc:  # noqa: BLE001 — the log still answers
        return None, f"{type(exc).__name__}: {exc}"


def _kinds_out(rule: Any) -> list[AlertKindOut]:
    out = []
    for k in KINDS.values():
        pages = [e.describe() for e in rule.entries if e.kind == k.name] if rule else []
        out.append(AlertKindOut(
            kind=k.name, group=k.group, topic=k.topic, subject_kind=k.subject_kind,
            lifecycle=k.lifecycle, summary=k.summary, pages_when=pages,
        ))
    return out


async def read_alert_log(
    conn: Any, *, hours: int, now: Optional[datetime] = None,
    kinds: Optional[list[str]] = None,
) -> AlertLogOut:
    """The route's whole answer off an open connection (also the test seam).

    ``kinds`` narrows every part to those alert kinds (``None`` = every kind:
    the admin route). The reader's Watch read (``reader_reads_api``) passes
    the world topic's kinds (:func:`needs_you_kinds`), so the engine's own
    health never reaches a reader."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    rule, rule_error = _rule_block()
    base = dict(
        window_hours=hours, read_at=now.isoformat(), kinds=_kinds_out(rule),
        rule_version=getattr(rule, "version", None), rule_path=getattr(rule, "path", None),
        rule_error=rule_error,
    )
    try:
        open_rows = [dict(r) for r in await conn.fetch(_OPEN_SQL, _OPEN_LIMIT, kinds)]
        closed_rows = [dict(r) for r in await conn.fetch(
            _CLOSED_SQL, since, _CLOSED_LIMIT, kinds)]
        count_rows = [dict(r) for r in await conn.fetch(_COUNTS_SQL, since, kinds)]
        paged_rows = [dict(r) for r in await conn.fetch(
            _PAGED_SQL, since, _PAGED_LIMIT, kinds)]
    except Exception as exc:  # noqa: BLE001 — a polling surface never 500s
        logger.info("v3.system.alert_log.unavailable err=%s", exc)
        return AlertLogOut(
            measured=False,
            not_measured_reason=f"the alert log could not be read ({type(exc).__name__})",
            **base,
        )
    counts: dict[tuple[str, str], AlertCount] = {}
    newest: Optional[datetime] = None
    for r in count_rows:
        key = (r["kind"], r["severity"])
        c = counts.setdefault(key, AlertCount(kind=r["kind"], severity=r["severity"]))
        n = int(r["n"])
        c.raised += n
        if r["decision"] in ("paged", "logged", "held", "deduped"):
            setattr(c, r["decision"], getattr(c, r["decision"]) + n)
        if isinstance(r.get("newest"), datetime) and (newest is None or r["newest"] > newest):
            newest = r["newest"]
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    paged = []
    for r in paged_rows:
        outcomes = _outcomes(r.get("outcomes"))
        paged.append(PagedAlert(
            event_id=str(r["id"]), alert_id=str(r["alert_id"]) if r.get("alert_id") else None,
            raised_at=_iso(r["raised_at"]) or "", kind=r["kind"], severity=r["severity"],
            subject_kind=r["subject_kind"], subject_id=r["subject_id"] or "",
            summary=r["summary"], action=r["action"], why=r["decision_reason"],
            rule_entry=r.get("rule_entry"), rule_version=r.get("rule_version"),
            outcomes=outcomes, delivered="delivered" in outcomes,
            raised_by=r.get("raised_by") or "",
        ))
    return AlertLogOut(
        measured=True,
        as_of=_iso(newest),
        open=[_episode(r, now=now) for r in open_rows],
        closed=[_episode({**r, "state": "closed"}, now=now) for r in closed_rows],
        counts=sorted(
            counts.values(), key=lambda c: (c.kind, -rank.get(c.severity, 0))
        ),
        paged=paged,
        **base,
    )


def register_alert_log_routes(router: APIRouter, deps: RegistryAPIDeps) -> None:
    """Declare the alert-log route on ``router`` (the v3 router)."""

    @router.get(_ROUTE, response_model=AlertLogOut)
    async def system_alert_log(
        hours: int = Query(
            default=DEFAULT_WINDOW_HOURS, ge=1, le=MAX_WINDOW_HOURS,
            description="Trailing window in hours (closed episodes, counts, pages).",
        ),
        _principal: str = Depends(require_bearer),
    ) -> AlertLogOut:
        try:
            async with deps.descriptor_registry.pg.acquire() as conn:
                return await read_alert_log(conn, hours=hours)
        except Exception as exc:  # noqa: BLE001 — a polling surface never 500s
            logger.info("v3.system.alert_log.unavailable err=%s", exc)
            return AlertLogOut(
                measured=False,
                not_measured_reason=f"no connection ({type(exc).__name__})",
                window_hours=hours,
                read_at=datetime.now(timezone.utc).isoformat(),
            )


__all__ = [
    "ALERT_LOG_ROUTE_VERSION",
    "ATTENTION_HIDDEN_SQL",
    "AttentionState",
    "LADDER_SQL",
    "attention_baseline",
    "attention_state",
    "severity_rank",
    "AlertLogOut",
    "DEFAULT_WINDOW_HOURS",
    "MAX_WINDOW_HOURS",
    "NEEDS_YOU_SQL",
    "NeedsYouEpisode",
    "NeedsYouSection",
    "fetch_needs_you",
    "needs_you_item",
    "needs_you_kinds",
    "read_alert_log",
    "register_alert_log_routes",
]
