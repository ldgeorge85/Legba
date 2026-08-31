# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the A10 collection export surface (``POST /api/v1/v3/export``).

Two layers, mirroring the module's own split:

  * PURE composition — ``build_document`` + ``render_markdown`` are DB-free,
    so the markdown document shape is pinned by a GOLDEN test (one fixture →
    exact expected markdown). A composition change must consciously update the
    golden.
  * INTEGRATION — the route against the live substrate via the ``migrated_pg``
    fixture (same app-fixture pattern as ``test_journal_api.py``): mixed
    finding + journal composition in basket order, citation resolution to the
    LIVE signal's title/canonical_url (with the honest fallback for a pruned
    signal), verify-state rendering (faithfulness / explicit unverified),
    effective-confidence folding, the missing-id honesty section, and the
    50-item cap's honest 413.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.alerts.sinks import ENV_PUBLIC_BASE_URL
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.export_api import (
    EXPORT_MAX_ITEMS,
    JOURNAL_TIER_LABELS,
    JOURNAL_VOICE_NOTE,
    PROVENANCE_NOTE,
    build_document,
    build_export_router,
    render_markdown,
)
from legba.data.analysts import unit_grounding as ug
from legba.data.provenance.models import FindingPayload
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache


_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "44" * 32)


def _fixed_identity() -> SigningIdentity:
    seed = b"export-api-test-fixed-seed-abcde"[:32]
    assert len(seed) == 32
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:export-api-test",
    )


@pytest.fixture(autouse=True)
def _no_ambient_public_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test here from the host's REAL ``.env``.

    ``legba.data.config._load_env`` falls back to the fixed path
    ``/usr/local/deployments/active/legba/.env`` when no repo-relative
    ``.env`` exists — on a shared box that IS the live deployment's env
    file, so a plain test run inherits the deployment's real
    ``LEGBA_PUBLIC_BASE_URL`` value. That turns every
    ``receipt_link()`` call absolute (see ``legba/data/alerts/sinks.py``),
    silently breaking the relative-path assertions these export tests pin
    (the receipt line is meant to be tested unset-base-url, not against
    whatever happens to be ambient). Clear it here so the suite is
    deterministic on every box; a test that wants the absolute-URL
    behaviour opts back in explicitly via ``monkeypatch.setenv``.
    """
    monkeypatch.delenv(ENV_PUBLIC_BASE_URL, raising=False)


# ---------------------------------------------------------------------------
# PURE — the golden markdown shape.
# ---------------------------------------------------------------------------


def _golden_items() -> list[dict]:
    return [
        {
            "kind": "finding",
            "id": "11111111-1111-1111-1111-111111111111",
            "row_kind": "finding",
            "title": "Coup risk rising in Ruritania",
            "analyst_id": "country_assessor",
            "analyst_version": "v3",
            "target_id": "country_g20_RR",
            "severity": "high",
            "produced_at": "2026-07-20T06:00:00+00:00",
            "superseded": False,
            "body": "Army units moved toward the capital overnight [1].",
            "citations": [
                {
                    "marker": "[1]",
                    "signal_id": "aaaa",
                    "title": "Troop columns filmed on Route 9",
                    "canonical_url": "https://example.org/route9",
                    "resolved": True,
                    # P2-1: our archived copy of the original bytes exists —
                    # the citation line states it + the verifiable hash.
                    "archived": True,
                    "archive_sha256": "d0" * 32,
                },
                {
                    "marker": "[2]",
                    "signal_id": "bbbb",
                    "title": "Official denial",
                    "canonical_url": "https://state.example/deny",
                    "resolved": False,
                },
                # 2026-08-30 — the kinds the signal_id filter used to strip.
                # A prior read HAS a drill target (an analyst_outputs uuid);
                # the four synthetic blocks have none by design, so none is
                # named for them rather than one being minted.
                {
                    "marker": "[3]",
                    "citation_kind": "prior_read",
                    "resolves_against": "data.citations",
                    "marker_class": "desk_grounding",
                    "signal_id": None,
                    "ref_id": "dddd",
                    "title": "Ruritania — morning read, 18 July",
                    "canonical_url": None,
                    "resolved": True,
                    "resolution_source": "in_row",
                    "archived": False,
                    "archive_sha256": None,
                },
                {
                    "marker": "[4]",
                    "citation_kind": "window_ledger",
                    "resolves_against": "data.citations",
                    "marker_class": "desk_grounding",
                    "signal_id": None,
                    "ref_id": None,
                    "title": "Window ledger (this unit's trailing 14-day record)",
                    "canonical_url": None,
                    "resolved": True,
                    "resolution_source": "in_row",
                    "archived": False,
                    "archive_sha256": None,
                },
                {
                    "marker": "[[ref:5]]",
                    "citation_kind": "finding",
                    "resolves_against": "analyst_outputs",
                    "marker_class": None,
                    "signal_id": None,
                    "ref_id": "eeee",
                    "title": "Ruritania unit read — 20 July",
                    "canonical_url": None,
                    "resolved": True,
                    "resolution_source": "in_row",
                    "archived": False,
                    "archive_sha256": None,
                },
            ],
            "verify_state": "faithfulness=0.67",
            "verify_flags": {"hard_fail": 1, "soft_fail": 1},
            "confidence": 0.72,
            "effective_confidence": 0.67,
            "receipt_path": "/api/v1/lineage/finding/11111111-1111-1111-1111-111111111111",
            "receipt_url": None,
        },
        {
            "kind": "journal_entry",
            "id": "22222222-2222-2222-2222-222222222222",
            "tier": "lens",
            "tier_label": JOURNAL_TIER_LABELS["lens"],
            "voice_note": JOURNAL_VOICE_NOTE,
            "title": "Trend lens — the week the wires went quiet",
            "analyst_id": "lens_trend",
            "analyst_version": "v1",
            "period_start": "2026-07-13T00:00:00+00:00",
            "period_end": "2026-07-20T00:00:00+00:00",
            "produced_at": "2026-07-20T02:00:00+00:00",
            "honesty_flags": ["forecast_unproven"],
            "body": "Signal volume fell for a third week.",
            "claims": [
                {
                    "text_span": "Signal volume fell for a third week",
                    "kind": "fact",
                    "refs": [
                        {"id": "cccc", "kind": "finding", "title": "Weekly volume digest"}
                    ],
                }
            ],
            "cited_substrate_refs": [],
            "verify_state": "faithfulness=0.91",
        },
        {
            "kind": "finding",
            "id": "33333333-3333-3333-3333-333333333333",
            "error": "not found in substrate",
        },
    ]


_GOLDEN_MD = """# Weekly board pack

> machine-generated export; every claim carries its citations; verify states as recorded

- generated_at: 2026-07-24T12:00:00+00:00
- items: 3 (1 not found)

---

## 1. Coup risk rising in Ruritania

- kind: `finding`
- analyst: country_assessor `v3`
- target: country_g20_RR
- produced_at: 2026-07-20T06:00:00+00:00
- severity: high
- confidence: 0.72 (effective 0.67 after verify fold)
- verify: faithfulness=0.67 · flags: 1 hard_fail, 1 soft_fail
- receipt: /api/v1/lineage/finding/11111111-1111-1111-1111-111111111111

Army units moved toward the capital overnight [1].

### Citations

- [1] Troop columns filmed on Route 9 — https://example.org/route9 — evidence preserved, sha256:d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0
- [2] Official denial — https://state.example/deny *(signal no longer in substrate; stored citation shown)*
- [3] Ruritania — morning read, 18 July — *desk grounding · prior read dddd; resolves against data.citations*
- [4] Window ledger (this unit's trailing 14-day record) — *desk grounding · window ledger; resolves against data.citations*
- [[ref:5]] Ruritania unit read — 20 July — *sub-claim finding eeee; resolves against analyst_outputs*

---

## 2. Trend lens — the week the wires went quiet

- kind: journal entry · tier: lens (weekly faculty lens)
- voice: reflective journal voice — off the fact/finding/nexus chain: an always-empty derived_from, excluded from the lineage catalog; citations live only in the row's claims / cited_substrate_refs, an up-only reference, not a lineage edge
- analyst: lens_trend `v1`
- period: 2026-07-13T00:00:00+00:00 → 2026-07-20T00:00:00+00:00
- produced_at: 2026-07-20T02:00:00+00:00
- verify: faithfulness=0.91
- honesty flags: forecast_unproven

Signal volume fell for a third week.

### Claims & cited refs

- [fact] "Signal volume fell for a third week" → finding: Weekly volume digest

---

## 3. (finding 33333333-3333-3333-3333-333333333333)

**not found in substrate** — the basket referenced a row this substrate does not hold (superseded/pruned or a different environment).
"""


def test_render_markdown_golden():
    """The GOLDEN markdown for one mixed fixture (finding + journal + missing).
    A composition change must consciously update this expected text."""
    doc = build_document(
        title="Weekly board pack",
        generated_at=datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc),
        items=_golden_items(),
    )
    assert render_markdown(doc) == _GOLDEN_MD


def test_build_document_header_counts_and_note():
    doc = build_document(
        title=None,
        generated_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        items=_golden_items(),
    )
    assert doc["title"] == "Legba export"  # empty title falls back
    assert doc["item_count"] == 3
    assert doc["missing_count"] == 1
    assert doc["provenance_note"] == PROVENANCE_NOTE
    # Basket order is preserved verbatim.
    assert [i["kind"] for i in doc["items"]] == [
        "finding", "journal_entry", "finding",
    ]


def test_export_cap_constant():
    assert EXPORT_MAX_ITEMS == 50


# ---------------------------------------------------------------------------
# App fixture (mirrors test_journal_api.py).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def export_app(migrated_pg: PostgresConfig):
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
    app.include_router(build_export_router(deps), prefix="/api/v1/v3")

    yield app, deps, pg_store

    await descriptor_registry.stop()
    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(export_app):
    app, _, _ = export_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Insertion helpers (direct SQL, mirroring test_substrate_reads_api /
# test_journal_api).
# ---------------------------------------------------------------------------


async def _insert_signal(
    pg_store: PostgresStore, *, title: str, canonical_url: str | None,
) -> UUID:
    row_id = uuid4()
    ts = datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals (
                id, source_id, source_version, produced_by_kind,
                fetched_at, modality, payload, language, geo,
                canonical_url, content_hash, derived_from, schema_uri
            ) VALUES (
                $1, $2, '', 'source',
                $3, 'text', $4::jsonb, 'en', '{}'::text[],
                $5, '', '{}'::uuid[], 'iglu:legba/signal/jsonschema/3-0-0'
            )
            """,
            row_id, f"src_export_{uuid4().hex[:8]}", ts,
            json.dumps({"title": title}), canonical_url,
        )
    return row_id


async def _insert_finding(
    pg_store: PostgresStore,
    *,
    title: str,
    body: str = "",
    confidence: float = 0.7,
    severity: str | None = "medium",
    target_id: str | None = None,
    analyst_id: str | None = "test_analyst",
    citations: list[dict] | None = None,
) -> UUID:
    row_id = uuid4()
    ts = datetime.now(timezone.utc)
    # THE REAL BINDING PATH. `analyst_outputs.data` holds the whole
    # `payload.model_dump()` (provenance.writes), and `FindingPayload` has its
    # OWN field named `data` — so a citation list written as
    # `finding.data['citations']` lands DOUBLE-nested at
    # `data->'data'->'citations'`. This fixture used to insert the flat shape
    # by hand, which is why it never caught the export reading the wrong level
    # (every real exported finding printed "no citations recorded"). Built
    # through the real payload model so the nesting can never drift out of the
    # fixture again.
    data: dict = FindingPayload(
        title=title,
        body=body,
        confidence=confidence,
        data={"citations": citations} if citations is not None else {},
    ).model_dump(mode="json")
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
                $9, '{}'::uuid[], 'iglu:legba/finding/jsonschema/1-0-0', NULL
            )
            """,
            row_id, title, body, confidence, severity,
            json.dumps(data), target_id, analyst_id, ts,
        )
    return row_id


async def _insert_faithfulness_critique(
    pg_store: PostgresStore,
    *,
    analyzed_output_id: UUID,
    overall_score: float,
    verification: dict | None = None,
) -> UUID:
    """A 'Faithfulness verify' critique row — the S8-T2 title-pinned shape the
    export lateral joins on, with the P0-T3 verification block under
    ``data.data.verification``."""
    row_id = uuid4()
    ts = datetime.now(timezone.utc)
    data: dict = {
        "analyzed_output_id": str(analyzed_output_id),
        "overall_score": overall_score,
    }
    if verification is not None:
        data["data"] = {"verification": verification}
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'critique', $2, '', $3, NULL, $4::jsonb,
                NULL, NULL, 'verify', NULL,
                $5, $6::uuid[], 'iglu:legba/critique/jsonschema/1-0-0', NULL
            )
            """,
            row_id, f"Faithfulness verify (score {overall_score:.2f})",
            overall_score, json.dumps(data), ts, [analyzed_output_id],
        )
    return row_id


async def _insert_journal_entry(
    pg_store: PostgresStore,
    *,
    entry_kind: str = "entry",
    title: str = "e",
    body: str = "body.",
    claims: list[dict] | None = None,
    honesty_flags: list[str] | None = None,
    analyst_id: str | None = "journal_assessor",
) -> UUID:
    row_id = uuid4()
    ts = datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO journal_entries (
                id, entry_kind, title, body, claims, cited_substrate_refs,
                honesty_flags, period_start, period_end, produced_at,
                analyst_id, analyst_version
            ) VALUES (
                $1, $2, $3, $4, $5::jsonb, '{}'::uuid[],
                $6::text[], $7, $7, $7,
                $8, 'v1'
            )
            """,
            row_id, entry_kind, title, body, json.dumps(claims or []),
            honesty_flags or [], ts, analyst_id,
        )
    return row_id


# ---------------------------------------------------------------------------
# Integration — the route.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_mixed_finding_and_journal_json(export_app, client: AsyncClient):
    """Mixed basket, JSON format: composition in basket order; the finding
    carries live-resolved citations (+ the honest unresolved fallback), the
    verify state with hard/soft flags, the folded effective_confidence, and
    the lineage receipt path; the journal entry carries its tier label, the
    VOICE framing note, and claims with refs resolved to (kind, title)."""
    _, _, pg_store = export_app

    sig_id = await _insert_signal(
        pg_store,
        title="Troop columns filmed on Route 9",
        canonical_url="https://example.org/route9",
    )
    ghost_sig = uuid4()  # cited but never inserted — the pruned-signal path
    finding_id = await _insert_finding(
        pg_store,
        title="Coup risk rising",
        body="Army units moved overnight [1]. Denied by state media [2].",
        confidence=0.72,
        severity="high",
        target_id="country_g20_RR",
        analyst_id="country_assessor",
        citations=[
            {"marker": "[1]", "signal_id": str(sig_id), "title": "stored title 1"},
            {
                "marker": "[2]",
                "signal_id": str(ghost_sig),
                "title": "stored ghost title",
                "source": "https://stored.example/ghost",
            },
        ],
    )
    await _insert_faithfulness_critique(
        pg_store,
        analyzed_output_id=finding_id,
        overall_score=0.5,
        verification={
            "faithfulness_score": 0.5,
            "checkable_claims": 2,
            "supported_claims": 1,
            "unsupported_spans": [
                {"text": "Denied by state media", "reason": "judge_contradicted",
                 "markers": [2], "fail_class": "hard_fail"},
            ],
            "judge_status": "judge",
        },
    )

    journal_id = await _insert_journal_entry(
        pg_store,
        entry_kind="lens",
        title="Trend lens — quiet wires",
        body="Signal volume fell for a third week.",
        claims=[
            {
                "text_span": "Signal volume fell for a third week",
                "kind": "fact",
                "refs": [str(finding_id)],
            }
        ],
        honesty_flags=["forecast_unproven"],
        analyst_id="lens_trend",
    )

    resp = await client.post(
        "/api/v1/v3/export",
        json={
            "items": [
                {"kind": "finding", "id": str(finding_id)},
                {"kind": "journal_entry", "id": str(journal_id)},
            ],
            "format": "json",
            "title": "Mixed pack",
        },
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()

    assert doc["title"] == "Mixed pack"
    assert doc["item_count"] == 2
    assert doc["missing_count"] == 0
    assert doc["provenance_note"] == PROVENANCE_NOTE

    f, j = doc["items"]
    # -- the finding --
    assert f["kind"] == "finding"
    assert f["id"] == str(finding_id)
    assert f["analyst_id"] == "country_assessor"
    assert f["target_id"] == "country_g20_RR"
    assert "[1]" in f["body"] and "[2]" in f["body"]
    # Citation [1] resolves LIVE (signal title + canonical_url beat the stored
    # copy); citation [2]'s signal is gone → stored fields + resolved=false.
    c1, c2 = f["citations"]
    assert c1 == {
        "marker": "[1]",
        # 2026-08-30 — every exported citation declares its kind and the set
        # its ordinal indexes, so a reader never has to infer "signal" from
        # the presence of `signal_id` (that inference IS the defect this
        # train closed for the four non-signal kinds).
        "citation_kind": "signal",
        "resolves_against": "signals",
        "marker_class": None,
        "signal_id": str(sig_id),
        "ref_id": None,
        "title": "Troop columns filmed on Route 9",
        "canonical_url": "https://example.org/route9",
        "resolved": True,
        "resolution_source": "live",
        # P2-1 additive evidence-archive surface — this signal carries no
        # object_ref, so both stay honestly empty (never fabricated).
        "archived": False,
        "archive_sha256": None,
    }
    assert c2["resolved"] is False
    assert c2["resolution_source"] == "stored"
    assert c2["title"] == "stored ghost title"
    assert c2["canonical_url"] == "https://stored.example/ghost"
    assert c2["archived"] is False and c2["archive_sha256"] is None
    # Verify state + flags + the confidence fold (min(0.72, 0.5)).
    assert f["verify_state"] == "faithfulness=0.50"
    assert f["verify_flags"] == {"hard_fail": 1}
    assert f["confidence"] == pytest.approx(0.72)
    assert f["effective_confidence"] == pytest.approx(0.5)
    assert f["receipt_path"] == f"/api/v1/lineage/finding/{finding_id}"

    # -- the journal entry --
    assert j["kind"] == "journal_entry"
    assert j["tier"] == "lens"
    assert j["tier_label"] == JOURNAL_TIER_LABELS["lens"]
    assert j["voice_note"] == JOURNAL_VOICE_NOTE
    assert j["honesty_flags"] == ["forecast_unproven"]
    # The claim's bare-UUID ref resolved to the cited finding's (kind, title).
    claim = j["claims"][0]
    assert claim["text_span"] == "Signal volume fell for a third week"
    assert claim["refs"] == [
        {"id": str(finding_id), "kind": "finding", "title": "Coup risk rising"}
    ]
    # No faithfulness critique on the journal row → explicit unverified.
    assert j["verify_state"] == (
        "unverified — no faithfulness verdict recorded for this journal entry"
    )


def _grounding_citations(start: int) -> list[dict]:
    """The five DESK GROUNDING citations, built by the REAL producer
    (``unit_grounding.citation_for_block``) rather than hand-shaped — so this
    test cannot pass against a citation shape the desk does not actually
    write, and gains the ``marker_class`` mark automatically the moment the
    producer stamps it."""
    rows = [
        {
            ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_PRIOR_READ,
            "id": str(uuid4()),
            "title": "Ruritania — morning read, 18 July",
            "body": "BLUF: tension flat versus the prior sweep.",
            "analyst_id": "country_assessor",
            "produced_at": "2026-07-18T06:00:00+00:00",
            "age_hours": 48.0,
        },
        {
            ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_WINDOW_LEDGER,
            ug.GROUNDING_PAYLOAD_KEY: [
                {
                    "finding_id": str(uuid4()),
                    "analyst_id": "escalation",
                    "severity": "elevated",
                    "severity_rank": 3,
                    "title": "Ruritania — dated fortnight record",
                    "read_date": "16 July 2026",
                    "day": "2026-07-16",
                    "produced_at": "2026-07-16T07:00:00+00:00",
                },
            ],
        },
        {
            ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_SITUATIONS,
            ug.GROUNDING_PAYLOAD_KEY: [
                {
                    "situation_id": str(uuid4()),
                    "name": "Ruritania — open frame",
                    "status": "active",
                    "intensity_score": 52.7,
                    "event_count": 31,
                    "last_event_at": "2026-07-17T12:00:00+00:00",
                    "age_days": 30.2,
                },
            ],
        },
        {
            ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_BASELINE,
            ug.GROUNDING_PAYLOAD_KEY: [
                {
                    "desk_id": "country_g20_RR",
                    "metric": "signal_volume_24h",
                    "expected": 41.2,
                    "center_median": 39.0,
                    "band_low": 21.4,
                    "band_high": 61.0,
                    "current": 118.0,
                    "deviation": "above",
                    "deviation_sigma": 3.7,
                    "n_sigma": 2.0,
                    "baseline_days": 28,
                    "sample_days": 26,
                    "active_days": 26,
                    "computed_at": "2026-07-18T06:00:00+00:00",
                },
            ],
        },
        {
            ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_QUESTIONS,
            ug.GROUNDING_PAYLOAD_KEY: [
                {
                    "question_id": str(uuid4()),
                    "question": "Who controls the Route 9 corridor?",
                    "asked_at": "2026-07-12T19:00:00+00:00",
                    "age_days": 6.8,
                    "analyst_id": "country_assessor",
                },
            ],
        },
    ]
    out = []
    for offset, row in enumerate(rows):
        cite = ug.citation_for_block(row, start + offset)
        assert cite is not None, row[ug.UNIT_GROUNDING_ROW_KEY]
        out.append(cite)
    return out


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_carries_every_citation_kind(export_app, client: AsyncClient):
    """THE 2026-08-30 CONSUMER REPAIR.

    A finding's ``data['citations']`` holds THREE shapes and only one of them
    carries a ``signal_id``: the unit signal ref, the composition sub-claim ref
    (``ref_kind='finding'``, ``ref_id``), and the five DESK GROUNDING blocks
    (four of which carry no id at all). The export kept only the first,
    so a composition report exported as uncited and a unit read lost every
    block the verify plane had scored SUPPORTED — and, because the reader also
    looked at the wrong nesting level, a REAL row lost all of them.

    Every kind must now survive with its ``citation_kind`` and its
    ``resolves_against`` declared, and the "no citations" line must appear
    only when there genuinely are none.
    """
    _, _, pg_store = export_app

    sig_id = await _insert_signal(
        pg_store, title="Route 9 column", canonical_url="https://example.org/r9",
    )
    sub_id = uuid4()
    citations = [
        {"marker": "[1]", "signal_id": str(sig_id), "title": "stored title"},
        # The composition convention — no signal_id, so it was stripped too.
        {
            "marker": "[[ref:2]]",
            "ordinal": 2,
            "ref_id": str(sub_id),
            "ref_kind": "finding",
            "title": "Ruritania unit read — 18 July",
            "evidence_text": "Units moved overnight.",
        },
        *_grounding_citations(3),
    ]
    finding_id = await _insert_finding(
        pg_store,
        title="Every citation kind",
        body="Claim [1]. Sub-claim [[ref:2]]. Prior [3]. Ledger [4].",
        citations=citations,
    )

    resp = await client.post(
        "/api/v1/v3/export",
        json={"items": [{"kind": "finding", "id": str(finding_id)}], "format": "json"},
    )
    assert resp.status_code == 200, resp.text
    exported = resp.json()["items"][0]["citations"]

    # Nothing dropped: 1 signal + 1 sub-claim + all five grounding blocks.
    assert len(exported) == 7
    assert [c["citation_kind"] for c in exported] == [
        "signal",
        "finding",
        "prior_read",
        "window_ledger",
        "situation_register",
        "desk_baseline",
        "open_questions",
    ]
    # Every entry names the SET its ordinal indexes — no reader has to infer
    # the kind from which id field happens to be populated.
    by_kind = {c["citation_kind"]: c for c in exported}
    assert by_kind["signal"]["resolves_against"] == "signals"
    assert by_kind["signal"]["resolved"] is True
    assert by_kind["signal"]["resolution_source"] == "live"
    assert by_kind["signal"]["title"] == "Route 9 column"  # live beats stored
    assert by_kind["finding"]["resolves_against"] == "analyst_outputs"
    assert by_kind["finding"]["ref_id"] == str(sub_id)
    for kind in (
        "prior_read", "window_ledger", "situation_register",
        "desk_baseline", "open_questions",
    ):
        entry = by_kind[kind]
        assert entry["resolves_against"] == "data.citations", kind
        assert entry["resolution_source"] == "in_row", kind
        assert entry["signal_id"] is None, kind
        assert entry["title"], kind
    # Only the prior read has a real drill target; the four synthetic blocks
    # carry none and NONE is minted for them.
    assert by_kind["prior_read"]["ref_id"]
    for kind in ("window_ledger", "situation_register", "desk_baseline", "open_questions"):
        assert by_kind[kind]["ref_id"] is None, kind

    # THE COUPLING, end to end (task #78 phase B). `resolves_against` in the
    # exported document is the value the PRODUCER wrote, not one this module
    # re-derived from `ref_kind` — the citations above came out of the real
    # `unit_grounding.citation_for_block`. The signal and sub-claim refs carry
    # no `marker_class`, and none is invented for them.
    for kind in (
        "prior_read", "window_ledger", "situation_register",
        "desk_baseline", "open_questions",
    ):
        produced = next(
            c for c in citations
            if c.get("ref_kind") == kind
        )
        assert produced["marker_class"] == "desk_grounding", kind
        assert by_kind[kind]["marker_class"] == produced["marker_class"], kind
        assert by_kind[kind]["resolves_against"] == produced["resolves_against"], kind
    assert by_kind["signal"]["marker_class"] is None
    assert by_kind["finding"]["marker_class"] is None

    # -- the markdown half: kind + target stated, and no false "no citations" --
    resp_md = await client.post(
        "/api/v1/v3/export",
        json={
            "items": [{"kind": "finding", "id": str(finding_id)}],
            "format": "markdown",
        },
    )
    assert resp_md.status_code == 200
    md = resp_md.text
    assert "no citations recorded on this row" not in md
    assert "- [1] Route 9 column — https://example.org/r9" in md
    assert f"*sub-claim finding {sub_id}; resolves against analyst_outputs*" in md
    assert "*desk grounding · prior read" in md
    assert "*desk grounding · window ledger; resolves against data.citations*" in md
    assert "*desk grounding · open questions; resolves against data.citations*" in md


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_classifies_pre_stamp_grounding_rows(export_app, client: AsyncClient):
    """The fallback is the load-bearing path, not a courtesy.

    `marker_class` / `resolves_against` arrive with the `2026-08-30/1` stamp,
    so EVERY row already in the substrate lacks them. Classification has to
    work off the canonical `GROUNDING_REF_KINDS` registry for those, or the
    repair only fixes findings that do not exist yet."""
    _, _, pg_store = export_app
    citations = [
        {k: v for k, v in c.items() if k not in ("marker_class", "resolves_against")}
        for c in _grounding_citations(1)
    ]
    assert all("marker_class" not in c for c in citations)
    finding_id = await _insert_finding(
        pg_store, title="Pre-stamp read", body="Prior [1].", citations=citations,
    )
    resp = await client.post(
        "/api/v1/v3/export",
        json={"items": [{"kind": "finding", "id": str(finding_id)}], "format": "json"},
    )
    assert resp.status_code == 200, resp.text
    exported = resp.json()["items"][0]["citations"]
    assert [c["citation_kind"] for c in exported] == [
        "prior_read", "window_ledger", "situation_register",
        "desk_baseline", "open_questions",
    ]
    # The mark is not fabricated for a row that never carried it; the
    # resolution target falls back to the one the kind means.
    assert all(c["marker_class"] is None for c in exported)
    assert all(c["resolves_against"] == "data.citations" for c in exported)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_uncited_finding_states_it_without_a_false_filter(
    export_app, client: AsyncClient,
):
    """The honest empty case survives the repair: a row with NO citations at
    all still says so. What changed is that the line is now reachable ONLY
    when the row is genuinely uncited — it used to fire for every finding
    whose citations were all grounding blocks, and (via the nesting bug) for
    every real finding regardless."""
    _, _, pg_store = export_app
    finding_id = await _insert_finding(
        pg_store, title="Uncited read", body="No markers here.",
    )
    resp = await client.post(
        "/api/v1/v3/export",
        json={
            "items": [{"kind": "finding", "id": str(finding_id)}],
            "format": "markdown",
        },
    )
    assert resp.status_code == 200
    assert "*(no citations recorded on this row)*" in resp.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_markdown_document(export_app, client: AsyncClient):
    """Markdown format: one text/markdown document with the header block, the
    per-item sections, resolved citation lines, and the VOICE-framed journal
    section."""
    _, _, pg_store = export_app

    sig_id = await _insert_signal(
        pg_store, title="Sig headline", canonical_url="https://example.org/sig",
    )
    finding_id = await _insert_finding(
        pg_store,
        title="MD finding",
        body="Claim [1].",
        citations=[{"marker": "[1]", "signal_id": str(sig_id)}],
    )
    journal_id = await _insert_journal_entry(
        pg_store, entry_kind="chronicle", title="MD chronicle", body="A week.",
    )

    resp = await client.post(
        "/api/v1/v3/export",
        json={
            "items": [
                {"kind": "finding", "id": str(finding_id)},
                {"kind": "journal_entry", "id": str(journal_id)},
            ],
            "format": "markdown",
            "title": "MD pack",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment; filename=" in resp.headers.get("content-disposition", "")
    md = resp.text

    assert md.startswith("# MD pack\n")
    assert f"> {PROVENANCE_NOTE}" in md
    assert "- items: 2\n" in md
    assert "## 1. MD finding" in md
    # No critique + a verify-covered analyst → the explicit unverified state.
    assert "- verify: unverified — no faithfulness verdict recorded for this finding" in md
    assert "- [1] Sig headline — https://example.org/sig" in md
    assert f"- receipt: /api/v1/lineage/finding/{finding_id}" in md
    assert "## 2. MD chronicle" in md
    assert f"tier: {JOURNAL_TIER_LABELS['chronicle']}" in md
    assert f"- voice: {JOURNAL_VOICE_NOTE}" in md


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_missing_id_is_stated_not_dropped(export_app, client: AsyncClient):
    """A basket id that resolves to no row exports as an explicit 'not found in
    substrate' item, counted in the header — never silently dropped."""
    _, _, pg_store = export_app
    finding_id = await _insert_finding(pg_store, title="Real one")
    ghost = uuid4()

    resp = await client.post(
        "/api/v1/v3/export",
        json={
            "items": [
                {"kind": "finding", "id": str(finding_id)},
                {"kind": "journal_entry", "id": str(ghost)},
            ],
            "format": "json",
        },
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["item_count"] == 2
    assert doc["missing_count"] == 1
    assert doc["items"][1] == {
        "kind": "journal_entry",
        "id": str(ghost),
        "error": "not found in substrate",
    }

    # The markdown render states it too.
    resp_md = await client.post(
        "/api/v1/v3/export",
        json={
            "items": [{"kind": "journal_entry", "id": str(ghost)}],
            "format": "markdown",
        },
    )
    assert resp_md.status_code == 200
    assert "not found in substrate" in resp_md.text
    assert "- items: 1 (1 not found)" in resp_md.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_cap_413_and_empty_400(export_app, client: AsyncClient):
    """51 items → an honest 413 naming the cap + the actual count; an empty
    basket → 400."""
    over = [{"kind": "finding", "id": str(uuid4())} for _ in range(EXPORT_MAX_ITEMS + 1)]
    resp = await client.post(
        "/api/v1/v3/export", json={"items": over, "format": "json"},
    )
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert str(EXPORT_MAX_ITEMS) in detail
    assert str(EXPORT_MAX_ITEMS + 1) in detail

    resp_empty = await client.post(
        "/api/v1/v3/export", json={"items": [], "format": "json"},
    )
    assert resp_empty.status_code == 400
