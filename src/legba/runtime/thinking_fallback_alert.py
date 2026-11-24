# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T-2 — raise the ``thinking_fallback`` alert when a thinking call falls back.

The provider plane (``data/stack/llm/thinking.py``) retries a thinking call
ONCE on the plain alias when the thinking alias itself caused the failure, and
tells every registered fallback sink. This module is the runtime's sink: it
raises one alert through the ONE alert path
(:func:`legba.data.alerts.raise_alert.raise_alert`), kind
``thinking_fallback`` — in the closed list, topic ``engine``, and named by NO
page-rule entry, so it is logged and shown on the Watch data and never pushed
unless the owner adds a rule line.

One episode per (component, reason): every further fallback re-raises the open
episode (its occurrence count is the rate), and the log closes it after a quiet
day.

The provider call runs on whatever event loop the analyst runs on; the
Postgres pool belongs to the host's loop. The sink therefore hands the raise to
the pool's loop (``run_coroutine_threadsafe``) and returns at once: it never
blocks a call and never raises into one. A leaf: ``dapr_host`` keeps one call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Mapping

from ..data.stack.llm.thinking import add_thinking_fallback_sink, remove_thinking_fallback_sink

logger = logging.getLogger(__name__)

ALERT_KIND = "thinking_fallback"
RAISED_BY = "runtime.thinking_fallback"


def alert_fields(event: Mapping[str, Any]) -> dict[str, Any]:
    """The ``raise_alert`` keyword arguments for one fallback event."""
    component = str(event.get("component_id") or "unknown")
    reason = str(event.get("reason") or "unknown")
    status = event.get("http_status")
    return {
        "kind": ALERT_KIND,
        "severity": "low",
        "subject_id": component,
        "summary": f"Thinking call on {component} fell back to the plain alias ({reason})",
        "detail": (
            f"A thinking call on {component} failed with {event.get('error')}"
            f"{f' (HTTP {status})' if status else ''}, a failure the thinking alias "
            f"causes ({reason}); it was retried once on the plain alias. The retry's "
            "receipt in analyst_traces.llm_calls carries thinking_fallback."
        ),
        "dedupe_key": f"{ALERT_KIND}:{component}:{reason}",
        "raised_by": RAISED_BY,
    }


def install_thinking_fallback_alerts(
    pool: Any, *, loop: asyncio.AbstractEventLoop | None = None, raise_fn: Any = None,
) -> Callable[[Mapping[str, Any]], None]:
    """Register the alert sink; returns it (``uninstall`` takes it back).

    Must be called on the loop that owns ``pool`` (the host's startup loop).
    """
    owner = loop or asyncio.get_running_loop()
    if raise_fn is None:
        from ..data.alerts.raise_alert import raise_alert as raise_fn

    def _sink(event: Mapping[str, Any]) -> None:
        try:
            coro = raise_fn(pool, **alert_fields(event))
            asyncio.run_coroutine_threadsafe(coro, owner)
        except Exception:  # noqa: BLE001 — never into the provider call
            logger.warning("thinking_fallback.alert_failed", exc_info=True)

    add_thinking_fallback_sink(_sink)
    return _sink


def uninstall(sink: Callable[[Mapping[str, Any]], None]) -> None:
    remove_thinking_fallback_sink(sink)


__all__ = [
    "ALERT_KIND",
    "RAISED_BY",
    "alert_fields",
    "install_thinking_fallback_alerts",
    "uninstall",
]
