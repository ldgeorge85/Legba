# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-2a — ``scripts/harvest_open_questions.py`` against the ephemeral test DB.

Covers the contract the harvest makes:

  * per-class selection — each of the five source classes yields its
    question-shaped candidates (and skips what is NOT question-shaped:
    collapsed contention groups, single-value groups, malformed stale roots);
  * dry-run performs ZERO writes while still reporting would-write counts;
  * apply lands ``hypotheses`` rows with ``status='open_question'``, the
    durable ``diagnostic_evidence`` marker, target scoping, and lineage;
  * re-runs are idempotent (the (harvest_class, source_id) marker dedups).

Uses the session-scoped ``migrated_pg`` ephemeral database (disposable-pg
pattern). All seeded ids/targets are uniquified so rows leaked by other tests
in the shared session DB can never flip an assertion; assertions are scoped to
THIS test's markers, never to global totals.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig

# Import the script as a module (scripts/ is not a package). Registered in
# sys.modules BEFORE exec so dataclass annotation introspection resolves.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "harvest_open_questions.py"
_spec = importlib.util.spec_from_file_location("harvest_open_questions", _SCRIPT)
harvest = importlib.util.module_from_spec(_spec)
sys.modules["harvest_open_questions"] = harvest
_spec.loader.exec_module(harvest)


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _ins_output(
    conn,
    *,
    kind: str,
    title: str,
    analyst_id: str,
    target_id: str | None = None,
    confidence: float = 0.5,
    data: dict | None = None,
    derived_from: list[UUID] | None = None,
) -> UUID:
    row_id = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, body, confidence, data, "
        "target_id, analyst_id, analyst_version, derived_from, schema_uri, run_id) "
        "VALUES ($1, $2, $3, '', $4, $5::jsonb, $6, $7, 'v1', $8, "
        "'iglu:legba/finding/jsonschema/1-0-0', $9)",
        row_id, kind, title, confidence, json.dumps(data or {}),
        target_id, analyst_id, derived_from or [], uuid4(),
    )
    return row_id


async def _marker_rows(conn, harvest_class: str, source_id: str) -> list:
    probe = json.dumps(
        [{"marker": harvest.MARKER_KEY, "harvest_class": harvest_class,
          "source_id": source_id}]
    )
    return await conn.fetch(
        "SELECT * FROM hypotheses WHERE status = 'open_question' "
        "AND diagnostic_evidence @> $1::jsonb",
        probe,
    )


async def _harvest_row_count(conn) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM hypotheses WHERE analyst_id = $1",
        harvest.HARVEST_ANALYST_ID,
    )


async def _seed_all_classes(conn) -> dict:
    """Seed one genuine candidate per harvest class + the skip-shaped rows.

    Returns the ids/keys needed by the assertions.
    """
    tag = uuid4().hex[:8]
    tgt = f"country_kw2_{tag}"
    # The freshness head gets its OWN target: a second open country_composition
    # head on `tgt` would race the disagreement head for the DISTINCT ON
    # latest-per-target slot (matching read-path semantics: only the latest
    # open head per target is reconciled).
    tgt_fresh = f"country_kw2f_{tag}"
    desk = f"desk_kw2_{tag}"

    # --- scorecard_disagreement: basis finding + scorecard head + comp head.
    basis = await _ins_output(
        conn, kind="finding", title="basis claim", analyst_id="leadership_transition",
        target_id=tgt,
    )
    scorecard = await _ins_output(
        conn, kind="scorecard", title=f"Scorecard {tgt}", analyst_id="scorecard_producer",
        target_id=tgt,
        data={"data": {"bands": {"dimensions": {
            "leadership_transition": {
                "band": "insufficient-evidence", "reason": "below-floor",
            },
            "escalation": {"band": "stable", "reason": ""},
        }}}},
    )
    comp = await _ins_output(
        conn, kind="finding", title=f"Composition {tgt}",
        analyst_id="country_composition", target_id=tgt,
        derived_from=[basis],
        data={"data": {"citations": [{
            "ref_id": str(basis), "ref_kind": "finding",
            "source": "leadership_transition",
        }]}},
    )

    # --- freshness_advisory: an OPEN comp head with one good + one malformed
    # stale root.
    old_input, new_input = uuid4(), uuid4()
    fresh_comp = await _ins_output(
        conn, kind="finding", title=f"Fresh comp {tgt_fresh}",
        analyst_id="country_composition", target_id=tgt_fresh,
        data={"data": {"freshness": {
            "inputs_as_of": [],
            "stale_roots": [
                {"unit": "escalation", "target": tgt_fresh,
                 "old_id": str(old_input), "old_title": "Old read",
                 "old_confidence": 0.8, "new_id": str(new_input),
                 "new_title": "New read", "new_confidence": 0.3,
                 "delta_confidence": 0.5},
                {"unit": "broken", "old_id": "not-a-uuid"},
            ],
            "advised": 1,
        }}},
    )

    # --- below_floor: graded finding under the floor + an ungraded sibling.
    floored = await _ins_output(
        conn, kind="finding", title="Floored claim", analyst_id="escalation",
        target_id=tgt, confidence=0.7,
    )
    critique = await _ins_output(
        conn, kind="critique", title="Faithfulness verify — Floored claim",
        analyst_id="critic",
        data={"analyzed_output_id": str(floored), "overall_score": 0.2},
    )
    ungraded = await _ins_output(
        conn, kind="finding", title="Ungraded claim", analyst_id="escalation",
        target_id=tgt, confidence=0.1,
    )

    # --- fact_contention: one open 2-value group, one collapsed, one
    # single-value.
    fact_a, fact_b = uuid4(), uuid4()
    open_group = uuid4()
    await conn.execute(
        "INSERT INTO fact_contention (id, subject_key, predicate_key, status, "
        "value_count) VALUES ($1, $2, 'leader', 'contested', 2)",
        open_group, f"subject_{tag}",
    )
    for fid, vk in ((fact_a, "alpha"), (fact_b, "beta")):
        await conn.execute(
            "INSERT INTO fact_contention_values (contention_id, value_key, "
            "representative_fact_id, is_junk) VALUES ($1, $2, $3, false)",
            open_group, vk, fid,
        )
    collapsed_group = uuid4()
    await conn.execute(
        "INSERT INTO fact_contention (id, subject_key, predicate_key, status, "
        "value_count) VALUES ($1, $2, 'capital', 'collapsed', 2)",
        collapsed_group, f"subject_collapsed_{tag}",
    )
    single_group = uuid4()
    await conn.execute(
        "INSERT INTO fact_contention (id, subject_key, predicate_key, status, "
        "value_count) VALUES ($1, $2, 'gdp', 'contested', 1)",
        single_group, f"subject_single_{tag}",
    )
    await conn.execute(
        "INSERT INTO fact_contention_values (contention_id, value_key, "
        "representative_fact_id, is_junk) VALUES ($1, 'only', $2, false)",
        single_group, uuid4(),
    )

    # --- collection_gap: one open finding with one good + one malformed gap.
    gap_finding = await _ins_output(
        conn, kind="finding", title="Collection requirements",
        analyst_id="deterministic",
        data={"data": {"sub_handler": "collection_gap", "gaps": [
            {"desk": desk, "dimension": "energy_security",
             "reason": "below-floor", "insufficient_count": 3,
             "window_scorecards": 4, "persistence": 0.75,
             "source_classes": ["official", "reporting"]},
            {"dimension": "no-desk-entry"},
        ]}},
    )

    return {
        "tag": tag, "tgt": tgt, "tgt_fresh": tgt_fresh, "desk": desk,
        "basis": basis, "scorecard": scorecard, "comp": comp,
        "fresh_comp": fresh_comp, "old_input": old_input, "new_input": new_input,
        "floored": floored, "critique": critique, "ungraded": ungraded,
        "open_group": open_group, "fact_a": fact_a, "fact_b": fact_b,
        "collapsed_group": collapsed_group, "single_group": single_group,
        "gap_finding": gap_finding,
    }


def _find(results, cls):
    assert cls in results, f"missing class {cls} in results"
    return results[cls]


# ---------------------------------------------------------------------------
# The full flow: dry-run zero-write -> apply -> idempotent re-apply
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_harvest_flow_dry_run_apply_idempotent(conn):
    seeded = await _seed_all_classes(conn)
    tgt, desk = seeded["tgt"], seeded["desk"]

    # ---- DRY-RUN: candidates reported, ZERO rows written.
    before = await _harvest_row_count(conn)
    dry = await harvest.run_harvest(conn, apply=False)
    assert await _harvest_row_count(conn) == before, "dry-run must write nothing"
    # Every class found at least OUR candidate (leaked rows may add more).
    for cls in harvest.HARVEST_CLASSES:
        assert _find(dry, cls).candidates >= 1, f"{cls}: expected a candidate"
    # The would-write column is populated on the dry run.
    assert _find(dry, "fact_contention").written >= 1

    # ---- APPLY: our five markers land, shaped correctly.
    applied = await harvest.run_harvest(conn, apply=True)
    for cls in harvest.HARVEST_CLASSES:
        assert _find(applied, cls).written >= 1, f"{cls}: expected a write"

    sc_sid = f"{tgt}:leadership_transition:{seeded['basis']}"
    rows = await _marker_rows(conn, "scorecard_disagreement", sc_sid)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "open_question"
    assert r["analyst_id"] == harvest.HARVEST_ANALYST_ID
    assert r["target_id"] == tgt
    assert "leadership_transition" in r["thesis"]
    got_refs = set(r["derived_from"])
    assert {seeded["basis"], seeded["scorecard"], seeded["comp"]} <= got_refs

    fr_sid = f"{seeded['fresh_comp']}:{seeded['old_input']}"
    rows = await _marker_rows(conn, "freshness_advisory", fr_sid)
    assert len(rows) == 1
    assert set(rows[0]["derived_from"]) == {
        seeded["fresh_comp"], seeded["old_input"], seeded["new_input"],
    }
    assert rows[0]["target_id"] == seeded["tgt_fresh"]

    rows = await _marker_rows(conn, "below_floor", str(seeded["floored"]))
    assert len(rows) == 1
    assert set(rows[0]["derived_from"]) == {seeded["floored"], seeded["critique"]}
    assert "Floored claim" in rows[0]["thesis"]
    # The ungraded sibling is NOT a below-floor question.
    assert await _marker_rows(conn, "below_floor", str(seeded["ungraded"])) == []

    rows = await _marker_rows(conn, "fact_contention", str(seeded["open_group"]))
    assert len(rows) == 1
    assert set(rows[0]["derived_from"]) == {seeded["fact_a"], seeded["fact_b"]}
    assert rows[0]["target_id"] is None
    # Collapsed + single-value groups are skipped, and counted as such.
    assert await _marker_rows(
        conn, "fact_contention", str(seeded["collapsed_group"])
    ) == []
    assert await _marker_rows(
        conn, "fact_contention", str(seeded["single_group"])
    ) == []
    fc = _find(applied, "fact_contention")
    assert fc.skipped.get("collapsed", 0) >= 1
    assert fc.skipped.get("single_value", 0) >= 1

    rows = await _marker_rows(conn, "collection_gap", f"{desk}:energy_security")
    assert len(rows) == 1
    assert set(rows[0]["derived_from"]) == {seeded["gap_finding"]}
    assert rows[0]["target_id"] == desk
    # Malformed entries were skipped honestly (never silently).
    assert _find(applied, "freshness_advisory").skipped.get(
        "malformed_stale_root", 0
    ) >= 1
    assert _find(applied, "collection_gap").skipped.get("malformed_gap", 0) >= 1

    # ---- RE-APPLY: fully idempotent — nothing new for ANY class.
    count_after_first = await _harvest_row_count(conn)
    again = await harvest.run_harvest(conn, apply=True)
    assert await _harvest_row_count(conn) == count_after_first
    for cls in harvest.HARVEST_CLASSES:
        c = _find(again, cls)
        assert c.written == 0, f"{cls}: re-apply must write nothing"
        assert c.existing >= 1, f"{cls}: re-apply must see the prior marker"


# ---------------------------------------------------------------------------
# Per-class selection
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_harvest_class_selection(conn):
    """``--classes fact_contention`` harvests ONLY that class."""
    tag = uuid4().hex[:8]
    group = uuid4()
    await conn.execute(
        "INSERT INTO fact_contention (id, subject_key, predicate_key, status, "
        "value_count) VALUES ($1, $2, 'leader', 'contested', 2)",
        group, f"subject_sel_{tag}",
    )
    for vk in ("alpha", "beta"):
        await conn.execute(
            "INSERT INTO fact_contention_values (contention_id, value_key, "
            "representative_fact_id, is_junk) VALUES ($1, $2, $3, false)",
            group, vk, uuid4(),
        )
    # A fresh collection_gap cell that would ALSO harvest if the class ran.
    desk = f"desk_sel_{tag}"
    await _ins_output(
        conn, kind="finding", title="Collection requirements",
        analyst_id="deterministic",
        data={"data": {"sub_handler": "collection_gap", "gaps": [
            {"desk": desk, "dimension": "escalation", "reason": "below-floor",
             "source_classes": ["reporting"]},
        ]}},
    )

    results = await harvest.run_harvest(
        conn, classes=["fact_contention"], apply=True
    )
    assert set(results) == {"fact_contention"}
    assert len(await _marker_rows(conn, "fact_contention", str(group))) == 1
    # The other class did NOT run.
    assert await _marker_rows(conn, "collection_gap", f"{desk}:escalation") == []


@pytest.mark.asyncio
async def test_unknown_class_rejected(conn):
    with pytest.raises(SystemExit):
        await harvest.run_harvest(conn, classes=["not_a_class"], apply=False)
