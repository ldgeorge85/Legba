# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The V-B absence screen reads a signal's BODY, not only its title (2026-08-21).

THE DEFECT. ``load_absence_slice_rows`` projected ``'' AS body`` for the signal
leg, so ``SliceRow.text`` — the surface stage 1 screens and stage 2 is SHOWN —
was a signal's TITLE and nothing else. The evidence a negative has to survive is
usually in the article, not the headline, so the platform's own absence backstop
was reading the one surface the evidence is not on.

THE MEASUREMENT it comes from (`planning/SALIENCE_DESIGN_2026-08-21.md` §1.2, the
`pro_ir_0817` case): the decisive proliferation row sat BODY-ONLY at slice
position [76]; the claim's content terms — "delivery", "enrichment",
"weaponization", "proliferation" — matched ZERO of the 120 slice titles, so the
screen returned candidates that did not include it. §1.4's control makes it
renderer-side rather than model-side: `int_ar_0807`, whose evidence is
title-visible at [1]-[4], was surfaced 2/2 by the same fleet at the same
temperature.

Why this test and not a replay: the producer-side fix for the class measured
NO-GO the same night (the model cites the pointer and denies anyway), so the
verify side is the live backstop. Its proof is a regression test on the
mechanism, not another prompt A/B.

WHAT IS AND IS NOT COVERED, stated because a fake connection cannot run SQL. The
Python screen surface is exercised through the REAL pipeline entry
(``verify_finding_faithfulness`` with a slice connection). The SQL PROJECTION —
the actual site of the defect — cannot be executed here, so it is pinned by
asserting the emitted query text: it must project a real body for signals and
must NOT contain the ``'' AS body`` literal that was the bug. That is a weaker
guard than execution and is labelled as such; it is exactly strong enough to
catch the regression it exists to catch.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

import pytest

from legba.data.provenance.absence_slice import (
    _ABSENCE_SLICE_BODY_CHARS,
    load_absence_slice_rows,
)
from legba.data.provenance.verify import verify_finding_faithfulness

_NUMBERED_CLAIM_RE = re.compile(r"^\s*\d+\.\s", re.MULTILINE)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _SliceJudge:
    """Answers the V-B stage-2 slice call; passes everything else."""

    subprovider = "stub"

    def __init__(self, slice_json: dict | None = None) -> None:
        self._slice = slice_json
        self.slice_calls = 0
        self.slice_prompts: list[str] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kw):
        prompt = messages[0]["content"]
        if "INPUT-SLICE" in (system or "") or "slice" in (system or "").lower():
            self.slice_calls += 1
            self.slice_prompts.append(prompt)
            return _Response(json.dumps(self._slice or {}))
        n = len(_NUMBERED_CLAIM_RE.findall(prompt))
        return _Response(json.dumps({"verdicts": ["supported"] * max(n, 1)}))


class _FakeConn:
    """asyncpg-shaped double. Returns the projection's OWN column names, so a
    row here is the shape the real query yields — the double cannot silently
    disagree with the SQL about what a row looks like."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.sql: str = ""

    async def fetchrow(self, sql: str, *args):
        if "analyst_traces" in sql:
            return {"input_row_refs": [uuid4() for _ in self._rows]}
        return None

    async def fetch(self, sql: str, *args):
        self.sql = sql
        return self._rows


def _signal(title: str, body: str = "") -> dict[str, Any]:
    return {
        "title": title,
        "body": body,
        "source_id": "src.wire",
        "provenance_kind": "",
        "row_kind": "signal",
    }


# The pro_ir_0817 shape: the claim denies proliferation activity; the decisive
# row's TITLE says nothing about it and its BODY does.
_TITLE_BLIND = "Regional roundup: economic talks resume in the Gulf"
_BODY_CARRIES = (
    "Officials confirmed a sharp expansion in the production of missiles and "
    "drones this month, including new enrichment-related procurement and a "
    "delivery of centrifuge components to a declared site."
)


# ---------------------------------------------------------------------------
# 1. The screen surface
# ---------------------------------------------------------------------------


async def test_signal_row_screens_on_title_and_body() -> None:
    """The rider itself: a signal's ``text`` now carries its body."""
    conn = _FakeConn([_signal(_TITLE_BLIND, _BODY_CARRIES)])
    rows = await load_absence_slice_rows(conn, uuid4())
    assert rows is not None and len(rows) == 1
    assert _TITLE_BLIND in rows[0].text  # every pre-rider title match preserved
    assert "production of missiles and drones" in rows[0].text  # now reachable


async def test_title_only_signal_is_byte_identical_to_before() -> None:
    """A signal with no body must screen exactly as it did pre-rider — the
    change can only ADD reachable text, never perturb the existing corpus."""
    conn = _FakeConn([_signal(_TITLE_BLIND)])
    rows = await load_absence_slice_rows(conn, uuid4())
    assert rows is not None and rows[0].text == _TITLE_BLIND


async def test_signal_body_is_capped() -> None:
    """A long article cannot dominate the screen's term statistics."""
    conn = _FakeConn([_signal("t", "x" * (_ABSENCE_SLICE_BODY_CHARS * 3))])
    rows = await load_absence_slice_rows(conn, uuid4())
    assert rows is not None
    assert len(rows[0].text) <= len("t ") + _ABSENCE_SLICE_BODY_CHARS


async def test_composed_row_behaviour_is_unchanged() -> None:
    """W1(b) still holds: a composed row shows its BODY, title only as fallback."""
    conn = _FakeConn([
        {"title": "topic", "body": "the verdict prose", "source_id": "",
         "provenance_kind": "", "row_kind": "output"},
        {"title": "topic only", "body": "", "source_id": "",
         "provenance_kind": "", "row_kind": "output"},
    ])
    rows = await load_absence_slice_rows(conn, uuid4())
    assert rows is not None
    assert rows[0].text == "the verdict prose"
    assert rows[1].text == "topic only"


# ---------------------------------------------------------------------------
# 2. The SQL projection (pinned by text — see the module docstring)
# ---------------------------------------------------------------------------


async def test_sql_projects_a_real_signal_body() -> None:
    """THE DEFECT, pinned. The signal leg must not hardcode an empty body, and
    must read the same fields the renderer reads."""
    conn = _FakeConn([_signal("t")])
    await load_absence_slice_rows(conn, uuid4())
    sql = " ".join(conn.sql.split())
    assert "'' AS body" not in sql, "the signal leg is projecting an empty body again"
    for field in ("distilled_body", "text_en", "archived_text", "summary"):
        assert f"payload->>'{field}'" in sql, f"render precedence lost {field}"
    # GDELT rows carry an OBJECT at raw_body; ->> would stringify the whole blob.
    assert "jsonb_typeof(payload->'raw_body') = 'string'" in sql


# ---------------------------------------------------------------------------
# 3. The real binding path
# ---------------------------------------------------------------------------


async def test_body_only_violator_reaches_the_slice_judge(monkeypatch) -> None:
    """REAL BINDING PATH: the pipeline entry, with a slice connection.

    A scope-qualified absence claim whose only violator is BODY-ONLY. Pre-rider
    the screen saw a title that shares nothing with the claim's content terms,
    so stage 2 was never consulted about it. Now the row reaches the judge — and
    the finding fails on it, which is the whole point of the backstop.
    """
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    claim = (
        "- No new enrichment, weaponization, or missile-delivery activity was "
        "reported in this desk's collection this cycle."
    )
    body = f"Diplomatic contacts continued this week [1].\n{claim}\n"
    citations = [{"marker": "[1]", "signal_id": str(uuid4()), "title": "talks"}]
    conn = _FakeConn([_signal(_TITLE_BLIND, _BODY_CARRIES)])
    judge = _SliceJudge({"violating_title": _TITLE_BLIND, "verdict": "contradicted"})
    report = await verify_finding_faithfulness(
        body=body,
        citations=citations,
        judge_llm=judge,
        target_id="country_watch_ir",
        slice_conn=conn,
        run_id=uuid4(),
    )
    assert judge.slice_calls == 1, "stage 2 was never consulted about the row"
    shown = judge.slice_prompts[0]
    assert "production of missiles and drones" in shown, (
        "the body-only violator did not reach the slice judge — the screen is "
        "still title-only"
    )


async def test_title_blind_row_was_invisible_before_the_rider() -> None:
    """The counterfactual that makes the test above mean something: with the
    body stripped (the pre-rider projection), the same row screens to a text
    that shares NO content term with the claim."""
    conn = _FakeConn([_signal(_TITLE_BLIND)])  # body == '' , i.e. pre-rider
    rows = await load_absence_slice_rows(conn, uuid4())
    assert rows is not None
    low = rows[0].text.lower()
    for term in ("enrichment", "weaponization", "missile", "delivery"):
        assert term not in low


@pytest.mark.parametrize("payload_body", ["", None])
async def test_missing_body_never_raises(payload_body) -> None:
    """Degrade-not-drop: a row shape without a usable body must not break the
    verify pass (the projection can yield NULL on old rows)."""
    conn = _FakeConn([{
        "title": "t", "body": payload_body, "source_id": "",
        "provenance_kind": "", "row_kind": "signal",
    }])
    rows = await load_absence_slice_rows(conn, uuid4())
    assert rows is not None and rows[0].text == "t"
