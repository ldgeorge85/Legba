# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-A — slice reachability (the flag), and the GATHER citation path.

Two things this file proves that the builder/handler suites cannot:

  * THE FLAG'S SLICE EFFECT, both directions (G6) and byte-identity (G2). A
    geo-matching research signal is ABSENT from a desk's slice at
    ``substrate`` and PRESENT at ``desks``; at ``desks`` the slice SQL carries
    no ``retrieval_origin`` clause at all, so the pre-R-A read is byte-for-byte
    intact. The dilution bound (G7) holds even at ``desks``.

  * THE GATHER CITATION PATH, through the REAL binding (G8). A GATHER round
    that calls ``web_evidence`` routes ``Agency.run_pack_tool → the research
    pack binding → the tool``; the tool LANDS a full-text row and returns it in
    ``rows``; and the row enters the finding's citation list numbered ``[N]``
    with a real ``source_text``. Before R-A a fetched page was counted at
    ``inline_target.py:1415`` and dropped — this is the fix, exercised end to
    end rather than by a direct function call.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import httpx
import pytest

from legba.data.analysts.agency import (
    Agency,
    AgencyToolBinding,
    GLOBAL_SCOPE,
    ToolContext,
    WritebackContext,
)
from legba.data.analysts.agency import research_tools, robots as robots_mod
from legba.data.analysts.inline_target import (
    InlineTargetDeps,
    _build_citation_index,
    _extract_citations,
    _gather,
)
from legba.data.provenance import AnalystContext
from legba.data import research_evidence as rev
from legba.data.research_flag import (
    RESEARCH_EVIDENCE_ENV,
    RESEARCH_DESKS,
    RESEARCH_OFF,
    RESEARCH_SLICE_EXCLUSION_SQL,
    RESEARCH_SUBSTRATE,
)
from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.stack.search import SearchResponse, SearchResult
from legba.runtime.dapr_actors import _read_substrate_slice


# The ``restore_source_credibility_seed`` fixture (tests/data_pkg/conftest.py)
# re-applies the baseline source_credibility rows onto an open connection —
# see its docstring for why (09-05 merge-wave: this file's reset fixture runs
# an unscoped ``DELETE FROM source_credibility`` that commits on the shared
# test DB).

pytestmark = [pytest.mark.asyncio]

_ARTICLE = (
    "<html><body><article><p>Fuel deliveries into the capital fell by four "
    "fifths this week as roadblocks held on the southern corridors, two haulage "
    "operators said.</p></article></body></html>"
)


# ---------------------------------------------------------------------------
# 1) SLICE SQL SHAPE — no DB (the clause is present/absent per flag)
# ---------------------------------------------------------------------------


class _CapturingConn:
    """Records the SQL passed to ``fetch`` (returns [] each time) and serves a
    canned target body from ``fetchrow`` so both slice paths build."""

    def __init__(self, target_body=None):
        self.sqls: list[str] = []
        self._body = target_body

    async def fetch(self, sql, *a, **k):
        self.sqls.append(sql)
        return []

    async def fetchrow(self, *a, **k):
        return {"body": self._body} if self._body is not None else None

    def signals_sql(self) -> str:
        for sql in self.sqls:
            if "FROM signals" in sql:
                return sql
        raise AssertionError("no FROM signals fetch captured")


def _descriptor() -> AnalystDescriptor:
    return AnalystDescriptor.model_validate(
        {
            "identity": {
                "id": "slice_probe", "name": "Slice Probe",
                "schema_uri": "legba/analyst/1.0.0", "version": "0" * 16,
                "kind": "inline_target",
                "type_signature": {
                    "input_type": "legba.runtime.SignalList",
                    "output_type": "legba.runtime.Finding",
                },
                "state": "active", "owner": "t",
            },
            "subscription": {"substrate": {"direct_queries": False}},
            "method": {
                "kind": "llm_planner",
                "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
                "llm": {"primary": {"factory_kind": "stack_ref", "raw": "llm.x",
                                    "expected_family": "llm_provider"}},
            },
            "cadence": {"fallback_schedule": "0 */6 * * *"},
        },
        strict=False,
    )


async def _slice_sql(target_body=None) -> str:
    conn = _CapturingConn(target_body=target_body)
    await _read_substrate_slice(
        conn, descriptor=_descriptor(),
        target_filter=("country_watch_il" if target_body else None),
    )
    return conn.signals_sql()


@pytest.mark.parametrize("mode", [RESEARCH_OFF, RESEARCH_SUBSTRATE])
async def test_slice_excludes_research_at_off_and_substrate(mode, monkeypatch):
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, mode)
    sql = await _slice_sql(target_body={"scope": {"geo": ["IL"]}})
    assert RESEARCH_SLICE_EXCLUSION_SQL in sql


async def test_slice_at_desks_is_byte_identical_no_origin_clause(monkeypatch):
    """G2 — at ``desks`` the read carries NO retrieval_origin clause, so the
    slice SQL is byte-for-byte what it was before R-A (the only change to this
    query is a clause added when the flag is NOT desks)."""
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_DESKS)
    sql = await _slice_sql(target_body={"scope": {"geo": ["IL"]}})
    assert "retrieval_origin" not in sql
    assert RESEARCH_SLICE_EXCLUSION_SQL not in sql
    # …and the META (no-target) path likewise.
    meta = await _slice_sql(target_body=None)
    assert "retrieval_origin" not in meta


async def test_off_and_desks_slice_sql_differ_by_exactly_the_one_clause(monkeypatch):
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_DESKS)
    desks = await _slice_sql(target_body={"scope": {"geo": ["IL"]}})
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_OFF)
    off = await _slice_sql(target_body={"scope": {"geo": ["IL"]}})
    # Removing the one appended clause (with its AND joiner) from the off-SQL
    # yields the desks-SQL EXACTLY — the flag's only footprint on this query.
    assert off != desks
    assert off.replace(" AND " + RESEARCH_SLICE_EXCLUSION_SQL, "") == desks


# ---------------------------------------------------------------------------
# DB helpers (all pools/connections live in the test's own loop)
# ---------------------------------------------------------------------------


@pytest.fixture
def dsn(migrated_pg):
    return migrated_pg.dsn


@asynccontextmanager
async def _conn(dsn):
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        await conn.close()


@asynccontextmanager
async def _pool(dsn):
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture(autouse=True)
async def _reset(dsn, restore_source_credibility_seed):
    async with _conn(dsn) as conn:
        await conn.execute("DELETE FROM evidence_archive")
        await conn.execute("DELETE FROM signals")
        await conn.execute("DELETE FROM source_credibility")
        await conn.execute("DELETE FROM target_descriptors")
    yield
    # RESTORE the canonical source_credibility seed — the unscoped delete above
    # commits on the shared test DB, so leaving it wiped poisons every later
    # source_credibility test in the suite (09-05 merge-wave ordering pollution).
    async with _conn(dsn) as conn:
        await restore_source_credibility_seed(conn)


async def _insert_curated(conn, url, *, geo=("IL",), source_id="source.wire",
                          age_seconds=0):
    await conn.execute(
        "INSERT INTO signals (source_id, canonical_url, geo, payload, fetched_at) "
        "VALUES ($1, $2, $3::text[], '{}'::jsonb, "
        "now() - make_interval(secs => $4))",
        source_id, url, list(geo), age_seconds,
    )


async def _insert_research(conn, url, *, provider="search.searxng.local", geo=("IL",)):
    hit = rev.ResearchHit(url=url, title="t", snippet="s", text="body text", query="q")
    row = rev.build_research_row(
        hit,
        rev.ProviderContext(component_id=provider, subprovider="searxng"),
        rev.ResearchDispatch(kind="open_question", target_id="country_watch_il", geo=tuple(geo)),
        run_id=uuid4(), requested_by="analyst::corpus_researcher",
        regime=RESEARCH_SUBSTRATE, fetched_at=datetime.now(timezone.utc),
    )
    return await rev.land_research_row(conn, row)


async def _insert_target_il(conn):
    await conn.execute(
        "INSERT INTO target_descriptors (descriptor_id, version, is_head, "
        "schema_uri, owner, name, body) VALUES "
        "($1, $2, TRUE, 'legba/target/1.0.0', 'test', 'IL', $3::jsonb)",
        "country_watch_il", "c" * 16, json.dumps({"scope": {"geo": ["IL"]}}),
    )


# ---------------------------------------------------------------------------
# 2) SLICE REACHABILITY — both directions, live (G6)
# ---------------------------------------------------------------------------


async def _read_il_slice(dsn):
    async with _conn(dsn) as conn:
        return await _read_substrate_slice(
            conn, descriptor=_descriptor(), target_filter="country_watch_il",
        )


async def test_research_row_absent_at_substrate_present_at_desks(dsn, monkeypatch):
    async with _conn(dsn) as conn:
        await _insert_target_il(conn)
        await _insert_curated(conn, "https://wire.test/a")
        rid = await _insert_research(conn, "https://research.test/b")
    assert rid is not None

    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_SUBSTRATE)
    # The slice can carry non-signal graph-structure context rows (id=None / no
    # id key) alongside signals — key on the resolvable id only.
    ids = {str(r.get("id")) for r in await _read_il_slice(dsn) if r.get("id")}
    assert str(rid) not in ids, "research is EXCLUDED from the desk slice at substrate"
    # …the curated IL signal is still there (the exclusion is origin-scoped).
    assert any("wire.test" in (r.get("source_url") or "") for r in await _read_il_slice(dsn))

    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_DESKS)
    ids_desks = {str(r.get("id")) for r in await _read_il_slice(dsn) if r.get("id")}
    assert str(rid) in ids_desks, "research REACHES the desk slice at desks"


async def test_research_geo_must_match_the_target(dsn, monkeypatch):
    """A research row for a DIFFERENT country never reaches the IL desk, flag
    regardless — geo is the reachability key."""
    async with _conn(dsn) as conn:
        await _insert_target_il(conn)
        rid = await _insert_research(conn, "https://research.test/ca", geo=("CA",))
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_DESKS)
    ids = {str(r.get("id")) for r in await _read_il_slice(dsn) if r.get("id")}
    assert str(rid) not in ids


async def test_dilution_bound_holds_even_at_desks(dsn, monkeypatch):
    """G7 — one synthetic source per provider means research can occupy at most
    ``LEGBA_GLOBAL_SLICE_PER_SOURCE_CAP`` (15) rows of a 120-row geo slice, by
    construction, under a deliberately flooded fixture."""
    async with _conn(dsn) as conn:
        await _insert_target_il(conn)
        # 150 curated rows across 150 DISTINCT sources (each under the cap, so
        # they legitimately fill a 120-row slice) — made slightly OLDER so the
        # research rows are the freshest and are walked FIRST, where the
        # per-source cap bites hardest.
        for i in range(150):
            await _insert_curated(
                conn, f"https://wire.test/{i}", source_id=f"source.wire.{i}",
                age_seconds=60,
            )
        # 40 research rows, ALL one synthetic source — the whole point.
        for i in range(40):
            await _insert_research(conn, f"https://research.test/flood/{i}")
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_DESKS)
    rows = await _read_il_slice(dsn)
    signal_rows = [r for r in rows if r.get("source_id")]
    research = [r for r in signal_rows if rev.is_research_source_id(r["source_id"])]
    assert len(signal_rows) >= 100, "the slice must be full for the bound to be meaningful"
    assert len(research) <= 15, (
        f"research took {len(research)} of {len(rows)} slice rows — the "
        "per-source dilution cap must bound it to 15"
    )
    assert research, "at desks, SOME research should reach the slice"


# ---------------------------------------------------------------------------
# 3) THE GATHER CITATION PATH — the REAL binding (G8)
# ---------------------------------------------------------------------------


class _StubProvider:
    component_id = "search.searxng.local"
    component_version = "feedfacefeedface"

    def __init__(self, response):
        self.response = response

    async def search(self, query, *, limit=5, params=None):
        return self.response


class _ScriptedLLM:
    subprovider = "openai"

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kwargs):
        self.calls.append({"system": system, "messages": messages})
        content = self._scripted.pop(0) if self._scripted else '{"done": true}'

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


def _research_pack() -> ActionPack:
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


@pytest.fixture
def web(monkeypatch):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200, text=_ARTICLE,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    def fake_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(**kwargs)

    monkeypatch.setattr(research_tools, "guarded_async_client", fake_client)
    monkeypatch.setattr(robots_mod, "guarded_async_client", fake_client)

    import ipaddress
    from legba.data.sources._egress import EgressBlockedError, _ip_blocked

    def _no_dns(host, port):
        try:
            lit = ipaddress.ip_address(host)
        except ValueError:
            return
        if _ip_blocked(lit):
            raise EgressBlockedError("blocked")

    monkeypatch.setattr(research_tools, "assert_public_host", _no_dns)


async def test_web_evidence_hit_enters_the_finding_citation_list(dsn, web, monkeypatch, tmp_path):
    """The whole point of R-A, exercised through the REAL GATHER binding.

    A scripted GATHER round calls ``web_evidence``; that routes through a REAL
    ``AgencyToolBinding`` → ``Agency.run_pack_tool`` → the tool, which lands a
    full-text row (a licence-cleared host) and returns it in ``rows``. The row
    must then be numbered ``[N]`` in the gathered context AND resolve in
    ``_extract_citations`` — the fix for the silent drop at inline_target:1415.
    """
    monkeypatch.setenv(RESEARCH_EVIDENCE_ENV, RESEARCH_SUBSTRATE)
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "arch"))
    async with _conn(dsn) as conn:
        await conn.execute(
            "INSERT INTO source_credibility (source_host, score, scored_by, "
            "license_class) VALUES ('cleared.test', 0.9, 'op', 'cc_by')"
        )

    provider = _StubProvider(SearchResponse(
        query="fuel embargo", provider="search.searxng.local", subprovider="searxng",
        results=[SearchResult(
            url="https://cleared.test/story", title="Fuel embargo tightens",
            snippet="Deliveries fell.", rank=1,
        )],
    ))

    async with _pool(dsn) as pool:
        research_binding = AgencyToolBinding(
            agency=Agency(),
            pack=_research_pack(),
            pg_pool=pool,
            tool_context=ToolContext(
                search=provider,
                writeback=WritebackContext(
                    pg_pool=pool,
                    analyst_ctx=AnalystContext(
                        analyst_id="corpus_researcher", analyst_version="b" * 16,
                        run_id=uuid4(), target_id=None, target_version=None,
                    ),
                ),
            ),
            analyst_grants=[ActionPackRef(pack_id="research")],
            target_allows=[ActionPackRef(pack_id="research")],
            scope=GLOBAL_SCOPE,
            requested_by="analyst::corpus_researcher",
            budget_account="research",
        )
        llm = _ScriptedLLM([
            '{"tool": "web_evidence", "args": {"query": "fuel embargo"}}',
            '{"done": true}',
        ])
        deps = InlineTargetDeps(llm=llm, max_rounds=2)

        # A read binding double for the loop's read leg (never invoked here —
        # the script only calls web_evidence, a web tool routed via tool_bindings).
        class _ReadBinding:
            async def run_tool(self, *a, **k):  # pragma: no cover
                raise AssertionError("no read tool was scripted")

        (
            gathered_context, _usage, refs, _steps, citation_extension,
        ) = await _gather(
            deps,
            binding=_ReadBinding(),
            user_prompt="p",
            target_id=None,
            analyst_id="corpus_researcher",
            steps=[],
            tool_bindings={"web_evidence": research_binding},
            base_offset=2,  # pretend two slice signals already numbered [1],[2]
        )

    # The web hit was LANDED and NUMBERED [3] — it did not silently drop.
    assert set(citation_extension.keys()) == {3}
    entry = citation_extension[3]
    landed_id = entry["signal_id"]
    assert landed_id
    # It carries a REAL body (the archived article) — never a title-only
    # citation, which would demote the finding (G8).
    assert entry["source_text"] and "haulage operators" in entry["source_text"]
    assert "[3]" in gathered_context

    # …and it is the row the tool actually wrote, tagged as research.
    async with _conn(dsn) as conn:
        row = await conn.fetchrow(
            "SELECT retrieval_origin, object_ref FROM signals WHERE id = $1::uuid",
            landed_id,
        )
    assert row["retrieval_origin"] == "web_search:search.searxng.local"
    assert row["object_ref"].startswith("cas:sha256/")  # full-text ⇒ archived

    # THE DROP FIX: merged into the slice index, a [3] marker in the prose now
    # RESOLVES (before R-A it was counted at inline_target:1415 and dropped).
    index = _build_citation_index([])  # no slice rows; the extension carries [3]
    for n, e in citation_extension.items():
        index.setdefault(n, e)
    citations, marker_count, resolved = _extract_citations(
        "The corridor reporting is corroborated [3].", index,
    )
    assert marker_count == 1 and resolved == 1
    assert citations[0]["signal_id"] == landed_id
