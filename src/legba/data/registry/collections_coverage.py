# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ERA COVERAGE MAP — what a holding DECLARES against what the table HOLDS.

7g-1 loaded a curated holding and wrote a ledger row saying how many pairs it
wrote. That answers "did the load finish". It does not answer the question a
reader of a cited historical number actually has, which is *what years does the
platform hold for this desk, and where are the holes* — and a load that
finished with 355 of 365 rows is indistinguishable, from the ledger alone, from
one that finished with all of them.

This module is that comparison, computed ONE way for TWO readers:

  * ``GET /api/v1/v3/collections/coverage?scope=<desk>`` — the map itself
    (``collections_api.py``);
  * the ``history_gap`` typed absence on ``GET /v3/absence?scope=<desk>``
    (``absence_api.py``).

A second implementation over the same tables is how two surfaces start telling
a reader two different stories about the same silence, so — exactly as
``absence_api`` and ``export_absences`` share one reader — there is one here.

WHAT "DECLARED" MEANS, precisely. The manifest's per-series ``coverage`` block
is not a guess: it is what ``scripts/verify_collection_manifest.py`` MEASURED
at the provider, recorded as ``first_valid_year`` / ``last_valid_year`` /
``values`` / ``nulls``, with ``held: false`` + a ``reason`` where a provider
holds a subject not at all. So the declared extent of a (series, subject) pair
is the value-bearing span the provider was measured to have — and a pair the
provider does NOT hold is NOT a gap in this platform, it is an absence at the
source, which the manifest already records in the provider's own terms. Those
two are kept apart here and named differently on both readers.

WHEN THE TWO DISAGREE ABOUT THEMSELVES. A coverage block whose ``values`` count
does not equal its own ``first..last`` span has interior provider nulls, and
the manifest does not record WHICH years they are. This module does not guess:
it reports the span, the counts and a ``declared_note`` saying the two do not
line up, and it does NOT emit a `history_gap` for that pair — an absence whose
proof cannot distinguish "we failed to load it" from "the provider never
published it" is not a typed absence, it is a blank with a label.

Registry-slim: stdlib + pydantic + ``..schemas.collection`` (itself stdlib +
pydantic). Nothing from ``legba.data.analysts`` or ``legba.runtime``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

__all__ = [
    "COLLECTIONS_COVERAGE_VERSION",
    "CoverageOut",
    "GAP_SHELF_LIFE_DAYS",
    "HISTORY_GAP_KIND",
    "SeriesCoverage",
    "SubjectHolding",
    "history_gap_items",
    "read_coverage",
    "year_spans",
]

#: The route's own version stamp, in the same shape the absence route uses.
COLLECTIONS_COVERAGE_VERSION: str = "2026-09/7g-2"

#: The absence kind this module produces.
HISTORY_GAP_KIND: str = "history_gap"

#: How long a measured history gap stays CURRENT.
#:
#: Every other absence kind takes its shelf life from the CADENCE of whatever
#: measured it (``absence_api.schedule_stamp``). A collection has no cadence by
#: construction — nothing schedules it, that is the whole point of the family —
#: so there is no next fire to compute from. Thirty days is therefore not a
#: schedule, it is a stated REVIEW interval: a gap nobody has re-run the loader
#: against in a month reads as *last known, not re-checked*, which is the same
#: sentence every stale absence reads as, for the same reason. It is a
#: constant rather than a guess dressed up as a schedule.
GAP_SHELF_LIFE_DAYS: int = 30


# ---------------------------------------------------------------------------
# SQL — three bounded reads
# ---------------------------------------------------------------------------

#: Every LOADED holding's head, with the body the manifest lives in.
_HOLDINGS_SQL = """
    SELECT descriptor_id, collection_version, licence_class, origin_class, body
      FROM collection_descriptors
     WHERE is_head IS TRUE
       AND state = 'loaded'
     ORDER BY descriptor_id
"""

#: The completed load per (collection, manifest version) — the RECEIPT a
#: `history_gap` points at. A gap's proof is not the missing row (there is no
#: row), it is the load that should have written it.
_LOADS_SQL = """
    SELECT id, collection_id, collection_version, status, finished_at,
           pairs_total, pairs_done, rows_written, rows_skipped
      FROM collection_loads
     WHERE collection_id = ANY($1::text[])
       AND status = 'completed'
     ORDER BY collection_id, started_at DESC
"""

#: What the table HOLDS for one subject, per series. One GroupAggregate over
#: the leading ``(collection_id, subject)`` columns of
#: ``observations_subject_period_idx`` — measured live 2026-09-25 on the pilot:
#: 2.142 ms, 75 shared buffer hits, ten series out of 87 rows scanned.
_HELD_SQL = """
    SELECT o.series_id,
           min(o.valid_from)  AS held_from,
           max(o.valid_to)    AS held_to,
           count(*)           AS rows_held,
           array_agg(DISTINCT EXTRACT(YEAR FROM o.valid_from)::int) AS years,
           max(o.record_time) AS last_record_time,
           min(o.unit)        AS unit
      FROM observations o
     WHERE o.collection_id = $1
       AND o.subject = $2
     GROUP BY o.series_id
     ORDER BY o.series_id
"""


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class YearSpan(BaseModel):
    """A contiguous run of valid-time YEARS. Inclusive at both ends."""

    from_year: int
    to_year: int


class DeclaredExtent(BaseModel):
    """What the manifest says the PROVIDER holds for one (series, subject).

    ``provider_holds`` is False when the manifest recorded the provider as
    holding nothing for this subject in any year (the World Bank publishes no
    external-debt figure for the US), and ``reason`` carries the manifest's own
    words for it. That is an absence at the SOURCE, never a gap here.
    """

    valid_from: str
    valid_to: str
    first_valid_year: Optional[int] = None
    last_valid_year: Optional[int] = None
    values: Optional[int] = None
    nulls: Optional[int] = None
    provider_holds: bool = True
    reason: Optional[str] = None
    measured_at: Optional[str] = None
    provider_last_updated: Optional[str] = None
    declared_note: Optional[str] = None


class HeldExtent(BaseModel):
    """What ``observations`` actually holds. All-absent renders as absent."""

    spans: list[YearSpan] = Field(default_factory=list)
    years: int = 0
    rows: int = 0
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    last_record_time: Optional[str] = None
    unit: Optional[str] = None


class SeriesCoverage(BaseModel):
    """One (series, subject) row of the era map."""

    series_id: str
    provider: Optional[str] = None
    indicator_name: Optional[str] = None
    unit: Optional[str] = None
    cadence: Optional[str] = None
    declared: DeclaredExtent
    held: HeldExtent
    holes: list[YearSpan] = Field(default_factory=list)
    status: str


class SubjectHolding(BaseModel):
    """One loaded holding, as it covers ONE desk."""

    collection_id: str
    collection_version: Optional[str] = None
    licence_class: Optional[str] = None
    origin_class: Optional[str] = None
    subject: str
    subject_name: Optional[str] = None
    subject_kind: str = "country"
    load_id: Optional[str] = None
    loaded_at: Optional[str] = None
    window_valid_from: Optional[str] = None
    window_valid_to: Optional[str] = None
    series: list[SeriesCoverage] = Field(default_factory=list)


class CoverageOut(BaseModel):
    """``collections`` empty means NO loaded holding names this desk — which is
    the absence of a holding, not an empty history, and ``not_held`` says so in
    the route's own words rather than leaving a reader to infer it."""

    version: str = COLLECTIONS_COVERAGE_VERSION
    scope: str
    read_at: str
    collections: list[SubjectHolding] = Field(default_factory=list)
    not_held: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure projection — no DB, no clock beyond what is passed in
# ---------------------------------------------------------------------------


def year_spans(years: Iterable[int]) -> list[YearSpan]:
    """Collapse a set of years into contiguous inclusive spans.

    ``[2016, 2017, 2018, 2020]`` → ``2016..2018`` and ``2020..2020``. The map
    is read as eras, and eleven single-year rows per series is a table nobody
    reads; the collapse is presentation, and it is lossless.
    """
    ordered = sorted({int(y) for y in years if y is not None})
    spans: list[YearSpan] = []
    for year in ordered:
        if spans and year == spans[-1].to_year + 1:
            spans[-1] = YearSpan(from_year=spans[-1].from_year, to_year=year)
        else:
            spans.append(YearSpan(from_year=year, to_year=year))
    return spans


def _declared(series: Mapping[str, Any], subject: str) -> DeclaredExtent:
    """The manifest's declared extent for one (series, subject)."""
    coverage = series.get("coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    block = coverage.get(subject)
    block = block if isinstance(block, Mapping) else {}
    values = _int(block.get("values"))
    first = _int(block.get("first_valid_year"))
    last = _int(block.get("last_valid_year"))
    held_flag = block.get("held")
    provider_holds = not (held_flag is False or (values is not None and values == 0))
    note: str | None = None
    if provider_holds and values is not None and first is not None and last is not None:
        span = last - first + 1
        if values != span:
            note = (
                f"the manifest declares {values} value(s) across "
                f"{first}-{last} ({span} years), so {span - values} year(s) "
                "inside that span carry no provider value and the manifest "
                "does not record which"
            )
    return DeclaredExtent(
        valid_from=str(series.get("valid_from") or ""),
        valid_to=str(series.get("valid_to") or ""),
        first_valid_year=first,
        last_valid_year=last,
        values=values,
        nulls=_int(block.get("nulls")),
        provider_holds=provider_holds,
        reason=_text(block.get("reason")),
        measured_at=_text(coverage.get("fetched_at")),
        provider_last_updated=_text(coverage.get("provider_last_updated")),
        declared_note=note,
    )


#: The four statuses a (series, subject) row can be in. Named as data so both
#: readers and the docs say the same four words.
_STATUS_COMPLETE = "held_complete"
_STATUS_PARTIAL = "held_with_holes"
_STATUS_NOT_LOADED = "declared_not_loaded"
_STATUS_NOT_PUBLISHED = "provider_holds_nothing"
_STATUS_UNDETERMINED = "declared_extent_undetermined"


def _series_coverage(
    series: Mapping[str, Any], subject: str, held_row: Mapping[str, Any] | None
) -> SeriesCoverage:
    """One era-map row: declared, held, and the holes between them."""
    declared = _declared(series, subject)
    held_years = sorted(
        {int(y) for y in (held_row or {}).get("years") or [] if y is not None}
    )
    held = HeldExtent(
        spans=year_spans(held_years),
        years=len(held_years),
        rows=_int((held_row or {}).get("rows_held")) or 0,
        valid_from=_iso((held_row or {}).get("held_from")),
        valid_to=_iso((held_row or {}).get("held_to")),
        last_record_time=_iso((held_row or {}).get("last_record_time")),
        unit=_text((held_row or {}).get("unit")) or _text(series.get("unit")),
    )
    holes: list[YearSpan] = []
    if not declared.provider_holds:
        status = _STATUS_NOT_PUBLISHED
    elif declared.declared_note is not None:
        # The manifest disagrees with itself about which years carry a value.
        # Reported, never guessed at — see the module note.
        status = _STATUS_UNDETERMINED
    elif declared.first_valid_year is None or declared.last_valid_year is None:
        status = _STATUS_UNDETERMINED
    else:
        expected = set(range(declared.first_valid_year, declared.last_valid_year + 1))
        missing = expected - set(held_years)
        holes = year_spans(missing)
        if not held_years:
            status = _STATUS_NOT_LOADED
        elif missing:
            status = _STATUS_PARTIAL
        else:
            status = _STATUS_COMPLETE
    return SeriesCoverage(
        series_id=str(series.get("series_id") or ""),
        provider=_text(series.get("provider")),
        indicator_name=_text(series.get("indicator_name")),
        unit=_text(series.get("unit")),
        cadence=_text(series.get("cadence")),
        declared=declared,
        held=held,
        holes=holes,
        status=status,
    )


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


async def read_coverage(  # type: ignore[no-untyped-def]
    conn,
    *,
    scope: str,
    now: datetime,
) -> CoverageOut:
    """The era coverage map for ONE desk.

    Three bounded reads: the loaded holdings (a handful of descriptor rows),
    their completed load receipts, and one grouped aggregate over
    ``observations`` per (holding, subject) pair the desk is named by.

    An unreadable step is never swallowed into a fabricated empty map — it
    raises, and the route answers with the error rather than with a coverage
    claim it did not make.
    """
    desk = (scope or "").strip()
    out = CoverageOut(scope=desk, read_at=now.isoformat())
    if not desk:
        return out

    holdings = [dict(r) for r in await conn.fetch(_HOLDINGS_SQL)]
    if not holdings:
        out.not_held.append(
            "no collection is in state 'loaded' — the platform holds no "
            "curated history for any desk yet"
        )
        return out

    loads: dict[str, dict[str, Any]] = {}
    for row in await conn.fetch(
        _LOADS_SQL, [str(h["descriptor_id"]) for h in holdings]
    ):
        loads.setdefault(str(row["collection_id"]), dict(row))

    covered_desks: set[str] = set()
    for holding in holdings:
        body = _jsonb(holding.get("body")) or {}
        subjects = body.get("subjects")
        subjects = subjects if isinstance(subjects, list) else []
        for entry in subjects:
            if not isinstance(entry, Mapping):
                continue
            entry_desk = _text(entry.get("desk"))
            if entry_desk:
                covered_desks.add(entry_desk)
            if entry_desk != desk:
                continue
            out.collections.append(
                await _holding_for_subject(
                    conn,
                    holding=holding,
                    body=body,
                    entry=entry,
                    load=loads.get(str(holding["descriptor_id"])),
                )
            )

    if not out.collections:
        named = ", ".join(sorted(covered_desks)) or "(none)"
        out.not_held.append(
            f"no loaded collection names this desk; the loaded holdings cover "
            f"{named}"
        )
    return out


async def _holding_for_subject(  # type: ignore[no-untyped-def]
    conn,
    *,
    holding: Mapping[str, Any],
    body: Mapping[str, Any],
    entry: Mapping[str, Any],
    load: Mapping[str, Any] | None,
) -> SubjectHolding:
    collection_id = str(holding["descriptor_id"])
    subject = str(entry.get("subject") or "")
    held_rows = {
        str(r["series_id"]): dict(r)
        for r in await conn.fetch(_HELD_SQL, collection_id, subject)
    }
    manifest = body.get("manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    series_list = manifest.get("series")
    series_list = series_list if isinstance(series_list, list) else []
    window = body.get("window")
    window = window if isinstance(window, Mapping) else {}
    rows: list[SeriesCoverage] = []
    for series in series_list:
        if not isinstance(series, Mapping):
            continue
        subjects = series.get("subjects")
        if not isinstance(subjects, list) or subject not in [
            str(s) for s in subjects
        ]:
            continue
        rows.append(
            _series_coverage(
                series, subject, held_rows.get(str(series.get("series_id") or ""))
            )
        )
    return SubjectHolding(
        collection_id=collection_id,
        collection_version=_text(holding.get("collection_version")),
        licence_class=_text(holding.get("licence_class")),
        origin_class=_text(holding.get("origin_class")),
        subject=subject,
        subject_name=_text(entry.get("name")),
        subject_kind=_text(body.get("subject_kind")) or "country",
        load_id=_text((load or {}).get("id")),
        loaded_at=_iso((load or {}).get("finished_at")),
        window_valid_from=_text(window.get("valid_from")),
        window_valid_to=_text(window.get("valid_to")),
        series=rows,
    )


# ---------------------------------------------------------------------------
# The typed absence — `history_gap`
# ---------------------------------------------------------------------------


def history_gap_items(
    coverage: CoverageOut, *, now: datetime
) -> tuple[list[dict[str, Any]], list[str]]:
    """``(absence items, not_measured sentences)`` for the ``history_gap`` kind.

    THREE ANSWERS, kept apart, because collapsing any two of them is how a
    reader ends up believing a provider never published a number we simply
    failed to load:

      * a (series, subject) pair the manifest declares and the table does not
        fully hold → ONE ``history_gap`` item, proved by the load receipt that
        should have written it;
      * a desk a loaded holding NAMES but for which nothing was written at
        all → ONE ``history_gap`` item for the whole desk;
      * a desk no loaded holding names → NOT a gap. It goes to
        ``not_measured`` naming which desks the holdings do cover, because a
        gap in a holding that does not exist is not an absence this platform
        can type.

    A pair the PROVIDER holds nothing for is never an item at either level: the
    manifest records that absence in the provider's own words, and restating it
    as our gap would move the blame and lose the reason.
    """
    items: list[dict[str, Any]] = []
    not_measured: list[str] = list(coverage.not_held)
    for holding in coverage.collections:
        gappy = [
            s
            for s in holding.series
            if s.status in (_STATUS_PARTIAL, _STATUS_NOT_LOADED)
        ]
        if not holding.series:
            items.append(
                _gap_item(
                    holding,
                    subject=f"{holding.collection_id}:{holding.subject}",
                    reason=(
                        "this holding names this desk but its manifest "
                        "declares no series for the desk's subject "
                        f"{holding.subject}"
                    ),
                    what=(
                        "every manifest series naming subject "
                        f"{holding.subject} in collection "
                        f"{holding.collection_id}"
                    ),
                    now=now,
                )
            )
            continue
        if not any(s.held.rows for s in holding.series):
            items.append(
                _gap_item(
                    holding,
                    subject=f"{holding.collection_id}:{holding.subject}",
                    reason=(
                        f"this holding declares {len(holding.series)} series "
                        f"for {holding.subject} and the observations table "
                        "holds no row for any of them"
                    ),
                    what=(
                        "observations rows for every manifest series naming "
                        f"subject {holding.subject} in collection "
                        f"{holding.collection_id}"
                    ),
                    now=now,
                )
            )
            continue
        for series in gappy:
            items.append(
                _gap_item(
                    holding,
                    subject=f"{series.series_id}:{holding.subject}",
                    reason=_gap_reason(series, holding.subject),
                    what=(
                        f"observations rows for series {series.series_id}, "
                        f"subject {holding.subject}, against the manifest's "
                        "measured provider coverage"
                    ),
                    now=now,
                    window=_holes_text(series),
                )
            )
        undetermined = [
            s for s in holding.series if s.status == _STATUS_UNDETERMINED
        ]
        for series in undetermined:
            not_measured.append(
                f"{HISTORY_GAP_KIND}: {series.series_id}:{holding.subject} — "
                + (
                    series.declared.declared_note
                    or "the manifest records no measured provider coverage for "
                    "this pair"
                )
                + ", so a hole here cannot be told apart from a provider null"
            )
    return items, not_measured


def _gap_reason(series: SeriesCoverage, subject: str) -> str:
    declared = series.declared
    span = (
        f"{declared.first_valid_year}-{declared.last_valid_year}"
        if declared.first_valid_year is not None
        else "its declared window"
    )
    if not series.held.rows:
        return (
            f"the manifest declares {declared.values} value(s) for {subject} "
            f"over {span} and the observations table holds none"
        )
    return (
        f"the manifest declares {declared.values} value(s) for {subject} over "
        f"{span}; the table holds {series.held.years} "
        f"({_spans_text(series.held.spans)})"
    )


def _holes_text(series: SeriesCoverage) -> str | None:
    if not series.holes:
        return None
    return f"missing valid years: {_spans_text(series.holes)}"


def _spans_text(spans: Sequence[YearSpan]) -> str:
    if not spans:
        return "—"
    return ", ".join(
        str(s.from_year) if s.from_year == s.to_year else f"{s.from_year}-{s.to_year}"
        for s in spans
    )


def _gap_item(
    holding: SubjectHolding,
    *,
    subject: str,
    reason: str,
    what: str,
    now: datetime,
    window: str | None = None,
) -> dict[str, Any]:
    """One ``history_gap``, stamped by the LOAD that should have written it.

    ``as_of`` is the load's ``finished_at`` — when the gap was actually
    measurable — and never the moment somebody asked. A holding with no
    completed load receipt carries ``as_of: null`` and says so in
    ``as_of_basis`` rather than borrowing the read's own clock, which is the
    same refusal every other absence kind makes.
    """
    loaded_at = _parse(holding.loaded_at)
    expires_at = (
        (loaded_at + timedelta(days=GAP_SHELF_LIFE_DAYS)) if loaded_at else None
    )
    return {
        "kind": HISTORY_GAP_KIND,
        "subject": subject,
        "since": holding.loaded_at,
        "window": window,
        "reason": reason,
        "as_of": holding.loaded_at,
        "as_of_basis": (
            "the completed load of this collection version"
            if holding.loaded_at
            else "no completed load is on record for this collection version"
        ),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "review": (
            None
            if expires_at
            else f"re-run scripts/load_collection.py {holding.collection_id}"
        ),
        "stale": bool(expires_at and expires_at < now),
        "proof": {
            "what_was_checked": (
                f"{what}, as recorded by the load of "
                f"{holding.collection_id} version "
                f"{holding.collection_version or '(unversioned)'}"
            ),
            "checked_at": holding.loaded_at,
            "ref": holding.load_id,
            "ref_kind": "collection_load" if holding.load_id else None,
        },
    }


# ---------------------------------------------------------------------------
# Coercions
# ---------------------------------------------------------------------------


def _jsonb(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return dict(value) if isinstance(value, Mapping) else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
