# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two outward alert TOPICS — ``world`` and ``engine`` (X4 ALERTS, W11-F17).

WHY. Until X4 every outward alert went to ONE ntfy topic: the world's
escalations, a desk's band move, and "a source has been silent for four hours"
all landed on the same phone channel with the same sound. An operator cannot
mute the engine's housekeeping without muting the world, so the loudest
surface the platform has carried both at once.

THE SPLIT.

* ``world`` — what the platform says about its subject: escalation transitions,
  verified high-severity findings, band moves (batched, one push per tick),
  situation escalations, operator watch hits, desk baseline deviations.
* ``engine`` — what the platform says about itself: source / search / cadence /
  judge health, production deficits, the coverage floor, the external audit,
  the alert-volume page. The host disk floor, the LLM heartbeat and the search
  canary post here too (host scripts, outside this table).

Every alert KIND the code emits is named in :data:`TOPIC_BY_KIND` (derived from
the kind declarations, R-1); a guard test
(``tests/data_pkg/alerts/test_alert_topics_x4.py``) enumerates the trigger
classes and the watchdog channels and fails when one is missing, so a new class
cannot silently land on the default.

TOPIC NAMES are env (``docs/TUNABLES.md`` §5): ``LEGBA_ALERT_NTFY_TOPIC_WORLD``
(default ``world``) and ``LEGBA_ALERT_NTFY_TOPIC_ENGINE`` (default ``engine``).
The ntfy SERVER is the one ``LEGBA_ALERT_NTFY_URL`` already names: its last path
segment (the pre-split topic) is dropped and the topic name appended, so an
existing ``http://ntfy/legba-alerts`` publishes to ``http://ntfy/world`` and
``http://ntfy/engine`` with no ``.env`` edit. ``LEGBA_ALERT_NTFY_TOPIC_SPLIT=0``
restores the single pre-split topic (the reversal switch).

Stdlib only: imported by the registry's volume route (registry-slim).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from ..type_registry import default_registry as _default_registry

logger = logging.getLogger(__name__)

TOPIC_WORLD = "world"
TOPIC_ENGINE = "engine"
#: L-1: the nightly suite's own topic (it posted there from the host before the
#: single alert path; it keeps its topic so the owner's subscriptions hold).
TOPIC_NIGHTLY = "nightly"
TOPICS: tuple[str, ...] = (TOPIC_WORLD, TOPIC_ENGINE, TOPIC_NIGHTLY)

#: The label the volume route gives ledger rows written before the split (they
#: carry no topic: every one of them went to the single pre-split topic).
TOPIC_PRE_SPLIT = "pre_split"

ENV_TOPIC_WORLD = "LEGBA_ALERT_NTFY_TOPIC_WORLD"
ENV_TOPIC_ENGINE = "LEGBA_ALERT_NTFY_TOPIC_ENGINE"
ENV_TOPIC_SPLIT = "LEGBA_ALERT_NTFY_TOPIC_SPLIT"
ENV_TOPIC_NIGHTLY = "LEGBA_ALERT_NTFY_TOPIC_NIGHTLY"

_ENV_BY_TOPIC = {
    TOPIC_WORLD: ENV_TOPIC_WORLD,
    TOPIC_ENGINE: ENV_TOPIC_ENGINE,
    TOPIC_NIGHTLY: ENV_TOPIC_NIGHTLY,
}

#: ntfy's own topic alphabet (1-64 of letters, digits, ``_`` and ``-``).
_TOPIC_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Every alert kind → its topic. A KIND is the trigger class for the trigger
#: scan's alerts, the channel name for everything else. DERIVED (R-1) from the
#: ``topic`` each ``alert_kind`` declaration carries
#: (``legba.data.type_registry.alert_kinds``).
def build_topic_by_kind(registry: Any = None) -> dict[str, str]:
    """``{kind: topic}`` from the declarations."""
    reg = registry or _default_registry()
    return {d.id: d.attr("topic") for d in reg.types("alert_kind")}


TOPIC_BY_KIND: Mapping[str, str] = build_topic_by_kind()


def topic_for(kind: str | None, *, default: str = TOPIC_WORLD) -> str:
    """The topic for one alert kind; ``default`` for a kind the table lacks.

    The default is the caller's: the escalation edge and the trigger scan pass
    ``world`` (a statement about the subject), deterministic runtime alerts
    pass ``engine`` (a statement about the engine).
    """
    return TOPIC_BY_KIND.get(str(kind or "").strip(), default)


def topic_for_rollup(member_kinds: Iterable[str]) -> str:
    """A per-desk rollup goes to ``world`` when ANY member does, else ``engine``.

    A rollup that folded one world alert must never be heard only by whoever
    listens to the engine.
    """
    kinds = [str(k) for k in member_kinds]
    if not kinds:
        return TOPIC_WORLD
    return (
        TOPIC_WORLD
        if any(topic_for(k) == TOPIC_WORLD for k in kinds)
        else TOPIC_ENGINE
    )


def split_enabled() -> bool:
    """``LEGBA_ALERT_NTFY_TOPIC_SPLIT`` — on unless set to 0/false/off/no."""
    raw = (os.environ.get(ENV_TOPIC_SPLIT) or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def topic_name(topic: str) -> str:
    """The ntfy topic NAME for a declared topic (env override, validated)."""
    env = _ENV_BY_TOPIC.get(topic)
    raw = (os.environ.get(env) or "").strip() if env else ""
    if raw and _TOPIC_NAME.match(raw):
        return raw
    if raw:
        logger.warning(
            "alert_topics.bad_topic_name env=%s — not an ntfy topic name "
            "(1-64 of A-Z a-z 0-9 _ -); using %r", env, topic,
        )
    return topic


def server_root(topic_url: str) -> str:
    """The ntfy server root of a full topic URL (its last path segment dropped).

    ``http://ntfy/legba-alerts`` → ``http://ntfy``;
    ``https://h/ntfy/legba-alerts`` → ``https://h/ntfy``. A URL with no path is
    already a root.
    """
    url = (topic_url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    root_path = path.rsplit("/", 1)[0] if "/" in path else ""
    return urlunsplit((parts.scheme, parts.netloc, root_path, "", "")).rstrip("/")


def topic_url(topic_url_env: str, topic: str) -> str:
    """Where a push for ``topic`` goes, given ``LEGBA_ALERT_NTFY_URL``.

    Split off: the configured URL unchanged (the single pre-split topic).
    """
    if not topic_url_env:
        return ""
    if not split_enabled():
        return topic_url_env.strip()
    return f"{server_root(topic_url_env)}/{topic_name(topic)}"


__all__ = [
    "ENV_TOPIC_ENGINE",
    "ENV_TOPIC_NIGHTLY",
    "ENV_TOPIC_SPLIT",
    "ENV_TOPIC_WORLD",
    "TOPICS",
    "TOPIC_BY_KIND",
    "TOPIC_ENGINE",
    "TOPIC_NIGHTLY",
    "TOPIC_PRE_SPLIT",
    "TOPIC_WORLD",
    "build_topic_by_kind",
    "server_root",
    "split_enabled",
    "topic_for",
    "topic_for_rollup",
    "topic_name",
    "topic_url",
]
