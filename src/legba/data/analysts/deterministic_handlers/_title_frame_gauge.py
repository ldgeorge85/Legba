# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The TITLE-FRAME GAUGE — deterministic headline-grammar metrics (P3).

The measurement instrument ``VOICE_ORGANIC_REVIEW_2026-09-01`` §5.2 asks for,
and the one it says must ship *before* any prompt change rather than after.

WHY IT EXISTS, IN ONE PARAGRAPH. On 2026-08-04 the composition tier acquired a
``<subject> <driving verb> <risk noun>`` headline lock that went from 0/60 world
reads before 2026-07-01 to 54/58 (93.1%) in the four weeks after, and **every
conventional diversity metric moved in the wrong direction while it happened**:
title entropy rose 6.02 → 6.42 bits, distinct-title rate reached 98.3%, mean
pairwise Jaccard *fell* 0.164 → 0.134 (review §1.2). The frame rotates its slots
— 24+ verbs, 27 subjects — while holding its GRAMMAR, so a vocabulary metric is
structurally blind to it. That is also why the VOICE program's own "template
echoes measured to zero" was true and useless: it was measured against a list of
banned PHRASES. **This module measures the grammar, and nothing here counts
words.**

WHAT IT MEASURES. Five counters, review §5.2, plus the counter-metric that keeps
the cure honest:

1. ``frame_rate`` — share of titles in the strict predicate frame (and
   ``frame_rate_core``, the conservative reading that drops the copular verbs,
   and ``crowned_rate``, the frame with only ONE subject in it — the split that
   separates the sentence SHAPE from the act of crowning).
2. ``crown_streak_max`` — the longest run of consecutive titles holding the same
   subject. Burkina Faso held it for 10 straight world reads.
3. ``crown_churn`` — the share of consecutive title pairs that CHANGE subject.
   The opposite failure, and today's world value is 67%: the read is
   simultaneously maximally repetitive in form and maximally unstable in
   content. A healthy read is neither, so both ends are reported.
4. ``dimension_coverage`` — how many of the eight desk dimensions the titles
   name across the window. The world tier's value is 3 of 8: five dimensions
   have never titled a world read.
5. ``concordance`` — the share of body prose paragraphs that mention the title's
   subject. The world tier's value is 0.458 against 17.2 named entities per
   body, which is the actual defect the review found: a single-driver headline
   sitting on a many-driver body.
6. ``roll_call_rate`` — the counter-metric. The predicate frame was itself
   installed as the cure for the pre-07-01 headline, which named NO entity at
   all in 60 of 60 world reads (``World situational assessment - 2026-06-16``).
   A "fix" that drives ``frame_rate`` down by walking back into that is not a
   fix, so the gauge measures both failures or it measures neither.

NO LLM, NO STAMP, NO REPAIR. Pure text over ``analyst_outputs.title`` /
``.body``. It COUNTS + NAMES, exactly like the sweep it rides in. Nothing here
reads or writes the verify plane: titles are producer-side output and every
number below is computed after the fact from rows that already exist.

THE CLASSIFIERS ARE PUBLISHED, NOT HIDDEN. Review §5.2 asks for the regex to
ship *with* the metric "so it can be argued with". Every lexicon in this module
is a module-level constant for that reason, and every one of them is a judgment
call that a later reader is invited to disagree with.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

#: Core-plane models emit full-width citation brackets, and the composition
#: tiers emit U+2011 non-breaking hyphens ("Asia‑Pacific") and curly quotes
#: ("Russia's") straight into titles. A gauge that tokenises before folding
#: these would score ``Asia‑Pacific`` and ``Asia-Pacific`` as different subjects
#: and break the crown streak on a typographic difference.
#:
#: LOCAL MIRROR, deliberately. The house keeps this regex duplicated per module
#: rather than importing it out of ``registry.export_api`` — see
#: ``_external_audit_sampling.py``'s note — because an analyst handler must not
#: drag a registry module into its import graph.
_VARIANT_CITATION_RE = re.compile(r"[【［〔〖](\s*\d+\s*)[】］〕〗]")

_DASHES = {
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
}
_QUOTES = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "“": '"',
    "”": '"',
}


def normalize(text: str) -> str:
    """NFKC + house folding: variant brackets, dashes and quotes to ASCII.

    NFKC first (it folds the full-width Latin the core plane sometimes emits),
    then the three explicit maps, because NFKC does NOT fold U+2011 or the curly
    quotes — the two that actually appear in live titles.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    out = _VARIANT_CITATION_RE.sub(lambda m: f"[{m.group(1).strip()}]", out)
    for src, dst in {**_DASHES, **_QUOTES}.items():
        out = out.replace(src, dst)
    return out


# ---------------------------------------------------------------------------
# The predicate frame
# ---------------------------------------------------------------------------

#: The driving-verb bases, as the review's §1 census published them. A title is
#: in the frame when one of these governs a risk noun.
_VERB_BASES: tuple[str, ...] = (
    "drive", "lift", "sustain", "eclipse", "dominate", "heighten", "raise",
    "push", "spike", "top", "keep", "fuel", "amplify", "outrank", "elevate",
    "remain", "escalate", "strain", "mount", "compound", "deepen", "stoke",
    "spark", "threaten", "intensify", "surge", "steady", "anchor", "face",
    "see", "lead", "stay", "persist",
)

#: The COPULAR tail of that list. Dropping it gives the conservative
#: "core-verb-only" reading, which is the number the review leans on where the
#: two disagree: the region tier reads 70.3% strict and 30.7% core, and the
#: honest statement is that the region tier is not the locked one.
_COPULAR_BASES: frozenset[str] = frozenset(
    {"face", "see", "stay", "lead", "anchor", "persist", "steady"}
)

#: Surface forms English inflection does not generate from the rules below.
_IRREGULAR_FORMS: Mapping[str, tuple[str, ...]] = {
    "drive": ("drive", "drives", "drove", "driven", "driving"),
    "keep": ("keep", "keeps", "kept", "keeping"),
    "see": ("see", "sees", "saw", "seen", "seeing"),
    "lead": ("lead", "leads", "led", "leading"),
    "top": ("top", "tops", "topped", "topping"),
    "fuel": ("fuel", "fuels", "fueled", "fuelled", "fueling", "fuelling"),
}


def _inflect(base: str) -> frozenset[str]:
    """The surface forms of one verb base — regular English rules + overrides."""
    if base in _IRREGULAR_FORMS:
        return frozenset(_IRREGULAR_FORMS[base])
    forms = {base}
    if base.endswith(("s", "x", "z", "ch", "sh")):
        forms.add(base + "es")
    elif base.endswith("y") and base[-2] not in "aeiou":
        forms.add(base[:-1] + "ies")
    else:
        forms.add(base + "s")
    if base.endswith("e"):
        forms |= {base + "d", base[:-1] + "ing"}
    elif base.endswith("y") and base[-2] not in "aeiou":
        forms |= {base[:-1] + "ied", base + "ing"}
    else:
        forms |= {base + "ed", base + "ing"}
    return frozenset(forms)


VERB_FORMS: Mapping[str, str] = {
    form: base for base in _VERB_BASES for form in _inflect(base)
}
CORE_VERB_FORMS: frozenset[str] = frozenset(
    form for form, base in VERB_FORMS.items() if base not in _COPULAR_BASES
)

#: The RISK OBJECT vocabulary. ``risk(s)`` subsumes the compound forms the
#: review lists separately (``escalation risk``, ``humanitarian risk``), so the
#: set is the HEADS of those phrases plus the bare ``escalation`` that carries
#: the ``global escalation`` variant on its own.
RISK_NOUNS: frozenset[str] = frozenset({
    "risk", "risks", "threat", "threats", "tension", "tensions",
    "instability", "volatility", "pressure", "pressures", "crisis", "crises",
    "driver", "drivers", "outlook", "landscape", "violence", "toll",
    "disruption", "disruptions", "escalation", "escalations", "unrest",
})

#: How far after the verb a risk object still counts as governed by it.
#: The review's own census window, kept so the numbers are comparable.
_FRAME_WINDOW_CHARS = 120

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def _tokens(text: str) -> list[tuple[str, int]]:
    """``(token, start_offset)`` pairs over normalised text."""
    return [(m.group(0), m.start()) for m in _TOKEN_RE.finditer(text)]


class FrameVerdict:
    """One title's frame reading — the strict and conservative calls together."""

    __slots__ = ("strict", "core", "verb", "risk_noun", "subject")

    def __init__(
        self,
        *,
        strict: bool,
        core: bool,
        verb: str | None,
        risk_noun: str | None,
        subject: str,
    ) -> None:
        self.strict = strict
        self.core = core
        self.verb = verb
        self.risk_noun = risk_noun
        self.subject = subject

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"FrameVerdict(strict={self.strict}, core={self.core}, "
            f"verb={self.verb!r}, risk_noun={self.risk_noun!r})"
        )


def classify_frame(title: str) -> FrameVerdict:
    """Is ``title`` in the ``<subject> <driving verb> <risk noun>`` frame?

    The review's definition, implemented literally: scanning all driving verbs,
    take the LAST one that has at least one token before it and is followed
    within :data:`_FRAME_WINDOW_CHARS` by a risk object. "Last" matters — a
    title like *"Sudan offensive widens as Hormuz traffic halts, raising oil
    risk"* has its frame at the end, not the start.

    ``subject`` is the text before that verb (review §1.3's method), falling
    back to the first six tokens when no frame is present, so an un-framed title
    still gets a subject for the streak and concordance metrics.
    """
    text = normalize(title).strip()
    toks = _tokens(text)
    lowered = text.lower()

    best: tuple[int, str, str] | None = None  # (token index, form, risk noun)
    for idx, (tok, start) in enumerate(toks):
        if idx == 0:
            continue  # the frame needs a subject in front of its verb
        form = tok.lower()
        if form not in VERB_FORMS:
            continue
        window = lowered[start + len(tok) : start + len(tok) + _FRAME_WINDOW_CHARS]
        hit = next((n for n, _ in _tokens(window) if n.lower() in RISK_NOUNS), None)
        if hit is not None:
            best = (idx, form, hit.lower())

    if best is None:
        subject = " ".join(t for t, _ in toks[:6])
        return FrameVerdict(
            strict=False, core=False, verb=None, risk_noun=None, subject=subject
        )

    idx, form, noun = best
    subject = " ".join(t for t, _ in toks[:idx])
    return FrameVerdict(
        strict=True,
        core=form in CORE_VERB_FORMS,
        verb=form,
        risk_noun=noun,
        subject=subject,
    )


# ---------------------------------------------------------------------------
# The roll-call counter-metric
# ---------------------------------------------------------------------------

#: Capitalised words that are NOT named entities. The roll-call test below is
#: "does this headline name anybody", so the whole question is which capitals
#: count — and these are the ones that do not: function words, the severity and
#: risk vocabulary the tier reaches for, the masthead nouns of the pre-07-01
#: era, and the scope words a universal headline is built out of.
GENERIC_CAPS: frozenset[str] = frozenset({
    # scope + quantity
    "global", "globally", "world", "world's", "worldwide", "international",
    "regional", "region", "regions", "multiple", "several", "many", "other",
    "others", "across", "amid", "amidst", "while", "and", "or", "as", "the",
    "a", "an", "of", "in", "on", "at", "to", "for", "with", "from", "by",
    "two", "three", "four", "five", "both", "all", "some", "no", "none",
    "new", "near", "near-term", "this", "that", "these", "those", "its",
    "their", "week", "day", "cycle", "window", "today", "daily", "morning",
    # masthead / apparatus nouns — the E0 headline vocabulary
    "assessment", "assessments", "update", "updates", "situational",
    "situation", "report", "overview", "briefing", "brief", "digest",
    "summary", "snapshot", "roundup", "round-up", "synthesis", "analyst",
    "analysts", "read", "reads", "picture", "target", "utc",
    # severity / risk vocabulary
    "escalation", "escalations", "escalating", "escalate", "escalates",
    "risk", "risks", "rising", "elevated", "heightened", "severe", "severity",
    "tension", "tensions", "instability", "volatility", "pressure",
    "pressures", "crisis", "crises", "conflict", "conflicts", "violence",
    "threat", "threats", "unrest", "disruption", "disruptions", "driver",
    "drivers", "outlook", "landscape", "toll", "stability", "security",
    "humanitarian", "signals", "mixed", "stays", "remains", "continues",
    "kinetic", "triggers", "trigger", "flashpoints", "flashpoint", "theatres",
    "theaters", "theatre", "theater", "energy", "posture", "vector", "vectors",
})

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def entity_tokens(title: str) -> list[str]:
    """The capitalised, non-generic tokens of ``title`` — its named subjects.

    Deliberately crude and deliberately published. It is the single feature that
    separates the two eras cleanly in the review's own census — pre-07-01 world
    titles carry NO named entity in 60 of 60, post-VOICE ones in 8 of 58 — so a
    capitalisation test with a stoplist is enough to police the floor this train
    must not fall through, and does not pretend to be entity resolution.
    """
    text = normalize(title)
    out: list[str] = []
    for tok, _ in _tokens(text):
        if not tok[:1].isupper():
            continue
        bare = tok.lower().rstrip("'s").rstrip("'")
        if bare in GENERIC_CAPS or tok.lower() in GENERIC_CAPS:
            continue
        # A DRIVING VERB IS NOT AN ENTITY, wherever it is capitalised — the
        # sentence-initial participle ("Escalating conflicts …", "Intensifying
        # proxy attacks …") and every Title Case headline. Keyed on
        # :data:`VERB_FORMS` rather than on a second hand-listed stoplist so the
        # two vocabularies cannot drift apart, and so this covers inflections
        # nobody thought to enumerate.
        if bare in VERB_FORMS:
            continue
        # A HYPHENATED COMPOUND OF GENERICS IS GENERIC. Found by the
        # TITLE-FRAME-FIX replay, which produced "Energy-security pressures and
        # kinetic triggers heighten global escalation risk" — a headline that
        # names nobody and scored as though it named somebody, because
        # "Energy-security" is one token and is not in the stoplist while both
        # of its parts are. Without this the roll-call counter has a blind spot
        # shaped exactly like the abstraction an un-crowned headline reaches for.
        parts = [p for p in bare.split("-") if p]
        if parts and all(p in GENERIC_CAPS for p in parts):
            continue
        if len(bare) < 2:
            continue
        out.append(tok)
    return out


def is_crowned(title: str) -> bool:
    """The frame with ONE subject in it — the defect at full strength.

    The review's frame definition is about GRAMMAR and says nothing about how
    many subjects fill the slot, so ``Myanmar instability and Ukraine drone
    surge drive heightened global escalation risk`` scores as frame — correctly,
    it IS the grammar — while being a materially different headline from
    ``Myanmar air strikes drive global escalation risk``. One crowns; the other
    does not.

    This splits them, and it is the sharper reading of what §1.3 actually
    measured: 27 distinct primary subjects across 58 reads, a 10-run Burkina
    Faso streak, and a body naming 17.2 entities under a headline naming one.
    :func:`classify_frame`'s ``frame_rate`` stays the headline number because it
    is the one comparable with the published 93.1%; this is the one to read when
    asking whether a change removed the crown or only the sentence shape.
    """
    verdict = classify_frame(title)
    return verdict.strict and len(entity_tokens(verdict.subject)) <= 1


def is_roll_call(title: str) -> bool:
    """Does ``title`` name nobody — the pre-07-01 failure the frame was curing?

    TRUE for ``World situational assessment - 2026-06-16`` (a masthead and a
    date), TRUE for ``Global escalation risk stays elevated across multiple
    regions`` (a universal true on any day), FALSE for anything that names a
    concrete subject. This is the regression guard on the OTHER side of the
    frame fix: any change that drives ``frame_rate`` down while driving this up
    has traded one degenerate headline for the one that came before it.
    """
    return not entity_tokens(title)


#: The apparatus nouns an E0 headline was BUILT from. A subset of
#: :data:`GENERIC_CAPS`, named separately because the masthead sub-class needs
#: one of them PRESENT while the roll-call class only needs entities absent.
MASTHEAD_NOUNS: frozenset[str] = frozenset({
    "assessment", "assessments", "update", "updates", "situational",
    "situation", "report", "overview", "briefing", "brief", "digest",
    "summary", "snapshot", "roundup", "round-up", "synthesis", "read", "reads",
})


def is_masthead(title: str) -> bool:
    """The strict sub-class: an apparatus LABEL that asserts nothing.

    ``World situational assessment - 2026-06-16`` is a masthead and a date, and
    it is the form that is unambiguously dead. The empty universal (*"Global
    escalation risk stays elevated across multiple regions"*) names nobody
    either — so it is roll-call — but it does make a claim, so it is NOT this.
    Keeping the two apart matters because they have different cures: the
    masthead needs a subject, the universal needs a specific.

    Hence the two conditions: an apparatus noun is PRESENT, and no driving verb
    is — a label with a verb in it has stopped being a label.
    """
    if entity_tokens(title):
        return False
    stripped = _YEAR_RE.sub("", normalize(title))
    words = [t.lower() for t, _ in _tokens(stripped)]
    if not any(w in MASTHEAD_NOUNS for w in words):
        return False
    return not any(w in VERB_FORMS for w in words)


# ---------------------------------------------------------------------------
# Dimension coverage
# ---------------------------------------------------------------------------

#: The eight bounded-unit desk dimensions, each as the vocabulary a title uses
#: when it is ABOUT that dimension. Terms are matched as substrings of the
#: lower-cased title, so stems ("escalat") catch their own inflections.
#:
#: Overlap is intentional and harmless: "missile" belongs to both the escalation
#: and military-posture desks in real life, and the metric counts DISTINCT
#: dimensions touched rather than partitioning titles between them.
DIMENSION_TERMS: Mapping[str, tuple[str, ...]] = {
    "escalation": (
        "escalat", "strike", "offensive", "clash", "shelling", "missile",
        "airstrike", "air strike", "war", "militant", "insurgen", "attack",
        "bombard", "ceasefire", "front line", "frontline",
    ),
    "energy_security": (
        "oil", "gas", "energy", "chokepoint", "refinery", "pipeline", "lng",
        "crude", "barrel", "tanker", "strait", "blockade", "power grid",
        "electricity", "fuel supply",
    ),
    "leadership_transition": (
        "succession", "election", "leadership", "transition", "president",
        "prime minister", "resign", "cabinet", "handover", "inaugurat",
        "impeach", "term limit", "junta leader",
    ),
    "military_posture": (
        "deployment", "deploy", "mobiliz", "mobilis", "exercise", "drill",
        "procurement", "basing", "buildup", "build-up", "posture", "troop",
        "naval", "fleet", "brigade", "garrison", "rearm",
    ),
    "internal_stability": (
        "protest", "unrest", "riot", "crackdown", "repression", "coup",
        "gang", "civil disorder", "curfew", "detention", "purge",
        "state of emergency",
    ),
    "economic_coercion": (
        "sanction", "tariff", "embargo", "export control", "blacklist",
        "asset freeze", "coercion", "boycott", "trade ban", "import ban",
    ),
    "narrative_coordination": (
        "disinformation", "propaganda", "narrative", "influence operation",
        "bot network", "coordinated messaging", "information campaign",
    ),
    "proliferation_watch": (
        "nuclear", "enrichment", "uranium", "warhead", "iaea", "safeguards",
        "centrifuge", "proliferation", "weapons-grade", "fissile",
    ),
}

DIMENSIONS: tuple[str, ...] = tuple(DIMENSION_TERMS)


def title_dimensions(title: str) -> set[str]:
    """Which desk dimensions ``title`` names."""
    low = normalize(title).lower()
    return {d for d, terms in DIMENSION_TERMS.items() if any(t in low for t in terms)}


# ---------------------------------------------------------------------------
# Headline / body concordance
# ---------------------------------------------------------------------------

#: The review's paragraph filter: prose paragraphs of at least this many
#: characters, with the dateline and heading-only lines dropped.
_MIN_PARAGRAPH_CHARS = 25


def prose_paragraphs(body: str) -> list[str]:
    """The body's PROSE paragraphs — headings, datelines and stubs removed."""
    out: list[str] = []
    for block in normalize(body or "").split("\n\n"):
        lines = [
            ln
            for ln in block.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        if not lines:
            continue
        para = " ".join(ln.strip() for ln in lines).strip()
        # The as-of line is italic, alone on the first line, and starts with the
        # word the D1 rule puts there.
        if para.startswith("*") and para.lower().lstrip("* ").startswith("as of"):
            continue
        if len(para) < _MIN_PARAGRAPH_CHARS:
            continue
        out.append(para)
    return out


def concordance(title: str, body: str) -> float | None:
    """Share of body prose paragraphs that mention the title's subject.

    ``None`` when the question is not askable — no prose paragraphs, or a title
    whose subject names nobody (there is nothing to look for, and scoring that
    0.0 would blame the body for the headline's emptiness).

    Review §1.3's number, and the one that names the actual defect: the world
    tier sits at 0.458 while its bodies carry 17.2 distinct entities. Note that
    this metric is NOT one that should be driven to 1.0 — a body that discusses
    only its headline's subject is the collapse the review found the tier had
    ALREADY escaped. It is a concordance check, not a target.
    """
    subject = classify_frame(title).subject
    terms = [t.lower().rstrip("'s").rstrip("'") for t in entity_tokens(subject)]
    if not terms:
        return None
    paras = prose_paragraphs(body)
    if not paras:
        return None
    hits = sum(1 for p in paras if any(t in p.lower() for t in terms))
    return hits / len(paras)


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------


def subject_key(title: str) -> tuple[str, ...]:
    """The comparable identity of a title's crowned subject.

    Entity tokens where there are any (so ``Myanmar air strikes drive …`` and
    ``Myanmar airstrike campaign drives …`` are ONE crown, which is the whole
    point of a streak metric), else the normalised first six tokens.
    """
    frame = classify_frame(title)
    ents = tuple(sorted(t.lower().rstrip("'s").rstrip("'") for t in entity_tokens(frame.subject)))
    if ents:
        return ents
    return tuple(w.lower() for w, _ in _tokens(normalize(title))[:6])


def streak_and_churn(titles: Sequence[str]) -> tuple[int, float | None]:
    """``(longest same-subject run, share of consecutive pairs that change)``.

    ``titles`` must be in RUN ORDER (oldest first or newest first — the two
    metrics are symmetric under reversal). Churn is ``None`` for fewer than two
    titles, because a single read has no consecutive pair and reporting 0.0
    would read as "perfectly stable".
    """
    if not titles:
        return 0, None
    keys = [subject_key(t) for t in titles]
    longest = run = 1
    changes = 0
    for prev, cur in zip(keys, keys[1:]):
        if cur == prev and cur:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
            changes += 1
    if len(keys) < 2:
        return longest, None
    return longest, changes / (len(keys) - 1)


# ---------------------------------------------------------------------------
# The per-analyst roll-up
# ---------------------------------------------------------------------------


def gauge_rows(rows: Iterable[Mapping[str, object]]) -> dict[str, dict[str, object]]:
    """Per-analyst gauge metrics over ``rows``.

    Each row needs ``analyst_id``, ``title`` and ``body``. Rows are expected in
    ``produced_at DESC`` order (the sweep's own idiom); the streak metrics are
    order-symmetric so this is not load-bearing, but the caller should not
    shuffle them.

    Ratios are ``None`` rather than 0.0 wherever the denominator is empty — a
    gauge that reports a confident zero for "no data" is exactly the failure
    mode §5.0 retires entropy for.
    """
    by_analyst: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        analyst = str(row.get("analyst_id") or "")
        title = row.get("title")
        if not analyst or not isinstance(title, str) or not title.strip():
            continue
        by_analyst.setdefault(analyst, []).append(row)

    out: dict[str, dict[str, object]] = {}
    for analyst, group in sorted(by_analyst.items()):
        titles = [str(r["title"]) for r in group]
        verdicts = [classify_frame(t) for t in titles]
        n = len(titles)
        dims: set[str] = set()
        for t in titles:
            dims |= title_dimensions(t)
        scores = [
            c
            for c in (
                concordance(str(r["title"]), str(r.get("body") or "")) for r in group
            )
            if c is not None
        ]
        longest, churn = streak_and_churn(titles)
        out[analyst] = {
            "n": n,
            "frame_rate": round(sum(v.strict for v in verdicts) / n, 4),
            "frame_rate_core": round(sum(v.core for v in verdicts) / n, 4),
            "crowned_rate": round(sum(is_crowned(t) for t in titles) / n, 4),
            "roll_call_rate": round(
                sum(is_roll_call(t) for t in titles) / n, 4
            ),
            "masthead_titles": sum(is_masthead(t) for t in titles),
            "crown_streak_max": longest,
            "crown_churn": None if churn is None else round(churn, 4),
            "dimension_coverage": len(dims),
            "dimensions_named": sorted(dims),
            "dimensions_total": len(DIMENSIONS),
            "concordance": (
                None if not scores else round(sum(scores) / len(scores), 4)
            ),
            "concordance_n": len(scores),
        }
    return out


__all__ = [
    "DIMENSIONS",
    "DIMENSION_TERMS",
    "GENERIC_CAPS",
    "RISK_NOUNS",
    "VERB_FORMS",
    "CORE_VERB_FORMS",
    "FrameVerdict",
    "classify_frame",
    "concordance",
    "entity_tokens",
    "gauge_rows",
    "MASTHEAD_NOUNS",
    "is_crowned",
    "is_masthead",
    "is_roll_call",
    "normalize",
    "prose_paragraphs",
    "streak_and_churn",
    "subject_key",
    "title_dimensions",
]
