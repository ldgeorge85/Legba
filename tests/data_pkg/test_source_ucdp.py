# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the UCDP GED source handler (S1-T9).

UCDP GED is a TOKEN-GATED conflict-event API: every request must carry a free
access token in the ``x-ucdp-access-token`` header. Tests mock ``httpx`` via
``httpx.MockTransport`` to exercise the full pull / pagination / cursor / health
logic without touching the network, plus a real fixture-file parse. An autouse
fixture supplies a token via the ``LEGBA_UCDP_ACCESS_TOKEN`` env fallback so the
parse/pagination tests exercise the wire logic (not the auth short-circuit); the
dedicated no-token test clears it to prove the clean-degrade path.

Test surface:

  * protocol satisfaction + config defaults / validation (resource / region /
    violence whitelists, page cap).
  * auth: no-token pull/health degrade quietly (no HTTP, no 401 spam); the
    token rides the ``x-ucdp-access-token`` header from either the vault ref
    (``secrets_resolve``) or the env fallback.
  * pull happy-path against the on-disk fixture → one Signal per event, with
    geo + actors + fatalities + event-type mapping asserted.
  * pull pagination — follows ``NextPageUrl``, terminates when it is empty.
  * cursor advancement persists the highest ``date_start`` and clears the
    mid-pull page pointer on drain.
  * cursor advancement makes the next pull use the stored ``StartDate`` floor.
  * ``NextPageUrl`` pointing at a different host is NOT followed (pagination
    stops) — the pin that keeps a compromised API from redirecting our walk.
  * client-side ``type_of_violence`` filter drops non-matching events.
  * ``max_pages`` caps a runaway paginating feed.
  * descriptor round-trips through the real SourceDescriptor schema + the
    production unwrap → UCDPConfig.
  * healthcheck: healthy / degraded / unhealthy paths.
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pytest
import yaml
from pydantic import ValidationError

from legba.data.schemas.source import SourceDescriptor
from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
    SourceHandler,
    SourceHealth,
)
from legba.data.sources.ucdp import (
    UCDP_API_BASE,
    UCDP_DEFAULT_VERSION,
    UCDP_PAGE_SIZE_MAX,
    UCDPConfig,
    UCDPSourceHandler,
)
from legba.runtime.source_factory import (
    _unwrap_factory_dict,
    build_source_handler,
    discover_source_kinds,
)


FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "ucdp_ged_sample.json"
DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parents[2] / "descriptors"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ucdp_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a token via the env fallback by default.

    The parse / pagination / cursor / health tests exercise the wire logic, not
    auth — without a token the handler correctly short-circuits to the clean-
    degrade path and yields nothing. Supplying a token here keeps those tests on
    the fetch path; the dedicated no-token tests ``delenv`` this to assert the
    degrade behavior.
    """
    monkeypatch.setenv("LEGBA_UCDP_ACCESS_TOKEN", "test-ucdp-token")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _make_event(
    *,
    ev_id: int = 1,
    date_start: str = "2023-05-10 00:00:00.000",
    type_of_violence: int = 1,
    country: str = "Syria",
    country_id: int = 652,
    side_a: str = "Government of Syria",
    side_b: str = "Syrian insurgents",
    latitude: float = 35.93,
    longitude: float = 36.63,
    best: int = 6,
) -> dict[str, Any]:
    return {
        "id": ev_id,
        "relid": f"REL-{ev_id}",
        "type_of_violence": type_of_violence,
        "conflict_name": "Syria: Government",
        "dyad_name": f"{side_a} - {side_b}",
        "side_a": side_a,
        "side_b": side_b,
        "side_a_2nd": "",
        "side_b_2nd": "",
        "number_of_sources": 2,
        "source_headline": "headline",
        "where_description": "Idlib city",
        "adm_1": "Idlib province",
        "adm_2": "",
        "latitude": latitude,
        "longitude": longitude,
        "country": country,
        "country_id": country_id,
        "region": "Middle East",
        "date_prec": 1,
        "date_start": date_start,
        "date_end": date_start,
        "deaths_civilians": 2,
        "best": best,
        "high": best + 2,
        "low": best - 1,
    }


def _make_ctx(
    *,
    config: UCDPConfig,
    store: InMemoryStateStore | None = None,
    now: datetime | None = None,
    secrets_resolve: Any = None,
) -> SourceContext:
    return SourceContext(
        target_id="target.test.syria",
        target_version="v0",
        source_id="source.ucdp.ged",
        config=config,
        state_store=store or InMemoryStateStore(),
        secrets_resolve=secrets_resolve,
        now_fn=(lambda: now) if now is not None else None,
        logger=logging.getLogger("test.ucdp"),
    )


class _MockResponses:
    """Serves the GED GETs in sequence, recording every request.

    Each entry is a handler ``(request) -> httpx.Response``; the Nth GET gets
    the Nth handler (clamped to the last). Pagination is driven by the
    ``NextPageUrl`` the handlers embed, so the handler-under-test decides how
    many GETs happen."""

    def __init__(self, responses: list[Callable[[httpx.Request], httpx.Response]]):
        self._responses = responses
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx](request)


def _page_response(
    events: list[dict[str, Any]], *, next_url: str | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def _h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "TotalCount": len(events),
                "TotalPages": 1,
                "PreviousPageUrl": "",
                "NextPageUrl": next_url or "",
                "Result": events,
            },
        )

    return _h


def _patch_client(handler: UCDPSourceHandler, mock: _MockResponses) -> None:
    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(mock))

    handler._http_client_factory = _factory


async def _collect(handler: UCDPSourceHandler, ctx: SourceContext, **kw: Any) -> list[Signal]:
    out: list[Signal] = []
    async for sig in handler.pull(ctx, **kw):
        out.append(sig)
    return out


# ---------------------------------------------------------------------------
# Protocol + config
# ---------------------------------------------------------------------------


def test_ucdp_satisfies_source_handler_protocol() -> None:
    handler = UCDPSourceHandler()
    assert isinstance(handler, SourceHandler)
    assert handler.kind == "ucdp"
    assert handler.family == "source"
    assert handler.schema_version.startswith("legba/source.ucdp/")
    assert handler.config_schema is UCDPConfig


def test_config_defaults() -> None:
    cfg = UCDPConfig()
    assert cfg.api_base == UCDP_API_BASE
    assert cfg.resource == "gedevents"
    assert cfg.version == UCDP_DEFAULT_VERSION
    assert cfg.country_ids is None
    assert cfg.region_ids is None
    assert cfg.type_of_violence is None
    assert cfg.lookback_days == 365
    assert cfg.page_size == UCDP_PAGE_SIZE_MAX
    assert cfg.max_pages == 500


def test_config_rejects_unknown_resource() -> None:
    with pytest.raises(ValidationError):
        UCDPConfig(resource="notarealresource")


def test_config_rejects_unknown_region_id() -> None:
    with pytest.raises(ValidationError):
        UCDPConfig(region_ids=[1, 9])


def test_config_rejects_unknown_violence_type() -> None:
    with pytest.raises(ValidationError):
        UCDPConfig(type_of_violence=[1, 4])


def test_config_rejects_oversized_page() -> None:
    with pytest.raises(ValidationError):
        UCDPConfig(page_size=UCDP_PAGE_SIZE_MAX + 1)


def test_config_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        UCDPConfig(api_key_secret="vault://ucdp/key")


def test_config_accepts_access_token_secret() -> None:
    # The credential is a vault ref, defaulting to None (no token configured).
    assert UCDPConfig().access_token_secret is None
    cfg = UCDPConfig(access_token_secret="source.ucdp.access_token")
    assert cfg.access_token_secret == "source.ucdp.access_token"


# ---------------------------------------------------------------------------
# pull — happy path against the on-disk fixture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_parses_fixture_into_signals() -> None:
    fixture = _load_fixture()
    handler = UCDPSourceHandler()

    def _serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture)

    mock = _MockResponses([_serve])
    _patch_client(handler, mock)

    store = InMemoryStateStore()
    ctx = _make_ctx(config=UCDPConfig(), store=store)
    collected = await _collect(
        handler, ctx, since=datetime(2023, 1, 1, tzinfo=timezone.utc)
    )

    assert len(collected) == 2
    s0 = collected[0]
    assert s0.source_id == "source.ucdp.ged"
    assert s0.content_hash and len(s0.content_hash) == 64
    assert s0.payload["event_type"] == "state-based"
    assert s0.payload["type_of_violence"] == 1
    assert s0.payload["actors"]["side_a"] == "Government of Syria"
    assert s0.payload["actors"]["side_b"] == "Syrian insurgents"
    assert s0.payload["geo"]["country"] == "Syria"
    assert s0.payload["geo"]["latitude"] == pytest.approx(35.9306)
    assert s0.payload["geo"]["region"] == "Middle East"
    assert s0.payload["fatalities"] == 6
    assert s0.payload["fatalities_estimates"]["high"] == 8
    assert s0.payload["external_id"] == "512345"
    assert s0.raw_provenance["kind"] == "ucdp"
    assert s0.raw_provenance["external_id"] == "512345"
    assert s0.raw_provenance["published_at"] == "2023-05-10T00:00:00+00:00"
    # One-sided violence event maps its label.
    assert collected[1].payload["event_type"] == "one-sided"
    assert collected[1].payload["geo"]["country"] == "Sudan"

    # Single page, empty NextPageUrl → cursor advanced to highest date_start,
    # page pointer cleared.
    cursor = store.snapshot().get("ucdp_cursor")
    assert cursor is not None
    assert cursor["last_date_start"] == "2023-06-02"
    assert cursor["next_page_url"] is None


@pytest.mark.asyncio
async def test_pull_query_params_sent() -> None:
    cfg = UCDPConfig(
        version="24.1",
        country_ids=[652],
        region_ids=[2],
        page_size=250,
    )
    handler = UCDPSourceHandler()
    mock = _MockResponses([_page_response([])])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=cfg)

    await _collect(handler, ctx, since=datetime(2023, 5, 1, tzinfo=timezone.utc))

    assert len(mock.calls) == 1
    url = mock.calls[0].url
    assert url.path == "/api/gedevents/24.1"
    assert url.params["pagesize"] == "250"
    assert url.params["StartDate"] == "2023-05-01"
    assert url.params["Country"] == "652"
    assert url.params["Region"] == "2"


# ---------------------------------------------------------------------------
# pull — pagination + cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_follows_next_page_url() -> None:
    handler = UCDPSourceHandler()
    page2 = f"{UCDP_API_BASE}/gedevents/24.1?page=1&pagesize=1000&StartDate=2023-01-01"
    ev1 = [_make_event(ev_id=1, date_start="2023-05-10 00:00:00.000")]
    ev2 = [_make_event(ev_id=2, date_start="2023-05-20 00:00:00.000")]
    mock = _MockResponses(
        [
            _page_response(ev1, next_url=page2),
            _page_response(ev2, next_url=None),
        ]
    )
    _patch_client(handler, mock)
    store = InMemoryStateStore()
    ctx = _make_ctx(config=UCDPConfig(), store=store)

    collected = await _collect(
        handler, ctx, since=datetime(2023, 1, 1, tzinfo=timezone.utc)
    )
    assert [s.payload["external_id"] for s in collected] == ["1", "2"]
    # Two GETs: page 1 then the followed NextPageUrl.
    assert len(mock.calls) == 2
    assert str(mock.calls[1].url) == page2
    cursor = store.snapshot()["ucdp_cursor"]
    assert cursor["last_date_start"] == "2023-05-20"
    assert cursor["next_page_url"] is None


@pytest.mark.asyncio
async def test_cursor_floor_used_on_next_pull() -> None:
    handler = UCDPSourceHandler()
    store = InMemoryStateStore()
    # Prime a cursor from a prior pull.
    await store.set(
        "ucdp_cursor", {"last_date_start": "2024-02-15", "next_page_url": None}
    )
    mock = _MockResponses([_page_response([])])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=UCDPConfig(), store=store)

    await _collect(handler, ctx, since=datetime(2020, 1, 1, tzinfo=timezone.utc))
    # The stored cursor floor (2024-02-15) beats the older `since`.
    assert mock.calls[0].url.params["StartDate"] == "2024-02-15"


@pytest.mark.asyncio
async def test_next_page_url_wrong_host_not_followed() -> None:
    handler = UCDPSourceHandler()
    evil = "https://attacker.example/api/gedevents/24.1?page=1"
    ev1 = [_make_event(ev_id=1)]
    mock = _MockResponses(
        [
            _page_response(ev1, next_url=evil),
            _page_response([_make_event(ev_id=99)], next_url=None),
        ]
    )
    _patch_client(handler, mock)
    ctx = _make_ctx(config=UCDPConfig())

    collected = await _collect(
        handler, ctx, since=datetime(2023, 1, 1, tzinfo=timezone.utc)
    )
    # Only the first page is fetched; the cross-host NextPageUrl is ignored.
    assert [s.payload["external_id"] for s in collected] == ["1"]
    assert len(mock.calls) == 1


@pytest.mark.asyncio
async def test_max_pages_caps_runaway() -> None:
    handler = UCDPSourceHandler()
    self_next = f"{UCDP_API_BASE}/gedevents/24.1?page=1&pagesize=1000"
    # Every page points at another page → would loop forever without the cap.
    mock = _MockResponses([_page_response([_make_event(ev_id=1)], next_url=self_next)])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=UCDPConfig(max_pages=3))

    collected = await _collect(
        handler, ctx, since=datetime(2023, 1, 1, tzinfo=timezone.utc)
    )
    assert len(collected) == 3
    assert len(mock.calls) == 3


@pytest.mark.asyncio
async def test_client_side_violence_filter() -> None:
    handler = UCDPSourceHandler()
    events = [
        _make_event(ev_id=1, type_of_violence=1),
        _make_event(ev_id=2, type_of_violence=2),
        _make_event(ev_id=3, type_of_violence=3),
    ]
    mock = _MockResponses([_page_response(events)])
    _patch_client(handler, mock)
    store = InMemoryStateStore()
    cfg = UCDPConfig(type_of_violence=[1, 3])
    ctx = _make_ctx(config=cfg, store=store)

    collected = await _collect(
        handler, ctx, since=datetime(2023, 1, 1, tzinfo=timezone.utc)
    )
    # type_of_violence 2 dropped; 1 + 3 kept.
    assert sorted(s.payload["external_id"] for s in collected) == ["1", "3"]
    # High-water still advances across ALL seen events (dedupe absorbs overlap).
    assert store.snapshot()["ucdp_cursor"]["last_date_start"] == "2023-05-10"


# ---------------------------------------------------------------------------
# auth — access token (x-ucdp-access-token header) + clean degrade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_degrades_quietly_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token → skip the pull entirely: no HTTP, no cursor mutation, and a
    quiet 'ucdp: no token configured' note on the health counters."""
    monkeypatch.delenv("LEGBA_UCDP_ACCESS_TOKEN", raising=False)
    handler = UCDPSourceHandler()
    mock = _MockResponses([_page_response([_make_event(ev_id=1)])])
    _patch_client(handler, mock)
    store = InMemoryStateStore()
    # No access_token_secret and no secrets_resolve on the ctx.
    ctx = _make_ctx(config=UCDPConfig(), store=store)

    collected = await _collect(
        handler, ctx, since=datetime(2023, 1, 1, tzinfo=timezone.utc)
    )
    assert collected == []
    # The degrade path must NOT touch the network (this is the anti-401-spam
    # guarantee) and must not advance/create the cursor.
    assert mock.calls == []
    assert store.snapshot().get("ucdp_cursor") is None
    assert handler._health.last_status == "degraded"
    assert handler._health.last_error == "ucdp: no token configured"


@pytest.mark.asyncio
async def test_health_check_degraded_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEGBA_UCDP_ACCESS_TOKEN", raising=False)
    handler = UCDPSourceHandler()
    mock = _MockResponses([_page_response([_make_event(ev_id=1)])])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=UCDPConfig())

    health = await handler.health_check(ctx)
    assert health.state == "degraded"
    assert health.last_error == "ucdp: no token configured"
    # Degrades WITHOUT probing — an unauthenticated probe would just 401.
    assert mock.calls == []


@pytest.mark.asyncio
async def test_pull_attaches_token_from_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault ref resolved via secrets_resolve → x-ucdp-access-token header.

    The env fallback is cleared to prove the token comes from the vault ref."""
    monkeypatch.delenv("LEGBA_UCDP_ACCESS_TOKEN", raising=False)
    handler = UCDPSourceHandler()
    mock = _MockResponses([_page_response([_make_event(ev_id=1)])])
    _patch_client(handler, mock)

    seen: list[str] = []

    async def _resolver(vault_id: str) -> str:
        seen.append(vault_id)
        return "SECRET-TOKEN-123"

    cfg = UCDPConfig(access_token_secret="source.ucdp.access_token")
    ctx = _make_ctx(config=cfg, secrets_resolve=_resolver)

    collected = await _collect(
        handler, ctx, since=datetime(2023, 1, 1, tzinfo=timezone.utc)
    )
    assert len(collected) == 1
    assert seen == ["source.ucdp.access_token"]
    assert len(mock.calls) == 1
    assert mock.calls[0].headers["x-ucdp-access-token"] == "SECRET-TOKEN-123"


@pytest.mark.asyncio
async def test_pull_attaches_token_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env fallback (no vault ref) → x-ucdp-access-token header."""
    monkeypatch.setenv("LEGBA_UCDP_ACCESS_TOKEN", "ENV-TOKEN-9")
    handler = UCDPSourceHandler()
    mock = _MockResponses([_page_response([_make_event(ev_id=1)])])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=UCDPConfig())

    collected = await _collect(
        handler, ctx, since=datetime(2023, 1, 1, tzinfo=timezone.utc)
    )
    assert len(collected) == 1
    assert mock.calls[0].headers["x-ucdp-access-token"] == "ENV-TOKEN-9"


@pytest.mark.asyncio
async def test_vault_ref_takes_precedence_over_env() -> None:
    """When both are present the vault ref wins (env is only a fallback)."""
    handler = UCDPSourceHandler()  # env token set by the autouse fixture
    mock = _MockResponses([_page_response([_make_event(ev_id=1)])])
    _patch_client(handler, mock)

    async def _resolver(vault_id: str) -> str:
        return "VAULT-WINS"

    cfg = UCDPConfig(access_token_secret="source.ucdp.access_token")
    ctx = _make_ctx(config=cfg, secrets_resolve=_resolver)

    await _collect(handler, ctx, since=datetime(2023, 1, 1, tzinfo=timezone.utc))
    assert mock.calls[0].headers["x-ucdp-access-token"] == "VAULT-WINS"


@pytest.mark.asyncio
async def test_token_rides_followed_next_page() -> None:
    """The token header rides EVERY request, including followed NextPageUrl."""
    handler = UCDPSourceHandler()  # env token from the autouse fixture
    page2 = f"{UCDP_API_BASE}/gedevents/24.1?page=1&pagesize=1000&StartDate=2023-01-01"
    mock = _MockResponses(
        [
            _page_response([_make_event(ev_id=1)], next_url=page2),
            _page_response([_make_event(ev_id=2)], next_url=None),
        ]
    )
    _patch_client(handler, mock)
    ctx = _make_ctx(config=UCDPConfig())

    await _collect(handler, ctx, since=datetime(2023, 1, 1, tzinfo=timezone.utc))
    assert len(mock.calls) == 2
    assert mock.calls[0].headers["x-ucdp-access-token"] == "test-ucdp-token"
    assert mock.calls[1].headers["x-ucdp-access-token"] == "test-ucdp-token"


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_healthy() -> None:
    handler = UCDPSourceHandler()
    mock = _MockResponses([_page_response([_make_event(ev_id=1)])])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=UCDPConfig())

    health = await handler.health_check(ctx)
    assert isinstance(health, SourceHealth)
    assert health.state == "healthy"
    assert mock.calls[0].url.params["pagesize"] == "1"


@pytest.mark.asyncio
async def test_health_check_degraded_on_bad_body() -> None:
    handler = UCDPSourceHandler()

    def _bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    mock = _MockResponses([_bad])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=UCDPConfig())

    health = await handler.health_check(ctx)
    assert health.state == "degraded"


@pytest.mark.asyncio
async def test_health_check_unhealthy_on_http_error() -> None:
    handler = UCDPSourceHandler()

    def _err(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    mock = _MockResponses([_err])
    _patch_client(handler, mock)
    ctx = _make_ctx(config=UCDPConfig())

    health = await handler.health_check(ctx)
    assert health.state == "unhealthy"


# ---------------------------------------------------------------------------
# descriptor round-trip + factory discovery
# ---------------------------------------------------------------------------


def test_descriptor_round_trips_and_config_parses() -> None:
    body = yaml.safe_load((DESCRIPTORS_DIR / "source_ucdp_ged.yaml").read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = SourceDescriptor.model_validate(body, strict=False)
    assert desc.identity.kind == "ucdp"
    assert desc.acquisition == "poll"
    assert desc.cadence is not None and desc.cadence.schedule is not None
    # The config parses through the production unwrap → UCDPConfig, exactly as
    # build_source_handler does before a SourceActor constructs the handler.
    parsed = UCDPConfig(**_unwrap_factory_dict(desc.config))
    assert parsed.version == "24.1"
    assert parsed.lookback_days == 365


def test_factory_discovers_ucdp_kind() -> None:
    registry = discover_source_kinds()
    assert "ucdp" in registry
    assert registry["ucdp"] is UCDPSourceHandler
    # The factory constructs the zero-arg handler (config rides SourceContext).
    handler = build_source_handler("ucdp", {}, registry=registry)
    assert handler.kind == "ucdp"
