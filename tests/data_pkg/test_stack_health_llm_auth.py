# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W-B3 — the ``llm_provider`` stack healthcheck must probe BOTH auth modes.

THE DEFECT. ``LLMProviderConfig`` makes auth a SWITCH: a bearer ``api_key``
OR the HTTP Basic ``api_user``/``api_pass`` pair, with the model validator
requiring at least one. The registry's probe tried only ``api_key`` — so
``llm.verify.slm_8b`` (the self-hosted Llama-3.1-8B behind Caddy Basic Auth,
which carries NO ``api_key`` field at all) reported UNHEALTHY "api_key not in
vault" while serving every request it was given. The probe was wrong about
the component, not the component about itself, and the message pointed the
operator at a vault entry that is not supposed to exist.
"""
from __future__ import annotations

import pytest

from legba.data.registry import health as health_mod
from legba.data.registry.credentials import MissingSecretError
from legba.data.registry.health import (
    HealthState,
    LLMProviderChecker,
    ResolvedConfig,
)
from legba.data.schemas import LLMProviderConfig, Property


class _FakeResolver:
    def __init__(self, secrets: dict[str, bytes]) -> None:
        self._secrets = secrets
        self.asked: list[str] = []

    async def verify_exists(self, secret_id: str) -> bool:
        return secret_id in self._secrets

    async def resolve(self, secret_id: str) -> bytes:
        self.asked.append(secret_id)
        if secret_id not in self._secrets:
            raise MissingSecretError(secret_id)
        return self._secrets[secret_id]


def _cfg(*, bearer: str | None = None, user: str | None = None,
         pass_: str | None = None) -> LLMProviderConfig:
    return LLMProviderConfig(
        api_endpoint=Property.Text.of("https://slm.example.internal/v1"),
        api_key=Property.Secret.of(bearer) if bearer else None,
        api_user=Property.Secret.of(user) if user else None,
        api_pass=Property.Secret.of(pass_) if pass_ else None,
        model_name=Property.Text.of("meta-llama/Llama-3.1-8B-Instruct"),
        max_tokens=Property.Number.of(1024, minimum=1, maximum=200000),
    )


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch):
    """The auth branch is what is under test; pin reachability so a probe
    never depends on the runner's network."""
    monkeypatch.setattr(health_mod, "_tcp_reachable", lambda host, port, **kw: True)


async def _check(cfg: LLMProviderConfig, secrets: dict[str, bytes]):
    resolver = _FakeResolver(secrets)
    return await LLMProviderChecker().check(
        "llm.verify.slm_8b", ResolvedConfig(config=cfg, resolver=resolver)
    )


# ---------------------------------------------------------------------------
# THE FIX — a Basic-only component is healthy
# ---------------------------------------------------------------------------


async def test_basic_auth_only_component_is_healthy():
    """The exact llm.verify.slm_8b shape: no api_key field, an api_user +
    api_pass pair in the vault. This reported UNHEALTHY before the fix."""
    cfg = _cfg(user="llm.verify.slm_8b.api_user", pass_="llm.verify.slm_8b.api_pass")
    got = await _check(
        cfg,
        {
            "llm.verify.slm_8b.api_user": b"legba",
            "llm.verify.slm_8b.api_pass": b"s3cret",
        },
    )
    assert got.state is HealthState.HEALTHY
    assert "auth=basic" in got.detail
    assert got.extra["auth"] == "basic"


async def test_bearer_only_component_still_passes():
    cfg = _cfg(bearer="llm.core.api_key")
    got = await _check(cfg, {"llm.core.api_key": b"sk-x"})
    assert got.state is HealthState.HEALTHY
    assert got.extra["auth"] == "bearer"


async def test_basic_wins_when_both_modes_resolve():
    """Mirrors ``LLMProviderHandler._auth_headers`` — the probe must report
    the mode the real calls will actually use, or it is describing a
    different component."""
    cfg = _cfg(bearer="k", user="u", pass_="p")
    got = await _check(cfg, {"k": b"sk", "u": b"legba", "p": b"pw"})
    assert got.state is HealthState.HEALTHY
    assert got.extra["auth"] == "basic"


# ---------------------------------------------------------------------------
# The failure message names the field that is ACTUALLY missing
# ---------------------------------------------------------------------------


async def test_a_missing_bearer_key_is_still_named_api_key():
    cfg = _cfg(bearer="llm.core.api_key")
    got = await _check(cfg, {})
    assert got.state is HealthState.UNHEALTHY
    assert got.detail == "api_key not in vault"


@pytest.mark.parametrize(
    "present,expected",
    [
        ({}, "api_user, api_pass not in vault"),
        ({"u": b"legba"}, "api_pass not in vault"),
        ({"p": b"pw"}, "api_user not in vault"),
    ],
)
async def test_a_basic_component_names_the_basic_field_it_is_missing(
    present, expected
):
    """The old message said "api_key not in vault" for every one of these —
    a field this component does not have, sending the operator to the wrong
    vault entry."""
    cfg = _cfg(user="u", pass_="p")
    got = await _check(cfg, present)
    assert got.state is HealthState.UNHEALTHY
    assert got.detail == expected
    assert "api_key" not in got.detail


async def test_a_half_declared_basic_pair_says_so_rather_than_blaming_api_key():
    """The schema validator forbids this shape, so reaching it means a row
    that predates the validator. Name the real problem."""
    cfg = LLMProviderConfig.model_construct(
        api_endpoint=Property.Text.of("https://slm.example.internal/v1"),
        api_key=None,
        api_user=Property.Secret.of("u"),
        api_pass=None,
        model_name=Property.Text.of("m"),
        max_tokens=Property.Number.of(16, minimum=1, maximum=200000),
    )
    got = await _check(cfg, {})
    assert got.state is HealthState.UNHEALTHY
    assert "api_user and api_pass" in got.detail


async def test_a_component_with_no_auth_field_at_all_says_exactly_that():
    cfg = LLMProviderConfig.model_construct(
        api_endpoint=Property.Text.of("https://slm.example.internal/v1"),
        api_key=None,
        api_user=None,
        api_pass=None,
        model_name=Property.Text.of("m"),
        max_tokens=Property.Number.of(16, minimum=1, maximum=200000),
    )
    got = await _check(cfg, {})
    assert got.state is HealthState.UNHEALTHY
    assert got.detail.startswith("no auth field configured")


# ---------------------------------------------------------------------------
# Everything else the probe promised is unchanged
# ---------------------------------------------------------------------------


async def test_an_unparseable_endpoint_still_fails_before_any_vault_lookup():
    cfg = LLMProviderConfig(
        api_endpoint=Property.Text.of("::::"),
        api_user=Property.Secret.of("u"),
        api_pass=Property.Secret.of("p"),
        model_name=Property.Text.of("m"),
        max_tokens=Property.Number.of(16, minimum=1, maximum=200000),
    )
    resolver = _FakeResolver({})
    got = await LLMProviderChecker().check(
        "llm.bad", ResolvedConfig(config=cfg, resolver=resolver)
    )
    assert got.state is HealthState.UNHEALTHY
    assert "unparseable endpoint" in got.detail
    assert resolver.asked == []


async def test_an_unreachable_endpoint_is_unhealthy_even_with_good_creds(
    monkeypatch,
):
    monkeypatch.setattr(health_mod, "_tcp_reachable", lambda host, port, **kw: False)
    cfg = _cfg(user="u", pass_="p")
    got = await _check(cfg, {"u": b"legba", "p": b"pw"})
    assert got.state is HealthState.UNHEALTHY
    assert "reachable=False" in got.detail


async def test_a_resolver_that_explodes_is_reported_as_such():
    class _Boom:
        async def verify_exists(self, secret_id: str) -> bool:
            return False

        async def resolve(self, secret_id: str) -> bytes:
            raise RuntimeError("vault sealed")

    cfg = _cfg(user="u", pass_="p")
    got = await LLMProviderChecker().check(
        "llm.verify.slm_8b", ResolvedConfig(config=cfg, resolver=_Boom())
    )
    assert got.state is HealthState.UNHEALTHY
    assert "credential resolve failed" in got.detail
