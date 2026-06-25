# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :class:`legba.data.sources.json_api.JsonApiSourceHandler` (S-3).

The generic polled JSON/CSV HTTP API kind. Coverage:

  * Config schema validation (defaults, unknown field, bad path syntax,
    unknown url_template placeholder, auth shape, csv/items_path conflict).
  * L-102 class-var contract + factory-tuple registration.
  * URL template substitution — cursor-driven window values, URL-quoting.
  * Items extraction incl. nested + bracket paths; CSV rows via stdlib csv.
  * Signal emission + dedupe key (content_hash stable across pulls,
    external_id resolution + fallbacks, no secret in payload/provenance).
  * Auth: header + query application; FAIL-LOUD refusal when a SecretRef is
    declared and no resolver is wired (on_configure / on_activate / pull
    raise, health_check reports unhealthy).
  * Cursor advance: last_pulled_at persisted on success, next window starts
    there, already-seen (timestamp <= window start) items are skipped; cursor
    NOT advanced on fetch failure.
  * One env-gated live probe (LEGBA_LIVE_PROBES=1) against ReliefWeb.

httpx is mocked via :class:`httpx.MockTransport` — the HTTP wire is the one
fake; URL rendering, JSON/CSV parsing, path resolution, and Signal mapping
all run for real.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
import pytest
from pydantic import ValidationError

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
)
from legba.data.sources.json_api import (
    _JSON_API_CURSOR_KEY,
    _JSON_API_HEALTH_KEY,
    JsonApiAuth,
    JsonApiAuthNotConfigured,
    JsonApiConfig,
    JsonApiSourceHandler,
    parse_path,
    resolve_path,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


FROZEN_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


NESTED_DOC = json.dumps({
    "meta": {"count": 2},
    "result": {
        "items": [
            {
                "id": 101,
                "attrs": {
                    "title": "First report",
                    "url": "https://upstream.invalid/r/101",
                    "date": {"created": "2026-06-09T11:30:00+00:00"},
                    "body": "body one",
                    "country": "bra",
                },
            },
            {
                "id": 102,
                "attrs": {
                    "title": "Second report",
                    "url": "https://upstream.invalid/r/102",
                    "date": {"created": "2026-06-09T09:00:00+00:00"},
                    "body": "body two",
                    "country": "United States",   # not ISO — must be dropped
                },
            },
            "junk-non-dict-item",
        ],
    },
})


CSV_DOC = "id,title,url\nrow-1,CSV one,https://upstream.invalid/c/1\nrow-2,CSV two,https://upstream.invalid/c/2\n"


def _nested_config(**overrides) -> JsonApiConfig:
    base = dict(
        url_template="https://upstream.invalid/api?from={window_start_iso}&to={window_end_iso}",
        items_path="result.items",
        id_path="id",
        title_path="attrs.title",
        url_path="attrs.url",
        timestamp_path="attrs.date.created",
        body_path="attrs.body",
        geo_path="attrs.country",
    )
    base.update(overrides)
    return JsonApiConfig(**base)


def _make_ctx(
    state: InMemoryStateStore | None = None,
    *,
    config: JsonApiConfig | None = None,
    secrets_resolve=None,
) -> SourceContext:
    return SourceContext(
        target_id="target.api",
        target_version="v-test",
        source_id="src.json_api_test",
        config=config or _nested_config(),
        state_store=state or InMemoryStateStore(),
        secrets_resolve=secrets_resolve,
        now_fn=lambda: FROZEN_NOW,
        scope_geo=["BRA"],
        scope_languages=["en"],
    )


def _make_handler(
    transport_handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: JsonApiConfig | None = None,
    secret_resolver=None,
) -> JsonApiSourceHandler:
    transport = httpx.MockTransport(transport_handler)
    client = httpx.AsyncClient(transport=transport, follow_redirects=True, timeout=5)
    return JsonApiSourceHandler(
        config or _nested_config(),
        http_client=client,
        secret_resolver=secret_resolver,
    )


async def _collect(it) -> list[Signal]:
    out: list[Signal] = []
    async for s in it:
        out.append(s)
    return out


def _ok_nested(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=NESTED_DOC, request=req)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_defaults():
    cfg = JsonApiConfig(url_template="https://x.invalid/api")
    assert cfg.method == "GET"
    assert cfg.response_format == "json"
    assert cfg.auth is None
    assert cfg.items_path == ""
    assert cfg.id_path == "id"
    assert cfg.modality == "text"
    assert cfg.lookback_minutes == 1440
    assert cfg.max_items_per_pull == 100
    assert cfg.timeout_seconds == 30


def test_config_rejects_unknown_field():
    with pytest.raises(ValidationError):
        JsonApiConfig(url_template="https://x.invalid/api", nope=1)  # type: ignore[call-arg]


def test_config_rejects_unknown_placeholder():
    with pytest.raises(ValidationError, match="unknown url_template placeholder"):
        JsonApiConfig(url_template="https://x.invalid/api?k={api_key}")


def test_config_rejects_positional_placeholder():
    with pytest.raises(ValidationError, match="must be named"):
        JsonApiConfig(url_template="https://x.invalid/api?k={}")


def test_config_rejects_bad_path_syntax():
    with pytest.raises(ValidationError):
        JsonApiConfig(url_template="https://x.invalid/api", items_path="a..b")
    with pytest.raises(ValidationError):
        JsonApiConfig(url_template="https://x.invalid/api", title_path="a[unclosed")


def test_config_rejects_items_path_for_csv():
    with pytest.raises(ValidationError, match="items_path must be empty"):
        JsonApiConfig(
            url_template="https://x.invalid/api",
            response_format="csv",
            items_path="rows",
        )


def test_auth_config_shape():
    auth = JsonApiAuth(
        mode="header",
        name="Authorization",
        secret_ref="reliefweb.api_key",
        value_template="Bearer {secret}",
    )
    assert auth.mode == "header"
    # secret_ref is a dotted vault id, never a raw secret shape.
    with pytest.raises(ValidationError):
        JsonApiAuth(name="X-Key", secret_ref="has space")
    with pytest.raises(ValidationError):
        JsonApiAuth(name="X-Key", secret_ref="a/b")
    # value_template must carry the {secret} slot.
    with pytest.raises(ValidationError, match="secret"):
        JsonApiAuth(name="X-Key", secret_ref="x.y", value_template="Bearer fixed")
    # header-injection guard on the name.
    with pytest.raises(ValidationError):
        JsonApiAuth(name="X Key", secret_ref="x.y")


def test_handler_class_contract():
    assert JsonApiSourceHandler.kind == "json_api"
    assert JsonApiSourceHandler.family == "source"
    assert JsonApiSourceHandler.schema_version == "legba/source.json_api/1-0-0"
    assert JsonApiSourceHandler.config_schema is JsonApiConfig
    assert JsonApiSourceHandler.handler_version


def test_factory_registration():
    from legba.runtime.source_factory import build_source_handler, discover_source_kinds

    registry = discover_source_kinds()
    assert registry["json_api"] is JsonApiSourceHandler
    handler = build_source_handler(
        "json_api",
        {"url_template": {"factory_kind": "text", "raw": "https://x.invalid/api"}},
        registry=registry,
    )
    assert isinstance(handler, JsonApiSourceHandler)


# ---------------------------------------------------------------------------
# JSONPath-lite resolver
# ---------------------------------------------------------------------------


def test_parse_path_forms():
    assert parse_path("") == []
    assert parse_path("a.b.c") == ["a", "b", "c"]
    assert parse_path("a[0].b") == ["a", 0, "b"]
    assert parse_path("['k with spaces'].x") == ["k with spaces", "x"]
    assert parse_path('["dotted.key"]') == ["dotted.key"]
    assert parse_path("[2]") == [2]
    assert parse_path("snake_case-hyph") == ["snake_case-hyph"]
    for bad in (".a", "a..b", "a[", "a[1", "a['x]", "a[?]"):
        with pytest.raises(ValueError):
            parse_path(bad)


def test_resolve_path_walk_and_misses():
    doc = {"a": {"b": [{"c": 7}]}, "dotted.key": "v"}
    assert resolve_path(doc, parse_path("a.b[0].c")) == 7
    assert resolve_path(doc, parse_path('["dotted.key"]')) == "v"
    assert resolve_path(doc, parse_path("a.b[9].c")) is None
    assert resolve_path(doc, parse_path("a.zzz")) is None
    assert resolve_path(doc, parse_path("a.b.c")) is None  # list under str key


# ---------------------------------------------------------------------------
# URL template substitution (cursor-driven window)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_url_template_substitution_first_run_window():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, text=NESTED_DOC, request=req)

    cfg = _nested_config(
        url_template=(
            "https://upstream.invalid/api?d={date_today}&y={date_yesterday}"
            "&from={window_start_iso}&to={window_end_iso}"
        ),
        lookback_minutes=120,
    )
    ja = _make_handler(handler, config=cfg)
    await _collect(ja.pull(_make_ctx(config=cfg), since=None))
    await ja.aclose()

    # The raw rendered URL is percent-encoded by the handler; assert on the
    # decoded query params (robust against httpx URL normalization).
    params = captured[0].url.params
    assert params["d"] == "2026-06-09"
    assert params["y"] == "2026-06-08"
    # Window = now - 120 min; the '+00:00' offset survives quoting round-trip.
    assert params["from"] == "2026-06-09T10:00:00+00:00"
    assert params["to"] == "2026-06-09T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Items extraction (nested paths + root array + csv) and signal emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_nested_items_signal_shape_and_dedupe_key():
    state = InMemoryStateStore()
    ctx = _make_ctx(state)
    ja = _make_handler(_ok_nested)
    signals = await _collect(ja.pull(ctx, since=None))
    await ja.aclose()

    # 2 dict items (the junk string entry is skipped).
    assert len(signals) == 2
    s = signals[0]
    assert s.source_id == "src.json_api_test"
    assert s.modality == "text"
    assert s.mime_type is None
    assert s.payload["external_id"] == "101"          # numeric id coerced
    assert s.payload["title"] == "First report"
    assert s.canonical_url == "https://upstream.invalid/r/101"
    assert s.payload["raw_body"] == "body one"
    assert s.payload["published_at"] == "2026-06-09T11:30:00+00:00"
    # geo: extracted code normalized to uppercase ISO-ish.
    assert s.geo == ["BRA"]
    # geo_path value not ISO-shaped → falls back to descriptor scope geo.
    assert signals[1].geo == ["BRA"]
    # language hint from the single scope language.
    assert s.language_hint == "en"
    # full raw item preserved for downstream.
    assert s.payload["item"]["attrs"]["title"] == "First report"
    # provenance carries the template + rendered request URL.
    assert s.raw_provenance["fetch_kind"] == "json_api"
    assert s.raw_provenance["request_url"].startswith("https://upstream.invalid/api?")

    # Dedupe key is stable: an identical re-pull produces identical hashes.
    state2 = InMemoryStateStore()
    ja2 = _make_handler(_ok_nested)
    signals2 = await _collect(ja2.pull(_make_ctx(state2), since=None))
    await ja2.aclose()
    assert [x.content_hash for x in signals] == [x.content_hash for x in signals2]
    assert signals[0].content_hash != signals[1].content_hash


@pytest.mark.asyncio
async def test_pull_root_array_and_id_fallbacks():
    doc = json.dumps([
        {"title": "no id, has url", "url": "https://u.invalid/a"},
        {"title": "no id no url"},
    ])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=doc, request=req)

    cfg = JsonApiConfig(
        url_template="https://x.invalid/api",
        items_path="",
        title_path="title",
        url_path="url",
    )
    signals = await _collect(
        _make_handler(handler, config=cfg).pull(_make_ctx(config=cfg), since=None)
    )
    assert len(signals) == 2
    # id falls back to the url, then to a stable content-hash prefix.
    assert signals[0].payload["external_id"] == "https://u.invalid/a"
    assert len(signals[1].payload["external_id"]) == 24


@pytest.mark.asyncio
async def test_pull_structured_modality_stamps_mime():
    cfg = _nested_config(modality="structured", static_tags=["humanitarian"])

    signals = await _collect(
        _make_handler(_ok_nested, config=cfg).pull(_make_ctx(config=cfg), since=None)
    )
    assert signals[0].modality == "structured"
    assert signals[0].mime_type == "application/json"
    assert signals[0].tags == ["humanitarian"]


@pytest.mark.asyncio
async def test_pull_csv_rows_are_items():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=CSV_DOC, request=req)

    cfg = JsonApiConfig(
        url_template="https://x.invalid/export.csv",
        response_format="csv",
        id_path="id",
        title_path="title",
        url_path="url",
    )
    signals = await _collect(
        _make_handler(handler, config=cfg).pull(_make_ctx(config=cfg), since=None)
    )
    assert [s.payload["external_id"] for s in signals] == ["row-1", "row-2"]
    assert signals[0].payload["title"] == "CSV one"
    assert signals[0].canonical_url == "https://upstream.invalid/c/1"


@pytest.mark.asyncio
async def test_items_path_miss_is_unhealthy():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"result": {"items": "nope"}}), request=req)

    state = InMemoryStateStore()
    signals = await _collect(_make_handler(handler).pull(_make_ctx(state), since=None))
    assert signals == []
    assert state.snapshot()[_JSON_API_HEALTH_KEY]["state"] == "unhealthy"
    # No cursor written for a failed parse.
    assert _JSON_API_CURSOR_KEY not in state.snapshot()


@pytest.mark.asyncio
async def test_max_items_per_pull_caps():
    doc = json.dumps([{"id": f"i{i}"} for i in range(10)])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=doc, request=req)

    cfg = JsonApiConfig(url_template="https://x.invalid/api", max_items_per_pull=3)
    signals = await _collect(
        _make_handler(handler, config=cfg).pull(_make_ctx(config=cfg), since=None)
    )
    assert len(signals) == 3


# ---------------------------------------------------------------------------
# Auth — application + fail-loud refusal
# ---------------------------------------------------------------------------


def _auth_cfg(mode: str) -> JsonApiConfig:
    return _nested_config(
        auth=JsonApiAuth(
            mode=mode,  # type: ignore[arg-type]
            name="Authorization" if mode == "header" else "key",
            secret_ref="upstream.api_key",
            value_template="Bearer {secret}" if mode == "header" else "{secret}",
        ),
    )


async def _fake_resolver(vault_id: str) -> str:
    assert vault_id == "upstream.api_key"
    return "sekrit-123"


@pytest.mark.asyncio
async def test_auth_header_applied_and_never_leaked():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, text=NESTED_DOC, request=req)

    cfg = _auth_cfg("header")
    ja = _make_handler(handler, config=cfg)
    ctx = _make_ctx(config=cfg, secrets_resolve=_fake_resolver)
    signals = await _collect(ja.pull(ctx, since=None))
    await ja.aclose()

    assert captured[0].headers["Authorization"] == "Bearer sekrit-123"
    # The secret never reaches payload / provenance / rendered URL.
    dumped = json.dumps([s.payload | s.raw_provenance for s in signals], default=str)
    assert "sekrit-123" not in dumped
    assert "sekrit-123" not in str(captured[0].url)


@pytest.mark.asyncio
async def test_auth_query_param_applied_but_not_in_provenance_url():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, text=NESTED_DOC, request=req)

    cfg = _auth_cfg("query")
    ja = _make_handler(handler, config=cfg)
    signals = await _collect(
        ja.pull(_make_ctx(config=cfg, secrets_resolve=_fake_resolver), since=None)
    )
    await ja.aclose()

    assert captured[0].url.params["key"] == "sekrit-123"
    # The auth param is applied at request time only — the request_url stamped
    # into payload/provenance is the auth-free rendered template.
    assert "sekrit-123" not in signals[0].raw_provenance["request_url"]
    assert "sekrit-123" not in signals[0].payload["source_url"]


@pytest.mark.asyncio
async def test_auth_missing_resolver_refuses_loud():
    cfg = _auth_cfg("header")
    ja = _make_handler(_ok_nested, config=cfg)
    ctx = _make_ctx(config=cfg, secrets_resolve=None)

    with pytest.raises(JsonApiAuthNotConfigured):
        await ja.on_configure(ctx)
    with pytest.raises(JsonApiAuthNotConfigured):
        await ja.on_activate(ctx)
    with pytest.raises(JsonApiAuthNotConfigured):
        await _collect(ja.pull(ctx, since=None))
    health = await ja.health_check(ctx)
    assert health.state == "unhealthy"
    assert "secret_ref" in (health.last_error or "")
    await ja.aclose()


@pytest.mark.asyncio
async def test_auth_constructor_resolver_satisfies_activation():
    cfg = _auth_cfg("header")
    ja = _make_handler(_ok_nested, config=cfg, secret_resolver=_fake_resolver)
    ctx = _make_ctx(config=cfg, secrets_resolve=None)
    await ja.on_activate(ctx)  # does not raise
    signals = await _collect(ja.pull(ctx, since=None))
    await ja.aclose()
    assert len(signals) == 2


# ---------------------------------------------------------------------------
# Cursor advance + window filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_advances_to_window_end_on_success():
    state = InMemoryStateStore()
    ja = _make_handler(_ok_nested)
    await _collect(ja.pull(_make_ctx(state), since=None))
    await ja.aclose()
    cur = state.snapshot()[_JSON_API_CURSOR_KEY]
    assert cur["last_pulled_at"] == FROZEN_NOW.isoformat()
    health = state.snapshot()[_JSON_API_HEALTH_KEY]
    assert health["state"] == "healthy"
    assert health["detail"]["items_yielded"] == 2


@pytest.mark.asyncio
async def test_second_pull_window_starts_at_cursor_and_filters_seen_items():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, text=NESTED_DOC, request=req)

    # Pre-seed the cursor between the two items' timestamps (11:30 / 09:00).
    seen_floor = datetime(2026, 6, 9, 10, 0, 0, tzinfo=timezone.utc)
    state = InMemoryStateStore(
        {_JSON_API_CURSOR_KEY: {"last_pulled_at": seen_floor.isoformat()}}
    )
    ja = _make_handler(handler)
    signals = await _collect(ja.pull(_make_ctx(state), since=None))
    await ja.aclose()

    # The window start is the stored cursor, substituted into the URL...
    assert captured[0].url.params["from"] == "2026-06-09T10:00:00+00:00"
    # ...and the 09:00 item (<= window start) is filtered; 11:30 passes.
    assert [s.payload["external_id"] for s in signals] == ["101"]
    # Cursor advanced to the new window end.
    assert state.snapshot()[_JSON_API_CURSOR_KEY]["last_pulled_at"] == FROZEN_NOW.isoformat()


@pytest.mark.asyncio
async def test_since_param_used_when_no_cursor():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, text=NESTED_DOC, request=req)

    since = datetime(2026, 6, 9, 11, 0, 0, tzinfo=timezone.utc)
    ja = _make_handler(handler)
    signals = await _collect(ja.pull(_make_ctx(), since=since))
    await ja.aclose()
    assert captured[0].url.params["from"] == "2026-06-09T11:00:00+00:00"
    # Only the 11:30 item is strictly after `since`.
    assert [s.payload["external_id"] for s in signals] == ["101"]


@pytest.mark.asyncio
async def test_cursor_not_advanced_on_http_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=req)

    floor_iso = "2026-06-09T10:00:00+00:00"
    state = InMemoryStateStore({_JSON_API_CURSOR_KEY: {"last_pulled_at": floor_iso}})
    ja = _make_handler(handler)
    signals = await _collect(ja.pull(_make_ctx(state), since=None))
    await ja.aclose()
    assert signals == []
    assert state.snapshot()[_JSON_API_CURSOR_KEY]["last_pulled_at"] == floor_iso
    assert state.snapshot()[_JSON_API_HEALTH_KEY]["state"] == "unhealthy"


@pytest.mark.asyncio
async def test_transient_503_retries_then_no_cursor_advance():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, request=req)

    state = InMemoryStateStore()
    signals = await _collect(_make_handler(handler).pull(_make_ctx(state), since=None))
    assert signals == []
    assert calls["n"] == 2  # one retry
    assert _JSON_API_CURSOR_KEY not in state.snapshot()
    assert state.snapshot()[_JSON_API_HEALTH_KEY]["state"] == "unhealthy"


@pytest.mark.asyncio
async def test_health_check_200_healthy_and_503_degraded():
    ok = _make_handler(lambda req: httpx.Response(200, text="[]", request=req))
    assert (await ok.health_check(_make_ctx())).state == "healthy"
    bad = _make_handler(lambda req: httpx.Response(503, request=req))
    assert (await bad.health_check(_make_ctx())).state == "degraded"


# ---------------------------------------------------------------------------
# Example descriptors — schema-validate both (registry-shape + handler config)
# ---------------------------------------------------------------------------


_DESCRIPTOR_FILES = ["source_reliefweb_api.yaml", "source_gdelt_doc_api.yaml"]


@pytest.mark.parametrize("fname", _DESCRIPTOR_FILES)
def test_example_descriptors_validate(fname):
    import pathlib

    import yaml

    from legba.data.schemas.source import SourceDescriptor
    from legba.runtime.source_factory import _unwrap_factory_dict

    path = pathlib.Path(__file__).resolve().parents[2] / "descriptors" / fname
    body = yaml.safe_load(path.read_text())
    desc = SourceDescriptor.model_validate(body, strict=False)
    assert desc.identity.kind == "json_api"
    assert desc.acquisition == "poll"
    # The config block parses against the real handler schema (the same path
    # build_source_handler takes at activation).
    cfg = JsonApiConfig(**_unwrap_factory_dict(desc.config))
    assert cfg.url_template.startswith("https://")
    # Keyless examples: no auth block, so they activate without a vault.
    assert cfg.auth is None


# ---------------------------------------------------------------------------
# Live probe — env-gated; the ONE test allowed to touch the real upstream.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("LEGBA_LIVE_PROBES") != "1",
    reason="live upstream probe; set LEGBA_LIVE_PROBES=1 to run",
)
@pytest.mark.asyncio
async def test_live_probe_gdelt_doc_api():
    """Pull the real GDELT DOC 2.0 API via the example descriptor's config.

    GDELT is the keyless probe target (ReliefWeb v2 requires an APPROVED
    appname — out-of-band registration, see the descriptor note). GDELT
    rate-limits to one request per ~5 s per IP, so the probe retries a 429
    a few times before judging.
    """
    import asyncio
    import pathlib

    import yaml

    from legba.runtime.source_factory import _unwrap_factory_dict

    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "descriptors" / "source_gdelt_doc_api.yaml"
    )
    body = yaml.safe_load(path.read_text())
    cfg = JsonApiConfig(**_unwrap_factory_dict(body["config"]))

    ja = JsonApiSourceHandler(cfg)
    state = InMemoryStateStore()
    ctx = SourceContext(
        target_id="target.live_probe",
        target_version="v-live",
        source_id="source.gdelt.doc_api",
        config=cfg,
        state_store=state,
        scope_languages=["en"],
    )
    signals: list[Signal] = []
    try:
        since = datetime.now(tz=timezone.utc) - timedelta(hours=2)
        for attempt in range(4):
            signals = await _collect(ja.pull(ctx, since=since))
            if signals:
                break
            health = state.snapshot().get(_JSON_API_HEALTH_KEY) or {}
            status = (health.get("detail") or {}).get("status")
            if status != 429:
                break
            await asyncio.sleep(10)  # GDELT per-IP rate limit
    finally:
        await ja.aclose()

    assert signals, (
        "GDELT DOC returned no articles for a 1h window "
        f"(health={state.snapshot().get(_JSON_API_HEALTH_KEY)!r})"
    )
    s = signals[0]
    assert s.payload["title"]
    assert s.canonical_url and s.canonical_url.startswith("http")
    # seendate arrives in GDELT's compact YYYYMMDDTHHMMSSZ form and must
    # parse into the canonical iso payload field.
    assert s.payload["published_at"]
