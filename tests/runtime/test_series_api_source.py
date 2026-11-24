# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-1 — the generic ``series_api`` kind, pure (no database).

* the config is checked AT SAVE: a source that cannot produce a dated numeric
  value is refused, and every output must agree with the sink's series registry;
* the two shipped descriptors (EIA spot prices, BIS policy rates) validate and
  build through the production factory;
* recorded payload shapes (values fictional) parse to the right records:
  absent values are absent, never zero; the euro area is not a country;
* the poll window re-reads lag + revision days (backfill on the first poll),
  pages, refuses to poll a keyed endpoint without a resolver, and keeps the
  key out of the record URL, the receipt URL and the httpx request log;
* series_change's rule-judged series: a level entered/left, a step change.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from legba.data.schemas.source import SourceDescriptor
from legba.data.schemas.source_series_api import SeriesApiConfig
from legba.data.sources._contract import InMemoryStateStore, SourceContext
from legba.data.sources.fetch_capture import CaptureLog, ReceiptTeeTransport, capture_scope
from legba.data.sources.series_api import (
    SeriesApiAuthNotConfigured,
    SeriesApiSourceHandler,
    parse_date,
    parse_rows,
    period_end,
    records_from_rows,
    window_start,
)

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "series_api"
EIA = REPO / "descriptors" / "source_eia_petroleum_spot_daily.yaml"
BIS = REPO / "descriptors" / "source_bis_policy_rates_daily.yaml"
NOW = datetime(2026, 3, 6, 12, tzinfo=timezone.utc)
SECRET = "k3y-fict1onal-0123456789"


def _body(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _cfg(path: Path) -> SeriesApiConfig:
    from legba.runtime.source_factory import _unwrap_factory_dict

    return SeriesApiConfig(**_unwrap_factory_dict(_body(path)["config"]))


def _ctx(cfg: SeriesApiConfig, *, secrets=None, state=None) -> SourceContext:
    return SourceContext(target_id="t", target_version="v", source_id="source.x", config=cfg,
                         state_store=state or InMemoryStateStore(), secrets_resolve=secrets,
                         now_fn=lambda: NOW)


# ---------------------------------------------------------------------------
# Save-time validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [EIA, BIS])
def test_the_shipped_descriptors_validate_and_build(path: Path) -> None:
    from legba.runtime.source_factory import build_source_handler

    body = _body(path)
    d = SourceDescriptor.model_validate(body, strict=False)
    assert d.identity.state.value == "draft" and d.sink.family == "observation"
    assert d.scope.license_class and d.scope.attribution
    h = build_source_handler("series_api", body["config"])
    assert isinstance(h, SeriesApiSourceHandler)
    active = copy.deepcopy(body)
    active["identity"]["state"] = "active"
    SourceDescriptor.model_validate(active, strict=False)   # active needs a cadence: it has one


def _mutate(path: Path, fn) -> dict:
    body = copy.deepcopy(_body(path))
    fn(body)
    return body


@pytest.mark.parametrize("fn, match", [
    (lambda b: b["config"]["outputs"]["raw"][0].update(unit="usd"), "unit"),
    (lambda b: b["config"]["outputs"]["raw"][0].update(series="eia.pet_spot.brent"), "not declared"),
    (lambda b: b["config"]["outputs"]["raw"][0].update(subject_kind="country"), "subject_kind"),
    (lambda b: b["config"]["outputs"]["raw"][0].update(value_field="price"), "value_field"),
    (lambda b: b["config"]["outputs"]["raw"][0].pop("subject"), "subject"),
    (lambda b: b["config"]["outputs"].update(raw=[]), "outputs"),
    (lambda b: b["config"]["date_format"].update(raw="dd/mm/yyyy"), "date_format"),
    (lambda b: b["config"]["rows_path"].update(raw=""), "rows_path"),
    (lambda b: b["config"]["url"].update(raw="http://api.eia.gov/v2/x"), "url"),
    (lambda b: b["config"]["auth"]["raw"].update(secret_ref="abc def"), "secret_ref"),
    (lambda b: b["config"]["params"]["raw"].append(["api_key", "x"]), "auth parameter"),
    (lambda b: b["sink"].update(family="document", config={}), "observation"),
    (lambda b: b["config"].update(surprise={"factory_kind": "text", "raw": "x"}), "surprise"),
])
def test_a_config_that_cannot_produce_a_dated_number_is_refused_at_save(fn, match) -> None:
    with pytest.raises(ValidationError, match=match):
        SourceDescriptor.model_validate(_mutate(EIA, fn), strict=False)


def test_a_document_source_is_untouched_by_the_series_check() -> None:
    body = _body(REPO / "descriptors" / "source_imf_portwatch_chokepoints.yaml")
    SourceDescriptor.model_validate(body, strict=False)


# ---------------------------------------------------------------------------
# Parsing recorded shapes
# ---------------------------------------------------------------------------


def test_eia_shape_parses_two_series_and_leaves_a_null_absent() -> None:
    cfg = _cfg(EIA)
    body = (FIX / "eia_pet_spot_daily.json").read_bytes()
    rows = parse_rows(body, cfg)
    recs, skipped = records_from_rows(rows, cfg, page_url="https://api.example/x", sha256="a" * 64)
    assert skipped == 1   # the null WTI value: absent, never zero
    by = {(r.series_id, r.valid_from): r for r in recs}
    brent = by[("eia.pet_spot.rbrte", date(2026, 3, 3))]
    assert brent.value == Decimal("101.25") and brent.unit == "usd_per_barrel" and brent.subject == "global"
    assert brent.provenance == {"series": "RBRTE", "units": "$/BBL", "value_field": "value"}
    assert len([r for r in recs if r.series_id == "eia.pet_spot.rwtc"]) == 2
    with pytest.raises(ValueError, match="rows_path"):
        parse_rows(b'{"error": {"code": "API_KEY_MISSING"}}', cfg)


def test_bis_shape_routes_countries_and_the_euro_area_and_skips_nan() -> None:
    cfg = _cfg(BIS)
    body = (FIX / "bis_ws_cbpol_daily.csv").read_bytes()
    recs, skipped = records_from_rows(parse_rows(body, cfg), cfg, page_url="u", sha256="b" * 64)
    assert skipped == 1   # CL 2026-03-02 NaN / OBS_STATUS M
    area = [r for r in recs if r.series_id == "bis.cbpol.policy_rate_area"]
    assert {r.subject for r in area} == {"XM"} and {r.subject_kind for r in area} == {"entity"}
    countries = [r for r in recs if r.series_id == "bis.cbpol.policy_rate"]
    assert {r.subject for r in countries} == {"CL", "US"} and {r.subject_kind for r in countries} == {"country"}
    us = sorted((r.valid_from, r.value) for r in countries if r.subject == "US")
    assert us[-1] == (date(2026, 3, 3), Decimal("1.875")) and all(r.unit == "percent" for r in recs)
    # Rows older than the asked window are not written (a provider returning more).
    later, _ = records_from_rows(parse_rows(body, cfg), cfg, page_url="u", sha256="b" * 64,
                                 since=date(2026, 3, 3))
    assert {r.valid_from for r in later} == {date(2026, 3, 3)}


@pytest.mark.parametrize("raw, fmt, want", [
    ("2026-03-02", "iso_date", date(2026, 3, 2)),
    ("2026-03-02T23:30:00-05:00", "iso_datetime", date(2026, 3, 3)),
    ("2026-03", "year_month", date(2026, 3, 1)),
    ("2026", "year", date(2026, 1, 1)),
    ("2026-Q3", "year_quarter", date(2026, 7, 1)),
    (1772409600000, "epoch_ms", date(2026, 3, 2)),
    ("not a date", "iso_date", None),
    (None, "iso_date", None),
])
def test_dates(raw, fmt, want) -> None:
    assert parse_date(raw, fmt) == want


def test_period_ends() -> None:
    assert period_end(date(2026, 2, 1), "month") == date(2026, 2, 28)
    assert period_end(date(2026, 4, 1), "quarter") == date(2026, 6, 30)
    assert period_end(date(2026, 1, 1), "year") == date(2026, 12, 31)
    assert period_end(date(2026, 3, 2), "week") == date(2026, 3, 8)
    assert period_end(date(2026, 3, 2), "day") == date(2026, 3, 2)


def test_the_window_rereads_lag_and_revisions_and_backfills_first() -> None:
    cfg = _cfg(EIA)
    today = date(2026, 11, 13)
    assert window_start(today, cfg, first_poll=True) == date(2025, 10, 9)        # 400 days
    assert window_start(today, cfg, first_poll=False) == date(2026, 10, 12)      # 2 + 30 days
    assert window_start(today, cfg.model_copy(update={"backfill_from": "2020-01-01"}),
                        first_poll=False) == date(2020, 1, 1)


# ---------------------------------------------------------------------------
# The handler over a transport double
# ---------------------------------------------------------------------------


class _Eia:
    """An EIA API double: pages of 5 rows over a fixed table; answers 403
    without the key, like the real endpoint."""

    def __init__(self, n: int = 12) -> None:
        self.rows = [{"period": f"2026-03-{d:02d}", "series": s, "value": f"{90 + d}.5", "units": "$/BBL"}
                     for d in range(1, n // 2 + 1) for s in ("RBRTE", "RWTC")]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.params.get("api_key") != SECRET:
            return httpx.Response(403, json={"error": {"code": "API_KEY_MISSING"}})
        start = request.url.params["start"]
        rows = [r for r in self.rows if r["period"] >= start]
        off, size = int(request.url.params["offset"]), int(request.url.params["length"])
        return httpx.Response(200, json={"response": {"total": str(len(rows)), "data": rows[off:off + size]}})


async def _secrets(ref: str) -> str:
    assert ref == "source.eia.api_key"
    return SECRET


@pytest.mark.asyncio
async def test_the_handler_pages_and_keeps_the_key_out_of_every_record_receipt_and_log(caplog) -> None:
    cfg = _cfg(EIA).model_copy(update={"pagination": _cfg(EIA).pagination.model_copy(update={"page_size": 10})})
    api = _Eia()
    client = httpx.AsyncClient(transport=ReceiptTeeTransport(httpx.MockTransport(api)))
    handler = SeriesApiSourceHandler(cfg, http_client=client)
    log = CaptureLog()
    caplog.set_level(logging.INFO, logger="httpx")
    recs = []
    with capture_scope(log):
        async for r in handler.pull(_ctx(cfg, secrets=_secrets)):
            recs.append(r)
    assert len(recs) == 12 and len(api.requests) == 2       # 12 rows, pages of 10
    assert api.requests[0].url.params["start"] == "2025-01-30"   # first poll: 400 days back
    assert api.requests[0].url.params.get_list("facets[series][]") == ["RBRTE", "RWTC"]
    assert all(SECRET not in r.source_url and "[redacted]" in r.source_url for r in recs)
    assert {c.url.count("[redacted]") for c in log.captures} == {1}
    assert all(SECRET not in c.url for c in log.captures)
    assert all(c.sha256 == hashlib.sha256(c.body).hexdigest() for c in log.captures)
    assert {r.fetch_sha256 for r in recs} == {c.sha256 for c in log.captures}
    assert caplog.records and all(SECRET not in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_a_keyed_source_without_a_resolver_refuses_and_never_calls() -> None:
    cfg = _cfg(EIA)
    api = _Eia()
    handler = SeriesApiSourceHandler(cfg, http_client=httpx.AsyncClient(transport=httpx.MockTransport(api)))
    state = InMemoryStateStore()
    with pytest.raises(SeriesApiAuthNotConfigured):
        async for _ in handler.pull(_ctx(cfg, state=state)):
            pass
    assert api.requests == []
    assert (await handler.health_check(_ctx(cfg, state=state))).state == "unhealthy"


@pytest.mark.asyncio
async def test_a_keyless_csv_source_polls_the_trailing_window_after_the_first() -> None:
    cfg = _cfg(BIS)
    body = (FIX / "bis_ws_cbpol_daily.csv").read_bytes()
    seen: list[httpx.Request] = []

    def bis(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "text/csv;charset=UTF-8"})

    handler = SeriesApiSourceHandler(cfg, http_client=httpx.AsyncClient(transport=httpx.MockTransport(bis)))
    state = InMemoryStateStore()
    first = [r async for r in handler.pull(_ctx(cfg, state=state))]
    second = [r async for r in handler.pull(_ctx(cfg, state=state))]
    assert len(first) == len(second) == 6
    assert seen[0].url.params["startPeriod"] == "2025-01-30" and seen[1].url.params["startPeriod"] == "2026-01-17"
    assert seen[0].url.params["format"] == "csv" and "api_key" not in str(seen[0].url)
    health = await handler.health_check(_ctx(cfg, state=state))
    assert health.state == "healthy" and health.detail["skipped"] == 1


@pytest.mark.asyncio
async def test_an_error_page_degrades_health_and_yields_nothing() -> None:
    cfg = _cfg(BIS)
    handler = SeriesApiSourceHandler(cfg, http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="no"))))
    state = InMemoryStateStore()
    assert [r async for r in handler.pull(_ctx(cfg, state=state))] == []
    assert (await handler.health_check(_ctx(cfg, state=state))).last_error == "HTTP 500"


# ---------------------------------------------------------------------------
# series_change: rule-judged series
# ---------------------------------------------------------------------------


def _pts(values: list[float], start: date = date(2026, 3, 1)) -> list[dict]:
    from datetime import timedelta

    return [{"valid_from": start + timedelta(days=i), "value": v, "id": f"o{i}"} for i, v in enumerate(values)]


def test_a_level_is_an_event_when_entered_or_left_not_while_held() -> None:
    from legba.data.analysts.deterministic_handlers.series_change import parse_level_rule, rule_events

    glob, op, level = parse_level_rule("eia.pet_spot.rbrte>=100")
    assert (glob, op, level) == ("eia.pet_spot.rbrte", ">=", 100.0)
    ev = rule_events(_pts([98, 99.5, 101, 103, 100, 99.9, 100]), levels=[(">=", 100.0)])
    assert [(e["status"], e["cur"]["value"]) for e in ev] == [
        ("level_entered", 101), ("level_left", 99.9), ("level_entered", 100)]
    with pytest.raises(ValueError):
        parse_level_rule("brent above 100")


def test_a_rate_change_is_a_step_event_and_a_flat_rate_is_quiet() -> None:
    from legba.data.analysts.deterministic_handlers.series_change import rule_events

    ev = rule_events(_pts([2.125, 2.125, 1.875, 1.875, 2.0]), step=True)
    assert [(e["status"], e["prev"]["value"], e["cur"]["value"]) for e in ev] == [
        ("step_down", 2.125, 1.875), ("step_up", 1.875, 2.0)]
    assert rule_events(_pts([4.0] * 30), step=True) == []
    assert rule_events(_pts([4.0, 4.05]), step=True, step_min=0.1) == []


def test_the_options_catalog_accepts_the_rules_and_refuses_a_bad_one() -> None:
    from legba.data.option_catalog.handler_options_catalog import HANDLER_OPTIONS

    specs = {s.name: s for s in HANDLER_OPTIONS["series_change"]}
    assert specs["level_rules"].validate(["eia.pet_spot.rbrte>=100"])[0]
    assert not specs["level_rules"].validate(["rbrte >= 100; drop"])[0]
    assert specs["step_series"].validate(["bis.cbpol.*"])[0]
    assert specs["step_min"].validate(0.25)[0] and not specs["step_min"].validate(-1)[0]
    shipped = yaml.safe_load((REPO / "descriptors" / "analyst_series_change.yaml").read_text())
    for name, value in shipped["method"]["options"].items():
        assert specs[name].validate(value)[0], name
