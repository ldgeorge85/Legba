# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.archive_store — the evidence archive's storage interface.

Two layers, kept apart so a later backend (the planned operator-side
content-addressed store) slots in without touching a caller:

* :class:`BlobBackend` — dumb keyed bytes. ``put`` (never overwrites: an
  existing key is left alone and reported), ``put_file``, ``get`` (optionally a
  byte range), ``stat``, ``exists``. There is deliberately NO delete: the
  archive's rule is never-overwrite / never-lose, and the two places that do
  remove a HOT copy (``archive_retention`` for uncited rows, the tier mover
  after a verified cold copy) do it on the local filesystem with their own
  guards, not through a backend any caller could reach.
  Implementations: :class:`LocalFsBackend` (the archive volume, and a
  "cold" directory for tests or a second disk) and :class:`S3Backend` (any
  S3-compatible endpoint; ``botocore``, already in the runtime image as
  aiobotocore's dependency — no new dependency).

* :class:`EvidenceStore` — the content-addressed hot archive on top of the
  local layout ``{root}/{sha[:2]}/{sha}`` (:mod:`legba.data.archive` owns the
  format). ``put`` stores the ORIGINAL's at-rest encoding
  (:mod:`legba.data.archive_codec`; zstd by default), ``get`` returns the
  original bytes re-verified against the address — from the hot copy, or,
  given a :class:`ColdLocation`, from a member range of a cold bundle.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterable, Protocol, runtime_checkable
from uuid import uuid4

from . import archive_codec
from .archive import cas_object_ref, cas_path


class BlobNotFound(KeyError):
    """The key is not present on the backend."""


class ColdBackendNotConfigured(RuntimeError):
    """No cold backend (or an incomplete one) is configured."""


class ColdTierError(RuntimeError):
    """A cold read could not return the object it was asked for. Raised, never
    swallowed into ``None``: an object that HAS a recorded cold location and
    cannot be read is an outage or damage to report, not a miss. Nothing is
    marked lost by raising it."""


class ColdTierUnavailable(ColdTierError):
    """The cold backend could not be reached (or answered with an error)."""


class ColdObjectInvalid(ColdTierError):
    """The recorded bundle is missing from the backend, or its member does not
    decode to the address it is recorded under."""


@dataclass(frozen=True)
class BlobStat:
    key: str
    size: int


@runtime_checkable
class BlobBackend(Protocol):
    """Keyed immutable bytes. No delete by design (see module docstring)."""

    name: str

    def put(self, key: str, data: bytes) -> bool:
        """Store ``data`` at ``key``; ``False`` (and nothing written) if present."""
        ...

    def put_file(self, key: str, path: Path) -> bool:
        """As :meth:`put`, streaming from a local file."""
        ...

    def get(self, key: str, *, start: int | None = None, length: int | None = None) -> bytes:
        """The bytes at ``key`` (or ``length`` bytes from ``start``)."""
        ...

    def get_to_file(self, key: str, path: Path) -> None:
        """Download ``key`` to the local ``path`` (overwritten)."""
        ...

    def stat(self, key: str) -> BlobStat | None:
        ...

    def exists(self, key: str) -> bool:
        ...


def _safe_key(key: str) -> str:
    p = PurePosixPath(key)
    if not key or p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts):
        raise ValueError(f"unsafe blob key {key!r}")
    return str(p)


def _sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(dest: Path, data: bytes | None = None, *, src: BinaryIO | None = None,
                 mode: int | None = None) -> None:
    """Temp file in ``dest``'s directory, fsync, rename over ``dest``.

    Readers see the old object or the new one, never a partial one."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".tmp.{os.getpid()}.{uuid4().hex[:8]}.{dest.name[:12]}"
    try:
        with open(tmp, "wb") as fh:
            if data is not None:
                fh.write(data)
            elif src is not None:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(dest.parent)


class LocalFsBackend:
    """Keys are relative paths under ``root``."""

    def __init__(self, root: Path, *, name: str = "local") -> None:
        self.root = Path(root)
        self.name = name

    def _path(self, key: str) -> Path:
        return self.root / _safe_key(key)

    def put(self, key: str, data: bytes) -> bool:
        dest = self._path(key)
        if dest.exists():
            return False
        atomic_write(dest, data)
        return True

    def put_file(self, key: str, path: Path) -> bool:
        dest = self._path(key)
        if dest.exists():
            return False
        with open(path, "rb") as src:
            atomic_write(dest, src=src)
        return True

    def get(self, key: str, *, start: int | None = None, length: int | None = None) -> bytes:
        path = self._path(key)
        try:
            with open(path, "rb") as fh:
                if start is not None:
                    fh.seek(start)
                return fh.read() if length is None else fh.read(length)
        except FileNotFoundError as exc:
            raise BlobNotFound(key) from exc

    def get_to_file(self, key: str, path: Path) -> None:
        src = self._path(key)
        if not src.exists():
            raise BlobNotFound(key)
        with open(src, "rb") as fh:
            atomic_write(Path(path), src=fh)

    def replace_damaged(self, key: str, path: Path, expected_sha256: str) -> bool:
        """Overwrite ``key`` with ``path`` ONLY when the object there does not
        hash to ``expected_sha256`` (its own content address) and ``path``
        does. ``True`` when replaced. See :func:`archive_tier.upload_and_verify`."""
        dest = self._path(key)
        if _sha256_path(Path(path)) != expected_sha256:
            return False
        if dest.exists() and _sha256_path(dest) == expected_sha256:
            return False
        with open(path, "rb") as src:
            atomic_write(dest, src=src)
        return True

    def stat(self, key: str) -> BlobStat | None:
        try:
            return BlobStat(key, self._path(key).stat().st_size)
        except FileNotFoundError:
            return None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3Backend:
    """Any S3-compatible object store, via ``botocore`` (no vendor assumed).

    Credentials are read from the env vars NAMED by ``access_key_env`` /
    ``secret_key_env`` at client build time; the values are never logged or
    stored on the instance. ``path_style`` addresses ``endpoint/bucket/key``
    (what most self-hosted stores want). A ``client`` may be injected (tests)."""

    def __init__(
        self, *, bucket: str, endpoint: str | None = None, prefix: str = "",
        region: str | None = None, path_style: bool = True,
        access_key_env: str = "LEGBA_ARCHIVE_S3_ACCESS_KEY_ID",
        secret_key_env: str = "LEGBA_ARCHIVE_S3_SECRET_ACCESS_KEY",
        client: Any = None, name: str = "s3",
    ) -> None:
        if not bucket:
            raise ColdBackendNotConfigured("S3 backend needs a bucket")
        self.bucket = bucket
        self.endpoint = endpoint
        self.prefix = prefix.strip("/")
        self.region = region
        self.path_style = path_style
        self.access_key_env = access_key_env
        self.secret_key_env = secret_key_env
        self.name = name
        self._client = client

    def _c(self) -> Any:
        if self._client is None:
            try:
                import botocore.session  # noqa: PLC0415 — lazy, runtime image only
                from botocore.config import Config  # noqa: PLC0415
            except ImportError as exc:
                raise ColdBackendNotConfigured(
                    "S3 backend needs botocore (shipped in the runtime image "
                    "via aiobotocore); not importable here"
                ) from exc
            key = os.environ.get(self.access_key_env)
            secret = os.environ.get(self.secret_key_env)
            if not key or not secret:
                raise ColdBackendNotConfigured(
                    f"S3 credentials missing: set {self.access_key_env} and "
                    f"{self.secret_key_env}"
                )
            self._client = botocore.session.get_session().create_client(
                "s3", endpoint_url=self.endpoint or None,
                region_name=self.region or "us-east-1",
                aws_access_key_id=key, aws_secret_access_key=secret,
                # Bounded timeouts: an unreachable endpoint fails a read in
                # seconds with a ColdTierUnavailable, not after minutes.
                config=Config(s3={"addressing_style": "path" if self.path_style else "auto"},
                              connect_timeout=5, read_timeout=120,
                              retries={"max_attempts": 3, "mode": "standard"}),
            )
        return self._client

    def _k(self, key: str) -> str:
        key = _safe_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    @staticmethod
    def _is_404(exc: Exception) -> bool:
        resp = getattr(exc, "response", None) or {}
        code = str((resp.get("Error") or {}).get("Code", ""))
        status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        return code in {"404", "NoSuchKey", "NotFound"} or status == 404

    def stat(self, key: str) -> BlobStat | None:
        try:
            head = self._c().head_object(Bucket=self.bucket, Key=self._k(key))
        except Exception as exc:
            if self._is_404(exc):
                return None
            raise
        return BlobStat(key, int(head["ContentLength"]))

    def exists(self, key: str) -> bool:
        return self.stat(key) is not None

    def put(self, key: str, data: bytes) -> bool:
        if self.exists(key):
            return False
        self._c().put_object(
            Bucket=self.bucket, Key=self._k(key), Body=data,
            ContentType="application/octet-stream",
        )
        return True

    def put_file(self, key: str, path: Path) -> bool:
        if self.exists(key):
            return False
        with open(path, "rb") as fh:
            self._c().put_object(
                Bucket=self.bucket, Key=self._k(key), Body=fh,
                ContentLength=Path(path).stat().st_size,
                ContentType="application/octet-stream",
            )
        return True

    def get(self, key: str, *, start: int | None = None, length: int | None = None) -> bytes:
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": self._k(key)}
        if start is not None or length is not None:
            lo = start or 0
            kwargs["Range"] = f"bytes={lo}-" if length is None else f"bytes={lo}-{lo + length - 1}"
        try:
            resp = self._c().get_object(**kwargs)
        except Exception as exc:
            if self._is_404(exc):
                raise BlobNotFound(key) from exc
            raise
        return resp["Body"].read()

    def get_to_file(self, key: str, path: Path) -> None:
        try:
            resp = self._c().get_object(Bucket=self.bucket, Key=self._k(key))
        except Exception as exc:
            if self._is_404(exc):
                raise BlobNotFound(key) from exc
            raise
        atomic_write(Path(path), src=resp["Body"])

    def replace_damaged(self, key: str, path: Path, expected_sha256: str) -> bool:
        """As :meth:`LocalFsBackend.replace_damaged`: overwrite only an object
        that does not hash to its own content address, only with bytes that do."""
        if _sha256_path(Path(path)) != expected_sha256:
            return False
        h = hashlib.sha256()
        try:
            body = self._c().get_object(Bucket=self.bucket, Key=self._k(key))["Body"]
            for chunk in iter(lambda: body.read(1 << 20), b""):
                h.update(chunk)
            if h.hexdigest() == expected_sha256:
                return False
        except Exception as exc:
            if not self._is_404(exc):
                raise
        with open(path, "rb") as fh:
            self._c().put_object(
                Bucket=self.bucket, Key=self._k(key), Body=fh,
                ContentLength=Path(path).stat().st_size,
                ContentType="application/octet-stream",
            )
        return True


# ---------------------------------------------------------------------------
# Cold backend from env
# ---------------------------------------------------------------------------

COLD_BACKEND_ENV = "LEGBA_ARCHIVE_COLD_BACKEND"         # "" (none) | "local" | "s3"
COLD_LOCAL_ROOT_ENV = "LEGBA_ARCHIVE_COLD_LOCAL_ROOT"
S3_ENDPOINT_ENV = "LEGBA_ARCHIVE_S3_ENDPOINT"
S3_BUCKET_ENV = "LEGBA_ARCHIVE_S3_BUCKET"
S3_PREFIX_ENV = "LEGBA_ARCHIVE_S3_PREFIX"
S3_REGION_ENV = "LEGBA_ARCHIVE_S3_REGION"
S3_PATH_STYLE_ENV = "LEGBA_ARCHIVE_S3_PATH_STYLE"
S3_ACCESS_KEY_ENV_ENV = "LEGBA_ARCHIVE_S3_ACCESS_KEY_ENV"
S3_SECRET_KEY_ENV_ENV = "LEGBA_ARCHIVE_S3_SECRET_KEY_ENV"


def cold_backend_from_env(env: dict[str, str] | None = None) -> BlobBackend:
    """The configured cold backend; raises :class:`ColdBackendNotConfigured`
    when none (or an incomplete one) is configured — the tier mover refuses."""
    e = os.environ if env is None else env
    kind = (e.get(COLD_BACKEND_ENV) or "").strip().lower()
    if kind == "local":
        root = (e.get(COLD_LOCAL_ROOT_ENV) or "").strip()
        if not root:
            raise ColdBackendNotConfigured(f"{COLD_BACKEND_ENV}=local needs {COLD_LOCAL_ROOT_ENV}")
        return LocalFsBackend(Path(root), name="cold-local")
    if kind == "s3":
        bucket = (e.get(S3_BUCKET_ENV) or "").strip()
        if not bucket:
            raise ColdBackendNotConfigured(f"{COLD_BACKEND_ENV}=s3 needs {S3_BUCKET_ENV}")
        return S3Backend(
            bucket=bucket,
            endpoint=(e.get(S3_ENDPOINT_ENV) or "").strip() or None,
            prefix=(e.get(S3_PREFIX_ENV) or "legba-archive").strip(),
            region=(e.get(S3_REGION_ENV) or "").strip() or None,
            path_style=(e.get(S3_PATH_STYLE_ENV, "1").strip().lower() not in {"0", "false", "no"}),
            access_key_env=(e.get(S3_ACCESS_KEY_ENV_ENV) or "LEGBA_ARCHIVE_S3_ACCESS_KEY_ID").strip(),
            secret_key_env=(e.get(S3_SECRET_KEY_ENV_ENV) or "LEGBA_ARCHIVE_S3_SECRET_ACCESS_KEY").strip(),
            name="cold-s3",
        )
    if kind:
        raise ColdBackendNotConfigured(f"{COLD_BACKEND_ENV}={kind!r} is not a known backend (local|s3)")
    raise ColdBackendNotConfigured(
        f"no cold backend configured ({COLD_BACKEND_ENV} unset) — tiering refuses to run"
    )


# ---------------------------------------------------------------------------
# The content-addressed hot archive
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PutResult:
    sha256: str
    object_ref: str
    existed: bool
    stored_size: int | None   # bytes at rest (None when the object already existed)
    codec: str | None


@dataclass(frozen=True)
class ColdLocation:
    """Where a retired object's at-rest bytes live: ``length`` bytes at
    ``offset`` inside the bundle object ``bundle_key`` on the cold backend."""

    bundle_key: str
    offset: int
    length: int


class EvidenceStore:
    """The CAS archive at ``root`` with transparent codecs and a cold fallback."""

    def __init__(self, root: Path, *, codec: str | None = None, level: int | None = None,
                 cold: BlobBackend | None = None,
                 locator: Callable[[str], "ColdLocation | None"] | None = None) -> None:
        self.root = Path(root)
        self.codec = codec
        self.level = level
        self.cold = cold
        self.locator = locator

    @classmethod
    def from_env(cls, root: Path, env: dict[str, str] | None = None) -> "EvidenceStore":
        """The store every READER should use: the hot archive at ``root`` plus,
        when ``LEGBA_ARCHIVE_COLD_BACKEND`` is configured, the cold backend and
        the local cold index (:class:`LocalColdIndex`) as its locator — so an
        object retired to cold reads back transparently with no database at
        hand. No backend configured → a plain hot store (nothing can be cold)."""
        try:
            cold = cold_backend_from_env(env)
        except ColdBackendNotConfigured:
            return cls(root)
        return cls(root, cold=cold, locator=LocalColdIndex(root).lookup)

    def path(self, sha256_hex: str) -> Path:
        return cas_path(self.root, sha256_hex.lower())

    def put(self, body: bytes) -> PutResult:
        """Content-address ``body``. An existing object is never rewritten —
        the same address IS the same original bytes, whatever its codec."""
        digest = hashlib.sha256(body).hexdigest()
        dest = self.path(digest)
        if dest.exists():
            return PutResult(digest, cas_object_ref(digest), True, None, None)
        enc = archive_codec.encode(body, codec=self.codec, level=self.level)
        atomic_write(dest, enc.data)
        return PutResult(digest, cas_object_ref(digest), False, len(enc.data), enc.codec)

    def put_stored(self, sha256_hex: str, stored: bytes) -> bool:
        """Write an already-encoded object (rehydration from cold) after
        checking it decodes to ``sha256_hex``. ``False`` if present."""
        dest = self.path(sha256_hex)
        if dest.exists():
            return False
        if archive_codec.decode_verified(stored, sha256_hex) is None:
            raise ValueError(f"stored bytes do not decode to {sha256_hex}")
        atomic_write(dest, stored)
        return True

    def read_stored(self, sha256_hex: str) -> bytes | None:
        try:
            return self.path(sha256_hex).read_bytes()
        except OSError:
            return None

    def get(self, sha256_hex: str, *, cold_location: ColdLocation | None = None,
            rehydrate: bool = False) -> bytes | None:
        """The ORIGINAL bytes for ``sha256_hex`` — verified, or ``None``.

        Hot copy first. If absent (or corrupt) and a cold backend is configured,
        the cold location is ``cold_location`` or, when not given, the
        ``locator``'s answer; the member range is then fetched from the bundle,
        and ``rehydrate`` writes the hot copy back. ``None`` means "not held
        here and not recorded as cold". A RECORDED cold object that cannot be
        read raises: :class:`ColdTierUnavailable` (backend unreachable) or
        :class:`ColdObjectInvalid` (bundle missing / member does not verify)."""
        digest = sha256_hex.lower()
        stored = self.read_stored(digest)
        if stored is not None:
            body = archive_codec.decode_verified(stored, digest)
            if body is not None:
                return body
        if self.cold is None:
            return None
        if cold_location is None and self.locator is not None:
            cold_location = self.locator(digest)
        if cold_location is None:
            return None
        try:
            member = self.cold.get(cold_location.bundle_key, start=cold_location.offset,
                                   length=cold_location.length)
        except BlobNotFound as exc:
            raise ColdObjectInvalid(
                f"{digest}: recorded bundle {cold_location.bundle_key} is missing on "
                f"cold backend {self.cold.name}"
            ) from exc
        except Exception as exc:
            raise ColdTierUnavailable(
                f"{digest}: cold backend {self.cold.name} unreachable reading "
                f"{cold_location.bundle_key}: {type(exc).__name__}: {exc}"
            ) from exc
        body = archive_codec.decode_verified(member, digest)
        if body is None:
            raise ColdObjectInvalid(
                f"{digest}: member at {cold_location.bundle_key}"
                f"[{cold_location.offset}:+{cold_location.length}] does not verify"
            )
        if rehydrate and stored is None:
            try:
                self.put_stored(digest, member)
            except OSError:
                pass  # a read never fails for want of a writable hot tier
        return body


# ---------------------------------------------------------------------------
# The local cold index (DB-free locator)
# ---------------------------------------------------------------------------

COLD_INDEX_DIR = "cold-index"


class LocalColdIndex:
    """Where each retired object lives, kept ON the hot archive volume beside
    the objects: ``{root}/cold-index/<sha[:2]>.tsv``, one line per address,
    ``sha256<TAB>bundle_key<TAB>offset<TAB>length``; the last line for an
    address wins. It lets a reader with no database connection (the
    instrument snapshot loader) find a cold object. The database
    (``evidence_archive_cold``) stays authoritative — the tier mover appends
    here after recording there, and ``scripts/archive_tier.py --rebuild-index``
    rewrites the index from it. A wrong or stale line can only cost a miss:
    every cold read is re-verified against the address."""

    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / COLD_INDEX_DIR

    def shard(self, sha256_hex: str) -> Path:
        return self.dir / f"{sha256_hex[:2].lower()}.tsv"

    def lookup(self, sha256_hex: str) -> ColdLocation | None:
        digest = sha256_hex.lower()
        try:
            text = self.shard(digest).read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        found: ColdLocation | None = None
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) != 4 or parts[0] != digest:
                continue
            try:
                found = ColdLocation(parts[1], int(parts[2]), int(parts[3]))
            except ValueError:
                continue
        return found

    def record(self, entries: Iterable[tuple[str, ColdLocation]]) -> int:
        """Append ``(sha256, location)`` lines (grouped per shard, fsynced)."""
        by_shard: dict[Path, list[str]] = {}
        n = 0
        for sha, loc in entries:
            digest = sha.lower()
            by_shard.setdefault(self.shard(digest), []).append(
                f"{digest}\t{_safe_key(loc.bundle_key)}\t{int(loc.offset)}\t{int(loc.length)}\n")
            n += 1
        if by_shard:
            self.dir.mkdir(parents=True, exist_ok=True)
        for path, lines in by_shard.items():
            with open(path, "a", encoding="ascii") as fh:
                fh.write("".join(lines))
                fh.flush()
                os.fsync(fh.fileno())
        if by_shard:
            _fsync_dir(self.dir)
        return n

    def rewrite(self, entries: Iterable[tuple[str, ColdLocation]]) -> int:
        """Replace the whole index with ``entries`` (each shard atomically)."""
        by_shard: dict[Path, list[str]] = {}
        n = 0
        for sha, loc in entries:
            digest = sha.lower()
            by_shard.setdefault(self.shard(digest), []).append(
                f"{digest}\t{_safe_key(loc.bundle_key)}\t{int(loc.offset)}\t{int(loc.length)}\n")
            n += 1
        self.dir.mkdir(parents=True, exist_ok=True)
        for old in self.dir.glob("*.tsv"):
            if old not in by_shard:
                old.unlink()
        for path, lines in by_shard.items():
            atomic_write(path, "".join(lines).encode("ascii"))
        return n


__all__ = [
    "BlobBackend",
    "BlobNotFound",
    "BlobStat",
    "COLD_BACKEND_ENV",
    "COLD_LOCAL_ROOT_ENV",
    "COLD_INDEX_DIR",
    "ColdBackendNotConfigured",
    "ColdLocation",
    "ColdObjectInvalid",
    "ColdTierError",
    "ColdTierUnavailable",
    "EvidenceStore",
    "LocalColdIndex",
    "LocalFsBackend",
    "PutResult",
    "S3Backend",
    "S3_ACCESS_KEY_ENV_ENV",
    "S3_BUCKET_ENV",
    "S3_ENDPOINT_ENV",
    "S3_PATH_STYLE_ENV",
    "S3_PREFIX_ENV",
    "S3_REGION_ENV",
    "S3_SECRET_KEY_ENV_ENV",
    "atomic_write",
    "cold_backend_from_env",
]
