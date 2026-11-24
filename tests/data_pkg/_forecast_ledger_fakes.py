# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An in-memory connection that answers the forecast-ledger store's SQL.

Dispatch is on the store's OWN SQL constants (``legba.data.forecast_ledger``),
so the store functions under test are the production ones; an unknown
statement fails the test rather than passing silently. The 0242 triggers are
DB-side and are pinned by ``test_forecast_ledger_db.py`` on a migrated DB;
this fake mirrors the two the store must never trip (no UPDATE of a sealed
number, no change to a sealed question but status) so a store bug that
would raise in Postgres raises here too.
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from legba.data import forecast_ledger as fl


class FakeLedgerConn:
    def __init__(self) -> None:
        self.questions: dict[str, dict[str, Any]] = {}
        self.numbers: dict[str, dict[str, Any]] = {}
        self.resolutions: dict[str, dict[str, Any]] = {}
        self.overrides: list[dict[str, Any]] = []
        self.statements: list[str] = []

    # -- helpers -----------------------------------------------------------
    def _q_by_slug(self, slug: str) -> dict[str, Any] | None:
        return next((q for q in self.questions.values() if q["slug"] == slug), None)

    @asynccontextmanager
    async def transaction(self):
        yield self

    # -- asyncpg surface ---------------------------------------------------
    async def fetchrow(self, sql: str, *a: Any) -> dict[str, Any] | None:
        self.statements.append(sql)
        if sql == fl._SELECT_QUESTION_BY_SLUG:
            q = self._q_by_slug(a[0])
            return None if q is None else {k: q[k] for k in ("id", "sealed_at", "content_sha256")}
        if sql == "SELECT id::text AS id FROM forecast_questions WHERE slug = $1":
            q = self._q_by_slug(a[0])  # scripts/load_forecasts.py's seal lookup
            return None if q is None else {"id": q["id"]}
        if sql == fl._SELECT_QUESTION_BY_ID:
            q = self.questions.get(str(a[0]))
            return None if q is None else dict(q)
        if sql == fl._INSERT_QUESTION:
            qid = str(uuid.uuid4())
            cols = [c.strip() for c in fl._Q_COLS.split(",")]
            row = dict(zip(cols, a))
            row.update(id=qid, status="open", sealed_at=None,
                       created_at=datetime.now(timezone.utc))
            self.questions[qid] = row
            return {"id": qid}
        if sql == fl._SELECT_NUMBER:
            for n in self.numbers.values():
                if (n["question_id"], n["issuer_kind"], n["issuer_ref"]) == (str(a[0]), a[1], a[2]):
                    return {k: n[k] for k in ("id", "content_sha256", "sealed_at")}
            return None
        if sql == fl._INSERT_NUMBER:
            if self.questions[str(a[0])]["sealed_at"] is not None:
                raise RuntimeError("trigger: sealed question, no new number")
            nid = str(uuid.uuid4())
            self.numbers[nid] = {
                "id": nid, "question_id": str(a[0]), "issuer_kind": a[1], "issuer_ref": a[2],
                "p": a[3], "absent_reason": a[4], "as_of": a[5], "rationale": a[6],
                "rationale_ref": a[7], "detail": json.loads(a[8]), "content_sha256": a[9],
                "issued_at": datetime.now(timezone.utc), "sealed_at": None,
            }
            return {"id": nid}
        if sql == fl._ACTIVE_RESOLUTION:
            for r in self.resolutions.values():
                if r["question_id"] == str(a[0]) and r["active"]:
                    return {"id": r["id"], "content_sha256": r["content_sha256"]}
            return None
        if sql == fl._INSERT_RESOLUTION:
            rid = str(uuid.uuid4())
            self.resolutions[rid] = {
                "id": rid, "question_id": str(a[0]), "outcome": a[1], "resolved_at": a[2],
                "resolved_by": a[3], "evidence_refs": list(a[4]), "early": a[5],
                "proposal_id": a[6], "note": a[7], "content_sha256": a[8], "active": True,
            }
            return {"id": rid}
        raise AssertionError(f"unexpected fetchrow SQL: {sql[:80]}")

    async def fetchval(self, sql: str, *a: Any) -> Any:
        self.statements.append(sql)
        if sql == fl._OPERATOR_NUMBER_COUNT:
            return sum(
                1 for n in self.numbers.values()
                if n["question_id"] == str(a[0]) and n["issuer_kind"] == "operator"
                and n["p"] is not None
            )
        raise AssertionError(f"unexpected fetchval SQL: {sql[:80]}")

    async def execute(self, sql: str, *a: Any) -> str:
        self.statements.append(sql)
        if sql == fl._UPDATE_QUESTION:
            q = self.questions[str(a[0])]
            if q["sealed_at"] is not None:
                return "UPDATE 0"
            cols = [c.strip() for c in fl._Q_COLS.split(",")]
            q.update(dict(zip(cols, a[1:])))
            return "UPDATE 1"
        if sql == fl._UPDATE_NUMBER:
            n = self.numbers[str(a[0])]
            if n["sealed_at"] is not None:
                raise RuntimeError("trigger: sealed number, UPDATE refused")
            n.update(p=a[1], absent_reason=a[2], as_of=a[3], rationale=a[4],
                     rationale_ref=a[5], detail=json.loads(a[6]), content_sha256=a[7])
            return "UPDATE 1"
        if sql == fl._SEAL_QUESTION:
            self.questions[str(a[0])]["sealed_at"] = a[1]
            return "UPDATE 1"
        if sql == fl._SEAL_NUMBERS:
            for n in self.numbers.values():
                if n["question_id"] == str(a[0]) and n["sealed_at"] is None:
                    n["sealed_at"] = a[1]
            return "UPDATE 1"
        if sql == fl._RETIRE_RESOLUTION:
            self.resolutions[str(a[0])]["active"] = False
            return "UPDATE 1"
        if sql == fl._SET_STATUS:
            self.questions[str(a[0])]["status"] = a[1]
            return "UPDATE 1"
        if sql == fl._INSERT_OVERRIDE:
            self.overrides.append({
                "actor": a[0], "object_kind": a[1], "object_id": a[2], "action": a[3],
                "before_sha256": a[4], "after_sha256": a[5], "reason": a[6],
            })
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute SQL: {sql[:80]}")

    async def fetch(self, sql: str, *a: Any) -> list[dict[str, Any]]:
        self.statements.append(sql)
        if sql == fl.LEDGER_QUESTIONS_SQL:
            rows = sorted(self.questions.values(), key=lambda q: (q["due_at"], q["slug"]))
            return [dict(r) for r in rows[: a[0]]]
        if sql == fl.LEDGER_NUMBERS_SQL:
            ids = {str(x) for x in a[0]}
            return [
                {**n, "p": None if n["p"] is None else float(n["p"])}
                for n in self.numbers.values() if n["question_id"] in ids
            ]
        if sql == fl.LEDGER_RESOLUTIONS_SQL:
            ids = {str(x) for x in a[0]}
            return [r for r in self.resolutions.values() if r["active"] and r["question_id"] in ids]
        if sql == fl._NUMBERS_FOR_QUESTION:
            return [
                {**n, "p": None if n["p"] is None else float(n["p"])}
                for n in self.numbers.values() if n["question_id"] == str(a[0])
            ]
        if sql == fl.LEDGER_LEAVES_SQL:
            rows = (
                [{"kind": "forecast_number", "id": k, "content_sha256": v["content_sha256"]}
                 for k, v in self.numbers.items()]
                + [{"kind": "forecast_question", "id": k, "content_sha256": v["content_sha256"]}
                   for k, v in self.questions.items()]
                + [{"kind": "forecast_resolution", "id": k, "content_sha256": v["content_sha256"]}
                   for k, v in self.resolutions.items()]
            )
            return sorted(rows, key=lambda r: (r["kind"], r["id"]))
        raise AssertionError(f"unexpected fetch SQL: {sql[:80]}")


class FakePool:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn
