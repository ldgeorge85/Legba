# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the full-set :func:`build_filter_handler` (post-spike Phase 5a).

Coverage:

  * Each filter kind constructs successfully from a representative config
    with the right deps wired.
  * Missing required deps raise a clear ``ValueError`` naming the kind and
    the missing dep — no silent stubs.
  * Unknown kinds raise ``ValueError`` listing the known kinds.
  * L-248 ``tiers`` opt-out is preserved (operator override wins).

These tests don't talk to Postgres / Redis / NLP service over the wire —
construction is the contract under test. Real-traffic tests for each
handler live in ``tests/data_pkg/test_filter_*``.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.data.filters import (
    Dedupe4TierHandler,
    LanguageDetectHandler,
)
from legba.data.filters.classify import ClassifyHandler, Label
from legba.data.filters.fact_extractor import FactExtractorHandler
from legba.data.filters.geocode import GeocodeHandler
from legba.data.filters.ner import NERMultilingualHandler
from legba.data.filters.source_credibility import SourceCredibilityHandler
from legba.data.stack.nlp_service.client import NlpServiceClient
from legba.runtime.pipeline import (
    _KNOWN_KINDS,
    _DeferredGoogleBackend,
    build_filter_handler,
)


# ---------------------------------------------------------------------------
# Lightweight test doubles — none of these talk to a real service.
# We pass them only to satisfy the build_filter_handler dep guards; the
# handlers themselves don't invoke them during construction.
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Async surface stub satisfying the dedupe + geocode redis ports.

    No transform-time assertion — pure construction-time placeholder.
    """

    async def get(self, name: str) -> Any: return None
    async def set(self, name: str, value: Any, ex: int | None = None) -> Any: return None
    async def setex(self, key: str, ttl: int, value: bytes) -> None: return None
    async def expire(self, name: str, seconds: int) -> bool: return True
    async def zadd(self, name: str, mapping: dict[str, float]) -> int: return 0
    async def zrangebyscore(self, *a: Any, **kw: Any) -> list: return []
    async def zremrangebyscore(self, *a: Any, **kw: Any) -> int: return 0


class _FakePgPool:
    """Placeholder asyncpg-shaped pool. Construction only; never queried."""


class _FakeQdrant:
    async def get_collections(self) -> Any: return None
    async def create_collection(self, **kw: Any) -> Any: return None
    async def query_points(self, **kw: Any) -> Any: return None
    async def upsert(self, **kw: Any) -> Any: return None


class _FakeEmbedder:
    async def embed(self, text: str) -> list[float]:
        return [0.0] * 8


def _nlp_factory() -> NlpServiceClient:
    """Return a deferred NlpServiceClient stub. Network never touched
    (the httpx client is built lazily on first request)."""
    return NlpServiceClient(
        endpoint="http://stub.test",
        api_user="u",
        api_pass="p",
    )


async def _secret_resolve(secret_id: str) -> bytes:
    """Async secret resolver stub. Returns deterministic bytes."""
    return b"fake-api-key-for-" + secret_id.encode()


# ---------------------------------------------------------------------------
# 1. Construction smoke tests — every kind from a representative config.
# ---------------------------------------------------------------------------


class TestEachKindConstructs:

    def test_language_detect(self) -> None:
        h = build_filter_handler(
            kind="language_detect",
            config={"min_confidence": 0.6},
        )
        assert isinstance(h, LanguageDetectHandler)

    def test_dedupe_tier_1(self) -> None:
        h = build_filter_handler(
            kind="dedupe_tier_1",
            config={},
            redis_client=_FakeRedis(),
        )
        assert isinstance(h, Dedupe4TierHandler)
        assert h.is_tier_active(1) is True
        assert h.is_tier_active(2) is False
        assert h.is_tier_active(3) is False
        assert h.is_tier_active(4) is False

    def test_dedupe_tier_2(self) -> None:
        h = build_filter_handler(
            kind="dedupe_tier_2",
            config={},
            redis_client=_FakeRedis(),
        )
        assert isinstance(h, Dedupe4TierHandler)
        assert h.is_tier_active(2) is True
        assert h.is_tier_active(1) is False

    def test_dedupe_tier_3(self) -> None:
        h = build_filter_handler(
            kind="dedupe_tier_3",
            config={},
            redis_client=_FakeRedis(),
            qdrant_client=_FakeQdrant(),
            embedding_service=_FakeEmbedder(),
        )
        assert isinstance(h, Dedupe4TierHandler)
        assert h.is_tier_active(3) is True
        # The other tiers should be disabled.
        assert h.is_tier_active(1) is False
        assert h.is_tier_active(2) is False
        assert h.is_tier_active(4) is False

    def test_dedupe_tier_4(self) -> None:
        h = build_filter_handler(
            kind="dedupe_tier_4",
            config={},
            redis_client=_FakeRedis(),
        )
        assert isinstance(h, Dedupe4TierHandler)
        assert h.is_tier_active(4) is True
        assert h.is_tier_active(1) is False

    def test_source_credibility(self) -> None:
        h = build_filter_handler(
            kind="source_credibility",
            config={"min_score": 0.4},
            pg_pool=_FakePgPool(),
        )
        assert isinstance(h, SourceCredibilityHandler)

    def test_geocode_nominatim_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default backend is nominatim — needs no secret resolution.

        B-3: against the PUBLIC endpoint a real operator contact email
        (LEGBA_GEOCODER_CONTACT_EMAIL) is required per OSM usage policy.
        """
        monkeypatch.setenv("LEGBA_GEOCODER_CONTACT_EMAIL", "ops@example.com")
        h = build_filter_handler(
            kind="geocode",
            config={},
            redis_client=_FakeRedis(),
        )
        assert isinstance(h, GeocodeHandler)

    def test_geocode_nominatim_public_without_contact_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B-3: public Nominatim with no contact email fails loud at build."""
        monkeypatch.delenv("LEGBA_GEOCODER_CONTACT_EMAIL", raising=False)
        with pytest.raises(RuntimeError, match="LEGBA_GEOCODER_CONTACT_EMAIL"):
            build_filter_handler(
                kind="geocode",
                config={},
                redis_client=_FakeRedis(),
            )

    def test_geocode_nominatim_public_placeholder_contact_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B-3: the .env.example `.invalid` placeholder counts as unset."""
        monkeypatch.setenv("LEGBA_GEOCODER_CONTACT_EMAIL", "ops@example.invalid")
        with pytest.raises(RuntimeError, match="LEGBA_GEOCODER_CONTACT_EMAIL"):
            build_filter_handler(
                kind="geocode",
                config={},
                redis_client=_FakeRedis(),
            )

    def test_geocode_nominatim_self_hosted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Self-hosted Nominatim needs no operator contact email."""
        monkeypatch.delenv("LEGBA_GEOCODER_CONTACT_EMAIL", raising=False)
        h = build_filter_handler(
            kind="geocode",
            config={"nominatim_url": "https://nominatim.internal:7070"},
            redis_client=_FakeRedis(),
        )
        assert isinstance(h, GeocodeHandler)

    def test_geocode_google_with_secrets(self) -> None:
        """Google backend requires secrets_resolve + a secret ref."""
        h = build_filter_handler(
            kind="geocode",
            config={
                "backend": "google",
                "google_api_key_secret_ref": "vault://geocode/google_key",
            },
            redis_client=_FakeRedis(),
            secrets_resolve=_secret_resolve,
        )
        assert isinstance(h, GeocodeHandler)
        # The Google backend is deferred — verify the wrapper is in place.
        assert isinstance(h._backend, _DeferredGoogleBackend)
        assert h._backend.name == "google"

    def test_ner_multilingual(self) -> None:
        h = build_filter_handler(
            kind="ner_multilingual",
            config={"languages": ["en", "pt"], "default_language": "en"},
            nlp_client_factory=_nlp_factory,
        )
        assert isinstance(h, NERMultilingualHandler)

    def test_classify_with_labels(self) -> None:
        """Spot-check matches the brief's verification snippet."""
        h = build_filter_handler(
            kind="classify",
            config={
                "taxonomy_schema": "iglu:com.legba/event_type/jsonschema/1-0-0",
                "labels": [{"name": "earthquake"}, {"name": "flood"}],
            },
            nlp_client_factory=lambda: NlpServiceClient(
                endpoint="http://stub.test",
                api_user="u",
                api_pass="p",
            ),
        )
        assert isinstance(h, ClassifyHandler)

    def test_classify_use_server_defaults(self) -> None:
        """Server-defaults branch skips the labels requirement."""
        h = build_filter_handler(
            kind="classify",
            config={
                "taxonomy_schema": "iglu:com.legba/event_type/jsonschema/1-0-0",
                "use_server_defaults": True,
            },
            nlp_client_factory=_nlp_factory,
        )
        assert isinstance(h, ClassifyHandler)

    def test_fact_extractor_relation_default(self) -> None:
        """Default relation backend builds with just a pg_pool (anchor §5)."""
        h = build_filter_handler(
            kind="fact_extractor",
            config={"backend": "relation"},
            pg_pool=_FakePgPool(),
        )
        assert isinstance(h, FactExtractorHandler)
        assert h.config.backend == "relation"

    def test_fact_extractor_llm_threads_factory(self) -> None:
        """The llm backend builds when the llm_handler_factory is threaded
        through (the new optional param). No live model — construction only."""
        async def _llm_factory(component_id: str) -> Any:  # pragma: no cover
            return object()
        h = build_filter_handler(
            kind="fact_extractor",
            config={"backend": "llm", "llm_component_id": "c.8b"},
            pg_pool=_FakePgPool(),
            llm_handler_factory=_llm_factory,
        )
        assert isinstance(h, FactExtractorHandler)

    def test_fact_extractor_slm_validate_builds_validator(self) -> None:
        """slm_validate_relations=True wires a relationship validator (W3).

        The validator is constructed from the same llm_handler_factory the 8B
        path uses (provider plane, never litellm); construction only — no
        live model call."""
        from legba.data.filters.slm_relationship_validate import (
            SLMRelationshipValidateHandler,
        )

        async def _llm_factory(component_id: str) -> Any:  # pragma: no cover
            return object()

        h = build_filter_handler(
            kind="fact_extractor",
            config={
                "backend": "relation",
                "slm_validate_relations": True,
                "slm_validate_component_id": "c.slm",
                "slm_validate_max_triples": 7,
            },
            pg_pool=_FakePgPool(),
            llm_handler_factory=_llm_factory,
        )
        assert isinstance(h, FactExtractorHandler)
        assert h.config.slm_validate_relations is True
        validator = h._relationship_validator
        assert isinstance(validator, SLMRelationshipValidateHandler)
        # The per-signal cap propagates from the fact_extractor knob.
        assert validator.config.max_triples_per_signal == 7

    def test_fact_extractor_slm_validate_off_builds_no_validator(self) -> None:
        """Default (flag off) wires NO validator — the path is unchanged."""
        h = build_filter_handler(
            kind="fact_extractor",
            config={"backend": "relation"},
            pg_pool=_FakePgPool(),
        )
        assert isinstance(h, FactExtractorHandler)
        assert h.config.slm_validate_relations is False
        assert h._relationship_validator is None


# ---------------------------------------------------------------------------
# 2. Missing-dep error paths — preserve the spike's redis contract +
#    extend for the other kinds.
# ---------------------------------------------------------------------------


class TestMissingDepRaisesValueError:

    @pytest.mark.parametrize("kind", ["dedupe_tier_1", "dedupe_tier_2", "dedupe_tier_4"])
    def test_dedupe_without_redis(self, kind: str) -> None:
        with pytest.raises(ValueError, match=r"requires a redis client"):
            build_filter_handler(kind=kind, config={})

    def test_dedupe_tier_3_without_redis(self) -> None:
        with pytest.raises(ValueError, match=r"requires a redis client"):
            build_filter_handler(
                kind="dedupe_tier_3",
                config={},
                qdrant_client=_FakeQdrant(),
                embedding_service=_FakeEmbedder(),
            )

    def test_dedupe_tier_3_without_qdrant(self) -> None:
        with pytest.raises(ValueError, match=r"requires a qdrant_client"):
            build_filter_handler(
                kind="dedupe_tier_3",
                config={},
                redis_client=_FakeRedis(),
                embedding_service=_FakeEmbedder(),
            )

    def test_dedupe_tier_3_without_embedding_service(self) -> None:
        with pytest.raises(ValueError, match=r"requires an embedding_service"):
            build_filter_handler(
                kind="dedupe_tier_3",
                config={},
                redis_client=_FakeRedis(),
                qdrant_client=_FakeQdrant(),
            )

    def test_source_credibility_without_pg_pool(self) -> None:
        with pytest.raises(ValueError, match=r"requires a pg_pool"):
            build_filter_handler(
                kind="source_credibility",
                config={},
            )

    def test_geocode_google_without_secrets_resolve(self) -> None:
        with pytest.raises(ValueError, match=r"requires\s+secrets_resolve"):
            build_filter_handler(
                kind="geocode",
                config={
                    "backend": "google",
                    "google_api_key_secret_ref": "vault://geocode/google_key",
                },
                redis_client=_FakeRedis(),
            )

    def test_geocode_google_without_secret_ref(self) -> None:
        with pytest.raises(ValueError, match=r"google_api_key_secret_ref"):
            build_filter_handler(
                kind="geocode",
                config={"backend": "google"},
                redis_client=_FakeRedis(),
                secrets_resolve=_secret_resolve,
            )

    def test_ner_without_nlp_client_factory(self) -> None:
        with pytest.raises(ValueError, match=r"requires an nlp_client_factory"):
            build_filter_handler(
                kind="ner_multilingual",
                config={},
            )

    def test_classify_without_nlp_client_factory(self) -> None:
        with pytest.raises(ValueError, match=r"requires an nlp_client_factory"):
            build_filter_handler(
                kind="classify",
                config={
                    "taxonomy_schema": "iglu:com.legba/event_type/jsonschema/1-0-0",
                    "labels": [{"name": "earthquake"}],
                },
            )

    def test_fact_extractor_without_pg_pool(self) -> None:
        with pytest.raises(ValueError, match=r"requires a pg_pool"):
            build_filter_handler(
                kind="fact_extractor",
                config={"backend": "relation"},
            )

    def test_fact_extractor_slm_validate_without_factory_raises(self) -> None:
        # no-stub proof: slm_validate_relations needs the provider-plane
        # factory; flag on without it raises at the builder layer.
        with pytest.raises(ValueError, match=r"llm_handler_factory"):
            build_filter_handler(
                kind="fact_extractor",
                config={"backend": "relation", "slm_validate_relations": True},
                pg_pool=_FakePgPool(),
            )

    def test_fact_extractor_llm_without_factory_raises(self) -> None:
        # no-stub proof at the builder layer: llm backend without a factory
        # raises (the handler's FactExtractorUnconfigured is a RuntimeError).
        with pytest.raises(Exception, match=r"llm_handler_factory"):
            build_filter_handler(
                kind="fact_extractor",
                config={"backend": "llm", "llm_component_id": "c.8b"},
                pg_pool=_FakePgPool(),
            )


# ---------------------------------------------------------------------------
# 3. Unknown-kind contract.
# ---------------------------------------------------------------------------


class TestUnknownKindRaises:

    def test_unknown_kind_lists_known_kinds(self) -> None:
        with pytest.raises(ValueError) as ei:
            build_filter_handler(kind="nonsense_kind", config={})
        msg = str(ei.value)
        assert "nonsense_kind" in msg
        # Every known kind should be named in the error message so the
        # operator can copy-paste a fix.
        for known in _KNOWN_KINDS:
            assert known in msg

    def test_empty_kind_raises(self) -> None:
        with pytest.raises(ValueError, match=r"not supported"):
            build_filter_handler(kind="", config={})


# ---------------------------------------------------------------------------
# 4. L-248: operator-override of ``tiers`` wins over per-stage narrowing.
# ---------------------------------------------------------------------------


class TestL248TiersOptOut:

    def test_explicit_tiers_list_preserved(self) -> None:
        """An operator passing ``tiers=[1, 2]`` on a tier_1 stage keeps
        both tiers active — the explicit selector wins."""
        h = build_filter_handler(
            kind="dedupe_tier_1",
            config={"tiers": [1, 2]},
            redis_client=_FakeRedis(),
        )
        assert isinstance(h, Dedupe4TierHandler)
        assert h.is_tier_active(1) is True
        # tier 2 is in the operator's selector AND its per-tier ``enabled``
        # default is True (we don't auto-disable when tiers includes it).
        assert h.is_tier_active(2) is True

    def test_default_narrowing_for_tier_2_stage(self) -> None:
        """No explicit ``tiers`` → narrow to the named tier only."""
        h = build_filter_handler(
            kind="dedupe_tier_2",
            config={},
            redis_client=_FakeRedis(),
        )
        assert isinstance(h, Dedupe4TierHandler)
        assert h.is_tier_active(2) is True
        assert h.is_tier_active(1) is False
        assert h.is_tier_active(3) is False
        assert h.is_tier_active(4) is False


# ---------------------------------------------------------------------------
# 5. Known-kinds frozenset is complete (registry sanity).
# ---------------------------------------------------------------------------


def test_known_kinds_set_matches_brief() -> None:
    """The Phase-4 brief enumerated 9 kinds; anchor §5 PIECE 2 adds
    ``fact_extractor`` (altitude-0 extraction); the entity-resolution W1 pass
    adds ``slm_entity_resolve`` (the descriptor-gated SLM disambiguator, OFF
    unless a descriptor names it). The registry must match exactly."""
    expected = frozenset({
        "language_detect",
        "dedupe_tier_1",
        "dedupe_tier_2",
        "dedupe_tier_3",
        "dedupe_tier_4",
        "source_credibility",
        "geocode",
        "ner_multilingual",
        "classify",
        "fact_extractor",
        "slm_entity_resolve",
    })
    assert _KNOWN_KINDS == expected
