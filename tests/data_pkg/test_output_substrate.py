# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-190 substrate-write surface tests.

Reuses the L-001 ``migrated_pg`` fixture (see ``tests/data_pkg/conftest.py``)
so writes land in the same fresh test database as the rest of the
``legba.data`` suite. Real Postgres — no asyncpg mocks; this is the typed
facade over the substrate write path and contract value comes from a real
INSERT round-trip.

Covers, per payload type (finding / situation / hypothesis / prediction /
alert):

  * Happy path — typed payload writes correctly + provenance columns
    populated from the bare scalars.
  * Dict-shaped payload accepted + validated against the kind's pydantic
    model.
  * Invalid payload raises ``pydantic.ValidationError`` synchronously
    (substrate-writer is the typed entry point; bad payloads do not route
    to ``output_dead_letter``).
  * Lineage chain — ``derived_from`` UUIDs are preserved on the new row.

Plus:

  * Module-level ``KIND_NAME == "substrate_writer"`` for host registry
    discovery.
  * Re-imports from ``legba.data.outputs`` namespace.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from pydantic import ValidationError

from legba.data.config import PostgresConfig
from legba.data.outputs import substrate
from legba.data.outputs.substrate import (
    KIND_NAME,
    SubstrateWriteFailed,
    write_alert,
    write_finding,
    write_hypothesis,
    write_prediction,
    write_situation,
)
from legba.data.provenance import (
    AlertPayload,
    FindingPayload,
    HypothesisPayload,
    PredictionPayload,
    SituationPayload,
)
from legba.data.sources._contract import Signal
from legba.runtime.source_actor import write_canonical_signal


# ---------------------------------------------------------------------------
# Unit tests (no DB)
# ---------------------------------------------------------------------------


def test_kind_name_constant():
    """Host registry discovers the kind via the module-level ``KIND_NAME``."""
    assert KIND_NAME == "substrate_writer"
    assert substrate.KIND_NAME == "substrate_writer"


def test_module_exposes_per_payload_writers():
    for fn in (write_finding, write_situation, write_hypothesis,
               write_prediction, write_alert):
        assert callable(fn)


def test_substrate_write_failed_carries_kind_and_dlq():
    """The escape-hatch exception preserves both the kind + the DLQ entry."""
    from legba.data.provenance.dlq import OutputDeadLetterEntry
    from legba.data.provenance.kinds import OutputKind
    from datetime import datetime, timezone

    entry = OutputDeadLetterEntry(
        id=uuid4(),
        run_id=None,
        analyst_id="a",
        analyst_version="v",
        declared_schema_uri="iglu:legba/finding/jsonschema/1-0-0",
        attempted_payload={},
        validation_error={"errors": [], "rendered": "x", "model": None},
        produced_at=datetime.now(tz=timezone.utc),
    )
    exc = SubstrateWriteFailed(kind=OutputKind.FINDING, dlq_entry=entry)
    assert exc.kind is OutputKind.FINDING
    assert exc.dlq_entry is entry


# ---------------------------------------------------------------------------
# Integration fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


async def _seed_signal(conn) -> UUID:
    """Plant a root signal so analyst outputs have an ancestor to derive from.

    Source-first pivot (migration 0024): signals are target-agnostic +
    modality-first; the pre-pivot ``write_target_signal`` /
    ``SignalPayload`` path (which targeted the dropped ``signals.data`` /
    ``target_id`` columns) is retired. We seed through the canonical
    source-first writer (:func:`write_canonical_signal`) with a
    target-agnostic :class:`Signal`. ``derived_from`` is a plain ``uuid[]``
    with no FK, so the returned signal id is a valid ancestor for the
    analyst writes under test.
    """
    signal = Signal(
        source_id="rss_main",
        modality="text",
        payload={"title": "root signal", "category": "energy"},
        content_hash=uuid4().hex,
    )
    sid = await write_canonical_signal(
        conn, signal, source_version="v1", owner_tenant="default",
    )
    return sid


# ---------------------------------------------------------------------------
# write_finding
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_finding_happy_path_typed_payload(pg_conn):
    sig_id = await _seed_signal(pg_conn)
    analyst_id = f"analyst.finding_{uuid4().hex[:8]}"
    run_id = uuid4()

    new_id = await write_finding(
        pg_conn,
        "br_energy_test_l190",
        analyst_id,
        FindingPayload(
            title="L-190 finding",
            body="extracted observation",
            evidence=["e1", "e2"],
        ),
        [sig_id],
        analyst_version="v1.2.3",
        target_version="abc123def456",
        run_id=run_id,
    )
    assert isinstance(new_id, UUID)

    fetched = await pg_conn.fetchrow(
        "SELECT kind, title, body, analyst_id, target_id, "
        "analyst_version, target_version, run_id, derived_from, schema_uri "
        "FROM analyst_outputs WHERE id = $1",
        new_id,
    )
    assert fetched["kind"] == "finding"
    assert fetched["title"] == "L-190 finding"
    assert fetched["body"] == "extracted observation"
    assert fetched["analyst_id"] == analyst_id
    assert fetched["target_id"] == "br_energy_test_l190"
    assert fetched["analyst_version"] == "v1.2.3"
    assert fetched["target_version"] == "abc123def456"
    assert fetched["run_id"] == run_id
    assert fetched["derived_from"] == [sig_id]
    assert fetched["schema_uri"] == "iglu:legba/finding/jsonschema/1-0-0"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_finding_accepts_dict_payload(pg_conn):
    """Dict-shaped payloads get coerced through the pydantic model."""
    new_id = await write_finding(
        pg_conn,
        None,                            # target_id may be None for cross-target
        "analyst.dict",
        {"title": "from dict", "body": "ok"},
        [],
    )
    fetched = await pg_conn.fetchrow(
        "SELECT title, kind FROM analyst_outputs WHERE id = $1", new_id
    )
    assert fetched["title"] == "from dict"
    assert fetched["kind"] == "finding"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_finding_rejects_invalid_payload_synchronously(pg_conn):
    """Missing required field → ``ValidationError`` raised, no DLQ row."""
    # Snapshot DLQ count to prove nothing was written.
    before = await pg_conn.fetchval(
        "SELECT COUNT(*) FROM output_dead_letter "
        "WHERE analyst_id = 'analyst.bad_finding'"
    )
    with pytest.raises(ValidationError):
        await write_finding(
            pg_conn,
            "tgt",
            "analyst.bad_finding",
            {"body": "no title"},        # title required
            [],
        )
    after = await pg_conn.fetchval(
        "SELECT COUNT(*) FROM output_dead_letter "
        "WHERE analyst_id = 'analyst.bad_finding'"
    )
    assert before == after, (
        "substrate.write_finding must raise — not route to DLQ — on "
        "validation failure (typed-entry-point contract)"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_finding_extra_field_rejected(pg_conn):
    """FindingPayload uses ``extra='forbid'`` — unknown fields raise."""
    with pytest.raises(ValidationError):
        await write_finding(
            pg_conn,
            "tgt",
            "analyst.extra",
            {"title": "ok", "not_a_field": True},
            [],
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_finding_preserves_multi_ancestor_lineage(pg_conn):
    s1 = await _seed_signal(pg_conn)
    s2 = await _seed_signal(pg_conn)
    s3 = await _seed_signal(pg_conn)

    f1 = await write_finding(
        pg_conn,
        "br_energy_test_l190",
        "analyst.chain",
        FindingPayload(title="layer-1"),
        [s1, s2],
    )
    f2 = await write_finding(
        pg_conn,
        "br_energy_test_l190",
        "analyst.chain",
        FindingPayload(title="layer-2"),
        [f1, s3],
    )
    fetched = await pg_conn.fetchrow(
        "SELECT derived_from FROM analyst_outputs WHERE id = $1", f2
    )
    # Order is preserved per write_analyst_output contract.
    assert fetched["derived_from"] == [f1, s3]


# ---------------------------------------------------------------------------
# write_situation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_situation_happy_path(pg_conn):
    sig_id = await _seed_signal(pg_conn)
    new_id = await write_situation(
        pg_conn,
        "br_energy_test_l190",
        "analyst.sit",
        SituationPayload(
            name="Brazil energy spike",
            category="energy",
            intensity_score=0.75,
            event_count=3,
        ),
        [sig_id],
        analyst_version="v1",
        target_version="abc123def456",
    )
    assert isinstance(new_id, UUID)
    fetched = await pg_conn.fetchrow(
        "SELECT name, category, intensity_score, event_count, "
        "analyst_id, target_id, derived_from, schema_uri "
        "FROM situations WHERE id = $1",
        new_id,
    )
    assert fetched["name"] == "Brazil energy spike"
    assert fetched["category"] == "energy"
    assert fetched["intensity_score"] == pytest.approx(0.75)
    assert fetched["event_count"] == 3
    assert fetched["analyst_id"] == "analyst.sit"
    assert fetched["target_id"] == "br_energy_test_l190"
    assert fetched["derived_from"] == [sig_id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_situation_upserts_on_signature(pg_conn):
    """A situation re-emitted with the same (situation_signature, analyst_id)
    UPDATES the existing row instead of duplicating it — the migration-0040
    upsert key the standard write path now targets (the prior plain INSERT had
    no key, which is why situation_clustering had to bypass it)."""
    from datetime import datetime, timezone

    sig_id = await _seed_signal(pg_conn)
    sig = f"sig:upsert_{uuid4().hex[:8]}"
    await write_situation(
        pg_conn, "tgt_upsert", "analyst.sit_upsert",
        SituationPayload(
            name="v1", category="war", intensity_score=0.5, event_count=1,
            situation_signature=sig,
            valid_from=datetime(2026, 2, 28, tzinfo=timezone.utc),
        ),
        [sig_id],
    )
    await write_situation(
        pg_conn, "tgt_upsert", "analyst.sit_upsert",
        SituationPayload(
            name="v2", category="war", intensity_score=0.9, event_count=5,
            status="closed", situation_signature=sig,
            valid_from=datetime(2026, 2, 28, tzinfo=timezone.utc),
            valid_until=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ),
        [sig_id],
    )
    rows = await pg_conn.fetch(
        "SELECT name, event_count, intensity_score, situation_signature, "
        "valid_from, valid_until "
        "FROM situations WHERE situation_signature=$1 AND analyst_id=$2",
        sig, "analyst.sit_upsert",
    )
    assert len(rows) == 1, "re-emit must upsert on (signature, analyst_id), not duplicate"
    assert rows[0]["name"] == "v2"                       # updated in place
    assert rows[0]["event_count"] == 5
    assert rows[0]["intensity_score"] == pytest.approx(0.9)
    assert rows[0]["situation_signature"] == sig         # promoted to a real column
    assert rows[0]["valid_until"] is not None            # close-stamp carried through


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_situation_signatured_requires_analyst_id(pg_conn):
    """A signatured situation with no analyst_id is rejected (the upsert key is
    (situation_signature, analyst_id); a NULL analyst_id would silently
    duplicate instead of upsert). Fail loud, not corrupt the dedup."""
    sig_id = await _seed_signal(pg_conn)
    with pytest.raises(ValueError, match="analyst_id"):
        await write_situation(
            pg_conn, "tgt_noanalyst", "",  # empty analyst_id
            SituationPayload(
                name="x", category="war",
                situation_signature=f"sig:noanalyst_{uuid4().hex[:8]}",
            ),
            [sig_id],
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_situation_rejects_invalid_payload(pg_conn):
    """Negative intensity_score is rejected by SituationPayload (ge=0.0)."""
    with pytest.raises(ValidationError):
        await write_situation(
            pg_conn,
            "tgt",
            "analyst.bad_sit",
            {"name": "x", "intensity_score": -1.0},
            [],
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clustering_upsert_writes_columns_and_is_idempotent(pg_conn):
    """The situation_clustering live writer (_upsert_situation) populates the
    first-class situation_signature + temporal columns and UPSERTs atomically:
    a second run on the same signature UPDATES the one row (created→updated),
    and a closed cluster stamps valid_until."""
    from datetime import datetime, timezone

    from legba.data.analysts.deterministic_handlers import situation_clustering as sc

    sig = f"sig:cluster_{uuid4().hex[:8]}"
    analyst = "situation_clustering"
    # Two members, newest LONG ago → the cluster is 'closed' (stamps valid_until).
    members = [
        {"id": str(uuid4()), "title": "older", "situation_signature": sig,
         "produced_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"id": str(uuid4()), "title": "newest", "situation_signature": sig,
         "produced_at": datetime(2026, 1, 10, tzinfo=timezone.utc)},
    ]
    fields = sc._situation_fields(sig, members, now=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert fields["status"] == "closed"  # stale → closed → valid_until stamped

    a1 = await sc._upsert_situation(
        pg_conn, fields=fields, analyst_id=analyst, analyst_version="v1",
        target_id=None, run_id=None,
    )
    a2 = await sc._upsert_situation(
        pg_conn, fields=fields, analyst_id=analyst, analyst_version="v1",
        target_id=None, run_id=None,
    )
    assert a1 == "created" and a2 == "updated"  # atomic upsert, not a duplicate

    rows = await pg_conn.fetch(
        "SELECT situation_signature, valid_from, valid_until, status "
        "FROM situations WHERE situation_signature=$1 AND analyst_id=$2",
        sig, analyst,
    )
    assert len(rows) == 1, "upsert keeps a single row per (signature, analyst_id)"
    assert rows[0]["situation_signature"] == sig          # first-class column
    assert rows[0]["valid_from"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert rows[0]["valid_until"] == datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert rows[0]["status"] == "closed"


# ---------------------------------------------------------------------------
# write_hypothesis
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_hypothesis_happy_path(pg_conn):
    sig_id = await _seed_signal(pg_conn)
    new_id = await write_hypothesis(
        pg_conn,
        "br_energy_test_l190",
        "analyst.hyp",
        HypothesisPayload(
            thesis="Demand will spike",
            counter_thesis="No spike",
            evidence_balance=2,
            status="active",
        ),
        [sig_id],
        analyst_version="v1",
    )
    fetched = await pg_conn.fetchrow(
        "SELECT thesis, counter_thesis, evidence_balance, status, "
        "analyst_id, target_id, derived_from "
        "FROM hypotheses WHERE id = $1",
        new_id,
    )
    assert fetched["thesis"] == "Demand will spike"
    assert fetched["counter_thesis"] == "No spike"
    assert fetched["evidence_balance"] == 2
    assert fetched["status"] == "active"
    assert fetched["analyst_id"] == "analyst.hyp"
    assert fetched["derived_from"] == [sig_id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_hypothesis_rejects_empty_thesis(pg_conn):
    """thesis has min_length=1 — empty string rejected."""
    with pytest.raises(ValidationError):
        await write_hypothesis(
            pg_conn,
            "tgt",
            "analyst.bad_hyp",
            {"thesis": ""},
            [],
        )


# ---------------------------------------------------------------------------
# write_prediction
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_prediction_happy_path(pg_conn):
    # Source-first pivot (migration 0024) DROPPED the dedicated `predictions`
    # table; OutputKind.PREDICTION now routes through `analyst_outputs`
    # (kind='prediction') with the typed PredictionPayload dump in `data`.
    sig_id = await _seed_signal(pg_conn)
    new_id = await write_prediction(
        pg_conn,
        "br_energy_test_l190",
        "analyst.pred",
        PredictionPayload(
            hypothesis="Demand spike sustained 24h",
            source_cycle=1,
            region="BR",
            confidence=0.8,
            evidence_for=["e1"],
            evidence_against=[],
        ),
        [sig_id],
    )
    fetched = await pg_conn.fetchrow(
        "SELECT kind, data, confidence, analyst_id, derived_from "
        "FROM analyst_outputs WHERE id = $1",
        new_id,
    )
    assert fetched["kind"] == "prediction"
    data = (
        json.loads(fetched["data"])
        if isinstance(fetched["data"], str)
        else fetched["data"]
    )
    assert data["hypothesis"] == "Demand spike sustained 24h"
    assert data["region"] == "BR"
    assert data["evidence_for"] == ["e1"]
    assert fetched["confidence"] == pytest.approx(0.8)
    assert fetched["analyst_id"] == "analyst.pred"
    assert fetched["derived_from"] == [sig_id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_prediction_rejects_out_of_range_confidence(pg_conn):
    """confidence has ge=0/le=1 — 1.5 must raise."""
    with pytest.raises(ValidationError):
        await write_prediction(
            pg_conn,
            "tgt",
            "analyst.bad_pred",
            {"hypothesis": "h", "confidence": 1.5},
            [],
        )


# ---------------------------------------------------------------------------
# write_alert
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_alert_carries_severity(pg_conn):
    sig_id = await _seed_signal(pg_conn)
    new_id = await write_alert(
        pg_conn,
        "br_energy_test_l190",
        "analyst.alert",
        AlertPayload(
            title="High-sev alert",
            severity="high",
            routing_hint="ops-pager",
        ),
        [sig_id],
    )
    fetched = await pg_conn.fetchrow(
        "SELECT kind, severity, title, analyst_id, derived_from "
        "FROM analyst_outputs WHERE id = $1",
        new_id,
    )
    assert fetched["kind"] == "alert"
    assert fetched["severity"] == "high"
    assert fetched["title"] == "High-sev alert"
    assert fetched["analyst_id"] == "analyst.alert"
    assert fetched["derived_from"] == [sig_id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_alert_rejects_invalid_severity(pg_conn):
    """severity literal enum — 'catastrophic' must raise."""
    with pytest.raises(ValidationError):
        await write_alert(
            pg_conn,
            "tgt",
            "analyst.bad_alert",
            {"title": "x", "severity": "catastrophic"},
            [],
        )


# ---------------------------------------------------------------------------
# NATS publish pass-through
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publish_fn_passed_through_to_writer(pg_conn):
    """``publish_fn`` is forwarded to the underlying writer (best-effort)."""
    captured: list[tuple[str, bytes]] = []

    async def publish_fn(subject: str, data: bytes) -> None:
        captured.append((subject, data))

    sig_id = await _seed_signal(pg_conn)
    await write_finding(
        pg_conn,
        "br_energy_test_l190",
        "analyst.pub",
        FindingPayload(title="pub-finding"),
        [sig_id],
        publish_fn=publish_fn,
    )
    assert len(captured) == 1
    subject, _ = captured[0]
    # nats_subject_pattern is "analyst.{analyst_id}.finding".
    assert subject == "analyst.analyst.pub.finding"
