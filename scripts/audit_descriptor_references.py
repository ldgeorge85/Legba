#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-3 — resolve every string reference in the LIVE registry against the code.

The descriptor drift guard in
``tests/data_pkg/test_descriptor_reference_resolution_k3.py`` checks
``descriptors/*.yaml``. That is the right thing for CI, and it is not the same
question as this one: **the yaml files are the intent, the database rows are
what runs.** A descriptor edited through the API, a promotion that rewrote
``method.prompt_module``, or a row registered before a module was renamed can
all leave the DB naming something the tree does not have while every yaml file
in the repo is perfectly clean.

Read-only. Never writes, never re-POSTs, never touches the registry API — it
opens the substrate pool, reads ``is_head`` rows, and resolves. Exits non-zero
when any reference on a binding-state descriptor fails to resolve, so it can
gate a deploy.

This is the check ``planning/CODE_CLEANUP_ANALYSIS_2026-08-02.md`` §5 Phase 3
specifies as the verification for any rename wave: *"a descriptor-integrity
check that every impl / prompt_module / *_type string in the database (not just
the yaml files) resolves to a real module — run against the live registry,
failing loud."*

Run INSIDE the runtime container so the resolver imports the DEPLOYED package
rather than a checkout — resolving against a different tree than production
runs is the one way this script can lie::

    docker run --rm --network legba_default \\
      -e LEGBA_DATA_PG_HOST=legba-postgres-1 -e LEGBA_DATA_PG_DB=legba \\
      -e LEGBA_DATA_PG_USER=legba -e LEGBA_DATA_PG_PASSWORD=legba \\
      -v "$(pwd)":/work -w /work --entrypoint python \\
      legba/legba-runtime-dapr:latest /work/scripts/audit_descriptor_references.py

Options:
  --all-states   include draft / configured / retired rows (reported, not fatal)
  --json         emit machine-readable output for a dashboard or a diff
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys


logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("audit_descriptor_references")
log.setLevel(logging.INFO)


#: Only these states mean the runtime will bind the descriptor and follow its
#: references. Mirrors ``DescriptorRegistry._BINDING_STATES`` — a retired
#: analyst naming a deleted module is not a defect.
BINDING_STATES = ("active", "paused")

#: (table, family) pairs. ``target_descriptors`` and ``wiring_descriptors`` are
#: absent deliberately: targets carry no code-naming strings (their vocabulary
#: references are validated separately against the vocab cache), and
#: ``wiring_descriptors`` has no model, no route and no writer.
_TABLES: tuple[tuple[str, str], ...] = (
    ("analyst_descriptors", "analyst"),
    ("source_descriptors", "source"),
    ("action_pack_descriptors", "action_pack"),
)


async def _load_rows(conn, table: str, family: str) -> list[dict]:
    has_kind = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = $1 AND column_name = 'kind')",
        table,
    )
    kind_expr = "kind" if has_kind else "NULL::text"
    rows = await conn.fetch(
        f"SELECT descriptor_id, version, state, {kind_expr} AS kind, body "
        f"FROM {table} WHERE is_head"
    )
    out = []
    for r in rows:
        body = r["body"]
        if isinstance(body, str):
            body = json.loads(body)
        out.append({
            "family": family,
            "descriptor_id": r["descriptor_id"],
            "version": r["version"],
            "state": r["state"],
            "kind": r["kind"],
            "body": body or {},
        })
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all-states", action="store_true",
                    help="audit every state, not just active/paused")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    # Deferred imports — the deployed (baked) legba package inside the image.
    from legba.data.config import PostgresConfig
    from legba.data.postgres import PostgresStore
    from legba.data.registry.descriptor_refs import (
        ReferenceStatus, audit_references, extract_references,
    )

    store = PostgresStore(PostgresConfig.from_env())
    await store.connect()
    try:
        async with store.pool.acquire() as conn:
            rows: list[dict] = []
            for table, family in _TABLES:
                rows.extend(await _load_rows(conn, table, family))
    finally:
        await store.close()

    refs = []
    for row in rows:
        if not args.all_states and row["state"] not in BINDING_STATES:
            continue
        refs.extend(extract_references(
            family=row["family"],
            descriptor_id=row["descriptor_id"],
            version=row["version"],
            state=row["state"],
            body=row["body"],
            kind=row["kind"],
        ))

    resolutions = audit_references(refs)
    failures = [r for r in resolutions if r.failing]
    implicit = [r for r in resolutions if r.status is ReferenceStatus.IMPLICIT]

    if args.json:
        print(json.dumps({
            "descriptors": len(rows),
            "references": len(resolutions),
            "ok": sum(1 for r in resolutions if r.status is ReferenceStatus.OK),
            "implicit": [r.as_line() for r in implicit],
            "failures": [r.as_line() for r in failures],
        }, indent=2))
    else:
        by_type: dict[str, dict[str, int]] = {}
        for r in resolutions:
            by_type.setdefault(r.reference.ref_type.value, {}).setdefault(
                r.status.value, 0)
            by_type[r.reference.ref_type.value][r.status.value] += 1

        log.info("descriptors read: %d   references resolved: %d",
                 len(rows), len(resolutions))
        for ref_type in sorted(by_type):
            log.info("  %-22s %s", ref_type, by_type[ref_type])

        if implicit:
            log.warning(
                "%d reference(s) bind ONLY via the dapr_actors identity.id "
                "fallback — they work today, and renaming the descriptor id "
                "unbinds them with no error:", len(implicit),
            )
            for r in implicit:
                log.warning("  %s", r.as_line())

        if failures:
            log.error("%d UNRESOLVED reference(s):", len(failures))
            for r in failures:
                log.error("  %s", r.as_line())
        else:
            log.info("every reference resolves.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
