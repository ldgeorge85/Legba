# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane-4 vector-corpus loader tests (S5-T2) — INFRA-FREE.

A fake in-process Qdrant (real cosine over stored vectors + payload-filter
count/delete/search), a deterministic fake embedder, and an in-memory
``seed_batches`` ledger stand in for live infra. Covers the task's required
cases: a fixture doc loads; a re-run is a no-op; search returns the expected
chunk; force = delete-and-reload. Plus: unknown-corpus refusal, missing
text_ref skip, dry-run writes nothing, collection ensure, provenance/license
inheritance, and multi-chunk chunk_part identity.

Also covers the ``exemplar`` corpus (EXEMPLAR_SHELF_DRAFT_2026-07-31.md §4
step 4): a ready shelf id is accepted, an alias of a merged pattern resolves
and inherits the SURVIVOR's doctrine_source/research_ref, a retired id is
refused with the shelf's reversal note, a held (not-yet-ready) id is refused,
and a missing exemplar_id is refused — none of these touch the real curated
``seeds/exemplar_shelf.yaml`` (gitignored); a small in-memory shelf built via
``parse_shelf`` stands in.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.config import QdrantConfig
from legba.data.qdrant import QdrantStore
from legba.data.rag.lane4_loader import CORPUS_COLLECTIONS, load_vector_batch
from legba.data.seed.exemplar_shelf import DEFAULT_YAML, ExemplarShelf, parse_shelf
from legba.data.seed.manual_schema import (
    BatchManifest,
    BatchMode,
    ManualDocRecord,
    ValidatedBatch,
)

_DIM = 24


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Deterministic bag-of-words hashing embedder.

    Same text → same vector (so a query embedding of a chunk's own text scores
    1.0 against it); different text → different vector. Records every call so a
    no-op re-run can be asserted to NOT embed.
    """

    def __init__(self) -> None:
        self.dim = _DIM
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        vec = [0.0] * _DIM
        for word in text.lower().split():
            vec[hash(word) % _DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class _Hit:
    def __init__(self, pid: str, score: float, payload: dict[str, Any]) -> None:
        self.id = pid
        self.score = score
        self.payload = payload


class _QueryResponse:
    def __init__(self, points: list[_Hit]) -> None:
        self.points = points


class _CountResult:
    def __init__(self, count: int) -> None:
        self.count = count


def _match(payload: dict[str, Any], flt: Any) -> bool:
    """Match a payload against a qmodels.Filter (must=[FieldCondition...])."""
    if flt is None:
        return True
    for cond in getattr(flt, "must", None) or []:
        key = getattr(cond, "key", None)
        want = getattr(getattr(cond, "match", None), "value", None)
        if payload.get(key) != want:
            return False
    return True


class _FakeQdrant:
    """Minimal async Qdrant with real cosine + payload-filter ops."""

    def __init__(self) -> None:
        # name -> {point_id: (vector, payload)}
        self.collections: dict[str, dict[str, tuple[list[float], dict[str, Any]]]] = {}

    async def get_collections(self) -> Any:
        names = [type("C", (), {"name": n}) for n in self.collections]
        return type("R", (), {"collections": names})

    async def create_collection(self, *, collection_name: str, vectors_config: Any) -> None:
        self.collections.setdefault(collection_name, {})

    async def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        store = self.collections.setdefault(collection_name, {})
        for p in points:
            store[str(p.id)] = (list(p.vector), dict(p.payload))

    async def count(self, *, collection_name: str, count_filter: Any, exact: bool = True) -> Any:
        store = self.collections.get(collection_name, {})
        n = sum(1 for _, pl in store.values() if _match(pl, count_filter))
        return _CountResult(n)

    async def delete(self, *, collection_name: str, points_selector: Any) -> None:
        store = self.collections.get(collection_name, {})
        flt = getattr(points_selector, "filter", None)
        drop = [pid for pid, (_, pl) in store.items() if _match(pl, flt)]
        for pid in drop:
            del store[pid]

    async def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        limit: int,
        query_filter: Any = None,
        with_payload: bool = True,
    ) -> _QueryResponse:
        store = self.collections.get(collection_name, {})
        scored: list[_Hit] = []
        for pid, (vec, pl) in store.items():
            if not _match(pl, query_filter):
                continue
            scored.append(_Hit(pid, _cosine(query, vec), pl))
        scored.sort(key=lambda h: h.score, reverse=True)
        return _QueryResponse(scored[:limit])


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class _InMemoryLedger:
    """seed_batches stand-in keyed on (source, kind, manifest.content_hash)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def _find(self, source: str, kind: str, content_hash: str) -> dict[str, Any] | None:
        for row in reversed(self.rows):
            if (
                row["source"] == source
                and row["kind"] == kind
                and row["manifest"].get("content_hash") == content_hash
            ):
                return row
        return None

    async def find_batch(self, *, source: str, kind: str, content_hash: str) -> UUID | None:
        row = self._find(source, kind, content_hash)
        return row["id"] if row else None

    async def upsert_batch(
        self, *, source: str, kind: str, source_type: str, manifest: dict[str, Any]
    ) -> UUID:
        row = self._find(source, kind, manifest.get("content_hash", ""))
        if row is not None:
            row.update(source_type=source_type, manifest=manifest)
            return row["id"]
        row = {
            "id": uuid4(),
            "source": source,
            "kind": kind,
            "source_type": source_type,
            "manifest": manifest,
            "counts": {},
        }
        self.rows.append(row)
        return row["id"]

    async def set_counts(self, batch_id: UUID, counts: dict[str, int]) -> None:
        for row in self.rows:
            if row["id"] == batch_id:
                row["counts"] = counts


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _store() -> tuple[QdrantStore, _FakeQdrant]:
    fake = _FakeQdrant()
    store = QdrantStore(QdrantConfig())
    store._client = fake  # type: ignore[attr-defined]
    return store, fake


def _batch(
    docs: list[dict[str, Any]],
    *,
    mode: str = "skip",
    provenance: str = "curated",
    batch_id: str = "vec-batch-1",
    license: str | None = "CC0-1.0",
) -> ValidatedBatch:
    manifest = BatchManifest.model_validate(
        {
            "schema_version": "1",
            "batch_id": batch_id,
            "operator": "legba-dev",
            "created_at": datetime(2026, 7, 2, tzinfo=timezone.utc),
            "default_provenance": provenance,
            "mode": mode,
            "license": license,
            "source_url": "https://example.invalid/corpus",
            "files": {"docs": "docs.jsonl"},
        }
    )
    return ValidatedBatch(
        manifest=manifest,
        docs=[ManualDocRecord.model_validate(d) for d in docs],
    )


_WC = "world_context"


def _doc(doc_id: str, seq: int, text: str, **kw: Any) -> dict[str, Any]:
    return {"corpus": _WC, "doc_id": doc_id, "chunk_seq": seq, "text": text, **kw}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_fixture_doc_loads() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    ledger = _InMemoryLedger()
    batch = _batch(
        [
            _doc("tl-handbook", 0, "Testlandia is a synthetic polity used for fixtures.",
                 title="Testlandia brief", section="overview", countries=["Testlandia"]),
            _doc("tl-handbook", 1, "Its economy is fictional and small.",
                 title="Testlandia brief", section="economy"),
        ]
    )
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb, ledger=ledger
    )
    assert not res.errors
    assert res.no_op is False
    assert res.collections == {_WC: "world_context"}
    # Two short docs → two chunks, both embedded + upserted.
    assert res.counts["chunks"] == 2
    assert len(emb.calls) == 2
    assert len(fake.collections["world_context"]) == 2
    assert res.seed_batch_id is not None
    assert len(ledger.rows) == 1
    # Payload carries the RAG-plan metadata + inherited provenance/license.
    _, payload = next(iter(fake.collections["world_context"].values()))
    assert payload["corpus"] == _WC
    assert payload["provenance"] == "curated"
    assert payload["license"] == "CC0-1.0"
    assert payload["seed_batch_id"] == str(res.seed_batch_id)


async def test_rerun_is_noop() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    ledger = _InMemoryLedger()
    batch = _batch([_doc("d1", 0, "alpha beta gamma delta")])

    r1 = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb, ledger=ledger
    )
    assert r1.counts["chunks"] == 1
    calls_after_first = len(emb.calls)
    points_after_first = len(fake.collections["world_context"])

    r2 = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb, ledger=ledger
    )
    assert r2.no_op is True
    assert r2.counts["skipped_docs"] == 1
    # No re-embed, no new points.
    assert len(emb.calls) == calls_after_first
    assert len(fake.collections["world_context"]) == points_after_first
    assert len(ledger.rows) == 1  # deduped, not a second ledger row


async def test_search_returns_expected_chunk() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    ledger = _InMemoryLedger()
    batch = _batch(
        [
            _doc("econ", 0, "the fictional economy of testlandia exports synthetic ore"),
            _doc("mil", 0, "the imaginary army of testlandia trains recruits daily"),
        ]
    )
    await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb, ledger=ledger
    )
    q = await emb.embed("the fictional economy of testlandia exports synthetic ore")
    rows = await store.search("world_context", query_embedding=q, limit=2)
    assert rows
    assert rows[0]["payload"]["doc_id"] == "econ"
    assert rows[0]["score"] > 0.99


async def test_force_delete_and_reload() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    ledger = _InMemoryLedger()

    v1 = _batch([_doc("d1", 0, "original synthetic body one")])
    await load_vector_batch(
        batch=v1, batch_dir=".", store=store, embedder=emb, ledger=ledger
    )
    assert len(fake.collections["world_context"]) == 1

    # Changed text → force reload the doc's chunks.
    v2 = _batch([_doc("d1", 0, "rewritten synthetic body two three four")], mode="force")
    res = await load_vector_batch(
        batch=v2, batch_dir=".", store=store, embedder=emb, ledger=ledger, mode=BatchMode.FORCE
    )
    assert res.mode == "force"
    assert res.counts["deleted_docs"] == 1
    assert res.counts["deleted_points"] == 1
    # Still exactly one point for the doc — deleted-and-reloaded, no orphan.
    n = await store.count_doc_points("world_context", corpus=_WC, doc_id="d1")
    assert n == 1
    _, payload = next(iter(fake.collections["world_context"].values()))
    assert "rewritten" in payload["text"]


async def test_unknown_corpus_refused() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    batch = _batch([{"corpus": "not_a_corpus", "doc_id": "x", "chunk_seq": 0, "text": "hi"}])
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb, ledger=_InMemoryLedger()
    )
    assert res.errors
    assert "unknown corpus" in res.errors[0]
    assert not fake.collections  # nothing created or written
    assert emb.calls == []


async def test_missing_text_ref_skipped_others_load(tmp_path: Any) -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    batch = _batch(
        [
            _doc("ok", 0, "inline body loads fine"),
            {"corpus": _WC, "doc_id": "bad", "chunk_seq": 0, "text_ref": "docs/missing.md"},
        ]
    )
    res = await load_vector_batch(
        batch=batch, batch_dir=str(tmp_path), store=store, embedder=emb, ledger=_InMemoryLedger()
    )
    assert res.counts["skipped_docs"] == 1
    assert any("not found" in e for e in res.errors)
    # The good doc still loaded.
    assert res.counts["chunks"] == 1
    assert len(fake.collections["world_context"]) == 1


async def test_text_ref_resolves_from_batch_dir(tmp_path: Any) -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "brief.md").write_text(
        "# Brief\n\nReferenced synthetic content body.\n", encoding="utf-8"
    )
    batch = _batch([{"corpus": _WC, "doc_id": "r", "chunk_seq": 0, "text_ref": "docs/brief.md"}])
    res = await load_vector_batch(
        batch=batch, batch_dir=str(tmp_path), store=store, embedder=emb, ledger=_InMemoryLedger()
    )
    assert not res.errors
    assert res.counts["chunks"] == 1
    _, payload = next(iter(fake.collections["world_context"].values()))
    assert "Referenced synthetic" in payload["text"]
    assert payload["section"] == "Brief"


async def test_dry_run_writes_nothing() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    ledger = _InMemoryLedger()
    batch = _batch([_doc("d1", 0, "some synthetic body")])
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb, ledger=ledger, dry_run=True
    )
    assert res.dry_run is True
    assert res.counts["chunks"] == 1  # planned
    assert emb.calls == []            # no embed
    assert fake.collections == {}     # no upsert / no ensure
    assert ledger.rows == []          # no ledger row


async def test_multichunk_doc_gets_chunk_parts() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    body = "the synthetic ministry issued a fictional communique today. " * 120
    batch = _batch([_doc("big", 7, body)])
    res = await load_vector_batch(
        batch=batch,
        batch_dir=".",
        store=store,
        embedder=emb,
        ledger=_InMemoryLedger(),
        max_tokens=200,
        target_tokens=150,
    )
    payloads = [pl for _, pl in fake.collections["world_context"].values()]
    assert len(payloads) >= 2
    # All share the record's chunk_seq; chunk_part is the 0-based sub-index.
    assert {pl["chunk_seq"] for pl in payloads} == {7}
    parts = sorted(pl["chunk_part"] for pl in payloads)
    assert parts == list(range(len(parts)))
    # Distinct deterministic point ids (no collision).
    assert len(set(fake.collections["world_context"].keys())) == len(payloads)


async def test_ensure_corpus_collections_idempotent() -> None:
    store, fake = _store()
    assert await store.ensure_world_context_collection() is True
    assert await store.ensure_world_context_collection() is False
    assert await store.ensure_tradecraft_collection() is True
    assert await store.ensure_exemplar_collection() is True
    assert await store.ensure_exemplar_collection() is False
    assert set(fake.collections) == {"world_context", "tradecraft", "exemplar"}


def test_qdrant_config_has_corpus_collection_names() -> None:
    cfg = QdrantConfig()
    assert cfg.world_context_collection == "world_context"
    assert cfg.tradecraft_collection == "tradecraft"
    assert cfg.exemplar_collection == "exemplar"


def test_corpus_collections_registers_exemplar() -> None:
    assert CORPUS_COLLECTIONS["exemplar"] == (
        "ensure_exemplar_collection",
        "exemplar_collection",
    )
    assert set(CORPUS_COLLECTIONS) == {"world_context", "tradecraft", "exemplar"}


# ---------------------------------------------------------------------------
# The `exemplar` corpus — shelf-backed id validation (§4 step 4)
# ---------------------------------------------------------------------------


def _test_shelf() -> ExemplarShelf:
    """A small in-memory shelf: one ready pattern, one merged-alias pattern,
    one held pattern, one retired pattern — enough to exercise every branch
    of :func:`~legba.data.rag.lane4_loader.check_exemplar_id`."""
    raw: dict[str, Any] = {
        "version": 1,
        "patterns": [
            {
                "id": "port_capacity_crisis",
                "slot": "15",
                "title": "Port Capacity Crisis",
                "doctrine_source": "Port/congestion economics; war-risk premium response",
                "research_ref": "planning/exemplars_research/t2_maritime_economic.md",
                "status": "ready",
            },
            {
                "id": "prewar_mobilization_warning",
                "slot": "3",
                "title": "Prewar Mobilization Warning",
                "doctrine_source": "Grabo I&W tradition",
                "research_ref": "planning/exemplars_research/t1_military_coercive.md",
                "status": "ready",
                "aliases": ["exercise_as_cover"],
                "merged_from": [
                    {
                        "id": "exercise_as_cover",
                        "variant": "exercise_as_cover",
                        "merge_reason": "T1's own question — folded as a modifier",
                    }
                ],
                "variants": [{"id": "exercise_as_cover", "framing": "snap-exercise cover"}],
            },
            {
                "id": "export_control_minerals_cascade",
                "slot": "12",
                "title": "Export-Control Minerals Cascade",
                "doctrine_source": "Export-control / critical-minerals coercion literature",
                "research_ref": "planning/exemplars_research/t2_maritime_economic.md",
                "status": "hold",
                "authoring_gate": "no clean stalled-negative found",
            },
        ],
        "retired": [
            {
                "id": "hybrid_composite",
                "slot": "22",
                "title": "Hybrid Composite",
                "disposition": "killed",
                "reason": "no clean discriminator against ordinary escalation",
                "reversal": "Operator override: re-admit with a CONTESTED flag "
                "if current relevance is reweighted.",
            }
        ],
    }
    return parse_shelf(raw)


def _exemplar_doc(doc_id: str, text: str, exemplar_id: str | None, **kw: Any) -> dict[str, Any]:
    data = dict(kw.pop("data", {}))
    if exemplar_id is not None:
        data["exemplar_id"] = exemplar_id
    return {"corpus": "exemplar", "doc_id": doc_id, "chunk_seq": 0, "text": text, "data": data, **kw}


async def test_exemplar_ready_id_accepted() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    batch = _batch([_exemplar_doc("bts-2026", "port congestion baseline text", "port_capacity_crisis")])
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb,
        ledger=_InMemoryLedger(), shelf=_test_shelf(),
    )
    assert not res.errors
    assert res.counts["chunks"] == 1
    _, payload = next(iter(fake.collections["exemplar"].values()))
    assert payload["exemplar_id"] == "port_capacity_crisis"
    assert payload["doctrine_source"] == "Port/congestion economics; war-risk premium response"
    assert payload["research_ref"] == "planning/exemplars_research/t2_maritime_economic.md"
    assert payload["source_class"] == "template"


async def test_exemplar_alias_id_accepted_inherits_survivor_metadata() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    batch = _batch([_exemplar_doc("marad-2026", "snap exercise cover text", "exercise_as_cover")])
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb,
        ledger=_InMemoryLedger(), shelf=_test_shelf(),
    )
    assert not res.errors
    _, payload = next(iter(fake.collections["exemplar"].values()))
    # The RAW alias is kept as exemplar_id (not normalized to the survivor's
    # id) — but doctrine_source/research_ref are the SURVIVOR's, per the
    # shelf's resolve() semantics.
    assert payload["exemplar_id"] == "exercise_as_cover"
    assert payload["doctrine_source"] == "Grabo I&W tradition"
    assert payload["research_ref"] == "planning/exemplars_research/t1_military_coercive.md"
    assert payload["source_class"] == "template"


async def test_exemplar_retired_id_refused_with_reversal_note() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    batch = _batch([_exemplar_doc("bad-1", "hybrid composite text", "hybrid_composite")])
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb,
        ledger=_InMemoryLedger(), shelf=_test_shelf(),
    )
    assert res.counts["skipped_docs"] == 1
    assert res.counts["chunks"] == 0
    assert res.errors
    assert "retired" in res.errors[0]
    assert "Operator override: re-admit" in res.errors[0]
    assert "exemplar" not in fake.collections or not fake.collections["exemplar"]


async def test_exemplar_held_id_refused() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    batch = _batch(
        [_exemplar_doc("bad-2", "minerals cascade text", "export_control_minerals_cascade")]
    )
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb,
        ledger=_InMemoryLedger(), shelf=_test_shelf(),
    )
    assert res.counts["skipped_docs"] == 1
    assert res.errors
    assert "hold" in res.errors[0]
    assert "no clean stalled-negative found" in res.errors[0]


async def test_exemplar_missing_id_refused() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    batch = _batch([_exemplar_doc("bad-3", "no id given", None)])
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb,
        ledger=_InMemoryLedger(), shelf=_test_shelf(),
    )
    assert res.counts["skipped_docs"] == 1
    assert res.errors
    assert "missing exemplar_id" in res.errors[0]


async def test_exemplar_unknown_id_refused() -> None:
    store, fake = _store()
    emb = _FakeEmbedder()
    batch = _batch([_exemplar_doc("bad-4", "nonsense id", "not_a_real_pattern")])
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb,
        ledger=_InMemoryLedger(), shelf=_test_shelf(),
    )
    assert res.counts["skipped_docs"] == 1
    assert "not on the shelf" in res.errors[0]


async def test_exemplar_empty_shelf_refuses_everything() -> None:
    """The house seed rule: a missing curated ``seeds/exemplar_shelf.yaml``
    degrades to an EMPTY :class:`ExemplarShelf` (``load_shelf`` on an absent
    file — see ``legba.data.seed.exemplar_shelf``), never invents a pattern.
    Feeding the loader that empty shelf directly proves every exemplar_id is
    then refused, never silently accepted, regardless of which id is asked
    for."""
    store, fake = _store()
    emb = _FakeEmbedder()
    batch = _batch([_exemplar_doc("x", "text", "port_capacity_crisis")])
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb,
        ledger=_InMemoryLedger(), shelf=ExemplarShelf(),
    )
    assert res.counts["skipped_docs"] == 1
    assert "not on the shelf" in res.errors[0]


@pytest.mark.skipif(
    not DEFAULT_YAML.exists(),
    reason="curated seeds/exemplar_shelf.yaml absent (gitignored — the "
    "test_seed_sipri precedent: skip rather than assert on curated data)",
)
async def test_exemplar_lazy_loads_default_shelf_when_not_injected() -> None:
    """``shelf=None`` (the CLI's non-test path) lazily loads the real curated
    shelf — proven here against whatever is actually on disk, so this only
    runs when the curated file happens to be present locally."""
    store, fake = _store()
    emb = _FakeEmbedder()
    batch = _batch([_exemplar_doc("x", "text", "port_capacity_crisis")])
    res = await load_vector_batch(
        batch=batch, batch_dir=".", store=store, embedder=emb, ledger=_InMemoryLedger(),
    )
    assert not res.errors
    assert res.counts["chunks"] == 1
