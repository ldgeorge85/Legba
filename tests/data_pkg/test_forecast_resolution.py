# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The resolution record, the job, the handler, the consult tool and
the anchor, over the store fakes (the production SQL constants answer).

  * the job proposes on due questions only, writes once per reading
    (idempotent: a re-run writes nothing and alerts nothing), supersedes on a
    changed reading, settles series_threshold by rule and nothing else;
  * a proposal that needs a judgement cannot be confirmed — the override path
    writes an override_log row with the reason, then the resolution;
  * a correction is a new resolution and a superseding record — no row edited;
  * dry run and the rehearsal write nothing;
  * the 0244 records fold into the receipt anchor;
  * ``list_due_resolvers`` reaches the consult through every surface.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from legba.data import forecast_ledger as fl
from legba.data import anchor_proof as ap
from legba.data import forecast_resolution as fr
from legba.data.analysts.deterministic_handlers import forecast_resolver as handler
from legba.data.analysts.deterministic_handlers import receipt_anchor as ra

from ._forecast_ledger_fakes import FakePool
from ._forecast_resolution_fakes import FakeResolutionConn

SEALED = datetime(2026, 3, 1, tzinfo=timezone.utc)
DUE = datetime(2026, 3, 20, 23, 59, 59, tzinfo=timezone.utc)
SPENT = DUE + timedelta(hours=73)
_DONE = "Russia and Ukraine exchanged 300 prisoners of war on Tuesday."


def _exchange(slug: str = "FX1-exchange", void_rules: str | None = None) -> fl.ForecastQuestion:
    return fl.ForecastQuestion(
        slug=slug, question="A fictional exchange is carried out in the window.",
        resolver_kind="event_reported",
        resolver_spec={"class": "prisoner exchange carried out", "actors": ["RU", "UA"],
                       "window": ["2026-03-01", "2026-03-20"]},
        due_at=DUE, window_start=SEALED, window_end=DUE,
        void_rules=void_rules or "Neither government states anything and no outlet reports on an exchange "
                                 "by due + grace.",
    )


def _series(slug: str = "FX2-series") -> fl.ForecastQuestion:
    return fl.ForecastQuestion(
        slug=slug, question="A fictional series is at or above 100.", resolver_kind="series_threshold",
        resolver_spec={"source": "FICT", "series": "FX1", "op": ">=", "value": 100, "on": "2026-03-20"},
        due_at=DUE, grace_hours=0,
    )


async def _seal(conn, q: fl.ForecastQuestion, *numbers: tuple[str, float]) -> str:
    _, qid = await fl.upsert_question(conn, q)
    for kind, p in numbers or (("operator", 0.6),):
        await fl.state_number(conn, qid, fl.ForecastNumber(kind, kind, p, SEALED - timedelta(days=1)))
    await fl.seal_question(conn, qid, at=SEALED)
    return qid


def _signal(conn, source: str, text: str, at: datetime = SEALED + timedelta(days=4)) -> None:
    conn.signals.append({
        "signal_id": f"00000000-0000-0000-0000-{len(conn.signals) + 1:012d}", "source_id": source,
        "fetched_at": at, "published_at": at.isoformat(), "url": f"https://{source}.example/x",
        "title": "Exchange", "summary": "", "text": text,
    })


class _Alerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def __call__(self, summary: str, detail: str, severity: str) -> None:
        self.sent.append((summary, detail, severity))


async def test_the_job_skips_questions_not_yet_due_and_proposes_on_due_ones():
    conn = FakeResolutionConn()
    await _seal(conn, _exchange())
    alerts = _Alerts()
    r = await fr.run_resolution_job(conn, now=DUE - timedelta(days=1), alert=alerts)
    assert r["questions"] == [] and conn.records == [] and alerts.sent == []
    _signal(conn, "source.alpha.news", _DONE)
    _signal(conn, "source.beta.news", _DONE)
    r = await fr.run_resolution_job(conn, now=SPENT, alert=alerts)
    (q,) = r["questions"]
    assert q["outcome"] == "yes" and q["record"] == "written"
    (rec,) = conn.records
    assert rec["record_kind"] == "proposal" and rec["actor"] == fr.RESOLVER_ACTOR
    assert rec["resolver_version"] and len(rec["evidence"]) == 2
    assert all(e["fetched_at"] and e["published_vs_window"] == "inside_window" for e in rec["evidence"])
    assert conn.resolutions == {}          # event_reported is never settled by rule
    assert len(alerts.sent) == 1 and "proposal yes" in alerts.sent[0][0]
    assert "confirm --record" in alerts.sent[0][1]


async def test_the_job_is_idempotent_and_supersedes_on_a_changed_reading():
    conn = FakeResolutionConn()
    await _seal(conn, _exchange())
    _signal(conn, "source.alpha.news", _DONE)
    alerts = _Alerts()
    first = await fr.run_resolution_job(conn, now=SPENT, alert=alerts)
    again = await fr.run_resolution_job(conn, now=SPENT + timedelta(hours=6), alert=alerts)
    assert first["written"] == 1 and again["written"] == 0 and again["unchanged"] == 1
    assert len(conn.records) == 1 and len(alerts.sent) == 1
    assert conn.records[0]["outcome"] == "needs_judgement"   # one publisher, below the bar
    _signal(conn, "source.beta.news", _DONE)
    third = await fr.run_resolution_job(conn, now=SPENT + timedelta(hours=12), alert=alerts)
    assert third["written"] == 1 and len(conn.records) == 2
    assert conn.records[1]["supersedes_id"] == conn.records[0]["id"]
    assert conn.records[1]["outcome"] == "yes"


async def test_dry_run_and_rehearsal_write_nothing():
    conn = FakeResolutionConn()
    await _seal(conn, _exchange())
    _signal(conn, "source.alpha.news", _DONE)
    alerts = _Alerts()
    r = await fr.run_resolution_job(conn, now=SPENT, dry_run=True, alert=alerts)
    assert r["questions"][0]["record"] == "dry_run"
    r = await fr.run_resolution_job(conn, now=SEALED + timedelta(days=5), dry_run=True, as_if_due=True)
    assert r["questions"][0]["outcome"] in ("needs_judgement", "yes", "no", "undetermined")
    assert conn.records == [] and conn.resolutions == {} and alerts.sent == []


async def test_confirmation_writes_the_resolution_and_the_status_moves():
    conn = FakeResolutionConn()
    qid = await _seal(conn, _exchange())
    _signal(conn, "source.alpha.news", _DONE)
    _signal(conn, "source.beta.news", _DONE)
    await fr.run_resolution_job(conn, now=SPENT)
    prop = conn.records[0]
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fr.confirm_proposal(conn, prop["id"], actor=fr.RESOLVER_ACTOR, now=SPENT)
    assert exc.value.code == "operator_required"
    cid = await fr.confirm_proposal(conn, prop["id"], actor="operator:1", now=SPENT)
    conf = next(r for r in conn.records if r["id"] == cid)
    assert conf["record_kind"] == "confirmation" and conf["basis_record_id"] == prop["id"]
    (res,) = conn.resolutions.values()
    assert res["outcome"] == "yes" and res["resolved_by"] == "operator:1"
    assert conf["resolution_id"] == res["id"]
    assert any(ref.startswith("signal:") for ref in res["evidence_refs"])
    assert any(ref.startswith("url:") for ref in res["evidence_refs"])
    assert conn.questions[qid]["status"] == "resolved"


async def test_a_needs_judgement_proposal_is_decided_only_through_an_override():
    conn = FakeResolutionConn()
    qid = await _seal(conn, _exchange())
    await fr.run_resolution_job(conn, now=SPENT)          # nothing anywhere: quiet no-show
    prop = conn.records[0]
    assert prop["outcome"] == "needs_judgement"
    assert prop["judgements"][0]["key"] == "main:quiet_no_show"
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fr.confirm_proposal(conn, prop["id"], actor="operator:1", now=SPENT)
    assert exc.value.code == "needs_judgement"
    with pytest.raises(fl.LedgerRefusal):
        await fr.override_resolution(conn, qid, outcome="no", actor="operator:1", reason=" ", now=SPENT)
    with pytest.raises(fl.LedgerRefusal):
        await fr.override_resolution(conn, qid, outcome="no", actor="resolver:x", reason="r", now=SPENT)
    oid = await fr.override_resolution(
        conn, qid, outcome="no", actor="operator:1", reason="nothing happened; silence is a no-show",
        judgements=["main:quiet_no_show"], basis_record_id=prop["id"], now=SPENT)
    ov = next(r for r in conn.records if r["id"] == oid)
    (log,) = conn.override_rows
    assert ov["override_log_id"] == log["id"] and log["actor"] == "operator:1"
    assert log["object_kind"] == "forecast_question" and "quiet_no_show" in log["reason"]
    assert log["before_sha256"] == prop["content_sha256"]
    (res,) = conn.resolutions.values()
    assert log["after_sha256"] == res["content_sha256"] and res["outcome"] == "no"
    assert conn.questions[qid]["status"] == "resolved"
    with pytest.raises(fl.LedgerRefusal) as exc:
        await fr.override_resolution(conn, qid, outcome="void", actor="operator:1", reason="again", now=SPENT)
    assert exc.value.code == "already_resolved"


async def test_a_correction_is_a_new_row_never_an_edit():
    conn = FakeResolutionConn()
    qid = await _seal(conn, _exchange())
    await fr.run_resolution_job(conn, now=SPENT)
    first = await fr.override_resolution(conn, qid, outcome="no", actor="operator:1", reason="first read",
                                         now=SPENT)
    before = [dict(r) for r in conn.records]
    with pytest.raises(fl.LedgerRefusal):
        await fr.correct_resolution(conn, qid, outcome="void", actor="operator:1", reason="", now=SPENT)
    cid = await fr.correct_resolution(conn, qid, outcome="void", actor="operator:1",
                                      reason="the premise failed before the window", now=SPENT)
    assert conn.records[: len(before)] == before          # nothing earlier changed
    corr = next(r for r in conn.records if r["id"] == cid)
    assert corr["supersedes_id"] == first and corr["reason"].startswith("the premise")
    active = [r for r in conn.resolutions.values() if r["active"]]
    assert len(active) == 1 and active[0]["outcome"] == "void"
    assert conn.questions[qid]["status"] == "void"
    assert [o["action"] for o in conn.overrides] == ["correct"]   # the 0242 store's log row


async def test_series_threshold_settles_by_rule_and_alerts_once():
    conn = FakeResolutionConn()
    qid = await _seal(conn, _series())
    conn.observations.append({"id": "o1", "series_id": "fict.fx1", "valid_from": "2026-03-20", "value": 104.2,
                              "unit": "usd", "source_url": "https://fict.example", "sha256": "b" * 64,
                              "record_time": "2026-03-20"})
    alerts = _Alerts()
    r = await fr.run_resolution_job(conn, now=DUE + timedelta(hours=1), alert=alerts)
    assert r["settled"] == 1
    kinds = [x["record_kind"] for x in conn.records]
    assert kinds == ["proposal", "confirmation"]
    assert conn.records[1]["actor"] == fr.RESOLVER_ACTOR
    (res,) = conn.resolutions.values()
    assert res["outcome"] == "yes" and res["evidence_refs"] == ["observation:o1"]
    assert conn.questions[qid]["status"] == "resolved"
    assert "resolved yes by rule" in alerts.sent[0][0]
    again = await fr.run_resolution_job(conn, now=DUE + timedelta(hours=7), alert=alerts)
    assert again["questions"] == [] and len(alerts.sent) == 1   # resolved: no longer open


async def test_an_unbuilt_kind_is_reported_and_alerted_never_proposed():
    conn = FakeResolutionConn()
    q = fl.ForecastQuestion(slug="FX3-count", question="A fictional count", resolver_kind="count_threshold",
                            resolver_spec={"authority": "Fict", "count": "n", "op": ">=", "value": 1}, due_at=DUE)
    await _seal(conn, q)
    alerts = _Alerts()
    r = await fr.run_resolution_job(conn, now=SPENT, alert=alerts)
    assert r["errors"] == 1 and "SEAMS #76" in r["questions"][0]["error"]
    assert conn.records == [] and "not built" in alerts.sent[0][0]


async def test_the_handler_returns_a_trace_only_receipt():
    conn = FakeResolutionConn()
    await _seal(conn, _exchange())

    class _Deps:
        pg_pool = FakePool(conn)

    out = await handler.handle([], {}, _Deps())
    assert out.finding.data["sub_handler"] == "forecast_resolver"
    assert out.usage["prompt_tokens"] == 0
    empty = await handler.handle([], {}, None)
    assert empty.finding.data["warnings"] == ["forecast_resolver.no_pool"]
    from legba.data.analysts import deterministic
    from legba.data.provenance.kinds import TRACE_ONLY

    assert deterministic.SUB_HANDLERS["forecast_resolver"] is handler.handle
    assert deterministic.OUTPUT_KIND_BY_SUB_HANDLER["forecast_resolver"] is TRACE_ONLY


class _AnchorConn(FakeResolutionConn):
    """The anchor's statements: heads, the ensure, the pending pull, and the
    0262 tree store (the day's anchored root is the first ensured one)."""

    def __init__(self) -> None:
        super().__init__()
        self.anchored: dict = {}
        self.trees: dict = {}
        self.tree_leaves: dict = {}

    async def fetchval(self, sql, *a):
        if sql == ap.ANCHORED_ROOT_SQL:
            return self.anchored.get(a[0])
        return await super().fetchval(sql, *a)

    async def fetch(self, sql, *a):
        if "DISTINCT ON (analyst_id)" in sql or ("DISTINCT ON" in sql and "analyst_traces" in sql):
            return [{"analyst_id": "analyst_a", "receipt_hash": "aa" * 32}]
        if "status = 'pending'" in sql:
            return []
        return await super().fetch(sql, *a)

    async def execute(self, sql, *a):
        if "INSERT INTO receipt_anchors" in sql:
            self.anchored.setdefault(a[0], a[1])
            return "INSERT 0 1"
        if sql == ap.INSERT_LEAVES_SQL:
            self.tree_leaves[a[0]] = list(zip(a[1], a[2], a[3], a[4], a[5]))
            return "INSERT 0 1"
        return await super().execute(sql, *a)

    async def fetchrow(self, sql, *a):
        if sql == ap.INSERT_TREE_SQL:
            if a[0] in self.trees:
                return None
            self.trees[a[0]] = a
            return {"day": a[0]}
        # Migration 0248: the audit-log chain head is the anchor's last leaf.
        if "FROM descriptor_audit_log" in sql:
            return {"chain_seq": 7, "row_hash": "bb" * 32}
        return await super().fetchrow(sql, *a)


async def test_the_receipt_anchor_folds_the_resolution_records():
    conn = _AnchorConn()
    await _seal(conn, _exchange())
    await fr.run_resolution_job(conn, now=SPENT)

    class _Deps:
        pg_pool = FakePool(conn)

    d = (await ra.handle([], {}, _Deps())).finding.data
    # one question + one number + one proposal record
    assert d["ledger_leaf_count"] == 3 and d["warnings"] == []
    rec_leaf = fl.ledger_leaf("forecast_resolution_record", conn.records[0]["id"], conn.records[0]["content_sha256"])
    ledger = [fl.ledger_leaf(r["kind"], r["id"], r["content_sha256"])
              for r in await FakeResolutionConn.fetch(conn, fl.LEDGER_LEAVES_SQL)]
    assert d["root_hash"] == ra.merkle_root(
        [ra.leaf_for("analyst_a", "aa" * 32)] + ledger + [rec_leaf] + [ra.audit_chain_leaf(7, "bb" * 32)]
    ).hex()
    assert d["audit_chain_head"] == {"chain_seq": 7, "row_hash": "bb" * 32}
    # F-1 (0262): the ordered leaves of the anchored tree are stored, and they
    # re-fold to the anchored root.
    assert d["leaves_stored"] == "stored"
    (day, stored), = conn.tree_leaves.items()
    assert [p for p, *_ in stored] == list(range(d["leaf_count"]))
    assert stored[0][2] == "analyst_head"
    assert stored[-1][2:] == ("descriptor_audit_log", "7", "bb" * 32)
    assert ra.merkle_root([bytes.fromhex(h) for _, h, *_ in stored]).hex() == d["root_hash"]
    # A second tick the same day folds the same rows: the tree is not rewritten.
    d2 = (await ra.handle([], {}, _Deps())).finding.data
    assert d2["leaves_stored"] == "already_stored"


async def test_list_due_resolvers_reads_the_ledger_with_the_latest_proposal():
    conn = FakeResolutionConn()
    await _seal(conn, _exchange(), ("operator", 0.7), ("clef_arm", 0.4))
    await fr.run_resolution_job(conn, now=SPENT)
    out = await fr.list_due_resolvers(conn, now=DUE - timedelta(days=3), horizon_days=7)
    (item,) = out["items"]
    assert item["slug"] == "FX1-exchange" and item["days_to_due"] == pytest.approx(3.0, abs=0.01)
    assert {n["issuer_kind"] for n in item["numbers"]} == {"operator", "clef_arm"}
    assert item["latest_proposal"]["outcome"] == "needs_judgement"
    assert (await fr.list_due_resolvers(conn, now=DUE - timedelta(days=30), horizon_days=7))["items"] == []


_REPO = pathlib.Path(__file__).resolve().parents[2]


async def test_the_consult_tool_is_on_every_surface():
    from legba.data.analysts.agency.substrate_read import SUBSTRATE_READ_TOOLS
    from legba.data.analysts.consult_system_prompt import _SYSTEM_PROMPT as CONSULT_SYSTEM_PROMPT
    from legba.data.analysts.consult_tool_routing import _KNOWN_TOOLS, _dispatch_tool
    from legba.data.stack.llm.tool_rounds import TOOL_SCHEMAS

    assert "list_due_resolvers" in _KNOWN_TOOLS and "list_due_resolvers" in SUBSTRATE_READ_TOOLS
    pack = yaml.safe_load((_REPO / "descriptors/action_pack_substrate_read.yaml").read_text())
    assert "list_due_resolvers" in {t["name"] for t in pack["tools"]}
    assert set(TOOL_SCHEMAS["list_due_resolvers"].json_schema["properties"]) == {
        "horizon_days", "include_closed", "limit"}
    assert "list_due_resolvers(" in CONSULT_SYSTEM_PROMPT

    from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort as SubstrateQueryPort

    conn = FakeResolutionConn()
    await _seal(conn, _exchange())
    port = SubstrateQueryPort.__new__(SubstrateQueryPort)
    port._pool = FakePool(conn)
    out = await _dispatch_tool(port, name="list_due_resolvers", args={"horizon_days": 400},
                               scope_predicate=None)
    assert out["horizon_days"] == 120 and isinstance(out["items"], list)
