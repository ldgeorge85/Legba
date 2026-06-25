#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 5c — register the thematic SITUATION targets.

Registers the first non-geo (thematic) targets — situation FRAMES whose
``scope.predicate`` (free-text ``contains_any``) focuses the analyst slice on a
cross-country situation no single G20 country target captures. Mirrors the G20
country-target registrar (``bringup_register_g20_country_targets.py``) but loads
the descriptor YAML and validates it against the real ``TargetDescriptor`` model
(``scope.domain == 'thematic'``).

Run via the test image against the live DB:
  docker run --rm --network host --entrypoint python3 -v $PWD:/app -w /app \\
    -e PYTHONPATH=/app/src:/install/lib/python3.11/site-packages \\
    -e LEGBA_DATA_PG_HOST=127.0.0.1 -e LEGBA_DATA_PG_USER=legba \\
    -e LEGBA_DATA_PG_PASSWORD=legba -e LEGBA_DATA_PG_DB=legba \\
    legba/legba-test:latest scripts/bringup_register_situation_targets.py
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

from legba.data.schemas.target import TargetDescriptor  # noqa: E402

DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parent.parent / "descriptors"

# The thematic situation targets to register (descriptor file stems).
SITUATION_TARGET_FILES = [
    "target_situation_iran_war.yaml",
]


def _load(name: str) -> TargetDescriptor:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return TargetDescriptor.model_validate(body, strict=False)


async def main() -> int:
    pg, reg = await open_registry()
    try:
        results: list[RegisterResult] = []
        for fname in SITUATION_TARGET_FILES:
            desc = _load(fname)
            results.append(
                await register_descriptor(pg, reg, family=Family.TARGET, descriptor=desc)
            )
        failures = print_results("Thematic situation targets:", results)
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
