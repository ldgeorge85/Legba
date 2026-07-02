# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the manual-ingest batch loader (S4-T2) + CLI wiring (S4-T3).

Two layers:

  * PURE (no DB) — the mode CLASSIFIERS (skip/merge/force), the manifest→mode /
    provenance→source_type mapping, the record→seed-payload mapping + honest
    confidence resolution, and the report tallying. These run in the fast suite.
  * INTEGRATION (``@pytest.mark.integration``, needs Postgres) — the full
    loader through the seed plane: a fixture batch × 3 modes behaves per spec;
    ``skip`` re-run is a ledger-deduped no-op; ``force`` leaves a prior row
    SUPERSEDED (closed, not gone) and clears the source-tier trap that blocks a
    manual ``merge``; a ``dry_run`` writes NOTHING yet its counts MATCH the wet
    run's (same code path, transaction rolled back).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from legba.data.seed import (
    BatchMode,
    ManualBatchSeedSource,
    PriorFact,
    PriorNexus,
    ProvenanceTier,
    RecordAction,
    SeedEntity,
    SeedFact,
    SeedNexus,
    classify_fact,
    classify_nexus,
    run_manual_batch,
)
from legba.data.seed.manual_batch import (
    ManualBatchReport,
    _coerce_mode,
    _seed_payloads,
    _source_type_for,
)
from legba.data.seed.manual_schema import (
    BatchFiles,
    BatchManifest,
    validate_batch,
)

FIXTURES = Path(__file__).parent / "fixtures"
VALID_BATCH = FIXTURES / "manual_batch_valid"

# _source_tier_rank: seed/curated = 2 (authoritative), everything else = 1.
TIER_CURATED = 2
TIER_MANUAL = 1


# ---------------------------------------------------------------------------
# PURE: classify_fact — skip / merge / force + the source-tier guard
# ---------------------------------------------------------------------------


def test_classify_fact_skip_short_circuits_when_a_prior_is_open():
    priors = [PriorFact(value="Old", tier=TIER_MANUAL)]
    # Any open prior for the subject+predicate → SKIP (insert-if-absent).
    assert (
        classify_fact(
            incoming_value="New", mode=BatchMode.SKIP,
            priors=priors, incoming_tier=TIER_MANUAL,
        )
        is RecordAction.SKIP
    )
    # A same-value re-run is likewise a SKIP (idempotent no-op).
    assert (
        classify_fact(
            incoming_value="Old", mode=BatchMode.SKIP,
            priors=priors, incoming_tier=TIER_MANUAL,
        )
        is RecordAction.SKIP
    )


def test_classify_fact_skip_creates_when_absent():
    assert (
        classify_fact(
            incoming_value="New", mode=BatchMode.SKIP,
            priors=[], incoming_tier=TIER_MANUAL,
        )
        is RecordAction.CREATE
    )


def test_classify_fact_merge_unchanged_and_create():
    # same value already open → UNCHANGED (nothing to do, no confidence drift).
    assert (
        classify_fact(
            incoming_value="Same", mode=BatchMode.MERGE,
            priors=[PriorFact(value="same", tier=TIER_MANUAL)],
            incoming_tier=TIER_MANUAL,
        )
        is RecordAction.UNCHANGED
    )
    # no prior → CREATE.
    assert (
        classify_fact(
            incoming_value="Fresh", mode=BatchMode.MERGE,
            priors=[], incoming_tier=TIER_MANUAL,
        )
        is RecordAction.CREATE
    )


def test_classify_fact_merge_supersede_same_tier():
    # A differing-value prior of the SAME (or lower) tier → SUPERSEDE.
    assert (
        classify_fact(
            incoming_value="New", mode=BatchMode.MERGE,
            priors=[PriorFact(value="Old", tier=TIER_MANUAL)],
            incoming_tier=TIER_MANUAL,
        )
        is RecordAction.SUPERSEDE
    )


def test_classify_fact_merge_conflict_on_higher_tier_prior():
    # THE TIER TRAP: a manual (tier 1) merge cannot retire a seed/curated
    # (tier 2) prior → CONFLICT (reported, not a silent contention).
    assert (
        classify_fact(
            incoming_value="New", mode=BatchMode.MERGE,
            priors=[PriorFact(value="Old", tier=TIER_CURATED)],
            incoming_tier=TIER_MANUAL,
        )
        is RecordAction.CONFLICT
    )
    # A curated (tier 2) merge over a seed/curated (tier 2) prior is same-tier
    # recency — SUPERSEDE, no conflict.
    assert (
        classify_fact(
            incoming_value="New", mode=BatchMode.MERGE,
            priors=[PriorFact(value="Old", tier=TIER_CURATED)],
            incoming_tier=TIER_CURATED,
        )
        is RecordAction.SUPERSEDE
    )


def test_classify_fact_force_overrides_the_tier_trap():
    # force = operator authority: supersede a higher-tier prior regardless.
    assert (
        classify_fact(
            incoming_value="New", mode=BatchMode.FORCE,
            priors=[PriorFact(value="Old", tier=TIER_CURATED)],
            incoming_tier=TIER_MANUAL,
        )
        is RecordAction.SUPERSEDE
    )
    # but a same-value force is still a no-op (the value is already the truth).
    assert (
        classify_fact(
            incoming_value="Old", mode=BatchMode.FORCE,
            priors=[PriorFact(value="Old", tier=TIER_CURATED)],
            incoming_tier=TIER_MANUAL,
        )
        is RecordAction.UNCHANGED
    )


# ---------------------------------------------------------------------------
# PURE: classify_nexus — no tier guard (merge ≡ force)
# ---------------------------------------------------------------------------


def test_classify_nexus_skip_and_create():
    assert (
        classify_nexus(
            incoming_polarity=1, incoming_label="", mode=BatchMode.SKIP,
            priors=[PriorNexus(polarity=-1, label="")],
        )
        is RecordAction.SKIP
    )
    assert (
        classify_nexus(
            incoming_polarity=1, incoming_label="", mode=BatchMode.SKIP, priors=[],
        )
        is RecordAction.CREATE
    )


def test_classify_nexus_merge_unchanged_when_polarity_and_label_match():
    assert (
        classify_nexus(
            incoming_polarity=1, incoming_label="Allied", mode=BatchMode.MERGE,
            priors=[PriorNexus(polarity=1, label="allied")],
        )
        is RecordAction.UNCHANGED
    )


def test_classify_nexus_supersede_on_polarity_or_label_change():
    # polarity flip → SUPERSEDE (merge and force coincide — no tier guard).
    for mode in (BatchMode.MERGE, BatchMode.FORCE):
        assert (
            classify_nexus(
                incoming_polarity=-1, incoming_label="x", mode=mode,
                priors=[PriorNexus(polarity=1, label="x")],
            )
            is RecordAction.SUPERSEDE
        )
    # label change with same polarity → SUPERSEDE.
    assert (
        classify_nexus(
            incoming_polarity=1, incoming_label="new label", mode=BatchMode.MERGE,
            priors=[PriorNexus(polarity=1, label="old label")],
        )
        is RecordAction.SUPERSEDE
    )


# ---------------------------------------------------------------------------
# PURE: manifest → mode / provenance → source_type
# ---------------------------------------------------------------------------


def _manifest(**over) -> BatchManifest:
    base = dict(
        schema_version="1",
        batch_id="t-batch",
        operator="legba-dev",
        created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        default_provenance=ProvenanceTier.MANUAL,
        mode=BatchMode.SKIP,
        default_confidence=0.9,
        files=BatchFiles(facts="facts.jsonl"),
    )
    base.update(over)
    return BatchManifest(**base)


def test_coerce_mode_cli_override_wins_else_manifest():
    m = _manifest(mode=BatchMode.SKIP)
    assert _coerce_mode(None, m) is BatchMode.SKIP           # manifest default
    assert _coerce_mode("force", m) is BatchMode.FORCE       # string override
    assert _coerce_mode(BatchMode.MERGE, m) is BatchMode.MERGE
    assert _coerce_mode(None, _manifest(mode=BatchMode.MERGE)) is BatchMode.MERGE


def test_source_type_maps_provenance_tier_and_never_widens():
    assert _source_type_for(_manifest(default_provenance=ProvenanceTier.CURATED)) == "curated"
    # MANUAL stays 'manual' — stored, not grounding-injected.
    assert _source_type_for(_manifest(default_provenance=ProvenanceTier.MANUAL)) == "manual"


# ---------------------------------------------------------------------------
# PURE: record → seed payload mapping + honest confidence
# ---------------------------------------------------------------------------


def test_seed_payloads_map_the_fixture_and_resolve_confidence():
    vb = validate_batch(VALID_BATCH)
    assert vb.ok
    payloads = _seed_payloads(vb)

    facts = [p for p in payloads if isinstance(p, SeedFact)]
    ents = [p for p in payloads if isinstance(p, SeedEntity)]
    nexuses = [p for p in payloads if isinstance(p, SeedNexus)]
    assert len(facts) == 3 and len(ents) == 2 and len(nexuses) == 2

    by_pred = {f.predicate: f for f in facts}
    # per-record confidence honoured …
    assert by_pred["head of state"].confidence == pytest.approx(0.95)
    # … and the batch default (0.9) fills a record that omitted it (no silent 1.0).
    assert by_pred["capital"].confidence == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_adapter_fetch_map_sets_source_type_from_tier():
    from legba.data.seed import SeedContext

    src = ManualBatchSeedSource()
    vb = await src.fetch(SeedContext(options={"batch_dir": str(VALID_BATCH)}))
    # The valid fixture declares default_provenance: curated.
    assert src.source_type == "curated"
    payloads = list(src.map(vb))
    assert any(isinstance(p, SeedFact) for p in payloads)


def test_adapter_fetch_requires_batch_dir():
    import asyncio

    from legba.data.seed import SeedContext

    src = ManualBatchSeedSource()
    with pytest.raises(ValueError):
        asyncio.run(src.fetch(SeedContext(options={})))


# ---------------------------------------------------------------------------
# PURE: the report tally + as_dict
# ---------------------------------------------------------------------------


def test_report_tally_and_serialization():
    r = ManualBatchReport(
        batch_id="b", mode="merge", source_type="manual",
        grounding_eligible=False, dry_run=True,
    )
    r.record("facts", RecordAction.CREATE)
    r.record("facts", RecordAction.SUPERSEDE)
    r.record("facts", RecordAction.SKIP)
    r.record("nexuses", RecordAction.CREATE)
    assert r.has_writes_pending is True
    d = r.as_dict()
    assert d["facts"]["create"] == 1 and d["facts"]["supersede"] == 1
    assert d["facts"]["skip"] == 1 and d["nexuses"]["create"] == 1
    assert d["grounding_eligible"] is False
    # a report with only skips/unchanged/conflicts touches nothing.
    r2 = ManualBatchReport(
        batch_id="b", mode="skip", source_type="manual",
        grounding_eligible=False, dry_run=False,
    )
    r2.record("facts", RecordAction.SKIP)
    r2.record("facts", RecordAction.UNCHANGED)
    assert r2.has_writes_pending is False


# ===========================================================================
# INTEGRATION — the full loader through the seed plane (needs Postgres).
# ===========================================================================


import asyncpg  # noqa: E402
import pytest_asyncio  # noqa: E402

from legba.data.config import PostgresConfig  # noqa: E402
from legba.data.provenance import AnalystContext, FactPayload, write_fact  # noqa: E402


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


def _write_batch(
    tmp_path: Path,
    *,
    provenance: str = "manual",
    mode: str = "skip",
    default_confidence: float | None = 0.9,
    facts: list[dict] | None = None,
    nexuses: list[dict] | None = None,
    entities: list[dict] | None = None,
    batch_id: str | None = None,
) -> Path:
    """Materialize a manual-ingest batch directory and return its path."""
    d = tmp_path / f"batch_{uuid4().hex[:8]}"
    d.mkdir()
    files: dict[str, str] = {}
    if facts is not None:
        (d / "facts.jsonl").write_text(
            "\n".join(json.dumps(x) for x in facts), encoding="utf-8"
        )
        files["facts"] = "facts.jsonl"
    if nexuses is not None:
        (d / "nexuses.jsonl").write_text(
            "\n".join(json.dumps(x) for x in nexuses), encoding="utf-8"
        )
        files["nexuses"] = "nexuses.jsonl"
    if entities is not None:
        (d / "entities.jsonl").write_text(
            "\n".join(json.dumps(x) for x in entities), encoding="utf-8"
        )
        files["entities"] = "entities.jsonl"
    manifest = {
        "schema_version": "1",
        "batch_id": batch_id or f"batch-{uuid4().hex[:8]}",
        "operator": "legba-dev",
        "created_at": "2026-07-02T00:00:00Z",
        "default_provenance": provenance,
        "mode": mode,
        "default_confidence": default_confidence,
        "files": files,
    }
    import yaml

    (d / "batch_manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return d


async def _open_fact(conn, subject, predicate):
    return await conn.fetchrow(
        "SELECT id, value, source_type, valid_until, superseded_by FROM facts "
        "WHERE lower(subject)=lower($1) AND predicate=$2 "
        "AND valid_until IS NULL AND superseded_by IS NULL",
        subject,
        predicate,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_three_modes_behave_per_spec(pg_pool, tmp_path):
    subj = f"Zed_{uuid4().hex[:8]}"
    fact = {"subject": subj, "predicate": "head of state", "value": "Ada",
            "valid_from": "2025-01-01"}

    # --- skip on an empty (subject) state → CREATE; re-run → SKIP.
    b = _write_batch(tmp_path, mode="skip", facts=[fact])
    r1 = await run_manual_batch(pg_pool, batch_dir=b, dry_run=False)
    assert r1.facts["create"] == 1 and not r1.errors
    r2 = await run_manual_batch(pg_pool, batch_dir=b, dry_run=False)
    assert r2.facts["skip"] == 1 and r2.facts["create"] == 0

    # --- merge same value → UNCHANGED; merge changed value (same tier) → SUPERSEDE.
    b_same = _write_batch(tmp_path, mode="merge", facts=[fact])
    rs = await run_manual_batch(pg_pool, batch_dir=b_same, dry_run=False)
    assert rs.facts["unchanged"] == 1

    changed = {**fact, "value": "Bo"}
    b_chg = _write_batch(tmp_path, mode="merge", facts=[changed])
    rc = await run_manual_batch(pg_pool, batch_dir=b_chg, dry_run=False)
    assert rc.facts["supersede"] == 1
    async with pg_pool.acquire() as conn:
        open_row = await _open_fact(conn, subj, "head of state")
        assert open_row["value"] == "Bo", "the new value is the open truth"
        # exactly one open row for the (subject, predicate).
        n_open = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE lower(subject)=lower($1) "
            "AND predicate='head of state' AND valid_until IS NULL "
            "AND superseded_by IS NULL",
            subj,
        )
        assert n_open == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_skip_rerun_is_ledger_deduped_no_op(pg_pool, tmp_path):
    subj = f"Skp_{uuid4().hex[:8]}"
    b = _write_batch(
        tmp_path, mode="skip",
        facts=[{"subject": subj, "predicate": "capital", "value": "X",
                "valid_from": "2000-01-01"}],
    )
    r1 = await run_manual_batch(pg_pool, batch_dir=b, dry_run=False)
    assert r1.facts["create"] == 1
    async with pg_pool.acquire() as conn:
        ledger_1 = await conn.fetchval(
            "SELECT count(*) FROM seed_batches WHERE kind=$1", r1.batch_id
        )
        open_1 = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE lower(subject)=lower($1) "
            "AND valid_until IS NULL AND superseded_by IS NULL", subj
        )
    assert ledger_1 == 1

    r2 = await run_manual_batch(pg_pool, batch_dir=b, dry_run=False)
    assert r2.facts["skip"] == 1 and r2.facts["create"] == 0
    assert r2.seed_batch_id == r1.seed_batch_id, "re-run reuses the ONE ledger row"
    async with pg_pool.acquire() as conn:
        ledger_2 = await conn.fetchval(
            "SELECT count(*) FROM seed_batches WHERE kind=$1", r1.batch_id
        )
        open_2 = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE lower(subject)=lower($1) "
            "AND valid_until IS NULL AND superseded_by IS NULL", subj
        )
    assert ledger_2 == 1, "no duplicate ledger row on re-run"
    assert open_2 == open_1, "no new open rows on a skip re-run"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_force_clears_tier_trap_and_supersedes_not_deletes(pg_pool, tmp_path):
    """A manual MERGE cannot retire a SEED prior (tier trap → conflict); a manual
    FORCE can — leaving the prior CLOSED (superseded), never deleted, and WITHOUT
    widening the new row's provenance to grounding-eligible 'curated'."""
    subj = f"Iran_{uuid4().hex[:8]}"
    # A pre-existing SEED (authoritative, tier 2) fact.
    actx = AnalystContext(
        analyst_id="seed.test", analyst_version="seed",
        run_id=uuid4(), target_id=None, target_version=None,
    )
    async with pg_pool.acquire() as conn:
        out, dlq = await write_fact(
            conn, analyst_ctx=actx,
            payload=FactPayload(
                subject=subj, predicate="head of state", value="Old Leader",
                confidence=0.95, source_type="seed",
                valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ),
            derived_from=[], source_type="seed",
        )
        assert dlq is None and out is not None
        seed_id = out.id

    changed = {"subject": subj, "predicate": "head of state", "value": "New Leader",
               "valid_from": "2026-03-08", "confidence": 0.9}

    # --- MERGE (manual, tier 1) → CONFLICT: the seed prior is untouched.
    b_merge = _write_batch(tmp_path, provenance="manual", mode="merge", facts=[changed])
    rm = await run_manual_batch(pg_pool, batch_dir=b_merge, dry_run=False)
    assert rm.facts["conflict"] == 1 and rm.conflicts, "manual merge blocked by tier"
    async with pg_pool.acquire() as conn:
        still = await _open_fact(conn, subj, "head of state")
        assert still is not None and still["value"] == "Old Leader"
        assert still["valid_until"] is None, "merge did NOT close the seed prior"

    # --- FORCE (manual) → SUPERSEDE: prior closed (not gone), new row is 'manual'.
    b_force = _write_batch(tmp_path, provenance="manual", mode="force", facts=[changed])
    rf = await run_manual_batch(pg_pool, batch_dir=b_force, dry_run=False)
    assert rf.facts["supersede"] == 1 and not rf.errors
    async with pg_pool.acquire() as conn:
        # The prior seed row still EXISTS but is CLOSED + points at the successor.
        prior = await conn.fetchrow(
            "SELECT valid_until, superseded_by, source_type FROM facts WHERE id=$1",
            seed_id,
        )
        assert prior is not None, "history preserved — the prior row is not deleted"
        assert prior["valid_until"] is not None, "prior closed via valid_until"
        assert prior["superseded_by"] is not None, "prior points at the successor"
        # The current open truth is 'New Leader', stamped 'manual' (NOT widened
        # to grounding-eligible 'curated').
        cur = await _open_fact(conn, subj, "head of state")
        assert cur["value"] == "New Leader"
        assert cur["source_type"] == "manual", "force must not widen provenance tier"
        assert cur["id"] == prior["superseded_by"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_writes_nothing_and_counts_match_wet(pg_pool, tmp_path):
    subj = f"Dry_{uuid4().hex[:8]}"
    facts = [
        {"subject": subj, "predicate": "head of state", "value": "A",
         "valid_from": "2025-01-01"},
        {"subject": subj, "predicate": "capital", "value": "C",
         "valid_from": "1900-01-01"},
    ]
    nexuses = [
        {"subject": subj, "object": "PACT", "rel_type": "member of",
         "polarity": 1, "valid_from": "2025-01-01", "confidence": 0.9},
    ]
    b = _write_batch(tmp_path, mode="merge", facts=facts, nexuses=nexuses)

    async with pg_pool.acquire() as conn:
        ledger_before = await conn.fetchval("SELECT count(*) FROM seed_batches")
        facts_before = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE lower(subject)=lower($1)", subj
        )

    dry = await run_manual_batch(pg_pool, batch_dir=b, dry_run=True)
    assert dry.dry_run is True and dry.seed_batch_id is None

    async with pg_pool.acquire() as conn:
        ledger_mid = await conn.fetchval("SELECT count(*) FROM seed_batches")
        facts_mid = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE lower(subject)=lower($1)", subj
        )
    assert ledger_mid == ledger_before, "dry-run recorded NO ledger row"
    assert facts_mid == facts_before, "dry-run wrote NO facts"

    wet = await run_manual_batch(pg_pool, batch_dir=b, dry_run=False)
    # The tallies MATCH — dry-run classified against the same starting state.
    assert dry.facts == wet.facts
    assert dry.nexuses == wet.nexuses
    assert wet.facts["create"] == 2 and wet.nexuses["create"] == 1

    async with pg_pool.acquire() as conn:
        facts_after = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE lower(subject)=lower($1) "
            "AND valid_until IS NULL AND superseded_by IS NULL", subj
        )
        # The report was persisted onto the ledger row.
        manifest = await conn.fetchval(
            "SELECT manifest FROM seed_batches WHERE id=$1", wet.seed_batch_id
        )
    assert facts_after == 2, "wet run wrote the two facts"
    manifest = manifest if isinstance(manifest, dict) else json.loads(manifest)
    assert manifest.get("report", {}).get("facts", {}).get("create") == 2
