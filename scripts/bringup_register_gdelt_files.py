# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the GDELT 2.0 15-minute file-dump source descriptor.

Reads ``descriptors/source_gdelt_files.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting (use the PUT /descriptors/source/{id} route to UPDATE an existing
head — bringup POST is create-only).

This registers ``source.gdelt.files`` (kind ``gdelt_files``, handler
``src/legba/data/sources/gdelt_files.py``) — the replacement GDELT
acquisition path after ``source.gdelt.doc_api`` (the keyless DOC 2.0 API)
started 429-ing at the IP level even for small, spaced queries (verified
2026-07-21). The file dump has no API and no rate limit: it polls
``http://data.gdeltproject.org/gdeltv2/lastupdate.txt`` on a 15-minute
cadence and fetches the events-export CSV zip directly.

The descriptor ships ``state: draft`` (S-2 convention) — this script
registers it but does NOT activate it. An operator reviews the filter
thresholds and flips ``configured`` -> ``active`` deliberately (see the
OPERATOR FLIP comment block at the top of the descriptor YAML).

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
        "source",
        "source_gdelt_files.yaml",
        "source.gdelt.files",
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

        if registered:
            print(
                "\nNOTE: source.gdelt.files registers as `draft` (deliberately "
                "not active). Review the filter thresholds in "
                "descriptors/source_gdelt_files.yaml, then flip it live via:\n"
                "  POST {registry}/descriptors/source/source.gdelt.files/transition "
                '{"to_state": "configured"}\n'
                "  POST {registry}/descriptors/source/source.gdelt.files/transition "
                '{"to_state": "active"}'
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
