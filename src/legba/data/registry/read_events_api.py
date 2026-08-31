# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read telemetry — ``/api/v1/read-events`` (D2e, the oracle wager's instrument).

THE GAP this closes, in one line: the substrate has ~80 tables receipting
every WRITE and not one recording a READ.

``planning/CAMPAIGN_2026-08-29/PREMISE_REASON_TO_EXIST.md`` §2.1 says the
Glass-Tower ruling — "a self-driving organ **the operator reads**" — is
falsified in its reading half by "zero finding-level drills in 15 days". That
number is an inference off Caddy access logs. It is the best evidence that
existed and it is not good enough to settle a 90-day wager, because an access
log cannot tell a panel mount from a build event, and cannot see a citation
chip being followed at all. §5 Option 1 names the fix directly: *"Instrument
reading (opens, drills)."* This module is that instrument.

Two endpoints, deliberately asymmetric in weight:

``POST /api/v1/read-events``
    The ingest. A batch of client-observed events lands as ONE prepared
    INSERT run over the batch on one connection (``executemany``) — no
    joins, no subject resolution, no product-table reads. It is the dumbest
    endpoint in the registry and that is the design: telemetry that is slow
    enough to notice is telemetry the operator turns off, and a read plane
    that taxes reading defeats itself. Accepts up to ``_MAX_BATCH`` events;
    ``202 Accepted`` with a written count.

``GET /api/v1/read-events/rollup``
    The scoreboard. Events by kind by day over a bounded window (default 30d,
    max 365d), plus the wager's headline scalars: reads today, reads this
    week, and the brief-read day count — "on how many of the last N days did
    the operator open the morning product at all?". This is what day 90 reads.

CONVENTIONS — the same as the rest of the registry HTTP plane:

  * bearer-gated via :func:`~legba.data.registry.api.require_bearer` (the
    single dependency; there is no separate read/write gate here, matching
    ``watchlist_api``);
  * validation errors are 422 with a stated reason, never a silent coercion —
    with ONE deliberate exception documented under `_PARTIAL_BATCH` below;
  * no ``session_nonce`` is minted server-side and none is treated as an
    identity: single-operator, single-tenant, and the nonce exists only to
    separate one long morning from eight visits.

WHY THE INGEST IS 202 AND NOT 201
    A read receipt is not a resource the caller will ever fetch back by id.
    The client fires and forgets (``sendBeacon`` on unload cannot read a
    response at all). 202 states the honest contract: accepted, counted, no
    addressable thing created for you.

.. _`_PARTIAL_BATCH`:

THE ONE PLACE WE DO NOT FAIL LOUD, AND WHY
    A batch is validated per-event, and an invalid event is DROPPED rather
    than 422-ing the whole batch — the response reports ``accepted`` and
    ``rejected`` counts so the drop is never silent. This is the opposite of
    the house rule and it earns the exception: the batch is assembled by a
    browser across several seconds of unrelated user actions, so one
    malformed event (a stale kind after a UI refactor, a subject id that came
    back null from a racing fetch) would otherwise discard a whole morning of
    genuine reading. Losing real evidence to protect a schema we already
    enforce in the CHECK constraints would corrupt the measurement in the
    direction that flatters nobody. The counts make the loss auditable, and
    the server logs each rejection reason at WARNING.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

# The closed vocabulary. Mirrors the CHECK in migration 0189 exactly — if
# these two lists ever disagree, ingest starts 500-ing on a constraint
# violation rather than silently writing a kind no rollup counts.
READ_EVENT_KINDS = (
    "panel_open",
    "workspace_open",
    "finding_open",
    "lineage_walk",
    "citation_drill",
    "consult_open",
    "brief_read",
)

ReadEventKind = Literal[
    "panel_open",
    "workspace_open",
    "finding_open",
    "lineage_walk",
    "citation_drill",
    "consult_open",
    "brief_read",
]

# One POST every few seconds carrying at most a few dozen events is the
# expected shape; 500 is a generous ceiling that still bounds a single
# INSERT's parameter count well inside asyncpg's limits.
_MAX_BATCH = 500

_MAX_SUBJECT_KIND = 64
_MAX_SUBJECT_ID = 512
_MAX_WORKSPACE = 64
_MAX_NONCE = 128

# Clock-skew tolerance, mirroring the migration's CHECK. A browser whose
# clock is set ahead must not be able to backdate a month of reading into one
# day, so anything beyond this is rejected rather than clamped — clamping
# would fabricate an attention timestamp, which is the one thing this table
# must never do.
_MAX_FUTURE_SKEW = timedelta(hours=1)

# Events older than this are almost certainly a replayed localStorage queue
# from a tab that sat open for weeks; accepting them would smear a single
# session across the whole window. Dropped with a logged reason.
_MAX_AGE = timedelta(days=7)

_DEFAULT_ROLLUP_DAYS = 30
_MAX_ROLLUP_DAYS = 365


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class ReadEventIn(BaseModel):
    """One observed act of reading, as the browser saw it.

    ``occurred_at`` is CLIENT time on purpose (see 0189's header): the
    evidence is the operator's attention, which happened in the browser, not
    when a batched POST happened to drain. The server records its own clock
    separately in ``received_at``.
    """

    occurred_at: datetime
    event_kind: ReadEventKind
    workspace: str = Field(min_length=1, max_length=_MAX_WORKSPACE)
    session_nonce: str = Field(min_length=1, max_length=_MAX_NONCE)
    subject_kind: str | None = Field(default=None, max_length=_MAX_SUBJECT_KIND)
    subject_id: str | None = Field(default=None, max_length=_MAX_SUBJECT_ID)
    dwell_ms: int | None = Field(default=None, ge=0)


class ReadEventBatch(BaseModel):
    """A debounced batch. One POST per few seconds is the intended cadence."""

    events: list[ReadEventIn] = Field(min_length=1, max_length=_MAX_BATCH)


class IngestResult(BaseModel):
    """Honest counts. ``rejected`` is never hidden — see the module docstring."""

    accepted: int
    rejected: int
    reasons: list[str] = Field(default_factory=list)


class RollupDay(BaseModel):
    """One (day, kind) cell of the scoreboard."""

    day: date
    event_kind: str
    events: int
    sessions: int


class RollupTotals(BaseModel):
    """The wager's headline scalars, computed over the same window."""

    reads_today: int
    reads_this_week: int
    brief_reads_today: int
    brief_reads_this_week: int
    # "On how many of the last `window_days` days did the operator open the
    # morning product at all?" — the single number the 90-day verdict turns
    # on, per PREMISE §5 Option 1.
    brief_read_days: int
    active_days: int
    sessions_this_week: int
    window_days: int


class RollupOut(BaseModel):
    """Daily rollup + totals. The whole read side of the plane."""

    since: date
    totals: RollupTotals
    days: list[RollupDay]


# ---------------------------------------------------------------------------
# Per-event validation (pure — unit-testable without the app)
# ---------------------------------------------------------------------------


def validate_event(event: ReadEventIn, *, now: datetime) -> str | None:
    """Return a rejection reason, or ``None`` when the event is writable.

    Pure and clock-injected so the skew rules are testable without freezing
    time. Everything checked here is ALSO a CHECK constraint in 0189 — this
    function exists so a bad event costs one dropped row instead of a failed
    transaction that takes the whole batch's good rows with it.
    """
    occurred = event.occurred_at
    if occurred.tzinfo is None:
        # A naive timestamp is a client bug, not an act of reading we can
        # place on a timeline. Treating it as UTC would invent a fact.
        return "occurred_at must be timezone-aware"

    if occurred > now + _MAX_FUTURE_SKEW:
        return "occurred_at is too far in the future (client clock skew)"
    if occurred < now - _MAX_AGE:
        return "occurred_at is older than the replay bound"

    # Subjects arrive whole or not at all — half a subject is a bug, and the
    # 0189 CHECK would reject the whole INSERT for it.
    if (event.subject_kind is None) != (event.subject_id is None):
        return "subject_kind and subject_id must be set together or both null"
    if event.subject_kind is not None and not event.subject_kind.strip():
        return "subject_kind must not be blank"
    if event.subject_id is not None and not event.subject_id.strip():
        return "subject_id must not be blank"
    if not event.workspace.strip():
        return "workspace must not be blank"
    if not event.session_nonce.strip():
        return "session_nonce must not be blank"
    return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_read_events_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the read-telemetry router (mounted at ``/api/v1``)."""

    router = APIRouter(tags=["read-events"])

    @router.post(
        "/read-events",
        response_model=IngestResult,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def ingest_read_events(
        batch: ReadEventBatch,
        principal: str = Depends(require_bearer),
    ) -> IngestResult:
        """Append a batch of read receipts. Append-only, no joins, no reads."""
        now = datetime.now(timezone.utc)

        rows: list[tuple[Any, ...]] = []
        reasons: list[str] = []
        for event in batch.events:
            reason = validate_event(event, now=now)
            if reason is not None:
                reasons.append(reason)
                logger.warning(
                    "read_events: dropped %s event — %s",
                    event.event_kind,
                    reason,
                )
                continue
            rows.append(
                (
                    event.occurred_at,
                    event.event_kind,
                    event.subject_kind,
                    event.subject_id,
                    event.workspace,
                    event.session_nonce,
                    event.dwell_ms,
                )
            )

        if rows:
            async with deps.descriptor_registry.pg.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO public.read_events (
                        occurred_at, event_kind, subject_kind, subject_id,
                        workspace, session_nonce, dwell_ms
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    rows,
                )

        # De-duplicate reasons so a batch of 200 identically-broken events
        # reports one line rather than a 200-element wall.
        return IngestResult(
            accepted=len(rows),
            rejected=len(reasons),
            reasons=sorted(set(reasons)),
        )

    @router.get("/read-events/rollup", response_model=RollupOut)
    async def read_events_rollup(
        days: int = Query(
            default=_DEFAULT_ROLLUP_DAYS, ge=1, le=_MAX_ROLLUP_DAYS
        ),
        principal: str = Depends(require_bearer),
    ) -> RollupOut:
        """Daily rollup by kind + the wager's headline scalars.

        The window is bounded and the grouping is done in Postgres — the API
        never streams the raw log to a caller, because the scoreboard tile
        refreshes on a timer and a growing table must not turn a dashboard
        into a table scan the operator can feel.
        """
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days - 1)).date()

        async with deps.descriptor_registry.pg.acquire() as conn:
            day_rows = await conn.fetch(
                """
                SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day,
                       event_kind,
                       count(*)                       AS events,
                       count(DISTINCT session_nonce)  AS sessions
                  FROM public.read_events
                 WHERE occurred_at >= $1::date
                 GROUP BY 1, 2
                 ORDER BY 1 DESC, 2 ASC
                """,
                since,
            )
            totals_row = await conn.fetchrow(
                """
                SELECT
                  count(*) FILTER (
                    WHERE (occurred_at AT TIME ZONE 'UTC')::date
                          = (now() AT TIME ZONE 'UTC')::date
                  ) AS reads_today,
                  count(*) FILTER (
                    WHERE occurred_at >= now() - interval '7 days'
                  ) AS reads_this_week,
                  count(*) FILTER (
                    WHERE event_kind = 'brief_read'
                      AND (occurred_at AT TIME ZONE 'UTC')::date
                          = (now() AT TIME ZONE 'UTC')::date
                  ) AS brief_reads_today,
                  count(*) FILTER (
                    WHERE event_kind = 'brief_read'
                      AND occurred_at >= now() - interval '7 days'
                  ) AS brief_reads_this_week,
                  count(DISTINCT (occurred_at AT TIME ZONE 'UTC')::date)
                    FILTER (WHERE event_kind = 'brief_read')
                    AS brief_read_days,
                  count(DISTINCT (occurred_at AT TIME ZONE 'UTC')::date)
                    AS active_days,
                  count(DISTINCT session_nonce) FILTER (
                    WHERE occurred_at >= now() - interval '7 days'
                  ) AS sessions_this_week
                  FROM public.read_events
                 WHERE occurred_at >= $1::date
                """,
                since,
            )

        totals = RollupTotals(
            reads_today=int(totals_row["reads_today"] or 0),
            reads_this_week=int(totals_row["reads_this_week"] or 0),
            brief_reads_today=int(totals_row["brief_reads_today"] or 0),
            brief_reads_this_week=int(totals_row["brief_reads_this_week"] or 0),
            brief_read_days=int(totals_row["brief_read_days"] or 0),
            active_days=int(totals_row["active_days"] or 0),
            sessions_this_week=int(totals_row["sessions_this_week"] or 0),
            window_days=days,
        )

        return RollupOut(
            since=since,
            totals=totals,
            days=[
                RollupDay(
                    day=r["day"],
                    event_kind=r["event_kind"],
                    events=int(r["events"]),
                    sessions=int(r["sessions"]),
                )
                for r in day_rows
            ],
        )

    return router


__all__ = [
    "READ_EVENT_KINDS",
    "IngestResult",
    "ReadEventBatch",
    "ReadEventIn",
    "RollupDay",
    "RollupOut",
    "RollupTotals",
    "build_read_events_router",
    "validate_event",
]
