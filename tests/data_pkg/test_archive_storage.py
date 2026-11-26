# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-K — the evidence archive's codecs, storage interface, in-place
compaction and cold tiering.

The invariant every test leans on: whatever the bytes rest as (identity,
zstd, a member of a cold bundle), a read by content address returns the
ORIGINAL bytes that hash to the address, or nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import os
import random
import sys
from pathlib import Path

import pytest

zstandard = pytest.importorskip("zstandard")

from legba.data import archive_codec  # noqa: E402
from legba.data import archive_tier as tier  # noqa: E402
from legba.data.archive import cas_path  # noqa: E402
from legba.data.archive_store import (  # noqa: E402
    BlobNotFound,
    ColdBackendNotConfigured,
    ColdLocation,
    EvidenceStore,
    LocalFsBackend,
    S3Backend,
    cold_backend_from_env,
)
from legba.data.sources.instrument_snapshot import load_object, store_object  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"_nk_{name}",REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[f"_nk_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


compact_mod = _load_script("archive_compact")
tier_mod = _load_script("archive_tier")
backup_lib = _load_script("backup_lib")

HTML = b"<html><head><title>x</title></head><body>" + b"<p>evidence paragraph</p>" * 400 + b"</body></html>"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _legacy(root: Path, body: bytes) -> Path:
    """An object as the pre-N-K writer left it: the raw original bytes."""
    p = cas_path(root, _sha(body))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


# ---------------------------------------------------------------------------
# codec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [b"", b"a", HTML, os.urandom(3 << 20) + HTML * 200])
def test_codec_round_trip_empty_small_large(body):
    enc = archive_codec.encode(body, level=3)
    assert enc.codec == "zstd" and archive_codec.is_zstd(enc.data)
    assert archive_codec.decode(enc.data) == body
    assert archive_codec.decode_verified(enc.data, _sha(body)) == body
    assert archive_codec.stored_codec(enc.data, _sha(body)) == "zstd"


def test_codec_original_that_starts_with_the_magic_is_wrapped():
    inner = archive_codec.encode(HTML).data          # an archived .zst file
    outer = archive_codec.encode(inner).data
    assert archive_codec.decode_verified(outer, _sha(inner)) == inner


def test_codec_legacy_object_starting_with_magic_is_read_raw():
    legacy = archive_codec.ZSTD_MAGIC + b"not a real frame, just bytes"
    assert archive_codec.decode_verified(legacy, _sha(legacy)) == legacy
    assert archive_codec.stored_codec(legacy, _sha(legacy)) == "identity"
    # …and a real frame stored RAW (legacy) is returned raw, not decoded.
    frame = archive_codec.encode(HTML).data
    assert archive_codec.decode_verified(frame, _sha(frame)) == frame


def test_codec_never_returns_bytes_that_do_not_match():
    enc = archive_codec.encode(HTML).data
    assert archive_codec.decode_verified(enc, "0" * 64) is None
    assert archive_codec.decode_verified(b"plain", "0" * 64) is None
    assert archive_codec.stored_codec(enc[:-3], _sha(HTML)) is None    # truncated frame


def test_codec_without_binding_writes_identity_and_reads_loud(monkeypatch):
    frame = archive_codec.encode(HTML).data
    monkeypatch.setattr(archive_codec, "_zstd", lambda: None)
    enc = archive_codec.encode(HTML)
    assert enc.codec == "identity" and enc.data == HTML
    with pytest.raises(archive_codec.ArchiveCodecUnavailable):
        archive_codec.decode_verified(frame, _sha(HTML))


def test_codec_knobs(monkeypatch):
    monkeypatch.setenv(archive_codec.CODEC_ENV, "identity")
    assert archive_codec.encode(HTML).codec == "identity"
    monkeypatch.setenv(archive_codec.CODEC_ENV, "zstd")
    monkeypatch.setenv(archive_codec.ZSTD_LEVEL_ENV, "99")
    assert archive_codec.configured_level() == 19
    monkeypatch.setenv(archive_codec.ZSTD_LEVEL_ENV, "junk")
    assert archive_codec.configured_level() == 3


# ---------------------------------------------------------------------------
# store: mixed hot reads, dedup, the instrument reader
# ---------------------------------------------------------------------------


def test_store_put_get_and_dedup(tmp_path):
    store = EvidenceStore(tmp_path)
    res = store.put(HTML)
    assert not res.existed and res.codec == "zstd" and res.stored_size < len(HTML)
    assert archive_codec.is_zstd(store.path(res.sha256).read_bytes())
    assert store.get(res.sha256) == HTML
    again = store.put(HTML)
    assert again.existed and again.sha256 == res.sha256
    assert not [p for p in store.path(res.sha256).parent.iterdir() if p.name.startswith(".tmp.")]


def test_store_mixed_legacy_and_compressed(tmp_path):
    store = EvidenceStore(tmp_path)
    old = b"<html>legacy uncompressed object</html>" * 50
    _legacy(tmp_path, old)
    new = store.put(HTML)
    assert store.get(_sha(old)) == old
    assert store.get(new.sha256) == HTML
    # A legacy object is never rewritten by a later put of the same bytes.
    assert store.put(old).existed
    assert cas_path(tmp_path, _sha(old)).read_bytes() == old


def test_store_corrupt_object_reads_none(tmp_path):
    store = EvidenceStore(tmp_path)
    res = store.put(HTML)
    store.path(res.sha256).write_bytes(b"tampered")
    assert store.get(res.sha256) is None


def test_instrument_snapshot_reader_is_transparent(tmp_path):
    old = b"<xml>legacy</xml>"
    _legacy(tmp_path, old)
    sha, ref, existed = store_object(tmp_path, HTML)
    assert not existed and ref == f"cas:sha256/{sha}"
    assert load_object(tmp_path, sha) == HTML
    assert load_object(tmp_path, _sha(old)) == old


def test_backup_restore_drill_rehash_reads_originals(tmp_path):
    """The receipts re-hash the restore drill runs decodes compressed objects."""
    store = EvidenceStore(tmp_path)
    a = store.put(HTML).sha256
    legacy = b"legacy receipt bytes"
    _legacy(tmp_path, legacy)
    rows = [(f"{a[:2]}/{a}", a), (f"{_sha(legacy)[:2]}/{_sha(legacy)}", _sha(legacy))]
    res = backup_lib.verify_hash_sample(rows, tmp_path)
    assert res == {"checked": 2, "ok": 2, "problems": []}


def test_backup_rehash_cli_fallback(tmp_path, monkeypatch):
    import builtins
    if not __import__("shutil").which("zstd"):
        pytest.skip("zstd CLI not present")
    real_import = builtins.__import__

    def no_zstandard(name, *a, **k):
        if name == "zstandard":
            raise ImportError(name)
        return real_import(name, *a, **k)

    a = EvidenceStore(tmp_path).put(HTML).sha256
    monkeypatch.setattr(builtins, "__import__", no_zstandard)
    assert backup_lib.archive_original_sha256(cas_path(tmp_path, a), a) == a


# ---------------------------------------------------------------------------
# compaction
# ---------------------------------------------------------------------------


def _tree(root: Path) -> dict[str, bytes]:
    bodies = {f"page{i}": (f"<html>{i}</html>".encode() + HTML * (i + 1)) for i in range(12)}
    for b in bodies.values():
        _legacy(root, b)
    return {_sha(b): b for b in bodies.values()}


def _all_verify(root: Path, originals: dict[str, bytes]) -> None:
    store = EvidenceStore(root)
    for sha, body in originals.items():
        assert store.get(sha) == body, sha


def test_compact_dry_run_measures_and_writes_nothing(tmp_path):
    originals = _tree(tmp_path)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    rep = compact_mod.compact(tmp_path, prefixes=compact_mod.parse_prefixes(None), apply=False)
    assert rep.files_seen == len(originals) and rep.compressed == len(originals)
    assert rep.bytes_after < rep.bytes_before
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_compact_apply_preserves_meta_and_is_idempotent(tmp_path):
    originals = _tree(tmp_path)
    some = cas_path(tmp_path, next(iter(originals)))
    os.chmod(some, 0o640)
    os.utime(some, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    rep = compact_mod.compact(tmp_path, prefixes=compact_mod.parse_prefixes(None), apply=True)
    assert rep.compressed == len(originals) and rep.errors == 0
    st = some.stat()
    assert st.st_mtime_ns == 1_700_000_000_000_000_000 and (st.st_mode & 0o777) == 0o640
    assert all(archive_codec.is_zstd(cas_path(tmp_path, s).read_bytes()) for s in originals)
    _all_verify(tmp_path, originals)
    again = compact_mod.compact(tmp_path, prefixes=compact_mod.parse_prefixes(None), apply=True)
    assert again.compressed == 0 and again.already_compressed == len(originals)
    assert again.bytes_before == rep.bytes_after


def test_compact_mismatch_orphan_rows_without_file_and_foreign(tmp_path):
    originals = _tree(tmp_path)
    shas = list(originals)
    bad = cas_path(tmp_path, shas[0])
    bad.write_bytes(b"bit rot")                       # address no longer matches
    orphan_sha = shas[1]
    known = set(shas) - {orphan_sha} | {"ab" + "c" * 62}   # a row whose file is gone
    (tmp_path / "grading" / "aa").mkdir(parents=True)
    (tmp_path / "grading" / "aa" / ("a" * 64)).write_bytes(HTML)
    tmp_litter = cas_path(tmp_path, shas[2]).parent / ".tmp.123.deadbeef.abc"
    tmp_litter.write_bytes(b"in-flight writer temp")
    rep = compact_mod.compact(tmp_path, prefixes=compact_mod.parse_prefixes(None),
                              apply=True, shas=known)
    assert rep.mismatches == 1 and bad.read_bytes() == b"bit rot"
    assert rep.orphans == 1 and not archive_codec.is_zstd(cas_path(tmp_path, orphan_sha).read_bytes())
    assert rep.rows_without_file == 1
    assert rep.skipped_tmp == 1 and tmp_litter.exists()
    assert (tmp_path / "grading" / "aa" / ("a" * 64)).read_bytes() == HTML   # other layouts untouched
    rep2 = compact_mod.compact(tmp_path, prefixes=compact_mod.parse_prefixes(None),
                               apply=True, shas=known, include_orphans=True)
    assert rep2.orphans_compressed == 1


def test_compact_kill_and_resume(tmp_path, monkeypatch):
    originals = _tree(tmp_path)
    real_replace = os.replace
    calls = {"n": 0}

    def killed_after_three(src, dst):
        calls["n"] += 1
        if calls["n"] == 4:
            raise KeyboardInterrupt("simulated kill")
        return real_replace(src, dst)

    monkeypatch.setattr(compact_mod.os, "replace", killed_after_three)
    with pytest.raises(KeyboardInterrupt):
        compact_mod.compact(tmp_path, prefixes=compact_mod.parse_prefixes(None), apply=True)
    monkeypatch.setattr(compact_mod.os, "replace", real_replace)
    _all_verify(tmp_path, originals)                  # every object still readable
    # A kill -9 leaves a temp file; the next run sweeps a stale one.
    d = next(p for p in tmp_path.iterdir() if p.is_dir())
    stale = d / f"{compact_mod.TMP_PREFIX}999.0123456789abcdef"
    stale.write_bytes(b"partial")
    os.utime(stale, (0, 0))
    rep = compact_mod.compact(tmp_path, prefixes=compact_mod.parse_prefixes(None), apply=True)
    assert rep.already_compressed == 3 and rep.compressed == len(originals) - 3
    assert rep.stale_tmp_removed == 1 and not stale.exists()
    _all_verify(tmp_path, originals)


def test_compact_concurrent_writer_replacing_the_file_is_left_alone(tmp_path):
    originals = _tree(tmp_path)
    victim = sorted(originals)[0]
    path = cas_path(tmp_path, victim)

    class RacingPacer(compact_mod.Pacer):
        def account(self, n):
            if path.exists() and not archive_codec.is_zstd(path.read_bytes()):
                tmp = path.parent / ".tmp.writer"
                tmp.write_bytes(originals[victim])
                os.replace(tmp, path)                # a writer's temp+rename: new inode

    rep = compact_mod.compact(tmp_path, prefixes=compact_mod.parse_prefixes(None),
                              apply=True, pacer=RacingPacer(0))
    assert rep.skipped_changed >= 1
    _all_verify(tmp_path, originals)
    # The next pass picks it up.
    rep2 = compact_mod.compact(tmp_path, prefixes=compact_mod.parse_prefixes(None), apply=True)
    assert rep2.compressed >= 1 and rep2.mismatches == 0


def test_compact_prefix_and_limit():
    assert compact_mod.parse_prefixes(["00"]) == ["00"]
    assert compact_mod.parse_prefixes(["0e-11"]) == ["0e", "0f", "10", "11"]
    with pytest.raises(ValueError):
        compact_mod.parse_prefixes(["zz"])


def test_compact_limit_stops_early(tmp_path):
    originals = _tree(tmp_path)
    rep = compact_mod.compact(tmp_path, prefixes=compact_mod.parse_prefixes(None), apply=True, limit=5)
    assert rep.compressed == 5
    _all_verify(tmp_path, originals)


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------


class _S3Error(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data, self._pos = data, 0

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._data) - self._pos
        out = self._data[self._pos:self._pos + n]
        self._pos += len(out)
        return out


class InProcessS3:
    """The four S3 calls the backend makes, over a dict (an in-process S3)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[str] = []

    def head_object(self, Bucket, Key):
        self.calls.append("head")
        if (Bucket, Key) not in self.objects:
            raise _S3Error("404", 404)
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, **_kw):
        self.calls.append("put")
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.read()

    def get_object(self, Bucket, Key, Range=None):
        self.calls.append("get" if Range is None else "get-range")
        if (Bucket, Key) not in self.objects:
            raise _S3Error("NoSuchKey", 404)
        data = self.objects[(Bucket, Key)]
        if Range:
            lo, _, hi = Range[len("bytes="):].partition("-")
            data = data[int(lo): (int(hi) + 1 if hi else None)]
        return {"Body": _Body(data)}


@pytest.fixture(params=["local", "s3"])
def cold(request, tmp_path):
    if request.param == "local":
        return LocalFsBackend(tmp_path / "cold", name="cold-local")
    return S3Backend(bucket="evidence", prefix="legba", client=InProcessS3(), name="cold-s3")


def test_backend_never_overwrites_and_ranges(cold, tmp_path):
    assert cold.put("k/one", b"0123456789") is True
    assert cold.put("k/one", b"XXXXXXXXXX") is False
    assert cold.get("k/one") == b"0123456789"
    assert cold.get("k/one", start=3, length=4) == b"3456"
    assert cold.stat("k/one").size == 10 and cold.exists("k/one")
    assert cold.stat("k/none") is None
    with pytest.raises(BlobNotFound):
        cold.get("k/none")
    assert not hasattr(cold, "delete")
    with pytest.raises(ValueError):
        cold.put("../escape", b"x")


def test_cold_backend_from_env_refuses_when_unset_or_incomplete():
    with pytest.raises(ColdBackendNotConfigured):
        cold_backend_from_env({})
    with pytest.raises(ColdBackendNotConfigured):
        cold_backend_from_env({"LEGBA_ARCHIVE_COLD_BACKEND": "s3"})
    with pytest.raises(ColdBackendNotConfigured):
        cold_backend_from_env({"LEGBA_ARCHIVE_COLD_BACKEND": "local"})
    with pytest.raises(ColdBackendNotConfigured):
        cold_backend_from_env({"LEGBA_ARCHIVE_COLD_BACKEND": "ftp"})
    s3 = cold_backend_from_env({"LEGBA_ARCHIVE_COLD_BACKEND": "s3", "LEGBA_ARCHIVE_S3_BUCKET": "b",
                                "LEGBA_ARCHIVE_S3_PATH_STYLE": "0"})
    assert isinstance(s3, S3Backend) and s3.path_style is False and s3.prefix == "legba-archive"


def test_s3_backend_missing_credentials_is_loud(monkeypatch):
    pytest.importorskip("botocore")
    monkeypatch.delenv("LEGBA_ARCHIVE_S3_ACCESS_KEY_ID", raising=False)
    b = S3Backend(bucket="b", endpoint="http://127.0.0.1:9")
    with pytest.raises(ColdBackendNotConfigured, match="LEGBA_ARCHIVE_S3_ACCESS_KEY_ID"):
        b.exists("x")


# ---------------------------------------------------------------------------
# tiering
# ---------------------------------------------------------------------------


def _bodies(n: int) -> list[bytes]:
    rnd = random.Random(7)
    return [HTML + bytes(rnd.getrandbits(8) for _ in range(64)) + str(i).encode() for i in range(n)]


def test_bundle_pack_verify_and_ranged_fetch(tmp_path, cold):
    hot = EvidenceStore(tmp_path / "hot", cold=cold)
    bodies = _bodies(5) + [b""]
    legacy = b"legacy raw object" * 10
    _legacy(tmp_path / "hot", legacy)
    shas = [hot.put(b).sha256 for b in bodies] + [_sha(legacy)]
    members = [(s, *tier.cold_member_bytes(hot.read_stored(s), s)) for s in shas]
    idx = tier.build_bundle("2026-08", members, tmp_path / "b.tar")
    assert tier.verify_bundle_file(tmp_path / "b.tar", idx) == []
    assert all(c == "zstd" for _, _, c in idx.members.values())    # identity re-encoded
    assert tier.upload_and_verify(cold, tmp_path / "b.tar", idx, tmp_path) == []
    # The bundle is a plain tar an operator can read with no Legba code.
    import tarfile
    with tarfile.open(tmp_path / "b.tar") as tf:
        assert tier.INDEX_MEMBER in tf.getnames()
    for s in shas:
        hot.path(s).unlink()
    for body in bodies + [legacy]:
        s = _sha(body)
        assert hot.get(s) is None
        assert hot.get(s, cold_location=idx.location(s)) == body
    s0 = _sha(bodies[0])
    assert hot.get(s0, cold_location=idx.location(s0), rehydrate=True) == bodies[0]
    assert hot.get(s0) == bodies[0]                                 # back on the hot tier


def test_bundle_with_a_corrupt_member_fails_verification(tmp_path):
    hot = EvidenceStore(tmp_path / "hot")
    shas = [hot.put(b).sha256 for b in _bodies(3)]
    members = [(s, *tier.cold_member_bytes(hot.read_stored(s), s)) for s in shas]
    idx = tier.build_bundle("2026-08", members, tmp_path / "b.tar")
    off, length, _ = idx.members[shas[1]]
    raw = bytearray((tmp_path / "b.tar").read_bytes())
    raw[off + length - 2] ^= 0xFF
    (tmp_path / "b.tar").write_bytes(bytes(raw))
    problems = tier.verify_bundle_file(tmp_path / "b.tar", idx)
    assert "bundle sha256 mismatch" in problems and any(shas[1] in p for p in problems)


def test_cold_member_refuses_a_mismatching_object():
    with pytest.raises(ValueError):
        tier.cold_member_bytes(b"rot", "0" * 64)


class TierConn:
    """The handful of statements the mover issues, over in-memory rows."""

    def __init__(self, rows: list[dict], *, migrated: bool = True) -> None:
        self.rows = rows                         # {"sha", "month", "recent": bool}
        self.migrated = migrated
        # N-L migration 0245 absent: the mover takes the pre-receipt statements.
        self.fetch_versions = False
        self.bundles: dict[str, tuple] = {}
        self.cold: dict[str, tuple] = {}
        self.hot_removed: set[str] = set()

    async def fetchval(self, sql, *args):
        if sql is tier.TABLES_PRESENT_SQL:
            return self.migrated
        if sql is tier.FETCH_VERSIONS_PRESENT_SQL:
            return self.fetch_versions
        if sql is tier.STILL_COLD_ELIGIBLE_SQL:
            sha = args[0]
            recent = any(r["sha"] == sha and r.get("recent") for r in self.rows)
            return (not recent) and sha in self.cold
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if sql is tier.PENDING_HOT_REMOVAL_SQL:   # The resume sweep
            return [{"sha256": s} for s in self.cold if s not in self.hot_removed][: args[0]]
        assert sql is tier.CANDIDATES_SQL
        return [r for r in self.rows if r["sha"] not in self.cold][: args[1]]

    async def fetchrow(self, sql, *args):
        assert sql is tier.COLD_LOCATION_SQL
        c = self.cold.get(args[0])
        return None if c is None else {"bundle_key": c[0], "member_offset": c[1], "member_length": c[2]}

    async def execute(self, sql, *args):
        if sql is tier.INSERT_BUNDLE_SQL:
            self.bundles.setdefault(args[0], args)
        elif sql is tier.INSERT_COLD_SQL:
            self.cold.setdefault(args[0], args[1:])
        elif sql is tier.MARK_HOT_REMOVED_SQL:
            self.hot_removed.add(args[0])
        else:
            raise AssertionError(sql)

    @contextlib.asynccontextmanager
    async def _tx(self):
        yield

    def transaction(self):
        return self._tx()


def _tier_setup(tmp_path, cold):
    hot = EvidenceStore(tmp_path / "hot", cold=cold)
    bodies = _bodies(6)
    rows = []
    for i, b in enumerate(bodies):
        s = hot.put(b).sha256
        rows.append({"sha": s, "month": "2026-07" if i < 4 else "2026-08"})
    return hot, bodies, rows


def test_tier_refuses_without_backend_or_migration(tmp_path, cold):
    hot, _, rows = _tier_setup(tmp_path, cold)
    with pytest.raises(tier_mod.Refused, match="no cold backend"):
        asyncio.run(tier_mod.run_tier(TierConn(rows), hot, None, tier.TierPolicy(),
                                      apply=True, limit=100, spool=tmp_path / "spool"))
    with pytest.raises(tier_mod.Refused, match="0243"):
        asyncio.run(tier_mod.run_tier(TierConn(rows, migrated=False), hot, cold, tier.TierPolicy(),
                                      apply=True, limit=100, spool=tmp_path / "spool"))


def test_tier_main_refuses_with_no_backend_configured(monkeypatch, capsys):
    monkeypatch.delenv("LEGBA_ARCHIVE_COLD_BACKEND", raising=False)
    assert tier_mod.main([]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_tier_dry_run_moves_nothing(tmp_path, cold):
    hot, bodies, rows = _tier_setup(tmp_path, cold)
    conn = TierConn(rows)
    rep = asyncio.run(tier_mod.run_tier(conn, hot, cold, tier.TierPolicy(), apply=False,
                                        limit=100, spool=tmp_path / "spool"))
    assert rep["candidates"] == 6 and rep["bundles_planned"] == 2
    assert set(rep["months"]) == {"2026-07", "2026-08"}
    assert conn.cold == {} and all(hot.get(_sha(b)) == b for b in bodies)


def test_tier_apply_packs_verifies_records_then_retires(tmp_path, cold):
    hot, bodies, rows = _tier_setup(tmp_path, cold)
    rows[5]["recent"] = True             # a row inside the hot window cites it now
    conn = TierConn(rows)
    policy = tier.TierPolicy(hot_days=90, pack_bytes=1 << 30)
    rep = asyncio.run(tier_mod.run_tier(conn, hot, cold, policy, apply=True, limit=100,
                                        spool=tmp_path / "spool"))
    assert rep["bundles_uploaded"] == 2 and rep["objects_tiered"] == 6
    assert rep["hot_removed"] == 5 and rep["hot_kept_recent"] == 1
    assert len(conn.bundles) == 2 and len(conn.cold) == 6
    assert not list((tmp_path / "spool").iterdir())                 # spool cleaned
    # Every object — retired or not — still reads back as the original bytes.
    for b in bodies:
        assert asyncio.run(tier.read_original(conn, hot, _sha(b))) == b
    assert hot.path(rows[5]["sha"]).exists()
    assert not hot.path(rows[0]["sha"]).exists()
    # A second run finds nothing new.
    rep2 = asyncio.run(tier_mod.run_tier(conn, hot, cold, policy, apply=True, limit=100,
                                         spool=tmp_path / "spool"))
    assert rep2["candidates"] == 0


def test_tier_upload_that_reads_back_wrong_keeps_hot(tmp_path):
    class LyingBackend(LocalFsBackend):
        def get_to_file(self, key, path):
            super().get_to_file(key, path)
            raw = bytearray(Path(path).read_bytes())
            mid = len(raw) // 3
            raw[mid:mid + 64] = bytes(b ^ 0xFF for b in raw[mid:mid + 64])
            Path(path).write_bytes(bytes(raw))

    cold = LyingBackend(tmp_path / "cold", name="cold-lying")
    hot, bodies, rows = _tier_setup(tmp_path, cold)
    conn = TierConn(rows)
    rep = asyncio.run(tier_mod.run_tier(conn, hot, cold, tier.TierPolicy(), apply=True,
                                        limit=100, spool=tmp_path / "spool"))
    assert len(rep["bundles_failed"]) == 2 and rep["hot_removed"] == 0
    assert conn.cold == {} and all(hot.get(_sha(b)) == b for b in bodies)


def test_tier_mismatching_hot_object_is_not_packed(tmp_path, cold):
    hot, bodies, rows = _tier_setup(tmp_path, cold)
    hot.path(rows[0]["sha"]).write_bytes(b"rot")
    conn = TierConn(rows)
    rep = asyncio.run(tier_mod.run_tier(conn, hot, cold, tier.TierPolicy(), apply=True,
                                        limit=100, spool=tmp_path / "spool"))
    assert rep["mismatches"] == [rows[0]["sha"]]
    assert rows[0]["sha"] not in conn.cold
    assert hot.path(rows[0]["sha"]).read_bytes() == b"rot"          # never touched


def test_tier_policy_from_env():
    p = tier.TierPolicy.from_env({"LEGBA_ARCHIVE_HOT_DAYS": "120", "LEGBA_ARCHIVE_PACK_MB": "64",
                                  "LEGBA_ARCHIVE_COLD_BACKEND": "S3"})
    assert (p.hot_days, p.pack_bytes, p.cold_backend) == (120, 64 << 20, "s3")
    assert tier.TierPolicy.from_env({"LEGBA_ARCHIVE_HOT_DAYS": "-1"}).hot_days == 90


def test_cold_location_none_before_migration():
    conn = TierConn([], migrated=False)
    assert asyncio.run(tier.cold_location(conn, "a" * 64)) is None


def test_migration_0243_is_additive():
    sql = (REPO / "src/legba/data/migrations/0243_evidence_archive_tiering.sql").read_text()
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "DROP" not in body.upper() and "ALTER TABLE" not in body.upper()
    assert body.count("IF NOT EXISTS") == 3
    _ = ColdLocation  # the public read type stays importable


def test_tier_fetch_variants_actually_extend_the_base_statements():
    """The N-L variants are built by str.replace on the base SQL; a drifted
    anchor would silently yield the base statement (no fetch_versions)."""
    assert tier.CANDIDATES_WITH_FETCH_SQL != tier.CANDIDATES_SQL
    assert "fetch_versions" in tier.CANDIDATES_WITH_FETCH_SQL
    assert tier.STILL_COLD_ELIGIBLE_WITH_FETCH_SQL != tier.STILL_COLD_ELIGIBLE_SQL
    assert "fetch_versions" in tier.STILL_COLD_ELIGIBLE_WITH_FETCH_SQL
