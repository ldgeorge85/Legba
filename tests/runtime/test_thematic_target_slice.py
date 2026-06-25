# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 5c — thematic-target slice focusing.

The operator's "a target predicate CAN be 'iran war'": a non-geo target whose
``scope.predicate`` (free-text ``contains_any``) focuses the analyst slice on a
cross-country situation. Covers the three new pieces without a live DB:

  * the ``contains_any`` free-text helper + its ``signal.text`` ctx;
  * ``filter_rows_by_residual`` — the batch residual filter the slice reader
    applies (compile-once, fail-closed), proving the iran-war predicate narrows
    a mixed signal set to only the iran-war signals;
  * ``ThematicScope`` validates as a TargetDescriptor scope (domain=thematic).

The end-to-end slice read (``_read_substrate_slice`` applying the predicate
against live signals) is exercised against the running stack at deploy time.
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.predicates import (
    PredicateSurface,
    compile_predicate,
)
from legba.data.schemas.analyst import AnalystDescriptor
from legba.runtime.dapr_actors import (
    _critic_fanout_max,
    _diversify_by_source,
    _global_slice_per_source_cap,
    _read_substrate_slice,
)
from legba.runtime.subscription.filter import (
    _signal_residual_ctx,
    filter_rows_by_residual,
)


def _minimal_inline_descriptor() -> AnalystDescriptor:
    return AnalystDescriptor.model_validate({
        "identity": {
            "id": "slice_probe", "name": "Slice Probe",
            "schema_uri": "legba/analyst/1.0.0", "version": "0" * 16,
            "kind": "inline_target",
            "type_signature": {
                "input_type": "legba.runtime.SignalList",
                "output_type": "legba.runtime.Finding",
            },
            "state": "active", "owner": "t",
        },
        "subscription": {"substrate": {"direct_queries": False}},
        "method": {
            "kind": "llm_planner",
            "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            "llm": {"primary": {"factory_kind": "stack_ref", "raw": "llm.x",
                                "expected_family": "llm_provider"}},
        },
        "cadence": {"fallback_schedule": "0 */6 * * *"},
    }, strict=False)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


class _FakeConn:
    """Minimal asyncpg-conn stand-in: fetch returns [], fetchrow returns None.
    Lets the no-target slice path run with no DB so the UnboundLocalError
    regression is caught in the UNIT suite (the integration variant below was
    skipped in the daprd-baseline run and never gated the merge that shipped
    the crash)."""
    async def fetch(self, *a, **k):
        return []

    async def fetchrow(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_read_substrate_slice_no_target_unit_no_unbind():
    """UNIT regression (no DB): target_filter=None must not UnboundLocalError on
    scope_predicate — the 5c slice bug that hard-failed every meta analyst."""
    rows = await _read_substrate_slice(
        _FakeConn(), descriptor=_minimal_inline_descriptor(), target_filter=None,
    )
    assert rows == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_substrate_slice_no_target_does_not_unbind(pg_pool):
    """Regression: a NO-target META analyst (target_filter=None — world_assessor /
    situation_clustering / thematic_proposal) must NOT hit an UnboundLocalError on
    scope_predicate. The 5c slice change introduced the var inside the
    `if target_filter:` block; it must be initialized before."""
    async with pg_pool.acquire() as conn:
        rows = await _read_substrate_slice(
            conn, descriptor=_minimal_inline_descriptor(), target_filter=None,
        )
    assert isinstance(rows, list)  # no crash, no predicate applied

# The live thematic frame from descriptors/target_situation_iran_war.yaml.
_IRAN_WAR_PRED = (
    'contains_any(["iran","tehran","khamenei","irgc","revolutionary guard"]) '
    'and contains_any(["war","strike","missile","attack","military","conflict",'
    '"drone","retaliation","ceasefire","nuclear"])'
)


def _sig(title: str, summary: str = "") -> dict:
    return {"payload": {"title": title, "summary": summary}, "geo": []}


def test_contains_any_helper_matches_signal_text():
    compiled = compile_predicate(_IRAN_WAR_PRED, PredicateSurface.TARGET_SCOPE)
    assert compiled.evaluate(_signal_residual_ctx(
        _sig("Iran launches missile strike on US base")))
    # mentions Iran but no conflict term → no match (both clauses required).
    assert not compiled.evaluate(_signal_residual_ctx(
        _sig("Iran signs trade deal with India")))
    # conflict term but not Iran → no match.
    assert not compiled.evaluate(_signal_residual_ctx(
        _sig("Russia missile strike on Ukraine")))
    # match can come from the summary, not just the title.
    assert compiled.evaluate(_signal_residual_ctx(
        _sig("Regional tensions", "Tehran threatens retaliation after the strike")))


def test_contains_any_matches_on_word_boundary_not_substring():
    """DQ-#70/F2: a term matches on WORD BOUNDARIES, not as a raw substring —
    "war" no longer matches "Warsaw"/"forward". Under the old substring helper,
    'Iran summit held in Warsaw' would FALSELY satisfy the conflict clause
    (war ⊂ Warsaw); with word-boundary matching it must not."""
    compiled = compile_predicate(_IRAN_WAR_PRED, PredicateSurface.TARGET_SCOPE)
    # Mentions Iran, but the only "war" is inside "Warsaw" → conflict clause
    # NOT satisfied → no match (was a false positive under substring matching).
    assert not compiled.evaluate(_signal_residual_ctx(
        _sig("Iran summit held in Warsaw")))
    # The real word "war" still matches.
    assert compiled.evaluate(_signal_residual_ctx(
        _sig("Iran war escalates across the region")))


def test_filter_rows_by_residual_focuses_the_slice():
    """The batch filter the slice reader applies keeps ONLY the rows matching
    the thematic predicate — the core 5c proof that a thematic target gets a
    focused slice, not the whole 24h pool."""
    rows = [
        _sig("Iran missile strike hits US carrier in the Gulf"),   # hit
        _sig("Brazil Pix digital payment system update"),          # miss
        _sig("Tehran and Washington trade fire as war escalates"), # hit
        _sig("South Korea signs MOUs with Italy"),                 # miss
        _sig("IRGC drone downed over the strait"),                 # hit
    ]
    kept = filter_rows_by_residual(_IRAN_WAR_PRED, rows)
    titles = [r["payload"]["title"] for r in kept]
    assert len(kept) == 3
    assert all("Iran" in t or "Tehran" in t or "IRGC" in t for t in titles)
    assert "Brazil Pix digital payment system update" not in titles


def test_filter_rows_by_residual_empty_predicate_is_passthrough():
    rows = [_sig("anything"), _sig("else")]
    assert filter_rows_by_residual("", rows) == rows


def test_filter_rows_by_residual_bad_predicate_fails_closed_whole_batch():
    """A predicate that cannot compile drops the WHOLE slice (returns []) —
    loud-but-safe: a thematic target with a broken predicate reads nothing
    rather than everything."""
    rows = [_sig("Iran strike")]
    # references a helper that needs ctx no slice provides on this surface.
    assert filter_rows_by_residual('org_match()', rows) == []


def test_diversify_by_source_caps_a_firehose_when_diversity_exists():
    """FIX-3: a broad-pool slice dominated by one high-volume source (NWS) is
    capped per-source so geopolitical/news signals reach the assessor. With a
    realistic diverse pool (over-fetch 200: a firehose + many other sources) the
    cap holds at 15 NWS and the rest fill from the diverse tail."""
    # 160 NWS (recency-first) + 60 across 30 distinct news sources (2 each).
    rows = [{"source_id": "source.nws.active_alerts", "id": i} for i in range(160)]
    rows += [{"source_id": f"source.news.{i % 30}", "id": 1000 + i} for i in range(60)]
    kept = _diversify_by_source(rows, per_source_cap=15, limit=50)
    assert len(kept) == 50
    nws = sum(1 for r in kept if r["source_id"] == "source.nws.active_alerts")
    assert nws == 15                             # firehose capped at the cap
    assert sum(1 for r in kept if r["source_id"].startswith("source.news")) == 35


def test_diversify_by_source_backfills_when_diversity_exhausted():
    """When the pool lacks enough distinct sources to fill the limit under the
    cap, back-fill in recency order so the slice is never smaller than the plain
    recency cut would have been (don't starve a thin-but-single-source day)."""
    rows = [{"source_id": "source.only", "id": i} for i in range(70)]
    kept = _diversify_by_source(rows, per_source_cap=15, limit=50)
    assert len(kept) == 50  # back-fill to the limit from the only available source


def test_global_slice_per_source_cap_env(monkeypatch):
    monkeypatch.delenv("LEGBA_GLOBAL_SLICE_PER_SOURCE_CAP", raising=False)
    assert _global_slice_per_source_cap() == 15
    monkeypatch.setenv("LEGBA_GLOBAL_SLICE_PER_SOURCE_CAP", "10")
    assert _global_slice_per_source_cap() == 10
    monkeypatch.setenv("LEGBA_GLOBAL_SLICE_PER_SOURCE_CAP", "junk")
    assert _global_slice_per_source_cap() == 15


def test_critic_fanout_max_env(monkeypatch):
    # #75: the old hardcoded 4/tick was an under-capacity. Default raised to 12,
    # env-tunable so an operator can pace backlog drain (budget stays the ceiling).
    monkeypatch.delenv("LEGBA_CRITIC_FANOUT_MAX", raising=False)
    assert _critic_fanout_max() == 12
    monkeypatch.setenv("LEGBA_CRITIC_FANOUT_MAX", "30")
    assert _critic_fanout_max() == 30
    monkeypatch.setenv("LEGBA_CRITIC_FANOUT_MAX", "0")   # floored to >=1
    assert _critic_fanout_max() == 1
    monkeypatch.setenv("LEGBA_CRITIC_FANOUT_MAX", "junk")
    assert _critic_fanout_max() == 12


def test_thematic_scope_validates_as_target_descriptor():
    from legba.data.schemas.target import TargetDescriptor, ThematicScope

    body = {
        "identity": {
            "id": "situation_test", "name": "Situation Test",
            "schema_uri": "legba/target/2.0.0", "version": "0" * 16,
            "abstraction_level": "L1", "inherits": [], "state": "active",
            "owner": "t", "created": "2026-06-20T00:00:00Z",
        },
        "scope": {
            "domain": "thematic",
            "themes": ["iran", "war"],
            "geo": [],
            "entity_classes": ["country", "person"],
            "time_horizon_days": 90,
            "predicate": _IRAN_WAR_PRED,
            "tags": ["thematic", "situation"],
        },
        "sources": [
            {"source_selector": {"tags": ["news"], "kinds": ["rss"],
                                 "owner_tenant": "shared"},
             "subscription": {"canonical_only": True}},
        ],
    }
    desc = TargetDescriptor.model_validate(body, strict=False)
    assert isinstance(desc.scope, ThematicScope)
    assert desc.scope.domain == "thematic"
    assert desc.scope.predicate is not None  # compiled at registration
