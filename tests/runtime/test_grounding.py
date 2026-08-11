# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tier-1 knowledge-grounding tests (L-241 grounding).

Covers the analysis-time current-world-state injection that fixes
stale-cutoff analyst LLMs backfilling e.g. "former president":

  * candidate extraction (target geo + slice entities, junk-rejection);
  * the substrate resolver's current-facts gate (a superseded / expired
    leader is NOT returned — the stub records the SQL it ran);
  * preamble assembly (dated header + one line per fact/nexus; empty → None);
  * the inline_target runner PREPENDS the preamble (and is byte-for-byte
    unchanged when no grounding hook is wired — off-by-default);
  * CANARY: a US "head of state = Donald Trump (current)" fact lands in the
    assembled LLM context a US assessor sees;
  * the deps-builder gate (`grounding.enabled`) decides whether a hook is
    installed at all.

These are pure-Python unit tests — the substrate reads go through a recording
stub pool, so no live DB is needed (the candidate / preamble / runner paths
have no DB dependency at all). The Tier-0 supersession test (a real leader
change closing the prior fact) is the PG integration test in
``tests/data_pkg/test_seed_grounding_supersession.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

import pytest

from legba.data.analysts.inline_target import InlineTargetDeps, run_method
from legba.runtime.grounding import (
    GroundingFact,
    GroundingGraphStructure,
    GroundingNarrative,
    GroundingNexus,
    GroundingSituation,
    SubstrateGroundingResolver,
    build_graph_structure_block,
    build_grounding_preamble,
    build_narratives_block,
    build_situations_block,
    collect_grounding_candidates,
    is_non_event_situation_name,
    situation_scope_for_target,
)
from legba.runtime.grounding import (
    GroundingInterestingItem,
    _collect_interesting,
    _top_brokers,
    _top_graph_items,
    _top_proxy_chains,
)


# --- ASSESSED STRUCTURE block (graph-in-analysis, Phase 0) -------------------


def test_graph_structure_block_renders_fenced_analysis_derived():
    s = GroundingGraphStructure(
        frustration=[("Iran", 22.0), ("United States", 19.0)],
        brokers=[("Iran", 0.028)],
        proxy_chains=["A → C → B [hostile path]"],
    )
    block = build_graph_structure_block(s)
    assert block is not None
    # Fenced + clearly analysis-derived, NOT ground truth (trust boundary).
    assert "ASSESSED STRUCTURE" in block
    assert "NOT operator-vetted ground truth" in block
    assert "Iran (22)" in block          # frustration as an int count
    assert "Iran (0.028)" in block        # broker betweenness
    assert "A → C → B [hostile path]" in block


def test_graph_structure_block_empty_returns_none():
    assert build_graph_structure_block(None) is None
    assert build_graph_structure_block(
        GroundingGraphStructure(frustration=[], brokers=[], proxy_chains=[])
    ) is None


def test_top_graph_items_prioritises_candidates_then_drops_junk():
    # candidate (iran) first even though United States has a higher value;
    # too-short / bare-QID names dropped.
    out = _top_graph_items(
        {"Iran": 22, "United States": 99, "US": 5, "Q42": 7}, {"iran"}, 6, min_value=0.0,
    )
    assert out[0][0] == "Iran"                      # candidate floated to front
    names = [n for n, _ in out]
    assert "United States" in names and "US" not in names and "Q42" not in names


def test_top_brokers_excludes_zero_betweenness_hubs():
    # a high-degree catalog hub with betweenness 0 is NOT a broker.
    out = _top_brokers(
        {"Iran": {"betweenness": 0.028, "degree": 71},
         "Interpol": {"betweenness": 0.0, "degree": 185}}, set(), 6,
    )
    assert [n for n, _ in out] == ["Iran"]


def test_top_proxy_chains_renders_dict_and_signs_path():
    out = _top_proxy_chains([{"subject": "A", "via": "C", "object": "B", "sign": -1}], set(), 6)
    assert out == ["A → C → B [hostile path]"]


# --- ASSESSED STRUCTURE — the shared "interesting" shortlist (P2 #99) --------


def test_collect_interesting_dedupes_scopes_and_ranks():
    payloads = {
        "graph_mining": {
            "interesting": [
                {"kind": "broker", "label": "Turkey", "score": 0.91,
                 "rationale": "between camps", "entities": ["Turkey"]},
                {"kind": "tense_actor", "label": "Brazil", "score": 0.99,
                 "rationale": "global top", "entities": ["Brazil"]},
            ],
        },
        "structural_balance": {
            "interesting": [
                {"kind": "tense_actor", "label": "Iran", "score": 0.5,
                 "rationale": "iran tense", "entities": ["Iran"]},
                # exact dup of the broker above — must dedupe on (kind, label).
                {"kind": "broker", "label": "Turkey", "score": 0.91,
                 "rationale": "dup", "entities": ["Turkey"]},
            ],
        },
    }
    items = _collect_interesting(payloads, {"iran"}, 12)
    # dedup: 3 distinct (Turkey/Brazil/Iran), and the in-scope Iran (lower score)
    # floats ahead of the higher-scored global Brazil.
    assert len(items) == 3
    assert items[0].label == "Iran"
    assert isinstance(items[0], GroundingInterestingItem)


def test_collect_interesting_drops_junk_rows():
    payloads = {
        "graph_mining": {
            "interesting": [
                {"kind": "broker", "label": "", "score": 0.9},          # no label
                {"kind": "broker", "score": 0.9},                        # missing label
                "not-a-dict",                                            # wrong type
                {"kind": "broker", "label": "Turkey", "score": "x"},     # bad score → 0.0
            ],
        },
    }
    items = _collect_interesting(payloads, set(), 12)
    assert [i.label for i in items] == ["Turkey"]
    assert items[0].score == 0.0


def test_graph_structure_block_prefers_interesting_shortlist():
    s = GroundingGraphStructure(
        frustration=[], brokers=[], proxy_chains=[],
        interesting=[
            GroundingInterestingItem(
                kind="tense_actor", label="Iran", score=0.95,
                rationale="most sign-imbalanced ties", entities=["Iran"]),
            GroundingInterestingItem(
                kind="proxy_chain", label="Iran -> Hezbollah -> Israel", score=0.8,
                rationale="hostile cut-out path", entities=["Iran", "Israel"]),
        ],
    )
    block = build_graph_structure_block(s)
    assert block is not None
    assert "ASSESSED STRUCTURE" in block
    assert "NOT operator-vetted ground truth" in block
    # kind-grouped with the producer's rationale rendered.
    assert "Most structurally tense actors" in block
    assert "Iran — most sign-imbalanced ties" in block
    assert "Iran -> Hezbollah -> Israel — hostile cut-out path" in block


# ---------------------------------------------------------------------------
# Recording stub pool — captures (sql, params) so a test can assert the
# current-facts gate is in the WHERE clause and feed canned rows back.
# ---------------------------------------------------------------------------


class _StubConn:
    def __init__(self, fetch_rows: dict[str, list[dict[str, Any]]], log: list[tuple[str, tuple]]):
        self._fetch_rows = fetch_rows
        self._log = log

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self._log.append((sql, params))
        # Route by table keyword so one stub serves facts + edges + situations.
        if "FROM facts" in sql:
            return self._fetch_rows.get("facts", [])
        # W3-A: the signed ground-truth relationships come from `entity_edges`
        # now. The fixture key stays "nexuses" — it names the PREAMBLE SECTION
        # these rows render into, and every test in this file uses it.
        if "FROM entity_edges" in sql or "FROM nexuses" in sql:
            return self._fetch_rows.get("nexuses", [])
        if "FROM situations" in sql:
            return self._fetch_rows.get("situations", [])
        if "FROM narratives" in sql:
            return self._fetch_rows.get("narratives", [])
        return []


class _StubAcquire:
    def __init__(self, conn: _StubConn):
        self._conn = conn

    async def __aenter__(self) -> _StubConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _StubPool:
    """asyncpg.Pool-shaped stub: ``async with pool.acquire() as conn``."""

    def __init__(self, fetch_rows: dict[str, list[dict[str, Any]]] | None = None):
        self.log: list[tuple[str, tuple]] = []
        self._conn = _StubConn(fetch_rows or {}, self.log)

    def acquire(self) -> _StubAcquire:
        return _StubAcquire(self._conn)


# ---------------------------------------------------------------------------
# LLM test double — captures the user prompt the runner actually sends.
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _CapturingLLM:
    subprovider = "openai"

    def __init__(self) -> None:
        self.last_user_prompt: str | None = None

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        # inline_target sends one user message carrying the rendered prompt.
        self.last_user_prompt = messages[-1]["content"] if messages else ""
        finding = {"title": "t", "body": "b", "confidence": 0.5, "evidence": [], "tags": []}
        return _Response(content=json.dumps(finding), usage=_Usage())


def _signal(geo: list[str] | None = None, tags: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "title": "Some event",
        "produced_at": "2026-06-18T00:00:00+00:00",
        "source_url": "https://example.invalid/x",
        "data": {"summary": "an observation"},
        "geo": geo or [],
        "tags": tags or [],
    }


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------


def test_candidates_target_geo_from_target_id_and_slice():
    inputs = [_signal(geo=["United States", "United States"]), _signal(geo=["United States"])]
    cands = collect_grounding_candidates(
        inputs, target_id="country_g20_us", scope=["target_geo"],
    )
    # target_id slug token first, then the dominant slice geo.
    assert "us" in [c.casefold() for c in cands]
    assert "united states" in [c.casefold() for c in cands]


def test_candidates_slice_entities_reject_junk_and_provenance_tags():
    inputs = [
        _signal(tags=["Donald Trump", "target:country_g20_us", "severity:high", "42", "g20", "US"])
    ]
    cands = collect_grounding_candidates(inputs, target_id=None, scope=["slice_entities"])
    lowered = [c.casefold() for c in cands]
    assert "donald trump" in lowered
    # provenance/synthetic tags + pure-numeric + sub-3-char are dropped.
    assert not any(c.startswith(("target:", "severity:")) for c in cands)
    assert "42" not in cands
    assert "us" not in lowered  # < 3 chars
    assert "g20" not in lowered  # synthetic scope tag


def test_candidates_lift_payload_entities_conf_gated():
    """FIX-1: NER writes named entities to payload.entities (row['data']['entities'])
    — NOT tags/key_entities, which are usually empty on a raw ingested signal.
    collect_grounding_candidates must lift them (conf-gated) so a geopolitical
    signal in the slice actually contributes its countries as candidates."""
    row = {
        "geo": [], "tags": [],
        "data": {"entities": [
            {"text": "Iran", "class": "location", "confidence": 1.0},
            {"text": "Tehran", "class": "location", "confidence": 0.9},
            {"text": "noise", "class": "misc", "confidence": 0.2},  # below 0.5 → dropped
        ]},
    }
    cands = [c.casefold() for c in collect_grounding_candidates(
        [row], target_id=None, scope=["slice_entities"])]
    assert "iran" in cands and "tehran" in cands
    assert "noise" not in cands


def test_candidates_static_prepended_regardless_of_slice():
    """FIX-2: static_candidates are always added even when the slice is empty /
    flooded — guarantees the global assessor grounds the ongoing world-state."""
    cands = collect_grounding_candidates(
        [_signal()], target_id=None, scope=["slice_entities"],
        static_candidates=["Iran", "United States", "Israel"],
    )
    lc = [c.casefold() for c in cands]
    assert "iran" in lc and "united states" in lc and "israel" in lc


def test_candidates_empty_scope_returns_nothing():
    inputs = [_signal(geo=["United States"], tags=["Donald Trump"])]
    assert collect_grounding_candidates(inputs, target_id="country_g20_us", scope=[]) == []


# ---------------------------------------------------------------------------
# Resolver — current-facts gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_applies_current_facts_gate_in_sql():
    pool = _StubPool(fetch_rows={"facts": [], "nexuses": []})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    await resolver.resolve(["United States"], max_facts=30)
    facts_sql = next(sql for sql, _ in pool.log if "FROM facts" in sql)
    # The temporal-honesty gate (matches substrate_query_port). The fact query
    # is aliased ``f`` since Wave 5 LEFT JOINs the contention sidecar (``fc``),
    # so the gate columns now carry the ``f.`` qualifier.
    assert "f.superseded_by IS NULL" in facts_sql
    assert "f.valid_until IS NULL OR f.valid_until > now()" in facts_sql
    # Curated/seed provenance is preferred in the ORDER BY.
    assert "source_type IN ('seed','curated')" in facts_sql


@pytest.mark.asyncio
async def test_resolver_empty_candidates_short_circuits_no_query():
    pool = _StubPool()
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    facts, nexuses = await resolver.resolve([], max_facts=30)
    assert facts == [] and nexuses == []
    assert pool.log == [], "no candidates → no DB query"


@pytest.mark.asyncio
async def test_resolver_skips_bare_qid_fact_value():
    """A fact whose VALUE degraded to a bare Wikidata QID (label lookup failed)
    must NOT reach the preamble — injecting 'head of state: Q22686' is worse
    than nothing. The labelled fact survives; the bare-QID one is dropped."""
    rows = {
        "facts": [
            {
                "subject": "United States", "predicate": "head of state",
                "value": "Q22686",  # bare QID — unreadable
                "valid_from": datetime(2025, 1, 20, tzinfo=timezone.utc),
                "source_type": "seed", "confidence": 0.95,
            },
            {
                "subject": "Mexico", "predicate": "head of state",
                "value": "Donald Trump",  # normal label — passes through
                "valid_from": datetime(2025, 1, 20, tzinfo=timezone.utc),
                "source_type": "seed", "confidence": 0.95,
            },
        ],
        "nexuses": [],
    }
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(fetch_rows=rows))
    facts, _ = await resolver.resolve(["United States", "Mexico"], max_facts=30)
    values = [f.value for f in facts]
    assert "Q22686" not in values  # the bare QID is skipped entirely
    assert "Donald Trump" in values  # a normal name still passes
    preamble = build_grounding_preamble(facts, [])
    assert preamble is not None
    assert "Q22686" not in preamble


@pytest.mark.asyncio
async def test_resolver_sql_excludes_bare_qid_values():
    """The fact query carries a bare-QID exclusion so the LIMIT budget is spent
    only on renderable facts (the budget is not consumed by skipped QID rows)."""
    pool = _StubPool(fetch_rows={"facts": [], "nexuses": []})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    await resolver.resolve(["United States"], max_facts=30)
    facts_sql = next(sql for sql, _ in pool.log if "FROM facts" in sql)
    assert "value !~ '^Q[0-9]+$'" in facts_sql


# ---------------------------------------------------------------------------
# Provenance gate — only operator-vetted source_type reaches the preamble.
# The behavioral proof (ingestion junk on the SAME subject is dropped against a
# real Postgres) is tests/data_pkg/test_grounding_provenance_gate.py; here we
# assert the SQL FILTER + bound param + the env override.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_facts_sql_filters_to_trusted_provenance():
    """The grounding-quality gate: facts are restricted to vetted provenance in
    the WHERE clause (not merely preferred in the ORDER BY), and the trusted
    source_types ride as a bound param — so ingestion/agent NER junk
    ('Iran controls Israel', 'Hitler leader of Germany' at conf 1.0) can never
    reach the 'treat as ground truth' preamble."""
    pool = _StubPool(fetch_rows={"facts": [], "nexuses": []})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    await resolver.resolve(["Iran"], max_facts=30)
    facts_sql, facts_params = next(
        (sql, p) for sql, p in pool.log if "FROM facts" in sql
    )
    assert "source_type = ANY($2::text[])" in facts_sql
    # The trusted set is the 2nd bound param == the default seed/curated gate.
    assert list(facts_params[1]) == ["seed", "curated"]


@pytest.mark.asyncio
async def test_resolver_nexuses_sql_filters_to_trusted_provenance():
    """The same provenance gate applies to signed edges — the reified /
    promoted 'agent' lane is an analysis product, excluded from ground truth.

    W3-A adds a SECOND gate on the same query: `edge_family`. It does not
    replace the provenance gate, it narrows it — a curated edge somehow tiered
    `cooccurrence` is still a co-mention and still must not be asserted as
    ground truth. Note the family list here is the INVERSE of the analytics
    handlers': grounding WANTS the imported `reference` lattice, because it is
    rendering things that are true rather than measuring alignment.
    """
    from legba.runtime.grounding import _GROUNDING_EDGE_FAMILIES

    pool = _StubPool(fetch_rows={"facts": [], "nexuses": []})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    await resolver.resolve(["Iran"], max_facts=30)  # facts empty → edge query runs
    nexus_sql, nexus_params = next(
        (sql, p) for sql, p in pool.log if "FROM entity_edges" in sql
    )
    assert "e.source_type = ANY($2::text[])" in nexus_sql
    assert list(nexus_params[1]) == ["seed", "curated"]
    assert "e.edge_family = ANY($4::text[])" in nexus_sql
    assert list(nexus_params[3]) == list(_GROUNDING_EDGE_FAMILIES)
    assert "cooccurrence" not in _GROUNDING_EDGE_FAMILIES
    assert "reference" in _GROUNDING_EDGE_FAMILIES


def test_trusted_source_types_default_and_env_override(monkeypatch):
    from legba.runtime.grounding import trusted_source_types

    # Default when unset.
    monkeypatch.delenv("LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES", raising=False)
    assert trusted_source_types() == ("seed", "curated")
    # Blank / whitespace → safe default (never an empty admit-nothing set).
    monkeypatch.setenv("LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES", "   ")
    assert trusted_source_types() == ("seed", "curated")
    # Custom comma list — lowercased + trimmed, order preserved.
    monkeypatch.setenv("LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES", "Seed, Wikidata ,curated")
    assert trusted_source_types() == ("seed", "wikidata", "curated")
    # All-empty tokens collapse back to the safe default.
    monkeypatch.setenv("LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES", ", ,")
    assert trusted_source_types() == ("seed", "curated")


# ---------------------------------------------------------------------------
# Situations grounding (Phase 5a.3) — the ASSESSED SITUATIONS block, rendered
# SEPARATELY from + fenced off against the ground-truth fact block.
# ---------------------------------------------------------------------------


def test_situation_scope_for_target_scopes_country_else_global():
    # A country target scopes to its own situations (situations.target_id).
    assert situation_scope_for_target("country_g20_us") == "country_g20_us"
    # A global meta-analyst (no country target) grounds against ALL targets.
    assert situation_scope_for_target(None) is None
    assert situation_scope_for_target("world_assessor") is None
    assert situation_scope_for_target("") is None


@pytest.mark.asyncio
async def test_resolve_situations_sql_gates_open_frames_and_scope():
    """The situations query surfaces only OPEN frames (valid_until NULL / future
    AND status != closed) and scopes by target_id param."""
    pool = _StubPool(fetch_rows={"situations": []})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    await resolver.resolve_situations(scope_target_id="country_g20_us", limit=8)
    sql, params = next((s, p) for s, p in pool.log if "FROM situations" in s)
    assert "superseded_by IS NULL" in sql
    assert "valid_until IS NULL OR valid_until > now()" in sql
    assert "status <> 'closed'" in sql
    assert "intensity_score >= $2" in sql       # the quality floor
    assert "target_id = $1" in sql              # scope on the populated target_id
    assert params[0] == "country_g20_us"         # the scope filter param
    assert params[1] == 0.0                      # default intensity floor (off)


@pytest.mark.asyncio
async def test_resolve_situations_drops_non_event_frames():
    """A clustered 'nothing to report' non-event frame is never grounded."""
    rows = {
        "situations": [
            {
                "name": "No France-specific weather alerts in the latest batch of signals",
                "category": "country_g20_fr", "status": "dormant",
                "intensity_score": 1.4, "valid_from": None, "last_event_at": None,
            },
            {
                "name": "US–Iran War", "category": "country_g20_ir",
                "status": "active", "intensity_score": 1.6,
                "valid_from": datetime(2026, 2, 28, tzinfo=timezone.utc),
                "last_event_at": None,
            },
        ]
    }
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(fetch_rows=rows))
    sits = await resolver.resolve_situations(scope_target_id=None, limit=8)
    names = {s.name for s in sits}
    assert "US–Iran War" in names
    assert not any(n.startswith("No France-specific") for n in names)


def test_non_event_regex_matches_live_status_quo_shapes():
    """DQ P6 — the broadened non-event filter catches the MID-STRING status-quo
    frames the legacy 'starts-with-No' anchor missed (the live pollution class),
    while still catching the legacy 'No … alerts … in the latest batch' shape."""
    steady_state = [
        "United States – No observable WMD proliferation activity",
        "Canada – No discernible standing military posture shift",
        "Saudi Arabia – No significant internal instability signals",
        # DQ P6 r2: 'clear'/'evident' non-observation qualifiers (Japan/Saudi live
        # frames the round-1 alternation missed).
        "Japan – No clear standing military posture shift",
        "Saudi Arabia – No clear standing military posture shift",
        "Argentina – No evident change in standing military posture",
        "France – No observable coercive economic pressure (neither target nor wielder)",
        "Canada – Stability maintained (no dominant instability vector)",
        "North Korea – Status quo across examined domains with thin evidence",
        "Russia – Low leadership transition risk",
        "Taiwan – Overall Stability with Low Near-Term Escalation Risk",
        "No coordinated narrative detected – organic heat-wave coverage",
        # legacy shape must still match
        "No France-specific weather alerts in the latest batch of signals",
    ]
    for name in steady_state:
        assert is_non_event_situation_name(name), f"should be non-event: {name!r}"


def test_non_event_regex_keeps_real_event_frames():
    """The filter must NOT swallow real EVENT frames — including 'No-fly'/'No
    deal' events (not status-quo qualifiers) and legitimate energy_security
    'low/elevated energy-security pressure' reads (a low read is a real
    assessment, not a non-event)."""
    real_frames = [
        "South Korea – Border Island Live-Fire Drills Drive Escalation Risk",
        "UK – Naval Drone Demonstration Drives Escalation Risk",
        "India – Emerging social-media-driven unrest",
        "Argentina – Elite/Regime Fracture",
        "Germany faces low current energy-security pressure",
        "Japan faces elevated energy-security pressure",
        "No-fly zone declared over the contested corridor",
        "No deal reached in the ceasefire talks",
        # DQ P6 r2: the negation branch is ANCHORED — a real event whose post-
        # qualifier word is a CHANGE noun ("de-escalation"), or that merely mentions
        # "no significant" mid-sentence, must NOT be classified non-event.
        "No significant de-escalation; airstrikes intensify along the border",
        "Iran – No significant de-escalation; airstrikes intensify along the border",
        "Airstrikes continue with no significant restraint reported by monitors",
        "US–Iran War",
    ]
    for name in real_frames:
        assert not is_non_event_situation_name(name), f"should be real: {name!r}"


def test_situation_grounding_min_intensity_env(monkeypatch):
    from legba.runtime.grounding import situation_grounding_min_intensity

    monkeypatch.delenv("LEGBA_SITUATION_GROUNDING_MIN_INTENSITY", raising=False)
    assert situation_grounding_min_intensity() == 0.0   # off by default
    monkeypatch.setenv("LEGBA_SITUATION_GROUNDING_MIN_INTENSITY", "1.5")
    assert situation_grounding_min_intensity() == 1.5
    monkeypatch.setenv("LEGBA_SITUATION_GROUNDING_MIN_INTENSITY", "garbage")
    assert situation_grounding_min_intensity() == 0.0   # bad value → off


@pytest.mark.asyncio
async def test_resolve_situations_global_scope_passes_null_target():
    pool = _StubPool(fetch_rows={"situations": []})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    await resolver.resolve_situations(scope_target_id=None, limit=8)
    _sql, params = next((s, p) for s, p in pool.log if "FROM situations" in s)
    assert params[0] is None  # global view → no target_id filter


@pytest.mark.asyncio
async def test_resolve_situations_maps_rows():
    rows = {
        "situations": [
            {
                "name": "US–Iran War", "category": "country_g20_ir",
                "status": "active", "intensity_score": 1.5,
                "valid_from": datetime(2026, 2, 28, tzinfo=timezone.utc),
                "last_event_at": None,
            }
        ]
    }
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(fetch_rows=rows))
    sits = await resolver.resolve_situations(scope_target_id="country_g20_ir", limit=8)
    assert len(sits) == 1
    assert sits[0].name == "US–Iran War"
    assert "ongoing since 2026-02-28" in sits[0].render()


def test_situation_render_shows_staleness_age():
    """A dormant frame's last-activity age is surfaced so the LLM down-weights
    a quiet frame (review follow-up #66.1)."""
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    s = GroundingSituation(
        name="Quiet Frame", category="country_g20_fr", status="dormant",
        intensity_score=1.2, valid_from=datetime(2026, 6, 10, tzinfo=timezone.utc),
        last_event_at=datetime(2026, 6, 13, tzinfo=timezone.utc),
    )
    rendered = s.render(now=now)
    assert "last activity 7d ago" in rendered
    assert "dormant" in rendered
    # No last_event_at → no staleness clause.
    s2 = GroundingSituation(
        name="X", category=None, status="active", intensity_score=1.0,
        valid_from=None, last_event_at=None,
    )
    assert "last activity" not in s2.render(now=now)


def test_build_situations_block_is_labelled_and_not_ground_truth():
    block = build_situations_block([
        GroundingSituation(
            name="US–Iran War", category="country_g20_ir", status="active",
            intensity_score=1.5, valid_from=datetime(2026, 2, 28, tzinfo=timezone.utc),
        )
    ])
    assert block is not None
    assert "ASSESSED SITUATIONS" in block
    assert "NOT operator-vetted ground truth" in block
    assert "- US–Iran War" in block
    # The trust-boundary header of the GROUND-TRUTH block must NOT appear here.
    assert "AUTHORITATIVE CURRENT CONTEXT" not in block


def test_build_situations_block_none_when_empty():
    assert build_situations_block([]) is None


# --- ASSESSED NARRATIVES block (W-3e — mig 0102 sidecar as a grounding source)


def _narrative_row(
    subject: str = "iran",
    *,
    status: str = "contested",
    surfaced_value: Any = None,
    lead: str | None = "source_wire_a",
) -> dict[str, Any]:
    return {
        "subject_key": subject,
        "predicate_key": "enrichment level",
        "status": status,
        "surfaced_value": surfaced_value,
        "variant_count": 3,
        "carrier_source_count": 5,
        "publish_dated_source_count": 4,
        "first_seen_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "last_seen_at": datetime(2026, 7, 26, tzinfo=timezone.utc),
        "lead_source_id": lead,
        "max_echo_lag_hours": 36.0,
    }


@pytest.mark.asyncio
async def test_resolve_narratives_empty_sidecar_yields_empty_and_no_block():
    """Honest empty state: nothing in the sidecar ⇒ [] ⇒ NO block (no
    fabricated header). The SQL excludes collapsed families."""
    pool = _StubPool(fetch_rows={"narratives": []})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    out = await resolver.resolve_narratives(target_id=None, limit=8)
    assert out == []
    assert build_narratives_block(out) is None
    sql, params = pool.log[-1]
    assert "FROM narratives" in sql
    assert "status <> 'collapsed'" in sql
    assert "ORDER BY last_seen_at DESC" in sql


@pytest.mark.asyncio
async def test_resolve_narratives_populated_maps_rows_and_renders():
    pool = _StubPool(fetch_rows={"narratives": [
        _narrative_row(),
        _narrative_row(subject="strait shipping", status="surfaced",
                       surfaced_value="closed to tankers", lead=None),
    ]})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    out = await resolver.resolve_narratives(target_id=None, limit=8)
    assert [n.subject_key for n in out] == ["iran", "strait shipping"]
    r0 = out[0].render()
    assert "[contested] 'iran' enrichment level" in r0
    assert "3 competing variant(s) across 5 source(s)" in r0
    assert "active 2026-07-20 -> 2026-07-26" in r0
    assert "first published by source_wire_a" in r0
    # Echo-lead honesty rides in the render itself, not only the header.
    assert "NOT evidence of copying" in r0
    assert "no surfaced winner — do not treat any variant as settled" in r0
    r1 = out[1].render()
    assert "arbiter-surfaced winner='closed to tankers'" in r1

    block = build_narratives_block(out)
    assert block is not None
    assert "ASSESSED NARRATIVES" in block
    assert "detect-only" in block
    assert "NOT operator-vetted ground truth" in block
    assert "never by itself evidence of copying or coordination" in block
    # Fenced off: never laundered into the ground-truth block's header.
    assert "AUTHORITATIVE CURRENT CONTEXT" not in block


@pytest.mark.asyncio
async def test_resolve_narratives_scopes_per_country_by_subject_whole_word():
    """A per-country run keeps only narratives whose subject mentions the
    target's geo names — whole-word ('in' the ISO slug never matches inside
    'shipping'; 'india' matches)."""
    pool = _StubPool(fetch_rows={"narratives": [
        _narrative_row(subject="india border clashes"),
        _narrative_row(subject="brazil currency policy"),
        _narrative_row(subject="shipping insurance"),  # 'in' must NOT match
    ]})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    out = await resolver.resolve_narratives(target_id="country_g20_in", limit=8)
    assert [n.subject_key for n in out] == ["india border clashes"]
    # Global run keeps everything (recency top, no scope filter).
    out_global = await resolver.resolve_narratives(target_id=None, limit=8)
    assert len(out_global) == 3


@pytest.mark.asyncio
async def test_resolve_narratives_read_failure_degrades_to_empty():
    """Degrade-not-drop: a read failure (e.g. a pre-0102 DB without the
    sidecar) logs + yields [] — never an exception into the run."""

    class _RaisingPool:
        def acquire(self):
            raise RuntimeError("relation narratives does not exist")

    resolver = SubstrateGroundingResolver(pg_pool=_RaisingPool())
    assert await resolver.resolve_narratives(target_id=None, limit=8) == []


@pytest.mark.asyncio
async def test_resolve_narratives_bounded_by_cap():
    rows = [
        _narrative_row(subject=f"topic {i}") for i in range(20)
    ]
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(fetch_rows={"narratives": rows})
    )
    # A generous caller budget is clamped to the module cap (8).
    out = await resolver.resolve_narratives(target_id=None, limit=30)
    assert len(out) == 8


@pytest.mark.asyncio
async def test_resolver_bare_qid_does_not_consume_fact_budget():
    """With a small max_facts, a bare-QID fact returned by the stub must not
    eat a budget slot — the renderable facts still all land."""
    rows = {
        "facts": [
            {
                "subject": "United States", "predicate": "head of state",
                "value": "Q22686",  # bare QID — should be skipped, not counted
                "valid_from": datetime(2025, 1, 20, tzinfo=timezone.utc),
                "source_type": "seed", "confidence": 0.99,
            },
            {
                "subject": "United States", "predicate": "head of government",
                "value": "Jane Doe",
                "valid_from": datetime(2025, 1, 20, tzinfo=timezone.utc),
                "source_type": "seed", "confidence": 0.90,
            },
            {
                "subject": "United States", "predicate": "capital",
                "value": "Washington",
                "valid_from": None, "source_type": "seed", "confidence": 0.90,
            },
        ],
        "nexuses": [],
    }
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(fetch_rows=rows))
    # Budget of 2: a naive count would let the QID row crowd one real fact out;
    # skipping it means both renderable facts survive.
    facts, _ = await resolver.resolve(["United States"], max_facts=2)
    values = [f.value for f in facts]
    assert "Q22686" not in values
    assert "Jane Doe" in values
    assert "Washington" in values


@pytest.mark.asyncio
async def test_resolver_skips_bare_qid_nexus_endpoints():
    """A signed nexus whose subject OR object is a bare QID renders an
    unreadable edge line — it is skipped; a fully-labelled edge survives."""
    rows = {
        "facts": [],
        "nexuses": [
            {
                "subject": "Q30", "rel_type": "member of", "object": "NATO",
                "polarity": 1, "valid_from": None,
            },
            {
                "subject": "United States", "rel_type": "member of", "object": "Q1065",
                "polarity": 1, "valid_from": None,
            },
            {
                "subject": "United States", "rel_type": "member of", "object": "NATO",
                "polarity": 1, "valid_from": datetime(1949, 4, 4, tzinfo=timezone.utc),
            },
        ],
    }
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(fetch_rows=rows))
    _, nexuses = await resolver.resolve(["United States"], max_facts=30)
    rendered = [n.render() for n in nexuses]
    assert len(nexuses) == 1
    assert any("United States member of NATO" in r for r in rendered)
    assert not any("Q30" in r or "Q1065" in r for r in rendered)


@pytest.mark.asyncio
async def test_grounding_iran_preamble_includes_current_leader_and_war():
    """CURRENT-WORLD-STATE canary: an Iran-scoped grounding preamble carries the
    CURRENT Supreme Leader (Mojtaba Khamenei) AND the active US-Israel-Iran war.

    This is the temporal-collapse fix: a stale-cutoff world_assessor that lacks
    these facts would frame a months-old strike as current. The seed (Tier 0)
    makes them current/temporally-honest; here the resolver (Tier 1) surfaces
    BOTH the country-subject office fact and the signed -1 active-conflict
    nexuses for the same Iran candidate, and the preamble renders them cleanly.
    """
    rows = {
        "facts": [
            {
                "subject": "Iran", "predicate": "head of state",
                "value": "Mojtaba Khamenei",
                "valid_from": datetime(2026, 3, 8, tzinfo=timezone.utc),
                "source_type": "seed", "confidence": 0.95,
            },
            {
                "subject": "Iran", "predicate": "head of government",
                "value": "Masoud Pezeshkian",
                "valid_from": datetime(2024, 7, 28, tzinfo=timezone.utc),
                "source_type": "seed", "confidence": 0.95,
            },
        ],
        "nexuses": [
            {
                "subject": "Iran", "rel_type": "in active conflict with",
                "object": "United States", "polarity": -1,
                "valid_from": datetime(2026, 2, 28, tzinfo=timezone.utc),
            },
            {
                "subject": "Iran", "rel_type": "in active conflict with",
                "object": "Israel", "polarity": -1,
                "valid_from": datetime(2026, 2, 28, tzinfo=timezone.utc),
            },
        ],
    }
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(fetch_rows=rows))
    facts, nexuses = await resolver.resolve(["Iran"], max_facts=30)
    preamble = build_grounding_preamble(
        facts, nexuses, now=datetime(2026, 6, 19, tzinfo=timezone.utc),
    )
    assert preamble is not None
    # The CURRENT Supreme Leader (NOT the killed Ali Khamenei — the resolver's
    # current-facts gate already excludes the closed row).
    assert "Iran — head of state: Mojtaba Khamenei (since 2026-03-08)" in preamble
    assert "Iran — head of government: Masoud Pezeshkian (since 2024-07-28)" in preamble
    # The active-conflict layer renders as a signed antagonistic relationship.
    assert "Iran in active conflict with United States [antagonistic] (since 2026-02-28)" in preamble
    assert "Iran in active conflict with Israel [antagonistic] (since 2026-02-28)" in preamble


@pytest.mark.asyncio
async def test_resolver_maps_rows_to_grounding_facts():
    rows = {
        "facts": [
            {
                "subject": "United States",
                "predicate": "head of state",
                "value": "Donald Trump",
                "valid_from": datetime(2025, 1, 20, tzinfo=timezone.utc),
                "source_type": "seed",
                "confidence": 0.95,
            }
        ],
        "nexuses": [],
    }
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(fetch_rows=rows))
    facts, _ = await resolver.resolve(["United States"], max_facts=30)
    assert len(facts) == 1
    assert facts[0].value == "Donald Trump"
    assert "since 2025-01-20" in facts[0].render()
    # A row without the Wave-5 contention columns degrades to uncontested.
    assert facts[0].contested is False
    assert facts[0].surfaced_winner is False
    assert "CONTESTED" not in facts[0].render()
    assert "DISPUTED" not in facts[0].render()


# ---------------------------------------------------------------------------
# CONTESTED annotation (Wave 5, #101) — the grounding preamble TELLS the LLM a
# value is disputed instead of asserting it as plain ground truth. The sidecar
# is joined read-only; the provenance gate is untouched (only seed/curated
# facts are SELECTed — the join merely annotates one of them).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_facts_sql_left_joins_contention_sidecar():
    """The fact query LEFT JOINs the contention sidecar so an eligible fact in a
    live dispute can be annotated — read-only, no provenance-gate change."""
    pool = _StubPool(fetch_rows={"facts": [], "nexuses": []})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    await resolver.resolve(["United States"], max_facts=30)
    facts_sql = next(sql for sql, _ in pool.log if "FROM facts" in sql)
    assert "LEFT JOIN fact_contention fc ON fc.id = f.contention_id" in facts_sql
    # Only a LIVE group (contested/surfaced) annotates; a collapsed group reads
    # as uncontested (we trust the sidecar status over a stale facts marker).
    assert "fc.status IN ('contested','surfaced')" in facts_sql
    # The provenance gate is unchanged — still seed/curated only.
    assert "source_type = ANY($2::text[])" in facts_sql


def test_preamble_includes_current_officeholder_anchor():
    """M13(a): the curated current-officeholder anchor heads every built preamble
    so a grounded assessor never mis-states the SITTING US president as former —
    independent of whether "United States" is a resolved grounding candidate."""
    f = GroundingFact(
        subject="Iran", predicate="head of government", value="Masoud Pezeshkian",
        valid_from=datetime(2024, 7, 28, tzinfo=timezone.utc),
        source_type="seed", confidence=0.9,
    )
    preamble = build_grounding_preamble(
        [f], [], now=datetime(2026, 7, 6, tzinfo=timezone.utc),
    )
    assert preamble is not None
    assert "Donald Trump" in preamble
    assert "do NOT refer to him as a" in preamble
    # The anchor heads the fact list (before the resolved Iran fact).
    assert preamble.index("Donald Trump") < preamble.index("Masoud Pezeshkian")


def test_preamble_still_none_when_no_facts_or_nexuses():
    """M13(a) must not change the empty-candidate contract — no facts AND no
    nexuses still yields NO preamble (the anchor never emits a lone block)."""
    assert build_grounding_preamble([], []) is None


def test_grounding_fact_render_surfaced_winner_is_annotated():
    f = GroundingFact(
        subject="Country X", predicate="capital", value="Alpha",
        valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
        source_type="curated", confidence=0.9,
        contested=True, surfaced_winner=True, contention_value_count=3,
    )
    line = f.render()
    assert "Country X — capital: Alpha (since 2025-01-01)" in line
    assert "CONTESTED: 3 sources disagree; surfaced winner" in line
    # A surfaced winner is NOT marked DISPUTED.
    assert "DISPUTED" not in line


def test_grounding_fact_render_contested_non_winner_is_disputed():
    f = GroundingFact(
        subject="Country X", predicate="capital", value="Beta",
        valid_from=None, source_type="curated", confidence=0.6,
        contested=True, surfaced_winner=False, contention_value_count=2,
    )
    line = f.render()
    # A contested non-winner / abstained group reads DISPUTED — never settled.
    assert "DISPUTED: 2 sources disagree; no surfaced winner" in line
    assert "surfaced winner]" not in line  # not the winner phrasing


def test_grounding_fact_render_uncontested_is_unchanged():
    f = GroundingFact(
        subject="Country X", predicate="capital", value="Alpha",
        valid_from=None, source_type="seed", confidence=0.9,
    )
    line = f.render()
    assert line == "Country X — capital: Alpha"
    assert "CONTESTED" not in line and "DISPUTED" not in line


def test_grounding_fact_render_contested_unknown_count_falls_back():
    f = GroundingFact(
        subject="Country X", predicate="capital", value="Alpha",
        valid_from=None, source_type="seed", confidence=0.9,
        contested=True, surfaced_winner=True, contention_value_count=None,
    )
    assert "CONTESTED: multiple sources disagree; surfaced winner" in f.render()


@pytest.mark.asyncio
async def test_resolver_maps_contention_columns_from_row():
    """A joined row carrying the contention columns maps onto the GroundingFact
    + the preamble surfaces the CONTESTED annotation (the live surfacing path).
    A ``collapsed`` group already folds to contested=False in SQL, so the row
    the resolver sees here is the live (annotate) case."""
    rows = {
        "facts": [
            {
                "subject": "Country X", "predicate": "capital", "value": "Alpha",
                "valid_from": datetime(2025, 1, 1, tzinfo=timezone.utc),
                "source_type": "curated", "confidence": 0.9,
                "contested": True, "surfaced_winner": True,
                "contention_value_count": 3,
            }
        ],
        "nexuses": [],
    }
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(fetch_rows=rows))
    facts, _ = await resolver.resolve(["Country X"], max_facts=30)
    assert len(facts) == 1
    assert facts[0].contested is True
    assert facts[0].surfaced_winner is True
    assert facts[0].contention_value_count == 3
    preamble = build_grounding_preamble(facts, [])
    assert preamble is not None
    assert "CONTESTED: 3 sources disagree; surfaced winner" in preamble


# ---------------------------------------------------------------------------
# Preamble assembly
# ---------------------------------------------------------------------------


def test_preamble_none_when_empty():
    assert build_grounding_preamble([], []) is None


def test_preamble_dated_header_and_lines():
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)
    facts = [
        GroundingFact(
            subject="United States", predicate="head of state", value="Donald Trump",
            valid_from=datetime(2025, 1, 20, tzinfo=timezone.utc),
            source_type="seed", confidence=0.95,
        )
    ]
    nexuses = [
        GroundingNexus(
            subject="United States", rel_type="member of", object="NATO",
            polarity=1, valid_from=datetime(1949, 4, 4, tzinfo=timezone.utc),
        )
    ]
    out = build_grounding_preamble(facts, nexuses, now=now)
    assert out is not None
    assert "AUTHORITATIVE CURRENT CONTEXT (as of 2026-06-18" in out
    assert "treat as ground truth over" in out
    assert "United States — head of state: Donald Trump (since 2025-01-20)" in out
    assert "United States member of NATO [supportive] (since 1949-04-04)" in out


# ---------------------------------------------------------------------------
# Runner injection — on, off (default), and the canary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_off_by_default_no_preamble():
    """No grounding hook on the deps → the user prompt is the plain slice."""
    llm = _CapturingLLM()
    deps = InlineTargetDeps(llm=llm)  # grounding_hook defaults to None
    await run_method([_signal()], {"target_id": "country_g20_us"}, deps)
    assert llm.last_user_prompt is not None
    assert "AUTHORITATIVE CURRENT CONTEXT" not in llm.last_user_prompt


@pytest.mark.asyncio
async def test_runner_prepends_current_leader_canary():
    """CANARY: with grounding wired, a US assessor's LLM context carries
    'Donald Trump' as the CURRENT head of state — the fix for the
    stale-cutoff 'former president' error."""
    llm = _CapturingLLM()

    async def _hook(inputs, options):
        facts = [
            GroundingFact(
                subject="United States", predicate="head of state",
                value="Donald Trump",
                valid_from=datetime(2025, 1, 20, tzinfo=timezone.utc),
                source_type="seed", confidence=0.95,
            )
        ]
        return build_grounding_preamble(facts, [])

    deps = InlineTargetDeps(llm=llm, grounding_hook=_hook)
    await run_method([_signal(geo=["United States"])], {"target_id": "country_g20_us"}, deps)
    prompt = llm.last_user_prompt or ""
    assert "AUTHORITATIVE CURRENT CONTEXT" in prompt
    assert "head of state: Donald Trump (since 2025-01-20)" in prompt
    # The preamble PRECEDES the rendered slice header.
    assert prompt.index("AUTHORITATIVE CURRENT CONTEXT") < prompt.index("Target:")


@pytest.mark.asyncio
async def test_runner_superseded_leader_not_injected():
    """A superseded/expired leader fact is filtered by the resolver's
    current-facts gate, so it never reaches the preamble. Modeled here by a
    resolver over a stub pool that returns NO open rows for the stale leader
    (the gate excludes it) → the hook yields no preamble."""
    llm = _CapturingLLM()
    # Stub pool returns no current rows (the prior, now-expired leader is
    # invisible to the `valid_until IS NULL OR valid_until > now()` gate).
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(fetch_rows={"facts": [], "nexuses": []}))

    async def _hook(inputs, options):
        from legba.runtime.grounding import collect_grounding_candidates
        cands = collect_grounding_candidates(
            inputs, target_id=options.get("target_id"), scope=["target_geo"],
        )
        facts, nexuses = await resolver.resolve(cands, max_facts=30)
        return build_grounding_preamble(facts, nexuses)

    deps = InlineTargetDeps(llm=llm, grounding_hook=_hook)
    await run_method([_signal(geo=["United States"])], {"target_id": "country_g20_us"}, deps)
    prompt = llm.last_user_prompt or ""
    assert "AUTHORITATIVE CURRENT CONTEXT" not in prompt
    assert "Joe Biden" not in prompt


@pytest.mark.asyncio
async def test_runner_grounding_failure_degrades_not_drops():
    """A grounding hook that raises must NOT fail the analyst run — the
    LLM call still happens with the plain (un-grounded) prompt."""
    llm = _CapturingLLM()

    async def _boom(inputs, options):
        raise RuntimeError("substrate down")

    deps = InlineTargetDeps(llm=llm, grounding_hook=_boom)
    res = await run_method([_signal()], {"target_id": "country_g20_us"}, deps)
    assert res.finding is not None
    assert "AUTHORITATIVE CURRENT CONTEXT" not in (llm.last_user_prompt or "")


# ---------------------------------------------------------------------------
# Deps-builder gate — grounding.enabled decides whether a hook is installed
# ---------------------------------------------------------------------------


def _descriptor_with_grounding(enabled: bool | None, sources: list[str] | None = None):
    """Build a minimal valid inline_target descriptor; enabled=None omits the block."""
    from legba.data.schemas.analyst import AnalystDescriptor

    body: dict[str, Any] = {
        "identity": {
            "id": "g_test",
            "name": "G Test",
            "schema_uri": "legba/analyst/1.0.0",
            "version": "0" * 16,
            "kind": "inline_target",
            "type_signature": {
                "input_type": "legba.runtime.SignalList",
                "output_type": "legba.runtime.Finding",
            },
            "state": "active",
            "owner": "t",
        },
        "subscription": {"substrate": {"direct_queries": False}},
        "method": {
            "kind": "llm_planner",
            "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            "llm": {"primary": {"factory_kind": "stack_ref", "raw": "llm.x", "expected_family": "llm_provider"}},
        },
        "cadence": {"fallback_schedule": "0 */6 * * *"},
    }
    if enabled is not None:
        body["grounding"] = {
            "enabled": enabled, "sources": sources or ["substrate"],
        }
    return AnalystDescriptor.model_validate(body, strict=False)


def test_deps_builder_gate_installs_hook_only_when_enabled():
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    pool = _StubPool()
    # Enabled → a hook.
    assert _build_grounding_hook(_descriptor_with_grounding(True), pg_pool=pool) is not None
    # Disabled → None.
    assert _build_grounding_hook(_descriptor_with_grounding(False), pg_pool=pool) is None
    # Absent block → None.
    assert _build_grounding_hook(_descriptor_with_grounding(None), pg_pool=pool) is None
    # No pool → None even when enabled (degrade).
    assert _build_grounding_hook(_descriptor_with_grounding(True), pg_pool=None) is None


# ---------------------------------------------------------------------------
# L-114 — the embedder threads through the resolver (S5-T1)
# ---------------------------------------------------------------------------


def test_resolver_carries_optional_embedder():
    """SubstrateGroundingResolver accepts + stores an embedder; defaults None."""
    pool = _StubPool()
    assert SubstrateGroundingResolver(pg_pool=pool)._embedder is None
    sentinel = object()
    assert (
        SubstrateGroundingResolver(pg_pool=pool, embedder=sentinel)._embedder
        is sentinel
    )


def test_deps_builder_threads_embedder_into_resolver():
    """`_build_grounding_hook(embedder=…)` reaches the closed-over resolver.

    The hook closes over the resolver; the embedder must arrive on it so the
    Tier-2 vector:world_context follow-up has it in hand (L-114 threading)."""
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    pool = _StubPool()
    sentinel = object()
    hook = _build_grounding_hook(
        _descriptor_with_grounding(True), pg_pool=pool, embedder=sentinel,
    )
    assert hook is not None
    # Pull the resolver out of the hook's closure and assert it carries it.
    freevars = dict(zip(hook.__code__.co_freevars, hook.__closure__ or ()))
    resolver = freevars["resolver"].cell_contents
    assert isinstance(resolver, SubstrateGroundingResolver)
    assert resolver._embedder is sentinel

    # Default (no embedder threaded) → the resolver carries None, unchanged.
    hook_none = _build_grounding_hook(_descriptor_with_grounding(True), pg_pool=pool)
    freevars_none = dict(zip(hook_none.__code__.co_freevars, hook_none.__closure__ or ()))
    assert freevars_none["resolver"].cell_contents._embedder is None


@pytest.mark.asyncio
async def test_deps_builder_hook_runs_resolver_and_builds_preamble():
    """End-to-end (stub pool): the installed hook extracts candidates, runs the
    resolver, and returns the dated preamble carrying the current leader."""
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    rows = {
        "facts": [
            {
                "subject": "United States", "predicate": "head of state",
                "value": "Donald Trump",
                "valid_from": datetime(2025, 1, 20, tzinfo=timezone.utc),
                "source_type": "seed", "confidence": 0.95,
            }
        ],
        "nexuses": [],
    }
    pool = _StubPool(fetch_rows=rows)
    hook = _build_grounding_hook(_descriptor_with_grounding(True), pg_pool=pool)
    assert hook is not None
    preamble = await hook([_signal(geo=["United States"])], {"target_id": "country_g20_us"})
    assert preamble is not None
    assert "head of state: Donald Trump (since 2025-01-20)" in preamble


@pytest.mark.asyncio
async def test_deps_builder_hook_appends_assessed_situations_block():
    """With sources=[substrate, situations], the hook emits the ground-truth
    block AND a SEPARATE 'ASSESSED SITUATIONS' block — the situation appears
    after, and is never laundered into, the ground-truth header."""
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    rows = {
        "facts": [
            {
                "subject": "Iran", "predicate": "head of state",
                "value": "Mojtaba Khamenei",
                "valid_from": datetime(2026, 3, 8, tzinfo=timezone.utc),
                "source_type": "seed", "confidence": 0.95,
            }
        ],
        "nexuses": [],
        "situations": [
            {
                "name": "US–Iran War", "category": "country_g20_ir",
                "status": "active", "intensity_score": 1.6,
                "valid_from": datetime(2026, 2, 28, tzinfo=timezone.utc),
                "last_event_at": None,
            }
        ],
    }
    pool = _StubPool(fetch_rows=rows)
    desc = _descriptor_with_grounding(True, sources=["substrate", "situations"])
    hook = _build_grounding_hook(desc, pg_pool=pool)
    assert hook is not None
    out = await hook([_signal(geo=["Iran"])], {"target_id": "country_g20_ir"})
    assert out is not None
    assert "AUTHORITATIVE CURRENT CONTEXT" in out      # ground-truth block
    assert "head of state: Mojtaba Khamenei" in out
    assert "ASSESSED SITUATIONS" in out                # separate frames block
    assert "US–Iran War" in out
    # The situations block is rendered AFTER the ground-truth block, separately.
    assert out.index("AUTHORITATIVE CURRENT CONTEXT") < out.index("ASSESSED SITUATIONS")


# ---------------------------------------------------------------------------
# D4 contamination — per-country scoping of graph-centrality + situations
# (regression: a country run NARROWS to its target; a META run keeps GLOBAL).
# ---------------------------------------------------------------------------


class _GraphMetricsStubConn:
    """Serves a single canned graph_metrics fetch for resolve_graph_structure."""

    def __init__(self, payload_rows: list[dict[str, Any]]) -> None:
        self._rows = payload_rows

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        if "FROM graph_metrics" in sql:
            return self._rows
        return []


class _GraphMetricsStubPool:
    def __init__(self, payload_rows: list[dict[str, Any]]) -> None:
        self._conn = _GraphMetricsStubConn(payload_rows)

    def acquire(self) -> _StubAcquire:
        return _StubAcquire(self._conn)


def _graph_rows_with_interesting() -> list[dict[str, Any]]:
    # graph_mining carries an interesting shortlist where the US (globally most
    # central) outscores Indonesia. The per-country run for Indonesia must NOT
    # inherit the US item; the global (meta) run must keep it.
    return [
        {
            "metric_kind": "graph_mining",
            "payload": {
                "interesting": [
                    {"kind": "broker", "label": "United States", "score": 0.99,
                     "rationale": "globally most central", "entities": ["United States"]},
                    {"kind": "tense_actor", "label": "Indonesia", "score": 0.40,
                     "rationale": "regional tension", "entities": ["Indonesia"]},
                ]
            },
        },
        {"metric_kind": "structural_balance", "payload": {}},
    ]


def test_top_graph_items_target_scoped_drops_global_tail():
    # Per-country (target_scoped=True): only the candidate (iran) survives — the
    # globally-higher United States tail is DROPPED, not used to top up.
    scoped = _top_graph_items(
        {"Iran": 22, "United States": 99}, {"iran"}, 6, min_value=0.0, target_scoped=True,
    )
    assert [n for n, _ in scoped] == ["Iran"]
    # Meta (target_scoped=False, the default): global tail is kept (US present).
    glob = _top_graph_items(
        {"Iran": 22, "United States": 99}, {"iran"}, 6, min_value=0.0,
    )
    assert "United States" in [n for n, _ in glob]


def test_top_graph_items_scoped_empty_candidates_degrades_to_global():
    # A scoped run with NO candidates must not blank the block — it degrades to
    # the global view rather than returning nothing.
    out = _top_graph_items(
        {"United States": 99}, set(), 6, min_value=0.0, target_scoped=True,
    )
    assert [n for n, _ in out] == ["United States"]


def test_collect_interesting_target_scoped_drops_out_of_scope():
    payloads = {
        "graph_mining": {
            "interesting": [
                {"kind": "broker", "label": "United States", "score": 0.99,
                 "rationale": "global", "entities": ["United States"]},
                {"kind": "tense_actor", "label": "Indonesia", "score": 0.4,
                 "rationale": "scoped", "entities": ["Indonesia"]},
            ]
        }
    }
    scoped = _collect_interesting(payloads, {"indonesia"}, 12, target_scoped=True)
    assert [i.label for i in scoped] == ["Indonesia"]   # US dropped
    glob = _collect_interesting(payloads, {"indonesia"}, 12)  # default = global
    assert {"United States", "Indonesia"} == {i.label for i in glob}


def test_top_proxy_chains_target_scoped_drops_non_candidate_chains():
    chains = [
        {"subject": "United States", "via": "X", "object": "China", "sign": -1},
        {"subject": "Indonesia", "via": "Y", "object": "Malaysia", "sign": 1},
    ]
    scoped = _top_proxy_chains(chains, {"indonesia"}, 6, target_scoped=True)
    assert scoped == ["Indonesia → Y → Malaysia [aligned path]"]
    glob = _top_proxy_chains(chains, {"indonesia"}, 6)
    assert len(glob) == 2


@pytest.mark.asyncio
async def test_resolve_graph_structure_country_scopes_meta_keeps_global():
    """THE mandate regression. resolve_graph_structure:
      * a META / no-target run KEEPS the GLOBAL structure (the US item present);
      * a PER-COUNTRY run (Indonesia) NARROWS to its candidate — no US leak.
    Same candidate name set + same metrics; only the scope_target_id differs.
    """
    pool = _GraphMetricsStubPool(_graph_rows_with_interesting())
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    candidates = ["Indonesia"]

    # META (world_assessor): no target → global structure block, US KEPT.
    meta = await resolver.resolve_graph_structure(
        candidates, limit=8, scope_target_id=None,
    )
    assert meta is not None
    meta_labels = {i.label for i in meta.interesting}
    assert "United States" in meta_labels, "meta run must keep the global structure"
    assert "Indonesia" in meta_labels

    # PER-COUNTRY (Indonesia): scope_target_id is a country id → US DROPPED.
    country = await resolver.resolve_graph_structure(
        candidates, limit=8, scope_target_id="country_g20_id",
    )
    assert country is not None
    country_labels = {i.label for i in country.interesting}
    assert "United States" not in country_labels, "country run must NOT inherit the US-central item"
    assert country_labels == {"Indonesia"}


@pytest.mark.asyncio
async def test_resolve_graph_structure_self_scopes_from_resolver_target():
    """When the resolver is CONSTRUCTED with a country target_id, it self-scopes
    even if the caller passes no explicit scope arg (live wiring path)."""
    pool = _GraphMetricsStubPool(_graph_rows_with_interesting())
    resolver = SubstrateGroundingResolver(pg_pool=pool, target_id="country_g20_id")
    out = await resolver.resolve_graph_structure(["Indonesia"], limit=8)
    assert out is not None
    assert {i.label for i in out.interesting} == {"Indonesia"}


def test_is_per_country_target_only_country_ids():
    from legba.runtime.grounding import is_per_country_target
    assert is_per_country_target("country_g20_id") is True
    assert is_per_country_target(None) is False          # meta / world_assessor
    assert is_per_country_target("situation_iran_war") is False  # thematic


# ---------------------------------------------------------------------------
# D4 off-target guard — a per-country finding naming ONLY other countries
# ---------------------------------------------------------------------------


def test_finding_off_target_flags_us_only_indonesia_run():
    from legba.runtime.grounding import finding_is_off_target
    # The literal D4 shape: an Indonesia run whose finding is all about the US.
    assert finding_is_off_target(
        target_id="country_g20_id",
        text="The United States escalated sanctions against China and Russia.",
        key_entities=["United States", "China"],
        geo=["United States"],
    ) is True


def test_finding_on_target_when_it_names_its_own_country():
    from legba.runtime.grounding import finding_is_off_target
    # Mentions Indonesia (slug token 'id' won't match, but the geo tag does).
    assert finding_is_off_target(
        target_id="country_g20_id",
        text="Indonesia's central bank held rates as the US dollar strengthened.",
        key_entities=["Indonesia", "United States"],
        geo=["Indonesia"],
    ) is False


def test_finding_off_target_false_for_meta_run():
    from legba.runtime.grounding import finding_is_off_target
    # A meta / no-target run is NEVER gated — the world assessor talks about
    # every country by design.
    assert finding_is_off_target(
        target_id=None,
        text="The United States and China escalated tariffs.",
        key_entities=["United States", "China"],
    ) is False


def test_finding_off_target_false_when_no_country_named():
    from legba.runtime.grounding import finding_is_off_target
    # Names no country at all (generic/thin finding) → NOT suppressed; we only
    # gate a finding demonstrably about OTHER countries.
    assert finding_is_off_target(
        target_id="country_g20_id",
        text="Heavy rainfall triggered local flooding and transport delays.",
        key_entities=["flooding"],
    ) is False


def test_target_scope_names_lifts_slug_token():
    from legba.runtime.grounding import target_scope_names
    assert "us" in target_scope_names("country_g20_us")
    assert target_scope_names(None) == set()


def test_pk_watch_desk_slug_maps_to_pakistan_name():
    # S1-T2: the `pk` gazetteer entry is what lifts the pk desk out of the
    # fail-open blind spot — its scope names must include the country NAME, not
    # just the bare ISO slug, so the guard can tell Pakistan from India.
    from legba.runtime.grounding import target_scope_names
    names = target_scope_names("country_watch_pk")
    assert "pk" in names and "pakistan" in names


def test_finding_off_target_flags_india_only_pakistan_watch_run():
    # The precision the pk mapping buys: a pk desk whose finding is entirely
    # about India (an OTHER country) → OFF-target. Without the `pk`→'pakistan'
    # entry the guard would fail OPEN here (own == {'pk'} ⊆ slug tokens) and
    # publish an India report as a Pakistan product.
    from legba.runtime.grounding import finding_is_off_target
    assert finding_is_off_target(
        target_id="country_watch_pk",
        text="India test-fired a new missile amid rising tensions with China.",
        key_entities=["India", "China"],
        geo=["India"],
    ) is True


def test_finding_on_target_when_pakistan_watch_run_names_pakistan():
    # Mentions its OWN country (Pakistan) → on-target even though it also names
    # India, because naming the target geo anywhere clears the guard.
    from legba.runtime.grounding import finding_is_off_target
    assert finding_is_off_target(
        target_id="country_watch_pk",
        text="Pakistan's military responded to cross-border clashes with India.",
        key_entities=["Pakistan", "India"],
        geo=["Pakistan"],
    ) is False


# ---------------------------------------------------------------------------
# D4 off-target guard — END-TO-END through inline_target.run_method:
# a per-country run whose finding is all about OTHER countries → force_trace_only.
# ---------------------------------------------------------------------------


class _FixedFindingLLM:
    """LLM double returning a caller-supplied finding JSON verbatim."""

    subprovider = "openai"

    def __init__(self, finding: dict[str, Any]) -> None:
        self._finding = finding

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kwargs) -> Any:
        return _Response(content=json.dumps(self._finding), usage=_Usage())


@pytest.mark.asyncio
async def test_run_method_off_target_country_finding_forces_trace_only():
    """An Indonesia run whose finding names ONLY the US/China → TRACE_ONLY."""
    llm = _FixedFindingLLM({
        "title": "US escalates sanctions on China",
        "body": "The United States imposed new sanctions on China and Russia.",
        "confidence": 0.7, "evidence": [], "tags": ["United States", "China"],
    })
    deps = InlineTargetDeps(llm=llm)
    result = await run_method(
        [_signal(geo=["United States"])], {"target_id": "country_g20_id"}, deps,
    )
    assert result.force_trace_only is True
    assert any(s.get("kind") == "off_target_guard" for s in result.intermediate_steps)


@pytest.mark.asyncio
async def test_run_method_on_target_country_finding_publishes():
    """An Indonesia run whose finding actually names Indonesia → published."""
    llm = _FixedFindingLLM({
        "title": "Indonesia central bank holds rates",
        "body": "Indonesia kept its policy rate steady amid a stronger US dollar.",
        "confidence": 0.7, "evidence": [], "tags": ["Indonesia"],
    })
    deps = InlineTargetDeps(llm=llm)
    result = await run_method(
        [_signal(geo=["Indonesia"])], {"target_id": "country_g20_id"}, deps,
    )
    assert result.force_trace_only is False


@pytest.mark.asyncio
async def test_run_method_meta_run_never_off_target_gated():
    """A META / no-target run (world_assessor) is never gated even when its
    finding is entirely about other countries."""
    llm = _FixedFindingLLM({
        "title": "US-China trade war intensifies",
        "body": "The United States and China escalated tariffs.",
        "confidence": 0.7, "evidence": [], "tags": ["United States", "China"],
    })
    deps = InlineTargetDeps(llm=llm)
    result = await run_method([_signal()], {"target_id": None}, deps)
    assert result.force_trace_only is False
