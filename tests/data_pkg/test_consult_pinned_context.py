# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``pinned_context`` — the field the consult front door used to swallow.

The workstation's Consult panel has sent ``pinned_context`` on every POST for
the whole of its life. ``ConsultRequest`` declared no such field and set no
``model_config``, so pydantic v2's ``extra='ignore'`` dropped it silently:
only the client-side ``[Pinned …]`` text prefix ever reached the planner, and
Deep Consult — which sends no prefix either — got nothing at all.

What is asserted here, end to end:

  * the chat front door ACCEPTS the pins and forwards them on the actor's
    first input row (``label`` accepted as ``title``, the SPA's key);
  * a request WITHOUT pins produces the byte-identical invoke body it produced
    before the field existed — no ``pinned_context`` key at all (the golden);
  * oversize by count, by total chars, or an unresolvable kind is a 422 the
    client can see, not a silent truncation it cannot;
  * the same holds for the DEEP front door, and the deep kind normalizes the
    rows onto the durable workflow input;
  * the analyst renders them as a leading ``PINNED CONTEXT`` block, and
    renders NOTHING when there are none (the prompt golden).
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import legba.data.registry.consult_api as consult_api
import legba.data.registry.deep_consult_api as deep_api
from legba.data.analysts.consult_on_demand import _render_user_prompt
from legba.data.pinned_context import (
    MAX_PINNED_RECORDS,
    MAX_PINNED_TOTAL_CHARS,
    MAX_PIN_TEXT_CHARS,
    PINNED_CONTEXT_HEADER,
    normalize_pinned_context,
    pinned_context_chars,
    render_pinned_context_block,
)
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.consult_api import build_consult_router
from legba.data.registry.deep_consult_api import build_deep_consult_router


API_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"

# What the SPA actually puts on the wire today (`Consult.tsx` :332) — kind, id
# and `label`. The hydrated `text` is what the mobile lane will add.
SPA_PINS = [
    {"kind": "finding", "id": "11111111-1111-1111-1111-111111111111",
     "label": "Border buildup"},
    {"kind": "target", "id": "brazil", "label": "Brazil"},
]


# ---------------------------------------------------------------------------
# Stubs (mirrors of test_consult_api_chat.py's — kept local so this file reads
# on its own)
# ---------------------------------------------------------------------------


class _NoReadBackPg:
    def acquire(self):
        raise AssertionError("chat path must not read back from the DB")


class _DescRow:
    version = "v" + "a" * 16


class _DescriptorRegistry:
    def __init__(self, pg: Any) -> None:
        self.pg = pg

    async def get(self, descriptor_id, *, family, version=None):
        return _DescRow()


def _stub_dapr(actor_envelope: dict[str, Any], captured: dict[str, Any]):
    class _Resp:
        status_code = 200

        def json(self):
            return actor_envelope

        @property
        def text(self):
            return json.dumps(actor_envelope)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put(self, url, json=None, headers=None):
            captured["url"] = url
            captured["body"] = json
            return _Resp()

    return _Client


def _deps(pg: Any) -> RegistryAPIDeps:
    os.environ.pop(API_TOKEN_ENV, None)  # dev mode — token optional
    return RegistryAPIDeps(
        descriptor_registry=_DescriptorRegistry(pg),  # type: ignore[arg-type]
        stack_registry=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=None,
    )


def _chat_app(pg: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(build_consult_router(_deps(pg)), prefix="/api/v1")
    return app


def _deep_app(pg: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(build_deep_consult_router(_deps(pg)), prefix="/api/v1")
    return app


_CHAT_ENVELOPE = {
    "outcome": "success",
    "mode": "chat",
    "consult_response": {"question": "q", "answer": "a"},
    "derived_from": [],
}
_DEEP_ENVELOPE = {
    "outcome": "success",
    "mode": "deep_consult",
    "task_id": "deep_consult.global.abcd1234",
    "status": "running",
    "run_id": "abcd1234-0000-0000-0000-000000000000",
}


class _StatusPg:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def acquire(self):
        row = self._row

        class _Ctx:
            async def __aenter__(self_inner):
                class _Conn:
                    async def fetchrow(self, *a, **k):
                        return row

                    async def fetch(self, *a, **k):
                        return []

                return _Conn()

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


# ---------------------------------------------------------------------------
# Chat front door
# ---------------------------------------------------------------------------


def test_chat_pins_are_accepted_and_forwarded(monkeypatch):
    """The bug in one assertion: the pins reach the actor's input row.

    Before the fix this key was absent no matter what the client sent.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(_CHAT_ENVELOPE, captured),
    )
    client = TestClient(_chat_app(_NoReadBackPg()))
    r = client.post(
        "/api/v1/consult",
        json={"question": "What touches Brazil?", "pinned_context": SPA_PINS},
    )
    assert r.status_code == 200, r.text

    sent = captured["body"]["inputs"][0]
    assert sent["pinned_context"] == [
        {"kind": "finding", "id": "11111111-1111-1111-1111-111111111111",
         "title": "Border buildup", "text": None},
        {"kind": "target", "id": "brazil", "title": "Brazil", "text": None},
    ]


def test_chat_pin_title_key_also_accepted(monkeypatch):
    """``title`` is the canonical key; ``label`` is the SPA synonym. Both land
    on ``title`` so the analyst has exactly one field to render."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(_CHAT_ENVELOPE, captured),
    )
    r = TestClient(_chat_app(_NoReadBackPg())).post(
        "/api/v1/consult",
        json={
            "question": "q",
            "pinned_context": [
                {"kind": "report", "id": "r1", "title": "Morning Read",
                 "text": "the body"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert captured["body"]["inputs"][0]["pinned_context"] == [
        {"kind": "report", "id": "r1", "title": "Morning Read",
         "text": "the body"},
    ]


def test_chat_without_pins_is_byte_identical_to_the_old_shape(monkeypatch):
    """GOLDEN — a pre-``pinned_context`` client must produce the invoke body it
    produced before the field existed: no ``pinned_context`` key at all, not an
    empty list. An old browser tab is a client no server change can update."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(_CHAT_ENVELOPE, captured),
    )
    r = TestClient(_chat_app(_NoReadBackPg())).post(
        "/api/v1/consult",
        json={
            "question": "q",
            "mode": "chat",
            "request_id": "fixed-123",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text
    assert captured["body"] == {
        "trigger_kind": "method",
        "inputs": [{
            "question": "q",
            "scope_predicate": None,
            "max_tool_rounds": 10,
            "mode": "chat",
            "request_id": "fixed-123",
            "messages": [{"role": "user", "content": "hi"}],
        }],
    }


def test_chat_empty_pin_list_also_omits_the_key(monkeypatch):
    """``pinned_context: []`` is the same statement as omitting it."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(_CHAT_ENVELOPE, captured),
    )
    r = TestClient(_chat_app(_NoReadBackPg())).post(
        "/api/v1/consult", json={"question": "q", "pinned_context": []},
    )
    assert r.status_code == 200, r.text
    assert "pinned_context" not in captured["body"]["inputs"][0]


def test_chat_too_many_pins_is_422(monkeypatch):
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(_CHAT_ENVELOPE, {}),
    )
    over = [
        {"kind": "finding", "id": f"id-{i}"}
        for i in range(MAX_PINNED_RECORDS + 1)
    ]
    r = TestClient(_chat_app(_NoReadBackPg())).post(
        "/api/v1/consult", json={"question": "q", "pinned_context": over},
    )
    assert r.status_code == 422, r.text


def test_chat_oversize_pin_body_is_422(monkeypatch):
    """One entry past the per-entry text cap."""
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(_CHAT_ENVELOPE, {}),
    )
    r = TestClient(_chat_app(_NoReadBackPg())).post(
        "/api/v1/consult",
        json={
            "question": "q",
            "pinned_context": [
                {"kind": "finding", "id": "f1", "text": "x" * (MAX_PIN_TEXT_CHARS + 1)},
            ],
        },
    )
    assert r.status_code == 422, r.text


def test_chat_pins_over_the_total_char_cap_are_422(monkeypatch):
    """Each entry legal, the SUM not. Twenty maxed bodies would be 160k chars —
    far past the consult input budget — so the sum is gated too."""
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(_CHAT_ENVELOPE, {}),
    )
    pins = [
        {"kind": "finding", "id": f"f{i}", "text": "x" * MAX_PIN_TEXT_CHARS}
        for i in range(5)
    ]
    assert all(len(p["text"]) <= MAX_PIN_TEXT_CHARS for p in pins)
    assert pinned_context_chars(pins) > MAX_PINNED_TOTAL_CHARS
    r = TestClient(_chat_app(_NoReadBackPg())).post(
        "/api/v1/consult", json={"question": "q", "pinned_context": pins},
    )
    assert r.status_code == 422, r.text


def test_chat_unknown_pin_kind_is_422(monkeypatch):
    """A kind nothing can resolve is a client bug — fail it where the client
    can see it rather than pinning a record the model can never look up."""
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(_CHAT_ENVELOPE, {}),
    )
    r = TestClient(_chat_app(_NoReadBackPg())).post(
        "/api/v1/consult",
        json={"question": "q", "pinned_context": [{"kind": "wormhole", "id": "x"}]},
    )
    assert r.status_code == 422, r.text


def test_chat_logs_a_pin_counter(monkeypatch, caplog):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        consult_api.httpx, "AsyncClient", _stub_dapr(_CHAT_ENVELOPE, captured),
    )
    with caplog.at_level("INFO", logger=consult_api.logger.name):
        TestClient(_chat_app(_NoReadBackPg())).post(
            "/api/v1/consult",
            json={"question": "q", "pinned_context": SPA_PINS},
        )
    invoke = [r for r in caplog.records if "consult.invoke" in r.getMessage()]
    assert invoke, "no consult.invoke log line"
    assert "pinned_context=2" in invoke[0].getMessage()


# ---------------------------------------------------------------------------
# Deep front door — the path that carried no pins at all
# ---------------------------------------------------------------------------


def test_deep_pins_are_accepted_and_forwarded(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        deep_api.httpx, "AsyncClient", _stub_dapr(_DEEP_ENVELOPE, captured),
    )
    r = TestClient(_deep_app(_StatusPg(None))).post(
        "/api/v1/deep_consult",
        json={"question": "q", "pinned_context": SPA_PINS},
    )
    assert r.status_code == 202, r.text
    sent = captured["body"]["inputs"][0]
    assert [p["id"] for p in sent["pinned_context"]] == [
        "11111111-1111-1111-1111-111111111111", "brazil",
    ]
    assert sent["pinned_context"][0]["title"] == "Border buildup"


def test_deep_without_pins_is_byte_identical_to_the_old_shape(monkeypatch):
    """GOLDEN — the deep submit's input row, unchanged for a pre-pin client."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        deep_api.httpx, "AsyncClient", _stub_dapr(_DEEP_ENVELOPE, captured),
    )
    r = TestClient(_deep_app(_StatusPg(None))).post(
        "/api/v1/deep_consult", json={"question": "q"},
    )
    assert r.status_code == 202, r.text
    assert captured["body"]["inputs"] == [
        {"question": "q", "scope_predicate": None},
    ]


def test_deep_too_many_pins_is_422(monkeypatch):
    monkeypatch.setattr(
        deep_api.httpx, "AsyncClient", _stub_dapr(_DEEP_ENVELOPE, {}),
    )
    over = [
        {"kind": "finding", "id": f"id-{i}"}
        for i in range(MAX_PINNED_RECORDS + 1)
    ]
    r = TestClient(_deep_app(_StatusPg(None))).post(
        "/api/v1/deep_consult", json={"question": "q", "pinned_context": over},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_deep_kind_normalizes_pins_onto_the_workflow_input():
    """The deep kind clamps the rows ONCE, at submit, because the workflow
    input is durable: what gets persisted is the normalized set, not whatever
    arrived on the queue."""
    from legba.data.analysts.deep_consult import DeepConsultKindDeps, run_method

    captured: dict[str, Any] = {}

    class _FakeClient:
        async def start_deep_consult_workflow(self, wf_input, *, workflow_id):
            captured["wf_input"] = wf_input
            return "task-1"

    deps = DeepConsultKindDeps(
        workflow_client=_FakeClient(), llm_component_id="llm.anthropic.opus_4_7",
    )
    await run_method(
        [{"question": "q", "pinned_context": SPA_PINS}],
        {"analyst_id": "deep_consult", "run_id": str(uuid4())},
        deps,
    )
    assert captured["wf_input"].pinned_context == [
        {"kind": "finding", "id": "11111111-1111-1111-1111-111111111111",
         "title": "Border buildup", "text": ""},
        {"kind": "target", "id": "brazil", "title": "Brazil", "text": ""},
    ]

    # And a submit with no pins leaves the durable input's default alone.
    captured.clear()
    await run_method(
        [{"question": "q"}],
        {"analyst_id": "deep_consult", "run_id": str(uuid4())},
        deps,
    )
    assert captured["wf_input"].pinned_context == []


# ---------------------------------------------------------------------------
# Rendering — what the planner actually reads
# ---------------------------------------------------------------------------


def test_block_leads_with_the_header_and_names_every_pin():
    block = render_pinned_context_block([
        {"kind": "finding", "id": "F1", "label": "Border buildup",
         "text": "two brigades moved"},
        {"kind": "target", "id": "brazil", "title": "Brazil"},
    ])
    assert block.startswith(PINNED_CONTEXT_HEADER)
    assert "2 record(s)" in block
    assert '[1] finding "Border buildup" (id=F1)' in block
    assert "    two brigades moved" in block
    assert '[2] target "Brazil" (id=brazil)' in block


def test_no_pins_renders_nothing():
    """The empty render is what keeps the no-pins prompt byte-identical."""
    assert render_pinned_context_block([]) == ""
    assert render_pinned_context_block(None) == ""
    assert render_pinned_context_block("not a list") == ""
    assert render_pinned_context_block([{"kind": "finding"}]) == ""  # no id


def test_user_prompt_without_pins_is_unchanged():
    """GOLDEN — the prompt the consult loop built before this field existed."""
    assert _render_user_prompt("What touches Brazil?", None) == (
        "Operator question:\nWhat touches Brazil?"
    )
    assert _render_user_prompt("q", "scope_geo('BR')") == (
        "Operator question:\nq\n\n"
        "Scope predicate (apply to substrate queries): scope_geo('BR')"
    )


def test_user_prompt_leads_with_the_pinned_block():
    """The pins come FIRST: they qualify the question that follows."""
    block = render_pinned_context_block(SPA_PINS)
    prompt = _render_user_prompt("q", None, block)
    assert prompt.startswith(PINNED_CONTEXT_HEADER)
    assert prompt.endswith("Operator question:\nq")


def test_normalize_clamps_count_and_drops_junk():
    raw: list[Any] = [
        {"kind": "finding", "id": "ok"},
        "not a mapping",
        {"kind": "finding"},          # no id
        {"kind": "finding", "id": ""},  # blank id
        {"id": "kindless"},           # kind defaults
    ]
    rows = normalize_pinned_context(raw)
    assert [r["id"] for r in rows] == ["ok", "kindless"]
    assert rows[1]["kind"] == "record"

    over = [{"kind": "finding", "id": f"i{n}"} for n in range(MAX_PINNED_RECORDS + 5)]
    assert len(normalize_pinned_context(over)) == MAX_PINNED_RECORDS


def test_normalize_clamps_the_shared_char_budget():
    """The analyst CLAMPS where the registry rejects — the actor input is JSON
    off a queue, and a run must not die on a soft field."""
    rows = normalize_pinned_context([
        {"kind": "finding", "id": f"f{i}", "text": "x" * MAX_PIN_TEXT_CHARS}
        for i in range(10)
    ])
    assert pinned_context_chars(rows) <= MAX_PINNED_TOTAL_CHARS
    assert rows, "clamping must not empty the set"
