# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F68b — deep consult's tool context goes through the reader renderer.

``runtime/dapr_workflow/deep_consult.py`` used to render acquired tool results
into the analyze prompt with a raw ``json.dumps`` (and never forwarded the
asker's display class to the re-entrant synthesis loop). Now both go through
``consult_tool_rendering.tool_context_renderer``: a SENTINEL planted in
``archived_text`` / ``source_text`` of a tool result never reaches ANY message
the deep-consult model is handed when the run's class is a reader; the
operator control run DOES carry it, so a green reader run is a projection and
not an empty pipe.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from legba.runtime.dapr_workflow.deep_consult import (
    DeepConsultStageDeps,
    DeepConsultWorkflowInput,
    _run_acquire,
    _run_analyze,
    _run_plan,
)

SENTINEL = "SENTINEL-F68B-DEEP-CONSULT-ARCHIVED-PAGE-9917"
TITLE = "Ministry closes the corridor"
PAGE = "Opening paragraph of the page. " + SENTINEL + " Closing paragraph."


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _LLM:
    subprovider = "vllm-test"
    model_name = "test-model"

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs) -> Any:
        self.calls.append({"messages": messages, "system": kwargs.get("system")})
        return _Reply(self.script.pop(0))


class _Substrate:
    """Every search returns one publisher document carrying the sentinel."""

    async def search_signals(self, *, query: str, limit: int = 20,
                             scope_predicate: str | None = None) -> dict[str, Any]:
        sid = str(uuid4())
        return {
            "rows": [{
                "id": sid, "title": TITLE,
                "payload": {"archived_text": PAGE, "raw_body": f"<p>{PAGE}</p>",
                            "summary": "Short summary."},
                "citations": [{"marker": "[1]", "snippet": "Short summary.",
                               "source_text": PAGE}],
            }],
            "refs": [sid],
        }


def _plan() -> str:
    return json.dumps({
        "sub_queries": ["corridor"],
        "tool_plan": [{"tool": "search_signals", "args": {"query": "corridor"}}],
    })


def _round() -> str:
    # A drill-down tool round INSIDE the synthesis loop.
    return json.dumps({"tools": [
        {"tool": "search_signals", "args": {"query": "corridor again"}}]})


def _final() -> str:
    return json.dumps({"final": True, "answer": "Closed.", "uncertainty": 0.3,
                       "cited_refs": [], "unanswered_aspects": []})


async def _run(display_class: str) -> str:
    llm = _LLM([_plan(), _round(), _final(), json.dumps({})])
    deps = DeepConsultStageDeps(llm=llm, substrate=_Substrate())
    kw: dict[str, Any] = {}
    if display_class:
        kw["display_class"] = display_class
    wf = DeepConsultWorkflowInput(
        question="Is the corridor open?", run_id=str(uuid4()),
        analyst_id="deep_consult", analyst_version="v0", **kw,
    )
    plan = await _run_plan(wf, deps)
    acquired = await _run_acquire(plan, deps)
    # The raw acquire evidence is untouched (refs / gates still feed on it);
    # only the CONTEXT copy is projected.
    assert SENTINEL in json.dumps(acquired["evidence"], default=str)
    await _run_analyze(acquired, deps)
    # The analyze stage made the brief call AND the tool-round call.
    assert len(llm.calls) >= 3, "the model was never handed the tool results"
    return json.dumps(llm.calls[1:], default=str)  # everything after the plan


@pytest.mark.asyncio
async def test_sentinel_never_reaches_the_deep_consult_context_for_a_reader() -> None:
    seen = await _run("reader")
    assert SENTINEL not in seen
    assert "Short summary." in seen  # the cited span survives: a projection


@pytest.mark.asyncio
async def test_a_non_operator_stamp_fails_closed_to_the_reader() -> None:
    assert SENTINEL not in await _run("anything-else")


@pytest.mark.asyncio
async def test_operator_and_unstamped_runs_keep_the_full_context() -> None:
    assert SENTINEL in await _run("operator")
    assert SENTINEL in await _run("")


@pytest.mark.asyncio
async def test_submit_stamps_the_display_class_on_the_durable_input() -> None:
    from legba.data.analysts.deep_consult import DeepConsultKindDeps, run_method

    captured: dict[str, Any] = {}

    class _Client:
        async def start_deep_consult_workflow(self, wf_input, *, workflow_id):
            captured["wf"] = wf_input
            return "task-1"

    deps = DeepConsultKindDeps(
        workflow_client=_Client(), llm_component_id="llm.primary.openai_compat",
    )
    opts = {"analyst_id": "deep_consult", "run_id": str(uuid4())}
    await run_method([{"question": "q", "display_class": "reader"}], opts, deps)
    assert captured["wf"].display_class == "reader"
    await run_method([{"question": "q"}], opts, deps)
    assert captured["wf"].display_class == ""
