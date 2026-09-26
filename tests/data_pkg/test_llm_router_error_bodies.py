# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A router's 200 that carries a provider error is a FAILED call, fleet-wide.

An OpenRouter-style router commits its headers before the upstream provider has
served, so an upstream overload comes back as **HTTP 200** with the real status
inside the body and no ``choices``::

    {"id": ..., "error": {"message": "Upstream error from Nvidia: Service
     temporarily overloaded", "code": 502}}

``_call_chat`` switched on ``response.status_code``, so its retry predicate
(``{429, 500, 502, 503, 529}``) never fired for this shape and ``_account_call``
recorded ``status="success"`` for **626** calls on the judge route between
2026-09-06 and 2026-09-08 -- the one surface that could have paged on a
two-day grader outage. The judge lane repaired it ABOVE the client, for the
verify judge only; the width grader and every analyst route share this client
and were latently exposed.

Every test here traverses the REAL client path -- a real concrete handler over
``httpx.MockTransport`` -> the real ``chat_complete`` -> the real
``_call_chat`` / ``_parse_response`` / ``_account_call`` -> the real
``run_accounting`` recorder. Nothing on the handler is patched.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Mapping

import httpx
import pytest

from legba.data import run_accounting
from legba.data.registry.credentials import MissingSecretError
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import VLLMProviderHandler
from legba.data.stack.llm.base import (
    HardLLMFailure,
    RouterStatus,
    TransientLLMFailure,
)

# ``asyncio_mode = "auto"`` (pyproject.toml) runs the async tests; the one
# synchronous test below is left unmarked on purpose.

_COMPONENT_ID = "llm.judge.openrouter_nemotron120b.openai_compat"
_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
_MESSAGES: list[Mapping[str, Any]] = [{"role": "user", "content": "grade this"}]


@contextmanager
def _account():
    """Bind a run account so the REAL ``record_llm_call`` has somewhere to go."""
    token = run_accounting.bind_run_accounting()
    try:
        yield
    finally:
        run_accounting.reset_run_accounting(token)


# ---------------------------------------------------------------------------
# Harness — a REAL handler with its transport swapped for a mock.
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
    def log(self, level, msg, /, **fields): ...

    def event(self, name, payload=None): ...

    def span(self, name, /, **attrs):
        class _S:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

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


async def _handler(responder) -> VLLMProviderHandler:
    """A configured OpenAI-compatible handler whose wire is ``responder``."""
    cfg = LLMProviderConfig(
        api_endpoint=Property.Text.of("https://openrouter.ai/api/v1"),
        api_key=Property.Secret.of("test.api_key"),
        model_name=Property.Text.of(_MODEL),
        max_tokens=Property.Number.of(1024, minimum=1, maximum=200000),
    )
    handler = VLLMProviderHandler()
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
        base_url="https://openrouter.ai",
        headers=handler._auth_headers(),  # noqa: SLF001
        transport=httpx.MockTransport(responder),
        timeout=httpx.Timeout(10.0),
    )
    return handler


def _ok_body() -> dict:
    return {
        "id": "chatcmpl-ok",
        "provider": "Nvidia",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "a verdict"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 90, "completion_tokens": 12, "total_tokens": 102},
    }


def _envelope_body(code: int | None = 502, **err_extra: Any) -> dict:
    """The LIVE shape: HTTP 200, a top-level error, no ``choices``."""
    err: dict[str, Any] = {
        "message": "Upstream error from Nvidia: Service temporarily overloaded"
    }
    if code is not None:
        err["code"] = code
    err.update(err_extra)
    return {"id": "gen-live", "error": err}


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The backoff is real code; waiting for it is not the thing under test."""
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(seconds: float, *args, **kwargs):
        # Record what the backoff ASKED for, then yield to the loop instead of
        # actually waiting — the schedule is the assertion, the wall clock is
        # not. Still a real await, so nothing that depends on a scheduling
        # point (httpx, the loop itself) changes shape under the patch.
        slept.append(seconds)
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return slept


# ---------------------------------------------------------------------------
# 1. The retry now fires — on the body, not the status line
# ---------------------------------------------------------------------------


async def test_router_200_wrapping_502_is_retried_then_succeeds(_no_real_sleep):
    """The 626-call class: two overloads, then a real answer."""
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) <= 2:
            return httpx.Response(200, json=_envelope_body(502))
        return httpx.Response(200, json=_ok_body())

    handler = await _handler(responder)
    with _account():
        response = await handler.chat_complete(_MESSAGES)
        rows = run_accounting.current_llm_calls()

    assert response.content == "a verdict"
    assert response.finish_reason == "stop"
    assert len(calls) == 3, "the in-body 502 must be retried, not accepted"
    assert _no_real_sleep == [1.0, 2.0] or _no_real_sleep == [1, 2], _no_real_sleep

    # ONE receipt: the retries are inside `_call_chat`, below the accounting
    # chokepoint, exactly as they have always been for a status-line 502.
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert "http_status" not in rows[0]
    assert "router_status" not in rows[0]


async def test_exhausted_router_502_raises_transient_with_both_statuses(
    _no_real_sleep,
):
    """Four straight overloads: a transient failure carrying 200 AND 502."""
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_envelope_body(502))

    handler = await _handler(responder)
    with _account():
        with pytest.raises(TransientLLMFailure) as excinfo:
            await handler.chat_complete(_MESSAGES)
        rows = run_accounting.current_llm_calls()

    assert len(calls) == 4, "1 attempt + 3 retries, the pre-existing budget"
    exc = excinfo.value

    # BOTH numbers survive on the exception — this is what keeps the judge
    # transport shim's receipts meaning what they meant.
    assert isinstance(exc.status, RouterStatus)
    assert int(exc.status) == 502
    assert exc.status.router_status == 200
    assert str(exc.status) == "200/502"

    # THE FIX, in the receipt: `success` is now `transient_fail`, and the row
    # names the inner code AND the router's.
    assert len(rows) == 1
    assert rows[0]["status"] == "transient_fail"
    assert rows[0]["error"] == "TransientLLMFailure"
    assert rows[0]["http_status"] == 502
    assert rows[0]["router_status"] == 200


async def test_router_200_wrapping_400_is_not_retried(_no_real_sleep):
    """An inner 4xx is a verdict about the request — the non-retryable path."""
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200, json=_envelope_body(400, message="model not serviceable")
        )

    handler = await _handler(responder)
    with _account():
        with pytest.raises(HardLLMFailure) as excinfo:
            await handler.chat_complete(_MESSAGES)
        rows = run_accounting.current_llm_calls()

    assert len(calls) == 1, "a 4xx cannot be fixed by asking again"
    assert _no_real_sleep == []
    assert str(excinfo.value.status) == "200/400"
    assert rows[0]["status"] == "hard_fail"
    assert rows[0]["http_status"] == 400
    assert rows[0]["router_status"] == 200


async def test_inner_429_retries_and_honours_envelope_retry_after(_no_real_sleep):
    """``Retry-After`` echoed inside ``error.metadata.headers`` wins the backoff."""
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json=_envelope_body(
                    429, metadata={"headers": {"Retry-After": "7"}}
                ),
            )
        return httpx.Response(200, json=_ok_body())

    handler = await _handler(responder)
    response = await handler.chat_complete(_MESSAGES)

    assert response.content == "a verdict"
    assert _no_real_sleep == [7.0], "the provider's own instruction, not 2**attempt"


async def test_unnamed_inner_error_is_retried(_no_real_sleep):
    """An envelope with no parseable code is an unexplained failure, so: retry."""
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json=_envelope_body(None))
        return httpx.Response(200, json=_ok_body())

    handler = await _handler(responder)
    assert (await handler.chat_complete(_MESSAGES)).content == "a verdict"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 2. The per-choice shape — and the partial generation it must NOT discard
# ---------------------------------------------------------------------------


async def test_per_choice_error_with_no_content_is_a_provider_error(_no_real_sleep):
    """``choices[0].error`` with nothing generated is the same failure."""
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "gen-mid",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": ""},
                            "finish_reason": "error",
                            "error": {
                                "code": 502,
                                "message": "Provider disconnected mid-stream",
                            },
                        }
                    ],
                },
            )
        return httpx.Response(200, json=_ok_body())

    handler = await _handler(responder)
    assert (await handler.chat_complete(_MESSAGES)).content == "a verdict"
    assert len(calls) == 2


async def test_partial_generation_alongside_an_error_is_kept(_no_real_sleep):
    """Tokens already generated and already billed are never thrown away.

    OpenRouter can return the prefix a provider produced before it dropped. That
    response is degraded, not absent: it comes back, with ``finish_reason`` the
    body's own ``"error"`` so the receipt still says so out loud.
    """
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "id": "gen-partial",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "partial out"},
                        "finish_reason": "error",
                        "error": {"code": 502, "message": "disconnected"},
                    }
                ],
            },
        )

    handler = await _handler(responder)
    with _account():
        response = await handler.chat_complete(_MESSAGES)
        rows = run_accounting.current_llm_calls()

    assert len(calls) == 1, "a partial answer is not retried — it is already billed"
    assert response.content == "partial out"
    assert response.finish_reason == "error"
    assert rows[0]["status"] == "success"


# ---------------------------------------------------------------------------
# 3. Nothing else moved
# ---------------------------------------------------------------------------


async def test_a_normal_200_is_unchanged(_no_real_sleep):
    """The byte-identity check: one call, one success receipt, no new fields."""

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body())

    handler = await _handler(responder)
    with _account():
        response = await handler.chat_complete(_MESSAGES)
        rows = run_accounting.current_llm_calls()

    assert response.content == "a verdict"
    assert _no_real_sleep == []
    assert rows[0]["status"] == "success"
    assert rows[0]["finish_reason"] == "stop"
    assert rows[0]["served_by"] == "Nvidia"
    assert "http_status" not in rows[0] and "router_status" not in rows[0]


async def test_a_genuinely_empty_body_is_still_an_empty_answer(_no_real_sleep):
    """``judge_empty`` keeps its meaning: no envelope, no error, no retry.

    ``choices: []`` with no error object is the platform's existing honest
    "the model answered and said nothing" — it must NOT become a transport
    failure, or the one reason code that distinguishes a silent model from a
    broken route loses its meaning.
    """
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"id": "gen-empty", "choices": []})

    handler = await _handler(responder)
    with _account():
        response = await handler.chat_complete(_MESSAGES)
        rows = run_accounting.current_llm_calls()

    assert len(calls) == 1
    assert response.content == ""
    assert response.finish_reason == "error"
    assert rows[0]["status"] == "success"


async def test_status_line_502_is_unchanged(_no_real_sleep):
    """The pre-existing predicate still owns the pre-existing shape."""
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=_ok_body())

    handler = await _handler(responder)
    assert (await handler.chat_complete(_MESSAGES)).content == "a verdict"
    assert len(calls) == 2
    assert _no_real_sleep == [1]


# ---------------------------------------------------------------------------
# 4. The receipt token the judge transport shim stamps
# ---------------------------------------------------------------------------


async def test_the_shim_still_stamps_200_502_from_the_raised_error(_no_real_sleep):
    """`judge_transport._exception_status` is `str(exc.status)`; pin the token.

    The shim is NOT edited by this lane. It classifies a raised call by
    stringifying the exception's ``status``, so ``RouterStatus`` is the whole
    compatibility contract: the receipts on the verify row keep reading
    ``"200/502"``, and ``status_is_retryable`` keeps splitting it back to 502.
    """
    from legba.data.provenance.judge_transport import (
        _exception_status,
        status_is_retryable,
    )

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope_body(502))

    handler = await _handler(responder)
    with pytest.raises(TransientLLMFailure) as excinfo:
        await handler.chat_complete(_MESSAGES)

    token = _exception_status(excinfo.value)
    assert token == "200/502"
    assert status_is_retryable(token) is True


async def test_the_shim_refuses_to_retry_a_router_wrapped_4xx(_no_real_sleep):
    """An inner 400 must remain non-retryable at BOTH layers."""
    from legba.data.provenance.judge_transport import (
        _exception_status,
        status_is_retryable,
    )

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope_body(400))

    handler = await _handler(responder)
    with pytest.raises(HardLLMFailure) as excinfo:
        await handler.chat_complete(_MESSAGES)

    token = _exception_status(excinfo.value)
    assert token == "200/400"
    assert status_is_retryable(token) is False


def test_router_status_serialises_as_the_inner_code():
    """The receipt field is a plain number to every reader that already had one."""
    import json

    status = RouterStatus(502, router=200)
    assert json.dumps({"http_status": int(status)}) == '{"http_status": 502}'
    assert status == 502 and status >= 500
    assert str(status) == "200/502"
