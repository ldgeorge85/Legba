#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-derive which origins share an owner and fill the origin tables.

Counting "independent sources" needs one owner per voice. This derives it at
the finest level that is a real voice (``legba.data.origins``):

* a Telegram item is its CHANNEL, however many channels one source carries;
* an item of an aggregator (GDELT and the like) is the publisher DOMAIN of the
  underlying article, never the aggregator;
* any other source is its publisher: the registrable domain of its address
  (a department path on an umbrella host such as gov.uk is its own owner);
* the deployment's owner map joins what the domain cannot (the same publisher
  on two domains, a proxied and a direct feed, a channel run by an outlet).

    python3 scripts/derive_owner_groups.py [--map FILE] [--show REGEX ...]
    python3 scripts/derive_owner_groups.py --apply

DRY-RUN BY DEFAULT: nothing is written, the session is read-only, and the
before/after summary is printed. ``--apply`` writes through
``origins.sync_derived``: new origins are inserted, derived ones whose rule now
says another group are moved, admin-corrected ones are never touched, every
write is logged, and a second run writes nothing (idempotent).

``--map FILE`` names the owner map (default: ``$LEGBA_OWNER_MAP``, else
``seeds/owner_map.yaml`` if present, else empty; the tracked format example is
``seeds/owner_map.example.yaml``). ``--show REGEX`` (repeatable) prints every
group whose key or member origins match, to inspect a publisher by name.
Connection from ``LEGBA_DATA_PG_*``.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import re
import sys
from collections import Counter, defaultdict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.origins import (  # noqa: E402
    DERIVATION_PRINCIPAL,
    Derivation,
    _source_heads,
    derived_item_assignments,
    derive_ledger_host,
    derive_knowledge_set,
    derive_source,
    sync_derived,
)
from legba.data.owner_map import OwnerMapError, load_owner_map  # noqa: E402

_STORED_SQL = """
SELECT o.key AS origin_key, o.origin_kind, g.key AS group_key, o.rule, o.corrected
  FROM origins o JOIN origin_groups g ON g.id = o.origin_group_id
"""
_LEDGER_HOSTS_SQL = "SELECT source_host FROM source_credibility"
_KNOWLEDGE_SETS_SQL = "SELECT DISTINCT kind FROM seed_batches WHERE kind IS NOT NULL"
TOP = 12


async def _connect(*, read_only: bool):
    import asyncpg

    settings = {"application_name": "legba-derive-owner-groups", "statement_timeout": "300000"}
    if read_only:
        settings["default_transaction_read_only"] = "on"
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
        server_settings=settings,
    )


async def plan(conn, owner_map):
    """``(derivations, item_counts, stored)``: every origin the rules derive now
    (items included), how many stored items sit behind each item-level origin,
    and what the tables hold now. Read-only."""
    heads = await _source_heads(conn)
    derived: list[Derivation] = [
        derive_source(h["descriptor_id"], h["kind"], h["body"], name=h["name"], owner_map=owner_map)
        for h in heads
    ]
    for r in await conn.fetch(_LEDGER_HOSTS_SQL):
        if r["source_host"]:
            derived.append(derive_ledger_host(str(r["source_host"]), owner_map=owner_map))
    for r in await conn.fetch(_KNOWLEDGE_SETS_SQL):
        derived.append(derive_knowledge_set(str(r["kind"])))
    item_counts: dict[str, int] = {}
    for d, n in await derived_item_assignments(conn, heads, owner_map):
        derived.append(d)
        item_counts[d.origin_key] = n
    stored = {r["origin_key"]: dict(r) for r in await conn.fetch(_STORED_SQL)}
    return derived, item_counts, stored


def _groups(pairs):
    groups: dict[str, list[str]] = defaultdict(list)
    for origin_key, group_key in pairs:
        groups[group_key].append(origin_key)
    return groups


def _largest(groups, n=TOP):
    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:n]
    return [(k, len(v)) for k, v in ranked if len(v) > 1]


def summarize(derived, item_counts, stored) -> dict:
    """The before/after numbers (pure)."""
    after_group = {d.origin_key: d.group_key for d in derived}
    # What the tables would hold: every stored row stays (an admin correction
    # keeps its group), derived rows take the derived group.
    merged = {k: v["group_key"] for k, v in stored.items()}
    corrected = {k for k, v in stored.items() if v["corrected"]}
    for k, g in after_group.items():
        if k not in corrected:
            merged[k] = g
    before = {k: v["group_key"] for k, v in stored.items()}

    def kind_of(k):
        return k.split(":", 1)[0]

    out = {"stored_rows": len(stored)}
    for label, mapping in (("before", before), ("after", merged)):
        src = {k: g for k, g in mapping.items() if kind_of(k) == "source"}
        out[label] = {
            "origins": len(mapping),
            "owner_groups": len(set(mapping.values())),
            "sources": len(src),
            "source_owners": len(set(src.values())),
            "largest_groups": _largest(_groups(mapping.items())),
        }
    inserted = sum(1 for k in after_group if k not in stored)
    moved = sum(1 for k, g in after_group.items()
                if k in stored and k not in corrected and stored[k]["group_key"] != g)
    kept = sum(1 for k in after_group if k in corrected)
    out["changes"] = {"inserted": inserted, "moved": moved, "corrected_kept": kept,
                      "unchanged": len(after_group) - inserted - moved - kept}
    out["by_rule"] = dict(Counter(d.rule for d in derived))
    channels = [d for d in derived if d.origin_kind == "telegram_channel"]
    domains = [d for d in derived if d.origin_kind == "publisher_domain"]
    out["telegram_channels"] = {
        "channels": len(channels), "owners": len({d.group_key for d in channels}),
        "items": sum(item_counts.get(d.origin_key, 0) for d in channels),
    }
    out["aggregator_domains"] = {
        "domains": len(domains), "owners": len({d.group_key for d in domains}),
        "items": sum(item_counts.get(d.origin_key, 0) for d in domains),
        "top_by_items": sorted(((d.group_key, item_counts.get(d.origin_key, 0)) for d in domains),
                               key=lambda kv: (-kv[1], kv[0]))[:TOP],
    }
    out["_after"] = merged
    return out


def render(s: dict, shows: list[str]) -> str:
    lines = ["owner groups: dry-run summary", ""]
    for label in ("before", "after"):
        b = s[label]
        lines.append(f"{label}: {b['sources']} sources -> {b['source_owners']} source owners; "
                     f"{b['origins']} origins in {b['owner_groups']} owner groups")
        for k, n in b["largest_groups"]:
            lines.append(f"    {n:5d}  {k}")
    c = s["changes"]
    lines += ["", f"origins to insert {c['inserted']}, move {c['moved']}, "
                  f"unchanged {c['unchanged']}, admin-corrected (kept) {c['corrected_kept']}",
              "by rule: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_rule"].items()))]
    t = s["telegram_channels"]
    lines += ["", f"telegram: {t['channels']} channels -> {t['owners']} owners ({t['items']} items)"]
    a = s["aggregator_domains"]
    lines.append(f"aggregators: {a['domains']} publisher domains -> {a['owners']} owners "
                 f"({a['items']} items); most items:")
    lines += [f"    {n:7d}  {k}" for k, n in a["top_by_items"]]
    merged = s["_after"]
    groups = _groups(merged.items())
    for pattern in shows:
        rx = re.compile(pattern, re.I)
        lines += ["", f"--show {pattern}"]
        hits = [(g, o) for g, o in sorted(groups.items()) if rx.search(g) or any(rx.search(x) for x in o)]
        for g, o in hits:
            lines.append(f"  {g}")
            lines += [f"      {x}" for x in sorted(o)[:8]]
            if len(o) > 8:
                lines.append(f"      ... {len(o) - 8} more")
        if not hits:
            lines.append("  (no group matches)")
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="write the derived owners (default: dry-run)")
    ap.add_argument("--map", help="owner map file (default: $LEGBA_OWNER_MAP or seeds/owner_map.yaml)")
    ap.add_argument("--show", action="append", default=[], metavar="REGEX",
                    help="also list the groups matching this pattern")
    args = ap.parse_args()
    try:
        owner_map = load_owner_map(args.map)
    except OwnerMapError as exc:
        print(f"owner map: {exc}", file=sys.stderr)
        return 2
    try:
        conn = await _connect(read_only=not args.apply)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot connect: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        derived, item_counts, stored = await plan(conn, owner_map)
        summary = summarize(derived, item_counts, stored)
        print(render(summary, args.show))
        print(f"\nowner map: {'loaded' if owner_map else 'empty or absent'}")
        if not args.apply:
            print("\ndry-run: nothing written. Re-run with --apply to write.")
            return 0
        counts = await sync_derived(conn, derived, written_by=DERIVATION_PRINCIPAL)
        print(f"\napplied: {counts}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
