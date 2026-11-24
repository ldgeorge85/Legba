# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""STALE-READ MARKING on compositions (W11 SWEEP-2; FRESH_EYES_2026-10-05 §5).

19 of 40 compositions quoted unit reads 34-94 h old on 10-05, and a reader saw
nothing: a world read quoting a 94-hour-old head looked the same as a fresh one.
The production gauge knew (``production_gauge_staleness``); the record did not
say. This module makes the record say it. It changes NO selection rule — which
read a composition quotes is ``read_other_analyst_findings`` /
``composition_window.read_floor_fallback_heads``' business — it only MARKS.

Two halves, one idea:

* :func:`stamp_unit_cadence` (read time, one query) — each consumed unit's own
  cadence (its head descriptor's ``cadence.cooldown_seconds``) rides the rows to
  the DB-less ``_run``, the ``_stamp_horizon`` idiom. A unit with no declared
  cooldown carries no cadence, and nothing about it is called stale or fresh.
* :func:`mark_stale_reads` (compose time, zero queries) — every head in the
  ``data.head_ages`` stamp and every row of the assembly's coverage ledger that
  carries an ``age_h`` gains ``cadence_h`` and ``stale``: true when the read is
  older than ``cadence × STALE_READ_CADENCE_MULTIPLE``. The count is the
  composition's own ``stale_unit_reads``. ``None`` (no key written) when no
  quoted read had a known cadence — unmeasured is never 0.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, MutableMapping, Sequence

logger = logging.getLogger(__name__)

#: THE TUNABLE (docs/TUNABLES.md §3). A quoted unit read older than its unit's
#: cadence times this is marked stale. 3 ⇒ an 11 h unit is stale past 33 h —
#: within an hour of the staleness gauge's own 34 h bar, by construction.
STALE_READ_CADENCE_MULTIPLE_ENV = "LEGBA_STALE_READ_CADENCE_MULTIPLE"
STALE_READ_CADENCE_MULTIPLE_DEFAULT = 3.0

#: Row key the unit's cadence (hours) rides on, read time → compose time.
UNIT_CADENCE_ROW_KEY = "_unit_cadence_h"

_UNIT_CADENCE_SQL = """
    SELECT descriptor_id,
           (body -> 'cadence' ->> 'cooldown_seconds')::bigint AS cooldown_s
      FROM analyst_descriptors
     WHERE is_head AND descriptor_id = ANY($1::text[])
"""


def stale_read_cadence_multiple() -> float:
    """The multiple, from the env; a missing, unparsable or non-positive value
    is the default (a bad knob never disables the mark)."""
    raw = os.getenv(STALE_READ_CADENCE_MULTIPLE_ENV)
    if raw is None or not raw.strip():
        return STALE_READ_CADENCE_MULTIPLE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return STALE_READ_CADENCE_MULTIPLE_DEFAULT
    return value if value > 0 else STALE_READ_CADENCE_MULTIPLE_DEFAULT


async def stamp_unit_cadence(conn: Any, rows: Sequence[MutableMapping[str, Any]]) -> None:
    """Denormalize each row's unit cadence (hours) onto it — one query.

    Best-effort: a failed lookup leaves every row unstamped, which renders as
    "cadence unknown" (no stale mark either way), never as fresh.
    """
    ids = sorted({str(r["analyst_id"]) for r in rows if r.get("analyst_id")})
    if not ids:
        return
    try:
        found = await conn.fetch(_UNIT_CADENCE_SQL, ids)
        hours = {
            str(r["descriptor_id"]): int(r["cooldown_s"]) / 3600.0
            for r in found
            if r["cooldown_s"] is not None and int(r["cooldown_s"]) > 0
        }
    except Exception as exc:  # noqa: BLE001 — marking never fails a compose
        logger.warning("composition.stale_reads.cadence_lookup_failed err=%s", exc)
        return
    for row in rows:
        cadence = hours.get(str(row.get("analyst_id") or ""))
        if cadence is not None:
            row[UNIT_CADENCE_ROW_KEY] = round(cadence, 3)


def _cadence_by_unit(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        cadence = row.get(UNIT_CADENCE_ROW_KEY)
        unit = row.get("analyst_id")
        if unit and isinstance(cadence, (int, float)) and not isinstance(cadence, bool):
            out.setdefault(str(unit), float(cadence))
    return out


def _mark(entry: MutableMapping[str, Any], unit: Any, cadence: Mapping[str, float], k: float) -> bool | None:
    """Mark one entry in place; True/False = stale or not, None = not measured."""
    c = cadence.get(str(unit or ""))
    age = entry.get("age_h")
    if c is None or not isinstance(age, (int, float)) or isinstance(age, bool):
        return None
    entry["cadence_h"] = round(c, 2)
    entry["stale"] = bool(age > c * k)
    return entry["stale"]


def mark_stale_reads(
    data: MutableMapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    multiple: float | None = None,
) -> int | None:
    """Mark the composition's quoted unit reads stale/fresh; stamp the count.

    ``data`` is the finding's ``data`` dict (``head_ages`` and ``assembly``
    already set); ``rows`` the consumed rows carrying :data:`UNIT_CADENCE_ROW_KEY`.
    Writes ``data["stale_unit_reads"]`` (the number of head_ages heads marked
    stale) and returns it; writes nothing and returns ``None`` when no head had
    a known cadence.
    """
    k = stale_read_cadence_multiple() if multiple is None else float(multiple)
    cadence = _cadence_by_unit(rows)
    if not cadence:
        return None
    measured = 0
    stale = 0
    head_ages = data.get("head_ages")
    if isinstance(head_ages, dict):
        for head in head_ages.get("heads") or []:
            if isinstance(head, dict):
                verdict = _mark(head, head.get("analyst_id"), cadence, k)
                if verdict is not None:
                    measured += 1
                    stale += int(verdict)
        if measured:
            head_ages["stale_multiple"] = k
    assembly = data.get("assembly")
    if isinstance(assembly, dict):
        for entry in assembly.get("coverage") or []:
            if isinstance(entry, dict):
                _mark(entry, entry.get("unit"), cadence, k)
    if not measured:
        return None
    data["stale_unit_reads"] = stale
    return stale


__all__ = [
    "STALE_READ_CADENCE_MULTIPLE_DEFAULT",
    "STALE_READ_CADENCE_MULTIPLE_ENV",
    "UNIT_CADENCE_ROW_KEY",
    "mark_stale_reads",
    "stale_read_cadence_multiple",
    "stamp_unit_cadence",
]
