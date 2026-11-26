# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reader's own reads of two admin lists: read-only versions of what the
reader surfaces need.

The reader surfaces were
calling routes whose minimum role is ``admin``. Most of those reads were
already safe for a reader and were lowered in ``route_policy`` as they are.
Two were NOT, because the admin answer carries more than a reader may see,
so the admin route keeps its role and the reader surfaces read a narrow
projection here instead:

  * ``GET /v3/roster?family=target|analyst|source`` — the subject / analyst /
    source ROSTER: id, name, family, kind, state, version, created, and the
    summary body the console's rosters render (``identity.name`` and the
    ``scope`` keys of :data:`descriptor_list_weight.SUMMARY_SCOPE_KEYS`, plus
    how many sources a subject declares). For a source row, the few
    reader-facing columns the source list lifts to the top (geo, languages,
    tags, the public assurance grade). NEVER the descriptor body beyond that
    (prompts, credentials and vault references, acquisition config, endpoints
    and the owner live there), never ``owner``, ``owner_tenant``,
    ``type_signature``, ``warnings``, ``inherits`` or the signature columns.
    The admin route ``/registry/descriptors`` (and ``/registry/sources``)
    answer the whole record and stay ``admin``.
  * ``GET /v3/watch/alerts?hours=`` — the Watch surface's alert log for a
    reader: the world topic's kinds only (the reader's "needs you" rule,
    ``alert_log_api.NEEDS_YOU_READER_TOPIC`` — the engine's own health never
    reaches a reader), and without who acknowledged or snoozed (and their
    note), who raised it, the page sinks' outcomes, the page rule's file
    path or its load error. The admin route ``/v3/system/alerts/log`` keeps
    the whole log and stays ``admin``.

Read-only. Registry-slim: stdlib + fastapi + pydantic + ``.api`` + the alert
package's leaf modules (the ``alert_log_api`` precedent).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .alert_log_api import (
    DEFAULT_WINDOW_HOURS,
    MAX_WINDOW_HOURS,
    AlertLogOut,
    needs_you_kinds,
    read_alert_log,
)
from .api import RegistryAPIDeps, load_assurance_grades, require_bearer
from .descriptor import DescriptorPredicate, Family
from .descriptor_list_weight import summary_body

logger = logging.getLogger(__name__)

READER_READS_VERSION = "2026-11/ra1"
ROSTER_ROUTE = "/roster"
WATCH_ALERTS_ROUTE = "/watch/alerts"

#: The families a reader's roster may list.
RosterFamily = Literal["target", "analyst", "source"]

#: The default (and the most) rows a roster answers.
ROSTER_DEFAULT_LIMIT = 500
ROSTER_MAX_LIMIT = 1000

class RosterRow(BaseModel):
    """One roster row: what the reader surfaces render, nothing more."""

    descriptor_id: str
    family: str
    name: str
    kind: Optional[str] = None
    state: str
    version: str
    is_head: bool = True
    created_at: datetime
    #: ``descriptor_list_weight.summary_body``: ``identity.name``, the roster's
    #: ``scope`` keys and ``declared_sources``.
    body: dict[str, Any] = Field(default_factory=dict)
    # A source row's reader-facing columns (``SourceDescriptorOut``'s).
    geo: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    assurance_grade: Optional[str] = None


def _str_list(v: Any) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else []


def roster_row(row: Any) -> RosterRow:
    """A ``DescriptorRow`` → its :class:`RosterRow`. Pure."""
    body = row.body if isinstance(getattr(row, "body", None), dict) else {}
    out = RosterRow(
        descriptor_id=row.descriptor_id,
        family=getattr(row.family, "value", str(row.family)),
        name=row.name,
        kind=row.kind,
        state=row.state,
        version=row.version,
        is_head=bool(row.is_head),
        created_at=row.created_at,
        body=summary_body(body),
    )
    if out.family == Family.SOURCE.value:
        scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
        out.geo = _str_list(scope.get("geo"))
        out.languages = _str_list(scope.get("languages"))
        out.tags = _str_list(scope.get("tags"))
        identity = body.get("identity") if isinstance(body.get("identity"), dict) else {}
        if out.kind is None and isinstance(identity.get("kind"), str):
            out.kind = identity["kind"]
        # The id a source's signals carry (``source_id``) — the subject's
        # Sources tab joins on it.
        if isinstance(identity.get("id"), str):
            out.body.setdefault("identity", {})["id"] = identity["id"]
    return out


class ReaderAttention(BaseModel):
    """An episode's acknowledgement and snooze, as a reader sees them: whether
    each holds and when — never who, never their note."""

    acked_at: Optional[str] = None
    ack_active: bool = False
    snoozed_until: Optional[str] = None
    snooze_option: Optional[str] = None
    snooze_active: bool = False
    resurfaced: bool = False
    hidden_from_needs_you: bool = False


class ReaderAlertEpisode(BaseModel):
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
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None
    attention: ReaderAttention = Field(default_factory=ReaderAttention)


class ReaderPagedAlert(BaseModel):
    event_id: str
    alert_id: Optional[str] = None
    raised_at: str
    kind: str
    severity: str
    subject_kind: str
    subject_id: str
    summary: str
    why: str
    rule_entry: Optional[str] = None
    rule_version: Optional[str] = None
    delivered: bool = False


class ReaderAlertKind(BaseModel):
    kind: str
    group: str
    topic: str
    subject_kind: str
    summary: str
    pages_when: list[str] = Field(default_factory=list)


class ReaderAlertCount(BaseModel):
    kind: str
    severity: str
    raised: int = 0
    paged: int = 0
    logged: int = 0
    held: int = 0
    deduped: int = 0


class ReaderAlertLogOut(BaseModel):
    """The Watch's alert log for a reader: ``/v3/system/alerts/log``'s shape,
    narrowed (see the module docstring)."""

    version: str = READER_READS_VERSION
    measured: bool
    not_measured_reason: Optional[str] = None
    window_hours: int
    read_at: str
    as_of: Optional[str] = None
    topic: str = "world"
    ladder: list[str] = Field(default_factory=list)
    rule_version: Optional[str] = None
    kinds: list[ReaderAlertKind] = Field(default_factory=list)
    open: list[ReaderAlertEpisode] = Field(default_factory=list)
    closed: list[ReaderAlertEpisode] = Field(default_factory=list)
    counts: list[ReaderAlertCount] = Field(default_factory=list)
    paged: list[ReaderPagedAlert] = Field(default_factory=list)


def reader_alert_log(full: AlertLogOut, *, kinds: list[str]) -> ReaderAlertLogOut:
    """The admin answer (already narrowed to ``kinds`` by the query) → the
    reader's. Pure: every field is copied by name, so a field added to the
    admin shape never reaches a reader until it is named here."""
    allowed = set(kinds)

    def ep(e: Any) -> ReaderAlertEpisode:
        a = e.attention
        return ReaderAlertEpisode(
            id=e.id, kind=e.kind, group=e.group, topic=e.topic,
            subject_kind=e.subject_kind, subject_id=e.subject_id,
            severity=e.severity, max_severity=e.max_severity, summary=e.summary,
            opened_at=e.opened_at, last_raised_at=e.last_raised_at,
            occurrences=e.occurrences, paged_count=e.paged_count,
            paged_severity=e.paged_severity, last_paged_at=e.last_paged_at,
            closed_at=e.closed_at, close_reason=e.close_reason,
            attention=ReaderAttention(
                acked_at=a.acked_at, ack_active=a.ack_active,
                snoozed_until=a.snoozed_until, snooze_option=a.snooze_option,
                snooze_active=a.snooze_active, resurfaced=a.resurfaced,
                hidden_from_needs_you=a.hidden_from_needs_you,
            ),
        )

    return ReaderAlertLogOut(
        measured=full.measured,
        not_measured_reason=full.not_measured_reason,
        window_hours=full.window_hours,
        read_at=full.read_at,
        as_of=full.as_of,
        ladder=list(full.ladder),
        rule_version=full.rule_version,
        kinds=[
            ReaderAlertKind(
                kind=k.kind, group=k.group, topic=k.topic,
                subject_kind=k.subject_kind, summary=k.summary,
                pages_when=list(k.pages_when),
            )
            for k in full.kinds if k.kind in allowed
        ],
        open=[ep(e) for e in full.open if e.kind in allowed],
        closed=[ep(e) for e in full.closed if e.kind in allowed],
        counts=[
            ReaderAlertCount(
                kind=c.kind, severity=c.severity, raised=c.raised, paged=c.paged,
                logged=c.logged, held=c.held, deduped=c.deduped,
            )
            for c in full.counts if c.kind in allowed
        ],
        paged=[
            ReaderPagedAlert(
                event_id=p.event_id, alert_id=p.alert_id, raised_at=p.raised_at,
                kind=p.kind, severity=p.severity, subject_kind=p.subject_kind,
                subject_id=p.subject_id, summary=p.summary, why=p.why,
                rule_entry=p.rule_entry, rule_version=p.rule_version,
                delivered=p.delivered,
            )
            for p in full.paged if p.kind in allowed
        ],
    )


def build_reader_reads_router(deps: RegistryAPIDeps) -> APIRouter:
    """The read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["reader"])

    def _deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(ROSTER_ROUTE, response_model=list[RosterRow])
    async def reader_roster(
        request: Request,
        family: RosterFamily = Query(...),
        limit: int = Query(default=ROSTER_DEFAULT_LIMIT, ge=1, le=ROSTER_MAX_LIMIT),
        _principal: str = Depends(require_bearer),
    ) -> list[RosterRow]:
        d = _deps(request)
        rows = await d.descriptor_registry.list(DescriptorPredicate(
            family=Family(family), head_only=True, limit=limit,
        ))
        out = [roster_row(r) for r in rows]
        if family == Family.SOURCE.value and out:
            grades = await load_assurance_grades(
                d.descriptor_registry.pg, [o.descriptor_id for o in out],
            )
            for o in out:
                o.assurance_grade = grades.get(o.descriptor_id)
        return out

    @router.get(WATCH_ALERTS_ROUTE, response_model=ReaderAlertLogOut)
    async def reader_watch_alerts(
        request: Request,
        hours: int = Query(
            default=DEFAULT_WINDOW_HOURS, ge=1, le=MAX_WINDOW_HOURS,
            description="Trailing window in hours (closed episodes, counts, pages).",
        ),
        _principal: str = Depends(require_bearer),
    ) -> ReaderAlertLogOut:
        kinds = needs_you_kinds(is_admin=False) or []
        try:
            async with _deps(request).descriptor_registry.pg.acquire() as conn:
                full = await read_alert_log(conn, hours=hours, kinds=kinds)
        except Exception as exc:  # noqa: BLE001 — a polling surface never 500s
            logger.info("v3.watch.alerts.unavailable err=%s", type(exc).__name__)
            return ReaderAlertLogOut(
                measured=False,
                not_measured_reason=f"no connection ({type(exc).__name__})",
                window_hours=hours,
                read_at=datetime.now(timezone.utc).isoformat(),
            )
        return reader_alert_log(full, kinds=kinds)

    return router


__all__ = [
    "READER_READS_VERSION",
    "ROSTER_ROUTE",
    "WATCH_ALERTS_ROUTE",
    "ReaderAlertLogOut",
    "RosterRow",
    "build_reader_reads_router",
    "reader_alert_log",
    "roster_row",
]
