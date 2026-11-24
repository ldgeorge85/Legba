#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retire evidence-archive objects older than the hot window to a cold backend.

DRY-RUN BY DEFAULT: prints what would be packed (per month: objects, bytes,
bundles) and writes nothing. ``--apply`` packs, uploads, verifies, records
and only then removes hot copies. Policy and the bundle format are in
:mod:`legba.data.archive_tier`; the backend in
:mod:`legba.data.archive_store` (``LEGBA_ARCHIVE_COLD_BACKEND``).
Operator guide: docs/operations/archive-tiering.md.

REFUSES (exit 2) when no cold backend is configured, or when migration 0243
(``evidence_archive_cold`` / ``evidence_archive_bundles``) is not applied.

Order per bundle — the never-lose order: read each hot object and verify it
against its address (a mismatching object is reported and NOT packed) →
build the tar + index → verify the local file → upload (idempotent: the key
is the tar's hash) → download it back in full and decode-verify every member
→ record the bundle and every member's location in ONE transaction → append
the locations to the local cold index → per object, re-check no row inside
the hot window cites it now, then remove the hot copy and stamp
``hot_removed_at``. ``--keep-hot`` stops before removal (a copy-only pass).
A failure at any step before the record leaves the hot copies untouched and
nothing recorded (an uploaded but unrecorded bundle is never read); a
failure after it leaves a cold copy AND a hot copy, and the next ``--apply``
resumes the removal (recorded rows with ``hot_removed_at`` unset).

OTHER MODES (each needs the backend and the migration, like the mover):

* ``--verify`` — download every RECORDED bundle in full and re-verify it (the
  bundle hash, every member against its address, the index stored beside
  it, the member counts against ``evidence_archive_cold``). Writes nothing.
* ``--restore SHA [SHA ...]`` / ``--restore-month YYYY-MM`` /
  ``--restore-all`` — bring retired objects back to the hot tier (each
  re-verified), clearing ``hot_removed_at``.
* ``--copy-from PREFIX`` — "blob storage that can be anywhere": copy every
  recorded bundle from the backend described by the ``PREFIX``-renamed env
  vars (``LEGBA_ARCHIVE_SRC_COLD_BACKEND``, ``LEGBA_ARCHIVE_SRC_S3_ENDPOINT``,
  ... for ``PREFIX=LEGBA_ARCHIVE_SRC_``) to the configured backend, verifying
  each copy there. Bundle keys are content hashes, so no row changes; the
  repoint is then only the readers' env.
* ``--rebuild-index`` — rewrite the local cold index
  (``{root}/cold-index/``, the DB-free locator) from the database.

Exit codes: 0 clean, 1 something failed (reported in the JSON), 2 refused.

Run inside the runtime image (asyncpg + botocore + zstandard), with the
archive volume and the database env::

    docker run --rm --network legba_default --env-file .env \\
        -v legba_archive:/var/lib/legba/archive -v "$PWD/scripts:/s:ro" \\
        --entrypoint python legba/legba-runtime-dapr:latest /s/archive_tier.py [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from legba.data import archive_tier as tier
except ImportError:  # a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from legba.data import archive_tier as tier
from legba.data.archive import archive_root
from legba.data.archive_store import (
    BlobBackend,
    ColdBackendNotConfigured,
    ColdLocation,
    EvidenceStore,
    LocalColdIndex,
    cold_backend_from_env,
)


class Refused(RuntimeError):
    """The mover will not run (no backend, no migration)."""


def plan_packs(store: EvidenceStore, rows: list[Any], pack_bytes: int) -> tuple[list[tuple[str, list[str]]], dict]:
    """Group candidate addresses by month and fill bundles up to ``pack_bytes``
    of at-rest bytes. Returns ``(packs, summary)``; hot-missing objects are
    counted, not packed (nothing to move)."""
    by_month: dict[str, list[tuple[str, int]]] = defaultdict(list)
    summary: dict[str, Any] = {"candidates": len(rows), "hot_missing": 0, "months": {}}
    for r in rows:
        sha = r["sha"]
        try:
            size = store.path(sha).stat().st_size
        except FileNotFoundError:
            summary["hot_missing"] += 1
            continue
        by_month[r["month"]].append((sha, size))
    packs: list[tuple[str, list[str]]] = []
    for month in sorted(by_month):
        cur: list[str] = []
        cur_bytes = 0
        for sha, size in by_month[month]:
            if cur and cur_bytes + size > pack_bytes:
                packs.append((month, cur))
                cur, cur_bytes = [], 0
            cur.append(sha)
            cur_bytes += size
        if cur:
            packs.append((month, cur))
        summary["months"][month] = {
            "objects": len(by_month[month]),
            "stored_bytes": sum(s for _, s in by_month[month]),
            "bundles": sum(1 for m, _ in packs if m == month),
        }
    return packs, summary


async def run_tier(conn: Any, store: EvidenceStore, backend: BlobBackend | None,
                   policy: tier.TierPolicy, *, apply: bool, limit: int, spool: Path,
                   keep_hot: bool = False) -> dict:
    if backend is None:
        raise Refused("no cold backend configured (LEGBA_ARCHIVE_COLD_BACKEND) — nothing moves")
    if not await conn.fetchval(tier.TABLES_PRESENT_SQL):
        raise Refused("migration 0243 (evidence_archive_cold / _bundles) is not applied")
    # N-L: fetch-receipt versions (migration 0245) are candidates too, once present.
    with_fetch = bool(await conn.fetchval(tier.FETCH_VERSIONS_PRESENT_SQL))
    still_sql = tier.STILL_COLD_ELIGIBLE_WITH_FETCH_SQL if with_fetch else tier.STILL_COLD_ELIGIBLE_SQL
    rows = await conn.fetch(
        tier.CANDIDATES_WITH_FETCH_SQL if with_fetch else tier.CANDIDATES_SQL, policy.hot_days, limit,
    )
    packs, summary = plan_packs(store, list(rows), policy.pack_bytes)
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run", "backend": backend.name,
        "hot_days": policy.hot_days, "pack_bytes": policy.pack_bytes, **summary,
        "bundles_planned": len(packs), "bundles_uploaded": 0, "bundles_failed": [],
        "objects_tiered": 0, "stored_bytes_tiered": 0, "original_bytes_tiered": 0,
        "hot_removed": 0, "hot_kept_recent": 0, "resumed_checked": 0,
        "mismatches": [], "bytes_freed": 0,
    }
    if not apply:
        return report
    spool.mkdir(parents=True, exist_ok=True)
    # A killed earlier pass leaves its half-built bundle behind (C-1 measured
    # 548 MB); it is never trusted (nothing recorded points at it) — clear it.
    # One mover at a time per spool directory.
    for pattern in ("????-??.*.tar", "*.readback", "*.verify", "*.copy"):
        for stale in spool.glob(pattern):
            stale.unlink(missing_ok=True)
            report["stale_spool_removed"] = report.get("stale_spool_removed", 0) + 1
    index_file = LocalColdIndex(store.root)
    handled: set[str] = set()
    for month, shas in packs:
        members: list[tuple[str, bytes, int]] = []
        for sha in shas:
            stored = store.read_stored(sha)
            if stored is None:
                report["hot_missing"] += 1
                continue
            try:
                data, orig = tier.cold_member_bytes(stored, sha)
            except ValueError:
                report["mismatches"].append(sha)
                continue
            members.append((sha, data, orig))
        if not members:
            continue
        bundle_path = spool / f"{month}.{os.getpid()}.tar"
        try:
            index = tier.build_bundle(month, members, bundle_path)
            problems = tier.verify_bundle_file(bundle_path, index)
            if not problems:
                problems = tier.upload_and_verify(backend, bundle_path, index, spool)
        except Exception as exc:  # backend down, disk full: nothing recorded, hot kept
            problems = [f"{type(exc).__name__}: {exc}"[:500]]
        finally:
            bundle_path.unlink(missing_ok=True)
        if problems:
            report["bundles_failed"].append({"month": month, "problems": problems[:20]})
            continue
        report["bundles_uploaded"] += 1
        async with conn.transaction():
            await conn.execute(
                tier.INSERT_BUNDLE_SQL, index.key, backend.name, month, index.bundle_sha256,
                len(index.members), index.stored_bytes, index.original_bytes,
            )
            for sha, (off, length, codec) in index.members.items():
                await conn.execute(tier.INSERT_COLD_SQL, sha, index.key, off, length, codec)
        # The DB-free locator follows the record (never precedes it).
        index_file.record((sha, index.location(sha)) for sha in index.members)
        report["objects_tiered"] += len(index.members)
        report["stored_bytes_tiered"] += index.stored_bytes
        report["original_bytes_tiered"] += index.original_bytes
        if keep_hot:
            continue
        for sha in index.members:
            handled.add(sha)
            await _remove_hot(conn, store, sha, still_sql, policy, report)
    if not keep_hot:
        # Resume: recorded cold objects whose hot copy survived an earlier
        # pass (a crash between record and removal, or a --keep-hot pass).
        for r in await conn.fetch(tier.PENDING_HOT_REMOVAL_SQL, limit):
            if r["sha256"] in handled:
                continue
            report["resumed_checked"] += 1
            await _remove_hot(conn, store, r["sha256"], still_sql, policy, report)
    return report


async def _remove_hot(conn: Any, store: EvidenceStore, sha: str, still_sql: str,
                      policy: tier.TierPolicy, report: dict) -> None:
    """Remove one hot copy whose cold copy is recorded — after re-checking that
    no row inside the hot window cites it now."""
    if not await conn.fetchval(still_sql, sha, policy.hot_days):
        report["hot_kept_recent"] += 1
        return
    path = store.path(sha)
    try:
        size = path.stat().st_size
        path.unlink()
    except FileNotFoundError:
        await conn.execute(tier.MARK_HOT_REMOVED_SQL, sha)   # gone already: stamp it
        return
    report["hot_removed"] += 1
    report["bytes_freed"] += size
    await conn.execute(tier.MARK_HOT_REMOVED_SQL, sha)


async def verify_all(conn: Any, backend: BlobBackend, spool: Path) -> dict:
    """Re-verify every recorded bundle on ``backend``. Writes nothing."""
    rep: dict[str, Any] = {"mode": "verify", "backend": backend.name, "bundles": 0,
                           "verified": 0, "members": 0, "failed": [], "unreachable": None}
    for b in await conn.fetch(tier.RECORDED_BUNDLES_SQL):
        rep["bundles"] += 1
        members = await conn.fetch(tier.BUNDLE_MEMBERS_SQL, b["bundle_key"])
        problems: list[str] = []
        if not (int(b["member_count"]) == int(b["cold_rows"]) == len(members)):
            problems.append(f"member counts disagree: bundle {b['member_count']}, "
                            f"cold rows {b['cold_rows']}")
        try:
            index = tier.index_from_rows(b, members)
            problems.extend(tier.verify_remote_bundle(backend, index, spool))
        except Exception as exc:  # transport: the backend, not the bundle
            rep["unreachable"] = f"{type(exc).__name__}: {exc}"[:500]
            rep["failed"].append({"bundle": b["bundle_key"], "problems": ["backend unreachable"]})
            break
        rep["members"] += len(members)
        if problems:
            rep["failed"].append({"bundle": b["bundle_key"], "problems": problems[:20]})
        else:
            rep["verified"] += 1
    return rep


_RESTORE_SELECT_SQL = """
SELECT c.sha256 FROM evidence_archive_cold c JOIN evidence_archive_bundles b USING (bundle_key)
 WHERE c.hot_removed_at IS NOT NULL AND ($1::text IS NULL OR b.month = $1)
 ORDER BY c.sha256
"""


async def restore(conn: Any, store: EvidenceStore, *, shas: list[str] | None = None,
                  month: str | None = None, everything: bool = False) -> dict:
    """Rehydrate retired objects to the hot tier (each re-verified)."""
    rep: dict[str, Any] = {"mode": "restore", "selected": 0, "restored": 0, "already_hot": 0,
                           "failed": []}
    if shas:
        rows: list[Any] = [{"sha256": s.lower()} for s in shas]
    else:
        rows = list(await conn.fetch(_RESTORE_SELECT_SQL, None if everything else month))
    for r in rows:
        sha = r["sha256"]
        rep["selected"] += 1
        if store.path(sha).exists():
            rep["already_hot"] += 1
            continue
        try:
            body = await tier.read_original(conn, store, sha, rehydrate=True)
        except Exception as exc:  # ColdTierError and transport: reported, never marked lost
            rep["failed"].append({"sha256": sha, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        if body is None or not store.path(sha).exists():
            rep["failed"].append({"sha256": sha, "error": "not recorded as cold"})
            continue
        rep["restored"] += 1
        await conn.execute(tier.CLEAR_HOT_REMOVED_SQL, sha)
    return rep


async def copy_from(conn: Any, src: BlobBackend, dst: BlobBackend, spool: Path) -> dict:
    """Copy every recorded bundle from ``src`` to ``dst``, verifying each on ``dst``."""
    rep: dict[str, Any] = {"mode": "copy", "from": src.name, "to": dst.name, "bundles": 0,
                           "copied_verified": 0, "failed": []}
    for b in await conn.fetch(tier.RECORDED_BUNDLES_SQL):
        rep["bundles"] += 1
        index = tier.index_from_rows(b, await conn.fetch(tier.BUNDLE_MEMBERS_SQL, b["bundle_key"]))
        try:
            problems = tier.copy_bundle(src, dst, index, spool)
        except Exception as exc:
            problems = [f"{type(exc).__name__}: {exc}"[:300]]
        if problems:
            rep["failed"].append({"bundle": b["bundle_key"], "problems": problems})
        else:
            rep["copied_verified"] += 1
    return rep


async def rebuild_index(conn: Any, root: Path) -> dict:
    rows = await conn.fetch(tier.ALL_COLD_LOCATIONS_SQL)
    n = LocalColdIndex(root).rewrite(
        (r["sha256"], ColdLocation(r["bundle_key"], int(r["member_offset"]), int(r["member_length"])))
        for r in rows
    )
    return {"mode": "rebuild-index", "entries": n}


def backend_from_prefix(prefix: str, env: dict[str, str] | None = None) -> BlobBackend:
    """A backend described by ``PREFIX``-renamed env vars (``--copy-from``)."""
    e = os.environ if env is None else env
    renamed = {"LEGBA_ARCHIVE_" + k[len(prefix):]: v for k, v in e.items() if k.startswith(prefix)}
    return cold_backend_from_env(renamed)


async def _amain(args: argparse.Namespace) -> int:
    policy = tier.TierPolicy.from_env()
    if args.hot_days:
        policy = tier.TierPolicy(args.hot_days, policy.pack_bytes, policy.cold_backend)
    try:
        backend = cold_backend_from_env()
    except ColdBackendNotConfigured as exc:
        print(f"archive_tier: REFUSED — {exc}", file=sys.stderr)
        return 2
    import asyncpg  # noqa: PLC0415 — runtime image only

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        if not await conn.fetchval(tier.TABLES_PRESENT_SQL):
            raise Refused("migration 0243 (evidence_archive_cold / _bundles) is not applied")
        spool = Path(args.spool)
        if args.verify:
            report = await verify_all(conn, backend, spool)
        elif args.restore or args.restore_month or args.restore_all:
            report = await restore(conn, EvidenceStore(Path(args.root), cold=backend),
                                   shas=args.restore, month=args.restore_month,
                                   everything=args.restore_all)
        elif args.copy_from:
            report = await copy_from(conn, backend_from_prefix(args.copy_from), backend, spool)
        elif args.rebuild_index:
            report = await rebuild_index(conn, Path(args.root))
        else:
            report = await run_tier(
                conn, EvidenceStore(Path(args.root)), backend, policy, apply=args.apply,
                limit=args.limit, spool=spool, keep_hot=args.keep_hot,
            )
    except (Refused, ColdBackendNotConfigured) as exc:
        print(f"archive_tier: REFUSED — {exc}", file=sys.stderr)
        return 2
    finally:
        await conn.close()
    print(json.dumps(report, indent=2, default=str))
    return 1 if report.get("failed") or report.get("bundles_failed") else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(archive_root()))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=20000, help="max objects considered this run")
    ap.add_argument("--hot-days", type=int, default=0, help="override LEGBA_ARCHIVE_HOT_DAYS")
    ap.add_argument("--spool", default="/tmp/legba-archive-tier", help="bundle build dir")
    ap.add_argument("--keep-hot", action="store_true", help="copy to cold, keep hot copies")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="re-verify every recorded bundle")
    mode.add_argument("--restore", nargs="+", metavar="SHA", help="rehydrate these objects")
    mode.add_argument("--restore-month", metavar="YYYY-MM", help="rehydrate one month's objects")
    mode.add_argument("--restore-all", action="store_true", help="rehydrate every retired object")
    mode.add_argument("--copy-from", metavar="PREFIX",
                      help="copy every recorded bundle from the PREFIX-described backend here")
    mode.add_argument("--rebuild-index", action="store_true",
                      help="rewrite the local cold index from the database")
    return asyncio.run(_amain(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
