# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-3 — Brazil Energy Predictor descriptor end-to-end test.

Wires the production ``descriptors/analyst_india_energy_predictor.yaml``
descriptor through the real path:

  registry register → analyst_deps_builder._build_predictor →
  predictor.run_method (real AutoARIMA / naive fallback) →
  write_analyst_output(OutputKind.PREDICTION) → predictions row.

Daprd is NOT in the loop here.  The actor's run path
(``AnalystActor.run`` in :mod:`legba.runtime.dapr_actors`) is exercised
in :mod:`tests.runtime.test_spike_integration`'s gate-9 test
(``test_gate9_predictor_writes_prediction_kind_through_daprd``);
this file complements that by:

  * Mirroring the **production** descriptor body (loaded from the YAML
    file) rather than the spike's bespoke fixture, so a descriptor-
    schema regression here surfaces as a fail.
  * Covering the **naive-mean fallback path** when fewer than
    ``predictor.MIN_OBSERVATIONS`` daily buckets are available.
  * Covering the **narrative-LLM-degraded path** when the optional
    narrative handler raises mid-run.

Together with gate 9, this closes the K-3 "predictor activation gate"
deliverable: the descriptor activates, the predictor fires, a row
lands in ``predictions`` with sensible values, and the failure modes
have explicit test coverage.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts.predictor import (
    DEFAULT_HORIZON_DAYS,
    MIN_OBSERVATIONS,
    PredictorDeps,
    run_method as predictor_run_method,
)
from legba.data.provenance._core import AnalystContext
from legba.data.provenance.kinds import OutputKind
from legba.data.provenance.writes import write_analyst_output
from legba.data.registry.audit import AuditLogger
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import load_default_identity
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.schemas.analyst import AnalystDescriptor, AnalystKind


# ---------------------------------------------------------------------------
# Module paths
# ---------------------------------------------------------------------------


DESCRIPTOR_YAML = (
    Path(__file__).resolve().parents[2]
    / "descriptors"
    / "analyst_india_energy_predictor.yaml"
)

SIGNAL_SCHEMA_URI = "iglu:legba/signal/jsonschema/2-0-0"
PREDICTION_SCHEMA_URI = "iglu:legba/prediction/jsonschema/2-0-0"
TARGET_ID = "india_energy_infra"
TARGET_VERSION = "f" * 64  # arbitrary 64-hex; signals don't FK the target row


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):  # type: ignore[no-untyped-def]
    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1,
        max_size=4,
    )
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def pg_store(migrated_pg):  # type: ignore[no-untyped-def]
    from legba.data.postgres import PostgresStore

    store = PostgresStore(migrated_pg)
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def descriptor_registry(pg_store):  # type: ignore[no-untyped-def]
    """Real DescriptorRegistry, no NATS — the registry's NATS publish path is
    best-effort and gated on ``nats_store`` being non-None."""
    identity = load_default_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    await vocab.refresh()
    reg = DescriptorRegistry(
        pg_store,
        nats_store=None,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await reg.start()
    try:
        yield reg
    finally:
        await reg.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_descriptor_yaml() -> dict[str, Any]:
    """Load the production predictor descriptor YAML + stamp a placeholder
    version (the registry overwrites with the real content hash)."""
    with open(DESCRIPTOR_YAML) as f:
        body = yaml.safe_load(f)
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


async def _register_predictor_descriptor(
    descriptor_registry: DescriptorRegistry,
    *,
    actor: str = "k3_predictor_e2e",
) -> AnalystDescriptor:
    """POST-equivalent: parse YAML → AnalystDescriptor → register.

    Mirrors what ``scripts/bringup_register_brazil_predictor.py`` does
    over HTTP; running through ``DescriptorRegistry.register`` directly
    keeps the test in-process (no HTTP fixture needed).

    Idempotent: a prior test in the same session leaves a head row
    behind; we just return the already-registered head instead of
    raising ``VersionConflict``.  This matches the bringup script's
    "skip if head exists" semantics.
    """
    from legba.data.registry.errors import DescriptorNotFound, VersionConflict

    body = _load_descriptor_yaml()
    # ``strict=False`` matches the registry's :func:`_parse_descriptor` —
    # over the HTTP wire JSON-native types (string enums, ISO datetimes)
    # coerce into the strict-by-default pydantic models.  Without this
    # the YAML-loaded ``state: active`` string is rejected for not being
    # a :class:`LifecycleState` instance.
    descriptor = AnalystDescriptor.model_validate(body, strict=False)
    try:
        await descriptor_registry.register(descriptor, actor=actor)
    except VersionConflict:
        # Head already present from an earlier test — fine, use it.
        pass
    try:
        return await descriptor_registry.get_typed(
            descriptor.identity.id, family=Family.ANALYST,
        )
    except DescriptorNotFound as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            f"register succeeded but get_typed 404'd: {exc}"
        ) from exc


async def _seed_signals(
    pg_pool: asyncpg.Pool,
    *,
    target_id: str,
    days: int,
    distinct_daily_buckets: int,
) -> list[UUID]:
    """Insert ``days`` synthetic signals spread across ``distinct_daily_buckets``
    distinct calendar days within the last 24h.

    Why this shape: the runtime's
    :func:`legba.runtime.dapr_actors._read_substrate_slice` reader hard-codes a
    24h substrate window (it ignores ``subscription.time_window``).  Inside
    that window the predictor's daily bucketer counts distinct calendar
    days — so ``distinct_daily_buckets`` controls whether AutoARIMA
    (≥ MIN_OBSERVATIONS) or the naive-mean fallback fires.

    For coverage we let the caller invoke the predictor directly with
    inputs we choose explicitly — ``_call_predictor`` below — so the
    24h window doesn't constrain us here.  This helper just lays down
    real ``signals`` rows so the ``predictions.derived_from`` FK-like
    UUID array has substrate to point at.
    """
    now = datetime.now(tz=timezone.utc)
    ids: list[UUID] = []
    async with pg_pool.acquire() as conn:
        for d in range(days):
            # Map each signal to a calendar day in [-distinct_daily_buckets+1, 0].
            bucket = d % distinct_daily_buckets
            day = now - timedelta(
                days=distinct_daily_buckets - 1 - bucket,
                hours=(d * 0.5) % 23,
            )
            new_id = uuid4()
            # Source-first pivot (migration 0024): `signals` are source-owned +
            # target-agnostic. Seed the source-first shape (source_id/modality/
            # payload/content_hash/fetched_at) — `derived_from` is a plain
            # uuid[] with no FK, so these ids are valid ancestors for the
            # prediction's derived_from array.
            await conn.execute(
                """
                INSERT INTO signals (
                    id, source_id, source_version, produced_by_kind,
                    fetched_at, modality, payload, content_hash,
                    canonical_url, language_hint, derived_from, schema_uri
                ) VALUES (
                    $1, $2, '', 'source',
                    $3, 'text', $4::jsonb, $5,
                    $6, 'en', '{}'::uuid[], $7
                )
                """,
                new_id,
                "rss_main",
                day,
                json.dumps({
                    "summary": f"test signal {d}",
                    "sentiment": 0.0,
                    "title": f"K-3 e2e test signal {d}",
                }),
                f"k3-e2e-{new_id.hex}",
                f"https://example.invalid/k3-e2e/{new_id}",
                SIGNAL_SCHEMA_URI,
            )
            ids.append(new_id)
    return ids


def _build_inputs(
    signal_ids: list[UUID],
    *,
    distinct_daily_buckets: int,
) -> list[dict[str, Any]]:
    """Build the ``inputs`` list the predictor's ``run_method`` consumes.

    Distributes ``signal_ids`` across ``distinct_daily_buckets`` calendar
    days, anchored backward from "today" so the daily aggregator sees
    a contiguous N-day series.  Counts per day are uneven on purpose so
    the AutoARIMA fit has a non-trivial signal to chew on.
    """
    base = datetime.now(tz=timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0,
    )
    inputs: list[dict[str, Any]] = []
    for i, sid in enumerate(signal_ids):
        bucket = i % distinct_daily_buckets
        produced_at = base - timedelta(days=distinct_daily_buckets - 1 - bucket)
        inputs.append({
            "id": str(sid),
            "produced_at": produced_at,
            "title": f"test signal {i}",
            "data": {"sentiment": 0.05 * (i - len(signal_ids) / 2)},
        })
    return inputs


async def _write_prediction_row(
    pg_pool: asyncpg.Pool,
    *,
    method_result: Any,
    descriptor: AnalystDescriptor,
    derived_from: list[UUID],
) -> UUID:
    """Persist the predictor's method_result through the same OutputKind.
    PREDICTION write path the actor uses.

    Source-first pivot (migration 0024) DROPPED the dedicated ``predictions``
    table; OutputKind.PREDICTION now routes through ``write_analyst_output``
    into ``analyst_outputs`` (kind='prediction'), with the typed PredictionPayload
    dump landing in the ``data`` JSONB column.

    Returns the new prediction row's UUID.  Asserts the write succeeded
    (no DLQ entry).
    """
    # Pull the typed PredictionPayload-dump out of the finding's
    # ``data["prediction"]`` blob — that's the contract the dispatch
    # table in dapr_actors._PAYLOAD_SELECTORS encodes for PREDICTION.
    finding = method_result.finding
    payload = finding.data.get("prediction")
    assert payload is not None, "predictor must populate finding.data['prediction']"

    analyst_ctx = AnalystContext(
        analyst_id=descriptor.identity.id,
        analyst_version=descriptor.identity.version,
        run_id=uuid4(),
        target_id=TARGET_ID,
        target_version=TARGET_VERSION,
    )
    async with pg_pool.acquire() as conn:
        row, dlq = await write_analyst_output(
            conn,
            analyst_ctx=analyst_ctx,
            kind=OutputKind.PREDICTION,
            output_payload=payload,
            derived_from=derived_from,
        )
    assert dlq is None, f"prediction write hit DLQ: {dlq!r}"
    assert row is not None
    return row.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_predictor_descriptor_registers_with_real_yaml(
    descriptor_registry: DescriptorRegistry,
) -> None:
    """The production YAML descriptor parses + registers cleanly.

    Catches schema drift in the descriptor body the moment it'd ship.
    """
    typed = await _register_predictor_descriptor(descriptor_registry)

    assert typed.identity.id == "india_energy_predictor"
    assert typed.identity.kind == AnalystKind.PREDICTOR
    assert typed.identity.state.value == "active"
    assert typed.method.kind == "stat_forecaster"
    assert typed.method.llm.get("horizon_days") == 7
    assert typed.method.llm.get("ci_level") == 90
    # No primary LLM stack-ref — narrative is OFF on this descriptor.
    assert typed.method.llm.get("primary") is None
    # Predictor MUST be subscribed to india_energy_infra signals.
    assert typed.subscription.targets is not None
    assert typed.subscription.targets.predicate == (
        'target_id() == "india_energy_infra"'
    )
    assert "signal" in typed.subscription.targets.data_types
    # Cadence: fallback_schedule is DELIBERATELY NULLED (P0-T6 SEQUENCED
    # FREEZE, docs/SEAMS.md #32) — "a forecast is a CLAIM that must be SCORED
    # before it ships." A null schedule means dapr_actors' on-activate gate
    # registers NO reminder, so the freeze is mechanically self-enforcing.
    # RETURNS at P4 behind the Brier scoreboard alongside country_predictor
    # and the GEPA optimizer (same freeze pattern). This assertion now guards
    # the freeze holding, not the pre-freeze "*/30 * * * *" beat.
    assert typed.cadence.fallback_schedule is None
    assert typed.cadence.cooldown_seconds == 900


@pytest.mark.asyncio
async def test_predictor_happy_path_writes_prediction_row(
    pg_pool: asyncpg.Pool,
    descriptor_registry: DescriptorRegistry,
) -> None:
    """End-to-end happy path:

      1. Seed 14 signals across 14 distinct calendar days (above
         MIN_OBSERVATIONS so AutoARIMA fires).
      2. Run the registered predictor's run_method.
      3. Write the PREDICTION payload via write_analyst_output.
      4. Assert the predictions row lands with sensible values:
         finite point_estimate, ci_lower <= point <= ci_upper,
         category=event_count_forecast, derived_from populated.

    Mirrors what the actor does at run time, minus daprd.
    """
    descriptor = await _register_predictor_descriptor(descriptor_registry)
    seeded = await _seed_signals(
        pg_pool, target_id=TARGET_ID, days=14, distinct_daily_buckets=14,
    )
    inputs = _build_inputs(seeded, distinct_daily_buckets=14)

    deps = PredictorDeps(
        llm=None,  # stat-only mode matches the descriptor
        horizon_days=int(descriptor.method.llm.get("horizon_days") or 7),
        ci_level=int(descriptor.method.llm.get("ci_level") or 80),
    )
    options = {
        "analyst_id": descriptor.identity.id,
        "analyst_version": descriptor.identity.version,
        "run_id": str(uuid4()),
        "target_id": TARGET_ID,
    }
    method_result = await predictor_run_method(inputs, options, deps)

    finding = method_result.finding
    # Sanity: method should be auto_arima OR naive_mean (defensive — if
    # statsforecast errors out _forecast_arima falls back).  In either
    # case the typed payload + extras must be present.
    forecast_method = finding.data.get("forecast_method")
    assert forecast_method in ("auto_arima", "naive_mean"), forecast_method
    assert finding.data["horizon_days"] == DEFAULT_HORIZON_DAYS
    assert finding.data["ci_level"] == 90
    pred = finding.data["prediction"]
    assert isinstance(pred["point_estimate"], (int, float))
    assert pred["ci_lower"] <= pred["point_estimate"] <= pred["ci_upper"]
    # Naive-mean clips lo to 0 — check the bound:
    assert pred["ci_lower"] >= 0.0
    assert len(pred["horizon_series"]) == DEFAULT_HORIZON_DAYS

    # Persist + assert the predictions row exists.
    pred_id = await _write_prediction_row(
        pg_pool,
        method_result=method_result,
        descriptor=descriptor,
        derived_from=seeded,
    )
    # Source-first pivot: the row lands in `analyst_outputs` (kind='prediction');
    # hypothesis/category/region live inside the `data` JSONB blob.
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, kind, data, target_id, "
            "       analyst_id, derived_from, schema_uri, confidence "
            "FROM analyst_outputs WHERE id = $1",
            pred_id,
        )
    assert row is not None
    assert row["kind"] == "prediction"
    data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
    assert data["category"] == "event_count_forecast"
    assert data["region"] == TARGET_ID
    assert row["target_id"] == TARGET_ID
    assert row["analyst_id"] == descriptor.identity.id
    assert PREDICTION_SCHEMA_URI in (row["schema_uri"] or "")
    derived = [UUID(str(u)) for u in (row["derived_from"] or [])]
    assert set(derived).issubset(set(seeded)), derived
    # Confidence shrinks with CI width; with 14d of synthetic data the
    # bands aren't degenerate, so we expect 0.1 ≤ confidence ≤ 0.9 per
    # the predictor's bounding.
    assert 0.1 <= float(row["confidence"]) <= 0.9


@pytest.mark.asyncio
async def test_predictor_low_signal_count_falls_back_to_naive_mean(
    pg_pool: asyncpg.Pool,
    descriptor_registry: DescriptorRegistry,
) -> None:
    """Fewer than ``MIN_OBSERVATIONS`` daily buckets → naive-mean
    fallback fires.  Predictor still emits a PREDICTION payload —
    operators see a forecast with wider bands rather than no row at all.
    """
    descriptor = await _register_predictor_descriptor(descriptor_registry)
    # 2 distinct daily buckets — below MIN_OBSERVATIONS=5.
    low_count_days = 2
    assert low_count_days < MIN_OBSERVATIONS
    seeded = await _seed_signals(
        pg_pool, target_id=TARGET_ID,
        days=6, distinct_daily_buckets=low_count_days,
    )
    inputs = _build_inputs(seeded, distinct_daily_buckets=low_count_days)

    method_result = await predictor_run_method(
        inputs,
        options={
            "analyst_id": descriptor.identity.id,
            "analyst_version": descriptor.identity.version,
            "run_id": str(uuid4()),
            "target_id": TARGET_ID,
        },
        deps=PredictorDeps(llm=None, horizon_days=7, ci_level=90),
    )
    assert method_result.finding.data["forecast_method"] == "naive_mean"
    pred = method_result.finding.data["prediction"]
    assert pred["method"] == "naive_mean"
    assert pred["ci_lower"] <= pred["point_estimate"] <= pred["ci_upper"]
    # Naive-mean's z-table for 90% CI uses z≈1.645.  With degenerate
    # series the predictor enforces a half_width floor of 0.5, so the
    # interval is never zero-width.
    assert pred["ci_upper"] > pred["ci_lower"]

    pred_id = await _write_prediction_row(
        pg_pool,
        method_result=method_result,
        descriptor=descriptor,
        derived_from=seeded,
    )
    async with pg_pool.acquire() as conn:
        landed = await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs "
            "WHERE id = $1 AND kind = 'prediction'",
            pred_id,
        )
    assert landed == 1


class _RaisingLLMHandler:
    """LLM handler whose ``chat_complete`` always raises.

    Models the realistic failure mode: vLLM endpoint returns 5xx mid-
    cycle.  The predictor's ``_generate_narrative`` catches Exception,
    falls back to ``_NARRATIVE_FALLBACK``, and proceeds — the numeric
    forecast is the load-bearing output and the narrative is decorative
    per the L-174 contract.
    """

    subprovider = "test-raising"

    async def chat_complete(
        self,
        messages,
        *,
        max_tokens=None,
        temperature=None,
        system=None,
        **kwargs,
    ):
        raise RuntimeError("simulated LLM outage")


@pytest.mark.asyncio
async def test_predictor_narrative_llm_failure_degrades_gracefully(
    pg_pool: asyncpg.Pool,
    descriptor_registry: DescriptorRegistry,
) -> None:
    """Narrative LLM raises → predictor falls back to the deterministic
    narrative string and still emits a PREDICTION payload.

    The descriptor here uses the production YAML (LLM not wired at the
    descriptor level), but a future revision that attaches a primary
    StackRef must not break the predictor's run cycle on LLM failure —
    we exercise that contract by injecting a raising handler directly
    via ``PredictorDeps(llm=...)``.
    """
    descriptor = await _register_predictor_descriptor(descriptor_registry)
    seeded = await _seed_signals(
        pg_pool, target_id=TARGET_ID, days=10, distinct_daily_buckets=10,
    )
    inputs = _build_inputs(seeded, distinct_daily_buckets=10)

    method_result = await predictor_run_method(
        inputs,
        options={
            "analyst_id": descriptor.identity.id,
            "analyst_version": descriptor.identity.version,
            "run_id": str(uuid4()),
            "target_id": TARGET_ID,
        },
        deps=PredictorDeps(
            llm=_RaisingLLMHandler(),
            horizon_days=7,
            ci_level=90,
        ),
    )
    finding = method_result.finding
    # Narrative fell back to the canned terminal string; the finding's
    # body carries that string + the hypothesis prefix.
    pred = finding.data["prediction"]
    assert "no narrative available" in pred["narrative"]
    assert "no narrative available" in finding.body
    # The numeric forecast still landed.
    assert pred["point_estimate"] >= 0.0
    assert pred["ci_lower"] <= pred["point_estimate"] <= pred["ci_upper"]
    # Usage dict empty when the call failed.
    assert method_result.usage == {}

    pred_id = await _write_prediction_row(
        pg_pool,
        method_result=method_result,
        descriptor=descriptor,
        derived_from=seeded,
    )
    async with pg_pool.acquire() as conn:
        landed = await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs "
            "WHERE id = $1 AND kind = 'prediction'",
            pred_id,
        )
    assert landed == 1
