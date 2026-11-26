# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A finding returned with a title and no readable body gets ONE repair call.

The live shape: on a quiet window the scheduled desk wrote its JSON object
(title, confidence, tags, indicators) and put the prose body beside it, before
or after the object, instead of in its ``body`` field. The parser found no
body and the run was lost with no retry. The handler now sends the model its
own reply and one line saying what was missing, re-parses the answer, and
fails as before only if that also fails. Pure - no database, no model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

import pytest

from legba.data.analysts.inline_target import (
    EMPTY_BODY_REPAIR_LINE,
    InlineTargetDeps,
    _coerce_finding,
    run_method,
)
from legba.data.analysts.output_contract import EmptyBodyContractError, OutputContractError
from legba.data.run_accounting import bind_run_accounting, current_steps, reset_run_accounting


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _Scripted:
    subprovider = "openai"

    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages: list[Mapping[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"messages": [dict(m) for m in messages], **kwargs})
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return _Response(content=reply, usage=_Usage())


_ENVELOPE = {
    "title": "Brazil posture static: no force, basing or procurement move in this slice",
    "confidence": 0.3,
    "evidence": ["1"],
    "tags": ["topic:military_posture", "severity:moderate", "severity_delta:steady"],
}
_PROSE = (
    "*As of 24 November 2026.*\n\n**BLUF:** No capability, deployment or exercise "
    "signal appears in this desk's collection through 24 November [1].\n\n"
    "## What changed\n\n- Nothing material changed against the prior read [1]."
)
#: The JSON object first, the body written after it (live 2026-11-24, Brazil).
BODY_AFTER = json.dumps(_ENVELOPE) + "\n\n" + _PROSE
#: The body first, the object in a fence after it (live 2026-11-22, Mali).
BODY_BEFORE = _PROSE + "\n\n```json\n" + json.dumps(_ENVELOPE, indent=2) + "\n```"
#: The object with one stray line beside it: too little to be a body, so the repair call runs.
STRAY_LINE = json.dumps(_ENVELOPE) + "\n\nNothing to add."
GOOD = json.dumps({**_ENVELOPE, "body": _PROSE})


def _inputs() -> list[dict[str, Any]]:
    return [{
        "id": uuid4(), "title": "Brazil defence budget line", "produced_at": "2026-11-24T08:00:00+00:00",
        "source_url": "https://example.com/a", "data": {"summary": "A fiscal bill passed."},
    }]


async def _run(llm: _Scripted) -> tuple[Any, list[dict[str, Any]], BaseException | None]:
    token = bind_run_accounting()
    err: BaseException | None = None
    result = None
    try:
        try:
            result = await run_method(_inputs(), {"target_id": "country_br"}, InlineTargetDeps(llm=llm))
        except OutputContractError as exc:
            err = exc
        steps = current_steps()
    finally:
        reset_run_accounting(token)
    if result is not None:
        steps = list(result.intermediate_steps)
    return result, steps, err


def _repair_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in steps if s.get("kind") == "empty_body_repair"]


def test_the_live_shape_with_a_stray_line_raises_the_repairable_error() -> None:
    first = STRAY_LINE
    with pytest.raises(EmptyBodyContractError) as info:
        _coerce_finding(first, fallback_title="Assessment for country_br")
    assert info.value.title.startswith("Brazil posture static")


def test_an_untitled_tool_call_is_not_repairable() -> None:
    raw = '{"tool": "search_corpus", "args": {"query": "q"}}'
    with pytest.raises(OutputContractError) as info:
        _coerce_finding(raw, fallback_title="Assessment for x")
    assert not isinstance(info.value, EmptyBodyContractError)


@pytest.mark.asyncio
async def test_one_repair_call_publishes_the_finding() -> None:
    first = STRAY_LINE
    llm = _Scripted([first, GOOD])
    result, steps, err = await _run(llm)
    assert err is None and result is not None
    assert result.finding.title.startswith("Brazil posture static")
    assert "**BLUF:** No capability" in result.finding.body
    assert len(llm.calls) == 2
    sent = llm.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"] == first            # the model's own reply
    assert sent[2]["content"] == EMPTY_BODY_REPAIR_LINE
    assert llm.calls[1]["system"] == llm.calls[0]["system"]
    (step,) = _repair_steps(steps)
    assert step["outcome"] == "repaired"
    # Both calls' tokens are in the run's usage.
    assert result.usage["prompt_tokens"] == 200 and result.usage["completion_tokens"] == 100


@pytest.mark.asyncio
async def test_a_second_empty_body_fails_as_before() -> None:
    llm = _Scripted([STRAY_LINE, STRAY_LINE])
    _, steps, err = await _run(llm)
    assert isinstance(err, OutputContractError) and "no readable body" in str(err)
    assert len(llm.calls) == 2
    (step,) = _repair_steps(steps)
    assert step["outcome"] == "still_failing"
    assert [s for s in steps if s.get("kind") == "final_answer_unreadable"]


@pytest.mark.asyncio
async def test_a_failed_repair_call_fails_on_the_original_error() -> None:
    llm = _Scripted([STRAY_LINE, RuntimeError("transport down")])
    _, steps, err = await _run(llm)
    assert isinstance(err, EmptyBodyContractError)
    (step,) = _repair_steps(steps)
    assert step["outcome"] == "llm_error"


@pytest.mark.asyncio
async def test_a_good_first_reply_makes_one_call() -> None:
    llm = _Scripted([GOOD])
    result, steps, err = await _run(llm)
    assert err is None and len(llm.calls) == 1 and not _repair_steps(steps)


def _kinds(steps: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [s for s in steps if s.get("kind") == kind]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [BODY_AFTER, BODY_BEFORE], ids=["body_after", "body_before"])
async def test_prose_outside_the_object_is_the_body_with_no_second_call(first: str) -> None:
    llm = _Scripted([first])
    result, steps, err = await _run(llm)
    assert err is None and result is not None
    assert len(llm.calls) == 1 and not _repair_steps(steps)
    assert result.finding.body.startswith("*As of 24 November 2026.*")
    assert "```" not in result.finding.body and '"title"' not in result.finding.body
    (step,) = _kinds(steps, "body_outside_object")
    assert step["chars"] == len(result.finding.body)
    assert "body_outside_object_chars" not in result.finding.data


@pytest.mark.asyncio
async def test_a_thinking_block_alone_is_not_a_body() -> None:
    thought = "<think>" + _PROSE + "</think>"
    llm = _Scripted([json.dumps(_ENVELOPE) + "\n" + thought, GOOD])
    result, steps, err = await _run(llm)
    assert err is None and len(llm.calls) == 2
    assert not _kinds(steps, "body_outside_object") and _repair_steps(steps)


@pytest.mark.asyncio
async def test_one_stray_line_still_runs_the_repair_call() -> None:
    llm = _Scripted([STRAY_LINE, GOOD])
    _, steps, err = await _run(llm)
    assert err is None and len(llm.calls) == 2
    assert not _kinds(steps, "body_outside_object") and _repair_steps(steps)


@pytest.mark.asyncio
async def test_an_object_with_its_own_body_is_untouched() -> None:
    llm = _Scripted([GOOD + "\n\n" + _PROSE.replace("BLUF", "OTHER")])
    result, steps, err = await _run(llm)
    assert err is None and len(llm.calls) == 1
    assert result.finding.body == _PROSE
    assert not _kinds(steps, "body_outside_object")
