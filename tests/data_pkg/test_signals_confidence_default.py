# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wave B prereq #2 — `signals.confidence` default + Pydantic default.

Two rails:

  * Pydantic ``SignalPayload.confidence`` defaults to 0.5 (not None).
  * Migration 0016 asserts column DEFAULT 0.5 + NOT NULL on
    analyst_outputs (the pre-pivot signals/predictions assertions were
    deleted — migration 0024 dropped `signals.confidence` and the
    `predictions` table outright in the source-first re-cut).

Both rails defend against the Wave A Gate-9 failure mode where the
predictor's narrative-time SignalPayload construction omitted
``confidence`` and the asyncpg INSERT raised ``NotNullViolation``.
"""

from __future__ import annotations

import asyncpg
import pytest

from legba.data.config import PostgresConfig
from legba.data.migrate import apply_primary_migrations
from legba.data.provenance.models import SignalPayload


# ---------------------------------------------------------------------------
# Rail (a) — Pydantic default
# ---------------------------------------------------------------------------


def test_signal_payload_confidence_default_is_half():
    """``confidence=`` omitted → 0.5, not None."""
    p = SignalPayload(title="some signal")
    assert p.confidence == 0.5


def test_signal_payload_confidence_is_a_required_float_when_passed():
    """Passing None must still fail validation (the schema is float-only)."""
    with pytest.raises(Exception):
        SignalPayload(title="some signal", confidence=None)  # type: ignore[arg-type]


def test_signal_payload_confidence_clamped_to_range():
    """``ge=0.0, le=1.0`` still enforced — out-of-range fails."""
    with pytest.raises(Exception):
        SignalPayload(title="some signal", confidence=1.5)
    with pytest.raises(Exception):
        SignalPayload(title="some signal", confidence=-0.1)


# ---------------------------------------------------------------------------
# Rail (b) — DB column default
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_analyst_outputs_confidence_column_default_is_half(
    migrated_pg: PostgresConfig,
) -> None:
    """Migration 0016 also covers analyst_outputs.confidence."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        row = await conn.fetchrow(
            """
            SELECT column_default, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'analyst_outputs' AND column_name = 'confidence'
            """
        )
    finally:
        await conn.close()
    assert row is not None
    assert "0.5" in (row["column_default"] or "")
    assert row["is_nullable"] == "NO"


# test_migration_0016_recorded_in_ledger removed: the 30-migration chain was
# flattened to 0001_baseline.sql (clean-slate release). The confidence-default
# schema is verified by the schema tests above; baseline-in-ledger by
# test_migrations.py.


@pytest.mark.integration
async def test_baseline_migration_idempotent(
    migrated_pg: PostgresConfig,
) -> None:
    """Re-running apply_primary_migrations is a no-op once the baseline ran."""
    applied = await apply_primary_migrations(migrated_pg)
    assert "0001_baseline.sql" not in applied
