# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scheduled-events calendar — the ONE store for ``scheduled_events``
(migration 0240, W11-F141).

Two readers share this module and no other code touches the table:

  * the consult tool ``list_scheduled_events`` — the runtime port
    (``runtime/substrate_query_port.py``) parses the planner's ISO args into
    datetimes and calls :func:`list_events`; and
  * the loader ``scripts/load_scheduled_events.py`` — parses
    ``seeds/scheduled_events.yaml`` with :func:`parse_events_yaml` and writes
    through :func:`insert_event` (``provenance='seed'``).

The window predicate lives ONCE, in Python (:func:`overlaps_window` /
:func:`select_events`), not in the SELECT: the table is an operator-curated
calendar (hundreds of rows at most), and one implementation of "in the
window" is what a fake store can exercise — an event overlaps the window
when it starts before ``window_end`` and has not ended before
``window_start``; a point event (``ends_at`` NULL) is in the window when its
``starts_at`` is. Retired rows are never events.

Honesty contract (the lane's defect): an empty answer is always covered —
``coverage.total_rows`` counts every live row the table holds so "no events
held for this window" and "the calendar is empty" stay two different facts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import yaml

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIDENCE_LEVELS",
    "DEFAULT_YAML",
    "INSERT_EVENT_SQL",
    "LIST_EVENTS_SQL",
    "LoadResult",
    "insert_event",
    "list_events",
    "overlaps_window",
    "parse_events_yaml",
    "select_events",
]

#: The ``confidence`` CHECK vocabulary (migration 0240).
CONFIDENCE_LEVELS: tuple[str, ...] = ("confirmed", "reported", "tentative")

#: Default input for the loader — gitignored per ``seeds/*.yaml``; the tracked
#: schema doc is ``seeds/scheduled_events.example.yaml`` (FICTIONAL rows only).
#: This file is …/src/legba/data/scheduled_events.py → 3 parents up = repo root.
DEFAULT_YAML = Path(__file__).resolve().parents[3] / "seeds" / "scheduled_events.yaml"

#: The insert. ``(title, starts_at)`` is the loader's idempotence key — a
#: re-run of the same file is a no-op, never a duplicate row.
#: Who writes the rows (lane 9): this loader, named once. The row's own
#: ``provenance`` says where the entry came from; this says what wrote it.
WRITTEN_BY = "rule:scheduled_events_loader"

INSERT_EVENT_SQL = f"""
INSERT INTO scheduled_events (
    id, title, starts_at, ends_at, scope, subject_keys, source_url,
    confidence, provenance, written_by
) VALUES ($1, $2, $3, $4, $5, $6::text[], $7, $8, $9, legba_principal('{WRITTEN_BY}'))
ON CONFLICT (title, starts_at) DO NOTHING
RETURNING id
"""

#: The read. Every row is fetched — the table is a curated calendar —
#: and the retired exclusion, the window and the filters all live in
#: :func:`select_events`, keeping ONE implementation of "in the window"
#: that a fake store exercises end-to-end.
LIST_EVENTS_SQL = """
SELECT id, title, starts_at, ends_at, scope, subject_keys, source_url,
       confidence, provenance, created_at, retired_at
  FROM scheduled_events
 ORDER BY starts_at, id
"""


def _iso(value: Any, *, name: str) -> datetime:
    """An aware UTC instant. Naive input is read as UTC; a ``date`` is
    midnight UTC; anything unparseable refuses rather than guessing."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            try:
                dt = datetime.fromisoformat(raw.upper().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"{name}: cannot parse {value!r} as ISO-8601"
                ) from exc
    else:
        raise ValueError(f"{name}: expected an ISO-8601 instant, got {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _isostr(value: Any) -> str | None:
    """ISO-8601 for anything carrying one, else None (never a bare str() —
    a date that cannot be placed stays absent)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).isoformat()
    return None


def overlaps_window(
    starts_at: datetime,
    ends_at: datetime | None,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """Does the event overlap the half-open window ``[start, end)``?

    A point event (``ends_at`` NULL) is in the window when its ``starts_at``
    is; a ranged event overlaps when it starts before the window ends and
    ends after the window starts — a summit that began yesterday and closes
    tomorrow IS in tomorrow's consult window.
    """
    if starts_at >= window_end:
        return False
    if ends_at is None:
        return starts_at >= window_start
    return ends_at > window_start


def event_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """One stored row → the wire dict (timestamps ISO, ids str)."""
    return {
        "id": str(row.get("id")),
        "title": row.get("title"),
        "starts_at": _isostr(row.get("starts_at")),
        "ends_at": _isostr(row.get("ends_at")),
        "scope": row.get("scope") or "global",
        "subject_keys": [str(k) for k in (row.get("subject_keys") or [])],
        "source_url": row.get("source_url"),
        "confidence": row.get("confidence") or "confirmed",
        "provenance": row.get("provenance"),
        "created_at": _isostr(row.get("created_at")),
        "retired_at": _isostr(row.get("retired_at")),
    }


def _row_instant(row: Mapping[str, Any], key: str) -> datetime | None:
    """The row's ``key`` as a datetime, tolerating wire dicts (ISO strings)
    and store rows (datetimes) alike. Unparseable → ``None``."""
    raw = row.get(key)
    if raw is None:
        return None
    try:
        return _iso(raw, name=key)
    except ValueError:
        return None


def select_events(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_start: Any,
    window_end: Any,
    scope: str | None = None,
    subject_keys: Sequence[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The live rows overlapping ``[window_start, window_end)``, sorted by
    ``starts_at`` then id and capped at ``limit``.

    The ONE place "in the window" is defined — a retired row is never an
    event, ``scope`` is an exact match on the row's scope tag, and
    ``subject_keys`` matches on ANY shared key. Rows whose ``starts_at``
    cannot be parsed are dropped: an event with no usable start cannot be
    placed on a calendar.
    """
    w_start = _iso(window_start, name="window_start")
    w_end = _iso(window_end, name="window_end")
    wanted_keys = {str(k) for k in (subject_keys or []) if str(k).strip()}
    out: list[tuple[datetime, str, dict[str, Any]]] = []
    for row in rows:
        if row.get("retired_at") is not None:
            continue
        if scope is not None and str(row.get("scope") or "global") != str(scope):
            continue
        if wanted_keys and not (
            wanted_keys & {str(k) for k in (row.get("subject_keys") or [])}
        ):
            continue
        starts_at = _row_instant(row, "starts_at")
        if starts_at is None:
            continue
        ends_at = _row_instant(row, "ends_at")
        if not overlaps_window(starts_at, ends_at, w_start, w_end):
            continue
        out.append((starts_at, str(row.get("id")), event_dict(row)))
    out.sort(key=lambda item: (item[0], item[1]))
    return [d for _, _, d in out[: max(1, int(limit))]]


async def list_events(
    conn: Any,
    *,
    window_start: Any,
    window_end: Any,
    scope: str | None = None,
    subject_keys: Sequence[str] | None = None,
    limit: int = 50,
    now: Any = None,
) -> dict[str, Any]:
    """The ``list_scheduled_events`` payload: ``rows`` + ``coverage``.

    ``coverage.total_rows`` counts every live row the table holds, so a
    reader can say "no events held for this window" or "the calendar is
    empty" and never mistake one for the other.
    """
    w_start = _iso(window_start, name="window_start")
    w_end = _iso(window_end, name="window_end")
    records = await conn.fetch(LIST_EVENTS_SQL)
    stored = [dict(r) for r in records]
    total = sum(1 for r in stored if r.get("retired_at") is None)
    rows = select_events(
        stored,
        window_start=w_start,
        window_end=w_end,
        scope=scope,
        subject_keys=subject_keys,
        limit=limit,
    )
    as_of = _iso(now, name="now") if now is not None else datetime.now(timezone.utc)
    return {
        "rows": rows,
        "refs": [r["id"] for r in rows],
        "count": len(rows),
        "coverage": {
            "window": [w_start.isoformat(), w_end.isoformat()],
            "n": len(rows),
            "as_of": as_of.isoformat(),
            "total_rows": total,
        },
    }


def _valid_provenance(provenance: str) -> bool:
    return provenance in ("operator", "seed") or provenance.startswith("signal:")


async def insert_event(
    conn: Any,
    *,
    title: str,
    starts_at: Any,
    ends_at: Any = None,
    scope: str = "global",
    subject_keys: Sequence[str] = (),
    source_url: str | None = None,
    confidence: str = "confirmed",
    provenance: str = "operator",
) -> str | None:
    """INSERT one scheduled event. Returns the new id, ``None`` on the
    ``(title, starts_at)`` conflict — the loader's idempotence is the
    schema's, not a convention."""
    if confidence not in CONFIDENCE_LEVELS:
        raise ValueError(
            f"confidence {confidence!r} is not one of {CONFIDENCE_LEVELS}"
        )
    if not _valid_provenance(provenance):
        raise ValueError(
            "provenance must be 'operator', 'seed' or 'signal:<id>', "
            f"got {provenance!r}"
        )
    row = await conn.fetchrow(
        INSERT_EVENT_SQL,
        uuid4(),
        str(title),
        _iso(starts_at, name="starts_at"),
        _iso(ends_at, name="ends_at") if ends_at is not None else None,
        str(scope or "global"),
        [str(k) for k in subject_keys],
        source_url,
        confidence,
        provenance,
    )
    return str(row["id"]) if row is not None else None


# ---------------------------------------------------------------------------
# The seed file — machinery, never data (house rule 8)
# ---------------------------------------------------------------------------


@dataclass
class LoadResult:
    """Summary of one loader run (the bulk-upload errors-list house pattern)."""

    inserted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }


def parse_events_yaml(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse + validate a ``scheduled_events.yaml`` seed file.

    Returns ``(specs, errors)`` — bad rows are reported and SKIPPED, the
    rest of the file still loads (the bulk-upload house pattern). Each spec
    is the kwargs dict :func:`insert_event` takes, minus ``provenance`` —
    the loader stamps ``'seed'``.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rows = raw.get("events") or []
    specs: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, row in enumerate(rows, start=1):
        try:
            specs.append(_parse_event_row(row, i))
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append(f"row {i}: {exc}")
    return specs, errors


def _parse_event_row(row: Any, i: int) -> dict[str, Any]:
    """One YAML mapping → validated ``insert_event`` kwargs."""
    if not isinstance(row, Mapping):
        raise ValueError("expected a mapping")
    title = str(row.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    if row.get("starts_at") is None:
        raise ValueError("starts_at is required")
    starts_at = _iso(row.get("starts_at"), name="starts_at")
    ends_at = (
        _iso(row.get("ends_at"), name="ends_at")
        if row.get("ends_at") is not None
        else None
    )
    if ends_at is not None and ends_at < starts_at:
        raise ValueError("ends_at is before starts_at")
    confidence = str(row.get("confidence") or "confirmed").strip()
    if confidence not in CONFIDENCE_LEVELS:
        raise ValueError(
            f"confidence {confidence!r} is not one of {CONFIDENCE_LEVELS}"
        )
    keys = row.get("subject_keys") or []
    if not isinstance(keys, (list, tuple)):
        raise ValueError("subject_keys must be a list")
    return {
        "title": title,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "scope": str(row.get("scope") or "global").strip() or "global",
        "subject_keys": [str(k).strip() for k in keys if str(k).strip()],
        "source_url": (
            str(row["source_url"]).strip() if row.get("source_url") else None
        ),
        "confidence": confidence,
    }
