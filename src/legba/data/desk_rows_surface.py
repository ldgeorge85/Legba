# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SURFACE subjects and GENERIC ROLES for contract-v2 fact rows (lane D-2).

THE UNRESOLVED ACTOR. A fact row's quote may name an actor that no entity the
entity layer knows matches — a company, a militia, an official the linker has
not met. D-1 could only fail such a row (``subject_not_offered`` /
``subject_not_named`` against the wrong S-id). Contract v2.1 gives it a
declared, typed outcome instead: the row carries the actor's SURFACE FORM —
copied verbatim from one of its own quotes — as its subject
(``subject_kind: "surface"``). It stays checkable (the name is in the quote by
construction, and the verifier still judges the claim), and it is counted
apart so the entity layer's gap is visible rather than charged to the row.

WHAT A SURFACE SUBJECT IS NOT (each refused with a named reason):

==================================  =========================================
``surface_not_in_quote``            not a verbatim substring of a cited quote
``surface_not_proper_name``         not shaped like a proper name (1-8 words,
                                    capitalised words joined only by
                                    connectors such as "of", "al-", "and")
``subject_generic_role``            a generic role — "officials", "a
                                    spokesperson", "the ministry", "security
                                    forces" — never a subject (below)
``surface_names_an_offered_subject``  it IS an offered subject's name: the
                                    row must take that S-id
==================================  =========================================

WHAT A SURFACE SUBJECT MEANS FOR PERSISTENCE. It is a name, not a key: it is
not an entity id, it cannot be merged with another row's subject, followed
over time, or joined to the entity layer until something resolves it to an
entity. The statement layer's closed subject vocabulary (``statement_gate``
G8: target / entity / series / place) has no ``surface`` kind, so a surface
row can never be written there as is — the shadow records it, counted under
its own key, and anything that needs an entity id must skip it.

GENERIC ROLES ("officials", "a spokesperson", "authorities", "the military")
are never subjects, neither as a surface form nor as a counterpart given as
words: a row whose speaker is a generic role attaches to the BODY the role
speaks for when the quote names it (an offered id, or that body's name as a
surface form), or is written ``attributed`` with the speaker as the quote
states it and the subject the claim is about.

A LEAF: stdlib + the names rule.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from .provenance.text_fold import normalize_for_match

SURFACE = "surface"

#: Connectors a multi-word proper name may carry between capitalised words.
_CONNECTORS = frozenset({"of", "the", "for", "and", "al", "el", "de", "del", "da", "di", "du", "la", "le",
                         "bin", "ibn", "abu", "von", "van", "der", "den", "on", "&", "y", "e", "au"})
#: A surface form is at most this many words.
MAX_SURFACE_WORDS = 8

#: Role nouns that are never a subject on their own (with any of the
#: modifiers below in front of them).
_ROLE = (r"officials?|spokes(?:person|man|woman|people)|authorities|sources?|analysts?|experts?|residents?|"
         r"witnesses|diplomats?|observers?|government|administration|ministry|military|army|police|troops|"
         r"forces|militants?|rebels?|insurgents?|protesters?|lawmakers?|legislators?|company|operator|"
         r"regulator|traders?|investors?|media|state media|prosecutors?|leaders?|ministers?|officers?|"
         r"commanders?|soldiers|activists?|critics|opposition|security services|agency|agencies|"
         r"the president|the premier|the prime minister|state|people")
_MOD = (r"(?:the|a|an|some|several|two|three|many|senior|local|top|unnamed|anonymous|government|"
        r"military|security|state|regional|foreign|western|european|federal|provincial|city|"
        r"official|other)")
GENERIC_ROLE_RE = re.compile(rf"^(?:{_MOD}\s+)*(?:{_ROLE})$", re.IGNORECASE)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().strip(" ,.;:\"'“”‘’()"))


def is_generic_role(text: str) -> bool:
    """The words name a role, not a party ("officials", "a spokesperson",
    "Senior military officials", "the Ministry")."""
    return bool(GENERIC_ROLE_RE.match(_clean(text)))


def is_proper_name_shaped(text: str) -> bool:
    """1-:data:`MAX_SURFACE_WORDS` words; the first word capitalised (or an
    acronym / a word with a digit); every other word capitalised, an acronym,
    a hyphenated capitalised compound or a connector; never ending in a
    connector."""
    words = _clean(text).split()
    if not words or len(words) > MAX_SURFACE_WORDS:
        return False

    def cap(w: str) -> bool:
        core = w.strip("'’.,")
        if not core:
            return False
        head = re.split(r"[-’']", core)[-1] if core.lower().startswith(("al-", "el-")) else core
        return head[:1].isupper() or any(ch.isdigit() for ch in core)

    if not cap(words[0]) or words[-1].lower() in _CONNECTORS:
        return False
    return all(cap(w) or w.lower() in _CONNECTORS for w in words[1:])


def in_quotes(surface: str, quotes: Iterable[str]) -> bool:
    """The surface form is a verbatim substring of a quote (whitespace runs
    folded; case kept)."""
    s = re.sub(r"\s+", " ", (surface or "").strip())
    return bool(s) and any(s in re.sub(r"\s+", " ", q or "") for q in quotes)


def names_an_offered(surface: str, subjects: Sequence[Any]) -> int | None:
    """The 1-based index of the offered subject one of whose usable names IS
    this surface form (folded, a leading article dropped), else ``None``."""
    want = normalize_for_match(re.sub(r"^(?:the|a|an)\s+", "", _clean(surface), flags=re.IGNORECASE))
    if not want:
        return None
    for i, s in enumerate(subjects, start=1):
        for n in getattr(s, "names", ()):
            if normalize_for_match(str(getattr(n, "name", n))) == want:
                return i
    return None


def surface_refusals(surface: str, quotes: Sequence[str], subjects: Sequence[Any]) -> list[str]:
    """Every reason ``surface`` cannot be a fact row's subject (empty = it
    can)."""
    out: list[str] = []
    s = _clean(surface)
    if not s:
        return ["surface_missing"]
    if not in_quotes(s, quotes):
        out.append("surface_not_in_quote")
    if is_generic_role(s):
        out.append("subject_generic_role")
    elif not is_proper_name_shaped(s):
        out.append("surface_not_proper_name")
    if names_an_offered(s, subjects) is not None:
        out.append("surface_names_an_offered_subject")
    return out


__all__ = [
    "GENERIC_ROLE_RE",
    "MAX_SURFACE_WORDS",
    "SURFACE",
    "in_quotes",
    "is_generic_role",
    "is_proper_name_shaped",
    "names_an_offered",
    "surface_refusals",
]
