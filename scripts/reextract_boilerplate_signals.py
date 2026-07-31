#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-extraction tool for ALREADY-STORED JS-wall/bot-check/redirect garbage
bodies (V-E1 historical backfill).

planning/VERIFY_PATH_STRUCTURAL_FIXES_SPEC_2026-07-31.md §V-E1: the
``evidence_archiver`` deny-gate (:func:`legba.data.analysts.deterministic_
handlers.evidence_archiver._match_wall_pattern`) rejects a JS-wall/bot-check/
redirect-interstitial body AT EXTRACTION TIME, going forward. It does nothing
for rows the OLD (un-gated) code already wrote. A read-only live-DB audit
(2026-07-31) found the SAME artifact JUDGE_READOUT §5 named ("JavaScript is
disabled in your browser" — Le Monde) sitting in 86 rows
(lemonde.fr x83, press.un.org x3), plus 148 france24.com rows ("One of your
browser extensions seems to be blocking the video player..."), 83 en.irna.ir
rows (a Google-redirect "Transferring to the website..." interstitial), and a
handful of others — all still ``payload.archived_text`` today, actively
grounding judged claims.

This is the SAME class of problem migration 0118 fixed for the Telegram-
widget case (a blanket SQL strip for one host) — but a blanket SQL strip is
not appropriate here: the deny-gate needs to run in PYTHON (the same
:func:`_match_wall_pattern` the live archiver runs, imported directly so the
two can never drift), and a genuine re-extraction attempt is worth making
before concluding a row is unrecoverable. Hence a script, not a migration.

This script finds candidates, reports counts by pattern and by host, and
(with ``--apply``) corrects the STORED row. Bytes are NEVER touched, NEVER
re-fetched over the network — this reads ONLY the content-addressed bytes
the archiver already stored, via the SAME helpers evidence_archiver itself
uses (:mod:`legba.data.archive`).

Per-row correction (``--apply`` only):

  1. Resolve ``signals.object_ref`` -> sha256 -> the CAS path under
     ``LEGBA_ARCHIVE_ROOT``. A row whose CAS object is missing on this host
     is SKIPPED and counted (``bytes_missing``) — never guessed, never
     deleted, never fabricated.
  2. Re-run Trafilatura extraction on those bytes
     (``evidence_archiver._extract_text``) — a genuine RE-extraction, not an
     assumption that the stored text would come out identically — then
     re-check the result against the CURRENT deny gate
     (``evidence_archiver._match_wall_pattern``).
  3. Still a wall (or Trafilatura now finds nothing at all — the expected
     outcome for a genuine no-JS-fallback page: the fetched bytes never
     contained the article, so no amount of re-parsing recovers it) ->
     STRIP ``payload.archived_text`` and ``payload.archived_text_chars``
     entirely (counted ``stripped``). The ``evidence_archive`` sidecar's
     ``text_extracted`` is reset to ``false`` — the row returns to exactly
     the state it would be in had V-E1 existed at original archive time:
     "bytes archived, text not (yet) successfully extracted."
  4. Genuinely clean now (rare — e.g. a decode/library difference) ->
     UPGRADE: write the new text + char count (counted ``upgraded``).
  5. Either outcome trips the corpus dirty-marker contract
     (``indexed_at = NULL`` + ``updated_at = now()`` in the SAME statement —
     see corpus_indexer's DIRTY-MARKER CONTRACT) so corpus_indexer
     re-projects the doc on its next sweep: the garbage text stops being
     served to search/judge consumers within one indexer tick.

DRY-RUN BY DEFAULT — reports only; touches neither the database nor the
archive filesystem. ``--apply`` is an OPERATOR-GATED data op — per house
discipline this script does not invoke itself with ``--apply``; an operator
runs that explicitly after reviewing the dry-run report.

Run in the registry container (runtime deps present) against the live Postgres:

  docker exec -e LEGBA_DATA_PG_DB=legba \\
      -e LEGBA_DATA_PG_HOST=legba-postgres-1 \\
      -e LEGBA_ARCHIVE_ROOT=/var/lib/legba/archive \\
      -e PYTHONPATH=/app/src \\
      <registry-container> python3 /app/scripts/reextract_boilerplate_signals.py

Or on the host with the repo + archive volume mounted:

  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      LEGBA_ARCHIVE_ROOT=/var/lib/legba/archive \\
      python3 scripts/reextract_boilerplate_signals.py        # dry-run
  ... --apply                                                 # execute

Flags:
  --apply          execute (default: dry-run, read-only, no filesystem access)
  --batch-rows N   candidate rows per SELECT (default 500)
  --limit N        stop after scanning N candidate rows
  --quiet-rows     suppress per-row garbage lines (summary only)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

# Make `legba` importable when run from a checkout.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg

from legba.data.analysts.deterministic_handlers.evidence_archiver import (
    _DEFAULT_MAX_TEXT_CHARS,
    _extract_text,
    _match_wall_pattern,
)
from legba.data.archive import archive_root, cas_path, sha256_from_object_ref


async def _connect_pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


def _host(url: str | None) -> str:
    """Netloc of ``url``, lowered; a stable, low-cardinality grouping key."""
    if not url:
        return "(no-url)"
    try:
        return urlsplit(url).netloc.lower() or "(no-host)"
    except ValueError:
        return "(unparseable)"


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


# Broad pre-filter (any row carrying archived_text); the exact deny-pattern
# match happens in Python via the SAME gate the live archiver runs.
_CANDIDATES_SQL = """
SELECT id, canonical_url, payload, object_ref
  FROM signals
 WHERE payload ? 'archived_text'
   AND id > $1
 ORDER BY id
 LIMIT $2
"""

# Still garbage on re-extraction: remove BOTH V-E2 fields, trip the corpus
# dirty-marker contract (indexed_at = NULL + updated_at = now() together —
# see corpus_indexer's DIRTY-MARKER CONTRACT).
_STRIP_SIGNAL_SQL = """
UPDATE signals
   SET payload = payload - '{archived_text,archived_text_chars}'::text[],
       indexed_at = NULL,
       updated_at = now()
 WHERE id = $1
"""

# Genuinely clean on re-extraction: replace both fields, same dirty-marker
# contract.
_UPGRADE_SIGNAL_SQL = """
UPDATE signals
   SET payload = jsonb_set(
                   jsonb_set(payload, '{archived_text}', to_jsonb($2::text)),
                   '{archived_text_chars}', to_jsonb($3::int)
                 ),
       indexed_at = NULL,
       updated_at = now()
 WHERE id = $1
"""

_SIDECAR_STRIP_SQL = """
UPDATE evidence_archive
   SET text_extracted = false,
       last_error = $2,
       updated_at = now()
 WHERE signal_id = $1
"""

_SIDECAR_UPGRADE_SQL = """
UPDATE evidence_archive
   SET text_extracted = true,
       last_error = NULL,
       updated_at = now()
 WHERE signal_id = $1
"""


async def run(
    conn: asyncpg.Connection,
    *,
    apply: bool = False,
    batch_rows: int = 500,
    limit: int | None = None,
    quiet: bool = False,
    quiet_rows: bool = False,
    archive_root_override: Any = None,
) -> dict[str, Any]:
    """Scan (and with ``apply=True`` correct) every candidate row. Returns
    counters (+ ``by_pattern`` / ``by_host`` breakdowns and a bounded
    ``samples`` list) for both human review and test assertions."""

    def _say(msg: str) -> None:
        if not quiet:
            print(msg)

    counters: dict[str, int] = {
        "candidates_scanned": 0,
        "garbage_found": 0,
        "bytes_missing": 0,
        "no_object_ref": 0,
        "stripped": 0,
        "upgraded": 0,
    }
    by_pattern: Counter[str] = Counter()
    by_host: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []

    root = archive_root_override if archive_root_override is not None else archive_root()
    after: Any = "00000000-0000-0000-0000-000000000000"

    while True:
        want = batch_rows
        if limit is not None:
            want = min(want, limit - counters["candidates_scanned"])
            if want <= 0:
                break
        rows = await conn.fetch(_CANDIDATES_SQL, after, want)
        if not rows:
            break
        after = rows[-1]["id"]

        for r in rows:
            counters["candidates_scanned"] += 1
            payload = _parse_payload(r["payload"])
            text = payload.get("archived_text")
            if not isinstance(text, str):
                continue
            pattern = _match_wall_pattern(text)
            if pattern is None:
                continue

            counters["garbage_found"] += 1
            host = _host(r["canonical_url"])
            by_pattern[pattern] += 1
            by_host[host] += 1
            samples.append({
                "id": str(r["id"]), "host": host, "pattern": pattern,
                "canonical_url": r["canonical_url"],
            })
            if not quiet_rows:
                _say(f" garbage   {r['id']} {host:<32} pattern={pattern!r}")

            if not apply:
                continue

            digest = sha256_from_object_ref(r["object_ref"])
            if digest is None:
                counters["no_object_ref"] += 1
                continue
            obj_path = cas_path(root, digest)
            if not obj_path.exists():
                counters["bytes_missing"] += 1
                continue

            body = obj_path.read_bytes()
            new_text = _extract_text(body, None, max_chars=_DEFAULT_MAX_TEXT_CHARS)
            new_pattern = _match_wall_pattern(new_text) if new_text is not None else None
            still_wall = new_text is None or new_pattern is not None

            async with conn.transaction():
                if still_wall:
                    reason = (
                        f"reextract_boilerplate_signals: still matches {new_pattern!r}"
                        if new_pattern is not None
                        else "reextract_boilerplate_signals: re-extraction found no text"
                    )
                    await conn.execute(_STRIP_SIGNAL_SQL, r["id"])
                    await conn.execute(_SIDECAR_STRIP_SQL, r["id"], reason)
                    counters["stripped"] += 1
                else:
                    await conn.execute(
                        _UPGRADE_SIGNAL_SQL, r["id"], new_text, len(new_text),
                    )
                    await conn.execute(_SIDECAR_UPGRADE_SQL, r["id"])
                    counters["upgraded"] += 1

    result: dict[str, Any] = dict(counters)
    result["by_pattern"] = dict(by_pattern)
    result["by_host"] = dict(by_host)
    result["samples"] = samples[:50]

    _say("=" * 78)
    mode = "APPLIED" if apply else "DRY RUN — no rows changed (re-run with --apply)"
    _say(f" {mode}")
    _say(f" candidates scanned (archived_text present)  : {counters['candidates_scanned']:>7,}")
    _say(f" garbage bodies found (deny-pattern match)    : {counters['garbage_found']:>7,}")
    _say(" by pattern:")
    for pat, n in by_pattern.most_common():
        _say(f"   {n:>6,}  {pat}")
    _say(" by host:")
    for host, n in by_host.most_common():
        _say(f"   {n:>6,}  {host}")
    if apply:
        _say(f" stripped (still garbage on re-extraction)    : {counters['stripped']:>7,}")
        _say(f" upgraded (genuinely clean on re-extraction)  : {counters['upgraded']:>7,}")
        _say(f" bytes missing (CAS object absent, skipped)   : {counters['bytes_missing']:>7,}")
        _say(f" no object_ref (skipped)                      : {counters['no_object_ref']:>7,}")
    _say("=" * 78)
    return result


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Find + (with --apply) correct ALREADY-STORED JS-wall/"
        "bot-check/redirect garbage bodies in signals.payload.archived_text "
        "(V-E1 historical backfill). Dry-run by default.",
    )
    ap.add_argument("--apply", action="store_true",
                    help="execute (default: dry-run, read-only)")
    ap.add_argument("--batch-rows", type=int, default=500)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--quiet-rows", action="store_true",
                    help="suppress per-row garbage lines")
    args = ap.parse_args()

    conn = await _connect_pg()
    try:
        await run(
            conn,
            apply=args.apply,
            batch_rows=args.batch_rows,
            limit=args.limit,
            quiet_rows=args.quiet_rows,
        )
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
