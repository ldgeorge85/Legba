#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Un-alias the FALSE tier-1 collapses of valueless-identifier URLs — ONCE, dry-run first.

``canonical_url()`` used to drop every valueless query key, so the
SPIP CMS's ``/spip.php?article<N>`` canonicalised to ``/spip.php`` and ingest
tier 1 aliased every lefaso.net article to the first one (live 11-13: 1,505
of 1,508 rows pointing at 6bd8e160, ``reason='ingest_url'``). The rule now
keeps a valueless key that carries a digit (``legba.data._url_canon``). This
script re-derives each affected row's canonical under the CURRENT rule and
repairs the links tier 1 got wrong.

What it does, per row of the listed hosts, in ``(fetched_at, id)`` order —
the order ingest saw them:

* the row's correct canonical is the EARLIEST row of the same tenant whose
  current canonical form equals its own (a real re-ingest of one article
  stays an alias, re-pointed at that article's first row), resolved
  transitively; with no earlier match the row is its own canonical;
* only a link tier 1 wrote (``signal_aliases.reason = 'ingest_url'``) is ever
  removed or re-pointed — a row linked by any other producer or reason is
  left as it is and counted;
* every row a change touches is first copied (its ``canonical_signal_id`` and
  every ``signal_aliases`` row it is the alias of) into
  ``signal_alias_backup_n_u`` (``ON CONFLICT DO NOTHING``: the first,
  pre-repair values are the ones kept, so a re-run never overwrites them);
* the update NULLs ``indexed_at`` (the 0082 corpus dirty-marker) so the corpus
  re-projects the row, and is guarded on the ``canonical_signal_id`` it read,
  so a concurrent writer is never overwritten;
* IDEMPOTENT: a repaired set re-derives to what it now has (``unchanged``).

Rows are never deleted. Hosts default to the two whose canonical form the N-U
rule changes on the live set (lefaso.net, sawatdeenetwork.com); ``--host``
replaces the list.

Run AFTER the code carrying the new rule is deployed and AFTER
``scripts/backfill_signal_url_hash.py --apply --recompute`` (inside the
runtime container, DB env from the container)::

  python3 scripts/unalias_valueless_id_urls_once.py              # dry run
  python3 scripts/unalias_valueless_id_urls_once.py --apply      # execute
  python3 scripts/unalias_valueless_id_urls_once.py              # re-run: 0 to change
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import asyncpg  # noqa: E402

from legba.data._url_canon import canonical_url, strip_www  # noqa: E402

BACKUP_TABLE = "signal_alias_backup_n_u"
DEFAULT_HOSTS = ("lefaso.net", "sawatdeenetwork.com")
TIER1_REASON = "ingest_url"
PRODUCED_BY = "n_u_unalias_valueless_id"

_SELECT_SQL = """
SELECT s.id, s.owner_tenant, s.canonical_url, s.canonical_signal_id, s.fetched_at,
       COALESCE((SELECT json_agg(json_build_object(
                    'canonical_signal_id', a.canonical_signal_id, 'reason', a.reason,
                    'score', a.score, 'produced_by', a.produced_by,
                    'produced_at', a.produced_at))
                   FROM signal_aliases a WHERE a.alias_signal_id = s.id), '[]') AS links
  FROM signals s
 WHERE s.canonical_url ~* $1
 ORDER BY s.fetched_at, s.id
"""

_BACKUP_DDL = f"""
CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
    id                  uuid PRIMARY KEY,
    canonical_signal_id uuid,
    alias_links         jsonb NOT NULL,
    backed_up_at        timestamptz NOT NULL DEFAULT now()
)
"""

_BACKUP_SQL = f"""
INSERT INTO {BACKUP_TABLE} (id, canonical_signal_id, alias_links)
VALUES ($1, $2, $3::jsonb)
ON CONFLICT (id) DO NOTHING
"""

_DELETE_LINK_SQL = """
DELETE FROM signal_aliases
 WHERE alias_signal_id = $1 AND canonical_signal_id = $2 AND reason = $3
"""

_UPDATE_SQL = """
UPDATE signals
   SET canonical_signal_id = $2, indexed_at = NULL
 WHERE id = $1 AND canonical_signal_id IS NOT DISTINCT FROM $3
"""

_LINK_SQL = """
INSERT INTO signal_aliases (alias_signal_id, canonical_signal_id, reason, score, produced_by)
VALUES ($1, $2, $3, 1.0, $4)
ON CONFLICT (alias_signal_id, canonical_signal_id) DO NOTHING
"""


def host_pattern(hosts: tuple[str, ...]) -> str:
    """The POSIX regex selecting stored URLs on ``hosts`` (``www.`` optional)."""
    alt = "|".join(re.escape(h) for h in hosts)
    return rf"^[a-z]+://(www\.)?({alt})([:/?#]|$)"


@dataclass
class Change:
    id: UUID
    old: UUID | None
    new: UUID
    links: list[dict[str, Any]]
    drop: UUID | None          # the tier-1 link removed (its canonical), if any
    url: str


@dataclass
class Plan:
    assessed: int = 0
    unchanged: int = 0
    restored_self: int = 0     # a false alias that becomes its own canonical
    repointed: int = 0         # a true duplicate re-pointed at its article's first row
    skipped_other_link: int = 0
    changes: list[Change] = field(default_factory=list)


def _links(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    return list(raw or [])


def plan(rows: list[dict[str, Any]], hosts: tuple[str, ...]) -> Plan:
    """Pure. ``rows`` in ``(fetched_at, id)`` order."""
    out = Plan()
    first_of: dict[tuple[str, str], UUID] = {}
    resolved: dict[UUID, UUID] = {}
    for r in rows:
        url = r["canonical_url"] or ""
        try:
            host = strip_www(urlsplit(url.strip()).hostname or "")
        except ValueError:
            continue
        if host not in hosts:
            continue
        out.assessed += 1
        sid = r["id"]
        key = (r["owner_tenant"] or "", canonical_url(url))
        first = first_of.setdefault(key, sid)
        desired = sid if first == sid else resolved.get(first, first)
        resolved[sid] = desired
        current = r["canonical_signal_id"]
        if current is None or current in (sid, desired):
            # Not an alias (or already right): this pass only repairs links
            # tier 1 wrote; it never mints a new alias for an unlinked row.
            out.unchanged += 1
            resolved[sid] = sid if current in (None, sid) else current
            continue
        links = _links(r["links"])
        mine = [ln for ln in links if str(ln.get("canonical_signal_id")) == str(current)]
        if not mine or any(ln.get("reason") != TIER1_REASON for ln in mine):
            out.skipped_other_link += 1
            resolved[sid] = current
            continue
        out.changes.append(Change(id=sid, old=current, new=desired, links=links,
                                  drop=current, url=url))
        if desired == sid:
            out.restored_self += 1
        else:
            out.repointed += 1
    return out


async def apply_plan(conn: asyncpg.Connection, p: Plan, *, batch: int = 500) -> int:
    await conn.execute(_BACKUP_DDL)
    done = 0
    for i in range(0, len(p.changes), batch):
        chunk = p.changes[i:i + batch]
        async with conn.transaction():
            await conn.executemany(_BACKUP_SQL, [
                (c.id, c.old, json.dumps(c.links, default=str)) for c in chunk])
            for c in chunk:
                if c.drop is not None:
                    await conn.execute(_DELETE_LINK_SQL, c.id, c.drop, TIER1_REASON)
                status = await conn.execute(_UPDATE_SQL, c.id, c.new, c.old)
                if status.endswith(" 1"):
                    done += 1
                    if c.new != c.id:
                        await conn.execute(_LINK_SQL, c.id, c.new, TIER1_REASON, PRODUCED_BY)
    return done


async def run(conn: asyncpg.Connection, *, hosts: tuple[str, ...], apply: bool = False,
              quiet: bool = False, show: int = 10) -> Plan:
    rows = [dict(r) for r in await conn.fetch(_SELECT_SQL, host_pattern(hosts))]
    p = plan(rows, hosts)
    say = (lambda *_: None) if quiet else print
    say("=" * 78)
    say(f" {'APPLY' if apply else 'DRY RUN — nothing written (re-run with --apply)'}"
        f"  hosts={list(hosts)}  backup={BACKUP_TABLE}")
    for k in ("assessed", "unchanged", "restored_self", "repointed", "skipped_other_link"):
        say(f" {k:<20}: {getattr(p, k):>7,}")
    say(f" changes             : {len(p.changes):>7,}")
    for c in p.changes[:show]:
        say(f"   {c.id} {c.old} -> {c.new}  {c.url[:70]}")
    if apply and p.changes:
        n = await apply_plan(conn, p)
        say(f" applied             : {n:>7,}  (a guard miss = a concurrent writer; re-run)")
    say("=" * 78)
    return p


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    ap.add_argument("--host", action="append", default=None,
                    help=f"a host to repair (repeatable; default {list(DEFAULT_HOSTS)})")
    ap.add_argument("--show", type=int, default=10, help="changes to print")
    args = ap.parse_args()
    hosts = tuple(h.lower().removeprefix("www.") for h in (args.host or DEFAULT_HOSTS))
    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        await run(conn, hosts=hosts, apply=args.apply, show=args.show)
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
