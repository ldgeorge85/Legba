# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``reenrich_pending`` deterministic sub-handler (W11-F118).

The sweep over rows the source actor wrote UNENRICHED (``payload.enrichment_pending``,
W11-F103). DETERMINISTIC — a recording fake pg_pool (the reenrich_ner idiom), a fake
registry serving a real source descriptor, and a fake host enrichment factory /
fake NLP client. Covered:

  * registration (TRACE_ONLY) and the zeroed synthetic run;
  * selection — the index-friendly predicate, oldest first, the per-run bound, and
    that migration 0239's partial index carries exactly that predicate;
  * the descriptor chain — the row's own source descriptor is fetched (once per
    source per tick) and its stage runs through ``run_baseline``; the row is updated
    in place and the marker removed;
  * failure leaves the marker — a silent stage degradation, a chain error and a
    timeout; attempts are counted only on a tick where another row was enriched;
    the first timeout with nothing enriched stops the tick;
  * the fallback chain (no resolvable descriptor) — the live NER handler;
  * the declared ``pending_batch`` option and the deps wiring.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import yaml

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    TRACE_ONLY,
    run_method,
)
from legba.data.analysts.deterministic_handlers import reenrich_pending as rp
from legba.data.option_catalog.handler_options import (
    known_option_names,
    resolve_handler_options,
)
from legba.data.stack.nlp_service import NlpServiceUnavailable
from legba.runtime.analyst_deps_reenrich import (
    _wire_reenrich_pending,
    wire_reenrich_kind_deps,
)

SUB = "reenrich_pending"
_REPO = Path(__file__).resolve().parents[2]
_SOURCE_YAML = _REPO / "descriptors" / "source_spiegel_international.yaml"
_SOURCE_ID = "source.spiegel.international"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, pool):
        self._pool = pool

    async def fetch(self, sql, *args):
        self._pool.calls.append(("fetch", sql, args))
        return self._pool.rows

    async def fetchval(self, sql, *args):
        self._pool.calls.append(("fetchval", sql, args))
        return self._pool.backlog

    async def execute(self, sql, *args):
        self._pool.calls.append(("execute", sql, args))
        return "UPDATE 1"


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, rows, *, backlog=0):
        self.rows = rows
        self.backlog = backlog
        self.calls: list[tuple] = []

    def acquire(self):
        return _Acquire(_FakeConn(self))

    def executed(self, sql):
        return [c for c in self.calls if c[0] == "execute" and c[1] == sql]


class _FakeRegistry:
    def __init__(self, typed):
        self.typed = typed
        self.calls: list[tuple[str, str]] = []

    async def get_descriptor_typed(self, descriptor_id, *, family):
        self.calls.append((descriptor_id, family))
        return self.typed.get(descriptor_id)


class _FakeHostFactory:
    """Stands in for ``dapr_host._source_enrichment_factory``: an async
    ``sd -> stage`` whose stage annotates + promotes, and carries the
    ``enrichment_state`` the host stage exposes. ``degrade_on`` makes the stage
    fall back to the unenriched signal (the host's silent path) for a title."""

    def __init__(self, *, degrade_on: str | None = None, raise_on: str | None = None,
                 sleep_s: float = 0.0):
        self.degrade_on = degrade_on
        self.raise_on = raise_on
        self.sleep_s = sleep_s
        self.built_for: list[str] = []
        self.seen: list[Any] = []

    async def __call__(self, sd):
        self.built_for.append(sd.identity.id)
        state = {"handlers": [], "fallbacks": 0}

        async def _stage(signal, ctx):
            self.seen.append((signal, ctx))
            title = signal.payload.get("title", "")
            if self.sleep_s:
                await asyncio.sleep(self.sleep_s)
            if self.raise_on and self.raise_on in title:
                raise RuntimeError("chain boom")
            if self.degrade_on and self.degrade_on in title:
                state["fallbacks"] += 1
                return signal
            signal.payload["language"] = "de"
            signal.payload["entities"] = [{"text": "Olaf Scholz", "class": "person"}]
            signal.payload["geo"] = {"country_iso2": "DE"}
            signal.language = signal.language or "de"
            signal.geo.append("DE")
            signal.entity_classes.append("person")
            return signal

        _stage.enrichment_state = state
        return _stage


class _FakeNlp:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.extract_calls: list[str] = []

    async def translate(self, text, *, source_lang, target_lang):
        return {"translated": f"TR({source_lang}): {text}"}

    async def extract(self, text):
        self.extract_calls.append(text)
        if self.fail:
            raise NlpServiceUnavailable("extract backend boom")
        return {"triples": [
            {"subject": "Vladimir Putin", "predicate": "met", "object": "Joe Biden"},
        ]}


@dataclass
class _Deps:
    pg_pool: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


def _typed():
    return {_SOURCE_ID: yaml.safe_load(_SOURCE_YAML.read_text())}


def _row(title, *, source_id=_SOURCE_ID, reason="nlp_degraded_this_poll", **extra):
    payload = {"title": title, "summary": "Der Kanzler sprach in Berlin.",
               "enrichment_pending": reason, **extra}
    return {
        "id": uuid4(), "source_id": source_id, "language": None, "geo": [],
        "tags": ["news"], "entity_classes": [], "payload": payload,
        "canonical_url": f"https://example.org/{uuid4()}", "content_hash": "h",
    }


def _descriptor_deps(pool, factory, registry=None):
    return _Deps(pg_pool=pool, extras={
        rp.REGISTRY_DEPS_EXTRA_KEY: registry or _FakeRegistry(_typed()),
        rp.STAGE_FACTORY_DEPS_EXTRA_KEY: factory,
    })


async def _run(deps, **options):
    return (await rp.handle([], {"sub_handler": SUB, **options}, deps)).finding.data


# ---------------------------------------------------------------------------
# Registration / synthetic / no chain
# ---------------------------------------------------------------------------


def test_registered_trace_only():
    assert SUB_HANDLERS[SUB] is rp.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER[SUB] is TRACE_ONLY


async def test_synthetic_no_deps_zeroed_run():
    result = await run_method([], {"sub_handler": SUB, "analyst_id": "rp", "run_id": uuid4()}, None)
    data = result.finding.data
    assert {k: data[k] for k in ("examined", "enriched", "still_pending", "failed")} == {
        "examined": 0, "enriched": 0, "still_pending": 0, "failed": 0,
    }
    assert result.usage == {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


async def test_no_chain_wired_is_a_no_op_tick():
    pool = _FakePool([_row("x")])
    data = await _run(_Deps(pg_pool=pool))
    assert data["skipped_no_chain"] == 1 and data["examined"] == 0
    assert pool.calls == []


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


async def test_selection_is_the_marker_oldest_first_and_bounded():
    pool = _FakePool([])
    data = await _run(_descriptor_deps(pool, _FakeHostFactory()))
    assert data["examined"] == 0
    (kind, sql, args), = [c for c in pool.calls if c[0] == "fetch"]
    assert "payload ? 'enrichment_pending'" in sql
    assert "ORDER BY fetched_at ASC" in sql and "LIMIT $1" in sql
    assert args == (200, rp.PENDING_MAX_ATTEMPTS)  # default pending_batch


async def test_the_batch_bound_is_the_pending_batch_option():
    pool = _FakePool([])
    await _run(_descriptor_deps(pool, _FakeHostFactory()), pending_batch=7)
    (_, _, args), = [c for c in pool.calls if c[0] == "fetch"]
    assert args == (7, rp.PENDING_MAX_ATTEMPTS)


def test_migration_0239_indexes_exactly_the_sweep_predicate():
    sql = (_REPO / "src/legba/data/migrations/0239_signal_enrichment_pending_index.sql").read_text()
    upper = sql.upper()
    assert "BEGIN;" not in upper and "COMMIT;" not in upper
    assert "CREATE INDEX IF NOT EXISTS idx_signals_enrichment_pending" in sql
    assert "USING btree (fetched_at)" in sql
    assert "WHERE payload ? 'enrichment_pending';" in sql
    # the same predicate the SELECT and the backlog count use
    assert "payload ? 'enrichment_pending'" in rp._SELECT_BATCH_SQL
    assert "payload ? 'enrichment_pending'" in rp._COUNT_PENDING_SQL


# ---------------------------------------------------------------------------
# The descriptor chain — in-place update + marker removal
# ---------------------------------------------------------------------------


async def test_descriptor_chain_updates_in_place_and_removes_the_marker():
    rows = [_row("Scholz in Berlin"), _row("Scholz in Paris", reason="enrich_hold_exhausted")]
    pool = _FakePool(rows, backlog=3)
    factory = _FakeHostFactory()
    registry = _FakeRegistry(_typed())
    data = await _run(_descriptor_deps(pool, factory, registry))

    assert (data["examined"], data["enriched"], data["failed"], data["still_pending"]) == (2, 2, 0, 3)
    assert data["fallback_chain"] == 0
    # the row's own source descriptor, fetched once for the two rows of that source
    assert registry.calls == [(_SOURCE_ID, "source")]
    assert factory.built_for == [_SOURCE_ID]
    # run_baseline drove the stage: tier 1 parked the descriptor's origin first
    signal, ctx = factory.seen[0]
    assert ctx.source_id == _SOURCE_ID and ctx.scope_geo == ["DE"]
    assert signal.payload["publisher_origin"] == ["DE"]

    writes = pool.executed(rp._WRITE_ENRICHED_SQL)
    assert [w[2][0] for w in writes] == [r["id"] for r in rows]
    _, _, (sid, changed_json, language, geo, tags, classes) = writes[0]
    changed = json.loads(changed_json)
    assert "enrichment_pending" not in changed and "title" not in changed  # changed keys only
    assert changed["entities"] == [{"text": "Olaf Scholz", "class": "person"}]
    assert changed["language"] == "de"
    assert (language, geo, tags, classes) == ("de", ["DE"], ["news"], ["person"])
    sql = rp._WRITE_ENRICHED_SQL
    assert "- 'enrichment_pending' - 'enrichment_pending_attempts'" in sql
    assert "COALESCE(payload, '{}'::jsonb) || $2::jsonb" in sql
    assert "entities_resolved_at = NULL" in sql and "indexed_at = NULL" in sql
    assert "AND payload ? 'enrichment_pending'" in sql
    assert pool.executed(rp._BUMP_ATTEMPTS_SQL) == []


async def test_a_source_with_no_enrichment_stages_is_simply_unmarked():
    typed = _typed()
    typed[_SOURCE_ID]["pipeline"]["enrichment"] = []

    async def _factory(sd):
        return None  # the host factory's answer for a chain-less source

    pool = _FakePool([_row("x")])
    data = await _run(_descriptor_deps(pool, _factory, _FakeRegistry(typed)))
    assert (data["enriched"], data["failed"]) == (1, 0)
    assert len(pool.executed(rp._WRITE_ENRICHED_SQL)) == 1


# ---------------------------------------------------------------------------
# Failure leaves the marker
# ---------------------------------------------------------------------------


async def test_a_silently_degraded_row_keeps_its_marker_and_counts_an_attempt():
    good, bad = _row("Scholz in Berlin"), _row("POISON headline")
    pool = _FakePool([bad, good], backlog=1)
    data = await _run(_descriptor_deps(pool, _FakeHostFactory(degrade_on="POISON")))
    assert (data["examined"], data["enriched"], data["failed"], data["still_pending"]) == (2, 1, 1, 1)
    assert [w[2][0] for w in pool.executed(rp._WRITE_ENRICHED_SQL)] == [good["id"]]
    # another row was enriched → the plane is healthy → count the attempt
    (_, sql, (ids,)), = pool.executed(rp._BUMP_ATTEMPTS_SQL)
    assert ids == [bad["id"]]
    assert "jsonb_set(" in sql and "{enrichment_pending_attempts}" in sql
    assert "- 'enrichment_pending'" not in sql  # the marker stays


async def test_a_chain_error_keeps_the_marker_and_an_all_fail_tick_counts_nothing():
    rows = [_row("BOOM one"), _row("BOOM two")]
    pool = _FakePool(rows, backlog=2)
    data = await _run(_descriptor_deps(pool, _FakeHostFactory(raise_on="BOOM")))
    assert (data["examined"], data["enriched"], data["failed"], data["still_pending"]) == (2, 0, 2, 2)
    assert pool.executed(rp._WRITE_ENRICHED_SQL) == []
    assert pool.executed(rp._BUMP_ATTEMPTS_SQL) == []  # probable outage: no attempt


async def test_the_first_timeout_with_nothing_enriched_stops_the_tick(monkeypatch):
    monkeypatch.setattr(rp, "REENRICH_TIMEOUT_SECONDS", 0.01)
    rows = [_row("a"), _row("b"), _row("c")]
    pool = _FakePool(rows, backlog=3)
    factory = _FakeHostFactory(sleep_s=0.2)
    data = await _run(_descriptor_deps(pool, factory))
    assert (data["examined"], data["enriched"], data["failed"]) == (3, 0, 1)
    assert len(factory.seen) == 1  # one stall, not three
    assert pool.executed(rp._WRITE_ENRICHED_SQL) == []
    assert pool.executed(rp._BUMP_ATTEMPTS_SQL) == []


def test_the_attempt_cap_filters_within_the_pending_pool():
    sql = rp._SELECT_BATCH_SQL
    assert "enrichment_pending_attempts" in sql and "< $2" in sql
    assert rp.PENDING_MAX_ATTEMPTS == 5


# ---------------------------------------------------------------------------
# The fallback chain
# ---------------------------------------------------------------------------


async def test_unresolvable_descriptor_falls_back_to_the_live_ner_chain():
    row = _row("Vladimir Putin met Joe Biden", source_id="source.retired")
    pool = _FakePool([row])
    nlp = _FakeNlp()
    deps = _descriptor_deps(pool, _FakeHostFactory(), _FakeRegistry({}))
    deps.extras[rp.NLP_DEPS_EXTRA_KEY] = nlp
    data = await _run(deps)
    assert (data["enriched"], data["fallback_chain"], data["failed"]) == (1, 1, 0)
    assert nlp.extract_calls, "the live NER handler ran"
    (_, _, (_, changed_json, _lang, _geo, _tags, classes)), = pool.executed(rp._WRITE_ENRICHED_SQL)
    assert json.loads(changed_json)["entities"]
    assert "person" in classes


async def test_fallback_ner_failure_keeps_the_marker():
    pool = _FakePool([_row("Vladimir Putin met Joe Biden")])
    data = await _run(_Deps(pg_pool=pool, extras={rp.NLP_DEPS_EXTRA_KEY: _FakeNlp(fail=True)}))
    assert (data["enriched"], data["failed"]) == (0, 1)
    assert pool.executed(rp._WRITE_ENRICHED_SQL) == []


# ---------------------------------------------------------------------------
# The declared option + the wiring
# ---------------------------------------------------------------------------


def test_pending_batch_is_declared_and_read():
    assert known_option_names(SUB) == ("pending_batch",)
    assert resolve_handler_options(SUB, {"pending_batch": 50}).accepted == {"pending_batch": 50}
    for bad in (0, 5001, "many"):
        assert resolve_handler_options(SUB, {"pending_batch": bad}).rejected
    src = (_REPO / "src/legba/data/analysts/deterministic_handlers/reenrich_pending.py").read_text()
    assert 'options.get("pending_batch"' in src


async def test_wiring_threads_both_chains_only_for_the_bound_sub_handler():
    descriptor = SimpleNamespace(identity=SimpleNamespace(id="reenrich_pending"))
    registry, factory, nlp = object(), object(), object()
    wired = await _wire_reenrich_pending(
        descriptor, _Deps(), nlp_client=nlp, registry_client=registry,
        source_enrichment_factory=factory,
    )
    assert wired.extras == {
        rp.REGISTRY_DEPS_EXTRA_KEY: registry,
        rp.STAGE_FACTORY_DEPS_EXTRA_KEY: factory,
        rp.NLP_DEPS_EXTRA_KEY: nlp,
    }
    bare = _Deps()
    assert await _wire_reenrich_pending(
        descriptor, bare, nlp_client=None, registry_client=registry,
        source_enrichment_factory=None,
    ) is bare
    other = _Deps()
    assert await wire_reenrich_kind_deps(
        descriptor, other, sub_handler="signal_embedder", nlp_client=nlp,
        registry_client=registry, source_enrichment_factory=factory,
    ) is other


def test_the_host_exposes_its_stage_state_and_threads_the_factory():
    src = (_REPO / "src/legba/runtime/dapr_host.py").read_text()
    assert "source_enrichment_factory=lambda sd: _source_enrichment_factory(sd)" in src
    assert '_stage.enrichment_state = state' in src
    assert 'state["fallbacks"] += 1' in src
