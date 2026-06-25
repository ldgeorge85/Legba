# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed-list cross-check — the consult front door's target id must be one the
canonical p17 workingset bringup actually registers.

Guards the §1.1 drift class: ``consult_api`` hardcoded ``legba_consult_default``
while the live seed registered ``consult_default``, so ``POST /api/v1/consult``
404'd against every real deployment (the marquee governed consult, dead one hop
out). Same spirit as the boot-wiring test — assert the wiring the code *assumes*
is the wiring bringup *produces*, mechanically, so the two can't drift again.

Side-effect-free: parses ``ANALYST_FILES`` out of the bringup script via AST
(no import — the script pulls httpx/_token at module load) and reads the
descriptor yamls directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

from legba.data.registry.consult_api import CONSULT_ANALYST_ID

# tests/data_pkg/<file> -> repo root
_REPO = Path(__file__).resolve().parents[2]
_BRINGUP = _REPO / "scripts" / "bringup_register_p17_workingset.py"
_DESCRIPTORS = _REPO / "descriptors"


def _workingset_analyst_files() -> list[str]:
    """Extract the ``ANALYST_FILES`` list literal without importing the script."""
    tree = ast.parse(_BRINGUP.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "ANALYST_FILES" for t in node.targets
        ):
            return [ast.literal_eval(elt) for elt in node.value.elts]
    raise AssertionError(
        f"ANALYST_FILES not found in {_BRINGUP} — the cross-check can't run; "
        "the bringup's seed-list shape changed."
    )


def _descriptor_id(fname: str) -> str:
    doc = yaml.safe_load((_DESCRIPTORS / fname).read_text())
    return doc["identity"]["id"]


def test_consult_target_id_registered_by_workingset_bringup() -> None:
    registered = {_descriptor_id(f) for f in _workingset_analyst_files()}
    assert CONSULT_ANALYST_ID in registered, (
        f"consult_api targets {CONSULT_ANALYST_ID!r} but the canonical p17 "
        f"workingset bringup registers {sorted(registered)} — the consult "
        f"front door will 404 against the live seed. Repoint "
        f"consult_api.CONSULT_ANALYST_ID or add the descriptor to "
        f"bringup_register_p17_workingset.ANALYST_FILES."
    )
