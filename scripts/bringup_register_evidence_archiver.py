# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Evidence Archiver analyst descriptor.

Reads ``descriptors/analyst_evidence_archiver.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting.

This wires the ``evidence_archiver`` deterministic sweep as a DRAFT analyst
(P2-1 cited-evidence archival — program §A3). Once the operator activates it,
the runtime's reconciler spins up a deterministic actor that, on its cadence,
archives the next budgeted batch of signals CITED by verified findings:
original bytes fetched (SSRF egress guard + the P2-2 license gate), stored
content-addressed at ``{LEGBA_ARCHIVE_ROOT}/{sha256[:2]}/{sha256}``, the
``evidence_archive`` sidecar upserted, and ``signals.object_ref`` stamped with
``cas:sha256/<hex>`` so the receipt chain terminates in OUR verifiable copy.

PREREQUISITES (before activating the draft):
  1. Migration 0104_evidence_archive.sql applied
     (``docker exec legba-registry-1 python -m legba.data.migrate`` — verify
     head in db ``legba``).
  2. The ``legba_archive`` volume mounted on legba-runtime-dapr (compose) and
     ``LEGBA_ARCHIVE_ROOT`` resolvable/writable in-container.

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
        "analyst_evidence_archiver.yaml",
        "evidence_archiver",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())
