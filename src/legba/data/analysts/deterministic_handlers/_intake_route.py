# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The evidence archiver's re-fetch, routed through the ingestion intake.

The archiver re-fetches a cited signal's page when the poll kept no copy of
it (``_ingest_copy.py``). With ``options.fetch_via_intake`` on (default OFF)
that re-fetch becomes an intake request instead of a fetch of the
archiver's own: reason ``backfill``, sink ``receipt`` (keep the version,
write no records), under the page pipeline ``options.intake_pipeline``, as a
child of the signal. The intake then applies its admission (freshness: a
page fetched in the last hours is SATISFIED from the stored version, no
fetch at all; per-host politeness and daily caps) and the drain fetches it
through the pipeline's own acquisition code, writing the receipt. On a later
tick the archiver archives the version the request produced, exactly as it
archives a poll's ingest copy.

The archiver keeps its own fetch as the fallback, so nothing regresses:

* a host with a non-default fetch policy (impersonation — Times of Israel,
  Jerusalem Post) is never routed: the archiver's two-leg client is the
  transport that answers there, and the page pipeline's plain client is not;
* a request the intake refuses, that failed, that finished without a stored
  version (the page answered 4xx), or that is still pending after
  ``options.intake_wait_minutes`` (default 60 — the drain may be off) falls
  back to the archiver's own fetch, as before;
* a request still pending inside that wait defers the signal to a later tick
  (counted ``intake_deferred``; the signal stays a candidate).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["ARCHIVER_REQUESTER", "COPY", "DEFER", "FALLBACK", "intake_copy_or_defer"]

ARCHIVER_REQUESTER = "evidence_archiver"
COPY, DEFER, FALLBACK = "copy", "defer", "fallback"

_LAST_REQUEST_SQL = """
SELECT id, state, result_version_id, created_at FROM ingestion_requests
 WHERE parent_signal_id = $1 AND requested_by = $2 AND reason = 'backfill'
 ORDER BY created_at DESC LIMIT 1
"""

_VERSION_BYTES_SQL = """
SELECT sha256, content_type FROM fetch_versions
 WHERE id = $1 AND object_ref IS NOT NULL AND bytes_pruned_at IS NULL
"""


async def _version_copy(pool: Any, version_id: int, root: Path) -> tuple[bytes, str | None, str | None] | None:
    from ...archive_store import EvidenceStore  # noqa: PLC0415

    async with pool.acquire() as conn:
        row = await conn.fetchrow(_VERSION_BYTES_SQL, int(version_id))
    if row is None:
        return None
    body = await asyncio.to_thread(EvidenceStore(root).get, row["sha256"])
    if body is None:
        return None
    ctype = row["content_type"]
    charset = None
    if ctype and "charset=" in ctype:
        charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or None
    return body, ctype, charset


async def intake_copy_or_defer(
    pool: Any, signal_id: Any, url: str, *, pipeline: str, root: Path,
    wait_minutes: float = 60.0, now: datetime | None = None,
) -> tuple[str, Any]:
    """``(COPY, (body, content_type, charset))`` — archive these bytes;
    ``(DEFER, request_id)`` — skip this signal this tick;
    ``(FALLBACK, why)`` — fetch as the archiver always did."""
    from ...intake_store import SubmitRequest, submit_request  # noqa: PLC0415
    from ...sources._fetch_policy import DEFAULT_FETCH_POLICY, fetch_policy_for  # noqa: PLC0415

    if not pipeline:
        return FALLBACK, "no intake_pipeline"
    if fetch_policy_for(url) != DEFAULT_FETCH_POLICY:
        return FALLBACK, "host has its own fetch policy"
    now = now or datetime.now(tz=timezone.utc)
    try:
        async with pool.acquire() as conn:
            last = await conn.fetchrow(_LAST_REQUEST_SQL, signal_id, ARCHIVER_REQUESTER)
        if last is None:
            res = await submit_request(pool, SubmitRequest(
                target_kind="address", address=url, pipeline_source_id=pipeline,
                sink="receipt", reason="backfill", requested_by=ARCHIVER_REQUESTER,
                parent_signal_id=signal_id,
            ), now=now)
            state, version_id, rid = res.state, res.result_version_id, res.id
            created = now
        else:
            state, version_id, rid = last["state"], last["result_version_id"], last["id"]
            created = last["created_at"]
    except Exception as exc:  # the intake is unavailable: the archiver fetches
        logger.warning("evidence_archiver.intake_route_failed signal=%s err=%s", signal_id, exc)
        return FALLBACK, f"intake unavailable: {exc}"
    if state == "done" and version_id is not None:
        copy = await _version_copy(pool, version_id, root)
        return (COPY, copy) if copy is not None else (FALLBACK, "version bytes not held")
    if state in ("queued", "claimed") and now - created < timedelta(minutes=wait_minutes):
        return DEFER, rid
    return FALLBACK, f"intake request {rid} ended {state}"
