"""THE UNSUPPORTED-BY-SPINE MARKER — seven classes, two detectors (D-1 §2.4).

WHAT THIS IS FOR. The Assessment is the one surface in the tower that is allowed
to publish a flagged sentence rather than drop it. That is the journal's off-chain
privilege (``actor_critic``: *"the entry itself is NEVER mutated; the verdict is a
side critique row"*) imported for the one channel that has earned it — the
Assessment cannot write a fact into the record, because the record is the
assembly and the assembly is quotation. The record itself may never do this
(VOICE §3.4 C1/C4): rule (g)'s DROP stays the differentiator everywhere else.

So this module does not gate and does not edit. It MARKS, with character offsets,
and the marks publish beside the prose.

THE SPLIT IS THE SPEC. Six classes are deterministic and run here; one is a
judge's and does not exist until D-3 lands its arm:

  * ``rank``           — a cross-block ranking claim. No spine block can make one:
                         every block is bounded to ONE target and ONE question, so
                         "the top risk this cycle" is a claim about a comparison
                         the record never performed.
  * ``superlative``    — unbounded scope attached to a claim resting on
                         target-scoped blocks ("worldwide", "anywhere").
  * ``causal_link``    — an inferential connective BETWEEN two ordinals in one
                         sentence. The MAY-NOT list from design §B.2: the tier may
                         relay two blocks; it may not weld them.
  * ``scope_widening`` — the M-8 shape at this tier: a collection-scoped negative
                         in the cited span republished as a world negative. This
                         one is not re-implemented — it IS
                         ``composition_integrity.absence_scope_laundered``, run
                         with the SPINE's spans as the cited heads. Reuse, not a
                         second opinion.
  * ``instrument_prose`` — THE 2026-09-05 CLASS, and it exists because the
                         marker surface was UNDER-REPORTING. The first natural
                         post-fix live Assessment graded ``citation_support``
                         0.4286 with all four ``soft_fail`` claims in one class
                         — the voice narrating the RECORD'S OWN MACHINERY as if
                         it were a claim about the world — while this module
                         found ZERO marks on the same body. A marker surface
                         that publishes a checked zero over the exact defect
                         that failed the read is the M-11 shape in a new
                         costume, so the class is deterministic and runs here.
                         See :data:`_INSTRUMENT_TERMS` for what it fires on and
                         the two exemptions for what it may not tax.
  * ``aperture_unrostered`` — THE 2026-09-06 CLASS (G3, D-6 §6.1's second
                         follow-on). The body shape MANDATES a
                         ``## What this reading misses`` section, and until this
                         train the voice was handed only COUNTS to write it
                         from — so it guessed, and the N=5 replay caught it
                         guessing: *"provides no coverage of Central Asia or
                         broader South America"* on a record that names neither.
                         The record knew the real answer and was throwing it
                         away at the prompt boundary: the drop ledger carries
                         the desk, target and head title of every read this
                         surface saw and did not carry, and D-2b persisted
                         ``coverage_roster`` beside the coverage ledger.
                         ``assessment_prompts.declared_aperture`` now hands all
                         of it over, and this class is its fence: in the
                         aperture section ONLY, a proper name that appears
                         nowhere in the roster, the drop ledger, the carried
                         blocks or their quoted spans is a name the voice
                         supplied from outside the record. See
                         :func:`aperture_vocabulary`.
  * ``aperture_guess``  — THE 2026-09-26 CLASS (o4). The SAME section, the
                         other half of the same failure. ``aperture_unrostered``
                         catches the voice naming a place the record never saw;
                         this one catches it naming NO PLACE AT ALL — an absence
                         asserted over a TOPIC CLASS ("gaps in coverage of
                         possible proliferation developments, economic-coercion
                         or escalation dynamics") on a record that handed over
                         26 uncarried units BY NAME. The live 2026-09-25 12:35Z
                         world read wrote exactly that, and the section's whole
                         warrant is the opposite instruction: NAME THOSE. A
                         generalisation is not a smaller version of naming them,
                         it is the guess the declared aperture exists to
                         retire. See :func:`aperture_declared_units`.
  * ``uncited``        — THE 2026-09-24 CLASS (H8). A sentence that names NO
                         ordinal at all. The ordinal fence says every sentence
                         names at least one [[ref:N]]; a sentence with none is
                         unsupported by construction, whatever it says — the
                         world voice's 00:37Z read lost one of its seven
                         checkable sentences exactly this way (the roster
                         sentence, written bare). Headings and bold-only
                         lines (the headline, a "**BLUF:**" label) are exempt;
                         everything else in the body is a claim and carries
                         its warrant or is marked.
  * ``fact``           — an assertion no spine span states (the reactor-shutdown
                         class). Deterministic containment cannot catch a claim
                         two paraphrase hops from a record that denies it; a judge
                         can, because under §3.5 the Assessment's evidence map IS
                         the spine's spans. Until that arm exists the payload says
                         ``judge_state: "not_run"`` in words.

THE CHECKED NEGATIVE, AGAIN. ``unsupported: []`` means nothing on its own — §1.6
rule 2's whole argument. :func:`find_unsupported` therefore returns a COUNTER
block beside the marks (sentences examined, classes run, which detector ran and
which did not), and the payload publishes it. A "nothing flagged" line that does
not say what it checked is the M-11 defect in a new costume, one tier up.

OFFSETS ARE UTF-16 CODE UNITS, and that is deliberate. The reader marks the prose
by slicing the body it was served — ``body.slice(char_start, char_end)`` in
JavaScript, whose index unit is the UTF-16 code unit. Python string indices are
CODE POINTS. The two agree for every character in the BMP (i.e. for every
character any live read has ever contained) and disagree by one per astral
character. Emitting the reader's unit means the mark lands on the words it names
even the first time an emoji or a rare CJK extension reaches a body — and the
alternative fails silently, moving every later mark left by one. The same lesson
as D-2's byte offsets: the offset must be taken in the unit of the thing that
will index with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..provenance.composition_integrity import (
    absence_scope_laundered,
    has_collection_denominator_scope,
    ref_ordinals,
)
from ..provenance.text_fold import normalize_for_match
from .assembly_spans import first_sentence_end
from .assessment_prompts import declared_aperture, record_arithmetic
from .unit_names import payload_names, unit_label

__all__ = [
    "DETECTOR_DETERMINISTIC",
    "DETECTOR_JUDGE",
    "JUDGE_STATE_NOT_RUN",
    "JUDGE_STATE_RAN",
    "Sentence",
    "UNSUPPORTED_APERTURE_GUESS",
    "UNSUPPORTED_APERTURE_UNROSTERED",
    "UNSUPPORTED_CAUSAL_LINK",
    "UNSUPPORTED_CLASSES",
    "UNSUPPORTED_DETERMINISTIC_CLASSES",
    "UNSUPPORTED_FACT",
    "UNSUPPORTED_INSTRUMENT_PROSE",
    "UNSUPPORTED_JUDGE_CLASSES",
    "UNSUPPORTED_RANK",
    "UNSUPPORTED_SCOPE_WIDENING",
    "UNSUPPORTED_SUPERLATIVE",
    "UNSUPPORTED_UNCITED",
    "aperture_declared_units",
    "aperture_unit_identifiers",
    "aperture_vocabulary",
    "find_unsupported",
    "mark_text",
    "segment_sentences",
    "spine_ordinals",
    "spine_span_text",
    "utf16_offset",
]


UNSUPPORTED_RANK: str = "rank"
UNSUPPORTED_SUPERLATIVE: str = "superlative"
UNSUPPORTED_CAUSAL_LINK: str = "causal_link"
UNSUPPORTED_SCOPE_WIDENING: str = "scope_widening"
UNSUPPORTED_INSTRUMENT_PROSE: str = "instrument_prose"
UNSUPPORTED_APERTURE_UNROSTERED: str = "aperture_unrostered"
UNSUPPORTED_APERTURE_GUESS: str = "aperture_guess"
UNSUPPORTED_UNCITED: str = "uncited"
UNSUPPORTED_FACT: str = "fact"

#: §2.4's table, in its order, with the 2026-09-05 class appended before the
#: judge's. The reader has copy for exactly these, and renders an unknown class
#: as its bare name — so adding one is a UI change and not a silent one, and
#: ``AssessmentBand.CLASS_COPY`` + ``assemblyModel.UnsupportedClass`` moved in
#: the same commit.
UNSUPPORTED_CLASSES: tuple[str, ...] = (
    UNSUPPORTED_RANK,
    UNSUPPORTED_SUPERLATIVE,
    UNSUPPORTED_CAUSAL_LINK,
    UNSUPPORTED_SCOPE_WIDENING,
    UNSUPPORTED_INSTRUMENT_PROSE,
    UNSUPPORTED_APERTURE_UNROSTERED,
    UNSUPPORTED_APERTURE_GUESS,
    UNSUPPORTED_UNCITED,
    UNSUPPORTED_FACT,
)
UNSUPPORTED_DETERMINISTIC_CLASSES: tuple[str, ...] = (
    UNSUPPORTED_RANK,
    UNSUPPORTED_SUPERLATIVE,
    UNSUPPORTED_CAUSAL_LINK,
    UNSUPPORTED_SCOPE_WIDENING,
    UNSUPPORTED_INSTRUMENT_PROSE,
    UNSUPPORTED_APERTURE_UNROSTERED,
    UNSUPPORTED_APERTURE_GUESS,
    UNSUPPORTED_UNCITED,
)
UNSUPPORTED_JUDGE_CLASSES: tuple[str, ...] = (UNSUPPORTED_FACT,)

DETECTOR_DETERMINISTIC: str = "deterministic"
DETECTOR_JUDGE: str = "judge"

#: The judge arm is D-3's. Saying so on the row is the difference between "no
#: fabricated fact was found" and "nobody looked for one".
JUDGE_STATE_NOT_RUN: str = "not_run"
JUDGE_STATE_RAN: str = "ran"

#: The version this pass publishes under, so a lexicon change is legible in the
#: data rather than only in a diff. Bumped with the lexicons, never silently.
#: v2 (2026-09-05) adds the ``instrument_prose`` class and its lexicon.
#: v3 (2026-09-06) adds the ``aperture_unrostered`` class and the record
#: vocabulary it fences the blind-spot section to.
#: v5 (2026-09-26) adds the ``aperture_guess`` class and the UNIT set — not the
#: word vocabulary — the blind-spot section's absence sentences are fenced to.
UNSUPPORTED_VERSION: str = "unsupported.v5"


# ---------------------------------------------------------------------------
# THE LEXICONS — published with the metric, per §2.4 ("Lexicon-based, published
# with the metric"). Every entry earns its place by naming a comparison or a
# link the spine cannot make. They are matched on WORD boundaries over the
# folded text, so "leads" matches and "leaderships" does not.
# ---------------------------------------------------------------------------

#: RANK. A claim that puts one subject above the others. The spine's blocks are
#: ORDERED (severity, then cited mass) but the order is not a ranking of the
#: WORLD — it is a ranking of what this surface carried, and §1.5.3's whole
#: apparatus exists because the tier kept crowning on a key with sd 0.024.
_RANK_TERMS: tuple[str, ...] = (
    "the top risk", "the top story", "the top concern", "the top driver",
    "the most consequential", "the most significant", "the most important",
    "the most serious", "the most urgent", "the biggest", "the largest",
    "the gravest", "the worst", "the greatest",
    "the primary driver", "the main driver", "the principal driver",
    "the key driver", "the dominant", "the leading", "the foremost",
    "the single most", "first among", "chief among", "above all",
    "defines the", "outranks", "outweighs", "eclipses", "dominates",
    "takes precedence", "matters most", "leads this cycle",
    "the central story", "the defining",
)

#: SUPERLATIVE / UNBOUNDED SCOPE. The inverse of the collection-denominator
#: predicate (§3.2): these widen a claim off its block's target and onto the
#: world. "Globally" on a page whose every block names one country is a claim
#: about 195 countries resting on eight.
_UNBOUNDED_SCOPE_TERMS: tuple[str, ...] = (
    "worldwide", "globally", "global", "everywhere", "anywhere",
    "across the world", "around the world", "in every region",
    "in every country", "universally", "no country", "all countries",
    "all regions", "the whole world", "internationally",
)

#: CAUSAL LINK. Design §B.2's MAY-NOT list, verbatim in spirit: the connectives
#: that turn two relayed blocks into one authored inference.
_CAUSAL_TERMS: tuple[str, ...] = (
    "indicating", "underpinning", "sustaining", "because", "which drives",
    "driving", "reinforcing", "compounding", "as a result", "therefore",
    "consequently", "stems from", "stemming from", "leading to", "amplifies",
    "amplifying", "feeds", "feeding", "fuels", "fuelling", "fueling",
    "explains", "which is why", "so that", "thereby", "resulting in",
    "in turn", "and so", "hence",
)

#: INSTRUMENT PROSE. The vocabulary of the MACHINE, in a channel whose subject
#: is the world. Every entry names something the record shows the voice so it can
#: DECIDE — and that the evidence map cannot grade a claim about:
#:
#:   * the per-block ATTRIBUTION LINE (``assembly_render._attribution``) — cited
#:     mass, verify score, severity, produced-at. Rendered on the record's page,
#:     absent from ``spine_span_text``, therefore unsupported by construction
#:     however accurately it is copied. Three of the four live ``soft_fail``
#:     claims of 2026-09-06 were exactly this.
#:   * the record's own SHELVING — read counts, block counts, "meta-statements",
#:     which blocks were substantive. The record states what it carried; it makes
#:     no claim about what its blocks CONSIST OF.
#:   * what the TIER LACKS. The aperture is legitimate and mandated (body shape
#:     section 4) — but as a statement about the WORLD. "The tier does not
#:     contain granular data on export volumes" reviews the instrument.
#:
#: What is deliberately ABSENT from this lexicon is every counter the record
#: actually publishes about itself — reads carried, below the floor, candidates
#: ranked, pairs examined, roster units, top-share, ratio. Those ride in every
#: citation's ``evidence_text`` under ``EVIDENCE_ARITHMETIC_RULE`` (P1,
#: ``fa3d2d85``) and ARE gradeable: the same live body that failed four claims
#: had "Six reads fell below the verification floor" graded *supported*. The
#: arithmetic exemption below is the mechanical form of that distinction.
_INSTRUMENT_TERMS: tuple[str, ...] = (
    # -- the per-block attribution line -------------------------------------
    "cited mass", "citation mass", "high-mass", "low-mass",
    "verification score", "verify score", "verification level",
    "verified score", "verification rating", "verification data",
    "well-verified", "poorly-verified", "better-verified", "less-verified",
    "highly-verified", "verification above", "verification below",
    # -- the record's own shelving ------------------------------------------
    "read counts", "read count", "block counts", "block count",
    "meta-statement", "meta-statements", "meta-commentary",
    "lacks substantive", "lack substantive", "lacking substantive",
    "no substantive content", "peripheral role",
    # -- what the tier lacks ------------------------------------------------
    "the tier does not contain", "the tier does not carry",
    "the tier does not include", "the tier lacks", "this tier lacks",
    "the record does not contain", "the record does not include",
    "the record lacks", "the surface lacks", "this surface lacks",
    "the assembly lacks", "no granular data", "lacks granular",
)

#: THE APERTURE SECTION'S OWN HEADING, folded. ``ASSESSMENT_BODY_SHAPE`` item 4
#: names it exactly and the shape mechanics put a header alone on its line, so
#: the section is findable without parsing markdown structure. The class runs in
#: THIS SECTION ONLY: everywhere else in the body a proper noun is a claim about
#: what the record SAYS, and the ordinal fence plus the ``fact`` class already
#: own that. Here it is a claim about what the record COULD NOT SEE, which has a
#: different truthmaker — the ledger — and until this train had none at all.
_APERTURE_HEADING: str = "what this reading misses"

#: A run of capitalised words, taken on the RAW sentence because the offset has
#: to index the sentence the reader slices (the fold is not length-preserving —
#: the module's standing lesson). Internal lowercase joiners are admitted so
#: "Strait of Hormuz" and "Republic of the Congo" are ONE name rather than two
#: fragments, which matters because a fragment is more likely to collide with
#: the record's vocabulary by accident than a whole name is.
_PROPER_RUN_RE = re.compile(
    r"(?<![\w'\u2019-])"
    r"[A-Z][\w'\u2019-]*"
    r"(?:\s+(?:of|the|and|de|del|da|la|le|el|los|las|van|von)\s+[A-Z][\w'\u2019-]*"
    r"|\s+[A-Z][\w'\u2019-]*)*"
)

#: Capitalised words that are NOT names: sentence openers, connectives, the
#: calendar, and the record's own furniture. A run made only of these is not a
#: name and never marked. Deliberately generous — a false ``aperture_unrostered``
#: publishes a mark on an honest blind-spot sentence, which is the expensive
#: error in a class whose whole purpose is to reward naming real things.
_APERTURE_STOPWORDS: frozenset[str] = frozenset("""
a an and the this that these those it its there here they them their
no not none nothing neither nor never without
what where which while when whether why how who whom whose
but or so yet however although though because since unless until
across between among within without beyond behind beside beyond
both each every any all some most more less fewer few several many much
one two three four five six seven eight nine ten first second third fourth
given taken together finally consequently likewise similarly additionally
moreover furthermore conversely instead rather notably crucially importantly
overall meanwhile still also thus hence therefore nonetheless nevertheless
block blocks record records read reads reading assessment coverage tier
january february march april may june july august september october november
december monday tuesday wednesday thursday friday saturday sunday
bluf ref refs
""".split())


#: Where a marked clause ends: the next clause boundary, or the sentence end.
#: Marking to the end of the CLAUSE rather than the end of the SENTENCE is what
#: makes the render readable — "defines the top risk in this cycle" underlined,
#: not the whole 40-word sentence it sits inside.
_CLAUSE_BREAK_RE = re.compile(r"[,;:—–]|\swhile\s|\swhereas\s|\sbut\s|\sand\s")

#: A marked clause never runs longer than this. A mark that covers a paragraph
#: has stopped being a mark.
MAX_MARK_CHARS: int = 160

#: A ``[[ref:N]]`` marker CONTAINS A COLON, and a colon is a clause break. Left
#: alone, the clause scan cuts a mark in half INSIDE the marker — "the top risk
#: in this cycle [[ref" — which is an underline that ends mid-token on the
#: reader's page. The mask replaces each marker with a same-length run of a
#: neutral character, so every offset taken against the masked string still
#: indexes the raw sentence exactly. Same discipline as the fold rule: if a
#: transform is used to FIND an offset, it has to be length-preserving.
_MASK_CHAR: str = " "  # a plain space: not punctuation, not a word character

_REF_RE = re.compile(r"\[\[ref:(\d{1,3})\]\]")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")

#: What turns a ranking word — or a causal connective — into a DENIAL of one.
#: Matched in the 40 characters BEFORE the lexicon hit: "neither X outweighs Y",
#: "no single thread dominates", "treated as independent strands RATHER THAN
#: mutually reinforcing dynamics". A denial of a link is not the link, and it is
#: usually the record's own finding being relayed (the tension detector found no
#: pair), so marking it would tax exactly the sentence the contract asks for.
_NEGATOR_RE = re.compile(
    r"(?<![\w-])(?:no|not|neither|nor|none|never|without|hardly|nothing)(?![\w-])"
    r"|rather than|instead of",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Sentence:
    """One segmented sentence, with its offsets into the ORIGINAL body.

    ``index`` counts every unit the segmenter produced — headings and the as-of
    line included — so a sentence index is stable under an edit that adds a
    heading, and a reader comparing ``markers[].sentence_index`` with
    ``unsupported[].sentence_index`` is comparing the same axis.
    """

    index: int
    start: int
    end: int
    text: str


def utf16_offset(body: str, index: int) -> int:
    """``index`` (a Python code-point index) as a UTF-16 code-unit offset.

    Identical below U+10000, which is every character in every body measured for
    this program; +1 per astral character before ``index`` otherwise. See the
    module banner for why the reader's unit is the right one to publish.
    """
    return len(body[:index].encode("utf-16-le")) // 2


def mark_text(body: str, mark: Mapping[str, Any]) -> str:
    """The words a mark actually covers, sliced the way the READER slices.

    The inverse of :func:`utf16_offset`, and the reason a test can prove the
    marks land on their own text rather than trusting the arithmetic that
    produced them.
    """
    units = body.encode("utf-16-le")
    start = int(mark.get("char_start") or 0) * 2
    end = int(mark.get("char_end") or 0) * 2
    return units[start:end].decode("utf-16-le")


def segment_sentences(body: str) -> list[Sentence]:
    """Every sentence in ``body``, with offsets, headings and stamps included.

    Paragraph-aware (a heading is its own unit and never glues to the sentence
    after it) and abbreviation-aware, by reusing D-2's ``first_sentence_end`` —
    the same segmentation the spine's lead spans were cut with, which is what
    keeps "U.S." from ending a sentence in one module and not the other.
    """
    out: list[Sentence] = []
    index = 0
    for line_start, line in _lines(body):
        if not line.strip():
            continue
        if _HEADING_RE.match(line):
            out.append(Sentence(index, line_start, line_start + len(line), line))
            index += 1
            continue
        pos = 0
        while pos < len(line):
            end = first_sentence_end(line, pos)
            raw = line[pos:end]
            if raw.strip():
                lead = len(raw) - len(raw.lstrip())
                trail = len(raw) - len(raw.rstrip())
                out.append(Sentence(
                    index,
                    line_start + pos + lead,
                    line_start + end - trail,
                    raw.strip(),
                ))
                index += 1
            if end <= pos:
                break
            pos = end
    return out


def _lines(body: str) -> list[tuple[int, str]]:
    """``(offset, line)`` for every line, offsets into the original body."""
    out: list[tuple[int, str]] = []
    pos = 0
    for line in str(body or "").split("\n"):
        out.append((pos, line))
        pos += len(line) + 1
    return out


def spine_ordinals(spine: Mapping[str, Any]) -> list[int]:
    """The ordinals the spine actually carries."""
    return [int(b.get("ordinal") or 0) for b in (spine.get("blocks") or [])]


def spine_span_text(spine: Mapping[str, Any]) -> dict[int, str]:
    """``ordinal -> the block's quoted words``.

    THIS IS THE EVIDENCE MAP (§3.5): the words the spine carried, which is the
    only thing the Assessment was shown and therefore the only thing it may rest
    on. The map is defined by what the VOICE WAS HANDED, not by what a tier
    happens to quote — that is the 2026-09-05 lesson (an Assessment graded 0.00
    on six faithful claims because the map omitted the arithmetic printed on the
    record's own page), and it is why this function joins a block's spans rather
    than picking one.

    P3 LANE A, AND IT FALLS OUT OF THAT DEFINITION RATHER THAN BEING ADDED TO
    IT. A COUNTRY block carries TWO spans: the quoted lead, and the origin desk
    head IN FULL under ``context_body``. The country prompt prints both — the
    record quotes the lead, the DESK READS IN FULL section prints the body — so
    both are fidelity-to-spine and both belong in the map. The join below
    already produces exactly that, ``lead + "\n" + body``, with no clause for
    it: an ordinal's evidence is every span the ordinal carried. World and
    thematic blocks carry one span and the result is byte-identical to D-6's.

    STEP E CHANGES NOTHING HERE EITHER, AND THAT IS THE POINT. A world block
    now also carries a ``context_body`` span — that country's own assessment in
    full — and the world prompt prints it under THE COUNTRY READS IN FULL. So it
    is fidelity-to-spine by the same definition, it enters the map through the
    same join, and the judge grades a world claim against the country voice's
    whole text rather than against the one sentence the record quotes. No clause
    was added for the country tier and none is added for this one: the map is
    what the voice was handed, and the join says so. A THEMATIC block carries no
    context span and its evidence is byte-identical to D-6's.
    """
    out: dict[int, str] = {}
    for block in spine.get("blocks") or []:
        ordinal = int(block.get("ordinal") or 0)
        if not ordinal:
            continue
        out[ordinal] = "\n".join(
            str(s.get("text") or "") for s in (block.get("spans") or [])
        )
    return out


def _target_scoped_ordinals(spine: Mapping[str, Any]) -> set[int]:
    """Ordinals whose block is bounded to ONE target — i.e. almost all of them.

    A block with no ``target_id`` (a thematic desk head, say) is not
    target-scoped, and a world-scope word resting only on those is not a
    widening.
    """
    out: set[int] = set()
    for block in spine.get("blocks") or []:
        if block.get("target_id"):
            out.add(int(block.get("ordinal") or 0))
    return out


def _folded(text: str) -> str:
    return normalize_for_match(text)


def _term_in(term: str, text: str) -> bool:
    """Word-boundary containment over the folded text."""
    folded = _folded(text)
    needle = _folded(term)
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", folded) is not None


def _mask_refs(sentence: str) -> str:
    """``sentence`` with every ``[[ref:N]]`` blanked, LENGTH PRESERVED."""
    return _REF_RE.sub(lambda m: _MASK_CHAR * (m.end() - m.start()), sentence)


def _first_negation(sentence: str) -> int:
    """Where the sentence's denial STARTS, or ``0``.

    The calibrated absence vocabulary is imported rather than re-listed —
    ``absence_slice._ABSENCE_MARKERS`` is the same set the floor exemption, the
    V3 route, V-B and ``desk_absence_sentences`` all share, and a second copy
    here would drift from all four the first time one is tuned. What is NOT
    imported is that module's own position helper: it indexes a FOLDED string,
    and a fold that deletes a soft hyphen does not index the raw sentence. The
    idiom is found here on the raw text, case-insensitively, the same way every
    other lexicon in this module is.
    """
    from ..provenance.absence_slice import _ABSENCE_MARKERS

    hit = _earliest_term(sentence, tuple(_ABSENCE_MARKERS))
    if hit is not None:
        return hit[0]
    opener = re.search(r"(?<![\w-])(?:no|none|nothing|neither|not)\s", sentence, re.I)
    return opener.start() if opener is not None else 0


def _find_term(sentence: str, term: str) -> int:
    """Where ``term`` starts in ``sentence`` (raw index), or ``-1``.

    Case-insensitive on the RAW string. The fold is not length-preserving — a
    U+00AD vanishes under it — so an offset taken against folded text does not
    index the sentence it claims to describe. That is D-2's lesson, and this is
    where it applies at this tier.
    """
    match = re.search(rf"(?<!\w){re.escape(term)}(?!\w)", sentence, re.IGNORECASE)
    return -1 if match is None else match.start()


def _earliest_term(sentence: str, terms: Sequence[str]) -> tuple[int, str] | None:
    """The EARLIEST lexicon hit in the sentence, not the first one in the list.

    List order is an accident of authoring; sentence order is what a reader
    sees. Taking the earliest makes the mark land on the head of the claim
    ("defines the top risk…") rather than on a phrase inside it ("the top
    risk…"), and it makes the choice independent of how the lexicon is sorted.
    """
    best: tuple[int, str] | None = None
    for term in terms:
        at = _find_term(sentence, term)
        if at < 0:
            continue
        if best is None or at < best[0] or (at == best[0] and len(term) > len(best[1])):
            best = (at, term)
    return best


#: What may stand between two words of an instrument term in real prose. The
#: core plane writes U+2011 NON-BREAKING HYPHEN — ``text_fold``'s banner measures
#: it on 58.2% of graded claims, and the live 2026-09-06 body carries it inside
#: "high‑mass", "well‑verified" and "meta‑statements". The FOLD maps it to a
#: plain hyphen, which is why :func:`_term_in` sees these terms; but the fold is
#: not length-preserving, so the RAW scan that produces an OFFSET cannot use it.
#: The tolerance therefore lives in the PATTERN instead of in a transform of the
#: text, which is the same lesson from the other direction: never take an offset
#: against a string you rewrote.
_FLEX_SEP: str = r"[\s\u00a0\u202f\u2007\u2010-\u2015\u2212-]+"
_FLEX_CACHE: dict[str, re.Pattern[str]] = {}


def _flex_re(term: str) -> re.Pattern[str]:
    """``term`` as a pattern whose spaces and hyphens accept either, and any
    dash the core plane emits. Cached: the lexicon is fixed and the scan runs
    once per term per sentence.

    THE TRAILING ``s?`` IS NOT COSMETIC. The N=5 replay's second spine wrote
    *"their verification scores are lower"* and the class did not fire, because
    the lexicon holds the singular and ``(?!\\w)`` refuses the plural. Listing
    both forms of every entry doubles a lexicon that has to stay readable, so
    the plural lives in the pattern. It cannot widen the match to a different
    word: ``s?`` matches a literal ``s`` or nothing, and the boundary still has
    to hold after it, so "mass" reaches "masses" no more than it did before.
    """
    cached = _FLEX_CACHE.get(term)
    if cached is None:
        parts = [re.escape(p) for p in re.split(r"[-\s]+", term) if p]
        cached = re.compile(
            rf"(?<!\w){_FLEX_SEP.join(parts)}s?(?!\w)", re.IGNORECASE
        )
        _FLEX_CACHE[term] = cached
    return cached


def _find_flex(sentence: str, term: str) -> int:
    """Where ``term`` starts in ``sentence`` (RAW index), or ``-1``."""
    match = _flex_re(term).search(sentence)
    return -1 if match is None else match.start()


def _flex_in(text: str, term: str) -> bool:
    """Is ``term`` present in ``text`` under the same tolerance? Used for the
    two exemptions, where only the ANSWER is needed and no offset is taken."""
    return _flex_re(term).search(str(text or "")) is not None


def _clause(sentence: str, start: int) -> tuple[int, int]:
    """``(start, end)`` of the clause beginning at ``start`` inside ``sentence``.

    The scan runs over the REF-MASKED sentence so a marker's own colon cannot
    end the clause, and the end is walked back off trailing punctuation, a
    trailing ``[[ref:N]]`` and any partial word — a dotted underline that stops
    mid-token reads as a rendering bug rather than as a judgement.
    """
    masked = _mask_refs(sentence)
    tail = masked[start:]
    break_at = _CLAUSE_BREAK_RE.search(tail, 1)
    end = start + (break_at.start() if break_at else len(tail))
    hard_cap = min(start + MAX_MARK_CHARS, len(sentence))
    if end > hard_cap:
        # Cutting on the cap: retreat to the last word boundary inside it.
        cut = masked.rfind(" ", start, hard_cap)
        end = cut if cut > start else hard_cap
    end = min(end, len(sentence))
    # ``isspace()`` rather than a literal set: the core plane emits U+202F and
    # U+00A0 inside its prose, and a mark that ends on an invisible character
    # renders as an underline with a gap on the end of it.
    while end > start and (
        sentence[end - 1].isspace()
        or sentence[end - 1] in ".!?…,;:"
        or masked[end - 1] == _MASK_CHAR
    ):
        end -= 1
    return start, max(end, start + 1)


def _mark(
    body: str,
    sentence: Sentence,
    rel_start: int,
    rel_end: int,
    *,
    klass: str,
    detector: str,
    note: str,
) -> dict[str, Any]:
    abs_start = sentence.start + rel_start
    abs_end = sentence.start + rel_end
    return {
        "sentence_index": sentence.index,
        "char_start": utf16_offset(body, abs_start),
        "char_end": utf16_offset(body, abs_end),
        "text": body[abs_start:abs_end],
        "class": klass,
        "detector": detector,
        "note": note,
    }


def _rank_marks(
    body: str,
    sentence: Sentence,
    cited: Mapping[int, str],
    candidates: int,
    earned_ordinals: set[int],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    hit = _earliest_term(sentence.text, _RANK_TERMS)
    if hit is not None:
        at, term = hit
        # THE THIRD EXEMPTION: the record's OWN earned lead. When the
        # concentration test fired and this sentence crowns exactly the blocks
        # it crowned, the ranking is the record's arithmetic being relayed.
        # Marking it would put a "no spine block makes one" note next to a
        # number on the same page saying concentration WAS earned.
        if earned_ordinals and cited and set(cited) <= earned_ordinals:
            return out
        # THE EXEMPTION (§2.4): a ranking word the DESK itself used, inside a
        # span this sentence cites, is relayed rather than authored. The channel
        # may carry a desk's own superlative; it may not mint one.
        if any(_term_in(term, span) for span in cited.values()):
            return out
        # THE SECOND EXEMPTION, and it is not in the spec because the spec could
        # not see it until a voice wrote under this contract: a NEGATED ranking
        # word is a REFUSAL to rank — "neither outweighs the other", "no single
        # thread dominates" — which is the exact bottom line the prompt asks for
        # on a cycle whose lead test did not fire. Marking it would tax the
        # honest sentence and reward the crowned one, which is the failure this
        # whole program exists to reverse.
        if _NEGATOR_RE.search(sentence.text[max(0, at - 40):at]) is not None:
            return out
        start, end = _clause(sentence.text, at)
        ordinals = sorted(cited)
        # The spine's order is a fact this sentence is contradicting or
        # inventing, so it goes in the note: a carried block's RANK IS ITS
        # ORDINAL (§1.5.1 makes the order total and the selection its strict
        # prefix), out of the day's whole candidate pool.
        where = (
            f" The spine's own order puts the block this sentence cites at "
            f"rank {ordinals[0]} of {candidates} candidates."
            if ordinals and candidates
            else " This sentence cites no spine block at all."
        )
        out.append(_mark(
            body, sentence, start, end,
            klass=UNSUPPORTED_RANK,
            detector=DETECTOR_DETERMINISTIC,
            note=(
                "Cross-block ranking claim; no spine block makes one — every "
                "block is bounded to one target and one question." + where
            ),
        ))
    # ONE rank mark per sentence: a second hit in the same sentence is the same
    # claim said twice, and two underlines on one clause read as two findings.
    return out


def _superlative_marks(
    body: str,
    sentence: Sentence,
    cited: Mapping[int, str],
    scoped: set[int],
    cited_ordinals: Sequence[int],
) -> list[dict[str, Any]]:
    if not cited_ordinals or not any(o in scoped for o in cited_ordinals):
        return []
    out: list[dict[str, Any]] = []
    hit = _earliest_term(sentence.text, _UNBOUNDED_SCOPE_TERMS)
    if hit is not None:
        at, term = hit
        if any(_term_in(term, span) for span in cited.values()):
            return out
        start, end = _clause(sentence.text, at)
        out.append(_mark(
            body, sentence, start, end,
            klass=UNSUPPORTED_SUPERLATIVE,
            detector=DETECTOR_DETERMINISTIC,
            note=(
                f"Unbounded scope ({term!r}) over a claim whose cited "
                f"block(s) {', '.join(str(o) for o in cited_ordinals)} are "
                f"bounded to one target each. The spine carries no "
                f"world-denominator, so this widens the claim off its evidence."
            ),
        ))
    return out


def _causal_marks(
    body: str, sentence: Sentence, cited_ordinals: Sequence[int],
) -> list[dict[str, Any]]:
    if len(set(cited_ordinals)) < 2:
        return []
    out: list[dict[str, Any]] = []
    hit = _earliest_term(sentence.text, _CAUSAL_TERMS)
    if hit is not None:
        at, term = hit
        # A DENIED link is not a link: "treated as independent strands rather
        # than mutually reinforcing dynamics" is the record's own zero-tension
        # finding being relayed, and marking it would flag the honest sentence.
        if _NEGATOR_RE.search(sentence.text[max(0, at - 40):at]) is not None:
            return out
        start, end = _clause(sentence.text, at)
        out.append(_mark(
            body, sentence, start, end,
            klass=UNSUPPORTED_CAUSAL_LINK,
            detector=DETECTOR_DETERMINISTIC,
            note=(
                f"Inferential connective ({term!r}) between spine blocks "
                f"{', '.join(str(o) for o in sorted(set(cited_ordinals)))}. "
                f"Each block answers its own bounded question; the link "
                f"between them is this voice's, not the record's."
            ),
        ))
    return out


def _scope_widening_marks(
    body: str, sentence: Sentence, cited: Mapping[int, str],
) -> list[dict[str, Any]]:
    """The M-8 shape, detected by the module that already owns it.

    ``absence_scope_laundered`` fires only when ALL of its five conditions hold —
    the sentence is an absence claim under the shared grammar, it carries no
    collection denominator of its own, it cites a resolvable ordinal, a cited
    span carries a collection-SCOPED negative about the same subject, and no
    cited span already published it unscoped. That last one matters here: if the
    record already said it about the world, this channel laundered nothing and
    the defect is one floor down.
    """
    if has_collection_denominator_scope(sentence.text):
        return []
    why = absence_scope_laundered(sentence.text, dict(cited))
    if not why:
        return []
    # Mark from the NEGATION, not from the start of the sentence. The claim is
    # the denial and its missing qualifier; a preamble ("The record shows that…")
    # is not part of what was widened, and a mark that swallows a whole sentence
    # also swallows every other mark inside it at render time.
    at = _first_negation(sentence.text)
    start, end = _clause(sentence.text, at)
    return [_mark(
        body, sentence, start, end,
        klass=UNSUPPORTED_SCOPE_WIDENING,
        detector=DETECTOR_DETERMINISTIC,
        note=f"Collection-scoped negative republished as a world negative: {why}",
    )]


def _instrument_marks(
    body: str,
    sentence: Sentence,
    cited: Mapping[int, str],
    arithmetic: str,
) -> list[dict[str, Any]]:
    """THE 2026-09-05 CLASS: a sentence whose subject is the INSTRUMENT.

    The line this draws is the evidence map's own. P1 (``fa3d2d85``) put the
    record's ARITHMETIC BLOCK behind every ordinal's ``evidence_text``, so the
    counters the record publishes about itself are gradeable — and the live
    2026-09-06 body proves it, because "Six reads fell below the verification
    floor" graded *supported* in the same read whose per-block numbers did not.
    What stayed ungradeable is everything on a BLOCK's attribution line
    (``assembly_render._attribution``: cited mass, verify score, severity,
    produced-at) plus claims about what the tier CONSISTS OF or LACKS. Those are
    unsupported by construction, however accurately they were copied off the
    page, and the four ``soft_fail`` claims that dragged that read to 0.4286
    were all of them.

    TWO EXEMPTIONS, both the module's existing idiom rather than a new one:

      * THE ARITHMETIC EXEMPTION. If the record's own arithmetic block uses the
        term, the voice is relaying a counter it was HANDED and that counter is
        in its evidence map, so the claim is gradeable and this class has
        nothing to say about it. This is what keeps the honest aperture sentence
        — the one the body shape asks for in section 4 — off the marker surface.
      * THE RELAY EXEMPTION, as for ``rank`` and ``superlative``: a term the
        DESK itself used inside a span this sentence cites is carried, not
        minted.

    Every hit is tested, not just the earliest: a sentence that relays one
    counter and invents another is marked on the invented one. ONE mark per
    sentence — a second hit is the same claim said twice.
    """
    hits: list[tuple[int, str]] = []
    for term in _INSTRUMENT_TERMS:
        at = _find_flex(sentence.text, term)
        if at < 0:
            continue
        if _flex_in(arithmetic, term):
            continue
        if any(_flex_in(span, term) for span in cited.values()):
            continue
        hits.append((at, term))
    if not hits:
        return []
    at, term = min(hits, key=lambda h: (h[0], -len(h[1])))
    start, end = _clause(sentence.text, at)
    return [_mark(
        body, sentence, start, end,
        klass=UNSUPPORTED_INSTRUMENT_PROSE,
        detector=DETECTOR_DETERMINISTIC,
        note=(
            f"Instrument prose ({term!r}): this sentence's subject is the "
            f"record's own machinery, not the world. A block's attribution "
            f"line — cited mass, verify score, severity, produced-at — is not "
            f"in this channel's evidence map, and neither is a claim about "
            f"what the tier consists of or lacks, so the sentence cannot be "
            f"supported however accurately it was copied. The record's OWN "
            f"counters (reads carried, below the floor, candidates ranked, "
            f"pairs examined, top-share) ARE citable, in the arithmetic "
            f"block's words and with an ordinal."
        ),
    )]


_WORD_RE = re.compile(r"[a-z0-9]+")


def aperture_vocabulary(spine: Mapping[str, Any]) -> frozenset[str]:
    """Every word the RECORD itself puts on the table, folded and split.

    THE DENOMINATOR THE BLIND-SPOT SECTION IS FENCED TO, and it is assembled
    from exactly three places, all of them already on the spine row:

      * THE DECLARED APERTURE — ``coverage_roster``, the coverage ledger's own
        units, and the drop ledger's desks, targets and head TITLES
        (``declared_aperture`` renders precisely this set, so the fence and the
        prompt cannot drift: the voice is marked against the same bytes it was
        handed).
      * THE CARRIED BLOCKS' IDENTITY — desk, target, target name, question. A
        record that carried a read about a place has plainly seen the place.
      * THE BLOCKS' QUOTED SPANS. The relay idiom, stated generously on purpose:
        a name the desks themselves wrote is a name the record mentions, and
        marking it would tax a sentence for using the record's own words. It is
        why this class fires only on a name that appears NOWHERE — which is the
        narrow claim it is entitled to make.

    Split into WORDS rather than kept as phrases, and matched with ANY rather
    than ALL below, because a multi-word name whose head the record does carry
    ("the Strait of Hormuz shipping lane") is not a guess, and the expensive
    error here is a false mark on an honest sentence.
    """
    pool: list[str] = [declared_aperture(spine)]
    for block in spine.get("blocks") or []:
        pool.extend(
            str(block.get(key) or "")
            for key in ("desk", "target_id", "target_name", "question")
        )
        pool.extend(str(sp.get("text") or "") for sp in (block.get("spans") or []))
    for key in ("coverage_roster",):
        pool.extend(str(u) for u in (spine.get(key) or []))
    for entry in spine.get("coverage") or []:
        pool.append(str(entry.get("unit") or ""))
    return frozenset(_WORD_RE.findall(_folded("\n".join(pool))))


def aperture_unit_identifiers(spine: Mapping[str, Any]) -> frozenset[str]:
    """The record's own MACHINE IDENTIFIERS for the units it declared missing.

    THE N=5 REPLAY FOUND THIS ONE, and it was a false fire on the exact prose the
    train exists to produce. Arm B's 09-06 body wrote

        "The tier omitted reads on Russia (country_g20_ru), Britain
         (country_g20_gb), Haiti (country_watch_ht) … that were seen and ranked
         below the cut"

    — the voice reading the drop ledger and naming five real uncovered units, in
    the world's nouns, with the record's own handle beside each. The ledger
    carries the SLUG (``country_g20_ru``) and never the human name, so "Russia"
    resolved against nothing and :func:`_aperture_marks` marked the best aperture
    sentence in the replay.

    The fix is not to teach this module a gazetteer — that would be a second
    world model inside a fence whose whole point is that the record is the only
    one. It is to notice what the sentence DID: a blind-spot sentence carrying a
    declared unit's identifier has grounded itself in the ledger, and the proper
    nouns beside that identifier are its human reading. So a sentence naming any
    identifier below exempts every run inside it.

    Narrower than :func:`aperture_vocabulary` on purpose: an identifier is a
    handle a voice can only have got from the record, so this cannot be satisfied
    by ordinary prose the way a shared English word can.
    """
    out: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if len(text) > 3 and not text.isspace():
            out.add(_folded(text))

    drops = spine.get("drops") or {}
    for key in ("not_selected", "shown_not_carried", "trimmed", "below_floor"):
        for row in drops.get(key) or []:
            _add(row.get("desk"))
            _add(row.get("target_id"))
            _add(row.get("target_name"))
    for row in drops.get("no_head") or []:
        _add(row.get("unit"))
    for unit in spine.get("coverage_roster") or []:
        _add(unit)
    for entry in spine.get("coverage") or []:
        _add(entry.get("unit"))
    return frozenset(out)


def _aperture_marks(
    body: str,
    sentence: Sentence,
    vocabulary: frozenset[str],
    identifiers: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """THE 2026-09-06 CLASS: a blind spot the voice named and the record cannot.

    Runs ONLY inside ``## What this reading misses`` (see
    :data:`_APERTURE_HEADING` for why the section matters). One mark per
    sentence, on the FIRST unknown name, because a sentence that invents two
    blind spots has made one mistake twice.

    The offset is taken on the RAW sentence and the lookup on the FOLDED
    vocabulary — the module's standing rule, because the fold deletes a soft
    hyphen and an offset taken against a rewritten string does not index the
    string it claims to describe.
    """
    masked = _mask_refs(sentence.text)
    # THE LEDGER-GROUNDING EXEMPTION (see :func:`aperture_unit_identifiers`): a
    # sentence quoting a declared unit's own handle is reading the drop ledger,
    # and the proper nouns beside that handle are its human reading. Checked
    # first, once, over the whole sentence.
    if identifiers:
        folded_sentence = _folded(sentence.text)
        if any(
            re.search(rf"(?<!\w){re.escape(ident)}(?!\w)", folded_sentence)
            for ident in identifiers
        ):
            return []
    for match in _PROPER_RUN_RE.finditer(masked):
        run = match.group(0)
        words = _WORD_RE.findall(_folded(run))
        content = [w for w in words if w not in _APERTURE_STOPWORDS and len(w) > 2]
        if not content:
            continue
        if any(word in vocabulary for word in content):
            continue
        start, end = _clause(sentence.text, match.start())
        return [_mark(
            body, sentence, start, end,
            klass=UNSUPPORTED_APERTURE_UNROSTERED,
            detector=DETECTOR_DETERMINISTIC,
            note=(
                f"Aperture claim naming {run!r}, which appears NOWHERE in this "
                f"record — not in the coverage roster, not in the drop ledger "
                f"(the desks, targets and head titles of every read this "
                f"surface saw and did not carry), not in a carried block and "
                f"not in a quoted span. The blind-spot section is mandated and "
                f"the record hands you its DECLARED APERTURE by name; a unit "
                f"the record never declared is a guess about the world rather "
                f"than a reading of what this surface could not see."
            ),
        )]
    return []


#: WHAT SEPARATES A UNIT FROM A TOPIC in the record's own label. ``unit_label``
#: renders ``"G20 — Argentina (country_g20_ar)"`` — desk-or-frame, an em dash,
#: the human name, the handle. The voice writes the HUMAN HALF ("Argentina"),
#: so the tail after the separator is a unit name in its own right and has to
#: be admitted as one; without it the honest sentence that names twenty-six
#: countries in the world's nouns would be marked as naming none of them.
_UNIT_LABEL_SPLIT_RE = re.compile(r"\s+[\u2014\u2013-]\s+")


def aperture_declared_units(spine: Mapping[str, Any]) -> frozenset[str]:
    """The record's DECLARED APERTURE as a set of UNITS, folded.

    THE DENOMINATOR FOR ``aperture_guess``, and it is deliberately NOT
    :func:`aperture_vocabulary` (every word the record puts on the table) and
    NOT :func:`aperture_unit_identifiers` (which also carries the DESK, because
    a sentence quoting ``escalation`` has quoted the drop ledger's own handle
    and the proper nouns beside it are its human reading).

    A DESK IS EXCLUDED HERE, and that exclusion is the whole class. The live
    2026-09-25 12:35Z specimen generalised into *"additional economic-coercion
    or escalation dynamics"* — which is the DESK vocabulary, exactly. A desk is
    a QUESTION this surface asks; a unit is a PLACE it asks it about, and the
    26 things the record handed over by name are units. Admitting a bare desk
    as "a named unit" would exempt the one sentence this class exists for. A
    desk named WITH its target — the arithmetic block's own spelling,
    ``escalation on Watch — Sudan (country_watch_sd)`` — carries the target and
    passes here on the target, which is the right reason to pass.

    Three spellings of every unit are admitted, because the record prints all
    three and the voice may reuse any of them: the SLUG (``country_g20_ar``),
    the full LABEL (``G20 — Argentina (country_g20_ar)``) and the human NAME
    with and without its frame (``G20 — Argentina``, ``Argentina``). Carried
    blocks are in the set as well as dropped ones: a sentence anchored to a
    block the record DID carry is anchored to the record either way, and the
    expensive error in this class — as in its sibling — is a false mark on an
    honest sentence.
    """
    out: set[str] = set()
    names = payload_names(spine)

    def _add(value: Any) -> None:
        text = _folded(str(value or "").strip())
        if len(text) > 3 and not text.isspace():
            out.add(text)

    def _unit(slug: Any, name: Any = None) -> None:
        handle = str(slug or "").strip()
        human = str(name or names.get(handle) or "").strip()
        _add(handle)
        _add(unit_label(human, handle))
        _add(human)
        # The TAIL only. ``G20`` and ``Watch`` are FRAMES, not units — the
        # record groups by them and a sentence naming one has still named no
        # place — so the split keeps the last segment ("Argentina", "Sudan")
        # and drops the frame in front of the separator.
        _add(_UNIT_LABEL_SPLIT_RE.split(human)[-1])

    drops = spine.get("drops") or {}
    for key in ("not_selected", "shown_not_carried", "trimmed", "below_floor"):
        for row in drops.get(key) or []:
            _unit(row.get("target_id"), row.get("target_name"))
    for row in drops.get("no_head") or []:
        _unit(row.get("unit"))
    for unit in spine.get("coverage_roster") or []:
        _unit(unit)
    for entry in spine.get("coverage") or []:
        _unit(entry.get("unit"))
    for block in spine.get("blocks") or []:
        _unit(block.get("target_id"), block.get("target_name"))
    return frozenset(out)


def _aperture_absence_at(sentence: str) -> int:
    """Where this sentence ASSERTS AN ABSENCE (raw index), or ``-1``.

    The grammar is IMPORTED, never re-listed: ``absence_slice._ABSENCE_MARKERS``
    is the same calibrated set the floor exemption, the V3 route, V-B and
    ``desk_absence_sentences`` share, and a second copy here would drift from
    all four the first time one is tuned (:func:`_first_negation`'s own rule).
    What is not imported is that module's position helper, which indexes a
    FOLDED string — the offset has to be taken on the raw sentence.

    Narrower than :func:`_first_negation` by one word: a bare ``not`` opens
    far more sentences that assert something than sentences that deny one, and
    this class publishes a mark rather than choosing between two verdicts, so
    the generous reading belongs on the exemption side and not here.
    """
    from ..provenance.absence_slice import _ABSENCE_MARKERS

    at: list[int] = []
    hit = _earliest_term(sentence, tuple(_ABSENCE_MARKERS))
    if hit is not None:
        at.append(hit[0])
    opener = re.search(
        r"(?<![\w-])(?:no|none|nothing|neither)\s", sentence, re.IGNORECASE
    )
    if opener is not None:
        at.append(opener.start())
    return min(at) if at else -1


def _aperture_guess_marks(
    body: str, sentence: Sentence, units: frozenset[str],
) -> list[dict[str, Any]]:
    """THE 2026-09-26 CLASS: a blind spot asserted over a TOPIC CLASS.

    Runs in ``## What this reading misses`` only, and only where
    :func:`_aperture_marks` found nothing — the more specific diagnosis keeps
    the label (the severity chain's own ordering rule). A sentence that names a
    place the record never declared is an ``aperture_unrostered``; a sentence
    that names NO place at all, while denying a whole class of subject matter,
    is this one.

    THE PREDICATE IS THE SECTION'S OWN INSTRUCTION, INVERTED. The prompt says
    *NAME THOSE* and the record hands the names over; so an absence assertion
    inside this section that reaches for none of them has generalised past the
    aperture it was handed. There is no topic lexicon here and there could not
    honestly be one: the class is decided by what the sentence DOES NOT name,
    against the record's own unit set, which is the only world model this fence
    is allowed to hold.
    """
    at = _aperture_absence_at(sentence.text)
    if at < 0:
        return []
    folded = _folded(sentence.text)
    if any(
        re.search(rf"(?<!\w){re.escape(unit)}(?!\w)", folded) for unit in units
    ):
        return []
    start, end = _clause(sentence.text, at)
    bare = not ref_ordinals(sentence.text)
    return [_mark(
        body, sentence, start, end,
        klass=UNSUPPORTED_APERTURE_GUESS,
        detector=DETECTOR_DETERMINISTIC,
        note=(
            f"Blind-spot claim denying a CLASS OF SUBJECT MATTER while naming "
            f"none of the {len(units)} units this record declared as its "
            f"aperture. The record hands the blind-spot section its uncarried "
            f"units BY NAME — desk, target and head title — and the section's "
            f"instruction is to name them; a sentence that generalises past "
            f"them instead ('gaps in coverage of possible … dynamics') states "
            f"an absence whose truthmaker is nowhere in the record, and the "
            f"real uncovered units were sitting in the ledger unnamed."
            + (
                " This sentence also names no ordinal: the uncited class would "
                "mark it as a whole, and the more specific diagnosis keeps the "
                "label."
                if bare
                else ""
            )
        ),
    )]


#: A line that is ONLY a bold run — the headline the OUTPUT clause asks for,
#: or a label like ``**BLUF:**`` — carries no claim of its own and is exempt
#: from the uncited class. A bold run followed by prose is a sentence like any
#: other.
_BOLD_ONLY_RE = re.compile(r"^\s*(?:[-*]\s+)?\*\*[^*]+\*\*\s*[:.]?\s*$")


def _uncited_marks(
    body: str, sentence: Sentence, ordinals: Sequence[int],
) -> list[dict[str, Any]]:
    """H8 — the sentence names no ordinal, so nothing on the record warrants it.

    The whole sentence is the mark: there is no clause to isolate when the
    defect is the absence of a citation rather than a word inside one. A
    sentence with no letters (a bare bullet, a rule) is not a claim.
    """
    if ordinals:
        return []
    text = sentence.text
    if _BOLD_ONLY_RE.match(text) or not re.search(r"[A-Za-z]", text):
        return []
    return [_mark(
        body, sentence, 0, len(text),
        klass=UNSUPPORTED_UNCITED,
        detector=DETECTOR_DETERMINISTIC,
        note=(
            "No ordinal: unsupported by construction — every sentence names "
            "at least one [[ref:N]] it rests on (the ordinal fence)"
        ),
    )]


def _dedupe(marks: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Non-overlapping, ordered marks — plus how many overlaps were suppressed.

    The reader drops an overlapping mark silently (``markBody`` keeps the first),
    so the row must not carry a mark the render will not show. Resolving it HERE
    and publishing the count keeps the row and the page telling the same story —
    the same reason the coverage ledger is persisted rather than re-derived.
    """
    def _rank(mark: Mapping[str, Any]) -> tuple[int, int, int]:
        klass = str(mark["class"])
        # An unknown class sorts last rather than raising: a judge arm that
        # invents a sixth class must not be able to crash the producer.
        order = (
            UNSUPPORTED_CLASSES.index(klass)
            if klass in UNSUPPORTED_CLASSES
            else len(UNSUPPORTED_CLASSES)
        )
        return (
            int(mark["char_start"]),
            -(int(mark["char_end"]) - int(mark["char_start"])),
            order,
        )

    ordered = sorted(marks, key=_rank)
    kept: list[dict[str, Any]] = []
    suppressed = 0
    cursor = -1
    for mark in ordered:
        if int(mark["char_start"]) < cursor:
            suppressed += 1
            continue
        kept.append(dict(mark))
        cursor = int(mark["char_end"])
    return kept, suppressed


def find_unsupported(
    body: str,
    spine: Mapping[str, Any],
    *,
    judge_marks: Iterable[Mapping[str, Any]] = (),
    judge_ran: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(unsupported[], unsupported_checked)`` for one Assessment body.

    ``judge_marks`` is the seam for D-3's ``fact`` arm: marks arriving from the
    judge are merged into the same list with ``detector: "judge"`` and are
    de-overlapped against the deterministic ones. ``judge_ran`` is what the
    payload publishes — a caller that passes no marks because the arm does not
    exist yet is a different fact from a judge that ran and found nothing, and
    the row must be able to tell them apart.
    """
    body = str(body or "")
    sentences = segment_sentences(body)
    spans = spine_span_text(spine)
    scoped = _target_scoped_ordinals(spine)
    # THE INSTRUMENT CLASS'S EXEMPTION SET, rendered ONCE per body. It is the
    # same bytes the prompt hands the voice and the same bytes P1 appends to
    # every citation's ``evidence_text``, imported from the one function that
    # produces them rather than re-listed here — a second copy would drift from
    # the map the first time a counter is added, and the whole point of the
    # exemption is that it tracks what is gradeable.
    arithmetic = record_arithmetic(spine)
    lead = spine.get("lead") or {}
    candidates = int((lead.get("test") or {}).get("n_candidates") or 0) or len(
        spine.get("blocks") or []
    )
    # THE ONE RANKING THE RECORD DOES PERFORM. No BLOCK ranks anything — each is
    # bounded to one target and one question — but the ASSEMBLY runs the
    # earned-lead test over the whole candidate pool, and when it fires, the
    # record has itself said that one thread outweighs the others. A voice that
    # crowns exactly that thread is relaying the record's arithmetic, not
    # minting a comparison, and marking it would contradict a number printed on
    # the same page. Empty when the lead was not earned — under `co_leads` or
    # `none` a ranking claim is precisely what the record refused to make.
    earned_ordinals: set[int] = (
        {int(o) for o in (lead.get("block_ordinals") or [])}
        if str(lead.get("kind") or "") == "earned_single"
        and bool((lead.get("test") or {}).get("earned"))
        else set()
    )

    # THE APERTURE CLASS'S DENOMINATOR, built ONCE per body from the record's
    # own three surfaces (see :func:`aperture_vocabulary`). The section flag is
    # carried across sentences because ``segment_sentences`` emits a heading as
    # its own unit — which is exactly what makes the section boundary legible
    # here without re-parsing the markdown.
    vocabulary = aperture_vocabulary(spine)
    identifiers = aperture_unit_identifiers(spine)
    # o4: the UNIT set the section's ABSENCE sentences are fenced to — narrower
    # than the vocabulary and narrower than the identifiers, because a desk is
    # the topic class the guess generalises into (see aperture_declared_units).
    units = aperture_declared_units(spine)
    in_aperture = False

    found: list[dict[str, Any]] = []
    for sentence in sentences:
        if _HEADING_RE.match(sentence.text):
            in_aperture = _APERTURE_HEADING in _folded(sentence.text)
            continue
        ordinals = ref_ordinals(sentence.text)
        cited = {o: spans[o] for o in ordinals if o in spans}
        found.extend(
            _rank_marks(body, sentence, cited, candidates, earned_ordinals)
        )
        found.extend(_superlative_marks(body, sentence, cited, scoped, ordinals))
        found.extend(_causal_marks(body, sentence, ordinals))
        found.extend(_scope_widening_marks(body, sentence, cited))
        found.extend(_instrument_marks(body, sentence, cited, arithmetic))
        guess: list[dict[str, Any]] = []
        if in_aperture:
            # ORDERING IS A CONTRACT here, and it is the severity chain's:
            # the most specific available diagnosis wins the label. A sentence
            # naming a place the record never declared is ``aperture_unrostered``
            # and this class does not also fire on it — two underlines on one
            # clause read as two findings, and the dedupe would drop one of them
            # at render time anyway.
            unrostered = _aperture_marks(body, sentence, vocabulary, identifiers)
            found.extend(unrostered)
            if not unrostered:
                guess = _aperture_guess_marks(body, sentence, units)
                found.extend(guess)
        # H8 YIELDS TO THE SPECIFIC DIAGNOSIS. ``uncited`` marks the WHOLE
        # sentence, so a guess mark inside one is suppressed by the dedupe and
        # the row would publish a checked zero over the class that fired. The
        # generic reason ("whatever it says") is carried into the guess note
        # instead, so nothing the row knew is lost. Additive: a body that
        # produces no guess mark is byte-identical to v4.
        if not guess:
            found.extend(_uncited_marks(body, sentence, ordinals))

    for mark in judge_marks:
        entry = dict(mark)
        entry.setdefault("detector", DETECTOR_JUDGE)
        entry.setdefault("class", UNSUPPORTED_FACT)
        found.append(entry)

    kept, suppressed = _dedupe(found)
    by_class = {
        klass: sum(1 for m in kept if m["class"] == klass)
        for klass in UNSUPPORTED_CLASSES
    }
    checked = {
        "version": UNSUPPORTED_VERSION,
        "sentences_examined": sum(
            1 for s in sentences if not _HEADING_RE.match(s.text)
        ),
        "marks": len(kept),
        "by_class": by_class,
        "overlaps_suppressed": suppressed,
        "deterministic_classes": list(UNSUPPORTED_DETERMINISTIC_CLASSES),
        "judge_classes": list(UNSUPPORTED_JUDGE_CLASSES),
        # The honest half of a zero. "No fabricated fact was flagged" and "nobody
        # looked for one" are different sentences, and only one of them is true
        # before D-3.
        "judge_state": JUDGE_STATE_RAN if judge_ran else JUDGE_STATE_NOT_RUN,
        "judge_note": (
            "the fact class needs the judge arm (D-1 §3.5); it has not landed, "
            "so a claim two paraphrase hops from a span that denies it is NOT "
            "yet detected here"
        ) if not judge_ran else "",
        "spine_candidates": candidates,
        # G3: the SIZE of the aperture denominator. A row publishing zero
        # ``aperture_unrostered`` marks over a 4-word vocabulary and one over a
        # 900-word vocabulary are different facts, and the M-11 rule ("a
        # nothing-flagged line that does not say what it checked") applies to
        # this class exactly as it does to the others.
        "aperture_vocabulary_words": len(vocabulary),
        # o4: and the size of the UNIT denominator, for the same M-11 reason —
        # zero ``aperture_guess`` marks over a record that declared no units at
        # all is a different fact from zero over one that declared 26.
        "aperture_units_declared": len(units),
    }
    return kept, checked
