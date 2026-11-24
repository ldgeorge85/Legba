# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W4 F-3 — the per-subject-group floor on the world read
(``legba.data.analysts.assembly_floor``; design note
``planning/v2/design/F-3_WORLD_FLOOR.md``).

What this file proves:

  * THE SELECTION — the floor selects each group's ``order_key``-best, the
    strict prefix fills, the published order is the carried set then the rest;
    on the live 2933f773 shape the Americas (best order rank 17) is carried and
    Africa's third block (rank 16) is the one displaced.
  * INERTNESS — floor off (country / thematic / region), a floor that does not
    bite, and a partially grouped slice are all byte-identical to the pure
    ``order_key`` sort + tier-aware prefix.
  * D1 — a cap below the group count grows to it.
  * THE LEDGER AND THE STAMPS — ``floor`` / ``order_rank`` / ``carried_by_floor``
    / ``displaced_by_floor`` / ``floor_stale`` (D4, option-resolved), written
    only on the rows they are true of; ``ledger=None`` returns the payload
    object itself.
  * THE ASSESSMENT LINE — present only when the floor promoted, and never read
    as a desk-absence sentence.
  * THE REAL BINDING — ``run_method`` on a world run of the 2933f773 shape
    carries 6 of 6 groups, and ``floor.honoured`` is READ BACK off the row the
    real write path persisted.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import assembly_floor as af
from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assembly_render as ar
from legba.data.analysts import assessment_prompts as apr
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext, write_finding
from legba.data.provenance import assembly_arms as AA
from legba.data.provenance import composition_integrity as ci

BODY = (
    "**BLUF:** A coordinated escalation narrative is emerging that links "
    "recent strikes to retaliatory messaging [1].\n"
)

#: World read 2933f773 (2026-10-02 00:20Z), the design note's fixture: 33
#: candidates in six groups, (order rank, unit, group). The Americas' best is
#: rank 17 against a cap of 16 — carried 0 of 6 by the pure prefix.
LIVE_2933F773: tuple[tuple[int, str, str], ...] = (
    (1, "escalation_composition", "thematic:escalation_composition"),
    (2, "country_watch_ua", "region_europe"),
    (3, "country_g20_sa", "region_mena"),
    (4, "country_watch_ir", "region_mena"),
    (5, "country_g20_ru", "region_europe"),
    (6, "country_watch_pk", "region_indo_pacific"),
    (7, "country_watch_il", "region_mena"),
    (8, "country_g20_gb", "region_europe"),
    (9, "country_watch_mm", "region_indo_pacific"),
    (10, "country_g20_cn", "region_indo_pacific"),
    (11, "country_g20_fr", "region_europe"),
    (12, "country_g20_za", "region_africa"),
    (13, "country_watch_sd", "region_africa"),
    (14, "country_g20_kr", "region_indo_pacific"),
    (15, "country_watch_kp", "region_indo_pacific"),
    (16, "country_watch_ml", "region_africa"),
    (17, "country_g20_ca", "region_americas"),
    (18, "country_g20_id", "region_indo_pacific"),
    (19, "country_g20_us", "region_americas"),
    (20, "country_watch_ht", "region_americas"),
    (21, "country_g20_in", "region_indo_pacific"),
    (22, "country_watch_ne", "region_africa"),
    (23, "country_g20_au", "region_indo_pacific"),
    (24, "country_g20_jp", "region_indo_pacific"),
    (25, "country_g20_ar", "region_americas"),
    (26, "country_g20_mx", "region_americas"),
    (27, "country_watch_cd", "region_africa"),
    (28, "country_watch_tw", "region_indo_pacific"),
    (29, "country_g20_de", "region_europe"),
    (30, "country_g20_tr", "region_mena"),
    (31, "country_g20_it", "region_europe"),
    (32, "country_g20_br", "region_americas"),
    (33, "country_watch_bf", "region_africa"),
)

#: The slice's group roster as ``composition_slice`` stamps it (roster order,
#: then thematic lanes), plus a declared thematic lane with NO row this cycle.
COVERAGE = [
    {"region_id": "region_africa", "region_name": "Africa", "mode": "countries", "input_count": 7},
    {"region_id": "region_americas", "region_name": "Americas", "mode": "countries", "input_count": 7},
    {"region_id": "region_europe", "region_name": "Europe", "mode": "countries", "input_count": 6},
    {"region_id": "region_indo_pacific", "region_name": "Indo-Pacific", "mode": "countries", "input_count": 10},
    {"region_id": "region_mena", "region_name": "MENA", "mode": "countries", "input_count": 4},
    {"region_id": "thematic:escalation_composition",
     "region_name": "escalation_composition (cross-region thematic)",
     "mode": "thematic", "input_count": 1},
    {"region_id": "thematic:escalation_dyad",
     "region_name": "escalation_dyad (cross-region thematic)",
     "mode": "thematic_gap", "input_count": 0},
]

# W4-F2 (2026-10-08): the rows' clock is read when ``_rows()`` is CALLED, not
# when this module is imported. A module-level ``NOW`` froze at collection
# time while ``head_age_hours`` reads the wall clock at run time, so under a
# shuffled 30-minute session the "6.0h old" a world run renders drifted to
# "6.4h old" whenever the module ran minutes after collection — green alone,
# red under shuffle. Ages are relative to the instant the rows are built.


def _uid(unit: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"f3-floor/{unit}"))


def _rows(
    shape=LIVE_2933F773,
    *,
    grouped: bool = True,
    coverage: bool = True,
    age_h: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for rank, unit, group in shape:
        mass = round(4.0 - rank * 0.1, 2)
        produced = now - timedelta(hours=(age_h or {}).get(unit, 6.0))
        row: dict[str, Any] = {
            "id": _uid(unit),
            "kind": "finding",
            "analyst_id": "country_composition",
            "analyst_version": "vtest",
            "target_id": unit,
            "target_version": None,
            "title": f"read {unit}",
            "body": BODY,
            "severity": "high",
            "confidence": 0.8,
            "produced_at": produced.isoformat(),
            "faithfulness_score": 0.9,
            "effective_confidence": 0.8,
            "evidence": [],
            "derived_from": [],
            "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
            "data": {"data": {"citations": []}},
            ap.CITED_SALIENCE_ROW_KEY: {
                "cited_max": mass, "cited_mass": mass, "n_above": 0,
                "n_cited_scored": 1, "n_cited": 1, "source": "fixture",
                "version": "fixture",
            },
        }
        if grouped:
            row[af.SUBJECT_GROUP_ROW_KEY] = group
            if coverage:
                row[af.SUBJECT_GROUP_COVERAGE_KEY] = COVERAGE
        rows.append(row)
    return rows


def _id_of(unit: str) -> str:
    return _uid(unit)


def _world_payload(rows=None, **sel_kw):
    rows = _rows() if rows is None else rows
    sel = af.floor_select(rows, tier=ap.TIER_WORLD, apply_floor=True, **sel_kw)
    payload = ap.build_assembly(
        tier=ap.TIER_WORLD,
        as_of="2026-10-02T00:20:00+00:00",
        candidates=sel.order,
        carried=sel.carried,
    )
    return sel, payload


# ---------------------------------------------------------------------------
# 1. THE SELECTION
# ---------------------------------------------------------------------------


def test_the_floor_carries_the_americas_and_displaces_africas_third_block() -> None:
    sel = af.floor_select(_rows(), tier=ap.TIER_WORLD, apply_floor=True)
    carried_units = [r["target_id"] for r in sel.carried]
    assert len(sel.carried) == ap.WORLD_BLOCK_CAP == 16
    assert "country_g20_ca" in carried_units
    assert "country_watch_ml" not in carried_units
    # The printed order stays by merit: ordinal 1 is the pure head and the
    # promoted block prints LAST among the carried.
    assert carried_units[0] == "escalation_composition"
    assert carried_units[-1] == "country_g20_ca"
    # The published order is carried + rest, the rest still in order_key.
    pure = sorted(_rows(), key=ap.order_key)
    rest = [r for r in pure if r["id"] not in {c["id"] for c in sel.carried}]
    assert [r["id"] for r in sel.order] == (
        [r["id"] for r in sel.carried] + [r["id"] for r in rest]
    )
    led = sel.ledger
    assert led["promoted"] == [_id_of("country_g20_ca")]
    assert led["displaced"] == [_id_of("country_watch_ml")]
    assert led["honoured"] is True
    assert (led["cap"], led["cap_effective"]) == (16, 16)
    assert led["version"] == "subject_group_floor.v1"
    assert led["grain"] == "region"
    assert led["per_group"] == 1


def test_the_group_ledger_adds_up_and_names_every_roster_group() -> None:
    led = af.floor_select(_rows(), tier=ap.TIER_WORLD, apply_floor=True).ledger
    groups = {g["group_id"]: g for g in led["groups"]}
    # Roster order, the declared-but-empty lane included as a no_candidate row.
    assert [g["group_id"] for g in led["groups"]] == [c["region_id"] for c in COVERAGE]
    assert sum(g["offered"] for g in led["groups"]) == 33
    assert sum(g["carried"] for g in led["groups"]) == 16
    am = groups["region_americas"]
    assert am == {
        "group_id": "region_americas", "group_name": "Americas", "offered": 6,
        "carried": 1, "best_order_rank": 17, "reason": af.REASON_KEPT_BY_FLOOR,
    }
    assert groups["region_africa"]["carried"] == 2  # 3 by the pure prefix
    assert groups["region_africa"]["reason"] == af.REASON_IN_PREFIX
    dyad = groups["thematic:escalation_dyad"]
    assert dyad["reason"] == af.REASON_NO_CANDIDATE
    assert (dyad["offered"], dyad["carried"], dyad["best_order_rank"]) == (0, 0, None)


def test_the_floor_that_does_not_bite_is_byte_identical_to_the_pure_prefix() -> None:
    shape = tuple(r for r in LIVE_2933F773 if r[2] != "region_americas")
    rows = _rows(shape)
    pure = sorted(rows, key=ap.order_key)
    sel = af.floor_select(rows, tier=ap.TIER_WORLD, apply_floor=True)
    assert [r["id"] for r in sel.order] == [r["id"] for r in pure]
    assert [r["id"] for r in sel.carried] == [r["id"] for r in pure[:16]]
    assert sel.ledger["promoted"] == [] and sel.ledger["displaced"] == []


@pytest.mark.parametrize("tier", [ap.TIER_COUNTRY, ap.TIER_THEMATIC, ap.TIER_WORLD])
def test_floor_off_is_the_pure_sort_and_the_tier_aware_prefix(tier) -> None:
    rows = _rows()
    sel = af.floor_select(rows, tier=tier, apply_floor=False)
    pure = sorted(rows, key=ap.order_key)
    assert sel.ledger is None
    assert [r["id"] for r in sel.order] == [r["id"] for r in pure]
    assert sel.carried == ap.select_carried(pure, tier=tier)


def test_a_partially_grouped_slice_degrades_to_the_pure_order() -> None:
    """The no-roster topology stamps thematic rows and leaves country rows
    unbucketed: a floor over that would publish group arithmetic that does not
    add up to the candidates, so it degrades whole."""
    rows = _rows()
    for r in rows[1:]:
        r.pop(af.SUBJECT_GROUP_ROW_KEY)
    sel = af.floor_select(rows, tier=ap.TIER_WORLD, apply_floor=True)
    pure = sorted(rows, key=ap.order_key)
    assert sel.ledger is None
    assert [r["id"] for r in sel.order] == [r["id"] for r in pure]
    assert [r["id"] for r in sel.carried] == [r["id"] for r in pure[:16]]


def test_d1_a_cap_below_the_group_count_grows_the_carry_to_it() -> None:
    shape = tuple((i + 1, f"unit_{i}", f"group_{i}") for i in range(20))
    sel = af.floor_select(_rows(shape, coverage=False), tier=ap.TIER_WORLD, apply_floor=True)
    assert len(sel.carried) == 20
    assert sel.ledger["cap_effective"] == 20 and sel.ledger["cap"] == 16
    assert sel.ledger["honoured"] is True
    # Promoted = the four floor members below the cap; nothing displaced.
    assert len(sel.ledger["promoted"]) == 4 and sel.ledger["displaced"] == []
    # No coverage roster: groups are named by their ids.
    assert sel.ledger["groups"][0]["group_name"] == sel.ledger["groups"][0]["group_id"]


# ---------------------------------------------------------------------------
# 2. THE STAMPS
# ---------------------------------------------------------------------------


def test_stamp_floor_writes_the_audit_line_and_the_row_stamps() -> None:
    sel, payload = _world_payload()
    out = af.stamp_floor(payload, sel.ledger)
    assert out is not payload and "floor" not in payload  # pure post-pass
    floor = out["floor"]
    assert not any(k.startswith("_") for k in floor)
    assert floor["stale_hours"] == 72.0
    ranks = [b["order_rank"] for b in out["blocks"]]
    assert ranks == list(range(1, 16)) + [17]
    kept = [b for b in out["blocks"] if b.get("carried_by_floor")]
    assert [b["ordinal"] for b in kept] == [16]
    assert all("carried_by_floor" not in b for b in out["blocks"][:15])
    assert all("floor_stale" not in b for b in out["blocks"])  # 6 h old
    ns = out["drops"]["not_selected"]
    assert [d["order_rank"] for d in ns][:2] == [16, 18]
    assert [d["rank"] for d in ns][:2] == [17, 18]  # the PUBLISHED rank
    displaced = [d for d in ns if d.get("displaced_by_floor")]
    assert [d["finding_id"] for d in displaced] == [_id_of("country_watch_ml")]
    assert all(
        "order_rank" in d for d in out["drops"]["shown_not_carried"]
    )
    # Nothing else on the payload moved.
    assert out["drops"]["counts"] == payload["drops"]["counts"]
    assert out["lead"] == payload["lead"]


def test_stamp_floor_without_a_ledger_returns_the_payload_itself() -> None:
    rows = _rows()
    sel = af.floor_select(rows, tier=ap.TIER_COUNTRY, apply_floor=False)
    payload = ap.build_assembly(
        tier=ap.TIER_COUNTRY, as_of="2026-10-02T00:20:00+00:00",
        candidates=sel.order, carried=sel.carried,
    )
    before = json.dumps(payload, sort_keys=True, default=str)
    assert af.stamp_floor(payload, None) is payload
    assert json.dumps(payload, sort_keys=True, default=str) == before


def test_d4_an_old_block_the_floor_kept_is_stamped_stale_by_the_option_bar() -> None:
    rows = _rows(age_h={"country_g20_ca": 196.6})
    sel, payload = _world_payload(rows)
    out = af.stamp_floor(payload, sel.ledger)
    kept = [b for b in out["blocks"] if b.get("carried_by_floor")]
    assert kept[0]["floor_stale"] is True
    # The bar is option-resolved: 200 h makes the same block fresh.
    out2 = af.stamp_floor(
        payload, sel.ledger,
        stale_hours=af.floor_stale_hours({af.FLOOR_STALE_HOURS_OPTION: 200}),
    )
    assert "floor_stale" not in [b for b in out2["blocks"] if b.get("carried_by_floor")][0]
    assert out2["floor"]["stale_hours"] == 200.0


@pytest.mark.parametrize("raw,want", [
    (None, 72.0), (48, 48.0), (12.5, 12.5), (0, 72.0), (-3, 72.0),
    (True, 72.0), ("96", 72.0),
])
def test_the_stale_bar_resolver(raw, want) -> None:
    opts = {} if raw is None else {af.FLOOR_STALE_HOURS_OPTION: raw}
    assert af.floor_stale_hours(opts) == want


def test_the_stale_knob_is_declared_in_the_kind_catalog() -> None:
    """The catalog is the contract: a descriptor carrying the knob must parse
    on the runtime (the 09-28 dead-analyst lesson)."""
    from legba.data.option_catalog.handler_options import (
        ANALYST_KIND_OPTIONS,
        resolve_kind_options,
    )

    names = {s.name for s in ANALYST_KIND_OPTIONS["meta_findings_synthesizer"]}
    assert af.FLOOR_STALE_HOURS_OPTION in names
    res = resolve_kind_options(
        "meta_findings_synthesizer", {af.FLOOR_STALE_HOURS_OPTION: 96}
    )
    assert res.accepted == {af.FLOOR_STALE_HOURS_OPTION: 96.0}
    assert not res.rejected


def test_the_grain_is_the_slice_roster_tag() -> None:
    from legba.data.analysts.composition_slice import REGION_FRAME_TAG

    assert af.floor_select(
        _rows(), tier=ap.TIER_WORLD, apply_floor=True
    ).ledger["grain"] == REGION_FRAME_TAG


# ---------------------------------------------------------------------------
# 3. THE ARMS PASS, AND THE ASSESSMENT LINE
# ---------------------------------------------------------------------------


def test_a_floored_world_payload_passes_every_selection_identity() -> None:
    sel, payload = _world_payload()
    out = af.stamp_floor(payload, sel.ledger)
    res = AA.selection_honesty(out)
    assert res.findings == []
    assert res.counters.get("assembly_floor_checked") == 1
    assert "assembly_drop_order_violation" not in res.counters


def test_the_assessment_names_the_block_the_floor_kept() -> None:
    sel, payload = _world_payload()
    out = af.stamp_floor(payload, sel.ledger)
    text = apr.declared_aperture(out)
    line = [ln for ln in text.splitlines() if "floor" in ln]
    assert len(line) == 1
    assert line[0].startswith(
        "  * carried by the region floor, below the cut it would have ranked at: "
    )
    assert "country_g20_ca" in line[0]
    # Phrased as carriage, never as a desk negative.
    assert ci.desk_absence_sentences(line[0]) == []


def test_no_promotion_no_assessment_line() -> None:
    shape = tuple(r for r in LIVE_2933F773 if r[2] != "region_americas")
    sel, payload = _world_payload(_rows(shape))
    out = af.stamp_floor(payload, sel.ledger)
    assert "floor" not in apr.declared_aperture(out)
    assert apr.declared_aperture(out) == apr.declared_aperture(payload)


# ---------------------------------------------------------------------------
# 4. THE REAL BINDING — run_method on a world run, persisted, read back
# ---------------------------------------------------------------------------


class _NeverCalledLLM:
    subprovider = "never_called"

    async def chat_complete(self, *a, **k):  # pragma: no cover
        raise AssertionError("the assembly path is deterministic")


class _Deps:
    def __init__(self) -> None:
        self.llm = _NeverCalledLLM()


_WORLD_OPTIONS = {"analyst_id": "world_assessor", "composition": True}


@pytest.mark.asyncio
async def test_a_world_run_on_the_2933f773_shape_carries_six_of_six_groups(
    monkeypatch,
) -> None:
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "world_assessor")
    result = await synth.run_method(_rows(), dict(_WORLD_OPTIONS), _Deps())
    payload = result.finding.data["assembly"]
    floor = payload["floor"]
    assert floor["honoured"] is True
    carried = [g for g in floor["groups"] if g["carried"] >= 1]
    offered = [g for g in floor["groups"] if g["offered"] >= 1]
    assert len(carried) == len(offered) == 6
    assert floor["promoted"] == [_id_of("country_g20_ca")]
    # derived_from is minted from the PUBLISHED order: ordinal N is entry N.
    assert [str(u) for u in result.derived_from][:16] == [
        b["finding_id"] for b in payload["blocks"]
    ]
    assert payload["blocks"][-1]["carried_by_floor"] is True
    # ARM 4(b) still holds: every not-selected row's published rank is above 16.
    assert min(d["rank"] for d in payload["drops"]["not_selected"]) == 17
    # The steps receipt.
    floor_steps = [s for s in result.intermediate_steps if s.get("phase") == "floor"]
    assert floor_steps == [{
        "phase": "floor", "kind": "subject_group_floor.v1", "groups": 7,
        "honoured": True, "promoted": 1, "displaced": 1,
    }]
    # The body says so, directly under the As-of line.
    lines = result.finding.body.splitlines()
    assert lines[0].startswith("*As of ")
    assert lines[1] == "*6 regions carried, floor honoured — 1 kept by the floor.*"
    assert "— kept by the floor (order rank 17), 6.0h old" in result.finding.body


@pytest.mark.asyncio
async def test_a_country_run_carries_no_floor_and_no_floor_step(monkeypatch) -> None:
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "country_composition")
    rows = _rows(LIVE_2933F773[:8])
    result = await synth.run_method(
        rows,
        {"analyst_id": "country_composition", "target_id": "country_g20_in",
         "composition": True},
        _Deps(),
    )
    payload = result.finding.data["assembly"]
    assert "floor" not in payload
    assert all("order_rank" not in b for b in payload["blocks"])
    assert not any(s.get("phase") == "floor" for s in result.intermediate_steps)
    assert "floor" not in result.finding.body.splitlines()[1]


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_floor_is_persisted_and_read_back_off_the_real_row(
    pg_conn, monkeypatch,
) -> None:
    """The value claimed persisted, proven on the real driver: the world run's
    finding goes through the actor's own write path and ``floor`` is read back
    off ``analyst_outputs.data``."""
    from uuid import UUID, uuid4

    monkeypatch.setenv(ap.ASSEMBLY_ENV, "world_assessor")
    result = await synth.run_method(_rows(), dict(_WORLD_OPTIONS), _Deps())
    ctx = AnalystContext(
        analyst_id="world_assessor_f3_test",
        analyst_version="v" + uuid4().hex[:8],
        run_id=uuid4(),
        target_id="world_f3_test",
        target_version="abc123def456",
    )
    row, dlq = await write_finding(
        pg_conn,
        analyst_ctx=ctx,
        payload=result.finding,
        derived_from=[UUID(str(u)) for u in result.derived_from],
    )
    assert dlq is None and row is not None
    data = await pg_conn.fetchval(
        "SELECT data FROM analyst_outputs WHERE id = $1", row.id
    )
    data = data if isinstance(data, dict) else json.loads(data)
    floor = data["data"]["assembly"]["floor"]
    assert floor["honoured"] is True
    assert floor["version"] == "subject_group_floor.v1"
    assert sum(1 for g in floor["groups"] if g["carried"] >= 1) == 6
    blocks = data["data"]["assembly"]["blocks"]
    assert [b.get("carried_by_floor") for b in blocks].count(True) == 1
    stored = await pg_conn.fetchval(
        "SELECT derived_from FROM analyst_outputs WHERE id = $1", row.id
    )
    assert [str(u) for u in stored][:16] == [b["finding_id"] for b in blocks]
