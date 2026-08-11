# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-09 tests for the ``cross_source_dedup`` deterministic sub-handler.

Two layers:

  * **Synthetic** (``deps=None``) — content-hash grouping + deterministic
    canonical selection over pre-shaped input rows. No substrate needed; runs
    in every CI lane.
  * **Live pivot DB** (env-gated, ``LEGBA_PIVOT_PG_DSN`` or the dev-rig default)
    — the P-09 acceptance: insert the same content via 2 ``source_id``s into the
    ``legba_pivot_test`` ``signals`` table, run the handler, and assert it links
    1 canonical + 1 alias with BOTH raw rows preserved and a ``canonical_only``
    subscription seeing exactly 1. Skips cleanly when the dev rig is down.

The dispatcher contract (registered in
:data:`legba.data.analysts.deterministic.SUB_HANDLERS`) is asserted too — P-09
requires this be a *first-class* deterministic sub-handler, not hidden magic.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    run_method,
)
from legba.data.analysts.deterministic_handlers import cross_source_dedup
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "cross_source_dedup"


# ---------------------------------------------------------------------------
# Fakes — a recording asyncpg pool for the substrate-free semantic-pass tests
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls

    async def fetch(self, sql, *args):
        self._calls.append(("fetch", sql, args))
        return self._rows

    async def fetchval(self, sql, *args):
        self._calls.append(("fetchval", sql, args))
        # "does this signal row still exist" → yes; "already canonical" → no.
        return 1 if "SELECT 1 FROM signals" in sql else None

    async def execute(self, sql, *args):
        self._calls.append(("execute", sql, args))
        return "UPDATE 1"


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


class _FakeQdrant:
    """A client exposing BOTH surfaces the handler requires.

    Subclasses override :meth:`hits_for` to say what one point's neighbours are;
    the batch call routes through the same method, so a test cannot accidentally
    give the batched path different behaviour from the serial one.
    """

    def __init__(self):
        self.point_calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    def hits_for(self, point_id: str) -> list:
        return []

    async def query_points(self, **kwargs):
        point_id = kwargs["query"]
        self.point_calls.append(point_id)
        return SimpleNamespace(points=self.hits_for(point_id))

    async def query_batch_points(self, *, collection_name, requests, **kwargs):
        ids = [str(r.query) for r in requests]
        self.batch_calls.append(ids)
        return [SimpleNamespace(points=self.hits_for(i)) for i in ids]


# ---------------------------------------------------------------------------
# Registration — P-09 demands a real registered deterministic sub-handler
# ---------------------------------------------------------------------------


def test_cross_source_dedup_registered():
    assert SUB in SUB_HANDLERS, "cross_source_dedup missing from SUB_HANDLERS"
    assert SUB in OUTPUT_KIND_BY_SUB_HANDLER
    assert SUB_HANDLERS[SUB] is cross_source_dedup.handle


# ---------------------------------------------------------------------------
# R2 — the qdrant_collection default drift guard + qdrant_errors hardening
#
# Root cause (2026-07): this handler's ``qdrant_collection`` default was the
# literal ``"signals"``, a collection nothing ever creates — the live Qdrant
# only ever holds ``legba_signals`` (signal_embedder / QdrantConfig). The
# descriptor never overrode it, so every semantic-dedup pass queried a
# nonexistent collection, raised, and was swallowed by the best-effort
# except-log in ``_resolve_semantic_pool`` — zero ``signal_aliases`` rows with
# ``reason='semantic_qdrant'`` in all of history, with NOTHING in the receipt
# to say so.
# ---------------------------------------------------------------------------


def test_qdrant_collection_default_matches_the_shared_canonical_name():
    """Cross-module drift guard: import both the handler and the shared
    Qdrant config (the source of truth signal_embedder writes through via
    ``store.cfg.signals_collection``) and assert they name the SAME
    collection. This is the exact class of bug that shipped: a hardcoded
    literal ("signals") silently diverged from the real collection name
    ("legba_signals") and nobody noticed for the handler's entire history."""
    from legba.data.analysts.deterministic_handlers import signal_embedder  # noqa: F401 — drift guard: prove the module imports clean alongside the config it relies on
    from legba.data.config import QdrantConfig

    assert cross_source_dedup._DEFAULT_QDRANT_COLLECTION == QdrantConfig().signals_collection
    assert cross_source_dedup._DEFAULT_QDRANT_COLLECTION == "legba_signals"


async def test_semantic_pool_qdrant_error_counts_the_candidate_not_the_pass():
    """A per-candidate Qdrant/transport failure must surface as a COUNT in
    ``qdrant_errors`` — not just the WARNING log line (which nothing consumes as
    a signal) — and must NOT abort the remaining candidates. The try used to
    wrap the whole pass, so with rows ordered ``fetched_at ASC`` the earliest
    bad row killed every run."""
    rows = [{"id": uuid4(), "embedding_ref": str(uuid4()), "fetched_at": 1}
            for _ in range(3)]
    poison = rows[0]["embedding_ref"]
    pool = _FakePool(rows)

    class _FlakyQdrant(_FakeQdrant):
        """One point id 404s — as a real missing Qdrant point does. It fails the
        BATCH, which must then be retried serially so only that one candidate is
        lost."""

        async def query_batch_points(self, *, collection_name, requests, **kwargs):
            self.batch_calls.append([str(r.query) for r in requests])
            raise RuntimeError("404: no point with that id")

        async def query_points(self, **kwargs):
            if kwargs["query"] == poison:
                self.point_calls.append(kwargs["query"])
                raise RuntimeError("404: no point with that id")
            return await super().query_points(**kwargs)

    qdrant = _FlakyQdrant()
    aliases_linked, sets, qdrant_errors, examined, _gated = (
        await cross_source_dedup._resolve_semantic_pool(
            pool, qdrant,
            threshold=0.95,
            collection="legba_signals",
            produced_by="test_dedup",
            owner_tenant=None,
        )
    )
    assert aliases_linked == 0
    assert sets == []
    assert qdrant_errors == 1
    # THE regression: the failure cost ONE candidate, not the whole pass. The
    # batch was tried once, then every candidate was retried individually.
    assert examined == 3
    assert len(qdrant.batch_calls) == 1
    assert len(qdrant.point_calls) == 3


# ---------------------------------------------------------------------------
# B-4 — the query_points migration + the LOUD client-contract failure
#
# Root cause (P2 §2.1, 2026-08-02): `_qdrant_neighbours` reached the vector
# store through `getattr(qdrant, "recommend", None)` with an
# `if recommend is None: return []` fallthrough. `recommend()` was deprecated in
# qdrant-client 1.10 and REMOVED in favour of `query_points()`; the runtime
# image ships 1.18.0. So the getattr returned None on every call for every
# signal, the semantic tier issued ZERO Qdrant queries in all of history, and
# the receipt reported `semantic_aliases=0` and `qdrant_errors=0` at the same
# time — the counter added by the previous repair could only fire from inside a
# try that no Qdrant call ever entered. The fix that repaired the collection
# name could not be falsified by its own receipt because the code path it
# repaired was unreachable.
# ---------------------------------------------------------------------------


def test_installed_qdrant_client_exposes_query_points():
    """Assert against the INSTALLED client, never a mock.

    This is the whole shape of the defect: every test in this file mocked the
    vector store, so no test could observe that the real client had dropped the
    method the handler called. A mock cannot tell you your dependency changed
    under you — only the dependency can.
    """
    qdrant_client = pytest.importorskip("qdrant_client")

    for cls_name in ("QdrantClient", "AsyncQdrantClient"):
        cls = getattr(qdrant_client, cls_name)
        assert callable(getattr(cls, "query_points", None)), (
            f"{cls_name}.query_points is missing — the semantic dedup tier "
            "calls it directly. Do NOT reintroduce a getattr fallthrough."
        )
    # And the removed method stays removed: if a downgrade ever puts it back,
    # this says so out loud rather than letting the old call site look viable.
    assert not hasattr(qdrant_client.AsyncQdrantClient, "recommend"), (
        "recommend() is back on the installed client — it was removed in "
        "qdrant-client 1.10 and the handler no longer calls it; check the "
        "runtime image's pin before treating this as good news."
    )


def test_missing_query_points_raises_loudly_and_is_not_degradable():
    """A client that cannot do the work RAISES. It does not return [], it does
    not set a counter, it does not log-and-continue — because the old silent
    return is exactly why the tier was dead for its entire history."""
    with pytest.raises(cross_source_dedup.QdrantClientContractError) as exc:
        cross_source_dedup._require_query_points(object())
    # The message must name the failure so an operator can act on it.
    assert "query_points" in str(exc.value)


async def test_neighbours_raise_on_a_client_without_query_points():
    """The raise happens at the CALL SITE too, not only in the guard helper."""
    with pytest.raises(cross_source_dedup.QdrantClientContractError):
        await cross_source_dedup._qdrant_neighbours(
            object(), "legba_signals", uuid4(), 0.95,
        )


async def test_contract_error_escapes_the_semantic_pass():
    """The per-candidate degrade must NOT swallow a contract failure — it is a
    deploy defect, not a transient one, and it will not fix itself next
    cadence."""
    pool = _FakePool([{"id": uuid4(), "embedding_ref": str(uuid4()), "fetched_at": 1}])
    with pytest.raises(cross_source_dedup.QdrantClientContractError):
        await cross_source_dedup._resolve_semantic_pool(
            pool, object(),
            threshold=0.95,
            collection="legba_signals",
            produced_by="test_dedup",
            owner_tenant=None,
        )


async def test_neighbours_call_the_query_points_api_by_point_id():
    """Pin the call shape: `query=<point id>` (search with that point's stored
    vector), the threshold pushed DOWN into Qdrant, payloads left behind."""
    recorded: dict[str, Any] = {}
    hit_a, hit_b = uuid4(), uuid4()

    class _Qdrant:
        async def query_points(self, **kwargs):
            recorded.update(kwargs)
            return SimpleNamespace(points=[
                SimpleNamespace(id=hit_a, score=0.991),
                SimpleNamespace(id=hit_b, score=0.972),
            ])

    point_id = uuid4()
    out = await cross_source_dedup._qdrant_neighbours(
        _Qdrant(), "legba_signals", point_id, 0.97,
    )
    assert out == [(hit_a, 0.991), (hit_b, 0.972)]
    assert recorded["collection_name"] == "legba_signals"
    assert recorded["query"] == str(point_id)
    assert recorded["score_threshold"] == 0.97
    assert recorded["with_payload"] is False
    assert recorded["limit"] == cross_source_dedup._SEMANTIC_TOP_K


async def test_neighbours_tolerate_a_bare_sequence_response():
    """QueryResponse wraps the hits in `.points`; a bare sequence still works so
    the helper is not welded to one client version's wrapper."""
    hit = uuid4()

    class _Qdrant:
        async def query_points(self, **kwargs):
            return [SimpleNamespace(id=hit, score=0.99)]

    assert await cross_source_dedup._qdrant_neighbours(
        _Qdrant(), "legba_signals", uuid4(), 0.95,
    ) == [(hit, 0.99)]


async def test_semantic_candidates_exclude_the_embedder_drain_sentinels():
    """Eligibility must match the uuid SHAPE, not `IS NOT NULL`.

    `embedding_ref` is a sentinel column: signal_embedder writes 'no_body' /
    'short_body' / 'embed_failed' on its drain paths. `IS NOT NULL` admitted all
    three, and passing 'no_body' to Qdrant as a point id raises — which, with
    the old whole-pass try and `fetched_at ASC` ordering, would have aborted
    every run at the earliest sentinel row. The tier would have gone from
    silently-zero to loudly-zero."""
    pool = _FakePool([])

    class _Qdrant(_FakeQdrant):
        def hits_for(self, point_id):  # pragma: no cover — no rows
            raise AssertionError("no candidates should have been queried")

    await cross_source_dedup._resolve_semantic_pool(
        pool, _Qdrant(),
        threshold=0.95, collection="legba_signals",
        produced_by="test_dedup", owner_tenant=None,
    )
    fetched = [c for c in pool.calls if c[0] == "fetch"]
    assert len(fetched) == 1
    sql, args = fetched[0][1], fetched[0][2]
    assert "IS NOT NULL" not in sql, "sentinel-admitting eligibility filter is back"
    assert "embedding_ref ~" in sql
    assert cross_source_dedup._UUID_EMBEDDING_REF_RE in args
    # And the pass is BOUNDED — the candidate query returned 99,946 rows a run.
    assert "LIMIT" in sql
    assert cross_source_dedup.DEFAULT_MAX_SEMANTIC_CANDIDATES in args


# ---------------------------------------------------------------------------
# B-5 — THE NEIGHBOUR GATE
#
# Filtering the CANDIDATE side is not enough. A neighbour arrives from Qdrant,
# and the collection holds points the substrate no longer vouches for: orphans
# (signals_retention has no vector-store leg, so 11-21% of points outlive their
# row) and quarantined sub-floor embeddings (migration 0130 moved 36,733 rows —
# 61.2% of the vectored corpus — off their uuid marker because the vector was
# built from junk input). Those points are still IN Qdrant and still come back
# as neighbours. They are exactly the degenerate class that sits at cosine ~1.0
# regardless of content, and they are the difference between 0.728 and 0.992
# measured precision.
# ---------------------------------------------------------------------------


class _GateConn:
    """Records the gate query and answers it from a fixed linkable set."""

    def __init__(self, rows, linkable):
        self._rows = rows
        self._linkable = {str(i) for i in linkable}
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if "embedding_ref ~ $2" in sql:  # the neighbour gate
            return [{"id": i} for i in args[0] if str(i) in self._linkable]
        return self._rows

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        # The alias INSERT ... RETURNING: report a genuinely new row.
        return args[0] if "INSERT INTO signal_aliases" in sql else None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "UPDATE 1"


class _GatePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


async def test_neighbour_gate_refuses_orphan_and_quarantined_points():
    """A neighbour is linkable only if its signal row still exists AND carries a
    real vector. Here two of three neighbours fail the gate; only the third is
    linked, and the two refusals are COUNTED rather than silently dropped."""
    candidate = uuid4()
    good, orphan, quarantined = uuid4(), uuid4(), uuid4()
    rows = [{"id": candidate, "embedding_ref": str(candidate), "fetched_at": 1}]
    conn = _GateConn(rows, linkable=[good])
    pool = _GatePool(conn)

    class _Qdrant(_FakeQdrant):
        def hits_for(self, point_id):
            return [
                SimpleNamespace(id=good, score=0.995),
                SimpleNamespace(id=orphan, score=0.994),
                SimpleNamespace(id=quarantined, score=0.993),
            ]

    aliases, sets, errors, examined, gated = (
        await cross_source_dedup._resolve_semantic_pool(
            pool, _Qdrant(),
            threshold=0.97, collection="legba_signals",
            produced_by="test_gate", owner_tenant=None,
        )
    )
    assert examined == 1
    assert errors == 0
    assert gated == 2, "the orphan and the quarantined point must be refused"
    assert aliases == 1
    assert sets and sets[0]["alias_signal_ids"] == [str(good)]

    # ONE gate query for the whole neighbour set — not a PK lookup per
    # neighbour, which is what made this loop the dominant Postgres load.
    gate_calls = [c for c in conn.calls
                  if c[0] == "fetch" and "embedding_ref ~ $2" in c[1]]
    assert len(gate_calls) == 1
    assert set(gate_calls[0][2][0]) == {good, orphan, quarantined}
    assert gate_calls[0][2][1] == cross_source_dedup._UUID_EMBEDDING_REF_RE
    # And the per-neighbour existence lookup is gone entirely.
    assert not [c for c in conn.calls
                if c[0] == "fetchval" and "SELECT 1 FROM signals" in c[1]]


async def test_a_fully_gated_candidate_never_becomes_a_canonical():
    """If every neighbour is refused there is no duplicate set, so the candidate
    must NOT be stamped as its own canonical — that would be a write with no
    corresponding link."""
    candidate = uuid4()
    rows = [{"id": candidate, "embedding_ref": str(candidate), "fetched_at": 1}]
    conn = _GateConn(rows, linkable=[])  # nothing linkable
    pool = _GatePool(conn)

    class _Qdrant(_FakeQdrant):
        def hits_for(self, point_id):
            return [SimpleNamespace(id=uuid4(), score=0.99)]

    aliases, sets, _errors, _examined, gated = (
        await cross_source_dedup._resolve_semantic_pool(
            pool, _Qdrant(),
            threshold=0.97, collection="legba_signals",
            produced_by="test_gate", owner_tenant=None,
        )
    )
    # `gated` counts refused NEIGHBOURS in both branches — one neighbour was
    # proposed, one was refused.
    assert (aliases, sets, gated) == (0, [], 1)
    assert not [c for c in conn.calls if c[0] == "execute"], (
        "a fully-gated candidate was written to the substrate anyway"
    )


# ---------------------------------------------------------------------------
# B-5 — THE PAIRWISE-SCAN COLLAPSE
#
# The pass issued one query_points AND one Postgres PK lookup per candidate,
# over an unbounded 99,946-row candidate set. It was the single largest analyst
# in the fleet (61.9 of 73.6 analyst-hours/day) and the single dominant Postgres
# load (~6,800 lookups/sec), for zero output. Measured against the live
# collection, 50 point queries cost 366ms serially and 68ms batched.
# ---------------------------------------------------------------------------


def test_descriptor_declares_no_targets_so_the_sweep_runs_once():
    """THE fan-out fix, pinned in the only place that decides it.

    `AnalystActor._cadence_targets` reads `subscription.targets`: absent means
    one global run; PRESENT WITHOUT A PREDICATE means fan out to every active
    target. This descriptor had a bare `targets: {data_types: [signal],
    time_window: 24h}` under a comment claiming it was target-agnostic — so the
    runtime made 44 copies, each running an identical full-pool scan, 43 of them
    pure waste. `target_id` never narrowed a single query; the handler reads it
    once, to suffix the finding title.

    A `targets` block here is not a config preference, it is a 44x multiplier on
    the largest analyst in the fleet. If one ever comes back it needs a
    predicate and a reason.
    """
    import yaml

    from pathlib import Path

    path = (
        Path(__file__).parents[2] / "descriptors" / "analyst_cross_source_dedup.yaml"
    )
    descriptor = yaml.safe_load(path.read_text(encoding="utf-8"))
    subscription = descriptor["subscription"]
    assert "targets" not in subscription, (
        "cross_source_dedup declares subscription.targets again — with no "
        "predicate that fans the whole-pool sweep out to every active target"
    )
    # The sweep reads the substrate directly; it does not consume a materialized
    # per-target input slice.
    assert subscription["substrate"]["direct_queries"] is True
    # Cadence is now the ONLY trigger (no targets => no reactive registration),
    # so it has to be there.
    assert descriptor["cadence"]["fallback_schedule"]


def test_installed_qdrant_client_exposes_query_batch_points():
    """Again against the INSTALLED client, for the same reason as
    query_points: a mock cannot tell you the dependency changed."""
    qdrant_client = pytest.importorskip("qdrant_client")
    assert callable(getattr(qdrant_client.AsyncQdrantClient, "query_batch_points", None))
    assert callable(getattr(qdrant_client.QdrantClient, "query_batch_points", None))


def test_missing_query_batch_points_raises_rather_than_costing_5x_quietly():
    with pytest.raises(cross_source_dedup.QdrantClientContractError) as exc:
        cross_source_dedup._require_query_batch_points(object())
    assert "query_batch_points" in str(exc.value)


async def test_whole_run_is_one_qdrant_round_trip_and_one_gate_query():
    """THE collapse, pinned. N candidates must cost ONE batched Qdrant call and
    ONE neighbour-gate query — not N of each."""
    n = 40
    rows = [{"id": uuid4(), "embedding_ref": str(uuid4()), "fetched_at": i}
            for i in range(n)]
    hit_by_ref = {r["embedding_ref"]: uuid4() for r in rows}
    conn = _GateConn(rows, linkable=list(hit_by_ref.values()))
    pool = _GatePool(conn)

    class _Qdrant(_FakeQdrant):
        def hits_for(self, point_id):
            return [SimpleNamespace(id=hit_by_ref[point_id], score=0.99)]

    qdrant = _Qdrant()
    _aliases, _sets, errors, examined, _gated = (
        await cross_source_dedup._resolve_semantic_pool(
            pool, qdrant,
            threshold=0.97, collection="legba_signals",
            produced_by="test_batch", owner_tenant=None,
        )
    )
    assert examined == n
    assert errors == 0
    # One batch, no per-point calls on the happy path.
    assert len(qdrant.batch_calls) == 1
    assert len(qdrant.batch_calls[0]) == n
    assert qdrant.point_calls == []
    # One gate query for every neighbour the whole run proposed.
    gate_calls = [c for c in conn.calls
                  if c[0] == "fetch" and "embedding_ref ~ $2" in c[1]]
    assert len(gate_calls) == 1
    assert len(gate_calls[0][2][0]) == n


async def test_batches_are_chunked_so_one_request_cannot_grow_without_bound():
    chunk = cross_source_dedup._SEMANTIC_BATCH_CHUNK
    n = chunk * 2 + 5
    rows = [{"id": uuid4(), "embedding_ref": str(uuid4()), "fetched_at": i}
            for i in range(n)]
    pool = _FakePool(rows)
    qdrant = _FakeQdrant()
    await cross_source_dedup._resolve_semantic_pool(
        pool, qdrant,
        threshold=0.97, collection="legba_signals",
        produced_by="test_chunk", owner_tenant=None,
    )
    assert [len(c) for c in qdrant.batch_calls] == [chunk, chunk, 5]


async def test_batch_length_mismatch_raises_instead_of_misattributing_neighbours():
    """``query_batch_points`` results are POSITIONAL. A short reply would shift
    every subsequent candidate's neighbours onto the wrong signal and link rows
    that were never similar — so it must raise, not be zipped past."""

    class _ShortQdrant(_FakeQdrant):
        async def query_batch_points(self, *, collection_name, requests, **kwargs):
            return [SimpleNamespace(points=[])] * (len(requests) - 1)

    with pytest.raises(cross_source_dedup.QdrantClientContractError) as exc:
        await cross_source_dedup._batch_chunk(
            _ShortQdrant(), "legba_signals", [uuid4(), uuid4()], 0.97, 10,
        )
    assert "positional" in str(exc.value)


async def test_batched_and_serial_paths_agree():
    """The serial fallback must produce what the batch would have. If they can
    disagree, a chunk failure silently changes the run's OUTPUT as well as its
    cost."""
    ids = [uuid4() for _ in range(5)]
    hit_by_ref = {str(i): uuid4() for i in ids}

    class _Qdrant(_FakeQdrant):
        def hits_for(self, point_id):
            return [SimpleNamespace(id=hit_by_ref[point_id], score=0.985)]

    batched, failed_b = await cross_source_dedup._qdrant_neighbours_batch(
        _Qdrant(), "legba_signals", [str(i) for i in ids], 0.97,
    )
    serial = [
        await cross_source_dedup._qdrant_neighbours(_Qdrant(), "legba_signals", str(i), 0.97)
        for i in ids
    ]
    assert failed_b == 0
    assert batched == serial


def test_uuid_eligibility_regex_rejects_every_embedder_sentinel():
    """Cross-module drift guard: the regex must reject the sentinels the
    embedder actually writes, read from the embedder itself."""
    from legba.data.analysts.deterministic_handlers import signal_embedder

    pattern = re.compile(cross_source_dedup._UUID_EMBEDDING_REF_RE)
    for sentinel in (
        signal_embedder._NO_BODY_MARKER,
        signal_embedder._SHORT_BODY_MARKER,
        signal_embedder._FAILED_MARKER,
    ):
        assert not pattern.match(sentinel), f"{sentinel!r} would reach Qdrant"
    assert pattern.match(str(uuid4()))


async def test_synthetic_path_reports_zero_qdrant_errors():
    """The synthetic (deps=None) path never touches Qdrant — the receipt must
    say so honestly (``qdrant_errors=0``), not omit the key."""
    inputs = [
        {"id": str(uuid4()), "source_id": "a", "content_hash": "x", "fetched_at": 1},
    ]
    result = await run_method(inputs, {"sub_handler": SUB}, None)
    assert result.finding.data["qdrant_errors"] == 0


# ---------------------------------------------------------------------------
# Synthetic path — content-hash grouping, deterministic canonical
# ---------------------------------------------------------------------------


async def test_synthetic_links_cross_source_duplicate():
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    sig_a, sig_b, sig_c = uuid4(), uuid4(), uuid4()
    inputs = [
        # same content via 2 sources — A is earlier => canonical
        {"id": str(sig_a), "source_id": "src_A", "content_hash": "H1", "fetched_at": t0},
        {"id": str(sig_b), "source_id": "src_B", "content_hash": "H1",
         "fetched_at": t0 + timedelta(minutes=5)},
        # unique content — no link
        {"id": str(sig_c), "source_id": "src_A", "content_hash": "H2", "fetched_at": t0},
    ]
    result = await run_method(
        inputs, {"sub_handler": SUB, "analyst_id": "dedup", "run_id": uuid4()}, None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["canonical_count"] == 1
    assert data["aliases_linked"] == 1
    assert data["exact_aliases"] == 1
    # deterministic canonical = earliest fetched_at
    one_set = data["sets"][0]
    assert one_set["canonical_signal_id"] == str(sig_a)
    assert one_set["alias_signal_ids"] == [str(sig_b)]
    assert one_set["reason"] == "content_hash"
    # never spends tokens
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


async def test_synthetic_no_duplicates_links_nothing():
    inputs = [
        {"id": str(uuid4()), "source_id": "a", "content_hash": "x", "fetched_at": 1},
        {"id": str(uuid4()), "source_id": "b", "content_hash": "y", "fetched_at": 2},
    ]
    result = await run_method(inputs, {"sub_handler": SUB}, None)
    assert result.finding.data["canonical_count"] == 0
    assert result.finding.data["aliases_linked"] == 0


async def test_synthetic_ignores_empty_content_hash():
    # Empty content_hash is the pre-enrichment / raw shape — never deduped.
    inputs = [
        {"id": str(uuid4()), "source_id": "a", "content_hash": "", "fetched_at": 1},
        {"id": str(uuid4()), "source_id": "b", "content_hash": "", "fetched_at": 2},
    ]
    result = await run_method(inputs, {"sub_handler": SUB}, None)
    assert result.finding.data["aliases_linked"] == 0


# ---------------------------------------------------------------------------
# Live pivot-DB acceptance (env-gated)
# ---------------------------------------------------------------------------


_PIVOT_DB = {
    "host": os.environ.get("LEGBA_PIVOT_PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("LEGBA_PIVOT_PG_PORT", "5432")),
    "user": os.environ.get("LEGBA_PIVOT_PG_USER", "legba"),
    "password": os.environ.get("LEGBA_PIVOT_PG_PASSWORD", "legba"),
    "database": os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test"),
}


@pytest.fixture
async def pivot_pool():
    """asyncpg pool against the pivot substrate DB; skip if unreachable."""
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(min_size=1, max_size=4, **_PIVOT_DB)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"legba_pivot_test unreachable: {exc}")
    # Confirm the pivot substrate (not the legacy schema) is present.
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT to_regclass('signal_aliases')")
        has_canon = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='signals' AND column_name='canonical_signal_id'"
        )
    if not ok or not has_canon:
        await pool.close()
        pytest.skip("pivot substrate (signal_aliases / canonical_signal_id) not present")
    yield pool
    await pool.close()


async def test_live_pivot_acceptance(pivot_pool):
    """P-09 acceptance — same content via 2 sources => 1 canonical + 1 alias,
    both raw rows preserved, canonical_only sees 1, rerun idempotent."""
    import json

    from legba.runtime.deps import StandardDeps

    tenant = f"p09_test_{uuid4().hex[:8]}"
    produced_by = "test_dedup_p09"
    content_hash = f"p09_{uuid4().hex}"
    sig_a, sig_b = uuid4(), uuid4()
    t0 = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)

    async with pivot_pool.acquire() as conn:
        for sid, ts, payload, sigid in [
            ("source_reuters", t0, {"title": "Quake hits region"}, sig_a),
            ("source_ap", t0 + timedelta(minutes=3), {"title": "Quake hits region"}, sig_b),
        ]:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload, content_hash, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5,$6)""",
                sigid, sid, tenant, json.dumps(payload), content_hash, ts,
            )

    deps = StandardDeps(pg_pool=pivot_pool)
    try:
        result = await run_method(
            [], {"sub_handler": SUB, "analyst_id": produced_by,
                 "run_id": uuid4(), "owner_tenant": tenant}, deps,
        )
        data = result.finding.data
        assert data["canonical_count"] == 1, data
        assert data["aliases_linked"] == 1, data
        assert data["exact_aliases"] == 1, data

        async with pivot_pool.acquire() as conn:
            # BOTH raw rows survive — never destructive collapse.
            raw = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant)
            assert raw == 2

            aliases = await conn.fetch(
                "SELECT alias_signal_id, canonical_signal_id, reason, score "
                "FROM signal_aliases WHERE produced_by=$1", produced_by)
            assert len(aliases) == 1
            assert str(aliases[0]["canonical_signal_id"]) == str(sig_a)  # earliest
            assert str(aliases[0]["alias_signal_id"]) == str(sig_b)
            assert aliases[0]["reason"] == "content_hash"
            assert abs(aliases[0]["score"] - 1.0) < 1e-6

            # canonical points at itself; alias points at canonical
            ca = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", sig_a)
            cb = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", sig_b)
            assert str(ca) == str(sig_a)
            assert str(cb) == str(sig_a)

            # a canonical_only subscription sees exactly 1.
            canon_only = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1 "
                "AND (canonical_signal_id = id OR canonical_signal_id IS NULL)", tenant)
            assert canon_only == 1

        # Rerun is idempotent — links 0 new aliases, never collapses.
        rerun = await run_method(
            [], {"sub_handler": SUB, "analyst_id": produced_by,
                 "run_id": uuid4(), "owner_tenant": tenant}, deps,
        )
        assert rerun.finding.data["aliases_linked"] == 0
        async with pivot_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM signal_aliases WHERE produced_by=$1",
                produced_by) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant) == 2
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)


# ---------------------------------------------------------------------------
# Bounded + incremental per-run work (the actor-invoke timeout fix)
# ---------------------------------------------------------------------------


async def _seed_dup_groups(conn, tenant: str, n_groups: int) -> dict[str, tuple]:
    """Seed ``n_groups`` 2-row content_hash duplicate groups for ``tenant``.

    Returns ``{content_hash: (canonical_id, alias_id)}`` where the canonical is
    the earlier-fetched row (so dedupe should pick it).
    """
    import json

    t0 = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    # Per-call batch nonce so repeated calls for the same tenant never reuse a
    # content_hash (each call yields a fresh, independent set of groups).
    batch = uuid4().hex[:8]
    groups: dict[str, tuple] = {}
    for i in range(n_groups):
        ch = f"bnd_{tenant}_{batch}_{i:05d}"
        canon, alias = uuid4(), uuid4()
        for sid, ts, sigid in [
            ("source_a", t0, canon),
            ("source_b", t0 + timedelta(minutes=1), alias),
        ]:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload, content_hash, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5,$6)""",
                sigid, sid, tenant, json.dumps({"title": ch}), ch, ts,
            )
        groups[ch] = (canon, alias)
    return groups


async def _unresolved_group_count(conn, tenant: str) -> int:
    """Number of content_hash groups still holding an unresolved member."""
    return await conn.fetchval(
        """
        SELECT count(*) FROM (
            SELECT content_hash
            FROM signals
            WHERE owner_tenant = $1 AND content_hash <> ''
            GROUP BY content_hash
            HAVING COUNT(*) > 1
               AND COUNT(*) FILTER (WHERE canonical_signal_id IS NULL) > 0
        ) s
        """,
        tenant,
    )


async def test_bounded_cap_processes_only_max_groups_per_run(pivot_pool):
    """N > cap unresolved groups → one run resolves exactly ``cap`` groups and
    leaves the rest for the next run; successive idempotent runs drain the
    backlog until every group is resolved (eventual consistency)."""
    from legba.runtime.deps import StandardDeps

    tenant = f"bnd_cap_{uuid4().hex[:8]}"
    produced_by = "test_dedup_bounded"
    n_groups = 7
    cap = 3
    deps = StandardDeps(pg_pool=pivot_pool)

    try:
        async with pivot_pool.acquire() as conn:
            await _seed_dup_groups(conn, tenant, n_groups)
            assert await _unresolved_group_count(conn, tenant) == n_groups

        opts = {
            "sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
            "owner_tenant": tenant, "max_groups_per_run": cap,
        }

        # Run 1 — bounded to exactly `cap` groups.
        r1 = await run_method([], dict(opts, run_id=uuid4()), deps)
        assert r1.finding.data["canonical_count"] == cap, r1.finding.data
        assert r1.finding.data["aliases_linked"] == cap, r1.finding.data
        async with pivot_pool.acquire() as conn:
            assert await _unresolved_group_count(conn, tenant) == n_groups - cap

        # Run 2 — next `cap` groups.
        r2 = await run_method([], dict(opts, run_id=uuid4()), deps)
        assert r2.finding.data["canonical_count"] == cap, r2.finding.data
        async with pivot_pool.acquire() as conn:
            assert await _unresolved_group_count(conn, tenant) == n_groups - 2 * cap

        # Run 3 — the final group (< cap remaining).
        r3 = await run_method([], dict(opts, run_id=uuid4()), deps)
        assert r3.finding.data["canonical_count"] == n_groups - 2 * cap, r3.finding.data
        async with pivot_pool.acquire() as conn:
            assert await _unresolved_group_count(conn, tenant) == 0
            # All N groups now fully resolved: N canonicals + N aliases.
            n_aliases = await conn.fetchval(
                "SELECT count(*) FROM signal_aliases WHERE produced_by=$1", produced_by)
            assert n_aliases == n_groups
            # No raw rows lost (2 per group).
            assert await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant) == 2 * n_groups

        # Run 4 — fully drained → idempotent no-op.
        r4 = await run_method([], dict(opts, run_id=uuid4()), deps)
        assert r4.finding.data["canonical_count"] == 0
        assert r4.finding.data["aliases_linked"] == 0
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)


async def test_already_canonicalised_group_not_reprocessed(pivot_pool):
    """A fully-canonicalised group is skipped in SQL — never re-resolved — so a
    cap-sized run spends its whole budget on *unresolved* groups only."""
    from legba.runtime.deps import StandardDeps

    tenant = f"bnd_skip_{uuid4().hex[:8]}"
    produced_by = "test_dedup_skip"
    cap = 2
    deps = StandardDeps(pg_pool=pivot_pool)

    try:
        async with pivot_pool.acquire() as conn:
            groups = await _seed_dup_groups(conn, tenant, 5)

        opts = {
            "sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
            "owner_tenant": tenant, "max_groups_per_run": cap,
        }

        # Drain all 5 groups (cap=2 → 2,2,1).
        for _ in range(3):
            await run_method([], dict(opts, run_id=uuid4()), deps)
        async with pivot_pool.acquire() as conn:
            assert await _unresolved_group_count(conn, tenant) == 0
            resolved_aliases = await conn.fetchval(
                "SELECT count(*) FROM signal_aliases WHERE produced_by=$1", produced_by)
        assert resolved_aliases == 5

        # Insert ONE brand-new unresolved group. With every old group already
        # canonicalised, the next run must spend its (capped) budget resolving
        # exactly the new group — proving settled groups are skipped, not
        # re-walked.
        async with pivot_pool.acquire() as conn:
            new = await _seed_dup_groups(conn, tenant, 1)
            assert await _unresolved_group_count(conn, tenant) == 1
        new_ch, (new_canon, new_alias) = next(iter(new.items()))

        r = await run_method([], dict(opts, run_id=uuid4()), deps)
        assert r.finding.data["canonical_count"] == 1, r.finding.data
        assert r.finding.data["aliases_linked"] == 1, r.finding.data
        async with pivot_pool.acquire() as conn:
            assert await _unresolved_group_count(conn, tenant) == 0
            # The new group is correctly linked; total aliases = 5 old + 1 new.
            assert await conn.fetchval(
                "SELECT count(*) FROM signal_aliases WHERE produced_by=$1",
                produced_by) == 6
            cb = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", new_alias)
            assert str(cb) == str(new_canon)
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)


async def test_processed_group_outcome_unchanged_under_cap(pivot_pool):
    """The dedupe result for a group the bounded pass *does* process is
    identical to the old unbounded behaviour: earliest-fetched canonical, one
    alias linked, both raw rows preserved, canonical_only sees exactly one."""
    from legba.runtime.deps import StandardDeps

    tenant = f"bnd_eq_{uuid4().hex[:8]}"
    produced_by = "test_dedup_equiv"
    deps = StandardDeps(pg_pool=pivot_pool)

    try:
        async with pivot_pool.acquire() as conn:
            groups = await _seed_dup_groups(conn, tenant, 1)
        ch, (canon, alias) = next(iter(groups.items()))

        r = await run_method(
            [],
            {"sub_handler": SUB, "analyst_id": produced_by, "run_id": uuid4(),
             "owner_tenant": tenant, "max_groups_per_run": 500},
            deps,
        )
        data = r.finding.data
        assert data["canonical_count"] == 1
        assert data["aliases_linked"] == 1
        assert data["exact_aliases"] == 1

        async with pivot_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT alias_signal_id, canonical_signal_id, reason, score "
                "FROM signal_aliases WHERE produced_by=$1", produced_by)
            assert str(row["canonical_signal_id"]) == str(canon)  # earliest fetched
            assert str(row["alias_signal_id"]) == str(alias)
            assert row["reason"] == "content_hash"
            assert abs(row["score"] - 1.0) < 1e-6

            ca = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", canon)
            cb = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", alias)
            assert str(ca) == str(canon)
            assert str(cb) == str(canon)

            assert await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant) == 2
            canon_only = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1 "
                "AND (canonical_signal_id = id OR canonical_signal_id IS NULL)", tenant)
            assert canon_only == 1
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)
