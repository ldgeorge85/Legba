# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Roll 83 — a first read's movement call is ``new``, decided in code.

The cyber desk's first run (roll 82) tagged ``severity_delta:steady`` with no
prior finding anywhere. Whether a prior finding exists is a row, not a
judgement, so ``inline_target.run_method`` settles it before NARRATE. Held on
the real path: the cyber desk's own descriptor options through the runtime's
own merge into ``run_method`` (stub LLM, no model call), and the probe's SQL
against a real migrated Postgres.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

import pytest
import yaml

from legba.data.analysts import severity_delta_first_read as SD
from legba.data.provenance.models import severity_delta_from_tags
from legba.data.schemas.analyst import AnalystDescriptor

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _unit() -> AnalystDescriptor:
    return AnalystDescriptor.model_validate(
        yaml.safe_load((_ROOT / "descriptors" / "analyst_cyber_watch.yaml").read_text()))


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _LLM:
    subprovider = "openai"

    def __init__(self, tags: list[str]) -> None:
        self.tags = tags

    async def chat_complete(self, messages: list[Mapping[str, Any]], **kwargs: Any) -> Any:
        return _Response(content=json.dumps({
            "title": "Quiet window on the cyber desk", "body": "**BLUF:** CISA added one entry [1].",
            "confidence": 0.3, "evidence": ["1"], "tags": self.tags}), usage=_Usage())


class _PG:
    """Answers the roster count (nothing) and the prior-finding probe."""

    def __init__(self, prior: Any) -> None:
        self.prior = prior
        self.probes: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        assert sql == SD._PRIOR_FINDING_SQL
        self.probes.append(args)
        if isinstance(self.prior, Exception):
            raise self.prior
        return self.prior


async def _run(tags: list[str], pg: Any) -> Any:
    from legba.data.analysts.inline_target import InlineTargetDeps, run_method
    from legba.runtime.dapr_actors import _merge_descriptor_options

    options: dict[str, Any] = {"target_id": "theme_cyber", "analyst_id": "cyber_watch",
                               "slice_window_hours": 12}
    _merge_descriptor_options(options, _unit(), actor_id="test")
    row = {"title": "CVE-2026-0001", "source_url": "", "source_id": "source.cisa.kev",
           "produced_at": "2026-11-14T08:00:00+00:00",
           "data": {"published_at": "2026-11-14T07:00:00+00:00", "summary": "CVE-2026-0001"}}
    return await run_method([row], options, InlineTargetDeps(llm=_LLM(tags), pg=pg))


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", ["steady", "rose", "fell"])
async def test_the_first_read_of_a_target_is_new_whatever_the_model_claimed(claimed: str) -> None:
    pg = _PG(prior=False)
    result = await _run(["severity:moderate", f"severity_delta:{claimed}"], pg)
    tags = result.finding.tags
    assert severity_delta_from_tags(tags) == "new"
    assert sum(1 for t in tags if t.startswith("severity_delta:")) == 1
    assert "severity:moderate" in tags
    assert pg.probes == [("cyber_watch", "theme_cyber")]
    step = next(s for s in result.intermediate_steps if s.get("kind") == "severity_delta_first_read")
    assert step == {"phase": "narrate", "kind": "severity_delta_first_read", "claimed": claimed, "set": "new"}


@pytest.mark.asyncio
async def test_a_target_with_a_prior_finding_keeps_the_models_call() -> None:
    result = await _run(["severity:high", "severity_delta:steady"], _PG(prior=True))
    assert severity_delta_from_tags(result.finding.tags) == "steady"
    assert not any(s.get("kind") == "severity_delta_first_read" for s in result.intermediate_steps)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", [None, RuntimeError("substrate blip"), 1])
async def test_an_unknown_answer_leaves_the_call_alone(prior: Any) -> None:
    result = await _run(["severity:low", "severity_delta:steady"], _PG(prior=prior))
    assert severity_delta_from_tags(result.finding.tags) == "steady"


@pytest.mark.asyncio
async def test_no_tag_stays_no_tag_and_new_is_not_probed() -> None:
    pg = _PG(prior=False)
    result = await _run(["severity:low"], pg)
    assert severity_delta_from_tags(result.finding.tags) is None
    result = await _run(["severity:low", "severity_delta:new"], pg)
    assert severity_delta_from_tags(result.finding.tags) == "new"
    assert pg.probes == []


@pytest.mark.asyncio
async def test_without_a_pg_nothing_changes() -> None:
    result = await _run(["severity:low", "severity_delta:steady"], None)
    assert severity_delta_from_tags(result.finding.tags) == "steady"


# ---------------------------------------------------------------------------
# The probe against a real migrated Postgres (the isolated test stack)
# ---------------------------------------------------------------------------


@pytest.fixture
async def pool(migrated_pg):
    import asyncpg

    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    yield p
    await p.close()


@pytest.mark.asyncio
async def test_the_probe_reads_this_analysts_findings_for_this_target(pool) -> None:
    analyst = f"sd_first_read_{uuid4().hex[:8]}"
    assert await SD.has_prior_finding(pool, analyst_id=analyst, target_id="theme_x") is False
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO analyst_outputs (kind, title, body, confidence, data, analyst_id, target_id, schema_uri)"
            " VALUES ('finding', 't', 'b', 0.5, '{}'::jsonb, $1, 'theme_x', 'legba/finding/1.0.0')", analyst)
    try:
        assert await SD.has_prior_finding(pool, analyst_id=analyst, target_id="theme_x") is True
        assert await SD.has_prior_finding(pool, analyst_id=analyst, target_id="theme_y") is False
        assert await SD.has_prior_finding(pool, analyst_id=analyst, target_id=None) is False
    finally:
        async with pool.acquire() as c:
            await c.execute("DELETE FROM analyst_outputs WHERE analyst_id = $1", analyst)
