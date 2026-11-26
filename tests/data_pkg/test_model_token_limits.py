# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""No model call is held back by a token limit of ours.

* The OpenAI-compatible wire carries the maximum output the stack component
  declares for the alias a call names (the thinking alias has its own, larger
  one), less only when the counted input leaves less room in the declared
  window; an alias with no declared maximum gets no ``max_tokens`` at all.
* No caller-side number (a descriptor's ``method.llm.max_tokens``, a kind's
  ``DEFAULT_MAX_TOKENS``, the DSPy worker's default) reaches a wire or shrinks
  an input ceiling.
* The input budgets follow the declared context window, leaving room for the
  route's full output; ``LEGBA_LLM_INPUT_TOKEN_BUDGET`` overrides when set.
* A call cut at its output limit (finish reason ``length``) is recorded on the
  run's receipt and marked on the published output.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from legba.data.registry.credentials import MissingSecretError  # noqa: F401 — cycle warm-up
from legba.data.analysts import _llm_budget as budget
from legba.data.analysts import consult_context_ceiling as cc
from legba.data.analysts import gather_context_budget as gcb
from legba.data.analysts import meta_findings_synthesizer as M
from legba.data.registry import flow_params as fp
from legba.data.run_accounting import bind_run_accounting, current_llm_calls, reset_run_accounting
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import VLLMProviderHandler
from legba.data.stack.llm.thinking import bind_thinking
from legba.runtime.actor_payload import OUTPUT_TRUNCATED_KEY, mark_truncated_output

WINDOW = 262_144
FAST_MAX = 32_768
DEEP_MAX = 131_072


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEGBA_LLM_SEND_MAX_TOKENS", raising=False)
    monkeypatch.delenv(budget.LLM_INPUT_TOKEN_BUDGET_ENV, raising=False)


def _cfg(**limits: Any) -> LLMProviderConfig:
    fields: dict[str, Any] = {
        "api_endpoint": Property.Text.of("https://gw.example.test/v1"),
        "api_key": Property.Secret.of("k"),
        "model_name": Property.Text.of("fast"),
        "thinking_model_name": Property.Text.of("deep"),
    }
    for name, value in limits.items():
        fields[name] = Property.Number.of(value) if isinstance(value, int) else Property.Text.of(value)
    return LLMProviderConfig(**fields)


def _declared() -> LLMProviderConfig:
    return _cfg(max_output_tokens=FAST_MAX, thinking_max_output_tokens=DEEP_MAX,
                context_window_tokens=WINDOW)


class _Wire:
    def __init__(self, finish: str = "stop") -> None:
        self.bodies: list[dict[str, Any]] = []
        self.finish = finish

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={
            "model": self.bodies[-1].get("model"),
            "choices": [{"index": 0, "message": {"content": "ok"}, "finish_reason": self.finish}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        })


def _handler(cfg: LLMProviderConfig, wire: _Wire | None = None) -> VLLMProviderHandler:
    h = VLLMProviderHandler()
    h._cfg = cfg  # noqa: SLF001
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = "llm.test.openai_compat"  # noqa: SLF001
    if wire is not None:
        transport = httpx.AsyncBaseTransport()
        transport.handle_async_request = wire.handle_async_request  # type: ignore[method-assign]
        h._client = httpx.AsyncClient(  # noqa: SLF001
            base_url="https://gw.example.test", transport=transport, timeout=10.0,
        )
    return h


def _payload(h: VLLMProviderHandler, model: str, *, content: str = "hi", **kw: Any) -> dict[str, Any]:
    return h._build_chat_payload(  # noqa: SLF001
        messages=[{"role": "user", "content": content}], system=None, tools=None,
        model=model, max_tokens=512, temperature=None, reasoning_effort=None, **kw,
    )


# ---------------------------------------------------------------------------
# The wire value per alias
# ---------------------------------------------------------------------------


def test_each_alias_sends_its_own_declared_maximum() -> None:
    h = _handler(_declared())
    assert _payload(h, "fast")["max_tokens"] == FAST_MAX
    assert _payload(h, "deep")["max_tokens"] == DEEP_MAX
    assert h.max_output_tokens_by_alias() == {"fast": FAST_MAX, "deep": DEEP_MAX}
    # A model the component does not describe gets nothing rather than a guess.
    assert "max_tokens" not in _payload(h, "some-other-model")


@pytest.mark.asyncio
async def test_a_thinking_call_carries_the_thinking_maximum_end_to_end() -> None:
    wire = _Wire()
    h = _handler(_declared(), wire)
    await h.chat_complete([{"role": "user", "content": "q"}], max_tokens=7, thinking=True)
    await bind_thinking(h, True).chat_complete([{"role": "user", "content": "q"}], max_tokens=7)
    await h.chat_complete([{"role": "user", "content": "q"}], max_tokens=7)
    assert [(b["model"], b["max_tokens"]) for b in wire.bodies] == [
        ("deep", DEEP_MAX), ("deep", DEEP_MAX), ("fast", FAST_MAX),
    ]


def test_nothing_is_sent_when_nothing_is_declared() -> None:
    h = _handler(_cfg())
    assert "max_tokens" not in _payload(h, "fast")
    assert "max_tokens" not in _payload(h, "deep")
    # Only the plain alias declared: the thinking alias still gets nothing —
    # never the plain alias's smaller number.
    plain_only = _handler(_cfg(max_output_tokens=FAST_MAX))
    assert _payload(plain_only, "fast")["max_tokens"] == FAST_MAX
    assert "max_tokens" not in _payload(plain_only, "deep")


def test_the_request_shrinks_only_when_the_counted_input_leaves_less_room() -> None:
    h = _handler(_declared())
    small = _payload(h, "deep")
    assert small["max_tokens"] == DEEP_MAX and h._wire_receipt_fields(small) is None  # noqa: SLF001
    # ~200,000 estimated input tokens: 62,144-ish left in the window.
    big = _payload(h, "deep", content="x" * 800_000)
    counted = h._counted_input_tokens(big)  # noqa: SLF001
    assert big["max_tokens"] == WINDOW - counted < DEEP_MAX
    assert h._wire_receipt_fields(big) == {  # noqa: SLF001
        "max_tokens_sent": WINDOW - counted, "max_tokens_model_max": DEEP_MAX,
        "max_tokens_limited_by": "context_window",
    }
    # The same input on the plain alias still fits its smaller maximum whole.
    assert _payload(h, "fast", content="x" * 800_000)["max_tokens"] == FAST_MAX
    # With no window declared (and none listed) the maximum goes unchanged.
    no_window = _handler(_cfg(max_output_tokens=FAST_MAX, thinking_max_output_tokens=DEEP_MAX))
    assert _payload(no_window, "deep", content="x" * 800_000)["max_tokens"] == DEEP_MAX


def test_declared_limits_may_name_an_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    from legba.data.schemas.env_refs import EnvRefError

    h = _handler(_cfg(max_output_tokens="${env:LEGBA_X_MAX_OUT}",
                      context_window_tokens="${env:LEGBA_X_WINDOW}"))
    monkeypatch.setenv("LEGBA_X_MAX_OUT", "32768")
    monkeypatch.setenv("LEGBA_X_WINDOW", "262144")
    assert _payload(h, "fast")["max_tokens"] == 32_768
    assert h.context_window_tokens() == 262_144
    # Unset: a loud failure naming the variable, never a silent "undeclared".
    monkeypatch.delenv("LEGBA_X_MAX_OUT")
    with pytest.raises(EnvRefError, match="LEGBA_X_MAX_OUT"):
        _payload(h, "fast")


def test_a_malformed_limit_is_refused_at_parse_time() -> None:
    with pytest.raises(ValueError, match="max_output_tokens"):
        _cfg(max_output_tokens="lots")
    with pytest.raises(ValueError, match="thinking_model_name"):
        LLMProviderConfig(
            api_endpoint=Property.Text.of("https://gw.example.test/v1"),
            api_key=Property.Secret.of("k"), model_name=Property.Text.of("fast"),
            thinking_max_output_tokens=Property.Number.of(DEEP_MAX),
        )


# ---------------------------------------------------------------------------
# Former caps, now inert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_callers_max_tokens_never_reaches_the_wire() -> None:
    wire = _Wire()
    await _handler(_declared(), wire).chat_complete([{"role": "user", "content": "q"}], max_tokens=768)
    await _handler(_cfg(), wire).chat_complete([{"role": "user", "content": "q"}], max_tokens=768)
    assert wire.bodies[0]["max_tokens"] == FAST_MAX
    assert "max_tokens" not in wire.bodies[1]


def test_the_consult_ceiling_no_longer_subtracts_the_descriptors_max_tokens() -> None:
    import inspect

    assert "max_tokens" not in inspect.signature(cc.derive_ceiling).parameters
    assert cc.derive_ceiling(_handler(_declared())) == (
        WINDOW - DEEP_MAX - budget.DERIVED_MARGIN_TOKENS, cc.SOURCE_MODEL_WINDOW,
    )


def test_the_dspy_adapter_passes_no_output_limit() -> None:
    import inspect

    from legba.runtime.dapr_workflow import dspy_lm

    assert "max_tokens" not in inspect.signature(dspy_lm.make_provider_lm).parameters
    assert "max_tokens=" not in inspect.getsource(dspy_lm.make_provider_lm)


# ---------------------------------------------------------------------------
# Input ceilings
# ---------------------------------------------------------------------------


def test_the_input_budget_follows_the_declared_window(monkeypatch: pytest.MonkeyPatch) -> None:
    both = _handler(_declared())
    expected = WINDOW - DEEP_MAX - budget.DERIVED_MARGIN_TOKENS  # 126,976
    assert budget.derived_input_budget(both) == (expected, budget.SOURCE_MODEL_WINDOW)
    # A route whose largest maximum is the plain alias's leaves input more room.
    plain = _handler(_cfg(max_output_tokens=FAST_MAX, context_window_tokens=WINDOW))
    assert budget.input_token_budget(plain) == WINDOW - FAST_MAX - budget.DERIVED_MARGIN_TOKENS
    # A window and no declared maximum: half the window is kept for output.
    bare = _handler(_cfg(context_window_tokens=WINDOW))
    assert budget.input_token_budget(bare) == WINDOW // 2 - budget.DERIVED_MARGIN_TOKENS
    # No window anywhere: the default.
    assert budget.derived_input_budget(_handler(_cfg())) == (
        budget.DEFAULT_INPUT_TOKEN_BUDGET, budget.SOURCE_INPUT_BUDGET_DEFAULT,
    )
    assert budget.input_token_budget() == budget.DEFAULT_INPUT_TOKEN_BUDGET
    # The explicit override wins over the window.
    monkeypatch.setenv(budget.LLM_INPUT_TOKEN_BUDGET_ENV, "65536")
    assert budget.derived_input_budget(both) == (65_536, budget.SOURCE_INPUT_BUDGET_ENV)


def test_every_derived_ceiling_reads_the_window() -> None:
    h = _handler(_declared())
    window_budget = budget.input_token_budget(h)
    # The unit path's GATHER evidence cap.
    ctx = gcb.resolve_gather_context_budget(
        {}, gather_system="s" * 1000, max_rounds=3, engaged=True, llm=h,
    )
    assert ctx is not None and ctx.input_token_budget == window_budget
    assert ctx.evidence_char_cap == gcb.derive_evidence_char_cap(1000, window_budget)
    without = gcb.resolve_gather_context_budget(
        {}, gather_system="s" * 1000, max_rounds=3, engaged=True,
    )
    assert without is not None and without.input_token_budget == budget.DEFAULT_INPUT_TOKEN_BUDGET
    # The composition path's per-input excerpt (a run with many inputs shows it).
    n = 64
    assert M.composition_body_cap(n, window_budget) > M.composition_body_cap(n)
    # The consult ceiling.
    assert cc.derive_ceiling(h)[0] == window_budget


def test_the_flow_view_mirrors_the_derived_budget() -> None:
    h = _handler(_declared())
    value, _basis = fp.derived_input_budget(WINDOW, [FAST_MAX, DEEP_MAX])
    assert value == budget.input_token_budget(h)
    assert fp.derived_input_budget(WINDOW, [])[0] == budget.input_token_budget(
        _handler(_cfg(context_window_tokens=WINDOW)))


# ---------------------------------------------------------------------------
# What an operator sees
# ---------------------------------------------------------------------------


def test_the_flow_view_says_what_each_alias_is_sent() -> None:
    declared = fp.openai_compat_limits({
        "model": "fast", "thinking_model": "deep", "max_output_tokens": FAST_MAX,
        "thinking_max_output_tokens": DEEP_MAX, "context_window_tokens": WINDOW,
    })
    assert declared["max_tokens_sent"] == FAST_MAX
    assert declared["max_tokens_sent_thinking"] == DEEP_MAX
    assert "fast: SENT 32,768" in declared["max_tokens_basis"]
    assert "deep: SENT 131,072" in declared["max_tokens_basis"]
    assert declared["input_budget_tokens"] == WINDOW - DEEP_MAX - fp.INPUT_BUDGET_MARGIN
    bare = fp.openai_compat_limits({"model": "gemma"})
    assert bare["max_tokens_sent"] is None
    assert "relies on the endpoint's default" in bare["max_tokens_basis"]
    unresolved = fp.openai_compat_limits({
        "model": "gemma", "limits_unresolved": {"max_output_tokens": "${env:LEGBA_X}"},
    })
    assert "${env:LEGBA_X}" in unresolved["max_tokens_basis"]


# ---------------------------------------------------------------------------
# Truncation is never silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_length_stop_is_on_the_receipt_and_on_the_published_output() -> None:
    from legba.data.provenance.models import FindingPayload

    token = bind_run_accounting()
    try:
        await _handler(_declared(), _Wire(finish="length")).chat_complete(
            [{"role": "user", "content": "q"}], thinking=True,
        )
        calls = current_llm_calls()
    finally:
        reset_run_accounting(token)
    assert calls[-1]["finish_reason"] == "length" and calls[-1]["model"] == "deep"
    payload = FindingPayload(title="t", body="cut mid-")
    step = mark_truncated_output(payload, calls)
    assert payload.data[OUTPUT_TRUNCATED_KEY]["last_call_cut"] is True
    assert step is not None and step["kind"] == OUTPUT_TRUNCATED_KEY and step["calls_cut"] == 1
    # A run whose calls all finished leaves the output untouched.
    whole = FindingPayload(title="t", body="whole")
    assert mark_truncated_output(whole, [{"status": "success", "finish_reason": "stop"}]) is None
    assert OUTPUT_TRUNCATED_KEY not in whole.data
