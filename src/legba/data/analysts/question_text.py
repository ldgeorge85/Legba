# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CW-3 — DEICTIC OPEN QUESTIONS: detect them, and make them self-contained.

THE DEFECT
----------
An open question is written by an analyst that is *looking at* a finding. So
the thesis is written the way a person writes a note to themselves:

    "Is the framing of **the incident** being driven by an orchestrated
     campaign from Russian or Iranian state actors?"
    "Will the narrative extend to concrete policy actions ... in response to
     **the alleged Ukrainian attack**?"

Which incident. Which attack. The answer lived in the origin finding
(``derived_from``) and was never copied into the thesis, so from the moment the
row lands the question is a pronoun with no antecedent. Every downstream reader
— the vector plane, the entity plane, an 8B gate, a 120B confirm — is handed a
string that does not say what it is about.

K-4 round 3 measured the cost on 120 labeled rows off the live gated stream
(``planning/K4_LABELS_R3.csv``): deictic theses scored **0.133**, and the
worst single desk-class, ``narrative_coordination``, scored **0.071**. Those
rows are not near-misses that better weighting would fix; they are pairs where
the matcher could not have been right, because the thesis does not carry the
proposition. The two worst offenders in the round — "the incident" (3 rows,
0 correct) and "the alleged Ukrainian attack" (6 rows, 0 correct) — were both
about the SAME Caspian-vessel event, named in both origin findings.

THE FIX, IN TWO PLACES
----------------------
1. **At harvest** (the real one). :func:`inline_referents` folds the origin
   finding's title into the stored thesis, so the row is self-contained the
   moment it is written and every reader downstream — not just the matcher —
   gets a question that says what it is about. This is the fix; the guard
   below is the backstop.
2. **At match time** (the backstop). :func:`is_deictic` re-tests the STORED
   thesis. A question that is still deictic — every row written before this
   landed, plus anything a future writer produces without a resolvable origin
   — is skipped by ``claim_watch`` and counted, never matched blind. That
   mirrors the META_QUESTION_CLASSES precedent exactly: measured at 2/58, the
   meta classes were excluded rather than down-weighted, because a class the
   matcher structurally cannot get right is not a tuning problem.

WHY THE DETECTOR IS THIS NARROW
-------------------------------
A loose detector here is expensive: it silently mutes standing questions. So
it fires only on an ANAPHORIC noun phrase — a definite/demonstrative
determiner in front of a curated event-referent noun (or any noun under an
explicitly back-pointing modifier like "cited" / "aforementioned") — and ONLY
when that phrase carries no antecedent of its own.

"Antecedent of its own" is the whole conservatism. A capitalised modifier
inside the phrase names the thing ("the **Red Sea** explosion", "the
**Telegram** lawsuit") and the phrase is self-contained. A NATIONAL DEMONYM
does not ("the alleged **Ukrainian** attack" still does not say which attack),
so :func:`legba.data._entity_canon.is_demonym` is consulted rather than a bare
``istitle()`` test — the same curated map the fact plane already trusts for
exactly this "is this word a name or a nationality" question.

Measured against the 120 R3 rows this fires on 12 of them, of which **0 were
correct matches** — the five theses above — and on none of the 48 correct
matches. In particular it deliberately does NOT fire on "…given recent
transits reported?" (5 rows, 3 correct): "recent" is vague, not anaphoric, and
a detector that cannot tell those apart would cost real evidence.
"""
from __future__ import annotations

import re
from typing import Iterable

from .._entity_canon import is_demonym

__all__ = [
    "ANAPHORIC_MODIFIERS",
    "CONTEXT_JOINER",
    "DEICTIC_NOUNS",
    "MAX_CONTEXT_CHARS",
    "OFFICE_TITLES",
    "deictic_spans",
    "has_inlined_context",
    "inline_referents",
    "is_deictic",
    "named_offices",
    "named_referents",
    "ungrounded_office",
]


#: Nouns that name an EVENT/ARTIFACT rather than a participant. Under a
#: definite or demonstrative determiner these are back-references: "the
#: incident" is only meaningful if you already know which one. Curated — a
#: participant noun ("the government", "the military", "the party") is
#: deliberately absent, because a desk-scoped thesis names its participants by
#: role all the time and that is not a dangling reference. Lower-cased.
DEICTIC_NOUNS: frozenset[str] = frozenset({
    "incident", "attack", "attacks", "strike", "strikes", "raid", "raids",
    "explosion", "blast", "event", "episode", "clash", "clashes",
    "escalation", "crisis", "dispute", "affair", "development",
    "narrative", "narratives", "framing", "messaging", "campaign",
    "story", "coverage", "reporting", "rumour", "rumor",
    "report", "statement", "statements", "remark", "remarks",
    "announcement", "claim", "claims", "allegation", "allegations",
    "accusation", "accusations", "denial",
    # NB "ruling" is deliberately ABSENT even though a court ruling is a
    # textbook deictic: "the ruling party" is far commoner in this corpus and
    # a detector that cannot tell a participle from a noun would mute whole
    # desks. "verdict"/"decision"/"order" carry the concept unambiguously.
    "decision", "verdict", "order", "move", "measure",
    "investigation", "inquiry", "case", "lawsuit", "proceeding",
    "deal", "agreement", "proposal", "plan", "initiative",
    "meeting", "summit", "visit", "call", "talks",
    "protest", "protests", "demonstration", "demonstrations",
    "outage", "disruption", "shortage", "breach", "leak",
})

#: Modifiers that are THEMSELVES back-pointers: they only mean anything
#: relative to text the reader is assumed to have. Under one of these ANY noun
#: is a dangling reference ("the cited coalition strain"), so the curated noun
#: set is bypassed. Kept tiny and unambiguous — "announced" and "reported" are
#: NOT here: "the announced repatriation of 5,000 Rohingya refugees" is a
#: perfectly self-contained thesis and scored a correct match in R3.
ANAPHORIC_MODIFIERS: frozenset[str] = frozenset({
    "cited", "aforementioned", "abovementioned", "above-mentioned",
    "said", "aforesaid", "foregoing", "latter", "former",
})

#: Determiners that make a noun phrase definite/demonstrative. An INDEFINITE
#: phrase ("an incident", "a narrative") is a generic, not a back-reference,
#: and is never flagged.
_DETERMINERS: frozenset[str] = frozenset({"the", "this", "that", "these", "those"})

#: TEMPORAL modifiers, which make a phrase VAGUE rather than ANAPHORIC. "the
#: recent attacks" points at a time window, not at a specific antecedent
#: sitting in some other document — a reader with no prior context can still
#: tell what class of thing is meant and when. R3 pins the distinction: "…the
#: recent attacks on actual vessel transit volumes" and "…given recent
#: transits reported" both scored correct matches, while "the incident" and
#: "the alleged Ukrainian attack" scored 0/9. A phrase carrying one of these
#: is exempt unless an ANAPHORIC modifier overrides it.
_TEMPORAL_MODIFIERS: frozenset[str] = frozenset({
    "recent", "latest", "current", "ongoing", "continuing", "continued",
    "renewed", "new", "upcoming", "planned", "expected", "further",
    "additional", "repeated", "persistent", "prolonged", "rising",
})

#: Up to this many modifier words may sit between the determiner and the noun
#: ("the alleged Ukrainian attack" = 2). Beyond that the phrase is long enough
#: to be carrying its own content.
_MAX_MODIFIERS = 3

#: Prepositions that introduce a NAMING complement. English puts an antecedent
#: on either side of the head noun — "the **Red Sea** explosion" before it,
#: "the strike **on the Jordan base**" after it — and a detector that only
#: looks left would flag the second, which is fully self-contained.
_COMPLEMENT_PREPS: frozenset[str] = frozenset({
    "of", "on", "in", "at", "against", "by", "between", "over", "near",
    "from", "to", "with", "around", "targeting", "involving", "aboard",
})

#: How far past the head noun the complement scan looks. Short: "the strike on
#: the Jordan base" needs three, and a longer window starts collecting the
#: names of unrelated later clauses.
_COMPLEMENT_WINDOW = 4

#: A word-ish token: letters first, then word chars / apostrophes / any
#: hyphen. The UNICODE hyphens matter — LLM-written theses are full of
#: U+2011 ("no‑confidence", "pre‑emptive", "behind‑the‑scenes"), and splitting
#: on them manufactures phantom noun phrases: "behind‑the‑scenes narrative"
#: tokenised naively yields a determiner "the" followed by "scenes narrative",
#: which is a detector firing on its own tokenizer rather than on the text.
_WORD_RE = re.compile(r"[A-Za-z][\w'’\-‐-―]*")

#: How the origin finding's title is folded into a thesis. A visible, greppable
#: joiner rather than a silent rewrite: the stored thesis must still read as
#: the analyst's question, with the referent APPENDED, so a human reading the
#: row can see what was resolved and what was asked.
CONTEXT_JOINER = " (in reference to: "

#: The inlined referent is a title, not a body.
MAX_CONTEXT_CHARS = 240


def _is_name(word: str) -> bool:
    """A capitalised token that is not a national demonym.

    "the **Red Sea** explosion" names its referent; "the alleged **Ukrainian**
    attack" does not — a nationality is not an antecedent, and treating one as
    such is precisely how a narrow detector turns into no detector at all.
    :func:`legba.data._entity_canon.is_demonym` is the curated map the fact
    plane already trusts for this exact question.
    """
    token = str(word or "").strip(" \t'’-‐‑‒–—")
    return bool(token) and token[:1].isupper() and not is_demonym(token)


def _phrase_has_antecedent(mods: Iterable[str], following: Iterable[str]) -> bool:
    """Does this noun phrase name its own referent, on either side?

    Before the head noun ("the **Red Sea** explosion") or in a prepositional
    complement after it ("the strike **on the Jordan base**"). Both are
    ordinary English; a left-only scan would flag the second, which says
    exactly what it is about.
    """
    if any(_is_name(w) for w in mods):
        return True
    tail = list(following)[:_COMPLEMENT_WINDOW]
    if not tail or tail[0].lower() not in _COMPLEMENT_PREPS:
        return False
    return any(_is_name(w) for w in tail[1:])


def deictic_spans(thesis: str) -> list[str]:
    """Every dangling back-reference in ``thesis``, in order of appearance.

    Empty means the thesis stands on its own. The spans are returned (rather
    than a bare bool) so a counter, a log line or an operator can see WHAT was
    unresolved — "this question was skipped" is not a useful thing to be told
    without it.
    """
    text = " ".join(str(thesis or "").split())
    if not text:
        return []
    words = _WORD_RE.findall(text)
    found: list[str] = []
    for i, word in enumerate(words):
        if word.lower() not in _DETERMINERS:
            continue
        # EVERY admissible noun position is tested, shortest phrase first —
        # "the narrative pre-emptive political signaling" must be read as
        # "the narrative", not as a four-word phrase ending in "signaling".
        # A single greedy regex silently reads only the longest, which is how
        # a detector ends up looking correct on the rows it happens to catch.
        for span in range(1, _MAX_MODIFIERS + 2):
            if i + span >= len(words):
                break
            mods = words[i + 1 : i + span]
            noun = words[i + span].lower()
            anaphoric = any(w.lower() in ANAPHORIC_MODIFIERS for w in mods)
            if noun not in DEICTIC_NOUNS and not anaphoric:
                continue
            # An explicit back-pointer ("the cited X") is dangling by
            # construction — the modifier ITSELF says the referent is
            # elsewhere — so neither a name nor a date rescues it.
            if not anaphoric:
                if any(w.lower() in _TEMPORAL_MODIFIERS for w in mods):
                    continue
                if _phrase_has_antecedent(mods, words[i + span + 1 :]):
                    continue
            found.append(" ".join(words[i : i + span + 1]))
            break
    return found


def is_deictic(thesis: str) -> bool:
    """True when the thesis leans on a referent it does not carry."""
    return bool(deictic_spans(thesis))


def has_inlined_context(thesis: str) -> bool:
    """True when :func:`inline_referents` already folded a referent in.

    Idempotency guard: harvest paths run repeatedly over the same source, and
    a thesis must never accumulate the same context clause twice.
    """
    return CONTEXT_JOINER.strip() in str(thesis or "")


def inline_referents(thesis: str, context: str) -> str:
    """The thesis, made self-contained against ``context`` (the origin
    finding's title).

    Returns the thesis UNCHANGED when there is nothing to resolve, when there
    is no context to resolve it against, or when a referent was already
    inlined. It never rewrites the analyst's words — the referent is appended
    in a visible clause, so the row reads as "what was asked" plus "what it was
    asked about" rather than as a sentence somebody's regex edited.

    The caller decides WHERE the context comes from; this function only knows
    that a deictic thesis plus a title is better than a deictic thesis.
    """
    text = " ".join(str(thesis or "").split())
    ctx = " ".join(str(context or "").split())[:MAX_CONTEXT_CHARS].rstrip(" .;:,")
    if not text or not ctx or has_inlined_context(text):
        return text
    if not is_deictic(text):
        return text
    # Nothing is gained by pointing a thesis at itself.
    if ctx.casefold() in text.casefold():
        return text
    return f"{text}{CONTEXT_JOINER}{ctx})"


# ---------------------------------------------------------------------------
# CW-8 — the OFFICE-WITH-NO-WORLD guard
# ---------------------------------------------------------------------------
#
# K-4 R3 carried a question about the loyalty of Iran's military "to the
# Supreme Leader versus the Prime Minister". Iran abolished the premiership in
# 1989. Two rows, both wrong, and the interesting part is WHY the error was
# invisible: the thesis names two OFFICES and no country, no person, no
# institution — nothing a reader or a matcher could have checked the offices
# against. An office is not a referent. It is a slot, and a slot is only
# meaningful once something says whose.
#
# WHAT THIS GUARD DOES NOT DO, stated plainly. It does not decide whether a
# country HAS a given office; that is not derivable from this substrate and
# pretending otherwise would be worse than not trying. The facts plane carries
# `head of state` / `head of government` per country, but with enough
# extraction noise ("United Kingdom head of state: Keir Starmer", "France head
# of state: Sébastien Lecornu") that "the UK has no prime minister" comes out
# exactly as derivable as "Iran has none", and entity_profiles attests no
# office-to-country binding at all (four "Prime Minister's Office" org rows in
# the whole plane, none country-bound). So the guard catches the CLASS the
# Iran row belongs to — an office question with nothing to bind the office to
# — which is both what makes it wrong and what made it uncheckable.

#: Office / role titles that name a SLOT rather than a referent. Matched as
#: whole phrases, case-insensitively, so "the prime minister" and "PM" both
#: count and "Prime Minister Shehbaz Sharif" is a person (the name is a
#: referent and rescues the thesis on its own).
OFFICE_TITLES: frozenset[str] = frozenset({
    "prime minister", "premier", "pm", "president", "vice president",
    "supreme leader", "chancellor", "taoiseach", "chief minister",
    "head of state", "head of government", "monarch", "king", "queen",
    "emir", "sultan", "crown prince", "regent",
    "foreign minister", "defence minister", "defense minister",
    "interior minister", "finance minister", "energy minister",
    "attorney general", "chief of staff", "chief of the general staff",
    "commander in chief", "commander-in-chief", "central bank governor",
    "speaker", "deputy prime minister", "first minister",
    "the military", "the government", "the ruling party", "the cabinet",
    "security forces", "the civilian leadership", "the armed forces",
})

#: Sentence-initial words that are capitalised only by position and never
#: name anything. Cheaper and more honest than a POS tagger for the one job
#: this needs: "Will Iran ..." must not read "Will" as a referent.
_SENTENCE_INITIAL: frozenset[str] = frozenset({
    "will", "would", "can", "could", "should", "does", "do", "did", "is",
    "are", "was", "were", "has", "have", "had", "what", "when", "where",
    "which", "who", "whom", "whose", "why", "how", "if", "the", "a", "an",
    "and", "or", "but", "in", "on", "at", "to", "for", "given", "may",
    "might", "must", "shall", "there", "this", "that", "these", "those",
})

#: A run of capitalised word-ish tokens — the cheap proper-noun proxy. Runs
#: may include internal lowercase connectors ("Strait of Hormuz") so a real
#: multi-word name is not split into fragments.
_CAP_RUN_RE = re.compile(
    r"\b[A-Z][\w'’\-‐-―\.]*(?:\s+(?:of|the|and|for|de|del|al|bin)\s+"
    r"[A-Z][\w'’\-‐-―\.]*|\s+[A-Z][\w'’\-‐-―\.]*)*"
)


def named_offices(thesis: str) -> list[str]:
    """Every office/role title the thesis names, lower-cased, in order."""
    text = " ".join(str(thesis or "").split()).lower()
    if not text:
        return []
    found: list[str] = []
    for title in OFFICE_TITLES:
        if re.search(rf"(?<!\w){re.escape(title)}(?!\w)", text):
            found.append(title)
    return sorted(found)


def named_referents(thesis: str) -> list[str]:
    """The proper-noun-ish things the thesis actually names.

    Office titles are EXCLUDED by construction — that is the whole point.
    Sentence-initial function words are dropped, and a run that reduces to
    nothing but an office title contributes nothing.
    """
    text = " ".join(str(thesis or "").split())
    if not text:
        return []
    out: list[str] = []
    for m in _CAP_RUN_RE.finditer(text):
        run = m.group(0).strip(" .,;:!?")
        low = run.lower()
        if not run or low in _SENTENCE_INITIAL or low in OFFICE_TITLES:
            continue
        # Strip a leading sentence-initial word ("Will Iran ..." -> "Iran").
        words = run.split()
        while words and words[0].lower() in _SENTENCE_INITIAL:
            words.pop(0)
        if not words:
            continue
        run = " ".join(words)
        if run.lower() in OFFICE_TITLES:
            continue
        # A HYPHENATED compound can smuggle an office in as if it were a name:
        # "PM-military tension" reads as a capitalised run, and the Iran row
        # that started this guard is grounded by nothing else. Split, drop the
        # office segments, and require what survives to still be capitalised —
        # "Iran-Iraq" and "Trump-brokered" keep their name, "PM-military"
        # keeps nothing.
        parts = [
            p.strip() for p in re.split(r"[-‐-―]", run) if p.strip()
        ]
        parts = [p for p in parts if p.lower() not in OFFICE_TITLES]
        if not any(p[:1].isupper() for p in parts):
            continue
        out.append(run)
    return out


def ungrounded_office(thesis: str) -> list[str]:
    """The offices a thesis asks about with NOTHING to bind them to.

    Non-empty means the thesis names one or more offices and names no
    referent at all — the Iran-premiership shape. Empty means either no
    office is named, or something in the thesis says whose office it is.
    """
    offices = named_offices(thesis)
    if not offices:
        return []
    return [] if named_referents(thesis) else offices
