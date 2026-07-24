# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``signal_embedder`` deterministic sub-handler.

The VECTOR PLANE of the signal-content-depth program (embeds signal bodies into
the Qdrant ``legba_signals`` collection so ``vector_search`` lights up). These
tests are DETERMINISTIC and need no live substrate / Qdrant / embedding gateway —
they cover:

  * **Registration** — a first-class registered TRACE_ONLY deterministic sub-handler.
  * **Synthetic** (``deps=None``) — no substrate → a zeroed, well-formed run.
  * **Body-pick precedence** — ``_pick_body`` prefers the distilled brief, falls
    back through raw_body → teaser fields, and HTML-cleans.
  * **Degrade paths** — a missing Qdrant store OR a missing embedder no-ops the
    tick (the respective ``skipped_*`` counter set, rows left UNSTAMPED — the
    SELECT never runs).
  * **Happy path** — a fake embedder + store returns canned vectors → points are
    upserted into legba_signals + the embedded rows are stamped with their own id.
  * **No-body path** — a body-less row is drained with the ``no_body`` sentinel
    (no embed call).
  * **Failure paths** — a poison row amid a healthy tick is stamped ``embed_failed``
    (drains, never wedges); an all-fail tick (probable outage) leaves the rows
    UNSTAMPED for retry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    TRACE_ONLY,
    run_method,
)
from legba.data.analysts.deterministic_handlers import signal_embedder
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "signal_embedder"


# ---------------------------------------------------------------------------
# Fakes — a recording pg_pool / Qdrant store / hosted embedder
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls

    async def fetch(self, sql, *args):
        self._calls.append(("fetch", sql, args))
        return self._rows

    async def execute(self, sql, *args):
        self._calls.append(("execute", sql, args))
        n = len(args[0]) if args and isinstance(args[0], list) else 0
        return f"UPDATE {n}"


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, rows):
        self.calls: list[tuple] = []
        self._conn = _FakeConn(rows, self.calls)

    def acquire(self):
        return _FakeAcquire(self._conn)


class _FakeStore:
    def __init__(self):
        self.cfg = SimpleNamespace(
            signals_collection="legba_signals", host="qdrant", port=6333
        )
        self.ensured = 0
        self.upserts: list[tuple[str, list]] = []

    async def ensure_signals_collection(self):
        self.ensured += 1
        return True

    async def upsert_points(self, collection, points):
        pts = list(points)
        self.upserts.append((collection, pts))
        return len(pts)


class _FakeEmbedder:
    dim = 1024

    def __init__(self, *, vec=None, fail_marker: str | None = None):
        self.vec = vec if vec is not None else [0.1, 0.2, 0.3, 0.4]
        self.fail_marker = fail_marker
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail_marker is not None and self.fail_marker in text:
            raise RuntimeError("embed backend boom")
        return list(self.vec)


class _FakeDeps:
    def __init__(self, pool, extras):
        self.pg_pool = pool
        self.extras = extras


def _row(payload, **facets):
    row = {
        "id": uuid4(),
        "payload": payload,
        "geo": facets.get("geo"),
        "tags": facets.get("tags"),
        "entity_classes": facets.get("entity_classes"),
        "language": facets.get("language"),
        "modality": facets.get("modality", "text"),
        "source_id": facets.get("source_id"),
        "fetched_at": facets.get("fetched_at"),
    }
    return row


def _executed(pool, sql):
    return [c for c in pool.calls if c[0] == "execute" and c[1] == sql]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_signal_embedder_registered():
    assert SUB in SUB_HANDLERS, "signal_embedder missing from SUB_HANDLERS"
    assert SUB_HANDLERS[SUB] is signal_embedder.handle
    # Its real product is the Qdrant write — the run summary is trace-only.
    assert OUTPUT_KIND_BY_SUB_HANDLER[SUB] is TRACE_ONLY


# ---------------------------------------------------------------------------
# Synthetic path — no substrate, zeroed run, never spends tokens
# ---------------------------------------------------------------------------


async def test_synthetic_no_deps_zeroed_run():
    result = await run_method(
        [], {"sub_handler": SUB, "analyst_id": "se", "run_id": uuid4()}, None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["examined"] == 0
    assert data["embedded"] == 0
    assert data["skipped_no_body"] == 0
    assert data["failures"] == 0
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


# ---------------------------------------------------------------------------
# Body-pick precedence + HTML clean (pure function)
# ---------------------------------------------------------------------------


def test_pick_body_prefers_distilled_over_raw_and_teaser():
    body = signal_embedder._pick_body(
        {
            "distilled_body": "our tuned analytic brief",
            "raw_body": "the full article body text",
            "summary": "the rss teaser",
        }
    )
    assert body == "our tuned analytic brief"


def test_pick_body_falls_back_to_raw_body_then_teasers():
    assert (
        signal_embedder._pick_body({"raw_body": "full article", "summary": "teaser"})
        == "full article"
    )
    # No distilled/raw → summary; then description; then content_text; then text.
    assert signal_embedder._pick_body({"summary": "s"}) == "s"
    assert signal_embedder._pick_body({"description": "d"}) == "d"
    assert signal_embedder._pick_body({"content_text": "c"}) == "c"
    assert signal_embedder._pick_body({"text": "t"}) == "t"


def test_pick_body_html_cleaned_and_empty():
    cleaned = signal_embedder._pick_body(
        {"raw_body": "<p>Hello <b>world</b></p><script>bad()</script>"}
    )
    assert cleaned == "Hello world"
    # No usable body field → empty string (drains as no_body).
    assert signal_embedder._pick_body({}) == ""
    assert signal_embedder._pick_body({"raw_body": "   "}) == ""


# ---------------------------------------------------------------------------
# Degrade paths — a missing plane no-ops the tick, rows left UNSTAMPED
# ---------------------------------------------------------------------------


async def test_degrade_no_store():
    pool = _FakePool([_row({"raw_body": "x" * 100})])
    deps = _FakeDeps(pool, {signal_embedder.EMBEDDER_DEPS_EXTRA_KEY: _FakeEmbedder()})
    result = await signal_embedder.handle(
        [], {"sub_handler": SUB, "analyst_id": "se", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["skipped_no_store"] == 1
    assert data["skipped_no_embedder"] == 0
    assert data["examined"] == 0
    # No SELECT / stamp ran — the no-op precedes the sweep.
    assert pool.calls == []


async def test_degrade_no_embedder():
    pool = _FakePool([_row({"raw_body": "x" * 100})])
    deps = _FakeDeps(pool, {signal_embedder.QDRANT_DEPS_EXTRA_KEY: _FakeStore()})
    result = await signal_embedder.handle(
        [], {"sub_handler": SUB, "analyst_id": "se", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["skipped_no_embedder"] == 1
    assert data["skipped_no_store"] == 0
    assert data["examined"] == 0
    assert pool.calls == []


async def test_degrade_neither_wired():
    pool = _FakePool([_row({"raw_body": "x" * 100})])
    deps = _FakeDeps(pool, {})
    result = await signal_embedder.handle(
        [], {"sub_handler": SUB, "analyst_id": "se", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["skipped_no_store"] == 1
    assert data["skipped_no_embedder"] == 1
    assert pool.calls == []


# ---------------------------------------------------------------------------
# Happy path — embed + upsert + stamp the embedded rows with their own id
# ---------------------------------------------------------------------------


async def test_happy_path_embeds_upserts_and_stamps():
    rows = [
        _row(
            {"title": "First", "raw_body": "<p>the first article body</p>"},
            geo=["US"], tags=["econ"], language="en", modality="text",
            source_id="src-a", fetched_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        ),
        _row(
            {"title": "Second", "distilled_body": "the distilled brief"},
            geo=["IR"], source_id="src-b",
        ),
    ]
    pool = _FakePool(rows)
    store = _FakeStore()
    embedder = _FakeEmbedder(vec=[0.5] * 4)
    deps = _FakeDeps(
        pool,
        {
            signal_embedder.QDRANT_DEPS_EXTRA_KEY: store,
            signal_embedder.EMBEDDER_DEPS_EXTRA_KEY: embedder,
        },
    )

    result = await signal_embedder.handle(
        [], {"sub_handler": SUB, "analyst_id": "se", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["examined"] == 2
    assert data["embedded"] == 2
    assert data["failures"] == 0
    assert data["skipped_no_body"] == 0

    # The collection was ensured once and both points were upserted into it.
    assert store.ensured == 1
    assert len(store.upserts) == 1
    collection, points = store.upserts[0]
    assert collection == "legba_signals"
    assert len(points) == 2
    # Point id = the signal id (str); payload carries the facets + title.
    pid0, vec0, payload0 = points[0]
    assert pid0 == str(rows[0]["id"])
    assert vec0 == [0.5] * 4
    assert payload0["signal_id"] == str(rows[0]["id"])
    assert payload0["geo"] == ["US"]
    assert payload0["source_id"] == "src-a"
    assert payload0["title"] == "First"
    # datetime facet is ISO-formatted (JSON-serializable for the Qdrant payload).
    assert payload0["fetched_at"] == "2026-07-08T00:00:00+00:00"

    # The embedded rows were stamped in ONE bulk UPDATE carrying both ids.
    stamped = _executed(pool, signal_embedder._STAMP_EMBEDDED_BULK_SQL)
    assert len(stamped) == 1
    assert set(stamped[0][2][0]) == {rows[0]["id"], rows[1]["id"]}


# ---------------------------------------------------------------------------
# No-body path — drained with the sentinel, no embed call
# ---------------------------------------------------------------------------


async def test_no_body_row_stamped_with_sentinel():
    rows = [_row({"title": "no body here"})]  # no body-bearing field
    pool = _FakePool(rows)
    store = _FakeStore()
    embedder = _FakeEmbedder()
    deps = _FakeDeps(
        pool,
        {
            signal_embedder.QDRANT_DEPS_EXTRA_KEY: store,
            signal_embedder.EMBEDDER_DEPS_EXTRA_KEY: embedder,
        },
    )

    result = await signal_embedder.handle(
        [], {"sub_handler": SUB, "analyst_id": "se", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["examined"] == 1
    assert data["embedded"] == 0
    assert data["skipped_no_body"] == 1
    # No embed call, no upsert, collection not touched.
    assert embedder.calls == []
    assert store.upserts == []
    assert store.ensured == 0
    # Stamped with the no_body sentinel.
    stamped = _executed(pool, signal_embedder._STAMP_NO_BODY_BULK_SQL)
    assert len(stamped) == 1
    assert stamped[0][2][0] == [rows[0]["id"]]
    assert stamped[0][2][1] == signal_embedder._NO_BODY_MARKER


# ---------------------------------------------------------------------------
# Failure paths — poison row (healthy tick) vs all-fail (outage)
# ---------------------------------------------------------------------------


async def test_poison_row_stamped_failed_when_tick_healthy():
    rows = [
        _row({"raw_body": "a healthy article body"}),
        _row({"raw_body": "this one is POISON and raises"}),
    ]
    pool = _FakePool(rows)
    store = _FakeStore()
    embedder = _FakeEmbedder(fail_marker="POISON")
    deps = _FakeDeps(
        pool,
        {
            signal_embedder.QDRANT_DEPS_EXTRA_KEY: store,
            signal_embedder.EMBEDDER_DEPS_EXTRA_KEY: embedder,
        },
    )

    result = await signal_embedder.handle(
        [], {"sub_handler": SUB, "analyst_id": "se", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["embedded"] == 1
    assert data["failures"] == 1
    # The healthy row was upserted; the poison row was stamped the failed sentinel.
    assert len(store.upserts[0][1]) == 1
    failed = _executed(pool, signal_embedder._STAMP_FAILED_BULK_SQL)
    # (_STAMP_FAILED_BULK_SQL == _STAMP_NO_BODY_BULK_SQL text; match on the sentinel arg.)
    failed_marks = [c for c in failed if c[2][1] == signal_embedder._FAILED_MARKER]
    assert len(failed_marks) == 1
    assert failed_marks[0][2][0] == [rows[1]["id"]]


async def test_all_fail_tick_leaves_rows_unstamped():
    rows = [
        _row({"raw_body": "POISON one"}),
        _row({"raw_body": "POISON two"}),
    ]
    pool = _FakePool(rows)
    store = _FakeStore()
    embedder = _FakeEmbedder(fail_marker="POISON")
    deps = _FakeDeps(
        pool,
        {
            signal_embedder.QDRANT_DEPS_EXTRA_KEY: store,
            signal_embedder.EMBEDDER_DEPS_EXTRA_KEY: embedder,
        },
    )

    result = await signal_embedder.handle(
        [], {"sub_handler": SUB, "analyst_id": "se", "run_id": uuid4()}, deps,
    )
    data = result.finding.data
    assert data["examined"] == 2
    assert data["embedded"] == 0
    assert data["failures"] == 2
    # Nothing embedded → probable outage → no stamp UPDATE ran at all (rows stay
    # embedding_ref IS NULL, retried next tick).
    assert store.upserts == []
    assert [c for c in pool.calls if c[0] == "execute"] == []
