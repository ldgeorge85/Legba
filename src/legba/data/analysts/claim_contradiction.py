# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 — cross-finding contradiction detection over a desk's VERIFIED set.

The measured case (review1 #2, carried to REVIEW_TRIAGE R2): one desk published
"the Strait of Hormuz remains effectively shut" and, seventy-nine minutes later,
"no concrete closure is in place". Both findings passed verify. Both entered the
same composition. The composition **agreed with both of them.**

Nothing in the tower was wrong, exactly — each finding was faithful to what it
cited. The tower simply had no mechanism that could notice P and ¬P sitting in
the same input set, because it had never been asked to compare the inputs to each
other. The composition prompt does carry a rule about surfacing factual
disagreement (``_tension_rule``, "one says a chokepoint is closed, another says no
closure is in place" — the rule was written FROM this case). A prompt rule is a
request to notice. This is the noticing.

DELIBERATELY NARROW. It looks for one shape and one shape only: two claims from
DIFFERENT findings that speak about the same subject and take opposite positions
on the same POLARITY GROUP — a small closed vocabulary of the state-oppositions
that matter in this domain (open/closed, ceasefire/fighting, sanctioned/lifted…),
with explicit negation handling. It will miss real contradictions expressed in
ways this vocabulary does not cover. That is the correct trade for a check whose
output is shown to an LLM as fact and counted as a verify failure: a missed
contradiction leaves today's behaviour, a fabricated one poisons a composition.

CALIBRATED AGAINST THE LIVE CORPUS, and the calibration is most of the design.
Swept over 32 country desks / 264 input refs / 1,592 verified claims:

    first cut   57 pairs across 24 of 32 desks — almost all false
    shipped     0 pairs

The four rules that closed the gap, each traceable to a real false pair:

1. **Scaffold words are not subjects.** An Indonesian stability read paired with
   an Indonesian energy read on ``{"bluf", "indonesia"}`` — one markup token and
   one country name.
2. **A polarity term must sit NEXT TO the shared subject** (``_SUBJECT_PROXIMITY``).
   Without it a BLUF enumerating five domains reads as an assertion about every
   state it names, and any claim mentioning any of them contradicts it.
3. **Long sentences are not single propositions** (``_MAX_CLAIM_CHARS``).
4. **The vocabulary is pruned to state-oppositions**, not directions or moods —
   see the note under ``_POLARITY_GROUPS`` for exactly what came out and why.

Zero detections on a real day is the EXPECTED reading, not a broken check: the
Hormuz pair was one event, and the tests pin both that it fires on that shape and
that it stays silent on the four false shapes above.

Detection is DETECT-ONLY here — it returns findings, mutates nothing, and never
decides which side is right. Deciding is the composition's job, which is why the
output is rendered into its input rather than applied to its output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: Words that carry no subject identity: ordinary function words, the reporting
#: verbs every finding uses, and — the ones that actually bit — the DOCUMENT
#: SCAFFOLD vocabulary. A first sweep over the live corpus paired an Indonesian
#: stability read with an Indonesian energy read because they shared exactly
#: ``{"bluf", "indonesia"}``: one markup token and one country. Markup is not a
#: subject, and neither, on its own, is the desk's own name.
_STOPWORDS = frozenset(
    """
    a an the of in on at to for from by with without into over under and or but
    is are was were be been being has have had do does did will would can could
    may might must shall should as that this these those it its their his her
    they them we our you your i he she who whom which what when where while
    there here than then so such per about across amid within between during
    remains remain remained continues continue continued reported reports report
    according said says say stated state states indicates indicate indicated
    appears appear appeared assessed assessment amid also still yet however
    currently now recently new newly further additional more most less least
    bluf key point points assessment indicators watch severity confidence
    trajectory outlook horizon summary finding findings read reads prior
    previous window slice desk domain domains analysis analyst signal signals
    evidence source sources ref refs collection observed observation note noted
    remain unchanged change changes changed level levels risk driven drivers
    near term overall trend pressure concern concerns
    steady tension situation level plausible shift factor critical ongoing
    any some other others consequently pending trajectory posture status
    """.split()
)

#: The POLARITY GROUPS. Each is one state-opposition; a claim that names a term
#: from either side takes that side's sign. A negation in front of the term flips
#: it. Two claims contradict when they land on the same group with opposite signs.
#:
#: Every group here is a state a desk actually asserts and denies — kept to the
#: chokepoint / conflict / infrastructure / governance vocabulary the fleet reads.
_POLARITY_GROUPS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "closure": (
        frozenset(
            {
                "closed", "closure", "closures", "shut", "shutdown", "blockade",
                "blockaded", "blocked", "sealed", "suspended", "halted", "severed",
            }
        ),
        frozenset(
            {
                "open", "opened", "reopened", "operating", "operational", "flowing",
                "unimpeded", "transiting", "resumed", "restored", "uninterrupted",
            }
        ),
    ),
    "hostilities": (
        frozenset(
            {
                "fighting", "clashes", "offensive", "strikes", "shelling",
                "hostilities", "escalation", "escalating", "escalate",
                "escalated", "attacks", "combat",
            }
        ),
        frozenset(
            {
                "ceasefire", "truce", "armistice", "calm", "de-escalation",
                "deescalation", "de-escalating", "deescalating", "de-escalate",
                "deescalate", "withdrawal", "withdrew", "peace", "quiet",
            }
        ),
    ),
    "supply": (
        frozenset({"shortage", "shortages", "outage", "outages", "blackout",
                   "rationing", "curtailed"}),
        frozenset({"surplus", "uninterrupted", "restored"}),
    ),
    "control": (
        frozenset({"captured", "seized", "controls", "controlled", "occupied"}),
        frozenset({"ceded", "relinquished", "abandoned", "retreated", "expelled"}),
    ),
    "sanctions": (
        frozenset({"sanctioned", "embargo", "banned", "prohibited"}),
        frozenset({"lifted", "eased", "waiver", "exempted", "relaxed"}),
    ),
}
# NOTE on what was REMOVED, because the removals are the calibration:
#
#   * ``trend`` (rising/falling) — every finding describes a direction, so it
#     matched everywhere and meant nothing.
#   * ``stable`` / ``steady`` / ``normal`` from ``supply``'s negative side, and
#     ``disruption(s)`` from its positive side — "remains steady" is the single
#     most common phrase in the corpus, and pairing it with any "disruption"
#     mention made a stability read contradict an energy read about the same
#     country. Both are ordinary prose, neither is a claim about supply STATE.
#   * ``holds`` / ``took`` / ``lost`` / ``withdrew`` from ``control`` — far too
#     polysemous ("talks were held", "the meeting took place", "lost ground").
#   * ``sanctions`` / ``restrictions`` (the bare nouns) — naming a sanctions
#     regime is not asserting one exists now.
#
# The first live sweep with the un-pruned table produced 57 pairs across 24 of 32
# desks. Almost every one was spurious. A detector at that precision, wired into a
# composition's input as a stated fact, would manufacture disagreements for the
# fleet to write up — strictly worse than the silence it replaced.

#: Negators. A negator within ``_NEGATION_WINDOW`` tokens BEFORE a polarity term
#: flips that term's sign — "no concrete closure" is the negative side of
#: ``closure``, and it is the exact phrasing of the live case.
_NEGATORS = frozenset(
    {
        "no", "not", "never", "without", "nor", "neither", "denies", "denied",
        "denying", "absent", "lacks", "lacking", "failed", "unable", "cannot",
        "n't", "none", "nothing", "refutes", "refuted", "rejects", "rejected",
    }
)
_NEGATION_WINDOW = 4

#: A pair must share at least this many SUBJECT tokens.
_MIN_SUBJECT_OVERLAP = 2

#: …and the shared subject must be this fraction of the smaller claim's subject
#: set. Raw overlap alone let two long, unrelated claims about one country pair on
#: any two incidental words; requiring the shared part to be a real PROPORTION of
#: what the shorter claim is about is what makes "both of these are about the
#: Strait of Hormuz" different from "both of these mention Indonesia".
_MIN_SUBJECT_COVERAGE = 0.34

#: Whether a single shared PROPER NOUN is enough on its own. It is not, and the
#: measurement is why.
#:
#: The argument for it is good: "a ceasefire in Rafah has held" against "fighting
#: in Rafah intensified" shares exactly one identity token, and requiring two
#: makes the check blind to every disagreement about a single named place — which
#: is most of them. Enabling it took the live sweep from 0 pairs to 5. All five
#: were false, and all five for the same reason: the shared proper noun was
#: ``Israeli`` / ``France`` / ``Saudi`` / ``South`` — THE DESK'S OWN NAME. Every
#: claim on a country desk names that country, so it is the least distinctive
#: token available, and treating it as an identity anchor makes any two claims on
#: a desk eligible to contradict each other.
#:
#: Fixing that properly needs a notion of desk SCOPE the detector does not have
#: (target_id is ``country_watch_il``, not "Israel"), or a document-frequency cut
#: that a two-input desk cannot support. So this stays off, the recall gap is
#: real and known — single-named-subject disagreements are missed — and the flag
#: is left in place because flipping it is the whole experiment, and the number
#: to beat is written down.
_PROPER_NOUN_IS_SUFFICIENT = False

#: A polarity term only counts when a SHARED subject token sits within this many
#: tokens of it. This is the load-bearing precision rule. Without it, "Israel
#: faces low energy-security pressure, with no supply disruptions, price shocks,
#: infrastructure attacks…" reads as an assertion about every state named in it,
#: and any other Israel claim mentioning any of them "contradicts" it. With it,
#: the polarity term must sit next to the thing the pair actually shares — which
#: is what "these two claims disagree ABOUT X" means.
_SUBJECT_PROXIMITY = 6

#: Claims longer than this are not single propositions. A 400-character sentence
#: enumerating five domains cannot be assigned one polarity, and trying is how a
#: detector invents disagreement.
_MAX_CLAIM_CHARS = 320

#: Never surface more than this many pairs into one composition — a long tension
#: list is noise, and the top few by overlap are the informative ones.
MAX_CONTRADICTIONS = 5

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_RAW_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")
_MARKER_RE = re.compile(r"\[\[ref:[^\]]{1,64}\]\]|\[\d+(?:\s*-\s*\d+)?\]")


@dataclass(frozen=True)
class ClaimContradiction:
    """One detected P ∧ ¬P pair. Detect-only: no winner is named."""

    group: str
    subject: tuple[str, ...]
    a_ref: int
    a_text: str
    b_ref: int
    b_text: str
    #: ``+1`` side asserts the group's positive state, ``-1`` denies it.
    a_sign: int = 0
    b_sign: int = 0
    overlap: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "subject": list(self.subject),
            "a_ref": self.a_ref,
            "a_text": self.a_text[:400],
            "a_sign": self.a_sign,
            "b_ref": self.b_ref,
            "b_text": self.b_text[:400],
            "b_sign": self.b_sign,
            "overlap": self.overlap,
        }


@dataclass
class _ClaimView:
    ref: int
    text: str
    tokens: tuple[str, ...] = ()
    subject: frozenset[str] = field(default_factory=frozenset)
    #: Subject tokens that appeared CAPITALIZED away from a sentence opening —
    #: proper nouns, i.e. the tokens that name a specific thing.
    proper: frozenset[str] = field(default_factory=frozenset)
    #: group name -> sign (+1 asserts, -1 denies). A claim may touch >1 group.
    polarity: dict[str, int] = field(default_factory=dict)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(_MARKER_RE.sub(" ", text or "").lower())


def _proper_tokens(text: str) -> frozenset[str]:
    """Lowercased tokens that appeared Capitalized somewhere NOT at a sentence
    opening. The sentence-initial exclusion is what stops "The", "No" and every
    other ordinary first word from being read as a name."""
    cleaned = _MARKER_RE.sub(" ", text or "")
    out: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+", cleaned):
        for i, tok in enumerate(_RAW_TOKEN_RE.findall(sentence)):
            if i == 0:
                continue
            if tok[0].isupper() and len(tok) > 2:
                out.add(tok.lower())
    return frozenset(out)


def _polarity_of(
    tokens: Sequence[str], *, anchors: frozenset[str] | None = None
) -> dict[str, int]:
    """``{group: sign}`` for every polarity group this token stream takes a side on.

    A term on the group's POSITIVE side scores ``+1``; the NEGATIVE side ``-1``; a
    negator within the preceding window flips it. When a claim lands on both signs
    of one group (a genuinely mixed sentence — "the port reopened but the strait
    stays shut") the group is DROPPED rather than guessed: an ambiguous polarity is
    not evidence of anything, and forcing it would manufacture contradictions.

    ``anchors`` — when given, a polarity term only counts if one of these tokens
    (the SHARED subject of the candidate pair) sits within ``_SUBJECT_PROXIMITY``.
    That is what makes the verdict mean "this claim takes a position on THAT
    subject" rather than "this claim contains that word somewhere".
    """
    out: dict[str, int] = {}
    dropped: set[str] = set()
    for i, tok in enumerate(tokens):
        for group, (positive, negative) in _POLARITY_GROUPS.items():
            if tok in positive:
                sign = 1
            elif tok in negative:
                sign = -1
            else:
                continue
            if anchors is not None:
                near = tokens[
                    max(0, i - _SUBJECT_PROXIMITY):i + _SUBJECT_PROXIMITY + 1
                ]
                if not any(w in anchors for w in near):
                    continue
            window = tokens[max(0, i - _NEGATION_WINDOW):i]
            if any(w in _NEGATORS for w in window):
                sign = -sign
            if group in out and out[group] != sign:
                dropped.add(group)
            out[group] = sign
    for group in dropped:
        out.pop(group, None)
    return out


def _subject_tokens(tokens: Sequence[str]) -> frozenset[str]:
    """The identity words — everything that is not a stopword, a negator, or a
    polarity term. Those three are what the claims are DISAGREEING with; the
    remainder is what they are disagreeing ABOUT."""
    polarity_terms = {
        t for pos, neg in _POLARITY_GROUPS.values() for t in (pos | neg)
    }
    return frozenset(
        t
        for t in tokens
        if len(t) > 2
        and t not in _STOPWORDS
        and t not in _NEGATORS
        and t not in polarity_terms
    )


def _view(ref: int, text: str) -> _ClaimView:
    tokens = _tokenize(text)
    subject = _subject_tokens(tokens)
    return _ClaimView(
        ref=ref,
        text=text,
        tokens=tuple(tokens),
        subject=subject,
        proper=frozenset(_proper_tokens(text) & subject),
        polarity=_polarity_of(tokens),
    )


def detect_contradictions(
    claims_by_ref: Mapping[int, Iterable[str]],
    *,
    max_pairs: int = MAX_CONTRADICTIONS,
) -> list[ClaimContradiction]:
    """Detect P ∧ ¬P across the composition's input set.

    ``claims_by_ref`` maps each input's ``[[ref:N]]`` ordinal to that finding's
    VERIFIED claim texts. Pairs are only ever formed ACROSS refs — a single
    finding contradicting itself is a different defect with a different owner
    (the unit's own verify pass), and reporting it here would put a desk's
    internal hedging into a composition's tension section as if two desks
    disagreed.

    Returned strongest-first by subject overlap. Pure, deterministic, allocation-
    bounded; never raises on malformed input.
    """
    views: list[_ClaimView] = []
    for ref, claims in (claims_by_ref or {}).items():
        try:
            ordinal = int(ref)
        except (TypeError, ValueError):
            continue
        for text in claims or ():
            if not isinstance(text, str) or not text.strip():
                continue
            # A long enumerating sentence is not one proposition (see
            # _MAX_CLAIM_CHARS). Skipping it costs recall on compound prose and
            # buys the precision this check lives or dies by.
            if len(text) > _MAX_CLAIM_CHARS:
                continue
            view = _view(ordinal, text)
            # Only claims that take a side on some group can contradict anything.
            if view.polarity:
                views.append(view)

    found: list[ClaimContradiction] = []
    seen: set[tuple[int, int, str]] = set()
    for i, a in enumerate(views):
        for b in views[i + 1:]:
            if a.ref == b.ref:
                continue
            shared_subject = a.subject & b.subject
            shared_proper = a.proper & b.proper
            if shared_proper and _PROPER_NOUN_IS_SUFFICIENT:
                pass  # a shared NAME carries the overlap on its own
            elif len(shared_subject) < _MIN_SUBJECT_OVERLAP:
                continue
            else:
                smaller = min(len(a.subject), len(b.subject)) or 1
                if len(shared_subject) / smaller < _MIN_SUBJECT_COVERAGE:
                    continue
            # RE-DERIVE each side's polarity anchored on what the pair actually
            # shares. The unanchored pass above is only a cheap prefilter; this is
            # the verdict, and a claim whose polarity term is nowhere near the
            # shared subject drops out here.
            a_pol = _polarity_of(a.tokens, anchors=shared_subject)
            b_pol = _polarity_of(b.tokens, anchors=shared_subject)
            for group, a_sign in a_pol.items():
                b_sign = b_pol.get(group)
                if b_sign is None or b_sign == a_sign:
                    continue
                key = (min(a.ref, b.ref), max(a.ref, b.ref), group)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    ClaimContradiction(
                        group=group,
                        subject=tuple(sorted(shared_subject)[:6]),
                        a_ref=a.ref,
                        a_text=a.text,
                        a_sign=a_sign,
                        b_ref=b.ref,
                        b_text=b.text,
                        b_sign=b_sign,
                        overlap=len(shared_subject),
                    )
                )
    found.sort(key=lambda c: (-c.overlap, c.a_ref, c.b_ref, c.group))
    return found[:max_pairs]


def render_tension_block(contradictions: Sequence[ClaimContradiction]) -> str:
    """The composition input block. ``""`` when nothing was detected.

    Written as EVIDENCE, not as an instruction: it states what was found and which
    two handles carry it, and leaves the adjudication to the composition (whose
    ``## Tension`` rule already says how to write it up). The ``[[tension:N]]``
    handle mirrors the ``[[contested:<id>]]`` convention the fact-contention block
    established, so the prompt grammar stays one grammar.
    """
    if not contradictions:
        return ""
    lines = [
        "DETECTED CONTRADICTIONS among the findings above "
        f"({len(contradictions)}). These were computed by comparing the shown "
        "findings' VERIFIED claims against each other — they are not a "
        "suggestion, they are a measurement. Each pair asserts incompatible "
        "states of the same subject. You MUST address every pair in the "
        "'## Tension' section: name both sides, cite BOTH [[ref:N]] handles, "
        "and say which is better supported and why. Do NOT average them into a "
        "consensus, and do NOT silently drop one side.",
    ]
    for n, c in enumerate(contradictions, start=1):
        asserts, denies = (
            (c.a_ref, c.b_ref) if c.a_sign > 0 else (c.b_ref, c.a_ref)
        )
        a_txt, b_txt = (
            (c.a_text, c.b_text) if c.a_sign > 0 else (c.b_text, c.a_text)
        )
        lines.append(
            f"[[tension:{n}]] subject={' '.join(c.subject)} state={c.group} :: "
            f"[[ref:{asserts}]] ASSERTS it — \"{a_txt[:240]}\" ;; "
            f"[[ref:{denies}]] DENIES it — \"{b_txt[:240]}\""
        )
    return "\n".join(lines)


__all__ = [
    "MAX_CONTRADICTIONS",
    "ClaimContradiction",
    "detect_contradictions",
    "render_tension_block",
]
