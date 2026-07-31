# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loud-degrade guard for a malformed ``method.llm.*`` descriptor entry.

Live 2026-07-30: a ``method.llm.primary`` entry authored in the WRONG shape
(``{"stack_ref": "llm.primary.openai_compat"}`` instead of the
property-factory form ``{"raw": ..., "factory_kind": "stack_ref",
"expected_family": "llm_provider"}``) left the W-B2 claim_watch
bearing-CONFIRM leg dark with ZERO trace — ``_primary_llm_component_id``
returned ``None`` exactly as it does for an operator who never configured
the key at all, so a descriptor AUTHORING MISTAKE was indistinguishable from
"nothing configured, as documented/expected."

These tests pin the fix in ``_extract_llm_ref_component_id`` (the helper
shared by ``_primary_llm_component_id`` / ``_narrate_llm_component_id`` /
``_verify_llm_component_id``): a key that is ABSENT stays silent (no
regression); a key that is PRESENT but unrecognized-shaped logs a WARNING
carrying the distinct ``llm_ref_malformed`` token + the descriptor id + the
key name, and still returns ``None`` — the build is never failed, only made
observable. No DB / Dapr / registry — pure function calls over an in-process
descriptor.
"""

from __future__ import annotations

import logging

import pytest

from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    MappingBlock,
    MethodBlock,
    SubscriptionBlock,
    TypeSignature,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Property
from legba.runtime.analyst_deps_builder import (
    _narrate_llm_component_id,
    _primary_llm_component_id,
    _verify_llm_component_id,
)

_VERSION = "0" * 64
_PRIMARY_REF = "llm.primary.openai_compat"


def _descriptor(*, llm: dict) -> AnalystDescriptor:
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="malformed_llm_ref_test",
            name="malformed llm ref test",
            schema_uri="legba/analyst/1.0.0",
            version=_VERSION,
            kind=AnalystKind.INLINE_TARGET,
            type_signature=TypeSignature(
                input_type="legba.x.In", output_type="legba.x.Out",
            ),
            state=LifecycleState.ACTIVE,
            owner="test",
        ),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.prompts.inline_target.v1",
            llm=llm,
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
    )


_LOGGER_NAME = "legba.runtime.analyst_deps_builder"


# ---------------------------------------------------------------------------
# Malformed shape → WARNING + None (never raises, never fails the build)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, helper",
    [
        ("primary", _primary_llm_component_id),
        ("narrate", _narrate_llm_component_id),
        ("verify", _verify_llm_component_id),
    ],
)
def test_malformed_mapping_shape_warns_and_returns_none(key, helper, caplog):
    """The live defect shape: {"stack_ref": <ref>} instead of the
    property-factory {"raw": ..., "factory_kind": "stack_ref", ...} form."""
    descriptor = _descriptor(llm={key: {"stack_ref": _PRIMARY_REF}})
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = helper(descriptor)
    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, f"expected a WARNING for malformed method.llm.{key}"
    assert any("llm_ref_malformed" in r.message for r in warnings)
    assert any(f"key={key}" in r.message for r in warnings)
    assert any("malformed_llm_ref_test" in r.message for r in warnings)


def test_malformed_shape_never_raises():
    """Never fail the build — a malformed ref degrades, it does not except."""
    descriptor = _descriptor(llm={"primary": {"stack_ref": _PRIMARY_REF}})
    assert _primary_llm_component_id(descriptor) is None  # no raise


def test_empty_string_ref_also_warns(caplog):
    """A present-but-empty ref (a descriptor authoring slip, e.g. raw: "")
    is likewise a malformed entry, not a silent absence."""
    descriptor = _descriptor(llm={"primary": ""})
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = _primary_llm_component_id(descriptor)
    assert result is None
    assert any(
        "llm_ref_malformed" in r.message for r in caplog.records
        if r.levelno == logging.WARNING
    )


# ---------------------------------------------------------------------------
# Absent key → stays silent (zero-regression: this is the documented,
# expected "not configured" case, not a defect)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, helper",
    [
        ("primary", _primary_llm_component_id),
        ("narrate", _narrate_llm_component_id),
        ("verify", _verify_llm_component_id),
    ],
)
def test_absent_key_stays_silent(key, helper, caplog):
    descriptor = _descriptor(llm={})
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = helper(descriptor)
    assert result is None
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# Correct shapes → resolve exactly as today, no warning at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, helper",
    [
        ("primary", _primary_llm_component_id),
        ("narrate", _narrate_llm_component_id),
        ("verify", _verify_llm_component_id),
    ],
)
def test_property_factory_shape_resolves_no_warning(key, helper, caplog):
    descriptor = _descriptor(llm={
        key: Property.StackRef(
            raw=_PRIMARY_REF, expected_family="llm_provider",
        ).model_dump(),
    })
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = helper(descriptor)
    assert result == _PRIMARY_REF
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.parametrize(
    "key, helper",
    [
        ("primary", _primary_llm_component_id),
        ("narrate", _narrate_llm_component_id),
        ("verify", _verify_llm_component_id),
    ],
)
def test_bare_string_shape_resolves_no_warning(key, helper, caplog):
    descriptor = _descriptor(llm={key: _PRIMARY_REF})
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = helper(descriptor)
    assert result == _PRIMARY_REF
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_live_stack_ref_object_shape_resolves_no_warning(caplog):
    """A live Property.StackRef instance (in-process descriptor construction,
    not a registry round-trip) is also a recognized, non-malformed shape."""
    descriptor = _descriptor(llm={
        "primary": Property.StackRef(raw=_PRIMARY_REF, expected_family="llm_provider"),
    })
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = _primary_llm_component_id(descriptor)
    assert result == _PRIMARY_REF
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
