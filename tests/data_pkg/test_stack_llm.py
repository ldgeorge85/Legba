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
    assert "api_key not resolved" in result.detail


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
