# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Country-alias gazetteer (:mod:`legba.data._country_aliases`) + its E3 probe.

E4a recall lever (2026-07-28, precision 1.000 / recall 0.347): official-vs-
common state names ("Myanmar"/"Burma") share no block-key token and no trigram
mass, so the E3 probes could never even PROPOSE the pair. The gazetteer is a
small curated exact-match module; the probe in ``generate_candidates`` turns a
group co-membership into a candidate pair (auto_merge only for two
country-typed rows; anything else gray). Covered here:

  * every curated alias pair resolves (pure, incl. case/diacritic/article
    variants) and the SQL normalization twin agrees with the Python one;
  * every FORBIDDEN pair does NOT resolve (Sudan/South Sudan, the Korea pairs,
    China/Taiwan, bare Congo, cross-Congo groups);
  * DB probe: country+country alias twins -> auto_merge candidate;
    country+location / country+generic -> gray; person-typed sides -> never
    proposed; forbidden pairs -> no candidate at all;
  * non-country entities are unaffected (no gazetteer signal, no new pairs).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data._country_aliases import (
    SQL_NORM_TEMPLATE,
    alias_group_id,
    alias_group_key,
    alias_surfaces,
    are_country_aliases,
    normalize_country_surface,
)
from legba.data._entity_candidates import CandidatePair, generate_candidates
from legba.data.config import PostgresConfig


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _seed(conn: Any, name: str, *, cls: str = "country",
                geo: str | None = None) -> str:
    """Insert an ACTIVE row (idempotent on the (lower(name), class) unique key
    — this file seeds REAL country names, which another test in the shared
    session DB may have inserted too); return the row's id."""
    eid = str(uuid4())
    row = await conn.fetchrow(
        """
        INSERT INTO entity_profiles
            (id, canonical_name, entity_class, entity_type, geo_country, data)
        VALUES ($1::uuid, $2, $3, $3, $4, '{}'::jsonb)
        ON CONFLICT (lower(canonical_name), entity_class) DO UPDATE
            SET updated_at = now()
        RETURNING id
        """,
        eid, name, cls, geo,
    )
    return str(row["id"])


def _find(pairs: list[CandidatePair], a: str, b: str) -> CandidatePair | None:
    names = {a.lower(), b.lower()}
    for p in pairs:
        if {p.left_name.lower(), p.right_name.lower()} == names:
            return p
    return None


# ---------------------------------------------------------------------------
# Pure gazetteer semantics (no DB)
# ---------------------------------------------------------------------------

#: One representative pair per curated group + variant robustness cases.
_EXPECTED_ALIAS_PAIRS = [
    ("East Timor", "Timor-Leste"),
    ("Democratic Republic of the Congo", "DR Congo"),
    ("DRC", "Congo-Kinshasa"),
    ("DR Congo", "DRC"),
    ("Republic of the Congo", "Congo-Brazzaville"),
    ("Myanmar", "Burma"),
    ("Côte d'Ivoire", "Ivory Coast"),
    ("Cabo Verde", "Cape Verde"),
    ("Eswatini", "Swaziland"),
    ("North Macedonia", "Macedonia"),
    ("Czechia", "Czech Republic"),
    ("Türkiye", "Turkey"),
    ("Holy See", "Vatican City"),
    # normalization robustness: case, diacritic fold, leading article
    ("BURMA", "myanmar"),
    ("Turkiye", "Turkey"),
    ("the Democratic Republic of the Congo", "DRC"),
    ("the Czech Republic", "Czechia"),
]

_FORBIDDEN_PAIRS = [
    ("Sudan", "South Sudan"),
    ("North Korea", "South Korea"),
    ("Korea", "North Korea"),
    ("Korea", "South Korea"),
    ("China", "Taiwan"),
    # the two Congo republics are DISTINCT states in SEPARATE groups
    ("Democratic Republic of the Congo", "Republic of the Congo"),
    ("Congo-Kinshasa", "Congo-Brazzaville"),
    ("DRC", "Congo-Brazzaville"),
    # bare "Congo" is ambiguous and in NO group
    ("Congo", "Democratic Republic of the Congo"),
    ("Congo", "Republic of the Congo"),
]


def test_every_curated_alias_pair_resolves():
    for a, b in _EXPECTED_ALIAS_PAIRS:
        assert are_country_aliases(a, b), (a, b)
        assert are_country_aliases(b, a), (b, a)  # bidirectional


def test_forbidden_pairs_never_resolve():
    for a, b in _FORBIDDEN_PAIRS:
        assert not are_country_aliases(a, b), (a, b)
        assert not are_country_aliases(b, a), (b, a)


def test_non_country_names_have_no_group():
    for name in ("Boeing", "Ali Khamenei", "the Atlantic", "NATO",
                 "Black Sea Fleet", "", "Macedonia Airlines"):
        assert alias_group_id(name) is None, name
    # a name is not its own alias
    assert not are_country_aliases("Myanmar", "Myanmar")
    assert not are_country_aliases("Türkiye", "Turkiye")  # same normalized form


def test_group_key_is_stable_first_member():
    assert alias_group_key("Burma") == "myanmar"
    assert alias_group_key("Turkey") == "turkiye"
    assert alias_group_key("Congo-Brazzaville") == "republic of the congo"
    assert alias_group_key("Boeing") is None


def test_normalization_shape():
    assert normalize_country_surface("Côte d'Ivoire") == "cote d ivoire"
    assert normalize_country_surface("Türkiye") == "turkiye"
    assert normalize_country_surface("  the  Czech   Republic ") == "czech republic"
    assert normalize_country_surface("Timor-Leste") == "timor leste"
    # idempotent
    for s in alias_surfaces():
        assert normalize_country_surface(s) == s


# ---------------------------------------------------------------------------
# SQL twin agreement + the candidate probe (DB)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_normalization_twin_agrees_with_python(pg_pool):
    """The probe matches rows via SQL_NORM_TEMPLATE; if the SQL and Python
    normalizations ever drift, alias rows silently stop matching. Assert
    agreement on every curated surface plus the variant-bearing raw forms."""
    expr = SQL_NORM_TEMPLATE.format(col="$1")
    raws = [s for group_pair in _EXPECTED_ALIAS_PAIRS for s in group_pair]
    async with pg_pool.acquire() as conn:
        for raw in raws + list(alias_surfaces()):
            got = await conn.fetchval(f"SELECT {expr}", raw)
            assert got == normalize_country_surface(raw), raw


@pytest.mark.integration
@pytest.mark.asyncio
async def test_country_country_alias_twin_is_auto_merge(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Myanmar", cls="country")
        await _seed(conn, "Burma", cls="country")
        await _seed(conn, "Czechia", cls="country")
        await _seed(conn, "Czech Republic", cls="country")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=0)
    p = _find(pairs, "Myanmar", "Burma")
    assert p is not None, "the curated alias pair must surface as a candidate"
    assert p.band == "auto_merge", p
    assert "country_alias" in p.signals
    p2 = _find(pairs, "Czechia", "Czech Republic")
    assert p2 is not None
    assert p2.band == "auto_merge", p2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mixed_class_alias_pair_is_gray(pg_pool):
    # A location-typed side may be a different referent (the Greek region
    # "Macedonia"), so anything but country+country stays LLM-adjudicated.
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Côte d'Ivoire", cls="country")
        await _seed(conn, "Ivory Coast", cls="location")
        await _seed(conn, "Eswatini", cls="country")
        await _seed(conn, "Swaziland", cls="entity")  # generic bucket side
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=0)
    p = _find(pairs, "Côte d'Ivoire", "Ivory Coast")
    assert p is not None, "the alias probe must bridge country<->location"
    assert p.band == "gray", p
    assert "country_alias" in p.signals
    p2 = _find(pairs, "Eswatini", "Swaziland")
    assert p2 is not None
    assert p2.band == "gray", p2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_person_typed_side_never_proposed(pg_pool):
    # A person named after a country is not that country: no gazetteer pair.
    # (Block keys differ and trgm is off, so no other probe pairs them either.)
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Cabo Verde", cls="country")
        await _seed(conn, "Cape Verde", cls="person")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=0)
    assert _find(pairs, "Cabo Verde", "Cape Verde") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forbidden_pairs_are_not_candidates(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Sudan", cls="country")
        await _seed(conn, "South Sudan", cls="country")
        await _seed(conn, "North Korea", cls="country")
        await _seed(conn, "South Korea", cls="country")
        await _seed(conn, "China", cls="country")
        await _seed(conn, "Taiwan", cls="country")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=0)
    assert _find(pairs, "Sudan", "South Sudan") is None
    assert _find(pairs, "North Korea", "South Korea") is None
    assert _find(pairs, "China", "Taiwan") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_congo_groups_never_pair(pg_pool):
    # The two Congo republics are DISTINCT states: members of the DRC group
    # must never pair with members of the Brazzaville group.
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Congo-Kinshasa", cls="country")
        await _seed(conn, "Congo-Brazzaville", cls="country")
        await _seed(conn, "DRC", cls="country")
        await _seed(conn, "Republic of the Congo", cls="country")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=0)
    assert _find(pairs, "Congo-Kinshasa", "Congo-Brazzaville") is None
    assert _find(pairs, "DRC", "Republic of the Congo") is None
    assert _find(pairs, "DRC", "Congo-Brazzaville") is None
    # while WITHIN-group members do pair
    p = _find(pairs, "Congo-Kinshasa", "DRC")
    assert p is not None and p.band == "auto_merge"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_non_country_entities_unaffected(pg_pool):
    # Ordinary org rows gain no gazetteer signal and no new pairs from this
    # probe; the exact-key path still bands them exactly as before.
    async with pg_pool.acquire() as conn:
        await _seed(conn, "Zzaliasorg Council", cls="organization")
        await _seed(conn, "the Zzaliasorg Council", cls="organization")
        pairs = await generate_candidates(conn, exact_limit=2000, trgm_limit=0)
    p = _find(pairs, "Zzaliasorg Council", "the Zzaliasorg Council")
    assert p is not None
    assert p.band == "auto_merge"
    assert "country_alias" not in p.signals
    assert not [
        q for q in pairs
        if "country_alias" in q.signals
        and "zzaliasorg" in (q.left_name + q.right_name).lower()
    ]
