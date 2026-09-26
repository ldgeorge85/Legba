# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 7g-1 — ``scripts/load_collection.py``: the loader, and the firewall.

NO NETWORK. Every fetch here is served from the RECORDED provider fixtures
``tests/data_pkg/fixtures/collection_manifest_pilot/`` (two real World Bank v2
responses, two real EIA bulk records), through the verifier's own parsers —
``year_map_from_world_bank`` / ``year_map_from_eia_record`` — so the row
builder, the SQL, the ledger and the idempotency key are the REAL ones and
only the transport is substituted. Four (series, subject) pairs of the shipped
pilot manifest are covered by those recordings, and they are a deliberate
cross-section: a full ten-year series, a series the provider holds NOTHING for
in any year, an EIA production series, and an EIA series the provider FROZE in
2021.

What this file proves, in order:

1. **Rows.** The four pairs produce exactly the years the provider holds,
   each row carrying its collection, its history origin class, the URL and
   the digest of the file the number came from, and a provenance block. The
   unheld series produces NO ROWS — absence is absence, never a zero.
2. **Idempotency.** A second load of the same collection version writes
   nothing new and REPORTS that it wrote nothing.
3. **Resume.** A load interrupted after N pairs records its resume key and a
   non-``completed`` status; ``--resume`` finishes the rest and the final row
   set is identical to an uninterrupted run's.
4. **The document seam (SEAMS #62).** A ``documents_*`` loader kind refuses
   at the top of the load — before a single fetch.
5. **THE FIREWALL.** Loading a decade of history moves NOTHING on the eight
   fenced surfaces: no trigger state, no coalescer wake, no freshness or
   source-health row, no calibration row, no salience write, no alert row,
   no analyst output — and, structurally, the loader cannot even reach them:
   it imports no NATS client and no ingest-pipeline module, so there is no
   publish for a trigger to wake on.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import sys
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.config import PostgresConfig
from legba.data.schemas.collection import CollectionDescriptor

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PILOT = REPO_ROOT / "descriptors" / "collection_series_pilot_2016_2026.yaml"
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "collection_manifest_pilot"
LOADER_PATH = REPO_ROOT / "scripts" / "load_collection.py"


def _load_loader() -> Any:
    name = "load_collection"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, LOADER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lc = _load_loader()


#: The four (series, subject) pairs the verifier recorded, and what each is a
#: case OF. Nothing else is fetched here, because nothing else was recorded.
FIXTURE_PAIRS: dict[tuple[str, str], tuple[str, str]] = {
    ("wb.cpi_inflation_annual_pct", "IR"): (
        "world_bank_ir_cpi.json", "a full ten-year series",
    ),
    ("wb.external_debt_stocks_usd", "US"): (
        "world_bank_us_external_debt.json",
        "a series the provider holds NOTHING for",
    ),
    ("eia.crude_oil_production_tbpd", "US"): (
        "eia_intl_57_1_usa_tbpd_a.json", "an EIA bulk record",
    ),
    ("eia.crude_oil_exports_tbpd", "IR"): (
        "eia_intl_57_4_irn_tbpd_a.json", "an EIA series frozen in 2021",
    ),
}


# ---------------------------------------------------------------------------
# The fixture fetcher — real parsers, recorded bytes, no socket
# ---------------------------------------------------------------------------


class FixturePairFetcher:
    """Serves the recorded provider bytes through the REAL parsers."""

    def __init__(
        self,
        *,
        fail_on: tuple[str, str] | None = None,
        drop_record_time: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.prepared = False
        self._fail_on = fail_on
        self._drop_record_time = drop_record_time
        self.closed = False

    async def prepare(self, plan: Any) -> None:
        self.prepared = True

    async def fetch(self, series: Any, subject: str, iso3: str | None) -> Any:
        key = (series.series_id, subject)
        if self._fail_on == key:
            raise RuntimeError("provider went away mid-load")
        self.calls.append(key)
        name, _why = FIXTURE_PAIRS[key]
        raw = (FIXTURES / name).read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if series.fetch.mode == "json_api":
            if self._drop_record_time and isinstance(payload, list):
                payload[0].pop("lastupdated", None)
            by_year, last_updated = lc.vcm.year_map_from_world_bank(payload)
        else:
            if self._drop_record_time and isinstance(payload, dict):
                payload.pop("last_updated", None)
            by_year, last_updated = lc.vcm.year_map_from_eia_record(payload)
        return lc.FetchedSeries(
            by_year=by_year,
            provider_last_updated=last_updated,
            source_url=series.source_url_template.format(
                subject=subject, iso3=iso3 or ""
            ),
            sha256=lc.hashlib.sha256(raw).hexdigest(),
        )

    async def close(self) -> None:
        self.closed = True


def _pilot_descriptor(collection_id: str, **overrides: Any) -> CollectionDescriptor:
    body = yaml.safe_load(PILOT.read_text(encoding="utf-8"))
    body["identity"]["id"] = collection_id
    for key, value in overrides.items():
        body[key] = value
    return CollectionDescriptor.model_validate(body, strict=False)


def _fixture_plan(descriptor: CollectionDescriptor) -> Any:
    """The pilot plan, narrowed to the pairs the verifier actually recorded."""
    plan = lc.build_plan(descriptor, lc.vcm.parse_manifest(PILOT))
    plan.pairs = [
        (series, subject)
        for series, subject in plan.pairs
        if (series.series_id, subject) in FIXTURE_PAIRS
    ]
    assert len(plan.pairs) == len(FIXTURE_PAIRS)
    return plan


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.execute(
        "DELETE FROM observations WHERE collection_id LIKE 'tload_%'"
    )
    await c.execute(
        "DELETE FROM collection_loads WHERE collection_id LIKE 'tload_%'"
    )
    await c.close()


def _cid() -> str:
    return f"tload_{uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# 1. Rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_load_writes_the_years_the_provider_holds(conn) -> None:
    cid = _cid()
    descriptor = _pilot_descriptor(cid)
    plan = _fixture_plan(descriptor)
    fetcher = FixturePairFetcher()

    result = await lc.run_load(plan, fetcher, conn)

    assert result.status == "completed"
    assert result.pairs_done == 4
    assert fetcher.prepared and fetcher.closed

    rows = await conn.fetch(
        "SELECT series_id, subject, valid_from, valid_to, value, unit, "
        "       origin_class, source_url, sha256, provenance, record_time "
        "  FROM observations WHERE collection_id = $1 "
        " ORDER BY series_id, subject, valid_from",
        cid,
    )
    assert len(rows) == result.rows_written

    by_series: dict[str, list[Any]] = {}
    for row in rows:
        by_series.setdefault(row["series_id"], []).append(row)

    # The full ten-year World Bank series: 2016..2025, every year held.
    cpi = by_series["wb.cpi_inflation_annual_pct"]
    assert [r["valid_from"].year for r in cpi] == list(range(2016, 2026))
    assert all(r["valid_to"].month == 12 and r["valid_to"].day == 31 for r in cpi)
    assert all(r["unit"] == "pct_per_year" for r in cpi)

    # The EIA series the provider FROZE in 2021 stops at 2018 for Iran — the
    # missing tail is missing, not zero.
    exports = by_series["eia.crude_oil_exports_tbpd"]
    assert [r["valid_from"].year for r in exports] == [2016, 2017, 2018]

    # The series the provider holds NOTHING for produces NO ROW AT ALL.
    assert "wb.external_debt_stocks_usd" not in by_series

    # Every row carries its firewall stamp and its receipt.
    for row in rows:
        assert row["origin_class"] == "archive"
        assert row["source_url"].startswith("https://")
        assert len(row["sha256"]) == 64
        prov = json.loads(row["provenance"])
        assert prov["collection_version"] == descriptor.manifest_hash()
        assert prov["licence_class"] == "public"
        assert prov["provider"] in {"world_bank", "eia"}
        assert prov["provider_last_updated"]
        assert isinstance(prov["row_offset"], int)
        # record_time is the PROVIDER's stamp, never the load time.
        assert row["record_time"].year <= 2026


@pytest.mark.asyncio
async def test_the_load_ledger_records_what_happened(conn) -> None:
    cid = _cid()
    descriptor = _pilot_descriptor(cid)
    result = await lc.run_load(_fixture_plan(descriptor), FixturePairFetcher(), conn)

    row = await conn.fetchrow(
        "SELECT * FROM collection_loads WHERE collection_id = $1", cid
    )
    assert row is not None
    assert row["collection_version"] == descriptor.manifest_hash()
    assert row["loader_kind"] == "series_api"
    assert row["status"] == "completed"
    assert row["pairs_total"] == 4
    assert row["pairs_done"] == 4
    assert row["rows_written"] == result.rows_written
    assert row["finished_at"] is not None
    assert row["resume_key"] == "eia.crude_oil_exports_tbpd:IR"


@pytest.mark.asyncio
async def test_a_pair_with_no_provider_stamp_is_skipped_not_guessed(conn) -> None:
    """``record_time`` is when the PROVIDER published a figure.

    Stamping ``now()`` because the provider gave no revision date would turn a
    2016 number into something recorded today — the exact lie the bitemporal
    table exists to prevent. The pair is skipped, with its reason in the
    ledger.
    """
    cid = _cid()
    descriptor = _pilot_descriptor(cid)
    result = await lc.run_load(
        _fixture_plan(descriptor),
        FixturePairFetcher(drop_record_time=True),
        conn,
    )
    assert result.rows_written == 0
    assert result.rows_skipped == 4
    assert all("record_time is unknowable" in s for s in result.skipped)
    assert await conn.fetchval(
        "SELECT count(*) FROM observations WHERE collection_id = $1", cid
    ) == 0
    ledger = await conn.fetchrow(
        "SELECT provenance FROM collection_loads WHERE collection_id = $1", cid
    )
    assert len(json.loads(ledger["provenance"])["skipped"]) == 4


# ---------------------------------------------------------------------------
# 2. Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loading_twice_writes_the_same_rows_and_says_so(conn) -> None:
    cid = _cid()
    descriptor = _pilot_descriptor(cid)

    first = await lc.run_load(
        _fixture_plan(descriptor), FixturePairFetcher(), conn
    )
    after_first = await conn.fetch(
        "SELECT id, series_id, subject, valid_from, value FROM observations "
        " WHERE collection_id = $1 ORDER BY series_id, subject, valid_from",
        cid,
    )
    assert first.rows_written == len(after_first) > 0

    second = await lc.run_load(
        _fixture_plan(descriptor), FixturePairFetcher(), conn
    )
    after_second = await conn.fetch(
        "SELECT id, series_id, subject, valid_from, value FROM observations "
        " WHERE collection_id = $1 ORDER BY series_id, subject, valid_from",
        cid,
    )
    assert second.status == "completed"
    # Not "it did not crash" — it REPORTS that it wrote nothing, and the rows
    # are the same rows (same ids: nothing was deleted and re-inserted).
    assert second.rows_written == 0
    assert [r["id"] for r in after_second] == [r["id"] for r in after_first]
    assert await conn.fetchval(
        "SELECT count(*) FROM collection_loads WHERE collection_id = $1", cid
    ) == 1


# ---------------------------------------------------------------------------
# 3. Resume
# ---------------------------------------------------------------------------


def _small_batch(descriptor: CollectionDescriptor) -> CollectionDescriptor:
    """The pilot with ``loader.batch = 5``, so a flush happens mid-run.

    At the shipped batch of 200 a four-pair load flushes exactly once, at the
    end, and there is no partial state to resume FROM. Shrinking the batch is
    what makes the resume path reachable in a test that still writes real
    rows through the real SQL.
    """
    return descriptor.model_copy(
        update={"loader": descriptor.loader.model_copy(update={"batch": 5})}
    )


@pytest.mark.asyncio
async def test_an_interrupted_load_resumes_to_the_same_row_set(conn) -> None:
    cid = _cid()
    descriptor = _small_batch(_pilot_descriptor(cid))

    # A reference run, to compare against.
    reference = await lc.run_load(
        _fixture_plan(_pilot_descriptor(_cid())), FixturePairFetcher(), conn
    )

    plan = _fixture_plan(descriptor)
    third_pair = (plan.pairs[2][0].series_id, plan.pairs[2][1])
    with pytest.raises(RuntimeError, match="provider went away"):
        await lc.run_load(plan, FixturePairFetcher(fail_on=third_pair), conn)

    ledger = await conn.fetchrow(
        "SELECT status, resume_key, pairs_done, rows_written, error "
        "  FROM collection_loads WHERE collection_id = $1", cid,
    )
    assert ledger["status"] == "failed"
    assert "provider went away" in ledger["error"]
    # The first pair's ten rows flushed (batch 5); the second pair holds
    # nothing, so nothing flushed after it and the key stays on the first.
    assert ledger["resume_key"] == lc.pair_key(plan.pairs[0][0], plan.pairs[0][1])
    assert ledger["pairs_done"] == 1
    partial = await conn.fetchval(
        "SELECT count(*) FROM observations WHERE collection_id = $1", cid
    )
    assert partial == ledger["rows_written"] > 0

    resumed = await lc.run_load(
        _fixture_plan(descriptor), FixturePairFetcher(), conn, resume=True
    )
    assert resumed.status == "completed"
    assert resumed.pairs_done == 4

    final = await conn.fetchval(
        "SELECT count(*) FROM observations WHERE collection_id = $1", cid
    )
    assert final == reference.rows_written
    assert final > partial
    ledger = await conn.fetchrow(
        "SELECT status, pairs_done FROM collection_loads WHERE collection_id = $1",
        cid,
    )
    assert ledger["status"] == "completed" and ledger["pairs_done"] == 4


@pytest.mark.asyncio
async def test_resume_refetches_only_what_is_not_durable(conn) -> None:
    """Resume skips the pairs whose rows are COMMITTED, and no more.

    A pair that was fetched into a batch the crash took down is fetched
    again — which is free, because the identity key makes the second write a
    no-op. The rule is "never skip a pair whose rows are not on disk", not
    "never fetch twice".
    """
    cid = _cid()
    descriptor = _small_batch(_pilot_descriptor(cid))
    plan = _fixture_plan(descriptor)
    third_pair = (plan.pairs[2][0].series_id, plan.pairs[2][1])
    with pytest.raises(RuntimeError):
        await lc.run_load(plan, FixturePairFetcher(fail_on=third_pair), conn)

    fetcher = FixturePairFetcher()
    await lc.run_load(_fixture_plan(descriptor), fetcher, conn, resume=True)
    assert fetcher.calls == [
        (series.series_id, subject) for series, subject in plan.pairs[1:]
    ]


# ---------------------------------------------------------------------------
# 4. --dry-run and the document seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_touches_the_database_not_at_all(conn) -> None:
    cid = _cid()
    descriptor = _pilot_descriptor(cid)
    result = await lc.run_load(
        _fixture_plan(descriptor), FixturePairFetcher(), None, dry_run=True
    )
    assert result.status == "completed"
    assert result.rows_written > 0            # it says what it WOULD write
    assert await conn.fetchval(
        "SELECT count(*) FROM observations WHERE collection_id = $1", cid
    ) == 0
    assert await conn.fetchval(
        "SELECT count(*) FROM collection_loads WHERE collection_id = $1", cid
    ) == 0


@pytest.mark.asyncio
async def test_a_real_load_without_a_connection_refuses() -> None:
    """The dry run must be incapable of writing, not merely unwilling —
    so the inverse is refused rather than silently becoming a dry run."""
    descriptor = _pilot_descriptor(_cid())
    with pytest.raises(lc.CollectionLoadError, match="needs a database"):
        await lc.run_load(_fixture_plan(descriptor), FixturePairFetcher(), None)


@pytest.mark.asyncio
async def test_a_document_loader_kind_refuses_before_any_fetch(conn) -> None:
    """SEAMS #62 — nothing fetched, nothing written, and the error says why."""
    cid = _cid()
    # The pilot manifest with ONLY its loader kind swapped. `model_copy` is
    # deliberate: the schema's own bulk/series cross-check would refuse this
    # combination at parse time, and what is under test here is the LOADER's
    # refusal — the layer that protects a document manifest the schema would
    # happily accept.
    descriptor = _pilot_descriptor(cid)
    descriptor = descriptor.model_copy(
        update={
            "loader": descriptor.loader.model_copy(
                update={"kind": "documents_wacz"}
            )
        }
    )
    plan = _fixture_plan(descriptor)
    fetcher = FixturePairFetcher()
    with pytest.raises(lc.DocumentLoaderNotBuilt) as exc:
        await lc.run_load(plan, fetcher, conn)
    assert "SEAMS #62" in str(exc.value)
    # The CHECK the message names must be the one migration 0220 actually
    # creates (the pre-rename name would send a reader to a constraint that
    # does not exist).
    assert "signals_origin_class_history_writer_not_built" in str(exc.value)
    assert fetcher.calls == [] and not fetcher.prepared
    assert await conn.fetchval(
        "SELECT count(*) FROM collection_loads WHERE collection_id = $1", cid
    ) == 0


# ---------------------------------------------------------------------------
# 5. THE FIREWALL
# ---------------------------------------------------------------------------


#: Everything the eight fenced surfaces write. Nothing in this list may move
#: while a decade of history lands in `observations`.
FENCED_TABLES: tuple[str, ...] = (
    # reactive triggers / the coalescer
    "trigger_state",
    # freshness + source health
    "source_poll_outcomes", "source_track_records", "source_dossiers",
    "source_ratings", "source_credibility",
    # calibration
    "band_calibration_claims", "band_calibration_scan_state",
    "grader_calibrations",
    # surge detection (the 24h-bucket baselines the edge detector reads)
    "desk_baselines",
    # alerts
    "alert_sink_deliveries", "alert_trigger_watermarks",
    # cadence analysts and everything downstream of them
    "analyst_outputs", "analyst_traces", "signals", "facts", "events",
    "nexuses", "situations",
)


async def _counts(conn) -> dict[str, int]:
    return {
        table: int(await conn.fetchval(f"SELECT count(*) FROM {table}"))
        for table in FENCED_TABLES
    }


@pytest.mark.asyncio
async def test_a_decade_of_history_moves_nothing_on_the_fenced_surfaces(
    conn,
) -> None:
    """The tested trigger bypass (§4), read off the tables those surfaces write.

    A live signal is planted first so "unchanged" means "still exactly what I
    put there" rather than "still empty", and its salience and update stamp
    are checked individually — salience is a COLUMN on `signals`, so a row
    count alone would not notice a salience pass running over the load.
    """
    cid = _cid()
    sentinel = uuid4()
    await conn.execute(
        """
        INSERT INTO signals (id, source_id, schema_uri, payload, geo,
                             fetched_at, origin_class)
        VALUES ($1, 'source.firewall.sentinel',
                'iglu:legba/signal/jsonschema/3-0-0',
                '{"title": "a live signal"}'::jsonb, ARRAY['IR'],
                now(), 'live')
        """,
        sentinel,
    )
    try:
        before = await _counts(conn)
        before_sentinel = await conn.fetchrow(
            "SELECT salience, updated_at FROM signals WHERE id = $1", sentinel
        )

        result = await lc.run_load(
            _fixture_plan(_pilot_descriptor(cid)), FixturePairFetcher(), conn
        )
        assert result.rows_written > 0

        after = await _counts(conn)
        moved = {t: (before[t], after[t]) for t in FENCED_TABLES
                 if before[t] != after[t]}
        assert moved == {}, f"the load moved a fenced surface: {moved}"

        after_sentinel = await conn.fetchrow(
            "SELECT salience, updated_at FROM signals WHERE id = $1", sentinel
        )
        assert after_sentinel["salience"] == before_sentinel["salience"]
        assert after_sentinel["updated_at"] == before_sentinel["updated_at"]

        # …and the rows DID land, in the only two tables a load may touch.
        assert await conn.fetchval(
            "SELECT count(*) FROM observations WHERE collection_id = $1", cid
        ) == result.rows_written
        assert await conn.fetchval(
            "SELECT count(*) FROM collection_loads WHERE collection_id = $1", cid
        ) == 1
    finally:
        await conn.execute("DELETE FROM signals WHERE id = $1", sentinel)


def test_the_loader_cannot_reach_the_event_plane_at_all() -> None:
    """The firewall's FIRST line is structural, not behavioural.

    The load writes rows directly. If it could publish to NATS or call the
    ingest pipeline, a reactive trigger could wake on a ten-year backfill
    however carefully the SQL was written — so the check is on the import
    graph, where a future edit that reintroduces the path fails here rather
    than in production.
    """
    tree = ast.parse(LOADER_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_fragments = (
        "nats", "legba.runtime", "legba.data.filters", "legba.data.sources",
        "legba.data.analysts", "legba.data.outputs", "legba.data.alerts",
    )
    offenders = sorted(
        name for name in imported
        if any(fragment in name for fragment in forbidden_fragments)
    )
    assert offenders == [], (
        f"the collection loader imports the live plane: {offenders}"
    )
    # And the only legba modules it does reach are the config and the schema.
    legba_imports = sorted(n for n in imported if n.startswith("legba"))
    assert legba_imports == [
        "legba.data.config", "legba.data.schemas.collection",
    ]


def test_the_loader_writes_only_observations_and_its_own_ledger() -> None:
    """Every SQL verb in the loader, read off the source.

    A grep is a weak test in general; here it is the right one, because the
    claim being made is about the SET of tables this program can write, and
    that set is small enough to enumerate.
    """
    text = LOADER_PATH.read_text(encoding="utf-8")
    written: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        for verb in ("INSERT INTO ", "UPDATE ", "DELETE FROM "):
            if upper.startswith(verb):
                written.add(stripped[len(verb):].split()[0].rstrip(";"))
    assert written == {"observations", "collection_loads"}, written
