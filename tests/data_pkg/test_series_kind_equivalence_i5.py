# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""I-5 — the EXISTING series and register bindings read byte-for-byte as before.

Lane I-5 widened ``series_api`` (root / indexed row paths, positional rows, a
wider closed date vocabulary, a templated since parameter, a JSON-stat reader,
a period-END window test) and the ``xml_records`` differ (an attribute key and
attribute paths). None of that may move a source that already exists.

The digests below were computed on the PRE-I-5 code (tip 1d5055f7) over the
same inputs and pinned here; the test recomputes them on the current code.
Inputs: the two recorded-shape fixtures the S-1 tests use (EIA JSON, BIS CSV),
two synthetic CSV bodies shaped like the ECB SDMX header X-1 checked, the two
SDN fixtures and a text body; per series descriptor the records (every field
the observation sink writes), the request parameters a poll sends for a fixed
window and page, and the window starts; per register binding the parsed
entries (key, digest, per-field digests, label, summary), the published date
and the diff.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.schemas.source_series_api import SeriesApiConfig
from legba.data.sources.instrument_diff import diff_instruments
from legba.data.sources.instrument_snapshot import InstrumentSnapshotConfig, parse_instrument
from legba.data.sources.series_api import (
    SeriesApiSourceHandler,
    parse_rows,
    records_from_rows,
    window_start,
)
from legba.runtime.source_factory import _unwrap_factory_dict

REPO = Path(__file__).resolve().parents[2]
DESC = REPO / "descriptors"
PROFILE = REPO / "profiles" / "sanctions_export_control" / "descriptors"
FIX = REPO / "tests" / "fixtures"

_ECB_EXR_CSV = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-01,1.0712,A\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-02,,M\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-05,1.0699,A\n"
    "EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2026-10-05,0.8411,A\n"
).encode()
_ECB_FM_CSV = (
    "KEY,FREQ,REF_AREA,CURRENCY,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    "FM.D.U2.EUR.4F.KR.DFR.LEV,D,U2,EUR,2026-09-30,2.5,A\n"
    "FM.D.U2.EUR.4F.KR.DFR.LEV,D,U2,EUR,2026-10-01,2.25,A\n"
    "FM.D.XX.EUR.4F.KR.DFR.LEV,D,XX,EUR,2026-10-01,9.9,A\n"
).encode()

SERIES_CASES: dict[str, tuple[Path, bytes]] = {
    "eia": (DESC / "source_eia_petroleum_spot_daily.yaml", (FIX / "series_api" / "eia_pet_spot_daily.json").read_bytes()),
    "bis": (DESC / "source_bis_policy_rates_daily.yaml", (FIX / "series_api" / "bis_ws_cbpol_daily.csv").read_bytes()),
    "ecb_exr": (DESC / "source_ecb_exr_usd_daily.yaml", _ECB_EXR_CSV),
    "ecb_fm": (DESC / "source_ecb_deposit_rate_daily.yaml", _ECB_FM_CSV),
}

#: sha256 of :func:`_series_view` on the PRE-I-5 code (1d5055f7).
SERIES_GOLDEN: dict[str, str] = {
    "eia": "dd094637a1ec143494e873bc4568e30a650de1d378502b34a6d34a865280fb37",
    "bis": "6159600a747d243a5165a4b07420978e529fd7d4b12a15df34bc99a52bc771ca",
    "ecb_exr": "62bf00ec0b7c8eba392c2ceffc271aa9240f3112745e5c56582049b920be6abe",
    "ecb_fm": "640d89240499bbf1cbc11d34a45a9feb483a0f9dedd1d2f17a1f0d24f52c7424",
}

_SDN_A = (FIX / "instrument_snapshot" / "sdn_a.xml").read_bytes()
_SDN_B = (FIX / "instrument_snapshot" / "sdn_b.xml").read_bytes()

REGISTER_CASES: dict[str, Path] = {
    "ofac_sdn": PROFILE / "source_ofac_sdn_list.yaml",
    "uk_text": PROFILE / "source_uk_sanctions_list.yaml",
    "ecfr_text": PROFILE / "source_ecfr_bis_entity_list.yaml",
    "canada_text": DESC / "source_canada_sema_sanctions_list.yaml",
}

#: sha256 of :func:`_register_view` on the PRE-I-5 code (1d5055f7).
REGISTER_GOLDEN: dict[str, str] = {
    "ofac_sdn": "e1556e5f2e8f1edb1f5d115656a26c10cb9f93baacf6822614cd67f8acd6cf5e",
    "uk_text": "7b5b43b25e30c45a8215b22f0202ef23cd3236cbd977b06395f1c26b2aa80a5b",
    "ecfr_text": "7b5b43b25e30c45a8215b22f0202ef23cd3236cbd977b06395f1c26b2aa80a5b",
    "canada_text": "7b5b43b25e30c45a8215b22f0202ef23cd3236cbd977b06395f1c26b2aa80a5b",
}


def _sha(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cfg(path: Path) -> tuple[dict[str, Any], Any]:
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    return body, _unwrap_factory_dict(body["config"])


def _series_view(path: Path, payload: bytes) -> dict[str, Any]:
    _, raw = _cfg(path)
    cfg = SeriesApiConfig.model_validate(raw)
    rows = parse_rows(payload, cfg)
    views: dict[str, Any] = {"rows": [dict(r) for r in rows]}
    for label, since in (("all", None), ("since", date(2026, 3, 3)), ("since_oct", date(2026, 10, 2))):
        recs, skipped = records_from_rows(rows, cfg, page_url="https://example.invalid/p?x=1",
                                          sha256="c" * 64, since=since)
        views[label] = {"skipped": skipped, "records": [r.model_dump(mode="json") for r in recs]}
    handler = SeriesApiSourceHandler(cfg)
    views["requests"] = [handler._request(date(2026, 10, 1), page, secret)
                         for page in (0, 1) for secret in (None, "s3cr3t")]
    today = date(2026, 11, 14)
    views["windows"] = [window_start(today, cfg, first_poll=True).isoformat(),
                        window_start(today, cfg, first_poll=False).isoformat()]
    return views


def _parsed_view(p: Any) -> dict[str, Any]:
    return {
        "differ": p.differ, "published_date": p.published_date, "declared_count": p.declared_count,
        "duplicate_keys": p.duplicate_keys, "records_without_key": p.records_without_key,
        "entry_count": p.entry_count, "text_lines": p.text_lines,
        "entries": {k: [e.key, e.digest, e.fields, e.label, e.summary] for k, e in sorted(p.entries.items())},
    }


def _register_view(path: Path) -> dict[str, Any]:
    _, raw = _cfg(path)
    cfg = InstrumentSnapshotConfig.model_validate(raw)
    a, b = parse_instrument(_SDN_A, cfg), parse_instrument(_SDN_B, cfg)
    return {"a": _parsed_view(a), "b": _parsed_view(b), "diff": diff_instruments(a, b)}


@pytest.mark.parametrize("name", sorted(SERIES_CASES))
def test_existing_series_sources_read_exactly_as_before(name: str) -> None:
    path, payload = SERIES_CASES[name]
    view = _series_view(path, payload)
    if name == "ecb_exr":
        # The ONE intended change: X-1 wrote the ECB rate as `ratio` before the
        # currency-pair unit existed; I-5 moves it to `fx_quote_per_base`.
        # Everything else about the reading is pinned unchanged.
        units = {r["unit"] for k in ("all", "since", "since_oct") for r in view[k]["records"]}
        assert units == {"fx_quote_per_base"}
        for k in ("all", "since", "since_oct"):
            for r in view[k]["records"]:
                r["unit"] = "ratio"
    assert _sha(view) == SERIES_GOLDEN[name]


@pytest.mark.parametrize("name", sorted(REGISTER_CASES))
def test_existing_register_bindings_read_exactly_as_before(name: str) -> None:
    assert _sha(_register_view(REGISTER_CASES[name])) == REGISTER_GOLDEN[name]
