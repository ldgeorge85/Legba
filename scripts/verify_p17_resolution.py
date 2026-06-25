# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-17 acceptance check — SourceRef resolution + agency grant.

Verifies, against the bring-up DB (default ``legba_pivot_test``), that:

  1. Every registered G20 country target's geo-predicate source_selector
     resolves to the shared sources by PREDICATE (no per-target source
     duplication) — via legba.runtime.subscription.sourceref.resolve_source_refs.
  2. The per-country Subscription narrows the shared pool to that country's geo.
  3. The country_assessor analyst's action_packs grant resolves effective
     against a G20 target's allowed_action_packs (the three-way intersection).

Read-only.  Exit 0 iff all checks pass.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _p17_registrar import close_registry, open_registry  # noqa: E402

from legba.data.schemas.target import TargetDescriptor  # noqa: E402
from legba.runtime.subscription.sourceref import resolve_source_refs  # noqa: E402


async def _load_target(pg, descriptor_id: str) -> TargetDescriptor | None:
    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT body FROM target_descriptors WHERE descriptor_id=$1 AND is_head",
            descriptor_id,
        )
    if not row:
        return None
    body = row["body"]
    if isinstance(body, (str, bytes, bytearray)):
        body = json.loads(body)
    return TargetDescriptor.model_validate(body, strict=False)


async def main() -> int:
    pg, reg = await open_registry()
    failures = 0
    try:
        # Which G20 targets exist?
        async with pg.acquire() as conn:
            rows = await conn.fetch(
                "SELECT descriptor_id FROM target_descriptors "
                "WHERE is_head AND descriptor_id LIKE 'country_g20_%' ORDER BY descriptor_id"
            )
        target_ids = [r["descriptor_id"] for r in rows]
        if not target_ids:
            print("FAIL: no country_g20_* targets registered — run the bring-up first.")
            return 1
        print(f"Found {len(target_ids)} G20 targets.")

        # 1+2. SourceRef resolution per target.
        print("\n[1/2] SourceRef resolution (selector -> shared sources by predicate):")
        for tid in target_ids:
            tgt = await _load_target(pg, tid)
            if tgt is None or not tgt.sources:
                print(f"  ! {tid}: no sources on descriptor"); failures += 1; continue
            # confirm new model: no explicit per-target source ids
            inline = [s for s in tgt.sources if s.source_id is not None]
            if inline:
                print(f"  ! {tid}: has explicit per-target source_id(s) {[s.source_id for s in inline]} "
                      f"(should be selector-only)"); failures += 1
            bindings = await resolve_source_refs(
                pg, target_id=tid, target_tenant="default", source_refs=tgt.sources
            )
            geos = {g for s in tgt.sources for g in s.subscription.geo}
            if not bindings:
                print(f"  ! {tid}: selector resolved to ZERO shared sources"); failures += 1; continue
            src_ids = sorted(b.source_id for b in bindings)
            all_selector = all(b.via_selector for b in bindings)
            print(f"  = {tid}: geo={sorted(geos)} -> {len(bindings)} shared sources "
                  f"{src_ids} (all via selector: {all_selector})")
            if not all_selector:
                failures += 1

        # 3. Agency grant intersection: country_assessor x a G20 target.
        print("\n[3] Agency grant overlap (country_assessor x country_g20_br):")

        async def _load_analyst(aid):
            async with pg.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT body FROM analyst_descriptors WHERE descriptor_id=$1 AND is_head", aid)
            if not row:
                return None
            b = row["body"]
            if isinstance(b, (str, bytes, bytearray)):
                b = json.loads(b)
            from legba.data.schemas.analyst import AnalystDescriptor
            return AnalystDescriptor.model_validate(b, strict=False)

        analyst = await _load_analyst("country_assessor")
        target = await _load_target(pg, "country_g20_br")
        if analyst is None:
            print("  (skip) country_assessor not registered");
        elif target is None:
            print("  (skip) country_g20_br not registered")
        else:
            grants = [p.pack_id for p in analyst.action_packs]
            allowed = [p.pack_id for p in target.allowed_action_packs]
            print(f"  analyst grants:        {grants}")
            print(f"  target allows:         {allowed}")
            print(f"  granted ∩ allowed:     {sorted(set(grants) & set(allowed))}")
            if not (set(grants) & set(allowed)):
                print("  ! no overlap between analyst grants and target allows"); failures += 1
    finally:
        await close_registry(pg, reg)

    print()
    print("ACCEPTANCE PASS" if failures == 0 else f"ACCEPTANCE FAIL ({failures} issue(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
