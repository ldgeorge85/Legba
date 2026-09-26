# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane narrative (Program 7 piece 7e) — the ``narrative_coordination`` unit's
deterministic SPREAD BLOCK.

Covers the brief's five assertions:

  1. a synthetic slice with one sentence reused across 3 distinct sources
     inside a 2h window reports exactly ONE framing, 3 sources, < 2h spread;
  2. a slice with no cross-source reuse reports NO framings;
  3. the rendered header (``_render_user_prompt``) actually contains the
     block's text;
  4. ``analyst_narrative_coordination.yaml`` still validates against the real
     ``AnalystDescriptor`` schema (the exact bringup ``_load`` path) with the
     new ``method.options.spread_block`` knob and the extended prompt;
  5. byte-identity: engaging an EMPTY spread block changes the header by
     EXACTLY the block's own (multi-line) "none found" text — nothing else
     moves.

Plus two cheap regression guards: a same-source repeat never manufactures a
framing (the wire-pair-collapse precision guard, generalized), and the S1-T8
/ Phase-V D8a invariant (the prompt never claims a per-signal source-class
field) still holds now that the block DOES carry one.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any, Mapping

import pytest
import yaml

from legba.data.analysts.inline_target import InlineTargetDeps, run_method
from legba.data.analysts.slice_render import _render_user_prompt
from legba.data.analysts.spread_block import (
    SpreadBlockResult,
    build_spread_block,
)
from legba.data.schemas.analyst import AnalystDescriptor

_DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parents[2] / "descriptors"

#: A sentence long enough (>= _MIN_SENTENCE_TOKENS) to key, reused verbatim
#: (aside from an innocuous trailing clause difference) across three rows.
_REUSED_SENTENCE = (
    "Officials in the capital announced a sweeping new set of measures today."
)


def _row(
    *,
    title: str,
    source_url: str,
    produced_at: str,
    summary: str,
    source_id: str | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {"published_at": produced_at, "summary": summary}
    row: dict[str, object] = {
        "title": title,
        "source_url": source_url,
        "produced_at": produced_at,
        "data": data,
    }
    if source_id is not None:
        row["source_id"] = source_id
    return row


def _reused_slice() -> list[dict[str, object]]:
    """Three DISTINCT-masthead rows carrying the same sentence, 1.5h apart."""
    return [
        _row(
            title="Capital sees new measures",
            source_url="https://a-example.com/1",
            produced_at="2026-08-14T08:00:00+00:00",
            summary=f"{_REUSED_SENTENCE} Reaction was immediate.",
            source_id="source.a_example",
        ),
        _row(
            title="Government unveils package",
            source_url="https://b-example.org/2",
            produced_at="2026-08-14T09:00:00+00:00",
            summary=f"{_REUSED_SENTENCE} Analysts were divided.",
            source_id="source.b_example",
        ),
        _row(
            title="New measures announced",
            source_url="https://c-example.net/3",
            produced_at="2026-08-14T09:30:00+00:00",
            summary=f"{_REUSED_SENTENCE} The opposition condemned it.",
            source_id="source.c_example",
        ),
    ]


def _organic_slice() -> list[dict[str, object]]:
    """Three rows with genuinely distinct wording — no reuse anywhere."""
    return [
        _row(
            title="A port strike begins",
            source_url="https://a-example.com/1",
            produced_at="2026-08-14T08:00:00+00:00",
            summary="Dockworkers walked off the job over a pay dispute this morning.",
        ),
        _row(
            title="Central bank holds rates",
            source_url="https://b-example.org/2",
            produced_at="2026-08-14T09:00:00+00:00",
            summary="The monetary authority left its benchmark rate unchanged today.",
        ),
        _row(
            title="Border crossing reopens",
            source_url="https://c-example.net/3",
            produced_at="2026-08-14T09:30:00+00:00",
            summary="Traffic resumed at the crossing after a week-long closure.",
        ),
    ]


# ---------------------------------------------------------------------------
# 1/2. build_spread_block — reuse vs organic
# ---------------------------------------------------------------------------


def test_reused_sentence_across_three_sources_is_one_framing_under_two_hours():
    result = build_spread_block(_reused_slice())
    assert len(result.framings) == 1
    framing = result.framings[0]
    assert framing.source_count == 3
    assert framing.ordinals == (1, 2, 3)
    assert framing.hours_spread < 2.0
    assert result.max_sources == 3
    assert result.min_hours == pytest.approx(framing.hours_spread)


def test_organic_slice_reports_no_framings():
    result = build_spread_block(_organic_slice())
    assert result.framings == ()
    assert result.max_sources == 0
    assert result.min_hours is None


def test_same_masthead_repeat_never_manufactures_a_framing():
    """The wire-pair-collapse precision guard, generalized: a SINGLE source
    repeating its own sentence across two rows is not spread — it takes >= 2
    DISTINCT mastheads for a framing to exist at all."""
    rows = [
        _row(
            title="First run",
            source_url="https://a-example.com/1",
            produced_at="2026-08-14T08:00:00+00:00",
            summary=_REUSED_SENTENCE,
        ),
        _row(
            title="Correction",
            source_url="https://a-example.com/2",
            produced_at="2026-08-14T08:30:00+00:00",
            summary=_REUSED_SENTENCE,
        ),
    ]
    result = build_spread_block(rows)
    assert result.framings == ()


def test_class_mix_resolves_from_the_supplied_map_and_degrades_to_unknown():
    slice_rows = _reused_slice()
    result = build_spread_block(
        slice_rows,
        source_class_by_source_id={
            "source.a_example": "state_media",
            "source.b_example": "state_media",
            # source.c_example intentionally absent -> "unknown"
        },
    )
    assert result.framings[0].class_mix == ("state_media", "unknown")

    # No map at all -> every source "unknown", never fabricated.
    result_no_map = build_spread_block(slice_rows)
    assert result_no_map.framings[0].class_mix == ("unknown",)


# ---------------------------------------------------------------------------
# 3. The rendered header actually carries the block
# ---------------------------------------------------------------------------


def test_rendered_header_contains_the_spread_block():
    slice_rows = _reused_slice()
    result = build_spread_block(slice_rows)
    prompt = _render_user_prompt(slice_rows, "narrative_test_target", spread_block=result.text)
    assert result.text in prompt
    assert "[1][2][3]" in prompt
    assert "SPREAD BLOCK" in prompt


# ---------------------------------------------------------------------------
# 4. The descriptor still validates
# ---------------------------------------------------------------------------


def test_narrative_coordination_descriptor_still_validates():
    """Exact mirror of scripts/bringup_register_analysts._load (same path
    tests/data_pkg/test_p2_units.py exercises for all four P2-T2 units)."""
    body = yaml.safe_load(
        (_DESCRIPTORS_DIR / "analyst_narrative_coordination.yaml").read_text()
    )
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == "narrative_coordination"
    assert desc.method.options.get("spread_block") is True
    prompt = desc.method.system_prompt or ""
    assert "SPREAD BLOCK" in prompt
    assert "[N]" in prompt


def test_narrative_coordination_prompt_still_carries_no_per_signal_source_class():
    """S1-T8 / Phase-V D8a is still honoured for the PER-SIGNAL render: the
    prompt states the class mix is a block-level exception, never claims a
    per-signal field, and never spells the literal ``source_class`` token
    (test_source_class_taxonomy.py holds the render side of this contract)."""
    body = yaml.safe_load(
        (_DESCRIPTORS_DIR / "analyst_narrative_coordination.yaml").read_text()
    )
    prompt = body["method"]["system_prompt"]
    lowered = prompt.lower()
    assert "source_class" not in lowered
    assert "source=" in lowered
    assert "outlet identity" in lowered


# ---------------------------------------------------------------------------
# 5. Byte-identity: an EMPTY spread block changes the header by exactly its
#    own lines, nothing else.
# ---------------------------------------------------------------------------


def test_empty_spread_block_changes_the_header_by_exactly_its_own_lines():
    slice_rows = _organic_slice()
    empty_result: SpreadBlockResult = build_spread_block(slice_rows)
    assert empty_result.framings == ()

    baseline = _render_user_prompt(slice_rows, "narrative_test_target")
    engaged = _render_user_prompt(
        slice_rows, "narrative_test_target", spread_block=empty_result.text,
    )

    header_base, body_base = baseline.split("\n\n", 1)
    header_engaged, body_engaged = engaged.split("\n\n", 1)

    # The signal body block is completely untouched.
    assert body_engaged == body_base
    # The header differs by EXACTLY one appended line — the block's own
    # honest "none found" text — never a silently omitted block.
    assert header_engaged == header_base + "\n" + empty_result.text
    assert "no near-verbatim cross-source reuse detected" in header_engaged


def test_absent_spread_block_param_is_byte_identical_to_pre_lane_render():
    """The default (``spread_block=None``) contract every OTHER inline_target
    unit relies on: adding the parameter changed nothing for a caller that
    doesn't pass it."""
    slice_rows = _organic_slice()
    rendered = _render_user_prompt(slice_rows, "narrative_test_target")
    assert "SPREAD BLOCK" not in rendered


# ---------------------------------------------------------------------------
# End to end through run_method — the real X-1 option-catalog binding path,
# mirroring tests/data_pkg/test_wire_pair_collapse.py's own run_method proof.
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


class _CapturingLLM:
    """Records the rendered user prompt so a test can assert on what the desk saw."""

    subprovider = "openai"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def last_user_prompt(self) -> str:
        return str(self.calls[-1]["messages"][-1]["content"])

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
        return _Response(
            content=json.dumps({
                "title": "Test finding",
                "body": "Organic coverage [1].",
                "confidence": 0.3,
                "evidence": [1],
                "tags": ["severity:low"],
            }),
            usage=_Usage(),
        )


@pytest.mark.asyncio
async def test_run_method_engages_the_spread_block_only_when_the_descriptor_opts_in():
    """The real X-1 gate: ``options["spread_block"]`` is what
    ``_merge_descriptor_options`` would have set from
    ``method.options.spread_block`` on the live descriptor. ``deps.pg`` is
    unset (no substrate in this test), so class mix degrades to "unknown" —
    the run still completes and the block still renders."""
    llm = _CapturingLLM()
    rows = _reused_slice()

    result = await run_method(
        rows,
        {
            "target_id": "country_g20_au",
            "analyst_id": "narrative_coordination",
            "spread_block": True,
        },
        InlineTargetDeps(llm=llm),
    )

    prompt = llm.last_user_prompt
    assert "SPREAD BLOCK" in prompt
    assert "[1][2][3]" in prompt
    assert "class mix: unknown" in prompt

    spread_step = next(
        s for s in result.intermediate_steps if s.get("kind") == "spread_block"
    )
    assert spread_step["spread_framings"] == 1
    assert spread_step["spread_max_sources"] == 3
    assert spread_step["spread_min_hours"] < 2.0


@pytest.mark.asyncio
async def test_run_method_leaves_every_other_unit_unaffected():
    """No ``spread_block`` option (every OTHER inline_target descriptor's
    shape) -> no block in the prompt, no receipt step. Byte-for-byte
    unchanged, same contract task #57 held for the wire-pair collapse."""
    llm = _CapturingLLM()
    rows = _reused_slice()

    result = await run_method(
        rows,
        {"target_id": "country_g20_au", "analyst_id": "escalation"},
        InlineTargetDeps(llm=llm),
    )

    assert "SPREAD BLOCK" not in llm.last_user_prompt
    assert not any(
        s.get("kind") == "spread_block" for s in result.intermediate_steps
    )
