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
            json.dumps({}), target_id, analyst_id,
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
    """
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
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
                $1, 'critique', 'critique', '', $2, NULL, $3::jsonb,
                NULL, NULL, 'country_critic', NULL,
                $4, $5, 'iglu:legba/critique/jsonschema/1-0-0', NULL
            )
            """,
            row_id, overall_score, json.dumps(data),
            ts, [analyzed_output_id],
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
    # produced_at is ISO-8601.
    datetime.fromisoformat(row["produced_at"])


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
