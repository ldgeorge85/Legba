# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The five consult sessions of 2026-09-28 as the divergence fixture and the
admission test for a tool-using role (W1 F-10).

One question, run five times on 2026-09-28 against the live consult loop —
Opus (``181a8bab``), the core model BEFORE the fixes (``96d7bfc2``), and three
core-model proof runs AFTER them (``6dd39dfd``, ``2d026feb``, ``5d66ccbb``). The
recorded rows live under ``fixtures/consult_divergence/2026-09-28/`` exactly as
exported; ``docs/history/CONSULT_DIVERGENCE_2026-09-28.md`` reads them side by
side. They are the model-divergence layer's first case, and this test is the
admission bar any model must clear for a tool-using role: **every tool call it
writes must pass** ``validate_args`` against ``TOOL_SCHEMAS`` — except that a
call written into the arguments of a tool that has not run (a tool RESULT where
the INPUT belongs) must be refused, and refused under its own name,
``result_shaped_arg``.

Two recorded calls are such fabrications:

* ``96d7bfc2`` call 0 — ``list_situations`` with ``result`` / ``status: "ok"`` /
  ``metadata`` written into its arguments. Pre-fix it RAN and returned count 0.
* ``2d026feb`` call 0 — the same shape, ``result`` / ``status: "success"`` /
  ``cited_refs``. Post-fix the live loop BLOCKED it (the recorded result is the
  ``tool_blocked: result_shaped_arg`` message) and the model recovered on its
  next round. The brief for this fixture said every post-fix call must pass;
  the recording says one post-fix call was a fabrication the guard caught, so
  the test pins it as a block, which is the guard working, not a regression.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from legba.data.stack.llm.tool_rounds import (
    CAUSE_RESULT_SHAPED_ARG,
    TOOL_SCHEMAS,
    split_arg_problem,
    validate_args,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "consult_divergence" / "2026-09-28"

#: session prefix -> (who wrote it, the call indexes that are fabrications).
_SESSIONS: dict[str, tuple[str, frozenset[int]]] = {
    "181a8bab": ("opus", frozenset()),
    "96d7bfc2": ("core_before_fixes", frozenset({0})),
    "6dd39dfd": ("core_after_fixes_1", frozenset()),
    "2d026feb": ("core_after_fixes_2", frozenset({0})),
    "5d66ccbb": ("core_after_fixes_3", frozenset()),
}


def _load(prefix: str) -> list[dict]:
    label = _SESSIONS[prefix][0]
    return json.loads((_FIXTURES / f"{prefix}_{label}.json").read_text())


def _assistant(prefix: str) -> dict:
    (row,) = [r for r in _load(prefix) if r["role"] == "assistant"]
    return row


def _replay(prefix: str) -> list[tuple[int, str, str | None]]:
    """(index, tool, validate_args verdict) for every recorded call."""
    return [
        (i, c["tool"], validate_args(TOOL_SCHEMAS.get(c["tool"]), c["args"]))
        for i, c in enumerate(_assistant(prefix)["tool_calls"])
    ]


def test_the_five_sessions_are_all_on_disk():
    on_disk = sorted(p.name for p in _FIXTURES.glob("*.json"))
    assert on_disk == sorted(f"{p}_{label}.json" for p, (label, _) in _SESSIONS.items())


def test_the_fixture_is_one_question_asked_five_times():
    """The user turn is the question; the post-fix runs carry a shorter
    wording than the first two (245 chars against 260) so compare the opening,
    not the whole string."""
    questions = {
        p: next(r for r in _load(p) if r["role"] == "user")["content"]
        for p in _SESSIONS
    }
    assert len(questions) == 5
    for p, q in questions.items():
        assert "larger arc" in q and "geopolitics" in q, p
    for p in _SESSIONS:
        assert _assistant(p)["synthesis_status"] == "complete"


@pytest.mark.parametrize("prefix", sorted(_SESSIONS))
def test_every_recorded_call_passes_or_is_a_named_fabrication(prefix):
    _, fabricated = _SESSIONS[prefix]
    calls = _assistant(prefix)["tool_calls"]
    assert calls, "a session with no recorded tool call proves nothing"
    for i, tool, problem in _replay(prefix):
        if i in fabricated:
            assert problem is not None, (
                f"{prefix} call {i} ({tool}) is a fabricated result and must "
                "be refused"
            )
            cause, _detail = split_arg_problem(problem)
            assert cause == CAUSE_RESULT_SHAPED_ARG, (prefix, i, problem)
        else:
            assert problem is None, (
                f"{prefix} call {i} ({tool}) {calls[i]['args']!r} was refused "
                f"by the admission check: {problem}"
            )


def test_the_pre_fix_fabrication_blocks_as_result_shaped_arg():
    """96d7bfc2 call 0: the tool had not run, and the model wrote its result
    into the call. It RAN at the time (count 0, refs 0) — that is the defect."""
    call = _assistant("96d7bfc2")["tool_calls"][0]
    assert call["tool"] == "list_situations"
    assert {"result", "metadata"} <= set(call["args"])
    assert call["result"] == {"refs": 0, "count": 0}
    problem = validate_args(TOOL_SCHEMAS["list_situations"], call["args"])
    assert problem is not None
    assert split_arg_problem(problem)[0] == CAUSE_RESULT_SHAPED_ARG


def test_the_post_fix_fabrication_was_blocked_live_for_the_same_cause():
    """2d026feb call 0 is recorded as blocked by the live guard; replaying it
    names the same cause the loop recorded, and the very next call passes."""
    row = _assistant("2d026feb")
    first, retry = row["tool_calls"][0], row["tool_calls"][1]
    assert isinstance(first["result"], str)
    assert first["result"].startswith(f"tool_blocked: {CAUSE_RESULT_SHAPED_ARG}")
    problem = validate_args(TOOL_SCHEMAS[first["tool"]], first["args"])
    assert problem is not None
    assert split_arg_problem(problem)[0] == CAUSE_RESULT_SHAPED_ARG
    assert retry["tool"] == "list_situations" and retry["args"] == {"limit": 20}
    assert validate_args(TOOL_SCHEMAS[retry["tool"]], retry["args"]) is None
    blocked_steps = [
        s for s in row["steps"] if s["kind"] == "tool_call" and not s["ok"]
    ]
    assert len(blocked_steps) == 1


def test_only_the_recorded_blocks_are_blocked_across_all_sessions():
    """The whole fixture in one number: 2 refusals in 45 recorded calls."""
    total = blocked = 0
    for p in _SESSIONS:
        for _i, _tool, problem in _replay(p):
            total += 1
            blocked += problem is not None
    assert (total, blocked) == (45, 2)


def test_no_secret_shaped_string_is_in_the_fixtures():
    shapes = (
        r"bearer\s+\S+", r"api[_-]?key", r"password", r"secret",
        r"\bsk-[a-z0-9_-]{12,}", r"\beyj[a-z0-9_-]{10,}", r"https?://\S+",
    )
    for path in sorted(_FIXTURES.glob("*.json")):
        text = path.read_text().lower()
        for shape in shapes:
            assert re.search(shape, text) is None, f"{path.name}: {shape}"
