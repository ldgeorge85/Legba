# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the three substrate-read endpoints
(`/api/v1/findings`, `/situations`, `/signals`).

These run against the live substrate via the `migrated_pg` fixture from
`conftest.py`. We build a real FastAPI app, wire it to a real
`DescriptorRegistry` (whose `.pg` pool is the read path the
substrate-read router uses), and hit HTTP. No mocks for substrate
boundaries — Lewis's hard rule.

Coverage per endpoint:
  * empty-result path (returns `{"data": [], "next_cursor": null}`).
  * single-row path (insert one row, fetch, verify shape).
  * pagination (insert >limit rows, walk the cursor, verify no dupes).
  * filter combinations (target_id + since, plus one endpoint-specific
    filter where applicable).

Auth: tests run in dev-mode (no `LEGBA_REGISTRY_API_TOKEN`), so
`require_bearer` returns `"anonymous"` and unauthenticated requests
pass. One auth-specific test asserts that this is the case so the
default test posture is documented.
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

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import (
    API_TOKEN_ENV,
    RegistryAPIDeps,
)
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.substrate_reads_api import (
    build_substrate_reads_router,
)
from legba.data.registry.vocabulary_cache import VocabularyCache


# Mandatory env for vault + signing identity (mirrors the L-113 integ test).
_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)


def _fixed_identity() -> SigningIdentity:
    # Exactly 32 bytes — deterministic seed for test reproducibility.
    seed = b"substrate-reads-api-test-seed-xy"
    assert len(seed) == 32
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:substrate-reads-test",
    )


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def substrate_app(migrated_pg: PostgresConfig):
    """Build a FastAPI app with the substrate-reads router mounted.

    Note: `migrated_pg` is session-scoped and shared with the rest of
    the data_pkg test suite. We do NOT truncate the underlying tables —
    each test below scopes its query by a unique `target_id` so it only
    observes its own rows. That keeps us from clobbering state inserted
    by sibling tests that share the same migrated DB.
    """
    # Dev-mode auth: no token configured → `require_bearer` returns
    # "anonymous" for missing/any bearer.
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

    stack_registry = StackRegistry(
        pg_store,
        vault,
        audit=audit,
        dlq=dlq,
    )

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
    app.include_router(
        build_substrate_reads_router(deps), prefix="/api/v1",
    )

    yield app, deps, pg_store

    await descriptor_registry.stop()
    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(substrate_app):
    app, _, _ = substrate_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Insertion helpers — direct SQL, mirrors the provenance/writes.py inserts.
# ---------------------------------------------------------------------------


def _unique_target_id(label: str) -> str:
    """Per-test unique target_id so queries scope to only this test's rows."""
    return f"sra-{label}-{uuid4().hex[:10]}"


def _unique_source_id(label: str) -> str:
    """Per-test unique source_id. Source-first signals are target-agnostic;
    the read endpoint scopes signals by ``source_id`` (an exact filter), so
    each signal test uses a unique source_id to observe only its own rows."""
    return f"src_{label}_{uuid4().hex[:10]}".lower()


async def _insert_finding(
    pg_store: PostgresStore,
    *,
    title: str = "f",
    body: str = "",
    confidence: float = 0.7,
    severity: str | None = "medium",
    target_id: str | None = None,
    analyst_id: str | None = "test_analyst",
    produced_at: datetime | None = None,
    schema_uri: str = "iglu:legba/finding/jsonschema/1-0-0",
    data: dict[str, Any] | None = None,
) -> UUID:
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'finding', $2, $3, $4, $5, $6::jsonb,
                $7, NULL, $8, NULL,
                $9, $10, $11, NULL
            )
            """,
            row_id, title, body, confidence, severity,
            json.dumps(data if data is not None else {}), target_id, analyst_id,
            ts, [], schema_uri,
        )
    return row_id


async def _insert_critique(
    pg_store: PostgresStore,
    *,
    analyzed_output_id: UUID,
    overall_score: float,
    produced_at: datetime | None = None,
    data_extra: dict | None = None,
    title: str | None = None,
) -> UUID:
    """Insert an L-175 critique row (kind='critique') graded against a finding.

    Mirrors the production write path: the CritiquePayload model_dump lands in
    ``data`` (so ``data.overall_score`` + ``data.analyzed_output_id`` are the
    join keys S3's /findings LEFT JOIN reads), and the analyzed finding's id is
    the first ``derived_from`` edge.

    ``data_extra`` (P0-T3) is merged into the critique's nested ``data`` key
    (``data.data``) — mirrors how the faithfulness verify pass stores its
    ``verification`` block via ``CritiquePayload.data`` so the /findings lateral
    can surface ``data->'data'->'verification'``.

    ``title`` defaults to the faithfulness-verify convention
    (``"Faithfulness verify (score …)"``) because the /findings lateral pins on
    ``title LIKE 'Faithfulness verify%'`` (S8-T2) — ONLY the faithfulness verdict
    drives ``effective_confidence``. Pass a generic ``title`` (e.g. ``"critique"``
    for a country_critic) to insert a row the lateral deliberately IGNORES.
    """
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    row_title = title if title is not None else f"Faithfulness verify (score {overall_score:.2f})"
    data = {
        "kind_marker": "critique",
        "analyzed_output_id": str(analyzed_output_id),
        "overall_score": overall_score,
        "scores": {"factuality": overall_score},
    }
    if data_extra is not None:
        data["data"] = data_extra
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'critique', $6, '', $2, NULL, $3::jsonb,
                NULL, NULL, 'country_critic', NULL,
                $4, $5, 'iglu:legba/critique/jsonschema/1-0-0', NULL
            )
            """,
            row_id, overall_score, json.dumps(data),
            ts, [analyzed_output_id], row_title,
        )
    return row_id


async def _insert_situation(
    pg_store: PostgresStore,
    *,
    name: str = "s",
    status_val: str = "active",
    target_id: str | None = None,
    produced_at: datetime | None = None,
) -> UUID:
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO situations (
                id, data, name, status, category, last_event_at,
                event_count, intensity_score,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, $2::jsonb, $3, $4, '', NULL,
                0, 0.5,
                $5, NULL, NULL, NULL,
                $6, $7, 'iglu:legba/situation/jsonschema/2-0-0', NULL
            )
            """,
            row_id, json.dumps({}), name, status_val,
            target_id, ts, [],
        )
    return row_id


async def _insert_signal(
    pg_store: PostgresStore,
    *,
    title: str = "sig",
    language: str = "en",
    source_id: str = "src_test",
    geo: list[str] | None = None,
    produced_at: datetime | None = None,
) -> UUID:
    # Source-first pivot: the `signals` table was re-cut (migration 0024) —
    # target-agnostic + modality-first. ``title`` lives in ``payload``,
    # ``produced_at``→``fetched_at``, and the per-target columns are gone.
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals (
                id, source_id, source_version, produced_by_kind,
                fetched_at, modality, payload, language, geo,
                content_hash, derived_from, schema_uri
            ) VALUES (
                $1, $2, '', 'source',
                $3, 'text', $4::jsonb, $5, $6::text[],
                '', $7::uuid[], 'iglu:legba/signal/jsonschema/3-0-0'
            )
            """,
            row_id, source_id, ts, json.dumps({"title": title}),
            language, geo or [], [],
        )
    return row_id


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_empty(substrate_app, client: AsyncClient):
    # Query a target_id no row exists for → guaranteed-empty result.
    tid = _unique_target_id("findings-empty")
    r = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"data": [], "next_cursor": None}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_single_row_shape(substrate_app, client: AsyncClient):
    _, _, pg_store = substrate_app
    tid = _unique_target_id("findings-shape")
    row_id = await _insert_finding(
        pg_store,
        title="Brazil energy import spike",
        body="Spot demand jumped 12 % WoW.",
        confidence=0.81,
        severity="high",
        target_id=tid,
        analyst_id="trend_synth",
    )

    r = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_cursor"] is None
    assert len(body["data"]) == 1
    row = body["data"][0]
    assert row["id"] == str(row_id)
    assert row["kind"] == "finding"
    assert row["title"] == "Brazil energy import spike"
    assert row["severity"] == "high"
    assert row["target_id"] == tid
    assert row["analyst_id"] == "trend_synth"
    assert row["confidence"] == pytest.approx(0.81, abs=1e-4)
    # P0-4 — a verify-covered analyst carries NO structural exemption stamp.
    assert row["verify_exempt"] is None
    # produced_at is ISO-8601.
    datetime.fromisoformat(row["produced_at"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_structural_analyst_stamped_verify_exempt(
    substrate_app, client: AsyncClient,
):
    """P0-4 — a finding from a verify-exempt deterministic structural analyst
    (graph_mining et al.) is stamped ``verify_exempt: "structural"`` by the
    projection, so every client can badge it `unverified — structural`."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("findings-structural")
    row_id = await _insert_finding(
        pg_store,
        title="Graph mining: 3 communities, 1 proxy chain",
        confidence=1.0,
        severity=None,
        target_id=tid,
        analyst_id="graph_mining",
    )

    r = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()["data"]}
    assert rows[str(row_id)]["verify_exempt"] == "structural"
    # The stamp never invents a verify block or a score.
    assert rows[str(row_id)]["verification"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_structural_verified_badge_and_off_safe(
    substrate_app, client: AsyncClient,
):
    """C2b — through the real /findings laterals: a structural finding WITH a
    passing structural critique reads ``structural-verified`` and surfaces the
    structural verification detail; a FLAGGED one stays ``structural`` and does
    NOT demote effective_confidence (OFF-safe: overall_score pinned to 1.0)."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("findings-structural-verified")

    verified_fid = await _insert_finding(
        pg_store, title="Geo convergence scan: 1 formation",
        confidence=1.0, severity=None, target_id=tid,
        analyst_id="geo_convergence_scan",
    )
    await _insert_critique(
        pg_store, analyzed_output_id=verified_fid, overall_score=1.0,
        title="Structural verify (verified)",
        data_extra={"verification": {
            "structural_verify": True, "structural_verified": True,
            "checkable_claims": 1, "supported_claims": 1,
        }},
    )

    flagged_fid = await _insert_finding(
        pg_store, title="Geo convergence scan: miscounted",
        confidence=1.0, severity=None, target_id=tid,
        analyst_id="geo_convergence_scan",
    )
    # OFF-safe: even a FLAGGED verdict is written with overall_score=1.0 by
    # default, so it never demotes; structural_verified=false keeps the honest
    # badge.
    await _insert_critique(
        pg_store, analyzed_output_id=flagged_fid, overall_score=1.0,
        title="Structural verify (FLAGGED — 1 miscount(s))",
        data_extra={"verification": {
            "structural_verify": True, "structural_verified": False,
            "checkable_claims": 1, "supported_claims": 0, "miscount_claims": 1,
        }},
    )

    r = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()["data"]}

    v = rows[str(verified_fid)]
    assert v["verify_exempt"] == "structural-verified"
    # The structural verification detail is surfaced (no faithfulness block).
    assert v["verification"]["structural_verify"] is True
    assert v["effective_confidence"] == pytest.approx(1.0)

    f = rows[str(flagged_fid)]
    assert f["verify_exempt"] == "structural"          # not verified — honest
    assert f["verification"]["miscount_claims"] == 1   # the flag is still shown
    assert f["effective_confidence"] == pytest.approx(1.0)  # OFF-safe: no demotion


def test_hydrate_finding_stamps_structural_exemption():
    """Pure-logic P0-4 coverage of the projection stamp (no DB): analyst_id in
    the structural registry → ``verify_exempt="structural"``; anything else
    (verified analysts, NULL analyst_id) → honest ``None``."""
    from legba.data.registry.substrate_reads_api import _hydrate_finding

    def _row(analyst_id: str | None) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "id": uuid4(),
            "kind": "finding",
            "title": "t",
            "body": "",
            "confidence": 1.0,
            "severity": None,
            "data": "{}",
            "target_id": None,
            "target_version": None,
            "analyst_id": analyst_id,
            "analyst_version": None,
            "produced_at": now,
            "derived_from": [],
            "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
            "run_id": None,
            "created_at": now,
            "critic_score": None,
            "verification": None,
        }

    assert _hydrate_finding(_row("graph_mining")).verify_exempt == "structural"
    assert _hydrate_finding(_row("thematic_proposal")).verify_exempt == "structural"
    assert _hydrate_finding(_row("indicator_tracker")).verify_exempt == "structural"
    assert _hydrate_finding(_row("country_assessor")).verify_exempt is None
    assert _hydrate_finding(_row(None)).verify_exempt is None


def test_hydrate_finding_below_floor_mark():
    """E-1 (2026-07-27 sweep item 3) — the EXPLICIT below-floor annotation:
    ``True`` iff a GRADED finding's effective_confidence sits under the
    system-wide 0.50 floor; ``False`` when graded and clearing; honest ``None``
    when never graded (no fabricated verdict). ANNOTATE-not-exclude: the row
    itself still serves — the mark makes it distinguishable."""
    from legba.data.analysts.deterministic_handlers.scorecard_banding import (
        FAITH_FLOOR,
    )
    from legba.data.registry.substrate_reads_api import (
        _FAITH_FLOOR,
        _hydrate_finding,
    )

    # The registry-slim mirror stays in lockstep with the ONE source constant.
    assert _FAITH_FLOOR == FAITH_FLOOR

    def _row(confidence: float, critic_score: float | None) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "id": uuid4(), "kind": "finding", "title": "t", "body": "",
            "confidence": confidence, "severity": None, "data": "{}",
            "target_id": None, "target_version": None,
            "analyst_id": "country_assessor", "analyst_version": None,
            "produced_at": now, "derived_from": [],
            "schema_uri": "iglu:legba/finding/jsonschema/1-0-0", "run_id": None,
            "created_at": now, "critic_score": critic_score,
            "verification": None,
        }

    # Graded below the floor (min(0.9, 0.30) = 0.30 < 0.50) → True.
    low = _hydrate_finding(_row(0.9, 0.30))
    assert low.below_floor is True
    assert low.effective_confidence == pytest.approx(0.30)
    # Graded and clearing (min(0.9, 0.85) = 0.85) → False.
    ok = _hydrate_finding(_row(0.9, 0.85))
    assert ok.below_floor is False
    # Low OWN confidence but graded fine (min(0.3, 0.9) = 0.3) → True (the
    # floor is on the FOLD, min(confidence, faithfulness) — the 0.50 decision).
    assert _hydrate_finding(_row(0.3, 0.9)).below_floor is True
    # Never graded → honest None, even at low confidence (no fabricated verdict).
    ungraded = _hydrate_finding(_row(0.2, None))
    assert ungraded.below_floor is None
    # The boundary: exactly at the floor clears (>= FAITH_FLOOR is not below).
    assert _hydrate_finding(_row(1.0, FAITH_FLOOR)).below_floor is False


def test_hydrate_finding_structural_verified_badge_flip():
    """C2b — a structural finding WITH a passing structural critique reads
    ``structural-verified``; a FLAGGED / absent one stays honest ``structural``;
    a non-structural analyst is never badged. OFF-safe: no effective_confidence
    demotion by default (the structural score never gates unless the flag is on)."""
    from legba.data.registry.substrate_reads_api import _hydrate_finding

    def _row(analyst_id, *, structural_verified=None, structural_score=None):
        now = datetime.now(timezone.utc)
        return {
            "id": uuid4(), "kind": "finding", "title": "t", "body": "",
            "confidence": 1.0, "severity": None, "data": "{}",
            "target_id": None, "target_version": None, "analyst_id": analyst_id,
            "analyst_version": None, "produced_at": now, "derived_from": [],
            "schema_uri": "iglu:legba/finding/jsonschema/1-0-0", "run_id": None,
            "created_at": now, "critic_score": None, "verification": None,
            "structural_verified": structural_verified,
            "structural_score": structural_score,
            "structural_verification": None,
        }

    # Passing structural critique (server stamps 'true' text off the jsonb ->>).
    verified = _hydrate_finding(_row("geo_convergence_scan", structural_verified="true"))
    assert verified.verify_exempt == "structural-verified"
    # Flagged / not-verified structural critique → honest 'structural'.
    flagged = _hydrate_finding(_row("geo_convergence_scan", structural_verified="false"))
    assert flagged.verify_exempt == "structural"
    # No structural critique at all → 'structural'.
    assert _hydrate_finding(_row("geo_convergence_scan")).verify_exempt == "structural"
    # Non-structural analyst is never badged, verdict or not.
    assert _hydrate_finding(_row("country_assessor", structural_verified="true")).verify_exempt is None
    # OFF-safe: a flagged structural score does NOT demote effective_confidence.
    off = _hydrate_finding(
        _row("geo_convergence_scan", structural_verified="false", structural_score=0.0)
    )
    assert off.effective_confidence == 1.0


# ---------------------------------------------------------------------------
# `findings_projection` (7b-v leaf) — pure, no DB. Pins the judgment weight's
# citation key set so the route (`_hydrate_finding_judgment`) and the leaf
# cannot drift apart.
# ---------------------------------------------------------------------------


def test_findings_projection_judgment_fields_pinned():
    """The exact key set — no more, no less — every judgment-weight citation
    carries. If this test moves, `CitationJudgmentEntry` (substrate_reads_api)
    and `JudgmentFindingRow`/checkedBand's fixture (morningReadBands.test.ts)
    must move with it."""
    from legba.data.findings_projection import JUDGMENT_FIELDS

    assert JUDGMENT_FIELDS == {
        "ordinal", "source", "source_id", "produced_at",
        "single_source", "wire_folded", "marker_class",
    }


def test_findings_projection_reduces_to_pinned_keys_only():
    """A richly-populated stored citation entry (evidence_text, title, tier,
    derived_from, effective_confidence, ref_id — the bulk of what makes a
    finding's `data` heavy) reduces to EXACTLY `JUDGMENT_FIELDS`, values
    carried through untouched."""
    from legba.data.findings_projection import JUDGMENT_FIELDS, project_citations

    rich_entry = {
        "marker": "[[ref:1]]",
        "ordinal": 1,
        "ref_id": "11111111-1111-1111-1111-111111111111",
        "ref_kind": "finding",
        "tier": "periphery",
        "source": "security_desk",
        "source_id": "src_reuters",
        "target_id": "country_g20_br",
        "title": "Border incident escalates",
        "produced_at": "2026-09-24T07:00:00Z",
        "evidence_text": "a full paragraph of quoted prose" * 20,
        "effective_confidence": 0.81,
        "derived_from": ["22222222-2222-2222-2222-222222222222"],
        "single_source": True,
        "wire_folded": True,
        "marker_class": None,
    }
    [reduced] = project_citations([rich_entry])
    assert set(reduced.keys()) == JUDGMENT_FIELDS
    assert reduced["ordinal"] == 1
    assert reduced["source"] == "security_desk"
    assert reduced["source_id"] == "src_reuters"
    assert reduced["produced_at"] == "2026-09-24T07:00:00Z"
    assert reduced["single_source"] is True
    assert reduced["wire_folded"] is True
    assert reduced["marker_class"] is None
    for shed in ("evidence_text", "title", "tier", "derived_from",
                 "effective_confidence", "ref_id", "target_id", "marker"):
        assert shed not in reduced


def test_findings_projection_absent_leaves_default_never_fabricate():
    """A minimal signal citation (no single_source/wire_folded/marker_class
    stamped — the common case) reduces with honest defaults, never a
    fabricated `True`."""
    from legba.data.findings_projection import project_citations

    [reduced] = project_citations([{"ordinal": 2, "signal_id": "sig-1"}])
    assert reduced == {
        "ordinal": 2, "source": None, "source_id": None, "produced_at": None,
        "single_source": False, "wire_folded": False, "marker_class": None,
    }


def test_findings_projection_handles_string_and_malformed_input():
    """`citations_raw` arrives as whatever the pool handed back — a decoded
    list (the common case), a raw JSON string (a codec-less connection), or
    absent — and a non-mapping entry (never a citation this reduction
    fabricates a shape for) is skipped rather than raising."""
    from legba.data.findings_projection import project_citations

    assert project_citations(None) == []
    assert project_citations("not json") == []
    assert project_citations({"not": "a list"}) == []
    assert project_citations([{"ordinal": 1}, "bogus-entry", 42]) == [
        {
            "ordinal": 1, "source": None, "source_id": None,
            "produced_at": None, "single_source": False,
            "wire_folded": False, "marker_class": None,
        },
    ]
    import json
    as_string = json.dumps([{"ordinal": 3, "source": "wire"}])
    assert project_citations(as_string)[0]["ordinal"] == 3
    assert project_citations(as_string)[0]["source"] == "wire"


def test_findings_projection_ordinal_never_string_coerced():
    """An ordinal is `None` unless it is genuinely an `int` — never guessed
    from a string, mirroring `export_api._ordinal`."""
    from legba.data.findings_projection import project_citations

    [reduced] = project_citations([{"ordinal": "1"}])
    assert reduced["ordinal"] is None


def test_hydrate_finding_judgment_shape():
    """`_hydrate_finding_judgment` maps a `fields=judgment` SQL row to
    `FindingJudgmentRow` — the verify block WHOLE, citations reduced, and
    NEVER a `data`/`derived_from`/`body` key on the model at all."""
    from legba.data.registry.substrate_reads_api import (
        FindingJudgmentRow,
        _hydrate_finding_judgment,
    )

    now = datetime.now(timezone.utc)
    row = {
        "id": uuid4(), "kind": "finding", "title": "Brazil energy import spike",
        "analyst_id": "world_assessor", "analyst_version": "3",
        "target_id": "country_g20_br", "target_version": "7",
        "produced_at": now, "severity": "high", "confidence": 0.81,
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "verification": json.dumps({
            "faithfulness_score": 0.62, "judge_status": "llm",
            "unsupported_spans": [
                {"text": "Troop numbers doubled.", "reason": "no_citation", "markers": []},
            ],
        }),
        "citations_raw": json.dumps([
            {"ordinal": 1, "source": "security_desk", "single_source": True,
             "evidence_text": "shed on the wire"},
        ]),
    }
    out = _hydrate_finding_judgment(row)
    assert isinstance(out, FindingJudgmentRow)
    assert out.id == str(row["id"])
    assert out.title == "Brazil energy import spike"
    assert out.analyst_version == "3"
    assert out.target_version == "7"
    assert out.verification["faithfulness_score"] == pytest.approx(0.62, abs=1e-4)
    assert out.verification["unsupported_spans"][0]["text"] == "Troop numbers doubled."
    assert len(out.citations) == 1
    assert out.citations[0].ordinal == 1
    assert out.citations[0].source == "security_desk"
    assert out.citations[0].single_source is True
    dumped = out.model_dump()
    for absent in ("data", "derived_from", "body", "critic_score",
                   "effective_confidence", "verify_exempt", "below_floor"):
        assert absent not in dumped


def test_hydrate_finding_judgment_legacy_row_has_null_verification_empty_citations():
    """A pre-assembly / unverified / uncited row → `verification` is `null`
    (never fabricated) and `citations` is `[]` (never guessed)."""
    from legba.data.registry.substrate_reads_api import _hydrate_finding_judgment

    now = datetime.now(timezone.utc)
    row = {
        "id": uuid4(), "kind": "finding", "title": "legacy read",
        "analyst_id": None, "analyst_version": None,
        "target_id": None, "target_version": None,
        "produced_at": now, "severity": None, "confidence": 0.5,
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "verification": None, "citations_raw": None,
    }
    out = _hydrate_finding_judgment(row)
    assert out.verification is None
    assert out.citations == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_pagination_walks_cursor(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    tid = _unique_target_id("findings-page")

    # Insert 7 rows at strictly decreasing produced_at so order is stable.
    now = datetime.now(timezone.utc)
    inserted: list[str] = []
    for i in range(7):
        rid = await _insert_finding(
            pg_store,
            title=f"f-{i}",
            target_id=tid,
            produced_at=now - timedelta(seconds=i),
        )
        inserted.append(str(rid))
    # inserted[0] is newest → comes first.

    # Walk in pages of 3: expect (3, 3, 1).
    seen: list[str] = []
    cursor: str | None = None
    page_count = 0
    while True:
        params: dict[str, Any] = {"limit": 3, "target_id": tid}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/findings", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        page_count += 1
        page_ids = [row["id"] for row in body["data"]]
        seen.extend(page_ids)
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert page_count < 10  # safety net

    assert page_count == 3
    assert seen == inserted  # newest first, no dupes, no skips


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_filter_target_and_since(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    tid_a = _unique_target_id("findings-tA")
    tid_b = _unique_target_id("findings-tB")

    base = datetime.now(timezone.utc)
    # Match: target=A, recent.
    keep_id = await _insert_finding(
        pg_store, target_id=tid_a, produced_at=base,
    )
    # Skip: target=B.
    await _insert_finding(pg_store, target_id=tid_b, produced_at=base)
    # Skip: target=A but too old.
    await _insert_finding(
        pg_store, target_id=tid_a, produced_at=base - timedelta(days=2),
    )

    r = await client.get(
        "/api/v1/findings",
        params={
            "target_id": tid_a,
            "since": (base - timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [row["id"] for row in body["data"]]
    assert ids == [str(keep_id)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_filter_severity(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    tid = _unique_target_id("findings-sev")
    high_id = await _insert_finding(
        pg_store, severity="high", target_id=tid,
    )
    await _insert_finding(pg_store, severity="low", target_id=tid)

    r = await client.get(
        "/api/v1/findings",
        params={"severity": "high", "target_id": tid},
    )
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["data"]]
    assert ids == [str(high_id)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_limit_validation(client: AsyncClient):
    r = await client.get("/api/v1/findings", params={"limit": 0})
    assert r.status_code == 400
    r = await client.get("/api/v1/findings", params={"limit": 501})
    assert r.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_dev_mode_accepts_anonymous(client: AsyncClient):
    # No Authorization header → require_bearer returns "anonymous" in dev.
    r = await client.get("/api/v1/findings")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# S3 — critic actuator (critic_score surface + confidence fold)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_uncritiqued_has_null_critic_score(
    substrate_app, client: AsyncClient,
):
    """A finding with no critique → critic_score is null and
    effective_confidence falls back to the finding's own confidence."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("critic-none")
    await _insert_finding(
        pg_store, title="uncritiqued", confidence=0.8, target_id=tid,
    )
    r = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]
    assert row["critic_score"] is None
    assert row["effective_confidence"] == pytest.approx(0.8, abs=1e-4)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_critic_score_folds_confidence_down(
    substrate_app, client: AsyncClient,
):
    """A finding the critic graded BELOW its own confidence → critic_score is
    surfaced and effective_confidence = min(confidence, critic_score). This is
    the actuation: the poorly-graded finding reads as lower-confidence."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("critic-fold")
    fid = await _insert_finding(
        pg_store, title="overconfident", confidence=0.9, target_id=tid,
    )
    await _insert_critique(pg_store, analyzed_output_id=fid, overall_score=0.3)

    r = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]
    assert row["id"] == str(fid)
    assert row["confidence"] == pytest.approx(0.9, abs=1e-4)
    assert row["critic_score"] == pytest.approx(0.3, abs=1e-4)
    # min(0.9, 0.3) = 0.3 — the critic pulled the surfaced confidence down.
    assert row["effective_confidence"] == pytest.approx(0.3, abs=1e-4)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_high_critic_score_does_not_inflate(
    substrate_app, client: AsyncClient,
):
    """A critic score ABOVE the finding's confidence never inflates it —
    the fold is min(), so a well-graded finding keeps its own confidence."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("critic-high")
    fid = await _insert_finding(
        pg_store, title="modest", confidence=0.5, target_id=tid,
    )
    await _insert_critique(pg_store, analyzed_output_id=fid, overall_score=0.95)

    r = await client.get("/api/v1/findings", params={"target_id": tid})
    row = r.json()["data"][0]
    assert row["critic_score"] == pytest.approx(0.95, abs=1e-4)
    assert row["effective_confidence"] == pytest.approx(0.5, abs=1e-4)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_latest_critique_wins(
    substrate_app, client: AsyncClient,
):
    """When a finding was re-critiqued, the LATEST critique's score is the one
    surfaced (the lateral picks newest produced_at)."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("critic-latest")
    fid = await _insert_finding(
        pg_store, title="rec", confidence=0.9, target_id=tid,
    )
    older = datetime.now(timezone.utc) - timedelta(hours=2)
    await _insert_critique(
        pg_store, analyzed_output_id=fid, overall_score=0.2, produced_at=older,
    )
    await _insert_critique(pg_store, analyzed_output_id=fid, overall_score=0.6)

    r = await client.get("/api/v1/findings", params={"target_id": tid})
    row = r.json()["data"][0]
    assert row["critic_score"] == pytest.approx(0.6, abs=1e-4)
    assert row["effective_confidence"] == pytest.approx(0.6, abs=1e-4)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_faithfulness_verification_block_surfaces(
    substrate_app, client: AsyncClient,
):
    """P0-T3 / ACCEPTANCE 4: a finding with a faithfulness critique surfaces the
    verification block naming the unsupported spans AND folds effective_confidence
    down to the faithfulness score."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("verify-block")
    fid = await _insert_finding(
        pg_store, title="cited but partly fabricated", confidence=0.85, target_id=tid,
    )
    verification = {
        "verification": {
            "faithfulness_score": 0.5,
            "checkable_claims": 2,
            "supported_claims": 1,
            "unsupported_spans": [
                {"text": "A coup attempt overnight.", "reason": "no_citation", "markers": []},
            ],
            "judge_status": "deterministic",
            "judge_unavailable_reason": "flag_off",
        }
    }
    await _insert_critique(
        pg_store, analyzed_output_id=fid, overall_score=0.5, data_extra=verification,
    )

    r = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]
    assert row["id"] == str(fid)
    # The gate demoted the surfaced confidence to the faithfulness score.
    assert row["effective_confidence"] == pytest.approx(0.5, abs=1e-4)
    # The verification block explains WHY (names the unsupported span).
    assert row["verification"] is not None
    assert row["verification"]["faithfulness_score"] == pytest.approx(0.5, abs=1e-4)
    assert row["verification"]["unsupported_spans"][0]["reason"] == "no_citation"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_generic_critique_cannot_overwrite_faithfulness_demotion(
    substrate_app, client: AsyncClient,
):
    """S8-T2 — a later GENERIC critique (e.g. a country_critic, title='critique')
    landing AFTER the faithfulness verdict must NOT win the lateral and un-demote
    the finding. The lateral pins on ``title LIKE 'Faithfulness verify%'``, so
    effective_confidence stays at the faithfulness score and the verification
    block still surfaces. Regression guard for the anchor (b) defect."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("verify-pin")
    fid = await _insert_finding(
        pg_store, title="cited but partly fabricated", confidence=0.9, target_id=tid,
    )
    verification = {
        "verification": {
            "faithfulness_score": 0.3,
            "checkable_claims": 3,
            "supported_claims": 1,
            "unsupported_spans": [
                {"text": "A coup attempt overnight.", "reason": "no_citation", "markers": []},
            ],
            "judge_status": "deterministic",
        }
    }
    # 1) The faithfulness verdict demotes to 0.3 (lands FIRST).
    older = datetime.now(timezone.utc) - timedelta(hours=1)
    await _insert_critique(
        pg_store, analyzed_output_id=fid, overall_score=0.3,
        data_extra=verification, produced_at=older,
    )
    # 2) A LATER generic country_critic critique scores it high — the row that
    #    (pre-fix) won the produced_at race and un-demoted the finding.
    await _insert_critique(
        pg_store, analyzed_output_id=fid, overall_score=0.9, title="critique",
    )

    r = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]
    assert row["id"] == str(fid)
    # The faithfulness demotion HOLDS — the generic critique did not overwrite it.
    assert row["critic_score"] == pytest.approx(0.3, abs=1e-4)
    assert row["effective_confidence"] == pytest.approx(0.3, abs=1e-4)
    # And the faithfulness verification block still surfaces (its detail — the
    # generic critique carries none).
    assert row["verification"] is not None
    assert row["verification"]["faithfulness_score"] == pytest.approx(0.3, abs=1e-4)
    assert row["verification"]["unsupported_spans"][0]["reason"] == "no_citation"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_unverified_has_no_verification_block(
    substrate_app, client: AsyncClient,
):
    """ACCEPTANCE 3: a finding with NO faithfulness critique → verification is
    null (no fabricated block) and effective_confidence == confidence."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("verify-none")
    await _insert_finding(
        pg_store, title="legacy unverified", confidence=0.7, target_id=tid,
    )
    r = await client.get("/api/v1/findings", params={"target_id": tid})
    row = r.json()["data"][0]
    assert row["verification"] is None
    assert row["effective_confidence"] == pytest.approx(0.7, abs=1e-4)


# ---------------------------------------------------------------------------
# P1-T1 — reachability facets: orphan (target_id_null), full-text (q),
# analyst-set (analyst_id_in). The ~1100 NULL-target "orphan" findings are
# unreachable from any country view without these.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_target_id_null_returns_only_orphans(
    substrate_app, client: AsyncClient,
):
    """`target_id_null=true` returns ONLY NULL-target findings. We tag the two
    orphans this test inserts with a unique analyst_id so we can isolate them
    from the shared DB's other NULL-target rows, then assert each has a NULL
    target_id."""
    _, _, pg_store = substrate_app
    analyst = f"orphan_{uuid4().hex[:10]}"
    tid = _unique_target_id("orphan-targeted")
    orphan_a = await _insert_finding(
        pg_store, title="orphan a", target_id=None, analyst_id=analyst,
    )
    orphan_b = await _insert_finding(
        pg_store, title="orphan b", target_id=None, analyst_id=analyst,
    )
    # A targeted finding by the same analyst must NOT appear.
    await _insert_finding(
        pg_store, title="has a target", target_id=tid, analyst_id=analyst,
    )

    r = await client.get(
        "/api/v1/findings",
        params={"target_id_null": "true", "analyst_id": analyst},
    )
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    ids = {row["id"] for row in rows}
    assert ids == {str(orphan_a), str(orphan_b)}
    # Every returned row really is a NULL-target orphan.
    assert all(row["target_id"] is None for row in rows)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_q_matches_title_or_body_keyword(
    substrate_app, client: AsyncClient,
):
    """`q` full-text-matches a keyword in title OR body. Scoped to this test's
    analyst so the shared DB doesn't bleed in."""
    _, _, pg_store = substrate_app
    analyst = f"fts_{uuid4().hex[:10]}"
    in_title = await _insert_finding(
        pg_store, title="Sahel insurgency escalation", body="routine prose",
        target_id=None, analyst_id=analyst,
    )
    in_body = await _insert_finding(
        pg_store, title="routine title", body="A new insurgency cell emerged.",
        target_id=None, analyst_id=analyst,
    )
    # No "insurgency" anywhere → must not match.
    await _insert_finding(
        pg_store, title="energy prices", body="spot demand up 12 %",
        target_id=None, analyst_id=analyst,
    )

    r = await client.get(
        "/api/v1/findings",
        params={"q": "insurgency", "analyst_id": analyst},
    )
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["data"]}
    assert ids == {str(in_title), str(in_body)}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_analyst_id_in_returns_union(
    substrate_app, client: AsyncClient,
):
    """`analyst_id_in` is a CSV of analyst ids; the result is the UNION across
    them. A finding by an analyst NOT in the set is excluded."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("analyst-set")
    a1 = f"a1_{uuid4().hex[:6]}"
    a2 = f"a2_{uuid4().hex[:6]}"
    a3 = f"a3_{uuid4().hex[:6]}"
    f1 = await _insert_finding(pg_store, target_id=tid, analyst_id=a1)
    f2 = await _insert_finding(pg_store, target_id=tid, analyst_id=a2)
    # a3 is NOT in the set → excluded.
    await _insert_finding(pg_store, target_id=tid, analyst_id=a3)

    r = await client.get(
        "/api/v1/findings",
        params={"analyst_id_in": f"{a1},{a2}", "target_id": tid},
    )
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["data"]}
    assert ids == {str(f1), str(f2)}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_q_filter_composes_with_cursor_pagination(
    substrate_app, client: AsyncClient,
):
    """An existing facet (here `q`) + cursor pagination still walk correctly:
    5 matching rows over 2-row pages → (2, 2, 1), newest-first, no dupes."""
    _, _, pg_store = substrate_app
    analyst = f"page_{uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    inserted: list[str] = []
    for i in range(5):
        rid = await _insert_finding(
            pg_store, title=f"drought update {i}", body="severe drought ongoing",
            target_id=None, analyst_id=analyst,
            produced_at=now - timedelta(seconds=i),
        )
        inserted.append(str(rid))
    # A non-matching row by the same analyst must never appear.
    await _insert_finding(
        pg_store, title="unrelated", body="nothing here",
        target_id=None, analyst_id=analyst, produced_at=now - timedelta(seconds=9),
    )

    seen: list[str] = []
    cursor: str | None = None
    page_count = 0
    while True:
        params: dict[str, Any] = {
            "limit": 2, "q": "drought", "analyst_id": analyst,
        }
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/findings", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        page_count += 1
        seen.extend(row["id"] for row in body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert page_count < 10

    assert page_count == 3
    assert seen == inserted  # newest-first, no dupes, no skips


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_reachability_facets_preserve_verification_fold(
    substrate_app, client: AsyncClient,
):
    """A NET-NEW facet (here the orphan filter) must NOT disturb the P0-T3
    verification block + the critic effective_confidence fold: an orphan
    finding with a faithfulness critique still surfaces the demoted confidence
    and the named unsupported span."""
    _, _, pg_store = substrate_app
    analyst = f"orphverify_{uuid4().hex[:8]}"
    fid = await _insert_finding(
        pg_store, title="orphan partly fabricated", confidence=0.85,
        target_id=None, analyst_id=analyst,
    )
    verification = {
        "verification": {
            "faithfulness_score": 0.4,
            "checkable_claims": 3,
            "supported_claims": 1,
            "unsupported_spans": [
                {"text": "Border clash overnight.", "reason": "no_citation", "markers": []},
            ],
            "judge_status": "deterministic",
        }
    }
    await _insert_critique(
        pg_store, analyzed_output_id=fid, overall_score=0.4, data_extra=verification,
    )

    r = await client.get(
        "/api/v1/findings",
        params={"target_id_null": "true", "analyst_id": analyst},
    )
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == str(fid)
    assert row["target_id"] is None
    # Critic fold unchanged: min(0.85, 0.4) = 0.4.
    assert row["critic_score"] == pytest.approx(0.4, abs=1e-4)
    assert row["effective_confidence"] == pytest.approx(0.4, abs=1e-4)
    # Verification block unchanged: names the unsupported span.
    assert row["verification"] is not None
    assert row["verification"]["faithfulness_score"] == pytest.approx(0.4, abs=1e-4)
    assert row["verification"]["unsupported_spans"][0]["reason"] == "no_citation"


# ---------------------------------------------------------------------------
# GLASS-1 — the server-side verification facet (`verified=` / `judge_status=`).
# The Live Feed's verification chips used to sieve the fetched page client-side
# (wrong at any real corpus size); these pin that the filter now runs in the
# route's WHERE, over the SAME surfaced verification block the badge reads, and
# that pagination counts the FILTERED population.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_filter_verified_true_and_false(
    substrate_app, client: AsyncClient,
):
    """`verified=true` returns ONLY rows whose surfaced verification block
    carries a measured faithfulness_score; `verified=false` returns everything
    else — never verified, a faithfulness critique WITHOUT a verification
    block, and a structural-verified row (its block has no faithfulness_score:
    re-computation is not the faithfulness pass)."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("verified-facet")

    verified_fid = await _insert_finding(
        pg_store, title="verified", confidence=0.9, target_id=tid,
    )
    await _insert_critique(
        pg_store, analyzed_output_id=verified_fid, overall_score=0.9,
        data_extra={"verification": {
            "faithfulness_score": 0.9, "judge_status": "llm",
        }},
    )
    unverified_fid = await _insert_finding(
        pg_store, title="never verified", confidence=0.7, target_id=tid,
    )
    # A faithfulness critique whose payload carries NO verification block —
    # the surfaced block is null, so the row reads unverified (honest absence).
    scoreless_fid = await _insert_finding(
        pg_store, title="critiqued without a block", confidence=0.8, target_id=tid,
    )
    await _insert_critique(
        pg_store, analyzed_output_id=scoreless_fid, overall_score=0.8,
    )
    structural_fid = await _insert_finding(
        pg_store, title="structural, recomputation-verified", confidence=1.0,
        severity=None, target_id=tid, analyst_id="geo_convergence_scan",
    )
    await _insert_critique(
        pg_store, analyzed_output_id=structural_fid, overall_score=1.0,
        title="Structural verify (verified)",
        data_extra={"verification": {
            "structural_verify": True, "structural_verified": True,
            "checkable_claims": 1, "supported_claims": 1,
        }},
    )

    r = await client.get(
        "/api/v1/findings", params={"target_id": tid, "verified": "true"},
    )
    assert r.status_code == 200, r.text
    assert {row["id"] for row in r.json()["data"]} == {str(verified_fid)}

    r = await client.get(
        "/api/v1/findings", params={"target_id": tid, "verified": "false"},
    )
    assert r.status_code == 200, r.text
    assert {row["id"] for row in r.json()["data"]} == {
        str(unverified_fid), str(scoreless_fid), str(structural_fid),
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_filter_judge_status_each_value(
    substrate_app, client: AsyncClient,
):
    """`judge_status=` matches each of the three run modes exactly — and the J2
    `unsampled` stratum is a FIRST-CLASS value: one query returns exactly the
    rows the sampling gate skipped. An unverified row (no block, hence no
    judge_status) matches NO value; an unsampled row still counts as VERIFIED
    (the deterministic floor ran — an honest state, never an error)."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("judge-facet")

    by_status: dict[str, UUID] = {}
    for status_val in ("llm", "deterministic", "unsampled"):
        fid = await _insert_finding(
            pg_store, title=f"judged {status_val}", confidence=0.9, target_id=tid,
        )
        await _insert_critique(
            pg_store, analyzed_output_id=fid, overall_score=0.7,
            data_extra={"verification": {
                "faithfulness_score": 0.7, "judge_status": status_val,
            }},
        )
        by_status[status_val] = fid
    # No critique at all → no judge_status → matches no value.
    await _insert_finding(
        pg_store, title="never verified", confidence=0.7, target_id=tid,
    )

    for status_val, fid in by_status.items():
        r = await client.get(
            "/api/v1/findings",
            params={"target_id": tid, "judge_status": status_val},
        )
        assert r.status_code == 200, r.text
        assert {row["id"] for row in r.json()["data"]} == {str(fid)}, status_val

    # The unsampled row IS verified — the floor measured a faithfulness score.
    r = await client.get(
        "/api/v1/findings",
        params={"target_id": tid, "verified": "true", "judge_status": "unsampled"},
    )
    assert r.status_code == 200, r.text
    assert {row["id"] for row in r.json()["data"]} == {str(by_status["unsampled"])}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_judge_status_rejects_unknown_value(client: AsyncClient):
    """The `JudgeStatus` enum is closed — an unknown value is a request error,
    never a silent empty page."""
    r = await client.get("/api/v1/findings", params={"judge_status": "bogus"})
    assert r.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_verified_filter_composes_with_cursor_pagination(
    substrate_app, client: AsyncClient,
):
    """The filtered COUNT drives pagination: 5 verified rows interleaved with 2
    unverified ones, walked at limit=2 under `verified=true`, page as (2, 2, 1)
    over ONLY the verified rows — full pages of the filtered population, not
    fetched-then-discarded ones (the client-side sieve this facet replaces)."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("verified-page")
    now = datetime.now(timezone.utc)
    verified_ids: list[str] = []
    for i in range(7):
        fid = await _insert_finding(
            pg_store, title=f"vf-{i}", confidence=0.9, target_id=tid,
            produced_at=now - timedelta(seconds=i),
        )
        if i in (2, 5):
            continue  # two interleaved unverified rows — must never appear
        await _insert_critique(
            pg_store, analyzed_output_id=fid, overall_score=0.8,
            data_extra={"verification": {
                "faithfulness_score": 0.8, "judge_status": "llm",
            }},
        )
        verified_ids.append(str(fid))

    seen: list[str] = []
    cursor: str | None = None
    page_count = 0
    while True:
        params: dict[str, Any] = {
            "limit": 2, "target_id": tid, "verified": "true",
        }
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/findings", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        page_count += 1
        seen.extend(row["id"] for row in body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert page_count < 10

    assert page_count == 3
    assert seen == verified_ids  # newest-first, no dupes, no unverified leaks


# ---------------------------------------------------------------------------
# Situations
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_situations_empty(substrate_app, client: AsyncClient):
    tid = _unique_target_id("sit-empty")
    r = await client.get("/api/v1/situations", params={"target_id": tid})
    assert r.status_code == 200
    assert r.json() == {"data": [], "next_cursor": None}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_situations_single_row_shape(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    tid = _unique_target_id("sit-shape")
    row_id = await _insert_situation(
        pg_store, name="Drought, NE BR", status_val="escalating",
        target_id=tid,
    )
    r = await client.get("/api/v1/situations", params={"target_id": tid})
    assert r.status_code == 200
    body = r.json()
    assert body["next_cursor"] is None
    assert len(body["data"]) == 1
    row = body["data"][0]
    assert row["id"] == str(row_id)
    assert row["name"] == "Drought, NE BR"
    assert row["status"] == "escalating"
    assert row["target_id"] == tid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_situations_pagination_walks_cursor(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    tid = _unique_target_id("sit-page")
    now = datetime.now(timezone.utc)
    inserted: list[str] = []
    for i in range(5):
        rid = await _insert_situation(
            pg_store,
            name=f"s-{i}",
            target_id=tid,
            produced_at=now - timedelta(seconds=i),
        )
        inserted.append(str(rid))

    seen: list[str] = []
    cursor: str | None = None
    page_count = 0
    while True:
        params: dict[str, Any] = {"limit": 2, "target_id": tid}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/situations", params=params)
        assert r.status_code == 200
        body = r.json()
        page_count += 1
        seen.extend(row["id"] for row in body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert page_count < 10

    assert page_count == 3
    assert seen == inserted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_situations_filter_state_and_target(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    tid_x = _unique_target_id("sit-tX")
    tid_y = _unique_target_id("sit-tY")
    base = datetime.now(timezone.utc)
    keep = await _insert_situation(
        pg_store, status_val="active", target_id=tid_x, produced_at=base,
    )
    await _insert_situation(
        pg_store, status_val="resolved", target_id=tid_x, produced_at=base,
    )
    await _insert_situation(
        pg_store, status_val="active", target_id=tid_y, produced_at=base,
    )

    r = await client.get(
        "/api/v1/situations",
        params={
            "state": "active",
            "target_id": tid_x,
            "since": (base - timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    ids = [row["id"] for row in r.json()["data"]]
    assert ids == [str(keep)]


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signals_empty(substrate_app, client: AsyncClient):
    sid = _unique_source_id("sig-empty")
    r = await client.get("/api/v1/signals", params={"source_id": sid})
    assert r.status_code == 200
    assert r.json() == {"data": [], "next_cursor": None}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signals_single_row_shape(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    src = _unique_source_id("sig-shape")
    row_id = await _insert_signal(
        pg_store, title="Reuters headline", language="en",
        source_id=src,
    )
    r = await client.get("/api/v1/signals", params={"source_id": src})
    assert r.status_code == 200
    body = r.json()
    assert body["next_cursor"] is None
    assert len(body["data"]) == 1
    row = body["data"][0]
    assert row["id"] == str(row_id)
    # Source-first: ``title`` is hydrated from payload->>'title'.
    assert row["title"] == "Reuters headline"
    assert row["language"] == "en"
    # Source-first: signals are target-agnostic; source_id is the origin.
    assert row["source_id"] == src
    assert row["classification_scores"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signals_pagination_walks_cursor(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    src = _unique_source_id("sig-page")
    now = datetime.now(timezone.utc)
    inserted: list[str] = []
    for i in range(6):
        rid = await _insert_signal(
            pg_store, title=f"sig-{i}",
            source_id=src,
            produced_at=now - timedelta(seconds=i),
        )
        inserted.append(str(rid))

    seen: list[str] = []
    cursor: str | None = None
    page_count = 0
    while True:
        params: dict[str, Any] = {"limit": 2, "source_id": src}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/signals", params=params)
        assert r.status_code == 200
        body = r.json()
        page_count += 1
        seen.extend(row["id"] for row in body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert page_count < 10

    assert page_count == 3
    assert seen == inserted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signals_filter_source_and_since(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    src_a = _unique_source_id("sig-sA")
    src_b = _unique_source_id("sig-sB")
    base = datetime.now(timezone.utc)
    keep = await _insert_signal(
        pg_store, source_id=src_a, produced_at=base,
    )
    await _insert_signal(pg_store, source_id=src_b, produced_at=base)
    await _insert_signal(
        pg_store, source_id=src_a, produced_at=base - timedelta(days=2),
    )

    r = await client.get(
        "/api/v1/signals",
        params={
            "source_id": src_a,
            "since": (base - timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["data"]]
    assert ids == [str(keep)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signals_filter_language(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    src = _unique_source_id("sig-lang")
    en_id = await _insert_signal(pg_store, language="en", source_id=src)
    await _insert_signal(pg_store, language="pt", source_id=src)

    r = await client.get(
        "/api/v1/signals",
        params={"language": "en", "source_id": src},
    )
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["data"]]
    assert ids == [str(en_id)]


# ---------------------------------------------------------------------------
# Wave-E — access_class_in (SEAMS #59). Opt-in only: absent is byte-identical
# to today's behaviour, present narrows by the row's own access_class
# (signals) or by a linked signal's access_class (findings, single-hop over
# derived_from). Nothing here proves enforcement — there is none yet.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signals_access_class_in_filters_and_is_byte_identical_absent(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    src = _unique_source_id("sig-access")
    licensed_id = await _insert_signal(pg_store, source_id=src, title="licensed one")
    restricted_id = await _insert_signal(pg_store, source_id=src, title="restricted one")
    async with pg_store.acquire() as conn:
        await conn.execute(
            "UPDATE signals SET access_class = 'licensed_commercial' WHERE id = $1",
            licensed_id,
        )
        await conn.execute(
            "UPDATE signals SET access_class = 'restricted' WHERE id = $1",
            restricted_id,
        )

    # Absent — BOTH rows: the pre-Wave-E behaviour, unchanged.
    r_absent = await client.get("/api/v1/signals", params={"source_id": src})
    assert r_absent.status_code == 200
    ids_absent = {row["id"] for row in r_absent.json()["data"]}
    assert ids_absent == {str(licensed_id), str(restricted_id)}

    # Present — narrows to the requested class only.
    r_filtered = await client.get(
        "/api/v1/signals",
        params={"source_id": src, "access_class_in": "licensed_commercial"},
    )
    assert r_filtered.status_code == 200
    ids_filtered = [row["id"] for row in r_filtered.json()["data"]]
    assert ids_filtered == [str(licensed_id)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signals_access_class_in_unknown_value_400(
    client: AsyncClient,
):
    r = await client.get(
        "/api/v1/signals", params={"access_class_in": "nonsense"},
    )
    assert r.status_code == 400
    assert "nonsense" in r.json()["detail"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_access_class_in_filters_and_is_byte_identical_absent(
    substrate_app, client: AsyncClient,
):
    _, _, pg_store = substrate_app
    tid = _unique_target_id("finding-access")
    src = _unique_source_id("finding-access-sig")
    sig_id = await _insert_signal(pg_store, source_id=src)
    finding_id, other_id = uuid4(), uuid4()
    async with pg_store.acquire() as conn:
        await conn.execute(
            "UPDATE signals SET access_class = 'internal' WHERE id = $1", sig_id,
        )
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'finding', 'derived from internal signal', '', 0.7,
                'medium', '{}'::jsonb, $2, NULL, 'test_analyst', NULL,
                now(), $3::uuid[], 'iglu:legba/finding/jsonschema/1-0-0', NULL
            )
            """,
            finding_id, tid, [sig_id],
        )
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'finding', 'no linked signals', '', 0.7,
                'medium', '{}'::jsonb, $2, NULL, 'test_analyst', NULL,
                now(), '{}'::uuid[], 'iglu:legba/finding/jsonschema/1-0-0', NULL
            )
            """,
            other_id, tid,
        )

    # Absent — both findings: the pre-Wave-E behaviour, unchanged.
    r_absent = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r_absent.status_code == 200
    ids_absent = {row["id"] for row in r_absent.json()["data"]}
    assert ids_absent == {str(finding_id), str(other_id)}

    # Present — only the finding whose derived_from signal matches.
    r_filtered = await client.get(
        "/api/v1/findings",
        params={"target_id": tid, "access_class_in": "internal"},
    )
    assert r_filtered.status_code == 200
    ids_filtered = [row["id"] for row in r_filtered.json()["data"]]
    assert ids_filtered == [str(finding_id)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_access_class_in_unknown_value_400(
    client: AsyncClient,
):
    r = await client.get(
        "/api/v1/findings", params={"access_class_in": "nonsense"},
    )
    assert r.status_code == 400
    assert "nonsense" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Contention read API (Holes-B Wave 5, #101) — hydration shape unit tests.
#
# The end-to-end HTTP coverage needs the 0055 fact_contention sidecar in the
# migrated test DB; these pure-unit tests pin the response SHAPE of the
# `/contention` hydration (group + per-value clusters) without a live DB so the
# UI / consult contract is locked regardless of the integration env.
# ---------------------------------------------------------------------------


def _contention_group_row(**over: Any) -> dict[str, Any]:
    base = {
        "id": uuid4(),
        "subject_key": "country x",
        "predicate_key": "capital",
        "status": "surfaced",
        "surfaced_value": "Alpha",
        "value_count": 2,
        "junk_count": 1,
        "opened_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "resolved_at": datetime(2026, 6, 28, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 28, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


def _contention_value_row(**over: Any) -> dict[str, Any]:
    base = {
        "value_key": "alpha",
        "representative_fact_id": uuid4(),
        "distinct_source_count": 3,
        "source_credibility_sum": 2.4,
        "confidence_max": 0.92,
        "confidence_mean": 0.81,
        "source_types": ["ingestion", "curated"],
        "arbiter_score": 0.77,
        "surfaced_winner": True,
        "is_junk": False,
        "junk_reason": None,
        "latest_asserted_at": datetime(2026, 6, 27, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


def test_hydrate_contention_value_maps_support_columns():
    from legba.data.registry.substrate_reads_api import _hydrate_contention_value

    row = _contention_value_row()
    out = _hydrate_contention_value(row)
    assert out.value_key == "alpha"
    assert out.representative_fact_id == str(row["representative_fact_id"])
    assert out.distinct_source_count == 3
    assert out.source_credibility_sum == pytest.approx(2.4)
    assert out.source_types == ["ingestion", "curated"]
    assert out.arbiter_score == pytest.approx(0.77)
    assert out.surfaced_winner is True
    assert out.is_junk is False
    assert out.junk_reason is None


def test_hydrate_contention_value_handles_junk_and_null_score():
    from legba.data.registry.substrate_reads_api import _hydrate_contention_value

    row = _contention_value_row(
        value_key="berlin", surfaced_winner=False, is_junk=True,
        junk_reason="inverted_relation", arbiter_score=None,
        representative_fact_id=None, source_types=[],
    )
    out = _hydrate_contention_value(row)
    assert out.is_junk is True
    assert out.junk_reason == "inverted_relation"
    assert out.arbiter_score is None
    assert out.representative_fact_id is None
    assert out.source_types == []


def test_hydrate_contention_group_with_values():
    from legba.data.registry.substrate_reads_api import _hydrate_contention

    grow = _contention_group_row()
    winner = _contention_value_row(value_key="alpha", surfaced_winner=True)
    loser = _contention_value_row(
        value_key="beta", surfaced_winner=False, arbiter_score=0.41,
        distinct_source_count=1,
    )
    out = _hydrate_contention(grow, [winner, loser])
    assert out.id == str(grow["id"])
    assert out.subject_key == "country x"
    assert out.predicate_key == "capital"
    assert out.status == "surfaced"
    assert out.surfaced_value == "Alpha"
    assert out.value_count == 2
    assert out.junk_count == 1
    # The per-value support panel data is surfaced in the SQL-supplied order
    # (winner first), with exactly one flagged winner.
    assert [v.value_key for v in out.values] == ["alpha", "beta"]
    assert [v.surfaced_winner for v in out.values] == [True, False]


def test_hydrate_contention_abstained_group_has_null_winner():
    from legba.data.registry.substrate_reads_api import _hydrate_contention

    grow = _contention_group_row(status="contested", surfaced_value=None)
    out = _hydrate_contention(grow, [
        _contention_value_row(value_key="alpha", surfaced_winner=False),
        _contention_value_row(value_key="beta", surfaced_winner=False),
    ])
    assert out.status == "contested"
    assert out.surfaced_value is None
    # No surfaced winner anywhere — an honest "disputed, unresolved".
    assert not any(v.surfaced_winner for v in out.values)


# ---------------------------------------------------------------------------
# `/findings?fields=summary` — the list-view weight (build report
# "findings fields=summary"). Default (no `fields`) is the pre-existing,
# UNTOUCHED code path (`_hydrate_finding` / `FindingsPage`); this section
# proves it stays byte-identical, proves the new `FindingsSummaryPage` shape,
# the 422 on an unknown `fields` value, and the size win the whole feature
# exists for.
# ---------------------------------------------------------------------------


_SUMMARY_ASSEMBLY_DATA: dict[str, Any] = {
    "data": {
        "assembly": {
            "schema": "assembly.v1",
            "regime": "assembly",
            "tier": "world",
            "lead": {"kind": "earned_single", "block_ordinals": [1]},
            "drops": {
                "counts": {
                    "shown": 5,
                    "carried": 3,
                    "shown_not_carried": 2,
                    "candidates": 5,
                    "not_selected": 2,
                    "trimmed": 0,
                    "below_floor": 0,
                    "no_head": 0,
                    "invisible_heads": None,
                },
            },
            # Full block content — deliberately NOT in the summary projection
            # (this is exactly the weight `fields=summary` exists to shed).
            "blocks": [
                {"ordinal": 1, "finding_id": "x", "desk": "d", "spans": []},
            ],
        },
    },
}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_default_omits_fields_stays_full_shape(
    substrate_app, client: AsyncClient,
):
    """GOLDEN — no `fields` param is the pre-existing, UNTOUCHED code path.

    Every field the full `FindingRow` shape has ever carried is still present
    (including the RAW double-nested `data.data.assembly` this test's fixture
    writes), and none of the new summary-only field names leak onto it — the
    two branches share nothing but the WHERE-clause builder and the cursor
    codec.
    """
    _, _, pg_store = substrate_app
    tid = _unique_target_id("summary-golden")
    fid = await _insert_finding(
        pg_store,
        title="World read, 2026-09-06",
        body="Full composed prose the summary weight never carries.",
        confidence=0.81,
        severity="high",
        target_id=tid,
        analyst_id="world_assessor",
        data=_SUMMARY_ASSEMBLY_DATA,
    )
    await _insert_critique(
        pg_store,
        analyzed_output_id=fid,
        overall_score=0.9,
        data_extra={"verification": {"faithfulness_score": 0.9, "score_state": "scored"}},
    )

    r = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]

    # Every pre-existing field, present and correct.
    assert set(row.keys()) == {
        "id", "kind", "title", "body", "confidence", "severity", "data",
        "target_id", "target_version", "analyst_id", "analyst_version",
        "produced_at", "derived_from", "schema_uri", "run_id", "created_at",
        "critic_score", "effective_confidence", "verification",
        "verify_exempt", "below_floor",
    }
    assert row["body"] == "Full composed prose the summary weight never carries."
    assert row["data"] == _SUMMARY_ASSEMBLY_DATA
    assert row["verification"]["faithfulness_score"] == pytest.approx(0.9, abs=1e-4)
    assert row["verification"]["score_state"] == "scored"
    # min(confidence=0.81, critic_score=0.9) — no demotion (already the lower).
    assert row["effective_confidence"] == pytest.approx(0.81, abs=1e-4)
    # No cross-contamination from the summary hydration path.
    for leaked in (
        "assembly_tier", "assembly_regime", "assembly_lead_kind",
        "drops_counts", "verification_faithfulness_score",
        "verification_score_state",
    ):
        assert leaked not in row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_summary_shape(substrate_app, client: AsyncClient):
    """`fields=summary` returns exactly the nine base columns plus the six
    assembly/verification/drops leaves — never `body`, the raw `data` blob,
    `derived_from`, `critic_score`/`effective_confidence`, or the full
    `verification` block (`claim_verdicts`/`unsupported_spans` and friends)."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("summary-shape")
    fid = await _insert_finding(
        pg_store,
        title="World read, 2026-09-06",
        body="Full composed prose the summary weight never carries.",
        confidence=0.81,
        severity="high",
        target_id=tid,
        analyst_id="world_assessor",
        data=_SUMMARY_ASSEMBLY_DATA,
    )
    await _insert_critique(
        pg_store,
        analyzed_output_id=fid,
        overall_score=0.9,
        data_extra={
            "verification": {
                "faithfulness_score": 0.9,
                "score_state": "scored",
                "checkable_claims": 4,
                "supported_claims": 4,
                "unsupported_spans": [],
                "claim_verdicts": [],
            },
        },
    )

    r = await client.get(
        "/api/v1/findings", params={"target_id": tid, "fields": "summary"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "next_cursor"}
    assert body["next_cursor"] is None
    assert len(body["data"]) == 1
    row = body["data"][0]

    assert set(row.keys()) == {
        "id", "analyst_id", "target_id", "kind", "title", "created_at",
        "produced_at", "severity", "confidence",
        "assembly_tier", "assembly_regime", "assembly_lead_kind",
        "drops_counts", "verification_faithfulness_score",
        "verification_score_state",
    }
    assert row["id"] == str(fid)
    assert row["analyst_id"] == "world_assessor"
    assert row["target_id"] == tid
    assert row["kind"] == "finding"
    assert row["title"] == "World read, 2026-09-06"
    assert row["severity"] == "high"
    assert row["confidence"] == pytest.approx(0.81, abs=1e-4)
    assert row["assembly_tier"] == "world"
    assert row["assembly_regime"] == "assembly"
    assert row["assembly_lead_kind"] == "earned_single"
    assert row["drops_counts"] == {
        "shown": 5, "carried": 3, "shown_not_carried": 2, "candidates": 5,
        "not_selected": 2, "trimmed": 0, "below_floor": 0, "no_head": 0,
        "invisible_heads": None,
    }
    assert row["verification_faithfulness_score"] == pytest.approx(0.9, abs=1e-4)
    assert row["verification_score_state"] == "scored"
    datetime.fromisoformat(row["produced_at"])
    datetime.fromisoformat(row["created_at"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_summary_legacy_row_has_null_leaves(
    substrate_app, client: AsyncClient,
):
    """A pre-assembly / unverified row → every leaf is `null`, never
    fabricated — the same honesty rule `_hydrate_finding` follows."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("summary-legacy")
    fid = await _insert_finding(
        pg_store, title="legacy composed read", target_id=tid,
    )

    r = await client.get(
        "/api/v1/findings", params={"target_id": tid, "fields": "summary"},
    )
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]
    assert row["id"] == str(fid)
    assert row["assembly_tier"] is None
    assert row["assembly_regime"] is None
    assert row["assembly_lead_kind"] is None
    assert row["drops_counts"] is None
    assert row["verification_faithfulness_score"] is None
    assert row["verification_score_state"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_summary_faithfulness_object_fallback_not_per_key(
    substrate_app, client: AsyncClient,
):
    """The SQL's `coalesce(c.verification, s.structural_verification)` must be
    OBJECT-level, matching `_hydrate_finding`'s Python fallback — not a
    per-key coalesce. A finding can carry a faithfulness block whose
    `faithfulness_score` is legitimately `null` (Q-1's `unassessable` state)
    while ALSO carrying an (unrelated, contrived-for-this-test) structural
    critique with a real score. A per-key coalesce would leak the structural
    number into a row the faithfulness pass explicitly marked unassessable;
    the object-level form must not.
    """
    _, _, pg_store = substrate_app
    tid = _unique_target_id("summary-unassessable")
    fid = await _insert_finding(pg_store, title="zero-claim critique", target_id=tid)
    await _insert_critique(
        pg_store,
        analyzed_output_id=fid,
        overall_score=1.0,
        data_extra={
            "verification": {"faithfulness_score": None, "score_state": "unassessable"},
        },
    )
    # A structural critique that would leak a wrong score under a per-key
    # coalesce — the finding never routes through the structural verify path
    # in production, but the SQL must not depend on that being true.
    await _insert_critique(
        pg_store,
        analyzed_output_id=fid,
        overall_score=0.77,
        title="Structural verify (score 0.77)",
        data_extra={
            "verification": {"faithfulness_score": 0.77, "score_state": "scored"},
        },
    )

    r = await client.get(
        "/api/v1/findings", params={"target_id": tid, "fields": "summary"},
    )
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]
    assert row["verification_faithfulness_score"] is None
    assert row["verification_score_state"] == "unassessable"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_fields_unknown_value_422(client: AsyncClient):
    """An unknown `fields` value 422s (this route's own spec) — unlike
    `journal_api.py`'s hand-rolled `_validate_fields`, which 400s; the two
    routes deliberately disagree here."""
    r = await client.get("/api/v1/findings", params={"fields": "bogus"})
    assert r.status_code == 422
    r = await client.get("/api/v1/findings", params={"fields": "full"})
    assert r.status_code == 422  # "full" is not a valid value on THIS route — only the default (omitted) is full weight.
    # Both real spellings still work.
    r = await client.get("/api/v1/findings")
    assert r.status_code == 200
    r = await client.get("/api/v1/findings", params={"fields": "summary"})
    assert r.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_summary_pagination_walks_cursor(
    substrate_app, client: AsyncClient,
):
    """Pagination/cursor mechanics are unchanged under `fields=summary` — the
    WHERE-clause builder and the `(produced_at, id)` cursor codec are SHARED
    with the default branch."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("summary-page")
    now = datetime.now(timezone.utc)
    inserted: list[str] = []
    for i in range(7):
        rid = await _insert_finding(
            pg_store, title=f"f-{i}", target_id=tid,
            produced_at=now - timedelta(seconds=i),
        )
        inserted.append(str(rid))

    seen: list[str] = []
    cursor: str | None = None
    page_count = 0
    while True:
        params: dict[str, Any] = {"limit": 3, "target_id": tid, "fields": "summary"}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/findings", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        page_count += 1
        seen.extend(row["id"] for row in body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert page_count < 10

    assert page_count == 3
    assert seen == inserted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_summary_row_under_2kb(substrate_app, client: AsyncClient):
    """The size assertion the build report requires: a summary row for a
    REALISTIC heavy assembly read (many blocks, long quoted spans, a
    claim-verdict ledger) stays under 2 KB, while the SAME row at full weight
    is many times larger."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("summary-size")

    long_span = "A " + ("quoted desk sentence with real prose content. " * 40)
    heavy_data = {
        "data": {
            "assembly": {
                "schema": "assembly.v1",
                "regime": "assembly",
                "tier": "world",
                "lead": {"kind": "co_leads", "block_ordinals": [1, 2]},
                "drops": {
                    "counts": {
                        "shown": 40, "carried": 12, "shown_not_carried": 28,
                        "candidates": 40, "not_selected": 28, "trimmed": 0,
                        "below_floor": 0, "no_head": 0, "invisible_heads": None,
                    },
                },
                "blocks": [
                    {
                        "ordinal": n,
                        "finding_id": f"finding-{n}",
                        "desk": f"desk_{n}",
                        "target_id": f"country_{n}",
                        "target_name": f"Country {n}",
                        "spans": [
                            {"role": "bluf", "text": long_span, "markers": ["[1]"]},
                            {"role": "body", "text": long_span, "markers": ["[2]"]},
                        ],
                        "signals": [
                            {"marker": "[1]", "title": long_span[:200], "url": "https://example.test/a"},
                        ],
                    }
                    for n in range(1, 13)
                ],
            },
        },
    }
    heavy_verification = {
        "verification": {
            "faithfulness_score": 0.87,
            "score_state": "scored",
            "checkable_claims": 24,
            "supported_claims": 21,
            "unsupported_spans": [
                {"text": long_span, "reason": "no_citation", "markers": []}
                for _ in range(6)
            ],
            "claim_verdicts": [
                {"text": long_span, "verdict": "supported", "kind": "fact"}
                for _ in range(24)
            ],
        },
    }
    fid = await _insert_finding(
        pg_store,
        title="Heavy world read",
        body="\n\n".join([long_span] * 20),
        confidence=0.81,
        target_id=tid,
        analyst_id="world_assessor",
        data=heavy_data,
    )
    await _insert_critique(
        pg_store, analyzed_output_id=fid, overall_score=0.87,
        data_extra=heavy_verification,
    )

    r_full = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r_full.status_code == 200, r_full.text
    full_row_bytes = len(json.dumps(r_full.json()["data"][0]).encode("utf-8"))

    r_summary = await client.get(
        "/api/v1/findings", params={"target_id": tid, "fields": "summary"},
    )
    assert r_summary.status_code == 200, r_summary.text
    summary_row_bytes = len(json.dumps(r_summary.json()["data"][0]).encode("utf-8"))

    assert full_row_bytes > 10_000, (
        f"fixture not heavy enough to prove the point: {full_row_bytes} bytes"
    )
    assert summary_row_bytes < 2048, f"summary row is {summary_row_bytes} bytes"


# ---------------------------------------------------------------------------
# `/findings?fields=judgment` (7b-v) — the Morning Read CHECKED band's weight.
# The verify verdict WHOLE (`unsupported_spans` included — the one thing
# `fields=summary` drops and this band exists to read) plus citations reduced
# to `findings_projection.JUDGMENT_FIELDS` — never `data`, `derived_from`, or
# `body`.
# ---------------------------------------------------------------------------


_JUDGMENT_ASSEMBLY_DATA: dict[str, Any] = {
    "data": {
        "assembly": {
            "schema": "assembly.v1",
            "regime": "assembly",
            "tier": "world",
            "lead": {"kind": "earned_single", "block_ordinals": [1]},
            "blocks": [
                {"ordinal": 1, "finding_id": "x", "desk": "d", "spans": []},
            ],
        },
        # Nested storage shape (the live one — provenance.writes double-nests
        # a payload's own `data` field, see `export_api._citation_list`'s
        # docstring). `fields=judgment` must resolve this WITHOUT ever
        # sending the sibling `assembly` key above.
        "citations": [
            {
                "marker": "[[ref:1]]", "ordinal": 1, "ref_id": "finding-x",
                "ref_kind": "finding", "source": "security_desk",
                "source_id": None, "title": "Border incident escalates",
                "produced_at": "2026-09-24T07:00:00Z",
                "evidence_text": "a full quoted paragraph" * 10,
                "single_source": True,
            },
        ],
    },
}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_judgment_shape(substrate_app, client: AsyncClient):
    """`fields=judgment` returns exactly the eleven scalar/verify/citations
    leaves the brief names — never `body`, the raw `data` blob,
    `derived_from`, `critic_score`/`effective_confidence`, or
    `verify_exempt`/`below_floor`."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("judgment-shape")
    fid = await _insert_finding(
        pg_store,
        title="Brazil energy import spike",
        body="Full composed prose the judgment weight never carries.",
        confidence=0.81,
        severity="high",
        target_id=tid,
        analyst_id="world_assessor",
        data=_JUDGMENT_ASSEMBLY_DATA,
    )
    await _insert_critique(
        pg_store,
        analyzed_output_id=fid,
        overall_score=0.9,
        data_extra={
            "verification": {
                "faithfulness_score": 0.62,
                "judge_status": "llm",
                "checkable_claims": 5,
                "supported_claims": 3,
                "unsupported_spans": [
                    {"text": "Troop numbers doubled overnight.", "reason": "no_citation", "markers": []},
                ],
            },
        },
    )

    r = await client.get(
        "/api/v1/findings", params={"target_id": tid, "fields": "judgment"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "next_cursor"}
    assert len(body["data"]) == 1
    row = body["data"][0]

    assert set(row.keys()) == {
        "id", "kind", "title", "analyst_id", "analyst_version",
        "target_id", "target_version", "produced_at", "severity",
        "confidence", "schema_uri", "verification", "citations",
    }
    assert row["id"] == str(fid)
    assert row["kind"] == "finding"
    assert row["title"] == "Brazil energy import spike"
    assert row["analyst_id"] == "world_assessor"
    assert row["target_id"] == tid
    assert row["severity"] == "high"
    assert row["confidence"] == pytest.approx(0.81, abs=1e-4)
    datetime.fromisoformat(row["produced_at"])

    # The verify block WHOLE — `unsupported_spans` included, the exact leaf
    # `fields=summary` drops and this band exists to read.
    assert row["verification"]["faithfulness_score"] == pytest.approx(0.62, abs=1e-4)
    assert row["verification"]["judge_status"] == "llm"
    assert len(row["verification"]["unsupported_spans"]) == 1
    assert row["verification"]["unsupported_spans"][0]["text"] == (
        "Troop numbers doubled overnight."
    )

    # The citation, reduced to the judgment key set only.
    assert len(row["citations"]) == 1
    citation = row["citations"][0]
    assert set(citation.keys()) == {
        "ordinal", "source", "source_id", "produced_at",
        "single_source", "wire_folded", "marker_class",
    }
    assert citation["ordinal"] == 1
    assert citation["source"] == "security_desk"
    assert citation["produced_at"] == "2026-09-24T07:00:00Z"
    assert citation["single_source"] is True
    assert citation["wire_folded"] is False
    for shed in ("evidence_text", "title", "ref_id", "ref_kind", "marker"):
        assert shed not in citation


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_judgment_flat_citations_fallback(
    substrate_app, client: AsyncClient,
):
    """A row whose `data.citations` is FLAT (no nested `data.data` key —
    the pre-`inline_target` double-nesting shape) still resolves — the SQL's
    `coalesce` falls through exactly like `export_api._citation_list`."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("judgment-flat")
    fid = await _insert_finding(
        pg_store,
        title="flat-shape citations",
        target_id=tid,
        data={"citations": [{"ordinal": 5, "source": "flat_source"}]},
    )
    r = await client.get(
        "/api/v1/findings", params={"target_id": tid, "fields": "judgment"},
    )
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]
    assert row["id"] == str(fid)
    assert row["citations"] == [{
        "ordinal": 5, "source": "flat_source", "source_id": None,
        "produced_at": None, "single_source": False, "wire_folded": False,
        "marker_class": None,
    }]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_judgment_legacy_row_has_null_verification_empty_citations(
    substrate_app, client: AsyncClient,
):
    """A pre-assembly / unverified / uncited row → `verification` is `null`
    and `citations` is `[]` — the same honesty rule every other weight on
    this route follows."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("judgment-legacy")
    fid = await _insert_finding(pg_store, title="legacy composed read", target_id=tid)

    r = await client.get(
        "/api/v1/findings", params={"target_id": tid, "fields": "judgment"},
    )
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]
    assert row["id"] == str(fid)
    assert row["verification"] is None
    assert row["citations"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_judgment_faithfulness_object_fallback_not_per_key(
    substrate_app, client: AsyncClient,
):
    """Same OBJECT-level `coalesce(faithfulness, structural)` fallback the
    other weights use — a finding with a legitimately `unassessable`
    faithfulness block never leaks an unrelated structural score."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("judgment-unassessable")
    fid = await _insert_finding(pg_store, title="zero-claim critique", target_id=tid)
    await _insert_critique(
        pg_store, analyzed_output_id=fid, overall_score=1.0,
        data_extra={"verification": {"faithfulness_score": None, "score_state": "unassessable"}},
    )
    await _insert_critique(
        pg_store, analyzed_output_id=fid, overall_score=0.77,
        title="Structural verify (score 0.77)",
        data_extra={"verification": {"faithfulness_score": 0.77, "score_state": "scored"}},
    )

    r = await client.get(
        "/api/v1/findings", params={"target_id": tid, "fields": "judgment"},
    )
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]
    assert row["verification"]["faithfulness_score"] is None
    assert row["verification"]["score_state"] == "unassessable"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_judgment_unknown_value_still_422s(client: AsyncClient):
    """`fields=judgment` is a real spelling alongside `summary`; an unknown
    value still 422s — the route's 422-not-400 spec is unaffected."""
    r = await client.get("/api/v1/findings", params={"fields": "judgment"})
    assert r.status_code == 200
    r = await client.get("/api/v1/findings", params={"fields": "bogus"})
    assert r.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_judgment_pagination_walks_cursor(
    substrate_app, client: AsyncClient,
):
    """Pagination/cursor mechanics are unchanged under `fields=judgment` — the
    WHERE-clause builder and the `(produced_at, id)` cursor codec are SHARED
    with the default and `fields=summary` branches."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("judgment-page")
    now = datetime.now(timezone.utc)
    inserted: list[str] = []
    for i in range(7):
        rid = await _insert_finding(
            pg_store, title=f"f-{i}", target_id=tid,
            produced_at=now - timedelta(seconds=i),
        )
        inserted.append(str(rid))

    seen: list[str] = []
    cursor: str | None = None
    page_count = 0
    while True:
        params: dict[str, Any] = {"target_id": tid, "limit": 3, "fields": "judgment"}
        if cursor is not None:
            params["cursor"] = cursor
        r = await client.get("/api/v1/findings", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        seen.extend(row["id"] for row in body["data"])
        page_count += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert page_count < 10, "cursor never terminated"
    assert page_count == 3
    assert seen == inserted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_findings_judgment_row_far_smaller_than_default(
    substrate_app, client: AsyncClient,
):
    """The size assertion the build report requires: a judgment row for a
    REALISTIC heavy assembly read (many blocks, long quoted spans, a
    citations array) stays a small fraction of the SAME row at default
    weight — the byte win `fields=judgment` exists for."""
    _, _, pg_store = substrate_app
    tid = _unique_target_id("judgment-size")

    long_span = "A " + ("quoted desk sentence with real prose content. " * 40)
    heavy_data = {
        "data": {
            "assembly": {
                "schema": "assembly.v1", "regime": "assembly", "tier": "world",
                "lead": {"kind": "co_leads", "block_ordinals": [1, 2]},
                "blocks": [
                    {
                        "ordinal": n, "finding_id": f"finding-{n}",
                        "desk": f"desk_{n}", "target_id": f"country_{n}",
                        "target_name": f"Country {n}",
                        "spans": [
                            {"role": "bluf", "text": long_span, "markers": ["[1]"]},
                            {"role": "body", "text": long_span, "markers": ["[2]"]},
                        ],
                        "signals": [
                            {"marker": "[1]", "title": long_span[:200], "url": "https://example.test/a"},
                        ],
                    }
                    for n in range(1, 13)
                ],
            },
            "citations": [
                {
                    "marker": f"[[ref:{n}]]", "ordinal": n, "ref_id": f"finding-{n}",
                    "ref_kind": "finding", "source": f"desk_{n}",
                    "title": f"Country {n} read", "produced_at": "2026-09-24T07:00:00Z",
                    "evidence_text": long_span, "single_source": bool(n % 2),
                }
                for n in range(1, 13)
            ],
        },
    }
    heavy_verification = {
        "verification": {
            "faithfulness_score": 0.87, "score_state": "scored",
            "checkable_claims": 24, "supported_claims": 21,
            "unsupported_spans": [
                {"text": long_span, "reason": "no_citation", "markers": []}
                for _ in range(6)
            ],
            "claim_verdicts": [
                {"text": long_span, "verdict": "supported", "kind": "fact"}
                for _ in range(24)
            ],
        },
    }
    fid = await _insert_finding(
        pg_store,
        title="Heavy world read",
        body="\n\n".join([long_span] * 20),
        confidence=0.81,
        target_id=tid,
        analyst_id="world_assessor",
        data=heavy_data,
    )
    await _insert_critique(
        pg_store, analyzed_output_id=fid, overall_score=0.87,
        data_extra=heavy_verification,
    )

    r_full = await client.get("/api/v1/findings", params={"target_id": tid})
    assert r_full.status_code == 200, r_full.text
    full_row_bytes = len(json.dumps(r_full.json()["data"][0]).encode("utf-8"))

    r_judgment = await client.get(
        "/api/v1/findings", params={"target_id": tid, "fields": "judgment"},
    )
    assert r_judgment.status_code == 200, r_judgment.text
    judgment_row_bytes = len(json.dumps(r_judgment.json()["data"][0]).encode("utf-8"))
    judgment_row = r_judgment.json()["data"][0]

    # The verify block (with its 6 unsupported spans + 24 claim verdicts) DOES
    # carry through whole — the judgment weight is not "small at any cost",
    # it sheds `data`/`derived_from`/`body`, never the verdict this band reads.
    assert len(judgment_row["verification"]["unsupported_spans"]) == 6
    assert len(judgment_row["citations"]) == 12

    assert full_row_bytes > 10_000, (
        f"fixture not heavy enough to prove the point: {full_row_bytes} bytes"
    )
    assert judgment_row_bytes < full_row_bytes * 0.5, (
        f"judgment row ({judgment_row_bytes}B) is not meaningfully smaller "
        f"than the full row ({full_row_bytes}B)"
    )
    print(
        f"\n[fields=judgment size] full={full_row_bytes}B "
        f"judgment={judgment_row_bytes}B "
        f"ratio={judgment_row_bytes / full_row_bytes:.2f}",
    )
