# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``verify_backfill`` deterministic sub-handler.

The sweep over cited findings whose verify leg never ran (a rolling deploy
recreated the runtime between the finding's write and its critique's). It runs
``legba.runtime.rejudge_floor``'s missing-mode selection and verify-and-write on
cadence. DETERMINISTIC — a recording fake pool, the REAL verify pass and the REAL
critique writer, and a judge-rung double (one ``supported`` verdict per claim, or
the real ``HardLLMFailure``). No network, no DB. Covered:

  * registration (TRACE_ONLY), the zeroed synthetic run, the no-op ticks (judge
    flag off; no registry / secrets wiring);
  * the declared options (``backfill_batch``, ``min_age_minutes``) and that the
    handler reads them into the selection;
  * the counters — examined / verified / failed / still_missing — over a tick
    that verifies one row and fails another, and the parking rule;
  * the descriptor YAML validates, schedules on a free slot, and the heartbeat
    excludes the analyst;
  * the deps wiring threads the registry client only for this sub-handler.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    TRACE_ONLY,
    run_method,
)
from legba.data.analysts.deterministic_handlers import verify_backfill as vb
from legba.data.option_catalog.handler_options import (
    known_option_names,
    resolve_handler_options,
)
from legba.data.stack.llm.base import HardLLMFailure
from legba.runtime import judge_fallback_chain as JC
from legba.runtime import rejudge_floor as RF
from legba.runtime.analyst_deps_reenrich import (
    _wire_verify_backfill,
    wire_reenrich_kind_deps,
)

SUB = "verify_backfill"
_REPO = Path(__file__).resolve().parents[2]
_YAML = _REPO / "descriptors" / "analyst_verify_backfill.yaml"
_CLAIM_NUM = re.compile(r"^\s*(\d+)\.\s", re.M)


@pytest.fixture(autouse=True)
def _judge_on_and_fresh_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    monkeypatch.delenv(JC.JUDGE_WEAK_RUNGS_ENV, raising=False)
    monkeypatch.delenv(JC.JUDGE_CHAIN_DEFAULT_ENV, raising=False)
    vb._ATTEMPTS.clear()


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
    """Answers ``supported`` for every claim, or fails when the finding body
    the judge is asked about contains ``fail_on``."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.subprovider = "sub-hosted"
        self._cfg = SimpleNamespace(model_name=SimpleNamespace(raw="vendor/hosted"))
        self.fail_on = fail_on
        self.calls = 0

    async def chat_complete(self, messages, **kw):
        self.calls += 1
        text = " ".join(str(m.get("content", "")) for m in messages)
        if self.fail_on and self.fail_on in text:
            raise HardLLMFailure("payment required", status=402)
        tail = str(messages[-1]["content"]).split("CLAIMS:\n")[-1]
        n = len({int(m) for m in _CLAIM_NUM.findall(tail)})
        return _Answer(json.dumps({"verdicts": ["supported"] * n}))


class _Tx:
    def __init__(self, pool: "_FakePool") -> None:
        self.pool = pool

    async def __aenter__(self):
        self.pool._pending = []
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pending, self.pool._pending = self.pool._pending, None
        if exc_type is None:
            self.pool.committed.extend(pending)
        return False


class _FakeConn:
    def __init__(self, pool: "_FakePool") -> None:
        self._pool = pool

    def transaction(self) -> _Tx:
        return _Tx(self._pool)

    async def fetch(self, sql, *args):
        self._pool.fetches.append((sql, args))
        if sql is RF.SELECT_MISSING_SQL:
            return self._pool.rows
        return []

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        self._pool.fetches.append((sql, args))
        if sql is RF.COUNT_MISSING_SQL:
            return self._pool.backlog
        return False  # HAS_CRITIQUE_SQL: nothing landed meanwhile

    async def execute(self, sql, *args):
        target = self._pool._pending if self._pool._pending is not None else self._pool.committed
        target.append((sql, args))
        return "INSERT 0 1"


class _Acquire:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        self._pool.acquired += 1
        return _FakeConn(self._pool)

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, rows, *, backlog=0):
        self.rows = rows
        self.backlog = backlog
        self.fetches: list[tuple] = []
        self.committed: list[tuple] = []
        self._pending: list | None = None
        self.acquired = 0

    def acquire(self):
        return _Acquire(self)

    def inserts(self):
        return [a for s, a in self.committed if "INSERT INTO analyst_outputs" in s]


@dataclass
class _Deps:
    pg_pool: Any = None
    secrets_resolve: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


def _record(body: str = "The lira fell three percent on Monday", analyst: str = "unit_tr"):
    return {
        "finding_id": uuid4(),
        "finding_produced_at": datetime(2026, 10, 24, 6, tzinfo=timezone.utc),
        "finding_run_id": uuid4(),
        "finding_analyst_id": analyst,
        "finding_analyst_version": "1.0.0",
        "finding_title": "Lira",
        "finding_body": f"## Key developments\n- {body} [1].\n",
        "finding_confidence": 0.8,
        "finding_payload_data": {"citations": [{
            "marker": "[1]", "signal_id": str(uuid4()), "title": "Lira",
            "source_text": f"{body}.",
        }]},
        "finding_target_id": "country_g20_tr",
    }


def _wired_deps(pool, monkeypatch, rung, *, resolved=None):
    """Deps with a registry + secrets resolver; the live resolver is replaced by
    one handing back ``rung`` as every analyst's judge (the registry is never
    called)."""
    closed: list[bool] = []

    def fake_resolver(*, registry_client, secrets_resolve):
        async def resolve(aid):
            if resolved is not None:
                resolved.append(aid)
            return RF.AnalystJudge(kind="inline_target", judge=rung,
                                   judge_llm_ref="llm.judge.x", judge_route="configured")

        async def close():
            closed.append(True)

        return resolve, close

    monkeypatch.setattr(RF, "live_judge_resolver", fake_resolver)

    async def secrets(_ref):
        return b""

    deps = _Deps(pg_pool=pool, secrets_resolve=secrets,
                 extras={vb.REGISTRY_DEPS_EXTRA_KEY: object()})
    return deps, closed


async def _run(deps, **options):
    return (await vb.handle([], {"sub_handler": SUB, **options}, deps)).finding.data


# ---------------------------------------------------------------------------
# Registration / synthetic / no-op ticks
# ---------------------------------------------------------------------------


def test_registered_trace_only():
    assert SUB_HANDLERS[SUB] is vb.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER[SUB] is TRACE_ONLY


async def test_synthetic_no_deps_zeroed_run():
    result = await run_method([], {"sub_handler": SUB, "analyst_id": "vb", "run_id": uuid4()},
                              None)
    data = result.finding.data
    assert {k: data[k] for k in ("examined", "verified", "failed", "still_missing")} == {
        "examined": 0, "verified": 0, "failed": 0, "still_missing": 0,
    }
    assert result.usage == {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


async def test_judge_flag_off_is_a_no_op_tick(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "0")
    pool = _FakePool([_record()])
    deps, _ = _wired_deps(pool, monkeypatch, _Rung())
    data = await _run(deps)
    assert data["skipped_no_judge"] == 1 and data["examined"] == 0
    assert pool.fetches == [] and pool.acquired == 0


async def test_no_registry_or_secrets_is_a_no_op_tick():
    pool = _FakePool([_record()])
    data = await _run(_Deps(pg_pool=pool))
    assert data["skipped_no_judge"] == 1 and pool.acquired == 0


# ---------------------------------------------------------------------------
# Options + counters
# ---------------------------------------------------------------------------


def test_both_options_are_declared_and_bounded():
    assert known_option_names(SUB) == ("backfill_batch", "min_age_minutes")
    ok = resolve_handler_options(SUB, {"backfill_batch": 50, "min_age_minutes": 30})
    assert ok.accepted == {"backfill_batch": 50, "min_age_minutes": 30} and not ok.rejected
    for bad in ({"backfill_batch": 0}, {"backfill_batch": 501}, {"backfill_batch": "x"},
                {"min_age_minutes": 5}, {"min_age_minutes": 10081}):
        assert resolve_handler_options(SUB, bad).rejected, bad


async def test_a_tick_verifies_counts_and_reads_its_options(monkeypatch):
    good, bad = _record(), _record(body="Poison claim about the lira")
    pool = _FakePool([good, bad], backlog=7)
    resolved: list[str] = []
    rung = _Rung(fail_on="Poison claim")
    deps, closed = _wired_deps(pool, monkeypatch, rung, resolved=resolved)
    data = await _run(deps, backfill_batch=17, min_age_minutes=45)

    assert data["examined"] == 2 and data["verified"] == 1 and data["failed"] == 1
    assert data["still_missing"] == 7 and data["skipped"] == 0
    assert data["skipped_no_judge"] == 0
    assert resolved == ["unit_tr"] and closed == [True]
    # The options reached the selection: limit and min-age.
    [(sql, args)] = [(s, a) for s, a in pool.fetches if s is RF.SELECT_MISSING_SQL]
    assert args[2] == 45 and args[-1] == 17 and args[6] == []
    since, until = args[0], args[1]
    assert until - since == vb.LOOKBACK
    [(_, cargs)] = [(s, a) for s, a in pool.fetches if s is RF.COUNT_MISSING_SQL]
    assert cargs[2] == 45
    # One critique written, stamped as a backfill of the good finding's run; each
    # row ran on its own pooled connection (+ the selection and the count).
    [ins] = pool.inserts()
    payload = next(json.loads(a) for a in ins if isinstance(a, str) and '"overall_score"' in a)
    v = payload["data"]["verification"]
    assert payload["analyzed_output_id"] == str(good["finding_id"])
    assert v["judge_trigger"] == "verify_backfill"
    assert v["backfill_of_run"] == str(good["finding_run_id"])
    assert pool.acquired == 4
    # The failed row counted an attempt (the tick was healthy); not parked yet.
    assert vb._ATTEMPTS == {bad["finding_id"]: 1} and data["parked"] == 0


async def test_a_poison_row_is_parked_after_max_attempts_and_excluded(monkeypatch):
    good, bad = _record(), _record(body="Poison claim about the lira")
    vb._ATTEMPTS[bad["finding_id"]] = vb.MAX_ATTEMPTS - 1
    pool = _FakePool([good, bad])
    deps, _ = _wired_deps(pool, monkeypatch, _Rung(fail_on="Poison claim"))
    data = await _run(deps)
    assert data["parked"] == 1
    # The next tick leaves it out of the selection.
    pool2 = _FakePool([])
    deps2, _ = _wired_deps(pool2, monkeypatch, _Rung())
    await _run(deps2)
    [(_, args)] = [(s, a) for s, a in pool2.fetches if s is RF.SELECT_MISSING_SQL]
    assert args[6] == [bad["finding_id"]]


async def test_an_all_fail_tick_counts_no_attempts(monkeypatch):
    rows = [_record(body="Poison claim one"), _record(body="Poison claim two")]
    pool = _FakePool(rows, backlog=2)
    deps, _ = _wired_deps(pool, monkeypatch, _Rung(fail_on="Poison claim"))
    data = await _run(deps)
    assert data["verified"] == 0 and data["failed"] == 2
    assert pool.inserts() == [] and vb._ATTEMPTS == {}


def test_the_handler_reads_the_declared_options():
    src = (_REPO / "src/legba/data/analysts/deterministic_handlers/verify_backfill.py").read_text()
    assert 'options.get("backfill_batch"' in src
    assert 'options.get("min_age_minutes"' in src


# ---------------------------------------------------------------------------
# Descriptor, heartbeat, wiring
# ---------------------------------------------------------------------------


def test_descriptor_validates_and_declares_only_catalogued_options():
    from legba.data.schemas.analyst import AnalystDescriptor

    body = yaml.safe_load(_YAML.read_text())
    desc = AnalystDescriptor.model_validate(body)
    assert desc.identity.id == SUB and body["method"]["sub_handler"] == SUB
    assert body["identity"]["kind"] == "deterministic"
    assert "llm" not in body["method"]
    assert body["cadence"]["fallback_schedule"] == "12-59/30 * * * *"
    res = resolve_handler_options(SUB, body["method"]["options"])
    assert res.accepted == {"backfill_batch": 50, "min_age_minutes": 30} and not res.rejected


def test_the_schedule_minutes_are_free_of_every_other_hourly_sweep():
    mine = {12, 42}
    for path in sorted((_REPO / "descriptors").glob("analyst_*.yaml")):
        if path == _YAML:
            continue
        m = re.search(r'^\s*fallback_schedule:\s*"([^"]+)"', path.read_text(), re.M)
        if not m:
            continue
        minute, hour = m.group(1).split()[:2]
        if hour not in ("*", "*/1"):
            continue
        used: set[int] = set()
        for piece in minute.split(","):
            step = 1
            if "/" in piece:
                piece, s = piece.split("/")
                step = int(s)
            lo, hi = (0, 59) if piece == "*" else (
                tuple(map(int, piece.split("-"))) if "-" in piece
                else (int(piece), 59 if step != 1 else int(piece)))
            used.update(range(lo, hi + 1, step))
        assert not (used & mine), f"{path.name} also fires at {sorted(used & mine)}"


def test_the_heartbeat_silence_check_excludes_the_sweep():
    sh = (_REPO / "scripts/host_llm_heartbeat.sh").read_text()
    block = sh.split("analyst_id NOT IN (", 1)[1].split(")", 1)[0]
    assert "'verify_backfill'" in block and "'reenrich_pending'" in block


async def test_wiring_threads_the_registry_only_for_the_bound_sub_handler():
    descriptor = SimpleNamespace(identity=SimpleNamespace(id=SUB))
    registry = object()
    wired = _wire_verify_backfill(descriptor, _Deps(), registry_client=registry)
    assert wired.extras == {vb.REGISTRY_DEPS_EXTRA_KEY: registry}
    bare = _Deps()
    assert _wire_verify_backfill(descriptor, bare, registry_client=None) is bare
    via_dispatch = await wire_reenrich_kind_deps(
        descriptor, _Deps(), sub_handler=SUB, nlp_client=None, registry_client=registry,
    )
    assert via_dispatch.extras == {vb.REGISTRY_DEPS_EXTRA_KEY: registry}
    other = _Deps()
    assert await wire_reenrich_kind_deps(
        descriptor, other, sub_handler="signal_embedder", nlp_client=None,
        registry_client=registry,
    ) is other
