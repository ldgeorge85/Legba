# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WORLD READ SELECTION RULES — the politics floor's eligibility test and the
cross-unit shared thread (N-C, 2026-11-11). Deterministic; no model, no clock,
no connection.

Built from the operator's review of world record fd6584eb (2026-11-11 12:21Z),
items WR5 and WR8, traced in the 11-11b quality sweep:

  1. THE POLITICS FLOOR (WR8). The restraint-shift floor admitted a political
     block only when ``reports_change`` — a LEXICAL change detector — fired on it.
     A blocked impeachment reads as "no change" to that detector, so the US
     internal-stability block (the House's 211-204 Hegseth referral vote, cited
     mass 0.80, the heaviest political block on the roster) lost its slot to a
     Pakistan block at 0.05. The floor now asks a different question: does the
     block NAME A DATED INSTITUTIONAL EVENT — a vote, a referral, an arrest, a
     resignation, an appointment — in a clause that is not negated? Among the
     blocks that do, cited mass decides (``assembly_world.plan_world_carry``).

  2. THE SHARED THREAD (WR5). The world read carries one block per unit under a
     16-slot cap, and nothing joined one event across units: the DOJ indictment of
     three Iran-backed operatives over a plot against a UK air base was in the US
     escalation head's citations and the UK heads', and the UK composition ranked
     18th. :func:`cross_unit_threads` finds every cited signal that heads in two or
     more units rest on; :func:`pick_thread_unit` chooses ONE uncarried unit that
     shares a thread with a carried one, and the plan gives it a slot.

PLATFORM, NOT EXEMPLAR. The event lexicon is a named constant
(:data:`INSTITUTIONAL_EVENT_RE`) and the desks it applies to are the caller's
(``assembly_world.RESTRAINT_SHIFT_DESKS``); the mechanism is "a dated,
non-negated institution-acts clause" and "a signal two units' heads both cite".
"""

from __future__ import annotations

import re
from typing import Any, Collection, Iterable, Mapping, Sequence

from ..provenance.text_fold import normalize_for_match
from .assembly_carry import block_is_absence, block_mass, child_assembly
from .assembly_spans import quoted_spans

__all__ = [
    "DATE_RE",
    "INSTITUTIONAL_EVENT_RE",
    "POLITICS_FLOOR_RULE",
    "THREAD_LINES_MAX",
    "THREAD_MAGNITUDE_FLOOR",
    "THREAD_SLOTS",
    "THREAD_VERSION",
    "block_names_dated_institutional_event",
    "cross_unit_threads",
    "names_dated_institutional_event",
    "pick_thread_unit",
    "thread_block_for",
]

#: The rule id the carry plan's restraint-floor audit line publishes.
POLITICS_FLOOR_RULE: str = "dated_institutional_event.v1"

# ---------------------------------------------------------------------------
# 1. A DATED INSTITUTIONAL EVENT
# ---------------------------------------------------------------------------

#: Acts an institution performs on a person or a mandate. Deliberately NOT
#: "coup" (desk boilerplate: "keeping coup vulnerability checked"), "strike" or
#: "protest" (street events, not institutional acts), or "summit" / "chair"
#: (routine head-of-state business, which a leadership desk reports as stasis).
INSTITUTIONAL_EVENT_RE = re.compile(
    r"\b(?:vot(?:e|es|ed|ing)|referr(?:al|ed)|refer(?:s)?|impeach\w*|"
    r"arrest\w*|detain\w*|detention\w*|resign\w*|appoint\w*|nominat\w*|"
    r"elect(?:s|ed|ion|ions)?|re-elect\w*|dismiss\w*|sack(?:s|ed)?|oust\w*|"
    r"sworn|inaugurat\w*|no-confidence|censure\w*|indict\w*|convict\w*|"
    r"decree\w*|dissol\w*|coup attempt|mandate|replac(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
_WEEKDAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"

#: A date: "10 November", "10-11 November", "November 10", an ISO date, "on
#: Tuesday", "yesterday", or a dated offset ("three days after the midterm
#: defeat" — the live Miller-for-Wiles swap is dated only that way).
DATE_RE = re.compile(
    rf"\b(?:\d{{1,2}}(?:\s*[-–]\s*\d{{1,2}})?\s+(?:{_MONTHS})|(?:{_MONTHS})\s+\d{{1,2}}"
    rf"|20\d\d-\d\d-\d\d|on\s+(?:{_WEEKDAYS})|yesterday"
    rf"|(?:\d+|one|two|three|four|five|six|seven|a)\s+(?:days?|weeks?)\s+(?:after|before|ago))\b",
    re.IGNORECASE,
)

#: A date that stamps the desk's COLLECTION WINDOW or its prior read rather than
#: an event: "through 11 November", "since 9 November", "the 10 November prior
#: read". Every desk BLUF carries one, so it can never date an event.
_WINDOW_DATE_PREFIX_RE = re.compile(
    r"(?:through|thru|until|since|as of|by)\s*$", re.IGNORECASE
)
_WINDOW_DATE_SUFFIX_RE = re.compile(r"^\s*(?:\d{4}\s+)?(?:prior\s+)?read\b", re.IGNORECASE)

#: A negation BEFORE the event word in its clause voids it ("no signal ...
#: reports a no-confidence or impeachment move"). ``no-`` as a prefix
#: ("no-confidence") is the event's own spelling, not a negation.
_NEGATION_RE = re.compile(r"\b(?:no(?!-)|not|never|nor|without|absent)\b", re.IGNORECASE)

#: Clause boundaries: punctuation, a spaced dash, and the coordinators every desk
#: BLUF hangs its provenance clause on ("…, while no mass unrest … appears").
_CLAUSE_SPLIT_RE = re.compile(
    r"[;,()\[\]]|\s[—–-]\s|—|\bwhile\b|\bbut\b|\byet\b|\bwhereas\b",
    re.IGNORECASE,
)


def _event_dates(clause: str) -> list[re.Match[str]]:
    out = []
    for m in DATE_RE.finditer(clause):
        if _WINDOW_DATE_PREFIX_RE.search(clause[: m.start()]):
            continue
        if _WINDOW_DATE_SUFFIX_RE.match(clause[m.end():]):
            continue
        out.append(m)
    return out


def names_dated_institutional_event(text: Any) -> bool:
    """Does ``text`` name an institutional act with a calendar date in the same,
    non-negated clause? Total: ``False`` on anything it cannot read.

    *"…as the House blocked the Hegseth impeachment on 10 November by a 211-204
    referral vote…"* — yes. *"…no signal in this desk's collection through 11
    November reports a no-confidence or impeachment move…"* — no (negated, and
    the only date is the collection window's). *"…the SAF's detention of Ahmed
    Haroun is now publicly disputed…"* — no (undated).
    """
    folded = normalize_for_match(text)
    if not folded:
        return False
    for clause in _CLAUSE_SPLIT_RE.split(folded):
        if not clause or not clause.strip():
            continue
        dates = _event_dates(clause)
        if not dates:
            continue
        for ev in INSTITUTIONAL_EVENT_RE.finditer(clause):
            if _NEGATION_RE.search(clause[: ev.start()]):
                continue
            return True
    return False


def block_names_dated_institutional_event(block: Mapping[str, Any]) -> bool:
    """The block form: its LEAD quoted span, and never an absence block."""
    if not isinstance(block, Mapping) or block_is_absence(block):
        return False
    spans = quoted_spans(block)
    if not spans or not isinstance(spans[0], Mapping):
        return False
    return names_dated_institutional_event(spans[0].get("text"))


# ---------------------------------------------------------------------------
# 2. THE CROSS-UNIT SHARED THREAD
# ---------------------------------------------------------------------------

THREAD_VERSION: str = "cross_unit_thread.v1"

#: A shared signal below this salience magnitude is not a thread: on fd6584eb
#: the 0.30 shared items were bilateral courtesy calls ("Turkish, Brazilian
#: presidents discuss bilateral ties"), the 0.60 ones the DOJ indictment and the
#: Mildenhall relocation.
THREAD_MAGNITUDE_FLOOR: float = 0.5

#: World slots a shared thread may claim for a unit the order put below the cut.
THREAD_SLOTS: int = 1

#: Thread lines published on the record (heaviest first).
THREAD_LINES_MAX: int = 8

_TERM_RE = re.compile(r"[a-z][a-z'-]{3,}")
_TERM_STOP = frozenset({
    "says", "said", "over", "with", "from", "into", "amid", "after", "about",
    "that", "this", "their", "they", "will", "have", "been", "more", "than",
    "report", "reports", "reported", "news", "live", "update", "updates",
})


def _terms(text: Any) -> set[str]:
    out = set()
    for t in _TERM_RE.findall(normalize_for_match(text).lower()):
        t = t.strip("'-")
        if len(t) < 4 or t in _TERM_STOP:
            continue
        out.add(t[:-1] if t.endswith("s") and len(t) > 4 else t)
    return out


def _unit_of(row: Mapping[str, Any], block: Mapping[str, Any]) -> str:
    return str(block.get("target_id") or row.get("target_id") or "")


def _magnitude(sig: Mapping[str, Any]) -> float:
    try:
        return float(sig.get("salience_magnitude") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def cross_unit_threads(
    rows: Sequence[Mapping[str, Any]],
    *,
    magnitude_floor: float = THREAD_MAGNITUDE_FLOOR,
) -> list[dict[str, Any]]:
    """Every signal cited by heads in TWO OR MORE units, grouped by the unit set.

    Read off the candidates' own child assemblies (``blocks[].signals[]`` is the
    origin desk head's citation list), so nothing is fetched and nothing is
    re-scored. A unit is the block's ``target_id`` (a thematic candidate's
    blocks are country desk heads, so they count for their country, never for
    the thematic lane). Ordered heaviest first: units, then top magnitude, then
    signal count, then the unit list — total and reproducible.
    """
    by_sig: dict[str, dict[str, Any]] = {}
    for row in rows:
        child = child_assembly(row)
        for block in (child or {}).get("blocks") or ():
            if not isinstance(block, Mapping):
                continue
            unit = _unit_of(row, block)
            if not unit:
                continue
            for sig in block.get("signals") or ():
                if not isinstance(sig, Mapping) or not sig.get("signal_id"):
                    continue
                sid = str(sig["signal_id"])
                entry = by_sig.setdefault(sid, {
                    "units": set(), "title": str(sig.get("title") or ""),
                    "source_id": sig.get("source_id"), "magnitude": 0.0,
                })
                entry["units"].add(unit)
                entry["magnitude"] = max(entry["magnitude"], _magnitude(sig))
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for sid, e in by_sig.items():
        if len(e["units"]) < 2 or e["magnitude"] < float(magnitude_floor):
            continue
        key = tuple(sorted(e["units"]))
        g = grouped.setdefault(key, {
            "units": list(key), "signal_ids": [], "titles": [], "max_magnitude": 0.0,
        })
        g["signal_ids"].append(sid)
        g["titles"].append(e["title"])
        g["max_magnitude"] = round(max(g["max_magnitude"], e["magnitude"]), 4)
    out = []
    for g in grouped.values():
        order = sorted(range(len(g["signal_ids"])), key=lambda i: g["signal_ids"][i])
        g["signal_ids"] = [g["signal_ids"][i] for i in order]
        g["titles"] = [g["titles"][i] for i in order]
        out.append(g)
    out.sort(key=lambda g: (
        -len(g["units"]), -g["max_magnitude"], -len(g["signal_ids"]), g["units"],
    ))
    return out


def pick_thread_unit(
    threads: Sequence[Mapping[str, Any]],
    *,
    carried_units: Collection[str],
    candidates_by_unit: Mapping[str, Mapping[str, Any]],
    rank: Mapping[str, int],
) -> tuple[str, list[Mapping[str, Any]]] | None:
    """The ONE uncarried unit a shared thread earns a slot for, with the threads
    that earn it, or ``None``.

    Eligible: a unit that has its own candidate row, is not carried, and shares a
    thread with at least one CARRIED unit (a thread between two uncarried units
    is a line, not a slot). Ranked by the heaviest such thread, then the number
    of carried units it joins, then the candidate's pure order rank.
    """
    carried = {str(u) for u in carried_units}
    best: tuple[tuple[float, int, int, str], str, list[Mapping[str, Any]]] | None = None
    for unit, row in candidates_by_unit.items():
        if unit in carried:
            continue
        joined = [
            t for t in threads
            if unit in t["units"] and any(u in carried for u in t["units"] if u != unit)
        ]
        if not joined:
            continue
        partners = {u for t in joined for u in t["units"] if u != unit and u in carried}
        key = (
            -max(float(t["max_magnitude"]) for t in joined),
            -len(partners),
            int(rank.get(str(row.get("id") or ""), 10**6)),
            unit,
        )
        if best is None or key < best[0]:
            best = (key, unit, joined)
    return None if best is None else (best[1], best[2])


def _bluf_markers(block: Mapping[str, Any]) -> set[str]:
    spans = quoted_spans(block)
    if not spans or not isinstance(spans[0], Mapping):
        return set()
    return {str(m) for m in spans[0].get("markers") or ()}


def thread_block_for(
    row: Mapping[str, Any],
    threads: Iterable[Mapping[str, Any]],
    *,
    exclude: Collection[str] = (),
) -> Mapping[str, Any] | None:
    """The block of ``row``'s child assembly that CARRIES the thread.

    Among the child's blocks that cite a thread signal (and are not taken
    elsewhere, nor absences): the one whose quoted sentence cites a thread
    signal by marker, then the one whose sentence shares most words with the
    thread's headlines, then the heaviest by cited mass, then the lowest
    ordinal. On fd6584eb this is the UK escalation read that leads with the
    Mildenhall relocation and the Fairford indictment, not the UK maritime read
    that cites the same item below its lead.
    """
    sids: set[str] = set()
    titles: list[str] = []
    for t in threads:
        sids.update(str(s) for s in t.get("signal_ids") or ())
        titles.extend(str(x) for x in t.get("titles") or ())
    head_terms = set().union(*(_terms(t) for t in titles)) if titles else set()
    taken = {str(x) for x in exclude}
    scored = []
    for block in (child_assembly(row) or {}).get("blocks") or ():
        if not isinstance(block, Mapping):
            continue
        if str(block.get("finding_id") or "") in taken or block_is_absence(block):
            continue
        cites = {
            str(s.get("marker") or ""): str(s.get("signal_id") or "")
            for s in block.get("signals") or () if isinstance(s, Mapping)
        }
        if not sids & set(cites.values()):
            continue
        in_lead = any(cites.get(m) in sids for m in _bluf_markers(block))
        spans = quoted_spans(block)
        lead_text = spans[0].get("text") if spans and isinstance(spans[0], Mapping) else ""
        overlap = len(_terms(lead_text) & head_terms)
        scored.append((
            (-int(in_lead), -overlap, -block_mass(block), int(block.get("ordinal") or 0)),
            block,
        ))
    if not scored:
        return None
    scored.sort(key=lambda kb: kb[0])
    return scored[0][1]
