# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FETCH_REVIEW (a)/(a′) through the REAL binding path.

Every test here drives ``Agency.run_pack_tool`` against a fresh migrated
Postgres — the three-way resolve, the governor, the invocation ledger, the
handler dispatch and the settle all really run, and ``action_pack_invocations``
is asserted so this counts as coverage of the BINDING and not of a helper. A
unit test of ``_egress_impersonate`` alone would not (review §5, and the
memory rule "tests must traverse the real binding path").

What it proves:

  1. **reuters.com is never connected to, flag off OR on.** Its robots.txt
     says ``# Block all other bots`` / ``User-agent: *`` / ``Disallow: /`` —
     as explicit as robots gets. The review is categorical: no fetcher option
     may be applied to reuters.com, and it asked for this as a NAMED test so
     nobody has to rediscover §2. Robots is checked BEFORE any page connect,
     so the page URL must never appear in the transport's log.
  2. **A block reads as a block, not as an empty web.** A 403 with a
     Cloudflare interstitial, and a 200 carrying a DataDome one, both surface
     ``blocked_by_challenge`` on the tool output, ``depth_reason=fetch_blocked``
     on the teaser the planner reads, and the tell on the landed row's payload.
     Before (a′) both landed as ``fetch_failed`` / ``no_main_text_extracted``,
     i.e. indistinguishable from "nothing was published".
  3. **A genuine article is untouched by any of it** — still full_text, still
     archived, no false block.
  4. **The licence gate is NOT touched.** A blocked host with no affirmative
     licence class is a teaser either way; the block is reported, and the
     depth rule stays exactly where ``depth_for_license`` put it. That gate is
     the operator's (review §1), and this lane does not move it.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from legba.data.analysts.agency import (
    Agency,
    TargetScopeView,
    ToolCall,
    ToolContext,
    WritebackContext,
)
from legba.data.analysts.agency import research_tools, robots as robots_mod
from legba.data.analysts.deterministic_handlers._challenge_detect import (
    BLOCKED_BY_CHALLENGE,
)
from legba.data.provenance import AnalystContext
from legba.data.research_evidence import (
    DEPTH_FULL_TEXT,
    DEPTH_TEASER,
    REASON_FETCH_BLOCKED,
)
from legba.data.research_flag import RESEARCH_EVIDENCE_ENV, RESEARCH_SUBSTRATE
from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.data.sources._egress import FETCH_IMPERSONATE_ENV
from legba.data.stack.search import SearchResponse, SearchResult

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Bodies — reconstructed from the tells FETCH_REVIEW §2 measured live.
# ---------------------------------------------------------------------------

ARTICLE_HTML = (
    "<html><head><title>Fuel embargo</title></head><body><article>"
    "<p>Fuel deliveries into Bamako fell by four fifths this week as JNIM "
    "roadblocks held on the three southern corridors, according to two "
    "haulage operators reached by telephone in the capital.</p>"
    "<p>The national fuel importers' association said reserves would last "
    "eleven days at current rationing levels, and that the government had "
    "begun releasing strategic stocks to hospitals.</p>"
    "</article></body></html>"
)

#: Cloudflare's "Just a moment..." interstitial (AP's was 5 507 B at 403).
CLOUDFLARE_HTML = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<div id=\"cf-wrapper\"><h1>apnews.com</h1>"
    "<p>Verifying you are human. This may take a few seconds.</p></div>"
    "<script src=\"/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1\">"
    "</script></body></html>"
)

#: DataDome's interstitial served with **200**, which is the case that used to
#: read as "the web had nothing": bytes landed, extraction found no article.
DATADOME_HTML = (
    "<html><head><title>news</title></head><body>"
    "<p>Please enable JS and disable any ad blocker</p>"
    "<script>var dd={'rt':'i','cid':'AHrlqAAAAA','hsh':'2211'}</script>"
    "</body></html>"
)

ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"
#: reuters.com's own, verbatim in shape (review §2, line 127 of their file).
ROBOTS_BLOCK_ALL = "# Block all other bots\nUser-agent: *\nAllow: /plus/\nDisallow: /\n"


# ---------------------------------------------------------------------------
# Fixtures — the same shape the R-A e2e uses (that file is NOT edited).
# ---------------------------------------------------------------------------


@pytest.fixture
def dsn(migrated_pg):
    return migrated_pg.dsn


@asynccontextmanager
async def _pool(dsn):
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        yield p
    finally:
        await p.close()


@asynccontextmanager
async def _conn(dsn):
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        await conn.close()


#: Every host this file touches. The cleanup below is SCOPED to these, and
#: deliberately not the unscoped ``DELETE FROM source_credibility`` the older
#: R-A e2e uses: that delete commits on the shared session-scoped test DB and
#: needs a restore fixture to undo, which is the 09-05 merge-wave ordering
#: pollution. A file that only ever owns its own hosts needs neither.
_OWNED_HOSTS: tuple[str, ...] = (
    "www.reuters.com", "apnews.com",
    "blocked.test", "cleared.test", "unreviewed.test",
)


@pytest_asyncio.fixture(autouse=True)
async def _reset_db(dsn):
    async with _conn(dsn) as conn:
        like = [f"%//{h}/%" for h in _OWNED_HOSTS]
        await conn.execute(
            "DELETE FROM evidence_archive WHERE signal_id IN ("
            "  SELECT id FROM signals WHERE canonical_url LIKE ANY($1::text[]))",
            like,
        )
        await conn.execute(
            "DELETE FROM signals WHERE canonical_url LIKE ANY($1::text[])", like,
        )
        await conn.execute(
            "DELETE FROM source_credibility WHERE source_host = ANY($1::text[])",
            list(_OWNED_HOSTS),
        )
    yield


@pytest.fixture(autouse=True)
def _archive_root(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))


@pytest.fixture(autouse=True)
def _substrate_regime(monkeypatch):
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_SUBSTRATE)


@pytest.fixture(autouse=True)
def _flag_off_by_default(monkeypatch):
    """The SHIPPED default. Individual tests opt in to ``on`` explicitly."""
    monkeypatch.delenv(FETCH_IMPERSONATE_ENV, raising=False)


@pytest.fixture(autouse=True)
def _no_dns_egress(monkeypatch):
    """Skip getaddrinfo for the fictional test hosts, keep the IP-literal check.

    Same shim (and same reasoning) as the R-A e2e's: the SSRF pre-check
    resolves before connect, and these hosts do not resolve.
    """
    import ipaddress

    from legba.data.sources._egress import EgressBlockedError, _ip_blocked

    def _fake(host, port):
        if not host:
            raise EgressBlockedError("egress blocked: empty host")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            return
        if _ip_blocked(literal):
            raise EgressBlockedError(f"egress blocked: {host} is a non-public address")

    monkeypatch.setattr(research_tools, "assert_public_host", _fake)


@pytest.fixture
def web(monkeypatch):
    """Serve robots.txt + one page through a mock transport, recording URLs.

    Installed on the page fetch (``research_tools.guarded_async_client``, which
    ``fetch_client`` is handed at the call site) and on the robots fetch.
    """
    seen: list[str] = []

    def _install(*, robots_body=ROBOTS_ALLOW_ALL, page_status=200,
                 page_body=ARTICLE_HTML):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=robots_body)
            return httpx.Response(
                page_status, text=page_body,
                headers={"content-type": "text/html; charset=utf-8"},
            )

        def fake_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            kwargs.pop("guarded", None)
            return httpx.AsyncClient(**kwargs)

        monkeypatch.setattr(research_tools, "guarded_async_client", fake_client)
        monkeypatch.setattr(robots_mod, "guarded_async_client", fake_client)
        return seen

    return _install


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
    component_version = "feedfacefeedface"
    component_id = "search.searxng.local"

    def __init__(self, response: SearchResponse) -> None:
        self.response = response
        self.queries: list[str] = []

    async def search(self, query, *, limit=5, params=None):
        self.queries.append(query)
        return self.response


def _hit(url: str) -> SearchResult:
    return SearchResult(
        url=url,
        title="Fuel embargo tightens on the Bamako corridors",
        snippet="Deliveries fell sharply as roadblocks held.",
        engine="duckduckgo", rank=1, published_at="2026-09-04T06:00:00Z",
    )


def _response(url: str) -> SearchResponse:
    return SearchResponse(
        query="mali fuel embargo", results=[_hit(url)],
        provider="search.searxng.local", subprovider="searxng",
    )


async def _clear_host(dsn, host: str, license_class: str = "cc_by") -> None:
    """Give a host an AFFIRMATIVE licence class so full_text depth is even
    attempted. Without it ``depth_for_license`` stops at teaser and no fetch
    happens at all — which is the review's §1 point, and is why the licence
    gate is not what this lane is testing."""
    async with _conn(dsn) as conn:
        await conn.execute(
            "INSERT INTO source_credibility (source_host, score, scored_by, "
            "license_class) VALUES ($1, 0.9, 'operator.test', $2) "
            "ON CONFLICT (source_host) DO UPDATE SET license_class = $2",
            host, license_class,
        )


async def _run(dsn, provider, *, budget_account=None):
    """One call through the REAL agency gate."""
    async with _pool(dsn) as pool:
        ctx = ToolContext(search=provider)
        ctx.writeback = WritebackContext(
            pg_pool=pool,
            analyst_ctx=AnalystContext(
                analyst_id="corpus_researcher", analyst_version="b" * 16,
                run_id=uuid4(), target_id=None, target_version=None,
            ),
        )
        call = ToolCall(
            pack_id="research", tool_name="web_evidence",
            args={"query": "mali fuel embargo"},
            requested_by="analyst::corpus_researcher",
            budget_account=budget_account or f"acct-{uuid4().hex[:8]}",
        )
        async with _conn(dsn) as conn:
            return await Agency().run_pack_tool(
                conn, pack=_pack(), call=call,
                analyst_grants=[ActionPackRef(pack_id="research")],
                target_allows=[ActionPackRef(pack_id="research")],
                scope=TargetScopeView(target_id="__global__"), ctx=ctx,
            )


# ---------------------------------------------------------------------------
# 1. reuters.com — closed by robots, flag off AND on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [None, "on", "fallback"])
async def test_reuters_com_is_refused_by_robots_and_never_connected_to(
    dsn, web, monkeypatch, flag,
):
    """``# Block all other bots`` is an ANSWER, not an obstacle.

    No fetcher option in FETCH_REVIEW may be applied to reuters.com. Robots is
    evaluated before any page connect, so with the flag in ANY state the only
    URL the transport ever sees is ``/robots.txt``.
    """
    if flag is not None:
        monkeypatch.setenv(FETCH_IMPERSONATE_ENV, flag)
    await _clear_host(dsn, "www.reuters.com")
    url = "https://www.reuters.com/world/middle-east/some-story-2026-09-16/"
    seen = web(robots_body=ROBOTS_BLOCK_ALL)
    provider = _StubProvider(_response(url))

    outcome = await _run(dsn, provider)

    assert outcome.tool_result.status == "completed"
    out = outcome.tool_result.output
    assert out["refused_robots"] == 1
    # The PAGE was never requested — only robots.txt.
    assert seen and all(u.endswith("/robots.txt") for u in seen), seen
    assert url not in seen
    # A robots refusal is not a block we could fix, and must not be counted
    # as one: the impersonation flag has nothing to say about it.
    assert out[BLOCKED_BY_CHALLENGE] == 0
    # It lands as a teaser with the robots reason, not as a fetch failure.
    assert out["teaser_hits"][0]["depth_reason"] == "robots_disallowed"


# ---------------------------------------------------------------------------
# 2. (a′) a block reads as a block
# ---------------------------------------------------------------------------


async def test_a_403_interstitial_surfaces_blocked_by_challenge(dsn, web):
    """AP's case: the edge answers 403 and ``raise_for_status`` discards the
    body inside the streaming fetch. The STATUS alone is enough."""
    await _clear_host(dsn, "apnews.com")
    url = "https://apnews.com/article/mali-fuel-embargo-abc123"
    web(page_status=403, page_body=CLOUDFLARE_HTML)
    provider = _StubProvider(_response(url))

    outcome = await _run(dsn, provider)
    out = outcome.tool_result.output

    assert out[BLOCKED_BY_CHALLENGE] == 1
    assert out["fetch_failed"] == 1
    teaser = out["teaser_hits"][0]
    assert teaser["depth"] == DEPTH_TEASER
    assert teaser["depth_reason"] == REASON_FETCH_BLOCKED
    assert teaser[BLOCKED_BY_CHALLENGE] == "http_403"
    # The durable carry: a later reader of the row, not just this planner.
    async with _conn(dsn) as conn:
        payload = await conn.fetchval(
            "SELECT payload->'research' FROM signals WHERE canonical_url = $1", url,
        )
    import json as _json
    assert _json.loads(payload)[BLOCKED_BY_CHALLENGE] == "http_403"


async def test_a_200_interstitial_surfaces_blocked_by_challenge(dsn, web):
    """THE false-absence case. 200 + a DataDome body: bytes landed, extraction
    found no article. Before (a′) this was ``no_main_text_extracted`` — which
    a planner reads as "the web had nothing"."""
    await _clear_host(dsn, "blocked.test")
    url = "https://blocked.test/article/1"
    web(page_status=200, page_body=DATADOME_HTML)
    provider = _StubProvider(_response(url))

    outcome = await _run(dsn, provider)
    out = outcome.tool_result.output

    assert out[BLOCKED_BY_CHALLENGE] == 1
    teaser = out["teaser_hits"][0]
    assert teaser["depth_reason"] == REASON_FETCH_BLOCKED
    assert teaser[BLOCKED_BY_CHALLENGE] == "enable js and disable any ad blocker"
    # No bytes were kept and nothing was numbered — the demotion is unchanged.
    assert out["archived"] == 0


async def test_a_genuine_article_is_not_blocked_and_still_reaches_full_text(dsn, web):
    """The no-regression half: (a′) must not turn a real page into a block."""
    await _clear_host(dsn, "cleared.test")
    url = "https://cleared.test/article/1"
    web(page_status=200, page_body=ARTICLE_HTML)
    provider = _StubProvider(_response(url))

    outcome = await _run(dsn, provider)
    out = outcome.tool_result.output

    assert out[BLOCKED_BY_CHALLENGE] == 0
    assert out["archived"] == 1
    assert out["rows"][0]["source"]["research_depth"] == DEPTH_FULL_TEXT
    assert "Bamako" in out["rows"][0]["source"]["raw_body"]


async def test_the_licence_gate_is_untouched_by_the_block_report(dsn, web):
    """An UNREVIEWED host is a teaser whether or not it blocked us, and the
    tool never fetched it. ``depth_for_license`` is the operator's gate; this
    lane reports blocks, it does not widen depth."""
    url = "https://unreviewed.test/article/1"   # no source_credibility row
    seen = web(page_status=403, page_body=CLOUDFLARE_HTML)
    provider = _StubProvider(_response(url))

    outcome = await _run(dsn, provider)
    out = outcome.tool_result.output

    assert out["teaser"] == 1
    assert out["teaser_hits"][0]["depth_reason"] == "license_unreviewed"
    assert out[BLOCKED_BY_CHALLENGE] == 0
    assert seen == []  # never fetched, not even robots.txt


# ---------------------------------------------------------------------------
# 3. This really is the binding path
# ---------------------------------------------------------------------------


async def test_the_call_lands_in_action_pack_invocations(dsn, web):
    """``action_pack_invocations`` is the audit the memory rule names: if this
    row is absent, the test drove a helper and not the bound tool."""
    await _clear_host(dsn, "cleared.test")
    web(page_status=200, page_body=ARTICLE_HTML)
    account = f"acct-{uuid4().hex[:8]}"
    provider = _StubProvider(_response("https://cleared.test/article/1"))

    outcome = await _run(dsn, provider, budget_account=account)
    assert outcome.admitted is True

    async with _conn(dsn) as conn:
        rows = await conn.fetch(
            "SELECT tool_name, outcome, budget_account FROM "
            "action_pack_invocations WHERE tool_name = 'web_evidence'",
        )
    assert rows, "no invocation ledger row — the bound tool never ran"
    assert rows[-1]["outcome"] == "completed"
