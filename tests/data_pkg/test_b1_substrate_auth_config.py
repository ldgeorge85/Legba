# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B-1 — stop publishing the substrate (review §5, CRITICAL).

Three guarantees, all config-level (the coordinated cutover applies them):

  1. docker-compose.yml publishes NO host port on 0.0.0.0 except the Caddy
     edge (80/443) — every other ``ports:`` mapping carries an explicit
     ``127.0.0.1:`` host bind, so published substrate ports can no longer
     bypass the Caddy TLS/basic-auth perimeter.
  2. NATS token auth (LEGBA_NATS_TOKEN) threads through NatsConfig — both
     ``from_env()`` AND direct dataclass construction (the stack-handler /
     test pattern) — and NatsStore.connect() passes it to nats.connect().
     Empty/unset = unauthenticated (pre-cutover behaviour).
  3. Redis requirepass (LEGBA_REDIS_PASSWORD) threads through RedisConfig
     the same way, and the dapr pubsub component resolves the NATS token
     via the existing local.env secret-store pattern (no plaintext secret
     committed).

These tests parse YAML / construct dataclasses only — no docker daemon and
no live substrate required (the NatsStore test monkeypatches nats.connect;
mocks live in tests/ only, per the no-stubs rule).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# Resolve the repo root RELATIVE to this file so the test exercises the
# checked-out tree it lives in (worktree-safe — unlike a hardcoded path).
REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
SLM_COMPOSE_FILE = REPO_ROOT / "legba-models" / "docker-compose.slm.yml"
PUBSUB_COMPONENT = REPO_ROOT / "dapr" / "components" / "pubsub.yaml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


# ---------------------------------------------------------------------------
# 1. Port-publishing perimeter
# ---------------------------------------------------------------------------


def _load_services(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and "services" in doc, f"{path} is not a compose file"
    return doc["services"]


def test_only_caddy_publishes_beyond_loopback() -> None:
    """Every ``ports:`` mapping except Caddy's must bind 127.0.0.1.

    Regression guard for the review-§5 finding: 0.0.0.0 LISTEN for
    postgres/redis/NATS/qdrant/registry/runtime/daprd
    bypassed the Caddy perimeter on a host terminating a public TLS domain
    (firewalld's docker policy ACCEPTs forwarded traffic)."""
    services = _load_services(COMPOSE_FILE)
    offenders: list[str] = []
    for name, body in services.items():
        for entry in body.get("ports", []) or []:
            entry = str(entry)
            if name == "legba-caddy":
                # The edge is the ONLY all-interfaces surface: 80 + 443 only.
                host_part = entry.split(":")[0]
                assert host_part in {"80", "443"}, (
                    f"legba-caddy may only publish 80/443; got {entry!r}"
                )
                continue
            if not entry.startswith("127.0.0.1:"):
                offenders.append(f"{name}: {entry}")
    assert not offenders, (
        "non-loopback host port binds found (must be '127.0.0.1:<host>:<container>'):\n  "
        + "\n  ".join(offenders)
    )


def test_slm_overlay_binds_loopback() -> None:
    """The legba-models SLM overlay must not publish 8701 on 0.0.0.0."""
    services = _load_services(SLM_COMPOSE_FILE)
    for name, body in services.items():
        for entry in body.get("ports", []) or []:
            assert str(entry).startswith("127.0.0.1:"), (
                f"{name}: {entry!r} must bind 127.0.0.1"
            )


def test_nats_and_redis_commands_carry_auth_wiring() -> None:
    """The substrate nats/redis services must thread the B-1 env keys."""
    services = _load_services(COMPOSE_FILE)

    nats_cmd = " ".join(
        services["nats"]["command"]
        if isinstance(services["nats"]["command"], list)
        else [services["nats"]["command"]]
    )
    assert "LEGBA_NATS_TOKEN" in nats_cmd and "--auth" in nats_cmd, (
        "nats service command must conditionally enable --auth from LEGBA_NATS_TOKEN"
    )
    assert "LEGBA_NATS_TOKEN" in services["nats"].get("environment", {}), (
        "nats service must receive LEGBA_NATS_TOKEN via environment"
    )

    redis_cmd = services["redis"]["command"]
    assert "--requirepass" in redis_cmd, (
        "redis service command must wire --requirepass from LEGBA_REDIS_PASSWORD"
    )
    # The healthcheck must keep passing once requirepass is active.
    health = " ".join(services["redis"]["healthcheck"]["test"])
    assert "LEGBA_REDIS_PASSWORD" in health, (
        "redis healthcheck must authenticate when LEGBA_REDIS_PASSWORD is set"
    )

    # daprd must always carry the env key so the local.env secret store can
    # resolve pubsub.yaml's secretKeyRef even pre-cutover (empty value).
    assert "LEGBA_NATS_TOKEN" in services["dapr-sidecar"].get("environment", {}), (
        "dapr-sidecar must guarantee LEGBA_NATS_TOKEN exists in its environment"
    )


# ---------------------------------------------------------------------------
# 2. NATS token threading (config + connect)
# ---------------------------------------------------------------------------


def test_nats_config_reads_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from legba.data.config import NatsConfig

    monkeypatch.setenv("LEGBA_NATS_TOKEN", "tok-b1-test")
    assert NatsConfig.from_env().token == "tok-b1-test"
    # Direct construction (stack handlers / tests build NatsConfig(url=...))
    # must pick up the env token too — that is the whole point of the
    # default_factory.
    assert NatsConfig(url="nats://example:4222").token == "tok-b1-test"

    monkeypatch.delenv("LEGBA_NATS_TOKEN")
    assert NatsConfig.from_env().token is None
    assert NatsConfig(url="nats://example:4222").token is None

    # Empty string means "no auth", not an empty token.
    monkeypatch.setenv("LEGBA_NATS_TOKEN", "")
    assert NatsConfig.from_env().token is None


@pytest.mark.asyncio
async def test_nats_store_passes_token_to_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import legba.data.nats as nats_mod
    from legba.data.config import NatsConfig
    from legba.data.nats import NatsStore

    captured: dict = {}

    class _FakeNC:
        is_connected = True

        def jetstream(self):
            return object()

    async def _fake_connect(**kwargs):
        captured.update(kwargs)
        return _FakeNC()

    monkeypatch.setattr(nats_mod.nats, "connect", _fake_connect)

    store = NatsStore(NatsConfig(url="nats://example:4222", token="tok-b1-test"))
    await store.connect()
    assert captured["token"] == "tok-b1-test"
    assert captured["servers"] == ["nats://example:4222"]

    # token=None → the kwarg is omitted entirely (unauthenticated connect).
    captured.clear()
    store2 = NatsStore(NatsConfig(url="nats://example:4222", token=None))
    await store2.connect()
    assert "token" not in captured


# ---------------------------------------------------------------------------
# 3. Redis password threading + secret-store discipline
# ---------------------------------------------------------------------------


def test_redis_config_reads_password_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from legba.data.config import RedisConfig

    for key in ("LEGBA_DATA_REDIS_PASSWORD", "LEGBA_REDIS_PASSWORD", "REDIS_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    assert RedisConfig.from_env().password is None
    assert RedisConfig().password is None

    monkeypatch.setenv("LEGBA_REDIS_PASSWORD", "pw-b1-test")
    assert RedisConfig.from_env().password == "pw-b1-test"
    assert RedisConfig().password == "pw-b1-test"
    # url embeds the auth section once a password is present.
    assert RedisConfig().url.startswith("redis://:pw-b1-test@")

    # The most-specific key still wins.
    monkeypatch.setenv("LEGBA_DATA_REDIS_PASSWORD", "pw-specific")
    assert RedisConfig.from_env().password == "pw-specific"


def test_pubsub_component_resolves_token_via_secret_store() -> None:
    """dapr pubsub.yaml must use the repo's secretKeyRef/local.env pattern
    (statestore.yaml precedent) — never a committed plaintext token."""
    doc = yaml.safe_load(PUBSUB_COMPONENT.read_text(encoding="utf-8"))
    meta = {m["name"]: m for m in doc["spec"]["metadata"]}
    token = meta["token"]
    assert "value" not in token, "pubsub token must not be an inline value"
    assert token["secretKeyRef"] == {
        "name": "LEGBA_NATS_TOKEN",
        "key": "LEGBA_NATS_TOKEN",
    }
    assert doc.get("auth", {}).get("secretStore") == "legba-env-secrets", (
        "pubsub.yaml must declare auth.secretStore=legba-env-secrets"
    )


def test_env_example_carries_b1_keys() -> None:
    body = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "\nLEGBA_NATS_TOKEN=\n" in body, ".env.example must carry LEGBA_NATS_TOKEN="
    assert "\nLEGBA_REDIS_PASSWORD=\n" in body, (
        ".env.example must carry LEGBA_REDIS_PASSWORD="
    )
