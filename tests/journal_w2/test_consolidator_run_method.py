# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Journal Wave 2 — the consolidator ``run_method`` emits entry_kind='consolidation'
(plan §4.7 / §12). No DB / NATS / Dapr — the LLM + binding are stubs.

The tier is the descriptor: ONE run_method, the entry_kind selected purely from
the running ``analyst_id`` (``journal_consolidator`` → consolidation; any other →
entry). This proves the discriminator at the run_method OUTPUT level (the
write-path supersession that the consolidation kind triggers is verified on the
disposable DB in test_consolidation_supersession.py).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping
from uuid import uuid4

import pytest

from legba.data.analysts.inline_target import InlineTargetDeps
from legba.data.analysts.journal_assessor import CONSOLIDATOR_ANALYST_ID, run_method


class _Usage:
    prompt_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0

    def get(self, k: str, default: int = 0) -> int:  # dict-ish access the folder uses
        return getattr(self, k, default)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _ScriptedLLM:
    subprovider = "anthropic"

    def __init__(self, scripted: list[str]) -> None:
        self._scripted = list(scripted)

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        content = self._scripted.pop(0) if self._scripted else '{"done": true}'
        return _Response(content)


def _options(analyst_id: str) -> dict[str, Any]:
    # No binding → the honesty post-step conservatively flags both legs; that's
    # fine here, we assert the TIER, not the honesty path.
    return {"analyst_id": analyst_id}


@pytest.mark.asyncio
async def test_consolidator_run_method_emits_consolidation():
    ref = uuid4()
    scripted = [
        f"Field notes: the quiet held [[ref:{ref}]].",                 # field-notes seam
        f"# Inner landscape\n\nThe quiet held [[ref:{ref}]].\n\n"      # narrate
        "I keep carrying the worry from last week forward.",
    ]
    deps = InlineTargetDeps(llm=_ScriptedLLM(scripted), system_prompt="CONSOLIDATOR", max_rounds=1)
    result = await run_method([{"title": "seed"}], _options(CONSOLIDATOR_ANALYST_ID), deps)
    payload = result.finding
    # THE tier assertion: the consolidator id → entry_kind 'consolidation'.
    assert payload.entry_kind == "consolidation"
    # Still off the chain, still a journal payload, citations still parsed.
    assert result.derived_from == []
    assert ref in payload.cited_substrate_refs
    # A coerce_journal step recorded the tier.
    coerce = [s for s in result.intermediate_steps if s.get("kind") == "coerce_journal"]
    assert coerce and coerce[0]["entry_kind"] == "consolidation"
    # The tier was recorded at wake.
    wake_tier = [s for s in result.intermediate_steps if s.get("kind") == "tier"]
    assert wake_tier and wake_tier[0]["entry_kind"] == "consolidation"


@pytest.mark.asyncio
async def test_entry_tier_still_emits_entry_same_run_method():
    """The SAME run_method, run as the entry-tier id, still appends an 'entry' —
    proving the discriminator is the id, not a hardcoded consolidation."""
    scripted = ["field notes", "An ordinary entry."]
    deps = InlineTargetDeps(llm=_ScriptedLLM(scripted), system_prompt="JOURNAL", max_rounds=1)
    result = await run_method([{"title": "seed"}], _options("journal_assessor"), deps)
    assert result.finding.entry_kind == "entry"


@pytest.mark.asyncio
async def test_empty_consolidation_body_falls_back_in_tier_voice():
    """A degenerate (empty) narrate still produces a valid consolidation payload
    with the tier-correct fallback body + title."""
    scripted = ["field notes", "   "]  # blank narrate
    deps = InlineTargetDeps(llm=_ScriptedLLM(scripted), system_prompt="C", max_rounds=1)
    result = await run_method([{"title": "s"}], _options(CONSOLIDATOR_ANALYST_ID), deps)
    assert result.finding.entry_kind == "consolidation"
    assert result.finding.body == "(empty consolidation)"
    assert result.finding.title == "Journal consolidation"


# ---------------------------------------------------------------------------
# Consolidation prose-shape guard — live defect 2026-07-31 02:07Z. A
# gather-timeout left NARRATE emitting a raw tool-call envelope
# (``{"tool": "get_source_health", "call": {...}}``) that task #236's
# ``_guard_against_tool_call_leak`` does NOT catch (its allowlist has no
# ``"call"`` key, and the envelope is well over its 120-char short-content
# floor) — it sailed through and was persisted verbatim. These tests exercise
# the SECOND, tier-scoped backstop added at the persist boundary.
# ---------------------------------------------------------------------------

_LEAKED_TOOL_CALL_JSON = json.dumps({
    "tool": "get_source_health",
    "call": {
        "metric": "feed_health",
        "window": "7d",
        "detail": "full",
        "reason": "checking collection posture before reflecting on this cycle",
    },
})


@pytest.mark.asyncio
async def test_tool_call_json_body_rejected_and_retried_recovers():
    """A leaked tool-call envelope on the first narrate pass triggers ONE
    retry; a clean retry recovers and publishes normally (not trace-only)."""
    scripted = [
        "field notes",                 # field-notes seam
        _LEAKED_TOOL_CALL_JSON,         # narrate — the leaked shape
        "# Inner landscape\n\nThe week held steady; I keep carrying the "
        "worry from last cycle forward, watching the tower settle.",  # retry
    ]
    deps = InlineTargetDeps(llm=_ScriptedLLM(scripted), system_prompt="C", max_rounds=1)
    result = await run_method([{"title": "s"}], _options(CONSOLIDATOR_ANALYST_ID), deps)
    assert result.finding.entry_kind == "consolidation"
    assert result.finding.body.startswith("# Inner landscape")  # recovered prose, not JSON
    assert getattr(result, "force_trace_only", False) is False
    rejected_steps = [
        s for s in result.intermediate_steps
        if s.get("kind") == "consolidation_shape_rejected"
    ]
    recovered_steps = [
        s for s in result.intermediate_steps
        if s.get("kind") == "consolidation_shape_recovered"
    ]
    assert rejected_steps and recovered_steps


@pytest.mark.asyncio
async def test_tool_call_json_body_rejected_still_bad_on_retry_writes_no_entry(
    caplog: pytest.LogCaptureFixture,
):
    """A retry that ALSO leaks tool-call JSON forces TRACE_ONLY (no persisted
    journal_entries row) and logs the distinct
    ``consolidation_shape_rejected`` WARNING token — an absent entry beats a
    garbage one."""
    scripted = [
        "field notes",
        _LEAKED_TOOL_CALL_JSON,  # narrate — leaked shape
        _LEAKED_TOOL_CALL_JSON,  # retry — STILL leaked
    ]
    deps = InlineTargetDeps(llm=_ScriptedLLM(scripted), system_prompt="C", max_rounds=1)
    with caplog.at_level(logging.WARNING, logger="legba.data.analysts.journal_assessor"):
        result = await run_method([{"title": "s"}], _options(CONSOLIDATOR_ANALYST_ID), deps)
    assert result.finding.entry_kind == "consolidation"
    assert getattr(result, "force_trace_only", False) is True
    fatal_steps = [
        s for s in result.intermediate_steps
        if s.get("kind") == "consolidation_shape_rejected_fatal"
    ]
    assert fatal_steps
    assert any(
        "consolidation_shape_rejected" in rec.message for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_prose_consolidation_body_unaffected_by_shape_guard():
    """Ordinary prose never trips the guard — zero-regression path."""
    scripted = [
        "field notes",
        "# Inner landscape\n\nThe quiet held; nothing to report but the "
        "usual steady watch over the wire this week.",
    ]
    deps = InlineTargetDeps(llm=_ScriptedLLM(scripted), system_prompt="C", max_rounds=1)
    result = await run_method([{"title": "s"}], _options(CONSOLIDATOR_ANALYST_ID), deps)
    assert result.finding.body.startswith("# Inner landscape")
    assert getattr(result, "force_trace_only", False) is False
    assert not [
        s for s in result.intermediate_steps
        if s.get("kind", "").startswith("consolidation_shape_rejected")
    ]
