# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E-3 — ``GET /api/v1/v3/eval/runs``, the published evaluation record.

The router over the store fake (the production store functions answer it):
every version listed, a correction's earlier version ``superseded``, a
withdrawn version listed with its reason (never dropped); nothing published
is 200 with ``runs: []``; an unreadable table is 200 ``available: false``.
Plus the gate: bearer-gated (``require_bearer``), the ``read`` role, NOT a
public route; mounted on the real app; importable in the slim image.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data import eval_runs as er
from legba.data.registry import route_roles as rr
from legba.data.registry._deps import DEV_MODE_ENV, require_bearer
from legba.data.registry.eval_runs_api import EVAL_RUNS_ROUTE_VERSION, build_eval_runs_router

from ._eval_runs_fakes import FakeEvalConn, FakePool, arm, run

_ROUTE = "/api/v1/v3/eval/runs"
ACTOR = "user:0b4c2e0e-1111-4222-8333-944455556666"


def _app(pool, *, bearer: bool = True) -> FastAPI:
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_eval_runs_router(deps), prefix="/api/v1/v3")
    if bearer:
        app.dependency_overrides[require_bearer] = lambda: "test"
    return app


async def _get(app: FastAPI, path: str = _ROUTE):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(path)


async def _seeded() -> FakeEvalConn:
    conn = FakeEvalConn()
    await er.publish(conn, run(), actor=ACTOR)
    await er.publish(conn, run(arms=(arm("A", 0.4), arm("B", 0.5, sample_of="A", sample_index=2))),
                     actor=ACTOR, correction_reason="arm B was a second sample of A")
    await er.publish(conn, run("inv-002", title="Invented withdrawn run",
                               as_of=datetime(2026, 2, 1, tzinfo=timezone.utc)), actor=ACTOR)
    await er.withdraw(conn, "inv-002", actor=ACTOR, reason="published in error")
    return conn


async def test_every_version_with_status_in_words() -> None:
    r = await _get(_app(FakePool(await _seeded())))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == EVAL_RUNS_ROUTE_VERSION and body["available"] is True
    assert [(x["run_key"], x["version"], x["status"]) for x in body["runs"]] == [
        ("inv-001", 2, "current"), ("inv-001", 1, "superseded"), ("inv-002", 1, "withdrawn")]
    cur, old, gone = body["runs"]
    assert cur["supersedes"] == old["id"] and old["superseded_by"] == cur["id"]
    assert cur["correction_reason"] == "arm B was a second sample of A"
    assert gone["withdrawn_reason"] == "published in error" and gone["withdrawn_at"]
    assert cur["arms"][1] == {"id": "B", "label": "Invented arm B", "description": "An invented way of answering, called B.",
                              "sample_of": "A", "sample_index": 2}
    res = cur["results"][1]
    assert (res["score"], res["ci_low"], res["ci_high"], res["ci_level"], res["n"]) == (0.5, 0.4, 0.6, 0.95, 12)
    assert res["by_class"][0] == {"class": "ALPHA", "score": 0.5, "n": 6}
    assert cur["design"] == {"preregistered_at": None, "prereg_sha256": "a" * 64, "version": "1.0"}
    assert cur["question_classes"] == [{"class": "ALPHA", "n": 6}, {"class": "BETA", "n": 6}]
    assert cur["gold_versions"] == ["gold v1"] and cur["grader"] == "invented-grader"
    assert "published_by" not in cur  # the audit actor stays in the table


async def test_nothing_published_and_unreadable_are_two_answers() -> None:
    r = await _get(_app(FakePool(FakeEvalConn())))
    assert r.status_code == 200 and r.json()["runs"] == [] and r.json()["available"] is True

    class _Missing(FakeEvalConn):
        async def fetch(self, sql, *a):
            raise RuntimeError('relation "eval_runs" does not exist')

    r = await _get(_app(FakePool(_Missing())))
    assert r.status_code == 200 and r.json() == {"version": EVAL_RUNS_ROUTE_VERSION, "available": False, "runs": []}
    assert (await _get(_app(FakePool(FakeEvalConn())), _ROUTE + "?limit=0")).status_code == 422


async def test_bearer_gated_read_role_never_public(monkeypatch) -> None:
    # without the override the route's own dependency refuses an anonymous caller
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    r = await _get(_app(FakePool(FakeEvalConn()), bearer=False))
    assert r.status_code in (401, 403, 503)
    assert rr.required_role_for("GET", _ROUTE) == rr.ROLE_READ
    assert ("GET", _ROUTE) not in rr.PUBLIC_ROUTES


def test_the_route_is_mounted_on_the_real_app() -> None:
    text = (pathlib.Path(__file__).resolve().parents[2]
            / "src/legba/data/registry/server.py").read_text(encoding="utf-8")
    assert "build_eval_runs_router(deps), prefix=\"/api/v1/v3\"" in text


_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


def test_the_route_and_its_store_import_in_the_slim_image() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.eval_runs_api as api
        import legba.data.eval_runs as store

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the eval-runs route: %r" % leaked
        assert api.build_eval_runs_router is not None and store.fetch_runs is not None
        print("OK", store.__file__)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0 and "OK" in result.stdout, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}")
    assert _SRC in result.stdout  # the worktree's own source, not the main checkout's
