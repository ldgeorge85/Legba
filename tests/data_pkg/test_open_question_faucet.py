# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-2b — the unit open-question faucet: payload field + conversion + prompts.

Covers the three layers of the faucet:

  * SCHEMA — ``data.open_questions`` is additive-optional on ``FindingPayload``
    (old payloads without the field validate byte-for-byte unchanged); present
    ⇒ strictly validated + normalized (the ``indicators`` precedent), malformed
    ⇒ ValidationError (→ DLQ at the write path).
  * COERCION — ``inline_target._coerce_open_questions`` / ``_coerce_finding``
    tolerate a noisy LLM array: malformed entries drop honestly, valid ones
    survive, nothing is fabricated to fill a quota.
  * CONVERSION — ``inline_target.convert_open_questions`` turns the persisted
    block into ``hypotheses`` rows (status='open_question', lineage = finding +
    resolved citation signals, unit + target stamped), idempotent per
    (finding, question-text), degrade-not-break.
  * PROMPTS — all 10 inline-unit descriptors carry the OPEN QUESTIONS
    instruction block and still validate as AnalystDescriptor.
"""
from __future__ import annotations

import json
import pathlib
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml
from pydantic import ValidationError

from legba.data.analysts.inline_target import (
    OPEN_QUESTION_MARKER_KEY,
    _coerce_finding,
    _coerce_open_questions,
    convert_open_questions,
)
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext
from legba.data.provenance.models import FindingPayload
from legba.data.schemas.analyst import (
    MAX_OPEN_QUESTIONS,
    AnalystDescriptor,
    OpenQuestionEntry,
    validate_open_questions,
)

# ---------------------------------------------------------------------------
# Schema — additive-optional field on FindingPayload
# ---------------------------------------------------------------------------


def test_old_payload_without_field_still_validates():
    """Additive-optional: a payload with no open_questions key is unchanged."""
    p = FindingPayload(title="t", body="b", data={"raw_llm_response": "x"})
    assert "open_questions" not in p.data


def test_payload_with_valid_open_questions_normalizes():
    p = FindingPayload(
        title="t",
        data={"open_questions": [
            {"question": "Is the mobilization confirmed?", "refs": [1, 3]},
            {"question": "Why did output drop?", "refs": []},
        ]},
    )
    assert p.data["open_questions"] == [
        {"question": "Is the mobilization confirmed?", "refs": [1, 3]},
        {"question": "Why did output drop?", "refs": []},
    ]


@pytest.mark.parametrize("bad", [
    "not-a-list",
    [{"question": "", "refs": []}],                      # empty question
    [{"question": "q?", "refs": "nope"}],                # refs not a list
    [{"question": "q?", "refs": [1], "extra": True}],    # extra key (forbid)
    [["not", "an", "object"]],
])
def test_payload_with_malformed_open_questions_rejects(bad):
    with pytest.raises(ValidationError):
        FindingPayload(title="t", data={"open_questions": bad})


def test_validate_open_questions_none_and_cap():
    assert validate_open_questions(None) == []
    over = [{"question": f"q{i}?", "refs": []} for i in range(MAX_OPEN_QUESTIONS + 1)]
    with pytest.raises(ValueError):
        validate_open_questions(over)


def test_open_question_entry_shape():
    e = OpenQuestionEntry(question="q?", refs=[2])
    assert e.model_dump(mode="json") == {"question": "q?", "refs": [2]}


# ---------------------------------------------------------------------------
# Coercion — lenient ingestion of the LLM array
# ---------------------------------------------------------------------------


def test_coerce_open_questions_drops_malformed_keeps_valid():
    raw = [
        {"question": "Is the strike corroborated?", "refs": [1, "2", None, 0, 1]},
        {"question": ""},                       # empty question — dropped
        "not-an-object",                        # dropped
        {"question": "Is the strike corroborated?", "refs": [4]},  # dup — dropped
        {"question": "Second question?", "refs": "garbage"},       # refs degrade to []
    ]
    out = _coerce_open_questions(raw)
    assert out == [
        {"question": "Is the strike corroborated?", "refs": [1, 2]},
        {"question": "Second question?", "refs": []},
    ]


def test_coerce_open_questions_caps_and_handles_absent():
    assert _coerce_open_questions(None) == []
    assert _coerce_open_questions("nope") == []
    many = [{"question": f"q{i}?", "refs": []} for i in range(20)]
    assert len(_coerce_open_questions(many)) == MAX_OPEN_QUESTIONS


def test_coerce_finding_lands_open_questions_block():
    raw = json.dumps({
        "title": "T",
        "body": "**BLUF:** x [1]",
        "confidence": 0.7,
        "open_questions": [
            {"question": "Unresolved?", "refs": [1]},
            {"question": "", "refs": [2]},      # malformed — dropped, honestly
        ],
    })
    finding = _coerce_finding(raw, fallback_title="fb")
    assert finding.data["open_questions"] == [{"question": "Unresolved?", "refs": [1]}]


def test_coerce_finding_without_block_has_no_key():
    raw = json.dumps({"title": "T", "body": "x [1]", "confidence": 0.5})
    finding = _coerce_finding(raw, fallback_title="fb")
    assert "open_questions" not in finding.data


def test_coerce_finding_tolerates_nested_block():
    raw = json.dumps({
        "title": "T", "body": "x [1]",
        "data": {"open_questions": [{"question": "Nested?", "refs": []}]},
    })
    finding = _coerce_finding(raw, fallback_title="fb")
    assert finding.data["open_questions"] == [{"question": "Nested?", "refs": []}]


# ---------------------------------------------------------------------------
# Conversion — payload block -> hypotheses rows (DB-backed)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    # Good citizenship on the session-shared DB: this file's converted rows
    # carry the containment marker — sweep them so global open-question
    # consumers (the claim_watch scanner tests) never see our leftovers.
    try:
        await c.execute(
            "DELETE FROM hypotheses WHERE status = 'open_question' "
            "AND diagnostic_evidence @> $1::jsonb",
            json.dumps([{"marker": OPEN_QUESTION_MARKER_KEY}]),
        )
    finally:
        await c.close()


def _ctx(target_id: str | None = None) -> AnalystContext:
    return AnalystContext(
        analyst_id="escalation",
        analyst_version="v1",
        run_id=uuid4(),
        target_id=target_id,
    )


async def _rows_for_finding(conn, finding_id) -> list:
    probe = json.dumps([{
        "marker": OPEN_QUESTION_MARKER_KEY,
        "origin": "unit_payload",
        "finding_id": str(finding_id),
    }])
    return await conn.fetch(
        "SELECT * FROM hypotheses WHERE status = 'open_question' "
        "AND diagnostic_evidence @> $1::jsonb ORDER BY produced_at, id",
        probe,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_convert_writes_rows_with_lineage_and_scope(conn):
    finding_id = uuid4()
    sig1, sig3 = uuid4(), uuid4()
    tgt = f"country_oq_{uuid4().hex[:8]}"
    finding_data = {
        "citations": [
            {"marker": "[1]", "signal_id": str(sig1), "title": "a"},
            {"marker": "[3]", "signal_id": str(sig3)},
            {"marker": "not-a-marker", "signal_id": str(uuid4())},
        ],
        "open_questions": [
            {"question": "Is the mobilization confirmed?", "refs": [1, 3]},
            {"question": "Unreffed but real?", "refs": [9]},   # 9 unresolvable
        ],
    }
    written = await convert_open_questions(
        conn, finding_data=finding_data, finding_id=finding_id, analyst_ctx=_ctx(tgt),
    )
    assert written == 2

    rows = await _rows_for_finding(conn, finding_id)
    assert len(rows) == 2
    by_thesis = {r["thesis"]: r for r in rows}
    r1 = by_thesis["Is the mobilization confirmed?"]
    assert set(r1["derived_from"]) == {finding_id, sig1, sig3}
    assert r1["status"] == "open_question"
    assert r1["analyst_id"] == "escalation"
    assert r1["target_id"] == tgt
    r2 = by_thesis["Unreffed but real?"]
    # The unresolvable ref degrades to finding-only lineage — never fabricated.
    assert set(r2["derived_from"]) == {finding_id}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_convert_is_idempotent_per_finding_question(conn):
    finding_id = uuid4()
    finding_data = {
        "citations": [],
        "open_questions": [{"question": "Only once?", "refs": []}],
    }
    first = await convert_open_questions(
        conn, finding_data=finding_data, finding_id=finding_id, analyst_ctx=_ctx(),
    )
    second = await convert_open_questions(
        conn, finding_data=finding_data, finding_id=finding_id, analyst_ctx=_ctx(),
    )
    assert first == 1
    assert second == 0
    assert len(await _rows_for_finding(conn, finding_id)) == 1
    # The SAME question from a DIFFERENT finding is a new row (per-finding key).
    other = uuid4()
    assert await convert_open_questions(
        conn, finding_data=finding_data, finding_id=other, analyst_ctx=_ctx(),
    ) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_convert_degrades_on_malformed_data(conn):
    """Malformed payload shapes yield 0 writes and never raise."""
    fid = uuid4()
    for bad in (
        None,
        {},
        {"open_questions": None},
        {"open_questions": "nope"},
        {"open_questions": [None, "x", {"refs": [1]}, {"question": "  "}]},
    ):
        assert await convert_open_questions(
            conn, finding_data=bad, finding_id=fid, analyst_ctx=_ctx(),
        ) == 0
    assert await _rows_for_finding(conn, fid) == []


@pytest.mark.asyncio
async def test_convert_entry_isolation_on_broken_conn():
    """A substrate error inside an entry skips it — siblings unaffected, no raise."""

    class _BrokenConn:
        async def fetchval(self, *a, **kw):
            raise RuntimeError("substrate down")

    written = await convert_open_questions(
        _BrokenConn(),
        finding_data={"open_questions": [{"question": "q?", "refs": []}]},
        finding_id=uuid4(),
        analyst_ctx=_ctx(),
    )
    assert written == 0


# ---------------------------------------------------------------------------
# Prompts — the 10 inline-unit descriptors carry the instruction block
# ---------------------------------------------------------------------------

_DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parents[2] / "descriptors"

_UNIT_DESCRIPTORS = (
    "analyst_escalation.yaml",
    "analyst_proliferation_watch.yaml",
    "analyst_economic_coercion.yaml",
    "analyst_military_posture.yaml",
    "analyst_internal_stability.yaml",
    "analyst_narrative_coordination.yaml",
    "analyst_energy_security.yaml",
    "analyst_leadership_transition.yaml",
    "analyst_cross_doc_corroborator.yaml",
    "analyst_corpus_researcher.yaml",
)


@pytest.mark.parametrize("name", _UNIT_DESCRIPTORS)
def test_unit_descriptor_carries_open_question_block(name: str):
    """Each inline-unit prompt instructs the optional open_questions field —
    and the descriptor still validates against the real schema."""
    body = yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    prompt = desc.method.system_prompt or ""
    assert '"open_questions"' in prompt, f"{name}: missing the field instruction"
    # Wrap-tolerant (the block scalar folds the sentence across lines).
    flat = " ".join(prompt.split())
    assert "NEVER invent questions to fill the quota" in flat, (
        f"{name}: missing the no-quota rule"
    )
    assert '"refs"' in prompt, f"{name}: missing the refs tie-in"
