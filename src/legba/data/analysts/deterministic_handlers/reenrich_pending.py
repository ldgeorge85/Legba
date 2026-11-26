# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``reenrich_pending`` sub-handler — the sweep over rows written UNENRICHED.

W11-F103 made the source actor write an entry WITHOUT its enrichment chain when
the NLP plane is degraded for the rest of a poll, or when the same entry has been
skipped on three consecutive polls (``runtime/source_enrich_hold.py``). Such a
row carries ``payload.enrichment_pending`` (``nlp_degraded_this_poll`` /
``enrich_hold_exhausted``) and nothing else ever re-enriches it: ``reenrich_ner``
selects only telegram / non-Latin rows, ``reenrich_translation`` keys on
``payload.language`` (unset when language detection was bypassed), and a later
re-pull of the same entry collapses into the existing row. This sweep is the
missing leg.

Per tick (a cadence-driven deterministic META analyst, like the sibling sweeps):

  * SELECT the next ``pending_batch`` rows (default 200) ``WHERE payload ?
    'enrichment_pending'``, OLDEST first — the partial index
    ``idx_signals_enrichment_pending`` (migration 0239) covers exactly that
    predicate, so the scan never touches the enriched pool.
  * Run the SAME chain the ingest path runs: :func:`run_baseline` with the row's
    own source descriptor's ``pipeline.enrichment`` stage, built by the HOST's
    ``_source_enrichment_factory`` (threaded onto ``deps.extras`` by
    ``runtime/analyst_deps_reenrich.py``; the descriptor is fetched from the
    registry once per source per tick). When the descriptor cannot be resolved
    (retired source, registry miss, factory not wired) the FALLBACK chain runs
    instead: language_detect + the live translate-then-NER handler
    ``reenrich_ner`` already reuses, followed by the same payload->column
    promote the host stage applies. Media is never re-fetched (``reference``):
    the bypassed write ran tier 2 already; only the enrichment stage was skipped.
  * SUCCESS: update the row IN PLACE — the payload keys the chain changed are
    merged in (``payload || changed``, so a sibling sweep's concurrent write to
    another key survives), ``language`` / ``geo`` / ``tags`` / ``entity_classes``
    are rewritten, ``entities_resolved_at`` and ``indexed_at`` are reset so
    entity resolution and the corpus indexer pick the row up again, and the
    ``enrichment_pending`` marker (with its attempt counter) is REMOVED.
  * FAILURE (the chain raised, timed out, or a stage degraded silently — the
    NER / translate failure counters moved, or the host stage fell back to the
    unenriched signal): the marker STAYS. On a tick where at least one other row
    was enriched (the plane is healthy, so this row is the problem) its
    ``payload.enrichment_pending_attempts`` is bumped; a row at
    :data:`PENDING_MAX_ATTEMPTS` leaves the scan but keeps its marker, so a
    handful of poison rows can never wedge the oldest-first queue and nothing is
    silently dropped. On a tick where NOTHING was enriched (a probable outage)
    no attempt is counted, and the first TIMEOUT stops the tick — one stall per
    tick, not one per row, the same rule the ingest path follows.

Output ``data`` keys (the cadence receipt):
    examined          int — rows pulled this run
    enriched          int — rows re-enriched and un-marked this run
    failed            int — rows whose chain failed this run (marker kept)
    still_pending     int — rows still carrying the marker after this run
                            (the whole backlog, parked rows included)
    fallback_chain    int — of ``enriched``, rows that went through the fallback
                            chain (their source descriptor was not resolvable)
    skipped_no_chain  int — 1 when neither chain was wired (the SELECT never ran)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Mapping

from pydantic import BaseModel

from ... import profile_constants as _profile_constants
from ...provenance.models import FindingPayload
from ...sources._contract import InMemoryStateStore, Signal, SourceContext
from ...sources.baseline import run_baseline
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "reenrich_pending"

#: ``deps.extras`` keys, threaded by ``runtime/analyst_deps_reenrich.py`` only
#: when the bound sub-handler is ``reenrich_pending``.
NLP_DEPS_EXTRA_KEY = "reenrich_pending_nlp"
REGISTRY_DEPS_EXTRA_KEY = "reenrich_pending_registry"
STAGE_FACTORY_DEPS_EXTRA_KEY = "reenrich_pending_stage_factory"

#: The marker the source actor writes (``source_enrich_hold.EnrichHold``) and the
#: attempt counter this sweep keeps beside it.
PENDING_KEY = "enrichment_pending"
ATTEMPTS_KEY = "enrichment_pending_attempts"

#: Rows pulled per tick — the SELECT ``LIMIT``; the ``pending_batch`` option.
_DEFAULT_PENDING_BATCH = 200

#: Failed attempts (counted on healthy ticks only) after which a row leaves the
#: scan. It keeps its marker and stays in ``still_pending``.
PENDING_MAX_ATTEMPTS = 5

#: Per-row wall clock around the chain (the hosted client carries its own httpx
#: timeout too; this is the belt — the same figure as reenrich_ner).
REENRICH_TIMEOUT_SECONDS = 60.0

_ATTEMPTS_SQL = (
    "CASE WHEN jsonb_typeof(payload->'enrichment_pending_attempts') = 'number' "
    "THEN (payload->>'enrichment_pending_attempts')::numeric ELSE 0 END"
)

#: OLDEST-first over the pending pool. ``payload ? 'enrichment_pending'`` is the
#: partial-index predicate (0239); the attempts bound filters within it. $1 = the
#: per-run LIMIT; $2 = PENDING_MAX_ATTEMPTS.
_SELECT_BATCH_SQL = f"""
    SELECT id, source_id, language, geo, tags, entity_classes, payload,
           canonical_url, content_hash
      FROM signals
     WHERE payload ? 'enrichment_pending'
       AND {_ATTEMPTS_SQL} < $2
     ORDER BY fetched_at ASC
     LIMIT $1
"""

#: Re-enriched row, one atomic UPDATE: merge the changed payload keys, drop the
#: marker + its counter, rewrite the typed columns, and re-queue the row for
#: entity resolution and the corpus index. The ``payload ?`` guard makes a row
#: un-marked by someone else in the meantime a no-op. $1 id; $2 changed keys
#: (jsonb object); $3 language; $4 geo; $5 tags; $6 entity_classes.
_WRITE_ENRICHED_SQL = """
    UPDATE signals
       SET payload = (COALESCE(payload, '{}'::jsonb) || $2::jsonb)
                     - 'enrichment_pending' - 'enrichment_pending_attempts',
           language = $3,
           geo = $4::text[],
           tags = $5::text[],
           entity_classes = $6::text[],
           entities_resolved_at = NULL,
           indexed_at = NULL,
           updated_at = now()
     WHERE id = $1
       AND payload ? 'enrichment_pending'
"""

#: Failed rows on a healthy tick: count the attempt, keep the marker. $1 = uuid[].
_BUMP_ATTEMPTS_SQL = f"""
    UPDATE signals
       SET payload = jsonb_set(
               payload, '{{enrichment_pending_attempts}}',
               to_jsonb(({_ATTEMPTS_SQL})::int + 1), true
           ),
           updated_at = now()
     WHERE id = ANY($1::uuid[])
       AND payload ? 'enrichment_pending'
"""

#: The backlog after this run (rides the same partial index).
_COUNT_PENDING_SQL = "SELECT count(*) FROM signals WHERE payload ? 'enrichment_pending'"


def _zero_counters() -> dict[str, int]:
    return {
        "examined": 0,
        "enriched": 0,
        "failed": 0,
        "still_pending": 0,
        "fallback_chain": 0,
        "skipped_no_chain": 0,
    }


def _as_dict(v: Any) -> dict[str, Any]:
    """A payload column (jsonb dict or JSON string) as a dict; else ``{}``."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _extra(deps: Any | None, key: str) -> Any | None:
    extras = getattr(deps, "extras", None) if deps is not None else None
    return extras.get(key) if isinstance(extras, Mapping) else None


# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------


class _Chain:
    """One source's enrichment stage + what failure detection reads.

    ``stage`` is ``None`` when the source declares no enrichment stages (the
    bypass skipped nothing, so the row only needs its marker removed)."""

    def __init__(
        self, stage: Any, *, scope_geo: list[str], via: str,
        handlers: list[Any] | None = None, state: Mapping[str, Any] | None = None,
    ) -> None:
        self.stage = stage
        self.scope_geo = scope_geo
        self.via = via
        self._handlers = list(handlers or [])
        self._state = state

    def failure_marks(self) -> int:
        """A monotone count of silent degradations: the NER / translate failure
        counters of every stage handler plus the host stage's fallbacks."""
        n = 0
        handlers = self._handlers
        state = self._state
        if state is not None:
            handlers = list(state.get("handlers") or [])
            n += int(state.get("fallbacks") or 0)
        for h in handlers:
            for attr in ("_signals_failed", "_translate_failures"):
                val = getattr(h, attr, 0)
                n += val if isinstance(val, int) and not isinstance(val, bool) else 0
        return n


def _promote(sig: Signal) -> Signal:
    """Payload enrichment -> the typed columns, exactly as the host's source
    enrichment stage does (``dapr_host._source_enrichment_factory``); used by the
    fallback chain only — the descriptor chain's stage promotes itself."""
    pl = sig.payload if isinstance(sig.payload, dict) else {}
    from ...filters._geo_precedence import promoted_geo_codes

    for iso2 in promoted_geo_codes(pl.get("geo")):
        if iso2 not in sig.geo:
            sig.geo.append(iso2)
    lang = pl.get("language")
    if lang and not sig.language:
        sig.language = lang
    ents = pl.get("entities")
    if isinstance(ents, list):
        for e in ents:
            cls = e.get("class") if isinstance(e, dict) else None
            if cls and cls not in sig.entity_classes:
                sig.entity_classes.append(cls)
    return sig


def _build_fallback_chain(nlp_client: Any) -> _Chain:
    """language_detect + the live translate-then-NER handler (the chain
    ``reenrich_ner`` reuses), annotate-only, then the promote."""
    from ...filters._contract import FilterContext
    from .reenrich_ner import _build_ner_handler

    handlers: list[Any] = []
    try:
        from ...filters.language_detect import LanguageDetectConfig, LanguageDetectHandler

        handlers.append(LanguageDetectHandler(LanguageDetectConfig()))
    except Exception as exc:  # no detector backend importable — NER still runs
        logger.warning("reenrich_pending.language_detect_unavailable err=%s", exc)
    handlers.append(
        _build_ner_handler(nlp_client, list(_profile_constants.translation_languages()))
    )
    ctx = FilterContext(target_id=SUB_HANDLER_NAME, filter_id=SUB_HANDLER_NAME)

    async def _stage(signal: Signal, _ctx: Any) -> Signal:
        current = signal
        for h in handlers:
            out = await h.transform(current, ctx)
            current = out if out is not None else current  # annotate-only
        return _promote(current)

    return _Chain(_stage, scope_geo=[], via="fallback", handlers=handlers)


async def _descriptor_chain(source_id: str, registry: Any, factory: Any) -> _Chain | None:
    """The row's own source chain via the host factory, or ``None`` when the
    descriptor cannot be resolved (the caller falls back)."""
    from ...schemas.source import SourceDescriptor

    try:
        typed = await registry.get_descriptor_typed(source_id, family="source")
        if typed is None:
            return None
        sd = SourceDescriptor.model_validate(typed, strict=False)
        stage = factory(sd)
        if inspect.isawaitable(stage):
            stage = await stage
    except Exception as exc:
        logger.warning(
            "reenrich_pending.descriptor_chain_failed source_id=%s err=%s", source_id, exc,
        )
        return None
    return _Chain(
        stage,
        scope_geo=list(getattr(sd.scope, "geo", []) or []),
        via="descriptor",
        state=getattr(stage, "enrichment_state", None),
    )


class _ChainResolver:
    """Per-tick, per-source chain cache: one registry fetch per source."""

    def __init__(self, *, registry: Any, factory: Any, nlp_client: Any) -> None:
        self._registry = registry
        self._factory = factory
        self._nlp = nlp_client
        self._by_source: dict[str, _Chain | None] = {}
        self._fallback: _Chain | None = None

    async def get(self, source_id: str) -> _Chain | None:
        if source_id in self._by_source:
            return self._by_source[source_id]
        chain = None
        if self._registry is not None and self._factory is not None:
            chain = await _descriptor_chain(source_id, self._registry, self._factory)
        if chain is None and self._nlp is not None:
            if self._fallback is None:
                self._fallback = _build_fallback_chain(self._nlp)
            chain = self._fallback
        self._by_source[source_id] = chain
        return chain


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _signal_from_row(r: Mapping[str, Any]) -> Signal:
    payload = dict(_as_dict(r["payload"]))
    return Signal(
        signal_id=r["id"],
        source_id=r["source_id"] or SUB_HANDLER_NAME,
        language=r["language"],
        language_hint=r["language"],
        geo=list(r["geo"] or []),
        tags=list(r["tags"] or []),
        entity_classes=list(r["entity_classes"] or []),
        payload=payload,
        canonical_url=r["canonical_url"],
        content_hash=r["content_hash"] or "",
    )


class _NoConfig(BaseModel):
    """The baseline reads no handler config; SourceContext requires a model."""


def _source_ctx(source_id: str, chain: _Chain) -> SourceContext:
    return SourceContext(
        target_id=source_id,
        target_version="",
        source_id=source_id,
        config=_NoConfig(),
        state_store=InMemoryStateStore(),
        scope_geo=list(chain.scope_geo),
        logger=logger,
    )


async def _enrich_one(r: Mapping[str, Any], chain: _Chain) -> tuple[Signal | None, bool]:
    """Run the chain over one row. Returns ``(signal or None on failure,
    timed_out)``."""
    try:
        signal = _signal_from_row(r)
    except Exception as exc:
        logger.warning("reenrich_pending.row_unreadable signal_id=%s err=%s", r["id"], exc)
        return None, False
    before = chain.failure_marks()
    try:
        out = await asyncio.wait_for(
            run_baseline(
                signal, _source_ctx(signal.source_id, chain),
                media="reference", enrichment_stage=chain.stage,
            ),
            timeout=REENRICH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("reenrich_pending.timeout signal_id=%s", r["id"])
        return None, True
    except Exception as exc:
        logger.warning("reenrich_pending.chain_error signal_id=%s err=%s", r["id"], exc)
        return None, False
    if out is None or chain.failure_marks() > before:
        return None, False
    return out, False


def _changed_payload(original: Mapping[str, Any], enriched: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: v for k, v in enriched.items()
        if k not in (PENDING_KEY, ATTEMPTS_KEY) and original.get(k) != v
    }


async def _sweep_batch(pool: Any, *, resolver: _ChainResolver, pending_batch: int) -> dict[str, int]:
    counters = _zero_counters()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_BATCH_SQL, pending_batch, PENDING_MAX_ATTEMPTS)
    counters["examined"] = len(rows)

    failed_ids: list[Any] = []
    for r in rows:
        chain = await resolver.get(r["source_id"] or "")
        if chain is None:
            failed_ids.append(r["id"])
            continue
        original = _as_dict(r["payload"])
        out, timed_out = await _enrich_one(r, chain)
        if out is None:
            failed_ids.append(r["id"])
            if timed_out and counters["enriched"] == 0:
                logger.warning(
                    "reenrich_pending.nlp_outage — the first row timed out with none "
                    "enriched this tick; stopping (rows keep their marker)",
                )
                break
            continue
        changed = _changed_payload(original, out.payload if isinstance(out.payload, dict) else {})
        async with pool.acquire() as conn:
            await conn.execute(
                _WRITE_ENRICHED_SQL, r["id"], json.dumps(changed, default=str),
                out.language, list(out.geo), list(out.tags), list(out.entity_classes),
            )
        counters["enriched"] += 1
        counters["fallback_chain"] += chain.via == "fallback"

    counters["failed"] = len(failed_ids)
    async with pool.acquire() as conn:
        if failed_ids and counters["enriched"] > 0:
            # The plane enriched other rows this tick, so these rows are the
            # problem: count the attempt (a row at the cap leaves the scan).
            await conn.execute(_BUMP_ATTEMPTS_SQL, failed_ids)
        elif failed_ids:
            logger.warning(
                "reenrich_pending.no_success failed=%d — nothing enriched this tick; "
                "no attempt counted (a probable NLP-plane outage)", len(failed_ids),
            )
        counters["still_pending"] = int(await conn.fetchval(_COUNT_PENDING_SQL) or 0)
    return counters


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    title = (
        f"Pending-enrichment sweep: enriched {counters.get('enriched', 0)} of "
        f"{counters.get('examined', 0)} examined, {counters.get('failed', 0)} failed, "
        f"{counters.get('still_pending', 0)} still pending"
    )
    tags = ["deterministic", SUB_HANDLER_NAME]
    if counters.get("enriched", 0):
        tags.append("reenriched")
    return FindingPayload(
        title=title[:2048],
        body="\n".join(f"{k}={v}" for k, v in counters.items())[:65536],
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
    """Sub-handler entry point — see the module docstring. ``deps is None``
    (unit-test path) yields a zeroed run; usage is always zero (no LLM)."""
    counters = _zero_counters()
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        resolver = _ChainResolver(
            registry=_extra(deps, REGISTRY_DEPS_EXTRA_KEY),
            factory=_extra(deps, STAGE_FACTORY_DEPS_EXTRA_KEY),
            nlp_client=_extra(deps, NLP_DEPS_EXTRA_KEY),
        )
        if resolver._factory is None and resolver._nlp is None:
            counters["skipped_no_chain"] = 1
            logger.warning(
                "reenrich_pending.no_chain — neither the source-enrichment factory nor "
                "an NLP client is on deps.extras; rows left pending this tick",
            )
        else:
            pending_batch = int(options.get("pending_batch", _DEFAULT_PENDING_BATCH))
            try:
                counters = await _sweep_batch(
                    pool, resolver=resolver, pending_batch=pending_batch,
                )
            except Exception as exc:
                logger.warning("reenrich_pending.failed err=%s", exc)
    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "NLP_DEPS_EXTRA_KEY",
    "PENDING_MAX_ATTEMPTS",
    "REGISTRY_DEPS_EXTRA_KEY",
    "STAGE_FACTORY_DEPS_EXTRA_KEY",
    "SUB_HANDLER_NAME",
    "handle",
]
