# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Failure traces on dead runs (§31.1) — a started run ALWAYS leaves a receipt.

Before this landed the ``analyst_traces`` row was written on the SUCCESS path
only (the ``receipt_chain.record(...)`` call that follows the analyst-output
INSERT). A run that exhausted its transient retries — or raised anywhere else
past the substrate read — returned ``TRANSIENT_FAIL`` to the caller and left
NOTHING on disk. Every staleness / run-history read then showed the analyst's
last SUCCESSFUL run, so a fleet failing on every tick was indistinguishable
from a healthy fleet between cadences. Traceless death hid two incidents.

The invariants pinned here:

  * a run that dies with exhausted transient retries writes an
    ``analyst_traces`` row with ``status='failed'`` carrying the error class,
    the classified bucket, the settled outcome and the attempt count;
  * the hard + budget buckets get the same receipt (with their own outcome);
  * a run that already wrote its SUCCESS trace is never re-traced (the table
    is PRIMARY KEY (run_id) — a second row would violate the PK);
  * a failing trace write NEVER masks the original exception;
  * the success path is unchanged — same outcome, same single trace,
    ``status='success'``.

Pure-logic: the fake state manager + stubbed deps resolver from
``test_actor_lifecycle_idempotent.py``, plus the fake asyncpg pool from
``test_trace_only_dispatch.py`` so the REAL
:class:`legba.data.provenance.receipts.RuntimeReceiptChain` drives the INSERT.
No Postgres, no daprd.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from legba.data.provenance.models import FindingPayload
from legba.data.provenance.receipts import RuntimeReceiptChain
from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    MappingBlock,
    MethodBlock,
    RetryBlock,
    SubscriptionBlock,
    TransientRetryPolicy,
    TypeSignature,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Property
from legba.data.stack.llm import (
    BudgetExhausted,
    HardLLMFailure,
    TransientLLMFailure,
)
from legba.runtime import dapr_actors
from legba.runtime.dapr_actors import (
    ACTIVE,
    ActorRunOutcome,
    AnalystActor,
    TRACE_STATUS_FAILED,
    _AnalystDeps,
    _write_failure_trace,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStateManager:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def try_get_state(self, name: str):
        if name in self._store:
            return True, self._store[name]
        return False, None

    async def set_state(self, name: str, value: Any) -> None:
        self._store[name] = value

    async def save_state(self) -> None:
        return None


class _FakeActorId:
    def __init__(self, actor_id: str) -> None:
        self.id = actor_id


class _TraceCapturingConn:
    """Captures the analyst_traces INSERT the real receipt chain issues."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, *_a, **_k):
        return None                      # no prior head → ZERO_HASH

    async def fetchval(self, *_a, **_k):
        return 0                         # zero existing traces

    async def execute(self, query: str, *args):
        assert "INSERT INTO analyst_traces" in query, query
        # Positional order per receipts.py: run_id($1) analyst_id($2)
        # analyst_version($3) target_id($4) cadence_trigger($5) ...
        # status($15) error_payload($16) run_started_at($17) run_ended_at($18)
        self._rows.append(
            {
                "run_id": args[0],
                "analyst_id": args[1],
                "target_id": args[3],
                "cadence_trigger": args[4],
                "output_row_refs": args[12],
                "output_payload": json.loads(args[13]),
                "status": args[14],
                "error_payload": (
                    json.loads(args[15]) if args[15] is not None else None
                ),
                "run_started_at": args[16],
                "run_ended_at": args[17],
            }
        )


class _TracePool:
    """asyncpg-pool shape shared by the actor body AND the receipt chain."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._conn = _TraceCapturingConn(self.rows)

    def acquire(self):
        return self._conn


class _FakeStandardDeps:
    def __init__(self, pool: _TracePool) -> None:
        self.pg_pool = pool
        self.nats_publish = None


class _ExplodingPool:
    """A pool whose every acquire raises — the "Postgres is why the run died"
    shape. Proves the failure-trace write cannot mask the original error."""

    def acquire(self):
        raise RuntimeError("pool is gone")


# ---------------------------------------------------------------------------
# Descriptor / deps scaffolding (mirrors test_actor_lifecycle_idempotent.py)
# ---------------------------------------------------------------------------

_VERSION = "deadbeefdeadbeef" + "0" * 48
_ANALYST_ID = "failure_trace_probe"
_PRIMARY_ID = f"analyst::{_ANALYST_ID}::" + _VERSION[:16]


def _build_descriptor(*, max_attempts: int = 2) -> AnalystDescriptor:
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id=_ANALYST_ID,
            name="failure_trace_probe (trace-on-death test)",
            schema_uri="legba/analyst/1.0.0",
            version=_VERSION,
            kind=AnalystKind.INLINE_TARGET,
            type_signature=TypeSignature(
                input_type="legba.runtime.SignalList",
                output_type="legba.runtime.Finding",
            ),
            state=LifecycleState.ACTIVE,
            owner="failure_trace_test",
        ),
        subscription=SubscriptionBlock(targets=None),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.openai_compat",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1024,
            },
            retry=RetryBlock(
                transient=TransientRetryPolicy(
                    max_attempts=max_attempts,
                    backoff="constant",
                    initial_delay_seconds=0.0,
                    max_delay_seconds=0.0,
                ),
            ),
        ),
        cadence=CadenceBlock(fallback_schedule="*/10 * * * *", cooldown_seconds=0),
        outputs=[],
    )


async def _one_row_read_slice(conn, *, descriptor, target_filter):
    """A non-empty slice so run() reaches dispatch instead of NOOP'ing."""
    return [{"id": str(uuid4()), "target_id": "US", "title": "probe row"}]


def _make_deps(
    descriptor: AnalystDescriptor,
    *,
    run_method,
    pool: _TracePool,
    receipt_chain,
) -> _AnalystDeps:
    return _AnalystDeps.model_construct(
        descriptor=descriptor,
        deps=_FakeStandardDeps(pool),
        run_method=run_method,
        kind_deps=None,
        output_kind=dapr_actors.OutputKind.FINDING,
        budget=None,
        fallback_run_method=None,
        fallback_kind_deps=None,
        primary_llm_ref="",
        fallback_llm_ref="",
        receipt_chain=receipt_chain,
        read_slice=_one_row_read_slice,
    )


def _make_actor() -> AnalystActor:
    actor = object.__new__(AnalystActor)
    actor.id = _FakeActorId(_PRIMARY_ID)
    actor._state_manager = _FakeStateManager()
    return actor


async def _seed_active(actor: AnalystActor) -> None:
    await actor._set_record(
        {
            "actor_id": _PRIMARY_ID,
            "actor_kind": "analyst",
            "descriptor_id": _ANALYST_ID,
            "descriptor_version": _VERSION,
            "lifecycle": ACTIVE,
            "error_count": 0,
        }
    )


@pytest.fixture(autouse=True)
def _reset_deps_registry():
    dapr_actors.clear_deps_registry()
    yield
    dapr_actors.clear_deps_registry()


def _register(deps: _AnalystDeps) -> None:
    async def resolver(actor_id: str):
        return deps

    dapr_actors.register_analyst_deps_resolver(resolver)


async def _drive_failing_run(exc: BaseException, *, max_attempts: int = 2):
    """Run the actor with a run_method that always raises ``exc``."""
    calls = {"n": 0}

    async def _always_raises(*_a, **_k):
        calls["n"] += 1
        raise exc

    pool = _TracePool()
    chain = RuntimeReceiptChain(pool)          # type: ignore[arg-type]
    descriptor = _build_descriptor(max_attempts=max_attempts)
    deps = _make_deps(
        descriptor, run_method=_always_raises, pool=pool, receipt_chain=chain,
    )
    _register(deps)
    actor = _make_actor()
    await _seed_active(actor)
    out = await actor.run({"trigger_kind": "cadence", "target_filter": "US"})
    return out, pool, calls


# ---------------------------------------------------------------------------
# 1) The headline: an exhausted transient run leaves a FAILED trace.
# ---------------------------------------------------------------------------


async def test_transient_exhausted_writes_failed_trace() -> None:
    out, pool, calls = await _drive_failing_run(
        TransientLLMFailure("upstream 503", status=503), max_attempts=2,
    )

    assert out["outcome"] == ActorRunOutcome.TRANSIENT_FAIL.value
    assert calls["n"] == 2, "the transient policy should have burned 2 attempts"

    assert len(pool.rows) == 1, "exactly one analyst_traces row for the dead run"
    row = pool.rows[0]
    assert row["status"] == TRACE_STATUS_FAILED
    assert row["status"] != "success"
    assert row["analyst_id"] == _ANALYST_ID
    assert row["target_id"] == "US"
    assert row["cadence_trigger"] == "cadence"
    # Nothing landed — the trace must not claim otherwise.
    assert row["output_row_refs"] == []
    # run_ended_at is stamped (the column is nullable; a dead run must not
    # leave it NULL or "still running" and "died" look identical).
    assert isinstance(row["run_ended_at"], datetime)
    assert row["run_ended_at"] >= row["run_started_at"]

    err = row["error_payload"]
    assert err is not None
    assert err["error_class"] == "TransientLLMFailure"
    assert err["bucket"] == "transient"
    assert err["outcome"] == ActorRunOutcome.TRANSIENT_FAIL.value
    assert err["attempts_made"] == 2
    assert err["max_attempts"] == 2
    assert "upstream 503" in err["error"]
    # The error also rides output_payload so the receipt hash is computed over
    # real run content rather than an empty dict.
    assert row["output_payload"]["error_class"] == "TransientLLMFailure"


async def test_transient_exhausted_attempt_count_tracks_the_policy() -> None:
    """The attempt count is the DIAGNOSTIC — it separates "the model 500'd
    three times" from "it blew up once and was never retried"."""
    _out, pool, calls = await _drive_failing_run(
        TransientLLMFailure("flaky", status=429), max_attempts=3,
    )
    assert calls["n"] == 3
    assert pool.rows[0]["error_payload"]["attempts_made"] == 3
    assert pool.rows[0]["error_payload"]["max_attempts"] == 3


# ---------------------------------------------------------------------------
# 2) The other two buckets get the same receipt.
# ---------------------------------------------------------------------------


async def test_hard_failure_writes_failed_trace() -> None:
    out, pool, calls = await _drive_failing_run(HardLLMFailure("401", status=401))

    assert out["outcome"] == ActorRunOutcome.HARD_FAIL.value
    assert calls["n"] == 1, "a hard failure must not consume transient retries"
    assert len(pool.rows) == 1
    err = pool.rows[0]["error_payload"]
    assert pool.rows[0]["status"] == TRACE_STATUS_FAILED
    assert err["bucket"] == "hard"
    assert err["outcome"] == ActorRunOutcome.HARD_FAIL.value
    assert err["error_class"] == "HardLLMFailure"


async def test_unknown_exception_writes_failed_trace() -> None:
    """A bare ValueError classifies hard — the descriptor-is-malformed shape.
    This is the case that produced NOTHING at all before."""
    out, pool, _calls = await _drive_failing_run(ValueError("bad descriptor"))

    assert out["outcome"] == ActorRunOutcome.HARD_FAIL.value
    assert len(pool.rows) == 1
    err = pool.rows[0]["error_payload"]
    assert err["error_class"] == "ValueError"
    assert err["bucket"] == "hard"
    assert "bad descriptor" in err["error"]


async def test_budget_exhausted_writes_failed_trace() -> None:
    out, pool, _calls = await _drive_failing_run(BudgetExhausted("over cap"))

    assert out["outcome"] == ActorRunOutcome.BUDGET_THROTTLED.value
    assert len(pool.rows) == 1
    err = pool.rows[0]["error_payload"]
    assert err["bucket"] == "budget"
    assert err["outcome"] == ActorRunOutcome.BUDGET_THROTTLED.value


# ---------------------------------------------------------------------------
# 3) The success path is unchanged.
# ---------------------------------------------------------------------------


async def test_success_path_writes_exactly_one_success_trace() -> None:
    """Byte-identical success behaviour: one trace, status='success', no
    error_payload, and the outcome the caller has always seen."""

    class _Result:
        finding = FindingPayload(
            title="probe finding",
            body="a body long enough to be a body",
            confidence=0.5,
            data={},
        )
        intermediate_steps = None
        tool_calls = None
        prompt_module_hash = None
        prompt_rendered = None

    async def _succeeds(*_a, **_k):
        return _Result()

    pool = _TracePool()
    chain = RuntimeReceiptChain(pool)          # type: ignore[arg-type]
    deps = _make_deps(
        _build_descriptor(),
        run_method=_succeeds,
        pool=pool,
        # TRACE_ONLY-style: no analyst_outputs write path is exercised here;
        # the chain still records the run exactly as production does.
        receipt_chain=chain,
    )
    deps.output_kind = dapr_actors.TRACE_ONLY
    _register(deps)
    actor = _make_actor()
    await _seed_active(actor)

    out = await actor.run({"trigger_kind": "cadence", "target_filter": "US"})

    assert out["outcome"] == ActorRunOutcome.SUCCESS.value
    assert len(pool.rows) == 1, "no extra trace on the success path"
    row = pool.rows[0]
    assert row["status"] == "success"
    assert row["error_payload"] is None


# ---------------------------------------------------------------------------
# 4) The trace write must never mask the original error.
# ---------------------------------------------------------------------------


async def test_failing_trace_write_is_swallowed() -> None:
    """The failure-trace path runs precisely when things are broken — often
    when Postgres itself is why the run died. It must degrade to a log line."""
    chain = RuntimeReceiptChain(_ExplodingPool())   # type: ignore[arg-type]
    wrote = await _write_failure_trace(
        chain,
        run_id=uuid4(),
        analyst_id=_ANALYST_ID,
        analyst_version=_VERSION,
        cadence_trigger="cadence",
        target_id="US",
        exc=TransientLLMFailure("503", status=503),
        bucket_kind="transient",
        attempts_made=3,
        max_attempts=3,
        run_started_at=datetime.now(timezone.utc),
    )
    assert wrote is False


async def test_dead_run_with_exploding_trace_still_returns_its_outcome() -> None:
    """End-to-end: the actor's declared outcome survives a trace-write blowup."""

    async def _always_raises(*_a, **_k):
        raise TransientLLMFailure("503", status=503)

    pool = _TracePool()
    deps = _make_deps(
        _build_descriptor(max_attempts=1),
        run_method=_always_raises,
        pool=pool,
        receipt_chain=RuntimeReceiptChain(_ExplodingPool()),  # type: ignore[arg-type]
    )
    _register(deps)
    actor = _make_actor()
    await _seed_active(actor)

    out = await actor.run({"trigger_kind": "cadence", "target_filter": "US"})
    assert out["outcome"] == ActorRunOutcome.TRANSIENT_FAIL.value
    assert "503" in out["error"]


async def test_no_receipt_chain_degrades_to_noop() -> None:
    """``receipt_chain=None`` is the spike integration-test path — the failure
    trace degrades exactly as the success trace does."""
    wrote = await _write_failure_trace(
        None,
        run_id=uuid4(),
        analyst_id=_ANALYST_ID,
        analyst_version=_VERSION,
        cadence_trigger="method",
        target_id=None,
        exc=ValueError("x"),
        bucket_kind="hard",
        attempts_made=0,
        max_attempts=None,
        run_started_at=datetime.now(timezone.utc),
    )
    assert wrote is False


# ---------------------------------------------------------------------------
# 5) The status vocabulary the partial index was built for.
# ---------------------------------------------------------------------------


def test_failed_status_is_indexed_by_the_partial_status_index() -> None:
    """``analyst_traces_status_idx`` is ``... WHERE status <> 'success'``, so
    the failure vocabulary must not be 'success' or the whole point of the
    index (cheap "show me the dead runs") is lost."""
    assert TRACE_STATUS_FAILED != "success"
    assert TRACE_STATUS_FAILED == "failed"
