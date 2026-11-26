# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""STRONG ACTIVITY OUTSIDE THE ROSTER — named in the world read's aperture.

THE GAP. The world read
can name only roster units, so a country with no unit is invisible to it however
loud its week was. On 2026-11-07 Paraguay had 75 signals (an impeachment, an
arrest, a new president sworn in; 61 above the mass floor) and the voice was
correctly forbidden from mentioning it: no unit, no name in the record, so any
sentence about it was an ``aperture_guess``.

WHAT THIS ADDS, AND WHAT IT DOES NOT. One read at world-slice time, where a
connection legally exists: per ISO country, the 7-day count of canonical signals
geo-tagged to it and the count at or above the salience mass floor, for
countries that are in NO subject-roster unit's ``scope.geo``. Up to
:data:`OFF_ROSTER_MAX_NAMED` countries whose above-floor count reaches
:data:`OFF_ROSTER_MIN_ABOVE_FLOOR` are stamped onto the slice, persisted on the
world record's assembly payload, and rendered as ONE line of the declared
aperture (``off_roster_aperture``, the pure half: the Assessment prompt module
may import nothing that can read the substrate, so the renderer lives apart from
this read). The voice may then say they exist and how loud the substrate is about
them. Nothing is analysed and no unit is created; the line says so.

THE THRESHOLD, FROM LIVE COUNTS (read-only, 2026-11-07 ~17Z, 7 days, canonical
signals, magnitude >= 0.50). Off-roster: Yemen 288, Palestine 87, Oman 58,
Paraguay 56, Lebanon 52, Poland 30, Morocco 24, Syria 22, United Arab Emirates
20. Yemen, Oman and Poland sit in supply-chain lane scopes (Red Sea, Hormuz,
Baltic), so they have a unit and are not named. Roster country desks run from
558 (US) down to under 8 (nine desks), median about 20. 50 sits in the widest
gap of the uncovered tail (Lebanon 52, then Morocco 24) and above 24 of the 32
country desks: a country over it is louder this week than most countries the
record does analyse. Today's line names Palestine, Paraguay and Lebanon.

PLATFORM, NOT EXEMPLAR. The roster is the profile's subject-roster tags
(``rosters.subject_roster_tags()``), the floor is ``signal_salience.MASS_FLOOR``,
the names are ``iso_countries``'. Nothing here names a country.

FAIL-SOFT, SAID ONCE. The read is an enrichment of the aperture: a failure logs
``off_roster_activity.read_failed`` and the world read composes without the line
(absent, which the aperture renders as nothing, never as "none found").
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from .. import rosters as _rosters
from .off_roster_aperture import OFF_ROSTER_PAYLOAD_KEY
from .signal_salience import MASS_FLOOR

logger = logging.getLogger(__name__)

__all__ = [
    "OFF_ROSTER_MAX_NAMED",
    "OFF_ROSTER_MIN_ABOVE_FLOOR",
    "OFF_ROSTER_PAYLOAD_KEY",
    "OFF_ROSTER_ROW_KEY",
    "OFF_ROSTER_WINDOW_DAYS",
    "off_roster_of",
    "read_off_roster_activity",
    "stamp_off_roster",
    "with_off_roster",
]

#: The trailing window, in days.
OFF_ROSTER_WINDOW_DAYS: int = 7

#: The bar: canonical signals at or above ``MASS_FLOOR`` in the window. See the
#: module banner for the live distribution it was read from.
OFF_ROSTER_MIN_ABOVE_FLOOR: int = 50

#: At most this many countries are named, loudest first.
OFF_ROSTER_MAX_NAMED: int = 3

#: The slice-row stamp (the ``_world_roster`` idiom: a DB-less ``_run`` builds
#: the payload and must carry a fact it cannot recover from the rows).
OFF_ROSTER_ROW_KEY: str = "_off_roster_activity"


# $1 subject-roster tags, $2 window days, $3 mass floor, $4 bar, $5 cap.
# ``iso_countries`` both names the code and drops anything that is not one.
_OFF_ROSTER_SQL = """
    WITH covered AS (
        SELECT DISTINCT jsonb_array_elements_text(t.body -> 'scope' -> 'geo') AS code
          FROM target_descriptors t
         WHERE t.is_head = TRUE
           AND COALESCE(t.state, 'active') <> 'retired'
           AND (t.body -> 'scope' -> 'tags') ?| $1::text[]
           AND jsonb_typeof(t.body -> 'scope' -> 'geo') = 'array'
    ), counts AS (
        SELECT g.code,
               count(*) AS signals,
               count(*) FILTER (
                   WHERE jsonb_typeof(s.salience -> 'magnitude') = 'number'
                     AND (s.salience ->> 'magnitude')::real >= $3
               ) AS above_floor
          FROM signals s
         CROSS JOIN LATERAL unnest(s.geo) AS g(code)
         WHERE s.fetched_at > NOW() - make_interval(days => $2)
           AND s.canonical_signal_id IS NULL
         GROUP BY g.code
    )
    SELECT c.code, i.name, c.signals, c.above_floor
      FROM counts c
      JOIN iso_countries i ON i.iso2 = c.code
     WHERE NOT EXISTS (SELECT 1 FROM covered v WHERE v.code = c.code)
       AND c.above_floor >= $4
     ORDER BY c.above_floor DESC, c.signals DESC, c.code
     LIMIT $5
"""


async def read_off_roster_activity(conn: Any) -> dict[str, Any] | None:
    """The off-roster countries over the bar, or ``None`` when the read failed.

    An empty ``countries`` list is a real answer (nothing over the bar this
    week) and is kept distinct from ``None`` (not measured).
    """
    try:
        rows = await conn.fetch(
            _OFF_ROSTER_SQL,
            list(_rosters.subject_roster_tags()),
            OFF_ROSTER_WINDOW_DAYS,
            float(MASS_FLOOR),
            OFF_ROSTER_MIN_ABOVE_FLOOR,
            OFF_ROSTER_MAX_NAMED,
        )
        countries = [
            {
                "code": str(r["code"]),
                "name": str(r["name"]),
                "signals": int(r["signals"]),
                "above_floor": int(r["above_floor"]),
            }
            for r in rows
        ]
    except Exception as exc:  # an enrichment; the world read composes without it
        logger.warning("off_roster_activity.read_failed err=%s", exc)
        return None
    return {
        "window_days": OFF_ROSTER_WINDOW_DAYS,
        "mass_floor": float(MASS_FLOOR),
        "min_above_floor": OFF_ROSTER_MIN_ABOVE_FLOOR,
        "countries": countries,
    }


def stamp_off_roster(
    rows: Sequence[dict[str, Any]], activity: Mapping[str, Any] | None
) -> None:
    """Denormalise the read onto every slice row (in place); ``None`` stamps nothing."""
    if activity is None:
        return
    for row in rows:
        row[OFF_ROSTER_ROW_KEY] = dict(activity)


def off_roster_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The stamp off the first row that carries it, or ``None``."""
    for row in rows:
        value = row.get(OFF_ROSTER_ROW_KEY)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def with_off_roster(
    payload: dict[str, Any], rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The payload with the read persisted beside the roster it is outside of.
    No stamp (every tier but the world, a failed read) ⇒ the payload unchanged."""
    activity = off_roster_of(rows)
    if activity is not None:
        payload[OFF_ROSTER_PAYLOAD_KEY] = activity
    return payload
