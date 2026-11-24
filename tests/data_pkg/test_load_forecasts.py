# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ST0-B — ``scripts/load_forecasts.py``: dry run, load, idempotent re-run,
and the separate ``--seal`` step that seals every question or none.

Fakes only: the store fake answers the store's own SQL
(``_forecast_ledger_fakes.py``), so the loader drives the production store.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from ._forecast_ledger_fakes import FakeLedgerConn

pytestmark = [pytest.mark.asyncio]

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO / "seeds" / "forecasts.example.yaml"
SEAL_AT = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)


def _loader():
    spec = importlib.util.spec_from_file_location("load_forecasts", _REPO / "scripts" / "load_forecasts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _factory(conn):
    async def _f():
        return conn
    return _f


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "forecasts.yaml"
    doc = {"operator_as_of": "2027-02-20T00:00:00Z", "forecasts": rows}
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


def _complete_example(tmp_path: Path) -> Path:
    doc = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    doc["forecasts"][1]["operator_probability"] = 0.4
    p = tmp_path / "forecasts.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


async def test_the_example_parses_two_fictional_rows_and_the_preflight_names_the_owed_number():
    lf = _loader()
    code, out = await lf.run(_factory(None), _EXAMPLE, dry_run=True, do_seal=False)
    assert code == 0
    assert [r["slug"] for r in out["valid_rows"]] == ["FX1-klarvia-port-closure", "FX2-widget-index-threshold"]
    assert out["errors"] == []
    assert out["seal_preflight"] == ["FX2-widget-index-threshold: operator_probability is owed"]


async def test_load_then_rerun_writes_nothing_new():
    lf = _loader()
    conn = FakeLedgerConn()
    code, out = await lf.run(_factory(conn), _EXAMPLE, dry_run=False, do_seal=False)
    assert code == 0
    assert out["questions"] == {"inserted": 2, "updated": 0, "unchanged": 0}
    assert out["operator_numbers"] == {"inserted": 1, "updated": 0, "unchanged": 0}
    assert out["owed"] == ["FX2-widget-index-threshold"]
    code, again = await lf.run(_factory(conn), _EXAMPLE, dry_run=False, do_seal=False)
    assert code == 0
    assert again["questions"] == {"inserted": 0, "updated": 0, "unchanged": 2}
    assert again["operator_numbers"] == {"inserted": 0, "updated": 0, "unchanged": 1}
    assert len(conn.questions) == 2 and len(conn.numbers) == 1


async def test_the_seal_refuses_everything_while_one_number_is_owed(tmp_path):
    lf = _loader()
    conn = FakeLedgerConn()
    await lf.run(_factory(conn), _EXAMPLE, dry_run=False, do_seal=False)
    code, out = await lf.run(_factory(conn), _EXAMPLE, dry_run=False, do_seal=True, at=SEAL_AT)
    assert code == 1 and out["refused"] is True and out["sealed"] == []
    assert any("no operator number" in p for p in out["problems"])
    assert all(q["sealed_at"] is None for q in conn.questions.values())


async def test_the_seal_refuses_a_placeholder_and_an_unloaded_question(tmp_path):
    lf = _loader()
    conn = FakeLedgerConn()
    doc = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    doc["forecasts"][0]["condition"] = "<OPERATOR TO WRITE>"
    doc["forecasts"][1]["operator_probability"] = 0.4
    path = _write(tmp_path, doc["forecasts"][:1])
    await lf.run(_factory(conn), path, dry_run=False, do_seal=False)
    path = _write(tmp_path, doc["forecasts"])
    code, out = await lf.run(_factory(conn), path, dry_run=False, do_seal=True, at=SEAL_AT)
    assert code == 1
    assert any("placeholder" in p for p in out["problems"])
    assert any(p == "FX2-widget-index-threshold: not loaded" for p in out["problems"])


async def test_seal_dry_run_reports_and_seals_nothing_then_the_seal_seals_all(tmp_path):
    lf = _loader()
    conn = FakeLedgerConn()
    path = _complete_example(tmp_path)
    await lf.run(_factory(conn), path, dry_run=False, do_seal=False)
    code, out = await lf.run(_factory(conn), path, dry_run=True, do_seal=True, at=SEAL_AT)
    assert code == 0 and out["problems"] == [] and out["sealed"] == []
    assert all(q["sealed_at"] is None for q in conn.questions.values())
    code, out = await lf.run(_factory(conn), path, dry_run=False, do_seal=True, at=SEAL_AT)
    assert code == 0 and sorted(out["sealed"]) == ["FX1-klarvia-port-closure", "FX2-widget-index-threshold"]
    assert all(q["sealed_at"] == SEAL_AT for q in conn.questions.values())
    assert all(n["sealed_at"] == SEAL_AT for n in conn.numbers.values())
    # A later change to a sealed question is refused and named; the rest stay.
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["forecasts"][1]["operator_probability"] = 0.6
    code, out = await lf.run(_factory(conn), _write(tmp_path, doc["forecasts"]), dry_run=False, do_seal=False)
    assert code == 1
    assert any("sealed" in e for e in out["errors"])


async def test_a_number_without_its_as_of_is_refused(tmp_path):
    from legba.data.forecast_ledger import parse_forecasts_yaml

    doc = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    p = tmp_path / "f.yaml"
    p.write_text(yaml.safe_dump({"forecasts": doc["forecasts"]}), encoding="utf-8")
    entries, errors = parse_forecasts_yaml(p)
    assert [e.question.slug for e in entries] == ["FX2-widget-index-threshold"]  # owed: no as_of needed
    assert "operator_as_of" in errors[0]


async def test_a_missing_file_is_a_supported_state():
    lf = _loader()
    code, out = await lf.run(_factory(None), Path("/nonexistent/forecasts.yaml"), dry_run=False, do_seal=False)
    assert code == 0 and "no seed file" in out["note"]


async def test_a_bad_row_is_reported_and_skipped(tmp_path):
    lf = _loader()
    doc = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    doc["forecasts"][0]["resolver_kind"] = "vibes"
    code, out = await lf.run(_factory(None), _write(tmp_path, doc["forecasts"]), dry_run=True, do_seal=False)
    assert code == 1
    assert len(out["valid_rows"]) == 1
    assert out["errors"] and "vibes" in out["errors"][0]
