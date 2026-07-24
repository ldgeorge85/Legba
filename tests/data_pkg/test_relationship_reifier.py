# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PIECE A — relationship_reifier producer + the dormant refinement consumers.

Three layers, all without a live model (the typing LLM is a canned stub):

  1. The reifier with a STUBBED LLM types co-mentioned pairs and side-writes a
     real signed typed nexus row (over a real test pg_pool).
  2. The reifier degrades-not-drops: a raising LLM yields a clean summary with
     0 written + degraded>0, never an exception.
  3. The dormant consumers (structural_balance + graph_mining + nexus_decay)
     run over seeded nexuses WITHOUT error and reflect the signed edges.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import (
    graph_mining,
    nexus_decay,
    structural_balance,
)
from legba.data.analysts.relationship_reifier import (
    ReifierDeps,
    _coerce_typing,
    _extract_json_object,
    run_method,
)
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext, NexusPayload, write_nexus


# ---------------------------------------------------------------------------
# Stub LLM (canned typing JSON) — NO live model in unit tests.
# ---------------------------------------------------------------------------


class _CannedTyperLLM:
    """Returns ``obj`` verbatim for EVERY call by default — fine when the test
    seeds exactly one candidate pair (the historical shape).

    ``run_method``'s candidate reader (``_read_candidates``) pulls from the
    LIVE, UNSCOPED ``proposed_edges`` table (shared session DB across the
    whole suite), so if any OTHER test file leaves an open (un-nexused)
    ``co_occurs`` edge at/above ``MIN_EDGE_CONFIDENCE`` when this test runs,
    that unrelated pair can also land in the SAME run's candidate window
    (``deps.max_candidates``). A pair-agnostic stub would then answer that
    unrelated pair with THIS object too, writing a second/third nexus row
    that overwrites the intended (subject, object) — caught live 2026-07-23
    while verifying TEST_DEBT_RECON.md Bucket H under the full suite.
    ``only_for=(source, target)`` scopes the canned response to ONLY the
    prompt naming that exact "Entity A"/"Entity B" pair (matches
    ``_build_user_prompt``'s first two lines); any other pair gets a neutral
    ``{"related": False}`` (a no-op per ``_coerce_typing``), so an unrelated
    candidate sharing the run never gets mistyped into this test's nexus.
    """

    subprovider = "stub"

    def __init__(
        self,
        obj: dict[str, Any],
        *,
        pt: int = 11,
        ct: int = 7,
        only_for: tuple[str, str] | None = None,
    ) -> None:
        self._obj = obj
        self._pt = pt
        self._ct = ct
        self._only_for = only_for
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"system": system, "max_tokens": max_tokens})
        pt, ct = self._pt, self._ct
        if self._only_for is not None:
            source, target = self._only_for
            prompt = str(messages[0].get("content") or "") if messages else ""
            if (
                f"Entity A: {source}" not in prompt
                or f"Entity B: {target}" not in prompt
            ):
                obj: dict[str, Any] = {"related": False}
            else:
                obj = self._obj
        else:
            obj = self._obj

        class _Usage:
            prompt_tokens = pt
            completion_tokens = ct
            reasoning_tokens = 0

        class _Response:
            content = json.dumps(obj)
            usage = _Usage()

        return _Response()


class _RaisingLLM:
    subprovider = "raising"

    async def chat_complete(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("synthetic typing failure")


# ---------------------------------------------------------------------------
# Pure-unit: parse + coerce (no DB)
# ---------------------------------------------------------------------------


def test_extract_json_object_handles_fences():
    raw = "```json\n{\"related\": true, \"rel_type\": \"HostileTo\"}\n```"
    obj = _extract_json_object(raw)
    assert obj is not None and obj["rel_type"] == "HostileTo"
    assert _extract_json_object("no json here") is None


def test_coerce_typing_canonical_polarity_and_skip():
    # related=false → skip.
    assert _coerce_typing(
        {"related": False}, fallback_subject="A", fallback_object="B"
    ) is None
    # off-list rel_type → skip (consumers can't sign it).
    assert _coerce_typing(
        {"related": True, "rel_type": "FriendsWithBenefits", "subject": "A",
         "object": "B"},
        fallback_subject="A", fallback_object="B",
    ) is None
    # HostileTo → canonical -1 regardless of the LLM-claimed polarity (the
    # POLARITY table is authoritative when non-zero).
    p = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "Iran",
         "object": "Israel", "polarity": 1},
        fallback_subject="Iran", fallback_object="Israel",
    )
    assert isinstance(p, NexusPayload)
    assert p.polarity == -1


def test_coerce_typing_intermediary_select_or_null():
    # An intermediary that IS in the offered set survives → channel proxy (#99).
    p = _coerce_typing(
        {"related": True, "rel_type": "SuppliesWeaponsTo", "subject": "Iran",
         "object": "Israel", "intermediary": "Hezbollah", "channel": "proxy"},
        fallback_subject="Iran", fallback_object="Israel",
        allowed_intermediaries=["Hezbollah", "Houthis"],
    )
    assert isinstance(p, NexusPayload)
    assert p.intermediary == "Hezbollah"
    assert p.channel == "proxy"
    # A hallucinated intermediary NOT in the offered set is dropped to null and
    # the relationship stays direct — the typer may never free-text a proxy.
    p2 = _coerce_typing(
        {"related": True, "rel_type": "SuppliesWeaponsTo", "subject": "Iran",
         "object": "Israel", "intermediary": "Wagner Group", "channel": "proxy"},
        fallback_subject="Iran", fallback_object="Israel",
        allowed_intermediaries=["Hezbollah"],
    )
    assert isinstance(p2, NexusPayload)
    assert p2.intermediary is None
    # No candidate set offered at all → any returned intermediary is dropped.
    p3 = _coerce_typing(
        {"related": True, "rel_type": "SuppliesWeaponsTo", "subject": "Iran",
         "object": "Israel", "intermediary": "Hezbollah"},
        fallback_subject="Iran", fallback_object="Israel",
    )
    assert isinstance(p3, NexusPayload)
    assert p3.intermediary is None
    # An offered intermediary that collides with an endpoint is also dropped.
    p4 = _coerce_typing(
        {"related": True, "rel_type": "SuppliesWeaponsTo", "subject": "Iran",
         "object": "Israel", "intermediary": "Iran"},
        fallback_subject="Iran", fallback_object="Israel",
        allowed_intermediaries=["Iran"],
    )
    assert isinstance(p4, NexusPayload)
    assert p4.intermediary is None


def test_build_user_prompt_offers_intermediaries():
    from legba.data.analysts.relationship_reifier import _build_user_prompt

    prompt = _build_user_prompt(
        source="Iran", target="Israel", evidence_text="co-mention", facts=[],
        candidate_intermediaries=["Hezbollah", "Houthis"],
    )
    assert "Candidate intermediaries" in prompt
    assert "Hezbollah" in prompt and "Houthis" in prompt
    # No candidates → no offer block (keeps direct-typing behavior unchanged).
    bare = _build_user_prompt(
        source="A", target="B", evidence_text="x", facts=[],
    )
    assert "Candidate intermediaries" not in bare


# ---------------------------------------------------------------------------
# Fixtures (DB)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _seed_proposed_edge(pool, *, src: str, tgt: str, conf: float = 0.6):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO proposed_edges
                (source_entity, target_entity, relationship_type, confidence,
                 evidence_text, status)
            VALUES ($1, $2, 'co_occurs', $3, $4, 'pending')
            ON CONFLICT (lower(source_entity), lower(target_entity),
                         relationship_type)
            DO UPDATE SET confidence = EXCLUDED.confidence
            """,
            src, tgt, conf, f"{src} and {tgt} appear together",
        )


# ---------------------------------------------------------------------------
# The reifier types + writes a nexus (stubbed LLM, real pg_pool)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reifier_types_and_writes_nexus(pg_pool):
    await _seed_proposed_edge(pg_pool, src="IranUNIQ", tgt="IsraelUNIQ", conf=0.7)
    # _coerce_typing's SELECT-or-null intermediary hardening (#99 proxy-chain,
    # see test_coerce_typing_intermediary_select_or_null) only keeps an
    # LLM-returned intermediary if it was OFFERED as a candidate by
    # _intermediary_candidates_for — which requires a co_occurs edge between
    # the intermediary and BOTH endpoints. Seed those so "HamasUNIQ" is a real
    # candidate, not silently dropped to null (TEST_DEBT_RECON.md Bucket H).
    # Confidence is DELIBERATELY BELOW MIN_EDGE_CONFIDENCE (0.45 in
    # relationship_reifier.py) so _read_candidates() does NOT also surface
    # these two edges as their OWN top-level candidate pairs — if it did, the
    # canned stub LLM (which always answers subject=IranUNIQ/object=IsraelUNIQ
    # verbatim, regardless of which pair is actually being typed) would write
    # a SECOND/THIRD nexus row targeting the same (IranUNIQ, IsraelUNIQ)
    # subject/object, superseding the first write with intermediary=None
    # (HamasUNIQ is not a valid self-intermediary for its own pair).
    # _intermediary_candidates_for has no confidence filter, so this edge is
    # still found there.
    await _seed_proposed_edge(pg_pool, src="IranUNIQ", tgt="HamasUNIQ", conf=0.3)
    await _seed_proposed_edge(pg_pool, src="IsraelUNIQ", tgt="HamasUNIQ", conf=0.3)
    # only_for scopes the canned response to the IranUNIQ/IsraelUNIQ pair only
    # (see _CannedTyperLLM docstring) — _read_candidates() reads the LIVE
    # shared-session proposed_edges table unscoped, so an unrelated open
    # co_occurs edge left by another test file could otherwise ride along in
    # this run's candidate window and get mistyped into this test's nexus.
    llm = _CannedTyperLLM(
        {
            "related": True,
            "subject": "IranUNIQ",
            "object": "IsraelUNIQ",
            "intermediary": "HamasUNIQ",
            "rel_type": "SuppliesWeaponsTo",
            "polarity": -1,
            "intent": "hostile",
            "channel": "proxy",
            "confidence": 0.8,
        },
        only_for=("IranUNIQ", "IsraelUNIQ"),
    )
    deps = ReifierDeps(llm=llm, pg_pool=pg_pool, max_candidates=10)

    result = await run_method(
        inputs=[],
        options={"analyst_id": "relationship_reifier", "run_id": str(uuid4())},
        deps=deps,
    )
    data = result.finding.data
    assert data["written"] >= 1, data
    assert data["typed"] >= 1
    # The typing call DID happen (usage rolled up).
    assert result.usage["prompt_tokens"] > 0
    assert llm.calls, "the stub LLM must have been called"

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT subject, intermediary, object, rel_type, polarity, channel, "
            "valid_from FROM nexuses "
            "WHERE lower(subject)='iranuniq' AND lower(object)='israeluniq' "
            "AND valid_until IS NULL AND superseded_by IS NULL"
        )
    assert row is not None, "a signed typed nexus row must be written"
    # Phase B item 5: rel_type is stored in the canonical lowercase-spaced form.
    # The polarity sign is resolved upstream from the CamelCase rel_type
    # (POLARITY map) BEFORE the write, so the sign is unaffected.
    assert row["rel_type"] == "supplies weapons to"
    assert row["polarity"] == -1, "SuppliesWeaponsTo is canonically -1"
    assert row["intermediary"] == "HamasUNIQ"
    assert row["channel"] == "proxy"
    assert row["valid_from"] is not None, "valid_from stamped at event time"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reifier_degrades_not_drops_on_llm_failure(pg_pool):
    await _seed_proposed_edge(pg_pool, src="DegradeA", tgt="DegradeB", conf=0.7)
    deps = ReifierDeps(llm=_RaisingLLM(), pg_pool=pg_pool, max_candidates=10)
    result = await run_method(
        inputs=[],
        options={"analyst_id": "relationship_reifier", "run_id": str(uuid4())},
        deps=deps,
    )
    data = result.finding.data
    assert data["written"] == 0
    assert data["degraded"] >= 1, "a raising LLM must degrade, not raise"


# ---------------------------------------------------------------------------
# The dormant refinement consumers run over seeded nexuses without error
# ---------------------------------------------------------------------------


def _ctx() -> AnalystContext:
    return AnalystContext(
        analyst_id="seed.reifier",
        analyst_version="v1",
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )


async def _seed_nexus(pool, *, subject, object_, polarity, rel_type,
                      intermediary=None, created_days_ago=0):
    async with pool.acquire() as conn:
        out, _ = await write_nexus(
            conn,
            analyst_ctx=_ctx(),
            payload=NexusPayload(
                subject=subject, object=object_, intermediary=intermediary,
                rel_type=rel_type, polarity=polarity,
                label=f"{subject} {rel_type} {object_}",
            ),
            derived_from=[],
        )
        if created_days_ago:
            await conn.execute(
                "UPDATE nexuses SET created_at = now() - ($2 || ' days')::interval "
                "WHERE id = $1",
                out.id, str(created_days_ago),
            )
        return out.id


class _PoolDeps:
    def __init__(self, pool):
        self.pg_pool = pool


@pytest.mark.integration
@pytest.mark.asyncio
async def test_structural_balance_runs_over_seeded_nexuses(pg_pool):
    # An UNBALANCED triad among uniquely-named entities: A-B +, B-C +, A-C -.
    await _seed_nexus(pg_pool, subject="SbA", object_="SbB", polarity=1, rel_type="AlliedWith")
    await _seed_nexus(pg_pool, subject="SbB", object_="SbC", polarity=1, rel_type="AlliedWith")
    await _seed_nexus(pg_pool, subject="SbA", object_="SbC", polarity=-1, rel_type="HostileTo")

    result = await structural_balance.handle(
        inputs=[],
        options={"augment_from_age": False},
        deps=_PoolDeps(pg_pool),
    )
    data = result.finding.data
    # The seeded signed triad must register as a signed triad (balanced or
    # unbalanced) — i.e. the consumer actually saw the signed nexus edges.
    assert (data["balanced_count"] + data["unbalanced_count"]) >= 1, data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_mining_runs_over_seeded_nexuses(pg_pool):
    # A proxy chain: actor -> via -> target (no direct actor->target edge).
    await _seed_nexus(
        pg_pool, subject="GmActor", object_="GmTarget", intermediary="GmVia",
        polarity=-1, rel_type="SuppliesWeaponsTo",
    )
    result = await graph_mining.handle(
        inputs=[],
        options={"augment_from_age": False},
        deps=_PoolDeps(pg_pool),
    )
    # No exception is the bar; the run must also report a node count > 0,
    # proving it ingested the reified edges.
    assert result.finding.data["node_count"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nexus_decay_runs_clean_over_seeded_nexuses(pg_pool):
    # A stale OPEN nexus (created 40 days ago, confidence 1.0) must decay.
    nid = await _seed_nexus(
        pg_pool, subject="DecayA", object_="DecayB", polarity=-1,
        rel_type="HostileTo", created_days_ago=40,
    )
    result = await nexus_decay.handle(
        inputs=[], options={}, deps=_PoolDeps(pg_pool),
    )
    assert "decayed_count" in result.finding.data
    async with pg_pool.acquire() as conn:
        conf = await conn.fetchval("SELECT confidence FROM nexuses WHERE id=$1", nid)
    assert conf < 1.0, "a 40-day-old open nexus must have been confidence-decayed"
