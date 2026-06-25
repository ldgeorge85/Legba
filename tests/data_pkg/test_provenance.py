# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit + integration tests for legba.data.provenance."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest

from legba.data.config import PostgresConfig
from legba.data.provenance import (
    AnalystContext,
    LEGACY_TARGET_SENTINEL,
    SchemaUri,
    TargetContext,
    ZERO_HASH,
    append_derived_from,
    canonical_json,
    compute_receipt_hash,
    from_analyst,
    from_target,
    is_valid_schema_uri,
    legacy_provenance,
    parse_schema_uri,
    query_ancestors,
    sha256_canonical,
)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_schema_uri_iglu_round_trip():
    raw = "iglu:legba/signal/jsonschema/2-0-0"
    parsed = parse_schema_uri(raw)
    assert parsed.family == "signal"
    assert parsed.major == 2 and parsed.minor == 0 and parsed.patch == 0
    assert parsed.render() == raw


def test_schema_uri_bare_round_trip():
    raw = "legba/target/3.1.4"
    parsed = parse_schema_uri(raw)
    assert parsed.family == "target"
    assert (parsed.major, parsed.minor, parsed.patch) == (3, 1, 4)
    assert parsed.render() == raw


def test_schema_uri_bumps():
    s = SchemaUri("event", 2, 0, 0, "iglu")
    assert s.bump_patch().render().endswith("2-0-1")
    assert s.bump_minor().render().endswith("2-1-0")
    assert s.bump_major().render().endswith("3-0-0")


def test_schema_uri_invalid():
    assert not is_valid_schema_uri("not a uri")
    with pytest.raises(ValueError):
        parse_schema_uri("not a uri")


def test_from_target_sets_fields():
    ctx = TargetContext(target_id="india_energy", target_version="abc123")
    prov = from_target(ctx, schema_uri="iglu:legba/signal/jsonschema/2-0-0")
    assert prov.target_id == "india_energy"
    assert prov.target_version == "abc123"
    assert prov.analyst_id is None
    assert prov.run_id is None
    assert prov.derived_from == []


def test_from_analyst_sets_fields():
    run = uuid4()
    ctx = AnalystContext(
        analyst_id="critic_v2",
        analyst_version="hhhh",
        run_id=run,
        target_id="india_energy",
        target_version="abc123",
    )
    prov = from_analyst(
        ctx,
        schema_uri="iglu:legba/finding/jsonschema/1-0-0",
        derived_from=[uuid4(), uuid4()],
    )
    assert prov.analyst_id == "critic_v2"
    assert prov.run_id == run
    assert len(prov.derived_from) == 2


def test_legacy_provenance_uses_sentinel():
    prov = legacy_provenance("iglu:legba/signal/jsonschema/2-0-0")
    assert prov.target_id == LEGACY_TARGET_SENTINEL
    assert prov.target_version == "legacy"


def test_append_derived_from_dedupes():
    a, b, c = uuid4(), uuid4(), uuid4()
    result = append_derived_from([a, b], [b, c, a])
    assert result == [a, b, c]


def test_canonical_json_sorted_keys():
    data = {"b": 1, "a": 2, "c": {"y": 1, "x": 2}}
    bytes_out = canonical_json(data)
    assert bytes_out == b'{"a":2,"b":1,"c":{"x":2,"y":1}}'


def test_sha256_canonical_deterministic():
    a = sha256_canonical({"x": 1, "y": 2})
    b = sha256_canonical({"y": 2, "x": 1})
    assert a == b
    assert len(a) == 64


def test_compute_receipt_hash_chain():
    run_id = uuid4()
    end = datetime.now(tz=timezone.utc)
    h1 = compute_receipt_hash(
        run_id=run_id,
        analyst_id="a",
        analyst_version="v",
        input_row_refs=[],
        prompt_module_hash=None,
        prompt_rendered=None,
        output_row_refs=[],
        output_payload={"r": 1},
        run_ended_at=end,
        prev_receipt_hash=None,
    )
    h2 = compute_receipt_hash(
        run_id=run_id,
        analyst_id="a",
        analyst_version="v",
        input_row_refs=[],
        prompt_module_hash=None,
        prompt_rendered=None,
        output_row_refs=[],
        output_payload={"r": 1},
        run_ended_at=end,
        prev_receipt_hash=h1,
    )
    assert h1 != h2  # chain hash changes with prev
    assert len(h1) == 64 and len(h2) == 64
    assert ZERO_HASH == "0" * 64


# ---------------------------------------------------------------------------
# Integration — lineage query against migrated schema
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="retired: builds signal→signal lineage on `signals` and calls query_ancestors(conn, 'signals', ...), which selects the dropped target_id/analyst_id/produced_at columns (pre-pivot target-owned signal model, migration 0024) + INSERTs the dropped signals.data/title/target_id columns — see PIVOT_BUILD_PLAN")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_lineage_query_round_trip(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        # Build a 3-step lineage: signal A → finding B (derived_from=[A]) → meta C (=[B])
        a_id, b_id, c_id = uuid4(), uuid4(), uuid4()
        await conn.execute(
            """
            INSERT INTO signals
              (id, data, title, target_id, target_version, produced_at,
               derived_from, schema_uri)
            VALUES ($1, '{}'::jsonb, 'a', 'br_energy', 'tv', NOW(), '{}'::UUID[],
                    'iglu:legba/signal/jsonschema/2-0-0')
            """,
            a_id,
        )
        await conn.execute(
            """
            INSERT INTO signals
              (id, data, title, target_id, target_version, analyst_id,
               analyst_version, produced_at, derived_from, schema_uri, run_id)
            VALUES ($1, '{}'::jsonb, 'b', 'br_energy', 'tv', 'analyst_b', 'av',
                    NOW(), $2, 'iglu:legba/signal/jsonschema/2-0-0', NULL)
            """,
            b_id, [a_id],
        )
        await conn.execute(
            """
            INSERT INTO signals
              (id, data, title, target_id, target_version, analyst_id,
               analyst_version, produced_at, derived_from, schema_uri, run_id)
            VALUES ($1, '{}'::jsonb, 'c', NULL, NULL, 'analyst_c', 'av',
                    NOW(), $2, 'iglu:legba/signal/jsonschema/2-0-0', NULL)
            """,
            c_id, [b_id],
        )

        ancestors = await query_ancestors(conn, "signals", c_id)
        ids = {a["id"] for a in ancestors}
        assert c_id in ids
        assert b_id in ids
        assert a_id in ids
    finally:
        await conn.close()
