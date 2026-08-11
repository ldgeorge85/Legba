# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Situation Clustering analyst descriptor.

Reads ``descriptors/analyst_situation_clustering.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

Situation clustering is the producer that materializes the ``situations`` table —
the last dark leg of the analysis plane. The situations read API, the lineage
walk, and the STIX incident producer all already consumed it, but nothing wrote
it, so it sat at 0 rows. This deterministic analyst reads the
``situation_signature``-stamped findings (stamped by finding_supersession) and
upserts one ``situations`` row per signature.

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — resolved via scripts/_token.py (env → .env → dev).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bringup_http import register_create_only, registry_base  # noqa: E402
from _token import resolve_token  # noqa: E402


BASE = registry_base()
TOKEN = resolve_token()

# (family, file, descriptor_id)
TO_REGISTER = [
    (
        "analyst",
        "analyst_situation_clustering.yaml",
        "situation_clustering",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())
