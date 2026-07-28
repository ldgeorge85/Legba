# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.archive — the cited-evidence archive's content-address helpers.

P2-1 (program §A3): the ``evidence_archiver`` deterministic sweep stores the
ORIGINAL bytes behind cited signals content-addressed on the archive volume:

    object bytes  →  {LEGBA_ARCHIVE_ROOT}/{sha256[:2]}/{sha256}
    object_ref    =  "cas:sha256/<hex>"          (stamped on signals.object_ref)

The recorded ``object_ref`` is a RELATIVE content address on purpose — the
archive root can move (or become an object store: MinIO/SeaweedFS are the
DIRECTION §5 candidates) without rewriting a single row, and every read
surface can derive both ``archived`` (ref present) and ``archive_sha256``
(the receipt-chain hash anchor) from the EXISTING ``signals.object_ref``
column alone — no sidecar join, no migration dependency on the read path.

This module is the ONE place that owns the address format. The writer
(:mod:`legba.data.analysts.deterministic_handlers.evidence_archiver`) and the
read projections (export/lineage/substrate reads) both import from here so
the format can never fork.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Env var naming the archive root; compose mounts the ``legba_archive`` named
#: volume at the default.
ARCHIVE_ROOT_ENV = "LEGBA_ARCHIVE_ROOT"
DEFAULT_ARCHIVE_ROOT = "/var/lib/legba/archive"

#: The object_ref scheme — a RELATIVE content address (see module docstring).
CAS_PREFIX = "cas:sha256/"

_HEX = set("0123456789abcdef")


def archive_root() -> Path:
    """The configured archive root (NOT created here — the writer owns mkdir)."""
    return Path(os.environ.get(ARCHIVE_ROOT_ENV, DEFAULT_ARCHIVE_ROOT))


def cas_object_ref(sha256_hex: str) -> str:
    """The relative content address recorded as ``object_ref``."""
    return f"{CAS_PREFIX}{sha256_hex}"


def cas_path(root: Path, sha256_hex: str) -> Path:
    """Filesystem location of a CAS object under ``root``."""
    return root / sha256_hex[:2] / sha256_hex


def sha256_from_object_ref(object_ref: str | None) -> str | None:
    """Parse the sha256 hex out of a ``cas:sha256/<hex>`` object_ref.

    The read-surface helper — export/lineage/signal projections derive
    ``archive_sha256`` from ``signals.object_ref`` with this. ``None`` for
    NULL / foreign / unparseable refs — never a fabricated hash."""
    if not object_ref or not isinstance(object_ref, str):
        return None
    if not object_ref.startswith(CAS_PREFIX):
        return None
    digest = object_ref[len(CAS_PREFIX):].strip().lower()
    if len(digest) != 64 or any(c not in _HEX for c in digest):
        return None
    return digest


__all__ = [
    "ARCHIVE_ROOT_ENV",
    "DEFAULT_ARCHIVE_ROOT",
    "CAS_PREFIX",
    "archive_root",
    "cas_object_ref",
    "cas_path",
    "sha256_from_object_ref",
]
