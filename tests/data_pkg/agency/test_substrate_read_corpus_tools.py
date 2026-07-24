# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stage 1 — the OpenSearch full-text corpus readers (search_corpus /
read_document) on the substrate_read pack + the PostgresQdrantSubstrateQueryPort.

No live OpenSearch: a FAKE store stands in for :class:`OpenSearchStore` (its
``connect`` is asserted idempotent-safe, ``search`` / ``get`` return canned
rows). Covers the honesty/degrade contract the readers clone from
``search_context`` / ``vector_search``:

  * no store wired  → ``no_corpus_wired`` (never connects);
  * canned rows     → the ``{rows, count, query, filters, size}`` shape;
  * filter hygiene  → non-whitelisted / None-valued keys are dropped;
  * size clamp      → ``[1, _SEARCH_CORPUS_MAX_SIZE]``;
  * read_document   → ``found`` / ``not_found`` / (backend) ``error``.

Also exercises the pack-handler wrapping (``search_corpus_tool`` /
``read_document_tool`` → ``ToolResult``) so the governed consult surface is
covered end-to-end, and asserts the drift alignment (tuple/handlers) locally.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.data.analysts.agency.substrate_read import (
    SUBSTRATE_READ_TOOLS,
    read_document_tool,
    register_substrate_read_tools,
    search_corpus_tool,
)
from legba.data.analysts.agency.tools import ToolCall, ToolContext, ToolRegistry
from legba.runtime.substrate_query_port import (
    _SEARCH_CORPUS_MAX_SIZE,
    PostgresQdrantSubstrateQueryPort,
)

# asyncio_mode = "auto" (pyproject) collects the async tests below without a
# marker; the one sync test (drift alignment) stays sync — so no module-level
# asyncio mark (that would warn on the sync test).


class _FakeOpenSearchStore:
    """Stands in for :class:`legba.data.opensearch.OpenSearchStore`.

    ``connect`` is idempotent-safe (just counts calls); ``search`` / ``get``
    return the canned payload set per-test and record the args they saw so a
    test can assert the port passed the right index / stripped query / filters.
    """

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        doc: dict[str, Any] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self._rows = rows or []
        self._doc = doc
        self._raise_on = raise_on
        self.connects = 0
        self.searched: list[dict[str, Any]] = []
        self.got: list[dict[str, Any]] = []

    async def connect(self) -> None:
        self.connects += 1

    async def search(
        self,
        index: str,
        query: str | None,
        *,
        filters: dict[str, Any] | None = None,
        size: int = 10,
    ) -> list[dict[str, Any]]:
        self.searched.append(
            {"index": index, "query": query, "filters": filters, "size": size}
        )
        if self._raise_on == "search":
            raise RuntimeError("opensearch down")
        return list(self._rows)

    async def get(self, index: str, doc_id: str) -> dict[str, Any] | None:
        self.got.append({"index": index, "doc_id": doc_id})
        if self._raise_on == "get":
            raise RuntimeError("opensearch down")
        return self._doc


def _port(store: _FakeOpenSearchStore | None) -> PostgresQdrantSubstrateQueryPort:
    """The real port over a fake corpus store (pg/qdrant are unused by these
    two readers, so None is fine — they never touch them)."""
    return PostgresQdrantSubstrateQueryPort(
        pg_pool=None,  # type: ignore[arg-type]
        qdrant_client=None,
        opensearch_store=store,
    )


# ---------------------------------------------------------------------------
# (a) degrade to no_corpus_wired when no store is wired — never connects
# ---------------------------------------------------------------------------


async def test_search_corpus_no_corpus_wired():
    out = await _port(None).search_corpus(query="iran nuclear")
    assert out == {
        "rows": [],
        "count": 0,
        "query": "iran nuclear",
        "filters": {},
        "size": 10,
        "status": "no_corpus_wired",
    }


async def test_read_document_no_corpus_wired():
    out = await _port(None).read_document(doc_id="abc")
    assert out == {"status": "no_corpus_wired", "doc_id": "abc"}


# ---------------------------------------------------------------------------
# (b) with a fake store returning canned rows → the expected shape
# ---------------------------------------------------------------------------


async def test_search_corpus_returns_shape():
    rows = [{"id": "s1", "score": 4.2, "source": {"title": "Iran brief"}}]
    store = _FakeOpenSearchStore(rows=rows)
    out = await _port(store).search_corpus(query="  iran nuclear  ", size=5)

    assert out["rows"] == rows
    assert out["count"] == 1
    # the ORIGINAL (unstripped) query is echoed back
    assert out["query"] == "  iran nuclear  "
    assert out["filters"] == {}
    assert out["size"] == 5
    # connected once (lazy) and hit the corpus index with the STRIPPED query
    assert store.connects == 1
    assert store.searched[0]["index"] == "legba_signals_corpus"
    assert store.searched[0]["query"] == "iran nuclear"
    assert store.searched[0]["filters"] is None
    assert store.searched[0]["size"] == 5


async def test_read_document_found():
    doc = {"title": "Iran brief", "raw_body": "the full article text", "geo": ["ir"]}
    store = _FakeOpenSearchStore(doc=doc)
    out = await _port(store).read_document(doc_id="s1")

    assert out == {"status": "found", "doc_id": "s1", "document": doc}
    assert store.connects == 1
    assert store.got[0] == {"index": "legba_signals_corpus", "doc_id": "s1"}


async def test_search_corpus_filter_only_browse():
    """A falsy query is allowed — the store gets query=None (match_all browse)."""
    store = _FakeOpenSearchStore(rows=[])
    out = await _port(store).search_corpus(query="", filters={"geo": "ir"})
    assert out["query"] == ""
    assert store.searched[0]["query"] is None
    assert store.searched[0]["filters"] == {"geo": "ir"}


# ---------------------------------------------------------------------------
# (c) filter sanitization — non-whitelisted / None-valued keys are dropped
# ---------------------------------------------------------------------------


async def test_search_corpus_filter_sanitization():
    store = _FakeOpenSearchStore(rows=[])
    out = await _port(store).search_corpus(
        query="x",
        filters={
            "geo": "ir",            # whitelisted scalar — kept
            "tags": ["a", "b"],     # whitelisted list — kept
            "language": None,       # whitelisted but None — dropped
            "bogus": "drop-me",     # not whitelisted — dropped
            "raw_body": "nope",     # a text field, not a facet — dropped
        },
    )
    assert out["filters"] == {"geo": "ir", "tags": ["a", "b"]}
    assert store.searched[0]["filters"] == {"geo": "ir", "tags": ["a", "b"]}


async def test_search_corpus_all_filters_dropped_passes_none():
    """Every filter sanitized away → the store sees filters=None, not {}."""
    store = _FakeOpenSearchStore(rows=[])
    out = await _port(store).search_corpus(query="x", filters={"bogus": 1})
    assert out["filters"] == {}
    assert store.searched[0]["filters"] is None


@pytest.mark.parametrize("bad", ["geo:ir", ["geo", "ir"], 42])
async def test_search_corpus_non_dict_filters_coerced(bad):
    """A mis-emitted non-dict ``filters`` (string / list / int) coerces to {}
    and degrades to an unfiltered search — never an AttributeError into the loop."""
    store = _FakeOpenSearchStore(rows=[])
    out = await _port(store).search_corpus(query="x", filters=bad)  # type: ignore[arg-type]
    assert out["filters"] == {}
    assert store.searched[-1]["filters"] is None


# ---------------------------------------------------------------------------
# (d) size clamping to [1, _SEARCH_CORPUS_MAX_SIZE]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requested,expected",
    [(0, 1), (-5, 1), (1, 1), (10, 10), (50, 50), (1000, _SEARCH_CORPUS_MAX_SIZE)],
)
async def test_search_corpus_size_clamping(requested, expected):
    store = _FakeOpenSearchStore(rows=[])
    out = await _port(store).search_corpus(query="x", size=requested)
    assert out["size"] == expected
    assert store.searched[-1]["size"] == expected


# ---------------------------------------------------------------------------
# (e) read_document not_found + both readers degrade-not-break on a backend error
# ---------------------------------------------------------------------------


async def test_read_document_not_found():
    store = _FakeOpenSearchStore(doc=None)
    out = await _port(store).read_document(doc_id="missing")
    assert out == {"status": "not_found", "doc_id": "missing"}


async def test_search_corpus_backend_error_folds():
    store = _FakeOpenSearchStore(raise_on="search")
    out = await _port(store).search_corpus(query="x", filters={"geo": "ir"})
    assert out["rows"] == [] and out["count"] == 0
    assert out["filters"] == {"geo": "ir"}  # clean filters preserved on the error shape
    assert out["size"] == 10
    assert out["error"].startswith("corpus_search_failed:")


async def test_read_document_backend_error_folds():
    store = _FakeOpenSearchStore(raise_on="get")
    out = await _port(store).read_document(doc_id="x")
    assert out["status"] == "error"
    assert out["doc_id"] == "x"
    assert out["error"].startswith("read_document_failed:")


# ---------------------------------------------------------------------------
# Pack-handler wrapping — the governed consult surface (search_corpus_tool /
# read_document_tool → ToolResult) folds the port mapping through unchanged.
# ---------------------------------------------------------------------------


async def test_search_corpus_tool_wraps_port_output():
    store = _FakeOpenSearchStore(rows=[{"id": "s1", "score": 1.0, "source": {}}])
    call = ToolCall(
        pack_id="substrate_read",
        tool_name="search_corpus",
        args={"query": "iran", "size": 3, "filters": {"geo": "ir", "bogus": "x"}},
    )
    res = await search_corpus_tool(call, None, ToolContext(substrate=_port(store)))
    assert res.status == "completed"
    assert res.output["count"] == 1
    assert res.output["size"] == 3
    assert res.output["filters"] == {"geo": "ir"}  # sanitized through the pack path


async def test_read_document_tool_wraps_port_output():
    store = _FakeOpenSearchStore(doc={"title": "T", "raw_body": "full body"})
    call = ToolCall(
        pack_id="substrate_read", tool_name="read_document", args={"doc_id": "s1"},
    )
    res = await read_document_tool(call, None, ToolContext(substrate=_port(store)))
    assert res.status == "completed"
    assert res.output["status"] == "found"
    assert res.output["document"]["raw_body"] == "full body"


async def test_pack_tool_no_substrate_wired_fails():
    """No SubstrateQueryPort on the context → a failed ToolResult, not a raise."""
    call = ToolCall(
        pack_id="substrate_read", tool_name="search_corpus", args={"query": "x"},
    )
    res = await search_corpus_tool(call, None, ToolContext(substrate=None))
    assert res.status == "failed"
    assert "no SubstrateQueryPort" in (res.error or "")


# ---------------------------------------------------------------------------
# Local drift alignment — both new tools are in the tuple AND registered.
# ---------------------------------------------------------------------------


def test_corpus_tools_in_tuple_and_registered():
    for name in ("search_corpus", "read_document"):
        assert name in SUBSTRATE_READ_TOOLS
    reg = ToolRegistry()
    register_substrate_read_tools(reg)
    assert {"search_corpus", "read_document"} <= set(reg.names)
