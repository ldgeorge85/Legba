# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Roll-28 proof regression (2026-09-28): an answer cited entirely to REAL
platform rows that carry no origin class (situations, findings, hypotheses,
events, nexuses) is grounded, not held — and the evidence floor counts those
rows too. A live core-plane run once cited twelve real active
situations and was bannered "nothing resolved" with uncertainty floored to
0.85, because only signal-class rows were counted as resolved.
"""

from __future__ import annotations

import asyncio

from legba.data.analysts.consult_evidence_floor import (
    resolved_ref_count,
    uncertainty_floor_for,
)
from legba.data.analysts.consult_round_protocol import provenance_hold


def _census(**over):
    base = {
        "live": 0, "history": 0, "web_retrieval": 0, "seed": 0,
        "unresolved": 0, "fabricated": 0, "malformed": [], "by_kind": {},
        "model_knowledge": 23, "sentences_examined": 36, "cited_total": 12,
    }
    base.update(over)
    return base


def test_twelve_real_situations_are_not_held():
    census = _census(unresolved=12, by_kind={"situation": 12})
    assert provenance_hold(census) is None


def test_a_finding_row_alone_clears_the_hold():
    assert provenance_hold(_census(by_kind={"finding": 1})) is None


def test_only_fabricated_citations_still_hold():
    census = _census(fabricated=6, cited_total=6)
    held = provenance_hold(census)
    assert held is not None
    banner, reason = held
    assert banner.startswith("> **Held:")
    assert reason


def test_nothing_cited_and_asserting_still_holds():
    assert provenance_hold(_census(cited_total=0)) is not None


class _Substrate:
    def __init__(self, out):
        self._out = out
        self.calls = 0

    async def classify_cited_refs(self, *, refs):
        self.calls += 1
        return self._out


def test_the_floor_counts_matched_platform_rows():
    sub = _Substrate({"resolved": 0, "unresolved": 12, "matched": 12, "asked": 12})
    _cls, resolved = asyncio.run(resolved_ref_count(sub, ["a"] * 12))
    assert resolved == 12
    assert uncertainty_floor_for(resolved) == 0.45
    assert sub.calls == 1


def test_the_floor_falls_back_to_resolved_for_an_old_classifier():
    sub = _Substrate({"resolved": 3, "unresolved": 0, "asked": 3})
    _cls, resolved = asyncio.run(resolved_ref_count(sub, ["a", "b", "c"]))
    assert resolved == 3
    assert uncertainty_floor_for(resolved) == 0.60
