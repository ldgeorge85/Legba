# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""U-4 — ``GET /api/v1/v3/map/series`` and ``GET /api/v1/v3/map/at_sea``.

The router mounted on a FastAPI app over a fake pool, the two folds, the
degrade-to-``available: false`` rule, and the slim-image guard: the module
ships in the registry image, so it must import with the analyst runtime's
dependency stack poisoned.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.registry._deps import require_bearer
from legba.data.registry.map_layers_api import (
    MAP_LAYERS_ROUTE_VERSION,
    build_map_layers_router,
    fold_at_sea,
    fold_series,
    fold_subject_read,
)

NOW = datetime(2026, 11, 13, 11, 0, tzinfo=timezone.utc)


class _Conn:
    def __init__(self, rows=None, fail=False):
        self.rows = rows or []
        self.fail = fail
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.fail:
            raise RuntimeError('relation "observations" does not exist')
        return self.rows


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def _app(conn) -> FastAPI:
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=_Pool(conn)))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_map_layers_router(deps), prefix="/api/v1/v3")
    app.dependency_overrides[require_bearer] = lambda: "test"
    return app


async def _get(app: FastAPI, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(path)


def _day_rows(subject: str, values: list[float], *, last: date, revised_last: bool = False):
    out = []
    for i, v in enumerate(values):
        d = last - timedelta(days=len(values) - 1 - i)
        out.append({
            "subject": subject, "valid_from": d, "value": Decimal(str(v)), "unit": "vessels",
            "record_time": NOW - timedelta(days=len(values) - i),
            "provenance": {"portname": "Fictional Strait"},
            "vintages": 2 if (revised_last and i == len(values) - 1) else 1,
        })
    return out


def test_the_series_fold_baselines_on_the_days_before_the_latest():
    rows = _day_rows("cp1", [10, 10, 10, 10, 5], last=date(2026, 11, 8), revised_last=True)
    rows += _day_rows("cp2", [40], last=date(2026, 11, 8))
    out = fold_series(rows)
    assert [s.subject for s in out] == ["cp1", "cp2"]
    cp1, cp2 = out
    assert cp1.latest == 5 and cp1.baseline_30d == 10 and cp1.pct_of_baseline == 50.0
    assert cp1.revised is True and cp1.name == "Fictional Strait" and len(cp1.values) == 5
    # one day and no earlier one: no baseline, and never a 100 % it did not earn
    assert cp2.baseline_30d is None and cp2.pct_of_baseline is None and cp2.baseline_days == 0


def test_the_at_sea_fold_drops_rows_without_a_point():
    rows = [
        {"id": "s1", "source_id": "src.a", "fetched_at": NOW, "title": "Attack",
         "published_at": "2026-11-12T13:45:06+00:00",
         "geo": {"lat": 25.9, "lon": 55.7, "at_sea": True, "location_name": "Strait",
                 "inferred": [{"iso2": "AE", "distance_km": 15.6}, {"iso2": "OM"}]}},
        {"id": "s2", "source_id": "src.a", "fetched_at": NOW, "title": "x", "geo": {"at_sea": True}},
        {"id": "s3", "source_id": "src.a", "fetched_at": NOW, "title": "x",
         "geo": '{"lat": 1, "lon": 2, "at_sea": false}'},
    ]
    out = fold_at_sea(rows)
    assert [i.id for i in out] == ["s1"]
    assert [c.iso2 for c in out[0].inferred] == ["AE", "OM"] and out[0].inferred[1].distance_km is None


async def test_the_series_route_answers_and_refuses_a_history_holding():
    conn = _Conn(_day_rows("cp1", [10, 10, 9], last=date(2026, 11, 8)))
    app = _app(conn)
    r = await _get(app, "/api/v1/v3/map/series?collection_id=live:source.x&series_id=x.n_total")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == MAP_LAYERS_ROUTE_VERSION and body["available"] is True
    assert body["unit"] == "vessels" and body["total_subjects"] == 1
    assert body["subjects"][0]["pct_of_baseline"] == 90.0
    # the window anchors on the newest held day and is at least the baseline
    assert conn.calls[0][1] == ("live:source.x", "x.n_total", 30)
    # a curated history holding is not served here
    r = await _get(app, "/api/v1/v3/map/series?collection_id=collection.pilot&series_id=x")
    assert r.status_code == 422


async def test_an_unreadable_store_is_not_an_empty_one():
    app = _app(_Conn(fail=True))
    r = await _get(app, "/api/v1/v3/map/series?collection_id=live:source.x&series_id=x")
    assert r.status_code == 200 and r.json()["available"] is False and r.json()["subjects"] == []
    r = await _get(app, "/api/v1/v3/map/at_sea")
    assert r.status_code == 200 and r.json()["available"] is False and r.json()["incidents"] == []
    r = await _get(_app(_Conn([])), "/api/v1/v3/map/at_sea?days=7&limit=5")
    assert r.json()["available"] is True and r.json()["total"] == 0


async def test_the_at_sea_route_says_when_it_truncated():
    rows = [{"id": f"s{i}", "source_id": "a", "fetched_at": NOW, "title": "t", "published_at": None,
             "geo": {"lat": 1.0, "lon": 2.0, "at_sea": True}} for i in range(3)]
    r = await _get(_app(_Conn(rows)), "/api/v1/v3/map/at_sea?limit=2")
    body = r.json()
    assert body["total"] == 2 and body["truncated"] is True


def test_a_subject_read_folds_like_the_full_findings_read():
    row = {"id": "f1", "title": "t", "target_id": "s1", "analyst_id": "comp", "produced_at": NOW,
           "confidence": Decimal("0.8"), "critic_score": 0.55, "citation_count": 7,
           "verification": {"faithfulness_score": 0.6, "judge_status": "llm", "unsupported_spans": [{"text": "x"}],
                            "claim_verdicts": [1, 2]},
           "structural_score": 0.4, "structural_verification": {"faithfulness_score": 0.1}}
    out = fold_subject_read(row, structural_gate=False)
    assert out.effective_confidence == 0.55 and out.confidence == 0.8 and out.citation_count == 7
    # the per-claim arrays never travel; the scalar leaves do
    assert out.verification == {"faithfulness_score": 0.6, "judge_status": "llm"}
    assert fold_subject_read(row, structural_gate=True).effective_confidence == 0.4
    bare = fold_subject_read({**row, "critic_score": None, "verification": None}, structural_gate=False)
    assert bare.effective_confidence == 0.8 and bare.verification == {"faithfulness_score": 0.1}


async def test_the_subject_reads_route_and_its_degraded_answer():
    rows = [{"id": "f1", "title": "t", "target_id": "s1", "analyst_id": "comp", "produced_at": NOW,
             "confidence": 0.7, "critic_score": None, "citation_count": 2, "verification": None,
             "structural_score": None, "structural_verification": None}]
    conn = _Conn(rows)
    r = await _get(_app(conn), "/api/v1/v3/map/subject_reads?analyst_id=comp&limit=5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True and body["data"][0]["target_id"] == "s1"
    assert conn.calls[0][1] == ("comp", 5)
    r = await _get(_app(_Conn(fail=True)), "/api/v1/v3/map/subject_reads?analyst_id=comp")
    assert r.json()["available"] is False and r.json()["data"] == []


def test_the_router_is_mounted_on_the_real_app():
    text = (pathlib.Path(__file__).resolve().parents[2]
            / "src/legba/data/registry/server.py").read_text(encoding="utf-8")
    assert 'build_map_layers_router(deps), prefix="/api/v1/v3"' in text


_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


def test_the_route_imports_in_the_slim_image() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.map_layers_api as api

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the map route: %r" % leaked
        assert api.build_map_layers_router is not None
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
