# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The exogenous forecast ledger's model and store, with fakes only.

Pins: the vocabularies against migration 0242's CHECKs; the resolver-spec
rules; the clamp; the hash recipe (a number binds its question's words); the
store's idempotence on the slug; the seal (refuses without an operator
number, with a placeholder left, and refuses every later position); the
resolution rules (before due, early with evidence, unsealed, a correction
needs a reason and leaves an override row); the Ledger's ordering; and the
receipt anchor folding every ledger hash into the day's root.

The DB-side guards (the 0242 triggers, migration idempotence, the score
view) are ``test_forecast_ledger_db.py`` on a migrated test DB.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from legba.data import forecast_ledger as fl
from legba.data.analysts.deterministic_handlers import receipt_anchor as ra

from ._forecast_ledger_fakes import FakeLedgerConn, FakePool

pytestmark = [pytest.mark.asyncio]

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src/legba/data/migrations/0242_forecast_ledger.sql"
)
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 12, 15, 23, 59, 59, tzinfo=timezone.utc)


def _q(**over) -> fl.ForecastQuestion:
    base = dict(
        slug="FX1-klarvia-port-strike",
        question="A strike closes the port of Klarvia for 24 h between 11-11 and 12-15",
        resolver_kind="event_reported",
        resolver_spec={"class": "port closure", "actor": "dockworkers", "target_area": "Klarvia"},
        due_at=DUE,
        window_start=datetime(2026, 11, 11, tzinfo=timezone.utc),
        window_end=DUE,
        question_class="labour",
        void_rules="the port authority publishes nothing by due + grace",
        subject_keys=("country_kv",),
    )
    base.update(over)
    return fl.ForecastQuestion(**base)


def _op(p: float | None = 0.3, **over) -> fl.ForecastNumber:
    base = dict(issuer_kind="operator", issuer_ref="operator", p=p, as_of=NOW)
    base.update(over)
    return fl.ForecastNumber(**base)


def _check_values(sql: str, column: str) -> set[str]:
    m = re.search(column + r"\s+IN\s*\(([^)]*)\)", sql, re.S)
    assert m, f"no CHECK on {column}"
    return {v.strip().strip("'") for v in m.group(1).replace("\n", " ").split(",") if v.strip()}


# ---------------------------------------------------------------------------
# Vocabulary drift guards — the slim route reads no DDL
# ---------------------------------------------------------------------------


async def test_vocabularies_match_the_migration_checks():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert _check_values(sql, "resolver_kind") == set(fl.RESOLVER_KINDS + fl.ENDOGENOUS_RESOLVER_KINDS)
    assert _check_values(sql, "issuer_kind") == set(fl.ISSUER_KINDS)
    assert _check_values(sql, "outcome") == set(fl.OUTCOMES)
    assert _check_values(sql, r"status") >= set(fl.STATUSES)
    assert "p >= 0.01 AND p <= 0.99" in sql
    assert (fl.P_MIN, fl.P_MAX) == (0.01, 0.99)


async def test_the_decided_six_resolver_kinds():
    assert fl.RESOLVER_KINDS == (
        "event_reported", "official_document", "official_result",
        "series_threshold", "count_threshold", "scheduled_event_status",
    )
    assert fl.MACHINE_RESOLVABLE == {"series_threshold"}


async def test_the_migration_is_idempotent_by_construction():
    sql = MIGRATION.read_text(encoding="utf-8")
    body = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))
    assert re.search(r"CREATE TABLE (?!IF NOT EXISTS)", body) is None
    assert re.search(r"CREATE (UNIQUE )?INDEX (?!IF NOT EXISTS)", body) is None
    triggers = re.findall(r"CREATE TRIGGER (\w+)", body)
    assert len(triggers) == 6
    for t in triggers:
        assert f"DROP TRIGGER IF EXISTS {t} ON" in body
    assert re.search(r"^\s*(BEGIN|COMMIT)\s*;", body, re.M) is None
    assert "CREATE OR REPLACE VIEW public.forecast_scores" in body


# ---------------------------------------------------------------------------
# Resolver specs, the clamp, the hashes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind, spec", [
    ("event_reported", {"class": "strike"}),
    ("official_document", {"issuers": ["A", "B"], "doc_type": "memorandum"}),
    ("official_result", {"authority": "CEC", "outcome": "party X wins"}),
    ("series_threshold", {"source": "EIA", "series": "RBRTE", "op": ">=", "value": 100, "on": "2026-12-31"}),
    ("count_threshold", {"authority": "Interior Ministry", "count": "turnout", "op": ">=", "value": 500000}),
    ("scheduled_event_status", {"scheduled_event_id": "x", "expect": "held"}),
])
async def test_each_kind_accepts_its_spec_and_refuses_an_empty_one(kind, spec):
    assert fl.resolver_spec_problems(kind, spec) == []
    first_key = next(iter(spec))
    broken = {k: v for k, v in spec.items() if k != first_key}
    assert any(first_key in p for p in fl.resolver_spec_problems(kind, broken or {"x": 1}))


async def test_spec_rules_refuse_unknown_kind_bad_op_and_nest_one_level():
    assert fl.resolver_spec_problems("vibes", {"a": 1})
    bad = {"source": "EIA", "series": "RBRTE", "op": "~", "value": 100, "on": "2026-12-31"}
    assert any("op" in p for p in fl.resolver_spec_problems("series_threshold", bad))
    composite = {
        "issuers": ["RU", "UA"], "doc_type": "halt",
        "also": [{"kind": "event_reported", "negate": True, "spec": {"class": "refinery strike"}}],
    }
    assert fl.resolver_spec_problems("official_document", composite) == []
    composite["also"][0]["spec"] = {}
    assert fl.resolver_spec_problems("official_document", composite)


async def test_event_reported_needs_a_window_and_endogenous_kinds_are_not_exogenous():
    assert any("window" in p for p in _q(window_start=None, window_end=None).problems())
    assert _q(window_start=None, window_end=None,
              resolver_spec={"class": "x", "window": ["2026-11-11", "2026-12-15"]}).problems() == []
    q = _q(resolver_kind="indicator_state", resolver_spec={"indicator": "x"})
    assert any("endogenous" in p for p in q.problems())


async def test_clamp():
    assert fl.clamp_p(0.0) == 0.01
    assert fl.clamp_p(1.0) == 0.99
    assert fl.clamp_p(0.2504) == 0.25


async def test_hashes_are_stable_and_a_number_binds_its_question():
    q1, q2 = _q(), _q()
    assert q1.content_sha256() == q2.content_sha256()
    assert _q(question="other words").content_sha256() != q1.content_sha256()
    n = _op(0.25)
    assert n.content_sha256(q1.content_sha256()) != n.content_sha256(
        _q(question="other words").content_sha256())
    # detail is the meter, not the claim
    assert _op(0.25, detail={"usd": 1}).content_sha256("a" * 64) == n.content_sha256("a" * 64)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


async def test_upsert_is_idempotent_on_the_slug_and_restates_before_the_seal():
    conn = FakeLedgerConn()
    out, qid = await fl.upsert_question(conn, _q())
    assert out == "inserted"
    assert await fl.upsert_question(conn, _q()) == ("unchanged", qid)
    assert await fl.upsert_question(conn, _q(condition="if the talks fail")) == ("updated", qid)
    assert len(conn.questions) == 1


async def test_an_invalid_question_is_refused_before_the_wire():
    conn = FakeLedgerConn()
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fl.upsert_question(conn, _q(resolver_spec={}))
    assert exc.value.code == "invalid"
    assert conn.statements == []


async def test_numbers_state_restate_and_absence_needs_a_reason():
    conn = FakeLedgerConn()
    _, qid = await fl.upsert_question(conn, _q())
    out, nid = await fl.state_number(conn, qid, _op(0.3))
    assert out == "inserted"
    assert await fl.state_number(conn, qid, _op(0.3)) == ("unchanged", nid)
    assert await fl.state_number(conn, qid, _op(0.35)) == ("updated", nid)
    assert float(conn.numbers[nid]["p"]) == 0.35
    with pytest.raises(fl.LedgerRefusal):
        await fl.state_number(conn, qid, _op(None, issuer_kind="tower", issuer_ref="tower"))
    out, _ = await fl.state_number(conn, qid, _op(
        None, issuer_kind="tower", issuer_ref="tower", absent_reason="no analyst emitted a number"))
    assert out == "inserted"
    with pytest.raises(fl.LedgerRefusal):
        await fl.state_number(conn, qid, _op(0.3, issuer_kind="pundit"))


async def test_the_seal_refuses_without_an_operator_number_or_with_a_placeholder():
    conn = FakeLedgerConn()
    _, qid = await fl.upsert_question(conn, _q(condition="<OPERATOR TO WRITE>"))
    problems = await fl.seal_problems(conn, qid)
    assert any("placeholder" in p for p in problems)
    assert any("no operator number" in p for p in problems)
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fl.seal_question(conn, qid)
    assert exc.value.code == "seal_incomplete"
    assert conn.questions[qid]["sealed_at"] is None


async def test_the_seal_stamps_question_and_numbers_and_refuses_every_later_position():
    conn = FakeLedgerConn()
    _, qid = await fl.upsert_question(conn, _q())
    _, nid = await fl.state_number(conn, qid, _op(0.25))
    await fl.state_number(conn, qid, _op(0.4, issuer_kind="clef_arm", issuer_ref="llm.decide.x"))
    when = await fl.seal_question(conn, qid, at=NOW)
    assert conn.questions[qid]["sealed_at"] == when
    assert all(n["sealed_at"] == when for n in conn.numbers.values())
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fl.state_number(conn, qid, _op(0.5))
    assert exc.value.code == "sealed"
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fl.upsert_question(conn, _q(question="rewritten after the seal"))
    assert exc.value.code == "sealed"
    assert (await fl.upsert_question(conn, _q()))[0] == "unchanged"
    with pytest.raises(fl.LedgerRefusal):
        await fl.seal_question(conn, qid)


async def test_the_seal_refuses_a_number_stated_on_an_earlier_wording():
    conn = FakeLedgerConn()
    _, qid = await fl.upsert_question(conn, _q(condition="<OPERATOR TO WRITE>"))
    await fl.state_number(conn, qid, _op(0.25))
    await fl.state_number(conn, qid, _op(0.4, issuer_kind="clef_arm", issuer_ref="decision-model"))
    await fl.upsert_question(conn, _q(condition="if the talks fail by 11-20"))
    await fl.state_number(conn, qid, _op(0.25))  # the operator restates; the arm does not
    problems = await fl.seal_problems(conn, qid)
    assert problems == [
        "FX1-klarvia-port-strike: the clef_arm number (decision-model) was stated on an earlier "
        "wording of the question; restate it"
    ]
    await fl.state_number(conn, qid, _op(0.4, issuer_kind="clef_arm", issuer_ref="decision-model"))
    assert await fl.seal_problems(conn, qid) == []


async def _sealed(conn: FakeLedgerConn) -> str:
    _, qid = await fl.upsert_question(conn, _q())
    await fl.state_number(conn, qid, _op(0.25))
    await fl.seal_question(conn, qid, at=NOW)
    return qid


async def test_resolution_refuses_before_due_unless_early_with_evidence():
    conn = FakeLedgerConn()
    qid = await _sealed(conn)
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fl.resolve(conn, qid, outcome="yes", resolved_by="operator:1", now=NOW)
    assert exc.value.code == "before_due"
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fl.resolve(conn, qid, outcome="yes", resolved_by="operator:1", now=NOW, early=True)
    assert exc.value.code == "early_without_evidence"
    rid = await fl.resolve(conn, qid, outcome="yes", resolved_by="operator:1", now=NOW,
                           early=True, evidence_refs=["url:https://example.org/statement"])
    assert conn.resolutions[rid]["early"] is True
    assert conn.questions[qid]["status"] == "resolved"


async def test_resolution_after_due_needs_no_flag_and_unsealed_only_voids():
    conn = FakeLedgerConn()
    qid = await _sealed(conn)
    after = DUE + timedelta(hours=1)
    rid = await fl.resolve(conn, qid, outcome="no", resolved_by="operator:1", now=after)
    assert conn.resolutions[rid]["early"] is False
    _, q2 = await fl.upsert_question(conn, _q(slug="FX2-unsealed"))
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fl.resolve(conn, q2, outcome="yes", resolved_by="operator:1", now=after)
    assert exc.value.code == "unsealed"
    await fl.resolve(conn, q2, outcome="void", resolved_by="operator:1", now=after)
    assert conn.questions[q2]["status"] == "void"


async def test_a_second_resolution_is_a_correction_with_a_reason_and_an_override_row():
    conn = FakeLedgerConn()
    qid = await _sealed(conn)
    after = DUE + timedelta(hours=1)
    first = await fl.resolve(conn, qid, outcome="no", resolved_by="operator:1", now=after)
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fl.resolve(conn, qid, outcome="yes", resolved_by="operator:1", now=after)
    assert exc.value.code == "already_resolved"
    second = await fl.resolve(conn, qid, outcome="yes", resolved_by="operator:1", now=after,
                              correction_reason="the CENTCOM statement surfaced late")
    assert conn.resolutions[first]["active"] is False
    assert conn.resolutions[second]["active"] is True
    (ov,) = conn.overrides
    assert ov["object_id"] == first and ov["reason"].startswith("the CENTCOM")
    assert ov["before_sha256"] == conn.resolutions[first]["content_sha256"]
    assert ov["after_sha256"] == conn.resolutions[second]["content_sha256"]


# ---------------------------------------------------------------------------
# The read and the anchor
# ---------------------------------------------------------------------------


async def test_the_ledger_reads_open_first_by_due_with_every_issuer():
    conn = FakeLedgerConn()
    late = await _sealed(conn)
    _, early = await fl.upsert_question(conn, _q(slug="FX0-early", due_at=DUE - timedelta(days=20),
                                                  window_end=DUE - timedelta(days=20)))
    await fl.state_number(conn, early, _op(0.6))
    await fl.state_number(conn, early, _op(None, issuer_kind="tower", issuer_ref="tower",
                                           absent_reason="no analyst number"))
    await fl.state_number(conn, early, _op(0.5, issuer_kind="clef_arm", issuer_ref="decision-model"))
    _, closed = await fl.upsert_question(conn, _q(slug="FX9-closed", due_at=DUE - timedelta(days=40),
                                                   window_start=None, window_end=None,
                                                   resolver_spec={"class": "x", "window": ["a", "b"]}))
    await fl.resolve(conn, closed, outcome="void", resolved_by="operator:1", now=NOW)
    rows = await fl.fetch_ledger(conn, now=NOW)
    assert [r["slug"] for r in rows] == ["FX0-early", "FX1-klarvia-port-strike", "FX9-closed"]
    first = rows[0]
    assert [n["issuer_kind"] for n in first["numbers"]] == ["operator", "tower", "clef_arm"]
    assert first["numbers"][1]["p"] is None and first["numbers"][1]["absent_reason"]
    assert rows[1]["sealed_at"] is not None and rows[1]["numbers"][0]["sealed"] is True
    assert rows[2]["resolution"]["outcome"] == "void"
    assert rows[0]["overdue"] is False
    later = await fl.fetch_ledger(conn, now=DUE + timedelta(days=5))
    assert {r["slug"]: r["overdue"] for r in later}["FX1-klarvia-port-strike"] is True


class _Deps:
    def __init__(self, pool):
        self.pg_pool = pool


class _AnchorLedgerConn(FakeLedgerConn):
    """The ledger fake plus the anchor's own three statements."""

    def __init__(self, heads):
        super().__init__()
        self._heads = heads
        self.ensures: list[tuple] = []

    async def fetch(self, sql, *a):
        if "DISTINCT ON" in sql:
            return self._heads
        if "status = 'pending'" in sql:
            return []
        return await super().fetch(sql, *a)

    async def execute(self, sql, *a):
        if "INSERT INTO receipt_anchors" in sql:
            self.ensures.append(a)
            return "INSERT 0 1"
        return await super().execute(sql, *a)


async def test_the_anchor_folds_every_ledger_hash_after_the_chain_heads():
    heads = [{"analyst_id": "analyst_a", "receipt_hash": "aa" * 32}]
    conn = _AnchorLedgerConn(heads)
    qid = await _sealed(conn)
    result = await ra.handle([], {}, _Deps(FakePool(conn)))
    d = result.finding.data
    assert d["ledger_leaf_count"] == 2  # one question, one number
    assert d["leaf_count"] == 3
    leaves = [ra.leaf_for("analyst_a", "aa" * 32)] + [
        fl.ledger_leaf(r["kind"], r["id"], r["content_sha256"])
        for r in await FakeLedgerConn.fetch(conn, fl.LEDGER_LEAVES_SQL)
    ]
    assert d["root_hash"] == ra.merkle_root(leaves).hex()
    # Restating a number before the seal would move the root: the hash is in it.
    assert conn.numbers and qid in {n["question_id"] for n in conn.numbers.values()}


async def test_an_unreadable_ledger_anchors_the_heads_alone_and_says_so():
    class _NoLedger(_AnchorLedgerConn):
        async def fetch(self, sql, *a):
            if sql == fl.LEDGER_LEAVES_SQL:
                raise RuntimeError('relation "forecast_numbers" does not exist')
            return await super().fetch(sql, *a)

    conn = _NoLedger([{"analyst_id": "analyst_a", "receipt_hash": "aa" * 32}])
    result = await ra.handle([], {}, _Deps(FakePool(conn)))
    d = result.finding.data
    assert d["leaf_count"] == 1 and d["ledger_leaf_count"] == 0
    assert any(w.startswith("receipt_anchor.ledger_unavailable") for w in d["warnings"])
    assert d["root_hash"] == ra.merkle_root([ra.leaf_for("analyst_a", "aa" * 32)]).hex()


async def test_the_leaf_order_is_byte_collated():
    assert 'COLLATE "C"' in fl.LEDGER_LEAVES_SQL
    assert fl.ledger_leaf("forecast_number", "x", "a" * 64) != fl.ledger_leaf("forecast_question", "x", "a" * 64)
