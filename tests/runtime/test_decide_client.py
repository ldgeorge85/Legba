# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""the decision client (``POST {api_endpoint}/decide``).

Every test drives the REAL handler: a :class:`DecisionProviderHandler`
configured through ``on_configure`` (the vault resolve included), its
transport swapped for ``httpx.MockTransport`` (the
``test_llm_router_error_bodies`` idiom), the real ``decide`` → ``_post_decide``
→ ``parse_answers`` → ``_account_decide`` → ``run_accounting`` path. No network:
the handler's configure-time model-list probe is a no-op by design.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from legba.data import run_accounting
from legba.data.registry.credentials import MissingSecretError
from legba.data.schemas import LLMProviderConfig, Property
from legba.data.stack.llm import LLM_HANDLERS, DecisionProviderHandler
from legba.data.stack.llm.base import HardLLMFailure, TransientLLMFailure
from tests._gateway_env import GATEWAY_ENV

_COMPONENT_ID = "llm.decide.gateway_typed_choice"
_REPO = Path(__file__).resolve().parents[2]
_YAML = _REPO / "descriptors" / "stack_component_llm_decide_gateway_typed_choice.yaml"

_QUESTIONS = {
    "bears": {
        "type": "choice",
        "instructions": "Does the new evidence bear on the question?",
        "criteria": {"yes": "it bears", "no": "it does not"},
    }
}


def _ok_body(choice: str = "yes", p_yes: float = 0.9143) -> dict[str, Any]:
    return {
        "model": "decision-model",
        "answers": {
            "bears": {
                "type": "choice",
                "choice": choice,
                "confidence": max(p_yes, 1 - p_yes),
                "probabilities": {"yes": p_yes, "no": round(1 - p_yes, 4)},
            }
        },
        "usage": {"input_tokens": 317, "output_tokens": 0},
    }


@contextmanager
def _account():
    token = run_accounting.bind_run_accounting()
    try:
        yield
    finally:
        run_accounting.reset_run_accounting(token)


class _Resolver:
    def __init__(self, secrets: dict[str, bytes]):
        self._secrets = secrets

    async def verify_exists(self, secret_id: str) -> bool:
        return secret_id in self._secrets

    async def resolve(self, secret_id: str) -> bytes:
        if secret_id not in self._secrets:
            raise MissingSecretError(secret_id)
        return self._secrets[secret_id]


class _Tel:
    def log(self, level, msg, /, **fields): ...

    def event(self, name, payload=None): ...

    def span(self, name, /, **attrs):  # pragma: no cover
        raise AssertionError("unused")


@dataclass
class _Ctx:
    instance_id: str
    instance_version: str
    config: LLMProviderConfig
    secrets: Any
    budget: Any = None

    def telemetry(self):
        return _Tel()


def _config(*, max_concurrent: int | None = None) -> LLMProviderConfig:
    kwargs: dict[str, Any] = {}
    if max_concurrent is not None:
        kwargs["max_concurrent"] = Property.Number.of(max_concurrent, minimum=1, maximum=1000)
    return LLMProviderConfig(
        api_endpoint=Property.Text.of("https://gateway.example.invalid/v1"),
        api_key=Property.Secret.of("llm.gateway.api_key"),
        model_name=Property.Text.of("decision-model"),
        max_tokens=Property.Number.of(1, minimum=1, maximum=200000),
        **kwargs,
    )


async def _handler(responder, *, max_concurrent: int | None = None) -> DecisionProviderHandler:
    handler = DecisionProviderHandler()
    await handler.on_configure(
        _Ctx(
            instance_id=_COMPONENT_ID,
            instance_version="0" * 16,
            config=_config(max_concurrent=max_concurrent),
            secrets=_Resolver({"llm.gateway.api_key": b"tok-test"}),
        )
    )
    assert handler._client is None, "configure must make no network call"  # noqa: SLF001
    handler._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://gateway.example.invalid",
        headers=handler._auth_headers(),  # noqa: SLF001
        transport=httpx.MockTransport(responder),
        timeout=httpx.Timeout(10.0),
    )
    return handler


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(seconds: float, *args, **kwargs):
        slept.append(seconds)
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return slept


# ---------------------------------------------------------------------------
# 200 — the wire shape, the parse, the receipt
# ---------------------------------------------------------------------------


async def test_a_200_is_parsed_and_the_request_is_the_decide_contract():
    seen: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok_body("yes", 0.9143))

    handler = await _handler(responder)
    with _account():
        answers = await handler.decide("Question thesis: x\nNews item: y", _QUESTIONS, leg="bearing_shadow")
        calls = run_accounting.current_llm_calls()

    assert answers == {
        "bears": {
            "type": "choice",
            "choice": "yes",
            "confidence": 0.9143,
            "probabilities": {"yes": 0.9143, "no": 0.0857},
        }
    }
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/v1/decide"
    assert req.headers["authorization"] == "Bearer tok-test"
    body = json.loads(req.content)
    assert body == {
        "model": "decision-model",
        "state": "Question thesis: x\nNews item: y",
        "questions": _QUESTIONS,
    }
    assert "max_tokens" not in body, "a decision call generates no tokens"

    (rec,) = calls
    assert rec["component_id"] == _COMPONENT_ID
    assert rec["subprovider"] == "decide"
    assert rec["model"] == "decision-model"
    assert rec["status"] == "success" and rec["http_status"] == 200
    assert rec["leg"] == "bearing_shadow"
    assert rec["prompt_tokens"] == 317 and rec["completion_tokens"] == 0
    assert isinstance(rec["duration_ms"], int)
    assert rec["prompt_sha256"] and "x\\nNews" not in json.dumps(rec)  # digested, not stored


async def test_an_answer_outside_the_criteria_is_a_hard_failure():
    def responder(request):
        body = _ok_body()
        body["answers"]["bears"]["choice"] = "maybe"
        return httpx.Response(200, json=body)

    handler = await _handler(responder)
    with _account():
        with pytest.raises(HardLLMFailure, match="not one of its criteria"):
            await handler.decide("s", _QUESTIONS)
        (rec,) = run_accounting.current_llm_calls()
    assert rec["status"] == "hard_fail"


async def test_a_missing_answer_is_a_hard_failure():
    handler = await _handler(lambda r: httpx.Response(200, json={"answers": {}}))
    with pytest.raises(HardLLMFailure, match="no answer for question 'bears'"):
        await handler.decide("s", _QUESTIONS)


async def test_questions_without_criteria_never_reach_the_wire():
    seen: list[int] = []
    handler = await _handler(lambda r: seen.append(1) or httpx.Response(200, json=_ok_body()))
    bad = {"bears": {"type": "choice", "instructions": "q", "criteria": {}}}
    with pytest.raises(HardLLMFailure, match="non-empty criteria"):
        await handler.decide("s", bad)
    with pytest.raises(HardLLMFailure, match="type"):
        await handler.decide("s", {"b": {"type": "score", "instructions": "q", "criteria": {"a": "b"}}})
    assert seen == []


async def test_the_chat_surface_refuses_loudly():
    handler = await _handler(lambda r: httpx.Response(200, json=_ok_body()))
    with pytest.raises(HardLLMFailure, match="DECISION component"):
        await handler.chat_complete([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# 429 / 5xx — transient, Retry-After honoured through the shared helper
# ---------------------------------------------------------------------------


async def test_a_429_with_retry_after_is_retried_after_the_header(_no_real_sleep):
    calls: list[int] = []

    def responder(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={"error": {"message": "slow down", "type": "rate_limit"}},
            )
        return httpx.Response(200, json=_ok_body("no", 0.12))

    handler = await _handler(responder)
    answers = await handler.decide("s", _QUESTIONS)
    assert answers["bears"]["choice"] == "no"
    assert len(calls) == 2
    assert _no_real_sleep == [7.0]


async def test_an_exhausted_429_raises_transient_carrying_retry_after(_no_real_sleep):
    def responder(request):
        return httpx.Response(
            429,
            headers={"Retry-After": "9999"},  # capped by the shared helper
            json={"error": {"message": "slow down", "type": "rate_limit"}},
        )

    handler = await _handler(responder)
    with _account():
        with pytest.raises(TransientLLMFailure) as ei:
            await handler.decide("s", _QUESTIONS, max_retries=1, leg="bearing_shadow")
        (rec,) = run_accounting.current_llm_calls()
    assert ei.value.status == 429
    assert ei.value.retry_after == 120.0
    assert "rate_limit: slow down" in str(ei.value)
    assert _no_real_sleep == [120.0]  # one retry, waited the capped header
    assert rec["status"] == "transient_fail" and rec["http_status"] == 429
    assert rec["leg"] == "bearing_shadow"


async def test_a_503_is_transient_and_retried_on_the_backoff(_no_real_sleep):
    statuses = [503, 502, 200]

    def responder(request):
        code = statuses.pop(0)
        if code == 200:
            return httpx.Response(200, json=_ok_body())
        return httpx.Response(code, json={"error": {"message": "busy", "type": "server"}})

    handler = await _handler(responder)
    answers = await handler.decide("s", _QUESTIONS)
    assert answers["bears"]["choice"] == "yes"
    assert _no_real_sleep == [1, 2]


# ---------------------------------------------------------------------------
# 4xx — hard, no retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 401, 422])
async def test_a_4xx_is_hard_and_never_retried(code, _no_real_sleep):
    calls: list[int] = []

    def responder(request):
        calls.append(1)
        return httpx.Response(
            code, json={"error": {"message": "criteria must be non-empty", "type": "invalid_request"}}
        )

    handler = await _handler(responder)
    with _account():
        with pytest.raises(HardLLMFailure) as ei:
            await handler.decide("s", _QUESTIONS)
        (rec,) = run_accounting.current_llm_calls()
    assert ei.value.status == code
    assert "invalid_request: criteria must be non-empty" in str(ei.value)
    assert calls == [1] and _no_real_sleep == []
    assert rec["status"] == "hard_fail" and rec["http_status"] == code
    assert "error" in rec and "criteria must be" not in json.dumps(rec)  # class, never the body


# ---------------------------------------------------------------------------
# max_concurrent — the per-component semaphore
# ---------------------------------------------------------------------------


async def test_max_concurrent_gates_in_flight_calls():
    # Each responder holds its slot on an Event, so "in flight" is real.
    state = {"in_flight": 0, "peak": 0}
    gate = asyncio.Event()

    async def responder(request):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await gate.wait()
        state["in_flight"] -= 1
        return httpx.Response(200, json=_ok_body())

    handler = await _handler(responder, max_concurrent=2)
    tasks = [asyncio.create_task(handler.decide("s", _QUESTIONS)) for _ in range(6)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert state["in_flight"] == 2, "only max_concurrent calls may be on the wire"
    gate.set()
    results = await asyncio.gather(*tasks)
    assert len(results) == 6 and state["peak"] == 2


async def test_no_max_concurrent_means_no_gate():
    state = {"in_flight": 0, "peak": 0}
    gate = asyncio.Event()

    async def responder(request):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await gate.wait()
        state["in_flight"] -= 1
        return httpx.Response(200, json=_ok_body())

    handler = await _handler(responder)
    tasks = [asyncio.create_task(handler.decide("s", _QUESTIONS)) for _ in range(5)]
    for _ in range(20):
        await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)
    assert state["peak"] == 5


# ---------------------------------------------------------------------------
# Routing, the descriptor, the family fence
# ---------------------------------------------------------------------------


def test_a_decide_component_id_routes_to_the_decision_client():
    from legba.runtime.analyst_deps_builder import infer_llm_subprovider

    assert infer_llm_subprovider(_COMPONENT_ID, endpoint="https://gateway.example.invalid/v1") == "decide"
    assert LLM_HANDLERS["decide"] is DecisionProviderHandler
    # The chat components on the same gateway keep their route.
    assert infer_llm_subprovider(
        "llm.verify.gateway_judge.openai_compat", endpoint="https://gateway.example.invalid/v1"
    ) == "vllm"


async def test_the_production_build_path_yields_a_configured_decision_client():
    from legba.runtime.analyst_deps_builder import build_llm_handler_from_stack_component
    from legba.runtime.registry_client import RegistryHTTPClient

    body = yaml.safe_load(_YAML.read_text())

    def registry(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/stack/{_COMPONENT_ID}")
        return httpx.Response(200, json={"id": _COMPONENT_ID, "version": "0" * 16, "body": body})

    inner = httpx.AsyncClient(transport=httpx.MockTransport(registry), base_url="http://registry.test")
    client = RegistryHTTPClient(base_url="http://registry.test", token=None, client=inner)

    async def secrets(secret_id: str) -> bytes:
        assert secret_id == "llm.gateway.api_key"
        return b"tok-test"

    handler = await build_llm_handler_from_stack_component(
        _COMPONENT_ID, registry_client=client, secrets_resolve=secrets,
    )
    assert isinstance(handler, DecisionProviderHandler)
    # The descriptor names its model and endpoint by variable; the build resolves them.
    assert handler._cfg.model_name.raw == GATEWAY_ENV["LEGBA_GATEWAY_DECIDE_MODEL"]  # noqa: SLF001
    assert handler._cfg.api_endpoint.raw == GATEWAY_ENV["LEGBA_GATEWAY_API_ENDPOINT"]  # noqa: SLF001
    assert handler._concurrency_limit() == 8  # noqa: SLF001
    assert handler._client is None  # noqa: SLF001 — nothing dialled at build
    await inner.aclose()


def test_the_descriptor_validates_and_is_registered():
    import ast

    from legba.data.schemas.stack import LLMProvider
    from legba.runtime.external_grader_route import GRADER_FAMILY_BY_COMPONENT

    body = yaml.safe_load(_YAML.read_text())
    model = LLMProvider.model_validate(body)
    assert model.id == _COMPONENT_ID
    assert ".decide." in model.id, "the id is what routes it to the /decide client"
    assert model.state.value == "draft", "registering it must activate nothing"
    assert model.schema_uri == "legba/stack/llm_provider/1.0.0"
    cfg = model.config
    # The endpoint and the served alias are deployment data: named by variable.
    assert cfg.api_endpoint.raw == "${env:LEGBA_GATEWAY_API_ENDPOINT}"
    assert cfg.api_key.raw == "llm.gateway.api_key"
    assert cfg.model_name.raw == "${env:LEGBA_GATEWAY_DECIDE_MODEL}"
    # No output limit: a decision call generates no tokens.
    assert cfg.max_tokens is None and cfg.max_output_tokens is None
    assert cfg.timeout_seconds.raw == 30
    assert cfg.tier.raw == "cheap"
    assert cfg.max_concurrent.raw == 8
    assert "family" not in body
    assert GRADER_FAMILY_BY_COMPONENT[_COMPONENT_ID] == "decision_model"

    # The registrar LISTS it (read off its source: importing the registrar
    # resolves an operator token, which a unit test has no business doing).
    tree = ast.parse((_REPO / "scripts" / "bringup_register_stack.py").read_text())
    listed: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_STACK_DESCRIPTOR_FILES" for t in node.targets
        ):
            listed = [e.value for e in node.value.elts]
    assert _YAML.name in listed


@pytest.fixture(autouse=True)
def _gateway_env(monkeypatch):
    """The gateway descriptors name their endpoint and models as ${env:NAME}."""
    from tests._gateway_env import set_gateway_env

    set_gateway_env(monkeypatch)
