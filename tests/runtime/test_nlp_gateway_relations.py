# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""fact quality on the gateway's ``/relations`` answer.

The first gateway facts (2026-10-23) landed at the 0.5 floor as junk
("Melania Trump | operates in | Oleshky", "truce | signed agreement with |
Oleshky", "Oleshky | located in | Ukrainian city"). These tests pin the three
component knobs and the junk gate on :class:`GatewayNlpClient`:

  * ``relation_confidence`` — min (default) / mean / none of the endpoint scores;
  * ``relation_threshold`` — sent as ``threshold``, the 400-naming-it fallback,
    and the client-side filter on the same score;
  * ``relation_labels`` — the default DERIVED from the graph's relationship types;
  * head != tail, >= 2 chars, not a bare stopword.

No network, no container, no registry: ``httpx.MockTransport`` throughout.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from legba.data.schemas.stack import NLPService, NLPServiceConfig
from legba.data.stack.nlp_service import (
    GatewayNlpClient,
    NlpServiceClient,
    NlpServiceUnavailable,
)
from legba.data.stack.nlp_service.gateway import DEFAULT_RELATION_LABELS as GW_LABELS
from legba.data.stack.nlp_service.gateway import NlpServiceRejected
from legba.data.stack.nlp_service.gateway_relations import (
    DEFAULT_RELATION_LABELS,
    LEGACY_GLIREL_RELATION_LABELS,
    aggregate_confidence,
    apply_relation_quality,
    derive_relation_labels,
    junk_reason,
)
from legba.data.vocabulary import (
    PREDICATE_CANONICAL,
    RELATIONSHIP_TYPES,
    normalize_predicate,
)
from legba.runtime.nlp_client_factory import (
    NlpClientFactoryError,
    build_nlp_client_from_stack_component,
)
from legba.runtime.registry_client import RegistryHTTPClient

_REPO = Path(__file__).resolve().parents[2]
_YAML = _REPO / "descriptors" / "stack_component_nlp_gateway_bearer.yaml"
_COMPONENT_ID = "nlp.gateway.bearer"
_BASE = "https://gw.test/v1"
_TOKEN = "tok-test"
_TEXT = "Melania Trump visited Kyiv; Russian forces shelled Oleshky."


def _hit(head: str, tail: str, hc: float | None = None, tc: float | None = None) -> Any:
    if hc is None and tc is None:
        return [head, tail]
    return {"head": {"text": head, "confidence": hc}, "tail": {"text": tail, "confidence": tc}}


def _answer(**by_label: list[Any]) -> dict[str, Any]:
    return {"model": "ner", "results": [{"relation_extraction": by_label}]}


class _Gateway:
    def __init__(self, *responses: httpx.Response):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def body(self, i: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def _client(gw: _Gateway, **kw: Any) -> GatewayNlpClient:
    async def _sleep(_s: float) -> None:
        return None

    inner = httpx.AsyncClient(transport=httpx.MockTransport(gw), base_url=_BASE)
    return GatewayNlpClient(endpoint=_BASE, api_key=_TOKEN, client=inner, sleep=_sleep, **kw)


_MIXED = _answer(
    leader_of=[_hit("Vladimir Putin", "Russia", 0.95, 0.91)],
    # The live junk shape: a strong head, a weak tail.
    operates_in=[_hit("Melania Trump", "Oleshky", 0.93, 0.31)],
    located_in=[_hit("Oleshky", "Kherson Oblast", 0.62, 0.58)],
)


# ---------------------------------------------------------------------------
# 1. confidence: min / mean / none
# ---------------------------------------------------------------------------


async def test_default_confidence_is_the_min_of_the_endpoints():
    client = _client(_Gateway(httpx.Response(200, json=_MIXED)))
    assert client.relation_confidence == "min" and client.relation_threshold == 0.5
    triples = (await client.extract(_TEXT))["triples"]
    by_subject = {t["subject"]: t for t in triples}
    assert by_subject["Vladimir Putin"]["confidence"] == 0.91
    assert by_subject["Oleshky"]["confidence"] == 0.58
    # The endpoint scores are still carried.
    assert by_subject["Oleshky"]["subject_confidence"] == 0.62
    assert by_subject["Oleshky"]["object_confidence"] == 0.58
    # min(0.93, 0.31) = 0.31 < 0.5: the junk pair never reaches the fact path.
    assert "Melania Trump" not in by_subject


async def test_mean_confidence_lets_a_strong_end_lift_its_partner():
    client = _client(_Gateway(httpx.Response(200, json=_MIXED)), relation_confidence="mean")
    triples = (await client.extract(_TEXT))["triples"]
    by_subject = {t["subject"]: t for t in triples}
    assert by_subject["Vladimir Putin"]["confidence"] == 0.93
    # mean(0.93, 0.31) = 0.62 — exactly why min is the default.
    assert by_subject["Melania Trump"]["confidence"] == 0.62


async def test_none_confidence_stamps_nothing_but_still_gates_on_the_min():
    client = _client(_Gateway(httpx.Response(200, json=_MIXED)), relation_confidence="NONE")
    triples = (await client.extract(_TEXT))["triples"]
    assert all("confidence" not in t for t in triples), "the fact path keeps its floor"
    assert {t["subject"] for t in triples} == {"Vladimir Putin", "Oleshky"}
    assert client.extract_counts["dropped_below_threshold"] == 1


def test_aggregate_needs_both_endpoint_scores():
    assert aggregate_confidence(0.9, None, "min") is None
    assert aggregate_confidence(None, 0.9, "mean") is None
    assert aggregate_confidence(0.9, 0.7, "none") is None
    assert aggregate_confidence(0.9, 0.7, "min") == 0.7
    assert aggregate_confidence(0.9, 0.7, "mean") == 0.8
    assert aggregate_confidence(1.4, 1.2, "min") == 1.0  # clamped


def test_a_bad_mode_or_threshold_fails_at_construction():
    with pytest.raises(ValueError, match="relation_confidence"):
        GatewayNlpClient(endpoint=_BASE, api_key=_TOKEN, relation_confidence="max")
    with pytest.raises(ValueError, match="relation_threshold"):
        GatewayNlpClient(endpoint=_BASE, api_key=_TOKEN, relation_threshold=1.5)


# ---------------------------------------------------------------------------
# 2. threshold: sent, the 400 fallback, client-side filtering
# ---------------------------------------------------------------------------


async def test_threshold_is_sent_and_below_threshold_hits_are_dropped_and_counted(caplog):
    gw = _Gateway(httpx.Response(200, json=_MIXED))
    client = _client(gw, relation_threshold=0.6)
    with caplog.at_level(logging.DEBUG, logger="legba.data.stack.nlp_service.gateway"):
        triples = (await client.extract(_TEXT))["triples"]
    assert gw.body()["threshold"] == 0.6
    assert [t["subject"] for t in triples] == ["Vladimir Putin"]
    assert client.extract_counts == {
        "calls": 1, "hits": 3, "kept": 1, "dropped_junk": 0,
        "dropped_below_threshold": 2, "threshold_refused": 0,
    }
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("nlp_gateway.extract"))
    assert "dropped_below_threshold=2" in line and "threshold_sent=True" in line
    assert _TEXT not in line and "Oleshky" not in line, "counts only, never text"


async def test_threshold_zero_is_off_not_sent_nothing_dropped():
    gw = _Gateway(httpx.Response(200, json=_MIXED))
    client = _client(gw, relation_threshold=0)
    assert client.relation_threshold is None
    triples = (await client.extract(_TEXT))["triples"]
    assert "threshold" not in gw.body()
    assert len(triples) == 3
    assert {t["subject"]: t["confidence"] for t in triples}["Melania Trump"] == 0.31


@pytest.mark.parametrize("refusal", [
    httpx.Response(400, json={"error": {
        "message": "Unrecognized request argument supplied: threshold",
        "type": "invalid_request_error"}}),
    httpx.Response(422, json={"detail": [
        {"loc": ["body", "threshold"], "msg": "Extra inputs are not permitted",
         "input": 0.5}]}),
])
async def test_a_refused_threshold_is_retried_once_without_it_then_filtered_here(refusal):
    gw = _Gateway(refusal, httpx.Response(200, json=_MIXED), httpx.Response(200, json=_MIXED))
    client = _client(gw)
    triples = (await client.extract(_TEXT))["triples"]
    assert len(gw.requests) == 2
    assert gw.body(0)["threshold"] == 0.5
    assert "threshold" not in gw.body(1)
    # The call was not lost, and the client-side filter still holds the line.
    assert {t["subject"] for t in triples} == {"Vladimir Putin", "Oleshky"}
    assert client.extract_counts["threshold_refused"] == 1
    assert client.extract_counts["dropped_below_threshold"] == 1
    # Sticky: the next call does not pay the refusal again.
    await client.extract(_TEXT)
    assert len(gw.requests) == 3 and "threshold" not in gw.body(2)


async def test_a_400_that_does_not_name_threshold_is_not_retried():
    gw = _Gateway(httpx.Response(400, json={"error": {"message": "text: too long"}}))
    client = _client(gw)
    with pytest.raises(NlpServiceUnavailable) as exc:
        await client.extract(_TEXT)
    assert isinstance(exc.value, NlpServiceRejected) and exc.value.status == 400
    assert len(gw.requests) == 1


async def test_a_refusal_with_no_threshold_sent_is_not_retried():
    gw = _Gateway(httpx.Response(400, json={"error": {"message": "threshold: bad"}}))
    client = _client(gw, relation_threshold=0)
    with pytest.raises(NlpServiceUnavailable):
        await client.extract(_TEXT)
    assert len(gw.requests) == 1


async def test_unscored_plain_pairs_are_never_dropped_by_the_threshold():
    gw = _Gateway(httpx.Response(200, json=_answer(supplies_to=[_hit("Iran", "Hezbollah")])))
    client = _client(gw, relation_labels=["supplies to"], relation_threshold=0.9)
    triples = (await client.extract(_TEXT))["triples"]
    assert [(t["subject"], t["object"]) for t in triples] == [("Iran", "Hezbollah")]
    assert "confidence" not in triples[0], "no score to stamp: the fact path's floor"


# ---------------------------------------------------------------------------
# 3. vocabulary: the default labels are what the graph consumes
# ---------------------------------------------------------------------------


def _label_to_type() -> dict[str, str]:
    """Row-predicate form → graph relationship type, via the write path's canon."""
    out: dict[str, str] = {}
    for rel_type in RELATIONSHIP_TYPES:
        out[normalize_predicate(rel_type)] = rel_type
    return out


def test_the_default_labels_are_derived_from_the_graph_relationship_types():
    assert GW_LABELS is DEFAULT_RELATION_LABELS
    assert DEFAULT_RELATION_LABELS == derive_relation_labels(RELATIONSHIP_TYPES)
    mapping = _label_to_type()
    # Every label sent maps back onto a graph relationship type ...
    assert {mapping[label] for label in DEFAULT_RELATION_LABELS} <= set(RELATIONSHIP_TYPES)
    # ... and every graph type except the generic fallback is asked for.
    assert {mapping[label] for label in DEFAULT_RELATION_LABELS} == (
        set(RELATIONSHIP_TYPES) - {"CoOccursWith"}
    )
    # The label IS the write path's predicate form: normalize_predicate keeps it.
    assert all(normalize_predicate(lbl) == lbl for lbl in DEFAULT_RELATION_LABELS)
    assert all(lbl == lbl.casefold() for lbl in DEFAULT_RELATION_LABELS)
    assert DEFAULT_RELATION_LABELS == (
        "hostile to", "located in", "allied with", "party to", "targets",
        "operates in", "member of", "leader of", "conducted via",
        "supplies weapons to", "part of", "affiliated with", "involved in",
    )


def test_the_reifier_can_sign_every_default_label():
    pytest.importorskip("networkx")
    from legba.data.analysts.deterministic_handlers.structural_balance import POLARITY

    mapping = _label_to_type()
    assert {mapping[lbl] for lbl in DEFAULT_RELATION_LABELS} <= set(POLARITY)


def test_every_default_label_has_a_decay_class():
    from legba.data.facts.decay import DEFAULT_PREDICATE_CLASSES

    assert set(DEFAULT_RELATION_LABELS) <= set(DEFAULT_PREDICATE_CLASSES)


def test_the_generic_glirel_labels_nothing_types_are_gone():
    dropped = set(LEGACY_GLIREL_RELATION_LABELS) - set(DEFAULT_RELATION_LABELS)
    assert dropped == {
        "headquarters in", "founded by", "ally of", "opponent of", "conflict with",
        "signed agreement with", "sanctioned by", "spokesperson for", "supplies to",
        "border with", "capital of", "controls", "parent organization of",
        "subsidiary of", "employed by",
    }
    assert not any(normalize_predicate(lbl) in _label_to_type() for lbl in dropped)


def test_a_future_vocabulary_type_is_split_from_camelcase():
    assert derive_relation_labels(["LeaderOf", "FundedBy", "CoOccursWith"]) == (
        "leader of", "funded by",
    )
    assert "fundedby" not in PREDICATE_CANONICAL


async def test_the_labels_override_is_kept():
    gw = _Gateway(httpx.Response(200, json=_answer()))
    client = _client(gw, relation_labels=list(LEGACY_GLIREL_RELATION_LABELS))
    await client.extract(_TEXT)
    assert gw.body()["relations"] == list(LEGACY_GLIREL_RELATION_LABELS)


# ---------------------------------------------------------------------------
# 4. the junk gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("head, tail, reason", [
    ("Oleshky", "oleshky.", "same_endpoint"),
    ("  Kyiv ", "KYIV", "same_endpoint"),
    ("X", "Russia", "short_endpoint"),
    ("Russia", "'", "short_endpoint"),
    ("he", "Russia", "stopword_endpoint"),
    ("Iran", "The", "stopword_endpoint"),
    ("US", "NATO", None),        # an acronym is not the stopword "us"
    ("WHO", "Geneva", None),     # ... nor "who"
    ("UK", "Ukraine", None),
])
def test_junk_reason(head, tail, reason):
    assert junk_reason(head, tail) == reason


async def test_junk_pairs_are_dropped_and_counted_before_the_threshold():
    gw = _Gateway(httpx.Response(200, json=_answer(
        located_in=[_hit("Oleshky", "Oleshky", 0.9, 0.9), _hit("he", "Kyiv", 0.9, 0.9),
                    _hit("Oleshky", "Kherson Oblast", 0.8, 0.7)],
    )))
    client = _client(gw)
    triples = (await client.extract(_TEXT))["triples"]
    assert [(t["subject"], t["object"]) for t in triples] == [("Oleshky", "Kherson Oblast")]
    assert client.extract_counts["dropped_junk"] == 2
    assert client.extract_counts["dropped_below_threshold"] == 0


def test_apply_relation_quality_is_pure_and_counts():
    triples = [
        {"subject": "A1", "predicate": "p", "object": "B1",
         "subject_confidence": 0.9, "object_confidence": 0.4},
        {"subject": "A2", "predicate": "p", "object": "A2"},
    ]
    kept, counts = apply_relation_quality(triples)
    assert kept == [] and counts == {"hits": 2, "kept": 0, "dropped_junk": 1,
                                     "dropped_below_threshold": 1}
    assert "confidence" not in triples[0], "the input is not mutated"


# ---------------------------------------------------------------------------
# 5. schema, descriptor, factory; the legacy client unchanged
# ---------------------------------------------------------------------------


def test_the_descriptor_carries_the_new_knobs_and_validates():
    cfg = NLPService.model_validate(yaml.safe_load(_YAML.read_text())).config
    assert cfg.relation_confidence.raw == "min"
    assert cfg.relation_threshold.raw == 0.5
    assert cfg.relation_labels is None, "absent so the default tracks the vocabulary"


@pytest.mark.parametrize("field, raw, needle", [
    ("relation_confidence", {"factory_kind": "text", "raw": "max"}, "must be 'min'"),
    ("relation_threshold", {"factory_kind": "number", "raw": 1.5}, r"\[0, 1\]"),
    ("relation_threshold", {"factory_kind": "number", "raw": -0.1}, r"\[0, 1\]"),
])
def test_the_schema_refuses_bad_knob_values(field, raw, needle):
    body = yaml.safe_load(_YAML.read_text())["config"]
    body[field] = raw
    with pytest.raises(ValueError, match=needle):
        NLPServiceConfig.model_validate(body)


def _legacy_config() -> dict[str, Any]:
    t = lambda s: {"factory_kind": "text", "raw": s}  # noqa: E731
    return {
        "endpoint": t("https://nlp.example.internal"),
        "api_user": {"factory_kind": "secret", "raw": "nlp.local.legba_models.api_user"},
        "api_pass": {"factory_kind": "secret", "raw": "nlp.local.legba_models.api_pass"},
        "timeout_seconds": {"factory_kind": "number", "raw": 60},
        "translate_path": t("/translate"), "classify_path": t("/classify"),
        "extract_path": t("/extract"), "summarize_path": t("/summarize"),
        "health_path": t("/health"),
    }


def test_the_legacy_row_validates_unchanged_and_refuses_the_gateway_knobs():
    cfg = NLPServiceConfig.model_validate(_legacy_config())
    assert cfg.relation_confidence is None and cfg.relation_threshold is None
    dumped = cfg.model_dump(exclude_none=True)
    assert "relation_confidence" not in dumped and "relation_threshold" not in dumped
    for field, raw in (("relation_confidence", {"factory_kind": "text", "raw": "min"}),
                       ("relation_threshold", {"factory_kind": "number", "raw": 0.5})):
        body = _legacy_config()
        body[field] = raw
        with pytest.raises(ValueError, match="bearer_gateway"):
            NLPServiceConfig.model_validate(body)


def test_the_legacy_client_is_unchanged():
    params = inspect.signature(NlpServiceClient.__init__).parameters
    assert "relation_confidence" not in params and "relation_threshold" not in params


async def test_the_legacy_client_passes_extract_through_untouched():
    answer = {"triples": [{"subject": "he", "predicate": "operates in", "object": "he"}],
              "ms": 1.0}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer)

    client = NlpServiceClient(endpoint="http://legba-models.test")
    client._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="http://legba-models.test",
    )
    assert await client.extract("x") == answer
    assert json.loads(seen[0].content) == {"text": "x"}


def _registry(body: dict[str, Any]) -> RegistryHTTPClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": _COMPONENT_ID, "version": "0" * 16, "body": body})

    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                              base_url="http://registry.test")
    return RegistryHTTPClient(base_url="http://registry.test", token=None, client=inner)


async def _secrets(_secret_id: str) -> bytes:
    return _TOKEN.encode()


async def test_the_factory_applies_the_descriptor_knobs():
    body = yaml.safe_load(_YAML.read_text())
    body["config"]["relation_confidence"]["raw"] = "mean"
    body["config"]["relation_threshold"]["raw"] = 0.35
    client = await build_nlp_client_from_stack_component(
        _COMPONENT_ID, registry_client=_registry(body), secrets_resolve=_secrets,
    )
    assert client.relation_confidence == "mean" and client.relation_threshold == 0.35
    assert client.relation_labels == list(DEFAULT_RELATION_LABELS)


async def test_the_factory_defaults_when_the_knobs_are_absent():
    body = yaml.safe_load(_YAML.read_text())
    body["config"].pop("relation_confidence")
    body["config"].pop("relation_threshold")
    client = await build_nlp_client_from_stack_component(
        _COMPONENT_ID, registry_client=_registry(body), secrets_resolve=_secrets,
    )
    assert client.relation_confidence == "min" and client.relation_threshold == 0.5


async def test_the_factory_fails_loud_on_a_bad_knob():
    body = yaml.safe_load(_YAML.read_text())
    body["config"]["relation_confidence"]["raw"] = "max"
    with pytest.raises(NlpClientFactoryError, match="relation_confidence"):
        await build_nlp_client_from_stack_component(
            _COMPONENT_ID, registry_client=_registry(body), secrets_resolve=_secrets,
        )


@pytest.fixture(autouse=True)
def _gateway_env(monkeypatch):
    """The gateway descriptors name their endpoint and models as ${env:NAME}."""
    from tests._gateway_env import set_gateway_env

    set_gateway_env(monkeypatch)
