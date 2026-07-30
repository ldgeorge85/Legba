#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""backfill_gazetteer_geo.py — bounded one-shot re-geocode via the live gazetteer.

THE GAP THIS CLOSES: the 2026-07 geo-honesty fixes (`ACQUISITION.md`, "Geo
honesty — two contamination fixes") made the ingest-time geocode path prefer
**untagged over mistagged** — a signal whose only place evidence is a bare
NER `location`-class entity ("Kharkiv", "Fallujah") and no `country`-class
entity is left with an EMPTY indexed `geo` column rather than a guessed one,
because a bare city genuinely "can't be mapped to a country without the
online gazetteer" (`filters/geocode.py:country_iso2s_from_country_entities`).
That is the correct ingest-time default (never guess), but it leaves a
backlog of rows that a live geocoder call COULD resolve. ACQUISITION.md says
plainly: "re-geocoding historical rows is a separate operator-gated backfill."
This script is that backfill.

THE MARKER (derived, not a stored column): there is no dedicated
"needs-gazetteer" flag in the schema — grepping the geo-honesty write path
(`filters/geocode.py`, `sources/baseline.py`) turns up no such column. The
honest state is exactly the SQL predicate below: an empty indexed `geo`,
plus a `location`-class entity present, plus no `country`-class entity (the
one case the offline country gazetteer structurally cannot resolve). At
authoring time this was measured at ~550 rows; ingestion is continuous, so
the live count will have grown since — this script re-derives the set fresh
on every run rather than trusting a stale number.

SAFETY:
  * `--dry-run` is the DEFAULT (no `--apply` flag): lists the candidate rows
    and what WOULD be geocoded — a plain read query, no network calls, no
    writes. Safe to run against the live DB at any time.
  * `--apply` performs the real work: one live gazetteer (Nominatim) call per
    row via the SAME backend class the ingest-time filter uses
    (`filters.geocode.NominatimBackend`), rate-limited by an explicit sleep
    between calls (`--sleep-seconds`, default 1.1s — the backend already
    self-throttles the PUBLIC endpoint to 1 req/sec per OSM policy; this is a
    belt-and-suspenders floor that also applies against a self-hosted
    instance) — and writes the resolved country back to both the indexed
    `geo` column (routing/matching) and `payload->'geo'` (the same shape
    `GeocodeHandler.transform()` would have written at ingest time). A row
    the gazetteer cannot resolve is left untouched and counted, never
    guessed.
  * This script's own operator NEVER passes `--apply` — the live apply is
    reserved for the main session. `--apply` is fully implemented (not a
    stub) so it is ready when an operator chooses to run it.

USAGE:
    python3 scripts/backfill_gazetteer_geo.py                    # dry-run, default limit
    python3 scripts/backfill_gazetteer_geo.py --limit 50          # smaller dry-run preview
    python3 scripts/backfill_gazetteer_geo.py --apply --limit 50 --sleep-seconds 1.1
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore

logger = logging.getLogger("backfill_gazetteer_geo")

DEFAULT_LIMIT = 600
DEFAULT_SLEEP_SECONDS = 1.1

# The derived "needs the live gazetteer" predicate (see module docstring):
# indexed geo is empty, a bare location-class NER entity is present, and no
# country-class entity is — the one shape the offline country gazetteer
# cannot resolve on its own.
_CANDIDATE_SQL = """
    SELECT id, source_id, fetched_at, canonical_url,
           payload -> 'title' AS title, payload -> 'entities' AS entities
      FROM signals
     WHERE geo = '{}'::text[]
       AND 'location' = ANY(entity_classes)
       AND NOT ('country' = ANY(entity_classes))
     ORDER BY fetched_at DESC
     LIMIT $1
"""

_UPDATE_SQL = """
    UPDATE signals
       SET geo = $2::text[],
           payload = jsonb_set(payload, '{geo}', $3::jsonb, true),
           updated_at = now()
     WHERE id = $1
"""


def _location_candidates(entities: Any) -> list[str]:
    """The candidate query strings for one row, best-first.

    Delegates to the SAME helper the live ingest-time filter uses
    (``filters.geocode.place_candidates_from_entities``) so a backfilled row
    resolves identically to how it would have at ingest time — country-class
    entity text first (none exist for our candidate set by construction),
    then location-class text in NER order.
    """
    from legba.data.filters.geocode import place_candidates_from_entities

    return place_candidates_from_entities(entities)


async def fetch_candidates(store: PostgresStore, limit: int) -> list[dict[str, Any]]:
    async with store.acquire() as conn:
        rows = await conn.fetch(_CANDIDATE_SQL, limit)
    return [dict(r) for r in rows]


def _row_summary(row: dict[str, Any]) -> str:
    candidates = _location_candidates(row.get("entities"))
    title = row.get("title")
    title_str = title if isinstance(title, str) else ""
    return (
        f"{row['id']}  source={row['source_id']!r}  "
        f"fetched_at={row['fetched_at'].isoformat() if row.get('fetched_at') else '?'}  "
        f"candidates={candidates!r}  title={title_str[:80]!r}"
    )


async def dry_run(store: PostgresStore, limit: int) -> int:
    rows = await fetch_candidates(store, limit)
    print(f"# {len(rows)} candidate row(s) whose geo needs the live gazetteer (limit={limit})")
    print("# dry-run — read-only, no network calls, nothing written")
    for row in rows:
        candidates = _location_candidates(row.get("entities"))
        if not candidates:
            # Shouldn't happen given the SQL predicate, but never silently
            # skip counting — surface it instead of hiding a query/data drift.
            print(f"  SKIP (no location candidate text extracted) {row['id']}")
            continue
        print(f"  {_row_summary(row)}")
    return len(rows)


async def apply_backfill(store: PostgresStore, limit: int, sleep_seconds: float) -> dict[str, int]:
    """The live path — one gazetteer call per candidate row, rate-limited.

    Never invoked unless an operator explicitly passes `--apply`. Uses the
    SAME `NominatimBackend` class (and the same contact-email / self-hosted
    resolution rules) the ingest-time `GeocodeHandler` filter uses, so a
    backfilled row is geocoded under the identical policy a live signal
    would be.
    """
    from legba.data.filters.geocode import (
        NominatimBackend,
        geocoder_contact_email,
        resolve_user_agent,
    )
    import os

    nominatim_url = os.environ.get("LEGBA_GEOCODER_NOMINATIM_URL") or None
    user_agent = resolve_user_agent(None, nominatim_url=nominatim_url)
    backend = NominatimBackend(base_url=nominatim_url, user_agent=user_agent)
    if nominatim_url is None and geocoder_contact_email() is None:
        # resolve_user_agent already raises in this case — unreachable in
        # practice, but keep the intent explicit rather than relying solely
        # on the imported function's side effect.
        raise RuntimeError(
            "refusing to hit the public Nominatim endpoint without an "
            "operator contact — set LEGBA_GEOCODER_CONTACT_EMAIL or "
            "LEGBA_GEOCODER_NOMINATIM_URL (self-hosted)"
        )

    rows = await fetch_candidates(store, limit)
    stats = {"total": len(rows), "resolved": 0, "unresolved": 0, "skipped": 0}
    logger.info("apply: %d candidate row(s), backend=%s", len(rows), backend.name)

    async with store.acquire() as conn:
        for i, row in enumerate(rows):
            candidates = _location_candidates(row.get("entities"))
            if not candidates:
                stats["skipped"] += 1
                continue

            resolved = None
            for query in candidates:
                try:
                    resolved = await backend.geocode(query)
                except Exception as exc:  # noqa: BLE001 — one bad row must not kill the batch
                    logger.warning(
                        "geocode failed id=%s query=%r err=%s", row["id"], query, exc
                    )
                    resolved = None
                if resolved is not None:
                    break

            if resolved is None:
                stats["unresolved"] += 1
                logger.info(
                    "unresolved id=%s candidates=%r — left untouched", row["id"], candidates
                )
            else:
                iso2 = resolved.country_iso2
                if not iso2:
                    stats["unresolved"] += 1
                    logger.info(
                        "resolved but no country_iso2 id=%s result=%r — left untouched",
                        row["id"], resolved,
                    )
                else:
                    geo_payload = resolved.to_payload("country")
                    import json as _json

                    await conn.execute(
                        _UPDATE_SQL, row["id"], [iso2], _json.dumps(geo_payload)
                    )
                    stats["resolved"] += 1
                    logger.info(
                        "resolved id=%s -> %s (%s)", row["id"], iso2, resolved.country
                    )

            # Rate-limit floor between rows regardless of backend, and skip
            # the sleep after the very last row.
            if i < len(rows) - 1:
                await asyncio.sleep(sleep_seconds)

    return stats


async def _main_async(args: argparse.Namespace) -> int:
    store = PostgresStore(PostgresConfig.from_env())
    await store.connect()
    try:
        if not args.apply:
            await dry_run(store, args.limit)
            return 0
        stats = await apply_backfill(store, args.limit, args.sleep_seconds)
        print(
            f"apply done: total={stats['total']} resolved={stats['resolved']} "
            f"unresolved={stats['unresolved']} skipped={stats['skipped']}"
        )
        return 0
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded one-shot re-geocode of signals whose geo needs the live "
            "gazetteer (dry-run by default; see module docstring)."
        )
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"max candidate rows to consider (default {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS,
        help=(
            "seconds to sleep between live gazetteer calls in --apply mode "
            f"(default {DEFAULT_SLEEP_SECONDS}; the public Nominatim policy "
            "is 1 req/sec, enforced again inside NominatimBackend itself)"
        ),
    )
    parser.add_argument(
        "--apply", action="store_true",
        help=(
            "DANGEROUS: perform live gazetteer calls and write results back "
            "to signals.geo / signals.payload->'geo'. Default is dry-run "
            "(read-only, no network calls, nothing written)."
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
