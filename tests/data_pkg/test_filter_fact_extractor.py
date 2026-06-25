# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit + integration tests for the ``fact_extractor`` enrichment stage
(anchor §5 PIECE 2).

The 8B/LLM model is STUBBED — no live model in unit tests. The default
``relation`` backend reuses the REBEL triples already on the signal (or the
mocked ``/extract`` endpoint via ``httpx.MockTransport``, as in
``test_filter_ner``). DB-backed cases use the same ``migrated_pg`` fixture as
``test_writes``.

Covers (mirrors the plan's test plan §6.1):
  * relation backend writes (subject, predicate, value) rows with
    source_type='ingestion', valid_from = payload _published_at_dt,
    derived_from=[signal_id].
  * /extract fallback when payload['entities'] is empty.
  * non-entity rejection ("50%" / "June 2026" dropped via _is_nonentity_candidate).
  * idempotency: same triple twice → ONE row, confidence=max, lineage unioned.
  * PIECE B supersession: a new value for an existing (subject, predicate)
    closes the prior open row (valid_until + superseded_by) and opens the new
    one; an identical-triple re-assert does NOT supersede (upsert only).
  * max_facts_per_signal cap.
  * backend='llm' with no llm_handler_factory raises (no-stub proof).
  * backend='llm' with a stubbed handler writes parsed triples.
  * transform NEVER returns None and NEVER raises on extractor failure.
  * handler ClassVar shape (L-102 §1/§3).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.filters._contract import FilterContext, FilterHealth, StreamHandler
from legba.data.filters.fact_extractor import (
    _INGESTION_DEFAULT_CONFIDENCE,
    FactExtractorConfig,
    FactExtractorHandler,
    FactExtractorUnconfigured,
    _entities_to_triples,
    _event_time,
    _is_junk_triple,
    _is_quantity_phrase,
    _parse_llm_triples,
    _resolve_ingestion_confidence,
    _scrub_entity_surface,
)


def test_scrub_entity_surface_html_and_zero_width():
    """DQ-H4 pre-write scrub: HTML-unescape + strip zero-width chars + collapse WS."""
    assert _scrub_entity_surface("Benjamin Netanyahu&#039;s") == "Benjamin Netanyahu's"
    assert _scrub_entity_surface("Iran&amp;Israel") == "Iran&Israel"
    # Zero-width space (U+200B) inside a name is removed.
    assert _scrub_entity_surface("Vladimir​Putin") == "VladimirPutin"
    assert _scrub_entity_surface("  spaced   out  ") == "spaced out"
    assert _scrub_entity_surface("") == ""
from legba.data.filters.slm_relationship_validate import (
    SLMRelationshipValidateConfig,
    SLMRelationshipValidateHandler,
)
from legba.data.sources._contract import Signal
from legba.data.stack.nlp_service import NlpServiceClient


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


def _ctx() -> FilterContext:
    return FilterContext(
        target_id="t.test",
        target_version="v1",
        filter_id="fact_extractor_test",
        logger=logging.getLogger("test.fact_extractor"),
    )


def _signal(
    *,
    entities: list[dict[str, Any]] | None = None,
    title: str = "",
    body: str = "",
    published_at: datetime | None = None,
) -> Signal:
    payload: dict[str, Any] = {}
    if title:
        payload["title"] = title
    if body:
        payload["body"] = body
    if entities is not None:
        payload["entities"] = entities
    if published_at is not None:
        payload["_published_at_dt"] = published_at
    return Signal(
        source_id="src.test",
        payload=payload,
        content_hash=f"hash-{title}{body}",
    )


def _build_nlp_client(handler: Any) -> NlpServiceClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(
        base_url="https://models.test.invalid",
        transport=transport,
        timeout=5.0,
    )
    return NlpServiceClient(
        endpoint="https://models.test.invalid",
        api_user="u",
        api_pass="p",
        client=inner,
    )


def _extract_handler(triples: list[dict[str, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/extract":
            return httpx.Response(200, json={"triples": triples, "ms": 1.0})
        return httpx.Response(404, json={"error": "unexpected"})
    return handler


class _StubLLMResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubLLMHandler:
    def __init__(self, content: str | Exception) -> None:
        self._content = content
        self.calls = 0

    async def chat_complete(self, messages, *, system=None, **kwargs):
        self.calls += 1
        if isinstance(self._content, Exception):
            raise self._content
        return _StubLLMResponse(self._content)


def _llm_factory(handler: _StubLLMHandler):
    async def factory(component_id: str):
        return handler
    return factory


# REBEL-shaped entities (as ner_multilingual emits): subject then object,
# sharing a predicate.
def _rebel_entities() -> list[dict[str, Any]]:
    return [
        {"class": "corporation", "text": "Apple Inc.",
         "predicate": "headquarters location", "confidence": 1.0},
        {"class": "location", "text": "Cupertino",
         "predicate": "headquarters location", "confidence": 1.0},
        {"class": "person", "text": "Tim Cook",
         "predicate": "employer", "confidence": 0.9},
        {"class": "corporation", "text": "Apple Inc.",
         "predicate": "employer", "confidence": 0.9},
    ]


# ---------------------------------------------------------------------------
# Pure-unit (no DB)
# ---------------------------------------------------------------------------


def test_is_junk_triple_rejects_leadership_predicates():
    # DQ-H3: ingestion NER must not assert leadership facts (seed/curated own
    # them). The live junk this kills:
    assert _is_junk_triple("Adolf Hitler", "leader of", "Germany") is True
    assert _is_junk_triple("Didier Deschamps", "leader of", "Algeria") is True
    assert _is_junk_triple("Antonio Guterres", "leader of", "Haiti") is True
    assert _is_junk_triple("United States", "head of state", "Joe Biden") is True
    # CamelCase normalizes to the same canonical predicate → also rejected.
    assert _is_junk_triple("Javier Milei", "LeaderOf", "Argentina") is True
    # A NON-leadership relation with the same endpoints is NOT rejected here.
    assert _is_junk_triple("Germany", "member of", "NATO") is False


def test_handler_class_vars():
    assert FactExtractorHandler.kind == "fact_extractor"
    assert FactExtractorHandler.family == "filter"
    assert FactExtractorHandler.config_schema is FactExtractorConfig
    assert FactExtractorHandler.idempotent is True
    assert FactExtractorHandler.output_contract == {}
    assert isinstance(FactExtractorHandler(  # satisfies the StreamHandler proto
        FactExtractorConfig(), pg_pool=object()), StreamHandler)


def test_config_defaults():
    cfg = FactExtractorConfig()
    assert cfg.backend == "relation"
    assert cfg.max_facts_per_signal == 50
    assert cfg.emit_graph_edges is False
    # The quantity-endpoint validity gate ships ON by default (graph-and-data
    # Wave-1b item 4) — REBEL stamps every triple confidence=1.0, so this is the
    # only floor against the worst spelled-out-quantity noise.
    assert cfg.reject_quantity_endpoints is True
    # Relation-type allowlist is OFF by default (None) — typing still stamped.
    assert cfg.relation_allowlist is None


def test_config_rejects_bad_backend():
    with pytest.raises(ValueError):
        FactExtractorConfig(backend="nonsense").validated_backend()


@pytest.mark.parametrize(
    "text,expected",
    [
        # The audit's noise endpoints — pure quantity / ordinal phrases.
        ("sixth", True),
        ("At least five", True),
        ("first", True),
        ("several thousand", True),
        ("of sixth", True),
        ("five", True),
        ("12", True),
        # Real entities — a single nominal token keeps the endpoint.
        ("NEET", False),
        ("Strait of Hormuz", False),
        ("World Cup", False),
        ("five US senators", False),  # mixed → kept (conservative)
        ("FBI", False),
        ("", False),                  # empty → this gate makes no claim
    ],
)
def test_is_quantity_phrase(text, expected):
    """The light validity gate flags endpoints that are ENTIRELY spelled-out
    numbers / ordinals / quantity-qualifiers, and keeps anything with a real
    nominal token (conservative)."""
    assert _is_quantity_phrase(text) is expected


def test_requires_pg_pool():
    with pytest.raises(FactExtractorUnconfigured):
        FactExtractorHandler(FactExtractorConfig(), pg_pool=None)


def test_llm_backend_requires_factory():
    # no-stub proof: backend='llm' with no llm_handler_factory raises loud.
    with pytest.raises(FactExtractorUnconfigured):
        FactExtractorHandler(
            FactExtractorConfig(backend="llm", llm_component_id="c.8b"),
            pg_pool=object(),
            llm_handler_factory=None,
        )


def test_event_time_precedence():
    pub = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    sig = _signal(published_at=pub, title="x")
    assert _event_time(sig) == pub
    # No logical ts → falls back to fetched_at.
    sig2 = _signal(title="x")
    assert _event_time(sig2) == (
        sig2.fetched_at if sig2.fetched_at.tzinfo else sig2.fetched_at.replace(tzinfo=timezone.utc)
    )


def test_entities_to_triples_pairs_by_predicate():
    triples = _entities_to_triples(_rebel_entities())
    norm = {(t["subject"], t["predicate"], t["object"]) for t in triples}
    assert ("Apple Inc.", "headquarters location", "Cupertino") in norm
    assert ("Tim Cook", "employer", "Apple Inc.") in norm


def test_parse_llm_triples_tolerant():
    content = '```json\n[{"subject":"A","predicate":"located in","value":"B"}]\n```'
    out = _parse_llm_triples(content)
    # A score-less LLM triple carries confidence=None (NOT a fabricated 1.0) so
    # the write-path resolver applies the sane default rather than laundering a
    # fake 1.0 (Phase B item 4).
    assert out == [
        {"subject": "A", "predicate": "located in", "object": "B",
         "confidence": None}
    ]
    assert _parse_llm_triples("not json") == []


# --- Phase B item 2: NER-junk gate ----------------------------------------


@pytest.mark.parametrize(
    "subject,value,expected",
    [
        # Self-referential — subject == value, or one is a token-aligned proper
        # subphrase of the other (a co-reference artifact, not a relation).
        ("Putin", "Vladimir Putin", True),
        ("Vladimir Putin", "Putin", True),
        ("France", "France", True),
        ("france", "  France ", True),       # casefold + strip
        # HTML-entity-escaped endpoints (leaked un-unescaped text).
        ("Macron&#39;s", "France", True),
        ("AT&amp;T", "United States", True),
        ("France", "Q&#x27;s", True),
        # Empty / pure-numeric / pure-punctuation endpoints.
        ("", "France", True),
        ("   ", "France", True),
        ("...", "France", True),
        ("2026", "France", True),
        ("France", "1,234", True),
        # Legitimate distinct-name triples — MUST pass (not junk).
        ("Macron", "France", False),
        ("BBC", "United Kingdom", False),
        ("Apple Inc.", "Cupertino", False),
        ("Iran", "Iranian", False),          # token-boundary guard: not a sub
        ("US", "United States", False),       # distinct tokens, not a subphrase
    ],
)
def test_is_junk_triple(subject, value, expected):
    """Self-referential / HTML-escaped / empty / numeric endpoints are junk;
    distinct-name triples pass (conservative)."""
    assert _is_junk_triple(subject, "related to", value) is expected


# --- Phase B item 4: real ingestion confidence -----------------------------


def test_resolve_ingestion_confidence_relation_sentinel_demoted():
    """REBEL stamps a synthetic 1.0 on every triple; the relation backend must
    demote that exact sentinel to the sane default (NOT land at 1.000)."""
    assert _resolve_ingestion_confidence(
        {"confidence": 1.0}, "relation"
    ) == _INGESTION_DEFAULT_CONFIDENCE
    assert _INGESTION_DEFAULT_CONFIDENCE < 1.0


def test_resolve_ingestion_confidence_keeps_real_scores():
    # A genuine sub-1.0 REBEL score is a real measurement → kept.
    assert _resolve_ingestion_confidence({"confidence": 0.9}, "relation") == pytest.approx(0.9)
    # An LLM-provided score is used as-is (any value, including a deliberate 1.0).
    assert _resolve_ingestion_confidence({"confidence": 0.8}, "llm") == pytest.approx(0.8)


def test_resolve_ingestion_confidence_missing_falls_to_default():
    assert _resolve_ingestion_confidence({}, "relation") == _INGESTION_DEFAULT_CONFIDENCE
    assert _resolve_ingestion_confidence({"confidence": None}, "llm") == _INGESTION_DEFAULT_CONFIDENCE
    assert _resolve_ingestion_confidence({"confidence": "junk"}, "llm") == _INGESTION_DEFAULT_CONFIDENCE


# ---------------------------------------------------------------------------
# DB-backed (relation backend)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_relation_backend_writes_ingestion_facts(pg_pool):
    pub = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
    handler = FactExtractorHandler(
        FactExtractorConfig(backend="relation"), pg_pool=pg_pool
    )
    await handler.on_activate(_ctx())
    sig = _signal(entities=_rebel_entities(), title="Apple news", published_at=pub)

    out = await handler.transform(sig, _ctx())
    assert out is sig  # never drops

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT subject, predicate, value, source_type, valid_from, "
            "derived_from, confidence FROM facts "
            "WHERE data->>'signal_id' = $1 ORDER BY subject",
            str(sig.signal_id),
        )
    assert rows, "expected ingestion facts written"
    for r in rows:
        assert r["source_type"] == "ingestion"
        assert r["valid_from"] == pub
        assert str(sig.signal_id) in [str(u) for u in r["derived_from"]]
        assert 0.0 <= r["confidence"] <= 1.0
    triple_set = {(r["subject"], r["predicate"], r["value"]) for r in rows}
    assert ("Apple Inc.", "headquarters location", "Cupertino") in triple_set


@pytest.mark.integration
@pytest.mark.asyncio
async def test_extract_fallback_when_no_entities(pg_pool):
    client = _build_nlp_client(_extract_handler([
        {"subject": "Berlin", "predicate": "capital of", "object": "Germany"},
    ]))
    handler = FactExtractorHandler(
        FactExtractorConfig(backend="relation"),
        pg_pool=pg_pool,
        nlp_client=client,
    )
    await handler.on_activate(_ctx())
    sig = _signal(title="Berlin is the capital of Germany")  # no entities

    await handler.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT subject, predicate, value FROM facts "
            "WHERE lower(subject)='berlin'"
        )
    assert row is not None
    assert row["predicate"] == "capital of"
    assert row["value"] == "Germany"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nonentity_endpoints_rejected(pg_pool):
    entities = [
        {"class": "entity", "text": "Inflation", "predicate": "rose to", "confidence": 1.0},
        {"class": "entity", "text": "50%", "predicate": "rose to", "confidence": 1.0},
        {"class": "event", "text": "Summit", "predicate": "scheduled for", "confidence": 1.0},
        {"class": "entity", "text": "June 2026", "predicate": "scheduled for", "confidence": 1.0},
    ]
    handler = FactExtractorHandler(FactExtractorConfig(), pg_pool=pg_pool)
    await handler.on_activate(_ctx())
    sig = _signal(entities=entities, title="econ")
    await handler.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        bad = await conn.fetch(
            "SELECT value FROM facts WHERE value = ANY($1::text[])",
            ["50%", "June 2026"],
        )
    assert bad == [], "numeric/date endpoints must be rejected"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_quantity_endpoints_gate(pg_pool):
    """With reject_quantity_endpoints=True, a triple whose value is a pure
    spelled-out quantity ("At least five") is dropped, while a sibling triple
    with a real-name endpoint survives. Mirrors the live REBEL noise the audit
    flagged ("FBI controls At least five")."""
    nonce = uuid4().hex[:8]
    acme, town = f"AcmeCo_{nonce}", f"Springs_{nonce}"
    entities = [
        # Noise: value is a pure quantity phrase → dropped by the gate.
        {"class": "entity", "text": "FBI", "predicate": "controls", "confidence": 1.0},
        {"class": "entity", "text": "At least five", "predicate": "controls", "confidence": 1.0},
        # Good: both endpoints are real names → kept. Unique-suffixed so the
        # Phase B identical-triple dedupe (shared session DB) never collapses
        # this onto a sibling test's Apple/Cupertino row.
        {"class": "entity", "text": acme, "predicate": "headquarters location", "confidence": 1.0},
        {"class": "location", "text": town, "predicate": "headquarters location", "confidence": 1.0},
    ]
    handler = FactExtractorHandler(
        FactExtractorConfig(reject_quantity_endpoints=True), pg_pool=pg_pool
    )
    await handler.on_activate(_ctx())
    sig = _signal(entities=entities, title="quantity-gate")
    await handler.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT subject, value FROM facts WHERE data->>'signal_id' = $1",
            str(sig.signal_id),
        )
    triples = {(r["subject"], r["value"]) for r in rows}
    assert ("FBI", "At least five") not in triples, "quantity endpoint must be gated"
    assert (acme, town) in triples, "real-name triple must survive"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quantity_gate_explicitly_off_keeps_triple(pg_pool):
    """The gate can be turned OFF per descriptor: with
    reject_quantity_endpoints=False, a quantity endpoint is NOT dropped by this
    gate (the default is now ON — graph-and-data Wave-1b item 4)."""
    entities = [
        {"class": "entity", "text": "FBI", "predicate": "controls", "confidence": 1.0},
        {"class": "entity", "text": "At least five", "predicate": "controls", "confidence": 1.0},
    ]
    handler = FactExtractorHandler(
        FactExtractorConfig(reject_quantity_endpoints=False), pg_pool=pg_pool
    )
    await handler.on_activate(_ctx())
    sig = _signal(entities=entities, title="quantity-gate-off")
    await handler.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM facts WHERE data->>'signal_id' = $1 "
            "AND value = 'At least five'",
            str(sig.signal_id),
        )
    assert row is not None, "gate off must keep the quantity endpoint"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_relation_type_stamped_and_allowlist_filters(pg_pool):
    """Every kept fact carries a canonical data.relation_type; a configured
    allowlist keeps only triples whose canonical type is on it
    (graph-and-data Wave-1b item 4)."""
    # Unique-suffixed names so the shared dev DB's other fact rows (and other
    # tests') never perturb this assertion — the lookup is scoped by signal_id
    # too, but distinct names keep the intent unambiguous.
    nonce = uuid4().hex[:8]
    acme, town = f"AcmeCo_{nonce}", f"Springs_{nonce}"
    globex, initech = f"Globex_{nonce}", f"Initech_{nonce}"
    entities = [
        # "headquarters location" → LocatedIn (on the allowlist below → kept).
        {"class": "corporation", "text": acme,
         "predicate": "headquarters location", "confidence": 1.0},
        {"class": "location", "text": town,
         "predicate": "headquarters location", "confidence": 1.0},
        # "rivals" → CoOccursWith generic (NOT on the allowlist → dropped).
        {"class": "organization", "text": globex,
         "predicate": "rivals", "confidence": 1.0},
        {"class": "organization", "text": initech,
         "predicate": "rivals", "confidence": 1.0},
    ]
    handler = FactExtractorHandler(
        FactExtractorConfig(relation_allowlist=["LocatedIn", "MemberOf"]),
        pg_pool=pg_pool,
    )
    await handler.on_activate(_ctx())
    sig = _signal(entities=entities, title="allowlist")
    await handler.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT subject, value, data->>'relation_type' AS rt FROM facts "
            "WHERE data->>'signal_id' = $1",
            str(sig.signal_id),
        )
    by_subj = {r["subject"]: r["rt"] for r in rows}
    assert by_subj.get(acme) == "LocatedIn", "allowed type kept + typed"
    assert globex not in by_subj, "off-allowlist (generic) triple must be dropped"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotent_upsert(pg_pool):
    pub = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    ents = [
        {"class": "location", "text": "Paris", "predicate": "capital of", "confidence": 0.5},
        {"class": "country", "text": "France", "predicate": "capital of", "confidence": 0.5},
    ]
    h = FactExtractorHandler(FactExtractorConfig(), pg_pool=pg_pool)
    await h.on_activate(_ctx())
    s1 = _signal(entities=ents, title="a", published_at=pub)
    await h.transform(s1, _ctx())

    # Second signal, same triple+valid_from, higher confidence.
    ents2 = [dict(e, confidence=0.9) for e in ents]
    s2 = _signal(entities=ents2, title="b", published_at=pub)
    await h.transform(s2, _ctx())

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT confidence, derived_from FROM facts "
            "WHERE lower(subject)='paris' AND lower(predicate)='capital of' "
            "AND lower(value)='france' AND valid_from=$1",
            pub,
        )
    assert len(rows) == 1, "duplicate triple must upsert to ONE row"
    assert rows[0]["confidence"] == pytest.approx(0.9)
    lineage = [str(u) for u in rows[0]["derived_from"]]
    assert str(s1.signal_id) in lineage and str(s2.signal_id) in lineage


@pytest.mark.integration
@pytest.mark.asyncio
async def test_value_change_supersedes_prior(pg_pool):
    """PIECE B auto-supersession: a NEW value for the same (subject, predicate)
    closes the prior open row (valid_until + superseded_by) and opens the new
    one as the single canonical (open) row."""
    h = FactExtractorHandler(FactExtractorConfig(), pg_pool=pg_pool)
    await h.on_activate(_ctx())

    # First assertion: Paris is the capital of France.
    pub1 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    ents1 = [
        {"class": "person", "text": "Acmestan_fx", "predicate": "led by", "confidence": 1.0},
        {"class": "person", "text": "Alice", "predicate": "led by", "confidence": 1.0},
    ]
    s1 = _signal(entities=ents1, title="a", published_at=pub1)
    await h.transform(s1, _ctx())

    # Second assertion: same subject+predicate, DIFFERENT value (new leader).
    pub2 = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    ents2 = [
        {"class": "person", "text": "Acmestan_fx", "predicate": "led by", "confidence": 1.0},
        {"class": "person", "text": "Bob", "predicate": "led by", "confidence": 1.0},
    ]
    s2 = _signal(entities=ents2, title="b", published_at=pub2)
    await h.transform(s2, _ctx())

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT value, valid_until, superseded_by FROM facts "
            "WHERE lower(subject)='acmestan_fx' AND lower(predicate)='led by' "
            "ORDER BY value"
        )
        open_rows = await conn.fetch(
            "SELECT id, value FROM facts "
            "WHERE lower(subject)='acmestan_fx' AND lower(predicate)='led by' "
            "AND valid_until IS NULL AND superseded_by IS NULL"
        )
    by_value = {r["value"]: r for r in rows}
    assert set(by_value) == {"Alice", "Bob"}
    # Prior (Alice) is CLOSED + chained to the new row.
    assert by_value["Alice"]["valid_until"] is not None
    assert by_value["Alice"]["superseded_by"] is not None
    # New (Bob) is OPEN.
    assert by_value["Bob"]["valid_until"] is None
    assert by_value["Bob"]["superseded_by"] is None
    # Exactly ONE open canonical row, and it is Bob.
    assert len(open_rows) == 1
    assert open_rows[0]["value"] == "Bob"
    # The chain points at the new (Bob) row id.
    assert by_value["Alice"]["superseded_by"] == open_rows[0]["id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_identical_triple_does_not_supersede(pg_pool):
    """A same-value re-assert is the idempotency upsert, NOT a supersession:
    the open row stays open (no valid_until / superseded_by), confidence lifts."""
    h = FactExtractorHandler(FactExtractorConfig(), pg_pool=pg_pool)
    await h.on_activate(_ctx())
    pub = datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc)
    ents = [
        {"class": "location", "text": "Lisbon", "predicate": "capital of", "confidence": 0.4},
        {"class": "country", "text": "Portugal", "predicate": "capital of", "confidence": 0.4},
    ]
    s1 = _signal(entities=ents, title="a", published_at=pub)
    await h.transform(s1, _ctx())
    # Re-assert the SAME triple+valid_from at higher confidence.
    ents2 = [dict(e, confidence=0.9) for e in ents]
    s2 = _signal(entities=ents2, title="b", published_at=pub)
    await h.transform(s2, _ctx())

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT confidence, valid_until, superseded_by FROM facts "
            "WHERE lower(subject)='lisbon' AND lower(predicate)='capital of' "
            "AND lower(value)='portugal'"
        )
    assert len(rows) == 1, "identical triple must upsert to ONE row (no spurious supersession)"
    assert rows[0]["valid_until"] is None, "same-value re-assert must NOT close the row"
    assert rows[0]["superseded_by"] is None
    assert rows[0]["confidence"] == pytest.approx(0.9)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingestion_confidence_not_all_one(pg_pool):
    """Phase B item 4: REBEL's synthetic 1.0 must NOT land as a 1.000 ingestion
    confidence — the relation backend demotes it to the sane default."""
    pub = datetime(2026, 6, 7, 0, 0, tzinfo=timezone.utc)
    nonce = uuid4().hex[:8]
    # REBEL-shaped entities all carry confidence=1.0 (the synthetic sentinel).
    # Unique-suffixed so the dedupe never collapses onto a sibling test's row.
    ents = [
        {"class": "corporation", "text": f"ConfCo_{nonce}", "predicate": "headquarters location", "confidence": 1.0},
        {"class": "location", "text": f"ConfTown_{nonce}", "predicate": "headquarters location", "confidence": 1.0},
    ]
    h = FactExtractorHandler(FactExtractorConfig(backend="relation"), pg_pool=pg_pool)
    await h.on_activate(_ctx())
    sig = _signal(entities=ents, title="conf", published_at=pub)
    await h.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT confidence FROM facts WHERE data->>'signal_id' = $1",
            str(sig.signal_id),
        )
    assert rows, "expected an ingestion fact"
    for r in rows:
        assert r["confidence"] < 1.0, "REBEL's synthetic 1.0 must be demoted"
        assert r["confidence"] == pytest.approx(_INGESTION_DEFAULT_CONFIDENCE)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_identical_triple_dedupe_across_event_times(pg_pool):
    """Phase B item 3: the SAME (subject, predicate, value) re-ingested from two
    signals with DIFFERENT event-times must collapse to ONE open row (the 0032
    index keys on valid_from too, so without the dedupe each event-time would
    accumulate a separate open row — the live 'Russian located in UK' ×8 bug).
    A DIFFERENT value still supersedes (the dedupe does not break that)."""
    h = FactExtractorHandler(FactExtractorConfig(backend="relation"), pg_pool=pg_pool)
    await h.on_activate(_ctx())
    subj = f"Dedupestan_{uuid4().hex[:8]}"

    def _ents(value: str) -> list[dict[str, Any]]:
        return [
            {"class": "country", "text": subj, "predicate": "located in", "confidence": 0.5},
            {"class": "location", "text": value, "predicate": "located in", "confidence": 0.5},
        ]

    # Same triple, three distinct event-times.
    for day in (1, 2, 3):
        pub = datetime(2026, 6, day, 0, 0, tzinfo=timezone.utc)
        await h.transform(_signal(entities=_ents("UK"), title=f"d{day}", published_at=pub), _ctx())

    async with pg_pool.acquire() as conn:
        open_rows = await conn.fetch(
            "SELECT id, valid_from FROM facts "
            "WHERE lower(subject)=lower($1) AND lower(predicate)='located in' "
            "AND lower(value)='uk' "
            "AND valid_until IS NULL AND superseded_by IS NULL",
            subj,
        )
    assert len(open_rows) == 1, "identical triple across event-times must dedupe to ONE open row"
    # The collapsed row keeps the EARLIEST event-time (LEAST on refresh).
    assert open_rows[0]["valid_from"] == datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)

    # A DIFFERENT value still supersedes (dedupe complements, not replaces).
    pub4 = datetime(2026, 6, 4, 0, 0, tzinfo=timezone.utc)
    await h.transform(_signal(entities=_ents("France"), title="d4", published_at=pub4), _ctx())
    async with pg_pool.acquire() as conn:
        open_after = await conn.fetch(
            "SELECT value FROM facts "
            "WHERE lower(subject)=lower($1) AND lower(predicate)='located in' "
            "AND valid_until IS NULL AND superseded_by IS NULL",
            subj,
        )
    assert {r["value"] for r in open_after} == {"France"}, "value change supersedes to the single new open row"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_camelcase_predicate_normalized_at_ingestion(pg_pool):
    """Phase B item 5: a CamelCase predicate (an LLM/SLM might emit one) is
    converged to the canonical lowercase-spaced form at the write."""
    # NB: uses a NON-leadership CamelCase predicate (MemberOf) — DQ-H3 drops
    # ingestion leadership predicates (LeaderOf/leader of/...) outright.
    stub = _StubLLMHandler(
        json.dumps([
            {"subject": "Narnia", "predicate": "MemberOf", "value": "Alliance",
             "confidence": 0.8},
        ])
    )
    h = FactExtractorHandler(
        FactExtractorConfig(backend="llm", llm_component_id="c.8b"),
        pg_pool=pg_pool,
        llm_handler_factory=_llm_factory(stub),
    )
    await h.on_activate(_ctx())
    sig = _signal(title="Narnia is a member of the Alliance")
    await h.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT predicate FROM facts WHERE lower(subject)='narnia'"
        )
    assert row is not None
    assert row["predicate"] == "member of", "CamelCase predicate must normalize to lowercase-spaced"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_junk_triple_dropped_at_ingestion(pg_pool):
    """Phase B item 2: a self-referential triple ('Putin'→'Vladimir Putin') is
    dropped at ingestion; a sibling distinct-name triple survives."""
    ents = [
        # Self-referential → dropped by the junk gate.
        {"class": "person", "text": "Putin", "predicate": "also known as", "confidence": 1.0},
        {"class": "person", "text": "Vladimir Putin", "predicate": "also known as", "confidence": 1.0},
        # Distinct names, NON-leadership relation → kept. (DQ-H3 drops leadership
        # predicates, so the surviving control uses 'operates in'.)
        {"class": "org", "text": "BBC", "predicate": "operates in", "confidence": 1.0},
        {"class": "country", "text": "United Kingdom", "predicate": "operates in", "confidence": 1.0},
    ]
    h = FactExtractorHandler(FactExtractorConfig(backend="relation"), pg_pool=pg_pool)
    await h.on_activate(_ctx())
    sig = _signal(entities=ents, title="junk")
    await h.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT subject, value FROM facts WHERE data->>'signal_id' = $1",
            str(sig.signal_id),
        )
    pairs = {(r["subject"], r["value"]) for r in rows}
    assert ("Putin", "Vladimir Putin") not in pairs, "self-referential triple must be dropped"
    assert ("BBC", "United Kingdom") in pairs, "distinct-name triple must survive"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_max_facts_cap(pg_pool):
    ents: list[dict[str, Any]] = []
    for i in range(20):
        ents.append({"class": "person", "text": f"Person{i}", "predicate": "knows", "confidence": 1.0})
        ents.append({"class": "person", "text": f"Other{i}", "predicate": "knows", "confidence": 1.0})
    h = FactExtractorHandler(
        FactExtractorConfig(max_facts_per_signal=3), pg_pool=pg_pool
    )
    await h.on_activate(_ctx())
    sig = _signal(entities=ents, title="cap")
    await h.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE data->>'signal_id' = $1",
            str(sig.signal_id),
        )
    assert n == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transform_never_raises_on_db_error(pg_pool):
    # Closed pool → write raises internally; transform must swallow + return signal.
    bad_pool = await asyncpg.create_pool(
        (await _cfg_from_pool(pg_pool)), min_size=1, max_size=1
    )
    await bad_pool.close()
    h = FactExtractorHandler(FactExtractorConfig(), pg_pool=bad_pool)
    await h.on_activate(_ctx())
    sig = _signal(entities=_rebel_entities(), title="x")
    out = await h.transform(sig, _ctx())
    assert out is sig
    health = await h.health_check(_ctx())
    assert isinstance(health, FilterHealth)
    assert health.state in ("degraded", "unhealthy")


async def _cfg_from_pool(pool) -> str:
    # Recover the DSN from one of the pool's connections for the bad-pool test.
    async with pool.acquire() as conn:
        db = await conn.fetchval("SELECT current_database()")
    return f"postgresql://legba:legba@127.0.0.1:5432/{db}"


# ---------------------------------------------------------------------------
# LLM backend (stubbed model)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llm_backend_writes_parsed_triples(pg_pool):
    stub = _StubLLMHandler(
        json.dumps([
            {"subject": "Acme Corp", "predicate": "located in",
             "value": "Springfield", "confidence": 0.8},
        ])
    )
    h = FactExtractorHandler(
        FactExtractorConfig(backend="llm", llm_component_id="c.8b"),
        pg_pool=pg_pool,
        llm_handler_factory=_llm_factory(stub),
    )
    await h.on_activate(_ctx())
    sig = _signal(title="Acme Corp is located in Springfield")
    await h.transform(sig, _ctx())
    assert stub.calls == 1
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT predicate, value, confidence FROM facts "
            "WHERE lower(subject)='acme corp'"
        )
    assert row is not None
    assert row["predicate"] == "located in"
    assert row["value"] == "Springfield"
    assert row["confidence"] == pytest.approx(0.8)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llm_failure_degrades_not_drops(pg_pool):
    stub = _StubLLMHandler(RuntimeError("model down"))
    h = FactExtractorHandler(
        FactExtractorConfig(backend="llm", llm_component_id="c.8b"),
        pg_pool=pg_pool,
        llm_handler_factory=_llm_factory(stub),
    )
    await h.on_activate(_ctx())
    sig = _signal(title="something")
    out = await h.transform(sig, _ctx())
    assert out is sig  # never drops, never raises
    health = await h.health_check(_ctx())
    assert health.state == "degraded"


# ---------------------------------------------------------------------------
# SLM relationship-validation stage (opt-in, W3)
# ---------------------------------------------------------------------------


class _StubValidateSLM:
    """``complete``-shape SLM returning a fixed verdict batch.

    ``verdicts`` is a dict keyed by triple_index -> verdict-fields, so each
    candidate triple gets a deterministic valid/confidence/corrected_type
    answer regardless of the order the handler batches them.
    """

    def __init__(self, verdicts: dict[int, dict[str, Any]] | Exception) -> None:
        self._verdicts = verdicts
        self.calls = 0

    async def complete(self, prompt: str, system: str = "", json_schema=None):
        self.calls += 1
        if isinstance(self._verdicts, Exception):
            raise self._verdicts
        return {
            "verdicts": [
                {"triple_index": idx, **fields}
                for idx, fields in self._verdicts.items()
            ]
        }


def _validator(verdicts, **cfg_overrides) -> SLMRelationshipValidateHandler:
    cfg = SLMRelationshipValidateConfig(**cfg_overrides)
    return SLMRelationshipValidateHandler(cfg, slm=_StubValidateSLM(verdicts))


# Two REBEL triples: index 0 = the good one (Apple/Cupertino), index 1 = the
# bad one (Tim Cook/Apple via 'employer'). The slot order the validator sees
# matches _entities_to_triples output order. ``nonce`` suffixes the endpoint
# names so a test that WRITES through the shared session DB never collides
# with a sibling via the Phase B identical-triple dedupe (pass a nonce when
# the test asserts the row was written).
def _two_triple_entities(nonce: str = "") -> list[dict[str, Any]]:
    sfx = f" {nonce}" if nonce else ""
    return [
        {"class": "corporation", "text": f"Apple Inc.{sfx}",
         "predicate": "headquarters location", "confidence": 1.0},
        {"class": "location", "text": f"Cupertino{sfx}",
         "predicate": "headquarters location", "confidence": 1.0},
        {"class": "person", "text": f"Tim Cook{sfx}",
         "predicate": "employer", "confidence": 1.0},
        {"class": "corporation", "text": f"Hamas{sfx}",
         "predicate": "employer", "confidence": 1.0},
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slm_validate_drops_contradicted_relation(pg_pool):
    """Flag ON: the SLM marks slot-1 invalid → that fact is NOT written; the
    slot-0 valid relation survives. (drop_invalid defaults True.)"""
    validator = _validator({
        0: {"valid": True, "confidence": 0.95},
        1: {"valid": False, "confidence": 0.9, "reasoning": "hallucinated"},
    })
    h = FactExtractorHandler(
        FactExtractorConfig(backend="relation", slm_validate_relations=True),
        pg_pool=pg_pool,
        relationship_validator=validator,
    )
    await h.on_activate(_ctx())
    nonce = uuid4().hex[:8]
    apple, cupertino = f"Apple Inc. {nonce}", f"Cupertino {nonce}"
    cook, hamas = f"Tim Cook {nonce}", f"Hamas {nonce}"
    sig = _signal(entities=_two_triple_entities(nonce), title="apple")
    await h.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT subject, value FROM facts WHERE data->>'signal_id' = $1",
            str(sig.signal_id),
        )
    pairs = {(r["subject"], r["value"]) for r in rows}
    assert (apple, cupertino) in pairs  # valid kept
    assert (cook, hamas) not in pairs   # contradicted dropped
    # The surviving fact carries the SLM verdict in its data jsonb.
    async with pg_pool.acquire() as conn:
        data_row = await conn.fetchrow(
            "SELECT data FROM facts WHERE subject=$2 "
            "AND data->>'signal_id' = $1",
            str(sig.signal_id), apple,
        )
    data = json.loads(data_row["data"])
    assert data["slm_validated"] is True
    assert data["slm_valid"] is True
    assert validator._slm.calls == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slm_validate_low_confidence_dropped(pg_pool):
    """A triple the SLM marks valid but below the min-confidence floor is
    treated as invalid and dropped."""
    validator = _validator(
        {0: {"valid": True, "confidence": 0.2}},
        max_triples_per_signal=50,
    )
    h = FactExtractorHandler(
        FactExtractorConfig(
            backend="relation",
            slm_validate_relations=True,
            slm_validate_min_confidence=0.7,
        ),
        pg_pool=pg_pool,
        relationship_validator=validator,
    )
    await h.on_activate(_ctx())
    # Single triple (Apple/Cupertino).
    sig = _signal(entities=_two_triple_entities(uuid4().hex[:8])[:2], title="lowconf")
    await h.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT subject FROM facts WHERE data->>'signal_id' = $1",
            str(sig.signal_id),
        )
    assert rows == [], "below-floor relation must be dropped"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slm_validate_flag_not_drop_keeps_invalid(pg_pool):
    """drop_invalid=False: an invalid relation is KEPT but its fact carries
    the negative verdict (flag-not-drop)."""
    validator = _validator({0: {"valid": False, "confidence": 0.9}})
    h = FactExtractorHandler(
        FactExtractorConfig(
            backend="relation",
            slm_validate_relations=True,
            slm_validate_drop_invalid=False,
        ),
        pg_pool=pg_pool,
        relationship_validator=validator,
    )
    await h.on_activate(_ctx())
    sig = _signal(entities=_two_triple_entities(uuid4().hex[:8])[:2], title="flagkeep")
    await h.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data FROM facts WHERE data->>'signal_id' = $1",
            str(sig.signal_id),
        )
    assert row is not None, "flag-not-drop keeps the fact"
    data = json.loads(row["data"])
    assert data["slm_validated"] is True
    assert data["slm_valid"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slm_validate_corrected_type_retypes_predicate(pg_pool):
    """A valid verdict carrying corrected_type re-types the surviving fact's
    predicate."""
    validator = _validator({
        0: {"valid": True, "confidence": 0.9, "corrected_type": "located_in"},
    })
    h = FactExtractorHandler(
        FactExtractorConfig(backend="relation", slm_validate_relations=True),
        pg_pool=pg_pool,
        relationship_validator=validator,
    )
    await h.on_activate(_ctx())
    sig = _signal(entities=_two_triple_entities(uuid4().hex[:8])[:2], title="retype")
    await h.transform(sig, _ctx())
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT predicate FROM facts WHERE data->>'signal_id' = $1",
            str(sig.signal_id),
        )
    assert row is not None
    # Predicate is lowercased by the write path.
    assert row["predicate"] == "located_in"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slm_validate_failure_degrades_not_drops(pg_pool):
    """SLM down: triples flow through UNVALIDATED (degrade-not-drop) — facts
    are still written, and health flips degraded."""
    validator = _validator(RuntimeError("slm down"))
    h = FactExtractorHandler(
        FactExtractorConfig(backend="relation", slm_validate_relations=True),
        pg_pool=pg_pool,
        relationship_validator=validator,
    )
    await h.on_activate(_ctx())
    sig = _signal(entities=_two_triple_entities(uuid4().hex[:8])[:2], title="slmdown")
    out = await h.transform(sig, _ctx())
    assert out is sig
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT subject FROM facts WHERE data->>'signal_id' = $1",
            str(sig.signal_id),
        )
    assert rows, "SLM failure must NOT drop facts (degrade-not-drop)"
    health = await h.health_check(_ctx())
    assert health.state == "degraded"


@pytest.mark.asyncio
async def test_slm_validate_off_is_unchanged_no_validator_call():
    """Default (flag off): no validator is needed, no SLM hop, no behaviour
    change. Pure-unit — the relation backend reads triples off the signal."""
    cfg = FactExtractorConfig(backend="relation")
    assert cfg.slm_validate_relations is False
    # Constructing WITHOUT a validator is fine when the flag is off.
    h = FactExtractorHandler(cfg, pg_pool=_FakePool())
    triples = await h._extract_triples(
        _signal(entities=_two_triple_entities()), _ctx()
    )
    # _slm_validate_triples is never reached when the flag is off; prove the
    # gate by asserting the handler holds no validator and the flag is off.
    assert h._relationship_validator is None
    assert len(triples) == 2


def test_slm_validate_flag_requires_validator_at_construction():
    """no-stub: flag on without a wired validator raises loud at construction."""
    with pytest.raises(FactExtractorUnconfigured, match=r"relationship_validator"):
        FactExtractorHandler(
            FactExtractorConfig(backend="relation", slm_validate_relations=True),
            pg_pool=_FakePool(),
        )


class _FakePool:
    """Minimal pool double for construction-only / no-DB unit tests."""

    def acquire(self):  # pragma: no cover - not used in these unit tests
        raise AssertionError("DB not expected in this unit test")
