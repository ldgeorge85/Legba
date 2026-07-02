# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Switchable LLM provider auth — Bearer OR HTTP Basic (backward-compatible).

The verify judge (self-hosted Llama-3.1-8B) sits behind Caddy HTTP Basic
auth, but the historical handler sent only ``Authorization: Bearer``. Auth is
now a SWITCH: a component uses Bearer (``api_key``) OR HTTP Basic
(``api_user`` + ``api_pass``). Both must work, and every existing
``api_key``-only component must keep sending the identical Bearer header.

Precedence (implemented in ``LLMProviderHandler._auth_headers``): when BOTH
modes are configured, Basic wins.

All deterministic — the vault secret resolution is mocked; no live network.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import pytest

from legba.data.registry.credentials import (
    CredentialResolverProtocol,
    MissingSecretError,
)
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import HardLLMFailure, OpenAIProviderHandler


# ---------------------------------------------------------------------------
# Fakes (mirror tests/data_pkg/test_stack_llm.py)
# ---------------------------------------------------------------------------


class _FakeResolver:
    """Stand-in for `CredentialResolverProtocol`. Returns the configured
    plaintext bytes; raises `MissingSecretError` for an unknown id."""

    def __init__(self, secrets: dict[str, bytes]):
        self._secrets = secrets

    async def verify_exists(self, secret_id: str) -> bool:
        return secret_id in self._secrets

    async def resolve(self, secret_id: str) -> bytes:
        if secret_id not in self._secrets:
            raise MissingSecretError(secret_id)
        return self._secrets[secret_id]


@dataclass
class _FakeCtx:
    instance_id: str
    instance_version: str
    config: LLMProviderConfig
    secrets: CredentialResolverProtocol

    def telemetry(self):
        return _TelStub()


class _TelStub:
    def log(self, level, msg, /, **fields):
        pass

    def event(self, name, payload=None):
        pass

    def span(self, name, /, **attrs):
        class _S:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _S()


def _cfg(
    *,
    api_key: str | None = None,
    api_user: str | None = None,
    api_pass: str | None = None,
    endpoint: str = "https://slm.internal",
) -> LLMProviderConfig:
    kwargs: dict = {
        "api_endpoint": Property.Text.of(endpoint),
        "model_name": Property.Text.of("llama-3.1-8b"),
        "max_tokens": Property.Number.of(1024, minimum=1, maximum=200000),
    }
    if api_key is not None:
        kwargs["api_key"] = Property.Secret.of(api_key)
    if api_user is not None:
        kwargs["api_user"] = Property.Secret.of(api_user)
    if api_pass is not None:
        kwargs["api_pass"] = Property.Secret.of(api_pass)
    return LLMProviderConfig(**kwargs)


def _ctx(cfg: LLMProviderConfig, secrets: dict[str, bytes]) -> _FakeCtx:
    return _FakeCtx(
        instance_id="llm.slm.test",
        instance_version="0" * 16,
        config=cfg,
        secrets=_FakeResolver(secrets),
    )


# ---------------------------------------------------------------------------
# Bearer (historical) path — backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_only_sends_bearer_unchanged():
    """Existing components (api_key only) send the identical Bearer header."""
    h = OpenAIProviderHandler()
    cfg = _cfg(api_key="slm.api_key")
    await h.on_configure(_ctx(cfg, {"slm.api_key": b"sk-bearer-token"}))

    assert h._api_key == "sk-bearer-token"  # noqa: SLF001
    assert h._api_user is None  # noqa: SLF001
    assert h._api_pass is None  # noqa: SLF001

    headers = h._auth_headers()  # noqa: SLF001
    assert headers["Authorization"] == "Bearer sk-bearer-token"
    assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# HTTP Basic path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_only_sends_basic_b64():
    """api_user + api_pass only → Basic header with correct base64(user:pass);
    on_configure resolves BOTH credentials from the vault."""
    h = OpenAIProviderHandler()
    cfg = _cfg(api_user="slm.user", api_pass="slm.pass")
    await h.on_configure(
        _ctx(cfg, {"slm.user": b"caddy_user", "slm.pass": b"caddy_pw"})
    )

    assert h._api_key is None  # noqa: SLF001
    assert h._api_user == "caddy_user"  # noqa: SLF001
    assert h._api_pass == "caddy_pw"  # noqa: SLF001

    expected = base64.b64encode(b"caddy_user:caddy_pw").decode("ascii")
    headers = h._auth_headers()  # noqa: SLF001
    assert headers["Authorization"] == f"Basic {expected}"
    assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# Precedence — both configured → Basic wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_present_basic_wins():
    """When both auth modes are configured, Basic takes precedence."""
    h = OpenAIProviderHandler()
    cfg = _cfg(api_key="slm.api_key", api_user="slm.user", api_pass="slm.pass")
    await h.on_configure(
        _ctx(
            cfg,
            {
                "slm.api_key": b"sk-bearer-token",
                "slm.user": b"caddy_user",
                "slm.pass": b"caddy_pw",
            },
        )
    )

    expected = base64.b64encode(b"caddy_user:caddy_pw").decode("ascii")
    headers = h._auth_headers()  # noqa: SLF001
    assert headers["Authorization"] == f"Basic {expected}"
    assert "Bearer" not in headers["Authorization"]


# ---------------------------------------------------------------------------
# Validator — at least one auth mode required
# ---------------------------------------------------------------------------


def test_validator_rejects_no_auth():
    """Config with NEITHER api_key NOR (api_user & api_pass) → raises."""
    with pytest.raises(ValueError, match="at least one auth mode"):
        _cfg()  # no api_key, no api_user/api_pass


def test_validator_rejects_lone_basic_half():
    """A lone api_user (or lone api_pass) is not a usable Basic credential."""
    with pytest.raises(ValueError, match="at least one auth mode"):
        _cfg(api_user="slm.user")  # api_pass missing
    with pytest.raises(ValueError, match="at least one auth mode"):
        _cfg(api_pass="slm.pass")  # api_user missing


def test_validator_accepts_bearer_or_basic():
    """Either valid mode parses cleanly."""
    _cfg(api_key="slm.api_key")
    _cfg(api_user="slm.user", api_pass="slm.pass")


# ---------------------------------------------------------------------------
# on_configure guard — fail loud when neither credential resolves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_configure_missing_basic_secret_raises():
    """Basic-only config but the vault is missing the user/pass secrets →
    HardLLMFailure (no silent anonymous access)."""
    h = OpenAIProviderHandler()
    cfg = _cfg(api_user="slm.user", api_pass="slm.pass")
    with pytest.raises(HardLLMFailure, match="vault missing basic-auth"):
        await h.on_configure(_ctx(cfg, {}))
