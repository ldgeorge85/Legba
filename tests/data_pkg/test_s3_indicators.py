# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S3-T1 — structured I&W indicators contract.

Covers the three pieces of the S3-T1 slice, all DETERMINISTIC (the optional LLM
judge is OFF by default or a canned stub — no test depends on a live LLM):

  1. SCHEMA — a fixture FindingPayload round-trips a well-formed
     ``data.indicators[]`` block through validation (normalized to canonical
     JSON-safe primitives); a malformed block trips a ValidationError → DLQ.
  2. INGESTION — ``inline_target._coerce_finding`` extracts the LLM's top-level
     ``indicators`` array and DROPS malformed entries (degrade-not-drop).
  3. VERIFY — an uncited ``triggered`` indicator DEMOTES faithfulness while an
     uncited ``not_observed`` / ``expired`` does NOT (forward-looking → exempt),
     and the forward-looking 'Indicators to watch' PROSE section stays dropped
     wholesale (never re-scored by the structured check).
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from legba.data.analysts.inline_target import _coerce_finding, _coerce_indicators
from legba.data.provenance.models import FindingPayload
from legba.data.provenance.verify import (
    _fold_indicators,
    _indicator_spans,
    verify_finding_faithfulness,
)
from legba.data.schemas.analyst import IndicatorEntry, validate_indicators


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _entry(
    *,
    status: str = "not_observed",
    citations: list[int] | None = None,
    id_: str = "reservist-mobilization",
    statement: str = "Reservists mobilized in the eastern military district",
    horizon: str = "2026-08-01",
    first: str = "2026-07-02",
) -> dict:
    return {
        "id": id_,
        "statement": statement,
        "status": status,
        "horizon_date": horizon,
        "first_seen": first,
        "citations": citations if citations is not None else [],
    }


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _AllSupportedJudge:
    """Stub judge that marks every graded claim 'supported' (never live)."""

    subprovider = "stub"

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):
        # #116d: _run_judge now enforces len(verdicts) == len(claims) (no more
        # zip-truncation). The only body this stub grades has a single prose
        # claim (the '## Assessment' line), so return exactly one 'supported'.
        return _Resp('{"verdicts": ["supported"]}')


# ---------------------------------------------------------------------------
# 1. SCHEMA — round-trip + validation
# ---------------------------------------------------------------------------


def test_finding_roundtrips_structured_indicators():
    """A well-formed indicators block validates + is normalized to canonical
    JSON-safe primitives (ISO date strings), surviving a full model_dump."""
    entries = [
        _entry(status="triggered", citations=[2], id_="a"),
        _entry(status="not_observed", id_="b"),
    ]
    fp = FindingPayload(
        title="Escalation risk",
        body="body",
        data={"citations": [{"marker": "[2]", "signal_id": str(uuid4())}], "indicators": entries},
    )
    got = fp.data["indicators"]
    assert len(got) == 2
    assert got[0]["status"] == "triggered"
    assert got[0]["citations"] == [2]
    # date fields normalized to ISO strings (JSONB-safe, round-trippable).
    assert got[0]["horizon_date"] == "2026-08-01"
    assert got[0]["first_seen"] == "2026-07-02"
    dumped = fp.model_dump(mode="json")
    assert dumped["data"]["indicators"][1]["id"] == "b"


def test_indicator_entry_typed_shape():
    ie = IndicatorEntry(**_entry(status="triggered", citations=[1, 4]))
    assert ie.status == "triggered"
    assert ie.citations == [1, 4]
    assert str(ie.horizon_date) == "2026-08-01"


def test_validate_indicators_absent_is_empty():
    assert validate_indicators(None) == []


def test_indicators_block_must_be_a_list():
    """A present-but-non-list indicators block is a gross type error → reject."""
    with pytest.raises(ValidationError):
        FindingPayload(title="t", body="b", data={"indicators": "nope"})


def test_malformed_indicator_entry_rejected_by_schema():
    """A finding with a structurally broken indicator (missing required fields)
    trips a ValidationError so the write path DLQs it — same fail-loud contract
    the other typed payload fields carry."""
    with pytest.raises(ValidationError):
        FindingPayload(
            title="t",
            body="b",
            data={"indicators": [{"id": "a", "status": "triggered"}]},
        )


def test_finding_without_indicators_is_byte_identical():
    """No indicators key → data untouched (no empty list injected)."""
    fp = FindingPayload(title="t", body="b", data={"citations": []})
    assert "indicators" not in fp.data


# ---------------------------------------------------------------------------
# 2. INGESTION — _coerce_finding / _coerce_indicators (degrade-not-drop)
# ---------------------------------------------------------------------------


def test_coerce_finding_extracts_indicators_and_drops_malformed():
    good = _entry(status="not_observed", id_="a")
    bad = {"id": "b", "status": "triggered"}  # missing statement/dates
    raw = json.dumps(
        {
            "title": "T",
            "body": "B",
            "confidence": 0.5,
            "tags": ["escalation"],
            "indicators": [good, bad, "not-a-dict"],
        }
    )
    fp = _coerce_finding(raw, fallback_title="fb")
    inds = fp.data.get("indicators")
    assert isinstance(inds, list) and len(inds) == 1
    assert inds[0]["id"] == "a"


def test_coerce_finding_without_indicators_omits_key():
    raw = json.dumps({"title": "T", "body": "B", "confidence": 0.5, "tags": ["escalation"]})
    fp = _coerce_finding(raw, fallback_title="fb")
    assert "indicators" not in fp.data


def test_coerce_finding_reads_nested_data_indicators():
    """Tolerate a model that nested the array under `data` instead of top-level."""
    raw = json.dumps(
        {
            "title": "T",
            "body": "B",
            "confidence": 0.5,
            "tags": ["escalation"],
            "data": {"indicators": [_entry(status="triggered", citations=[1], id_="z")]},
        }
    )
    fp = _coerce_finding(raw, fallback_title="fb")
    assert fp.data["indicators"][0]["id"] == "z"


def test_coerce_indicators_non_list_is_empty():
    assert _coerce_indicators(None) == []
    assert _coerce_indicators("x") == []


# ---------------------------------------------------------------------------
# 3. VERIFY — triggered must cite; not_observed/expired exempt
# ---------------------------------------------------------------------------


def test_indicator_spans_counts_only_triggered():
    inds = [
        {"status": "triggered", "citations": [1]},   # supported
        {"status": "triggered", "citations": []},    # unsupported
        {"status": "not_observed", "citations": []}, # exempt
        {"status": "expired", "citations": []},      # exempt
        "garbage",                                    # skipped
    ]
    checkable, supported, spans = _indicator_spans(inds)
    assert checkable == 2
    assert supported == 1
    assert len(spans) == 1
    assert spans[0].reason == "indicator_uncited_triggered"


def test_fold_indicators_noop_without_triggered():
    from legba.data.provenance.verify import FaithfulnessReport

    floor = FaithfulnessReport(faithfulness_score=1.0, checkable_claims=2, supported_claims=2)
    same = _fold_indicators(floor, [{"status": "not_observed", "citations": []}])
    assert same is floor  # byte-identical: nothing to fold


async def test_uncited_triggered_demotes(monkeypatch):
    """ACCEPTANCE: an uncited `triggered` indicator is an unsupported span that
    folds into the faithfulness score (judge OFF)."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body = "## Assessment\nThe situation is calm across the border [1].\n"
    citations = [{"marker": "[1]", "signal_id": str(uuid4())}]
    indicators = [_entry(status="triggered", citations=[])]
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, indicators=indicators
    )
    # 1 supported prose claim + 1 uncited triggered indicator (unsupported).
    assert rep.checkable_claims == 2
    assert rep.supported_claims == 1
    assert rep.faithfulness_score == pytest.approx(0.5)
    assert any(s.reason == "indicator_uncited_triggered" for s in rep.unsupported_spans)


async def test_uncited_not_observed_does_not_demote(monkeypatch):
    """ACCEPTANCE (the honesty nuance): an uncited `not_observed` indicator is
    forward-looking → exempt; faithfulness stays 1.0."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body = "## Assessment\nThe situation is calm across the border [1].\n"
    citations = [{"marker": "[1]", "signal_id": str(uuid4())}]
    indicators = [_entry(status="not_observed", citations=[])]
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, indicators=indicators
    )
    assert rep.checkable_claims == 1  # only the prose claim
    assert rep.supported_claims == 1
    assert rep.faithfulness_score == 1.0
    assert not any(s.reason == "indicator_uncited_triggered" for s in rep.unsupported_spans)


async def test_cited_triggered_does_not_demote(monkeypatch):
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body = "## Assessment\nThe situation is calm across the border [1].\n"
    citations = [{"marker": "[1]", "signal_id": str(uuid4())}]
    indicators = [_entry(status="triggered", citations=[1])]
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, indicators=indicators
    )
    assert rep.checkable_claims == 2
    assert rep.supported_claims == 2
    assert rep.faithfulness_score == 1.0


async def test_exempt_prose_watch_section_still_not_rescored(monkeypatch):
    """The forward-looking 'Indicators to watch' PROSE stays dropped wholesale;
    ONLY the structured mirror is scored. Adding an uncited triggered structured
    indicator raises checkable by exactly 1 (the entry) — the prose watch bullets
    are still never counted."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body = (
        "## Assessment\nThe grid is stable under current policy [1].\n"
        "## Indicators to watch\n"
        "- A sudden refinery outage would break this assessment.\n"
        "- Any new sanctions on this country's crude exports.\n"
    )
    citations = [{"marker": "[1]", "signal_id": str(uuid4())}]
    # No structured block: the prose watch section contributes nothing.
    rep_prose = await verify_finding_faithfulness(body=body, citations=citations)
    assert rep_prose.checkable_claims == 1  # only the Assessment line
    # One uncited triggered structured indicator → +1 checkable ONLY (prose watch
    # bullets remain excluded).
    indicators = [_entry(status="triggered", statement="refinery outage", citations=[])]
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, indicators=indicators
    )
    assert rep.checkable_claims == 2
    assert rep.supported_claims == 1
    assert rep.faithfulness_score == pytest.approx(0.5)


async def test_uncited_triggered_survives_judge_min(monkeypatch):
    """The demotion is folded into the floor BEFORE the judge, so the judge's
    min(floor, judge) refinement preserves it even when the judge passes all
    prose."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = "## Assessment\nThe situation is calm across the border [1].\n"
    citations = [{"marker": "[1]", "signal_id": str(uuid4())}]
    indicators = [_entry(status="triggered", citations=[])]
    rep = await verify_finding_faithfulness(
        body=body,
        citations=citations,
        indicators=indicators,
        judge_llm=_AllSupportedJudge(),
    )
    assert rep.judge_status == "llm"
    # floor folded indicators → 0.5; judge says prose supported (1.0);
    # min(0.5, 1.0) = 0.5 → demotion survives.
    assert rep.faithfulness_score == pytest.approx(0.5)


async def test_no_indicators_arg_is_byte_identical(monkeypatch):
    """Absent indicators → the pass is byte-identical to the pre-S3-T1 floor."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body = "## Assessment\nThe grid is stable under current policy [1].\n"
    citations = [{"marker": "[1]", "signal_id": str(uuid4())}]
    rep = await verify_finding_faithfulness(body=body, citations=citations)
    assert rep.checkable_claims == 1
    assert rep.supported_claims == 1
    assert rep.faithfulness_score == 1.0
