# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.opensearch — wrapper around `opensearch-py` (AsyncOpenSearch).

The OpenSearch corpus is the INDEX PLANE of the signal-content-depth program: a
single-node, internal-only, full-text index of the shared signal pool (a MINING
substrate — BM25 keyword search over the raw bodies + lightweight keyword/date
facets). Signals already live structured in Postgres (`signals`) and, for the
RAG corpus, as vectors in Qdrant (`legba.data.qdrant`); this store is the third
leg — cheap lexical retrieval over the WHOLE corpus body, not just the analytic
slice.

Shape mirrors :mod:`legba.data.qdrant` deliberately: a soft-import guard (so the
module imports cleanly on a host without ``opensearch-py`` installed — the
descriptor-model code + the deterministic test suite import this transitively),
an ``__init__(cfg)`` / ``from_env()`` classmethod, an idempotent ``ensure_index``
create-if-absent, and I/O passthroughs (``bulk_index`` / ``search`` / ``get``)
that keep all opensearch-py wire handling in ONE place. No module-level
singleton — callers build via :meth:`OpenSearchStore.from_env`.

This is the INDEX plane only. The READ tools (``search_corpus`` /
``read_document`` on the ``substrate_read`` pack) are a SEPARATE build.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

try:
    from opensearchpy import AsyncOpenSearch
    from opensearchpy.helpers import async_bulk
except ImportError:  # pragma: no cover — opensearch-py must be installed in-image
    AsyncOpenSearch = None  # type: ignore[assignment]
    async_bulk = None  # type: ignore[assignment]

from .config import OpenSearchConfig
from .retrieval_origin import resolve_retrieval_origin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Index mapping (the schema ensure_index materializes) + the signal projection
# ---------------------------------------------------------------------------
#
# Single-node → number_of_replicas 0 (no replica shard can allocate anyway).
# Text fields use the `english` analyzer for BM25 stemming/stopwords; the facet
# fields are keyword (exact-match term filters). `published_at` is a payload
# STRING (not a clean column), so it is mapped lenient (ignore_malformed) — a
# non-ISO value is dropped from the index rather than failing the whole doc.

CORPUS_INDEX_MAPPING: dict[str, Any] = {
    "settings": {
        "index": {
            "number_of_replicas": 0,
        },
    },
    "mappings": {
        "properties": {
            # BM25 text (english analyzer)
            "title": {"type": "text", "analyzer": "english"},
            "distilled_body": {"type": "text", "analyzer": "english"},
            "raw_body": {"type": "text", "analyzer": "english"},
            # R6: the chat-platform full message body (payload.text —
            # telegram/discord; see _to_signal in those source modules). These
            # modalities never populate raw_body, so without this field their
            # content was invisible to the corpus (~96.8% of telegram signals).
            "text": {"type": "text", "analyzer": "english"},
            # P2-1 / S-10: the evidence_archiver's Trafilatura-extracted FULL
            # article text (payload.archived_text) — upgrades the thin teaser
            # doc for every archived cited signal via the corpus_indexer
            # dirty-marker re-index.
            "archived_text": {"type": "text", "analyzer": "english"},
            "summary": {"type": "text", "analyzer": "english"},
            "best_body": {"type": "text", "analyzer": "english"},
            "entities_text": {"type": "text", "analyzer": "english"},
            # keyword facets (exact-match term filters)
            "source_id": {"type": "keyword"},
            "geo": {"type": "keyword"},
            "tags": {"type": "keyword"},
            "entity_classes": {"type": "keyword"},
            "language": {"type": "keyword"},
            "modality": {"type": "keyword"},
            "retention_class": {"type": "keyword"},
            "canonical_url": {"type": "keyword"},
            "license_class": {"type": "keyword"},
            # R-3b / migration 0112 — WHERE this evidence was retrieved from,
            # orthogonal to license_class (what we may keep) and source_class
            # (editorial authority). Absent = a curated registered source; the
            # facet only ever carries the non-default values.
            "retrieval_origin": {"type": "keyword"},
            # numeric
            "source_credibility": {"type": "float"},
            # dates — fetched_at is a clean column; published_at is a payload
            # string, so map it lenient.
            "fetched_at": {"type": "date"},
            "published_at": {"type": "date", "ignore_malformed": True},
        },
    },
}

#: BM25 multi_match target fields (title + best_body carry the analytic weight;
#: raw_body/summary/distilled_body/entities_text broaden recall).
_SEARCH_FIELDS: tuple[str, ...] = (
    "title^2",
    "best_body^1.5",
    "distilled_body",
    "text",
    "archived_text",
    "summary",
    "raw_body",
    "entities_text",
)

#: best_body preference order (first non-empty wins) — OUR distilled brief
#: first, then the chat-platform full message body (R6: telegram/discord
#: populate ONLY payload.text, never raw_body — this must outrank
#: archived_text because a t.me/telegram-widget "archive" of a chat message
#: is embed-widget UI CHROME, not an article (see evidence_archiver's
#: t.me skip-extraction guard) — real prose from the platform itself is
#: always preferable to whatever an article-extractor made of a page with no
#: article on it), then the archived FULL article text (P2-1 evidence
#: archival — a genuine upgrade for actual article pages, when present), then
#: the ingest body, then the teaser, then rarer manual/derived text fields.
_BEST_BODY_FIELDS: tuple[str, ...] = (
    "distilled_body",
    "text",
    "archived_text",
    "raw_body",
    "summary",
    "description",
    "content_text",
)


def _as_dict(v: Any) -> dict[str, Any]:
    """Coerce a jsonb value to a dict (asyncpg may hand back a str or a dict)."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        import json

        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _as_str_list(v: Any) -> list[str]:
    """text[]/list → a clean list of non-empty strings (drops empties)."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple, set)):
        out: list[str] = []
        for x in v:
            if x is None:
                continue
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    return []


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso(v: Any) -> str | None:
    """datetime → ISO 8601 string; pass a str through; else None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, str):
        return v.strip() or None
    return None


def _first_nonempty(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    for k in keys:
        val = payload.get(k)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _as_text_field(v: Any) -> str | None:
    """Coerce a payload value destined for a ``text``-mapped corpus field.

    R13: some signals' body-shaped payload keys hold a non-string value — a
    dict/list of STRUCTURED data (e.g. a GDELT CAMEO event dump landing in
    ``payload.raw_body``), not prose. Handing that straight to OpenSearch
    against a ``{"type": "text"}`` mapping is a ``mapper_parsing_exception``
    (the bulk indexer rejects the whole doc). Structured records are not
    searchable prose anyway, so the correct move is to OMIT the field for
    this doc (never stringify — a raw ``repr()``/JSON dump of a CAMEO event
    would just pollute BM25 with punctuation/field-name noise), rather than
    coerce it into something that parses. ``None`` propagates to the
    existing drop-empties pass in :func:`signal_to_doc`.
    """
    if isinstance(v, str):
        return v.strip() or None
    return None


def _entities_text(payload: Mapping[str, Any]) -> str:
    """Join the NER MENTION texts (payload.entities[].text — under ``text``, NOT
    ``name``; class is under ``class``). Order-preserving de-dup so a repeated
    mention does not bloat the field."""
    ents = payload.get("entities")
    if not isinstance(ents, list):
        return ""
    seen: set[str] = set()
    out: list[str] = []
    for e in ents:
        if not isinstance(e, Mapping):
            continue
        t = e.get("text")
        if isinstance(t, str) and t.strip():
            key = t.strip()
            if key not in seen:
                seen.add(key)
                out.append(key)
    return " ".join(out)


def _license_class(row: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    """Lightweight license metadata: an explicit ``payload.license_class`` if the
    ingest set one, else the manual-batch provenance license (raw_provenance ->
    provenance -> license), else None. Internal-only corpus, so this is a facet
    hint, not an enforcement gate."""
    lc = payload.get("license_class")
    if isinstance(lc, str) and lc.strip():
        return lc.strip()
    prov = _as_dict(row.get("raw_provenance"))
    inner = prov.get("provenance")
    if isinstance(inner, Mapping):
        lic = inner.get("license")
        if isinstance(lic, str) and lic.strip():
            return lic.strip()
    lic = prov.get("license")
    if isinstance(lic, str) and lic.strip():
        return lic.strip()
    return None


def signal_license_class(
    row: Mapping[str, Any], payload: Mapping[str, Any] | None = None,
) -> str | None:
    """Public license-class resolution for a ``signals`` row.

    ``payload.license_class`` (the LIC-2 SourceScope→signal ingest stamp)
    first, then the manual-batch ``raw_provenance`` license, else ``None``.
    The ONE resolution shared by the corpus doc projection (facet hint) and
    the P2-2 evidence-archiver license gate (enforcement) — so the gate and
    the facet can never disagree. ``payload`` may be omitted; it is then
    coerced from ``row['payload']`` (asyncpg jsonb may arrive as str)."""
    if payload is None:
        payload = _as_dict(row.get("payload"))
    return _license_class(row, payload)


def signal_retrieval_origin(
    row: Mapping[str, Any], payload: Mapping[str, Any] | None = None,
) -> str | None:
    """Public retrieval-origin resolution for a ``signals`` row (mig 0112).

    Delegates to :func:`legba.data.retrieval_origin.resolve_retrieval_origin`
    — the ONE owner of the column-then-payload resolution — so this corpus
    facet and the evidence-archiver's fail-closed retention gate read exactly
    the same value. ``None`` = a curated registered source (the default for
    every row written before the concept existed; no backfill).
    """
    if payload is None:
        payload = _as_dict(row.get("payload"))
    return resolve_retrieval_origin(row, payload)


def signal_to_doc(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a ``signals`` row → an OpenSearch corpus doc.

    ``row`` is any mapping over the signal columns + ``payload`` (an asyncpg
    ``Record`` or a plain dict). The returned doc carries ``_id`` = the signal id
    (so a re-index OVERWRITES in place — idempotent), plus the text/keyword/date
    fields declared in :data:`CORPUS_INDEX_MAPPING`. None/empty fields are dropped
    so a doc stays lean and a null never reaches a date/float mapping.

    Every ``text``-mapped field sourced directly from ``payload`` goes through
    :func:`_as_text_field` (R13) — a non-string value there (a dict/list of
    structured data, e.g. a GDELT CAMEO event dump in ``payload.raw_body``) is
    OMITTED rather than handed to OpenSearch, which would reject the whole
    doc with a ``mapper_parsing_exception``.
    """
    r = dict(row)
    payload = _as_dict(r.get("payload"))

    doc: dict[str, Any] = {
        "_id": str(r.get("id")),
        # text
        "title": _as_text_field(payload.get("title")),
        "distilled_body": _as_text_field(payload.get("distilled_body")),
        "raw_body": _as_text_field(payload.get("raw_body")),
        # R6: the chat-platform full message body (telegram/discord) — see
        # _BEST_BODY_FIELDS docstring for why this outranks archived_text.
        "text": _as_text_field(payload.get("text")),
        # P2-1: the archived FULL article text (evidence_archiver writes
        # payload.archived_text + nulls indexed_at per the dirty-marker
        # contract, so the re-index lands it here — the S-10 depth fix).
        "archived_text": _as_text_field(payload.get("archived_text")),
        "summary": _as_text_field(payload.get("summary")),
        "best_body": _first_nonempty(payload, _BEST_BODY_FIELDS) or None,
        "entities_text": _entities_text(payload) or None,
        # keyword facets (columns)
        "source_id": r.get("source_id"),
        "geo": _as_str_list(r.get("geo")),
        "tags": _as_str_list(r.get("tags")),
        "entity_classes": _as_str_list(r.get("entity_classes")),
        "language": r.get("language"),
        "modality": r.get("modality"),
        "retention_class": r.get("retention_class"),
        "canonical_url": r.get("canonical_url"),
        "license_class": _license_class(r, payload),
        # R-3b — the retrieval-origin facet (migration 0112). Resolved by the
        # ONE owner so this hint and the archiver's retention gate can never
        # disagree; None for a curated source, which the drop-empties pass
        # below removes from the doc entirely (honest absence, no backfill).
        "retrieval_origin": signal_retrieval_origin(r, payload),
        # numeric / dates
        "source_credibility": _as_float(r.get("source_credibility")),
        "fetched_at": _iso(r.get("fetched_at")),
        "published_at": payload.get("published_at"),
    }
    # Drop None / empty-string / empty-list values (keep _id always).
    return {
        k: v
        for k, v in doc.items()
        if k == "_id" or (v is not None and v != "" and v != [])
    }


def _delete_status(err: Any) -> int | None:
    """HTTP status out of one ``async_bulk`` delete-error entry, or ``None``.

    The helper shape is ``{"delete": {"status": 404, ...}}``; anything else
    (a string, a different op) reads as "not a 404" so it stays in the error
    count rather than being silently forgiven.
    """
    if not isinstance(err, Mapping):
        return None
    inner = err.get("delete")
    if not isinstance(inner, Mapping):
        return None
    status = inner.get("status")
    return status if isinstance(status, int) else None


class OpenSearchStore:
    """Async wrapper around ``AsyncOpenSearch`` for the signal corpus index.

    Exposes only what the index-plane needs:

      * ``ensure_index(name, mapping)`` — idempotent create-if-absent (HEAD then
        create on 404).
      * ``bulk_index(index, docs)`` — bulk upsert; each doc's ``_id`` = the signal
        id so a re-index overwrites in place. Returns the success count.
      * ``bulk_delete(index, doc_ids)`` — bulk delete by ``_id``; an already-absent
        doc counts as a success. The DELETE-EXCEPTION, drained by the
        ``corpus_retention`` sweep off the ``corpus_tombstones`` queue (0175) —
        see the method docstring for why a derived projection is not substrate.
      * ``search(index, query, filters, size)`` — BM25 multi_match over the text
        fields + keyword term filters; returns scored rows.
      * ``get(index, doc_id)`` — the ``_source`` of one doc, or ``None``.
    """

    def __init__(self, cfg: OpenSearchConfig):
        if AsyncOpenSearch is None:  # pragma: no cover
            raise RuntimeError("opensearch-py is not installed")
        self._cfg = cfg
        self._client: "AsyncOpenSearch | None" = None

    @classmethod
    def from_env(cls) -> "OpenSearchStore":
        return cls(OpenSearchConfig.from_env())

    @property
    def client(self) -> "AsyncOpenSearch":
        if self._client is None:
            raise RuntimeError("OpenSearchStore not connected")
        return self._client

    @property
    def cfg(self) -> OpenSearchConfig:
        return self._cfg

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = AsyncOpenSearch(
            hosts=[{"host": self._cfg.host, "port": self._cfg.port}],
            use_ssl=self._cfg.use_ssl,
            verify_certs=self._cfg.verify_certs,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    async def ensure_index(self, name: str, mapping: Mapping[str, Any]) -> bool:
        """Create ``name`` with ``mapping`` if it doesn't exist. Idempotent.

        Returns True if newly created, False if it already existed. Tolerates a
        create race (another writer created it between the HEAD and the create).
        """
        exists = await self.client.indices.exists(index=name)
        if exists:
            return False
        try:
            await self.client.indices.create(index=name, body=dict(mapping))
        except Exception as exc:  # create race / already-exists → no-op
            if getattr(exc, "status_code", None) == 400 and (
                "resource_already_exists" in str(exc)
            ):
                return False
            raise
        return True

    # ------------------------------------------------------------------
    # Doc I/O passthroughs
    # ------------------------------------------------------------------

    async def bulk_index(self, index: str, docs: Iterable[Mapping[str, Any]]) -> int:
        """Bulk-index ``docs`` (each carrying ``_id``) into ``index``.

        Idempotent by design: a doc's ``_id`` = the signal id, so a re-index
        OVERWRITES the same doc in place rather than duplicating it. Returns the
        number of docs successfully indexed. A transport/connection failure
        RAISES (the caller leaves the batch un-stamped → retried next tick); a
        per-doc mapping error is logged and counted as a non-success (it is NOT
        re-raised) so one poison doc never wedges the batch.
        """
        actions: list[dict[str, Any]] = []
        for doc in docs:
            d = dict(doc)
            _id = d.pop("_id", None)
            action: dict[str, Any] = {
                "_op_type": "index",
                "_index": index,
                "_source": d,
            }
            if _id is not None:
                action["_id"] = str(_id)
            actions.append(action)
        if not actions:
            return 0
        success, errors = await async_bulk(
            self.client, actions, raise_on_error=False, stats_only=False
        )
        if errors:
            logger.warning(
                "opensearch.bulk_index index=%s indexed=%d errors=%d first_err=%s",
                index,
                success,
                len(errors),
                str(errors[0])[:300],
            )
        return int(success)

    async def bulk_delete(self, index: str, doc_ids: Iterable[str]) -> int:
        """Bulk-delete ``doc_ids`` from ``index``. Returns the number deleted.

        THE DELETE-EXCEPTION. This platform does not hard-delete substrate: facts
        are superseded, journal rows are soft-closed, entity folds are reversible.
        This method is an explicit, bounded exception for the same reason
        :meth:`legba.data.qdrant.QdrantStore.delete_doc_points` is one — a corpus
        doc is not substrate, it is a DERIVED PROJECTION of a ``signals`` row
        (``_id`` IS that row's uuid). When the row is gone the projection is not
        evidence of anything; it is a search hit pointing at nothing, and
        ``read_document`` will serve it verbatim because that path does no
        Postgres existence check. Keeping it is the destructive option.

        Deleting a doc that is ALREADY absent is a success, not an error: the
        drain is idempotent, and a 404 on delete means the desired end state
        holds. Those are filtered out of the error count rather than retried
        forever.

        A transport/connection failure RAISES (the caller leaves the batch
        un-stamped → retried next tick); per-doc failures are logged, counted as
        non-successes, and never re-raised, so one poison id cannot wedge a batch.
        """
        actions = [
            {"_op_type": "delete", "_index": index, "_id": str(d)} for d in doc_ids
        ]
        if not actions:
            return 0
        success, errors = await async_bulk(
            self.client, actions, raise_on_error=False, stats_only=False
        )
        # A 404 means the doc is already gone — that IS the outcome we wanted.
        real_errors = [e for e in (errors or []) if _delete_status(e) != 404]
        already_absent = len(errors or []) - len(real_errors)
        if real_errors:
            logger.warning(
                "opensearch.bulk_delete index=%s deleted=%d errors=%d first_err=%s",
                index,
                success,
                len(real_errors),
                str(real_errors[0])[:300],
            )
        return int(success) + already_absent

    async def search(
        self,
        index: str,
        query: str | None,
        *,
        filters: Mapping[str, Any] | None = None,
        size: int = 10,
    ) -> list[dict[str, Any]]:
        """BM25 multi_match over the text fields + keyword term filters.

        ``filters`` maps a keyword field → a scalar (term) or a list (terms).
        A falsy ``query`` degrades to match_all (filter-only browse). Returns
        ``[{"id", "score", "source"}]`` sorted by BM25 score.
        """
        must: list[dict[str, Any]] = []
        if query:
            must.append(
                {
                    "multi_match": {
                        "query": query,
                        "fields": list(_SEARCH_FIELDS),
                        "type": "best_fields",
                    }
                }
            )
        else:
            must.append({"match_all": {}})

        filter_clauses: list[dict[str, Any]] = []
        for field, value in (filters or {}).items():
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                vals = [v for v in value if v is not None]
                if vals:
                    filter_clauses.append({"terms": {field: list(vals)}})
            else:
                filter_clauses.append({"term": {field: value}})

        body = {
            "size": int(size),
            "query": {"bool": {"must": must, "filter": filter_clauses}},
        }
        resp = await self.client.search(index=index, body=body)
        hits = (resp.get("hits") or {}).get("hits") or []
        rows: list[dict[str, Any]] = []
        for h in hits:
            rows.append(
                {
                    "id": h.get("_id"),
                    "score": h.get("_score"),
                    "source": h.get("_source") or {},
                }
            )
        return rows

    async def get(self, index: str, doc_id: str) -> dict[str, Any] | None:
        """Fetch one doc's ``_source`` by id; ``None`` on a 404."""
        try:
            resp = await self.client.get(index=index, id=str(doc_id))
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        return resp.get("_source")


__all__ = [
    "CORPUS_INDEX_MAPPING",
    "OpenSearchStore",
    "signal_license_class",
    "signal_retrieval_origin",
    "signal_to_doc",
]
