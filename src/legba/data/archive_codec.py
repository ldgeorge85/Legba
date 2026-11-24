# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.archive_codec — transparent at-rest codecs for the evidence archive.

The archive stores the ORIGINAL bytes behind a citation under their sha256
(:mod:`legba.data.archive`). From N-K on, the bytes may rest COMPRESSED: a
stored object is either the original itself (``identity`` — every object
written before N-K) or one zstd frame whose decompression is the original
(``zstd``). There is no sidecar flag and no database column a reader needs:
the codec is detected from the stored bytes' leading frame magic, so legacy
and compressed objects coexist with no migration.

The invariant that never moves: the content address, ``evidence_archive.sha256``
and ``size_bytes`` all describe the ORIGINAL bytes. Every receipt re-hash
decodes first, then hashes.

Ambiguity is resolved by the hash, never guessed. An ORIGINAL that itself
begins with the zstd magic (someone archived a ``.zst`` file) is written as a
zstd frame AROUND it, so one decode yields the original. A LEGACY identity
object that happens to begin with the magic decodes to something whose hash
is wrong (or fails to decode) — :func:`decode_verified` then falls back to the
raw bytes and accepts them only if THEY hash to the address. A caller holding
the content address (every CAS read does: the address is the file name)
therefore always gets the original bytes or ``None``, never a wrong body.

The binding is ``zstandard`` (python-zstandard, BSD). It is imported lazily:
an image without it still reads every identity object, writes identity (see
:func:`encode`), and raises :class:`ArchiveCodecUnavailable` — loud, never a
wrong body — the first time it meets a zstd object it cannot decode.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: zstd frame magic (RFC 8878 §3.1.1), little-endian 0xFD2FB528.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

CODEC_IDENTITY = "identity"
CODEC_ZSTD = "zstd"

#: Env knobs (documented in docs/TUNABLES.md).
CODEC_ENV = "LEGBA_ARCHIVE_CODEC"              # "zstd" (default) | "identity"
ZSTD_LEVEL_ENV = "LEGBA_ARCHIVE_ZSTD_LEVEL"    # 1..19, default 3
DEFAULT_ZSTD_LEVEL = 3

#: Hard ceiling on one decoded object. The archiver caps a fetch far below
#: this; the ceiling only stops a corrupt/hostile frame header from asking
#: for an absurd allocation.
MAX_DECODED_BYTES = 1 << 31


class ArchiveCodecUnavailable(RuntimeError):
    """A zstd object was met and no zstd binding is importable here."""


@dataclass(frozen=True)
class Encoded:
    """What :func:`encode` stored: the bytes at rest and how to read them."""

    data: bytes
    codec: str


def _zstd() -> Any | None:
    try:
        import zstandard  # noqa: PLC0415 — optional binding, lazy on purpose
    except ImportError:
        return None
    return zstandard


def zstd_available() -> bool:
    return _zstd() is not None


def is_zstd(head: bytes) -> bool:
    """True when ``head`` starts with a zstd frame magic."""
    return head[:4] == ZSTD_MAGIC


def configured_codec() -> str:
    raw = (os.environ.get(CODEC_ENV) or CODEC_ZSTD).strip().lower()
    return CODEC_IDENTITY if raw in {CODEC_IDENTITY, "none", "off", "0"} else CODEC_ZSTD


def configured_level() -> int:
    try:
        level = int(os.environ.get(ZSTD_LEVEL_ENV, DEFAULT_ZSTD_LEVEL))
    except (TypeError, ValueError):
        return DEFAULT_ZSTD_LEVEL
    return min(19, max(1, level))


_warned_no_binding = False


def encode(body: bytes, *, codec: str | None = None, level: int | None = None) -> Encoded:
    """The at-rest form of ``body``.

    ``zstd`` is the default. When the binding is missing the object is stored
    ``identity`` — evidence is never refused for want of a codec — and the
    first such write logs an ERROR naming what the image lacks. A zstd frame
    is always written with its content size, so a decode can be bounded."""
    global _warned_no_binding
    want = codec or configured_codec()
    if want == CODEC_IDENTITY:
        return Encoded(body, CODEC_IDENTITY)
    zstd = _zstd()
    if zstd is None:
        if not _warned_no_binding:
            _warned_no_binding = True
            logger.error(
                "archive_codec.no_zstd_binding — 'zstandard' is not importable "
                "in this image; archive objects are being stored UNCOMPRESSED "
                "(identity). Add zstandard to the image (Dockerfile.runtime) "
                "or set %s=identity to silence this.", CODEC_ENV,
            )
        return Encoded(body, CODEC_IDENTITY)
    lvl = configured_level() if level is None else min(19, max(1, int(level)))
    frame = zstd.ZstdCompressor(level=lvl, write_content_size=True).compress(body)
    return Encoded(frame, CODEC_ZSTD)


def decode_zstd(stored: bytes) -> bytes:
    """Decompress one zstd frame (raises on a corrupt frame)."""
    zstd = _zstd()
    if zstd is None:
        raise ArchiveCodecUnavailable(
            "archive object is zstd-compressed and the 'zstandard' binding is "
            "not importable in this process — install zstandard in this image"
        )
    return zstd.ZstdDecompressor().decompress(stored, max_output_size=MAX_DECODED_BYTES)


def decode(stored: bytes) -> bytes:
    """The original bytes of a stored object WITHOUT a hash to check against.

    Use :func:`decode_verified` whenever the content address is known — this
    form cannot tell a legacy object that merely begins with the magic from a
    compressed one."""
    return decode_zstd(stored) if is_zstd(stored) else stored


def decode_verified(stored: bytes, sha256_hex: str) -> bytes | None:
    """The original bytes whose sha256 is ``sha256_hex``, or ``None``.

    Never returns bytes that do not hash to the address."""
    want = sha256_hex.lower()
    if is_zstd(stored):
        try:
            body = decode_zstd(stored)
        except ArchiveCodecUnavailable:
            # A legacy identity object could begin with the magic; only the
            # raw hash can say. If it matches, the raw bytes ARE the original.
            if hashlib.sha256(stored).hexdigest() == want:
                return stored
            raise
        except Exception:  # corrupt frame → maybe a legacy object, checked below
            body = None
        if body is not None and hashlib.sha256(body).hexdigest() == want:
            return body
    return stored if hashlib.sha256(stored).hexdigest() == want else None


def stored_codec(stored: bytes, sha256_hex: str) -> str | None:
    """Which codec ``stored`` is at rest under, given its address; ``None``
    when it is neither (a corrupt or mismatching object)."""
    want = sha256_hex.lower()
    if is_zstd(stored):
        try:
            if hashlib.sha256(decode_zstd(stored)).hexdigest() == want:
                return CODEC_ZSTD
        except ArchiveCodecUnavailable:
            raise
        except Exception:
            pass
    return CODEC_IDENTITY if hashlib.sha256(stored).hexdigest() == want else None


__all__ = [
    "ArchiveCodecUnavailable",
    "CODEC_ENV",
    "CODEC_IDENTITY",
    "CODEC_ZSTD",
    "DEFAULT_ZSTD_LEVEL",
    "Encoded",
    "MAX_DECODED_BYTES",
    "ZSTD_LEVEL_ENV",
    "ZSTD_MAGIC",
    "configured_codec",
    "configured_level",
    "decode",
    "decode_verified",
    "decode_zstd",
    "encode",
    "is_zstd",
    "stored_codec",
    "zstd_available",
]
