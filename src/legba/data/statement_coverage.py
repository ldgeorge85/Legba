# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""COVERAGE — statements about what the collection holds or how its reporting
framed something (lane S-6).

A desk writes "covered by three outlets", "the framing spans 16.9 hours
across six distinct outlets", "this window's 38 signals", "reporting framed
the vote as a referendum on the government". Such a sentence is not a report
of the world: it does not say the vote happened, and no quote from one of the
rows states it. Nor is it an absence ("no signal in this window …"). Typed
as an event or a level it can only fail; typed as an absence it is refused
for stating none. S-5 measured these as a top failure class.

WHAT IT IS — A BEHAVIOUR CLASS, BESIDE ABSENCE. The merged architecture
recommendation (DATA_LAYERS 2026-11-14, 04 §1.5 point 2, §4.1 point 3) keeps a
closed list of behaviour classes in code, each fixing what may establish a
thing, what it is checked against and how it ages; statement, assessment and
absence are separate classes and a restatement is a link. Coverage is a
class of its own on that list, a sibling of absence: both are records ABOUT
THE COLLECTION, scoped to the handed slice (collection, sources, window),
never statements about the world. Where absence says "nothing matching X was
collected", coverage says "what was collected, counted": its support is the
cited ROWS themselves, checked by code, never a quote and never the verifier.
In the piece tally it is therefore counted apart (a sixth bucket), because
the outcome list mirrors the classes; it is the class, not the bucket, that
carries the rule.

THE GATE (:func:`evaluate_coverage_gates`), code only:

* ``G8.coverage_kind`` — the kind is ``coverage``.
* ``G8.coverage_stated`` — the text speaks about the reporting or the
  collection (outlets, sources, signals, items, reports, reporting,
  coverage, carried, framed, slice, window, collection …). A world fact
  typed coverage to dodge the verifier fails here.
* ``G8.coverage_not_absence`` — a piece that states an absence ("no item
  …") is an absence, not coverage.
* ``G8.coverage_scope`` — the handed slice says what the collection was:
  its id and its window (as for an absence).
* ``G8.coverage_rows`` — the piece cites at least one row, and every cited
  row was handed (G3).
* ``G8.coverage_counts`` — every figure the text states is one code can
  check against the rows, and checks out:

  =====================================  ======================================
  the text says                          code checks
  =====================================  ======================================
  N outlets / sources / publishers       at least N distinct ORIGIN GROUPS among
  (also "N distinct outlets")            the cited rows (G7's independence
                                         count); exactly N after "only" /
                                         "exactly" / "just"
  N signals / items / reports / articles at least N distinct cited rows (exactly
  / stories / framings                   N after "only" / "exactly" / "just")
  this window's / slice's N signals,     N equals the number of rows the run
  N collected items, an N-signal slice   was handed
  an N-hour (-day) slice / window        the handed window's length, within one
                                         unit
  spans / within / across N hours        the spread of the cited rows'
  (days)                                 publication times: ≈ N (spans,
                                         across) or ≤ N (within), to the
                                         stated precision
  =====================================  ======================================

  Any other figure is ``coverage_number_unchecked`` and fails: a figure code
  cannot check never rides along.
* ``G8.coverage_dates`` — every date it states is a cited row's publication
  day or a day inside the handed window.
* ``G11`` — no control text.

A coverage statement never establishes the reported event: it is never sent
to the verifier (``not_applicable``) and is never a premise of anything but
an assessment.

PURE. stdlib + the content and number readers + the independence count.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Any

from .statement_content import find_date_ranges, find_dates, strip_markers
from .statement_gate import (
    absence_scope,
    access_ceiling,
    check_cited_ids,
    check_control_text,
    check_statement_shape,
)
from .statement_independence import count_groups
from .statement_numbers import EXACT, read_figures
from .statement_shapes import (
    CheckResult,
    EvidenceTexts,
    GateReport,
    HandedSlice,
    StatementKind,
    StatementLike,
    SupportLike,
    enum_value,
)

_COVERAGE_RE = re.compile(
    r"\b(?:outlets?|sources?|publishers?|signals?|items?|reports?|reporting|reported\s+by|coverage|"
    r"covered|covers?|carried|carr(?:y|ies)|carrying|framings?|framed|frames?|wires?|headlines?|"
    r"articles?|stories|slice|window|collection|collected|newsrooms?|press|media|dispatches|spread)\b",
    re.IGNORECASE,
)
_OUTLETS_RE = re.compile(
    r"^(?:\s+(?:distinct|different|separate|independent|unique|other|more|new|main|major|named|"
    r"national|foreign|local|international|wire|news))*\s+(?:outlets?|sources?|publishers?|origins?|"
    r"newsrooms?|media\s+(?:outlets?|organi[sz]ations?)|wire\s+services?|agencies)\b",
    re.IGNORECASE,
)
_ROWS_RE = re.compile(
    r"^(?:-|(?:\s+(?:distinct|different|separate|other|more|new|collected|cited|sports?|news|"
    r"wire|related|relevant|recent|unrelated|of\s+the|such))*\s+)(?:signals?|items?|reports?|articles?|"
    r"stories|story|records?|pieces|headlines?|framings?|dispatches|posts?|rows)\b",
    re.IGNORECASE,
)
_EXACTLY_BEFORE_RE = re.compile(r"(?<![\w-])(?:only|exactly|just|a\s+total\s+of)\s+$", re.IGNORECASE)
_SLICE_POSSESSIVE_RE = re.compile(
    r"(?<![\w-])(?:(?:this|the)\s+(?:window|slice|collection|desk|read)['’]s|the\s+window\s+holds|"
    r"the\s+slice\s+holds|(?:this|the)\s+(?:window|slice)\s+(?:of|with|carries|contains|holds))\s+$",
    re.IGNORECASE,
)
_COLLECTED_AFTER_RE = re.compile(r"^\s+(?:collected|handed)\b", re.IGNORECASE)
_COLLECTED_BEFORE_RE = re.compile(r"(?<![\w-])(?:the\s+)?(?:collected|handed)\s+$", re.IGNORECASE)
_SLICE_NOUN_AFTER_RE = re.compile(r"^(?:-[a-z]+)?\s+(?:slice|window|collection)\b", re.IGNORECASE)
_SLICE_OF_BEFORE_RE = re.compile(r"(?<![\w-])(?:slice|window|collection)\s+(?:of|spanning|covering)\s+$",
                                 re.IGNORECASE)
_WITHIN_BEFORE_RE = re.compile(r"(?<![\w-])(?:within|inside|in\s+under|in)\s+$", re.IGNORECASE)
_SPAN_BEFORE_RE = re.compile(r"(?<![\w-])(?:spans?|spanning|spanned|across|over)\s+$", re.IGNORECASE)

_UNIT_HOURS = {"minute": 1 / 60, "hour": 1.0, "day": 24.0, "week": 168.0}


#: What makes a piece an ABSENCE rather than coverage: it says something is
#: NOT in the collection ("no item …", "none of …", "nothing …"). A contrast
#: inside a framing ("framed it as X, not as Y") is coverage, not an absence.
_ABSENCE_CLAIM_RE = re.compile(r"\b(?:no|none|nothing|neither)\b(?!-)", re.IGNORECASE)


def states_an_absence(text: str | None) -> bool:
    """Does ``text`` state that something is absent from the collection?"""
    return bool(text) and bool(_ABSENCE_CLAIM_RE.search(strip_markers(text)))


def coverage_stated(text: str | None) -> bool:
    """Does ``text`` speak about the reporting or the collection?"""
    return bool(text) and bool(_COVERAGE_RE.search(strip_markers(text)))


def _hours(a: datetime, b: datetime) -> float:
    return abs((b - a).total_seconds()) / 3600.0


def _utc(v: Any) -> datetime | None:
    if not isinstance(v, datetime):
        return None
    return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


def _half_unit(raw: str) -> float:
    """Half a unit of the stated precision: 16.9 → 0.05, 17 → 0.5."""
    m = re.search(r"\.(\d+)", raw)
    return 0.5 * 10 ** (-len(m.group(1))) if m else 0.5


def _check_figures(
    text: str, rows: Sequence[SupportLike], texts: EvidenceTexts, handed: HandedSlice,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Each figure checked against the rows; ``(checked, failures)``."""
    checked: list[dict[str, Any]] = []
    failures: list[str] = []
    distinct_rows = len({str(r.ref_id).lower() for r in rows})
    groups = count_groups(rows, texts).groups if rows else 0
    pubs = sorted(p for p in (_utc(getattr(r, "published_at", None)) for r in rows) if p is not None)
    ws, we = _utc(getattr(handed, "window_start", None)), _utc(getattr(handed, "window_end", None))
    handed_n = len(handed.ref_ids)
    for f in read_figures(text, side="statement"):
        before, after = text[max(0, f.span[0] - 40):f.span[0]], text[f.span[1]:f.span[1] + 60]
        exact = bool(_EXACTLY_BEFORE_RE.search(before))
        n = f.value
        row: dict[str, Any] = {"figure": f.raw, "read_as": f.label()}
        ok: bool | None = None
        if f.is_range or f.qualifier != EXACT:
            ok = None
        elif f.unit in _UNIT_HOURS:
            hours = n * _UNIT_HOURS[f.unit]
            tol = _half_unit(f.raw) * _UNIT_HOURS[f.unit]
            if _SLICE_NOUN_AFTER_RE.match(after) or _SLICE_OF_BEFORE_RE.search(before):
                row["against"] = "handed_window"
                if ws and we:
                    length = _hours(ws, we)
                    row["window_hours"] = round(length, 3)
                    ok = abs(length - hours) <= max(tol, _UNIT_HOURS[f.unit])
            elif _WITHIN_BEFORE_RE.search(before) or _SPAN_BEFORE_RE.search(before):
                row["against"] = "cited_publication_spread"
                if len(pubs) >= 2:
                    spread = _hours(pubs[0], pubs[-1])
                    row["spread_hours"] = round(spread, 3)
                    ok = spread <= hours + tol if _WITHIN_BEFORE_RE.search(before) \
                        else abs(spread - hours) <= tol
        elif f.unit is None and _OUTLETS_RE.match(after):
            row.update(against="origin_groups", groups=groups)
            ok = groups == n if exact else groups >= n
        elif f.unit is None and (_SLICE_POSSESSIVE_RE.search(before) or _COLLECTED_BEFORE_RE.search(before)
                                 or _COLLECTED_AFTER_RE.match(after)
                                 or (_ROWS_RE.match(after) and _SLICE_NOUN_AFTER_RE.match(
                                     after[_ROWS_RE.match(after).end():]))):
            row.update(against="handed_rows", handed=handed_n)
            ok = handed_n == n if handed_n else None
        elif f.unit is None and _ROWS_RE.match(after):
            row.update(against="cited_rows", rows=distinct_rows)
            ok = distinct_rows == n if exact else distinct_rows >= n
        if ok is None:
            failures.append("coverage_number_unchecked")
            row["passed"] = False
        else:
            row["passed"] = bool(ok)
            if not ok:
                failures.append("coverage_count_mismatch")
        checked.append(row)
    return checked, failures


def _day(v: Any) -> date | None:
    d = _utc(v)
    return d.astimezone(timezone.utc).date() if d else None


def _check_dates(text: str, rows: Sequence[SupportLike], handed: HandedSlice) -> tuple[list[str], list[str]]:
    pub_days = {d for d in (_day(getattr(r, "published_at", None)) for r in rows) if d}
    ws, we = _day(getattr(handed, "window_start", None)), _day(getattr(handed, "window_end", None))
    years = {d.year for d in pub_days} | ({ws.year} if ws else set()) | ({we.year} if we else set())
    stated = list(find_dates(text))
    stated += [d for r in find_date_ranges(text) for d in (r.start, r.end)]
    ok_raw, bad_raw = [], []
    for m in stated:
        if m.day is None:
            ok_raw.append(m.raw)       # a month: the slice's month is not checked here
            continue
        cands = [date(y, m.month, m.day) for y in ([m.year] if m.year else sorted(years))
                 if _valid_day(y, m.month, m.day)]
        good = any(c in pub_days or (ws and we and ws <= c <= we) for c in cands)
        (ok_raw if good else bad_raw).append(m.raw)
    return ok_raw, bad_raw


def _valid_day(y: int, m: int, d: int) -> bool:
    try:
        date(y, m, d)
    except ValueError:
        return False
    return True


def evaluate_coverage_gates(
    statement: StatementLike,
    *,
    handed: HandedSlice,
    rows: Sequence[SupportLike],
    texts: EvidenceTexts,
) -> GateReport:
    """The code gates for a COVERAGE statement (the module doc). ``rows`` are
    the rows the piece cites, as support-shaped rows (no quote needed): the
    rows it counts."""
    checks: list[CheckResult] = [c for c in check_statement_shape(statement, ())
                                 if c.check != "evidence_present"]
    if enum_value(statement.kind) != StatementKind.COVERAGE.value:
        checks.append(CheckResult("G8", "coverage_kind", False, "not_coverage"))
    body = strip_markers(statement.text)
    stated = coverage_stated(body)
    checks.append(CheckResult("G8", "coverage_stated", stated, None if stated else "coverage_not_stated"))
    absent = states_an_absence(body)
    checks.append(CheckResult("G8", "coverage_not_absence", not absent,
                              "coverage_states_absence" if absent else None))
    scope = absence_scope(handed)
    missing = [k for k in ("collection", "window_start", "window_end") if not scope.get(k)]
    checks.append(CheckResult("G8", "coverage_scope", not missing,
                              "coverage_scope_unknown" if missing else None, None,
                              {**scope, "missing": missing}))
    cited = [str(r.ref_id) for r in rows if r.ref_id is not None]
    checks.append(CheckResult("G8", "coverage_rows", bool(cited), None if cited else "coverage_rows_missing",
                              None, {"rows": len(set(cited))}))
    if cited:
        checks.append(check_cited_ids(cited, handed))
    figures, fails = _check_figures(body, rows, texts, handed)
    checks.append(CheckResult("G8", "coverage_counts", not fails, fails[0] if fails else None, None,
                              {"figures": figures}))
    ok_dates, bad_dates = _check_dates(body, rows, handed)
    checks.append(CheckResult("G8", "coverage_dates", not bad_dates,
                              "coverage_date_unchecked" if bad_dates else None, None,
                              {"accepted": ok_dates, "missing": bad_dates}))
    checks.append(check_control_text(statement, ()))
    # the ceiling is the counted rows': a count of restricted rows is as
    # restricted as they are (G13)
    ceiling, ceiling_check = access_ceiling(rows)
    checks.append(ceiling_check)
    groups = count_groups(rows, texts) if rows else None
    return GateReport(checks=tuple(checks),
                      independent_groups=groups.groups if groups else 0,
                      checkable_groups=groups.checkable_groups if groups else 0,
                      access_ceiling=ceiling)


__all__ = [
    "coverage_stated",
    "evaluate_coverage_gates",
]
