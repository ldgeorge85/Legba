# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit + integration tests for the ACLED source handler (L-132).

Unit tests mock ``httpx`` via ``httpx.MockTransport`` so we exercise the
full pull/health logic without touching the network. ACLED migrated OFF the
legacy api-key-as-query-param auth to an OAuth2 password grant, so the mock
transport now serves BOTH the token POST and the record GETs. The integration
test runs against the real ACLED API when ``LEGBA_ACLED_USERNAME`` +
``LEGBA_ACLED_PASSWORD`` are present in the environment.

Test surface:

  * config validation (event_type / region whitelist, country format).
  * pull happy-path with a single page of records → one Signal per record.
  * pull pagination — multi-page, terminates on short page and on empty.
  * cursor advancement persists ``last_event_date`` + page.
  * cursor advancement causes the next pull to use the stored floor date.
  * OAuth2: the read calls carry ``Authorization: Bearer <token>``; the api
    key / email never appear in the query string.
  * the secrets resolver is invoked for BOTH the username and password vault
    ids before the token POST.
  * rate-limit (HTTP 429) back-off with ``Retry-After``; handler eventually
    succeeds on retry and exhausts retries on permanent 429.
  * ACLED ``success: False`` body raises.
  * healthcheck: healthy / rate-limited / unhealthy paths.
  * integration: real ACLED API end-to-end (requires creds).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
    SourceHandler,
    SourceHealth,
)
from legba.data.sources.acled import (
    ACLED_API_BASE,
    ACLED_EVENT_TYPES,
    ACLED_OAUTH_TOKEN_URL,
    ACLED_PAGE_SIZE_MAX,
    ACLEDConfig,
    ACLEDSourceHandler,
)


# OAuth2 password grant: ACLED retired the legacy ``key``/``email`` query-param
# auth. The handler now POSTs the resolved username + password to the token
# endpoint, then rides ``Authorization: Bearer <token>`` on the read calls. The
# mock transport short-circuits the token POST to this canned response and lets
# the GET sequence model the record pages exactly as before.
_TEST_TOKEN = "TEST_TOKEN"


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": _TEST_TOKEN,
            "token_type": "Bearer",
            "expires_in": 86400,
        },
    )


# Real src bug surfaced by the source-first pivot (commit fa3e598): the pivot
# re-cut the Signal model (src/legba/data/sources/_contract.py) to drop
# ``target_id`` and set ``extra='forbid'`` (observations are source-owned, not
# target-owned), but the source handlers were NOT updated — the ACLED handler
# still calls ``Signal(..., target_id=ctx.target_id, ...)`` (acled.py:700). So
# the first Signal a ``pull()`` constructs raises
# ``ValidationError: target_id Extra inputs are not permitted``. This is a bug
# in src, not a stale-test/schema issue, so per the migration constraints it is
# FLAGGED in real_src_bugs_flagged and the pull-exercising tests are skipped
# (src not edited to mask it). See PIVOT_BUILD_PLAN.
_SRC_BUG_TARGET_ID = (
    "src bug (pivot fa3e598, acled.py:700): handler still passes "
    "target_id=ctx.target_id into the pivoted Signal model (extra='forbid', "
    "target_id dropped) so pull() raises ValidationError. Flagged in "
    "real_src_bugs_flagged; src not edited. See PIVOT_BUILD_PLAN."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    data_id: int | str = 1,
    event_date: str = "2026-05-10",
    country: str = "Brazil",
    iso3: str = "BRA",
    event_type: str = "Protests",
    actor1: str = "Protesters",
    actor2: str = "",
    fatalities: int = 0,
    latitude: float = -15.78,
    longitude: float = -47.93,
    location: str = "Brasilia",
    admin1: str = "Distrito Federal",
    notes: str = "Sample notes.",
    source: str = "Local press",
    region: str = "South America",
) -> dict[str, Any]:
    return {
        "data_id": data_id,
        "event_id_cnty": f"BRA{data_id}",
        "event_date": event_date,
        "event_type": event_type,
        "sub_event_type": "Peaceful protest",
        "actor1": actor1,
        "actor2": actor2,
        "assoc_actor_1": "",
        "assoc_actor_2": "",
        "interaction": "60",
        "country": country,
        "iso3": iso3,
        "admin1": admin1,
        "admin2": "",
        "location": location,
        "latitude": latitude,
        "longitude": longitude,
        "geo_precision": 1,
        "source": source,
        "source_scale": "Local",
        "notes": notes,
        "fatalities": fatalities,
        "region": region,
    }


def _make_ctx(
    *,
    config: ACLEDConfig,
    store: InMemoryStateStore | None = None,
    secrets_resolve: Callable[..., Any] | None = None,
    now: datetime | None = None,
) -> SourceContext:
    return SourceContext(
        target_id="target.test.brazil",
        target_version="v0",
        source_id="source.acled.brazil",
        config=config,
        state_store=store or InMemoryStateStore(),
        secrets_resolve=secrets_resolve,
        now_fn=(lambda: now) if now is not None else None,
        logger=logging.getLogger("test.acled"),
    )


class _MockResponses:
    """Builds a list of MockTransport handler functions that respond to the
    record GET requests in sequence. Useful for paginated-response tests.

    OAuth2: a POST to the token endpoint short-circuits to the canned token
    response and is recorded under ``token_calls`` — it is NOT counted as a
    read page, so the GET-sequence indexing (and every ``mock.calls`` length /
    ``page`` assertion) is unaffected by the auth round-trip."""

    def __init__(self, responses: list[Callable[[httpx.Request], httpx.Response]]):
        self._responses = responses
        self._calls: list[httpx.Request] = []
        self._token_calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # OAuth2 token POST → canned Bearer token; never a read page.
        if request.method == "POST" and str(request.url) == ACLED_OAUTH_TOKEN_URL:
            self._token_calls.append(request)
            return _token_response()
        self._calls.append(request)
        idx = min(len(self._calls) - 1, len(self._responses) - 1)
        return self._responses[idx](request)

    @property
    def calls(self) -> list[httpx.Request]:
        """The record GET requests only (token POSTs are excluded)."""
        return self._calls

    @property
    def token_calls(self) -> list[httpx.Request]:
        return self._token_calls


def _ok_response(records: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    def _h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": records, "count": len(records)})
    return _h


def _err_response(status: int, body: Any) -> Callable[[httpx.Request], httpx.Response]:
    def _h(request: httpx.Request) -> httpx.Response:
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=str(body))
    return _h


def _rate_limit_response(
    *, retry_after: str | None = "0", body: dict[str, Any] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    def _h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json=body or {"success": False, "error": "rate_limited"},
            headers=headers,
        )
    return _h


def _patch_client(handler: ACLEDSourceHandler, mock: _MockResponses) -> None:
    """Install a per-call client factory that returns an AsyncClient backed
    by ``MockTransport``. Each call opens a fresh client (matching the
    handler's ``async with`` usage)."""
    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(mock))
    handler._http_client_factory = _factory


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


def test_acled_satisfies_source_handler_protocol() -> None:
    handler = ACLEDSourceHandler()
    assert isinstance(handler, SourceHandler)
    assert handler.kind == "acled"
    assert handler.family == "source"
    assert handler.schema_version.startswith("legba/source.acled/")
    assert handler.config_schema is ACLEDConfig


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_config_minimal_valid() -> None:
    cfg = ACLEDConfig(
        username_secret="vault://acled/user",
        password_secret="vault://acled/pass",
    )
    # OAuth2 defaults.
    assert cfg.client_id == "acled"
    assert cfg.token_url == ACLED_OAUTH_TOKEN_URL
    assert cfg.api_base == ACLED_API_BASE
    assert cfg.email is None
    assert cfg.country is None
    assert cfg.event_types is None
    assert cfg.region is None
    assert cfg.lookback_days == 7
    assert cfg.page_size == ACLED_PAGE_SIZE_MAX


def test_config_rejects_unknown_event_type() -> None:
    with pytest.raises(ValidationError):
        ACLEDConfig(
            username_secret="vault://acled/user",
            password_secret="vault://acled/pass",
            event_types=["Battles", "Mardi Gras"],
        )


def test_config_rejects_unknown_region() -> None:
    with pytest.raises(ValidationError):
        ACLEDConfig(
            username_secret="vault://acled/user",
            password_secret="vault://acled/pass",
            region="Atlantis",
        )


def test_config_country_alpha3() -> None:
    cfg = ACLEDConfig(
        username_secret="vault://acled/user",
        password_secret="vault://acled/pass",
        country="BRA",
    )
    assert cfg.country == "BRA"
    with pytest.raises(ValidationError):
        ACLEDConfig(
            username_secret="vault://acled/user",
            password_secret="vault://acled/pass",
            country="BR",
        )
    with pytest.raises(ValidationError):
        ACLEDConfig(
            username_secret="vault://acled/user",
            password_secret="vault://acled/pass",
            country="BRAZIL",
        )


def test_config_rejects_oversized_page() -> None:
    with pytest.raises(ValidationError):
        ACLEDConfig(
            username_secret="vault://acled/user",
            password_secret="vault://acled/pass",
            page_size=ACLED_PAGE_SIZE_MAX + 1,
        )


def test_config_rejects_legacy_api_key_secret() -> None:
    """OAuth2 migration: ``api_key_secret`` was the legacy auth field. With
    ``extra='forbid'`` a descriptor still carrying it must fail loud rather
    than silently ignore the (now meaningless) key."""
    with pytest.raises(ValidationError):
        ACLEDConfig(
            api_key_secret="vault://acled/key",
            email="ops@example.org",
        )


def test_config_requires_both_oauth_secrets() -> None:
    """Both the username and password vault refs are mandatory."""
    with pytest.raises(ValidationError):
        ACLEDConfig(username_secret="vault://acled/user")
    with pytest.raises(ValidationError):
        ACLEDConfig(password_secret="vault://acled/pass")


def test_config_event_types_whitelist_covers_brief() -> None:
    """All event types listed in the L-132 brief are accepted."""
    cfg = ACLEDConfig(
        username_secret="vault://acled/user",
        password_secret="vault://acled/pass",
        event_types=[
            "Battles",
            "Explosions/Remote violence",
            "Violence against civilians",
            "Protests",
            "Riots",
            "Strategic developments",
        ],
    )
    assert set(cfg.event_types or []) <= ACLED_EVENT_TYPES


# ---------------------------------------------------------------------------
# pull — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_single_page_yields_signals() -> None:
    cfg = ACLEDConfig(
        username_secret="vault://acled/user",
        password_secret="vault://acled/pass",
        country="BRA",
        page_size=5,
    )
    handler = ACLEDSourceHandler()
    records = [_make_record(data_id=i, event_date=f"2026-05-{10 + i:02d}") for i in range(3)]
    mock = _MockResponses([_ok_response(records)])
    _patch_client(handler, mock)

    store = InMemoryStateStore()
    ctx = _make_ctx(config=cfg, store=store)

    collected: list[Signal] = []
    async for sig in handler.pull(ctx, since=datetime(2026, 5, 1, tzinfo=timezone.utc)):
        collected.append(sig)

    assert len(collected) == 3
    s0 = collected[0]
    assert s0.source_id == "source.acled.brazil"
    # Source-first pivot: Signal is target-agnostic (target_id dropped).
    assert s0.content_hash and len(s0.content_hash) == 64
    assert s0.payload["event_type"] == "Protests"
    assert s0.payload["geo"]["iso3"] == "BRA"
    assert s0.payload["geo"]["latitude"] == pytest.approx(-15.78)
    assert s0.payload["actors"]["actor1"] == "Protesters"
    assert s0.payload["fatalities"] == 0
    assert s0.payload["source"] == "Local press"
    assert s0.payload["external_id"] == "0"
    assert s0.raw_provenance["kind"] == "acled"
    assert s0.raw_provenance["external_id"] == "0"
    # Single page below page_size → cursor reset to page=1.
    cursor = store.snapshot().get("acled_cursor")
    assert cursor is not None
    assert cursor["page"] == 1
    assert cursor["last_event_date"] == "2026-05-12"


@pytest.mark.asyncio
async def test_pull_query_params_sent() -> None:
    cfg = ACLEDConfig(
        username_secret="vault://acled/user",
        password_secret="vault://acled/pass",
        country="BRA",
        region="South America",
        event_types=["Protests", "Riots"],
        lookback_days=14,
        page_size=10,
    )
    handler = ACLEDSourceHandler()
    mock = _MockResponses([_ok_response([])])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=cfg)

    async for _ in handler.pull(ctx, since=datetime(2026, 5, 1, tzinfo=timezone.utc)):
        pass

    assert mock.calls, "expected at least one read HTTP call"
    req = mock.calls[0]
    qp = dict(req.url.params)
    # OAuth2: auth is the Bearer header, NOT a query param.
    assert req.headers.get("Authorization") == f"Bearer {_TEST_TOKEN}"
    assert "key" not in qp
    assert "email" not in qp
    assert qp["iso3"] == "BRA"
    assert qp["region"] == "South America"
    assert qp["event_date"] == "2026-05-01"
    assert qp["event_date_where"] == ">="
    assert qp["limit"] == "10"
    assert qp["page"] == "1"
    assert "Protests" in qp["event_type"]
    assert ":OR:" in qp["event_type"]
    # The token POST happened exactly once before the reads.
    assert len(mock.token_calls) == 1


@pytest.mark.asyncio
async def test_pull_uses_secrets_resolver() -> None:
    """When the runtime supplies ``secrets_resolve``, the handler must resolve
    BOTH the username and password vault ids (for the OAuth2 password grant)
    and then authenticate the reads with the resulting Bearer token."""
    cfg = ACLEDConfig(
        username_secret="vault://acled/user",
        password_secret="vault://acled/pass",
    )
    handler = ACLEDSourceHandler()
    mock = _MockResponses([_ok_response([])])
    _patch_client(handler, mock)

    resolver_calls: list[str] = []

    async def fake_resolver(vault_id: str) -> str:
        resolver_calls.append(vault_id)
        # Echo a distinguishable plaintext per id so a swap would be visible.
        return {
            "vault://acled/user": "acled-user@example.org",
            "vault://acled/pass": "s3cr3t",
        }[vault_id]

    ctx = _make_ctx(config=cfg, secrets_resolve=fake_resolver)
    async for _ in handler.pull(ctx, since=None):
        pass

    # The resolver is invoked for BOTH vault ids (order: username, password).
    assert resolver_calls == ["vault://acled/user", "vault://acled/pass"]

    # The resolved creds rode the token POST as form data; the read then
    # carries the Bearer token, and no vault id / credential leaks into the
    # query string.
    assert len(mock.token_calls) == 1
    token_req = mock.token_calls[0]
    token_form = dict(httpx.QueryParams(token_req.content.decode()))
    assert token_form["username"] == "acled-user@example.org"
    assert token_form["password"] == "s3cr3t"
    assert token_form["grant_type"] == "password"

    read_req = mock.calls[0]
    assert read_req.headers.get("Authorization") == f"Bearer {_TEST_TOKEN}"
    qp = dict(read_req.url.params)
    assert "key" not in qp
    assert "email" not in qp
    # Neither the vault id nor a resolved credential leaks into the wire query.
    assert "vault" not in str(read_req.url)
    assert "s3cr3t" not in str(read_req.url)


# ---------------------------------------------------------------------------
# pull — pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_paginates_multiple_pages() -> None:
    cfg = ACLEDConfig(
        username_secret="vault://acled/user",
        password_secret="vault://acled/pass",
        page_size=2,
    )
    handler = ACLEDSourceHandler()
    page1 = [_make_record(data_id=1, event_date="2026-05-10"), _make_record(data_id=2, event_date="2026-05-11")]
    page2 = [_make_record(data_id=3, event_date="2026-05-12"), _make_record(data_id=4, event_date="2026-05-13")]
    page3 = [_make_record(data_id=5, event_date="2026-05-14")]
    mock = _MockResponses([_ok_response(page1), _ok_response(page2), _ok_response(page3)])
    _patch_client(handler, mock)

    store = InMemoryStateStore()
    ctx = _make_ctx(config=cfg, store=store)

    sigs: list[Signal] = []
    async for sig in handler.pull(ctx, since=None):
        sigs.append(sig)

    assert len(sigs) == 5
    # 3 HTTP pages issued; the third page came up short, so we stop.
    pages_requested = [dict(c.url.params).get("page") for c in mock.calls]
    assert pages_requested == ["1", "2", "3"]
    cursor = store.snapshot()["acled_cursor"]
    assert cursor["page"] == 1                                # reset on short page
    assert cursor["last_event_date"] == "2026-05-14"


@pytest.mark.asyncio
async def test_pull_terminates_on_empty_page() -> None:
    cfg = ACLEDConfig(username_secret="vault://acled/user", password_secret="vault://acled/pass", page_size=5)
    handler = ACLEDSourceHandler()
    page1 = [_make_record(data_id=i, event_date=f"2026-05-{10 + i:02d}") for i in range(5)]
    mock = _MockResponses([_ok_response(page1), _ok_response([])])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=cfg)

    sigs = [s async for s in handler.pull(ctx, since=None)]
    assert len(sigs) == 5
    assert len(mock.calls) == 2


@pytest.mark.asyncio
async def test_pull_resumes_from_persisted_cursor() -> None:
    cfg = ACLEDConfig(username_secret="vault://acled/user", password_secret="vault://acled/pass", page_size=2)
    handler = ACLEDSourceHandler()
    mock = _MockResponses([_ok_response([])])
    _patch_client(handler, mock)

    store = InMemoryStateStore(initial={
        "acled_cursor": {"last_event_date": "2026-04-30", "page": 3},
    })
    ctx = _make_ctx(config=cfg, store=store)

    async for _ in handler.pull(ctx, since=datetime(2025, 1, 1, tzinfo=timezone.utc)):
        pass

    qp = dict(mock.calls[0].url.params)
    assert qp["page"] == "3"
    # cursor floor wins over `since` when present.
    assert qp["event_date"] == "2026-04-30"


# ---------------------------------------------------------------------------
# Rate limit / errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ACLEDConfig(username_secret="vault://acled/user", password_secret="vault://acled/pass", page_size=5)
    handler = ACLEDSourceHandler()

    records = [_make_record(data_id=1)]
    mock = _MockResponses([
        _rate_limit_response(retry_after="0"),
        _ok_response(records),
    ])
    _patch_client(handler, mock)

    # Patch sleep so the test never actually sleeps.
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    ctx = _make_ctx(config=cfg)
    sigs = [s async for s in handler.pull(ctx, since=None)]
    assert len(sigs) == 1
    assert len(mock.calls) == 2
    # We slept according to Retry-After before the retry.
    assert sleep_calls and sleep_calls[0] == 0.0


@pytest.mark.asyncio
async def test_pull_exhausts_429_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ACLEDConfig(username_secret="vault://acled/user", password_secret="vault://acled/pass", page_size=5)
    handler = ACLEDSourceHandler()
    # Always rate-limited.
    mock = _MockResponses([_rate_limit_response(retry_after="0")])
    _patch_client(handler, mock)

    async def fake_sleep(_: float) -> None:
        return None
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    ctx = _make_ctx(config=cfg)
    with pytest.raises(httpx.HTTPStatusError):
        async for _ in handler.pull(ctx, since=None):
            pass
    # 5 attempts: initial + 4 retries.
    assert len(mock.calls) == 5


@pytest.mark.asyncio
async def test_pull_raises_on_acled_success_false() -> None:
    cfg = ACLEDConfig(username_secret="vault://acled/user", password_secret="vault://acled/pass")
    handler = ACLEDSourceHandler()
    mock = _MockResponses([
        _err_response(200, {"success": False, "error": "invalid api key"}),
    ])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=cfg)

    with pytest.raises(httpx.HTTPError):
        async for _ in handler.pull(ctx, since=None):
            pass


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_happy() -> None:
    cfg = ACLEDConfig(username_secret="vault://acled/user", password_secret="vault://acled/pass")
    handler = ACLEDSourceHandler()
    mock = _MockResponses([_ok_response([_make_record(data_id=1)])])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=cfg)

    health = await handler.health_check(ctx)
    assert isinstance(health, SourceHealth)
    assert health.state == "healthy"
    assert health.last_error is None
    # The probe sends limit=1.
    qp = dict(mock.calls[0].url.params)
    assert qp["limit"] == "1"


@pytest.mark.asyncio
async def test_health_check_rate_limited() -> None:
    cfg = ACLEDConfig(username_secret="vault://acled/user", password_secret="vault://acled/pass")
    handler = ACLEDSourceHandler()
    mock = _MockResponses([_rate_limit_response()])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=cfg)

    health = await handler.health_check(ctx)
    assert health.state == "degraded"
    assert "429" in (health.last_error or "")


@pytest.mark.asyncio
async def test_health_check_unhealthy_on_5xx() -> None:
    cfg = ACLEDConfig(username_secret="vault://acled/user", password_secret="vault://acled/pass")
    handler = ACLEDSourceHandler()
    mock = _MockResponses([_err_response(503, "Service Unavailable")])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=cfg)

    health = await handler.health_check(ctx)
    assert health.state == "unhealthy"
    assert health.last_error


# ---------------------------------------------------------------------------
# Signal payload shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_payload_carries_required_fields() -> None:
    """Verify that the brief's required Signal fields are populated."""
    cfg = ACLEDConfig(username_secret="vault://acled/user", password_secret="vault://acled/pass", page_size=5)
    handler = ACLEDSourceHandler()
    rec = _make_record(
        data_id=42,
        event_date="2026-05-13",
        country="Brazil",
        iso3="BRA",
        event_type="Riots",
        actor1="Group A",
        actor2="Group B",
        fatalities=3,
        latitude=-23.55,
        longitude=-46.63,
        location="Sao Paulo",
        admin1="Sao Paulo State",
        notes="Clash between groups.",
        source="Wire service",
    )
    mock = _MockResponses([_ok_response([rec])])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=cfg)

    sigs = [s async for s in handler.pull(ctx, since=None)]
    assert len(sigs) == 1
    p = sigs[0].payload

    # Brief item 3 requirements:
    assert p["external_id"] == "42"
    assert p["geo"]["latitude"] == pytest.approx(-23.55)
    assert p["geo"]["longitude"] == pytest.approx(-46.63)
    assert p["geo"]["location"] == "Sao Paulo"
    assert p["geo"]["admin1"] == "Sao Paulo State"
    assert p["actors"]["actor1"] == "Group A"
    assert p["actors"]["actor2"] == "Group B"
    assert p["event_type"] == "Riots"
    assert p["fatalities"] == 3
    assert p["notes"] == "Clash between groups."
    assert p["source"] == "Wire service"
    # Published_at preserved on raw_provenance for downstream filters.
    assert sigs[0].raw_provenance["published_at"] == "2026-05-13T00:00:00+00:00"


@pytest.mark.asyncio
async def test_signal_content_hash_changes_when_record_revised() -> None:
    """A late-edit on notes / fatalities must yield a different hash so the
    L-151 dedupe recognizes the revision as a new payload version."""
    cfg = ACLEDConfig(username_secret="vault://acled/user", password_secret="vault://acled/pass")
    handler = ACLEDSourceHandler()

    rec_v1 = _make_record(data_id=7, fatalities=0)
    rec_v2 = _make_record(data_id=7, fatalities=2)
    mock = _MockResponses([_ok_response([rec_v1, rec_v2])])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=cfg)

    sigs = [s async for s in handler.pull(ctx, since=None)]
    assert sigs[0].content_hash != sigs[1].content_hash
    # external_id stable across the revision.
    assert sigs[0].payload["external_id"] == sigs[1].payload["external_id"]


# ---------------------------------------------------------------------------
# Integration test — gated on env credentials.
# ---------------------------------------------------------------------------


# OAuth2 password grant: the live integration test needs the ACLED account
# username (the registration email) + password. With no resolver in ctx the
# handler treats the *_secret values as literal plaintext (the documented
# bootstrap/test passthrough), so we feed the real creds in directly.
_REAL_USERNAME = os.environ.get("LEGBA_ACLED_USERNAME")
_REAL_PASSWORD = os.environ.get("LEGBA_ACLED_PASSWORD")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not (_REAL_USERNAME and _REAL_PASSWORD),
    reason="LEGBA_ACLED_USERNAME / LEGBA_ACLED_PASSWORD not set",
)
async def test_real_acled_healthcheck_and_pull_one_record() -> None:
    cfg = ACLEDConfig(
        username_secret=_REAL_USERNAME or "",
        password_secret=_REAL_PASSWORD or "",
        country="USA",
        lookback_days=14,
        page_size=1,
    )
    handler = ACLEDSourceHandler()
    ctx = _make_ctx(config=cfg)

    health = await handler.health_check(ctx)
    assert health.state in ("healthy", "degraded"), health

    # Pull one record only — page_size=1 + early break.
    count = 0
    async for sig in handler.pull(ctx, since=None):
        count += 1
        assert sig.payload["event_type"]
        if count >= 1:
            break
    assert count >= 1
