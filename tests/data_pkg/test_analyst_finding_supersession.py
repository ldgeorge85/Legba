# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-FS tests for the ``finding_supersession`` deterministic sub-handler.

Two layers (mirrors the P-09 cross_source_dedup test shape):

  * **Synthetic** (``deps=None``) — situation-signature clustering + deterministic
    latest selection over pre-shaped finding rows. No substrate needed; runs in
    every CI lane.
  * **Live pivot DB** (env-gated, dev-rig default) — the P-FS acceptance: insert
    two near-duplicate findings for the same situation into the
    ``legba_pivot_test`` ``analyst_outputs`` table, run the handler, and assert
    it links 1 supersession (both rows preserved) and a "latest per situation"
    query returns exactly 1. Skips cleanly when the dev rig is down.

The dispatcher contract (registered in
:data:`legba.data.analysts.deterministic.SUB_HANDLERS`) is asserted too.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    run_method,
)
from legba.data.analysts.deterministic_handlers import finding_supersession
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "finding_supersession"


# ---------------------------------------------------------------------------
# Registration — P-FS demands a real registered deterministic sub-handler
# ---------------------------------------------------------------------------


def test_finding_supersession_registered():
    assert SUB in SUB_HANDLERS, "finding_supersession missing from SUB_HANDLERS"
    assert SUB in OUTPUT_KIND_BY_SUB_HANDLER
    assert SUB_HANDLERS[SUB] is finding_supersession.handle


# ---------------------------------------------------------------------------
# Signature derivation
# ---------------------------------------------------------------------------


def test_derive_signature_explicit_id_wins():
    sig = finding_supersession.derive_signature({"situation_id": "sit-42", "actors": ["x"]})
    assert sig == "sit:sit-42"


def test_derive_signature_topic_level_and_order_invariant():
    # Signatures are TOPIC-LEVEL (entity K=0) so findings about the same
    # evolving event cluster even as their entity tails churn — the lifecycle
    # decay then makes the situation breathe. Still entity-gated + case-folded.
    a = finding_supersession.derive_signature(
        {"category": "Conflict", "actors": ["Russia", "Ukraine"]})
    b = finding_supersession.derive_signature(
        {"category": "conflict", "actors": ["ukraine", "RUSSIA"]})
    assert a == b == "sig:conflict"
    # Different entity lists, SAME topic → SAME signature (the loosening that
    # makes situations actually form, vs the old full-entity-set key).
    c = finding_supersession.derive_signature(
        {"category": "conflict", "actors": ["NATO", "Poland"]})
    assert c == "sig:conflict"


def test_derive_signature_none_without_entities():
    # A bare summary finding (no entities) must NOT cluster.
    assert finding_supersession.derive_signature({"category": "metrics"}) is None
    assert finding_supersession.derive_signature({}) is None


def test_narrate_lifts_entities_enabling_signature():
    """inline_target._narrate lifts the LLM's entity tags into
    ``data.key_entities`` + sets ``data.category`` — the missing wire that left
    every assessor finding with a None signature (entities lived only in
    free-text ``tags``, which derive_signature does not read). After narration
    the finding clusters.
    """
    from legba.data.analysts.inline_target import _narrate

    raw = FindingPayload(
        title="M6.2 earthquake near San Juan",
        body="A strong quake struck San Juan Province.",
        confidence=0.7,
        tags=["earthquake", "Argentina", "San Juan Province", "2026"],
        data={"raw_llm_response": "..."},
    )
    narrated = _narrate(raw, target_id="country_g20_ar", analyst_id="country_assessor")

    # FindingPayload is extra='forbid', so the structured fields land in the
    # inner payload data (the row's data->'data'), not the top level.
    entities = narrated.data["key_entities"]
    assert "Argentina" in entities and "San Juan Province" in entities
    assert "2026" not in entities  # bare-year junk dropped
    assert not any(t.startswith(("target:", "analyst:")) for t in entities)
    assert narrated.data["category"] == "country_g20_ar"

    # Production passes the FULL finding dump (the analyst_outputs.data column =
    # payload.model_dump) to derive_signature — the nested-read finds the inner
    # entities and the previously-None signature now derives.
    dump = narrated.model_dump(mode="python")
    sig = finding_supersession.derive_signature(dump)
    assert sig is not None
    # Topic-level signature (entity K=0) — keyed on the assessor's category
    # (= target_id), so successive Argentina findings cluster as one situation.
    assert sig == "sig:country_g20_ar"

    # Scoping guard: a deterministic METRICS finding (inner = counts only) must
    # still NOT cluster — its inner dict has no entity keys.
    metrics_dump = {
        "title": "dedup metrics", "tags": ["metrics"],
        "data": {"canonical_count": 5, "sub_handler": "cross_source_dedup"},
    }
    assert finding_supersession.derive_signature(metrics_dump) is None


# ---------------------------------------------------------------------------
# Synthetic path — clustering + deterministic latest
# ---------------------------------------------------------------------------


async def test_synthetic_supersedes_near_dup_for_same_situation():
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    f_old, f_new, f_other = uuid4(), uuid4(), uuid4()
    inputs = [
        # two near-dups for the same situation (same entities+topic) — newest wins
        {"id": str(f_old), "produced_at": t0,
         "data": {"category": "conflict", "actors": ["Russia", "Ukraine"]}},
        {"id": str(f_new), "produced_at": t0 + timedelta(hours=6),
         "data": {"category": "conflict", "actors": ["ukraine", "russia"]}},
        # a different situation — not clustered
        {"id": str(f_other), "produced_at": t0,
         "data": {"category": "economy", "actors": ["Brazil"]}},
    ]
    result = await run_method(
        inputs, {"sub_handler": SUB, "analyst_id": "fs", "run_id": uuid4()}, None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["clustered_count"] == 1
    assert data["superseded_count"] == 1
    # one clustered situation (russia/ukraine) + one singleton (brazil) = 2 live
    assert data["latest_count"] == 2
    one = data["clusters"][0]
    assert one["latest_finding_id"] == str(f_new)  # newest produced_at
    assert one["superseded_finding_ids"] == [str(f_old)]
    assert one["reason"] == "signature_match"
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


def test_composition_analyst_findings_are_not_clustered():
    """DQ P6 — a COMPOSITION / META producer's report is a receipt, not an
    evolving-situation finding: it must be EXCLUDED from clustering (and thus
    never gets a situation_signature stamped / never mints a situation), while a
    real unit finding for the same topic IS clustered."""
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    unit_a, unit_b = uuid4(), uuid4()
    comp_a, comp_b = uuid4(), uuid4()
    findings = [
        # two near-dup UNIT findings (same analyst) — SHOULD cluster
        {"id": str(unit_a), "produced_at": t0, "analyst_id": "internal_stability",
         "data": {"category": "country_g20_us", "actors": ["us"]}},
        {"id": str(unit_b), "produced_at": t0 + timedelta(hours=1),
         "analyst_id": "internal_stability",
         "data": {"category": "country_g20_us", "actors": ["us"]}},
        # two near-dup COMPOSITION findings — must be EXCLUDED entirely
        {"id": str(comp_a), "produced_at": t0, "analyst_id": "country_composition",
         "data": {"category": "country_g20_us", "actors": ["us"]}},
        {"id": str(comp_b), "produced_at": t0 + timedelta(hours=1),
         "analyst_id": "country_composition",
         "data": {"category": "country_g20_us", "actors": ["us"]}},
    ]
    groups = finding_supersession._cluster(findings, sub_handler_fallback=None)
    # exactly one cluster, and it is the UNIT one — no country_composition cluster
    assert len(groups) == 1
    clustered_ids = {str(r["id"]) for rows in groups.values() for r in rows}
    assert clustered_ids == {str(unit_a), str(unit_b)}
    assert str(comp_a) not in clustered_ids and str(comp_b) not in clustered_ids
    assert "country_composition" in finding_supersession._COMPOSITION_ANALYST_IDS
    assert "world_assessor" in finding_supersession._COMPOSITION_ANALYST_IDS


async def test_synthetic_explicit_situation_id_clusters():
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    f1, f2 = uuid4(), uuid4()
    inputs = [
        {"id": str(f1), "produced_at": t0, "data": {"situation_id": "S1"}},
        {"id": str(f2), "produced_at": t0 + timedelta(hours=1), "data": {"situation_id": "S1"}},
    ]
    result = await run_method(inputs, {"sub_handler": SUB}, None)
    data = result.finding.data
    assert data["clustered_count"] == 1
    assert data["superseded_count"] == 1
    assert data["clusters"][0]["reason"] == "situation_id"
    assert data["clusters"][0]["latest_finding_id"] == str(f2)


async def test_synthetic_no_dups_supersedes_nothing():
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    inputs = [
        {"id": str(uuid4()), "produced_at": t0, "data": {"category": "a", "actors": ["x"]}},
        {"id": str(uuid4()), "produced_at": t0, "data": {"category": "b", "actors": ["y"]}},
    ]
    result = await run_method(inputs, {"sub_handler": SUB}, None)
    assert result.finding.data["clustered_count"] == 0
    assert result.finding.data["superseded_count"] == 0


async def test_synthetic_summary_findings_never_cluster():
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    inputs = [
        {"id": str(uuid4()), "produced_at": t0, "data": {"category": "metrics"}},
        {"id": str(uuid4()), "produced_at": t0, "data": {"category": "metrics"}},
    ]
    result = await run_method(inputs, {"sub_handler": SUB}, None)
    assert result.finding.data["superseded_count"] == 0


# ---------------------------------------------------------------------------
# Live pivot-DB acceptance (env-gated)
# ---------------------------------------------------------------------------


_PIVOT_DB = {
    "host": os.environ.get("LEGBA_PIVOT_PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("LEGBA_PIVOT_PG_PORT", "5432")),
    "user": os.environ.get("LEGBA_PIVOT_PG_USER", "legba"),
    "password": os.environ.get("LEGBA_PIVOT_PG_PASSWORD", "legba"),
    "database": os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test"),
}


@pytest.fixture
async def pivot_pool():
    """asyncpg pool against the pivot substrate DB; skip if unreachable."""
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(min_size=1, max_size=4, **_PIVOT_DB)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"legba_pivot_test unreachable: {exc}")
    # Confirm the P-FS substrate (0025) is present.
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT to_regclass('finding_supersessions')")
        has_col = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='analyst_outputs' AND column_name='superseded_by'"
        )
    if not ok or not has_col:
        await pool.close()
        pytest.skip("P-FS substrate (finding_supersessions / superseded_by) not present")
    yield pool
    await pool.close()


async def _insert_finding(conn, *, fid, title, data, produced_at, analyst_id):
    await conn.execute(
        """INSERT INTO analyst_outputs
               (id, kind, title, body, confidence, data, analyst_id,
                produced_at, schema_uri)
           VALUES ($1,'finding',$2,'',1.0,$3::jsonb,$4,$5,
                   'iglu:legba/finding/jsonschema/1-0-0')""",
        fid, title, json.dumps(data), analyst_id, produced_at,
    )


async def test_live_pivot_acceptance(pivot_pool):
    """P-FS acceptance — two near-dup findings for the same situation =>
    1 latest/canonical + 1 supersession link (both rows preserved), and a
    'latest per situation' query returns exactly 1. Rerun idempotent."""
    from legba.runtime.deps import StandardDeps

    tenant = f"pfs_{uuid4().hex[:8]}"
    analyst_id = f"evolving_analyst_{tenant}"
    produced_by = "test_supersession_pfs"
    f_old, f_new = uuid4(), uuid4()
    t0 = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)

    base_entities = {"category": "conflict", "actors": ["Russia", "Ukraine"],
                     "owner_tenant": tenant}

    async with pivot_pool.acquire() as conn:
        # cycle N: first assessment
        await _insert_finding(
            conn, fid=f_old, title="Escalation in eastern front (assessment v1)",
            data=dict(base_entities, cycle=1), produced_at=t0, analyst_id=analyst_id)
        # cycle N+1: near-dup re-assessment of the SAME situation (different
        # title, same entities+topic) — this is the live duplicate problem.
        await _insert_finding(
            conn, fid=f_new, title="Continued escalation eastern front (v2)",
            data=dict(base_entities, cycle=2), produced_at=t0 + timedelta(hours=6),
            analyst_id=analyst_id)

    deps = StandardDeps(pg_pool=pivot_pool)
    try:
        result = await run_method(
            [], {"sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
                 "scope_analyst_id": analyst_id, "owner_tenant": tenant}, deps,
        )
        data = result.finding.data
        assert data["clustered_count"] == 1, data
        assert data["superseded_count"] == 1, data

        async with pivot_pool.acquire() as conn:
            # BOTH finding rows survive — never destructive collapse.
            raw = await conn.fetchval(
                "SELECT count(*) FROM analyst_outputs WHERE analyst_id=$1", analyst_id)
            assert raw == 2

            # one supersession link: old superseded by new.
            links = await conn.fetch(
                "SELECT superseded_finding_id, superseding_finding_id, reason, score "
                "FROM finding_supersessions WHERE produced_by=$1", produced_by)
            assert len(links) == 1
            assert str(links[0]["superseded_finding_id"]) == str(f_old)
            assert str(links[0]["superseding_finding_id"]) == str(f_new)  # newest wins
            assert links[0]["reason"] == "signature_match"
            assert abs(links[0]["score"] - 1.0) < 1e-6

            # superseded row points at the superseding one; latest has no pointer.
            old_ptr = await conn.fetchval(
                "SELECT superseded_by FROM analyst_outputs WHERE id=$1", f_old)
            new_ptr = await conn.fetchval(
                "SELECT superseded_by FROM analyst_outputs WHERE id=$1", f_new)
            assert str(old_ptr) == str(f_new)
            assert new_ptr is None

            # both rows share the same situation_signature.
            sig_old = await conn.fetchval(
                "SELECT situation_signature FROM analyst_outputs WHERE id=$1", f_old)
            sig_new = await conn.fetchval(
                "SELECT situation_signature FROM analyst_outputs WHERE id=$1", f_new)
            assert sig_old == sig_new and sig_old is not None

            # THE acceptance: 'latest per situation' returns exactly 1.
            latest = await conn.fetch(
                """SELECT id FROM analyst_outputs
                   WHERE kind='finding' AND situation_signature=$1
                     AND superseded_by IS NULL""",
                sig_new)
            assert len(latest) == 1
            assert str(latest[0]["id"]) == str(f_new)

        # Rerun is idempotent — links 0 new, never collapses. The old finding is
        # already superseded so it drops out of the eligible set.
        rerun = await run_method(
            [], {"sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
                 "scope_analyst_id": analyst_id, "owner_tenant": tenant}, deps,
        )
        assert rerun.finding.data["superseded_count"] == 0
        async with pivot_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM finding_supersessions WHERE produced_by=$1",
                produced_by) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM analyst_outputs WHERE analyst_id=$1",
                analyst_id) == 2
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM finding_supersessions WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM analyst_outputs WHERE analyst_id=$1", analyst_id)


async def test_live_third_cycle_supersedes_chain(pivot_pool):
    """A third re-assessment supersedes the second; latest-per-situation still 1.

    Exercises the 'evolving situation across many cycles' core of the risk item.
    """
    from legba.runtime.deps import StandardDeps

    tenant = f"pfs3_{uuid4().hex[:8]}"
    analyst_id = f"evolving3_{tenant}"
    produced_by = "test_supersession_pfs3"
    f1, f2, f3 = uuid4(), uuid4(), uuid4()
    t0 = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)
    base = {"category": "conflict", "actors": ["Sudan", "RSF"], "owner_tenant": tenant}

    deps = StandardDeps(pg_pool=pivot_pool)
    try:
        async with pivot_pool.acquire() as conn:
            await _insert_finding(conn, fid=f1, title="Sudan v1",
                                  data=dict(base, cycle=1), produced_at=t0,
                                  analyst_id=analyst_id)
            await _insert_finding(conn, fid=f2, title="Sudan v2",
                                  data=dict(base, cycle=2),
                                  produced_at=t0 + timedelta(hours=2),
                                  analyst_id=analyst_id)
        # first run: f1 superseded by f2.
        await run_method(
            [], {"sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
                 "scope_analyst_id": analyst_id, "owner_tenant": tenant}, deps)
        # cycle 3 arrives.
        async with pivot_pool.acquire() as conn:
            await _insert_finding(conn, fid=f3, title="Sudan v3",
                                  data=dict(base, cycle=3),
                                  produced_at=t0 + timedelta(hours=4),
                                  analyst_id=analyst_id)
        # second run: f3 is the new latest, f2 gets superseded by it.
        r2 = await run_method(
            [], {"sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
                 "scope_analyst_id": analyst_id, "owner_tenant": tenant}, deps)
        assert r2.finding.data["superseded_count"] == 1

        async with pivot_pool.acquire() as conn:
            # all three rows preserved.
            assert await conn.fetchval(
                "SELECT count(*) FROM analyst_outputs WHERE analyst_id=$1",
                analyst_id) == 3
            sig = await conn.fetchval(
                "SELECT situation_signature FROM analyst_outputs WHERE id=$1", f3)
            # exactly one latest for the situation, and it is f3.
            latest = await conn.fetch(
                "SELECT id FROM analyst_outputs WHERE kind='finding' "
                "AND situation_signature=$1 AND superseded_by IS NULL", sig)
            assert len(latest) == 1
            assert str(latest[0]["id"]) == str(f3)
            # f2 now points at f3.
            f2_ptr = await conn.fetchval(
                "SELECT superseded_by FROM analyst_outputs WHERE id=$1", f2)
            assert str(f2_ptr) == str(f3)
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM finding_supersessions WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM analyst_outputs WHERE analyst_id=$1", analyst_id)


async def test_live_fetch_window_newest_first_not_starved_by_old_noise(pivot_pool, monkeypatch):
    """DQ-C3 regression: when the open-finding pool EXCEEDS the fetch cap and is
    dominated by OLD entity-less noise (the cross_source_dedup metric class), the
    fresh substantive findings must still be clustered+superseded. The old code
    (ORDER BY produced_at ASC) fetched the OLDEST cap rows = all noise, starving
    the new findings → the situations leg silently froze. With newest-first +
    the cap as a safety valve, the substantive pair wins."""
    from legba.runtime.deps import StandardDeps

    tenant = f"dqc3_{uuid4().hex[:8]}"
    analyst_id = f"starve_{tenant}"
    produced_by = "test_supersession_dqc3"
    t0 = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)
    # Tiny cap so the window math is deterministic: 5 old noise + 2 new = 7 > 3.
    monkeypatch.setattr(finding_supersession, "_MAX_FINDINGS", 3)

    f_old, f_new = uuid4(), uuid4()
    ent = {"category": "conflict", "actors": ["Russia", "Ukraine"], "owner_tenant": tenant}
    try:
        async with pivot_pool.acquire() as conn:
            # 5 OLD entity-less metric findings (no derivable signature) — the noise
            # that floods the oldest-N window in prod.
            for i in range(5):
                await _insert_finding(
                    conn, fid=uuid4(), title=f"metrics tick {i}",
                    data={"category": "metrics", "owner_tenant": tenant},
                    produced_at=t0, analyst_id=analyst_id)
            # 2 NEWER substantive findings for the SAME situation (clusterable).
            await _insert_finding(
                conn, fid=f_old, title="Escalation v1", data=dict(ent, cycle=1),
                produced_at=t0 + timedelta(hours=6), analyst_id=analyst_id)
            await _insert_finding(
                conn, fid=f_new, title="Escalation v2", data=dict(ent, cycle=2),
                produced_at=t0 + timedelta(hours=12), analyst_id=analyst_id)

        deps = StandardDeps(pg_pool=pivot_pool)
        result = await run_method(
            [], {"sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
                 "scope_analyst_id": analyst_id, "owner_tenant": tenant}, deps,
        )
        # Newest-first: the substantive pair is in the 3-row window → clustered.
        assert result.finding.data["superseded_count"] == 1, result.finding.data
        async with pivot_pool.acquire() as conn:
            old_ptr = await conn.fetchval(
                "SELECT superseded_by FROM analyst_outputs WHERE id=$1", f_old)
            new_ptr = await conn.fetchval(
                "SELECT superseded_by FROM analyst_outputs WHERE id=$1", f_new)
            assert str(old_ptr) == str(f_new)  # fresh finding wins, not starved
            assert new_ptr is None
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM finding_supersessions WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM analyst_outputs WHERE analyst_id=$1", analyst_id)
