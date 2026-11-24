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

THE MATCHING CONTRACT (P-6, written down 2026-11-14; both lookup paths obey it)
------------------------------------------------------------------------------
Input. ``text = str(name).strip()`` (Python whitespace); empty -> ``name``
unchanged. ``cls`` = the stripped ``entity_class`` or None.

Active row. ``COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk')``
AND ``merged_into IS NULL``; ``dormant`` is active.

Merged-away rows are FOLLOWED (roll 85). A profile with ``merged_into`` set
was merged into another; until roll 85 only ``gc_status`` was read, and 1,451
such tombstones (``gc_status`` 'dormant', 2026-11-15) stayed electable — 891 of
the 74,565 ingestion links of the week before landed on 119 of them. Now a
row MATCHES as before (any row but ``junk``), and is replaced by the end of
its ``merged_into`` chain (at most :data:`FOLLOW_MAX_HOPS` hops; the live
chains are at most 4 deep); that keeper is elected if it is active, ranked by
the KEEPER's priority and age. A ``gc_status='merged'`` row with no pointer
ends at itself and stays unelectable, as before. Both paths (scan and key
table, single and batch) and the ingestion resolver's own copies run the same
rule (:data:`FOLLOW_SQL`).

Priority (of the elected keeper). country 0 < organization = corporation 1 < location 2 < person 3 <
entity 4 < any other class 5; then the OLDEST ``created_at``. The scan stops
there, so two rows equal on (priority, created_at) are returned in whatever
order Postgres reads them (non-deterministic). The key-table path adds a
final ``id ASC`` so the same rule always elects the same row.

Aliases. ``data->'merged_aliases'`` when it is a jsonb ARRAY; anything else
counts as no aliases. Elements are read with ``jsonb_array_elements_text``
(a non-string element compares by its JSON text, a JSON null never matches).

Folding. Postgres ``lower()`` under the database collation and ``btrim``
(spaces only). NO diacritic folding (``unaccent`` is not used), no
punctuation folding, no transliteration, NO fuzzy or prefix step.

  1. EXACT: ``lower(canonical_name) = lower(text)`` -> best active row
     (``_EXACT_SQL``; already index-backed by ``idx_entity_profiles_name_class``;
     not changed by P-6).
  1b. EXACT-ALIAS override, only when the step-1 row's class is '' or
     'entity': best active row with an alias where
     ``lower(btrim(alias)) = lower(btrim(text))`` and
     ``lower(btrim(canonical_name)) <> lower(btrim(text))``; it replaces the
     step-1 row iff its priority is strictly better AND
     ``_fallback_class_ok(its class, cls)``.
  2. NORMALIZED fallback, only when step 1 found nothing:
     ``probe = lookup_key(text)`` (Python: collapse whitespace, lower, strip
     one leading the/a/an); if non-empty, best active row where
     ``norm(canonical_name) = probe`` OR ``norm(alias) = probe`` for some alias,
     ``norm(x) = regexp_replace(regexp_replace(lower(btrim(x)),
     '^(the|a|an)\s+', '', 'g'), '\s+', ' ', 'g')``; folded only when
     ``_fallback_class_ok(its class, cls)``.
  Result: the elected row's stripped ``canonical_name``, or ``name``.

The ingestion resolver (``entity_resolution._resolve_batch``) runs step 1,
then step 2's predicate (its copy reads ``COALESCE(data->'merged_aliases',
'[]')``, which RAISES on a non-array value where this module's copy reads no
aliases; 0 such rows live on 2026-11-14), then its own index-backed person
transliteration probe, which P-6 does not touch.

How aliases change. Ingestion unions folded surfaces into a keeper's
``merged_aliases`` (``_record_provenance``); the entity researcher's merge
moves a loser's name + aliases onto the keeper and stamps the loser
``gc_status='merged'``; Consult entity writes create, edit, merge and split
(``registry/consult_write_entity_apply.py``); entity_gc retires rows by
gc_status and deletes. Every one of them is a write to ``entity_profiles``.

THE KEY TABLE (migration 0263) AND THE SWITCH
---------------------------------------------
Steps 1b and 2 used to read every ``entity_profiles`` row per call (243k rows,
up to 0.57 s). ``entity_alias_keys(key_kind, key, entity_id)`` holds, for each
profile, ``('alias_lower', lower(btrim(alias)))`` and ``('norm', norm(x))`` for
its canonical name and aliases, computed by the SAME SQL expressions in the
same database, and rewritten by trigger in the same transaction as every
profile insert and every update of ``canonical_name`` / ``merged_aliases``.
The active filter and the tie-break are read from ``entity_profiles`` by
primary key at lookup time, so a class change or retirement cannot go stale.

``LEGBA_ENTITY_ALIAS_LOOKUP`` (read per call):
  * ``scan`` (unset, default): exactly the statements this module ran before
    P-6, in the same order.
  * ``shadow``: runs the scan AND the key table for steps 1b/2, SERVES THE
    SCAN'S ANSWER, and logs every disagreement (entity ids and a class, never
    names) plus a running summary with counts and per-path milliseconds.
  * ``table``: serves the key table.
A probe longer than ``_TABLE_PROBE_MAX_BYTES`` is always served by the scan
(the table does not store keys over 2000 bytes), so the cap changes no answer.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable

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

#: The deepest ``merged_into`` chain followed (live max 4, 2026-11-15).
FOLLOW_MAX_HOPS = 8

#: Roll 85 — a matched row ``p`` replaced by the end of its ``merged_into``
#: chain ``k`` (``p`` itself when it was never merged away). Used verbatim by
#: every election statement, here and in the ingestion resolver. The keeper's
#: columns come out of the LATERAL itself (a primary-key read per MATCHED row):
#: a plain join to ``entity_profiles k`` let the planner hash the whole table
#: (0.93-1.47 s on live against 1 ms per matched row this way).
FOLLOW_SQL = f"""CROSS JOIN LATERAL (
        WITH RECURSIVE ch(id, nxt, depth) AS (
            SELECT p.id, p.merged_into, 0
          UNION ALL
            SELECT e.id, e.merged_into, ch.depth + 1
              FROM ch JOIN entity_profiles e ON e.id = ch.nxt
             WHERE ch.depth < {FOLLOW_MAX_HOPS}
        )
        SELECT e.id, e.entity_class, e.canonical_name, e.created_at, e.merged_into, e.data
          FROM ch JOIN entity_profiles e ON e.id = ch.id
         ORDER BY ch.depth DESC
         LIMIT 1
      ) k"""

#: A row that can MATCH (and be followed): anything but junk.
MATCHABLE_P = "COALESCE(p.data->>'gc_status', '') <> 'junk'"
#: The elected keeper: active and the end of its chain.
ACTIVE_K = "COALESCE(k.data->>'gc_status', '') NOT IN ('merged', 'junk') AND k.merged_into IS NULL"


def _p(col: str) -> str:
    """The class-priority CASE over ``col``."""
    return _CLASS_PRIORITY_SQL.replace("CASE entity_class", f"CASE {col}", 1)


#: The priority of the elected keeper.
PRIO_K = _p("k.entity_class")

#: The two whole-table SCANS (steps 1b and 2) collect the matched ids first,
#: once (an InitPlan), and follow only those: written as one join, the
#: planner costs the follow for every row it guesses might match (121k of
#: 243k) and spends ~0.5 s compiling the statement (JIT) before it runs.
SCAN_MATCHED_OPEN = "unnest(ARRAY(SELECT p.id FROM entity_profiles p WHERE "
SCAN_MATCHED_CLOSE = ")) AS m(id) JOIN entity_profiles p ON p.id = m.id"

# Exact-name keeper probe. Mirrors entity_resolution.py:561-569 — the FAST path
# on ``idx_entity_profiles_name_class``, excluding gc merged/junk losers. Also
# returns the class priority so the caller can compare it against an exact-ALIAS
# keeper (the E1.1 override below).
_EXACT_SQL = f"""
    SELECT k.id, k.entity_class, k.canonical_name, {PRIO_K} AS prio
      FROM entity_profiles p
      {FOLLOW_SQL}
     WHERE lower(p.canonical_name) = lower($1)
       AND {MATCHABLE_P}
       AND {ACTIVE_K}
     ORDER BY {PRIO_K}, k.created_at ASC
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
    SELECT k.id, k.entity_class, k.canonical_name, {PRIO_K} AS prio
      FROM {SCAN_MATCHED_OPEN}{MATCHABLE_P}
       AND lower(btrim(p.canonical_name)) <> lower(btrim($1))
       AND EXISTS (
         SELECT 1
           FROM jsonb_array_elements_text(
               CASE WHEN jsonb_typeof(p.data->'merged_aliases') = 'array'
                    THEN p.data->'merged_aliases' ELSE '[]'::jsonb END
           ) AS al
          WHERE lower(btrim(al)) = lower(btrim($1))
       ){SCAN_MATCHED_CLOSE}
      {FOLLOW_SQL}
     WHERE {ACTIVE_K}
     ORDER BY {PRIO_K}, k.created_at ASC
     LIMIT 1
"""

# Article/alias-aware fallback probe. Mirrors entity_resolution.py:584-609 —
# article/case/whitespace-normalize BOTH sides (same rule as ``lookup_key``) and
# also probe each keeper's ``merged_aliases`` so a folded surface converges onto
# the ACTIVE keeper. ``$1`` is the ``lookup_key``-normalized incoming name.
_ALIAS_SQL = f"""
    SELECT k.id, k.entity_class, k.canonical_name
      FROM {SCAN_MATCHED_OPEN}{MATCHABLE_P}
       AND (
         regexp_replace(regexp_replace(
             lower(btrim(p.canonical_name)),
             '^(the|a|an)\\s+', '', 'g'),
             '\\s+', ' ', 'g') = $1
         OR EXISTS (
           SELECT 1
             FROM jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(p.data->'merged_aliases') = 'array'
                      THEN p.data->'merged_aliases' ELSE '[]'::jsonb END
             ) AS al
            WHERE regexp_replace(regexp_replace(
                lower(btrim(al)),
                '^(the|a|an)\\s+', '', 'g'),
                '\\s+', ' ', 'g') = $1
         )
       ){SCAN_MATCHED_CLOSE}
      {FOLLOW_SQL}
     WHERE {ACTIVE_K}
     ORDER BY {PRIO_K}, k.created_at ASC
     LIMIT 1
"""


# ---------------------------------------------------------------------------
# P-6 — the key-table lookup and its switch.
# ---------------------------------------------------------------------------

#: The env switch. ``scan`` (default) / ``shadow`` / ``table``; read per call.
ALIAS_LOOKUP_ENV = "LEGBA_ENTITY_ALIAS_LOOKUP"
ALIAS_LOOKUP_MODES: tuple[str, ...] = ("scan", "shadow", "table")

#: A probe whose UTF-8 form is longer than this is always served by the scan.
#: The table stores keys up to 2000 bytes (0263); ``lower()`` can lengthen a
#: string (at most about 3x for the worst full case mappings), so 600 bytes of
#: input can never produce a key the table declined to store.
_TABLE_PROBE_MAX_BYTES = 600

_warned_modes: set[str] = set()


def alias_lookup_mode() -> str:
    """The current ``LEGBA_ENTITY_ALIAS_LOOKUP`` mode; unknown values = scan."""
    raw = os.environ.get(ALIAS_LOOKUP_ENV, "").strip().lower()
    if not raw:
        return "scan"
    if raw in ALIAS_LOOKUP_MODES:
        return raw
    if raw not in _warned_modes:
        _warned_modes.add(raw)
        logger.warning(
            "entity_alias_lookup.unknown_mode value=%r -> scan (expected one of %s)",
            raw[:40], "/".join(ALIAS_LOOKUP_MODES),
        )
    return "scan"


#: Step 1b through the key table. Same predicate as ``_EXACT_ALIAS_SQL``
#: (the key row ``ak`` names the matched profile ``p``, followed to ``k``).
_EXACT_ALIAS_TABLE_SQL = f"""
    SELECT k.id, k.entity_class, k.canonical_name, {PRIO_K} AS prio
      FROM entity_alias_keys ak
      JOIN entity_profiles p ON p.id = ak.entity_id
      {FOLLOW_SQL}
     WHERE ak.key_kind = 'alias_lower'
       AND ak.key = lower(btrim($1))
       AND {MATCHABLE_P}
       AND lower(btrim(p.canonical_name)) <> lower(btrim($1))
       AND {ACTIVE_K}
     ORDER BY {PRIO_K}, k.created_at ASC, k.id ASC
     LIMIT 1
"""

#: Step 2 through the key table. Same predicate as ``_ALIAS_SQL`` (and as the
#: ingestion resolver's normalized fallback). ``$1`` is ``lookup_key(name)``.
_ALIAS_TABLE_SQL = f"""
    SELECT k.id, k.entity_class, k.canonical_name
      FROM entity_alias_keys ak
      JOIN entity_profiles p ON p.id = ak.entity_id
      {FOLLOW_SQL}
     WHERE ak.key_kind = 'norm'
       AND ak.key = $1
       AND {MATCHABLE_P}
       AND {ACTIVE_K}
     ORDER BY {PRIO_K}, k.created_at ASC, k.id ASC
     LIMIT 1
"""

#: Batch forms (table mode only): one statement for N names.
_EXACT_BATCH_SQL = f"""
    SELECT DISTINCT ON (q.t) q.t, k.id, k.entity_class, k.canonical_name,
           {PRIO_K} AS prio
      FROM unnest($1::text[]) AS q(t)
      JOIN entity_profiles p ON lower(p.canonical_name) = lower(q.t)
      {FOLLOW_SQL}
     WHERE {MATCHABLE_P}
       AND {ACTIVE_K}
     ORDER BY q.t, {PRIO_K}, k.created_at ASC, k.id ASC
"""

_EXACT_ALIAS_BATCH_SQL = f"""
    SELECT DISTINCT ON (q.t) q.t, k.id, k.entity_class, k.canonical_name,
           {PRIO_K} AS prio
      FROM unnest($1::text[]) AS q(t)
      JOIN entity_alias_keys ak
        ON ak.key_kind = 'alias_lower' AND ak.key = lower(btrim(q.t))
      JOIN entity_profiles p ON p.id = ak.entity_id
      {FOLLOW_SQL}
     WHERE {MATCHABLE_P}
       AND lower(btrim(p.canonical_name)) <> lower(btrim(q.t))
       AND {ACTIVE_K}
     ORDER BY q.t, {PRIO_K}, k.created_at ASC, k.id ASC
"""

_ALIAS_BATCH_SQL = f"""
    SELECT DISTINCT ON (q.t) q.t, k.id, k.entity_class, k.canonical_name
      FROM unnest($1::text[]) AS q(t)
      JOIN entity_alias_keys ak ON ak.key_kind = 'norm' AND ak.key = q.t
      JOIN entity_profiles p ON p.id = ak.entity_id
      {FOLLOW_SQL}
     WHERE {MATCHABLE_P}
       AND {ACTIVE_K}
     ORDER BY q.t, {PRIO_K}, k.created_at ASC, k.id ASC
"""

_PRIO_P = _p("p.entity_class")

#: Shadow-only: the order keys of the two rows a disagreement names.
_ORDER_KEYS_SQL = f"""
    SELECT p.id, {_PRIO_P} AS prio, p.created_at
      FROM entity_profiles p
     WHERE p.id = ANY($1::uuid[])
"""

#: Running shadow counts (per process). Keys: ``<probe>.<class>``,
#: ``<probe>.lookups`` and per-path milliseconds. Never names.
SHADOW_STATS: dict[str, float] = {}
_SHADOW_SUMMARY_EVERY = 500


def _bump(key: str, by: float = 1.0) -> None:
    SHADOW_STATS[key] = SHADOW_STATS.get(key, 0.0) + by


def _rid(row: Any) -> str | None:
    return None if row is None else str(row["id"])


async def _shadow_classify(conn: Any, scan_row: Any, table_row: Any) -> str:
    """Name the class of one disagreement (never raises)."""
    if table_row is None:
        return "missing_in_table"
    if scan_row is None:
        return "extra_in_table"
    try:
        rows = await conn.fetch(
            _ORDER_KEYS_SQL, [str(scan_row["id"]), str(table_row["id"])])
        keys = {str(r["id"]): (int(r["prio"]), r["created_at"]) for r in rows}
        a, b = keys.get(str(scan_row["id"])), keys.get(str(table_row["id"]))
        if a is not None and a == b:
            return "tie_order"
    except Exception:  # noqa: BLE001 — classification is best-effort
        return "different_unclassified"
    return "different_keeper"


async def _probe(
    conn: Any, *, kind: str, scan_sql: str, table_sql: str, arg: str,
) -> Any:
    """Run one step-1b / step-2 probe under the current switch.

    ``scan``: ``conn.fetchrow(scan_sql, arg)`` — the pre-P-6 statement.
    ``table``: the key table. ``shadow``: both; the scan's row is returned.
    """
    mode = alias_lookup_mode()
    if mode == "scan" or len(arg.encode("utf-8")) > _TABLE_PROBE_MAX_BYTES:
        return await conn.fetchrow(scan_sql, arg)
    if mode == "table":
        return await conn.fetchrow(table_sql, arg)
    # shadow
    t0 = time.perf_counter()
    scan_row = await conn.fetchrow(scan_sql, arg)
    t1 = time.perf_counter()
    table_err = False
    try:
        table_row = await conn.fetchrow(table_sql, arg)
    except Exception as exc:  # noqa: BLE001 — shadow must never move an answer
        table_row, table_err = None, True
        logger.warning("entity_alias_lookup.shadow_table_error probe=%s err=%s",
                       kind, type(exc).__name__)
    t2 = time.perf_counter()
    _bump(f"{kind}.lookups")
    _bump(f"{kind}.scan_ms", (t1 - t0) * 1000.0)
    _bump(f"{kind}.table_ms", (t2 - t1) * 1000.0)
    if table_err:
        verdict = "table_error"
    elif _rid(scan_row) == _rid(table_row):
        verdict = "agree"
    else:
        verdict = await _shadow_classify(conn, scan_row, table_row)
    _bump(f"{kind}.{verdict}")
    if verdict != "agree":
        logger.warning(
            "entity_alias_lookup.shadow_disagree probe=%s class=%s scan_id=%s "
            "table_id=%s",
            kind, verdict, _rid(scan_row), _rid(table_row),
        )
    total = int(sum(v for k, v in SHADOW_STATS.items() if k.endswith(".lookups")))
    if total % _SHADOW_SUMMARY_EVERY == 0:
        logger.info(
            "entity_alias_lookup.shadow_summary %s",
            " ".join(f"{k}={round(v, 1)}" for k, v in sorted(SHADOW_STATS.items())),
        )
    return scan_row


async def fetch_exact_alias_keeper(conn: Any, text: str) -> Any:
    """Step 1b (the exact-alias probe) under the switch."""
    return await _probe(conn, kind="alias_lower", scan_sql=_EXACT_ALIAS_SQL,
                        table_sql=_EXACT_ALIAS_TABLE_SQL, arg=text)


async def fetch_normalized_keeper(conn: Any, scan_sql: str, probe: str) -> Any:
    """Step 2 (the normalized fallback) under the switch.

    ``scan_sql`` is the caller's own pre-P-6 statement, sent unchanged in
    ``scan`` mode (this module's ``_ALIAS_SQL``, or the ingestion resolver's
    inline copy), so ``scan`` stays the exact statement each caller ran.
    """
    return await _probe(conn, kind="norm", scan_sql=scan_sql,
                        table_sql=_ALIAS_TABLE_SQL, arg=probe)


def _decide(
    original: str, cls: str | None, exact_row: Any, alias_row: Any,
    norm_row: Any,
) -> str:
    """The election given the three probe rows (pure; see the contract).

    ``alias_row`` is consulted only when ``exact_row`` is a class-less hit and
    ``norm_row`` only when ``exact_row`` is None — the callers fetch them under
    the same conditions.
    """
    row = exact_row
    exact_cls = (str(row["entity_class"] or "").strip().lower()
                 if row is not None else None)
    if row is not None and exact_cls in _CLASSLESS_ENDPOINT:
        if (
            alias_row is not None
            and int(alias_row["prio"]) < int(row["prio"])
            and _fallback_class_ok(
                str(alias_row["entity_class"] or "").strip().lower(), cls)
        ):
            row = alias_row
    via_fallback = False
    if row is None and norm_row is not None:
        row, via_fallback = norm_row, True
    if row is None:
        return original
    # FALLBACK class-guard (E1 adversarial #1): a normalized/alias match can
    # collide two DISTINCT referents that merely normalize the same — fold
    # only when the endpoint class is class-less or compatible with the
    # keeper's. Dormant for today's "entity" callers; see _fallback_class_ok.
    if via_fallback:
        stored_cls = str(row["entity_class"] or "").strip().lower()
        if not _fallback_class_ok(stored_cls, cls):
            return original
    # Never rewrite to an empty / whitespace keeper surface.
    return str(row["canonical_name"] or "").strip() or original


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
    de-fragmentation LOSER must never be elected), a row merged away
    (``merged_into`` set) is followed to its keeper (roll 85), and the highest
    class-priority ACTIVE keeper wins (tie → oldest ``created_at``).

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
        alias_row = None
        if row is not None and exact_cls in _CLASSLESS_ENDPOINT:
            alias_row = await fetch_exact_alias_keeper(conn, text)

        # 2) ALIAS/ARTICLE fallback — only when the exact probe missed.
        norm_row = None
        if row is None:
            probe = lookup_key(text)
            if probe:
                norm_row = await fetch_normalized_keeper(conn, _ALIAS_SQL, probe)

        keeper = _decide(original, cls, row, alias_row, norm_row)
        if ckey is not None:
            cache[ckey] = keeper
        return keeper
    except Exception as exc:  # pragma: no cover - defensive; degrade-not-break
        logger.warning(
            "resolve_keeper.degraded name=%r class=%r err=%s",
            name, entity_class, exc,
        )
        return original


async def resolve_keepers(
    conn: Any,
    names: Iterable[str],
    *,
    entity_class: str | None = None,
    cache: dict[str, str] | None = None,
) -> dict[str, str]:
    """:func:`resolve_keeper` for many names: ``{name: keeper}``.

    ``scan`` / ``shadow``: one :func:`resolve_keeper` per name, in order — the
    same statements as calling it in a loop. ``table``: at most three
    statements for the whole list (exact, exact-alias, normalized), then the
    same :func:`_decide` per name. Any error falls back to the per-name loop,
    which itself never raises.
    """
    ordered = list(names)
    if alias_lookup_mode() != "table":
        return {n: await resolve_keeper(conn, n, entity_class=entity_class,
                                        cache=cache) for n in ordered}
    out: dict[str, str] = {}
    try:
        cls = (str(entity_class).strip() or None) if entity_class else None
        todo: dict[str, list[str]] = {}
        for n in ordered:
            text = str(n or "").strip()
            if not text:
                out[n] = n
                continue
            ckey = f"{text.lower()}\x00{cls or ''}"
            if cache is not None and ckey in cache:
                out[n] = cache[ckey]
                continue
            todo.setdefault(text, []).append(n)
        for t in [t for t in todo if len(t.encode("utf-8")) > _TABLE_PROBE_MAX_BYTES]:
            for n in todo.pop(t):
                out[n] = await resolve_keeper(conn, n, entity_class=entity_class,
                                              cache=cache)
        if not todo:
            return out
        texts = list(todo)
        exact = {r["t"]: r for r in await conn.fetch(_EXACT_BATCH_SQL, texts)}
        need_alias = [
            t for t, r in exact.items()
            if str(r["entity_class"] or "").strip().lower() in _CLASSLESS_ENDPOINT
        ]
        alias = ({r["t"]: r for r in await conn.fetch(_EXACT_ALIAS_BATCH_SQL, need_alias)}
                 if need_alias else {})
        probes = {t: lookup_key(t) for t in texts if t not in exact}
        probes = {t: k for t, k in probes.items() if k}
        norm_by_key = ({r["t"]: r for r in await conn.fetch(
            _ALIAS_BATCH_SQL, sorted(set(probes.values())))} if probes else {})
        for t, originals in todo.items():
            norm_row = norm_by_key.get(probes[t]) if t in probes else None
            for n in originals:
                keeper = _decide(n, cls, exact.get(t), alias.get(t), norm_row)
                out[n] = keeper
                if cache is not None:
                    cache[f"{t.lower()}\x00{cls or ''}"] = keeper
        return out
    except Exception as exc:  # noqa: BLE001 — degrade to the per-name path
        logger.warning("resolve_keepers.batch_degraded n=%d err=%s",
                       len(ordered), type(exc).__name__)
        return {n: await resolve_keeper(conn, n, entity_class=entity_class,
                                        cache=cache) for n in ordered}


__all__ = [
    "ACTIVE_K",
    "ALIAS_LOOKUP_ENV",
    "ALIAS_LOOKUP_MODES",
    "FOLLOW_MAX_HOPS",
    "FOLLOW_SQL",
    "MATCHABLE_P",
    "PRIO_K",
    "SCAN_MATCHED_CLOSE",
    "SCAN_MATCHED_OPEN",
    "alias_lookup_mode",
    "fetch_exact_alias_keeper",
    "fetch_normalized_keeper",
    "resolve_keeper",
    "resolve_keepers",
]
