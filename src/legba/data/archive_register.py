# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the evidence archive into ``fetch_versions`` BY HASH (lane B-1).

``evidence_archive`` (migration 0104) holds the evidence archiver's copy of a
cited signal's page, one row per signal, the bytes in the content-addressed
store. ``fetch_versions`` (0245) is the register of fetched bytes since
2026-11-13. They did not join. This pass makes the archive's objects versions
in the one register WITHOUT touching a byte:

* a version row per ``(canonical address of the archived URL, sha256)`` whose
  ``object_ref`` is the archive's existing object (``cas:sha256/<hex>``),
  ``role = 'archiver'``, ``first_seen_at``/``last_seen_at`` = the ARCHIVE time,
  ``registered`` = ``{"from": "evidence_archive", "batch", "action", "at",
  "status_inferred": true}`` — labelled honestly: ARCHIVED LATER, never
  "received at poll". An existing version at the same address and hash (the
  archiver's own N-L row, written with no ``object_ref``) is REUSED; when it
  carries no ``object_ref`` the archive's object is recorded on it
  (``action = "object_ref_filled"``, ``store_status = 'existed'``);
* the signal linked to that version with role ``archived`` and the run's
  ``register_batch``.

Only rows with status ``archived`` (bytes held, the page the archiver meant to
keep) are registered: ``blocked_challenge`` rows hold a challenge wall, not
the page, and ``failed`` / ``skipped_*`` rows hold nothing. Media objects
(``media_object_ref``) are not registered here. Nothing in ``evidence_archive``
or on disk is written, moved or deleted.

DRY-RUN BY DEFAULT: walks the whole archive in keyset batches and counts what
``apply`` would write. ``apply`` writes in bounded batches (each its own
transaction), resumable from the last signal id (``after``) and idempotent (a
linked signal is skipped; versions upsert on their unique key; links are
``ON CONFLICT DO NOTHING``). A sample of each batch's objects is read back
through the archive's reader and its hash verified; mismatches are reported,
never repaired.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ._url_canon import canonical_url
from .archive import cas_object_ref

__all__ = ["MeasureReport", "RegisterReport", "measure_states", "register", "verify_objects"]

REGISTER_ROLE = "archived"
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"

CANDIDATES_SQL = """
SELECT ea.signal_id, ea.sha256, ea.object_ref, ea.size_bytes, ea.content_type, ea.fetched_url,
       coalesce(ea.archived_at, ea.updated_at, ea.created_at) AS archived_at,
       (s.id IS NOT NULL) AS signal_exists,
       EXISTS (SELECT 1 FROM fetch_version_records r
                WHERE r.record_kind = 'signal' AND r.record_id = ea.signal_id
                  AND r.role = 'archived') AS linked
  FROM evidence_archive ea
  LEFT JOIN signals s ON s.id = ea.signal_id
 WHERE ea.status = 'archived' AND ea.object_ref IS NOT NULL AND ea.sha256 IS NOT NULL
   AND ea.signal_id > $1::uuid
 ORDER BY ea.signal_id
 LIMIT $2
"""

EXISTING_SQL = """
SELECT v.id, v.address, v.sha256, (v.object_ref IS NOT NULL) AS has_ref
  FROM fetch_versions v
  JOIN unnest($1::text[], $2::text[]) AS k(address, sha256)
    ON v.address = k.address AND v.sha256 = k.sha256
"""

UPSERT_SQL = """
INSERT INTO fetch_versions (
    receipt_kind, address, url, sha256, hash_basis, role, status, content_type,
    size_bytes, object_ref, store_status, first_seen_at, last_seen_at, registered
)
SELECT 'fetch', a, u, sh, 'entity', 'archiver', 200, ct, sz, ref, 'existed', f, l, reg::jsonb
  FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::bigint[], $6::text[],
              $7::timestamptz[], $8::timestamptz[], $9::text[])
       AS k(a, u, sh, ct, sz, ref, f, l, reg)
ON CONFLICT (address, sha256) DO UPDATE SET
    object_ref   = EXCLUDED.object_ref,
    store_status = 'existed',
    registered   = jsonb_set(EXCLUDED.registered, '{action}', '"object_ref_filled"')
 WHERE fetch_versions.object_ref IS NULL
RETURNING id, address, sha256, (xmax = 0) AS inserted
"""

LINK_SQL = """
INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256, register_batch)
SELECT 'signal', i, 'archived', v, s, $4
  FROM unnest($1::uuid[], $2::bigint[], $3::text[]) AS k(i, v, s)
ON CONFLICT (record_kind, record_id, role) DO NOTHING
RETURNING record_id
"""


@dataclass
class RegisterReport:
    batch: str
    applied: bool
    batches: int = 0
    examined: int = 0
    no_signal: int = 0            # archive rows whose signal row is gone
    already_linked: int = 0
    ref_mismatch: int = 0         # object_ref is not cas:sha256/<sha256> (never registered)
    signals_to_link: int = 0
    objects: int = 0              # distinct sha256 among signals to link
    versions_new: int = 0         # (address, sha256) not yet in fetch_versions
    versions_existing: int = 0    # reused as they are
    versions_ref_filled: int = 0  # existing, no object_ref: the archive's object recorded
    links_written: int = 0
    verified: int = 0
    verify_missing: int = 0
    verify_mismatch: int = 0
    mismatch_sample: list[str] = field(default_factory=list)
    last_signal_id: str | None = None
    complete: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _address(url: str | None) -> str | None:
    if not url:
        return None
    return canonical_url(url) or url


def _group(rows: Iterable[Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """One version per (address, sha256): earliest archive time first-seen,
    latest last-seen, the first row's URL / type / size."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["_address"], str(r["sha256"]))
        g = groups.get(key)
        at = r["archived_at"]
        if g is None:
            groups[key] = {"url": r["fetched_url"], "content_type": r["content_type"],
                           "size": r["size_bytes"], "first": at, "last": at,
                           "signals": [(r["signal_id"], r["sha256"])]}
        else:
            g["first"] = min(g["first"], at)
            g["last"] = max(g["last"], at)
            g["signals"].append((r["signal_id"], r["sha256"]))
    return groups


async def verify_objects(conn: Any, store: Any, shas: Iterable[str], report: RegisterReport) -> None:
    """Read each object back through the archive's reader and check its hash.
    Read-only; a mismatch is counted and named, never repaired."""
    from . import archive_codec  # noqa: PLC0415
    from .archive_store import ColdTierError  # noqa: PLC0415
    from .archive_tier import read_original  # noqa: PLC0415

    for sha in shas:
        stored = store.read_stored(sha)
        if stored is not None:
            ok = archive_codec.decode_verified(stored, sha) is not None
        else:
            try:
                ok = (await read_original(conn, store, sha)) is not None
            except ColdTierError:
                ok = False
            if not ok:
                report.verify_missing += 1
                continue
        report.verified += 1
        if not ok:
            report.verify_mismatch += 1
            if len(report.mismatch_sample) < 20:
                report.mismatch_sample.append(sha)


async def register(
    conn: Any, *, apply: bool, batch_size: int = 2000, max_batches: int = 0,
    after: str | None = None, verify_per_batch: int = 2, store: Any = None,
    batch_id: str | None = None, seed: int = 0,
) -> RegisterReport:
    """One pass (module docstring). ``max_batches`` 0 = until the end."""
    batch = batch_id or f"b1-register-{datetime.now(tz=timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    report = RegisterReport(batch=batch, applied=apply)
    rnd = random.Random(seed)
    cursor = after or _ZERO_UUID
    seen_keys: set[tuple[str, str]] = set()   # versions counted (a dry run writes none)
    seen_shas: set[str] = set()
    while not max_batches or report.batches < max_batches:
        rows = await conn.fetch(CANDIDATES_SQL, cursor, batch_size)
        if not rows:
            report.complete = True
            break
        report.batches += 1
        report.examined += len(rows)
        cursor = str(rows[-1]["signal_id"])
        report.last_signal_id = cursor
        todo = []
        for r in rows:
            if not r["signal_exists"]:
                report.no_signal += 1
            elif r["linked"]:
                report.already_linked += 1
            elif r["object_ref"] != cas_object_ref(str(r["sha256"])) or not _address(r["fetched_url"]):
                report.ref_mismatch += 1
            else:
                d = dict(r)
                d["_address"] = _address(r["fetched_url"])
                todo.append(d)
        report.signals_to_link += len(todo)
        if not todo:
            continue
        groups = _group(todo)
        keys = list(groups)
        new_shas = {sha for _a, sha in keys} - seen_shas
        report.objects += len(new_shas)
        seen_shas |= new_shas
        existing = {(e["address"], e["sha256"]): e for e in await conn.fetch(
            EXISTING_SQL, [k[0] for k in keys], [k[1] for k in keys])}
        for k in keys:
            if k in seen_keys:
                continue
            seen_keys.add(k)
            e = existing.get(k)
            if e is not None and e["has_ref"]:
                report.versions_existing += 1
            elif not apply:   # apply counts these from what the upsert returns
                if e is None:
                    report.versions_new += 1
                else:
                    report.versions_ref_filled += 1
        if store is not None and verify_per_batch > 0:
            shas = sorted({sha for _a, sha in keys})
            await verify_objects(conn, store, rnd.sample(shas, min(verify_per_batch, len(shas))), report)
        if not apply:
            continue
        at = datetime.now(tz=timezone.utc).isoformat()
        reg = json.dumps({"from": "evidence_archive", "batch": batch, "action": "inserted",
                          "at": at, "status_inferred": True}, sort_keys=True)
        up = [k for k in keys if k not in existing or not existing[k]["has_ref"]]
        async with conn.transaction():
            ids: dict[tuple[str, str], int] = {k: int(e["id"]) for k, e in existing.items()}
            if up:
                g = [groups[k] for k in up]
                returned = await conn.fetch(
                    UPSERT_SQL,
                    [k[0] for k in up], [x["url"] for x in g], [k[1] for k in up],
                    [x["content_type"] for x in g], [int(x["size"] or 0) for x in g],
                    [cas_object_ref(k[1]) for k in up], [x["first"] for x in g],
                    [x["last"] for x in g], [reg] * len(up),
                )
                for r in returned:
                    ids[(r["address"], r["sha256"])] = int(r["id"])
                    if r["inserted"]:
                        report.versions_new += 1
                    else:
                        report.versions_ref_filled += 1
            sig_ids, vids, shas = [], [], []
            for k, g in groups.items():
                vid = ids.get(k)
                if vid is None:
                    continue
                for sid, sha in g["signals"]:
                    sig_ids.append(sid)
                    vids.append(vid)
                    shas.append(sha)
            written = await conn.fetch(LINK_SQL, sig_ids, vids, shas, batch)
            report.links_written += len(written)
    return report


# ---------------------------------------------------------------------------
# What a reader would say, over the whole table (read-only measurement)
# ---------------------------------------------------------------------------

MEASURE_SQL = """
WITH received AS (
    SELECT DISTINCT r.record_id
      FROM fetch_version_records r JOIN fetch_versions v ON v.id = r.version_id
     WHERE r.record_kind = 'signal' AND r.role IN ('response', 'page', 'inbound')
       AND v.role <> 'archiver' AND v.object_ref IS NOT NULL AND v.bytes_pruned_at IS NULL
), archived AS (
    SELECT ea.signal_id FROM evidence_archive ea JOIN signals s ON s.id = ea.signal_id
     WHERE ea.status = 'archived' AND ea.object_ref IS NOT NULL AND ea.sha256 IS NOT NULL
)
SELECT (SELECT count(*) FROM signals) AS signals_total,
       (SELECT count(*) FROM received) AS received_bytes,
       (SELECT count(*) FROM archived) AS archive_registrable_signals,
       (SELECT count(*) FROM archived a WHERE NOT EXISTS
            (SELECT 1 FROM received x WHERE x.record_id = a.signal_id)) AS archived_later_only
"""


@dataclass
class MeasureReport:
    signals_total: int
    received_bytes: int
    archived_later_after_register: int
    no_bytes_kept_after_register: int
    archive_registrable_signals: int


async def measure_states(conn: Any) -> MeasureReport:
    """The three labels over every signal, as they WOULD read after a full
    register: received bytes / archived later (archive registrable and no held
    received bytes) / no bytes kept (the rest)."""
    r = await conn.fetchrow(MEASURE_SQL)
    total, rec, arch = int(r["signals_total"]), int(r["received_bytes"]), int(r["archived_later_only"])
    return MeasureReport(total, rec, arch, total - rec - arch, int(r["archive_registrable_signals"]))


def default_store(root: Path | None = None) -> Any:
    from .archive import archive_root  # noqa: PLC0415
    from .archive_store import EvidenceStore  # noqa: PLC0415

    return EvidenceStore.from_env(root if root is not None else archive_root())
