# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE alert severity ladder — one module, imported everywhere an alert is
raised, recorded, decided or delivered (L-1, single alert path).

======== ==== ===========================================================
level    rank meaning
======== ==== ===========================================================
info     0    a record: a recovery, a held raise, a housekeeping fact
low      1    worth keeping; a person reads it in the log, not on a phone
medium   2    worth a look today
high     3    worth acting on now
critical 4    the platform or the world is in a state that needs a person
======== ==== ===========================================================

The analytic units speak a seven-word ladder of their own (``moderate`` and
``elevated`` between medium and high). Those two words map EXPLICITLY at the
raise boundary (:data:`SEVERITY_ALIASES`) — they are never folded to ``info``
(the fold that left 371 escalations silent in 30 days). Any other word off the
ladder takes :data:`UNKNOWN_SEVERITY_DEFAULT` and logs
``alert_sinks.severity_unknown``: an alerting path fails toward paging.

Stdlib only: the registry's alert-log route imports it in the slim image.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger("legba.data.alerts.sinks")

Severity = Literal["info", "low", "medium", "high", "critical"]

SEVERITY_ORDER: dict[str, int] = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

#: The ladder in rank order (a tuple, so it can be iterated and indexed).
SEVERITIES: tuple[str, ...] = tuple(sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.get))

#: The units' words that sit between the ladder's rungs (``agency/binding.py``
#: ``_SEVERITY_RANK``: moderate = medium, elevated between medium and high).
SEVERITY_ALIASES: dict[str, str] = {
    "moderate": "medium",
    "elevated": "high",
}

#: Where an unrecognised severity word lands on an alert path.
UNKNOWN_SEVERITY_DEFAULT = "medium"


def normalise_severity(raw: Any, *, default: str = "info") -> str:
    """Coerce an arbitrary severity value onto the ladder (defensive, silent)."""
    value = str(raw or "").strip().lower()
    value = SEVERITY_ALIASES.get(value, value)
    return value if value in SEVERITY_ORDER else default


def map_alert_severity(
    raw: Any, *, default: str = UNKNOWN_SEVERITY_DEFAULT, context: str = ""
) -> str:
    """:func:`normalise_severity` for an alert edge: an unknown or missing
    severity takes ``default`` and logs ``alert_sinks.severity_unknown``."""
    value = str(raw or "").strip().lower()
    mapped = SEVERITY_ALIASES.get(value, value)
    if mapped in SEVERITY_ORDER:
        return mapped
    logger.warning(
        "alert_sinks.severity_unknown raw=%r context=%s -> %s",
        raw, context or "-", default,
    )
    return default


def severity_rank(severity: Any) -> int:
    """The rank of a ladder word (an off-ladder word ranks as ``info``)."""
    return SEVERITY_ORDER.get(normalise_severity(severity), 0)


__all__ = [
    "SEVERITIES",
    "SEVERITY_ALIASES",
    "SEVERITY_ORDER",
    "Severity",
    "UNKNOWN_SEVERITY_DEFAULT",
    "map_alert_severity",
    "normalise_severity",
    "severity_rank",
]
