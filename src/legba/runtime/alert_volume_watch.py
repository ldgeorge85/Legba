# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``alert_volume_high`` page — the alert plane itself too loud (X4 ALERTS).

WHY A PAGE. The operator's phone is the loudest surface the platform has, and
until X4 nothing measured how loud: review C counted ≈ 80 pushes a day by hand,
53 % of them saying "no change". A noise regression (a new trigger class, a
cooldown misset, a budget lifted) should page ONCE, on the engine topic, not be
discovered by a reviewer a week later.

THE MEASURE. Pushes per topic over the trailing :data:`CAP_WINDOW_HOURS` by the
ONE rule the volume route reads (:mod:`legba.data.registry.alert_volume` — a
push is a ``delivered`` row of an outward sink; rows written before the topic
split count toward no topic). The page fires when a topic is ABOVE its declared
cap (``LEGBA_ALERT_VOLUME_DAILY_CAP_WORLD`` / ``_ENGINE``, default 50).

THE EDGE. The watchdog's transition-edge ledger idiom (B0-12): ONE durable
``alert_sink_deliveries`` row (``sink_kind='liveness_watchdog'``, channel
``alert_volume_high``, ``sink_target`` = the topic) plus the outward fan-out
(engine topic) when a topic ENTERS the state; one ``recovered`` row when it
falls back to the cap or under; silence in between; restart-safe through the
ledger seed. Checked at most every :data:`MIN_INTERVAL_S`.

Fail-safe: a read failure is no verdict this pass, never a crash.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..data.alerts.topics import TOPIC_ENGINE, TOPICS
from ..data.registry.alert_volume import (
    CAP_ENV_BY_TOPIC,
    CAP_WINDOW_HOURS,
    OUTWARD_SINK_KINDS,
    VOLUME_WINDOW_SQL,
    daily_cap,
    summarize,
)

logger = logging.getLogger(__name__)

ALERT_CHANNEL_ALERT_VOLUME = "alert_volume_high"

#: The watchdog loop ticks every ~60 s; a 24 h count moves slowly.
MIN_INTERVAL_S = 300.0

#: id(watchdog) → monotonic time of the last pass (one watchdog per process).
_LAST_PASS: dict[int, float] = {}


def evaluate(pushed: dict[str, int], caps: dict[str, int]) -> list[str]:
    """The declared topics whose pushes are ABOVE their cap. Pure."""
    return [t for t in TOPICS if int(pushed.get(t, 0)) > int(caps[t])]


def alert_text(
    topic: str, pushed: int, cap: int, kinds: list[dict[str, Any]],
) -> tuple[str, str]:
    top = sorted(
        (k for k in kinds if k.get("topic") == topic and int(k.get("pushed") or 0)),
        key=lambda k: -int(k["pushed"]),
    )[:5]
    loudest = "; ".join(f"{k['kind']} {k['pushed']}" for k in top) or "no kind recorded"
    title = (
        f"Alert volume high: {pushed} pushes on '{topic}' in {CAP_WINDOW_HOURS} h "
        f"(cap {cap})"
    )
    body = (
        f"The '{topic}' topic sent {pushed} pushes in the last {CAP_WINDOW_HOURS} h, "
        f"above its declared cap of {cap} ({CAP_ENV_BY_TOPIC[topic]}). Loudest "
        f"kinds: {loudest}. Read /api/v1/v3/system/alerts/volume?days=1 for the "
        "per-kind table; the knobs are docs/TUNABLES.md (the daily page "
        "budget, the per-kind cap, the band batch, the level-only rule)."
    )
    return title, body


async def check_alert_volume_once(
    wd: Any, *, now: Optional[datetime] = None, force: bool = False,
) -> list[str]:
    """One pass. Returns the topics whose ENTRY edge fired this pass.

    Leader-gated like its siblings; rate-limited to :data:`MIN_INTERVAL_S`
    unless ``force``. Never raises.
    """
    try:
        if wd._pg is None:
            return []
        if wd._is_leader is not None and not wd._is_leader():
            return []
        mono = time.monotonic()
        last = _LAST_PASS.get(id(wd))
        if not force and last is not None and (mono - last) < MIN_INTERVAL_S:
            return []
        _LAST_PASS[id(wd)] = mono
        now = now or datetime.now(timezone.utc)
        async with wd._pg.acquire() as conn:
            rows = await conn.fetch(
                VOLUME_WINDOW_SQL,
                now - timedelta(hours=CAP_WINDOW_HOURS),
                list(OUTWARD_SINK_KINDS),
            )
        summary = summarize([dict(r) for r in rows])
        pushed = {t["topic"]: int(t["pushed"]) for t in summary["topics"]}
        caps = {t: daily_cap(t) for t in TOPICS}
        over = evaluate(pushed, caps)
        states = await wd._get_alert_states(ALERT_CHANNEL_ALERT_VOLUME)
        fired: list[str] = []
        for topic in over:
            if states.get(topic) == "entered":
                continue  # ongoing — the entry edge already fired
            title, body = alert_text(topic, pushed[topic], caps[topic], summary["kinds"])
            logger.error(
                "liveness_watchdog.alert_volume_high topic=%s pushed=%d cap=%d",
                topic, pushed[topic], caps[topic],
            )
            await wd._record_transition(
                channel=ALERT_CHANNEL_ALERT_VOLUME, entity_id=topic,
                state="entered", severity="high", kind="alert_volume_high",
                title=title, body=body,
                extra={
                    "topic": topic,
                    "pushed": pushed[topic],
                    "cap": caps[topic],
                    "cap_env": CAP_ENV_BY_TOPIC[topic],
                    "window_hours": CAP_WINDOW_HOURS,
                    "as_of": summary["as_of"],
                    "page_topic": TOPIC_ENGINE,
                    "page_budget_exempt": True,
                },
            )
            fired.append(topic)
        await wd._emit_recoveries(
            channel=ALERT_CHANNEL_ALERT_VOLUME, kind="alert_volume_recovered",
            noun="Alert topic", still_bad=set(over),
        )
        return fired
    except Exception as exc:  # noqa: BLE001 — never kill the watchdog loop
        logger.warning("liveness_watchdog.alert_volume_check_error err=%s", exc)
        return []


__all__ = [
    "ALERT_CHANNEL_ALERT_VOLUME",
    "MIN_INTERVAL_S",
    "alert_text",
    "check_alert_volume_once",
    "evaluate",
]
