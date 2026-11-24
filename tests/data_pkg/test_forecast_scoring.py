# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ST1-A/B — Brier scoring and ``GET /api/v1/v3/ledger/scorecard``.

Hand-computed Brier cases; the single-arm and void rules; the payload before
any resolution (useful and honest: numbers, spread, due dates, "0 resolved")
and after; the minimum-sample constants reused, not re-typed; the route on a
FastAPI app over the store fakes; and the slim-image import guard.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data import calibration_honesty as ch
from legba.data import forecast_ledger as fl
from legba.data import forecast_resolution as fr
from legba.data import forecast_scoring as fs
from legba.data.registry._deps import require_bearer
from legba.data.registry.forecast_scorecard_api import build_forecast_scorecard_router

from ._forecast_ledger_fakes import FakePool
from ._forecast_resolution_fakes import FakeResolutionConn

SEALED = datetime(2026, 3, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 3, 2, tzinfo=timezone.utc)


def test_brier_is_squared_error_and_void_is_none():
    assert fs.brier(0.7, "yes") == pytest.approx(0.09)
    assert fs.brier(0.7, "no") == pytest.approx(0.49)
    assert fs.brier(0.5, "yes") == 0.25
    assert fs.brier(0.2, "void") is None and fs.brier(None, "yes") is None


def test_the_thresholds_are_the_shared_constants():
    assert fs.CALIBRATION_BINS_MIN_SAMPLE == ch.ACUTE_PILOT_MIN_SAMPLE
    src = pathlib.Path(fs.__file__).read_text()
    assert "30" not in src.replace("2026", "")      # never hard-coded again (W11-F189)


def _row(slug, due, numbers, outcome=None, *, spec=None, exogenous=True):
    return {
        "id": f"id-{slug}", "slug": slug, "question": f"Fictional {slug}", "status":
            {"yes": "resolved", "no": "resolved", "void": "void"}.get(outcome, "open"),
        "resolver_kind": "event_reported", "resolver_spec": spec or {}, "due_at": due.isoformat(),
        "grace_hours": 72, "overdue": False, "sealed_at": SEALED.isoformat(), "exogenous": exogenous,
        "numbers": [{"issuer_kind": k, "issuer_ref": k, "p": p, "absent_reason": None if p is not None else "none"}
                    for k, p in numbers],
        "resolution": None if outcome is None else {"id": f"r-{slug}", "outcome": outcome, "note": "n",
                                                     "evidence_refs": ["url:https://example.org/a"]},
    }


def test_before_any_resolution_the_scorecard_is_still_useful_and_honest():
    d = NOW + timedelta(days=10)
    rows = [
        _row("FX1", d, [("operator", 0.85), ("frontier_arm", 0.88), ("clef_arm", 0.968), ("tower", None)]),
        _row("FX2", d + timedelta(days=5), [("operator", 0.75)]),     # single arm
    ]
    sc = fs.build_scorecard(rows, {}, now=NOW)
    h = sc["header"]
    assert h["n_resolved"] == 0 and h["n_sealed"] == 2 and h["skill_claim"] is False
    assert h["statement"] == "0 resolved; scores appear at 5."
    assert sc["scores_withheld"] is True and sc["scores_min_resolved"] == fs.SCORES_MIN_RESOLVED == 5
    q1, q2 = sc["questions"]
    assert q1["spread"] == {"n": 3, "min": 0.85, "max": 0.968, "range": 0.118}
    assert q2["single_arm"] is True and q2["spread"] is None
    assert all(n["brier"] is None for n in q1["numbers"])
    assert [a["issuer_kind"] for a in sc["arms"]] == ["operator", "frontier_arm", "clef_arm"]
    assert all(a["mean_brier"] is None and a["note"] == "scores appear at 5 resolved"
               for a in sc["arms"])
    assert sc["calibration"]["available"] is False
    assert sc["calibration"]["reason"] == f"n too small (0 < {ch.ACUTE_PILOT_MIN_SAMPLE})"
    assert [x["slug"] for x in sc["due_next"]] == ["FX1", "FX2"]
    assert sc["single_arm"] == [{"slug": "FX2", "issuer_kind": "operator", "p": 0.75, "brier": None,
                                 "outcome": None}]
    assert "legacy calibration" in sc["separation_note"]


def _resolved_rows(extra_multi: int):
    rows = [
        _row("FA", NOW, [("operator", 0.8), ("clef_arm", 0.6)], "yes"),
        _row("FB", NOW, [("operator", 0.3), ("clef_arm", 0.1)], "no"),
        _row("FC", NOW, [("operator", 0.4), ("clef_arm", 0.9)], "void"),
        _row("FD", NOW, [("operator", 0.75)], "yes"),
        _row("FE", NOW, [("operator", 0.5), ("clef_arm", 0.5)], None, exogenous=False),
    ]
    rows += [_row(f"FX{i}", NOW, [("operator", 0.5), ("clef_arm", 0.5)], "yes") for i in range(extra_multi)]
    return rows


def _no_score_anywhere(sc) -> bool:
    nums = [n["brier"] for q in sc["questions"] for n in q["numbers"]]
    nums += [b["brier"] for q in sc["questions"] for b in q["baselines"]]
    nums += [a[k] for a in sc["arms"] for k in ("mean_brier", "baseline_mean_brier", "skill_vs_climatology")]
    nums += [x["brier"] for x in sc["single_arm"]]
    return all(v is None for v in nums)


def test_below_five_resolved_no_brier_figure_appears_anywhere():
    sc = fs.build_scorecard(_resolved_rows(1), {}, now=NOW)       # FA, FB, FD, FX0 = 4 resolved
    h = sc["header"]
    assert (h["n_questions"], h["n_resolved"], h["n_void"]) == (5, 4, 1)   # endogenous never counted
    assert sc["scores_withheld"] is True and h["statement"] == "4 resolved; scores appear at 5."
    assert _no_score_anywhere(sc)
    fa = sc["questions"][0]
    assert fa["outcome"] == "yes" and fa["resolution"]["evidence_refs"] == ["url:https://example.org/a"]
    assert [n["p"] for n in fa["numbers"]] == [0.8, 0.6] and fa["spread"]["range"] == 0.2
    assert sc["void_questions"] == [{"slug": "FC", "reason": "n"}]


def test_from_five_resolved_brier_per_number_per_arm_with_n_void_excluded_single_arm_apart():
    sc = fs.build_scorecard(_resolved_rows(3), {}, now=NOW)       # 6 resolved, 5 multi-arm
    assert sc["scores_withheld"] is False and sc["header"]["skill_claim"] is True
    fa = sc["questions"][0]
    assert [n["brier"] for n in fa["numbers"]] == [pytest.approx(0.04), pytest.approx(0.16)]
    assert fa["baselines"] == [{"name": "climatological", "p": 0.5, "brier": 0.25}]
    arms = {a["issuer_kind"]: a for a in sc["arms"]}
    # operator (0.2^2 + 0.3^2 + 3 x 0.5^2) / 5 = 0.176 ; clef (0.4^2 + 0.1^2 + 3 x 0.25) / 5 = 0.184
    assert arms["operator"]["n"] == 5 and arms["operator"]["mean_brier"] == pytest.approx(0.176)
    assert arms["clef_arm"]["mean_brier"] == pytest.approx(0.184)
    assert arms["operator"]["baseline_mean_brier"] == 0.25
    assert arms["operator"]["skill_vs_climatology"] == pytest.approx(1 - 0.176 / 0.25)
    assert sc["single_arm"] == [{"slug": "FD", "issuer_kind": "operator", "p": 0.75,
                                 "brier": pytest.approx(0.0625), "outcome": "yes"}]
    assert next(q for q in sc["questions"] if q["slug"] == "FC")["numbers"][0]["brier"] is None
    assert sc["calibration"]["available"] is False                # bins wait for their own minimum


def test_skill_and_a_declared_base_rate_appear_at_the_minimum_sample():
    rows = [_row(f"F{i}", NOW, [("operator", 0.9), ("clef_arm", 0.6)], "yes", spec={"base_rate": 0.3})
            for i in range(fs.SCORES_MIN_RESOLVED)]
    sc = fs.build_scorecard(rows, {}, now=NOW)
    op = next(a for a in sc["arms"] if a["issuer_kind"] == "operator")
    assert op["skill_vs_climatology"] == pytest.approx(1 - 0.01 / 0.25)
    assert sc["header"]["skill_claim"] is True
    assert sc["questions"][0]["baselines"][1] == {"name": "base_rate", "p": 0.3, "brier": pytest.approx(0.49)}


def _app(pool) -> FastAPI:
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_forecast_scorecard_router(deps), prefix="/api/v1/v3")
    app.dependency_overrides[require_bearer] = lambda: "test"
    return app


async def _get(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get("/api/v1/v3/ledger/scorecard")


async def _sealed_conn() -> tuple[FakeResolutionConn, str]:
    conn = FakeResolutionConn()
    q = fl.ForecastQuestion(slug="FX9", question="A fictional series", resolver_kind="series_threshold",
                            resolver_spec={"source": "F", "series": "S", "op": ">=", "value": 1, "on": "2026-03-05"},
                            due_at=datetime(2026, 3, 5, tzinfo=timezone.utc), grace_hours=0)
    _, qid = await fl.upsert_question(conn, q)
    for kind, p in (("operator", 0.7), ("frontier_arm", 0.4)):
        await fl.state_number(conn, qid, fl.ForecastNumber(kind, kind, p, SEALED))
    await fl.seal_question(conn, qid, at=SEALED)
    return conn, qid


async def test_the_route_before_and_after_a_resolution():
    conn, qid = await _sealed_conn()
    r = await _get(_app(FakePool(conn)))
    body = r.json()
    assert r.status_code == 200 and body["available"] is True and body["records_available"] is True
    assert body["header"]["n_resolved"] == 0 and body["questions"][0]["spread"]["range"] == 0.3
    conn.observations.append({"id": "o1", "series_id": "f.s", "valid_from": "2026-03-05", "value": 2.0,
                              "unit": "x", "source_url": "u", "sha256": "c" * 64, "record_time": "2026-03-05"})
    await fr.run_resolution_job(conn, now=datetime(2026, 3, 6, tzinfo=timezone.utc))
    body = (await _get(_app(FakePool(conn)))).json()
    q = body["questions"][0]
    assert q["outcome"] == "yes" and q["resolution"]["evidence_refs"] == ["observation:o1"]
    assert [n["brier"] for n in q["numbers"]] == [None, None] and body["scores_withheld"] is True
    assert q["latest_act"]["record_kind"] == "confirmation" and q["proposal"]["outcome"] == "yes"
    assert body["header"]["n_resolved"] == 1


async def test_the_route_degrades_honestly():
    class _NoRecords(FakeResolutionConn):
        async def fetch(self, sql, *a):
            if sql == fr.LATEST_RECORDS_FOR_SQL:
                raise RuntimeError('relation "forecast_resolution_records" does not exist')
            return await super().fetch(sql, *a)

    conn, _ = await _sealed_conn()
    conn.__class__ = _NoRecords
    body = (await _get(_app(FakePool(conn)))).json()
    assert body["available"] is True and body["records_available"] is False

    class _NoLedger(FakeResolutionConn):
        async def fetch(self, sql, *a):
            raise RuntimeError("down")

    body = (await _get(_app(FakePool(_NoLedger())))).json()
    assert body["available"] is False and body["questions"] == [] and body["scores_withheld"] is True


async def test_the_route_is_mounted_and_bearer_gated(monkeypatch):
    text = (pathlib.Path(__file__).resolve().parents[2] / "src/legba/data/registry/server.py").read_text()
    assert 'build_forecast_scorecard_router(deps), prefix="/api/v1/v3"' in text
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=FakePool(FakeResolutionConn())))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_forecast_scorecard_router(deps), prefix="/api/v1/v3")
    # tests/conftest.py turns dev mode on suite-wide (any bearer accepted when
    # no token is configured); the gate is only observable with it OFF.
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    r = await _get(app)
    assert r.status_code == 503                    # no token configured, no dev mode: closed
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", "t-scorecard")
    r = await _get(app)
    assert r.status_code == 401                    # no bearer: refused, never served
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/v1/v3/ledger/scorecard", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 403
        r = await c.get("/api/v1/v3/ledger/scorecard", headers={"Authorization": "Bearer t-scorecard"})
        assert r.status_code == 200                # mounted: the right bearer is served


_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


def test_the_scorecard_route_and_its_stores_import_in_the_slim_image() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.forecast_scorecard_api as api
        import legba.data.forecast_resolution, legba.data.forecast_scoring
        import legba.data.forecast_resolvers, legba.data.forecast_evidence

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
            or m.startswith("legba.data.alerts")
        )
        assert not leaked, "analyst/runtime reachable from the scorecard route: %r" % leaked
        assert api.build_forecast_scorecard_router is not None
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0 and "OK" in result.stdout, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}")


def test_reliability_bins_appear_per_arm_only_at_the_calibration_minimum():
    n = fs.CALIBRATION_BINS_MIN_SAMPLE
    rows = [_row(f"F{i}", NOW, [("operator", 0.72), ("clef_arm", 0.3)], "yes" if i % 4 else "no")
            for i in range(n - 1)]
    assert fs.build_scorecard(rows, {}, now=NOW)["calibration"]["available"] is False
    rows.append(_row("Flast", NOW, [("operator", 0.72), ("clef_arm", 0.3)], "yes"))
    cal = fs.build_scorecard(rows, {}, now=NOW)["calibration"]
    assert cal["available"] is True and cal["bins_arm"] == "operator"
    seven = next(b for b in cal["bins"] if b["lo"] == 0.7)
    assert seven["n"] == n and seven["mean_p"] == pytest.approx(0.72)
    assert sum(b["n"] for b in cal["bins"]) == n                 # one arm's numbers, never pooled
