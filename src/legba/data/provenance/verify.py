# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verification machinery — operator + test-time sanity checks.

Two entry points:

  * ``verify_provenance_complete(conn, table, row_id)`` — does the row carry
    all required provenance fields? Returns a structured ``ProvenanceReport``
    rather than raising, so callers can decide policy (fail-fast in tests,
    surface in UI for operators).

  * ``validate_lineage(conn, table, row_id, max_depth=20)`` — walks the
    ``derived_from`` graph backward; detects cycles, dangling refs, missing
    ancestors; returns ``LineageReport``.

Both helpers route through the existing L-001 ``query_ancestors`` recursive
CTE where possible and use direct fetches for single-row checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from ._core import is_valid_schema_uri


_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _safe_table_name(name: str) -> bool:
    return bool(_TABLE_RE.match(name))


# ---------------------------------------------------------------------------
# verify_provenance_complete
# ---------------------------------------------------------------------------


@dataclass
class ProvenanceReport:
    row_id: UUID
    table: str
    ok: bool
    missing: list[str] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def issues(self) -> list[str]:
        return self.missing + self.malformed


# Required per row kind. A row is either source-kind (analyst_id NULL,
# run_id NULL) or analyst-kind (analyst_id NOT NULL, run_id NOT NULL).
# Both kinds require: schema_uri, produced_at, derived_from (may be empty),
# target_id+target_version (sources always carry these; analyst rows may
# carry NULL if the output is genuinely cross-target / target-less).
_ALWAYS_REQUIRED = ("schema_uri", "produced_at")


async def verify_provenance_complete(
    conn: asyncpg.Connection,
    table: str,
    row_id: UUID,
) -> ProvenanceReport:
    """Fetch the row and check its universal-provenance fields."""
    if not _safe_table_name(table):
        raise ValueError(f"unsafe table name: {table!r}")

    row = await conn.fetchrow(
        f"""
        SELECT id, target_id, target_version, analyst_id, analyst_version,
               produced_at, derived_from, schema_uri, run_id
        FROM {table}
        WHERE id = $1
        """,
        row_id,
    )
    if row is None:
        return ProvenanceReport(
            row_id=row_id,
            table=table,
            ok=False,
            missing=["row_not_found"],
        )

    raw = dict(row)
    missing: list[str] = []
    malformed: list[str] = []

    for col in _ALWAYS_REQUIRED:
        if raw.get(col) in (None, ""):
            missing.append(col)

    if raw.get("derived_from") is None:
        missing.append("derived_from")

    schema_uri = raw.get("schema_uri")
    if schema_uri and not is_valid_schema_uri(schema_uri):
        malformed.append(f"schema_uri:{schema_uri!r}")

    # Distinguish row kind. Analyst rows MUST carry analyst_version + run_id.
    analyst_id = raw.get("analyst_id")
    if analyst_id is not None:
        if not raw.get("analyst_version"):
            missing.append("analyst_version")
        if not raw.get("run_id"):
            missing.append("run_id")
    else:
        # Source-kind row: target_id + target_version must be present
        # (legacy back-tagged rows carry sentinel per DM-3, still non-null).
        if not raw.get("target_id"):
            missing.append("target_id")
        if not raw.get("target_version"):
            missing.append("target_version")

    produced_at = raw.get("produced_at")
    if produced_at is not None and not isinstance(produced_at, datetime):
        malformed.append("produced_at:not_datetime")

    derived_from = raw.get("derived_from")
    if derived_from is not None:
        if not isinstance(derived_from, (list, tuple)):
            malformed.append("derived_from:not_array")
        else:
            for i, item in enumerate(derived_from):
                if not isinstance(item, UUID):
                    malformed.append(f"derived_from[{i}]:not_uuid")
                    break

    return ProvenanceReport(
        row_id=row_id,
        table=table,
        ok=not (missing or malformed),
        missing=missing,
        malformed=malformed,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# validate_lineage
# ---------------------------------------------------------------------------


@dataclass
class LineageNode:
    row_id: UUID
    depth: int
    target_id: str | None
    analyst_id: str | None
    derived_from: list[UUID]


@dataclass
class LineageReport:
    root_id: UUID
    table: str
    nodes: list[LineageNode] = field(default_factory=list)
    cycles: list[list[UUID]] = field(default_factory=list)
    dangling: list[UUID] = field(default_factory=list)
    depth_exhausted: bool = False
    max_depth: int = 20

    @property
    def ok(self) -> bool:
        return not (self.cycles or self.dangling or self.depth_exhausted)


async def validate_lineage(
    conn: asyncpg.Connection,
    table: str,
    row_id: UUID,
    *,
    max_depth: int = 20,
) -> LineageReport:
    """Walk ``derived_from`` from row_id; detect cycles, missing ancestors.

    Uses an iterative BFS so cycle detection is structural rather than
    SQL-recursion-limit-based. Each row is fetched once; missing ancestors
    (derived_from references a non-existent id) become ``dangling``.
    """
    if not _safe_table_name(table):
        raise ValueError(f"unsafe table name: {table!r}")

    report = LineageReport(root_id=row_id, table=table, max_depth=max_depth)
    visited: set[UUID] = set()
    on_path: dict[UUID, list[UUID]] = {}     # row → path from root
    queue: list[tuple[UUID, int, list[UUID]]] = [(row_id, 0, [row_id])]

    while queue:
        current, depth, path = queue.pop(0)

        if depth > max_depth:
            report.depth_exhausted = True
            continue

        if current in visited:
            continue
        visited.add(current)

        row = await conn.fetchrow(
            f"""
            SELECT id, target_id, analyst_id, derived_from
            FROM {table}
            WHERE id = $1
            """,
            current,
        )
        if row is None:
            report.dangling.append(current)
            continue

        derived_from = list(row["derived_from"] or [])
        report.nodes.append(
            LineageNode(
                row_id=current,
                depth=depth,
                target_id=row["target_id"],
                analyst_id=row["analyst_id"],
                derived_from=derived_from,
            )
        )

        for ancestor in derived_from:
            if ancestor in path:
                # Cycle — record the offending path slice.
                cycle = path[path.index(ancestor):] + [ancestor]
                report.cycles.append(cycle)
                continue
            queue.append((ancestor, depth + 1, path + [ancestor]))

    return report
