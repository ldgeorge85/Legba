# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E1 — canonicalize-AT-WRITE: the shared keeper-election helper + its wiring.

:func:`legba.data._entity_resolve.resolve_keeper` runs the SAME any-class +
merged_aliases keeper election the ingestion resolver uses
(``entity_resolution.py`` lines 561-609), so a nexus/fact ENDPOINT string
converges onto its elected ``entity_profiles`` keeper's canonical_name BEFORE the
write instead of forking a distinct graph actor.

Covered here:

  * exact ``lower(canonical_name)`` match → keeper's canonical_name;
  * ``merged_aliases`` containment (the SNSC / Resistance fragments) → keeper;
  * article/case/whitespace variants → keeper (the M4 lookup_key normalization);
  * NO match → the input unchanged;
  * a ``gc_status`` merged/junk loser is EXCLUDED (never elected);
  * ANY error (a raising conn) → the input unchanged, no raise (degrade-not-break);
  * class-awareness: a class-less endpoint still elects an 'organization' keeper;
  * N4 — two surfaces that fold onto ONE keeper collapse to a self-loop and are
    DROPPED by the producers (reifier + proposed_edge_governance).
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from legba.data._entity_resolve import resolve_keeper
from legba.data.config import PostgresConfig


# ---------------------------------------------------------------------------
# Fixtures — a fresh migrated test DB + a small seeded keeper set
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _seed_keeper(
    conn: Any,
    *,
    name: str,
    entity_class: str = "organization",
    aliases: list[str] | None = None,
    gc_status: str | None = None,
) -> None:
    import json

    data: dict[str, Any] = {}
    if aliases:
        data["merged_aliases"] = aliases
    if gc_status:
        data["gc_status"] = gc_status
    # The session-scoped test DB is shared across the whole suite, so seeds must
    # be idempotent — a keeper already present (from another test) is fine; we
    # assert on the canonical_name, never on a row count. Refresh the aliases so
    # the containment probe finds them regardless of insertion order.
    await conn.execute(
        """
        INSERT INTO entity_profiles
            (id, canonical_name, entity_class, entity_type, data)
        VALUES (gen_random_uuid(), $1, $2, $2, $3::jsonb)
        ON CONFLICT (lower(canonical_name), entity_class) DO UPDATE
            SET data = EXCLUDED.data
        """,
        name, entity_class, json.dumps(data),
    )


@pytest_asyncio.fixture
async def seeded_pool(pg_pool):
    """Seed a keeper set that mirrors the live damage E1 addresses."""
    async with pg_pool.acquire() as conn:
        await _seed_keeper(
            conn,
            name="Supreme National Security Council",
            aliases=["SNSC"],
        )
        await _seed_keeper(
            conn,
            name="Axis of Resistance",
            aliases=["Resistance"],
        )
        await _seed_keeper(conn, name="Ali Khamenei", entity_class="person",
                           aliases=["Ayatollah Ali Khamenei", "Khamenei"])
        # A de-fragmentation LOSER — must NEVER be elected.
        await _seed_keeper(
            conn,
            name="Dead Fragment Node",
            aliases=["ZZLoser"],
            gc_status="merged",
        )
    return pg_pool


# ---------------------------------------------------------------------------
# resolve_keeper — DB-backed election
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exact_name_elects_keeper(seeded_pool):
    async with seeded_pool.acquire() as conn:
        # Case/whitespace-insensitive exact match resolves to the stored surface.
        assert (
            await resolve_keeper(conn, "supreme national security council")
            == "Supreme National Security Council"
        )
        assert (
            await resolve_keeper(conn, "  Axis  of  Resistance ")
            == "Axis of Resistance"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_alias_containment_elects_keeper(seeded_pool):
    async with seeded_pool.acquire() as conn:
        # The SNSC fragment (a person/org fragment in the live damage) → keeper.
        assert (
            await resolve_keeper(conn, "SNSC")
            == "Supreme National Security Council"
        )
        # The "Resistance" fragment → the Axis of Resistance keeper.
        assert await resolve_keeper(conn, "Resistance") == "Axis of Resistance"
        # A Khamenei surface variant folds onto the one person keeper.
        assert await resolve_keeper(conn, "Khamenei") == "Ali Khamenei"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_article_case_ws_variant_elects_keeper(seeded_pool):
    async with seeded_pool.acquire() as conn:
        # A leading article + case + whitespace variant of an alias still folds
        # (the M4 lookup_key normalization mirrored on both sides).
        assert (
            await resolve_keeper(conn, "the  SNSC")
            == "Supreme National Security Council"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_match_returns_input_unchanged(seeded_pool):
    async with seeded_pool.acquire() as conn:
        assert (
            await resolve_keeper(conn, "Nonexistent Blorp Entity")
            == "Nonexistent Blorp Entity"
        )
        # Empty input degrades to itself (never a spurious keeper).
        assert await resolve_keeper(conn, "") == ""
        assert await resolve_keeper(conn, "   ") == "   "


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gc_merged_loser_never_elected(seeded_pool):
    async with seeded_pool.acquire() as conn:
        # 'ZZLoser' is ONLY an alias on a gc_status='merged' row — excluded from
        # both probes, so it returns unchanged (never re-attaches a dead node).
        assert await resolve_keeper(conn, "ZZLoser") == "ZZLoser"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_class_aware_but_falls_back_to_any_class(seeded_pool):
    async with seeded_pool.acquire() as conn:
        # The SNSC endpoint carries no class ('entity'); the keeper is an
        # 'organization'. The any-class election still finds it.
        assert (
            await resolve_keeper(conn, "SNSC", entity_class="entity")
            == "Supreme National Security Council"
        )
        # An explicit (matching) class is also fine.
        assert (
            await resolve_keeper(conn, "SNSC", entity_class="organization")
            == "Supreme National Security Council"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fallback_incompatible_class_keeps_distinct(seeded_pool):
    """E1 adversarial #1 — a FALLBACK (alias/normalized) match must NOT fold when
    the endpoint declares a specific class incompatible with the keeper's (the
    'the Atlantic' magazine vs 'Atlantic' ocean class). A class-less endpoint and
    an EXACT-surface match still fold."""
    async with seeded_pool.acquire() as conn:
        # 'SNSC' is an alias on an 'organization' keeper → a FALLBACK match. A
        # 'person'-typed endpoint is incompatible → the fold is refused (distinct).
        assert await resolve_keeper(conn, "SNSC", entity_class="person") == "SNSC"
        # organization / corporation are an explicitly-equivalent pair → still folds.
        assert (
            await resolve_keeper(conn, "SNSC", entity_class="corporation")
            == "Supreme National Security Council"
        )
        # A class-LESS endpoint can never be declared incompatible → still folds
        # (the SNSC raison d'être).
        assert (
            await resolve_keeper(conn, "SNSC", entity_class="entity")
            == "Supreme National Security Council"
        )
        # An EXACT-surface match is the same referent by construction — it folds
        # even under an "incompatible" class (the guard is fallback-only).
        assert (
            await resolve_keeper(
                conn, "supreme national security council", entity_class="person"
            )
            == "Supreme National Security Council"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e1_1_exact_alias_override_prefers_higher_class_keeper(pg_pool):
    """E1.1 — the aliased-fragment leak. A bare fragment that ALSO exists as its
    OWN active 'entity' row (the live SNSC row 7b1671c9) used to short-circuit the
    exact-canonical probe and resolve to ITSELF, never folding to the higher-class
    keeper that already lists it as a merged_alias. The override must now prefer
    the keeper. Uses unique names (shared session DB)."""
    async with pg_pool.acquire() as conn:
        # the higher-class keeper claims the acronym as an alias ...
        await _seed_keeper(conn, name="Supreme National Security CouncilE11",
                           aliases=["SNSCE11"])
        # ... AND the bare fragment exists as its OWN active 'entity' row.
        await _seed_keeper(conn, name="SNSCE11", entity_class="entity")
        # class-less endpoint: the override folds onto the org, not the fragment.
        assert (await resolve_keeper(conn, "SNSCE11")
                == "Supreme National Security CouncilE11")
        # an explicit 'entity' class is still class-less → still folds.
        assert (await resolve_keeper(conn, "SNSCE11", entity_class="entity")
                == "Supreme National Security CouncilE11")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e1_1_override_respects_class_guard(pg_pool):
    """The E1.1 override is class-guarded: a person-typed endpoint that exactly
    matches a bare 'entity' fragment must NOT be pulled onto an incompatible-class
    (organization) alias-keeper — it stays the exact fragment surface (the
    'Mercury' person vs 'Mercury Systems' org)."""
    async with pg_pool.acquire() as conn:
        await _seed_keeper(conn, name="Mercury SystemsE11", aliases=["MercuryE11"])
        await _seed_keeper(conn, name="MercuryE11", entity_class="entity")
        # person endpoint: the org alias-keeper is class-incompatible → no fold.
        assert (await resolve_keeper(conn, "MercuryE11", entity_class="person")
                == "MercuryE11")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e1_1_classified_exact_row_not_folded(pg_pool):
    """Precision fix (adversarial review): the override fires ONLY when the
    exact-canonical hit is itself a bare 'entity' fragment. A PROPERLY-CLASSIFIED
    exact row (a 'location') that merely shares a surface with some org's accreted
    alias is a real, deliberately-distinct entity and must NOT be blind-folded —
    that ambiguity is the LLM researcher's call (the Palmyra city vs a 'Palmyra
    Atoll…' org that folded the string as an alias)."""
    async with pg_pool.acquire() as conn:
        await _seed_keeper(conn, name="Palmyra Atoll ResearchE11", aliases=["PalmyraE11"])
        await _seed_keeper(conn, name="PalmyraE11", entity_class="location")
        # class-less endpoint, but the exact row is a real 'location' → NOT folded.
        assert await resolve_keeper(conn, "PalmyraE11") == "PalmyraE11"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e1_1_country_exact_match_never_overridden(pg_pool):
    """A country exact-canonical match is never overridden — a country is a
    properly-classified row (not a bare 'entity' fragment), so the alias-keeper
    probe is skipped entirely (subsumed by the classified-row guard above)."""
    async with pg_pool.acquire() as conn:
        await _seed_keeper(conn, name="GeorgiaE11", entity_class="country")
        # an org that (wrongly) lists the country surface as an alias must NOT win.
        await _seed_keeper(conn, name="Georgia Tech UnivE11", aliases=["GeorgiaE11"])
        assert await resolve_keeper(conn, "GeorgiaE11") == "GeorgiaE11"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_per_cycle_cache_short_circuits_probe(seeded_pool):
    """The per-cycle memo returns a resolved surface WITHOUT re-probing — proven
    by swapping in a conn whose every probe raises and still getting the cached
    keeper. The key separates by class (a class-blind hit never masks a scoped)."""
    cache: dict[str, str] = {}
    async with seeded_pool.acquire() as conn:
        # Populate: class-less folds to the keeper; person-typed stays distinct.
        assert (
            await resolve_keeper(conn, "SNSC", entity_class="entity", cache=cache)
            == "Supreme National Security Council"
        )
        assert (
            await resolve_keeper(conn, "SNSC", entity_class="person", cache=cache)
            == "SNSC"
        )
    # Two distinct entries — the class is part of the key.
    assert len(cache) == 2
    # A raising conn now proves the second read never touches the DB.
    raising = _RaisingConn()
    assert (
        await resolve_keeper(raising, "SNSC", entity_class="entity", cache=cache)
        == "Supreme National Security Council"
    )
    assert (
        await resolve_keeper(raising, "SNSC", entity_class="person", cache=cache)
        == "SNSC"
    )


# ---------------------------------------------------------------------------
# resolve_keeper — degrade-not-break (a raising conn NEVER raises / NEVER drops)
# ---------------------------------------------------------------------------


class _RaisingConn:
    """A conn whose every probe raises — the resolver must swallow it."""

    async def fetchrow(self, *_a: Any, **_k: Any) -> Any:
        raise RuntimeError("simulated DB failure")


@pytest.mark.asyncio
async def test_error_returns_input_unchanged_no_raise():
    conn = _RaisingConn()
    # A probe failure must leave the ORIGINAL endpoint, never raise, never empty.
    assert await resolve_keeper(conn, "SNSC") == "SNSC"
    assert await resolve_keeper(conn, "Iran", entity_class="country") == "Iran"
    # Empty input short-circuits before any probe — still no raise.
    assert await resolve_keeper(conn, "") == ""


# ---------------------------------------------------------------------------
# Producer-level — the reifier rewrites an alias endpoint to its keeper and
# drops a resulting self-loop (N4)
# ---------------------------------------------------------------------------


import json
from uuid import uuid4


class _CannedTyperLLM:
    """Canned typing JSON — mirrors the reifier suite's stub (no live model)."""

    subprovider = "stub"

    def __init__(self, obj: dict[str, Any]) -> None:
        self._obj = obj
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, *, max_tokens=None,
                            temperature=None, system=None, **kwargs):
        self.calls.append({"system": system})
        content_json = json.dumps(self._obj)

        class _Usage:
            prompt_tokens = 11
            completion_tokens = 7
            reasoning_tokens = 0

        class _Response:
            content = content_json
            usage = _Usage()

        return _Response()


def _tag() -> str:
    """A surface suffix nothing else in the suite (or the last nightly) carries.

    ORDER DEPENDENCE, and where it came from. `proposed_edges`, `entity_profiles`
    and `nexuses` all live in the session-scoped test DB the whole suite shares,
    and that DB PERSISTS between runs. The producer under test here —
    `_promote_candidates` — is a global drain: it takes every pending edge above
    `min_confidence` and writes a nexus for each. So a fixed surface string is
    not the test's own row, it is a row co-owned by every past and future run.

    Under `--randomly-seed` that landed as a real failure: the read-back
    `ORDER BY created_at DESC LIMIT 1` over `lower(object)='united states'`
    returned `SNSCUNIQ` — a sibling test's fragment, promoted un-rewritten
    because ITS keeper had not been seeded yet — and the assertion failed on a
    row this test never created, while its own edge had been rewritten
    perfectly. Tagging the surfaces makes each test's endpoints unforgeable, so
    the read-back can name them instead of guessing at "the newest one".
    """
    return uuid4().hex[:8].upper()


async def _nexuses_touching(conn, tag: str) -> int:
    """How many live nexuses carry THIS test's tagged surfaces on either end."""
    return await conn.fetchval(
        "SELECT count(*) FROM nexuses "
        " WHERE subject LIKE '%' || $1 || '%' OR object LIKE '%' || $1 || '%'",
        tag,
    )


async def _seed_proposed_edge(conn, *, src: str, tgt: str, conf: float = 0.7):
    await conn.execute(
        """
        INSERT INTO proposed_edges
            (source_entity, target_entity, relationship_type, confidence,
             evidence_text, status)
        VALUES ($1, $2, 'co_occurs', $3, $4, 'pending')
        ON CONFLICT (lower(source_entity), lower(target_entity),
                     relationship_type)
        -- Status must RESET on re-seed: the pending-only selector (K-G2 queue
        -- fix) makes a processed pair terminal, so a stale 'promoted'/'rejected'
        -- from a prior invocation against a persistent test DB would silently
        -- starve the test that re-seeds the same pair.
        DO UPDATE SET confidence = EXCLUDED.confidence,
                      status = 'pending', reviewed_at = NULL
        """,
        src, tgt, conf, f"{src} and {tgt} appear together",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reifier_rewrites_alias_endpoint_to_keeper(pg_pool):
    from legba.data.analysts.relationship_reifier import ReifierDeps, run_method

    # A keeper whose merged_aliases hold the fragment endpoint the LLM will emit.
    async with pg_pool.acquire() as conn:
        await _seed_keeper(
            conn,
            name="Supreme National Security Council",
            aliases=["SNSCUNIQ"],
        )
        await _seed_keeper(conn, name="United States", entity_class="country")
        await _seed_proposed_edge(conn, src="SNSCUNIQ", tgt="United States")

    # The typer returns the FRAGMENT surface as the subject — E1 must rewrite it.
    llm = _CannedTyperLLM({
        "related": True,
        "subject": "SNSCUNIQ",
        "object": "United States",
        "rel_type": "HostileTo",
        "intent": "hostile",
        "channel": "direct",
        "confidence": 0.8,
    })
    deps = ReifierDeps(llm=llm, pg_pool=pg_pool, max_candidates=10)
    result = await run_method(
        inputs=[],
        options={"analyst_id": "relationship_reifier", "run_id": str(uuid4()),
                 # These tests exercise KEEPER REWRITING, not candidate
                 # qualification — the single-evidence fixtures predate the
                 # K-G2 bar and would be (correctly) below it. Disable the
                 # bar explicitly so the test keeps testing what it tests.
                 "qualification_bar": 0.0, "min_independent_sources": 0},
        deps=deps,
    )
    assert result.finding.data["written"] >= 1, result.finding.data

    # The written nexus carries the KEEPER canonical_name, NOT the fragment.
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT subject, object, label FROM nexuses "
            "WHERE lower(object)='united states' "
            "AND valid_until IS NULL AND superseded_by IS NULL "
            "ORDER BY created_at DESC LIMIT 1"
        )
    assert row is not None, "a nexus must be written"
    assert row["subject"] == "Supreme National Security Council", row["subject"]
    # The fragment must NOT survive anywhere as a graph actor endpoint.
    assert row["subject"] != "SNSCUNIQ"
    # The label is refreshed with the keeper surface.
    assert "Supreme National Security Council" in row["label"]
    assert "SNSCUNIQ" not in row["label"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reifier_drops_keeper_self_loop_n4(pg_pool):
    from legba.data.analysts.relationship_reifier import ReifierDeps, run_method

    # ONE keeper holds BOTH surfaces as aliases — so both endpoints resolve to it.
    tag = _tag()
    keeper, frag = f"Axis of Resistance{tag}", f"Resistance{tag}FRAG"
    async with pg_pool.acquire() as conn:
        await _seed_keeper(conn, name=keeper, aliases=[frag])
        await _seed_proposed_edge(conn, src=keeper, tgt=frag)

    # The typer emits the two DISTINCT surfaces (they differ as strings, so the
    # pre-keeper self-loop gate in _coerce_typing does NOT catch them — that is
    # exactly the N4 miss). After keeper rewrite both become the SAME keeper.
    llm = _CannedTyperLLM({
        "related": True,
        "subject": keeper,
        "object": frag,
        "rel_type": "AffiliatedWith",
        "intent": "supportive",
        "channel": "direct",
        "confidence": 0.8,
    })
    deps = ReifierDeps(llm=llm, pg_pool=pg_pool, max_candidates=10)
    result = await run_method(
        inputs=[],
        options={"analyst_id": "relationship_reifier", "run_id": str(uuid4()),
                 # These tests exercise KEEPER REWRITING, not candidate
                 # qualification — the single-evidence fixtures predate the
                 # K-G2 bar and would be (correctly) below it. Disable the
                 # bar explicitly so the test keeps testing what it tests.
                 "qualification_bar": 0.0, "min_independent_sources": 0},
        deps=deps,
    )
    # The protected PROPERTY: a keeper-collapsed self-loop never becomes a
    # nexus. The MECHANISM moved with the K-G2 queue fix — the merge-aware
    # selector now drops the pair BEFORE typing (keeper_self_loop counter)
    # instead of after the LLM call, which is the same outcome one LLM call
    # cheaper. Accept either path; reject only a written edge.
    data = result.finding.data
    assert data["written"] == 0, data
    sel = data.get("selection", {})
    assert data["typed"] >= 1 or sel.get("keeper_self_loop", 0) >= 1, data

    async with pg_pool.acquire() as conn:
        n = await _nexuses_touching(conn, tag)
    assert n == 0, "the self-loop nexus must NOT be written"


# ---------------------------------------------------------------------------
# Producer-level — proposed_edge_governance rewrites + drops the self-loop
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governance_rewrites_alias_endpoint_to_keeper(pg_pool):
    from legba.data.analysts.deterministic_handlers.proposed_edge_governance import (
        _promote_candidates,
    )
    from legba.data.provenance import AnalystContext

    tag = _tag()
    keeper, frag = f"Supreme National Security Council{tag}", f"SNSC{tag}"
    country = f"United States{tag}"
    async with pg_pool.acquire() as conn:
        await _seed_keeper(conn, name=keeper, aliases=[frag])
        await _seed_keeper(conn, name=country, entity_class="country")
        # A promotable co-occurrence whose SOURCE endpoint is the fragment alias.
        await _seed_proposed_edge(conn, src=frag, tgt=country, conf=0.9)

        actx = AnalystContext(
            analyst_id="proposed_edge_governance",
            analyst_version="test",
            run_id=uuid4(),
            target_id=None,
            target_version=None,
        )
        promoted = await _promote_candidates(
            conn,
            analyst_id="proposed_edge_governance",
            analyst_version="test",
            run_id=actx.run_id,
            target_id=None,
            target_version=None,
            min_confidence=0.6,
            limit=50,
        )
        assert promoted >= 1, "the alias-endpoint edge must promote"
        # Read back THIS edge's nexus by its own tagged object, not "the newest
        # row that happens to point at the United States" — `_promote_candidates`
        # drains every pending edge in the shared queue, so that ordering picked
        # whichever sibling promoted last (see `_tag`).
        rows = await conn.fetch(
            "SELECT subject, object FROM nexuses "
            "WHERE object = $1 "
            "AND valid_until IS NULL AND superseded_by IS NULL",
            country,
        )
    assert len(rows) == 1, "one edge in, one live nexus out"
    assert rows[0]["subject"] == keeper, rows[0]["subject"]
    assert rows[0]["subject"] != frag


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governance_drops_keeper_self_loop_n4(pg_pool):
    from legba.data.analysts.deterministic_handlers.proposed_edge_governance import (
        _promote_candidates,
    )

    tag = _tag()
    keeper, frag = f"Axis of Resistance{tag}", f"Resistance{tag}FRAG"
    async with pg_pool.acquire() as conn:
        await _seed_keeper(conn, name=keeper, aliases=[frag])
        await _seed_proposed_edge(conn, src=keeper, tgt=frag, conf=0.9)
        await _promote_candidates(
            conn,
            analyst_id="proposed_edge_governance",
            analyst_version="test",
            run_id=uuid4(),
            target_id=None,
            target_version=None,
            min_confidence=0.6,
            limit=50,
        )
        # It did NOT promote (rewrite → self-loop → rejected). The old
        # `promoted == 0` asserted the whole shared queue was empty of anything
        # promotable — a statement about the suite, and false the moment a
        # sibling left a pending edge behind. What this test claims is about ITS
        # pair: rejected, and no nexus. Both are stronger scoped, because a
        # global zero was equally consistent with the drain never running.
        status = await conn.fetchval(
            "SELECT status FROM proposed_edges "
            "WHERE source_entity = $1 AND target_entity = $2",
            keeper, frag,
        )
        assert status == "rejected", status
        n = await _nexuses_touching(conn, tag)
    assert n == 0, "the self-loop nexus must NOT be written"
