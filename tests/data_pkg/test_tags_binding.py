# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Namespaced tag binding `<family>:<value>`.

* `bind_tag`: bound / unclassified (raw kept) / legacy `src:`; never raises;
  one log line per new unknown, capped.
* `has_tag()` never lets a signal tag collide with a target tag.
* The runtime vocabulary cache: TTL, per-family degrade, the previous snapshot
  kept on an index failure — and read through the REAL registry route.
* The four producers persist `data.classifications` on the real driver.
* `scripts/reclassify_tags.py`: the dry-run census on seeded rows; `--apply`
  resumes from its cursor.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import string
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data import tags as tags_mod
from legba.data.postgres import PostgresStore
from legba.data.predicates import PredicateSurface, compile_predicate
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps, build_router
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.tags import (
    EMPTY_INDEX,
    STATUS_BOUND,
    STATUS_LEGACY_SRC,
    STATUS_UNCLASSIFIED,
    VocabularyIndex,
    bind_tag,
    bind_tags,
    classifications,
    install_vocabulary_provider,
    signal_tag_key,
    stamp_classifications,
    tag_vocabulary_index,
)
from legba.runtime.registry_client import RegistryHTTPClient
from legba.runtime.vocabulary_runtime import (
    VOCABULARY_PATH,
    RuntimeVocabulary,
    install_tag_vocabulary,
    uninstall_tag_vocabulary,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _idx(**kw) -> VocabularyIndex:
    entries = [
        {"family": "entity_class", "value": "person", "aliases": ["individual"]},
        {"family": "entity_class", "value": "software", "aliases": []},
        {"family": "acme.domain", "value": "energy", "aliases": ["power"]},
        {"family": "acme.domain", "value": "old", "aliases": [], "deprecated": "2026-01-01"},
        {"family": "acme.frame", "value": "lane", "aliases": []},
    ]
    return VocabularyIndex.from_entries(entries, **kw)


@pytest.fixture(autouse=True)
def _reset_binding_state():
    tags_mod.reset_unknown_log()
    install_vocabulary_provider(None)
    yield
    uninstall_tag_vocabulary()
    tags_mod.reset_unknown_log()


# ---------------------------------------------------------------------------
# bind_tag
# ---------------------------------------------------------------------------


def test_a_known_family_and_value_binds_alias_resolved():
    idx = _idx()
    b = bind_tag("entity_class:person", families=idx)
    assert (b.family, b.value, b.status) == ("entity_class", "person", STATUS_BOUND)
    b = bind_tag("acme.domain:power", families=idx)
    assert (b.family, b.value, b.status, b.raw) == (
        "acme.domain", "energy", STATUS_BOUND, "acme.domain:power",
    )
    assert b.as_classification() == {
        "family": "acme.domain", "value": "energy", "status": "bound",
    }


def test_a_known_family_with_an_unknown_value_is_unclassified_raw_kept():
    idx = _idx()
    for raw in ("acme.domain:fusion", "acme.domain:old", "entity_class:Person"):
        b = bind_tag(raw, families=idx)
        assert b.status == STATUS_UNCLASSIFIED, raw
        assert b.as_classification()["raw"] == raw


def test_an_unloaded_family_degrades_every_tag_in_it_to_unclassified():
    # never registered
    assert bind_tag("other.axis:x", families=_idx()).status == STATUS_UNCLASSIFIED
    # registered but left unloaded by the registry: even a known value
    idx = _idx(unloaded={"acme.domain"})
    b = bind_tag("acme.domain:energy", families=idx)
    assert (b.family, b.value, b.status) == ("acme.domain", "energy", STATUS_UNCLASSIFIED)
    # the other families are unaffected
    assert bind_tag("acme.frame:lane", families=idx).status == STATUS_BOUND
    # no vocabulary at all
    assert bind_tag("entity_class:person", families=EMPTY_INDEX).status == STATUS_UNCLASSIFIED


def test_a_bare_value_is_legacy_src_and_src_prefix_is_idempotent():
    idx = _idx()
    for raw in ("world_news", "أخبار", "src:world_news"):
        b = bind_tag(raw, families=idx)
        assert b.family == "src" and b.status == STATUS_LEGACY_SRC
    assert bind_tag("src:world_news", families=idx).key == bind_tag("world_news", families=idx).key


def test_a_colon_after_a_non_family_prefix_is_a_bare_value():
    idx = _idx()
    for raw in ("News: World", "severity:high", "http://x.y/z", "Acme.Domain:energy", "acme.domain:"):
        b = bind_tag(raw, families=idx)
        assert (b.family, b.value, b.status) == ("src", raw, STATUS_LEGACY_SRC), raw


def test_bind_tag_never_raises():
    idx = _idx()

    class _Boom:
        def __str__(self):
            raise RuntimeError("no")

    for raw in (None, "", "   ", 42, ["x"], _Boom()):
        b = bind_tag(raw, families=idx)
        assert b.status in (STATUS_UNCLASSIFIED, STATUS_LEGACY_SRC)
    assert bind_tags([None, "", "x", "x", "src:x"], families=idx) == [
        bind_tag("x", families=idx),
    ]


def test_one_log_line_per_new_unknown_and_the_log_is_capped(caplog, monkeypatch):
    idx = _idx()
    with caplog.at_level(logging.INFO, logger="legba.data.tags"):
        for _ in range(3):
            bind_tag("acme.domain:fusion", families=idx)
        bind_tag("acme.domain:solar", families=idx)
    lines = [r.getMessage() for r in caplog.records if "tags.unclassified" in r.getMessage()]
    assert len(lines) == 2
    caplog.clear()
    tags_mod.reset_unknown_log()
    monkeypatch.setattr(tags_mod, "UNKNOWN_LOG_CAP", 2)
    with caplog.at_level(logging.INFO, logger="legba.data.tags"):
        for v in ("a", "b", "c", "d"):
            bind_tag(f"acme.domain:{v}", families=idx)
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("tags.unclassified family" in m for m in msgs) == 2
    assert sum("log cap reached" in m for m in msgs) == 1


def test_stamp_classifications_binds_the_producer_axes_without_mutating():
    idx = _idx()
    data = {"source": "x", "tags": ["acme.domain:energy", "loose"]}
    out = stamp_classifications(
        "entity_profiles", {"entity_class": "person"}, data, families=idx,
    )
    assert "classifications" not in data
    assert out["source"] == "x"
    assert out["classifications"] == [
        {"family": "entity_class", "value": "person", "status": "bound"},
        {"family": "acme.domain", "value": "energy", "status": "bound"},
        {"family": "src", "value": "loose", "status": "legacy_src"},
    ]
    out = stamp_classifications("facts", {"predicate": "located in"}, None, families=idx)
    assert out == {"classifications": [
        {"family": "src", "value": "located in", "status": "legacy_src"},
    ]}
    assert stamp_classifications("situations", {"category": ""}, {}, families=idx) == {
        "classifications": [],
    }


# ---------------------------------------------------------------------------
# has_tag() — namespaced, no signal/target collision
# ---------------------------------------------------------------------------


def _scope(src: str, ctx: dict) -> bool:
    return compile_predicate(src, PredicateSurface.TARGET_SCOPE).evaluate(ctx)


def _sub(src: str, ctx: dict) -> bool:
    return compile_predicate(src, PredicateSurface.ANALYST_SUBSCRIPTION).evaluate(ctx)


def test_a_signal_tag_never_satisfies_a_target_tag_predicate():
    # The live hazard: a publisher category "g20" on a signal used to satisfy
    # the desk roster predicate has_tag("g20").
    sig = {"signal": {"tags": ["g20", "watch"]}}
    assert _scope('has_tag("g20")', sig) is False
    assert _scope('has_tag("g20") or has_tag("watch")', sig) is False
    assert _scope('has_any_tag(["g20", "watch"])', sig) is False
    # the source category is reachable by its namespace
    assert _scope('has_tag("src:g20")', sig) is True
    assert signal_tag_key("g20") == "src:g20"
    # the target surface still matches its own tag, verbatim
    assert _sub('has_tag("g20")', {"target": {"tags": ["g20"]}}) is True
    # both surfaces in one ctx: only the target's tag counts for the bare name
    both = {"signal": {"tags": ["g20"]}, "target": {"tags": ["watch"]}}
    assert _scope('has_tag("g20")', both) is False
    assert _scope('has_tag("watch")', both) is True


def test_registered_tags_compare_verbatim_and_src_is_signal_only():
    sig = {"signal": {"tags": ["acme.domain:energy", "severity:high"]}}
    assert _scope('has_tag("acme.domain:energy")', sig) is True
    # "severity:high" is not family-shaped → a bare source category
    assert _scope('has_tag("severity:high")', sig) is False
    assert _scope('has_tag("src:severity:high")', sig) is True
    # a target tag in the reserved src namespace is ignored
    assert _sub('has_tag("src:energy")', {"target": {"tags": ["src:energy"]}}) is False
    # event tags (analyst-emitted) compare verbatim
    trig = compile_predicate(
        'has_tag("severity:high")', PredicateSurface.CADENCE_TRIGGER,
        ctx_contract=frozenset({"event.tags"}),
    )
    assert trig.evaluate({"event": {"tags": ["severity:high"]}}) is True


# ---------------------------------------------------------------------------
# The runtime vocabulary cache — TTL + degrade
# ---------------------------------------------------------------------------


class _Registry:
    """Answers the two vocabulary routes from a dict; records every call."""

    def __init__(self, families: dict[str, list[dict]], *, unloaded=()):
        self.families = families
        self.unloaded = list(unloaded)
        self.calls: list[str] = []
        self.index_status = 200
        self.family_status: dict[str, int] = {}
        self.raise_on_index = False

    async def request_json(self, method: str, path: str, **_kw):
        self.calls.append(path)
        if path == VOCABULARY_PATH:
            if self.raise_on_index:
                raise ConnectionError("registry down")
            return self.index_status, {
                "families": [{"family": f, "values": len(v)} for f, v in self.families.items()],
                "families_unloaded": [
                    {"family": f, "code": "family_unknown", "detail": "", "rows": 1}
                    for f in self.unloaded
                ],
                "rows_skipped": [],
            }
        fam = path.rsplit("/", 1)[1]
        st = self.family_status.get(fam, 200)
        return st, (self.families.get(fam, []) if st == 200 else {"detail": "boom"})


def _rows(family: str, *values: str) -> list[dict]:
    return [{"family": family, "value": v, "aliases": [], "deprecated": None} for v in values]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.mark.asyncio
async def test_runtime_cache_honours_the_ttl():
    reg = _Registry({"entity_class": _rows("entity_class", "person")})
    clock = _Clock()
    rv = RuntimeVocabulary(reg.request_json, ttl_seconds=300, clock=clock)
    idx = await rv.index()  # first read waits for the priming refresh
    assert idx.resolve("entity_class", "person") == "person"
    n = len(reg.calls)
    clock.t += 299
    await rv.index()
    assert len(reg.calls) == n  # within the TTL: no read
    clock.t += 2
    reg.families["acme.domain"] = _rows("acme.domain", "energy")
    stale = await rv.index()  # stale: returns the snapshot, refreshes in background
    assert stale.resolve("acme.domain", "energy") is None
    await rv._task
    assert (await rv.index()).resolve("acme.domain", "energy") == "energy"


@pytest.mark.asyncio
async def test_runtime_cache_degrades_per_family():
    reg = _Registry(
        {"entity_class": _rows("entity_class", "person"),
         "acme.domain": _rows("acme.domain", "energy"),
         "acme.frame": _rows("acme.frame", "lane")},
        unloaded=["acme.frame"],
    )
    reg.family_status["acme.domain"] = 500
    rv = RuntimeVocabulary(reg.request_json, ttl_seconds=300)
    idx = await rv.refresh()
    assert idx.family_loaded("entity_class")
    assert not idx.family_loaded("acme.domain")  # its read failed
    assert not idx.family_loaded("acme.frame")   # the registry left it unloaded
    assert bind_tag("acme.domain:energy", families=idx).status == STATUS_UNCLASSIFIED
    assert bind_tag("entity_class:person", families=idx).status == STATUS_BOUND
    assert f"{VOCABULARY_PATH}/acme.frame" not in reg.calls


@pytest.mark.asyncio
async def test_an_index_failure_keeps_the_previous_snapshot_and_retries_sooner():
    reg = _Registry({"entity_class": _rows("entity_class", "person")})
    clock = _Clock()
    rv = RuntimeVocabulary(reg.request_json, ttl_seconds=300, clock=clock)
    await rv.index()
    reg.raise_on_index = True
    clock.t += 301
    await rv.index()
    await rv._task
    assert rv.last_error and "index read failed" in rv.last_error
    assert rv.snapshot.resolve("entity_class", "person") == "person"  # kept
    n = len(reg.calls)
    clock.t += 29
    await rv.index()
    assert len(reg.calls) == n  # retry not yet due
    reg.raise_on_index = False
    clock.t += 2
    await rv.index()
    await rv._task
    assert rv.last_error is None


@pytest.mark.asyncio
async def test_no_snapshot_and_a_dead_registry_is_the_empty_index_never_a_crash():
    reg = _Registry({})
    reg.raise_on_index = True
    rv = RuntimeVocabulary(reg.request_json, ttl_seconds=300)
    idx = await rv.index()
    assert idx is EMPTY_INDEX
    assert bind_tag("entity_class:person", families=idx).status == STATUS_UNCLASSIFIED


@pytest.mark.asyncio
async def test_no_provider_means_empty_and_a_failing_provider_degrades():
    assert await tag_vocabulary_index() is EMPTY_INDEX

    async def _bad():
        raise RuntimeError("x")

    install_vocabulary_provider(_bad)
    assert await tag_vocabulary_index() is EMPTY_INDEX


def test_ttl_default_is_300_and_env_overrides(monkeypatch):
    monkeypatch.delenv("LEGBA_TAG_VOCABULARY_TTL_SECONDS", raising=False)
    assert RuntimeVocabulary(_Registry({}).request_json).ttl_seconds == 300.0
    monkeypatch.setenv("LEGBA_TAG_VOCABULARY_TTL_SECONDS", "60")
    assert RuntimeVocabulary(_Registry({}).request_json).ttl_seconds == 60.0
    monkeypatch.setenv("LEGBA_TAG_VOCABULARY_TTL_SECONDS", "nope")
    assert RuntimeVocabulary(_Registry({}).request_json).ttl_seconds == 300.0


def test_dapr_host_wires_the_cache_in_one_line():
    src = (REPO_ROOT / "src/legba/runtime/dapr_host.py").read_text()
    assert "registry_client = install_tag_vocabulary(RegistryHTTPClient())" in src
    assert src.count("install_tag_vocabulary") == 2  # the import + the call


# ---------------------------------------------------------------------------
# Real driver: the registry route, the producers, the script
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg(migrated_pg):
    store = PostgresStore(migrated_pg)
    await store.connect()
    yield store
    async with store.acquire() as conn:
        await conn.execute("DELETE FROM vocabulary_entries WHERE family LIKE 'tg%'")
    await store.close()


@pytest_asyncio.fixture
async def profile(pg):
    """A registered profile family `<prof>.domain` with `energy` (alias
    `power`), plus a BARE family the registry leaves unloaded."""
    prof = "tg" + "".join(random.choices(string.ascii_lowercase, k=6))
    async with pg.acquire() as conn:
        await conn.execute(
            "INSERT INTO vocabulary_entries (family, value, aliases) VALUES ($1, 'energy', $2)",
            f"{prof}.domain", ["power"],
        )
        await conn.execute(
            "INSERT INTO vocabulary_entries (family, value) VALUES ($1, 'x')", f"{prof}bare",
        )
    return prof


class _PgOnly:
    def __init__(self, pg):
        self.pg = pg


@pytest_asyncio.fixture
async def registry_client(pg):
    """A RegistryHTTPClient whose transport is the REAL registry router."""
    os.environ.pop(API_TOKEN_ENV, None)
    cache = VocabularyCache(pg)
    deps = RegistryAPIDeps(
        descriptor_registry=_PgOnly(pg),  # type: ignore[arg-type]
        stack_registry=None, vault=None, dlq=None, audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=cache, nats_store=None, conversion_registry=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_router(deps), prefix="/api/v1/registry")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield RegistryHTTPClient(base_url="http://t", token="", client=c), cache


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_cache_reads_the_real_vocabulary_route(registry_client, profile):
    client, cache = registry_client
    await cache.refresh()
    assert install_tag_vocabulary(client, ttl_seconds=300) is client
    idx = await tag_vocabulary_index()
    assert idx.loaded_at is not None
    assert bind_tag(f"{profile}.domain:power", families=idx).as_classification() == {
        "family": f"{profile}.domain", "value": "energy", "status": "bound",
    }
    assert bind_tag("entity_class:person", families=idx).status == STATUS_BOUND
    assert f"{profile}bare" in idx.unloaded
    assert bind_tag(f"{profile}.domain:fusion", families=idx).status == STATUS_UNCLASSIFIED


async def _install_db_index(pg) -> VocabularyIndex:
    cache = VocabularyCache(pg)
    await cache.refresh()
    idx = VocabularyIndex.from_cache(cache)

    async def _provider():
        return idx

    install_vocabulary_provider(_provider)
    return idx


async def _seed_signal(conn, *, source_id="rss_main", tags=(), payload=None, fetched_at=None):
    return await conn.fetchval(
        "INSERT INTO signals (source_id, modality, payload, content_hash, fetched_at, tags)"
        " VALUES ($1, 'text', $2::jsonb, $3, $4, $5::text[]) RETURNING id",
        source_id, json.dumps(payload or {}), f"h-{uuid4().hex}",
        fetched_at or datetime.now(tz=timezone.utc), list(tags),
    )


def _cls(raw) -> list[dict]:
    data = json.loads(raw) if isinstance(raw, str) else raw
    return data["classifications"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_four_producers_persist_classifications(pg, profile, monkeypatch):
    from legba.data.analysts.deterministic_handlers.entity_resolution import _resolve_batch
    from legba.data.analysts.deterministic_handlers.situation_clustering import (
        _upsert_situation,
    )
    from legba.data.filters.fact_extractor import _insert_ingestion_fact
    from legba.data.provenance import AnalystContext, write_event, write_fact, write_situation
    from legba.data.provenance.models import EventPayload, EventSignalLinkPayload

    await _install_db_index(pg)
    fam = f"{profile}.domain"
    ctx = AnalystContext(
        analyst_id=f"analyst.tag1_{uuid4().hex[:8]}", analyst_version="t",
        run_id=uuid4(), target_id=None, target_version=None,
    )
    async with pg.acquire() as conn:
        # facts — the ingest-time enrichment
        sig = await _seed_signal(conn)
        fid = uuid4()
        subj = f"Subject {uuid4().hex[:8]}"
        await _insert_ingestion_fact(
            conn, fact_id=fid, subject=subj, predicate="located in", value="Somewhere",
            confidence=0.6, valid_from=datetime.now(tz=timezone.utc), geo_lat=None,
            geo_lon=None, data={"tags": [f"{fam}:power"]}, evidence_set={},
            derived_from=[sig],
        )
        got = _cls(await conn.fetchval("SELECT data FROM facts WHERE id = $1", fid))
        assert got == [
            {"family": "src", "value": "located in", "status": "legacy_src"},
            {"family": fam, "value": "energy", "status": "bound"},
        ]
        # facts — the analyst write path
        subj2 = f"Subject {uuid4().hex[:8]}"
        out, dlq = await write_fact(
            conn, analyst_ctx=ctx,
            payload={"subject": subj2, "predicate": "member of", "value": "Club",
                     "data": {"tags": [f"{fam}:fusion"]}},
            derived_from=[sig],
        )
        assert dlq is None, dlq
        got = _cls(await conn.fetchval(
            "SELECT data FROM facts WHERE subject = $1 AND valid_until IS NULL", subj2))
        assert {"family": fam, "value": "fusion", "status": "unclassified",
                "raw": f"{fam}:fusion"} in got
        # situations — the analyst write path and the clusterer's upsert
        out, dlq = await write_situation(
            conn, analyst_ctx=ctx,
            payload={"name": f"Sit {uuid4().hex[:6]}", "category": "country_g20_br",
                     "situation_signature": f"sig-{uuid4().hex}"},
            derived_from=[sig],
        )
        assert dlq is None, dlq
        got = _cls(await conn.fetchval(
            "SELECT data FROM situations WHERE analyst_id = $1", ctx.analyst_id))
        assert got == [{"family": "src", "value": "country_g20_br", "status": "legacy_src"}]
        sig_key = f"sig-{uuid4().hex}"
        await _upsert_situation(
            conn,
            fields={
                "situation_signature": sig_key, "member_finding_ids": [str(uuid4())],
                "name": "Clustered", "status": "active", "category": f"{fam}:energy",
                "last_event_at": datetime.now(tz=timezone.utc), "event_count": 1,
                "intensity_score": 0.5, "valid_from": datetime.now(tz=timezone.utc),
                "valid_until": None,
            },
            analyst_id=ctx.analyst_id, analyst_version="t", target_id=None, run_id=None,
        )
        got = _cls(await conn.fetchval(
            "SELECT data FROM situations WHERE situation_signature = $1", sig_key))
        assert got == [{"family": fam, "value": "energy", "status": "bound"}]
        # events
        monkeypatch.setenv("LEGBA_EVENTS", "1")
        evt_sig = f"evt:tag1 {uuid4().hex[:8]}#evt:xx"
        out, dlq = await write_event(
            conn, analyst_ctx=ctx,
            payload=EventPayload(
                event_signature=evt_sig, title="An occurrence", category=f"{fam}:nope",
                signals=[EventSignalLinkPayload(
                    signal_id=sig, linked_at=datetime.now(tz=timezone.utc),
                    source_class="reporting")],
            ),
            derived_from=[sig],
        )
        assert dlq is None, dlq
        got = _cls(await conn.fetchval(
            "SELECT data FROM events WHERE event_signature = $1", evt_sig))
        assert got == [{"family": fam, "value": "nope", "status": "unclassified",
                        "raw": f"{fam}:nope"}]
        # entity profiles — the resolver
        name = "Halvard " + "".join(random.choices(string.ascii_lowercase, k=8)).title()
        await _seed_signal(
            conn, payload={"title": f"{name} spoke",
                           "entities": [{"text": name, "class": "person"}]},
            fetched_at=datetime(1970, 1, 2, tzinfo=timezone.utc),
        )
    await _resolve_batch(pg, batch_limit=50, analyst_id="entity_resolution")
    async with pg.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT data FROM entity_profiles WHERE lower(canonical_name) = lower($1)", name)
    assert raw is not None, "the resolver did not write the profile"
    assert _cls(raw) == [{"family": "entity_class", "value": "person", "status": "bound"}]


# ---------------------------------------------------------------------------
# scripts/reclassify_tags.py
# ---------------------------------------------------------------------------


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "reclassify_tags", REPO_ROOT / "scripts" / "reclassify_tags.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_counts_on_seeded_rows(pg, profile):
    rt = _load_script()
    idx = await _install_db_index(pg)
    src = f"tag1_{uuid4().hex[:8]}"
    fam = f"{profile}.domain"
    async with pg.acquire() as conn:
        await _seed_signal(conn, source_id=src, tags=["world_news", "Software", f"{fam}:power"])
        await _seed_signal(conn, source_id=src, tags=["world_news", "entity_class:nonesuch"])
        await _seed_signal(conn, source_id=src, tags=[f"{profile}bare:x"])
        await _seed_signal(conn, source_id=src, tags=[])
        census = await rt.take_census(conn, batch_size=2, source_id=src)
    assert census.rows_scanned == 4 and census.rows_tagged == 3
    rep = rt.summarize(census, idx)
    assert rep["distinct_values"] == 5 and rep["tag_occurrences"] == 6
    assert rep["by_status"] == {
        "bound": {"values": 1, "occurrences": 1},
        "legacy_src": {"values": 3, "occurrences": 4},
        "unclassified": {"values": 1, "occurrences": 1},
    }
    assert rep["top_unclassified"] == [
        {"raw": "entity_class:nonesuch", "occurrences": 1, "family": "entity_class"},
    ]
    assert rep["top_legacy_src"][0] == {"raw": "world_news", "occurrences": 2}
    # "<prof>bare:x" is not family-shaped (no dot) → a bare value
    assert {"raw": f"{profile}bare:x", "occurrences": 1} in rep["top_legacy_src"]
    assert {"raw": "Software", "occurrences": 1,
            "candidates": ["entity_class:software"]} in rep["crosswalk_candidates"]
    text = rt.render(rep)
    assert "crosswalk candidates" in text and "legacy_src" in text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_writes_in_batches_and_resumes_from_its_cursor(pg, profile, tmp_path):
    rt = _load_script()
    idx = await _install_db_index(pg)
    src = f"tag1_{uuid4().hex[:8]}"
    fam = f"{profile}.domain"
    async with pg.acquire() as conn:
        ids = [
            await _seed_signal(conn, source_id=src, tags=["world_news", f"{fam}:power"]),
            await _seed_signal(conn, source_id=src, tags=["sport"]),
            await _seed_signal(conn, source_id=src, tags=[]),
            await _seed_signal(conn, source_id=src, tags=["entity_class:person"]),
            await _seed_signal(conn, source_id=src, tags=["x.y:z"]),
        ]
        cur = tmp_path / "cursor.json"
        st = await rt.apply(conn, idx, cursor_path=cur, batch_size=2, max_batches=1,
                            source_id=src)
        assert st["batches"] == 1 and st["rows_scanned"] == 2 and not st["done"]
        saved = json.loads(cur.read_text())
        assert saved["last_id"] == st["last_id"]
        written_first = await conn.fetchval(
            "SELECT count(*) FROM signals WHERE source_id = $1 AND data IS NOT NULL", src)
        assert written_first == st["rows_written"] >= 1
        st = await rt.apply(conn, idx, cursor_path=cur, batch_size=2, source_id=src)
        assert st["done"] and st["batches"] == 3 and st["rows_scanned"] == 5
        assert st["rows_written"] == 4
        rows = {r["id"]: r for r in await conn.fetch(
            "SELECT id, data, tags FROM signals WHERE source_id = $1", src)}
    assert rows[ids[2]]["data"] is None  # untagged: never classified
    assert _cls(rows[ids[0]]["data"]) == [
        {"family": "src", "value": "world_news", "status": "legacy_src"},
        {"family": fam, "value": "energy", "status": "bound"},
    ]
    assert _cls(rows[ids[3]]["data"]) == [
        {"family": "entity_class", "value": "person", "status": "bound"},
    ]
    assert _cls(rows[ids[4]]["data"]) == [
        {"family": "x.y", "value": "z", "status": "unclassified", "raw": "x.y:z"},
    ]
    assert rows[ids[0]]["tags"] == ["world_news", f"{fam}:power"]  # never rewritten
    # a source-mismatched cursor refuses rather than silently restarting
    with pytest.raises(SystemExit):
        async with pg.acquire() as conn:
            await rt.apply(conn, idx, cursor_path=cur, source_id="other")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_w10_f2_apply_unions_with_a_tax2_stamped_rows_declared_entries(pg, profile, tmp_path):
    """The reclassify apply no longer SETS the key from tags alone: a
    TAX-2-stamped row keeps its source's ``src:`` scope entries (no raw tag
    yields them), its tag-derived entries are re-bound, and a bare-tag row gets
    its ``legacy_src`` entries. A second apply over its own output is a no-op."""
    rt = _load_script()
    idx = await _install_db_index(pg)
    src = f"tag1_{uuid4().hex[:8]}"
    fam = f"{profile}.domain"
    tax2 = [
        {"family": fam, "value": "energy", "status": "bound"},
        {"family": "src", "value": "wire_feed", "status": "legacy_src"},
    ]
    async with pg.acquire() as conn:
        stamped = await _seed_signal(conn, source_id=src, tags=[f"{fam}:power", "world_news"])
        await conn.execute(
            "UPDATE signals SET data = $2::jsonb WHERE id = $1",
            stamped, json.dumps({"classifications": tax2}),
        )
        bare = await _seed_signal(conn, source_id=src, tags=["sport"])
        cur = tmp_path / "cursor.json"
        st = await rt.apply(conn, idx, cursor_path=cur, batch_size=10, source_id=src)
        assert st["done"] and st["rows_written"] == 2
        rows = {r["id"]: r["data"] for r in await conn.fetch(
            "SELECT id, data FROM signals WHERE source_id = $1", src)}
        assert _cls(rows[stamped]) == [
            {"family": fam, "value": "energy", "status": "bound"},
            {"family": "src", "value": "wire_feed", "status": "legacy_src"},  # intact
            {"family": "src", "value": "world_news", "status": "legacy_src"},
        ]
        assert _cls(rows[bare]) == [{"family": "src", "value": "sport", "status": "legacy_src"}]
        # Idempotent: a second apply rewrites the same lists.
        await rt.apply(conn, idx, cursor_path=cur, batch_size=10, source_id=src, restart=True)
        again = {r["id"]: r["data"] for r in await conn.fetch(
            "SELECT id, data FROM signals WHERE source_id = $1", src)}
    assert _cls(again[stamped]) == _cls(rows[stamped])
    assert _cls(again[bare]) == _cls(rows[bare])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0228_adds_a_nullable_signals_data_column(migrated_pg):
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        row = await conn.fetchrow(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns"
            " WHERE table_name = 'signals' AND column_name = 'data'")
    finally:
        await conn.close()
    assert row is not None
    assert (row["data_type"], row["is_nullable"], row["column_default"]) == ("jsonb", "YES", None)
    sql = (REPO_ROOT / "src/legba/data/migrations/0228_signal_classifications.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS data jsonb" in sql


def test_the_family_shape_rule_mirrors_the_vocabulary_schema():
    from legba.data.schemas import vocabulary as sv

    assert tags_mod.PLATFORM_FAMILIES == sv.PLATFORM_FAMILIES
    assert tags_mod.NAMESPACED_FAMILY_RE.pattern == sv.NAMESPACED_FAMILY_RE.pattern


def test_tags_is_a_stdlib_leaf():
    import ast

    tree = ast.parse((REPO_ROOT / "src/legba/data/tags.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"relative import in tags.py: {node.module}"
            assert not (node.module or "").startswith("legba"), node.module
        elif isinstance(node, ast.Import):
            assert all(not a.name.startswith("legba") for a in node.names)


def test_classifications_shape_is_the_documented_one():
    out = classifications(["entity_class:person", "a.b:c", "plain"], families=_idx())
    for c in out:
        assert set(c) <= {"family", "value", "status", "raw"}
        assert c["status"] in tags_mod.STATUSES
