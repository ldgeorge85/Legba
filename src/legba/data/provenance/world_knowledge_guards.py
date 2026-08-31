# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WORLD-KNOWLEDGE + TARGET GUARDS — the M13 / M15 / E-1 family, in one module.

Everything here answers a question the faithfulness judge is not asked: not "did
the citations support this claim" but "is this claim about the WORLD wrong on its
face". Three guards, one shape — each is a cheap detector that FLAGS by emitting
an ``UnsupportedSpan``, never deletes, and fails OPEN whenever it cannot tell:

  * **M13 stale-cutoff leader** (:func:`stale_leader_spans`) — a curated regex
    pair over the one live model-internal error (the sitting US president called
    "former"; a predecessor asserted as current).
  * **E-1 facts-reconciled officeholder** (:func:`stale_leader_vs_facts_spans`
    and its extractor :func:`extract_officeholder_claims`) — M13's data-backed
    sibling, reconciling a named officeholder claim against the OPEN ``facts``
    row for that office.
  * **M15 cross-target leak** (:func:`cross_target_leak_span`) — a per-country
    desk whose finding names only OTHER countries and never its own.

Extracted from ``verify.py`` 2026-08-29 (the [N+1] transparency train), which
found the module at 5,891 lines against a 5,900 DO-NOT-RAISE ceiling — nine lines
of headroom, i.e. blocked. This family was the seam: it carries its own section
banner, its own dedicated test file
(``tests/data_pkg/test_verify_world_knowledge_guards.py``), and it is the largest
block in the file whose members only ever talk to each other.

WHAT DELIBERATELY STAYS IN ``verify.py`` — the FOLDS
(``_fold_world_knowledge_guards`` / ``_fold_guard_spans``), for the reason the
V-I banner already recorded about the markerless-uncited fold: they manipulate
the report + ledger types that module owns, and ``_fold_guard_spans`` is shared
with W31 and the absence route besides.

THE IMPORT EDGE. The gazetteer comes from ``absence_slice`` (a sibling, one way,
no cycle — the M15 guard and the V-B slice scope have always read the SAME
tables). ``UnsupportedSpan`` cannot: ``verify`` imports THIS module, so the span
type late-binds through :func:`_verify` at call time — the
``judge_input_checks`` / ``composition_integrity`` pattern. ``verify``
RE-EXPORTS every name moved here, so ``verify.stale_leader_spans``,
``verify.extract_officeholder_claims``, ``verify._STALE_LEADER_REASON`` and the
rest resolve exactly as before.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .absence_slice import (
    _COUNTRY_TOKENS,
    _TARGET_SLUG_TO_COUNTRY,
    _country_desk_slug,
    _mentions_own_country,
    _names_country,
)

if TYPE_CHECKING:  # pragma: no cover — annotations only
    from .verify import UnsupportedSpan

logger = logging.getLogger(__name__)


def _verify():
    """Lazy accessor — this module is imported BY verify, so the edge runs one
    way at call time only (the ``judge_input_checks`` pattern)."""
    from . import verify

    return verify


# ---------------------------------------------------------------------------
# M13 / M15 (2026-07-06) — write/verify-time world-knowledge + target guards
# ---------------------------------------------------------------------------
#
# The faithfulness judge grades CITATION-support, not world-knowledge, so two
# defect classes both the citation floor AND the judge miss:
#
#   M13 STALE-CUTOFF LEADER — an assessor back-fills a current officeholder from a
#     pre-cutoff training prior ("renewed cooperation via FORMER President Trump"
#     while the cited signals establish Trump as the SITTING president).
#   M15 CROSS-TARGET LEAK — a per-country UNIT finding whose named subject-country
#     is the WRONG one (a Turkey desk head titled/bodied entirely "Romania").
#
# Both are cheap LEXICAL backstops that FLAG (add an unsupported span → demote
# effective_confidence via the min(confidence, faithfulness) gate), NEVER delete.
# Kept LOCAL + stdlib-only so verify.py stays slim-image-safe (no runtime import);
# the curated maps deliberately MIRROR their runtime counterparts (the
# legba.runtime.grounding current-officeholder anchor / finding_is_off_target
# gazetteer) — minimal by design (US president only; a small country-token set).

_STALE_LEADER_REASON = "stale_leader"
_CROSS_TARGET_REASON = "cross_target_leak"

# Curated CURRENT officeholders (US president ONLY — the one clear live stale-
# cutoff error; extend only for a NEW confirmed live error). Two stale shapes:
#   * a "former/ex/past ... <current holder>" reference — calling the SITTING
#     holder "former" is always a temporal error;
#   * a predecessor asserted as the CURRENT / sitting holder.
# The qualifier→title separator is ``[-\s]+`` so the HYPHENATED "ex-President
# Trump" matches (``ex`` + ``-`` + ``President``) as well as the spaced forms
# ("former President Trump" / "past President Trump").
_STALE_TRUMP_FORMER_RE = re.compile(
    r"\b(?:former|ex|past|previous)[-\s]+(?:u\.?\s?s\.?\s+)?presidents?\s+"
    r"(?:donald\s+(?:j\.?\s+)?)?trump\b"
    r"|\btrump\b\s*,?\s+(?:the\s+)?(?:former|ex|past|previous)[-\s]+"
    r"(?:u\.?\s?s\.?\s+)?president\b",
    re.IGNORECASE,
)
# A predecessor asserted AS THE CURRENT holder — ONLY explicit current-frame
# shapes. The bare "now/today within N chars" proximity is DELIBERATELY dropped:
# it false-flagged "President Biden, NOW a private citizen" (which correctly says
# Biden is out of office). Two accepted shapes: an explicit "current/sitting/
# incumbent (US) president … Biden", or "President Biden {remains in office | is
# the current/sitting president}".
_STALE_WRONG_POTUS_RE = re.compile(
    r"\b(?:current|sitting|incumbent)\s+(?:u\.?\s?s\.?\s+)?president[^.\n;]{0,32}"
    r"\b(?:joe\s+)?biden\b"
    r"|\bpresident\s+(?:joe\s+)?biden\b[^.\n;]{0,24}"
    r"\b(?:remains?\s+in\s+office|is\s+(?:the\s+)?(?:current|sitting|incumbent)(?:\s+president)?)\b",
    re.IGNORECASE,
)
_STALE_LEADER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_STALE_TRUMP_FORMER_RE,
     "the current US president is Donald Trump, not a 'former' one"),
    (_STALE_WRONG_POTUS_RE,
     "the current US president is Donald Trump, not Biden"),
)


def stale_leader_spans(text: str) -> list[UnsupportedSpan]:
    """FLAG stale-cutoff current-leader errors in ``text`` (M13).

    Curated + US-only + conservative — at most one span per pattern. Never raises.
    """
    if not text:
        return []
    spans: list[UnsupportedSpan] = []
    for regex, label in _STALE_LEADER_PATTERNS:
        m = regex.search(text)
        if m:
            frag = text[max(0, m.start() - 12): m.end() + 12].strip()
            spans.append(
                _verify().UnsupportedSpan(
                    text=f"stale current-leader reference — {label} (…{frag}…)"[:400],
                    reason=_STALE_LEADER_REASON,
                )
            )
    return spans


# ---------------------------------------------------------------------------
# E-1 (2026-07-27 sweep rec #2) — the FACTS-RECONCILED officeholder guard.
#
# The M13 heuristic above works off a curated regex pair (model-internal world
# knowledge, US-only). This guard is its data-backed sibling: when a finding
# names a person in an officeholder ROLE for a country ("DRC Prime Minister
# <name>", "President <name> of Venezuela"), probe the CURRENT officeholder
# facts (predicate in the head-of-state / head-of-government / leader-of
# family, superseded_by IS NULL AND valid_until IS NULL) and FLAG a mismatch.
#
# HONESTY CONSTRAINTS (load-bearing):
#   * the seed facts can THEMSELVES be stale (known live: the DRC PM row is
#     wrong upstream) — a mismatch DEMOTES/flags via the existing unsupported-
#     span path, NEVER auto-corrects either side;
#   * the reason is ``stale_leader_vs_facts`` (distinct from the heuristic's
#     ``stale_leader``) so calibration can score the two evidence bases apart;
#   * fail-OPEN everywhere: no current fact row for the claimed office → no
#     flag; a claimed person matching ANY current family officeholder (role
#     confusion, co-office) → no flag; a facts read failure → degrade, no flag.
# ---------------------------------------------------------------------------

_STALE_LEADER_VS_FACTS_REASON = "stale_leader_vs_facts"

# Country alias groups: every surface form the extractor recognizes AND every
# candidate ``facts.subject`` spelling probed (lower()-compared). One tuple per
# country so a match on any surface probes all its spellings. Conservative +
# minimal by design (the seeded-world scope), like the M13/M15 maps above.
_OFFICEHOLDER_COUNTRY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("united states", "united states of america", "america", "usa"),
    ("united kingdom", "great britain", "britain"),
    ("russia", "russian federation"),
    ("china", "people's republic of china"),
    ("india",), ("france",), ("germany",), ("italy",), ("japan",),
    ("canada",), ("mexico",), ("brazil",), ("argentina",), ("australia",),
    ("turkey", "turkiye"), ("iran",), ("israel",), ("ukraine",),
    ("saudi arabia",), ("south korea",), ("north korea",), ("south africa",),
    ("indonesia",), ("pakistan",), ("venezuela",), ("slovenia",),
    ("somalia",), ("sudan",), ("egypt",), ("nigeria",), ("poland",),
    ("spain",), ("netherlands",),
    ("democratic republic of the congo", "dr congo", "drc", "congo-kinshasa"),
)
# Uppercase-only acronym surfaces (NEVER matched case-insensitively — "us" /
# "it" / "in" are ordinary English words) → their alias group.
_OFFICEHOLDER_ACRONYMS: dict[str, tuple[str, ...]] = {
    "US": _OFFICEHOLDER_COUNTRY_GROUPS[0],
    "U.S.": _OFFICEHOLDER_COUNTRY_GROUPS[0],
    "USA": _OFFICEHOLDER_COUNTRY_GROUPS[0],
    "UK": _OFFICEHOLDER_COUNTRY_GROUPS[1],
    "DRC": _OFFICEHOLDER_COUNTRY_GROUPS[-1],
}

# Role surface → the facts predicates whose CURRENT row is the reconciliation
# BASIS (canonical lowercase-spaced forms — vocabulary.PREDICATE_CANONICAL).
# "president" maps to BOTH office predicates: seeds store an executive
# president under 'head of government' where a separate head of state exists
# (e.g. Iran), and under 'head of state' elsewhere.
_OFFICEHOLDER_ROLE_PREDICATES: dict[str, tuple[str, ...]] = {
    "president": ("head of state", "head of government"),
    "prime minister": ("head of government",),
    "chancellor": ("head of government",),
    "premier": ("head of government",),
}
_OFFICEHOLDER_FAMILY_PREDICATES: tuple[str, ...] = (
    "head of state", "head of government",
)
_LEADER_OF_PREDICATE = "leader of"

# A qualifier immediately before the match that makes the phrase NOT a
# current-officeholder claim ("former President X" is correct prose about a
# predecessor; "Vice President X" is a different office).
_OFFICEHOLDER_SKIP_QUALIFIER_RE = re.compile(
    r"(?:former|ex|past|previous|then|outgoing|incoming|late|deputy|vice|"
    r"acting|interim)[-\s]+$",
    re.IGNORECASE,
)


def _officeholder_country_alternation() -> str:
    """The country alternation for the extractor regexes: case-insensitive full
    names (longest-first so 'united states of america' beats 'united states')
    plus the uppercase-only acronym branch."""
    names = sorted(
        {n for grp in _OFFICEHOLDER_COUNTRY_GROUPS for n in grp},
        key=len, reverse=True,
    )
    ci = "|".join(re.escape(n) for n in names)
    acro = "|".join(re.escape(a) for a in _OFFICEHOLDER_ACRONYMS)
    return f"(?:(?i:{ci})|(?:{acro}))"


_OFFICEHOLDER_NAME_RE = r"[A-Z][\w'’.\-]+(?:\s+[A-Z][\w'’.\-]+){0,3}"
_OFFICEHOLDER_ROLE_RE = r"(?i:prime\s+minister|president|chancellor|premier)"
_COUNTRY_ALT = _officeholder_country_alternation()

# "<Country>['s] [current|sitting|incumbent|new] <Role> <Name>"
_OFFICEHOLDER_COUNTRY_FIRST_RE = re.compile(
    rf"\b(?P<country>{_COUNTRY_ALT})(?:['’]s)?\s+"
    rf"(?:(?i:current|sitting|incumbent|new)\s+)?"
    rf"(?P<role>{_OFFICEHOLDER_ROLE_RE})\s+"
    rf"(?P<name>{_OFFICEHOLDER_NAME_RE})"
)
# "<Role> <Name> of [the] <Country>"
_OFFICEHOLDER_ROLE_FIRST_RE = re.compile(
    rf"\b(?P<role>{_OFFICEHOLDER_ROLE_RE})\s+"
    rf"(?P<name>{_OFFICEHOLDER_NAME_RE})\s+of\s+(?:(?i:the)\s+)?"
    rf"(?P<country>{_COUNTRY_ALT})(?![a-z0-9])"
)


@dataclass
class OfficeholderClaim:
    """One extracted "<person> holds <role> for <country>" claim."""

    role: str                       # normalized role key (lowercase, spaced)
    person: str                     # the claimed officeholder, as written
    country_surface: str            # the country as written in the prose
    country_aliases: tuple[str, ...]  # candidate facts.subject spellings (lower)


def _country_aliases_for(surface: str) -> tuple[str, ...]:
    if surface in _OFFICEHOLDER_ACRONYMS:
        return _OFFICEHOLDER_ACRONYMS[surface]
    s = surface.casefold()
    for grp in _OFFICEHOLDER_COUNTRY_GROUPS:
        if s in grp:
            return grp
    return (s,)


def extract_officeholder_claims(text: str) -> list[OfficeholderClaim]:
    """PURE lexical extraction of current-officeholder claims (no DB, no LLM).

    Conservative: only the two explicit shapes; a preceding former/ex/vice/…
    qualifier disqualifies the match (correct prose about a predecessor or a
    different office must never enter the probe). De-duplicated on
    (role, country, person). Never raises."""
    if not text:
        return []
    out: list[OfficeholderClaim] = []
    seen: set[tuple[str, str, str]] = set()
    for regex in (_OFFICEHOLDER_COUNTRY_FIRST_RE, _OFFICEHOLDER_ROLE_FIRST_RE):
        for m in regex.finditer(text):
            window = text[max(0, m.start() - 24):m.start()]
            if _OFFICEHOLDER_SKIP_QUALIFIER_RE.search(window):
                continue
            role = re.sub(r"\s+", " ", m.group("role")).strip().casefold()
            if role not in _OFFICEHOLDER_ROLE_PREDICATES:
                continue  # defensive — the alternation and the map must agree
            surface = m.group("country").strip()
            person = m.group("name").strip()
            aliases = _country_aliases_for(surface)
            key = (role, aliases[0], person.casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append(OfficeholderClaim(
                role=role,
                person=person,
                country_surface=surface,
                country_aliases=aliases,
            ))
    return out


#: Honorifics / particles that carry no identity and must never be the ONLY
#: token two names share ("President Lee" vs "President Kim").
_PERSON_NAME_NOISE: frozenset[str] = frozenset(
    {
        "mr", "mrs", "ms", "dr", "sir", "the", "his", "her", "their",
        "president", "prime", "minister", "chancellor", "premier", "excellency",
        "hon", "rt", "van", "von", "der", "den", "del", "della", "bin", "ibn",
        "abu", "al", "el", "de", "da", "dos", "das", "jr", "sr",
    }
)


def _person_name_tokens(name: str) -> set[str]:
    """Diacritic-folded, casefolded name tokens (len ≥ 3) for tolerant person
    matching — 'Janša' matches 'Jansa', 'Donald J. Trump' matches 'Trump'.

    W4 (2026-08-02): POSSESSIVES are stripped, so "Trump's" matches
    "Donald Trump" — the live ``stale_leader_vs_facts`` false positive was
    exactly that, a genuine officeholder reference in the genitive scoring as a
    stale-leader mismatch because ``'`` is a word character to the splitter.
    Honorifics and name particles are dropped for the same reason a shared
    "president" must not make two different people match.
    """
    norm = unicodedata.normalize("NFKD", name or "")
    norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
    out: set[str] = set()
    for raw in re.split(r"[^\w'’\-]+", norm.casefold()):
        # Genitive forms: "trump's" / "trump’s" / a plural-possessive "harris'".
        token = re.sub(r"['’]s\b", "", raw).strip("'’-")
        if len(token) >= 3 and token not in _PERSON_NAME_NOISE:
            out.add(token)
    return out


_CURRENT_OFFICEHOLDER_SQL = """
    SELECT lower(predicate) AS predicate, subject, value
      FROM facts
     WHERE superseded_by IS NULL
       AND valid_until IS NULL
       AND (
             (lower(subject) = ANY($1::text[])
              AND lower(predicate) = ANY($2::text[]))
          OR (lower(predicate) = $3 AND lower(value) = ANY($1::text[]))
           )
"""

#: At most this many facts-reconciled spans per finding (bounded, skimmable).
_STALE_VS_FACTS_MAX_SPANS = 4
#: At most this many extracted claims probed per finding (bounds the queries).
_STALE_VS_FACTS_MAX_CLAIMS = 8


async def stale_leader_vs_facts_spans(
    conn: Any, text: str,
) -> list[UnsupportedSpan]:
    """FLAG officeholder claims that contradict the CURRENT facts-table row.

    For each extracted claim, reads the country's OPEN officeholder facts
    (country-subject 'head of state'/'head of government' rows + person-subject
    'leader of' rows; ``superseded_by IS NULL AND valid_until IS NULL``) and
    emits a ``stale_leader_vs_facts`` span when the claimed office has a
    current fact naming someone else AND the claimed person matches NO current
    family officeholder. Fail-open + degrade-not-drop throughout; never raises.
    """
    try:
        claims = extract_officeholder_claims(text)
    except Exception as exc:  # pragma: no cover — pure path, defensive only
        logger.warning("verify.stale_leader_vs_facts.extract_failed err=%s", exc)
        return []
    spans: list[UnsupportedSpan] = []
    for claim in claims[:_STALE_VS_FACTS_MAX_CLAIMS]:
        try:
            rows = await conn.fetch(
                _CURRENT_OFFICEHOLDER_SQL,
                list(claim.country_aliases),
                list(_OFFICEHOLDER_FAMILY_PREDICATES),
                _LEADER_OF_PREDICATE,
            )
        except Exception as exc:  # degrade-not-drop — facts read must not block
            logger.warning(
                "verify.stale_leader_vs_facts.read_failed country=%s err=%s",
                claim.country_surface, exc,
            )
            return spans
        role_preds = set(_OFFICEHOLDER_ROLE_PREDICATES[claim.role])
        basis = sorted({
            str(r["value"]) for r in rows
            if r["predicate"] in role_preds and r["value"]
        })
        if not basis:
            continue  # no CURRENT fact for the claimed office — fail-open
        holders = [
            str(r["value"]) for r in rows
            if r["predicate"] in _OFFICEHOLDER_FAMILY_PREDICATES and r["value"]
        ] + [
            str(r["subject"]) for r in rows
            if r["predicate"] == _LEADER_OF_PREDICATE and r["subject"]
        ]
        claimed_tokens = _person_name_tokens(claim.person)
        if not claimed_tokens:
            continue
        if any(claimed_tokens & _person_name_tokens(h) for h in holders):
            continue  # matches a current family officeholder — consistent
        spans.append(_verify().UnsupportedSpan(
            text=(
                f"officeholder mismatch vs facts — the finding names "
                f"{claim.person!r} as {claim.role} of {claim.country_surface}, "
                f"but the current open officeholder fact(s) name "
                f"{', '.join(basis[:3])}. Flag-only: the seed facts can "
                f"themselves be stale — never auto-corrected"
            )[:400],
            reason=_STALE_LEADER_VS_FACTS_REASON,
        ))
        if len(spans) >= _STALE_VS_FACTS_MAX_SPANS:
            break
    return spans


def cross_target_leak_span(
    *, title: str, body: str, target_id: str | None,
) -> UnsupportedSpan | None:
    """FLAG a per-country finding whose named subject-country contradicts its desk
    (M15): it names a DIFFERENT country and NEVER its own target geo.

    Conservative fail-OPEN (mirrors :func:`grounding.finding_is_off_target`): a
    finding that mentions its own country anywhere, or that names no country at
    all, is NOT flagged. Non-country / unmapped desks are never flagged."""
    slug = _country_desk_slug(target_id)
    if slug is None:
        return None
    # Build the own-mention set from ONLY the country NAME tokens — NEVER the bare
    # ISO-2 slug. A slug such as 'in' (India), 'it' (Italy), 'us' (US), 'id'
    # (Indonesia) is a common English word that _mentions_country would match in
    # normal prose, firing the on-target early-return on EVERY finding and silently
    # disabling the guard for those desks. Fail-OPEN when the desk has no country-
    # NAME mapping (an unmapped slug): we cannot tell its own country → never flag.
    own = {n.casefold() for n in _TARGET_SLUG_TO_COUNTRY.get(slug, ())}
    if not own:
        return None
    haystack_lc = f"{title}\n{body}".casefold()
    # V-I2 (2026-08-05): three surfaces, not one. The name, its DEMONYM (a US
    # desk writing "American outlets" names its own country), and the
    # CASE-SENSITIVE abbreviations a casefolded set cannot hold — bare "US" is
    # the pronoun "us" once you lower it. 08-04 rec #4, and 100% of the 08-05
    # `cross_target_leak` class was a finding that said "US" six times.
    if _mentions_own_country(slug, own, title, body):
        return None  # on-target — mentions its own geo somewhere
    others = {c for c in _COUNTRY_TOKENS if c not in own}
    # SYMMETRY: the other-country arm reads demonyms too, so the tolerance
    # cannot skew the on-target / off-target decision in one direction only.
    named = sorted(c for c in others if _names_country(c, haystack_lc))
    if not named:
        return None  # names no country at all — generic/thin, not off-target
    return _verify().UnsupportedSpan(
        text=(
            f"cross-target leak — desk target '{target_id}' but the finding names "
            f"only other countries ({', '.join(named[:5])}) and never its own"
        )[:400],
        reason=_CROSS_TARGET_REASON,
    )
