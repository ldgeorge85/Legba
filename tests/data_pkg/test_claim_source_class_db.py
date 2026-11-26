# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CLAIM source class on the real path (migrated Postgres, the
isolated test stack only).

Covered: the intake refuses a request born of a claim row's links
(``claim_link_refused``) unless the target host is an ordinary registered
source; the fact write path refuses a fact resting only on claim rows and
never takes a claim row's credibility; the SQL counting paths that carry the
not-a-claim predicate run against the real schema. The pure halves are in
``test_claim_source_class.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from legba.data.intake_store import SubmitRequest, submit_request
from legba.data.provenance.writes import (
    ClaimOnlyFactRefused,
    claim_lineage_counts,
    max_signal_credibility,
    refuse_claim_only_fact,
)
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources._contract import Signal
from legba.runtime.sinks.signal import write_canonical_signal

from tests.runtime._ingest_fakes import descriptor

pytestmark = pytest.mark.asyncio

CLAIMS = "source.test.c1_claims"
FEED = "source.test.c1_feed"
PAGES = "source.test.c1_pages"
FEED_HOST = "news.c1.example.org"
CLAIM_HOST = "tracker.c1.example.org"
IMPL = "legba.data.sources.scrapers.example_news:ExampleNewsScraper"
_IDS = (CLAIMS, FEED, PAGES)


def _claims_desc() -> SourceDescriptor:
    return SourceDescriptor.model_validate({
        "identity": {"id": CLAIMS, "name": "claims", "kind": "rss",
                     "schema_uri": "legba/source/1.0.0", "version": "c" * 16,
                     "owner": "test:c1", "created": "2026-11-14T00:00:00Z",
                     "state": "configured"},
        "scope": {"owner_tenant": "shared", "source_class": "claim", "access_class": "public",
                  "claim": {"claimant": "Example tracker", "verb": "listed"}},
        "cadence": {"schedule": {"raw": "0 * * * *"}},
        "config": {"url": {"factory_kind": "text", "raw": f"https://{CLAIM_HOST}/rss.xml"}},
    }, strict=False)


async def _put(conn, desc: Any, state: str) -> None:
    await conn.execute(
        "INSERT INTO source_descriptors (descriptor_id, version, schema_uri, is_head, kind, state, "
        "owner, name, body) VALUES ($1, $2, $3, TRUE, $4, $5, 'test:c1', $6, $7::jsonb)",
        desc.identity.id, desc.identity.version, desc.identity.schema_uri, desc.identity.kind,
        state, desc.identity.name, json.dumps(desc.model_dump(mode="json")),
    )


async def _signal(conn, source_id: str, *, claim: bool, credibility: float | None = None) -> uuid.UUID:
    payload: dict[str, Any] = {"title": f"row {uuid.uuid4().hex[:6]}"}
    if claim:
        payload["claim"] = {"source_class": "claim", "claimant": "Example tracker",
                            "claimant_from": "declared", "verb": "listed"}
    sig = Signal(source_id=source_id, payload=payload, source_credibility=credibility,
                 canonical_url=f"https://{CLAIM_HOST if claim else FEED_HOST}/{uuid.uuid4().hex}",
                 content_hash=uuid.uuid4().hex)
    written = await write_canonical_signal(conn, sig, source_version="c" * 16,
                                           owner_tenant="shared", access_class="public")
    assert written is not None
    return sig.signal_id


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with p.acquire() as c:
        await c.execute("DELETE FROM ingestion_requests WHERE pipeline_source_id = ANY($1::text[])",
                        list(_IDS))
        await c.execute("DELETE FROM signals WHERE source_id = ANY($1::text[])", list(_IDS))
        await c.execute("DELETE FROM source_descriptors WHERE descriptor_id = ANY($1::text[])",
                        list(_IDS))
        await _put(c, _claims_desc(), "configured")
        await _put(c, descriptor(FEED, "rss", {"url": f"https://{FEED_HOST}/feed"}), "active")
        await _put(c, descriptor(PAGES, "scraper",
                                 {"impl": IMPL, "seed_urls": [], "max_depth": 0}), "paused")
    yield p
    async with p.acquire() as c:
        await c.execute("DELETE FROM ingestion_requests WHERE pipeline_source_id = ANY($1::text[])",
                        list(_IDS))
        await c.execute("DELETE FROM signals WHERE source_id = ANY($1::text[])", list(_IDS))
        await c.execute("DELETE FROM source_descriptors WHERE descriptor_id = ANY($1::text[])",
                        list(_IDS))
    await p.close()


def _req(address: str, parent: uuid.UUID | None, **over: Any) -> SubmitRequest:
    fields: dict[str, Any] = {"reason": "agent_request", **over}
    return SubmitRequest(target_kind="address", address=address, pipeline_source_id=PAGES,
                         requested_by="test:c1", parent_signal_id=parent, **fields)


# ---------------------------------------------------------------------------
# The intake: links out of a claim row
# ---------------------------------------------------------------------------


async def test_a_link_out_of_a_claim_row_is_refused(pool):
    async with pool.acquire() as c:
        parent = await _signal(c, CLAIMS, claim=True)
    res = await submit_request(pool, _req("https://leak.c1.example.net/dump.zip", parent))
    assert res.state == "refused" and res.refusal_reason == "claim_link_refused"
    assert res.detail["claim_source"] == CLAIMS
    assert "links in a claim are not followed" in res.detail["why"]


async def test_a_link_to_the_claim_sources_own_host_is_refused(pool):
    """The claim source's own host is not an ORDINARY source: the archive of
    a claim post is refused too."""
    async with pool.acquire() as c:
        parent = await _signal(c, CLAIMS, claim=True)
    res = await submit_request(pool, _req(f"https://{CLAIM_HOST}/post/1", parent))
    assert res.refusal_reason == "claim_link_refused"


async def test_a_link_to_an_ordinary_registered_source_host_passes_this_rule(pool):
    async with pool.acquire() as c:
        parent = await _signal(c, CLAIMS, claim=True)
    res = await submit_request(pool, _req(f"https://www.{FEED_HOST}/story/7", parent))
    assert res.refusal_reason != "claim_link_refused"


async def test_a_followed_reference_from_a_claim_source_is_refused(pool):
    res = await submit_request(pool, _req(
        "https://leak.c1.example.net/x", None, reason="followed_reference",
        follow_source_id=CLAIMS, link_kind="anchor", budget_class="follow",
    ))
    assert res.state == "refused"
    # The follow policy check runs first (a claim source can declare none);
    # either refusal keeps the link unfollowed.
    assert res.refusal_reason in ("follow_not_allowed", "claim_link_refused")


async def test_an_ordinary_parent_is_unaffected(pool):
    async with pool.acquire() as c:
        parent = await _signal(c, FEED, claim=False)
    res = await submit_request(pool, _req("https://leak.c1.example.net/dump.zip", parent))
    assert res.refusal_reason != "claim_link_refused"


# ---------------------------------------------------------------------------
# The fact write path
# ---------------------------------------------------------------------------


async def test_a_fact_resting_only_on_claims_is_refused(pool):
    async with pool.acquire() as c:
        a = await _signal(c, CLAIMS, claim=True)
        b = await _signal(c, CLAIMS, claim=True)
        r = await _signal(c, FEED, claim=False)
        assert await claim_lineage_counts(c, [a, b, r]) == (2, 3)
        with pytest.raises(ClaimOnlyFactRefused, match="claim_only_lineage"):
            await refuse_claim_only_fact(c, predicate="attacked", derived_from=[a, b])
        # A fact ABOUT the claim is fine; so is one a non-claim row also backs.
        await refuse_claim_only_fact(c, predicate="claimed responsibility for", derived_from=[a])
        await refuse_claim_only_fact(c, predicate="attacked", derived_from=[a, r])
        await refuse_claim_only_fact(c, predicate="attacked", derived_from=[])


async def test_a_claim_row_is_never_a_facts_credibility_input(pool):
    async with pool.acquire() as c:
        claim = await _signal(c, CLAIMS, claim=True, credibility=0.95)
        plain = await _signal(c, FEED, claim=False, credibility=0.6)
        assert await max_signal_credibility(c, [claim, plain]) == pytest.approx(0.6)
        assert await max_signal_credibility(c, [claim]) is None


# ---------------------------------------------------------------------------
# The counting SQL runs on the real schema
# ---------------------------------------------------------------------------


async def test_counting_sql_with_the_claim_predicate_plans(pool):
    from legba.data.analysts import edge_qualification as EQ
    from legba.data.analysts.deterministic_handlers import evidence_archiver as EA
    from legba.data.analysts.deterministic_handlers import source_track_record as TR

    async with pool.acquire() as c:
        await c.execute("EXPLAIN " + TR._RECORDS_SQL, datetime.now(timezone.utc), None, None)
        await c.execute("EXPLAIN " + EA._SELECT_CANDIDATES_SQL, 24, 0.5, 3, 10)
        await c.execute("EXPLAIN " + EQ.scored_pool_sql(status_filter="pe.status = 'pending'"))
        await c.execute("EXPLAIN " + EQ.POOL_SCORING_SQL.format(status_filter="pe.status = 'pending'"))


# ---------------------------------------------------------------------------
# One hop more: a fact resting on a finding that rested only on claims
# ---------------------------------------------------------------------------


async def _finding(c, citations: list[dict[str, Any]]) -> uuid.UUID:
    fid = uuid.uuid4()
    await c.execute(
        "INSERT INTO analyst_outputs (id, kind, title, body, confidence, data, analyst_id, schema_uri) "
        "VALUES ($1, 'finding', 'c2 finding', '', 0.6, $2::jsonb, 'unit.c2_test', "
        "'iglu:legba/finding/jsonschema/1-0-0')",
        fid, json.dumps({"data": {"citations": citations}}),
    )
    return fid


async def test_a_fact_resting_on_a_claim_only_finding_is_refused(pool):
    from legba.data.provenance.writes import _CLAIM_ONLY_FINDINGS_SQL

    stamp = {"source_class": "claim", "claimant": "Example tracker",
             "claimant_from": "declared", "verb": "listed"}
    async with pool.acquire() as c:
        try:
            a = await _signal(c, CLAIMS, claim=True)
            r = await _signal(c, FEED, claim=False)
            only_claims = await _finding(c, [{"marker": "[1]", "signal_id": str(a), "claim": stamp}])
            mixed = await _finding(c, [{"marker": "[1]", "signal_id": str(a), "claim": stamp},
                                       {"marker": "[2]", "signal_id": str(r)}])
            no_signals = await _finding(c, [{"marker": "[[ref:1]]", "ref_kind": "finding"}])
            with pytest.raises(ClaimOnlyFactRefused, match="through a finding"):
                await refuse_claim_only_fact(c, predicate="attacked", derived_from=[only_claims])
            with pytest.raises(ClaimOnlyFactRefused):
                await refuse_claim_only_fact(c, predicate="attacked", derived_from=[only_claims, a])
            # A finding a non-claim row also backs, a finding citing no signal,
            # a non-claim signal beside it, and a fact ABOUT the claim all pass.
            await refuse_claim_only_fact(c, predicate="attacked", derived_from=[mixed])
            await refuse_claim_only_fact(c, predicate="attacked", derived_from=[no_signals])
            await refuse_claim_only_fact(c, predicate="attacked", derived_from=[only_claims, r])
            await refuse_claim_only_fact(c, predicate="claimed responsibility for",
                                         derived_from=[only_claims])
            await c.execute("EXPLAIN " + _CLAIM_ONLY_FINDINGS_SQL, [a])
        finally:
            await c.execute("DELETE FROM analyst_outputs WHERE analyst_id = 'unit.c2_test'")


async def test_the_poll_record_carries_the_claim_hygiene_counts(pool):
    from legba.runtime.source_poll_outcome import write_poll_outcome

    async with pool.acquire() as c:
        try:
            await write_poll_outcome(
                c, source_id=CLAIMS, source_version="c" * 16, owner_tenant="shared",
                outcome="success", health_state=None, capped=False, signals_written=2,
                error=None, claim_hygiene={"rows": 2, "url_to_host": 3, "email": 1},
            )
            await write_poll_outcome(
                c, source_id=FEED, source_version="f" * 16, owner_tenant="shared",
                outcome="success", health_state=None, capped=False, signals_written=1, error=None,
            )
            rows = {r["source_id"]: r["claim_hygiene"] for r in await c.fetch(
                "SELECT source_id, claim_hygiene FROM source_poll_outcomes "
                "WHERE source_id = ANY($1::text[])", [CLAIMS, FEED])}
            got = rows[CLAIMS]
            assert (json.loads(got) if isinstance(got, str) else got) == {
                "rows": 2, "url_to_host": 3, "email": 1}
            assert rows[FEED] is None
        finally:
            await c.execute("DELETE FROM source_poll_outcomes WHERE source_id = ANY($1::text[])",
                            [CLAIMS, FEED])
