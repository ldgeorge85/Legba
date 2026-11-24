# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``due_and_absent`` sub-handler — what was expected by a date, and what
this collection holds no record of. No model call. Lane E-1 (2026-11-14).

WHAT COUNTS AS AN EXPECTATION. Only a row that carries an explicit date in
the record; nothing is inferred and no model reads anything:

* ``forecast_question`` — a SEALED question of the exogenous forecast ledger
  (migration 0242), still ``open`` after ``due_at + grace_hours``, with no
  active resolution and no decisive (yes / no / void) proposal. When the
  resolver job has read it, its newest proposal's ``searched`` scope is what
  was looked for.
* ``acute_forecast`` — an acute hazard pilot forecast (0047) whose window
  closed more than ``acute_grace_hours`` ago with no graded outcome (a row
  voided from grading is never due).
* ``scheduled_event`` — a calendar row (0240) whose end (its start, for a
  point event) plus ``scheduled_tolerance_hours`` has passed, with no
  canonical signal collected since its start whose title or summary carries
  the row's own declared ``subject_keys``. A row with no subject keys
  declares nothing to look for and is counted ``not_checkable``, never
  reported.
* ``inquiry_expectation`` — an inquiry ledger row (0215) still ``open`` after
  its ``resolves_by``.
* ``series_period`` — a LIVE series (observations origin ``live``, 0246) whose
  next period is overdue: the newest period start held, plus the series'
  declared ``period``, plus its source's declared ``config.lag_days``, plus
  ``series_tolerance`` for that period, has passed. This is the publisher's
  expected item; the liveness watchdog's cadence stall is about the POLL. The
  two are kept apart: every series row says how the source's polls fared in
  the window, and when they were silent or failing the row says the watchdog
  owns it and no ``due_and_absent`` alert is raised.

Rows of the record that were looked at and NOT used: ``events`` (no row
carries a start later than its own creation: nothing future-dated), the
``valid_until`` / ``expires_at`` columns (a shelf life, not an expectation),
``indicator_flips`` (no deadline).

THE WORDING RULE. An absence in this collection is not proof of absence in
the world. Every line says "no row or signal matching it has been collected"
and names what was looked for, which sources would have carried it, and
whether those sources were themselves silent or failing in the window; it
never says that something did not happen. :func:`check_wording` refuses the
banned phrasings (:data:`BANNED_PHRASES`) in every title, body and line the
detector builds — a violation raises, so it can never reach the record.

DEDUPE. The finding's ``data.ledger`` holds every expectation currently due
and absent. The next run reads its own newest finding's ledger and reports a
row only when its state changes: ``newly_overdue`` (first seen due and
absent), ``resolved_late`` (a previously reported row now has its record) or
``voided`` (the row was withdrawn, retired or voided in its own table). A run
with no change is trace-only: no feed row, the ledger stays on the last real
finding. A type whose read failed carries its ledger rows unchanged and is
named UNMEASURED; a failed read is never a measured zero.

ALERTS. Every reported change raises one alert of kind ``due_and_absent``
through the one alert path (``raise_alert``) at ``low`` — a newly overdue row
opens an episode, a late resolution or a void closes it. Page rule
2026-11-14/1 names no entry for the kind, so every raise is LOGGED, never
paged.

Read at ``GET /api/v1/findings?analyst_id=due_and_absent`` with the
``unverified — structural`` badge (``STRUCTURAL_VERIFY_EXEMPT_ANALYSTS``).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from . import due_and_absent_reads as R
from .forecast_acute import HAZARD_SEVERE_SOURCES, VOID_PREFIX

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "due_and_absent"
METHOD_VERSION = "due_and_absent/2026-11.1"
ALERT_KIND = "due_and_absent"

FORECAST = "forecast_question"
ACUTE = "acute_forecast"
SCHEDULED = "scheduled_event"
INQUIRY = "inquiry_expectation"
SERIES = "series_period"
TYPES: tuple[str, ...] = (FORECAST, ACUTE, SCHEDULED, INQUIRY, SERIES)

NEWLY_OVERDUE = "newly_overdue"
RESOLVED_LATE = "resolved_late"
VOIDED = "voided"
TRANSITIONS: tuple[str, ...] = (NEWLY_OVERDUE, RESOLVED_LATE, VOIDED)

DEFAULT_ACUTE_GRACE_HOURS = 24
DEFAULT_SCHEDULED_TOLERANCE_HOURS = 48
#: A calendar row is first read when its due moment lies within this many
#: days; the signal scan reaches back at most this plus a week (the scan's
#: cost is ~0.3 s per day of signals, measured on live 2026-11-14).
DEFAULT_SCHEDULED_LOOKBACK_DAYS = 14
DEFAULT_SCHEDULED_MIN_KEY_HITS = 2
DEFAULT_SERIES_TOLERANCE: tuple[str, ...] = ("day=3", "week=3", "month=7", "quarter=14", "year=30")
DEFAULT_MAX_ROWS_PER_TYPE = 500
#: The farthest back a source-health window reaches.
HEALTH_WINDOW_MAX_DAYS = 30

#: A declared series period → its length in days (the observations CHECK
#: vocabulary, migration 0246).
PERIOD_DAYS: Mapping[str, int] = {"day": 1, "week": 7, "month": 31, "quarter": 92, "year": 366}

ABSENCE_SENTENCE = "No row or signal matching it has been collected."
DISCLAIMER = (
    "An absence in this collection is not proof of absence in the world: each line says what "
    "this collection holds as of {as_of}, what was looked for, which sources would have carried "
    "it, and whether those sources were themselves silent or failing in the window."
)

#: Phrasings that claim something about the world from an absence in the
#: collection. Refused in everything the detector writes.
BANNED_PHRASES: tuple[str, ...] = (
    "did not happen", "didn't happen", "has not happened", "hasn't happened", "never happened",
    "did not occur", "didn't occur", "has not occurred", "hasn't occurred", "never occurred",
    "failed to happen", "failed to occur", "did not take place", "never took place",
    "was not held", "no such event", "is confirmed absent",
)

_HEALTH_WORDS: Mapping[str, str] = {
    "silent": "silent (no poll recorded in the window)",
    "failing": "failing (every poll in the window errored)",
    "partly_failing": "partly failing",
    "polling_clean": "polling clean",
}


class WordingViolation(ValueError):
    """A detector text claimed an absence in the world (the wording rule)."""


#: Text quoted from a row of the record (a calendar title, an inquiry's own
#: words) sits between these marks; the wording rule binds the detector's own
#: words, never what it quotes.
_OPEN, _CLOSE = "‘", "’"
_QUOTED = re.compile(f"{_OPEN}[^{_CLOSE}]*{_CLOSE}")


def quote(text: Any) -> str:
    """Quote record text between the marks (inner marks flattened)."""
    flat = " ".join(str(text or "").split()).replace(_OPEN, "'").replace(_CLOSE, "'")
    return f"{_OPEN}{flat}{_CLOSE}"


def check_wording(text: str) -> str:
    """Return ``text`` unchanged, or raise :class:`WordingViolation`.

    Quoted record text (:func:`quote`) is not the detector's claim and is not
    scanned; everything else is."""
    low = " ".join(_QUOTED.sub(" ", str(text or "")).lower().split())
    for phrase in BANNED_PHRASES:
        if phrase in low:
            raise WordingViolation(
                f"due_and_absent wording rule: {phrase!r} claims an absence in the world; "
                "say 'no row or signal matching it has been collected'")
    return text


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0), tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            return _utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _iso(value: Any) -> str | None:
    v = _utc(value)
    return v.isoformat(timespec="seconds") if v is not None else None


def _day(value: Any) -> str:
    v = _utc(value)
    return v.strftime("%Y-%m-%d %H:%MZ") if v is not None else "an unknown time"


def _span(delta: timedelta) -> str:
    secs = max(0, int(delta.total_seconds()))
    days, rem = divmod(secs, 86400)
    hours = rem // 3600
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h" if hours else f"{max(1, rem // 60)} min"


def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _exp(type_: str, ref_kind: str, ref_id: str, *, key: str | None = None, what: str,
         by: datetime, state: str, looked_for: str, sources: Sequence[str] | None,
         subject: tuple[str, str], window_from: datetime | None, met_at: Any = None,
         void_reason: str | None = None, note: str | None = None,
         carrier: str | None = None, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "key": key or f"{type_}:{ref_id}", "type": type_, "ref_kind": ref_kind, "ref_id": ref_id,
        "what": what, "by": by, "state": state, "met_at": _utc(met_at), "void_reason": void_reason,
        "looked_for": looked_for, "sources": list(sources) if sources is not None else None,
        "subject_kind": subject[0], "subject_id": subject[1], "window_from": window_from,
        "note": note, "carrier": carrier, "extra": dict(extra or {}),
    }


# ---------------------------------------------------------------------------
# Classification, one function per expectation type (pure)
# ---------------------------------------------------------------------------


def describe_searched(searched: Any) -> str | None:
    """The resolver's own record of what it searched, in words (pure)."""
    s = _as_dict(searched)
    if not s:
        return None
    if s.get("series"):
        return (f"series {s.get('series')} from {s.get('source')} on {s.get('on')}: "
                f"{s.get('observations_held', 0)} observations held, newest {s.get('newest_held') or 'none'}")
    parts = _as_dict(s.get("parts"))
    lines = []
    for name, raw in parts.items():
        p = _as_dict(raw)
        terms = ", ".join(str(t) for t in (p.get("prefilter_all") or [])) or "no prefilter"
        lines.append(
            f"{name}: {p.get('corpus') or 'signals'} fetched {p.get('fetched_from') or '?'} .. "
            f"{p.get('fetched_to') or '?'} matching all of [{terms}], {p.get('rows_matched', 0)} "
            f"matched of {p.get('signals_in_scope', 0)} in scope")
    return "; ".join(lines) or None


def classify_forecast_question(row: Mapping[str, Any]) -> dict[str, Any]:
    due = _utc(row["due_at"])
    grace = int(row.get("grace_hours") or 0)
    by = due + timedelta(hours=grace)
    res = row.get("resolution_outcome")
    met_times = [t for t in (
        row.get("resolved_at") if res in ("yes", "no") else None,
        row.get("decisive_proposal_at"), row.get("legacy_proposal_at"),
    ) if t is not None]
    state, void_reason, met_at = "absent", None, None
    if row.get("status") == "void" or res == "void":
        state, void_reason = "voided", "voided in the forecast ledger"
    elif met_times or row.get("status") == "resolved":
        state, met_at = "met", (min(_utc(t) for t in met_times) if met_times else None)
    searched = describe_searched(row.get("proposal_searched"))
    looked_for = ("an active forecast_resolutions row, or a yes / no / void proposal in "
                  "forecast_resolution_records or forecast_resolution_proposals, for this question")
    note = None
    if row.get("proposal_outcome"):
        looked_for += (f"; the resolver's newest reading ({row['proposal_outcome']}, "
                       f"{_day(row.get('proposal_at'))}) searched: {searched or 'scope not recorded'}")
    else:
        note = "the resolver job has recorded no reading of this question"
    src = _as_dict(row.get("proposal_searched")).get("source")
    slug = str(row.get("slug") or row["id"])
    return _exp(
        FORECAST, "forecast_questions", str(row["id"]),
        what=(f"a resolution or proposed resolution of the sealed forecast question {quote(slug)} "
              f"({row.get('resolver_kind')}), due {_day(due)} with {grace} h grace"),
        by=by, state=state, met_at=met_at, void_reason=void_reason, looked_for=looked_for,
        sources=[str(src)] if src else None, subject=("question", slug),
        window_from=_utc(row.get("window_start")) or due - timedelta(days=7), note=note,
        extra={"slug": slug, "proposal_id": row.get("proposal_id")},
    )


def classify_acute_forecast(row: Mapping[str, Any], *, grace_hours: int) -> dict[str, Any]:
    ws, we = _utc(row["window_start"]), _utc(row["window_end"])
    by = we + timedelta(hours=grace_hours)
    marked = str(row.get("resolved_by") or "")
    state, void_reason, note = "absent", None, None
    if marked.startswith(VOID_PREFIX):
        state, void_reason = "voided", f"withdrawn from grading ({marked})"
    elif row.get("resolved_outcome") is not None:
        state = "met"
    elif marked:
        note = f"the resolver marked the row {marked}"
    region = str(row.get("region") or "?")
    srcs = list(HAZARD_SEVERE_SOURCES)
    return _exp(
        ACUTE, "acute_forecasts", str(row["id"]),
        what=(f"a graded outcome for the acute hazard forecast {region} / {row.get('event_class')} "
              f"over [{_day(ws)}, {_day(we)})"),
        by=by, state=state, met_at=row.get("resolved_at") if state == "met" else None,
        void_reason=void_reason,
        looked_for=(f"a graded outcome (resolved_outcome) on this row, which the exogenous resolver "
                    f"writes from signals of {', '.join(srcs)} geo-overlapping {region} inside the window"),
        sources=srcs, subject=("question", f"acute:{region}:{ws.date().isoformat()}"),
        window_from=ws, note=note, extra={"region": region},
    )


def classify_scheduled_event(row: Mapping[str, Any], match: Mapping[str, Any] | None, *,
                             tolerance_hours: int, min_key_hits: int) -> dict[str, Any]:
    start = _utc(row["starts_at"])
    end = _utc(row.get("ends_at")) or start
    by = end + timedelta(hours=tolerance_hours)
    keys = R.fold_keys(row.get("subject_keys") or [])
    need = min(min_key_hits, len(keys))
    state, void_reason, met_at = "absent", None, None
    if row.get("retired_at") is not None:
        state, void_reason = "voided", f"retired in the calendar {_day(row['retired_at'])}"
    elif not keys:
        state = "not_checkable"
    elif match is not None:
        state, met_at = "met", match.get("fetched_at")
    span = _day(start) + (f" to {_day(row['ends_at'])}" if row.get("ends_at") else "")
    scope = str(row.get("scope") or "global")
    return _exp(
        SCHEDULED, "scheduled_events", str(row["id"]),
        what=(f"a signal reporting the scheduled event {quote(row.get('title'))} ({scope}, "
              f"{row.get('confidence')}), set for {span}"),
        by=by, state=state, met_at=met_at, void_reason=void_reason,
        looked_for=(f"a canonical signal collected since {_day(start)} whose title or summary carries "
                    f"at least {need} of the calendar row's subject keys [{', '.join(quote(k) for k in keys)}] "
                    "(whole words, any case)"),
        sources=None, subject=("target", scope), window_from=start,
        extra={"matched_signal": (match or {}).get("id")},
    )


def classify_inquiry(row: Mapping[str, Any]) -> dict[str, Any]:
    by = _utc(row["resolves_by"])
    status = str(row.get("status") or "open")
    state, void_reason = "absent", None
    if status in ("confirmed", "refuted", "answered"):
        state = "met"
    elif status in ("withdrawn", "expired"):
        state, void_reason = "voided", f"closed by its owner as {status}"
    owner = str(row.get("descriptor_id") or "?")
    text = " ".join(str(row.get("text") or "").split())[:200]
    return _exp(
        INQUIRY, "inquiry_ledger", str(row["id"]),
        what=(f"a close (confirmed / refuted / answered) of the inquiry ledger {row.get('kind')} "
              f"owned by {owner}, due by {_day(by)}: {quote(text)}"),
        by=by, state=state, met_at=row.get("closed_at") if state == "met" else None,
        void_reason=void_reason,
        looked_for=(f"a terminal status on this ledger row, written by its owning inquiry ({owner}) "
                    "through the inquiry_state pack"),
        sources=None, subject=("analyst", owner), window_from=by, carrier=owner,
    )


def parse_tolerance(raw: Iterable[str] | None) -> dict[str, int]:
    out = {k: int(v) for k, v in (s.split("=", 1) for s in DEFAULT_SERIES_TOLERANCE)}
    for item in raw or ():
        name, _, val = str(item).partition("=")
        if name in PERIOD_DAYS and val.strip().isdigit():
            out[name] = int(val)
    return out


def classify_series(row: Mapping[str, Any], source: Mapping[str, Any] | None, *,
                    now: datetime, tolerance: Mapping[str, int]) -> dict[str, Any]:
    sid, series = str(row["source_id"]), str(row["series_id"])
    period = str(row.get("period") or "day")
    newest = _utc(row["newest"])
    raw_lag = (source or {}).get("lag_days")
    lag_declared = isinstance(raw_lag, (int, str)) and str(raw_lag).strip().isdigit()
    lag = int(str(raw_lag).strip()) if lag_declared else 0
    expected_start = newest + timedelta(days=PERIOD_DAYS.get(period, 1))
    published_by = expected_start + timedelta(days=lag)
    by = published_by + timedelta(days=tolerance.get(period, 3))
    state, void_reason = ("absent" if by < now else "met"), None
    if source is None or source.get("state") != "active":
        state = "voided"
        void_reason = (f"its source is no longer active (state {source.get('state')})"
                       if source else "its source has no descriptor head")
    return _exp(
        SERIES, "observations", str(row.get("newest_id") or ""), key=f"{SERIES}:{sid}:{series}",
        what=(f"the next {period} value of the live series {series} from {sid} (the period starting "
              f"{expected_start.date().isoformat()}), published "
              + (f"with a declared lag of {lag} day(s)" if lag_declared else "with no declared lag (0 assumed)")
              + f", within {tolerance.get(period, 3)} day(s) tolerance"),
        by=by, state=state, void_reason=void_reason,
        looked_for=(f"a live observations row of {series} from {sid} with a period start after "
                    f"{newest.date().isoformat()} (the newest held, across {row.get('subjects', 0)} subject(s))"),
        sources=[sid], subject=("source", sid), window_from=published_by,
        extra={"newest": newest.date().isoformat(), "period": period, "lag_days": lag},
    )


# ---------------------------------------------------------------------------
# The ledger diff (pure)
# ---------------------------------------------------------------------------


def prior_ledger(row_data: Any) -> dict[str, dict[str, Any]]:
    """The previous finding's ledger off its stored ``data`` (the
    FindingPayload dump: the handler's own ``data`` sits one level in)."""
    outer = _as_dict(row_data)
    inner = _as_dict(outer.get("data")) or outer
    if inner.get("sub_handler") != SUB_HANDLER_NAME:
        return {}
    return {str(k): _as_dict(v) for k, v in _as_dict(inner.get("ledger")).items()}


def diff(current: Sequence[Mapping[str, Any]], prior: Mapping[str, Mapping[str, Any]], *,
         now: datetime, measured_types: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """``(reported changes, new ledger)``.

    A row due and absent and not in the prior ledger is ``newly_overdue``; a
    prior row now met is ``resolved_late``; a prior row now voided, no longer
    checkable, or no longer in the record is ``voided``; a prior row still
    absent is carried silently. Prior rows of a type that was not measured
    this run (a failed or disabled read) are carried unchanged."""
    measured = set(measured_types)
    ledger: dict[str, dict[str, Any]] = {}
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in current:
        key = e["key"]
        seen.add(key)
        before = prior.get(key)
        if e["state"] == "absent":
            ledger[key] = dict(before) if before else {
                "type": e["type"], "ref_id": e["ref_id"], "since": now.isoformat(timespec="seconds"),
                "by": _iso(e["by"]), "subject_kind": e["subject_kind"], "subject_id": e["subject_id"],
            }
            if before is None:
                changes.append({**e, "transition": NEWLY_OVERDUE})
            continue
        if before is None:
            continue
        if e["state"] == "met":
            changes.append({**e, "transition": RESOLVED_LATE, "since": before.get("since")})
        else:
            reason = e["void_reason"] or "the row no longer declares anything to look for"
            changes.append({**e, "transition": VOIDED, "void_reason": reason, "since": before.get("since")})
    for key, before in prior.items():
        if key in seen:
            continue
        if before.get("type") not in measured:
            ledger[key] = dict(before)
            continue
        changes.append({
            "key": key, "type": before.get("type"), "ref_kind": None, "ref_id": before.get("ref_id"),
            "what": f"the expectation previously reported as {key}", "by": _utc(before.get("by")),
            "state": "voided", "met_at": None, "transition": VOIDED, "since": before.get("since"),
            "void_reason": "its row is no longer in the record", "looked_for": "", "sources": None,
            "subject_kind": before.get("subject_kind") or "platform",
            "subject_id": before.get("subject_id") or key, "window_from": None, "note": None,
            "carrier": None, "extra": {},
        })
    return changes, ledger


# ---------------------------------------------------------------------------
# Rendering (pure; every string passes the wording rule)
# ---------------------------------------------------------------------------


def describe_health(sources: Sequence[str] | None, health: Mapping[str, Mapping[str, Any]],
                    *, carrier: str | None = None, carrier_outputs: int | None = None) -> str:
    if carrier is not None:
        if carrier_outputs is None:
            return f"Carried by its owning inquiry {carrier}; whether it ran in the window was not measured."
        if carrier_outputs == 0:
            return f"Carried by its owning inquiry {carrier}, which has written nothing since the due date."
        return (f"Carried by its owning inquiry {carrier}, which has written {carrier_outputs} "
                "output(s) since the due date.")
    if not health:
        return "Source health in the window was not measured."
    if sources:
        parts = []
        for sid in sources:
            h = health.get(sid) or {"class": "silent", "watchdog": []}
            word = _HEALTH_WORDS.get(h["class"], h["class"])
            if h["class"] == "partly_failing":
                word += f" ({h.get('errors', 0)} of {h.get('polls', 0)} polls errored)"
            elif h["class"] == "polling_clean":
                word += f" ({h.get('polls', 0)} polls, {h.get('written', 0)} new rows)"
            dog = h.get("watchdog") or []
            word += f"; watchdog episodes: {', '.join(dog)}" if dog else "; no watchdog episode"
            parts.append(f"{sid}: {word}")
        return "Sources that would carry it: " + "; ".join(parts) + "."
    tally: dict[str, int] = {}
    for h in health.values():
        tally[h["class"]] = tally.get(h["class"], 0) + 1
    dogged = sum(1 for h in health.values() if h.get("watchdog"))
    counts = ", ".join(f"{tally.get(c, 0)} {c.replace('_', ' ')}"
                       for c in ("polling_clean", "partly_failing", "failing", "silent"))
    return (f"The row names no source, so any of the {len(health)} active sources could carry it; "
            f"in the window: {counts}; {dogged} with a watchdog episode.")


def watchdog_owns(change: Mapping[str, Any], health: Mapping[str, Mapping[str, Any]]) -> bool:
    """A series row whose own source's polls were silent or failing belongs to
    the liveness watchdog (its cadence-stall / source-health alerts)."""
    if change.get("type") != SERIES:
        return False
    srcs = change.get("sources") or []
    return bool(srcs) and all((health.get(s) or {}).get("class") in ("silent", "failing") for s in srcs)


def render_change(change: Mapping[str, Any], *, now: datetime, health_text: str | None = None,
                  owned_by_watchdog: bool = False) -> str:
    t = change["transition"]
    by = change.get("by")
    head = f"[{change['type']}] {change['ref_kind'] or 'row'} {change['ref_id']}"
    if t == NEWLY_OVERDUE:
        lines = [
            f"{head}: expected {change['what']}; due by {_day(by)}, overdue by {_span(now - by)}.",
            f"Looked for: {change['looked_for']}. {ABSENCE_SENTENCE}",
        ]
        if change.get("note"):
            lines.append(f"Note: {change['note']}.")
        if health_text:
            lines.append(health_text)
        if owned_by_watchdog:
            lines.append("The source's own polls were silent or failing: the liveness watchdog's "
                         "source-health alerts own this; no due_and_absent alert is raised.")
    elif t == RESOLVED_LATE:
        met = change.get("met_at")
        when = (f"collected {_day(met)}, {_span(met - by)} after it was due" if met is not None and by is not None
                and met > by else "now held")
        lines = [f"{head}: reported due and absent since {_day(change.get('since'))}; "
                 f"a matching record is {when}: {change['what']}."]
    else:
        lines = [f"{head}: reported due and absent since {_day(change.get('since'))}; the expectation "
                 f"is void: {change.get('void_reason')}. ({change['what']})"]
    return check_wording(" ".join(lines))


def build_finding(changes: Sequence[Mapping[str, Any]], rendered: Sequence[str], *,
                  ledger: Mapping[str, Mapping[str, Any]], counts: Mapping[str, Any],
                  read_errors: Mapping[str, str], truncated: Sequence[str],
                  alerts: Mapping[str, int], as_of: datetime) -> FindingPayload:
    by_t = {t: sum(1 for c in changes if c["transition"] == t) for t in TRANSITIONS}
    title = check_wording(
        f"Due and absent: {by_t[NEWLY_OVERDUE]} newly overdue, {by_t[RESOLVED_LATE]} resolved late, "
        f"{by_t[VOIDED]} voided ({len(ledger)} due and absent in all)")
    head = [
        f"Rule {METHOD_VERSION}: an expectation is a row of the record that carries an explicit date "
        "(a sealed forecast's due date, an acute forecast's window, a calendar row's date, an inquiry "
        "row's resolves-by, a live series' period and declared lag); it is due and absent when the date "
        "and its tolerance have passed and the record it implies is not in this collection. A line is "
        "written only when its state changes (newly overdue, resolved late, voided).",
        DISCLAIMER.format(as_of=_day(as_of)),
    ]
    if read_errors:
        head.append("UNMEASURED this run (the read failed; earlier rows carried unchanged): "
                    + ", ".join(f"{t} ({e})" for t, e in sorted(read_errors.items())) + ".")
    if truncated:
        head.append("Read cap reached for: " + ", ".join(sorted(truncated)) + " (rows beyond the cap not read).")
    body = check_wording("\n\n".join(["\n".join(head), *rendered]))
    rows = [{
        "key": c["key"], "type": c["type"], "transition": c["transition"], "ref_kind": c["ref_kind"],
        "ref_id": c["ref_id"], "what": c["what"], "by": _iso(c.get("by")), "met_at": _iso(c.get("met_at")),
        "since": c.get("since"), "void_reason": c.get("void_reason"), "looked_for": c.get("looked_for"),
        "sources": c.get("sources"), "subject_kind": c["subject_kind"], "subject_id": c["subject_id"],
        "overdue_hours": (round((as_of - c["by"]).total_seconds() / 3600.0, 1)
                          if c["transition"] == NEWLY_OVERDUE and c.get("by") else None),
        "health": c.get("health"), "alert": c.get("alert"),
    } for c in changes]
    return FindingPayload(
        title=title[:2048], body=body[:65536], confidence=1.0,
        tags=["due_and_absent", "deterministic", "no_llm"],
        data={
            "sub_handler": SUB_HANDLER_NAME, "method_version": METHOD_VERSION,
            "as_of": as_of.isoformat(timespec="seconds"), "transitions": by_t,
            "counts": dict(counts), "read_errors": dict(read_errors), "truncated": list(truncated),
            "alerts": dict(alerts), "rows": rows, "ledger": {k: dict(v) for k, v in ledger.items()},
            "citations": [{"ref_kind": c["ref_kind"], "ref_id": c["ref_id"], "key": c["key"]}
                          for c in changes if c.get("ref_kind")],
        },
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _uuids(values: Iterable[Any]) -> list[str]:
    out = []
    for v in values:
        try:
            out.append(str(UUID(str(v))))
        except (TypeError, ValueError):
            continue
    return out


async def _collect(conn: Any, *, now: datetime, opts: Mapping[str, Any],
                   prior: Mapping[str, Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any],
                                                                     dict[str, str], list[str], list[str]]:
    """Every expectation of every enabled type, classified."""
    limit = int(opts["max_rows"])
    prior_ids = {t: _uuids(v.get("ref_id") for v in prior.values() if v.get("type") == t) for t in TYPES}
    out: list[dict[str, Any]] = []
    counts: dict[str, Any] = {}
    errors: dict[str, str] = {}
    truncated: list[str] = []
    measured: list[str] = []
    for type_ in opts["types"]:
        try:
            if type_ == FORECAST:
                rows = await R.fetch(conn, R.FORECAST_QUESTIONS_SQL, now, prior_ids[FORECAST], limit)
                exps = [classify_forecast_question(r) for r in rows]
            elif type_ == ACUTE:
                grace = int(opts["acute_grace_hours"])
                rows = await R.fetch(conn, R.ACUTE_FORECASTS_SQL, now - timedelta(hours=grace),
                                     prior_ids[ACUTE], limit)
                exps = [classify_acute_forecast(r, grace_hours=grace) for r in rows]
            elif type_ == SCHEDULED:
                tol, hits = int(opts["scheduled_tolerance_hours"]), int(opts["scheduled_min_key_hits"])
                lookback = timedelta(days=int(opts["scheduled_lookback_days"]))
                rows = await R.fetch(conn, R.SCHEDULED_EVENTS_SQL, now, tol, now - lookback,
                                     prior_ids[SCHEDULED], limit)
                live = [{**r, "starts_at": _utc(r["starts_at"])} for r in rows if r.get("retired_at") is None]
                # The scan floor: a row is searched from its own start, but
                # never further back than the look-back plus a week — a match
                # older than that was there to be seen by an earlier daily run.
                floor = max(min((r["starts_at"] for r in live), default=now), now - lookback - timedelta(days=7))
                matches = await R.first_matches(conn, live, floor=floor, until=now, min_hits=hits)
                exps = [classify_scheduled_event(r, matches.get(str(r["id"])), tolerance_hours=tol,
                                                 min_key_hits=hits) for r in rows]
            elif type_ == INQUIRY:
                rows = await R.fetch(conn, R.INQUIRY_SQL, now, prior_ids[INQUIRY], limit)
                exps = [classify_inquiry(r) for r in rows]
            elif type_ == SERIES:
                rows = await R.fetch(conn, R.SERIES_SQL, limit)
                srcs = {str(s["descriptor_id"]): dict(s) for s in await R.fetch(
                    conn, R.SERIES_SOURCES_SQL, sorted({str(r["source_id"]) for r in rows}))}
                tol_map = parse_tolerance(opts["series_tolerance"])
                exps = [classify_series(r, srcs.get(str(r["source_id"])), now=now, tolerance=tol_map)
                        for r in rows]
            else:
                continue
        except Exception as exc:  # noqa: BLE001 — a failed read is UNMEASURED, never a zero
            logger.warning("due_and_absent.read_failed type=%s err=%s", type_, exc)
            errors[type_] = type(exc).__name__
            continue
        measured.append(type_)
        if len(rows) >= limit:
            truncated.append(type_)
        out.extend(exps)
        counts[type_] = {s: sum(1 for e in exps if e["state"] == s)
                         for s in ("absent", "met", "voided", "not_checkable")}
    return out, counts, errors, truncated, measured


async def _annotate(conn: Any, changes: list[dict[str, Any]], *, now: datetime) -> None:
    """Source (or carrier) health for each newly overdue row, in its window."""
    active: list[str] | None = None
    for c in changes:
        if c["transition"] != NEWLY_OVERDUE:
            continue
        since = max(c.get("window_from") or now - timedelta(days=1), now - timedelta(days=HEALTH_WINDOW_MAX_DAYS))
        since = min(since, now)
        try:
            if c.get("carrier"):
                n = await R.analyst_outputs_since(conn, c["carrier"], c["by"])
                c["health"] = {"carrier": c["carrier"], "outputs_since_due": n}
                c["health_text"] = describe_health(None, {}, carrier=c["carrier"], carrier_outputs=n)
                continue
            if c.get("sources"):
                health = await R.source_health(conn, c["sources"], since, now)
            else:
                if active is None:
                    active = await R.active_sources(conn)
                health = await R.source_health(conn, active, since, now)
        except Exception as exc:  # noqa: BLE001 — health is context, never a reason to drop the row
            logger.warning("due_and_absent.health_failed key=%s err=%s", c["key"], exc)
            c["health_text"] = "Source health in the window was not measured (the read failed)."
            continue
        c["health"] = ({s: health.get(s) for s in c["sources"]} if c.get("sources")
                       else {"window_from": _iso(since), "sources": len(health),
                             "by_class": {k: sum(1 for h in health.values() if h["class"] == k)
                                          for k in ("polling_clean", "partly_failing", "failing", "silent")}})
        c["health_text"] = describe_health(c.get("sources"), health)
        c["watchdog_owns"] = watchdog_owns(c, health)


async def _raise(pool: Any, change: Mapping[str, Any], text: str) -> str:
    """One logged alert per reported change. Returns raised / failed."""
    try:
        from ...alerts.raise_alert import raise_alert

        await raise_alert(
            pool, kind="due_and_absent", severity="low",
            summary=f"Due and absent ({change['transition']}): {change['key']}",
            subject_id=change["subject_id"], subject_kind=change["subject_kind"],
            dedupe_key=f"{ALERT_KIND}:{change['key']}", detail=text,
            evidence_refs=[change["ref_id"]] if change.get("ref_id") else [],
            close=change["transition"] != NEWLY_OVERDUE, raised_by=SUB_HANDLER_NAME,
        )
        return "raised"
    except Exception as exc:  # noqa: BLE001 — an alert never fails the run
        logger.warning("due_and_absent.alert_failed key=%s err=%s", change.get("key"), exc)
        return "failed"


def resolve_options(options: Mapping[str, Any]) -> dict[str, Any]:
    types = [t for t in (options.get("expectation_types") or TYPES) if t in TYPES]
    return {
        "types": types,
        "acute_grace_hours": max(1, int(options.get("acute_grace_hours", DEFAULT_ACUTE_GRACE_HOURS))),
        "scheduled_tolerance_hours": max(1, int(options.get(
            "scheduled_tolerance_hours", DEFAULT_SCHEDULED_TOLERANCE_HOURS))),
        "scheduled_lookback_days": max(1, int(options.get(
            "scheduled_lookback_days", DEFAULT_SCHEDULED_LOOKBACK_DAYS))),
        "scheduled_min_key_hits": max(1, int(options.get(
            "scheduled_min_key_hits", DEFAULT_SCHEDULED_MIN_KEY_HITS))),
        "series_tolerance": list(options.get("series_tolerance") or DEFAULT_SERIES_TOLERANCE),
        "max_rows": max(1, int(options.get("max_rows_per_type", DEFAULT_MAX_ROWS_PER_TYPE))),
    }


async def handle(
    inputs: list[dict[str, Any]], options: Mapping[str, Any], deps: Any | None,
) -> AnalystMethodResult:
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "due_and_absent requires a live deps.pg_pool — refusing to report "
            "'nothing is due and absent' without reading the record"
        )
    opts = resolve_options(options)
    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    now = datetime.now(tz=timezone.utc)

    async with pool.acquire() as conn:
        prior_row = await conn.fetchrow(R.PRIOR_SQL, analyst_id)
        prior = prior_ledger(prior_row["data"] if prior_row is not None else None)
        current, counts, errors, truncated, measured = await _collect(conn, now=now, opts=opts, prior=prior)
        changes, ledger = diff(current, prior, now=now, measured_types=measured)
        await _annotate(conn, changes, now=now)

    rendered: list[str] = []
    alerts = {"raised": 0, "failed": 0, "watchdog_owns": 0}
    for c in changes:
        owned = bool(c.get("watchdog_owns"))
        text = render_change(c, now=now, health_text=c.get("health_text"), owned_by_watchdog=owned)
        rendered.append(text)
        if owned:
            c["alert"] = "watchdog_owns"
            alerts["watchdog_owns"] += 1
            continue
        c["alert"] = await _raise(pool, c, text)
        alerts[c["alert"]] += 1

    finding = build_finding(changes, rendered, ledger=ledger, counts=counts, read_errors=errors,
                            truncated=truncated, alerts=alerts, as_of=now)
    logger.info("due_and_absent.done changes=%d ledger=%d errors=%s", len(changes), len(ledger), sorted(errors))
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        # No state changed = no feed row; the ledger stays on the last real finding.
        force_trace_only=not changes,
        derived_from=[UUID(u) for u in _uuids(c.get("ref_id") for c in changes)],
    )


__all__ = [
    "ABSENCE_SENTENCE",
    "ALERT_KIND",
    "BANNED_PHRASES",
    "METHOD_VERSION",
    "SUB_HANDLER_NAME",
    "TRANSITIONS",
    "TYPES",
    "WordingViolation",
    "build_finding",
    "check_wording",
    "classify_acute_forecast",
    "classify_forecast_question",
    "classify_inquiry",
    "classify_scheduled_event",
    "classify_series",
    "describe_health",
    "diff",
    "handle",
    "prior_ledger",
    "quote",
    "render_change",
    "resolve_options",
    "watchdog_owns",
]
