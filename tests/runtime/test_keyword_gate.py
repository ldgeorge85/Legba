# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W3 SRC-3 — the ``keyword_gate`` filter kind + the RSS keyword sugar.

Matcher semantics (whole-word, phrase, include-none, exclude-wins, Unicode,
mode any/all), the real ``build_filter_handler`` registration, the source
schema's refusal of a gate under ``pipeline.enrichment``, the worked example
descriptors, and the ingest ORDER: a dropped item never reaches the
enrichment stage — proven by driving the real SourceCore poll path (gates
built through the real pipeline builder) with a fake enrichment stage that
records every call, plus the per-source drop counter in the cursor row.
DB-free: the SourceCore doubles mirror tests/runtime/test_source_actor_cursor.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from pydantic import ValidationError

from legba.data.filters.keyword_gate import (
    REASON_INCLUDE_NONE,
    KeywordGate,
    KeywordGateConfig,
    KeywordGateHandler,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock,
    FilterStage,
    SourceDescriptor,
    SourceIdentity,
    SourcePipeline,
    SourceScope,
)
from legba.data.sources._contract import InMemoryStateStore, Signal, SourceContext
from legba.data.sources.rss import RSSConfig, RSSSourceHandler
from legba.runtime.deps import StandardDeps
from legba.runtime.pipeline import _KNOWN_KINDS, PipelineRunner, build_filter_handler
from legba.runtime.source_actor import SourceCore, SourceDeps, _RawConfig
from legba.runtime.source_factory import _unwrap_factory_dict
from legba.runtime.source_keyword_gate import SourceIngestGate

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "descriptors" / "examples"


def _sig(title: str = "", raw_body: str = "", **extra) -> Signal:
    payload = {"title": title, "raw_body": raw_body, **extra}
    return Signal(source_id="source.test.kw", modality="text", payload=payload)


def _gate(**cfg) -> KeywordGate:
    return KeywordGate(KeywordGateConfig(**cfg))


# ---------------------------------------------------------------------------
# Matcher semantics
# ---------------------------------------------------------------------------


def test_whole_word_no_substring_surprises():
    g = _gate(include=["iran"])
    assert not g.evaluate(_sig("Festival in Mirandola opens")).admit
    assert not g.evaluate(_sig("Iranian ministers meet")).admit
    assert g.evaluate(_sig("Iran-backed group claims strike")).admit
    assert g.evaluate(_sig("IRAN's envoy speaks")).admit
    assert g.evaluate(_sig("talks with Iran.")).admit


def test_phrase_matches_across_whitespace_but_not_inflection():
    g = _gate(include=["export control"])
    assert g.evaluate(_sig("New EXPORT\n   Control rule")).admit
    assert not g.evaluate(_sig("new export controls announced")).admit
    assert not g.evaluate(_sig("the export controller resigned")).admit
    assert not g.evaluate(_sig("export of control systems")).admit


def test_include_none_drops_with_reason():
    d = _gate(include=["sanctions", "entity list"]).evaluate(_sig("Weather today"))
    assert d.admit is False and d.reason == REASON_INCLUDE_NONE


def test_exclude_wins_over_include():
    g = _gate(include=["sanctions"], exclude=["job vacancy"])
    d = g.evaluate(_sig("Sanctions office: job vacancy posted"))
    assert d.admit is False and d.reason == "exclude:job vacancy"
    assert g.evaluate(_sig("New sanctions designations")).admit


def test_exclude_only_admits_everything_else():
    g = _gate(exclude=["football"])
    assert g.evaluate(_sig("Parliament votes")).admit
    assert not g.evaluate(_sig("Football results")).admit


def test_unicode_aware_matching():
    # casefold: ß == ss; NFKC: a decomposed ü equals the composed one.
    assert _gate(include=["straße"]).evaluate(_sig("STRASSE closed")).admit
    assert _gate(include=["zürich"]).evaluate(_sig("Zürich court")).admit
    # Cyrillic whole-word: a word boundary is a Unicode \w boundary.
    g = _gate(include=["москва"])
    assert g.evaluate(_sig("Заявление: МОСКВА отвергла")).admit
    assert not g.evaluate(_sig("Москварека")).admit
    # Accented letters are word characters — no partial match inside a word.
    assert not _gate(include=["sao"]).evaluate(_sig("são paulo")).admit


def test_mode_any_vs_all():
    any_g = _gate(include=["sanctions", "russia"], mode="any")
    all_g = _gate(include=["sanctions", "russia"], mode="all")
    one = _sig("New sanctions on shipping")
    both = _sig("New sanctions on Russia")
    assert any_g.evaluate(one).admit and any_g.evaluate(both).admit
    d = all_g.evaluate(one)
    assert d.admit is False and d.reason == "include:missing:russia"
    assert all_g.evaluate(both).admit


def test_case_sensitive_mode_and_fields_and_html():
    cs = _gate(include=["EU"], case_insensitive=False)
    assert cs.evaluate(_sig("EU ministers")).admit
    assert not cs.evaluate(_sig("eu ministers")).admit
    # Only the named fields are scanned.
    title_only = _gate(include=["sanctions"], fields=["title"])
    assert not title_only.evaluate(_sig("Daily brief", raw_body="sanctions")).admit
    # A term inside an HTML attribute does not admit; entities are unescaped.
    g = _gate(include=["iran"])
    assert not g.evaluate(_sig("x", raw_body='<a href="/iran/">read</a>')).admit
    assert _gate(include=["at&t"]).evaluate(_sig("x", raw_body="AT&amp;T deal")).admit


def test_config_refuses_empty_gate_and_bad_terms():
    with pytest.raises(ValidationError):
        KeywordGateConfig()
    with pytest.raises(ValidationError):
        KeywordGateConfig(include=["  "])
    with pytest.raises(ValidationError):
        KeywordGateConfig(include=["a"], mode="most")
    with pytest.raises(ValidationError):
        KeywordGateConfig(include=["a"], bogus=1)
    assert KeywordGate.from_lists([], []) is None


def test_admit_counts_and_drain_resets():
    g = _gate(include=["sanctions"], exclude=["vacancy"])
    assert g.admit(_sig("sanctions"))
    assert not g.admit(_sig("weather"))
    assert not g.admit(_sig("sanctions vacancy"))
    s = g.drain_stats()
    assert s.dropped == 2
    assert dict(s.by_reason) == {REASON_INCLUDE_NONE: 1, "exclude:vacancy": 1}
    assert g.drain_stats().dropped == 0


def test_drop_is_logged_at_debug_with_term_never_body(caplog):
    secret = "BODY-TEXT-MUST-NOT-BE-LOGGED"
    g = _gate(exclude=["vacancy"])
    with caplog.at_level("DEBUG", logger="legba.data.filters.keyword_gate"):
        assert not g.admit(_sig("vacancy notice", raw_body=secret))
    text = caplog.text
    assert "exclude:vacancy" in text
    assert secret not in text and "vacancy notice" not in text
    assert all(r.levelname == "DEBUG" for r in caplog.records)


# ---------------------------------------------------------------------------
# Registration in the real pipeline builder
# ---------------------------------------------------------------------------


def test_registered_in_build_filter_handler():
    assert "keyword_gate" in _KNOWN_KINDS
    h = build_filter_handler(kind="keyword_gate", config={"include": ["x"]})
    assert isinstance(h, KeywordGateHandler)
    with pytest.raises(ValueError):
        build_filter_handler(kind="keyword_gate", config={})


@pytest.mark.asyncio
async def test_pipeline_runner_drop_never_reaches_later_stage():
    """In a real PipelineRunner chain the gate's None stops the signal before
    the next stage (a recorder standing in for an enrichment filter)."""
    seen: list[str] = []

    class _Recorder:
        async def transform(self, signal, ctx):
            seen.append(signal.payload["title"])
            return signal

    gate = build_filter_handler(kind="keyword_gate", config={"include": ["sanctions"]})
    runner = PipelineRunner(
        [("keyword_gate", gate), ("recorder", _Recorder())],
        ctx_factory=lambda fid: SourceContext(
            target_id="t", target_version="v", source_id="s",
            config=_RawConfig(), state_store=InMemoryStateStore(),
        ),
    )

    async def _gen():
        for t in ("sanctions on X", "weather", "football"):
            yield _sig(t)

    out = [s.payload["title"] async for s in runner.run(_gen())]
    assert out == ["sanctions on X"]
    assert seen == ["sanctions on X"]


# ---------------------------------------------------------------------------
# Descriptor schema + the worked examples
# ---------------------------------------------------------------------------


def _load_example(name: str) -> SourceDescriptor:
    body = yaml.safe_load((EXAMPLES / name).read_text())
    return SourceDescriptor.model_validate(body, strict=False)


def test_schema_accepts_gate_in_ingestion_filters_refuses_in_enrichment():
    stage = FilterStage(kind="keyword_gate", config={"include": ["x"]})
    assert SourcePipeline(ingestion_filters=[stage]).ingestion_filters[0].kind == "keyword_gate"
    with pytest.raises(ValidationError, match="ingestion_filters"):
        SourcePipeline(enrichment=[stage])


def test_example_descriptors_validate_as_the_registrars_do():
    stage_form = _load_example("source_example_keyword_gate_stage.yaml")
    gates = SourceIngestGate(stage_form.pipeline.ingestion_filters)
    assert gates.gated
    sugar_form = _load_example("source_example_keyword_rss_sugar.yaml")
    cfg = RSSConfig(**_unwrap_factory_dict(sugar_form.config))
    assert cfg.keyword_exclude and cfg.keyword_include == []
    assert isinstance(RSSSourceHandler(cfg).keyword_gate, KeywordGate)
    # An ungated RSS config carries no gate at all.
    assert RSSSourceHandler(RSSConfig(url="http://x")).keyword_gate is None


# ---------------------------------------------------------------------------
# SourceCore: order (gate before enrichment) + the per-source counter
# ---------------------------------------------------------------------------


class _FakeConn:
    async def fetchrow(self, query, *args):
        return {"id": args[0]}

    async def fetchval(self, *a, **k):  # pragma: no cover
        return None

    async def execute(self, *a, **k):
        return None


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def acquire(self):
        return _FakeAcquire()


class _StubHandler:
    def __init__(self, signals):
        self._signals = signals

    async def pull(self, ctx, since=None):
        for s in self._signals:
            yield s


def _descriptor(source_id: str, *, ingestion_filters=(), config=None) -> SourceDescriptor:
    return SourceDescriptor(
        identity=SourceIdentity(
            id=source_id, name="kw-test", kind="rss",
            schema_uri="legba/source/3.0.0", version="a" * 16,
            owner="test:kw", created=datetime.now(tz=timezone.utc),
            state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant="acme", languages=["en"]),
        acquisition="poll",
        config=config or {"url": "http://unused"},
        cadence=CadenceBlock(schedule=Cron(raw="*/5 * * * *")),
        pipeline=SourcePipeline(
            media="reference", ingestion_filters=list(ingestion_filters),
        ),
    )


def _core(sd: SourceDescriptor, handler, store: InMemoryStateStore):
    enriched: list[str] = []

    async def _enrichment(signal, ctx):  # the fake enrichment stage (NER stand-in)
        enriched.append(signal.payload.get("title"))
        return signal

    deps = StandardDeps(pg_pool=_FakePool(), nats_publish=None)
    core = SourceCore(
        f"source::{sd.identity.id}::kw",
        SourceDeps(descriptor=sd, deps=deps, enrichment_stage=_enrichment),
    )
    core.sd.handler = handler

    def _make_context():
        return SourceContext(
            target_id=sd.identity.id, target_version=sd.identity.version,
            source_id=sd.identity.id, config=_RawConfig(**(sd.config or {})),
            state_store=store,
        )

    core._make_context = _make_context  # type: ignore[method-assign]
    return core, enriched


def _entry(title: str, ts: datetime) -> Signal:
    return Signal(
        source_id="source.test.kw", modality="text",
        payload={"title": title, "_published_at_dt": ts}, content_hash=uuid4().hex,
    )


@pytest.mark.asyncio
async def test_stage_drop_never_reaches_enrichment_and_is_counted():
    sid = f"source.test.kw_{uuid4().hex[:8]}"
    sd = _descriptor(sid, ingestion_filters=[
        FilterStage(kind="keyword_gate", config={
            "include": ["sanctions"], "exclude": ["vacancy"],
        }),
        FilterStage(kind="dedupe_tier_1", config={}),
    ])
    base = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    titles = ["New sanctions listed", "Weather", "Sanctions vacancy", "More sanctions"]
    entries = [_entry(t, base + timedelta(minutes=i)) for i, t in enumerate(titles)]
    store = InMemoryStateStore()
    core, enriched = _core(sd, _StubHandler(entries), store)

    result = await core.pull_once()

    assert enriched == ["New sanctions listed", "More sanctions"]
    assert result["signals_written"] == 2
    assert result["keyword_filtered"] == 2
    cursor = await store.get("cursor")
    assert cursor["keyword_filtered_total"] == 2
    assert cursor["keyword_filtered_last_poll"] == 2
    assert cursor["keyword_filtered_by_reason"] == {
        REASON_INCLUDE_NONE: 1, "exclude:vacancy": 1,
    }
    # Dropped entries still count as consumed: the cursor advanced past the
    # newest entry (a drop is judged once, not re-judged next poll).
    assert datetime.fromisoformat(cursor["last_pulled_at"]) == base + timedelta(minutes=3)

    # Second poll accumulates (cumulative total, per-poll last).
    core.sd.handler = _StubHandler([_entry("Weather again", base + timedelta(hours=1))])
    result2 = await core.pull_once()
    cursor2 = await store.get("cursor")
    assert result2["keyword_filtered"] == 1
    assert cursor2["keyword_filtered_total"] == 3
    assert cursor2["keyword_filtered_last_poll"] == 1
    assert cursor2["keyword_filtered_by_reason"][REASON_INCLUDE_NONE] == 2


@pytest.mark.asyncio
async def test_ungated_source_writes_no_counter_keys():
    sid = f"source.test.kw_{uuid4().hex[:8]}"
    sd = _descriptor(sid)
    store = InMemoryStateStore()
    base = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    core, enriched = _core(sd, _StubHandler([_entry("anything", base)]), store)
    result = await core.pull_once()
    assert enriched == ["anything"]
    assert result["keyword_filtered"] is None   # not gated → absent, not 0
    cursor = await store.get("cursor")
    assert not any(k.startswith("keyword_filtered") for k in cursor)


_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Wide feed</title>
<link>https://feed.example.invalid/</link><description>d</description>
<item><title>Sanctions update for shipping</title>
<link>https://feed.example.invalid/1</link><description>body one</description>
<pubDate>Wed, 01 Oct 2026 09:00:00 +0000</pubDate><guid>g1</guid></item>
<item><title>Football results</title>
<link>https://feed.example.invalid/2</link><description>body two</description>
<pubDate>Wed, 01 Oct 2026 09:05:00 +0000</pubDate><guid>g2</guid></item>
<item><title>Festival in Mirandola</title>
<link>https://feed.example.invalid/3</link><description>body three</description>
<pubDate>Wed, 01 Oct 2026 09:10:00 +0000</pubDate><guid>g3</guid></item>
</channel></rss>"""


@pytest.mark.asyncio
async def test_rss_sugar_routes_through_the_same_matcher(monkeypatch):
    """The RSSConfig keyword_* sugar builds the SAME KeywordGate and SourceCore
    applies it pre-enrichment; every decision goes through KeywordGate.evaluate
    (spied) — there is no second matcher."""
    calls: list[str] = []
    real_evaluate = KeywordGate.evaluate

    def _spy(self, signal):
        calls.append(signal.payload.get("title"))
        return real_evaluate(self, signal)

    monkeypatch.setattr(KeywordGate, "evaluate", _spy)

    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=_FEED, request=req))
    cfg = RSSConfig(
        url="https://feed.example.invalid/rss",
        keyword_include=["sanctions", "iran"], keyword_exclude=["football"],
        first_poll_max_entry_age_days=0,  # the fixture feed is old-dated
    )
    handler = RSSSourceHandler(cfg, http_client=httpx.AsyncClient(transport=transport))
    sid = f"source.test.kw_{uuid4().hex[:8]}"
    sd = _descriptor(sid, config={
        "url": cfg.url, "keyword_include": cfg.keyword_include,
        "keyword_exclude": cfg.keyword_exclude,
    })
    store = InMemoryStateStore()
    core, enriched = _core(sd, handler, store)

    result = await core.pull_once()

    assert enriched == ["Sanctions update for shipping"]
    assert sorted(calls) == sorted([
        "Sanctions update for shipping", "Football results", "Festival in Mirandola",
    ])
    assert result["keyword_filtered"] == 2
    cursor = await store.get("cursor")
    assert cursor["keyword_filtered_by_reason"] == {
        "exclude:football": 1, REASON_INCLUDE_NONE: 1,
    }


_PG = dict(
    host="127.0.0.1", port=5432, user="legba", password="legba",
    database="legba_pivot_test",
)


@pytest.mark.asyncio
async def test_counter_persists_on_the_real_driver():
    """The drop counter lands in the source's ``cursor`` row of
    ``actor_filter_state`` on real Postgres (the test DB the sibling
    acquisition tests use) — read back with SQL, not through the store."""
    import asyncpg

    from legba.runtime.state import SCHEMA as STATE_SCHEMA

    try:
        pool = await asyncpg.create_pool(**_PG)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.skip(f"test Postgres unavailable: {exc}")
    try:
        async with pool.acquire() as c:
            await c.execute(STATE_SCHEMA)
        sid = f"source.test.kw_pg_{uuid4().hex[:8]}"
        sd = _descriptor(sid, ingestion_filters=[
            FilterStage(kind="keyword_gate", config={"exclude": ["football"]}),
        ])
        actor_id = f"source::{sid}::kwpg"
        core = SourceCore(actor_id, SourceDeps(
            descriptor=sd, deps=StandardDeps(pg_pool=pool, nats_publish=None),
        ))
        base = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
        core.sd.handler = _StubHandler([
            _entry("Football tonight", base),
            _entry("Football again", base + timedelta(minutes=1)),
        ])
        result = await core.pull_once()
        assert result["keyword_filtered"] == 2 and result["signals_written"] == 0

        async with pool.acquire() as c:
            raw = await c.fetchval(
                "SELECT value FROM public.actor_filter_state "
                "WHERE actor_id=$1 AND filter_id=$2 AND state_key='cursor'",
                actor_id, f"source:{sid}",
            )
            n_signals = await c.fetchval(
                "SELECT count(*) FROM signals WHERE source_id=$1", sid,
            )
        row = raw if isinstance(raw, dict) else __import__("json").loads(raw)
        assert row["keyword_filtered_total"] == 2
        assert row["keyword_filtered_last_poll"] == 2
        assert row["keyword_filtered_by_reason"] == {"exclude:football": 2}
        assert n_signals == 0
    finally:
        async with pool.acquire() as c:
            await c.execute(
                "DELETE FROM public.actor_filter_state WHERE filter_id LIKE "
                "'source:source.test.kw_pg_%'"
            )
            await c.execute(
                "DELETE FROM public.source_poll_outcomes WHERE source_id LIKE "
                "'source.test.kw_pg_%'"
            )
        await pool.close()


@pytest.mark.parametrize("first", [
    "legba.data.filters.keyword_gate",
    "legba.data.filters",
    "legba.data.sources",
    "legba.data.sources.rss",
])
def test_import_order_has_no_cycle(first):
    """rss imports the gate and the filters package imports sources — every
    entry order must import cleanly in a fresh interpreter."""
    import subprocess
    import sys

    code = (
        f"import {first}\n"
        "from legba.data.sources.rss import RSSSourceHandler\n"
        "from legba.data.filters.keyword_gate import KeywordGate\n"
        "from legba.data.filters import LanguageDetectHandler\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.asyncio
async def test_handler_health_check_reports_shape():
    h = build_filter_handler(kind="keyword_gate", config={"include": ["a"], "exclude": ["b"]})
    health = await h.health_check(None)
    assert health.state == "healthy"
    assert health.detail["include_terms"] == 1 and health.detail["exclude_terms"] == 1
