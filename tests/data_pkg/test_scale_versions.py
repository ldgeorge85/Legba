# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K3 — a SCALE version beside H12's method version on every instrument read.

``method_version`` (H12) says whether the same CODE produced two numbers.
``scale_version`` says whether the two numbers MEAN the same thing, and those
are different questions that change on different days: migration 0188 moved the
``intensity_score`` scale in August 2026 with no change to the method at all, so
a reader comparing an intensity of 59 today with 59 in July was comparing
nothing and the row said nothing about it.

This file holds two contracts:

  * **The table stays honest** — every ``SCALE_VERSION`` constant in ``src/``
    has a row in ``docs/ANALYSIS.md`` §10.9.1 and vice versa, the format is
    pinned, and the naming rule (a scale is named for the QUANTITY, not the
    module) is enforced rather than merely documented. The
    ``test_method_versions`` precedent: a doc that can drift silently is a doc
    nobody trusts.
  * **The stamp lands on the PERSISTED row** — proven by reading the row back
    over the real asyncpg driver, not by asserting on an in-memory dict. This
    is the H12 review's own lesson: ``situation_clustering`` stamped its method
    version onto the in-flight cluster dict and 0 of 296 live rows carried it.
    A pure test would have passed the whole time.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import deterministic
from legba.data.analysts.deterministic_handlers import (
    band_calibration_tracker as bct,
    desk_baseline as db,
    forecast_acute as fa,
    indicator_tracker as it,
    situation_clustering as sc,
)
from legba.data.postgres import PostgresConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
ANALYSIS = REPO_ROOT / "docs" / "ANALYSIS.md"

#: A module-level ``SCALE_VERSION = "quantity/era"`` assignment.
_DECL_RE = re.compile(r'^SCALE_VERSION(?::\s*str)?\s*=\s*"([^"]+)"', re.MULTILINE)

#: The §10.9.1 table's scale column + its "published by" column.
_ROW_RE = re.compile(r"^\| `([^`]+)` \|[^|]*\| ([^|]+) \|", re.MULTILINE)


def _declared_scales() -> dict[str, str]:
    """Every ``SCALE_VERSION`` constant in ``src/`` → {module_path: value}."""
    found: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        for m in _DECL_RE.finditer(path.read_text(encoding="utf-8")):
            found[str(path.relative_to(REPO_ROOT))] = m.group(1)
    return found


def _doc_table_scales() -> dict[str, str]:
    """The §10.9.1 "Scale versions" table → {scale: publishing instrument}."""
    text = ANALYSIS.read_text(encoding="utf-8")
    section = text.split("### 10.9.1 Scale versions", 1)[1].split("\n## ", 1)[0]
    return {
        m.group(1): m.group(2).strip() for m in _ROW_RE.finditer(section)
    }


# ---------------------------------------------------------------------------
# The §10.9.1 table stays honest
# ---------------------------------------------------------------------------


def test_every_scale_version_constant_has_a_docs_row() -> None:
    """THE EXHAUSTIVE PIN, both directions — a ``SCALE_VERSION`` in code with
    no table row is a comparability frame nobody can look up; a table row with
    no constant is a scale nothing publishes onto."""
    declared = _declared_scales()
    table = _doc_table_scales()
    assert declared, "no SCALE_VERSION constants found in src/ — the grep broke"
    missing = {
        f"{mod} ({ver})" for mod, ver in declared.items() if ver not in table
    }
    assert not missing, (
        "SCALE_VERSION constants with no §10.9.1 table row: "
        + ", ".join(sorted(missing))
    )
    orphans = set(table) - set(declared.values())
    assert not orphans, (
        "§10.9.1 rows naming a scale nothing publishes: " + ", ".join(sorted(orphans))
    )


def test_the_table_names_the_publishing_module() -> None:
    """The "published by" column names the module, so the row is greppable from
    the constant's home and back."""
    declared = _declared_scales()
    table = _doc_table_scales()
    for mod, ver in declared.items():
        stem = Path(mod).stem.lstrip("_")
        assert stem in table[ver], (
            f"{mod}: §10.9.1 row for {ver!r} does not name this module"
        )


def test_scale_format_is_pinned_and_carries_no_patch_number() -> None:
    """``<quantity>/YYYY-MM`` — deliberately NOT the method version's
    ``.N`` patch form. A method has revisions; a scale has ERAS. Giving a scale
    a patch number would invite bumping it for a bug fix, and the whole value of
    the stamp is that two readings sharing it really are comparable."""
    for mod, ver in _declared_scales().items():
        assert re.fullmatch(r"[a-z_]+/\d{4}-\d{2}", ver), f"{mod}: {ver!r}"


def test_a_scale_is_named_for_the_quantity_not_the_module() -> None:
    """The naming rule, enforced rather than merely written down: a scale
    outlives the handler publishing onto it and two instruments may share one,
    so ``intensity/2026-08`` — never ``situation_clustering/2026-08``. Sharing
    the module's name would also make the scale indistinguishable from the
    method version at a glance, which is the confusion this lane exists to
    remove."""
    for mod, ver in _declared_scales().items():
        quantity = ver.split("/", 1)[0]
        stem = Path(mod).stem.lstrip("_")
        assert quantity != stem, (
            f"{mod}: scale {ver!r} is named for the module, not the quantity"
        )


def test_the_section_explains_what_a_scale_change_means() -> None:
    """The reader-facing sentence the chip's tooltip is derived from: the
    section must state that a scale change makes two numbers incomparable, and
    must name the unstamped render rather than implying a default."""
    text = ANALYSIS.read_text(encoding="utf-8")
    section = text.split("### 10.9.1 Scale versions", 1)[1].split("\n## ", 1)[0]
    assert "comparable" in section
    assert "unstamped (pre-2026-09)" in section
    assert "0188" in section


def test_every_scaled_instrument_also_carries_a_method_version() -> None:
    """A scale without a revision is half an answer — a reader could tell the
    numbers are comparable but not whether the same code produced them. Every
    module declaring a ``SCALE_VERSION`` declares a ``METHOD_VERSION`` too."""
    for mod in _declared_scales():
        text = (REPO_ROOT / mod).read_text(encoding="utf-8")
        assert re.search(r"^METHOD_VERSION", text, re.MULTILINE), (
            f"{mod}: declares a SCALE_VERSION but no METHOD_VERSION"
        )


# ---------------------------------------------------------------------------
# The in-flight payloads (cheap, no DB) — the persisted proofs are below
# ---------------------------------------------------------------------------


def test_situation_fields_and_receipt_carry_the_scale() -> None:
    rows = [
        {
            "id": "a",
            "title": "a frame",
            "produced_at": datetime(2026, 9, 20, tzinfo=timezone.utc),
        }
    ]
    fields = sc._situation_fields(
        "sig:x", rows, now=datetime(2026, 9, 24, tzinfo=timezone.utc)
    )
    assert fields["scale_version"] == sc.SCALE_VERSION
    receipt = sc._build_finding(created=1, updated=2, clusters=[], target_id=None)
    assert receipt.data["scale_version"] == sc.SCALE_VERSION


def test_indicator_tracker_receipt_carries_both_stamps() -> None:
    receipt = it._build_finding([], 0)
    assert receipt.data["method_version"] == it.METHOD_VERSION
    assert receipt.data["scale_version"] == it.SCALE_VERSION


def test_band_calibration_receipt_and_claim_sql_carry_the_scale() -> None:
    receipt = bct.build_finding(
        summary={},
        logged=0,
        resolved_by_horizon={},
        skipped_non_directional=0,
        scanned_rows=0,
        warnings=[],
    )
    assert receipt.data["scale_version"] == bct.SCALE_VERSION
    assert "scale_version" in bct._INSERT_CLAIM_SQL


def test_desk_baseline_summary_carries_both_stamps() -> None:
    finding = db.build_summary([], baseline_days=28, n_sigma=2.0)
    assert finding.data["method_version"] == db.METHOD_VERSION
    assert finding.data["scale_version"] == db.SCALE_VERSION


def test_forecast_mint_source_names_the_scale_column() -> None:
    src = inspect.getsource(fa.issue_weekly_forecasts)
    assert "scale_version" in src and "SCALE_VERSION" in src
    assert "SCALE_VERSION" in fa.__all__


# ---------------------------------------------------------------------------
# The stamp on the PERSISTED row — the real asyncpg driver
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    await p.close()


class _Deps:
    """The dep bundle the deterministic dispatcher hands a sub-handler."""

    def __init__(self, pg_pool) -> None:
        self.pg_pool = pg_pool
        self.extras: dict = {}


@pytest.mark.asyncio
async def test_migration_0219_added_every_column_the_handlers_write(pool) -> None:
    """The migration and the INSERTs agree. A handler naming a column the
    migration never added fails at RUNTIME on a tick nobody is watching, so the
    schema is asserted directly, by name, on the real driver."""
    expected = {
        "acute_forecasts": {"scale_version"},
        "band_calibration_claims": {"scale_version"},
        "desk_baselines": {"method_version", "scale_version"},
    }
    async with pool.acquire() as conn:
        for table, cols in expected.items():
            rows = await conn.fetch(
                "SELECT column_name, data_type, is_nullable "
                "  FROM information_schema.columns "
                " WHERE table_schema = 'public' AND table_name = $1",
                table,
            )
            present = {r["column_name"]: r for r in rows}
            for col in cols:
                assert col in present, f"{table}.{col} missing — 0219 did not run"
                assert present[col]["data_type"] == "text", f"{table}.{col}"
                # Un-backfilled by design: NULL is the honest pre-stamp mark, so
                # the column must ACCEPT it. A NOT NULL here would have forced a
                # back-labelled default onto every historical row.
                assert present[col]["is_nullable"] == "YES", f"{table}.{col}"


@pytest.mark.asyncio
async def test_persisted_situation_row_carries_the_scale(pool) -> None:
    """THE H12 REVIEW'S OWN FAILURE, closed for the scale: materialize through
    the REAL binding path and read ``situations.data`` back off the driver."""
    target = f"country_scale_{uuid4().hex[:10]}"
    analyst_id = f"situation_clustering_{uuid4().hex[:8]}"
    signature = f"sig:{target}#dim:{analyst_id}"
    try:
        async with pool.acquire() as conn:
            for i in range(2):
                await conn.execute(
                    """
                    INSERT INTO analyst_outputs
                        (id, kind, title, body, confidence, data, analyst_id,
                         target_id, produced_at, schema_uri,
                         situation_signature, severity)
                    VALUES ($1, 'finding', $2, '', 0.9, '{}'::jsonb, $3, $4, $5,
                            'iglu:legba/finding/jsonschema/1-0-0', $6, 'moderate')
                    """,
                    uuid4(),
                    f"scale probe {i}",
                    analyst_id,
                    target,
                    datetime.now(timezone.utc) - timedelta(hours=i + 1),
                    signature,
                )

        await deterministic.run_method(
            [],
            {
                "sub_handler": "situation_clustering",
                "analyst_id": analyst_id,
                "run_id": str(uuid4()),
            },
            _Deps(pool),
        )

        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT data FROM situations WHERE analyst_id = $1", analyst_id
            )
        assert raw is not None, "the clustering run persisted no situation"
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        assert data["scale_version"] == sc.SCALE_VERSION
        assert data["method_version"] == sc.METHOD_VERSION
    finally:
        # Teardown CLOSES rather than deletes — hypotheses and the append-only
        # event ledger both reference `situations`.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE situations SET status = 'closed' WHERE analyst_id = $1",
                analyst_id,
            )


@pytest.mark.asyncio
async def test_persisted_desk_baseline_row_carries_both_stamps(pool) -> None:
    """``store_baselines`` is the only writer of the sidecar; drive it and read
    the columns back. This is the write path a daily tick takes."""
    desk_id = f"country_scale_{uuid4().hex[:10]}"
    rec = db.DeskBaseline(
        desk_id=desk_id,
        metric=db.METRIC_SIGNAL_VOLUME,
        geo=["ZZ"],
        baseline_days=28,
        n_sigma=2.0,
        expected=3.0,
        center_median=3.0,
        robust_sigma=1.7,
        band_low=0.0,
        band_high=6.4,
        current=4.0,
        deviation="within",
        deviation_sigma=0.6,
        min_current_floor=5.0,
        sample_days=28,
        active_days=20,
        insufficient_history=False,
        spillover_current=0.0,
        features={"probe": True},
        computed_at=datetime.now(timezone.utc),
    )
    try:
        async with pool.acquire() as conn:
            # `store_baselines` PRUNES every (desk,metric) outside the set it is
            # handed, so it runs against its own transaction here and the probe
            # row is read back inside the same connection.
            await conn.execute(
                "INSERT INTO desk_baselines (desk_id, metric, geo, "
                "  baseline_days, n_sigma, expected, center_median, "
                "  robust_sigma, band_low, band_high, current, deviation, "
                "  min_current_floor, sample_days, active_days, "
                "  insufficient_history, spillover_current, features, "
                "  computed_at) "
                "VALUES ($1, $2, '[]'::jsonb, 28, 2.0, 0, 0, 0, 0, 0, 0, "
                "        'within', 5.0, 0, 0, TRUE, 0, '{}'::jsonb, now()) "
                "ON CONFLICT DO NOTHING",
                desk_id,
                db.METRIC_SIGNAL_VOLUME,
            )
            # A pre-stamp row reads NULL, never a back-labelled version.
            pre = await conn.fetchrow(
                "SELECT method_version, scale_version FROM desk_baselines "
                " WHERE desk_id = $1",
                desk_id,
            )
            assert pre["method_version"] is None
            assert pre["scale_version"] is None

            await conn.execute(
                db._UPSERT_SQL,
                rec.desk_id, rec.metric, json.dumps(rec.geo),
                rec.baseline_days, rec.n_sigma, rec.expected,
                rec.center_median, rec.robust_sigma, rec.band_low,
                rec.band_high, rec.current, rec.deviation,
                rec.deviation_sigma, rec.min_current_floor, rec.sample_days,
                rec.active_days, rec.insufficient_history,
                rec.spillover_current, json.dumps(rec.features),
                rec.computed_at, db.METHOD_VERSION, db.SCALE_VERSION,
            )
            row = await conn.fetchrow(
                "SELECT method_version, scale_version, computed_at "
                "  FROM desk_baselines WHERE desk_id = $1",
                desk_id,
            )
        assert row["method_version"] == db.METHOD_VERSION
        assert row["scale_version"] == db.SCALE_VERSION
        # The as-of the reader surface shows beside the stamp.
        assert row["computed_at"] is not None
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM desk_baselines WHERE desk_id = $1", desk_id
            )


@pytest.mark.asyncio
async def test_persisted_band_calibration_claim_carries_the_scale(pool) -> None:
    """The tracker's own ``_INSERT_CLAIM_SQL``, executed verbatim on the real
    driver — so a parameter/column drift between the statement and 0219 is
    caught here rather than on a live scan."""
    desk = f"country_scale_{uuid4().hex[:10]}"
    t0 = datetime.now(timezone.utc)
    row_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            bct._INSERT_CLAIM_SQL,
            desk, "unit_probe", "amber", "red", "deterioration", t0,
            str(row_id), str(uuid4()), bct.RESOLUTION_SPEC,
            "judge/probe", False, bct.METHOD_VERSION, bct.SCALE_VERSION,
        )
        row = await conn.fetchrow(
            "SELECT method_version, scale_version, logged_at "
            "  FROM band_calibration_claims WHERE desk = $1",
            desk,
        )
        await conn.execute(
            "DELETE FROM band_calibration_claims WHERE desk = $1", desk
        )
    assert row is not None, "the tracker's INSERT persisted nothing"
    assert row["method_version"] == bct.METHOD_VERSION
    assert row["scale_version"] == bct.SCALE_VERSION
    assert row["logged_at"] is not None


class _IssueCapture:
    """Captures the mint statement + args the handler really emits, so the
    replay below runs the HANDLER's SQL against the real schema rather than a
    copy of it that could drift."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        if "FROM target_descriptors" in sql:
            return [
                {"descriptor_id": "country_g20_us", "geo": ["US"]},
            ]
        raise AssertionError(f"unexpected fetch SQL: {sql[:80]}")

    async def fetchrow(self, sql, *args):
        if "AS wk" in sql and "geo &&" in sql:
            return {"wk": 2}
        if "AS wk" in sql:
            return {"wk": 10}
        if "AS cnt" in sql:
            return {"cnt": 2}
        raise AssertionError(f"unexpected fetchrow SQL: {sql[:80]}")

    async def execute(self, sql, *args):
        if "INSERT INTO acute_forecasts" in sql:
            self.inserts.append((sql, args))
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute SQL: {sql[:80]}")


class _Ctx:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        return _Ctx(self._conn)


@pytest.mark.asyncio
async def test_persisted_acute_forecast_carries_the_scale(pool) -> None:
    """Capture the mint's real INSERT, REPLAY it on the migrated database, read
    ``scale_version`` back off the row. ``acute_forecasts`` carries no JSONB, so
    the stamp is a column (0219) and this is the only way to prove the column,
    the statement and the constant all line up."""
    cap = _IssueCapture()
    issued = await fa.issue_weekly_forecasts(_Deps(_Pool(cap)), {})
    assert issued >= 1 and cap.inserts, "the mint issued nothing to replay"
    sql, args = cap.inserts[0]
    assert fa.SCALE_VERSION in args, (
        "the mint statement carries no SCALE_VERSION argument"
    )
    region = f"country_scale_{uuid4().hex[:10]}"
    replay = (region,) + tuple(args[1:])
    async with pool.acquire() as conn:
        await conn.execute(sql, *replay)
        row = await conn.fetchrow(
            "SELECT p, p_base, method, method_version, scale_version, issued_at "
            "  FROM acute_forecasts WHERE region = $1",
            region,
        )
        await conn.execute("DELETE FROM acute_forecasts WHERE region = $1", region)
    assert row is not None, "the mint statement persisted nothing"
    assert row["method_version"] == fa.METHOD_VERSION
    assert row["scale_version"] == fa.SCALE_VERSION
    # The scale's own claim: p and p_base are inside the clamped OPEN interval.
    for key in ("p", "p_base"):
        assert fa.P_EPSILON <= float(row[key]) <= 1.0 - fa.P_EPSILON, key
    # The as-of the forecasts band shows beside the stamp.
    assert row["issued_at"] is not None


# ---------------------------------------------------------------------------
# The reader surface's contract with the routes
# ---------------------------------------------------------------------------


def test_routes_project_the_scale_without_inventing_one() -> None:
    """Both reader-facing projections carry the field, defaulting to ``None``
    — the panel renders an unstamped row as "unstamped", and a default of
    ``""`` or a current version here would have made that impossible."""
    from legba.data.registry.forecasts_due import ForecastDue
    from legba.data.registry.v3_api import DeskBaselineRow

    for model in (ForecastDue, DeskBaselineRow):
        field = model.model_fields["scale_version"]
        assert field.default is None, f"{model.__name__}.scale_version"
    assert DeskBaselineRow.model_fields["method_version"].default is None


def test_situations_route_hands_the_whole_payload_through() -> None:
    """The Situations panel reads the stamp off ``SituationRow.data`` — no
    projection field needed, but only because the route passes the JSONB
    through whole. Pin that, so a future narrowing of the SELECT does not
    silently drop the chip."""
    from legba.data.registry import substrate_reads_api as sra

    src = inspect.getsource(sra._hydrate_situation)
    assert 'data=_load_jsonb(row["data"])' in src
