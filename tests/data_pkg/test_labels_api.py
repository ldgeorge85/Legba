# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-T4 — labeled reference-set substrate + labels API.

DB-backed against the ISOLATED ``legba_test_<uuid>`` database the data_pkg
``migrated_pg`` fixture creates, migrates to head (so migration 0057 actually
applies via the real runner), and drops at session end — NEVER the live ``legba``
data. Exercises the ``labels_api`` router end-to-end over a real asyncpg pool via
httpx ASGITransport:

  * POST /eval/labels inserts a gold label → 201, returns the stored row;
  * GET /eval/labels?unit_analyst_id=&target_id= reads it back, and the
    correctness-relevant fields (reference_answer, canonical_source_ids grounding,
    unit/target) round-trip exactly;
  * >=10 labels for ONE unit is supported (the table + the (unit, target) index);
  * a malformed grounding id is rejected loudly (never silently mis-grounded);
  * the migration file exists at the next free number + is idempotently guarded.
"""
from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from legba.data.registry.labels_api import _compose_badge, build_labels_router

# asyncio_mode = "auto" (pyproject) auto-marks the async tests; the lone sync
# migration-file test must NOT carry an asyncio mark, so no module-level mark.


# ---------------------------------------------------------------------------
# Minimal deps stub — the router touches ONLY deps.descriptor_registry.pg.acquire()
# ---------------------------------------------------------------------------


class _Reg:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg = pool


class _Deps:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.descriptor_registry = _Reg(pool)


@pytest_asyncio.fixture
async def client(migrated_pg, monkeypatch):
    """A bearer-passing httpx client wired to the labels router over a real pool
    on the isolated migrated test DB."""
    # Dev-mode so require_bearer accepts any/no bearer regardless of the host env.
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")

    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    app = FastAPI()
    app.include_router(build_labels_router(_Deps(pool)), prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t"
    ) as c:
        try:
            yield c
        finally:
            await pool.close()


async def test_post_then_get_roundtrips(client):
    unit = f"unit_corr_{uuid4().hex[:8]}"
    target = "country:US"
    sources = [str(uuid4()), str(uuid4())]
    body = {
        "unit_analyst_id": unit,
        "target_id": target,
        "reference_answer": "US is a nuclear-weapon state (NPT-recognized).",
        "canonical_source_ids": sources,
        "labeled_by": "operator",
    }
    resp = await client.post("/api/v1/eval/labels", json=body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["unit_analyst_id"] == unit
    assert created["target_id"] == target
    assert created["reference_answer"] == body["reference_answer"]
    assert set(created["canonical_source_ids"]) == set(sources)
    assert created["labeled_by"] == "operator"
    assert created["id"]
    assert created["created_at"]

    # Read it back, filtered by (unit, target).
    got = await client.get(
        "/api/v1/eval/labels",
        params={"unit_analyst_id": unit, "target_id": target},
    )
    assert got.status_code == 200, got.text
    labels = got.json()["labels"]
    assert len(labels) == 1
    row = labels[0]
    assert row["id"] == created["id"]
    # Correctness-relevant fields round-trip exactly.
    assert row["reference_answer"] == body["reference_answer"]
    assert set(row["canonical_source_ids"]) == set(sources)
    assert row["unit_analyst_id"] == unit
    assert row["target_id"] == target


async def test_labeled_by_defaults_to_principal(client):
    unit = f"unit_default_{uuid4().hex[:8]}"
    resp = await client.post(
        "/api/v1/eval/labels",
        json={
            "unit_analyst_id": unit,
            "reference_answer": "no target — a meta unit's gold answer",
            "canonical_source_ids": [],
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    # target_id omitted → NULL (a meta / non-target unit); labeled_by stamped from
    # the authenticated principal (dev-mode → "anonymous"), never left empty.
    assert created["target_id"] is None
    assert created["labeled_by"] == "anonymous"
    assert created["canonical_source_ids"] == []


async def test_at_least_ten_labels_for_one_unit(client):
    unit = f"unit_bulk_{uuid4().hex[:8]}"
    n = 12
    for i in range(n):
        resp = await client.post(
            "/api/v1/eval/labels",
            json={
                "unit_analyst_id": unit,
                "target_id": f"country:G{i:02d}",
                "reference_answer": f"gold answer #{i}",
                "canonical_source_ids": [str(uuid4())],
            },
        )
        assert resp.status_code == 201, resp.text

    got = await client.get(
        "/api/v1/eval/labels",
        params={"unit_analyst_id": unit, "limit": 1000},
    )
    assert got.status_code == 200, got.text
    labels = got.json()["labels"]
    assert len(labels) == n >= 10
    # The (unit, target) filter narrows to one specific gold row.
    narrowed = await client.get(
        "/api/v1/eval/labels",
        params={"unit_analyst_id": unit, "target_id": "country:G05"},
    )
    assert narrowed.status_code == 200
    one = narrowed.json()["labels"]
    assert len(one) == 1
    assert one[0]["reference_answer"] == "gold answer #5"


async def test_malformed_grounding_id_rejected(client):
    resp = await client.post(
        "/api/v1/eval/labels",
        json={
            "unit_analyst_id": "unit_bad",
            "reference_answer": "x",
            "canonical_source_ids": ["not-a-uuid"],
        },
    )
    assert resp.status_code == 400, resp.text
    assert "uuid" in resp.text.lower()


def test_migration_file_present_at_next_number_and_idempotent():
    mig_dir = (
        Path(__file__).resolve().parents[2]
        / "src" / "legba" / "data" / "migrations"
    )
    target = mig_dir / "0057_unit_reference_labels.sql"
    assert target.exists(), f"missing migration: {target}"

    # 0057 was the next free number when this migration was added (head 0056
    # then). Later migrations (0058+) legitimately follow, so assert 0057 exists
    # and followed 0056 with no gap — NOT that it is the GLOBAL head, which
    # regressed this test every time a new migration landed (matches the
    # integrity-sweep tests' 0056/0059 pattern).
    nums = sorted(
        int(m.group(1))
        for p in mig_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")
        if (m := re.match(r"(\d{4})_", p.name))
    )
    assert 57 in nums
    assert 56 in nums
    assert nums.count(57) == 1, "duplicate 0057_ migration number"

    sql = target.read_text()
    # Idempotent guards (re-apply / fresh cold-start safe) + the required columns.
    assert "CREATE TABLE IF NOT EXISTS public.unit_reference_labels" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_unit_reference_labels_unit_target" in sql
    for col in (
        "unit_analyst_id",
        "target_id",
        "reference_answer",
        "canonical_source_ids uuid[]",
        "labeled_by",
        "created_at",
    ):
        assert col in sql, f"migration missing column/marker: {col}"


# ---------------------------------------------------------------------------
# P2-T6 — eval scoreboard badge (the honest "no invented number" contract)
# ---------------------------------------------------------------------------


def test_badge_labeled_unit_shows_both_scores():
    """A labeled+scored unit shows faithfulness AND correctness with the n."""
    badge = _compose_badge(0.90, 0.78, 12)
    assert badge == "verified | faithfulness 0.90 | correctness 0.78 (n=12)"


def test_badge_unlabeled_unit_says_unmeasured_not_a_number():
    """The T6 honesty contract: 0 labels → correctness is reported as
    'unmeasured (N labels)', NEVER an invented number (and never 0.00)."""
    badge = _compose_badge(0.45, None, 0)
    assert badge == "verified | faithfulness 0.45 | unmeasured (0 labels)"
    assert "correctness" not in badge  # no fabricated correctness figure


def test_badge_never_verified_unit_is_still_honest():
    """No faithfulness yet (None) + no labels → just the honest unmeasured tail."""
    assert _compose_badge(None, None, 0) == "verified | unmeasured (0 labels)"


def test_badge_real_zero_correctness_is_shown_not_hidden():
    """A genuine 0.0 (the unit cited NONE of the canonical evidence) is a real
    signal and IS shown — only an UNMEASURED correctness is suppressed."""
    badge = _compose_badge(0.6, 0.0, 3)
    assert badge == "verified | faithfulness 0.60 | correctness 0.00 (n=3)"


async def test_eval_scores_empty_when_scorer_never_ran(client):
    """With no unit_correctness_scorer output present, /eval/scores returns an
    honest scoreboard — no scored_at, no invented unit rows. P2-5: a unit row
    MAY still appear without a scorer run, but only when it carries REAL
    operator gold-set verdicts (the live correctness_labels overlay; the
    session-shared test DB may hold some from the goldset-loop tests) — never
    a fabricated scorer-side number."""
    resp = await client.get("/api/v1/eval/scores")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scored_at"] is None
    for u in body["units"]:
        # Every row present is operator-label-backed, and the scorer-side keys
        # stay honestly unmeasured.
        assert u["n_operator_labels"] > 0
        assert u["correctness_vs_reference"] is None
        assert u["faithfulness"] is None
        assert u["n_labeled"] == 0
