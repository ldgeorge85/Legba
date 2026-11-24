# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/alerts/volume?days=7`` — how many pushes the alert plane
sent, per topic (``world`` / ``engine``) and per kind (X4 ALERTS, W11-F17).

Before X4 the only way to know the operator's phone took ≈ 80 pushes a day was
to count ledger rows by hand. This route is the count, by the ONE rule the
watchdog's ``alert_volume_high`` page also reads
(:mod:`.alert_volume` — what a push is, what each column means, why the
internal rows are not counted).

HONESTY. A failed read is ``measured: false`` with the reason, at HTTP 200 (a
polling surface never 500s). ``as_of`` is the newest ledger row read, not the
route's clock. ``caps`` are the DECLARED per-topic 24 h caps the watchdog pages
above; ``per_day`` is a mean over the whole window, zero-push days included.
Rows written before the topic split report under ``pre_split`` — no topic is
invented for them.

Declared on the v3 router (``register_alert_volume_routes``) — the
``optimizer_review_api`` precedent — so no mount changes. Registry-slim:
stdlib + fastapi + pydantic + ``.api`` + ``.alert_volume``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .alert_volume import (
    CAP_ENV_BY_TOPIC,
    CAP_WINDOW_HOURS,
    OUTWARD_SINK_KINDS,
    VOLUME_WINDOW_SQL,
    caps,
    summarize,
)
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

ALERT_VOLUME_ROUTE_VERSION = "2026-10/x4-alerts"
_ROUTE = "/system/alerts/volume"
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 30

_BASIS = (
    "alert_sink_deliveries rows of the outward sinks (ntfy, webhook) in the "
    "window; a push is a 'delivered' row. Internal rows (the escalate pack's "
    "bus publish, the watchdog's transition record) are not pushes and are not "
    "counted. A batched push counts once; alerts_carried counts the alert rows "
    "it carried."
)


class _Tally(BaseModel):
    pushed: int = 0
    coalesced: int = 0
    level_only: int = 0
    failed: int = 0
    unconfigured: int = 0
    other: int = 0
    alerts_carried: int = 0


class AlertVolumeTopic(_Tally):
    topic: str
    #: pushes ÷ window days (a mean over the whole window).
    per_day: Optional[float] = None


class AlertVolumeKind(_Tally):
    topic: str
    kind: str


class AlertVolumeDay(_Tally):
    day: str
    topic: str
    kind: str


class AlertVolumeOut(BaseModel):
    version: str = ALERT_VOLUME_ROUTE_VERSION
    measured: bool
    not_measured_reason: Optional[str] = None
    window_days: int
    read_at: str
    as_of: Optional[str] = None
    basis: str = _BASIS
    sink_kinds: list[str] = Field(default_factory=lambda: list(OUTWARD_SINK_KINDS))
    #: The declared per-topic cap (pushes in any trailing 24 h) and its env.
    caps: dict[str, int] = Field(default_factory=dict)
    cap_env: dict[str, str] = Field(default_factory=lambda: dict(CAP_ENV_BY_TOPIC))
    cap_window_hours: int = CAP_WINDOW_HOURS
    topics: list[AlertVolumeTopic] = Field(default_factory=list)
    kinds: list[AlertVolumeKind] = Field(default_factory=list)
    days: list[AlertVolumeDay] = Field(default_factory=list)


async def read_alert_volume(
    conn, *, days: int, now: Optional[datetime] = None,
) -> AlertVolumeOut:
    """The route's whole answer off an open connection (also the test seam)."""
    now = now or datetime.now(timezone.utc)
    try:
        rows = await conn.fetch(
            VOLUME_WINDOW_SQL, now - timedelta(days=days), list(OUTWARD_SINK_KINDS)
        )
    except Exception as exc:  # noqa: BLE001 — a polling surface never 500s
        logger.info("v3.system.alert_volume.unavailable err=%s", exc)
        return AlertVolumeOut(
            measured=False,
            not_measured_reason=(
                f"alert_sink_deliveries could not be read ({type(exc).__name__})"
            ),
            window_days=days, read_at=now.isoformat(), caps=caps(),
        )
    summary = summarize([dict(r) for r in rows], window_days=days)
    return AlertVolumeOut(
        measured=True,
        window_days=days,
        read_at=now.isoformat(),
        as_of=summary["as_of"],
        caps=caps(),
        topics=[AlertVolumeTopic(**t) for t in summary["topics"]],
        kinds=[AlertVolumeKind(**k) for k in summary["kinds"]],
        days=[AlertVolumeDay(**d) for d in summary["days"]],
    )


def register_alert_volume_routes(router: APIRouter, deps: RegistryAPIDeps) -> None:
    """Declare the volume route on ``router`` (the v3 router)."""

    @router.get(_ROUTE, response_model=AlertVolumeOut)
    async def system_alert_volume(
        days: int = Query(
            default=DEFAULT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS,
            description="Trailing window in days.",
        ),
        _principal: str = Depends(require_bearer),
    ) -> AlertVolumeOut:
        try:
            async with deps.descriptor_registry.pg.acquire() as conn:
                return await read_alert_volume(conn, days=days)
        except Exception as exc:  # noqa: BLE001 — a polling surface never 500s
            logger.info("v3.system.alert_volume.unavailable err=%s", exc)
            return AlertVolumeOut(
                measured=False,
                not_measured_reason=f"no connection ({type(exc).__name__})",
                window_days=days,
                read_at=datetime.now(timezone.utc).isoformat(),
                caps=caps(),
            )


__all__ = [
    "ALERT_VOLUME_ROUTE_VERSION",
    "AlertVolumeOut",
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "read_alert_volume",
    "register_alert_volume_routes",
]
