# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source drift CLASSIFICATION — what changed between two versions of one page.
Rule-based, in code, no model.

INPUT. Two versions of one canonical address (``fetch_versions``): the
EXTRACTED main text each kept (the archiver's Trafilatura pass, stored on the
version row), optionally each version's ``<title>``, and whether the later
fetch came back gone (404/410, redirected away — the drift row's
``later_gone``). Bytes are never compared: a byte-only change with the same
extracted text is ``identical_text`` by definition.

THE CLOSED LIST (:data:`DRIFT_CLASSES`, ordered by severity — the final class
is the most severe facet that fired):

* ``identical_text``   bytes changed, extracted text did not.
* ``cosmetic``         only page furniture changed: navigation, related-link
                       blocks, "Published 10 hours ago", share/comment counts,
                       "Loading...", consent/ad lines (:data:`CHROME_PATTERNS`).
* ``appended``         sentences ADDED and nothing of the earlier text removed
                       or reworded (an update or a correction notice added,
                       the original retained).
* ``minor_edit``       typo-scale: case/punctuation/hyphenation only, or at
                       most :data:`MINOR_MAX_SENTENCES` sentences reworded with
                       at most :data:`MINOR_MAX_CHANGED_WORDS` words changed in
                       total and no number, date, name or negation touched.
* ``title_changed``    the headline changed and the body changed no more than
                       a minor edit.
* ``substantive_edit`` an earlier sentence removed, more rewording than a minor
                       edit, or ANY changed sentence whose numbers, dates,
                       names or negations differ (:func:`_critical_tokens`) —
                       always at least this class, whatever the word count.
* ``retracted_or_replaced`` a retraction/withdrawal marker appeared, a
                       correction notice appeared AND the text changed, or the
                       article was replaced by another (almost nothing of the
                       earlier text survives — :data:`REPLACED_MAX_RETENTION`
                       and :data:`REPLACED_MAX_JACCARD`).
* ``gone``             the later fetch did not answer (404/410, redirected
                       away) or kept no article text (a wall or an empty page).

``unclassifiable`` (outside the list, counted, never flagged) — neither
version kept article text, so there is nothing to compare.

THE THRESHOLDS were set from a hand-classified live sample (2026-11-13: the 47
differing consecutive pairs among the 78 addresses the evidence archive holds
more than one copy of, plus 40 byte-only pairs). Each is a named constant with
the reason next to it. Most live change was NOT cosmetic: developing stories
re-published under the same address (death tolls revised 19 → 17 and 7 → 14,
headlines rewritten, "the AFP news agency" → "the BBC"), and rolling live blogs
whose earlier entries scroll off. Cosmetic change in the sample was BBC's
"Related topics / Published 10 hours ago" block and the Christian Science
Monitor's "Loading... / Why We Wrote This" chrome.

THE EVIDENCE (:class:`DriftClassification`) records which rule fired, sizes,
counts of added/removed/reworded/chrome sentences, and a compact diff — the
changed sentence pairs, the removed and added sentences (each bounded), and
the numbers, dates, names and negations that changed — so a reader sees WHY
without opening the pages.

THE CLAIM JUDGE (:data:`CLAIM_JUDGE_SEAM`, filled by D-2): whether a
substantive edit changes the SPECIFIC claim a finding cites. The exact span
check (:func:`check_cited_span`) answers "is the cited sentence still there";
:mod:`legba.data.evidence_drift_assess` turns it into a per-flag claim effect
(survives / gone / changed with the after-text / not checkable) and assesses
the change's kind; the model step it may call (behind its own switch) lives
there, never here.

Stdlib + the shared fold (:mod:`legba.data.provenance.text_fold`) only.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional, Sequence

from .provenance.text_fold import normalize_for_match

__all__ = [
    "CLASSIFIER_VERSION",
    "CLAIM_JUDGE_SEAM",
    "DRIFT_CLASSES",
    "FLAGGING_CLASSES",
    "UNCLASSIFIABLE",
    "DriftClassification",
    "SpanCheck",
    "check_cited_span",
    "classify_pair",
    "class_severity",
    "extract_title",
    "segments",
]

#: Bumped when a rule or a threshold changes what a pair classifies as.
CLASSIFIER_VERSION = "drift-classify/1"

DRIFT_CLASSES: tuple[str, ...] = (
    "identical_text",
    "cosmetic",
    "appended",
    "minor_edit",
    "title_changed",
    "substantive_edit",
    "retracted_or_replaced",
    "gone",
)
UNCLASSIFIABLE = "unclassifiable"

#: The classes that write flags on dependants (cosmetic classes are counted).
FLAGGING_CLASSES: frozenset[str] = frozenset(
    {"substantive_edit", "title_changed", "retracted_or_replaced", "gone"}
)

#: "Does this substantive edit change the claim THIS citation makes?" — built
#: by D-2 (SEAMS row 79, resolved): legba.data.evidence_drift_assess.
CLAIM_JUDGE_SEAM = "evidence_drift.claim_judge (legba.data.evidence_drift_assess)"

# -- thresholds (from the 2026-11-13 live sample) ------------------------------

#: A removed sentence is paired with an added one as a REWORDING when the
#: folded strings are at least this similar (difflib ratio). Measured on the
#: sample: the 141 accepted pairings have median 0.94 (tenth percentile 0.67);
#: the best candidate of the 105 sentences left unpaired has median 0.39
#: (ninetieth percentile 0.53). There is no clean gap around 0.6 — a pair on
#: either side ends as "reworded" or as "removed + added", and both are
#: substantive unless the rewording is minor, so the threshold only moves
#: pairs between ``minor_edit`` and ``substantive_edit``/``sentence_removed``.
PAIR_MIN_RATIO = 0.6

#: A minor edit touches at most this many sentences ...
MINOR_MAX_SENTENCES = 3
#: ... and at most this many words across them. Of the sample's 74 reworded
#: pairs that changed no number, date, name or negation, 67 changed 1-4 words
#: ("which" -> "that", "has been forced" -> "was forced"); the tail was 5, 5,
#: 8, 8, 11, 15 and 25.
MINOR_MAX_CHANGED_WORDS = 12

#: Replaced: under this share of the earlier content sentences survive
#: (exactly or reworded) ...
REPLACED_MAX_RETENTION = 0.2
#: ... AND the two texts share under this share of their word vocabulary
#: (Jaccard). In the sample the six replacements (rolling live blogs whose
#: entries scrolled off: RFE/RL, the Guardian) had retention 0.00-0.04 and
#: Jaccard 0.21-0.25; the closest in-place rewrites of a developing story
#: (Dawn's Jharkhand protest, the Guardian's Qeshm strike, Hindustan Times'
#: live page) had Jaccard 0.31-0.34 and stay ``substantive_edit``.
REPLACED_MAX_JACCARD = 0.3

#: Pairing is quadratic; above this many unmatched sentences per side the
#: rewording search is skipped and every unmatched sentence counts as removed
#: or added (the pair is then judged by retention alone).
MAX_PAIRING = 250

#: Evidence bounds: changed pairs / removed / added sentences kept, and the
#: characters kept per sentence.
EVIDENCE_MAX_ITEMS = 8
EVIDENCE_MAX_CHARS = 280

#: A cited span's sentence shorter than this many words is not checked (a
#: two-word fragment matches anywhere).
SPAN_MIN_WORDS = 5

# -- page furniture -------------------------------------------------------------

_REL_TIME = (r"(?:\d+\s*(?:secs?|seconds?|mins?|minutes?|hrs?|hours?|days?|weeks?|months?)"
             r"(?:\s*\d+\s*(?:mins?|minutes?|secs?|seconds?))?\s*ago)")
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_CLOCK = r"(?:\d{1,2}[:.]\d{2}\s*(?:am|pm)?(?:\s*[a-z]{2,4})?)"
_DATE = (rf"(?:\d{{1,2}}\s+{_MONTH}\s+\d{{4}}|{_MONTH}\s+\d{{1,2}}(?:,?\s+\d{{4}})?|"
         r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})")

#: A segment that is ONLY page furniture (matched against the folded text, so
#: lower case). Each entry is one family seen live or in common news chrome.
CHROME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in (
    r"^-?\s*related (topics|stories|articles|content|links)\b.*$",
    r"^-?\s*(more on this story|more from|read more|see also|also read|recommended|most read|"
    r"top stories|latest news|trending)\b.{0,80}$",
    rf"^-?\s*(published|updated|last updated|posted|first published)\s*:?\s*(on\s*)?"
    rf"({_REL_TIME}|{_DATE}|{_CLOCK}|\d+).{{0,40}}$",
    rf"^-?\s*{_REL_TIME}$",
    rf"^-?\s*({_DATE}|{_CLOCK})([\s,•|-]*({_DATE}|{_CLOCK}|update[ds]?:?))*\s*$",
    r"^-?\s*\d[\d,.]*\s*[km]?\s*(shares?|comments?|views?|likes?|reactions?|replies)$",
    r"^-?\s*(comments?|shares?)\s*\(?\d[\d,.]*\)?$",
    r"^-?\s*(share|share this( article| story)?|share on \w+|tweet|email|print|copy link|save|"
    r"follow us.*|sign up.*|subscribe.*|newsletter.*|download the app.*)$",
    r"^-?\s*(advertisement|sponsored( content)?|ad|skip advertisement|story continues below.*)$",
    r"^-?\s*(loading\.*|please wait\.*|why we wrote this)$",
    r"^-?\s*(image|photo|video)\s*(source|caption|credit)s?\s*[:,].{0,120}$",
    r"^-?\s*(getty images|reuters|afp|ap|epa|bloomberg)(\s*(via|/).{0,40})?$",
    r".*\b(cookies?|consent|privacy (settings|policy)|accept all|manage preferences)\b.{0,160}$",
    r"^-?\s*\|\s*[^|]{1,60}$",              # "| Amman, Jordan" (a dateline chip)
    r"^-?\s*listen to this article.*$",
    r"^-?\s*(by\s+)?[a-z.' -]{2,40}\s*(•|\|)\s*.{0,40}$",   # byline · date chips
))

#: A live-blog timestamp glued to the front of an entry ("- 4 Mins ago", "8:37
#: AM IST, September 2"): stripped before comparison, so a re-timed entry is
#: the same entry.
_LEADING_STAMP = re.compile(
    rf"^-?\s*(?:{_REL_TIME}|{_CLOCK}(?:\s*,\s*{_DATE})?|{_DATE})\s*[-:,|•]?\s*"
)

# -- markers ----------------------------------------------------------------------

_RETRACTION = re.compile(
    r"(this (article|story|report|piece) (has been|was) (retracted|withdrawn|removed|taken down)"
    r"|\bretraction\s*:|\bwe have (retracted|withdrawn|removed) (this|the)"
    r"|editor'?s note\s*:.{0,200}\b(retract|withdr)"
    r"|this (article|story) is no longer available)"
)
_CORRECTION = re.compile(
    r"(\bcorrection\s*:|\bclarification\s*:|an earlier version of this (article|story|report)"
    r"|this (article|story|report) (has been|was) (corrected|amended|updated to correct)"
    r"|we (have )?corrected)"
)

# -- critical tokens ----------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,.]*\d|\d")
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "thirteen": "13", "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "hundred": "100", "hundreds": "100s", "thousand": "1000",
    "thousands": "1000s", "million": "1e6", "millions": "1e6s", "billion": "1e9",
    "dozen": "12", "dozens": "12s", "half": "0.5", "double": "x2", "triple": "x3",
}
_NEGATIONS = frozenset({
    "not", "no", "never", "none", "nobody", "nothing", "neither", "nor", "without",
    "denied", "denies", "deny", "denying", "refused", "refuses", "rejected", "rejects",
    "false", "untrue", "cannot", "unable", "failed", "fails",
})
_MONTHS = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "yesterday", "today", "tomorrow", "tonight",
})
#: Capitalised words that are not names (sentence furniture, honorifics).
_NOT_NAMES = frozenset({
    "mr", "mrs", "ms", "dr", "sir", "the", "a", "an", "in", "on", "at", "of", "and", "but",
    "he", "she", "it", "they", "we", "i", "his", "her", "their", "its", "this", "that",
    "these", "those", "there", "while", "however", "meanwhile", "earlier", "later", "after",
    "before", "according", "speaking", "for", "with", "as", "if", "when", "what", "who",
})
_WORD = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9])")
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_TITLE_RE = re.compile(
    rb"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']", re.IGNORECASE)


def class_severity(name: str) -> int:
    """Position in :data:`DRIFT_CLASSES` (``unclassifiable`` = -1)."""
    try:
        return DRIFT_CLASSES.index(name)
    except ValueError:
        return -1


@dataclass
class _Seg:
    raw: str
    key: str          # the fold, leading stamp stripped
    loose: str        # key with everything but letters and digits removed
    chrome: bool


@dataclass
class DriftClassification:
    drift_class: str
    rule: str
    classifier_version: str = CLASSIFIER_VERSION
    earlier_chars: int = 0
    later_chars: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    retention: Optional[float] = None
    jaccard: Optional[float] = None
    title_changed: bool = False
    titles: dict[str, Optional[str]] = field(default_factory=dict)
    markers: list[str] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    critical: dict[str, list[str]] = field(default_factory=dict)
    facets: list[str] = field(default_factory=list)

    @property
    def flags_dependants(self) -> bool:
        return self.drift_class in FLAGGING_CLASSES

    def evidence(self) -> dict[str, Any]:
        """The JSON evidence recorded with the class (everything but the class)."""
        out = asdict(self)
        out.pop("drift_class", None)
        return out


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EVIDENCE_MAX_CHARS else text[: EVIDENCE_MAX_CHARS - 1] + "…"


def _split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        out.extend(p.strip() for p in _SENTENCE_SPLIT.split(line) if p.strip())
    return out


def _is_chrome(key: str) -> bool:
    if not key:
        return True
    if len(key) > 220:
        return False
    return any(p.match(key) for p in CHROME_PATTERNS)


def segments(text: Optional[str]) -> list[_Seg]:
    """The text as comparable sentences: split on lines then sentence ends,
    folded (:func:`normalize_for_match`), a leading live-blog timestamp
    stripped, furniture marked."""
    segs: list[_Seg] = []
    for raw in _split_sentences(text or ""):
        folded = normalize_for_match(raw)
        key = _LEADING_STAMP.sub("", folded, count=1).strip() or folded
        loose = re.sub(r"[\W_]+", "", key)
        segs.append(_Seg(raw=raw, key=key, loose=loose, chrome=_is_chrome(folded) or not loose))
    return segs


def extract_title(body: Optional[bytes]) -> Optional[str]:
    """The page's ``<title>`` (or ``og:title``), whitespace-collapsed; None
    when the bytes carry neither. Best-effort, never raises."""
    if not body:
        return None
    try:
        head = body[:200_000]
        m = _OG_TITLE_RE.search(head) or _TITLE_RE.search(head)
        if not m:
            return None
        import html  # noqa: PLC0415

        title = html.unescape(m.group(1).decode("utf-8", "replace"))
        title = " ".join(title.split())
        return title[:500] or None
    except Exception:  # noqa: BLE001 — a title is a bonus
        return None


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _critical_tokens(raw: str) -> dict[str, set[str]]:
    """Numbers (digits and number words), dates (month/day words), names
    (capitalised words not opening the sentence, and acronyms) and negations
    of one ORIGINAL-case sentence."""
    nums = {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(raw)}
    words = _WORD.findall(raw)
    lower = [w.lower().strip("'’") for w in words]
    nums |= {_NUMBER_WORDS[w] for w in lower if w in _NUMBER_WORDS}
    dates = {w for w in lower if w in _MONTHS}
    negs = {w for w in lower if w in _NEGATIONS or w.endswith("n't") or w.endswith("n’t")}
    names: set[str] = set()
    for i, w in enumerate(words):
        lw = lower[i]
        if lw in _MONTHS or lw in _NOT_NAMES or len(w) < 2:
            continue
        if w.isupper() and len(w) >= 2:
            names.add(w)
        elif w[0].isupper():
            names.add(w.strip("'’").removesuffix("'s").removesuffix("’s"))
    return {"numbers": nums, "dates": dates, "names": names, "negations": negs}


def _name_stem(name: str) -> str:
    """An inflection-tolerant key for a capitalised name: an acronym is
    itself; a word keeps its first ``max(4, len - 3)`` letters, so a
    demonym or plural of the same name ("Israel"/"Israeli", "Houthis"/
    "Houthi", "Argentina"/"Argentine") is the same name while a different
    one ("Kutla"/"Kotla", "Iran"/"Iraq", "AFP"/"BBC") is not."""
    if name.isupper():
        return name
    low = name.lower()
    return low[: max(4, len(low) - 3)]


def _critical_diff(a: str, b: str) -> dict[str, list[str]]:
    ta, tb = _critical_tokens(a), _critical_tokens(b)
    # A capitalised word is a NAME change only when the other sentence does
    # not carry the same word in lower case (a sentence-opening "The", or
    # "President" -> "president" re-cased, is not a name changing).
    la, lb = set(_words(a)), set(_words(b))
    ta["names"] = {n for n in ta["names"] if n in tb["names"] or n.lower() not in lb}
    tb["names"] = {n for n in tb["names"] if n in ta["names"] or n.lower() not in la}
    out: dict[str, list[str]] = {}
    for k in ta:
        gone, new = sorted(ta[k] - tb[k]), sorted(tb[k] - ta[k])
        if k == "names":
            # "Israel"/"Israeli", "Houthis"/"Houthi": the same name inflected.
            sa, sb = {_name_stem(x) for x in tb[k]}, {_name_stem(x) for x in ta[k]}
            gone = [x for x in gone if _name_stem(x) not in sa]
            new = [x for x in new if _name_stem(x) not in sb]
        if gone or new:
            out[k] = [f"-{x}" for x in gone] + [f"+{x}" for x in new]
    return out


def _changed_words(a: str, b: str) -> int:
    wa, wb = _words(a), _words(b)
    sm = SequenceMatcher(None, wa, wb, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")


def _pair(removed: list[_Seg], added: list[_Seg]) -> tuple[list[tuple[_Seg, _Seg]], list[_Seg], list[_Seg]]:
    """Match each removed sentence to its best reworded counterpart among the
    added ones (greedy, best ratio first). Returns (pairs, unmatched removed,
    unmatched added)."""
    if not removed or not added or len(removed) > MAX_PAIRING or len(added) > MAX_PAIRING:
        return [], removed, added
    cands: list[tuple[float, int, int]] = []
    for i, r in enumerate(removed):
        sm = SequenceMatcher(None, r.key, "", autojunk=False)
        for j, a in enumerate(added):
            sm.set_seq2(a.key)
            if sm.real_quick_ratio() < PAIR_MIN_RATIO or sm.quick_ratio() < PAIR_MIN_RATIO:
                continue
            ratio = sm.ratio()
            if ratio >= PAIR_MIN_RATIO:
                cands.append((ratio, i, j))
    cands.sort(reverse=True)
    used_r: set[int] = set()
    used_a: set[int] = set()
    pairs: list[tuple[_Seg, _Seg]] = []
    for _ratio, i, j in cands:
        if i in used_r or j in used_a:
            continue
        used_r.add(i)
        used_a.add(j)
        pairs.append((removed[i], added[j]))
    return (pairs, [r for i, r in enumerate(removed) if i not in used_r],
            [a for j, a in enumerate(added) if j not in used_a])


def _multiset_diff(a: Sequence[_Seg], b: Sequence[_Seg], attr: str) -> tuple[list[_Seg], list[_Seg]]:
    """(in a not b, in b not a) by ``attr``, counting duplicates."""
    pool: dict[str, int] = {}
    for s in b:
        pool[getattr(s, attr)] = pool.get(getattr(s, attr), 0) + 1
    only_a: list[_Seg] = []
    for s in a:
        k = getattr(s, attr)
        if pool.get(k, 0) > 0:
            pool[k] -= 1
        else:
            only_a.append(s)
    pool = {}
    for s in a:
        pool[getattr(s, attr)] = pool.get(getattr(s, attr), 0) + 1
    only_b: list[_Seg] = []
    for s in b:
        k = getattr(s, attr)
        if pool.get(k, 0) > 0:
            pool[k] -= 1
        else:
            only_b.append(s)
    return only_a, only_b


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / max(1, len(sa | sb))


def classify_pair(
    earlier_text: Optional[str],
    later_text: Optional[str],
    *,
    later_gone: bool = False,
    later_status: Optional[int] = None,
    earlier_title: Optional[str] = None,
    later_title: Optional[str] = None,
) -> DriftClassification:
    """Classify one consecutive pair of versions of one address. Total: never
    raises; deterministic for the same inputs."""
    e_text, l_text = earlier_text or "", later_text or ""
    titles = {"earlier": earlier_title, "later": later_title}
    base = dict(earlier_chars=len(e_text), later_chars=len(l_text), titles=titles)

    if later_gone or (later_status is not None and later_status in (404, 410, 451)):
        return DriftClassification("gone", "later_fetch_gone", facets=["gone"],
                                   counts={"status": int(later_status or 0)}, **base)
    if earlier_text is None and later_text is None:
        return DriftClassification(UNCLASSIFIABLE, "no_text_either_side", **base)
    if later_text is None or not l_text.strip():
        return DriftClassification("gone", "later_kept_no_article_text", facets=["gone"], **base)
    if earlier_text is None or not e_text.strip():
        return DriftClassification(UNCLASSIFIABLE, "earlier_kept_no_article_text", **base)
    if e_text == l_text:
        return DriftClassification("identical_text", "extracted_text_equal",
                                   facets=["identical_text"], **base)

    e_segs, l_segs = segments(e_text), segments(l_text)
    e_content = [s for s in e_segs if not s.chrome]
    l_content = [s for s in l_segs if not s.chrome]
    removed, added = _multiset_diff(e_content, l_content, "key")
    chrome_removed, chrome_added = _multiset_diff(
        [s for s in e_segs if s.chrome], [s for s in l_segs if s.chrome], "key")
    # Typographic: the same sentence modulo punctuation/hyphenation/spacing.
    typo_removed, typo_added = _multiset_diff(removed, added, "loose")
    typographic = len(removed) - len(typo_removed)
    removed, added = typo_removed, typo_added
    pairs, rem_only, add_only = _pair(removed, added)

    e_words, l_words = _words(" ".join(s.raw for s in e_content)), _words(" ".join(s.raw for s in l_content))
    jacc = _jaccard(e_words, l_words)
    retained = len(e_content) - len(rem_only)
    retention = (retained / len(e_content)) if e_content else 1.0

    e_fold, l_fold = normalize_for_match(e_text), normalize_for_match(l_text)
    markers: list[str] = []
    if _RETRACTION.search(l_fold) and not _RETRACTION.search(e_fold):
        markers.append("retraction_marker")
    if _CORRECTION.search(l_fold) and not _CORRECTION.search(e_fold):
        markers.append("correction_notice")

    title_changed = bool(
        earlier_title and later_title
        and re.sub(r"[\W_]+", "", normalize_for_match(earlier_title))
        != re.sub(r"[\W_]+", "", normalize_for_match(later_title))
    )

    changed: list[dict[str, Any]] = []
    critical: dict[str, list[str]] = {}
    changed_words = 0
    critical_pairs = 0
    for r, a in pairs:
        cd = _critical_diff(r.raw, a.raw)
        n = _changed_words(r.raw, a.raw)
        changed_words += n
        if cd:
            critical_pairs += 1
            for k, v in cd.items():
                critical.setdefault(k, [])
                critical[k].extend(x for x in v if x not in critical[k])
        changed.append({"earlier": _clip(r.raw), "later": _clip(a.raw),
                        "changed_words": n, "critical": cd})
    # Removed sentences carry critical tokens too: a number that only lived in
    # a removed sentence is a removed number.
    for r in rem_only:
        for k, v in _critical_tokens(r.raw).items():
            if k == "negations":
                continue
            for x in sorted(v):
                tag = f"-{x}"
                bucket = critical.setdefault(k, [])
                if tag not in bucket and len(bucket) < 40:
                    bucket.append(tag)
    changed.sort(key=lambda c: (-len(c["critical"]), -c["changed_words"]))

    counts = {
        "earlier_sentences": len(e_content), "later_sentences": len(l_content),
        "removed": len(rem_only), "added": len(add_only), "reworded": len(pairs),
        "reworded_critical": critical_pairs, "changed_words": changed_words,
        "typographic": typographic, "chrome_removed": len(chrome_removed),
        "chrome_added": len(chrome_added),
    }
    evidence = dict(
        counts=counts, retention=round(retention, 3), jaccard=round(jacc, 3),
        title_changed=title_changed, markers=markers,
        changed=changed[:EVIDENCE_MAX_ITEMS],
        removed=[_clip(s.raw) for s in rem_only[:EVIDENCE_MAX_ITEMS]],
        added=[_clip(s.raw) for s in add_only[:EVIDENCE_MAX_ITEMS]],
        critical={k: v[:20] for k, v in critical.items()}, **base,
    )

    facets: list[tuple[str, str]] = []
    body_changed = bool(rem_only or pairs or add_only or typographic)
    if "retraction_marker" in markers:
        facets.append(("retracted_or_replaced", "retraction_marker"))
    if "correction_notice" in markers and (rem_only or pairs):
        facets.append(("retracted_or_replaced", "correction_notice_with_edit"))
    if e_content and retention < REPLACED_MAX_RETENTION and jacc < REPLACED_MAX_JACCARD:
        facets.append(("retracted_or_replaced", "article_replaced"))
    if critical_pairs:
        facets.append(("substantive_edit", "critical_token_changed"))
    if rem_only:
        facets.append(("substantive_edit", "sentence_removed"))
    if pairs and not critical_pairs:
        if len(pairs) > MINOR_MAX_SENTENCES or changed_words > MINOR_MAX_CHANGED_WORDS:
            facets.append(("substantive_edit", "rewording_beyond_minor"))
        else:
            facets.append(("minor_edit", "few_words_reworded"))
    if title_changed:
        facets.append(("title_changed", "title_differs"))
    if add_only and not rem_only and not pairs:
        facets.append(("appended", "correction_notice_added" if "correction_notice" in markers
                       else "sentences_added_original_retained"))
    if typographic and not facets:
        facets.append(("minor_edit", "case_punctuation_or_hyphenation_only"))
    if not body_changed and not facets:
        if chrome_removed or chrome_added:
            facets.append(("cosmetic", "page_furniture_only"))
        else:
            # The folds are equal and no furniture moved: case/spacing only.
            facets.append(("minor_edit", "case_or_spacing_only"))

    drift_class, rule = max(facets, key=lambda f: class_severity(f[0]))
    return DriftClassification(drift_class, rule, facets=sorted({f[0] for f in facets},
                                                                  key=class_severity), **evidence)


# -- the cited span ----------------------------------------------------------------

@dataclass
class SpanCheck:
    """Does the span a citation carries still appear in the later text?

    Exact containment under the shared fold, sentence by sentence (a citation
    snippet is often several sentences joined, with page furniture between).
    ``in_earlier`` says the span came from this page at all; ``in_later`` is
    True only when every span sentence present in the earlier text is still
    present in the later one."""

    checked: int = 0
    in_earlier: int = 0
    in_later: int = 0
    missing_in_later: list[str] = field(default_factory=list)

    @property
    def span_in_earlier(self) -> Optional[bool]:
        return None if self.checked == 0 else self.in_earlier > 0

    @property
    def span_in_later(self) -> Optional[bool]:
        if self.checked == 0 or self.in_earlier == 0:
            return None
        return not self.missing_in_later

    def as_dict(self) -> dict[str, Any]:
        return {"checked": self.checked, "in_earlier": self.in_earlier, "in_later": self.in_later,
                "missing_in_later": self.missing_in_later[:EVIDENCE_MAX_ITEMS],
                "span_in_earlier": self.span_in_earlier, "span_in_later": self.span_in_later}


def check_cited_span(span: Optional[str], earlier_text: Optional[str],
                     later_text: Optional[str]) -> SpanCheck:
    """The exact (folded) string check of a cited span against both texts."""
    out = SpanCheck()
    if not span or not span.strip():
        return out
    e_fold = normalize_for_match(earlier_text or "")
    l_fold = normalize_for_match(later_text or "")
    for sent in _split_sentences(span.replace("…", "…\n")):
        sent = sent.rstrip("…").strip()
        if len(_words(sent)) < SPAN_MIN_WORDS:
            continue
        key = normalize_for_match(sent)
        out.checked += 1
        if key in e_fold:
            out.in_earlier += 1
            if key in l_fold:
                out.in_later += 1
            else:
                out.missing_in_later.append(_clip(sent))
    return out
