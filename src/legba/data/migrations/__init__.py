# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.migrations — CREATE-only Postgres migrations for L-001.

Migration policy per Lewis 2026-05-15 (LB-3 substrate clean restart):
**no migration of legacy data**, CREATE-only DDL, fresh start.

Files are plain SQL (`*.sql`), numbered sequentially. The `apply` runner in
this module discovers them in order and applies each in its own transaction.
Each migration registers itself in the `legba_data_migrations` ledger so
re-runs are idempotent.

Migration list (CREATE-only — see individual files for full DDL):
  0001  extensions + universal provenance template
  0002  core substrate tables (signals/events/entities/links + situations/
        hypotheses/predictions/proposed_edges/goals/sources/watchlist/users)
        — all with universal provenance columns; dead columns retired;
        5 zero-row tables omitted.
  0003  facts (attribute-half only; relationship-half retires to AGE per DM-2)
  0004  AGE setup (extension, graph, search_path); seed labels per
        `vocabulary.RELATIONSHIP_TYPES` + `ENTITY_CLASSES`.
  0005  runtime tables: analyst_traces, analyst_critiques, budget_ledger,
        graph_metrics (per L-090 §4.4 and L-107 §3).
  0006  descriptor registry tables (targets + analysts namespaces; versioning).
  0007  stack registry tables (typed kinds with discriminator).
  0008  conversion webhook registry table.
  0009  dead-letter tables (descriptor + output) + descriptor audit log
        + receipt-chain checkpoint table.
  0010  seed vocabulary rows (entity_classes + relationship_types).
  0011  stack credentials table.
  0012  analyst output tables (per-kind specializations).
  0013  conversion webhook runtime state.
  0014  source credibility registry (L-152).
  0015  cost model — `cost_estimate_usd` column on `budget_ledger` (L-245).
  0016  Wave B prereq #2 — assert + restate `confidence REAL NOT NULL
        DEFAULT 0.5` on signals/analyst_outputs/predictions.  Defense-in-
        depth at DB level alongside the Pydantic `SignalPayload.confidence
        = 0.5` default.
  0017  UI panel registrations table (L-192 / L-204 substrate side).
  0019  ISO 3166-1 country snapshot (L-181 substrate-cached list source).
  0020  L-200 / Wave D geopolitical vocabulary extension — 8 entity_classes
        + 10 relationship_types referenced by template_country.yaml +
        discovery_geopolitical_countries.yaml. Idempotent on conflict.
  0021+ source-first pivot era (see individual file headers). Note the
        CREATE-only policy above applied to the pre-pivot chain: 0024
        re-cuts the substrate (DROPs legacy sources/predictions,
        re-creates signals source-owned) and 0030 drops the dead 0002
        legacy tables (events/goals/watchlist/… — full list + kept-table
        rationale in its header). Clean-slate, no production data.
  0039  consult audit trail — consult_sessions (one per chat conversation /
        deep-consult task) + consult_turns (append-only user/assistant log with
        steps/tool_calls/cited_refs jsonb + finding_id). Backs the consult
        prior-chats + continue + deep-consult task-history surfaces.

The runner is intentionally minimal — alembic is *not* required for this
phase; SQL files keep the substrate factor explicit and reviewable. We can
move to alembic later if the migration set grows.
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR: Path = Path(__file__).parent

__all__ = ["MIGRATIONS_DIR"]
