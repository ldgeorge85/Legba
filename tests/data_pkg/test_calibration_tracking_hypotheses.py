# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PIECE C — the dormant calibration_tracking loop, closed over ACH hypotheses.

The calibration_tracking handler is BUILT but was unregistered (no producer). The
ACH competing_hypotheses kind resolves hypotheses EXOGENOUSLY (the
``resolved_outcome`` column, migration 0038 — graded against subsequent facts /
an operator label, NOT the hypothesis's own ``evidence_balance``), so the
calibration loop has non-circular outcomes to Brier-score. This covers:

  * the handler runs over SEEDED exogenously-resolved hypotheses and produces a
    Brier score + a non-zero sample (i.e. it actually pulled the hypotheses table
    by ``resolved_outcome`` — the closure the deep review names as dormant);
  * pure-logic Brier / reliability-bin math (no DB).
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import SUB_HANDLERS, OUTPUT_KIND_BY_SUB_HANDLER
from legba.data.analysts.deterministic_handlers import calibration_tracking as cal
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext, HypothesisPayload, write_hypothesis


# ---------------------------------------------------------------------------
# Pure-logic unit tests (no DB)
# ---------------------------------------------------------------------------


def test_registered_in_dispatch_table():
    assert "calibration_tracking" in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER["calibration_tracking"].value == "finding"


def test_brier_perfect_and_baseline():
    # Perfectly calibrated (claim matches outcome) → 0.
    perfect = [
        {"claimed_confidence": 1.0, "outcome": 1},
        {"claimed_confidence": 0.0, "outcome": 0},
    ]
    assert cal._brier(perfect) == 0.0
    # 50/50 guesses → 0.25 baseline.
    baseline = [
        {"claimed_confidence": 0.5, "outcome": 1},
        {"claimed_confidence": 0.5, "outcome": 0},
    ]
    assert cal._brier(baseline) == 0.25
    assert cal._brier([]) is None


# ---------------------------------------------------------------------------
# Live-DB: the loop runs over seeded resolved hypotheses
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    yield pool
    await pool.close()


class _Deps:
    def __init__(self, pool):
        self.pg_pool = pool
        self.nats_publish = None


async def _seed_resolved_hypothesis(conn, *, analyst_id, outcome, balance):
    """Seed a hypothesis with an EXOGENOUS resolved_outcome (the 0038 column),
    NOT a status — the calibration pull reads resolved_outcome to avoid the
    circular Brier."""
    ctx = AnalystContext(analyst_id=analyst_id, analyst_version="v1", run_id=uuid4())
    payload = HypothesisPayload(
        thesis=f"outcome{outcome} hypothesis {uuid4().hex[:6]}",
        counter_thesis="the opposite",
        evidence_balance=balance,
        status="active",
    )
    out, _ = await write_hypothesis(conn, analyst_ctx=ctx, payload=payload, derived_from=[])
    await conn.execute(
        "UPDATE hypotheses SET resolved_outcome = $2, resolved_at = NOW(), "
        "resolved_by = 'subsequent_facts' WHERE id = $1",
        out.id, int(outcome),
    )
    return out.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_calibration_runs_over_seeded_hypotheses(pg_pool):
    """The calibration loop pulls EXOGENOUSLY-resolved hypotheses (resolved_outcome
    1/0), Brier-scores them, and reports a non-empty sample — proving the closure
    runs over the ACH outputs without the circular status read."""
    analyst_id = f"cal_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _seed_resolved_hypothesis(conn, analyst_id=analyst_id, outcome=1, balance=3)
        await _seed_resolved_hypothesis(conn, analyst_id=analyst_id, outcome=0, balance=-3)
        await _seed_resolved_hypothesis(conn, analyst_id=analyst_id, outcome=1, balance=2)

    # Pull directly first so the assertion is scoped to THIS analyst (the handler
    # aggregates globally, which other tests' leaked rows would otherwise dilute).
    pulled = await cal._pull_resolved_claims(_Deps(pg_pool), {"lookback_days": 365})
    mine = [r for r in pulled if r["analyst_id"] == analyst_id]
    assert len(mine) == 3, "all three resolved hypotheses must be pulled"
    assert all(r["claim_kind"] == "hypothesis" for r in mine)
    # Exogenous outcomes read straight off resolved_outcome (1/1/0).
    assert sorted(r["outcome"] for r in mine) == [0, 1, 1]
    # Claimed confidence derived from |evidence_balance| > 0.5.
    assert all(r["claimed_confidence"] > 0.5 for r in mine)

    # The full handler runs over a real sample. D16 — `subsequent_facts` is the
    # WEAK/LEXICAL tier (a substring direction proxy), so these resolutions are
    # DEMOTED out of the headline exogenous Brier: they land in the weak bucket,
    # the headline stays withheld (insufficient_exogenous, no falsifiable rows),
    # and the weak Brier carries the number.
    result = await cal.handle(
        inputs=[],
        options={"lookback_days": 365, "pull_from_substrate": True,
                 "min_exogenous": 1, "resolve_predictions": False},
        deps=_Deps(pg_pool),
    )
    data = result.finding.data
    assert data["sub_handler"] == "calibration_tracking"
    assert data["sample_size"] >= 3

    # D16: subsequent_facts is WEAK — never headline. Asserted on THIS test's
    # own rows, because the headline is a property of the whole substrate.
    #
    # This used to read `assert data["brier"] is None`, which says "nobody in
    # the entire shared DB has a falsifiable resolution". That is a statement
    # about the suite, not about `subsequent_facts`, and under `--randomly-seed`
    # a sibling file seeded an exogenous resolution first, the headline computed
    # legitimately, and this failed at `0.64 is None` — while the three rows it
    # was actually about had been tiered exactly right. The scoped form below is
    # also the stronger claim: a headline of None is equally consistent with a
    # sample that was empty, or misclassified into self-consistency, so the old
    # assertion could not tell "demoted to weak" from "lost".
    assert not any(cal._is_exogenous(r) for r in mine), (
        "a lexical subsequent_facts resolution must never count as exogenous"
    )
    assert all(cal._is_weak_tier(r) for r in mine), (
        "all three must land in the WEAK tier, not self-consistency"
    )
    # The weak bucket is a lower bound over the shared substrate, which is
    # order-proof in the direction that matters: my three are IN it.
    assert data["weak_sample_size"] >= 3
    assert data["brier_weak"] is not None
    # Per-analyst breakdown carries our analyst's Brier.
    assert analyst_id in data["per_analyst"]
