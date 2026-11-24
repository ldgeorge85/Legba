# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The evidence archiver's two N-L hooks (kept out of the archiver module).

1. :func:`load_ingest_copy` — before re-fetching a cited signal's page, look
   for the bytes the POLL kept (``fetch_version_records`` → ``fetch_versions``,
   migration 0245): the signal's ``page`` link, or its ``response`` link when
   that response IS the page (same canonical address). Found and intact in the
   evidence store → the archiver archives THOSE bytes; the re-fetch is the
   fallback for signals with no ingest-time copy. Knob
   ``options.prefer_ingest_copy`` (default true). A copy that has aged into
   the COLD tier is read from there (``archive_tier.read_original``: the
   recorded location, the configured backend); only when the cold read fails
   (backend down, damaged bundle — logged as a warning, nothing marked) does
   the archiver fall back to the re-fetch.
2. :func:`observe_refetch` — when the archiver does re-fetch, the result is
   recorded as a version of the page's address (role ``archiver``), so a page
   that changed (different extracted-text hash) or stopped answering (an HTTP
   error status) since an earlier version leaves one plain drift row. No
   classification, no flag, no re-run.

Both degrade to "not available" when migration 0245 is not applied yet —
the archiver then behaves exactly as before.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..._url_canon import canonical_url
from ...archive_store import ColdTierError, EvidenceStore
from ...archive_tier import read_original

logger = logging.getLogger(__name__)

__all__ = ["load_ingest_copy", "observe_refetch"]

_INGEST_COPY_SQL = """
SELECT v.sha256, v.content_type, r.role
  FROM fetch_version_records r
  JOIN fetch_versions v ON v.id = r.version_id
 WHERE r.record_kind = 'signal' AND r.record_id = $1
   AND v.object_ref IS NOT NULL AND v.bytes_pruned_at IS NULL
   AND (r.role = 'page' OR v.address = $2)
 ORDER BY (r.role = 'page') DESC
 LIMIT 1
"""


def _missing_table(exc: Exception) -> bool:
    return type(exc).__name__ in {"UndefinedTableError", "UndefinedColumnError"}


async def load_ingest_copy(
    pool: Any, signal_id: Any, url: str, root: Path,
) -> tuple[bytes, str | None, str | None] | None:
    """``(body, content_type, charset)`` of the signal's ingest-time page copy,
    re-verified against its sha256, or ``None``."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_INGEST_COPY_SQL, signal_id, canonical_url(url) or url)
            try:
                sha, ctype = (row["sha256"], row["content_type"]) if row is not None else (None, None)
            except (KeyError, TypeError, IndexError):
                return None
            if not isinstance(sha, str):
                return None
            try:
                body = await read_original(conn, EvidenceStore.from_env(root), sha)
            except ColdTierError as exc:
                logger.warning(
                    "evidence_archiver.ingest_copy.cold_unreadable signal=%s sha=%s err=%s "
                    "— falling back to the re-fetch", signal_id, sha, exc,
                )
                return None
    except Exception as exc:
        if not _missing_table(exc):
            logger.warning("evidence_archiver.ingest_copy.lookup_failed signal=%s err=%s", signal_id, exc)
        return None
    if body is None:
        return None
    charset = None
    if ctype and "charset=" in ctype:
        charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or None
    return body, ctype, charset


async def observe_refetch(
    pool: Any, url: str, *, root: Path, body: bytes | None, content_type: str | None,
    error_status: int | None, source_id: str | None,
) -> None:
    """Record the archiver's fallback fetch as a page version (or a 'gone'
    observation on an HTTP error status). Best-effort, never raises."""
    from ....data.sources.fetch_capture import FetchCapture  # noqa: PLC0415
    from ....runtime.fetch_receipts import VersionWriter  # noqa: PLC0415

    now = datetime.now(tz=timezone.utc)
    writer = VersionWriter(root=root)
    try:
        async with pool.acquire() as conn:
            if body is not None:
                cap = FetchCapture(
                    url=url, method="GET", status=200, content_type=content_type,
                    content_encoding=None, role="archiver", fetched_at=now,
                    complete=True, body=body, sha256=hashlib.sha256(body).hexdigest(),
                    size=len(body),
                )
                await writer.write(conn, cap, source_id=source_id, observed_by="archiver", store=False)
            elif error_status is not None:
                await writer.note_gone(
                    conn, url=url, status=error_status, fetched_at=now,
                    source_id=source_id, observed_by="archiver",
                )
    except Exception as exc:
        if not _missing_table(exc):
            logger.warning("evidence_archiver.version_observe_failed url=%s err=%s", url, exc)
