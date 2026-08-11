# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-5 — correctness gold-set labeling loop (worksheet + label upsert + scoreboard n).

DB-backed against the ISOLATED ``legba_test_<uuid>`` database the data_pkg
``migrated_pg`` fixture creates (so migration 0096 applies via the real
runner) — NEVER the live ``legba`` data. Exercises ``goldset_api`` +
``labels_api`` end-to-end over a real asyncpg pool via httpx ASGITransport:

  * GET /v3/eval/goldset/worksheet — shape, week pinning (two reads → the same
    membership + order), label state + labeled_count/all_labeled progression;
  * POST /v3/eval/goldset/label — upsert semantics (one verdict per finding,
    latest wins, created_at pinned to the FIRST label), server-side
    finding_snapshot (supersession can't orphan the judgment), closed
    vocabulary, 404/400 honesty;
  * GET /eval/scores — the operator aggregate overlay: n grows live as labels
    land, without any scorer run, and the badge says so;
  * migration 0096 exists + is idempotently guarded;
  * the extended badge composer stays backward-compatible.
"""
from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest_asyncio
from fastapi import FastAPI

from legba.data.registry.goldset_api import build_goldset_router
from legba.data.registry.labels_api import _compose_badge, build_labels_router

# asyncio_mode = "auto" (pyproject) auto-marks the async tests.


class _Reg:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg = pool


class _Deps:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.descriptor_registry = _Reg(pool)


@pytest_asyncio.fixture
async def gs(migrated_pg, monkeypatch):
    """(client, pool) wired to the goldset router (under /api/v1/v3, as
    server.py mounts it) + the labels router (under /api/v1, for /eval/scores)
    over a real pool on the isolated migrated test DB."""
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")

    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    app = FastAPI()
    app.include_router(build_goldset_router(_Deps(pool)), prefix="/api/v1/v3")
    app.include_router(build_labels_router(_Deps(pool)), prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            yield c, pool
        finally:
            await pool.close()


async def _seed_finding(
    pool: asyncpg.Pool,
    *,
    unit: str,
    target: str = "country_g20_us",
    title: str = "US escalation posture read",
    faithfulness: float | None = 0.9,
) -> str:
    """Insert one verified head finding (+ its faithfulness critique) and
    return its id. Shapes mirror the live writer: the resolved citations nest
    under data->'data'->'citations'; the critique carries analyzed_output_id +
    overall_score and the 'Faithfulness verify' title."""
    sig = str(uuid4())
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO analyst_outputs
                (kind, title, body, confidence, data, target_id, analyst_id,
                 schema_uri)
            VALUES ('finding', $1, $2, 0.8, $3::jsonb, $4, $5,
                    'legba://finding/1-0-0')
            RETURNING id::text AS id
            """,
            title,
            "The posture hardened this week [1].",
            (
                '{"tags": ["verified"], "data": {"citations": '
                '[{"marker": "[1]", "signal_id": "%s", '
                '"title": "wire story", "source": "test-src"}]}}' % sig
            ),
            target,
            unit,
        )
        fid = row["id"]
        if faithfulness is not None:
            await conn.execute(
                """
                INSERT INTO analyst_outputs
                    (kind, title, body, confidence, data, target_id, analyst_id,
                     schema_uri)
                VALUES ('critique', $1, '', $2, $3::jsonb, $4, $5,
                        'legba://critique/1-0-0')
                """,
                f"Faithfulness verify (score {faithfulness:.2f})",
                faithfulness,
                (
                    '{"analyzed_output_id": "%s", "overall_score": %s}'
                    % (fid, faithfulness)
                ),
                target,
                unit,
            )
    return fid


# ---------------------------------------------------------------------------
# The worksheet loop — shape, pinning, label state, honest exhaustion
# ---------------------------------------------------------------------------


async def test_worksheet_loop_shape_pinning_and_progress(gs):
    client, pool = gs
    # The test DB is SESSION-shared across data_pkg modules: start this loop on
    # a clean candidate slate (no pinned week, no stray bounded-unit findings
    # from earlier modules) so the pin captures exactly what this test seeds.
    from legba.data.registry.goldset_sampling import DEFAULT_UNITS

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM goldset_week_samples")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = ANY($1::text[])",
            list(DEFAULT_UNITS),
        )
    # Seed verified findings in two real bounded units BEFORE the first read
    # (the first worksheet GET pins the week's membership).
    fids = []
    for unit, faith in (
        ("escalation", 0.9),
        ("escalation", 0.55),
        ("energy_security", 0.85),
    ):
        fids.append(await _seed_finding(pool, unit=unit, faithfulness=faith))

    resp = await client.get("/api/v1/v3/eval/goldset/worksheet")
    assert resp.status_code == 200, resp.text
    ws = resp.json()
    # Envelope shape.
    assert re.fullmatch(r"\d{4}-W\d{2}", ws["week"])
    assert ws["week_started_at"] and ws["next_sample_at"]
    assert ws["sample_size"] == len(ws["items"]) == 3
    assert ws["labeled_count"] == 0
    assert ws["all_labeled"] is False
    # Item shape — the reading kit's inputs + honest state.
    item = ws["items"][0]
    for key in (
        "finding_id", "unit", "target_id", "title", "body", "data",
        "citations", "faithfulness", "produced_at", "superseded", "label",
    ):
        assert key in item, f"worksheet item missing {key}"
    assert item["label"] is None
    assert item["superseded"] is False
    assert item["citations"] and item["citations"][0]["marker"] == "[1]"
    # data carries the nested envelope the UI's extractCitations reads.
    assert item["data"]["data"]["citations"][0]["signal_id"]
    # Per-unit coverage: both seeded units appear.
    assert {i["unit"] for i in ws["items"]} == {"escalation", "energy_security"}

    # PINNING — a second read (even after new candidates appear) returns the
    # SAME membership in the SAME order: same week → same sample, hard.
    await _seed_finding(pool, unit="military_posture")
    again = (await client.get("/api/v1/v3/eval/goldset/worksheet")).json()
    assert [i["finding_id"] for i in again["items"]] == [
        i["finding_id"] for i in ws["items"]
    ]

    # Label one item → the worksheet shows the saved state, count moves.
    first = ws["items"][0]["finding_id"]
    lab = await client.post(
        "/api/v1/v3/eval/goldset/label",
        json={"finding_id": first, "label": "correct", "rationale": "checked"},
    )
    assert lab.status_code == 200, lab.text
    ws2 = (await client.get("/api/v1/v3/eval/goldset/worksheet")).json()
    labeled = next(i for i in ws2["items"] if i["finding_id"] == first)
    assert labeled["label"]["label"] == "correct"
    assert labeled["label"]["rationale"] == "checked"
    assert ws2["labeled_count"] == 1
    assert ws2["all_labeled"] is False

    # Label the rest → the honest exhausted state the panel renders
    # ("all labeled — next sample Monday").
    for i in ws2["items"]:
        if i["label"] is None:
            r = await client.post(
                "/api/v1/v3/eval/goldset/label",
                json={"finding_id": i["finding_id"], "label": "partially_correct"},
            )
            assert r.status_code == 200
    done = (await client.get("/api/v1/v3/eval/goldset/worksheet")).json()
    assert done["labeled_count"] == done["sample_size"] == 3
    assert done["all_labeled"] is True


# ---------------------------------------------------------------------------
# Label upsert semantics + snapshot
# ---------------------------------------------------------------------------


async def test_label_upsert_latest_wins_created_at_pinned(gs):
    client, pool = gs
    fid = await _seed_finding(pool, unit="narrative_coordination")

    a = await client.post(
        "/api/v1/v3/eval/goldset/label",
        json={"finding_id": fid, "label": "incorrect", "rationale": "wrong actor"},
    )
    assert a.status_code == 200, a.text
    first = a.json()
    assert first["label"] == "incorrect"
    assert first["unit_analyst_id"] == "narrative_coordination"
    # labeled_by defaults to the authenticated principal (dev-mode "anonymous").
    assert first["labeled_by"] == "anonymous"

    b = await client.post(
        "/api/v1/v3/eval/goldset/label",
        json={"finding_id": fid, "label": "partially_correct", "labeled_by": "lewis"},
    )
    assert b.status_code == 200
    second = b.json()
    # Upsert: same row (one verdict per finding), verdict + labeler move,
    # created_at (the FIRST label time, the weekly-exclusion key) is pinned.
    assert second["id"] == first["id"]
    assert second["label"] == "partially_correct"
    assert second["labeled_by"] == "lewis"
    assert second["created_at"] == first["created_at"]
    assert second["labeled_at"] >= first["labeled_at"]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT label, finding_snapshot FROM correctness_labels "
            "WHERE finding_id = $1",
            __import__("uuid").UUID(fid),
        )
    assert len(rows) == 1
    snap = rows[0]["finding_snapshot"]
    if isinstance(snap, str):
        import json

        snap = json.loads(snap)
    # The snapshot pins what was judged: title + claims + citations.
    assert snap["title"] == "US escalation posture read"
    assert "[1]" in snap["body"]
    assert snap["citations"][0]["marker"] == "[1]"
    assert snap["unit_analyst_id"] == "narrative_coordination"


async def test_label_honesty_unknown_finding_bad_uuid_bad_vocab(gs):
    client, _pool = gs
    missing = await client.post(
        "/api/v1/v3/eval/goldset/label",
        json={"finding_id": str(uuid4()), "label": "correct"},
    )
    assert missing.status_code == 404
    bad_uuid = await client.post(
        "/api/v1/v3/eval/goldset/label",
        json={"finding_id": "not-a-uuid", "label": "correct"},
    )
    assert bad_uuid.status_code == 400
    bad_vocab = await client.post(
        "/api/v1/v3/eval/goldset/label",
        json={"finding_id": str(uuid4()), "label": "sort_of_right"},
    )
    assert bad_vocab.status_code == 422  # closed vocabulary, pydantic-enforced


# ---------------------------------------------------------------------------
# Scoreboard — operator n grows live, no scorer run required
# ---------------------------------------------------------------------------


async def test_scoreboard_operator_n_grows_as_labels_land(gs):
    client, pool = gs
    # A unit id unique to this test so parallel-seeded labels can't pollute it.
    unit = f"unit_gs_{uuid4().hex[:8]}"
    fids = [
        await _seed_finding(pool, unit=unit, title=f"read {i}") for i in range(3)
    ]

    async def unit_row():
        resp = await client.get("/api/v1/eval/scores")
        assert resp.status_code == 200, resp.text
        return next(
            (u for u in resp.json()["units"] if u["unit"] == unit), None
        )

    # Before any verdict: the unit has no row (no scorer run, no labels).
    assert await unit_row() is None

    # correct → n=1, operator 1.00; the badge carries the growing n.
    await client.post(
        "/api/v1/v3/eval/goldset/label",
        json={"finding_id": fids[0], "label": "correct"},
    )
    row = await unit_row()
    assert row is not None
    assert row["n_operator_labels"] == 1
    assert row["n_operator_scored"] == 1
    assert row["correctness_operator"] == 1.0
    # M-1 tiny-n: a single verdict is shown WITH its n and marked indicative —
    # never withheld, and never renderable as a measured rate.
    assert "operator 1.00 (n=1, indicative)" in row["badge"]
    assert row["operator_sufficient"] is False
    assert row["operator_mix"]["correct"] == 1
    assert "indicative" in (row["operator_status"] or "")
    # The deterministic reference leg stays honestly unmeasured — segregated.
    assert row["correctness_vs_reference"] is None
    assert "unmeasured" in row["badge"]

    # + incorrect → n=2, mean 0.5.
    await client.post(
        "/api/v1/v3/eval/goldset/label",
        json={"finding_id": fids[1], "label": "incorrect"},
    )
    row = await unit_row()
    assert row["n_operator_scored"] == 2
    assert row["correctness_operator"] == 0.5
    assert "operator 0.50 (n=2, indicative)" in row["badge"]
    assert row["operator_mix"]["incorrect"] == 1

    # + unresolvable → counted as a label, EXCLUDED from the score + its n.
    await client.post(
        "/api/v1/v3/eval/goldset/label",
        json={"finding_id": fids[2], "label": "unresolvable"},
    )
    row = await unit_row()
    assert row["n_operator_labels"] == 3
    assert row["n_operator_scored"] == 2
    assert row["correctness_operator"] == 0.5
    assert "operator 0.50 (n=2, indicative)" in row["badge"]
    # The excluded verdict is REPORTED in the mix, never silently dropped.
    assert row["operator_mix"]["unresolvable"] == 1


# ---------------------------------------------------------------------------
# Badge composer — the P2-5 segment is additive; legacy badges are unchanged
# ---------------------------------------------------------------------------


def test_badge_operator_segment_appends_after_reference():
    """M-1 — the operator segment carries its own n and, below the axis floor,
    says so. The gold set does not scale by construction, so 'n=6' without a
    qualifier would invite exactly the reading the floor exists to prevent."""
    from legba.data import correctness_axis

    badge = _compose_badge(0.9, 0.78, 12, operator_correctness=0.75, n_operator_scored=6, n_operator_labels=7)
    assert badge == (
        "verified | faithfulness 0.90 | correctness 0.78 (n=12) "
        "| operator 0.75 (n=6, indicative)"
    )
    # At or above the floor the qualifier drops — the reading has earned it.
    at_floor = _compose_badge(
        0.9, 0.78, 12,
        operator_correctness=0.62,
        n_operator_scored=correctness_axis.MIN_UNIT_LABELS,
        n_operator_labels=correctness_axis.MIN_UNIT_LABELS,
    )
    assert at_floor.endswith(
        f"operator 0.62 (n={correctness_axis.MIN_UNIT_LABELS})"
    )
    assert "indicative" not in at_floor


def test_badge_all_unresolvable_is_said_not_scored():
    badge = _compose_badge(0.9, None, 0, operator_correctness=None, n_operator_scored=0, n_operator_labels=3)
    assert badge.endswith("operator unresolved (3 labels)")
    assert "operator 0" not in badge  # no fabricated operator number


def test_badge_without_operator_labels_is_byte_identical_to_legacy():
    assert _compose_badge(0.45, None, 0) == (
        "verified | faithfulness 0.45 | unmeasured (0 labels)"
    )


# ---------------------------------------------------------------------------
# Migration 0096 — present + idempotently guarded (the 0057 test's pattern)
# ---------------------------------------------------------------------------


def test_migration_0096_present_and_idempotent():
    mig_dir = (
        Path(__file__).resolve().parents[2] / "src" / "legba" / "data" / "migrations"
    )
    target = mig_dir / "0096_correctness_labels.sql"
    assert target.exists(), f"missing migration: {target}"

    nums = [
        int(m.group(1))
        for p in mig_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")
        if (m := re.match(r"(\d{4})_", p.name))
    ]
    assert nums.count(96) == 1, "duplicate 0096_ migration number"

    sql = target.read_text()
    assert "CREATE TABLE IF NOT EXISTS public.correctness_labels" in sql
    assert "CREATE TABLE IF NOT EXISTS public.goldset_week_samples" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_correctness_labels_unit" in sql
    for marker in (
        "finding_id",
        "unit_analyst_id",
        "label",
        "rationale",
        "labeled_by",
        "labeled_at",
        "created_at",
        "finding_snapshot",
        "'correct', 'partially_correct', 'incorrect', 'unresolvable'",
    ):
        assert marker in sql, f"migration missing column/marker: {marker}"
