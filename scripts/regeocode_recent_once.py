#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-tag RECENT signals under the lane N-O geo rules — ONCE, dry-run first.

Lane N-O changed how ``signals.geo`` is derived at ingest
(:mod:`legba.data.filters._geo_precedence` + the token rules in
:mod:`legba.data.filters._geocode_rules`): an official source's declared
country is always in its geo, subject-zone countries are added beside it,
and non-place tokens ("91 FR 60505", "GI Breuer", image-hash hex, "SDN List",
an ALL-CAPS NWS body) no longer geocode. New ingest only. This script re-runs
the SAME filter code offline over a bounded date window of stored rows and
reports (default) or applies (``--apply``) the changed tags.

Honesty rails (the ``regeocode_contaminated_signals`` contract)
---------------------------------------------------------------
* OFFLINE: no geocoder backend call. A country-name query resolves through
  pycountry; an authoritative geometry resolves point-in-country. A row whose
  answer would need the online gazetteer (the ladder reaches a city / place
  query before anything offline resolves) is SKIPPED and counted, never
  guessed.
* BACKUP FIRST: ``--apply`` copies each changed row's ``geo``,
  ``payload.geo`` and ``payload.geo_fallback`` into
  ``signals_geo_backup_n_o`` BEFORE updating it (``ON CONFLICT DO NOTHING`` —
  the first, pre-lane values are the ones kept, so a re-run never overwrites
  the backup with already-fixed values).
* Corpus dirty-marker: every updated row NULLs ``indexed_at`` in the same
  statement (the 0082 rule) so the corpus indexer re-projects ``geo``.
* IDEMPOTENT: a fixed row re-derives to the geo it now has ⇒ ``unchanged``.
* BOUNDED: ``--since-days`` (default 7) or ``--since``/``--until`` (<= 31 d).
* SCOPED: codes the source handler set itself (GDELT's structured country)
  are kept; a stored-untagged row that only the publisher-origin fallback
  would tag (lane N-B's new-ingest-only rule) is skipped unless
  ``--include-fallback``.

Run on the host with the repo mounted (registry container works the same):

  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/regeocode_recent_once.py --quiet-rows     # dry-run
  ... --apply                                                   # execute
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg
import pycountry

from legba.data.filters._contract import FilterContext
from legba.data.filters._geo_precedence import promoted_geo_codes
from legba.data.filters._geocode_rules import QUERY_NAMES
from legba.data.filters.geocode import (
    _COUNTRY_NAME_INDEX,
    GeocodeConfig,
    GeocodeHandler,
    GeocodeResult,
    extract_country_iso2_from_text,
)
from legba.data.sources._contract import Signal
from legba.data.sources._geo_origin_fallback import origin_fallback
from legba.data.sources.baseline import _origin_corroborated_by_content

BACKUP_TABLE = "signals_geo_backup_n_o"


class OfflineBackend:
    """Country-name queries only; records any other query it was asked."""

    name = "offline"

    def __init__(self) -> None:
        self.unresolved: list[str] = []

    async def geocode(self, query: str) -> GeocodeResult | None:
        key = query.strip().lower()
        iso = _COUNTRY_NAME_INDEX.get(key) or next(
            (i for i, n in QUERY_NAMES.items() if n.lower() == key), None)
        if iso is None:
            try:
                iso = pycountry.countries.lookup(query.strip()).alpha_2
            except LookupError:
                self.unresolved.append(query)
                return None
        c = pycountry.countries.get(alpha_2=iso)
        return GeocodeResult(
            country=getattr(c, "name", iso), country_iso2=iso,
            country_iso3=getattr(c, "alpha_3", None), region=None,
            municipality=None, address=None, lat=None, lon=None,
            precision="country", source="offline",
        )

    async def reachable(self) -> bool:
        return True


@dataclass
class Decision:
    action: str          # changed | unchanged | skip_needs_backend | skip_fallback_only | skip_title_loss
    old_geo: list[str] = field(default_factory=list)
    new_geo: list[str] = field(default_factory=list)
    geo_block: dict[str, Any] | None = None
    geo_fallback: dict[str, Any] | None = None
    reason: str = ""


#: ``payload.geo.source`` values the geocoder's TEXT ladder writes — a block
#: from one of these is re-derived from scratch.
_TEXT_SOURCES = frozenset({"nominatim", "google", "declared", "offline"})


def _strip(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload as the geocoder first saw it. A text-ladder block is
    dropped; a ``geometry`` block keeps only its coordinate (the live
    geometry-first branch re-reads it); any other block was written by the
    SOURCE HANDLER before the geocoder ran and is kept whole (live, the
    geocoder skipped such a row)."""
    out = dict(payload)
    out.pop("geo_fallback", None)
    geo = out.get("geo")
    if isinstance(geo, dict):
        src = geo.get("source")
        if src in _TEXT_SOURCES:
            out.pop("geo")
        elif src == "geometry":
            out["geo"] = {k: geo[k] for k in ("lat", "lon", "geometry") if k in geo}
    return out


#: The text fields whose LEAD a held change is checked against (roll 74).
_HOLD_FIELDS = ("title", "summary", "text", "raw_body")
_HOLD_LEAD_CHARS = 400


def _plainly_named_codes(payload: dict[str, Any]) -> set[str]:
    """The first country each text field's lead names, read WITHOUT the NER
    person-name mask. Roll 74 found the lane N-B guard blanking country names
    inside NER surfaces mislabelled ``person`` ("US House", "Panama Canal",
    "Government of Liberia"), so a re-derive can drop a country the title
    plainly names; ``--hold-title-losses`` leaves such a row as it is."""
    from legba.data.filters._geo_precedence import payload_language
    lang = payload_language(payload)
    out: set[str] = set()
    for f in _HOLD_FIELDS:
        v = payload.get(f)
        if isinstance(v, str) and v.strip():
            hit = extract_country_iso2_from_text(v[:_HOLD_LEAD_CHARS], language=lang)
            if hit:
                out.add(hit)
    return out


async def assess_row(
    payload: dict[str, Any],
    geo: list[str],
    *,
    source_id: str,
    source_class: str | None,
    scope_geo: list[str],
    scope_languages: list[str] | None = None,
    canonical_url: str | None = None,
    include_fallback: bool = False,
    hold_title_losses: bool = False,
) -> Decision:
    """Re-derive one stored row's geo under the CURRENT rules, offline."""
    backend = OfflineBackend()
    handler = GeocodeHandler(GeocodeConfig(nominatim_url="http://offline"), backend=backend)
    ctx = FilterContext(
        target_id=source_id, scope_geo=list(scope_geo or []),
        scope_languages=list(scope_languages or []), source_class=source_class,
    )
    sig = Signal(source_id=source_id, payload=_strip(payload), canonical_url=canonical_url)
    out = await handler.transform(sig, ctx)
    block = out.payload.get("geo") if isinstance(out.payload.get("geo"), dict) else None
    basis = (block or {}).get("derivation") or {}
    primary = (block or {}).get("country_iso2")
    if backend.unresolved and (block or {}).get("source") != "geometry" \
            and primary not in geo:
        # The live ladder asked the gazetteer about a place BEFORE anything
        # resolved offline; the live answer may differ, so do not guess.
        return Decision("skip_needs_backend", old_geo=list(geo),
                        reason=f"needs the gazetteer for {backend.unresolved[0]!r}")
    origin = payload.get("publisher_origin")
    hinted = [o for o in origin if isinstance(o, str) and o] if isinstance(origin, list) else []
    # Codes the SOURCE HANDLER set itself (GDELT's structured country, ...)
    # are not the geocoder's to change: keep every stored code that neither
    # the stored geocode block, the stored fallback, nor the corroborated
    # publisher origin accounts for.
    derived_before = set(promoted_geo_codes(payload.get("geo")))
    fb_before = payload.get("geo_fallback")
    if isinstance(fb_before, dict):
        derived_before |= {str(c) for c in fb_before.get("iso2") or []}
    if not derived_before and hinted and set(geo) <= set(hinted):
        derived_before |= set(geo)
    kept = [c for c in geo if c not in derived_before]
    new_geo = kept + [c for c in promoted_geo_codes(block) if c not in kept]
    fallback = None
    if not new_geo:
        corroborated = _origin_corroborated_by_content(out, hinted) if hinted else []
        new_geo = corroborated or origin_fallback(out, hinted) or []
        fallback = out.payload.get("geo_fallback")
        if not geo and not include_fallback:
            # Stored untagged, now only the publisher-origin rung would tag
            # it: that is lane N-B's (new-ingest-only) fallback, not N-O's.
            return Decision("skip_fallback_only", old_geo=[], new_geo=new_geo,
                            reason="publisher-origin backfill (use --include-fallback)")
    if set(new_geo) == set(geo):
        return Decision("unchanged", old_geo=list(geo), new_geo=list(geo))
    if hold_title_losses:
        lost = (set(geo) - set(new_geo)) & _plainly_named_codes(payload)
        if lost:
            return Decision("skip_title_loss", old_geo=list(geo), new_geo=list(new_geo),
                            reason=f"would drop {sorted(lost)}, which the text names")
    if block is not None:
        stored = payload.get("geo") if isinstance(payload.get("geo"), dict) else {}
        if stored.get("country_iso2") == block.get("country_iso2"):
            # Same primary: keep the stored (precise, backend-resolved) point.
            block = {**stored, "countries": block.get("countries"),
                     "derivation": basis or block.get("derivation")}
    return Decision("changed", old_geo=list(geo), new_geo=list(new_geo),
                    geo_block=block, geo_fallback=fallback,
                    reason=json.dumps(basis, sort_keys=True) if basis else "fallback")


_SELECT_SQL = """
SELECT s.id, s.source_id, s.geo, s.payload, s.canonical_url,
       d.body -> 'scope' ->> 'source_class'          AS source_class,
       COALESCE(d.body -> 'scope' -> 'geo', '[]')       AS scope_geo,
       COALESCE(d.body -> 'scope' -> 'languages', '[]') AS scope_languages
  FROM signals s
  LEFT JOIN source_descriptors d ON d.descriptor_id = s.source_id AND d.is_head
 WHERE s.produced_by_kind = 'source'
   AND s.created_at >= $1 AND s.created_at < $2
   AND s.id > $3::uuid
 ORDER BY s.id
 LIMIT $4
"""

_BACKUP_DDL = f"""
CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
    id            uuid PRIMARY KEY,
    geo           text[] NOT NULL,
    payload_geo   jsonb,
    payload_geo_fallback jsonb,
    backed_up_at  timestamptz NOT NULL DEFAULT now()
)
"""

_BACKUP_SQL = f"""
INSERT INTO {BACKUP_TABLE} (id, geo, payload_geo, payload_geo_fallback)
SELECT id, geo, payload -> 'geo', payload -> 'geo_fallback' FROM signals WHERE id = $1
ON CONFLICT (id) DO NOTHING
"""

_UPDATE_SQL = """
UPDATE signals
   SET geo = $2::text[],
       payload = CASE WHEN $3::jsonb IS NULL THEN payload - 'geo'
                      ELSE jsonb_set(payload, '{geo}', $3::jsonb) END
                 - 'geo_fallback'
                 || CASE WHEN $4::jsonb IS NULL THEN '{}'::jsonb
                         ELSE jsonb_build_object('geo_fallback', $4::jsonb) END,
       indexed_at = NULL,
       updated_at = NOW()
 WHERE id = $1
"""


def _json(raw: Any, default: Any) -> Any:
    if isinstance(raw, (str, bytes)):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default
    return raw if raw is not None else default


async def run(
    conn: asyncpg.Connection,
    *,
    since: datetime,
    until: datetime,
    apply: bool = False,
    include_fallback: bool = False,
    hold_title_losses: bool = False,
    batch_rows: int = 500,
    quiet: bool = False,
    quiet_rows: bool = False,
) -> dict[str, Any]:
    def _say(msg: str) -> None:
        if not quiet:
            print(msg)

    counters = {"assessed": 0, "changed": 0, "unchanged": 0, "skip_needs_backend": 0,
                "skip_fallback_only": 0, "skip_title_loss": 0,
                "gain_second_country": 0, "cleared": 0}
    by_country_delta: dict[str, int] = {}
    changes: list[dict[str, Any]] = []
    if apply:
        await conn.execute(_BACKUP_DDL)
    after: Any = "00000000-0000-0000-0000-000000000000"
    while True:
        rows = await conn.fetch(_SELECT_SQL, since, until, after, batch_rows)
        if not rows:
            break
        after = rows[-1]["id"]
        updates: list[tuple[Any, ...]] = []
        for r in rows:
            counters["assessed"] += 1
            d = await assess_row(
                _json(r["payload"], {}), list(r["geo"] or []),
                source_id=r["source_id"], source_class=r["source_class"],
                scope_geo=_json(r["scope_geo"], []),
                scope_languages=_json(r["scope_languages"], []),
                canonical_url=r["canonical_url"], include_fallback=include_fallback,
                hold_title_losses=hold_title_losses,
            )
            counters[d.action] += 1
            if d.action != "changed":
                continue
            if len(d.new_geo) >= 2 and len(d.old_geo) < 2:
                counters["gain_second_country"] += 1
            if not d.new_geo:
                counters["cleared"] += 1
            for c in set(d.new_geo) - set(d.old_geo):
                by_country_delta[c] = by_country_delta.get(c, 0) + 1
            for c in set(d.old_geo) - set(d.new_geo):
                by_country_delta[c] = by_country_delta.get(c, 0) - 1
            changes.append({"id": str(r["id"]), "source_id": r["source_id"],
                            "before": d.old_geo, "after": d.new_geo})
            updates.append((
                r["id"], d.new_geo,
                json.dumps(d.geo_block) if d.geo_block is not None else None,
                json.dumps(d.geo_fallback) if d.geo_fallback is not None else None,
            ))
            if not quiet_rows:
                _say(f" {'FIX ' if apply else 'would'} {r['id']} {r['source_id'][:34]:<34} "
                     f"{d.old_geo} -> {d.new_geo}  {d.reason}")
        if apply and updates:
            async with conn.transaction():
                await conn.executemany(_BACKUP_SQL, [(u[0],) for u in updates])
                await conn.executemany(_UPDATE_SQL, updates)

    result: dict[str, Any] = dict(counters)
    result["country_delta"] = dict(sorted(by_country_delta.items(), key=lambda kv: -abs(kv[1])))
    result["changes"] = changes
    _say("=" * 78)
    _say(" APPLIED (backup: %s)" % BACKUP_TABLE if apply
         else " DRY RUN — no rows changed (re-run with --apply)")
    _say(f" window                     : {since.isoformat()} .. {until.isoformat()}")
    for k in counters:
        _say(f" {k:<27}: {counters[k]:>7,}")
    top = list(result["country_delta"].items())[:12]
    _say(f" country delta (top)        : {top}")
    _say("=" * 78)
    return result


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    ap.add_argument("--since-days", type=int, default=7)
    ap.add_argument("--since", type=str, default=None, help="ISO timestamp (overrides --since-days)")
    ap.add_argument("--until", type=str, default=None, help="ISO timestamp (default: now)")
    ap.add_argument("--include-fallback", action="store_true",
                    help="also tag stored-untagged rows by the publisher-origin fallback")
    ap.add_argument("--hold-title-losses", action="store_true",
                    help="leave a row unchanged when the re-derive would drop a country "
                         "its title/lead names (read without the NER person mask)")
    ap.add_argument("--batch-rows", type=int, default=500)
    ap.add_argument("--quiet-rows", action="store_true")
    args = ap.parse_args()
    now = datetime.now(tz=timezone.utc)
    until = datetime.fromisoformat(args.until) if args.until else now
    since = (datetime.fromisoformat(args.since) if args.since
             else until - timedelta(days=args.since_days))
    if until - since > timedelta(days=31):
        ap.error("window is bounded to 31 days")
    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        await run(conn, since=since, until=until, apply=args.apply,
                  include_fallback=args.include_fallback,
                  hold_title_losses=args.hold_title_losses,
                  batch_rows=args.batch_rows, quiet_rows=args.quiet_rows)
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
