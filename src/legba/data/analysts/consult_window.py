# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult turn's WINDOW CONTRACT — pure, no I/O.

THE DEFECT. A consult turn was asked about infrastructure
disruptions "for this window". No window reached the turn (the console's
"asking about … window …" line sends nothing), the model chose one itself and
opened "Survey window: … disruption inventory 2026-11-12 to 2026-11-14 …",
and nothing anywhere said that the window was the model's choice, or that it
was narrower than what the asker could see.

THE CONTRACT. Every turn has exactly one window IN FORCE, resolved here in a
fixed order, and the answer states which source it came from:

1. ``user_words`` — the question states one: a date range ("2026-11-12 to
   2026-11-14", "between … and …", "from … to …"), "since <date>", a relative
   span ("last 48 hours", "past 7 days", "last two weeks"), or a named
   span ("today", "yesterday", "this week", "this month", "on <date>").
2. ``console`` — the request carries the console's pinned window
   (``inputs[0]["window"]``: ``{"start", "end"}`` ISO instants, or
   ``{"hours", "end"}``).
3. ``default`` — :data:`DEFAULT_WINDOW_HOURS` ending at the turn
   (``LEGBA_CONSULT_DEFAULT_WINDOW_HOURS``), the same 168 hours
   ``list_findings(mode="survey")`` reads by default.

(1) also reads numeric day/month dates under a stated rule,
"from the 3rd to the 10th", "early / mid / late <month>" on fixed day bands
(``consult_window_words``; the reading taken is printed) — and EVENT
ANCHORS ("since the ceasefire"; ``consult_window_anchor``): one whose own
words carry a date is anchored on it; any other is declared on the window as
asked for and NOT resolved, and the precedence above stands.

A question that points AT a window without naming one ("for this window",
"in this window") is recorded as ``deictic`` — when no console window arrived,
the rendered line says so, because that is precisely the reviewed turn.

NARROWING AND WIDENING (:func:`assess_reach`). The window in force is compared
against the dates of the rows the answer CITES (and against what the model
declared in its contract block). A citation span that leaves a day or more of
the window uncited at either edge while the coverage shows rows there is a
narrowing; declared with a reason it is reported as declared, otherwise as
undeclared. Cited rows dated before the window are a wider reach, allowed only
as labelled context.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .consult_window_anchor import Anchor, find_anchor
from .consult_window_words import parse_words

__all__ = [
    "ANCHOR_NOT_RESOLVED",
    "DEFAULT_WINDOW_HOURS",
    "DEFAULT_WINDOW_HOURS_ENV",
    "SOURCE_CONSOLE",
    "SOURCE_DEFAULT",
    "SOURCE_USER_WORDS",
    "TurnWindow",
    "assess_reach",
    "default_window_hours",
    "parse_instant",
    "parse_user_window",
    "render_window_lines",
    "resolve_window",
]

SOURCE_USER_WORDS = "user_words"
SOURCE_CONSOLE = "console"
SOURCE_DEFAULT = "default"

DEFAULT_WINDOW_HOURS_ENV = "LEGBA_CONSULT_DEFAULT_WINDOW_HOURS"
#: 168 h — the platform's survey default (``list_findings(mode="survey")``
#: reads ``since_hours=168``), so an unstated window means the same week the
#: survey reader already shows.
DEFAULT_WINDOW_HOURS = 168
#: A narrowing is reported when an edge of the window at least this wide is
#: left uncited while rows exist there.
EDGE_TOLERANCE = timedelta(hours=24)

_ISO_DATE = r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2})?(?:Z|\+00:?00)?)?"
_RANGE_SEP = r"(?:\s*(?:to|through|thru|until|till|and|–|—|→|\.\.)\s*|\s+-\s+)"
_RANGE_RE = re.compile(_ISO_DATE + _RANGE_SEP + _ISO_DATE, re.IGNORECASE)
_SINCE_RE = re.compile(r"\bsince\s+" + _ISO_DATE, re.IGNORECASE)
_ON_RE = re.compile(r"\b(?:on|for|during)\s+" + _ISO_DATE + r"(?!\s*" + _RANGE_SEP + r"\d)",
                    re.IGNORECASE)
_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fourteen": 14,
    "thirty": 30, "a": 1, "an": 1,
}
_RELATIVE_RE = re.compile(
    # ``prior`` / ``previous`` / ``preceding`` are deliberately NOT window words:
    # "compare with the prior six months" names a BASELINE, not the window.
    r"\b(?:last|past)\s+(\d{1,4}|" + "|".join(_NUM_WORDS)
    + r")?\s*(hours?|hrs?|h|days?|d|weeks?|wks?|months?)\b",
    re.IGNORECASE,
)
_NAMED_RE = re.compile(
    # Q-2: "late this month" while it is still mid-month is a band still to
    # come (``consult_window_words``), never "this month".
    r"(?<!early )(?<!mid )(?<!mid-)(?<!late )"
    r"\b(today|yesterday|this week|this month|last week|past week|last month|past month)\b",
    re.IGNORECASE,
)
_DEICTIC_RE = re.compile(
    r"\b(?:this|that|the (?:current|selected|pinned))\s+(?:window|period|span|timeframe|time frame)\b",
    re.IGNORECASE,
)


def default_window_hours() -> int:
    """Env ``LEGBA_CONSULT_DEFAULT_WINDOW_HOURS`` (a malformed pin falls back)."""
    raw = (os.getenv(DEFAULT_WINDOW_HOURS_ENV) or "").strip()
    try:
        value = int(float(raw)) if raw else DEFAULT_WINDOW_HOURS
    except ValueError:
        return DEFAULT_WINDOW_HOURS
    return value if value > 0 else DEFAULT_WINDOW_HOURS


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _instant(day: str, hhmm: str | None) -> datetime:
    d = date.fromisoformat(day)
    hour, minute = (int(x) for x in hhmm.split(":")) if hhmm else (0, 0)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_instant(value: Any) -> datetime | None:
    """An ISO instant (or a datetime) as UTC, else ``None``."""
    return _parse_iso(value)


@dataclass(frozen=True)
class TurnWindow:
    """The one window in force for a turn."""

    start: datetime
    end: datetime
    source: str
    #: The words / value it came from ("last 48 hours", "console 24h", "168h default").
    basis: str
    #: The question pointed at a window without naming one ("this window").
    deictic: bool = False
    #: A console window that arrived but could not be read, kept so the answer can say so.
    console_rejected: str | None = None
    #: Q-2 — how the asker's words were read when they could be read more than
    #: one way (a numeric date, a range of bare ordinals, early/mid/late), or
    #: the date an event anchor's own words gave. Printed, so it can be corrected.
    reading: str | None = None
    #: Q-2 — the event anchor the question names (``consult_window_anchor``):
    #: ``{"words", "phrase", "relation", "resolved", ["date", "how"], ["reason"]}``.
    #: ``resolved`` False = asked for and NOT resolved; the window above stands.
    anchor: dict[str, Any] | None = None

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def days(self, *, now: datetime | None = None) -> list[date]:
        """Every UTC calendar day the window touches (capped at ``now``)."""
        last = min(self.end, _utc(now)) if now else self.end
        out: list[date] = []
        d = self.start.date()
        while datetime(d.year, d.month, d.day, tzinfo=timezone.utc) < last:
            out.append(d)
            d += timedelta(days=1)
        return out or [self.start.date()]

    def contains(self, at: datetime) -> bool:
        return self.start <= _utc(at) <= self.end

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "hours": round(self.hours, 1),
            "source": self.source,
            "basis": self.basis,
            "deictic": self.deictic,
            **({"console_rejected": self.console_rejected} if self.console_rejected else {}),
            **({"reading": self.reading} if self.reading else {}),
            **({"anchor": dict(self.anchor)} if self.anchor else {}),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "TurnWindow | None":
        """The window a turn STORED (``as_dict``), or ``None`` when unreadable.

        A recovered turn (``synthesize_from``) answers under the window its
        original turn resolved, at that turn's instant — re-resolving "the last
        48 hours" at recovery time would move it.
        """
        if not isinstance(raw, Mapping):
            return None
        start, end = _parse_iso(raw.get("start")), _parse_iso(raw.get("end"))
        source = str(raw.get("source") or "")
        if start is None or end is None or start >= end or source not in _SOURCE_WORDS:
            return None
        anchor = raw.get("anchor")
        return cls(
            start, end, source, str(raw.get("basis") or ""),
            deictic=bool(raw.get("deictic")),
            console_rejected=str(raw["console_rejected"]) if raw.get("console_rejected") else None,
            reading=str(raw["reading"]) if raw.get("reading") else None,
            anchor=dict(anchor) if isinstance(anchor, Mapping) else None,
        )


def _relative_span(amount: str | None, unit: str) -> timedelta:
    n = 1
    if amount:
        n = int(amount) if amount.isdigit() else _NUM_WORDS.get(amount.lower(), 1)
    unit = unit.lower()
    if unit.startswith("h"):
        return timedelta(hours=n)
    if unit.startswith("w"):
        return timedelta(weeks=n)
    if unit.startswith("m"):
        return timedelta(days=30 * n)
    return timedelta(days=n)


def parse_user_window(question: str, *, now: datetime) -> tuple[datetime, datetime, str] | None:
    """``(start, end, basis)`` when the question STATES a window, else ``None``.

    Only phrases that name a window count: a bare date ("by 2026-12-31" in a
    forecast) is never read as one. An end date is inclusive (to the end of
    that UTC day). :func:`resolve_window` also carries the reading and the
    event anchor (Q-2); this keeps the three-part answer.
    """
    stated = _parse_stated(question or "", now=_utc(now), anchor=None)
    return None if stated is None else stated[:3]


_Stated = tuple  # (start, end, basis, reading | None)


def _parse_stated(text: str, *, now: datetime, anchor: Anchor | None) -> _Stated | None:
    """``(start, end, basis, reading)`` in the fixed order of the module doc.

    A DATED event anchor ("since the strike on 3 Nov") is read after every
    strong phrasing and before the weak "on <date>" / "in <month>" ones, which
    would otherwise take the anchor's own date as a one-day window.
    """
    m = _RANGE_RE.search(text)
    if m:
        start = _instant(m.group(1), m.group(2))
        end = _instant(m.group(3), m.group(4))
        if not m.group(4):
            end += timedelta(days=1)
        if end > start:
            return start, end, m.group(0).strip(), None
    m = _SINCE_RE.search(text)
    if m:
        start = _instant(m.group(1), m.group(2))
        if start < now:
            return start, now, m.group(0).strip(), None
    # Dates and spans in WORDS ("from 3 Nov to 10 Nov", "since Tuesday", "the
    # past fortnight", "3/11 to 10/11", "mid-November") —
    # ``consult_window_words``; the weak "on"/"in" forms last.
    worded = parse_words(text, now=now)
    if worded is not None:
        return (*worded, None) if len(worded) == 3 else worded
    m = _RELATIVE_RE.search(text)
    if m:
        return now - _relative_span(m.group(1), m.group(2)), now, m.group(0).strip(), None
    m = _NAMED_RE.search(text)
    if m:
        word = m.group(1).lower()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if word == "today":
            return midnight, now, m.group(0), None
        if word == "yesterday":
            return midnight - timedelta(days=1), midnight, m.group(0), None
        if word == "this week":
            return midnight - timedelta(days=now.weekday()), now, m.group(0), None
        if word == "this month":
            return midnight.replace(day=1), now, m.group(0), None
        if word in ("last week", "past week"):
            return now - timedelta(days=7), now, m.group(0), None
        return now - timedelta(days=30), now, m.group(0), None
    if anchor is not None and anchor.on is not None:
        at = datetime(anchor.on.year, anchor.on.month, anchor.on.day, tzinfo=timezone.utc)
        if at < now:
            reading = (f"anchored on the date your words give for the event, "
                       f"{anchor.on.isoformat()}" + (f" ({anchor.reading})" if anchor.reading else ""))
            if anchor.relation == "start":
                return at, now, anchor.words, reading
            hours = default_window_hours()
            return (at - timedelta(hours=hours), at, anchor.words,
                    reading + f"; the window closes there and keeps the {hours}h default span before it")
    if anchor is not None:
        # The weak forms never read a date out of an anchor's own words.
        a, b = anchor.span
        text = text[:a] + " " * (b - a) + text[b:]
    m = _ON_RE.search(text)
    if m:
        start = _instant(m.group(1), None)
        return start, start + timedelta(days=1), m.group(0).strip(), None
    late = parse_words(text, now=now, late=True)
    return None if late is None else (*late, None)


def _console_window(raw: Any, *, now: datetime) -> tuple[tuple[datetime, datetime, str] | None, str | None]:
    """``(window, rejected_reason)`` off ``inputs[0]["window"]``."""
    if raw is None:
        return None, None
    if not isinstance(raw, Mapping):
        return None, f"not an object ({type(raw).__name__})"
    end = _parse_iso(raw.get("end")) or now
    start = _parse_iso(raw.get("start"))
    if start is None and raw.get("hours") is not None:
        try:
            hours = float(raw["hours"])
        except (TypeError, ValueError):
            return None, f"hours {raw.get('hours')!r} is not a number"
        if hours <= 0:
            return None, "hours must be positive"
        start = end - timedelta(hours=hours)
    if start is None:
        return None, "neither start nor hours"
    if start >= end:
        return None, "start is not before end"
    label = str(raw.get("label") or "").strip()[:60]
    hours = (end - start).total_seconds() / 3600.0
    return (start, end, label or f"console {hours:g}h"), None


#: Why an event anchor with no date in its words is not resolved (printed).
ANCHOR_NOT_RESOLVED = (
    "the platform does not date a named event by guessing or by a model, and the "
    "question gives no date for it"
)


def resolve_window(
    question: str, console: Any = None, *, now: datetime | None = None,
) -> TurnWindow:
    """The window in force: the user's words, else the console's, else the default.

    Q-2: an event anchor ("since the ceasefire") is detected and, unless its
    own words carry a date, DECLARED unresolved on the window (``anchor``),
    while the precedence above stands unchanged.
    """
    now = _utc(now or datetime.now(timezone.utc))
    deictic = bool(_DEICTIC_RE.search(question or ""))
    found = find_anchor(question or "", now=now)
    stated = _parse_stated(question or "", now=now, anchor=found)
    console_win, rejected = _console_window(console, now=now)
    anchor: dict[str, Any] | None = None
    if found is not None:
        used = stated is not None and stated[2] == found.words
        reason = None
        if not used:
            reason = (
                found.unused_date or (
                    "a window stated earlier in the question's own words is in force"
                    if found.on is not None else ANCHOR_NOT_RESOLVED
                )
            )
        anchor = found.as_dict(resolved=used, reason=reason)
    if stated is not None:
        return TurnWindow(stated[0], stated[1], SOURCE_USER_WORDS, stated[2],
                          deictic=deictic, console_rejected=rejected,
                          reading=stated[3], anchor=anchor)
    if console_win is not None:
        return TurnWindow(console_win[0], console_win[1], SOURCE_CONSOLE, console_win[2],
                          deictic=deictic, anchor=anchor)
    hours = default_window_hours()
    return TurnWindow(now - timedelta(hours=hours), now, SOURCE_DEFAULT,
                      f"{hours}h default", deictic=deictic, console_rejected=rejected,
                      anchor=anchor)


# ---------------------------------------------------------------------------
# Narrowing / widening, against the rows the answer cites
# ---------------------------------------------------------------------------


@dataclass
class Reach:
    """How the answer's citations sit against the window in force."""

    cited_dated: int = 0
    in_window: int = 0
    before_window: int = 0
    after_window: int = 0
    span_start: datetime | None = None
    span_end: datetime | None = None
    earliest_outside: datetime | None = None
    narrowed: str | None = None          # None | "declared" | "undeclared"
    narrowed_reason: str | None = None
    uncited_edge_days: list[str] = field(default_factory=list)
    wider_reach: str | None = None       # None | "declared" | "undeclared"
    wider_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        iso = lambda d: d.isoformat() if d else None  # noqa: E731
        return {
            "cited_dated": self.cited_dated,
            "in_window": self.in_window,
            "before_window": self.before_window,
            "after_window": self.after_window,
            "span_start": iso(self.span_start),
            "span_end": iso(self.span_end),
            "earliest_outside": iso(self.earliest_outside),
            "narrowed": self.narrowed,
            "narrowed_reason": self.narrowed_reason,
            "uncited_edge_days": list(self.uncited_edge_days),
            "wider_reach": self.wider_reach,
            "wider_reason": self.wider_reason,
        }


def assess_reach(
    window: TurnWindow,
    cited_dates: Iterable[datetime],
    *,
    rows_by_day: Mapping[str, int] | None = None,
    declared: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Reach:
    """Compare the citation span with the window in force.

    ``rows_by_day`` (ISO day → rows the record holds that day; from the
    coverage tool) decides whether an uncited edge is a narrowing (rows
    existed and were not used) or sparse data (nothing there to cite). Unknown
    coverage (``None``) counts an uncited edge as a narrowing — the answer
    cannot show it was sparse. ``declared`` is the model's contract block:
    ``window_used`` {start, end, reason} and ``wider_reach`` (a reason string).
    """
    now = _utc(now or datetime.now(timezone.utc))
    dates = sorted(_utc(d) for d in cited_dates if d is not None)
    reach = Reach(cited_dated=len(dates))
    inside = [d for d in dates if window.contains(d)]
    reach.in_window = len(inside)
    before = [d for d in dates if d < window.start]
    reach.before_window = len(before)
    reach.after_window = len([d for d in dates if d > window.end])
    if before:
        reach.earliest_outside = before[0]
    declared = declared or {}
    used = declared.get("window_used") if isinstance(declared.get("window_used"), Mapping) else None
    used_reason = str((used or {}).get("reason") or "").strip() or None
    wider = declared.get("wider_reach")
    wider_reason = str(wider).strip() if isinstance(wider, str) and wider.strip() else None

    if inside:
        reach.span_start, reach.span_end = inside[0], inside[-1]
    end = min(window.end, now)
    edges: list[str] = []
    if window.end - window.start > EDGE_TOLERANCE:
        lead_until = reach.span_start or end
        trail_from = reach.span_end or window.start
        for day in window.days(now=now):
            day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            day_end = min(day_start + timedelta(days=1), end)
            leading = day_end <= lead_until and lead_until - window.start >= EDGE_TOLERANCE
            trailing = day_start >= trail_from and end - trail_from >= 2 * EDGE_TOLERANCE
            if not (leading or trailing):
                continue
            held = None if rows_by_day is None else int(rows_by_day.get(day.isoformat(), 0))
            if held is None or held > 0:
                edges.append(day.isoformat())
    declared_narrow = False
    if used is not None:
        u_start, u_end = _parse_iso(used.get("start")), _parse_iso(used.get("end"))
        if (u_start and u_start - window.start > timedelta(hours=12)) or (
            u_end and end - u_end > timedelta(hours=12)
        ):
            declared_narrow = True
    if declared_narrow or edges:
        reach.uncited_edge_days = edges
        reach.narrowed = "declared" if (declared_narrow and used_reason) else "undeclared"
        # A reason counts only when the model declared a NARROWER window_used;
        # one attached to a full-window declaration explains nothing here.
        reach.narrowed_reason = used_reason if declared_narrow else None
    if before:
        reach.wider_reach = "declared" if wider_reason else "undeclared"
        reach.wider_reason = wider_reason
    return reach


_SOURCE_WORDS = {
    SOURCE_USER_WORDS: "taken from your words",
    SOURCE_CONSOLE: "the console's pinned window",
    SOURCE_DEFAULT: "the declared default (no window in your words or from the console)",
}


def _fmt(at: datetime) -> str:
    return _utc(at).strftime("%Y-%m-%d %H:%M")


def _anchor_lines(window: TurnWindow) -> list[str]:
    """The plain statement of an event anchor that was asked for and not resolved."""
    a = window.anchor or {}
    if not a or a.get("resolved"):
        return []
    stands = {
        SOURCE_USER_WORDS: "the window stated in your words",
        SOURCE_CONSOLE: "the console's window",
        SOURCE_DEFAULT: "the declared default",
    }[window.source]
    return [
        f"> **Event-anchored window not resolved:** the question asks for the window "
        f"\"{a.get('words')}\"; it was not resolved ({a.get('reason') or ANCHOR_NOT_RESOLVED}), "
        f"so {stands} above is in force. Give the event's date (for example \"since 3 Nov\") to anchor it."
    ]


def render_window_lines(window: TurnWindow, reach: Reach | None = None) -> str:
    """The fixed, visible window statement the answer leads with (code-built)."""
    lines = [
        f"> **Window in force:** {_fmt(window.start)} → {_fmt(window.end)} UTC "
        f"({window.hours / 24:.1f} days) — {_SOURCE_WORDS[window.source]}"
        + (f": \"{window.basis}\"" if window.source == SOURCE_USER_WORDS else "")
        + "."
    ]
    if window.deictic and window.source == SOURCE_DEFAULT:
        lines.append(
            "> The question refers to \"this window\", but no console window reached "
            "this turn, so the default applied."
        )
    if window.reading:
        lines.append(f"> **Read as:** {window.reading}.")
    lines += _anchor_lines(window)
    if window.console_rejected:
        lines.append(f"> A console window arrived but was unreadable ({window.console_rejected}).")
    if reach is not None and reach.narrowed:
        span = (
            f"{_fmt(reach.span_start)} → {_fmt(reach.span_end)} UTC"
            if reach.span_start and reach.span_end else "no day of the window"
        )
        edge = (
            f" Days in the window with rows but no citation at its edges: "
            f"{', '.join(reach.uncited_edge_days)}." if reach.uncited_edge_days else ""
        )
        if reach.narrowed == "declared":
            lines.append(
                f"> **Narrowed (declared):** the answer draws on {span}; its reason: "
                f"{reach.narrowed_reason}.{edge}"
            )
        else:
            lines.append(
                f"> **Narrowed (not declared):** the answer's citations span {span} only, "
                f"and it gave no reason for using less than the window in force.{edge}"
            )
    if reach is not None and reach.wider_reach:
        why = (f" Reason given: {reach.wider_reason}." if reach.wider_reason
               else " No reason was given.")
        lines.append(
            f"> **Wider reach (context only):** {reach.before_window} cited row(s) are "
            f"dated before the window, the earliest {_fmt(reach.earliest_outside)} UTC."
            + why
        )
    return "\n".join(lines)
