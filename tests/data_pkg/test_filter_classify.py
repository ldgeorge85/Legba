# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the L-155 ClassifyHandler (HTTP variant).

Architectural-drift correction (2026-05-22): the pre-reshape suite used a
deterministic in-process stub embedder + cosine. The post-reshape handler
calls the hosted Legba-models ``POST /classify`` endpoint. Tests inject an
``httpx.MockTransport`` so we assert the wire shape and the
classification → payload mutation without a running service.

Coverage:

  * Wire shape — path ``/classify``, Basic Auth, JSON body containing
    ``text`` and optional ``labels``.
  * 3 labels × 3 signals — each lands on the right label.
  * Threshold-below → ``"other"``.
  * Multi-label correctness.
  * Rules-then-hosted short-circuit.
  * Graceful degradation — 5xx / 401 → ``"other"`` with confidence 0.0.
  * Lifecycle — on_pause drops activation flag.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from legba.data.filters.classify import (
    CLASSIFY_KIND,
    CLASSIFY_SCHEMA_VERSION,
    Classification,
    ClassifyConfig,
    ClassifyHandler,
    DEFAULT_SENTIMENT_SEEDS,
    Label,
    OTHER_LABEL,
    Rule,
    SENTIMENT_LABELS,
    _build_label_anchor,
    _cosine,
    _signal_text,
    _taxonomy_fingerprint,
)
from legba.data.filters._contract import FilterContext, FilterHealth, StreamHandler
from legba.data.sources._contract import Signal
from legba.data.stack.nlp_service import NlpServiceClient


# ---------------------------------------------------------------------------
# Mock-transport helpers
# ---------------------------------------------------------------------------


def _build_client(
    handler: Any,
    *,
    base_url: str = "https://models.test.invalid",
    api_user: str | None = "test-user",
    api_pass: str | None = "test-pass",
) -> NlpServiceClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(
        base_url=base_url,
        transport=transport,
        auth=httpx.BasicAuth(api_user, api_pass) if api_user else None,
        timeout=5.0,
    )
    return NlpServiceClient(
        endpoint=base_url,
        api_user=api_user,
        api_pass=api_pass,
        client=inner,
    )


# Score table: text-substring → {label: confidence}. The handler returns
# the top label and confidence among the scores. Defaults to a low-score
# "other" distribution.
def _make_classify_handler(
    scores_by_substring: dict[str, dict[str, float]] | None = None,
    *,
    captured_requests: list[httpx.Request] | None = None,
    status: int = 200,
    response_body: dict[str, Any] | None = None,
) -> Any:
    scores_by_substring = scores_by_substring or {}
    captured_requests = captured_requests if captured_requests is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ok", "gpu": True, "models_loaded": True},
            )
        if request.url.path == "/classify":
            if status != 200:
                return httpx.Response(status, json=response_body or {"error": "test"})
            req_body = json.loads(request.content.decode("utf-8"))
            text = req_body.get("text", "")
            requested_labels = req_body.get("labels")
            # Pick a scores dict by matching substrings in `text`. First
            # substring match wins. If no match → uniform low scores across
            # the requested labels.
            scores: dict[str, float] | None = None
            for needle, table in scores_by_substring.items():
                if needle.lower() in text.lower():
                    scores = dict(table)
                    break
            if scores is None:
                labels = requested_labels or ["outage", "protest", "cyberattack"]
                scores = {label: 0.10 for label in labels}
            top_name, top_score = max(scores.items(), key=lambda kv: kv[1])
            return httpx.Response(
                200,
                json={
                    "category": top_name,
                    "confidence": float(top_score),
                    "scores": {k: float(v) for k, v in scores.items()},
                    "ms": 188.4,
                },
            )
        return httpx.Response(404, json={"error": "unexpected path"})

    return handler, captured_requests


def _signal(payload: dict[str, Any]) -> Signal:
    # Source-first pivot: Signal is source-owned and target-agnostic — the
    # dropped ``target_id`` lives only on derived analyst outputs now
    # (see PIVOT_BUILD_PLAN; src/legba/data/sources/_contract.py Signal).
    return Signal(
        signal_id=uuid4(),
        source_id="src-test",
        payload=payload,
        content_hash="h" * 64,
        canonical_url=None,
        language_hint=None,
    )


# ---------------------------------------------------------------------------
# Standard 3-category taxonomy
# ---------------------------------------------------------------------------


def _default_labels() -> list[Label]:
    return [
        Label(
            name="outage",
            description="Electricity outage / blackout / power loss events",
            examples=[
                "Major outage hit the grid",
                "Citywide blackout reported",
            ],
        ),
        Label(
            name="protest",
            description="Public protest / march / civil rally activity",
            examples=["Thousands joined the protest"],
        ),
        Label(
            name="cyberattack",
            description="Cyberattack / ransomware / hack incidents",
            examples=["Ransomware locked the systems"],
        ),
    ]


def _default_config(**overrides: Any) -> ClassifyConfig:
    base = dict(
        taxonomy_schema="energy_event_taxonomy",
        labels=_default_labels(),
        backend="zero_shot_hosted",
        min_confidence=0.4,
    )
    base.update(overrides)
    return ClassifyConfig(**base)


def _ctx() -> FilterContext:
    return FilterContext(target_id="tgt-test", target_version="v1", filter_id="f-test")


def _strong_3way_scores() -> dict[str, dict[str, float]]:
    """Maps text substrings to label-score tables. Strong dominance for the
    matching category so the handler's threshold logic is reliably exercised.
    """
    return {
        "blackout": {"outage": 0.98, "protest": 0.01, "cyberattack": 0.01},
        "outage": {"outage": 0.98, "protest": 0.01, "cyberattack": 0.01},
        "power loss": {"outage": 0.96, "protest": 0.02, "cyberattack": 0.02},
        "protest": {"protest": 0.97, "outage": 0.02, "cyberattack": 0.01},
        "march": {"protest": 0.96, "outage": 0.02, "cyberattack": 0.02},
        "rally": {"protest": 0.95, "outage": 0.03, "cyberattack": 0.02},
        "ransomware": {"cyberattack": 0.99, "outage": 0.005, "protest": 0.005},
        "hack": {"cyberattack": 0.97, "outage": 0.015, "protest": 0.015},
        "cyberattack": {"cyberattack": 0.99, "outage": 0.005, "protest": 0.005},
    }


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_default_backend_is_zero_shot_hosted(self) -> None:
        cfg = _default_config()
        assert cfg.backend == "zero_shot_hosted"
        assert cfg.min_confidence == 0.4
        assert cfg.multi_label is False
        assert cfg.sentiment is False

    def test_legacy_zero_shot_embedding_alias_accepted(self) -> None:
        """``zero_shot_embedding`` is a legacy alias mapped to the hosted backend."""
        cfg = ClassifyConfig(
            taxonomy_schema="x",
            labels=_default_labels(),
            backend="zero_shot_embedding",
        )
        assert cfg.backend == "zero_shot_embedding"

    def test_zero_shot_requires_labels(self) -> None:
        with pytest.raises(ValidationError):
            ClassifyConfig(
                taxonomy_schema="x",
                labels=[],
                backend="zero_shot_hosted",
            )

    def test_use_server_defaults_skips_labels_requirement(self) -> None:
        cfg = ClassifyConfig(
            taxonomy_schema="x",
            labels=[],
            backend="zero_shot_hosted",
            use_server_defaults=True,
        )
        assert cfg.use_server_defaults is True

    def test_fine_tuned_requires_model_path(self) -> None:
        with pytest.raises(ValidationError):
            ClassifyConfig(
                taxonomy_schema="x",
                labels=_default_labels(),
                backend="fine_tuned",
            )

    def test_rules_then_hosted_requires_rules(self) -> None:
        with pytest.raises(ValidationError):
            ClassifyConfig(
                taxonomy_schema="x",
                labels=_default_labels(),
                backend="rules_then_hosted",
                rules=[],
            )

    def test_duplicate_label_names_rejected(self) -> None:
        labels = _default_labels() + [_default_labels()[0]]
        with pytest.raises(ValidationError):
            ClassifyConfig(taxonomy_schema="x", labels=labels)

    def test_reserved_label_name_other_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Label(name="other", description="x")
        with pytest.raises(ValidationError):
            Label(name="OTHER", description="x")

    def test_min_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ClassifyConfig(
                taxonomy_schema="x",
                labels=_default_labels(),
                min_confidence=0.0,
            )
        with pytest.raises(ValidationError):
            ClassifyConfig(
                taxonomy_schema="x",
                labels=_default_labels(),
                min_confidence=1.5,
            )

    def test_severity_taxonomy_duplicate_rejected(self) -> None:
        sev = [Label(name="low"), Label(name="low")]
        with pytest.raises(ValidationError):
            ClassifyConfig(
                taxonomy_schema="x",
                labels=_default_labels(),
                severity_taxonomy=sev,
            )

    def test_invalid_regex_pattern_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Rule(pattern="(unbalanced", label="foo")


# ---------------------------------------------------------------------------
# Envelope / contract conformance
# ---------------------------------------------------------------------------


class TestContractConformance:
    def test_kind_and_schema_version_classvars(self) -> None:
        assert ClassifyHandler.kind == "classify"
        assert ClassifyHandler.family == "filter"
        assert ClassifyHandler.schema_version == CLASSIFY_SCHEMA_VERSION
        assert CLASSIFY_KIND == "classify"

    def test_output_contract_declares_classification(self) -> None:
        keys = set(ClassifyHandler.output_contract.keys())
        assert "payload.classification" in keys
        assert "payload.classification.event_type" in keys

    def test_satisfies_stream_handler_protocol(self) -> None:
        handler_fn, _ = _make_classify_handler({})
        client = _build_client(handler_fn)
        handler = ClassifyHandler(_default_config(), nlp_client=client)
        assert isinstance(handler, StreamHandler)


# ---------------------------------------------------------------------------
# Wire-shape assertions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWireShape:
    async def test_classify_request_path_auth_and_body(self) -> None:
        requests: list[httpx.Request] = []
        handler_fn, _ = _make_classify_handler(
            _strong_3way_scores(),
            captured_requests=requests,
        )
        client = _build_client(handler_fn, api_user="alice", api_pass="s3cret")
        handler = ClassifyHandler(_default_config(), nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        sig = _signal({"title": "evt", "body": "Major blackout reported overnight"})
        await handler.transform(sig, _ctx())

        # /health on activate + /classify on transform.
        paths = [r.url.path for r in requests]
        assert "/health" in paths
        assert "/classify" in paths
        classify_req = next(r for r in requests if r.url.path == "/classify")
        auth = classify_req.headers.get("authorization", "")
        assert auth.startswith("Basic ")
        body = json.loads(classify_req.content.decode("utf-8"))
        assert "text" in body
        assert body["labels"] == ["outage", "protest", "cyberattack"]

    async def test_use_server_defaults_omits_labels(self) -> None:
        requests: list[httpx.Request] = []
        handler_fn, _ = _make_classify_handler(
            _strong_3way_scores(),
            captured_requests=requests,
        )
        client = _build_client(handler_fn)
        cfg = ClassifyConfig(
            taxonomy_schema="server_defaults",
            labels=[],
            backend="zero_shot_hosted",
            use_server_defaults=True,
        )
        handler = ClassifyHandler(cfg, nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        await handler.transform(_signal({"body": "Major blackout overnight"}), _ctx())
        classify_req = next(r for r in requests if r.url.path == "/classify")
        body = json.loads(classify_req.content.decode("utf-8"))
        assert "labels" not in body


# ---------------------------------------------------------------------------
# Zero-shot classification — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestZeroShotClassification:
    async def test_three_categories_three_signals_each(self) -> None:
        handler_fn, _ = _make_classify_handler(_strong_3way_scores())
        client = _build_client(handler_fn)
        cfg = _default_config()
        handler = ClassifyHandler(cfg, nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()

        cases = {
            "outage": [
                "Power loss across northern districts last night",
                "Authorities confirm a blackout in the capital",
                "Major outage knocked the grid for hours",
            ],
            "protest": [
                "Thousands joined a protest downtown",
                "A peaceful march made its way to the square",
                "The rally drew a large crowd",
            ],
            "cyberattack": [
                "Ransomware crippled the hospital network",
                "Hack disrupted the rail signalling system",
                "A coordinated cyberattack on the utility",
            ],
        }

        ctx = _ctx()
        for expected, texts in cases.items():
            for body in texts:
                sig = _signal({"title": "evt", "body": body})
                out = await handler.transform(sig, ctx)
                assert out is not None, f"signal dropped unexpectedly: {body}"
                cls = out.payload["classification"]
                assert cls["event_type"] == expected, (
                    f"expected {expected!r}, got {cls['event_type']!r} for {body!r}"
                )
                assert cls["backend_used"] == "zero_shot"
                assert cls["schema"] == "energy_event_taxonomy"
                assert cls["confidence"] >= cfg.min_confidence
                assert set(cls["label_scores"].keys()) == {"outage", "protest", "cyberattack"}


# ---------------------------------------------------------------------------
# Threshold-below → "other"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSubConfidenceFallsBackToOther:
    async def test_low_confidence_returns_other(self) -> None:
        """When the top score falls below ``min_confidence`` the handler
        stamps ``"other"`` and preserves the score on the payload."""
        # Default scores (no substring match) are uniform 0.10 — well
        # below the 0.4 threshold.
        handler_fn, _ = _make_classify_handler({})
        client = _build_client(handler_fn)
        cfg = _default_config(min_confidence=0.4)
        handler = ClassifyHandler(cfg, nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        ctx = _ctx()
        sig = _signal({"body": "totally unrelated content about flowers"})
        out = await handler.transform(sig, ctx)
        assert out is not None
        cls = out.payload["classification"]
        assert cls["event_type"] == OTHER_LABEL
        assert cls["confidence"] < cfg.min_confidence
        assert cls["backend_used"] == "zero_shot"


# ---------------------------------------------------------------------------
# Multi-label
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMultiLabel:
    async def test_multi_label_above_threshold(self) -> None:
        scores = {
            "blackout": {"outage": 0.7, "cyberattack": 0.6, "protest": 0.05},
        }
        handler_fn, _ = _make_classify_handler(scores)
        client = _build_client(handler_fn)
        cfg = _default_config(multi_label=True, min_confidence=0.5)
        handler = ClassifyHandler(cfg, nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        ctx = _ctx()
        sig = _signal({"body": "Reports of a blackout following a ransomware hack"})
        out = await handler.transform(sig, ctx)
        cls = out.payload["classification"]
        assert isinstance(cls["event_type"], list)
        assert set(cls["event_type"]) == {"outage", "cyberattack"}
        assert cls["confidence"] >= cfg.min_confidence

    async def test_multi_label_with_only_one_above_threshold(self) -> None:
        scores = {
            "blackout": {"outage": 0.8, "protest": 0.05, "cyberattack": 0.05},
        }
        handler_fn, _ = _make_classify_handler(scores)
        client = _build_client(handler_fn)
        cfg = _default_config(multi_label=True, min_confidence=0.4)
        handler = ClassifyHandler(cfg, nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        sig = _signal({"body": "Just a blackout, nothing else"})
        out = await handler.transform(sig, _ctx())
        cls = out.payload["classification"]
        assert cls["event_type"] == ["outage"]

    async def test_multi_label_none_above_threshold_returns_other(self) -> None:
        handler_fn, _ = _make_classify_handler({})
        client = _build_client(handler_fn)
        cfg = _default_config(multi_label=True, min_confidence=0.95)
        handler = ClassifyHandler(cfg, nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        sig = _signal({"body": "completely unrelated content"})
        out = await handler.transform(sig, _ctx())
        cls = out.payload["classification"]
        assert cls["event_type"] == OTHER_LABEL


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSeverity:
    async def test_severity_taxonomy_via_second_classify_call(self) -> None:
        """When ``severity_taxonomy`` is set the handler issues a second
        /classify call against those labels and records the result."""
        # The scoring table maps the same text to the right answers for
        # whichever label set is in the request. We discriminate by checking
        # whether the request labels include 'low'/'high'/'medium' (severity)
        # vs the main taxonomy.
        def custom_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "models_loaded": True})
            body = json.loads(request.content.decode("utf-8"))
            text = body.get("text", "").lower()
            labels = body.get("labels", [])
            if set(labels) == {"low", "medium", "high"}:
                # Severity call.
                if "major" in text:
                    return httpx.Response(200, json={
                        "category": "high", "confidence": 0.92,
                        "scores": {"high": 0.92, "medium": 0.05, "low": 0.03},
                        "ms": 100.0,
                    })
                return httpx.Response(200, json={
                    "category": "low", "confidence": 0.6,
                    "scores": {"low": 0.6, "medium": 0.3, "high": 0.1},
                    "ms": 100.0,
                })
            # Main classification.
            return httpx.Response(200, json={
                "category": "outage", "confidence": 0.95,
                "scores": {"outage": 0.95, "protest": 0.03, "cyberattack": 0.02},
                "ms": 100.0,
            })

        client = _build_client(custom_handler)
        cfg = _default_config(
            severity_taxonomy=[
                Label(name="low"),
                Label(name="high"),
                Label(name="medium"),
            ],
            min_confidence=0.5,
        )
        handler = ClassifyHandler(cfg, nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        sig = _signal({"body": "A major blackout hit overnight"})
        out = await handler.transform(sig, _ctx())
        cls = out.payload["classification"]
        assert cls["event_type"] == "outage"
        assert cls["severity"] == "high"


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSentiment:
    async def test_sentiment_returns_one_of_three(self) -> None:
        def custom_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "models_loaded": True})
            body = json.loads(request.content.decode("utf-8"))
            labels = body.get("labels", [])
            if set(labels) == set(SENTIMENT_LABELS):
                return httpx.Response(200, json={
                    "category": "negative", "confidence": 0.8,
                    "scores": {"negative": 0.8, "positive": 0.1, "neutral": 0.1},
                    "ms": 100.0,
                })
            return httpx.Response(200, json={
                "category": "outage", "confidence": 0.95,
                "scores": {"outage": 0.95, "protest": 0.03, "cyberattack": 0.02},
                "ms": 100.0,
            })

        client = _build_client(custom_handler)
        cfg = _default_config(sentiment=True, min_confidence=0.4)
        handler = ClassifyHandler(cfg, nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        sig = _signal({"body": "blackout was a clear loss"})
        out = await handler.transform(sig, _ctx())
        cls = out.payload["classification"]
        assert cls["sentiment"] in SENTIMENT_LABELS


# ---------------------------------------------------------------------------
# Rules-then-hosted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRulesThenHosted:
    async def test_rule_hit_short_circuits_hosted_call(self) -> None:
        requests: list[httpx.Request] = []
        handler_fn, _ = _make_classify_handler(
            _strong_3way_scores(),
            captured_requests=requests,
        )
        client = _build_client(handler_fn)
        cfg = ClassifyConfig(
            taxonomy_schema="energy_event_taxonomy",
            backend="rules_then_hosted",
            labels=_default_labels(),
            rules=[Rule(pattern=r"\bICS-CERT advisory\b",
                         label="cyberattack", confidence=0.99)],
            min_confidence=0.4,
        )
        handler = ClassifyHandler(cfg, nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        sig = _signal({"body": "ICS-CERT advisory issued for unrelated vendor"})
        out = await handler.transform(sig, _ctx())
        cls = out.payload["classification"]
        assert cls["event_type"] == "cyberattack"
        assert cls["backend_used"] == "rule"
        assert cls["confidence"] == pytest.approx(0.99)
        # /classify was NOT called — only /health on activate.
        classify_paths = [r.url.path for r in requests if r.url.path == "/classify"]
        assert classify_paths == []

    async def test_rule_miss_falls_through_to_hosted(self) -> None:
        handler_fn, _ = _make_classify_handler(_strong_3way_scores())
        client = _build_client(handler_fn)
        cfg = ClassifyConfig(
            taxonomy_schema="energy_event_taxonomy",
            backend="rules_then_hosted",
            labels=_default_labels(),
            rules=[Rule(pattern=r"\bICS-CERT advisory\b", label="cyberattack")],
            min_confidence=0.4,
        )
        handler = ClassifyHandler(cfg, nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        sig = _signal({"body": "Major blackout reported overnight"})
        out = await handler.transform(sig, _ctx())
        cls = out.payload["classification"]
        assert cls["backend_used"] == "zero_shot"
        assert cls["event_type"] == "outage"


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGracefulDegradation:
    async def test_service_5xx_stamps_other(self) -> None:
        handler_fn, _ = _make_classify_handler({}, status=503,
                                                response_body={"error": "down"})
        client = _build_client(handler_fn)
        handler = ClassifyHandler(_default_config(), nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        sig = _signal({"body": "Major blackout reported overnight"})
        out = await handler.transform(sig, _ctx())
        assert out is not None
        cls = out.payload["classification"]
        assert cls["event_type"] == OTHER_LABEL
        assert cls["confidence"] == 0.0
        health = await handler.health_check(_ctx())
        assert health.state == "degraded"

    async def test_service_401_stamps_other_with_auth_error(self) -> None:
        handler_fn, _ = _make_classify_handler({}, status=401,
                                                response_body={"detail": "auth"})
        client = _build_client(handler_fn)
        handler = ClassifyHandler(_default_config(), nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        sig = _signal({"body": "Major blackout reported overnight"})
        out = await handler.transform(sig, _ctx())
        cls = out.payload["classification"]
        assert cls["event_type"] == OTHER_LABEL
        health = await handler.health_check(_ctx())
        assert health.state == "degraded"
        assert "auth" in (health.last_error or "")


# ---------------------------------------------------------------------------
# Health + lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHealth:
    async def test_health_before_activate_is_degraded(self) -> None:
        handler_fn, _ = _make_classify_handler(_strong_3way_scores())
        client = _build_client(handler_fn)
        handler = ClassifyHandler(_default_config(), nlp_client=client)
        health = await handler.health_check(_ctx())
        assert isinstance(health, FilterHealth)
        assert health.state == "degraded"

    async def test_health_after_activate_is_healthy(self) -> None:
        handler_fn, _ = _make_classify_handler(_strong_3way_scores())
        client = _build_client(handler_fn)
        handler = ClassifyHandler(_default_config(), nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        health = await handler.health_check(_ctx())
        assert health.state == "healthy"
        assert health.detail["backend"] == "zero_shot_hosted"
        assert health.detail["service_bound"] is True

    async def test_pause_drops_activation(self) -> None:
        handler_fn, _ = _make_classify_handler(_strong_3way_scores())
        client = _build_client(handler_fn)
        handler = ClassifyHandler(_default_config(), nlp_client=client)
        await handler.on_configure(nlp_client=client)
        await handler.on_activate()
        assert handler.is_activated
        await handler.on_pause()
        assert handler.is_activated is False


# ---------------------------------------------------------------------------
# Helpers (preserved for backwards-compat assertion targets)
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_cosine_orthogonal_zero(self) -> None:
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_cosine_identical_one(self) -> None:
        v = [0.6, 0.8]
        assert _cosine(v, v) == pytest.approx(1.0, rel=1e-9)

    def test_cosine_zero_vec_returns_zero(self) -> None:
        assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_cosine_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            _cosine([1.0], [1.0, 0.0])

    def test_build_label_anchor_includes_name_and_examples(self) -> None:
        lab = Label(name="protest", description="public protest", examples=["march"])
        anchor = _build_label_anchor(lab)
        assert "protest" in anchor
        assert "public protest" in anchor
        assert "march" in anchor

    def test_signal_text_takes_title_and_body(self) -> None:
        sig = _signal({"title": "T", "body": "B", "junk": 1})
        assert _signal_text(sig, 1024) == "T\nB"

    def test_signal_text_truncates(self) -> None:
        sig = _signal({"body": "x" * 5000})
        assert len(_signal_text(sig, 100)) == 100

    def test_taxonomy_fingerprint_stable_for_same_labels(self) -> None:
        fp1 = _taxonomy_fingerprint("schema", _default_labels(), [])
        fp2 = _taxonomy_fingerprint("schema", _default_labels(), [])
        assert fp1 == fp2

    def test_taxonomy_fingerprint_changes_on_label_change(self) -> None:
        labels1 = _default_labels()
        labels2 = list(labels1)
        labels2[0] = Label(
            name=labels2[0].name,
            description="DIFFERENT",
            examples=labels2[0].examples,
        )
        fp1 = _taxonomy_fingerprint("schema", labels1, [])
        fp2 = _taxonomy_fingerprint("schema", labels2, [])
        assert fp1 != fp2

    def test_default_sentiment_seeds_canonical_keys(self) -> None:
        assert set(DEFAULT_SENTIMENT_SEEDS.keys()) == set(SENTIMENT_LABELS)


# ---------------------------------------------------------------------------
# Per-target taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPerTargetTaxonomy:
    async def test_two_instances_with_different_schemas_isolated(self) -> None:
        # Energy taxonomy.
        a_handler_fn, _ = _make_classify_handler(_strong_3way_scores())
        # Disaster taxonomy.
        disaster_scores = {
            "hurricane warning": {"hurricane": 0.95, "earthquake": 0.05},
            "quake": {"earthquake": 0.95, "hurricane": 0.05},
        }
        b_handler_fn, _ = _make_classify_handler(disaster_scores)
        client_a = _build_client(a_handler_fn)
        client_b = _build_client(b_handler_fn)
        cfg_a = _default_config(taxonomy_schema="energy_event_taxonomy")
        cfg_b = ClassifyConfig(
            taxonomy_schema="natural_disaster_taxonomy",
            labels=[
                Label(name="hurricane", description="Hurricane / storm events"),
                Label(name="earthquake", description="Earthquake events"),
            ],
        )
        a = ClassifyHandler(cfg_a, nlp_client=client_a)
        b = ClassifyHandler(cfg_b, nlp_client=client_b)
        await a.on_configure(nlp_client=client_a)
        await b.on_configure(nlp_client=client_b)
        await a.on_activate()
        await b.on_activate()
        out_a = await a.transform(_signal({"body": "outage in the grid"}), _ctx())
        out_b = await b.transform(_signal({"body": "hurricane warning issued"}), _ctx())
        assert out_a.payload["classification"]["event_type"] == "outage"
        assert out_a.payload["classification"]["schema"] == "energy_event_taxonomy"
        assert out_b.payload["classification"]["event_type"] == "hurricane"
        assert out_b.payload["classification"]["schema"] == "natural_disaster_taxonomy"
