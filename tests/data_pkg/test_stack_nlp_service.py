# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ``nlp_service`` stack kind + :class:`NlpServiceClient`.

Architectural-drift correction (2026-05-22). The hosted Legba-models NLP
service ships as a stack-component descriptor that the filter handlers
(L-154 NER, L-155 classify) bind to via ``Property.StackRef``.

Tested surface:

  * Schema validation — :class:`NLPServiceConfig` accepts the canonical
    field set and rejects extras.
  * Stack-kind registration — ``"nlp_service"`` is in ``KIND_MODELS``;
    schema_uri is recognised by ``kind_from_schema_uri``.
  * Health-checker registration — the dispatcher has an
    ``"nlp_service"`` checker that issues GET /health and maps
    HTTP statuses to :class:`HealthState`.
  * :class:`NlpServiceClient` — typed methods + graceful degradation
    behaviour via ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from legba.data.registry.health import (
    HEALTH_CHECKERS,
    HealthState,
    NLPServiceChecker,
    ResolvedConfig,
)
from legba.data.registry.stack import KIND_MODELS, kind_from_schema_uri
from legba.data.schemas.properties import Property
from legba.data.schemas.stack import NLPService, NLPServiceConfig
from legba.data.stack.nlp_service import (
    NlpServiceAuthError,
    NlpServiceClient,
    NlpServiceUnavailable,
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestNLPServiceSchema:
    def test_config_accepts_canonical_fields(self) -> None:
        cfg = NLPServiceConfig(
            endpoint=Property.Text.of("https://models.test.invalid"),
            api_user=Property.Secret.of("nlp.test.api_user"),
            api_pass=Property.Secret.of("nlp.test.api_pass"),
            timeout_seconds=Property.Number.of(60, minimum=1, maximum=600),
        )
        assert cfg.endpoint.raw == "https://models.test.invalid"
        # Paths default to /translate /classify /extract /summarize /health.
        assert cfg.translate_path.raw == "/translate"
        assert cfg.classify_path.raw == "/classify"
        assert cfg.extract_path.raw == "/extract"
        assert cfg.summarize_path.raw == "/summarize"
        assert cfg.health_path.raw == "/health"

    def test_config_rejects_unknown_field(self) -> None:
        with pytest.raises(Exception):
            NLPServiceConfig(
                endpoint=Property.Text.of("https://x.invalid"),
                bogus="surprise",  # type: ignore[call-arg]
            )

    def test_top_level_descriptor_validates(self) -> None:
        comp = NLPService(
            id="nlp.test.legba_models",
            name="Test NLP service",
            schema_uri="legba/stack/nlp_service/1.0.0",
            version="0" * 16,
            owner="lewis@local",
            config=NLPServiceConfig(
                endpoint=Property.Text.of("https://models.test.invalid"),
                api_user=Property.Secret.of("nlp.test.api_user"),
                api_pass=Property.Secret.of("nlp.test.api_pass"),
            ),
        )
        assert comp.id == "nlp.test.legba_models"
        assert comp.schema_uri == "legba/stack/nlp_service/1.0.0"


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


class TestKindCatalog:
    def test_nlp_service_in_kind_models(self) -> None:
        assert "nlp_service" in KIND_MODELS
        assert KIND_MODELS["nlp_service"] is NLPService

    def test_schema_uri_parses_to_kind(self) -> None:
        assert kind_from_schema_uri("legba/stack/nlp_service/1.0.0") == "nlp_service"

    def test_health_checker_registered(self) -> None:
        assert "nlp_service" in HEALTH_CHECKERS
        assert isinstance(HEALTH_CHECKERS["nlp_service"], NLPServiceChecker)


# ---------------------------------------------------------------------------
# NlpServiceClient — typed methods + graceful degradation
# ---------------------------------------------------------------------------


def _build_client(handler: Any, **kwargs: Any) -> NlpServiceClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(
        base_url=kwargs.get("base_url", "https://models.test.invalid"),
        transport=transport,
        auth=httpx.BasicAuth("u", "p"),
        timeout=5.0,
    )
    return NlpServiceClient(
        endpoint=kwargs.get("base_url", "https://models.test.invalid"),
        api_user="u",
        api_pass="p",
        client=inner,
    )


@pytest.mark.asyncio
class TestNlpServiceClient:
    async def test_classify_returns_raw_dict(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/classify"
            body = json.loads(request.content.decode("utf-8"))
            assert body["text"] == "Alice works at Acme Corp."
            assert body["labels"] == ["foo", "bar"]
            return httpx.Response(200, json={
                "category": "foo", "confidence": 0.91,
                "scores": {"foo": 0.91, "bar": 0.09},
                "ms": 50.0,
            })

        client = _build_client(h)
        data = await client.classify("Alice works at Acme Corp.", labels=["foo", "bar"])
        assert data["category"] == "foo"
        assert data["confidence"] == 0.91

    async def test_classify_omits_labels_when_none(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            assert "labels" not in body
            return httpx.Response(200, json={
                "category": "conflict", "confidence": 0.9, "scores": {}, "ms": 50.0,
            })

        client = _build_client(h)
        data = await client.classify("Some text")
        assert data["category"] == "conflict"

    async def test_extract_returns_triples(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/extract"
            return httpx.Response(200, json={
                "triples": [{"subject": "A", "predicate": "loves", "object": "B"}],
                "ms": 800.0,
            })

        client = _build_client(h)
        data = await client.extract("A loves B")
        assert data["triples"][0]["object"] == "B"

    async def test_translate_returns_translation(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/translate"
            return httpx.Response(200, json={
                "translated": "hello world", "source_lang": "es",
                "target_lang": "en", "ms": 600.0,
            })

        client = _build_client(h)
        data = await client.translate("hola mundo", source_lang="es")
        assert data["translated"] == "hello world"

    async def test_5xx_raises_unavailable(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="down")

        client = _build_client(h)
        with pytest.raises(NlpServiceUnavailable):
            await client.classify("anything")

    async def test_401_raises_auth_error(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "unauthorized"})

        client = _build_client(h)
        with pytest.raises(NlpServiceAuthError):
            await client.classify("anything")

    async def test_health_returns_dict_on_ok(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok", "models_loaded": True})

        client = _build_client(h)
        data = await client.health()
        assert data["status"] == "ok"

    async def test_health_401_raises_auth(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        client = _build_client(h)
        with pytest.raises(NlpServiceAuthError):
            await client.health()

    async def test_network_error_raises_unavailable(self) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated")

        client = _build_client(h)
        with pytest.raises(NlpServiceUnavailable):
            await client.classify("anything")


# ---------------------------------------------------------------------------
# Health checker
# ---------------------------------------------------------------------------


class _StubResolver:
    """Minimal credentials resolver returning fixed bytes for any secret_id."""

    def __init__(self, user: bytes | None = b"u", pw: bytes | None = b"p") -> None:
        self._user = user
        self._pw = pw

    async def verify_exists(self, secret_id: str) -> bool:
        return True

    async def resolve(self, secret_id: str) -> bytes:
        if "user" in secret_id:
            assert self._user is not None
            return self._user
        assert self._pw is not None
        return self._pw


def _config(**overrides: Any) -> NLPServiceConfig:
    base = dict(
        endpoint=Property.Text.of("https://models.test.invalid"),
        api_user=Property.Secret.of("nlp.test.api_user"),
        api_pass=Property.Secret.of("nlp.test.api_pass"),
    )
    base.update(overrides)
    return NLPServiceConfig(**base)


def _patch_async_client(monkeypatch, handler) -> None:
    """Patch ``httpx.AsyncClient.__init__`` to inject a MockTransport.

    The NLPServiceChecker constructs its own ``httpx.AsyncClient`` inside
    ``check()`` via ``async with``. We swap the constructor to slot in a
    MockTransport while keeping the rest of the AsyncClient API intact.
    """
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


@pytest.mark.asyncio
class TestNLPServiceChecker:
    async def test_health_ok_maps_to_healthy(self, monkeypatch) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok", "models_loaded": True})

        _patch_async_client(monkeypatch, h)
        cfg = _config()
        resolved = ResolvedConfig(config=cfg, resolver=_StubResolver())
        checker = NLPServiceChecker()
        result = await checker.check("nlp.test.legba_models", resolved)
        assert result.state == HealthState.HEALTHY

    async def test_401_maps_to_degraded(self, monkeypatch) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        _patch_async_client(monkeypatch, h)
        cfg = _config()
        resolved = ResolvedConfig(config=cfg, resolver=_StubResolver())
        checker = NLPServiceChecker()
        result = await checker.check("nlp.test.legba_models", resolved)
        assert result.state == HealthState.DEGRADED
        assert "401" in result.detail

    async def test_network_error_maps_to_unhealthy(self, monkeypatch) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated")

        _patch_async_client(monkeypatch, h)
        cfg = _config()
        resolved = ResolvedConfig(config=cfg, resolver=_StubResolver())
        checker = NLPServiceChecker()
        result = await checker.check("nlp.test.legba_models", resolved)
        assert result.state == HealthState.UNHEALTHY
        assert "failed" in result.detail
