# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Standing External Auditor analyst descriptor (D5).

Reads ``descriptors/analyst_standing_auditor.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting.

This ships the platform's only STANDING external check as a first-class
``deterministic`` analyst. ~84% of the fleet's LLM calls are the system watching
itself; nothing standing checks a TOP-LAYER claim against the world. After this
script runs (and the operator flips ``state`` to ``active``) the reconciler spins
up a deterministic actor that daily samples the world read plus a date-seeded
rotation of desk heads, checks 1-2 checkable world-claims from each against
EXTERNAL search through the ``web_access`` pack tool, and writes a SUPPORTED /
CONTRADICTED / NOT_FOUND critique per claim — plus a heartbeat row that makes the
auditor itself watchable.

PREREQUISITES (all DEPLOY steps, none of them this script's job):

  * the ``web_access`` ACTION PACK must be registered
    (``scripts/bringup_register_action_packs.py``) and its ``web_search``
    provider bound, or the auditor runs and reports an unaudited heartbeat;
  * a NEW deterministic sub-handler requires a REGISTRY REBUILD (the sub_handler
    dispatch table is compiled into the image) AND a runtime recreate to
    activate — see the memory feedback on shared-schema / new-sub-handler
    deploys;
  * migration 0091 (``alert_trigger_watermarks``) must be applied — it is, fleet
    wide, since the alert-trigger scan; the auditor rides it for its heartbeat
    rather than adding a table.

Registration is CREATE-ONLY. To change a live descriptor (e.g. widen
``method.options.max_desks``) PUT it instead:

  curl -X PUT "$LEGBA_REGISTRY_URL/analyst/standing_auditor" \\
       -H "Authorization: Bearer $LEGBA_REGISTRY_TOKEN" \\
       -H 'Content-Type: application/json' \\
       --data-binary @<(python3 -c 'import json,sys,yaml; \\
           json.dump(yaml.safe_load(open(sys.argv[1])), sys.stdout)' \\
           descriptors/analyst_standing_auditor.yaml)

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
    ("analyst", "analyst_standing_auditor.yaml", "standing_auditor"),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())
