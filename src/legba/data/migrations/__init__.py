# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.migrations — CREATE-only Postgres migrations for L-001.

Migration policy per Lewis 2026-05-15 (LB-3 substrate clean restart):
**no migration of legacy data**, CREATE-only DDL, fresh start.

Files are plain SQL (`*.sql`): numbered `NNNN_slug.sql` up to
`LAST_NUMBERED`, TIME-NAMED `YYYYMMDDTHHMM_slug.sql` (UTC) after that — see
"Naming" below. The `apply` runner (`legba.data.migrate`) takes them in
`ordered_migration_files()` order and applies each in its own transaction.
Each migration registers itself in the `legba_data_migrations` ledger so
re-runs are idempotent.

Migration list (CREATE-only — see individual files for full DDL):
  0001  extensions + universal provenance template
  0002  core substrate tables (signals/events/entities/links + situations/
        hypotheses/predictions/proposed_edges/goals/sources/watchlist/users)
        — all with universal provenance columns; dead columns retired;
        5 zero-row tables omitted.
  0003  facts (attribute-half only; relationship-half retires to AGE)
  0004  AGE setup (extension, graph, search_path); seed labels per
        `vocabulary.RELATIONSHIP_TYPES` + `ENTITY_CLASSES`.
  0005  runtime tables: analyst_traces, analyst_critiques, budget_ledger,
        graph_metrics.
  0006  descriptor registry tables (targets + analysts namespaces; versioning).
  0007  stack registry tables (typed kinds with discriminator).
  0008  conversion webhook registry table.
  0009  dead-letter tables (descriptor + output) + descriptor audit log
        + receipt-chain checkpoint table.
  0010  seed vocabulary rows (entity_classes + relationship_types).
  0011  stack credentials table.
  0012  analyst output tables (per-kind specializations).
  0013  conversion webhook runtime state.
  0014  source credibility registry.
  0015  cost model — `cost_estimate_usd` column on `budget_ledger`.
  0016  assert + restate `confidence REAL NOT NULL
        DEFAULT 0.5` on signals/analyst_outputs/predictions.  Defense-in-
        depth at DB level alongside the Pydantic `SignalPayload.confidence
        = 0.5` default.
  0017  UI panel registrations table.
  0019  ISO 3166-1 country snapshot (substrate-cached list source).
  0020  geopolitical vocabulary extension — 8 entity_classes
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

import re
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR: Path = Path(__file__).parent

__all__ = [
    "LAST_NUMBERED",
    "MIGRATIONS_DIR",
    "MigrationNameError",
    "migration_sort_key",
    "new_migration_name",
    "ordered_migration_files",
]

# ---------------------------------------------------------------------------
# Naming (R-1): numbered names are closed; new migrations are TIME-NAMED.
# ---------------------------------------------------------------------------
#
# A counter ("the next free number") is shared state every parallel lane
# reads and writes, so two lanes take the same number and one renumbers after
# the other merges (two renumbers in the fortnight before this). A UTC
# timestamp plus a slug is chosen by each lane alone and cannot collide on
# merge: ``20261114T1232_claim_host_class.sql``.
#
# * Numbered names (``NNNN_slug.sql``) stay exactly as they are, applied in the
#   order they always were. No new one may be numbered above LAST_NUMBERED
#   (the numbers already in flight when the scheme changed).
# * Time-named names (``YYYYMMDDTHHMM_slug.sql``, UTC) sort AFTER every
#   numbered one (``0…`` < ``2…`` in name order, and the key below says so
#   explicitly), then by time, then by slug — two lanes that pick the same
#   minute still get a deterministic order and no collision.
# * The ledger (``legba_data_migrations``) records by NAME, so a new scheme
#   needs no ledger change, and applying twice applies nothing the second time.
# * ``scripts/new_migration.py <slug>`` writes a correctly named file.

#: The last counter number a migration may carry: the highest numbered file
#: present when time names began (0263-0268 were already taken: 0266 one
#: byte path, 0268 the desk-rows shadow).
#: Anything newer is time-named.
LAST_NUMBERED = 268

_NUMBERED = re.compile(r"^(\d{4})_([a-z0-9][a-z0-9_]*)\.sql$")
_TIMED = re.compile(r"^(\d{8}T\d{4})_([a-z0-9][a-z0-9_]*)\.sql$")
_TIME_FORMAT = "%Y%m%dT%H%M"


class MigrationNameError(ValueError):
    """A ``*.sql`` file in the migrations directory has a name the runner
    cannot order. Raised (every bad name at once) instead of skipping it: a
    skipped migration is a schema that silently never arrives."""


def migration_sort_key(name: str) -> tuple[int, str, str]:
    """``(0, number, slug)`` for a numbered name, ``(1, timestamp, slug)`` for a
    time-named one; :class:`MigrationNameError` for anything else."""
    m = _NUMBERED.match(name)
    if m:
        if int(m.group(1)) > LAST_NUMBERED:
            raise MigrationNameError(
                f"{name}: numbered migrations are closed above {LAST_NUMBERED:04d}; "
                "time-name it (scripts/new_migration.py <slug>)")
        return (0, m.group(1), m.group(2))
    m = _TIMED.match(name)
    if m:
        try:
            datetime.strptime(m.group(1), _TIME_FORMAT)
        except ValueError as exc:
            raise MigrationNameError(f"{name}: not a real UTC time ({exc})") from exc
        return (1, m.group(1), m.group(2))
    raise MigrationNameError(
        f"{name}: a migration is NNNN_slug.sql (closed) or YYYYMMDDTHHMM_slug.sql "
        "(UTC, lower_snake slug)")


def ordered_migration_files(directory: Path | None = None) -> list[Path]:
    """Every ``*.sql`` in ``directory`` in apply order; raises
    :class:`MigrationNameError` naming EVERY bad name (never skips one)."""
    directory = directory or MIGRATIONS_DIR
    keyed: list[tuple[tuple[int, str, str], Path]] = []
    bad: list[str] = []
    for path in directory.glob("*.sql"):
        try:
            keyed.append((migration_sort_key(path.name), path))
        except MigrationNameError as exc:
            bad.append(str(exc))
    numbers: dict[str, list[str]] = {}
    for key, path in keyed:
        if key[0] == 0:
            numbers.setdefault(key[1], []).append(path.name)
    bad += [f"two migrations share number {n}: {names}"
            for n, names in sorted(numbers.items()) if len(names) > 1]
    if bad:
        raise MigrationNameError("; ".join(sorted(bad)))
    return [path for _, path in sorted(keyed)]


def new_migration_name(slug: str, now: datetime | None = None) -> str:
    """The name for a new migration: ``YYYYMMDDTHHMM_<slug>.sql`` in UTC."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    name = f"{now.strftime(_TIME_FORMAT)}_{slug}.sql"
    migration_sort_key(name)  # validates the slug
    return name
