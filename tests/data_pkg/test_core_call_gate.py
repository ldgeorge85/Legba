# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T-1 — one process-wide concurrency bound on the core-plane model.

``call_gate.CallGate`` is held around the network hop by
``LLMProviderHandler.chat_complete`` for the core component only. Pinned:
the bound holds across handler instances AND event loops (a deep-consult
activity runs on its own ``asyncio.run`` loop); scheduled calls never take
the interactive reserve; a freed slot goes to a waiting interactive call
first; FIFO within a class; a cancelled waiter leaves the queue and loses
nothing; nothing is dropped; a wait is on the receipt; other components keep
their per-instance ``max_concurrent`` exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import httpx
import pytest

from legba.data.registry.credentials import MissingSecretError  # noqa: F401 — cycle warm-up

from legba.data import run_accounting
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import VLLMProviderHandler
from legba.data.stack.llm import call_gate as cg

CORE_ID = "llm.primary.openai_compat"


@pytest.fixture(autouse=True)
def _fresh_gates(monkeypatch: pytest.MonkeyPatch):
    for env in (cg.COMPONENT_ENV, cg.LIMIT_ENV, cg.RESERVED_ENV):
        monkeypatch.delenv(env, raising=False)
    cg.clear_gates()
    yield
    cg.clear_gates()


async def _hold(gate: cg.CallGate, order: list[str], name: str, release: asyncio.Event,
                priority: str) -> None:
    async with gate.slot(priority):
        order.append(name)
        await release.wait()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_calls_never_take_the_interactive_reserve() -> None:
    gate = cg.CallGate("g", limit=3, reserved=1)
    release = asyncio.Event()
    order: list[str] = []
    sched = [asyncio.create_task(_hold(gate, order, f"s{i}", release, cg.SCHEDULED))
             for i in range(4)]
    await _settle()
    assert order == ["s0", "s1"]  # 3 - 1 reserved
    snap = gate.snapshot()
    assert snap["in_flight"] == {"interactive": 0, "scheduled": 2}
    assert snap["queued"] == {"interactive": 0, "scheduled": 2}
    # The reserve is there for a person, immediately.
    inter = asyncio.create_task(_hold(gate, order, "i0", release, cg.INTERACTIVE))
    await _settle()
    assert order == ["s0", "s1", "i0"]
    release.set()
    await asyncio.gather(*sched, inter)
    assert sorted(order) == ["i0", "s0", "s1", "s2", "s3"]  # nothing dropped
    assert gate.snapshot()["in_flight"] == {"interactive": 0, "scheduled": 0}


@pytest.mark.asyncio
async def test_a_freed_slot_goes_to_a_waiting_interactive_call_first_then_fifo() -> None:
    gate = cg.CallGate("g", limit=2, reserved=0)
    gates_open = {n: asyncio.Event() for n in ("a", "b", "s1", "s2", "i1", "i2")}
    order: list[str] = []

    async def run(name: str, prio: str) -> None:
        async with gate.slot(prio):
            order.append(name)
            await gates_open[name].wait()

    tasks = [asyncio.create_task(run("a", cg.SCHEDULED)),
             asyncio.create_task(run("b", cg.SCHEDULED))]
    await _settle()
    tasks += [asyncio.create_task(run("s1", cg.SCHEDULED)),
              asyncio.create_task(run("s2", cg.SCHEDULED))]
    await _settle()
    tasks += [asyncio.create_task(run("i1", cg.INTERACTIVE)),
              asyncio.create_task(run("i2", cg.INTERACTIVE))]
    await _settle()
    assert order == ["a", "b"]
    gates_open["a"].set()
    await _settle()
    assert order[-1] == "i1"  # arrived after s1/s2, served first
    gates_open["b"].set()
    await _settle()
    assert order[-1] == "i2"
    gates_open["i1"].set()
    await _settle()
    assert order[-1] == "s1"  # FIFO within scheduled
    for ev in gates_open.values():
        ev.set()
    await asyncio.gather(*tasks)
    assert order == ["a", "b", "i1", "i2", "s1", "s2"]


@pytest.mark.asyncio
async def test_a_cancelled_waiter_leaves_the_queue_and_the_slot_is_not_lost() -> None:
    gate = cg.CallGate("g", limit=1, reserved=0)
    release = asyncio.Event()
    order: list[str] = []
    first = asyncio.create_task(_hold(gate, order, "first", release, cg.SCHEDULED))
    await _settle()
    doomed = asyncio.create_task(_hold(gate, order, "doomed", release, cg.SCHEDULED))
    later = asyncio.create_task(_hold(gate, order, "later", release, cg.SCHEDULED))
    await _settle()
    doomed.cancel()
    await _settle()
    assert gate.snapshot()["queued"]["scheduled"] == 1
    release.set()
    await asyncio.gather(first, later)
    with pytest.raises(asyncio.CancelledError):
        await doomed
    assert order == ["first", "later"]
    assert gate.snapshot()["in_flight"] == {"interactive": 0, "scheduled": 0}


def test_the_bound_holds_across_event_loops_in_one_process() -> None:
    """A deep-consult activity runs on its own ``asyncio.run`` loop in a worker
    thread; the gate is one bound for all of them."""
    gate = cg.CallGate("g", limit=1, reserved=0)
    peak = {"now": 0, "max": 0}
    lock = threading.Lock()

    async def one_call() -> None:
        async with gate.slot(cg.SCHEDULED):
            with lock:
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.02)
            with lock:
                peak["now"] -= 1

    async def three_calls() -> None:
        await asyncio.wait_for(asyncio.gather(*(one_call() for _ in range(3))), 10)

    def loop_worker() -> None:
        asyncio.run(three_calls())

    threads = [threading.Thread(target=loop_worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert peak["max"] == 1
    snap = gate.snapshot()
    assert snap["granted"] == 9 and snap["in_flight"]["scheduled"] == 0


@pytest.mark.asyncio
async def test_a_wait_is_measured() -> None:
    gate = cg.CallGate("g", limit=1, reserved=0)
    release = asyncio.Event()
    holder = asyncio.create_task(_hold(gate, [], "h", release, cg.SCHEDULED))
    await _settle()

    async def waiter() -> cg.SlotTicket:
        async with gate.slot(cg.SCHEDULED) as ticket:
            return ticket

    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    release.set()
    ticket = await w
    await holder
    assert ticket.wait_ms >= 40
    snap = gate.snapshot()
    assert snap["waited"] == 1 and snap["wait_max_ms"] >= 40 and snap["peak_queued"] == 1


def test_the_priority_follows_the_context() -> None:
    assert cg.current_priority() == cg.SCHEDULED
    with cg.interactive_calls():
        assert cg.current_priority() == cg.INTERACTIVE
    assert cg.current_priority() == cg.SCHEDULED


# ---------------------------------------------------------------------------
# Configuration and the process registry
# ---------------------------------------------------------------------------


def test_the_core_component_is_gated_with_the_defaults_and_nothing_else_is() -> None:
    gate = cg.gate_for_component(CORE_ID)
    assert gate is not None and gate is cg.gate_for_component(CORE_ID)
    assert (gate.limit, gate.reserved) == (cg.DEFAULT_LIMIT, cg.DEFAULT_RESERVED) == (12, 2)
    assert cg.gate_for_component("llm.verify.gateway_judge.openai_compat") is None
    assert cg.gate_for_component("") is None and cg.gate_for_component(None) is None
    assert [s["gate"] for s in cg.gate_snapshots()] == [CORE_ID]


def test_the_environment_sets_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cg.LIMIT_ENV, "6")
    monkeypatch.setenv(cg.RESERVED_ENV, "9")  # clamped to limit - 1
    gate = cg.gate_for_component(CORE_ID)
    assert (gate.limit, gate.reserved) == (6, 5)
    cg.clear_gates()
    monkeypatch.setenv(cg.COMPONENT_ENV, "llm.core.elsewhere")
    assert cg.gate_for_component(CORE_ID) is None
    assert cg.gate_for_component("llm.core.elsewhere") is not None


@pytest.mark.parametrize("bad", ["0", "-3", "many"])
def test_a_bad_limit_falls_back_to_the_default_never_to_unbounded(
    monkeypatch: pytest.MonkeyPatch, bad: str,
) -> None:
    monkeypatch.setenv(cg.LIMIT_ENV, bad)
    assert cg.gate_for_component(CORE_ID).limit == cg.DEFAULT_LIMIT


# ---------------------------------------------------------------------------
# Through the real handler
# ---------------------------------------------------------------------------


def _handler(instance_id: str, *, max_concurrent: int | None = None) -> VLLMProviderHandler:
    extra: dict[str, Any] = {}
    if max_concurrent is not None:
        extra["max_concurrent"] = Property.Number.of(max_concurrent, minimum=1)
    h = VLLMProviderHandler()
    h._cfg = LLMProviderConfig(  # noqa: SLF001
        api_endpoint=Property.Text.of("https://gw.example.test/v1"),
        api_key=Property.Secret.of("k"),
        model_name=Property.Text.of("fast"),
        max_tokens=Property.Number.of(16384, minimum=1, maximum=200000),
        **extra,
    )
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = instance_id  # noqa: SLF001
    return h


class _SlowWire:
    """An async transport that counts concurrent requests and holds each."""

    def __init__(self, hold: asyncio.Event) -> None:
        self.hold = hold
        self.now = 0
        self.peak = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.now += 1
        self.peak = max(self.peak, self.now)
        await self.hold.wait()
        self.now -= 1
        return httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


def _install(h: VLLMProviderHandler, wire: _SlowWire) -> None:
    transport = httpx.AsyncBaseTransport()
    transport.handle_async_request = wire.handle_async_request  # type: ignore[method-assign]
    h._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://gw.example.test", transport=transport, timeout=10.0,
    )


@pytest.mark.asyncio
async def test_two_handler_instances_of_the_core_component_share_one_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cg.LIMIT_ENV, "3")
    monkeypatch.setenv(cg.RESERVED_ENV, "0")
    hold = asyncio.Event()
    wire = _SlowWire(hold)
    # The component row's per-instance max_concurrent (16 live) steps aside.
    handlers = [_handler(CORE_ID, max_concurrent=16) for _ in range(2)]
    for h in handlers:
        _install(h, wire)
    calls = [asyncio.create_task(h.chat_complete([{"role": "user", "content": "q"}], stream=False))
             for h in handlers for _ in range(4)]
    await asyncio.sleep(0.05)
    assert wire.peak == 3
    hold.set()
    results = await asyncio.gather(*calls)
    assert len(results) == 8 and all(r.content == "ok" for r in results)


@pytest.mark.asyncio
async def test_a_waited_call_carries_gate_wait_ms_on_its_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cg.LIMIT_ENV, "1")
    hold = asyncio.Event()
    wire = _SlowWire(hold)
    h = _handler(CORE_ID)
    _install(h, wire)

    async def call() -> list[dict[str, Any]]:
        token = run_accounting.bind_run_accounting()
        try:
            await h.chat_complete([{"role": "user", "content": "q"}], stream=False)
            return run_accounting.current_llm_calls()
        finally:
            run_accounting.reset_run_accounting(token)

    first = asyncio.create_task(call())
    await asyncio.sleep(0.01)
    second = asyncio.create_task(call())
    await asyncio.sleep(0.05)
    hold.set()
    (r1,), (r2,) = await asyncio.gather(first, second)
    assert "gate_wait_ms" not in r1
    assert r2["gate_wait_ms"] >= 30


@pytest.mark.asyncio
async def test_another_component_keeps_its_own_per_instance_limit() -> None:
    hold = asyncio.Event()
    wire = _SlowWire(hold)
    h = _handler("llm.verify.gateway_judge.openai_compat", max_concurrent=2)
    _install(h, wire)
    calls = [asyncio.create_task(h.chat_complete([{"role": "user", "content": "q"}], stream=False))
             for _ in range(4)]
    await asyncio.sleep(0.05)
    assert wire.peak == 2
    hold.set()
    await asyncio.gather(*calls)
    assert cg.gate_snapshots() == []


def test_the_runtime_healthz_reports_the_gates() -> None:
    assert json.dumps(cg.gate_snapshots()) == "[]"


@pytest.mark.asyncio
async def test_the_runtime_healthz_answers_its_own_body_not_daprs() -> None:
    """``DaprActor(app)`` registers its own ``GET /healthz``
    (``{"status": "ok"}``) and the first matching route answers, so the
    runtime's route mounted after it was dead (no
    ``llm_gates``). Mounted first, through the same helper ``main()`` and the
    spike builder call, the runtime's body is what a probe reads."""
    from dapr.ext.fastapi import DaprActor
    from fastapi import FastAPI

    from legba.runtime.dapr_host import build_dapr_host_app, mount_runtime_healthz

    app = FastAPI()
    mount_runtime_healthz(app, {})
    DaprActor(app)
    spike = build_dapr_host_app()
    for a in (app, spike):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=a), base_url="http://rt"
        ) as c:
            r = await c.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["llm_gates"] == []
        assert "actor_types" in body and "leader_election" in body


def test_main_mounts_the_runtime_healthz_before_dapractor() -> None:
    """``main()`` builds its app inline, so the order is read from its AST:
    the helper call precedes ``DaprActor(app)``."""
    import ast
    import inspect

    from legba.runtime import dapr_host

    tree = ast.parse(inspect.getsource(dapr_host.main))
    calls = [n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in ("mount_runtime_healthz", "DaprActor")]
    lines = {name: min(n.lineno for n in ast.walk(tree)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == name) for name in set(calls)}
    assert set(lines) == {"mount_runtime_healthz", "DaprActor"}
    assert lines["mount_runtime_healthz"] < lines["DaprActor"]


@pytest.mark.asyncio
async def test_a_consult_turns_call_receipts_come_from_the_real_call_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consult turn's ``usage.call_receipts`` is the run account's
    per-call entries (recorded by the provider handler itself), normalised by
    ``consult_turn_stamps.call_receipts``: the alias that answered, tokens in
    and out, reasoning tokens (``None`` when the gateway reports none), the
    wait at the core gate and the duration — through the real handler, the
    real gate and the real account, and through ``usage_receipt``."""
    from legba.data.registry import consult_turn_stamps as st

    monkeypatch.setenv(cg.LIMIT_ENV, "1")
    hold = asyncio.Event()
    wire = _SlowWire(hold)
    h = _handler(CORE_ID)
    _install(h, wire)
    token = run_accounting.bind_run_accounting()
    try:
        mark = run_accounting.llm_call_watermark()
        first = asyncio.create_task(h.chat_complete([{"role": "user", "content": "q"}], stream=False))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(h.chat_complete([{"role": "user", "content": "q"}], stream=False))
        await asyncio.sleep(0.05)
        hold.set()
        await asyncio.gather(first, second)
        raw = run_accounting.llm_calls_since(mark)
    finally:
        run_accounting.reset_run_accounting(token)
    rows = st.call_receipts(raw)
    assert rows is not None and len(rows) == 2
    for r in rows:
        assert r["model"] == "fast" and r["component_id"] == CORE_ID and r["status"] == "success"
        assert (r["prompt_tokens"], r["completion_tokens"]) == (1, 1)
        assert r["reasoning_tokens"] in (None, 0)
        assert isinstance(r["duration_ms"], int) and r["duration_ms"] >= 0
        assert "completion_text" not in r and "prompt_sha256" not in r
    assert rows[0]["gate_wait_ms"] == 0 and rows[1]["gate_wait_ms"] >= 30
    receipt = st.usage_receipt({"calls": 2, "call_receipts": raw}, plane="core", route=CORE_ID)
    assert receipt["call_receipts"] == rows
    # a run that carried none says so, never "zero calls"
    assert st.usage_receipt({"calls": 2}, plane="core", route=CORE_ID)["call_receipts"] is None
    assert st.call_receipts([{"truncated": 3}, "x"]) == []


@pytest.mark.asyncio
async def test_a_consult_turn_names_the_model_its_calls_ran_on() -> None:
    """Every call of a live core chat turn ran the thinking alias
    ``deep`` while the turn said ``fast`` — the handler's configured name,
    which ``SpendGuard.receipt`` reads off the config. Through the real handler
    and the real account: the turn now says what the calls ran, and when they
    differ it says so (``mixed``) rather than picking one."""
    from legba.data.analysts.consult_spend_guard import SpendGuard
    from legba.data.registry import consult_turn_stamps as st

    hold = asyncio.Event()
    hold.set()
    h = _handler(CORE_ID)
    h._cfg = h._cfg.model_copy(update={"thinking_model_name": Property.Text.of("deep")})  # noqa: SLF001
    _install(h, _SlowWire(hold))
    token = run_accounting.bind_run_accounting()
    try:
        mark = run_accounting.llm_call_watermark()
        for _ in range(3):
            await h.chat_complete([{"role": "user", "content": "q"}], stream=False, thinking=True)
        thinking_only = run_accounting.llm_calls_since(mark)
        await h.chat_complete([{"role": "user", "content": "q"}], stream=False, thinking=False)
        mixed = run_accounting.llm_calls_since(mark)
    finally:
        run_accounting.reset_run_accounting(token)
    configured = SpendGuard().receipt(h, elapsed_s=1.0)
    assert configured["model"] == "fast"  # what the turn used to say
    u = st.usage_receipt({**configured, "call_receipts": thinking_only}, plane="core", route=CORE_ID)
    assert (u["model"], u["models"], u["configured_model"]) == ("deep", {"deep": 3}, "fast")
    u = st.usage_receipt({**configured, "call_receipts": mixed}, plane="core", route=CORE_ID)
    assert u["model"] == "mixed: deep ×3, fast ×1" and u["models"] == {"deep": 3, "fast": 1}
    # no receipts: the configured name, and no per-model count
    u = st.usage_receipt(configured, plane="core", route=CORE_ID)
    assert (u["model"], u["models"]) == ("fast", None)
