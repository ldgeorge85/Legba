# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An in-memory connection that answers ``legba.data.eval_runs``'s SQL (E-3).

Dispatch is on the store's OWN SQL constants, so the functions under test are
the production ones; an unknown statement fails the test. The 0260 guard
(append-only, one-time withdrawal) is DB-side and pinned on a migrated DB by
``test_migration_0260_eval_runs.py``; this fake mirrors what the store relies
on (unique (run_key, version), the withdrawal's ``withdrawn_at IS NULL``).
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from legba.data import eval_runs as er

_RUN_COLS = [
    "run_key", "version", "supersedes_id", "correction_reason", "title", "purpose", "design_sha256",
    "design_version", "design_registered_at", "as_of", "question_count", "question_classes", "grader",
    "temperature", "gold_versions", "metric", "metric_label", "higher_is_better", "ci_level", "caveats",
    "deviations", "source_schema", "source_sha256", "published_by", "content_sha256",
]
_ARM_COLS = [
    "run_id", "position", "arm_key", "label", "description", "sample_of", "sample_index", "score",
    "ci_low", "ci_high", "n", "by_class",
]


class FakeEvalConn:
    def __init__(self, now: datetime | None = None) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.arms: list[dict[str, Any]] = []
        self.now = now or datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
        self.closed = False

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def close(self) -> None:
        self.closed = True

    def _latest(self, key: str) -> dict[str, Any] | None:
        rows = [r for r in self.runs.values() if r["run_key"] == key]
        return max(rows, key=lambda r: r["version"]) if rows else None

    async def fetchrow(self, sql: str, *a: Any) -> dict[str, Any] | None:
        if sql == er._SELECT_LATEST:
            r = self._latest(a[0])
            return None if r is None else {k: r[k] for k in ("id", "version", "content_sha256", "withdrawn_at")}
        if sql == er._SELECT_VERSION:
            r = next((r for r in self.runs.values() if r["run_key"] == a[0] and r["version"] == a[1]), None)
            return None if r is None else {k: r[k] for k in ("id", "version", "withdrawn_at")}
        if sql == er._INSERT_RUN:
            row = dict(zip(_RUN_COLS, a))
            if any(r["run_key"] == row["run_key"] and r["version"] == row["version"] for r in self.runs.values()):
                raise RuntimeError("duplicate key value violates unique constraint uq_eval_runs_key_version")
            rid = str(uuid.uuid4())
            row.update(id=rid, published_at=self.now, withdrawn_at=None, withdrawn_by=None, withdrawn_reason=None)
            self.runs[rid] = row
            return {"id": rid, "published_at": self.now}
        if sql == er._WITHDRAW:
            r = self.runs.get(a[0])
            if r is None or r["withdrawn_at"] is not None:
                return None
            r.update(withdrawn_at=self.now, withdrawn_by=a[1], withdrawn_reason=a[2])
            return {"withdrawn_at": self.now}
        if sql.startswith("SELECT id::text AS id, role, status FROM users"):
            return None
        raise AssertionError(f"unexpected fetchrow: {sql[:80]}")

    async def execute(self, sql: str, *a: Any) -> str:
        if sql == er._INSERT_ARM:
            self.arms.append(dict(zip(_ARM_COLS, a)))
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {sql[:80]}")

    async def fetch(self, sql: str, *a: Any) -> list[dict[str, Any]]:
        if sql == er.SELECT_RUNS:
            rows = sorted(self.runs.values(), key=lambda r: (-r["as_of"].timestamp(), r["run_key"], -r["version"]))
            return [dict(r) for r in rows[: a[0]]]
        if sql == er.SELECT_ARMS:
            ids = set(a[0])
            return [dict(x) for x in sorted(self.arms, key=lambda x: (x["run_id"], x["position"])) if x["run_id"] in ids]
        raise AssertionError(f"unexpected fetch: {sql[:80]}")


class FakePool:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def arm(key: str, score: float, n: int = 12, **kw: Any) -> er.EvalArmRecord:
    return er.EvalArmRecord(
        arm_key=key, label=kw.pop("label", f"Invented arm {key}"),
        description=kw.pop("description", f"An invented way of answering, called {key}."),
        n=n, score=score, ci_low=kw.pop("ci_low", score - 0.1), ci_high=kw.pop("ci_high", score + 0.1),
        by_class=kw.pop("by_class", (er.ClassScore("ALPHA", score, n // 2), er.ClassScore("BETA", score, n - n // 2))),
        **kw,
    )


def run(key: str = "inv-001", **kw: Any) -> er.EvalRunRecord:
    base: dict[str, Any] = dict(
        run_key=key, title="Invented evaluation", design_sha256="a" * 64, design_version="1.0",
        as_of=datetime(2026, 3, 1, tzinfo=timezone.utc), question_count=12,
        question_classes=(("ALPHA", 6), ("BETA", 6)), grader="invented-grader", temperature=1.0,
        gold_versions=("gold v1",), metric="correct", metric_label="Share correct",
        source_schema="legba.bakeoff.report/1", source_sha256="b" * 64, ci_level=0.95,
        caveats=("An invented caveat.",), deviations=("D1: an invented departure",),
        arms=(arm("A", 0.4), arm("B", 0.6)),
    )
    base.update(kw)
    return er.EvalRunRecord(**base)


def jsonish(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v
