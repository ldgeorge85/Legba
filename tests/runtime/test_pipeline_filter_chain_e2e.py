# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end integration test for the 7-stage acquisition filter chain.

Builds the 7-stage pipeline that the live ``india_energy_infra`` target
descriptor declares (Group F filter e2e validation, post Wave A):

  1. ``language_detect``
  2. ``ner_multilingual``
  3. ``classify``
  4. ``source_credibility``
  5. ``geocode``
  6. ``dedupe_tier_1``
  7. ``dedupe_tier_2``

For each stage we construct the real handler via
:func:`legba.runtime.pipeline.build_filter_handler` — the SAME entry point
the production runtime calls — and feed it the actual deps (Redis fake,
asyncpg-shaped Postgres pool fake, hosted-NLP client wired to
``httpx.MockTransport``, deterministic geocode backend swapped via the
handler's protected backend field). No handler internals are stubbed; the
real ``transform`` runs on each signal.

Five fixture signals flow through. Each filter's expected enrichment is
asserted on the output signal:

  * language_detect → ``payload['language']`` set + ``language_confidence``.
  * ner_multilingual → ``payload['entities']`` populated for signals whose
    /extract mock returns triples.
  * classify → ``payload['classification']`` dict with ``event_type``,
    ``confidence``, ``backend_used``, ``schema``.
  * source_credibility → ``signal.source_credibility`` float, with
    ``below_credibility_threshold`` set per the configured floor.
  * geocode → ``payload['geo']`` dict with ``country`` + ``lat``/``lon``.
  * dedupe_tier_1/2 → duplicates dropped (one Signal twice → second is
    filtered out).

Marked ``slow`` so the data_pkg fast-suite skip honors the gate.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Mapping
from uuid import uuid4

import httpx
import pytest

from legba.data.filters._contract import FilterContext, FilterHealth
from legba.data.filters.geocode import GeocodeResult
from legba.data.sources._contract import Signal
from legba.data.stack.nlp_service import NlpServiceClient
from legba.runtime.pipeline import PipelineRunner, build_filter_handler


pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Substrate fakes — only the surfaces each handler actually touches.
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal in-process Redis covering dedupe + geocode-cache + flush.

    The dedupe handler uses ``get``/``set``/``expire``/``zadd``/
    ``zrangebyscore``/``zremrangebyscore``. The geocode handler uses
    ``get``/``setex`` (CacheStore-shaped wrapper) — we satisfy both via
    a single in-memory KV.
    """

    def __init__(self) -> None:
        self._kv: dict[str, bytes] = {}
        self._zset: dict[str, dict[str, float]] = {}

    async def get(self, name: str) -> Any:
        return self._kv.get(name)

    async def set(self, name: str, value: Any, ex: int | None = None) -> Any:
        if isinstance(value, str):
            value = value.encode("utf-8")
        elif not isinstance(value, (bytes, bytearray)):
            value = str(value).encode("utf-8")
        self._kv[name] = bytes(value)
        return True

    async def setex(self, key: str, ttl: int, value: Any) -> None:
        await self.set(key, value, ex=ttl)

    async def expire(self, name: str, seconds: int) -> bool:
        return True

    async def zadd(self, name: str, mapping: Mapping[str, float]) -> int:
        bucket = self._zset.setdefault(name, {})
        added = 0
        for member, score in mapping.items():
            if member not in bucket:
                added += 1
            bucket[member] = score
        return added

    async def zrangebyscore(
        self,
        name: str,
        min: float,
        max: float,
        withscores: bool = False,
    ) -> list:
        bucket = self._zset.get(name, {})
        members = [(m, s) for m, s in bucket.items() if min <= s <= max]
        members.sort(key=lambda x: x[1])
        if withscores:
            return [(m.encode("utf-8"), s) for m, s in members]
        return [m.encode("utf-8") for m, _ in members]

    async def zremrangebyscore(self, name: str, min: float, max: float) -> int:
        bucket = self._zset.get(name)
        if not bucket:
            return 0
        to_remove = [m for m, s in bucket.items() if min <= s <= max]
        for m in to_remove:
            del bucket[m]
        return len(to_remove)


class _FakePoolConnection:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, sql: str, candidates: list[str]) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for c in candidates:
            row = self._rows.get(c)
            if row is not None:
                hits.append(row)
        return hits

    async def fetchval(self, sql: str, *args: Any) -> int:
        return len(self._rows)


class _FakePoolAcquireCtx:
    def __init__(self, conn: _FakePoolConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakePoolConnection:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakePgPool:
    """asyncpg-shaped pool fake. Only ``acquire()`` + ``fetch`` are used
    by the source_credibility handler. We pre-load it with a few rows
    keyed by host so the lookup is deterministic.
    """

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self._rows = rows

    def acquire(self) -> _FakePoolAcquireCtx:
        return _FakePoolAcquireCtx(_FakePoolConnection(self._rows))


# ---------------------------------------------------------------------------
# NLP service mock — single transport that answers /health, /extract,
# /classify. Each fixture text has a deterministic response.
# ---------------------------------------------------------------------------


def _make_nlp_mock_handler(
    *,
    extract_by_text: dict[str, list[dict[str, str]]],
    classify_top_by_text: dict[str, tuple[str, float]],
    classify_scores_by_text: dict[str, dict[str, float]],
):
    """Returns an httpx MockTransport request handler.

    /extract returns triples keyed by request text.
    /classify returns the top category + score keyed by request text;
    falls back to ('other', 0.1) for unknown text.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(
                200,
                json={"status": "ok", "gpu": True, "models_loaded": True},
            )
        body = request.content.decode("utf-8") if request.content else ""
        import json as _json

        try:
            data = _json.loads(body) if body else {}
        except Exception:
            data = {}
        text = data.get("text", "")
        if path == "/extract":
            triples = extract_by_text.get(text, [])
            return httpx.Response(200, json={"triples": triples, "ms": 1.0})
        if path == "/classify":
            top, score = classify_top_by_text.get(text, ("other", 0.1))
            scores = classify_scores_by_text.get(text, {top: score})
            return httpx.Response(
                200, json={"category": top, "confidence": score, "scores": scores},
            )
        return httpx.Response(404, json={"error": f"unexpected path {path}"})

    return handler


def _build_nlp_client(handler_fn) -> NlpServiceClient:
    transport = httpx.MockTransport(handler_fn)
    inner = httpx.AsyncClient(
        base_url="https://models.test.invalid",
        transport=transport,
        auth=httpx.BasicAuth("u", "p"),
        timeout=5.0,
    )
    return NlpServiceClient(
        endpoint="https://models.test.invalid",
        api_user="u",
        api_pass="p",
        client=inner,
    )


# ---------------------------------------------------------------------------
# Geocode stub backend — deterministic per-query result.
# ---------------------------------------------------------------------------


class _FakeGeocodeBackend:
    """Deterministic geocode backend keyed by query substring.

    Matches the :class:`GeocodeBackend` protocol surface used by
    :class:`GeocodeHandler`. No HTTP traffic.
    """

    name = "nominatim"

    def __init__(self, results_by_query: dict[str, GeocodeResult]) -> None:
        self._results = {k.lower(): v for k, v in results_by_query.items()}

    async def geocode(self, query: str) -> GeocodeResult | None:
        q = (query or "").lower()
        # Exact match first, then any substring fallback (so a candidate
        # query "Brazil" matches the registered "brazil" exactly and a
        # longer candidate like "Brazil energy" still resolves).
        if q in self._results:
            return self._results[q]
        for key, value in self._results.items():
            if key in q or q in key:
                return value
        return None

    async def reachable(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Fixture signal corpus — 5 signals exercising the pipeline.
#   * sig1, sig2 — distinct Brazilian energy-news items.
#   * sig3 — duplicate of sig1 (URL + content_hash) → must be dropped by
#     dedupe.
#   * sig4 — pt-BR signal with a known mention of "Brasilia" → geocode
#     should resolve to BR.
#   * sig5 — empty-text signal that exercises the graceful-degradation
#     paths (entities=[], classification=other-confidence-0.0).
# ---------------------------------------------------------------------------


def _make_signals() -> list[Signal]:
    # Source-first pivot (P-06): the Signal is target-agnostic — ``target_id``
    # left the schema entirely (it lives only on derived analyst outputs).
    sig1 = Signal(
        signal_id=uuid4(),
        source_id="agbrasil_econ_rss",
        payload={
            "title": "Petrobras announces new offshore platform in Brazil",
            "body": "Petrobras Inc., the Brazilian oil major, opened a new"
                    " offshore drilling platform near Rio de Janeiro this"
                    " week. President Silva attended the inauguration.",
            "source_url": "https://agenciabrasil.ebc.com.br/economia/post1",
        },
        content_hash="h-petrobras-001",
        canonical_url="https://agenciabrasil.ebc.com.br/economia/post1",
        language_hint=None,
    )
    sig2 = Signal(
        signal_id=uuid4(),
        source_id="agbrasil_econ_rss",
        payload={
            "title": "Vale Corp. signs energy supply deal in Brazil",
            "body": "Vale Corp., the Brazilian mining giant, agreed a long"
                    " term power purchase agreement with the Ministry of"
                    " Mines and Energy. The deal will boost capacity in"
                    " northern Brazil.",
            "source_url": "https://agenciabrasil.ebc.com.br/economia/post2",
        },
        content_hash="h-vale-002",
        canonical_url="https://agenciabrasil.ebc.com.br/economia/post2",
        language_hint=None,
    )
    # sig3 is a content-hash duplicate of sig1 (same URL + content_hash);
    # dedupe_tier_1 must drop it.
    sig3 = Signal(
        signal_id=uuid4(),
        source_id="agbrasil_econ_rss",
        payload={
            "title": "Petrobras announces new offshore platform in Brazil",
            "body": "Petrobras Inc., the Brazilian oil major, opened a new"
                    " offshore drilling platform near Rio de Janeiro this"
                    " week. President Silva attended the inauguration.",
            "source_url": "https://agenciabrasil.ebc.com.br/economia/post1",
        },
        content_hash="h-petrobras-001",
        canonical_url="https://agenciabrasil.ebc.com.br/economia/post1",
        language_hint=None,
    )
    sig4 = Signal(
        signal_id=uuid4(),
        source_id="agbrasil_econ_rss",
        payload={
            "title": "Aneel publica nova tarifa de energia em Brasilia",
            "body": "A Aneel, agencia reguladora, publicou hoje em Brasilia"
                    " uma nova tarifa para o setor de energia eletrica no"
                    " Brasil. A medida entra em vigor em junho.",
            "source_url": "https://agenciabrasil.ebc.com.br/economia/post4",
        },
        content_hash="h-aneel-004",
        canonical_url="https://agenciabrasil.ebc.com.br/economia/post4",
        language_hint=None,
    )
    sig5 = Signal(
        signal_id=uuid4(),
        source_id="agbrasil_econ_rss",
        payload={
            "title": "",
            "body": "",
            "source_url": "https://agenciabrasil.ebc.com.br/economia/post5",
        },
        content_hash="h-empty-005",
        canonical_url="https://agenciabrasil.ebc.com.br/economia/post5",
        language_hint="pt",
    )
    return [sig1, sig2, sig3, sig4, sig5]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def _ctx_factory(filter_id: str) -> FilterContext:
    return FilterContext(
        target_id="india_energy_infra",
        target_version="a28b4db7e6fb31c7",
        filter_id=filter_id,
        logger=logging.getLogger(f"test.brazil_pipeline.{filter_id}"),
        scope_geo=["BR"],
        scope_languages=["pt-BR", "en"],
    )


@pytest.mark.asyncio
async def test_brazil_full_pipeline_e2e(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the 7-stage Brazil pipeline and stream 5 signals through it."""

    # ----- substrate fakes ----------------------------------------------
    redis = FakeRedis()
    # source_credibility row: agenciabrasil.ebc.com.br has a moderate score.
    pg_pool = FakePgPool(
        rows={
            "agenciabrasil.ebc.com.br": {
                "source_host": "agenciabrasil.ebc.com.br",
                "score": 0.62,
                "score_rationale": "state news agency; moderate trust",
            },
        },
    )

    # NLP mock — keyed by the exact text the NER/classify handlers will
    # produce after concatenating title+body. The NER handler joins with
    # "\n" between title and body.
    sig1_text = (
        "Petrobras announces new offshore platform in Brazil\n"
        "Petrobras Inc., the Brazilian oil major, opened a new offshore"
        " drilling platform near Rio de Janeiro this week. President Silva"
        " attended the inauguration."
    )
    sig2_text = (
        "Vale Corp. signs energy supply deal in Brazil\n"
        "Vale Corp., the Brazilian mining giant, agreed a long term power"
        " purchase agreement with the Ministry of Mines and Energy. The"
        " deal will boost capacity in northern Brazil."
    )
    sig4_text = (
        "Aneel publica nova tarifa de energia em Brasilia\n"
        "A Aneel, agencia reguladora, publicou hoje em Brasilia uma nova"
        " tarifa para o setor de energia eletrica no Brasil. A medida"
        " entra em vigor em junho."
    )

    # classify's _signal_text uses title + first non-empty body field.
    sig1_classify_text = (
        "Petrobras announces new offshore platform in Brazil\n"
        "Petrobras Inc., the Brazilian oil major, opened a new offshore"
        " drilling platform near Rio de Janeiro this week. President Silva"
        " attended the inauguration."
    )
    sig2_classify_text = (
        "Vale Corp. signs energy supply deal in Brazil\n"
        "Vale Corp., the Brazilian mining giant, agreed a long term power"
        " purchase agreement with the Ministry of Mines and Energy. The"
        " deal will boost capacity in northern Brazil."
    )
    sig4_classify_text = (
        "Aneel publica nova tarifa de energia em Brasilia\n"
        "A Aneel, agencia reguladora, publicou hoje em Brasilia uma nova"
        " tarifa para o setor de energia eletrica no Brasil. A medida"
        " entra em vigor em junho."
    )

    extract_by_text = {
        sig1_text: [
            {"subject": "Petrobras Inc.", "predicate": "headquarters location",
             "object": "Brazil"},
            {"subject": "President Silva", "predicate": "occupation",
             "object": "President"},
        ],
        sig2_text: [
            {"subject": "Vale Corp.", "predicate": "headquarters location",
             "object": "Brazil"},
            {"subject": "Ministry of Mines and Energy", "predicate": "country",
             "object": "Brazil"},
        ],
        sig4_text: [
            {"subject": "Aneel", "predicate": "country",
             "object": "Brasil"},
        ],
    }
    classify_top_by_text = {
        sig1_classify_text: ("energy", 0.78),
        sig2_classify_text: ("energy", 0.71),
        sig4_classify_text: ("energy", 0.65),
    }
    classify_scores_by_text = {
        sig1_classify_text: {"energy": 0.78, "politics": 0.12, "other": 0.1},
        sig2_classify_text: {"energy": 0.71, "business": 0.21, "other": 0.08},
        sig4_classify_text: {"energy": 0.65, "policy": 0.25, "other": 0.10},
    }

    nlp_handler_fn = _make_nlp_mock_handler(
        extract_by_text=extract_by_text,
        classify_top_by_text=classify_top_by_text,
        classify_scores_by_text=classify_scores_by_text,
    )

    nlp_client = _build_nlp_client(nlp_handler_fn)

    # Deterministic geocode results for the BR-related candidates the
    # GeocodeHandler will inspect (TLD fallback also fires for .br domains).
    br_result = GeocodeResult(
        country="Brazil",
        country_iso2="BR",
        country_iso3="BRA",
        region="Distrito Federal",
        municipality="Brasilia",
        address=None,
        lat=-15.79,
        lon=-47.88,
        precision="municipality",
        source="nominatim",
    )
    fake_geocode_backend = _FakeGeocodeBackend(
        results_by_query={
            "brazil": br_result,
            "brasil": br_result,
            "brasilia": br_result,
            "rio de janeiro": br_result,
        },
    )

    # ----- build the 7 stages via the production entry point -----------
    # B-3: the geocode builder refuses public-Nominatim construction
    # without an operator contact email (OSM usage policy). The built
    # backend is swapped for the deterministic stub below, so no real
    # request ever carries this address.
    monkeypatch.setenv("LEGBA_GEOCODER_CONTACT_EMAIL", "ops@example.com")
    stages_spec: list[tuple[str, dict[str, Any]]] = [
        ("language_detect", {}),
        ("ner_multilingual", {"default_language": "en",
                               "languages": ["en", "pt"]}),
        ("classify", {
            "taxonomy_schema": "iglu:legba/event_type/jsonschema/1-0-0",
            "use_server_defaults": True,
        }),
        ("source_credibility", {}),
        ("geocode", {"backend": "nominatim"}),
        ("dedupe_tier_1", {}),
        ("dedupe_tier_2", {}),
    ]
    built: list[tuple[str, Any]] = []
    activation_errors: dict[str, str] = {}
    for kind, cfg in stages_spec:
        try:
            h = build_filter_handler(
                kind=kind,
                config=cfg,
                redis_client=redis,
                pg_pool=pg_pool,
                nlp_client_factory=lambda nlp=nlp_client: nlp,
                qdrant_client=None,
                embedding_service=None,
                secrets_resolve=None,
            )
        except Exception as exc:                                     # noqa: BLE001
            activation_errors[kind] = repr(exc)
            continue
        # Swap the geocode handler's nominatim backend for our deterministic
        # stub so the test never reaches the public OSM endpoint.
        if kind == "geocode":
            h._backend = fake_geocode_backend  # noqa: SLF001
        # Run the optional configure/activate lifecycle on each handler
        # using the per-kind keyword shape each handler exposes. Handlers
        # that don't expose the hook are skipped. This matches what the
        # runtime does in production at actor activation.
        try:
            on_configure = getattr(h, "on_configure", None)
            if on_configure is not None:
                if kind == "ner_multilingual":
                    await on_configure(_ctx_factory(kind), nlp_client=nlp_client)
                elif kind == "classify":
                    await on_configure(nlp_client=nlp_client)
                else:
                    # language_detect / dedupe / geocode / source_credibility:
                    # configure takes a single FilterContext positional arg.
                    await on_configure(_ctx_factory(kind))
            on_activate = getattr(h, "on_activate", None)
            if on_activate is not None:
                await on_activate(_ctx_factory(kind))
        except Exception as exc:                                     # noqa: BLE001
            activation_errors[kind] = f"activate: {exc!r}"
        built.append((kind, h))

    assert not activation_errors, (
        f"some filter handlers failed to construct/activate: "
        f"{activation_errors}"
    )

    runner = PipelineRunner(stages=built, ctx_factory=_ctx_factory)

    # ----- stream the 5 fixture signals through ------------------------
    signals = _make_signals()

    async def _gen() -> AsyncIterator[Signal]:
        for s in signals:
            yield s

    survivors: list[Signal] = []
    async for out in runner.run(_gen()):
        survivors.append(out)

    # ----- assertions --------------------------------------------------

    # Per the dedupe handler contract (legba.data.filters.dedupe §1) the
    # handler MARKS duplicates rather than dropping them — it sets
    # ``payload['duplicate_of']`` + ``payload['dedupe_tier']`` on a hit
    # and lets downstream consumers decide drop policy. The substrate-
    # write stage filters on these flags. So all 5 signals reach
    # ``survivors``; we assert the mark instead.
    survivor_ids = {str(s.signal_id) for s in survivors}
    assert str(signals[0].signal_id) in survivor_ids, (
        "sig1 (first occurrence) should survive"
    )
    assert str(signals[1].signal_id) in survivor_ids, "sig2 should survive"
    assert str(signals[2].signal_id) in survivor_ids, "sig3 should survive (marked, not dropped)"
    assert str(signals[3].signal_id) in survivor_ids, "sig4 should survive"

    by_id = {str(s.signal_id): s for s in survivors}

    # sig3 is the URL-canonical duplicate of sig1 — the dedupe handler
    # should mark it with the matched external id (the canonical URL
    # hash for sig1) + tier number.
    s3 = by_id[str(signals[2].signal_id)]
    assert s3.payload.get("duplicate_of"), (
        f"dedupe: sig3 should carry payload['duplicate_of']; got payload="
        f"{ {k: v for k, v in s3.payload.items() if k in ('duplicate_of', 'dedupe_tier')} }"
    )
    assert s3.payload.get("dedupe_tier") in (1, 2), (
        f"dedupe: sig3 should be flagged on tier 1 or 2; got "
        f"{s3.payload.get('dedupe_tier')!r}"
    )

    # sig1 (first occurrence) must NOT carry a duplicate_of mark.
    s1_dup = by_id[str(signals[0].signal_id)].payload.get("duplicate_of")
    assert not s1_dup, (
        f"dedupe: sig1 (first occurrence) should not be marked duplicate, "
        f"got duplicate_of={s1_dup!r}"
    )

    # language_detect must populate payload['language'] on every survivor
    # whose text was non-empty.
    s1 = by_id[str(signals[0].signal_id)]
    assert s1.payload.get("language"), (
        f"language_detect: sig1 missing language: {s1.payload}"
    )
    assert "language_confidence" in s1.payload, (
        "language_detect: sig1 missing language_confidence"
    )

    s4 = by_id[str(signals[3].signal_id)]
    assert s4.payload.get("language"), (
        f"language_detect: sig4 (Portuguese) missing language: {s4.payload}"
    )

    # ner_multilingual: entities list must be populated for sig1 + sig2
    # + sig4 (their /extract mocks return triples).
    assert isinstance(s1.payload.get("entities"), list), (
        "ner_multilingual: sig1 missing entities list"
    )
    assert len(s1.payload["entities"]) > 0, (
        f"ner_multilingual: sig1 entities empty; payload={s1.payload}"
    )
    # Check at least one entity has the expected shape.
    e0 = s1.payload["entities"][0]
    for fld in ("class", "text", "lang", "confidence"):
        assert fld in e0, f"entity shape missing {fld}: {e0}"

    # classify: classification dict must be present + carry our schema +
    # event_type for the 3 non-empty signals.
    cls1 = s1.payload.get("classification")
    assert isinstance(cls1, dict), (
        f"classify: sig1 missing classification dict; payload keys="
        f"{list(s1.payload.keys())}"
    )
    assert cls1.get("schema") == "iglu:legba/event_type/jsonschema/1-0-0", (
        f"classify: sig1 schema mismatch: {cls1.get('schema')!r}"
    )
    assert cls1.get("event_type") == "energy", (
        f"classify: sig1 expected event_type=energy, got "
        f"{cls1.get('event_type')!r}"
    )
    assert cls1.get("backend_used") in ("zero_shot", "rule"), (
        f"classify: sig1 unexpected backend_used={cls1.get('backend_used')!r}"
    )

    # source_credibility: each surviving signal should have the score
    # field populated (host = agenciabrasil.ebc.com.br).
    assert s1.source_credibility == pytest.approx(0.62), (
        f"source_credibility: sig1 expected 0.62 got "
        f"{s1.source_credibility!r}"
    )
    assert s1.source_credibility_rationale is not None, (
        "source_credibility: rationale not set on sig1"
    )
    # min_score defaults to 0.3, and 0.62 > 0.3 → not below threshold.
    assert s1.below_credibility_threshold is False

    # geocode: payload['geo'] must be populated. The handler infers from
    # title first; sig1 title mentions Brazil so the stub backend resolves.
    geo1 = s1.payload.get("geo")
    assert isinstance(geo1, dict), (
        f"geocode: sig1 missing geo dict; payload keys="
        f"{list(s1.payload.keys())}"
    )
    assert geo1.get("country_iso2") == "BR", (
        f"geocode: sig1 expected country_iso2=BR got {geo1!r}"
    )
    assert geo1.get("lat") is not None and geo1.get("lon") is not None, (
        f"geocode: sig1 missing lat/lon: {geo1!r}"
    )

    # sig4 (pt-BR text about Brasilia) — geocode should also resolve.
    geo4 = s4.payload.get("geo")
    assert isinstance(geo4, dict), (
        f"geocode: sig4 missing geo dict; payload keys="
        f"{list(s4.payload.keys())}"
    )
    assert geo4.get("country_iso2") == "BR"

    # sig5 (empty text) — pipeline must degrade gracefully: it should
    # still survive (no drop), but with NO entities + classification
    # marked "other" + no geo.
    s5_id = str(signals[4].signal_id)
    if s5_id in by_id:
        s5 = by_id[s5_id]
        # entities default to [] for empty-text inputs.
        assert s5.payload.get("entities") == [], (
            f"ner_multilingual: sig5 (empty) should yield []; got "
            f"{s5.payload.get('entities')!r}"
        )
        cls5 = s5.payload.get("classification")
        assert isinstance(cls5, dict)
        assert cls5.get("event_type") == "other", (
            f"classify: sig5 (empty) should be 'other'; got "
            f"{cls5.get('event_type')!r}"
        )

    # ----- summary stats — surface to logs for quick eyeballing ---------
    logger = logging.getLogger("test.brazil_pipeline")
    logger.info(
        "brazil_pipeline: in=%d out=%d dropped=%d activation_errors=%d",
        len(signals), len(survivors), len(signals) - len(survivors),
        len(activation_errors),
    )
