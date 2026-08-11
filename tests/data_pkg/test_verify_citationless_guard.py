# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A3 (2026-07-31) — the CITATION-LESS guard.

Readout structural finding #1: findings with no resolvable citations array fail
at 26.3% against a 6.1% base rate. A zero-citation finding's absence claims are
structurally unpassable — the judge is handed an EMPTY evidence map, so every
claim reads as ungrounded no matter how faithful it is. Two producer classes
ship 100% citation-less and ~5-12% of the rest ship an empty array, some
legitimately (composition refs that resolve differently) and some defectively.

Fixing the producers is V-A. THIS is the guard that keeps the class VISIBLE so
it can never silently regrow: the judge path logs and counts
``citationless_graded`` whenever it grades a finding with no resolvable
citations. Counts and logs ONLY — it changes no verdict and no score.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from legba.data.provenance.verify import (
    build_faithfulness_critique_payload,
    verify_finding_faithfulness,
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    subprovider = "stub-judge"

    def __init__(self, verdicts_json: str) -> None:
        self._json = verdicts_json

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        return _Response(self._json)


_BODY = "Alpha struck Bravo base on Monday.\nCharlie seized the port on Tuesday.\n"
_ALL_SUPPORTED = '{"verdicts": ["supported", "supported"]}'


async def test_empty_citations_array_is_counted(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body=_BODY, citations=[], judge_llm=_StubJudge(_ALL_SUPPORTED)
    )
    assert report.judge_status == "llm"
    assert report.counters["citationless_graded"] == 1


async def test_absent_citations_key_is_counted(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body=_BODY, citations=None, judge_llm=_StubJudge(_ALL_SUPPORTED)
    )
    assert report.counters["citationless_graded"] == 1


async def test_unresolvable_citations_are_counted(monkeypatch) -> None:
    """Entries with no signal_id resolve to NOTHING — the judge's evidence map is
    just as empty as with no array at all (the honesty guarantee that stops an
    unresolved ref from passing the floor is exactly what empties it)."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body=_BODY,
        citations=[{"marker": "[1]", "title": "(unresolved substrate ref)"}],
        judge_llm=_StubJudge(_ALL_SUPPORTED),
    )
    assert report.counters["citationless_graded"] == 1


async def test_composition_with_no_resolvable_ordinals_is_counted(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body=_BODY,
        citations=[{"marker": "[[ref:x]]", "ref_kind": "finding"}],
        judge_llm=_StubJudge(_ALL_SUPPORTED),
    )
    assert report.counters["citationless_graded"] == 1


async def test_a_normally_cited_finding_is_not_counted(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body="Alpha struck Bravo base on Monday [1].\nCharlie seized the port [1].\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4()), "title": "Strike"}],
        judge_llm=_StubJudge(_ALL_SUPPORTED),
    )
    assert "citationless_graded" not in report.counters


async def test_guard_is_judge_path_only(monkeypatch) -> None:
    """The class the readout measured is a JUDGE failure mode (an empty evidence
    map). With the judge off nothing was graded, so nothing is counted."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(body=_BODY, citations=[])
    assert "citationless_graded" not in report.counters


async def test_guard_changes_no_verdict_and_no_score(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body=_BODY, citations=[], judge_llm=_StubJudge(_ALL_SUPPORTED)
    )
    assert report.faithfulness_score == 1.0
    assert report.supported_claims == 2
    assert all(cv.verdict == "supported" for cv in report.claim_verdicts)


async def test_guard_logs_and_persists(monkeypatch, caplog) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    with caplog.at_level(logging.WARNING, logger="legba.data.provenance.verify"):
        report = await verify_finding_faithfulness(
            body=_BODY, citations=[], judge_llm=_StubJudge(_ALL_SUPPORTED)
        )
    assert any("citationless_graded" in r.message for r in caplog.records)
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    counters: dict[str, Any] = payload["data"]["verification"]["counters"]
    assert counters["citationless_graded"] == 1


# ---------------------------------------------------------------------------
# W3 (2026-08-02) — the shape split, and the mechanism it exists to pin.
#
# The 08-02 readout called this guard "under-firing ~12x" for economic_coercion /
# energy_security / proliferation_watch, on the strength of a DB projection
# showing 2,752 rows with a bare-ordinal ``data.evidence`` array and zero
# ``data.citations``. Traced read-only against the live substrate, that is a
# PROJECTION ARTIFACT: ``analyst_outputs.data`` stores the WHOLE serialized
# FindingPayload, so a finding's own ``data['citations']`` sits one level down at
# ``data->'data'->'citations'`` — populated on 94% of those rows — while
# ``FindingPayload.evidence`` is the separate top-level evidence-IDENTIFIER
# field the verify path never reads. Citations resolve normally, and the guard's
# 8.3% firing rate matches the real 6.6% no-resolvable-citation rate.
#
# These tests pin BOTH halves of that finding so it cannot be re-litigated from a
# JSONB path: the shape those producers actually emit resolves, and the guard
# now names WHICH citationless shape it saw.
# ---------------------------------------------------------------------------


async def test_the_bare_ordinal_evidence_shape_still_resolves_its_citations(
    monkeypatch,
) -> None:
    """The economic_coercion / energy_security / proliferation_watch payload:
    bare-ordinal ``evidence`` identifiers AND a populated ``data['citations']``.
    The evidence field is not a citation shape and is not one the resolver should
    ever learn — the citations it ships alongside are the real bridge."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    # What the producer emits: FindingPayload.evidence = ["1", "2", "3"] and
    # FindingPayload.data["citations"] = the real marker -> signal bridge.
    payload_evidence = ["1", "2", "3"]
    citations = [
        {"marker": f"[{n}]", "signal_id": str(uuid4()), "title": f"cited row {n}"}
        for n in (1, 2, 3)
    ]
    assert payload_evidence  # the field exists; the verify path is handed the OTHER one
    report = await verify_finding_faithfulness(
        body="Alpha struck Bravo base on Monday [1].\nCharlie seized the port [2].\n",
        citations=citations,
        judge_llm=_StubJudge(_ALL_SUPPORTED),
    )
    assert "citationless_graded" not in report.counters


async def test_the_guard_names_which_citationless_shape_it_saw(monkeypatch) -> None:
    """``citations_absent`` (the producer emitted none) and
    ``citations_unresolvable`` (a bridge ran and produced no usable ids) are
    different defects with different owners — the pooled counter cannot tell
    them apart, which is how a projection artifact passed for a producer bug."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    absent = await verify_finding_faithfulness(
        body=_BODY, citations=None, judge_llm=_StubJudge(_ALL_SUPPORTED)
    )
    assert absent.counters["citations_absent"] == 1
    assert "citations_unresolvable" not in absent.counters

    empty = await verify_finding_faithfulness(
        body=_BODY, citations=[], judge_llm=_StubJudge(_ALL_SUPPORTED)
    )
    assert empty.counters["citations_absent"] == 1

    unresolvable = await verify_finding_faithfulness(
        body=_BODY,
        citations=[{"marker": "[1]", "title": "(unresolved substrate ref)"}],
        judge_llm=_StubJudge(_ALL_SUPPORTED),
    )
    assert unresolvable.counters["citations_unresolvable"] == 1
    assert "citations_absent" not in unresolvable.counters
    # The pooled contract counter still fires for every shape.
    for report in (absent, empty, unresolvable):
        assert report.counters["citationless_graded"] == 1


async def test_the_shape_and_convention_reach_the_log_line(monkeypatch, caplog) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    with caplog.at_level(logging.WARNING, logger="legba.data.provenance.verify"):
        await verify_finding_faithfulness(
            body=_BODY,
            citations=[{"marker": "[[ref:x]]", "ref_kind": "finding"}],
            judge_llm=_StubJudge(_ALL_SUPPORTED),
        )
    line = next(
        r.getMessage() for r in caplog.records if "citationless" in r.getMessage()
    )
    assert "shape=citations_unresolvable" in line
    assert "convention=subclaim" in line
