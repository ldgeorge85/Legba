# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.archive_tier — hot/cold tiering for the evidence archive.

POLICY (env knobs, docs/TUNABLES.md):

* ``LEGBA_ARCHIVE_HOT_DAYS`` (default 90) — an object stays on the host while
  ANY row that references it (``evidence_archive.sha256`` or
  ``media_sha256``) is younger than this.
* ``LEGBA_ARCHIVE_COLD_BACKEND`` (``local`` | ``s3``; unset = no cold tier, the
  mover refuses) — see :func:`legba.data.archive_store.cold_backend_from_env`.
* ``LEGBA_ARCHIVE_PACK_MB`` (default 512) — the at-rest size a bundle is
  filled to before a new one starts.

BUNDLES: older objects are packed into MONTHLY bundles (the month of the
newest row referencing the object). A bundle is a plain POSIX tar — an
operator can ``tar -x`` it with no Legba code — of members
``objects/<sha[:2]>/<sha>`` holding each object's at-rest bytes (one zstd frame
per member; an identity object is compressed on the way in), plus a final
``index.json`` member. The index maps ``sha256 → [offset, length, codec]``,
where offset/length address the member's DATA inside the tar, so a read is
one ranged GET. The bundle is content-addressed: its key is
``bundles/<YYYY-MM>/<sha256 of the tar>.tar`` and the same index is also
stored beside it as ``….tar.index.json``.

LOCATION: an object's cold location is recorded per CONTENT ADDRESS in
``evidence_archive_cold`` (migration 0243), not per row: one stored object
serves every row that cites the same bytes (and the media leg), so a
sha-keyed location is the one that can never disagree with itself, and every
row reaches it through the ``sha256``/``object_ref`` it already carries.

NEVER-LOSE ORDER (the mover, ``scripts/archive_tier.py``): build → upload →
read the whole bundle BACK and decode-verify every member against its sha256
and the bundle against its own hash → record bundle + locations (one
transaction) → only then remove each hot copy, after re-checking that no
newer row has started citing it. Nothing in this module deletes from a cold
backend; retiring is allowed, deleting is not.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import archive_codec
from .archive_store import BlobBackend, ColdLocation, EvidenceStore

HOT_DAYS_ENV = "LEGBA_ARCHIVE_HOT_DAYS"
PACK_MB_ENV = "LEGBA_ARCHIVE_PACK_MB"
DEFAULT_HOT_DAYS = 90
DEFAULT_PACK_MB = 512
INDEX_MEMBER = "index.json"
BUNDLE_FORMAT = "legba-archive-bundle/1"


@dataclass(frozen=True)
class TierPolicy:
    hot_days: int = DEFAULT_HOT_DAYS
    pack_bytes: int = DEFAULT_PACK_MB << 20
    cold_backend: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "TierPolicy":
        e = os.environ if env is None else env

        def _pos(name: str, default: int) -> int:
            try:
                v = int(e.get(name, default))
            except (TypeError, ValueError):
                return default
            return v if v > 0 else default

        return cls(
            hot_days=_pos(HOT_DAYS_ENV, DEFAULT_HOT_DAYS),
            pack_bytes=_pos(PACK_MB_ENV, DEFAULT_PACK_MB) << 20,
            cold_backend=(e.get("LEGBA_ARCHIVE_COLD_BACKEND") or "").strip().lower(),
        )


@dataclass
class BundleIndex:
    month: str
    members: dict[str, tuple[int, int, str]] = field(default_factory=dict)
    bundle_sha256: str = ""
    stored_bytes: int = 0
    original_bytes: int = 0

    @property
    def key(self) -> str:
        return f"bundles/{self.month}/{self.bundle_sha256}.tar"

    def to_json(self) -> bytes:
        return json.dumps({
            "format": BUNDLE_FORMAT, "month": self.month,
            "original_bytes": self.original_bytes,
            "members": {k: list(v) for k, v in sorted(self.members.items())},
        }, sort_keys=True).encode()

    def location(self, sha256_hex: str) -> ColdLocation:
        off, length, _codec = self.members[sha256_hex]
        return ColdLocation(self.key, off, length)


def member_name(sha256_hex: str) -> str:
    return f"objects/{sha256_hex[:2]}/{sha256_hex}"


def cold_member_bytes(stored: bytes, sha256_hex: str, *, level: int | None = None) -> tuple[bytes, int]:
    """The member bytes for an object (always one zstd frame when the binding
    is present) and the ORIGINAL size. Raises ``ValueError`` when ``stored``
    does not decode to ``sha256_hex`` — a mismatching object is never packed."""
    body = archive_codec.decode_verified(stored, sha256_hex)
    if body is None:
        raise ValueError(f"object {sha256_hex} does not verify; not packed")
    if archive_codec.is_zstd(stored) and archive_codec.stored_codec(stored, sha256_hex) == archive_codec.CODEC_ZSTD:
        return stored, len(body)
    return archive_codec.encode(body, level=level).data, len(body)


def build_bundle(month: str, members: Iterable[tuple[str, bytes, int]], out_path: Path) -> BundleIndex:
    """Write the tar at ``out_path`` from ``(sha256, member_bytes,
    original_size)`` and return its index (offsets read back from the tar)."""
    index = BundleIndex(month=month)
    names: dict[str, str] = {}
    with tarfile.open(out_path, "w", format=tarfile.PAX_FORMAT) as tar:
        for sha, data, orig in members:
            if sha in names:
                continue
            info = tarfile.TarInfo(member_name(sha))
            info.size = len(data)
            info.mode = 0o444
            tar.addfile(info, io.BytesIO(data))
            names[sha] = info.name
            index.original_bytes += orig
    # Offsets come from READING the tar back — the format decides them, not us.
    with tarfile.open(out_path, "r") as tar:
        by_name = {m.name: m for m in tar.getmembers()}
    for sha, name in names.items():
        m = by_name[name]
        data_head = _read_range(out_path, m.offset_data, 4)
        codec = archive_codec.CODEC_ZSTD if archive_codec.is_zstd(data_head) else archive_codec.CODEC_IDENTITY
        index.members[sha] = (m.offset_data, m.size, codec)
        index.stored_bytes += m.size
    # Append the index as the last member (self-describing bundle).
    payload = index.to_json()
    with tarfile.open(out_path, "a", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(INDEX_MEMBER)
        info.size = len(payload)
        info.mode = 0o444
        tar.addfile(info, io.BytesIO(payload))
    index.bundle_sha256 = _sha256_file(out_path)
    return index


def _read_range(path: Path, offset: int, length: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(length)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_bundle_file(path: Path, index: BundleIndex) -> list[str]:
    """Every member decodes to its sha256 and the file hashes to the bundle
    id. Returns the problems (empty = verified)."""
    problems: list[str] = []
    if _sha256_file(path) != index.bundle_sha256:
        problems.append("bundle sha256 mismatch")
    for sha, (off, length, _codec) in index.members.items():
        member = _read_range(path, off, length)
        if len(member) != length or archive_codec.decode_verified(member, sha) is None:
            problems.append(f"member {sha} does not verify")
    return problems


def upload_and_verify(backend: BlobBackend, bundle_path: Path, index: BundleIndex,
                      scratch: Path) -> list[str]:
    """Upload the bundle (+ its index), download it back in full and verify.
    The upload is idempotent: a bundle key IS its content hash.

    C-1: when the key was ALREADY present and what it holds does not verify
    (an earlier upload that was corrupted in flight and never recorded), the
    damaged object would otherwise squat the content-addressed key forever —
    ``put`` never overwrites. The backend's ``replace_damaged`` (the one
    exception to never-overwrite: only an object that does not hash to its own
    key, only by bytes that do) repairs it, and the read-back runs again."""
    created = backend.put_file(index.key, bundle_path)
    backend.put(index.key + ".index.json", index.to_json())
    problems = _readback_problems(backend, index, scratch)
    if problems and not created:
        repair = getattr(backend, "replace_damaged", None)
        if repair is not None and repair(index.key, bundle_path, index.bundle_sha256):
            problems = _readback_problems(backend, index, scratch)
    return problems


def _readback_problems(backend: BlobBackend, index: BundleIndex, scratch: Path) -> list[str]:
    back = scratch / f"{index.bundle_sha256}.readback"
    try:
        backend.get_to_file(index.key, back)
        return verify_bundle_file(back, index)
    finally:
        back.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# SQL (migration 0243) + the cold-aware read
# ---------------------------------------------------------------------------

TABLES_PRESENT_SQL = (
    "SELECT to_regclass('public.evidence_archive_cold') IS NOT NULL "
    "AND to_regclass('public.evidence_archive_bundles') IS NOT NULL"
)

#: Content addresses whose EVERY referencing row is older than the hot window
#: and which are not yet cold; oldest first. $1 = hot_days, $2 = limit.
CANDIDATES_SQL = """
WITH refs AS (
    SELECT sha256 AS sha, coalesce(archived_at, updated_at) AS at
      FROM evidence_archive WHERE sha256 IS NOT NULL AND object_ref IS NOT NULL
    UNION ALL
    SELECT media_sha256, coalesce(archived_at, updated_at)
      FROM evidence_archive WHERE media_sha256 IS NOT NULL AND media_object_ref IS NOT NULL
)
SELECT r.sha, max(r.at) AS newest, to_char(max(r.at) AT TIME ZONE 'UTC', 'YYYY-MM') AS month
  FROM refs r
 WHERE NOT EXISTS (SELECT 1 FROM evidence_archive_cold c WHERE c.sha256 = r.sha)
 GROUP BY r.sha
HAVING max(r.at) < now() - make_interval(days => $1)
 ORDER BY newest
 LIMIT $2
"""

#: Re-check just before a hot copy is removed: still no row inside the hot
#: window. $1 = sha, $2 = hot_days. Matched through ``object_ref`` /
#: ``media_object_ref`` (``cas:sha256/<sha>``), which carry the partial
#: indexes from 0104/0224, NOT through ``sha256``/``media_sha256`` (no index:
#: C-1 measured the per-object sequential scan at ~10 ms on 54k rows, i.e.
#: minutes per 25k-object pass, and it grows with the archive). The two
#: always agree (measured live 2026-11-13: 0 rows where object_ref differs
#: from 'cas:sha256/' || sha256, same for media); a row with a ref is the
#: only kind CANDIDATES_SQL counts anyway.
STILL_COLD_ELIGIBLE_SQL = """
SELECT NOT EXISTS (
    SELECT 1 FROM evidence_archive
     WHERE (object_ref = 'cas:sha256/' || $1 OR media_object_ref = 'cas:sha256/' || $1)
       AND coalesce(archived_at, updated_at) >= now() - make_interval(days => $2)
) AND EXISTS (SELECT 1 FROM evidence_archive_cold WHERE sha256 = $1)
"""

#: N-L (migration 0245): fetch-receipt bytes join the cold tier. A held
#: fetch version that survives the retention pruner (it is REFERENCED —
#: legba.data.fetch_receipt_retention; the pruner runs at 30 days, inside this
#: 90-day hot window) ages out here like an archived page. Same shape as the
#: two statements above, with the version's last sighting as its age.
FETCH_VERSIONS_PRESENT_SQL = "SELECT to_regclass('public.fetch_versions') IS NOT NULL"

CANDIDATES_WITH_FETCH_SQL = CANDIDATES_SQL.replace(
    "      FROM evidence_archive WHERE media_sha256 IS NOT NULL AND media_object_ref IS NOT NULL\n)",
    "      FROM evidence_archive WHERE media_sha256 IS NOT NULL AND media_object_ref IS NOT NULL\n"
    "    UNION ALL\n"
    "    SELECT sha256, last_seen_at\n"
    "      FROM fetch_versions WHERE object_ref IS NOT NULL AND bytes_pruned_at IS NULL\n)",
)

STILL_COLD_ELIGIBLE_WITH_FETCH_SQL = STILL_COLD_ELIGIBLE_SQL.replace(
    ") AND EXISTS (SELECT 1 FROM evidence_archive_cold WHERE sha256 = $1)",
    ") AND NOT EXISTS (\n"
    "    SELECT 1 FROM fetch_versions\n"
    "     WHERE sha256 = $1 AND bytes_pruned_at IS NULL\n"
    "       AND last_seen_at >= now() - make_interval(days => $2)\n"
    ") AND EXISTS (SELECT 1 FROM evidence_archive_cold WHERE sha256 = $1)",
)

INSERT_BUNDLE_SQL = """
INSERT INTO evidence_archive_bundles
    (bundle_key, backend, month, bundle_sha256, member_count, stored_bytes, original_bytes, verified_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, now())
ON CONFLICT (bundle_key) DO NOTHING
"""

INSERT_COLD_SQL = """
INSERT INTO evidence_archive_cold (sha256, bundle_key, member_offset, member_length, stored_codec)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (sha256) DO NOTHING
"""

MARK_HOT_REMOVED_SQL = "UPDATE evidence_archive_cold SET hot_removed_at = now() WHERE sha256 = $1"

COLD_LOCATION_SQL = """
SELECT bundle_key, member_offset, member_length FROM evidence_archive_cold WHERE sha256 = $1
"""


#: Recorded cold objects whose hot copy is still present (a pass that crashed
#: between record and removal, or a ``--keep-hot`` pass). $1 = limit.
PENDING_HOT_REMOVAL_SQL = """
SELECT sha256 FROM evidence_archive_cold WHERE hot_removed_at IS NULL ORDER BY tiered_at, sha256 LIMIT $1
"""

#: A restored (rehydrated) object has a hot copy again.
CLEAR_HOT_REMOVED_SQL = "UPDATE evidence_archive_cold SET hot_removed_at = NULL WHERE sha256 = $1"

#: Every recorded bundle with the number of cold rows that point into it.
RECORDED_BUNDLES_SQL = """
SELECT b.bundle_key, b.backend, b.month, b.bundle_sha256, b.member_count, b.stored_bytes,
       b.original_bytes, (SELECT count(*) FROM evidence_archive_cold c
                           WHERE c.bundle_key = b.bundle_key) AS cold_rows
  FROM evidence_archive_bundles b ORDER BY b.month, b.bundle_key
"""

BUNDLE_MEMBERS_SQL = """
SELECT sha256, member_offset, member_length, stored_codec, hot_removed_at
  FROM evidence_archive_cold WHERE bundle_key = $1
"""

ALL_COLD_LOCATIONS_SQL = """
SELECT sha256, bundle_key, member_offset, member_length FROM evidence_archive_cold ORDER BY sha256
"""


def index_from_rows(bundle: Any, members: Iterable[Any]) -> BundleIndex:
    """The :class:`BundleIndex` the database records for one bundle."""
    month = str(bundle["month"])
    idx = BundleIndex(month=month, bundle_sha256=str(bundle["bundle_sha256"]),
                      stored_bytes=int(bundle["stored_bytes"]),
                      original_bytes=int(bundle["original_bytes"]))
    for m in members:
        idx.members[str(m["sha256"])] = (int(m["member_offset"]), int(m["member_length"]),
                                          str(m["stored_codec"]))
    if idx.key != str(bundle["bundle_key"]):
        raise ValueError(f"bundle key {bundle['bundle_key']} does not match its month/hash")
    return idx


def verify_remote_bundle(backend: BlobBackend, index: BundleIndex, scratch: Path) -> list[str]:
    """Download a RECORDED bundle in full and re-verify it: the bundle hash,
    every recorded member against its address, and the index stored beside it
    against the recorded members. Returns the problems (empty = verified).
    Transport errors propagate (the caller reports the backend unreachable)."""
    problems: list[str] = []
    scratch.mkdir(parents=True, exist_ok=True)
    back = scratch / f"{index.bundle_sha256}.verify"
    try:
        from .archive_store import BlobNotFound  # noqa: PLC0415

        try:
            backend.get_to_file(index.key, back)
        except BlobNotFound:
            return [f"bundle {index.key} missing on {backend.name}"]
        problems.extend(verify_bundle_file(back, index))
    finally:
        back.unlink(missing_ok=True)
    try:
        side = json.loads(backend.get(index.key + ".index.json"))
        side_members = {k: tuple(v) for k, v in (side.get("members") or {}).items()}
        want = {k: tuple(v) for k, v in index.members.items()}
        if side_members != want:
            problems.append("stored index.json disagrees with the recorded members")
    except Exception as exc:  # noqa: BLE001 — reported, never fatal to the others
        problems.append(f"index.json unreadable: {type(exc).__name__}")
    return problems


def copy_bundle(src: BlobBackend, dst: BlobBackend, index: BundleIndex, scratch: Path) -> list[str]:
    """Copy one recorded bundle (+ its index) from ``src`` to ``dst`` and
    verify the copy ON ``dst``. Idempotent (``put`` never overwrites)."""
    scratch.mkdir(parents=True, exist_ok=True)
    local = scratch / f"{index.bundle_sha256}.copy"
    try:
        src.get_to_file(index.key, local)
        problems = verify_bundle_file(local, index)
        if problems:
            return ["source copy does not verify: " + "; ".join(problems[:5])]
        dst.put_file(index.key, local)
        dst.put(index.key + ".index.json", index.to_json())
    finally:
        local.unlink(missing_ok=True)
    return verify_remote_bundle(dst, index, scratch)


async def cold_location(conn: Any, sha256_hex: str) -> ColdLocation | None:
    """The recorded cold location of an object, or ``None`` (also when the
    tier tables are not migrated yet — nothing can be cold then)."""
    if not await conn.fetchval(TABLES_PRESENT_SQL):
        return None
    row = await conn.fetchrow(COLD_LOCATION_SQL, sha256_hex.lower())
    if row is None:
        return None
    return ColdLocation(row["bundle_key"], int(row["member_offset"]), int(row["member_length"]))


async def read_original(conn: Any, store: EvidenceStore, sha256_hex: str, *,
                        rehydrate: bool = False) -> bytes | None:
    """The ORIGINAL bytes for ``sha256_hex`` from the hot copy, else from its
    cold bundle (when ``store.cold`` is configured) — verified, or ``None``.
    The database's recorded location wins; without one the store's own
    locator (the local cold index) is asked. A recorded cold object that
    cannot be read RAISES (:class:`~legba.data.archive_store.ColdTierError`)."""
    digest = sha256_hex.lower()
    if store.cold is None:
        return store.get(digest)
    stored = store.read_stored(digest)
    if stored is not None:
        body = archive_codec.decode_verified(stored, digest)
        if body is not None:
            return body
    loc = await cold_location(conn, digest)
    if loc is None:
        return store.get(digest, rehydrate=rehydrate)  # the locator's answer, if any
    return store.get(digest, cold_location=loc, rehydrate=rehydrate)


__all__ = [
    "ALL_COLD_LOCATIONS_SQL",
    "BUNDLE_FORMAT",
    "BUNDLE_MEMBERS_SQL",
    "CLEAR_HOT_REMOVED_SQL",
    "PENDING_HOT_REMOVAL_SQL",
    "RECORDED_BUNDLES_SQL",
    "BundleIndex",
    "CANDIDATES_SQL",
    "COLD_LOCATION_SQL",
    "DEFAULT_HOT_DAYS",
    "DEFAULT_PACK_MB",
    "HOT_DAYS_ENV",
    "INDEX_MEMBER",
    "INSERT_BUNDLE_SQL",
    "INSERT_COLD_SQL",
    "MARK_HOT_REMOVED_SQL",
    "PACK_MB_ENV",
    "STILL_COLD_ELIGIBLE_SQL",
    "TABLES_PRESENT_SQL",
    "TierPolicy",
    "build_bundle",
    "cold_location",
    "cold_member_bytes",
    "copy_bundle",
    "index_from_rows",
    "member_name",
    "read_original",
    "upload_and_verify",
    "verify_bundle_file",
    "verify_remote_bundle",
]
