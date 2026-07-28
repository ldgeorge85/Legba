#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""diagnose_stale_leaders.py — READ-ONLY re-seed delta preview (S-3).

Lists the CURRENT officeholder facts in ``facts`` (head-of-state / head-of-
government office facts, still OPEN) whose value DISAGREES with a FRESH Wikidata
current-holder pull — i.e. exactly the leaders a re-seed of the
``wikidata_leaders`` adapter would supersede. Run it BEFORE re-seeding so the
operator can see the delta first.

WRITES NOTHING. The only DB access is SELECT; the "fresh" side runs the seed
adapter's ``fetch`` + ``map`` in memory (no driver, no write path). Safe to run
against the live DB and the live Wikidata endpoint (the seed adapter already
uses the guarded egress client + WDQS's requested user-agent).

Usage:
    # live: fresh pull from WDQS, compared to the open office facts in the DB
    python3 scripts/diagnose_stale_leaders.py

    # offline / test: use a canned SPARQL-JSON fixture instead of the network
    python3 scripts/diagnose_stale_leaders.py --fixture path/to/sparql.json

    # machine-readable
    python3 scripts/diagnose_stale_leaders.py --json

Connection: reads PostgresConfig.from_env() (the same LEGBA_* env the runtime
uses). Run it where that env points at the target DB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# Make `legba` importable when run from a checkout.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover — dotenv optional
    pass

import asyncpg

from legba.data.config import PostgresConfig
from legba.data.seed import SeedContext, SeedFact
from legba.data.seed.adapters.wikidata_leaders import (
    WikidataLeadersSeedSource,
    _HEAD_OF_GOVERNMENT_PREDICATE,
    _HEAD_OF_STATE_PREDICATE,
)

#: The two country-subject office predicates the adapter writes (subject=country,
#: value=leader). These are the "who currently holds office X in country Y" rows.
_OFFICE_PREDICATES = (_HEAD_OF_STATE_PREDICATE, _HEAD_OF_GOVERNMENT_PREDICATE)


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _country_key(s: str | None) -> str:
    """Country match key: lowercased, trimmed, leading 'the ' stripped.

    Curated/older seeds carry 'The Democratic Republic of the Congo' while a
    fresh Wikidata label is 'Democratic Republic of the Congo' — an exact match
    would miss that (the confirmed S-3 case) and file it under UNCONFIRMED. We
    only strip a LEADING 'the ' so mid-string 'the' (…of *the* Congo) is intact.
    """
    k = _norm(s)
    if k.startswith("the "):
        k = k[4:]
    return k


async def _db_open_office_facts(pool: asyncpg.Pool) -> list[dict[str, object]]:
    """The OPEN (current) office facts in the DB: subject=country, value=leader.

    Open = ``valid_until IS NULL AND superseded_by IS NULL`` (the fact_decay /
    supersession model: a superseded/expired row is closed, not deleted). We do
    NOT filter by seed_adapter — a wikidata re-seed keys on the same
    (country, predicate) supersession key, so a curated ``world_baseline`` office
    fact that disagrees with fresh Wikidata is ALSO part of the re-seed delta;
    the ``source`` column tells the operator which it is.
    """
    query = """
        SELECT subject, predicate, value, valid_from, confidence,
               coalesce(data->>'seed_adapter', source_type) AS source,
               data->>'as_of' AS as_of
          FROM facts
         WHERE predicate = ANY($1::text[])
           AND valid_until IS NULL
           AND superseded_by IS NULL
         ORDER BY subject, predicate
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, list(_OFFICE_PREDICATES))
    return [dict(r) for r in rows]


async def _fresh_office_holders(fixture: dict | None) -> dict[tuple[str, str], dict[str, object]]:
    """Run the seed adapter's fetch+map (NO write) → the fresh current holder
    per ``(lower(country), office_predicate)``.

    With ``fixture`` this uses the offline SPARQL-JSON path; without it, ``fetch``
    hits the live WDQS endpoint (read-only) — the same pull a re-seed would do,
    so the delta shown is exactly what a re-seed would change.
    """
    adapter = WikidataLeadersSeedSource()
    ctx = SeedContext(options={"sparql_json": fixture} if fixture is not None else {})
    raw = await adapter.fetch(ctx)
    fresh: dict[tuple[str, str], dict[str, object]] = {}
    for payload in adapter.map(raw):
        if not isinstance(payload, SeedFact):
            continue
        if payload.predicate not in _OFFICE_PREDICATES:
            continue
        # subject=country, value=leader for the office facts.
        key = (_country_key(payload.subject), payload.predicate)
        fresh[key] = {
            "country": payload.subject,
            "leader": payload.value,
            "valid_from": payload.valid_from,
        }
    return fresh


def _classify(
    db_rows: list[dict[str, object]],
    fresh: dict[tuple[str, str], dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """Split into stale (re-seed would supersede), unconfirmed, and new."""
    stale: list[dict[str, object]] = []
    unconfirmed: list[dict[str, object]] = []
    db_keys: set[tuple[str, str]] = set()
    for r in db_rows:
        key = (_country_key(str(r["subject"])), str(r["predicate"]))
        db_keys.add(key)
        match = fresh.get(key)
        if match is None:
            # No fresh holder for this (country, office): a spelling mismatch, an
            # undated/unlabelled fresh holder the adapter dropped, or a country no
            # longer returned. NOT asserted stale — flagged for the operator.
            unconfirmed.append(r)
            continue
        if _norm(str(r["value"])) != _norm(str(match["leader"])):
            stale.append(
                {
                    "country": r["subject"],
                    "office": r["predicate"],
                    "db_value": r["value"],
                    "db_source": r["source"],
                    "db_valid_from": r["valid_from"],
                    "db_as_of": r["as_of"],
                    "fresh_value": match["leader"],
                    "fresh_valid_from": match["valid_from"],
                }
            )
    # Fresh holders with no matching OPEN DB fact — a re-seed would ADD these.
    new = [
        {"country": v["country"], "office": pred, "fresh_value": v["leader"],
         "fresh_valid_from": v["valid_from"]}
        for (ckey, pred), v in fresh.items()
        if (ckey, pred) not in db_keys
    ]
    return {"stale": stale, "unconfirmed": unconfirmed, "new": new}


def _print_report(result: dict[str, list[dict[str, object]]], *, limit: int | None) -> None:
    stale = result["stale"]
    unconfirmed = result["unconfirmed"]
    new = result["new"]

    print("=" * 78)
    print("STALE current officeholder facts — a re-seed WOULD SUPERSEDE these")
    print("(DB open office fact value disagrees with a fresh Wikidata pull)")
    print("=" * 78)
    if not stale:
        print("  (none — every open office fact agrees with fresh Wikidata)")
    for i, r in enumerate(stale):
        if limit is not None and i >= limit:
            print(f"  … and {len(stale) - limit} more")
            break
        print(f"  {r['country']}  [{r['office']}]")
        print(f"      DB     : {r['db_value']!r}  (source={r['db_source']}, "
              f"valid_from={r['db_valid_from']}, as_of={r['db_as_of']})")
        print(f"      FRESH  : {r['fresh_value']!r}  (valid_from={r['fresh_valid_from']})")

    print()
    print("-" * 78)
    print(f"UNCONFIRMED: {len(unconfirmed)} open office fact(s) had NO fresh match "
          "(spelling/undated/dropped — NOT asserted stale)")
    print(f"NEW        : {len(new)} fresh holder(s) with no matching open DB fact "
          "(a re-seed would ADD)")
    print("-" * 78)
    print(f"SUMMARY: stale={len(stale)}  unconfirmed={len(unconfirmed)}  new={len(new)}")


async def _run(*, fixture_path: str | None, as_json: bool, limit: int | None) -> int:
    fixture = None
    if fixture_path:
        with open(fixture_path, "r", encoding="utf-8") as fh:
            fixture = json.load(fh)

    cfg = PostgresConfig.from_env()
    pool = await asyncpg.create_pool(cfg.dsn, min_size=1, max_size=2)
    try:
        db_rows = await _db_open_office_facts(pool)
    finally:
        await pool.close()

    fresh = await _fresh_office_holders(fixture)
    result = _classify(db_rows, fresh)

    if as_json:
        # datetimes → ISO for JSON.
        def _enc(o: object) -> object:
            if hasattr(o, "isoformat"):
                return o.isoformat()
            return str(o)

        print(json.dumps(result, default=_enc, indent=2))
    else:
        _print_report(result, limit=limit)
    # Exit 0 always (this is a read-only report, not a gate); the counts are the
    # signal. Non-empty `stale` is expected when the seed has drifted.
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY: preview which current officeholder facts a "
        "wikidata_leaders re-seed would supersede (S-3)."
    )
    parser.add_argument(
        "--fixture",
        metavar="PATH",
        help="offline SPARQL-JSON fixture ({'leaders':[...],'alliances':[...]}); "
        "skips the live WDQS pull (for testing).",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--limit", type=int, default=None, help="cap the stale rows printed"
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(fixture_path=args.fixture, as_json=args.json, limit=args.limit)
    )


if __name__ == "__main__":  # pragma: no cover — manual invocation
    raise SystemExit(main())
