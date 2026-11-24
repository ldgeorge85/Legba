# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OPEN-1 (W2) — the situation clusterer's composition exclusion is DERIVED from
the registry, with the literal frozenset as the floor and the fallback.

The literal ``_COMPOSITION_ANALYST_IDS`` failed OPEN: a report producer nobody
listed flowed into the clusterer and minted a fake situation out of a report
(live 2026-10-03: ``escalation_dyad``, kind ``meta_findings_synthesizer``, had
minted a dormant "Cross-desk read" frame on ``composition:escalation_dyad:world``).

Covered:
  * the GUARD — the derivation over the repository's analyst heads is a superset
    of the literal (and catches the dyad the literal missed);
  * the live resolver: registry ∪ literal on success, the literal alone on any
    failure, never an empty (fail-open) set;
  * the two gates (finding_supersession's fetch + cluster, situation_clustering's
    materialization) through a migrated database: an UNLISTED analyst whose head
    declares a composition kind never reaches the clusterer.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts import deterministic
from legba.data.analysts.deterministic_handlers import finding_supersession as fs
from legba.data.config import PostgresConfig

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _committed_analyst_heads() -> list[dict[str, Any]]:
    heads = []
    for path in sorted((REPO_ROOT / "descriptors").glob("analyst_*.yaml")):
        ident = (yaml.safe_load(path.read_text()) or {}).get("identity") or {}
        heads.append({"descriptor_id": ident.get("id"), "kind": ident.get("kind")})
    return heads


def test_derived_set_is_a_superset_of_the_literal():
    """THE GUARD. Every id the literal names is a head whose declared kind the
    derivation recognises — so the registry path can only ever exclude MORE."""
    derived = fs.derive_composition_analyst_ids(_committed_analyst_heads())
    missing = fs._COMPOSITION_ANALYST_IDS - derived
    assert not missing, (
        f"literal ids whose head kind is not a COMPOSITION_PRODUCER_KINDS kind: "
        f"{sorted(missing)} — add the kind, or the derivation fails open for them"
    )
    # ...and it catches the producer the literal missed.
    assert "escalation_dyad" in derived
    assert "escalation_dyad" not in fs._COMPOSITION_ANALYST_IDS


def test_derivation_never_admits_a_first_order_kind():
    derived = fs.derive_composition_analyst_ids(_committed_analyst_heads())
    by_id = {h["descriptor_id"]: h["kind"] for h in _committed_analyst_heads()}
    assert {by_id[i] for i in derived} <= set(fs.COMPOSITION_PRODUCER_KINDS)
    for unit in ("escalation", "military_posture", "situation_clustering"):
        assert unit not in derived


def test_derive_tolerates_mixed_and_malformed_rows():
    rows = [
        ("a_comp", "meta_findings_synthesizer"),
        {"descriptor_id": "b_corr", "kind": "cross_analyst_correlator"},
        ("unit", "inline_target"),
        (None, "meta_findings_synthesizer"),
        ("short",),
        42,
    ]
    assert fs.derive_composition_analyst_ids(rows) == {"a_comp", "b_corr"}
    assert fs.derive_composition_analyst_ids(None) == frozenset()


class _Conn:
    def __init__(self, rows: Any = None, exc: Exception | None = None) -> None:
        self._rows = rows or []
        self._exc = exc
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        if self._exc is not None:
            raise self._exc
        return self._rows


@pytest.mark.asyncio
async def test_resolver_unions_the_registry_with_the_literal():
    conn = _Conn([{"descriptor_id": "escalation_dyad"}, {"descriptor_id": "world_assessor"}])
    got = await fs.resolve_composition_analyst_ids(conn)
    assert got == fs._COMPOSITION_ANALYST_IDS | {"escalation_dyad"}
    sql, params = conn.calls[0]
    assert "analyst_descriptors" in sql and "is_head" in sql
    assert params[0] == sorted(fs.COMPOSITION_PRODUCER_KINDS)


@pytest.mark.asyncio
async def test_resolver_falls_back_to_the_literal_never_to_empty():
    assert await fs.resolve_composition_analyst_ids(_Conn([])) == fs._COMPOSITION_ANALYST_IDS
    failed = await fs.resolve_composition_analyst_ids(_Conn(exc=RuntimeError("down")))
    assert failed == fs._COMPOSITION_ANALYST_IDS


def test_cluster_honours_the_derived_exclusion():
    rows = [
        {"id": "1", "produced_at": 1, "analyst_id": "escalation_dyad",
         "situation_signature": "sit:composition:escalation_dyad:world"},
        {"id": "2", "produced_at": 2, "analyst_id": "escalation_dyad",
         "situation_signature": "sit:composition:escalation_dyad:world"},
    ]
    # The literal alone lets the dyad through (the defect)...
    assert fs._cluster([dict(r) for r in rows], sub_handler_fallback=None)
    # ...the derived set does not.
    derived = fs._COMPOSITION_ANALYST_IDS | {"escalation_dyad"}
    assert fs._cluster(
        [dict(r) for r in rows], sub_handler_fallback=None, exclude=derived,
    ) == {}


# ---------------------------------------------------------------------------
# Through a migrated database — the real binding paths
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def unlisted_producer(pool):
    """A composition producer the literal has never heard of: a head declaring
    ``meta_findings_synthesizer``, plus two findings stamped on one composition
    signature (exactly the dyad's live shape)."""
    analyst_id = f"open1_dyad_{uuid4().hex[:10]}"
    sig = f"sit:composition:{analyst_id}:world"
    ids = []
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_descriptors
                (descriptor_id, version, schema_uri, is_head, kind, state, owner,
                 name, body)
            VALUES ($1, 'v1', 'legba/analyst/1.0.0', TRUE,
                    'meta_findings_synthesizer', 'active', 'test', 'OPEN-1 probe',
                    $2::jsonb)
            """,
            analyst_id,
            json.dumps({"identity": {"id": analyst_id,
                                     "kind": "meta_findings_synthesizer"}}),
        )
        for hours_ago in (1, 2):
            fid = uuid4()
            ids.append(fid)
            await conn.execute(
                """
                INSERT INTO analyst_outputs
                    (id, kind, title, body, confidence, data, analyst_id,
                     target_id, produced_at, schema_uri, situation_signature,
                     severity)
                VALUES ($1, 'finding', 'Cross-desk read', '', 0.9, '{}'::jsonb,
                        $2, NULL, $3, 'iglu:legba/finding/jsonschema/1-0-0', $4,
                        'moderate')
                """,
                fid, analyst_id,
                datetime.now(timezone.utc) - timedelta(hours=hours_ago), sig,
            )
    yield pool, analyst_id, sig, ids
    async with pool.acquire() as conn:
        # Un-stamp the probe rows so no later fleet-wide sweep on the shared test
        # DB can cluster them once their head is gone.
        await conn.execute(
            "UPDATE analyst_outputs SET situation_signature = NULL "
            "WHERE analyst_id = $1",
            analyst_id,
        )
        await conn.execute(
            "DELETE FROM analyst_descriptors WHERE descriptor_id = $1", analyst_id,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unlisted_composition_producer_never_reaches_the_supersession_fetch(
    unlisted_producer,
):
    pool, analyst_id, _sig, ids = unlisted_producer
    async with pool.acquire() as conn:
        exclude = await fs.resolve_composition_analyst_ids(conn)
        assert analyst_id in exclude
        assert fs._COMPOSITION_ANALYST_IDS <= exclude
        rows = await fs._fetch_findings(
            conn, lookback_days=30, analyst_id=analyst_id, owner_tenant=None,
            exclude=exclude,
        )
    assert rows == []
    # Control: the literal alone (the old gate) DOES let this producer through.
    async with pool.acquire() as conn:
        leaked = await fs._fetch_findings(
            conn, lookback_days=30, analyst_id=analyst_id, owner_tenant=None,
        )
    assert {r["id"] for r in leaked} == set(ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unlisted_composition_producer_never_mints_a_situation(unlisted_producer):
    """THE ACCEPTANCE, through the real dispatch path: the clustering sweep runs
    and no frame exists for the unlisted producer's composition signature."""
    pool, _analyst_id, sig, _ids = unlisted_producer
    clustering_id = f"situation_clustering_{uuid4().hex[:8]}"
    try:
        await deterministic.run_method(
            [],
            {"sub_handler": "situation_clustering", "analyst_id": clustering_id,
             "run_id": str(uuid4())},
            type("_Deps", (), {"pg_pool": pool})(),
        )
        async with pool.acquire() as conn:
            minted = await conn.fetchval(
                "SELECT count(*) FROM situations "
                "WHERE analyst_id = $1 AND situation_signature LIKE $2",
                clustering_id, f"{sig}%",
            )
        assert minted == 0
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE situations SET status = 'closed' WHERE analyst_id = $1",
                clustering_id,
            )
