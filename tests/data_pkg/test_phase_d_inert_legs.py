# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase D — the four built-but-INERT analysis legs, wired + proven.

Live-DB coverage (one shared pg_pool) for the four fixes the deep-review audit
found computed-but-unwritten / scored-but-unfed / 100%-pending:

  * FIX P2-1 graph_metrics sink — a graph handler run inserts a graph_metrics row
    with the right metric_kind + payload shape (the table had 0 rows / no writer).
  * FIX P2-3 signals.source_credibility — a signal from a SCORED host lands with a
    non-NULL credibility at the canonical write path; an UNKNOWN host stays NULL.
  * FIX P1-2 ACH outcome-resolution (self-consistency Brier) — a confirmed + a
    refuted hypothesis get resolved_outcome stamped via the status_transition
    resolver, and calibration_tracking then computes n>0 with a numeric Brier and
    the self_consistency_only honesty flag.
  * FIX P3-1 proposed_edges governance — a high-confidence co_occurs edge promotes
    to a nexus + status flips to 'promoted'; a low-confidence stale edge is
    rejected; a low-confidence fresh edge stays pending.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.competing_hypotheses import (
    _resolve_hypotheses_by_status_transition,
)
from legba.data.analysts.deterministic_handlers import (
    calibration_tracking as cal,
    graph_mining,
    proposed_edge_governance,
)
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext, HypothesisPayload, write_hypothesis
from legba.data.sources._contract import Signal
from legba.runtime.source_actor import lookup_source_credibility, write_canonical_signal


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


class _Deps:
    def __init__(self, pool):
        self.pg_pool = pool


# ---------------------------------------------------------------------------
# FIX P2-1 — graph_metrics sink
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_mining_writes_graph_metrics_row(pg_pool):
    """A graph_mining run inserts ONE graph_metrics row with the right shape."""
    analyst_id = f"gm_{uuid4().hex[:8]}"
    # Pre-shaped inputs (no AGE/nexus augmentation) so the run is deterministic.
    inputs = [
        {"source_entity": "AlphaPDX", "target_entity": "BetaPDX", "edge_label": "AlliedWith", "polarity": 1},
        {"source_entity": "BetaPDX", "target_entity": "GammaPDX", "edge_label": "HostileTo", "polarity": -1},
        {"source_entity": "GammaPDX", "target_entity": "AlphaPDX", "edge_label": "HostileTo", "polarity": -1},
    ]
    options = {
        "analyst_id": analyst_id,
        "analyst_version": "v1",
        "augment_from_nexuses": False,
        "augment_from_age": False,
        "target_id": "tgt.test",
    }
    result = await graph_mining.handle(inputs, options, _Deps(pg_pool))
    assert result.finding.data["sub_handler"] == "graph_mining"

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT metric_kind, analyst_id, analyst_version, payload "
            "FROM graph_metrics WHERE analyst_id = $1",
            analyst_id,
        )
    assert len(rows) == 1, "exactly one graph_metrics row per run"
    row = rows[0]
    assert row["metric_kind"] == "graph_mining"
    assert row["analyst_version"] == "v1"
    import json as _json
    payload = row["payload"] if isinstance(row["payload"], dict) else _json.loads(row["payload"])
    # The sink carries the bounded counts/scalars (not the full membership).
    for key in ("community_count", "proxy_chain_count", "node_count", "edge_count"):
        assert key in payload, f"{key} missing from graph_metrics payload"
    assert payload["node_count"] == 3


# ---------------------------------------------------------------------------
# FIX P2-3 — signals.source_credibility population at the write path
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signal_source_credibility_scored_host_and_unknown(pg_pool):
    """A signal from a SCORED host lands non-NULL; an UNKNOWN host stays NULL."""
    scored_host = f"scored-{uuid4().hex[:8]}.example"
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO source_credibility (source_host, score, tier) "
            "VALUES ($1, $2, $3) ON CONFLICT (source_host) DO UPDATE SET score = EXCLUDED.score",
            scored_host, 0.9, "gov",
        )

    src_id = f"source.test.cred_{uuid4().hex[:8]}"
    scored_sig = Signal(
        source_id=src_id,
        canonical_url=f"https://{scored_host}/article/123",
    )
    unknown_sig = Signal(
        source_id=src_id,
        canonical_url=f"https://nobody-{uuid4().hex[:8]}.invalid/x",
    )

    # Direct lookup proves the host→score resolution.
    async with pg_pool.acquire() as conn:
        got = await lookup_source_credibility(conn, scored_sig)
        miss = await lookup_source_credibility(conn, unknown_sig)
    assert got == pytest.approx(0.9)
    assert miss is None

    # End-to-end write path stamps the column.
    async with pg_pool.acquire() as conn:
        scored_id = await write_canonical_signal(
            conn, scored_sig, source_version="v", owner_tenant="t",
        )
        unknown_id = await write_canonical_signal(
            conn, unknown_sig, source_version="v", owner_tenant="t",
        )
        scored_cred = await conn.fetchval(
            "SELECT source_credibility FROM signals WHERE id = $1", scored_id,
        )
        unknown_cred = await conn.fetchval(
            "SELECT source_credibility FROM signals WHERE id = $1", unknown_id,
        )
    assert scored_cred is not None and float(scored_cred) == pytest.approx(0.9)
    assert unknown_cred is None, "unknown host degrades gracefully to NULL"


# ---------------------------------------------------------------------------
# FIX P1-2 — ACH status_transition resolution + self-consistency Brier
# ---------------------------------------------------------------------------


async def _seed_terminal_hypothesis(conn, *, analyst_id, status, balance):
    """Seed a hypothesis at a TERMINAL status, resolved_outcome left NULL."""
    ctx = AnalystContext(analyst_id=analyst_id, analyst_version="v1", run_id=uuid4())
    payload = HypothesisPayload(
        thesis=f"{status} hypothesis {uuid4().hex[:6]}",
        counter_thesis="the opposite",
        evidence_balance=balance,
        status=status,
    )
    out, _ = await write_hypothesis(conn, analyst_ctx=ctx, payload=payload, derived_from=[])
    return out.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_transition_resolution_and_self_consistency_brier(pg_pool):
    """confirmed→1 / refuted→0 get stamped resolved_by='status_transition'; the
    calibration loop then reports n>0, a numeric Brier, and the self-consistency
    honesty flag (no exogenous outcomes in the sample)."""
    from datetime import datetime, timezone

    analyst_id = f"ach_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        cid = await _seed_terminal_hypothesis(conn, analyst_id=analyst_id, status="confirmed", balance=3)
        rid = await _seed_terminal_hypothesis(conn, analyst_id=analyst_id, status="refuted", balance=-3)

        n = await _resolve_hypotheses_by_status_transition(
            conn, now=datetime.now(tz=timezone.utc),
        )
        assert n >= 2, "both terminal hypotheses resolved"

        rows = await conn.fetch(
            "SELECT status, resolved_outcome, resolved_by FROM hypotheses "
            "WHERE id = ANY($1::uuid[]) ORDER BY status",
            [cid, rid],
        )
    by_status = {r["status"]: r for r in rows}
    assert by_status["confirmed"]["resolved_outcome"] == 1
    assert by_status["refuted"]["resolved_outcome"] == 0
    assert by_status["confirmed"]["resolved_by"] == "status_transition"
    assert by_status["refuted"]["resolved_by"] == "status_transition"

    # Scope the assertion to THIS analyst (the handler aggregates globally).
    pulled = await cal._pull_resolved_claims(_Deps(pg_pool), {"lookback_days": 365})
    mine = [r for r in pulled if r["analyst_id"] == analyst_id]
    assert len(mine) == 2
    assert all(r["resolved_by"] == "status_transition" for r in mine)
    breakdown, self_only = cal._resolution_source_breakdown(mine)
    assert breakdown == {"status_transition": 2}
    assert self_only is True

    # Full handler run. The scoped self-consistency check above (over `mine`)
    # is the precise assertion; handle() aggregates GLOBALLY, so here we just
    # verify the DQ-H2 honesty axis is REPORTED (exact values depend on other
    # analysts' leaked rows in the shared test DB).
    result = await cal.handle(
        inputs=[],
        options={"lookback_days": 365, "pull_from_substrate": True,
                 "resolve_predictions": False},
        deps=_Deps(pg_pool),
    )
    data = result.finding.data
    assert data["sample_size"] >= 2
    # Honesty axis fields present (the split + fraction + pooled diagnostic).
    assert "self_consistency_fraction" in data
    assert "brier_self_consistency" in data
    assert "brier_pooled" in data and data["brier_pooled"] is not None
    assert "insufficient_exogenous" in data
    assert "resolution_sources" in data
    assert "status_transition" in data["resolution_sources"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signals_slice_inputs_do_not_suppress_the_resolved_pull(pg_pool):
    """K-2 regression: the cadence actor feeds calibration_tracking the generic
    SIGNALS slice as `inputs` (non-empty, no claimed_confidence). The handler
    must IGNORE that and still pull resolved hypotheses — the old `if not rows`
    guard saw the non-empty signals slice and skipped the pull, dropping all 50
    as invalid (sample_size=0, brier=null)."""
    from datetime import datetime, timezone

    analyst_id = f"ach_slice_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _seed_terminal_hypothesis(conn, analyst_id=analyst_id, status="confirmed", balance=3)
        await _seed_terminal_hypothesis(conn, analyst_id=analyst_id, status="refuted", balance=-3)
        await _resolve_hypotheses_by_status_transition(conn, now=datetime.now(tz=timezone.utc))

    # Signals-shaped rows (what _read_substrate_slice returns) — NO claimed_confidence.
    signal_slice = [
        {"id": str(uuid4()), "title": "some weather alert", "produced_at": datetime.now(tz=timezone.utc)},
        {"id": str(uuid4()), "title": "an earthquake", "produced_at": datetime.now(tz=timezone.utc)},
    ]
    result = await cal.handle(
        inputs=signal_slice,
        options={"lookback_days": 365, "pull_from_substrate": True},
        deps=_Deps(pg_pool),
    )
    data = result.finding.data
    # The pull ran despite the non-empty signals slice → real claims scored,
    # NOT "dropped_invalid count=2" over the signal rows. The pooled Brier (the
    # math over the pulled claims) proves rows were scored; the headline stays
    # None here because the seeded resolutions are self-consistency (DQ-H2).
    assert data["sample_size"] >= 2, "signals slice must not suppress the resolved-claims pull"
    assert data["brier_pooled"] is not None and isinstance(data["brier_pooled"], float)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_transition_never_overwrites_exogenous(pg_pool):
    """An already-exogenously-resolved hypothesis is NOT re-stamped by the
    self-consistency resolver (the resolved_outcome IS NULL guard)."""
    from datetime import datetime, timezone

    analyst_id = f"ach_exo_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        hid = await _seed_terminal_hypothesis(conn, analyst_id=analyst_id, status="confirmed", balance=3)
        # Pre-resolve EXOGENOUSLY with outcome 0 (disagreeing with the status).
        await conn.execute(
            "UPDATE hypotheses SET resolved_outcome = 0, resolved_at = NOW(), "
            "resolved_by = 'subsequent_facts' WHERE id = $1",
            hid,
        )
        await _resolve_hypotheses_by_status_transition(
            conn, now=datetime.now(tz=timezone.utc),
        )
        row = await conn.fetchrow(
            "SELECT resolved_outcome, resolved_by FROM hypotheses WHERE id = $1", hid,
        )
    assert row["resolved_outcome"] == 0, "exogenous outcome preserved"
    assert row["resolved_by"] == "subsequent_facts", "exogenous source preserved"


# ---------------------------------------------------------------------------
# FIX P3-1 — proposed_edges governance
# ---------------------------------------------------------------------------


async def _seed_proposed_edge(conn, *, src, tgt, conf, age_days=0):
    await conn.execute(
        """
        INSERT INTO proposed_edges
            (source_entity, target_entity, relationship_type, confidence,
             evidence_text, status, produced_at)
        VALUES ($1, $2, 'co_occurs', $3, $4, 'pending',
                NOW() - make_interval(days => $5))
        ON CONFLICT (lower(source_entity), lower(target_entity), relationship_type)
        DO UPDATE SET confidence = EXCLUDED.confidence, status = 'pending',
                      produced_at = EXCLUDED.produced_at
        """,
        src, tgt, conf, f"{src} and {tgt} together", age_days,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proposed_edge_governance_promote_and_reject(pg_pool):
    """High-confidence → promoted to a nexus + status flips; low+stale → rejected;
    low+fresh → stays pending."""
    tag = uuid4().hex[:8]
    hi_src, hi_tgt = f"HiA_{tag}", f"HiB_{tag}"
    lo_stale_src, lo_stale_tgt = f"LoStaleA_{tag}", f"LoStaleB_{tag}"
    lo_fresh_src, lo_fresh_tgt = f"LoFreshA_{tag}", f"LoFreshB_{tag}"

    async with pg_pool.acquire() as conn:
        await _seed_proposed_edge(conn, src=hi_src, tgt=hi_tgt, conf=0.7)
        await _seed_proposed_edge(conn, src=lo_stale_src, tgt=lo_stale_tgt, conf=0.4, age_days=45)
        await _seed_proposed_edge(conn, src=lo_fresh_src, tgt=lo_fresh_tgt, conf=0.4, age_days=0)

    result = await proposed_edge_governance.handle(
        inputs=[],
        options={
            "analyst_id": f"peg_{tag}",
            "analyst_version": "v1",
            "promote_min_confidence": 0.6,
            "reject_max_confidence": 0.45,
            "reject_min_age_days": 30,
        },
        deps=_Deps(pg_pool),
    )
    data = result.finding.data
    assert data["promoted_count"] >= 1
    assert data["rejected_count"] >= 1

    async with pg_pool.acquire() as conn:
        statuses = {
            r["source_entity"]: r["status"]
            for r in await conn.fetch(
                "SELECT source_entity, status FROM proposed_edges "
                "WHERE source_entity = ANY($1)",
                [hi_src, lo_stale_src, lo_fresh_src],
            )
        }
        # The high-confidence pair was reified into a nexus.
        nexus = await conn.fetchval(
            "SELECT count(*) FROM nexuses WHERE lower(subject)=lower($1) "
            "AND lower(object)=lower($2) AND superseded_by IS NULL",
            hi_src, hi_tgt,
        )
    assert statuses[hi_src] == "promoted"
    assert statuses[lo_stale_src] == "rejected"
    assert statuses[lo_fresh_src] == "pending", "fresh under-corroborated edge survives"
    assert int(nexus) >= 1, "promoted edge created a nexus"
