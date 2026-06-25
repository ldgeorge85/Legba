# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.provenance — universal provenance helpers.

Per `design/legba_observability.md` §1 (universal row fields) + §7 (receipt
chain alignment with Mnemosyne D5).

Three concerns covered here:

  1. Construct provenance row fields from a target or analyst context.
  2. `derived_from` array helpers (append, query lineage).
  3. Iglu schema URI helpers (parse, validate, build).
  4. Receipt-hash helpers (canonical-JSON + SHA-256).

Importable both by the runtime (when it lands per L-002 / L-110) and by
substrate-write callers (current and future).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID, uuid4

import asyncpg


# ---------------------------------------------------------------------------
# Context types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetContext:
    """The (target_id, target_version) pair for a row produced by a target pipeline."""

    target_id: str
    target_version: str            # content-hash of target descriptor


@dataclass(frozen=True)
class AnalystContext:
    """The (analyst_id, analyst_version) pair plus the run id."""

    analyst_id: str
    analyst_version: str           # content-hash of analyst descriptor
    run_id: UUID
    target_id: str | None = None
    target_version: str | None = None


# ---------------------------------------------------------------------------
# Iglu URI helpers
# ---------------------------------------------------------------------------

# Substrate-row form: 'iglu:legba/<entity>/jsonschema/<major>-<minor>-<patch>'.
# Descriptor form (per L-101 §1): 'legba/<family>/<major>.<minor>.<patch>'.
# Both supported.

_IGLU_RE = re.compile(
    r"^iglu:legba/(?P<family>[a-z_]+(?:/[a-z_]+)?)/jsonschema/"
    r"(?P<major>\d+)-(?P<minor>\d+)-(?P<patch>\d+)$"
)
_BARE_RE = re.compile(
    r"^legba/(?P<family>[a-z_]+(?:/[a-z_]+)?)/"
    r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$"
)


@dataclass(frozen=True)
class SchemaUri:
    family: str
    major: int
    minor: int
    patch: int
    form: str = "iglu"  # "iglu" or "bare"

    def render(self) -> str:
        if self.form == "bare":
            return f"legba/{self.family}/{self.major}.{self.minor}.{self.patch}"
        return (
            f"iglu:legba/{self.family}/jsonschema/"
            f"{self.major}-{self.minor}-{self.patch}"
        )

    def bump_patch(self) -> "SchemaUri":
        return SchemaUri(self.family, self.major, self.minor, self.patch + 1, self.form)

    def bump_minor(self) -> "SchemaUri":
        return SchemaUri(self.family, self.major, self.minor + 1, 0, self.form)

    def bump_major(self) -> "SchemaUri":
        return SchemaUri(self.family, self.major + 1, 0, 0, self.form)


def parse_schema_uri(uri: str) -> SchemaUri:
    """Parse either 'iglu:legba/...' or 'legba/...' form. Raises ValueError on bad URI."""
    m = _IGLU_RE.match(uri)
    if m:
        return SchemaUri(
            family=m.group("family"),
            major=int(m.group("major")),
            minor=int(m.group("minor")),
            patch=int(m.group("patch")),
            form="iglu",
        )
    m = _BARE_RE.match(uri)
    if m:
        return SchemaUri(
            family=m.group("family"),
            major=int(m.group("major")),
            minor=int(m.group("minor")),
            patch=int(m.group("patch")),
            form="bare",
        )
    raise ValueError(f"invalid schema_uri: {uri!r}")


def is_valid_schema_uri(uri: str) -> bool:
    try:
        parse_schema_uri(uri)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Provenance row construction
# ---------------------------------------------------------------------------


@dataclass
class ProvenanceFields:
    """Universal provenance columns per L-107 §1 / L-090 §4.1."""

    target_id: str | None = None
    target_version: str | None = None
    analyst_id: str | None = None
    analyst_version: str | None = None
    produced_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    derived_from: list[UUID] = field(default_factory=list)
    schema_uri: str = ""
    run_id: UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # UUID list → list of strings for serialization parity (consumer can pass
        # native UUID[] into asyncpg either way).
        d["derived_from"] = list(self.derived_from)
        return d

    def as_kwargs(self) -> dict[str, Any]:
        """Returns the dict suitable for unpacking into an INSERT, but with
        native UUID values preserved (asyncpg likes those)."""
        return {
            "target_id": self.target_id,
            "target_version": self.target_version,
            "analyst_id": self.analyst_id,
            "analyst_version": self.analyst_version,
            "produced_at": self.produced_at,
            "derived_from": list(self.derived_from),
            "schema_uri": self.schema_uri,
            "run_id": self.run_id,
        }


def from_target(
    ctx: TargetContext,
    *,
    schema_uri: str,
    derived_from: Sequence[UUID] | None = None,
) -> ProvenanceFields:
    """Build provenance fields for a row written by a target pipeline."""
    if not is_valid_schema_uri(schema_uri):
        raise ValueError(f"schema_uri invalid: {schema_uri!r}")
    return ProvenanceFields(
        target_id=ctx.target_id,
        target_version=ctx.target_version,
        analyst_id=None,
        analyst_version=None,
        derived_from=list(derived_from or []),
        schema_uri=schema_uri,
        run_id=None,
    )


def from_analyst(
    ctx: AnalystContext,
    *,
    schema_uri: str,
    derived_from: Sequence[UUID] | None = None,
) -> ProvenanceFields:
    """Build provenance fields for a row produced by an analyst run."""
    if not is_valid_schema_uri(schema_uri):
        raise ValueError(f"schema_uri invalid: {schema_uri!r}")
    return ProvenanceFields(
        target_id=ctx.target_id,
        target_version=ctx.target_version,
        analyst_id=ctx.analyst_id,
        analyst_version=ctx.analyst_version,
        derived_from=list(derived_from or []),
        schema_uri=schema_uri,
        run_id=ctx.run_id,
    )


# Legacy sentinel per DM-3.
LEGACY_TARGET_SENTINEL = "pre-descriptor.legacy"


def legacy_provenance(schema_uri: str) -> ProvenanceFields:
    """Pre-descriptor back-tag (DM-3) — used only when ingesting legacy rows."""
    if not is_valid_schema_uri(schema_uri):
        raise ValueError(f"schema_uri invalid: {schema_uri!r}")
    return ProvenanceFields(
        target_id=LEGACY_TARGET_SENTINEL,
        target_version="legacy",
        derived_from=[],
        schema_uri=schema_uri,
    )


# ---------------------------------------------------------------------------
# derived_from helpers
# ---------------------------------------------------------------------------


def append_derived_from(
    existing: Sequence[UUID] | None, new_ids: Iterable[UUID]
) -> list[UUID]:
    """Append-and-dedupe UUIDs onto a `derived_from` list."""
    seen: set[UUID] = set()
    out: list[UUID] = []
    for u in (existing or ()):
        if u not in seen:
            seen.add(u)
            out.append(u)
    for u in new_ids:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def query_ancestors(
    conn: asyncpg.Connection,
    table: str,
    row_id: UUID,
    *,
    max_depth: int = 32,
) -> list[dict[str, Any]]:
    """Return the lineage backward from `row_id` in `table`.

    Walks `derived_from` recursively up to `max_depth` levels. The table must
    carry the universal provenance columns from migration 0002.
    """
    if not _safe_table_name(table):
        raise ValueError(f"unsafe table name: {table!r}")
    sql = f"""
    WITH RECURSIVE ancestors AS (
        SELECT id, target_id, analyst_id, produced_at, derived_from, 0 AS depth
        FROM {table}
        WHERE id = $1
        UNION ALL
        SELECT t.id, t.target_id, t.analyst_id, t.produced_at, t.derived_from, a.depth + 1
        FROM {table} t
        JOIN ancestors a ON t.id = ANY(a.derived_from)
        WHERE a.depth < $2
    )
    SELECT * FROM ancestors ORDER BY depth, produced_at;
    """
    rows = await conn.fetch(sql, row_id, max_depth)
    return [dict(r) for r in rows]


async def query_descendants(
    conn: asyncpg.Connection,
    table: str,
    target_id: str,
    *,
    max_depth: int = 32,
) -> list[dict[str, Any]]:
    """Return all rows in `table` derived (transitively) from anything tagged
    with `target_id`. Forward query per L-107 §2(a).
    """
    if not _safe_table_name(table):
        raise ValueError(f"unsafe table name: {table!r}")
    sql = f"""
    WITH RECURSIVE descendants AS (
        SELECT id, target_id, analyst_id, produced_at, derived_from, 0 AS depth
        FROM {table}
        WHERE target_id = $1
        UNION ALL
        SELECT t.id, t.target_id, t.analyst_id, t.produced_at, t.derived_from, d.depth + 1
        FROM {table} t
        JOIN descendants d ON d.id = ANY(t.derived_from)
        WHERE d.depth < $2
    )
    SELECT DISTINCT id, target_id, analyst_id, produced_at FROM descendants
    ORDER BY produced_at;
    """
    rows = await conn.fetch(sql, target_id, max_depth)
    return [dict(r) for r in rows]


_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _safe_table_name(name: str) -> bool:
    return bool(_TABLE_RE.match(name))


# ---------------------------------------------------------------------------
# Canonical JSON + receipt hashing
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> bytes:
    """Sort keys, no spaces, UTF-8, ensure_ascii=False — Mnemosyne D5 form."""
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return {k: v for k, v in value.__dict__.items() if not k.startswith("_")}
    raise TypeError(f"cannot serialize {value!r}")


def sha256_canonical(obj: Any) -> str:
    """SHA-256 hex over canonical-JSON of `obj`."""
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def compute_receipt_hash(
    *,
    run_id: UUID,
    analyst_id: str,
    analyst_version: str,
    input_row_refs: Sequence[UUID],
    prompt_module_hash: str | None,
    prompt_rendered: str | None,
    output_row_refs: Sequence[UUID],
    output_payload: Any,
    run_ended_at: datetime,
    prev_receipt_hash: str | None = None,
) -> str:
    """Compute the per-run receipt hash per L-107 §7.

    The chain field `prev_receipt_hash` is included to make the chain
    tamper-evident. `prev_receipt_hash=None` means "this is the first run for
    this analyst" — caller passes `ZERO_HASH` if the verifier expects it.
    """
    payload = {
        "run_id": str(run_id),
        "analyst_id": analyst_id,
        "analyst_version": analyst_version,
        "input_row_refs": sorted(str(r) for r in input_row_refs),
        "prompt_module_hash": prompt_module_hash,
        "prompt_rendered": prompt_rendered,
        "output_row_refs": sorted(str(r) for r in output_row_refs),
        "output_payload": output_payload,
        "run_ended_at": run_ended_at.astimezone(timezone.utc).isoformat(),
        "prev_receipt_hash": prev_receipt_hash or ZERO_HASH,
    }
    return sha256_canonical(payload)


ZERO_HASH: str = "0" * 64


__all__ = [
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
]
