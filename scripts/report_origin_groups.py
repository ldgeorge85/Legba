#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The derived origin groups, and where independence counting WOULD change if
it counted origin groups — READ-ONLY (data-model lane 9).

Derives every origin from what the database holds (``legba.data.origins``:
source heads, the licence ledger, the knowledge sets) WITHOUT writing it, then
measures three places that count "independent" sources today:

1. ``statement_independence`` (gate G7): each source is its own group until
   groups exist — how many sources share a group with another source;
2. ``edge_qualification``: folds sources by the publisher segment of the id
   (``source.<publisher>.<feed>``) — where that fold and the origin groups
   disagree;
3. ``events.distinct_source_count`` (shown to desks as "sources=N"): how many
   events would count fewer origins than sources, and how many multi-source
   events would fall to one origin.

    python3 scripts/report_origin_groups.py [--json] [--list]

``--list`` adds the known-wrong cases by name (public publisher hosts and
descriptor ids only). Connection from ``LEGBA_DATA_PG_*``; the session is
``default_transaction_read_only`` with a 120 s statement timeout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import sys
from collections import Counter, defaultdict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.origins import TWO_LABEL_SUFFIXES, derived_assignments  # noqa: E402

_ACTIVE_SOURCES_SQL = "SELECT descriptor_id FROM source_descriptors WHERE is_head AND state = 'active'"

#: One pass over signal_event_links (no signals join: the link row carries
#: source_id). The mapping arrives as two parallel arrays.
EVENTS_SQL = """
WITH m AS (SELECT * FROM unnest($1::text[], $2::text[]) AS m(source_id, gkey)),
per AS (
    SELECT l.event_id,
           count(DISTINCT NULLIF(l.source_id, '')) AS d,
           count(DISTINCT COALESCE(m.gkey, 'source:' || l.source_id))
               FILTER (WHERE NULLIF(l.source_id, '') IS NOT NULL) AS g
      FROM signal_event_links l
      LEFT JOIN m ON m.source_id = l.source_id
     GROUP BY l.event_id
)
SELECT count(*) AS events,
       count(*) FILTER (WHERE per.d >= 2) AS multi_source,
       count(*) FILTER (WHERE per.g < per.d) AS fewer_origins,
       count(*) FILTER (WHERE per.d >= 2 AND per.g < 2) AS multi_to_one,
       count(*) FILTER (WHERE e.updated_at >= now() - interval '30 days') AS events_30d,
       count(*) FILTER (WHERE e.updated_at >= now() - interval '30 days' AND per.g < per.d)
           AS fewer_origins_30d,
       count(*) FILTER (WHERE e.updated_at >= now() - interval '30 days'
                          AND per.d >= 2 AND per.g < 2) AS multi_to_one_30d
  FROM per JOIN events e ON e.id = per.event_id
"""

#: A host whose second-level label looks like a public suffix the built-in
#: list does not know (``agency.gov.xx`` -> ``gov.xx``) is grouped too wide.
_SUSPECT_SLD = re.compile(r"^(?:gov|gob|go|govt|gouv|mil|ac|edu|co|com|org|net|or|ne|nic)\.[a-z]{2}$")


def _family(source_id: str) -> str:
    parts = source_id.split(".")
    return parts[1] if len(parts) > 1 else source_id


async def _connect():
    import asyncpg

    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
        server_settings={
            "default_transaction_read_only": "on",
            "statement_timeout": "120000",
            "application_name": "legba-origin-groups-report",
        },
    )


async def build(conn) -> dict:
    derived = await derived_assignments(conn)
    active = {r["descriptor_id"] for r in await conn.fetch(_ACTIVE_SOURCES_SQL)}
    by_kind = Counter(d.origin_kind for d in derived)
    by_rule = Counter(d.rule for d in derived)
    groups: dict[str, list] = defaultdict(list)
    for d in derived:
        groups[d.group_key].append(d)
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    src = [d for d in derived if d.origin_kind == "source"]
    src_group = {d.ref: d.group_key for d in src}

    # 1. G7: active sources that share a group with another active source.
    active_groups = Counter(src_group[s] for s in active if s in src_group)
    g7_collapsing = sorted(s for s in active if active_groups.get(src_group.get(s), 0) > 1)

    # 2. edge_qualification's publisher-segment fold vs origin groups (active).
    fam_to_groups: dict[str, set] = defaultdict(set)
    grp_to_fams: dict[str, set] = defaultdict(set)
    for s in active:
        if s in src_group:
            fam_to_groups[_family(s)].add(src_group[s])
            grp_to_fams[src_group[s]].add(_family(s))
    fold_splits = {f: sorted(g) for f, g in fam_to_groups.items() if len(g) > 1}
    fold_merges = {g: sorted(f) for g, f in grp_to_fams.items() if len(f) > 1}

    # 3. events.
    ids = sorted(src_group)
    ev = await conn.fetchrow(EVENTS_SQL, ids, [src_group[i] for i in ids])

    suspect = sorted({d.group_key for d in derived if d.group_key.startswith("host:")
                      and _SUSPECT_SLD.match(".".join(d.group_key[5:].split(".")[-2:]))
                      and ".".join(d.group_key[5:].split(".")[-2:]) not in TWO_LABEL_SUFFIXES})
    bundles = [(d.ref, d.detail.get("channels")) for d in src if d.rule == "telegram_bundle"]
    selfs = sorted(d.ref for d in src if d.rule == "descriptor_self")
    return {
        "origins": len(derived),
        "origins_by_kind": dict(by_kind),
        "origins_by_rule": dict(by_rule),
        "groups": len(groups),
        "groups_with_more_than_one_origin": len(multi),
        "origins_in_shared_groups": sum(len(v) for v in multi.values()),
        "active_sources": len(active),
        "active_source_groups": len(active_groups),
        "g7_active_sources_sharing_a_group": len(g7_collapsing),
        "edge_fold_publisher_segments_split_by_groups": len(fold_splits),
        "edge_fold_groups_merging_publisher_segments": len(fold_merges),
        "events": dict(ev) if ev else {},
        "known_wrong": {
            "telegram_bundles": bundles,
            "descriptor_self": selfs,
            "suspect_suffix_groups": suspect,
        },
        "_lists": {
            "multi_groups": {k: sorted(d.origin_key for d in v) for k, v in sorted(multi.items())},
            "fold_splits": fold_splits,
            "fold_merges": fold_merges,
        },
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    try:
        conn = await _connect()
    except Exception as exc:  # noqa: BLE001
        print(f"cannot connect: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        async with conn.transaction(readonly=True):
            report = await build(conn)
    finally:
        await conn.close()
    if not args.list:
        report.pop("_lists")
        report["known_wrong"] = {k: len(v) for k, v in report["known_wrong"].items()}
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
