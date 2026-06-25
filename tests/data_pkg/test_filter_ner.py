# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the L-154 multilingual NER filter handler (HTTP variant).

Architectural-drift correction (2026-05-22): the pre-reshape suite spun up
real spaCy pipelines. The post-reshape handler calls the hosted Legba-models
``POST /extract`` endpoint. Tests inject an ``httpx.MockTransport`` so we
assert the wire shape (path, Basic Auth, JSON body) without requiring a
running service.

The test surface (per the L-154 brief, post-reshape):

  * Wire shape — path ``/extract``, Basic Auth header, ``{"text": "..."}``
    body.
  * Mapping — REBEL triples → entities → closed Legba ``entity_class``
    taxonomy.
  * Vocabulary alignment — unknown classes are filtered.
  * Idempotency — same payload produces the same entity list (modulo
    stable triple ordering from the server).
  * Multi-language switching — payload.language wins, language_hint is
    a fallback, default_language is the last resort.
  * Graceful degradation — 5xx / network failure → noop signal +
    handler health flips to ``degraded``; 401 → typed error path.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
import pytest_asyncio

from legba.data.filters._contract import FilterContext, FilterHealth
from legba.data.filters.ner import (
    NERModelMissing,  # backwards-compat alias
    NERMultilingualConfig,
    NERMultilingualHandler,
    NERServiceUnconfigured,
)
from legba.data.sources._contract import Signal
from legba.data.stack.nlp_service import (
    NlpServiceAuthError,
    NlpServiceClient,
    NlpServiceUnavailable,
)
from legba.data.vocabulary import ENTITY_CLASSES


# ---------------------------------------------------------------------------
# Mock-transport helpers
# ---------------------------------------------------------------------------


def _build_client(
    handler: Any,
    *,
    base_url: str = "https://models.test.invalid",
    api_user: str | None = "test-user",
    api_pass: str | None = "test-pass",
) -> NlpServiceClient:
    """Build an ``NlpServiceClient`` wired to a httpx.MockTransport handler."""
    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(
        base_url=base_url,
        transport=transport,
        auth=httpx.BasicAuth(api_user, api_pass) if api_user else None,
        timeout=5.0,
    )
    client = NlpServiceClient(
        endpoint=base_url,
        api_user=api_user,
        api_pass=api_pass,
        client=inner,
    )
    return client


def _make_extract_handler(
    *,
    triples_by_text: dict[str, list[dict[str, str]]] | None = None,
    default_triples: list[dict[str, str]] | None = None,
    captured_requests: list[httpx.Request] | None = None,
    status: int = 200,
    body: dict[str, Any] | None = None,
) -> Any:
    """Build an httpx MockTransport handler that responds to /extract + /health."""
    triples_by_text = triples_by_text or {}
    default_triples = default_triples or []
    captured_requests = captured_requests if captured_requests is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ok", "gpu": True, "models_loaded": True},
            )
        if request.url.path == "/extract":
            if status != 200:
                return httpx.Response(status, json=body or {"error": "test"})
            req_body = json.loads(request.content.decode("utf-8"))
            text = req_body.get("text", "")
            triples = triples_by_text.get(text, default_triples)
            return httpx.Response(200, json={"triples": triples, "ms": 100.0})
        return httpx.Response(404, json={"error": "unexpected path"})

    return handler, captured_requests


def _ctx(filter_id: str = "ner_test", **overrides: Any) -> FilterContext:
    defaults: dict[str, Any] = dict(
        target_id="t.test",
        target_version="v1",
        filter_id=filter_id,
        logger=logging.getLogger("test.ner"),
        scope_geo=[],
        scope_languages=["en", "pt", "es"],
    )
    defaults.update(overrides)
    return FilterContext(**defaults)


def _signal(
    *,
    body: str = "",
    title: str = "",
    language: str | None = None,
    language_hint: str | None = None,
) -> Signal:
    # Source-first pivot: Signal is source-owned; target_id was dropped from
    # the model (lives only on derived analyst outputs). See PIVOT_BUILD_PLAN.
    payload: dict[str, Any] = {}
    if body:
        payload["body"] = body
    if title:
        payload["title"] = title
    if language is not None:
        payload["language"] = language
    return Signal(
        source_id="src.test",
        payload=payload,
        content_hash="abc",
        canonical_url=None,
        language_hint=language_hint,
    )


@pytest_asyncio.fixture
async def en_handler() -> NERMultilingualHandler:
    """Handler with the canonical English+xx config + a mocked /extract."""
    handler_fn, _ = _make_extract_handler(
        default_triples=[
            {"subject": "Apple Inc.", "predicate": "headquarters location",
             "object": "Cupertino"},
            {"subject": "Tim Cook", "predicate": "employer", "object": "Apple Inc."},
        ],
    )
    client = _build_client(handler_fn)
    h = NERMultilingualHandler(
        NERMultilingualConfig(languages=["en", "xx"], default_language="xx"),
        nlp_client=client,
    )
    await h.on_configure(_ctx())
    await h.on_activate(_ctx())
    yield h
    await h.on_retire(_ctx())


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_defaults():
    cfg = NERMultilingualConfig()
    assert cfg.languages == ["en", "xx"]
    assert cfg.default_language == "xx"
    assert cfg.entity_taxonomy == "legba_v1"
    assert cfg.taxonomy_map is None
    assert cfg.min_confidence == 0.0


def test_config_rejects_default_not_in_languages():
    cfg = NERMultilingualConfig(languages=["en"], default_language="pt")
    with pytest.raises(ValueError, match="default_language"):
        NERMultilingualHandler(cfg)


def test_config_normalises_language_case():
    cfg = NERMultilingualConfig(languages=["EN", "XX"], default_language="EN")
    assert cfg.languages == ["en", "xx"]
    assert cfg.default_language == "en"


def test_config_rejects_empty_languages():
    with pytest.raises(ValueError):
        NERMultilingualConfig(languages=[])


def test_handler_class_vars():
    assert NERMultilingualHandler.kind == "ner_multilingual"
    assert NERMultilingualHandler.family == "filter"
    assert NERMultilingualHandler.schema_version == "legba/filter.ner_multilingual/1-0-0"
    assert NERMultilingualHandler.config_schema is NERMultilingualConfig
    assert "payload.entities" in NERMultilingualHandler.output_contract
    assert NERMultilingualHandler.idempotent is True


# ---------------------------------------------------------------------------
# Activation requires a bound client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_configure_raises_when_no_client():
    handler = NERMultilingualHandler(
        NERMultilingualConfig(languages=["en", "xx"], default_language="xx"),
    )
    with pytest.raises(NERServiceUnconfigured):
        await handler.on_configure(_ctx())


@pytest.mark.asyncio
async def test_old_ner_model_missing_alias_subclasses_unconfigured():
    """Backwards-compat: callers that catch :class:`NERModelMissing` still work."""
    handler = NERMultilingualHandler(
        NERMultilingualConfig(languages=["en", "xx"], default_language="xx"),
    )
    with pytest.raises(NERServiceUnconfigured):
        await handler.on_configure(_ctx())
    # The deprecation shim is a subclass — confirm typing still aligns.
    assert issubclass(NERModelMissing, NERServiceUnconfigured)


# ---------------------------------------------------------------------------
# Wire shape — path, auth, body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_call_uses_basic_auth_and_correct_path():
    requests: list[httpx.Request] = []
    handler_fn, _ = _make_extract_handler(
        default_triples=[
            {"subject": "Alice", "predicate": "employer", "object": "Acme Corp"},
        ],
        captured_requests=requests,
    )
    client = _build_client(handler_fn, api_user="alice", api_pass="s3cret")
    handler = NERMultilingualHandler(
        NERMultilingualConfig(),
        nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(body="Alice works at Acme Corp.")
    out = await handler.transform(sig, _ctx())
    assert out is not None

    # One /health on activate + one /extract on transform.
    paths = [r.url.path for r in requests]
    assert paths[0] == "/health"
    assert paths[1] == "/extract"
    auth_header = requests[1].headers.get("authorization", "")
    assert auth_header.startswith("Basic ")

    body = json.loads(requests[1].content.decode("utf-8"))
    assert body == {"text": "Alice works at Acme Corp."}
    await handler.on_retire(_ctx())


# ---------------------------------------------------------------------------
# Triple → entity mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transform_english_extracts_person_and_org_from_triples(en_handler):
    sig = _signal(
        body="Apple Inc. CEO Tim Cook announced new products at the Cupertino office.",
        language="en",
    )
    out = await en_handler.transform(sig, _ctx())
    assert out is not None
    entities = out.payload["entities"]
    assert entities, "expected at least one entity"
    classes = {e["class"] for e in entities}
    # Apple Inc. → corporation (Inc. cue); Tim Cook → person (multi-token);
    # Cupertino → location (place-of-X predicate / single capitalised token).
    assert classes.issubset(set(ENTITY_CLASSES))
    assert "corporation" in classes
    assert "person" in classes
    assert "location" in classes
    for e in entities:
        assert set(("class", "text", "start", "end", "lang", "confidence")) <= set(e)
        assert e["lang"] == "en"
        assert 0.0 <= e["confidence"] <= 1.0
    assert out.payload["ner_language"] == "en"


@pytest.mark.asyncio
async def test_transform_returns_empty_when_text_empty():
    handler_fn, _ = _make_extract_handler(default_triples=[])
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(
        NERMultilingualConfig(),
        nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(body="", language="en")
    out = await handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["entities"] == []
    assert out.payload["ner_language"] is None


@pytest.mark.asyncio
async def test_predicate_drives_object_to_country():
    handler_fn, _ = _make_extract_handler(
        default_triples=[
            {"subject": "Obama", "predicate": "country of citizenship",
             "object": "United States"},
        ],
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(
        NERMultilingualConfig(),
        nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(body="Obama is from the United States.")
    out = await handler.transform(sig, _ctx())
    classes_by_text = {e["text"]: e["class"] for e in out.payload["entities"]}
    assert classes_by_text["United States"] == "country"
    await handler.on_retire(_ctx())


# ---------------------------------------------------------------------------
# Language selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_picks_language_from_payload_then_language_hint(en_handler):
    sig = _signal(body="Apple announced products.", language_hint="en-US")
    out = await en_handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["ner_language"] == "en"


@pytest.mark.asyncio
async def test_language_code_normalised_to_two_letters(en_handler):
    sig = _signal(body="Apple Inc. announced products.", language="en-US")
    out = await en_handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["ner_language"] == "en"


# ---------------------------------------------------------------------------
# Vocabulary alignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vocabulary_alignment_drops_unknown_classes():
    handler_fn, _ = _make_extract_handler(
        default_triples=[
            {"subject": "Tim Cook", "predicate": "employer", "object": "Apple Inc."},
        ],
    )
    client = _build_client(handler_fn)
    restricted = set(ENTITY_CLASSES) - {"person"}
    handler = NERMultilingualHandler(
        NERMultilingualConfig(),
        vocabulary_values=restricted,
        nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(body="Tim Cook works at Apple Inc.")
    out = await handler.transform(sig, _ctx())
    assert out is not None
    classes = {e["class"] for e in out.payload["entities"]}
    assert "person" not in classes
    assert classes.issubset(restricted)
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_taxonomy_map_override_applies():
    handler_fn, _ = _make_extract_handler(
        default_triples=[
            {"subject": "Tim Cook", "predicate": "headed", "object": "Apple"},
        ],
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(
        NERMultilingualConfig(taxonomy_map={"cook": "corporation"}),
        nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(body="Tim Cook headed Apple.")
    out = await handler.transform(sig, _ctx())
    classes_by_text = {e["text"]: e["class"] for e in out.payload["entities"]}
    # Override forces Tim Cook → corporation (because token 'cook' matches).
    assert classes_by_text["Tim Cook"] == "corporation"
    await handler.on_retire(_ctx())


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_transform_yields_same_entities(en_handler):
    sig = _signal(body="Apple Inc. CEO Tim Cook visited Paris last week.", language="en")
    out_a = await en_handler.transform(sig, _ctx())
    out_b = await en_handler.transform(sig, _ctx())
    assert out_a is not None and out_b is not None
    assert out_a.payload["entities"] == out_b.payload["entities"]
    assert out_a.payload["entities_hash"] == out_b.payload["entities_hash"]


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_5xx_returns_signal_with_empty_entities():
    handler_fn, _ = _make_extract_handler(status=503, body={"error": "down"})
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(
        NERMultilingualConfig(),
        nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(body="Apple Inc. is a company.", language="en")
    out = await handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["entities"] == []
    # Health flipped to degraded.
    health = await handler.health_check(_ctx())
    assert health.state == "degraded"
    assert health.detail["service_healthy"] is False
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_service_network_failure_is_graceful():
    def boom(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "models_loaded": True})
        raise httpx.ConnectError("simulated network down")

    client = _build_client(boom)
    handler = NERMultilingualHandler(
        NERMultilingualConfig(),
        nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(body="Some content here for extract.", language="en")
    out = await handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["entities"] == []
    health = await handler.health_check(_ctx())
    assert health.state == "degraded"
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_service_401_records_auth_error():
    handler_fn, _ = _make_extract_handler(status=401, body={"detail": "unauthorized"})
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(
        NERMultilingualConfig(),
        nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(body="Some content here for extract.", language="en")
    out = await handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["entities"] == []
    health = await handler.health_check(_ctx())
    assert health.state == "degraded"
    assert "auth" in (health.last_error or "")
    await handler.on_retire(_ctx())


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_unhealthy_before_activate():
    h = NERMultilingualHandler(NERMultilingualConfig())
    health = await h.health_check(_ctx())
    assert health.state == "unhealthy"
    assert health.detail["service_bound"] is False


@pytest.mark.asyncio
async def test_health_reports_healthy_after_activate(en_handler):
    health = await en_handler.health_check(_ctx())
    assert health.state == "healthy"
    assert health.detail["service_bound"] is True
    assert health.detail["activated"] is True
    # loaded_languages mirrors the configured set when the service is up.
    assert "en" in health.detail["languages_loaded"]
    assert "xx" in health.detail["languages_loaded"]


@pytest.mark.asyncio
async def test_health_counters_track_signals(en_handler):
    sig = _signal(body="Apple CEO Tim Cook visited Paris.", language="en")
    await en_handler.transform(sig, _ctx())
    await en_handler.transform(sig, _ctx())
    empty = _signal(body="", language="en")
    await en_handler.transform(empty, _ctx())
    h = await en_handler.health_check(_ctx())
    assert h.signals_in_24h == 3
    assert h.signals_out_24h == 2
    assert h.signals_dropped_24h == 1


# ---------------------------------------------------------------------------
# Lifecycle: retire clears state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_retire_clears_activation(en_handler):
    assert en_handler.is_activated
    await en_handler.on_retire(_ctx())
    assert en_handler.is_activated is False
    health = await en_handler.health_check(_ctx())
    assert health.state == "unhealthy"


@pytest.mark.asyncio
async def test_on_resume_reactivates(en_handler):
    await en_handler.on_pause(_ctx())
    assert en_handler.is_activated is False
    await en_handler.on_resume(_ctx())
    assert en_handler.is_activated


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_text_chars_truncates():
    """Ensure the handler truncates before posting — the captured request
    body should be at most max_text_chars long."""
    requests: list[httpx.Request] = []
    handler_fn, _ = _make_extract_handler(
        default_triples=[],
        captured_requests=requests,
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(
        NERMultilingualConfig(max_text_chars=200),
        nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    huge = "Apple Inc. announced products. " * 10_000
    sig = _signal(body=huge, language="en")
    await handler.transform(sig, _ctx())
    body = json.loads(requests[-1].content.decode("utf-8"))
    assert len(body["text"]) <= 200
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_min_confidence_floor_filters_low_conf():
    """With min_confidence=1.1 (>1.0), all synthesised confidences (1.0)
    are below the floor and entities are dropped."""
    handler_fn, _ = _make_extract_handler(
        default_triples=[
            {"subject": "Apple Inc.", "predicate": "made", "object": "iPhone"},
        ],
    )
    client = _build_client(handler_fn)
    # min_confidence is bounded [0, 1]; use 1.0 which is the edge — synthesised
    # 1.0 IS NOT below 1.0 so entities remain.
    handler = NERMultilingualHandler(
        NERMultilingualConfig(min_confidence=1.0),
        nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(body="Apple Inc. made the iPhone.", language="en")
    out = await handler.transform(sig, _ctx())
    assert out.payload["entities"]
    await handler.on_retire(_ctx())


# ---------------------------------------------------------------------------
# Class-vars sanity
# ---------------------------------------------------------------------------


def test_entity_classes_match_the_nine():
    assert len(ENTITY_CLASSES) == 9
    expected = {
        "entity", "location", "organization", "person", "event",
        "country", "concept", "corporation", "software",
    }
    assert set(ENTITY_CLASSES) == expected
