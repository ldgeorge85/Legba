# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Chronicle assessor analyst descriptor (the public-record tier).

Reads ``descriptors/analyst_chronicle_assessor.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting (use the PUT /descriptors/analyst/chronicle_assessor route to
UPDATE an existing head — bringup POST is create-only).

This wires the ``chronicle_assessor`` analyst (build spec
planning/CHRONICLE_BUILD_2026-07-21.md): the THIRD id on the journal_assessor
kind. On its weekly cadence (Mondays 06:00 UTC) it GATHERs over the verified
tower top + the corpus, writes ONE detached third-person cited chronicle entry
into ``journal_entries`` (entry_kind='chronicle'), and the V1 faithfulness
verify grades it post-persist. NO publish edge exists — entries accumulate
internally; the Ghost sink is a deliberate later, human-gated addition.

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — resolved via scripts/_token.py (env → .env → dev).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bringup_http import (  # noqa: E402
    exists_head,
    load_yaml,
    registry_base,
    registry_client,
)
from _token import resolve_token  # noqa: E402


BASE = registry_base()
TOKEN = resolve_token()

# (family, file, descriptor_id)
TO_REGISTER = [
    (
        "analyst",
        "analyst_chronicle_assessor.yaml",
        "chronicle_assessor",
    ),
]


def main() -> int:
    with registry_client(BASE, TOKEN) as client:
        registered: list[tuple[str, str, str]] = []
        skipped: list[tuple[str, str]] = []
        failures: list[str] = []

        for family, fname, desc_id in TO_REGISTER:
            try:
                if exists_head(client, family, desc_id):
                    skipped.append((family, desc_id))
                    continue
            except Exception as exc:
                failures.append(f"{family}/{desc_id}: pre-check {exc}")
                continue

            body = load_yaml(fname)
            r = client.post(f"/descriptors/{family}", json=body)
            if r.status_code not in (200, 201):
                failures.append(
                    f"{family}/{desc_id}: HTTP {r.status_code} {r.text[:500]}"
                )
                continue
            out = r.json()
            registered.append((family, desc_id, out.get("version", "?")[:12]))

        print("Registered:")
        for f, d, v in registered:
            print(f"  + {f}/{d} @ {v}")
        print("Skipped (head already present):")
        for f, d in skipped:
            print(f"  = {f}/{d}")
        if failures:
            print("FAILURES:")
            for line in failures:
                print(f"  ! {line}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
