# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.provenance — universal provenance helpers + substrate write
wrappers (L-001 + L-114).

This package re-exports the L-001 read/build helpers (`from_target`,
`from_analyst`, `query_ancestors`, `compute_receipt_hash`, …) plus adds the
L-114 write-side machinery:

  * `writes.write_analyst_output`       — generic analyst-output wrapper
  * `writes.write_finding` …            — per-output-kind specializations
  * `receipts.RuntimeReceiptChain`      — per-analyst prev-receipt tracker
  * `checkpointer.AuditCheckpointer`    — Ed25519-signed checkpoint loop
  * `verify.verify_provenance_complete` — single-row provenance checker
  * `verify.validate_lineage`           — derived_from walker w/ cycle detect
  * `budget.compute_cost_usd`           — L-245 provider-agnostic $ cost
  * `budget.record_budget`              — L-245 budget_ledger upsert helper

The split:

  * `_core`         — L-001 row builders, lineage queries, schema-URI parse,
                      canonical-JSON, receipt-hash function.
  * `kinds`         — kind registry (output kind → table + pydantic model
                      + schema_uri + NATS subject).
  * `models`        — pydantic payload models per output kind.
  * `writes`        — public write wrappers (signals + analyst outputs).
  * `dlq`           — output_dead_letter row construction + insert.
  * `receipts`      — chain-state per (analyst_id, run kind).
  * `checkpointer`  — per-minute asyncio task that signs the chain head.
  * `verify`        — operator-facing verifier helpers.
"""

from __future__ import annotations

# Re-export L-001 surface unchanged.
from ._core import (
    AnalystContext,
    LEGACY_TARGET_SENTINEL,
    ProvenanceFields,
    SchemaUri,
    TargetContext,
    ZERO_HASH,
    append_derived_from,
    canonical_json,
    compute_receipt_hash,
    from_analyst,
    from_target,
    is_valid_schema_uri,
    legacy_provenance,
    parse_schema_uri,
    query_ancestors,
    query_descendants,
    sha256_canonical,
)

# L-114 surface.
from .kinds import (
    OutputKind,
    OutputKindSpec,
    KIND_REGISTRY,
    spec_for_kind,
    register_kind,
)
from .models import (
    SignalPayload,
    FactPayload,
    NexusPayload,
    FindingPayload,
    SituationPayload,
    HypothesisPayload,
    PredictionPayload,
    AlertPayload,
    MetaFindingPayload,
    CritiquePayload,
    ConsultResponsePayload,
    PromptModuleCandidatePayload,
    JournalPayload,
    JournalClaim,
)
from .writes import (
    SignalRow,
    OutputRow,
    write_analyst_output,
    write_finding,
    write_situation,
    write_hypothesis,
    write_prediction,
    write_alert,
    write_meta_finding,
    write_critique,
    write_fact,
    write_nexus,
    write_journal,
    supersede_prior_consolidation,
)
from .budget import BudgetLedgerRow, compute_cost_usd, record_budget
from .dlq import OutputDeadLetterEntry, route_to_output_dead_letter
from .receipts import RuntimeReceiptChain
from .checkpointer import (
    AuditCheckpointer,
    CheckpointerConfig,
    Ed25519Signer,
)
from .verify import (
    ProvenanceReport,
    LineageReport,
    LineageNode,
    verify_provenance_complete,
    validate_lineage,
)

__all__ = [
    # L-001 — context + row construction
    "TargetContext",
    "AnalystContext",
    "ProvenanceFields",
    "SchemaUri",
    "parse_schema_uri",
    "is_valid_schema_uri",
    "from_target",
    "from_analyst",
    "legacy_provenance",
    "LEGACY_TARGET_SENTINEL",
    "append_derived_from",
    "query_ancestors",
    "query_descendants",
    "canonical_json",
    "sha256_canonical",
    "compute_receipt_hash",
    "ZERO_HASH",
    # L-114 — kinds + payload models
    "OutputKind",
    "OutputKindSpec",
    "KIND_REGISTRY",
    "spec_for_kind",
    "register_kind",
    "SignalPayload",
    "FactPayload",
    "NexusPayload",
    "FindingPayload",
    "SituationPayload",
    "HypothesisPayload",
    "PredictionPayload",
    "AlertPayload",
    "MetaFindingPayload",
    "CritiquePayload",
    "ConsultResponsePayload",
    "PromptModuleCandidatePayload",
    "JournalPayload",
    "JournalClaim",
    # L-114 — write wrappers
    "SignalRow",
    "OutputRow",
    "write_analyst_output",
    "write_finding",
    "write_situation",
    "write_hypothesis",
    "write_prediction",
    "write_alert",
    "write_meta_finding",
    "write_critique",
    "write_fact",
    "write_nexus",
    "write_journal",
    "supersede_prior_consolidation",
    # L-114 — DLQ
    "OutputDeadLetterEntry",
    "route_to_output_dead_letter",
    # L-245 — budget_ledger cost-model writer
    "BudgetLedgerRow",
    "compute_cost_usd",
    "record_budget",
    # L-114 — receipt chain + checkpointer
    "RuntimeReceiptChain",
    "AuditCheckpointer",
    "CheckpointerConfig",
    "Ed25519Signer",
    # L-114 — verifier
    "ProvenanceReport",
    "LineageReport",
    "LineageNode",
    "verify_provenance_complete",
    "validate_lineage",
]
