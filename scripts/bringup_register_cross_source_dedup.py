# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Cross-Source Dedup analyst descriptor (P-09).

Reads ``descriptors/analyst_cross_source_dedup.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

P-09 ships cross-source dedup as a FIRST-CLASS ``deterministic`` analyst
descriptor (decision P-02) — not hidden substrate magic. After this script runs
the runtime's reconciler spins up a deterministic actor that sweeps the shared
raw signal pool, linking content-hash (and, when Qdrant is wired,
semantic-near) duplicates to a canonical via ``signal_aliases`` +
``signals.canonical_signal_id``. NEVER destructive — every raw row is kept.

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — defaults to "dev" (registry is in dev-mode
                                anonymous-bearer when LEGBA_REGISTRY_API_TOKEN
                                is unset, so any value works).
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
        "analyst_cross_source_dedup.yaml",
        "cross_source_dedup",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())
