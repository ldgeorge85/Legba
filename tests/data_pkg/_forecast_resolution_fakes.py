# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ledger fake plus the 0244 resolution records, the evidence search and
the anchor's record leaves; F-1 (0262): the rejection act, the acting role,
the reads the write routes take (acts on a basis, corrections of an act, the
questions a person holds a number on).

Dispatch is on the production SQL constants, so the store, the job and the
search under test are the production ones; an unknown statement fails. The
records are append-only here as in Postgres: the fake has no UPDATE path for
them, and the (question_id, fingerprint) unique index is honoured
(ON CONFLICT DO NOTHING returns no row).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from legba.data import forecast_evidence as fe
from legba.data import forecast_resolution as fr

from ._forecast_ledger_fakes import FakeLedgerConn

_REC_COLS = [c.strip() for c in fr._REC_COLS.split(",")]


def _pg_are_to_py(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern.replace(r"\m", r"\b").replace(r"\M", r"\b"), re.IGNORECASE)


class FakeResolutionConn(FakeLedgerConn):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []
        self.override_rows: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []
        self.sources: dict[str, dict[str, Any]] = {}
        self.observations: list[dict[str, Any]] = []
        self._clock = 0

    def _rec_public(self, r: dict[str, Any]) -> dict[str, Any]:
        return {**r, "evidence": json.dumps(r["evidence"]), "judgements": json.dumps(r["judgements"])}

    async def fetchrow(self, sql: str, *a: Any):
        if sql in (fr.INSERT_RECORD_SQL, fr.INSERT_RECORD_WITH_ROLE_SQL):
            row = dict(zip(_REC_COLS + ["actor_role"], a))
            row.setdefault("actor_role", None)
            if any(r["question_id"] == str(row["question_id"]) and r["fingerprint"] == row["fingerprint"]
                   for r in self.records):
                return None
            if row["record_kind"] == "rejection":
                assert row["basis_record_id"] and not row["resolution_id"], "0262 CHECK: rejection shape"
            elif row["record_kind"] != "proposal":
                assert row["outcome"] in ("yes", "no", "void") and row["resolution_id"], "0244 CHECK"
            assert row["actor_role"] in (None, "owner", "admin", "contributor"), "0262 CHECK: role"
            if row["record_kind"] in ("override", "correction", "rejection"):
                assert (row["reason"] or "").strip(), "0244 CHECK: reason"
            if row["record_kind"] == "override":
                assert row["override_log_id"], "0244 CHECK: override logged"
            self._clock += 1
            row.update(
                id=str(uuid.uuid4()), question_id=str(row["question_id"]),
                recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc).replace(microsecond=self._clock),
                evidence=json.loads(row["evidence"]), judgements=json.loads(row["judgements"]),
                clauses=json.loads(row["clauses"]), searched=json.loads(row["searched"]),
            )
            self.records.append(row)
            return {"id": row["id"]}
        if sql == fr.LATEST_RECORD_SQL:
            sel = [r for r in self.records if r["question_id"] == str(a[0]) and r["record_kind"] in a[1]]
            return self._rec_public(sel[-1]) if sel else None
        if sql == fr.RECORD_BY_ID_SQL:
            r = next((r for r in self.records if r["id"] == str(a[0])), None)
            return None if r is None else self._rec_public(r)
        if sql == fr.INSERT_OVERRIDE_RETURNING_SQL:
            oid = str(uuid.uuid4())
            self.override_rows.append({"id": oid, "actor": a[0], "object_kind": a[1], "object_id": a[2],
                                       "action": a[3], "before_sha256": a[4], "after_sha256": a[5],
                                       "reason": a[6]})
            return {"id": oid}
        return await super().fetchrow(sql, *a)

    async def fetchval(self, sql: str, *a: Any):
        if sql == fr.RESOLUTION_SHA_SQL:
            return self.resolutions[str(a[0])]["content_sha256"]
        if sql == fe.WINDOW_COUNT_SQL:
            return sum(1 for s in self.signals if a[0] <= s["fetched_at"] <= a[1])
        return await super().fetchval(sql, *a)

    async def fetch(self, sql: str, *a: Any):
        if sql == fr.JOB_QUESTIONS_SQL:
            rows = [q for q in self.questions.values() if q["sealed_at"] is not None and q["status"] == "open"]
            return [dict(q) for q in sorted(rows, key=lambda q: (q["due_at"], q["slug"]))]
        if sql == fr.ACTS_ON_BASIS_SQL:
            return [self._rec_public(r) for r in self.records
                    if r.get("basis_record_id") == str(a[0]) and r["record_kind"] != "proposal"]
        if sql == fr.CORRECTIONS_OF_SQL:
            return [self._rec_public(r) for r in self.records
                    if r.get("supersedes_id") == str(a[0]) and r["record_kind"] == "correction"]
        if sql == fr.QUEUE_SQL:
            out = []
            for q in sorted(self.questions.values(), key=lambda q: (q["due_at"], q["slug"])):
                if q["sealed_at"] is None or q["status"] != "open":
                    continue
                props = [r for r in self.records if r["question_id"] == q["id"] and r["record_kind"] == "proposal"]
                if not props:
                    continue
                p = props[-1]
                if any(r["question_id"] == q["id"] and r["record_kind"] != "proposal"
                       and r["recorded_at"] >= p["recorded_at"] for r in self.records):
                    continue
                out.append({
                    **{k: q.get(k) for k in ("id", "slug", "question", "resolver_kind", "window_start",
                                             "window_end", "due_at", "grace_hours", "condition",
                                             "void_rules", "content_sha256")},
                    "resolver_spec": q["resolver_spec"] if isinstance(q["resolver_spec"], str)
                    else json.dumps(q["resolver_spec"]),
                    "proposal_id": p["id"], "outcome": p["outcome"], "as_of": p["as_of"],
                    "recorded_at": p["recorded_at"], "evidence": json.dumps(p["evidence"]),
                    "clauses": json.dumps(p["clauses"]), "judgements": json.dumps(p["judgements"]),
                    "void_rule": p["void_rule"],
                })
            return out[: a[0]]
        if sql == fr.ENTERED_NUMBER_SQL:
            qids, who = {str(x) for x in a[0]}, set(a[1])
            return [{"question_id": q} for q in sorted({
                n["question_id"] for n in self.numbers.values()
                if n["question_id"] in qids and (n["issuer_ref"] in who
                                                 or (n.get("detail") or {}).get("entered_by") in who)})]
        if sql == fr.LATEST_RECORDS_FOR_SQL:
            ids = {str(x) for x in a[0]}
            out: dict[tuple, dict] = {}
            for r in self.records:
                if r["question_id"] in ids:
                    out[(r["question_id"], r["record_kind"] == "proposal")] = self._rec_public(r)
            return list(out.values())
        if sql == fr.RECORD_LEAVES_SQL:
            return sorted(({"kind": "forecast_resolution_record", "id": r["id"],
                            "content_sha256": r["content_sha256"]} for r in self.records), key=lambda r: r["id"])
        if sql == fe.SOURCE_CLASSES_SQL:
            return [{"descriptor_id": k, "source_class": v.get("source_class"),
                     "geo": json.dumps(v.get("geo", [])), "state": v.get("state", "active")}
                    for k, v in self.sources.items()]
        if sql == fe.SIGNALS_SQL:
            pats = [_pg_are_to_py(p) for p in a[2]]
            out = []
            for s in sorted(self.signals, key=lambda s: s["fetched_at"]):
                blob = " ".join(str(s.get(k) or "") for k in ("title", "summary", "text"))
                if a[0] <= s["fetched_at"] <= a[1] and all(p.search(blob) for p in pats):
                    out.append(dict(s))
            return out[: a[3]]
        if sql == fe.OBSERVATIONS_SQL:
            # $1 source, $2 series, $3 as-of, $4 floor: the series named by
            # source + series (the vintage choice is the resolver's own too).
            src, ser = str(a[0]).lower(), str(a[1]).lower()
            return [o for o in self.observations
                    if o["series_id"].lower().startswith(src + ".") and o["series_id"].lower().endswith("." + ser)]
        return await super().fetch(sql, *a)
