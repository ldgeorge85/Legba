# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``reenrich_ner`` sub-handler — ONE-TIME NER backfill over the historical backlog.

The multilingual/telegram NER fix (``NERMultilingualHandler`` M11 translate-then-NER
+ M12 telegram ``payload.text``) landed AFTER ~9,143 signals were already ingested,
so those rows carry ``payload.entities = []`` (0 entities) forever — the FORWARD fix
only runs at ingest and never revisits a persisted signal. This sub-handler is the
BACKFILL leg: an async cadence sweep that re-runs the SAME LIVE handler over the
historical backlog so the pre-fix signals gain their entities and re-enter the
downstream entity-resolution / graph / retrieval planes.

It is an ASYNC SWEEP (never inline in ingest), mirroring ``signal_summarizer`` /
``corpus_indexer`` / ``signal_embedder`` exactly: every time the bound
``deterministic`` analyst fires (cadence), it re-enriches the next throttled batch
of candidate signals, draining the ~9,143 backlog over a few hours.

Candidate = a signal with NO entities that the OLD ner path could not enrich:

  * a TELEGRAM signal — its message body lives in ``payload.text``, which the
    pre-M12 field set never fed to /extract; OR
  * a NON-LATIN-script language (``ar/fa/he/ru/uk/zh/ja/ko/hi/th/ur`` — the handler's
    default ``translate_languages``) — the pre-M11 English-only spaCy extracted ~0
    spans, so translate-then-NER is what unlocks them.

Per examined signal (mirrors the stamp-all-examined idempotency of the sibling
sweeps, so the partial index ``idx_signals_needs_reenrich`` drains and nothing is
re-scanned):

  * SELECT the next batch ``WHERE reenriched_at IS NULL AND <candidate predicate>
    ORDER BY fetched_at DESC LIMIT max_reenrich`` (newest-first, per the summarizer's
    recency rationale; migration 0085 adds the marker column + partial scan index).
  * Rebuild a :class:`legba.data.sources.Signal` from the row payload and run the
    LIVE :meth:`NERMultilingualHandler.transform` — REUSING the production
    translate-then-NER path (built ONCE per tick from the ner config + the threaded
    ``nlp_client``; this module NEVER reimplements translate / NER).
  * On a row that GAINS entities: persist ``payload.entities``, PROMOTE the distinct
    entity classes into the ``signals.entity_classes`` column (mirrors the forward
    ingest promote in ``dapr_host._source_enrichment_factory``), reset
    ``entities_resolved_at = NULL`` so the existing ``entity_resolution`` sweep
    re-folds the new entities into ``entity_profiles``, and stamp ``reenriched_at``.
  * On a row that yields NO entities (genuine empty — the NER ran, found nothing):
    bulk-stamp ``reenriched_at`` only (drain; ``entities_resolved_at`` untouched).

Degrade-not-break (mirrors ``signal_embedder`` exactly):
  * If the hosted ``nlp_client`` is absent from ``deps.extras`` (the NLP plane didn't
    wire — dep missing / models-host unreachable at deps-build), the tick NO-OPs with
    a LOUD warning and ``skipped_no_nlp = 1``, leaving rows un-enriched for a tick
    where the plane IS wired (the SELECT never runs).
  * ``transform`` swallows a per-signal service failure and returns ``entities=[]``;
    the sweep detects it via the handler's ``_signals_failed`` delta so a
    service-failed row is NOT mistaken for a genuine empty. If the tick re-enriched
    at least one OTHER row (the backend is healthy → this is a poison / transient
    row) the failed row is stamped ``reenriched_at`` + a ``payload.reenrich_failed``
    sentinel so it drains and one bad row never wedges the sweep forever. If NOTHING
    re-enriched this tick (a probable models-host outage) the failed rows are left
    UNSTAMPED (retried next tick) so a transient outage can't burn the whole batch —
    the sweep goes LOUD instead.
  * A per-signal ``transform`` EXCEPTION (should not happen — transform is
    graceful-degrade) is caught and treated as a failure exactly like the above.

Output ``data`` keys (the cadence receipt the operator reads):
    examined        int — rows pulled this run
    reenriched      int — rows that GAINED entities this run (payload + classes +
                          entities_resolved_at reset + reenriched_at stamped)
    entities_added  int — total entities written across the re-enriched rows
    no_entities     int — rows drained (NER ran, found nothing) — reenriched_at only
    failures        int — rows whose NER call failed this run (service / exception)
    skipped_no_nlp  int — 1 when the hosted nlp_client was not wired (else 0)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

from ...filters._contract import FilterContext
from ...provenance.models import FindingPayload
from ...sources._contract import Signal
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "reenrich_ner"

#: The key under which the runtime stashes the hosted :class:`NlpServiceClient` on
#: ``StandardDeps.extras`` for this sweep (wired in analyst_deps_builder when the
#: bound sub-handler is ``reenrich_ner``). Absent → the sweep no-ops that tick.
NLP_DEPS_EXTRA_KEY = "reenrich_ner_nlp"

#: Per-run cap on the number of signals re-enriched (each is a bounded hosted
#: translate + /extract round-trip). This IS the SELECT ``LIMIT`` — the sweep pulls
#: at most this many candidates per tick. ~500/tick over a 15-min cadence drains the
#: ~9,143 backlog in a few hours, then idles (no new candidates arrive — the forward
#: fix enriches every new signal at ingest).
_DEFAULT_MAX_REENRICH = 500

#: Per-signal wall-clock timeout (seconds) around the translate+NER transform. On
#: expiry the row DEGRADES to a failure (never a hang / retry-forever) — same shape
#: as the summarizer's LLM timeout. The hosted client carries its own httpx timeout
#: too; this is the belt.
REENRICH_TIMEOUT_SECONDS = 60.0

#: The non-Latin source-language set routed through translate-then-NER, threaded into
#: BOTH the SELECT predicate and the NER handler config so the sweep's candidate set
#: and the handler's translate routing stay in sync. Mirrors
#: ``ner.py::_NON_LATIN_TRANSLATE_LANGS`` (kept as a literal here so this backfill
#: module is self-contained and the SELECT never imports the httpx-bearing ner
#: module at query time).
_DEFAULT_TRANSLATE_LANGS: tuple[str, ...] = (
    "ar", "fa", "he", "ru", "uk", "zh", "ja", "ko", "hi", "th", "ur",
)

#: Marker written into ``payload.reenrich_failed`` on a poison row (NER failed on an
#: otherwise-healthy tick) so it drains out of the partial index and is never
#: retried forever (mirrors the summarizer's ``summarize_failed`` sentinel).
_FAILED_MARKER_KEY = "reenrich_failed"

#: NEWEST-first scan of the un-re-enriched candidate pool (WHERE matches the partial
#: index idx_signals_needs_reenrich from migration 0085; btree supports the reverse
#: scan). ``reenriched_at IS NULL`` is the forward-progress gate; the candidate
#: predicate is a no-entity signal from telegram OR a non-Latin language. $1 = the
#: per-run LIMIT (max_reenrich); $2 = the translate-language set (kept in sync with
#: the NER handler config the sweep builds).
_SELECT_BATCH_SQL = """
    SELECT id, source_id, language, entity_classes, payload
      FROM signals
     WHERE reenriched_at IS NULL
       AND (
             payload IS NULL
             OR NOT (payload ? 'entities')
             OR jsonb_typeof(payload->'entities') <> 'array'
             OR jsonb_array_length(payload->'entities') = 0
           )
       AND (
             source_id ILIKE '%telegram%'
             OR lower(payload->>'language') = ANY($2::text[])
           )
     ORDER BY fetched_at DESC
     LIMIT $1
"""

#: Re-enriched row — write payload.entities + the promoted entity_classes, reset
#: entities_resolved_at so entity_resolution re-folds the row, and stamp
#: reenriched_at, all in ONE atomic UPDATE so the row is never left half-written.
#: $1 = id; $2 = json.dumps(entities list); $3 = the merged distinct class list.
_WRITE_REENRICHED_SQL = """
    UPDATE signals
       SET payload = jsonb_set(
               COALESCE(payload, '{}'::jsonb), '{entities}', $2::jsonb, true
           ),
           entity_classes = $3::text[],
           entities_resolved_at = NULL,
           reenriched_at = now(),
           indexed_at = NULL,
           updated_at = now()
     WHERE id = $1
"""

#: No-entity rows (NER ran, found nothing) — bulk-stamp reenriched_at to drain them
#: (entities_resolved_at untouched: no new entities to re-fold). $1 = uuid[].
_STAMP_NO_ENTITIES_BULK_SQL = """
    UPDATE signals
       SET reenriched_at = now()
     WHERE id = ANY($1::uuid[])
"""

#: Poison rows (NER failed, but the backend is healthy) — bulk-stamp reenriched_at +
#: a payload.reenrich_failed sentinel so one bad row never wedges the sweep. $1 =
#: uuid[]. (Only run when the tick re-enriched at least one other row — see the
#: outage guard in _sweep_batch.)
_STAMP_FAILED_BULK_SQL = """
    UPDATE signals
       SET payload = jsonb_set(
               COALESCE(payload, '{}'::jsonb), '{reenrich_failed}', 'true'::jsonb, true
           ),
           reenriched_at = now(),
           updated_at = now()
     WHERE id = ANY($1::uuid[])
"""


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


def _classes_from_entities(entities: list[Any]) -> list[str]:
    """Distinct ``entity.class`` values, first-occurrence order (mirrors the forward
    promote in ``dapr_host._source_enrichment_factory`` ~L1928-1933)."""
    classes: list[str] = []
    for e in entities:
        cls = e.get("class") if isinstance(e, dict) else None
        if isinstance(cls, str) and cls and cls not in classes:
            classes.append(cls)
    return classes


def _resolve_nlp(deps: Any | None) -> Any | None:
    """Pull the hosted :class:`NlpServiceClient` off ``deps.extras`` (or ``None``).

    Injected by
    :func:`legba.runtime.analyst_deps_builder._wire_reenrich_ner` when the bound
    sub-handler is ``reenrich_ner``. Absent → the sweep no-ops that tick."""
    if deps is None:
        return None
    extras = getattr(deps, "extras", None)
    if not isinstance(extras, Mapping):
        return None
    return extras.get(NLP_DEPS_EXTRA_KEY)


def _build_ner_handler(nlp_client: Any, translate_languages: list[str]) -> Any:
    """Construct the LIVE :class:`NERMultilingualHandler` ONCE per tick from the ner
    config + the threaded client. Lazily imported so this backfill module (and the
    deterministic-kind registry that imports it) never drags the httpx-bearing
    nlp_service client into the import chain at load time (the runtime
    sandbox-cascade rule). The handler needs no lifecycle hooks for ``transform`` —
    only ``self._client`` + ``self._config`` — so we hand it the client directly."""
    from ...filters.ner import NERMultilingualConfig, NERMultilingualHandler

    cfg = NERMultilingualConfig(translate_languages=list(translate_languages))
    return NERMultilingualHandler(cfg, nlp_client=nlp_client)


async def _sweep_batch(
    pool: Any,
    *,
    nlp_client: Any,
    translate_languages: list[str],
    max_reenrich: int,
) -> dict[str, int]:
    """Re-enrich the next throttled batch of candidate signals via the LIVE NER path.

    Sequential (no gather-fanout) so a tick never bursts the hosted models plane. The
    pooled connection is NOT held across the slow translate+NER calls — the batch is
    SELECTed once, then each re-enriched row briefly re-acquires a connection to
    write, and the drained/failed rows are bulk-stamped at the end. All writes are
    idempotent + forward-progressing."""
    counters = {
        "examined": 0,
        "reenriched": 0,
        "entities_added": 0,
        "no_entities": 0,
        "failures": 0,
        "skipped_no_nlp": 0,
    }

    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_BATCH_SQL, max_reenrich, list(translate_languages))
    if not rows:
        return counters
    counters["examined"] = len(rows)

    # Build the production NER handler ONCE and reuse it across the batch.
    handler = _build_ner_handler(nlp_client, translate_languages)
    ctx = FilterContext(target_id=SUB_HANDLER_NAME, filter_id=SUB_HANDLER_NAME)

    no_entity_ids: list[Any] = []
    failed_ids: list[Any] = []
    reenriched = 0
    entities_added = 0

    for r in rows:
        # transform() swallows a service failure and returns entities=[]; track the
        # handler's own failure counter so a service-failed row is distinguished from
        # a genuine empty (and never drained as if the NER had truly found nothing).
        prev_failed = int(getattr(handler, "_signals_failed", 0))
        # ALSO track the translate-failure counter: _maybe_translate swallows a
        # /translate 5xx (best-effort) — it bumps _translate_failures, NOT
        # _signals_failed, then runs /extract on the UNTRANSLATED non-Latin text
        # (→ ~0 entities). Without this a translate-only outage would mis-classify
        # those rows as genuine-empty and DRAIN them (never retried) — burning the
        # exact non-Latin rows this backfill exists to rescue.
        prev_tx_failed = int(getattr(handler, "_translate_failures", 0))
        payload = _as_dict(r["payload"])
        signal = Signal(
            signal_id=r["id"],
            source_id=r["source_id"] or SUB_HANDLER_NAME,
            language_hint=r["language"],
            payload=payload,
        )
        try:
            out = await asyncio.wait_for(
                handler.transform(signal, ctx), timeout=REENRICH_TIMEOUT_SECONDS
            )
        except Exception as exc:  # degrade-not-break: transform should never raise
            logger.warning("reenrich_ner.transform_error signal_id=%s err=%s", r["id"], exc)
            failed_ids.append(r["id"])
            continue

        service_failed = int(getattr(handler, "_signals_failed", 0)) > prev_failed
        translate_failed = int(getattr(handler, "_translate_failures", 0)) > prev_tx_failed
        out_payload = out.payload if isinstance(out.payload, dict) else {}
        entities = out_payload.get("entities")
        entities = entities if isinstance(entities, list) else []

        if not entities:
            if service_failed or translate_failed:
                # An NLP hop failed (NER OR translate) — NOT a genuine empty. A
                # translate failure means /extract ran on untranslated non-Latin
                # text (→ ~0 entities), so treat it as a failure and let the outage
                # guard below decide drain-vs-retry rather than permanently draining
                # the very non-Latin rows this backfill exists to fix.
                failed_ids.append(r["id"])
            else:
                # Genuine empty — the NER ran and found nothing. Drain it.
                no_entity_ids.append(r["id"])
            continue

        # GAINED entities — persist payload + promote classes + reset the resolution
        # marker so entity_resolution re-folds the row, all in one atomic UPDATE.
        new_classes = _classes_from_entities(entities)
        existing = list(r["entity_classes"] or [])
        merged = existing + [c for c in new_classes if c not in existing]
        async with pool.acquire() as conn:
            await conn.execute(
                _WRITE_REENRICHED_SQL, r["id"], json.dumps(entities), merged
            )
        reenriched += 1
        entities_added += len(entities)

    # Bulk-stamp the drained + failed rows (one UPDATE per outcome group).
    async with pool.acquire() as conn:
        if no_entity_ids:
            await conn.execute(_STAMP_NO_ENTITIES_BULK_SQL, no_entity_ids)
        if failed_ids:
            counters["failures"] = len(failed_ids)
            if reenriched > 0:
                # The backend re-enriched other rows this tick → it is healthy, so a
                # failure is a POISON row: stamp it (+ the sentinel) so it drains and
                # one bad row never wedges the sweep forever.
                await conn.execute(_STAMP_FAILED_BULK_SQL, failed_ids)
            else:
                # NOTHING re-enriched this tick → a probable models-host OUTAGE. Leave
                # the failed rows UNSTAMPED (retried next tick) so a transient outage
                # can't burn the whole batch. Go LOUD so the misconfig is visible.
                logger.warning(
                    "reenrich_ner.nlp_outage failures=%d — no signal re-enriched "
                    "this tick; leaving rows UNSTAMPED for retry (likely a hosted "
                    "NLP-plane outage, not poison rows)",
                    len(failed_ids),
                )

    counters["reenriched"] = reenriched
    counters["entities_added"] = entities_added
    counters["no_entities"] = len(no_entity_ids)
    return counters


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    title = (
        f"NER backfill: re-enriched {counters.get('reenriched', 0)} signal(s) "
        f"(+{counters.get('entities_added', 0)} entities), "
        f"examined {counters.get('examined', 0)}, "
        f"{counters.get('failures', 0)} failed"
    )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "reenrich_ner"]
    if counters.get("reenriched", 0):
        tags.append("reenriched")
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
    ignored — the unit of work is "the next batch of un-re-enriched candidate
    signals"). ``deps is None`` (unit-test path) yields a zeroed run. Usage is always
    zeroed (deterministic kind, no LLM — the hosted NER/translate plane is
    self-hosted / $0)."""
    counters: dict[str, int] = {
        "examined": 0,
        "reenriched": 0,
        "entities_added": 0,
        "no_entities": 0,
        "failures": 0,
        "skipped_no_nlp": 0,
    }
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        nlp_client = _resolve_nlp(deps)
        if nlp_client is None:
            # The NLP plane isn't wired (dep missing / models-host unreachable at
            # deps-build). No-op this tick — leave rows un-enriched for a tick where
            # the plane IS wired. Go LOUD so a mis-wire is observable.
            counters["skipped_no_nlp"] = 1
            logger.warning(
                "reenrich_ner.no_nlp — NlpServiceClient absent from deps.extras[%r]; "
                "the NLP plane did not wire (dep missing / models-host unreachable). "
                "Signals left un-re-enriched this tick.",
                NLP_DEPS_EXTRA_KEY,
            )
        else:
            max_reenrich = int(options.get("max_reenrich", _DEFAULT_MAX_REENRICH))
            translate_languages = list(
                options.get("translate_languages", _DEFAULT_TRANSLATE_LANGS)
            )
            try:
                counters = await _sweep_batch(
                    pool,
                    nlp_client=nlp_client,
                    translate_languages=translate_languages,
                    max_reenrich=max_reenrich,
                )
            except Exception as exc:
                logger.warning("reenrich_ner.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME", "NLP_DEPS_EXTRA_KEY"]
