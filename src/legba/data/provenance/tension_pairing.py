# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WHAT MAKES TWO BLOCKS ONE VARIABLE — the pairing rules behind a declared tension.

THE DEFECT (world read 64f74201 / record e30e67d3, 2026-11-07). ``direction_conflict``
was built for the composition checking itself against ITS OWN desk, where "the two
spans are about one subject" is a fair gate. The world tier's ``_tensions`` reused it
across 16 carried blocks and 33 candidates and the gate became "any shared word of
five letters": 48 pairs out of 392 examined, the subjects being "remain" (9),
"november, remain" (6), "driver, remain" (4), "pressure" (2). Saudi Arabia's
economic-coercion read paired with Sudan's energy-security read on "pressure"; Israel's
procurement squeeze paired with the US force lay-down on "military, posture, strike".
Neither pair is one variable, and the template then said "describe the same dimension".

THE RULE (N-C, 2026-11-11 — "same entity AND same variable, no exceptions"):

  0. the two blocks must be about the SAME ENTITY. A pair whose targets differ is
     never a tension (``cross_unit``). Measured on world record fd6584eb: Türkiye's
     leadership read paired with Burkina Faso's composition on the leadership desk's
     own template words ("assessed", "likelihood", "cabinet"), and Iran's escalation
     read paired with the US escalation read on "tanker, trump". Two countries'
     desks answering the same bounded question about different countries are two
     variables, whatever words they share. The rules below (kept for any caller
     that marks a pair cross-unit and still wants the reason) used to admit such a
     pair; the entity rule now decides first.
  1. the two blocks must come from the SAME desk (the desk's bounded question is the
     dimension), and
  2. they must be about the same VARIABLE. The strongest evidence is a shared
     indicator id (``level_indicators`` / the desk's ``indicators[]``): when both
     sides carry indicator ids and none is shared they are different variables, and
     when one is shared it is the subject. Blocks do not yet carry an indicator id
     (the spine copies the BLUF span and the desk, nothing finer), so the fallback is
     a shared CONTENT term once the stopword screen below has removed dates, months,
     generic verbs and nouns, and the desks' own boilerplate.

PURE: strings in, a verdict out. No connection, no clock.
"""
from __future__ import annotations

from typing import Any, Iterable

__all__ = [
    "CROSS_UNIT_BLOCKS",
    "TENSION_STOPWORDS",
    "TENSION_STOPWORDS_DESK",
    "TENSION_STOPWORDS_GENERIC",
    "TENSION_SUBJECT_MIN_CHARS",
    "indicator_ids",
    "pair_scope_blocker",
    "shared_indicator_ids",
    "tension_subject_terms",
]

#: Rule 0 — a cross-unit pair is never one variable. A module constant (not an
#: env flag) so the rule is stated once and a test can pin it.
CROSS_UNIT_BLOCKS: bool = True

#: A shared term shorter than this binds nothing (the shipped H2 floor, unchanged).
TENSION_SUBJECT_MIN_CHARS: int = 5

#: Terms that say nothing about WHICH variable a span is about, matched on the
#: singular-stemmed, lowercased terms ``_content_terms`` returns. Measured live on
#: e30e67d3: every one of these was a "shared subject" in the 48 declared pairs.
#:
#: Split in two because the callers differ. ``direction_conflict`` also serves the
#: composition-vs-its-own-desk check (a HARD verifier class), where a desk's own
#: vocabulary ("energy", "military") IS the subject; only the generic set may touch
#: it. Dates, and the verbs and hedges every desk BLUF is written in, name no
#: variable anywhere.
TENSION_STOPWORDS_GENERIC: frozenset[str] = frozenset({
    # --- dates: a shared week is not a shared variable
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "today", "yesterday", "tonight", "overnight", "weekend", "window", "week",
    "month", "year", "quarter", "since", "until", "prior", "latest", "recent",
    "current", "ongoing",
    # --- generic verbs and connectives that carry no subject
    "remain", "continue", "persist", "stay", "keep", "maintain", "sustain",
    "become", "through", "across", "within", "toward", "towards", "while",
    "between", "against", "despite", "after", "before", "during", "among",
    "around", "beyond", "under",
    # --- hedges
    "likely", "still", "already", "further", "several", "other", "about",
    "which", "their", "there", "these", "those", "would", "could", "might",
    "should", "being", "where", "whose",
})

#: The CROSS-UNIT additions: the desks' own names and the nouns every desk read is
#: written in. Two different countries' military_posture reads always share
#: "military" and "posture"; that is the desk, not a variable.
TENSION_STOPWORDS_DESK: frozenset[str] = frozenset({
    "military", "posture", "energy", "security", "economic", "coercion",
    "escalation", "internal", "stability", "narrative", "coordination",
    "leadership", "transition", "proliferation", "assessment", "indicator",
    "driver", "standing", "pressure", "condition", "trajectory", "level",
    "baseline", "read", "desk", "report", "reporting",
    "strike", "raid", "attack", "force", "state", "country", "government",
    "major", "amount", "number", "period", "pace", "scale", "high", "peak",
    "record", "single", "least", "hold", "amid", "given", "over",
    # the economic_coercion role frame ("TARGET of ... WIELDER of ... squeeze")
    "wielder", "target", "squeeze", "sanction",
})

#: The full cross-unit screen.
TENSION_STOPWORDS: frozenset[str] = TENSION_STOPWORDS_GENERIC | TENSION_STOPWORDS_DESK


def tension_subject_terms(
    shared: Iterable[str], *, cross_unit: bool = False
) -> set[str]:
    """The shared terms that can actually name a variable: the length floor, then
    the stopword screen (the generic set always; the desk-boilerplate set too when
    the pair is cross-unit). ``shared`` is the intersection the caller computed."""
    screen = TENSION_STOPWORDS if cross_unit else TENSION_STOPWORDS_GENERIC
    return {
        t for t in shared
        if len(t) >= TENSION_SUBJECT_MIN_CHARS and t not in screen
    }


def indicator_ids(value: Any) -> frozenset[str]:
    """A block's indicator ids, tolerant of every shape a caller might hold: ``None``,
    one id, an iterable of ids, or an iterable of ``{"id": ...}`` mappings."""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value.strip()}) if value.strip() else frozenset()
    out: set[str] = set()
    try:
        for item in value:
            if isinstance(item, dict):
                item = item.get("id") or item.get("indicator_id")
            if isinstance(item, str) and item.strip():
                out.add(item.strip())
    except TypeError:
        return frozenset()
    return frozenset(out)


def shared_indicator_ids(a: Any, b: Any) -> frozenset[str]:
    """The indicator ids both sides carry (empty when either side carries none)."""
    return indicator_ids(a) & indicator_ids(b)


def is_cross_unit(target_a: str | None, target_b: str | None) -> bool:
    """Both targets known and different."""
    return bool(target_a and target_b and target_a != target_b)


def pair_scope_blocker(
    *,
    target_a: str | None = None,
    target_b: str | None = None,
    desk_a: str | None = None,
    desk_b: str | None = None,
    indicators_a: Any = None,
    indicators_b: Any = None,
) -> str | None:
    """Why this pair cannot be one variable, or ``None`` when it may proceed.

    A pair whose two targets are both known and DIFFERENT is blocked outright
    (``cross_unit``, rule 0), and a same-unit pair whose two desks differ is
    blocked as ``different_desk``: a tension is one entity and one variable at
    every tier, no exceptions. A text-only caller (the composition-vs-its-own-desk
    check, which passes no desks) is not gated here.
    """
    if not is_cross_unit(target_a, target_b):
        # Same unit: still one VARIABLE — two desks of one country are two
        # bounded questions (N-C follow-up: every tier, no exceptions).
        if desk_a and desk_b and desk_a != desk_b:
            return "different_desk"
        return None
    if CROSS_UNIT_BLOCKS:
        return "cross_unit"
    if desk_a and desk_b and desk_a != desk_b:
        return "different_desk"
    ia, ib = indicator_ids(indicators_a), indicator_ids(indicators_b)
    if ia and ib and not (ia & ib):
        return "different_indicator"
    return None
