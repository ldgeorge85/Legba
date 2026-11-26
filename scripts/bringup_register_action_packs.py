# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the seed action packs (media_processing / incident_response /
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
    # Journal Assessor — the journal's GOVERNED read pack
    # (ONE reused tool, list_findings). Granted ONLY to journal_assessor.
    ("action_pack", "action_pack_journal_read.yaml", "journal_read"),
    # Journal Assessor — the PROPOSE-AND-GATE write pack:
    # each tool writes ONLY a pending journal_proposals row, NEVER a live table.
    # Granted to BOTH journal tiers (journal_assessor + journal_consolidator).
    ("action_pack", "action_pack_journal_propose.yaml", "journal_propose"),
    # R-A (research program) — the OUTBOUND RESEARCH write path. One tool,
    # `web_evidence`, which searches the open web and LANDS what it keeps as
    # ordinary `signals` rows tagged with `retrieval_origin`. A separate pack
    # from `web_access` on purpose: web_access is read-only by contract and its
    # governor's `budget_account` is shared by every caller (the standing
    # auditor among them), so a research money cap could not live there.
    # Gated at runtime by LEGBA_RESEARCH_EVIDENCE (default `off` — the tool is
    # granted-but-refuses until an operator opens it).
    ("action_pack", "action_pack_research.yaml", "research"),
    # C-1b — consult presents a case: the nine read-only workstation actions an
    # armed consult turn may emit. Granted ONLY to consult_default.
    ("action_pack", "action_pack_workstation_drive.yaml", "workstation_drive"),
    # C-2b-1 — the consult write plane: five tools, thin clients of the
    # registry's one executor. Granted ONLY to consult_default; writes stay off
    # until the registry's LEGBA_CONSULT_WRITES is on.
    ("action_pack", "action_pack_consult_write.yaml", "consult_write"),
    # I-1 — the ingestion intake: an agent asks for something to be ingested
    # (one queue, the intake's admission control). Granted to NO analyst until
    # an operator adds the grant; refuses until LEGBA_INTAKE_AGENT_REQUESTS is on.
    ("action_pack", "action_pack_ingestion_intake.yaml", "ingestion_intake"),
    # Source drift: the versions of a cited page and what changed, a
    # re-read request, a re-derive request, the flag review. Granted to NO
    # analyst until an operator adds the grant.
    ("action_pack", "action_pack_evidence_drift.yaml", "evidence_drift"),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())
