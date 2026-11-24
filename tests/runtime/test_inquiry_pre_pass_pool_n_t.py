# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-T (2026-11-13) — the inquiry pre-pass receives the substrate pool.

``crossroads_detectors.pre_pass_block`` reads ``deps.pg_pool`` for its two leaf
reads (``journal_entries.lens`` and ``fact_contention``). The deps the runtime
actually hands an ``inquiry`` run are the journal family's
:class:`InlineTargetDeps`, which never carried ``pg_pool`` — so on every
crossroads run since the kind shipped (2026-09-24) both leaf reads were
reported failed, silently (the 9 and 13 November bodies both say so). The unit
tests passed because they handed the hook a hand-built ``_Deps`` with the
attribute. These tests go through the REAL builder and the REAL seam.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from legba.data.analysts import crossroads_detectors as cd
from legba.data.analysts.inline_target import InlineTargetDeps
from legba.data.analysts.inquiry import resolve_pre_pass_block
from legba.runtime.analyst_deps_builder import build_analyst_run_method
from legba.runtime.deps import StandardDeps

_REPO = Path(__file__).resolve().parents[2]


class _StubLLM:
    subprovider = "stub-test"

    def thinking_available(self) -> bool:
        # T-2: crossroads' voice thinks (method.llm.narrate.thinking), so the
        # core component it stands in for declares a thinking alias.
        return True

    async def chat_complete(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


async def _stub_secrets(_secret_id: str) -> bytes:  # pragma: no cover
    return b"stub"


class _Pool:
    """Records every leaf query; answers with no rows."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return []


def _crossroads_descriptor():
    from legba.data.schemas.analyst import AnalystDescriptor

    body = yaml.safe_load((_REPO / "descriptors/analyst_crossroads.yaml").read_text())
    body.setdefault("identity", {})["version"] = "0" * 64
    return AnalystDescriptor.model_validate(body, strict=False)


@pytest.mark.asyncio
async def test_the_built_inquiry_deps_carry_the_pool_to_the_pre_pass() -> None:
    pool = _Pool()
    desc = _crossroads_descriptor()
    _run, kind_deps, _kind, _chain, _slice = await build_analyst_run_method(
        desc,
        deps=StandardDeps(pg_pool=pool, nats_publish=None, secrets_resolve=_stub_secrets),
        registry_client=AsyncMock(),
        pg_pool=pool,
        llm_handler_factory=AsyncMock(return_value=_StubLLM()),
    )
    assert isinstance(kind_deps, InlineTargetDeps)
    assert kind_deps.pg_pool is pool, "the inquiry pre-pass reads deps.pg_pool"
    steps: list[dict] = []
    block = await resolve_pre_pass_block(dict(desc.method.options), kind_deps, steps=steps)
    assert block and "CROSSROADS BLOCK" in block
    assert cd._LENS_ROWS_SQL in pool.sql, "the lens leaf read ran on the pool"
    assert "journal_entries.lens (leaf)" not in block, "no longer reported failed"


@pytest.mark.asyncio
async def test_the_pre_pass_warns_when_it_gets_no_pool(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=cd.__name__)
    await cd.pre_pass_block({}, InlineTargetDeps(llm=_StubLLM()))
    assert any("crossroads.pre_pass.no_pool" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_pool_on_the_real_deps_class_reaches_both_leaves() -> None:
    """With a pool on the real deps class the lens leaf is no longer a failed
    read (the contention leaf runs only when a contradiction names a subject)."""
    pool = _Pool()
    substrate = await cd.fetch_crossroads_substrate(
        tool_bindings={}, pg=InlineTargetDeps(llm=_StubLLM(), pg_pool=pool).pg_pool,
        lens_analyst_ids=("lens_trend",), now=cd.datetime.datetime.now(cd.datetime.timezone.utc),
        window_hours=24,
    )
    assert "journal_entries.lens (leaf)" not in substrate.reads_failed
    assert cd._LENS_ROWS_SQL in pool.sql
