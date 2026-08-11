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

import datetime
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from legba.data.analysts.inline_target import (
    _coerce_finding,
    _coerce_indicators,
    _derive_indicators_from_prose,
)
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
# 2b. EMIT FALLBACK — derive indicators from the prose watch section (S3-T1)
# ---------------------------------------------------------------------------
#
# The core plane won't populate the JSON `indicators` array but DOES write the
# prose "## Indicators to watch" bullet list. When the structured array is
# absent, `_coerce_finding` mines those bullets into `not_observed` entries.


_PROSE_BODY = (
    "The border is calm for now.\n"
    "## Assessment\n"
    "The situation is stable under current policy [1].\n"
    "## Indicators to watch\n"
    "- Reservists mobilized in the eastern district [2].\n"
    "- Any new sanctions on this country's crude exports.\n"
    "- Closure of the main border crossing [3][4]\n"
)


def test_derive_indicators_from_prose_basic():
    today = datetime.date(2026, 7, 2)
    got = _derive_indicators_from_prose(_PROSE_BODY, today=today)
    assert len(got) == 3
    # All derived entries are forward-looking `not_observed` with no citations.
    assert {e["status"] for e in got} == {"not_observed"}
    assert all(e["citations"] == [] for e in got)
    # `[N]` markers stripped; whitespace-before-punct tightened.
    assert got[0]["statement"] == "Reservists mobilized in the eastern district."
    assert got[2]["statement"] == "Closure of the main border crossing"
    assert all("[" not in e["statement"] for e in got)
    # Slug derived from the statement; dates carried per the schema.
    assert got[0]["id"] == "reservists-mobilized-in-the-eastern-district"
    assert got[0]["first_seen"] == "2026-07-02"
    assert got[0]["horizon_date"] == "2026-08-01"  # today + 30d


def test_derive_indicators_no_watch_section_is_empty():
    body = "## Assessment\nThe grid is stable [1].\n- a stray bullet with no watch heading\n"
    assert _derive_indicators_from_prose(body) == []
    assert _derive_indicators_from_prose("") == []


def test_derive_indicators_tolerates_bold_heading_and_stops_at_next_heading():
    body = (
        "**Indicators to watch**\n"
        "- Grid frequency excursions beyond tolerance\n"
        "## Sources\n"
        "- This source bullet must NOT become an indicator\n"
    )
    got = _derive_indicators_from_prose(body)
    assert len(got) == 1
    assert got[0]["id"] == "grid-frequency-excursions-beyond-tolerance"


def test_derive_indicators_caps_at_six_and_dedups_slugs():
    bullets = "\n".join(
        f"- Reservists mobilized in the eastern district near town {i}" for i in range(8)
    )
    body = "## Indicators to watch\n" + bullets + "\n"
    got = _derive_indicators_from_prose(body)
    assert len(got) == 6  # capped
    ids = [e["id"] for e in got]
    assert len(ids) == len(set(ids))  # slugs de-duplicated within the finding


def test_coerce_finding_derives_indicators_when_json_absent():
    """ACCEPTANCE: a finding whose body has a prose watch list but NO JSON
    `indicators` array → data.indicators is populated (derived, not_observed)."""
    raw = json.dumps(
        {"title": "T", "body": _PROSE_BODY, "confidence": 0.5, "tags": ["escalation"]}
    )
    fp = _coerce_finding(raw, fallback_title="fb")
    inds = fp.data.get("indicators")
    assert isinstance(inds, list) and len(inds) == 3
    assert {e["status"] for e in inds} == {"not_observed"}
    assert all("[" not in e["statement"] for e in inds)
    today = datetime.date.today()
    assert inds[0]["first_seen"] == today.isoformat()


def test_coerce_finding_keeps_model_indicators_over_prose_fallback():
    """When the model DOES emit the structured array, keep it — never override
    with the prose-derived fallback even if a watch section is also present."""
    model = _entry(status="triggered", citations=[1], id_="model-a")
    raw = json.dumps(
        {
            "title": "T",
            "body": _PROSE_BODY,  # also carries a prose watch section
            "confidence": 0.5,
            "tags": ["escalation"],
            "indicators": [model],
        }
    )
    fp = _coerce_finding(raw, fallback_title="fb")
    inds = fp.data["indicators"]
    assert len(inds) == 1
    assert inds[0]["id"] == "model-a"
    assert inds[0]["status"] == "triggered"


def test_coerce_finding_no_prose_no_json_omits_indicators():
    """Neither a structured array nor a prose watch section → key absent."""
    raw = json.dumps(
        {
            "title": "T",
            "body": "## Assessment\nThe grid is stable under current policy [1].\n",
            "confidence": 0.5,
            "tags": ["escalation"],
        }
    )
    fp = _coerce_finding(raw, fallback_title="fb")
    assert "indicators" not in fp.data


# ---------------------------------------------------------------------------
# 2b. DS-1 — the DEGRADE path keeps the I&W block too
# ---------------------------------------------------------------------------
#
# The live shape, not a hypothetical: a ``max_tokens`` cut lands mid-JSON, the
# parse raises, and ``_salvage_envelope_body`` recovers a COMPLETE markdown body
# from the ``"body"`` string the model had already finished writing. The
# structured array serializes AFTER the body, so truncation takes the array and
# leaves the prose — and the degrade path used to drop the prose watch section
# on the floor rather than deriving from it. Measured on disruption_status: 3 of
# 76 findings (all lane_black_sea), each carrying six good watch bullets and no
# ``data.indicators``, invisible to the indicator_tracker diff.

#: A real truncated envelope — cut inside the ``indicators`` array, body intact.
_TRUNCATED_MID_INDICATORS = (
    '{\n'
    '  "title": "Black Sea lane - interdiction risk - degrading",\n'
    '  "body": "*As of 2026-08-05; slice covers the trailing 24h to the run '
    'date; 19 signals.*\\n**BLUF:** No material change; the Black Sea lane '
    'continues to face a degrading interdiction and physical-risk environment '
    '[1].\\n\\n## Indicators to watch\\n- New confirmed drone or missile '
    'attacks on civilian vessels in the Black Sea.\\n- Announcements of '
    'insurers reinstating or further withdrawing war-risk coverage.\\n- '
    'Reported reductions in vessel transits or cargo volumes through Black Sea '
    'ports.\\n",\n'
    '  "confidence": 0.55,\n'
    '  "evidence": ["1", "3"],\n'
    '  "tags": ["topic:disruption_status", "severity:elevated"],\n'
    '  "indicators": [\n'
    '    {"id": "corridor-attack-or-seizure", "statement": "Attack, seizure'
)


def test_a_truncated_envelope_keeps_the_indicators_its_prose_carries():
    """ACCEPTANCE (DS-1): the degrade path derives the I&W block from the
    salvaged body, exactly as the structured path already does."""
    fp = _coerce_finding(_TRUNCATED_MID_INDICATORS, fallback_title="fb")
    # It really is the degrade path — this is not the structured branch.
    assert fp.confidence == 0.3
    assert fp.tags == ["unstructured"]
    # The salvaged body is markdown, not JSON scaffolding.
    assert fp.body.startswith("*As of 2026-08-05;")
    assert '"body"' not in fp.body
    inds = fp.data.get("indicators")
    assert isinstance(inds, list) and len(inds) == 3
    assert {e["status"] for e in inds} == {"not_observed"}
    assert inds[0]["statement"].startswith("New confirmed drone or missile")
    # Derived entries are pre-registered, never cited: the prose bullets are
    # forward-looking, and a citation nobody made must not be invented.
    assert all(e["citations"] == [] for e in inds)


def test_a_truncated_envelope_without_a_watch_section_invents_nothing():
    """Degrade-not-fabricate is the whole posture: no watch section in the
    salvaged prose ⇒ no ``indicators`` key at all."""
    raw = (
        '{\n  "title": "T",\n'
        '  "body": "*As of 2026-08-05.*\\n**BLUF:** Transits held steady at '
        'the reported level [2].\\n",\n'
        '  "confidence": 0.55,\n  "indicators": [\n    {"id": "transit'
    )
    fp = _coerce_finding(raw, fallback_title="fb")
    assert fp.confidence == 0.3
    assert "indicators" not in fp.data


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
    """An uncited 'triggered' indicator is a judge-BLIND structured defect. The
    judge is authoritative over PROSE (C1: no min() co-veto), but the residual
    indicator penalty is still folded into the refined score, so the demotion
    survives even when the judge passes all prose."""
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
    # 1 prose claim (judge: supported) + 1 uncited-triggered indicator (a
    # judge-blind residual floor span) → 1/(1+1) = 0.5; the demotion survives.
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
