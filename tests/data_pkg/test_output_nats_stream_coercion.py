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
