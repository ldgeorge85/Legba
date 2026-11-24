# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SW-2 — the HISTORICAL SERIES block's per-run WITNESS, on the binding path.

The 7g-2 pilot granted ``offer_history`` to the energy unit on four desks and a
week of runs cited zero observations, while the trace kept 32k of a ~170k-char
prompt — so whether the block was offered, resolved the desk, returned rows or
rendered could not be read back. This file drives the path the actor drives:

    descriptor ``method.options`` → ``_read_substrate_slice`` (the X-1 kind
    resolution + the grounding gather) → the REAL ``collection_descriptors`` /
    ``observations`` SQL on the migrated test database → ``run_method`` (the
    partition, render, receipts and citation resolution) → ``write_finding``,
    read back on the real driver.

Only the signal / memory reads are served by a stand-in (they are not what is
under test and would need a whole corpus); every statement that touches the
holding goes to Postgres.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.history_grounding import HISTORY_DETAIL_RECEIPT_KEY
from legba.runtime.actor_substrate_slice import _read_substrate_slice

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_TAG = f"tsw2_{uuid4().hex[:8]}"
_COLLECTION = f"collection.{_TAG}"
_NAMED_DESK = f"country_watch_ua_{_TAG}"
_UNNAMED_DESK = f"country_watch_zz_{_TAG}"

_DESCRIPTOR = """
    INSERT INTO collection_descriptors
        (descriptor_id, version, schema_uri, abstraction_level, state, owner,
         name, body, is_head, origin_shape, origin_class, licence_class,
         collection_version)
    VALUES ($1, $2, 'legba/collection/1.0.0', 'L0', 'loaded', 'test_sw2',
            'pilot under test', $3::jsonb, TRUE, 'archive_only', 'archive',
            'public', 'v1')
"""

_INSERT = """
    INSERT INTO observations
        (collection_id, series_id, subject_kind, subject, valid_from,
         valid_to, record_time, value, unit, source_url, sha256,
         origin_class, provenance)
    VALUES ($1, $2, 'country', 'UA', $3, $4, $5, $6, 'tbpd',
            'https://example.invalid/eia.json', 'deadbeef', 'archive',
            $7::jsonb)
"""


@pytest_asyncio.fixture
async def pg(migrated_pg):
    conn = await asyncpg.connect(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
    )
    body = json.dumps({
        "subjects": [{"desk": _NAMED_DESK, "subject": "UA", "name": "Ukraine"}],
        "subject_kind": "country",
    })
    await conn.execute(_DESCRIPTOR, _COLLECTION, "0" * 64, body)
    prov = json.dumps({
        "provider": "eia", "indicator_name": "Crude oil production",
        "licence_class": "public",
    })
    for series, year, value in (
        ("eia.crude_oil_production_tbpd", 2024, "41.5"),
        ("eia.crude_oil_production_tbpd", 2025, "40.2"),
        ("eia.refined_products_consumption_tbpd", 2025, "199.0"),
    ):
        await conn.execute(
            _INSERT, _COLLECTION, series, date(year, 1, 1), date(year, 12, 31),
            datetime(year + 1, 6, 1, tzinfo=timezone.utc), Decimal(value), prov,
        )
    try:
        yield conn
    finally:
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = $1", f"energy_{_TAG}"
        )
        await conn.execute(
            "DELETE FROM observations WHERE collection_id = $1", _COLLECTION
        )
        await conn.execute(
            "DELETE FROM collection_descriptors WHERE descriptor_id = $1",
            _COLLECTION,
        )
        await conn.close()


class _HybridConn:
    """The slice reader's connection: the HOLDING reads go to Postgres, the
    signal / memory reads to a one-signal stand-in."""

    def __init__(self, real: asyncpg.Connection) -> None:
        self._real = real
        self.queries: list[str] = []

    async def fetchrow(self, _query: str, *_params: Any) -> dict[str, Any]:
        return {"body": json.dumps({"sources": [], "scope": {"geo": ["UA"]}})}

    async def fetch(self, query: str, *params: Any) -> list[Any]:
        self.queries.append(query)
        if "collection_descriptors" in query or "FROM observations" in query:
            return await self._real.fetch(query, *params)
        if "FROM signals" in query:
            return [{
                "id": uuid4(), "source_id": "s1", "source_version": None,
                "canonical_url": "https://example.test/1",
                "payload": {"title": "Strikes on the grid", "summary": "Power cuts."},
                "language": "en", "geo": ["UA"], "tags": [], "fetched_at": None,
                "derived_from": None, "entity_classes": [],
                "source_credibility": None, "modality": None, "salience": None,
            }]
        if "FROM situations" in query:
            # One open frame, so EVERY run here has a grounding section and
            # therefore receipts — the ungranted run included.
            return [{
                "id": uuid4(), "name": "Ukraine - grid campaign",
                "status": "active", "intensity_score": 50.0, "event_count": 3,
                "last_event_at": None, "opened_at": None, "age_days": 4.0,
                "target_id": "desk",
            }]
        return []

    def touched_the_holding(self) -> bool:
        return any(
            "collection_descriptors" in q or "FROM observations" in q
            for q in self.queries
        )


def _descriptor(options: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        identity=SimpleNamespace(id=f"energy_{_TAG}", kind="inline_target"),
        subscription=SimpleNamespace(
            substrate={}, targets=SimpleNamespace(time_window="72h"),
        ),
        method=SimpleNamespace(options=options),
    )


class _LLM:
    subprovider = "openai"

    def __init__(self, body: str) -> None:
        self.body = body
        self.user_prompt = ""

    async def chat_complete(self, messages: Any, **_kwargs: Any):
        for m in messages or []:
            if m.get("role") == "user":
                self.user_prompt = m.get("content", "")
        return SimpleNamespace(
            content=json.dumps({
                "title": "Energy pressure", "body": self.body,
                "confidence": 0.6, "evidence": ["1"],
                "tags": ["severity:elevated"],
            }),
            usage=SimpleNamespace(
                prompt_tokens=10, completion_tokens=5, reasoning_tokens=0,
            ),
        )


async def _drive(pg, *, desk: str, options: dict[str, Any], body: str):
    from legba.data.analysts.inline_target import InlineTargetDeps, run_method

    conn = _HybridConn(pg)
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor(options), target_filter=desk,
    )
    llm = _LLM(body)
    result = await run_method(
        rows,
        {"target_id": desk, "analyst_id": f"energy_{_TAG}"},
        InlineTargetDeps(llm=llm),
    )
    return result, llm, conn


def _step(result, **match: Any) -> dict[str, Any] | None:
    for s in result.intermediate_steps:
        if all(s.get(k) == v for k, v in match.items()):
            return s
    return None


async def test_a_named_desk_gets_the_block_the_receipt_and_a_cited_observation(pg):
    # One signal → [1]; the open-situation register → [2]; the history lines
    # are [3] and [4] (two series, latest period each).
    body = (
        "Ukrainian crude output was 40.2 thousand barrels per day in 2025 [3], "
        "and strikes cut power this week [1]."
    )
    result, llm, conn = await _drive(
        pg, desk=_NAMED_DESK, options={"offer_history": True}, body=body,
    )

    assert conn.touched_the_holding()
    assert "HISTORICAL SERIES — 2 curated historical series" in llm.user_prompt
    assert "(historical: valid 2025..2025, recorded 2026-06)" in llm.user_prompt

    expected = {
        "offered": True, "subjects_resolved": 1, "series_rows": 2,
        "rendered": True, "reason": "rendered",
    }
    orient = _step(result, phase="orient")
    ground = _step(result, kind="desk_grounding_blocks")
    assert orient["grounding_history_ref"] == 1
    assert orient[HISTORY_DETAIL_RECEIPT_KEY] == expected
    assert ground["grounding_history_ref"] == 1
    assert ground[HISTORY_DETAIL_RECEIPT_KEY] == expected
    assert ground["start_ordinal"] == 2
    assert ground["blocks"] == 2

    data = result.finding.data
    assert data["grounding_history_ref"] == 1
    assert data[HISTORY_DETAIL_RECEIPT_KEY] == expected
    cited = {c["marker"]: c for c in data["citations"]}
    assert cited["[3]"]["ref_kind"] == "observation"
    assert cited["[3]"]["observation"]["series_id"] == "eia.crude_oil_production_tbpd"
    assert cited["[3]"]["observation"]["value"] == "40.2"

    # PERSISTED — written through the substrate writer and read back on the
    # real driver, not asserted on the in-memory payload alone.
    from legba.data.outputs.substrate import write_finding

    row_id = await write_finding(
        pg, _NAMED_DESK, f"energy_{_TAG}", result.finding, result.derived_from,
    )
    stored = await pg.fetchrow(
        "SELECT data->'data'->'grounding_history_detail' AS detail, "
        "data->'data'->>'grounding_history_ref' AS ref, "
        "(SELECT count(*) FROM jsonb_array_elements(data->'data'->'citations') c "
        " WHERE c->>'ref_kind' = 'observation') AS observations "
        "FROM analyst_outputs WHERE id = $1",
        row_id,
    )
    assert json.loads(stored["detail"]) == expected
    assert stored["ref"] == "1"
    assert stored["observations"] == 1


async def test_a_desk_the_holding_does_not_name_says_no_subjects(pg):
    result, llm, conn = await _drive(
        pg, desk=_UNNAMED_DESK, options={"offer_history": True},
        body="Strikes cut power [1].",
    )
    assert conn.touched_the_holding(), "the grant fired the desk resolution"
    assert "HISTORICAL SERIES" not in llm.user_prompt
    detail = {
        "offered": True, "subjects_resolved": 0, "series_rows": 0,
        "rendered": False, "reason": "no_subjects",
    }
    orient = _step(result, phase="orient")
    assert orient["grounding_history_ref"] == 0
    assert orient[HISTORY_DETAIL_RECEIPT_KEY] == detail
    assert result.finding.data["grounding_history_ref"] == 0
    assert result.finding.data[HISTORY_DETAIL_RECEIPT_KEY] == detail
    ground = _step(result, kind="desk_grounding_blocks")
    assert ground["blocks"] == 1, "only the register rendered"
    assert ground[HISTORY_DETAIL_RECEIPT_KEY] == detail


async def test_ungranted_says_not_offered_and_its_prompt_did_not_move_a_byte(pg):
    body = "Strikes cut power [1]."
    ungranted, llm_off, conn_off = await _drive(
        pg, desk=_NAMED_DESK, options={}, body=body,
    )
    granted_empty, llm_empty, _ = await _drive(
        pg, desk=_UNNAMED_DESK, options={"offer_history": True}, body=body,
    )
    assert not conn_off.touched_the_holding(), "an ungranted run costs nothing"
    assert "HISTORICAL SERIES" not in llm_off.user_prompt
    # The flip-kit bar: an ungranted prompt and a granted-but-empty prompt are
    # the SAME BYTES once the desk id (the only input that differs) is equal.
    assert llm_off.user_prompt.replace(_NAMED_DESK, "DESK") == (
        llm_empty.user_prompt.replace(_UNNAMED_DESK, "DESK")
    )
    # The receipt is on the trace — the run had a grounding section, so it
    # carries every receipt key, and the history one says WHY it is absent...
    orient = _step(ungranted, phase="orient")
    assert orient["grounding_situations_ref"] == 1
    assert orient["grounding_history_ref"] == 0
    assert orient[HISTORY_DETAIL_RECEIPT_KEY] == {
        "offered": False, "subjects_resolved": None, "series_rows": None,
        "rendered": False, "reason": "not_offered",
    }
    # ...and an ungranted unit's FINDING carries nothing new.
    assert "grounding_history_ref" not in ungranted.finding.data
    assert HISTORY_DETAIL_RECEIPT_KEY not in ungranted.finding.data
