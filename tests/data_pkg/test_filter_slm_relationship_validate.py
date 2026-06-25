# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for :class:`legba.data.filters.SLMRelationshipValidateHandler` (L-202).

No external services required. Cases covered:

  * Pass-through when no relationships array exists.
  * Pass-through when provider not wired.
  * Idempotency on previously-validated triples.
  * ``force_validate`` overrides idempotency.
  * Triple without subject/object is skipped.
  * Verdict applied — ``valid`` / ``corrected_type`` / confidence /
    reasoning + ``slm_validated`` flag.
  * Reclassification (corrected_type set).
  * Reification heuristic — HostileTo pair + SuppliesWeaponsTo trigger
    sets ``needs_reification=True``.
  * Reification doesn't fire without HostileTo pre-knowledge.
  * SLM failure → all triples remain unvalidated.
  * ``max_triples_per_signal`` caps the batch.
  * Batch parses both ``{"verdicts": [...]}`` and bare ``[...]`` shapes.
  * Single-verdict object also accepted.
  * Hostile-pair canonicalisation (order independent).
  * Both ``complete`` and ``chat_complete`` dispatch shapes work.
  * Handler ClassVar shape matches L-102 §1 / §3.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from legba.data.filters import (
    FilterContext,
    SLMRelationshipValidateConfig,
    SLMRelationshipValidateHandler,
)
from legba.data.filters._contract import FilterHealth, StreamHandler
from legba.data.filters.slm_relationship_validate import (
    CORRECTED_TYPE_KEY,
    NEEDS_REIFICATION_KEY,
    RELATIONSHIPS_PAYLOAD_KEY,
    SLM_RELATIONSHIP_VALIDATE_KIND,
    SLM_RELATIONSHIP_VALIDATE_SCHEMA_VERSION,
    SLM_VALIDATED_FLAG,
    SYSTEM_PROMPT,
    VALID_KEY,
    VALIDATION_CONFIDENCE_KEY,
    VALIDATION_REASONING_KEY,
    RelationshipVerdict,
)
from legba.data.sources._contract import Signal


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubSLM:
    """Legacy ``complete``-shape SLM."""

    def __init__(
        self,
        response: Any | Exception = None,
    ) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({
            "prompt": prompt, "system": system, "schema": json_schema,
        })
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


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


def _signal(
    relationships: list[dict[str, Any]],
    **extra_payload: Any,
) -> Signal:
    payload = {
        RELATIONSHIPS_PAYLOAD_KEY: relationships,
        "source_text": "Iran funds Hamas operations in Gaza according to recent reports.",
        **extra_payload,
    }
    return Signal(
        source_id="test_source",
        payload=payload,
        content_hash="deadbeef",
    )


def _ctx(**overrides: Any) -> FilterContext:
    defaults = {
        "target_id": "test_target",
        "target_version": "v1",
        "filter_id": "slm_relationship_validate@1",
        "logger": logging.getLogger("test.slm_relationship_validate"),
    }
    defaults.update(overrides)
    return FilterContext(**defaults)


def _config(**overrides: Any) -> SLMRelationshipValidateConfig:
    defaults: dict[str, Any] = {
        "max_source_chars": 1000,
        "max_triples_per_signal": 50,
        "known_hostile_pairs": [],
        "force_validate": False,
    }
    defaults.update(overrides)
    return SLMRelationshipValidateConfig(**defaults)


# ---------------------------------------------------------------------------
# ClassVar / Protocol shape
# ---------------------------------------------------------------------------


def test_classvars_match_l102_contract() -> None:
    handler = SLMRelationshipValidateHandler(_config())
    assert handler.kind == SLM_RELATIONSHIP_VALIDATE_KIND
    assert handler.kind == "slm_relationship_validate"
    assert handler.family == "filter"
    assert handler.schema_version == SLM_RELATIONSHIP_VALIDATE_SCHEMA_VERSION
    assert handler.config_schema is SLMRelationshipValidateConfig
    assert handler.idempotent is True


def test_satisfies_streamhandler_protocol() -> None:
    handler = SLMRelationshipValidateHandler(_config())
    assert isinstance(handler, StreamHandler)


def test_output_contract_declares_payload_keys() -> None:
    contract = SLMRelationshipValidateHandler.output_contract
    assert f"payload.{RELATIONSHIPS_PAYLOAD_KEY}" in contract


# ---------------------------------------------------------------------------
# transform — gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passthrough_when_no_relationships() -> None:
    slm = _StubSLM(response={"verdicts": []})
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = Signal(
        source_id="s",
        payload={"text": "no triples"}, content_hash="abc",
    )
    out = await handler.transform(sig, _ctx())
    assert out is sig
    assert slm.calls == []


@pytest.mark.asyncio
async def test_passthrough_when_provider_unwired() -> None:
    handler = SLMRelationshipValidateHandler(_config(), slm=None)
    sig = _signal([{"subject": "Iran", "predicate": "FundedBy", "object": "Hamas"}])
    out = await handler.transform(sig, _ctx())
    assert out is not None
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert SLM_VALIDATED_FLAG not in triple


@pytest.mark.asyncio
async def test_already_validated_triple_skipped() -> None:
    slm = _StubSLM(response={"verdicts": []})
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = _signal([{
        "subject": "Iran", "predicate": "FundedBy", "object": "Hamas",
        SLM_VALIDATED_FLAG: True,
    }])
    await handler.transform(sig, _ctx())
    assert slm.calls == []


@pytest.mark.asyncio
async def test_force_validate_overrides_idempotency() -> None:
    slm = _StubSLM(response={"verdicts": [{
        "triple_index": 0, "valid": True, "confidence": 0.9,
    }]})
    handler = SLMRelationshipValidateHandler(
        _config(force_validate=True), slm=slm,
    )
    sig = _signal([{
        "subject": "Iran", "predicate": "FundedBy", "object": "Hamas",
        SLM_VALIDATED_FLAG: True, VALID_KEY: False,
    }])
    out = await handler.transform(sig, _ctx())
    assert len(slm.calls) == 1
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert triple[VALID_KEY] is True


@pytest.mark.asyncio
async def test_triple_without_subject_or_object_skipped() -> None:
    slm = _StubSLM(response={"verdicts": []})
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = _signal([
        {"subject": "", "predicate": "FundedBy", "object": "Hamas"},
        {"subject": "Iran", "predicate": "FundedBy", "object": ""},
    ])
    await handler.transform(sig, _ctx())
    assert slm.calls == []


# ---------------------------------------------------------------------------
# transform — verdict application
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_verdict_applied() -> None:
    slm = _StubSLM(response={"verdicts": [{
        "triple_index": 0, "valid": True,
        "confidence": 0.88, "reasoning": "supported by source text",
    }]})
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = _signal([{
        "subject": "Iran", "predicate": "FundedBy", "object": "Hamas",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert triple[VALID_KEY] is True
    assert triple[SLM_VALIDATED_FLAG] is True
    assert triple[VALIDATION_CONFIDENCE_KEY] == 0.88
    assert "supported" in triple[VALIDATION_REASONING_KEY]
    assert triple[CORRECTED_TYPE_KEY] is None


@pytest.mark.asyncio
async def test_invalid_verdict_applied() -> None:
    slm = _StubSLM(response={"verdicts": [{
        "triple_index": 0, "valid": False,
        "confidence": 0.95, "reasoning": "hallucinated",
    }]})
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = _signal([{
        "subject": "Iran", "predicate": "supplies", "object": "Hamas",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert triple[VALID_KEY] is False
    assert triple[SLM_VALIDATED_FLAG] is True


@pytest.mark.asyncio
async def test_reclassified_verdict_records_corrected_type() -> None:
    slm = _StubSLM(response={"verdicts": [{
        "triple_index": 0, "valid": False, "corrected_type": "FundedBy",
        "confidence": 0.8, "reasoning": "funding, not supplies",
    }]})
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = _signal([{
        "subject": "Iran", "predicate": "supplies", "object": "Hamas",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert triple[CORRECTED_TYPE_KEY] == "FundedBy"
    assert handler._triples_reclassified == 1


# ---------------------------------------------------------------------------
# transform — reification heuristic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reification_flagged_for_hostile_pair_plus_supplies_weapons() -> None:
    """HostileTo pair + SuppliesWeaponsTo triple → needs_reification flag."""
    slm = _StubSLM(response={"verdicts": [{
        "triple_index": 0, "valid": True,
        "confidence": 0.9, "reasoning": "confirmed",
    }]})
    handler = SLMRelationshipValidateHandler(
        _config(known_hostile_pairs=["Iran|Israel"]),
        slm=slm,
    )
    sig = _signal([{
        "subject": "Iran", "predicate": "SuppliesWeaponsTo", "object": "Israel",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert triple.get(NEEDS_REIFICATION_KEY) is True
    assert handler._triples_reification_flagged == 1


@pytest.mark.asyncio
async def test_reification_flagged_for_funded_by_after_reclassification() -> None:
    """Reclassification to FundedBy + HostileTo pair → reification."""
    slm = _StubSLM(response={"verdicts": [{
        "triple_index": 0, "valid": False, "corrected_type": "FundedBy",
        "confidence": 0.85, "reasoning": "funding instead",
    }]})
    handler = SLMRelationshipValidateHandler(
        _config(known_hostile_pairs=["Iran|Israel"]),
        slm=slm,
    )
    sig = _signal([{
        "subject": "Iran", "predicate": "supplies", "object": "Israel",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert triple.get(NEEDS_REIFICATION_KEY) is True


@pytest.mark.asyncio
async def test_reification_not_flagged_without_hostile_pair() -> None:
    slm = _StubSLM(response={"verdicts": [{
        "triple_index": 0, "valid": True, "confidence": 0.9,
    }]})
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = _signal([{
        "subject": "Iran", "predicate": "SuppliesWeaponsTo", "object": "Israel",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert NEEDS_REIFICATION_KEY not in triple


@pytest.mark.asyncio
async def test_reification_not_flagged_for_unrelated_edge_type() -> None:
    slm = _StubSLM(response={"verdicts": [{
        "triple_index": 0, "valid": True, "confidence": 0.9,
    }]})
    handler = SLMRelationshipValidateHandler(
        _config(known_hostile_pairs=["Iran|Israel"]),
        slm=slm,
    )
    sig = _signal([{
        "subject": "Iran", "predicate": "allied_with", "object": "Israel",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert NEEDS_REIFICATION_KEY not in triple


@pytest.mark.asyncio
async def test_hostile_pair_canonicalisation_order_independent() -> None:
    """``"Israel|Iran"`` and ``"Iran|Israel"`` resolve to the same pair."""
    slm = _StubSLM(response={"verdicts": [{
        "triple_index": 0, "valid": True, "confidence": 0.9,
    }]})
    handler = SLMRelationshipValidateHandler(
        _config(known_hostile_pairs=["Israel|Iran"]),  # reversed
        slm=slm,
    )
    sig = _signal([{
        "subject": "iran", "predicate": "SuppliesWeaponsTo", "object": "Israel",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert triple.get(NEEDS_REIFICATION_KEY) is True


# ---------------------------------------------------------------------------
# transform — batch + failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slm_failure_leaves_triples_unvalidated() -> None:
    slm = _StubSLM(response=RuntimeError("slm down"))
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = _signal([
        {"subject": "Iran", "predicate": "FundedBy", "object": "Hamas"},
    ])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert SLM_VALIDATED_FLAG not in triple
    assert handler._signals_dropped == 1


@pytest.mark.asyncio
async def test_max_triples_per_signal_caps_batch() -> None:
    """Only the first N triples are validated per signal."""
    slm = _StubSLM(response={"verdicts": [
        {"triple_index": i, "valid": True, "confidence": 0.9}
        for i in range(2)
    ]})
    handler = SLMRelationshipValidateHandler(
        _config(max_triples_per_signal=2), slm=slm,
    )
    triples = [
        {"subject": f"A{i}", "predicate": "allied_with", "object": f"B{i}"}
        for i in range(5)
    ]
    sig = _signal(triples)
    out = await handler.transform(sig, _ctx())
    out_triples = out.payload[RELATIONSHIPS_PAYLOAD_KEY]
    # First 2 validated, remaining 3 untouched
    assert out_triples[0][SLM_VALIDATED_FLAG] is True
    assert out_triples[1][SLM_VALIDATED_FLAG] is True
    assert SLM_VALIDATED_FLAG not in out_triples[2]
    assert SLM_VALIDATED_FLAG not in out_triples[3]
    assert SLM_VALIDATED_FLAG not in out_triples[4]


@pytest.mark.asyncio
async def test_bare_list_response_shape_accepted() -> None:
    """SLM that returns a bare ``[...]`` instead of ``{"verdicts": [...]}``
    is still parsed correctly."""
    slm = _StubSLM(response=[{
        "triple_index": 0, "valid": True, "confidence": 0.9,
    }])
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = _signal([{
        "subject": "Iran", "predicate": "FundedBy", "object": "Hamas",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert triple[VALID_KEY] is True


@pytest.mark.asyncio
async def test_single_verdict_object_shape_accepted() -> None:
    """SLM that returns a single verdict object (not wrapped) is accepted."""
    slm = _StubSLM(response={
        "triple_index": 0, "valid": True, "confidence": 0.9,
    })
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = _signal([{
        "subject": "Iran", "predicate": "FundedBy", "object": "Hamas",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert triple[VALID_KEY] is True


@pytest.mark.asyncio
async def test_chat_complete_path_works() -> None:
    content = json.dumps({
        "verdicts": [{
            "triple_index": 0, "valid": True, "confidence": 0.9,
            "reasoning": "ok",
        }],
    })
    slm = _StubChatSLM(content)
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    sig = _signal([{
        "subject": "Iran", "predicate": "FundedBy", "object": "Hamas",
    }])
    out = await handler.transform(sig, _ctx())
    triple = out.payload[RELATIONSHIPS_PAYLOAD_KEY][0]
    assert triple[VALID_KEY] is True
    assert slm.calls[0]["system"] == SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# health_check + lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_degraded_without_provider() -> None:
    handler = SLMRelationshipValidateHandler(_config(), slm=None)
    health = await handler.health_check(_ctx())
    assert health.state == "degraded"
    assert health.detail["slm_wired"] is False


@pytest.mark.asyncio
async def test_health_check_healthy_when_wired() -> None:
    slm = _StubSLM(response={"verdicts": []})
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    health = await handler.health_check(_ctx())
    assert isinstance(health, FilterHealth)
    assert health.state == "healthy"


@pytest.mark.asyncio
async def test_on_configure_wires_slm() -> None:
    handler = SLMRelationshipValidateHandler(_config(), slm=None)
    slm = _StubSLM(response={"verdicts": []})
    await handler.on_configure(_ctx(), slm=slm)
    assert handler._slm is slm


@pytest.mark.asyncio
async def test_on_retire_clears_slm() -> None:
    slm = _StubSLM(response={"verdicts": []})
    handler = SLMRelationshipValidateHandler(_config(), slm=slm)
    await handler.on_retire(_ctx())
    assert handler._slm is None


# ---------------------------------------------------------------------------
# Verdict pydantic shape
# ---------------------------------------------------------------------------


def test_relationship_verdict_defaults() -> None:
    v = RelationshipVerdict(triple_index=0, valid=True)
    assert v.confidence == 0.7
    assert v.corrected_type is None
    assert v.reasoning == ""


def test_relationship_verdict_confidence_clamped() -> None:
    with pytest.raises(Exception):
        RelationshipVerdict(triple_index=0, valid=True, confidence=2.0)
