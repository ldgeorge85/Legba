# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Finding Supersession analyst descriptor (P-FS).

Reads ``descriptors/analyst_finding_supersession.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

P-FS ships finding-level dedup / supersession as a FIRST-CLASS ``deterministic``
analyst descriptor (PIVOT_BUILD_PLAN §12, W3) — not hidden substrate magic.
After this script runs the runtime's reconciler spins up a deterministic actor
that, on its cadence, clusters the shared finding pool by ``situation_signature``
and links near-duplicate re-assessments of the same situation: the newest
finding is canonical and older near-dups get ``analyst_outputs.superseded_by``
set + a ``finding_supersessions`` link row (older -> newer). NEVER destructive —
every finding row is preserved so the audit trail of how the assessment evolved
stays intact. This is the analysis-plane mechanism for the live
duplicate-findings problem (signal-level P-09 dedup does NOT cover it).

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
        "analyst_finding_supersession.yaml",
        "finding_supersession",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())
