# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-17 — register the P-11 seed action packs directly against the DB.

Direct-DB sibling of scripts/bringup_register_action_packs.py (which POSTs to
the REST registry).  This variant uses the same DescriptorRegistry path the
other P-17 bring-up scripts use so the whole working set lands in ONE database
(default ``legba_pivot_test``) in one pass, with deterministic DB selection.

Reuses the same P-11 seed descriptor YAMLs unchanged:
  * media_processing  (process_media → W2 job plane)
  * incident_response (escalate / create_incident → channels)
  * discovery         (discover_sources → W2 job plane)

Idempotent.  Each pack is validated against the real pydantic ActionPack schema
before it touches the DB.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _p17_registrar import (  # noqa: E402
    Family,
    RegisterResult,
    close_registry,
    open_registry,
    print_results,
    register_descriptor,
)

from legba.data.schemas.action_pack import ActionPack  # noqa: E402

DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parent.parent / "descriptors"

PACK_FILES = [
    "action_pack_media_processing.yaml",
    "action_pack_incident_response.yaml",
    "action_pack_substrate_read.yaml",
    "action_pack_escalate.yaml",
]


def _load(name: str) -> ActionPack:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return ActionPack.model_validate(body, strict=False)


async def main() -> int:
    pg, reg = await open_registry()
    try:
        results: list[RegisterResult] = []
        for fname in PACK_FILES:
            desc = _load(fname)
            results.append(
                await register_descriptor(pg, reg, family=Family.ACTION_PACK, descriptor=desc)
            )
        failures = print_results("Seed action packs:", results)
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
