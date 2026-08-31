# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H2 — COMPOSITION-LAYER INTEGRITY: the judge subsystem's sixth brick.

Every check before this one grades a finding against its CITATIONS or against
what its producer was SHOWN. This one grades the COMPOSITION against the DESK
READS IT CITES, on the one axis the R2 correctness round measured and nothing
in the pass could see: **the composition may not claim what its inputs do not
support** — not a stronger scope, not the opposite direction, and not words it
puts in a named desk's mouth.

THE MEASUREMENT THAT ORDERED THIS BRICK (CORRECTNESS-R2, 2026-08-25, ten
externally graded country reads). The attribution ranked caveat stripping at the
composition second by mechanism mass and called it "the cheapest fix in the
list: the rule already exists and is already obeyed one layer up." Three
separable classes were named, each verified verbatim in the graded packets:

* **SCOPE DELETION (JP).** Same country, same day, two rows apart:

      unit head 10:01Z  "No coordinated narrative appears in THIS DESK'S
                         COLLECTION for Japan through 25 Aug 2026 ..."
      composition 13:01Z "No coordinated narrative appears in JAPAN'S
                         INFORMATION ENVIRONMENT in the latest 72-hour slice ..."

  The desk wrote a collection-scoped negative — exactly what the unit prompts
  and ``absence.v4`` require. The composition deleted the qualifier and
  published a world claim as its BLUF. All three of the JP lane's ``inaccurate``
  verdicts trace to that single deletion, and the ML lane gave the class its
  sharpest formulation: abstention leaking into prose is *"strictly worse than a
  wrong band — it launders 'we didn't look' into 'we looked and it's fine'."*

* **DIRECTION INVERSION UNDER ATTRIBUTION (GB).** The composition wrote *"The
  military-posture read CONFIRMS that the UK is modestly INCREASING offensive
  support ... EXPANDING its standing posture"* while the military_posture head
  it cites opens *"No material change; the United Kingdom's standing offensive
  support to Ukraine remains UNCHANGED in this window."* The composition did not
  merely overstate — it named a desk as its authority for that desk's negation.

* **A CLAIM THE NAMED DESK DOES NOT MAKE (GB, IR).** GB: *"the internal-stability
  read notes only modest, isolated protests"*, where that desk's head is *"No
  unrest or coup-related activity appears in this desk's collection"* — it
  reports no unrest at all. IR: *"the military-posture desk notes that Iran has
  legislated fees ... translating its 'SMART-DEFENCE' doctrine into an
  operational tool"*, where the phrase "smart-defence" appears nowhere in that
  desk's read, which speaks only of a "doctrinal shift". Provenance corruption,
  distinct from caveat stripping.

WHY THIS COULD NOT ALREADY BE CAUGHT. W31 (:func:`verify.unscoped_absence_spans`)
is the existing world-scoped-absence backstop and it is STRUCTURALLY BLIND to the
JP sentence, twice over:

  1. W31 skips any span carrying a citation marker (evidence-anchored spans
     pass). The composition's laundered negative CITES the desk head it
     laundered — citing is precisely what exempts it. The two checks are
     therefore DISJOINT BY CONSTRUCTION, not merely by calibration: W31 owns the
     markerless world negative, this brick owns the cited one, and no span can
     be charged twice.
  2. W31's main-assertion test (``_is_strong_absence_assertion``) requires an
     absence VERB from a fixed set; the JP BLUF's predicate is "appears", which
     is not in it. Recorded here as an adjacent gap, deliberately NOT patched in
     this train — widening that regex moves the live W31 population and would
     need its own stamp rationale.

  This brick therefore routes on the BROADER calibrated grammar
  (``_is_absence_claim`` — the set the floor exemption, the V3 route and V-B all
  share) rather than W31's strong-opener bar. It can afford to: it does not fire
  on phrasing alone, it fires only when the CITED DESK said the same thing WITH
  the scope the composition dropped. The corroborant is what earns the looser
  grammar.

THE SCOPE LEXICON IS NOT THE SCOPE TEST. ``_COLLECTION_SCOPE_MARKERS`` holds
"slice" and "window", and it is right to: at the UNIT layer a desk that says "in
this window" has bounded its denominator. At the COMPOSITION layer that is false
and the JP BLUF is the proof — it says *"in the latest 72-hour slice"* while
asserting something about *"Japan's information environment"*. A time bound
answers WHEN, a collection bound answers WHAT WAS SEARCHED, and only the second
is the scope a laundered absence deleted. So this module carries its own,
sharper predicate (:func:`has_collection_denominator_scope`) — the shared lexicon
minus the two pure-time nouns — and never re-uses ``_has_collection_scope`` for
the equivalence test.

DETERMINISTIC vs JUDGE-SIDE. Three arms below are deterministic because their
truthmaker is text the pass already holds: the cited sub-claim's own body,
captured at synth time in ``citations[].evidence_text``. What is NOT
deterministic is a direction inversion against a desk whose OWN read is
internally ambivalent — the IR military_posture head opens "tighter control of
the Strait of Hormuz" and then reports, as the window's change, a joint
Iran-Oman navigational corridor and a mine-clearance project. A polarity test
sees both poles in one head and correctly declines (the AMBIVALENCE GUARD in
:func:`direction_conflict`, pinned by its own test). That class is routed to the
judge instead, by name and with the sentence, in
:data:`RUBRIC`.

ALL FOUR FOLDS ARE COMPOSITION-ONLY BY CONSTRUCTION — they need the ``[[ref:N]]``
sub-claim convention and the evidence text behind it. A unit finding's citations
resolve to signals, ``_ordinal_evidence_map`` is empty, and every arm is inert:
byte-identical for every unit caller, exactly like R2/R3 one brick over.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Mapping

from .absence_slice import (
    _ABSENCE_SCOPE_QUALIFIERS,
    _COLLECTION_SCOPE_MARKERS,
    _absence_content_terms,
    _absence_route_exclusion,
    _first_absence_marker_pos,
    _is_absence_claim,
)

logger = logging.getLogger(__name__)


def _verify():
    """Lazy accessor — this module is imported BY verify, so the edge runs one
    way at call time only (the ``judge_input_checks`` pattern)."""
    from . import verify

    return verify


# ---------------------------------------------------------------------------
# THE FOUR REASONS
# ---------------------------------------------------------------------------

#: H2-a — a world-scoped absence whose ONLY [[ref:N]] support is a desk-scoped
#: absence about the same thing. SOFT: the composition invented no fact, it
#: deleted a denominator — the overclaim family W31 already lives in, and the
#: same class the unit layer is charged for when it phrases one this way.
ABSENCE_SCOPE_LAUNDERED = "absence_scope_laundered"

#: H2-b1 — an ATTRIBUTED clause ("the X read confirms that Y") whose direction is
#: the OPPOSITE of the cited desk head's own verdict. HARD, and it is the only
#: hard class in this brick: this is the house definition of hard verbatim — "a
#: claim its own cited source contradicts" — with the aggravator that the
#: composition named the source as its authority. It earns the severity the V-D
#: way: the detail must NAME both poles verbatim, from the composition clause and
#: from the desk's own BLUF, or nothing is emitted.
ATTRIBUTION_DIRECTION_CONFLICT = "attribution_direction_conflict"

#: H2-b2 — an ATTRIBUTED clause asserting the PRESENCE of something the cited
#: desk head records as ABSENT. SOFT: the shared-term test that binds the
#: composition's subject to the desk's denial is a heuristic, and a false HARD is
#: the expensive error in every readout this pass has ever produced.
ATTRIBUTION_ASSERTS_DESK_NEGATIVE = "attribution_asserts_desk_negative"

#: H2-b3 — an ATTRIBUTED clause putting a QUOTED phrase in a named desk's mouth
#: that appears nowhere in that desk's cited text. SOFT, same family: a coinage
#: attributed to a desk is a provenance defect, not a fabricated world fact.
ATTRIBUTION_UNGROUNDED_QUOTE = "attribution_ungrounded_quote"

#: The four severities, spelled HERE next to the rationale above and merged into
#: ``verify._FAIL_CLASS_BY_REASON`` — which stays THE table (one lookup, one
#: drift guard). The severities live beside the reasons because the argument for
#: each is the paragraph above it, not a line in a list one file over; the
#: 6-line ceiling on ``verify.py`` is what makes carrying them as a mapping the
#: only honest option, and it is the right one anyway.
FAIL_CLASSES: dict[str, str] = {
    ABSENCE_SCOPE_LAUNDERED: "soft_fail",
    ATTRIBUTION_DIRECTION_CONFLICT: "hard_fail",
    ATTRIBUTION_ASSERTS_DESK_NEGATIVE: "soft_fail",
    ATTRIBUTION_UNGROUNDED_QUOTE: "soft_fail",
}


# ---------------------------------------------------------------------------
# NORMALISATION — the composition prose is model-authored markdown and uses the
# typographic forms a wire does: U+2011 non-breaking hyphens inside desk names
# ("military‑posture"), curly quotes around coinages, NBSP before units. Every
# comparison in this module runs over the normalised form on BOTH sides, so a
# typographic difference can neither create a violation nor hide one.
# ---------------------------------------------------------------------------

_PUNCT_FOLD = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "−": "-", "“": '"', "”": '"', "„": '"', "«": '"',
    "»": '"', "‘": "'", "’": "'", "‚": "'",
    " ": " ", " ": " ", " ": " ", " ": " ",
}
_PUNCT_FOLD_RE = re.compile("|".join(re.escape(k) for k in _PUNCT_FOLD))

#: Markers blanked before any lexical test — the same shapes ``absence_slice``
#: strips, spelled here so this module imports nothing from ``verify`` at
#: definition time.
_MARKER_STRIP_RE = re.compile(r"\[\[ref:\d+\]\]|\[\d+\]")
_REF_ORDINAL_RE = re.compile(r"\[\[ref:(\d+)\]\]")


def normalize(text: Any) -> str:
    """Typographic fold + whitespace collapse. Total: never raises, never None."""
    if not isinstance(text, str) or not text:
        return ""
    folded = _PUNCT_FOLD_RE.sub(lambda m: _PUNCT_FOLD[m.group(0)], text)
    return re.sub(r"[ \t]+", " ", folded)


def _plain(text: str) -> str:
    """Normalised, markdown-emphasis-flattened, marker-blanked, lowercased, and
    stripped of a leading BLUF label.

    The label strip mirrors W31's ``_BLUF_LEAD_RE`` and is not cosmetic: it puts
    the absence idiom back at position 0 so the ``no ...`` opener test can reach
    it, and it keeps the token "bluf" out of a claim's content terms, where it
    would otherwise pad a subject-overlap count with a word about the document's
    own furniture.
    """
    core = normalize(text).strip().lstrip("#-*> ").strip()
    core = re.sub(r"[*_`]+", "", core)
    core = _MARKER_STRIP_RE.sub(" ", core).lower().strip()
    return re.sub(r"^bluf\s*[:—–-]\s*", "", core).strip()


def ref_ordinals(claim: str) -> list[int]:
    """The ``[[ref:N]]`` ordinals a claim cites, in order, deduplicated."""
    out: list[int] = []
    for m in _REF_ORDINAL_RE.finditer(normalize(claim)):
        n = int(m.group(1))
        if n not in out:
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# SCOPE EQUIVALENCE — the JP mechanism's whole hinge (see the module banner).
# ---------------------------------------------------------------------------

#: The two shared-lexicon entries that bound WHEN rather than WHAT WAS SEARCHED.
#: Removing them is the entire difference between this predicate and
#: ``verify._has_collection_scope``, and it is the difference that lets the JP
#: BLUF — "in the latest 72-hour SLICE" about "JAPAN'S INFORMATION ENVIRONMENT" —
#: read as the unscoped world claim it is.
_TIME_ONLY_SCOPE_MARKERS: frozenset[str] = frozenset({"slice", "window"})

#: The DENOMINATOR-scoping lexicon: language that bounds a negative to what was
#: actually collected or searched. Derived from the shared set so a future
#: addition there arrives here automatically, minus the time nouns.
_COLLECTION_DENOMINATOR_MARKERS: tuple[str, ...] = tuple(
    m for m in _COLLECTION_SCOPE_MARKERS if m not in _TIME_ONLY_SCOPE_MARKERS
)


def has_collection_denominator_scope(text: str) -> bool:
    """Does this text bound its negative to a COLLECTION rather than a clock?

    True for "in this desk's collection", "in collected reporting", "among the
    monitored sources", "in the available evidence". FALSE for "in this window"
    and "in the latest 72-hour slice" — those bound WHEN the desk looked, which
    is not the qualifier a laundered absence deletes.
    """
    low = _plain(text)
    return any(m in low for m in _COLLECTION_DENOMINATOR_MARKERS)


# ---------------------------------------------------------------------------
# DESK READS — the cited head's own verdict, and the negatives it recorded.
# ---------------------------------------------------------------------------

_AS_OF_LINE_RE = re.compile(r"^\s*\*[^*\n]*as of[^*\n]*\*\s*$", re.IGNORECASE)
_BLUF_RE = re.compile(
    r"\*\*\s*BLUF\s*:?\s*\*\*\s*:?\s*(.+?)(?:\n\s*\n|\n\s*#|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def desk_verdict_text(head_text: str) -> str:
    """The desk's OWN ANSWER — its BLUF — not its whole body.

    Load-bearing, and the reason :func:`direction_conflict` is usable at all. A
    desk read's body enumerates what it looked at, what it ruled out and what
    would change its mind; run a polarity test over all of that and both poles
    are present in almost every head, so every comparison is suppressed and the
    check is dead. The BLUF is the one sentence that states the desk's verdict,
    and the verdict is what a composition claims to be relaying.

    Falls back to the first non-empty, non-heading, non-``As of`` paragraph when
    a head carries no BLUF label. Returns ``""`` for unusable input — the caller
    then decides nothing, which is the honest degrade.
    """
    text = normalize(head_text)
    if not text.strip():
        return ""
    m = _BLUF_RE.search(text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    for para in re.split(r"\n\s*\n", text):
        stripped = para.strip()
        if not stripped or stripped.startswith("#") or _AS_OF_LINE_RE.match(stripped):
            continue
        return re.sub(r"\s+", " ", stripped).strip()
    return ""


def desk_absence_sentences(head_text: str) -> list[str]:
    """Every sentence in a cited desk head that DENIES something about the world.

    Uses the shared ``_is_absence_claim`` grammar — the same calibrated lexical
    set the floor exemption, the V3 route and V-B all share, so what counts as an
    absence here cannot drift from what counts as one anywhere else — and then
    drops what V-B's ROUTER already drops (``_absence_route_exclusion``):
    continuity, volume, trajectory and premise-clause shapes.

    That second filter is not tidiness, it is the difference between this check
    working and not. Every desk head opens its ledger with a CONTINUITY bullet —
    *"No material change since the prior read on 24 Aug, which also found no
    coordinated narrative"* — and V-G2 already established what that shape is: a
    DIFF between two assessments, not a claim about the world, and a category
    error to decide as one. Left in, it broke this module in BOTH directions on
    the same JP fixture: it presented as an unscoped desk negative (suppressing a
    real laundering via the rule below), while being nothing of the kind — the
    desk had scoped its actual verdict one paragraph up.
    """
    text = normalize(head_text)
    if not text.strip():
        return []
    out: list[str] = []
    for claim in _verify()._segment_claims(text):
        low = _plain(claim)
        if low and _is_absence_claim(low) and _absence_route_exclusion(claim) is None:
            out.append(claim)
    return out


def _denied_terms(sentence: str, *, target_id: str | None) -> set[str]:
    """The content terms a negative sentence DENIES.

    Everything after the sentence's first absence idiom, run through the shared
    ``_absence_content_terms`` screen (function words, the absence/scope
    vocabulary and the desk's own country tokens all dropped). Taking the TAIL
    rather than the whole sentence is what keeps a negative's preamble — "The
    prior read flagged high pressure, but ..." — out of the denial.
    """
    low = _plain(sentence)
    pos = _first_absence_marker_pos(low)
    tail = low[pos:] if pos >= 0 else low
    return _absence_content_terms(tail, target_id=target_id)


def _content_terms(text: str, *, target_id: str | None) -> set[str]:
    """The discriminating content terms of any span (the same screen, no tail
    cut) — used for the SUBJECT-OVERLAP gate every arm applies."""
    return _absence_content_terms(_plain(text), target_id=target_id)


def _absence_subject_overlap(a: str, b: str, *, target_id: str | None) -> set[str]:
    """The terms binding one absence claim to another, as ONE subject.

    Starts from the shared V-B screen, then adds back any SCALE/NOVELTY qualifier
    present on BOTH sides. V-B drops those because they carry no signal against a
    slice of headlines; matching two ABSENCE CLAIMS to each other is the opposite
    case — "coordinated" is the single most discriminating word in "no
    coordinated narrative", and without it the JP match would rest on "narrative"
    alone. Adding only what is on both sides can sharpen the match and can never
    loosen it: this is a strict superset of the intersection, drawn from terms
    both texts already contain.
    """
    shared = _content_terms(a, target_id=target_id) & _content_terms(
        b, target_id=target_id
    )
    a_low, b_low = _plain(a), _plain(b)
    return shared | {
        q for q in _ABSENCE_SCOPE_QUALIFIERS
        if re.search(rf"(?<![\w-]){re.escape(q)}(?![\w-])", a_low)
        and re.search(rf"(?<![\w-]){re.escape(q)}(?![\w-])", b_low)
    }


# ---------------------------------------------------------------------------
# DIRECTION POLARITY — deliberately five families and four opposition pairs. A
# family is a claim about which WAY a thing moved; an opposition is a pair no
# honest relay can hold at once. Everything outside this table is not decided
# here (it goes to the judge, by name, in the rubric below).
# ---------------------------------------------------------------------------

_DIRECTION_FAMILIES: dict[str, re.Pattern[str]] = {
    "increase": re.compile(
        r"(?<![\w-])(?:increas\w*|expand\w*|expansion|ris(?:e|es|ing)|rose|risen"
        r"|grow\w*|growth|escalat\w*|deepen\w*|boost\w*|widen\w*|strengthen\w*"
        r"|intensif\w*|accelerat\w*|surg\w*|elevat\w*|heighten\w*|greater|higher"
        r"|stepped up|ramping up)(?![\w-])"
    ),
    "decrease": re.compile(
        r"(?<![\w-])(?:decreas\w*|declin\w*|falls?|falling|fell|fallen|shrink\w*"
        r"|shrank|shrunk|eas(?:e|es|ed|ing)|reduc\w*|reduction|narrow\w*"
        r"|weaken\w*|lower|lowered|drops?|dropped|dropping|contract(?:s|ed|ing)"
        r"|contraction|diminish\w*|subsid\w*)(?![\w-])"
    ),
    # DIRECTION WORDS ONLY. The absence idioms — "no new", "no material change",
    # "no change" — are deliberately NOT here, and the exclusion is a measured
    # one. Swept across the ten graded compositions, "no new" in this family
    # produced the arm's ONE false hard fail: the BF internal-stability desk
    # opens "No NEW internal-stability threats are observed in this desk's
    # collection" — an ABSENCE claim about threats — and the composition's
    # faithful "a modest INCREASE in elite fracture" was convicted against it,
    # while the desk's very next line records "a modest RISE in elite fracture".
    # A negative is not a direction verdict; reading one as the other is a
    # category error, and the absence grammar already has two arms of its own.
    # A desk that genuinely holds a level says so ("remains unchanged"), which
    # is what the GB head says and why that case still convicts.
    "unchanged": re.compile(
        r"(?<![\w-])(?:unchanged|steady|stable|static|flat|status quo"
        r"|holds? steady|remains? at|remains? the same)(?![\w-])"
    ),
    "tighten": re.compile(
        r"(?<![\w-])(?:tighten\w*|tighter|seals?|sealed|sealing|clos(?:e|es|ed|ing)"
        r"|closure|restrict\w*|blockad\w*|curtail\w*|blocks?|blocked|blocking"
        r"|shut|barred|interdict\w*)(?![\w-])"
    ),
    "open": re.compile(
        r"(?<![\w-])(?:opens?|opened|opening|reopen\w*|re-open\w*|corridors?"
        r"|mine[- ]clear\w*|demining|unblock\w*"
        r"|resum(?:e|es|ed|ing|ption))(?![\w-])"
    ),
}

_OPPOSED_POLES: tuple[frozenset[str], ...] = (
    frozenset({"increase", "decrease"}),
    frozenset({"increase", "unchanged"}),
    frozenset({"decrease", "unchanged"}),
    frozenset({"tighten", "open"}),
)


def direction_poles(text: str) -> dict[str, str]:
    """``{family: the verbatim span that put it there}`` for one piece of prose.

    The verbatim span is not decoration: it is what the HARD detail must name, on
    both sides, before this brick is allowed to charge a hard failure.
    """
    low = _plain(text)
    out: dict[str, str] = {}
    for family, pattern in _DIRECTION_FAMILIES.items():
        m = pattern.search(low)
        if m:
            out[family] = m.group(0)
    return out


def _opposed(a: str, b: str) -> bool:
    return frozenset({a, b}) in _OPPOSED_POLES


# ---------------------------------------------------------------------------
# ATTRIBUTION — the sentence shape "the <desk> read/desk <verb> that Y".
# ---------------------------------------------------------------------------

_ATTRIBUTION_VERBS = (
    "confirms", "confirm", "confirmed",
    "says", "say", "said",
    "reports", "report", "reported",
    "notes", "note", "noted",
    "finds", "find", "found",
    "states", "state", "stated",
    "shows", "show", "showed",
    "indicates", "indicate", "indicated",
    "observes", "observe", "observed",
    "assesses", "assess", "assessed",
    "describes", "describe", "described",
    "records", "record", "recorded",
)

_ATTRIBUTION_RE = re.compile(
    r"(?<![\w-])the\s+([a-z][a-z_-]{2,40}?)\s+"
    r"(?:read|desk|unit|assessment|analysis)\s+"
    r"(" + "|".join(_ATTRIBUTION_VERBS) + r")(?![\w-])\s*(?:that\s+)?",
    re.IGNORECASE,
)


def attributed_clause(claim: str) -> tuple[str, str] | None:
    """``(desk slug, the content attributed to it)``, or ``None``.

    The desk slug is normalised to the analyst-id spelling ("military-posture",
    "military‑posture" and "military posture" all become ``military_posture``) so
    it can be matched against ``citations[].source``. The content is everything
    after the attribution verb — which is exactly the span the desk is being made
    to vouch for.
    """
    text = normalize(claim)
    m = _ATTRIBUTION_RE.search(text)
    if m is None:
        return None
    slug = re.sub(r"[-\s]+", "_", m.group(1).strip().lower())
    content = text[m.end():].strip()
    if not slug or len(content.split()) < 3:
        return None
    return slug, content


# ---------------------------------------------------------------------------
# THE FOUR DETECTORS. Each is PURE: prose in, an optional detail string out.
# A detail string IS the violation (and is what the ledger row carries); ``None``
# means the arm decided nothing, which is never the same as deciding it passed.
# ---------------------------------------------------------------------------

#: Shared terms a composition negative and a desk negative must have in common
#: before one is treated as the other's source. Two, not one: a single shared
#: noun between two negatives on a country desk is coincidence.
_ABSENCE_SUBJECT_OVERLAP = 2

#: Shared terms binding an attributed clause to the desk's denial (H2-b2). One,
#: because the attribution gate in front of it is already narrow — the clause has
#: NAMED the desk it is quoting — and because the term must additionally survive
#: the length floor below.
_NEGATIVE_SUBJECT_OVERLAP = 1

#: Minimum length for a term to bind an attributed clause to a desk denial. Short
#: tokens ("risk", "read", "unit") pair anything with anything.
_BINDING_TERM_CHARS = 5


def absence_scope_laundered(
    claim: str, cited_heads: Mapping[int, str], *, target_id: str | None = None
) -> str | None:
    """H2-a — the JP mechanism: a scoped desk negative republished unscoped.

    Fires only when EVERY one of these holds, which is what keeps it off the
    honest cases:

    1. the composition claim is an absence claim (the shared grammar);
    2. it carries NO collection-DENOMINATOR scope of its own — a time bound
       ("in this window", "in the latest 72-hour slice") is not scope here;
    3. it cites at least one resolvable ``[[ref:N]]`` sub-claim;
    4. among the cited heads there is a negative about the SAME subject (two or
       more shared content terms) that IS collection-scoped; and
    5. no cited head carries an equally-unscoped negative on that subject — if
       some desk already published it as a world claim, the composition did not
       launder anything and W31 owns the defect one layer up.

    A composition that keeps the qualifier passes, which is the behaviour the
    check wants and the reason the fix is cheap: the rule already exists and is
    already obeyed at the desk.
    """
    low = _plain(claim)
    if not low or not _is_absence_claim(low):
        return None
    if has_collection_denominator_scope(claim):
        return None
    ords = ref_ordinals(claim)
    if not ords:
        return None
    claim_terms = _content_terms(claim, target_id=target_id)
    if len(claim_terms) < _ABSENCE_SUBJECT_OVERLAP:
        return None

    scoped_hit: tuple[int, str, set[str]] | None = None
    for n in ords:
        head = cited_heads.get(n)
        if not head:
            continue
        for sentence in desk_absence_sentences(head):
            pos = _first_absence_marker_pos(_plain(sentence))
            denial = _plain(sentence)[pos:] if pos >= 0 else _plain(sentence)
            shared = _absence_subject_overlap(claim, denial, target_id=target_id)
            if len(shared) < _ABSENCE_SUBJECT_OVERLAP:
                continue
            if has_collection_denominator_scope(sentence):
                if scoped_hit is None:
                    scoped_hit = (n, sentence, shared)
            else:
                # (5) a desk already said it unscoped — nothing was laundered.
                return None
    if scoped_hit is None:
        return None
    n, sentence, shared = scoped_hit
    return (
        f"the composition states this negative about the world; its cited "
        f"[[ref:{n}]] states it about a COLLECTION: "
        f"{normalize(sentence).strip()[:400]!r} "
        f"(shared subject: {', '.join(sorted(shared))})"
    )


def direction_conflict(
    content: str, verdict_text: str, *, target_id: str | None = None
) -> str | None:
    """H2-b1 — the GB mechanism: the desk is cited FOR its own negation.

    Both sides are reduced to direction FAMILIES; a violation needs a pair the
    opposition table calls incompatible, plus three guards that decide the
    check's whole precision posture:

    * THE AMBIVALENCE GUARD — if the desk's verdict itself carries the pole the
      composition asserts, nothing is emitted. This is not a rounding error, it
      is the check declining a case it cannot decide: the IR military_posture
      head opens "moving its maritime doctrine toward TIGHTER CONTROL of the
      Strait of Hormuz through a joint Iran-Oman CORRIDOR" — both poles, one
      sentence — and no polarity test can honestly say which one a composition
      inverted. That case is the judge's, by name, in the rubric below.
    * THE SYMMETRIC GUARD — likewise if the composition carries the desk's pole.
    * THE SUBJECT GATE — the two spans must be about the same thing. Two
      direction words in one paragraph are not a contradiction unless they point
      at one subject, and this is the only hard class in the brick.

    Returns the detail naming BOTH verbatim poles, which is how the hard class is
    earned (the V-D rule: a hard verdict must point at the thing it convicts on).
    """
    comp = direction_poles(content)
    desk = direction_poles(verdict_text)
    if not comp or not desk:
        return None
    subject = {
        t for t in (_content_terms(content, target_id=target_id)
                    & _content_terms(verdict_text, target_id=target_id))
        if len(t) >= _BINDING_TERM_CHARS
    }
    if not subject:
        return None
    for c_family, c_span in comp.items():
        if c_family in desk:
            continue  # ambivalence guard (composition side agrees somewhere)
        for d_family, d_span in desk.items():
            if d_family in comp or not _opposed(c_family, d_family):
                continue
            return (
                f"the composition attributes {c_family.upper()} "
                f"({c_span!r}) to a desk whose own verdict is "
                f"{d_family.upper()} ({d_span!r}); "
                f"one subject: {', '.join(sorted(subject))}"
            )
    return None


def asserts_desk_negative(
    content: str, head_text: str, *, target_id: str | None = None
) -> str | None:
    """H2-b2 — the GB internal-stability mechanism: the desk reports NOTHING.

    The composition names a desk and attributes to it the PRESENCE of something
    that desk's read records as absent. The binding is a shared content term of
    at least :data:`_BINDING_TERM_CHARS` characters drawn from what the desk
    actually DENIED (the tail after its absence idiom), and the attributed
    content must carry no negative of its own — a composition faithfully
    relaying the denial says so, and pays nothing.
    """
    if _is_absence_claim(_plain(content)):
        return None
    comp_terms = {t for t in _content_terms(content, target_id=target_id)
                  if len(t) >= _BINDING_TERM_CHARS}
    if not comp_terms:
        return None
    for sentence in desk_absence_sentences(head_text):
        denied = {t for t in _denied_terms(sentence, target_id=target_id)
                  if len(t) >= _BINDING_TERM_CHARS}
        shared = comp_terms & denied
        if len(shared) < _NEGATIVE_SUBJECT_OVERLAP:
            continue
        return (
            f"the composition attributes {', '.join(sorted(shared))} to a desk "
            f"that records their ABSENCE: {normalize(sentence).strip()[:400]!r}"
        )
    return None


_QUOTED_RE = re.compile(r"\"([^\"\n]{3,80})\"")
#: Quoted spans that are not coinages and must never be checked for grounding.
_QUOTE_SKIP_RE = re.compile(r"^[\W\d\s]+$")


def ungrounded_quote(content: str, head_text: str) -> str | None:
    """H2-b3 — the IR mechanism: a coinage put in a named desk's mouth.

    A phrase the composition QUOTES while attributing it to a desk must appear in
    that desk's cited text. IR: *"translating its 'smart-defence' doctrine"*
    attributed to a military_posture read that speaks only of a "doctrinal
    shift". Comparison runs over the normalised, case-folded form on both sides
    with hyphens and spaces unified, so a typographic difference can neither
    create the violation nor hide it.
    """
    head = re.sub(r"[-\s]+", " ", _plain(head_text))
    if not head:
        return None
    for m in _QUOTED_RE.finditer(normalize(content)):
        phrase = m.group(1).strip()
        if len(phrase) < 3 or _QUOTE_SKIP_RE.match(phrase):
            continue
        needle = re.sub(r"[-\s]+", " ", phrase.lower()).strip(" .,;:")
        if needle and needle not in head:
            return (
                f"the composition quotes {phrase!r} as the named desk's own "
                f"words; the phrase appears nowhere in that desk's cited read"
            )
    return None


# ---------------------------------------------------------------------------
# THE FOLD
# ---------------------------------------------------------------------------

#: Every counter this brick can bump, so the receipts are enumerable from code
#: rather than by grepping for ``bump(`` (the V-G8 fidelity rule: an attempt and
#: a survival must both be countable).
COUNTERS: tuple[str, ...] = (
    "composition_integrity_absence_claims_seen",
    "composition_integrity_absence_scope_laundered",
    "composition_integrity_attributions_seen",
    "composition_integrity_attributions_clean",
    "composition_integrity_attribution_direction_conflict",
    "composition_integrity_attribution_asserts_desk_negative",
    "composition_integrity_attribution_ungrounded_quote",
    "composition_integrity_desk_unresolved",
)


def _fold_soft(report: Any, *, text: str, reason: str, markers: list[Any],
               counter: str, detail: str | None) -> Any:
    """One checkable-but-unsupported claim + its ledger row + its counter.

    Byte-identical arithmetic to ``verify._fold_guard_spans`` and to
    ``judge_input_checks._fold_soft``: the denominator grows by one, the
    numerator does not. The hard/soft SEVERITY is not decided here — it comes off
    the one ``_FAIL_CLASS_BY_REASON`` table via ``ClaimVerdict.failed``, so this
    helper is correct for all four reasons and cannot disagree with the table.
    """
    v = _verify()
    span = v.UnsupportedSpan(
        text=text[:2000], reason=reason, markers=list(markers), detail=detail,
    )
    checkable = report.checkable_claims + 1
    supported = report.supported_claims
    out = v.FaithfulnessReport(
        faithfulness_score=(1.0 if checkable == 0 else supported / checkable),
        checkable_claims=checkable,
        supported_claims=supported,
        unsupported_spans=list(report.unsupported_spans) + [span],
        judge_status=report.judge_status,
        judge_unavailable_reason=report.judge_unavailable_reason,
        confidence_ceiling=report.confidence_ceiling,
        branch_scores=report.branch_scores,
        claim_verdicts=list(report.claim_verdicts)
        + [v.ClaimVerdict.failed(span.text, reason, list(markers), detail)],
        counters=dict(report.counters),
        score_denominator=checkable,
        score_state=report.score_state,
        score_state_reason=report.score_state_reason,
    )
    out.bump(counter)
    return out


def _resolve_head(
    slug: str, ords: Iterable[int],
    cited_heads: Mapping[int, str], sources: Mapping[int, str],
) -> tuple[int, str] | None:
    """The desk head an attributed clause is vouching for, or ``None``.

    Two routes, most specific first: a cited ordinal whose ``source`` IS the
    named desk, else — when the clause cites exactly ONE thing — that thing. A
    clause naming a desk it does not cite resolves to nothing and is COUNTED
    (``composition_integrity_desk_unresolved``), never guessed: putting the wrong
    head behind an attribution would manufacture exactly the defect this brick
    exists to catch.
    """
    ord_list = [n for n in ords if cited_heads.get(n)]
    for n in ord_list:
        src = re.sub(r"[-\s]+", "_", str(sources.get(n, "")).strip().lower())
        if src and (src == slug or src.endswith("_" + slug) or slug in src.split("_")
                    or src in slug):
            return n, cited_heads[n]
    if len(ord_list) == 1:
        return ord_list[0], cited_heads[ord_list[0]]
    return None


def fold(
    report: Any, *, body: str, citations: Any, target_id: str | None = None,
) -> Any:
    """H2 — all four arms, one pass over the composition's own claims.

    Named for MODULE-QUALIFIED use (``composition_integrity.fold(...)``) — the
    one call site in ``verify.py`` reads unambiguously that way, and the whole
    train had six lines of module-size ceiling to spend there.

    No-op for every non-composition caller: without the ``[[ref:N]]`` sub-claim
    convention the evidence map is empty and no arm can route. At most ONE
    violation is charged per claim (the attribution arms take precedence over the
    absence arm, and within the attribution arms the order is direction →
    negation → quote), so a sentence that is wrong in two ways costs what a
    sentence that is wrong in one way costs. Never raises: a malformed citation
    list or head degrades to no flag, which is the honest direction.
    """
    v = _verify()
    if not body or not v._uses_subclaim_convention(citations):
        return report
    try:
        cited_heads = {
            n: normalize(t) for n, t in v._ordinal_evidence_map(citations).items()
        }
        sources = v._ordinal_source_map(citations)
        claims = v._segment_claims(body)
    except Exception as exc:  # noqa: BLE001 — degrade-not-drop, never break verify
        logger.warning("verify.composition_integrity.setup_failed err=%s", exc)
        return report
    if not cited_heads:
        return report

    out = report
    for claim in claims:
        try:
            out = _grade_one(
                out, claim=claim, cited_heads=cited_heads, sources=sources,
                target_id=target_id,
            )
        except Exception as exc:  # noqa: BLE001 — one bad claim never breaks a pass
            logger.warning(
                "verify.composition_integrity.claim_failed err=%s claim=%r",
                exc, claim[:120],
            )
    return out


def _grade_one(
    report: Any, *, claim: str, cited_heads: Mapping[int, str],
    sources: Mapping[int, str], target_id: str | None,
) -> Any:
    """One composition claim, at most one violation. See the fold's docstring for
    the precedence rule and why it is a precedence rather than a sum."""
    ords = ref_ordinals(claim)
    attributed = attributed_clause(claim)

    if attributed is not None:
        slug, content = attributed
        report.bump("composition_integrity_attributions_seen")
        resolved = _resolve_head(slug, ords, cited_heads, sources)
        if resolved is None:
            report.bump("composition_integrity_desk_unresolved")
            return report
        n, head = resolved
        detail = direction_conflict(
            content, desk_verdict_text(head), target_id=target_id)
        if detail is not None:
            logger.warning(
                "verify.composition_integrity.direction_conflict desk=%s ref=%s "
                "— %s", slug, n, detail,
            )
            return _fold_soft(
                report, text=claim, reason=ATTRIBUTION_DIRECTION_CONFLICT,
                markers=[n],
                counter="composition_integrity_attribution_direction_conflict",
                detail=detail,
            )
        detail = asserts_desk_negative(content, head, target_id=target_id)
        if detail is not None:
            logger.warning(
                "verify.composition_integrity.asserts_desk_negative desk=%s "
                "ref=%s — %s", slug, n, detail,
            )
            return _fold_soft(
                report, text=claim, reason=ATTRIBUTION_ASSERTS_DESK_NEGATIVE,
                markers=[n],
                counter="composition_integrity_attribution_asserts_desk_negative",
                detail=detail,
            )
        detail = ungrounded_quote(content, head)
        if detail is not None:
            logger.warning(
                "verify.composition_integrity.ungrounded_quote desk=%s ref=%s "
                "— %s", slug, n, detail,
            )
            return _fold_soft(
                report, text=claim, reason=ATTRIBUTION_UNGROUNDED_QUOTE,
                markers=[n],
                counter="composition_integrity_attribution_ungrounded_quote",
                detail=detail,
            )
        report.bump("composition_integrity_attributions_clean")
        return report

    if ords and _is_absence_claim(_plain(claim)):
        report.bump("composition_integrity_absence_claims_seen")
        detail = absence_scope_laundered(claim, cited_heads, target_id=target_id)
        if detail is not None:
            logger.warning(
                "verify.composition_integrity.absence_scope_laundered refs=%s "
                "— %s", ords, detail,
            )
            return _fold_soft(
                report, text=claim, reason=ABSENCE_SCOPE_LAUNDERED,
                markers=ords,
                counter="composition_integrity_absence_scope_laundered",
                detail=detail,
            )
    return report


# ---------------------------------------------------------------------------
# THE JUDGE-SIDE ARM (H2 §2). What a lexical test cannot decide, the judge is
# TOLD — by name, with the graded sentences as the negatives.
#
# The three shapes below are not new doctrine. The composition prompt has always
# carried a collection-scoped absence rule and always asked for faithful
# attribution, and every one of these sentences shipped anyway — which is why
# three of them are now deterministic. This block is for the residue: the fourth
# shape, the IR direction inversion against a desk whose own read holds both
# poles, which no lexical test can honestly call and which the judge is the only
# grader in the system positioned to see.
#
# House idiom followed (``_ABSENCE_JUDGE_SYSTEM``'s "WHAT GOES WRONG IN THIS
# SEAT"): a prose list of failure records, each with its example quoted inline,
# appended as an ADDITIVE block to the composition lead.
#
# ONE DELIBERATE DEPARTURE — the shapes are LETTERED, not numbered. That rubric
# is a SYSTEM prompt, where its "1. / 2. / 3." can never collide with anything;
# this block rides the USER turn, immediately above the NUMBERED CLAIM LIST the
# judge must return one verdict per, in order. A numbered rubric there puts four
# more "N." lines in the same message as the claims and invites exactly the
# miscount the ``(#116d)`` length contract exists to catch — a test double in
# ``test_composition_tiered_evidence`` counted six claims where there were two
# the first time this shipped numbered, which is the cheap version of a real
# judge doing the same thing.
# ---------------------------------------------------------------------------

RUBRIC = (
    "WHAT GOES WRONG BETWEEN A DESK AND A COMPOSITION. Ten country reads were "
    "graded against the world by independent analysts. The composition layer — "
    "not the desks — produced these four shapes, and each is a claim the cited "
    "sub-claim does NOT support. Grade a clause that matches one of them "
    "UNSUPPORTED, or CONTRADICTED where the sub-claim states the opposite.\n"
    "SHAPE A. SCOPE DELETED. A desk bounds a negative to what it COLLECTED; the "
    "composition republishes it as a fact about the world. The desk wrote 'No "
    "coordinated narrative appears in THIS DESK'S COLLECTION for Japan'; the "
    "composition led with 'No coordinated narrative appears in JAPAN'S "
    "INFORMATION ENVIRONMENT'. A time bound is not a collection bound — 'in the "
    "latest 72-hour slice' says when the desk looked, not what it searched, and "
    "does not restore the deleted qualifier.\n"
    "SHAPE B. THE DESK CITED FOR ITS OWN NEGATION. 'The military-posture read CONFIRMS "
    "that the UK is modestly INCREASING offensive support ... EXPANDING its "
    "standing posture', where that read says 'No material change; the United "
    "Kingdom's standing offensive support to Ukraine remains UNCHANGED'. Naming "
    "a desk as the authority for the reverse of its verdict is the worst version "
    "of this family, because the attribution is what a reader trusts.\n"
    "SHAPE C. A CLAIM THE NAMED DESK DOES NOT MAKE. 'The internal-stability read notes "
    "only modest, isolated protests', where that desk reports NO unrest at all; "
    "or a phrase QUOTED as a desk's own words that appears nowhere in it — "
    "'translating its \"smart-defence\" doctrine' attributed to a read that "
    "speaks only of a 'doctrinal shift'. Weakening an absence into a small "
    "positive is still inventing the positive.\n"
    "SHAPE D. DIRECTION INVERTED ACROSS THE LAYER. The military-posture desk reported "
    "the window's maritime change as a joint Iran-Oman NAVIGATIONAL CORRIDOR and "
    "a MINE-CLEARANCE project — an opening — and the composition synthesised it "
    "as 'simultaneously TIGHTENING maritime control'. This one is yours alone: "
    "the mechanical checks decline it, because that desk's own BLUF says "
    "'tighter control' while its body reports the corridor, and no lexical test "
    "can say which the composition inverted. Read what the sub-claim reports as "
    "HAVING CHANGED, not the word its BLUF chose, and mark the clause "
    "CONTRADICTED when the composition sends the change the other way.\n"
    "A composition that KEEPS the desk's qualifier, relays the desk's direction, "
    "or names a disagreement it found is behaving correctly and is SUPPORTED. "
    "Do not penalise ordinary summarisation, shortening, or aggregation — only "
    "the four shapes above.\n\n"
)


# ``fold``, ``RUBRIC``, ``FAIL_CLASSES`` and ``COUNTERS`` are deliberately short:
# they are only ever reached MODULE-QUALIFIED (``composition_integrity.fold``),
# where the module name carries the meaning and the call site costs one line of a
# ceiling that had six. Everything else is spelled in full for the tests.
__all__ = [
    "ABSENCE_SCOPE_LAUNDERED",
    "ATTRIBUTION_ASSERTS_DESK_NEGATIVE",
    "ATTRIBUTION_DIRECTION_CONFLICT",
    "ATTRIBUTION_UNGROUNDED_QUOTE",
    "COUNTERS",
    "FAIL_CLASSES",
    "RUBRIC",
    "absence_scope_laundered",
    "asserts_desk_negative",
    "attributed_clause",
    "desk_absence_sentences",
    "desk_verdict_text",
    "direction_conflict",
    "direction_poles",
    "fold",
    "has_collection_denominator_scope",
    "normalize",
    "ref_ordinals",
    "ungrounded_quote",
]
