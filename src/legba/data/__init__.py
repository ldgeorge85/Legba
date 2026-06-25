# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data — Legba data substrate package (L-001).

This package owns the redesigned substrate for the descriptor/registry/runtime
model described in `design/legba_topology_redesign.md` v2.

Provides:
  * `legba.data.config`            — typed env configuration per store.
  * `legba.data.postgres`          — async asyncpg pool with AGE-aware Cypher helper.
  * `legba.data.qdrant`            — Qdrant wrapper (only `legba_signals` survives).
  * `legba.data.redis`             — redis-py async client.
  * `legba.data.nats`              — nats-py async client + JetStream helper.
  * `legba.data.provenance`        — provenance row construction, lineage
                                     helpers, Iglu URI helpers, receipt hashing.
  * `legba.data.migrations`        — CREATE-only SQL migrations (L-090 schema).
  * `legba.data.schemas`           — vendored pydantic descriptor schemas (L-101).
  * `legba.data.smoke`             — sanity verification utilities.

Phase 1 task L-001. See:
  * /usr/local/deployments/myshit/personal/plans/legba_execution_plan.md
  * /usr/local/deployments/myshit/personal/plans/design/legba_data_mapping.md
  * /usr/local/deployments/myshit/personal/plans/design/legba_storage_layout.md
  * /usr/local/deployments/myshit/personal/plans/design/legba_descriptor_schemas.md
  * /usr/local/deployments/myshit/personal/plans/design/legba_observability.md
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "__version__",
]
