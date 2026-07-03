# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-170 tests for the inline_target analyst kind.

Covers:
  * happy path — synthetic signal list → structured finding.
  * empty signals — short-circuit returns an "empty_slice" finding.
  * LLM error — exceptions propagate to the runtime (kind_contracts §7).
  * oversized signal payload — slice is trimmed; truncation preserved.
  * markdown-fenced JSON response — REFLECT strips fences.
  * malformed JSON response — REFLECT downgrades to "unstructured".
  * NARRATE attaches target/analyst tags + derived_from lineage.
  * DSPy module compiles + signature shape — via pytest.importorskip.

Tests use a typed LLM test double conforming to ``LLMHandlerLike``
(same pattern as ``tests/runtime/test_spike_integration.py``).

The data_pkg ``conftest.py`` brings up substrate containers, but these
tests don't touch substrate — they're pure-Python unit tests on the
kind handler.  The conftest's session-scoped substrate fixture is
``autouse=True`` so containers will spin up regardless; that's fine
(spin-up is cached across the suite).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.inline_target import (
    AnalystMethodResult,
    HANDLER_VERSION,
    InlineTargetDeps,
    InlineTargetRunner,
    KIND_NAME,
    PROMPT_MODULE_PATH,
    SCHEMA_VERSION,
    _build_citation_index,
    _coerce_finding,
    _extract_citations,
    _normalize_citation_markers,
    _orient,
    _render_user_prompt,
    _title_from_text,
    _unwrap_envelope_body,
    build_prompt_module,
    run_method,
)
from legba.data.analysts.agency.agency import AgencyOutcome
from legba.data.analysts.agency.tools import ToolResult
from legba.data.provenance.models import FindingPayload


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _StubLLMHandler:
    """Typed test double conforming to ``LLMHandlerLike``.

    Returns a canned JSON finding by default; ``content_override`` lets
    individual tests inject markdown fences, malformed JSON, etc.
    ``raise_on_call`` lets the error-case test exercise the propagation
    path.
    """

    subprovider = "openai"

    def __init__(
        self,
        *,
        content_override: str | None = None,
        raise_on_call: type[BaseException] | None = None,
    ) -> None:
        self._content_override = content_override
        self._raise_on_call = raise_on_call
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
        })
        if self._raise_on_call is not None:
            raise self._raise_on_call("simulated LLM transport failure")
        if self._content_override is not None:
            return _Response(content=self._content_override, usage=_Usage())
        finding = {
            "title": "Brazil Energy Infrastructure Update",
            "body": (
                "Itaipu upgrade complete; Northeast wind capacity record; "
                "Petrobras Q1 refinery figures published."
            ),
            "confidence": 0.85,
            "evidence": [
                "Itaipu upgrade May 19",
                "Wind capacity record May 18",
                "Petrobras Q1 figures",
            ],
            "tags": ["energy", "infrastructure"],
        }
        return _Response(content=json.dumps(finding), usage=_Usage())


def _signal_row(
    *,
    id_: UUID,
    title: str = "Itaipu hydro plant upgrade complete",
    produced_at: str = "2026-05-19T14:00:00+00:00",
    source_url: str = "https://example.com/news/itaipu",
    snippet: str = "Brazil's Itaipu hydroelectric plant completes its turbine upgrade.",
) -> dict[str, Any]:
    return {
        "id": id_,
        "title": title,
        "produced_at": produced_at,
        "source_url": source_url,
        "data": {"summary": snippet},
    }


# ---------------------------------------------------------------------------
# Identity constants
# ---------------------------------------------------------------------------


def test_kind_identity_constants():
    """KIND_NAME, SCHEMA_VERSION, HANDLER_VERSION, PROMPT_MODULE_PATH
    match the L-170 contract.  These are load-bearing — the registry
    indexes by them and the optimizer imports the prompt module by
    string path."""
    assert KIND_NAME == "inline_target"
    assert SCHEMA_VERSION == "legba/analyst.inline_target/1-0-0"
    assert HANDLER_VERSION == "0.1.0"
    assert PROMPT_MODULE_PATH == "legba.prompts.inline_target.v1"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_happy_path():
    """Synthetic signal list → run_method → AnalystMethodResult with a
    structured finding, lineage list, usage dict, and the full
    7-phase intermediate_steps trace."""
    sig_ids = [uuid4() for _ in range(3)]
    inputs = [
        _signal_row(id_=sig_ids[0], produced_at="2026-05-19T14:00:00+00:00",
                    title="Itaipu hydro upgrade"),
        _signal_row(id_=sig_ids[1], produced_at="2026-05-18T09:30:00+00:00",
                    title="Wind capacity record"),
        _signal_row(id_=sig_ids[2], produced_at="2026-05-17T10:00:00+00:00",
                    title="Petrobras Q1 figures"),
    ]
    llm = _StubLLMHandler()
    deps = InlineTargetDeps(llm=llm)

    result = await run_method(
        inputs,
        {"target_id": "india_energy", "analyst_id": "analyst.india_energy"},
        deps,
    )

    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    assert result.finding.title == "Brazil Energy Infrastructure Update"
    assert result.finding.confidence == 0.85
    assert len(result.finding.evidence) == 3

    # Lineage — all 3 signal UUIDs carried through.
    assert set(result.derived_from) == set(sig_ids)

    # Usage — token counts surfaced from the stub.
    assert result.usage["prompt_tokens"] == 100
    assert result.usage["completion_tokens"] == 50

    # Cycle envelope — all 7 phases recorded.
    phases = [step["phase"] for step in result.intermediate_steps]
    assert phases == [
        "wake", "orient", "plan", "reason", "reflect", "narrate", "persist",
    ]

    # NARRATE stamped target + analyst tags.
    assert "target:india_energy" in result.finding.tags
    assert "analyst:analyst.india_energy" in result.finding.tags
    # Original LLM tags preserved.
    assert "energy" in result.finding.tags

    # Single LLM call.
    assert len(llm.calls) == 1
    assert llm.calls[0]["max_tokens"] == 1024
    assert llm.calls[0]["temperature"] == 0.2


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_empty_signals():
    """Empty input → no LLM call, "empty_slice" finding, full envelope
    trace (no `reason` phase since no LLM call happened)."""
    llm = _StubLLMHandler()
    result = await run_method([], {"target_id": "india_energy"}, llm)

    # No LLM call.
    assert llm.calls == []

    # Defensive finding.
    assert result.finding.title.startswith("No signals for")
    assert result.finding.confidence == 0.0
    assert "empty_slice" in result.finding.tags

    # No lineage to attach.
    assert result.derived_from == []

    # No usage recorded.
    assert result.usage == {}

    # Envelope: wake, orient (noop), reflect-noop, narrate, persist.
    phases = [step["phase"] for step in result.intermediate_steps]
    assert "reason" not in phases
    assert phases[0] == "wake"
    assert phases[-1] == "persist"


# ---------------------------------------------------------------------------
# LLM error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_llm_error_propagates():
    """A raised exception in the LLM bubbles up — the runtime classifies
    it (kind_contracts §7: TransientFailure / BudgetExhausted /
    HardFailure).  The kind handler MUST NOT swallow."""
    class _SimulatedTransport(RuntimeError):
        pass

    llm = _StubLLMHandler(raise_on_call=_SimulatedTransport)
    sig_id = uuid4()
    inputs = [_signal_row(id_=sig_id)]

    with pytest.raises(_SimulatedTransport):
        await run_method(inputs, {"target_id": "india_energy"}, llm)


# ---------------------------------------------------------------------------
# Oversized payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_input_token_budget_trims(monkeypatch):
    """The slice is bounded by an INPUT-token budget, not a fixed row count.
    With a tiny budget only the most-recent signals that fit are kept; the
    dropped rows do NOT appear in derived_from. (The model's OUTPUT is never
    capped — we don't send max_tokens to the core plane.)"""
    monkeypatch.setenv("LEGBA_LLM_INPUT_TOKEN_BUDGET", "200")
    sig_ids = [uuid4() for _ in range(100)]
    inputs = [
        _signal_row(
            id_=sig_ids[i],
            title=f"Signal #{i}",
            produced_at=f"2026-05-{(i % 28) + 1:02d}T12:00:00+00:00",
        )
        for i in range(100)
    ]
    llm = _StubLLMHandler()

    result = await run_method(inputs, {"target_id": "india_energy"}, llm)

    # The token budget trimmed the 100 rows to a small set (not the old fixed 20).
    kept = len(result.derived_from)
    assert 0 < kept < 100

    # The orient phase recorded the budget-driven trim.
    orient_step = next(
        s for s in result.intermediate_steps if s["phase"] == "orient"
    )
    assert orient_step["in_count"] == 100
    assert orient_step["kept_count"] == kept
    assert orient_step["derived_count"] == kept

    # The rendered prompt header reflects the kept count.
    plan_step = next(
        s for s in result.intermediate_steps if s["phase"] == "plan"
    )
    user_msg = llm.calls[0]["messages"][0]["content"]
    assert f"Number of signals: {kept}" in user_msg
    assert plan_step["prompt_module"] == PROMPT_MODULE_PATH


@pytest.mark.asyncio
async def test_run_method_keeps_more_than_old_cap_under_default_budget(monkeypatch):
    """Under the default ~32K budget a 60-signal slice of normal signals is kept
    whole — proving the old fixed 20-row cap is gone (the bound is tokens now)."""
    monkeypatch.delenv("LEGBA_LLM_INPUT_TOKEN_BUDGET", raising=False)
    inputs = [
        _signal_row(
            id_=uuid4(),
            title=f"Signal #{i}",
            produced_at=f"2026-05-{(i % 28) + 1:02d}T12:00:00+00:00",
        )
        for i in range(60)
    ]
    llm = _StubLLMHandler()

    result = await run_method(inputs, {"target_id": "india_energy"}, llm)

    # All 60 kept — well under the token budget, and far above the old cap of 20.
    assert len(result.derived_from) == 60


# ---------------------------------------------------------------------------
# REFLECT — markdown fence stripping
# ---------------------------------------------------------------------------


def test_coerce_finding_markdown_fenced_json():
    """LLM wraps the JSON in ```json ... ```; REFLECT strips fences."""
    raw = (
        "```json\n"
        '{"title": "x", "body": "y", "confidence": 0.7, '
        '"evidence": ["a"], "tags": ["b"]}\n'
        "```"
    )
    finding = _coerce_finding(raw, fallback_title="fallback")
    assert finding.title == "x"
    assert finding.body == "y"
    assert finding.confidence == 0.7
    assert finding.evidence == ["a"]
    assert finding.tags == ["b"]
    # "unstructured" tag not present — parse succeeded.
    assert "unstructured" not in finding.tags


def test_coerce_finding_malformed_json():
    """Bad JSON → "unstructured" finding with the raw body preserved.

    D27: the title is now LIFTED from the LLM prose (the first usable line)
    rather than the static placeholder, so the product surface shows a real
    headline instead of "Assessment for country_g20_XX". The placeholder is
    only the last resort when no usable line exists.
    """
    raw = "this is not json at all { incomplete"
    finding = _coerce_finding(raw, fallback_title="Default Title")
    # Title derived from the prose (not the placeholder).
    assert finding.title == raw
    assert raw in finding.body
    assert "unstructured" in finding.tags
    assert finding.confidence == 0.3


def test_coerce_finding_whitespace_only_falls_back_to_placeholder():
    """D27: only when the LLM produced no usable line at all does the static
    fallback title surface."""
    finding = _coerce_finding("   \n  \n", fallback_title="Default Title")
    assert finding.title == "Default Title"
    assert "unstructured" in finding.tags


def test_coerce_finding_trailing_text():
    """JSON followed by trailing prose — REFLECT clips at the closing brace."""
    raw = (
        '{"title": "x", "body": "y", "confidence": 0.5, '
        '"evidence": [], "tags": []}\n'
        "Some explanation the model added after the JSON object."
    )
    finding = _coerce_finding(raw, fallback_title="fallback")
    assert finding.title == "x"
    # The trailing text is NOT in body (we clipped at the brace) — but
    # it IS in `data.raw_llm_response`, which is good for audit.
    assert finding.body == "y"
    assert "raw_llm_response" in finding.data


def test_coerce_finding_non_object_json():
    """JSON parses but isn't a dict — falls back to unstructured."""
    raw = '["not", "a", "dict"]'
    finding = _coerce_finding(raw, fallback_title="fallback")
    assert "unstructured" in finding.tags


# ---------------------------------------------------------------------------
# D27 — product-surface: LLM-authored title fallback + JSON-envelope unwrap
# ---------------------------------------------------------------------------


def test_title_from_text_prefers_markdown_heading():
    text = "## Turkey: rising lira pressure\n\nBody prose here."
    assert _title_from_text(text, fallback_title="ph") == "Turkey: rising lira pressure"


def test_title_from_text_strips_bluf_label():
    text = "BLUF: India tightens export controls on rice.\nMore detail follows."
    assert _title_from_text(text, fallback_title="ph") == (
        "India tightens export controls on rice."
    )


def test_title_from_text_first_usable_line_and_skips_braces():
    text = "{\n  Brazil fiscal outlook deteriorates\n}"
    assert _title_from_text(text, fallback_title="ph") == (
        "Brazil fiscal outlook deteriorates"
    )


def test_title_from_text_falls_back_when_empty():
    assert _title_from_text("   \n\n", fallback_title="Assessment for x") == (
        "Assessment for x"
    )


def test_unwrap_envelope_body_unwraps_double_wrapped_json():
    inner = '{"title": "T", "body": "## Real markdown\\n- point"}'
    assert _unwrap_envelope_body(inner) == "## Real markdown\n- point"


def test_unwrap_envelope_body_unwraps_fenced_envelope():
    inner = '```json\n{"title": "T", "body": "plain markdown body"}\n```'
    assert _unwrap_envelope_body(inner) == "plain markdown body"


def test_unwrap_envelope_body_passthrough_for_plain_markdown():
    body = "## Heading\nNormal markdown, not an envelope."
    assert _unwrap_envelope_body(body) == body


def test_coerce_finding_unwraps_envelope_body_into_markdown():
    """D27: a double-wrapped {title, body} in the body column is unwrapped so the
    body holds rendered markdown, not raw JSON."""
    raw = json.dumps(
        {
            "title": "Outer title",
            "body": json.dumps({"title": "Inner", "body": "## Rendered\nmarkdown"}),
            "confidence": 0.6,
        }
    )
    finding = _coerce_finding(raw, fallback_title="ph")
    assert finding.title == "Outer title"
    assert finding.body == "## Rendered\nmarkdown"
    assert "{" not in finding.body.splitlines()[0]


def test_coerce_finding_lifts_title_when_body_present_but_title_missing():
    """D27: a parsed dict with body but no title lifts the title from the body
    markdown rather than using the static placeholder."""
    raw = json.dumps({"body": "# Argentina inflation reaccelerates\nDetails."})
    finding = _coerce_finding(raw, fallback_title="Assessment for country_g20_ar")
    assert finding.title == "Argentina inflation reaccelerates"
    assert finding.title != "Assessment for country_g20_ar"


# ---------------------------------------------------------------------------
# D27 SECOND PASS — the live us/cn/fr/za case the W4 fix MISSED: the body
# COLUMN is a JSON-STRINGIFIED envelope (a string that literally starts with
# {"title": ...}), not a {title,body} dict whose body field is stringified.
# Detect it, render the inner body as markdown, lift the inner title.
# ---------------------------------------------------------------------------


def test_unwrap_envelope_returns_inner_title_and_body():
    """_unwrap_envelope lifts BOTH the inner body and the inner title."""
    from legba.data.analysts.inline_target import _unwrap_envelope

    inner = '{"title": "US: shutdown risk rises", "body": "## Markdown\\n- pt"}'
    body, title = _unwrap_envelope(inner)
    assert body == "## Markdown\n- pt"
    assert title == "US: shutdown risk rises"


def test_unwrap_envelope_passthrough_returns_none_title():
    """Plain markdown is NOT an envelope → body unchanged, title None."""
    from legba.data.analysts.inline_target import _unwrap_envelope

    body, title = _unwrap_envelope("## Plain heading\nnot an envelope")
    assert body == "## Plain heading\nnot an envelope"
    assert title is None


def test_coerce_finding_body_column_is_stringified_envelope_missing_outer_title():
    """The verbatim live us/cn/fr/za defect: the outer dict's ``body`` field is
    a JSON-STRINGIFIED ``{title, body}`` envelope and the OUTER title is
    missing — the inner title must be lifted (not the static placeholder) and
    the inner body rendered as markdown (no leading brace)."""
    raw = json.dumps(
        {
            "body": json.dumps(
                {
                    "title": "France: pension reform standoff deepens",
                    "body": "## Key developments\n- strikes widen [1]",
                }
            ),
            "confidence": 0.55,
        }
    )
    finding = _coerce_finding(raw, fallback_title="Assessment for country_g20_fr")
    assert finding.title == "France: pension reform standoff deepens"
    assert finding.title != "Assessment for country_g20_fr"
    assert finding.body == "## Key developments\n- strikes widen [1]"
    assert not finding.body.lstrip().startswith("{")


def test_coerce_finding_stringified_envelope_outer_title_wins():
    """When the OUTER title is present it wins over the inner envelope title,
    but the body is still unwrapped to the inner markdown."""
    raw = json.dumps(
        {
            "title": "South Africa: load-shedding eases",
            "body": json.dumps({"title": "Inner ZA", "body": "## Body\nprose"}),
        }
    )
    finding = _coerce_finding(raw, fallback_title="Assessment for country_g20_za")
    assert finding.title == "South Africa: load-shedding eases"
    assert finding.body == "## Body\nprose"


def test_coerce_finding_stringified_envelope_no_inner_title_lifts_from_body():
    """The CN case: body is a stringified envelope with NO inner title and NO
    outer title → the title is lifted from the inner body markdown heading."""
    raw = json.dumps(
        {
            "body": json.dumps({"body": "# China export curbs tighten\nDetails."}),
        }
    )
    finding = _coerce_finding(raw, fallback_title="Assessment for country_g20_cn")
    assert finding.title == "China export curbs tighten"
    assert finding.title != "Assessment for country_g20_cn"
    assert finding.body == "# China export curbs tighten\nDetails."


def test_coerce_finding_stringified_fenced_envelope_body():
    """The body column is a fenced ```json {title, body} ``` block — unwrap it
    and lift the inner title."""
    raw = json.dumps(
        {
            "body": '```json\n{"title": "US: rates held", "body": "plain body"}\n```',
        }
    )
    finding = _coerce_finding(raw, fallback_title="ph")
    assert finding.title == "US: rates held"
    assert finding.body == "plain body"


# ---------------------------------------------------------------------------
# #125 — parse-fallback envelope salvage: never persist a raw {title, body} JSON
# string as the body, even on a malformed/truncated envelope or a deeply-nested
# one the single-level unwrap missed.
# ---------------------------------------------------------------------------


def test_coerce_finding_truncated_envelope_does_not_store_raw_json():
    """#125: a MALFORMED/TRUNCATED {title, body} envelope (the stream cut off
    mid-body, so json.loads fails) must NOT land as a raw JSON string — the inner
    markdown body is salvaged instead."""
    raw = (
        '{"title": "US: shutdown risk rises", '
        '"body": "## Assessment\\n\\nThe federal government faces a funding lapse [1].'
    )  # NOTE: truncated — no closing quote / brace.
    finding = _coerce_finding(raw, fallback_title="Assessment for country_g20_us")
    # The JSON wrapper is GONE — no scaffolding keys, no leading brace.
    assert '"title"' not in finding.body
    assert '"body"' not in finding.body
    assert not finding.body.lstrip().startswith("{")
    # The salvaged markdown IS present.
    assert "The federal government faces a funding lapse" in finding.body
    assert "unstructured" in finding.tags


def test_coerce_finding_truncated_envelope_with_trailing_corruption():
    """#125: a complete body string but corrupt trailing bytes (invalid JSON after
    the body) also salvages the inner markdown rather than storing the wrapper."""
    raw = (
        '{"title": "AR", "body": "# Argentina inflation reaccelerates\\nDetail here."'
        ", broken garbage ]]"
    )
    finding = _coerce_finding(raw, fallback_title="ph")
    assert '"body"' not in finding.body
    assert finding.body.startswith("# Argentina inflation reaccelerates")
    # The lifted title comes from the salvaged markdown heading.
    assert finding.title == "Argentina inflation reaccelerates"


def test_coerce_finding_unwraps_triple_nested_envelope():
    """#125: an envelope nested 2+ levels deep is fully peeled by the recursive
    unwrap — no raw JSON string is left in the body column."""
    innermost = "## Real markdown\n- a point"
    lvl1 = json.dumps({"title": "L1", "body": innermost})
    lvl2 = json.dumps({"title": "L2", "body": lvl1})
    raw = json.dumps({"title": "Outer", "body": lvl2})
    finding = _coerce_finding(raw, fallback_title="ph")
    assert finding.title == "Outer"
    assert finding.body == innermost
    assert "{" not in finding.body.splitlines()[0]


def test_coerce_finding_non_envelope_prose_preserved_on_parse_failure():
    """#125: the salvage guard is envelope-only — plain LLM prose that fails to
    parse is still kept verbatim (the byte-for-byte D27 fallback)."""
    raw = "this is not json at all { incomplete"
    finding = _coerce_finding(raw, fallback_title="Default Title")
    assert finding.title == raw
    assert raw in finding.body
    assert "unstructured" in finding.tags


def test_salvage_envelope_body_returns_none_for_non_envelope():
    """#125: the salvage helper returns None (caller keeps raw) when the text is
    not a JSON object or carries no body field."""
    from legba.data.analysts.inline_target import _salvage_envelope_body

    assert _salvage_envelope_body("just some prose, no braces") is None
    assert _salvage_envelope_body('{"title": "no body field here"}') is None
    assert _salvage_envelope_body("") is None


def test_build_citation_index_captures_snippet():
    """#116(e): the render-time index captures a compact evidence snippet from the
    signal's data so the verify judge sees content, not just the headline."""
    sid = uuid4()
    sliced = [
        {
            "id": sid,
            "title": "Fed decision",
            "source_url": "https://x/1",
            "data": {"summary": "The FOMC held the target range steady."},
        },
        {"id": uuid4(), "title": "No data row"},
    ]
    index = _build_citation_index(sliced)
    assert index[1]["snippet"] == "The FOMC held the target range steady."
    assert index[2]["snippet"] is None
    # And _extract_citations carries the snippet onto the citation entry.
    citations, _, _ = _extract_citations("A claim [1].", index)
    assert citations[0]["snippet"] == "The FOMC held the target range steady."


# ---------------------------------------------------------------------------
# ORIENT — sorts produced_at descending, handles None
# ---------------------------------------------------------------------------


def test_orient_sorts_newest_first():
    ids = [uuid4() for _ in range(3)]
    inputs = [
        {"id": ids[0], "produced_at": "2026-05-17T10:00:00+00:00"},
        {"id": ids[1], "produced_at": "2026-05-19T14:00:00+00:00"},
        {"id": ids[2], "produced_at": "2026-05-18T09:30:00+00:00"},
    ]
    sliced, derived = _orient(inputs, "india_energy")
    assert sliced[0]["id"] == ids[1]   # 2026-05-19 first
    assert sliced[1]["id"] == ids[2]   # 2026-05-18
    assert sliced[2]["id"] == ids[0]   # 2026-05-17
    assert derived == [ids[1], ids[2], ids[0]]


def test_orient_handles_missing_id():
    """Rows without an `id` field are kept in the slice but excluded
    from `derived_from`.  Rows with a string id get parsed to UUID."""
    valid_uuid = uuid4()
    inputs = [
        {"id": str(valid_uuid), "produced_at": "2026-05-19T14:00:00+00:00"},
        {"produced_at": "2026-05-18T09:30:00+00:00"},          # no id
        {"id": "not-a-uuid", "produced_at": "2026-05-17T10:00:00+00:00"},  # bad id
    ]
    sliced, derived = _orient(inputs, "india_energy")
    assert len(sliced) == 3                                     # all kept
    assert derived == [valid_uuid]                              # only valid


# ---------------------------------------------------------------------------
# Prompt rendering — input cap + truncation
# ---------------------------------------------------------------------------


def test_render_user_prompt_truncates_long_fields():
    long_title = "A" * 1000
    long_snippet = "B" * 5000
    inputs = [{
        "id": uuid4(),
        "title": long_title,
        "produced_at": "2026-05-19T14:00:00+00:00",
        "source_url": "https://example.com",
        "data": {"summary": long_snippet},
    }]
    rendered = _render_user_prompt(inputs, "india_energy")
    # Title truncated to 200 chars.
    assert "A" * 200 in rendered
    assert "A" * 201 not in rendered
    # Snippet truncated to _MAX_SNIPPET_CHARS (1500).
    assert "B" * 1500 in rendered
    assert "B" * 1501 not in rendered


# ---------------------------------------------------------------------------
# Backward-compat path — bare LLMHandlerLike accepted as deps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_accepts_bare_llm_handler():
    """The spike's old call shape — ``run_method(inputs, options, llm)``
    where deps is a bare LLM handler — still works.  Important for the
    LLMAnalystRunner re-export not to break."""
    sig_id = uuid4()
    inputs = [_signal_row(id_=sig_id)]
    llm = _StubLLMHandler()

    # Pass the LLM directly, not an InlineTargetDeps bundle.
    result = await run_method(inputs, {"target_id": "x"}, llm)
    assert result.finding.confidence == 0.85
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_inline_target_runner_call_shape():
    """InlineTargetRunner is the AnalystRunFn adapter the runtime keeps
    on _AnalystDeps.run_method.  It accepts (inputs, options) and
    returns an AnalystMethodResult — same shape as the spike's
    LLMAnalystRunner.__call__."""
    sig_id = uuid4()
    inputs = [_signal_row(id_=sig_id)]
    llm = _StubLLMHandler()
    runner = InlineTargetRunner(llm, max_tokens=512, temperature=0.4)

    result = await runner(inputs, {"target_id": "x"})
    assert isinstance(result, AnalystMethodResult)
    # The runner forwarded its config to the LLM call.
    assert llm.calls[0]["max_tokens"] == 512
    assert llm.calls[0]["temperature"] == 0.4


# ---------------------------------------------------------------------------
# Compat shim — legba.runtime.analyst_method still exposes the old names
# ---------------------------------------------------------------------------


def test_compat_shim_reexports():
    """The old ``legba.runtime.analyst_method`` shim re-exports
    LLMAnalystRunner (aliased to InlineTargetRunner), AnalystMethodResult,
    and LLMHandlerLike, so the spike's import sites keep working."""
    from legba.runtime import analyst_method

    assert analyst_method.LLMAnalystRunner is InlineTargetRunner
    assert analyst_method.AnalystMethodResult is AnalystMethodResult
    # LLMHandlerLike re-export — same protocol surface.
    from legba.data.analysts.inline_target import LLMHandlerLike as _LL
    assert analyst_method.LLMHandlerLike is _LL


# ---------------------------------------------------------------------------
# DSPy module compile (skip if dspy not installed)
# ---------------------------------------------------------------------------


def test_dspy_module_compiles():
    """The prompt module's DSPy class instantiates cleanly + carries
    the expected signature shape.

    Skipped when dspy isn't installed — the kind module still works
    via the direct chat_complete path; dspy is only required at
    optimizer (L-176) compile time and at runtime when the descriptor
    pins the prompt module path.
    """
    pytest.importorskip("dspy")

    from legba.prompts.inline_target.v1 import (
        InlineTargetCycle,
        ReasonSignature,
    )

    # Signature carries the typed in/out fields per L-105 §2.2.
    fields = ReasonSignature.model_fields
    assert "target_id" in fields
    assert "signals_block" in fields
    assert "rationale" in fields
    assert "title" in fields
    assert "body" in fields
    assert "confidence" in fields
    assert "evidence" in fields
    assert "tags" in fields

    # Module instantiates without an LM bound — instantiation is a
    # registration-time check; LM binding happens at forward() time.
    module = InlineTargetCycle()
    assert hasattr(module, "reason")

    # The kind handler's build_prompt_module() also returns a module.
    built = build_prompt_module()
    assert isinstance(built, InlineTargetCycle)


def test_build_prompt_module_raises_without_dspy():
    """If dspy is genuinely missing, build_prompt_module raises
    ModuleNotFoundError so the caller can fall back to the direct
    LLM-call path.  Skipped when dspy IS installed (the negative case
    isn't reachable then)."""
    try:
        import dspy  # noqa: F401
    except ModuleNotFoundError:
        with pytest.raises(ModuleNotFoundError):
            build_prompt_module()
    else:
        pytest.skip("dspy is installed; negative case not reachable here")


# ---------------------------------------------------------------------------
# S5 — agentic GATHER phase
# ---------------------------------------------------------------------------


class _ScriptedGatherLLM:
    """LLM double that emits a scripted sequence: GATHER turns first (tool
    calls / done), then the final synthesis JSON.

    The GATHER loop and the one-shot synthesis both call ``chat_complete``;
    this double pops scripted responses in order so a test can drive an exact
    GATHER → synthesis sequence and assert the gathered context reached the
    synthesis prompt.
    """

    subprovider = "openai"

    def __init__(self, scripted: list[str]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": messages, "system": system})
        content = self._scripted.pop(0) if self._scripted else '{"done": true}'
        return _Response(content=content, usage=_Usage())


class _FakeBinding:
    """Stand-in for the per-run AgencyToolBinding the actor injects.

    ``run_tool`` records the call and returns a canned admitted/blocked
    AgencyOutcome so the kind's governed-dispatch branch is exercised without
    a live agency plane.
    """

    def __init__(self, *, admit: bool = True, output: dict[str, Any] | None = None) -> None:
        self.admit = admit
        self.output = output or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> AgencyOutcome:
        self.calls.append((tool_name, dict(args)))
        if not self.admit:
            return AgencyOutcome(
                admitted=False,
                pack_id="substrate_read",
                tool_name=tool_name,
                block_cause="not_allowed",
                detail="target does not allow substrate_read",
            )
        return AgencyOutcome(
            admitted=True,
            pack_id="substrate_read",
            tool_name=tool_name,
            tool_result=ToolResult(status="completed", output=dict(self.output)),
        )


@pytest.mark.asyncio
async def test_gather_no_binding_is_single_shot():
    """Without a binding, run_method is byte-for-byte the legacy single-shot
    path — exactly ONE LLM call, no `gather` trace step (NO-OP back-compat)."""
    inputs = [_signal_row(id_=uuid4())]
    llm = _StubLLMHandler()
    deps = InlineTargetDeps(llm=llm)  # no agency_binding

    result = await run_method(inputs, {"target_id": "india_energy_infra"}, deps)

    assert len(llm.calls) == 1
    phases = [s["phase"] for s in result.intermediate_steps]
    assert "gather" not in phases
    assert result.finding.confidence == 0.85


@pytest.mark.asyncio
async def test_gather_engages_via_options_binding():
    """With a per-run binding in options, GATHER runs: a tool call is
    dispatched through the binding, its ref extends derived_from, and the
    gathered context reaches the synthesis prompt."""
    sig_id = uuid4()
    ref_id = uuid4()
    inputs = [_signal_row(id_=sig_id)]
    final_json = (
        '{"title": "Grounded finding", "body": "b", "confidence": 0.7, '
        '"evidence": [], "tags": ["energy"]}'
    )
    llm = _ScriptedGatherLLM([
        '{"tool": "query_facts", "args": {"subject": "Petrobras"}}',
        final_json,
    ])
    binding = _FakeBinding(output={"refs": [str(ref_id)], "rows": [{"x": 1}]})
    deps = InlineTargetDeps(llm=llm)  # default max_rounds=1 → one tool round

    result = await run_method(
        inputs,
        {"target_id": "india_energy_infra", "agency_binding": binding},
        deps,
    )

    # The tool was dispatched through the governed binding.
    assert binding.calls == [("query_facts", {"subject": "Petrobras"})]
    # Default single round: 1 GATHER tool round + 1 synthesis = 2 LLM calls.
    assert len(llm.calls) == 2
    # The gathered ref folded into the finding's lineage alongside the signal.
    assert ref_id in result.derived_from
    assert sig_id in result.derived_from
    # The synthesis prompt carried the SUBSTRATE INVESTIGATION preamble.
    synth_prompt = llm.calls[-1]["messages"][0]["content"]
    assert "SUBSTRATE INVESTIGATION" in synth_prompt
    # Trace recorded the gather phase.
    phases = [s["phase"] for s in result.intermediate_steps]
    assert "gather" in phases
    assert result.finding.title == "Grounded finding"


@pytest.mark.asyncio
async def test_gather_blocked_tool_degrades_not_drops():
    """A blocked tool call (target doesn't allow the pack) folds the block
    back into the loop and the run still lands a finding (degrade-not-drop)."""
    inputs = [_signal_row(id_=uuid4())]
    final_json = (
        '{"title": "Still landed", "body": "b", "confidence": 0.6, '
        '"evidence": [], "tags": []}'
    )
    # Two rounds so the blocked round is followed by a `done`, then synthesis.
    llm = _ScriptedGatherLLM([
        '{"tool": "query_facts", "args": {"subject": "X"}}',
        '{"done": true}',
        final_json,
    ])
    binding = _FakeBinding(admit=False)
    deps = InlineTargetDeps(llm=llm, max_rounds=2)

    result = await run_method(
        inputs,
        {"target_id": "t", "agency_binding": binding},
        deps,
    )

    assert result.finding.title == "Still landed"
    # The blocked tool call was recorded as a non-ok gather step.
    gather_steps = [s for s in result.intermediate_steps if s["phase"] == "gather"]
    assert any(s.get("kind") == "tool_call" and s.get("ok") is False for s in gather_steps)


@pytest.mark.asyncio
async def test_gather_unparseable_persists_raw():
    """An unparseable GATHER turn still degrades-not-drops (breaks to synthesis)
    but records a distinct ``unparseable`` step carrying the RAW reply, so a
    malformed gather leaves a debuggable trail instead of being silently
    conflated with a clean ``done``. Shared by journal_assessor's GATHER."""
    inputs = [_signal_row(id_=uuid4())]
    final_json = (
        '{"title": "Landed", "body": "b", "confidence": 0.6, '
        '"evidence": [], "tags": []}'
    )
    garbage = "the model rambled instead of emitting a tool call"
    llm = _ScriptedGatherLLM([garbage, final_json])
    binding = _FakeBinding()
    deps = InlineTargetDeps(llm=llm)  # default max_rounds=1 → one gather round

    result = await run_method(
        inputs,
        {"target_id": "t", "agency_binding": binding},
        deps,
    )

    assert result.finding.title == "Landed"
    # No tool dispatched — garbage is not a tool call.
    assert binding.calls == []
    gather_steps = [s for s in result.intermediate_steps if s["phase"] == "gather"]
    unparse = [s for s in gather_steps if s.get("kind") == "unparseable"]
    assert unparse, "expected an unparseable gather step"
    assert unparse[0]["raw"] == garbage


@pytest.mark.asyncio
async def test_gather_budget_precheck_skips_rounds():
    """When the budget precheck reports no headroom, GATHER is SKIPPED (not the
    finding) — the run is single-shot and records a skipped_budget step."""
    inputs = [_signal_row(id_=uuid4())]

    async def _no_headroom() -> bool:
        return False

    binding = _FakeBinding()
    llm = _StubLLMHandler()
    deps = InlineTargetDeps(llm=llm, budget_precheck=_no_headroom)

    result = await run_method(
        inputs,
        {"target_id": "t", "agency_binding": binding},
        deps,
    )

    # No tool call dispatched; exactly one (synthesis) LLM call.
    assert binding.calls == []
    assert len(llm.calls) == 1
    gather_steps = [s for s in result.intermediate_steps if s["phase"] == "gather"]
    assert gather_steps and gather_steps[0]["kind"] == "skipped_budget"


@pytest.mark.asyncio
async def test_gather_usage_folds_into_total():
    """GATHER rounds' tokens fold into the run's usage so the actor's post-run
    budget.record charges them against the same per-day cap."""
    inputs = [_signal_row(id_=uuid4())]
    final_json = (
        '{"title": "t", "body": "b", "confidence": 0.5, "evidence": [], "tags": []}'
    )
    # Single GATHER tool round + synthesis = 2 calls × (100 prompt + 50 compl).
    llm = _ScriptedGatherLLM([
        '{"tool": "search_signals", "args": {"query": "energy"}}',
        final_json,
    ])
    binding = _FakeBinding(output={"refs": []})
    deps = InlineTargetDeps(llm=llm, max_rounds=1)

    result = await run_method(
        inputs, {"target_id": "t", "agency_binding": binding}, deps,
    )

    # 2 LLM calls total (1 GATHER + 1 synthesis), each 100/50.
    assert len(llm.calls) == 2
    assert result.usage["prompt_tokens"] == 200
    assert result.usage["completion_tokens"] == 100


# ---------------------------------------------------------------------------
# SEAM #22 — GATHER actuation of the web_access / propose_facts write tools
# ---------------------------------------------------------------------------


class _FakePackBinding:
    """A per-pack AgencyToolBinding double for the multi-pack GATHER router.

    Like ``_FakeBinding`` but pinned to a ``pack_id`` so a test can assert that
    a write/web tool routed to ITS pack's binding (not the substrate_read one).
    Records every call; returns a canned admitted/blocked AgencyOutcome.
    """

    def __init__(
        self,
        *,
        pack_id: str,
        admit: bool = True,
        output: dict[str, Any] | None = None,
        status: str = "completed",
    ) -> None:
        self.pack_id = pack_id
        self.admit = admit
        self.output = output or {}
        self.status = status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> AgencyOutcome:
        self.calls.append((tool_name, dict(args)))
        if not self.admit:
            return AgencyOutcome(
                admitted=False, pack_id=self.pack_id, tool_name=tool_name,
                block_cause="not_allowed",
                detail=f"target does not allow {self.pack_id}",
            )
        return AgencyOutcome(
            admitted=True, pack_id=self.pack_id, tool_name=tool_name,
            tool_result=ToolResult(status=self.status, output=dict(self.output)),
        )


@pytest.mark.asyncio
async def test_gather_routes_propose_fact_to_write_binding():
    """A GATHER round naming propose_fact routes to the propose_facts binding
    (NOT the substrate_read one), and the proposed fact_id folds into lineage."""
    sig_id = uuid4()
    ref_id = uuid4()
    new_fact_id = uuid4()
    inputs = [_signal_row(id_=sig_id)]
    final_json = (
        '{"title": "Proposed", "body": "b", "confidence": 0.6, '
        '"evidence": [], "tags": []}'
    )
    llm = _ScriptedGatherLLM([
        # One write tool call, then synthesis.
        '{"tool": "propose_fact", "args": {"subject": "Zeta", '
        '"predicate": "controls", "value": "Port Theta", '
        f'"derived_from": ["{ref_id}"]}}}}',
        final_json,
    ])
    read_binding = _FakeBinding()  # substrate_read — must NOT receive the call
    write_binding = _FakePackBinding(
        pack_id="propose_facts",
        output={"fact_id": str(new_fact_id), "source_type": "proposed"},
    )
    deps = InlineTargetDeps(llm=llm, max_rounds=1)

    result = await run_method(
        inputs,
        {
            "target_id": "india_energy_infra",
            "agency_binding": read_binding,
            "gather_tool_bindings": {
                "propose_fact": write_binding,
                "request_source": write_binding,
                "open_question": write_binding,
            },
            "gather_write_prompt_fragments": [
                "Never propose a fact without derived_from lineage.",
            ],
        },
        deps,
    )

    # Routed to the WRITE pack's binding, never the read binding.
    assert write_binding.calls == [
        ("propose_fact", {
            "subject": "Zeta", "predicate": "controls",
            "value": "Port Theta", "derived_from": [str(ref_id)],
        })
    ]
    assert read_binding.calls == []
    # The proposed fact's returned id folded into the finding's lineage.
    assert new_fact_id in result.derived_from
    assert sig_id in result.derived_from
    # The write pack's operator-authored rule reached the GATHER system prompt.
    gather_system = llm.calls[0]["system"]
    assert "WRITE-BACK" in gather_system
    assert "derived_from lineage" in gather_system
    assert result.finding.title == "Proposed"


@pytest.mark.asyncio
async def test_gather_routes_web_fetch_to_web_binding():
    """A web_fetch GATHER call routes to the web_access binding; the read and
    write bindings are untouched. The web guidance is spliced into the suffix."""
    inputs = [_signal_row(id_=uuid4())]
    final_json = (
        '{"title": "t", "body": "b", "confidence": 0.5, "evidence": [], "tags": []}'
    )
    llm = _ScriptedGatherLLM([
        '{"tool": "web_fetch", "args": {"url": "https://example.org/a"}}',
        final_json,
    ])
    read_binding = _FakeBinding()
    web_binding = _FakePackBinding(
        pack_id="web_access", output={"url": "https://example.org/a", "body": "ok"})
    deps = InlineTargetDeps(llm=llm, max_rounds=1)

    result = await run_method(
        inputs,
        {
            "target_id": "t",
            "agency_binding": read_binding,
            "gather_tool_bindings": {
                "web_fetch": web_binding, "web_search": web_binding},
            "gather_web_prompt_fragments": ["Cite the URL you fetched."],
        },
        deps,
    )

    assert web_binding.calls == [("web_fetch", {"url": "https://example.org/a"})]
    assert read_binding.calls == []
    gather_system = llm.calls[0]["system"]
    assert "EXTERNAL EVIDENCE" in gather_system
    assert "Cite the URL you fetched." in gather_system
    assert result.finding.title == "t"


@pytest.mark.asyncio
async def test_gather_unbound_write_tool_is_loud_noop():
    """An un-GRANTED write pack means no per-tool binding: a propose_fact call
    is reported as 'tool_unbound' (folded back to the planner) and is NEVER
    dispatched through the substrate_read binding — the run still lands."""
    inputs = [_signal_row(id_=uuid4())]
    final_json = (
        '{"title": "Landed", "body": "b", "confidence": 0.5, '
        '"evidence": [], "tags": []}'
    )
    llm = _ScriptedGatherLLM([
        '{"tool": "propose_fact", "args": {"subject": "A", "predicate": "p", '
        '"value": "B", "derived_from": ["%s"]}}' % uuid4(),
        '{"done": true}',
        final_json,
    ])
    read_binding = _FakeBinding()
    deps = InlineTargetDeps(llm=llm, max_rounds=2)

    result = await run_method(
        inputs,
        # NO gather_tool_bindings → the write pack was not granted.
        {"target_id": "t", "agency_binding": read_binding},
        deps,
    )

    # The substrate_read binding NEVER saw the write tool (no ungoverned bypass).
    assert read_binding.calls == []
    gather_steps = [s for s in result.intermediate_steps if s["phase"] == "gather"]
    # The unbound call was recorded as a non-admitted, non-ok tool_call step.
    assert any(
        s.get("kind") == "tool_call" and s.get("tool") == "propose_fact"
        and s.get("admitted") is False and s.get("ok") is False
        for s in gather_steps
    )
    # Degrade-not-drop: the finding still landed.
    assert result.finding.title == "Landed"


@pytest.mark.asyncio
async def test_gather_blocked_write_tool_degrades_not_drops():
    """A write tool BLOCKED by the gate (target doesn't allow the write pack)
    folds the block back and the run still lands a finding."""
    inputs = [_signal_row(id_=uuid4())]
    final_json = (
        '{"title": "Still landed", "body": "b", "confidence": 0.5, '
        '"evidence": [], "tags": []}'
    )
    llm = _ScriptedGatherLLM([
        '{"tool": "open_question", "args": {"question": "Q?", '
        '"derived_from": ["%s"]}}' % uuid4(),
        '{"done": true}',
        final_json,
    ])
    read_binding = _FakeBinding()
    write_binding = _FakePackBinding(pack_id="propose_facts", admit=False)
    deps = InlineTargetDeps(llm=llm, max_rounds=2)

    result = await run_method(
        inputs,
        {
            "target_id": "t",
            "agency_binding": read_binding,
            "gather_tool_bindings": {"open_question": write_binding},
        },
        deps,
    )

    assert write_binding.calls and write_binding.calls[0][0] == "open_question"
    gather_steps = [s for s in result.intermediate_steps if s["phase"] == "gather"]
    assert any(
        s.get("kind") == "tool_call" and s.get("tool") == "open_question"
        and s.get("ok") is False
        for s in gather_steps
    )
    assert result.finding.title == "Still landed"


def test_gather_system_suffix_splice_is_copy_on_write():
    """``_gather_system_suffix`` composes the read suffix + optional sections
    WITHOUT mutating the module-level read-only suffix constant (so a write
    assessor's prompt can never leak into a read-only one)."""
    from legba.data.analysts.inline_target import (
        _GATHER_SYSTEM_SUFFIX,
        _gather_system_suffix,
    )

    read_only = _gather_system_suffix()
    both = _gather_system_suffix(
        web_fragments=["w-rule"], write_fragments=["x-rule"])

    # The read-only build equals the constant; the constant is unmutated.
    assert read_only == _GATHER_SYSTEM_SUFFIX
    assert "EXTERNAL EVIDENCE" not in _GATHER_SYSTEM_SUFFIX
    assert "WRITE-BACK" not in _GATHER_SYSTEM_SUFFIX
    # The spliced build is a strict superset carrying both sections + the rules.
    assert both.startswith(_GATHER_SYSTEM_SUFFIX)
    assert "EXTERNAL EVIDENCE" in both and "w-rule" in both
    assert "WRITE-BACK" in both and "x-rule" in both


@pytest.mark.asyncio
async def test_propose_fact_tool_lands_proposed_row_with_lineage():
    """The REAL propose_fact_tool (the handler the routed write binding calls)
    flows through write_fact and lands a source_type='proposed' row carrying the
    cited derived_from lineage — exercised against a recording write path so the
    unit test needs no live substrate. (The container e2e in
    tests/data_pkg/agency/test_web_and_propose_tools_e2e.py proves the SQL.)"""
    from legba.data.analysts.agency import write_tools as _wt
    from legba.data.analysts.agency.tools import (
        ToolCall as _TC,
        ToolContext as _Ctx,
        WritebackContext as _WB,
    )
    from legba.data.provenance import AnalystContext as _AC
    from legba.data.schemas.action_pack import ActionPack as _AP

    ref_id = uuid4()
    written: dict[str, Any] = {}

    class _FakeRow:
        def __init__(self, _id: UUID) -> None:
            self.id = _id

    async def _fake_write_fact(conn, *, analyst_ctx, payload, derived_from,
                               publish_fn=None, source_type=None):
        written["payload"] = payload
        written["derived_from"] = list(derived_from)
        written["source_type"] = source_type
        written["analyst_ctx"] = analyst_ctx
        return _FakeRow(uuid4()), None

    class _FakeConn:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    class _FakePool:
        def acquire(self):
            return _FakeConn()

    actx = _AC(
        analyst_id="analyst_x", analyst_version="v" * 16,
        run_id=uuid4(), target_id="t", target_version="t" * 16,
    )
    ctx = _Ctx(writeback=_WB(pg_pool=_FakePool(), analyst_ctx=actx))
    pack = _AP.model_validate(
        {
            "identity": {
                "id": "propose_facts", "name": "propose_facts",
                "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
                "state": "active", "owner": "s6_agency",
                "created": "2026-06-19T00:00:00Z",
            },
            "tools": [{"name": "propose_fact"}],
        },
        strict=False,
    )
    call = _TC(
        pack_id="propose_facts", tool_name="propose_fact",
        requested_by="analyst::analyst_x",
        args={
            "subject": "Zeta", "predicate": "controls", "value": "Port Theta",
            "confidence": 0.95,  # above the ceiling — must clamp
            "derived_from": [str(ref_id)],
        },
    )

    import unittest.mock as _mock
    with _mock.patch.object(_wt, "write_fact", _fake_write_fact):
        result = await _wt.propose_fact_tool(call, pack, ctx)

    assert result.status == "completed"
    assert result.output["source_type"] == "proposed"
    # Confidence clamped to the cautious ceiling.
    assert result.output["confidence"] <= 0.6
    # The write went through write_fact with proposed source_type + cited lineage.
    assert written["source_type"] == "proposed"
    assert written["derived_from"] == [ref_id]
    assert written["payload"]["source_type"] == "proposed"
    # Provenance stamped to the run.
    assert written["analyst_ctx"].run_id == actx.run_id


@pytest.mark.asyncio
async def test_propose_fact_tool_refuses_without_writeback():
    """No writeback surface wired → the handler returns a clean failure (never
    an un-stamped write). This is the seam's loud-refusal guard."""
    from legba.data.analysts.agency import write_tools as _wt
    from legba.data.analysts.agency.tools import (
        ToolCall as _TC, ToolContext as _Ctx,
    )
    from legba.data.schemas.action_pack import ActionPack as _AP

    pack = _AP.model_validate(
        {
            "identity": {
                "id": "propose_facts", "name": "propose_facts",
                "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
                "state": "active", "owner": "s6_agency",
                "created": "2026-06-19T00:00:00Z",
            },
            "tools": [{"name": "propose_fact"}],
        },
        strict=False,
    )
    call = _TC(
        pack_id="propose_facts", tool_name="propose_fact",
        args={"subject": "A", "predicate": "p", "value": "B",
              "derived_from": [str(uuid4())]},
    )
    # ctx.writeback is None.
    result = await _wt.propose_fact_tool(call, pack, _Ctx())
    assert result.status == "failed"
    assert "writeback" in (result.error or "")


# ---------------------------------------------------------------------------
# P0-T1 — cite the prose: map [N] markers -> signal ids, persist data['citations']
# ---------------------------------------------------------------------------


def test_build_citation_index_maps_position_to_signal_id():
    """The render-time index keys the 1-based slice position N to the signal id
    (the same N _render_signal stamps onto each [N] block), plus cheap fields."""
    ids = [uuid4() for _ in range(2)]
    sliced = [
        {"id": ids[0], "title": "First", "source_url": "https://a.example/1"},
        {"id": str(ids[1]), "title": "Second"},  # str id is parsed to UUID str
    ]
    index = _build_citation_index(sliced)
    assert index[1]["signal_id"] == str(ids[0])
    assert index[1]["title"] == "First"
    assert index[1]["source"] == "https://a.example/1"
    assert index[2]["signal_id"] == str(ids[1])
    # No source on row 2 → omitted (None).
    assert index[2]["source"] is None


def test_build_citation_index_indexes_unresolvable_id_as_none():
    """A row with no/bad id is still indexed (signal_id None) so a marker over it
    is counted as present-but-unmapped, never fabricated."""
    sliced = [{"title": "no id here"}, {"id": "not-a-uuid", "title": "bad id"}]
    index = _build_citation_index(sliced)
    assert index[1]["signal_id"] is None
    assert index[2]["signal_id"] is None


def test_extract_citations_resolves_markers_to_ids():
    """Over a known {N -> id} index, [N] markers in the prose resolve to citation
    entries; the prose is the source, the entries carry the ids alongside."""
    ids = [uuid4(), uuid4(), uuid4()]
    index = {
        1: {"signal_id": str(ids[0]), "title": "T1", "source": "https://x/1"},
        2: {"signal_id": str(ids[1]), "title": "T2", "source": None},
        3: {"signal_id": str(ids[2]), "title": None, "source": None},
    }
    body = (
        "## Key developments\n"
        "- Itaipu upgrade complete [1].\n"
        "- Wind record set [2]; reaffirmed again [2].\n"  # dup marker → one entry
        "- Petrobras Q1 published [3]."
    )
    citations, marker_count, resolved = _extract_citations(body, index)
    # Three DISTINCT markers, all resolved.
    assert marker_count == 3
    assert resolved == 3
    assert len(citations) == 3
    cited_ids = {c["signal_id"] for c in citations}
    assert cited_ids == {str(i) for i in ids}
    # First entry carries its cheap fields; markers preserved verbatim.
    first = citations[0]
    assert first["marker"] == "[1]"
    assert first["title"] == "T1"
    assert first["source"] == "https://x/1"


def test_extract_citations_counts_but_never_fabricates_unmapped_marker():
    """An out-of-range or unresolved marker is COUNTED but produces NO entry —
    no fabricated id."""
    sig_id = uuid4()
    index = {1: {"signal_id": str(sig_id), "title": "T", "source": None}}
    # [1] resolves; [7] is out of range; [2] maps to an index with no id.
    index[2] = {"signal_id": None, "title": "no id", "source": None}
    body = "Claim A [1]. Claim B [2]. Claim C [7]."
    citations, marker_count, resolved = _extract_citations(body, index)
    assert marker_count == 3            # all three distinct markers counted
    assert resolved == 1                # only [1] mapped to a real id
    assert [c["signal_id"] for c in citations] == [str(sig_id)]
    assert all(c["signal_id"] for c in citations)  # never a None/fabricated id


def test_normalize_citation_markers_rewrites_fullwidth_brackets():
    """REGRESSION (live 2026-06-30, energy_security unit): gpt-oss emitted CJK
    lenticular brackets ``【3】`` instead of ASCII ``[3]`` — the [N] parser missed
    them, so a correctly-cited finding resolved to ZERO citations (drill-to-source
    broke + faithfulness scored 0.00). Normalize variant brackets that wrap an
    integer to ASCII, then citations extract as if the model had used ``[N]``."""
    ids = [uuid4(), uuid4(), uuid4()]
    index = {
        1: {"signal_id": str(ids[0]), "title": "T1", "source": None},
        2: {"signal_id": str(ids[1]), "title": "T2", "source": None},
        3: {"signal_id": str(ids[2]), "title": "T3", "source": None},
    }
    # Mixed variant glyphs (lenticular 【】, fullwidth ［］), incl. inner spaces.
    raw = "Heat strain【1】. Grid stress ［2］. Rail incident 〔 3 〕."
    normalized = _normalize_citation_markers(raw)
    assert normalized == "Heat strain[1]. Grid stress [2]. Rail incident [3]."
    # Idempotent on already-ASCII prose.
    assert _normalize_citation_markers(normalized) == normalized
    # And the normalized body now resolves all three markers.
    citations, marker_count, resolved = _extract_citations(normalized, index)
    assert marker_count == 3
    assert resolved == 3
    assert {c["signal_id"] for c in citations} == {str(i) for i in ids}


def test_normalize_citation_markers_leaves_non_citation_glyphs_intact():
    """Only digit-wrapping bracket pairs are rewritten; lenticular brackets around
    NON-numeric prose (a legitimate stylistic use) are left untouched."""
    raw = "The directive 【emergency powers】 was issued [4]."
    # Worded lenticular bracket left intact; the ASCII [4] marker is unchanged.
    assert _normalize_citation_markers(raw) == raw
    assert "【emergency powers】" in _normalize_citation_markers(raw)


@pytest.mark.asyncio
async def test_run_method_persists_citations_in_finding_data():
    """ACCEPTANCE: over a FIXTURE synthesized response whose prose carries [1]
    and [2] against a KNOWN signal-id ordering, run_method persists
    data['citations'] — a non-empty list whose ids are all real fixture ids, with
    >=80% of the prose markers mapped — and the prose itself is untouched."""
    # KNOWN ordering: ORIENT sorts produced_at DESC, so [1] = newest = sig_ids[0].
    sig_ids = [uuid4() for _ in range(2)]
    inputs = [
        _signal_row(id_=sig_ids[0], produced_at="2026-05-19T14:00:00+00:00",
                    title="Itaipu hydro upgrade", source_url="https://ex/itaipu"),
        _signal_row(id_=sig_ids[1], produced_at="2026-05-18T09:30:00+00:00",
                    title="Wind capacity record", source_url="https://ex/wind"),
    ]
    body = (
        "## Key developments\n"
        "- Itaipu turbine upgrade completed [1].\n"
        "- Northeast wind capacity set a record [2]."
    )
    fixture = {
        "title": "Brazil energy: capacity gains",
        "body": body,
        "confidence": 0.8,
        "evidence": ["Itaipu upgrade", "Wind record"],
        "tags": ["energy"],
    }
    llm = _StubLLMHandler(content_override=json.dumps(fixture))
    deps = InlineTargetDeps(llm=llm)

    result = await run_method(
        inputs, {"target_id": "br_energy", "analyst_id": "analyst.br_energy"}, deps,
    )

    citations = result.finding.data.get("citations")
    assert isinstance(citations, list) and citations            # non-empty list
    fixture_ids = {str(i) for i in sig_ids}
    # Every cited signal_id is one of the fixture's signal ids.
    for c in citations:
        assert c["signal_id"] in fixture_ids
    # [1] -> newest signal, [2] -> next — verify the ordering mapping is right.
    by_marker = {c["marker"]: c["signal_id"] for c in citations}
    assert by_marker["[1]"] == str(sig_ids[0])
    assert by_marker["[2]"] == str(sig_ids[1])

    # >=80% of the [N] markers in the body map to a citation entry.
    import re as _re
    body_markers = {int(m) for m in _re.findall(r"\[(\d+)\]", result.finding.body)}
    mapped = {int(c["marker"].strip("[]")) for c in citations}
    assert len(mapped & body_markers) / len(body_markers) >= 0.8

    # The prose is UNTOUCHED — citations are added alongside, not replacing it.
    assert result.finding.body == body
    assert "[1]" in result.finding.body and "[2]" in result.finding.body

    # The reflect/coerce_finding trace step recorded the marker accounting
    # (folded in — no extra reflect step, so the 7-phase envelope is unchanged).
    cite_step = next(
        s for s in result.intermediate_steps
        if s.get("phase") == "reflect" and s.get("kind") == "coerce_finding"
    )
    assert cite_step["citation_markers"] == 2
    assert cite_step["citations_resolved"] == 2


@pytest.mark.asyncio
async def test_run_method_no_markers_leaves_no_citations_key():
    """A finding whose prose carries no [N] markers does NOT add a citations key
    (no empty-list noise) — back-compat for non-citing synthesis."""
    inputs = [_signal_row(id_=uuid4())]
    fixture = {
        "title": "t", "body": "Prose with no citation markers at all.",
        "confidence": 0.5, "evidence": [], "tags": [],
    }
    llm = _StubLLMHandler(content_override=json.dumps(fixture))
    result = await run_method(inputs, {"target_id": "t"}, InlineTargetDeps(llm=llm))
    assert "citations" not in result.finding.data
