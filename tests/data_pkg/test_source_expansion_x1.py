# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The source-expansion descriptors: shape, state, licence, Telegram.

* Every descriptor validates through the real ``SourceDescriptor`` model
  AND its handler's config schema (the production unwrap), ships ``draft``,
  and the reference host's overlay walks it to ``configured`` — never active.
* The licence ledger is never left unset; an attribution class carries a
  credit line; a non-commercial class records its condition in the header the
  way the PortWatch descriptor does.
* The register bindings pin the config licence to the scope licence and use a
  retainable class.
* The two ECB series parse synthetic rows shaped like the checked CSV header
  (no payload from the real endpoint is committed or fetched) into the series
  the sink declares.
* The staged Telegram batch rides the live monitor's account (the same three
  vault refs), reads text only, overlaps no live channel, and is pinned at
  ``configured``: one active ``telegram_channel`` descriptor per session.
"""

from __future__ import annotations

from tests._lifecycle_rows import lifecycle_text
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.schemas.source import SourceDescriptor
from legba.data.schemas.source_series_api import SeriesApiConfig
from legba.data.sources.instrument_snapshot import (
    RETENTION_FORBIDDEN_LICENSE_CLASSES,
    InstrumentSnapshotConfig,
)
from legba.data.sources.rss import RSSConfig
from legba.data.sources.series_api import records_from_rows
from legba.data.sources.telegram import TelegramChannelSourceConfig
from legba.runtime.source_factory import _unwrap_factory_dict

_ROOT = Path(__file__).resolve().parents[2]
_DESCRIPTORS = _ROOT / "descriptors"

X1_SOURCES: dict[str, str] = {
    "source_novayagazeta_europe.yaml": "rss",
    "source_radiotamazuj.yaml": "rss",
    "source_ocmedia.yaml": "rss",
    "source_ukmod_govuk.yaml": "rss",
    "source_ukofsi_govuk.yaml": "rss",
    "source_eucommission_press.yaml": "rss",
    "source_ecb_press.yaml": "rss",
    "source_federalreserve_speeches.yaml": "rss",
    "source_ecdc_threats.yaml": "rss",
    "source_ecdc_news.yaml": "rss",
    "source_ecb_exr_usd_daily.yaml": "series_api",
    "source_ecb_deposit_rate_daily.yaml": "series_api",
    "source_eu_fsf_sanctions_list.yaml": "instrument_snapshot",
    "source_canada_sema_sanctions_list.yaml": "instrument_snapshot",
    "source_telegram_x1_staged.yaml": "telegram_channel",
}
_CONFIG_SCHEMA = {
    "rss": RSSConfig,
    "series_api": SeriesApiConfig,
    "instrument_snapshot": InstrumentSnapshotConfig,
    "telegram_channel": TelegramChannelSourceConfig,
}
STAGED_TELEGRAM = "source_telegram_x1_staged.yaml"
LIVE_TELEGRAM = "source_telegram_monitor.yaml"


def _body(name: str) -> dict[str, Any]:
    body = yaml.safe_load((_DESCRIPTORS / name).read_text())
    body["identity"]["version"] = "0" * 16
    return body


def _desc(name: str) -> SourceDescriptor:
    return SourceDescriptor.model_validate(_body(name), strict=False)


def _lifecycle_rows() -> dict[str, tuple[str, str]]:
    """``{source id: (host state, file ships)}`` from the reference overlay."""
    return {
        m.group(1): (m.group(2), m.group(3))
        for m in re.finditer(
            r"^\| source \| (\S+) \| (\w+) \| (\w+) \|$",
            lifecycle_text(_DESCRIPTORS), re.M,
        )
    }


# ---------------------------------------------------------------------------
# Every descriptor: schema, handler config, state, licence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,kind", sorted(X1_SOURCES.items()))
def test_every_x1_descriptor_validates_ships_draft_and_walks_to_configured(name, kind):
    body = _body(name)
    d = SourceDescriptor.model_validate(body, strict=False)
    assert d.identity.kind == kind
    _CONFIG_SCHEMA[kind].model_validate(_unwrap_factory_dict(body["config"]))
    assert d.identity.state.value == "draft"
    assert d.acquisition == "poll" and d.cadence is not None and d.cadence.schedule is not None
    assert d.scope.access_class == "public"
    assert _lifecycle_rows().get(d.identity.id) == ("configured", "draft"), d.identity.id


@pytest.mark.parametrize("name", sorted(n for n in X1_SOURCES if n != STAGED_TELEGRAM))
def test_licence_is_declared_and_its_obligation_recorded(name):
    d = _desc(name)
    lic = d.scope.license_class
    assert lic is not None                                  # the ledger is never left unset
    if lic in ("open_gov_attribution", "cc_by", "cc_nc"):
        assert d.scope.attribution                          # the licence requires credit
    if lic == "cc_nc":
        # A non-commercial licence is an obligation, not a read restriction.
        assert d.scope.access_class == "public"


def test_the_rss_heads_keep_the_default_first_poll_age_cap():
    for name, kind in X1_SOURCES.items():
        if kind == "rss":
            cfg = RSSConfig.model_validate(_unwrap_factory_dict(_body(name)["config"]))
            assert cfg.first_poll_max_entry_age_days is None, name   # None = the 30-day default


@pytest.mark.parametrize(
    "name", ["source_eu_fsf_sanctions_list.yaml", "source_canada_sema_sanctions_list.yaml"]
)
def test_register_bindings_pin_a_retainable_licence_pair(name):
    body = _body(name)
    d = SourceDescriptor.model_validate(body, strict=False)
    cfg = InstrumentSnapshotConfig.model_validate(_unwrap_factory_dict(body["config"]))
    assert cfg.license_class == d.scope.license_class
    assert cfg.license_class not in RETENTION_FORBIDDEN_LICENSE_CLASSES
    if name == "source_eu_fsf_sanctions_list.yaml":
        # The record differ reads an attribute key; FSF is keyed on its own id.
        assert (cfg.differ, cfg.record_tag, cfg.key_path) == ("xml_records", "sanctionEntity", "@logicalId")
    else:
        assert cfg.differ == "text"             # the Canada entry element was never probed
    assert d.scope.source_class == "official"


# ---------------------------------------------------------------------------
# The ECB series on synthetic rows shaped like the checked CSV header
# ---------------------------------------------------------------------------


def _series_cfg(name: str) -> SeriesApiConfig:
    return SeriesApiConfig.model_validate(_unwrap_factory_dict(_body(name)["config"]))


def test_ecb_exr_rows_become_one_dated_ratio_per_day():
    cfg = _series_cfg("source_ecb_exr_usd_daily.yaml")
    rows = [
        {"KEY": "EXR.D.USD.EUR.SP00.A", "FREQ": "D", "CURRENCY": "USD", "CURRENCY_DENOM": "EUR",
         "EXR_TYPE": "SP00", "EXR_SUFFIX": "A", "TIME_PERIOD": "2026-11-12",
         "OBS_VALUE": "1.0841", "OBS_STATUS": "A"},
        {"KEY": "EXR.D.USD.EUR.SP00.A", "FREQ": "D", "CURRENCY": "USD", "CURRENCY_DENOM": "EUR",
         "EXR_TYPE": "SP00", "EXR_SUFFIX": "A", "TIME_PERIOD": "2026-11-13",
         "OBS_VALUE": "", "OBS_STATUS": "M"},               # missing stays absent
    ]
    recs, skipped = records_from_rows(rows, cfg, page_url="https://example.invalid/x", sha256="0" * 64)
    assert skipped == 1
    assert [(r.series_id, r.subject, r.subject_kind, r.unit, r.valid_from, str(r.value))
            for r in recs] == [
        ("ecb.exr.reference_rate", "USD", "entity", "fx_quote_per_base", date(2026, 11, 12), "1.0841")]
    assert recs[0].provenance["OBS_STATUS"] == "A"


def test_ecb_deposit_rate_rows_are_the_euro_area_percent():
    cfg = _series_cfg("source_ecb_deposit_rate_daily.yaml")
    rows = [
        {"KEY": "FM.D.U2.EUR.4F.KR.DFR.LEV", "FREQ": "D", "REF_AREA": "U2", "CURRENCY": "EUR",
         "TIME_PERIOD": "2026-11-14", "OBS_VALUE": "2.5", "OBS_STATUS": "A"},
        {"KEY": "FM.D.XX.EUR.4F.KR.DFR.LEV", "FREQ": "D", "REF_AREA": "XX", "CURRENCY": "EUR",
         "TIME_PERIOD": "2026-11-14", "OBS_VALUE": "9.9", "OBS_STATUS": "A"},  # filtered by where
    ]
    recs, _ = records_from_rows(rows, cfg, page_url="https://example.invalid/x", sha256="0" * 64)
    assert [(r.series_id, r.subject, r.unit, str(r.value)) for r in recs] == [
        ("ecb.fm.deposit_facility_rate", "XM", "percent", "2.5")]


def test_ecb_series_ask_only_for_the_parameters_the_review_checked():
    for name in ("source_ecb_exr_usd_daily.yaml", "source_ecb_deposit_rate_daily.yaml"):
        cfg = _series_cfg(name)
        assert cfg.auth is None and cfg.since_param is None
        assert {k for k, _ in cfg.params} == {"format", "lastNObservations"}
        assert cfg.format == "csv"


# ---------------------------------------------------------------------------
# Telegram: the staged batch rides the live account and never goes live itself
# ---------------------------------------------------------------------------


def _tg(name: str) -> TelegramChannelSourceConfig:
    return TelegramChannelSourceConfig.model_validate(_unwrap_factory_dict(_body(name)["config"]))


#: The ten channels, moved into the live descriptor on 2026-11-14
#: with their reviewed classes; the staged file holds none.
X1_MOVED_CLASSES: dict[str, str | None] = {
    "GeneralStaffZSU": "official", "kpszsu": "official", "mod_russia": "official",
    "medvedev_telegram": "official", "idfofficial": "official", "SyPresidency": "official",
    "DeepStateUA": "analysis",
    "suspilnenews": None, "zerkalo_io": None, "agentstvonews": None,   # batch default: reporting
}


def test_staged_telegram_rides_the_live_account_text_only():
    staged, live = _body(STAGED_TELEGRAM), _body(LIVE_TELEGRAM)
    for key in ("api_id_secret", "api_hash_secret", "session_secret"):
        assert staged["config"][key] == live["config"][key]
    assert staged["deps"]["vault_secrets"] == live["deps"]["vault_secrets"]
    cfg = _tg(STAGED_TELEGRAM)
    assert cfg.include_media is False
    assert cfg.channels == [] and cfg.classes == {}      # all ten moved
    # No handle is both staged and live (moving one means removing it here).
    norm = lambda xs: {c.lstrip("@").lower() for c in xs}  # noqa: E731
    assert not norm(cfg.channels) & norm(_tg(LIVE_TELEGRAM).channels)


def test_the_moved_channels_ride_the_live_descriptor_with_their_reviewed_classes():
    cfg = _tg(LIVE_TELEGRAM)
    assert len(cfg.channels) == 28 + 10
    assert len(set(cfg.channels)) == len(cfg.channels)
    for handle, cls in X1_MOVED_CLASSES.items():
        assert handle in cfg.channels, handle
        assert cfg.classes.get(handle) == cls, handle
    assert cfg.include_media is False
    assert _desc(LIVE_TELEGRAM).scope.source_class == "reporting"


def test_one_running_telegram_descriptor_per_session():
    """A second concurrent client on one Telegram session kills the session.
    Every telegram_channel descriptor on the session must stay at draft or
    configured on the reference host, except one."""
    rows = _lifecycle_rows()
    running: list[str] = []
    for path in sorted(_DESCRIPTORS.glob("source_*.yaml")):
        body = yaml.safe_load(path.read_text())
        if body["identity"]["kind"] != "telegram_channel":
            continue
        if body["config"]["session_secret"]["raw"] != "source.telegram.session":
            continue
        sid = body["identity"]["id"]
        state = rows.get(sid, (body["identity"]["state"], ""))[0]
        if state in ("active", "paused"):
            running.append(sid)
    assert running == ["source.telegram.org_channels"]
    assert rows["source.telegram.x1_staged_channels"][0] == "configured"
