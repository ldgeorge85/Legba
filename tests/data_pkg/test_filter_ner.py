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
    _classify_entity_text as _clf,
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
    text: str = "",
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
    if text:
        # Telegram-shaped signal: message body lives in payload.text and
        # title/summary/raw_body are empty (M12).
        payload["text"] = text
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


# ===========================================================================
# R8 — class precision: the person-default no longer swallows orgs and places
# ===========================================================================
#
# Live 2026-07 enrichment-quality sweep. `_classify_entity_text` ended in "two
# capitalised tokens, no cue -> person", so "White House" / "Yonhap News" /
# "Russian Federation" / "State Duma" typed PERSON, real places ("Kyiv",
# "Crete", "Yekaterinburg", "Germany") fell to the generic `entity` bucket, and
# inverted GLiREL triples typed people as places ("Seyyed Ali Khamenei" and
# "Keir Starmer" -> location). Entity auto-merge requires the same SPECIFIC
# class on both sides, so this class instability was the proximate blocker on
# 570 exact-key duplicate clusters (Zelensky x9, Trump x7).
#
# Each surface named in the sweep is locked below, alongside the fallbacks the
# fix must NOT disturb.


@pytest.mark.parametrize("surface,expected", [
    # --- were PERSON, are institutions -------------------------------------
    ("White House", "organization"),
    ("Yonhap News", "organization"),
    ("Yonhap", "organization"),
    ("State Duma", "organization"),
    ("Russian Federation", "country"),
    ("EU", "organization"),
    # --- were LOCATION off an inverted triple, are people -------------------
    ("Seyyed Ali Khamenei", "person"),
    ("Keir Starmer", "person"),
    # --- were the generic bucket, are places --------------------------------
    ("Kyiv", "location"),
    ("Crete", "location"),
    ("Yekaterinburg", "location"),
    ("Germany", "country"),
])
def test_r8_reported_misclassifications(surface, expected):
    assert _clf(surface) == expected, (
        f"{surface!r} should be {expected}, got {_clf(surface)!r}"
    )


@pytest.mark.parametrize("surface,predicate,expected", [
    # An inverted place predicate must not overrule a personal-name shape...
    ("Seyyed Ali Khamenei", "country of citizenship", "person"),
    ("Keir Starmer", "residence", "person"),
    ("Volodymyr Zelensky", "located in", "person"),
    # ... but a single-token unknown place still takes the predicate's word
    # (the guard is shape-only, so recall on real datelines is preserved).
    ("Damietta", "located in", "location"),
    ("Yerevan", "place of birth", "location"),
])
def test_r8_place_predicate_does_not_overrule_person_shape(
    surface, predicate, expected
):
    assert _clf(surface, predicate=predicate, slot="object") == expected


@pytest.mark.parametrize("surface,expected", [
    ("IAEA", "organization"),        # institution acronym, was `entity`
    ("IRGC", "organization"),
    ("TASS", "organization"),        # curated wire agency
    ("COVID", "entity"),             # disease, NOT an org
    ("SARS", "entity"),
    ("GDP", "entity"),               # economic measure
    ("THE", "entity"),               # ALL-CAPS headline residue
])
def test_r8_acronym_typing(surface, expected):
    assert _clf(surface) == expected


@pytest.mark.parametrize("surface,expected", [
    # KEEP-TESTS — the fallbacks and cues the fix must not disturb.
    ("Donald Trump", "person"),
    ("Volodymyr Zelensky", "person"),
    ("Vladimir Putin", "person"),
    ("A. Merkel", "person"),
    ("Michelle Steel", "person"),          # surname/org-suffix collision guard
    ("Emily Post", "person"),              # ... widened with the news heads
    ("Apple Inc.", "corporation"),         # legal form keeps the finer class
    ("Hurricane Helene", "event"),
    ("Severe Thunderstorm Warning", "event"),
    ("the Russian Foreign Ministry", "organization"),
    ("Bank of England", "organization"),
    ("Nippon Steel", "organization"),
])
def test_r8_keeps_existing_classifications(surface, expected):
    assert _clf(surface) == expected


def test_r8_operator_override_still_wins():
    # The taxonomy_map override is applied before every R8 tier.
    assert _clf("Kyiv", overrides={"kyiv": "concept"}) == "concept"


@pytest.mark.asyncio
async def test_r8_classes_reach_the_payload_through_transform():
    # The real binding path: the classes the handler stamps on payload.entities
    # (the same handler `reenrich_ner` builds and calls for the backfill).
    handler_fn, _captured = _make_ner_transport(
        default_triples=[
            {"subject": "White House", "predicate": "located in",
             "object": "Washington"},
            {"subject": "Yekaterinburg", "predicate": "country",
             "object": "Russian Federation"},
            {"subject": "Keir Starmer", "predicate": "head of government",
             "object": "United Kingdom"},
        ],
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(
        text=(
            "White House officials met in Washington. A scientist was beaten in "
            "Yekaterinburg, Russian Federation. Keir Starmer commented."
        ),
        language="en",
    )
    out = await handler.transform(sig, _ctx())
    assert out is not None
    by_text = {e["text"]: e["class"] for e in out.payload["entities"]}
    assert by_text["White House"] == "organization"
    assert by_text["Yekaterinburg"] == "location"
    assert by_text["Russian Federation"] == "country"
    assert by_text["Keir Starmer"] == "person"
    await handler.on_retire(_ctx())


# ===========================================================================
# M12 — telegram payload.text is now NER'd
# ===========================================================================
#
# Regression: telegram signals carry their message body in ``payload.text``
# and leave title/summary/raw_body empty. The pre-fix default ``text_fields``
# (["title","body","summary","raw_body"]) never saw it, so every telegram
# signal short-circuited with entities=[] + ner_language=NULL (7,164 signals
# live, 0 entities). The fix adds ``text`` to the default field set.


def _make_ner_transport(
    *,
    triples_by_text: dict[str, list[dict[str, str]]] | None = None,
    default_triples: list[dict[str, str]] | None = None,
    translations: dict[str, str] | None = None,
    captured: list[httpx.Request] | None = None,
    extract_status: int = 200,
    translate_status: int = 200,
) -> tuple[Any, list[httpx.Request]]:
    """MockTransport handler answering /health, /translate AND /extract.

    ``translations`` maps an input text -> its (canned) English translation;
    ``triples_by_text`` keys on the text /extract actually receives (i.e. the
    *translated* text on the M11 path). ``captured`` records every request so
    tests can assert the /translate-before-/extract ordering + wire bodies.
    """
    triples_by_text = triples_by_text or {}
    default_triples = default_triples or []
    translations = translations or {}
    captured = captured if captured is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "models_loaded": True})
        if path == "/translate":
            if translate_status != 200:
                return httpx.Response(translate_status, json={"error": "translate down"})
            body = json.loads(request.content.decode("utf-8"))
            txt = body.get("text", "")
            return httpx.Response(200, json={
                "translated": translations.get(txt, txt),
                "source_lang": body.get("source_lang"),
                "target_lang": body.get("target_lang", "en"),
                "ms": 1.0,
            })
        if path == "/extract":
            if extract_status != 200:
                return httpx.Response(extract_status, json={"error": "extract down"})
            body = json.loads(request.content.decode("utf-8"))
            txt = body.get("text", "")
            triples = triples_by_text.get(txt, default_triples)
            return httpx.Response(200, json={"triples": triples, "ms": 1.0})
        return httpx.Response(404, json={"error": "unexpected path"})

    return handler, captured


@pytest.mark.asyncio
async def test_m12_telegram_payload_text_yields_entities():
    """A telegram-shaped signal (content only in payload.text) now extracts
    entities and stamps ner_language — the M12 fix."""
    captured: list[httpx.Request] = []
    handler_fn, _ = _make_ner_transport(
        default_triples=[
            {"subject": "Vladimir Putin", "predicate": "head of state",
             "object": "Russia"},
        ],
        captured=captured,
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())

    sig = _signal(text="Vladimir Putin met the visiting delegation.", language="en")
    # Pre-fix: title/body/summary/raw_body all empty -> _extract_text == "".
    out = await handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["entities"], "payload.text must now be NER'd"
    assert out.payload["ner_language"] == "en"

    # The /extract wire body proves payload.text fed the extractor.
    extract_reqs = [r for r in captured if r.url.path == "/extract"]
    assert extract_reqs, "expected an /extract call"
    body = json.loads(extract_reqs[0].content.decode("utf-8"))
    assert body["text"] == "Vladimir Putin met the visiting delegation."
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_m12_empty_telegram_signal_still_drops():
    """An empty telegram message (no text anywhere) still short-circuits —
    the M12 fix must not turn empties into spurious extract calls."""
    captured: list[httpx.Request] = []
    handler_fn, _ = _make_ner_transport(default_triples=[], captured=captured)
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(text="", language="en")
    out = await handler.transform(sig, _ctx())
    assert out.payload["entities"] == []
    assert out.payload["ner_language"] is None
    assert "/extract" not in [r.url.path for r in captured]
    await handler.on_retire(_ctx())


def test_m12_text_in_default_text_fields():
    assert "text" in NERMultilingualConfig().text_fields


# ===========================================================================
# DQ R1 — the extractor's head/tail PAIRS are preserved
# ===========================================================================
#
# /extract returns real (subject, predicate, object) triples. The contractual
# entity list flattens them and DE-DUPES on endpoint text, so an entity claimed
# by an earlier triple is missing from every later one — which destroys the
# pairing. Downstream, fact_extractor re-paired that flat list BY LIST INDEX
# and manufactured relations nobody extracted. The pairs are now stamped
# alongside the entities so nothing has to be guessed back out.


@pytest.mark.asyncio
async def test_relations_preserve_pairs_the_entity_list_loses():
    """The de-dup that broke fact extraction, and the surface that fixes it."""
    handler_fn, _ = _make_extract_handler(
        default_triples=[
            {"subject": "Telegram", "predicate": "located in", "object": "Russia"},
            {"subject": "Telegram", "predicate": "founded by",
             "object": "Pavel Durov"},
        ],
    )
    client = _build_client(handler_fn)
    h = NERMultilingualHandler(
        NERMultilingualConfig(languages=["en", "xx"], default_language="xx"),
        nlp_client=client,
    )
    await h.on_configure(_ctx())
    await h.on_activate(_ctx())
    sig = _signal(
        body="Telegram founder Pavel Durov rejected the demand from Russia.",
        language="en",
    )
    out = await h.transform(sig, _ctx())

    # The lossy half: "Telegram" was claimed by the FIRST triple, so it is
    # absent from the 'founded by' group entirely. Nothing in the flat list
    # still says who founded what.
    founded_by = [
        e for e in out.payload["entities"] if e.get("predicate") == "founded by"
    ]
    assert [e["text"] for e in founded_by] == ["Pavel Durov"], (
        "the entity list drops the de-duped head — pairing is unrecoverable"
    )

    # The fix: the model's own pairs, intact and correctly directed.
    pairs = {
        (r["subject"], r["predicate"], r["object"]) for r in out.payload["relations"]
    }
    assert ("Telegram", "founded by", "Pavel Durov") in pairs
    assert ("Telegram", "located in", "Russia") in pairs
    # The garbage the index-pairing produced from this exact shape.
    assert ("Russia", "founded by", "Pavel Durov") not in pairs
    await h.on_retire(_ctx())


@pytest.mark.asyncio
async def test_relations_carry_offsets_locating_the_object_after_its_subject():
    """Offsets let the fact stage quote the asserting clause as evidence; the
    object resolves to the occurrence AFTER its subject, not the document's
    first mention of that surface."""
    handler_fn, _ = _make_extract_handler(
        default_triples=[
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
    body = "Apple Inc. reported earnings. Tim Cook still runs Apple Inc. today."
    out = await h.transform(_signal(body=body, language="en"), _ctx())

    rel = next(
        r for r in out.payload["relations"] if r["predicate"] == "employer"
    )
    assert body[rel["subject_start"]:rel["subject_end"]] == "Tim Cook"
    assert body[rel["object_start"]:rel["object_end"]] == "Apple Inc."
    # The SECOND "Apple Inc." — the one in Tim Cook's clause.
    assert rel["object_start"] > rel["subject_start"]
    await h.on_retire(_ctx())


@pytest.mark.asyncio
async def test_relations_reject_nonentity_endpoints():
    """A relation whose endpoint the entity list would refuse (bare
    number/date) must not sneak in through the pair surface."""
    handler_fn, _ = _make_extract_handler(
        default_triples=[
            {"subject": "Apple Inc.", "predicate": "employer", "object": "50%"},
            {"subject": "Tim Cook", "predicate": "employer",
             "object": "Apple Inc."},
        ],
    )
    client = _build_client(handler_fn)
    h = NERMultilingualHandler(
        NERMultilingualConfig(languages=["en", "xx"], default_language="xx"),
        nlp_client=client,
    )
    await h.on_configure(_ctx())
    await h.on_activate(_ctx())
    out = await h.transform(
        _signal(body="Tim Cook runs Apple Inc. and holds 50% approval.",
                language="en"),
        _ctx(),
    )
    objects = {r["object"] for r in out.payload["relations"]}
    assert "50%" not in objects
    assert "Apple Inc." in objects
    await h.on_retire(_ctx())


# ===========================================================================
# M11 — non-Latin NER via translate-then-NER
# ===========================================================================
#
# The hosted /extract runs spaCy en_core_web_trf (English-only), so Arabic /
# Cyrillic / CJK text extracts ~0 entities (live: `ar` 1,880 signals / 0 with
# entities). The fix translates non-Latin bodies to English via the hosted
# /translate (NLLB-200) endpoint BEFORE the /extract hop. Latin-script
# languages stay on the direct path.


def test_m11_config_translate_defaults():
    cfg = NERMultilingualConfig()
    assert cfg.translate_before_ner is True
    assert cfg.translate_target_language == "en"
    # Non-Latin scripts routed; Latin-script languages excluded by default.
    assert {"ar", "ru", "uk", "zh", "he"} <= set(cfg.translate_languages)
    assert "fr" not in cfg.translate_languages
    assert "es" not in cfg.translate_languages
    assert "en" not in cfg.translate_languages


def test_m11_script_detection_helpers():
    from legba.data.filters.ner import (
        _dominant_script_lang,
        _is_majority_non_latin,
    )
    assert _is_majority_non_latin("Россия провела переговоры")
    assert _is_majority_non_latin("إسرائيل تشن غارات على غزة")
    assert not _is_majority_non_latin("Israel launches strikes on Gaza")
    assert not _is_majority_non_latin("Le président Macron à Paris")
    assert not _is_majority_non_latin("12345 !!! ---")  # no letters at all
    assert _dominant_script_lang("Россия переговоры") == "ru"
    assert _dominant_script_lang("إسرائيل غزة") == "ar"
    assert _dominant_script_lang("日本の首相は東京で") == "ja"  # kana present
    assert _dominant_script_lang("윤석열 대통령은 서울") == "ko"
    assert _dominant_script_lang("Hello world") is None


@pytest.mark.asyncio
async def test_m11_arabic_translates_then_extracts():
    captured: list[httpx.Request] = []
    arabic = "إسرائيل تشن غارات جوية على غزة"
    english = "Israel launches airstrikes on Gaza"
    handler_fn, _ = _make_ner_transport(
        translations={arabic: english},
        triples_by_text={english: [
            {"subject": "Israel", "predicate": "country", "object": "Gaza"},
        ]},
        captured=captured,
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())

    sig = _signal(text=arabic, language="ar")
    out = await handler.transform(sig, _ctx())

    paths = [r.url.path for r in captured]
    assert "/translate" in paths and "/extract" in paths
    # translate happens BEFORE extract.
    assert paths.index("/translate") < paths.index("/extract")

    # /translate got source_lang=ar, target=en, the original Arabic text.
    treq = [r for r in captured if r.url.path == "/translate"][0]
    tbody = json.loads(treq.content.decode("utf-8"))
    assert tbody["source_lang"] == "ar"
    assert tbody["target_lang"] == "en"
    assert tbody["text"] == arabic

    # /extract saw the ENGLISH translation, not the Arabic original.
    ereq = [r for r in captured if r.url.path == "/extract"][0]
    assert json.loads(ereq.content.decode("utf-8"))["text"] == english

    # Entities extracted; provenance language stays the SOURCE lang (honest —
    # the signal is Arabic even though NER ran on the translation).
    entities = out.payload["entities"]
    assert entities, "Arabic signal must now yield entities via translate-then-NER"
    assert out.payload["ner_language"] == "ar"
    assert all(e["lang"] == "ar" for e in entities)
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_m11_english_signal_not_translated():
    captured: list[httpx.Request] = []
    handler_fn, _ = _make_ner_transport(
        default_triples=[
            {"subject": "Alice", "predicate": "employer", "object": "Acme Corp"},
        ],
        captured=captured,
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(text="Alice works at Acme Corp.", language="en")
    out = await handler.transform(sig, _ctx())
    assert out.payload["entities"]
    assert "/translate" not in [r.url.path for r in captured]
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_m11_latin_script_language_not_translated():
    """French is Latin-script — English spaCy still recognises its proper
    nouns, so it is NOT routed through translate (measured live: fr extracts)."""
    captured: list[httpx.Request] = []
    handler_fn, _ = _make_ner_transport(default_triples=[], captured=captured)
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(text="Le président a rencontré Emmanuel Macron à Paris.", language="fr")
    await handler.transform(sig, _ctx())
    assert "/translate" not in [r.url.path for r in captured]
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_m11_script_fallback_when_language_undetected():
    """language_detect can return 'und'/'xx' on a short non-Latin body. The
    script-detection fallback still routes it to translate (inferring the
    source lang from the dominant script)."""
    captured: list[httpx.Request] = []
    russian = "Россия провела переговоры в Стамбуле"
    english = "Russia held talks in Istanbul"
    handler_fn, _ = _make_ner_transport(
        translations={russian: english},
        triples_by_text={english: [
            {"subject": "Russia", "predicate": "event", "object": "talks"},
        ]},
        captured=captured,
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(text=russian, language="und")  # detection missed it
    out = await handler.transform(sig, _ctx())
    treqs = [r for r in captured if r.url.path == "/translate"]
    assert treqs, "script fallback should have triggered a translate"
    assert json.loads(treqs[0].content.decode("utf-8"))["source_lang"] == "ru"
    assert out.payload["entities"]
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_m11_translate_failure_falls_back_to_direct_extract():
    """A /translate outage must degrade gracefully: fall back to extracting
    the original text (empty for non-Latin, but the signal still flows and the
    gap is counted, not silent)."""
    captured: list[httpx.Request] = []
    arabic = "إسرائيل غزة تصعيد"
    handler_fn, _ = _make_ner_transport(
        translate_status=503,
        default_triples=[],  # English NER on Arabic yields nothing (realistic)
        captured=captured,
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(text=arabic, language="ar")
    out = await handler.transform(sig, _ctx())

    paths = [r.url.path for r in captured]
    assert "/translate" in paths  # attempted
    assert "/extract" in paths    # fell back
    # /extract got the ORIGINAL Arabic (the fallback path).
    ereq = [r for r in captured if r.url.path == "/extract"][0]
    assert json.loads(ereq.content.decode("utf-8"))["text"] == arabic
    assert out.payload["entities"] == []      # empty, but signal passed through
    assert out.payload["ner_language"] == "ar"

    # The failure is COUNTED (visible), not silent.
    health = await handler.health_check(_ctx())
    assert health.detail["translate_failures"] == 1
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_m11_translate_disabled_skips_translation():
    captured: list[httpx.Request] = []
    handler_fn, _ = _make_ner_transport(default_triples=[], captured=captured)
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(
        NERMultilingualConfig(translate_before_ner=False), nlp_client=client,
    )
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(text="إسرائيل غزة", language="ar")
    await handler.transform(sig, _ctx())
    assert "/translate" not in [r.url.path for r in captured]
    await handler.on_retire(_ctx())


@pytest.mark.asyncio
async def test_m11_health_surfaces_translate_counters():
    captured: list[httpx.Request] = []
    arabic = "إسرائيل تشن غارات"
    english = "Israel launches strikes"
    handler_fn, _ = _make_ner_transport(
        translations={arabic: english},
        triples_by_text={english: [
            {"subject": "Israel", "predicate": "country", "object": "Gaza"},
        ]},
        captured=captured,
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    await handler.transform(_signal(text=arabic, language="ar"), _ctx())
    health = await handler.health_check(_ctx())
    assert health.detail["translate_calls"] == 1
    assert health.detail["translate_failures"] == 0
    assert "ar" in health.detail["translate_languages"]
    await handler.on_retire(_ctx())


# ===========================================================================
# B0-7 (2026-07-10) — markdown link/bold syntax is stripped BEFORE /extract
# ===========================================================================
#
# Telegram payload.text carries raw markdown ("[**title**](url)") verbatim.
# Fed to /extract unstripped, the NER emits spans still wearing the syntax
# ("Ayatollah Ali Khamenei**](https://f24.my" — 115 junk entity_profiles
# minted live) including MID-NAME residue ("S**ergey") that the canon's
# name-level junk gate can't reject without losing the referent. The fix is
# a conservative regex pass in _extract_text: "[text](url)" -> text and
# paired "**text**" -> text; legitimate brackets in prose are untouched.


def test_b07_extract_text_strips_markdown_link_and_bold():
    handler = NERMultilingualHandler(NERMultilingualConfig())
    sig = _signal(
        text="[**Middle East live: Iran begins funeral**](https://f24.my/C2Ba.g)"
    )
    assert handler._extract_text(sig) == "Middle East live: Iran begins funeral"


def test_b07_extract_text_strips_midname_bold_residue():
    # The "S**ergey" class — bold markers splitting a name mid-token.
    handler = NERMultilingualHandler(NERMultilingualConfig())
    sig = _signal(text="Foreign Minister S**ergey Lavrov** spoke to reporters.")
    assert handler._extract_text(sig) == (
        "Foreign Minister Sergey Lavrov spoke to reporters."
    )


def test_b07_extract_text_leaves_legitimate_brackets_alone():
    # Conservative pass: prose brackets without "](" adjacency, parentheticals,
    # and unpaired asterisks are NOT rewritten.
    handler = NERMultilingualHandler(NERMultilingualConfig())
    for prose in (
        "The report [sic] said (see appendix) nothing changed.",
        "Indices a[1] (zero-based) are unaffected.",
        "A single * asterisk survives.",
    ):
        assert handler._extract_text(_signal(text=prose)) == prose


def test_b07_extract_text_plain_text_unchanged():
    handler = NERMultilingualHandler(NERMultilingualConfig())
    sig = _signal(title="Plain title", body="Plain body text.")
    assert handler._extract_text(sig) == "Plain title\nPlain body text."


@pytest.mark.asyncio
async def test_b07_extract_wire_body_receives_markdown_free_text():
    """End-to-end: a telegram markdown signal reaches /extract already clean."""
    captured: list[httpx.Request] = []
    handler_fn, _ = _make_ner_transport(
        default_triples=[
            {"subject": "Ayatollah Ali Khamenei", "predicate": "head of state",
             "object": "Iran"},
        ],
        captured=captured,
    )
    client = _build_client(handler_fn)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    await handler.on_configure(_ctx())
    await handler.on_activate(_ctx())
    sig = _signal(
        text="[**Iran begins funeral for Ayatollah Ali Khamenei**](https://f24.my/C2Ba.g)",
        language="en",
    )
    out = await handler.transform(sig, _ctx())
    assert out is not None
    extract_reqs = [r for r in captured if r.url.path == "/extract"]
    assert extract_reqs, "expected an /extract call"
    body = json.loads(extract_reqs[0].content.decode("utf-8"))
    assert body["text"] == "Iran begins funeral for Ayatollah Ali Khamenei"
    assert "](" not in body["text"] and "**" not in body["text"]
    await handler.on_retire(_ctx())
