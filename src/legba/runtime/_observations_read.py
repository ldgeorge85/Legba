# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The COLLECTION series reads — `observations`, bitemporally (7g-2 §6).

7g-1 built the holding: a `collection` descriptor family, the bitemporal
`observations` table and a loader that writes it directly, fenced from every
surface that reads "now". Nothing read it. This module is the read side —
three set-based queries over `observations` and nothing else:

  * :func:`series_history` — one series, one subject, a valid-time window.
  * :func:`series_compare`  — one series, several subjects, the same window.
  * :func:`latest_per_series` — the desk-grounding block's read: the most
    recent period HELD for each series a subject has, one row per series.

WHY A SIBLING LEAF AND NOT THE PORT. ``substrate_query_port.py`` keeps thin
``self``-delegating wrappers over module-level functions that take the pool as
their first argument — the shape ``substrate_temporal`` / ``substrate_frame_reads``
/ ``substrate_graph_walks`` already established when the port hit its size
ceiling. Nothing here writes, nothing here publishes, and nothing here imports
a NATS client: a collection's firewall (§5 of the design note) is enforced at
the table for the writer and by CONSTRUCTION here for the reader.

THE BITEMPORAL RULE, stated once. ``valid_from``/``valid_to`` is the period a
number is ABOUT; ``record_time`` is when the PROVIDER published or revised it.
A 2016 GDP figure restated in 2023 is TWO rows, and every read here picks ONE
row per (valid period) by ``record_time DESC`` — the LATEST revision by
default, or the latest revision **recorded on or before ``as_of``** when the
caller supplies one. That is what makes "what did we know in 2024 about 2016"
a different, answerable question from "what do we know now about 2016", and
what stops an as-of replay quietly inheriting a restatement that had not
happened yet. An ``as_of`` before anything was recorded returns NOTHING, which
is the honest answer and not an error.

THE SHAPE, and why it is `DISTINCT ON` rather than a probe. Each read is ONE
statement whose driving predicate is the leading columns of
``observations_subject_period_idx`` (collection_id, subject, series_id,
valid_from, valid_to, record_time DESC) — a correlated
``ORDER BY … LIMIT 1`` per series is the shape this repo's review rules name
as having failed repeatedly, and it is not used here.

THE PARTITION-PRUNING PREDICATE. ``observations`` is RANGE-partitioned on
``valid_from``, so a window given only as ``valid_to <= $to`` prunes NOTHING
above the window — every future partition is still scanned. Every read here
therefore carries the redundant-looking ``valid_from <= $to`` as well. It is
implied by ``valid_to <= $to`` (the table CHECKs ``valid_to >= valid_from``),
it changes no result, and it is what turns a 13-partition Append into a
3-partition one. Measured live 2026-09-25 on the pilot: 0.558 ms / 15 buffers
for a three-year window, against 1.454 ms / 35 buffers for the whole decade.

WHICH COLLECTIONS ARE READABLE. Only those whose descriptor head is in state
``loaded`` — :func:`loaded_collections`. A holding in ``draft`` or ``reviewed``
has not been approved by the operator and a ``superseded`` one has been
replaced; reading either would put an unapproved or retired number inside a
citation. The resolved id list is passed as an array so the reads keep their
leading index column, and an EMPTY list short-circuits to no rows rather than
degrading into an unbounded scan.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_SERIES_ROWS",
    "MAX_SUBJECTS",
    "OBSERVATION_REF_PREFIX",
    "desk_subjects",
    "latest_per_series",
    "loaded_collections",
    "observation_ref",
    "read_series_compare",
    "read_series_history",
    "series_compare",
    "series_history",
]


#: Hard cap on rows any one series read returns. Ten years of annual data for
#: one subject is 10 rows; a revised decade is ~20. 500 is far past anything a
#: prompt or a tool round can read and still be a bounded answer, and it is
#: what stops a caller asking for a whole holding in one call.
MAX_SERIES_ROWS: int = 500

#: Hard cap on the subject list a comparison may name. Four desks ship today;
#: the headroom is for a region, not for "every country".
MAX_SUBJECTS: int = 24

#: The token a citation carries for one observations row. Deliberately the same
#: ``<kind>:<uuid>`` spelling the event token uses, so a reader who has met one
#: already knows how to read the other.
OBSERVATION_REF_PREFIX: str = "observation"


def observation_ref(observation_id: Any) -> str:
    """``observation:<uuid>`` for one row — the citation builder's handle."""
    return f"{OBSERVATION_REF_PREFIX}:{observation_id}"


# ---------------------------------------------------------------------------
# Which holdings may be read, and which subject a desk is
# ---------------------------------------------------------------------------

#: The LOADED collection heads. ``state='loaded'`` is the operator's approval
#: plus a completed load; every other lifecycle state is excluded at the source
#: rather than filtered by a reader that might forget.
_LOADED_COLLECTIONS_SQL = """
    SELECT descriptor_id, collection_version, licence_class, origin_class
      FROM collection_descriptors
     WHERE is_head IS TRUE
       AND state = 'loaded'
     ORDER BY descriptor_id
"""

#: The desk → subject map, read off each LOADED holding's OWN ``subjects[]``
#: block. Deliberately NOT off ``target_descriptors.scope.geo``: the holding is
#: what declares which desk it was loaded for, and inferring the subject from a
#: desk's geo array would silently start reading another country's numbers the
#: day a desk's scope widens.
_DESK_SUBJECTS_SQL = """
    SELECT cd.descriptor_id                    AS collection_id,
           cd.collection_version               AS collection_version,
           cd.licence_class                    AS licence_class,
           s->>'subject'                       AS subject,
           s->>'name'                          AS subject_name,
           COALESCE(cd.body->>'subject_kind', 'country') AS subject_kind
      FROM collection_descriptors cd
      CROSS JOIN LATERAL jsonb_array_elements(cd.body->'subjects') s
     WHERE cd.is_head IS TRUE
       AND cd.state = 'loaded'
       AND s->>'desk' = $1
     ORDER BY cd.descriptor_id
"""


async def loaded_collections(conn) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Every LOADED collection head — ``[]`` when none is loaded.

    ``[]`` is a real answer and the callers treat it as one: no holding is
    approved, so no history exists to read and no block renders. It is never
    widened into "read everything".
    """
    rows = await conn.fetch(_LOADED_COLLECTIONS_SQL)
    return [
        {
            "collection_id": str(r["descriptor_id"]),
            "collection_version": _text(r["collection_version"]),
            "licence_class": _text(r["licence_class"]),
            "origin_class": _text(r["origin_class"]),
        }
        for r in rows
        if r["descriptor_id"]
    ]


async def desk_subjects(conn, *, desk: str) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """The (collection, subject) pairs a DESK is named by in a loaded holding.

    ``[]`` when no loaded collection names this desk — which is not a gap in a
    holding, it is the absence of a holding, and the callers say so in those
    words rather than reporting an empty history.
    """
    if not desk:
        return []
    rows = await conn.fetch(_DESK_SUBJECTS_SQL, str(desk))
    return [
        {
            "collection_id": str(r["collection_id"]),
            "collection_version": _text(r["collection_version"]),
            "licence_class": _text(r["licence_class"]),
            "subject": str(r["subject"]),
            "subject_name": _text(r["subject_name"]),
            "subject_kind": _text(r["subject_kind"]) or "country",
        }
        for r in rows
        if r["subject"]
    ]


# ---------------------------------------------------------------------------
# The three reads
# ---------------------------------------------------------------------------

#: The projection every read returns. Kept in one string so the three queries
#: cannot drift in what a cited observation carries.
_COLUMNS = """
       o.id, o.collection_id, o.series_id, o.subject, o.subject_kind,
       o.valid_from, o.valid_to, o.record_time, o.value, o.value_text,
       o.unit, o.source_url, o.sha256, o.origin_class, o.provenance
"""

_SERIES_HISTORY_SQL = f"""
    SELECT DISTINCT ON (o.valid_from, o.valid_to)
{_COLUMNS}
      FROM observations o
     WHERE o.collection_id = ANY($1::text[])
       AND o.series_id = $2
       AND o.subject = $3
       AND o.valid_from >= $4
       AND o.valid_from <= $5
       AND o.valid_to   <= $5
       AND ($6::timestamptz IS NULL OR o.record_time <= $6)
     ORDER BY o.valid_from, o.valid_to, o.record_time DESC
     LIMIT $7
"""

_SERIES_COMPARE_SQL = f"""
    SELECT DISTINCT ON (o.subject, o.valid_from, o.valid_to)
{_COLUMNS}
      FROM observations o
     WHERE o.collection_id = ANY($1::text[])
       AND o.series_id = $2
       AND o.subject = ANY($3::text[])
       AND o.valid_from >= $4
       AND o.valid_from <= $5
       AND o.valid_to   <= $5
       AND ($6::timestamptz IS NULL OR o.record_time <= $6)
     ORDER BY o.subject, o.valid_from, o.valid_to, o.record_time DESC
     LIMIT $7
"""

_LATEST_PER_SERIES_SQL = f"""
    SELECT DISTINCT ON (o.series_id)
{_COLUMNS}
      FROM observations o
     WHERE o.collection_id = ANY($1::text[])
       AND o.subject = $2
       AND o.valid_from >= $3
       AND ($4::timestamptz IS NULL OR o.record_time <= $4)
     ORDER BY o.series_id, o.valid_from DESC, o.valid_to DESC,
              o.record_time DESC
     LIMIT $5
"""


async def series_history(  # type: ignore[no-untyped-def]
    conn,
    *,
    collection_ids: Sequence[str],
    series_id: str,
    subject: str,
    valid_from: date,
    valid_to: date,
    as_of: datetime | None = None,
    limit: int = MAX_SERIES_ROWS,
) -> list[dict[str, Any]]:
    """One series for one subject over a valid-time window, latest revision first.

    ONE row per valid period: the revision with the greatest ``record_time``,
    or the greatest ``record_time <= as_of`` when an as-of is given. Rows come
    back oldest period first, which is the order a reader reads a series in.

    ``[]`` when the holding has no row for the window — absence is absence, and
    the caller renders it as such rather than as a zero.
    """
    if not collection_ids or not series_id or not subject:
        return []
    rows = await conn.fetch(
        _SERIES_HISTORY_SQL,
        [str(c) for c in collection_ids],
        str(series_id),
        str(subject),
        valid_from,
        valid_to,
        as_of,
        _clamp(limit, MAX_SERIES_ROWS),
    )
    return [_row(r) for r in rows]


async def series_compare(  # type: ignore[no-untyped-def]
    conn,
    *,
    collection_ids: Sequence[str],
    series_id: str,
    subjects: Sequence[str],
    valid_from: date,
    valid_to: date,
    as_of: datetime | None = None,
    limit: int = MAX_SERIES_ROWS,
) -> list[dict[str, Any]]:
    """One series across several subjects over the same window.

    The same per-(subject, period) revision rule as :func:`series_history`, in
    ONE statement rather than N calls: the subjects ride an array so the read
    keeps the index's leading ``(collection_id, subject)`` columns instead of
    becoming N round trips a caller has to stitch.

    A subject the holding does not carry simply contributes no rows. It is NOT
    padded with nulls: the caller can see which subjects answered by reading
    the rows, and a padded row would be a number nobody published.
    """
    wanted = _unique_strings(subjects, MAX_SUBJECTS)
    if not collection_ids or not series_id or not wanted:
        return []
    rows = await conn.fetch(
        _SERIES_COMPARE_SQL,
        [str(c) for c in collection_ids],
        str(series_id),
        wanted,
        valid_from,
        valid_to,
        as_of,
        _clamp(limit, MAX_SERIES_ROWS),
    )
    return [_row(r) for r in rows]


async def latest_per_series(  # type: ignore[no-untyped-def]
    conn,
    *,
    collection_ids: Sequence[str],
    subject: str,
    valid_from: date,
    as_of: datetime | None = None,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """The most recent period HELD for each series this subject has.

    The desk-grounding block's read: one line per series, so a desk sees WHICH
    numbers the platform holds for it before it is asked to reason about any of
    them. ``valid_from`` bounds the scan (and prunes partitions); the ``limit``
    bounds the LINES, not the scan, so it is a render bound and the docstring
    says so rather than implying a cost guarantee it does not give.
    """
    if not collection_ids or not subject:
        return []
    rows = await conn.fetch(
        _LATEST_PER_SERIES_SQL,
        [str(c) for c in collection_ids],
        str(subject),
        valid_from,
        as_of,
        _clamp(limit, MAX_SERIES_ROWS),
    )
    return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# The TOOL surface — string args in, the pack's {rows, refs, count} shape out
#
# The pack handler hands whatever the planner wrote. Parsing lives HERE rather
# than in the port wrapper or the handler so there is exactly one answer to
# "what does `from: 2016` mean" — and so a malformed window REFUSES instead of
# widening to all-time, the same posture ``query_events`` takes.
# ---------------------------------------------------------------------------


async def read_series_history(  # type: ignore[no-untyped-def]
    pool,
    *,
    series_id: str,
    subject: str,
    since: Any,
    until: Any,
    as_of: Any = None,
    collection_id: Any = None,
    limit: int = MAX_SERIES_ROWS,
) -> dict[str, Any]:
    """``series_history`` — one series, one subject, a bounded valid window."""
    return await _read(
        pool,
        kind="series_history",
        series_id=series_id,
        subjects=[subject],
        since=since,
        until=until,
        as_of=as_of,
        collection_id=collection_id,
        limit=limit,
    )


async def read_series_compare(  # type: ignore[no-untyped-def]
    pool,
    *,
    series_id: str,
    subjects: Any,
    since: Any,
    until: Any,
    as_of: Any = None,
    collection_id: Any = None,
    limit: int = MAX_SERIES_ROWS,
) -> dict[str, Any]:
    """``series_compare`` — one series across several subjects, same window."""
    return await _read(
        pool,
        kind="series_compare",
        series_id=series_id,
        subjects=subjects if isinstance(subjects, (list, tuple)) else [subjects],
        since=since,
        until=until,
        as_of=as_of,
        collection_id=collection_id,
        limit=limit,
    )


async def _read(  # type: ignore[no-untyped-def]
    pool,
    *,
    kind: str,
    series_id: str,
    subjects: Sequence[Any],
    since: Any,
    until: Any,
    as_of: Any,
    collection_id: Any,
    limit: int,
) -> dict[str, Any]:
    """The shared body of both tools — one connection, one statement.

    Every refusal is a NAMED ``error`` on a successful-shaped mapping rather
    than an exception: the tool loop folds the message back to the planner,
    which can then fix the argument, where a raised exception would come back
    as an opaque ``tool_failed``.
    """
    name = str(series_id or "").strip()
    if not name:
        return _refusal(kind, "series_id is required and must name a manifest series")
    wanted = _unique_strings(subjects, MAX_SUBJECTS)
    if not wanted:
        return _refusal(kind, "at least one subject is required (ISO-3166-1 alpha-2)")
    try:
        window_from = _as_date(since, "from")
        window_to = _as_date(until, "to")
        as_of_at = _as_datetime(as_of) if as_of not in (None, "") else None
    except ValueError as exc:
        return _refusal(kind, str(exc))
    if window_from > window_to:
        return _refusal(
            kind, f"from {window_from.isoformat()} is after to {window_to.isoformat()}"
        )

    async with pool.acquire() as conn:
        holdings = await loaded_collections(conn)
        ids = [h["collection_id"] for h in holdings]
        if collection_id:
            asked = str(collection_id).strip()
            if asked not in ids:
                return _refusal(
                    kind,
                    f"collection {asked!r} is not loaded; loaded holdings: "
                    + (", ".join(ids) if ids else "(none)"),
                )
            ids = [asked]
        if not ids:
            return _refusal(
                kind,
                "no collection is in state 'loaded' — the platform holds no "
                "history to read, which is an absence and not an error",
            )
        if kind == "series_history":
            rows = await series_history(
                conn,
                collection_ids=ids,
                series_id=name,
                subject=wanted[0],
                valid_from=window_from,
                valid_to=window_to,
                as_of=as_of_at,
                limit=limit,
            )
        else:
            rows = await series_compare(
                conn,
                collection_ids=ids,
                series_id=name,
                subjects=wanted,
                valid_from=window_from,
                valid_to=window_to,
                as_of=as_of_at,
                limit=limit,
            )

    return {
        "rows": rows,
        # The substrate ids the consult loop cites. An observations id is a
        # real row id, so it belongs here exactly as a signal id does.
        "refs": [r["observation_id"] for r in rows],
        "count": len(rows),
        "series_id": name,
        "subjects": wanted,
        "collections_read": ids,
        "valid_from": window_from.isoformat(),
        "valid_to": window_to.isoformat(),
        "as_of": as_of_at.isoformat() if as_of_at is not None else None,
        "as_of_note": (
            "rows are the latest revision RECORDED ON OR BEFORE this instant"
            if as_of_at is not None
            else "rows are the latest revision on record"
        ),
        "subjects_with_no_rows": [
            s for s in wanted if not any(r["subject"] == s for r in rows)
        ],
        "history_note": (
            "These are HISTORICAL observations from a curated holding, not "
            "live reporting. Every number is about its own valid period and "
            "was recorded by the provider at its own record_time; cite it "
            "with both."
        ),
    }


def _refusal(kind: str, why: str) -> dict[str, Any]:
    """A read that refuses, in the tools' own success shape (0 rows + error)."""
    logger.info("observations_read.refused tool=%s why=%s", kind, why)
    return {"rows": [], "refs": [], "count": 0, "error": why}


def _as_date(value: Any, label: str) -> date:
    """A bound of the valid window. A bare year (``2016``) is that year's start.

    Refuses rather than guessing: an unparseable bound would otherwise widen
    the window to everything the holding carries, which is the one failure a
    reader cannot see in the answer.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        raise ValueError(
            f"{label} is required — a series read is always over a bounded "
            "valid-time window, never all-time"
        )
    if len(text) == 4 and text.isdigit():
        return date(int(text), 1, 1) if label == "from" else date(int(text), 12, 31)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(
            f"{label}={text!r} is not a date (YYYY-MM-DD or YYYY)"
        ) from None


def _as_datetime(value: Any) -> datetime:
    """The as-of instant. A bare date is that day's start, in UTC."""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"as_of={value!r} is not an ISO-8601 instant") from None
    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Row projection — JSON-safe, never lossy about a number
# ---------------------------------------------------------------------------


def _row(record: Mapping[str, Any]) -> dict[str, Any]:
    """One observations row as a JSON-safe dict carrying its own provenance.

    ``value`` is a Postgres ``numeric``, which asyncpg hands back as a
    ``Decimal``. It is carried BOTH ways: ``value`` as a float for arithmetic
    and ``value_display`` as the Decimal's own string, because float() of a
    28-digit external-debt figure loses digits a citation would then be wrong
    about. A row with ``value_text`` instead of ``value`` (the table CHECKs
    exactly one) carries the text and no number.
    """
    value = record.get("value")
    out: dict[str, Any] = {
        "observation_id": str(record["id"]),
        "ref": observation_ref(record["id"]),
        "collection_id": _text(record.get("collection_id")),
        "series_id": _text(record.get("series_id")),
        "subject": _text(record.get("subject")),
        "subject_kind": _text(record.get("subject_kind")),
        "valid_from": _iso(record.get("valid_from")),
        "valid_to": _iso(record.get("valid_to")),
        "record_time": _iso(record.get("record_time")),
        "value": float(value) if isinstance(value, (Decimal, int, float)) else None,
        "value_display": (
            format(value, "f") if isinstance(value, Decimal) else _text(value)
        ),
        "value_text": _text(record.get("value_text")),
        "unit": _text(record.get("unit")),
        "source_url": _text(record.get("source_url")),
        "sha256": _text(record.get("sha256")),
        "origin_class": _text(record.get("origin_class")),
    }
    provenance = record.get("provenance")
    if isinstance(provenance, str):
        try:
            provenance = json.loads(provenance)
        except ValueError:  # pragma: no cover — jsonb never round-trips broken
            provenance = None
    if isinstance(provenance, Mapping):
        # Only the four fields a reader of a CITATION needs — who published it,
        # what the provider calls it, when the provider last moved the series,
        # and under which licence it was loaded. The whole blob also carries
        # the row offset and the manifest version, which belong in a forensic
        # read of the table and not in a prompt.
        out["provider"] = _text(provenance.get("provider"))
        out["indicator_name"] = _text(provenance.get("indicator_name"))
        out["provider_last_updated"] = _text(
            provenance.get("provider_last_updated")
        )
        out["licence_class"] = _text(provenance.get("licence_class"))
    return out


def _clamp(value: Any, ceiling: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ceiling
    return max(1, min(n, ceiling))


def _unique_strings(values: Iterable[Any], cap: int) -> list[str]:
    """De-duplicated, order-preserving, capped — never a set (order is the
    answer's order, and a set would shuffle a comparison between two runs)."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in values or ():
        text = str(raw).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= cap:
            break
    return out


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
