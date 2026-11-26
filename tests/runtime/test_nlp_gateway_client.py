# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""the hosted-NLP client contract on the model gateway.

legba-models (GLiREL/spaCy/DeBERTa/NLLB) is dead since 2026-10-22; the
gateway serves entities, relations and zero-shot classify. These tests pin
that :class:`GatewayNlpClient` answers in the legacy client's shapes and
raises its exceptions, so ``ner_multilingual``, ``fact_extractor`` and
``classify`` run on it unchanged — the NER and fact-extractor cases traverse
the REAL handler code over ``httpx.MockTransport`` (nothing on the handler is
patched). No network, no container, no registry: the gateway's shapes are the
ones verified live 2026-10-23.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
import yaml

from legba.data.schemas.stack import NLPService, NLPServiceConfig
from legba.data.stack.nlp_service import (
    GatewayNlpClient,
    NlpServiceAuthError,
    NlpServiceClient,
    NlpServiceUnavailable,
    NlpServiceUnsupported,
)
from legba.data.stack.nlp_service.gateway import (
    DEFAULT_CLASSIFY_CATEGORIES,
    DEFAULT_ENTITY_LABELS,
    DEFAULT_RELATION_LABELS,
    MAX_ATTEMPTS,
)
from legba.runtime.nlp_client_factory import (
    DEFAULT_NLP_COMPONENT_ID,
    NlpClientFactoryError,
    build_nlp_client_from_stack_component,
    resolve_nlp_component_id,
)
from legba.runtime.registry_client import RegistryHTTPClient
from tests._gateway_env import GATEWAY_ENV

_REPO = Path(__file__).resolve().parents[2]
_YAML = _REPO / "descriptors" / "stack_component_nlp_gateway_bearer.yaml"
_COMPONENT_ID = "nlp.gateway.bearer"
_BASE = "https://gw.test/v1"
_TOKEN = "tok-test"
_TEXT = "Cosmos 2612, operated by the Russian military, approached a US satellite."

_RELATIONS_WITH_CONF = {
    "model": "ner",
    "results": [{
        "relation_extraction": {
            "operated_by": [{
                "head": {"text": "Cosmos 2612", "confidence": 0.977},
                "tail": {"text": "Russian military", "confidence": 0.788},
            }],
            "leader_of": [{
                "head": {"text": "Vladimir Putin", "confidence": 0.95},
                "tail": {"text": "Russia", "confidence": 0.91},
            }],
            "supplies to": [],
        },
    }],
}


class _Gateway:
    """A scripted gateway: one queued response per call, every request kept."""

    def __init__(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return nxt(request) if callable(nxt) else nxt

    def body(self, i: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def _client(gw: _Gateway, **kw: Any) -> tuple[GatewayNlpClient, list[float]]:
    slept: list[float] = []

    async def _sleep(s: float) -> None:
        slept.append(s)

    inner = httpx.AsyncClient(transport=httpx.MockTransport(gw), base_url=_BASE)
    return GatewayNlpClient(
        endpoint=_BASE, api_key=_TOKEN, client=inner, sleep=_sleep, **kw,
    ), slept


# ---------------------------------------------------------------------------
# extract() -> /relations -> the legacy triple shape
# ---------------------------------------------------------------------------


async def test_extract_maps_relations_with_confidence_to_legacy_triples():
    gw = _Gateway(httpx.Response(200, json=_RELATIONS_WITH_CONF))
    client, _ = _client(gw)
    data = await client.extract(_TEXT)

    assert len(gw.requests) == 1, "extract is ONE gateway call"
    req = gw.requests[0]
    assert req.method == "POST" and req.url.path == "/v1/relations"
    assert req.headers["authorization"] == f"Bearer {_TOKEN}"
    assert gw.body() == {
        "model": "ner",
        "text": _TEXT,
        "relations": list(DEFAULT_RELATION_LABELS),
        "include_confidence": True,
        "threshold": 0.5,  # W11-F113 default
    }
    assert isinstance(data["ms"], float)
    triples = data["triples"]
    assert triples[0] == {
        "subject": "Cosmos 2612",
        # An unconfigured key keeps the lowercase-spaced surface the fact path keys on.
        "predicate": "operated by",
        "object": "Russian military",
        "subject_start": None, "subject_end": None,
        "object_start": None, "object_end": None,
        "subject_confidence": 0.977, "object_confidence": 0.788,
        # Min(head, tail) — the relation is as sure as its weaker end.
        "confidence": 0.788,
    }
    # A configured label comes back as the CONFIGURED surface, whatever the
    # separator the gateway keyed it by.
    assert triples[1]["predicate"] == "leader of"
    assert (triples[1]["subject"], triples[1]["object"]) == ("Vladimir Putin", "Russia")
    assert triples[1]["confidence"] == 0.91
    assert len(triples) == 2
    await client.aclose()


async def test_extract_maps_plain_pairs_without_confidence():
    body = {"model": "ner", "results": [{"relation_extraction": {
        "approached": [["Cosmos 2612", "US satellite"]],
        "supplies_to": [["Iran", "Hezbollah"], ["", "nobody"]],
    }}]}
    gw = _Gateway(httpx.Response(200, json=body))
    client, _ = _client(gw, relation_labels=["approached", "supplies to"])
    triples = (await client.extract(_TEXT))["triples"]
    assert gw.body()["relations"] == ["approached", "supplies to"]
    assert [(t["subject"], t["predicate"], t["object"]) for t in triples] == [
        ("Cosmos 2612", "approached", "US satellite"),
        ("Iran", "supplies to", "Hezbollah"),
    ]
    assert all("subject_confidence" not in t for t in triples)


async def test_extract_on_an_empty_answer_is_zero_triples():
    gw = _Gateway(httpx.Response(200, json={"model": "ner", "results": []}))
    client, _ = _client(gw)
    assert (await client.extract(_TEXT))["triples"] == []


# ---------------------------------------------------------------------------
# entities()
# ---------------------------------------------------------------------------


async def test_entities_maps_the_per_label_block():
    body = {"model": "ner", "results": [{"entities": {
        "person": [{"text": "Deputy Prime Minister Salvini", "confidence": 0.904,
                    "start": 0, "end": 29}],
        "satellite": [{"text": "Cosmos 2612", "confidence": 0.99}],
        "location": [],
    }}]}
    gw = _Gateway(httpx.Response(200, json=body))
    client, _ = _client(gw)
    out = await client.entities("Deputy Prime Minister Salvini said Cosmos 2612 ...")
    assert gw.requests[0].url.path == "/v1/entities"
    sent = gw.body()
    assert sent["model"] == "ner" and sent["entities"] == list(DEFAULT_ENTITY_LABELS)
    assert sent["include_confidence"] is True
    assert out["entities"] == [
        {"text": "Deputy Prime Minister Salvini", "label": "person",
         "confidence": 0.904, "start": 0, "end": 29},
        {"text": "Cosmos 2612", "label": "satellite", "confidence": 0.99,
         "start": None, "end": None},
    ]
    assert "entity" not in DEFAULT_ENTITY_LABELS


# ---------------------------------------------------------------------------
# errors, retries, Retry-After
# ---------------------------------------------------------------------------


async def test_422_names_the_field_and_is_unavailable_without_retry():
    gw = _Gateway(httpx.Response(422, json={"error": {
        "message": "entities: Field required", "type": "invalid_request_error"}}))
    client, slept = _client(gw)
    with pytest.raises(NlpServiceUnavailable) as exc:
        await client.entities(_TEXT)
    assert not isinstance(exc.value, NlpServiceAuthError)
    assert "entities: Field required" in str(exc.value)
    assert _TEXT not in str(exc.value), "request text never lands in an error"
    assert len(gw.requests) == 1 and slept == []


async def test_a_validation_body_echoing_the_input_is_not_repeated():
    gw = _Gateway(httpx.Response(422, json={"detail": [
        {"loc": ["body", "text"], "msg": "too long", "input": _TEXT}]}))
    client, _ = _client(gw)
    with pytest.raises(NlpServiceUnavailable) as exc:
        await client.extract(_TEXT)
    assert "body.text: too long" in str(exc.value)
    assert _TEXT not in str(exc.value)


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_are_the_auth_error(status):
    gw = _Gateway(httpx.Response(status, json={"error": {"message": "bad key"}}))
    client, _ = _client(gw)
    with pytest.raises(NlpServiceAuthError):
        await client.extract(_TEXT)
    assert len(gw.requests) == 1


async def test_429_with_retry_after_is_honoured_then_retried_once():
    gw = _Gateway(
        httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {
            "message": "rate limited", "type": "rate_limit", "limit": "concurrency"}}),
        httpx.Response(200, json=_RELATIONS_WITH_CONF),
    )
    client, slept = _client(gw)
    data = await client.extract(_TEXT)
    assert len(gw.requests) == 2
    assert slept == [2.0]
    assert len(data["triples"]) == 2


async def test_a_retry_after_past_the_client_ceiling_fails_now():
    gw = _Gateway(httpx.Response(429, headers={"Retry-After": "60"},
                                 json={"error": {"message": "tokens_per_day"}}))
    client, slept = _client(gw)
    with pytest.raises(NlpServiceUnavailable, match="Retry-After"):
        await client.extract(_TEXT)
    assert len(gw.requests) == 1 and slept == []


async def test_502_is_unavailable_after_the_bounded_retries():
    gw = _Gateway(httpx.Response(502, text="<html>bad gateway</html>"))
    client, slept = _client(gw)
    with pytest.raises(NlpServiceUnavailable, match="502"):
        await client.extract(_TEXT)
    assert len(gw.requests) == MAX_ATTEMPTS == 3
    assert slept == [0.5, 1.0]


async def test_a_transport_error_is_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    gw = _Gateway(boom)
    client, _ = _client(gw)
    with pytest.raises(NlpServiceUnavailable):
        await client.extract(_TEXT)
    assert len(gw.requests) == MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# translate with no translate_model / summarize -> the callers' degrade paths
# (translation on the chat model: tests/runtime/test_nlp_gateway_translate.py)
# ---------------------------------------------------------------------------


async def test_translate_raises_unsupported_without_a_network_call():
    gw = _Gateway(httpx.Response(200, json={}))
    client, _ = _client(gw)
    assert client.supports_translate is False
    with pytest.raises(NlpServiceUnsupported) as exc:
        await client.translate("Привет", source_lang="ru")
    assert isinstance(exc.value, NlpServiceUnavailable)
    with pytest.raises(NlpServiceUnavailable):
        await client.summarize(["a"])
    assert gw.requests == []


async def test_reenrich_translation_wiring_degrades_to_skipped_no_nlp():
    from legba.data.analysts.deterministic_handlers import reenrich_translation
    from legba.runtime.analyst_deps_builder import _wire_reenrich_translation

    client, _ = _client(_Gateway(httpx.Response(200, json={})))
    deps = SimpleNamespace(extras={}, pg_pool=object())
    descriptor = SimpleNamespace(identity=SimpleNamespace(id="analyst.reenrich_translation"))
    wired = await _wire_reenrich_translation(descriptor, deps, nlp_client=client)
    assert wired is deps, "the gateway client is not wired into the translate sweep"

    result = await reenrich_translation.handle([], {}, wired)
    assert result.finding.data["skipped_no_nlp"] == 1
    assert result.finding.data["failures"] == 0


# ---------------------------------------------------------------------------
# classify() -> the legacy {category, confidence, scores, ms}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {"model": "classify", "results": [{"labels": ["conflict", "economy"], "scores": [0.8, 0.2]}]},
    {"model": "classify", "results": [{"label": "conflict", "score": 0.8},
                                      {"label": "economy", "score": 0.2}]},
    {"model": "classify", "results": [{"scores": {"conflict": 0.8, "economy": 0.2}}]},
    {"labels": ["economy", "conflict"], "scores": [0.2, 0.8]},
])
async def test_classify_reads_the_plausible_shapes(body):
    gw = _Gateway(httpx.Response(200, json=body))
    client, _ = _client(gw)
    out = await client.classify("Shelling resumed overnight.", labels=["conflict", "economy"])
    assert gw.requests[0].url.path == "/v1/classify"
    assert gw.body() == {"model": "classify", "input": "Shelling resumed overnight.",
                         "labels": ["conflict", "economy"]}
    assert out["category"] == "conflict"
    assert out["confidence"] == 0.8
    assert out["scores"] == {"conflict": 0.8, "economy": 0.2}


async def test_classify_label_only_answer_and_server_defaults():
    long_label = "armed conflict or military action"
    gw = _Gateway(httpx.Response(200, json={"results": [{"label": long_label}]}))
    client, _ = _client(gw)
    out = await client.classify("Shelling resumed overnight.")
    assert gw.body()["labels"] == list(DEFAULT_CLASSIFY_CATEGORIES)
    assert out["category"] == "conflict", "server-default labels map to their short names"
    assert out["confidence"] == 1.0


async def test_classify_unrecognised_shape_is_unavailable():
    gw = _Gateway(httpx.Response(200, json={"weird": True}))
    client, _ = _client(gw)
    with pytest.raises(NlpServiceUnavailable, match="unrecognised"):
        await client.classify("x", labels=["a"])


async def test_health_synthesises_the_legacy_dict():
    gw = _Gateway(httpx.Response(200, json={"object": "list", "data": [
        {"id": "ner"}, {"id": "classify"}]}))
    client, _ = _client(gw)
    out = await client.health()
    assert gw.requests[0].method == "GET" and gw.requests[0].url.path == "/v1/models"
    assert out["status"] == "ok" and out["models_loaded"] is True
    assert out["models"] == ["ner", "classify"]


# ---------------------------------------------------------------------------
# The extractors run on it unchanged (real handler code, mock transport)
# ---------------------------------------------------------------------------


def _ctx():
    from legba.data.filters._contract import FilterContext

    return FilterContext(
        target_id="t.test", filter_id="gw_test",
        logger=logging.getLogger("test.nlp_gateway"),
        scope_geo=[], scope_languages=["en"],
    )


def _signal(text: str, language: str = "en"):
    from legba.data.sources import Signal

    return Signal(source_id="src.test", payload={"body": text, "language": language},
                  content_hash="abc", canonical_url=None)


async def test_ner_handler_emits_entities_and_relations_off_the_gateway():
    from legba.data.filters.ner import NERMultilingualConfig, NERMultilingualHandler

    gw = _Gateway(httpx.Response(200, json={"model": "ner", "results": [{
        "relation_extraction": {"leader_of": [{
            "head": {"text": "Vladimir Putin", "confidence": 0.95},
            "tail": {"text": "Russia", "confidence": 0.91}}]}}]}))
    client, _ = _client(gw)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    out = await handler.transform(
        _signal("Vladimir Putin, the leader of Russia, spoke today."), _ctx(),
    )
    texts = {e["text"] for e in out.payload["entities"]}
    assert {"Vladimir Putin", "Russia"} <= texts
    rels = out.payload["relations"]
    assert [(r["subject"], r["predicate"], r["object"]) for r in rels] == [
        ("Vladimir Putin", "leader of", "Russia")]
    # The min endpoint score rides through NER's relation surface.
    assert rels[0]["confidence"] == 0.91
    assert handler._signals_failed == 0  # noqa: SLF001


async def test_ner_handler_non_latin_signal_degrades_translate_and_still_extracts():
    from legba.data.filters.ner import NERMultilingualConfig, NERMultilingualHandler

    gw = _Gateway(httpx.Response(200, json={"model": "ner", "results": [{
        "relation_extraction": {}}]}))
    client, _ = _client(gw)
    handler = NERMultilingualHandler(NERMultilingualConfig(), nlp_client=client)
    out = await handler.transform(_signal("Путин заявил сегодня", language="ru"), _ctx())
    assert handler._translate_failures >= 1  # noqa: SLF001 — the degrade path
    assert [r.url.path for r in gw.requests] == ["/v1/relations"], "no translate call"
    assert out.payload["entities"] == []


async def test_fact_extractor_fallback_reads_gateway_triples():
    from legba.data.filters.fact_extractor import FactExtractorConfig, FactExtractorHandler

    gw = _Gateway(httpx.Response(200, json=_RELATIONS_WITH_CONF))
    client, _ = _client(gw)
    handler = FactExtractorHandler(FactExtractorConfig(), pg_pool=object(), nlp_client=client)
    triples = await handler._extract_relation(_signal(_TEXT), _ctx())  # noqa: SLF001
    assert [(t["subject"], t["predicate"], t["object"]) for t in triples] == [
        ("Cosmos 2612", "operated by", "Russian military"),
        ("Vladimir Putin", "leader of", "Russia"),
    ]

    gw_down = _Gateway(httpx.Response(503, json={"error": {"message": "bounce"}}))
    down, _ = _client(gw_down)
    handler = FactExtractorHandler(FactExtractorConfig(), pg_pool=object(), nlp_client=down)
    assert await handler._extract_relation(_signal(_TEXT), _ctx()) == []  # noqa: SLF001
    assert handler._degraded_this_call is True  # noqa: SLF001


# ---------------------------------------------------------------------------
# Schema, descriptor, factory, selection knob
# ---------------------------------------------------------------------------


def test_the_descriptor_validates_and_is_registered():
    body = yaml.safe_load(_YAML.read_text())
    model = NLPService.model_validate(body)
    assert model.id == _COMPONENT_ID
    assert model.state.value == "draft"
    assert model.schema_uri == "legba/stack/nlp_service/1.0.0"
    cfg = model.config
    # The endpoint is deployment data: the descriptor names the variable.
    assert cfg.endpoint.raw == "${env:LEGBA_GATEWAY_API_ENDPOINT}"
    assert cfg.api_key.raw == "llm.gateway.api_key"
    assert cfg.provider.raw == "bearer_gateway"
    assert cfg.timeout_seconds.raw == 30
    assert cfg.api_user is None and cfg.api_pass is None

    tree = ast.parse((_REPO / "scripts" / "bringup_register_stack.py").read_text())
    listed: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_STACK_DESCRIPTOR_FILES" for t in node.targets
        ):
            listed = [e.value for e in node.value.elts]
    assert _YAML.name in listed


def _legacy_config() -> dict[str, Any]:
    """The live nlp.local.legba_models config shape (bringup_register_stack)."""
    t = lambda s: {"factory_kind": "text", "raw": s}  # noqa: E731
    return {
        "endpoint": t("https://nlp.example.internal"),
        "api_user": {"factory_kind": "secret", "raw": "nlp.local.legba_models.api_user"},
        "api_pass": {"factory_kind": "secret", "raw": "nlp.local.legba_models.api_pass"},
        "timeout_seconds": {"factory_kind": "number", "raw": 60},
        "translate_path": t("/translate"), "classify_path": t("/classify"),
        "extract_path": t("/extract"), "summarize_path": t("/summarize"),
        "health_path": t("/health"),
    }


def test_the_legacy_component_still_validates_unchanged():
    cfg = NLPServiceConfig.model_validate(_legacy_config())
    assert cfg.provider is None and cfg.api_key is None
    assert cfg.relation_labels is None and cfg.entity_labels is None


@pytest.mark.parametrize("patch, needle", [
    ({"provider": {"factory_kind": "text", "raw": "bearer_gateway"}}, "requires api_key"),
    ({"api_key": {"factory_kind": "secret", "raw": "llm.gateway.api_key"}}, "api_user/api_pass"),
    ({"provider": {"factory_kind": "text", "raw": "spacy"}}, "must be"),
])
def test_provider_and_auth_mode_must_agree(patch, needle):
    cfg = _legacy_config()
    cfg.update(patch)
    with pytest.raises(ValueError, match=needle):
        NLPServiceConfig.model_validate(cfg)


def test_the_gateway_with_a_basic_pair_is_refused():
    body = yaml.safe_load(_YAML.read_text())["config"]
    body["api_user"] = {"factory_kind": "secret", "raw": "x.user"}
    body["api_pass"] = {"factory_kind": "secret", "raw": "x.pass"}
    with pytest.raises(ValueError, match="Bearer-only"):
        NLPServiceConfig.model_validate(body)


def _registry(component_id: str, body: dict[str, Any]) -> RegistryHTTPClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/stack/{component_id}")
        return httpx.Response(200, json={"id": component_id, "version": "0" * 16, "body": body})

    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://registry.test")
    return RegistryHTTPClient(base_url="http://registry.test", token=None, client=inner)


async def test_the_factory_builds_the_gateway_client_from_the_descriptor():
    body = yaml.safe_load(_YAML.read_text())
    body["config"]["relation_labels"] = {
        "factory_kind": "list", "raw": ["approached", "operated by"], "item_kind": "text"}
    resolved: list[str] = []

    async def secrets(secret_id: str) -> bytes:
        resolved.append(secret_id)
        return _TOKEN.encode()

    client = await build_nlp_client_from_stack_component(
        _COMPONENT_ID, registry_client=_registry(_COMPONENT_ID, body), secrets_resolve=secrets,
    )
    assert isinstance(client, GatewayNlpClient)
    assert resolved == ["llm.gateway.api_key"]
    assert client._endpoint == GATEWAY_ENV["LEGBA_GATEWAY_API_ENDPOINT"]  # noqa: SLF001
    assert client._health_path == "/models"  # noqa: SLF001
    assert client._timeout == 30.0  # noqa: SLF001
    assert client.relation_labels == ["approached", "operated by"]
    assert client.entity_labels == list(DEFAULT_ENTITY_LABELS)
    assert client._client is None, "nothing dialled at build"  # noqa: SLF001


async def test_the_factory_keeps_building_the_legacy_client_by_default():
    body = {"id": DEFAULT_NLP_COMPONENT_ID, "name": "x",
            "schema_uri": "legba/stack/nlp_service/1.0.0", "version": "0" * 16,
            "owner": "t", "state": "active", "config": _legacy_config()}

    async def secrets(secret_id: str) -> bytes:
        return b"u" if secret_id.endswith("user") else b"p"

    client = await build_nlp_client_from_stack_component(
        DEFAULT_NLP_COMPONENT_ID,
        registry_client=_registry(DEFAULT_NLP_COMPONENT_ID, body), secrets_resolve=secrets,
    )
    assert type(client) is NlpServiceClient
    assert getattr(client, "supports_translate", True) is True


@pytest.mark.parametrize("mutate, needle", [
    (lambda c: c.pop("api_key"), "requires an api_key"),
    (lambda c: c.__setitem__("provider", {"factory_kind": "text", "raw": "spacy"}),
     "unknown provider"),
])
async def test_the_factory_fails_loud_on_a_bad_gateway_row(mutate, needle):
    body = yaml.safe_load(_YAML.read_text())
    mutate(body["config"])

    async def secrets(secret_id: str) -> bytes:
        return b"k"

    with pytest.raises(NlpClientFactoryError, match=needle):
        await build_nlp_client_from_stack_component(
            _COMPONENT_ID, registry_client=_registry(_COMPONENT_ID, body),
            secrets_resolve=secrets,
        )


def test_the_component_id_knob(monkeypatch):
    monkeypatch.delenv("LEGBA_NLP_COMPONENT_ID", raising=False)
    assert resolve_nlp_component_id() == DEFAULT_NLP_COMPONENT_ID == "nlp.local.legba_models"
    monkeypatch.setenv("LEGBA_NLP_COMPONENT_ID", "  ")
    assert resolve_nlp_component_id() == DEFAULT_NLP_COMPONENT_ID
    monkeypatch.setenv("LEGBA_NLP_COMPONENT_ID", "nlp.gateway.bearer")
    assert resolve_nlp_component_id() == "nlp.gateway.bearer"


async def test_the_registry_health_checker_probes_the_gateway_with_bearer(monkeypatch):
    from legba.data.registry.health import HealthState, StackHealthDispatcher

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"object": "list", "data": [{"id": "ner"}]})

    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    class _Resolver:
        async def resolve(self, secret_id: str) -> bytes:
            assert secret_id == "llm.gateway.api_key"
            return _TOKEN.encode()

    # Through the dispatcher, which resolves the ${env:NAME} endpoint first.
    cfg = NLPService.model_validate(yaml.safe_load(_YAML.read_text())).config
    result = await StackHealthDispatcher().check_component(
        _COMPONENT_ID, "nlp_service", cfg, _Resolver(),
    )
    assert result.state == HealthState.HEALTHY, result.detail
    assert str(seen[0].url) == f"{GATEWAY_ENV['LEGBA_GATEWAY_API_ENDPOINT']}/models"
    assert seen[0].headers["authorization"] == f"Bearer {_TOKEN}"


@pytest.fixture(autouse=True)
def _gateway_env(monkeypatch):
    """The gateway descriptors name their endpoint and models as ${env:NAME}."""
    from tests._gateway_env import set_gateway_env

    set_gateway_env(monkeypatch)
