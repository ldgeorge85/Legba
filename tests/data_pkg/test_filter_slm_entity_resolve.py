# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for :class:`legba.data.filters.SLMEntityResolveHandler` (L-202).

No external services required. Both the SLM port and the candidate-port
are deterministic stubs. Cases covered:

  * Pass-through when no entities array exists.
  * Pass-through when provider / candidates not wired (degraded).
  * Upstream-confidence gate skips already-confident mentions.
  * Idempotency on previously-resolved entries.
  * ``force_resolve`` overrides idempotency.
  * Verdict applied — entry annotated with ``resolved_entity_id``,
    ``is_new_entity``, confidence, reasoning.
  * Cross-validation downgrade — high-confidence match with low trigram
    similarity gets dropped to 0.5.
  * New-entity verdict path.
  * Sub-confidence verdict recorded but not applied.
  * SLM raises → partial-batch results recorded.
  * Handler ClassVar shape matches L-102 §1 / §3.
  * ``max_entities_per_signal`` caps SLM calls.
  * Both ``complete`` and ``chat_complete`` dispatch shapes work.
  * Candidate-port stub semantics (substring scoring + entity-type filter).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import pytest

from legba.data.filters import (
    FilterContext,
    InMemoryCandidatePort,
    SLMEntityResolveConfig,
    SLMEntityResolveHandler,
)
from legba.data.filters._contract import FilterHealth, StreamHandler
from legba.data.filters.slm_entity_resolve import (
    ENTITIES_PAYLOAD_KEY,
    IS_NEW_ENTITY_KEY,
    RESOLUTION_CONFIDENCE_KEY,
    RESOLUTION_REASONING_KEY,
    RESOLVED_ENTITY_ID_KEY,
    SLM_ENTITY_RESOLVE_KIND,
    SLM_ENTITY_RESOLVE_SCHEMA_VERSION,
    SYSTEM_PROMPT,
    EntityResolutionVerdict,
)
from legba.data.sources._contract import Signal


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubSLM:
    """Implements the legacy ``complete`` shape. ``verdicts`` is keyed by
    entity name so the same handler can be tested across multiple entries."""

    def __init__(
        self,
        verdicts: dict[str, dict[str, Any] | Exception] | None = None,
        default: dict[str, Any] | Exception | None = None,
    ) -> None:
        self._verdicts = verdicts or {}
        self._default = default
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({
            "prompt": prompt, "system": system, "schema": json_schema,
        })
        # Pick verdict by inspecting the prompt for the entity name
        for name, verdict in self._verdicts.items():
            if f"Entity name: {name}\n" in prompt:
                if isinstance(verdict, Exception):
                    raise verdict
                return verdict
        if self._default is not None:
            if isinstance(self._default, Exception):
                raise self._default
            return self._default
        raise RuntimeError("no matching verdict configured for prompt")


class _StubChatResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubChatSLM:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[Any] = []

    async def chat_complete(
        self,
        messages: list[Any],
        *,
        system: str | None = None,
        **kwargs: Any,
    ) -> _StubChatResponse:
        self.calls.append({"messages": messages, "system": system})
        return _StubChatResponse(self._content)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signal(entities: list[Mapping[str, Any]], **extra_payload: Any) -> Signal:
    payload = {ENTITIES_PAYLOAD_KEY: entities, **extra_payload}
    return Signal(
        source_id="test_source",
        payload=payload,
        content_hash="deadbeef",
    )


def _ctx(**overrides: Any) -> FilterContext:
    defaults = {
        "target_id": "test_target",
        "target_version": "v1",
        "filter_id": "slm_entity_resolve@1",
        "logger": logging.getLogger("test.slm_entity_resolve"),
    }
    defaults.update(overrides)
    return FilterContext(**defaults)


def _config(**overrides: Any) -> SLMEntityResolveConfig:
    defaults: dict[str, Any] = {
        "upstream_confidence_floor": 0.6,
        "candidate_limit": 10,
        "cross_validation_floor": 0.3,
        "min_apply_confidence": 0.5,
        "max_entities_per_signal": 20,
        "force_resolve": False,
    }
    defaults.update(overrides)
    return SLMEntityResolveConfig(**defaults)


def _candidates_for(name: str, entity_type: str = "person") -> InMemoryCandidatePort:
    return InMemoryCandidatePort([
        {
            "entity_id": "ent-001",
            "canonical_name": name,
            "entity_type": entity_type,
            "description": "First exact-name candidate",
            # High trigram similarity → cross-validation downgrade doesn't fire
            "trgm_similarity": 0.95,
        },
        {
            "entity_id": "ent-002",
            "canonical_name": f"{name} Junior",
            "entity_type": entity_type,
            "description": "Second close-name candidate",
            "trgm_similarity": 0.70,
        },
    ])


# ---------------------------------------------------------------------------
# ClassVar / Protocol shape
# ---------------------------------------------------------------------------


def test_classvars_match_l102_contract() -> None:
    handler = SLMEntityResolveHandler(_config())
    assert handler.kind == SLM_ENTITY_RESOLVE_KIND == "slm_entity_resolve"
    assert handler.family == "filter"
    assert handler.schema_version == SLM_ENTITY_RESOLVE_SCHEMA_VERSION
    assert handler.config_schema is SLMEntityResolveConfig
    assert handler.idempotent is True


def test_satisfies_streamhandler_protocol() -> None:
    handler = SLMEntityResolveHandler(_config())
    assert isinstance(handler, StreamHandler)


def test_output_contract_declares_payload_keys() -> None:
    contract = SLMEntityResolveHandler.output_contract
    assert f"payload.{ENTITIES_PAYLOAD_KEY}" in contract


# ---------------------------------------------------------------------------
# transform — gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passthrough_when_no_entities() -> None:
    slm = _StubSLM(default={"entity_name": "x", "is_new_entity": True, "confidence": 0.9})
    cands = _candidates_for("X")
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    sig = Signal(
        source_id="s",
        payload={"text": "hello"}, content_hash="abc",
    )
    out = await handler.transform(sig, _ctx())
    assert out is sig
    assert slm.calls == []


@pytest.mark.asyncio
async def test_passthrough_when_provider_unwired() -> None:
    handler = SLMEntityResolveHandler(_config(), slm=None, candidates=None)
    sig = _signal([{"entity_name": "Putin", "entity_type": "person", "confidence": 0.4}])
    out = await handler.transform(sig, _ctx())
    assert out is not None
    assert RESOLVED_ENTITY_ID_KEY not in out.payload[ENTITIES_PAYLOAD_KEY][0]


@pytest.mark.asyncio
async def test_high_upstream_confidence_skipped() -> None:
    """upstream_confidence_floor gate — already-confident mentions are not refined."""
    slm = _StubSLM()
    cands = _candidates_for("Putin")
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    sig = _signal([{
        "entity_name": "Putin", "entity_type": "person", "confidence": 0.95,
    }])
    await handler.transform(sig, _ctx())
    assert slm.calls == []


@pytest.mark.asyncio
async def test_already_resolved_entry_skipped() -> None:
    slm = _StubSLM()
    cands = _candidates_for("Putin")
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    sig = _signal([{
        "entity_name": "Putin",
        "entity_type": "person",
        "confidence": 0.3,
        RESOLVED_ENTITY_ID_KEY: "ent-xyz",
    }])
    await handler.transform(sig, _ctx())
    assert slm.calls == []


@pytest.mark.asyncio
async def test_force_resolve_overrides_idempotency() -> None:
    slm = _StubSLM(default={
        "entity_name": "Putin", "matched_entity_id": "ent-001",
        "is_new_entity": False, "confidence": 0.92, "reasoning": "exact match",
    })
    cands = _candidates_for("Putin")
    handler = SLMEntityResolveHandler(
        _config(force_resolve=True), slm=slm, candidates=cands,
    )
    sig = _signal([{
        "entity_name": "Putin",
        "entity_type": "person",
        "confidence": 0.3,
        RESOLVED_ENTITY_ID_KEY: "ent-old",
    }])
    out = await handler.transform(sig, _ctx())
    assert len(slm.calls) == 1
    entry = out.payload[ENTITIES_PAYLOAD_KEY][0]
    assert entry[RESOLVED_ENTITY_ID_KEY] == "ent-001"


@pytest.mark.asyncio
async def test_missing_name_entry_skipped() -> None:
    slm = _StubSLM()
    cands = _candidates_for("Putin")
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    sig = _signal([{
        "entity_type": "person", "confidence": 0.3,  # no entity_name
    }])
    await handler.transform(sig, _ctx())
    assert slm.calls == []


# ---------------------------------------------------------------------------
# transform — verdict application
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_verdict_applied() -> None:
    slm = _StubSLM(default={
        "entity_name": "Putin", "matched_entity_id": "ent-001",
        "is_new_entity": False, "confidence": 0.92,
        "reasoning": "exact name match, person type",
    })
    cands = _candidates_for("Putin")
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    sig = _signal([{
        "entity_name": "Putin", "entity_type": "person", "confidence": 0.3,
    }])
    out = await handler.transform(sig, _ctx())
    entry = out.payload[ENTITIES_PAYLOAD_KEY][0]
    assert entry[RESOLVED_ENTITY_ID_KEY] == "ent-001"
    assert entry[IS_NEW_ENTITY_KEY] is False
    assert entry[RESOLUTION_CONFIDENCE_KEY] == 0.92
    assert "match" in entry[RESOLUTION_REASONING_KEY]
    assert handler._entities_resolved == 1


@pytest.mark.asyncio
async def test_new_entity_verdict_applied() -> None:
    slm = _StubSLM(default={
        "entity_name": "Putin", "matched_entity_id": None,
        "is_new_entity": True, "confidence": 0.87,
        "reasoning": "no close candidate",
    })
    cands = _candidates_for("Putin")
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    sig = _signal([{
        "entity_name": "Putin", "entity_type": "person", "confidence": 0.3,
    }])
    out = await handler.transform(sig, _ctx())
    entry = out.payload[ENTITIES_PAYLOAD_KEY][0]
    assert entry[RESOLVED_ENTITY_ID_KEY] is None
    assert entry[IS_NEW_ENTITY_KEY] is True
    assert entry[RESOLUTION_CONFIDENCE_KEY] == 0.87
    assert handler._entities_marked_new == 1


@pytest.mark.asyncio
async def test_low_confidence_verdict_records_but_doesnt_apply() -> None:
    slm = _StubSLM(default={
        "entity_name": "Putin", "matched_entity_id": "ent-001",
        "is_new_entity": False, "confidence": 0.30,
        "reasoning": "uncertain",
    })
    cands = _candidates_for("Putin")
    handler = SLMEntityResolveHandler(
        _config(min_apply_confidence=0.5), slm=slm, candidates=cands,
    )
    sig = _signal([{
        "entity_name": "Putin", "entity_type": "person", "confidence": 0.3,
    }])
    out = await handler.transform(sig, _ctx())
    entry = out.payload[ENTITIES_PAYLOAD_KEY][0]
    assert entry[RESOLVED_ENTITY_ID_KEY] is None
    assert entry[IS_NEW_ENTITY_KEY] is False
    assert entry[RESOLUTION_CONFIDENCE_KEY] == 0.30
    # not counted as resolved
    assert handler._entities_resolved == 0


@pytest.mark.asyncio
async def test_cross_validation_downgrade_low_trigram() -> None:
    """High-confidence SLM verdict + low trigram similarity → downgrade to 0.5."""
    slm = _StubSLM(default={
        "entity_name": "Putin", "matched_entity_id": "ent-zzz",
        "is_new_entity": False, "confidence": 0.95,
        "reasoning": "I think it matches",
    })
    cands = InMemoryCandidatePort([
        {
            "entity_id": "ent-zzz",
            "canonical_name": "Completely Different Name",
            "entity_type": "person",
            "trgm_similarity": 0.1,
        },
    ])
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    sig = _signal([{
        "entity_name": "Putin", "entity_type": "person", "confidence": 0.3,
    }])
    out = await handler.transform(sig, _ctx())
    entry = out.payload[ENTITIES_PAYLOAD_KEY][0]
    # Downgraded to 0.5 — exactly at the apply floor (>= 0.5), so the match
    # still applies but with lowered confidence.
    assert entry[RESOLUTION_CONFIDENCE_KEY] == 0.5


@pytest.mark.asyncio
async def test_slm_failure_one_entry_does_not_fail_signal() -> None:
    slm = _StubSLM(
        verdicts={
            "Bad": RuntimeError("slm boom"),
            "Good": {
                "entity_name": "Good", "matched_entity_id": "ent-good",
                "is_new_entity": False, "confidence": 0.9,
            },
        },
    )
    cands = InMemoryCandidatePort([
        {"entity_id": "ent-good", "canonical_name": "Good", "entity_type": "person"},
        {"entity_id": "ent-bad", "canonical_name": "Bad", "entity_type": "person"},
    ])
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    sig = _signal([
        {"entity_name": "Bad", "entity_type": "person", "confidence": 0.3},
        {"entity_name": "Good", "entity_type": "person", "confidence": 0.3},
    ])
    out = await handler.transform(sig, _ctx())
    entries = out.payload[ENTITIES_PAYLOAD_KEY]
    # Bad entry stayed unresolved; Good was resolved.
    assert RESOLVED_ENTITY_ID_KEY not in entries[0]
    assert entries[1][RESOLVED_ENTITY_ID_KEY] == "ent-good"
    assert handler._signals_dropped == 1


@pytest.mark.asyncio
async def test_max_entities_per_signal_caps_calls() -> None:
    slm = _StubSLM(default={
        "entity_name": "x", "matched_entity_id": "ent-1",
        "is_new_entity": False, "confidence": 0.9,
    })
    cands = InMemoryCandidatePort([
        {"entity_id": f"ent-{i}", "canonical_name": f"name-{i}",
         "entity_type": "person"} for i in range(10)
    ])
    handler = SLMEntityResolveHandler(
        _config(max_entities_per_signal=2), slm=slm, candidates=cands,
    )
    sig = _signal([
        {"entity_name": f"name-{i}", "entity_type": "person", "confidence": 0.3}
        for i in range(5)
    ])
    await handler.transform(sig, _ctx())
    assert len(slm.calls) == 2


@pytest.mark.asyncio
async def test_chat_complete_path_works() -> None:
    content = json.dumps({
        "entity_name": "Putin",
        "matched_entity_id": "ent-001",
        "is_new_entity": False,
        "confidence": 0.9,
        "reasoning": "match",
    })
    slm = _StubChatSLM(content)
    cands = _candidates_for("Putin")
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    sig = _signal([{
        "entity_name": "Putin", "entity_type": "person", "confidence": 0.3,
    }])
    out = await handler.transform(sig, _ctx())
    entry = out.payload[ENTITIES_PAYLOAD_KEY][0]
    assert entry[RESOLVED_ENTITY_ID_KEY] == "ent-001"
    assert slm.calls[0]["system"] == SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# health_check + lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_degraded_without_provider() -> None:
    handler = SLMEntityResolveHandler(_config(), slm=None, candidates=None)
    health = await handler.health_check(_ctx())
    assert health.state == "degraded"
    assert health.detail["slm_wired"] is False


@pytest.mark.asyncio
async def test_health_check_healthy_when_wired() -> None:
    slm = _StubSLM(default={"entity_name": "x", "is_new_entity": True, "confidence": 0.9})
    cands = _candidates_for("x")
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    health = await handler.health_check(_ctx())
    assert health.state == "healthy"
    assert health.detail["slm_wired"] is True
    assert health.detail["candidates_wired"] is True


@pytest.mark.asyncio
async def test_on_configure_wires_ports() -> None:
    handler = SLMEntityResolveHandler(_config(), slm=None, candidates=None)
    assert handler._slm is None
    slm = _StubSLM(default={"entity_name": "x", "is_new_entity": True, "confidence": 0.9})
    cands = _candidates_for("x")
    await handler.on_configure(_ctx(), slm=slm, candidates=cands)
    assert handler._slm is slm
    assert handler._candidates is cands


@pytest.mark.asyncio
async def test_on_retire_clears_ports() -> None:
    slm = _StubSLM(default={"entity_name": "x", "is_new_entity": True, "confidence": 0.9})
    cands = _candidates_for("x")
    handler = SLMEntityResolveHandler(_config(), slm=slm, candidates=cands)
    await handler.on_retire(_ctx())
    assert handler._slm is None
    assert handler._candidates is None


# ---------------------------------------------------------------------------
# Candidate-port stub semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inmemory_candidates_exact_match_scores_highest() -> None:
    cands = InMemoryCandidatePort([
        {"entity_id": "a", "canonical_name": "Alpha", "entity_type": "person"},
        {"entity_id": "b", "canonical_name": "Beta", "entity_type": "person"},
        {"entity_id": "c", "canonical_name": "Alpha Bravo", "entity_type": "person"},
    ])
    results = await cands.fetch_candidates(
        entity_name="Alpha", entity_type="person", limit=3,
    )
    assert results[0]["entity_id"] == "a"  # exact match ranks first


@pytest.mark.asyncio
async def test_inmemory_candidates_entity_type_filter() -> None:
    cands = InMemoryCandidatePort([
        {"entity_id": "a", "canonical_name": "Alpha", "entity_type": "person"},
        {"entity_id": "b", "canonical_name": "Alpha", "entity_type": "organization"},
    ])
    results = await cands.fetch_candidates(
        entity_name="Alpha", entity_type="organization", limit=5,
    )
    assert len(results) == 1
    assert results[0]["entity_id"] == "b"


@pytest.mark.asyncio
async def test_inmemory_candidates_other_type_includes_all() -> None:
    cands = InMemoryCandidatePort([
        {"entity_id": "a", "canonical_name": "Alpha", "entity_type": "person"},
        {"entity_id": "b", "canonical_name": "Beta", "entity_type": "organization"},
    ])
    results = await cands.fetch_candidates(
        entity_name="Alpha", entity_type="other", limit=5,
    )
    assert len(results) == 2


# ---------------------------------------------------------------------------
# Verdict pydantic shape
# ---------------------------------------------------------------------------


def test_entity_resolution_verdict_defaults() -> None:
    v = EntityResolutionVerdict(entity_name="x")
    assert v.matched_entity_id is None
    assert v.is_new_entity is False
    assert v.confidence == 0.0


def test_entity_resolution_verdict_clamps_confidence() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError
        EntityResolutionVerdict(entity_name="x", confidence=1.5)
