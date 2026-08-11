# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R11 — ``analyst_traces.llm_calls`` / ``tool_calls`` are actually populated.

Both columns have existed since the baseline migration
(``jsonb NOT NULL DEFAULT '[]'``) and NOTHING ever wrote them: every row
all-time carried ``[]``, so a receipt could not evidence whether the run called
a model at all — provenance survived only on the ``analyst_critiques`` rows.

The collection mechanism is :mod:`legba.data.run_accounting`: a task-local
per-run account that the provider chokepoint
(:meth:`LLMProviderHandler.chat_complete`) and the agency chokepoint
(:meth:`Agency.run_pack_tool`) append to, flushed into the receipt by
``AnalystActor.run``.

These tests traverse the REAL path end to end with no live Postgres / provider:
a real concrete provider handler over ``httpx.MockTransport`` → the real
``chat_complete`` → the real recorder → the real
:meth:`RuntimeReceiptChain.record` → the captured ``analyst_traces`` INSERT.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

import httpx
import pytest

from legba.data import run_accounting
from legba.data.analysts.agency.agency import Agency
from legba.data.analysts.agency.tools import ToolCall, ToolContext, ToolRegistry
from legba.data.provenance.receipts import RuntimeReceiptChain
from legba.data.registry.credentials import MissingSecretError
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.schemas.action_pack import ActionPack
from legba.data.stack.llm import OpenAIProviderHandler

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


_COMPONENT_ID = "llm.primary.openai_compat"
_MODEL = "gpt-oss-120b"


def _completion_body(*, prompt_tokens: int = 900, completion_tokens: int = 120) -> dict:
    return {
        "id": "chatcmpl-r11",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "a finding"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def _handler(responder) -> OpenAIProviderHandler:
    """A configured OpenAI-compat handler whose wire is ``responder``."""
    cfg = LLMProviderConfig(
        api_endpoint=Property.Text.of("https://llm.example.internal"),
        api_key=Property.Secret.of("test.api_key"),
        model_name=Property.Text.of(_MODEL),
        max_tokens=Property.Number.of(1024, minimum=1, maximum=200000),
    )
    handler = OpenAIProviderHandler()
    await handler.on_configure(
        _FakeCtx(
            instance_id=_COMPONENT_ID,
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


_MESSAGES: list[Mapping[str, Any]] = [{"role": "user", "content": "assess Iran"}]


# ---------------------------------------------------------------------------
# Fake asyncpg pool — captures the analyst_traces INSERT (same shape the
# trace-only dispatch test uses), so the REAL RuntimeReceiptChain runs.
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, store: dict):
        self._store = store

    async def fetchrow(self, *_a, **_k):
        return None

    async def fetchval(self, *_a, **_k):
        return 0

    async def execute(self, query: str, *args):
        assert "INSERT INTO analyst_traces" in query
        self._store["args"] = args
        # receipts.py column order: … intermediate_steps($10), llm_calls($11),
        # tool_calls($12) …
        self._store["llm_calls"] = json.loads(args[10])
        self._store["tool_calls"] = json.loads(args[11])
        self._store["prompt_rendered"] = args[8]


class _FakePool:
    def __init__(self):
        self.store: dict = {}
        self._conn = _FakeConn(self.store)

    def acquire(self):
        pool = self

        class _Acq:
            async def __aenter__(self):
                return pool._conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


async def _record(pool: _FakePool, *, llm_calls, tool_calls) -> None:
    """Drive the REAL receipt chain exactly as ``AnalystActor.run`` does."""
    chain = RuntimeReceiptChain(pool)  # type: ignore[arg-type]
    now = datetime.now(timezone.utc)
    await chain.record(
        run_id=uuid4(),
        analyst_id="signal_salience",
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
        llm_calls=llm_calls or None,
        tool_calls=tool_calls or None,
    )


# ---------------------------------------------------------------------------
# 1) An LLM run lands a populated llm_calls carrying the model + stack ref.
# ---------------------------------------------------------------------------


async def test_llm_run_lands_llm_calls_in_the_trace_row():
    handler = await _handler(
        lambda _req: httpx.Response(200, json=_completion_body())
    )
    token = run_accounting.bind_run_accounting()
    try:
        resp = await handler.chat_complete(_MESSAGES, system="you are a desk")
        assert resp.content == "a finding"
        calls = run_accounting.current_llm_calls()
        pool = _FakePool()
        await _record(pool, llm_calls=calls, tool_calls=[])
    finally:
        run_accounting.reset_run_accounting(token)
        await handler.on_retire(None)  # type: ignore[arg-type]

    persisted = pool.store["llm_calls"]
    assert len(persisted) == 1, persisted
    entry = persisted[0]
    # The two things the receipt could never evidence before: that a model was
    # called at all, and WHICH stack ref served it.
    assert entry["model"] == _MODEL
    assert entry["component_id"] == _COMPONENT_ID
    assert entry["subprovider"] == "openai"
    assert entry["status"] == "success"
    assert entry["prompt_tokens"] == 900
    assert entry["completion_tokens"] == 120
    assert entry["total_tokens"] == 1020
    assert entry["finish_reason"] == "stop"
    assert entry["duration_ms"] >= 0
    assert entry["at"]


async def test_prompt_is_evidenced_by_digest_not_by_its_text():
    """``prompt_rendered`` stays NULL; the bounded digest carries the evidence.

    Persisting the rendered prompt would put up to the full 32k-token input
    budget on every one of ~12.5k traces/48h. The sha256 + char count let an
    auditor re-render and prove identity without the bloat, and are two scalars
    rather than an unbounded blob.
    """
    handler = await _handler(
        lambda _req: httpx.Response(200, json=_completion_body())
    )
    token = run_accounting.bind_run_accounting()
    try:
        await handler.chat_complete(_MESSAGES, system="you are a desk")
        calls = run_accounting.current_llm_calls()
        pool = _FakePool()
        await _record(pool, llm_calls=calls, tool_calls=[])
    finally:
        run_accounting.reset_run_accounting(token)
        await handler.on_retire(None)  # type: ignore[arg-type]

    entry = pool.store["llm_calls"][0]
    assert len(entry["prompt_sha256"]) == 64
    assert entry["prompt_chars"] > 0
    assert pool.store["prompt_rendered"] is None
    # The digest is stable + discriminating.
    same, _ = run_accounting.prompt_digest(
        [{"role": "system", "content": "you are a desk"}, *_MESSAGES], None,
    )
    other, _ = run_accounting.prompt_digest(
        [{"role": "system", "content": "you are a desk"},
         {"role": "user", "content": "assess Peru"}], None,
    )
    assert same == entry["prompt_sha256"]
    assert other != entry["prompt_sha256"]


# ---------------------------------------------------------------------------
# 2) A deterministic (no-LLM) run records [] — matching the column default.
# ---------------------------------------------------------------------------


async def test_deterministic_run_records_empty_lists_not_null():
    token = run_accounting.bind_run_accounting()
    try:
        assert run_accounting.current_llm_calls() == []
        assert run_accounting.current_tool_calls() == []
        pool = _FakePool()
        await _record(
            pool,
            llm_calls=run_accounting.current_llm_calls(),
            tool_calls=run_accounting.current_tool_calls(),
        )
    finally:
        run_accounting.reset_run_accounting(token)

    # The column is `jsonb NOT NULL DEFAULT '[]'::jsonb` — an empty list, never
    # NULL, so "no LLM was called" and "we didn't look" stay distinguishable
    # only via the row existing at all.
    assert pool.store["llm_calls"] == []
    assert pool.store["tool_calls"] == []
    assert pool.store["args"][10] == "[]"


# ---------------------------------------------------------------------------
# 3) Failures are evidence too — and never leak the provider body.
# ---------------------------------------------------------------------------


async def test_failed_call_is_recorded_with_class_name_only():
    body = "invalid api key sk-live-DEADBEEF for account acme"
    handler = await _handler(lambda _req: httpx.Response(401, text=body))
    token = run_accounting.bind_run_accounting()
    try:
        with pytest.raises(Exception):
            await handler.chat_complete(_MESSAGES)
        calls = run_accounting.current_llm_calls()
    finally:
        run_accounting.reset_run_accounting(token)
        await handler.on_retire(None)  # type: ignore[arg-type]

    assert len(calls) == 1
    entry = calls[0]
    assert entry["status"] == "hard_fail"
    assert entry["error"] == "HardLLMFailure"
    assert entry["http_status"] == 401
    assert entry["model"] == _MODEL
    # The receipt must never carry a provider message — 4xx bodies echo request
    # content and can name credentials.
    blob = json.dumps(entry)
    assert "sk-live-DEADBEEF" not in blob
    assert "acme" not in blob


# ---------------------------------------------------------------------------
# 4) Isolation — no cross-actor leakage, no global state.
# ---------------------------------------------------------------------------


async def test_unbound_account_is_a_noop():
    """Outside a run (the registry process, the filter plane, scripts) nothing
    accumulates and nothing raises."""
    handler = await _handler(
        lambda _req: httpx.Response(200, json=_completion_body())
    )
    try:
        resp = await handler.chat_complete(_MESSAGES)
        assert resp.content == "a finding"
        assert run_accounting.current_llm_calls() == []
    finally:
        await handler.on_retire(None)  # type: ignore[arg-type]


async def test_concurrent_runs_do_not_leak_into_each_other():
    """Two analysts on one event loop, sharing ONE cached provider handler.

    The handlers are process-cached (``dapr_host._llm_handler_cache``), so any
    per-handler or module-global accumulator would cross-contaminate here.
    """
    handler = await _handler(
        lambda _req: httpx.Response(200, json=_completion_body())
    )

    async def one_run(n: int) -> list[dict]:
        token = run_accounting.bind_run_accounting()
        try:
            for _ in range(n):
                await handler.chat_complete(
                    [{"role": "user", "content": f"run-of-{n}"}]
                )
                await asyncio.sleep(0)  # interleave the two runs
            return run_accounting.current_llm_calls()
        finally:
            run_accounting.reset_run_accounting(token)

    try:
        a, b = await asyncio.gather(one_run(2), one_run(5))
    finally:
        await handler.on_retire(None)  # type: ignore[arg-type]

    assert len(a) == 2
    assert len(b) == 5


async def test_calls_from_a_child_task_land_in_the_runs_account():
    """A GATHER fan-out spawns tasks; their calls belong to the RUN."""
    handler = await _handler(
        lambda _req: httpx.Response(200, json=_completion_body())
    )
    token = run_accounting.bind_run_accounting()
    try:
        await asyncio.gather(
            *(handler.chat_complete(_MESSAGES) for _ in range(3))
        )
        calls = run_accounting.current_llm_calls()
    finally:
        run_accounting.reset_run_accounting(token)
        await handler.on_retire(None)  # type: ignore[arg-type]
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# 5) Bounded, and never able to fail a run.
# ---------------------------------------------------------------------------


async def test_the_list_is_capped_and_the_overflow_is_visible():
    token = run_accounting.bind_run_accounting()
    try:
        for i in range(run_accounting._MAX_CALLS + 5):  # noqa: SLF001
            run_accounting.record_llm_call(model=f"m{i}", status="success")
        calls = run_accounting.current_llm_calls()
    finally:
        run_accounting.reset_run_accounting(token)
    assert len(calls) == run_accounting._MAX_CALLS + 1  # noqa: SLF001
    assert calls[-1] == {"truncated": 5}


async def test_accounting_failure_never_fails_the_call(monkeypatch):
    """A broken recorder must not turn a successful LLM call into an error."""
    import legba.data.stack.llm.base as llm_base

    def _boom(**_fields):
        raise RuntimeError("accounting exploded")

    monkeypatch.setattr(llm_base, "record_llm_call", _boom)
    handler = await _handler(
        lambda _req: httpx.Response(200, json=_completion_body())
    )
    token = run_accounting.bind_run_accounting()
    try:
        resp = await handler.chat_complete(_MESSAGES)
        assert resp.content == "a finding"
        assert run_accounting.current_llm_calls() == []
    finally:
        run_accounting.reset_run_accounting(token)
        await handler.on_retire(None)  # type: ignore[arg-type]


async def test_accounting_failure_does_not_mask_the_provider_error(monkeypatch):
    """The recorder runs in a ``finally``; a raise there would swallow the
    provider exception on its way out. It must not."""
    import legba.data.stack.llm.base as llm_base

    def _boom(**_fields):
        raise RuntimeError("accounting exploded")

    monkeypatch.setattr(llm_base, "record_llm_call", _boom)
    handler = await _handler(lambda _req: httpx.Response(401, text="nope"))
    try:
        with pytest.raises(llm_base.HardLLMFailure):
            await handler.chat_complete(_MESSAGES)
    finally:
        await handler.on_retire(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 6) The agency chokepoint feeds tool_calls — including BLOCKED calls.
# ---------------------------------------------------------------------------


class _StubConn:
    """Enough asyncpg surface for the governor-event INSERT on the block path."""

    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, query: str, *_args):
        self.statements.append(query)


def _pack(pack_id: str, *, tools: list[str]) -> ActionPack:
    return ActionPack.model_validate(
        {
            "identity": {
                "id": pack_id, "name": pack_id,
                "schema_uri": "legba/action_pack/1.0.0",
                "version": "b" * 16, "state": "active", "owner": "r11",
                "created": datetime.now(timezone.utc).isoformat(),
            },
            "tools": [{"name": t} for t in tools],
            "applies_to_tags": [],
        },
        strict=False,
    )


async def test_blocked_tool_call_still_reaches_the_receipt():
    """A BLOCK is exactly the thing a receipt should be able to show.

    The analyst asked for a capability it does not hold; today that is visible
    only in ``governor_events``. Now the run's own trace carries it too.
    """
    from legba.data.analysts.agency.resolution import TargetScopeView

    agency = Agency(tool_registry=ToolRegistry())
    pack = _pack("substrate_read", tools=["search_corpus"])
    conn = _StubConn()
    token = run_accounting.bind_run_accounting()
    try:
        outcome = await agency.run_pack_tool(
            conn,  # type: ignore[arg-type]
            pack=pack,
            call=ToolCall(pack_id="substrate_read", tool_name="search_corpus"),
            analyst_grants=None,          # not granted → RESOLVE blocks
            target_allows=None,
            scope=TargetScopeView(target_id="iran", tags=[]),
            ctx=ToolContext(),
        )
        tool_calls = run_accounting.current_tool_calls()
    finally:
        run_accounting.reset_run_accounting(token)

    assert outcome.admitted is False
    assert len(tool_calls) == 1
    entry = tool_calls[0]
    assert entry["source"] == "agency"
    assert entry["pack"] == "substrate_read"
    assert entry["name"] == "search_corpus"
    assert entry["status"] == "blocked"
    assert entry["block_cause"] == outcome.block_cause
    assert entry["admitted"] is False
    # The durable governor ledger is untouched by the instrumentation.
    assert any("governor_events" in s for s in conn.statements)


async def test_agency_accounting_is_a_noop_outside_a_run():
    from legba.data.analysts.agency.resolution import TargetScopeView

    agency = Agency(tool_registry=ToolRegistry())
    outcome = await agency.run_pack_tool(
        _StubConn(),  # type: ignore[arg-type]
        pack=_pack("substrate_read", tools=["search_corpus"]),
        call=ToolCall(pack_id="substrate_read", tool_name="search_corpus"),
        analyst_grants=None,
        target_allows=None,
        scope=TargetScopeView(target_id="iran", tags=[]),
        ctx=ToolContext(),
    )
    assert outcome.admitted is False
    assert run_accounting.current_tool_calls() == []


# ---------------------------------------------------------------------------
# 7) Tool ARGUMENTS — bounded + secret-redacted.
#
# Without them the tool leg of a receipt is unfalsifiable: two `search_corpus`
# calls with completely different queries are indistinguishable in the audit
# record. They were previously omitted on the stated grounds that they "already
# live in action_pack_invocations" — they do not; that table has no args column
# (verified against the live schema), so the arguments were recorded nowhere.
#
# The receipt is durable, hash-chained, retained 45 days and leaves the box via
# substrate_export, so the redaction half of this is a security property, not a
# nicety.
# ---------------------------------------------------------------------------


async def test_args_are_recorded_so_two_calls_are_distinguishable():
    a = run_accounting.redact_tool_args({"query": "iran drone strike", "limit": 20})
    b = run_accounting.redact_tool_args({"query": "sudan famine", "limit": 20})
    assert a["query"] == "iran drone strike"
    assert a["limit"] == 20
    assert a != b


@pytest.mark.parametrize(
    "key",
    [
        "api_key", "apiKey", "API_KEY", "key", "map_key", "primary_key",
        "token", "auth_token", "authToken", "access.token", "password",
        "passwd", "pwd", "secret", "client_secret", "credential", "creds",
        "session", "cookie", "authorization", "private_key", "signature",
        "api_hash", "apiHash",
    ],
)
async def test_secret_named_args_are_redacted(key):
    out = run_accounting.redact_tool_args({key: "hunter2-the-real-thing"})
    assert out[key] == "[redacted]", f"{key} leaked a credential into the receipt"
    assert "hunter2" not in json.dumps(out)


@pytest.mark.parametrize(
    "key", ["keyword", "monkey", "content_hash", "signal_hash", "passage", "author"],
)
async def test_innocent_names_that_merely_contain_a_secret_token_survive(key):
    """Over-redaction is the chosen default, but it has to stop somewhere:
    substring matching would eat `keyword` and `monkey`, and putting `hash` in
    the token set would eat `content_hash` — load-bearing forensics, not
    secrets. Tokenised matching plus a narrow compound list is the line."""
    out = run_accounting.redact_tool_args({key: "value-here"})
    assert out[key] == "value-here"


@pytest.mark.parametrize(
    "value",
    [
        "sk-abcdef0123456789",
        "Bearer abcdef.0123456789",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
async def test_secret_shaped_values_are_redacted_under_an_innocent_key(value):
    """The second net: a credential passed as `q` or `note` still goes."""
    out = run_accounting.redact_tool_args({"note": value})
    assert out["note"] == "[redacted]"


async def test_nested_secrets_are_redacted():
    out = run_accounting.redact_tool_args(
        {"upstream": {"url": "https://x.invalid", "headers": {"authorization": "abc"}}}
    )
    assert out["upstream"]["headers"]["authorization"] == "[redacted]"
    assert out["upstream"]["url"] == "https://x.invalid"
    assert "abc" not in json.dumps(out)


async def test_args_are_bounded_on_every_axis():
    long_value = "x" * 5_000
    out = run_accounting.redact_tool_args({"q": long_value})
    assert len(out["q"]) < 400 and out["q"].startswith("xxx")

    out = run_accounting.redact_tool_args({"ids": list(range(500))})
    assert len(out["ids"]) == 11 and "more" in str(out["ids"][-1])

    many = {f"k{i}": i for i in range(200)}
    out = run_accounting.redact_tool_args(many)
    assert out["_truncated_keys"] == 200 - 25

    deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    assert "depth" in json.dumps(run_accounting.redact_tool_args(deep))


async def test_oversize_args_collapse_to_key_names():
    """The backstop: many individually-small keys still add up. The call stays
    identifiable (its key names survive) and the row stays small."""
    out = run_accounting.redact_tool_args(
        {f"field_{i}": "y" * 150 for i in range(20)}
    )
    assert "_oversize_chars" in out
    assert out["_keys"][0].startswith("field_")
    assert len(json.dumps(out)) < 2_000


async def test_redaction_never_raises():
    class _Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

        def __str__(self):
            raise RuntimeError("no str either")

    out = run_accounting.redact_tool_args({"bad": _Hostile()})
    assert out == {"_unrenderable": True}
    assert run_accounting.redact_tool_args(None) == {}
    assert run_accounting.redact_tool_args("not-a-dict") == {"_args": "not-a-dict"}


async def test_blocked_call_records_the_args_it_was_blocked_for():
    """A block is when you most want the arguments — they are the whole
    argument about whether the governor was right."""
    from legba.data.analysts.agency.resolution import TargetScopeView

    agency = Agency(tool_registry=ToolRegistry())
    token = run_accounting.bind_run_accounting()
    try:
        await agency.run_pack_tool(
            _StubConn(),  # type: ignore[arg-type]
            pack=_pack("substrate_read", tools=["search_corpus"]),
            call=ToolCall(
                pack_id="substrate_read",
                tool_name="search_corpus",
                args={"query": "iran drone strike", "api_key": "leak-me"},
            ),
            analyst_grants=None,
            target_allows=None,
            scope=TargetScopeView(target_id="iran", tags=[]),
            ctx=ToolContext(),
        )
        tool_calls = run_accounting.current_tool_calls()
    finally:
        run_accounting.reset_run_accounting(token)

    assert len(tool_calls) == 1
    args = tool_calls[0]["args"]
    assert args["query"] == "iran drone strike"
    assert args["api_key"] == "[redacted]"
    assert "leak-me" not in json.dumps(tool_calls)
