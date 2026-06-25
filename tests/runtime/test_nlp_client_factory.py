# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :mod:`legba.runtime.nlp_client_factory`.

Covers:

  * Happy path — registry returns a well-formed stack-component row,
    secrets resolve to UTF-8 bytes, the factory returns a configured
    :class:`NlpServiceClient` with the right endpoint + BasicAuth.
  * Happy path without auth — both secret refs absent → client builds
    without ``api_user`` / ``api_pass`` (internal-docker contract).
  * Per-endpoint path overrides are threaded through from config.
  * 404 stack component → :class:`NlpClientFactoryError` with the
    component id in the message.
  * Asymmetric auth refs (``api_user`` set, ``api_pass`` absent) →
    :class:`NlpClientFactoryError`.
  * Missing / non-mapping ``body.config`` → clear error.
  * Missing ``endpoint`` field → clear error.
  * Vault resolve failure → wrapped error with the secret id.
  * Direct-httpx fallback path (registry client without
    ``get_stack_component`` method) — exercised via a monkeypatched
    ``httpx.AsyncClient`` so the test doesn't need a live registry.
  * ``RegistryClientError`` → wrapped as :class:`NlpClientFactoryError`.

Gated live test (``LEGBA_TEST_LIVE_NLP=1``):

  * Hits the real legba-registry container at
    ``LEGBA_REGISTRY_API_URL`` and the real hosted-endpoint stack
    component.  Skipped by default so unit runs stay hermetic.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from legba.data.stack.nlp_service import NlpServiceClient
from legba.runtime.nlp_client_factory import (
    DEFAULT_NLP_COMPONENT_ID,
    LazyNlpClient,
    NlpClientFactoryError,
    build_nlp_client_from_stack_component,
)
from legba.runtime.registry_client import (
    RegistryClientError,
    RegistryHTTPClient,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


_VERSION = "0" * 64
_COMPONENT_ID = DEFAULT_NLP_COMPONENT_ID
_ENDPOINT = "https://nlp.example.internal"
_USER_SECRET_ID = "nlp.local.api_user"
_PASS_SECRET_ID = "nlp.local.api_pass"


def _full_body(
    *,
    endpoint: str = _ENDPOINT,
    include_auth: bool = True,
    include_path_overrides: bool = False,
) -> dict[str, Any]:
    """Build a well-formed stack-component row body.

    Matches :class:`legba.data.schemas.stack.NLPService` — FactoryValue
    dicts for every field, ``schema_uri`` set, ``state=active``.
    """
    config: dict[str, Any] = {
        "endpoint": {"factory_kind": "text", "raw": endpoint},
        "timeout_seconds": {"factory_kind": "number", "raw": 45},
    }
    if include_auth:
        config["api_user"] = {
            "factory_kind": "secret", "raw": _USER_SECRET_ID,
        }
        config["api_pass"] = {
            "factory_kind": "secret", "raw": _PASS_SECRET_ID,
        }
    else:
        config["api_user"] = None
        config["api_pass"] = None
    if include_path_overrides:
        config["translate_path"] = {
            "factory_kind": "text", "raw": "/v2/translate",
        }
        config["classify_path"] = {
            "factory_kind": "text", "raw": "/v2/classify",
        }
        config["extract_path"] = {
            "factory_kind": "text", "raw": "/v2/extract",
        }
        config["summarize_path"] = {
            "factory_kind": "text", "raw": "/v2/summarize",
        }
        config["health_path"] = {
            "factory_kind": "text", "raw": "/v2/health",
        }
    return {
        "id": _COMPONENT_ID,
        "version": _VERSION,
        "body": {
            "id": _COMPONENT_ID,
            "name": "Hosted Legba-models NLP",
            "schema_uri": "legba/stack/nlp_service/1.0.0",
            "version": _VERSION,
            "owner": "test",
            "state": "active",
            "config": config,
        },
    }


def _registry_client_with(
    component_id: str,
    body: dict[str, Any] | None,
    *,
    status_code: int = 200,
) -> RegistryHTTPClient:
    """Build a :class:`RegistryHTTPClient` wired to return ``body`` for the
    expected stack-component path.

    Constructing via the public surface (``client=inner``) so
    ``get_stack_component`` (the method under exercise) runs end to end
    against the mock transport.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        expected_suffix = f"/stack/{component_id}"
        assert request.url.path.endswith(expected_suffix), (
            f"unexpected request path: {request.url.path!r} "
            f"(expected suffix {expected_suffix!r})"
        )
        if status_code == 404:
            return httpx.Response(404)
        if status_code >= 400:
            return httpx.Response(status_code, text="boom")
        assert body is not None
        return httpx.Response(status_code, json=body)

    transport = httpx.MockTransport(_handler)
    inner = httpx.AsyncClient(
        transport=transport, base_url="http://registry.test",
    )
    return RegistryHTTPClient(
        base_url="http://registry.test", token=None, client=inner,
    )


class _SecretsStore:
    """Tiny in-memory vault stand-in.

    Records call ordering so tests can assert both refs were resolved.
    """

    def __init__(self, table: dict[str, bytes]) -> None:
        self._table = table
        self.calls: list[str] = []

    async def __call__(self, secret_id: str) -> bytes:
        self.calls.append(secret_id)
        try:
            return self._table[secret_id]
        except KeyError as exc:
            raise KeyError(f"unknown secret_id {secret_id!r}") from exc


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_nlp_client_happy_path() -> None:
    """Well-formed registry row + working secrets → configured client.

    Asserts the endpoint, BasicAuth, timeout, and default paths are
    threaded through correctly, and that the client did NOT open a
    socket at construction time (per the no-bootstrap-health-probe
    rule).
    """
    body = _full_body()
    client = _registry_client_with(_COMPONENT_ID, body)
    secrets = _SecretsStore({
        _USER_SECRET_ID: b"alice",
        _PASS_SECRET_ID: b"hunter2",
    })

    nlp = await build_nlp_client_from_stack_component(
        _COMPONENT_ID,
        registry_client=client,
        secrets_resolve=secrets,
    )

    assert isinstance(nlp, NlpServiceClient)
    assert nlp._endpoint == _ENDPOINT
    # Both secrets resolved, in the order the factory declared them.
    assert sorted(secrets.calls) == sorted(
        [_USER_SECRET_ID, _PASS_SECRET_ID]
    )
    # BasicAuth was constructed (api_user + api_pass both present).
    assert isinstance(nlp._auth, httpx.BasicAuth)
    # Timeout reflects the config (45, not the 60s default).
    assert nlp._timeout == 45.0
    # Default paths preserved.
    assert nlp._classify_path == "/classify"
    assert nlp._extract_path == "/extract"
    assert nlp._translate_path == "/translate"
    assert nlp._summarize_path == "/summarize"
    assert nlp._health_path == "/health"
    # NO httpx.AsyncClient was opened by the constructor.
    assert nlp._client is None

    await client.close()


@pytest.mark.asyncio
async def test_build_nlp_client_no_auth_internal_path() -> None:
    """Both secret refs absent → no BasicAuth wrapper (internal docker)."""
    body = _full_body(
        endpoint="http://legba-models:8700", include_auth=False,
    )
    client = _registry_client_with(_COMPONENT_ID, body)
    secrets = _SecretsStore({})

    nlp = await build_nlp_client_from_stack_component(
        _COMPONENT_ID,
        registry_client=client,
        secrets_resolve=secrets,
    )

    assert isinstance(nlp, NlpServiceClient)
    assert nlp._endpoint == "http://legba-models:8700"
    # No secret resolves happened.
    assert secrets.calls == []
    assert nlp._auth is None

    await client.close()


@pytest.mark.asyncio
async def test_build_nlp_client_path_overrides() -> None:
    """Per-endpoint path overrides flow through to the client."""
    body = _full_body(include_path_overrides=True)
    client = _registry_client_with(_COMPONENT_ID, body)
    secrets = _SecretsStore({
        _USER_SECRET_ID: b"alice",
        _PASS_SECRET_ID: b"hunter2",
    })

    nlp = await build_nlp_client_from_stack_component(
        _COMPONENT_ID,
        registry_client=client,
        secrets_resolve=secrets,
    )

    assert nlp._translate_path == "/v2/translate"
    assert nlp._classify_path == "/v2/classify"
    assert nlp._extract_path == "/v2/extract"
    assert nlp._summarize_path == "/v2/summarize"
    assert nlp._health_path == "/v2/health"

    await client.close()


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_nlp_client_404_raises() -> None:
    """Registry 404 surfaces a legible NlpClientFactoryError."""
    client = _registry_client_with(_COMPONENT_ID, None, status_code=404)
    secrets = _SecretsStore({})

    with pytest.raises(NlpClientFactoryError, match="not found"):
        await build_nlp_client_from_stack_component(
            _COMPONENT_ID,
            registry_client=client,
            secrets_resolve=secrets,
        )
    # No secret resolves on the failure path.
    assert secrets.calls == []
    await client.close()


@pytest.mark.asyncio
async def test_build_nlp_client_asymmetric_auth_raises() -> None:
    """``api_user`` set but ``api_pass`` absent → operator-error message."""
    body = _full_body()
    # Remove only api_pass.
    body["body"]["config"]["api_pass"] = None
    client = _registry_client_with(_COMPONENT_ID, body)
    secrets = _SecretsStore({_USER_SECRET_ID: b"alice"})

    with pytest.raises(
        NlpClientFactoryError, match="api_user and api_pass",
    ):
        await build_nlp_client_from_stack_component(
            _COMPONENT_ID,
            registry_client=client,
            secrets_resolve=secrets,
        )
    # Asymmetry check fires before any secret resolves.
    assert secrets.calls == []
    await client.close()


@pytest.mark.asyncio
async def test_build_nlp_client_missing_config_raises() -> None:
    """``body.config`` missing entirely → schema-shape error."""
    body = _full_body()
    del body["body"]["config"]
    client = _registry_client_with(_COMPONENT_ID, body)

    with pytest.raises(
        NlpClientFactoryError, match="body.config is missing",
    ):
        await build_nlp_client_from_stack_component(
            _COMPONENT_ID,
            registry_client=client,
            secrets_resolve=_SecretsStore({}),
        )
    await client.close()


@pytest.mark.asyncio
async def test_build_nlp_client_missing_endpoint_raises() -> None:
    """``config.endpoint`` missing → clear error."""
    body = _full_body()
    del body["body"]["config"]["endpoint"]
    client = _registry_client_with(_COMPONENT_ID, body)

    with pytest.raises(
        NlpClientFactoryError, match="config.endpoint is",
    ):
        await build_nlp_client_from_stack_component(
            _COMPONENT_ID,
            registry_client=client,
            secrets_resolve=_SecretsStore({}),
        )
    await client.close()


@pytest.mark.asyncio
async def test_build_nlp_client_vault_resolve_failure_wraps() -> None:
    """Vault raise propagates as NlpClientFactoryError with secret id."""
    body = _full_body()
    client = _registry_client_with(_COMPONENT_ID, body)

    async def _broken_vault(secret_id: str) -> bytes:
        raise RuntimeError(f"vault is down for {secret_id}")

    with pytest.raises(
        NlpClientFactoryError, match="vault resolve failed",
    ):
        await build_nlp_client_from_stack_component(
            _COMPONENT_ID,
            registry_client=client,
            secrets_resolve=_broken_vault,
        )
    await client.close()


@pytest.mark.asyncio
async def test_build_nlp_client_registry_5xx_wrapped() -> None:
    """Registry 5xx → RegistryClientError → wrapped as factory error."""
    client = _registry_client_with(_COMPONENT_ID, None, status_code=503)
    with pytest.raises(
        NlpClientFactoryError, match="registry lookup failed",
    ):
        await build_nlp_client_from_stack_component(
            _COMPONENT_ID,
            registry_client=client,
            secrets_resolve=_SecretsStore({}),
        )
    await client.close()


# ---------------------------------------------------------------------------
# Fallback path — registry_client missing get_stack_component method
# ---------------------------------------------------------------------------


class _LegacyRegistryClient:
    """Stand-in for a long-running RegistryHTTPClient instance that
    pre-dates the :meth:`get_stack_component` method.

    The factory must fall back to direct httpx — exercised here via a
    monkeypatched ``httpx.AsyncClient`` so we don't need a live socket.
    """


@pytest.mark.asyncio
async def test_build_nlp_client_fallback_to_direct_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry client without ``get_stack_component`` → direct httpx GET.

    Verifies the fallback honors ``LEGBA_REGISTRY_API_URL`` /
    ``LEGBA_REGISTRY_API_TOKEN`` and that a malformed response surfaces
    the same :class:`NlpClientFactoryError`.
    """
    monkeypatch.setenv(
        "LEGBA_REGISTRY_API_URL", "http://registry.fallback.test",
    )
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", "dev-token")

    body = _full_body()
    seen_url: dict[str, str] = {}
    seen_auth: dict[str, str | None] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_url["path"] = request.url.path
        seen_url["host"] = request.url.host
        seen_auth["bearer"] = request.headers.get("Authorization")
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(_handler)

    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    # Patch httpx.AsyncClient in the factory module's import-local
    # scope.  The factory does ``import httpx`` inside the fallback
    # branch (sandbox-cascade rule), so we patch the global module.
    monkeypatch.setattr(httpx, "AsyncClient", _patched_async_client)

    secrets = _SecretsStore({
        _USER_SECRET_ID: b"alice", _PASS_SECRET_ID: b"hunter2",
    })
    legacy = _LegacyRegistryClient()

    nlp = await build_nlp_client_from_stack_component(
        _COMPONENT_ID,
        registry_client=legacy,  # type: ignore[arg-type]
        secrets_resolve=secrets,
    )

    assert isinstance(nlp, NlpServiceClient)
    assert nlp._endpoint == _ENDPOINT
    # Fallback hit the env-var-driven URL with the env-driven token.
    assert seen_url["host"] == "registry.fallback.test"
    assert seen_url["path"].endswith(f"/stack/{_COMPONENT_ID}")
    assert seen_auth["bearer"] == "Bearer dev-token"


@pytest.mark.asyncio
async def test_build_nlp_client_fallback_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback 404 surfaces NlpClientFactoryError just like the
    primary path."""
    monkeypatch.setenv(
        "LEGBA_REGISTRY_API_URL", "http://registry.fallback.test",
    )
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    real_async_client = httpx.AsyncClient

    def _patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched)

    with pytest.raises(NlpClientFactoryError, match="not found"):
        await build_nlp_client_from_stack_component(
            _COMPONENT_ID,
            registry_client=_LegacyRegistryClient(),  # type: ignore[arg-type]
            secrets_resolve=_SecretsStore({}),
        )


# ---------------------------------------------------------------------------
# Construction does not call health() — guard against regression.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_nlp_client_does_not_probe_health() -> None:
    """The factory must not invoke health() at bootstrap (L-102 §1).

    Spy on :meth:`NlpServiceClient.health` via a class-level mock — the
    factory should never hit it.
    """
    body = _full_body()
    client = _registry_client_with(_COMPONENT_ID, body)
    secrets = _SecretsStore({
        _USER_SECRET_ID: b"alice", _PASS_SECRET_ID: b"hunter2",
    })

    health_mock = AsyncMock(return_value={"status": "should-not-be-called"})

    # Patch the bound method via mock.patch.object would require
    # constructing an instance first; the factory creates the instance
    # internally, so we patch the class symbol.
    import legba.data.stack.nlp_service.client as nlp_module

    original = nlp_module.NlpServiceClient.health
    nlp_module.NlpServiceClient.health = health_mock  # type: ignore[assignment]
    try:
        nlp = await build_nlp_client_from_stack_component(
            _COMPONENT_ID,
            registry_client=client,
            secrets_resolve=secrets,
        )
    finally:
        nlp_module.NlpServiceClient.health = original  # type: ignore[assignment]

    health_mock.assert_not_awaited()
    assert isinstance(nlp, NlpServiceClient)
    await client.close()


# ---------------------------------------------------------------------------
# LazyNlpClient — lazy resolve + retry / re-resolution (#91 §2.3)
# ---------------------------------------------------------------------------


class _ToggleRegistry:
    """Registry stand-in whose get_stack_component is operator-toggleable.

    Starts unavailable (raises ``RegistryClientError`` → the builder wraps it
    as ``NlpClientFactoryError``). Flip :attr:`available` to True to simulate
    a late seed / a recovered models-host. Records call count so the test can
    assert a successful build is cached (no further lookups) and a failed one
    is retried.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.available = False
        self.calls = 0

    async def get_stack_component(self, component_id: str) -> dict[str, Any]:
        self.calls += 1
        if not self.available:
            raise RegistryClientError(
                f"registry unavailable for {component_id!r} (boot-before-seed)"
            )
        return self._body


@pytest.mark.asyncio
async def test_lazy_nlp_client_resolves_on_first_use() -> None:
    """No build happens until get(); the first get() builds + caches."""
    registry = _ToggleRegistry(_full_body(include_auth=False))
    registry.available = True
    secrets = _SecretsStore({})

    lazy = LazyNlpClient(
        registry_client=registry,  # type: ignore[arg-type]
        secrets_resolve=secrets,
    )
    # Nothing built at construction — no registry round-trip yet.
    assert lazy.cached is None
    assert registry.calls == 0

    nlp = await lazy.get()
    assert isinstance(nlp, NlpServiceClient)
    assert lazy.cached is nlp
    assert registry.calls == 1


@pytest.mark.asyncio
async def test_lazy_nlp_client_caches_success() -> None:
    """A successful build is cached — repeat get() does NOT re-resolve."""
    registry = _ToggleRegistry(_full_body(include_auth=False))
    registry.available = True
    lazy = LazyNlpClient(
        registry_client=registry,  # type: ignore[arg-type]
        secrets_resolve=_SecretsStore({}),
    )

    first = await lazy.get()
    second = await lazy.get()
    third = await lazy.get()

    assert first is second is third
    # Built exactly once despite three get() calls.
    assert registry.calls == 1


@pytest.mark.asyncio
async def test_lazy_nlp_client_re_resolves_after_failure() -> None:
    """A failed attempt is NOT cached as a sticky None — the next get() retries.

    This is the #91 §2.3 fix: boot-before-seed / a transient models-host
    outage at boot must not pin the client degraded for the process lifetime.
    """
    registry = _ToggleRegistry(_full_body(include_auth=False))
    # Boot-before-seed: the stack component isn't registered yet.
    registry.available = False
    lazy = LazyNlpClient(
        registry_client=registry,  # type: ignore[arg-type]
        secrets_resolve=_SecretsStore({}),
    )

    # First attempt fails loud (NOT a silent None).
    with pytest.raises(NlpClientFactoryError):
        await lazy.get()
    assert lazy.cached is None
    assert registry.calls == 1

    # Second attempt while still unavailable — RE-resolves (retries), still fails.
    with pytest.raises(NlpClientFactoryError):
        await lazy.get()
    assert lazy.cached is None
    assert registry.calls == 2

    # The operator seeds the component / the host recovers — the NEXT get()
    # heals: builds + caches the client.
    registry.available = True
    nlp = await lazy.get()
    assert isinstance(nlp, NlpServiceClient)
    assert lazy.cached is nlp
    assert registry.calls == 3

    # And stays cached thereafter.
    again = await lazy.get()
    assert again is nlp
    assert registry.calls == 3


@pytest.mark.asyncio
async def test_lazy_nlp_client_concurrent_first_use_builds_once() -> None:
    """A burst of concurrent first-use get()s fans out ONE registry build."""
    import asyncio

    registry = _ToggleRegistry(_full_body(include_auth=False))
    registry.available = True
    lazy = LazyNlpClient(
        registry_client=registry,  # type: ignore[arg-type]
        secrets_resolve=_SecretsStore({}),
    )

    results = await asyncio.gather(*[lazy.get() for _ in range(8)])

    # All callers got the SAME client, built exactly once under the lock.
    assert all(r is results[0] for r in results)
    assert registry.calls == 1


# ---------------------------------------------------------------------------
# Gated live test — talks to the real registry + real hosted endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEGBA_TEST_LIVE_NLP") != "1",
    reason="set LEGBA_TEST_LIVE_NLP=1 to run against the real registry + hosted",
)
async def test_build_nlp_client_live() -> None:
    """Build against the real legba-registry + real hosted NLP endpoint.

    Requirements (the operator wires these — no auto-bring-up):

      * ``LEGBA_REGISTRY_API_URL`` points at a running legba-registry
        with the ``nlp.local.legba_models`` stack-component
        installed.
      * ``LEGBA_REGISTRY_API_TOKEN`` is valid (or registry is in dev
        mode).
      * ``LEGBA_DATA_MASTER_KEY`` is set so the production
        :class:`CredentialVault` can resolve the secret refs.

    On success the test runs a single ``health()`` probe against the
    hosted endpoint to confirm the end-to-end wiring.  The probe is
    outside the factory contract — we run it here to validate the live
    path, not inside the factory itself (per L-102 §1).
    """
    # Build a real RegistryHTTPClient + a real CredentialVault.  Both
    # are heavy imports — keep them local to the live branch so unit
    # runs don't pay the cost.
    from legba.data.config import PostgresConfig
    from legba.data.postgres import PostgresStore
    from legba.data.registry.credentials import CredentialVault

    pg_store = PostgresStore(PostgresConfig.from_env())
    await pg_store.connect()
    try:
        vault = CredentialVault(pg_store)

        async def _vault_resolve(secret_id: str) -> bytes:
            return await vault.resolve(secret_id)

        registry_client = RegistryHTTPClient()
        try:
            nlp = await build_nlp_client_from_stack_component(
                DEFAULT_NLP_COMPONENT_ID,
                registry_client=registry_client,
                secrets_resolve=_vault_resolve,
            )
            health = await nlp.health()
            # The exact body varies per legba-models version; just
            # assert it's a non-empty dict.
            assert isinstance(health, dict) and health
            await nlp.aclose()
        finally:
            await registry_client.close()
    finally:
        await pg_store.disconnect()
