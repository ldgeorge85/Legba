# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-17 — one-shot bring-up of the fresh source-first working set.

RETIRED (2026-07-02): DO NOT RUN. This LEGACY combined registrar registers
``country_assessor`` + ``country_critic`` (step 4 below) — both RETIRED live —
so executing it would RESURRECT dead analysts on the instance it targets. The
canonical, current bring-up is the phased ``deploy/deploy.sh`` (which uses
``bringup_register_g20_country_targets.py`` for targets and
``bringup_register_analysts.py`` for the live analyst set). This file is kept
only because ``tests/data_pkg/test_consult_target_seed_crosscheck.py`` AST-reads
its ``ANALYST_FILES`` literal (it never imports or runs it); the ``__main__``
entrypoint below hard-refuses to execute.

Populates a fresh instance (default DB ``legba_pivot_test``) in dependency
order, all in ONE process / ONE registry session:

  1. action packs   — media_processing / incident_response / discovery
                       (granted by analysts, allowed by targets).
  2. shared sources — source.bbc.world / source.aljazeera.world / source.dw.world
                       (the global feeds every target wires to by predicate).
  3. G20 targets    — country_g20_<iso2> x19 (geo-predicate source_selector +
                       per-country subscription + one inline analyst each).
  4. analysts       — country_assessor / country_critic / country_optimizer /
                       consult_default (action_packs grants, not tools_whitelist).

Order matters for the resolution legs: packs + sources must exist before the
targets/analysts that reference them resolve cleanly.  Every step is the same
idempotent register/update path; re-runs report ``unchanged``.

After this runs, a target's SourceRef resolves to the shared sources by
predicate (verify with scripts/verify_p17_resolution.py).

Override the target DB with LEGBA_DATA_PG_DB (defaults to legba_pivot_test).
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

import bringup_register_action_packs_db as packs_mod  # noqa: E402
import bringup_register_g20_country_targets as targets_mod  # noqa: E402

from legba.data.schemas.action_pack import ActionPack  # noqa: E402
from legba.data.schemas.analyst import AnalystDescriptor  # noqa: E402
from legba.data.schemas.source import SourceDescriptor  # noqa: E402

DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parent.parent / "descriptors"

SOURCE_FILES = ["source_bbc_world.yaml", "source_aljazeera_world.yaml", "source_dw_world.yaml"]
ANALYST_FILES = [
    "analyst_country_assessor.yaml",
    "analyst_country_critic.yaml",
    "analyst_country_optimizer.yaml",
    "analyst_consult_default.yaml",
]


def _load(name: str, cls):
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return cls.model_validate(body, strict=False)


async def main() -> int:
    pg, reg = await open_registry()
    total_failures = 0
    try:
        # 1. action packs
        results: list[RegisterResult] = []
        for fname in packs_mod.PACK_FILES:
            desc = _load(fname, ActionPack)
            results.append(await register_descriptor(pg, reg, family=Family.ACTION_PACK, descriptor=desc))
        total_failures += print_results("1. Seed action packs:", results)

        # 2. shared sources
        results = []
        for fname in SOURCE_FILES:
            desc = _load(fname, SourceDescriptor)
            results.append(await register_descriptor(pg, reg, family=Family.SOURCE, descriptor=desc))
        total_failures += print_results("2. Shared news sources:", results)

        # 3. G20 targets (synthesised from iso_countries)
        meta = await targets_mod._country_meta(pg)
        results = []
        for iso2 in targets_mod.G20_ISO2:
            m = meta.get(iso2, {"name": iso2, "languages": []})
            desc = targets_mod._build_target(iso2, m["name"], m["languages"])
            results.append(await register_descriptor(pg, reg, family=Family.TARGET, descriptor=desc))
        total_failures += print_results("3. G20 country targets:", results)

        # 4. analysts
        results = []
        for fname in ANALYST_FILES:
            desc = _load(fname, AnalystDescriptor)
            results.append(await register_descriptor(pg, reg, family=Family.ANALYST, descriptor=desc))
        total_failures += print_results("4. Source-first analyst set:", results)
    finally:
        await close_registry(pg, reg)

    print()
    if total_failures:
        print(f"DONE with {total_failures} failure(s).")
    else:
        print("DONE — fresh source-first working set registered cleanly.")
    return 1 if total_failures else 0


if __name__ == "__main__":
    # RETIRED — hard refuse. This legacy registrar would re-create the retired
    # country_assessor + country_critic. Use deploy/deploy.sh (which drives
    # bringup_register_g20_country_targets.py + bringup_register_analysts.py).
    print(
        "REFUSED: bringup_register_p17_workingset.py is RETIRED — it registers the "
        "retired country_assessor + country_critic and would resurrect dead "
        "analysts. Use deploy/deploy.sh (bringup_register_g20_country_targets.py + "
        "bringup_register_analysts.py) instead.",
        file=sys.stderr,
    )
    sys.exit(2)
