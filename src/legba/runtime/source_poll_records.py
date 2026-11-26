# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The poll's record plumbing beside the document path (N-L).

Kept out of ``source_actor.py`` (size gate) and out of the sinks (they do not
know about polls): the capture scope around the handler's iterator, the
routing of a non-document record to its sink, and the end-of-poll flush of
batched sinks and receipts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from ..data.sources._http_version import ProtocolPosture, posture_scope
from ..data.sources.fetch_capture import CaptureLog, capture_scope
from .fetch_receipts import ReceiptRecorder, RecordLink, current_inbound, link_inbound_record

logger = logging.getLogger("legba.runtime.source_actor")

__all__ = ["captured", "protocol_posture", "emit_record", "finish_poll", "link_inbound", "route_record", "sink_summary"]


def captured(agen: Any, log: CaptureLog | None, posture: ProtocolPosture | None = None) -> Any:
    """``agen`` with every step run inside the capture scope of ``log``.

    With receipts off (``log is None``) this returns ``agen`` ITSELF — the
    actor iterates the handler exactly as before. The scope covers only the
    handler's own steps, never the actor's enrichment between yields, so the
    NLP/geocode clients are never captured."""
    if log is None and posture is None:
        return agen
    return _scoped(agen, log, posture)


async def _scoped(agen: Any, log: CaptureLog | None, posture: ProtocolPosture | None) -> AsyncIterator[Any]:
    it = agen.__aiter__()
    try:
        while True:
            with capture_scope(log), posture_scope(posture):
                try:
                    item = await it.__anext__()
                except StopAsyncIteration:
                    return
            yield item
    finally:
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            with capture_scope(log), posture_scope(posture):
                await aclose()


def sink_summary(sink_counts: dict[str, Any]) -> str:
    """The ``sinks=`` field of ``source_actor.pull.done``: ``-`` for a
    document source, else ``observation:written=W,revised=R,unchanged=U,rejected=X``
    per family — so an observation-only poll logs its completion too."""
    if not sink_counts:
        return "-"
    return ";".join(
        f"{family}:" + ",".join(f"{k}={c.get(k, 0)}" for k in ("written", "revised", "unchanged", "rejected"))
        for family, c in sorted(sink_counts.items()) if isinstance(c, dict)
    ) or "-"


def route_record(core: Any, record: Any, link: RecordLink, receipts: ReceiptRecorder) -> None:
    """Hand a non-document record to its family's sink (buffered until
    :func:`finish_poll`). The record gets the sha256 of the response it was
    parsed from unless its handler already named one."""
    sink = core._sinks.for_record(record)
    if getattr(record, "fetch_sha256", "absent") is None:
        sha = receipts.sha_of(link.response)
        if sha is not None:
            record.fetch_sha256 = sha
    record_links = getattr(core, "_record_links", None)
    if record_links is None:
        record_links = core._record_links = {}
    record_links[id(record)] = link
    sink.accept(record)


async def finish_poll(core: Any, receipts: ReceiptRecorder) -> dict[str, Any]:
    """Flush batched sinks, persist receipts, link the written records.

    Best-effort and self-contained (its own connection): never raises, never
    masks the poll. Returns per-family counts (empty for a document source,
    which keeps that source's outcome row the statement it always was)."""
    batched = core._sinks.batched()
    has_captures = receipts.log is not None and bool(receipts.log.captures)
    if not batched and not has_captures:
        return {}
    counts: dict[str, Any] = {}
    links = getattr(core, "_record_links", None) or {}
    core._record_links = {}
    try:
        async with core.deps.pg_pool.acquire() as conn:
            await receipts.flush(conn)
            for sink in batched:
                result = await sink.flush(conn, polled_at=datetime.now(tz=timezone.utc))
                counts[sink.family] = result.as_counts()
                if result.rejected:
                    counts[sink.family]["rejected_reasons"] = sorted(set(result.rejected))[:20]
                await receipts.link_many(conn, sink.family, [   # N-R: batched links
                    (new_id, link) for new_id, rec in result.written_ids
                    if (link := links.get(id(rec))) is not None])
    except Exception:
        logger.warning(
            "source_actor.finish_poll.failed actor_id=%s", core.actor_id, exc_info=True,
        )
    return counts


# ---------------------------------------------------------------------------
# PUSH path — the same typed records, the same sinks, no cadence/cursor/outcome
# ---------------------------------------------------------------------------


async def emit_record(core: Any, record: Any) -> None:
    """A PUSHED non-document record (e.g. a numeric reading → ``observation``):
    route it to its family's sink and flush at once — a push event is its own
    unit, there is no poll to batch over. Errors propagate so the inbound
    drain NAKs and redelivers (the sinks are idempotent: an unchanged
    observation writes nothing)."""
    env = current_inbound()
    sink = core._sinks.for_record(record)
    if getattr(record, "fetch_sha256", "absent") is None and env is not None:
        import hashlib  # noqa: PLC0415

        record.fetch_sha256 = hashlib.sha256(env.body).hexdigest()
    sink.accept(record)
    async with core.deps.pg_pool.acquire() as conn:
        result = await sink.flush(conn, polled_at=datetime.now(tz=timezone.utc))
        if env is not None:
            for new_id, _rec in result.written_ids:
                try:
                    await link_inbound_record(conn, env, sink.family, new_id)
                except Exception:
                    logger.warning("source_actor.inbound_link.failed actor_id=%s", core.actor_id,
                                   exc_info=True)
    if result.rejected:
        logger.warning("source_actor.push.rejected actor_id=%s reasons=%s",
                       core.actor_id, sorted(set(result.rejected))[:5])


async def link_inbound(core: Any, record_kind: str, record_id: Any) -> None:
    """Link a pushed record to the envelope it came from (best-effort; a
    no-op outside the inbound drain or with receipts off)."""
    env = current_inbound()
    if env is None:
        return
    try:
        async with core.deps.pg_pool.acquire() as conn:
            await link_inbound_record(conn, env, record_kind, record_id)
    except Exception:
        logger.warning("source_actor.inbound_link.failed actor_id=%s", core.actor_id, exc_info=True)


def protocol_posture(descriptor: Any, cursor_raw: Any) -> ProtocolPosture:
    """This poll's protocol posture: the descriptor's ``fetch.http_version``
    and the per-host HTTP/1.1 memory carried on the actor cursor."""
    mode = getattr(getattr(descriptor, "fetch", None), "http_version", "auto")
    memory = cursor_raw.get("fetch_protocol_memory") if isinstance(cursor_raw, dict) else None
    return ProtocolPosture(mode=mode, memory=dict(memory) if isinstance(memory, dict) else {})
