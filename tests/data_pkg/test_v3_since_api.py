# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the P1-6 "since last visit" diff + band-trajectory routes.

Covers :mod:`legba.data.registry.since_api`:

  * ``GET /api/v1/v3/since?cursor=<ts>``            -> ``SinceResponse``
  * ``GET /api/v1/v3/eval/band_trajectory``          -> ``BandTrajectoryResponse``

Two layers, per the house v3-route pattern:

  * PURE tests (no DB): route registration, the registry-slim import guard,
    the mirrored-constant DRIFT GUARDS (the band ladder / faithfulness floor /
    band-transition classifier / situation decay thresholds each asserted
    equal to their source-of-truth producer modules — the
    ``STRUCTURAL_VERIFY_EXEMPT_ANALYSTS`` drift-guard precedent), and the
    pure reducers.
  * INTEGRATION tests over the ephemeral ``migrated_pg`` database + real HTTP
    (the ``test_substrate_reads_api`` fixture shape): diff correctness incl.
    supersession, band comparison, verify-gate + structural-exempt exclusion,
    the honest empty state (fresh cursor -> all-empty valid envelope), honest
    truncation, cursor validation, and the trajectory shape.

Auth: tests run in dev-mode (``LEGBA_DEV_MODE=1`` from tests/conftest.py, no
``LEGBA_REGISTRY_API_TOKEN``), so ``require_bearer`` returns ``"anonymous"``
and unauthenticated requests pass — the same bearer path as the rest of the
v3 surface.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

import legba.data.registry.since_api as since_api
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.since_api import (
    build_since_router,
    classify_band_transition,
    classify_situation_change,
    scorecard_band_changes,
    situation_decay_status,
    trajectory_desks,
)
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

# Mandatory env for vault + signing identity (mirrors test_substrate_reads_api).
_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)


def _fixed_identity() -> SigningIdentity:
    seed = b"v3-since-api-test-signing-seed-x"
    assert len(seed) == 32
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:v3-since-test",
    )


# ---------------------------------------------------------------------------
# Pure tests — registration, slimness, drift guards, reducers (no DB)
# ---------------------------------------------------------------------------


def test_since_routes_registered() -> None:
    """Both P1-6 routes register; neither shadows an existing v3 path."""
    router = build_since_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/since" in paths
    assert "/eval/band_trajectory" in paths

    # Cross-check against the v3 telemetry router: no collisions.
    from legba.data.registry.v3_api import build_v3_router

    v3_paths = {
        r.path
        for r in build_v3_router(deps=object()).routes  # type: ignore[arg-type]
    }
    assert not (paths & v3_paths)


def test_since_registry_slim_no_runtime_imports() -> None:
    """The module stays registry-slim: no runtime / deterministic-handler
    imports (the v3_api slim-image rule) — mirrors + drift guards instead."""
    src = since_api.__file__
    with open(src, "r", encoding="utf-8") as fh:
        text = fh.read()
    import_lines = "\n".join(
        ln for ln in text.splitlines()
        if ln.strip().startswith(("import ", "from "))
    )
    assert "scorecard_banding" not in import_lines
    assert "deterministic" not in import_lines
    assert "alert_trigger_scan" not in import_lines
    assert "situation_clustering" not in import_lines
    assert "legba.runtime" not in import_lines and "..runtime" not in import_lines


def test_drift_guard_band_constants() -> None:
    """The mirrored ladder / insufficient sentinel / effective-conf floor MUST
    stay equal to scorecard_banding's (the source of truth)."""
    from legba.data.analysts.deterministic_handlers import scorecard_banding

    assert since_api.BAND_LADDER == scorecard_banding.BAND_LADDER
    assert since_api.INSUFFICIENT_BAND == scorecard_banding.INSUFFICIENT
    assert since_api.EFFECTIVE_CONF_FLOOR == scorecard_banding.FAITH_FLOOR


def test_drift_guard_band_transition_classifier() -> None:
    """The mirrored classifier is EXTENSIONALLY equal to the P1-3 trigger's
    over every band pair (ladder x ladder + insufficient + off-ladder)."""
    from legba.data.analysts.deterministic_handlers import alert_trigger_scan

    values = list(since_api.BAND_LADDER) + [since_api.INSUFFICIENT_BAND, "bogus"]
    for frm in values:
        for to in values:
            if frm == to:
                continue
            assert classify_band_transition(frm, to) == (
                alert_trigger_scan.classify_band_transition(frm, to)
            ), f"classifier drift on {frm} -> {to}"


def test_drift_guard_situation_decay() -> None:
    """The mirrored decay thresholds + status function match
    situation_clustering's (the source of truth)."""
    from legba.data.analysts.deterministic_handlers import situation_clustering

    assert (
        since_api.SITUATION_ACTIVE_MAX_DAYS
        == situation_clustering._STATUS_ACTIVE_MAX_DAYS
    )
    assert (
        since_api.SITUATION_DORMANT_MAX_DAYS
        == situation_clustering._STATUS_DORMANT_MAX_DAYS
    )
    now = datetime.now(timezone.utc)
    for age_days in (0.0, 0.5, 1.99, 2.01, 5.0, 6.99, 7.01, 30.0):
        le = now - timedelta(days=age_days)
        assert situation_decay_status(le, now) == (
            situation_clustering._situation_status(le, now)
        ), f"decay drift at age {age_days}d"
    assert situation_decay_status(None, now) == (
        situation_clustering._situation_status(None, now)
    )


def _card_row(
    target: str,
    dims: dict[str, Any],
    *,
    produced_at: datetime,
    row_id: str | None = None,
) -> dict[str, Any]:
    """One scorecard row as the SQL returns it (payload-dump nesting)."""
    return {
        "target_id": target,
        "id": row_id or str(uuid4()),
        "produced_at": produced_at,
        "data": {
            "title": "t", "body": "b",
            "data": {"sub_handler": "scorecard_producer", "bands": {
                "target_id": target,
                "generated_at": produced_at.isoformat(),
                "floors": {"faith_floor": 0.50},
                "dimensions": dims,
                "composition": {"present": False, "basis": []},
            }},
        },
    }


def test_scorecard_band_changes_reducer() -> None:
    """The pure comparison: transitions emitted with the P1-3 shape; same-band
    dims, first-ever desks, and pre-cursor heads are all skipped honestly."""
    now = datetime.now(timezone.utc)
    cursor = now - timedelta(hours=1)
    prev = _card_row(
        "desk_a",
        {
            "escalation": {"band": "elevated", "basis": []},
            "energy_security": {"band": "insufficient-evidence", "basis": []},
            "internal_stability": {"band": "watch", "basis": []},
            "military_posture": {"band": "high", "basis": []},
        },
        produced_at=cursor - timedelta(hours=2),
    )
    head = _card_row(
        "desk_a",
        {
            "escalation": {"band": "high", "basis": []},          # deterioration
            "energy_security": {"band": "watch", "basis": []},    # evidence-gained
            "internal_stability": {"band": "watch", "basis": []}, # unchanged
            "military_posture": {"band": "insufficient-evidence", "basis": []},
            "brand_new_dim": {"band": "low", "basis": []},        # no FROM side
        },
        produced_at=now,
    )
    # A desk with no pre-cursor row: skipped (no FROM state).
    orphan_head = _card_row(
        "desk_b", {"escalation": {"band": "high", "basis": []}}, produced_at=now,
    )
    # A desk whose head predates the cursor: nothing new.
    stale_prev = _card_row(
        "desk_c", {"escalation": {"band": "low", "basis": []}},
        produced_at=cursor - timedelta(days=2),
    )
    stale_head = _card_row(
        "desk_c", {"escalation": {"band": "high", "basis": []}},
        produced_at=cursor - timedelta(days=1),
    )

    changes = scorecard_band_changes(
        [prev, stale_prev], [head, orphan_head, stale_head], cursor=cursor,
    )
    got = {
        (c.target_id, c.dimension): (c.from_band, c.to_band, c.direction, c.severity)
        for c in changes
    }
    assert got == {
        ("desk_a", "escalation"): ("elevated", "high", "deterioration", "high"),
        ("desk_a", "energy_security"): (
            "insufficient-evidence", "watch", "evidence-gained", "medium",
        ),
        ("desk_a", "military_posture"): (
            "high", "insufficient-evidence", "evidence-lost", "medium",
        ),
    }
    # Worst-first ordering: the high-severity deterioration leads.
    assert changes[0].dimension == "escalation"
    # Row attribution: from/to scorecard row ids + changed_at.
    assert changes[0].from_scorecard_row_id == prev["id"]
    assert changes[0].to_scorecard_row_id == head["id"]
    assert changes[0].changed_at == head["produced_at"]


def test_classify_situation_change_edges() -> None:
    now = datetime.now(timezone.utc)
    cursor = now - timedelta(hours=1)
    # Created since cursor -> appeared, no prior state.
    assert classify_situation_change(
        now - timedelta(minutes=5), now - timedelta(minutes=5),
        cursor=cursor, now=now,
    ) == ("appeared", None, "active")
    # Fresh events on a pre-existing situation -> escalating, from unknowable.
    assert classify_situation_change(
        cursor - timedelta(days=3), now - timedelta(minutes=10),
        cursor=cursor, now=now,
    ) == ("escalating", None, "active")
    # Decayed past the 7d boundary inside the window -> resolved.
    le = now - timedelta(days=7, minutes=30)
    assert classify_situation_change(
        cursor - timedelta(days=30), le, cursor=cursor, now=now,
    ) == ("resolved", "dormant", "closed")
    # Decayed past the 2d boundary inside the window -> quieted.
    le = now - timedelta(days=2, minutes=30)
    assert classify_situation_change(
        cursor - timedelta(days=30), le, cursor=cursor, now=now,
    ) == ("quieted", "active", "dormant")


def test_trajectory_desks_reducer() -> None:
    """Time-ascending rows group into per-desk dimension series with the
    persisted effective_confidence + the producer's faithfulness flag."""
    now = datetime.now(timezone.utc)
    rows = [
        _card_row(
            "desk_t",
            {"escalation": {
                "band": "low", "basis": [], "effective_confidence": 0.7,
                "eval": {"faithfulness": 0.9, "faithfulness_flagged": False},
            }},
            produced_at=now - timedelta(days=2),
        ),
        _card_row(
            "desk_t",
            {"escalation": {
                "band": "high", "basis": [], "effective_confidence": 0.61,
                "eval": {"faithfulness": 0.4, "faithfulness_flagged": True},
            },
             "energy_security": {"band": "insufficient-evidence", "basis": [],
                                 "effective_confidence": None}},
            produced_at=now - timedelta(days=1),
        ),
    ]
    desks = trajectory_desks(rows)
    assert [d.target_id for d in desks] == ["desk_t"]
    esc = desks[0].dimensions["escalation"]
    assert [p.band for p in esc] == ["low", "high"]
    assert esc[0].ts < esc[1].ts
    assert esc[0].effective_confidence == pytest.approx(0.7)
    assert esc[0].faithfulness_flagged is False
    assert esc[1].faithfulness_flagged is True
    # Insufficient dimension: band surfaced, confidence honestly None.
    ins = desks[0].dimensions["energy_security"]
    assert ins[0].band == "insufficient-evidence"
    assert ins[0].effective_confidence is None
    assert ins[0].faithfulness_flagged is False


# ---------------------------------------------------------------------------
# App fixture (ephemeral migrated DB + real HTTP — the substrate-reads shape)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def since_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    nats_store = NatsStore(NatsConfig.from_env())
    await nats_store.connect()

    identity = _fixed_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)

    descriptor_registry = DescriptorRegistry(
        pg_store,
        nats_store=nats_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await descriptor_registry.start()

    stack_registry = StackRegistry(pg_store, vault, audit=audit, dlq=dlq)

    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=stack_registry,
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=nats_store,
        conversion_registry=None,
    )

    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_since_router(deps), prefix="/api/v1/v3")

    yield app, deps, pg_store

    await descriptor_registry.stop()
    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(since_app):
    app, _, _ = since_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Insertion helpers — direct SQL, mirrors the provenance write shapes.
# ---------------------------------------------------------------------------


def _uid(label: str) -> str:
    return f"since-{label}-{uuid4().hex[:10]}"


async def _insert_finding(
    pg_store: PostgresStore,
    *,
    title: str = "f",
    confidence: float = 0.8,
    severity: str | None = "medium",
    target_id: str | None = None,
    analyst_id: str | None = "test_analyst",
    produced_at: datetime | None = None,
    superseded_by: UUID | None = None,
    superseded_at: datetime | None = None,
) -> UUID:
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, analyst_id, produced_at, derived_from, schema_uri,
                superseded_by, superseded_at
            ) VALUES (
                $1, 'finding', $2, '', $3, $4, '{}'::jsonb,
                $5, $6, $7, '{}', 'iglu:legba/finding/jsonschema/1-0-0',
                $8, $9
            )
            """,
            row_id, title, confidence, severity, target_id, analyst_id,
            ts, superseded_by, superseded_at,
        )
    return row_id


async def _insert_faith_critique(
    pg_store: PostgresStore,
    *,
    analyzed_output_id: UUID,
    overall_score: float,
    produced_at: datetime | None = None,
) -> UUID:
    """A faithfulness-verify critique (the title pin the laterals key on)."""
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    data = {
        "kind_marker": "critique",
        "analyzed_output_id": str(analyzed_output_id),
        "overall_score": overall_score,
        "scores": {"factuality": overall_score},
    }
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, data,
                analyst_id, produced_at, derived_from, schema_uri
            ) VALUES (
                $1, 'critique', $2, '', $3, $4::jsonb,
                'verify_inline', $5, $6, 'iglu:legba/critique/jsonschema/1-0-0'
            )
            """,
            row_id, f"Faithfulness verify (score {overall_score:.2f})",
            overall_score, json.dumps(data), ts, [analyzed_output_id],
        )
    return row_id


async def _insert_scorecard(
    pg_store: PostgresStore,
    *,
    target_id: str,
    dims: dict[str, Any],
    produced_at: datetime | None = None,
    superseded_by: UUID | None = None,
    superseded_at: datetime | None = None,
) -> UUID:
    """A kind='scorecard' row with the LIVE payload-dump nesting
    (``data.data.bands.dimensions`` — the eval_country_scorecard contract)."""
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    data = {
        "title": f"Scorecard {target_id}", "body": "", "kind_marker": "scorecard",
        "data": {"sub_handler": "scorecard_producer", "bands": {
            "target_id": target_id,
            "generated_at": ts.isoformat(),
            "floors": {"conf_floor": 0.35, "conf_confident": 0.60,
                       "faith_floor": 0.50},
            "dimensions": dims,
            "composition": {"present": False, "basis": []},
        }},
    }
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, data,
                target_id, analyst_id, produced_at, derived_from, schema_uri,
                superseded_by, superseded_at
            ) VALUES (
                $1, 'scorecard', $2, '', 1.0, $3::jsonb,
                $4, 'scorecard_producer', $5, '{}',
                'iglu:legba/scorecard/jsonschema/1-0-0', $6, $7
            )
            """,
            row_id, f"Scorecard {target_id}", json.dumps(data), target_id,
            ts, superseded_by, superseded_at,
        )
    return row_id


async def _insert_situation(
    pg_store: PostgresStore,
    *,
    name: str,
    status_val: str = "active",
    target_id: str | None = None,
    created_at: datetime | None = None,
    last_event_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> UUID:
    row_id = uuid4()
    now = datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO situations (
                id, data, name, status, category, last_event_at,
                event_count, intensity_score, target_id, produced_at,
                derived_from, schema_uri, created_at, updated_at
            ) VALUES (
                $1, '{}'::jsonb, $2, $3, 'test', $4,
                1, 0.5, $5, $6,
                '{}', 'iglu:legba/situation/jsonschema/2-0-0', $7, $8
            )
            """,
            row_id, name, status_val, last_event_at, target_id,
            created_at or now, created_at or now, updated_at or now,
        )
    return row_id


async def _insert_alert(
    pg_store: PostgresStore,
    *,
    title: str,
    severity: str = "medium",
    routing_hint: str = "band_crossing",
    target_id: str | None = None,
    produced_at: datetime | None = None,
) -> UUID:
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    data = {
        "kind_marker": "alert",
        "title": title, "body": "", "severity": severity,
        "routing_hint": routing_hint,
        "data": {"sub_handler": "alert_trigger_scan",
                 "trigger_class": routing_hint},
    }
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, analyst_id, produced_at, derived_from, schema_uri
            ) VALUES (
                $1, 'alert', $2, '', 1.0, $3, $4::jsonb,
                $5, 'alert_trigger_scan', $6, '{}',
                'iglu:legba/alert/jsonschema/1-0-0'
            )
            """,
            row_id, title, severity, json.dumps(data), target_id, ts,
        )
    return row_id


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _section_ids(section: dict[str, Any]) -> list[str]:
    return [item["id"] for item in section["items"]]


# ---------------------------------------------------------------------------
# /since — validation + empty state
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_cursor_validation(client: AsyncClient):
    # Missing cursor -> FastAPI's required-param 422.
    r = await client.get("/api/v1/v3/since")
    assert r.status_code == 422
    # Garbage cursor -> clear 400.
    r = await client.get("/api/v1/v3/since", params={"cursor": "not-a-ts"})
    assert r.status_code == 400
    assert "cursor" in r.json()["detail"]
    # >90d lookback -> clear 400 naming the bound.
    old = datetime.now(timezone.utc) - timedelta(days=91)
    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(old)})
    assert r.status_code == 400
    assert "90" in r.json()["detail"]
    # Exactly-at-the-bound is accepted.
    ok = datetime.now(timezone.utc) - timedelta(days=89)
    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(ok)})
    assert r.status_code == 200
    # A 'Z'-suffixed UTC cursor parses.
    z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = await client.get("/api/v1/v3/since", params={"cursor": z})
    assert r.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_empty_state(client: AsyncClient):
    """A fresh cursor returns a VALID all-empty envelope — never a 404."""
    cursor = datetime.now(timezone.utc)
    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(cursor)})
    assert r.status_code == 200, r.text
    body = r.json()
    for section in ("new_findings", "superseded", "band_changes",
                    "situations", "alerts"):
        assert body[section] == {"items": [], "total": 0, "truncated": False}
        assert body["counts"][section] == 0
    server_now = datetime.fromisoformat(body["server_now"])
    assert server_now >= cursor - timedelta(seconds=1)
    assert datetime.fromisoformat(body["cursor"]) == cursor


# ---------------------------------------------------------------------------
# /since — new_findings (verify gate + structural exemption)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_new_findings_verified_gate(since_app, client: AsyncClient):
    _, _, pg = since_app
    cursor = datetime.now(timezone.utc)
    tid = _uid("nf")

    # Qualifies: verified, min(conf, faith) >= 0.5.
    ok = await _insert_finding(
        pg, title="ok", confidence=0.8, severity="high", target_id=tid,
    )
    await _insert_faith_critique(pg, analyzed_output_id=ok, overall_score=0.9)
    # Excluded: no verify critique at all.
    unverified = await _insert_finding(
        pg, title="unverified", confidence=0.9, severity="high", target_id=tid,
    )
    # Excluded: verified but the fold lands below the 0.50 floor.
    low = await _insert_finding(
        pg, title="low", confidence=0.9, severity="high", target_id=tid,
    )
    await _insert_faith_critique(pg, analyzed_output_id=low, overall_score=0.3)
    # Excluded: structural verify-exempt analyst (even with a critique row).
    exempt = await _insert_finding(
        pg, title="exempt", confidence=0.9, severity="high", target_id=tid,
        analyst_id="graph_mining",
    )
    await _insert_faith_critique(pg, analyzed_output_id=exempt, overall_score=0.9)
    # Excluded: produced BEFORE the cursor (not new).
    stale = await _insert_finding(
        pg, title="stale", confidence=0.8, severity="high", target_id=tid,
        produced_at=cursor - timedelta(hours=2),
    )
    await _insert_faith_critique(pg, analyzed_output_id=stale, overall_score=0.9)
    # Excluded: superseded (not a head).
    dead = await _insert_finding(
        pg, title="dead", confidence=0.8, severity="high", target_id=tid,
        superseded_by=ok, superseded_at=datetime.now(timezone.utc),
    )
    await _insert_faith_critique(pg, analyzed_output_id=dead, overall_score=0.9)

    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(cursor)})
    assert r.status_code == 200, r.text
    body = r.json()
    ids = _section_ids(body["new_findings"])
    assert str(ok) in ids
    for excluded in (unverified, low, exempt, stale, dead):
        assert str(excluded) not in ids
    got = next(i for i in body["new_findings"]["items"] if i["id"] == str(ok))
    assert got["confidence"] == pytest.approx(0.8)
    assert got["faithfulness_score"] == pytest.approx(0.9, abs=1e-6)
    assert got["effective_confidence"] == pytest.approx(0.8)
    assert got["target_id"] == tid
    assert body["counts"]["new_findings"] == body["new_findings"]["total"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_new_findings_severity_ordered(since_app, client: AsyncClient):
    _, _, pg = since_app
    cursor = datetime.now(timezone.utc)
    tid = _uid("sev")
    order_in = [("medium", "m"), ("critical", "c"), ("high", "h")]
    inserted: dict[str, UUID] = {}
    for sev, label in order_in:
        fid = await _insert_finding(
            pg, title=label, confidence=0.8, severity=sev, target_id=tid,
        )
        await _insert_faith_critique(pg, analyzed_output_id=fid, overall_score=0.9)
        inserted[sev] = fid

    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(cursor)})
    assert r.status_code == 200, r.text
    mine = [
        i["severity"] for i in r.json()["new_findings"]["items"]
        if i["id"] in {str(v) for v in inserted.values()}
    ]
    assert mine == ["critical", "high", "medium"]


# ---------------------------------------------------------------------------
# /since — supersession (the reversal surface)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_supersession(since_app, client: AsyncClient):
    _, _, pg = since_app
    cursor = datetime.now(timezone.utc)
    tid = _uid("sup")

    # The replacement head (post-cursor, verified so it ALSO shows in new).
    replacement = await _insert_finding(
        pg, title="the replacement", confidence=0.8, severity="high",
        target_id=tid, analyst_id="unit_b",
    )
    await _insert_faith_critique(
        pg, analyzed_output_id=replacement, overall_score=0.9,
    )
    # WAS the head at cursor time; superseded after the cursor -> surfaces.
    reversed_head = await _insert_finding(
        pg, title="was the head", confidence=0.7, severity="high",
        target_id=tid, analyst_id="unit_a",
        produced_at=cursor - timedelta(hours=3),
        superseded_by=replacement,
        superseded_at=datetime.now(timezone.utc),
    )
    # Superseded BEFORE the cursor -> the client never saw it as head recently.
    old_flip = await _insert_finding(
        pg, title="old flip", confidence=0.7, target_id=tid,
        produced_at=cursor - timedelta(days=2),
        superseded_by=replacement,
        superseded_at=cursor - timedelta(days=1),
    )
    # Produced AND superseded after the cursor -> was never the client's head.
    churn = await _insert_finding(
        pg, title="churn", confidence=0.7, target_id=tid,
        superseded_by=replacement,
        superseded_at=datetime.now(timezone.utc),
    )

    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(cursor)})
    assert r.status_code == 200, r.text
    body = r.json()
    ids = _section_ids(body["superseded"])
    assert str(reversed_head) in ids
    assert str(old_flip) not in ids
    assert str(churn) not in ids
    got = next(
        i for i in body["superseded"]["items"] if i["id"] == str(reversed_head)
    )
    # The reversal names what replaced it.
    assert got["superseded_by"]["id"] == str(replacement)
    assert got["superseded_by"]["title"] == "the replacement"
    assert got["superseded_by"]["analyst_id"] == "unit_b"
    assert datetime.fromisoformat(got["superseded_at"]) > cursor
    # And the replacement itself is in new_findings (verified head).
    assert str(replacement) in _section_ids(body["new_findings"])
    # The superseded row is NOT in new_findings.
    assert str(reversed_head) not in _section_ids(body["new_findings"])


# ---------------------------------------------------------------------------
# /since — band changes
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_band_changes(since_app, client: AsyncClient):
    _, _, pg = since_app
    cursor = datetime.now(timezone.utc)
    desk = _uid("desk")

    head = await _insert_scorecard(
        pg, target_id=desk,
        dims={
            "escalation": {"band": "high", "basis": []},                  # ↑
            "leadership_transition": {"band": "low", "basis": []},        # =
            "energy_security": {"band": "watch", "basis": []},            # ins -> band
            "narrative_coordination": {
                "band": "insufficient-evidence", "basis": [],             # band -> ins
            },
        },
    )
    prev = await _insert_scorecard(
        pg, target_id=desk,
        dims={
            "escalation": {"band": "elevated", "basis": []},
            "leadership_transition": {"band": "low", "basis": []},
            "energy_security": {"band": "insufficient-evidence", "basis": []},
            "narrative_coordination": {"band": "high", "basis": []},
        },
        produced_at=cursor - timedelta(hours=2),
        superseded_by=head,
        superseded_at=datetime.now(timezone.utc),
    )
    # A desk whose ONLY scorecard is post-cursor: no FROM state, no changes.
    lone_desk = _uid("lone")
    await _insert_scorecard(
        pg, target_id=lone_desk,
        dims={"escalation": {"band": "critical", "basis": []}},
    )

    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(cursor)})
    assert r.status_code == 200, r.text
    body = r.json()
    mine = [c for c in body["band_changes"]["items"] if c["target_id"] == desk]
    got = {
        c["dimension"]: (c["from_band"], c["to_band"], c["direction"], c["severity"])
        for c in mine
    }
    assert got == {
        "escalation": ("elevated", "high", "deterioration", "high"),
        "energy_security": (
            "insufficient-evidence", "watch", "evidence-gained", "medium",
        ),
        "narrative_coordination": (
            "high", "insufficient-evidence", "evidence-lost", "medium",
        ),
    }
    for c in mine:
        assert c["from_scorecard_row_id"] == str(prev)
        assert c["to_scorecard_row_id"] == str(head)
    assert not any(
        c["target_id"] == lone_desk for c in body["band_changes"]["items"]
    )
    assert body["counts"]["band_changes"] == body["band_changes"]["total"]


# ---------------------------------------------------------------------------
# /since — situations + alerts
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_situations(since_app, client: AsyncClient):
    _, _, pg = since_app
    now = datetime.now(timezone.utc)
    cursor = now - timedelta(hours=1)

    appeared = await _insert_situation(
        pg, name="appeared", created_at=now - timedelta(minutes=5),
        last_event_at=now - timedelta(minutes=5),
    )
    escalating = await _insert_situation(
        pg, name="escalating", created_at=cursor - timedelta(days=3),
        last_event_at=now - timedelta(minutes=10),
    )
    resolved = await _insert_situation(
        pg, name="resolved", status_val="closed",
        created_at=cursor - timedelta(days=30),
        last_event_at=now - timedelta(days=7, minutes=30),
    )
    quieted = await _insert_situation(
        pg, name="quieted", status_val="dormant",
        created_at=cursor - timedelta(days=30),
        last_event_at=now - timedelta(days=2, minutes=30),
    )
    untouched = await _insert_situation(
        pg, name="untouched", status_val="closed",
        created_at=cursor - timedelta(days=60),
        last_event_at=now - timedelta(days=30),
    )

    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(cursor)})
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {i["id"]: i for i in body["situations"]["items"]}
    assert str(untouched) not in by_id

    assert by_id[str(appeared)]["change"] == "appeared"
    assert by_id[str(appeared)]["from_status"] is None

    assert by_id[str(escalating)]["change"] == "escalating"
    assert by_id[str(escalating)]["from_status"] is None
    assert by_id[str(escalating)]["to_status"] == "active"

    assert by_id[str(resolved)]["change"] == "resolved"
    assert by_id[str(resolved)]["from_status"] == "dormant"
    assert by_id[str(resolved)]["to_status"] == "closed"

    assert by_id[str(quieted)]["change"] == "quieted"
    assert by_id[str(quieted)]["from_status"] == "active"
    assert by_id[str(quieted)]["to_status"] == "dormant"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_alerts(since_app, client: AsyncClient):
    _, _, pg = since_app
    cursor = datetime.now(timezone.utc)
    tid = _uid("al")

    high = await _insert_alert(
        pg, title="band deterioration", severity="high",
        routing_hint="band_crossing", target_id=tid,
    )
    med = await _insert_alert(
        pg, title="baseline pop", severity="medium",
        routing_hint="baseline_deviation", target_id=tid,
    )
    old = await _insert_alert(
        pg, title="old alert", severity="critical", target_id=tid,
        produced_at=cursor - timedelta(hours=5),
    )

    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(cursor)})
    assert r.status_code == 200, r.text
    body = r.json()
    ids = _section_ids(body["alerts"])
    assert str(high) in ids and str(med) in ids
    assert str(old) not in ids
    mine = [i for i in body["alerts"]["items"] if i["target_id"] == tid]
    # Severity-ordered: high before medium.
    assert [i["id"] for i in mine] == [str(high), str(med)]
    got = mine[0]
    assert got["channel"] == "band_crossing"
    assert got["summary"] == "band deterioration"
    assert got["severity"] == "high"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_alerts_channel_filter(since_app, client: AsyncClient):
    """The additive ``?channel=`` scopes ONLY the alerts section (P4-3: the
    map's geo_convergence layer must reach its medium rows past the
    severity-ranked SECTION_CAP; unfiltered behaviour is unchanged)."""
    _, _, pg = since_app
    cursor = datetime.now(timezone.utc)
    tid = _uid("chan")

    geo = await _insert_alert(
        pg,
        title="Geo convergence formed: IQ — 3 source families, 12 signals (48h)",
        severity="medium", routing_hint="geo_convergence", target_id=tid,
    )
    band = await _insert_alert(
        pg, title="band deterioration", severity="high",
        routing_hint="band_crossing", target_id=tid,
    )

    # Unfiltered (default): both rows, severity-ranked — behaviour unchanged.
    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(cursor)})
    assert r.status_code == 200, r.text
    ids = _section_ids(r.json()["alerts"])
    assert str(geo) in ids and str(band) in ids

    # Filtered: ONLY the geo_convergence row; total/counts describe the scope.
    r = await client.get(
        "/api/v1/v3/since",
        params={"cursor": _iso(cursor), "channel": "geo_convergence"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert _section_ids(body["alerts"]) == [str(geo)]
    assert all(i["channel"] == "geo_convergence" for i in body["alerts"]["items"])
    assert body["alerts"]["total"] == 1
    assert body["alerts"]["truncated"] is False
    assert body["counts"]["alerts"] == 1
    # The other sections are NOT scoped by an alerts-channel filter.
    assert set(body["counts"]) == {
        "new_findings", "superseded", "band_changes", "situations", "alerts",
    }

    # Well-formed but unknown channel: valid all-empty section, never a 404.
    r = await client.get(
        "/api/v1/v3/since",
        params={"cursor": _iso(cursor), "channel": "no_such_channel"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["alerts"]["total"] == 0
    assert r.json()["alerts"]["items"] == []

    # Malformed channel: clear 400 naming the expected shape.
    r = await client.get(
        "/api/v1/v3/since",
        params={"cursor": _iso(cursor), "channel": "bad channel!"},
    )
    assert r.status_code == 400
    assert "channel" in r.json()["detail"]


# ---------------------------------------------------------------------------
# /since — honest truncation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_truncation_is_honest(
    since_app, client: AsyncClient, monkeypatch,
):
    """Items cap at SECTION_CAP while total/truncated tell the whole story."""
    _, _, pg = since_app
    monkeypatch.setattr(since_api, "SECTION_CAP", 2)
    cursor = datetime.now(timezone.utc)
    tid = _uid("cap")
    for n in range(3):
        fid = await _insert_finding(
            pg, title=f"capped-{n}", confidence=0.8, severity="high",
            target_id=tid,
        )
        await _insert_faith_critique(pg, analyzed_output_id=fid, overall_score=0.9)
        await _insert_alert(pg, title=f"alert-{n}", target_id=tid)

    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(cursor)})
    assert r.status_code == 200, r.text
    body = r.json()
    for section in ("new_findings", "alerts"):
        assert len(body[section]["items"]) == 2
        assert body[section]["total"] == 3
        assert body[section]["truncated"] is True
        assert body["counts"][section] == 3


# ---------------------------------------------------------------------------
# /eval/band_trajectory
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_band_trajectory_shape(since_app, client: AsyncClient):
    _, _, pg = since_app
    now = datetime.now(timezone.utc)
    desk = _uid("traj")

    third = await _insert_scorecard(
        pg, target_id=desk,
        dims={"escalation": {
            "band": "high", "basis": [], "effective_confidence": 0.66,
            "eval": {"faithfulness": 0.8, "faithfulness_flagged": False},
        }},
        produced_at=now - timedelta(days=1),
    )
    # Older rows are SUPERSEDED heads — still part of the trajectory. An hour
    # INSIDE the 2-day boundary so the windowed call below is not flaky on the
    # (Python now) vs (DB now()) skew at the exact edge.
    second = await _insert_scorecard(
        pg, target_id=desk,
        dims={"escalation": {
            "band": "elevated", "basis": [], "effective_confidence": 0.55,
            "eval": {"faithfulness": 0.4, "faithfulness_flagged": True},
        }},
        produced_at=now - timedelta(days=1, hours=23),
        superseded_by=third, superseded_at=now - timedelta(days=1),
    )
    first = await _insert_scorecard(
        pg, target_id=desk,
        dims={"escalation": {
            "band": "low", "basis": [], "effective_confidence": 0.7,
            "eval": {"faithfulness": 0.9, "faithfulness_flagged": False},
        }},
        produced_at=now - timedelta(days=3),
        superseded_by=second, superseded_at=now - timedelta(days=2),
    )

    r = await client.get(
        "/api/v1/v3/eval/band_trajectory",
        params={"target_id": desk, "days": 30},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 30
    assert body["truncated"] is False
    assert [d["target_id"] for d in body["desks"]] == [desk]
    esc = body["desks"][0]["dimensions"]["escalation"]
    assert [p["band"] for p in esc] == ["low", "elevated", "high"]
    assert [p["scorecard_row_id"] for p in esc] == [
        str(first), str(second), str(third),
    ]
    ts = [datetime.fromisoformat(p["ts"]) for p in esc]
    assert ts == sorted(ts)
    assert [p["effective_confidence"] for p in esc] == (
        [pytest.approx(0.7), pytest.approx(0.55), pytest.approx(0.66)]
    )
    assert [p["faithfulness_flagged"] for p in esc] == [False, True, False]

    # All desks when target_id omitted — includes this desk.
    r = await client.get("/api/v1/v3/eval/band_trajectory")
    assert r.status_code == 200, r.text
    assert desk in {d["target_id"] for d in r.json()["desks"]}

    # A 2-day window excludes the 3-day-old row (honest windowing).
    r = await client.get(
        "/api/v1/v3/eval/band_trajectory",
        params={"target_id": desk, "days": 2},
    )
    assert r.status_code == 200, r.text
    bands = [
        p["band"] for p in r.json()["desks"][0]["dimensions"]["escalation"]
    ]
    assert bands == ["elevated", "high"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_band_trajectory_days_validation(client: AsyncClient):
    r = await client.get(
        "/api/v1/v3/eval/band_trajectory", params={"days": 91},
    )
    assert r.status_code == 400
    assert "90" in r.json()["detail"]
    r = await client.get(
        "/api/v1/v3/eval/band_trajectory", params={"days": 0},
    )
    assert r.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_dev_mode_accepts_anonymous(client: AsyncClient):
    """Documents the dev-mode auth posture (the substrate-reads precedent):
    no configured token + LEGBA_DEV_MODE=1 -> require_bearer returns
    'anonymous' and the bearer-gated routes serve."""
    cursor = datetime.now(timezone.utc)
    r = await client.get("/api/v1/v3/since", params={"cursor": _iso(cursor)})
    assert r.status_code == 200
