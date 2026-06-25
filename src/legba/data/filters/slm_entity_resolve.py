# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SLM entity-resolution filter (L-202).

Stream-resident migration of the legacy
:mod:`legba.subconscious.entity_resolution` cycle-phase caller. The legacy
module ran on a timer, pulled ambiguous ``signal_entity_links`` rows out
of Postgres, fetched candidate ``entity_profiles`` for each, asked the
SLM to pick the best match or mark as new, then mutated link confidence
and inserted new entity rows.

In the stream-resident world, the same semantic work is done inline on
a per-signal basis:

  * The handler reads entity mentions off ``signal.payload["entities"]``
    (populated by the upstream :class:`NERMultilingualHandler`, L-154).
  * For each mention below a confidence floor, it pulls candidate
    matches from an injected :class:`EntityCandidatePort` (typically
    backed by a Postgres trigram-similarity query against the registry).
  * For each ambiguous mention it asks the SLM which candidate matches,
    or whether the mention is a new entity.
  * Verdicts are stamped back onto the same ``entities`` array
    (per-entry ``resolved_entity_id`` / ``is_new_entity`` /
    ``resolution_confidence`` / ``resolution_reasoning``). The handler
    does **not** mutate Postgres — substrate writes happen at the
    pipeline's output stage; this filter is enrichment-only.

LLM-bearing
-----------

Backed by the L-120 ``legba.data.stack.llm`` provider stack via the
:class:`SLMPort` structural-typing port (shared with the sibling
:class:`SLMClassificationRefineHandler`). Default config targets
gpt-oss-120b / vLLM per the L-202 brief; the concrete provider is wired
in by the runtime at activation.

Idempotency (L-102 §3)
----------------------

A signal where every entity entry already carries
``resolved_entity_id`` or ``is_new_entity`` is a no-op (unless
``force_resolve`` is set). Idempotency is per-entry — partially-resolved
batches re-resolve only the remaining mentions.

Failure semantics (L-102 §7)
----------------------------

  * Candidate fetch raises → that entry stays unresolved; other entries
    still attempt resolution.
  * SLM raises → entry stays unresolved; counters track the drop.
  * Provider not configured → pass-through (handler is degraded).
  * Never drops the signal from the stream.

This module depends only on the structural-typing surfaces in
``_contract.py``, the loose :class:`SLMPort` / :class:`EntityCandidatePort`
Protocols below, and reuses the helper utilities of the sibling
:mod:`slm_classification_refine` module (JSON parsing, await helper) to
avoid duplication.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import (
    Any,
    ClassVar,
    Mapping,
    Protocol,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field

from ..sources._contract import Signal
from ._contract import FilterContext, FilterHealth
from .slm_classification_refine import (
    ChatSLMPort,
    SLMPort,
    _maybe_await,
    _parse_json_loose,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


SLM_ENTITY_RESOLVE_KIND: str = "slm_entity_resolve"
SLM_ENTITY_RESOLVE_FAMILY: str = "filter"
SLM_ENTITY_RESOLVE_SCHEMA_VERSION: str = (
    "legba/filter.slm_entity_resolve/1-0-0"
)
SLM_ENTITY_RESOLVE_HANDLER_VERSION: str = "0.1.0"

#: Payload key the upstream NER filter writes mentions to.
ENTITIES_PAYLOAD_KEY: str = "entities"

#: Per-entity fields stamped by this handler.
RESOLVED_ENTITY_ID_KEY: str = "resolved_entity_id"
IS_NEW_ENTITY_KEY: str = "is_new_entity"
RESOLUTION_CONFIDENCE_KEY: str = "resolution_confidence"
RESOLUTION_REASONING_KEY: str = "resolution_reasoning"

#: System prompt — ported verbatim from
#: ``legba.subconscious.prompts.ENTITY_RESOLUTION_SYSTEM``.
SYSTEM_PROMPT: str = """\
You are an entity resolution specialist for an intelligence analysis system.

Your task: given an extracted entity name and its context, determine \
whether it matches an existing entity in the knowledge base or is a new \
entity.

Confidence calibration:
- 0.95+: Name, type, and context ALL clearly match a single candidate.
- 0.80-0.94: Strong match but minor ambiguity.
- 0.60-0.79: Probable match but real uncertainty.
- 0.40-0.59: Uncertain. Multiple candidates are roughly equally likely.
- Below 0.40: Very uncertain. Mark as new entity rather than guessing.

Be conservative. A false match is worse than marking a genuine entity \
as new — the conscious agent can merge later.

Rules:
- Match on semantic identity, not just string similarity.
- Consider entity type — don't match a person to an organization.
- If no candidate matches with reasonable confidence, mark as new entity.
- Output MUST be valid JSON matching the schema below.
"""


# JSON Schema the SLM is asked to emit per entity.
ENTITY_RESOLUTION_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_name": {"type": "string"},
        "matched_entity_id": {"type": ["string", "null"]},
        "is_new_entity": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reasoning": {"type": "string"},
    },
    "required": ["entity_name", "is_new_entity", "confidence"],
}


# ---------------------------------------------------------------------------
# EntityCandidatePort — sub-connection port for the registry-backed
# candidate-lookup query.
# ---------------------------------------------------------------------------


@runtime_checkable
class EntityCandidatePort(Protocol):
    """Structural-typing port for fetching candidate matches.

    Production binding will be backed by a Postgres trigram-similarity
    query against ``entity_profiles`` (mirroring the legacy
    :func:`legba.subconscious.entity_resolution.fetch_entity_candidates`).
    Tests inject a deterministic in-memory stub.
    """

    async def fetch_candidates(  # pragma: no cover - protocol surface
        self,
        *,
        entity_name: str,
        entity_type: str,
        limit: int = 10,
    ) -> list[Mapping[str, Any]]: ...


class InMemoryCandidatePort:
    """Process-local :class:`EntityCandidatePort` for tests and dev runs.

    Constructed from a static list of candidate dicts. ``fetch_candidates``
    returns the top-N matches ranked by a cheap substring score so unit
    tests can exercise the resolution flow without a live Postgres.

    Not crash-safe. Not production-quality. The runtime substitutes the
    real Postgres-backed port at activation.
    """

    def __init__(self, candidates: list[Mapping[str, Any]]) -> None:
        self._candidates: list[dict[str, Any]] = [
            dict(c) for c in candidates
        ]
        self.calls: list[tuple[str, str, int]] = []

    async def fetch_candidates(
        self,
        *,
        entity_name: str,
        entity_type: str,
        limit: int = 10,
    ) -> list[Mapping[str, Any]]:
        self.calls.append((entity_name, entity_type, limit))
        name_lc = entity_name.lower()

        def _score(c: Mapping[str, Any]) -> float:
            cname = str(c.get("canonical_name", "")).lower()
            if not cname:
                return 0.0
            if cname == name_lc:
                return 1.0
            if name_lc in cname or cname in name_lc:
                return 0.6
            # crude token overlap
            a = set(cname.split())
            b = set(name_lc.split())
            if not a or not b:
                return 0.0
            return len(a & b) / len(a | b)

        eligible = [
            c for c in self._candidates
            if entity_type == "other"
            or c.get("entity_type") == entity_type
        ]
        eligible.sort(key=_score, reverse=True)
        return eligible[:limit]


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class EntityResolutionVerdict(BaseModel):
    """Parsed SLM verdict for one entity mention.

    Mirrors :class:`legba.subconscious.schemas.EntityResolutionVerdict`
    but lives here so the legacy package can be deleted in L-205.
    """

    model_config = ConfigDict(extra="ignore")

    entity_name: str
    matched_entity_id: str | None = None
    is_new_entity: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class SLMEntityResolveConfig(BaseModel):
    """Pydantic config schema for :class:`SLMEntityResolveHandler`."""

    model_config = ConfigDict(extra="forbid")

    #: Skip mentions whose upstream NER confidence is already above this
    #: threshold. Mirrors the legacy ``< 0.6`` ambiguity floor.
    upstream_confidence_floor: float = Field(default=0.6, ge=0.0, le=1.0)

    #: Number of candidate matches to surface to the SLM.
    candidate_limit: int = Field(default=10, ge=1, le=100)

    #: Minimum trigram (or substring) similarity for the SLM to be
    #: trusted on a high-confidence match. Cross-validation downgrade
    #: threshold — mirrors the legacy ``< 0.3`` warning gate.
    cross_validation_floor: float = Field(default=0.3, ge=0.0, le=1.0)

    #: Confidence below which a verdict is recorded but not applied
    #: (the entry stays unresolved but ``resolution_reasoning`` records
    #: why).
    min_apply_confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    #: Maximum number of mentions per signal to process. Caps SLM cost
    #: on signals with very large entity lists.
    max_entities_per_signal: int = Field(default=20, ge=1, le=500)

    #: When ``True``, re-resolve even when the mention is already marked
    #: resolved. Default ``False`` (idempotent — matches L-102 §3).
    force_resolve: bool = False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class SLMEntityResolveHandler:
    """Stream-resident SLM entity-resolution handler.

    Conforms to the L-102 §3 ``StreamHandler`` Protocol — see
    :mod:`legba.data.filters._contract`. Lives downstream of the
    :class:`NERMultilingualHandler` (L-154); reads
    ``signal.payload["entities"]`` and stamps per-entry resolution
    annotations.
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = SLM_ENTITY_RESOLVE_KIND
    family: ClassVar[str] = SLM_ENTITY_RESOLVE_FAMILY
    schema_version: ClassVar[str] = SLM_ENTITY_RESOLVE_SCHEMA_VERSION
    handler_version: ClassVar[str] = SLM_ENTITY_RESOLVE_HANDLER_VERSION
    config_schema: ClassVar[type[BaseModel]] = SLMEntityResolveConfig

    idempotent: ClassVar[bool] = True

    output_contract: ClassVar[Mapping[str, type]] = {
        f"payload.{ENTITIES_PAYLOAD_KEY}": list,
        f"payload.{ENTITIES_PAYLOAD_KEY}[].{RESOLVED_ENTITY_ID_KEY}": object,
        f"payload.{ENTITIES_PAYLOAD_KEY}[].{IS_NEW_ENTITY_KEY}": bool,
        f"payload.{ENTITIES_PAYLOAD_KEY}[].{RESOLUTION_CONFIDENCE_KEY}": float,
    }

    def __init__(
        self,
        config: SLMEntityResolveConfig,
        *,
        slm: SLMPort | ChatSLMPort | None = None,
        candidates: EntityCandidatePort | None = None,
    ) -> None:
        self._config: SLMEntityResolveConfig = config
        self._slm: SLMPort | ChatSLMPort | None = slm
        self._candidates: EntityCandidatePort | None = candidates

        self._signals_in: int = 0
        self._signals_out: int = 0
        self._signals_dropped: int = 0
        self._entities_seen: int = 0
        self._entities_resolved: int = 0
        self._entities_marked_new: int = 0
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def config(self) -> SLMEntityResolveConfig:
        return self._config

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_configure(
        self,
        ctx: FilterContext | None = None,
        *,
        slm: SLMPort | ChatSLMPort | None = None,
        candidates: EntityCandidatePort | None = None,
    ) -> None:
        if slm is not None:
            self._slm = slm
        if candidates is not None:
            self._candidates = candidates

    async def on_activate(self, ctx: FilterContext | None = None) -> None:
        return None

    async def on_pause(self, ctx: FilterContext | None = None) -> None:
        return None

    async def on_resume(self, ctx: FilterContext | None = None) -> None:
        return None

    async def on_retire(self, ctx: FilterContext | None = None) -> None:
        self._slm = None
        self._candidates = None

    # ------------------------------------------------------------------
    # transform
    # ------------------------------------------------------------------

    async def transform(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> Signal | None:
        """Resolve ambiguous entities on the signal payload in place.

        Never drops a signal.
        """
        self._signals_in += 1

        entities = signal.payload.get(ENTITIES_PAYLOAD_KEY)
        if not isinstance(entities, list) or not entities:
            self._signals_out += 1
            return signal

        # Pass-through when provider isn't wired — handler is degraded.
        if self._slm is None or self._candidates is None:
            ctx.logger.debug(
                "slm_entity_resolve.no_provider signal_id=%s slm=%s cands=%s",
                signal.signal_id,
                self._slm is not None, self._candidates is not None,
            )
            self._signals_out += 1
            return signal

        new_entities: list[Any] = []
        processed = 0
        for entry in entities:
            new_entities.append(entry)
            if processed >= self._config.max_entities_per_signal:
                continue
            if not isinstance(entry, dict):
                continue
            if not self._needs_resolution(entry):
                continue

            self._entities_seen += 1
            try:
                verdict = await self._resolve_entry(entry, ctx=ctx)
            except Exception as exc:                              # noqa: BLE001
                self._last_error = f"resolve_failed: {exc!s}"
                ctx.logger.warning(
                    "slm_entity_resolve.entry_failed signal_id=%s name=%s err=%s",
                    signal.signal_id,
                    entry.get("entity_name") or entry.get("text"),
                    exc,
                )
                self._signals_dropped += 1
                continue
            if verdict is None:
                continue
            applied = self._apply_verdict_to_entry(entry, verdict)
            processed += 1
            if not applied:
                continue
            if verdict.is_new_entity:
                self._entities_marked_new += 1
            elif verdict.matched_entity_id is not None:
                self._entities_resolved += 1

        # Replace the payload entities array (preserves any
        # in-place-mutated dicts since we kept the same references).
        new_payload = dict(signal.payload)
        new_payload[ENTITIES_PAYLOAD_KEY] = new_entities
        out_signal = signal.model_copy(update={"payload": new_payload})
        self._signals_out += 1
        if processed:
            self._last_success_at = _now()
        return out_signal

    # ------------------------------------------------------------------
    # health_check
    # ------------------------------------------------------------------

    async def health_check(
        self, ctx: FilterContext | None = None,
    ) -> FilterHealth:
        provider_wired = self._slm is not None and self._candidates is not None
        state = "healthy" if provider_wired else "degraded"
        if self._last_error:
            state = "degraded"
        return FilterHealth(
            state=state,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            signals_in_24h=self._signals_in,
            signals_out_24h=self._signals_out,
            signals_dropped_24h=self._signals_dropped,
            detail={
                "slm_wired": self._slm is not None,
                "candidates_wired": self._candidates is not None,
                "upstream_confidence_floor": (
                    self._config.upstream_confidence_floor
                ),
                "min_apply_confidence": self._config.min_apply_confidence,
                "entities_resolved_total": self._entities_resolved,
                "entities_marked_new_total": self._entities_marked_new,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _needs_resolution(self, entry: Mapping[str, Any]) -> bool:
        """Decide whether one entity entry requires SLM resolution."""
        if not self._config.force_resolve:
            if entry.get(RESOLVED_ENTITY_ID_KEY) is not None:
                return False
            if entry.get(IS_NEW_ENTITY_KEY):
                return False

        name = entry.get("entity_name") or entry.get("text") or entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return False

        upstream_conf = entry.get("confidence")
        if isinstance(upstream_conf, (int, float)):
            if float(upstream_conf) >= self._config.upstream_confidence_floor:
                return False
        return True

    async def _resolve_entry(
        self,
        entry: Mapping[str, Any],
        *,
        ctx: FilterContext,
    ) -> EntityResolutionVerdict | None:
        """Look up candidates and call the SLM for one entity entry."""
        assert self._candidates is not None
        assert self._slm is not None

        entity_name = str(entry.get("entity_name") or entry.get("text") or "").strip()
        if not entity_name:
            return None
        entity_type = str(entry.get("entity_type") or "other")
        context = str(entry.get("context") or entry.get("snippet") or "")

        candidates = await self._candidates.fetch_candidates(
            entity_name=entity_name,
            entity_type=entity_type,
            limit=self._config.candidate_limit,
        )

        candidate_block: list[dict[str, Any]] = []
        candidate_trgm: dict[str, float] = {}
        for c in candidates:
            cid = str(c.get("entity_id") or c.get("id") or "")
            if not cid:
                continue
            candidate_block.append({
                "entity_id": cid,
                "canonical_name": c.get("canonical_name", ""),
                "entity_type": c.get("entity_type", ""),
                "description": c.get("description", ""),
            })
            sim = c.get("trgm_similarity")
            if isinstance(sim, (int, float)):
                candidate_trgm[cid] = float(sim)

        verdict = await self._call_slm(
            entity_name=entity_name,
            entity_type=entity_type,
            context=context,
            candidates=candidate_block,
        )

        # Cross-validation downgrade — mirror the legacy behaviour.
        if (
            verdict.matched_entity_id
            and not verdict.is_new_entity
            and verdict.confidence > 0.8
        ):
            sim = candidate_trgm.get(verdict.matched_entity_id, 0.0)
            if sim < self._config.cross_validation_floor:
                ctx.logger.warning(
                    "slm_entity_resolve.cross_validation_downgrade "
                    "name=%s matched=%s trgm=%.3f floor=%.3f",
                    entity_name, verdict.matched_entity_id, sim,
                    self._config.cross_validation_floor,
                )
                # mutate the parsed model — pydantic v2 allows attribute
                # assignment unless model_config is frozen, and our model
                # is not frozen.
                verdict.confidence = 0.5
        return verdict

    async def _call_slm(
        self,
        *,
        entity_name: str,
        entity_type: str,
        context: str,
        candidates: list[dict[str, Any]],
    ) -> EntityResolutionVerdict:
        """Invoke the SLM port and parse the verdict."""
        prompt = (
            "Resolve the following entity. Pick the best match from the "
            "candidates, or mark as new.\n\n"
            f"Entity name: {entity_name}\n"
            f"Context: {context}\n"
            f"Entity type (if known): {entity_type}\n\n"
            f"Candidates:\n{json.dumps(candidates, indent=2)}\n\n"
            "Output JSON schema:\n"
            f"{json.dumps(ENTITY_RESOLUTION_VERDICT_SCHEMA, indent=2)}"
        )

        complete = getattr(self._slm, "complete", None)
        if complete is not None and callable(complete):
            result = await _maybe_await(
                complete(
                    prompt=prompt,
                    system=SYSTEM_PROMPT,
                    json_schema=ENTITY_RESOLUTION_VERDICT_SCHEMA,
                )
            )
            if not isinstance(result, Mapping):
                raise RuntimeError(
                    f"slm.complete returned non-mapping: {type(result).__name__}"
                )
            payload = dict(result)
            payload.setdefault("entity_name", entity_name)
            return EntityResolutionVerdict.model_validate(payload)

        chat_complete = getattr(self._slm, "chat_complete", None)
        if chat_complete is not None and callable(chat_complete):
            response = await _maybe_await(
                chat_complete(
                    [{"role": "user", "content": prompt}],
                    system=SYSTEM_PROMPT,
                )
            )
            content = getattr(response, "content", None)
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("slm.chat_complete returned empty content")
            parsed = _parse_json_loose(content)
            if parsed is None:
                raise RuntimeError(
                    f"slm.chat_complete returned unparseable content: "
                    f"{content[:200]!r}"
                )
            parsed.setdefault("entity_name", entity_name)
            return EntityResolutionVerdict.model_validate(parsed)

        raise RuntimeError(
            "wired SLM port exposes neither complete() nor chat_complete()"
        )

    def _apply_verdict_to_entry(
        self,
        entry: dict[str, Any],
        verdict: EntityResolutionVerdict,
    ) -> bool:
        """Mutate ``entry`` in place with the resolution annotation.

        Sub-confidence verdicts are recorded with a ``None``
        ``resolved_entity_id`` and ``is_new_entity=False`` so downstream
        consumers can tell they were considered but not applied.

        Returns ``True`` when the verdict's decision (match / new) was
        actually applied to the entry; ``False`` when the verdict was
        recorded but skipped because it fell below the apply floor.
        """
        below_floor = verdict.confidence < self._config.min_apply_confidence
        entry[RESOLUTION_CONFIDENCE_KEY] = float(verdict.confidence)
        entry[RESOLUTION_REASONING_KEY] = verdict.reasoning
        if below_floor:
            entry[RESOLVED_ENTITY_ID_KEY] = None
            entry[IS_NEW_ENTITY_KEY] = False
            return False
        if verdict.is_new_entity:
            entry[RESOLVED_ENTITY_ID_KEY] = None
            entry[IS_NEW_ENTITY_KEY] = True
            return True
        entry[RESOLVED_ENTITY_ID_KEY] = verdict.matched_entity_id
        entry[IS_NEW_ENTITY_KEY] = False
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "ENTITIES_PAYLOAD_KEY",
    "ENTITY_RESOLUTION_VERDICT_SCHEMA",
    "EntityCandidatePort",
    "EntityResolutionVerdict",
    "IS_NEW_ENTITY_KEY",
    "InMemoryCandidatePort",
    "RESOLUTION_CONFIDENCE_KEY",
    "RESOLUTION_REASONING_KEY",
    "RESOLVED_ENTITY_ID_KEY",
    "SLMEntityResolveConfig",
    "SLMEntityResolveHandler",
    "SLM_ENTITY_RESOLVE_FAMILY",
    "SLM_ENTITY_RESOLVE_HANDLER_VERSION",
    "SLM_ENTITY_RESOLVE_KIND",
    "SLM_ENTITY_RESOLVE_SCHEMA_VERSION",
    "SYSTEM_PROMPT",
]
