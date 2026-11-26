# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Acknowledge and snooze an alert EPISODE (S-2, plan 4.4; migration 0261).

Routes (mounted under ``/api/v1/v3``):

  * ``POST   /system/alerts/{episode_id}/ack``     ``{"note"?}``        contributor+
  * ``DELETE /system/alerts/{episode_id}/ack``                          contributor+
  * ``POST   /system/alerts/{episode_id}/snooze``  ``{"duration", "note"?}`` contributor+
  * ``DELETE /system/alerts/{episode_id}/snooze``                       contributor+
  * ``GET    /system/alerts/{episode_id}/attention`` — the state and the
    whole history (closed episodes included)                            reader

WHAT THEY MEAN

* One SHARED instance: an acknowledgement or snooze removes the episode from
  "needs you" (``/v3/since?include=needs_you`` and ``/v3/since/counts``) for
  EVERYONE, not only the person who acted. Every response says so.
* Neither CLOSES the episode, stops its producer recording raises, or changes
  the page rule: pages still go out through the push service exactly as
  before. The plan (4.4) is silent on a snooze suppressing re-pages, so it
  does not.
* An acknowledgement hides the episode until it PAGES at a severity above the
  one it stood at when acknowledged (its baseline). A snooze hides it until
  its time passes (``1h`` / ``4h`` / ``24h`` / ``until_closed`` — a closed
  list, no free-form date) or it pages above its baseline. A later episode
  for the same subject is a new row: it starts unacknowledged.
* Every act is one ``alert_episode_actions`` row and one signed, chained
  ``descriptor_audit_log`` row (namespace ``alert``), both naming the stable
  actor string (``user:<id>``; with sign-in off the bearer's
  ``operator:bearer:<sha12>``) — never a credential.

IDEMPOTENT. Acknowledging an episode whose acknowledgement still holds,
un-acknowledging one with none recorded, repeating the same snooze within
:data:`SNOOZE_RETRY_WINDOW_S`, and un-snoozing with no snooze recorded answer
200 with ``changed: false`` and write nothing. A different snooze REPLACES
the current one (shorter or longer).

WHO. Contributor and admin (``route_policy.ROUTE_MIN_ROLES``); with sign-in
off, the operator class (the bearer) — the fleet's service credential is
refused (``_deps.HUMAN_ONLY_ROUTES``): acknowledging is a person's act. A
caller below admin acts only on the world topic's kinds (the ones its "needs
you" carries); any other episode is 404 to it, as it is invisible to it.
Same-origin protection on cookie writes is the session middleware's.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + ``.alert_log_api``
(+ ``.since_api`` for its counts cache) + ``.route_roles``.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from .alert_log_api import (
    AttentionState,
    attention_baseline,
    attention_state,
    needs_you_kinds,
)
from .api import RegistryAPIDeps, require_bearer
from .route_roles import RequestPrincipal, request_principal, resolve_principal

logger = logging.getLogger(__name__)

ALERT_ACK_ROUTE_VERSION = "2026-11/s2-ack-snooze"
_BASE = "/system/alerts/{episode_id}"

#: The closed list of snooze durations (``None`` = until the episode closes).
SNOOZE_OPTIONS: dict[str, Optional[timedelta]] = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "24h": timedelta(hours=24),
    "until_closed": None,
}
SnoozeOption = Literal["1h", "4h", "24h", "until_closed"]

#: The same snooze repeated within this many seconds is a retry: no new row.
SNOOZE_RETRY_WINDOW_S = 60.0

NOTE_MAX = 280

#: Audit namespace and actions (``descriptor_audit_log``).
AUDIT_NAMESPACE = "alert"
AUDIT_ACTIONS = {
    "acknowledged": "alert_acknowledge",
    "unacknowledged": "alert_unacknowledge",
    "snoozed": "alert_snooze",
    "unsnoozed": "alert_unsnooze",
}

SHARED_NOTE = (
    "Shared: this instance has one alert log, so an acknowledgement or snooze "
    "takes the alert out of \"needs you\" for everyone, not only for you."
)
PAGING_NOTE = (
    "Notifications are unchanged: the alert stays open, each time it rises "
    "is still recorded, and it still sends a notification. It comes back into "
    "\"needs you\" if it rises above the severity it had when you acted."
)

_EPISODE_SQL = """
    SELECT id::text AS id, kind, subject_kind, subject_id, state, severity,
           max_severity, summary, opened_at, closed_at, paged_count,
           paged_severity, last_paged_at,
           acked_at, acked_by, ack_note, ack_severity, snoozed_at, snoozed_by,
           snooze_option, snoozed_until, snooze_severity
      FROM alerts
     WHERE id = $1
"""

_HISTORY_SQL = """
    SELECT id::text AS id, at, action, actor, actor_kind, actor_role, note,
           snooze_option, snoozed_until, episode_severity, baseline_severity
      FROM alert_episode_actions
     WHERE alert_id = $1
     ORDER BY at, id
     LIMIT 500
"""

_ACK_SQL = """
    UPDATE alerts SET acked_at = $2, acked_by = $3, ack_note = $4, ack_severity = $5
     WHERE id = $1
"""
_UNACK_SQL = """
    UPDATE alerts SET acked_at = NULL, acked_by = NULL, ack_note = NULL,
                      ack_severity = NULL
     WHERE id = $1
"""
_SNOOZE_SQL = """
    UPDATE alerts SET snoozed_at = $2, snoozed_by = $3, snooze_option = $4,
                      snoozed_until = $5, snooze_severity = $6
     WHERE id = $1
"""
_UNSNOOZE_SQL = """
    UPDATE alerts SET snoozed_at = NULL, snoozed_by = NULL, snooze_option = NULL,
                      snoozed_until = NULL, snooze_severity = NULL
     WHERE id = $1
"""
_ACTION_SQL = """
    INSERT INTO alert_episode_actions (
        alert_id, at, action, actor, actor_kind, actor_role, note,
        snooze_option, snoozed_until, episode_severity, baseline_severity,
        audit_id
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    RETURNING id::text
"""


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class AckIn(BaseModel):
    """``POST …/ack`` body (optional). Nothing names the actor: the server
    records the authenticated caller."""

    model_config = ConfigDict(extra="forbid")
    note: Optional[str] = Field(default=None, max_length=NOTE_MAX)


class SnoozeIn(BaseModel):
    """``POST …/snooze`` body."""

    model_config = ConfigDict(extra="forbid")
    duration: SnoozeOption
    note: Optional[str] = Field(default=None, max_length=NOTE_MAX)


class AttentionEvent(BaseModel):
    """One act on the episode (``alert_episode_actions``)."""

    id: str
    at: str
    action: Literal["acknowledged", "unacknowledged", "snoozed", "unsnoozed"]
    actor: str
    actor_kind: str
    actor_role: str
    note: Optional[str] = None
    snooze_option: Optional[str] = None
    snoozed_until: Optional[str] = None
    episode_severity: str
    baseline_severity: Optional[str] = None


class AttentionOut(BaseModel):
    """The answer of every route here: the episode, its attention state as
    it now stands, and its whole acknowledge/snooze history."""

    version: str = ALERT_ACK_ROUTE_VERSION
    episode_id: str
    kind: str
    severity: str
    paged_severity: Optional[str] = None
    state: Literal["open", "closed"]
    #: What this request did (``None`` on the GET).
    action: Optional[Literal["acknowledged", "unacknowledged", "snoozed", "unsnoozed"]] = None
    #: False when the request found the state already as asked (no row written).
    changed: bool = False
    attention: AttentionState
    history: list[AttentionEvent] = Field(default_factory=list)
    shared: bool = True
    shared_note: str = SHARED_NOTE
    paging_note: str = PAGING_NOTE
    snooze_options: list[str] = Field(default_factory=lambda: list(SNOOZE_OPTIONS))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (str(v) if v is not None else None)


def _not_found(episode_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "alert_episode_not_found", "episode_id": episode_id},
    )


def _parse_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise _not_found(raw) from None


def _visible(kind: str, *, is_admin: bool) -> bool:
    """The episodes a caller may see or act on: every kind for the admin
    class; the world topic's kinds (its "needs you") below it."""
    allowed = needs_you_kinds(is_admin=is_admin)
    return allowed is None or kind in allowed


def _clean_note(note: Optional[str]) -> Optional[str]:
    if note is None:
        return None
    note = note.strip()
    return note or None


def snooze_until(option: str, *, now: datetime) -> Optional[datetime]:
    """The end a snooze option sets (``None`` for ``until_closed``). Pure."""
    if option not in SNOOZE_OPTIONS:
        raise ValueError(f"unknown snooze option {option!r}")
    span = SNOOZE_OPTIONS[option]
    return None if span is None else now + span


def _event(row: Any) -> AttentionEvent:
    return AttentionEvent(
        id=row["id"], at=_iso(row["at"]) or "", action=row["action"],
        actor=row["actor"], actor_kind=row["actor_kind"], actor_role=row["actor_role"],
        note=row["note"], snooze_option=row["snooze_option"],
        snoozed_until=_iso(row["snoozed_until"]),
        episode_severity=row["episode_severity"],
        baseline_severity=row["baseline_severity"],
    )


async def _answer(
    conn: Any, ep: Any, *, now: datetime, action: Optional[str] = None,
    changed: bool = False,
) -> AttentionOut:
    history = [_event(r) for r in await conn.fetch(_HISTORY_SQL, uuid.UUID(ep["id"]))]
    return AttentionOut(
        episode_id=ep["id"], kind=ep["kind"], severity=ep["severity"],
        paged_severity=ep["paged_severity"], state=ep["state"], action=action,
        changed=changed, attention=attention_state(ep, now=now), history=history,
    )


async def _load(conn: Any, episode_id: uuid.UUID, *, is_admin: bool, lock: bool) -> Any:
    ep = await conn.fetchrow(_EPISODE_SQL + (" FOR UPDATE" if lock else ""), episode_id)
    if ep is None or not _visible(ep["kind"], is_admin=is_admin):
        raise _not_found(str(episode_id))
    return ep


def _refuse_closed(ep: Any) -> None:
    if ep["state"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "alert_episode_closed",
                "episode_id": ep["id"],
                "message": (
                    "this episode has closed; nothing about it needs anyone. "
                    "Its acknowledgement history is kept (GET …/attention)."
                ),
            },
        )


async def _record(
    deps: RegistryAPIDeps, conn: Any, ep: Any, *, who: RequestPrincipal,
    action: str, now: datetime, note: Optional[str] = None,
    option: Optional[str] = None, until: Optional[datetime] = None,
    baseline: Optional[str] = None,
) -> None:
    """The signed audit row, then the episode's history row — on ``conn``,
    inside the caller's transaction (both roll back with the change)."""
    summary: dict[str, Any] = {
        "kind": ep["kind"], "subject_kind": ep["subject_kind"],
        "subject_id": ep["subject_id"], "episode_severity": ep["severity"],
        "paged_severity": ep["paged_severity"], "actor_kind": who.kind,
    }
    if baseline is not None:
        summary["baseline_severity"] = baseline
    if note is not None:
        summary["note"] = note
    if option is not None:
        summary["snooze_option"] = option
        summary["snoozed_until"] = _iso(until)
    entry = await deps.audit_logger.record(
        conn, actor_id=who.actor, actor_role=who.role, namespace=AUDIT_NAMESPACE,
        descriptor_id=ep["id"], action=AUDIT_ACTIONS[action],
        change_summary=summary, occurred_at=now,
    )
    await conn.execute(
        _ACTION_SQL, uuid.UUID(ep["id"]), now, action, who.actor, who.kind, who.role,
        note, option, until, ep["severity"], baseline, entry.id,
    )


def _invalidate_counts() -> None:
    """Drop ``/since/counts``' 30 s reuse so the badge reflects the act."""
    try:
        from .since_api import invalidate_since_counts

        invalidate_since_counts()
    except Exception as exc:  # noqa: BLE001 — a stale badge is not a failure
        logger.info("v3.alert_ack.counts_invalidate_failed err=%s", exc)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_alert_ack_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["v3-alerts"])

    def _is_admin(request: Request) -> bool:
        return resolve_principal(request).is_operator

    async def _act(request: Request, episode_id: str, action: str, body: Any) -> AttentionOut:
        eid = _parse_id(episode_id)
        is_admin = _is_admin(request)
        who = request_principal(request)
        now = datetime.now(timezone.utc)
        async with deps.descriptor_registry.pg.acquire() as conn:
            async with conn.transaction():
                ep = await _load(conn, eid, is_admin=is_admin, lock=True)
                _refuse_closed(ep)
                state = attention_state(ep, now=now)
                changed = False
                if action == "acknowledged":
                    if not state.ack_active:
                        note = _clean_note(body.note if body else None)
                        base = attention_baseline(ep["severity"], ep["paged_severity"])
                        await conn.execute(_ACK_SQL, eid, now, who.actor, note, base)
                        await _record(deps, conn, ep, who=who, action=action, now=now,
                                      note=note, baseline=base)
                        changed = True
                elif action == "unacknowledged":
                    if ep["acked_at"] is not None:
                        await conn.execute(_UNACK_SQL, eid)
                        await _record(deps, conn, ep, who=who, action=action, now=now)
                        changed = True
                elif action == "snoozed":
                    retry = (
                        state.snooze_active and ep["snooze_option"] == body.duration
                        and ep["snoozed_at"] is not None
                        and (now - ep["snoozed_at"]).total_seconds() < SNOOZE_RETRY_WINDOW_S
                    )
                    if not retry:
                        note = _clean_note(body.note)
                        until = snooze_until(body.duration, now=now)
                        base = attention_baseline(ep["severity"], ep["paged_severity"])
                        await conn.execute(_SNOOZE_SQL, eid, now, who.actor,
                                           body.duration, until, base)
                        await _record(deps, conn, ep, who=who, action=action, now=now,
                                      note=note, option=body.duration, until=until,
                                      baseline=base)
                        changed = True
                elif action == "unsnoozed":
                    if ep["snooze_option"] is not None:
                        await conn.execute(_UNSNOOZE_SQL, eid)
                        await _record(deps, conn, ep, who=who, action=action, now=now)
                        changed = True
                ep = await _load(conn, eid, is_admin=is_admin, lock=False)
                out = await _answer(conn, ep, now=now, action=action, changed=changed)
        if changed:
            _invalidate_counts()
            logger.info(
                "v3.alert_ack.%s episode=%s actor=%s", action, ep["id"], who.actor,
            )
        return out

    @router.get(f"{_BASE}/attention", response_model=AttentionOut)
    async def alert_attention(
        request: Request, episode_id: str,
        _principal: str = Depends(require_bearer),
    ) -> AttentionOut:
        """The episode's acknowledgement and snooze as they stand, and every
        act on it (closed episodes too). A reader may look; not change."""
        eid = _parse_id(episode_id)
        async with deps.descriptor_registry.pg.acquire() as conn:
            ep = await _load(conn, eid, is_admin=_is_admin(request), lock=False)
            return await _answer(conn, ep, now=datetime.now(timezone.utc))

    @router.post(f"{_BASE}/ack", response_model=AttentionOut)
    async def alert_acknowledge(
        request: Request, episode_id: str,
        body: Optional[AckIn] = Body(default=None),
        _principal: str = Depends(require_bearer),
    ) -> AttentionOut:
        """Acknowledge: out of "needs you" for everyone until it pages at a
        higher severity. Does not close it or stop its pages."""
        return await _act(request, episode_id, "acknowledged", body)

    @router.delete(f"{_BASE}/ack", response_model=AttentionOut)
    async def alert_unacknowledge(
        request: Request, episode_id: str,
        _principal: str = Depends(require_bearer),
    ) -> AttentionOut:
        """Withdraw the acknowledgement (recorded as its own act)."""
        return await _act(request, episode_id, "unacknowledged", None)

    @router.post(f"{_BASE}/snooze", response_model=AttentionOut)
    async def alert_snooze(
        request: Request, episode_id: str, body: SnoozeIn,
        _principal: str = Depends(require_bearer),
    ) -> AttentionOut:
        """Snooze for one of :data:`SNOOZE_OPTIONS`: out of "needs you" for
        everyone until then, or until it pages at a higher severity. Pages
        are not suppressed."""
        return await _act(request, episode_id, "snoozed", body)

    @router.delete(f"{_BASE}/snooze", response_model=AttentionOut)
    async def alert_unsnooze(
        request: Request, episode_id: str,
        _principal: str = Depends(require_bearer),
    ) -> AttentionOut:
        """End the snooze now (recorded as its own act)."""
        return await _act(request, episode_id, "unsnoozed", None)

    return router


__all__ = [
    "ALERT_ACK_ROUTE_VERSION",
    "AUDIT_ACTIONS",
    "AUDIT_NAMESPACE",
    "AckIn",
    "AttentionEvent",
    "AttentionOut",
    "NOTE_MAX",
    "SNOOZE_OPTIONS",
    "SNOOZE_RETRY_WINDOW_S",
    "SnoozeIn",
    "build_alert_ack_router",
    "snooze_until",
]
