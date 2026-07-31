# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E4d (core) — the run_entity_research orchestration (generate->adjudicate->exec).

Framework-agnostic: given a conn + an LLM it runs one research pass. Verified
here (real migrated DB, stub LLM):

  * DRY-RUN (apply=False, the default) reports what WOULD merge and mutates
    NOTHING;
  * apply=True actually tombstones the losers;
  * the report tallies + sample are populated; summary()/to_data() are sane.

Driven through the deterministic auto_merge band (a multi-token shared block key)
so the assertion doesn't depend on the LLM's per-pair numbering over the
session-shared test DB. The stub returns "[]", so any gray noise defaults to
unsure and never merges — only the seeded auto pairs move.
"""

from __future__ import annotations

import json
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data._entity_candidates import CandidatePair
from legba.data.analysts.entity_researcher import ResearchReport, run_entity_research
from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


class _EmptyLLM:
    """Returns an empty verdict array -> every gray pair defaults to unsure."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_complete(self, *a, **k):
        self.calls += 1

        class _R:
            content = "[]"
            usage = None

        return _R()


async def _seed(conn, name, *, cls="organization"):
    eid = str(uuid4())
    await conn.execute(
        "INSERT INTO entity_profiles (id, canonical_name, entity_class, entity_type,"
        " data) VALUES ($1::uuid,$2,$3,$3,'{}'::jsonb)", eid, name, cls)
    return eid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_reports_but_does_not_mutate(pg_pool):
    async with pg_pool.acquire() as conn:
        # Two multi-token shared-block-key org pairs -> auto_merge band.
        k1 = await _seed(conn, "Zzresearchd Alpha Keeper")
        l1 = await _seed(conn, "the Zzresearchd Alpha Keeper")  # same block key
        k2 = await _seed(conn, "Zzresearchd Beta Council")
        l2 = await _seed(conn, "the Zzresearchd Beta Council")

        rep = await run_entity_research(conn, _EmptyLLM(), apply=False)
        assert isinstance(rep, ResearchReport) and rep.mode == "dry_run"
        assert rep.merges_applied >= 2  # would-merge count includes my 2 auto pairs
        # ... but nothing is tombstoned
        tomb = await conn.fetchval(
            "SELECT count(*) FROM entity_profiles WHERE merged_into IS NOT NULL "
            "AND id = ANY($1::uuid[])", [l1, l2])
    assert tomb == 0, "dry-run must not mutate"
    assert rep.summary().startswith("entity_researcher [dry_run]")
    assert rep.to_data()["mode"] == "dry_run"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_merges_the_losers(pg_pool):
    async with pg_pool.acquire() as conn:
        k1 = await _seed(conn, "Zzresearchd Gamma Keeper")
        l1 = await _seed(conn, "the Zzresearchd Gamma Keeper")
        rep = await run_entity_research(conn, _EmptyLLM(), apply=True)
        assert rep.mode == "apply"
        # the seeded loser is tombstoned + redirects to a survivor
        row = await conn.fetchrow(
            "SELECT merged_into FROM entity_profiles WHERE id=$1::uuid", l1)
        resolved = str(await conn.fetchval("SELECT resolve_entity($1::uuid)", l1))
    assert row["merged_into"] is not None
    assert resolved != l1  # redirected to the keeper
    assert rep.merges_applied >= 1
    assert len(rep.sample) >= 1
    assert any("Zzresearchd Gamma" in s["keeper"] or "Zzresearchd Gamma" in s["loser"]
               for s in rep.sample)


# ---------------------------------------------------------------------------
# P4 Class 6 Obs. 2 (QW1-D fix 3) — the class_correction COUNTER + the
# apply-gated row-level hint, exercised through the FULL orchestration.
# generate_candidates is monkeypatched to a single controlled GRAY pair (real
# blocking/banding heuristics are out of scope here — Class 6's own adjudicate
# tests already cover parsing/recording at the adjudicate_pairs layer).
# ---------------------------------------------------------------------------


class _ClassCorrectionLLM:
    """Always flags side 'a' as mistyped -> 'event' for every gray pair."""

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                             system=None, **kw):
        class _R:
            content = json.dumps([{
                "n": 1, "verdict": "same", "confidence": 0.95,
                "why": "same tournament, article variant",
                "class_correction": {"side": "a", "correct_class": "event"},
            }])
            usage = None

        return _R()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_class_corrections_flagged_counted_in_dry_run_but_not_applied(
    pg_pool, monkeypatch,
):
    import legba.data.analysts.entity_researcher as er

    async with pg_pool.acquire() as conn:
        a_id = await _seed(conn, "Zzresearchd Orch Cup A", cls="person")
        b_id = await _seed(conn, "Zzresearchd the Orch Cup B", cls="person")
        pair = CandidatePair(
            left_id=a_id, left_name="Zzresearchd Orch Cup A", left_class="person",
            right_id=b_id, right_name="Zzresearchd the Orch Cup B",
            right_class="person", band="gray", score=0.7,
            signals=("trgm:0.7",), block_key="",
        )

        async def _fake_generate_candidates(*a, **k):
            return [pair]

        monkeypatch.setattr(er, "generate_candidates", _fake_generate_candidates)

        rep = await run_entity_research(conn, _ClassCorrectionLLM(), apply=False)
        assert rep.class_corrections_flagged == 1
        assert rep.class_correction_sample[0]["correct_class"] == "event"
        assert rep.class_correction_sample[0]["side"] == "a"
        row = await conn.fetchrow(
            "SELECT data FROM entity_profiles WHERE id=$1::uuid", a_id)
    data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
    # dry-run: counted, but NOT applied to entity_profiles (the apply gate).
    assert "adjudicator_class_hint" not in (data or {})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_class_corrections_applied_to_entity_profiles_in_apply_mode(
    pg_pool, monkeypatch,
):
    import legba.data.analysts.entity_researcher as er

    async with pg_pool.acquire() as conn:
        a_id = await _seed(conn, "Zzresearchd Orch Apply Cup A", cls="person")
        b_id = await _seed(conn, "Zzresearchd the Orch Apply Cup B", cls="person")
        pair = CandidatePair(
            left_id=a_id, left_name="Zzresearchd Orch Apply Cup A", left_class="person",
            right_id=b_id, right_name="Zzresearchd the Orch Apply Cup B",
            right_class="person", band="gray", score=0.7,
            signals=("trgm:0.7",), block_key="",
        )

        async def _fake_generate_candidates(*a, **k):
            return [pair]

        monkeypatch.setattr(er, "generate_candidates", _fake_generate_candidates)

        rep = await run_entity_research(conn, _ClassCorrectionLLM(), apply=True)
        assert rep.class_corrections_flagged == 1
        row = await conn.fetchrow(
            "SELECT data FROM entity_profiles WHERE id=$1::uuid", a_id)
    data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
    assert data["adjudicator_class_hint"]["correct_class"] == "event"
