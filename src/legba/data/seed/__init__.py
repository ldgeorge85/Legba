# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.seed — curated/authoritative seeding ROOTS (flavor b).

Public surface:

  * The framework primitives (``SeedSource`` protocol + typed
    ``SeedFact`` / ``SeedEntity`` / ``SeedNexus`` payloads + ``SeedContext``)
    — :mod:`legba.data.seed._base`.
  * The driver (``run_seed_source`` + ``SeedRunResult``) that resolves
    entities + writes via ``write_fact`` / ``write_nexus`` + records the
    ``seed_batches`` row — :mod:`legba.data.seed._driver`.
  * An adapter REGISTRY keyed by ``name`` so the ``scripts/seed.py`` CLI can
    ``--list`` and ``--source <name>``.

Adding an adapter = implement :class:`SeedSource` and register it in
``ADAPTERS`` below. Live tiers: the curated-YAML ``world_baseline`` +
``sipri_arms_transfers`` adapters, the Wikidata SPARQL ``wikidata_leaders``
adapter, and the ACLED ``acled_conflict`` backfill (the UCDP / World Bank tiers
grow in later).
"""

from __future__ import annotations

from ._base import (
    SeedContext,
    SeedEntity,
    SeedFact,
    SeedNexus,
    SeedPayload,
    SeedSource,
)
from ._driver import SeedRunResult, run_seed_source
from .manual_schema import (
    BatchManifest,
    BatchMode,
    BatchValidationError,
    ManualDocRecord,
    ManualEntityRecord,
    ManualFactRecord,
    ManualNexusRecord,
    ManualSignalRecord,
    ProvenanceTier,
    RecordError,
    ValidatedBatch,
    load_manifest,
    validate_batch,
)
from .manual_batch import (
    ManualBatchReport,
    ManualBatchSeedSource,
    PriorFact,
    PriorNexus,
    RecordAction,
    classify_fact,
    classify_nexus,
    manual_source_id,
    run_manual_batch,
    signal_from_record,
)
from .adapters.acled_conflict import ACLEDConflictSeedSource
from .adapters.sipri_arms_transfers import SIPRIArmsTransfersSeedSource
from .adapters.wikidata_leaders import WikidataLeadersSeedSource
from .adapters.world_baseline import WorldBaselineSeedSource


def _build_registry() -> dict[str, SeedSource]:
    sources: list[SeedSource] = [
        WorldBaselineSeedSource(),
        WikidataLeadersSeedSource(),
        ACLEDConflictSeedSource(),
        SIPRIArmsTransfersSeedSource(),
    ]
    return {s.name: s for s in sources}


#: name → adapter instance. The CLI reads this.
ADAPTERS: dict[str, SeedSource] = _build_registry()


def get_adapter(name: str) -> SeedSource:
    try:
        return ADAPTERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(ADAPTERS)) or "(none)"
        raise KeyError(f"unknown seed source {name!r}; known: {known}") from exc


def list_adapters() -> list[tuple[str, str]]:
    """Return ``[(name, source_type), …]`` for ``--list``."""
    return sorted((s.name, s.source_type) for s in ADAPTERS.values())


__all__ = [
    "SeedContext",
    "SeedEntity",
    "SeedFact",
    "SeedNexus",
    "SeedPayload",
    "SeedSource",
    "SeedRunResult",
    "run_seed_source",
    # manual-ingest batch format (S4-T1)
    "BatchManifest",
    "BatchMode",
    "BatchValidationError",
    "ManualDocRecord",
    "ManualEntityRecord",
    "ManualFactRecord",
    "ManualNexusRecord",
    "ManualSignalRecord",
    "ProvenanceTier",
    "RecordError",
    "ValidatedBatch",
    "load_manifest",
    "validate_batch",
    # manual-ingest batch loader (S4-T2)
    "ManualBatchReport",
    "ManualBatchSeedSource",
    "PriorFact",
    "PriorNexus",
    "RecordAction",
    "classify_fact",
    "classify_nexus",
    "run_manual_batch",
    # manual-ingest signals backfill lane (S4-T4)
    "manual_source_id",
    "signal_from_record",
    "ADAPTERS",
    "get_adapter",
    "list_adapters",
    "WorldBaselineSeedSource",
    "WikidataLeadersSeedSource",
    "ACLEDConflictSeedSource",
    "SIPRIArmsTransfersSeedSource",
]
