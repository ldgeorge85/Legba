#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill: canonicalize existing ``entity_profiles`` + merge fragments.

The Phase C canonicalization pass (``_entity_canon.canonicalize_entity``) fixes
NER-mention fragmentation + mistyping AT WRITE TIME going forward. This one-shot
repairs the EXISTING rows the audit found:

  * {US, U.S., USA, United States, America} → 9 profiles across 4 classes, with
    "United States" living as BOTH a country AND a person;
  * 18 NWS forecast offices typed ``person``;
  * "Trump" / "Donald Trump's" / "Donald Trump 's" / "The Trump administration"
    fragments incl. HTML-entity garbage ("Cape Verde&#039;s", "&apos;").

WHAT IT DOES (idempotent, operator-run)
---------------------------------------
For every ``entity_profiles`` row it computes ``canonicalize_entity(name,
class)``. Rows that canonicalize to the SAME ``(lower(canonical_name),
entity_class)`` are a MERGE GROUP. Per group it:

  1. picks a SURVIVOR (oldest ``created_at``, then smallest id) and renames /
     retypes it to the canonical ``(name, class)``;
  2. re-points every other row's ``signal_entity_links`` to the survivor
     (``ON CONFLICT`` on the ``(signal_id, entity_id, role)`` PK → drop the dup);
  3. unions each merged-away row's ``derived_from`` into the survivor, plus a
     content-addressed marker for each merged-away ORIGINAL surface form, plus
     the merged-away row's own id (full provenance);
  4. writes an ``entity_profile_versions`` row on the survivor recording the
     merge;
  5. DELETES the merged-away rows (their ``entity_profile_versions`` cascade).

A lone row whose name/class merely needs canonicalizing (no fragment to merge)
is renamed/retyped in place + gets a version row + its original surface form in
``derived_from``.

NOT RE-POINTED (documented limitation): ``proposed_edges`` references entities
by NAME (text), not id — co-occurrence edges keyed on a pre-merge surface form
are left as-is. The live resolver re-folds names going forward; a separate
edge-canonicalization pass is out of scope here.

SAFETY
------
  * DRY-RUN IS THE DEFAULT. It reports the merge plan + per-row rename/retype
    counts and writes NOTHING. Pass ``--commit`` to apply (inside one
    transaction; rolls back on any error).
  * Idempotent: a second ``--commit`` run finds every row already canonical →
    zero merges, zero renames.

CONNECTION
----------
``PostgresConfig.from_env()`` (LEGBA_DATA_PG_* / POSTGRES_* env), with an
explicit ``--db NAME`` override so the operator can target legba vs a copy.

Usage::

    # dry-run (default) — prints the plan, changes nothing
    set -a; . ./.env; set +a
    PYTHONPATH=src python3 scripts/backfill_entity_canonicalization.py

    # apply
    PYTHONPATH=src python3 scripts/backfill_entity_canonicalization.py --commit

    # target a specific DB
    PYTHONPATH=src python3 scripts/backfill_entity_canonicalization.py --db legba_copy
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

from legba.data._entity_canon import canonicalize_entity  # noqa: E402

# Same content-addressing namespace the live resolver uses, so a marker written
# by the backfill and by the resolver for the same surface form is identical.
from legba.data.analysts.deterministic_handlers.entity_resolution import (  # noqa: E402
    _alias_marker,
)
from legba.data.config import PostgresConfig  # noqa: E402


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--commit",
        action="store_true",
        help="Apply the changes (default: dry-run, writes nothing).",
    )
    p.add_argument(
        "--db",
        default=None,
        help="Override the target database name (default: from env).",
    )
    return p.parse_args(argv)


async def _connect(db_override: str | None) -> asyncpg.Connection:
    cfg = PostgresConfig.from_env()
    return await asyncpg.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=db_override or cfg.database,
    )


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


async def _plan(conn: asyncpg.Connection):
    """Compute the canonicalization + merge plan from current rows.

    Returns ``(groups, n_rows)`` where ``groups`` maps the canonical key
    ``(lower(name), class)`` → list of row dicts that canonicalize into it.
    Only groups that require a change (a merge OR a rename/retype) are kept.
    """
    rows = await conn.fetch(
        "SELECT id, canonical_name, entity_class, created_at, derived_from, data "
        "FROM entity_profiles"
    )
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        raw_name = r["canonical_name"]
        raw_cls = r["entity_class"]
        name, cls = canonicalize_entity(raw_name, raw_cls)
        if not name:
            # Pure-garbage name (e.g. "&apos;") — would strip away. Keep it as
            # its own singleton keyed on the raw value so we never collapse two
            # distinct garbage rows; report but do not rename to empty.
            name, cls = raw_name, raw_cls
        key = (name.lower(), cls)
        groups[key].append(
            {
                "id": r["id"],
                "raw_name": raw_name,
                "raw_class": raw_cls,
                "canon_name": name,
                "canon_class": cls,
                "created_at": r["created_at"],
                "derived_from": list(r["derived_from"] or []),
            }
        )

    # Keep only groups needing work: >1 member (merge) OR the single member's
    # name/class changed (rename/retype).
    actionable: dict[tuple[str, str], list[dict]] = {}
    for key, members in groups.items():
        if len(members) > 1:
            actionable[key] = members
        else:
            m = members[0]
            if m["raw_name"] != m["canon_name"] or m["raw_class"] != m["canon_class"]:
                actionable[key] = members
    return actionable, len(rows)


def _pick_survivor(members: list[dict]) -> dict:
    return sorted(
        members, key=lambda m: (m["created_at"], str(m["id"]))
    )[0]


async def _apply_group(
    conn: asyncpg.Connection,
    members: list[dict],
) -> None:
    """Apply one merge/rename group inside the caller's transaction."""
    canon_name = members[0]["canon_name"]
    canon_class = members[0]["canon_class"]
    survivor = _pick_survivor(members)
    survivor_id = survivor["id"]

    # Collect provenance markers: every member's ORIGINAL surface form + every
    # member's existing derived_from + each merged-away row's own id.
    markers: set[uuid.UUID] = set()
    readable: set[str] = set()
    for m in members:
        if m["raw_name"] != canon_name or m["raw_class"] != canon_class:
            markers.add(_alias_marker(m["raw_name"]))
            readable.add(m["raw_name"])
        for d in m["derived_from"]:
            markers.add(d if isinstance(d, uuid.UUID) else uuid.UUID(str(d)))

    # Re-point links from merged-away rows to the survivor (drop dups on PK).
    for m in members:
        if m["id"] == survivor_id:
            continue
        markers.add(m["id"])  # the merged-away id is itself provenance
        await conn.execute(
            """
            UPDATE signal_entity_links l
               SET entity_id = $2
             WHERE l.entity_id = $1
               AND NOT EXISTS (
                   SELECT 1 FROM signal_entity_links s
                    WHERE s.signal_id = l.signal_id
                      AND s.entity_id = $2
                      AND s.role = l.role
               )
            """,
            m["id"], survivor_id,
        )
        # Any links that couldn't move (a dup already on the survivor) are
        # removed with the row via the FK cascade on delete below.
        await conn.execute("DELETE FROM entity_profiles WHERE id = $1", m["id"])

    # Rename / retype + fold provenance onto the survivor.
    await conn.execute(
        """
        UPDATE entity_profiles
           SET canonical_name = $2,
               entity_class = $3,
               entity_type = $3,
               version = version + 1,
               derived_from = (
                   SELECT COALESCE(array_agg(DISTINCT mk), '{}'::uuid[])
                     FROM unnest(derived_from || $4::uuid[]) AS mk
               ),
               data = jsonb_set(
                   COALESCE(data, '{}'::jsonb),
                   '{merged_aliases}',
                   (
                       SELECT COALESCE(jsonb_agg(DISTINCT a ORDER BY a), '[]'::jsonb)
                         FROM jsonb_array_elements_text(
                             COALESCE(data->'merged_aliases', '[]'::jsonb)
                             || $5::jsonb
                         ) AS a
                   )
               ),
               updated_at = now()
         WHERE id = $1
        """,
        survivor_id,
        canon_name,
        canon_class,
        sorted(markers),
        json.dumps(sorted(readable)),
    )

    # Version row on the survivor recording the merge/rename.
    await conn.execute(
        """
        INSERT INTO entity_profile_versions
            (entity_id, version, data, analyst_id, analyst_version)
        SELECT ep.id, ep.version,
               jsonb_build_object(
                   'canonical_name', ep.canonical_name,
                   'entity_class', ep.entity_class,
                   'merged_aliases', COALESCE(ep.data->'merged_aliases', '[]'::jsonb),
                   'event', 'backfill_canonicalize'
               ),
               'backfill_entity_canonicalization', '1.0.0'
          FROM entity_profiles ep
         WHERE ep.id = $1
        """,
        survivor_id,
    )


async def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    conn = await _connect(args.db)
    try:
        dbname = await conn.fetchval("SELECT current_database()")
        print("=" * 64)
        print("Entity-profile canonicalization backfill")
        print("=" * 64)
        print(f"  DB:   {dbname}")
        print(f"  MODE: {'COMMIT (will write)' if args.commit else 'DRY RUN (no changes)'}")
        print()

        actionable, n_rows = await _plan(conn)
        n_merge_groups = sum(1 for m in actionable.values() if len(m) > 1)
        n_rows_merged_away = sum(
            len(m) - 1 for m in actionable.values() if len(m) > 1
        )
        n_renames = sum(1 for m in actionable.values() if len(m) == 1)

        print(f"  entity_profiles scanned: {n_rows}")
        print(f"  merge groups (>1 fragment): {n_merge_groups}")
        print(f"  rows merged away (deleted): {n_rows_merged_away}")
        print(f"  in-place renames/retypes:   {n_renames}")
        print()

        # Show a sample of the plan (largest groups first).
        sample = sorted(
            actionable.items(), key=lambda kv: len(kv[1]), reverse=True
        )[:20]
        for (lname, cls), members in sample:
            canon_name = members[0]["canon_name"]
            if len(members) > 1:
                frags = ", ".join(
                    f"{m['raw_name']!r}/{m['raw_class']}" for m in members
                )
                print(f"  MERGE → ({canon_name!r}, {cls}): {frags}")
            else:
                m = members[0]
                print(
                    f"  RETYPE/RENAME ({m['raw_name']!r}/{m['raw_class']}) "
                    f"→ ({canon_name!r}/{cls})"
                )
        if len(actionable) > len(sample):
            print(f"  ... and {len(actionable) - len(sample)} more group(s)")
        print()

        if not actionable:
            print("  Nothing to do — every profile is already canonical.")
            return 0

        if not args.commit:
            print("  DRY RUN: no changes written. Re-run with --commit to apply.")
            return 0

        applied = 0
        async with conn.transaction():
            for members in actionable.values():
                await _apply_group(conn, members)
                applied += 1
        print(f"  APPLIED: processed {applied} group(s).")

        # Post-condition report.
        remaining, _ = await _plan(conn)
        print(f"  Post-check: {len(remaining)} group(s) still actionable "
              f"(expected 0 — idempotent).")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
