# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-5 — pure unit tests for `nats_stream` payload coercion (no broker).

The runtime output dispatcher (`dapr_actors._emit_output_bindings`) hands the
`nats_stream` sink the LIVE analyst payload — a typed `FindingPayload` /
`AlertPayload` (a pydantic model), NOT a plain dict. Before the S-5 fix the
encode step rejected it with ``payload must be a Mapping/dict (got
FindingPayload)`` (observed 9x/48h on cross_doc_corroborator +
corpus_researcher) and those findings never reached the live UI event feed.

These tests exercise `_encode_payload` / `_coerce_to_mapping` directly — no
NATS broker required — so they run in the fast unit lane. The real-broker
round-trip lives in `test_output_nats_stream.py` (integration-marked).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from legba.data.outputs.nats_stream import (
    OutputPayloadError,
    _coerce_to_mapping,
    _encode_payload,
)
from legba.data.provenance.models import (
    AlertPayload,
    FindingPayload,
    MetaFindingPayload,
)
from legba.runtime.actor_output_emit import _emit_output_bindings


# ---------------------------------------------------------------------------
# The regression: a typed FindingPayload now coerces + encodes (was: raised).
# ---------------------------------------------------------------------------


def test_finding_payload_coerces_to_mapping():
    fp = FindingPayload(
        title="Corroborated cross-doc claim",
        body="Two independent sources agree.",
        confidence=0.72,
        evidence=["【1】", "【2】"],
        tags=["severity:high", "corroborated"],
        data={"nudge": 3},
    )
    m = _coerce_to_mapping(fp)
    assert isinstance(m, dict)
    assert m["title"] == "Corroborated cross-doc claim"
    assert m["confidence"] == 0.72
    assert m["tags"] == ["severity:high", "corroborated"]
    # kind_marker is part of the model_dump — the canonical shape.
    assert m["kind_marker"] == "finding"


def test_finding_payload_encodes_to_json_dict_no_raise():
    fp = FindingPayload(title="t", body="b", confidence=0.5)
    body = _encode_payload(fp)
    decoded = json.loads(body.decode("utf-8"))
    assert isinstance(decoded, dict)
    assert decoded["title"] == "t"
    assert decoded["kind_marker"] == "finding"


def test_finding_payload_with_uuid_and_datetime_in_data():
    """UUID / datetime nested in the free-form `data` dict serialize via the
    existing `_json_default` (unchanged by the coercion)."""
    fp = FindingPayload(
        title="t",
        body="b",
        confidence=0.5,
        data={"row_id": uuid.uuid4(), "seen_at": datetime.now(tz=timezone.utc)},
    )
    decoded = json.loads(_encode_payload(fp).decode("utf-8"))
    assert isinstance(decoded["data"]["row_id"], str)
    assert isinstance(decoded["data"]["seen_at"], str)


@pytest.mark.parametrize("model_cls", [FindingPayload, AlertPayload, MetaFindingPayload])
def test_all_typed_analyst_payloads_coerce(model_cls):
    """Every typed analyst payload the dispatcher may hand the sink coerces."""
    obj = model_cls(title="t", body="b", confidence=0.5)
    m = _coerce_to_mapping(obj)
    assert isinstance(m, dict) and m["title"] == "t"
    # And round-trips through encode without raising.
    assert json.loads(_encode_payload(obj).decode("utf-8"))["title"] == "t"


# ---------------------------------------------------------------------------
# The payload kinds that ALREADY worked must keep working unchanged.
# ---------------------------------------------------------------------------


def test_plain_dict_still_encodes():
    decoded = json.loads(_encode_payload({"hello": "world", "n": 7}).decode("utf-8"))
    assert decoded == {"hello": "world", "n": 7}


def test_str_payload_passes_through():
    assert _encode_payload("already encoded") == b"already encoded"


def test_bytes_payload_passes_through():
    assert _encode_payload(b"raw bytes") == b"raw bytes"


def test_dict_with_uuid_and_datetime_still_coerced():
    decoded = json.loads(
        _encode_payload(
            {"id": uuid.uuid4(), "ts": datetime.now(tz=timezone.utc), "x": 1}
        ).decode("utf-8")
    )
    assert isinstance(decoded["id"], str)
    assert isinstance(decoded["ts"], str)
    assert decoded["x"] == 1


# ---------------------------------------------------------------------------
# Programmer-error paths preserved: non-coercible payloads still raise.
# ---------------------------------------------------------------------------


def test_list_payload_still_raises():
    with pytest.raises(OutputPayloadError, match="must be a Mapping/dict"):
        _encode_payload([1, 2, 3])


def test_non_serializable_value_in_dict_still_raises():
    class NotSerializable:
        pass

    with pytest.raises(OutputPayloadError, match="not JSON-serializable"):
        _encode_payload({"obj": NotSerializable()})


def test_bare_non_coercible_object_reports_its_typename():
    class Widget:
        pass

    with pytest.raises(OutputPayloadError, match="got Widget"):
        _encode_payload(Widget())


# ---------------------------------------------------------------------------
# Plain (non-pydantic) dataclass fallback — dataclasses.asdict path.
# ---------------------------------------------------------------------------


def test_plain_dataclass_coerces_via_asdict():
    @dataclasses.dataclass
    class Leaf:
        a: int
        b: str

    m = _coerce_to_mapping(Leaf(a=1, b="two"))
    assert m == {"a": 1, "b": "two"}
    assert json.loads(_encode_payload(Leaf(a=1, b="two")).decode("utf-8")) == {
        "a": 1,
        "b": "two",
    }


def test_dataclass_type_object_not_coerced():
    """A dataclass *class* (not an instance) is not a payload — must not coerce."""

    @dataclasses.dataclass
    class Leaf:
        a: int

    assert _coerce_to_mapping(Leaf) is None


# ---------------------------------------------------------------------------
# The REAL binding path — `_emit_output_bindings` (dapr_actors.py:3733's
# dispatcher) through to a live `nats_stream.emit` publish.
#
# 2026-09-06 defect: `_emit_output_bindings` builds one
# `OutputDeps(nats=_NatsPublishAdapter(nats_publish))` for every emit-capable
# output-kind handler — the alert / stix_bundle / webhook sinks all read
# `deps.nats.publish_json(...)`, but `nats_stream._resolve_publisher` looked
# ONLY for `deps.nats_publish` / `deps.nats_store`, neither of which
# `OutputDeps` exposes. So every `nats_stream` output binding raised
# `OutputDepsError` on its very first live publish. Live evidence: zero
# `output_emit.ok kind=nats_stream` had EVER been logged, and the only two
# descriptors that had actually exercised this path —
# `corpus_researcher` (identity.kind=inline_target, method.kind=llm_planner —
# descriptors/analyst_corpus_researcher.yaml) and `cross_doc_corroborator` —
# both failed identically (`dapr_actors.output_emit.failed kind=nats_stream
# err=deps must expose either \`nats_publish\` ... got neither`).
#
# No broker needed: this leg (`deps.nats` → `_NatsPublishAdapter.publish_json`
# → the injected closure) never touches JetStream directly — it IS the
# closure. The real-broker round-trip for `deps.nats_publish` /
# `deps.nats_store` lives in `test_output_nats_stream.py` (integration-marked).
# ---------------------------------------------------------------------------


class _FakeOutputBinding:
    def __init__(self, kind: str, config: dict[str, Any]) -> None:
        self.kind = kind
        self.config = config


class _FakeIdentity:
    def __init__(self, analyst_id: str, version: str = "deadbeef") -> None:
        self.id = analyst_id
        self.version = version


class _FakeInlineTargetDescriptor:
    """Minimal descriptor stand-in shaped like corpus_researcher's
    ``outputs: [{kind: nats_stream, config: {channel: findings}}]`` binding —
    only the attributes ``_emit_output_bindings`` actually reads."""

    def __init__(self, analyst_id: str, *, channel: str = "findings") -> None:
        self.identity = _FakeIdentity(analyst_id)
        self.outputs = [_FakeOutputBinding("nats_stream", {"channel": channel})]


async def test_emit_output_bindings_nats_stream_reaches_real_publish_via_output_deps():
    """Exercise the REAL dispatcher entry point
    (``legba.runtime.actor_output_emit._emit_output_bindings`` — the function
    ``dapr_actors.AnalystActor.run`` calls at its output-emit step) for an
    ``llm_planner`` finding, with a fake ``nats_publish`` callable standing in
    for the runtime's real closure, and assert the finding actually reaches
    it on the canonical ``analyst.<id>.<channel>`` subject. This is the
    regression test for the fix: before it, this call raised
    ``OutputDepsError`` instead of publishing."""
    calls: list[tuple[str, bytes]] = []

    async def fake_nats_publish(subject: str, payload: bytes) -> None:
        calls.append((subject, payload))

    fp = FindingPayload(
        title="IRGC ballistic missile test", body="cited body", confidence=0.6,
    )
    descriptor = _FakeInlineTargetDescriptor("corpus_researcher")

    await _emit_output_bindings(
        descriptor=descriptor,
        payload=fp,
        output_id=uuid.uuid4(),
        derived_from=[],
        target_id=None,
        nats_publish=fake_nats_publish,
    )

    assert len(calls) == 1
    subject, body = calls[0]
    assert subject == "analyst.corpus_researcher.findings"
    decoded = json.loads(body.decode("utf-8"))
    assert decoded["title"] == "IRGC ballistic missile test"


async def test_emit_output_bindings_nats_stream_with_no_publisher_is_best_effort():
    """When the runtime has no NATS publisher wired at all (``nats_publish is
    None``, e.g. a degraded boot), the dispatcher's OutputDeps carries
    ``nats=None`` and the sink's OutputDepsError is caught + logged — the
    already-durable finding must never be lost over an export failure."""
    fp = FindingPayload(title="t", body="b", confidence=0.5)
    descriptor = _FakeInlineTargetDescriptor("corpus_researcher")

    # Must not raise — best-effort export, never breaks the run.
    await _emit_output_bindings(
        descriptor=descriptor,
        payload=fp,
        output_id=uuid.uuid4(),
        derived_from=[],
        target_id=None,
        nats_publish=None,
    )
