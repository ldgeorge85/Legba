# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the GDELT 15-minute file-dump source handler.

Replacement acquisition path for GDELT after ``source.gdelt.doc_api`` (the
keyless DOC 2.0 API, ``json_api`` kind) started 429-ing at the IP level
(verified 2026-07-21). This handler polls
``http://data.gdeltproject.org/gdeltv2/lastupdate.txt`` and fetches the
events-export CSV zip directly — no API, no rate limit.

Tests mock ``httpx`` via ``httpx.MockTransport`` for the network paths (index
+ file fetch) and use a real on-disk zip fixture
(``fixtures/gdelt_events_sample.export.CSV.zip``, 5 synthetic rows built to
exercise each filter gate individually) for the parse path, mirroring the
UCDP test's fixture-file + MockTransport combination.

Test surface:

  * protocol satisfaction + config defaults / validation.
  * ``lastupdate.txt`` parsing — three-line index, timestamp extraction,
    malformed-line tolerance.
  * events-CSV parsing — zip/unzip, 61-column tab-separated, short-row skip.
  * row filter — each gate (event_root_codes / min_num_mentions /
    goldstein_max / actor_country_fips) in isolation, AND-composition.
  * row -> Signal mapping — title synthesis, geo, content hash, payload shape.
  * pull happy-path against the mocked index + fixture zip -> only the rows
    that pass every gate become Signals.
  * cursor advance — persists the export-file timestamp; a second pull with
    an unchanged lastupdate.txt entry yields nothing (no re-processing).
  * bounding — ``max_signals_per_pull`` truncates.
  * descriptor round-trips through the real SourceDescriptor schema + the
    production unwrap -> GDELTFilesConfig; the runtime factory discovers the
    ``gdelt_files`` kind.
  * health_check: healthy / degraded / unhealthy paths.
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
from legba.data.sources.gdelt_files import (
    DEFAULT_EVENT_ROOT_CODES,
    DEFAULT_GOLDSTEIN_MAX,
    DEFAULT_MIN_NUM_MENTIONS,
    EVENTS_EXPORT_COLUMNS,
    GDELTFilesConfig,
    GDELTFilesSourceHandler,
    _correct_sqldate_year_offset,
    extract_export_timestamp,
    parse_events_csv,
    parse_lastupdate,
    row_passes_filter,
    row_to_signal,
    synthesize_title,
)
from legba.runtime.source_factory import (
    _unwrap_factory_dict,
    build_source_handler,
    discover_source_kinds,
)


FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "gdelt_events_sample.export.CSV.zip"
)
DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parents[2] / "descriptors"

_EXPORT_URL = "http://data.gdeltproject.org/gdeltv2/20260721143000.export.CSV.zip"
_MENTIONS_URL = "http://data.gdeltproject.org/gdeltv2/20260721143000.mentions.CSV.zip"
_GKG_URL = "http://data.gdeltproject.org/gdeltv2/20260721143000.gkg.csv.zip"

_LASTUPDATE_BODY = (
    f"12345 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa {_EXPORT_URL}\n"
    f"67890 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb {_MENTIONS_URL}\n"
    f"11111 cccccccccccccccccccccccccccccccc {_GKG_URL}\n"
)

# Wall-clock anchor for pull() tests: shortly AFTER the fixture's embedded
# export timestamp (2026-07-21T14:30:00Z) so the file reads as "new" under
# the handler's default 15-minute lookback, independent of the real
# wall-clock date the suite happens to run on.
_PULL_NOW = datetime(2026, 7, 21, 14, 35, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _make_row(**overrides: str) -> dict[str, str]:
    """A base 61-column row (root 19 fight, mentions 25, goldstein -6.0 —
    passes every default gate) with any field overridden for a test case."""
    base = {c: "" for c in EVENTS_EXPORT_COLUMNS}
    base.update(
        {
            "GLOBALEVENTID": "1000001",
            "SQLDATE": "20260721",
            "MonthYear": "202607",
            "Year": "2026",
            "Actor1Code": "USAGOV",
            "Actor1Name": "UNITED STATES",
            "Actor1CountryCode": "US",
            "Actor1Type1Code": "GOV",
            "Actor2Code": "IRNMIL",
            "Actor2Name": "IRAN",
            "Actor2CountryCode": "IR",
            "Actor2Type1Code": "MIL",
            "IsRootEvent": "1",
            "EventCode": "190",
            "EventBaseCode": "190",
            "EventRootCode": "19",
            "QuadClass": "4",
            "GoldsteinScale": "-6.0",
            "NumMentions": "25",
            "NumSources": "5",
            "NumArticles": "10",
            "AvgTone": "-4.2",
            "ActionGeo_Type": "1",
            "ActionGeo_FullName": "Strait of Hormuz",
            "ActionGeo_CountryCode": "IR",
            "ActionGeo_Lat": "26.5",
            "ActionGeo_Long": "56.25",
            "DATEADDED": "20260721143000",
            "SOURCEURL": "https://example.com/article1",
        }
    )
    base.update(overrides)
    return base


def _make_ctx(
    *,
    config: GDELTFilesConfig | None = None,
    store: InMemoryStateStore | None = None,
    now: datetime | None = None,
) -> SourceContext:
    return SourceContext(
        target_id="target.test.world",
        target_version="v0",
        source_id="source.gdelt.files",
        config=config or GDELTFilesConfig(),
        state_store=store or InMemoryStateStore(),
        now_fn=(lambda: now) if now is not None else None,
        logger=logging.getLogger("test.gdelt_files"),
    )


class _MockResponses:
    """Serves GETs in URL-keyed sequence, recording every request."""

    def __init__(self, by_url: dict[str, Callable[[httpx.Request], httpx.Response]]):
        self._by_url = by_url
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        handler = self._by_url.get(url)
        if handler is None:
            return httpx.Response(404, text=f"no mock for {url}")
        return handler(request)


def _text_response(body: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, text=body)


def _bytes_response(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, content=body)


def _patch_client(handler: GDELTFilesSourceHandler, mock: _MockResponses) -> None:
    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(mock))

    handler._http_client_factory = _factory


async def _collect(
    handler: GDELTFilesSourceHandler, ctx: SourceContext, **kw: Any
) -> list[Signal]:
    out: list[Signal] = []
    async for sig in handler.pull(ctx, **kw):
        out.append(sig)
    return out


# ---------------------------------------------------------------------------
# Protocol + config
# ---------------------------------------------------------------------------


def test_gdelt_files_satisfies_source_handler_protocol() -> None:
    handler = GDELTFilesSourceHandler()
    assert isinstance(handler, SourceHandler)
    assert handler.kind == "gdelt_files"
    assert handler.family == "source"
    assert handler.schema_version.startswith("legba/source.gdelt_files/")
    assert handler.config_schema is GDELTFilesConfig


def test_config_defaults() -> None:
    cfg = GDELTFilesConfig()
    assert cfg.event_root_codes == list(DEFAULT_EVENT_ROOT_CODES)
    assert cfg.min_num_mentions == DEFAULT_MIN_NUM_MENTIONS
    assert cfg.goldstein_max == DEFAULT_GOLDSTEIN_MAX
    assert cfg.actor_country_fips is None
    assert cfg.max_files_per_pull == 2
    assert cfg.max_signals_per_pull == 2000
    assert cfg.lookback_minutes == 15


def test_config_rejects_bad_root_code() -> None:
    with pytest.raises(ValidationError):
        GDELTFilesConfig(event_root_codes=["abc"])


def test_config_rejects_bad_fips_code() -> None:
    with pytest.raises(ValidationError):
        GDELTFilesConfig(actor_country_fips=["USA"])  # 3 letters, not FIPS-2


def test_config_pads_single_digit_root_code() -> None:
    cfg = GDELTFilesConfig(event_root_codes=["1", "20"])
    assert cfg.event_root_codes == ["01", "20"]


def test_config_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        GDELTFilesConfig(not_a_real_field=1)


def test_config_accepts_empty_root_codes_as_no_filter() -> None:
    cfg = GDELTFilesConfig(event_root_codes=[])
    assert cfg.event_root_codes == []


# ---------------------------------------------------------------------------
# lastupdate.txt parsing
# ---------------------------------------------------------------------------


def test_parse_lastupdate_three_lines() -> None:
    index = parse_lastupdate(_LASTUPDATE_BODY)
    assert set(index.keys()) == {"export", "mentions", "gkg"}
    assert index["export"]["url"] == _EXPORT_URL
    assert index["export"]["size"] == 12345
    assert index["export"]["timestamp"] == datetime(
        2026, 7, 21, 14, 30, 0, tzinfo=timezone.utc
    )


def test_parse_lastupdate_tolerates_malformed_line() -> None:
    body = _LASTUPDATE_BODY + "garbage line with no url\n"
    index = parse_lastupdate(body)
    assert set(index.keys()) == {"export", "mentions", "gkg"}


def test_parse_lastupdate_skips_blank_lines() -> None:
    body = "\n\n" + _LASTUPDATE_BODY + "\n\n"
    index = parse_lastupdate(body)
    assert len(index) == 3


def test_parse_lastupdate_empty_body_yields_no_entries() -> None:
    assert parse_lastupdate("") == {}


def test_extract_export_timestamp() -> None:
    ts = extract_export_timestamp(_EXPORT_URL)
    assert ts == datetime(2026, 7, 21, 14, 30, 0, tzinfo=timezone.utc)


def test_extract_export_timestamp_none_when_no_digits() -> None:
    assert extract_export_timestamp("http://example.com/not-a-gdelt-file.zip") is None


# ---------------------------------------------------------------------------
# Events-CSV parsing
# ---------------------------------------------------------------------------


def test_parse_events_csv_fixture_yields_five_rows() -> None:
    rows = parse_events_csv(_load_fixture_bytes(), max_rows=1000)
    assert len(rows) == 5
    assert rows[0]["GLOBALEVENTID"] == "1000001"
    assert rows[0]["EventRootCode"] == "19"
    assert set(rows[0].keys()) == set(EVENTS_EXPORT_COLUMNS)


def test_parse_events_csv_respects_max_rows() -> None:
    rows = parse_events_csv(_load_fixture_bytes(), max_rows=2)
    assert len(rows) == 2


def test_parse_events_csv_raises_on_empty_zip() -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    with pytest.raises(ValueError):
        parse_events_csv(buf.getvalue(), max_rows=100)


def test_parse_events_csv_raises_on_not_a_zip() -> None:
    import zipfile

    with pytest.raises(zipfile.BadZipFile):
        parse_events_csv(b"not a zip file at all", max_rows=100)


# ---------------------------------------------------------------------------
# Row filter
# ---------------------------------------------------------------------------


def test_filter_passes_default_row() -> None:
    cfg = GDELTFilesConfig()
    assert row_passes_filter(_make_row(), cfg) is True


def test_filter_drops_wrong_root_code() -> None:
    cfg = GDELTFilesConfig()
    row = _make_row(EventRootCode="01")  # "make statement" — not in 14-20
    assert row_passes_filter(row, cfg) is False


def test_filter_drops_low_mentions() -> None:
    cfg = GDELTFilesConfig()
    row = _make_row(NumMentions="2")  # below default floor of 10
    assert row_passes_filter(row, cfg) is False


def test_filter_drops_above_goldstein_ceiling() -> None:
    cfg = GDELTFilesConfig()
    row = _make_row(GoldsteinScale="1.0")  # positive, ceiling is -2.0
    assert row_passes_filter(row, cfg) is False


def test_filter_root_code_disabled_when_empty_list() -> None:
    cfg = GDELTFilesConfig(event_root_codes=[])
    row = _make_row(EventRootCode="01")
    assert row_passes_filter(row, cfg) is True


def test_filter_mentions_disabled_when_zero() -> None:
    cfg = GDELTFilesConfig(min_num_mentions=0)
    row = _make_row(NumMentions="0")
    assert row_passes_filter(row, cfg) is True


def test_filter_goldstein_disabled_when_none() -> None:
    cfg = GDELTFilesConfig(goldstein_max=None)
    row = _make_row(GoldsteinScale="10.0")
    assert row_passes_filter(row, cfg) is True


def test_filter_fips_allowlist_matches_any_of_three_fields() -> None:
    cfg = GDELTFilesConfig(actor_country_fips=["IR"])
    # Actor2CountryCode=IR in the base row — should match.
    assert row_passes_filter(_make_row(), cfg) is True


def test_filter_fips_allowlist_rejects_when_none_match() -> None:
    cfg = GDELTFilesConfig(actor_country_fips=["CH"])  # Switzerland FIPS
    assert row_passes_filter(_make_row(), cfg) is False


def test_filter_malformed_numeric_fields_fail_closed() -> None:
    cfg = GDELTFilesConfig()
    row = _make_row(NumMentions="not-a-number")
    assert row_passes_filter(row, cfg) is False


# ---------------------------------------------------------------------------
# Row -> Signal mapping
# ---------------------------------------------------------------------------


def test_synthesize_title_uses_actors_and_location() -> None:
    title = synthesize_title(_make_row())
    assert "UNITED STATES" in title
    assert "IRAN" in title
    assert "fight" in title
    assert "Strait of Hormuz" in title


def test_synthesize_title_degrades_gracefully_on_blank_actors() -> None:
    row = _make_row(Actor1Name="", Actor2Name="", ActionGeo_FullName="")
    title = synthesize_title(row)
    assert title  # never empty
    assert "Unidentified actors" in title


def test_row_to_signal_shape() -> None:
    ctx = _make_ctx()
    sig = row_to_signal(_make_row(), ctx=ctx, export_url=_EXPORT_URL)
    assert isinstance(sig, Signal)
    assert sig.source_id == "source.gdelt.files"
    assert sig.canonical_url == "https://example.com/article1"
    assert sig.payload["event_root_code"] == "19"
    assert sig.payload["goldstein_scale"] == -6.0
    assert sig.payload["num_mentions"] == 25
    assert sig.payload["export_file_url"] == _EXPORT_URL
    # B-6: the payload keeps the RAW FIPS value under its ``_fips`` key; the
    # indexed, desk-routing ``geo`` column carries the TRANSLATED ISO code. Iran
    # is a case where FIPS and ISO happen to agree ('IR' → 'IR'), which is
    # precisely why this assertion passed for months while the codes that do NOT
    # agree were misrouting — see test_fips_iso_crosswalk.py for the pairs that
    # do the damage.
    assert sig.payload["geo"]["country_code_fips"] == "IR"
    assert sig.geo == ["IR"]
    assert sig.content_hash  # non-empty
    assert sig.raw_provenance["kind"] == "gdelt_files"
    assert sig.raw_provenance["global_event_id"] == "1000001"


def test_row_to_signal_content_hash_stable_for_same_identity() -> None:
    ctx = _make_ctx()
    sig1 = row_to_signal(_make_row(), ctx=ctx, export_url=_EXPORT_URL)
    sig2 = row_to_signal(_make_row(), ctx=ctx, export_url=_EXPORT_URL)
    assert sig1.content_hash == sig2.content_hash


def test_row_to_signal_content_hash_differs_for_different_event_id() -> None:
    ctx = _make_ctx()
    sig1 = row_to_signal(_make_row(GLOBALEVENTID="1"), ctx=ctx, export_url=_EXPORT_URL)
    sig2 = row_to_signal(_make_row(GLOBALEVENTID="2"), ctx=ctx, export_url=_EXPORT_URL)
    assert sig1.content_hash != sig2.content_hash


# ---------------------------------------------------------------------------
# 2026-08-29 DQ sweep §3 — the SQLDATE year-off-by-one correction
#
# GDELT's own upstream events export sometimes carries SQLDATE with the year
# wrong by exactly one, month-day intact, while DATEADDED (a separate field
# on the SAME row) carries the correct year — confirmed upstream (not our
# parse) by inspecting the untouched raw payload on live data; this handler
# applies zero transformation to SQLDATE before this fix. The guard is
# deliberately narrow: only the exact deterministic signature (matching
# month-day, year off by exactly one, corrected date not in the future)
# corrects; everything else — including genuinely old events and the
# separate ~30-day month-lag band the same sweep found — passes through
# unchanged.
# ---------------------------------------------------------------------------


def test_sqldate_year_offset_corrects_the_deterministic_signature() -> None:
    # The exact live shape: SQLDATE one year behind DATEADDED, same month-day.
    assert _correct_sqldate_year_offset("20250829", "20260829180000") == (
        "20260829",
        True,
    )


def test_sqldate_year_offset_leaves_matching_year_untouched() -> None:
    # The overwhelmingly common case — SQLDATE and DATEADDED already agree.
    assert _correct_sqldate_year_offset("20260721", "20260721143000") == (
        "20260721",
        False,
    )


def test_sqldate_year_offset_leaves_month_lag_band_untouched() -> None:
    # The OTHER (different-shaped) staleness class the sweep found: month
    # decremented, not year — must NOT be swept up by this guard.
    assert _correct_sqldate_year_offset("20260728", "20260827180000") == (
        "20260728",
        False,
    )


def test_sqldate_year_offset_leaves_genuinely_old_events_untouched() -> None:
    # The 14/161 rows the sweep found genuinely years-old (no month-day
    # match at all) — the guard must not touch real historical events.
    assert _correct_sqldate_year_offset("20160101", "20260829180000") == (
        "20160101",
        False,
    )


def test_sqldate_year_offset_leaves_multi_year_gap_untouched() -> None:
    # Signature requires the gap be EXACTLY one year, not "any gap".
    assert _correct_sqldate_year_offset("20240829", "20260829180000") == (
        "20240829",
        False,
    )


def test_sqldate_year_offset_requires_dateadded_evidence() -> None:
    # No DATEADDED on the row → nothing to anchor the correction to; fail
    # safe rather than guess.
    assert _correct_sqldate_year_offset("20250829", None) == ("20250829", False)
    assert _correct_sqldate_year_offset("20250829", "") == ("20250829", False)
    assert _correct_sqldate_year_offset("20250829", "notadate") == (
        "20250829",
        False,
    )


def test_sqldate_year_offset_handles_missing_or_malformed_sqldate() -> None:
    assert _correct_sqldate_year_offset(None, "20260829180000") == (None, False)
    assert _correct_sqldate_year_offset("", "20260829180000") == ("", False)
    assert _correct_sqldate_year_offset("notadate", "20260829180000") == (
        "notadate",
        False,
    )


def test_sqldate_year_offset_never_raises_on_leap_day_mismatch() -> None:
    # Feb 29 in a leap year, DATEADDED's year a non-leap year — a naive
    # ``.replace(year=...)`` would raise; must fail safe instead.
    assert _correct_sqldate_year_offset("20280229", "20290228180000") == (
        "20280229",
        False,
    )


def test_sqldate_year_offset_rejects_a_future_corrected_date() -> None:
    # Guard 4: the corrected date must not land in the future relative to
    # wall-clock now (well beyond the future-skew tolerance).
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    assert _correct_sqldate_year_offset(
        "20250901", "20260901000000", now=now
    ) == ("20250901", False)


def test_sqldate_year_offset_allows_a_near_future_corrected_date() -> None:
    # Inside the future-skew tolerance (mirrors rss.py's 26h clamp) — a
    # correction landing a few hours "ahead" of the exact test anchor
    # (e.g. a different timezone slice of the same UTC day) is still
    # accepted rather than needlessly rejected.
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    assert _correct_sqldate_year_offset(
        "20250830", "20260830000000", now=now
    ) == ("20260830", True)


def test_row_to_signal_corrects_deterministic_year_offset() -> None:
    ctx = _make_ctx()
    row = _make_row(SQLDATE="20250721", DATEADDED="20260721143000")
    sig = row_to_signal(row, ctx=ctx, export_url=_EXPORT_URL)
    # The field the render layer and every staleness read actually consume.
    assert sig.payload["published_at"] == "20260721"
    # The untouched original stays available for audit in TWO places.
    assert sig.raw_provenance["sql_date"] == "20250721"
    assert sig.payload["raw_body"]["SQLDATE"] == "20250721"
    # The provenance marker records that the correction fired.
    assert sig.raw_provenance["sqldate_year_corrected"] is True
    assert sig.raw_provenance["published_at"] == "2026-07-21T00:00:00+00:00"


def test_row_to_signal_leaves_normal_sqldate_untouched() -> None:
    ctx = _make_ctx()
    sig = row_to_signal(_make_row(), ctx=ctx, export_url=_EXPORT_URL)
    assert sig.payload["published_at"] == "20260721"
    assert sig.raw_provenance["sqldate_year_corrected"] is False


def test_row_to_signal_leaves_genuinely_old_event_untouched() -> None:
    ctx = _make_ctx()
    row = _make_row(SQLDATE="20160101", DATEADDED="20260721143000")
    sig = row_to_signal(row, ctx=ctx, export_url=_EXPORT_URL)
    assert sig.payload["published_at"] == "20160101"
    assert sig.raw_provenance["sqldate_year_corrected"] is False


# ---------------------------------------------------------------------------
# pull()
# ---------------------------------------------------------------------------


async def test_pull_happy_path_only_passing_rows_emitted() -> None:
    handler = GDELTFilesSourceHandler()
    mock = _MockResponses(
        {
            handler.config.lastupdate_url: _text_response(_LASTUPDATE_BODY),
            _EXPORT_URL: _bytes_response(_load_fixture_bytes()),
        }
    )
    _patch_client(handler, mock)
    ctx = _make_ctx(now=_PULL_NOW)

    signals = await _collect(handler, ctx)

    # Of the 5 fixture rows: #1000001 (passes), #1000002 (bad root code),
    # #1000003 (low mentions), #1000004 (bad goldstein), #1000005 (passes,
    # all-US FIPS) — exactly 2 should survive the default filter.
    ids = {sig.payload["external_id"] for sig in signals}
    assert ids == {"1000001", "1000005"}


async def test_pull_advances_cursor_to_export_timestamp() -> None:
    handler = GDELTFilesSourceHandler()
    mock = _MockResponses(
        {
            handler.config.lastupdate_url: _text_response(_LASTUPDATE_BODY),
            _EXPORT_URL: _bytes_response(_load_fixture_bytes()),
        }
    )
    _patch_client(handler, mock)
    store = InMemoryStateStore()
    ctx = _make_ctx(store=store, now=_PULL_NOW)

    await _collect(handler, ctx)

    cursor = await store.get("gdelt_files_last_export_ts")
    assert cursor == "2026-07-21T14:30:00+00:00"


async def test_pull_second_call_with_unchanged_index_yields_nothing() -> None:
    """Once the cursor is at the export file's timestamp, re-polling the SAME
    lastupdate.txt entry (nothing new upstream) must not re-fetch/re-emit."""
    handler = GDELTFilesSourceHandler()
    call_log: list[str] = []

    def _index_handler(request: httpx.Request) -> httpx.Response:
        call_log.append(str(request.url))
        return httpx.Response(200, text=_LASTUPDATE_BODY)

    def _file_handler(request: httpx.Request) -> httpx.Response:
        call_log.append(str(request.url))
        return httpx.Response(200, content=_load_fixture_bytes())

    mock = _MockResponses(
        {
            handler.config.lastupdate_url: _index_handler,
            _EXPORT_URL: _file_handler,
        }
    )
    _patch_client(handler, mock)
    store = InMemoryStateStore()
    ctx = _make_ctx(store=store, now=_PULL_NOW)

    first = await _collect(handler, ctx)
    assert len(first) == 2

    second_urls_before = list(call_log)
    second = await _collect(handler, ctx)
    assert second == []
    # Only the index was hit the second time — the export file was NOT
    # re-fetched because its timestamp is not newer than the cursor.
    assert call_log[len(second_urls_before):] == [handler.config.lastupdate_url]


async def test_pull_no_events_line_in_index_degrades() -> None:
    handler = GDELTFilesSourceHandler()
    mock = _MockResponses(
        {
            handler.config.lastupdate_url: _text_response(
                f"12345 aaaa {_MENTIONS_URL}\n11111 bbbb {_GKG_URL}\n"
            ),
        }
    )
    _patch_client(handler, mock)
    store = InMemoryStateStore()
    ctx = _make_ctx(store=store)

    signals = await _collect(handler, ctx)
    assert signals == []

    health = await store.get("gdelt_files_health")
    assert health["state"] == "degraded"


async def test_pull_http_error_on_index_reports_unhealthy() -> None:
    handler = GDELTFilesSourceHandler()
    mock = _MockResponses(
        {
            handler.config.lastupdate_url: lambda r: httpx.Response(503, text="down"),
        }
    )
    _patch_client(handler, mock)
    store = InMemoryStateStore()
    ctx = _make_ctx(store=store)

    signals = await _collect(handler, ctx)
    assert signals == []

    health = await store.get("gdelt_files_health")
    assert health["state"] == "unhealthy"


async def test_pull_respects_max_signals_per_pull() -> None:
    cfg = GDELTFilesConfig(max_signals_per_pull=1, min_num_mentions=0, goldstein_max=None)
    handler = GDELTFilesSourceHandler(config=cfg)
    mock = _MockResponses(
        {
            cfg.lastupdate_url: _text_response(_LASTUPDATE_BODY),
            _EXPORT_URL: _bytes_response(_load_fixture_bytes()),
        }
    )
    _patch_client(handler, mock)
    ctx = _make_ctx(config=cfg, now=_PULL_NOW)

    signals = await _collect(handler, ctx)
    assert len(signals) == 1


async def test_pull_uses_since_hint_when_no_cursor() -> None:
    """With no stored cursor, an explicit `since` newer than the export file
    means that file is NOT new -> nothing emitted."""
    handler = GDELTFilesSourceHandler()
    mock = _MockResponses(
        {
            handler.config.lastupdate_url: _text_response(_LASTUPDATE_BODY),
            _EXPORT_URL: _bytes_response(_load_fixture_bytes()),
        }
    )
    _patch_client(handler, mock)
    ctx = _make_ctx()

    future_since = datetime(2026, 7, 21, 15, 0, 0, tzinfo=timezone.utc)
    signals = await _collect(handler, ctx, since=future_since)
    assert signals == []


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


async def test_health_check_healthy() -> None:
    handler = GDELTFilesSourceHandler()
    mock = _MockResponses(
        {handler.config.lastupdate_url: _text_response(_LASTUPDATE_BODY)}
    )
    _patch_client(handler, mock)
    ctx = _make_ctx()

    health = await handler.health_check(ctx)
    assert isinstance(health, SourceHealth)
    assert health.state == "healthy"
    assert health.detail["latest_export_ts"] == "2026-07-21T14:30:00+00:00"


async def test_health_check_degraded_when_no_events_line() -> None:
    handler = GDELTFilesSourceHandler()
    mock = _MockResponses(
        {
            handler.config.lastupdate_url: _text_response(
                f"12345 aaaa {_MENTIONS_URL}\n"
            ),
        }
    )
    _patch_client(handler, mock)
    ctx = _make_ctx()

    health = await handler.health_check(ctx)
    assert health.state == "degraded"


async def test_health_check_unhealthy_on_network_error() -> None:
    handler = GDELTFilesSourceHandler()
    mock = _MockResponses(
        {handler.config.lastupdate_url: lambda r: httpx.Response(500, text="err")}
    )
    _patch_client(handler, mock)
    ctx = _make_ctx()

    health = await handler.health_check(ctx)
    assert health.state == "unhealthy"


# ---------------------------------------------------------------------------
# Descriptor round-trip + factory discovery
# ---------------------------------------------------------------------------


def test_descriptor_round_trips_and_config_parses() -> None:
    body = yaml.safe_load((DESCRIPTORS_DIR / "source_gdelt_files.yaml").read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = SourceDescriptor.model_validate(body, strict=False)
    assert desc.identity.kind == "gdelt_files"
    assert desc.acquisition == "poll"
    assert desc.cadence is not None and desc.cadence.schedule is not None
    parsed = GDELTFilesConfig(**_unwrap_factory_dict(desc.config))
    assert parsed.event_root_codes == list(DEFAULT_EVENT_ROOT_CODES)
    assert parsed.min_num_mentions == 10


def test_descriptor_ships_draft_not_active() -> None:
    body = yaml.safe_load((DESCRIPTORS_DIR / "source_gdelt_files.yaml").read_text())
    assert body["identity"]["state"] == "draft"


def test_factory_discovers_gdelt_files_kind() -> None:
    registry = discover_source_kinds()
    assert "gdelt_files" in registry
    assert registry["gdelt_files"] is GDELTFilesSourceHandler


def test_factory_builds_gdelt_files_handler() -> None:
    registry = discover_source_kinds()
    handler = build_source_handler("gdelt_files", {}, registry=registry)
    assert isinstance(handler, GDELTFilesSourceHandler)
    assert isinstance(handler, SourceHandler)


# ---------------------------------------------------------------------------
# FilterStateStore scalar-cursor codec regression (source.gdelt.files outage,
# live 2026-07-24 onward — every poll failing with
# ``json.decoder.JSONDecodeError: Extra data: line 1 column 5 (char 4)``)
#
# ``gdelt_files.py`` is the only source handler in the tree that persists a
# BARE scalar (an ISO-timestamp string, ``newest_ts_processed.isoformat()``)
# as its cursor value via ``ctx.state_store.set`` — every other handler
# (rss/geojson/json_api/opensanctions/ucdp/acled/telegram) wraps its cursor
# in a dict. Production's ``FilterStateStore`` (``legba.runtime.state``)
# sits on a pool whose connections have a ``jsonb`` codec registered
# UNCONDITIONALLY (``PostgresStore._init_connection``) — the ``value``
# column therefore already arrives as a fully-decoded native Python value
# (str/int/float/bool/None/dict/list), not raw JSON text. The pre-fix
# ``FilterStateStore.get()`` special-cased only ``dict``/``list`` and
# unconditionally re-ran ``json.loads()`` on everything else, so a bare
# decoded string like ``"2026-07-24T09:45:00+00:00"`` (not valid JSON on its
# own — no wrapping quotes) blew up on every read after the FIRST successful
# write. ``InMemoryStateStore`` (used by every other test in this file)
# never serializes at all, so it could not have caught this — these tests
# exercise the real ``FilterStateStore`` against a fake pool that mimics the
# codec-registered connection's already-decoded ``value`` column.
# ---------------------------------------------------------------------------


class _FakeCodecConnection:
    """Mimics an asyncpg connection with ``PostgresStore``'s jsonb codec
    registered: ``fetchrow`` hands back the ``value`` column ALREADY decoded
    (any JSON shape), never raw JSON text — matching production, never the
    codec-less shape ``FilterStateStore.get()`` used to assume exclusively.
    """

    def __init__(self, table: dict[tuple[str, str, str], str]) -> None:
        self._table = table

    async def fetchrow(
        self, _query: str, actor_id: str, filter_id: str, key: str
    ) -> dict[str, Any] | None:
        raw = self._table.get((actor_id, filter_id, key))
        if raw is None:
            return None
        return {"value": json.loads(raw)}

    async def execute(
        self, _query: str, actor_id: str, filter_id: str, key: str, value: str
    ) -> None:
        self._table[(actor_id, filter_id, key)] = value


class _FakeAcquire:
    def __init__(self, conn: _FakeCodecConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeCodecConnection:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeCodecPool:
    """Stand-in for ``asyncpg.Pool`` whose connections carry the production
    jsonb codec — backs a real ``FilterStateStore`` without a live Postgres
    instance (worktree tests must not touch the live DB)."""

    def __init__(self) -> None:
        self._table: dict[tuple[str, str, str], str] = {}
        self._conn = _FakeCodecConnection(self._table)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


async def test_filter_state_store_scalar_string_roundtrips_through_codec_pool() -> None:
    """Pre-fix: raised json.decoder.JSONDecodeError on the read — the codec
    already decoded the stored JSON string once, and get() double-decoded."""
    from legba.runtime.state import FilterStateStore

    pool = _FakeCodecPool()
    store = FilterStateStore(pool, actor_id="source.gdelt.files", filter_id="filter")

    await store.set("gdelt_files_last_export_ts", "2026-07-24T09:45:00+00:00")
    result = await store.get("gdelt_files_last_export_ts")
    assert result == "2026-07-24T09:45:00+00:00"


async def test_filter_state_store_dict_value_still_roundtrips_through_codec_pool() -> None:
    """Guards the pre-existing dict-cursor path every OTHER source handler
    uses (rss/geojson/json_api/...) — the fix must not regress it."""
    from legba.runtime.state import FilterStateStore

    pool = _FakeCodecPool()
    store = FilterStateStore(pool, actor_id="a", filter_id="f")

    await store.set("cursor", {"etag": "abc", "consecutive_304": 0})
    result = await store.get("cursor")
    assert result == {"etag": "abc", "consecutive_304": 0}


async def test_filter_state_store_none_and_number_values_roundtrip() -> None:
    """Non-dict/list, non-str scalars (int/float/bool/None) must also pass
    through untouched — the pre-fix code would have tried json.loads() on
    an already-decoded int/float/bool too (a related latent bug, never
    reported live only because no handler happened to store one)."""
    from legba.runtime.state import FilterStateStore

    pool = _FakeCodecPool()
    store = FilterStateStore(pool, actor_id="a", filter_id="f")

    for key, value in (("n", 42), ("f", 3.5), ("b", True), ("z", None)):
        await store.set(key, value)
    assert await store.get("n") == 42
    assert await store.get("f") == 3.5
    assert await store.get("b") is True
    # A stored None round-trips as a cache miss (matches InMemoryStateStore /
    # every source handler's `if not raw: ...` no-prior-cursor convention).
    assert await store.get("z") is None


async def test_gdelt_files_cursor_read_survives_codec_decoded_scalar() -> None:
    """End-to-end through the real handler: seed the cursor exactly as the
    live 2026-07-24 incident left it after its one real success, then
    confirm the very next poll (lastupdate.txt still pointing at the same
    export file — the steady "nothing new yet" state every 15-min tick was
    landing in) reads it back without raising."""
    from legba.runtime.state import FilterStateStore

    pool = _FakeCodecPool()
    store = FilterStateStore(pool, actor_id="source.gdelt.files", filter_id="filter")
    await store.set("gdelt_files_last_export_ts", "2026-07-24T09:45:00+00:00")

    handler = GDELTFilesSourceHandler()
    same_export_url = (
        "http://data.gdeltproject.org/gdeltv2/20260724094500.export.CSV.zip"
    )
    mock = _MockResponses(
        {
            handler.config.lastupdate_url: _text_response(
                f"12345 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa {same_export_url}\n"
            ),
        }
    )
    _patch_client(handler, mock)
    ctx = SourceContext(
        target_id="target.test.world",
        target_version="v0",
        source_id="source.gdelt.files",
        config=GDELTFilesConfig(),
        state_store=store,
        logger=logging.getLogger("test.gdelt_files"),
    )

    signals = await _collect(handler, ctx)

    assert signals == []
    health = await store.get("gdelt_files_health")
    assert health["state"] == "healthy"
    # Cursor is unchanged — the index still points at the file we're already
    # past, so nothing new was walked.
    assert await store.get("gdelt_files_last_export_ts") == "2026-07-24T09:45:00+00:00"


# ---------------------------------------------------------------------------
# Predecessor retirement (2026-08-02, migration 0121)
#
# This handler exists to REPLACE source.gdelt.doc_api, but the old descriptor
# stayed `state='active'` and kept polling for weeks at an 84.3% HTTP-429 error
# rate — an un-retired legacy row, not a dual-source decision. Both paths share
# GDELT's PER-IP rate limit, so the dead one was spending the live one's
# headroom. These pin the disposal so it cannot quietly come back.
# ---------------------------------------------------------------------------


def _repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parents[2]


def test_doc_api_descriptor_declares_retired():
    """The DECLARED state matters as much as the DB row: bringup/registration
    reads the yaml, so leaving it `active` would resurrect the poller on the
    next register even after migration 0121 flipped the live row."""
    import yaml

    body = yaml.safe_load(
        (_repo_root() / "descriptors" / "source_gdelt_doc_api.yaml").read_text()
    )
    assert body["identity"]["id"] == "source.gdelt.doc_api"
    assert body["identity"]["state"] == "retired"


def test_successor_descriptor_is_not_retired():
    """Guard against retiring the wrong half of the swap."""
    import yaml

    body = yaml.safe_load(
        (_repo_root() / "descriptors" / "source_gdelt_files.yaml").read_text()
    )
    assert body["identity"]["id"] == "source.gdelt.files"
    assert body["identity"]["state"] != "retired"


def test_migration_0121_retires_by_state_not_delete():
    """RETIRE, not DELETE — doc_api has real ingested history (signals, poll
    outcomes, a source-quality track record) that must stay joinable. The
    opposite call from 0053, which deleted rows that had orphaned nothing."""
    sql = (
        _repo_root() / "src" / "legba" / "data" / "migrations"
        / "0121_retire_gdelt_doc_api.sql"
    ).read_text()
    assert "DELETE" not in sql.upper().split("--")[0] or "DELETE FROM" not in sql.upper()
    assert "state = 'retired'" in sql
    assert "source.gdelt.doc_api" in sql
    # Head row only, and idempotent on a second run.
    assert "is_head" in sql
    assert "state <> 'retired'" in sql
    # It must never touch the successor.
    assert "source.gdelt.files" not in sql.split("UPDATE")[-1]
