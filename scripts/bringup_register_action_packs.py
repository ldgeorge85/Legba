# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the P-11 seed action packs (media_processing / incident_response /
discovery).

Reads the ``descriptors/action_pack_*.yaml`` seed packs and POSTs them to the
registry's ``action_pack`` family. Idempotent: a pack whose head row already
exists is reported + skipped (exit 0).

After this runs, an analyst that GRANTS one of these packs (``action_packs:``)
AND whose target ALLOWS it (``allowed_action_packs:``), where the pack is
APPLICABLE (scope-tag / predicate), can invoke its tools through
``legba.data.analysts.agency.Agency.run_pack_tool`` — gated by the pack's
governor over the ``action_pack_invocations`` ledger (migration 0025).

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — defaults via scripts/_token.resolve_token().
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
    ("action_pack", "action_pack_media_processing.yaml", "media_processing"),
    ("action_pack", "action_pack_incident_response.yaml", "incident_response"),
    ("action_pack", "action_pack_substrate_read.yaml", "substrate_read"),
    ("action_pack", "action_pack_escalate.yaml", "escalate_finding"),
    # S6 — external evidence + operator-gated write-back packs.
    ("action_pack", "action_pack_web_access.yaml", "web_access"),
    ("action_pack", "action_pack_propose_facts.yaml", "propose_facts"),
    # Journal Assessor (plan §5 / §12 Wave 0) — the journal's GOVERNED read pack
    # (ONE reused tool, list_findings). Granted ONLY to journal_assessor (§7.6).
    ("action_pack", "action_pack_journal_read.yaml", "journal_read"),
    # Journal Assessor (plan §7 / §12 Wave 4) — the PROPOSE-AND-GATE write pack:
    # each tool writes ONLY a pending journal_proposals row, NEVER a live table.
    # Granted to BOTH journal tiers (journal_assessor + journal_consolidator).
    ("action_pack", "action_pack_journal_propose.yaml", "journal_propose"),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())
