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
