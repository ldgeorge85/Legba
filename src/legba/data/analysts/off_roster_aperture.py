# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The PURE half of ``off_roster_activity``: render the off-roster countries the
world record persisted, for the declared aperture and its fences.

Split from the read on purpose. ``assessment_prompts`` builds the voice's prompt
from the assembly payload alone and is AST-guarded against anything that can
reach the substrate; this module takes a payload and returns strings, and
imports nothing that reads. See ``off_roster_activity`` for the read, the
threshold and its live justification.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["OFF_ROSTER_PAYLOAD_KEY", "off_roster_lines", "off_roster_names"]

#: The key on the world record's assembly payload, read by the aperture.
OFF_ROSTER_PAYLOAD_KEY: str = "off_roster_activity"


def _countries(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    activity = payload.get(OFF_ROSTER_PAYLOAD_KEY)
    if not isinstance(activity, Mapping):
        return []
    return [c for c in activity.get("countries") or [] if isinstance(c, Mapping)]


def off_roster_names(payload: Mapping[str, Any]) -> list[str]:
    """The named countries, for the aperture fences: each ISO name, its short
    form before a comma ("Palestine, State of" is written "Palestine"), and its
    code."""
    out: list[str] = []
    for c in _countries(payload):
        name = str(c.get("name") or "").strip()
        out.extend([name, name.split(",")[0].strip(), str(c.get("code") or "")])
    return list(dict.fromkeys(n for n in out if n))


def off_roster_lines(payload: Mapping[str, Any]) -> list[str]:
    """The aperture line, or ``[]`` when nothing is over the bar or nothing was
    measured. Phrased as presence and volume only: the record holds counts for
    these countries and no reading of them."""
    countries = _countries(payload)
    if not countries:
        return []
    activity = payload.get(OFF_ROSTER_PAYLOAD_KEY) or {}
    days = activity.get("window_days", 0)
    floor = float(activity.get("mass_floor", 0.0))
    named = "; ".join(
        f"{c.get('name')} ({c.get('code')}): {int(c.get('above_floor') or 0)} of "
        f"{int(c.get('signals') or 0)} signals"
        for c in countries
    )
    return [
        f"  * strong activity OUTSIDE the roster — countries that are not units "
        f"of this record, with {int(activity.get('min_above_floor', 0))}+ "
        f"signals at magnitude {floor:.2f} or more in the last {int(days)} days: "
        f"{named}. You may say these places are active in the substrate and how "
        f"active; this record carries counts for them and no reading, so state "
        f"nothing about what is happening there."
    ]
