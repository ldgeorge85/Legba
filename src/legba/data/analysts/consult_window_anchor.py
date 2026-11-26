# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""EVENT-ANCHORED windows — detected, never guessed — pure, no I/O.

An asker often bounds a question by an event rather than a date: "since the
ceasefire", "after the summit", "since the strike on the port", "before the
vote". Dating that event is a judgement about the world: a model asked to do
it would pick a date and the window would silently rest on that pick. So the
platform does not resolve an anchor by a model or by a guess. The rule:

* an anchor whose OWN WORDS carry a date ("since the strike on 3 Nov", "after
  the summit of 2026-11-05", "since the outage on 3/11") is anchored on that
  date — the asker gave it; the numeric rule of ``consult_window_words``
  applies and the reading is stated;
* any other anchor is DECLARED and not resolved: the default or explicit
  window stays in force and the answer says plainly that an event-anchored
  window was asked for and not resolved, and how to anchor it (give a date).

No lookup by event title is made: the record's ``events`` table carries
titles a producer wrote ("Ceasefire takes effect along the line"), an exact
match on the asker's words would almost never hit, and a fuzzy one is a guess.

Relations: ``since`` / ``after`` / ``following`` / ``in the wake of`` open
the window at the anchor (``start``); ``before`` / ``prior to`` / ``ahead of``
/ ``in the run-up to`` close it there (``end``) — a dated end anchor keeps the
default span before it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from .consult_window_words import date_in

__all__ = ["Anchor", "find_anchor"]

_START = ("since", "after", "following", "in the wake of")
_ANCHOR_RE = re.compile(
    r"\b(since|after|following|in\s+the\s+wake\s+of|before|prior\s+to|ahead\s+of"
    r"|in\s+the\s+run[- ]up\s+to)\s+"
    r"(the|this|that|last\s+week[’']?s|yesterday[’']?s|today[’']?s)\s+"
    r"([^\n,;:?!()\"“”]{2,160})",
    re.IGNORECASE,
)
#: The phrase ends at the first of these (the question carries on past it).
_STOP = frozenset({
    "and", "or", "but", "what", "which", "how", "why", "when", "who", "whom",
    "whose", "has", "have", "had", "is", "are", "was", "were", "did", "do",
    "does", "will", "would", "could", "should", "can", "may", "might", "to",
    "for", "than", "that", "so", "then", "—", "–", "-", "until", "till",
    "through", "compared", "versus", "vs",
})
#: A head word that names a TIME, not an event: "since the start of the
#: month", "after the weekend", "before the end of the window" are windows (or
#: nothing), never anchors.
_TIME_HEADS = frozenset({
    "start", "beginning", "end", "middle", "week", "weekend", "month", "year",
    "day", "days", "hour", "hours", "past", "last", "previous", "next", "window",
    "period", "span", "morning", "evening", "night", "afternoon", "fortnight",
    "quarter", "first", "second", "record", "data", "time", "moment", "same",
    "latest", "most", "turn", "question", "answer", "report", "above", "below",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})
_MAX_WORDS = 8


@dataclass(frozen=True)
class Anchor:
    """An event anchor the question names."""

    #: The asker's words, from the relation on ("since the ceasefire").
    words: str
    #: The event phrase alone ("ceasefire", "strike on the port").
    phrase: str
    #: ``start`` (the window opens at the event) | ``end`` (it closes there).
    relation: str
    #: The date the asker's own words give for the event, else ``None``.
    on: date | None = None
    #: How a stated date was read (the numeric rule), else ``None``.
    reading: str | None = None
    #: Why a date in the anchor's words was not used, else ``None``.
    unused_date: str | None = None
    #: ``(start, end)`` of the anchor in the question — the weak window forms
    #: ("on 20 Nov") never read a date out of an anchor's own words.
    span: tuple[int, int] = (0, 0)

    def as_dict(self, *, resolved: bool, reason: str | None = None) -> dict[str, object]:
        out: dict[str, object] = {
            "words": self.words, "phrase": self.phrase, "relation": self.relation,
            "resolved": resolved,
        }
        if self.on is not None:
            out["date"] = self.on.isoformat()
            out["how"] = "date_in_question"
        if self.reading:
            out["reading"] = self.reading
        if reason:
            out["reason"] = reason
        return out


#: A word that starts a TIME phrase after the event ("the summit last week",
#: "the ceasefire in the last 48 hours"): the event phrase ends before it.
_TIME_TAIL = frozenset({"last", "past", "previous", "today", "yesterday", "since",
                        "during", "over", "within", "this"})
_TRAILING = frozenset({"in", "on", "at", "of", "the", "a", "an", "by"})


def _phrase(raw: str) -> tuple[str, int]:
    """``(phrase, characters of raw it spans)``."""
    words: list[str] = []
    ends: list[int] = []
    for m in re.finditer(r"\S+", raw):
        w = m.group(0)
        bare = w.strip(".'’\"").lower()
        if words and (bare in _STOP or bare in _TIME_TAIL):
            break
        words.append(w.rstrip("."))
        ends.append(m.end())
        if len(words) >= _MAX_WORDS:
            break
        if w.endswith(".") and not re.search(r"\d\.?$|^[A-Z][a-z]{2,3}\.$", w):
            break  # a full stop ends the phrase ("…the summit. What…")
    while len(words) > 1 and words[-1].lower() in _TRAILING:
        words.pop()
    return " ".join(words).strip(), (ends[len(words) - 1] if words else 0)


def find_anchor(question: str, *, now: datetime) -> Anchor | None:
    """The first event anchor the question names, or ``None``."""
    for m in _ANCHOR_RE.finditer(question or ""):
        phrase, used = _phrase(m.group(3))
        head = re.sub(r"[^a-z0-9]", "", (phrase.split() or [""])[0].lower())
        if not phrase or not head or head in _TIME_HEADS or head[0].isdigit():
            continue
        relation_word = re.sub(r"\s+", " ", m.group(1).lower())
        relation = "start" if relation_word in _START else "end"
        words = f"{relation_word} {m.group(2)} {phrase}"
        span = (m.start(), m.start(3) + used)
        found = date_in(phrase, now=now)
        if found is not None:
            # A year-less date is this year, or last year when this year's is
            # to come — right for "since", a guess for "before the vote on 20
            # Nov" (an event still ahead). Not used; said.
            ahead = found[0] > now.date() or (
                relation == "end" and found[0].year < now.year
                and not re.search(r"\b\d{4}\b", phrase)
            )
            if ahead:
                return Anchor(words, phrase, relation, span=span,
                              unused_date="the date in its words is still to come")
            return Anchor(words, phrase, relation, on=found[0], reading=found[1], span=span)
        return Anchor(words, phrase, relation, span=span)
    return None
