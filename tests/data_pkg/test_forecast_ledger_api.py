# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /api/v1/v3/ledger/forecasts``.

The router mounted on a FastAPI app over the store fake (the production
store functions answer it), plus the slim-image guard: the route ships in
the registry image, so it and the leaf store must import with the analyst
runtime's dependency stack poisoned.
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

from legba.data import forecast_ledger as fl
from legba.data.registry._deps import require_bearer
from legba.data.registry.forecast_ledger_api import (
    FORECAST_LEDGER_ROUTE_VERSION,
    build_forecast_ledger_router,
)

from ._forecast_ledger_fakes import FakeLedgerConn, FakePool

# asyncio_mode = "auto" (pyproject) runs the async tests.

_ROUTE = "/api/v1/v3/ledger/forecasts"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _app(pool) -> FastAPI:
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_forecast_ledger_router(deps), prefix="/api/v1/v3")
    app.dependency_overrides[require_bearer] = lambda: "test"
    return app


async def _get(app: FastAPI, path: str = _ROUTE):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(path)


def _q(slug: str, due: datetime) -> fl.ForecastQuestion:
    return fl.ForecastQuestion(
        slug=slug, question=f"Fictional question {slug}", resolver_kind="official_result",
        resolver_spec={"authority": "Fictional CEC", "outcome": "X wins"}, due_at=due,
    )


async def _seeded() -> FakeLedgerConn:
    conn = FakeLedgerConn()
    _, late = await fl.upsert_question(conn, _q("FX2-late", NOW + timedelta(days=40)))
    _, soon = await fl.upsert_question(conn, _q("FX1-soon", NOW + timedelta(days=10)))
    _, gone = await fl.upsert_question(conn, _q("FX0-gone", NOW + timedelta(days=1)))
    for qid, p in ((late, 0.2), (soon, 0.7)):
        await fl.state_number(conn, qid, fl.ForecastNumber("operator", "operator", p, NOW))
    await fl.state_number(conn, soon, fl.ForecastNumber(
        "tower", "tower", None, NOW, absent_reason="no scheduled analyst emitted a probability"))
    await fl.state_number(conn, soon, fl.ForecastNumber(
        "frontier_arm", "llm.anthropic.opus_4_7", 0.55, NOW, rationale="short"))
    await fl.seal_question(conn, soon, at=NOW)
    await fl.resolve(conn, gone, outcome="void", resolved_by="operator:1", now=NOW,
                     early=True, evidence_refs=["url:https://example.org/cancelled"])
    return conn


async def test_the_ledger_reads_open_first_by_due_with_every_issuer_and_absence():
    r = await _get(_app(FakePool(await _seeded())))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == FORECAST_LEDGER_ROUTE_VERSION and body["available"] is True
    assert body["resolver_kinds"] == list(fl.RESOLVER_KINDS)
    slugs = [f["slug"] for f in body["forecasts"]]
    assert slugs == ["FX1-soon", "FX2-late", "FX0-gone"]
    soon = body["forecasts"][0]
    assert soon["sealed_at"] is not None
    assert [(n["issuer_kind"], n["p"]) for n in soon["numbers"]] == [
        ("operator", 0.7), ("tower", None), ("frontier_arm", 0.55)]
    assert soon["numbers"][1]["absent_reason"].startswith("no scheduled analyst")
    assert body["forecasts"][2]["resolution"]["outcome"] == "void"


async def test_the_status_filter():
    app = _app(FakePool(await _seeded()))
    r = await _get(app, _ROUTE + "?status=open")
    assert [f["slug"] for f in r.json()["forecasts"]] == ["FX1-soon", "FX2-late"]
    assert (await _get(app, _ROUTE + "?status=bogus")).status_code == 422


async def test_an_empty_ledger_and_an_unreadable_one_are_two_answers():
    r = await _get(_app(FakePool(FakeLedgerConn())))
    assert r.json()["forecasts"] == [] and r.json()["available"] is True

    class _Missing(FakeLedgerConn):
        async def fetch(self, sql, *a):
            raise RuntimeError('relation "forecast_questions" does not exist')

    r = await _get(_app(FakePool(_Missing())))
    assert r.status_code == 200
    assert r.json()["forecasts"] == [] and r.json()["available"] is False


async def test_the_route_is_mounted_on_the_real_app():
    text = (pathlib.Path(__file__).resolve().parents[2]
            / "src/legba/data/registry/server.py").read_text(encoding="utf-8")
    assert "build_forecast_ledger_router(deps), prefix=\"/api/v1/v3\"" in text


_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


def test_the_route_and_its_store_import_in_the_slim_image() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.forecast_ledger_api as api
        import legba.data.forecast_ledger as store

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the ledger route: %r" % leaked
        assert api.build_forecast_ledger_router is not None
        assert store.fetch_ledger is not None
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
