# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.seed.adapters — concrete SeedSource adapters.

  * ``world_baseline`` — the curated-YAML proof adapter (zero network dep).
  * ``wikidata_leaders`` — Wikidata SPARQL: current heads of state/government
    → ``LeaderOf`` facts + alliance ``MemberOf`` signed nexuses.
  * ``acled_conflict`` — ACLED bulk conflict-events → conflict-event facts +
    signed ``HostileTo`` nexuses.
  * ``sipri_arms_transfers`` — curated-YAML SIPRI arms transfers → signed
    ``ArmsTransferTo`` nexuses (supplier→recipient, polarity +1 / supportive).

The UCDP / World Bank tiers (planning/SEEDING_SKETCH.md) grow in here as new
modules, each registered in :data:`legba.data.seed.ADAPTERS`.
"""

from __future__ import annotations

from .acled_conflict import ACLEDConflictSeedSource
from .sipri_arms_transfers import SIPRIArmsTransfersSeedSource
from .wikidata_leaders import WikidataLeadersSeedSource
from .world_baseline import WorldBaselineSeedSource

__all__ = [
    "WorldBaselineSeedSource",
    "WikidataLeadersSeedSource",
    "ACLEDConflictSeedSource",
    "SIPRIArmsTransfersSeedSource",
]
