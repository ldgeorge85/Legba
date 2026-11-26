# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source drift ASSESSMENT — what kind of change a classified page change is.
Written ONCE per classified change and reused by every unit that cites the
page. This fills ``CLAIM_JUDGE_SEAM`` (SEAMS row 79).

:mod:`legba.data.evidence_drift_classify` says HOW MUCH a page changed
(the class). This module says WHAT KIND of change it was, for the four
flag-writing classes (substantive_edit, title_changed, retracted_or_replaced,
gone), in code first:

OBSERVED (what the two versions show)

* ``type`` — the granular closed list :data:`CHANGE_TYPES`; ``mixed`` carries
  its component types; ``unassessable`` carries a reason.
* ``acknowledged`` — the HIGHEST of four levels, decided in code from the
  stored text and bytes (:func:`acknowledgement`): ``correction_notice`` (the
  text says what changed, or a correction/retraction marker) >
  ``visible_update_stamp`` (an "Updated ..." chip or update line a reader
  sees) > ``metadata_date_only`` (only the page's hidden modified date moved)
  > ``silent``; every signal that fired is kept beside it.
* ``scope`` — the claims that changed as bounded before -> after pairs, how
  many, and the share of the piece changed (D-1's retention).
* ``timing`` — hours between the earlier version's publication (else first
  read) and the later version, bucketed (:data:`TIMING_BUCKETS`).
* ``summary`` — one plain sentence a desk prompt can carry.

DERIVED (code only, never asked of a model), each with its inputs:

* ``history_rewrite`` — a claim removed, reversed or corrected AND late
  (:data:`HISTORY_REWRITE_LATE_HOURS`) AND not acknowledged at a level in
  :data:`ACKNOWLEDGING_LEVELS` (rule :data:`ACK_RULE_VERSION`: a correction
  notice or a visible update stamp counts; a hidden metadata date does not);
* ``late_unexplained_change`` — a claim removed, reversed or corrected AND
  late AND no correction notice (a stamp or a metadata date alone does not
  explain WHAT changed).

The TYPE is set by code alone. Nothing here states or infers WHY a source
changed a page. The model step (:mod:`legba.data.evidence_drift_assess_model`,
behind its own switch) writes only the summary, may raise a doubt about the
code type (stored beside it, never over it) and answers the not-checkable
citers; it has no motive field and its prompt forbids one.

Per CITER (:func:`claim_effect`): whether the cited text survives, is gone,
or is one side of a changed pair (with the after-text) — from D-1's exact
span check and the full pairing. ``not_checkable`` when there is no span or
the span was never in the page text.

Stdlib + the D-1 classifier only (the registry's slim image imports the
reads that use these names).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Optional, Sequence

from .evidence_drift_classify import (
    EVIDENCE_MAX_CHARS,
    _changed_words,
    _critical_diff,
    _critical_tokens,
    _multiset_diff,
    _pair,
    _words,
    segments,
)
from .provenance.text_fold import normalize_for_match

__all__ = [
    "ACKNOWLEDGED",
    "ACKNOWLEDGING_LEVELS",
    "ACK_RULE_VERSION",
    "ASSESS_RULES_VERSION",
    "AUTO_CLOSE_RULE_VERSION",
    "CHANGE_TYPES",
    "CLAIM_EFFECTS",
    "COMPONENT_TYPES",
    "HISTORY_REWRITE_LATE_HOURS",
    "REVIEW_PRIORITY",
    "TIMING_BUCKETS",
    "Assessment",
    "Diff",
    "acknowledgement",
    "assess_code",
    "auto_close_allowed",
    "claim_effect",
    "derive_history_rewrite",
    "derive_late_unexplained",
    "full_diff",
    "is_live_page",
    "name_fix",
    "page_stamps",
    "review_priority",
    "summary_line",
    "timing_bucket",
]

#: Bumped when a code rule changes what an assessment says.
ASSESS_RULES_VERSION = "drift-assess/2"
#: Bumped when the auto-close rule changes.
AUTO_CLOSE_RULE_VERSION = "drift-autoclose/1"

#: The granular types without the two wrappers.
COMPONENT_TYPES: tuple[str, ...] = (
    "spelling_or_name_correction",
    "update_new_facts",
    "clarification",
    "attribution_change",
    "reframing",
    "claim_removed",
    "claim_reversed",
    "fact_corrected",
    "retraction",
    "page_gone",
    "content_replaced",
)
CHANGE_TYPES: tuple[str, ...] = COMPONENT_TYPES + ("mixed", "unassessable")
#: Acknowledgement levels, highest first.
ACKNOWLEDGED: tuple[str, ...] = ("correction_notice", "visible_update_stamp", "metadata_date_only", "silent")

#: Which acknowledgement levels count as "acknowledged" for ``history_rewrite``
#: — a named, versioned rule (never an env switch). A hidden modified date
#: says nothing to a reader, so it does not count.
ACK_RULE_VERSION = "drift-ack/1"
ACKNOWLEDGING_LEVELS: frozenset[str] = frozenset({"correction_notice", "visible_update_stamp"})
#: The claim-level changes both derived labels look for.
_CLAIM_CHANGES = frozenset({"claim_removed", "claim_reversed", "fact_corrected"})
CLAIM_EFFECTS: tuple[str, ...] = (
    "cited_text_survives", "cited_text_gone", "cited_text_changed", "not_checkable")
TIMING_BUCKETS: tuple[str, ...] = ("same_day", "days", "weeks_plus", "unknown")

#: "Late", for ``history_rewrite``: the earlier text had stood unchanged for
#: at least this many hours after publication before it was changed. Measured
#: on the 2026-11-13 live sample (the 41 flag-writing archive pairs): every
#: in-place rewrite of a developing story — the Swat tolls, the AJK polls,
#: the Riyadh airport strike, the Mir Raza Ali case — was made within 31
#: hours of publication (the earlier copy was read 0.5-4 hours after
#: publication and the newsroom kept editing that day and the next); the
#: changes seen weeks later (a BBC espionage story re-worded 28 days on, a
#: Dawn helicopter-crash story re-written 20 days on) are a different kind
#: of event. 72 hours sits clear of the developing-story window with a day
#: of margin. The bound used is the LOWER bound of the edit's age (the
#: earlier version was still unchanged when last seen), so a slow re-check
#: cadence never makes an edit look late.
HISTORY_REWRITE_LATE_HOURS = 72.0

#: Review order: history rewrites, then the grave types, then the rest,
#: spelling/name corrections last.
REVIEW_PRIORITY = {"history_rewrite": 0, "model_doubt": 1, "grave": 2, "rest": 3, "spelling": 4}
_GRAVE = frozenset({"claim_reversed", "claim_removed", "fact_corrected", "retraction", "page_gone",
                    "content_replaced"})

#: Severity order of components (the first is shown first in a summary).
_SEVERITY = ("retraction", "page_gone", "content_replaced", "claim_reversed", "fact_corrected", "claim_removed",
             "attribution_change", "update_new_facts", "reframing", "clarification",
             "spelling_or_name_correction")

#: A name pair is a SPELLING fix when the folded strings are at least this
#: similar ("Auragzeb" -> "Aurangzeb" 0.94, "Kutla" -> "Kotla" 0.8,
#: "Muzafarrabad" -> "Muzaffarabad" 0.92) and share the first letter; a
#: different name ("Iran"/"Iraq" 0.75, "AFP"/"BBC" 0) stays a change.
NAME_FIX_MIN_RATIO = 0.8

#: A removed sentence counts as RESTATED (not a removed claim) when at least
#: this share of its content words, and every number in it, still appear in
#: the later text.
RESTATED_MIN_COVERAGE = 0.75

#: A name swapped for another in a sentence that otherwise changed at most
#: this many words is a corrected fact; more rewording is an update.
FACT_CORRECTED_MAX_WORDS = 6

#: A removed sentence is SUPERSEDED (an update, not a removed claim) when it
#: carries numbers or dates and a sentence of the later text (added, or the
#: later side of a changed pair) that also carries numbers or dates shares at
#: least this share of its content words and a name.
SUPERSEDED_MIN_OVERLAP = 0.2
#: ... or at least this share and a casualty/count word in common ("seven
#: people have been killed, and 18 others are injured" -> "At least 14
#: people ... at least 18 others injured").
SUPERSEDED_COUNT_MIN_OVERLAP = 0.15

#: Bounds on what an assessment keeps: before -> after pairs, removed claims,
#: characters per side.
SCOPE_MAX_PAIRS = 12
SUMMARY_MAX_CHARS = 200

_ATTRIBUTION_CUES = frozenset({
    "said", "says", "told", "according", "reported", "reports", "quoted", "quoting",
    "agency", "statement", "cited", "citing", "confirmed", "announced", "wrote",
    "news", "spokesperson", "spokesman", "spokeswoman", "source", "sources",
})
_COUNT_WORDS = frozenset({
    "killed", "dead", "deaths", "death", "toll", "died", "injured", "wounded",
    "martyred", "casualties", "victims", "missing", "arrested", "detained",
})
_STOP = frozenset(
    "the a an and or but of to in on at for with by from as is was were are be been has have had "
    "that this these those it its he she they them his her their which who whom what when where "
    "while also than then there here not no said says will would could should may might can into "
    "over after before about more most some any all such only other".split())

# -- markers ----------------------------------------------------------------------

#: A sentence that is newsroom process, not a claim: removing it removes no claim.
_PROCESS = re.compile(
    r"((sought|seeking|seeks|asked) (further )?comment|(has|have) (contacted|approached|asked)\b.{0,60}\bcomment"
    r"|did not (immediately )?respond to (a )?request|request for comment"
    r"|developing story|being updated|initial reports? .{0,40}inaccurate|strive to ensure"
    r"|with reporting by|additional reporting|read more|follow (our )?live|sign up"
    r"|this (article|story|report) (was|has been) (updated|amended))", re.I)
#: The page itself says the story changes / was updated (earlier or later text).
_UPDATE_NOTICE = re.compile(
    r"(^\s*updat(e|ed)\s*[:\-]|this is a developing story|story (is being|will be) updated"
    r"|this (article|story|report) (was|has been) updated|^\s*update\b)", re.I)
#: An ADDED sentence that acknowledges an earlier figure or version in-text.
_IN_TEXT_REVISION = re.compile(
    r"(revised (down|up|downward|upward)?\b.{0,60}\b(from|earlier|previous|initial)"
    r"|(an |the )?(earlier|previous|initial) (version|tally|figure|toll|count) (of|said|stated|put|had)"
    r"|correct(ed|s|ing) (an|the) (earlier|previous)|an earlier version of this)", re.I)
_RETRACTION = re.compile(
    r"(this (article|story|report|piece) (has been|was) (retracted|withdrawn|removed|taken down)"
    r"|\bretraction\s*:|we have (retracted|withdrawn|removed) (this|the))", re.I)
_CORRECTION = re.compile(
    r"(\bcorrection\s*:|\bclarification\s*:|an earlier version of this (article|story|report)"
    r"|this (article|story|report) (has been|was) (corrected|amended|updated to correct)"
    r"|we (have )?corrected)", re.I)
#: A claim is a sentence: it ends with sentence punctuation (or a closing quote).
_CLAIM_END = re.compile(r"[.!?…][\"'”’)\]]*\s*$")
_LIVE_URL = re.compile(r"(/live/|live-blog|liveblog|live-updates|-live-|/live$|live-news)", re.I)
_LEADING_CLOCK = re.compile(r"^-?\s*\d{1,2}[:.]\d{2}\s*(am|pm)?\b", re.I)

# -- bytes: publication / update stamps --------------------------------------------

_STAMP_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("published", re.compile(rb'"datePublished"\s*:\s*"([^"]{8,40})"')),
    ("published", re.compile(rb'article:published_time"?\s+content="([^"]{8,40})"')),
    ("published", re.compile(rb'content="([^"]{8,40})"\s+(?:property|name)="article:published_time"')),
    ("modified", re.compile(rb'"dateModified"\s*:\s*"([^"]{8,40})"')),
    ("modified", re.compile(rb'article:modified_time"?\s+content="([^"]{8,40})"')),
    ("modified", re.compile(rb'content="([^"]{8,40})"\s+(?:property|name)="article:modified_time"')),
)
_UPDATED_CHIP = re.compile(rb">\s*((?:last\s+)?updated\b[^<]{0,60})<", re.I)


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    s = raw.strip().replace(" ", "T", 1) if re.match(r"^\d{4}-\d{2}-\d{2} \d", raw.strip()) else raw.strip()
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def page_stamps(body: Optional[bytes]) -> dict[str, Any]:
    """The page's own publication / modification stamps from its stored
    bytes (JSON-LD ``datePublished``/``dateModified``, ``article:*_time``
    meta) and whether it shows a visible "Updated ..." chip. Best-effort,
    never raises; the earliest published and the latest modified win."""
    out: dict[str, Any] = {"published": None, "modified": None, "updated_chip": None}
    if not body:
        return out
    head = body[:600_000]
    try:
        for key, pat in _STAMP_PATTERNS:
            for m in pat.findall(head):
                ts = _parse_ts(m.decode("utf-8", "replace"))
                if ts is None:
                    continue
                cur = out[key]
                if cur is None or (ts < cur if key == "published" else ts > cur):
                    out[key] = ts
        chip = _UPDATED_CHIP.search(head)
        if chip:
            out["updated_chip"] = " ".join(chip.group(1).decode("utf-8", "replace").split())[:80]
    except Exception:  # noqa: BLE001 — stamps are a bonus
        return out
    return out


# -- the full diff -------------------------------------------------------------------

@dataclass
class Diff:
    """Every changed pair, removed and added sentence (raw text) of one
    change — D-1's pairing, unbounded (the classification keeps 8 of each)."""

    pairs: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    earlier_sentences: int = 0
    later_sentences: int = 0
    earlier_text: str = ""
    later_text: str = ""


def full_diff(earlier_text: Optional[str], later_text: Optional[str]) -> Diff:
    """D-1's pairing over the content sentences, kept whole."""
    e = [s for s in segments(earlier_text or "") if not s.chrome]
    lat = [s for s in segments(later_text or "") if not s.chrome]
    removed, added = _multiset_diff(e, lat, "key")
    removed, added = _multiset_diff(removed, added, "loose")
    pairs, rem_only, add_only = _pair(removed, added)
    return Diff(pairs=[(a.raw, b.raw) for a, b in pairs], removed=[s.raw for s in rem_only],
                added=[s.raw for s in add_only], earlier_sentences=len(e), later_sentences=len(lat),
                earlier_text=earlier_text or "", later_text=later_text or "")


def _clip(text: Optional[str], n: int = EVIDENCE_MAX_CHARS) -> Optional[str]:
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# -- code rules ------------------------------------------------------------------------

def name_fix(a: str, b: str) -> bool:
    """``a`` -> ``b`` is a spelling fix of one name: same first letter, folded
    similarity at least :data:`NAME_FIX_MIN_RATIO`, or the same name modulo
    punctuation ("Antigua" / "Antigua-")."""
    fa, fb = re.sub(r"[\W_]+", "", a.lower()), re.sub(r"[\W_]+", "", b.lower())
    if not fa or not fb:
        return False
    if fa == fb:
        return True
    if fa[0] != fb[0] or min(len(fa), len(fb)) < 4:
        return False
    return SequenceMatcher(None, fa, fb).ratio() >= NAME_FIX_MIN_RATIO


def _cap_runs(text: str) -> list[list[str]]:
    """Runs of adjacent capitalised words ("Mir Raza Ali", "US President
    Donald Trump") — one multi-word name per run."""
    runs: list[list[str]] = []
    cur: list[str] = []
    for w in re.findall(r"[\w'’-]+|[^\w\s]", text):
        if w[:1].isupper() and w[:1].isalpha():
            cur.append(w.strip("'’").removesuffix("'s").removesuffix("’s"))
        else:
            if len(cur) > 1:
                runs.append(cur)
            cur = []
    if len(cur) > 1:
        runs.append(cur)
    return runs


def _same_entity(gone: str, new: str, texts: Sequence[str]) -> bool:
    """Both names are parts of one multi-word PERSON-style name somewhere in
    the texts ("Raza" / "Ali" in "Mir Raza Ali"); acronyms never count."""
    if gone.isupper() or new.isupper():
        return False
    for t in texts:
        for run in _cap_runs(t):
            if gone in run and new in run:
                return True
    return False


def _abbreviation(before: str, after: str, removed: Iterable[str], added: Iterable[str]) -> set[str]:
    """Names that only switched between a multi-word name and its acronym
    ("United Kingdom" -> "UK", "Khyber Pakhtunkhwa" -> "KP", and back)."""
    hit: set[str] = set()
    for acros, full_text, longs in ((list(added), before, set(removed)), (list(removed), after, set(added))):
        for ac in (a for a in acros if a.isupper() and 2 <= len(a) <= 6):
            for run in _cap_runs(full_text):
                for i in range(len(run)):
                    for j in range(i + 2, min(len(run), i + len(ac)) + 1):
                        part = run[i:j]
                        if "".join(x[0] for x in part).upper() == ac and set(part) & longs:
                            hit |= (set(part) & longs) | {ac}
    return hit


def _near_attribution(sentence: str, name: str) -> bool:
    toks = [t.lower() for t in re.findall(r"[\w'’-]+", sentence)]
    lname = name.lower()
    for i, t in enumerate(toks):
        if t == lname:
            window = toks[max(0, i - 4): i + 5]
            if any(w in _ATTRIBUTION_CUES for w in window):
                return True
    return False


def _numbers(raw: str) -> list[float]:
    out = []
    for n in re.findall(r"\d[\d,]*\.?\d*", raw):
        try:
            out.append(float(n.replace(",", "")))
        except ValueError:
            continue
    return out


def _pair_components(before: str, after: str, texts: Sequence[str]) -> tuple[set[str], dict[str, Any]]:
    """The component types one changed sentence pair shows, and what was
    seen (the critical tokens, the rule)."""
    cd = _critical_diff(before, after)
    seen: dict[str, Any] = {"critical": cd}
    comps: set[str] = set()
    words_changed = _changed_words(before, after)
    neg = cd.get("negations")
    if neg:
        wb = [w for w in _words(before) if not _is_negation(w)]
        wa = [w for w in _words(after) if not _is_negation(w)]
        if _changed_words(" ".join(wb), " ".join(wa)) <= 2:
            comps.add("claim_reversed")
            seen["rule"] = "negation_toggled"
            return comps, seen
    nums = cd.get("numbers")
    if nums:
        gone = [float(x[1:]) for x in nums if x.startswith("-") and _is_float(x[1:])]
        new = [float(x[1:]) for x in nums if x.startswith("+") and _is_float(x[1:])]
        counts = any(w in _COUNT_WORDS for w in _words(before) + _words(after))
        g0, n0 = _closest([str(x) for x in gone], [str(x) for x in new]) if gone and new else (None, None)
        if counts and g0 is not None and float(n0) < float(g0):
            # a cumulative count that went DOWN: the earlier value was wrong
            comps.add("fact_corrected")
            seen["rule"] = "count_revised_down"
        else:
            # a count rising (7 -> 14) or any other figure moving: an update
            comps.add("update_new_facts")
            seen["rule"] = "count_rose" if counts and g0 is not None else "number_changed"
    names = cd.get("names") or []
    if names:
        rem = [x[1:] for x in names if x.startswith("-")]
        add = [x[1:] for x in names if x.startswith("+")]
        abbrev = _abbreviation(before, after, rem, add)
        rem = [r for r in rem if r not in abbrev]
        add = [a for a in add if a not in abbrev]
        fixes = [(r, a) for r in rem for a in add if name_fix(r, a)]
        fixed_r, fixed_a = {r for r, _ in fixes}, {a for _, a in fixes}
        same = [(r, a) for r in rem for a in add
                if r not in fixed_r and a not in fixed_a and _same_entity(r, a, texts)]
        same_r, same_a = {r for r, _ in same}, {a for _, a in same}
        if fixes or same:
            comps.add("spelling_or_name_correction")
            seen["name_fixes"] = [f"{r} -> {a}" for r, a in fixes + same]
        first_b, first_a = _first_word(before), _first_word(after)
        rest_r = [r for r in rem if r not in fixed_r and r not in same_r and r != first_b]
        rest_a = [a for a in add if a not in fixed_a and a not in same_a and a != first_a]
        # A name only dropped, next to a name the later sentence keeps, is
        # the same reference shortened ("US President Donald Trump" -> "Trump").
        kept = {w for run in _cap_runs(after) for w in run} | set(re.findall(r"\b[A-Z][\w'’-]+", after))
        rest_r = [r for r in rest_r if not any(r in run and set(run) & kept for run in _cap_runs(before))]
        rest_a = [a for a in rest_a if not any(a in run and set(run) & set(re.findall(r"\b[A-Z][\w'’-]+", before))
                                               for run in _cap_runs(after))]
        if rest_r and rest_a:
            if any(_near_attribution(before, r) for r in rest_r) and \
                    any(_near_attribution(after, a) for a in rest_a):
                comps.add("attribution_change")
                seen["attribution"] = {"from": rest_r, "to": rest_a}
            elif words_changed <= FACT_CORRECTED_MAX_WORDS:
                # one place / person / body swapped for another in an
                # otherwise unchanged sentence ("near the Strait of Hormuz"
                # -> "near Jask"): the earlier value was corrected
                comps.add("fact_corrected")
                seen["rule"] = "name_swapped"
                seen["swapped"] = {"from": rest_r, "to": rest_a}
            else:
                comps.add("update_new_facts")
                seen["rule"] = "name_replaced_in_rewrite"
        elif rest_a or rest_r:
            comps.add("clarification")
    if not comps and words_changed > 12:
        comps.add("clarification")
    return comps, seen


def _first_word(sentence: str) -> str:
    m = re.match(r"\W*([\w'’-]+)", sentence)
    return m.group(1) if m else ""


def _closest(gone: Sequence[str], new: Sequence[str]) -> tuple[str, str]:
    """The (earlier, later) number pair closest in magnitude — the revised
    figure, not an unrelated one added in the same sentence."""
    best = (gone[0], new[0])
    best_d = None
    for g in gone:
        for n in new:
            try:
                gv, nv = abs(float(g)) or 1.0, abs(float(n)) or 1.0
            except ValueError:
                continue
            d = max(gv, nv) / min(gv, nv)
            if best_d is None or d < best_d:
                best, best_d = (g, n), d
    return best


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _is_negation(w: str) -> bool:
    return bool(_critical_tokens(w)["negations"])


def _content_words(text: str) -> list[str]:
    return [w for w in _words(text) if len(w) >= 4 and w not in _STOP]


def _restated(sentence: str, later_fold_words: set[str], later_text: str) -> bool:
    words = _content_words(sentence)
    if not words:
        return True
    cover = sum(1 for w in words if w in later_fold_words) / len(words)
    nums = {n for n in re.findall(r"\d[\d,.]*\d|\d", sentence)}
    return cover >= RESTATED_MIN_COVERAGE and all(n in later_text for n in nums)


def _superseded_by(sentence: str, added: Sequence[str]) -> Optional[str]:
    """The added sentence that restates ``sentence`` with its numbers or
    dates changed, if any."""
    t = _critical_tokens(sentence)
    if not (t["numbers"] or t["dates"]):
        return None
    words = set(_content_words(sentence))
    stems = {n.lower()[:5] for n in t["names"]}
    for a in added:
        ta = _critical_tokens(a)
        if not (ta["numbers"] or ta["dates"]):
            continue
        aw = set(_content_words(a))
        overlap = len(words & aw) / max(1, len(words | aw))
        shared_name = bool(stems & {n.lower()[:5] for n in ta["names"]})
        shared_count = bool(words & aw & _COUNT_WORDS)
        if (overlap >= SUPERSEDED_MIN_OVERLAP and shared_name) or \
                (overlap >= SUPERSEDED_COUNT_MIN_OVERLAP and shared_count):
            return a
    return None


def is_live_page(address: Optional[str], diff: Optional[Diff] = None) -> bool:
    """A rolling live page: the address says so, or most of the earlier
    entries carry a leading clock time."""
    if address and _LIVE_URL.search(address):
        return True
    if diff and diff.earlier_text:
        segs = [s for s in segments(diff.earlier_text) if not s.chrome]
        if len(segs) >= 10 and sum(bool(_LEADING_CLOCK.match(s.raw)) for s in segs) >= len(segs) // 3:
            return True
    return False


def acknowledgement(diff: Diff, *, earlier_stamps: Mapping[str, Any] | None = None,
                    later_stamps: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """(highest level, evidence with every signal that fired), from code.

    * ``correction_notice`` — a correction/retraction marker new in the later
      text, or an ADDED sentence that says what changed ("revised down from
      an earlier tally of 19");
    * ``visible_update_stamp`` — the later page shows an "Updated ..." chip,
      or either text carries an update / developing-story line;
    * ``metadata_date_only`` — only the page's hidden modified date
      (JSON-LD / meta) moved past the earlier version's;
    * ``silent`` — none of these."""
    later_fold = normalize_for_match(diff.later_text)
    earlier_fold = normalize_for_match(diff.earlier_text)
    signals: list[str] = []
    ev: dict[str, Any] = {}
    if (_CORRECTION.search(later_fold) and not _CORRECTION.search(earlier_fold)) or \
            (_RETRACTION.search(later_fold) and not _RETRACTION.search(earlier_fold)):
        signals.append("correction_marker")
    for s in diff.added:
        if _IN_TEXT_REVISION.search(s):
            signals.append("in_text_revision")
            ev["sentence"] = _clip(s)
            break
    es, ls = dict(earlier_stamps or {}), dict(later_stamps or {})
    if ls.get("updated_chip"):
        signals.append("visible_updated_chip")
        ev["chip"] = ls["updated_chip"]
    for text in (diff.later_text, diff.earlier_text):
        hit = next((x.raw for x in segments(text) if _UPDATE_NOTICE.search(x.raw)), None)
        if hit:
            signals.append("update_notice_in_text")
            ev["notice"] = _clip(hit)
            break
    lm, em = ls.get("modified"), es.get("modified")
    if lm is not None and (em is None or lm > em) and (ls.get("published") is None or lm > ls["published"]):
        signals.append("modified_stamp_advanced")
        ev["modified"] = str(lm)
        ev["earlier_modified"] = None if em is None else str(em)
    if {"correction_marker", "in_text_revision"} & set(signals):
        level = "correction_notice"
    elif {"visible_updated_chip", "update_notice_in_text"} & set(signals):
        level = "visible_update_stamp"
    elif "modified_stamp_advanced" in signals:
        level = "metadata_date_only"
    else:
        level = "silent"
    return level, {"signals": signals, "rule_version": ACK_RULE_VERSION, **ev}


def timing_bucket(hours: Optional[float]) -> str:
    if hours is None or hours < 0:
        return "unknown"
    if hours < 24:
        return "same_day"
    if hours < 24 * 7:
        return "days"
    return "weeks_plus"


def _hours(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() / 3600.0, 2)


@dataclass
class Assessment:
    """One assessment of one classified change. ``observed`` fields describe
    the two versions; ``history_rewrite`` is DERIVED in code from them."""

    change_type: str
    component_types: list[str] = field(default_factory=list)
    acknowledged: str = "silent"
    acknowledged_evidence: dict[str, Any] = field(default_factory=dict)
    acknowledged_source: str = "code"
    scope_pairs: list[dict[str, Any]] = field(default_factory=list)
    removed_claims: list[str] = field(default_factory=list)
    claims_changed: int = 0
    share_changed: Optional[float] = None
    interval_hours: Optional[float] = None
    timing_bucket: str = "unknown"
    timing_basis: str = "unknown"
    standing_hours: Optional[float] = None
    summary: str = ""
    history_rewrite: bool = False
    history_rewrite_inputs: dict[str, Any] = field(default_factory=dict)
    late_unexplained_change: bool = False
    late_unexplained_inputs: dict[str, Any] = field(default_factory=dict)
    acknowledged_signals: list[str] = field(default_factory=list)
    model_type_doubt: Optional[dict[str, Any]] = None
    type_source: str = "code"
    code_type: str = ""
    code_components: list[str] = field(default_factory=list)
    unassessable_reason: Optional[str] = None
    observations: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _type_of(components: Iterable[str]) -> tuple[str, list[str]]:
    comps = sorted(set(components), key=_SEVERITY.index)
    if not comps:
        return "unassessable", []
    if len(comps) == 1:
        return comps[0], comps
    return "mixed", comps


def derive_history_rewrite(components: Sequence[str], acknowledged: str, standing_hours: Optional[float],
                           *, acknowledging: frozenset[str] = ACKNOWLEDGING_LEVELS) -> tuple[bool, dict[str, Any]]:
    """DERIVED, code only: a claim removed, reversed or corrected AND late
    (the earlier text stood at least :data:`HISTORY_REWRITE_LATE_HOURS`) AND
    not acknowledged at a level in ``acknowledging`` (default
    :data:`ACKNOWLEDGING_LEVELS`, rule :data:`ACK_RULE_VERSION`)."""
    claim_changed = bool(_CLAIM_CHANGES & set(components))
    late = standing_hours is not None and standing_hours >= HISTORY_REWRITE_LATE_HOURS
    acked = acknowledged in acknowledging
    inputs = {"claim_removed_reversed_or_corrected": claim_changed, "acknowledged": acknowledged,
              "acknowledging_levels": sorted(acknowledging), "acknowledged_counts": acked,
              "ack_rule_version": ACK_RULE_VERSION, "standing_hours": standing_hours,
              "late_hours": HISTORY_REWRITE_LATE_HOURS, "late": late, "rules_version": ASSESS_RULES_VERSION}
    return claim_changed and late and not acked, inputs


def derive_late_unexplained(components: Sequence[str], acknowledged: str,
                            standing_hours: Optional[float]) -> tuple[bool, dict[str, Any]]:
    """DERIVED, code only: a claim removed, reversed or corrected AND late AND
    no correction notice — fires when only a stamp or a metadata date moved."""
    claim_changed = bool(_CLAIM_CHANGES & set(components))
    late = standing_hours is not None and standing_hours >= HISTORY_REWRITE_LATE_HOURS
    inputs = {"claim_removed_reversed_or_corrected": claim_changed, "acknowledged": acknowledged,
              "correction_notice": acknowledged == "correction_notice", "standing_hours": standing_hours,
              "late_hours": HISTORY_REWRITE_LATE_HOURS, "late": late, "rules_version": ASSESS_RULES_VERSION}
    return claim_changed and late and acknowledged != "correction_notice", inputs


def summary_line(change_type: str, components: Sequence[str], scope_pairs: Sequence[Mapping[str, Any]],
                 removed: Sequence[str], added: int, *, status: Optional[int] = None) -> str:
    """One or two plain sentences from code: the most telling change first."""
    if change_type == "page_gone":
        return f"page no longer available{f' (HTTP {status})' if status else ''}"
    if change_type == "retraction":
        return "the page now carries a retraction notice"
    if change_type == "content_replaced":
        return "the address now carries different content (almost none of the earlier text survives)"
    parts: list[str] = []
    for p in scope_pairs:
        crit = p.get("critical") or {}
        if p.get("kind") == "fact_corrected" and crit.get("numbers"):
            g = [x[1:] for x in crit["numbers"] if x.startswith("-")]
            n = [x[1:] for x in crit["numbers"] if x.startswith("+")]
            if g and n:
                g0, n0 = _closest(g, n)
                parts.append(f"figure corrected {g0} → {n0}")
        elif p.get("kind") == "fact_corrected" and p.get("swapped"):
            sw = p["swapped"]
            parts.append(f"{' '.join(sw['from'][:2])} corrected to {' '.join(sw['to'][:2])}")
        elif p.get("kind") == "claim_reversed":
            parts.append("a statement's negation changed")
        elif p.get("kind") == "spelling_or_name_correction" and p.get("name_fixes"):
            parts.append("name corrected " + "; ".join(p["name_fixes"][:2]))
        elif p.get("kind") == "attribution_change" and p.get("attribution"):
            a = p["attribution"]
            parts.append(f"attribution changed {', '.join(a['from'][:2])} → {', '.join(a['to'][:2])}")
        elif p.get("kind") == "update_new_facts" and crit.get("numbers"):
            g = [x[1:] for x in crit["numbers"] if x.startswith("-")]
            n = [x[1:] for x in crit["numbers"] if x.startswith("+")]
            if g and n:
                g0, n0 = _closest(g, n)
                parts.append(f"figure updated {g0} → {n0}")
        if len(parts) >= 2:
            break
    if removed and "claim_removed" in components:
        parts.append(f"{len(removed)} earlier sentence(s) removed")
    if added and "update_new_facts" in components:
        parts.append(f"{added} sentence(s) added")
    if "reframing" in components and not parts:
        parts.append("headline reworded, same facts")
    if not parts:
        parts.append(change_type.replace("_", " "))
    text = "; ".join(dict.fromkeys(parts))
    return text[:SUMMARY_MAX_CHARS]


def assess_code(
    *, drift_class: str, rule: str, diff: Diff, address: Optional[str] = None,
    earlier_title: Optional[str] = None, later_title: Optional[str] = None,
    earlier_stamps: Mapping[str, Any] | None = None, later_stamps: Mapping[str, Any] | None = None,
    published_at: Optional[datetime] = None, earlier_seen_at: Optional[datetime] = None,
    earlier_last_seen_at: Optional[datetime] = None, later_seen_at: Optional[datetime] = None,
    later_status: Optional[int] = None, retention: Optional[float] = None,
) -> Assessment:
    """The code-only assessment of one classified change. Total: never
    raises for well-formed inputs; deterministic."""
    texts = (diff.earlier_text, diff.later_text)
    obs: dict[str, Any] = {"drift_class": drift_class, "rule": rule}
    comps: set[str] = set()
    pairs_out: list[dict[str, Any]] = []
    removed_claims: list[str] = []
    live = is_live_page(address, diff)
    obs["live_page"] = live
    reason: Optional[str] = None

    if drift_class == "gone":
        comps.add("page_gone")
    elif rule == "retraction_marker":
        comps.add("retraction")
    elif rule == "article_replaced":
        # D-1's replaced rule (retention and vocabulary overlap): the address
        # now carries different content — a live page rolled over, a story
        # reused.
        comps.add("content_replaced")
    else:
        for before, after in diff.pairs:
            pc, seen = _pair_components(before, after, texts)
            for k in pc:
                comps.add(k)
            if pc:
                kind = sorted(pc, key=_SEVERITY.index)[0]
                pairs_out.append({"before": _clip(before), "after": _clip(after), "kind": kind, **seen})
        later_words = set(_words(diff.later_text))
        for s in diff.removed:
            if _PROCESS.search(s) or len(_words(s)) < 6 or not _CLAIM_END.search(s):
                # newsroom process, a fragment, or a heading / related-link
                # line (no sentence end): no claim removed
                continue
            if live:
                obs["rolled_off"] = obs.get("rolled_off", 0) + 1
                continue
            if _restated(s, later_words, diff.later_text):
                obs["restated"] = obs.get("restated", 0) + 1
                continue
            by = _superseded_by(s, list(diff.added) + [a for _b, a in diff.pairs])
            if by is not None:
                obs["superseded"] = obs.get("superseded", 0) + 1
                comps.add("update_new_facts")
                continue
            removed_claims.append(s)
        if removed_claims:
            comps.add("claim_removed")
        added_claims = [s for s in diff.added if not _PROCESS.search(s) and len(_words(s)) >= 5]
        if added_claims:
            comps.add("update_new_facts")
        if earlier_title and later_title and \
                re.sub(r"[\W_]+", "", normalize_for_match(earlier_title)) != \
                re.sub(r"[\W_]+", "", normalize_for_match(later_title)):
            tcd = _critical_diff(earlier_title, later_title)
            obs["title"] = {"before": _clip(earlier_title), "after": _clip(later_title), "critical": tcd}
            if not tcd:
                comps.add("reframing")
            elif not comps:
                comps.add("update_new_facts")
        if not comps and diff.pairs:
            comps.add("clarification")
        # A rewording or a re-written headline that rides on new facts is part
        # of the update, not a separate kind of change: kept as an observation.
        if "update_new_facts" in comps:
            absorbed = sorted(comps & {"clarification", "reframing"})
            if absorbed:
                obs["absorbed_into_update"] = absorbed
                comps -= {"clarification", "reframing"}
    ack, ack_ev = acknowledgement(diff, earlier_stamps=earlier_stamps, later_stamps=later_stamps)
    if drift_class == "gone":
        ack, ack_ev = "silent", {"signals": [], "rule_version": ACK_RULE_VERSION, "page_gone": True}

    published = (earlier_stamps or {}).get("published") or published_at
    basis = "published" if published is not None else ("first_read" if earlier_seen_at else "unknown")
    start = published if published is not None else earlier_seen_at
    interval = _hours(start, later_seen_at)
    standing = _hours(start, earlier_last_seen_at or earlier_seen_at)
    if reason:
        ctype, clist = "unassessable", []
    else:
        ctype, clist = _type_of(comps)
        if ctype == "unassessable":
            reason = "no_component_found: the change classified as flag-writing but no rule named a type"
    hr, hr_inputs = derive_history_rewrite(clist, ack, standing)
    lu, lu_inputs = derive_late_unexplained(clist, ack, standing)
    claims_changed = len(pairs_out) + len(removed_claims)
    share = None if retention is None else round(max(0.0, 1.0 - float(retention)), 3)
    if share is None and diff.earlier_sentences:
        share = round(min(1.0, (len(diff.pairs) + len(diff.removed)) / diff.earlier_sentences), 3)
    pairs_out.sort(key=lambda p: _SEVERITY.index(p["kind"]))
    scope = pairs_out[:SCOPE_MAX_PAIRS]
    summary = summary_line(ctype, clist, scope, removed_claims, len(diff.added), status=later_status)
    obs.update({"pairs": len(diff.pairs), "removed": len(diff.removed), "added": len(diff.added),
                "earlier_sentences": diff.earlier_sentences, "later_sentences": diff.later_sentences,
                "stamps": {"earlier": _stamps_json(earlier_stamps), "later": _stamps_json(later_stamps)}})
    return Assessment(
        change_type=ctype, component_types=clist if ctype == "mixed" else ([] if ctype == "unassessable" else clist),
        acknowledged=ack, acknowledged_evidence=ack_ev, scope_pairs=scope,
        removed_claims=[_clip(s) or "" for s in removed_claims[:SCOPE_MAX_PAIRS]],
        claims_changed=claims_changed, share_changed=share, interval_hours=interval,
        timing_bucket=timing_bucket(interval), timing_basis=basis, standing_hours=standing,
        summary=summary, history_rewrite=hr, history_rewrite_inputs=hr_inputs,
        late_unexplained_change=lu, late_unexplained_inputs=lu_inputs,
        acknowledged_signals=list(ack_ev.get("signals") or []), type_source="code", code_type=ctype, code_components=list(clist),
        unassessable_reason=reason, observations=obs,
    )


def _stamps_json(st: Mapping[str, Any] | None) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(st or {}).items()}


# -- per citer ------------------------------------------------------------------------------

def claim_effect(span_check: Mapping[str, Any] | None, diff: Diff, *,
                 drift_class: str = "substantive_edit") -> tuple[str, Optional[str], dict[str, Any]]:
    """The per-flag verdict, in code: (effect, after_text, basis).

    * no span, or the span was never in the earlier text -> ``not_checkable``;
    * every span sentence still there -> ``cited_text_survives``;
    * a missing span sentence is the earlier side of a changed pair ->
      ``cited_text_changed`` with the later side;
    * otherwise (removed, or the page is gone) -> ``cited_text_gone``.
    """
    sc = dict(span_check or {})
    if not sc or sc.get("span_in_earlier") is not True:
        return "not_checkable", None, {"reason": "no_span" if not sc else "span_not_in_earlier_text"}
    if drift_class == "gone":
        return "cited_text_gone", None, {"reason": "page_gone"}
    if sc.get("span_in_later") is True:
        return "cited_text_survives", None, {"reason": "every_span_sentence_still_present"}
    missing = [m.rstrip("…").strip() for m in sc.get("missing_in_later") or []]
    for m in missing:
        mk = normalize_for_match(m)[:200]
        for before, after in diff.pairs:
            bk = normalize_for_match(before)
            if mk and (mk in bk or bk in mk):
                return "cited_text_changed", _clip(after, 2000), {"reason": "span_sentence_reworded",
                                                                  "before": _clip(before)}
    return "cited_text_gone", None, {"reason": "span_sentence_removed", "missing": missing[:3]}


def auto_close_allowed(*, change_type: str, code_type: str, code_effect: str) -> bool:
    """The tracked auto-close rule (:data:`AUTO_CLOSE_RULE_VERSION`): the
    change is a spelling/name correction (not mixed) by the final AND the code
    type, and the cited text survives by the CODE check."""
    return (change_type == "spelling_or_name_correction" and code_type == "spelling_or_name_correction"
            and code_effect == "cited_text_survives")


def review_priority(change_type: Optional[str], components: Sequence[str] | None,
                    history_rewrite: bool, late_unexplained: bool = False, model_doubt: bool = False) -> int:
    """Sort key for review lists (:data:`REVIEW_PRIORITY`): history rewrites
    and late unexplained changes, then rows the model doubts the code type
    of, then the grave types, then the rest, spelling corrections last."""
    if history_rewrite or late_unexplained:
        return REVIEW_PRIORITY["history_rewrite"]
    if model_doubt:
        return REVIEW_PRIORITY["model_doubt"]
    comps = set(components or ()) | ({change_type} if change_type else set())
    if comps & _GRAVE:
        return REVIEW_PRIORITY["grave"]
    if change_type == "spelling_or_name_correction":
        return REVIEW_PRIORITY["spelling"]
    return REVIEW_PRIORITY["rest"]
