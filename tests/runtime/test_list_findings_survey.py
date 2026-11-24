# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F150 — ``list_findings`` survey mode and paging, pinned.

The 2026-11-05 consult dig: all 44 targets had a live desk read, and the
consult saw about twelve. An unscoped ``list_findings`` returned the 30 newest
live findings across the platform, each with its full ~4 KB body, and the
round budget kept about a dozen of them. Survey mode returns one newest live
read per target, digest only. Paging gives both modes ``offset``,
``rows_total`` (the true count) and ``next_offset``.

Postgres is REAL (``migrated_pg``, the no-mocks rule). The session database is
shared with every other file in the run, so each test writes under its own
target ids and reads through a target filter; no assertion depends on rows
another test wrote.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.agency.substrate_read import _findings_paging_kw
from legba.data.stack.llm.tool_rounds import TOOL_SCHEMAS, validate_args
from legba.runtime import substrate_findings_reads as sfr
from legba.runtime.substrate_query_port import (
    PostgresQdrantSubstrateQueryPort,
    critic_folded_findings_sql,
)

_NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    yield pool
    await pool.close()


@pytest.fixture
def port(pg_pool):
    return PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)


def _tid(label: str) -> str:
    return f"f150_{label}_{uuid4().hex[:8]}"


async def _finding(
    pool, *, target_id: str, minutes_ago: float = 0.0, title: str = "t",
    body: str = "", analyst_id: str = "escalation", confidence: float = 0.6,
    severity: str | None = None, superseded_by: UUID | None = None,
) -> UUID:
    oid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, body, confidence, "
            "severity, target_id, analyst_id, produced_at, schema_uri, "
            "superseded_by) VALUES ($1, 'finding', $2, $3, $4, $5, $6, $7, $8, "
            "'iglu:legba/finding/jsonschema/1-0-0', $9)",
            oid, title, body, confidence, severity, target_id, analyst_id,
            _NOW - timedelta(minutes=minutes_ago), superseded_by,
        )
    return oid


async def _critique(pool, finding_id: UUID, score: float) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, body, confidence, "
            "data, analyst_id, produced_at, schema_uri) VALUES ($1, 'critique', "
            "'Faithfulness verify', '', 0.9, $2::jsonb, 'critic', now(), "
            "'iglu:legba/critique/jsonschema/1-0-0')",
            uuid4(),
            json.dumps({"analyzed_output_id": str(finding_id),
                        "overall_score": score}),
        )


# ---------------------------------------------------------------------------
# Survey mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_survey_is_one_row_per_target_even_when_a_target_has_ten_reads(
    port, pg_pool,
):
    busy, quiet = _tid("a_busy"), _tid("b_quiet")
    ids = [
        await _finding(pg_pool, target_id=busy, minutes_ago=10 * i,
                       title=f"busy {i}", body="x" * 1000)
        for i in range(10)
    ]
    q_new = await _finding(pg_pool, target_id=quiet, minutes_ago=30,
                           title="quiet new", body="short body")
    await _finding(pg_pool, target_id=quiet, minutes_ago=90, title="quiet old")

    out = await port.list_findings(mode="survey", target_ids=[quiet, busy])

    assert out["mode"] == "survey"
    assert [r["target_id"] for r in out["rows"]] == sorted([busy, quiet])
    by_target = {r["target_id"]: r for r in out["rows"]}
    assert by_target[busy]["id"] == str(ids[0])  # the newest of the ten
    assert by_target[quiet]["id"] == str(q_new)
    assert out["count"] == 2 and out["rows_total"] == 2
    assert "next_offset" not in out
    assert out["refs"] == [r["id"] for r in out["rows"]]
    assert out["window_hours"] == sfr.SURVEY_DEFAULT_WINDOW_HOURS
    assert "read_document" in out["note"]


@pytest.mark.asyncio
async def test_survey_rows_carry_a_digest_never_the_body(port, pg_pool):
    tid = _tid("digest")
    await _finding(pg_pool, target_id=tid, body="B" * 4000, title="T" * 400)
    (row,) = (await port.list_findings(mode="survey", target_id=tid))["rows"]

    assert "body" not in row
    assert row["digest"] == "B" * (sfr.SURVEY_DIGEST_CHARS - 3) + "..."
    assert row["body_chars"] == 4000 and row["body_truncated"] is True
    assert len(row["title"]) == sfr.SURVEY_TITLE_CHARS
    assert set(row) >= {
        "id", "target_id", "analyst_id", "produced_at", "severity",
        "confidence", "effective_confidence",
    }


def test_digest_drops_the_slice_preamble_and_cuts_at_a_word():
    body = (
        "*As of 5 November 2026; slice covers the trailing 72h; 23 signals.*\n\n"
        "**BLUF:** The legal-diplomatic track remains the driver. " + "word " * 80
    )
    text, cut = sfr.digest(body)
    assert text.startswith("BLUF: The legal-diplomatic track")
    assert cut and text.endswith("word...")
    assert len(text) <= sfr.SURVEY_DIGEST_CHARS
    assert sfr.digest("short body") == ("short body", False)
    # A body that is ONLY preamble keeps it rather than going blank.
    assert sfr.digest("*only a preamble*")[0] == "*only a preamble*"


@pytest.mark.asyncio
async def test_superseded_reads_never_win_the_survey(port, pg_pool):
    tid, gone = _tid("sup"), _tid("gone")
    live = await _finding(pg_pool, target_id=tid, minutes_ago=60, title="live")
    # NEWER than the live head, but superseded: it must not be the survey row.
    await _finding(pg_pool, target_id=tid, minutes_ago=1, title="stale",
                   superseded_by=live)
    # A target whose only reads are superseded has no live head at all.
    await _finding(pg_pool, target_id=gone, minutes_ago=5, superseded_by=live)

    out = await port.list_findings(mode="survey", target_ids=[tid, gone])

    assert [(r["target_id"], r["id"]) for r in out["rows"]] == [(tid, str(live))]
    assert out["rows_total"] == 1


@pytest.mark.asyncio
async def test_survey_pages_by_target_and_rows_total_is_the_true_count(
    port, pg_pool,
):
    targets = sorted(_tid(f"p{i}") for i in range(5))
    for t in targets:
        for k in range(3):
            await _finding(pg_pool, target_id=t, minutes_ago=k)

    first = await port.list_findings(mode="survey", target_ids=targets, limit=2)
    assert [r["target_id"] for r in first["rows"]] == targets[:2]
    assert first["rows_total"] == 5 and first["next_offset"] == 2

    seen = [r["target_id"] for r in first["rows"]]
    off = first["next_offset"]
    while off is not None:
        page = await port.list_findings(
            mode="survey", target_ids=targets, limit=2, offset=off,
        )
        assert page["rows_total"] == 5
        assert page["offset"] == off
        seen += [r["target_id"] for r in page["rows"]]
        off = page.get("next_offset")
    assert seen == targets

    past = await port.list_findings(
        mode="survey", target_ids=targets, limit=2, offset=50,
    )
    assert past["rows"] == [] and past["rows_total"] == 5


@pytest.mark.asyncio
async def test_survey_respects_window_and_analyst_filters(port, pg_pool):
    old, fresh = _tid("old"), _tid("fresh")
    await _finding(pg_pool, target_id=old, minutes_ago=60 * 24 * 10)
    esc = await _finding(pg_pool, target_id=fresh, minutes_ago=30,
                         analyst_id="escalation")
    await _finding(pg_pool, target_id=fresh, minutes_ago=5,
                   analyst_id="energy_security")

    week = await port.list_findings(mode="survey", target_ids=[old, fresh])
    assert [r["target_id"] for r in week["rows"]] == [fresh]

    month = await port.list_findings(
        mode="survey", target_ids=[old, fresh], since_hours=24 * 30,
    )
    assert {r["target_id"] for r in month["rows"]} == {old, fresh}
    assert month["window_hours"] == 720

    desk = await port.list_findings(
        mode="survey", target_ids=[fresh], analyst_id="escalation",
    )
    assert [r["id"] for r in desk["rows"]] == [str(esc)]


@pytest.mark.asyncio
async def test_survey_folds_the_critic(port, pg_pool):
    tid = _tid("critic")
    fid = await _finding(pg_pool, target_id=tid, confidence=0.8)
    await _critique(pg_pool, fid, 0.4)
    (row,) = (await port.list_findings(mode="survey", target_id=tid))["rows"]
    assert row["critic_score"] == pytest.approx(0.4)
    assert row["effective_confidence"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_survey_refuses_the_history_and_scope_flags(port):
    for kw in ({"include_superseded": True}, {"verified_only": True},
               {"believed_as_of": "2026-08-01"}):
        out = await port.list_findings(mode="survey", **kw)
        assert out["rows"] == [] and "survey mode" in out["error"], kw
    bad = await port.list_findings(mode="everything")
    assert bad["rows"] == [] and "mode must be one of" in bad["error"]


# ---------------------------------------------------------------------------
# Paging on the page mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_mode_rows_total_is_the_true_count_under_paging(
    port, pg_pool,
):
    tid = _tid("page")
    live = [await _finding(pg_pool, target_id=tid, minutes_ago=i,
                           title=f"r{i}") for i in range(7)]
    # Superseded rows are not live, so they are not counted.
    for i in range(3):
        await _finding(pg_pool, target_id=tid, minutes_ago=0.5 + i,
                       superseded_by=live[0])

    first = await port.list_findings(target_id=tid, limit=3)
    assert first["count"] == 3 and first["rows_total"] == 7
    assert first["next_offset"] == 3 and "offset" not in first

    second = await port.list_findings(target_id=tid, limit=3, offset=3)
    third = await port.list_findings(target_id=tid, limit=3, offset=6)
    assert second["rows_total"] == 7 and third["rows_total"] == 7
    assert second["next_offset"] == 6 and "next_offset" not in third
    got = [r["id"] for p in (first, second, third) for r in p["rows"]]
    assert got == [str(x) for x in live]  # newest first, no gap, no repeat


@pytest.mark.asyncio
async def test_page_mode_default_shape_is_unchanged(port, pg_pool):
    tid = _tid("default")
    for i in range(2):
        await _finding(pg_pool, target_id=tid, minutes_ago=i, body="full body")
    out = await port.list_findings(target_id=tid)
    assert out["count"] == 2 and out["rows_total"] == 2
    assert "next_offset" not in out and "mode" not in out
    assert all(r["body"] == "full body" for r in out["rows"])


def test_unpaged_sql_carries_no_offset():
    plain = critic_folded_findings_sql("f.kind = 'finding'", 1)
    paged = critic_folded_findings_sql("f.kind = 'finding'", 1, offset_param=2)
    assert "OFFSET" not in plain
    assert "LIMIT $1 OFFSET $2" in paged
    assert paged.replace("OFFSET $2 ", "") == plain


def test_survey_sql_shape():
    sql = sfr.findings_survey_sql(
        "f.kind = 'finding' AND f.superseded_by IS NULL AND f.produced_at >= $1",
        2, 3,
    )
    assert "DISTINCT ON (f.target_id)" in sql
    assert "ORDER BY f.target_id, f.produced_at DESC, f.id DESC" in sql
    assert "LIMIT $2 OFFSET $3" in sql
    assert f"left(s.body, {sfr.SURVEY_HEAD_CHARS})" in sql
    assert "LEFT JOIN LATERAL" not in sql
    assert "IN (SELECT id::text FROM p)" in sql
    count = sfr.rows_total_sql("f.kind = 'finding'", 1)
    assert "ORDER BY f.produced_at DESC" in count and "LIMIT $1" in count


# ---------------------------------------------------------------------------
# The surface the model sees
# ---------------------------------------------------------------------------


def test_tool_schema_declares_survey_and_paging():
    spec = TOOL_SCHEMAS["list_findings"]
    props = spec.json_schema["properties"]
    assert props["mode"]["enum"] == ["page", "survey"]
    assert {"offset", "target_ids"} <= set(props)
    assert "one row per target" in spec.description
    assert "digest only" in spec.description
    assert validate_args(spec, {
        "mode": "survey", "target_ids": ["a", "b"], "offset": "20",
    }) is None
    assert validate_args(spec, {"mode": "everything"}) is not None


def test_pack_handler_forwards_paging_only_when_asked():
    # A plain call is the historical one: limit 20, nothing else.
    assert _findings_paging_kw({}) == {"limit": 20}
    # A survey with no limit takes the port's survey default.
    assert _findings_paging_kw({"mode": "Survey"}) == {"mode": "survey"}
    assert _findings_paging_kw({
        "mode": "survey", "target_ids": "a, b", "offset": "4", "limit": 10,
    }) == {"mode": "survey", "target_ids": ["a", "b"], "offset": 4, "limit": 10}
