# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the Journal read surface (`/api/v1/journal`,
`/api/v1/journal/{id}`) — Voices panel step 1 (planning/VOICES_PANEL_SPEC.md
§3): the repeatable `kind` filter, the `fields=summary|full` weight mode, and
the §3.4 verify-score/verify-body critique join.

Runs against the live substrate via the `migrated_pg` fixture from
`conftest.py`, mirroring `test_substrate_reads_api.py`'s pattern exactly (real
FastAPI app + real `DescriptorRegistry`, no mocks for substrate boundaries).

Each test scopes its rows by inserting into a fresh `title`/`analyst_id` tag
(journal_entries has no per-test scoping column like `target_id`) and reading
back only the ids it inserted, so sibling tests sharing the migrated DB never
bleed into each other's assertions.
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

from legba.data.config import PostgresConfig
from legba.data.nats import NatsStore
from legba.data.config import NatsConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.journal_api import build_journal_router
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache


# Mandatory env for vault + signing identity (mirrors test_substrate_reads_api).
_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "44" * 32)


def _fixed_identity() -> SigningIdentity:
    seed = b"journal-api-test-fixed-seed-abcd"
    assert len(seed) == 32
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:journal-api-test",
    )


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def journal_app(migrated_pg: PostgresConfig):
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
    app.include_router(build_journal_router(deps), prefix="/api/v1")

    yield app, deps, pg_store

    await descriptor_registry.stop()
    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(journal_app):
    app, _, _ = journal_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Insertion helpers.
# ---------------------------------------------------------------------------


def _tag(label: str) -> str:
    """A unique title-prefix tag so a test scopes reads to only its own rows
    (journal_entries has no target_id-like column to filter on server-side —
    the tests instead over-fetch with a generous limit and filter client-side
    by id-membership, exactly as this helper's callers do)."""
    return f"japi-{label}-{uuid4().hex[:10]}"


async def _insert_entry(
    pg_store: PostgresStore,
    *,
    entry_kind: str = "entry",
    title: str = "e",
    body: str = "body.",
    claims: list[dict] | None = None,
    honesty_flags: list[str] | None = None,
    period_end: datetime | None = None,
    produced_at: datetime | None = None,
    valid_until: datetime | None = None,
    superseded_by: UUID | None = None,
    analyst_id: str | None = "journal_assessor",
) -> UUID:
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    pend = period_end or ts
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO journal_entries (
                id, entry_kind, title, body, claims, cited_substrate_refs,
                honesty_flags, period_start, period_end, produced_at,
                analyst_id, analyst_version, valid_until, superseded_by
            ) VALUES (
                $1, $2, $3, $4, $5::jsonb, '{}'::uuid[],
                $6::text[], $7, $7, $8,
                $9, 'v1', $10, $11
            )
            """,
            row_id, entry_kind, title, body, json.dumps(claims or []),
            honesty_flags or [], pend, ts, analyst_id, valid_until, superseded_by,
        )
    return row_id


async def _insert_journal_critique(
    pg_store: PostgresStore,
    *,
    analyzed_output_id: UUID,
    overall_score: float,
    produced_at: datetime | None = None,
    title: str | None = None,
    body_lines: list[str] | None = None,
) -> UUID:
    """Insert a 'Faithfulness verify' critique row graded against a journal
    entry (§3.4) — mirrors `_insert_critique` in test_substrate_reads_api.py,
    including the same title-pin convention (S8-T2): the title defaults to the
    faithfulness-verify form the join pins on (`title LIKE 'Faithfulness
    verify%'`); pass a generic `title` to insert a row the join deliberately
    ignores."""
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    row_title = title if title is not None else f"Faithfulness verify (score {overall_score:.2f})"
    body = "\n".join(
        body_lines
        or [
            f"Faithfulness verify of finding {analyzed_output_id}",
            f"  faithfulness_score={overall_score:.2f}",
        ]
    )
    data = {
        "analyzed_output_id": str(analyzed_output_id),
        "overall_score": overall_score,
    }
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'critique', $2, $3, $4, NULL, $5::jsonb,
                NULL, NULL, 'verify', NULL,
                $6, $7, 'iglu:legba/critique/jsonschema/1-0-0', NULL
            )
            """,
            row_id, row_title, body, overall_score, json.dumps(data),
            ts, [analyzed_output_id],
        )
    return row_id


# ---------------------------------------------------------------------------
# §3.1 — `kind` filter.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kind_omitted_defaults_to_all_append_tiers(
    journal_app, client: AsyncClient,
):
    """LV-2 tail (2026-07-23): the default stream carries EVERY append tier —
    entry, chronicle, lens, lens_diff — so faculty entries surface in the
    Voices panel without an explicit filter. Consolidation stays slot-only."""
    _, _, pg_store = journal_app
    tag = _tag("default")
    e_id = await _insert_entry(pg_store, entry_kind="entry", title=tag)
    c_id = await _insert_entry(pg_store, entry_kind="chronicle", title=tag)
    l_id = await _insert_entry(pg_store, entry_kind="lens", title=tag)
    d_id = await _insert_entry(pg_store, entry_kind="lens_diff", title=tag)

    r = await client.get("/api/v1/journal", params={"limit": 200})
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["entries"]}
    assert str(e_id) in ids
    assert str(c_id) in ids
    assert str(l_id) in ids
    assert str(d_id) in ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kind_filters_to_requested_kinds_only(
    journal_app, client: AsyncClient,
):
    _, _, pg_store = journal_app
    tag = _tag("filter")
    e_id = await _insert_entry(pg_store, entry_kind="entry", title=tag)
    c_id = await _insert_entry(pg_store, entry_kind="chronicle", title=tag)

    r = await client.get(
        "/api/v1/journal", params={"limit": 200, "kind": "chronicle"},
    )
    assert r.status_code == 200, r.text
    rows = r.json()["entries"]
    ids = {row["id"] for row in rows}
    assert str(c_id) in ids
    assert str(e_id) not in ids
    assert all(row["entry_kind"] == "chronicle" for row in rows)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kind_repeatable_param_unions(journal_app, client: AsyncClient):
    """`?kind=entry&kind=chronicle` — repeatable over CSV, union (OR) across
    the requested kinds."""
    _, _, pg_store = journal_app
    tag = _tag("union")
    e_id = await _insert_entry(pg_store, entry_kind="entry", title=tag)
    c_id = await _insert_entry(pg_store, entry_kind="chronicle", title=tag)

    r = await client.get(
        "/api/v1/journal",
        params=[("limit", "200"), ("kind", "entry"), ("kind", "chronicle")],
    )
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["entries"]}
    assert str(e_id) in ids and str(c_id) in ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kind_invalid_value_400s(client: AsyncClient):
    """A typo 400s rather than silently returning zero rows."""
    r = await client.get("/api/v1/journal", params={"kind": "jrnal"})
    assert r.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kind_excluding_consolidation_hides_the_slot(
    journal_app, client: AsyncClient,
):
    """Filtering to "just chronicles" also hides the pinned consolidation slot
    (§3.1) — the slot is fetched only when 'consolidation' is itself in the
    requested kind set."""
    _, _, pg_store = journal_app
    tag = _tag("hide-consol")
    await _insert_entry(pg_store, entry_kind="chronicle", title=tag)

    r = await client.get(
        "/api/v1/journal", params={"limit": 200, "kind": "chronicle"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["consolidation"] is None


# ---------------------------------------------------------------------------
# §3.3 — `fields=summary|full`.
# ---------------------------------------------------------------------------

_SUMMARY_KEYS = {
    "id", "entry_kind", "title", "honesty_flags", "period_start",
    "period_end", "produced_at", "analyst_id", "analyst_version",
    "verify_score",
}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fields_default_is_full(journal_app, client: AsyncClient):
    """Omitting `fields` behaves exactly like `fields=full` (backward
    compatible — today's shape, unchanged)."""
    _, _, pg_store = journal_app
    tag = _tag("full-default")
    e_id = await _insert_entry(
        pg_store, title=tag, body="the narrative body.",
        claims=[{"text_span": "a cited fact.", "kind": "fact", "refs": []}],
    )

    r = await client.get("/api/v1/journal", params={"limit": 200})
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["entries"] if x["id"] == str(e_id))
    assert row["body"] == "the narrative body."
    assert len(row["claims"]) == 1
    assert row["claims"][0]["text_span"] == "a cited fact."


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fields_summary_drops_body_and_claims(
    journal_app, client: AsyncClient,
):
    _, _, pg_store = journal_app
    tag = _tag("summary")
    e_id = await _insert_entry(
        pg_store, title=tag, body="should not appear.",
        claims=[{"text_span": "should not appear either.", "kind": "fact", "refs": []}],
    )

    r = await client.get(
        "/api/v1/journal", params={"limit": 200, "fields": "summary"},
    )
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["entries"] if x["id"] == str(e_id))
    assert set(row.keys()) == _SUMMARY_KEYS
    assert "body" not in row
    assert "claims" not in row
    assert "cited_substrate_refs" not in row
    assert "verify_body" not in row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fields_invalid_value_400s(client: AsyncClient):
    r = await client.get("/api/v1/journal", params={"fields": "compact"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# §3.4 — verify_score / verify_body (the critique join).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_score_null_when_no_critique(
    journal_app, client: AsyncClient,
):
    """§3.4's honest-absence convention — no fabricated score when no
    'Faithfulness verify' critique exists for the entry."""
    _, _, pg_store = journal_app
    tag = _tag("no-verify")
    e_id = await _insert_entry(pg_store, title=tag)

    r = await client.get("/api/v1/journal", params={"limit": 200})
    row = next(x for x in r.json()["entries"] if x["id"] == str(e_id))
    assert row["verify_score"] is None
    assert row["verify_body"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_score_and_body_surface_from_critique(
    journal_app, client: AsyncClient,
):
    """The critique's overall_score → verify_score; its body (which lists
    each contested span as `  - [judge_contradicted] ...` etc.) →
    verify_body, at fields=full."""
    _, _, pg_store = journal_app
    tag = _tag("verified")
    e_id = await _insert_entry(pg_store, title=tag)
    await _insert_journal_critique(
        pg_store,
        analyzed_output_id=e_id,
        overall_score=0.42,
        body_lines=[
            f"Faithfulness verify of finding {e_id}",
            "  faithfulness_score=0.42",
            "  checkable_claims=2 supported=1 unsupported=1",
            "  judge=llm",
            "  - [judge_contradicted] A contested span.",
        ],
    )

    r = await client.get("/api/v1/journal", params={"limit": 200})
    row = next(x for x in r.json()["entries"] if x["id"] == str(e_id))
    assert row["verify_score"] == pytest.approx(0.42, abs=1e-4)
    assert "[judge_contradicted] A contested span." in row["verify_body"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_score_present_in_summary_mode_too(
    journal_app, client: AsyncClient,
):
    """The score pill (§2a) needs verify_score in the cheap list read too —
    only verify_body (full-only) is summary-skipped."""
    _, _, pg_store = journal_app
    tag = _tag("summary-verify")
    e_id = await _insert_entry(pg_store, title=tag)
    await _insert_journal_critique(pg_store, analyzed_output_id=e_id, overall_score=0.77)

    r = await client.get(
        "/api/v1/journal", params={"limit": 200, "fields": "summary"},
    )
    row = next(x for x in r.json()["entries"] if x["id"] == str(e_id))
    assert row["verify_score"] == pytest.approx(0.77, abs=1e-4)
    assert "verify_body" not in row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_score_latest_critique_wins(
    journal_app, client: AsyncClient,
):
    _, _, pg_store = journal_app
    tag = _tag("latest-verify")
    e_id = await _insert_entry(pg_store, title=tag)
    older = datetime.now(timezone.utc) - timedelta(hours=2)
    await _insert_journal_critique(
        pg_store, analyzed_output_id=e_id, overall_score=0.2, produced_at=older,
    )
    await _insert_journal_critique(pg_store, analyzed_output_id=e_id, overall_score=0.65)

    r = await client.get("/api/v1/journal", params={"limit": 200})
    row = next(x for x in r.json()["entries"] if x["id"] == str(e_id))
    assert row["verify_score"] == pytest.approx(0.65, abs=1e-4)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_generic_critique_does_not_overwrite_faithfulness_pin(
    journal_app, client: AsyncClient,
):
    """Mirrors S8-T2's finding-side regression guard: a later GENERIC
    critique (title not matching 'Faithfulness verify%') must not win the
    produced_at race and mask the faithfulness verdict."""
    _, _, pg_store = journal_app
    tag = _tag("pin")
    e_id = await _insert_entry(pg_store, title=tag)
    older = datetime.now(timezone.utc) - timedelta(hours=1)
    await _insert_journal_critique(
        pg_store, analyzed_output_id=e_id, overall_score=0.3, produced_at=older,
    )
    await _insert_journal_critique(
        pg_store, analyzed_output_id=e_id, overall_score=0.95, title="critique",
    )

    r = await client.get("/api/v1/journal", params={"limit": 200})
    row = next(x for x in r.json()["entries"] if x["id"] == str(e_id))
    assert row["verify_score"] == pytest.approx(0.3, abs=1e-4)


# ---------------------------------------------------------------------------
# `GET /journal/{id}` — the reader-pane single-row fetch.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_single_entry_returns_full_shape(
    journal_app, client: AsyncClient,
):
    _, _, pg_store = journal_app
    tag = _tag("single")
    e_id = await _insert_entry(
        pg_store, title=tag, body="reader pane body.",
        claims=[{"text_span": "a claim.", "kind": "fact", "refs": []}],
    )
    await _insert_journal_critique(pg_store, analyzed_output_id=e_id, overall_score=0.55)

    r = await client.get(f"/api/v1/journal/{e_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(e_id)
    assert body["title"] == tag
    assert body["body"] == "reader pane body."
    assert len(body["claims"]) == 1
    assert body["verify_score"] == pytest.approx(0.55, abs=1e-4)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_single_entry_missing_id_404s(client: AsyncClient):
    r = await client.get(f"/api/v1/journal/{uuid4()}")
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_single_entry_malformed_id_422s(client: AsyncClient):
    """A non-UUID path segment fails FastAPI's own `UUID` path-param coercion
    (a 422, before the handler even runs) — not a 500."""
    r = await client.get("/api/v1/journal/not-a-uuid")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Dev-mode auth posture (documents the default test posture, mirrors
# test_substrate_reads_api.py's equivalent).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_journal_dev_mode_accepts_anonymous(client: AsyncClient):
    r = await client.get("/api/v1/journal")
    assert r.status_code == 200
