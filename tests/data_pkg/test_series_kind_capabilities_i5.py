# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""I-5 — the series-kind capabilities and the descriptors they unlock.

* ``series_api`` reads a top-level array (``$``), an indexed element
  (``[meta, rows]`` → ``1``) and positional rows (``row_columns``); a wider,
  still CLOSED date vocabulary (day-first forms matched exactly, never
  guessed); a since parameter rendered in the endpoint's own format inside an
  optional ``{date}`` template; the window keeps a period while its END is in
  it; a URL's own query is kept verbatim only when nothing is added.
* the JSON-stat 2.0 reader: dense and sparse values, status in all three
  shapes, both index shapes, and every malformed cube refused — including one
  over the declared cell bound.
* the record differ keys on an ATTRIBUTE (``@logicalId``) and reads
  attribute labels/summaries; an entry's own attributes are field ``@``.
* the currency-pair unit; series_change's named family rules (2026-11.3).
* the six I-5 descriptors validate through the real models and their
  handler's config schema, ship draft, walk to configured, and parse
  hand-made bodies in the documented shapes (invented values; no real data
  file is committed).

Equivalence for the EXISTING sources is pinned in
``test_series_kind_equivalence_i5.py``.
"""

from __future__ import annotations

from tests._lifecycle_rows import lifecycle_text

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import ValidationError

from legba.data.schemas.source import SourceDescriptor
from legba.data.schemas.source_series_api import DATE_FORMATS, SINCE_FORMATS, SeriesApiConfig
from legba.data.schemas.source_sink import UNITS
from legba.data.sources._contract import InMemoryStateStore, SourceContext
from legba.data.sources.instrument_diff import diff_instruments, parse_xml_records
from legba.data.sources.instrument_snapshot import InstrumentSnapshotConfig
from legba.data.sources.json_api import JsonApiConfig, JsonApiSourceHandler
from legba.data.sources.jsonstat import cube_rows
from legba.data.sources.series_api import (
    SeriesApiSourceHandler,
    format_since,
    parse_date,
    parse_rows,
    records_from_rows,
)
from legba.runtime.source_factory import _unwrap_factory_dict, build_source_handler

REPO = Path(__file__).resolve().parents[2]
DESC = REPO / "descriptors"
NOW = datetime(2026, 11, 14, 18, tzinfo=timezone.utc)


def _base(**over: Any) -> SeriesApiConfig:
    raw: dict[str, Any] = {
        "url": "https://example.invalid/series", "format": "json", "rows_path": "$",
        "date_field": "d", "date_format": "iso_date",
        "outputs": [{"series": "x.y", "value_field": "v", "subject": "global", "unit": "percent",
                     "methodology_version": "t/1"}],
    }
    raw.update(over)
    return SeriesApiConfig.model_validate(raw)


def _recs(cfg: SeriesApiConfig, body: bytes, since: date | None = None):
    return records_from_rows(parse_rows(body, cfg), cfg, page_url="https://example.invalid/p",
                             sha256="0" * 64, since=since)


# ---------------------------------------------------------------------------
# Row addressing
# ---------------------------------------------------------------------------


def test_a_top_level_array_is_read_at_the_root() -> None:
    cfg = _base()
    recs, skipped = _recs(cfg, b'[{"d": "2026-11-01", "v": 1.5}, {"d": "2026-11-02", "v": null}, 7]')
    assert [(r.valid_from, r.value) for r in recs] == [(date(2026, 11, 1), Decimal("1.5"))]
    assert skipped == 1                         # the null: absent, never zero; the scalar is not a row


def test_an_index_segment_reads_the_rows_of_a_meta_rows_pair() -> None:
    cfg = _base(rows_path="1", date_field="date", date_format="year",
                outputs=[{"series": "x.y", "value_field": "value", "subject_field": "country.id",
                          "unit": "percent", "period": "year", "subject_kind": "entity",
                          "methodology_version": "t/1"}])
    body = json.dumps([{"page": 1, "pages": 1, "total": 2},
                       [{"country": {"id": "AA"}, "date": "2025", "value": None},
                        {"country": {"id": "AA"}, "date": "2024", "value": 3.25}]]).encode()
    recs, skipped = _recs(cfg, body)
    assert [(r.subject, r.valid_from, r.valid_to, r.value) for r in recs] == [
        ("AA", date(2024, 1, 1), date(2024, 12, 31), Decimal("3.25"))]
    assert skipped == 1
    # An error document in the same top-level-array style says what it is.
    with pytest.raises(ValueError, match="Invalid value"):
        parse_rows(b'[{"message": [{"id": "120", "key": "Invalid value"}]}]', cfg)
    # An object key that LOOKS like an index is still a key.
    cfg_obj = _base(rows_path="data.1")
    assert parse_rows(b'{"data": {"1": [{"d": "2026-01-01", "v": 2}]}}', cfg_obj) == [{"d": "2026-01-01", "v": 2}]
    with pytest.raises(ValueError, match="not in the response"):
        parse_rows(b"[[]]", cfg)


def test_positional_rows_are_named_by_row_columns_and_a_wrong_width_is_refused() -> None:
    cfg = _base(rows_path="data", row_columns=["d", "v"])
    recs, _ = _recs(cfg, b'{"data": [["2026-11-01", "4.5"], ["2026-11-02", "4.75"]]}')
    assert [str(r.value) for r in recs] == ["4.5", "4.75"]
    with pytest.raises(ValueError, match="row_columns"):
        parse_rows(b'{"data": [["2026-11-01", "4.5", "x"]]}', cfg)
    with pytest.raises(ValueError, match="row_columns"):
        parse_rows(b'{"data": [{"d": "2026-11-01", "v": 1}]}', cfg)


@pytest.mark.parametrize("over, match", [
    ({"rows_path": "a..b"}, "rows_path"),
    ({"rows_path": "$.data"}, "rows_path"),
    ({"rows_path": "data[0]"}, "rows_path"),
    ({"format": "csv", "rows_path": "", "row_columns": ["d", "v"]}, "row_columns"),
    ({"format": "jsonstat", "rows_path": "$"}, "rows_path"),
    ({"row_columns": ["d", "d"]}, "distinct"),
    ({"url": "https://example.invalid/s?json", "params": [["a", "b"]]}, "query"),
    ({"url": "https://example.invalid/s?json", "since_param": {"name": "from"}}, "query"),
    ({"date_format": "dd/mm/yyyy"}, "date_format"),
    ({"date_format": "auto"}, "date_format"),
    ({"since_param": {"name": "from", "format": "dd.mm.yyyy"}}, "since_param"),
    ({"since_param": {"name": "from", "template": "{date}..{date}"}}, "template"),
    ({"since_param": {"name": "from", "template": "gte:{day}"}}, "template"),
    ({"jsonstat_max_cells": 2_000_000, "format": "jsonstat", "rows_path": ""}, "jsonstat_max_cells"),
])
def test_the_new_surface_is_closed_at_save(over, match) -> None:
    with pytest.raises(ValidationError, match=match):
        _base(**over)


# ---------------------------------------------------------------------------
# Dates: declared, exact, never guessed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw, fmt, want", [
    ("14.11.2026", "dmy_dotted", date(2026, 11, 14)),
    ("01.02.2026", "dmy_dotted", date(2026, 2, 1)),           # day first, always
    ("1.2.2026", "dmy_dotted", None),                          # not the declared form
    ("2026-11-14", "dmy_dotted", None),
    ("30.02.2026", "dmy_dotted", None),                        # no such day
    ("14/11/2026", "dmy_slashed", date(2026, 11, 14)),
    ("02/01/2026", "dmy_slashed", date(2026, 1, 2)),
    ("14.11.2026", "dmy_slashed", None),
    ("14/11/26", "dmy_slashed", None),
    ("01 Oct 2026", "day_mon_year", date(2026, 10, 1)),
    ("1 Oct 2026", "day_mon_year", date(2026, 10, 1)),
    ("01 oct 2026", "day_mon_year", None),                     # English abbreviations, as published
    ("01 Okt 2026", "day_mon_year", None),
    ("01 October 2026", "day_mon_year", None),
    ("01/Oct/2026", "day_mon_year", None),
    (" 14.11.2026 ", "dmy_dotted", date(2026, 11, 14)),        # surrounding blanks only
])
def test_day_first_dates_are_matched_exactly(raw, fmt, want) -> None:
    assert parse_date(raw, fmt) == want


def test_the_vocabularies_are_closed_lists() -> None:
    assert set(DATE_FORMATS) >= {"dmy_dotted", "dmy_slashed", "day_mon_year"}
    assert "day_mon_year_slashed" in SINCE_FORMATS and "day_mon_year_slashed" not in DATE_FORMATS


@pytest.mark.parametrize("fmt, template, want", [
    ("iso_date", None, "2026-10-01"),
    ("year_month", None, "2026-10"),
    ("year", None, "2026"),
    ("dmy_dotted", None, "01.10.2026"),
    ("dmy_slashed", None, "01/10/2026"),
    ("day_mon_year", None, "01 Oct 2026"),
    ("day_mon_year_slashed", None, "01/Oct/2026"),
    ("iso_date", "record_date:gte:{date}", "record_date:gte:2026-10-01"),
])
def test_a_since_parameter_is_rendered_in_the_endpoint_s_format(fmt, template, want) -> None:
    assert format_since(date(2026, 10, 1), fmt, template) == want


def test_a_period_is_kept_while_its_end_is_inside_the_window() -> None:
    cfg = _base(date_format="year_month",
                outputs=[{"series": "x.y", "value_field": "v", "subject": "global", "unit": "percent",
                          "period": "month", "methodology_version": "t/1"}])
    body = b'[{"d": "2026-08", "v": 1}, {"d": "2026-09", "v": 2}, {"d": "2026-10", "v": 3}]'
    recs, _ = _recs(cfg, body, since=date(2026, 9, 15))
    assert [r.valid_from for r in recs] == [date(2026, 9, 1), date(2026, 10, 1)]   # September ends inside


# ---------------------------------------------------------------------------
# The handler: the since parameter on the wire, a URL query kept verbatim
# ---------------------------------------------------------------------------


def _ctx(cfg) -> SourceContext:
    return SourceContext(target_id="t", target_version="v", source_id="source.x", config=cfg,
                         state_store=InMemoryStateStore(), now_fn=lambda: NOW)


async def _pull(cfg, responder) -> tuple[list, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    h = SeriesApiSourceHandler(cfg, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return [r async for r in h.pull(_ctx(cfg))], seen


@pytest.mark.asyncio
async def test_a_url_with_its_own_bare_flag_is_sent_verbatim() -> None:
    cfg = _base(url="https://example.invalid/statdirectory/exchange?json", date_field="exchangedate",
                date_format="dmy_dotted",
                outputs=[{"series": "x.fx", "value_field": "rate", "subject_field": "cc",
                          "unit": "fx_quote_per_base", "subject_kind": "entity", "methodology_version": "t/1"}])
    recs, seen = await _pull(cfg, lambda r: httpx.Response(200, json=[
        {"r030": 840, "txt": "x", "rate": 41.25, "cc": "USD", "exchangedate": "14.11.2026"}]))
    assert str(seen[0].url) == "https://example.invalid/statdirectory/exchange?json"
    assert [(r.subject, r.valid_from, r.value, r.unit) for r in recs] == [
        ("USD", date(2026, 11, 14), Decimal("41.25"), "fx_quote_per_base")]


@pytest.mark.asyncio
async def test_the_since_parameter_goes_out_in_the_declared_format() -> None:
    cfg = _base(format="csv", rows_path="", params=[["SeriesCodes", "X"]], date_field="DATE",
                date_format="day_mon_year",
                since_param={"name": "Datefrom", "format": "day_mon_year_slashed"},
                lag_days=1, revision_days=14, backfill_days=30,
                outputs=[{"series": "x.r", "value_field": "X", "subject": "GB", "unit": "percent",
                          "subject_kind": "country", "methodology_version": "t/1"}])
    recs, seen = await _pull(cfg, lambda r: httpx.Response(
        200, content=b"DATE,X\r\n14 Oct 2026,3.75\r\n13 Nov 2026,3.5\r\n"))
    assert seen[0].url.params["Datefrom"] == "15/Oct/2026"            # first poll: today - 30 days
    assert [(r.valid_from, str(r.value)) for r in recs] == [(date(2026, 11, 13), "3.5")]


# ---------------------------------------------------------------------------
# JSON-stat
# ---------------------------------------------------------------------------


def _cube(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "version": "2.0", "class": "dataset", "label": "invented",
        "id": ["freq", "geo", "time"], "size": [1, 2, 3],
        "dimension": {
            "freq": {"category": {"index": {"M": 0}}},
            "geo": {"category": {"index": {"AA": 0, "BB": 1}}},
            "time": {"category": {"index": ["2026-07", "2026-08", "2026-09"]}},
        },
        "value": {"0": 1.5, "2": 1.75, "4": 2.0},
        "status": {"2": "p"},
    }
    doc.update(over)
    return doc


def test_a_sparse_cube_becomes_one_row_per_present_cell() -> None:
    rows = cube_rows(_cube(), max_cells=100)
    assert rows == [
        {"freq": "M", "geo": "AA", "time": "2026-07", "value": 1.5},
        {"freq": "M", "geo": "AA", "time": "2026-09", "value": 1.75, "status": "p"},
        {"freq": "M", "geo": "BB", "time": "2026-08", "value": 2.0},
    ]


def test_a_dense_cube_with_status_as_a_list_or_one_string() -> None:
    dense = _cube(value=[1, None, 3, 4, 5, 6], status=["e", None, None, None, None, "p"])
    rows = cube_rows(dense, max_cells=6)
    assert [(r["geo"], r["time"], r["value"], r.get("status")) for r in rows] == [
        ("AA", "2026-07", 1, "e"), ("AA", "2026-09", 3, None), ("BB", "2026-07", 4, None),
        ("BB", "2026-08", 5, None), ("BB", "2026-09", 6, "p")]
    assert {r["status"] for r in cube_rows(_cube(status="x"), max_cells=6)} == {"x"}
    assert {r["status"] for r in cube_rows(_cube(status=["x"]), max_cells=6)} == {"x"}


@pytest.mark.parametrize("over, match", [
    ({"class": "bundle"}, "not a dataset"),
    ({"version": "1.0"}, "version"),
    ({"size": [1, 2]}, "sizes"),
    ({"size": [1, 3, 3]}, "categories"),
    ({"value": [1, 2, 3]}, "dense value"),
    ({"value": {"6": 1}}, "outside the cube"),
    ({"value": {"x": 1}}, "not an index"),
    ({"id": ["freq", "geo", "value"], "dimension": {"freq": {}, "geo": {}, "value": {}}}, "not 'value'"),
    ({"dimension": {"freq": {"category": {"index": {"M": 0}}}, "geo": {"category": {}},
                    "time": {"category": {"index": ["a", "b", "c"]}}}}, "no category index"),
    ({"dimension": {"freq": {"category": {"index": {"M": 0}}},
                    "geo": {"category": {"index": {"AA": 0, "BB": 0}}},
                    "time": {"category": {"index": ["a", "b", "c"]}}}}, "position"),
])
def test_a_malformed_cube_is_refused(over, match) -> None:
    with pytest.raises(ValueError, match=match):
        cube_rows(_cube(**over), max_cells=100)


def test_a_cube_over_the_declared_bound_is_refused_before_any_row() -> None:
    with pytest.raises(ValueError, match="over this source's bound of 5"):
        cube_rows(_cube(), max_cells=5)


def test_a_jsonstat_source_maps_cells_onto_observations() -> None:
    cfg = _base(format="jsonstat", rows_path="", date_field="time", date_format="year_month",
                provenance_fields=["status"], jsonstat_max_cells=50,
                outputs=[{"series": "x.hicp", "value_field": "value", "subject_field": "geo",
                          "where": [{"field": "freq", "values": ["M"]}], "unit": "percent",
                          "period": "month", "subject_kind": "entity", "methodology_version": "t/1"}])
    recs, _ = _recs(cfg, json.dumps(_cube()).encode())
    assert [(r.subject, r.valid_from, r.valid_to, str(r.value), r.provenance.get("status")) for r in recs] == [
        ("AA", date(2026, 7, 1), date(2026, 7, 31), "1.5", None),
        ("AA", date(2026, 9, 1), date(2026, 9, 30), "1.75", "p"),
        ("BB", date(2026, 8, 1), date(2026, 8, 31), "2.0", None)]
    with pytest.raises(ValueError, match="bound"):
        parse_rows(json.dumps(_cube()).encode(), cfg.model_copy(update={"jsonstat_max_cells": 4}))


# ---------------------------------------------------------------------------
# The record differ: an attribute key
# ---------------------------------------------------------------------------

_LIST_A = b"""<?xml version="1.0" encoding="UTF-8"?>
<!-- FIXTURE: an attribute-keyed register SHAPE with invented entries (no real party). -->
<export xmlns="urn:example:register" generationDate="2026-11-01T00:00:00">
  <entity logicalId="10" refNumber="XX.1.1">
    <regulation programme="AAA"/>
    <nameAlias wholeName="Example Holding One"/>
  </entity>
  <entity logicalId="11" refNumber="XX.1.2">
    <regulation programme="BBB"/>
    <nameAlias wholeName="Example Person Two"/>
  </entity>
  <entity logicalId="12" refNumber="XX.1.3">
    <nameAlias wholeName="Example Vessel Three"/>
  </entity>
  <entity refNumber="XX.1.9"><nameAlias wholeName="No Key"/></entity>
</export>
"""
_LIST_B = (_LIST_A.replace(b'generationDate="2026-11-01T00:00:00"', b'generationDate="2026-11-02T00:00:00"')
           .replace(b'refNumber="XX.1.2"', b'refNumber="XX.1.20"')
           .replace(b'<regulation programme="AAA"/>', b'<regulation programme="AAA"/><regulation programme="CCC"/>')
           .replace(b'<entity logicalId="12" refNumber="XX.1.3">\n    <nameAlias wholeName="Example Vessel Three"/>\n  </entity>',
                    b'<entity logicalId="13" refNumber="XX.1.4"><nameAlias wholeName="Example Firm Four"/></entity>'))


def _attr_parse(body: bytes):
    return parse_xml_records(body, record_tag="entity", key_path="@logicalId",
                             label_paths=["nameAlias/@wholeName"],
                             summary_paths=["@refNumber", "regulation/@programme"])


def test_an_attribute_key_reads_the_list_s_own_identifier() -> None:
    a = _attr_parse(_LIST_A)
    assert sorted(a.entries) == ["10", "11", "12"] and a.records_without_key == 1
    assert a.entries["10"].label == "Example Holding One"
    assert a.entries["10"].summary == {"@refNumber": ["XX.1.1"], "regulation/@programme": ["AAA"]}
    diff = diff_instruments(a, _attr_parse(_LIST_B))
    assert diff["counts"] == {"added": 1, "removed": 1, "changed": 2}
    assert [e["key"] for e in diff["added"]] == ["13"] and [e["key"] for e in diff["removed"]] == ["12"]
    changed = {c["key"]: c["fields_changed"] for c in diff["changed"]}
    assert changed == {"10": ["regulation"], "11": ["@"]}           # the entry's own attribute moved
    # A regenerated-but-identical list is zero entries changed.
    same = _LIST_A.replace(b"2026-11-01T00:00:00", b"2026-11-09T00:00:00")
    assert diff_instruments(a, _attr_parse(same))["counts"] == {"added": 0, "removed": 0, "changed": 0}


@pytest.mark.parametrize("over, match", [
    ({"key_path": "@a/b"}, "last"),
    ({"key_path": "x/@"}, "named"),
    ({"label_paths": ["@a/b"]}, "last"),
    ({"published_date_path": "@generationDate"}, "element text"),
])
def test_attribute_paths_are_checked_at_save(over, match) -> None:
    raw = {"instrument_name": "x", "url": "https://example.invalid/x.xml", "license_class": "cc_by",
           "differ": "xml_records", "record_tag": "entity", "key_path": "@logicalId", **over}
    with pytest.raises(ValidationError, match=match):
        InstrumentSnapshotConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Units and series_change's named family rules
# ---------------------------------------------------------------------------


def test_the_currency_pair_unit_is_in_the_closed_list() -> None:
    assert "fx_quote_per_base" in UNITS and "ratio" in UNITS


def _pts(values, start=date(2026, 10, 1)):
    from datetime import timedelta

    return [{"valid_from": start + timedelta(days=i), "value": v, "id": f"o{i}"} for i, v in enumerate(values)]


def test_named_rules_parse_and_refuse_an_unknown_name() -> None:
    from legba.data.analysts.deterministic_handlers.series_change import NAMED_RULES, parse_series_rule

    assert parse_series_rule("ecb.exr.*=fx_move") == ("ecb.exr.*", "fx_move")
    assert set(NAMED_RULES) == {"policy_rate", "fx_move"}
    for bad in ("ecb.exr.*=band", "ecb.exr.*", "ECB.*=fx_move"):
        with pytest.raises(ValueError):
            parse_series_rule(bad)


def test_an_fx_move_is_measured_across_the_current_window() -> None:
    from legba.data.analysts.deterministic_handlers.series_change import move_event

    flat = _pts([1.10, 1.101, 1.099, 1.102, 1.10, 1.098, 1.101, 1.10, 1.099, 1.10])
    assert move_event(flat, pct=2.0, current_days=7) is None             # daily wiggle: no event
    jump = _pts([1.10, 1.10, 1.10, 1.10, 1.11, 1.12, 1.13, 1.14, 1.13, 1.125])
    ev = move_event(jump, pct=2.0, current_days=7)
    assert ev is not None and ev["status"] == "move_up"
    assert ev["prev"]["valid_from"] == date(2026, 10, 3) and ev["cur"]["valid_from"] == date(2026, 10, 10)
    assert ev["move_pct"] == pytest.approx(2.273, abs=1e-3)
    assert move_event(_pts([0.0, 0.0, 0.0, 5.0]), pct=2.0, current_days=2) is None   # from zero: undefined
    assert move_event(_pts([1.0, 0.9]), pct=2.0, current_days=7) is None              # nothing before the window


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, *args):
        return self.rows


class _Pool:
    def __init__(self, rows):
        self.rows = rows

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return _Conn(pool.rows)

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def _obs(series, subject, values, start):
    from datetime import timedelta
    from uuid import uuid4

    return [{"id": uuid4(), "source_id": "source.x", "series_id": series, "subject": subject,
             "valid_from": start + timedelta(days=i), "value": Decimal(str(v)), "unit": "u",
             "record_time": NOW, "fetch_sha256": "f" * 64} for i, v in enumerate(values)]


@pytest.mark.asyncio
async def test_named_family_rules_judge_rates_and_fx_and_leave_the_rest_alone() -> None:
    from legba.data.analysts.deterministic_handlers import series_change

    today = datetime.now(tz=timezone.utc).date()
    from datetime import timedelta

    start = today - timedelta(days=12)
    rows = (_obs("ecb.fm.deposit_facility_rate", "XM", [2.5] * 8 + [2.25] * 4, start)
            + _obs("ecb.exr.reference_rate", "USD", [1.10] * 5 + [1.12, 1.13, 1.14, 1.15, 1.15, 1.15, 1.16], start)
            + _obs("nbu.exchange.official_rate", "EUR", [48.0, 48.1, 47.9] * 4, start))
    deps = type("D", (), {"pg_pool": _Pool(rows)})()
    opts = {"source_ids": ["source.x"], "series_rules": ["ecb.fm.*=policy_rate", "ecb.exr.*=fx_move"]}
    res = await series_change.handle([], opts, deps)
    data = res.finding.data
    assert data["method_version"] == "series_change/2026-11.3"
    got = {(c["series_id"], c["status"], c["rule"]) for c in data["crossings"]}
    assert got == {("ecb.fm.deposit_facility_rate", "step_down", "policy_rate:step>0"),
                   ("ecb.exr.reference_rate", "move_up", "fx_move:move>=2%")}
    assert data["counts"]["event"] == 2 and data["counts"]["insufficient"] == 1   # NBU: band, too few days
    # Without series_rules the same rows read as 2026-11.2 did: nothing rule-judged.
    plain = (await series_change.handle([], {"source_ids": ["source.x"]}, deps)).finding.data
    assert plain["counts"]["event"] == 0 and plain["crossings"] == []


def test_the_option_catalog_takes_the_proposed_rules_and_refuses_others() -> None:
    from legba.data.analysts.deterministic_handlers.series_change import NAMED_RULES, PROPOSED_SERIES_RULES
    from legba.data.option_catalog.handler_options_catalog import HANDLER_OPTIONS

    spec = {s.name: s for s in HANDLER_OPTIONS["series_change"]}["series_rules"]
    assert spec.validate(list(PROPOSED_SERIES_RULES))[0]
    assert not spec.validate(["ecb.exr.*=band"])[0]
    assert set(re.findall(r"[a-z_]+", spec.pattern.pattern.split("=")[1])) == set(NAMED_RULES)
    assert set(PROPOSED_SERIES_RULES) >= {"ecb.fm.*=policy_rate", "ecb.exr.*=fx_move"}


def test_the_shipped_descriptor_carries_the_proposed_rules() -> None:
    # Roll 84 registered the proposed rules: the tree's descriptor names exactly
    # them, in order, beside the 2026-11.2 options it kept.
    from legba.data.analysts.deterministic_handlers.series_change import PROPOSED_SERIES_RULES

    options = yaml.safe_load((DESC / "analyst_series_change.yaml").read_text())["method"]["options"]
    assert tuple(options["series_rules"]) == PROPOSED_SERIES_RULES
    assert options["level_rules"] == ["eia.pet_spot.rbrte>=100"] and options["step_series"] == ["bis.cbpol.*"]


# ---------------------------------------------------------------------------
# The I-5 descriptors
# ---------------------------------------------------------------------------

I5: dict[str, str] = {
    "source_worldbank_wdi_cpi_inflation_world.yaml": "series_api",
    "source_nbu_exchange_daily.yaml": "series_api",
    "source_bcb_selic_target.yaml": "series_api",
    "source_boe_bank_rate_daily.yaml": "series_api",
    "source_eurostat_hicp_ea_monthly.yaml": "series_api",
    "source_jma_quake_list.yaml": "json_api",
    # roll 84: the NBU key rates, written as I-5 wrote its siblings
    "source_nbu_key_rates.yaml": "series_api",
}
_SCHEMA = {"series_api": SeriesApiConfig, "json_api": JsonApiConfig}


def _body(name: str) -> dict[str, Any]:
    body = yaml.safe_load((DESC / name).read_text(encoding="utf-8"))
    body["identity"]["version"] = "0" * 16
    return body


def _lifecycle() -> dict[str, tuple[str, str]]:
    return {m.group(1): (m.group(2), m.group(3)) for m in re.finditer(
        r"^\| source \| (\S+) \| (\w+) \| (\w+) \|$", lifecycle_text(DESC), re.M)}


@pytest.mark.parametrize("name", sorted(I5))
def test_every_i5_descriptor_validates_ships_draft_and_walks_to_configured(name) -> None:
    body = _body(name)
    d = SourceDescriptor.model_validate(body, strict=False)
    kind = I5[name]
    assert d.identity.kind == kind
    _SCHEMA[kind].model_validate(_unwrap_factory_dict(body["config"]))
    build_source_handler(kind, body["config"])
    assert d.identity.state.value == "draft" and d.scope.access_class == "public"
    assert d.scope.source_class == "official"
    assert d.acquisition == "poll" and d.cadence is not None and d.cadence.schedule is not None
    assert _lifecycle().get(d.identity.id) == ("configured", "draft"), d.identity.id
    assert d.scope.license_class in ("cc_by", "open_gov_attribution", "open_data_sharealike")
    assert d.scope.attribution
    header = " ".join(ln.lstrip("#").strip() for ln in
                      (DESC / name).read_text().split("\nidentity:", 1)[0].splitlines())
    assert "robots.txt" in header
    if kind == "series_api":
        cfg = SeriesApiConfig.model_validate(_unwrap_factory_dict(body["config"]))
        assert cfg.auth is None                                     # nothing keyed


def _cfg(name: str) -> SeriesApiConfig:
    return SeriesApiConfig.model_validate(_unwrap_factory_dict(_body(name)["config"]))


def test_world_bank_meta_rows_pair_reads_the_world_row() -> None:
    cfg = _cfg("source_worldbank_wdi_cpi_inflation_world.yaml")
    body = json.dumps([
        {"page": 1, "pages": 1, "per_page": 100, "total": 3, "lastupdated": "2026-10-01"},
        [{"indicator": {"id": "FP.CPI.TOTL.ZG", "value": "invented"}, "country": {"id": "1W", "value": "World"},
          "countryiso3code": "WLD", "date": "2025", "value": None, "unit": "", "obs_status": "", "decimal": 1},
         {"indicator": {"id": "FP.CPI.TOTL.ZG", "value": "invented"}, "country": {"id": "1W", "value": "World"},
          "countryiso3code": "WLD", "date": "2024", "value": 4.4, "unit": "", "obs_status": "", "decimal": 1},
         {"indicator": {"id": "FP.CPI.TOTL.ZG", "value": "invented"}, "country": {"id": "ZZ", "value": "Other"},
          "countryiso3code": "ZZZ", "date": "2024", "value": 9.9, "unit": "", "obs_status": "", "decimal": 1}],
    ]).encode()
    recs, skipped = _recs(cfg, body, since=date(2024, 6, 1))
    assert [(r.series_id, r.subject, r.subject_kind, r.valid_from, r.valid_to, str(r.value), r.unit)
            for r in recs] == [("worldbank.wdi.fp_cpi_totl_zg", "global", "global", date(2024, 1, 1),
                                date(2024, 12, 31), "4.4", "percent")]
    assert skipped == 1                                             # 2025 null: absent


def test_nbu_top_level_array_with_dotted_dates() -> None:
    cfg = _cfg("source_nbu_exchange_daily.yaml")
    body = json.dumps([
        {"r030": 840, "txt": "invented", "rate": 41.0123, "cc": "USD", "exchangedate": "14.11.2026"},
        {"r030": 978, "txt": "invented", "rate": 47.5, "cc": "EUR", "exchangedate": "14.11.2026"},
    ], ensure_ascii=False).encode()
    recs, _ = _recs(cfg, body)
    assert [(r.subject, r.valid_from, str(r.value), r.unit, r.provenance["r030"]) for r in recs] == [
        ("USD", date(2026, 11, 14), "41.0123", "fx_quote_per_base", 840),
        ("EUR", date(2026, 11, 14), "47.5", "fx_quote_per_base", 978)]
    assert cfg.url.endswith("?json") and not cfg.params and cfg.since_param is None


def test_nbu_key_rates_compact_dates_and_one_series_per_id_api() -> None:
    """Roll 84: YYYYMMDD read exactly (``ymd_compact``); each ``id_api`` feeds
    its own series; rows before the window are not read (the endpoint has
    no verified date filter, so the window is applied here)."""
    cfg = _cfg("source_nbu_key_rates.yaml")
    row = {"txt": "invented", "txten": "invented", "leveli": 1, "parent": "Key_Rates_NBU", "freq": "D", "tzep": "T90"}
    body = json.dumps([
        {**row, "dt": "19920727", "id_api": "KEY_PolicyRate", "value": 30},
        {**row, "dt": "20261113", "id_api": "KEY_PolicyRate", "value": 15.5},
        {**row, "dt": "20261113", "id_api": "KEY_RateOvernightLoans", "value": 17.5},
        {**row, "dt": "20261113", "id_api": "KEY_RateOvernightCD", "value": 13.5},
        {**row, "dt": "2026-11-13", "id_api": "KEY_PolicyRate", "value": 99},
    ]).encode()
    recs, _ = _recs(cfg, body, since=date(2026, 11, 1))
    assert sorted((r.series_id, r.subject, r.valid_from, str(r.value), r.provenance["id_api"]) for r in recs) == [
        ("nbu.key.overnight_cd_rate", "UA", date(2026, 11, 13), "13.5", "KEY_RateOvernightCD"),
        ("nbu.key.overnight_loan_rate", "UA", date(2026, 11, 13), "17.5", "KEY_RateOvernightLoans"),
        ("nbu.key.policy_rate", "UA", date(2026, 11, 13), "15.5", "KEY_PolicyRate")]
    assert cfg.date_format == "ymd_compact" and cfg.url.endswith("/statdirectory/key?json")
    from legba.data.sources.series_api import parse_date
    assert parse_date("20261113", "ymd_compact") == date(2026, 11, 13)
    for bad in ("2026113", "2026-11-13", "20261313", "202611130"):
        assert parse_date(bad, "ymd_compact") is None
    from legba.data.analysts.deterministic_handlers.series_change import PROPOSED_SERIES_RULES
    assert "nbu.key.*=policy_rate" in PROPOSED_SERIES_RULES


def test_bcb_string_values_and_slashed_day_first_dates() -> None:
    cfg = _cfg("source_bcb_selic_target.yaml")
    body = b'[{"data":"12/11/2026","valor":"10.50"},{"data":"13/11/2026","valor":""},{"data":"01/12/2026","valor":"10.25"}]'
    recs, skipped = _recs(cfg, body)
    assert [(r.subject, r.subject_kind, r.valid_from, str(r.value)) for r in recs] == [
        ("BR", "country", date(2026, 11, 12), "10.50"), ("BR", "country", date(2026, 12, 1), "10.25")]
    assert skipped == 1


def test_boe_csv_dates_and_the_rendered_datefrom() -> None:
    cfg = _cfg("source_boe_bank_rate_daily.yaml")
    recs, _ = _recs(cfg, b"DATE,IUDBEDR\r\n03 Nov 2026,3.75\r\n04 Nov 2026,3.5\r\n")
    assert [(r.subject, r.valid_from, str(r.value)) for r in recs] == [
        ("GB", date(2026, 11, 3), "3.75"), ("GB", date(2026, 11, 4), "3.5")]
    params, _ = SeriesApiSourceHandler(cfg)._request(date(2026, 10, 1), 0, None)
    assert ("Datefrom", "01/Oct/2026") in params and ("SeriesCodes", "IUDBEDR") in params


def test_eurostat_hicp_cube_reads_the_euro_area_months() -> None:
    cfg = _cfg("source_eurostat_hicp_ea_monthly.yaml")
    cube = {
        "version": "2.0", "class": "dataset", "label": "invented", "source": "x", "updated": "2026-11-01",
        "id": ["freq", "unit", "coicop", "geo", "time"], "size": [1, 1, 1, 1, 2],
        "dimension": {d: {"category": {"index": {c: 0}}} for d, c in
                      (("freq", "M"), ("unit", "RCH_A"), ("coicop", "CP00"), ("geo", "EA"))}
        | {"time": {"category": {"index": {"2026-09": 0, "2026-10": 1}}}},
        "value": {"0": 2.1, "1": 2.0}, "status": {"1": "p"},
    }
    recs, _ = _recs(cfg, json.dumps(cube).encode())
    assert [(r.subject, r.subject_kind, r.valid_from, r.valid_to, str(r.value), r.provenance.get("status"))
            for r in recs] == [("EA", "entity", date(2026, 9, 1), date(2026, 9, 30), "2.1", None),
                               ("EA", "entity", date(2026, 10, 1), date(2026, 10, 31), "2.0", "p")]


@pytest.mark.asyncio
async def test_jma_bulletins_map_to_signals() -> None:
    body = _body("source_jma_quake_list.yaml")
    cfg = JsonApiConfig.model_validate(_unwrap_factory_dict(body["config"]))
    items = [
        {"ctt": "20261114101500", "eid": "20261114101000", "rdt": "2026-11-14T10:15:00+09:00",
         "ttl": "invented", "ift": "x", "ser": "1", "at": "2026-11-14T10:10:00+09:00", "anm": "invented",
         "acd": "000", "cod": "+35.0+139.0-10000/", "mag": "4.2", "maxi": "3", "int": [],
         "json": "20261114101500_20261114101000_TEST_1.json",
         "en_ttl": "Earthquake and Seismic Intensity Information", "en_anm": "Invented Region"},
        {"ctt": "20261114101200", "eid": "20261114101000", "rdt": "2026-11-14T10:12:00+09:00",
         "ttl": "invented", "ift": "x", "ser": 0, "at": "2026-11-14T10:10:00+09:00", "anm": "", "acd": "",
         "cod": "", "mag": "", "maxi": "3", "int": [], "json": "20261114101200_20261114101000_TEST_0.json",
         "en_ttl": "Seismic Intensity Information", "en_anm": ""},
    ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=items)))
    h = JsonApiSourceHandler(cfg, http_client=client)
    ctx = SourceContext(target_id="t", target_version="v", source_id="source.jma.quake_list", config=cfg,
                        state_store=InMemoryStateStore(), now_fn=lambda: NOW)
    sigs = [s async for s in h.pull(ctx, None)]
    by = {s.payload["external_id"]: s.payload for s in sigs}
    full = by["20261114101500_20261114101000_TEST_1.json"]
    assert full["title"] == "JMA Earthquake and Seismic Intensity Information: Invented Region M4.2"
    assert full["published_at"] == "2026-11-14T01:15:00+00:00"
    assert "Magnitude: 4.2" in full["raw_body"] and "Maximum seismic intensity (JMA scale): 3" in full["raw_body"]
    flash = by["20261114101200_20261114101000_TEST_0.json"]
    assert flash["title"] == "JMA Seismic Intensity Information"      # missing fields drop cleanly
