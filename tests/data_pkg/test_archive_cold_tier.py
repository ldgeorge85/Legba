# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cold tier read end to end, and its failure cases.

What these pin, over a local "cold" directory and an in-process S3:

* every READER returns the original bytes of an object that has gone cold:
  ``EvidenceStore.from_env(...).get`` (the DB-free locator: the local cold
  index), the instrument snapshot loader, the archiver's ingest-copy lookup,
  and a fetch-receipt read (``read_version_body``);
* an unreachable backend is a LOUD ``ColdTierUnavailable``, a missing bundle
  or a member that does not verify a LOUD ``ColdObjectInvalid`` — never a
  silent ``None`` for an object recorded as cold;
* the mover: an upload that raises leaves everything hot and nothing
  recorded; a crash between record and removal is resumed by the next pass;
* ``--verify`` catches a corrupted bundle at rest; ``--copy-from`` moves the
  tier to another backend and re-verifies; ``--restore`` brings objects back.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import random
import sys
from pathlib import Path
from typing import Any

import pytest

zstandard = pytest.importorskip("zstandard")

from legba.data import archive_tier as tier  # noqa: E402
from legba.data.archive_store import (  # noqa: E402
    COLD_INDEX_DIR,
    ColdLocation,
    ColdObjectInvalid,
    ColdTierError,
    ColdTierUnavailable,
    EvidenceStore,
    LocalColdIndex,
    LocalFsBackend,
)
from legba.data.sources.instrument_snapshot import load_object  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"_c1_{name}", REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[f"_c1_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


tier_mod = _load_script("archive_tier")

HTML = b"<html><body>" + b"<p>cold evidence paragraph</p>" * 300 + b"</body></html>"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _bodies(n: int, seed: int = 11) -> list[bytes]:
    rnd = random.Random(seed)
    return [HTML + bytes(rnd.getrandbits(8) for _ in range(48)) + str(i).encode() for i in range(n)]


class FakeConn:
    """Every statement the mover, its modes and the cold readers issue, over
    in-memory rows (the same idiom as test_archive_storage.TierConn)."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows                       # {"sha", "month", "recent"?}
        self.bundles: dict[str, dict] = {}
        self.cold: dict[str, dict] = {}
        self.versions: dict[int, dict] = {}
        self.ingest: dict[Any, dict] = {}

    async def fetchval(self, sql, *args):
        if sql is tier.TABLES_PRESENT_SQL:
            return True
        if sql is tier.FETCH_VERSIONS_PRESENT_SQL:
            return False
        if sql is tier.STILL_COLD_ELIGIBLE_SQL:
            sha = args[0]
            recent = any(r["sha"] == sha and r.get("recent") for r in self.rows)
            return (not recent) and sha in self.cold
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if sql is tier.CANDIDATES_SQL:
            return [r for r in self.rows if r["sha"] not in self.cold][: args[1]]
        if sql is tier.PENDING_HOT_REMOVAL_SQL:
            return [{"sha256": s} for s, c in self.cold.items() if c["hot_removed_at"] is None]
        if sql is tier.RECORDED_BUNDLES_SQL:
            return [{**b, "cold_rows": sum(1 for c in self.cold.values() if c["bundle_key"] == k)}
                    for k, b in sorted(self.bundles.items())]
        if sql is tier.BUNDLE_MEMBERS_SQL:
            return [{"sha256": s, **c} for s, c in self.cold.items() if c["bundle_key"] == args[0]]
        if sql is tier.ALL_COLD_LOCATIONS_SQL:
            return [{"sha256": s, **c} for s, c in sorted(self.cold.items())]
        if sql is tier_mod._RESTORE_SELECT_SQL:
            return [{"sha256": s} for s, c in sorted(self.cold.items())
                    if c["hot_removed_at"] is not None
                    and (args[0] is None or self.bundles[c["bundle_key"]]["month"] == args[0])]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if sql is tier.COLD_LOCATION_SQL:
            c = self.cold.get(args[0])
            return None if c is None else {k: c[k] for k in ("bundle_key", "member_offset", "member_length")}
        if "FROM fetch_versions WHERE id" in sql:
            return self.versions.get(args[0])
        if "fetch_version_records" in sql:
            return self.ingest.get(args[0])
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if sql is tier.INSERT_BUNDLE_SQL:
            self.bundles.setdefault(args[0], {
                "bundle_key": args[0], "backend": args[1], "month": args[2], "bundle_sha256": args[3],
                "member_count": args[4], "stored_bytes": args[5], "original_bytes": args[6]})
        elif sql is tier.INSERT_COLD_SQL:
            self.cold.setdefault(args[0], {"bundle_key": args[1], "member_offset": args[2],
                                           "member_length": args[3], "stored_codec": args[4],
                                           "hot_removed_at": None})
        elif sql is tier.MARK_HOT_REMOVED_SQL:
            self.cold[args[0]]["hot_removed_at"] = "now"
        elif sql is tier.CLEAR_HOT_REMOVED_SQL:
            self.cold[args[0]]["hot_removed_at"] = None
        else:
            raise AssertionError(sql)

    @contextlib.asynccontextmanager
    async def _tx(self):
        yield

    def transaction(self):
        return self._tx()


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    @contextlib.asynccontextmanager
    async def _acq(self):
        yield self.conn

    def acquire(self):
        return self._acq()


def _cold_env(monkeypatch, cold_dir: Path) -> None:
    monkeypatch.setenv("LEGBA_ARCHIVE_COLD_BACKEND", "local")
    monkeypatch.setenv("LEGBA_ARCHIVE_COLD_LOCAL_ROOT", str(cold_dir))


def _tiered(tmp_path: Path, n: int = 5) -> tuple[Path, LocalFsBackend, FakeConn, list[bytes], dict]:
    """``n`` objects put hot, tiered with the real mover, hot copies removed."""
    root = tmp_path / "hot"
    cold = LocalFsBackend(tmp_path / "cold", name="cold-local")
    store = EvidenceStore(root)
    bodies = _bodies(n)
    rows = [{"sha": store.put(b).sha256, "month": "2026-08"} for b in bodies]
    conn = FakeConn(rows)
    rep = asyncio.run(tier_mod.run_tier(conn, store, cold, tier.TierPolicy(), apply=True,
                                        limit=1000, spool=tmp_path / "spool"))
    assert rep["objects_tiered"] == n and rep["hot_removed"] == n, rep
    assert all(not store.path(_sha(b)).exists() for b in bodies)
    return root, cold, conn, bodies, rep


# ---------------------------------------------------------------------------
# reads from cold, through every reader
# ---------------------------------------------------------------------------


def test_from_env_store_reads_cold_through_the_local_index(tmp_path, monkeypatch):
    root, cold, conn, bodies, _ = _tiered(tmp_path)
    _cold_env(monkeypatch, tmp_path / "cold")
    assert (root / COLD_INDEX_DIR).is_dir()
    store = EvidenceStore.from_env(root)
    for b in bodies:
        assert store.get(_sha(b)) == b
    # Without a configured backend nothing can be cold: a plain miss.
    monkeypatch.delenv("LEGBA_ARCHIVE_COLD_BACKEND")
    assert EvidenceStore.from_env(root).get(_sha(bodies[0])) is None
    # An address never tiered is a plain miss, not an error.
    _cold_env(monkeypatch, tmp_path / "cold")
    assert EvidenceStore.from_env(root).get("ab" * 32) is None


def test_snapshot_loader_reads_a_cold_snapshot(tmp_path, monkeypatch):
    root, _, _, bodies, _ = _tiered(tmp_path)
    _cold_env(monkeypatch, tmp_path / "cold")
    assert load_object(root, _sha(bodies[2])) == bodies[2]


def test_read_original_prefers_the_recorded_location(tmp_path, monkeypatch):
    root, cold, conn, bodies, _ = _tiered(tmp_path)
    (root / COLD_INDEX_DIR).rename(tmp_path / "index-moved-away")   # no local index at all
    store = EvidenceStore(root, cold=cold)
    for b in bodies:
        assert asyncio.run(tier.read_original(conn, store, _sha(b))) == b
    s0 = _sha(bodies[0])
    assert asyncio.run(tier.read_original(conn, store, s0, rehydrate=True)) == bodies[0]
    assert store.path(s0).exists()


def test_archiver_ingest_copy_reads_a_cold_copy(tmp_path, monkeypatch):
    from legba.data.analysts.deterministic_handlers import _ingest_copy

    root, _, conn, bodies, _ = _tiered(tmp_path)
    _cold_env(monkeypatch, tmp_path / "cold")
    sid = "11111111-1111-1111-1111-111111111111"
    conn.ingest[sid] = {"sha256": _sha(bodies[1]), "content_type": "text/html; charset=utf-8",
                        "role": "page"}
    got = asyncio.run(_ingest_copy.load_ingest_copy(FakePool(conn), sid, "https://ex.test/a", root))
    assert got == (bodies[1], "text/html; charset=utf-8", "utf-8")


def test_archiver_ingest_copy_falls_back_loudly_when_cold_is_unreachable(tmp_path, monkeypatch, caplog):
    from legba.data.analysts.deterministic_handlers import _ingest_copy

    root, _, conn, bodies, _ = _tiered(tmp_path)
    monkeypatch.setenv("LEGBA_ARCHIVE_COLD_BACKEND", "s3")
    monkeypatch.setenv("LEGBA_ARCHIVE_S3_BUCKET", "gone")
    monkeypatch.setenv("LEGBA_ARCHIVE_S3_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("LEGBA_ARCHIVE_S3_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("LEGBA_ARCHIVE_S3_SECRET_ACCESS_KEY", "y")
    pytest.importorskip("botocore")
    sid = "22222222-2222-2222-2222-222222222222"
    conn.ingest[sid] = {"sha256": _sha(bodies[0]), "content_type": None, "role": "page"}
    with caplog.at_level("WARNING"):
        got = asyncio.run(_ingest_copy.load_ingest_copy(FakePool(conn), sid, "https://ex.test/a", root))
    assert got is None                          # the archiver re-fetches ...
    assert "cold_unreadable" in caplog.text     # ... and says why


def test_fetch_receipt_read_returns_cold_bytes(tmp_path, monkeypatch):
    from legba.runtime.fetch_receipts import read_version_body

    root, _, conn, bodies, _ = _tiered(tmp_path)
    _cold_env(monkeypatch, tmp_path / "cold")
    s = _sha(bodies[3])
    conn.versions[7] = {"sha256": s, "object_ref": f"cas:sha256/{s}", "bytes_pruned_at": None}
    conn.versions[8] = {"sha256": s, "object_ref": f"cas:sha256/{s}", "bytes_pruned_at": "2026-11-01"}
    conn.versions[9] = {"sha256": s, "object_ref": None, "bytes_pruned_at": None}
    assert asyncio.run(read_version_body(conn, 7, root=root)) == bodies[3]
    assert asyncio.run(read_version_body(conn, 8, root=root)) is None   # pruned: the row says so
    assert asyncio.run(read_version_body(conn, 9, root=root)) is None   # kept no bytes
    assert asyncio.run(read_version_body(conn, 10, root=root)) is None


# ---------------------------------------------------------------------------
# failure cases
# ---------------------------------------------------------------------------


class _DownBackend(LocalFsBackend):
    def get(self, key, *, start=None, length=None):
        raise ConnectionError("endpoint refused")

    def get_to_file(self, key, path):
        raise ConnectionError("endpoint refused")


def test_unreachable_backend_is_loud_and_marks_nothing(tmp_path):
    root, cold, conn, bodies, _ = _tiered(tmp_path)
    down = _DownBackend(tmp_path / "cold", name="cold-down")
    store = EvidenceStore(root, cold=down, locator=LocalColdIndex(root).lookup)
    before = {k: dict(v) for k, v in conn.cold.items()}
    with pytest.raises(ColdTierUnavailable, match="cold-down unreachable"):
        store.get(_sha(bodies[0]))
    with pytest.raises(ColdTierUnavailable):
        asyncio.run(tier.read_original(conn, EvidenceStore(root, cold=down), _sha(bodies[0])))
    assert conn.cold == before                 # nothing marked lost


def test_missing_bundle_and_corrupt_member_are_loud(tmp_path):
    root, cold, conn, bodies, _ = _tiered(tmp_path)
    s = _sha(bodies[0])
    loc = LocalColdIndex(root).lookup(s)
    store = EvidenceStore(root, cold=cold)
    with pytest.raises(ColdObjectInvalid, match="missing"):
        store.get(s, cold_location=ColdLocation("bundles/2026-08/" + "0" * 64 + ".tar", 0, 10))
    bundle = tmp_path / "cold" / loc.bundle_key
    raw = bytearray(bundle.read_bytes())
    raw[loc.offset + loc.length // 2] ^= 0xFF
    bundle.chmod(0o644)
    bundle.write_bytes(bytes(raw))
    with pytest.raises(ColdObjectInvalid, match="does not verify"):
        store.get(s, cold_location=loc)
    assert issubclass(ColdObjectInvalid, ColdTierError)


def test_upload_that_raises_keeps_everything_hot_and_records_nothing(tmp_path):
    class _Flaky(LocalFsBackend):
        def put_file(self, key, path):
            raise ConnectionError("connection reset mid-upload")

    root = tmp_path / "hot"
    store = EvidenceStore(root)
    bodies = _bodies(4)
    conn = FakeConn([{"sha": store.put(b).sha256, "month": "2026-08"} for b in bodies])
    rep = asyncio.run(tier_mod.run_tier(conn, store, _Flaky(tmp_path / "cold"), tier.TierPolicy(),
                                        apply=True, limit=100, spool=tmp_path / "spool"))
    assert len(rep["bundles_failed"]) == 1 and "connection reset" in rep["bundles_failed"][0]["problems"][0]
    assert conn.cold == {} and conn.bundles == {}
    assert all(store.get(_sha(b)) == b for b in bodies)
    assert not (root / COLD_INDEX_DIR).exists()
    assert not list((tmp_path / "spool").iterdir())
    # Re-running against a healthy backend completes; the same tar is the same key.
    good = LocalFsBackend(tmp_path / "cold")
    rep2 = asyncio.run(tier_mod.run_tier(conn, store, good, tier.TierPolicy(), apply=True,
                                         limit=100, spool=tmp_path / "spool"))
    assert rep2["objects_tiered"] == 4 and rep2["hot_removed"] == 4


def test_rerun_is_idempotent_and_an_unrecorded_bundle_is_never_trusted(tmp_path):
    """An upload that landed but was never recorded (killed before the DB
    write) is invisible to readers; the re-run re-uploads (put never
    overwrites; the same content is the same key) and records once."""
    root = tmp_path / "hot"
    store = EvidenceStore(root)
    bodies = _bodies(3)
    rows = [{"sha": store.put(b).sha256, "month": "2026-08"} for b in bodies]
    cold = LocalFsBackend(tmp_path / "cold")
    members = [(s["sha"], *tier.cold_member_bytes(store.read_stored(s["sha"]), s["sha"])) for s in rows]
    idx = tier.build_bundle("2026-08", members, tmp_path / "pre.tar")
    cold.put_file(idx.key, tmp_path / "pre.tar")            # uploaded, never recorded
    conn = FakeConn(rows)
    assert asyncio.run(tier.read_original(conn, EvidenceStore(root, cold=cold), rows[0]["sha"])) == bodies[0]
    rep = asyncio.run(tier_mod.run_tier(conn, store, cold, tier.TierPolicy(), apply=True,
                                        limit=100, spool=tmp_path / "spool"))
    assert rep["bundles_uploaded"] == 1 and list(conn.bundles) == [idx.key]
    rep2 = asyncio.run(tier_mod.run_tier(conn, store, cold, tier.TierPolicy(), apply=True,
                                         limit=100, spool=tmp_path / "spool"))
    assert rep2["candidates"] == 0 and rep2["bundles_uploaded"] == 0


def test_a_killed_pass_leaves_a_spool_tar_the_next_pass_clears(tmp_path):
    root = tmp_path / "hot"
    store = EvidenceStore(root)
    rows = [{"sha": store.put(b).sha256, "month": "2026-08"} for b in _bodies(2)]
    (tmp_path / "spool").mkdir()
    (tmp_path / "spool" / "2026-08.1.tar").write_bytes(b"half a bundle")
    (tmp_path / "spool" / "keep.me").write_bytes(b"not ours")
    rep = asyncio.run(tier_mod.run_tier(FakeConn(rows), store, LocalFsBackend(tmp_path / "cold"),
                                        tier.TierPolicy(), apply=True, limit=100, spool=tmp_path / "spool"))
    assert rep["stale_spool_removed"] == 1 and rep["objects_tiered"] == 2
    assert sorted(p.name for p in (tmp_path / "spool").iterdir()) == ["keep.me"]


def test_a_crash_between_record_and_removal_is_resumed(tmp_path):
    root = tmp_path / "hot"
    store = EvidenceStore(root)
    bodies = _bodies(4)
    rows = [{"sha": store.put(b).sha256, "month": "2026-08"} for b in bodies]
    rows[3]["recent"] = True
    conn = FakeConn(rows)
    cold = LocalFsBackend(tmp_path / "cold")
    keep = asyncio.run(tier_mod.run_tier(conn, store, cold, tier.TierPolicy(), apply=True, limit=100,
                                         spool=tmp_path / "spool", keep_hot=True))
    assert keep["objects_tiered"] == 4 and keep["hot_removed"] == 0
    rep = asyncio.run(tier_mod.run_tier(conn, store, cold, tier.TierPolicy(), apply=True, limit=100,
                                        spool=tmp_path / "spool"))
    assert rep["candidates"] == 0 and rep["resumed_checked"] == 4
    assert rep["hot_removed"] == 3 and rep["hot_kept_recent"] == 1
    assert store.path(rows[3]["sha"]).exists()


def test_verify_catches_a_corrupted_bundle_at_rest(tmp_path):
    root, cold, conn, bodies, _ = _tiered(tmp_path)
    ok = asyncio.run(tier_mod.verify_all(conn, cold, tmp_path / "spool"))
    assert ok["bundles"] == ok["verified"] == 1 and ok["members"] == 5 and ok["failed"] == []
    key = next(iter(conn.bundles))
    raw = bytearray((tmp_path / "cold" / key).read_bytes())
    raw[700] ^= 0xFF
    (tmp_path / "cold" / key).chmod(0o644)
    (tmp_path / "cold" / key).write_bytes(bytes(raw))
    bad = asyncio.run(tier_mod.verify_all(conn, cold, tmp_path / "spool"))
    assert bad["verified"] == 0 and "bundle sha256 mismatch" in bad["failed"][0]["problems"]
    down = asyncio.run(tier_mod.verify_all(conn, _DownBackend(tmp_path / "cold"), tmp_path / "spool"))
    assert down["unreachable"] and down["verified"] == 0          # reported, nothing marked


def test_copy_to_a_new_backend_then_read_only_from_it(tmp_path, monkeypatch):
    root, cold, conn, bodies, _ = _tiered(tmp_path)
    new = LocalFsBackend(tmp_path / "cold2", name="cold-new")
    rep = asyncio.run(tier_mod.copy_from(conn, cold, new, tmp_path / "spool"))
    assert rep["bundles"] == rep["copied_verified"] == 1 and rep["failed"] == []
    again = asyncio.run(tier_mod.copy_from(conn, cold, new, tmp_path / "spool"))
    assert again["copied_verified"] == 1                  # idempotent
    import shutil
    shutil.rmtree(tmp_path / "cold", ignore_errors=True)  # the old endpoint is gone
    _cold_env(monkeypatch, tmp_path / "cold2")
    store = EvidenceStore.from_env(root)
    assert all(store.get(_sha(b)) == b for b in bodies)
    assert asyncio.run(tier_mod.verify_all(conn, new, tmp_path / "spool"))["verified"] == 1


def test_backend_from_prefix_renames_the_env():
    b = tier_mod.backend_from_prefix("LEGBA_ARCHIVE_SRC_", {
        "LEGBA_ARCHIVE_SRC_COLD_BACKEND": "local", "LEGBA_ARCHIVE_SRC_COLD_LOCAL_ROOT": "/x",
        "LEGBA_ARCHIVE_COLD_BACKEND": "s3"})
    assert isinstance(b, LocalFsBackend) and str(b.root) == "/x"


def test_restore_month_rehydrates_and_clears_the_stamp(tmp_path):
    root, cold, conn, bodies, _ = _tiered(tmp_path)
    store = EvidenceStore(root, cold=cold)
    rep = asyncio.run(tier_mod.restore(conn, store, month="2026-08"))
    assert rep["selected"] == rep["restored"] == 5 and rep["failed"] == []
    assert all(EvidenceStore(root).get(_sha(b)) == b for b in bodies)   # hot again
    assert all(c["hot_removed_at"] is None for c in conn.cold.values())
    # A later pass retires them again from the recorded copies (no new upload).
    rep2 = asyncio.run(tier_mod.run_tier(conn, EvidenceStore(root), cold, tier.TierPolicy(),
                                         apply=True, limit=100, spool=tmp_path / "spool"))
    assert rep2["bundles_uploaded"] == 0 and rep2["hot_removed"] == 5


def test_local_index_last_line_wins_and_rebuilds_from_the_database(tmp_path):
    idx = LocalColdIndex(tmp_path)
    s = "cd" * 32
    idx.record([(s, ColdLocation("bundles/2026-08/a.tar", 1, 2))])
    idx.record([(s, ColdLocation("bundles/2026-08/b.tar", 3, 4))])
    assert idx.lookup(s) == ColdLocation("bundles/2026-08/b.tar", 3, 4)
    (idx.shard(s)).write_text(idx.shard(s).read_text() + "garbage line\n" + s + "\tk\tNaN\t1\n")
    assert idx.lookup(s) == ColdLocation("bundles/2026-08/b.tar", 3, 4)
    root, cold, conn, bodies, _ = _tiered(tmp_path / "t")
    import shutil
    shutil.rmtree(root / COLD_INDEX_DIR)
    rep = asyncio.run(tier_mod.rebuild_index(conn, root))
    assert rep["entries"] == 5
    store = EvidenceStore(root, cold=cold, locator=LocalColdIndex(root).lookup)
    assert all(store.get(_sha(b)) == b for b in bodies)


class _MiniS3:
    """head/put/get(+Range) over a dict — enough for S3Backend."""

    class _Err(Exception):
        def __init__(self):
            super().__init__("404")
            self.response = {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}

    class _Body:
        def __init__(self, data):
            self._d, self._p = data, 0

        def read(self, n=-1):
            n = len(self._d) - self._p if n is None or n < 0 else n
            out = self._d[self._p:self._p + n]
            self._p += len(out)
            return out

    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self._Err()
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, **_kw):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.read()

    def get_object(self, Bucket, Key, Range=None):
        if (Bucket, Key) not in self.objects:
            raise self._Err()
        data = self.objects[(Bucket, Key)]
        if Range:
            lo, _, hi = Range[len("bytes="):].partition("-")
            data = data[int(lo): (int(hi) + 1 if hi else None)]
        return {"Body": self._Body(data)}


@pytest.mark.parametrize("kind", ["local", "s3"])
def test_a_damaged_object_squatting_the_bundle_key_is_repaired_not_trusted(tmp_path, kind):
    """An earlier upload corrupted in flight left damaged bytes at the
    content-addressed key, unrecorded. put() never overwrites, so without the
    repair every later pass would fail forever; with it the key is rewritten
    with the bytes that hash to it, read back, verified, and only then recorded."""
    from legba.data.archive_store import S3Backend  # noqa: PLC0415

    root = tmp_path / "hot"
    store = EvidenceStore(root)
    bodies = _bodies(3)
    rows = [{"sha": store.put(b).sha256, "month": "2026-08"} for b in bodies]
    cold = (LocalFsBackend(tmp_path / "cold") if kind == "local"
            else S3Backend(bucket="b", client=_MiniS3(), name="cold-s3"))
    members = [(r["sha"], *tier.cold_member_bytes(store.read_stored(r["sha"]), r["sha"])) for r in rows]
    idx = tier.build_bundle("2026-08", members, tmp_path / "pre.tar")
    raw = bytearray((tmp_path / "pre.tar").read_bytes())
    raw[600] ^= 0xFF
    (tmp_path / "bad.tar").write_bytes(bytes(raw))
    cold.put_file(idx.key, tmp_path / "bad.tar")             # the squatter
    # Never replaced WITH bytes that do not hash to the key.
    assert cold.replace_damaged(idx.key, tmp_path / "bad.tar", idx.bundle_sha256) is False
    conn = FakeConn(rows)
    rep = asyncio.run(tier_mod.run_tier(conn, store, cold, tier.TierPolicy(), apply=True,
                                        limit=100, spool=tmp_path / "spool"))
    assert rep["bundles_failed"] == [] and rep["objects_tiered"] == 3 and rep["hot_removed"] == 3
    assert hashlib.sha256(cold.get(idx.key)).hexdigest() == idx.bundle_sha256
    # A verified object is never replaced.
    assert cold.replace_damaged(idx.key, tmp_path / "pre.tar", idx.bundle_sha256) is False
