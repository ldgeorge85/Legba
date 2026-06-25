# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :class:`legba.data.sources.gdelt.GDELTBigQuerySourceHandler` (L-131).

Coverage:

  * Config schema validation (FIPS country shape, regex compile, root-code
    padding, tone range ordering, cost caps).
  * Conformance with the L-102 source-kind class-var contract.
  * SQL composition: every filter mode produces the expected WHERE clauses
    and parameter bindings.
  * Cursor advance: pulls record ``gdelt_last_dateadded`` from the highest
    DATEADDED seen; subsequent pulls feed that back in.
  * Cost cap enforcement: dry-run estimate over ``cost_cap_bytes_per_pull``
    raises :class:`CostCapExceeded`; soft-warn threshold logs a warning.
  * Daily-cap accounting: bytes accumulate in state_store; pulls that would
    push past ``daily_cap_bytes`` refuse.
  * Healthcheck: dry-run path returns the cost estimate; missing credential
    or BigQuery error returns ``unhealthy``.
  * Live integration: optional, against real BigQuery if
    ``LEGBA_GCP_SERVICE_ACCOUNT_JSON`` env var is set.

Everything is exercised against an injected fake BigQuery client — the real
``google-cloud-bigquery`` package is an optional dependency and is *not*
required for the unit suite.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest
from pydantic import ValidationError

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
    SourceHealth,
)
from legba.data.sources.gdelt import (
    EVENT_COLUMNS,
    EVENTS_TABLE,
    HARD_BYTES_CAP,
    WARN_BYTES_THRESHOLD,
    CostCapExceeded,
    GDELTBigQuerySourceHandler,
    GDELTConfig,
    build_gdelt_sql,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


SERVICE_ACCOUNT_FIXTURE: dict[str, Any] = {
    "type": "service_account",
    "project_id": "legba-test-project",
    "private_key_id": "deadbeef",
    "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    "client_email": "legba-gdelt@legba-test-project.iam.gserviceaccount.com",
    "client_id": "0",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _sa_json_bytes() -> bytes:
    return json.dumps(SERVICE_ACCOUNT_FIXTURE).encode("utf-8")


async def _resolve_sa(secret_id: str) -> bytes:
    assert secret_id == "creds.gdelt.bigquery_sa"
    return _sa_json_bytes()


class FakeQueryJob:
    """Stand-in for ``google.cloud.bigquery.QueryJob``."""

    def __init__(
        self,
        *,
        total_bytes_processed: int = 1_000_000,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.total_bytes_processed = total_bytes_processed
        self._rows = rows or []
        self.calls = 0

    def result(self) -> list[dict[str, Any]]:
        self.calls += 1
        return self._rows


class FakeBQClient:
    """Records queries; returns configured dry-run + real jobs."""

    def __init__(
        self,
        *,
        dry_run_bytes: int = 50_000_000,
        rows: list[dict[str, Any]] | None = None,
        raise_on_real: Exception | None = None,
        raise_on_dry: Exception | None = None,
    ) -> None:
        self.dry_run_bytes = dry_run_bytes
        self._rows = rows or []
        self.raise_on_real = raise_on_real
        self.raise_on_dry = raise_on_dry
        self.recorded: list[dict[str, Any]] = []

    def query(self, sql: str, job_config: Any = None) -> FakeQueryJob:
        dry_run = bool(getattr(job_config, "dry_run", False))
        self.recorded.append(
            {
                "sql": sql,
                "dry_run": dry_run,
                "params": list(getattr(job_config, "query_parameters", []) or []),
            }
        )
        if dry_run:
            if self.raise_on_dry is not None:
                raise self.raise_on_dry
            return FakeQueryJob(total_bytes_processed=self.dry_run_bytes, rows=[])
        if self.raise_on_real is not None:
            raise self.raise_on_real
        return FakeQueryJob(
            total_bytes_processed=self.dry_run_bytes, rows=list(self._rows)
        )


def _make_handler(
    cfg: GDELTConfig | None = None,
    *,
    client: FakeBQClient | None = None,
    resolver: Callable[[str], "Any"] | None = None,
) -> tuple[GDELTBigQuerySourceHandler, FakeBQClient]:
    cfg = cfg or GDELTConfig(bq_credentials_secret="creds.gdelt.bigquery_sa")
    bq = client or FakeBQClient(dry_run_bytes=10_000_000)

    def factory(sa_info: dict[str, Any], project: str | None, location: str) -> FakeBQClient:
        # Record so tests can assert the credential was decoded.
        assert sa_info["project_id"] == SERVICE_ACCOUNT_FIXTURE["project_id"]
        assert location == cfg.bq_location
        return bq

    handler = GDELTBigQuerySourceHandler(
        cfg,
        credential_resolver=resolver or _resolve_sa,
        client_factory=factory,
    )
    return handler, bq


def _ctx(
    state: InMemoryStateStore | None = None,
    *,
    cfg: GDELTConfig | None = None,
) -> SourceContext:
    return SourceContext(
        target_id="india_energy",
        target_version="abcdef0123456789",
        source_id="gdelt_brazil",
        config=cfg or GDELTConfig(bq_credentials_secret="creds.gdelt.bigquery_sa"),
        state_store=state or InMemoryStateStore(),
    )


def _sample_row(
    *,
    global_event_id: int = 100,
    sqldate: int = 20250520,
    date_added: int = 20250520180000,
    country: str = "BR",
    event_root: str = "14",
) -> dict[str, Any]:
    return {
        "GLOBALEVENTID": global_event_id,
        "SQLDATE": sqldate,
        "MonthYear": 202505,
        "Year": 2025,
        "Actor1Code": "BRAGOV",
        "Actor1Name": "BRAZIL GOVERNMENT",
        "Actor1CountryCode": "BRA",
        "Actor1Type1Code": "GOV",
        "Actor2Code": "BRAENV",
        "Actor2Name": "ENVIRONMENTALIST",
        "Actor2CountryCode": "BRA",
        "Actor2Type1Code": "ENV",
        "IsRootEvent": 1,
        "EventCode": "145",
        "EventBaseCode": "145",
        "EventRootCode": event_root,
        "QuadClass": 3,
        "GoldsteinScale": -6.5,
        "NumMentions": 12,
        "NumSources": 3,
        "NumArticles": 4,
        "AvgTone": -3.2,
        "ActionGeo_Type": 4,
        "ActionGeo_FullName": "Brasilia, Distrito Federal, Brazil",
        "ActionGeo_CountryCode": country,
        "ActionGeo_ADM1Code": "BR07",
        "ActionGeo_Lat": -15.78,
        "ActionGeo_Long": -47.93,
        "DATEADDED": date_added,
        "SOURCEURL": f"https://news.example/{global_event_id}",
    }


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class TestGDELTConfigValidation:
    def test_minimal_config_accepts_just_credential(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="creds.gdelt.bq")
        assert cfg.lookback_minutes == 15
        assert cfg.cameo_country is None
        assert cfg.cost_cap_bytes_per_pull == WARN_BYTES_THRESHOLD

    def test_cameo_country_normalized_to_upper(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x", cameo_country="br")
        assert cfg.cameo_country == "BR"

    def test_cameo_country_rejects_three_letters(self) -> None:
        # FIPS 10-4 is two letters; ISO-3 is rejected.
        with pytest.raises(ValidationError):
            GDELTConfig(bq_credentials_secret="x", cameo_country="BRA")

    def test_actor_filter_rejects_bad_regex(self) -> None:
        with pytest.raises(ValidationError):
            GDELTConfig(bq_credentials_secret="x", actor_filter="((((")

    def test_event_root_codes_zero_padded(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x", event_root_codes=["1", "14"])
        assert cfg.event_root_codes == ["01", "14"]

    def test_event_root_codes_reject_non_digits(self) -> None:
        with pytest.raises(ValidationError):
            GDELTConfig(bq_credentials_secret="x", event_root_codes=["bad"])

    def test_tone_filter_min_greater_than_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GDELTConfig(
                bq_credentials_secret="x",
                tone_filter=(5.0, -5.0),
            )

    def test_daily_cap_below_pull_cap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GDELTConfig(
                bq_credentials_secret="x",
                cost_cap_bytes_per_pull=2_000_000_000,
                daily_cap_bytes=1_000_000_000,
            )

    def test_cost_cap_bounded_by_hard_cap(self) -> None:
        with pytest.raises(ValidationError):
            GDELTConfig(
                bq_credentials_secret="x",
                cost_cap_bytes_per_pull=HARD_BYTES_CAP * 10,
            )


# ---------------------------------------------------------------------------
# Class-var conformance to L-102 §2
# ---------------------------------------------------------------------------


def test_classvar_surface_matches_l102_contract() -> None:
    assert GDELTBigQuerySourceHandler.kind == "gdelt_query"
    assert GDELTBigQuerySourceHandler.family == "source"
    assert GDELTBigQuerySourceHandler.schema_version == "legba/source.gdelt_query/1-0-0"
    assert GDELTBigQuerySourceHandler.config_schema is GDELTConfig
    # Version is semver-shaped.
    parts = GDELTBigQuerySourceHandler.handler_version.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# SQL composition (pure)
# ---------------------------------------------------------------------------


class TestSqlComposition:
    def test_partitions_on_sqldate_and_dateadded(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x")
        since = datetime(2025, 5, 20, 18, 0, 0, tzinfo=timezone.utc)
        sql, params = build_gdelt_sql(cfg, since)
        assert "SQLDATE >= @sql_date_min" in sql
        assert "DATEADDED >= @date_added_min" in sql
        assert params["sql_date_min"] == 20250520
        assert params["date_added_min"] == 20250520180000
        assert params["row_limit"] == cfg.max_rows_per_pull

    def test_default_table_is_events(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x")
        sql, _ = build_gdelt_sql(cfg, datetime(2025, 5, 1, tzinfo=timezone.utc))
        assert f"FROM `{EVENTS_TABLE}`" in sql

    def test_country_filter_uses_named_param(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x", cameo_country="BR")
        sql, params = build_gdelt_sql(cfg, datetime(2025, 5, 1, tzinfo=timezone.utc))
        assert "ActionGeo_CountryCode = @country" in sql
        assert params["country"] == "BR"
        # User-controlled value travels via the parameter binding, not the
        # SQL text — guarantees no injection vector.
        assert "= 'BR'" not in sql
        assert "= \"BR\"" not in sql

    def test_country_filter_absent_when_unset(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x")
        sql, params = build_gdelt_sql(cfg, datetime(2025, 5, 1, tzinfo=timezone.utc))
        # Column is always SELECTed; what we care about is the WHERE clause.
        assert "ActionGeo_CountryCode = @country" not in sql
        assert "country" not in params

    def test_event_root_codes_use_unnest_param(self) -> None:
        cfg = GDELTConfig(
            bq_credentials_secret="x", event_root_codes=["14", "18", "19"]
        )
        sql, params = build_gdelt_sql(cfg, datetime(2025, 5, 1, tzinfo=timezone.utc))
        assert "EventRootCode IN UNNEST(@root_codes)" in sql
        assert params["root_codes"] == ["14", "18", "19"]

    def test_actor_filter_matches_either_actor_type(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x", actor_filter="ENERGY|UTIL")
        sql, params = build_gdelt_sql(cfg, datetime(2025, 5, 1, tzinfo=timezone.utc))
        assert "REGEXP_CONTAINS(IFNULL(Actor1Type1Code, ''), @actor_re)" in sql
        assert "REGEXP_CONTAINS(IFNULL(Actor2Type1Code, ''), @actor_re)" in sql
        assert params["actor_re"] == "ENERGY|UTIL"

    def test_tone_filter_uses_between(self) -> None:
        cfg = GDELTConfig(
            bq_credentials_secret="x", tone_filter=(-10.0, 0.0)
        )
        sql, params = build_gdelt_sql(cfg, datetime(2025, 5, 1, tzinfo=timezone.utc))
        assert "AvgTone BETWEEN @tone_lo AND @tone_hi" in sql
        assert params["tone_lo"] == -10.0
        assert params["tone_hi"] == 0.0

    def test_all_event_columns_selected(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x")
        sql, _ = build_gdelt_sql(cfg, datetime(2025, 5, 1, tzinfo=timezone.utc))
        for col in EVENT_COLUMNS:
            assert col in sql, f"missing column in SELECT: {col}"

    def test_sql_orders_by_dateadded_for_cursor_stability(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x")
        sql, _ = build_gdelt_sql(cfg, datetime(2025, 5, 1, tzinfo=timezone.utc))
        assert "ORDER BY DATEADDED ASC" in sql

    def test_since_defaulted_to_lookback_when_none(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x", lookback_minutes=60)
        sql, params = build_gdelt_sql(cfg, since=None)
        # Should produce *some* lower bound — exact int depends on now(),
        # but it should be a reasonable INT64.
        assert isinstance(params["sql_date_min"], int)
        assert params["sql_date_min"] > 20200000
        assert "SQLDATE >= @sql_date_min" in sql

    def test_naive_datetime_treated_as_utc(self) -> None:
        cfg = GDELTConfig(bq_credentials_secret="x")
        naive = datetime(2025, 5, 20, 18, 0, 0)
        sql, params = build_gdelt_sql(cfg, naive)
        assert params["sql_date_min"] == 20250520


# ---------------------------------------------------------------------------
# Pull happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_yields_signals_and_advances_cursor() -> None:
    rows = [
        _sample_row(global_event_id=10, date_added=20250520180000),
        _sample_row(global_event_id=11, date_added=20250520181500),
        _sample_row(global_event_id=12, date_added=20250520183000),
    ]
    cfg = GDELTConfig(bq_credentials_secret="creds.gdelt.bigquery_sa",
                      cameo_country="BR")
    handler, bq = _make_handler(cfg, client=FakeBQClient(
        dry_run_bytes=1_000_000, rows=rows,
    ))
    state = InMemoryStateStore()
    ctx = _ctx(state)

    out: list[Signal] = []
    async for sig in handler.pull(ctx, since=datetime(2025, 5, 20, 17, 0, tzinfo=timezone.utc)):
        out.append(sig)

    assert len(out) == 3
    # Signal envelope shape (L-102 §2).
    for sig in out:
        assert sig.source_id == "gdelt_brazil"
        # Source-first pivot: Signal is target-agnostic — target_id left the
        # schema (it lives only on derived analyst outputs). The handler stamps
        # source_id only; no target_id assertion.
        assert sig.canonical_url
        assert sig.content_hash
        assert sig.payload["event_code"] == "145"
        assert sig.payload["geo"]["country_code"] == "BR"
        assert sig.payload["raw_body"]["GLOBALEVENTID"] in (10, 11, 12)
        assert sig.raw_provenance["kind"] == "gdelt_query"
        assert sig.raw_provenance["table"] == EVENTS_TABLE

    # Cursor advances to max DATEADDED.
    assert await state.get("gdelt_last_dateadded") == 20250520183000
    # Two queries: dry-run + real.
    assert sum(1 for c in bq.recorded if c["dry_run"]) == 1
    assert sum(1 for c in bq.recorded if not c["dry_run"]) == 1
    # last_success_at populated.
    assert await state.get("gdelt_last_success_at")
    # error cleared.
    assert await state.get("gdelt_last_error") is None


@pytest.mark.asyncio
async def test_pull_uses_stored_cursor_when_since_omitted() -> None:
    rows = [_sample_row(global_event_id=42, date_added=20250520190000)]
    cfg = GDELTConfig(bq_credentials_secret="creds.gdelt.bigquery_sa")
    handler, bq = _make_handler(cfg, client=FakeBQClient(
        dry_run_bytes=500_000, rows=rows,
    ))
    state = InMemoryStateStore({"gdelt_last_dateadded": 20250520185500})
    ctx = _ctx(state)

    async for _ in handler.pull(ctx, since=None):
        pass

    # The dry-run params should reflect the stored cursor lower bound,
    # not now()-lookback.
    dry = next(c for c in bq.recorded if c["dry_run"])
    params_map = {p.name: p.value for p in dry["params"]}
    assert params_map["date_added_min"] == 20250520185500


@pytest.mark.asyncio
async def test_pull_records_rolling_row_count() -> None:
    rows = [_sample_row(global_event_id=i, date_added=20250520180000 + i)
            for i in range(5)]
    handler, _ = _make_handler(client=FakeBQClient(
        dry_run_bytes=100_000, rows=rows,
    ))
    state = InMemoryStateStore()
    ctx = _ctx(state)

    async for _ in handler.pull(ctx, since=datetime(2025, 5, 1, tzinfo=timezone.utc)):
        pass

    rolling = await state.get("gdelt_rows_pulled_24h")
    assert rolling, "expected rolling row counter to be populated"
    assert sum(n for (_ts, n) in rolling) == 5


# ---------------------------------------------------------------------------
# Cost cap enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_aborts_when_dry_run_exceeds_per_pull_cap() -> None:
    cfg = GDELTConfig(
        bq_credentials_secret="creds.gdelt.bigquery_sa",
        cost_cap_bytes_per_pull=10_000_000,        # 10 MB
    )
    huge = 5 * 1024 * 1024 * 1024                  # 5 GB
    handler, bq = _make_handler(cfg, client=FakeBQClient(dry_run_bytes=huge))
    state = InMemoryStateStore()
    ctx = _ctx(state)

    with pytest.raises(CostCapExceeded) as exc:
        async for _ in handler.pull(ctx, since=datetime(2025, 5, 1, tzinfo=timezone.utc)):
            pass

    assert exc.value.scope == "per-pull"
    assert exc.value.estimated == huge

    # Only the dry-run ran — no real query.
    assert all(c["dry_run"] for c in bq.recorded), \
        "real query MUST NOT execute after cost-cap breach"
    # Error captured in state.
    assert "GDELT pull aborted" in (await state.get("gdelt_last_error") or "")


@pytest.mark.asyncio
async def test_pull_aborts_when_daily_cap_would_overflow() -> None:
    cfg = GDELTConfig(
        bq_credentials_secret="creds.gdelt.bigquery_sa",
        cost_cap_bytes_per_pull=10_000_000,
        daily_cap_bytes=15_000_000,
    )
    handler, bq = _make_handler(cfg, client=FakeBQClient(dry_run_bytes=8_000_000))
    # Already 9 MB scanned today; next 8 MB would push past 15 MB.
    from legba.data.sources.gdelt import _utc_day_key
    today = _utc_day_key(datetime.now(timezone.utc))
    state = InMemoryStateStore({"gdelt_daily_bytes": {today: 9_000_000}})
    ctx = _ctx(state)

    with pytest.raises(CostCapExceeded) as exc:
        async for _ in handler.pull(ctx, since=datetime(2025, 5, 1, tzinfo=timezone.utc)):
            pass

    assert exc.value.scope == "daily"


@pytest.mark.asyncio
async def test_pull_emits_warning_above_soft_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = GDELTConfig(
        bq_credentials_secret="creds.gdelt.bigquery_sa",
        cost_cap_bytes_per_pull=HARD_BYTES_CAP,   # don't refuse — just warn
    )
    huge_below_cap = WARN_BYTES_THRESHOLD + 1
    handler, _ = _make_handler(cfg, client=FakeBQClient(
        dry_run_bytes=huge_below_cap, rows=[],
    ))
    ctx = _ctx()
    caplog.set_level(logging.WARNING, logger="legba.source")
    async for _ in handler.pull(ctx, since=datetime(2025, 5, 1, tzinfo=timezone.utc)):
        pass

    assert any("exceeds soft threshold" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_pull_accumulates_daily_bytes_on_success() -> None:
    cfg = GDELTConfig(
        bq_credentials_secret="creds.gdelt.bigquery_sa",
        cost_cap_bytes_per_pull=50_000_000,
        daily_cap_bytes=100_000_000,
    )
    handler, _ = _make_handler(cfg, client=FakeBQClient(
        dry_run_bytes=5_000_000, rows=[_sample_row()],
    ))
    state = InMemoryStateStore()
    ctx = _ctx(state)

    async for _ in handler.pull(ctx, since=datetime(2025, 5, 1, tzinfo=timezone.utc)):
        pass

    daily = await state.get("gdelt_daily_bytes")
    assert daily
    assert sum(daily.values()) == 5_000_000


# ---------------------------------------------------------------------------
# Credential resolution failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_raises_when_resolver_missing() -> None:
    cfg = GDELTConfig(bq_credentials_secret="creds.gdelt.bigquery_sa")
    handler = GDELTBigQuerySourceHandler(cfg)  # no resolver supplied
    ctx = _ctx()

    with pytest.raises(RuntimeError, match="no credential resolver"):
        async for _ in handler.pull(ctx, since=datetime(2025, 5, 1, tzinfo=timezone.utc)):
            pass


@pytest.mark.asyncio
async def test_pull_raises_on_bad_service_account_json() -> None:
    async def bad_resolver(_secret_id: str) -> bytes:
        return b"not-json"

    cfg = GDELTConfig(bq_credentials_secret="creds.gdelt.bigquery_sa")
    handler = GDELTBigQuerySourceHandler(cfg, credential_resolver=bad_resolver,
                                          client_factory=lambda *a, **k: FakeBQClient())
    ctx = _ctx()

    with pytest.raises(ValueError, match="not valid service-account JSON"):
        async for _ in handler.pull(ctx, since=datetime(2025, 5, 1, tzinfo=timezone.utc)):
            pass


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthcheck_reports_estimated_bytes() -> None:
    handler, _ = _make_handler(client=FakeBQClient(dry_run_bytes=42_000_000))
    ctx = _ctx()

    health = await handler.health_check(ctx)
    assert isinstance(health, SourceHealth)
    assert health.state == "healthy"
    assert health.detail["estimated_bytes_next_pull"] == 42_000_000
    assert health.detail["project_id"] == "legba-test-project"
    assert health.detail["kind"] == "gdelt_query"


@pytest.mark.asyncio
async def test_healthcheck_degraded_when_estimate_above_cap() -> None:
    cfg = GDELTConfig(
        bq_credentials_secret="creds.gdelt.bigquery_sa",
        cost_cap_bytes_per_pull=10_000_000,
    )
    handler, _ = _make_handler(cfg, client=FakeBQClient(dry_run_bytes=2_000_000_000))
    ctx = _ctx()

    health = await handler.health_check(ctx)
    assert health.state == "degraded"


@pytest.mark.asyncio
async def test_healthcheck_unhealthy_on_dry_run_error() -> None:
    handler, _ = _make_handler(client=FakeBQClient(
        raise_on_dry=RuntimeError("BigQuery permission denied"),
    ))
    ctx = _ctx()
    health = await handler.health_check(ctx)
    assert health.state == "unhealthy"
    assert "permission denied" in (health.last_error or "")


@pytest.mark.asyncio
async def test_healthcheck_unhealthy_on_credential_failure() -> None:
    async def broken_resolver(_sid: str) -> bytes:
        raise KeyError("creds.gdelt.bigquery_sa")

    cfg = GDELTConfig(bq_credentials_secret="creds.gdelt.bigquery_sa")
    handler = GDELTBigQuerySourceHandler(
        cfg,
        credential_resolver=broken_resolver,
        client_factory=lambda *a, **k: FakeBQClient(),
    )
    ctx = _ctx()
    health = await handler.health_check(ctx)
    assert health.state == "unhealthy"
    assert "creds.gdelt.bigquery_sa" in (health.last_error or "")


@pytest.mark.asyncio
async def test_healthcheck_includes_24h_row_count() -> None:
    handler, _ = _make_handler()
    now_iso = datetime.now(timezone.utc).isoformat()
    state = InMemoryStateStore({
        "gdelt_rows_pulled_24h": [(now_iso, 7), (now_iso, 3)],
        "gdelt_last_dateadded": 20250520180000,
    })
    ctx = _ctx(state)
    health = await handler.health_check(ctx)
    assert health.rows_pulled_24h == 10
    assert health.last_cursor == "20250520180000"


# ---------------------------------------------------------------------------
# Real-query failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_propagates_query_error_after_dry_run_ok() -> None:
    handler, _ = _make_handler(client=FakeBQClient(
        dry_run_bytes=500_000,
        raise_on_real=RuntimeError("query timeout"),
    ))
    state = InMemoryStateStore()
    ctx = _ctx(state)

    with pytest.raises(RuntimeError, match="query timeout"):
        async for _ in handler.pull(ctx, since=datetime(2025, 5, 1, tzinfo=timezone.utc)):
            pass

    err = await state.get("gdelt_last_error")
    assert err and "query" in err


# ---------------------------------------------------------------------------
# Live integration against real BigQuery — opt-in via env var
# ---------------------------------------------------------------------------


_LIVE_ENV = "LEGBA_GCP_SERVICE_ACCOUNT_JSON"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_bigquery_dry_run_estimates_under_cap() -> None:
    """Live: dry-run the configured query against real BigQuery.

    Capped at 1 MB scanned to avoid surprise costs. Skips unless
    ``LEGBA_GCP_SERVICE_ACCOUNT_JSON`` points to a service-account JSON
    file with bigquery.jobs.create on the legba-test project.
    """
    sa_path = os.getenv(_LIVE_ENV)
    if not sa_path:
        pytest.skip(
            f"{_LIVE_ENV} not set — skipping live BigQuery dry-run "
            "(set to a service-account JSON path to enable)"
        )
    if not os.path.isfile(sa_path):
        pytest.skip(f"{_LIVE_ENV} points to non-existent file: {sa_path}")

    try:
        from google.cloud import bigquery  # noqa: F401
    except ImportError:
        pytest.skip("google-cloud-bigquery not installed; skipping live test")

    sa_bytes = open(sa_path, "rb").read()

    async def resolver(_sid: str) -> bytes:
        return sa_bytes

    cfg = GDELTConfig(
        bq_credentials_secret="live.gdelt.sa",
        cameo_country="BR",
        event_root_codes=["14"],
        lookback_minutes=60,
        cost_cap_bytes_per_pull=1_000_000,    # 1 MB ceiling for the live test
        max_rows_per_pull=10,
    )

    # We construct the handler with the production client factory so this
    # exercises the real google-cloud-bigquery integration end-to-end.
    handler = GDELTBigQuerySourceHandler(cfg, credential_resolver=resolver)
    ctx = _ctx()

    health = await handler.health_check(ctx)
    # If the dry-run estimate is above 1 MB, the handler reports degraded
    # but did NOT actually scan — we accept either "healthy" or "degraded"
    # so long as no exception propagated.
    assert health.state in ("healthy", "degraded"), (
        f"live healthcheck returned {health.state}: {health.last_error}"
    )
    assert "estimated_bytes_next_pull" in health.detail


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_small_pull_against_real_bigquery() -> None:
    """Live: a real pull, capped at 1 MB scanned. Skips by default.

    Only runs when ``LEGBA_GCP_SERVICE_ACCOUNT_JSON`` is set. Caps everything
    aggressively — single country (BR), 60-minute window, 10-row limit, and
    a 1 MB cost cap that will refuse anything bigger.
    """
    sa_path = os.getenv(_LIVE_ENV)
    if not sa_path:
        pytest.skip(
            f"{_LIVE_ENV} not set — skipping live BigQuery small-pull test"
        )
    if not os.path.isfile(sa_path):
        pytest.skip(f"{_LIVE_ENV} points to non-existent file: {sa_path}")

    try:
        from google.cloud import bigquery  # noqa: F401
    except ImportError:
        pytest.skip("google-cloud-bigquery not installed; skipping live test")

    sa_bytes = open(sa_path, "rb").read()

    async def resolver(_sid: str) -> bytes:
        return sa_bytes

    cfg = GDELTConfig(
        bq_credentials_secret="live.gdelt.sa",
        cameo_country="BR",
        event_root_codes=["14"],
        lookback_minutes=60,
        cost_cap_bytes_per_pull=1_000_000,
        max_rows_per_pull=10,
    )
    handler = GDELTBigQuerySourceHandler(cfg, credential_resolver=resolver)
    ctx = _ctx()

    signals: list[Signal] = []
    try:
        async for sig in handler.pull(
            ctx, since=datetime.now(timezone.utc) - timedelta(minutes=60),
        ):
            signals.append(sig)
            if len(signals) >= 10:
                break
    except CostCapExceeded:
        # 1 MB ceiling may legitimately refuse — the test is satisfied that
        # the cap *worked*.
        pytest.skip("live dry-run estimate above 1 MB cap; verified cap engaged")

    # If we got rows, validate their shape.
    for sig in signals:
        assert sig.payload["geo"]["country_code"] == "BR"
        assert sig.payload["raw_body"]["GLOBALEVENTID"] is not None
