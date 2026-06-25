# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-phase LLM split (journal §4.1): the heavy GATHER loop runs on the PRIMARY
handler (InnoGPT / vLLM, Reasoning:high) while the VOICE (the field-notes seam +
NARRATE) runs on the SECOND handler (Opus).

No DB — scripted LLM doubles + a fake governed binding. Covers:

  * deps-build dual-handler: with BOTH refs set, deps.llm resolves to the
    InnoGPT/primary component and deps.narrate_llm() to the Opus/narrate
    component — DIFFERENT handlers / DIFFERENT component ids.
  * fallback: with NO narrate ref, deps.narrate_llm() == deps.llm (zero-regression).
  * backward-compat: a normal inline_target analyst (no narrate ref) builds
    unchanged and narrate_llm() == llm.
  * routing: _field_notes + _narrate_with_tools invoke the narrate handler;
    _gather invokes the primary handler (proven via per-phase spy handlers).
  * Reasoning:high is in the GATHER system prompt and ABSENT from the
    NARRATE/field-notes system prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from unittest.mock import AsyncMock

import pytest
import yaml

from legba.data.analysts.agency.agency import AgencyOutcome
from legba.data.analysts.agency.tools import ToolResult
from legba.data.analysts.inline_target import (
    _REASONING_HIGH_DIRECTIVE,
    InlineTargetDeps,
)
from legba.data.analysts.journal_assessor import run_method
from legba.data.schemas.analyst import (
    AnalystDescriptor,
    register_analyst_kind,
)
from legba.runtime.analyst_deps_builder import build_analyst_run_method
from legba.runtime.deps import StandardDeps

_DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 10
    completion_tokens = 20
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _SpyLLM:
    """Records every (system, messages) it is called with + which named plane it
    is, so a test can prove WHICH handler authored each phase + what system prompt
    that phase saw. Pops scripted responses in order."""

    def __init__(self, name: str, scripted: list[str]) -> None:
        self.subprovider = name
        self.name = name
        self._scripted = list(scripted)
        self.systems: list[str] = []
        self.calls = 0

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls += 1
        self.systems.append(system or "")
        content = self._scripted.pop(0) if self._scripted else '{"done": true}'
        return _Response(content)


class _FakeBinding:
    def __init__(self, outputs: dict[str, dict[str, Any]] | None = None) -> None:
        self.outputs = outputs or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> AgencyOutcome:
        self.calls.append((tool_name, dict(args)))
        return AgencyOutcome(
            admitted=True,
            pack_id="journal_read",
            tool_name=tool_name,
            tool_result=ToolResult(
                status="completed",
                output=dict(self.outputs.get(tool_name, {})),
            ),
        )


def _options(binding: Any) -> dict[str, Any]:
    return {
        "analyst_id": "journal_assessor",
        "agency_binding": binding,
        "gather_tool_bindings": {},
    }


# ---------------------------------------------------------------------------
# deps-build: dual-handler resolution
# ---------------------------------------------------------------------------


def _load_descriptor(filename: str) -> AnalystDescriptor:
    register_analyst_kind("journal_assessor")
    body = yaml.safe_load((_DESCRIPTORS / filename).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


class _IdHandler:
    """A handler that REMEMBERS the component id it was built from, so the test can
    assert which plane each deps field resolved to."""

    def __init__(self, component_id: str) -> None:
        self.subprovider = component_id
        self.component_id = component_id

    async def chat_complete(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@pytest.mark.asyncio
async def test_deps_build_resolves_two_distinct_handlers():
    """journal_assessor: BOTH refs set → deps.llm is the InnoGPT/primary component,
    deps.narrate_llm() is the Opus/narrate component — DIFFERENT handlers + ids."""
    # The factory is keyed by component id (the production llm_handler_factory
    # contract), so it lets us prove the two refs resolved to two distinct planes.
    async def factory(component_id: str) -> Any:
        return _IdHandler(component_id)

    _run, kind_deps, _ok, _rc, _rs = await build_analyst_run_method(
        _load_descriptor("analyst_journal_assessor.yaml"),
        deps=StandardDeps(pg_pool=None, nats_publish=None, secrets_resolve=None),
        registry_client=AsyncMock(),
        pg_pool=None,
        llm_handler_factory=factory,
    )
    assert kind_deps.llm.component_id == "llm.primary.openai_compat"   # InnoGPT gather
    assert kind_deps.llm_narrate is not None
    assert kind_deps.narrate_llm().component_id == "llm.deep_consult"  # Opus voice
    # DIFFERENT handlers.
    assert kind_deps.llm is not kind_deps.narrate_llm()
    assert kind_deps.llm.component_id != kind_deps.narrate_llm().component_id
    # The split flips on the gather Reasoning:high + threads the Opus narrate cap.
    assert kind_deps.gather_reasoning_high is True
    assert kind_deps.narrate_tokens() == 16384


@pytest.mark.asyncio
async def test_deps_build_consolidator_resolves_two_distinct_handlers():
    """The consolidation tier carries the SAME split (24576 narrate cap)."""
    async def factory(component_id: str) -> Any:
        return _IdHandler(component_id)

    _run, kind_deps, _ok, _rc, _rs = await build_analyst_run_method(
        _load_descriptor("analyst_journal_consolidator.yaml"),
        deps=StandardDeps(pg_pool=None, nats_publish=None, secrets_resolve=None),
        registry_client=AsyncMock(),
        pg_pool=None,
        llm_handler_factory=factory,
    )
    assert kind_deps.llm.component_id == "llm.primary.openai_compat"
    assert kind_deps.narrate_llm().component_id == "llm.deep_consult"
    assert kind_deps.gather_reasoning_high is True
    assert kind_deps.narrate_tokens() == 24576


@pytest.mark.asyncio
async def test_deps_build_falls_back_when_no_narrate_ref():
    """journal descriptor with the narrate ref REMOVED → llm_narrate stays None and
    narrate_llm() falls back to the primary handler (zero-regression path)."""
    register_analyst_kind("journal_assessor")
    body = yaml.safe_load(
        (_DESCRIPTORS / "analyst_journal_assessor.yaml").read_text()
    )
    body.setdefault("identity", {})["version"] = "0" * 16
    # Strip the narrate ref — simulate a descriptor that did NOT opt into the split.
    body["method"]["llm"].pop("narrate", None)
    descriptor = AnalystDescriptor.model_validate(body, strict=False)

    async def factory(component_id: str) -> Any:
        return _IdHandler(component_id)

    _run, kind_deps, _ok, _rc, _rs = await build_analyst_run_method(
        descriptor,
        deps=StandardDeps(pg_pool=None, nats_publish=None, secrets_resolve=None),
        registry_client=AsyncMock(),
        pg_pool=None,
        llm_handler_factory=factory,
    )
    assert kind_deps.llm_narrate is None
    assert kind_deps.narrate_llm() is kind_deps.llm     # FALLBACK to primary
    # No second plane → the gather reasoning directive stays OFF.
    assert kind_deps.gather_reasoning_high is False


@pytest.mark.asyncio
async def test_inline_target_backward_compat_unaffected():
    """A normal inline_target analyst (no narrate ref) builds unchanged and
    narrate_llm() == llm — the assessors are byte-unaffected."""
    register_analyst_kind("inline_target")
    body = yaml.safe_load(
        (_DESCRIPTORS / "analyst_country_assessor.yaml").read_text()
    )
    body.setdefault("identity", {})["version"] = "0" * 16
    descriptor = AnalystDescriptor.model_validate(body, strict=False)

    async def factory(component_id: str) -> Any:
        return _IdHandler(component_id)

    run_fn, kind_deps, _ok, _rc, _rs = await build_analyst_run_method(
        descriptor,
        deps=StandardDeps(pg_pool=None, nats_publish=None, secrets_resolve=None),
        registry_client=AsyncMock(),
        pg_pool=None,
        llm_handler_factory=factory,
    )
    # inline_target returns kind_deps=None (the runner closes over its own bundle);
    # reach into the runner's private deps to prove the fallback holds.
    inner = run_fn._deps  # noqa: SLF001 — test introspection
    assert inner.llm_narrate is None
    assert inner.narrate_llm() is inner.llm
    assert inner.gather_reasoning_high is False


# ---------------------------------------------------------------------------
# Routing: which handler authors each phase + the Reasoning:high split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_uses_primary_voice_uses_narrate_handler():
    """End-to-end run with TWO distinct spy handlers: prove GATHER calls the
    primary handler and the field-notes + NARRATE calls hit the narrate handler."""
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    # The PRIMARY (gather) plane only sees the GATHER round (1 call).
    primary = _SpyLLM("innogpt-gather", ['{"done": true}'])
    # The NARRATE (voice) plane sees field-notes (1) + narrate (1) = 2 calls.
    narrate = _SpyLLM("opus-voice", [
        "Field notes in my own voice.",
        "# Entry\n\nThe window was quiet.\n\nI wonder what we are missing.",
    ])
    deps = InlineTargetDeps(
        llm=primary,
        llm_narrate=narrate,
        system_prompt="PERSONA",
        max_rounds=1,
        agency_binding=binding,
        gather_reasoning_high=True,
    )
    result = await run_method([{"title": "seed"}], _options(binding), deps)

    # GATHER hit the primary handler exactly once; the voice handler ran the seam +
    # narrate (≥2 calls) and the primary handler did NOT author the voice.
    assert primary.calls == 1                       # GATHER only
    assert narrate.calls >= 2                        # field-notes + narrate
    assert result.finding.entry_kind == "entry"
    assert "window was quiet" in result.finding.body

    # The Reasoning:high directive is in the GATHER (primary) system prompt …
    assert any(_REASONING_HIGH_DIRECTIVE in s for s in primary.systems)
    # … and ABSENT from every voice (narrate-handler) system prompt.
    assert all(_REASONING_HIGH_DIRECTIVE not in s for s in narrate.systems)


@pytest.mark.asyncio
async def test_reasoning_high_absent_when_split_off():
    """No split (single handler, gather_reasoning_high=False) → the directive is
    absent from the GATHER system prompt too — byte-for-byte the prior behavior."""
    binding = _FakeBinding(outputs={"get_calibration": {
        "available": True, "forecast_unproven": True, "calibration_thin": True,
    }})
    only = _SpyLLM("single", [
        '{"done": true}',                 # GATHER
        "field notes",                    # seam (same handler)
        "An entry.",                      # narrate (same handler)
    ])
    deps = InlineTargetDeps(
        llm=only, system_prompt="P", max_rounds=1, agency_binding=binding,
    )  # gather_reasoning_high defaults False; no llm_narrate
    await run_method([{"title": "s"}], _options(binding), deps)
    assert all(_REASONING_HIGH_DIRECTIVE not in s for s in only.systems)
