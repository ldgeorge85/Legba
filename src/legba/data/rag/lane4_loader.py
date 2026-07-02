# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.rag.lane4_loader — the manual-ingest Lane-4 vector loader (S5-T2).

Lane 4 of the manual-ingest format (see :mod:`legba.data.seed.manual_schema`):
corpus documents destined for Qdrant. This loader takes a validated batch's
``docs`` lane, resolves + chunks each document (heading-aware, ~400-800 tokens),
embeds every chunk through the hosted embedder, and upserts the vectors into the
``world_context`` / ``tradecraft`` collections.

It rides the SAME machinery as the structured seed/manual lanes:

  * **Ledger + idempotency** — a ``seed_batches`` row per import, deduped on the
    natural key ``(source, kind, manifest.content_hash)`` exactly like
    :mod:`legba.data.seed._driver`. In ``skip`` mode a re-run of an identical
    batch short-circuits to a NO-OP (no re-embed, no re-upsert). Point ids are a
    deterministic ``uuid5`` of the chunk natural key, so even outside the ledger
    a re-upsert overwrites in place rather than duplicating.
  * **Natural key** — ``(corpus, doc_id, chunk_seq, chunk_part)``. The design's
    ``(corpus, doc_id, chunk_seq)`` is the operator's logical segment; the
    loader appends ``chunk_part`` (0-based) for the sub-chunks a single segment
    splits into when it exceeds the token band. Pre-chunked corpora carry
    ``chunk_part == 0`` throughout, i.e. exactly the design key.

**DELETE-EXCEPTION (documented, load-bearing):** ``--mode=force`` for the vector
lane is *delete-and-reload the doc's chunks*. The platform's no-hard-delete rule
(temporal supersession, never DELETE) is RELAXED for Lane 4 ONLY, because vector
rows are DERIVED, re-embeddable artifacts — a chunk can always be rebuilt from
its source document. Structured facts/nexuses (Lanes 1-3) keep supersession; a
force reload here deletes the ``(corpus, doc_id)`` points and re-embeds. This is
the single place in the codebase where a hard delete of substrate is correct.

Nothing here re-implements Qdrant point I/O — that lives on
:class:`legba.data.qdrant.QdrantStore` (``upsert_points`` / ``delete_doc_points``
/ ``count_doc_points`` / ``search``), so qmodels handling stays in one module.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid5

from ..seed.manual_schema import BatchMode, ManualDocRecord, ValidatedBatch
from .chunker import chunk_text

logger = logging.getLogger(__name__)

# Fixed namespace so a chunk's point id is a STABLE function of its natural key
# across processes/hosts (uuid5 is deterministic). Never regenerate this — it is
# the identity of every Lane-4 point ever written.
_LANE4_NS = UUID("6f2b1e4a-7c3d-5a8e-9b0f-1d2c3e4f5a6b")

#: corpus name → the QdrantStore ensure-method + config attr for its collection.
#: The two provisioned RAG corpora (RAG plan §B). A batch whose ``docs`` declare
#: any OTHER corpus is refused (the loader never silently mints an arbitrary
#: collection — the vector plane is exactly these curated corpora).
CORPUS_COLLECTIONS: dict[str, tuple[str, str]] = {
    "world_context": ("ensure_world_context_collection", "world_context_collection"),
    "tradecraft": ("ensure_tradecraft_collection", "tradecraft_collection"),
}

# seed_batches classification for a vector-lane import.
_SOURCE_PREFIX = "manual_vector"
_KIND = "docs"


# ---------------------------------------------------------------------------
# Injected collaborators (duck-typed so tests need no DB / no real embedder)
# ---------------------------------------------------------------------------


class _Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class SeedBatchLedger(Protocol):
    """The ``seed_batches`` ledger surface the loader needs.

    Factored behind a protocol so the loader carries no SQL and tests inject an
    in-memory ledger. :func:`pg_seed_batch_ledger` builds the asyncpg-backed
    implementation for the CLI.
    """

    async def find_batch(
        self, *, source: str, kind: str, content_hash: str
    ) -> UUID | None: ...

    async def upsert_batch(
        self, *, source: str, kind: str, source_type: str, manifest: dict[str, Any]
    ) -> UUID: ...

    async def set_counts(self, batch_id: UUID, counts: dict[str, int]) -> None: ...


class _PgSeedBatchLedger:
    """asyncpg-backed ``seed_batches`` ledger (mirrors ``_driver`` dedupe)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def find_batch(
        self, *, source: str, kind: str, content_hash: str
    ) -> UUID | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT id FROM seed_batches
                 WHERE source = $1 AND kind = $2
                   AND manifest->>'content_hash' = $3
                 ORDER BY imported_at DESC
                 LIMIT 1
                """,
                source,
                kind,
                content_hash,
            )

    async def upsert_batch(
        self, *, source: str, kind: str, source_type: str, manifest: dict[str, Any]
    ) -> UUID:
        async with self._pool.acquire() as conn:
            existing = await conn.fetchval(
                """
                SELECT id FROM seed_batches
                 WHERE source = $1 AND kind = $2
                   AND manifest->>'content_hash' = $3
                 ORDER BY imported_at DESC
                 LIMIT 1
                """,
                source,
                kind,
                manifest.get("content_hash"),
            )
            if existing is not None:
                return await conn.fetchval(
                    """
                    UPDATE seed_batches
                       SET source_type = $2, manifest = $3::jsonb, imported_at = now()
                     WHERE id = $1
                    RETURNING id
                    """,
                    existing,
                    source_type,
                    json.dumps(manifest),
                )
            return await conn.fetchval(
                """
                INSERT INTO seed_batches (source, kind, source_type, manifest)
                VALUES ($1, $2, $3, $4::jsonb)
                RETURNING id
                """,
                source,
                kind,
                source_type,
                json.dumps(manifest),
            )

    async def set_counts(self, batch_id: UUID, counts: dict[str, int]) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE seed_batches SET counts = $2::jsonb WHERE id = $1",
                batch_id,
                json.dumps(counts),
            )


def pg_seed_batch_ledger(pool: Any) -> SeedBatchLedger:
    """Build the asyncpg-backed ledger the CLI wires from a live pool."""
    return _PgSeedBatchLedger(pool)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class VectorLoadResult:
    """Outcome of :func:`load_vector_batch`."""

    mode: str
    dry_run: bool
    seed_batch_id: UUID | None = None
    collections: dict[str, str] = field(default_factory=dict)  # corpus → collection
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "docs": 0,
            "chunks": 0,
            "deleted_points": 0,
            "deleted_docs": 0,
            "skipped_docs": 0,
        }
    )
    no_op: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "dry_run": self.dry_run,
            "seed_batch_id": str(self.seed_batch_id) if self.seed_batch_id else None,
            "collections": dict(self.collections),
            "counts": dict(self.counts),
            "no_op": self.no_op,
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _docs_content_hash(docs: list[ManualDocRecord], manifest: Any) -> str:
    """Stable fingerprint of the docs lane + the batch's identity/mode.

    Excludes volatile fields (nothing here is time-varying) so a re-run over an
    identical batch hashes the same and the ledger dedupes it. Uses pydantic's
    JSON dump (datetimes serialize stably) over a sorted projection.
    """
    items = sorted(json.dumps(d.model_dump(mode="json"), sort_keys=True) for d in docs)
    prov = getattr(getattr(manifest, "default_provenance", None), "value", "")
    digest = hashlib.sha256()
    digest.update(str(getattr(manifest, "batch_id", "")).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(prov).encode("utf-8"))
    digest.update(b"\x00")
    for item in items:
        digest.update(item.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _point_id(collection: str, corpus: str, doc_id: str, chunk_seq: int, chunk_part: int) -> str:
    key = f"{collection}\x00{corpus}\x00{doc_id}\x00{chunk_seq}\x00{chunk_part}"
    return str(uuid5(_LANE4_NS, key))


def _resolve_text(rec: ManualDocRecord, batch_dir: Path) -> str:
    """Return the record's chunk text — inline ``text`` or read ``text_ref``.

    ``text_ref`` is a path relative to the batch directory (e.g.
    ``docs/handbook.md``). Raises ``ValueError`` when neither is present or the
    referenced file is missing (the loader records it as a per-doc error and
    skips, never fabricates empty content).
    """
    if rec.text is not None and rec.text.strip():
        return rec.text
    if rec.text_ref:
        path = (batch_dir / rec.text_ref).resolve()
        # Contain the read to the batch directory (no path traversal out).
        base = batch_dir.resolve()
        if base not in path.parents and path != base:
            raise ValueError(f"text_ref {rec.text_ref!r} escapes the batch directory")
        if not path.is_file():
            raise ValueError(f"text_ref {rec.text_ref!r} not found")
        return path.read_text(encoding="utf-8")
    raise ValueError("doc record has neither inline text nor text_ref")


def _build_payload(
    rec: ManualDocRecord,
    manifest: Any,
    *,
    section: str,
    chunk_part: int,
    chunk_text_body: str,
    seed_batch_id: UUID | None,
) -> dict[str, Any]:
    """Assemble the RAG-plan chunk metadata payload.

    License / source_url fall back to the manifest's batch-level defaults when
    the record omits them (BatchManifest's inherited provenance defaults).
    """
    eff = rec.effective_date.isoformat() if rec.effective_date else None
    return {
        "corpus": rec.corpus,
        "doc_id": rec.doc_id,
        "chunk_seq": rec.chunk_seq,
        "chunk_part": chunk_part,
        "title": rec.title,
        "section": section or rec.section,
        "countries": list(rec.countries),
        "topics": list(rec.topics),
        "lang": rec.lang,
        "license": rec.license or getattr(manifest, "license", None),
        "source_url": rec.source_url or getattr(manifest, "source_url", None),
        "effective_date": eff,
        "batch_id": getattr(manifest, "batch_id", None),
        "provenance": str(getattr(getattr(manifest, "default_provenance", ""), "value", "")),
        "seed_batch_id": str(seed_batch_id) if seed_batch_id else None,
        "text": chunk_text_body,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def load_vector_batch(
    *,
    batch: ValidatedBatch,
    batch_dir: str | Path,
    store: Any,
    embedder: _Embedder,
    ledger: SeedBatchLedger | None = None,
    mode: BatchMode | None = None,
    dry_run: bool = False,
    corpus_collections: Mapping[str, tuple[str, str]] | None = None,
    max_tokens: int = 800,
    target_tokens: int = 512,
    overlap_tokens: int = 64,
    upsert_batch_size: int = 128,
) -> VectorLoadResult:
    """Load a validated batch's ``docs`` lane into the RAG vector collections.

    ``store`` is a connected :class:`~legba.data.qdrant.QdrantStore`; ``embedder``
    is anything with ``async embed(text) -> list[float]`` (the hosted client, or
    a fake in tests). ``ledger`` (optional) rides ``seed_batches`` for the
    idempotency/no-op check; without it, deterministic point ids still make a
    re-upsert overwrite in place. ``mode`` overrides the manifest's mode.

    Modes: ``skip`` (default) inserts-if-absent — an identical re-run is a no-op;
    ``force`` deletes each ``(corpus, doc_id)``'s existing chunks then re-embeds
    (the documented DELETE-EXCEPTION). ``merge`` is treated as ``skip`` for the
    vector lane (chunks have no partial fields to merge — a changed doc is a
    force reload).
    """
    manifest = batch.manifest
    resolved_mode = mode or manifest.mode
    batch_dir = Path(batch_dir)
    corpus_map = dict(corpus_collections or CORPUS_COLLECTIONS)

    result = VectorLoadResult(mode=resolved_mode.value, dry_run=dry_run)
    docs = list(batch.docs)
    result.counts["docs"] = len(docs)
    if not docs:
        return result

    # Refuse unknown corpora up front (never silently mint a collection).
    corpora = []
    for corpus in dict.fromkeys(d.corpus for d in docs):  # order-preserving unique
        if corpus not in corpus_map:
            result.errors.append(
                f"unknown corpus {corpus!r} (known: {', '.join(sorted(corpus_map))})"
            )
        else:
            corpora.append(corpus)
    if result.errors:
        return result

    source = f"{_SOURCE_PREFIX}:{manifest.batch_id}"
    content_hash = _docs_content_hash(docs, manifest)
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    manifest_json: dict[str, Any] = {
        "lane": "vector",
        "batch_id": manifest.batch_id,
        "operator": manifest.operator,
        "provenance": manifest.default_provenance.value,
        "mode": resolved_mode.value,
        "content_hash": content_hash,
        "imported_at": now_iso,
        "docs": len(docs),
        "dry_run": dry_run,
    }

    # Ledger idempotency: a skip-mode re-run of an identical batch is a NO-OP.
    if ledger is not None and not dry_run:
        existing = await ledger.find_batch(
            source=source, kind=_KIND, content_hash=content_hash
        )
        # skip AND merge no-op on an identical re-run (neither hard-deletes);
        # only force always re-loads.
        if existing is not None and resolved_mode in (BatchMode.SKIP, BatchMode.MERGE):
            result.no_op = True
            result.seed_batch_id = existing
            result.counts["skipped_docs"] = len(docs)
            logger.info(
                "lane4.skip.noop source=%s content_hash=%s docs=%d",
                source, content_hash[:12], len(docs),
            )
            return result

    # Resolve + ensure each corpus's collection.
    collection_for: dict[str, str] = {}
    for corpus in corpora:
        ensure_name, cfg_attr = corpus_map[corpus]
        if not dry_run:
            await getattr(store, ensure_name)()
        collection_for[corpus] = getattr(store.cfg, cfg_attr)
        result.collections[corpus] = collection_for[corpus]

    # Establish the ledger row (so the payload can carry seed_batch_id).
    seed_batch_id: UUID | None = None
    if ledger is not None and not dry_run:
        seed_batch_id = await ledger.upsert_batch(
            source=source,
            kind=_KIND,
            source_type=manifest.default_provenance.value,
            manifest=manifest_json,
        )
        result.seed_batch_id = seed_batch_id

    # FORCE: delete-and-reload — drop every existing chunk of each doc first.
    if resolved_mode == BatchMode.FORCE and not dry_run:
        for corpus, doc_id in dict.fromkeys((d.corpus, d.doc_id) for d in docs):
            deleted = await store.delete_doc_points(
                collection_for[corpus], corpus=corpus, doc_id=doc_id
            )
            result.counts["deleted_points"] += int(deleted)
            result.counts["deleted_docs"] += 1

    # Chunk → embed → upsert, buffering per collection.
    pending: dict[str, list[tuple[str, list[float], dict[str, Any]]]] = {}

    async def _flush(collection: str, *, force: bool = False) -> None:
        buf = pending.get(collection)
        if not buf:
            return
        if force or len(buf) >= upsert_batch_size:
            written = await store.upsert_points(collection, buf)
            result.counts["chunks"] += int(written)
            pending[collection] = []

    for rec in docs:
        collection = collection_for[rec.corpus]
        try:
            text = _resolve_text(rec, batch_dir)
        except ValueError as exc:
            result.counts["skipped_docs"] += 1
            msg = f"doc ({rec.corpus}/{rec.doc_id}#{rec.chunk_seq}): {exc}"
            logger.warning("lane4.doc.skipped %s", msg)
            result.errors.append(msg)
            continue

        chunks = chunk_text(
            text,
            max_tokens=max_tokens,
            target_tokens=target_tokens,
            overlap_tokens=overlap_tokens,
            base_section=rec.section,
        )
        for chunk in chunks:
            payload = _build_payload(
                rec,
                manifest,
                section=chunk.section,
                chunk_part=chunk.seq,
                chunk_text_body=chunk.text,
                seed_batch_id=seed_batch_id,
            )
            if dry_run:
                result.counts["chunks"] += 1
                continue
            vec = await embedder.embed(chunk.text)
            pid = _point_id(collection, rec.corpus, rec.doc_id, rec.chunk_seq, chunk.seq)
            pending.setdefault(collection, []).append((pid, vec, payload))
            await _flush(collection)

    if not dry_run:
        for collection in list(pending):
            await _flush(collection, force=True)
        if ledger is not None and seed_batch_id is not None:
            await ledger.set_counts(seed_batch_id, dict(result.counts))

    return result


__all__ = [
    "CORPUS_COLLECTIONS",
    "SeedBatchLedger",
    "VectorLoadResult",
    "load_vector_batch",
    "pg_seed_batch_ledger",
]
