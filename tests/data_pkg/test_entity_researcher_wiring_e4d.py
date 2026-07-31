# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E4d (wiring) — entity_researcher as a registered analyst kind.

Verifies the framework glue: discover_analyst_kinds() picks up the kind, its
run_method returns an AnalystMethodResult receipt (dry-run mutates nothing / no
pool no-ops / a bare LLM is accepted for back-compat), and the descriptor YAML
validates with identity.kind = entity_researcher (the extension kind the
package registers on import).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

# importing the package runs _register_analyst_kind("entity_researcher")
import legba.data.analysts as analysts_pkg
from legba.data.analysts.entity_researcher import (
    EntityResearcherDeps,
    run_method,
)
from legba.data.config import PostgresConfig
from legba.data.provenance.kinds import TRACE_ONLY

#: Resolved from THIS file, not hardcoded: an absolute path to the main
#: checkout reads another tree's descriptors when the suite runs from a
#: worktree, silently testing the wrong YAML. Mirrors ``DESCRIPTOR_DIR`` in
#: ``test_handler_options_x1.py``.
_REPO = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


class _EmptyLLM:
    async def chat_complete(self, *a, **k):
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


def test_discover_registers_entity_researcher():
    reg = analysts_pkg.discover_analyst_kinds()
    assert "entity_researcher" in reg, sorted(reg)
    h = reg["entity_researcher"]
    assert h.run_method is run_method
    assert h.output_kind is TRACE_ONLY


def test_descriptor_yaml_validates():
    from legba.data.schemas.analyst import AnalystDescriptor
    body = yaml.safe_load(
        (_REPO / "descriptors/analyst_entity_researcher.yaml").read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == "entity_researcher"
    assert desc.identity.kind == "entity_researcher"
    llm = body["method"]["llm"]
    # merge_mode gates the mutating merge behavior (flipped to 'apply' 2026-07-12
    # after the E5 gate + a clean dry-run — see the descriptor header).
    assert llm["merge_mode"] in {"adjudicate_only", "apply"}
    # E6c reclassify options parse. reclassify_max is a live-tunable knob (shipped
    # OFF at 0, flipped to 150 on 07-13 after the reclassify_max=10 first run scored
    # 8/8 correct — see the descriptor header) that is expected to keep changing via
    # descriptor PUTs; assert shape (non-negative int), not a specific live value.
    assert int(llm["reclassify_max"]) >= 0
    assert 0.0 <= float(llm["reclass_min_confidence"]) <= 1.0
    # #219: reclass_entity_share SPLITS reclassify_max toward the generic
    # 'entity' pool; shipped OFF (0.0 = 100% person, pre-#219 behavior).
    assert 0.0 <= float(llm["reclass_entity_share"]) <= 1.0
    assert float(llm["reclass_entity_share"]) == 0.0
    # R9b: the trigram probe ships OFF, and its bounding floor ships alongside
    # it so the two are never set apart. Both are shape-asserted, not pinned to
    # a value — they are expected to move by descriptor PUT.
    assert int(llm["trgm_limit"]) >= 0
    assert int(llm["trgm_min_degree"]) >= 0
    assert int(llm["trgm_limit"]) == 0, "the unbounded probe must ship disabled"


# ---------------------------------------------------------------------------
# R9 — the descriptor knobs must REACH the deps. `trgm_limit` was read from the
# descriptor and then dropped on the floor (never passed to EntityResearcherDeps),
# so a PUT enabling the trigram probe did precisely nothing — dead config in the
# X-1 sense, and invisible because the shipped value equalled the default.
# ---------------------------------------------------------------------------


async def _build_deps(**llm_overrides):
    from legba.data.schemas.analyst import AnalystDescriptor
    from legba.runtime.analyst_deps_builder import _build_entity_researcher

    body = yaml.safe_load(
        (_REPO / "descriptors/analyst_entity_researcher.yaml").read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    body["method"]["llm"].update(llm_overrides)
    desc = AnalystDescriptor.model_validate(body, strict=False)
    handler = analysts_pkg.discover_analyst_kinds()["entity_researcher"]

    async def _resolve_llm():
        return _EmptyLLM()

    class _Deps:
        budget = None

    _run, deps, _kind = await _build_entity_researcher(
        desc, handler, _resolve_llm, pg_pool=None, deps=_Deps(),
    )
    return deps


@pytest.mark.asyncio
async def test_shipped_descriptor_builds_the_documented_deps():
    deps = await _build_deps()
    assert deps.max_pairs == 80          # the live per-run candidate cap
    assert deps.trgm_limit == 0          # probe OFF
    assert deps.trgm_min_degree == 0     # no floor (moot while the probe is off)


@pytest.mark.asyncio
async def test_trgm_knobs_reach_the_deps_bundle():
    """THE regression guard for the dropped read: a PUT of these two must be
    observable in the bundle the handler actually runs with."""
    deps = await _build_deps(trgm_limit=400, trgm_min_degree=25)
    assert deps.trgm_limit == 400
    assert deps.trgm_min_degree == 25


@pytest.mark.asyncio
async def test_candidate_cap_is_descriptor_settable():
    """R9a's other half — the per-run cap is the knob that decides how much of
    the (now degree-ranked) collision backlog each tick drains."""
    deps = await _build_deps(max_pairs=300)
    assert deps.max_pairs == 300


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_method_dry_run_receipt(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Zzwire Alpha Keeper")
        await _seed(conn, "the Zzwire Alpha Keeper")  # auto_merge pair
    deps = EntityResearcherDeps(llm=_EmptyLLM(), pg_pool=pg_pool)  # apply defaults False
    res = await run_method([], {"model_id": "stub"}, deps)
    data = res.finding.data
    assert data["mode"] == "dry_run"
    assert data["merges_applied"] >= 1  # would-merge count
    # dry-run mutated nothing
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM entity_profiles WHERE merged_into IS NOT NULL "
            "AND lower(canonical_name) LIKE '%zzwire alpha%'")
    assert n == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_method_no_pool_is_noop(pg_pool):
    deps = EntityResearcherDeps(llm=_EmptyLLM(), pg_pool=None)
    res = await run_method([], {}, deps)
    assert "noop" in res.finding.tags
    assert res.finding.data["reason"] == "no pg_pool"


@pytest.mark.asyncio
async def test_run_method_bare_llm_backcompat():
    # A bare LLMHandlerLike (no deps wrapper) is coerced; pg_pool None -> noop.
    res = await run_method([], {}, _EmptyLLM())
    assert "noop" in res.finding.tags
