# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keeper election for a nexus/fact endpoint surface — canonicalize-AT-WRITE (E1).

WHY THIS MODULE EXISTS
----------------------
:mod:`legba.data._entity_canon` normalizes an entity SURFACE (HTML-strip, alias
map, demonym→country, junk gate) but it is deliberately PURE — no DB connection,
no ``legba.data.analysts.*`` import (its hard layering rule, ``_entity_canon.py``
lines 16-18). So ``canonicalize_entity`` can turn ``"Israeli"`` into ``"Israel"``,
but it CANNOT know that the fragment ``"SNSC"`` folds onto the elected
``entity_profiles`` keeper "Supreme National Security Council", nor that 30
Khamenei surface variants all belong to one keeper. That resolution needs the
live ``entity_profiles`` table.

The two nexus producers (``relationship_reifier`` and
``proposed_edge_governance``) write endpoint STRINGS straight from
``proposed_edges`` — canonicalized only by ``canonicalize_entity``. So junk
fragments become distinct graph actors and junk edges get minted
(``Resistance —hostile to→ United States``).

THE FIX
-------
:func:`resolve_keeper` runs the SAME keeper-election probe the ingestion resolver
already uses (``entity_resolution.py`` lines 561-609): the any-class exact-name
pre-lookup, then the M4 article/alias-aware fallback (lookup_key normalization +
``data->merged_aliases`` containment). It elects the highest-priority ACTIVE
keeper row and returns its ``canonical_name`` — so the producer can REWRITE the
endpoint to the keeper's surface BEFORE ``write_nexus``. Fragments converge onto
one graph actor instead of forking.

This module lives at ``legba.data`` (a sibling of ``_entity_canon``) so both
producers can import it without a layering violation, and it takes a ``conn``
(which the canon must not). It depends only on ``_entity_canon.lookup_key`` (a
pure sibling) + stdlib + asyncpg-shaped ``conn``.

DEGRADE-NOT-BREAK CONTRACT
--------------------------
:func:`resolve_keeper` NEVER raises and NEVER drops. On any error, empty input,
or no keeper match it returns the input ``name`` unchanged. A resolution failure
on one endpoint must never sink the batch or lose the edge — the caller writes
the un-rewritten (but still canonicalized) endpoint, exactly as today.
"""

from __future__ import annotations

import logging
from typing import Any

from ._entity_canon import lookup_key

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Class priority — a keeper election prefers a country row over an organization
# over a location over a person over the generic ``entity`` bucket, tie-broken
# by oldest ``created_at``. This mirrors ``entity_resolution._CLASS_PRIORITY_SQL``
# BYTE-FOR-BYTE; it is re-stated here (not imported) so this module stays free of
# any ``legba.data.analysts.*`` dependency (the same layering rule the canon
# obeys). ``corporation`` shares the ``organization`` rank.
# ---------------------------------------------------------------------------
_CLASS_PRIORITY_SQL = (
    "CASE entity_class "
    "WHEN 'country' THEN 0 "
    "WHEN 'organization' THEN 1 "
    "WHEN 'corporation' THEN 1 "
    "WHEN 'location' THEN 2 "
    "WHEN 'person' THEN 3 "
    "WHEN 'entity' THEN 4 "
    "ELSE 5 END"
)

# ---------------------------------------------------------------------------
# FALLBACK class-guard (E1 adversarial #1). A normalized / alias probe can
# collide two DISTINCT referents that merely normalize to the same key ("the
# Atlantic" the magazine vs "Atlantic" the ocean; "the Sun"/"the Post"). The
# ingestion resolver folds a fallback match ONLY when the class is compatible
# (entity_resolution._fallback_class_compatible, lines 619-637); it treats an
# incompatible cross-class pairing as a distinct entity. resolve_keeper mirrors
# that — but with one deliberate widening: a CLASS-LESS endpoint ("entity" / no
# class, the E1 raison d'être of a bare fragment 'SNSC' electing an
# 'organization' keeper) can never be declared incompatible, so it always folds.
# The guard therefore stays DORMANT for today's callers (both pass "entity") and
# only bites when a future caller supplies a real class — closing the "E1 is
# less safe than the resolver it copies" gap without breaking the SNSC fold.
# Re-stated (not imported) to keep this module free of legba.data.analysts.*.
# ---------------------------------------------------------------------------
_FALLBACK_COMPATIBLE_PAIRS: frozenset[frozenset[str]] = frozenset({
    frozenset({"organization", "corporation"}),
})
#: An endpoint with no meaningful class — the fallback guard does not apply.
_CLASSLESS_ENDPOINT = frozenset({"", "entity"})


def _fallback_class_ok(stored_cls: str, cls: str | None) -> bool:
    """Whether a FALLBACK-probe match of a keeper of ``stored_cls`` may fold an
    endpoint declared ``cls``.

    Only the alias/normalized fallback is guarded — an EXACT-surface match is the
    same referent by construction and always folds. Compatible = a class-less
    endpoint (always), an identical class, or an explicitly-equivalent pair
    (organization/corporation). Every other cross-class pairing (incl. the
    country↔location ambiguity, absent from the pair set) is a distinct referent.
    """
    low = (cls or "").strip().lower()
    if low in _CLASSLESS_ENDPOINT:
        return True
    if low == stored_cls:
        return True
    return frozenset({stored_cls, low}) in _FALLBACK_COMPATIBLE_PAIRS

# Exact-name keeper probe. Mirrors entity_resolution.py:561-569 — the FAST path
# on ``idx_entity_profiles_name_class``, excluding gc merged/junk losers. Also
# returns the class priority so the caller can compare it against an exact-ALIAS
# keeper (the E1.1 override below).
_EXACT_SQL = f"""
    SELECT id, entity_class, canonical_name, {_CLASS_PRIORITY_SQL} AS prio
      FROM entity_profiles
     WHERE lower(canonical_name) = lower($1)
       AND COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk')
     ORDER BY {_CLASS_PRIORITY_SQL}, created_at ASC
     LIMIT 1
"""

# Exact-ALIAS keeper probe (E1.1 — the aliased-fragment-leak fix). A bare
# fragment can exist as its OWN active ``entity_profiles`` row (class 'entity')
# while a higher-class keeper already claims the SAME surface as a merged_alias
# (the 'SNSC' entity-row vs the 'Supreme National Security Council' organization
# that lists "SNSC" in ``merged_aliases``). The exact-canonical probe above
# returns the FRAGMENT and short-circuits before the normalized fallback can find
# the keeper. This probe finds the DIFFERENT, higher-priority keeper that has
# ALREADY claimed the surface as an alias, so the caller can prefer it. It
# matches the surface EXACTLY (case/whitespace-insensitive, NOT article-stripped
# — that stays the normalized fallback's job) and excludes the fragment's own row
# (``canonical_name <> surface``). ``$1`` is the RAW incoming surface.
_EXACT_ALIAS_SQL = f"""
    SELECT id, entity_class, canonical_name, {_CLASS_PRIORITY_SQL} AS prio
      FROM entity_profiles
     WHERE COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk')
       AND lower(btrim(canonical_name)) <> lower(btrim($1))
       AND EXISTS (
         SELECT 1
           FROM jsonb_array_elements_text(
               CASE WHEN jsonb_typeof(data->'merged_aliases') = 'array'
                    THEN data->'merged_aliases' ELSE '[]'::jsonb END
           ) AS al
          WHERE lower(btrim(al)) = lower(btrim($1))
       )
     ORDER BY {_CLASS_PRIORITY_SQL}, created_at ASC
     LIMIT 1
"""

# Article/alias-aware fallback probe. Mirrors entity_resolution.py:584-609 —
# article/case/whitespace-normalize BOTH sides (same rule as ``lookup_key``) and
# also probe each keeper's ``merged_aliases`` so a folded surface converges onto
# the ACTIVE keeper. ``$1`` is the ``lookup_key``-normalized incoming name.
_ALIAS_SQL = f"""
    SELECT id, entity_class, canonical_name
      FROM entity_profiles
     WHERE COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk')
       AND (
         regexp_replace(regexp_replace(
             lower(btrim(canonical_name)),
             '^(the|a|an)\\s+', '', 'g'),
             '\\s+', ' ', 'g') = $1
         OR EXISTS (
           SELECT 1
             FROM jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(data->'merged_aliases') = 'array'
                      THEN data->'merged_aliases' ELSE '[]'::jsonb END
             ) AS al
            WHERE regexp_replace(regexp_replace(
                lower(btrim(al)),
                '^(the|a|an)\\s+', '', 'g'),
                '\\s+', ' ', 'g') = $1
         )
       )
     ORDER BY {_CLASS_PRIORITY_SQL}, created_at ASC
     LIMIT 1
"""


async def resolve_keeper(
    conn: Any,
    name: str,
    *,
    entity_class: str | None = None,
    geo: str | None = None,
    cache: dict[str, str] | None = None,
) -> str:
    """Resolve ``name`` to its elected ``entity_profiles`` keeper's canonical_name.

    Runs the SAME keeper-election probe the ingestion resolver uses
    (``entity_resolution.py`` lines 561-609), with an E1.1 override:

      1. exact ``lower(canonical_name)`` match (the fast index path), then
      1b. E1.1 exact-ALIAS override — if that exact hit is itself a bare
          class-less ``entity`` FRAGMENT and a higher class-priority keeper
          already claims the same exact surface as a ``merged_alias``, prefer
          that keeper, then
      2. the M4 article/case/whitespace-normalized fallback + a
         ``data->merged_aliases`` containment probe (only when 1 missed).

    In every probe ``gc_status`` ``'merged'`` / ``'junk'`` rows are excluded (a
    de-fragmentation LOSER must never be elected), and the highest class-priority
    ACTIVE row wins (tie → oldest ``created_at``).

    The E1.1 override (step 1b) closes the aliased-fragment leak: a bare fragment
    that ALSO exists as its own active row (e.g. ``SNSC`` class ``entity``) used
    to short-circuit at step 1 and resolve to itself, never folding to the
    higher-class keeper (``Supreme National Security Council``, ``organization``)
    that already lists it as an alias. TWO guards keep it precise: (a) the exact
    hit must be a bare ``entity`` / class-less FRAGMENT — a properly-classified
    exact row (a ``location`` ``Palmyra``, a ``person`` ``Khamenei``, a
    ``country``) is a real, deliberately-distinct entity and is NEVER blind-folded
    even if some keeper accreted its surface as an alias (that ambiguity is the
    LLM researcher's call); and (b) ``_fallback_class_ok`` on the alias side, so a
    future class-bearing endpoint can't fold across an incompatible keeper class.
    ``geo`` is accepted for signature parity / future scoping; it is not yet a
    probe filter (endpoint geo is unreliable).

    Returns the keeper's ``canonical_name`` on a match, or the input ``name``
    unchanged when there is no keeper, the input is empty, or ANY error occurs.
    NEVER raises, NEVER returns empty for a non-empty input (degrade-not-break).
    """
    original = name
    try:
        text = str(name or "").strip()
        if not text:
            return original

        cls = (str(entity_class).strip() or None) if entity_class else None

        # Per-cycle memo (a caller-owned dict, fresh each run). The fallback probe
        # is an un-indexed scan (~137ms/miss) and endpoints repeat heavily within
        # one sweep ('United States' recurs across dozens of edges; governance can
        # issue up to 400 calls/run), so memoize the resolved surface. Key on both
        # inputs so a class-scoped call never reads a class-blind entry.
        ckey = f"{text.lower()}\x00{cls or ''}" if cache is not None else None
        if ckey is not None and ckey in cache:
            return cache[ckey]

        # 1) EXACT-canonical probe (any-class, by priority). The best-priority
        #    ACTIVE row whose canonical_name IS the surface.
        row = await conn.fetchrow(_EXACT_SQL, text)

        # 1b) EXACT-ALIAS override (E1.1): the exact-canonical hit can be a bare
        #     GENERIC FRAGMENT ('entity' class — an unclassified NER leak like
        #     'SNSC') while a higher-class keeper already claims the same surface
        #     as a merged_alias (the org that lists "SNSC"). Fold the fragment onto
        #     that keeper. GUARDED to fire ONLY when the exact hit is itself a
        #     class-less 'entity' fragment: a PROPERLY-CLASSIFIED exact row
        #     (a 'location' Palmyra, a 'person' Khamenei, a 'country') is a real,
        #     deliberately-distinct entity and is NEVER blind-folded here even if
        #     some keeper accreted its surface as an alias — that ambiguity is the
        #     LLM researcher's call, not a write-time rewrite (adversarial-review
        #     precision fix). The alias side is ALSO _fallback_class_ok-guarded so
        #     a future class-bearing endpoint can't fold across an incompatible
        #     keeper class.
        exact_cls = (str(row["entity_class"] or "").strip().lower()
                     if row is not None else None)
        if row is not None and exact_cls in _CLASSLESS_ENDPOINT:
            alias_row = await conn.fetchrow(_EXACT_ALIAS_SQL, text)
            if (
                alias_row is not None
                and int(alias_row["prio"]) < int(row["prio"])
                and _fallback_class_ok(
                    str(alias_row["entity_class"] or "").strip().lower(), cls)
            ):
                row = alias_row

        # 2) ALIAS/ARTICLE fallback — only when the exact probe missed.
        via_fallback = False
        if row is None:
            probe = lookup_key(text)
            if probe:
                row = await conn.fetchrow(_ALIAS_SQL, probe)
                via_fallback = row is not None

        if row is None:
            if ckey is not None:
                cache[ckey] = original
            return original

        # FALLBACK class-guard (E1 adversarial #1): a normalized/alias match can
        # collide two DISTINCT referents that merely normalize the same — fold
        # only when the endpoint class is class-less or compatible with the
        # keeper's. Dormant for today's "entity" callers; see _fallback_class_ok.
        if via_fallback:
            stored_cls = str(row["entity_class"] or "").strip().lower()
            if not _fallback_class_ok(stored_cls, cls):
                if ckey is not None:
                    cache[ckey] = original
                return original

        # Never rewrite to an empty / whitespace keeper surface.
        keeper = str(row["canonical_name"] or "").strip() or original
        if ckey is not None:
            cache[ckey] = keeper
        return keeper
    except Exception as exc:  # pragma: no cover - defensive; degrade-not-break
        logger.warning(
            "resolve_keeper.degraded name=%r class=%r err=%s",
            name, entity_class, exc,
        )
        return original


__all__ = ["resolve_keeper"]
