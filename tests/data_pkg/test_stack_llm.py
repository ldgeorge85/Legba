# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the L-120 LLM provider stack handlers.

Two flavors:

  * Unit — mock httpx via `MockTransport`. Verify request shape, tool-arg
    translation, usage parsing, error mapping, lifecycle idempotency,
    healthcheck behavior, cost calculation. Fast; no network.
  * Integration — guarded by env tokens:
      - LEGBA_VLLM_TOKEN     ⇒ live vLLM hit against llm.example.internal
      - LEGBA_ANTHROPIC_TOKEN   ⇒ live Anthropic 1-token completion
      - LEGBA_OPENAI_TOKEN      ⇒ live OpenAI 1-token completion

Skipped with clear marker when the env isn't set so unattended CI runs
clean and humans see exactly which tokens unlock which paths.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping
from unittest.mock import AsyncMock

import httpx
import pytest

from legba.data.registry.credentials import (
    CredentialResolverProtocol,
    MissingSecretError,
)
from legba.data.registry.health import HealthState
from legba.data.schemas import (
    LLMProvider,
    LLMProviderConfig,
    Property,
)
from legba.data.stack.llm import (
    AnthropicProviderHandler,
    BudgetReporter,
    HardLLMFailure,
    LLMChunk,
    LLMProviderHandler,
    LLMResponse,
    LLMToolCall,
    LLMUsage,
    LLM_HANDLERS,
    ModelPrice,
    OpenAIProviderHandler,
    TransientLLMFailure,
    VLLMProviderHandler,
    estimate_cost,
    resolve_handler,
)
from legba.data.stack.llm.base import _split_endpoint


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResolver:
    """Stand-in for `CredentialResolverProtocol`. Returns the configured
    plaintext bytes; raises `MissingSecretError` if asked for an unknown id."""

    def __init__(self, secrets: dict[str, bytes]):
        self._secrets = secrets

    async def verify_exists(self, secret_id: str) -> bool:
        return secret_id in self._secrets

    async def resolve(self, secret_id: str) -> bytes:
        if secret_id not in self._secrets:
            raise MissingSecretError(secret_id)
        return self._secrets[secret_id]


class _FakeBudgetReporter:
    def __init__(self, envelope: str = "ok"):
        self._envelope = envelope
        self.records: list[tuple[str, int, str | None]] = []
        self.envelope_calls = 0

    async def record(
        self,
        *,
        kind: str,
        amount: int,
        dimension: str | None = None,
    ) -> None:
        self.records.append((kind, amount, dimension))

    async def check_envelope(self) -> str:
        self.envelope_calls += 1
        return self._envelope


@dataclass
class _FakeCtx:
    """Lightweight test stand-in for L-103's `RuntimeContext`."""

    instance_id: str
    instance_version: str
    config: LLMProviderConfig
    secrets: CredentialResolverProtocol
    budget: BudgetReporter | None = None

    def telemetry(self):
        return _TelStub()


class _TelStub:
    def __init__(self):
        self.events: list[tuple[str, Mapping[str, Any] | None]] = []

    def log(self, level, msg, /, **fields):
        pass

    def event(self, name, payload=None):
        self.events.append((name, payload))

    def span(self, name, /, **attrs):
        class _S:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
        return _S()


def _make_cfg(
    *,
    endpoint: str = "https://api.example.com",
    model: str = "test-model",
    secret_id: str = "test.api_key",
    max_tokens: int = 1024,
) -> LLMProviderConfig:
    return LLMProviderConfig(
        api_endpoint=Property.Text.of(endpoint),
        api_key=Property.Secret.of(secret_id),
        model_name=Property.Text.of(model),
        max_tokens=Property.Number.of(max_tokens, minimum=1, maximum=200000),
    )


def _make_ctx(
    *,
    handler_kind: str = "anthropic",
    endpoint: str | None = None,
    model: str = "test-model",
    secret_id: str = "test.api_key",
    secret_value: bytes = b"sk-test-token",
    budget: BudgetReporter | None = None,
) -> _FakeCtx:
    if endpoint is None:
        endpoint = {
            "anthropic": "https://api.anthropic.com",
            "openai": "https://api.openai.com",
            "vllm": "https://llm.example.internal/v1",
        }[handler_kind]
    cfg = _make_cfg(endpoint=endpoint, model=model, secret_id=secret_id)
    return _FakeCtx(
        instance_id=f"llm.{handler_kind}.test",
        instance_version="0" * 16,
        config=cfg,
        secrets=_FakeResolver({secret_id: secret_value}),
        budget=budget,
    )


async def _install_mock_transport(handler: LLMProviderHandler, handler_fn):
    """Replace the handler's httpx.AsyncClient with one backed by a
    MockTransport that dispatches via `handler_fn(request) -> Response`."""
    transport = httpx.MockTransport(handler_fn)
    if handler._client is not None:  # noqa: SLF001
        await handler._client.aclose()  # noqa: SLF001
    cfg = handler._require_configured()  # noqa: SLF001
    handler._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=cfg.api_endpoint.raw.rstrip("/"),
        headers=handler._auth_headers(),  # noqa: SLF001
        transport=transport,
        timeout=httpx.Timeout(10.0),
    )


# ---------------------------------------------------------------------------
# Resolution + classvars
# ---------------------------------------------------------------------------


def test_subprovider_registry_complete():
    """All three concrete providers are registered in `LLM_HANDLERS`."""
    assert set(LLM_HANDLERS) == {"anthropic", "vllm", "openai"}
    assert resolve_handler("anthropic") is AnthropicProviderHandler
    assert resolve_handler("vllm") is VLLMProviderHandler
    assert resolve_handler("openai") is OpenAIProviderHandler
    with pytest.raises(KeyError):
        resolve_handler("unknown-subprovider")


def test_kindhandler_classvars():
    """All concrete subproviders carry the L-102 §1 KindHandler classvars."""
    for cls in (AnthropicProviderHandler, VLLMProviderHandler, OpenAIProviderHandler):
        assert cls.kind == "llm_provider"
        assert cls.family == "stack"
        assert cls.schema_version.startswith("legba/stack.llm_provider/")
        assert cls.config_schema is LLMProviderConfig
        assert cls.handler_version


def test_split_endpoint_variants():
    assert _split_endpoint("https://api.anthropic.com", 443) == ("api.anthropic.com", 443, "https")
    assert _split_endpoint("https://api.openai.com:8443/v1", 443) == ("api.openai.com", 8443, "https")
    assert _split_endpoint("http://localhost:8000", 443) == ("localhost", 8000, "http")
    assert _split_endpoint("api.openai.com", 443) == ("api.openai.com", 443, "https")


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


def test_estimate_cost_anthropic_opus_basic():
    usage = LLMUsage(
        prompt_tokens=1_000_000, completion_tokens=500_000,
        cache_read_tokens=100_000, cache_write_tokens=50_000,
        model="claude-opus-4-7",
    )
    cost = estimate_cost("claude-opus-4-7", usage, AnthropicProviderHandler.PRICE_TABLE)
    # 1M @ $15 + 0.5M @ $75 + 0.1M @ $1.50 + 0.05M @ $18.75
    # = 15 + 37.5 + 0.15 + 0.9375
    expected = 15.0 + 37.5 + 0.15 + 0.9375
    assert cost == pytest.approx(expected, rel=1e-4)


def test_estimate_cost_prefix_match():
    """Versioned models (e.g. claude-opus-4-7-20260301) roll up to the
    family entry via prefix match."""
    usage = LLMUsage(prompt_tokens=1_000_000, completion_tokens=0, model="x")
    cost = estimate_cost(
        "claude-opus-4-7-20260301", usage, AnthropicProviderHandler.PRICE_TABLE,
    )
    assert cost == pytest.approx(15.0)


def test_estimate_cost_unknown_model_zero():
    usage = LLMUsage(prompt_tokens=1_000, completion_tokens=1_000)
    assert estimate_cost("self-hosted-mystery-model", usage, {}) == 0.0


def test_estimate_cost_vllm_is_zero_by_default():
    """vLLM is self-hosted ⇒ no list price; cost rolls up to $0."""
    usage = LLMUsage(prompt_tokens=10_000_000, completion_tokens=10_000_000)
    assert estimate_cost("gpt-oss-120b", usage, VLLMProviderHandler.PRICE_TABLE) == 0.0


def test_estimate_cost_openai_reasoning_model():
    usage = LLMUsage(
        prompt_tokens=10_000, completion_tokens=10_000,
        reasoning_tokens=50_000, model="o3",
    )
    cost = estimate_cost("o3", usage, OpenAIProviderHandler.PRICE_TABLE)
    # 0.01M @ $30 + 0.01M @ $120 + 0.05M @ $120
    expected = 0.3 + 1.2 + 6.0
    assert cost == pytest.approx(expected, rel=1e-4)


# ---------------------------------------------------------------------------
# Tool translation
# ---------------------------------------------------------------------------


OPENAI_TOOL_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "search_signals",
            "description": "Search signals by query string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    }
]


def test_anthropic_tool_translation():
    h = AnthropicProviderHandler()
    out = h._translate_tools(OPENAI_TOOL_SPEC)  # noqa: SLF001
    assert len(out) == 1
    assert out[0]["name"] == "search_signals"
    assert out[0]["description"].startswith("Search signals")
    assert out[0]["input_schema"]["properties"]["query"]["type"] == "string"
    assert "function" not in out[0]
    assert "type" not in out[0]


def test_anthropic_tool_translation_passthrough_native_shape():
    """If the caller already passes Anthropic-native shape, accept it."""
    native = [{"name": "x", "description": "y", "input_schema": {"type": "object"}}]
    out = AnthropicProviderHandler()._translate_tools(native)  # noqa: SLF001
    assert out[0]["name"] == "x"
    assert out[0]["input_schema"] == {"type": "object"}


def test_openai_tool_translation_passthrough():
    h = OpenAIProviderHandler()
    out = h._translate_tools(OPENAI_TOOL_SPEC)  # noqa: SLF001
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "search_signals"


def test_openai_tool_translation_bare_shape_wrapped():
    h = OpenAIProviderHandler()
    bare = [{"name": "search", "description": "d", "parameters": {"type": "object"}}]
    out = h._translate_tools(bare)  # noqa: SLF001
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "search"


def test_vllm_tool_translation_matches_openai():
    h = VLLMProviderHandler()
    out_openai = OpenAIProviderHandler()._translate_tools(OPENAI_TOOL_SPEC)  # noqa: SLF001
    out_vllm = h._translate_tools(OPENAI_TOOL_SPEC)  # noqa: SLF001
    assert out_vllm == out_openai


# ---------------------------------------------------------------------------
# Message translation
# ---------------------------------------------------------------------------


def test_anthropic_hoists_system_message_from_messages_list():
    """Anthropic requires system at top-level, not in the messages list."""
    h = AnthropicProviderHandler()
    msgs = [
        {"role": "system", "content": "You are a careful analyst."},
        {"role": "user", "content": "Hello"},
    ]
    wire, sys_text = h._translate_messages(msgs, system=None)  # noqa: SLF001
    assert sys_text == "You are a careful analyst."
    assert all(m.get("role") != "system" for m in wire)
    assert wire == [{"role": "user", "content": "Hello"}]


def test_anthropic_omits_temperature_for_opus_4_8():
    """Opus 4.8 deprecated `temperature` (sending it 400s). The payload builder
    must DROP it for the opus-4-8+ line while still sending it for older models
    (Sonnet/Haiku) that accept it."""
    h = AnthropicProviderHandler()
    msgs = [{"role": "user", "content": "hi"}]

    opus = h._build_chat_payload(  # noqa: SLF001
        messages=msgs, system=None, tools=None,
        model="claude-opus-4-8", max_tokens=512, temperature=0.2,
        reasoning_effort=None,
    )
    assert "temperature" not in opus
    # Prefix match (dated revision) is also covered.
    opus_dated = h._build_chat_payload(  # noqa: SLF001
        messages=msgs, system=None, tools=None,
        model="claude-opus-4-8-20260301", max_tokens=512, temperature=0.2,
        reasoning_effort=None,
    )
    assert "temperature" not in opus_dated

    sonnet = h._build_chat_payload(  # noqa: SLF001
        messages=msgs, system=None, tools=None,
        model="claude-sonnet-4-6", max_tokens=512, temperature=0.2,
        reasoning_effort=None,
    )
    assert sonnet["temperature"] == 0.2


def test_anthropic_logs_the_dropped_temperature_once_per_model(caplog):
    """A SILENTLY dropped parameter is a dead knob nobody can see — the consult
    kind has been setting temperature=0.2 against a deployed opus-4-8 and
    tuning nothing. Log it, but once per handler per model: a 10-round consult
    must not emit ten identical warnings."""
    h = AnthropicProviderHandler()
    msgs = [{"role": "user", "content": "hi"}]

    def _build(model: str) -> None:
        h._build_chat_payload(  # noqa: SLF001
            messages=msgs, system=None, tools=None, model=model,
            max_tokens=512, temperature=0.2, reasoning_effort=None,
        )

    with caplog.at_level(logging.WARNING, logger="legba.data.stack.llm.anthropic"):
        for _ in range(10):                       # a whole consult loop
            _build("claude-opus-4-8")
        _build("claude-opus-4-8-20260301")        # a different model id
        _build("claude-sonnet-4-6")               # honors it — nothing to say

    dead = [r for r in caplog.records if "DEAD_KNOB" in r.getMessage()]
    assert len(dead) == 2                         # one per model, not per call
    assert "claude-opus-4-8" in dead[0].getMessage()
    assert "0.2" in dead[0].getMessage()


def test_anthropic_says_nothing_when_no_temperature_was_requested():
    """No caller intent, no dead knob, no log line."""
    h = AnthropicProviderHandler()
    h._build_chat_payload(  # noqa: SLF001
        messages=[{"role": "user", "content": "hi"}], system=None, tools=None,
        model="claude-opus-4-8", max_tokens=512, temperature=None,
        reasoning_effort=None,
    )
    assert h._temperature_drop_logged == set()  # noqa: SLF001


def test_anthropic_translates_tool_role_to_user():
    """Anthropic has no "tool" role — the text-protocol tool loops (consult /
    deep_consult) append {"role": "tool", ...}; it must become a user message."""
    h = AnthropicProviderHandler()
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": '{"tool": "search_signals", "args": {}}'},
        {"role": "tool", "name": "search_signals", "content": '{"results": []}'},
    ]
    wire, _ = h._translate_messages(msgs, system=None)  # noqa: SLF001
    assert all(m["role"] in {"user", "assistant"} for m in wire)
    tool_msg = wire[-1]
    assert tool_msg["role"] == "user"
    assert "Tool result (search_signals):" in tool_msg["content"]
    assert '{"results": []}' in tool_msg["content"]


def test_anthropic_merges_system_kwarg_and_message():
    """If both `system` kwarg and a system message are present, concat."""
    h = AnthropicProviderHandler()
    msgs = [
        {"role": "system", "content": "Extra system."},
        {"role": "user", "content": "Hi"},
    ]
    wire, sys_text = h._translate_messages(msgs, system="Base system.")  # noqa: SLF001
    assert sys_text is not None
    assert "Base system." in sys_text
    assert "Extra system." in sys_text


# ---------------------------------------------------------------------------
# Prompt caching (Anthropic-only) — QW1-E
# ---------------------------------------------------------------------------


def _big(prefix: str, handler: AnthropicProviderHandler) -> str:
    """A string comfortably past the caching char floor."""
    return prefix + "x" * handler.CACHE_MIN_SYSTEM_CHARS


def test_anthropic_caches_large_system_prompt():
    """The consult plane's ~10KB system prompt is byte-identical on every round
    of a run — it must carry a cache breakpoint so rounds 2+ read it back."""
    h = AnthropicProviderHandler()
    system = _big("You are an analyst. ", h)
    payload = h._build_chat_payload(  # noqa: SLF001
        messages=[{"role": "user", "content": "hi"}], system=system, tools=None,
        model="claude-opus-4-8", max_tokens=512, temperature=None,
        reasoning_effort=None,
    )
    assert payload["system"] == [
        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
    ]


def test_anthropic_leaves_short_system_prompt_as_bare_string():
    """Below Anthropic's minimum cacheable prefix a breakpoint is silently
    ignored anyway — keep the historical bare-string shape."""
    h = AnthropicProviderHandler()
    payload = h._build_chat_payload(  # noqa: SLF001
        messages=[{"role": "user", "content": "hi"}], system="You are an assistant.",
        tools=None, model="claude-opus-4-8", max_tokens=512, temperature=None,
        reasoning_effort=None,
    )
    assert payload["system"] == "You are an assistant."


def test_anthropic_caches_transcript_on_last_message():
    """A tool loop only APPENDS to its transcript, so the moving breakpoint on
    the newest turn lets round N+1 read what round N wrote."""
    h = AnthropicProviderHandler()
    messages = [
        {"role": "user", "content": _big("question ", h)},
        {"role": "assistant", "content": '{"tool": "list_situations", "args": {}}'},
        {"role": "user", "content": "Tool result (list_situations):\n{}"},
    ]
    payload = h._build_chat_payload(  # noqa: SLF001
        messages=messages, system=None, tools=None,
        model="claude-opus-4-8", max_tokens=512, temperature=None,
        reasoning_effort=None,
    )
    wire = payload["messages"]
    # Only the LAST message is rewritten; earlier turns stay byte-identical so
    # the cached prefix keeps matching.
    assert wire[:-1] == messages[:-1]
    assert wire[-1]["content"] == [
        {
            "type": "text",
            "text": "Tool result (list_situations):\n{}",
            "cache_control": {"type": "ephemeral"},
        },
    ]


def test_anthropic_skips_transcript_breakpoint_for_one_shot_call():
    """A single-message call can never read its own write back — marking it
    would only buy the 1.25x write premium."""
    h = AnthropicProviderHandler()
    messages = [{"role": "user", "content": _big("one shot ", h)}]
    payload = h._build_chat_payload(  # noqa: SLF001
        messages=messages, system=None, tools=None,
        model="claude-opus-4-8", max_tokens=512, temperature=None,
        reasoning_effort=None,
    )
    assert payload["messages"] == messages


def test_anthropic_skips_transcript_breakpoint_for_short_transcript():
    h = AnthropicProviderHandler()
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    payload = h._build_chat_payload(  # noqa: SLF001
        messages=messages, system=None, tools=None,
        model="claude-opus-4-8", max_tokens=512, temperature=None,
        reasoning_effort=None,
    )
    assert payload["messages"] == messages


def test_anthropic_marks_tool_roster_only_without_a_system_breakpoint():
    """`tools` renders BEFORE `system`, so a system breakpoint already caches
    the roster; the roster is marked separately only when it has nothing to
    ride on."""
    h = AnthropicProviderHandler()
    tools = [{"name": "search_signals", "description": "d", "input_schema": {}}]
    msgs = [{"role": "user", "content": "hi"}]

    without_system = h._build_chat_payload(  # noqa: SLF001
        messages=msgs, system=None, tools=tools, model="claude-opus-4-8",
        max_tokens=512, temperature=None, reasoning_effort=None,
    )
    assert without_system["tools"][-1]["cache_control"] == {"type": "ephemeral"}

    with_system = h._build_chat_payload(  # noqa: SLF001
        messages=msgs, system=_big("sys ", h), tools=tools,
        model="claude-opus-4-8", max_tokens=512, temperature=None,
        reasoning_effort=None,
    )
    assert "cache_control" not in with_system["tools"][-1]


def test_anthropic_cache_control_master_switch_restores_old_shape(monkeypatch):
    h = AnthropicProviderHandler()
    monkeypatch.setattr(type(h), "CACHE_CONTROL_ENABLED", False)
    system = _big("sys ", h)
    messages = [
        {"role": "user", "content": _big("q ", h)},
        {"role": "assistant", "content": "a"},
    ]
    payload = h._build_chat_payload(  # noqa: SLF001
        messages=messages, system=system, tools=None, model="claude-opus-4-8",
        max_tokens=512, temperature=None, reasoning_effort=None,
    )
    assert payload["system"] == system
    assert payload["messages"] == messages


def test_openai_and_vllm_payloads_carry_no_cache_control():
    """Caching is Anthropic-shaped and must not leak to other providers."""
    for handler in (OpenAIProviderHandler(), VLLMProviderHandler()):
        payload = handler._build_chat_payload(  # noqa: SLF001
            messages=[
                {"role": "user", "content": "x" * 8000},
                {"role": "assistant", "content": "y"},
            ],
            system="s" * 8000, tools=None, model="gpt-test",
            max_tokens=512, temperature=0.2, reasoning_effort=None,
        )
        assert "cache_control" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_anthropic_sends_cache_control_on_the_wire():
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)
    captured: list[dict[str, Any]] = []
    await _install_mock_transport(h, _capture(captured))

    system = _big("You are an analyst. ", h)
    await h.chat_complete(
        messages=[
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": '{"tool": "x", "args": {}}'},
            {"role": "user", "content": "Tool result (x):\n" + "r" * 5000},
        ],
        system=system,
    )
    body = captured[0]["json"]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_anthropic_degrades_silently_when_cache_control_rejected():
    """A provider that rejects the marker must cost the run NOTHING but the
    caching: strip, retry uncached, and stop marking for this instance."""
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if "cache_control" in json.dumps(body):
            return httpx.Response(400, json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "cache_control: unexpected field",
                },
            })
        return httpx.Response(200, json=_ANTHROPIC_OK_BODY)

    await _install_mock_transport(h, handler)

    system = _big("You are an analyst. ", h)
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a" * 5000},
    ]
    response = await h.chat_complete(messages=messages, system=system)
    assert response.content == "Hello there."          # the call SUCCEEDED
    assert len(captured) == 2                          # marked, then retried
    # The retry is the historical uncached shape, byte for byte.
    assert captured[1]["system"] == system
    assert captured[1]["messages"] == messages
    assert h._cache_control_ok is False                # noqa: SLF001

    # ...and the NEXT call is assembled uncached from the start.
    await h.chat_complete(messages=messages, system=system)
    assert len(captured) == 3
    assert captured[2]["system"] == system


@pytest.mark.asyncio
async def test_anthropic_non_cache_4xx_still_raises():
    """Only a cache_control rejection triggers the fallback — an auth or
    validation 4xx must still fail loud."""
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})

    await _install_mock_transport(h, handler)
    with pytest.raises(HardLLMFailure):
        await h.chat_complete(
            messages=[
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a" * 5000},
            ],
            system=_big("sys ", h),
        )
    assert len(calls) == 1          # no pointless retry
    assert h._cache_control_ok is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_cache_tokens_land_in_the_run_receipt():
    """`cache_read_tokens` in an llm_calls receipt IS the live proof that a
    cached prefix was hit — record it (and only when non-zero)."""
    from legba.data.run_accounting import (
        bind_run_accounting,
        current_llm_calls,
        reset_run_accounting,
    )

    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-7")
    await h.on_configure(ctx)
    await _install_mock_transport(h, _capture([]))

    token = bind_run_accounting()
    try:
        await h.chat_complete(messages=[{"role": "user", "content": "hi"}])
        calls = current_llm_calls()
    finally:
        reset_run_accounting(token)

    assert len(calls) == 1
    assert calls[0]["cache_read_tokens"] == 20      # from _ANTHROPIC_OK_BODY
    assert calls[0]["cache_write_tokens"] == 5


@pytest.mark.asyncio
async def test_router_served_by_lands_in_the_run_receipt():
    """A ROUTED response names who actually served it — record that.

    `model` is what we asked for and `subprovider` is which handler class
    asked; neither identifies the upstream that answered. Measured 2026-08-16:
    the same model id, prompt and 94 critiques flipped 13.6% of pass/fail
    verdicts between two providers of the same weights, including a
    pass-stratum claim. Without this field that drift cannot be seen at all.
    """
    from legba.data.run_accounting import (
        bind_run_accounting,
        current_llm_calls,
        reset_run_accounting,
    )

    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai")
    await h.on_configure(ctx)
    routed = {**_OPENAI_OK_BODY, "provider": "DeepInfra"}
    await _install_mock_transport(
        h, lambda request: httpx.Response(200, json=routed),
    )

    token = bind_run_accounting()
    try:
        await h.chat_complete(messages=[{"role": "user", "content": "hi"}])
        calls = current_llm_calls()
    finally:
        reset_run_accounting(token)

    assert calls[0]["served_by"] == "DeepInfra"


@pytest.mark.asyncio
async def test_unrouted_receipt_omits_served_by():
    """A direct endpoint names no provider, so the receipt must stay
    byte-identical — the field's PRESENCE is the evidence that a router chose
    on our behalf."""
    from legba.data.run_accounting import (
        bind_run_accounting,
        current_llm_calls,
        reset_run_accounting,
    )

    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai")
    await h.on_configure(ctx)
    await _install_mock_transport(
        h, lambda request: httpx.Response(200, json=_OPENAI_OK_BODY),
    )

    token = bind_run_accounting()
    try:
        await h.chat_complete(messages=[{"role": "user", "content": "hi"}])
        calls = current_llm_calls()
    finally:
        reset_run_accounting(token)

    assert "served_by" not in calls[0]


@pytest.mark.asyncio
async def test_uncached_receipt_omits_the_cache_fields():
    from legba.data.run_accounting import (
        bind_run_accounting,
        current_llm_calls,
        reset_run_accounting,
    )

    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai")
    await h.on_configure(ctx)
    await _install_mock_transport(
        h, lambda request: httpx.Response(200, json=_OPENAI_OK_BODY),
    )

    token = bind_run_accounting()
    try:
        await h.chat_complete(messages=[{"role": "user", "content": "hi"}])
        calls = current_llm_calls()
    finally:
        reset_run_accounting(token)

    assert "cache_read_tokens" not in calls[0]
    assert "cache_write_tokens" not in calls[0]


def test_openai_prepends_system_kwarg_when_no_system_in_messages():
    h = OpenAIProviderHandler()
    msgs = [{"role": "user", "content": "Hi"}]
    wire, sys_text = h._translate_messages(msgs, system="You are X.")  # noqa: SLF001
    assert sys_text is None
    assert wire[0]["role"] == "system"
    assert wire[0]["content"] == "You are X."
    assert wire[1]["role"] == "user"


def test_openai_does_not_double_prepend_system_kwarg():
    h = OpenAIProviderHandler()
    msgs = [
        {"role": "system", "content": "Already here."},
        {"role": "user", "content": "Hi"},
    ]
    wire, sys_text = h._translate_messages(msgs, system="You are X.")  # noqa: SLF001
    assert sys_text is None
    # The existing system message stays — kwarg dropped to avoid clobber.
    assert wire == msgs


# ---------------------------------------------------------------------------
# Lifecycle — configure / activate / pause / retire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_configure_loads_secret_and_caches_config():
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic")
    await h.on_configure(ctx)
    assert h._cfg is not None  # noqa: SLF001
    assert h._api_key == "sk-test-token"  # noqa: SLF001
    assert h._instance_id == "llm.anthropic.test"  # noqa: SLF001
    # Model list is the static catalog for Anthropic.
    assert "claude-opus-4-7" in h.model_list


@pytest.mark.asyncio
async def test_on_configure_missing_secret_raises_hard_failure():
    h = AnthropicProviderHandler()
    cfg = _make_cfg(secret_id="vault.missing")
    ctx = _FakeCtx(
        instance_id="x",
        instance_version="0" * 16,
        config=cfg,
        secrets=_FakeResolver({}),  # vault empty
    )
    with pytest.raises(HardLLMFailure, match="vault missing api_key"):
        await h.on_configure(ctx)


@pytest.mark.asyncio
async def test_lifecycle_pause_then_resume_idempotent():
    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai")
    await h.on_configure(ctx)
    await h.on_activate(ctx)
    assert h._client is not None  # noqa: SLF001
    await h.on_pause(ctx)
    assert h._client is None  # noqa: SLF001
    await h.on_pause(ctx)  # second pause = no-op
    await h.on_resume(ctx)
    assert h._client is not None  # noqa: SLF001
    await h.on_retire(ctx)
    assert h._client is None  # noqa: SLF001
    assert h._cfg is None  # noqa: SLF001
    await h.on_retire(ctx)  # idempotent


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_before_configure_is_unhealthy():
    h = OpenAIProviderHandler()
    result = await h.health_check()
    assert result.state == HealthState.UNHEALTHY
    assert "not configured" in result.detail


@pytest.mark.asyncio
async def test_health_check_unparseable_endpoint(monkeypatch):
    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai", endpoint="")
    # Endpoint validation runs at config-build time normally; here we bypass
    # by constructing the handler with an empty raw value.
    h._cfg = _make_cfg(endpoint="")  # noqa: SLF001
    h._api_key = "k"  # noqa: SLF001
    h._instance_id = "x"  # noqa: SLF001
    result = await h.health_check()
    assert result.state == HealthState.UNHEALTHY
    assert "unparseable endpoint" in result.detail


@pytest.mark.asyncio
async def test_health_check_no_api_key():
    h = OpenAIProviderHandler()
    h._cfg = _make_cfg()  # noqa: SLF001
    h._instance_id = "x"  # noqa: SLF001
    result = await h.health_check()
    assert result.state == HealthState.UNHEALTHY
    assert "auth credential not resolved" in result.detail


@pytest.mark.asyncio
async def test_health_check_does_not_call_model(monkeypatch):
    """The Phase-1/L-120 healthcheck must NOT issue a chat_complete to the
    model — that would burn tokens every 60s. We assert by replacing
    _call_chat with a sentinel that raises if invoked."""
    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai", endpoint="https://localhost:1")
    await h.on_configure(ctx)
    # Patch _call_chat to fail loudly — health_check must not invoke it.
    h._call_chat = AsyncMock(side_effect=AssertionError("model burn!"))  # noqa: SLF001
    # tcp probe will be False against localhost:1 — that's fine, we only
    # care that no model call happens.
    result = await h.health_check()
    assert result.detail.startswith("https://localhost:1") or "reachable=" in result.detail
    h._call_chat.assert_not_called()  # noqa: SLF001


# ---------------------------------------------------------------------------
# chat_complete — request shape (Anthropic)
# ---------------------------------------------------------------------------


_ANTHROPIC_OK_BODY = {
    "id": "msg_01ABC",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-4-7",
    "content": [
        {"type": "text", "text": "Hello there."}
    ],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5,
    },
}


def _capture(captured: list[dict[str, Any]]):
    """Build a MockTransport handler that records each request and returns
    a default JSON body. Customize per-test by mutating the return inside
    `handler`."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({
            "url": str(request.url),
            "method": request.method,
            "headers": dict(request.headers),
            "json": json.loads(request.content) if request.content else None,
        })
        return httpx.Response(200, json=_ANTHROPIC_OK_BODY)

    return handler


@pytest.mark.asyncio
async def test_anthropic_chat_complete_request_shape():
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-7")
    await h.on_configure(ctx)

    captured: list[dict[str, Any]] = []
    await _install_mock_transport(h, _capture(captured))

    response = await h.chat_complete(
        messages=[{"role": "user", "content": "Hello"}],
        system="You are an assistant.",
        max_tokens=512,
    )
    assert response.content == "Hello there."
    assert response.finish_reason == "stop"
    assert response.usage.prompt_tokens == 100
    assert response.usage.completion_tokens == 50
    assert response.usage.cache_read_tokens == 20
    assert response.usage.cache_write_tokens == 5
    assert response.usage.model == "claude-opus-4-7"
    assert response.usage.cost_estimate_usd > 0.0  # priced model

    # Assert request shape.
    req = captured[0]
    assert req["url"].endswith("/v1/messages")
    assert req["method"] == "POST"
    assert req["headers"]["x-api-key"] == "sk-test-token"
    assert req["headers"]["anthropic-version"]
    body = req["json"]
    assert body["model"] == "claude-opus-4-7"
    assert body["system"] == "You are an assistant."
    assert body["max_tokens"] == 512
    assert body["messages"] == [{"role": "user", "content": "Hello"}]
    assert "stream" in body


@pytest.mark.asyncio
async def test_anthropic_chat_complete_with_tools_translates():
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic")
    await h.on_configure(ctx)

    body = dict(_ANTHROPIC_OK_BODY)
    body["content"] = [
        {"type": "text", "text": "Let me search."},
        {
            "type": "tool_use",
            "id": "toolu_01ABC",
            "name": "search_signals",
            "input": {"query": "BR energy", "limit": 5},
        },
    ]
    body["stop_reason"] = "tool_use"

    captured: list[dict[str, Any]] = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    await _install_mock_transport(h, handler)

    response = await h.chat_complete(
        messages=[{"role": "user", "content": "Search BR energy."}],
        tools=OPENAI_TOOL_SPEC,
    )
    # Anthropic shape was actually sent — verify.
    wire_tools = captured[0]["tools"]
    assert wire_tools[0]["name"] == "search_signals"
    assert "input_schema" in wire_tools[0]
    assert "function" not in wire_tools[0]
    # Response parsed.
    assert response.content == "Let me search."
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.name == "search_signals"
    assert tc.id == "toolu_01ABC"
    assert tc.arguments == {"query": "BR energy", "limit": 5}


# ---------------------------------------------------------------------------
# Anthropic streaming — SSE accumulation, mid-stream honesty, output clamp
# ---------------------------------------------------------------------------


def _sse_body(events: list[dict[str, Any]]) -> bytes:
    """Render events the way Anthropic's SSE wire serves them."""
    lines: list[str] = []
    for event in events:
        lines.append(f"event: {event.get('type', 'message')}")
        lines.append("data: " + json.dumps(event))
        lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _sse_response(events: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_sse_body(events),
    )


_ANTHROPIC_SSE_OK: list[dict[str, Any]] = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_01S", "type": "message", "role": "assistant",
            "model": "claude-opus-4-8", "content": [],
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
                "output_tokens": 2,
            },
        },
    },
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "text", "text": ""}},
    {"type": "ping"},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "text_delta", "text": "Hello "}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "text_delta", "text": "streamed."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta",
     "delta": {"stop_reason": "end_turn", "stop_sequence": None},
     "usage": {"output_tokens": 50}},
    {"type": "message_stop"},
]


@pytest.mark.asyncio
async def test_anthropic_streams_and_accumulates_text():
    """`stream: true` on the wire; deltas accumulate to the SAME LLMResponse
    contract callers already consume — content, usage, stop reason."""
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _sse_response(_ANTHROPIC_SSE_OK)

    await _install_mock_transport(h, handler)
    response = await h.chat_complete(
        messages=[{"role": "user", "content": "Hello"}], max_tokens=512,
    )
    assert captured[0]["stream"] is True
    assert response.content == "Hello streamed."
    assert response.finish_reason == "stop"
    assert response.usage.prompt_tokens == 100
    assert response.usage.completion_tokens == 50      # message_delta wins
    assert response.usage.cache_read_tokens == 20
    assert response.usage.cache_write_tokens == 5
    assert response.usage.cost_estimate_usd > 0.0


@pytest.mark.asyncio
async def test_anthropic_streams_tool_use_via_input_json_deltas():
    """tool_use args arrive as input_json_delta fragments — the accumulator
    reassembles + parses them into the normalized tool_calls shape."""
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)

    events: list[dict[str, Any]] = [
        _ANTHROPIC_SSE_OK[0],
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Let me search."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_01S",
                           "name": "search_signals", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"que'}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta",
                   "partial_json": 'ry": "BR energy", "limit": 5}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta",
         "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 30}},
        {"type": "message_stop"},
    ]
    await _install_mock_transport(h, lambda request: _sse_response(events))
    response = await h.chat_complete(
        messages=[{"role": "user", "content": "Search."}],
    )
    assert response.content == "Let me search."
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.id == "toolu_01S"
    assert tc.name == "search_signals"
    assert tc.arguments == {"query": "BR energy", "limit": 5}


@pytest.mark.asyncio
async def test_anthropic_mid_stream_failure_returns_partial_with_error_state(caplog):
    """A stream that dies after partial content is handled HONESTLY: the
    partial text comes back under an explicit finish_reason='error' (plus a
    raw-body marker), the failure is logged loudly, and the call is NOT
    retried into a duplicate generation."""
    import logging as _logging

    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)

    events: list[dict[str, Any]] = [
        _ANTHROPIC_SSE_OK[0],
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Partial ans"}},
        {"type": "error",
         "error": {"type": "overloaded_error", "message": "mid-stream cut"}},
    ]
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _sse_response(events)

    await _install_mock_transport(h, handler)
    with caplog.at_level(
        _logging.WARNING, logger="legba.data.stack.llm.anthropic",
    ):
        response = await h.chat_complete(
            messages=[{"role": "user", "content": "q"}],
        )
    assert len(calls) == 1                              # NOT retried
    assert response.content == "Partial ans"           # partial preserved
    assert response.finish_reason == "error"           # explicit error state
    assert "overloaded_error" in (
        response.raw_response or {}
    ).get("legba_stream_error", "")
    assert any("MID_STREAM_FAILURE" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_anthropic_mid_stream_failure_before_content_raises_transient():
    """A stream that dies before ANY content is a transient failure — there
    is no partial to preserve and no duplicate-generation risk on retry."""
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)

    events: list[dict[str, Any]] = [
        _ANTHROPIC_SSE_OK[0],
        {"type": "error",
         "error": {"type": "overloaded_error", "message": "early cut"}},
    ]
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _sse_response(events)

    await _install_mock_transport(h, handler)
    with pytest.raises(TransientLLMFailure, match="before any content"):
        await h.chat_complete(messages=[{"role": "user", "content": "q"}])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_anthropic_truncated_stream_without_message_stop_is_error_state():
    """An abrupt close (no error event, no message_stop) must never read as a
    complete answer — it comes back as the explicit error state."""
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)

    events = _ANTHROPIC_SSE_OK[:5]  # cut before message_delta/message_stop
    await _install_mock_transport(h, lambda request: _sse_response(events))
    response = await h.chat_complete(messages=[{"role": "user", "content": "q"}])
    assert response.content == "Hello streamed."
    assert response.finish_reason == "error"
    assert "message_stop" in (
        response.raw_response or {}
    ).get("legba_stream_error", "")


@pytest.mark.asyncio
async def test_anthropic_pre_stream_retryable_status_still_retries():
    """Pre-stream failures keep the base handler's retry semantics — a 529
    before the stream opens is retried and the second attempt succeeds."""
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                529, headers={"retry-after": "0"},
                json={"error": {"type": "overloaded_error"}},
            )
        return _sse_response(_ANTHROPIC_SSE_OK)

    await _install_mock_transport(h, handler)
    response = await h.chat_complete(messages=[{"role": "user", "content": "q"}])
    assert len(calls) == 2
    assert response.content == "Hello streamed."
    assert response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_anthropic_non_sse_json_body_still_parses():
    """An Anthropic-compatible proxy that ignores `stream: true` and returns
    the complete JSON body degrades gracefully — streaming is a transport
    optimization, never a correctness requirement."""
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-7")
    await h.on_configure(ctx)
    await _install_mock_transport(
        h, lambda request: httpx.Response(200, json=_ANTHROPIC_OK_BODY),
    )
    response = await h.chat_complete(messages=[{"role": "user", "content": "q"}])
    assert response.content == "Hello there."
    assert response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_anthropic_clamps_max_tokens_over_documented_ceiling(caplog):
    """A max_tokens above Anthropic's documented per-request output ceiling
    is clamped on the wire and logged loudly — once per model, not per call."""
    import logging as _logging

    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _sse_response(_ANTHROPIC_SSE_OK)

    await _install_mock_transport(h, handler)
    with caplog.at_level(
        _logging.WARNING, logger="legba.data.stack.llm.anthropic",
    ):
        await h.chat_complete(
            messages=[{"role": "user", "content": "q"}], max_tokens=200_000,
        )
        await h.chat_complete(
            messages=[{"role": "user", "content": "q"}], max_tokens=200_000,
        )
    assert captured[0]["max_tokens"] == 128_000
    assert captured[1]["max_tokens"] == 128_000
    clamp_logs = [r for r in caplog.records if "CLAMPED" in r.message]
    assert len(clamp_logs) == 1                        # once per model


@pytest.mark.asyncio
async def test_anthropic_in_budget_max_tokens_not_clamped():
    """The deployed 32768 consult budget passes through untouched — it sits
    well under the Opus line's 128k documented ceiling."""
    h = AnthropicProviderHandler()
    ctx = _make_ctx(handler_kind="anthropic", model="claude-opus-4-8")
    await h.on_configure(ctx)

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _sse_response(_ANTHROPIC_SSE_OK)

    await _install_mock_transport(h, handler)
    await h.chat_complete(
        messages=[{"role": "user", "content": "q"}], max_tokens=32_768,
    )
    assert captured[0]["max_tokens"] == 32_768


# ---------------------------------------------------------------------------
# chat_complete — request shape (OpenAI + vLLM)
# ---------------------------------------------------------------------------


_OPENAI_OK_BODY = {
    "id": "chatcmpl_01",
    "object": "chat.completion",
    "model": "gpt-4.1",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Reply text.",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120},
}


@pytest.mark.asyncio
async def test_openai_chat_complete_request_shape():
    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai", model="gpt-4.1")
    await h.on_configure(ctx)

    captured: list[dict[str, Any]] = []

    def handler(request):
        captured.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "json": json.loads(request.content),
        })
        return httpx.Response(200, json=_OPENAI_OK_BODY)

    await _install_mock_transport(h, handler)

    response = await h.chat_complete(
        messages=[{"role": "user", "content": "Hi"}],
        system="System prompt.",
        max_tokens=256,
        temperature=0.4,
    )
    assert response.content == "Reply text."
    assert response.finish_reason == "stop"
    assert response.usage.prompt_tokens == 80
    assert response.usage.completion_tokens == 40
    assert response.usage.cost_estimate_usd > 0

    req = captured[0]
    assert req["url"].endswith("/v1/chat/completions")
    assert req["headers"]["authorization"] == "Bearer sk-test-token"
    body = req["json"]
    # System message prepended into messages list.
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == "System prompt."
    assert body["messages"][1]["role"] == "user"
    assert body["max_tokens"] == 256
    assert body["temperature"] == 0.4
    assert body["model"] == "gpt-4.1"


@pytest.mark.asyncio
async def test_openai_reasoning_model_uses_max_completion_tokens():
    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai", model="o3")
    await h.on_configure(ctx)

    captured: list[dict[str, Any]] = []

    def handler(request):
        captured.append(json.loads(request.content))
        body = dict(_OPENAI_OK_BODY)
        body["model"] = "o3"
        body["usage"] = {
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
            "completion_tokens_details": {"reasoning_tokens": 30},
            "prompt_tokens_details": {"cached_tokens": 10},
        }
        return httpx.Response(200, json=body)

    await _install_mock_transport(h, handler)

    response = await h.chat_complete(
        messages=[{"role": "user", "content": "Hi"}],
        max_tokens=2000,
        reasoning_effort="high",
        temperature=0.7,  # reasoning model should ignore this
    )
    body = captured[0]
    assert "max_completion_tokens" in body
    assert body["max_completion_tokens"] == 2000
    assert "max_tokens" not in body
    assert body["reasoning_effort"] == "high"
    # temperature dropped because reasoning models reject it
    assert "temperature" not in body

    assert response.usage.reasoning_tokens == 30
    assert response.usage.cache_read_tokens == 10


@pytest.mark.asyncio
async def test_vllm_chat_complete_coalesces_multi_choice():
    """vLLM reasoning mode emits multiple choices (reasoning + final);
    parser must join them in order."""
    h = VLLMProviderHandler()
    ctx = _make_ctx(handler_kind="vllm", model="gpt-oss-120b")
    await h.on_configure(ctx)

    body = {
        "id": "x",
        "model": "gpt-oss-120b",
        "choices": [
            {"index": 0, "message": {"content": "first choice text"}, "finish_reason": "stop"},
            {"index": 1, "message": {"content": "second choice text"}, "finish_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
    }

    def handler(request):
        # vLLM endpoint is /chat/completions (not /v1/chat/completions —
        # the api_endpoint in config ends with /v1 already).
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json=body)

    await _install_mock_transport(h, handler)

    response = await h.chat_complete(
        messages=[{"role": "user", "content": "hi"}],
    )
    assert "first choice text" in response.content
    assert "second choice text" in response.content
    assert response.usage.prompt_tokens == 20
    assert response.usage.cost_estimate_usd == 0.0  # self-hosted


@pytest.mark.asyncio
async def test_vllm_tools_round_trip():
    h = VLLMProviderHandler()
    ctx = _make_ctx(handler_kind="vllm")
    await h.on_configure(ctx)

    body = {
        "model": "gpt-oss-120b",
        "choices": [{
            "index": 0,
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "search_signals",
                        "arguments": '{"query": "X", "limit": 3}',
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }

    def handler(request):
        sent = json.loads(request.content)
        # Tools passed through in OpenAI shape.
        assert sent["tools"][0]["type"] == "function"
        return httpx.Response(200, json=body)

    await _install_mock_transport(h, handler)

    response = await h.chat_complete(
        messages=[{"role": "user", "content": "hi"}],
        tools=OPENAI_TOOL_SPEC,
    )
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].name == "search_signals"
    assert response.tool_calls[0].arguments == {"query": "X", "limit": 3}


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4xx_maps_to_hard_failure():
    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai")
    await h.on_configure(ctx)

    def handler(request):
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    await _install_mock_transport(h, handler)

    with pytest.raises(HardLLMFailure) as exc:
        await h.chat_complete(messages=[{"role": "user", "content": "hi"}])
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_500_retries_then_raises_transient():
    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai")
    await h.on_configure(ctx)

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    await _install_mock_transport(h, handler)

    with pytest.raises(TransientLLMFailure):
        await h.chat_complete(messages=[{"role": "user", "content": "hi"}])
    # 3 retries + initial attempt = 4 calls expected
    assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# Budget reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_records_tokens_after_call():
    h = AnthropicProviderHandler()
    budget = _FakeBudgetReporter(envelope="ok")
    ctx = _make_ctx(handler_kind="anthropic", budget=budget)
    await h.on_configure(ctx)

    def handler(request):
        return httpx.Response(200, json=_ANTHROPIC_OK_BODY)

    await _install_mock_transport(h, handler)
    await h.chat_complete(
        messages=[{"role": "user", "content": "hi"}],
        ctx=ctx,
    )
    assert budget.envelope_calls == 1
    assert len(budget.records) == 1
    kind, amount, dim = budget.records[0]
    assert kind == "tokens"
    # 100 input + 50 output = 150 total reported
    assert amount == 150
    assert dim.startswith("anthropic:")


@pytest.mark.asyncio
async def test_budget_exhausted_blocks_call():
    from legba.data.stack.llm import BudgetExhausted

    h = OpenAIProviderHandler()
    budget = _FakeBudgetReporter(envelope="exhausted")
    ctx = _make_ctx(handler_kind="openai", budget=budget)
    await h.on_configure(ctx)

    # No transport needed — must fail before HTTP call.
    with pytest.raises(BudgetExhausted):
        await h.chat_complete(
            messages=[{"role": "user", "content": "hi"}],
            ctx=ctx,
        )
    assert budget.envelope_calls == 1
    assert budget.records == []  # no tokens recorded


# ---------------------------------------------------------------------------
# stream_complete fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_complete_default_yields_one_chunk():
    h = OpenAIProviderHandler()
    ctx = _make_ctx(handler_kind="openai")
    await h.on_configure(ctx)

    def handler(request):
        return httpx.Response(200, json=_OPENAI_OK_BODY)

    await _install_mock_transport(h, handler)

    chunks: list[LLMChunk] = []
    async for chunk in h.stream_complete(
        messages=[{"role": "user", "content": "hi"}],
    ):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0].delta_content == "Reply text."
    assert chunks[0].finish_reason == "stop"


# ---------------------------------------------------------------------------
# Stack-component descriptor round-trip
# ---------------------------------------------------------------------------


def test_handler_consumes_real_llm_provider_descriptor():
    """The pydantic LLMProvider schema from L-101 §5 → handler config.

    Builds a real `LLMProvider` stack-component descriptor (the same model
    the stack registry stores) and verifies the handler can extract its
    config without runtime help."""
    component = LLMProvider(
        id="llm.anthropic.opus_4_7",
        name="Anthropic Opus",
        schema_uri="legba/stack/llm_provider/1.0.0",
        version="0" * 16,
        owner="lewis@local",
        config=LLMProviderConfig(
            api_endpoint=Property.Text.of("https://api.anthropic.com"),
            api_key=Property.Secret.of("llm.anthropic.opus_4_7.api_key"),
            model_name=Property.Text.of("claude-opus-4-7"),
            max_tokens=Property.Number.of(8192, minimum=1, maximum=200000),
        ),
    )
    # The handler config_schema must accept the typed config directly.
    assert isinstance(component.config, AnthropicProviderHandler.config_schema)
    assert component.config.model_name.raw == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# Integration tests — guarded by env tokens
# ---------------------------------------------------------------------------


_VLLM_TOKEN = os.getenv("LEGBA_VLLM_TOKEN")
_ANTHROPIC_TOKEN = os.getenv("LEGBA_ANTHROPIC_TOKEN")
_OPENAI_TOKEN = os.getenv("LEGBA_OPENAI_TOKEN")


@pytest.mark.integration
@pytest.mark.skipif(not _VLLM_TOKEN, reason="LEGBA_VLLM_TOKEN not set")
@pytest.mark.asyncio
async def test_integration_vllm_token_completion():
    """Live hit against self-hosted gpt-oss-120b. Smoke check only —
    1 input token, max_completion_tokens guarded."""
    h = VLLMProviderHandler()
    cfg = _make_cfg(
        endpoint="https://llm.example.internal/v1",
        model="gpt-oss-120b",
        secret_id="integration.vllm.token",
    )
    ctx = _FakeCtx(
        instance_id="integration.vllm",
        instance_version="0" * 16,
        config=cfg,
        secrets=_FakeResolver({"integration.vllm.token": _VLLM_TOKEN.encode()}),
    )
    await h.on_configure(ctx)
    response = await h.chat_complete(
        messages=[{"role": "user", "content": "Say hi in one word."}],
        max_tokens=8,
        send_max_tokens=True,
    )
    assert response.usage.prompt_tokens > 0
    assert response.usage.completion_tokens >= 0
    assert response.content or response.tool_calls  # something came back


@pytest.mark.integration
@pytest.mark.skipif(not _ANTHROPIC_TOKEN, reason="LEGBA_ANTHROPIC_TOKEN not set")
@pytest.mark.asyncio
async def test_integration_anthropic_smoke():
    h = AnthropicProviderHandler()
    cfg = _make_cfg(
        endpoint="https://api.anthropic.com",
        model="claude-haiku-3-5",
        secret_id="integration.anthropic.token",
    )
    ctx = _FakeCtx(
        instance_id="integration.anthropic",
        instance_version="0" * 16,
        config=cfg,
        secrets=_FakeResolver({"integration.anthropic.token": _ANTHROPIC_TOKEN.encode()}),
    )
    await h.on_configure(ctx)
    response = await h.chat_complete(
        messages=[{"role": "user", "content": "Reply with: ok"}],
        max_tokens=8,
    )
    assert response.usage.prompt_tokens > 0
    assert response.usage.cost_estimate_usd >= 0
    assert response.content


@pytest.mark.integration
@pytest.mark.skipif(not _OPENAI_TOKEN, reason="LEGBA_OPENAI_TOKEN not set")
@pytest.mark.asyncio
async def test_integration_openai_smoke():
    h = OpenAIProviderHandler()
    cfg = _make_cfg(
        endpoint="https://api.openai.com",
        model="gpt-4.1-mini",
        secret_id="integration.openai.token",
    )
    ctx = _FakeCtx(
        instance_id="integration.openai",
        instance_version="0" * 16,
        config=cfg,
        secrets=_FakeResolver({"integration.openai.token": _OPENAI_TOKEN.encode()}),
    )
    await h.on_configure(ctx)
    response = await h.chat_complete(
        messages=[{"role": "user", "content": "Reply with: ok"}],
        max_tokens=8,
    )
    assert response.usage.prompt_tokens > 0
    assert response.content


def test_vllm_max_tokens_env_opt_in(monkeypatch):
    """Default: max_tokens is OMITTED (self-hosted server budget governs).
    LEGBA_LLM_SEND_MAX_TOKENS=1 includes it (hosted-API truncation guard).
    Per-call send_max_tokens kwarg keeps working independently of the env."""
    h = VLLMProviderHandler()
    common = dict(
        messages=[{"role": "user", "content": "hi"}],
        system=None,
        tools=None,
        model="m",
        max_tokens=2048,
        temperature=0.0,
        reasoning_effort=None,
    )
    monkeypatch.delenv("LEGBA_LLM_SEND_MAX_TOKENS", raising=False)
    assert "max_tokens" not in h._build_chat_payload(**common)  # noqa: SLF001
    assert (
        h._build_chat_payload(**common, send_max_tokens=True)["max_tokens"]  # noqa: SLF001
        == 2048
    )
    monkeypatch.setenv("LEGBA_LLM_SEND_MAX_TOKENS", "1")
    assert h._build_chat_payload(**common)["max_tokens"] == 2048  # noqa: SLF001
    monkeypatch.setenv("LEGBA_LLM_SEND_MAX_TOKENS", "false")
    assert "max_tokens" not in h._build_chat_payload(**common)  # noqa: SLF001
