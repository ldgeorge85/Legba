# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for :class:`legba.data.filters.SLMClassificationRefineHandler` (L-202).

No external services required. The SLM port is a deterministic stub so the
tests exercise behaviour, not the upstream provider stack. Cases covered:

  * Idempotency on ``slm_refined=True``.
  * Pass-through when no classification annotation exists.
  * Boundary-case detection by score gap.
  * ``refine_other`` heuristic for ``"other"``-labelled signals.
  * ``force_refine`` overrides idempotency.
  * SLM failure → signal returned unmodified (never dropped).
  * Provider not wired → pass-through.
  * Sub-confidence verdict skipped (annotation preserved).
  * Verdict applied — payload extended with ``slm_refined`` flag,
    reasoning, all categories, and original event_type preserved as
    ``pre_slm_event_type``.
  * Handler ClassVar shape matches L-102 §1 / §3.
  * Health counters advance correctly.
  * Both legacy ``complete`` shape and L-120 ``chat_complete`` shape
    are dispatched correctly.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from legba.data.filters import (
    FilterContext,
    SLMClassificationRefineConfig,
    SLMClassificationRefineHandler,
)
from legba.data.filters._contract import FilterHealth, StreamHandler
from legba.data.filters.slm_classification_refine import (
    CLASSIFICATION_PAYLOAD_KEY,
    SLM_CLASSIFY_KIND,
    SLM_CLASSIFY_SCHEMA_VERSION,
    SLM_REFINED_FLAG,
    SYSTEM_PROMPT,
    ChatSLMPort,
    ClassificationVerdict,
    SLMPort,
    _extract_scores,
    _extract_text,
    _parse_json_loose,
)
from legba.data.sources._contract import Signal


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubLegacySLM:
    """Implements the legacy ``complete`` shape — returns parsed dicts."""

    def __init__(self, verdict: dict[str, Any] | Exception) -> None:
        self._verdict = verdict
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
        if isinstance(self._verdict, Exception):
            raise self._verdict
        return self._verdict


class _StubChatResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubChatSLM:
    """Implements the L-120 ``chat_complete`` shape."""

    def __init__(self, content: str | Exception) -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Any],
        *,
        system: str | None = None,
        **kwargs: Any,
    ) -> _StubChatResponse:
        self.calls.append({
            "messages": messages, "system": system, "kwargs": kwargs,
        })
        if isinstance(self._content, Exception):
            raise self._content
        return _StubChatResponse(self._content)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signal(payload: dict[str, Any], **overrides: Any) -> Signal:
    base = {
        "source_id": "test_source",
        "payload": payload,
        "content_hash": "deadbeef",
    }
    base.update(overrides)
    return Signal(**base)


def _ctx(**overrides: Any) -> FilterContext:
    defaults = {
        "target_id": "test_target",
        "target_version": "v1",
        "filter_id": "slm_classification_refine@1",
        "logger": logging.getLogger("test.slm_classify"),
    }
    defaults.update(overrides)
    return FilterContext(**defaults)


def _annotation(
    event_type: str = "conflict",
    scores: dict[str, float] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    base = {
        "event_type": event_type,
        "severity": None,
        "sentiment": None,
        "confidence": 0.55,
        "backend_used": "zero_shot",
        "schema": "test_taxonomy",
        "label_scores": scores or {"conflict": 0.55, "diplomatic": 0.50},
    }
    base.update(extra)
    return base


def _config(**overrides: Any) -> SLMClassificationRefineConfig:
    defaults: dict[str, Any] = {
        "boundary_gap": 0.1,
        "refine_other": True,
        "min_confidence": 0.5,
        "max_text_chars": 500,
        "force_refine": False,
    }
    defaults.update(overrides)
    return SLMClassificationRefineConfig(**defaults)


# ---------------------------------------------------------------------------
# ClassVar / Protocol shape tests
# ---------------------------------------------------------------------------


def test_classvars_match_l102_contract() -> None:
    """L-102 §1: kind, family, schema_version, config_schema, handler_version."""
    handler = SLMClassificationRefineHandler(_config())
    assert handler.kind == SLM_CLASSIFY_KIND == "slm_classification_refine"
    assert handler.family == "filter"
    assert handler.schema_version == SLM_CLASSIFY_SCHEMA_VERSION
    assert handler.config_schema is SLMClassificationRefineConfig
    assert isinstance(handler.handler_version, str)
    assert handler.idempotent is True


def test_satisfies_streamhandler_protocol() -> None:
    """runtime_checkable Protocol from _contract.py."""
    handler = SLMClassificationRefineHandler(_config())
    assert isinstance(handler, StreamHandler)


def test_output_contract_declares_payload_keys() -> None:
    contract = SLMClassificationRefineHandler.output_contract
    assert f"payload.{CLASSIFICATION_PAYLOAD_KEY}" in contract
    assert (
        f"payload.{CLASSIFICATION_PAYLOAD_KEY}.{SLM_REFINED_FLAG}" in contract
    )


# ---------------------------------------------------------------------------
# transform — gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passthrough_when_no_annotation() -> None:
    """Signals without a classification annotation are unchanged."""
    slm = _StubLegacySLM({"signal_id": "x", "corrected_categories": ["a"]})
    handler = SLMClassificationRefineHandler(_config(), slm=slm)
    sig = _signal({"text": "hello world"})
    out = await handler.transform(sig, _ctx())
    assert out is sig  # idiomatic pass-through (same object)
    assert slm.calls == []


@pytest.mark.asyncio
async def test_passthrough_when_provider_unwired() -> None:
    """Boundary-case signal with no SLM provider → pass-through."""
    handler = SLMClassificationRefineHandler(_config(), slm=None)
    payload = {"text": "x" * 100, CLASSIFICATION_PAYLOAD_KEY: _annotation()}
    out = await handler.transform(_signal(payload), _ctx())
    assert out is not None
    assert out.payload[CLASSIFICATION_PAYLOAD_KEY].get(SLM_REFINED_FLAG) is None


@pytest.mark.asyncio
async def test_idempotent_when_slm_refined_already_set() -> None:
    """slm_refined=True → no-op."""
    slm = _StubLegacySLM({"signal_id": "x", "corrected_categories": ["a"]})
    handler = SLMClassificationRefineHandler(_config(), slm=slm)
    annotation = _annotation()
    annotation[SLM_REFINED_FLAG] = True
    payload = {"text": "x" * 100, CLASSIFICATION_PAYLOAD_KEY: annotation}
    await handler.transform(_signal(payload), _ctx())
    assert slm.calls == []


@pytest.mark.asyncio
async def test_force_refine_overrides_idempotency() -> None:
    """force_refine=True bypasses the slm_refined idempotency gate."""
    verdict = {
        "signal_id": "x",
        "corrected_categories": ["military"],
        "confidence": 0.9,
        "reasoning": "ok",
    }
    slm = _StubLegacySLM(verdict)
    handler = SLMClassificationRefineHandler(
        _config(force_refine=True), slm=slm,
    )
    annotation = _annotation()
    annotation[SLM_REFINED_FLAG] = True
    payload = {"text": "x" * 100, CLASSIFICATION_PAYLOAD_KEY: annotation}
    await handler.transform(_signal(payload), _ctx())
    assert len(slm.calls) == 1


@pytest.mark.asyncio
async def test_boundary_case_by_score_gap() -> None:
    """Top-2 scores within ``boundary_gap`` → refinement triggered."""
    verdict = {
        "signal_id": "x",
        "corrected_categories": ["diplomatic", "conflict"],
        "confidence": 0.85,
        "reasoning": "tone is diplomatic",
    }
    slm = _StubLegacySLM(verdict)
    handler = SLMClassificationRefineHandler(_config(), slm=slm)
    payload = {
        "text": "Some signal text " * 10,
        CLASSIFICATION_PAYLOAD_KEY: _annotation(
            event_type="conflict",
            scores={"conflict": 0.50, "diplomatic": 0.45},
        ),
    }
    out = await handler.transform(_signal(payload), _ctx())
    assert out is not None
    cls = out.payload[CLASSIFICATION_PAYLOAD_KEY]
    assert cls["event_type"] == "diplomatic"
    assert cls[SLM_REFINED_FLAG] is True
    assert cls["pre_slm_event_type"] == "conflict"
    assert cls["slm_confidence"] == 0.85
    assert cls["slm_reasoning"] == "tone is diplomatic"
    assert cls["slm_all_categories"] == ["diplomatic", "conflict"]


@pytest.mark.asyncio
async def test_not_boundary_when_gap_wide() -> None:
    """Score gap above the threshold → no refinement."""
    slm = _StubLegacySLM({"signal_id": "x", "corrected_categories": ["a"]})
    handler = SLMClassificationRefineHandler(_config(boundary_gap=0.05), slm=slm)
    payload = {
        "text": "Some signal text " * 10,
        CLASSIFICATION_PAYLOAD_KEY: _annotation(
            event_type="conflict",
            scores={"conflict": 0.90, "diplomatic": 0.10},
        ),
    }
    await handler.transform(_signal(payload), _ctx())
    assert slm.calls == []


@pytest.mark.asyncio
async def test_other_label_triggers_refinement() -> None:
    """``"other"`` label triggers refinement via the strategy-2 heuristic."""
    verdict = {
        "signal_id": "x",
        "corrected_categories": ["economic"],
        "confidence": 0.7,
    }
    slm = _StubLegacySLM(verdict)
    handler = SLMClassificationRefineHandler(_config(), slm=slm)
    payload = {
        "text": "Some signal text " * 10,
        CLASSIFICATION_PAYLOAD_KEY: _annotation(
            event_type="other",
            scores={"other": 0.30},  # only one score → not boundary by gap
        ),
    }
    out = await handler.transform(_signal(payload), _ctx())
    cls = out.payload[CLASSIFICATION_PAYLOAD_KEY]
    assert cls["event_type"] == "economic"
    assert cls[SLM_REFINED_FLAG] is True


@pytest.mark.asyncio
async def test_refine_other_disabled_skips_other_label() -> None:
    slm = _StubLegacySLM({"signal_id": "x", "corrected_categories": ["a"]})
    handler = SLMClassificationRefineHandler(
        _config(refine_other=False), slm=slm,
    )
    payload = {
        "text": "Some signal text " * 10,
        CLASSIFICATION_PAYLOAD_KEY: _annotation(
            event_type="other",
            scores={"other": 0.30},
        ),
    }
    await handler.transform(_signal(payload), _ctx())
    assert slm.calls == []


# ---------------------------------------------------------------------------
# transform — verdict application
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_verdict_not_applied() -> None:
    """Verdict with confidence below ``min_confidence`` → annotation preserved."""
    verdict = {
        "signal_id": "x",
        "corrected_categories": ["military"],
        "confidence": 0.3,
        "reasoning": "unsure",
    }
    slm = _StubLegacySLM(verdict)
    handler = SLMClassificationRefineHandler(
        _config(min_confidence=0.5), slm=slm,
    )
    payload = {
        "text": "Some signal text " * 10,
        CLASSIFICATION_PAYLOAD_KEY: _annotation(),
    }
    out = await handler.transform(_signal(payload), _ctx())
    cls = out.payload[CLASSIFICATION_PAYLOAD_KEY]
    assert cls["event_type"] == "conflict"  # unchanged
    assert cls.get(SLM_REFINED_FLAG) is None


@pytest.mark.asyncio
async def test_empty_verdict_not_applied() -> None:
    """SLM returns empty corrected_categories → annotation preserved."""
    slm = _StubLegacySLM({
        "signal_id": "x",
        "corrected_categories": [],
        "confidence": 0.9,
    })
    handler = SLMClassificationRefineHandler(_config(), slm=slm)
    payload = {
        "text": "Some signal text " * 10,
        CLASSIFICATION_PAYLOAD_KEY: _annotation(),
    }
    out = await handler.transform(_signal(payload), _ctx())
    cls = out.payload[CLASSIFICATION_PAYLOAD_KEY]
    assert cls.get(SLM_REFINED_FLAG) is None


@pytest.mark.asyncio
async def test_slm_failure_signal_passes_through_unmodified() -> None:
    """SLM raises → signal returned unchanged, counters track drop."""
    slm = _StubLegacySLM(RuntimeError("boom"))
    handler = SLMClassificationRefineHandler(_config(), slm=slm)
    payload = {
        "text": "Some signal text " * 10,
        CLASSIFICATION_PAYLOAD_KEY: _annotation(),
    }
    out = await handler.transform(_signal(payload), _ctx())
    assert out is not None
    cls = out.payload[CLASSIFICATION_PAYLOAD_KEY]
    assert cls.get(SLM_REFINED_FLAG) is None
    assert handler._signals_dropped == 1


@pytest.mark.asyncio
async def test_chat_complete_path_used_when_complete_absent() -> None:
    """SLM exposing only chat_complete is dispatched correctly."""
    content = json.dumps({
        "signal_id": "x",
        "corrected_categories": ["military"],
        "confidence": 0.85,
        "reasoning": "ok",
    })
    slm = _StubChatSLM(content)
    handler = SLMClassificationRefineHandler(_config(), slm=slm)
    payload = {
        "text": "Some signal text " * 10,
        CLASSIFICATION_PAYLOAD_KEY: _annotation(
            event_type="conflict",
            scores={"conflict": 0.50, "diplomatic": 0.45},
        ),
    }
    out = await handler.transform(_signal(payload), _ctx())
    cls = out.payload[CLASSIFICATION_PAYLOAD_KEY]
    assert cls["event_type"] == "military"
    assert cls[SLM_REFINED_FLAG] is True
    assert len(slm.calls) == 1
    # System prompt must be passed through
    assert slm.calls[0]["system"] == SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_degraded_when_provider_unwired() -> None:
    handler = SLMClassificationRefineHandler(_config(), slm=None)
    health = await handler.health_check(_ctx())
    assert isinstance(health, FilterHealth)
    assert health.state == "degraded"
    assert health.detail["provider_wired"] is False


@pytest.mark.asyncio
async def test_health_check_healthy_when_wired() -> None:
    slm = _StubLegacySLM({"signal_id": "x", "corrected_categories": ["a"]})
    handler = SLMClassificationRefineHandler(_config(), slm=slm)
    health = await handler.health_check(_ctx())
    assert health.state == "healthy"
    assert health.detail["provider_wired"] is True


@pytest.mark.asyncio
async def test_counters_advance() -> None:
    slm = _StubLegacySLM({
        "signal_id": "x",
        "corrected_categories": ["military"],
        "confidence": 0.85,
    })
    handler = SLMClassificationRefineHandler(_config(), slm=slm)
    payload = {
        "text": "Some signal text " * 10,
        CLASSIFICATION_PAYLOAD_KEY: _annotation(
            scores={"conflict": 0.50, "diplomatic": 0.45},
        ),
    }
    for _ in range(3):
        await handler.transform(_signal(payload), _ctx())
    assert handler._signals_in == 3
    assert handler._signals_out == 3
    assert handler._signals_refined == 3


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_configure_wires_slm() -> None:
    """on_configure(slm=...) sets the provider."""
    handler = SLMClassificationRefineHandler(_config(), slm=None)
    assert handler._slm is None
    slm = _StubLegacySLM({"signal_id": "x", "corrected_categories": ["a"]})
    await handler.on_configure(_ctx(), slm=slm)
    assert handler._slm is slm


@pytest.mark.asyncio
async def test_on_retire_clears_slm() -> None:
    slm = _StubLegacySLM({"signal_id": "x", "corrected_categories": ["a"]})
    handler = SLMClassificationRefineHandler(_config(), slm=slm)
    await handler.on_retire(_ctx())
    assert handler._slm is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_extract_text_priority_order() -> None:
    """``text`` → ``title`` → ``summary`` → ``body`` → ``content`` → ``description``."""
    assert _extract_text({"text": "first", "title": "second"}, 500) == "first"
    assert _extract_text({"title": "second", "summary": "third"}, 500) == "second"
    assert _extract_text({"summary": "third"}, 500) == "third"
    assert _extract_text({"body": "fourth"}, 500) == "fourth"
    assert _extract_text({}, 500) == ""


def test_extract_text_truncates() -> None:
    payload = {"text": "x" * 1000}
    assert len(_extract_text(payload, 500)) == 500


def test_extract_scores_uses_label_scores_when_present() -> None:
    annotation = {"label_scores": {"a": 0.6, "b": 0.4}}
    assert _extract_scores(annotation) == {"a": 0.6, "b": 0.4}


def test_extract_scores_synthetic_fallback() -> None:
    annotation = {"event_type": "conflict"}
    out = _extract_scores(annotation)
    assert out["conflict"] == 0.45
    assert "unknown_secondary" in out


def test_parse_json_loose_plain() -> None:
    assert _parse_json_loose('{"a": 1}') == {"a": 1}


def test_parse_json_loose_markdown_fence() -> None:
    text = '```json\n{"a": 1}\n```'
    assert _parse_json_loose(text) == {"a": 1}


def test_parse_json_loose_first_block() -> None:
    text = 'preamble {"a": 1} trailing'
    assert _parse_json_loose(text) == {"a": 1}


def test_parse_json_loose_unparseable_returns_none() -> None:
    assert _parse_json_loose("not json at all") is None


def test_classification_verdict_strips_empties() -> None:
    v = ClassificationVerdict(
        signal_id="x",
        corrected_categories=["", "a", " ", "b"],
        confidence=0.5,
    )
    assert v.corrected_categories == ["a", "b"]
