# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``archive_retention`` sub-handler — expire the stored bytes of UNCITED archive rows.

The evidence archive exists to preserve the bytes behind CITED evidence. Until
this sweep nothing ever removed an object: the archive volume only grew. This is
the deterministic rule that gives it a ceiling without touching a byte anybody
relies on.

THE RULE
--------
An ``evidence_archive`` row is EXPIRABLE when ALL of these hold:

  * its status says bytes are held (``archived`` or ``blocked_challenge``);
  * its row is older than ``retention_days`` (default 45), by
    ``COALESCE(archived_at, created_at)``, strictly: a row exactly at the
    boundary is kept, one a second older is eligible;
  * its signal is cited by NOTHING: no ``analyst_outputs`` row (findings and
    every other output kind), no ``facts`` row and no ``events`` row carries the
    signal id in ``derived_from``. The brief names findings; the rule reads the
    wider set because the sweep is irreversible and a fact or an event built on
    a signal is a citation of it too. A CITED row is never touched.

An expirable row has its stored object(s) removed from the archive volume and
the row KEPT with ``status = 'expired_uncited'`` (migration 0224). The row keeps
``object_ref``, ``sha256``, ``fetched_url`` and ``license_class``: what was held,
what its hash was and why it went stay answerable. ``signals.object_ref`` is
cleared in the same transaction (only when it still names the expired object),
because the read surfaces derive ``archived`` from that column and must not
claim bytes that are gone. ``retention_class`` is NOT touched (see HONESTY).

SAFETY, IN ORDER
----------------
1. DEFAULT OFF. ``LEGBA_ARCHIVE_RETENTION`` unset/``0`` => the handler returns a
   zeroed receipt (``flag_off=1``) and touches neither the database nor the
   volume. It is not a ``retention_days=0`` convention; the flag is the switch.
2. Every candidate is RE-CHECKED under a row lock immediately before it is
   expired (same predicate, ``FOR UPDATE``): a row that was cited or changed
   between the scan and the action is skipped and counted ``skipped_changed``.
3. The object is removed AFTER the transaction commits. If the unlink fails the
   failure direction is an ORPHAN FILE (bytes outlive their row), never a row
   that claims bytes which are gone; the row is marked in ``last_error``.
4. Objects are CONTENT ADDRESSED and shared: two signals with identical bytes
   point at one file, and the same file can be a FETCH RECEIPT's bytes too. A
   file is removed only when nothing else that keeps bytes still references
   the address (:data:`SHARER_CHECKS`): no other non-expired archive row
   (``object_ref`` or ``media_object_ref``), no ``fetch_versions`` row still
   holding it (``object_ref`` set, ``bytes_pruned_at`` unset — the receipt
   pruner and the cold tier own those bytes), no ``observations`` row naming
   it (``sha256`` / ``fetch_sha256``). Each check runs only when its table or
   column exists (the migrations are additive). ``signals.object_ref`` needs no
   probe of its own: every signal carrying one has its own archive row with
   the same ref (measured live 2026-11-13: 0 exceptions of 180,358) and that
   row is the one checked above — and the one being expired clears its own.
   A retired object's COLD copy is never touched here.
5. The path is derived only from a parsed 64-hex digest
   (:func:`legba.data.archive.sha256_from_object_ref`); a foreign or malformed
   ref is never turned into a path.
6. No usable archive root => nothing is expired (``skipped_no_root=1``).
7. ``dry_run`` (option) counts what WOULD expire and writes nothing.

HONESTY (what is not built)
---------------------------
  * A signal that becomes cited AFTER its bytes expired is not re-archived: the
    sidecar row is terminal, exactly as ``skipped_*`` rows are. A re-evaluation
    pass is a declared seam (docs/SEAMS.md #42), not built.
  * ``signals.retention_class`` stays ``evidence_hold``, so the signal ROW is
    still exempt from ``signals_retention``. Only the bytes expire. Downgrading
    the hold is a separate operator decision.
  * Between the shared-file check and the unlink, a concurrent archiver run that
    stores identical bytes for a NEW signal could find the file present (dedup
    hit) and then lose it. The window is milliseconds and only reachable when
    bytes identical to an expired, uncited object are archived again; closing it
    needs a lock the archiver would also have to take. Not built.
  * A non-filesystem object store is not built (SEAMS #42).

Output ``data`` keys (the cadence receipt):
    flag_off, dry_run, retention_days, batch_limit
    skipped_no_root       1 when the archive root is missing/unwritable
    eligible              rows the scan returned this run (<= batch_limit)
    batch_full            1 when the scan hit batch_limit (more may remain)
    expired               rows moved to expired_uncited (0 in a dry run)
    skipped_changed       rows no longer eligible when re-checked under lock
    objects_removed       files unlinked
    objects_shared_kept   files kept because another live row still uses them
    objects_missing       files already absent (the desired end state)
    unlink_failed         files that could not be removed (orphans; see 3)
    bytes_removed         bytes of the files unlinked (dry run: bytes of the
                          eligible rows' recorded sizes, an upper bound)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

from ....runtime.analyst_method import AnalystMethodResult
from ...archive import archive_root, cas_path, sha256_from_object_ref
from ...provenance.models import FindingPayload

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "archive_retention"

#: The master switch. Default OFF: unset, empty, or anything but a truthy token.
ENABLED_ENV = "LEGBA_ARCHIVE_RETENTION"

#: Age, in days, past which an uncited archive row's bytes may expire.
DEFAULT_RETENTION_DAYS = 45

#: Rows expired per run. Each is its own short transaction, so this bounds the
#: run's wall time, not any lock.
DEFAULT_BATCH_LIMIT = 200

#: Statuses whose rows hold bytes on the volume (``blocked_challenge`` stores the
#: interstitial). Every other status has no object to remove.
BYTE_HOLDING_STATUSES: tuple[str, ...] = ("archived", "blocked_challenge")

#: Tables whose ``derived_from`` array makes a signal CITED. Each has a GIN index
#: on the column (checked live), so the anti-join is an index probe, not a scan.
#: Constants interpolated into SQL below; never operator input.
CITING_TABLES: tuple[str, ...] = ("analyst_outputs", "facts", "events")

_TRUTHY = ("1", "true", "yes", "on")


def retention_enabled() -> bool:
    """``LEGBA_ARCHIVE_RETENTION`` — default OFF."""
    return str(os.getenv(ENABLED_ENV, "")).strip().lower() in _TRUTHY


def _not_cited_probe_sql() -> str:
    """Per-row form: one GIN probe per citing table. Right for ONE signal (the
    lock re-check); measured 22 s over the whole archive, so not used for the scan."""
    return "\n".join(
        f"       AND NOT EXISTS (SELECT 1 FROM {t} c_{t}"
        f" WHERE c_{t}.derived_from @> ARRAY[ea.signal_id])"
        for t in CITING_TABLES
    )


def _not_cited_set_sql() -> str:
    """Set form for the scan: every lineage element of every citing table, once,
    anti-joined to the (small) candidate side with a hash — 0.9 s live against
    22 s for the per-row probe, because most candidates are cited and a probe per
    candidate per table is paid in full."""
    union = "\n        UNION ALL ".join(
        f"SELECT unnest(derived_from) AS sid FROM {t}" for t in CITING_TABLES
    )
    return (
        "       AND NOT EXISTS (SELECT 1 FROM (\n        "
        + union
        + "\n       ) cited WHERE cited.sid = ea.signal_id)"
    )


# $1 = byte-holding statuses, $2 = retention days. The age comparison is strict
# (<): a row exactly retention_days old is KEPT.
_AGE_PREDICATE = """
       ea.status = ANY($1::text[])
       AND COALESCE(ea.archived_at, ea.created_at)
           < now() - make_interval(days => $2::int)
"""

#: The scan, oldest first. $3 = batch limit.
SELECT_ELIGIBLE_SQL = f"""
    SELECT ea.signal_id, ea.object_ref, ea.media_object_ref,
           ea.size_bytes, ea.media_size_bytes
      FROM evidence_archive ea
     WHERE {_AGE_PREDICATE}
{_not_cited_set_sql()}
     ORDER BY COALESCE(ea.archived_at, ea.created_at) ASC
     LIMIT $3
"""

#: The same predicate re-evaluated for ONE row under a row lock. $3 = the signal id.
LOCK_ELIGIBLE_SQL = f"""
    SELECT ea.signal_id
      FROM evidence_archive ea
     WHERE ea.signal_id = $3
       AND {_AGE_PREDICATE}
{_not_cited_probe_sql()}
       FOR UPDATE OF ea
"""

#: Keeps object_ref / sha256 / url / licence as the receipt; only the status moves.
EXPIRE_ROW_SQL = """
    UPDATE evidence_archive
       SET status = 'expired_uncited',
           last_error = NULL,
           updated_at = now()
     WHERE signal_id = $1
"""

#: The read surfaces derive ``archived`` from this column; clear it only when it
#: still names the object being removed.
CLEAR_SIGNAL_REF_SQL = """
    UPDATE signals SET object_ref = NULL
     WHERE id = $1 AND object_ref = $2
"""

#: Does any OTHER live row still point at this content address? (Runs inside the
#: expiring transaction, so the row being expired no longer counts.) The
#: partial indexes from migration 0224 make this a probe. $1 = object_ref,
#: $2 = its 64-hex sha256. The base check is the archive itself; the sharer
#: checks below are OR-ed on when their table/column exists.
REFERENCED_ELSEWHERE_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM evidence_archive
         WHERE status <> 'expired_uncited'
           AND (object_ref = $1 OR media_object_ref = $1)
    ) OR (false AND $2::text IS NULL)   -- binds $2 (the sharer checks' sha256); folds away
"""

#: The OTHER keepers of a content address: ``(presence probe, OR-clause)``.
#: A fetch version that still holds the bytes (indexed on sha256), and an
#: observation naming them. Constants interpolated into SQL; never input.
SHARER_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "SELECT to_regclass('public.fetch_versions') IS NOT NULL",
        " OR EXISTS (SELECT 1 FROM fetch_versions fv WHERE fv.sha256 = $2"
        " AND fv.object_ref IS NOT NULL AND fv.bytes_pruned_at IS NULL)",
    ),
    (
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
        " AND table_name = 'observations' AND column_name = 'sha256')",
        " OR EXISTS (SELECT 1 FROM observations ob WHERE ob.sha256 = $2)",
    ),
    (
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
        " AND table_name = 'observations' AND column_name = 'fetch_sha256')",
        " OR EXISTS (SELECT 1 FROM observations ob2 WHERE ob2.fetch_sha256 = $2)",
    ),
)


async def referenced_elsewhere_sql(conn: Any) -> str:
    """:data:`REFERENCED_ELSEWHERE_SQL` with every sharer check whose table or
    column exists on this database OR-ed on."""
    sql = REFERENCED_ELSEWHERE_SQL.rstrip()
    for probe, clause in SHARER_CHECKS:
        if await conn.fetchval(probe):
            sql += "\n      " + clause
    return sql + "\n"

MARK_UNLINK_FAILED_SQL = """
    UPDATE evidence_archive SET last_error = $2 WHERE signal_id = $1
"""


def _zero_counters(
    *, retention_days: int, batch_limit: int, dry_run: bool
) -> dict[str, int]:
    return {
        "flag_off": 0,
        "dry_run": int(dry_run),
        "retention_days": retention_days,
        "batch_limit": batch_limit,
        "skipped_no_root": 0,
        "eligible": 0,
        "batch_full": 0,
        "expired": 0,
        "skipped_changed": 0,
        "objects_removed": 0,
        "objects_shared_kept": 0,
        "objects_missing": 0,
        "unlink_failed": 0,
        "bytes_removed": 0,
    }


def _usable_root() -> Path | None:
    """The archive root when it exists and is writable; ``None`` otherwise. Never
    created here — a missing root means the volume is not mounted, and expiring
    rows we cannot reach the bytes of would lie."""
    root = archive_root()
    try:
        if root.is_dir() and os.access(root, os.W_OK):
            return root
    except OSError:
        pass
    logger.warning(
        "archive_retention.no_root — archive root %s missing or unwritable; "
        "nothing expired this tick.", root,
    )
    return None


def _positive_int(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _remove_object(root: Path, ref: str | None, counters: dict[str, int]) -> str | None:
    """Unlink the CAS file behind ``ref``. Returns an error string on failure."""
    digest = sha256_from_object_ref(ref)
    if digest is None:
        # A foreign / malformed ref is never turned into a path.
        return None
    path = cas_path(root, digest)
    try:
        size = path.stat().st_size
        path.unlink()
    except FileNotFoundError:
        counters["objects_missing"] += 1
        return None
    except OSError as exc:
        counters["unlink_failed"] += 1
        return f"archive_retention unlink_failed: {path.name}: {exc}"
    counters["objects_removed"] += 1
    counters["bytes_removed"] += int(size)
    return None


async def _expire_one(
    pool: Any, root: Path, row: Mapping[str, Any], *, days: int,
    counters: dict[str, int], referenced_sql: str | None = None,
) -> None:
    signal_id = row["signal_id"]
    to_remove: list[str] = []
    async with pool.acquire() as conn:
        if referenced_sql is None:
            referenced_sql = await referenced_elsewhere_sql(conn)
        async with conn.transaction():
            locked = await conn.fetchrow(
                LOCK_ELIGIBLE_SQL, list(BYTE_HOLDING_STATUSES), days, signal_id
            )
            if locked is None:
                counters["skipped_changed"] += 1
                return
            await conn.execute(EXPIRE_ROW_SQL, signal_id)
            for ref in {r for r in (row["object_ref"], row["media_object_ref"]) if r}:
                if row["object_ref"] == ref:
                    await conn.execute(CLEAR_SIGNAL_REF_SQL, signal_id, ref)
                if await conn.fetchval(referenced_sql, ref, sha256_from_object_ref(ref)):
                    counters["objects_shared_kept"] += 1
                else:
                    to_remove.append(ref)
    counters["expired"] += 1
    # AFTER commit: the failure direction is an orphan file, never a lying row.
    errors = [e for e in (_remove_object(root, r, counters) for r in to_remove) if e]
    if errors:
        async with pool.acquire() as conn:
            await conn.execute(MARK_UNLINK_FAILED_SQL, signal_id, "; ".join(errors)[:1000])


async def _sweep(pool: Any, options: Mapping[str, Any]) -> dict[str, int]:
    days = _positive_int(options.get("retention_days"), DEFAULT_RETENTION_DAYS)
    limit = _positive_int(options.get("batch_limit"), DEFAULT_BATCH_LIMIT)
    dry_run = bool(options.get("dry_run", False))
    counters = _zero_counters(retention_days=days, batch_limit=limit, dry_run=dry_run)

    root = _usable_root()
    if root is None:
        counters["skipped_no_root"] = 1
        return counters

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            SELECT_ELIGIBLE_SQL, list(BYTE_HOLDING_STATUSES), days, limit
        )
    counters["eligible"] = len(rows)
    counters["batch_full"] = int(len(rows) >= limit)

    if dry_run:
        counters["bytes_removed"] = sum(
            int(r["size_bytes"] or 0) + int(r["media_size_bytes"] or 0) for r in rows
        )
        return counters

    async with pool.acquire() as conn:
        referenced_sql = await referenced_elsewhere_sql(conn)
    for row in rows:
        await _expire_one(pool, root, row, days=days, counters=counters,
                          referenced_sql=referenced_sql)
    return counters


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    if counters.get("flag_off"):
        title = "Archive retention: off (LEGBA_ARCHIVE_RETENTION unset); nothing examined"
    elif counters.get("skipped_no_root"):
        title = "Archive retention: archive root unusable; nothing expired"
    else:
        verb = "would expire" if counters.get("dry_run") else "expired"
        n = counters.get("eligible", 0) if counters.get("dry_run") else counters.get("expired", 0)
        title = (
            f"Archive retention: {verb} {n} uncited row(s) older than "
            f"{counters.get('retention_days', 0)} d, "
            f"{counters.get('objects_removed', 0)} object(s) removed"
        )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "archive_retention"]
    if counters.get("expired", 0):
        tags.append("expired")
    if counters.get("unlink_failed", 0):
        tags.append("unlink_failed")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Flag off => a zeroed receipt, no database or volume access at all. The
    ``inputs`` slice is ignored; the unit of work is the next batch of expirable
    rows, read directly via ``deps.pg_pool``. Usage is always zeroed."""
    days = _positive_int(options.get("retention_days"), DEFAULT_RETENTION_DAYS)
    limit = _positive_int(options.get("batch_limit"), DEFAULT_BATCH_LIMIT)
    counters = _zero_counters(
        retention_days=days, batch_limit=limit, dry_run=bool(options.get("dry_run", False))
    )
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if not retention_enabled():
        counters["flag_off"] = 1
    elif pool is not None:
        try:
            counters = await _sweep(pool, options)
        except Exception as exc:
            logger.warning("archive_retention.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "handle",
    "retention_enabled",
    "SUB_HANDLER_NAME",
    "ENABLED_ENV",
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_BATCH_LIMIT",
    "BYTE_HOLDING_STATUSES",
    "CITING_TABLES",
    "REFERENCED_ELSEWHERE_SQL",
    "SHARER_CHECKS",
    "referenced_elsewhere_sql",
]
