# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A body that skips the BLUF its prompt asked for is counted, never rejected.

``has_bluf_line`` is a warning predicate beside ``is_unusable_output``: the
run still publishes, and the run trace carries one ``bluf_missing_warning``
step so quiet-window bodies without a bottom line can be counted. Pure - no
database, no model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

import pytest

from legba.data.analysts.inline_target import InlineTargetDeps, run_method
from legba.data.analysts.output_contract import bluf_missing_step, has_bluf_line
from legba.data.run_accounting import bind_run_accounting, reset_run_accounting


@pytest.mark.parametrize(
    "body",
    [
        "*As of 3 May.*\n\n**BLUF:** No new move appears in this desk's collection through 3 May.",
        "**BLUF**: A supplier halted deliveries [1].",
        "  ** BLUF: ** A regulator opened a review [2].",
    ],
)
def test_a_bluf_line_is_recognised(body: str) -> None:
    assert has_bluf_line(body)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "## What changed\n\nNothing in this window moves the read.",
        "**BLUF:**\n\n## What changed",
        "The text mentions **BLUF:** in the middle of a line.",
    ],
)
def test_no_bluf_line(body: str) -> None:
    assert not has_bluf_line(body)


def test_the_step_fires_only_when_the_prompt_asked_for_a_bluf() -> None:
    asked = "Write '**BLUF:**' followed by ONE sentence."
    assert bluf_missing_step("## What changed\n\nNothing.", asked) == {
        "phase": "reflect", "kind": "bluf_missing_warning",
    }
    assert bluf_missing_step("**BLUF:** No move [1].", asked) is None
    assert bluf_missing_step("## Key findings\n\nA thing.", "No bottom line here.") is None
    assert bluf_missing_step("## Key findings", None) is None


@dataclass
class _Usage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _Canned:
    subprovider = "openai"

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def chat_complete(self, messages: list[Mapping[str, Any]], **kwargs: Any) -> Any:
        return _Response(content=self._reply, usage=_Usage())


def _reply(body: str) -> str:
    return json.dumps({
        "title": "Subject: no new move in this desk's collection since 2 May",
        "body": body,
        "confidence": 0.2,
        "evidence": ["1"],
        "tags": ["topic:example", "severity:low", "severity_delta:steady"],
    })


async def _steps(body: str) -> list[dict[str, Any]]:
    token = bind_run_accounting()
    try:
        result = await run_method(
            [{
                "id": uuid4(), "title": "A routine notice", "produced_at": "2026-05-03T08:00:00+00:00",
                "source_url": "https://example.com/a", "data": {"summary": "A routine notice."},
            }],
            {"target_id": "example_target"},
            InlineTargetDeps(
                llm=_Canned(_reply(body)),
                system_prompt="Write '**BLUF:**' followed by ONE sentence. Reply in JSON.",
            ),
        )
    finally:
        reset_run_accounting(token)
    return list(result.intermediate_steps)


@pytest.mark.asyncio
async def test_a_body_without_a_bluf_publishes_and_is_counted() -> None:
    steps = await _steps("## What changed\n\nNothing in this window moves the read [1].")
    assert [s for s in steps if s.get("kind") == "bluf_missing_warning"] == [
        {"phase": "reflect", "kind": "bluf_missing_warning"}
    ]


@pytest.mark.asyncio
async def test_a_body_with_a_bluf_carries_no_warning() -> None:
    steps = await _steps("**BLUF:** No new move appears in this desk's collection through 3 May.")
    assert not [s for s in steps if s.get("kind") == "bluf_missing_warning"]
