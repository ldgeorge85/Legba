# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The raw readers' ``known_at`` record-time cut (pure; fakes only).

Pinned here:

* the classification is COMPLETE: arm B's raw pack (descriptor, runtime variant)
  and :data:`KNOWN_AT_CUTS` name the same tools, every ``cut`` reader takes
  ``known_at`` on the port AND the Protocol, and the dispatcher refuses an
  unclassified tool under a cut — a new raw reader without a classified as-of
  predicate fails here and is refused at run time;
* the predicate is the temporal leaf's existing half-open anchor window;
* with no ``known_at`` the rendered SQL is byte-identical to the base (sha256 of
  the statement rendered at a99c1eb9);
* each reader drops / refuses rows that entered the record at or after K;
* D22: the series reads dispatch through the production router.

The database-backed proof (a row created after K is invisible) is
``test_raw_reader_known_at_db.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import yaml

from legba.data import raw_reader_known_at as K
from legba.data.analysts.consult_bakeoff import BAKEOFF_READ_PACKS, RAW_PACK_ID
from legba.data.analysts.consult_substrate_port import SubstrateQueryPort
from legba.data.analysts.consult_tool_routing import _KNOWN_TOOLS, _dispatch_tool
from legba.runtime import substrate_corpus_readers as corpus
from legba.runtime import substrate_known_at as cut
from legba.runtime import substrate_source_health as sh
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort as Port

ROOT = pathlib.Path(__file__).resolve().parents[2]
KAT = datetime(2026, 11, 5, 7, tzinfo=timezone.utc)
PRE = "11111111-1111-1111-1111-111111111111"
POST = "22222222-2222-2222-2222-222222222222"


class _Rec:
    """A pool + connection that records statements and answers the
    confirmation query with the ids listed in ``recorded``."""

    def __init__(self, recorded: set[str] | None = None) -> None:
        self.sql: list[tuple[str, tuple]] = []
        self.recorded = recorded or set()

    def acquire(self) -> "_Rec":
        return self

    async def __aenter__(self) -> "_Rec":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def fetch(self, sql: str, *a: Any) -> list:
        self.sql.append((sql, a))
        if sql.startswith("SELECT id::text AS id FROM signals WHERE id = ANY"):
            return [{"id": i} for i in a[0] if i in self.recorded]
        return []

    async def fetchval(self, sql: str, *a: Any) -> Any:
        self.sql.append((sql, a))
        return ""

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self.sql.append((sql, a))
        return None


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 1. The classification is complete, and enforced
# ---------------------------------------------------------------------------


def test_raw_pack_tools_are_exactly_the_classified_set():
    body = yaml.safe_load((ROOT / "descriptors" / "action_pack_substrate_read_raw.yaml").read_text())
    descriptor_tools = {t["name"] for t in body["tools"]}
    assert descriptor_tools == set(BAKEOFF_READ_PACKS[RAW_PACK_ID].tools)
    assert set(K.RAW_KNOWN_AT_CUTS) == descriptor_tools
    for name, c in K.KNOWN_AT_CUTS.items():
        assert c.kind in (K.CUT, K.EXEMPT), name
        assert c.how.strip(), name


@pytest.mark.parametrize("tool", sorted(K.cut_tools()))
def test_every_cut_reader_takes_known_at_on_the_port_and_the_protocol(tool):
    assert "known_at" in inspect.signature(getattr(Port, tool)).parameters
    assert "known_at" in inspect.signature(getattr(SubstrateQueryPort, tool)).parameters


def test_the_predicate_is_the_temporal_leafs_anchor_window():
    from legba.runtime.substrate_temporal import anchor_window_clause

    assert cut.recorded_before_sql("created_at", 2) == anchor_window_clause(
        "created_at", since_param=None, until_param=2) == "created_at < $2"


class _Port:
    """Records the kwargs every reader was called with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        async def reader(**kw: Any) -> dict:
            self.calls.append((name, kw))
            return {"rows": [], "refs": [], "count": 0}
        return reader


def test_dispatch_refuses_an_unclassified_tool_under_a_cut():
    p = _Port()
    for name in sorted(_KNOWN_TOOLS - set(K.KNOWN_AT_CUTS)):
        out = asyncio.run(_dispatch_tool(p, name=name, args={}, scope_predicate=None, known_at=KAT))
        assert out["error"].startswith("no_known_at_cut"), name
    assert p.calls == []


@pytest.mark.parametrize("tool,args", [
    ("search_signals", {"query": "x"}), ("search_corpus", {"query": "x"}),
    ("read_document", {"doc_id": PRE}), ("vector_search", {"query": "x"}),
    ("list_sources", {}),
])
def test_dispatch_forwards_known_at_to_cut_readers_never_from_args(tool, args):
    p = _Port()
    later = (KAT + timedelta(days=9)).isoformat()
    asyncio.run(_dispatch_tool(p, name=tool, args={**args, "known_at": later},
                               scope_predicate=None, known_at=KAT))
    assert p.calls[0][0] == tool and p.calls[0][1]["known_at"] == KAT


def test_dispatch_runs_an_exempt_reader_without_the_kwarg():
    p = _Port()
    asyncio.run(_dispatch_tool(p, name="list_targets", args={}, scope_predicate=None, known_at=KAT))
    assert p.calls == [("list_targets", {"active_only": True})]


def test_without_known_at_no_reader_is_handed_the_kwarg():
    p = _Port()
    for tool in sorted(K.KNOWN_AT_CUTS):
        asyncio.run(_dispatch_tool(p, name=tool, args={"query": "x", "doc_id": PRE}, scope_predicate=None))
    assert p.calls and all("known_at" not in kw for _, kw in p.calls)


# ---------------------------------------------------------------------------
# 2. Byte-identical without a cut; the cut in the statement with one
# ---------------------------------------------------------------------------

#: sha256 of the statements rendered by the base (a99c1eb9), no known_at.
_BASE_SEARCH_SIGNALS = "a281e342e7e55b7dc5076b5629506309922ef9ddbb2cc6229c288d725968fa8b"
_BASE_LIST_SOURCES = "2ebf7c7096803ae178ed67641268fa7bcdc3cda34eb85d7a97fb52278911baf0"


def test_search_signals_sql_unchanged_without_and_cut_with_known_at():
    r = _Rec()
    asyncio.run(Port(pg_pool=r, qdrant_client=None).search_signals(query="iran drone"))
    assert _sha(r.sql[0][0]) == _BASE_SEARCH_SIGNALS
    r = _Rec()
    out = asyncio.run(Port(pg_pool=r, qdrant_client=None).search_signals(query="iran drone", known_at=KAT))
    sql, params = r.sql[0]
    assert "  AND created_at < $2\nORDER BY" in sql and "LIMIT $3" in sql
    assert params[1] == KAT
    assert out["known_at_cut"]["known_at"] == KAT.isoformat()


def test_list_sources_sql_unchanged_without_and_judged_at_known_at_with(monkeypatch):
    async def no_profiles(pool):
        return {}
    monkeypatch.setattr(sh._staleness, "fetch_source_profiles", no_profiles)
    r = _Rec()
    asyncio.run(sh.list_sources(r))
    assert _sha(r.sql[0][0]) == _BASE_LIST_SOURCES and r.sql[0][1] == ()
    r = _Rec()
    out = asyncio.run(sh.list_sources(r, known_at=KAT))
    sql, params = r.sql[0]
    assert params == (KAT,)
    assert "WHERE source_id = s.descriptor_id AND created_at < $1 ) sig" in sql
    assert "AND occurred_at < $1 ORDER BY occurred_at DESC" in sql
    assert sql.count("o.occurred_at < $1 ORDER BY") == 2
    assert out["known_at_cut"]["known_at"] == KAT.isoformat()


# ---------------------------------------------------------------------------
# 3. Each reader drops what entered the record at or after K
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, hits: list[dict], doc: dict | None = None) -> None:
        self.hits, self.doc = hits, doc
        self.search_kw: dict = {}
        self.got: list[str] = []

    async def connect(self) -> None:
        return None

    async def search(self, index, query, **kw):
        self.search_kw = kw
        return self.hits

    async def get(self, index, doc_id):
        self.got.append(doc_id)
        return self.doc


def _hit(i: str) -> dict:
    return {"id": i, "score": 1.0, "source": {"title": "t", "best_body": "body", "fetched_at": "2026-11-01"}}


def test_search_corpus_ranges_the_index_and_confirms_each_hit():
    store = _Store([_hit(PRE), _hit(POST)])
    pool = _Rec(recorded={PRE})
    out = asyncio.run(corpus.search_corpus(store, "idx", query="q", pool=pool, known_at=KAT))
    assert store.search_kw["before"] == {"fetched_at": KAT}
    assert [r["id"] for r in out["rows"]] == [PRE] and out["refs"] == [PRE]
    assert out["known_at_cut"]["dropped_after_known_at"] == 1
    # no cut: the store is asked exactly as before
    asyncio.run(corpus.search_corpus(store, "idx", query="q"))
    assert "before" not in store.search_kw


def test_search_corpus_cut_without_a_pool_refuses():
    out = asyncio.run(corpus.search_corpus(_Store([_hit(PRE)]), "idx", query="q", known_at=KAT))
    assert out["rows"] == [] and "refused" in out["error"]


def test_read_document_after_known_at_returns_no_body():
    store = _Store([], doc={"title": "t", "best_body": "secret"})
    out = asyncio.run(corpus.read_document(store, "idx", _Rec(recorded=set()), doc_id=POST, known_at=KAT))
    assert out["status"] == "not_in_record_at_known_at" and out["refs"] == []
    assert "secret" not in str(out) and store.got == []


def test_read_document_before_known_at_reads_and_stamps():
    store = _Store([], doc={"title": "t", "best_body": "body text", "fetched_at": "2026-11-01"})
    out = asyncio.run(corpus.read_document(store, "idx", _Rec(recorded={PRE}), doc_id=PRE, known_at=KAT))
    assert out["refs"] == [PRE] and out["known_at_cut"]["dropped_after_known_at"] == 0


def test_vector_search_over_fetches_and_keeps_only_recorded_hits():
    class Emb:
        async def embed(self, q):
            return [0.1, 0.2]

    port = Port(pg_pool=_Rec(recorded={PRE}), qdrant_client=None, embedder=Emb())
    seen: dict = {}

    async def by_embedding(*, query_embedding, limit, target_id=None):
        seen["limit"] = limit
        rows = [{"signal_id": POST, "score": 0.9}, {"signal_id": PRE, "score": 0.8}]
        return {"rows": rows, "refs": [POST, PRE], "collection": "c"}

    port.vector_search_by_embedding = by_embedding  # type: ignore[method-assign]
    out = asyncio.run(port.vector_search(query="q", limit=1, known_at=KAT))
    assert seen["limit"] == 200  # over-fetch to make up for the drop
    assert [r["signal_id"] for r in out["rows"]] == [PRE] and out["refs"] == [PRE]
    assert out["known_at_cut"]["dropped_after_known_at"] == 1
    asyncio.run(port.vector_search(query="q", limit=3))
    assert seen["limit"] == 3


def test_confirm_treats_unknown_and_non_uuid_as_not_known():
    kept, refused = asyncio.run(cut.confirm_recorded_before(_Rec(recorded={PRE}), [PRE, POST, "nope"], KAT))
    assert kept == {PRE} and refused == {POST, "nope"}


def test_opensearch_search_adds_a_range_only_when_asked():
    from legba.data.opensearch import OpenSearchStore

    class Client:
        body: dict = {}

        async def search(self, index, body):
            Client.body = body
            return {"hits": {"hits": []}}

    store = OpenSearchStore.__new__(OpenSearchStore)
    store._client = Client()  # type: ignore[attr-defined]
    asyncio.run(store.search("idx", "q", before={"fetched_at": KAT}))
    assert {"range": {"fetched_at": {"lt": KAT.isoformat()}}} in Client.body["query"]["bool"]["filter"]
    asyncio.run(store.search("idx", "q"))
    assert Client.body["query"]["bool"]["filter"] == []


# ---------------------------------------------------------------------------
# 4. D22 — the series reads reach the port through the production router
# ---------------------------------------------------------------------------


def test_series_tools_dispatch_through_the_router():
    p = _Port()
    asyncio.run(_dispatch_tool(p, name="series_history", scope_predicate=None, args={
        "series_id": "gdp", "subject": "IR", "from": "2020-01-01", "to": "2025-01-01"}))
    asyncio.run(_dispatch_tool(p, name="series_compare", scope_predicate=None, args={
        "series_id": "gdp", "subjects": "IR, TR", "since": "2020-01-01", "until": "2025-01-01"}))
    (n1, k1), (n2, k2) = p.calls
    assert n1 == "series_history" and k1["subject"] == "IR" and k1["since"] == "2020-01-01"
    assert n2 == "series_compare" and k2["subjects"] == ["IR", "TR"] and k2["until"] == "2025-01-01"
