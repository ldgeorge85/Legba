# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PIECE A — the reified-typed-Nexus descriptors validate + the bringup wires
them.

Covers the producer + the lit-up dormant consumers:
  * analyst_relationship_reifier.yaml  (relationship_reifier — the producer)
  * analyst_structural_balance.yaml    (deterministic / structural_balance)
  * analyst_graph_mining.yaml          (deterministic / graph_mining)
  * analyst_nexus_decay.yaml           (deterministic / nexus_decay)

Each must:
  1. validate against the REAL AnalystDescriptor pydantic schema (the exact
     bringup ``_load`` path — the gate the registry runs);
  2. be present in scripts/bringup_register_analysts.ANALYST_FILES;
  3. carry an identity.kind the runtime can actually dispatch.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from legba.data.schemas.analyst import AnalystDescriptor

_DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parents[2] / "descriptors"

_NEW_FILES = {
    "analyst_relationship_reifier.yaml": ("relationship_reifier", "relationship_reifier"),
    "analyst_structural_balance.yaml": ("structural_balance", "deterministic"),
    "analyst_graph_mining.yaml": ("graph_mining", "deterministic"),
    "analyst_nexus_decay.yaml": ("nexus_decay", "deterministic"),
}


def _load(name: str) -> AnalystDescriptor:
    """Exact mirror of scripts/bringup_register_analysts._load."""
    body = yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


@pytest.mark.parametrize("name,expected", sorted(_NEW_FILES.items()))
def test_piece_a_descriptor_validates(name: str, expected: tuple[str, str]):
    exp_id, exp_kind = expected
    desc = _load(name)
    assert desc.identity.id == exp_id
    assert desc.identity.kind == exp_kind


def test_piece_a_descriptors_in_bringup_set():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_bringup_register_analysts",
        pathlib.Path(__file__).resolve().parents[2]
        / "scripts"
        / "bringup_register_analysts.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in _NEW_FILES:
        assert name in mod.ANALYST_FILES, f"{name} missing from bringup ANALYST_FILES"


def test_reifier_is_meta_llm_kind():
    """The producer is a META analyst (single global sweep), LLM-bearing
    (typing), and dispatchable by discover_analyst_kinds."""
    desc = _load("analyst_relationship_reifier.yaml")
    assert desc.identity.kind == "relationship_reifier"
    # META: no targets block (single global sweep per tick).
    assert desc.subscription.targets is None
    assert desc.subscription.other_analysts == []
    # Declares an LLM block (the D2 8B typing path).
    assert desc.method.llm is not None
    # The kind discovers + dispatches.
    from legba.data.analysts import discover_analyst_kinds

    assert "relationship_reifier" in discover_analyst_kinds()


@pytest.mark.parametrize(
    "name,sub_handler",
    [
        ("analyst_structural_balance.yaml", "structural_balance"),
        ("analyst_graph_mining.yaml", "graph_mining"),
        ("analyst_nexus_decay.yaml", "nexus_decay"),
    ],
)
def test_consumer_is_registered_deterministic_sub_handler(name, sub_handler):
    """Each lit-up consumer routes through the deterministic dispatcher and is
    in SUB_HANDLERS (else it would be a descriptor pointing at nothing)."""
    desc = _load(name)
    assert desc.identity.kind == "deterministic"
    assert desc.method.sub_handler == sub_handler
    assert desc.subscription.other_analysts == []
    from legba.data.analysts.deterministic import SUB_HANDLERS

    assert sub_handler in SUB_HANDLERS


def test_piece_a_cadence_staggered():
    """The reifier must fire BEFORE its consumers within a tick, and the two
    decay sweeps must not collide on the same minute."""
    reifier = _load("analyst_relationship_reifier.yaml")
    balance = _load("analyst_structural_balance.yaml")
    mining = _load("analyst_graph_mining.yaml")
    nexus_d = _load("analyst_nexus_decay.yaml")
    fact_d = _load("analyst_fact_decay.yaml")
    schedules = {
        reifier.cadence.fallback_schedule,
        balance.cadence.fallback_schedule,
        mining.cadence.fallback_schedule,
    }
    assert len(schedules) == 3, "reifier + the two refinement consumers stagger"
    assert nexus_d.cadence.fallback_schedule != fact_d.cadence.fallback_schedule
