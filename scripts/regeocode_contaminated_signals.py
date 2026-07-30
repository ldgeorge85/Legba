#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-geocode HISTORICAL geo-contaminated signals (S-2 backfill).

The S-2 ingest fix (`8b64f68`, merged `13ec4bd`) stopped TWO mechanisms that
stamped the wrong country onto ``signals.geo`` — but only for NEW rows. ~319
pre-fix rows (measured 2026-07-27, dominated by CNA=SG) still carry the wrong
tag and keep mis-routing into geo-scoped desk slices. This script is the
one-shot historical backfill: it SELECTs the rows matching the two S-2
contamination signatures and re-derives their geo with the CURRENT enrichment
rules — importing the very same helpers the live path runs
(:func:`legba.data.sources.baseline._origin_corroborated_by_content`,
:func:`legba.data.filters.geocode._partition_place_entities` /
``country_iso2s_from_country_entities`` / ``extract_country_iso2_from_text``)
so the backfill can never drift from the ingest behavior.

The two mechanisms, and their stored-row signatures
---------------------------------------------------
1. **Publisher-origin fallback** (``baseline.py`` — CNA→SG on world stories).
   Pre-fix, ``run_baseline`` stamped ``payload.publisher_origin`` (the outlet's
   home country) onto EVERY row whose body left ``geo`` empty. Signature:
   ``geo`` non-empty AND ``geo ⊆ payload.publisher_origin`` AND the geocoder's
   own promote (``payload.geo.country_iso2``) is absent from ``geo`` (i.e. the
   tag can ONLY have come from the fallback — a genuinely geocoded row is
   excluded). Re-derivation replays the CURRENT gate: the origin survives only
   where the story CONTENT corroborates it (country named in title/text/
   raw_body or a country-class NER entity); otherwise geo is CLEARED — a
   missing tag under-includes, a wrong one misroutes (the S-2 contract).

2. **Dateline / incidental-location out-vote** (``geocode.py`` — Yonhap→BR).
   Pre-fix, a ``location``-class NER entity a person was merely mentioned at
   could win the inference ladder. Signature: ``payload.geo.country_iso2``
   present AND in ``geo`` AND the entities carry a body-only (non-title)
   location AND the promoted ISO is NOT attested by any higher-priority
   offline candidate. Re-derivation mirrors the CURRENT ladder offline:
   country-class entities (pycountry) → title-corroborated subject locations
   (backend-only ⇒ row SKIPPED as ambiguous when they would decide) →
   title/text/raw_body country sweeps. When a higher-priority candidate
   resolves to a different country, the promoted ISO is replaced in ``geo``
   and ``payload.geo`` is rewritten to an honest COUNTRY-precision block
   (the wrong place's lat/lon is dropped, not kept); when nothing higher
   resolves, the row is LEFT ALONE — an incidental location may still
   legitimately resolve under the current rules, so it is not provably wrong.

Honesty rails
-------------
* OFFLINE ONLY: no geocoder backend call. Rows whose correct answer needs the
  online gazetteer (a subject location, a ``location_name`` hint) are skipped
  and counted — never guessed.
* Corpus dirty-marker contract: the OpenSearch corpus doc projects ``geo``,
  so every updated row also NULLs ``signals.indexed_at`` IN THE SAME STATEMENT
  (the 0082 lost-update rule) — the corpus_indexer sweep re-indexes it.
* DRY-RUN BY DEFAULT — prints before/after geo per row, writes nothing.
  ``--apply`` executes in bounded per-transaction batches. Idempotent: fixed
  rows re-assess as clean (or drop out of the candidate SELECT entirely).

Run in the registry container (runtime deps present) against the live Postgres:

  docker exec -e LEGBA_DATA_PG_DB=legba \\
      -e LEGBA_DATA_PG_HOST=legba-postgres-1 \\
      -e PYTHONPATH=/app/src \\
      <registry-container> python3 /app/scripts/regeocode_contaminated_signals.py

Or on the host with the repo mounted:

  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/regeocode_contaminated_signals.py          # dry-run
  ... --apply                                                    # execute

Flags:
  --apply          execute (default: dry-run, read-only)
  --batch-rows N   candidate rows per SELECT/transaction (default 500)
  --tenant T       restrict to one owner_tenant
  --source S       restrict to one source_id
  --limit N        stop after assessing N candidate rows
  --quiet-rows     suppress the per-row before/after lines (summary only)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

# Make `legba` importable when run from a checkout.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg
import pycountry

from legba.data.filters.geocode import (
    _partition_place_entities,
    country_iso2s_from_country_entities,
    extract_country_iso2_from_text,
)
from legba.data.sources.baseline import _origin_corroborated_by_content


async def _connect_pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


@dataclass
class Decision:
    """Outcome of assessing one stored row against the CURRENT S-2 rules."""

    action: str                       # 'fix' | 'clean' | 'skip_ambiguous' | 'not_candidate'
    mechanism: str | None = None      # 'publisher_origin' | 'dateline_location'
    new_geo: list[str] = field(default_factory=list)
    new_geo_block: dict[str, Any] | None = None   # payload.geo replacement (class 2)
    reason: str = ""


def _country_entity_iso(text: str) -> str | None:
    """Offline ISO2 for ONE country-class entity text (the live resolver)."""
    isos = country_iso2s_from_country_entities([{"class": "country", "text": text}])
    return next(iter(isos)) if len(isos) == 1 else (sorted(isos)[0] if isos else None)


def _country_block(iso2: str) -> dict[str, Any]:
    """Honest COUNTRY-precision payload.geo block for a re-derived ISO —
    no lat/lon (the offline path has none; a wrong point is worse than none)."""
    c = pycountry.countries.get(alpha_2=iso2)
    return {
        "country": getattr(c, "name", iso2),
        "country_iso2": iso2,
        "precision": "country",
        "backfill": "s2_regeocode_2026_07",
    }


def assess_row(payload: dict[str, Any], geo: list[str]) -> Decision:
    """Pure decision for one stored row — the whole selection + re-derivation.

    Matches CONTAMINATED rows and leaves clean rows alone; see the module
    docstring for the two signatures. No I/O, no backend.
    """
    if not geo:
        return Decision("not_candidate", reason="geo empty")
    gb = payload.get("geo")
    promoted = gb.get("country_iso2") if isinstance(gb, dict) else None

    # ---- Mechanism 1: publisher-origin fallback (CNA→SG class) -------------
    po = payload.get("publisher_origin")
    origin = [x for x in po if isinstance(x, str) and x] if isinstance(po, list) else []
    if origin and set(geo) <= set(origin) and (promoted is None or promoted not in geo):
        # Only the pre-fix fallback can have produced this tag. Replay the
        # CURRENT corroboration gate over the stored payload.
        corroborated = _origin_corroborated_by_content(
            SimpleNamespace(payload=payload), origin,
        )
        if set(corroborated) == set(geo):
            return Decision(
                "clean", mechanism="publisher_origin",
                reason="origin corroborated by content (current rule keeps it)",
            )
        return Decision(
            "fix", mechanism="publisher_origin", new_geo=list(corroborated),
            reason=(
                "uncorroborated publisher-origin tag"
                if not corroborated else "origin only partially corroborated"
            ),
        )

    # ---- Mechanism 2: dateline / incidental-location out-vote --------------
    if not promoted or promoted not in geo:
        return Decision("not_candidate", reason="no geocoder-promoted iso in geo")
    countries, locations = _partition_place_entities(payload.get("entities"))
    title = payload.get("title")
    title_lc = title.lower() if isinstance(title, str) else ""
    subject_locs = [l for l in locations if title_lc and l.lower() in title_lc]
    incidental = [l for l in locations if l not in subject_locs]
    if not incidental:
        return Decision("not_candidate", reason="no incidental location entity")
    if isinstance(gb, dict) and isinstance(gb.get("location_name"), str) \
            and gb["location_name"].strip():
        return Decision(
            "skip_ambiguous", mechanism="dateline_location",
            reason="payload.geo.location_name outranks entities (needs backend)",
        )

    # Higher-priority candidates, in the CURRENT ladder order, offline only.
    country_isos: list[str] = []
    for text in countries:
        iso = _country_entity_iso(text)
        if iso and iso not in country_isos:
            country_isos.append(iso)
    field_isos: list[str] = []
    for fname in ("title", "text", "raw_body"):
        value = payload.get(fname)
        if isinstance(value, str) and value.strip():
            iso = extract_country_iso2_from_text(value)
            if iso and iso not in field_isos:
                field_isos.append(iso)

    if promoted in country_isos or promoted in field_isos:
        return Decision(
            "clean", mechanism="dateline_location",
            reason="promoted iso attested by a higher-priority candidate",
        )
    if country_isos:
        verdict = country_isos[0]
    elif subject_locs:
        return Decision(
            "skip_ambiguous", mechanism="dateline_location",
            reason="subject location would decide (needs the online gazetteer)",
        )
    elif field_isos:
        verdict = field_isos[0]
    else:
        return Decision(
            "clean", mechanism="dateline_location",
            reason="no higher-priority candidate — incidental location may "
                   "legitimately resolve under current rules",
        )

    new_geo: list[str] = []
    for g in geo:
        mapped = verdict if g == promoted else g
        if mapped not in new_geo:
            new_geo.append(mapped)
    return Decision(
        "fix", mechanism="dateline_location", new_geo=new_geo,
        new_geo_block=_country_block(verdict),
        reason=f"incidental location out-voted the subject (current verdict {verdict})",
    )


# Broad SQL pre-filter; the exact signature match happens in assess_row.
_CANDIDATES_SQL = """
SELECT id, source_id, owner_tenant, fetched_at, geo, payload
FROM signals
WHERE geo <> '{{}}'
  AND id > {after}
  AND (
        payload ? 'publisher_origin'
     OR ((payload -> 'geo') ? 'country_iso2' AND payload ? 'entities')
  )
  {tenant} {source}
ORDER BY id
LIMIT {lim}
"""

# geo changed ⇒ the corpus doc (which projects geo) is stale ⇒ NULL indexed_at
# in the SAME statement (0082 dirty-marker rule). payload.geo is replaced only
# for class-2 fixes ($3 non-NULL).
_UPDATE_SQL = """
UPDATE signals
   SET geo = $2::text[],
       payload = CASE WHEN $3::jsonb IS NULL THEN payload
                      ELSE jsonb_set(payload, '{geo}', $3::jsonb) END,
       indexed_at = NULL,
       updated_at = NOW()
 WHERE id = $1
"""


def _parse_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            out = json.loads(raw)
            return out if isinstance(out, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


async def run(
    conn: asyncpg.Connection,
    *,
    apply: bool = False,
    batch_rows: int = 500,
    tenant: str | None = None,
    source: str | None = None,
    limit: int | None = None,
    quiet: bool = False,
    quiet_rows: bool = False,
) -> dict[str, Any]:
    """Assess (and with ``apply=True`` fix) all candidate rows. Returns counters."""
    def _say(msg: str) -> None:
        if not quiet:
            print(msg)

    counters = {
        "candidates_assessed": 0,
        "fixed_publisher_origin": 0,
        "fixed_dateline_location": 0,
        "cleared_geo": 0,
        "skipped_ambiguous": 0,
        "clean": 0,
        "not_candidate": 0,
    }
    fixes: list[dict[str, Any]] = []
    after: Any = "00000000-0000-0000-0000-000000000000"
    done = False

    while not done:
        params: list[Any] = []

        def _p(v: Any) -> str:
            params.append(v)
            return f"${len(params)}"

        want = batch_rows
        if limit is not None:
            want = min(want, limit - counters["candidates_assessed"])
            if want <= 0:
                break
        sql = _CANDIDATES_SQL.format(
            after=_p(after) + "::uuid",
            tenant=f"AND owner_tenant = {_p(tenant)}" if tenant is not None else "",
            source=f"AND source_id = {_p(source)}" if source is not None else "",
            lim=int(want),
        )
        rows = await conn.fetch(sql, *params)
        if not rows:
            break
        after = rows[-1]["id"]

        batch_updates: list[tuple[Any, list[str], str | None]] = []
        for r in rows:
            counters["candidates_assessed"] += 1
            payload = _parse_payload(r["payload"])
            geo = list(r["geo"] or [])
            d = assess_row(payload, geo)
            if d.action == "fix":
                counters[f"fixed_{d.mechanism}"] += 1
                if not d.new_geo:
                    counters["cleared_geo"] += 1
                block_json = (
                    json.dumps(d.new_geo_block)
                    if d.new_geo_block is not None else None
                )
                batch_updates.append((r["id"], d.new_geo, block_json))
                fixes.append({
                    "id": str(r["id"]),
                    "source_id": r["source_id"],
                    "mechanism": d.mechanism,
                    "before": geo,
                    "after": d.new_geo,
                    "reason": d.reason,
                })
                if not quiet_rows:
                    _say(
                        f" {'FIX ' if apply else 'would-fix'} {r['id']} "
                        f"{r['source_id'][:32]:<32} {d.mechanism:<18} "
                        f"geo {geo} -> {d.new_geo}  ({d.reason})"
                    )
            elif d.action == "skip_ambiguous":
                counters["skipped_ambiguous"] += 1
                if not quiet_rows:
                    _say(
                        f" skip      {r['id']} {r['source_id'][:32]:<32} "
                        f"geo {geo} stays  ({d.reason})"
                    )
            else:
                counters[d.action if d.action in counters else "clean"] += 1

        if apply and batch_updates:
            async with conn.transaction():
                await conn.executemany(_UPDATE_SQL, batch_updates)

    result: dict[str, Any] = dict(counters)
    result["fixes"] = fixes
    _say("=" * 78)
    mode = "APPLIED" if apply else "DRY RUN — no rows changed (re-run with --apply)"
    _say(f" {mode}")
    _say(f" candidates assessed        : {counters['candidates_assessed']:>7,}")
    _say(f" fixed (publisher origin)   : {counters['fixed_publisher_origin']:>7,}")
    _say(f" fixed (dateline location)  : {counters['fixed_dateline_location']:>7,}")
    _say(f"   of which geo CLEARED     : {counters['cleared_geo']:>7,}")
    _say(f" skipped (needs backend)    : {counters['skipped_ambiguous']:>7,}")
    _say(f" clean under current rules  : {counters['clean']:>7,}")
    _say(f" not in either signature    : {counters['not_candidate']:>7,}")
    _say("=" * 78)
    return result


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-derive geo for S-2-contaminated historical signals "
        "with the CURRENT enrichment rules (dry-run by default).",
    )
    ap.add_argument("--apply", action="store_true",
                    help="execute (default: dry-run, read-only)")
    ap.add_argument("--batch-rows", type=int, default=500)
    ap.add_argument("--tenant", type=str, default=None)
    ap.add_argument("--source", type=str, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--quiet-rows", action="store_true",
                    help="suppress per-row before/after lines")
    args = ap.parse_args()

    conn = await _connect_pg()
    try:
        await run(
            conn,
            apply=args.apply,
            batch_rows=args.batch_rows,
            tenant=args.tenant,
            source=args.source,
            limit=args.limit,
            quiet_rows=args.quiet_rows,
        )
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
