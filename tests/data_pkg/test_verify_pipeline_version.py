# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The JUDGE PIPELINE VERSION stamp (2026-07-31) — the population SPLIT key.

The verify gate is the product's keystone, so every structural change to it
ships behind ONE version stamp on the critique (the MATCHER_VERSION idiom).
Anything reading faithfulness history — band calibration, the gold-set loop, the
correctness scorer, the scorecard, the two-panel readout's own dossier query —
partitions on it, so critiques graded under different verify pipelines are never
POOLED.

That matters for THIS train specifically: V-F + V-C + V-D + V-B are expected to
shift mean faithfulness UPWARD, and that shift is a MEASUREMENT CORRECTION (the
readout established that both judges over-fail, so the prior mean UNDERSTATED
true faithfulness), not findings getting better. The stamp is what makes that
statement checkable instead of asserted.
"""

from __future__ import annotations

import re
from uuid import uuid4

from legba.data.provenance.models import CritiquePayload
from legba.data.provenance.verify import (
    JUDGE_PIPELINE_VERSION,
    build_faithfulness_critique_payload,
    verify_finding_faithfulness,
)


def test_version_value_and_shape() -> None:
    """ONE bump per train, ``<train date>/<n>``."""
    assert JUDGE_PIPELINE_VERSION == "2026-07-31/1"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}/\d+", JUDGE_PIPELINE_VERSION)


async def test_stamped_on_every_critique(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body="Alpha struck Bravo base on Monday [1].\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4())}],
    )
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    verification = payload["data"]["verification"]
    assert verification["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    # The block still validates as a CritiquePayload (extra='forbid' at the top
    # level; ``data`` is open JSONB, which is where the stamp lives).
    CritiquePayload.model_validate(payload)


async def test_stamped_on_the_trace_envelope_too(monkeypatch) -> None:
    """``report.as_dict()`` is what the actor returns into the run trace — it
    records which verify pipeline produced the number, not only the critique."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(body="", citations=[])
    assert report.as_dict()["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION


def test_one_stamp_for_the_whole_train() -> None:
    """A single module constant — a per-call or per-kind stamp would let two
    findings from the same deploy land in different populations."""
    import legba.data.provenance.verify as V

    assert isinstance(V.JUDGE_PIPELINE_VERSION, str)
    src = __import__("inspect").getsource(V)
    # Exactly one assignment; every other occurrence is a read.
    assert src.count("JUDGE_PIPELINE_VERSION = ") == 1
