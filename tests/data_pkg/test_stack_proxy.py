# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the L-123 proxy-pool stack handlers (`legba.data.stack.proxy`).

Three handler-level scopes:

  * Unit tests on URL formatting + session construction + country validation
    + credential resolution (no network, no Postgres). All driven against
    `BrightDataProxyHandler` with an in-memory fake resolver.
  * Unit tests on the `LocalNoneProxyHandler` no-op contract.
  * Integration test against real Bright Data only when
    `LEGBA_BRIGHT_DATA_CUSTOMER_ID`, `LEGBA_BRIGHT_DATA_ZONE`,
    `LEGBA_BRIGHT_DATA_PASSWORD` are present; otherwise skipped (Bright
    Data bills ~$8/GB).

The unit tests are deliberately self-contained — they don't depend on the
substrate containers being up. The integration test only spins up if the
env vars are set.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from legba.data.stack.proxy import (
    BRIGHT_DATA_DEFAULT_HOST,
    BRIGHT_DATA_DEFAULT_PORT,
    BRIGHT_DATA_KIND,
    BrightDataProxyHandler,
    DEEP_HEALTHCHECK_ENV,
    DEFAULT_ALLOWED_COUNTRIES,
    InvalidCountryError,
    LOCAL_NONE_KIND,
    LocalNoneProxyHandler,
    ProxyPoolHandler,
    UsageLedgerUnavailable,
    UsageRecord,
)
from legba.data.stack.proxy.bright_data import (
    MissingCredentialError,
    _DEEP_HEALTHCHECK_URL,
)
from legba.data.stack.proxy.local_none import DIRECT_SENTINEL


# ---------------------------------------------------------------------------
# Fake credential resolver — mirrors the L-111 protocol surface.
# ---------------------------------------------------------------------------


class FakeResolver:
    """Minimal `CredentialResolverProtocol` substitute. Stores plaintext in
    memory keyed by secret_id."""

    def __init__(self, mapping: dict[str, bytes] | None = None):
        self._store: dict[str, bytes] = dict(mapping or {})
        self.calls: list[str] = []

    def add(self, secret_id: str, value: bytes | str) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self._store[secret_id] = value

    async def resolve(self, secret_id: str) -> bytes:
        self.calls.append(secret_id)
        if secret_id not in self._store:
            raise KeyError(secret_id)
        return self._store[secret_id]

    async def verify_exists(self, secret_id: str) -> bool:
        return secret_id in self._store


def _make_resolver(
    prefix: str = "proxy.bright_data.creds",
    customer_id: str = "cust12345",
    zone: str = "residential_pool",
    password: str = "pw!secret",
) -> FakeResolver:
    r = FakeResolver()
    r.add(f"{prefix}.customer_id", customer_id)
    r.add(f"{prefix}.zone", zone)
    r.add(f"{prefix}.password", password)
    return r


# ---------------------------------------------------------------------------
# Fake ProxyPoolConfig — duck-typed against the real schema. We could use
# the real one, but importing it pulls in the registry's schemas package
# and forces a heavier dependency surface on unit tests.
# ---------------------------------------------------------------------------


class _Factory:
    def __init__(self, raw: Any):
        self.raw = raw


class FakeProxyConfig:
    def __init__(
        self,
        credentials_prefix: str = "proxy.bright_data.creds",
        geo_targeting: list[str] | None = None,
    ):
        self.provider = _Factory("bright_data")
        self.endpoint = None
        self.credentials = _Factory(credentials_prefix)
        self.geo_targeting = _Factory(list(geo_targeting or []))
        self.rotation = _Factory("session")


# ---------------------------------------------------------------------------
# Construction / class-level contract.
# ---------------------------------------------------------------------------


def test_bright_data_classvars_present():
    """L-102 §1: every handler must declare `kind`, `family`, `schema_version`."""
    assert BrightDataProxyHandler.kind == BRIGHT_DATA_KIND == "proxy_pool"
    assert BrightDataProxyHandler.family == "stack"
    assert BrightDataProxyHandler.schema_version.startswith("legba/stack/proxy_pool/")
    assert BrightDataProxyHandler.handler_version


def test_local_none_classvars_present():
    assert LocalNoneProxyHandler.kind == LOCAL_NONE_KIND == "proxy_pool"
    assert LocalNoneProxyHandler.family == "stack"
    assert LocalNoneProxyHandler.schema_version.startswith("legba/stack/proxy_pool/")


def test_bright_data_requires_credentials_prefix():
    """No prefix on the config and no explicit kwarg → construction must fail."""

    class _CfgNoCreds:
        provider = _Factory("bright_data")
        endpoint = None
        credentials = None
        geo_targeting = _Factory([])
        rotation = _Factory("session")

    with pytest.raises(ValueError, match="credentials prefix"):
        BrightDataProxyHandler(
            component_id="proxy.bright_data.test",
            config=_CfgNoCreds(),
            resolver=_make_resolver(),
        )


def test_bright_data_explicit_prefix_overrides_config():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(credentials_prefix="ignored.prefix"),
        resolver=_make_resolver(),
        credentials_prefix="override.prefix",
    )
    assert h.credentials_prefix == "override.prefix"


def test_proxy_pool_handler_abstract_base_requires_overrides():
    """Constructing the base class directly fails on missing class-level kind."""

    class _Empty(ProxyPoolHandler):
        kind = ""
        schema_version = "x"

    with pytest.raises(TypeError, match="kind"):
        _Empty(component_id="x", config=FakeProxyConfig())

    class _NoVersion(ProxyPoolHandler):
        kind = "proxy_pool"
        schema_version = ""

    with pytest.raises(TypeError, match="schema_version"):
        _NoVersion(component_id="x", config=FakeProxyConfig())


# ---------------------------------------------------------------------------
# Country handling.
# ---------------------------------------------------------------------------


def test_country_normalization_lowercase_input():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(geo_targeting=["us", "gb"]),
        resolver=_make_resolver(),
    )
    assert "US" in h.allowed_countries
    assert "GB" in h.allowed_countries


def test_country_validation_rejects_invalid_shape():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
    )
    with pytest.raises(ValueError, match="ISO 3166-1 alpha-2"):
        h._validate_country("USA")
    with pytest.raises(ValueError, match="ISO 3166-1 alpha-2"):
        h._validate_country("1")


def test_country_validation_rejects_disallowed():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(geo_targeting=["US", "GB"]),
        resolver=_make_resolver(),
    )
    with pytest.raises(InvalidCountryError) as excinfo:
        h._validate_country("DE")
    assert excinfo.value.country == "DE"
    assert set(excinfo.value.allowed) == {"US", "GB"}


def test_country_validation_accepts_allowed():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(geo_targeting=["US", "DE"]),
        resolver=_make_resolver(),
    )
    assert h._validate_country("us") == "US"
    assert h._validate_country("DE") == "DE"
    assert h._validate_country(None) is None


def test_default_allowed_countries_used_when_config_empty():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(geo_targeting=[]),
        resolver=_make_resolver(),
    )
    # The module default covers all of these.
    assert "US" in h.allowed_countries
    assert "JP" in h.allowed_countries
    assert "BR" in h.allowed_countries
    assert tuple(sorted(h.allowed_countries)) == tuple(sorted(DEFAULT_ALLOWED_COUNTRIES))


# ---------------------------------------------------------------------------
# URL formatting — the core of L-123.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bright_data_url_basic_no_country():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(
            customer_id="cust12345", zone="residential_pool", password="pw!secret",
        ),
    )
    url = await h.get_proxy_url()
    assert url == (
        "http://user-customer-cust12345-zone-residential_pool"
        ":pw!secret@brd.superproxy.io:22225"
    )


@pytest.mark.asyncio
async def test_bright_data_url_with_country():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(geo_targeting=["US", "DE"]),
        resolver=_make_resolver(
            customer_id="cust1", zone="zone1", password="pw1",
        ),
    )
    url = await h.get_proxy_url(country="DE")
    assert url == (
        "http://user-customer-cust1-zone-zone1-country-de"
        ":pw1@brd.superproxy.io:22225"
    )


@pytest.mark.asyncio
async def test_bright_data_url_with_sticky_session():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(geo_targeting=["US"]),
        resolver=_make_resolver(
            customer_id="c", zone="z", password="p",
        ),
    )
    url = await h.get_proxy_url(country="US", sticky=True, session_id="sess_abc")
    assert url == (
        "http://user-customer-c-zone-z-country-us-session-sess_abc"
        ":p@brd.superproxy.io:22225"
    )


@pytest.mark.asyncio
async def test_bright_data_url_sticky_requires_session_id():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
    )
    with pytest.raises(ValueError, match="session_id"):
        await h.get_proxy_url(sticky=True, session_id=None)


@pytest.mark.asyncio
async def test_bright_data_url_rejects_bad_session_id():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
    )
    # Uppercase / spaces / overly long → reject.
    with pytest.raises(ValueError, match="session_id"):
        await h.get_proxy_url(sticky=True, session_id="HAS UPPER")
    with pytest.raises(ValueError, match="session_id"):
        await h.get_proxy_url(sticky=True, session_id="a" * 33)


@pytest.mark.asyncio
async def test_bright_data_url_rejects_disallowed_country():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(geo_targeting=["US"]),
        resolver=_make_resolver(),
    )
    with pytest.raises(InvalidCountryError):
        await h.get_proxy_url(country="FR")


@pytest.mark.asyncio
async def test_bright_data_url_no_caching_resolves_each_call():
    """Per L-102 §7 — credentials must NOT be cached. Verify by counting
    resolver calls across two `get_proxy_url` invocations."""
    resolver = _make_resolver()
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=resolver,
    )
    await h.get_proxy_url()
    await h.get_proxy_url()
    # 3 sub-keys * 2 calls = 6 resolves.
    assert len(resolver.calls) == 6


@pytest.mark.asyncio
async def test_bright_data_url_uses_custom_host_port():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
        host="alt.example.com",
        port=33333,
    )
    url = await h.get_proxy_url()
    assert "alt.example.com:33333" in url


# ---------------------------------------------------------------------------
# Credential resolution failure paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bright_data_missing_credential_raises():
    resolver = FakeResolver()
    # only customer_id present
    resolver.add("proxy.bright_data.creds.customer_id", "cust1")
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=resolver,
    )
    with pytest.raises(MissingCredentialError) as excinfo:
        await h.get_proxy_url()
    assert excinfo.value.key == "zone"


@pytest.mark.asyncio
async def test_bright_data_missing_resolver_raises():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=None,
    )
    with pytest.raises(MissingCredentialError):
        await h.get_proxy_url()


@pytest.mark.asyncio
async def test_bright_data_empty_credential_rejected():
    resolver = FakeResolver()
    resolver.add("proxy.bright_data.creds.customer_id", "cust1")
    resolver.add("proxy.bright_data.creds.zone", "")
    resolver.add("proxy.bright_data.creds.password", "pw")
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=resolver,
    )
    with pytest.raises(MissingCredentialError):
        await h.get_proxy_url()


# ---------------------------------------------------------------------------
# Session factories.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_aiohttp_session_wires_proxy():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(geo_targeting=["US"]),
        resolver=_make_resolver(
            customer_id="c", zone="z", password="p",
        ),
    )
    session = await h.get_aiohttp_session(country="US")
    try:
        url = getattr(session, "legba_proxy_url", None)
        assert url is not None
        assert "country-us" in url
        assert "brd.superproxy.io:22225" in url
        assert getattr(session, "legba_proxy_country") == "US"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_get_httpx_async_client_wires_proxy():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(geo_targeting=["GB"]),
        resolver=_make_resolver(
            customer_id="c", zone="z", password="p",
        ),
    )
    client = await h.get_httpx_async_client(country="GB", timeout=5.0)
    try:
        assert isinstance(client, httpx.AsyncClient)
        url = getattr(client, "legba_proxy_url", None)
        assert url is not None
        assert "country-gb" in url
        assert "brd.superproxy.io:22225" in url
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# report_usage.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_usage_refuses_when_no_store():
    """No pg_store wired → loud refusal, never a faked-persisted record
    (docs/SEAMS.md: proxy usage ledger persistence)."""
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
    )
    with pytest.raises(UsageLedgerUnavailable, match="no pg_store wired"):
        await h.report_usage(bytes_used=1024, country="US")


@pytest.mark.asyncio
async def test_report_usage_rejects_negative():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
    )
    with pytest.raises(ValueError, match="bytes_used"):
        await h.report_usage(bytes_used=-1, country="US")


def _ledger_store(inserts: list, *, table_exists: bool = True):
    """Fake pg_store: reports `to_regclass` per ``table_exists`` and
    captures INSERT args into ``inserts``."""

    class _Conn:
        async def fetchval(self, *_a, **_kw):
            return table_exists

        async def execute(self, _sql, *args):
            inserts.append(tuple(args))

    class _AcquireCM:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_):
            return False

    class _Store:
        def acquire(self):
            return _AcquireCM()

    return _Store()


@pytest.mark.asyncio
async def test_report_usage_normalizes_country():
    inserts: list[tuple[Any, ...]] = []
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
        pg_store=_ledger_store(inserts),
    )
    record = await h.report_usage(bytes_used=512, country="us")
    assert record.country == "US"
    assert len(inserts) == 1


@pytest.mark.asyncio
async def test_report_usage_writes_to_ledger_when_table_exists():
    """Drive `report_usage` against a fake pg_store. The store reports the
    table exists and captures the insert."""
    inserts: list[tuple[Any, ...]] = []
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
        pg_store=_ledger_store(inserts),
    )
    record = await h.report_usage(bytes_used=4096, country="DE")
    assert isinstance(record, UsageRecord)
    assert len(inserts) == 1
    component_id, country, bytes_used, recorded_at = inserts[0]
    assert component_id == "proxy.bright_data.test"
    assert country == "DE"
    assert bytes_used == 4096
    assert record.recorded_at == recorded_at


@pytest.mark.asyncio
async def test_report_usage_refuses_when_table_missing():
    """When `to_regclass(...)` returns false: loud refusal, no insert,
    no faked-persisted record."""
    inserts: list[tuple[Any, ...]] = []
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
        pg_store=_ledger_store(inserts, table_exists=False),
    )
    with pytest.raises(UsageLedgerUnavailable, match="table absent"):
        await h.report_usage(bytes_used=1024, country="US")
    assert inserts == []


@pytest.mark.asyncio
async def test_report_usage_wraps_insert_failure():
    """An INSERT exception surfaces as UsageLedgerUnavailable — never
    swallowed into a fake success."""

    class _Conn:
        async def fetchval(self, *_a, **_kw):
            return True

        async def execute(self, *_a, **_kw):
            raise RuntimeError("connection reset")

    class _AcquireCM:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_):
            return False

    class _Store:
        def acquire(self):
            return _AcquireCM()

    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
        pg_store=_Store(),
    )
    with pytest.raises(UsageLedgerUnavailable, match="connection reset"):
        await h.report_usage(bytes_used=1024, country="US")


# ---------------------------------------------------------------------------
# Health.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_health_healthy_when_all_present():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
    )
    hc = await h.health_check()
    assert hc.state == "healthy"


@pytest.mark.asyncio
async def test_credential_health_unhealthy_when_missing():
    resolver = FakeResolver()
    resolver.add("proxy.bright_data.creds.customer_id", "cust")
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=resolver,
    )
    hc = await h.health_check()
    assert hc.state == "unhealthy"
    assert hc.last_error is not None


@pytest.mark.asyncio
async def test_health_degraded_without_resolver():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=None,
    )
    hc = await h.health_check()
    # No resolver wired → degraded (not unhealthy — the runtime might be
    # in the middle of wiring it).
    assert hc.state == "degraded"


@pytest.mark.asyncio
async def test_deep_health_check_gated_by_env(monkeypatch):
    """Default (env unset): returns unknown, makes no outbound call."""
    monkeypatch.delenv(DEEP_HEALTHCHECK_ENV, raising=False)
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
    )
    hc = await h.deep_health_check()
    assert hc.state == "unknown"


@pytest.mark.asyncio
async def test_deep_health_check_runs_when_enabled(monkeypatch):
    """With the env set, the deep check dispatches; we patch `_do_deep_health_check`
    to confirm dispatch without making a real outbound call."""
    monkeypatch.setenv(DEEP_HEALTHCHECK_ENV, "1")
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
    )

    called = {}

    async def _stub_deep():
        called["yes"] = True
        from legba.data.stack.proxy.bright_data import HandlerHealth as _HH
        return _HH(state="healthy", detail={"stubbed": True})

    h._do_deep_health_check = _stub_deep  # type: ignore[assignment]
    hc = await h.deep_health_check()
    assert called.get("yes") is True
    assert hc.state == "healthy"


# ---------------------------------------------------------------------------
# Lifecycle hooks — defaults are no-ops; verify they exist and don't raise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_hooks_no_op():
    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.test",
        config=FakeProxyConfig(),
        resolver=_make_resolver(),
    )
    # All hooks are awaitable and return None.
    assert await h.on_configure() is None
    assert await h.on_activate() is None
    assert await h.on_pause() is None
    assert await h.on_resume() is None
    assert await h.on_retire() is None


# ---------------------------------------------------------------------------
# LocalNoneProxyHandler.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_none_get_proxy_url_returns_sentinel():
    h = LocalNoneProxyHandler()
    assert await h.get_proxy_url() == DIRECT_SENTINEL


@pytest.mark.asyncio
async def test_local_none_get_proxy_url_validates_country():
    h = LocalNoneProxyHandler(allowed_countries=["US", "GB"])
    assert await h.get_proxy_url(country="US") == DIRECT_SENTINEL
    with pytest.raises(InvalidCountryError):
        await h.get_proxy_url(country="DE")


@pytest.mark.asyncio
async def test_local_none_health_always_healthy():
    h = LocalNoneProxyHandler()
    hc = await h.health_check()
    assert hc.state == "healthy"


@pytest.mark.asyncio
async def test_local_none_deep_health_unknown():
    h = LocalNoneProxyHandler()
    hc = await h._do_deep_health_check()
    # Deep check doesn't apply for the no-op handler.
    assert hc.state == "unknown"


@pytest.mark.asyncio
async def test_local_none_aiohttp_session_no_proxy():
    h = LocalNoneProxyHandler()
    session = await h.get_aiohttp_session()
    try:
        assert getattr(session, "legba_proxy_url") == DIRECT_SENTINEL
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_local_none_httpx_client_no_proxy():
    h = LocalNoneProxyHandler()
    client = await h.get_httpx_async_client()
    try:
        assert isinstance(client, httpx.AsyncClient)
        assert getattr(client, "legba_proxy_url") == DIRECT_SENTINEL
        # No proxy mounted on the client.
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_local_none_report_usage_inherits_loud_fail():
    """LocalNone inherits the base loud-fail contract: with no pg_store
    wired, report_usage refuses instead of returning a fake record."""
    h = LocalNoneProxyHandler()
    with pytest.raises(UsageLedgerUnavailable, match="no pg_store wired"):
        await h.report_usage(bytes_used=512, country="US")


@pytest.mark.asyncio
async def test_local_none_report_usage_persists_with_store():
    inserts: list[tuple[Any, ...]] = []
    h = LocalNoneProxyHandler(pg_store=_ledger_store(inserts))
    record = await h.report_usage(bytes_used=512, country="US")
    assert record.bytes_used == 512
    assert record.country == "US"
    assert record.component_id == "proxy.local.none"
    assert len(inserts) == 1


@pytest.mark.asyncio
async def test_local_none_no_resolver_needed():
    """The dev path must work without a vault at all."""
    h = LocalNoneProxyHandler(resolver=None)
    assert await h.get_proxy_url() == DIRECT_SENTINEL
    hc = await h.health_check()
    assert hc.state == "healthy"


# ---------------------------------------------------------------------------
# Real Bright Data integration test — gated, costs money.
# ---------------------------------------------------------------------------


_BRIGHT_DATA_ENV_KEYS = (
    "LEGBA_BRIGHT_DATA_CUSTOMER_ID",
    "LEGBA_BRIGHT_DATA_ZONE",
    "LEGBA_BRIGHT_DATA_PASSWORD",
)


def _bright_data_creds_present() -> bool:
    return all(os.getenv(k) for k in _BRIGHT_DATA_ENV_KEYS)


@pytest.mark.integration
@pytest.mark.skipif(
    not _bright_data_creds_present(),
    reason=(
        "real Bright Data integration test requires "
        f"{', '.join(_BRIGHT_DATA_ENV_KEYS)} env vars. Costs ~$8/GB; opt-in only."
    ),
)
@pytest.mark.asyncio
async def test_bright_data_real_outbound():
    """Live test against Bright Data's residential network. Only runs when
    the operator has explicitly set the env vars (signaling cost
    acknowledgment).

    Round-trips a single `ipinfo.io/json` request through a US-targeted
    exit and asserts the response carries a country code we recognize.
    Expected billed bytes: < 2 KiB.
    """

    class _EnvResolver:
        async def resolve(self, sid: str) -> bytes:
            suffix = sid.rsplit(".", 1)[-1]
            env_key_map = {
                "customer_id": "LEGBA_BRIGHT_DATA_CUSTOMER_ID",
                "zone": "LEGBA_BRIGHT_DATA_ZONE",
                "password": "LEGBA_BRIGHT_DATA_PASSWORD",
            }
            env_key = env_key_map.get(suffix)
            if env_key is None:
                raise KeyError(sid)
            value = os.environ.get(env_key)
            if not value:
                raise KeyError(sid)
            return value.encode("utf-8")

        async def verify_exists(self, sid: str) -> bool:
            try:
                await self.resolve(sid)
                return True
            except KeyError:
                return False

    h = BrightDataProxyHandler(
        component_id="proxy.bright_data.integration",
        config=FakeProxyConfig(geo_targeting=["US"]),
        resolver=_EnvResolver(),
        credentials_prefix="proxy.bright_data.creds",
    )
    client = await h.get_httpx_async_client(country="US", timeout=20.0)
    try:
        resp = await client.get(_DEEP_HEALTHCHECK_URL)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The exit country should be a 2-letter ISO code. Bright Data may
        # route to a nearby country if no US peers are free, so don't pin
        # exactly to "US" — just confirm the shape.
        cc = body.get("country")
        assert isinstance(cc, str) and len(cc) == 2
    finally:
        await client.aclose()
