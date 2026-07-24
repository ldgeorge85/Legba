# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``signal_embedder`` sub-handler — async embedding of signals into Qdrant.

The VECTOR PLANE of the signal-content-depth program. Downstream retrieval wants
SEMANTIC search over the whole signal body (cosine nearest-neighbour + payload
facets), not just the lexical corpus or the analytic slice — so this sub-handler
projects each signal into the Qdrant ``legba_signals`` collection (1024-dim
BGE-M3 cosine). Signals already live structured in Postgres and, for the lexical
mining substrate, in the OpenSearch corpus; this is the third leg — the vector
retrieval substrate that lights up ``vector_search`` (which no-ops today because
the collection holds 0 points).

It is an ASYNC SWEEP (never inline in ingest — ingestion must stay cheap): every
time the bound ``deterministic`` analyst fires (cadence), it embeds the next
throttled batch of un-embedded signals, draining the ~109k backlog over a few
days. Embedding is a HOSTED GPU call (the shared ``embed.primary.openai_compat``
gateway), so the per-tick batch is BOUNDED — much smaller than the lexical
corpus_indexer's, and run SEQUENTIALLY (mirroring signal_summarizer).

Per examined signal (mirrors ``signal_summarizer`` / ``entity_resolution``
stamp-all-examined idempotency, so the partial index drains and nothing is
re-scanned):

  * SELECT the next batch ``WHERE embedding_ref IS NULL ORDER BY fetched_at DESC``
    (newest-first — fresh signals reach the vector plane within a tick of ingest,
    per the summarizer's own recency rationale; migration 0084 adds the partial
    scan index over the un-embedded pool).
  * Pick the best body = FIRST non-empty HTML-cleaned field in precedence
    ``distilled_body`` (OUR analysis-tuned brief, preferred) → ``raw_body`` (the
    full article) → ``summary`` → ``description`` → ``content_text`` → ``text``.
    The input is bounded to :data:`MAX_BODY_INPUT_CHARS` so a huge body can never
    overrun the gateway.
  * If NO usable body → skip embedding BUT still stamp the marker (the sentinel
    ``embedding_ref = 'no_body'``) so short/no-body rows drain out of the partial
    index cheaply and are never re-scanned.
  * Embed each body via the wired hosted embedder (``await embedder.embed(text)``)
    and buffer a ``(str(signal_id), vector, payload)`` point. The payload carries
    the useful facets (geo / tags / entity_classes / language / modality /
    source_id / title / fetched_at) for filtered vector search.
  * ``ensure_signals_collection`` (idempotent) then ``upsert_points`` the batch.
    The point ``_id`` IS the signal id, so a re-embed OVERWRITES in place (never a
    duplicate — the sweep is idempotent).
  * Stamp ``embedding_ref = <signal id>`` on every successfully-embedded row in
    ONE bulk UPDATE (one UPDATE per outcome group, like the summarizer).

Degrade-not-break:
  * If the Qdrant store OR the hosted embedder is absent from ``deps.extras`` (the
    vector plane didn't wire — dep missing / gateway unreachable at deps-build),
    the tick NO-OPs with a LOUD warning (like corpus_indexer's no-store guard),
    leaving rows un-embedded for a tick where the plane IS wired.
  * A per-embed exception (502 / timeout) DEGRADES that one row. If the tick
    embedded at least one other signal (the backend is healthy → this is a poison
    row) the row is stamped with the ``embedding_ref = 'embed_failed'`` sentinel so
    it drains and one bad row never wedges the sweep forever (mirrors the
    summarizer's ``summarize_failed``). If NOTHING embedded this tick (a probable
    gateway outage) the failed rows are left UNSTAMPED (retried next tick) so a
    transient outage can't burn the whole batch — the sweep goes LOUD instead.
  * A transport failure in ``ensure_signals_collection`` / ``upsert_points`` RAISES
    to the caller BEFORE the stamp, so the batch is left un-stamped and retried
    next tick (the ``_id`` overwrite makes the retry idempotent).

Output ``data`` keys (the cadence receipt the operator reads):
    examined            int — rows pulled this run
    embedded            int — rows embedded + upserted into legba_signals this run
    skipped_no_body     int — rows drained with no embed call (no usable body)
    failures            int — rows whose embed call raised this run
    skipped_no_store    int — 1 when the Qdrant store was not wired (else 0)
    skipped_no_embedder int — 1 when the hosted embedder was not wired (else 0)
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "signal_embedder"

#: Keys under which the runtime stashes the connected QdrantStore + the hosted
#: embedding client on ``StandardDeps.extras`` for this sweep (wired in
#: analyst_deps_builder when the bound sub-handler is ``signal_embedder``).
QDRANT_DEPS_EXTRA_KEY = "signal_embedder_qdrant"
EMBEDDER_DEPS_EXTRA_KEY = "signal_embedder_embedder"

#: How many signals to SELECT per tick. Bounded (a hosted GPU call per body) and
#: kept close to the per-run embed cap so the SELECT rarely over-reads.
_DEFAULT_BATCH = 200

#: Per-run cap on hosted EMBED calls (the shared gateway throttle). Both a
#: successful embed and a raised call count toward it. ~200/tick over a 15-min
#: cadence drains the ~109k backlog in a few days.
_DEFAULT_MAX_EMBEDS = 200

#: Hard input bound: only the first N chars of the body are embedded so a
#: pathologically long article can never overrun the model context / gateway.
MAX_BODY_INPUT_CHARS = 8000

#: Per-call wall-clock timeout (seconds). On expiry the row DEGRADES (never a
#: hang / retry-forever) — same shape as the summarizer's LLM timeout.
EMBED_TIMEOUT_SECONDS = 30.0

#: Marker sentinels stamped on EXAMINED rows that were not point-embedded, so
#: they drain out of the un-embedded partial index and are never re-scanned.
_NO_BODY_MARKER = "no_body"
_FAILED_MARKER = "embed_failed"

#: Candidate body fields, in FIRST-NON-EMPTY precedence. distilled_body (OUR
#: analysis-tuned brief) leads because it is the compact, faithful representation
#: we most want a semantic vector of; raw_body (the full article) is the fallback
#: when a signal hasn't been summarized yet, then the publisher teaser fields.
#: (This differs from signal_summarizer's LONGEST-field pick, whose job is to find
#: the richest source TO summarize — here we want the best single embed input.)
_BODY_FIELDS = (
    "distilled_body",
    "raw_body",
    "summary",
    "description",
    "content_text",
    "text",
)

#: Facet columns copied verbatim into the Qdrant payload for filtered search.
_PAYLOAD_FACET_COLS = ("geo", "tags", "entity_classes", "language", "modality")

#: HTML strip so the body pick + the MAX_BODY_INPUT_CHARS truncation both operate
#: on real prose, not markup (raw_body is raw content:encoded HTML). Copied from
#: signal_summarizer so this module stays self-contained (a sibling-private helper
#: is not part of either module's public surface).
_HTML_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WS_RE = re.compile(r"\s+")


def _clean_html(text: str) -> str:
    """Drop script/style, strip tags, unescape entities, collapse whitespace."""
    if not text:
        return ""
    text = _HTML_SCRIPT_STYLE_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _HTML_WS_RE.sub(" ", text).strip()


def _as_dict(v: Any) -> dict[str, Any]:
    """Coerce a payload column (jsonb dict or JSON string) to a dict; else ``{}``."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _pick_body(payload: Mapping[str, Any]) -> str:
    """First non-empty HTML-cleaned candidate body (see ``_BODY_FIELDS`` order)."""
    for _k in _BODY_FIELDS:
        _v = payload.get(_k)
        if not (isinstance(_v, str) and _v.strip()):
            continue
        cleaned = _clean_html(_v)
        if cleaned:
            return cleaned
    return ""


def _jsonable(value: Any) -> Any:
    """Coerce a facet value to something the Qdrant payload can serialize.

    Passes lists / scalars through; ISO-formats datetimes; stringifies UUIDs /
    other objects. Best-effort — never raises (a facet is metadata, not the
    vector)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:  # pragma: no cover — defensive
            return str(value)
    return str(value)


def _build_payload(row: Mapping[str, Any], body_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble the Qdrant point payload (the facets a filtered search reads)."""
    payload: dict[str, Any] = {"signal_id": str(row["id"])}
    for col in _PAYLOAD_FACET_COLS:
        payload[col] = _jsonable(row.get(col))
    src = row.get("source_id")
    if src is not None:
        payload["source_id"] = _jsonable(src)
    fetched = row.get("fetched_at")
    if fetched is not None:
        payload["fetched_at"] = _jsonable(fetched)
    title = body_payload.get("title")
    if isinstance(title, str) and title.strip():
        payload["title"] = title.strip()
    return payload


#: NEWEST-first scan of the un-embedded pool (WHERE matches the partial index
#: idx_signals_unembedded from migration 0084; btree supports the reverse scan).
#: Newest-first so fresh signals reach the vector plane within ~a tick of ingest
#: (summarizer rationale). ``embedding_ref IS NULL`` is the first-embed gate; a
#: successful embed stamps embedding_ref = the signal id (a re-embed overwrites
#: the same Qdrant point, _id = signal id, so it is idempotent).
_SELECT_BATCH_SQL = """
    SELECT id, payload, geo, tags, entity_classes, language, modality,
           source_id, fetched_at
      FROM signals
     WHERE embedding_ref IS NULL
     ORDER BY fetched_at DESC
     LIMIT $1
"""

#: Successfully-embedded rows — stamp embedding_ref = the row's OWN id (the Qdrant
#: point id), in ONE bulk UPDATE. $1 = uuid[] of embedded ids.
_STAMP_EMBEDDED_BULK_SQL = """
    UPDATE signals
       SET embedding_ref = id::text
     WHERE id = ANY($1::uuid[])
"""

#: No-usable-body rows — bulk-stamp the sentinel (no embed call). $1 = uuid[].
_STAMP_NO_BODY_BULK_SQL = """
    UPDATE signals
       SET embedding_ref = $2
     WHERE id = ANY($1::uuid[])
"""

#: Poison rows (embed raised, but the backend is healthy) — bulk-stamp a failure
#: sentinel so one bad row never wedges the sweep. $1 = uuid[]; $2 = sentinel.
_STAMP_FAILED_BULK_SQL = _STAMP_NO_BODY_BULK_SQL


def _resolve_store(deps: Any | None) -> Any | None:
    """Pull the connected QdrantStore off ``deps.extras`` (or ``None``).

    Injected by
    :func:`legba.runtime.analyst_deps_builder._wire_signal_embedder` when the
    bound sub-handler is ``signal_embedder``. Absent → the sweep no-ops that
    tick."""
    if deps is None:
        return None
    extras = getattr(deps, "extras", None)
    if not isinstance(extras, Mapping):
        return None
    return extras.get(QDRANT_DEPS_EXTRA_KEY)


def _resolve_embedder(deps: Any | None) -> Any | None:
    """Pull the hosted embedding client off ``deps.extras`` (or ``None``).

    Injected by :func:`legba.runtime.analyst_deps_builder._wire_signal_embedder`
    (the process-lifetime ``embedding_service`` the host built at bring-up from
    ``embed.primary.openai_compat``). Absent → the sweep no-ops that tick."""
    if deps is None:
        return None
    extras = getattr(deps, "extras", None)
    if not isinstance(extras, Mapping):
        return None
    return extras.get(EMBEDDER_DEPS_EXTRA_KEY)


async def _embed_one(embedder: Any, body: str) -> list[float] | None:
    """Bounded, sequential embed of ONE body. Returns the vector, or ``None`` to
    DEGRADE (any timeout / exception / empty reply). The input is bounded to
    :data:`MAX_BODY_INPUT_CHARS`; a per-call wall-clock timeout guards a hang.
    NEVER raises to the caller (degrade-not-break)."""
    try:
        vec = await asyncio.wait_for(
            embedder.embed(body[:MAX_BODY_INPUT_CHARS]),
            timeout=EMBED_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("signal_embedder.embed_timeout")
        return None
    except Exception as exc:  # degrade-not-break: any backend failure → skip
        logger.warning("signal_embedder.embed_failed err=%s", exc)
        return None
    if not vec:
        return None
    return list(vec)


async def _sweep_batch(
    pool: Any,
    *,
    store: Any,
    embedder: Any,
    batch_limit: int,
    max_embeds: int,
) -> dict[str, int]:
    """Embed the next throttled batch of un-embedded signals into legba_signals.

    Sequential (no gather-fanout) so a tick never bursts the hosted gateway. The
    connection is NOT held across the slow embed calls — the batch is SELECTed
    once, embedded, upserted, then a fresh connection stamps the markers. All
    writes are idempotent + forward-progressing."""
    counters = {
        "examined": 0,
        "embedded": 0,
        "skipped_no_body": 0,
        "failures": 0,
        "skipped_no_store": 0,
        "skipped_no_embedder": 0,
    }

    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_BATCH_SQL, batch_limit)
    if not rows:
        return counters
    counters["examined"] = len(rows)

    embedded_ids: list[Any] = []
    no_body_ids: list[Any] = []
    failed_ids: list[Any] = []
    points: list[tuple[str, list[float], dict[str, Any]]] = []
    embed_attempts = 0

    for r in rows:
        body_payload = _as_dict(r["payload"])
        body = _pick_body(body_payload)
        if not body:
            # No usable body — drain cheaply with the sentinel (no embed call).
            no_body_ids.append(r["id"])
            continue
        if embed_attempts >= max_embeds:
            # Per-run gateway budget spent — leave the remaining rows UNSTAMPED
            # for the next tick.
            break
        embed_attempts += 1
        vec = await _embed_one(embedder, body)
        if vec is None:
            failed_ids.append(r["id"])
            continue
        points.append((str(r["id"]), vec, _build_payload(r, body_payload)))
        embedded_ids.append(r["id"])

    # Upsert the batch (ensure the collection idempotently first). RAISES before
    # any stamp on a transport failure → batch left un-stamped, retried next tick
    # (point _id = signal id → the re-embed overwrites in place).
    if points:
        await store.ensure_signals_collection()
        upserted = await store.upsert_points(store.cfg.signals_collection, points)
        counters["embedded"] = int(upserted)

    # Stamp examined rows in bulk, one UPDATE per outcome group (like the
    # summarizer). Un-embedded budget-overflow rows are intentionally NOT here —
    # they stay embedding_ref IS NULL and re-embed next tick.
    async with pool.acquire() as conn:
        if embedded_ids:
            await conn.execute(_STAMP_EMBEDDED_BULK_SQL, embedded_ids)
        if no_body_ids:
            await conn.execute(_STAMP_NO_BODY_BULK_SQL, no_body_ids, _NO_BODY_MARKER)
            counters["skipped_no_body"] = len(no_body_ids)
        if failed_ids:
            counters["failures"] = len(failed_ids)
            if embedded_ids:
                # The backend embedded other rows this tick → it is healthy, so a
                # failure is a POISON row: stamp the sentinel so it drains and one
                # bad row never wedges the sweep forever.
                await conn.execute(
                    _STAMP_FAILED_BULK_SQL, failed_ids, _FAILED_MARKER
                )
            else:
                # NOTHING embedded this tick → a probable gateway OUTAGE. Leave the
                # failed rows UNSTAMPED (retried next tick) so a transient outage
                # can't burn the whole batch. Go LOUD so the misconfig is visible.
                logger.warning(
                    "signal_embedder.embed_outage failures=%d — no signal embedded "
                    "this tick; leaving rows UNSTAMPED for retry (likely a hosted "
                    "embedding-gateway outage, not poison rows)",
                    len(failed_ids),
                )

    return counters


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    title = (
        f"Signal embedder: embedded {counters.get('embedded', 0)} signal(s), "
        f"examined {counters.get('examined', 0)}, "
        f"{counters.get('failures', 0)} failed"
    )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "signal_embedder"]
    if counters.get("embedded", 0):
        tags.append("embedded")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice is
    ignored — the unit of work is "the next batch of un-embedded signals").
    ``deps is None`` (unit-test path) yields a zeroed run. Usage is always zeroed
    (deterministic kind, no LLM — the hosted embedder is self-hosted / $0)."""
    counters: dict[str, int] = {
        "examined": 0,
        "embedded": 0,
        "skipped_no_body": 0,
        "failures": 0,
        "skipped_no_store": 0,
        "skipped_no_embedder": 0,
    }
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        store = _resolve_store(deps)
        embedder = _resolve_embedder(deps)
        if store is None or embedder is None:
            # The vector plane isn't fully wired (dep missing / gateway unreachable
            # at deps-build). No-op this tick — leave rows un-embedded for a tick
            # where the plane IS wired. Go LOUD so a mis-wire is observable.
            if store is None:
                counters["skipped_no_store"] = 1
            if embedder is None:
                counters["skipped_no_embedder"] = 1
            logger.warning(
                "signal_embedder.no_plane store=%s embedder=%s — the vector plane "
                "did not wire (dep missing / gateway unreachable). Signals left "
                "un-embedded this tick.",
                "wired" if store is not None else "absent",
                "wired" if embedder is not None else "absent",
            )
        else:
            batch_limit = int(options.get("batch_limit", _DEFAULT_BATCH))
            max_embeds = int(options.get("max_embeds", _DEFAULT_MAX_EMBEDS))
            try:
                counters = await _sweep_batch(
                    pool,
                    store=store,
                    embedder=embedder,
                    batch_limit=batch_limit,
                    max_embeds=max_embeds,
                )
            except Exception as exc:
                logger.warning("signal_embedder.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "handle",
    "SUB_HANDLER_NAME",
    "QDRANT_DEPS_EXTRA_KEY",
    "EMBEDDER_DEPS_EXTRA_KEY",
]
