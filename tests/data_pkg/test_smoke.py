# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end smoke test — runs `run_smoke()` against the migrated test DB."""

from __future__ import annotations

import pytest

from legba.data.config import PostgresConfig
from legba.data.smoke import run_smoke


@pytest.mark.integration
@pytest.mark.asyncio
async def test_smoke_passes(migrated_pg: PostgresConfig):
    result = await run_smoke(migrated_pg)
    assert result.ok, (
        f"smoke failed.\n"
        f"missing tables:        {result.tables_missing}\n"
        f"unexpected retired:    {result.tables_unexpected_retired}\n"
        f"provenance failures:   {result.provenance_table_failures}\n"
        f"vertex diff:           {result.vertex_label_diff}\n"
        f"edge diff:             {result.edge_label_diff}\n"
        f"errors:                {result.errors}"
    )
    assert result.sample_target_id
    assert result.sample_stack_component_id
    assert result.sample_trace_run_id
    assert result.lineage_query_ok
    assert len(result.age_vertex_labels) >= 9
    assert len(result.age_edge_labels) >= 14
    assert result.table_stats, "expected non-empty table stats"
