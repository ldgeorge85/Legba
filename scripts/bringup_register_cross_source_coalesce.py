# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Cross-Source Coalesce analyst descriptor (review P2).

Reads ``descriptors/analyst_cross_source_coalesce.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This is the OPT-IN bringup for the substrate-wide cross-source semantic/temporal
coalescing analyst — the review P2 (data-integrity) "cross-source semantic/
temporal coalescing not built" item. It is DELIBERATELY NOT in the default
``bringup_register_analysts.py`` set: the analyst is off-by-default (its handler's
``enabled`` option defaults False — mirroring ``signals_retention``), so an
operator must (1) run THIS script to register it, then (2) fire it with run
options ``{"enabled": true}`` to actually coalesce. It also requires the live
embedding service + Qdrant client on the rig; without them it refuses loud
(SEAM #19) rather than fabricating links.

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
        "analyst_cross_source_coalesce.yaml",
        "cross_source_coalesce",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())
