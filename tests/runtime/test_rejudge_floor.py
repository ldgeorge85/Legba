# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F102 — re-judging an outage's floor critiques (SEAMS #69's apply half).

``legba.runtime.rejudge_floor`` re-runs the critic's verify pass over a floor
critique's STORED finding against the CURRENT judge chain, writes an adjudicated
verdict as a NEW critique that closes the old floor (``superseded_by`` /
``superseded_at``), and never writes a second floor. ``scripts/
rejudge_floor_critiques.py --apply`` drives it behind an explicit ``--limit``.

No network, no DB, no paid call: the REAL verify pass and the REAL critique
writer run over a fake connection that records every statement (and discards
the ones a rolled-back transaction issued); the judge rungs are handler doubles
answering one ``supported`` verdict per claim, or raising the real
``HardLLMFailure`` the provider plane raises.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.stack.llm.base import HardLLMFailure
from legba.runtime import judge_fallback_chain as JC
from legba.runtime import rejudge_floor as RF

R1 = "llm.judge.rejudge_probe_hosted"
WEAK = "llm.verify.slm_8b"
_CLAIM_NUM = re.compile(r"^\s*(\d+)\.\s", re.M)


@pytest.fixture(autouse=True)
def _judge_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    monkeypatch.delenv(JC.JUDGE_WEAK_RUNGS_ENV, raising=False)
    monkeypatch.delenv(JC.JUDGE_CHAIN_DEFAULT_ENV, raising=False)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Answer:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()
        self.raw_response = {"choices": [{}]}


class _Rung:
    """A judge rung double: ``fail`` → raised on every call; else one
    ``supported`` verdict per claim it was asked to grade."""

    def __init__(self, name: str, *, fail: BaseException | None = None) -> None:
        self.subprovider = f"sub-{name}"
        self._cfg = SimpleNamespace(model_name=SimpleNamespace(raw=f"vendor/{name}"))
        self.fail = fail
        self.calls = 0

    async def chat_complete(self, messages, **kw):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        tail = str(messages[-1]["content"]).split("CLAIMS:\n")[-1]
        n = len({int(m) for m in _CLAIM_NUM.findall(tail)})
        return _Answer(json.dumps({"verdicts": ["supported"] * n}))


class _Tx:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn

    async def __aenter__(self) -> "_Tx":
        self.conn.events.append("begin")
        self.conn._pending = []
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        pending, self.conn._pending = self.conn._pending, None
        if exc_type is None:
            self.conn.committed.extend(pending)
            self.conn.events.append("commit")
        else:
            self.conn.events.append("rollback")
        return False


class _FakeConn:
    """Records statements. Inside a transaction they are PENDING until commit
    (a rollback drops them) — so ``committed`` is what the database would hold."""

    def __init__(self, *, update_status: str = "UPDATE 1", select_rows=None,
                 missing_rows=None, has_critique: bool = False) -> None:
        self.committed: list[tuple[str, tuple]] = []
        self.events: list[str] = []
        self.fetches: list[tuple[str, tuple]] = []
        self._pending: list[tuple[str, tuple]] | None = None
        self.update_status = update_status
        self.select_rows = list(select_rows or [])
        self.missing_rows = list(missing_rows or [])
        self.has_critique = has_critique

    def transaction(self) -> _Tx:
        return _Tx(self)

    async def execute(self, sql: str, *args):
        target = self._pending if self._pending is not None else self.committed
        target.append((sql, args))
        if sql.lstrip().startswith("UPDATE"):
            return self.update_status
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args):
        self.fetches.append((sql, args))
        if sql is RF.SELECT_APPLY_SQL:
            return self.select_rows
        if sql is RF.SELECT_MISSING_SQL:
            return self.missing_rows
        return []

    async def fetchrow(self, sql: str, *args):
        self.fetches.append((sql, args))
        return None

    async def fetchval(self, sql: str, *args):
        self.fetches.append((sql, args))
        if sql is RF.HAS_CRITIQUE_SQL:
            return self.has_critique
        return None

    # -- readers over what committed ----------------------------------------

    def inserts(self) -> list[tuple[str, tuple]]:
        return [(s, a) for s, a in self.committed if "INSERT INTO analyst_outputs" in s]

    def updates(self) -> list[tuple[str, tuple]]:
        return [(s, a) for s, a in self.committed if s.lstrip().startswith("UPDATE")]


def _insert_data(args: tuple) -> dict:
    for a in args:
        if isinstance(a, str) and a.lstrip().startswith("{"):
            parsed = json.loads(a)
            if "overall_score" in parsed:
                return parsed
    raise AssertionError("no critique payload in the INSERT")


def _floor_row(*, kind_data: dict | None = None, finding_id: UUID | None = None,
               analyst: str = "rejudge_probe_unit") -> RF.FloorCritique:
    fid = finding_id if finding_id is not None else uuid4()
    data = kind_data if kind_data is not None else {"citations": [{
        "marker": "[1]", "signal_id": str(uuid4()),
        "title": "Lira drops 3% on the day",
        "source_text": "The lira fell three percent on Monday.",
    }]}
    return RF.FloorCritique(
        critique_id=uuid4(),
        created_at=datetime(2026, 10, 18, 4, tzinfo=timezone.utc),
        run_id=uuid4(),
        analyst_id=analyst,
        analyst_version="1.0.0",
        critique_data={
            "overall_score": 0.85,
            "analyzed_output_id": str(fid),
            "analyzed_analyst_id": analyst,
            "analyzed_analyst_version": "1.0.0",
            "analyzed_model": "llm.primary.openai_compat",
            "data": {"verification": {
                "judge_status": "deterministic",
                "judge_unavailable_reason": "judge_error",
            }},
        },
        finding_id=fid,
        finding_title="Lira",
        finding_body="## Key developments\n- The lira fell three percent on Monday [1].\n",
        finding_confidence=0.8,
        finding_data=data,
        finding_target_id="country_g20_tr",
        finding_run_id=uuid4(),
    )


def _wiring(judge: Any, *, kind: str = "inline_target") -> RF.AnalystJudge:
    return RF.AnalystJudge(kind=kind, judge=judge, judge_llm_ref=R1,
                           judge_route="configured")


def _chain(*rungs: _Rung, refs: tuple[str, ...]) -> JC.JudgeChain:
    by_ref = dict(zip(refs, rungs))

    async def factory(ref: str):
        return by_ref[ref]

    return JC.JudgeChain(refs, first_handler=rungs[0], factory=factory,
                         route_class="configured")


# ---------------------------------------------------------------------------
# 1. Selection — still floor-only, unsuperseded, oldest first, bounded
# ---------------------------------------------------------------------------


async def test_selection_binds_the_window_reasons_analyst_and_limit_in_order():
    fid, cid, run = uuid4(), uuid4(), uuid4()
    record = {
        "critique_id": cid,
        "critique_created_at": datetime(2026, 10, 17, 1, tzinfo=timezone.utc),
        "critique_run_id": run,
        "critique_analyst_id": "a1",
        "critique_analyst_version": "v1",
        # asyncpg hands jsonb back as text without a codec — both are coerced.
        "critique_data": json.dumps({"overall_score": 0.85,
                                     "analyzed_analyst_id": "a1"}),
        "finding_id": fid,
        "finding_title": "T",
        "finding_body": "B [1].",
        "finding_confidence": 0.7,
        "finding_payload_data": json.dumps({"citations": [], "eval": {"x": 1}}),
        "finding_target_id": "country_g20_tr",
        "finding_run_id": run,
    }
    conn = _FakeConn(select_rows=[record])
    since = datetime(2026, 10, 17, tzinfo=timezone.utc)
    until = datetime(2026, 10, 23, 3, 8, tzinfo=timezone.utc)
    rows = await RF.select_floor_critiques(conn, since=since, until=until, limit=7,
                                           analyst_id="a1")
    sql, args = conn.fetches[0]
    assert sql is RF.SELECT_APPLY_SQL
    assert args == (since, until, "Faithfulness verify%", ["judge_error", "judge_empty"],
                    "a1", 7)
    [row] = rows
    assert row.critique_id == cid and row.finding_id == fid
    assert row.old_overall_score == pytest.approx(0.85)
    assert row.analyzed_analyst_id == "a1"
    assert row.finding_data == {"citations": [], "eval": {"x": 1}}
    assert row.finding_run_id == run


def test_selection_sql_is_still_floor_only_unsuperseded_and_oldest_first():
    sql = " ".join(RF.SELECT_APPLY_SQL.split())
    # Only the asked-and-unanswered floors.
    assert "->>'judge_status' = 'deterministic'" in sql
    assert "->>'judge_unavailable_reason' = ANY($4::text[])" in sql
    # Never a row already closed, never one a LATER critique replaced (the read
    # fold's own order: produced_at DESC, id DESC).
    assert "o.superseded_by IS NULL" in sql
    assert "NOT EXISTS" in sql and "(n.produced_at, n.id) > (o.produced_at, o.id)" in sql
    assert "ORDER BY o.created_at ASC, o.id ASC" in sql
    assert sql.rstrip().endswith("LIMIT $6")
    # The close is guarded the same way: a closed row is never re-pointed.
    close = " ".join(RF.SUPERSEDE_SQL.split())
    assert "SET superseded_by = $2, superseded_at = now()" in close
    assert "AND superseded_by IS NULL" in close and "kind = 'critique'" in close


# ---------------------------------------------------------------------------
# 2. The supersession write
# ---------------------------------------------------------------------------


async def test_an_answering_judge_writes_a_new_critique_that_closes_the_floor():
    conn = _FakeConn()
    row = _floor_row()
    rung = _Rung("hosted")
    out = await RF.rejudge_one(conn, row, _wiring(rung))

    assert out.outcome == RF.OUTCOME_REJUDGED, out.detail
    assert rung.calls >= 1
    assert conn.events == ["begin", "commit"]
    [(ins_sql, ins_args)] = conn.inserts()
    new_id = ins_args[0]
    assert out.new_critique_id == new_id
    data = _insert_data(ins_args)
    assert data["analyzed_output_id"] == str(row.finding_id)
    assert str(row.finding_id) in [str(x) for x in next(
        a for a in ins_args if isinstance(a, list))]  # derived_from = [finding]
    assert data["title"].startswith("Faithfulness verify")
    v = data["data"]["verification"]
    assert v["rejudge_of"] == str(row.critique_id)
    assert v["judge_trigger"] == "rejudge_floor"
    assert v["judge_status"] == "llm" and v["provisional"] is False
    assert v["judge_llm_ref"] == R1 and data["judge_llm_ref"] == R1
    assert out.judge_status == "llm" and out.judge_llm_ref == R1
    assert out.old_score == pytest.approx(0.85)
    assert out.new_score == pytest.approx(data["overall_score"])
    # The unit path's uncited-share stamp rides along, as on the critic's row.
    assert v["fact_sentences"] == 1 and v["uncited_fact_sentences"] == 0
    # The old floor is CLOSED by the new id — and nothing else is updated.
    [(upd_sql, upd_args)] = conn.updates()
    assert upd_sql is RF.SUPERSEDE_SQL
    assert upd_args == (row.critique_id, new_id)
    # The finding itself is never written.
    for sql, args in conn.committed:
        assert row.finding_id not in args, sql


async def test_a_later_chain_rung_that_answers_is_stamped_and_written():
    r1 = _Rung("hosted", fail=HardLLMFailure("payment required", status=402))
    r2 = _Rung("weak")
    conn = _FakeConn()
    row = _floor_row()
    out = await RF.rejudge_one(conn, row, _wiring(_chain(r1, r2, refs=(R1, WEAK))))
    assert out.outcome == RF.OUTCOME_REJUDGED
    v = _insert_data(conn.inserts()[0][1])["data"]["verification"]
    assert v["judge_chain"]["used"] == WEAK and v["judge_chain"]["weak"] is True
    assert v["judge_route"] == f"chain:{WEAK}" and out.judge_llm_ref == WEAK
    assert v["rejudge_of"] == str(row.critique_id)


async def test_skip_weak_never_supersedes_with_the_weak_rung():
    r1 = _Rung("hosted", fail=HardLLMFailure("payment required", status=402))
    r2 = _Rung("weak")
    conn = _FakeConn()
    out = await RF.rejudge_one(
        conn, _floor_row(), _wiring(_chain(r1, r2, refs=(R1, WEAK))), accept_weak=False,
    )
    assert out.outcome == RF.OUTCOME_WEAK_SKIPPED
    assert out.detail == f"weak_rung:{WEAK}"
    assert conn.committed == [] and conn.events == []


async def test_a_row_closed_meanwhile_rolls_the_new_critique_back():
    conn = _FakeConn(update_status="UPDATE 0")
    out = await RF.rejudge_one(conn, _floor_row(), _wiring(_Rung("hosted")))
    assert out.outcome == RF.OUTCOME_RACED
    assert out.new_critique_id is None and out.new_score is None
    assert conn.events == ["begin", "rollback"]
    assert conn.committed == []  # the INSERT went with the rollback


# ---------------------------------------------------------------------------
# 3. Never a second floor
# ---------------------------------------------------------------------------


async def test_a_judge_that_still_fails_writes_nothing_and_is_counted():
    rung = _Rung("hosted", fail=HardLLMFailure("payment required", status=402))
    conn = _FakeConn()
    row = _floor_row()
    out = await RF.rejudge_one(conn, row, _wiring(rung))
    assert rung.calls >= 1  # the judge WAS asked
    assert out.outcome == RF.OUTCOME_JUDGE_FAILED
    assert out.judge_status == "deterministic"
    assert out.detail == "judge_error"
    assert out.new_critique_id is None and out.new_score is None
    assert conn.committed == [] and conn.events == []


async def test_a_judge_flag_off_rerun_is_a_floor_and_writes_nothing(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "0")
    conn = _FakeConn()
    out = await RF.rejudge_one(conn, _floor_row(), _wiring(_Rung("hosted")))
    assert out.outcome == RF.OUTCOME_JUDGE_FAILED
    assert conn.committed == []


async def test_a_verify_error_or_timeout_writes_nothing():
    async def boom(**_kw):
        raise TimeoutError("hung rung")

    conn = _FakeConn()
    out = await RF.rejudge_one(conn, _floor_row(), _wiring(_Rung("x")), verify_fn=boom)
    assert out.outcome == RF.OUTCOME_JUDGE_FAILED
    assert out.detail == "verify_error:TimeoutError"
    assert conn.committed == []


async def test_no_judge_wired_is_a_failure_not_a_floor():
    conn = _FakeConn()
    out = await RF.rejudge_one(conn, _floor_row(), RF.AnalystJudge(kind="inline_target",
                                                                   judge=None))
    assert out.outcome == RF.OUTCOME_JUDGE_FAILED and out.detail == "no_judge_wired"
    assert conn.committed == []


# ---------------------------------------------------------------------------
# 4. The critic's scope guard, and the verify arguments it builds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind, data, reason", [
    ("journal_assessor", None, "journal_kind:journal_assessor"),
    ("meta_findings_synthesizer", {"eval": {}}, "no_citations"),
    ("critic", None, "kind_not_verified:critic"),
])
async def test_rows_the_critic_would_not_grade_are_skipped_unjudged(kind, data, reason):
    called = []

    async def never(**kw):
        called.append(kw)
        raise AssertionError("verify must not run")

    conn = _FakeConn()
    out = await RF.rejudge_one(
        conn, _floor_row(kind_data=data), _wiring(_Rung("x"), kind=kind), verify_fn=never,
    )
    assert out.outcome == RF.OUTCOME_SKIPPED and out.detail == reason
    assert called == [] and conn.committed == []


async def test_a_missing_finding_is_skipped():
    row = _floor_row()
    row = RF.FloorCritique(**{**row.__dict__, "finding_id": None})
    out = await RF.rejudge_one(_FakeConn(), row, _wiring(_Rung("x")))
    assert out.outcome == RF.OUTCOME_SKIPPED and out.detail == "finding_missing"


def test_verify_kwargs_are_the_critics_over_the_stored_finding():
    conn = object()
    judge = object()
    data = {"citations": [{"marker": "[1]"}], "indicators": [{"i": 1}],
            "eval": {"e": 1}, "assembly": {"schema": "assembly.v1"}}
    row = _floor_row(kind_data=data)
    unit = RF.verify_kwargs(row, kind="inline_target", judge_llm=judge, conn=conn)
    assert unit == {
        "body": row.finding_body, "citations": data["citations"], "judge_llm": judge,
        "finding_confidence": None,  # T7 stays off on the unit path
        "indicators": data["indicators"], "title": row.finding_title,
        "target_id": row.finding_target_id, "facts_conn": conn, "slice_conn": conn,
        "run_id": row.finding_run_id,  # the finding's OWN run → its retained slice
        "eval_block": data["eval"], "assembly": data["assembly"],
        "judge_sampling": None,
    }
    comp = RF.verify_kwargs(row, kind="meta_findings_synthesizer", judge_llm=judge,
                            conn=conn)
    assert comp["finding_confidence"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# 5. The run — one wiring per analyst, a stop when the chain is down again
# ---------------------------------------------------------------------------


async def test_apply_stops_after_consecutive_failures_and_resolves_once_per_analyst():
    resolved: list[str] = []
    dead = _Rung("hosted", fail=HardLLMFailure("payment required", status=402))

    async def resolve(aid: str):
        resolved.append(aid)
        return _wiring(dead)

    rows = [_floor_row() for _ in range(5)]
    conn = _FakeConn()
    seen: list[RF.RowOutcome] = []
    summary = await RF.apply(conn, rows, resolve, max_consecutive_failures=3,
                             on_row=seen.append)
    assert resolved == ["rejudge_probe_unit"]
    assert len(summary.outcomes) == 3 and len(seen) == 3
    assert summary.counts() == {RF.OUTCOME_JUDGE_FAILED: 3}
    assert summary.stopped_early and "3 consecutive" in summary.stopped_early
    assert conn.committed == []
    text = RF.render_summary(summary)
    assert "selected=5 processed=3" in text and "STOPPED EARLY" in text


async def test_apply_writes_each_answered_row_and_summarises():
    async def resolve(aid: str):
        return _wiring(_Rung("hosted"))

    rows = [_floor_row(), _floor_row()]
    conn = _FakeConn()
    summary = await RF.apply(conn, rows, resolve)
    assert summary.counts() == {RF.OUTCOME_REJUDGED: 2}
    assert len(conn.inserts()) == 2 and len(conn.updates()) == 2
    d = summary.as_dict()
    assert d["mean_old_score"] == pytest.approx(0.85) and d["stopped_early"] is None
    line = RF.render_row(summary.outcomes[0])
    assert "0.85 →" in line and "rejudged" in line and f"judge_llm_ref={R1}" in line


# ---------------------------------------------------------------------------
# 6. The script's refusals — before any read or write
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rejudge_floor_critiques.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rejudge_floor_critiques_f102", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["rejudge_floor_critiques_f102"] = module
    spec.loader.exec_module(module)
    return module


SCRIPT = _load_script()


@pytest.mark.parametrize("argv, needle", [
    (["--apply"], "explicit --limit"),
    (["--apply", "--limit", "501"], "at most 500"),
    (["--apply", "--limit", "0"], "--limit must be >= 1"),
    (["--apply", "--dry-run", "--limit", "5"], "exclusive"),
    (["--skip-weak"], "need --apply"),
    (["--apply", "--limit", "5", "--max-consecutive-failures", "-1"], ">= 0"),
])
def test_script_refusals(argv, needle):
    args = SCRIPT.build_parser().parse_args(argv)
    refusal = SCRIPT.check_args(args)
    assert refusal is not None and needle in refusal


def test_script_apply_refuses_with_the_judge_flag_off(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "0")
    args = SCRIPT.build_parser().parse_args(["--apply", "--limit", "5"])
    assert "LEGBA_VERIFY_LLM_JUDGE" in SCRIPT.check_args(args)


def test_script_apply_with_a_limit_and_the_judge_on_is_allowed():
    args = SCRIPT.build_parser().parse_args(["--apply", "--limit", "50"])
    assert SCRIPT.check_args(args) is None
    assert SCRIPT.check_args(SCRIPT.build_parser().parse_args([])) is None


async def test_script_main_apply_without_limit_exits_before_any_connection():
    with pytest.raises(SystemExit) as exc:
        await SCRIPT.main(["--apply"])
    assert exc.value.code == 2


def test_script_dry_run_render_names_the_built_seam_and_the_preview():
    out = {
        "since": "a", "until": "b", "analyst_id": None, "days": [],
        "floor_critiques": 0, "still_floor_only": 0,
        "would_rejudge": [{"created_at": "t", "critique_id": "c", "finding_id": "f",
                           "analyzed_analyst_id": "x", "old_score": 0.85}],
    }
    text = SCRIPT.render(out)
    assert "SEAMS #69" in text and "--apply --limit N" in text
    assert "critique=c" in text and "would re-judge" in text


# ---------------------------------------------------------------------------
# 7. W11-F84 — the MISSING mode: findings whose verify leg never ran
# ---------------------------------------------------------------------------


def _missing_row(*, kind_data: dict | None = None,
                 analyst: str = "rejudge_probe_unit") -> RF.MissingFinding:
    data = kind_data if kind_data is not None else {"citations": [{
        "marker": "[1]", "signal_id": str(uuid4()),
        "title": "Lira drops 3% on the day",
        "source_text": "The lira fell three percent on Monday.",
    }]}
    return RF.MissingFinding(
        finding_id=uuid4(),
        produced_at=datetime(2026, 10, 24, 9, tzinfo=timezone.utc),
        finding_run_id=uuid4(),
        analyst_id=analyst,
        analyst_version="2.0.0",
        finding_title="Lira",
        finding_body="## Key developments\n- The lira fell three percent on Monday [1].\n",
        finding_confidence=0.8,
        finding_data=data,
        finding_target_id="country_g20_tr",
    )


def _bwiring(judge: Any, *, kind: str = "inline_target") -> RF.AnalystJudge:
    return RF.AnalystJudge(kind=kind, judge=judge, judge_llm_ref=R1,
                           judge_route="configured",
                           analyzed_model="llm.primary.openai_compat")


async def test_missing_selection_binds_window_age_scope_exclusions_and_limit():
    fid, run = uuid4(), uuid4()
    record = {
        "finding_id": fid,
        "finding_produced_at": datetime(2026, 10, 24, 8, tzinfo=timezone.utc),
        "finding_run_id": run,
        "finding_analyst_id": "a1",
        "finding_analyst_version": "v3",
        "finding_title": "T",
        "finding_body": "B [1].",
        "finding_confidence": 0.7,
        "finding_payload_data": json.dumps({"citations": [{"marker": "[1]"}]}),
        "finding_target_id": "country_g20_tr",
    }
    conn = _FakeConn(missing_rows=[record])
    since = datetime(2026, 10, 17, tzinfo=timezone.utc)
    until = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    parked = uuid4()
    rows = await RF.select_missing_findings(
        conn, since=since, until=until, limit=9, min_age_minutes=45, analyst_id="a1",
        exclude=[str(parked)],
    )
    sql, args = conn.fetches[0]
    assert sql is RF.SELECT_MISSING_SQL
    assert args == (since, until, 45, "Faithfulness verify%", "a1", "region_rollup.v1",
                    [parked], 9)
    [row] = rows
    assert row.finding_id == fid and row.finding_run_id == run
    assert row.analyst_id == "a1" and row.analyzed_analyst_id == "a1"
    assert row.analyst_version == "v3"
    assert row.finding_data == {"citations": [{"marker": "[1]"}]}


def test_missing_sql_is_cited_covered_critique_less_aged_and_oldest_first():
    sql = " ".join(RF.SELECT_MISSING_SQL.split())
    assert "f.kind = 'finding'" in sql
    # The age bound: never a finding whose verify leg may still be running.
    assert "f.produced_at < LEAST($2::timestamptz, now() - make_interval(mins => $3::int))" in sql
    assert "f.produced_at >= $1::timestamptz" in sql
    # Has citations — a non-empty array, never jsonb_array_length on a scalar.
    assert ("CASE WHEN jsonb_typeof(f.data->'data'->'citations') = 'array' "
            "THEN jsonb_array_length(f.data->'data'->'citations') ELSE 0 END) > 0") in sql
    # The critic's scope: not the rollup (D-5), not superseded, an analyst the
    # critic covers (it has faithfulness critiques in the window).
    assert "COALESCE(f.data->'data'->'rollup'->>'schema', '') <> $6::text" in sql
    assert "f.superseded_by IS NULL" in sql
    assert ("f.analyst_id IN ( SELECT DISTINCT c.data->>'analyzed_analyst_id' "
            "FROM public.analyst_outputs c WHERE c.kind = 'critique' "
            "AND c.title LIKE $4::text AND c.produced_at >= $1::timestamptz )") in sql
    # NO faithfulness critique at all — on the indexed expression.
    # set-based (the critic guard forbids the per-row correlated probe on
    # analyzed_output_id): the window's critiqued ids as one hashed set.
    assert ("f.id::text NOT IN ( SELECT n.data->>'analyzed_output_id' "
            "FROM public.analyst_outputs n WHERE n.kind = 'critique' "
            "AND n.title LIKE $4::text AND n.produced_at >= $1::timestamptz "
            "AND n.data->>'analyzed_output_id' IS NOT NULL )") in sql
    assert "analyzed_output_id' = f.id::text" not in sql
    assert "NOT (f.id = ANY($7::uuid[]))" in sql
    assert "ORDER BY f.produced_at ASC, f.id ASC" in sql
    assert sql.rstrip().endswith("LIMIT $8")
    # The counts read the SAME predicate.
    for other in (RF.COUNT_MISSING_SQL, RF.COUNT_MISSING_BY_DAY_SQL):
        assert " ".join(RF._MISSING_FROM_WHERE.split()) in " ".join(other.split())


async def test_missing_counts_bind_the_same_args_without_exclusions():
    conn = _FakeConn()
    since = datetime(2026, 10, 17, tzinfo=timezone.utc)
    until = datetime(2026, 10, 24, tzinfo=timezone.utc)
    assert await RF.count_missing(conn, since=since, until=until) == 0
    assert await RF.count_missing_by_day(conn, since=since, until=until) == []
    (s1, a1), (s2, a2) = conn.fetches
    assert s1 is RF.COUNT_MISSING_SQL and s2 is RF.COUNT_MISSING_BY_DAY_SQL
    assert a1 == a2 == (since, until, RF.DEFAULT_MIN_AGE_MINUTES, "Faithfulness verify%",
                        None, "region_rollup.v1", [])


async def test_a_backfill_writes_the_critics_critique_with_its_stamps():
    conn = _FakeConn()
    row = _missing_row()
    rung = _Rung("hosted")
    out = await RF.backfill_one(conn, row, _bwiring(rung))

    assert out.outcome == RF.OUTCOME_BACKFILLED, out.detail
    assert rung.calls >= 1
    assert out.critique_id is None and out.old_score is None
    assert conn.events == ["begin", "commit"]
    # The advisory lock and the re-check ran INSIDE the transaction, first.
    (lock_sql, lock_args), (ins_sql, ins_args) = conn.committed
    assert lock_sql is RF.BACKFILL_LOCK_SQL
    assert lock_args == (f"verify_backfill:{row.finding_id}",)
    assert (RF.HAS_CRITIQUE_SQL, (str(row.finding_id), "Faithfulness verify%")) in conn.fetches
    assert "INSERT INTO analyst_outputs" in ins_sql
    assert out.new_critique_id == ins_args[0]
    data = _insert_data(ins_args)
    assert data["title"].startswith("Faithfulness verify")
    assert data["analyzed_output_id"] == str(row.finding_id)
    assert data["analyzed_analyst_id"] == row.analyst_id
    assert data["analyzed_analyst_version"] == "2.0.0"
    assert data["analyzed_model"] == "llm.primary.openai_compat"
    assert str(row.finding_id) in [str(x) for x in next(
        a for a in ins_args if isinstance(a, list))]  # derived_from = [finding]
    # The critic's analyst_ctx: the finding's analyst, version and run.
    assert row.analyst_id in ins_args and "2.0.0" in ins_args
    assert row.finding_run_id in ins_args
    v = data["data"]["verification"]
    assert v["judge_trigger"] == "verify_backfill"
    assert v["backfill_of_run"] == str(row.finding_run_id)
    assert "rejudge_of" not in v
    assert v["judge_status"] == "llm" and v["provisional"] is False
    assert v["judge_llm_ref"] == R1
    assert v["fact_sentences"] == 1  # the unit path's uncited-share stamp
    # Nothing is superseded, and the finding itself is never written.
    assert conn.updates() == []
    assert out.new_score == pytest.approx(data["overall_score"])


async def test_a_backfill_judge_failure_writes_nothing():
    rung = _Rung("hosted", fail=HardLLMFailure("payment required", status=402))
    conn = _FakeConn()
    out = await RF.backfill_one(conn, _missing_row(), _bwiring(rung))
    assert rung.calls >= 1
    assert out.outcome == RF.OUTCOME_JUDGE_FAILED and out.detail == "judge_error"
    assert out.new_critique_id is None and out.new_score is None
    assert conn.committed == [] and conn.events == []


async def test_a_backfill_with_the_judge_flag_off_or_no_judge_writes_nothing(monkeypatch):
    conn = _FakeConn()
    out = await RF.backfill_one(conn, _missing_row(),
                                RF.AnalystJudge(kind="inline_target", judge=None))
    assert out.outcome == RF.OUTCOME_JUDGE_FAILED and out.detail == "no_judge_wired"
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "0")
    out = await RF.backfill_one(conn, _missing_row(), _bwiring(_Rung("hosted")))
    assert out.outcome == RF.OUTCOME_JUDGE_FAILED
    assert conn.committed == []


async def test_a_critique_that_landed_meanwhile_wins_and_nothing_is_written():
    conn = _FakeConn(has_critique=True)
    out = await RF.backfill_one(conn, _missing_row(), _bwiring(_Rung("hosted")))
    assert out.outcome == RF.OUTCOME_RACED and out.new_critique_id is None
    assert out.new_score is None
    assert conn.events == ["begin", "rollback"] and conn.committed == []


@pytest.mark.parametrize("kind, data, reason", [
    ("journal_assessor", None, "journal_kind:journal_assessor"),
    ("meta_findings_synthesizer", {"eval": {}}, "no_citations"),
    ("critic", None, "kind_not_verified:critic"),
    ("inline_target", {"citations": [{"marker": "[1]"}],
                       "rollup": {"schema": "region_rollup.v1"}}, "deterministic_rollup"),
])
async def test_the_backfill_honours_the_critics_kind_guard(kind, data, reason):
    async def never(**kw):
        raise AssertionError("verify must not run")

    conn = _FakeConn()
    out = await RF.backfill_one(conn, _missing_row(kind_data=data),
                                _bwiring(_Rung("x"), kind=kind), verify_fn=never)
    assert out.outcome == RF.OUTCOME_SKIPPED and out.detail == reason
    assert conn.committed == []


async def test_apply_backfills_each_row_on_its_own_connection_and_summarises():
    conns: list[_FakeConn] = []

    class _Acq:
        async def __aenter__(self):
            conns.append(_FakeConn())
            return conns[-1]

        async def __aexit__(self, *a):
            return False

    async def resolve(aid: str):
        return _bwiring(_Rung("hosted"))

    summary = await RF.apply(None, [_missing_row(), _missing_row()], resolve,
                             acquire=_Acq)
    assert summary.mode == "backfill"
    assert summary.counts() == {RF.OUTCOME_BACKFILLED: 2}
    assert len(conns) == 2 and all(len(c.inserts()) == 1 for c in conns)
    d = summary.as_dict()
    assert d["mean_old_score"] is None and d["mean_new_score"] is not None
    assert d["rows"][0]["critique_id"] is None
    text = RF.render_summary(summary)
    assert text.startswith("verify backfill: selected=2 processed=2")
    assert "(no critique)" in RF.render_row(summary.outcomes[0])


async def test_apply_stops_starting_rows_past_its_wall_clock():
    async def resolve(aid: str):
        return _bwiring(_Rung("hosted"))

    summary = await RF.apply(_FakeConn(), [_missing_row()], resolve, max_seconds=0)
    assert summary.outcomes == [] and "run budget" in summary.stopped_early


def test_primary_model_ref_mirrors_the_critics_extraction():
    assert RF.primary_model_ref({"primary": {"raw": "llm.primary.x"}}) == "llm.primary.x"
    assert RF.primary_model_ref({"primary": "llm.primary.y"}) == "llm.primary.y"
    assert RF.primary_model_ref({}) == "" and RF.primary_model_ref(None) == ""


@pytest.mark.parametrize("argv, needle", [
    (["--missing", "--list", "5"], "--list is the floor mode"),
    (["--min-age-minutes", "40"], "needs --missing"),
    (["--missing", "--min-age-minutes", "-1"], ">= 0"),
    (["--missing", "--apply"], "explicit --limit"),
    (["--missing", "--apply", "--limit", "501"], "at most 500"),
])
def test_script_missing_refusals(argv, needle):
    args = SCRIPT.build_parser().parse_args(argv)
    refusal = SCRIPT.check_args(args)
    assert refusal is not None and needle in refusal


def test_script_missing_apply_with_a_limit_is_allowed():
    args = SCRIPT.build_parser().parse_args(
        ["--missing", "--apply", "--limit", "50", "--min-age-minutes", "60"])
    assert SCRIPT.check_args(args) is None
    assert SCRIPT._min_age(args) == 60
    assert SCRIPT._min_age(SCRIPT.build_parser().parse_args(["--missing"])) == 30


async def test_script_missing_dry_run_lists_per_utc_day_and_the_preview():
    from datetime import date

    class _Conn(_FakeConn):
        async def fetch(self, sql, *args):
            self.fetches.append((sql, args))
            if sql is RF.COUNT_MISSING_BY_DAY_SQL:
                return [{"day": date(2026, 10, 23), "missing": 4},
                        {"day": date(2026, 10, 24), "missing": 2}]
            return await super().fetch(sql, *args)

    rec = {
        "finding_id": uuid4(), "finding_run_id": uuid4(),
        "finding_produced_at": datetime(2026, 10, 23, 5, tzinfo=timezone.utc),
        "finding_analyst_id": "a1", "finding_analyst_version": "v",
        "finding_title": "T", "finding_body": "B", "finding_confidence": 0.5,
        "finding_payload_data": {"citations": [{}]}, "finding_target_id": None,
    }
    conn = _Conn(missing_rows=[rec])
    out = await SCRIPT.run_missing(
        conn, since=datetime(2026, 10, 17, tzinfo=timezone.utc),
        until=datetime(2026, 10, 24, 12, tzinfo=timezone.utc), min_age_minutes=30,
        preview_n=10,
    )
    assert out["mode"] == "missing" and out["missing"] == 6
    assert out["days"] == [{"day": "2026-10-23", "missing": 4},
                           {"day": "2026-10-24", "missing": 2}]
    assert out["would_backfill"][0]["finding_id"] == str(rec["finding_id"])
    text = SCRIPT.render_missing(out)
    assert "2026-10-23" in text and "NO faithfulness critique" in text
    assert "--missing --apply --limit N" in text and "would verify" in text
