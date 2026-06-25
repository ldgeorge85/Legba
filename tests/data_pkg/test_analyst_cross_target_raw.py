# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit + integration tests for the L-171 ``cross_target_raw`` analyst kind.

The kind is a pure-function async handler at the ``run_method`` boundary
— no substrate I/O inside ``run_method`` itself; the LLM port is the only
test-double surface (per task brief constraint "Real implementations; test
doubles only at LLM boundary").

The ``read_cross_target_slice`` helper IS exercised against the real
Postgres substrate so its multi-target query, time-window filter, and
column projection are verified end-to-end.

Coverage map (corresponds to brief acceptance items):

  * Module surface — ``KIND_NAME``, ``run_method``, ``CrossTargetRawRunner``,
    ``build_prompt_module``, ``read_cross_target_slice`` exist and are
    correctly typed (unit).
  * Shared types — ``AnalystMethodResult`` / ``LLMHandlerLike`` are the
    same Python objects as the inline_target sibling exposes (signature
    compatibility = interchangeable through the runtime actor).
  * Multi-target subscription read — fixtures across 3 targets, helper
    returns rows from all three, time-window correctly excludes ancient
    rows (integration).
  * Finding metadata — ``data.cross_target=True``, ``data.contributing_
    target_ids`` matches the union of input target_ids, tags include
    ``cross_target`` (unit).
  * Lineage ``derived_from`` — when the actor wrapper extracts UUIDs from
    input rows, it must surface signals from ALL contributing targets,
    not just one (integration: write signals across N targets, run the
    kind, verify the runner emits a finding whose contributing_target_ids
    spans the same N).
  * Prompt rendering — multi-target framing in user prompt, target_id
    annotations per row group (unit).
  * Empty input — runner emits a zero-target finding rather than raising
    (unit).
  * JSON robustness — markdown fences + trailing text are stripped,
    malformed JSON falls back gracefully (unit).
  * Provided vs derived target_ids — runner trusts ``options['target_ids']``
    when present, falls back to derived from row union (unit).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import cross_target_raw
from legba.data.analysts.cross_target_raw import (
    AnalystMethodResult,
    CrossTargetDeps,
    CrossTargetRawRunner,
    KIND_NAME,
    LLMHandlerLike,
    _extract_target_ids_from_inputs,
    _render_cross_target_user_prompt,
    build_prompt_module,
    read_cross_target_slice,
    run_method,
)
from legba.data.config import PostgresConfig
from legba.data.provenance import FindingPayload


# ---------------------------------------------------------------------------
# Test doubles — only the LLM boundary (per task constraint)
# ---------------------------------------------------------------------------


class _UsageStub:
    def __init__(self, p: int = 50, c: int = 40, r: int = 0) -> None:
        self.prompt_tokens = p
        self.completion_tokens = c
        self.reasoning_tokens = r


class _ResponseStub:
    def __init__(self, content: str, usage: _UsageStub | None = None) -> None:
        self.content = content
        self.usage = usage or _UsageStub()


class _StaticLLMStub:
    """LLM that returns a canned JSON finding referencing the multi-target slice."""

    subprovider = "test_static"

    def __init__(
        self,
        *,
        content: str | None = None,
        usage: _UsageStub | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._content = content
        self._usage = usage

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
            }
        )
        if self._content is None:
            content = json.dumps(
                {
                    "title": "Cross-target observation",
                    "body": "Three targets show correlated movement.",
                    "confidence": 0.7,
                    "evidence": ["evt-1", "evt-2"],
                    "tags": ["correlation"],
                }
            )
        else:
            content = self._content
        return _ResponseStub(content, self._usage)


class _DepsStub:
    """Minimal CrossTargetDeps satisfier — just carries an llm attribute."""

    def __init__(self, llm: LLMHandlerLike) -> None:
        self.llm = llm


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_kind_name():
    assert KIND_NAME == "cross_target_raw"


def test_module_exports_required_surface():
    # The package-level walker registers kinds by these names.
    assert hasattr(cross_target_raw, "KIND_NAME")
    assert hasattr(cross_target_raw, "run_method")
    assert hasattr(cross_target_raw, "CrossTargetRawRunner")
    assert hasattr(cross_target_raw, "build_prompt_module")
    assert hasattr(cross_target_raw, "read_cross_target_slice")
    # run_method is async
    import inspect
    assert inspect.iscoroutinefunction(run_method)


def test_shared_types_with_inline_target_sibling():
    """The kind's result + port types must be the exact same Python objects
    as inline_target's so the runtime actor wrapper can dispatch either
    kind through one code path (soft dependency on inline_target)."""
    from legba.data.analysts import inline_target

    assert AnalystMethodResult is inline_target.AnalystMethodResult
    assert LLMHandlerLike is inline_target.LLMHandlerLike


def test_build_prompt_module_returns_dspy_module():
    """Wave B prereq #4 (2026-05-21): build_prompt_module now returns a
    real dspy.Module instance instead of the placeholder dict descriptor.

    Skipped when dspy isn't installed — the optimizer (L-176) requires
    dspy at compile time, but the runtime's direct chat_complete path
    runs without it.
    """
    pytest.importorskip("dspy")
    from legba.prompts.cross_target_raw.v1 import CrossTargetRawCycle
    pm = build_prompt_module()
    assert isinstance(pm, CrossTargetRawCycle)


def test_prompt_module_path_constant_present():
    """The dotted import path is exposed as PROMPT_MODULE_PATH."""
    assert cross_target_raw.PROMPT_MODULE_PATH == "legba.prompts.cross_target_raw.v1"


def test_cross_target_deps_protocol_satisfied_by_simple_stub():
    """A bare object with an ``llm`` attribute satisfies the Protocol."""
    llm = _StaticLLMStub()
    deps = _DepsStub(llm)
    assert isinstance(deps, CrossTargetDeps)


# ---------------------------------------------------------------------------
# _extract_target_ids_from_inputs
# ---------------------------------------------------------------------------


def test_extract_target_ids_preserves_first_seen_order():
    inputs = [
        {"target_id": "br_energy", "title": "x"},
        {"target_id": "tw_semis", "title": "y"},
        {"target_id": "br_energy", "title": "z"},  # dup ignored
        {"target_id": "uk_grid", "title": "w"},
    ]
    assert _extract_target_ids_from_inputs(inputs) == ["br_energy", "tw_semis", "uk_grid"]


def test_extract_target_ids_skips_missing_and_blank():
    inputs = [
        {"target_id": "a", "title": "x"},
        {"target_id": None, "title": "y"},
        {"target_id": "", "title": "z"},
        {"title": "no-id"},
        {"target_id": "a", "title": "dup"},
        {"target_id": "b", "title": "ok"},
    ]
    assert _extract_target_ids_from_inputs(inputs) == ["a", "b"]


# ---------------------------------------------------------------------------
# _render_cross_target_user_prompt
# ---------------------------------------------------------------------------


def test_render_user_prompt_groups_by_target_and_names_all():
    inputs = [
        {"target_id": "br", "title": "BR signal 1", "source_url": "u1", "data": {"summary": "s1"}},
        {"target_id": "tw", "title": "TW signal 1", "source_url": "u2", "data": {"summary": "s2"}},
        {"target_id": "br", "title": "BR signal 2", "source_url": "u3", "data": {"summary": "s3"}},
    ]
    target_ids = ["br", "tw"]
    text = _render_cross_target_user_prompt(inputs, target_ids)
    # Header names both targets.
    assert "br" in text and "tw" in text
    assert "2 target(s)" in text or "2 targets" in text.lower()
    # Each target gets a group header.
    assert "target_id=br" in text
    assert "target_id=tw" in text
    # Snippets present.
    assert "s1" in text and "s2" in text and "s3" in text


def test_render_user_prompt_handles_no_targets():
    text = _render_cross_target_user_prompt([], [])
    assert "0 target" in text
    assert "Total signals: 0" in text


def test_render_user_prompt_caps_total_rows():
    """30-row cap protects LLM context budget."""
    inputs = [
        {"target_id": "t1", "title": f"sig {i}", "data": {"summary": f"snip {i}"}}
        for i in range(50)
    ]
    text = _render_cross_target_user_prompt(inputs, ["t1"])
    # We render at most 10/target (per-target cap of 10 in renderer), so
    # for 1 target with 50 signals we see exactly 10 rows.
    rendered_lines = [ln for ln in text.split("\n") if "produced_at=" in ln]
    assert len(rendered_lines) == 10


# ---------------------------------------------------------------------------
# CrossTargetRawRunner — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_happy_path_emits_cross_tagged_finding():
    """Three-target slice → finding carries cross_target metadata."""
    llm = _StaticLLMStub()
    runner = CrossTargetRawRunner(llm)
    inputs = [
        {"id": uuid4(), "target_id": "br", "title": "BR1", "data": {}, "produced_at": datetime.now(tz=timezone.utc)},
        {"id": uuid4(), "target_id": "tw", "title": "TW1", "data": {}, "produced_at": datetime.now(tz=timezone.utc)},
        {"id": uuid4(), "target_id": "uk", "title": "UK1", "data": {}, "produced_at": datetime.now(tz=timezone.utc)},
    ]
    options = {
        "analyst_id": "analyst.cross_test",
        "analyst_version": "v1",
        "run_id": uuid4(),
    }
    result = await runner(inputs, options)
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    # Metadata on the finding's data column.
    assert result.finding.data["cross_target"] is True
    assert set(result.finding.data["contributing_target_ids"]) == {"br", "tw", "uk"}
    # Tag carries cross_target marker.
    assert "cross_target" in result.finding.tags
    # Token usage rolled up.
    assert result.usage["prompt_tokens"] == 50
    assert result.usage["completion_tokens"] == 40
    # System prompt is the broader-data one.
    assert "MULTIPLE targets" in llm.calls[0]["system"] or "multi" in llm.calls[0]["system"].lower()


@pytest.mark.asyncio
async def test_run_method_module_entry_dispatches_via_deps_protocol():
    """The module-level run_method threads through CrossTargetDeps."""
    llm = _StaticLLMStub()
    deps = _DepsStub(llm)
    inputs = [
        {"id": uuid4(), "target_id": "a", "title": "A", "data": {}},
        {"id": uuid4(), "target_id": "b", "title": "B", "data": {}},
    ]
    result = await run_method(inputs, {"run_id": uuid4()}, deps)
    assert result.finding.data["cross_target"] is True
    assert set(result.finding.data["contributing_target_ids"]) == {"a", "b"}


@pytest.mark.asyncio
async def test_runner_prefers_provided_target_ids_then_unions_derived():
    """When subscription resolution provides target_ids, the runner uses
    them as the authoritative ordering; missing-from-provided derived
    targets are appended so stale resolutions can't drop visible rows."""
    llm = _StaticLLMStub()
    runner = CrossTargetRawRunner(llm)
    inputs = [
        # Row whose target_id WASN'T in the resolved list (e.g. a target
        # was newly added between resolve and run).
        {"id": uuid4(), "target_id": "surprise", "title": "X", "data": {}},
        {"id": uuid4(), "target_id": "provided_a", "title": "Y", "data": {}},
    ]
    options = {
        "run_id": uuid4(),
        "target_ids": ["provided_a", "provided_b"],  # provided_b has no rows
    }
    result = await runner(inputs, options)
    # Provided list comes first (preserving order), then surprise is unioned.
    cids = result.finding.data["contributing_target_ids"]
    assert cids[0] == "provided_a"
    assert cids[1] == "provided_b"
    assert "surprise" in cids
    # provided_b is reported even though no rows landed for it — that's
    # the runtime's promise; the finding declares which targets it CLAIMED
    # to read.


@pytest.mark.asyncio
async def test_runner_empty_input_emits_zero_target_finding():
    """No inputs → emit a defensive finding rather than crash."""
    llm = _StaticLLMStub()
    runner = CrossTargetRawRunner(llm)
    result = await runner([], {"run_id": uuid4()})
    assert isinstance(result, AnalystMethodResult)
    assert result.finding.data["cross_target"] is True
    assert result.finding.data["contributing_target_ids"] == []


@pytest.mark.asyncio
async def test_runner_ignores_non_string_target_ids_in_options():
    """target_ids: [1, None, ''] is malformed; runner falls back to derived."""
    llm = _StaticLLMStub()
    runner = CrossTargetRawRunner(llm)
    inputs = [{"id": uuid4(), "target_id": "real", "title": "X", "data": {}}]
    options = {"run_id": uuid4(), "target_ids": [1, None, "", "valid"]}
    result = await runner(inputs, options)
    cids = result.finding.data["contributing_target_ids"]
    assert "valid" in cids
    assert "real" in cids
    # Non-strings dropped.
    assert 1 not in cids and None not in cids and "" not in cids


# ---------------------------------------------------------------------------
# JSON parser robustness — markdown fences, trailing text, malformed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_handles_markdown_fenced_json():
    fenced = (
        "```json\n"
        + json.dumps({"title": "Fenced finding", "body": "ok", "confidence": 0.6})
        + "\n```"
    )
    llm = _StaticLLMStub(content=fenced)
    runner = CrossTargetRawRunner(llm)
    inputs = [{"id": uuid4(), "target_id": "a", "title": "x", "data": {}}]
    result = await runner(inputs, {})
    assert result.finding.title == "Fenced finding"
    assert result.finding.confidence == 0.6
    assert result.finding.data["cross_target"] is True


@pytest.mark.asyncio
async def test_runner_handles_trailing_text_after_json():
    payload = json.dumps({"title": "T", "body": "B", "confidence": 0.5})
    response = payload + "\n\nSome explanatory text the model added."
    llm = _StaticLLMStub(content=response)
    runner = CrossTargetRawRunner(llm)
    inputs = [{"id": uuid4(), "target_id": "a", "title": "x", "data": {}}]
    result = await runner(inputs, {})
    assert result.finding.title == "T"


@pytest.mark.asyncio
async def test_runner_handles_malformed_json_with_unstructured_tag():
    llm = _StaticLLMStub(content="this is not json")
    runner = CrossTargetRawRunner(llm)
    inputs = [{"id": uuid4(), "target_id": "a", "title": "x", "data": {}}]
    result = await runner(inputs, {})
    assert "unstructured" in result.finding.tags
    # cross_target tag is still applied so downstream filters can route it.
    assert "cross_target" in result.finding.tags
    # Finding still carries the metadata.
    assert result.finding.data["cross_target"] is True


@pytest.mark.asyncio
async def test_runner_handles_non_dict_json_response():
    """JSON that parses but isn't an object (e.g., array)."""
    llm = _StaticLLMStub(content="[1, 2, 3]")
    runner = CrossTargetRawRunner(llm)
    inputs = [{"id": uuid4(), "target_id": "a", "title": "x", "data": {}}]
    result = await runner(inputs, {})
    assert "unstructured" in result.finding.tags
    assert "cross_target" in result.finding.tags


@pytest.mark.asyncio
async def test_runner_idempotent_cross_target_tag():
    """If the model already returns cross_target in tags, we don't double-add."""
    content = json.dumps(
        {
            "title": "T",
            "body": "B",
            "confidence": 0.5,
            "tags": ["cross_target", "energy"],
        }
    )
    llm = _StaticLLMStub(content=content)
    runner = CrossTargetRawRunner(llm)
    inputs = [{"id": uuid4(), "target_id": "a", "title": "x", "data": {}}]
    result = await runner(inputs, {})
    assert result.finding.tags.count("cross_target") == 1


# ---------------------------------------------------------------------------
# Integration — read_cross_target_slice against real Postgres substrate
# ---------------------------------------------------------------------------


# C-1 NOTE: the five retired pre-pivot integration tests (multi-target
# slice / window / projection / lineage over target-owned `signals` rows
# written via write_target_signal) were DELETED — they asserted the
# pre-pivot signals shape migration 0024 dropped. Post-pivot cross-target
# reads are exercised through the source-first subscription engine slice
# (tests/runtime/test_subscription_engine.py).


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_cross_target_slice_empty_target_ids_returns_empty(pg_conn):
    rows = await read_cross_target_slice(pg_conn, target_ids=[])
    assert rows == []
