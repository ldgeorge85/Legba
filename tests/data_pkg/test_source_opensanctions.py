# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :class:`legba.data.sources.opensanctions.OpenSanctionsSourceHandler` (L-134).

Coverage:

  * Config schema validation (defaults, mode literal, self-hosted base_url
    requirement, blank dataset).
  * Conformance with the L-102 source-kind class-var contract.
  * ``bulk_csv`` mode:
      - happy-path streaming from a local fixture file.
      - schema_filter restricts emitted entities.
      - cursor advances to ``max(last_seen)`` across rows.
      - ``since`` hint filters rows.
      - HTTP HEAD healthcheck.
  * ``api`` mode (mocked httpx transport):
      - pagination via ``next_offset``.
      - ``since`` becomes ``modified_since`` query param.
      - schema_filter and dataset propagate.
      - Authorization header carries ``ApiKey <key>``.
      - GET /info healthcheck.
  * ``self_hosted`` mode: same wire format as ``api`` but on a
    caller-supplied base URL; ``base_url`` required at construction.
  * FollowTheMoney passthrough behavior (properties dict preserved /
    listified) — verifies the no-FTM-installed path.
  * Optional integration: against
    ``https://api.opensanctions.org`` when ``LEGBA_OPENSANCTIONS_API_KEY``
    is set in the environment (free tier exists).

httpx is mocked via :class:`httpx.MockTransport` so the handler hits a
deterministic transport while still exercising real ``httpx.AsyncClient``
machinery.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
import pytest
from pydantic import ValidationError

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
    SourceHealth,
)
from legba.data.sources.opensanctions import (
    OpenSanctionsConfig,
    OpenSanctionsSourceHandler,
    _BULK_BASE_DEFAULT,
    _BULK_RESUME_OFFSET_KEY,
    _BULK_TRAVERSED_KEY,
    _OS_CURSOR_KEY,
    _OS_HEALTH_KEY,
    _bulk_row_to_ftm_entity,
    _normalize_ftm,
    _parse_iso8601,
)


# Real src bug surfaced by the source-first pivot (commit fa3e598): the pivot
# re-cut the Signal model (src/legba/data/sources/_contract.py) to drop
# ``target_id`` and set ``extra='forbid'`` (observations are source-owned, not
# target-owned), but the source handlers were NOT updated — the OpenSanctions
# handler still calls ``Signal(..., target_id=ctx.target_id, ...)``
# (opensanctions.py:799). So the first Signal a ``pull()`` constructs raises
# ``ValidationError: target_id Extra inputs are not permitted``. This is a bug
# in src, not a stale-test/schema issue, so per the migration constraints it is
# FLAGGED in real_src_bugs_flagged and the pull-exercising tests are skipped
# (src is not edited to mask it). See PIVOT_BUILD_PLAN.
_SRC_BUG_TARGET_ID = (
    "src bug (pivot fa3e598, opensanctions.py:799): handler still passes "
    "target_id=ctx.target_id into the pivoted Signal model (extra='forbid', "
    "target_id dropped) so pull() raises ValidationError. Flagged in "
    "real_src_bugs_flagged; src not edited. See PIVOT_BUILD_PLAN."
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SAMPLE_BULK_CSV = (
    "id,schema,name,aliases,birth_date,countries,addresses,identifiers,"
    "sanctions,phones,emails,dataset,first_seen,last_seen,last_change,topics\n"
    "Q-IRAN-1,Person,Ali Khamenei,Ali Hosseini Khamenei;Khamene'i,1939-07-17,"
    "ir,Tehran,;,OFAC-SDN-1234,,,us_ofac_sdn;eu_fsf,2020-01-01T00:00:00,"
    "2025-05-01T10:00:00,2025-05-01T10:00:00,sanction;role.pep\n"
    "Q-ORG-1,Organization,Acme Trading Co,Acme,,ru,Moscow,REG-9999,"
    "OFAC-SDN-5555,,,us_ofac_sdn,2021-06-15T00:00:00,"
    "2025-04-10T12:00:00,2025-04-10T12:00:00,sanction\n"
    "Q-OLD-1,Person,Stale Entity,,1955-01-01,fr,,,SOME-ID,,,"
    "fr_ame,2010-01-01T00:00:00,2015-01-01T00:00:00,2015-01-01T00:00:00,\n"
)


SAMPLE_API_PAGE_1 = {
    "results": [
        {
            "id": "Q-API-1",
            "schema": "Person",
            "caption": "Vladimir Putin",
            "first_seen": "2020-01-01T00:00:00",
            "last_seen": "2025-05-15T08:00:00",
            "last_change": "2025-05-15T08:00:00",
            "datasets": ["us_ofac_sdn", "eu_fsf"],
            "countries": ["ru"],
            "topics": ["sanction", "role.pep"],
            "properties": {
                "name": ["Vladimir Putin"],
                "birthDate": ["1952-10-07"],
                "country": ["ru"],
            },
            "urls": ["https://www.opensanctions.org/entities/Q-API-1/"],
        },
        {
            "id": "Q-API-2",
            "schema": "Organization",
            "caption": "Test Org",
            "last_seen": "2025-05-10T00:00:00",
            "datasets": ["us_ofac_sdn"],
            "countries": ["ir"],
            "topics": ["sanction"],
            "properties": {
                "name": ["Test Org"],
                "country": ["ir"],
            },
        },
    ],
    "total": 3,
    "next_offset": 2,
}

SAMPLE_API_PAGE_2 = {
    "results": [
        {
            "id": "Q-API-3",
            "schema": "Person",
            "caption": "Filtered Out",
            "last_seen": "2025-05-20T00:00:00",
            "datasets": ["us_ofac_sdn"],
            "countries": ["us"],
            "topics": ["crime"],
            "properties": {"name": ["Filtered Out"]},
        },
    ],
    "total": 3,
    # No next_offset → stop.
}


def _make_ctx(
    state: InMemoryStateStore | None = None,
    *,
    config: OpenSanctionsConfig | None = None,
) -> SourceContext:
    return SourceContext(
        target_id="target.sanctions_watch",
        target_version="v-test",
        source_id="src.opensanctions_main",
        config=config or OpenSanctionsConfig(mode="bulk_csv", dataset="default"),
        state_store=state or InMemoryStateStore(),
        scope_geo=["IR", "RU"],
        scope_languages=["en"],
    )


def _make_handler_with_transport(
    transport_handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: OpenSanctionsConfig,
    api_key: str | None = None,
) -> OpenSanctionsSourceHandler:
    transport = httpx.MockTransport(transport_handler)
    client = httpx.AsyncClient(transport=transport, follow_redirects=True, timeout=5)
    return OpenSanctionsSourceHandler(config, http_client=client, api_key=api_key)


async def _collect(it):
    out: list[Signal] = []
    async for s in it:
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Config + class-var contract
# ---------------------------------------------------------------------------


def test_opensanctions_config_defaults():
    cfg = OpenSanctionsConfig()
    assert cfg.mode == "bulk_csv"
    assert cfg.dataset == "default"
    assert cfg.schema_filter is None
    assert cfg.api_page_size == 100
    assert cfg.timeout_seconds == 60
    assert cfg.user_agent.startswith("Legba/")
    assert cfg.max_bulk_rows == 0


def test_opensanctions_config_rejects_bad_mode():
    with pytest.raises(ValidationError):
        OpenSanctionsConfig(mode="ftp")  # type: ignore[arg-type]


def test_opensanctions_config_rejects_unknown_field():
    with pytest.raises(ValidationError):
        OpenSanctionsConfig(unknown="x")  # type: ignore[call-arg]


def test_opensanctions_config_rejects_blank_dataset():
    with pytest.raises(ValidationError):
        OpenSanctionsConfig(dataset="")


def test_self_hosted_requires_base_url():
    cfg = OpenSanctionsConfig(mode="self_hosted", base_url=None)
    with pytest.raises(ValueError):
        OpenSanctionsSourceHandler(cfg)


def test_handler_class_contract():
    assert OpenSanctionsSourceHandler.kind == "opensanctions"
    assert OpenSanctionsSourceHandler.family == "source"
    assert OpenSanctionsSourceHandler.schema_version == "legba/source.opensanctions/1-0-0"
    assert OpenSanctionsSourceHandler.config_schema is OpenSanctionsConfig
    assert OpenSanctionsSourceHandler.handler_version


# ---------------------------------------------------------------------------
# Bulk CSV mode — local fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def bulk_csv_path(tmp_path: Path) -> str:
    p = tmp_path / "targets.simple.csv"
    p.write_text(SAMPLE_BULK_CSV, encoding="utf-8")
    return str(p)


@pytest.mark.asyncio
async def test_pull_bulk_csv_yields_signals(bulk_csv_path):
    cfg = OpenSanctionsConfig(mode="bulk_csv", dataset="default")
    h = OpenSanctionsSourceHandler(cfg, bulk_local_path=bulk_csv_path)

    state = InMemoryStateStore()
    ctx = _make_ctx(state, config=cfg)

    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert len(signals) == 3
    s0 = signals[0]
    assert isinstance(s0, Signal)
    assert s0.source_id == "src.opensanctions_main"
    # Source-first pivot: Signal is target-agnostic (target_id dropped).
    assert s0.payload["external_id"] == "Q-IRAN-1"
    assert s0.payload["entity_type"] == "Person"
    assert s0.payload["countries"] == ["ir"]
    assert "sanction" in s0.payload["topics"]
    # Embedded FTM entity payload preserves properties as lists.
    ftm = s0.payload["entity_payload"]
    assert ftm["id"] == "Q-IRAN-1"
    assert ftm["schema"] == "Person"
    props = ftm["properties"]
    assert props["name"] == ["Ali Khamenei"]
    assert "country" in props
    # published_at populated from last_seen.
    assert s0.payload["published_at"].startswith("2025-05-01T10:00:00")
    # content_hash is a 64-char sha256.
    assert len(s0.content_hash) == 64
    # canonical_url defaults to opensanctions.org entity page.
    assert s0.canonical_url.endswith("Q-IRAN-1/")

    # Cursor advanced to max(last_seen) across all rows.
    cursor = state.snapshot()[_OS_CURSOR_KEY]
    assert cursor["last_seen"].startswith("2025-05-01T10:00:00")

    # Health record persisted healthy.
    health = state.snapshot()[_OS_HEALTH_KEY]
    assert health["state"] == "healthy"
    assert health["detail"]["mode"] == "bulk_csv"
    assert health["detail"]["entries_yielded"] == 3


@pytest.mark.asyncio
async def test_pull_bulk_csv_schema_filter_restricts(bulk_csv_path):
    cfg = OpenSanctionsConfig(
        mode="bulk_csv", dataset="default", schema_filter=["Organization"],
    )
    h = OpenSanctionsSourceHandler(cfg, bulk_local_path=bulk_csv_path)

    ctx = _make_ctx(config=cfg)
    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert len(signals) == 1
    assert signals[0].payload["external_id"] == "Q-ORG-1"
    assert signals[0].payload["entity_type"] == "Organization"


@pytest.mark.asyncio
async def test_pull_bulk_csv_since_filters(bulk_csv_path):
    cfg = OpenSanctionsConfig(mode="bulk_csv", dataset="default")
    h = OpenSanctionsSourceHandler(cfg, bulk_local_path=bulk_csv_path)

    # `since` after the stale Q-OLD-1 row (2015-01-01) but before the live rows.
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ctx = _make_ctx(config=cfg)
    signals = await _collect(h.pull(ctx, since=since))
    await h.aclose()

    ids = sorted(s.payload["external_id"] for s in signals)
    assert ids == ["Q-IRAN-1", "Q-ORG-1"]


@pytest.mark.asyncio
async def test_pull_bulk_csv_uses_persisted_cursor(bulk_csv_path):
    """If a cursor is already persisted, `pull(since=None)` still honors it."""
    cfg = OpenSanctionsConfig(mode="bulk_csv", dataset="default")
    h = OpenSanctionsSourceHandler(cfg, bulk_local_path=bulk_csv_path)

    state = InMemoryStateStore({
        _OS_CURSOR_KEY: {"last_seen": "2025-04-15T00:00:00+00:00"},
    })
    ctx = _make_ctx(state, config=cfg)
    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    # Q-ORG-1 (2025-04-10) is filtered out — its last_seen is older than cursor.
    ids = sorted(s.payload["external_id"] for s in signals)
    assert ids == ["Q-IRAN-1"]


@pytest.mark.asyncio
async def test_pull_bulk_csv_no_signals_yields_empty(tmp_path: Path):
    """Header-only file → no signals; cursor untouched, healthy reported."""
    p = tmp_path / "empty.csv"
    p.write_text(
        "id,schema,name,aliases,birth_date,countries,addresses,identifiers,"
        "sanctions,phones,emails,dataset,first_seen,last_seen,last_change,topics\n",
        encoding="utf-8",
    )
    cfg = OpenSanctionsConfig(mode="bulk_csv", dataset="default")
    h = OpenSanctionsSourceHandler(cfg, bulk_local_path=str(p))

    state = InMemoryStateStore()
    ctx = _make_ctx(state, config=cfg)
    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert signals == []
    assert _OS_CURSOR_KEY not in state.snapshot()
    health = state.snapshot()[_OS_HEALTH_KEY]
    assert health["state"] == "healthy"
    assert health["detail"]["entries_yielded"] == 0


@pytest.mark.asyncio
async def test_pull_bulk_csv_respects_max_bulk_rows(bulk_csv_path):
    cfg = OpenSanctionsConfig(mode="bulk_csv", dataset="default", max_bulk_rows=1)
    h = OpenSanctionsSourceHandler(cfg, bulk_local_path=bulk_csv_path)

    ctx = _make_ctx(config=cfg)
    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert len(signals) == 1


# ---------------------------------------------------------------------------
# Bulk CSV high-water-mark traversal (B — resume the dataset across pulls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_csv_skips_resume_offset(bulk_csv_path):
    """The actor publishes a high-water offset (emitted rows already consumed);
    the handler SKIPS that many emit-eligible rows so the pull resumes
    mid-snapshot instead of restarting from the top — the core bulk-traversal
    fix that lets the ~50k-row dataset be walked across daily pulls."""
    cfg = OpenSanctionsConfig(mode="bulk_csv", dataset="default")
    h = OpenSanctionsSourceHandler(cfg, bulk_local_path=bulk_csv_path)

    # Offset 1 -> skip the first emitted row (Q-IRAN-1); resume at the rest.
    state = InMemoryStateStore({_BULK_RESUME_OFFSET_KEY: 1})
    ctx = _make_ctx(state, config=cfg)
    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    ids = [s.payload["external_id"] for s in signals]
    assert ids == ["Q-ORG-1", "Q-OLD-1"]    # Q-IRAN-1 skipped (already consumed)


@pytest.mark.asyncio
async def test_bulk_csv_offset_past_tail_yields_nothing(bulk_csv_path):
    """An offset at/after the tail (the prior pull drained the snapshot) yields
    nothing and still reports reached_end so the actor resets to 0."""
    cfg = OpenSanctionsConfig(mode="bulk_csv", dataset="default")
    h = OpenSanctionsSourceHandler(cfg, bulk_local_path=bulk_csv_path)

    state = InMemoryStateStore({_BULK_RESUME_OFFSET_KEY: 3})   # 3 emit rows total
    ctx = _make_ctx(state, config=cfg)
    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert signals == []
    report = state.snapshot()[_BULK_TRAVERSED_KEY]
    assert report["reached_end"] is True
    assert report["rows"] == 0


@pytest.mark.asyncio
async def test_bulk_csv_reports_reached_end_on_full_walk(bulk_csv_path):
    """Draining the whole snapshot (no internal cap) reports reached_end=True
    so the actor resets the high-water mark and re-walks the next snapshot."""
    cfg = OpenSanctionsConfig(mode="bulk_csv", dataset="default")
    h = OpenSanctionsSourceHandler(cfg, bulk_local_path=bulk_csv_path)

    state = InMemoryStateStore()
    ctx = _make_ctx(state, config=cfg)
    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert len(signals) == 3
    report = state.snapshot()[_BULK_TRAVERSED_KEY]
    assert report["reached_end"] is True
    assert report["rows"] == 3


@pytest.mark.asyncio
async def test_bulk_csv_internal_cap_reports_not_reached_end(bulk_csv_path):
    """The handler's OWN ``max_bulk_rows`` cap is NOT end-of-stream — it reports
    reached_end=False so the actor keeps advancing the mark past these rows."""
    cfg = OpenSanctionsConfig(mode="bulk_csv", dataset="default", max_bulk_rows=2)
    h = OpenSanctionsSourceHandler(cfg, bulk_local_path=bulk_csv_path)

    state = InMemoryStateStore()
    ctx = _make_ctx(state, config=cfg)
    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert len(signals) == 2                  # capped by max_bulk_rows
    report = state.snapshot()[_BULK_TRAVERSED_KEY]
    assert report["reached_end"] is False     # NOT a full walk — more rows remain


@pytest.mark.asyncio
async def test_bulk_csv_resume_walks_dataset_across_pulls(bulk_csv_path):
    """End-to-end across pulls: simulate the actor's per-pull cap by using the
    handler's ``max_bulk_rows`` as a stand-in cap and threading the emitted
    count back as the next offset — the union of all pulls covers every row
    exactly once, proving no live entry is skipped and none re-walked within a
    snapshot."""
    seen: list[str] = []
    offset = 0
    state = InMemoryStateStore()
    for _ in range(5):  # generous bound; the dataset has 3 rows
        cfg = OpenSanctionsConfig(
            mode="bulk_csv", dataset="default", max_bulk_rows=1,
        )
        h = OpenSanctionsSourceHandler(cfg, bulk_local_path=bulk_csv_path)
        state._data[_BULK_RESUME_OFFSET_KEY] = offset
        ctx = _make_ctx(state, config=cfg)
        batch = await _collect(h.pull(ctx, since=None))
        await h.aclose()
        seen.extend(s.payload["external_id"] for s in batch)
        report = state.snapshot()[_BULK_TRAVERSED_KEY]
        if report["reached_end"]:
            break
        offset += len(batch)   # actor advances the mark by the consumed count

    assert seen == ["Q-IRAN-1", "Q-ORG-1", "Q-OLD-1"]   # every row once, in order


@pytest.mark.asyncio
async def test_health_check_bulk_csv_head_request():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        assert req.method == "HEAD"
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/csv",
                "Content-Length": "12345678",
                "Last-Modified": "Sun, 19 May 2025 00:00:00 GMT",
            },
            request=req,
        )

    cfg = OpenSanctionsConfig(mode="bulk_csv", dataset="all")
    h = _make_handler_with_transport(handler, config=cfg)
    ctx = _make_ctx(config=cfg)

    result = await h.health_check(ctx)
    await h.aclose()

    assert isinstance(result, SourceHealth)
    assert result.state == "healthy"
    assert result.detail["mode"] == "bulk_csv"
    assert captured[0].method == "HEAD"
    expected_url = (
        _BULK_BASE_DEFAULT + "/datasets/latest/all/targets.simple.csv"
    )
    assert str(captured[0].url) == expected_url


# ---------------------------------------------------------------------------
# API mode — mocked httpx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_api_paginates_and_emits():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        params = dict(req.url.params)
        offset = int(params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json=SAMPLE_API_PAGE_1, request=req)
        if offset == 2:
            return httpx.Response(200, json=SAMPLE_API_PAGE_2, request=req)
        return httpx.Response(200, json={"results": []}, request=req)

    cfg = OpenSanctionsConfig(mode="api", dataset="all", api_page_size=2)
    h = _make_handler_with_transport(handler, config=cfg, api_key="test-key")
    ctx = _make_ctx(config=cfg)

    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert len(signals) == 3
    ids = [s.payload["external_id"] for s in signals]
    assert ids == ["Q-API-1", "Q-API-2", "Q-API-3"]

    # Bearer-ish header carried `ApiKey <key>` per OpenSanctions docs.
    assert captured[0].headers.get("authorization") == "ApiKey test-key"

    # Page 1 query had offset=0; page 2 used the server-supplied next_offset=2.
    assert captured[0].url.params.get("offset") == "0"
    assert captured[1].url.params.get("offset") == "2"
    # Two paginated requests total (page 2 has no next_offset → stop).
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_pull_api_emits_modified_since_param():
    seen_params: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        for k, v in req.url.params.items():
            seen_params.setdefault(k, v)
        return httpx.Response(200, json={"results": []}, request=req)

    cfg = OpenSanctionsConfig(mode="api", dataset="all")
    h = _make_handler_with_transport(handler, config=cfg, api_key="k")
    ctx = _make_ctx(config=cfg)
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    await _collect(h.pull(ctx, since=since))
    await h.aclose()

    assert seen_params.get("modified_since", "").startswith("2025-01-01T00:00:00")
    assert seen_params.get("dataset") == "all"


@pytest.mark.asyncio
async def test_pull_api_schema_filter_propagates_as_repeated_param():
    seen_keys: list[str] = []
    seen_schemas: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        for k, v in req.url.params.multi_items():
            seen_keys.append(k)
            if k == "schema":
                seen_schemas.append(v)
        return httpx.Response(200, json={"results": []}, request=req)

    cfg = OpenSanctionsConfig(
        mode="api", dataset="all", schema_filter=["Person", "Organization"],
    )
    h = _make_handler_with_transport(handler, config=cfg, api_key="k")
    ctx = _make_ctx(config=cfg)
    await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert sorted(seen_schemas) == ["Organization", "Person"]


@pytest.mark.asyncio
async def test_pull_api_4xx_marks_unhealthy_and_stops():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"}, request=req)

    cfg = OpenSanctionsConfig(mode="api", dataset="all")
    h = _make_handler_with_transport(handler, config=cfg, api_key="bad")
    state = InMemoryStateStore()
    ctx = _make_ctx(state, config=cfg)
    signals = await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert signals == []
    health = state.snapshot()[_OS_HEALTH_KEY]
    assert health["state"] == "unhealthy"
    assert "401" in (health["last_error"] or "")


@pytest.mark.asyncio
async def test_health_check_api_info_probe():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/info")
        return httpx.Response(200, json={"version": "9.9.9"}, request=req)

    cfg = OpenSanctionsConfig(mode="api", dataset="default")
    h = _make_handler_with_transport(handler, config=cfg, api_key="k")
    ctx = _make_ctx(config=cfg)
    result = await h.health_check(ctx)
    await h.aclose()
    assert result.state == "healthy"
    assert result.detail["mode"] == "api"


# ---------------------------------------------------------------------------
# self_hosted mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_self_hosted_uses_base_url():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"results": []}, request=req)

    cfg = OpenSanctionsConfig(
        mode="self_hosted",
        dataset="all",
        base_url="http://opensanctions-internal:8000",
    )
    h = _make_handler_with_transport(handler, config=cfg)
    ctx = _make_ctx(config=cfg)
    await _collect(h.pull(ctx, since=None))
    await h.aclose()

    assert captured[0].url.host == "opensanctions-internal"
    assert captured[0].url.port == 8000
    assert "/entities/" in str(captured[0].url)


@pytest.mark.asyncio
async def test_health_check_self_hosted():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "1.0"}, request=req)

    cfg = OpenSanctionsConfig(
        mode="self_hosted",
        dataset="all",
        base_url="http://opensanctions-internal:8000",
    )
    h = _make_handler_with_transport(handler, config=cfg)
    ctx = _make_ctx(config=cfg)
    result = await h.health_check(ctx)
    await h.aclose()
    assert result.state == "healthy"


# ---------------------------------------------------------------------------
# FollowTheMoney passthrough behavior
# ---------------------------------------------------------------------------


def test_normalize_ftm_passthrough_listifies_properties():
    entity = {
        "id": "X-1",
        "schema": "Person",
        "properties": {
            "name": ["Alice"],
            "birthDate": "1970-01-01",   # scalar — should be listified
            "country": None,             # None → []
        },
    }
    normalized, props = _normalize_ftm(entity)
    assert isinstance(props["name"], list)
    assert props["birthDate"] == ["1970-01-01"]
    assert props["country"] == []
    # The normalized dict still carries id / schema.
    assert normalized["id"] == "X-1"


def test_bulk_row_to_ftm_entity_parses_multivalue_cells():
    row = {
        "id": "Q-1", "schema": "Person", "name": "Bob",
        "aliases": "Robert;Bobby",
        "birth_date": "1980-01-01",
        "countries": "us;uk",
        "addresses": "1 Main St;2 Side Rd",
        "identifiers": "ABC123",
        "sanctions": "OFAC-1",
        "phones": "",
        "emails": "",
        "dataset": "us_ofac_sdn;eu_fsf",
        "first_seen": "2020-01-01T00:00:00",
        "last_seen": "2025-01-01T00:00:00",
        "last_change": "2025-01-01T00:00:00",
        "topics": "sanction;role.pep",
    }
    entity = _bulk_row_to_ftm_entity(row)
    assert entity is not None
    assert entity["id"] == "Q-1"
    assert entity["schema"] == "Person"
    assert entity["countries"] == ["us", "uk"]
    assert entity["topics"] == ["sanction", "role.pep"]
    assert entity["properties"]["alias"] == ["Robert", "Bobby"]
    assert entity["datasets"] == ["us_ofac_sdn", "eu_fsf"]


def test_bulk_row_to_ftm_entity_rejects_missing_id_or_schema():
    assert _bulk_row_to_ftm_entity({"id": "", "schema": "Person", "name": "x"}) is None
    assert _bulk_row_to_ftm_entity({"id": "Q-1", "schema": "", "name": "x"}) is None


def test_parse_iso8601_handles_z_and_date_only():
    a = _parse_iso8601("2025-05-15T10:00:00Z")
    assert a is not None and a.tzinfo is not None
    b = _parse_iso8601("2025-05-15")
    assert b is not None and b.tzinfo is not None
    assert _parse_iso8601("garbage") is None
    assert _parse_iso8601("") is None


# ---------------------------------------------------------------------------
# Live integration test (gated on env)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("LEGBA_OPENSANCTIONS_API_KEY"),
    reason="LEGBA_OPENSANCTIONS_API_KEY not set",
)
async def test_live_api_pull_returns_at_least_one_signal():
    api_key = os.environ["LEGBA_OPENSANCTIONS_API_KEY"]
    cfg = OpenSanctionsConfig(
        mode="api",
        dataset="sanctions",
        api_page_size=5,
        schema_filter=["Person", "Organization"],
    )
    h = OpenSanctionsSourceHandler(cfg, api_key=api_key)
    ctx = _make_ctx(config=cfg)
    try:
        # Pull only a tiny window to keep the free-tier credit usage low.
        signals = []
        async for s in h.pull(ctx, since=None):
            signals.append(s)
            if len(signals) >= 1:
                break
        assert len(signals) >= 1
        # Sanity: FTM schema name set + entity_payload preserved.
        s0 = signals[0]
        assert s0.payload["entity_type"] in {
            "Person", "Organization", "Company", "LegalEntity", "Vessel",
        }
        assert isinstance(s0.payload["entity_payload"], dict)
    finally:
        await h.aclose()
