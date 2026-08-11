# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-4 — the faithfulness judge is receipted, budgeted and costed.

The defect (P3 §4a, the sharpest finding of that pass): the keystone verify gate
runs on an EXTERNAL, paid, rate-limited API, and appeared in no ``llm_calls``
receipt, no ``budget_ledger`` row and no cost estimate. Three days of
``analyst_traces.llm_calls`` contained zero rows matching the judge; every unit
run showed exactly ONE llm_call — the generation — and
``leadership_transition``'s ledger read 29,626 tokens/run, the generation
prompt+completion alone. Judge duration, tokens, cost, ``prompt_sha256`` and
``finish_reason`` did not exist anywhere, so any decision about judge VOLUME was
unmeasurable.

The cause was never a missing chokepoint. ``LLMProviderHandler.chat_complete``
accounted the judge call exactly like every other call — the run flushed the
account into the trace row BEFORE the verify pass (it must: V-B's absence-slice
check reads this run's ``input_row_refs`` back over the same connection), so
every judge record landed after the snapshot and was dropped at reset.

These tests traverse the REAL binding path, per the house rule that a test which
hand-builds the wiring proves nothing: a REAL provider handler over
``httpx.MockTransport`` standing in for the judge, through the REAL
``chat_complete`` → the REAL run-accounting watermark → the REAL
``RuntimeReceiptChain.append_llm_calls`` → the REAL
``record_judge_calls_budget``, against a REAL ``budget_ledger``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from legba.data import run_accounting
from legba.data.provenance.budget import (
    JUDGE_LEDGER_VERSION_PREFIX,
    is_judge_ledger_version,
    judge_ledger_version,
    record_judge_calls_budget,
)
from legba.data.provenance.receipts import RuntimeReceiptChain
from legba.data.registry.credentials import MissingSecretError
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import OpenAIProviderHandler
from legba.runtime.budget import BudgetEnforcer

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Provider harness — a REAL handler with its transport swapped for a mock.
# ---------------------------------------------------------------------------


class _FakeResolver:
    def __init__(self, secrets: dict[str, bytes]):
        self._secrets = secrets

    async def verify_exists(self, secret_id: str) -> bool:
        return secret_id in self._secrets

    async def resolve(self, secret_id: str) -> bytes:
        if secret_id not in self._secrets:
            raise MissingSecretError(secret_id)
        return self._secrets[secret_id]


class _TelStub:
    def log(self, level, msg, /, **fields):
        pass

    def event(self, name, payload=None):
        pass

    def span(self, name, /, **attrs):
        class _S:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
        return _S()


@dataclass
class _FakeCtx:
    instance_id: str
    instance_version: str
    config: LLMProviderConfig
    secrets: Any
    budget: Any = None

    def telemetry(self):
        return _TelStub()


#: The live judge route (P3 §4: rung 1, LEGBA_JUDGE_STACK_REF) and the live
#: generation component, so the test asserts the exact population split the
#: readout needs.
_JUDGE_COMPONENT = "llm.judge.cerebras_gemma4_31b.openai_compat"
_JUDGE_MODEL = "gemma-4-31b"
_GEN_COMPONENT = "llm.primary.openai_compat"
_GEN_MODEL = "gpt-oss-120b"

_MESSAGES: list[Mapping[str, Any]] = [{"role": "user", "content": "grade these claims"}]


def _completion_body(*, prompt_tokens: int, completion_tokens: int) -> dict:
    return {
        "id": "chatcmpl-s4",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"verdicts":["supported"]}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def _handler(component_id: str, model: str, responder) -> OpenAIProviderHandler:
    cfg = LLMProviderConfig(
        api_endpoint=Property.Text.of("https://llm.example.internal"),
        api_key=Property.Secret.of("test.api_key"),
        model_name=Property.Text.of(model),
        max_tokens=Property.Number.of(16384, minimum=1, maximum=200000),
    )
    handler = OpenAIProviderHandler()
    await handler.on_configure(
        _FakeCtx(
            instance_id=component_id,
            instance_version="0" * 16,
            config=cfg,
            secrets=_FakeResolver({"test.api_key": b"sk-test"}),
        )
    )
    if handler._client is not None:  # noqa: SLF001
        await handler._client.aclose()  # noqa: SLF001
    handler._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://llm.example.internal",
        headers=handler._auth_headers(),  # noqa: SLF001
        transport=httpx.MockTransport(responder),
        timeout=httpx.Timeout(10.0),
    )
    return handler


# ---------------------------------------------------------------------------
# 1) The watermark: the tail recorded AFTER the flush is the judge leg
# ---------------------------------------------------------------------------


async def test_the_judge_call_lands_after_the_flush_and_the_watermark_finds_it():
    """The whole mechanism in one test, over real handlers.

    Pre-fix this is the exact sequence that lost the judge: generation is
    recorded, the run flushes ``current_llm_calls()`` into the trace, the judge
    then answers into the SAME still-bound account, and nothing ever read it.
    """
    gen = await _handler(
        _GEN_COMPONENT, _GEN_MODEL,
        lambda _r: httpx.Response(
            200, json=_completion_body(prompt_tokens=28_000, completion_tokens=1_600),
        ),
    )
    judge = await _handler(
        _JUDGE_COMPONENT, _JUDGE_MODEL,
        lambda _r: httpx.Response(
            200, json=_completion_body(prompt_tokens=4_200, completion_tokens=310),
        ),
    )
    token = run_accounting.bind_run_accounting()
    try:
        # ---- the generation leg, then the receipt flush -------------------
        await gen.chat_complete(_MESSAGES, system="you are a desk")
        flushed = run_accounting.current_llm_calls()
        watermark = run_accounting.llm_call_watermark()

        assert len(flushed) == 1, "the receipt sees the generation, as it always did"
        assert flushed[0]["component_id"] == _GEN_COMPONENT
        assert watermark == 1

        # ---- the verify pass runs AFTER the flush ------------------------
        await judge.chat_complete(_MESSAGES, system="you are a faithfulness judge")

        tail = run_accounting.llm_calls_since(watermark)
    finally:
        run_accounting.reset_run_accounting(token)
        await gen.on_retire(None)  # type: ignore[arg-type]
        await judge.on_retire(None)  # type: ignore[arg-type]

    assert len(tail) == 1, "the judge call must be recoverable after the flush"
    entry = tail[0]
    # Everything P3 said did not exist anywhere for the judge leg.
    assert entry["component_id"] == _JUDGE_COMPONENT
    assert entry["model"] == _JUDGE_MODEL
    assert entry["status"] == "success"
    assert entry["prompt_tokens"] == 4_200
    assert entry["completion_tokens"] == 310
    assert entry["total_tokens"] == 4_510
    assert entry["finish_reason"] == "stop"
    assert entry["prompt_sha256"]
    assert entry["duration_ms"] >= 0


async def test_llm_calls_since_returns_copies_not_the_live_entries():
    """The caller stamps ``leg`` onto the tail; that must not mutate the
    account (a later whole-account flush would then carry a tag it never
    agreed to)."""
    token = run_accounting.bind_run_accounting()
    try:
        run_accounting.record_llm_call(component_id="a", total_tokens=1)
        tail = run_accounting.llm_calls_since(0)
        tail[0]["leg"] = "verify_judge"
        assert "leg" not in run_accounting.current_llm_calls()[0]
    finally:
        run_accounting.reset_run_accounting(token)


async def test_watermark_helpers_are_noops_when_no_account_is_bound():
    """The registry process, ad-hoc scripts and most tests bind nothing."""
    assert run_accounting.llm_call_watermark() == 0
    assert run_accounting.llm_calls_since(0) == []
    assert run_accounting.llm_calls_since(-5) == []


# ---------------------------------------------------------------------------
# 2) The receipt append — and the receipt CHAIN is untouched
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    import asyncpg

    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1,
        max_size=4,
    )
    yield pool
    await pool.close()


async def _write_trace(chain: RuntimeReceiptChain, run_id, analyst_id: str, calls):
    now = datetime.now(timezone.utc)
    return await chain.record(
        run_id=run_id,
        analyst_id=analyst_id,
        analyst_version="v1",
        cadence_trigger="cadence",
        target_id=None,
        input_row_refs=[],
        input_payload=None,
        prompt_module_hash=None,
        prompt_rendered=None,
        output_row_refs=[],
        output_payload={"title": "t"},
        run_started_at=now,
        run_ended_at=now,
        status="success",
        llm_calls=calls or None,
        tool_calls=None,
    )


async def test_append_lands_the_judge_call_beside_the_generation(pg_pool):
    """One run's receipt ends up carrying BOTH legs, distinguishable by tag."""
    chain = RuntimeReceiptChain(pg_pool)
    run_id = uuid4()
    analyst_id = f"s4_append_{uuid4().hex[:8]}"

    gen_call = {
        "component_id": _GEN_COMPONENT, "model": _GEN_MODEL,
        "status": "success", "prompt_tokens": 28_000,
        "completion_tokens": 1_600, "total_tokens": 29_600,
    }
    receipt_hash, _prev = await _write_trace(chain, run_id, analyst_id, [gen_call])

    judge_call = {
        "component_id": _JUDGE_COMPONENT, "subprovider": "openai",
        "model": _JUDGE_MODEL, "status": "success", "prompt_tokens": 4_200,
        "completion_tokens": 310, "total_tokens": 4_510, "leg": "verify_judge",
    }
    appended = await chain.append_llm_calls(run_id=run_id, calls=[judge_call])
    assert appended == 1

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT llm_calls, receipt_hash FROM analyst_traces WHERE run_id = $1",
            run_id,
        )
    persisted = json.loads(row["llm_calls"])
    assert len(persisted) == 2, persisted
    assert persisted[0]["component_id"] == _GEN_COMPONENT
    assert "leg" not in persisted[0], "the generation leg stays untagged"
    assert persisted[1]["component_id"] == _JUDGE_COMPONENT
    assert persisted[1]["leg"] == "verify_judge", (
        "the judge population must be filterable in the receipt"
    )
    # THE point of appending rather than re-recording: llm_calls is not in
    # compute_receipt_hash's payload, so the provenance chain is unchanged and
    # the row stays verifiable.
    assert row["receipt_hash"] == receipt_hash


async def test_append_is_a_quiet_zero_when_there_is_nothing_to_append(pg_pool):
    chain = RuntimeReceiptChain(pg_pool)
    assert await chain.append_llm_calls(run_id=uuid4(), calls=[]) == 0
    # A TRACE_ONLY run writes no row; appending must not raise or invent one.
    assert await chain.append_llm_calls(
        run_id=uuid4(), calls=[{"component_id": _JUDGE_COMPONENT}],
    ) == 0


# ---------------------------------------------------------------------------
# 3) The budget dimension — visible to the governor, invisible to the gate
# ---------------------------------------------------------------------------


async def test_judge_calls_land_their_own_ledger_dimension(pg_pool):
    analyst_id = f"s4_ledger_{uuid4().hex[:8]}"
    calls = [
        {"component_id": _JUDGE_COMPONENT, "subprovider": "openai",
         "model": _JUDGE_MODEL, "status": "success",
         "prompt_tokens": 4_200, "completion_tokens": 310},
        # A second partition of the SAME finding — one row, not two.
        {"component_id": _JUDGE_COMPONENT, "subprovider": "openai",
         "model": _JUDGE_MODEL, "status": "success",
         "prompt_tokens": 1_800, "completion_tokens": 140},
    ]
    async with pg_pool.acquire() as conn:
        rows = await record_judge_calls_budget(
            conn, analyst_id=analyst_id, calls=calls,
        )
        assert len(rows) == 1, "one upsert per distinct judge target, not per call"
        assert rows[0].analyst_version == judge_ledger_version(_JUDGE_COMPONENT)
        assert rows[0].tokens_used == 4_200 + 310 + 1_800 + 140
        assert rows[0].runs == 2, "runs counts judge CALLS — the tokens/call denominator"

        ledger = await conn.fetch(
            "SELECT analyst_version, tokens_used, runs, cost_estimate_usd "
            "FROM budget_ledger WHERE analyst_id = $1",
            analyst_id,
        )
    assert len(ledger) == 1
    assert is_judge_ledger_version(ledger[0]["analyst_version"])
    assert ledger[0]["analyst_version"].startswith(JUDGE_LEDGER_VERSION_PREFIX)
    # Cost resolves through the same price table every other row uses. An
    # unpriced model contributes 0 while tokens still accumulate — the honest,
    # pre-existing behaviour, not a judge-specific fudge.
    assert ledger[0]["cost_estimate_usd"] is not None


async def test_the_judge_row_never_throttles_the_desk_but_the_governor_sees_it(pg_pool):
    """The two properties that make this safe to turn on.

    Per-analyst enforcement reads ``WHERE analyst_id = $1 AND analyst_version =
    $2`` against the DESCRIPTOR version, so metering the judge must not start
    demoting desks. The global envelope sums the whole bucket, so it MUST see
    the judge — that is the accounting P3 found missing.
    """
    analyst_id = f"s4_gate_{uuid4().hex[:8]}"
    enforcer = BudgetEnforcer(
        analyst_id=analyst_id,
        analyst_version="v1",
        budget_tokens_per_day=10_000,
        provider="openai",
        model=_GEN_MODEL,
    )
    async with pg_pool.acquire() as conn:
        # The generation leg, metered as always.
        await enforcer.record(conn, prompt_tokens=6_000, completion_tokens=500)
        before = await enforcer.precall_check(conn, estimated_tokens=1)
        assert before.tokens_used_today == 6_500

        # A judge call big enough to blow the desk's cap if it were pooled in.
        await record_judge_calls_budget(
            conn, analyst_id=analyst_id,
            calls=[{
                "component_id": _JUDGE_COMPONENT, "subprovider": "openai",
                "model": _JUDGE_MODEL, "status": "success",
                "prompt_tokens": 50_000, "completion_tokens": 1_000,
            }],
        )
        after = await enforcer.precall_check(conn, estimated_tokens=1)
        assert after.outcome == "ok"
        assert after.tokens_used_today == 6_500, (
            "the judge dimension must stay out of the per-analyst gate"
        )

        bucket_total = await conn.fetchval(
            "SELECT SUM(tokens_used) FROM budget_ledger "
            "WHERE analyst_id = $1 AND bucket = CURRENT_DATE",
            analyst_id,
        )
    assert int(bucket_total) == 6_500 + 51_000, (
        "the global governor sums the bucket — it must see the judge leg"
    )


async def test_failed_and_unkeyable_judge_calls_are_skipped_for_the_ledger(pg_pool):
    """A failed call has no token counts. It is already evidenced per-call in
    the receipt; a zero-token ledger row would only corrupt the run count."""
    analyst_id = f"s4_skip_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        rows = await record_judge_calls_budget(
            conn, analyst_id=analyst_id,
            calls=[
                {"component_id": _JUDGE_COMPONENT, "subprovider": "openai",
                 "model": _JUDGE_MODEL, "status": "error", "error": "TimeoutError"},
                # No component_id — nothing to key a dimension on.
                {"subprovider": "openai", "model": _JUDGE_MODEL,
                 "prompt_tokens": 10, "completion_tokens": 2},
            ],
        )
        assert rows == []
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM budget_ledger WHERE analyst_id = $1", analyst_id,
        ) == 0

        assert await record_judge_calls_budget(
            conn, analyst_id=analyst_id, calls=[],
        ) == []


async def test_two_distinct_judges_in_one_run_get_one_row_each(pg_pool):
    """The route ladder can repoint mid-flight and the absence slice uses its
    own partition — the ledger must keep the populations apart."""
    analyst_id = f"s4_two_{uuid4().hex[:8]}"
    other = "llm.judge.openrouter.openai_compat"
    async with pg_pool.acquire() as conn:
        rows = await record_judge_calls_budget(
            conn, analyst_id=analyst_id,
            calls=[
                {"component_id": _JUDGE_COMPONENT, "subprovider": "openai",
                 "model": _JUDGE_MODEL, "prompt_tokens": 100, "completion_tokens": 10},
                {"component_id": other, "subprovider": "openai",
                 "model": "some-other", "prompt_tokens": 200, "completion_tokens": 20},
            ],
        )
    assert {r.analyst_version for r in rows} == {
        judge_ledger_version(_JUDGE_COMPONENT), judge_ledger_version(other),
    }


async def test_the_generation_version_can_never_look_like_a_judge_row():
    """Descriptor versions are content hashes; the prefix cannot collide."""
    assert not is_judge_ledger_version("v33d7a0b0e1f2a3b4")
    assert not is_judge_ledger_version("")
    assert is_judge_ledger_version(judge_ledger_version(_JUDGE_COMPONENT))
