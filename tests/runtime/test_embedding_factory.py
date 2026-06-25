# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :mod:`legba.runtime.embedding_factory`.

Coverage:

  * Happy path: mocked vLLM ``/v1/embeddings`` response yields a vector.
  * 401 auth-rejection path raises :class:`EmbeddingFactoryError`.
  * Malformed response (no ``data`` list) raises
    :class:`EmbeddingFactoryError`.
  * Registry 404 path raises a clear error.
  * Vault failure path raises a clear error.
  * Live ``embed.primary.openai_compat`` path against the
    real hosted endpoint, gated on ``LEGBA_TEST_LIVE_EMBEDDING=1`` so it
    only runs with vault credentials available.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from legba.runtime.embedding_factory import (
    EmbeddingFactoryError,
    HostedEmbeddingClient,
    build_embedding_service_from_stack_component,
)
from legba.runtime.registry_client import RegistryHTTPClient


_LIVE_EMBED_GATE = os.environ.get("LEGBA_TEST_LIVE_EMBEDDING") == "1"

skip_unless_live_embedding = pytest.mark.skipif(
    not _LIVE_EMBED_GATE,
    reason=(
        "LEGBA_TEST_LIVE_EMBEDDING=1 not set; skipping live embedding "
        "endpoint test"
    ),
)


# ---------------------------------------------------------------------------
# Registry fixtures
# ---------------------------------------------------------------------------


_COMPONENT_ID = "embed.primary.openai_compat"
_VERSION = "0" * 64
_API_KEY_SECRET_ID = "embed.primary.api_key"


def _embedding_body(
    endpoint: str,
    *,
    component_id: str = _COMPONENT_ID,
    model_name: str = "bge-m3",
    dim: int = 1024,
    include_api_key: bool = True,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "endpoint": {"factory_kind": "text", "raw": endpoint},
        "model_name": {"factory_kind": "text", "raw": model_name},
        "dim": {"factory_kind": "number", "raw": dim},
        "normalize": {
            "factory_kind": "dropdown_static",
            "raw": "true",
            "options": ["true", "false"],
        },
        "batch_size": {"factory_kind": "number", "raw": 64},
    }
    if include_api_key:
        config["api_key"] = {
            "factory_kind": "secret",
            "raw": _API_KEY_SECRET_ID,
        }
    return {
        "id": component_id,
        "version": _VERSION,
        "body": {
            "id": component_id,
            "name": "self-hosted bge-m3 embeddings",
            "schema_uri": "legba/stack/embedding/1.0.0",
            "version": _VERSION,
            "owner": "test",
            "state": "active",
            "config": config,
        },
    }


def _registry_client(
    *,
    response_factory,
    component_id: str = _COMPONENT_ID,
) -> RegistryHTTPClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/stack/{component_id}"), (
            f"unexpected path: {request.url.path}"
        )
        return response_factory(request)

    transport = httpx.MockTransport(_handler)
    inner = httpx.AsyncClient(
        transport=transport, base_url="http://registry.test",
    )
    return RegistryHTTPClient(
        base_url="http://registry.test", token=None, client=inner,
    )


async def _stub_secrets(secret_id: str) -> bytes:
    assert secret_id == _API_KEY_SECRET_ID
    return b"test-api-key-bytes"


async def _failing_secrets(secret_id: str) -> bytes:
    raise RuntimeError(f"vault unavailable for {secret_id!r}")


# ---------------------------------------------------------------------------
# Factory construction tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_embedding_service_happy_construction() -> None:
    """Registry row + valid secret → a configured HostedEmbeddingClient."""
    body = _embedding_body("https://llm.example.internal/v1")
    client = _registry_client(
        response_factory=lambda _req: httpx.Response(200, json=body),
    )
    try:
        svc = await build_embedding_service_from_stack_component(
            _COMPONENT_ID,
            registry_client=client,
            secrets_resolve=_stub_secrets,
        )
    finally:
        await client.close()
    assert isinstance(svc, HostedEmbeddingClient)
    assert svc.dim == 1024
    assert svc.normalize is True
    assert svc.batch_size == 64
    # The api_key is decoded from bytes — never log it, but the
    # internal field is the plaintext we'll send in the Bearer header.
    assert svc._api_key == "test-api-key-bytes"  # noqa: SLF001
    await svc.aclose()


@pytest.mark.asyncio
async def test_build_embedding_service_404_raises() -> None:
    """Registry 404 on lookup → typed factory error."""
    client = _registry_client(
        response_factory=lambda _req: httpx.Response(404),
    )
    try:
        with pytest.raises(EmbeddingFactoryError, match="not found"):
            await build_embedding_service_from_stack_component(
                _COMPONENT_ID,
                registry_client=client,
                secrets_resolve=_stub_secrets,
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_build_embedding_service_vault_failure_raises() -> None:
    """A vault that can't resolve the api_key surfaces a typed error."""
    body = _embedding_body("https://llm.example.internal/v1")
    client = _registry_client(
        response_factory=lambda _req: httpx.Response(200, json=body),
    )
    try:
        with pytest.raises(EmbeddingFactoryError, match="vault"):
            await build_embedding_service_from_stack_component(
                _COMPONENT_ID,
                registry_client=client,
                secrets_resolve=_failing_secrets,
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_build_embedding_service_missing_config_raises() -> None:
    """Missing ``body.config`` raises a clear error."""
    bad_body = {
        "id": _COMPONENT_ID,
        "version": _VERSION,
        "body": {"id": _COMPONENT_ID, "name": "broken"},
    }
    client = _registry_client(
        response_factory=lambda _req: httpx.Response(200, json=bad_body),
    )
    try:
        with pytest.raises(EmbeddingFactoryError, match="config"):
            await build_embedding_service_from_stack_component(
                _COMPONENT_ID,
                registry_client=client,
                secrets_resolve=_stub_secrets,
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_build_embedding_service_anonymous_endpoint() -> None:
    """A config without an api_key is allowed (internal docker net)."""
    body = _embedding_body(
        "http://embeddings.internal:8000/v1", include_api_key=False,
    )
    client = _registry_client(
        response_factory=lambda _req: httpx.Response(200, json=body),
    )
    try:
        # ``secrets_resolve`` would raise if called — verify the
        # factory short-circuits when api_key is absent.
        svc = await build_embedding_service_from_stack_component(
            _COMPONENT_ID,
            registry_client=client,
            secrets_resolve=_failing_secrets,
        )
    finally:
        await client.close()
    assert isinstance(svc, HostedEmbeddingClient)
    assert svc._api_key == ""  # noqa: SLF001
    await svc.aclose()


# ---------------------------------------------------------------------------
# embed() — happy path with mocked httpx
# ---------------------------------------------------------------------------


def _make_client_with_transport(transport: httpx.MockTransport) -> HostedEmbeddingClient:
    """Build a HostedEmbeddingClient wired to a MockTransport."""
    inner = httpx.AsyncClient(transport=transport)
    return HostedEmbeddingClient(
        endpoint="https://llm.example.internal/v1",
        api_key="test-bearer",
        model_name="bge-m3",
        dim=1024,
        normalize=True,
        batch_size=64,
        http_client=inner,
    )


@pytest.mark.asyncio
async def test_embed_happy_path_parses_openai_response() -> None:
    """The OpenAI-shaped response is parsed into a list[float]."""
    expected = [0.1, -0.2, 0.3, 0.4]

    def _handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        assert request.method == "POST"
        assert request.url.path.endswith("/v1/embeddings")
        assert request.headers["Authorization"] == "Bearer test-bearer"
        payload = _json.loads(request.content)
        assert payload["model"] == "bge-m3"
        assert payload["input"] == "hello world"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{
                    "object": "embedding",
                    "index": 0,
                    "embedding": expected,
                }],
                "model": "bge-m3",
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    svc = _make_client_with_transport(httpx.MockTransport(_handler))
    try:
        vec = await svc.embed("hello world")
    finally:
        await svc.aclose()
    assert vec == expected
    assert all(isinstance(x, float) for x in vec)


@pytest.mark.asyncio
async def test_embed_endpoint_without_v1_suffix_appends_path() -> None:
    """A bare host endpoint gets ``/v1/embeddings`` appended."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/embeddings")
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [0.0, 1.0]}],
            },
        )

    inner = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    svc = HostedEmbeddingClient(
        endpoint="https://llm.example.internal",
        api_key="k",
        model_name="m",
        dim=2,
        normalize=False,
        batch_size=1,
        http_client=inner,
    )
    try:
        vec = await svc.embed("x")
    finally:
        await svc.aclose()
    assert vec == [0.0, 1.0]


# ---------------------------------------------------------------------------
# embed() — 401 / 403 path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_401_raises_factory_error() -> None:
    """A 401 on the embedding endpoint raises a typed error."""

    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "invalid api key"}},
        )

    svc = _make_client_with_transport(httpx.MockTransport(_handler))
    try:
        with pytest.raises(EmbeddingFactoryError, match="rejected credentials"):
            await svc.embed("x")
    finally:
        await svc.aclose()


# ---------------------------------------------------------------------------
# embed() — malformed responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_malformed_response_no_data_raises() -> None:
    """A response without a ``data`` list raises a typed error."""

    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": []})

    svc = _make_client_with_transport(httpx.MockTransport(_handler))
    try:
        with pytest.raises(EmbeddingFactoryError, match="data"):
            await svc.embed("x")
    finally:
        await svc.aclose()


@pytest.mark.asyncio
async def test_embed_malformed_response_no_embedding_raises() -> None:
    """A ``data[0]`` item without an ``embedding`` raises a typed error."""

    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"object": "embedding", "index": 0}]},
        )

    svc = _make_client_with_transport(httpx.MockTransport(_handler))
    try:
        with pytest.raises(EmbeddingFactoryError, match="embedding"):
            await svc.embed("x")
    finally:
        await svc.aclose()


@pytest.mark.asyncio
async def test_embed_malformed_response_non_json_raises() -> None:
    """A non-JSON response body raises a typed error."""

    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    svc = _make_client_with_transport(httpx.MockTransport(_handler))
    try:
        with pytest.raises(EmbeddingFactoryError, match="non-JSON"):
            await svc.embed("x")
    finally:
        await svc.aclose()


# ---------------------------------------------------------------------------
# Live path — only runs with LEGBA_TEST_LIVE_EMBEDDING=1 + vault creds
# ---------------------------------------------------------------------------


@skip_unless_live_embedding
@pytest.mark.asyncio
async def test_build_embedding_service_live_against_hosted(
    migrated_pg,  # type: ignore[no-untyped-def]
) -> None:
    """LEGBA_TEST_LIVE_EMBEDDING=1 — hit the real hosted embedding endpoint.

    Resolves the api_key via the production :class:`CredentialVault`
    against the migrated test Postgres (which the data_pkg fixture
    populates with the same vault schema the runtime uses). The test
    needs ``LEGBA_DATA_MASTER_KEY`` set + a vault row at
    ``embed.primary.api_key`` — same prerequisites as the runtime.

    The body is the real ``embed.primary.openai_compat`` row
    cached in code; if the production row's endpoint / model_name
    drifts, update this constant rather than mocking the registry.
    """
    from legba.data.registry.credentials import CredentialVault
    from legba.data.postgres import PostgresStore

    # Reuse the data_pkg migrated_pg fixture's connection: it already
    # has the vault schema migrated. The fixture yields a
    # PostgresStore-shaped object.
    pg_store: PostgresStore = migrated_pg  # type: ignore[assignment]
    vault = CredentialVault(pg_store)

    async def _resolve(secret_id: str) -> bytes:
        return await vault.resolve(secret_id)

    body = _embedding_body(
        "https://llm.example.internal/v1",
        model_name=os.environ.get(
            "LEGBA_TEST_LIVE_EMBEDDING_MODEL",
            "bge-m3",
        ),
    )
    client = _registry_client(
        response_factory=lambda _req: httpx.Response(200, json=body),
    )
    try:
        svc = await build_embedding_service_from_stack_component(
            _COMPONENT_ID,
            registry_client=client,
            secrets_resolve=_resolve,
        )
    finally:
        await client.close()
    try:
        vec = await svc.embed("legba live embedding factory test")
    finally:
        await svc.aclose()
    assert isinstance(vec, list)
    assert len(vec) == svc.dim
    assert all(isinstance(x, float) for x in vec)
