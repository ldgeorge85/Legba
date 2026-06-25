# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-analyst receipt-chain wiring tests.

Covers the L-107 §7 chain head plumbing the actor runtime relies on:

  1. Building a chain for a fresh analyst — head starts at ZERO_HASH.
  2. Building a chain after an existing trace row exists — head hydrates
     from the analyst_traces tail so the next ``record()`` chains via the
     previous receipt_hash.
  3. Chain integrity across N records — each row's prev_receipt_hash
     equals the previous row's receipt_hash, forming a linked list.
  4. ``_AnalystDeps(receipt_chain=None)`` works as before (back-compat
     with the spike integration test, which builds the bundle directly
     without a chain).

The wiring lives in:

  * :mod:`legba.runtime.receipt_chain_factory` — process-global cache.
  * :mod:`legba.data.provenance.receipts` — the chain itself.
  * :mod:`legba.runtime.dapr_actors._AnalystDeps` — the field.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.provenance._core import ZERO_HASH
from legba.data.provenance.kinds import TRACE_ONLY, OutputKind
from legba.data.provenance.receipts import RuntimeReceiptChain
from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    MappingBlock,
    MethodBlock,
    SubscriptionBlock,
    TypeSignature,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.runtime.dapr_actors import _AnalystDeps
from legba.runtime.deps import StandardDeps
from legba.runtime.receipt_chain_factory import (
    build_receipt_chain_for_analyst,
    clear_receipt_chain_cache,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    """Fresh pool against the session test DB (mirrors data_pkg's fixture)."""
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def _drop_receipt_chain_cache():
    """Each test starts with a fresh process-global chain cache.

    The cache lives for the process lifetime in production; for tests we
    want each case to construct its own chain so the head-hydration
    assertions are deterministic across the file's test ordering.
    """
    clear_receipt_chain_cache()
    yield
    clear_receipt_chain_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ANALYST_VERSION_PLACEHOLDER = "0" * 64


def _analyst_descriptor(
    analyst_id: str, *, version: str = _ANALYST_VERSION_PLACEHOLDER,
) -> AnalystDescriptor:
    """Minimal AnalystDescriptor — only identity matters for the wiring test.

    The schema enforces ``version`` matches ``^[a-f0-9]{16,64}$`` and
    requires ``name`` / ``schema_uri`` / ``type_signature`` / ``owner``,
    so we mirror the test_analyst_deps_builder fixture's shape rather
    than open-code something that fails validation.
    """
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id=analyst_id,
            name="receipt-chain wiring test",
            schema_uri="legba/analyst/1.0.0",
            version=version,
            kind=AnalystKind.INLINE_TARGET,
            type_signature=TypeSignature(
                input_type="legba.x.In",
                output_type="legba.x.Out",
            ),
            state=LifecycleState.ACTIVE,
            owner="test",
        ),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.prompts.inline_target.v1",
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
    )


async def _insert_fake_trace_row(
    pg_pool: asyncpg.Pool,
    *,
    analyst_id: str,
    receipt_hash: str,
    prev_receipt_hash: str = ZERO_HASH,
    run_started_at: datetime | None = None,
) -> UUID:
    """Write a synthetic ``analyst_traces`` row so ``head()`` has something
    to hydrate from. Returns the inserted ``run_id``."""
    run_id = uuid4()
    started = run_started_at or datetime.now(tz=timezone.utc) - timedelta(seconds=5)
    ended = started + timedelta(seconds=1)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_traces (
                run_id, analyst_id, analyst_version,
                target_id, cadence_trigger,
                input_row_refs, input_payload,
                prompt_module_hash, prompt_rendered,
                intermediate_steps, llm_calls, tool_calls,
                output_row_refs, output_payload,
                status, error_payload,
                run_started_at, run_ended_at,
                receipt_hash, prev_receipt_hash
            ) VALUES (
                $1, $2, $3,
                $4, $5,
                $6, $7::jsonb,
                $8, $9,
                $10::jsonb, $11::jsonb, $12::jsonb,
                $13, $14::jsonb,
                $15, $16::jsonb,
                $17, $18,
                $19, $20
            )
            """,
            run_id, analyst_id, "v1",
            None, "manual",
            [], json.dumps({}),
            None, None,
            json.dumps([]), json.dumps([]), json.dumps([]),
            [], json.dumps({"seed": True}),
            "success", None,
            started, ended,
            receipt_hash, prev_receipt_hash,
        )
    return run_id


# ---------------------------------------------------------------------------
# Tests: factory + head hydration
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fresh_analyst_chain_starts_at_zero_hash(pg_pool):
    """No prior analyst_traces rows — head hydrates to ZERO_HASH."""
    analyst_id = f"analyst_fresh_{uuid4().hex[:8]}"
    chain = build_receipt_chain_for_analyst(analyst_id, "v1", pg_pool=pg_pool)

    assert isinstance(chain, RuntimeReceiptChain)
    head = await chain.head(analyst_id)
    assert head == ZERO_HASH

    # And the chain reports zero existing traces.
    cnt = await chain.count(analyst_id)
    assert cnt == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chain_hydrates_from_existing_tail(pg_pool):
    """When analyst_traces already has a row, head() returns its receipt_hash.

    The next ``record()`` then chains its prev_receipt_hash to that value.
    """
    analyst_id = f"analyst_hydrate_{uuid4().hex[:8]}"
    # HASH_A — a deterministic 64-char hex marker.
    HASH_A = "a" * 64
    await _insert_fake_trace_row(
        pg_pool,
        analyst_id=analyst_id,
        receipt_hash=HASH_A,
        prev_receipt_hash=ZERO_HASH,
    )

    chain = build_receipt_chain_for_analyst(analyst_id, "v1", pg_pool=pg_pool)
    head = await chain.head(analyst_id)
    assert head == HASH_A

    # Now record the next event — its prev_receipt_hash should equal HASH_A.
    started = datetime.now(tz=timezone.utc)
    receipt, prev = await chain.record(
        run_id=uuid4(),
        analyst_id=analyst_id,
        analyst_version="v1",
        cadence_trigger="method",
        target_id="br_test",
        input_row_refs=[],
        input_payload={},
        prompt_module_hash=None,
        prompt_rendered=None,
        output_row_refs=[],
        output_payload={"value": "next"},
        run_started_at=started,
        run_ended_at=started + timedelta(seconds=1),
    )
    assert prev == HASH_A
    assert receipt != HASH_A
    # And the new receipt is now the head.
    assert await chain.head(analyst_id) == receipt


# ---------------------------------------------------------------------------
# Tests: chain integrity across N records
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chain_integrity_linked_list_shape(pg_pool):
    """Record N events; verify the linked-list shape over the DB rows."""
    analyst_id = f"analyst_integrity_{uuid4().hex[:8]}"
    chain = build_receipt_chain_for_analyst(analyst_id, "v1", pg_pool=pg_pool)

    N = 4
    started_base = datetime.now(tz=timezone.utc) - timedelta(minutes=N)
    recorded: list[tuple[str, str]] = []  # (receipt, prev)
    for i in range(N):
        started = started_base + timedelta(seconds=i)
        receipt, prev = await chain.record(
            run_id=uuid4(),
            analyst_id=analyst_id,
            analyst_version="v1",
            cadence_trigger="method",
            target_id="br_test",
            input_row_refs=[],
            input_payload={"i": i},
            prompt_module_hash=None,
            prompt_rendered=None,
            output_row_refs=[],
            output_payload={"i": i, "k": "v"},
            run_started_at=started,
            run_ended_at=started + timedelta(seconds=1),
        )
        recorded.append((receipt, prev))

    # In-memory shape: first prev is ZERO_HASH, each subsequent prev equals
    # the previous receipt.
    assert recorded[0][1] == ZERO_HASH
    for i in range(1, N):
        assert recorded[i][1] == recorded[i - 1][0], (
            f"chain broken at i={i}: prev={recorded[i][1]!r} but expected "
            f"previous receipt {recorded[i - 1][0]!r}"
        )

    # All N receipts distinct (no accidental hash collision via stable
    # inputs).
    assert len({r for r, _ in recorded}) == N

    # DB shape mirrors the in-memory chain.
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT receipt_hash, prev_receipt_hash
            FROM analyst_traces
            WHERE analyst_id = $1
            ORDER BY run_started_at ASC
            """,
            analyst_id,
        )
    assert len(rows) == N
    assert rows[0]["prev_receipt_hash"] == ZERO_HASH
    for i in range(1, N):
        assert rows[i]["prev_receipt_hash"] == rows[i - 1]["receipt_hash"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_factory_caches_chain_per_analyst(pg_pool):
    """Two calls for the same (pool, analyst_id) return the SAME instance.

    This matters because the chain's in-memory head pointer + per-analyst
    asyncio.Lock must persist across actor runs in the same process; a
    fresh instance per call would reset the head and break the lock's
    sequential-chaining guarantee under concurrent runs.
    """
    analyst_id = f"analyst_cache_{uuid4().hex[:8]}"
    a = build_receipt_chain_for_analyst(analyst_id, "v1", pg_pool=pg_pool)
    b = build_receipt_chain_for_analyst(analyst_id, "v2", pg_pool=pg_pool)
    # Different version arg, same analyst_id — must reuse the chain so
    # version bumps extend the same Mnemosyne D5 chain (per L-107 §7).
    assert a is b

    # Different analyst_id → different chain instance.
    other_id = f"analyst_other_{uuid4().hex[:8]}"
    c = build_receipt_chain_for_analyst(other_id, "v1", pg_pool=pg_pool)
    assert c is not a


# ---------------------------------------------------------------------------
# Tests: _AnalystDeps back-compat
# ---------------------------------------------------------------------------


def test_analyst_deps_back_compat_without_receipt_chain():
    """``_AnalystDeps(receipt_chain=None)`` (the spike's shape) constructs
    cleanly and exposes ``receipt_chain`` as ``None`` for the actor's
    runtime check to short-circuit on."""
    descriptor = _analyst_descriptor(f"analyst_compat_{uuid4().hex[:8]}")

    # Minimal StandardDeps — no pg_pool / NATS / secrets needed; we
    # only assert the bundle constructs without the chain field.
    standard = StandardDeps(
        pg_pool=None,  # type: ignore[arg-type]
        nats_publish=None,
        secrets_resolve=None,
    )

    async def _run_method(inputs, options):  # pragma: no cover — never invoked
        return None

    deps = _AnalystDeps(
        descriptor=descriptor,
        deps=standard,
        run_method=_run_method,
    )
    # receipt_chain defaults to None — the actor's run path checks for
    # this and skips the chain step entirely.
    assert deps.receipt_chain is None
    # The pre-existing fields still resolve to their defaults so the
    # spike's call shape (descriptor, deps, run_method, budget=…) keeps
    # working without further changes.
    assert deps.kind_deps is None
    assert deps.output_kind == OutputKind.FINDING
    assert deps.budget is None


def test_analyst_deps_accepts_trace_only_output_kind():
    """``_AnalystDeps(output_kind=TRACE_ONLY)`` must validate.

    REGRESSION: the Findings-split made the META analyst kinds
    (relationship_reifier / competing_hypotheses) carry the ``TRACE_ONLY``
    sentinel as their bind-time ``output_kind`` (so they keep an analyst_traces
    receipt but write NO analyst_outputs FINDING). The deps-builder feeds that
    sentinel straight into ``_AnalystDeps(output_kind=...)`` at actor
    activation — when the field was typed strict ``OutputKind`` it raised a
    pydantic enum ValidationError and those actors FAILED TO ACTIVATE (live
    ``reconcile.failed ... input_value=TRACE_ONLY``). The field now accepts the
    sentinel; the per-run effective kind is resolved at write time.
    """
    descriptor = _analyst_descriptor(f"analyst_meta_{uuid4().hex[:8]}")
    standard = StandardDeps(
        pg_pool=None,  # type: ignore[arg-type]
        nats_publish=None,
        secrets_resolve=None,
    )

    async def _run_method(inputs, options):  # pragma: no cover — never invoked
        return None

    deps = _AnalystDeps(
        descriptor=descriptor,
        deps=standard,
        run_method=_run_method,
        output_kind=TRACE_ONLY,
    )
    assert deps.output_kind is TRACE_ONLY


def test_analyst_deps_accepts_explicit_receipt_chain(pg_pool):
    """``_AnalystDeps`` accepts a populated ``RuntimeReceiptChain`` — the
    production-resolver path always sets this field, the spike does not."""
    descriptor = _analyst_descriptor(f"analyst_explicit_{uuid4().hex[:8]}")
    standard = StandardDeps(
        pg_pool=None,  # type: ignore[arg-type]
        nats_publish=None,
        secrets_resolve=None,
    )

    async def _run_method(inputs, options):  # pragma: no cover — never invoked
        return None

    chain = build_receipt_chain_for_analyst(
        descriptor.identity.id,
        descriptor.identity.version,
        pg_pool=pg_pool,
    )
    deps = _AnalystDeps(
        descriptor=descriptor,
        deps=standard,
        run_method=_run_method,
        receipt_chain=chain,
    )
    assert deps.receipt_chain is chain
