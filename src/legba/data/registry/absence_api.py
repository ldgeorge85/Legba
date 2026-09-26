# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/absence?scope=<target_id>`` — typed absence as ONE named thing.

Seven things in this platform are the same primitive, and until now they were
four vocabularies on four surfaces: an ABSENCE with a SCOPE, a KIND, a LIVENESS
PROOF and a SHELF LIFE. This route names the primitive once and answers it for
one desk.

THE CLOSED VOCABULARY (:data:`ABSENCE_KINDS`, one line each on the wire under
``kinds``). Every item carries exactly one:

  * ``not_collected`` — nothing covers the subject for this desk. Today: a
    bounded unit with no read on record here at all. The SOURCE half of this
    kind ("no source covers this subject for this desk") is SEAMS #61: a desk's
    roster is derived from observed in-scope production over ``roster_days``,
    so this route never asserts a coverage gap from a roster it knows is
    production-derived rather than curated.
  * ``collected_but_silent`` — a covering source exists AND IS HEALTHY (it is
    polling, without recent hard errors) and carried nothing for this desk
    inside the window. Silent-but-healthy is its own kind, deliberately: it is
    a fact about the WORLD's quiet, not about a broken pipe, and folding it
    into staleness is how a quiet desk gets read as a broken one.
  * ``source_stale`` — the producer that measures the subject is past its own
    cadence budget or failing. Two producers reach this: a roster source whose
    in-scope production is over budget AND whose polling is unhealthy, and a
    bounded unit whose latest read is older than its own cadence with grace.
  * ``searched_found_nothing`` — the external audit searched and nothing in the
    results decided the claim (``external_grades.verdict = 'NOT_FOUND'``). The
    proof carries WHAT was searched: provider, status, liveness, result count.
  * ``search_failed`` — the search itself did not answer: the rung was
    unreachable, blocked by robots, or over budget
    (``verdict = 'UNCHECKED'`` with its recorded ``unchecked_reason``). NEVER
    conflated with "nothing found" — "we looked and saw nothing" and "we could
    not look" are different facts about the world and about us.
  * ``below_floor`` — evidence exists and did not clear the floor: the banded
    scorecard SAW this dimension and refused to band it
    (``band = 'insufficient-evidence'``). ``reason = 'no-finding'`` is
    deliberately NOT this kind — that is the card's own window seeing nothing,
    which ``not_collected`` / ``source_stale`` own.
  * ``layer_declared_absent`` — Program 6 L0's aperture declaration
    (``desk_apertures.declared = 'absent'``, migration 0214), with the
    operator's own reason carried verbatim. The code never infers ``absent``; a
    layer nobody has looked at is ``unmeasured`` and is not an absence.
  * ``history_gap`` (7g-2) — the only kind about the PAST rather than the
    present: a curated COLLECTION's manifest declares a (series, subject) for
    this desk over measured provider years, and the ``observations`` table
    does not hold them. Composed off the SAME reader the era coverage map
    serves (:mod:`legba.data.registry.collections_coverage`), so a hole shown
    on ``/v3/collections/coverage`` and a hole typed here are one measurement.
    Its proof is a LOAD receipt (``collection_loads``, ``ref_kind:
    collection_load``) rather than a read, because a gap is the absence of a
    row and the honest thing to point at is the run that should have written
    it. A year the PROVIDER never published is NOT this kind — the manifest
    records that in the provider's own words — and a desk no loaded holding
    names is not this kind either: it goes to ``not_measured`` naming which
    desks the holdings do cover, because a gap in a holding that does not
    exist is not an absence this platform can type.

EVERY ABSENCE IS STAMPED IN TIME
--------------------------------
An absence with no clock is a claim about the present made out of the past. So
each item carries three time fields and nothing is stamped ``now()``:

  * ``as_of`` — WHEN THE ABSENCE WAS MEASURED: the run, receipt or scan instant
    it came from (the grading run's ``graded_at``, the banding card's
    ``produced_at``, the unit's last run, the source's last in-scope signal,
    the operator's ``valid_from``). ``null`` only when no such instant exists
    at all, and then ``as_of_basis`` says so in words rather than substituting
    the read's own clock.
  * ``as_of_basis`` — which instant that is, named, so nobody has to guess.
  * ``expires_at`` — when it stops being current: the next scheduled run of the
    thing that measured it, computed from that producer's OWN declared cadence
    through :func:`legba.data.registry.source_freshness.next_fire_after`. A
    declared-absent layer has no schedule and carries ``expires_at: null`` with
    ``review: "map revision"``.
  * ``stale`` — ``expires_at`` is in the past: the measurement that produced
    this absence was due to be repeated and has not been. A stale item is LAST
    KNOWN, NOT RE-CHECKED, and the reader surface says exactly that. An old
    absence must never read as a current one.

THE PROOF IS THE POINT
----------------------
Every item also carries ``proof: {what_was_checked, checked_at, ref,
ref_kind}``. That is the difference between a typed absence and a blank: a
blank says nothing was shown; an absence says WHAT WAS LOOKED AT, WHEN, and
WHERE THE RECORD OF THAT LOOK LIVES. ``ref_kind`` names what the ref IS
(``finding`` / ``scorecard`` / ``source`` / ``map_version``) so a reader
surface offers a link only where one resolves, rather than inferring a table
from the shape of an id. An item whose proof cannot be constructed is not
emitted.

WHAT IS NOT MEASURED SAYS SO
----------------------------
Each kind reads independently and defensively. A kind whose source cannot be
read for this scope (the table is not deployed, the desk declares no geo, the
query fails) is OMITTED from ``absences`` and NAMED in ``not_measured`` with a
reason, at HTTP 200 — the ``external_audit_api`` / ``system_escalations``
idiom. A kind that read cleanly and found nothing contributes an empty list and
is NOT in ``not_measured``: "nothing absent" and "not checked" are different
answers, and this route never collapses them.

No count is invented. There is no total, no rate, and no ``0`` standing in for
an unread source.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + ``.source_freshness``.
Nothing from ``legba.data.analysts`` or ``legba.runtime`` — this module ships in
the slim registry image (``tests/data_pkg/test_absence_api.py`` pins it).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from . import source_freshness
from .api import RegistryAPIDeps, require_bearer
from .collections_coverage import history_gap_items, read_coverage

logger = logging.getLogger(__name__)

ABSENCE_ROUTE_VERSION = "2026-09/k5"

_ROUTE = "/absence"

#: The closed kind vocabulary and its published one-line meanings. This dict IS
#: the wire contract's ``kinds`` block — a reader never has to find the docs to
#: learn what a kind asserts, and the glossary entry lists the same seven.
ABSENCE_KINDS: dict[str, str] = {
    "not_collected": (
        "nothing covers this subject for this desk — no read has ever been "
        "produced for it here"
    ),
    "collected_but_silent": (
        "a covering source exists and is healthy (polling, no recent hard "
        "errors) and carried nothing for this desk in the window"
    ),
    "source_stale": (
        "the producer that measures this subject is past its own cadence "
        "budget or failing"
    ),
    "searched_found_nothing": (
        "the external audit searched and nothing in the results decided the "
        "claim; the proof carries what was searched"
    ),
    "search_failed": (
        "the search itself did not answer — unreachable, blocked, or over "
        "budget; never the same as finding nothing"
    ),
    "below_floor": (
        "evidence exists and did not clear the floor: the banded scorecard saw "
        "this dimension and refused to band it"
    ),
    "layer_declared_absent": (
        "an operator declared this information layer absent for this desk, "
        "with a reason (Program 6 aperture)"
    ),
    # 7g-2 — the EIGHTH kind. The seven above are all about the PRESENT: a
    # source that carried nothing, a unit that did not run, a dimension that
    # did not band. This one is about the PAST, and it is a different question
    # — a curated collection's manifest declares which years a provider holds
    # for this desk, and this kind is the years that declaration names and the
    # `observations` table does not carry. Deliberately NOT collapsed into
    # `not_collected`: that kind means nothing covers the subject at all,
    # while this one means something does, said so, and the row is missing
    # anyway — and the proof is a LOAD receipt, not a read.
    #
    # What it is NOT: a year the PROVIDER never published. The manifest
    # records those in the provider's own words and this kind never restates
    # them as ours (`collections_coverage.history_gap_items`).
    "history_gap": (
        "a curated collection declares this series-and-subject for this desk "
        "and the observations table does not hold it; the proof is the load "
        "that should have written it"
    ),
}

#: The nine bounded reasoning units (docs/ANALYSIS.md §4.1), in the display
#: order the desk gap strip uses. MIRRORS ``GAP_STRIP_UNITS`` in
#: ``legba-ui-v3/src/lib/gapStripModel.ts`` — that strip is the reader surface
#: this route is drilled from, and a unit in one list and not the other would
#: mean a cell that cannot be explained (or an explanation with no cell). The
#: two lists live in two languages and cannot share a literal, so
#: ``tests/data_pkg/test_absence_api.py`` PARSES the TypeScript and fails on
#: drift in either direction — the unit ids, and the grace multiple below.
BOUNDED_UNITS: tuple[str, ...] = (
    "leadership_transition",
    "energy_security",
    "escalation",
    "narrative_coordination",
    "internal_stability",
    "military_posture",
    "economic_coercion",
    "proliferation_watch",
    "disruption_status",
)

#: Grace over a unit's cadence interval before its latest read reads silent.
#: Equal to ``GAP_STRIP_GRACE_MULTIPLE`` in ``gapStripModel.ts`` (pinned by the
#: drift test) — these are clock-scheduled fires, so the multiple is small.
UNIT_GRACE_MULTIPLE = 1.5

#: The analyst whose run PRODUCES the banded card — the measurer behind
#: ``below_floor`` and behind the scan that makes a ``not_collected`` unit's
#: ``as_of`` a real instant rather than a null.
BANDING_ANALYST_ID = "scorecard_producer"

#: The analyst whose run produces the external grades — the measurer behind
#: ``searched_found_nothing`` and ``search_failed``.
AUDIT_ANALYST_ID = "standing_auditor"

#: The scorecard reason that is NOT a floor failure (see the banner): the
#: card's own window saw no finding, which the unit kinds answer properly.
_NOT_A_FLOOR_FAILURE = "no-finding"

_INSUFFICIENT_BAND = "insufficient-evidence"

#: A declared-absent layer is revised by a map revision, not by a clock.
_APERTURE_REVIEW = "map revision"

#: The route's published defaults, named so the SECOND caller of
#: :func:`read_absences` (the desk brief's ``absences`` block, composed
#: server-side in ``export_absences.py``) reads the desk on exactly the
#: windows the route does. Two surfaces on two different windows would
#: disagree about the same desk's silence and neither would be wrong.
DEFAULT_WINDOW_HOURS = 336
DEFAULT_ROSTER_DAYS = 30
DEFAULT_HEALTH_DAYS = 7
DEFAULT_LIMIT_PER_KIND = 25


# ---------------------------------------------------------------------------
# SQL — each statement bounded to the scope. Live EXPLAINs for every one are in
# the lane report; none is a sequential scan.
# ---------------------------------------------------------------------------

#: The audit's two absence verdicts in ONE read, so the route never pays twice
#: for the same index walk. ``idx_external_grades_window
#: (graded_at DESC, population, verdict)`` drives it; ``target_id`` filters the
#: window rather than leading it, which is right at this table's size.
#: ``UNCHECKABLE`` is deliberately absent: a claim with no world truth-maker was
#: decided before any query and is not an absence of evidence at all.
_AUDIT_SQL = """
SELECT claim_key, claim_text, analyst_id, verdict, unchecked_reason, graded_at,
       graded_output_id::text AS graded_output_id,
       search_provider, search_status, search_liveness, search_degraded,
       coalesce(array_length(source_urls, 1), 0) AS n_urls
  FROM public.external_grades
 WHERE target_id = $1
   AND verdict IN ('NOT_FOUND', 'UNCHECKED')
   AND graded_at > $2
 ORDER BY graded_at DESC
 LIMIT $3
"""

#: The live banded card for the desk — the same head selection
#: ``v3_api.eval_country_scorecard`` makes, so this route and that panel can
#: never disagree about which card is current.
_SCORECARD_SQL = """
SELECT DISTINCT ON (target_id) id::text AS id, produced_at, data
  FROM public.analyst_outputs
 WHERE kind = 'scorecard'
   AND superseded_by IS NULL
   AND target_id = $1
 ORDER BY target_id, produced_at DESC, id DESC
"""

#: ``uq_desk_apertures_open (target_id, layer)`` covers this exactly.
_APERTURE_SQL = """
SELECT layer, declared, reason, map_version, valid_from
  FROM public.desk_apertures
 WHERE target_id = $1
   AND declared = 'absent'
   AND valid_until IS NULL
 ORDER BY layer
"""

#: The latest read per unit for this desk. Mirrors what the gap strip's own
#: ``/findings?analyst_id_in=…&target_id=…`` read yields (no ``superseded_by``
#: predicate — that route has none either), collapsed server-side.
_UNIT_LATEST_SQL = """
SELECT DISTINCT ON (analyst_id)
       analyst_id, id::text AS id, produced_at
  FROM public.analyst_outputs
 WHERE kind = 'finding'
   AND target_id = $1
   AND analyst_id = ANY($2::text[])
 ORDER BY analyst_id, produced_at DESC, id DESC
"""

#: Every measuring analyst's OWN declared cadence, from its live descriptor
#: head — the nine units plus the two producers whose runs stamp the audit and
#: banding kinds. Never one shared constant: a producer moved off its schedule
#: changes only its own ``expires_at``.
_ANALYST_CADENCE_SQL = """
SELECT descriptor_id, body->'cadence'->>'fallback_schedule' AS schedule
  FROM public.analyst_descriptors
 WHERE is_head
   AND descriptor_id = ANY($1::text[])
"""

#: The desk's declared geo — the same per-target discriminator ``/signals``
#: resolves a ``target_id`` filter through.
_TARGET_GEO_SQL = """
SELECT body->'scope'->'geo' AS geo
  FROM public.target_descriptors
 WHERE descriptor_id = $1 AND is_head
"""

#: The desk's SOURCE ROSTER, and each roster source's freshest signal IN THIS
#: DESK'S GEO. Bounded by ``roster_days`` deliberately: a source that produced
#: nothing for this desk inside the window is not on its roster any more, and
#: the window is published on every item's proof rather than hidden in code.
#: The plan is a BitmapAnd of ``signals_geo_gin`` and ``signals_fetched_at_idx``.
_DESK_ROSTER_SQL = """
SELECT source_id, max(fetched_at) AS last_at
  FROM public.signals
 WHERE geo && $1::text[]
   AND fetched_at > $2
 GROUP BY source_id
"""

#: The two freshness INPUTS the ``source_quality`` view publishes, read straight
#: off the descriptor heads. The view itself is deliberately not read here: it
#: merges four organs and aggregates the WHOLE ``signals`` table to publish a
#: GLOBAL last-signal instant — both expensive (~235 ms measured) and the wrong
#: instant, since this route grades the desk-scoped one.
_SOURCE_HEADS_SQL = """
SELECT descriptor_id AS source_id,
       state AS declared_state,
       body->'cadence'->'schedule'->>'raw' AS cadence_raw
  FROM public.source_descriptors
 WHERE is_head
   AND descriptor_id = ANY($1::text[])
"""

#: IS THE SOURCE ITSELF HEALTHY? The poll ledger answers it without touching
#: ``signals`` again: a source that is still polling, without hard errors, is
#: ALIVE and its desk-level quiet is a fact about the world
#: (``collected_but_silent``); one whose polls stopped or are erroring is
#: ``source_stale``. ``source_poll_outcomes_source_time_idx`` covers this — the
#: probe measured ~5 ms on top of the roster scan.
_SOURCE_HEALTH_SQL = """
SELECT source_id,
       max(occurred_at) FILTER (WHERE outcome <> 'error') AS last_ok_poll_at,
       count(*) FILTER (WHERE outcome = 'error') AS error_polls
  FROM public.source_poll_outcomes
 WHERE source_id = ANY($1::text[])
   AND occurred_at > $2
 GROUP BY source_id
"""


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class AbsenceProof(BaseModel):
    """What makes an item a typed absence rather than a blank.

    ``what_was_checked`` is a sentence a reader can audit; ``checked_at`` is
    when that look happened; ``ref`` points at the row that holds the look, and
    ``ref_kind`` says WHAT that ref is (``finding`` / ``scorecard`` /
    ``source`` / ``map_version`` / ``collection_load``) so a reader surface can
    offer a link only where one resolves, instead of guessing from the shape
    of an id and shipping a click that 404s.
    """

    what_was_checked: str
    checked_at: Optional[str] = None
    ref: Optional[str] = None
    ref_kind: Optional[str] = None


class AbsenceItem(BaseModel):
    """One absence, of exactly one ``kind`` from :data:`ABSENCE_KINDS`.

    Extent: ``since`` when an instant is on record, ``window`` when the honest
    answer is a bounded period rather than a point.

    Time: ``as_of`` is when the absence was MEASURED (never the read's own
    clock) and ``as_of_basis`` names which instant that is; ``expires_at`` is
    when it stops being current — the next scheduled run of the thing that
    measured it — and ``stale`` is that instant already being past, i.e. LAST
    KNOWN, NOT RE-CHECKED. ``review`` replaces a schedule where there is none
    (a declared-absent layer is revised by a map revision, not by a clock).
    """

    kind: str
    subject: str
    since: Optional[str] = None
    window: Optional[str] = None
    reason: str
    as_of: Optional[str] = None
    as_of_basis: str
    expires_at: Optional[str] = None
    review: Optional[str] = None
    stale: bool = False
    proof: AbsenceProof


class AbsenceOut(BaseModel):
    """``absences`` empty means nothing is absent — a real answer.
    ``not_measured`` names the kinds that could not be read for this scope, and
    why; a kind in neither position was read and found nothing.

    ``read_at`` is when this ROUTE read, and is never an item's ``as_of``: an
    absence is stamped with when it was measured, not with when somebody asked.
    """

    version: str = ABSENCE_ROUTE_VERSION
    scope: str
    read_at: str
    kinds: dict[str, str] = Field(default_factory=lambda: dict(ABSENCE_KINDS))
    absences: list[AbsenceItem] = Field(default_factory=list)
    not_measured: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure projection — no DB, no clock beyond what is passed in. Tested directly.
# ---------------------------------------------------------------------------


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _parse(value: Any) -> Optional[datetime]:
    """An instant from a column or an ISO string, or ``None``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _quote(text: Any, limit: int = 180) -> str:
    """A claim quoted into a reason line, bounded and ellipsised honestly."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def schedule_stamp(
    *, cadence_raw: Optional[str], as_of: Optional[datetime], now: datetime
) -> tuple[Optional[str], bool]:
    """``(expires_at, stale)`` for an absence measured at ``as_of`` by a
    producer on ``cadence_raw``.

    The next fire is computed FROM ``as_of`` (not from now), so an absence whose
    measurer was due to repeat and did not comes back ``stale`` — which is the
    whole point: the alternative is an old absence quietly reading as current.
    A producer with no parsable cadence yields ``(None, False)``: no schedule,
    no expiry, and never a guessed one.
    """
    if as_of is None:
        return None, False
    nxt = source_freshness.next_fire_after(cadence_raw, as_of)
    if nxt is None:
        return None, False
    return _iso(nxt), nxt < now


def audit_item(row: Any, *, cadence_raw: Optional[str], now: datetime) -> AbsenceItem:
    """One external grade as an absence — ``searched_found_nothing`` when the
    search answered and decided nothing, ``search_failed`` when it did not
    answer at all. The proof names the search plane's own self-report, because
    a degraded search and a live one that found nothing are different facts.
    """
    provider = row["search_provider"] or "unrecorded provider"
    liveness = row["search_liveness"] or "unverified"
    search_status = row["search_status"] or "unrecorded"
    degraded = " DEGRADED;" if row["search_degraded"] else ""
    n_urls = int(row["n_urls"] or 0)
    graded_at = _parse(row["graded_at"])
    found_nothing = row["verdict"] == "NOT_FOUND"
    failure = str(row["unchecked_reason"] or "").strip() or "reason not recorded"
    expires_at, stale = schedule_stamp(
        cadence_raw=cadence_raw, as_of=graded_at, now=now
    )
    return AbsenceItem(
        kind="searched_found_nothing" if found_nothing else "search_failed",
        subject=str(row["claim_key"]),
        since=_iso(graded_at),
        reason=(
            (
                "the external audit searched and nothing in the results decided "
                "this claim: "
            )
            if found_nothing
            else f"the search did not answer ({failure}) for this claim: "
        )
        + f"“{_quote(row['claim_text'])}”",
        as_of=_iso(graded_at),
        as_of_basis="the external audit's grading run for this claim",
        expires_at=expires_at,
        stale=stale,
        proof=AbsenceProof(
            what_was_checked=(
                f"open-web search via {provider} (status={search_status}, "
                f"liveness={liveness};{degraded} {n_urls} result(s) returned"
                + (", none decisive" if found_nothing else f"; {failure}")
                + f") against the {row['analyst_id']} read"
            ),
            checked_at=_iso(graded_at),
            ref=row["graded_output_id"],
            ref_kind="finding",
        ),
    )


def below_floor_items(
    *,
    card_id: str,
    card_produced_at: Optional[datetime],
    dimensions: Any,
    cadence_raw: Optional[str],
    now: datetime,
) -> list[AbsenceItem]:
    """The banded card's refused dimensions.

    ``reason='no-finding'`` is excluded on purpose (see the banner): the card's
    own window seeing nothing is a staleness fact the unit kinds own.
    """
    if not isinstance(dimensions, dict):
        return []
    out: list[AbsenceItem] = []
    for name in sorted(dimensions):
        band = dimensions[name]
        if not isinstance(band, dict):
            continue
        if band.get("band") != _INSUFFICIENT_BAND:
            continue
        reason_code = str(band.get("reason") or "").strip()
        if reason_code == _NOT_A_FLOOR_FAILURE:
            continue
        critic = band.get("critic_score")
        critic_text = (
            f"folded critic score {float(critic):.2f}"
            if isinstance(critic, (int, float)) and not isinstance(critic, bool)
            else "critic score not measured"
        )
        measured = _parse(band.get("produced_at")) or card_produced_at
        expires_at, stale = schedule_stamp(
            cadence_raw=cadence_raw, as_of=measured, now=now
        )
        out.append(
            AbsenceItem(
                kind="below_floor",
                subject=str(name),
                since=_iso(measured),
                reason=(
                    "the banded scorecard saw this dimension's evidence and "
                    f"refused to band it: {reason_code or 'reason not recorded'}"
                ),
                as_of=_iso(measured),
                as_of_basis="the banded-scorecard run that graded this desk",
                expires_at=expires_at,
                stale=stale,
                proof=AbsenceProof(
                    what_was_checked=(
                        f"scorecard card {card_id}: the dimension's basis is "
                        f"empty and its band reads '{_INSUFFICIENT_BAND}' "
                        f"({critic_text})"
                    ),
                    checked_at=_iso(card_produced_at),
                    ref=card_id,
                    ref_kind="scorecard",
                ),
            )
        )
    return out


def aperture_item(row: Any) -> AbsenceItem:
    """An operator's aperture declaration. ``reason`` is the operator's own
    curated sentence, carried verbatim — this route never paraphrases it.

    No schedule measures this one: it changes when the layer map is revised, so
    ``expires_at`` is null and ``review`` says what would supersede it. It is
    therefore never ``stale``: a declaration does not go out of date on a clock.
    """
    declared_at = _parse(row["valid_from"])
    return AbsenceItem(
        kind="layer_declared_absent",
        subject=str(row["layer"]),
        since=_iso(declared_at),
        reason=(
            str(row["reason"] or "").strip()
            or "declared absent (no reason recorded)"
        ),
        as_of=_iso(declared_at),
        as_of_basis="the operator's aperture declaration for this layer",
        expires_at=None,
        review=_APERTURE_REVIEW,
        stale=False,
        proof=AbsenceProof(
            what_was_checked=(
                "the desk's aperture declaration for this layer, curated by an "
                f"operator under map_version {row['map_version']} "
                "(the loader never infers 'absent')"
            ),
            checked_at=_iso(declared_at),
            ref=str(row["map_version"]),
            ref_kind="map_version",
        ),
    )


def unit_item(
    *,
    unit: str,
    latest_id: Optional[str],
    latest_at: Optional[datetime],
    cadence_raw: Optional[str],
    interval_minutes: Optional[float],
    card_id: Optional[str],
    card_produced_at: Optional[datetime],
    now: datetime,
) -> Optional[AbsenceItem]:
    """One unit's absence, or ``None`` when the unit is current.

    Two kinds, deliberately distinct. NO READ ON RECORD is ``not_collected``:
    it carries a ``window`` rather than a ``since`` (there is no instant to
    point at, and inventing one is the exact lie this route exists to refuse),
    and its ``as_of`` is the banding run's scan of this desk when one exists —
    the last time anything looked here and found no read for this unit. A read
    past its cadence is ``source_stale``: the producer is overdue, and the
    absence is stamped with that producer's last run.
    """
    cadence_text = cadence_raw or "no schedule declared"
    if latest_at is None:
        expires_at, stale = schedule_stamp(
            cadence_raw=cadence_raw, as_of=card_produced_at, now=now
        )
        return AbsenceItem(
            kind="not_collected",
            subject=unit,
            window="no read on record for this desk",
            reason="this bounded unit has never produced a read for this desk",
            as_of=_iso(card_produced_at),
            as_of_basis=(
                "the banded-scorecard run's own scan of this desk's units"
                if card_produced_at is not None
                else "no run and no scan on record for this unit on this desk"
            ),
            expires_at=expires_at,
            stale=stale,
            proof=AbsenceProof(
                what_was_checked=(
                    f"the latest kind='finding' row for analyst '{unit}' on "
                    "this desk — none exists; the unit's declared cadence is "
                    f"'{cadence_text}'"
                ),
                checked_at=_iso(card_produced_at),
                ref=card_id,
                ref_kind="scorecard" if card_id else None,
            ),
        )
    if interval_minutes is None or interval_minutes <= 0:
        # No honest threshold exists, so no honest staleness verdict does
        # either. The unit is not reported silent against a guessed budget.
        return None
    age_minutes = (now - latest_at).total_seconds() / 60.0
    threshold = interval_minutes * UNIT_GRACE_MULTIPLE
    if age_minutes <= threshold:
        return None
    expires_at, stale = schedule_stamp(
        cadence_raw=cadence_raw, as_of=latest_at, now=now
    )
    return AbsenceItem(
        kind="source_stale",
        subject=unit,
        since=_iso(latest_at),
        reason=(
            f"the latest read is {age_minutes / 60.0:.1f}h old, past this "
            f"unit's own {interval_minutes / 60.0:.1f}h cadence "
            f"(×{UNIT_GRACE_MULTIPLE} grace = {threshold / 60.0:.1f}h)"
        ),
        as_of=_iso(latest_at),
        as_of_basis="this unit's last run for this desk",
        expires_at=expires_at,
        stale=stale,
        proof=AbsenceProof(
            what_was_checked=(
                f"the latest kind='finding' row for analyst '{unit}' on this "
                f"desk, against the cadence '{cadence_text}' declared on the "
                "unit's own descriptor head"
            ),
            checked_at=_iso(latest_at),
            ref=latest_id,
            ref_kind="finding",
        ),
    )


def source_item(
    *,
    source_id: str,
    grade: str,
    healthy: bool,
    health_note: str,
    last_at: Optional[datetime],
    budget_minutes: Optional[int],
    cadence_raw: Optional[str],
    geo: Iterable[str],
    roster_days: int,
    now: datetime,
) -> Optional[AbsenceItem]:
    """One roster source past its own cadence budget FOR THIS DESK.

    The KIND turns on the source's own health, which is the operator's
    distinction and the one a desk reader actually needs: a source that is
    still polling cleanly and simply carried nothing here is
    ``collected_but_silent`` — a fact about the world's quiet. A source whose
    polls stopped or are erroring is ``source_stale`` — a fact about our pipe.
    Folding the two together is how a quiet desk gets read as a broken one.

    ``ok`` is not an absence; ``empty`` cannot arise here (a roster source
    produced at least one in-scope signal in the window by construction); and
    ``ungraded`` is not an absence either — it means no honest budget exists,
    and a source with no parsable cadence must never be reported silent against
    a budget nobody declared.
    """
    if grade not in ("stale", "warn") or last_at is None or not budget_minutes:
        return None
    age_minutes = (now - last_at).total_seconds() / 60.0
    scope_geo = ", ".join(sorted(geo)) or "no geo"
    expires_at, stale = schedule_stamp(
        cadence_raw=cadence_raw, as_of=last_at, now=now
    )
    return AbsenceItem(
        kind="collected_but_silent" if healthy else "source_stale",
        subject=source_id,
        since=_iso(last_at),
        reason=(
            (
                "this source is healthy and carried nothing for this desk: "
                if healthy
                else "this source is late or failing: "
            )
            + f"{age_minutes / 60.0:.1f}h since its last in-scope signal, "
            f"against a {budget_minutes / 60.0:.1f}h budget derived from its "
            f"own cadence ({health_note})"
        ),
        as_of=_iso(last_at),
        as_of_basis="this source's most recent signal in this desk's scope",
        expires_at=expires_at,
        stale=stale,
        proof=AbsenceProof(
            what_was_checked=(
                f"signals from '{source_id}' whose geo overlaps [{scope_geo}] "
                f"in the last {roster_days}d, graded by the source-quality "
                f"freshness rule against cadence "
                f"'{cadence_raw or 'undeclared'}' (budget = interval × "
                f"{source_freshness.GRACE_MULTIPLE}, floor "
                f"{source_freshness.MIN_BUDGET_MINUTES}min; 'warn' past "
                f"×{source_freshness.WARN_MULTIPLE}); the poll ledger says "
                f"{health_note}"
            ),
            checked_at=_iso(last_at),
            ref=source_id,
            ref_kind="source",
        ),
    )


def poll_health(row: Any, *, health_days: int) -> tuple[bool, str]:
    """``(healthy, note)`` from the poll ledger for one roster source.

    Healthy means the pipe is working: a successful poll inside the health
    window and no hard errors in it. A source with NO ledger row at all is not
    called healthy — an unobserved poller is not an observed-working one.
    """
    if row is None:
        return False, f"no poll recorded in the last {health_days}d"
    errors = int(row["error_polls"] or 0)
    last_ok = row["last_ok_poll_at"]
    if last_ok is None:
        return False, f"no successful poll in the last {health_days}d ({errors} errors)"
    if errors:
        return False, f"{errors} failed poll(s) in the last {health_days}d"
    return True, f"polling cleanly (last successful poll {_iso(last_ok)})"


def _geo_list(raw: Any) -> list[str]:
    """The desk's ``scope.geo``, however asyncpg handed the jsonb back."""
    if raw is None:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [str(g) for g in value if isinstance(g, str) and g.strip()]


def _unreadable(kind: str, what: str, exc: BaseException) -> str:
    """The ``not_measured`` sentence for a kind whose source would not read.

    The exception CLASS is named, never the message: a driver message can carry
    a query fragment, and this string lands on a reader surface.
    """
    logger.info(
        "v3.absence.kind_unreadable kind=%s err_class=%s", kind, type(exc).__name__
    )
    return f"{kind}: {what} could not be read ({type(exc).__name__})"




# ---------------------------------------------------------------------------
# The READER — one desk's absences off an open connection.
#
# Module-level, not a router closure, because ONE other surface has to reach
# the same answer: the desk brief's ``absences`` block is composed SERVER-side
# (``export_absences.py``) so the printed brief and the exported JSON cannot
# disagree with each other or with this route. A second implementation over
# the same tables is how two surfaces start telling a reader two different
# stories about the same silence, so there is exactly one.
# ---------------------------------------------------------------------------


async def _read_card(conn: Any, target: str) -> Optional[Any]:
    return await conn.fetchrow(_SCORECARD_SQL, target)


async def _audit_kinds(
    conn: Any,
    target: str,
    cutoff: datetime,
    cap: int,
    cadence: Mapping[str, Optional[str]],
    now: datetime,
) -> list[AbsenceItem]:
    rows = await conn.fetch(_AUDIT_SQL, target, cutoff, cap)
    schedule = cadence.get(AUDIT_ANALYST_ID)
    return [audit_item(r, cadence_raw=schedule, now=now) for r in rows]


async def _below_floor(
    card: Optional[Any],
    cap: int,
    cadence: Mapping[str, Optional[str]],
    now: datetime,
    not_measured: list[str],
) -> list[AbsenceItem]:
    if card is None:
        not_measured.append(
            "below_floor: no banded scorecard has been computed for this "
            "desk yet"
        )
        return []
    raw = card["data"]
    payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    payload = payload if isinstance(payload, dict) else {}
    bands = ((payload.get("data") or {}).get("bands")) or {}
    return below_floor_items(
        card_id=card["id"],
        card_produced_at=_parse(card["produced_at"]),
        dimensions=bands.get("dimensions"),
        cadence_raw=cadence.get(BANDING_ANALYST_ID),
        now=now,
    )[:cap]


async def _declared_absent(conn: Any, target: str, cap: int) -> list[AbsenceItem]:
    rows = await conn.fetch(_APERTURE_SQL, target)
    return [aperture_item(r) for r in rows[:cap]]


async def _unit_kinds(
    conn: Any,
    target: str,
    cap: int,
    card: Optional[Any],
    cadence: Mapping[str, Optional[str]],
    now: datetime,
) -> list[AbsenceItem]:
    latest = {
        r["analyst_id"]: r
        for r in await conn.fetch(_UNIT_LATEST_SQL, target, list(BOUNDED_UNITS))
    }
    card_id = card["id"] if card is not None else None
    card_at = _parse(card["produced_at"]) if card is not None else None
    out: list[AbsenceItem] = []
    for unit in BOUNDED_UNITS:
        row = latest.get(unit)
        cadence_raw = cadence.get(unit)
        item = unit_item(
            unit=unit,
            latest_id=row["id"] if row else None,
            latest_at=_parse(row["produced_at"]) if row else None,
            cadence_raw=cadence_raw,
            interval_minutes=source_freshness.cadence_interval_minutes(cadence_raw),
            card_id=card_id,
            card_produced_at=card_at,
            now=now,
        )
        if item is not None:
            out.append(item)
    return out[:cap]


async def _source_kinds(
    conn: Any,
    target: str,
    cap: int,
    roster_days: int,
    roster_cutoff: datetime,
    health_days: int,
    health_cutoff: datetime,
    now: datetime,
    not_measured: list[str],
) -> list[AbsenceItem]:
    geo_row = await conn.fetchrow(_TARGET_GEO_SQL, target)
    geo = _geo_list(geo_row["geo"]) if geo_row else []
    if not geo:
        not_measured.append(
            "collected_but_silent / source_stale (sources): this desk "
            "declares no scope.geo, so it has no geo-resolved source "
            "roster to grade"
        )
        return []
    roster = await conn.fetch(_DESK_ROSTER_SQL, geo, roster_cutoff)
    ids = [r["source_id"] for r in roster if r["source_id"]]
    heads = {
        r["source_id"]: r
        for r in (await conn.fetch(_SOURCE_HEADS_SQL, ids) if ids else [])
    }
    health = {
        r["source_id"]: r
        for r in (
            await conn.fetch(_SOURCE_HEALTH_SQL, ids, health_cutoff) if ids else []
        )
    }
    out: list[AbsenceItem] = []
    for r in roster:
        head = heads.get(r["source_id"])
        if head is None:
            # An unregistered producer has no declared cadence to be late
            # against. Not an absence, and never a guessed one.
            continue
        budget = source_freshness.derive_budget_minutes(head["cadence_raw"])
        last_at = _parse(r["last_at"])
        healthy, note = poll_health(
            health.get(r["source_id"]), health_days=health_days
        )
        item = source_item(
            source_id=r["source_id"],
            grade=source_freshness.grade_freshness(
                state=head["declared_state"],
                age_seconds=(
                    int((now - last_at).total_seconds())
                    if last_at is not None
                    else None
                ),
                budget_minutes=budget,
            ),
            healthy=healthy,
            health_note=note,
            last_at=last_at,
            budget_minutes=budget,
            cadence_raw=head["cadence_raw"],
            geo=geo,
            roster_days=roster_days,
            now=now,
        )
        if item is not None:
            out.append(item)
    # Worst first — the longest silence is the one to read.
    out.sort(key=lambda i: i.since or "")
    return out[:cap]


async def read_absences(
    conn: Any,
    *,
    scope: str,
    now: datetime,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    roster_days: int = DEFAULT_ROSTER_DAYS,
    health_days: int = DEFAULT_HEALTH_DAYS,
    limit_per_kind: int = DEFAULT_LIMIT_PER_KIND,
) -> AbsenceOut:
    """Every typed absence for one desk, read off an OPEN connection.

    Each kind reads independently and defensively: a kind that raises is named
    in ``not_measured`` and the rest of the answer still ships, because a desk
    whose audit table is unreachable still has units, sources and apertures
    worth reading, and a 500 would say nothing about any of them.

    ``now`` is passed in rather than taken here so the CALLER's read instant
    stamps the whole answer — the export route composes one document at one
    instant, and a second, later clock inside this function would put two
    different "now"s on one page.
    """
    target = (scope or "").strip()
    cutoff = now - timedelta(hours=window_hours)
    roster_cutoff = now - timedelta(days=roster_days)
    health_cutoff = now - timedelta(days=health_days)
    absences: list[AbsenceItem] = []
    not_measured: list[str] = []

    # The measuring analysts' own cadences — one read, shared by every kind
    # that stamps an `expires_at`. A failure here degrades the STAMPS (no
    # schedule ⇒ no expiry, never a guessed one), not the absences, so it is
    # not a `not_measured` entry for any kind.
    cadence: dict[str, Optional[str]] = {}
    try:
        cadence = {
            r["descriptor_id"]: r["schedule"]
            for r in await conn.fetch(
                _ANALYST_CADENCE_SQL,
                list(BOUNDED_UNITS) + [BANDING_ANALYST_ID, AUDIT_ANALYST_ID],
            )
        }
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "v3.absence.cadence_unreadable err_class=%s", type(exc).__name__
        )

    card: Optional[Any] = None
    card_readable = True
    try:
        card = await _read_card(conn, target)
    except Exception as exc:  # noqa: BLE001
        card_readable = False
        not_measured.append(_unreadable("below_floor", "the banded scorecard", exc))

    try:
        absences.extend(
            await _audit_kinds(conn, target, cutoff, limit_per_kind, cadence, now)
        )
    except Exception as exc:  # noqa: BLE001 — name it, never fake it
        not_measured.append(
            _unreadable(
                "searched_found_nothing / search_failed", "external_grades", exc
            )
        )
    if card_readable:
        try:
            absences.extend(
                await _below_floor(card, limit_per_kind, cadence, now, not_measured)
            )
        except Exception as exc:  # noqa: BLE001
            not_measured.append(
                _unreadable("below_floor", "the banded scorecard", exc)
            )
    try:
        absences.extend(await _declared_absent(conn, target, limit_per_kind))
    except Exception as exc:  # noqa: BLE001
        not_measured.append(
            _unreadable("layer_declared_absent", "desk_apertures", exc)
        )
    try:
        absences.extend(
            await _unit_kinds(conn, target, limit_per_kind, card, cadence, now)
        )
    except Exception as exc:  # noqa: BLE001
        not_measured.append(
            _unreadable(
                "not_collected / source_stale (units)",
                "the unit reads for this desk",
                exc,
            )
        )
    try:
        # 7g-2 — HISTORY_GAP. Composed off the SAME reader the era coverage
        # map serves (`collections_coverage.read_coverage`), so a hole shown
        # on `/v3/collections/coverage` and a hole typed here are literally
        # the same measurement. A desk no loaded holding names is NOT a gap:
        # `history_gap_items` returns it as a `not_measured` sentence naming
        # the desks the holdings do cover.
        gaps, gap_notes = history_gap_items(
            await read_coverage(conn, scope=target, now=now), now=now
        )
        absences.extend(
            AbsenceItem.model_validate(g) for g in gaps[:limit_per_kind]
        )
        not_measured.extend(gap_notes)
    except Exception as exc:  # noqa: BLE001
        not_measured.append(
            _unreadable(
                "history_gap",
                "the loaded collections' coverage for this desk",
                exc,
            )
        )
    try:
        absences.extend(
            await _source_kinds(
                conn,
                target,
                limit_per_kind,
                roster_days,
                roster_cutoff,
                health_days,
                health_cutoff,
                now,
                not_measured,
            )
        )
    except Exception as exc:  # noqa: BLE001
        not_measured.append(
            _unreadable(
                "collected_but_silent / source_stale (sources)",
                "the desk's source roster",
                exc,
            )
        )

    return AbsenceOut(
        scope=target,
        read_at=now.isoformat(),
        absences=absences,
        not_measured=not_measured,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_absence_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["v3"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=AbsenceOut)
    async def absence(
        request: Request,
        scope: str = Query(
            ...,
            description="The desk (target_id) every absence is bounded to.",
        ),
        window_hours: int = Query(default=DEFAULT_WINDOW_HOURS, ge=1, le=2160),
        roster_days: int = Query(default=DEFAULT_ROSTER_DAYS, ge=1, le=365),
        health_days: int = Query(default=DEFAULT_HEALTH_DAYS, ge=1, le=90),
        limit_per_kind: int = Query(default=DEFAULT_LIMIT_PER_KIND, ge=1, le=200),
        principal: str = Depends(require_bearer),
    ) -> AbsenceOut:
        """Every typed absence for one desk, each with its kind, proof and clock.

        ``window_hours`` bounds the external-audit read; ``roster_days`` bounds
        which sources count as this desk's roster; ``health_days`` bounds the
        poll ledger that decides healthy-but-silent from stale; and
        ``limit_per_kind`` caps each read so one noisy kind cannot bury the
        others. All four are published on the items they shape.
        """
        target = (scope or "").strip()
        if not target:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "query parameter 'scope' is required and must name a desk "
                    "(target_id): GET /v3/absence?scope=<target_id>"
                ),
            )

        now = datetime.now(timezone.utc)
        d = _get_deps(request)
        async with d.descriptor_registry.pg.acquire() as conn:
            return await read_absences(
                conn,
                scope=target,
                now=now,
                window_hours=window_hours,
                roster_days=roster_days,
                health_days=health_days,
                limit_per_kind=limit_per_kind,
            )

    return router


__all__ = [
    "ABSENCE_KINDS",
    "ABSENCE_ROUTE_VERSION",
    "AUDIT_ANALYST_ID",
    "BANDING_ANALYST_ID",
    "BOUNDED_UNITS",
    "DEFAULT_HEALTH_DAYS",
    "DEFAULT_LIMIT_PER_KIND",
    "DEFAULT_ROSTER_DAYS",
    "DEFAULT_WINDOW_HOURS",
    "UNIT_GRACE_MULTIPLE",
    "AbsenceItem",
    "AbsenceOut",
    "AbsenceProof",
    "aperture_item",
    "audit_item",
    "below_floor_items",
    "build_absence_router",
    "poll_health",
    "read_absences",
    "schedule_stamp",
    "source_item",
    "unit_item",
]
