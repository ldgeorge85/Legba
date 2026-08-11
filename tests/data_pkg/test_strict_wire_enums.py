# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict-mode enum coercion on the WIRE path — the whole schema class (§31.1).

Every descriptor-identity model in :mod:`legba.data.schemas` is
``ConfigDict(strict=True)``, which switches pydantic's ``str`` → ``Enum``
coercion OFF. But the WIRE form of those models is always the bare string:
``model_dump(mode="json")`` turns ``LifecycleState.ACTIVE`` into ``"active"``,
and that is exactly what the registry's ``/typed`` route serves and what the
descriptor JSONB rows store. So a model can fail to parse the output of its own
serializer.

That is the 2026-08-01 unit-fleet outage. ``AnalystIdentity.state`` had no
before-validator, every typed-descriptor parse raised ``is_instance_of``,
actor activation spun in a hot descriptor-refetch loop, and 8,500 tests missed
it because every in-process construction passes the ENUM, never the string.

``analyst.py:_coerce_state`` fixed the one model that had already broken.
:class:`legba.data.schemas.lifecycle.WireEnumCoercion` generalizes it. These
tests cover the whole class of bug in three layers:

  1. per-model wire-shape parses for every strict model that carries an enum
     field (the readable, explicit regression);
  2. the keep-tests — in-process enum construction is unchanged, and a bogus
     string still REJECTS (coercion must not become permissiveness);
  3. a DRIFT GUARD that discovers every ``strict=True`` model in the schemas
     package and fails if any enum-typed field lacks a ``mode='before'``
     coercer — so the NEXT model to grow an enum field cannot ship the same
     outage.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from datetime import datetime, timezone
from enum import Enum
from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel

import legba.data.schemas as _schemas_pkg
from legba.data.schemas.action_pack import ActionPackIdentity
from legba.data.schemas.analyst import AnalystIdentity
from legba.data.schemas.lifecycle import (
    AbstractionLevel,
    LifecycleState,
    LifecycleTransition,
)
from legba.data.schemas.source import SourceIdentity
from legba.data.schemas.stack import PostgresCluster, StackComponentBase
from legba.data.schemas.target import TargetIdentity

_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
_HASH = "a" * 16


# ---------------------------------------------------------------------------
# Wire payloads — enums as the BARE STRINGS the registry actually serves.
#
# ``created`` / ``at`` stay real ``datetime`` objects: strict mode rejects
# raw-string datetimes too, independently of the enum bug, and the production
# wire path clears that with ``model_validate_json`` (JSON mode parses ISO
# strings under strict). Keeping datetimes native here isolates the enum
# behaviour; the full JSON round-trip is asserted separately below.
# ---------------------------------------------------------------------------

_TARGET_WIRE: dict[str, Any] = {
    "id": "wire_probe",
    "name": "Wire Probe",
    "schema_uri": "legba/target/1.0.0",
    "version": _HASH,
    "abstraction_level": "L1",
    "state": "active",
    "owner": "wire_test",
    "created": _NOW,
}

_SOURCE_WIRE: dict[str, Any] = {
    "id": "source.wire.probe",
    "name": "Wire Probe",
    "kind": "rss",
    "schema_uri": "legba/source/1.0.0",
    "version": _HASH,
    "abstraction_level": "L2",
    "state": "paused",
    "owner": "wire_test",
    "created": _NOW,
}

_ACTION_PACK_WIRE: dict[str, Any] = {
    "id": "pack.wire.probe",
    "name": "Wire Probe",
    "schema_uri": "legba/action_pack/1.0.0",
    "version": _HASH,
    "abstraction_level": "L3",
    "state": "configured",
    "owner": "wire_test",
    "created": _NOW,
}

_STACK_WIRE: dict[str, Any] = {
    "id": "pg.primary.wire_probe",
    "name": "Wire Probe",
    "schema_uri": "legba/stack/postgres_cluster/1.0.0",
    "version": _HASH,
    "state": "active",
    "owner": "wire_test",
}

_TRANSITION_WIRE: dict[str, Any] = {
    "descriptor_id": "wire_probe",
    "descriptor_kind": "analyst",
    "from_state": "configured",
    "to_state": "active",
    "at": _NOW,
    "actor": "wire_test",
}

_ANALYST_WIRE: dict[str, Any] = {
    "id": "wire_probe",
    "name": "Wire Probe",
    "schema_uri": "legba/analyst/1.0.0",
    "version": _HASH,
    "kind": "inline_target",
    "type_signature": {
        "deps_type": "legba.runtime.deps.StandardDeps",
        "input_type": "legba.runtime.SignalList",
        "output_type": "legba.runtime.Finding",
    },
    "state": "active",
    "owner": "wire_test",
}


# ---------------------------------------------------------------------------
# 1) Per-model wire-shape parses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, wire, expected",
    [
        (TargetIdentity, _TARGET_WIRE,
         {"state": LifecycleState.ACTIVE,
          "abstraction_level": AbstractionLevel.L1}),
        (SourceIdentity, _SOURCE_WIRE,
         {"state": LifecycleState.PAUSED,
          "abstraction_level": AbstractionLevel.L2}),
        (ActionPackIdentity, _ACTION_PACK_WIRE,
         {"state": LifecycleState.CONFIGURED,
          "abstraction_level": AbstractionLevel.L3}),
        (StackComponentBase, _STACK_WIRE,
         {"state": LifecycleState.ACTIVE}),
        (LifecycleTransition, _TRANSITION_WIRE,
         {"from_state": LifecycleState.CONFIGURED,
          "to_state": LifecycleState.ACTIVE}),
        (AnalystIdentity, _ANALYST_WIRE,
         {"state": LifecycleState.ACTIVE}),
    ],
    ids=["target", "source", "action_pack", "stack", "transition", "analyst"],
)
def test_wire_string_enums_parse_under_strict_mode(model, wire, expected):
    """The bare-string form the registry serves MUST parse into enum members."""
    parsed = model.model_validate(wire)
    for field, want in expected.items():
        got = getattr(parsed, field)
        assert got is want, f"{model.__name__}.{field} = {got!r}, want {want!r}"


@pytest.mark.parametrize(
    "wire_state",
    ["draft", "configured", "active", "paused", "retired"],
)
def test_every_lifecycle_state_string_parses(wire_state):
    """All five states, not just the one that happened to break."""
    for model, wire in (
        (TargetIdentity, _TARGET_WIRE),
        (SourceIdentity, _SOURCE_WIRE),
        (ActionPackIdentity, _ACTION_PACK_WIRE),
        (StackComponentBase, _STACK_WIRE),
    ):
        parsed = model.model_validate({**wire, "state": wire_state})
        assert parsed.state is LifecycleState(wire_state)


@pytest.mark.parametrize("wire_level", ["L1", "L2", "L3"])
def test_every_abstraction_level_string_parses(wire_level):
    for model, wire in (
        (TargetIdentity, _TARGET_WIRE),
        (SourceIdentity, _SOURCE_WIRE),
        (ActionPackIdentity, _ACTION_PACK_WIRE),
    ):
        parsed = model.model_validate({**wire, "abstraction_level": wire_level})
        assert parsed.abstraction_level is AbstractionLevel(wire_level)


def test_stack_subclasses_inherit_the_coercion():
    """All nine component families subclass ``StackComponentBase`` — the fix
    must reach every one WITHOUT a per-family edit.

    Structural rather than parse-based on purpose: each family's ``config``
    needs its own credential-bearing Property tree, and building nine of them
    would test the config schemas, not the inheritance this pins. The
    end-to-end parse is covered on the base class above; what matters here is
    that no family shadows ``model_config`` or the validator away.
    """
    families = [c for c in StackComponentBase.__subclasses__()]
    assert len(families) >= 9, f"expected the nine families, saw {len(families)}"
    assert PostgresCluster in families
    for family in families:
        before = {
            f
            for dec in family.__pydantic_decorators__.field_validators.values()
            if dec.info.mode == "before"
            for f in dec.info.fields
        }
        assert "state" in before, f"{family.__name__} lost the state coercer"
        assert family.model_fields["state"].annotation is LifecycleState


# ---------------------------------------------------------------------------
# 2) Keep-tests — in-process construction unchanged, bogus strings reject.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, wire",
    [
        (TargetIdentity, _TARGET_WIRE),
        (SourceIdentity, _SOURCE_WIRE),
        (ActionPackIdentity, _ACTION_PACK_WIRE),
        (StackComponentBase, _STACK_WIRE),
        (AnalystIdentity, _ANALYST_WIRE),
    ],
    ids=["target", "source", "action_pack", "stack", "analyst"],
)
def test_in_process_enum_form_still_accepted(model, wire):
    """The ergonomic form every existing call site uses must not regress."""
    parsed = model.model_validate({**wire, "state": LifecycleState.RETIRED})
    assert parsed.state is LifecycleState.RETIRED


@pytest.mark.parametrize(
    "model, wire",
    [
        (TargetIdentity, _TARGET_WIRE),
        (SourceIdentity, _SOURCE_WIRE),
        (ActionPackIdentity, _ACTION_PACK_WIRE),
        (StackComponentBase, _STACK_WIRE),
        (AnalystIdentity, _ANALYST_WIRE),
    ],
    ids=["target", "source", "action_pack", "stack", "analyst"],
)
def test_bogus_state_string_still_rejected(model, wire):
    """Coercion is not permissiveness — an unknown state must still fail."""
    with pytest.raises(Exception):
        model.model_validate({**wire, "state": "not_a_state"})


@pytest.mark.parametrize(
    "model, wire",
    [
        (TargetIdentity, _TARGET_WIRE),
        (SourceIdentity, _SOURCE_WIRE),
        (ActionPackIdentity, _ACTION_PACK_WIRE),
    ],
    ids=["target", "source", "action_pack"],
)
def test_bogus_abstraction_level_string_still_rejected(model, wire):
    with pytest.raises(Exception):
        model.model_validate({**wire, "abstraction_level": "L9"})


def test_illegal_transition_still_rejected_after_coercion():
    """``LifecycleTransition`` runs an FSM check AFTER coercion — coercing the
    strings must not smuggle an illegal edge past it."""
    with pytest.raises(Exception):
        LifecycleTransition.model_validate(
            {**_TRANSITION_WIRE, "from_state": "retired", "to_state": "active"}
        )


# ---------------------------------------------------------------------------
# 3) The real /typed round trip: dump → JSON → parse.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, wire",
    [
        (TargetIdentity, _TARGET_WIRE),
        (SourceIdentity, _SOURCE_WIRE),
        (ActionPackIdentity, _ACTION_PACK_WIRE),
        (StackComponentBase, _STACK_WIRE),
        (LifecycleTransition, _TRANSITION_WIRE),
        (AnalystIdentity, _ANALYST_WIRE),
    ],
    ids=["target", "source", "action_pack", "stack", "transition", "analyst"],
)
def test_json_round_trip_survives_strict_mode(model, wire):
    """``model_dump_json`` → ``model_validate_json`` is the shape the registry
    /typed route and the descriptor JSONB rows actually move. It must close."""
    original = model.model_validate(wire)
    reparsed = model.model_validate_json(original.model_dump_json())
    assert reparsed == original


# ---------------------------------------------------------------------------
# 4) Drift guard — the NEXT enum field cannot ship uncovered.
# ---------------------------------------------------------------------------


def _enum_types_in(annotation: Any) -> list[type]:
    """Every Enum class reachable in a field annotation (incl. Optional/list)."""
    found: list[type] = []
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return [annotation]
    for arg in get_args(annotation):
        found.extend(_enum_types_in(arg))
    if get_origin(annotation) is None and not found:
        return []
    return found


def _strict_models() -> list[type[BaseModel]]:
    """Every ``strict=True`` pydantic model defined in legba.data.schemas."""
    models: dict[str, type[BaseModel]] = {}
    for mod_info in pkgutil.walk_packages(
        _schemas_pkg.__path__, prefix=f"{_schemas_pkg.__name__}."
    ):
        module = importlib.import_module(mod_info.name)
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, BaseModel) or obj is BaseModel:
                continue
            if not (obj.model_config or {}).get("strict"):
                continue
            models[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return list(models.values())


def test_the_sweep_actually_finds_models():
    """Guard the guard — a broken discovery would make this file vacuous."""
    found = _strict_models()
    assert len(found) >= 40, f"only discovered {len(found)} strict models"
    assert TargetIdentity in found
    assert StackComponentBase in found


def test_every_strict_enum_field_has_a_before_coercer():
    """THE drift guard for the 2026-08-01 outage class.

    Under ``strict=True`` an enum-typed field cannot accept the bare string
    the system's own serializer emits. Any such field therefore needs a
    ``mode='before'`` validator, or the model is one ``model_validate`` away
    from taking its family down.
    """
    uncovered: list[str] = []
    audited: list[str] = []
    for model in _strict_models():
        before_fields: set[str] = set()
        for dec in model.__pydantic_decorators__.field_validators.values():
            if dec.info.mode == "before":
                before_fields.update(dec.info.fields)
        for field_name, field in model.model_fields.items():
            if not _enum_types_in(field.annotation):
                continue
            label = f"{model.__module__}.{model.__qualname__}.{field_name}"
            audited.append(label)
            if field_name not in before_fields:
                uncovered.append(label)

    assert audited, "no enum-typed strict fields found — the walk is broken"
    assert not uncovered, (
        "strict=True enum fields with no mode='before' string coercer — these "
        "reject the bare-string wire form the registry serves:\n  "
        + "\n  ".join(uncovered)
    )
