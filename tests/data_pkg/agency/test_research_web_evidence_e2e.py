# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-A end-to-end — ``web_evidence`` through the REAL agency gate.

Every test here drives ``Agency.run_pack_tool`` against a fresh migrated
Postgres: the three-way resolve, the governor, the invocation ledger, the
handler dispatch and the settle all really run, and the rows it asserts on are
rows the tool actually wrote. Nothing about the gate, the licence gate, the
egress guard or the write path is mocked. What IS stubbed is the two things a
test rig cannot have: the search PROVIDER (a canned ``SearchResponse``) and the
open web (an ``httpx.MockTransport`` serving robots.txt and one article).

What it proves, in the order the spec's gates are numbered:

  G1  every landed row carries ``retrieval_origin = web_search:<component>``
  G2  flag ``off`` ⇒ a loud refusal and ZERO rows written
  G3  the licence gate: cleared → CAS bytes; unreviewed → teaser, NO bytes,
      NO full text in payload.text; forbidden → never fetched, no row at all
  G4  the ceiling holds, through ``cited_mass.v1``'s own arithmetic
  G5  never auto-ground — the research source binds no pipeline because it has
      no descriptor
  G8  a full-text hit comes back as a row with a REAL body (no title-only
      citation can be minted from it)
  G11 ``web_access`` still writes nothing
  plus: ON CONFLICT replay, a degraded provider landing nothing, robots
  refusal, and the SSRF guard refusing a metadata address before connect.

The final section (2026-09-07) walks the DISPATCHED RUN end to end — the real
dispatch write, the real backlog ranking, the real deps-builder grounding hook,
the real ``inline_target`` run and GATHER loop, then this file's own real gate
and write path — because the 03:37Z failure was a chain, not a function.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from legba.data.analysts.agency import (
    GLOBAL_SCOPE,
    Agency,
    AgencyToolBinding,
    TargetScopeView,
    ToolCall,
    ToolContext,
    WritebackContext,
)
from legba.data.analysts.agency import research_tools, robots as robots_mod
from legba.data.provenance import AnalystContext
from legba.data.research_flag import (
    RESEARCH_EVIDENCE_ENV,
    RESEARCH_OFF,
    RESEARCH_SUBSTRATE,
)
from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.data.stack.search import SearchResponse, SearchResult


# The ``restore_source_credibility_seed`` fixture (tests/data_pkg/conftest.py)
# re-applies the baseline source_credibility rows onto an open connection —
# see its docstring for why (09-05 merge-wave: this file's reset fixture runs
# an unscoped ``DELETE FROM source_credibility`` that commits on the shared
# test DB).

pytestmark = [pytest.mark.asyncio]

_ARTICLE_HTML = (
    "<html><head><title>Fuel embargo</title></head><body><article>"
    "<p>Fuel deliveries into Bamako fell by four fifths this week as JNIM "
    "roadblocks held on the three southern corridors, according to two "
    "haulage operators reached by telephone in the capital.</p>"
    "<p>The national fuel importers' association said reserves would last "
    "eleven days at current rationing levels.</p>"
    "</article></body></html>"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dsn(migrated_pg):
    """The test DB DSN. NOT an async fixture on purpose: every pool and
    connection this file opens lives ENTIRELY inside the test coroutine's own
    event loop (see ``_pool``), so pytest-asyncio never has to tear an asyncpg
    pool down from a stale loop — the cross-loop finalizer hang that a
    module-level ``async def pool`` fixture triggers under asyncio_mode=auto."""
    return migrated_pg.dsn


@asynccontextmanager
async def _pool(dsn):
    """A short-lived pool, opened and closed within the caller's loop."""
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        yield p
    finally:
        await p.close()


async def _clean(dsn):
    conn = await asyncpg.connect(dsn)
    try:
        # The host ledger's new column (migration 0192) must be applied.
        assert await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_name = "
            "'source_credibility' AND column_name = 'license_class'"
        )
        await conn.execute("DELETE FROM evidence_archive")
        await conn.execute("DELETE FROM signals")
        await conn.execute("DELETE FROM source_credibility")
        await conn.execute("DELETE FROM target_descriptors")
    finally:
        await conn.close()


@asynccontextmanager
async def _conn(dsn):
    """A single connection, opened and closed within the caller's loop."""
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(autouse=True)
async def _reset_db(dsn, restore_source_credibility_seed):
    """Scrub the tables this file owns BEFORE each test, and RESTORE the
    canonical ``source_credibility`` seed AFTER — the unscoped delete in
    ``_clean`` commits on the shared test DB, so leaving it wiped poisons every
    later ``source_credibility`` test in the suite (the 09-05 merge-wave
    ordering pollution)."""
    await _clean(dsn)
    yield
    async with _conn(dsn) as conn:
        await restore_source_credibility_seed(conn)


@pytest.fixture
def archive_root(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(root))
    return root


@pytest.fixture(autouse=True)
def substrate_regime(monkeypatch):
    """Default every test to the WRITE-ON, desks-excluded rung."""
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_SUBSTRATE)


@pytest.fixture(autouse=True)
def _no_dns_egress(monkeypatch):
    """Skip DNS in the SSRF pre-check for the fictional ``.test`` hosts.

    The tool's SSRF pre-check (``assert_public_host``) resolves a hostname
    before connect. The reserved ``.test`` domain does not resolve, so under the
    REAL helper every hit would hang on a DNS timeout and then be wrongly
    dropped as egress-blocked. This shim keeps the SECURITY-critical behaviour —
    an IP LITERAL that is private / loopback / link-local / metadata is still
    refused (the ``169.254.169.254`` test depends on it) — and only skips the
    getaddrinfo leg for a non-literal test host, so the fetch path runs against
    the mock transport instead of the network.
    """
    import ipaddress

    from legba.data.sources._egress import EgressBlockedError, _ip_blocked

    def _fake(host, port):
        if not host:
            raise EgressBlockedError("egress blocked: empty host")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            return  # a hostname — the mock transport serves it; no DNS here
        if _ip_blocked(literal):
            raise EgressBlockedError(f"egress blocked: {host} is a non-public address")

    monkeypatch.setattr(research_tools, "assert_public_host", _fake)


def _pack() -> ActionPack:
    return ActionPack.model_validate(
        {
            "identity": {
                "id": "research", "name": "Outbound Research",
                "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
                "state": "active", "owner": "research_program",
                "created": datetime.now(timezone.utc).isoformat(),
            },
            "tools": [{"name": "web_evidence", "config": {"timeout_seconds": 5}}],
            "governor": {"budget_account": "research"},
        },
        strict=False,
    )


class _StubProvider:
    """A canned ``SearchResponse``. The provider is the ONE thing a test rig
    cannot have; everything downstream of it is real."""

    component_version = "feedfacefeedface"
    # A real bound handler carries its resolved component id; the tool reads it
    # to build ``retrieval_origin`` when the pack declares no explicit route
    # (the test pack keeps its config minimal).
    component_id = "search.searxng.local"

    def __init__(self, response: SearchResponse) -> None:
        self.response = response
        self.queries: list[str] = []

    async def search(self, query, *, limit=5, params=None):
        self.queries.append(query)
        return self.response


def _response(*results: SearchResult, **over) -> SearchResponse:
    body = {
        "query": "mali fuel embargo",
        "results": list(results),
        "provider": "search.searxng.local",
        "subprovider": "searxng",
    }
    body.update(over)
    return SearchResponse(**body)


def _hit(url: str, **over) -> SearchResult:
    body = {
        "url": url,
        "title": "Fuel embargo tightens on the Bamako corridors",
        "snippet": "Deliveries fell sharply as roadblocks held.",
        "engine": "duckduckgo",
        "rank": 1,
        "published_at": "2026-09-04T06:00:00Z",
    }
    body.update(over)
    return SearchResult(**body)


@pytest.fixture
def web(monkeypatch):
    """Serve robots.txt + article pages through a mock transport.

    Installed on BOTH egress sites the tool uses (the robots fetch and the page
    fetch), each of which independently opens a ``guarded_async_client``.
    """
    seen: list[str] = []

    def _install(*, robots_body: str = "User-agent: *\nAllow: /\n",
                 page_status: int = 200, page_body: str = _ARTICLE_HTML,
                 robots_status: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(robots_status, text=robots_body)
            return httpx.Response(
                page_status, text=page_body,
                headers={"content-type": "text/html; charset=utf-8"},
            )

        def fake_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return httpx.AsyncClient(**kwargs)

        monkeypatch.setattr(research_tools, "guarded_async_client", fake_client)
        monkeypatch.setattr(robots_mod, "guarded_async_client", fake_client)
        return seen

    return _install


async def _run(dsn, provider, *, args=None, writeback=True):
    """Drive one call through the REAL gate and return the AgencyOutcome.

    Opens its OWN pool inside the caller's event loop so ``writeback.pg_pool``
    (which ``web_evidence`` re-acquires from) is bound to the same loop as the
    test — no cross-loop teardown.
    """
    async with _pool(dsn) as pool:
        ctx = ToolContext(search=provider)
        if writeback:
            ctx.writeback = WritebackContext(
                pg_pool=pool,
                analyst_ctx=AnalystContext(
                    analyst_id="corpus_researcher", analyst_version="b" * 16,
                    run_id=uuid4(), target_id=None, target_version=None,
                ),
            )
        call = ToolCall(
            pack_id="research", tool_name="web_evidence",
            args=args or {"query": "mali fuel embargo"},
            requested_by="analyst::corpus_researcher",
            budget_account=f"acct-{uuid4().hex[:8]}",
        )
        async with _conn(dsn) as conn:
            return await Agency().run_pack_tool(
                conn, pack=_pack(), call=call,
                analyst_grants=[ActionPackRef(pack_id="research")],
                target_allows=[ActionPackRef(pack_id="research")],
                scope=TargetScopeView(target_id="__global__"), ctx=ctx,
            )


async def _signals(dsn):
    conn = await asyncpg.connect(dsn)
    try:
        # A research write is a NEW row keyed by (provider, canonical_url, run_id)
        # (`research_evidence`: `ON CONFLICT (id) DO NOTHING`), so a URL the desk
        # already held ends up with TWO rows — the curated one and the research
        # one. Callers key a dict by canonical_url and keep the LAST row, and
        # `ORDER BY canonical_url` alone leaves ties in heap order, which the
        # planner is free to change as the shared test table bloats over a full
        # suite run (2026-09-26, seed 654624946: the curated row came last and
        # `payload["research"]` raised KeyError; the file alone passes under the
        # same seed). Order the research row after the curated one explicitly.
        return await conn.fetch(
            "SELECT * FROM signals "
            "ORDER BY canonical_url, (payload ? 'research'), fetched_at, id"
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# G2 — the flag off: granted, and it REFUSES
# ---------------------------------------------------------------------------


async def test_flag_off_refuses_loudly_and_writes_nothing(dsn, monkeypatch):
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_OFF)
    provider = _StubProvider(_response(_hit("https://example.org/a")))
    outcome = await _run(dsn, provider)

    # The GATE admitted — the pack is effective. The TOOL refused.
    assert outcome.admitted is True
    assert outcome.tool_result.status == "failed"
    assert "research_evidence_disabled" in outcome.tool_result.error
    assert RESEARCH_EVIDENCE_ENV in outcome.tool_result.error
    # NO query was issued and NO row was written: the whole write path is dark.
    assert provider.queries == []
    async with _conn(dsn) as _c:
        assert await _c.fetchval("SELECT count(*) FROM signals") == 0
    async with _conn(dsn) as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM signals WHERE retrieval_origin IS NOT NULL"
        ) == 0
    # …and it is a CONFIGURATION statement, never an absence statement.
    assert "not a statement about the web" in outcome.tool_result.error


async def test_writeback_unavailable_is_a_hard_failure_with_no_deferral(dsn):
    provider = _StubProvider(_response(_hit("https://example.org/a")))
    outcome = await _run(dsn, provider, writeback=False)
    assert outcome.tool_result.status == "failed"
    assert "research_writeback_unavailable" in outcome.tool_result.error
    # NO deferral: waiting cannot fix a binding gap. "The write path is missing"
    # and "the web has nothing" must never share a wire shape.
    assert "deferral" not in outcome.tool_result.output
    assert provider.queries == []


# ---------------------------------------------------------------------------
# G1 / G3 (teaser) / G4 — the default path: an unreviewed host
# ---------------------------------------------------------------------------


async def test_unreviewed_host_lands_a_teaser_signal_with_no_bytes(dsn, web, archive_root):
    seen = web()
    provider = _StubProvider(_response(_hit("https://unreviewed.test/story")))
    outcome = await _run(dsn, provider)

    assert outcome.tool_result.status == "completed"
    out = outcome.tool_result.output
    assert out["landed"] == 1 and out["teaser"] == 1 and out["archived"] == 0
    # NOT numbered — a bodyless [N] would demote the finding.
    assert out["rows"] == []
    assert len(out["teaser_hits"]) == 1
    assert out["teaser_hits"][0]["depth_reason"] == "license_unreviewed"

    rows = await _signals(dsn)
    assert len(rows) == 1
    row = rows[0]
    # G1 — THE LABEL. The seam migration 0112 built, finally written.
    assert row["retrieval_origin"] == "web_search:search.searxng.local"
    assert row["source_id"] == "source.research.searxng_local"
    assert row["produced_by_kind"] == "research"
    # An unreviewed teaser must NOT claim evidence_hold, and holds no bytes.
    assert row["retention_class"] == "reference_only"
    assert row["object_ref"] is None
    # Indexable like any other signal — corpus_indexer's partial index scans
    # exactly this NULL, so the OpenSearch retrieval_origin facet fills itself.
    assert row["indexed_at"] is None
    # G4 — the ceiling, on the row.
    sal = json.loads(row["salience"]) if isinstance(row["salience"], str) else row["salience"]
    assert sal["magnitude"] == 0.5 and sal["authority"] == "unknown"
    assert row["source_credibility"] == pytest.approx(0.5)

    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    # G3's anti-laundering clause: the teaser body IS the snippet, and the
    # article text never touched this row.
    assert payload["text"] == "Deliveries fell sharply as roadblocks held."
    assert "haulage operators" not in json.dumps(payload)
    assert payload["research"]["depth"] == "teaser"
    # NO page was ever fetched — not even robots, because depth was decided
    # from the licence before any connection was considered.
    assert seen == []

    # The sidecar records WHY the bytes were withheld. This status has never
    # fired in 70,836 live rows; here is its first honest occasion.
    async with _conn(dsn) as conn:
        arch = await conn.fetchrow("SELECT * FROM evidence_archive")
    assert arch["status"] == "skipped_license_unreviewed"
    assert arch["retrieval_origin"] == "web_search:search.searxng.local"
    assert arch["object_ref"] is None and arch["sha256"] is None
    assert not list(archive_root.rglob("*")) if archive_root.exists() else True


async def test_landed_row_contributes_zero_cited_mass(dsn, web):
    """G4 through the REAL arithmetic, over the REAL landed row."""
    from legba.data.analysts.signal_salience import salience_over_ids

    web()
    await _run(dsn, _StubProvider(_response(_hit("https://unreviewed.test/a"))))
    rows = await _signals(dsn)
    sal = rows[0]["salience"]
    sal = json.loads(sal) if isinstance(sal, str) else sal
    sid = str(rows[0]["id"])
    block = salience_over_ids([sid], {sid: sal["magnitude"]})
    assert block["cited_mass"] == 0.0, (
        "an uncorroborated research signal must contribute EXACTLY zero mass — "
        "it can be read and cited, it can never crown the Morning Read"
    )
    assert block["n_cited_scored"] == 1  # still counted, still visible


# ---------------------------------------------------------------------------
# THE EMPTY-TEASER GUARD — a teaser hit with no snippet text is SKIPPED, not
# landed as a zero-text signals row (live incident 2026-09-06: run
# 46b41612-d8e6-45a1-bb16-24380a0becd9 landed an en.m.wikipedia.org hit with
# payload.text == '' — SearXNG returned the result with an empty snippet).
# ---------------------------------------------------------------------------


async def test_empty_snippet_teaser_hit_is_skipped_not_landed(dsn, web, archive_root):
    """An unreviewed host whose provider snippet is empty has NO text to land
    at teaser depth — no bytes are ever fetched there, so the snippet IS the
    text. The hit must be skipped, not written as a zero-text signals row, and
    counted honestly instead of silently inflating the landed count."""
    seen = web()
    provider = _StubProvider(
        _response(_hit("https://unreviewed.test/empty", snippet=""))
    )
    outcome = await _run(dsn, provider)

    assert outcome.tool_result.status == "completed"
    out = outcome.tool_result.output
    assert out["landed"] == 0 and out["teaser"] == 0
    assert out["skipped_empty_snippet"] == 1
    assert out["rows"] == [] and out["teaser_hits"] == []
    assert out["refused"] == [
        {
            "url": "https://unreviewed.test/empty",
            "host": "unreviewed.test",
            "reason": "empty_snippet",
            "depth_reason": "license_unreviewed",
        }
    ]

    # NOTHING landed: no signals row, no evidence_archive sidecar.
    assert await _signals(dsn) == []
    async with _conn(dsn) as conn:
        assert await conn.fetchval("SELECT count(*) FROM evidence_archive") == 0
    # Teaser depth never connects — an empty snippet doesn't change that.
    assert seen == []
    assert not list(archive_root.rglob("*")) if archive_root.exists() else True


async def test_whitespace_only_snippet_teaser_hit_is_skipped(dsn, web):
    """A snippet of pure whitespace is likewise zero text after normalisation
    and must be skipped exactly like a truly empty one."""
    seen = web()
    provider = _StubProvider(
        _response(_hit("https://unreviewed.test/whitespace", snippet="   \n\t  "))
    )
    outcome = await _run(dsn, provider)

    out = outcome.tool_result.output
    assert out["landed"] == 0 and out["teaser"] == 0
    assert out["skipped_empty_snippet"] == 1
    assert out["refused"][0]["reason"] == "empty_snippet"
    assert out["refused"][0]["url"] == "https://unreviewed.test/whitespace"
    assert await _signals(dsn) == []
    assert seen == []


async def test_real_snippet_teaser_hit_still_lands_byte_identically(dsn, web, archive_root):
    """GOLDEN: a non-empty snippet at teaser depth lands exactly as it did
    before the empty-teaser guard existed — this path is untouched."""
    seen = web()
    provider = _StubProvider(_response(_hit("https://unreviewed.test/real")))
    outcome = await _run(dsn, provider)

    out = outcome.tool_result.output
    assert out["landed"] == 1 and out["teaser"] == 1
    assert out["skipped_empty_snippet"] == 0
    assert out["refused"] == []

    rows = await _signals(dsn)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["text"] == "Deliveries fell sharply as roadblocks held."
    assert payload["summary"] == "Deliveries fell sharply as roadblocks held."
    assert payload["research"]["depth"] == "teaser"
    assert payload["research"]["depth_reason"] == "license_unreviewed"
    assert seen == []


async def test_mixed_empty_and_real_snippets_lands_two_skips_one(dsn, web):
    """One empty-snippet hit alongside two real ones: the two real hits land,
    and exactly the empty one is skipped and counted — the honest mixed case."""
    web()
    provider = _StubProvider(
        _response(
            _hit("https://unreviewed.test/a", snippet="First real snippet."),
            _hit("https://unreviewed.test/b", snippet=""),
            _hit("https://unreviewed.test/c", snippet="Third real snippet."),
        )
    )
    outcome = await _run(dsn, provider)

    out = outcome.tool_result.output
    assert out["landed"] == 2 and out["teaser"] == 2
    assert out["skipped_empty_snippet"] == 1
    assert [r["url"] for r in out["refused"]] == ["https://unreviewed.test/b"]

    urls = {r["canonical_url"] for r in await _signals(dsn)}
    assert urls == {"https://unreviewed.test/a", "https://unreviewed.test/c"}


async def test_only_hit_is_empty_snippet_reports_honestly_no_exception(dsn, web):
    """If the ONE hit in a run is an empty-snippet teaser, the run still
    completes and reports 0 landed / 1 skipped — never an exception, and never
    a fabricated 'no results' outcome."""
    web()
    provider = _StubProvider(
        _response(_hit("https://unreviewed.test/only", snippet=""))
    )
    outcome = await _run(dsn, provider)

    assert outcome.tool_result.status == "completed"
    out = outcome.tool_result.output
    assert out["landed"] == 0 and out["skipped_empty_snippet"] == 1
    assert out["rows"] == [] and out["teaser_hits"] == []
    assert await _signals(dsn) == []


async def test_empty_snippet_at_full_text_depth_is_untouched_by_the_guard(
    dsn, web, archive_root,
):
    """A licence-cleared host with an empty provider snippet is NOT subject to
    the empty-teaser guard: bytes are fetched, the text comes from extraction
    (never the snippet), so this still archives and lands full text."""
    await _clear_host(dsn, "cleared.test")
    web()
    provider = _StubProvider(
        _response(_hit("https://cleared.test/story", snippet=""))
    )
    outcome = await _run(dsn, provider)

    out = outcome.tool_result.output
    assert out["archived"] == 1 and out["teaser"] == 0
    assert out["skipped_empty_snippet"] == 0
    assert len(out["rows"]) == 1
    assert "haulage operators" in out["rows"][0]["source"]["raw_body"]


# ---------------------------------------------------------------------------
# G3 (cleared) / G8 — a licence-cleared host archives and becomes citable
# ---------------------------------------------------------------------------


async def _clear_host(dsn, host: str, license_class: str = "cc_by", score=0.9):
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO source_credibility (source_host, score, scored_by, "
            "license_class) VALUES ($1, $2, 'operator.test', $3)",
            host, score, license_class,
        )
    finally:
        await conn.close()


async def test_cleared_host_archives_bytes_and_returns_a_citable_row(dsn, web, archive_root):
    await _clear_host(dsn, "cleared.test")
    seen = web()
    provider = _StubProvider(_response(_hit("https://cleared.test/story")))
    outcome = await _run(dsn, provider)

    out = outcome.tool_result.output
    assert out["archived"] == 1 and out["teaser"] == 0
    # G8 — ONE numbered row, carrying a REAL body. A title-only citation cannot
    # be minted from this shape.
    assert len(out["rows"]) == 1
    entry = out["rows"][0]
    assert "haulage operators" in entry["source"]["raw_body"]
    assert entry["source"]["research_depth"] == "full_text"
    assert entry["source"]["retrieval_origin"] == "web_search:search.searxng.local"

    # robots.txt was asked FIRST, then the page.
    assert seen[0].endswith("/robots.txt")
    assert seen[1] == "https://cleared.test/story"

    rows = await _signals(dsn)
    row = rows[0]
    assert row["object_ref"].startswith("cas:sha256/")
    # The stamp raised retention — correct here precisely BECAUSE bytes landed.
    assert row["retention_class"] == "evidence_hold"
    # ARCHIVE-THEN-INDEX: the dirty marker is re-nulled so the corpus re-indexes
    # the row now that it has a body.
    assert row["indexed_at"] is None
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert "haulage operators" in payload["text"]
    assert "haulage operators" in payload["archived_text"]
    assert payload["research"]["depth"] == "full_text"
    assert payload["research"]["extract_source"] == "legba_trafilatura"
    # …and the BYTES are really on disk, content-addressed.
    digest = row["object_ref"].split("/")[-1]
    stored = archive_root / digest[:2] / digest
    assert stored.exists() and stored.read_bytes() == _ARTICLE_HTML.encode()

    async with _conn(dsn) as conn:
        arch = await conn.fetchrow("SELECT * FROM evidence_archive")
    assert arch["status"] == "archived" and arch["sha256"] == digest
    assert arch["text_extracted"] is True


async def test_forbidden_host_is_never_fetched_and_lands_no_row(dsn, web):
    await _clear_host(dsn, "walled.test", license_class="anti_ai_walled")
    seen = web()
    outcome = await _run(dsn, _StubProvider(_response(_hit("https://walled.test/a"))))
    out = outcome.tool_result.output
    assert out["refused_license"] == 1
    assert out["landed"] == 0 and out["teaser"] == 0
    assert out["refused"][0]["reason"] == "license_forbids"
    # Not one connection — not even robots.txt. The refusal is PRE-CONNECT,
    # which the archiver's retention-time refusal never was.
    assert seen == []
    assert await _signals(dsn) == []
    async with _conn(dsn) as conn:
        assert await conn.fetchval("SELECT count(*) FROM evidence_archive") == 0


async def test_robots_disallow_downgrades_a_cleared_host_to_teaser(dsn, web, archive_root):
    await _clear_host(dsn, "cleared.test")
    seen = web(robots_body="User-agent: *\nDisallow: /\n")
    outcome = await _run(dsn, _StubProvider(_response(_hit("https://cleared.test/a"))))
    out = outcome.tool_result.output
    assert out["refused_robots"] == 1
    assert out["archived"] == 0 and out["teaser"] == 1
    assert out["rows"] == []
    assert out["teaser_hits"][0]["depth_reason"] == "robots_disallowed"
    assert out["robots"]["disallowed"] == 1
    # robots.txt was fetched; the PAGE was not.
    assert [u for u in seen if not u.endswith("/robots.txt")] == []
    rows = await _signals(dsn)
    assert rows[0]["object_ref"] is None


async def test_unreachable_robots_fails_closed(dsn, web):
    await _clear_host(dsn, "cleared.test")
    web(robots_status=503)
    outcome = await _run(dsn, _StubProvider(_response(_hit("https://cleared.test/a"))))
    out = outcome.tool_result.output
    assert out["refused_robots"] == 1 and out["archived"] == 0
    assert out["robots"]["unreachable"] == 1


async def test_ssrf_hit_is_refused_before_connect_and_lands_nothing(dsn, web):
    """The guard already does this; the test proves R-A did not bypass it."""
    seen = web()
    outcome = await _run(
        dsn, _StubProvider(_response(
            _hit("http://169.254.169.254/latest/meta-data/"),
            _hit("https://unreviewed.test/ok"),
        )),
    )
    out = outcome.tool_result.output
    assert out["refused_egress"] == 1
    assert out["refused"][0]["reason"] == "egress_blocked"
    rows = await _signals(dsn)
    assert [r["canonical_url"] for r in rows] == ["https://unreviewed.test/ok"]
    assert not any("169.254" in u for u in seen)


async def test_page_fetch_failure_degrades_to_teaser_not_a_lost_row(dsn, web):
    await _clear_host(dsn, "cleared.test")
    web(page_status=502)
    outcome = await _run(dsn, _StubProvider(_response(_hit("https://cleared.test/a"))))
    out = outcome.tool_result.output
    assert out["fetch_failed"] == 1 and out["teaser"] == 1 and out["archived"] == 0
    # The hit is still EVIDENCE — the snippet lands, tagged, with the reason.
    rows = await _signals(dsn)
    payload = rows[0]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["research"]["depth_reason"] == "fetch_failed"
    assert payload["research"]["fetch_error"].startswith("HTTPStatusError")


# ---------------------------------------------------------------------------
# Idempotence, degradation, and the properties that must stay free
# ---------------------------------------------------------------------------


async def test_same_url_twice_in_one_run_lands_one_row(dsn, web):
    """A multi-round GATHER re-fetching the same URL must not duplicate it.

    The id is ``uuid5(provider ‖ url ‖ run)``, so the second write hits
    ``ON CONFLICT (id) DO NOTHING`` — idempotence by construction, not by a
    pre-SELECT that races itself.
    """
    web()
    provider = _StubProvider(_response(
        _hit("https://unreviewed.test/dup"), _hit("https://unreviewed.test/dup"),
    ))
    outcome = await _run(dsn, provider)
    out = outcome.tool_result.output
    assert out["landed"] == 1 and out["duplicate"] == 1
    assert len(await _signals(dsn)) == 1


async def test_degraded_empty_lands_nothing_and_defers(dsn, web):
    web()
    provider = _StubProvider(_response(
        degraded=True, degraded_detail="unresponsive_engines: brave, duckduckgo",
    ))
    outcome = await _run(dsn, provider)
    assert outcome.tool_result.status == "failed"
    assert "search_degraded_no_results" in outcome.tool_result.error
    assert "UNKNOWN, not absence" in outcome.tool_result.error
    out = outcome.tool_result.output
    assert out["landed"] == 0 and out["rows"] == []
    assert out["deferral"]["defer"] is True
    assert await _signals(dsn) == []


async def test_never_auto_grounds_the_research_source_binds_no_pipeline(dsn, web):
    """G5. Fact extraction is a per-SOURCE pipeline filter a descriptor binds.
    The research source has no descriptor, so it binds nothing — the property
    is free, and this assertion is what keeps it free."""
    web()
    await _run(dsn, _StubProvider(_response(_hit("https://unreviewed.test/a"))))
    rows = await _signals(dsn)
    async with _conn(dsn) as conn:
        # There is NO source_descriptors row for the synthetic id — deliberately.
        assert await conn.fetchval(
            "SELECT count(*) FROM source_descriptors WHERE descriptor_id LIKE "
            "'source.research.%'"
        ) == 0
        # …so no facts row can derive from a research signal.
        assert await conn.fetchval(
            "SELECT count(*) FROM facts WHERE $1::uuid = ANY(derived_from)",
            rows[0]["id"],
        ) == 0
    # And the write path never reaches fact extraction in the first place.
    import inspect

    from legba.data import research_evidence

    for mod in (research_evidence, research_tools):
        assert "fact_extractor" not in inspect.getsource(mod)


async def test_web_access_pack_is_untouched_and_still_writes_nothing(dsn):
    """G11. The auditor is R4's own instrument; its pack must not move."""
    import inspect

    from legba.data.analysts.agency import web_tools

    assert web_tools.WEB_ACCESS_TOOLS == ("web_fetch", "web_search")
    src = inspect.getsource(web_tools)
    assert "INSERT" not in src.upper().replace("INSERT INTO SIGNALS", "")
    assert "writeback" not in src
    # And a web_access call lands no row (here: the no-endpoint refusal).
    pack = ActionPack.model_validate(
        {
            "identity": {
                "id": "web_access", "name": "web", "schema_uri":
                "legba/action_pack/1.0.0", "version": "a" * 16, "state": "active",
                "owner": "s6_agency", "created": datetime.now(timezone.utc).isoformat(),
            },
            "tools": [{"name": "web_search"}],
        },
        strict=False,
    )
    call = ToolCall(
        pack_id="web_access", tool_name="web_search",
        args={"query": "x"}, budget_account=f"acct-{uuid4().hex[:8]}",
    )
    async with _conn(dsn) as conn:
        await Agency().run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[ActionPackRef(pack_id="web_access")],
            target_allows=[ActionPackRef(pack_id="web_access")],
            scope=TargetScopeView(target_id="t"), ctx=ToolContext(),
        )
        assert await conn.fetchval("SELECT count(*) FROM signals") == 0


async def test_clean_tool_failure_reason_reaches_the_receipt(dsn, monkeypatch):
    """F-13. A handler that RETURNS ``failed`` used to have its reason
    discarded — all 24 live ``web_search`` failures carry NULL error. One key,
    for every pack."""
    from legba.data import run_accounting

    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_OFF)
    token = run_accounting.bind_run_accounting()
    try:
        await _run(dsn, _StubProvider(_response()))
        calls = run_accounting.current_tool_calls()
    finally:
        run_accounting.reset_run_accounting(token)
    assert calls, "the receipt must record the call"
    last = calls[-1]
    assert last["status"] == "failed"
    assert "research_evidence_disabled" in last["error"]


# ---------------------------------------------------------------------------
# Dispatch — geo is inherited from the TARGET, never from the planner
# ---------------------------------------------------------------------------


async def test_geo_is_inherited_from_the_dispatching_questions_target(dsn, web):
    hyp = uuid4()
    async with _conn(dsn) as conn:
        await conn.execute(
            "INSERT INTO target_descriptors (descriptor_id, version, is_head, "
            "schema_uri, owner, name, body) "
            "VALUES ($1, $2, TRUE, 'legba/target/1.0.0', 'test', 'IL', $3::jsonb)",
            "country_watch_il", "c" * 16,
            json.dumps({"scope": {"geo": ["IL"], "tags": ["news"]}}),
        )
        await conn.execute(
            "INSERT INTO hypotheses (id, thesis, status, target_id) "
            "VALUES ($1, $2, 'open_question', $3)",
            hyp, "How likely is a change in the fuel corridor?", "country_watch_il",
        )
    web()
    outcome = await _run(
        dsn, _StubProvider(_response(_hit("https://unreviewed.test/il"))),
        args={"query": "israel fuel", "hypothesis_id": str(hyp)},
    )
    assert outcome.tool_result.status == "completed"
    rows = await _signals(dsn)
    assert rows[0]["geo"] == ["IL"]
    assert sorted(rows[0]["tags"]) == ["news", "research"]
    prov = rows[0]["raw_provenance"]
    prov = json.loads(prov) if isinstance(prov, str) else prov
    assert prov["dispatch"]["hypothesis_id"] == str(hyp)
    assert prov["dispatch"]["target_id"] == "country_watch_il"
    assert prov["dispatch"]["kind"] == "open_question"
    assert "fuel corridor" in prov["dispatch"]["bounded_question"]


async def test_a_planner_supplied_geo_cannot_reach_a_desk(dsn, web):
    """The planner's arguments are not a reachability claim. A made-up geo
    argument is ignored outright — the ONLY geo is the target descriptor's."""
    web()
    await _run(
        dsn, _StubProvider(_response(_hit("https://unreviewed.test/x"))),
        args={"query": "q", "geo": ["IL"], "target_id": "country_watch_il"},
    )
    rows = await _signals(dsn)
    assert rows[0]["geo"] == []


async def test_self_selected_run_has_novelty_scope_none(dsn, web):
    web()
    await _run(dsn, _StubProvider(_response(_hit("https://unreviewed.test/x"))))
    rows = await _signals(dsn)
    payload = rows[0]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    novelty = payload["research"]["novelty"]
    assert novelty["scope"] == "none"
    assert novelty["novel"] is None
    assert novelty["version"] == "novelty.v1"


async def test_novelty_is_measured_against_the_dispatching_targets_slice(dsn, web):
    hyp = uuid4()
    async with _conn(dsn) as conn:
        await conn.execute(
            "INSERT INTO target_descriptors (descriptor_id, version, is_head, "
            "schema_uri, owner, name, body) "
            "VALUES ($1, $2, TRUE, 'legba/target/1.0.0', 'test', 'IL', $3::jsonb)",
            "country_watch_il", "c" * 16, json.dumps({"scope": {"geo": ["IL"]}}),
        )
        await conn.execute(
            "INSERT INTO hypotheses (id, thesis, status, target_id) "
            "VALUES ($1, 'q', 'open_question', 'country_watch_il')", hyp,
        )
        # A curated row ALREADY in the desk's slice, at the same URL.
        await conn.execute(
            "INSERT INTO signals (source_id, canonical_url, geo, payload, "
            "fetched_at) VALUES ('source.wire', $1, ARRAY['IL'], '{}'::jsonb, now())",
            "https://unreviewed.test/known",
        )
    web()
    await _run(
        dsn,
        _StubProvider(_response(
            _hit("https://unreviewed.test/known"), _hit("https://unreviewed.test/new"),
        )),
        args={"query": "q", "hypothesis_id": str(hyp)},
    )
    by_url = {r["canonical_url"]: r for r in await _signals(dsn)}
    def _nov(url):
        p = by_url[url]["payload"]
        p = json.loads(p) if isinstance(p, str) else p
        return p["research"]["novelty"]

    known = _nov("https://unreviewed.test/known")
    fresh = _nov("https://unreviewed.test/new")
    assert known["scope"] == "dispatching_target"
    assert known["url_in_slice"] is True and known["novel"] is False
    assert fresh["url_in_slice"] is False and fresh["novel"] is True
    assert fresh["slice_target_id"] == "country_watch_il"
    # The two booleans R-D reads are mirrored onto raw_provenance, in agreement.
    prov = by_url["https://unreviewed.test/known"]["raw_provenance"]
    prov = json.loads(prov) if isinstance(prov, str) else prov
    assert prov["novelty"]["novel"] is False


async def test_restore_source_credibility_seed_fully_restores_a_scrubbed_table(
    dsn, restore_source_credibility_seed,
):
    """NEGATIVE test for the conftest primitive itself: a table left FULLY
    EMPTY by an unscoped DELETE (exactly what this file's own ``_reset_db``
    does before ``restore_source_credibility_seed`` runs) must come back with
    all 21 baseline rows — not a partial or silently-skipped restore — so the
    next file that wipes a seeded table has a proven one-line remedy."""
    async with _conn(dsn) as conn:
        await conn.execute("DELETE FROM source_credibility")
        count_after_scrub = await conn.fetchval("SELECT count(*) FROM source_credibility")
        assert count_after_scrub == 0, "the scrub itself must actually empty the table"

        await restore_source_credibility_seed(conn)
        count_after_restore = await conn.fetchval("SELECT count(*) FROM source_credibility")
        assert count_after_restore == 21, (
            f"expected all 21 baseline source_credibility rows restored, got {count_after_restore}"
        )


# ---------------------------------------------------------------------------
# THE DISPATCHED RUN, END TO END (2026-09-07)
#
# The 03:37Z failure was not one broken function; it was a chain that only
# fails when you walk the whole of it. So this walks the whole of it, through
# the REAL binding path, with only the socket and the model stubbed:
#
#   the real dispatch write -> the real backlog SQL + ranking -> the real
#   deps-builder grounding hook -> the real inline_target run -> the real
#   GATHER loop -> the real Agency gate -> the real web_evidence write path
#   -> the real signals row -> the real REFLECT answer-link -> the real settle.
#
# What it proves, in the order the defect broke it:
#
#   1. the dispatched coverage_floor gap — not the older unit_payload menu —
#      is the run's assignment, and the prompt names it as THE question;
#   2. the desk slice is NULL on the polity and the corpus read comes back
#      empty, so the web leg's null test is satisfied by construction;
#   3. ``web_evidence`` is INVOKED, carrying the hypothesis_id the prompt
#      printed — the argument the live prompt never showed and the tool is the
#      only reader of;
#   4. the landed signals row carries geo ["IL"], i.e. it reaches the desk that
#      asked (the 03:37Z run's five signals carried geo {});
#   5. the finding's answer-link resolves to that question;
#   6. the question's status moves — claimed by the run, then ANSWERED once
#      the bearing edge the runtime writes after persist exists.
# ---------------------------------------------------------------------------

_IL_TARGET = "country_watch_il"


def _researcher_descriptor():
    """``corpus_researcher``'s shape: a META inline_target opting into the
    ``open_questions`` grounding source and nothing else."""
    from legba.data.schemas.analyst import AnalystDescriptor

    return AnalystDescriptor.model_validate(
        {
            "identity": {
                "id": "corpus_researcher", "name": "R", "kind": "inline_target",
                "schema_uri": "legba/analyst/1.0.0", "version": "0" * 16,
                "type_signature": {
                    "input_type": "legba.runtime.SignalList",
                    "output_type": "legba.runtime.Finding",
                },
                "state": "active", "owner": "t",
            },
            "subscription": {
                "substrate": {"direct_queries": True, "gather_only": False}
            },
            "method": {
                "kind": "llm_planner",
                "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
                "llm": {"primary": {"factory_kind": "stack_ref", "raw": "llm.x",
                                    "expected_family": "llm_provider"}},
            },
            "cadence": {"fallback_schedule": "37 3,15 * * *"},
            "grounding": {
                "enabled": True, "sources": ["open_questions"], "max_facts": 8
            },
        },
        strict=False,
    )


class _NullCorpusBinding:
    """The substrate_read leg. ``search_corpus`` comes back EMPTY — which is
    the whole point: a coverage gap is measured ON our own collection, so the
    corpus read is null for that polity by construction. This is the null the
    web leg is supposed to fire on."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def run_tool(self, name, args):
        self.calls.append((name, dict(args)))

        class _R:
            status = "completed"
            error = None
            output = {"rows": [], "count": 0}

        class _O:
            admitted = True
            block_cause = None
            detail = None
            tool_result = _R()

        return _O()


class _AssignmentFollowingPlanner:
    """A planner that does what the prompt tells it, and records what it read.

    It carries no canned hypothesis id: it EXTRACTS the id from the rendered
    prompt with the same regex a reader would use. If the block does not print
    one, this cannot call the tool correctly and the test fails — which is
    exactly the property the live prompt lacked.
    """

    subprovider = "openai"
    _ID_RE = re.compile(r"hypothesis_id=([0-9a-fA-F-]{36})")

    def __init__(self):
        self.prompts: list[str] = []
        self.hypothesis_id: str | None = None
        self._turn = 0

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kwargs):
        convo = "\n".join(
            str(m.get("content") or "") for m in (messages or [])
        )
        self.prompts.append(convo)
        if self.hypothesis_id is None:
            found = self._ID_RE.search(convo)
            self.hypothesis_id = found.group(1) if found else None
        self._turn += 1

        if self._turn == 1:
            # Read the corpus first — the assignment says the corpus read is
            # part of the job even though it cannot be the end of it.
            content = json.dumps({
                "tool": "search_corpus",
                "args": {"query": "Palestine country_watch_il", "size": 5},
            })
        elif self._turn == 2:
            # The corpus came back empty. The assignment makes the web leg
            # mandatory and prints the id that carries the evidence to the desk.
            content = json.dumps({
                "tool": "web_evidence",
                "args": {
                    "query": "Palestine Israel policy",
                    "hypothesis_id": self.hypothesis_id or "",
                },
            })
        elif self._turn == 3:
            content = json.dumps({"done": True})
        else:
            content = json.dumps({
                "title": "Palestine bears on country_watch_il's internal stability",
                "body": "**BLUF:** the corpus held nothing; the web did [1].",
                "confidence": 0.4,
                "evidence": [],
                "tags": ["severity:low", "topic:corpus_research"],
                "addressed_question": "Q1",
            })

        class _U:
            prompt_tokens = 1
            completion_tokens = 1
            reasoning_tokens = 0
            total_tokens = 2

        class _R:
            def __init__(self, c):
                self.content = c
                self.usage = _U()

        return _R(content)


def _uk_slice() -> list[dict]:
    """A working set that is NULL on the polity — the live 03:37Z shape: 125
    UK-centric signals and nothing whatever on Israel or Palestine. This is
    what makes the corpus read a genuine null rather than a lazy one."""
    return [
        {
            "id": uuid4(),
            "produced_at": datetime.now(timezone.utc),
            "title_en": f"Wildfire near Sizewell B contained ({i})",
            "data": {"raw_body": "Crews held the fire line overnight."},
            "geo": ["GB"],
        }
        for i in range(3)
    ]


async def _sweep_backlog(dsn):
    """The backlog is shared across this DB; a sibling file's dispatched row
    would otherwise become THIS run's assignment. Scoped to the three statuses
    the drain and the claim use, the same sweep the dispatch suite performs and
    for the same reason."""
    async with _conn(dsn) as conn:
        await conn.execute(
            "DELETE FROM hypotheses WHERE status = ANY($1::text[])",
            ["open_question", "in_progress", "answered"],
        )


async def test_a_dispatched_run_answers_the_gap_and_lands_evidence_on_the_desk(
    dsn, web, archive_root,
):
    from legba.data.analysts.deterministic_handlers import _research_dispatch as rd
    from legba.data.analysts.inline_target import (
        InlineTargetDeps,
        run_method,
    )
    from legba.data.provenance.bearing import record_bearing_edge
    from legba.runtime import dispatched_question as dqm
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    await _sweep_backlog(dsn)
    run_id = uuid4()
    now = datetime.now(timezone.utc)

    async with _conn(dsn) as conn:
        # The desk that asked, and the geo that is the ONLY reachability key.
        await conn.execute(
            "INSERT INTO target_descriptors (descriptor_id, version, is_head, "
            "schema_uri, owner, name, body) VALUES ($1, $2, TRUE, "
            "'legba/target/1.0.0', 'test', 'IL', $3::jsonb)",
            _IL_TARGET, "c" * 16,
            json.dumps({"scope": {"geo": ["IL"], "tags": ["news"]}}),
        )
        # The menu the 03:37Z run picked off — older, and it must not win now.
        for i in range(3):
            await conn.execute(
                "INSERT INTO hypotheses (thesis, status, produced_at, "
                "diagnostic_evidence) VALUES ($1, 'open_question', "
                "now() - make_interval(days => $2), $3::jsonb)",
                f"Will the wildfire near Sizewell B force an outage? ({i})",
                35 + i,
                json.dumps([{"marker": "open_question_origin",
                             "origin": "unit_payload",
                             "finding_id": str(uuid4())}]),
            )
        # The dispatch, through the REAL writer.
        assert await rd.dispatch_open_questions(
            conn,
            [rd.dispatch_payload_for_cluster(
                target_id=_IL_TARGET, geo=["IL"],
                cluster={"name": "Palestine", "entity_fold": "palestine",
                         "n_signals": 238, "n_days": 15, "mean_magnitude": 0.52,
                         "slice_share": 0.2, "exemplar_signal_ids": []},
                open_frame_count=8, rising_edge=now,
                context={"desk_id": "internal_stability",
                         "bounded_question": "Where is stability going?"},
            )],
            alert_output_id=uuid4(), run_id=uuid4(),
        ) == 1
        qid = await conn.fetchval(
            "SELECT id FROM hypotheses WHERE analyst_id = $1",
            rd.DISPATCH_ANALYST_ID,
        )
        await conn.execute(
            "INSERT INTO source_credibility (source_host, score, scored_by, "
            "license_class) VALUES ('cleared.test', 0.9, 'op', 'cc_by')"
        )

    web()
    provider = _StubProvider(_response(
        _hit("https://cleared.test/palestine", title="Palestine policy shift"),
        query="Palestine Israel policy",
    ))
    planner = _AssignmentFollowingPlanner()

    async with _pool(dsn) as pool:
        research_binding = AgencyToolBinding(
            agency=Agency(),
            pack=_pack(),
            pg_pool=pool,
            tool_context=ToolContext(
                search=provider,
                writeback=WritebackContext(
                    pg_pool=pool,
                    analyst_ctx=AnalystContext(
                        analyst_id="corpus_researcher", analyst_version="b" * 16,
                        run_id=run_id, target_id=None, target_version=None,
                    ),
                ),
            ),
            analyst_grants=[ActionPackRef(pack_id="research")],
            target_allows=[ActionPackRef(pack_id="research")],
            scope=GLOBAL_SCOPE,
            requested_by="analyst::corpus_researcher",
            budget_account=f"acct-{uuid4().hex[:8]}",
        )
        read_binding = _NullCorpusBinding()
        hook = _build_grounding_hook(_researcher_descriptor(), pg_pool=pool)
        assert hook is not None

        result = await run_method(
            _uk_slice(),
            {
                "analyst_id": "corpus_researcher",
                "run_id": run_id,
                "agency_binding": read_binding,
                "gather_tool_bindings": {"web_evidence": research_binding},
            },
            InlineTargetDeps(
                llm=planner, grounding_hook=hook, max_rounds=4, max_tokens=512,
            ),
        )

    # 1. THE ASSIGNMENT reached the prompt as THE question, not as option one.
    prompt = planner.prompts[0]
    assert "DISPATCHED RESEARCH ASSIGNMENT" in prompt
    assert "STANDING OPEN QUESTIONS" not in prompt
    assert "Palestine" in prompt
    assert f"scope={_IL_TARGET} geo=IL" in prompt
    # 2. …and the slice it was handed is NULL on the polity: the working set is
    #    UK wildfire coverage, exactly as it was live.
    assert "Sizewell" in prompt and "[Q2]" not in prompt

    # 3. THE WEB LEG FIRED, carrying the id the block printed.
    assert planner.hypothesis_id == str(qid)
    assert [name for name, _ in read_binding.calls] == ["search_corpus"]
    tool_steps = [
        s for s in result.intermediate_steps
        if s.get("kind") == "tool_call" and s.get("tool") == "web_evidence"
    ]
    assert len(tool_steps) == 1 and tool_steps[0]["ok"] is True
    assert provider.queries == ["Palestine Israel policy"]

    # 4. THE EVIDENCE REACHES THE DESK THAT ASKED. The 03:37Z run's signals
    #    carried geo {} and reached nobody.
    rows = await _signals(dsn)
    assert rows, "web_evidence landed nothing"
    assert all(r["geo"] == ["IL"] for r in rows)
    assert all(
        r["retrieval_origin"] == "web_search:search.searxng.local" for r in rows
    )
    prov = rows[0]["raw_provenance"]
    prov = json.loads(prov) if isinstance(prov, str) else prov
    assert prov["dispatch"]["hypothesis_id"] == str(qid)
    assert prov["dispatch"]["target_id"] == _IL_TARGET

    # 5. THE ANSWER-LINK resolved against the run's own sink.
    link = result.finding.data.get("addressed_question")
    assert link is not None and link["hypothesis_id"] == str(qid)
    assert link["harvest_class"] == "coverage_floor"
    assert UUID(str(qid)) in result.derived_from

    # 6. THE STATUS MOVED. Claimed at GROUND…
    async with _conn(dsn) as conn:
        row = await conn.fetchrow(
            "SELECT status, run_id FROM hypotheses WHERE id = $1", qid
        )
    assert row["status"] == dqm.CLAIMED_STATUS and row["run_id"] == run_id

    # …and ANSWERED once the bearing edge the runtime writes after the output
    # row persists exists — the settle takes durable evidence, never a promise.
    async with _pool(dsn) as pool:
        async with pool.acquire() as conn:
            await record_bearing_edge(
                conn, src_kind="finding", src_id=uuid4(),
                src_as_of=datetime.now(timezone.utc), dst_kind="hypothesis",
                dst_id=qid, dst_as_of=now, weight=1.0, planes=["research"],
                matcher_version="test",
            )
        counts = await dqm.settle_claimed_questions(
            pool, resolved_by="corpus_researcher"
        )
    assert counts["answered"] == 1
    async with _conn(dsn) as conn:
        assert await conn.fetchval(
            "SELECT status FROM hypotheses WHERE id = $1", qid
        ) == dqm.ANSWERED_STATUS
        await conn.execute("DELETE FROM bearing_edges WHERE dst_id = $1", qid)
    await _sweep_backlog(dsn)


async def test_with_nothing_dispatched_the_same_run_self_selects_unchanged(
    dsn, web, archive_root,
):
    """The fallback, through the same real path: the identical backlog MINUS
    the dispatch renders the ordinary priority-ordered menu, nothing is
    claimed, and the run is free to self-select — which is exactly when it
    should be."""
    from legba.data.analysts.inline_target import (
        InlineTargetDeps,
        run_method,
    )
    from legba.runtime import dispatched_question as dqm
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    await _sweep_backlog(dsn)
    async with _conn(dsn) as conn:
        for i in range(3):
            await conn.execute(
                "INSERT INTO hypotheses (thesis, status, produced_at, "
                "diagnostic_evidence) VALUES ($1, 'open_question', "
                "now() - make_interval(days => $2), $3::jsonb)",
                f"Will the wildfire near Sizewell B force an outage? ({i})",
                35 + i,
                json.dumps([{"marker": "open_question_origin",
                             "origin": "unit_payload",
                             "finding_id": str(uuid4())}]),
            )

    class _SelfSelectingPlanner(_AssignmentFollowingPlanner):
        async def chat_complete(self, messages, **kwargs):
            self.prompts.append("\n".join(
                str(m.get("content") or "") for m in (messages or [])
            ))

            class _U:
                prompt_tokens = completion_tokens = 1
                reasoning_tokens = 0
                total_tokens = 2

            class _R:
                content = json.dumps({
                    "title": "A self-selected read",
                    "body": "**BLUF:** nothing was dispatched.",
                    "confidence": 0.3, "evidence": [],
                    "tags": ["severity:low", "topic:corpus_research"],
                })
                usage = _U()

            return _R()

    planner = _SelfSelectingPlanner()
    async with _pool(dsn) as pool:
        hook = _build_grounding_hook(_researcher_descriptor(), pg_pool=pool)
        result = await run_method(
            _uk_slice(),
            {"analyst_id": "corpus_researcher", "run_id": uuid4()},
            InlineTargetDeps(llm=planner, grounding_hook=hook, max_tokens=512),
        )

    prompt = planner.prompts[0]
    assert "STANDING OPEN QUESTIONS" in prompt
    assert "DISPATCHED RESEARCH ASSIGNMENT" not in prompt
    assert "hypothesis_id=" not in prompt
    # All three are still on the menu, tagged — the run picks, as it always did.
    assert all(f"[Q{i}]" in prompt for i in (1, 2, 3))
    # No question was claimed and no evidence was fetched: a self-selected run
    # is substrate-only, exactly as before.
    assert result.finding.data.get("addressed_question") is None
    async with _conn(dsn) as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM hypotheses WHERE status = $1", dqm.CLAIMED_STATUS,
        ) == 0
        assert await conn.fetchval("SELECT count(*) FROM signals") == 0
    await _sweep_backlog(dsn)


# ---------------------------------------------------------------------------
# THE PLANNER EXECUTES ITS ASSIGNMENT (2026-09-08)
#
# The section above proved the chain works when the model speaks the protocol.
# Three dispatched runs then showed it does not. Live, from analyst_traces:
#
#   09-07 15:37Z  success. Assignment present, ONE search_corpus call, round 2
#                 unparseable, and the FINDING's body was the model's own
#                 action text — "Attempt to fetch external evidence." plus
#                 {"action": "web_evidence", "query": …, "hypothesis_id": …}.
#                 Faithfulness 1.00 on a claim about nothing. The question was
#                 claimed at GROUND and stayed in_progress.
#   09-08 03:37Z  success. NO assignment block — the claim above had moved the
#                 row out of ``status='open_question'``, which is the only
#                 status the backlog SQL reads, so the ranker could not see it.
#                 Round 2 ``unrecognized``; body "Attempt tool use."
#   09-08 15:37Z  FAILED (OutputContractError, empty body). Also unassigned,
#                 self-selected a unit_payload question, and DID land five
#                 signals — with geo {} and dispatch.kind self_selected,
#                 because nothing outside an assignment block prints an id.
#
# So these three tests are the three defects, driven through the same REAL
# chain as the section above with the LIVE reply shapes as the stub's script.
# ---------------------------------------------------------------------------


#: The 09-07 15:37Z body, verbatim except for the id, which each test splices
#: in from the prompt it was actually shown.
_LIVE_NARRATION = (
    "Attempt to fetch external evidence.\n"
    "{{\n"
    '  "action": "web_evidence",\n'
    '  "query": "Israel Palestine conflict September 2026 news",\n'
    '  "hypothesis_id": "{qid}"\n'
    "}}"
)


class _LiveShapePlanner(_AssignmentFollowingPlanner):
    """Round 2 replies with the 09-07 narration + flat ``{"action": …}`` object.

    Everything else is the assignment-following planner's own script, so the
    ONLY difference between this test and the passing one above is the envelope
    the model put its call in — which is exactly the difference that cost three
    live runs.
    """

    #: Set by a subclass to drop the id from the action, testing the loop's
    #: own scope carry rather than the model's transcription.
    omit_hypothesis_id = False

    #: The ordinal the landed web row was numbered with in the synthesis
    #: prompt — read off the prompt rather than hardcoded, so the citation
    #: assertion tracks the real ``base_offset`` arithmetic.
    _GATHERED_RE = re.compile(r"\[(\d+)\][^\n]*Palestine policy shift")

    async def chat_complete(self, messages, **kwargs):
        turn = self._turn
        result = await super().chat_complete(messages, **kwargs)
        if turn == 1:  # the round-2 reply, after super() advanced _turn to 2
            qid = "" if self.omit_hypothesis_id else (self.hypothesis_id or "")
            narration = _LIVE_NARRATION.format(qid=qid)
            if self.omit_hypothesis_id:
                narration = "\n".join(
                    ln for ln in narration.splitlines()
                    if "hypothesis_id" not in ln
                ).replace('September 2026 news",', 'September 2026 news"')
            result.content = narration
        elif turn == 3:  # synthesis — cite the row the action actually landed
            found = self._GATHERED_RE.search(self.prompts[-1])
            n = found.group(1) if found else "?"
            result.content = json.dumps({
                "title": "Palestine bears on country_watch_il",
                "body": (
                    "**BLUF:** the corpus held nothing on the polity; the web "
                    f"returned a policy shift [{n}]."
                ),
                "confidence": 0.4,
                "evidence": [],
                "tags": ["severity:low", "topic:corpus_research"],
                "addressed_question": "Q1",
            })
        return result


class _IdlessLiveShapePlanner(_LiveShapePlanner):
    omit_hypothesis_id = True


async def _dispatch_the_il_gap(dsn, now):
    """Seed the desk, the older unit_payload menu and the REAL dispatch write;
    return the dispatched question's id. Same fixture as the section above,
    lifted out because three tests now need it."""
    from legba.data.analysts.deterministic_handlers import _research_dispatch as rd

    async with _conn(dsn) as conn:
        await conn.execute(
            "INSERT INTO target_descriptors (descriptor_id, version, is_head, "
            "schema_uri, owner, name, body) VALUES ($1, $2, TRUE, "
            "'legba/target/1.0.0', 'test', 'IL', $3::jsonb)",
            _IL_TARGET, "c" * 16,
            json.dumps({"scope": {"geo": ["IL"], "tags": ["news"]}}),
        )
        for i in range(3):
            await conn.execute(
                "INSERT INTO hypotheses (thesis, status, produced_at, "
                "diagnostic_evidence) VALUES ($1, 'open_question', "
                "now() - make_interval(days => $2), $3::jsonb)",
                f"Will the wildfire near Sizewell B force an outage? ({i})",
                35 + i,
                json.dumps([{"marker": "open_question_origin",
                             "origin": "unit_payload",
                             "finding_id": str(uuid4())}]),
            )
        assert await rd.dispatch_open_questions(
            conn,
            [rd.dispatch_payload_for_cluster(
                target_id=_IL_TARGET, geo=["IL"],
                cluster={"name": "Palestine", "entity_fold": "palestine",
                         "n_signals": 238, "n_days": 15, "mean_magnitude": 0.52,
                         "slice_share": 0.2, "exemplar_signal_ids": []},
                open_frame_count=8, rising_edge=now,
                context={"desk_id": "internal_stability",
                         "bounded_question": "Where is stability going?"},
            )],
            alert_output_id=uuid4(), run_id=uuid4(),
        ) == 1
        await conn.execute(
            "INSERT INTO source_credibility (source_host, score, scored_by, "
            "license_class) VALUES ('cleared.test', 0.9, 'op', 'cc_by')"
        )
        return await conn.fetchval(
            "SELECT id FROM hypotheses WHERE analyst_id = $1",
            rd.DISPATCH_ANALYST_ID,
        )


async def _run_researcher(dsn, planner, *, run_id, provider=None, rounds=4):
    """One corpus_researcher run through the real hook, loop, gate and writer."""
    from legba.data.analysts.inline_target import InlineTargetDeps, run_method
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    async with _pool(dsn) as pool:
        options = {"analyst_id": "corpus_researcher", "run_id": run_id,
                   "agency_binding": _NullCorpusBinding()}
        if provider is not None:
            options["gather_tool_bindings"] = {"web_evidence": AgencyToolBinding(
                agency=Agency(),
                pack=_pack(),
                pg_pool=pool,
                tool_context=ToolContext(
                    search=provider,
                    writeback=WritebackContext(
                        pg_pool=pool,
                        analyst_ctx=AnalystContext(
                            analyst_id="corpus_researcher",
                            analyst_version="b" * 16, run_id=run_id,
                            target_id=None, target_version=None,
                        ),
                    ),
                ),
                analyst_grants=[ActionPackRef(pack_id="research")],
                target_allows=[ActionPackRef(pack_id="research")],
                scope=GLOBAL_SCOPE,
                requested_by="analyst::corpus_researcher",
                budget_account=f"acct-{uuid4().hex[:8]}",
            )}
        hook = _build_grounding_hook(_researcher_descriptor(), pg_pool=pool)
        return await run_method(
            _uk_slice(), options,
            InlineTargetDeps(llm=planner, grounding_hook=hook,
                             max_rounds=rounds, max_tokens=512),
        )


async def _write_trace(dsn, *, run_id, analyst_id="corpus_researcher"):
    """The ``analyst_traces`` row every run leaves — success or hard failure.

    The re-render's ownership test reads it (``_STANDING_CLAIM_SQL``): a claim
    is THIS analyst's iff the run that took it left a trace naming this
    analyst. Ownership is read from the trace rather than re-stamped onto the
    question so a re-render never rewrites the record of who claimed it."""
    async with _conn(dsn) as conn:
        await conn.execute(
            "INSERT INTO analyst_traces (run_id, analyst_id, analyst_version, "
            "cadence_trigger, status, run_started_at, receipt_hash) "
            "VALUES ($1, $2, $3, 'schedule', 'failed', now(), $4)",
            run_id, analyst_id, "b" * 16, uuid4().hex,
        )


async def test_the_live_narration_shape_is_executed_not_published(
    dsn, web, archive_root,
):
    """(a) THE 09-07 DEFECT. The model emits the exact live narration + flat
    ``{"action": …}`` object. The loop must EXECUTE it — land the evidence on
    the desk, with the dispatch stamped on the row — and the finding must be
    grounded in what landed, not be the narration."""
    from legba.data.provenance.bearing import record_bearing_edge
    from legba.runtime import dispatched_question as dqm

    await _sweep_backlog(dsn)
    now = datetime.now(timezone.utc)
    qid = await _dispatch_the_il_gap(dsn, now)

    web()
    provider = _StubProvider(_response(
        _hit("https://cleared.test/palestine", title="Palestine policy shift"),
        query="Israel Palestine conflict September 2026 news",
    ))
    planner = _LiveShapePlanner()
    run_id = uuid4()
    result = await _run_researcher(dsn, planner, run_id=run_id, provider=provider)

    # THE ACTION RAN. The live run's round 2 was ``unrecognized``; this is a
    # completed tool_call, with the query the model actually asked for.
    steps = [s for s in result.intermediate_steps if s.get("kind") == "tool_call"]
    assert [s["tool"] for s in steps] == ["search_corpus", "web_evidence"]
    assert steps[1]["ok"] is True
    assert provider.queries == ["Israel Palestine conflict September 2026 news"]

    # THE EVIDENCE REACHED THE DESK, and says which question fetched it.
    rows = await _signals(dsn)
    assert rows and all(r["geo"] == ["IL"] for r in rows)
    payload = rows[0]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["research"]["dispatch"] == {
        "kind": "coverage_floor",          # the ALERT class, not "hypothesis"
        "hypothesis_id": str(qid),
        "target_id": _IL_TARGET,
    }

    # THE FINDING IS GROUNDED IN WHAT LANDED — not in the narration. The live
    # body was the action text itself; this one cites the row the action wrote.
    assert "Attempt to fetch external evidence" not in result.finding.body
    cited = {c.get("signal_id") for c in result.finding.data.get("citations", [])}
    assert str(rows[0]["id"]) in {str(c) for c in cited if c}
    assert result.finding.data["addressed_question"]["hypothesis_id"] == str(qid)

    # AND THE QUESTION SETTLES once the runtime's bearing edge exists.
    async with _pool(dsn) as pool:
        async with pool.acquire() as conn:
            await record_bearing_edge(
                conn, src_kind="finding", src_id=uuid4(),
                src_as_of=datetime.now(timezone.utc), dst_kind="hypothesis",
                dst_id=qid, dst_as_of=now, weight=1.0, planes=["research"],
                matcher_version="test",
            )
        assert (await dqm.settle_claimed_questions(
            pool, resolved_by="corpus_researcher"))["answered"] == 1
    async with _conn(dsn) as conn:
        assert await conn.fetchval(
            "SELECT status FROM hypotheses WHERE id = $1", qid
        ) == dqm.ANSWERED_STATUS
        await conn.execute("DELETE FROM bearing_edges WHERE dst_id = $1", qid)
    await _sweep_backlog(dsn)


async def test_an_action_without_the_id_still_reaches_the_desk(
    dsn, web, archive_root,
):
    """THE SCOPE CARRY, made structural. The same live shape with the
    ``hypothesis_id`` line dropped — the transcription failure the prompt
    cannot prevent. The run KNOWS its assignment, so the loop stamps the id and
    the evidence still lands with the desk's geo. The 09-08 15:37Z run, which
    had no assignment to stamp from, landed five rows with geo {}."""
    await _sweep_backlog(dsn)
    qid = await _dispatch_the_il_gap(dsn, datetime.now(timezone.utc))

    web()
    provider = _StubProvider(_response(
        _hit("https://cleared.test/palestine", title="Palestine policy shift"),
        query="Israel Palestine conflict September 2026 news",
    ))
    result = await _run_researcher(
        dsn, _IdlessLiveShapePlanner(), run_id=uuid4(), provider=provider,
    )

    stamped = [
        s for s in result.intermediate_steps
        if s.get("kind") == "tool_call" and s.get("tool") == "web_evidence"
    ]
    assert len(stamped) == 1 and stamped[0]["ok"] is True
    # The receipt names the id the loop supplied — absent when it supplied none.
    assert stamped[0]["dispatch_hypothesis_id"] == str(qid)

    rows = await _signals(dsn)
    assert rows and all(r["geo"] == ["IL"] for r in rows)
    prov = rows[0]["raw_provenance"]
    prov = json.loads(prov) if isinstance(prov, str) else prov
    assert prov["dispatch"]["hypothesis_id"] == str(qid)
    await _sweep_backlog(dsn)


class _NarratingProsePlanner(_AssignmentFollowingPlanner):
    """The 09-08 03:37Z shape: prose in GATHER (no parseable action at all),
    then a FINDING that is the announcement ``"Attempt tool use."``"""

    async def chat_complete(self, messages, **kwargs):
        result = await super().chat_complete(messages, **kwargs)
        result.content = (
            "Attempt tool use." if self._turn > 1
            else "I should look at the corpus for Palestine coverage."
        )
        return result


async def test_a_narrated_run_publishes_nothing_and_keeps_the_claim(
    dsn, web, archive_root,
):
    """(b) THE 09-08 03:37Z DEFECT. The planner never emits a parseable action
    and its finding is the announcement. Nothing may be published: the run
    fails loud with the receipt, and the question stays CLAIMED so the next
    tick can re-render it."""
    from legba.data.analysts.output_contract import OutputContractError
    from legba.data.analysts.planner_action import NARRATED_WITHOUT_ACTION
    from legba.runtime import dispatched_question as dqm

    await _sweep_backlog(dsn)
    qid = await _dispatch_the_il_gap(dsn, datetime.now(timezone.utc))

    web()
    with pytest.raises(OutputContractError) as raised:
        await _run_researcher(dsn, _NarratingProsePlanner(), run_id=uuid4())
    # The receipt is in the message, which is what an ``error_payload`` keeps
    # when a hard-failed run's steps are not persisted (as they were not live).
    assert NARRATED_WITHOUT_ACTION in str(raised.value)
    assert str(qid) in str(raised.value)

    async with _conn(dsn) as conn:
        # Nothing published, nothing fetched…
        assert await conn.fetchval("SELECT count(*) FROM signals") == 0
        # …and the claim STANDS: the gap is not answered and not abandoned.
        assert await conn.fetchval(
            "SELECT status FROM hypotheses WHERE id = $1", qid
        ) == dqm.CLAIMED_STATUS
    await _sweep_backlog(dsn)


async def test_the_next_tick_re_renders_a_claim_this_analyst_did_not_answer(
    dsn, web, archive_root,
):
    """(c) THE 09-08 03:37Z SECOND DEFECT, under the 09-09 claim rule. After
    the narrated run above the question was ``in_progress`` — invisible to the
    backlog SQL, which reads ``status='open_question'`` and nothing else — and
    live, the next two ticks were handed the ordinary unit_payload menu while
    the gap sat unassigned.

    The run FAILED and left a trace saying so, so the settle now RELEASES its
    claim (a claim held by a run that died is not a claim) and the question is
    back on the general backlog, where the ranker sees it and assigns it again.
    Either way the next tick gets the SAME assignment and never the menu; the
    difference this test pins is that the row is properly re-claimed by the run
    that now holds it, rather than left in a dead run's name."""
    await _sweep_backlog(dsn)
    qid = await _dispatch_the_il_gap(dsn, datetime.now(timezone.utc))

    web()
    first_run = uuid4()
    with pytest.raises(Exception):
        await _run_researcher(dsn, _NarratingProsePlanner(), run_id=first_run)
    await _write_trace(dsn, run_id=first_run)
    async with _conn(dsn) as conn:
        claimed_at, claim_run = await conn.fetchrow(
            "SELECT updated_at, run_id FROM hypotheses WHERE id = $1", qid
        )
    assert claim_run == first_run

    # THE NEXT TICK. Same analyst, new run, and the SAME assignment is offered.
    provider = _StubProvider(_response(
        _hit("https://cleared.test/palestine", title="Palestine policy shift"),
        query="Israel Palestine conflict September 2026 news",
    ))
    planner = _LiveShapePlanner()
    await _run_researcher(dsn, planner, run_id=uuid4(), provider=provider)

    prompt = planner.prompts[0]
    assert "DISPATCHED RESEARCH ASSIGNMENT" in prompt
    assert "STANDING OPEN QUESTIONS" not in prompt   # not back to the menu
    assert f"hypothesis_id={qid}" in prompt
    assert f"scope={_IL_TARGET} geo=IL" in prompt
    assert "[Q2]" not in prompt                      # the older menu stays off
    # THE DEAD RUN'S HOLD IS GONE. The claim moved to the run that actually
    # holds it now, and its clock restarted — the release put the row back on
    # the general backlog, where the 26h window has nothing left to protect.
    async with _conn(dsn) as conn:
        again, run_after = await conn.fetchrow(
            "SELECT updated_at, run_id FROM hypotheses WHERE id = $1", qid
        )
    assert run_after != first_run
    assert again > claimed_at
    await _sweep_backlog(dsn)


async def test_a_successful_run_that_did_not_answer_keeps_the_claim(dsn):
    """The half the release does NOT cover, and must not.

    A run that SUCCEEDED — published a finding, wrote a ``success`` trace — and
    still never answered its assignment (the 09-07 15:37Z shape: a narration
    published as a finding, no ``addressed_question``, no bearing edge) is not
    a dead run. Its claim stands, and ``resolve_standing_assignment``
    re-renders the same assignment WITHOUT re-claiming it, so the reclaim clock
    keeps running from the original claim and a gap nobody can answer still
    returns to the general backlog."""
    from legba.runtime import dispatched_question as dqm

    await _sweep_backlog(dsn)
    qid = await _dispatch_the_il_gap(dsn, datetime.now(timezone.utc))
    live_run = uuid4()
    async with _conn(dsn) as conn:
        await conn.execute(
            "UPDATE hypotheses SET status = $2, run_id = $3 WHERE id = $1",
            qid, dqm.CLAIMED_STATUS, live_run,
        )
        await conn.execute(
            "INSERT INTO analyst_traces (run_id, analyst_id, analyst_version, "
            "cadence_trigger, status, run_started_at, receipt_hash) "
            "VALUES ($1, 'corpus_researcher', $2, 'schedule', 'success', now(), $3)",
            live_run, "b" * 16, uuid4().hex,
        )
        before = await conn.fetchval(
            "SELECT updated_at FROM hypotheses WHERE id = $1", qid
        )

    async with _pool(dsn) as pool:
        counts = await dqm.settle_claimed_questions(
            pool, resolved_by="corpus_researcher",
        )
        assert counts["released"] == 0          # a live run's claim is untouched
        standing = await dqm.resolve_standing_assignment(
            pool, analyst_id="corpus_researcher",
        )
    assert standing is not None and str(standing.question_id) == str(qid)

    async with _conn(dsn) as conn:
        after, run_after = await conn.fetchrow(
            "SELECT updated_at, run_id FROM hypotheses WHERE id = $1", qid
        )
    assert after == before and run_after == live_run   # re-rendered, not re-claimed
    await _sweep_backlog(dsn)


async def test_a_claim_held_by_another_analyst_is_never_re_rendered(dsn):
    """The ownership half of the same rule. A claim whose run belongs to some
    OTHER analyst is not this run's job to re-render — it falls to the reclaim
    window, which is what that window is for."""
    from legba.runtime import dispatched_question as dqm

    await _sweep_backlog(dsn)
    qid = await _dispatch_the_il_gap(dsn, datetime.now(timezone.utc))
    other_run = uuid4()
    async with _conn(dsn) as conn:
        await conn.execute(
            "UPDATE hypotheses SET status = $2, run_id = $3 WHERE id = $1",
            qid, dqm.CLAIMED_STATUS, other_run,
        )
    await _write_trace(dsn, run_id=other_run, analyst_id="some_other_analyst")

    async with _pool(dsn) as pool:
        assert await dqm.resolve_standing_assignment(
            pool, analyst_id="corpus_researcher"
        ) is None
        # …and it IS this analyst's once a trace of ITS OWN names the run.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE analyst_traces SET analyst_id = 'corpus_researcher' "
                "WHERE run_id = $1", other_run,
            )
        standing = await dqm.resolve_standing_assignment(
            pool, analyst_id="corpus_researcher"
        )
    assert standing is not None
    assert str(standing.question_id) == str(qid)
    assert standing.harvest_class == "coverage_floor"
    assert standing.geo == ("IL",) and standing.target_id == _IL_TARGET
    await _sweep_backlog(dsn)


# ---------------------------------------------------------------------------
# THE FINAL TURN IS THE FINDING (2026-09-09)
#
# Three consecutive live hard fails — 09-08 15:37Z, 09-09 03:37Z, 09-09 15:37Z —
# all reading, character for character:
#
#     OutputContractError: model returned a finding contract with no readable
#     body (title='Assessment for target')
#
# with attempts_made=1, tool_calls 0, prompt_rendered NULL, intermediate_steps
# [] and llm_calls [] — while the runtime LOG showed the 03:37Z run had claimed
# the IL gap, sliced 8 rows, made five completions, searched the corpus, queried
# searxng and landed five signals. The failure path persisted none of it.
#
# REPRODUCED against the live core plane (gpt-oss-120b) on the rebuilt
# synthesis prompt: 5 of 6 samples returned a bare GATHER protocol object —
# {"tool": "web_evidence", "args": {…}} — as the FINAL answer, which parses to
# a dict with no title and no body and raises that exact string. With
# GATHERING_CLOSED_CLAUSE appended to the synthesis turn, 0 of 8.
#
# So: the trace must carry the evidence (§1), the receipt must name the shape
# (§2), the synthesis turn must say gathering is closed (§3), and a hard-failed
# assigned run must not hold its claim (§4).
# ---------------------------------------------------------------------------


class _FinalTurnIsAToolCallPlanner(_AssignmentFollowingPlanner):
    """The LIVE 09-09 shape: gathers correctly, then answers with a protocol
    object instead of a finding — the whole completion, nothing else."""

    async def chat_complete(self, messages, **kwargs):
        result = await super().chat_complete(messages, **kwargs)
        if self._turn > 3:  # the synthesis turn
            result.content = json.dumps({
                "tool": "web_evidence",
                "args": {"query": "Palestine impact on Israel security",
                         "hypothesis_id": self.hypothesis_id or ""},
            })
        return result


class _AccountedPlanner(_FinalTurnIsAToolCallPlanner):
    """The same planner, reporting itself into the run account the way the REAL
    provider chokepoint does — ``record_prompt_rendered`` before the call and
    ``LLMProviderHandler._account_call`` after it, with a real ``LLMResponse``.

    The transport is the one thing a test rig cannot have; everything the
    failure trace reads is produced by the runtime's own recorders."""

    #: ``_account_call`` reads this off the handler.
    _instance_id = "llm.primary.openai_compat"

    async def chat_complete(self, messages, *, system=None, **kwargs):
        import time as _time

        from legba.data.run_accounting import record_prompt_rendered
        from legba.data.stack.llm.base import LLMProviderHandler, LLMResponse

        record_prompt_rendered(system, messages)
        started = _time.monotonic()
        result = await super().chat_complete(messages, system=system, **kwargs)
        LLMProviderHandler._account_call(
            self,
            model="core-120b", messages=messages, system=system,
            started_monotonic=started,
            response=LLMResponse(content=result.content, finish_reason="stop"),
            exc=None,
        )
        return result


async def test_the_hard_fail_trace_carries_the_prompt_and_the_completions(
    dsn, web, archive_root,
):
    """(a) THE BLIND SPOT. A run that hard-fails must leave the SAME receipt a
    successful run leaves, minus the output: the rendered prompt, every
    completion with its raw text, the phases it reached, and the tools it
    called. Live, all four were empty and the class was undiagnosable."""
    from legba.data.provenance.receipts import RuntimeReceiptChain
    from legba.data.run_accounting import (
        bind_run_accounting, reset_run_accounting,
    )
    from legba.runtime.actor_payload import _write_failure_trace

    await _sweep_backlog(dsn)
    await _dispatch_the_il_gap(dsn, datetime.now(timezone.utc))

    web()
    run_id = uuid4()
    provider = _StubProvider(_response(
        _hit("https://cleared.test/palestine", title="Palestine policy shift"),
        query="Palestine Israel policy",
    ))
    token = bind_run_accounting()
    try:
        started = datetime.now(timezone.utc)
        with pytest.raises(Exception) as raised:
            await _run_researcher(
                dsn, _AccountedPlanner(), run_id=run_id, provider=provider,
            )
        async with _pool(dsn) as pool:
            assert await _write_failure_trace(
                RuntimeReceiptChain(pool),
                run_id=run_id, analyst_id="corpus_researcher",
                analyst_version="b" * 16, cadence_trigger="schedule",
                target_id=None, exc=raised.value, bucket_kind="hard",
                attempts_made=1, max_attempts=3, run_started_at=started,
            ) is True
    finally:
        reset_run_accounting(token)

    async with _conn(dsn) as conn:
        row = await conn.fetchrow(
            "SELECT status, prompt_rendered, prompt_sha256, intermediate_steps, "
            "llm_calls, tool_calls FROM analyst_traces WHERE run_id = $1", run_id,
        )
    assert row["status"] == "failed"

    # THE PROMPT. Not NULL, and it is the SYNTHESIS prompt — the last call the
    # run made — carrying the assignment it was given.
    prompt = row["prompt_rendered"]
    assert prompt and "DISPATCHED RESEARCH ASSIGNMENT" in prompt
    assert row["prompt_sha256"]

    # THE COMPLETIONS, with their raw text. The last one IS the defect.
    calls = json.loads(row["llm_calls"])
    assert len(calls) >= 4
    assert all(c["status"] == "success" for c in calls)
    assert '"tool": "web_evidence"' in calls[-1]["completion_text"]

    # THE PHASES IT REACHED, and the tools it really called.
    steps = json.loads(row["intermediate_steps"])
    kinds = [s.get("kind") for s in steps]
    assert "render_prompt" in kinds and "inject_preamble" in kinds
    assert "tool_call" in kinds
    assert json.loads(row["tool_calls"])          # the agency ledger, not empty
    await _sweep_backlog(dsn)


async def test_the_receipt_names_the_shape_and_quotes_the_raw_head(
    dsn, web, archive_root,
):
    """(c) THE RECEIPT. The bare ``output_contract_violation`` step said only
    that the contract broke. The run must degrade LOUD with the reason NAMED
    (``final_answer_unreadable`` / ``final_turn_is_a_tool_call``), the tool it
    tried to call, and the raw head — the three facts that took a live
    reproduction to obtain."""
    from legba.data.analysts.output_contract import OutputContractError
    from legba.data.analysts.planner_action import FINAL_ANSWER_UNREADABLE
    from legba.data.run_accounting import (
        bind_run_accounting, current_steps, reset_run_accounting,
    )

    await _sweep_backlog(dsn)
    qid = await _dispatch_the_il_gap(dsn, datetime.now(timezone.utc))

    web()
    provider = _StubProvider(_response(
        _hit("https://cleared.test/palestine", title="Palestine policy shift"),
        query="Palestine Israel policy",
    ))
    token = bind_run_accounting()
    try:
        with pytest.raises(OutputContractError) as raised:
            await _run_researcher(
                dsn, _FinalTurnIsAToolCallPlanner(), run_id=uuid4(),
                provider=provider,
            )
        steps = current_steps()
    finally:
        reset_run_accounting(token)

    assert "no readable body" in str(raised.value)
    receipt = [s for s in steps if s.get("kind") == FINAL_ANSWER_UNREADABLE]
    assert len(receipt) == 1
    step = receipt[0]
    assert step["reason"] == "final_turn_is_a_tool_call"
    assert step["tool"] == "web_evidence"
    assert step["hypothesis_id"] == str(qid)
    assert '"tool": "web_evidence"' in step["raw_head"]
    assert step["raw_chars"] > 0
    await _sweep_backlog(dsn)


async def test_the_synthesis_turn_is_told_gathering_is_closed(
    dsn, web, archive_root,
):
    """(b) THE FIX, at the layer that caused it. The synthesis call reuses the
    GATHER system prompt, whose standing order is to emit a bare protocol
    object; nothing ever said the gathering was over. It does now — on the
    synthesis turn only, and only for a run that had a gather binding."""
    from legba.data.analysts.inline_target import InlineTargetDeps, run_method
    from legba.data.analysts.planner_action import GATHERING_CLOSED_CLAUSE

    await _sweep_backlog(dsn)
    await _dispatch_the_il_gap(dsn, datetime.now(timezone.utc))

    web()
    provider = _StubProvider(_response(
        _hit("https://cleared.test/palestine", title="Palestine policy shift"),
        query="Palestine Israel policy",
    ))
    planner = _AssignmentFollowingPlanner()
    await _run_researcher(dsn, planner, run_id=uuid4(), provider=provider)

    # The GATHER rounds still get the protocol and NOT the closing clause…
    assert GATHERING_CLOSED_CLAUSE not in planner.prompts[0]
    # …and the synthesis turn — the last one — gets it.
    assert GATHERING_CLOSED_CLAUSE in planner.prompts[-1]

    # A run with NO gather binding was never told the protocol, so its prompt
    # is untouched: byte-identical for every single-shot analyst.
    single = _AssignmentFollowingPlanner()
    single._turn = 3                     # straight to the finding turn
    await run_method(
        _uk_slice(), {"analyst_id": "corpus_researcher", "run_id": uuid4()},
        InlineTargetDeps(llm=single, max_tokens=512),
    )
    assert GATHERING_CLOSED_CLAUSE not in "\n".join(single.prompts)
    await _sweep_backlog(dsn)


async def test_a_hard_failed_assigned_run_does_not_hold_its_claim(
    dsn, web, archive_root,
):
    """(c) THE CLAIM RULE. A claim exists to stop a second run duplicating a
    first run's work. A run recorded ``failed`` is doing no work — it published
    nothing and the DLQ already has it — so its hold is void at the next
    settle, not 26 hours later. Live, the IL gap sat ``in_progress`` across
    three consecutive hard fails and only the reclaim timer ever freed it."""
    from legba.data.analysts.output_contract import OutputContractError
    from legba.runtime import dispatched_question as dqm

    await _sweep_backlog(dsn)
    qid = await _dispatch_the_il_gap(dsn, datetime.now(timezone.utc))

    web()
    run_id = uuid4()
    provider = _StubProvider(_response(
        _hit("https://cleared.test/palestine", title="Palestine policy shift"),
        query="Palestine Israel policy",
    ))
    with pytest.raises(OutputContractError):
        await _run_researcher(
            dsn, _FinalTurnIsAToolCallPlanner(), run_id=run_id, provider=provider,
        )
    await _write_trace(dsn, run_id=run_id)       # status='failed'

    async with _conn(dsn) as conn:
        # It IS claimed until something settles it — the claim fired at GROUND.
        assert await conn.fetchval(
            "SELECT status FROM hypotheses WHERE id = $1", qid
        ) == dqm.CLAIMED_STATUS

    async with _pool(dsn) as pool:
        counts = await dqm.settle_claimed_questions(
            pool, resolved_by="corpus_researcher",
        )
    assert counts["released"] == 1
    assert counts["reclaimed"] == 0              # nowhere near the 26h window

    async with _conn(dsn) as conn:
        assert await conn.fetchval(
            "SELECT status FROM hypotheses WHERE id = $1", qid
        ) == "open_question"

    # And it is not ALSO re-rendered as a standing claim: the release put it
    # back on the general backlog, which is where the ranker reads it.
    async with _pool(dsn) as pool:
        assert await dqm.resolve_standing_assignment(
            pool, analyst_id="corpus_researcher"
        ) is None
    await _sweep_backlog(dsn)
