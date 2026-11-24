# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch receipts — persist what a poll received and link every record to it.

The capture half is :mod:`legba.data.sources.fetch_capture` (a tee on the
egress transport). This module is the write half, run by the source actor
inside the poll it belongs to:

* each complete 2xx capture becomes (or re-sights) a VERSION of its canonical
  address in ``fetch_versions`` (migration 0245): the body goes to the
  evidence store (content-addressed, zstd at rest, deduplicated by hash), the
  row carries source, poll time, URL, status, content type, sha256 and size.
  An identical body for the same address is the SAME version: ``last_seen_at``
  and ``fetch_count`` move, nothing is stored. A different body is a new row.
* each written record is linked to the version it was parsed from
  (``fetch_version_records``, insert-only) — a signal by role ``response``
  (the feed/API body) and, when the handler fetched its article page at
  ingest, role ``page``. An observation also carries the sha256 on its row.
* a PAGE version also stores the main text the platform already extracts
  (the evidence archiver's Trafilatura pass) and its sha256. When a later
  fetch of the same page address — another poll, or the archiver's fallback
  re-fetch — yields different extracted text, or no longer answers, ONE plain
  row lands in ``fetch_drift_observations``. Nothing reads it yet.

WHICH response a record came from: (1) the record names it — an
``ObservationRecord.fetch_sha256`` a paging handler set; (2) the handler names
the URL — ``receipt_url_field`` is a ``raw_provenance`` key holding the URL of
the file the record was read from (gdelt_files: ``export_file_url``); (3)
otherwise the newest complete 2xx response captured BEFORE the record was
yielded. (3) is right for every fetch-then-yield handler; a handler that
fetches several responses before yielding any must use (1) or (2). (2) follows
the redirect hops the scope saw (B-1: GDELT names ``http://`` and is answered
from ``https://``). (0, B-1) a handler's ``receipt_address(record)`` names the
capture by its receipt address (a Telegram message).

WHERE in the version the record's item sits (B-1): a handler's
``receipt_locator(record)``, else the record's own feed identity (payload
``external_id`` / ``link``) — written on the link (``locator``) so a reader
can return the item as received (``legba.data.byte_path``).

A SUBSCRIBED source (I-4) is captured by its stream ingest: each received
event is one capture (its bytes as they arrived) whose ``address`` is
``<canonical stream url>#event=<id>`` (``runtime/stream_ingest.py``), written
and linked by the same recorder.

EVERYTHING HERE IS BEST-EFFORT: a receipt failure is logged and the poll goes
on — it must never cost a signal. Not captured (recorded so in the deliverable,
never faked): 304s and error statuses (no body to rest on — the poll outcome
row records them). A non-HTTP transport records what it received itself
(``fetch_capture.capture_received``; Telegram since B-1); push sources keep
their envelope (below); the kinds still without a receipt are pinned in
``legba.data.sources.receipt_coverage``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..data._url_canon import canonical_url
from ..data.archive import archive_root
from ..data.sources.fetch_capture import (
    CaptureLog,
    FetchCapture,
    fetch_receipts_enabled,
)

logger = logging.getLogger(__name__)

__all__ = [
    "InboundEnvelope",
    "RecordLink",
    "current_inbound",
    "record_locator",
    "inbound_scope",
    "link_inbound_record",
    "persist_inbound",
    "redact_headers",
    "ReceiptRecorder",
    "VersionWriter",
    "extract_page_text",
    "read_version_body",
]

_TEXT_ROLES = frozenset({"page", "archiver"})
_MAX_TEXT_CHARS = 200_000

_UPSERT_VERSION_SQL = """
INSERT INTO fetch_versions (
    address, url, sha256, hash_basis, role, first_source_id, last_source_id,
    status, content_type, content_encoding, size_bytes, object_ref,
    store_status, stored_size, text_sha256, text_chars, extracted_text,
    first_seen_at, last_seen_at
) VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $17)
ON CONFLICT (address, sha256) DO UPDATE SET
    last_seen_at   = GREATEST(fetch_versions.last_seen_at, EXCLUDED.last_seen_at),
    fetch_count    = fetch_versions.fetch_count + 1,
    last_source_id = COALESCE(EXCLUDED.last_source_id, fetch_versions.last_source_id),
    object_ref     = COALESCE(fetch_versions.object_ref, EXCLUDED.object_ref),
    bytes_pruned_at = CASE WHEN EXCLUDED.store_status = 'stored'
                           THEN NULL ELSE fetch_versions.bytes_pruned_at END
RETURNING id, (xmax = 0) AS inserted, first_seen_at
"""

_PREVIOUS_PAGE_VERSION_SQL = """
SELECT id, sha256, text_sha256, size_bytes, last_seen_at
  FROM fetch_versions
 WHERE address = $1 AND id <> $2 AND role IN ('page', 'archiver')
 ORDER BY last_seen_at DESC, id DESC
 LIMIT 1
"""

_INSERT_DRIFT_SQL = """
INSERT INTO fetch_drift_observations (
    address, source_id, observed_by, earlier_version_id, later_version_id,
    earlier_fetched_at, later_fetched_at, earlier_sha256, later_sha256,
    earlier_text_sha256, later_text_sha256, earlier_size, later_size,
    later_status, later_gone
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
"""

_LINK_SQL = """
INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (record_kind, record_id, role) DO NOTHING
"""

#: B-1 (migration 0266): the same link with WHERE in the version the item sits.
_LINK_LOCATED_SQL = """
INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256, locator)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
ON CONFLICT (record_kind, record_id, role) DO NOTHING
"""

#: Links per INSERT statement in :meth:`ReceiptRecorder.link_many` (N-R).
LINK_BATCH_ROWS = 2000

_LINK_BATCH_SQL = """
INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256, locator)
SELECT r, i, ro, v, s, l::jsonb
  FROM unnest($1::text[], $2::uuid[], $3::text[], $4::bigint[], $5::text[], $6::text[])
       AS u(r, i, ro, v, s, l)
ON CONFLICT (record_kind, record_id, role) DO NOTHING
"""


def extract_page_text(body: bytes, content_type: str | None) -> str | None:
    """The main text the platform already extracts from a page (the evidence
    archiver's Trafilatura pass, V-E1 wall rejection included). ``None`` when
    nothing article-like is there. Derivation is deterministic for a given
    Trafilatura version, so the stored text is also re-derivable from the
    stored bytes."""
    from ..data.sources.page_text import (  # noqa: PLC0415 — the one extraction (I-2)
        _extract_text, _is_textual, _match_wall_pattern,
    )

    if not _is_textual(content_type, body):
        return None
    charset = None
    if content_type and "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";")[0].strip() or None
    text = _extract_text(body, charset, max_chars=_MAX_TEXT_CHARS)
    if text is None or _match_wall_pattern(text) is not None:
        return None
    return text


def _sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


_VERSION_BYTES_SQL = """
SELECT sha256, object_ref, bytes_pruned_at FROM fetch_versions WHERE id = $1
"""


async def read_version_body(conn: Any, version_id: int, *, root: Path | None = None) -> bytes | None:
    """The ORIGINAL bytes a fetch version kept — from the hot archive or, once
    the version has aged into the cold tier, from its bundle (re-verified
    against the version's sha256 either way). ``None`` when the version kept
    no bytes or they were pruned (its row and tombstone still say what they
    were). A cold copy that cannot be read raises
    :class:`~legba.data.archive_store.ColdTierError`."""
    from ..data.archive_store import EvidenceStore  # noqa: PLC0415
    from ..data.archive_tier import read_original  # noqa: PLC0415

    row = await conn.fetchrow(_VERSION_BYTES_SQL, version_id)
    if row is None or row["object_ref"] is None or row["bytes_pruned_at"] is not None:
        return None
    store = EvidenceStore.from_env(root if root is not None else archive_root())
    return await read_original(conn, store, str(row["sha256"]))


class VersionWriter:
    """Store bytes + upsert a version + note drift. Shared by the actor's
    receipts and the archiver's fallback re-fetch."""

    def __init__(self, *, root: Path | None = None) -> None:
        self._root = root

    def _store(self) -> Any | None:
        from ..data.archive_store import EvidenceStore  # noqa: PLC0415

        root = self._root if self._root is not None else archive_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return EvidenceStore(root)

    async def write(
        self, conn: Any, cap: FetchCapture, *, source_id: str | None,
        observed_by: str = "ingest", store: bool = True,
    ) -> tuple[int, bool] | None:
        """``(version_id, inserted)`` for a complete 2xx capture, else None.
        ``store=False`` records the version without writing bytes (the
        archiver stores its own copy through its own path)."""
        if not cap.complete or cap.sha256 is None or not (200 <= cap.status < 300):
            return None
        object_ref = None
        stored_size = None
        status = "not_stored"
        if cap.body is None:  # above MAX_CAPTURE_BYTES: only the wire hash was kept
            status = "too_large"
        elif not store:
            status = "not_stored"
        elif cap.body:
            evidence = self._store()
            if evidence is None:
                status = "store_unavailable"
            else:
                try:
                    res = await asyncio.to_thread(evidence.put, cap.body)
                    object_ref = res.object_ref
                    stored_size = res.stored_size
                    status = "existed" if res.existed else "stored"
                except Exception as exc:  # disk full, permissions: keep the receipt
                    logger.warning("fetch_receipts.store_failed url=%s err=%s", cap.url, exc)
                    status = "store_unavailable"
        text = None
        if cap.role in _TEXT_ROLES and cap.body:
            try:
                text = await asyncio.to_thread(extract_page_text, cap.body, cap.content_type)
            except Exception:  # extraction is a bonus, never a failure
                text = None
        address = cap.address or canonical_url(cap.url) or cap.url
        row = await conn.fetchrow(
            _UPSERT_VERSION_SQL, address, cap.url, cap.sha256, cap.hash_basis,
            cap.role, source_id, cap.status, cap.content_type, cap.content_encoding,
            cap.size, object_ref, status, stored_size, _sha(text),
            None if text is None else len(text), text, cap.fetched_at,
        )
        version_id, inserted = int(row["id"]), bool(row["inserted"])
        if inserted and cap.role in _TEXT_ROLES:
            await self._note_drift(
                conn, address=address, version_id=version_id, cap=cap,
                text_sha=_sha(text), source_id=source_id, observed_by=observed_by,
            )
        return version_id, inserted

    async def _note_drift(
        self, conn: Any, *, address: str, version_id: int, cap: FetchCapture,
        text_sha: str | None, source_id: str | None, observed_by: str,
    ) -> None:
        prev = await conn.fetchrow(_PREVIOUS_PAGE_VERSION_SQL, address, version_id)
        if prev is None or prev["text_sha256"] == text_sha:
            return  # first sighting, or a byte-only change the text ignores
        await conn.execute(
            _INSERT_DRIFT_SQL, address, source_id, observed_by, prev["id"], version_id,
            prev["last_seen_at"], cap.fetched_at, prev["sha256"], cap.sha256,
            prev["text_sha256"], text_sha, prev["size_bytes"], cap.size, cap.status, False,
        )

    async def note_gone(
        self, conn: Any, *, url: str, status: int | None, fetched_at: datetime,
        source_id: str | None, observed_by: str,
    ) -> bool:
        """A later fetch of a page address errored or redirected away: one
        drift row against the newest held version, no bytes. False when the
        address has no held version (nothing to have drifted from)."""
        address = canonical_url(url) or url
        prev = await conn.fetchrow(_PREVIOUS_PAGE_VERSION_SQL, address, -1)
        if prev is None:
            return False
        await conn.execute(
            _INSERT_DRIFT_SQL, address, source_id, observed_by, prev["id"], None,
            prev["last_seen_at"], fetched_at, prev["sha256"], None,
            prev["text_sha256"], None, prev["size_bytes"], None, status, True,
        )
        return True


@dataclass
class RecordLink:
    response: int | None
    pages: list[int] = field(default_factory=list)
    #: B-1: where the record's item sits inside the ``response`` version.
    locator: dict[str, Any] | None = None


def record_locator(record: Any, handler: Any) -> dict[str, Any] | None:
    """Where ``record``'s item sits inside the response it was parsed from:
    the handler's ``receipt_locator(record)``, else the record's feed
    identity (``payload.external_id`` / ``payload.link``, or a GeoJSON
    feature's ``raw_provenance.external_id``). ``None`` when nothing names it."""
    hook = getattr(handler, "receipt_locator", None)
    if callable(hook):
        try:
            loc = hook(record)
        except Exception:
            loc = None
        if loc is not None:
            return dict(loc)
    from ..data.byte_path import locator_from_signal  # noqa: PLC0415

    return locator_from_signal(getattr(record, "payload", None), getattr(record, "raw_provenance", None),
                               getattr(record, "canonical_url", None))


async def _exec_link(conn: Any, row: tuple[Any, ...]) -> None:
    """One link row ``(kind, id, role, version_id, sha256, locator_json)``.
    Without a locator — or on a database where migration 0266 has not added
    the column yet — the link is written exactly as before N-L's B-1 change."""
    if row[5] is None:
        await conn.execute(_LINK_SQL, *row[:5])
        return
    try:
        await conn.execute(_LINK_LOCATED_SQL, *row)
    except Exception as exc:
        if type(exc).__name__ != "UndefinedColumnError":
            raise
        await conn.execute(_LINK_SQL, *row[:5])


def _locator_json(loc: dict[str, Any] | None) -> str | None:
    import json  # noqa: PLC0415

    return None if loc is None else json.dumps(loc, sort_keys=True, default=str)


class ReceiptRecorder:
    """One poll's receipts: the capture log the actor scopes over the handler,
    and the writes that persist and link it."""

    def __init__(self, *, source_id: str, writer: VersionWriter | None = None,
                 enabled: bool | None = None) -> None:
        self.source_id = source_id
        self.enabled = fetch_receipts_enabled() if enabled is None else enabled
        self.log: CaptureLog | None = CaptureLog() if self.enabled else None
        self._writer = writer or VersionWriter()
        self._versions: dict[int, int | None] = {}
        self.failed = 0

    # -- linking -----------------------------------------------------------

    def link_for(self, record: Any, handler: Any) -> RecordLink:
        log = self.log
        if log is None or not log.captures:
            return RecordLink(None)
        pages = log.pages_since_last_yield()
        locator = record_locator(record, handler)
        explicit = getattr(record, "fetch_sha256", None)
        if explicit:
            for i in range(len(log.captures) - 1, -1, -1):
                if log.captures[i].sha256 == explicit:
                    return RecordLink(i, pages, locator)
            return RecordLink(None, pages)
        address_of = getattr(handler, "receipt_address", None)
        if callable(address_of):
            try:
                address = address_of(record)
            except Exception:
                address = None
            if address:
                for i in range(len(log.captures) - 1, -1, -1):
                    if log.captures[i].address == address and log.captures[i].complete:
                        return RecordLink(i, pages, locator)
                return RecordLink(None, pages)
        url_field = getattr(handler, "receipt_url_field", None)
        if url_field:
            url = (getattr(record, "raw_provenance", None) or {}).get(url_field)
            if isinstance(url, str) and url:
                return RecordLink(log.latest_response(url=url), pages, locator)
        return RecordLink(log.latest_response(), pages, locator)

    def sha_of(self, index: int | None) -> str | None:
        if index is None or self.log is None:
            return None
        return self.log.captures[index].sha256

    # -- persistence -------------------------------------------------------

    async def flush(self, conn: Any) -> None:
        """Persist every capture not yet persisted (idempotent per capture)."""
        if self.log is None:
            return
        for i, cap in enumerate(self.log.captures):
            if i in self._versions:
                continue
            self._versions[i] = None
            try:
                res = await self._writer.write(conn, cap, source_id=self.source_id)
            except Exception as exc:
                self.failed += 1
                logger.warning(
                    "fetch_receipts.persist_failed source=%s url=%s err=%s",
                    self.source_id, cap.url, exc,
                )
                continue
            self._versions[i] = None if res is None else res[0]

    async def link(self, conn: Any, record_kind: str, record_id: Any, link: RecordLink) -> None:
        if self.log is None or (link.response is None and not link.pages):
            return
        await self.flush(conn)
        pairs = [("response", link.response)] + [("page", p) for p in link.pages[:1]]
        for role, idx in pairs:
            vid = self._versions.get(idx) if idx is not None else None
            if vid is None:
                continue
            loc = link.locator if role == "response" else None
            try:
                await _exec_link(conn, (record_kind, record_id, role, vid,
                                        self.log.captures[idx].sha256, _locator_json(loc)))
            except Exception as exc:
                self.failed += 1
                logger.warning(
                    "fetch_receipts.link_failed source=%s record=%s err=%s",
                    self.source_id, record_id, exc,
                )

    async def link_many(self, conn: Any, record_kind: str,
                        items: list[tuple[Any, RecordLink]]) -> None:
        """:meth:`link` for many records, ONE statement per
        :data:`LINK_BATCH_ROWS` links (lane N-R: 8,036 per-row link inserts
        held the PortWatch actor ~1 min 50 s). Same rows, same ``ON CONFLICT
        DO NOTHING``; a batch the database refuses is retried row by row so
        one bad link costs one link, as before."""
        if self.log is None:
            return
        await self.flush(conn)
        rows: list[tuple[str, Any, str, int, str, str | None]] = []
        for record_id, link in items:
            pairs = [("response", link.response)] + [("page", p) for p in link.pages[:1]]
            for role, idx in pairs:
                vid = self._versions.get(idx) if idx is not None else None
                if vid is not None:
                    rows.append((record_kind, record_id, role, vid, self.log.captures[idx].sha256,
                                 _locator_json(link.locator) if role == "response" else None))
        for start in range(0, len(rows), LINK_BATCH_ROWS):
            chunk = rows[start:start + LINK_BATCH_ROWS]
            try:
                await conn.execute(_LINK_BATCH_SQL, *[list(col) for col in zip(*chunk)])
            except Exception as exc:
                logger.warning("fetch_receipts.link_batch_failed source=%s rows=%d err=%s",
                               self.source_id, len(chunk), exc)
                for row in chunk:
                    try:
                        await _exec_link(conn, row)
                    except Exception as row_exc:
                        self.failed += 1
                        logger.warning("fetch_receipts.link_failed source=%s record=%s err=%s",
                                       self.source_id, row[1], row_exc)


# ---------------------------------------------------------------------------
# PUSH: the inbound envelope receipt
# ---------------------------------------------------------------------------
#
# A pushed item is not something we fetched: there is no address to version
# and nothing to drift. Its receipt is the envelope exactly as the inbound
# drain received it — the body bytes (stored, content-addressed) and the
# request headers the handler verifies against — one row per (source, body
# sha256) with receipt_kind 'inbound' (migration 0245). A redelivery of the
# same envelope bumps fetch_count and stores nothing. Header values that can
# carry a secret (token, signature, authorization, key, cookie) are kept as
# ``sha256:<hex>`` so the receipt proves WHICH credential was presented
# without the archive ever holding it.

_SECRET_HEADER_HINTS = ("token", "signature", "authorization", "secret", "key", "cookie")

_UPSERT_INBOUND_SQL = """
INSERT INTO fetch_versions (
    receipt_kind, address, url, sha256, hash_basis, role, first_source_id,
    last_source_id, status, content_type, content_encoding, size_bytes,
    object_ref, store_status, stored_size, headers, first_seen_at, last_seen_at
) VALUES ('inbound', NULL, $1, $2, 'entity', 'inbound', $3, $3, 202, $4, NULL, $5,
          $6, $7, $8, $9::jsonb, $10, $10)
ON CONFLICT (first_source_id, sha256) WHERE receipt_kind = 'inbound' DO UPDATE SET
    last_seen_at = GREATEST(fetch_versions.last_seen_at, EXCLUDED.last_seen_at),
    fetch_count  = fetch_versions.fetch_count + 1,
    object_ref   = COALESCE(fetch_versions.object_ref, EXCLUDED.object_ref)
RETURNING id
"""


@dataclass
class InboundEnvelope:
    source_id: str
    body: bytes
    headers: dict[str, str]
    received_at: datetime
    version_id: int | None = None
    persisted: bool = False


_INBOUND: "contextvars.ContextVar[InboundEnvelope | None]" = contextvars.ContextVar(
    "legba_inbound_envelope", default=None,
)


@contextlib.contextmanager
def inbound_scope(env: InboundEnvelope | None) -> Iterator[None]:
    """Make ``env`` the envelope the push emit path links its records to."""
    token = _INBOUND.set(env if fetch_receipts_enabled() else None)
    try:
        yield
    finally:
        _INBOUND.reset(token)


def current_inbound() -> InboundEnvelope | None:
    return _INBOUND.get()


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in sorted((headers or {}).items()):
        name = str(k).lower()
        if any(h in name for h in _SECRET_HEADER_HINTS):
            out[name] = "sha256:" + hashlib.sha256(str(v).encode("utf-8")).hexdigest()
        else:
            out[name] = str(v)
    return out


async def persist_inbound(conn: Any, env: InboundEnvelope, *, writer: VersionWriter | None = None) -> int | None:
    """The envelope's receipt row (idempotent per source + body hash)."""
    if env.persisted:
        return env.version_id
    env.persisted = True
    import json  # noqa: PLC0415

    sha = hashlib.sha256(env.body).hexdigest()
    object_ref, stored_size, status = None, None, "not_stored"
    evidence = (writer or VersionWriter())._store()
    if evidence is None:
        status = "store_unavailable"
    elif env.body:
        try:
            res = await asyncio.to_thread(evidence.put, env.body)
            object_ref, stored_size = res.object_ref, res.stored_size
            status = "existed" if res.existed else "stored"
        except Exception as exc:
            logger.warning("fetch_receipts.inbound_store_failed source=%s err=%s", env.source_id, exc)
            status = "store_unavailable"
    env.version_id = await conn.fetchval(
        _UPSERT_INBOUND_SQL, f"/api/v1/webhooks/{env.source_id}", sha, env.source_id,
        env.headers.get("content-type"), len(env.body), object_ref, status, stored_size,
        json.dumps(redact_headers(env.headers)), env.received_at,
    )
    return env.version_id


async def link_inbound_record(conn: Any, env: InboundEnvelope, record_kind: str, record_id: Any) -> None:
    vid = await persist_inbound(conn, env)
    if vid is None:
        return
    await conn.execute(_LINK_SQL, record_kind, record_id, "inbound", vid,
                       hashlib.sha256(env.body).hexdigest())
