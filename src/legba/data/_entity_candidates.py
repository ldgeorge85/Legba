# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Blocking + candidate generation for the entity_researcher (E3).

WHY THIS MODULE EXISTS
----------------------
The entity_researcher (E4) must not compare all 22k ``entity_profiles`` rows
pairwise (2.4e8 pairs). The Splink / OpenSanctions doctrine: BLOCK on cheap
deterministic keys, adjudicate only the survivors. This module is the BLOCKING
half — it emits a bounded, ranked list of candidate MERGE pairs using three
signals, all backed by indexes from migration 0088:

  1. EXACT block key — ``entity_block_key(canonical_name)`` (unaccent + lower +
     strip-honorific + DISTINCT-tokens-sorted). "Ali Khamenei" and "Ayatollah
     Ali Khamenei" share it; "Mojtaba Khamenei" does NOT (father/son never
     auto-block). A functional btree indexes it.
  2. TRIGRAM similarity — ``similarity(lower(name), lower(name)) >= min_trgm``,
     GIN-accelerated (``%``), for typo / phonetic near-misses the exact key
     misses ("Netanyahu" / "Netanyahoo").

Each pair is SCOPED (same-or-compatible ``entity_class``, no ``geo_country``
conflict), HARD-NEGATIVE-filtered (junk fragment; incompatible class; geo
clash), and BANDED. **This module never merges** — the ``band`` is a SUGGESTION
the entity_researcher (E4) may fast-path or send to the LLM. E4 is the sole
merge authority (tombstone + redirect, reversible, ledgered — MP:DEC-B).

BANDS
-----
* ``auto_merge`` — exact MULTI-token block key, same SPECIFIC class (not the
  generic ``entity`` bucket), no geo conflict. The highest-precision signal
  (a shared 2+-token normalized key is rarely a coincidence). E4 may confirm
  these deterministically (``decided_by='deterministic'``) — still reversible.
* ``gray`` — everything else that survives the hard negatives: single-token
  exact keys, any ``entity``-bucket pairing, and all trigram-only matches.
  These REQUIRE the E4 LLM adjudicator (a single-token "atlantic" or a fuzzy
  match can be two distinct referents).

This module lives at ``legba.data`` (sibling of ``_entity_canon`` /
``_entity_resolve``); it imports only ``_entity_canon.is_junk_entity`` (pure) +
stdlib + an asyncpg-shaped ``conn``. No ``legba.data.analysts.*`` import
(the canon layering rule).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ._entity_canon import is_junk_entity

logger = logging.getLogger(__name__)

# Class pairs a MERGE candidate may span. Mirrors the E1 fallback doctrine:
# identical, or the org/corporation equivalence. country<->location is a
# deliberate keep-distinct ambiguity (absent here => incompatible). The generic
# ``entity`` bucket is compatible with anything (a class-less fragment may fold
# into a typed keeper) — but such a pair is always GRAY (LLM-adjudicated), never
# auto_merge.
_MERGE_COMPATIBLE_PAIRS: frozenset[frozenset[str]] = frozenset(
    {frozenset({"organization", "corporation"})}
)
#: Specific (non-generic) classes eligible for the deterministic auto_merge band.
_SPECIFIC_CLASSES: frozenset[str] = frozenset(
    {"country", "organization", "corporation", "location", "person"}
)

DEFAULT_MIN_TRGM = 0.55
DEFAULT_LIMIT = 500


def _class_compatible(a: str | None, b: str | None) -> bool:
    """True when two entity classes may be the SAME referent (merge-eligible)."""
    la = (a or "").strip().lower()
    lb = (b or "").strip().lower()
    if la in ("", "entity") or lb in ("", "entity"):
        return True  # a class-less/generic side can fold into anything (GRAY)
    if la == lb:
        return True
    return frozenset({la, lb}) in _MERGE_COMPATIBLE_PAIRS


def _geo_conflict(a: str | None, b: str | None) -> bool:
    """True only when BOTH sides carry a geo_country and they differ."""
    la = (a or "").strip().upper()
    lb = (b or "").strip().upper()
    return bool(la) and bool(lb) and la != lb


# Honorific/title/article tokens dropped before the ORDER-SENSITIVE auto_merge
# check (mirrors the entity_block_key strip in migration 0088). Kept as WHOLE
# tokens.
_ORDERED_HONORIFICS: frozenset[str] = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sir", "gen", "col", "lt", "sgt", "sen",
    "rep", "hon", "president", "pres", "minister", "ayatollah", "sheikh",
    "imam", "rabbi", "the",
})
_ORDERED_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _ordered_tokens(name: str) -> tuple[str, ...]:
    """Honorific-stripped token SEQUENCE, preserving ORDER + duplicates.

    Unlike ``entity_block_key`` (which sorts a DISTINCT set — the anagram hole),
    this keeps order so "Ali Khamenei" == "Ayatollah Ali Khamenei" (honorific
    dropped) but "Mohammed bin Salman" != "Salman bin Mohammed" (a patronymic
    reversal — two distinct people) and "Congo Republic" != "Republic Congo".
    """
    toks = _ORDERED_PUNCT_RE.sub(" ", str(name or "").lower()).split()
    return tuple(t for t in toks if t and t not in _ORDERED_HONORIFICS)


@dataclass(frozen=True)
class CandidatePair:
    """One candidate merge pair. Ordered so ``left`` is the deterministic-lower
    id (stable ``pair_key``); ``band`` is a SUGGESTION for E4, never a decision."""

    left_id: str
    left_name: str
    left_class: str
    right_id: str
    right_name: str
    right_class: str
    band: str  # 'auto_merge' | 'gray'
    score: float  # 0..1, higher = more likely the same referent (ranking)
    signals: tuple[str, ...]  # e.g. ('exact_block_key',) or ('trgm:0.78',)
    block_key: str

    @property
    def pair_key(self) -> str:
        """Order-independent key for dedup / the entity_judgement cache."""
        lo, hi = sorted((self.left_id, self.right_id))
        return f"{lo}::{hi}"


# Active-keeper guard (mirrors the 0088 partial indexes + the E5 merged_into
# tombstone). ``{p}`` is a table alias so the guard qualifies cleanly in a join.
def _active(p: str = "") -> str:
    q = f"{p}." if p else ""
    return (
        f"COALESCE({q}data->>'gc_status', '') NOT IN ('merged', 'junk') "
        f"AND {q}merged_into IS NULL"
    )


_ACTIVE = _active()

_EXACT_SQL = f"""
WITH keyed AS (
    SELECT id, canonical_name, entity_class, geo_country,
           entity_block_key(canonical_name) AS bk
      FROM entity_profiles
     WHERE {_ACTIVE}
       AND entity_block_key(canonical_name) <> ''
)
SELECT a.id AS aid, a.canonical_name AS aname, a.entity_class AS acls,
       a.geo_country AS ageo,
       b.id AS bid, b.canonical_name AS bname, b.entity_class AS bcls,
       b.geo_country AS bgeo,
       a.bk AS bk,
       array_length(string_to_array(a.bk, ' '), 1) AS ntok
  FROM keyed a
  JOIN keyed b ON a.bk = b.bk AND a.id < b.id
 ORDER BY a.bk
 LIMIT $1
"""

_TRGM_SQL = f"""
SELECT a.id AS aid, a.canonical_name AS aname, a.entity_class AS acls,
       a.geo_country AS ageo,
       b.id AS bid, b.canonical_name AS bname, b.entity_class AS bcls,
       b.geo_country AS bgeo,
       similarity(lower(a.canonical_name), lower(b.canonical_name)) AS sim
  FROM entity_profiles a
  JOIN entity_profiles b
    ON a.id < b.id
   AND lower(b.canonical_name) % lower(a.canonical_name)
   AND a.entity_class = b.entity_class
 WHERE {_active("a")}
   AND {_active("b")}
   AND similarity(lower(a.canonical_name), lower(b.canonical_name)) >= $1
 ORDER BY sim DESC
 LIMIT $2
"""


def _accept(aname: str, bname: str, acls: str, bcls: str,
            ageo: str | None, bgeo: str | None) -> bool:
    """Hard-negative gate shared by both probes."""
    if not aname or not bname:
        return False
    if is_junk_entity(aname) or is_junk_entity(bname):
        return False  # a junk fragment is E6's prune target, never a merge side
    if not _class_compatible(acls, bcls):
        return False
    if _geo_conflict(ageo, bgeo):
        return False
    return True


async def generate_candidates(
    conn,
    *,
    min_trgm: float = DEFAULT_MIN_TRGM,
    exact_limit: int = DEFAULT_LIMIT,
    trgm_limit: int = DEFAULT_LIMIT,
) -> list[CandidatePair]:
    """Emit deduped, ranked candidate merge pairs over the ACTIVE keeper set.

    Runs the exact-block-key probe then the trigram probe (both index-backed by
    migration 0088), applies the hard-negative gate, bands each survivor, and
    dedups by ``pair_key`` (exact wins over trgm for the same pair). Ordered by
    ``score`` descending (auto_merge candidates first). Pure read — never
    mutates, never raises for a normal empty result.
    """
    out: dict[str, CandidatePair] = {}

    # 1) EXACT block-key pairs.
    for r in await conn.fetch(_EXACT_SQL, int(exact_limit)):
        aname, bname = str(r["aname"] or ""), str(r["bname"] or "")
        acls, bcls = str(r["acls"] or ""), str(r["bcls"] or "")
        if not _accept(aname, bname, acls, bcls, r["ageo"], r["bgeo"]):
            continue
        ntok = int(r["ntok"] or 1)
        # auto_merge is the ONLY LLM-bypassing band, so it is deliberately narrow
        # (adversarial review CRITICAL): a shared block key is order-INSENSITIVE
        # (sorted DISTINCT tokens), so an anagram / patronymic-reversal collides
        # ("Mohammed bin Salman" vs "Salman bin Mohammed"; "Congo Republic" vs
        # "Republic Congo"). auto_merge therefore ALSO requires (a) an ORDER-
        # sensitive token-sequence match (so only honorific/article variants of
        # the SAME ordered name qualify) and (b) NEITHER side is a PERSON (name
        # permutation is meaningful for people — all person merges go to the LLM).
        # Everything else is GRAY (adjudicated).
        specific = (
            acls.lower() in _SPECIFIC_CLASSES and bcls.lower() in _SPECIFIC_CLASSES
        )
        person_involved = "person" in (acls.lower(), bcls.lower())
        ordered_match = _ordered_tokens(aname) == _ordered_tokens(bname)
        band = (
            "auto_merge"
            if (ntok >= 2 and specific and ordered_match and not person_involved)
            else "gray"
        )
        pair = CandidatePair(
            left_id=str(r["aid"]), left_name=aname, left_class=acls,
            right_id=str(r["bid"]), right_name=bname, right_class=bcls,
            band=band,
            score=1.0 if band == "auto_merge" else 0.80,
            signals=("exact_block_key",),
            block_key=str(r["bk"] or ""),
        )
        out[pair.pair_key] = pair

    # 2) TRIGRAM pairs (always GRAY — a fuzzy match needs the adjudicator).
    #    OPT-IN + bounded: the current trgm self-join is an un-blocked full scan
    #    (~61s over 22k rows live — the review's perf finding), too slow for the
    #    actor cadence. Callers pass trgm_limit<=0 to SKIP it (exact-block-key
    #    only — fast + high-precision); a proper per-record top-K trgm blocking
    #    (E3.1) will re-enable recall. Tests/CLI still pass a positive limit.
    for r in ([] if int(trgm_limit) <= 0
              else await conn.fetch(_TRGM_SQL, float(min_trgm), int(trgm_limit))):
        aname, bname = str(r["aname"] or ""), str(r["bname"] or "")
        acls, bcls = str(r["acls"] or ""), str(r["bcls"] or "")
        if not _accept(aname, bname, acls, bcls, r["ageo"], r["bgeo"]):
            continue
        left_id, right_id = str(r["aid"]), str(r["bid"])
        pair_key = "::".join(sorted((left_id, right_id)))
        if pair_key in out:
            # exact already emitted this pair — keep it, just note the signal
            existing = out[pair_key]
            out[pair_key] = CandidatePair(
                left_id=existing.left_id, left_name=existing.left_name,
                left_class=existing.left_class, right_id=existing.right_id,
                right_name=existing.right_name, right_class=existing.right_class,
                band=existing.band, score=existing.score,
                signals=existing.signals + (f"trgm:{float(r['sim']):.2f}",),
                block_key=existing.block_key,
            )
            continue
        sim = float(r["sim"] or 0.0)
        out[pair_key] = CandidatePair(
            left_id=left_id, left_name=aname, left_class=acls,
            right_id=right_id, right_name=bname, right_class=bcls,
            band="gray", score=min(0.79, sim),  # cap below the exact single-token 0.80
            signals=(f"trgm:{sim:.2f}",),
            block_key="",
        )

    return sorted(out.values(), key=lambda p: p.score, reverse=True)


__all__ = ["CandidatePair", "generate_candidates", "DEFAULT_MIN_TRGM"]
