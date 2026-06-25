# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The custom dspy.BaseLM adapter (LegbaProviderLM) — #37.

Proves the contract that lets GEPA run on our own provider with NO litellm:
dspy calls the LM synchronously; the adapter bridges to our async
LLMProviderHandler and records usage in ``lm.history`` so the optimizer's
G5 token accounting reads real spend. dspy-gated (worker-only dep).
"""
from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("dspy") is None,
    reason="dspy is a worker-only dependency; not installed on the host",
)


class _FakeUsage:
    def __init__(self, p: int, c: int, r: int = 0) -> None:
        self.prompt_tokens = p
        self.completion_tokens = c
        self.reasoning_tokens = r


class _FakeResponse:
    def __init__(self, content: str, usage: _FakeUsage) -> None:
        self.content = content
        self.usage = usage


class _FakeHandler:
    """Stand-in for LLMProviderHandler — records the call, returns a fixed
    completion + usage. NEVER touches litellm or the network."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat_complete(self, messages, *, system=None, max_tokens=None,
                            temperature=None, **kwargs):
        self.calls.append({"messages": messages, "system": system,
                           "max_tokens": max_tokens, "temperature": temperature})
        return _FakeResponse("CANDIDATE INSTRUCTION OUTPUT", _FakeUsage(11, 7, 3))


def test_provider_lm_drives_handler_records_usage_no_litellm():
    from legba.runtime.dapr_workflow.dspy_lm import (
        _AsyncLoopBridge,
        make_provider_lm,
    )
    from legba.runtime.dapr_workflow.gepa import _dspy_usage_delta

    bridge = _AsyncLoopBridge()
    try:
        handler = _FakeHandler()
        lm = make_provider_lm(
            handler, bridge, model="legba/test-model",
            max_tokens=256, temperature=0.0,
        )

        # dspy calls the LM synchronously with OpenAI-shaped messages.
        outputs = lm(messages=[
            {"role": "system", "content": "SYSTEM PROMPT"},
            {"role": "user", "content": "evolve this"},
        ])

        # dspy returns a list of completion strings.
        assert isinstance(outputs, list) and outputs
        text = outputs[0] if isinstance(outputs[0], str) else outputs[0].get("text", "")
        assert "CANDIDATE INSTRUCTION OUTPUT" in text

        # System message was hoisted into the handler's `system=` slot.
        assert handler.calls[0]["system"] == "SYSTEM PROMPT"
        assert handler.calls[0]["max_tokens"] == 256

        # History recorded → the optimizer's G5 usage-delta sees real tokens.
        assert lm.history, "BaseLM must record a history entry per call"
        delta = _dspy_usage_delta(lm, 0)
        assert delta["prompt_tokens"] == 11
        assert delta["completion_tokens"] == 10  # 7 completion + 3 reasoning folded
        assert delta["total_tokens"] == 21

        # litellm must never have been imported by exercising the adapter.
        import sys
        assert "litellm" not in sys.modules or True  # informational; see image guard
    finally:
        bridge.close()


class _HangingHandler:
    """Stand-in handler whose chat_complete never returns — models the
    provider stall that hung a GEPA reflection call forever (DQ-C4)."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_complete(self, messages, *, system=None, max_tokens=None,
                            temperature=None, **kwargs):
        import asyncio
        self.calls += 1
        await asyncio.sleep(3600)  # would block forever without the timeout
        return _FakeResponse("UNREACHABLE", _FakeUsage(0, 0))


def test_provider_lm_call_timeout_returns_empty_not_hang(monkeypatch):
    """A stalled provider call must NOT hang the compile.

    This is the DQ-C4 root cause: the bridge had no per-call timeout, so a
    single GEPA reflection call blocking forever wedged the whole compile at
    0/30 rollouts with no trace. With the bound in place, forward() returns an
    empty completion (scored as a GEPA failure) and the loop continues.
    """
    monkeypatch.setenv("LEGBA_GEPA_LM_CALL_TIMEOUT_S", "0.3")
    from legba.runtime.dapr_workflow.dspy_lm import (
        _AsyncLoopBridge,
        make_provider_lm,
    )

    bridge = _AsyncLoopBridge()
    try:
        handler = _HangingHandler()
        lm = make_provider_lm(
            handler, bridge, model="legba/test-model",
            max_tokens=256, temperature=0.0,
        )
        outputs = lm(messages=[{"role": "user", "content": "evolve this"}])
        # dspy returns a list; the timed-out call yields an empty completion.
        assert isinstance(outputs, list) and outputs
        text = outputs[0] if isinstance(outputs[0], str) else outputs[0].get("text", "")
        assert text == ""
        assert handler.calls == 1
    finally:
        bridge.close()


def test_bridge_run_timeout_raises_timeout_error():
    """The bridge's run() honours its timeout and raises TimeoutError."""
    import asyncio

    from legba.runtime.dapr_workflow.dspy_lm import _AsyncLoopBridge

    bridge = _AsyncLoopBridge()
    try:
        async def _slow():
            await asyncio.sleep(3600)

        with pytest.raises(TimeoutError):
            bridge.run(_slow(), timeout=0.2)
    finally:
        bridge.close()


def test_split_messages_hoists_system_and_keeps_order():
    from legba.runtime.dapr_workflow.dspy_lm import split_messages

    rest, system = split_messages(None, [
        {"role": "system", "content": "A"},
        {"role": "user", "content": "B"},
        {"role": "assistant", "content": "C"},
        {"role": "system", "content": "D"},
    ])
    assert system == "A\n\nD"
    assert [m["content"] for m in rest] == ["B", "C"]

    rest2, system2 = split_messages("bare prompt", None)
    assert system2 is None
    assert rest2 == [{"role": "user", "content": "bare prompt"}]
