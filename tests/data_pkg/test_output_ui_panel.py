# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-192 — `legba.data.outputs.ui_panel` registration tests.

Real Postgres (the shared `migrated_pg` fixture). Per L-001 the data-pkg
test suite runs against the live substrate; the ui_panel registry is
substrate-typed, so unit-only tests would not validate the contract that
matters (the SQL round-trip + unique-index guard).

Covers:

  * Module surface — ``KIND_NAME``, ``UIPanelRegistry``,
    ``PanelRegistration``, ``register_from_descriptor`` re-export.
  * Discovery — ``ui_panel`` shows up in ``discover_output_kinds`` with
    ``emit == None`` (this kind is registry-shaped, not emit-shaped).
  * ``register_from_descriptor`` materializes one row per ``ui_panel``
    entry on a descriptor's ``outputs`` block.
  * ``list_by_mode`` filters mode-conditionally; the column-based filter
    is the load-bearing path for L-204 bundle-time mode strip.
  * ``list_by_layout_slot`` resolves a preset-slot to its panel (or
    history with ``include_retired=True``).
  * Retirement — ``retire_for_descriptor`` soft-deletes every row tied
    to the named descriptor; subsequent ``list_by_mode`` excludes them.
  * Multi-panel-per-descriptor — a target descriptor declaring N
    ``ui_panel`` entries materializes N rows.
  * Layout-slot conflict — two descriptors claiming the same
    ``(mode, layout_slot)`` raises ``LayoutSlotConflict`` cleanly.
  * Mode-alias normalization — ``cis_fellowship`` and ``above-ai``
    persist as canonical snake_case.
  * Re-registration is idempotent (upsert on
    (descriptor_id, descriptor_version, panel_id)).
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.outputs import discover_output_kinds, ui_panel
from legba.data.outputs.ui_panel import (
    KIND_NAME,
    LayoutSlotConflict,
    PanelRegistration,
    UIPanelDescriptorError,
    UIPanelRegistry,
    parse_descriptor_panels,
    register_from_descriptor,
)


# ---------------------------------------------------------------------------
# Unit / surface tests
# ---------------------------------------------------------------------------


def test_kind_name_constant():
    assert KIND_NAME == "ui_panel"
    assert ui_panel.KIND_NAME == "ui_panel"


def test_module_exposes_expected_surface():
    """Discovery + integration code import these by name."""
    assert callable(register_from_descriptor)
    assert callable(parse_descriptor_panels)
    assert isinstance(PanelRegistration, type)
    assert isinstance(UIPanelRegistry, type)


def test_discover_picks_up_ui_panel():
    """``discover_output_kinds`` returns ui_panel with emit=None.

    The kind is registry-shaped, not emit-shaped — like ``substrate``,
    ``a2a_skill``, ``mcp_tool``. Operators wire panel materialization via
    :func:`register_from_descriptor`, not the dispatcher's ``emit``.
    """
    kinds = discover_output_kinds()
    assert KIND_NAME in kinds
    handler = kinds[KIND_NAME]
    assert handler.kind_name == KIND_NAME
    assert handler.emit is None
    assert handler.module is ui_panel


def test_parse_descriptor_panels_skips_other_kinds():
    """Only `kind: ui_panel` entries are picked up; siblings ignored."""
    regs = parse_descriptor_panels(
        descriptor_id="t.brazil",
        descriptor_version="abc1234567890def",
        descriptor_family="target",
        outputs=[
            {"kind": "a2a_skill", "config": {"skill_id": "x"}},
            {"kind": "ui_panel", "config": {
                "panel": "panels.target_overview",
                "mode": "personal",
                "layout_slot": "dashboard.brazil.overview",
            }},
            {"kind": "alert", "config": {}},
        ],
    )
    assert len(regs) == 1
    assert regs[0].panel_id == "target_overview"


def test_parse_strips_panels_prefix():
    """`panels.target_overview` → `target_overview`; bare form also accepted."""
    regs = parse_descriptor_panels(
        descriptor_id="t.x",
        descriptor_version="hashy",
        descriptor_family="target",
        outputs=[
            {"kind": "ui_panel", "config": {
                "panel": "panels.target_overview",
                "mode": "personal",
                "layout_slot": "slot.a",
            }},
            {"kind": "ui_panel", "config": {
                "panel": "target_signals",       # bare form
                "mode": "personal",
                "layout_slot": "slot.b",
            }},
        ],
    )
    assert {r.panel_id for r in regs} == {"target_overview", "target_signals"}


def test_parse_rejects_missing_required_fields():
    """`panel`, `mode`, `layout_slot` are mandatory."""
    with pytest.raises(UIPanelDescriptorError):
        parse_descriptor_panels(
            descriptor_id="t.x",
            descriptor_version="h",
            descriptor_family="target",
            outputs=[{"kind": "ui_panel", "config": {
                "mode": "personal", "layout_slot": "s",
            }}],
        )
    with pytest.raises(UIPanelDescriptorError):
        parse_descriptor_panels(
            descriptor_id="t.x",
            descriptor_version="h",
            descriptor_family="target",
            outputs=[{"kind": "ui_panel", "config": {
                "panel": "panels.x", "layout_slot": "s",
            }}],
        )
    with pytest.raises(UIPanelDescriptorError):
        parse_descriptor_panels(
            descriptor_id="t.x",
            descriptor_version="h",
            descriptor_family="target",
            outputs=[{"kind": "ui_panel", "config": {
                "panel": "panels.x", "mode": "personal",
            }}],
        )


def test_parse_rejects_unknown_mode():
    with pytest.raises(UIPanelDescriptorError):
        parse_descriptor_panels(
            descriptor_id="t.x",
            descriptor_version="h",
            descriptor_family="target",
            outputs=[{"kind": "ui_panel", "config": {
                "panel": "panels.x", "mode": "operator", "layout_slot": "s",
            }}],
        )


def test_parse_normalizes_mode_aliases():
    """`above-ai` → `above_ai`; `cis_fellowship` → `cis`."""
    regs = parse_descriptor_panels(
        descriptor_id="t.x",
        descriptor_version="h",
        descriptor_family="target",
        outputs=[
            {"kind": "ui_panel", "config": {
                "panel": "panels.a", "mode": "above-ai", "layout_slot": "s.a",
            }},
            {"kind": "ui_panel", "config": {
                "panel": "panels.b", "mode": "cis_fellowship", "layout_slot": "s.b",
            }},
            {"kind": "ui_panel", "config": {
                "panel": "panels.c", "mode": "Personal", "layout_slot": "s.c",
            }},
        ],
    )
    by_panel = {r.panel_id: r.mode for r in regs}
    assert by_panel["a"] == "above_ai"
    assert by_panel["b"] == "cis"
    assert by_panel["c"] == "personal"


def test_parse_rejects_duplicate_panel_id_in_outputs():
    """A descriptor cannot declare the same panel twice."""
    with pytest.raises(UIPanelDescriptorError):
        parse_descriptor_panels(
            descriptor_id="t.x",
            descriptor_version="h",
            descriptor_family="target",
            outputs=[
                {"kind": "ui_panel", "config": {
                    "panel": "panels.target_overview",
                    "mode": "personal", "layout_slot": "slot.one",
                }},
                {"kind": "ui_panel", "config": {
                    "panel": "panels.target_overview",
                    "mode": "personal", "layout_slot": "slot.two",
                }},
            ],
        )


def test_parse_rejects_bad_descriptor_family():
    with pytest.raises(UIPanelDescriptorError):
        parse_descriptor_panels(
            descriptor_id="t.x",
            descriptor_version="h",
            descriptor_family="wiring",                  # not allowed
            outputs=[],
        )


def test_parse_promotes_string_data_query_to_subject_shape():
    regs = parse_descriptor_panels(
        descriptor_id="a.x",
        descriptor_version="h",
        descriptor_family="analyst",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.analyst_runs",
            "mode": "personal",
            "layout_slot": "slot.x",
            "data_query": "analyst.x.runs",
        }}],
    )
    assert regs[0].data_query == {"subject": "analyst.x.runs"}


def test_parse_marks_analyst_id_on_analyst_family():
    regs = parse_descriptor_panels(
        descriptor_id="a.predictor",
        descriptor_version="h",
        descriptor_family="analyst",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.analyst_runs",
            "mode": "personal", "layout_slot": "slot.x",
        }}],
    )
    assert regs[0].analyst_id == "a.predictor"


def test_parse_leaves_analyst_id_none_on_target_family():
    regs = parse_descriptor_panels(
        descriptor_id="t.brazil",
        descriptor_version="h",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "mode": "personal", "layout_slot": "slot.x",
        }}],
    )
    assert regs[0].analyst_id is None


# ---------------------------------------------------------------------------
# Integration fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


def _outputs_for_target(target_id: str) -> list[dict]:
    """Materialize a realistic L-108 §3 outputs block for a target."""
    return [
        {"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "binding": {"target_id": target_id},
            "mode": "personal",
            "layout_slot": f"dashboard.{target_id}.overview",
            "title": f"Target — {target_id}",
            "data_query": {"kind": "rest", "path": f"/api/v3/targets/{target_id}"},
        }},
        {"kind": "ui_panel", "config": {
            "panel": "panels.target_signals",
            "binding": {"target_id": target_id},
            "mode": "personal",
            "layout_slot": f"dashboard.{target_id}.signals",
            "title": f"Signals — {target_id}",
            "data_query": "analyst.*.signals." + target_id,
        }},
        {"kind": "a2a_skill", "config": {"skill_id": "x"}},   # ignored
    ]


# ---------------------------------------------------------------------------
# register_from_descriptor — happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_from_descriptor_persists_each_ui_panel_entry(pg_conn):
    target = f"brazil_{uuid4().hex[:8]}"
    version = "deadbeef0000cafe"

    rows = await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version=version,
        descriptor_family="target",
        outputs=_outputs_for_target(target),
    )

    assert len(rows) == 2
    by_panel = {r.panel_id: r for r in rows}
    assert set(by_panel) == {"target_overview", "target_signals"}

    overview = by_panel["target_overview"]
    assert overview.id is not None
    assert overview.descriptor_id == target
    assert overview.descriptor_version == version
    assert overview.descriptor_family == "target"
    assert overview.mode == "personal"
    assert overview.layout_slot == f"dashboard.{target}.overview"
    assert overview.binding == {"target_id": target}
    assert overview.data_query == {
        "kind": "rest", "path": f"/api/v3/targets/{target}",
    }
    assert overview.retired is False
    assert overview.created_at is not None
    assert overview.analyst_id is None

    signals = by_panel["target_signals"]
    assert signals.data_query == {"subject": f"analyst.*.signals.{target}"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_from_descriptor_no_ui_panel_entries_returns_empty(pg_conn):
    """A descriptor with no ui_panel entries persists nothing."""
    rows = await register_from_descriptor(
        pg_conn,
        descriptor_id=f"t_{uuid4().hex[:8]}",
        descriptor_version="h",
        descriptor_family="target",
        outputs=[
            {"kind": "a2a_skill", "config": {"skill_id": "x"}},
            {"kind": "alert", "config": {}},
        ],
    )
    assert rows == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_from_descriptor_round_trips_get(pg_conn):
    target = f"t_{uuid4().hex[:8]}"
    rows = await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version="v1",
        descriptor_family="target",
        outputs=_outputs_for_target(target),
    )
    registry = UIPanelRegistry(pg_conn)
    fetched = await registry.get(rows[0].id)
    assert fetched is not None
    assert fetched.id == rows[0].id
    assert fetched.panel_id == rows[0].panel_id
    assert fetched.binding == rows[0].binding


# ---------------------------------------------------------------------------
# list_by_mode
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_by_mode_filters_by_canonical_mode(pg_conn):
    target_p = f"p_{uuid4().hex[:8]}"
    target_c = f"c_{uuid4().hex[:8]}"

    await register_from_descriptor(
        pg_conn,
        descriptor_id=target_p,
        descriptor_version="v",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "binding": {"target_id": target_p},
            "mode": "personal",
            "layout_slot": f"dashboard.{target_p}.overview",
        }}],
    )
    await register_from_descriptor(
        pg_conn,
        descriptor_id=target_c,
        descriptor_version="v",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "binding": {"target_id": target_c},
            "mode": "cis_fellowship",                    # alias → "cis"
            "layout_slot": f"dashboard.{target_c}.overview",
        }}],
    )

    reg = UIPanelRegistry(pg_conn)

    personal = await reg.list_by_mode("personal")
    cis = await reg.list_by_mode("cis")

    personal_ids = {r.descriptor_id for r in personal}
    cis_ids = {r.descriptor_id for r in cis}
    assert target_p in personal_ids
    assert target_p not in cis_ids
    assert target_c in cis_ids
    assert target_c not in personal_ids

    # Mode alias works on read side too.
    via_alias = await reg.list_by_mode("cis-fellowship")
    assert {r.descriptor_id for r in via_alias} == cis_ids


# ---------------------------------------------------------------------------
# list_by_layout_slot
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_by_layout_slot_returns_active_holder(pg_conn):
    target = f"t_{uuid4().hex[:8]}"
    slot = f"dashboard.{target}.overview"
    await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version="v",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "binding": {"target_id": target},
            "mode": "personal",
            "layout_slot": slot,
        }}],
    )
    reg = UIPanelRegistry(pg_conn)
    hits = await reg.list_by_layout_slot(slot)
    assert len(hits) == 1
    assert hits[0].descriptor_id == target


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_by_layout_slot_includes_retired_when_requested(pg_conn):
    """Layout restore (L-204) wants the history chain via include_retired."""
    target = f"t_{uuid4().hex[:8]}"
    slot = f"dashboard.{target}.slot"

    await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version="v",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "mode": "personal",
            "layout_slot": slot,
        }}],
    )
    reg = UIPanelRegistry(pg_conn)
    await reg.retire_for_descriptor(target)

    active_only = await reg.list_by_layout_slot(slot)
    with_history = await reg.list_by_layout_slot(slot, include_retired=True)

    assert active_only == []
    assert len(with_history) == 1
    assert with_history[0].retired is True
    assert with_history[0].retired_at is not None


# ---------------------------------------------------------------------------
# retire_for_descriptor
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retire_for_descriptor_soft_deletes_every_owned_row(pg_conn):
    target = f"t_{uuid4().hex[:8]}"
    await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version="v",
        descriptor_family="target",
        outputs=_outputs_for_target(target),
    )

    reg = UIPanelRegistry(pg_conn)
    before = await reg.list_by_mode("personal")
    n_before_for_target = sum(1 for r in before if r.descriptor_id == target)
    assert n_before_for_target == 2

    n_retired = await reg.retire_for_descriptor(target)
    assert n_retired == 2

    after = await reg.list_by_mode("personal")
    assert all(r.descriptor_id != target for r in after)

    # Idempotent — second retire returns 0.
    n_again = await reg.retire_for_descriptor(target)
    assert n_again == 0

    # Rows still present, just retired=True.
    everything = await reg.list_for_descriptor(target, include_retired=True)
    assert len(everything) == 2
    assert all(r.retired for r in everything)
    assert all(r.retired_at is not None for r in everything)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retire_for_descriptor_pinned_to_version(pg_conn):
    """Retiring v1 must leave v2 active."""
    target = f"t_{uuid4().hex[:8]}"
    outputs = [{"kind": "ui_panel", "config": {
        "panel": "panels.target_overview",
        "mode": "personal",
        "layout_slot": f"dashboard.{target}.overview",
    }}]

    # v1.
    await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version="v1",
        descriptor_family="target",
        outputs=outputs,
        retire_prior_versions=False,                 # leave v1 alone for now
    )
    # v2 — bypass auto-retire so we have two active rows for the test.
    await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version="v2",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_signals",        # different panel id
            "mode": "personal",
            "layout_slot": f"dashboard.{target}.signals",
        }}],
        retire_prior_versions=False,
    )

    reg = UIPanelRegistry(pg_conn)
    n = await reg.retire_for_descriptor(target, descriptor_version="v1")
    assert n == 1
    remaining = await reg.list_for_descriptor(target, include_retired=False)
    assert len(remaining) == 1
    assert remaining[0].descriptor_version == "v2"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_from_descriptor_retires_prior_versions(pg_conn):
    """Default flow: update fans out new rows + retires the prior version."""
    target = f"t_{uuid4().hex[:8]}"

    await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version="v1",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "mode": "personal",
            "layout_slot": f"dashboard.{target}.overview",
        }}],
    )
    await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version="v2",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "mode": "personal",
            "layout_slot": f"dashboard.{target}.overview",
        }}],
    )

    reg = UIPanelRegistry(pg_conn)
    history = await reg.list_for_descriptor(target, include_retired=True)
    versions = {(r.descriptor_version, r.retired) for r in history}
    assert ("v1", True) in versions
    assert ("v2", False) in versions


# ---------------------------------------------------------------------------
# Layout-slot conflict detection
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_layout_slot_conflict_across_descriptors(pg_conn):
    """Two descriptors claiming the same (mode, layout_slot) raises."""
    slot = f"dashboard.shared_slot.{uuid4().hex[:8]}"
    target_a = f"a_{uuid4().hex[:8]}"
    target_b = f"b_{uuid4().hex[:8]}"

    await register_from_descriptor(
        pg_conn,
        descriptor_id=target_a,
        descriptor_version="v",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "mode": "personal",
            "layout_slot": slot,
        }}],
    )

    with pytest.raises(LayoutSlotConflict) as excinfo:
        await register_from_descriptor(
            pg_conn,
            descriptor_id=target_b,
            descriptor_version="v",
            descriptor_family="target",
            outputs=[{"kind": "ui_panel", "config": {
                "panel": "panels.target_signals",
                "mode": "personal",
                "layout_slot": slot,
            }}],
        )
    assert excinfo.value.mode == "personal"
    assert excinfo.value.layout_slot == slot
    assert excinfo.value.existing_panel_id == "target_overview"
    assert excinfo.value.attempted_panel_id == "target_signals"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_layout_slot_can_be_reused_across_modes(pg_conn):
    """Slot conflict is scoped to (mode, layout_slot); different modes OK."""
    slot = f"dashboard.cross_mode.{uuid4().hex[:8]}"
    target_p = f"p_{uuid4().hex[:8]}"
    target_c = f"c_{uuid4().hex[:8]}"
    await register_from_descriptor(
        pg_conn,
        descriptor_id=target_p,
        descriptor_version="v",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "mode": "personal",
            "layout_slot": slot,
        }}],
    )
    # Same slot string, different mode — must succeed.
    rows = await register_from_descriptor(
        pg_conn,
        descriptor_id=target_c,
        descriptor_version="v",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "mode": "cis",
            "layout_slot": slot,
        }}],
    )
    assert len(rows) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_layout_slot_reclaimed_after_retire(pg_conn):
    """A retired panel releases its slot; a new registration can claim it."""
    slot = f"dashboard.reclaim.{uuid4().hex[:8]}"
    target_a = f"a_{uuid4().hex[:8]}"
    target_b = f"b_{uuid4().hex[:8]}"

    await register_from_descriptor(
        pg_conn,
        descriptor_id=target_a,
        descriptor_version="v",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_overview",
            "mode": "personal",
            "layout_slot": slot,
        }}],
    )
    reg = UIPanelRegistry(pg_conn)
    await reg.retire_for_descriptor(target_a)

    rows = await register_from_descriptor(
        pg_conn,
        descriptor_id=target_b,
        descriptor_version="v",
        descriptor_family="target",
        outputs=[{"kind": "ui_panel", "config": {
            "panel": "panels.target_signals",
            "mode": "personal",
            "layout_slot": slot,
        }}],
    )
    assert len(rows) == 1
    assert rows[0].descriptor_id == target_b


# ---------------------------------------------------------------------------
# Re-registration / idempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_re_register_same_version_is_idempotent_upsert(pg_conn):
    """Calling register_from_descriptor twice with the same (id, version)
    leaves exactly one row per panel_id and updates the body."""
    target = f"t_{uuid4().hex[:8]}"
    base_outputs = [{"kind": "ui_panel", "config": {
        "panel": "panels.target_overview",
        "binding": {"target_id": target},
        "mode": "personal",
        "layout_slot": f"dashboard.{target}.overview",
        "title": "First title",
    }}]

    first = await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version="v",
        descriptor_family="target",
        outputs=base_outputs,
    )

    # Re-register the same descriptor identity with a tweaked title.
    base_outputs[0]["config"]["title"] = "Second title"
    second = await register_from_descriptor(
        pg_conn,
        descriptor_id=target,
        descriptor_version="v",
        descriptor_family="target",
        outputs=base_outputs,
    )

    assert first[0].id == second[0].id          # same row, upserted
    assert second[0].title == "Second title"

    reg = UIPanelRegistry(pg_conn)
    history = await reg.list_for_descriptor(target, include_retired=True)
    assert len(history) == 1


# ---------------------------------------------------------------------------
# Analyst-family path
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_from_descriptor_analyst_family_marks_analyst_id(pg_conn):
    analyst_id = f"a.predictor_{uuid4().hex[:8]}"
    await register_from_descriptor(
        pg_conn,
        descriptor_id=analyst_id,
        descriptor_version="v",
        descriptor_family="analyst",
        outputs=[
            {"kind": "ui_panel", "config": {
                "panel": "panels.analyst_runs",
                "binding": {"analyst_id": analyst_id},
                "mode": "personal",
                "layout_slot": f"analyst.{analyst_id}.runs",
            }},
            {"kind": "ui_panel", "config": {
                "panel": "panels.analyst_forecasts",
                "binding": {"analyst_id": analyst_id},
                "mode": "personal",
                "layout_slot": f"analyst.{analyst_id}.forecasts",
            }},
        ],
    )
    reg = UIPanelRegistry(pg_conn)
    rows = await reg.list_for_descriptor(analyst_id, include_retired=False)
    assert len(rows) == 2
    assert all(r.descriptor_family == "analyst" for r in rows)
    assert all(r.analyst_id == analyst_id for r in rows)
