# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M-2 — every faithfulness aggregate names its judge population.

The 2026-08-02 engine review found ``judge_pipeline_version`` written twice in
``verify.py``, asserted by one test, described in two docs as the thing that
stops pre/post populations pooling — and read by nothing. Band calibration, the
Brier plane, the scorecard and the gold-set loop all pooled across the 07-30
judge swap and the 07-31 train.

These tests pin the boundary at each aggregate, INCLUDING the one that is
deliberately not split: an unexplained absence of a split is indistinguishable
from an oversight, and the next reader would either "fix" it wrongly or file it
as a bug again.
"""

from __future__ import annotations

import json

import pytest

from legba.data import correctness_axis
from legba.data.provenance.verify import JUDGE_PIPELINE_VERSION


# ---------------------------------------------------------------------------
# The Brier plane — deliberately NOT split, and it says so
# ---------------------------------------------------------------------------


def _calibration_payload(**over):
    from legba.data.analysts.deterministic_handlers import calibration_tracking as ct

    kwargs = dict(
        brier=0.2, sample_size=10, reliability_bins=[], per_analyst={},
        rolling=[], drift_z=None, drift_threshold=2.0, resolution_sources={},
        self_consistency_only=False, brier_exogenous=0.2,
        brier_self_consistency=None, brier_pooled=0.2,
        exogenous_sample_size=10, self_consistency_fraction=0.0,
        insufficient_exogenous=False, forecast_acute={}, warnings=[],
        target_id=None,
    )
    kwargs.update(over)
    return ct._build_finding(**kwargs)


def test_brier_plane_declares_its_non_split_with_a_reason():
    """Silence would read as an oversight. The payload states the decision, the
    current stamp, and the one real coupling it refuses to fake a fix for."""
    payload = _calibration_payload()
    pop = payload.data["judge_pipeline_population"]
    assert pop["split_by_judge_pipeline_version"] is False
    assert pop["current_judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    # WHY: neither scored quantity comes from the judge...
    assert pop["scored_quantities_are_judge_produced"] is False
    # ...but the verify gate does decide which claims exist to resolve, and that
    # selection effect is acknowledged rather than silently corrected for.
    assert pop["verify_gate_selection_effect"] is True
    assert "SELECTION effect" in pop["note"]
    assert "not split" in pop["note"].lower()


def test_brier_plane_declaration_reaches_the_readable_body():
    body = _calibration_payload().body
    assert "judge_pipeline_population=not_split" in body
    assert JUDGE_PIPELINE_VERSION in body


def test_brier_plane_never_carries_the_operator_correctness_axis():
    """The standing never-pool rule at the Brier boundary (labels_api)."""
    correctness_axis.assert_not_pooled(
        _calibration_payload().data, what="the calibration_tracking payload"
    )


# ---------------------------------------------------------------------------
# Band calibration — split, with priors annotated
# ---------------------------------------------------------------------------


def test_band_calibration_never_carries_the_operator_correctness_axis():
    from legba.data.analysts.deterministic_handlers import (
        band_calibration_tracker as bct,
    )

    summary = bct.summarize_claims(
        [{"dimension": "escalation", "direction": "deterioration",
          "outcome_14": "held", "outcome_28": None}],
        lookback_days=14,
        population={"judge_pipeline_version": JUDGE_PIPELINE_VERSION},
    )
    correctness_axis.assert_not_pooled(
        summary, what="the band_calibration summary"
    )
    # Bands are ordinal verdicts, not probabilities — and not correctness either.
    assert summary["no_brier"] is True


def test_band_calibration_priors_carry_their_own_n_and_never_a_combined_rate():
    from legba.data.analysts.deterministic_handlers import (
        band_calibration_tracker as bct,
    )

    blocks = bct.summarize_prior_populations(
        [
            {"judge_pipeline_version": "2026-07-31/1", "dimension": "escalation",
             "direction": "deterioration", "outcome_14": "held",
             "outcome_28": None},
            {"judge_pipeline_version": None, "dimension": "escalation",
             "direction": "deterioration", "outcome_14": "reverted",
             "outcome_28": None},
        ],
        lookback_days=14,
    )
    assert len(blocks) == 2
    for block in blocks:
        # Each block is self-contained: its own stamp, its own n, its own rates.
        assert "judge_pipeline_version" in block
        assert block["claims_total"] == 1
        assert block["horizons"]["14d"]["persistence_rate"] in (0.0, 1.0)
        # No block reports anything about the OTHER population.
        assert "combined" not in block
        assert "pooled" not in block


# ---------------------------------------------------------------------------
# The scorecard rollup — every critic row names its judge
# ---------------------------------------------------------------------------


def test_scorecard_row_carries_the_split_key():
    from legba.data.registry.v3_api import ScorecardRow

    row = ScorecardRow(
        id="1", analyst_id="escalation", overall_score=0.92,
        produced_at="2026-08-03T00:00:00+00:00",
        judge_pipeline_version="2026-08-03/1",
    )
    assert row.judge_pipeline_version == "2026-08-03/1"
    # A pre-stamp row is honestly None, never coerced to the current version —
    # the UI must be able to render it as its own series.
    legacy = ScorecardRow(
        id="2", analyst_id="escalation", overall_score=0.84,
        produced_at="2026-07-20T00:00:00+00:00",
    )
    assert legacy.judge_pipeline_version is None


def test_scorecard_route_selects_the_split_key_from_the_verification_block():
    """Guards the SQL: the stamp lives at data.data.verification, one level down
    from the critique payload root, and reading it from the wrong nesting would
    silently return NULL for every row (which is exactly what 'no reader' looked
    like)."""
    from legba.data.registry import v3_api

    # P-4 moved the route's SQL to module constants that read the critique
    # document once as ``x.d`` (the same nesting under the payload root), and
    # both reads (with and without an analyst filter) share that projection.
    proj = v3_api._SCORECARD_PROJECTION
    assert (
        "x.d->'data'->'verification'->>'judge_pipeline_version'" in proj
    ), "the scorecard rollup must read the stamp from the verification block"
    for sql in (v3_api._SCORECARD_SQL, v3_api._SCORECARD_FOR_ANALYST_SQL):
        assert proj in sql


# ---------------------------------------------------------------------------
# The scorer's own faithfulness mean
# ---------------------------------------------------------------------------


def test_unit_scorer_faithfulness_sql_filters_and_groups_on_the_split_key():
    """2026-08-29 — the filter is now the lineage-declared POOL rather than the
    single head stamp (``judge_pipeline_version.poolable_stamps``), so the
    parameter is a ``text[]`` and the complement is ``NOT (... = ANY(...))``.
    The PROPERTY this test has always guarded is unchanged and is what is
    asserted below: the headline is filtered to one declared population, the
    priors are grouped so each excluded pipeline keeps its own mean, and the
    excluded counter reads the exact complement of the headline."""
    from legba.data.analysts.deterministic_handlers import (
        unit_correctness_scorer as ucs,
    )

    # The headline mean is FILTERED to one declared population...
    assert "judge_pipeline_version' = ANY($3::text[])" in ucs._FAITHFULNESS_SQL
    # ...and the priors are GROUPED by it, so each gets its own mean rather than
    # collapsing into a single excluded count.
    assert "GROUP BY 1" in ucs._FAITHFULNESS_PRIORS_SQL
    assert "avg(confidence)" in ucs._FAITHFULNESS_PRIORS_SQL
    # The excluded counter and the priors read the SAME complement, so the two
    # can be reconciled by a reader. COALESCE keeps a pre-stamp NULL row counted
    # as excluded instead of vanishing into three-valued logic.
    for sql in (ucs._FAITHFULNESS_EXCLUDED_SQL, ucs._FAITHFULNESS_PRIORS_SQL):
        assert "COALESCE(" in sql and "= ANY($3::text[])" in sql
        assert "NOT (COALESCE(" in sql


# ---------------------------------------------------------------------------
# P-4 — GET /eval/scorecard: bounded reads, the response unchanged
# ---------------------------------------------------------------------------
#
# The route took up to 19.4 s on live: it evaluated seven ``data->...``
# projections (each a fresh detoast) on every critique before a top-N sort kept
# 500. It now orders on the narrow columns and reads the document only for the
# rows it returns. The reference below is the pre-P-4 SQL verbatim; the
# container test proves the route answers exactly what it answered.

_PRE_P4_SCORECARD_SQL = """
SELECT id::text AS id,
       data->>'analyzed_analyst_id'      AS analyst_id,
       data->>'analyzed_analyst_version' AS analyst_version,
       COALESCE(data->'scores', '{}'::jsonb) AS scores,
       COALESCE((data->>'overall_score')::float8, 0.0) AS overall_score,
       created_at AS produced_at, data->'data'->'verification'->>'score_state' AS score_state,
       data->'data'->'verification'->>'judge_pipeline_version'
           AS judge_pipeline_version
  FROM public.analyst_outputs
 WHERE kind = 'critique'
   AND data->>'analyzed_analyst_id' IS NOT NULL
   AND ($1::text IS NULL OR data->>'analyzed_analyst_id' = $1)
 ORDER BY created_at DESC
 LIMIT $2
"""

_SCORECARD_ROW_KEYS = {
    "id", "analyst_id", "analyst_version", "scores", "overall_score",
    "ground_truth_accuracy", "produced_at", "judge_pipeline_version",
    "score_state",
}


class _ScorecardConn:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


def _scorecard_client(conn):
    import contextlib
    import os
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from legba.data.registry.api import API_TOKEN_ENV
    from legba.data.registry.v3_api import build_v3_router

    os.environ.pop(API_TOKEN_ENV, None)

    @contextlib.asynccontextmanager
    async def acquire():
        yield conn

    deps = SimpleNamespace(
        descriptor_registry=SimpleNamespace(pg=SimpleNamespace(acquire=acquire)),
    )
    app = FastAPI()
    app.include_router(build_v3_router(deps), prefix="/api/v1/v3")  # type: ignore[arg-type]
    return TestClient(app)


def test_scorecard_route_picks_the_bounded_read_and_keeps_its_shape():
    from datetime import datetime, timezone

    from legba.data.registry import v3_api

    row = {
        "id": "c1", "analyst_id": "escalation", "analyst_version": "v3",
        "scores": json.dumps({"evidence": 0.8, "calibration": 1, "note": "x"}),
        "overall_score": 0.9,
        "produced_at": datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc),
        "score_state": "scored", "judge_pipeline_version": "2026-08-03/1",
    }
    conn = _ScorecardConn([row])
    client = _scorecard_client(conn)

    r = client.get("/api/v1/v3/eval/scorecard", params={"limit": 500})
    assert r.status_code == 200, r.text
    assert conn.calls == [(v3_api._SCORECARD_SQL, (500,))]
    body = r.json()
    assert len(body) == 1 and set(body[0]) == _SCORECARD_ROW_KEYS
    assert body[0] == {
        "id": "c1", "analyst_id": "escalation", "analyst_version": "v3",
        # non-numeric axis values are dropped, ints become floats
        "scores": {"evidence": 0.8, "calibration": 1.0},
        "overall_score": 0.9, "ground_truth_accuracy": None,
        "produced_at": "2026-11-14T06:00:00+00:00",
        "judge_pipeline_version": "2026-08-03/1", "score_state": "scored",
    }

    conn.calls.clear()
    r = client.get(
        "/api/v1/v3/eval/scorecard",
        params={"analyst_id": "escalation", "limit": 7},
    )
    assert r.status_code == 200, r.text
    assert conn.calls == [(v3_api._SCORECARD_FOR_ANALYST_SQL, ("escalation", 7))]


def test_scorecard_sql_never_projects_the_document_before_the_limit():
    """The shape of the fix: the ordered set is chosen from (id, created_at)
    alone, and the document is read once per returned row, by primary key."""
    from legba.data.registry import v3_api

    for sql in (v3_api._SCORECARD_SQL, v3_api._SCORECARD_FOR_ANALYST_SQL):
        assert "SELECT id, created_at" in sql
        assert "jsonb_path_query_first(ao.data, '$') AS d" in sql
        assert "WHERE ao.id = o.id" in sql and "OFFSET 0) x" in sql
        assert "ORDER BY o.created_at DESC, o.id DESC" in sql
    # The unfiltered read never evaluates a data-> filter per critique.
    inner = v3_api._SCORECARD_SQL.split(") o")[0]
    assert "data->" not in inner
    # The filtered read's ONE per-row detoast is the key match.
    inner_f = v3_api._SCORECARD_FOR_ANALYST_SQL.split(") o")[0]
    assert inner_f.count("data->") == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scorecard_route_answers_what_the_pre_p4_sql_answered(migrated_pg):
    """Real SQL on the isolated stack: ties on created_at (one transaction's
    ``now()``), a critique without an analyzed analyst, a null overall score,
    non-numeric axes, an absent verification block, and limits that cut inside
    a tie — the route returns the pre-P-4 rows (ordered newest first, ``id``
    breaking ties, which the old ORDER BY left to the plan)."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from httpx import ASGITransport, AsyncClient

    import os

    from legba.data.postgres import PostgresStore
    from legba.data.registry.api import API_TOKEN_ENV

    os.environ.pop(API_TOKEN_ENV, None)
    store = PostgresStore(migrated_pg)
    await store.connect()
    try:
        aid = f"p4_sc_{uuid.uuid4().hex[:8]}"
        other = f"p4_sc_other_{uuid.uuid4().hex[:8]}"
        base = datetime(2031, 1, 1, tzinfo=timezone.utc)  # newest in the table
        docs = []
        for i in range(12):
            d: dict = {
                "analyzed_analyst_id": aid if i % 4 else other,
                "analyzed_analyst_version": f"v{i}",
                "scores": {"evidence": i / 10, "note": "n", "k": i},
                "data": {"verification": {
                    "score_state": "scored" if i % 3 else "carried_ungraded",
                    "judge_pipeline_version": None if i == 5 else "2026-08-03/1",
                }},
            }
            if i != 2:
                d["overall_score"] = i / 12
            if i == 7:
                d.pop("data")
            if i == 9:
                d.pop("analyzed_analyst_id")  # excluded from every read
            # pairs share created_at: (0,1) (2,3) ... -> ties everywhere
            docs.append((uuid.uuid4(), d, base - timedelta(minutes=i // 2)))
        async with store.acquire() as conn:
            for cid, d, at in docs:
                await conn.execute(
                    """
                    INSERT INTO analyst_outputs
                        (id, kind, title, body, analyst_id, analyst_version,
                         produced_at, created_at, derived_from, schema_uri, data)
                    VALUES ($1, 'critique', 'Faithfulness verify: p4', '',
                            'critic', 'v1', $2, $2, '{}'::uuid[],
                            'iglu:legba/critique/jsonschema/1-0-0', $3::jsonb)
                    """,
                    cid, at, json.dumps(d),
                )

        from types import SimpleNamespace

        from fastapi import FastAPI

        from legba.data.registry.v3_api import build_v3_router

        app = FastAPI()
        app.include_router(
            build_v3_router(SimpleNamespace(  # type: ignore[arg-type]
                descriptor_registry=SimpleNamespace(pg=store),
            )),
            prefix="/api/v1/v3",
        )

        def _norm(rows):
            return sorted(
                rows, key=lambda r: (r["produced_at"], r["id"]), reverse=True,
            )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t",
        ) as client:
            for analyst, limit in (
                (None, 500), (None, 1), (None, 3), (None, 2000),
                (aid, 500), (aid, 1), (aid, 3), (other, 500), ("nobody", 5),
            ):
                params = {"limit": limit}
                if analyst is not None:
                    params["analyst_id"] = analyst
                r = await client.get("/api/v1/v3/eval/scorecard", params=params)
                assert r.status_code == 200, r.text
                got = r.json()
                async with store.acquire() as conn:
                    ref = await conn.fetch(
                        _PRE_P4_SCORECARD_SQL, analyst, limit * 4 + 10,
                    )
                # The old ORDER BY left created_at ties to the plan; compare
                # against its rows ordered (created_at, id) DESC, cut at limit.
                exp = _norm([
                    {
                        "id": x["id"], "analyst_id": x["analyst_id"],
                        "analyst_version": x["analyst_version"],
                        "scores": {
                            k: float(v)
                            for k, v in (
                                json.loads(x["scores"])
                                if isinstance(x["scores"], str)
                                else dict(x["scores"])
                            ).items()
                            if isinstance(v, (int, float))
                        },
                        "overall_score": float(x["overall_score"]),
                        "ground_truth_accuracy": None,
                        "produced_at": x["produced_at"].isoformat(),
                        "judge_pipeline_version": x["judge_pipeline_version"],
                        "score_state": x["score_state"],
                    }
                    for x in ref
                ])[:limit]
                assert got == exp, (analyst, limit)
                if analyst == aid and limit == 500:
                    assert len(got) == 8  # 9 of 12 are aid's; #9 has no id
                    assert all(set(g) == _SCORECARD_ROW_KEYS for g in got)
    finally:
        async with store.acquire() as conn:
            await conn.execute(
                "DELETE FROM analyst_outputs WHERE kind = 'critique' "
                "AND data->>'analyzed_analyst_id' IN ($1, $2) "
                "OR (kind = 'critique' AND created_at >= $3 "
                "    AND title = 'Faithfulness verify: p4')",
                aid, other, datetime(2030, 12, 31, tzinfo=timezone.utc),
            )
        await store.close()
