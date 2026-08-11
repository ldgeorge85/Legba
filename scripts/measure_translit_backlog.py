#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""measure_translit_backlog.py — the V-G6 phonetic-alias POPULATION census.

WHY THIS EXISTS AS A COMMITTED SCRIPT
-------------------------------------
The "720-pair transliteration backlog" has been quoted three times now — in
commit ``801e8477``, in migration ``0165``'s header, and in the W3-C audit — and
until this file it lived in nobody's tree. It is not a queue and never was:
there is no backlog table, no counter, no persisted list. It is *whatever the
V-G6 predicate accepts against the live person table today*, recomputed from
scratch each time anyone asks, which is why the number moved from 720 (at commit
time, 20,512 rows) to 726 (a day later, 20,621 rows) without anything happening.

That is fine — recomputable is better than retrievable — but only if everyone
recomputes it the SAME way. The W3-C audit had to re-derive the predicate by
hand from a commit message to check its own numbers. This script is that
derivation, committed, so the next before/after comparison is a diff of two runs
rather than a diff of two reconstructions.

WHAT IT MEASURES
----------------
Three numbers over ACTIVE person rows (``merged_into IS NULL``, ``gc_status``
not merged/junk):

  ``population``  rows the predicate is evaluated over
  ``accepted``    pairs accepted by V-G6 conditions 1-4 (the historical figure:
                  same token count >= 2, no digits, whole-string Levenshtein
                  <= 2, per-token double-metaphone overlap primary OR alt)
  ``gated``       pairs condition 5 — the W3-C COMPASS GATE — then rejects

``accepted - gated`` is the backlog as the shipped guard now sees it.

THE GATE'S TOKEN LIST IS IMPORTED, NOT RETYPED. ``DIRECTIONAL_TOKENS`` comes
from :mod:`legba.data._entity_canon` and is bound as a query PARAMETER, the same
way the live probe in ``entity_resolution._TRANSLIT_PROBE_SQL`` binds it. Two
copies of a word list is how a before/after measurement quietly stops comparing
like with like.

NOT THE PREFILTER. The census deliberately does NOT apply migration 0165's
``entity_phonetic_key`` prefilter, because 0165's own header measures that
prefilter at 97% recall of what the predicate accepts — including it would
under-count by ~3% and would not be the same number 720/726 refer to. The
prefilter is a cost optimisation for the single-row live probe, not part of the
predicate's meaning.

SAFETY: read-only. The session is opened with
``default_transaction_read_only = on`` (server-enforced) in addition to issuing
nothing but SELECTs. This script proposes no merge, writes no table, and emits
no migration — per the W3-C verdict, resolving any of these pairs is a per-pair
human-gated decision, not a batch this script is allowed to prepare.

COST: an O(n^2) self-join over ~20.6k rows, roughly 3 minutes. It is a census,
run deliberately, not something to put on a cadence.

USAGE
-----
    python3 scripts/measure_translit_backlog.py                  # the counts
    python3 scripts/measure_translit_backlog.py --show-gated     # + the pairs
                                                                 #   condition 5
                                                                 #   removed
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover — dotenv optional
    pass

from legba.data._entity_canon import DIRECTIONAL_TOKENS
from legba.data.config import PostgresConfig

#: The ACTIVE person population, spelled once and reused by every query below so
#: the three counts cannot drift onto different denominators.
_ACTIVE_PERSON = """
    SELECT id,
           canonical_name,
           lower(btrim(canonical_name)) AS lo,
           regexp_split_to_array(lower(btrim(canonical_name)), '\\s+') AS toks
      FROM entity_profiles
     WHERE entity_class = 'person'
       AND merged_into IS NULL
       AND COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk')
       AND canonical_name !~ '[0-9]'
"""

#: V-G6 conditions 1-4, as a self-join. ``a.id < b.id`` counts each unordered
#: pair once. Verbatim from commit 801e8477 — same token count >= 2, no digits
#: (already applied in the population), Levenshtein <= 2, and per-token double
#: metaphone overlap testing PRIMARY *or* ALT on each side (the alt code is the
#: load-bearing half: dmetaphone('bluth')='PL0' but dmetaphone('balut')='PLT',
#: and it is their alt codes that match).
_PAIRS_CTE = f"""
WITH act AS ({_ACTIVE_PERSON}),
pairs AS (
    SELECT a.canonical_name AS name_a, b.canonical_name AS name_b,
           a.toks AS toks_a, b.toks AS toks_b
      FROM act a
      JOIN act b
        ON a.id < b.id
       AND array_length(a.toks, 1) >= 2
       AND array_length(a.toks, 1) = array_length(b.toks, 1)
       AND a.lo <> b.lo
       AND levenshtein(a.lo, b.lo) <= 2
       AND NOT EXISTS (
           SELECT 1
             FROM generate_subscripts(a.toks, 1) AS k
            WHERE NOT (
                ARRAY[dmetaphone(a.toks[k]), dmetaphone_alt(a.toks[k])]
                && ARRAY[dmetaphone(b.toks[k]), dmetaphone_alt(b.toks[k])]
            )
       )
)
"""

#: Condition 5 — the W3-C compass gate, as the boolean the live probe negates.
#: ``$1`` is DIRECTIONAL_TOKENS.
_GATED_EXPR = """
    EXISTS (
        SELECT 1
          FROM generate_subscripts(toks_a, 1) AS d
         WHERE toks_a[d] IS DISTINCT FROM toks_b[d]
           AND (toks_a[d] = ANY($1::text[]) OR toks_b[d] = ANY($1::text[]))
    )
"""

_COUNT_SQL = _PAIRS_CTE + f"""
SELECT count(*) AS accepted,
       count(*) FILTER (WHERE {_GATED_EXPR}) AS gated
  FROM pairs
"""

_GATED_ROWS_SQL = _PAIRS_CTE + f"""
SELECT name_a, name_b FROM pairs WHERE {_GATED_EXPR} ORDER BY name_a, name_b
"""

_POPULATION_SQL = f"SELECT count(*) FROM ({_ACTIVE_PERSON}) act"


async def _run(show_gated: bool) -> int:
    import asyncpg

    cfg = PostgresConfig.from_env()
    conn = await asyncpg.connect(
        host=cfg.host, port=cfg.port, user=cfg.user,
        password=cfg.password, database=cfg.database,
        server_settings={"default_transaction_read_only": "on"},
    )
    try:
        tokens = sorted(DIRECTIONAL_TOKENS)
        population = await conn.fetchval(_POPULATION_SQL)
        row = await conn.fetchrow(_COUNT_SQL, tokens)
        accepted, gated = int(row["accepted"]), int(row["gated"])
        print(f"active person population : {population}")
        print(f"pairs accepted (V-G6 1-4): {accepted}")
        print(f"rejected by compass gate : {gated}")
        print(f"backlog after the gate   : {accepted - gated}")
        print(f"direction tokens bound   : {len(tokens)}")
        if show_gated:
            print()
            for r in await conn.fetch(_GATED_ROWS_SQL, tokens):
                print(f"  {r['name_a']}  ||  {r['name_b']}")
    finally:
        await conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--show-gated", action="store_true",
        help="also print every pair the compass gate removes",
    )
    args = ap.parse_args()
    return asyncio.run(_run(args.show_gated))


if __name__ == "__main__":
    raise SystemExit(main())
