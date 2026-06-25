# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-2 — keyed source descriptors FAIL LOUD when the vault key is absent.

The activation-gating contract for the committed keyed descriptors
(descriptors/source_acled_conflict.yaml et al): the descriptor carries a
VAULT REF, the runtime threads ``StandardDeps.secrets_resolve`` (in
production: ``CredentialVault.resolve`` — see
scripts/bringup_source_first_host.py) into the handler, and resolving an
ABSENT key raises ``MissingSecretError`` (``KeyError`` subclass,
src/legba/data/registry/credentials.py: ``raise MissingSecretError(secret_id)``
when the ``stack_credentials`` head-row lookup returns None). No HTTP runs,
no signal is fabricated.

This module proves that with NO mock vault: a real ``CredentialVault`` over
the per-session fresh migrated Postgres (``migrated_pg`` — a brand-new
``legba_test_<uuid>`` DB, so the descriptors' secret ids are guaranteed
absent), descriptor configs loaded from the committed YAMLs, handlers
constructed through the production factory path.

Covered kinds:

  * acled        — pull() resolves the OAuth2 username/password vault refs
                   first (to mint the Bearer token); MissingSecretError.
  * mediacloud   — pull() resolves the key before any HTTP; built via
                   ``build_source_handler`` so the factory-threaded
                   resolver slot is exercised end-to-end.
  * opensanctions (api mode) — pull() resolves the vault ref BEFORE any
                   HTTP (never calls the keyed API unauthenticated).

Positive control: storing the secret first makes the same acled resolution
succeed — proving the failure above is the vault miss, not broken plumbing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.credentials import CredentialVault, MissingSecretError
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources._contract import InMemoryStateStore, SourceContext
from legba.data.sources.acled import ACLEDConfig, ACLEDSourceHandler
from legba.runtime.source_factory import (
    _unwrap_factory_dict,
    build_source_handler,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"

# Any 32 bytes work — a vault MISS raises before decryption is attempted.
_TEST_MASTER_KEY_HEX = "ab" * 32


def _load_descriptor(name: str) -> SourceDescriptor:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return SourceDescriptor.model_validate(body, strict=False)


def _ctx(config: Any, secrets_resolve: Any) -> SourceContext:
    return SourceContext(
        target_id="test.s2.gating",
        target_version="v0",
        source_id="test.s2.gating",
        config=config,
        state_store=InMemoryStateStore(),
        secrets_resolve=secrets_resolve,
        logger=logging.getLogger("test.s2.gating"),
    )


@pytest_asyncio.fixture
async def vault_store(migrated_pg: PostgresConfig) -> PostgresStore:
    """Store over the FRESH per-session migrated DB — its stack_credentials
    table (migration 0011) starts empty, so the descriptors' vault ids are
    guaranteed absent without truncating anything shared."""
    s = PostgresStore(migrated_pg)
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def vault(vault_store: PostgresStore) -> CredentialVault:
    return CredentialVault(
        vault_store, master_key=bytes.fromhex(_TEST_MASTER_KEY_HEX),
    )


# ---------------------------------------------------------------------------
# acled — the required keyed-kind fail-loud assertion
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acled_pull_fails_loud_when_vault_key_absent(
    vault: CredentialVault,
) -> None:
    desc = _load_descriptor("source_acled_conflict.yaml")
    cfg = ACLEDConfig(**_unwrap_factory_dict(desc.config))
    # OAuth2 password grant: the descriptor carries username + password vault
    # refs (the legacy api_key_secret is gone).
    assert cfg.username_secret == "source.acled.username"
    assert cfg.password_secret == "source.acled.password"

    # Precondition (fresh DB): the vault genuinely has neither credential.
    assert await vault.verify_exists(cfg.username_secret) is False
    assert await vault.verify_exists(cfg.password_secret) is False

    handler = ACLEDSourceHandler()
    ctx = _ctx(cfg, vault.resolve)

    # pull() mints the Bearer token first, which resolves the username vault
    # ref before any HTTP — so an absent credential fails loud, no signal.
    with pytest.raises(MissingSecretError) as exc:
        async for _ in handler.pull(ctx, since=None):
            pytest.fail("pull yielded a signal with the vault credential absent")
    # The loud failure names the missing vault id for the operator. The token
    # mint resolves the username first, so that is the id surfaced.
    assert "source.acled.username" in str(exc.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acled_health_check_reports_unhealthy_not_fake_healthy(
    vault: CredentialVault,
) -> None:
    """The health probe path must surface the credential failure too — under
    OAuth2 the token mint resolves the vault refs first, so an absent
    credential yields an `oauth_token_failed` / unhealthy probe that names the
    missing secret, never a fabricated healthy."""
    desc = _load_descriptor("source_acled_conflict.yaml")
    cfg = ACLEDConfig(**_unwrap_factory_dict(desc.config))
    handler = ACLEDSourceHandler()
    health = await handler.health_check(_ctx(cfg, vault.resolve))
    assert health.state == "unhealthy"
    # The OAuth2 token mint failed because the vault has no such secret; the
    # probe surfaces that loudly (and names the missing vault id).
    assert "oauth_token_failed" in (health.last_error or "")
    assert "source.acled.username" in (health.last_error or "")


# ---------------------------------------------------------------------------
# mediacloud — through the production factory (resolver threading included)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mediacloud_pull_fails_loud_when_vault_key_absent(
    vault: CredentialVault,
) -> None:
    desc = _load_descriptor("source_mediacloud.yaml")
    assert await vault.verify_exists("source.mediacloud.api_key") is False

    # Production construction path: unwrap + config_schema parse + the
    # resolver threaded into the handler's `secret_resolver` slot.
    handler = build_source_handler(
        desc.identity.kind, desc.config, secrets_resolve=vault.resolve,
    )
    ctx = _ctx(handler._config, vault.resolve)

    with pytest.raises(MissingSecretError) as exc:
        async for _ in handler.pull(ctx, since=None):
            pytest.fail("pull yielded a signal with the vault key absent")
    assert "source.mediacloud.api_key" in str(exc.value)


# ---------------------------------------------------------------------------
# opensanctions api mode — resolves BEFORE any HTTP, fails loud
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_opensanctions_api_pull_fails_loud_when_vault_key_absent(
    vault: CredentialVault,
) -> None:
    desc = _load_descriptor("source_opensanctions_api.yaml")
    assert await vault.verify_exists("source.opensanctions.api_key") is False

    handler = build_source_handler(
        desc.identity.kind, desc.config, secrets_resolve=vault.resolve,
    )
    ctx = _ctx(handler._config, vault.resolve)

    with pytest.raises(MissingSecretError) as exc:
        async for _ in handler.pull(ctx, since=None):
            pytest.fail("pull yielded a signal with the vault key absent")
    assert "source.opensanctions.api_key" in str(exc.value)


# ---------------------------------------------------------------------------
# Positive control — same plumbing, key present, resolution succeeds.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acled_resolution_succeeds_once_key_stored(
    vault: CredentialVault,
) -> None:
    """Stores the descriptor's vault ids, re-runs the exact resolution the
    failing test exercised (for BOTH OAuth2 credentials), and gets the
    plaintext back — proving the fail-loud result above is the vault miss, not
    broken wiring."""
    desc = _load_descriptor("source_acled_conflict.yaml")
    cfg = ACLEDConfig(**_unwrap_factory_dict(desc.config))
    try:
        await vault.store_secret(
            cfg.username_secret, "acled-user@example.org", actor="test:s2",
        )
        await vault.store_secret(
            cfg.password_secret, "not-a-real-password", actor="test:s2",
        )
        assert await vault.resolve(cfg.username_secret) == b"acled-user@example.org"
        assert await vault.resolve(cfg.password_secret) == b"not-a-real-password"
    finally:
        # Leave the (per-session, throwaway) DB clean for sibling tests
        # that assert absence.
        await vault.delete_secret(cfg.username_secret)
        await vault.delete_secret(cfg.password_secret)
