# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/ledger/resolution-records`` (U-11) — one question's resolution record.

  * the shape over a fake connection: newest first, evidence capped with the
    total kept, JSON columns decoded, no confirm route advertised;
  * the honest degradation: the 0244 table unreadable is ``available: false``
    at HTTP 200, never a 500 and never an empty "nothing proposed";
  * a malformed question id is refused at the edge (422), the query never runs;
  * the bearer gate (dev mode off);
  * the real binding path on a migrated test DB: a proposal and a confirmation
    written by the real store come back through the mounted router;
  * the slim-image import guard.
"""
from __future__ import annotations

import json
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

from legba.data.registry._deps import require_bearer
from legba.data.registry.forecast_records_api import (
    EVIDENCE_CAP,
    FORECAST_RECORDS_ROUTE_VERSION,
    RECORDS_FOR_QUESTION_SQL,
    build_forecast_records_router,
)

QID = "0b5a1c3e-1111-4222-8333-944455556666"
ROUTE = "/api/v1/v3/ledger/resolution-records"


class _Conn:
    def __init__(self, rows=None, fail: Exception | None = None):
        self.rows = rows or []
        self.fail = fail
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.fail:
            raise self.fail
        return self.rows


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _app(conn, *, gate: bool = False) -> FastAPI:
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=_Pool(conn)))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_forecast_records_router(deps), prefix="/api/v1/v3")
    if not gate:
        app.dependency_overrides[require_bearer] = lambda: "test"
    return app


async def _get(app, query: str = f"?question_id={QID}", headers=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(ROUTE + query, headers=headers or {})


def _row(kind: str, outcome: str, at: datetime, evidence: list, **kw) -> dict:
    return {
        "id": kw.get("id", f"rec-{kind}-{outcome}"), "question_id": QID, "record_kind": kind,
        "outcome": outcome, "resolver_kind": "event_reported", "resolver_version": "rv-test",
        "as_of": at, "recorded_at": at, "actor": kw.get("actor", "resolver:test"),
        "evidence": json.dumps(evidence), "clauses": json.dumps(kw.get("clauses", [])),
        "judgements": json.dumps(kw.get("judgements", [])), "searched": json.dumps(kw.get("searched", {})),
        "void_rule": json.dumps(kw["void_rule"]) if "void_rule" in kw else None,
        "basis_record_id": kw.get("basis"), "supersedes_id": None, "resolution_id": kw.get("resolution"),
        "reason": kw.get("reason"), "content_sha256": "a" * 64,
    }


AT = datetime(2026, 3, 22, 6, 0, tzinfo=timezone.utc)


async def test_the_records_come_back_decoded_with_evidence_capped():
    many = [{"ref": f"signal:{i}", "title": f"An invented report {i}", "clause": "event"} for i in range(EVIDENCE_CAP + 7)]
    conn = _Conn([
        _row("confirmation", "yes", AT + timedelta(hours=2), [], actor="operator:alice", basis="rec-p", resolution="res-1"),
        _row("proposal", "yes", AT, many, id="rec-p",
             clauses=[{"id": "main:event", "text": "an exchange carried out", "status": "met", "evidence": ["signal:0"]}],
             judgements=[{"key": "main:attribution", "question": "Is this the agreed exchange?"}],
             searched={"corpus": "signals", "rows_matched": 47}, void_rule={"kind": "silence", "text": "no report"}),
    ])
    r = await _get(_app(conn))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == FORECAST_RECORDS_ROUTE_VERSION and body["available"] is True
    assert body["confirm_route"] is None and body["evidence_cap"] == EVIDENCE_CAP
    act, prop = body["records"]
    assert act["record_kind"] == "confirmation" and act["actor"] == "operator:alice" and act["basis_record_id"] == "rec-p"
    assert prop["evidence_total"] == EVIDENCE_CAP + 7 and len(prop["evidence"]) == EVIDENCE_CAP
    assert prop["clauses"][0]["status"] == "met" and prop["judgements"][0]["key"] == "main:attribution"
    assert prop["searched"]["rows_matched"] == 47 and prop["void_rule"]["kind"] == "silence"
    assert prop["as_of"] == AT.isoformat()
    # One bounded query, keyed by the question, the default limit.
    sql, args = conn.calls[0]
    assert sql == RECORDS_FOR_QUESTION_SQL and args == (QID, 20)


async def test_an_unreadable_record_table_says_so():
    r = await _get(_app(_Conn(fail=RuntimeError('relation "forecast_resolution_records" does not exist'))))
    assert r.status_code == 200
    assert r.json()["available"] is False and r.json()["records"] == []


async def test_an_empty_record_is_available_and_empty():
    body = (await _get(_app(_Conn([])))).json()
    assert body["available"] is True and body["records"] == []


async def test_a_bad_question_id_is_refused_before_any_query():
    conn = _Conn([])
    for q in ("?question_id=F8-slug", "", f"?question_id={QID}&limit=0", f"?question_id={QID}&limit=101"):
        r = await _get(_app(conn), q)
        assert r.status_code == 422, q
    assert conn.calls == []


async def test_the_route_is_mounted_and_bearer_gated(monkeypatch):
    text = (pathlib.Path(__file__).resolve().parents[2] / "src/legba/data/registry/server.py").read_text()
    assert 'build_forecast_records_router(deps), prefix="/api/v1/v3"' in text
    app = _app(_Conn([]), gate=True)
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    assert (await _get(app)).status_code == 503
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", "t-records")
    assert (await _get(app)).status_code == 401
    assert (await _get(app, headers={"Authorization": "Bearer nope"})).status_code == 403
    assert (await _get(app, headers={"Authorization": "Bearer t-records"})).status_code == 200


# ---------------------------------------------------------------------------
# The real binding path — the store writes, the mounted router reads.
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_records_written_by_the_store_read_back_through_the_router(migrated_pg, clean_tables):
    import asyncpg

    from legba.data import forecast_ledger as fl
    from legba.data import forecast_resolution as fr
    from legba.data import forecast_resolvers as rules

    await clean_tables("forecast_resolution_records", "forecast_resolutions", "forecast_resolution_proposals",
                       "forecast_numbers", "forecast_questions", "scorecard_entries", "override_log")
    sealed = datetime(2026, 3, 1, tzinfo=timezone.utc)
    due = datetime(2026, 3, 20, 23, 59, 59, tzinfo=timezone.utc)
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        q = fl.ForecastQuestion(
            slug="FX-u11", question="A fictional exchange is carried out.", resolver_kind="event_reported",
            resolver_spec={"class": "prisoner exchange", "actors": ["RU", "UA"], "window": ["2026-03-01", "2026-03-20"]},
            due_at=due, window_start=sealed, window_end=due,
            void_rules="Neither government states anything and no outlet reports on an exchange by due + grace.",
        )
        _, qid = await fl.upsert_question(conn, q)
        await fl.state_number(conn, qid, fl.ForecastNumber("operator", "operator", 0.6, sealed))
        await fl.seal_question(conn, qid, at=sealed)
        row = dict(await conn.fetchrow(fl._SELECT_QUESTION_BY_ID, qid))
        p = rules.Proposal(outcome="yes", resolver_kind="event_reported", as_of=due + timedelta(hours=73),
                           evidence=[{"ref": "signal:00000000-0000-0000-0000-000000000001",
                                      "url": "https://example.org/a", "title": "An invented report"}])
        status, rec = await fr.write_proposal(conn, row, p)
        assert status == "written"
        await fr.confirm_proposal(conn, rec, actor="operator:test", now=due + timedelta(hours=80))
    finally:
        await conn.close()

    class _RealPool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    self_inner.c = await asyncpg.connect(migrated_pg.dsn)
                    return self_inner.c

                async def __aexit__(self_inner, *exc):
                    await self_inner.c.close()
                    return False

            return _Ctx()

    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=_RealPool()))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_forecast_records_router(deps), prefix="/api/v1/v3")
    app.dependency_overrides[require_bearer] = lambda: "test"
    body = (await _get(app, f"?question_id={qid}")).json()
    assert body["available"] is True
    kinds = [r["record_kind"] for r in body["records"]]
    assert kinds == ["confirmation", "proposal"]
    prop = body["records"][1]
    assert prop["outcome"] == "yes" and prop["evidence"][0]["url"] == "https://example.org/a"
    assert prop["evidence_total"] == 1 and len(prop["content_sha256"]) == 64


# ---------------------------------------------------------------------------
# The slim-image import guard.
# ---------------------------------------------------------------------------

_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


def test_the_route_imports_without_the_runtime_stack() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.registry.forecast_records_api as api
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts") or m.startswith("legba.runtime"))
        assert not leaked, "analyst/runtime package reachable from the route: %r" % leaked
        assert api.build_forecast_records_router is not None
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
