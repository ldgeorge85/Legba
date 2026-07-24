# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E3 — blocking + candidate generation (:mod:`legba.data._entity_candidates`).

The entity_researcher (E4) must not compare 22k profiles pairwise. E3 BLOCKS on
migration-0088 indexes (exact ``entity_block_key`` + trigram) and emits banded,
hard-negative-filtered candidate pairs. This module NEVER merges — it only
generates + suggests a band. Covered here:

  * exact MULTI-token block key + same SPECIFIC class → ``auto_merge``
    (Ali Khamenei ≡ Ayatollah Ali Khamenei);
  * the father/son SAFETY property: Ali Khamenei is NEVER auto_merged with
    Mojtaba Khamenei (distinct block keys) — at most a GRAY suggestion;
  * a SINGLE-token exact key → ``gray`` (needs the LLM), never auto_merge;
  * class-incompatible pair (org vs location sharing a key) → NOT a candidate;
  * a geo_country conflict → NOT a candidate;
  * a junk fragment endpoint → NOT a candidate;
  * a trigram near-miss (Netanyahu/Netanyahoo) → ``gray``.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data._entity_candidates import CandidatePair, generate_candidates
from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _seed(conn: Any, name: str, *, cls: str = "person",
                geo: str | None = None) -> str:
    """Insert a fresh ACTIVE keeper; return its id. Unique names per test keep
    the session-shared DB from cross-contaminating candidate sets."""
    eid = str(uuid4())
    await conn.execute(
        """
        INSERT INTO entity_profiles
            (id, canonical_name, entity_class, entity_type, geo_country, data)
        VALUES ($1::uuid, $2, $3, $3, $4, '{}'::jsonb)
        """,
        eid, name, cls, geo,
    )
    return eid


def _find(pairs: list[CandidatePair], a: str, b: str) -> CandidatePair | None:
    names = {a.lower(), b.lower()}
    for p in pairs:
        if {p.left_name.lower(), p.right_name.lower()} == names:
            return p
    return None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multitoken_exact_same_class_is_auto_merge(pg_pool):
    async with pg_pool.acquire() as conn:
        # A NON-person org article-variant (ordered tokens equal after strip).
        await _seed(conn, "Zzqmerge Council", cls="organization")
        await _seed(conn, "the Zzqmerge Council", cls="organization")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=2000)
    p = _find(pairs, "Zzqmerge Council", "the Zzqmerge Council")
    assert p is not None, "the article variant must block onto the base name"
    assert p.band == "auto_merge", p
    assert "exact_block_key" in p.signals


@pytest.mark.integration
@pytest.mark.asyncio
async def test_person_multitoken_exact_is_gray_not_auto(pg_pool):
    # SAFETY (review CRITICAL): persons NEVER auto_merge — name permutation is
    # meaningful (father/son, patronymic reversal). All person merges -> LLM.
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Zzperson Alpha Name", cls="person")
        await _seed(conn, "Dr Zzperson Alpha Name", cls="person")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=2000)
    p = _find(pairs, "Zzperson Alpha Name", "Dr Zzperson Alpha Name")
    assert p is not None
    assert p.band == "gray", "person pairs are adjudicated, never auto_merged"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_anagram_not_auto_merge(pg_pool):
    # SAFETY (review CRITICAL): two DISTINCT orgs whose tokens are a permutation
    # share the sorted-DISTINCT block key but must NOT auto_merge — the
    # order-sensitive guard demotes the anagram to gray ("Congo Republic" vs
    # "Republic Congo"). Multi-token, non-person, same class.
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Zzanag Republic", cls="organization")
        await _seed(conn, "Republic Zzanag", cls="organization")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=2000)
    p = _find(pairs, "Zzanag Republic", "Republic Zzanag")
    assert p is not None, "the anagram shares a block key -> is a candidate"
    assert p.band == "gray", "a token permutation must not auto_merge"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_father_son_never_auto_merge(pg_pool):
    """SAFETY: distinct people who share a surname must NOT auto_merge."""
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Aliqq Zzsurname", cls="person")
        await _seed(conn, "Mojtabaqq Zzsurname", cls="person")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=2000)
    p = _find(pairs, "Aliqq Zzsurname", "Mojtabaqq Zzsurname")
    # They have DISTINCT block keys ([aliqq zzsurname] vs [mojtabaqq zzsurname]),
    # so exact blocking never pairs them; if trigram surfaces them at all it is
    # only ever a GRAY suggestion the LLM must confirm — NEVER auto_merge.
    assert p is None or p.band == "gray", p


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_token_exact_is_gray(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Zzatlanticorg", cls="organization")
        await _seed(conn, "the Zzatlanticorg", cls="organization")  # -> same 1-tok key
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=2000)
    p = _find(pairs, "Zzatlanticorg", "the Zzatlanticorg")
    assert p is not None
    assert p.band == "gray", "a single-token exact key needs the adjudicator"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_incompatible_class_not_a_candidate(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Zzmagocean", cls="organization")
        await _seed(conn, "Zzmagocean", cls="location")  # magazine vs ocean
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=2000)
    assert _find(pairs, "Zzmagocean", "Zzmagocean") is None, \
        "org<->location is a keep-distinct ambiguity, never a merge candidate"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_geo_conflict_not_a_candidate(pg_pool):
    # (lower(name), class) is UNIQUE, so the two sides must be distinct SURFACES
    # that share a BLOCK KEY — "Zzgeoclash Cityname" / "the Zzgeoclash Cityname"
    # both key to [cityname zzgeoclash] (would auto_merge) but sit in IR vs IL.
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Zzgeoclash Cityname", cls="location", geo="IR")
        await _seed(conn, "the Zzgeoclash Cityname", cls="location", geo="IL")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=2000)
    assert _find(pairs, "Zzgeoclash Cityname", "the Zzgeoclash Cityname") is None, \
        "two same-key places in different countries are distinct (geo conflict)"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_junk_fragment_not_a_candidate(pg_pool):
    async with pg_pool.acquire() as conn:
        # "west" is a _VAGUE_ENDPOINT_TOKENS junk term (is_junk_entity True).
        await _seed(conn, "West", cls="entity")
        await _seed(conn, "the West", cls="entity")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=2000)
    assert _find(pairs, "West", "the West") is None, \
        "a junk fragment is E6's prune target, never a merge side"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trigram_near_miss_is_gray(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Zzbibinetanyahu", cls="person")
        await _seed(conn, "Zzbibinetanyahoo", cls="person")  # 1-char typo
        pairs = await generate_candidates(
            conn, min_trgm=0.5, exact_limit=2000, trgm_limit=2000,
        )
    p = _find(pairs, "Zzbibinetanyahu", "Zzbibinetanyahoo")
    assert p is not None, "a 1-char typo must surface via the trigram probe"
    assert p.band == "gray"
    assert any(s.startswith("trgm:") for s in p.signals)


@pytest.mark.asyncio
async def test_pure_helpers_no_db():
    """The class/geo helpers are pure — exercise them without a DB."""
    from legba.data._entity_candidates import _class_compatible, _geo_conflict
    assert _class_compatible("person", "person")
    assert _class_compatible("organization", "corporation")
    assert _class_compatible("entity", "person")  # generic folds into anything
    assert not _class_compatible("person", "organization")
    assert not _class_compatible("country", "location")  # keep-distinct
    assert _geo_conflict("IR", "IL")
    assert not _geo_conflict("IR", "IR")
    assert not _geo_conflict(None, "IL")  # one side unknown => no conflict
    assert not _geo_conflict("", "IL")
