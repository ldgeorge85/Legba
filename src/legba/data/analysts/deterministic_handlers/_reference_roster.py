# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2-FIX(2) — THE ROSTER, THE ATTEMPT LEDGER AND THE ORDER THEY IMPLY.

WHAT WENT WRONG (D5, ops 2026-09-17). The scheduler ordered the roster by each
target's newest SUCCESSFUL reference — "most overdue first", where overdue is
measured from ``unit_references.built_at``. A build that fails writes no row,
so a failing target's overdue-ness does not move. Argentina
(``country_g20_ar``) is largely unfetchable to this lane — 3 of 16 pages usable
— and it has never produced a reference, which makes it permanently the MOST
overdue target on a 32-country roster. Two consecutive builds spent on it:
00:43Z burned 928 s and 4.5 M tokens for ``all_rejected``/0 committed, and the
05:20Z dry run stopped correctly at 304 s with ``no_commit``. The second one was
the D3 fix WORKING. It was also the proof that working was not enough: the
hourly tick would pick Argentina again at 06:43, at 07:43, and at every tick
after that, and the other 29 due targets would never be built at all.

THE FIX, AND WHY IT NEEDS NO MIGRATION. The ordering key stops being "last
SUCCESS" and becomes "last ATTEMPT", which is a fact the builder already
persists: every tick writes a receipt to ``analyst_outputs`` under
``analyst_id = 'reference_builder'``, and this module adds one compact,
bounded object to that receipt's ``data`` — ``attempts``, one entry per target
the run actually put on the plane. Reading the last N receipts back gives every
target's last attempt time, its outcome, and how many times in a row it has
failed. A jsonb key on a row the lane was already writing is the whole storage
design; there is no new table and no migration.

THREE RULES COME OUT OF IT:

  * **Order by last attempt, NULLS FIRST.** A target nobody has tried goes
    ahead of one that was tried an hour ago, whatever their reference ages say.
    Most-overdue is still the tie-break, so on a roster where nothing has been
    attempted the behaviour is exactly the old one.
  * **Back off after a failure.** ``no_commit`` and ``all_rejected`` make a
    target ineligible for :data:`DEFAULT_RETRY_BACKOFF_HOURS`. It is not
    dropped and not hidden: it stays in the receipt's due queue carrying the
    reason ``retry_backoff`` and the instant it comes back, so an operator
    reading the row can see exactly why it was skipped.
  * **Three strikes is a FLAG, never a deletion.** After three consecutive
    failures the target is named ``unbuildable_by_lane`` on the receipt and
    ``scripts/reference_topup_packet.py --candidates`` lists it. It keeps being
    retried at the backoff cadence forever: "this lane cannot fetch Argentina"
    is a statement about today's fetchers and today's licence classes, both of
    which change, and a target silently dropped from a roster is how a country
    disappears from a correctness programme without anyone deciding that.

WHAT IS NOT AN ATTEMPT. ``no_model`` and ``no_web`` are refusals BEFORE the
build: no search, no fetch, no model round, nothing spent. Recording them would
rotate the queue on the strength of a misconfiguration that affects every target
equally, and would push a perfectly buildable country to the back for a reason
that has nothing to do with it. They are not recorded, which also means that
while the web pack is ungranted the tick keeps returning instantly and costing
nothing at all.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ... import gazetteer as GZ
from ... import profile_constants as _constants
from ... import rosters as _rosters
from ... import subject as SUBJECT
from ..unit_names import label_name, parse_unit_label
from . import _reference_store as STORE

logger = logging.getLogger(__name__)

#: The ``analyst_outputs.analyst_id`` the builder's own receipts carry. It is
#: the descriptor's ``identity.id``, which is the sub-handler name.
RECEIPT_ANALYST_ID = "reference_builder"

#: How long a target that just FAILED stays ineligible, in hours.
#:
#: 24 h against a 7-day cadence: a target gets at most one build a day, so a
#: chronically failing country costs the lane one build in twenty-four rather
#: than one every hour, and the roster still cycles far faster than the cadence
#: needs. Set to 0 to disable the fence entirely — which is the only way to
#: MEASURE what it is doing, the same reason the domain blocklist honours
#: ``block_after: 0``.
DEFAULT_RETRY_BACKOFF_HOURS = 24

#: The env override.
#:
#: NOTE THE PRECEDENCE, which is the OPPOSITE of ``build_max_seconds`` and
#: deliberate. The two walls are volume knobs an operator sizes once and leaves
#: alone, so the descriptor wins there. This is an INCIDENT lever: the thing an
#: operator reaches for is "the lane is stuck on one country, let it move on",
#: at a moment when a descriptor PUT + re-register + fresh actor record is
#: exactly the ceremony they cannot afford. An env change and a recreate wins
#: over the descriptor here, and the receipt names which source it read.
RETRY_BACKOFF_ENV = "LEGBA_REFERENCE_RETRY_BACKOFF_HOURS"

#: Consecutive failures before a target is flagged ``unbuildable_by_lane``.
#: Three, because two is a coincidence a flaky search plane can produce on its
#: own and four wastes another day finding out.
UNBUILDABLE_AFTER_FAILURES = 3

#: How far back the attempt ledger is read. A tick that builds writes one
#: ``attempts`` entry, at most ``max_targets_per_run`` of them, ~5 a day at the
#: shipped cadence — so 90 days at 500 rows reads the whole history the lane has
#: and still cannot walk the table.
ATTEMPT_LOOKBACK_DAYS = 90
ATTEMPT_ROW_CAP = 500

#: Receipt entries in ``attempts``. One run builds ``max_targets_per_run``
#: targets (shipped: 1); the cap is the same "a finding body is not a log" rule
#: the rest of the receipt follows.
ATTEMPT_ITEM_CAP = 16

#: The build statuses that mean THE TARGET FAILED — the two the D3 fix split
#: apart, which are opposite diagnoses of the same zero. Both leave
#: ``unit_references`` untouched, which is exactly why ordering by last success
#: could never see them.
FAILED_STATUSES = frozenset({
    "no_commit", "all_rejected",
    # D6 — a commit that carried no development WHILE the loop's own page
    # manifest held in-window material. It is a failed build like the other two
    # (nothing written, the target must yield the hour to the next one) and it
    # is named separately because it points somewhere else entirely: not at the
    # plane, not at the fences, at the model.
    "empty_commit_with_material",
})

#: The statuses that mean a build RAN, whatever came out of it. ``duplicate``
#: is a success (the model rebuilt bytes already loaded) and
#: ``built_not_written`` is a dry run that produced a fenced reference; both
#: reset the strike counter. Anything not listed here never reached the plane.
ATTEMPTED_STATUSES = FAILED_STATUSES | {
    "built", "duplicate", "built_not_written",
}

#: Ordering reasons written onto every due-queue entry.
REASON_NEVER_ATTEMPTED = "never_attempted"
REASON_OLDEST_ATTEMPT = "oldest_attempt"
REASON_RETRY_BACKOFF = "retry_backoff"
REASON_NAMED = "named_by_operator"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# SQL — parameterised, SELECT-only
# ---------------------------------------------------------------------------

#: The live roster: targets whose dimension desks have produced recently. The
#: population is DERIVED from what the fleet actually reads, so a subject that
#: joins the read set joins this roster with no edit here and no list anywhere.
#:
#: INST-1 — THE ROSTER IS THE PROFILE'S, NOT THE COUNTRIES'. ``$4`` is
#: :func:`reference_roster_tags` (the scored roster tags plus the profile's
#: ``constants.reference_subject_tags``): a target whose live head is ACTIVE
#: and carries one of them is on the roster, over the units it actually
#: produced. A target with NO head descriptor at all is admitted as at the pin
#: (fail-open: the roster never silently loses a producing desk because its
#: descriptor row is missing); a head that is paused, draft or retired, or that
#: carries none of the tags, is not.
#: The head filter runs AFTER the aggregate — once per target, not once per
#: finding row (the body is TOASTed; per-row it is ~15k detoasts a tick).
ROSTER_SQL = """
SELECT r.target_id, r.dims
  FROM (
        SELECT target_id,
               array_agg(DISTINCT analyst_id ORDER BY analyst_id) AS dims
          FROM analyst_outputs
         WHERE kind = 'finding'
           AND analyst_id = ANY($1::text[])
           AND produced_at > $2::timestamptz - make_interval(days => $3::int)
         GROUP BY target_id
       ) AS r
  LEFT JOIN target_descriptors td
    ON td.descriptor_id = r.target_id AND td.is_head = TRUE
 WHERE td.descriptor_id IS NULL
    OR (COALESCE(td.state, 'active') = 'active'
        AND (td.body -> 'scope' -> 'tags') ?| $4::text[])
 ORDER BY r.target_id
"""

#: One target's live head tags — which units the grader EXPECTS a head from
#: (:func:`expected_units`).
TARGET_TAGS_SQL = """
SELECT ARRAY(SELECT jsonb_array_elements_text(td.body -> 'scope' -> 'tags')) AS tags
  FROM target_descriptors td
 WHERE td.is_head = TRUE
   AND td.descriptor_id = $1
"""

#: The target's declared subject fields and the gazetteer read, for the
#: reference's SUBJECT line: the subject ladder's own statements
#: (``legba.data.subject``, W10 SUBJECT-1).
TARGET_IDENTITY_SQL = SUBJECT.HEADS_SQL
ISO_NAMES_SQL = SUBJECT.GAZETTEER_SQL

#: THE ATTEMPT LEDGER, read out of the lane's OWN receipts.
#:
#: ``analyst_outputs.data`` holds the WHOLE ``FindingPayload`` dump — title,
#: body, tags, confidence, ``kind_marker`` and the payload's own free-form
#: ``data`` — so the receipt's ``data['attempts']`` lands one level deeper, at
#: ``data -> 'data' -> 'attempts'``. That nesting is the platform's, not this
#: lane's (``writes._insert_analyst_output`` dumps the model), and a test
#: persists a real receipt through ``write_finding`` and reads it back through
#: this statement so the two can never drift apart silently.
#:
#: Bounded three ways: the analyst id (indexed), a lookback window, and a row
#: cap. ``produced_at <= $2`` rather than ``now()`` so a replay pinned to a past
#: ``as_of`` sees the ledger as it stood then.
ATTEMPTS_SQL = """
SELECT ao.produced_at AS produced_at,
       ao.data -> 'data' -> 'attempts' AS attempts
  FROM analyst_outputs ao
 WHERE ao.analyst_id = $1
   AND ao.kind = 'finding'
   AND ao.produced_at <= $2
   AND ao.produced_at > $2::timestamptz - make_interval(days => $3::int)
   AND jsonb_typeof(ao.data -> 'data' -> 'attempts') = 'object'
   AND ao.data -> 'data' -> 'attempts' <> '{}'::jsonb
 ORDER BY ao.produced_at DESC, ao.id DESC
 LIMIT $4
"""


# ---------------------------------------------------------------------------
# The roster (INST-1: the profile's subjects, not the countries)
# ---------------------------------------------------------------------------


def reference_roster_tags() -> tuple[str, ...]:
    """The tags that put a subject on the CORRECTNESS roster: the profile's
    scored roster tags, then its ``constants.reference_subject_tags``, in
    declared order, once each."""
    return tuple(dict.fromkeys(
        (*_rosters.scored_roster_tags(), *_constants.reference_subject_tags())
    ))


def reference_units() -> tuple[str, ...]:
    """The units the reference builder references and the grader grades, in
    roster order: the scored subjects' units (``scored_subject_units``) plus
    every unit scoped to a ``reference_subject_tags`` leg. With no reference
    tag declared it IS ``scored_subject_units`` — the pin's set."""
    base = set(_rosters.scored_subject_units())
    extra = set(_constants.reference_subject_tags())
    return tuple(
        u for u in _rosters.unit_roster()
        if u in base or _rosters.terms_for(u).scope_tag in extra
    )


def units_for_tags(
    tags: Sequence[str], units: Sequence[str] | None = None
) -> tuple[str, ...]:
    """The units (of ``units``, default :func:`reference_units`) that fire on
    a subject carrying ``tags`` — the roster's own fan-out rule: an unscoped
    unit on any scored subject; a unit scoped to a roster LEG on that leg's
    subjects; a unit narrowed WITHIN the scored legs (a scope tag that is not
    a roster tag) on the scored subjects that carry it."""
    have = set(tags)
    scored = set(_rosters.scored_roster_tags())
    legs = set(_rosters.subject_roster_tags())
    on_scored = bool(have & scored)
    out: list[str] = []
    for unit in (reference_units() if units is None else units):
        scope = _rosters.terms_for(unit).scope_tag
        if scope is None:
            fires = on_scored
        elif scope in legs:
            fires = scope in have
        else:
            fires = on_scored and scope in have
        if fires:
            out.append(unit)
    return tuple(out)


def on_scored_leg(units: Sequence[str]) -> bool:
    """Does a subject that fires ``units`` sit on a SCORED leg — does it fire
    any fixed scorecard dimension? The floor-1 subject composition is expected
    exactly there."""
    return bool(set(units) & set(_rosters.scored_unit_roster()))


async def expected_units(
    conn: Any, target_id: str, units: Sequence[str]
) -> tuple[str, ...] | None:
    """The units of ``units`` a head is EXPECTED from on ``target_id`` — its
    live head's tags through :func:`units_for_tags`. ``None`` when the target
    has no readable head: the caller then expects every unit, the pin's rule.
    """
    try:
        row = await conn.fetchrow(TARGET_TAGS_SQL, target_id)
    except Exception as exc:  # noqa: BLE001 — degrade to the pin's rule
        logger.warning(
            "reference_roster.tags_unreadable target=%s err=%s", target_id, exc
        )
        return None
    if row is None:
        return None
    return units_for_tags([str(t) for t in (row["tags"] or [])], units)


async def resolve_roster(
    conn: Any,
    *,
    t0: datetime,
    dimensions: Sequence[str],
    roster_window_days: int,
    tags: Sequence[str] | None = None,
) -> dict[str, list[str]]:
    """``{target_id: [the dimensions THAT target actually carries]}``.

    Per-target rather than a constant list, because ``thin`` must mean
    something: a target with seven desks is thin on the seven it has, and
    banding an eighth dimension it does not read would manufacture a thin
    dimension that no live unit will ever be graded on.

    ``tags`` (default :func:`reference_roster_tags`) selects the subjects —
    see :data:`ROSTER_SQL`.
    """
    roster_tags = list(reference_roster_tags() if tags is None else tags)
    rows = await conn.fetch(
        ROSTER_SQL, list(dimensions), t0, int(roster_window_days), roster_tags
    )
    return {
        str(r["target_id"]): [str(d) for d in (r["dims"] or [])]
        for r in rows
    }


async def target_identity(
    conn: Any, target_ids: Sequence[str], *, detail: bool = False
) -> dict[str, dict[str, Any]]:
    """``{target_id: {"name": …, "code": …}}`` — the reference's subject.

    THE SUBJECT LADDER (``legba.data.subject``, W10 SUBJECT-1): the code is the
    descriptor's declared ``subject_code``, else its single ``scope.geo`` code,
    else the target id; the name is the gazetteer's for that code, else the
    descriptor's name, else the de-slugged id. A lane or a flow (several geo
    codes) is therefore keyed by its own id, where the first geo code used to
    name it after one riparian country, and a target whose descriptor fails to
    answer resolves through the profile's declared subjects and then to its id
    — never the id's last two characters.

    Degrades rather than failing: a reference for a target whose descriptor is
    mid-rename is still a reference, and refusing to build one over a display
    name would be the wrong trade.

    INST-1 — THE GAZETTEER RUNG IS THE PROFILE'S. The name rung reads the
    gazetteer only for a code that IS one of the subject's declared geo codes
    (where the pin filtered on a two-character code), and only when the
    profile's gazetteer declares a ``polities`` section; a profile that
    declares none reads it ABSENT (recorded by name, ``gazetteer.or_empty``)
    and every subject is named by its descriptor. ``detail=True`` adds the
    subject's ``kind``, whether it is a ``polity``, the ``noun`` the
    instruction calls it by and the ``query_name`` its searches substitute
    (:func:`subject_detail`).
    """
    try:
        rows = await conn.fetch(TARGET_IDENTITY_SQL, list(target_ids))
    except Exception as exc:  # noqa: BLE001 — degrade, never lose the run
        logger.warning("reference_builder.identity_failed err=%s", exc)
        rows = []
    codes = {
        str(row["target_id"]): SUBJECT.subject_code(row) for row in rows
    }
    polities = GZ.or_empty("polities", consumer="reference_roster")
    has_polities = bool(polities.universe or polities.curated)
    gazetteer: dict[str, str] = {}
    if rows and has_polities:
        try:
            gazetteer = await SUBJECT.gazetteer_names(conn, {
                codes[str(row["target_id"])] for row in rows
                if codes[str(row["target_id"])] in SUBJECT.subject_geo(row)
            })
        except Exception as exc:  # noqa: BLE001 — a nicer name is not worth a run
            logger.debug("reference_builder.iso_names_failed err=%s", exc)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        tid = str(row["target_id"])
        out[tid] = {
            "name": SUBJECT.subject_name(row, gazetteer=gazetteer),
            "code": codes[tid],
        }
        if detail:
            out[tid].update(
                subject_detail(row, out[tid], polity=codes[tid] in gazetteer)
            )
    for target_id in target_ids:
        tid = str(target_id)
        if tid not in out:
            subject = SUBJECT.resolve(tid)
            out[tid] = {"name": subject.name, "code": subject.code}
            if detail:
                out[tid].update(subject_detail(
                    subject, out[tid],
                    polity=has_polities and subject.code in subject.geo,
                ))
    return out


def subject_detail(
    target: Any, identity: Mapping[str, Any], *, polity: bool
) -> dict[str, Any]:
    """What the instruction and the query plan need beyond code and name.

    ``noun`` is ``country`` for a polity (a subject named by the gazetteer's
    polity rung — the exemplar's 32 desks, byte-identical to the pin's
    instruction); otherwise the TIER half of the subject's label (``Lane —
    Strait of Hormuz`` -> ``lane``), else ``subject``. ``query_name`` is the
    name a search substitutes for ``{subject}``: a polity's gazetteer name, or
    the NAME half of the label (``Strait of Hormuz``) — a query names the
    place, never the tier.
    """
    name = str(identity.get("name") or "")
    if polity:
        noun, query_name = "country", name
    else:
        tier = parse_unit_label(name).tier.strip().lower()
        noun = tier or "subject"
        query_name = label_name(name) or name
    return {
        "kind": SUBJECT.subject_kind(target),
        "polity": bool(polity),
        "noun": noun,
        "query_name": query_name,
    }


# ---------------------------------------------------------------------------
# The attempt ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    """One target's standing in the attempt ledger, folded over the receipts."""

    #: When the target was last put on the plane (its run's ``t0``).
    last_attempt_at: datetime | None = None
    #: The build status that attempt ended in.
    last_status: str = ""
    #: Failures in a row, counted back from the newest attempt until the first
    #: success. Zero when the last attempt succeeded.
    consecutive_failures: int = 0

    @property
    def failed(self) -> bool:
        return self.last_status in FAILED_STATUSES

    @property
    def unbuildable(self) -> bool:
        return self.consecutive_failures >= UNBUILDABLE_AFTER_FAILURES

    def as_record(self) -> dict[str, Any]:
        return {
            "last_attempt_at": (
                self.last_attempt_at.isoformat() if self.last_attempt_at else None
            ),
            "last_attempt_status": self.last_status or None,
            "consecutive_failures": int(self.consecutive_failures),
        }


def retry_backoff_hours(knob: Any = None) -> tuple[float, str]:
    """``(hours, source)`` — the env first, the descriptor knob second.

    Takes the KNOB'S VALUE rather than the option mapping, so the handler that
    owns the options is the module that reads ``options["retry_backoff_hours"]``
    — which is what the X-1 reachability sweep proves, and what keeps this
    module a scheduler library rather than a second reader of the catalog.

    See :data:`RETRY_BACKOFF_ENV` for why this precedence is inverted relative
    to the two build walls. A garbled env falls back rather than raising: a
    typo in an operator's ``.env`` must not take the lane down, and the source
    string on the receipt says which value was actually used.
    """
    raw = (os.getenv(RETRY_BACKOFF_ENV) or "").strip()
    if raw:
        try:
            hours = float(raw)
            if hours >= 0:
                return hours, RETRY_BACKOFF_ENV
            logger.warning(
                "reference_builder.backoff_negative env=%s raw=%r — ignoring",
                RETRY_BACKOFF_ENV, raw,
            )
        except ValueError:
            logger.warning(
                "reference_builder.backoff_unparseable env=%s raw=%r — ignoring",
                RETRY_BACKOFF_ENV, raw,
            )
    if knob is not None:
        try:
            hours = float(knob)
            if hours >= 0:
                return hours, "retry_backoff_hours"
        except (TypeError, ValueError):
            logger.warning(
                "reference_builder.backoff_option_unparseable raw=%r", knob
            )
    return float(DEFAULT_RETRY_BACKOFF_HOURS), "default"


def attempt_records(
    per_target: Sequence[Mapping[str, Any]], *, at: datetime
) -> dict[str, dict[str, Any]]:
    """The compact ``attempts`` object this run adds to its own receipt.

    One entry per target that actually reached the plane — see the module
    banner on why ``no_model``/``no_web`` are not attempts. Three scalar fields
    per target and a hard cap, because this object is read back by every future
    tick and a receipt that grows without bound would eventually be the reason
    the lane is slow.
    """
    out: dict[str, dict[str, Any]] = {}
    for target in per_target:
        status = str(target.get("status") or "")
        if status not in ATTEMPTED_STATUSES:
            continue
        target_id = str(target.get("target_id") or "").strip()
        if not target_id:
            continue
        out[target_id] = {
            "at": at.isoformat(),
            "status": status,
            "ok": status not in FAILED_STATUSES,
        }
    return dict(sorted(out.items())[:ATTEMPT_ITEM_CAP])


def fold_attempts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Attempt]:
    """Fold receipt rows (NEWEST FIRST) into one :class:`Attempt` per target.

    The newest row a target appears in sets its last attempt; the consecutive
    failure count walks backwards from there and stops at the first success, so
    a target that failed twice, built once and then failed once is on ONE
    strike, not three.
    """
    folded: dict[str, Attempt] = {}
    closed: set[str] = set()
    for row in rows:
        record_map = row if isinstance(row, Mapping) else dict(row)
        raw = record_map.get("attempts")
        blob = json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {})
        if not isinstance(blob, Mapping):
            continue
        produced_at = record_map.get("produced_at")
        for target_id, record in blob.items():
            if not isinstance(record, Mapping):
                continue
            key = str(target_id)
            if key in closed:
                continue
            status = str(record.get("status") or "")
            failed = status in FAILED_STATUSES
            when = _instant(record.get("at")) or _instant(produced_at)
            if key not in folded:
                folded[key] = Attempt(
                    last_attempt_at=when,
                    last_status=status,
                    consecutive_failures=1 if failed else 0,
                )
                if not failed:
                    closed.add(key)
                continue
            if failed:
                prior = folded[key]
                folded[key] = Attempt(
                    last_attempt_at=prior.last_attempt_at,
                    last_status=prior.last_status,
                    consecutive_failures=prior.consecutive_failures + 1,
                )
            else:
                closed.add(key)
    return folded


async def load_attempts(
    conn: Any,
    *,
    t0: datetime,
    analyst_id: str = RECEIPT_ANALYST_ID,
    lookback_days: int = ATTEMPT_LOOKBACK_DAYS,
    row_cap: int = ATTEMPT_ROW_CAP,
) -> dict[str, Attempt]:
    """Read the attempt ledger out of the lane's own receipts.

    DEGRADES, never raises: with no ledger the ordering falls back to the old
    most-overdue-first behaviour, which is a worse schedule but still a
    schedule. A builder that refused to run because it could not read its own
    history would have turned a reporting problem into an outage.
    """
    try:
        rows = await conn.fetch(
            ATTEMPTS_SQL, analyst_id, t0, int(lookback_days), int(row_cap)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("reference_builder.attempts_unreadable err=%s", exc)
        return {}
    return fold_attempts([dict(r) for r in rows])


def apply_outcomes(
    attempts: Mapping[str, Attempt],
    per_target: Sequence[Mapping[str, Any]],
    *,
    at: datetime,
) -> dict[str, Attempt]:
    """The ledger as it stands AFTER this run's builds.

    The receipt has to name the third strike on the tick that earns it, not one
    tick later — an operator reading "built no reference" for the third time
    running should see ``unbuildable_by_lane`` on that same row rather than
    having to count back through three receipts themselves.
    """
    updated = dict(attempts)
    for target in per_target:
        status = str(target.get("status") or "")
        if status not in ATTEMPTED_STATUSES:
            continue
        target_id = str(target.get("target_id") or "").strip()
        if not target_id:
            continue
        prior = updated.get(target_id, Attempt())
        failed = status in FAILED_STATUSES
        updated[target_id] = Attempt(
            last_attempt_at=at,
            last_status=status,
            consecutive_failures=(
                prior.consecutive_failures + 1 if failed else 0
            ),
        )
    return updated


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------


def order_due(
    entries: Sequence[Mapping[str, Any]],
    attempts: Mapping[str, Attempt],
    *,
    t0: datetime,
    backoff_hours: float = 0.0,
) -> list[dict[str, Any]]:
    """THE SCHEDULER. Pure, so it can be argued with a fixture roster.

    ``entries`` are the cadence-due targets (newest reference older than the
    cadence, or none) carrying ``target_id`` and ``age_days``. Out comes the
    same set, annotated and ORDERED:

      1. eligible targets, by ``last_attempt_at ASC NULLS FIRST``, then most
         overdue, then target id — so a target nobody has tried goes first, and
         after that the one waiting longest since its last build attempt;
      2. then the backed-off ones, soonest-to-return first. They stay in the
         list because the receipt has to be able to say WHY a target that is
         plainly overdue was not picked.

    Nothing is ever removed. ``eligible`` is a field, not a filter, and the
    caller selects from the head of the eligible run.
    """
    window = timedelta(hours=max(0.0, float(backoff_hours)))
    annotated: list[dict[str, Any]] = []
    for raw in entries:
        entry = dict(raw)
        target_id = str(entry.get("target_id") or "")
        attempt = attempts.get(target_id, Attempt())
        entry.update(attempt.as_record())
        entry["unbuildable_by_lane"] = attempt.unbuildable
        retry_after: datetime | None = None
        if (
            window
            and attempt.failed
            and attempt.last_attempt_at is not None
        ):
            candidate = attempt.last_attempt_at + window
            if candidate > t0:
                retry_after = candidate
        entry["eligible"] = retry_after is None
        entry["retry_after"] = retry_after.isoformat() if retry_after else None
        if retry_after is not None:
            entry["reason"] = REASON_RETRY_BACKOFF
            entry["_sort_after"] = retry_after
        elif attempt.last_attempt_at is None:
            entry["reason"] = REASON_NEVER_ATTEMPTED
        else:
            entry["reason"] = REASON_OLDEST_ATTEMPT
        annotated.append(entry)

    def _eligible_key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
        raw_at = entry.get("last_attempt_at")
        when = _instant(raw_at)
        age = entry.get("age_days")
        return (
            0 if when is None else 1,
            when or _EPOCH,
            0 if age is None else 1,
            -float(age or 0.0),
            str(entry.get("target_id") or ""),
        )

    eligible = sorted(
        (e for e in annotated if e["eligible"]), key=_eligible_key
    )
    waiting = sorted(
        (e for e in annotated if not e["eligible"]),
        key=lambda e: (e["_sort_after"], str(e.get("target_id") or "")),
    )
    for entry in waiting:
        entry.pop("_sort_after", None)
    return [*eligible, *waiting]


async def due_targets(
    conn: Any,
    roster: Mapping[str, Sequence[str]],
    *,
    t0: datetime,
    cadence_days: int,
    attempts: Mapping[str, Attempt] | None = None,
    backoff_hours: float = 0.0,
) -> list[dict[str, Any]]:
    """The roster with the ones not yet due removed, in :func:`order_due` order.

    This is the whole scheduler. There is no stagger table and no per-country
    cron: a target is DUE when its newest reference is older than the cadence
    (or when it has none) — that part is unchanged and is a statement about the
    PRODUCT, not about the lane's luck. What changed in R2-FIX(2) is which due
    target the run takes: the one waiting longest since it was last ATTEMPTED,
    not since it last succeeded. Ordering by success meant a target that cannot
    succeed held the front of the queue for ever.

    A roster that grows gets served in turn; a roster that shrinks needs no
    cleanup; a run that is skipped delays a country by one tick rather than
    losing its slot.
    """
    cutoff = t0 - timedelta(days=int(cadence_days))
    out: list[dict[str, Any]] = []
    for target_id in sorted(roster):
        row = await conn.fetchrow(STORE.LATEST_REFERENCE_SQL, target_id)
        built_at = row["built_at"] if row is not None else None
        if built_at is not None and built_at > cutoff:
            continue
        out.append({
            "target_id": target_id,
            "dimensions": list(roster[target_id]),
            "last_built_at": built_at.isoformat() if built_at else None,
            "age_days": (
                round((t0 - built_at).total_seconds() / 86400.0, 2)
                if built_at else None
            ),
        })
    return order_due(out, attempts or {}, t0=t0, backoff_hours=backoff_hours)


def named_targets(
    explicit: Sequence[str],
    roster: Mapping[str, Sequence[str]],
    *,
    dimensions: Sequence[str],
    attempts: Mapping[str, Attempt] | None = None,
) -> list[dict[str, Any]]:
    """Operator-named targets, annotated like a due entry but never gated.

    A named target BYPASSES both the cadence and the backoff, because naming a
    target is an operator saying "build this one now" and a lane that answered
    "not for another nineteen hours" would be obeying its own scheduler over
    the person running it. The attempt history still travels on the entry, so
    the receipt can say the target is on two strikes while it builds it anyway.
    """
    ledger = attempts or {}
    out: list[dict[str, Any]] = []
    for target_id in explicit:
        attempt = ledger.get(target_id, Attempt())
        entry: dict[str, Any] = {
            "target_id": target_id,
            "dimensions": list(roster.get(target_id) or dimensions),
            "last_built_at": None,
            "age_days": None,
            "eligible": True,
            "retry_after": None,
            "reason": REASON_NAMED,
            "unbuildable_by_lane": attempt.unbuildable,
        }
        entry.update(attempt.as_record())
        out.append(entry)
    return out


def unbuildable_targets(attempts: Mapping[str, Attempt]) -> list[str]:
    """Targets at or past the strike limit, sorted. Never a removal list."""
    return sorted(t for t, a in attempts.items() if a.unbuildable)


def queue_line(entry: Mapping[str, Any]) -> str:
    """One due-queue entry rendered for the receipt body."""
    reason = str(entry.get("reason") or "")
    bits = [f"{entry.get('target_id')}: {reason}"]
    if reason == REASON_RETRY_BACKOFF:
        bits.append(f"until {entry.get('retry_after')}")
    last = entry.get("last_attempt_at")
    if last:
        bits.append(f"last attempt {last} ({entry.get('last_attempt_status')})")
    failures = int(entry.get("consecutive_failures") or 0)
    if failures:
        bits.append(f"{failures} consecutive failure(s)")
    if entry.get("unbuildable_by_lane"):
        bits.append("UNBUILDABLE_BY_LANE")
    age = entry.get("age_days")
    bits.append("never built" if age is None else f"reference {age}d old")
    return ", ".join(bits)


def _instant(value: Any) -> datetime | None:
    """A tz-aware instant from a datetime or an ISO string; ``None`` if neither."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            return STORE.iso(value)
        except ValueError:
            return None
    return None


__all__ = [
    "ATTEMPTED_STATUSES",
    "ATTEMPTS_SQL",
    "ATTEMPT_ITEM_CAP",
    "ATTEMPT_LOOKBACK_DAYS",
    "ATTEMPT_ROW_CAP",
    "Attempt",
    "DEFAULT_RETRY_BACKOFF_HOURS",
    "FAILED_STATUSES",
    "ISO_NAMES_SQL",
    "TARGET_TAGS_SQL",
    "REASON_NAMED",
    "REASON_NEVER_ATTEMPTED",
    "REASON_OLDEST_ATTEMPT",
    "REASON_RETRY_BACKOFF",
    "RECEIPT_ANALYST_ID",
    "RETRY_BACKOFF_ENV",
    "ROSTER_SQL",
    "TARGET_IDENTITY_SQL",
    "UNBUILDABLE_AFTER_FAILURES",
    "apply_outcomes",
    "attempt_records",
    "due_targets",
    "expected_units",
    "fold_attempts",
    "load_attempts",
    "named_targets",
    "on_scored_leg",
    "order_due",
    "queue_line",
    "reference_roster_tags",
    "reference_units",
    "resolve_roster",
    "retry_backoff_hours",
    "subject_detail",
    "target_identity",
    "unbuildable_targets",
    "units_for_tags",
]
