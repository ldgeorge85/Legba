# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lineage walk surface for the registry HTTP API (L-204 P-5).

Mounts under ``/api/v1/lineage`` alongside the v1 registry and v3 telemetry
routers. Built via ``build_lineage_router(deps)``; ``server.py`` wires it
with::

    from .lineage_api import build_lineage_router
    app.include_router(build_lineage_router(deps), prefix="/api/v1")

The endpoint:

    GET /api/v1/lineage/{row_kind}/{row_id}
        ?direction=upstream|downstream|both
        &depth=N   (default 3, capped at 10)

Returns a ``LineageReport`` walking the universal ``derived_from uuid[]``
column. ``direction=upstream`` walks parents (rows this row was derived
FROM); ``downstream`` walks children (rows derived FROM this row). ``both``
walks both, with the root carrying ``depth=0`` and edges marked by the
direction of derivation (parent → child).

Cross-table walk strategy
=========================

The universal-provenance contract from migration 0002 keeps the
``derived_from uuid[]`` column on every substrate table, and UUIDs are
globally unique across substrate (gen_random_uuid()/UUIDv4). But a single
row can legitimately have parents in a *different* table than itself —
e.g. a ``finding`` (in ``analyst_outputs``) derived from a ``signal`` (in
``signals``); a ``situation`` (in ``situations``) derived from a
``finding`` (in ``analyst_outputs``); a ``prompt_module_candidate`` (in
``analyst_outputs``) derived from a ``finding`` (also in ``analyst_outputs``
but with a different ``kind`` discriminator).

Two implementation options:

  1. **Per-table recursive CTE** (what ``provenance._core.query_ancestors``
     does). Fast, but each CTE only sees rows in its own table — the walk
     stops the moment lineage crosses a table boundary. Useless here.

  2. **Per-table CTE per hop with cross-table union** — possible but the
     SQL becomes a code-generated UNION ALL over every (table_for_kind,
     direction) pair, and the recursive frontier explodes.

  3. **App-side BFS** — fetch the root, look up its ``derived_from`` UUIDs
     across the candidate substrate tables, repeat for each frontier
     until depth or empty. One round-trip per hop per direction with a
     single batched ``id = ANY($1)`` per table. With ~7 substrate tables
     and depth=3, worst case is 7 * 3 * 2 = 42 lightweight indexed
     lookups, all hitting `id` (primary key) or the `derived_from` GIN
     index. The numbers stay small because the frontier itself stays
     small for the operator-facing "where did this come from" question.

We picked option 3 (app-side BFS) for:

  * Truly cross-table walks. A signal→finding→situation chain Just Works.
  * Cycle protection lives in Python (a single visited set keyed by UUID),
    so a row referencing itself or an earlier ancestor doesn't infinite-
    loop the recursive CTE.
  * The depth-truncation flag can distinguish "depth exhausted with more
    frontier" from "fully walked" without inspecting CTE metadata.
  * Single Postgres pool checkout per request.

Bounds on depth: default 3 because the operator-facing surface is "show me
where this came from" (signal → finding → situation → analyst_output is
3-4 hops typical). 10 is the hard cap so a graph explosion can't pin a
worker. Hitting the cap sets ``truncated_at_depth=true`` in the response
so the UI can render "show more" affordances.

Supported ``row_kind`` set
==========================

``signal``, ``finding``, ``situation``, ``prediction``, ``hypothesis``,
``critique``, ``alert``, ``meta_finding``, ``prompt_module_candidate``.
The user-supplied root must be one of these (else 400). But the walk
itself looks across *every* substrate table that carries the universal
provenance columns — e.g. ``facts`` and ``situations`` — because a
supported row's parents/children may live there. Cross-substrate row
kinds discovered during the walk are labelled by the table they came
from (``fact`` for ``facts``, ``situation`` for ``situations``), or by
``analyst_outputs.kind`` for the polymorphic analyst-output table.

No mocks. Real recursive walk over real columns.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal, Mapping
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from ..provenance._core import compute_receipt_hash
from .api import RegistryAPIDeps, require_bearer


# The ONLY badge this surface may emit for an analyst_traces chain. The chain
# is a SHA-256 hash-chain (receipt_hash / prev_receipt_hash), NOT an Ed25519
# signature — Ed25519 signing covers the audit checkpointer's chain HEAD, not
# the individual trace row. So the honest claim is bounded to "this single
# node re-hashes to the value the chain stored" — i.e. the row was not
# mutated after it was recorded. We deliberately do NOT claim "signed",
# "tamper-proof", or any Ed25519 guarantee for the per-row receipt.
_RECEIPT_BADGE = "chain-consistent (single-node)"


# ---------------------------------------------------------------------------
# Substrate-table catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SubstrateTable:
    """One substrate table carrying universal provenance columns.

    ``title_expr`` is the SQL expression used to project the row's human-
    readable title, since not every table has a literal ``title`` column.
    ``kind_expr`` is the SQL expression that yields the row_kind label —
    a string literal for dedicated tables, ``kind`` for analyst_outputs.
    """

    table: str
    kind_expr: str
    title_expr: str
    # Universal-provenance column expressions. Default to the literal column
    # name; overridden per table where the source-first schema diverges
    # (e.g. signals is target-agnostic + uses fetched_at, not produced_at).
    produced_expr: str = "produced_at"
    target_expr: str = "target_id"
    analyst_expr: str = "analyst_id"
    # Link/media surface — only signals carry these (modality-first columns).
    # Every other table projects NULL so the BFS keeps a uniform column shape.
    canonical_url_expr: str = "NULL::text"
    media_ref_expr: str = "NULL::text"
    modality_expr: str = "NULL::text"
    mime_type_expr: str = "NULL::text"
    # The full report PAYLOAD (jsonb) carrying the written report (summary /
    # body / assessment / …). Projected for the ROOT node only (the Inspector
    # reads it to render the report text); the lineage walk root carries
    # metadata only without this. Default NULL where the row has no payload.
    body_expr: str = "NULL::jsonb"
    # The producing run id — joins the root to its analyst_traces receipt
    # chain (root-only projection). Every substrate table except signals
    # carries a literal ``run_id`` column; signals are source-ingested (no
    # analyst run) so they project NULL.
    run_id_expr: str = "run_id"


# Every substrate table that carries universal provenance columns
# (target_id, analyst_id, derived_from, schema_uri, produced_at). The walk
# fans out across all of these on each hop, even though only a subset of
# row_kinds are valid roots — a signal's children may legitimately be
# findings, situations, hypotheses, or facts.
_SUBSTRATE_TABLES: tuple[_SubstrateTable, ...] = (
    _SubstrateTable(
        table="signals",
        kind_expr="'signal'",
        # Source-first: signals are target-agnostic + modality-first — no
        # title/target_id/analyst_id columns, and the timestamp is fetched_at.
        title_expr="payload->>'title'",
        produced_expr="fetched_at",
        target_expr="NULL::text",
        analyst_expr="NULL::text",
        canonical_url_expr="canonical_url",
        media_ref_expr="media_ref",
        modality_expr="modality",
        mime_type_expr="mime_type",
        body_expr="payload",
        # Signals are source-ingested, not analyst-run output — no run_id.
        run_id_expr="NULL::uuid",
    ),
    # events table dropped in the source-first pivot (migration 0030 —
    # target-agnostic signals replaced the events concept; nothing has
    # written `events` since 0024). Removed from the lineage catalog so the
    # cross-table derived_from walk doesn't query a non-existent relation.
    _SubstrateTable(
        table="situations",
        kind_expr="'situation'",
        title_expr="name",
        body_expr="data",
    ),
    _SubstrateTable(
        table="hypotheses",
        kind_expr="'hypothesis'",
        # `thesis` is the user-readable statement; clamp to a sensible length
        # so a multi-paragraph hypothesis doesn't bloat the response.
        title_expr="LEFT(thesis, 240)",
        # No payload column — surface the full thesis + counter as the report.
        body_expr="jsonb_build_object('thesis', thesis, 'counter_thesis', counter_thesis)",
    ),
    # predictions table dropped in the source-first pivot — hypotheses (above)
    # is its successor and is already in the catalog.
    _SubstrateTable(
        table="facts",
        kind_expr="'fact'",
        title_expr="subject || ' ' || predicate || ' ' || value",
    ),
    _SubstrateTable(
        table="analyst_outputs",
        # analyst_outputs is polymorphic — kind column is the discriminator.
        kind_expr="kind",
        title_expr="title",
        # The report payload — findings/meta_findings/alerts/critiques all live
        # here; `data` carries summary/body/assessment, the actual report text.
        body_expr="data",
    ),
)


# Supported root row_kinds (per the brief). The walk crosses table boundaries
# but the user-supplied root has to be one of these substrate kinds —
# unknown row_kinds are 400. Map kind → (table, optional kind filter).
_TABLES_BY_KIND: dict[str, tuple[str, str | None]] = {
    # Dedicated tables.
    "signal":      ("signals",        None),
    "situation":   ("situations",     None),
    "hypothesis":  ("hypotheses",     None),
    # Polymorphic — analyst_outputs.kind discriminates.
    "finding":                 ("analyst_outputs", "finding"),
    "meta_finding":            ("analyst_outputs", "meta_finding"),
    "alert":                   ("analyst_outputs", "alert"),
    "critique":                ("analyst_outputs", "critique"),
    "prompt_module_candidate": ("analyst_outputs", "prompt_module_candidate"),
}


# Hard bound on walk depth — see module docstring.
_MAX_DEPTH = 10
_DEFAULT_DEPTH = 3


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class ReceiptChainNode(BaseModel):
    """The analyst_traces receipt-chain receipt for the run that produced a
    node — surfaced on the ROOT and on every WALK node mapping to an analyst
    run (P1-T4; the ROOT-only ``body`` payload stays root-only, but the receipt
    travels with each node so the UI can drill the DAG one hop at a time).

    HONESTY CONTRACT: the receipt chain is a SHA-256 hash-chain, not a
    signature. ``receipt_hash`` / ``prev_receipt_hash`` are the values the
    chain stored; ``chain_consistent`` is RE-COMPUTED here (the trace row is
    re-hashed via :func:`compute_receipt_hash` and compared to the stored
    ``receipt_hash``) — never the trust of a stored boolean. A mutated /
    forked row re-hashes to a different value → ``chain_consistent=False``.

    ``signer_did`` is populated ONLY when an ``audit_checkpoints`` row whose
    ``chain_head_hash`` equals this trace's ``receipt_hash`` exists (i.e. an
    Ed25519 checkpoint actually covers THIS row as the chain head it signed);
    otherwise it is ``None`` — we never imply a signer that didn't sign.

    ``badge`` is fixed to ``"chain-consistent (single-node)"``. No field here
    claims "signed", "tamper-proof", or Ed25519 for the per-row receipt.
    """

    run_id: str
    receipt_hash: str
    prev_receipt_hash: str | None
    # RE-COMPUTED, not the trust of any stored flag.
    chain_consistent: bool
    # Present only when an audit_checkpoint covers this row's receipt_hash.
    signer_did: str | None = None
    badge: str = _RECEIPT_BADGE


class LineageNode(BaseModel):
    """One row in the lineage graph.

    Fields mirror the universal-provenance columns. ``depth`` is 0 for the
    root and increments by 1 per hop in either direction. For ``both``
    walks, a downstream child at distance 2 carries ``depth=2`` exactly
    like an upstream ancestor at distance 2 — direction is encoded in the
    edges, not in the depth.
    """

    id: str
    row_kind: str
    title: str | None
    produced_at: datetime
    target_id: str | None
    analyst_id: str | None
    schema_uri: str
    depth: int
    # The report PAYLOAD — present on the ROOT node only (the Inspector reads it
    # to render the written report: summary/body/assessment). None for non-root
    # walk nodes (kept lean) and for rows with no payload (e.g. facts).
    body: dict[str, Any] | None = None
    # Link/media surface — populated for signal rows (the acquisition source),
    # None for analyst-output / event / situation kinds. This is what lets a
    # lineage walk reach the clickable source, not just a payload title.
    canonical_url: str | None = None
    media_ref: str | None = None
    modality: str | None = None
    mime_type: str | None = None
    # The receipt-chain receipt for the run that produced this row — surfaced
    # on the ROOT *and* on every WALK node that maps to an analyst run (P1-T4,
    # so the P1-T5 UI can drill the DAG one hop at a time). Signals /
    # source-ingested rows (and analyst_outputs predating the chain) carry None
    # honestly — no producing trace to re-hash. See ``ReceiptChainNode`` for
    # the honesty contract; ``chain_consistent`` is RE-COMPUTED per node.
    receipt: ReceiptChainNode | None = None


class LineageEdge(BaseModel):
    """A parent→child derivation edge. ``parent`` is the row the ``child``
    was derived from (i.e. ``parent`` ∈ ``child.derived_from``)."""

    parent: str
    child: str


class LineageReport(BaseModel):
    """Response body for ``GET /api/v1/lineage/{row_kind}/{row_id}``."""

    root: LineageNode
    nodes: list[LineageNode] = Field(default_factory=list)
    edges: list[LineageEdge] = Field(default_factory=list)
    truncated_at_depth: bool = False


# ---------------------------------------------------------------------------
# Walk helpers
# ---------------------------------------------------------------------------


def _parse_body(raw: object) -> dict[str, Any] | None:
    """Coerce a projected ``lineage_body`` jsonb cell into a dict.

    The lineage connection sets no jsonb codec, so asyncpg returns the column
    as a JSON string; parse it. Already-a-dict (if a codec is ever added) and
    NULL (non-root / payload-less rows) both degrade cleanly to dict|None."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _receipt_node_from_trace(
    trace: Mapping[str, Any],
    *,
    covering_checkpoint: Mapping[str, Any] | None = None,
) -> ReceiptChainNode:
    """Build the honest receipt node for one ``analyst_traces`` row.

    ``chain_consistent`` is computed by RE-HASHING the trace's content via
    :func:`compute_receipt_hash` and comparing to the stored ``receipt_hash``
    — a mutated payload (or a forked/relabelled row) re-hashes differently and
    flips this to ``False``. We never read a stored consistency flag (there
    isn't one, and trusting one would defeat the point).

    ``output_payload`` is stored as jsonb and comes back from asyncpg as a JSON
    string; we parse it so the re-hash sees the same Python object the writer
    hashed. ``output_payload IS NULL`` was written as ``{}`` by the writer
    (``json.dumps(output_payload or {})``), so a NULL column re-hashes against
    ``{}`` to stay faithful to the recorded value.

    ``signer_did`` is taken from a covering ``audit_checkpoints`` row ONLY —
    i.e. one whose ``chain_head_hash`` equals this trace's ``receipt_hash``. If
    no checkpoint signed this exact head, ``signer_did`` stays ``None``; the
    badge never upgrades to "signed".
    """
    stored_hash: str = trace["receipt_hash"]
    prev_hash: str | None = trace.get("prev_receipt_hash")

    recomputed = compute_receipt_hash(
        run_id=trace["run_id"],
        analyst_id=trace["analyst_id"],
        analyst_version=trace["analyst_version"],
        input_row_refs=list(trace["input_row_refs"] or []),
        prompt_module_hash=trace.get("prompt_module_hash"),
        prompt_rendered=trace.get("prompt_rendered"),
        output_row_refs=list(trace["output_row_refs"] or []),
        output_payload=_parse_body(trace.get("output_payload")) or {},
        run_ended_at=trace["run_ended_at"],
        prev_receipt_hash=prev_hash,
    )

    signer_did: str | None = None
    if covering_checkpoint is not None:
        # Only honest when the checkpoint actually signed THIS receipt as the
        # chain head it covered.
        if covering_checkpoint.get("chain_head_hash") == stored_hash:
            signer_did = covering_checkpoint.get("signer_did")

    return ReceiptChainNode(
        run_id=str(trace["run_id"]),
        receipt_hash=stored_hash,
        prev_receipt_hash=prev_hash,
        chain_consistent=(recomputed == stored_hash),
        signer_did=signer_did,
        badge=_RECEIPT_BADGE,
    )


async def _covering_checkpoint(
    conn: asyncpg.Connection,
    trace: asyncpg.Record | Mapping[str, Any],
) -> asyncpg.Record | None:
    """Look up the ``audit_checkpoints`` row (if any) that COVERS this trace.

    A checkpoint covers the trace only when it signed this exact ``receipt_hash``
    as the chain head it checkpointed (the checkpointer signs heads, so non-head
    rows stay uncovered → ``signer_did`` honestly None)."""
    return await conn.fetchrow(
        "SELECT signer_did, chain_head_hash FROM audit_checkpoints "
        "WHERE analyst_id = $1 AND chain_head_hash = $2 "
        "ORDER BY checkpointed_at DESC LIMIT 1",
        trace["analyst_id"], trace["receipt_hash"],
    )


async def _receipt_for_trace(
    conn: asyncpg.Connection,
    trace: asyncpg.Record | None,
) -> ReceiptChainNode | None:
    """Wrap a fetched ``analyst_traces`` row into its honest receipt node,
    resolving the covering checkpoint. ``None`` trace → ``None`` receipt
    (a node with no producing analyst run — e.g. a raw signal)."""
    if trace is None:
        return None
    checkpoint = await _covering_checkpoint(conn, trace)
    return _receipt_node_from_trace(trace, covering_checkpoint=checkpoint)


async def _fetch_trace_for_node(
    conn: asyncpg.Connection,
    *,
    node_id: UUID,
    run_id: UUID | None,
) -> asyncpg.Record | None:
    """Map ONE substrate row → its producing ``analyst_traces`` row, if any.

    Two lookups, first hit wins (mirrors the root resolution):

      1. ``analyst_traces.run_id = run_id`` (the direct producer link, when the
         node projection carried a ``run_id`` — only the root does today, but
         the helper stays general).
      2. ``node_id = ANY(analyst_traces.output_row_refs)`` (the L-107 §7
         lineage-into-chain: the trace records the rows it produced) — the path
         every WALK child uses, since walk projections don't carry ``run_id``.

    Returns ``None`` for rows with no producing trace (a raw signal, a
    source-ingested row, or an analyst_output predating the receipt chain) —
    honestly absent, never a fabricated receipt.
    """
    trace: asyncpg.Record | None = None
    if run_id is not None:
        trace = await conn.fetchrow(
            "SELECT * FROM analyst_traces WHERE run_id = $1",
            run_id,
        )
    if trace is None:
        trace = await conn.fetchrow(
            "SELECT * FROM analyst_traces WHERE $1 = ANY(output_row_refs) "
            "ORDER BY run_started_at DESC LIMIT 1",
            node_id,
        )
    return trace


async def _fetch_receipt_for_root(
    conn: asyncpg.Connection,
    root: asyncpg.Record,
) -> ReceiptChainNode | None:
    """Resolve the receipt-chain receipt for the root row, if it came from a
    recorded analyst run.

    The finding/meta_finding/critique/etc. carries its producing run's
    ``run_id`` (analyst_outputs.run_id). We map root → trace two ways and take
    the first hit:

      1. ``analyst_traces.run_id = root.run_id`` (the direct producer link).
      2. ``root.id = ANY(analyst_traces.output_row_refs)`` (the L-107 §7
         lineage-into-chain: the trace records the rows it produced) — covers
         older rows whose ``run_id`` column wasn't populated.

    Returns ``None`` for roots with no producing trace (e.g. a raw signal, or
    an analyst_output predating the receipt chain) — honestly absent, not a
    fabricated receipt.
    """
    # The root projection carries run_id (it's projected with_body); walk
    # children do not, so read it defensively. asyncpg.Record raises KeyError
    # for an absent column rather than returning None.
    try:
        run_id = root["run_id"]
    except KeyError:
        run_id = None

    trace = await _fetch_trace_for_node(
        conn, node_id=root["id"], run_id=run_id,
    )
    return await _receipt_for_trace(conn, trace)


async def _attach_receipts_to_walk(
    conn: asyncpg.Connection,
    nodes: list[LineageNode],
) -> None:
    """Attach each WALK node's own receipt-chain receipt, in place (P1-T4).

    P0-T4 enriched the ROOT only. To drill the DAG one hop at a time (the P1-T5
    UI) EVERY analyst-produced node needs its receipt + recomputed
    ``chain_consistent`` flag. We resolve each node's producing trace via the
    ``output_row_refs`` containment path (walk projections don't carry
    ``run_id``), then attach the honest single-node receipt.

    Nodes with no producing analyst run — signals / source-ingested rows, or
    analyst_outputs predating the receipt chain — keep ``receipt=None`` (the
    field's default) honestly: there is no trace to re-hash, so we fabricate
    nothing. ``chain_consistent`` is RE-COMPUTED per node (never a stored flag)
    inside :func:`_receipt_node_from_trace`.
    """
    for node in nodes:
        trace = await _fetch_trace_for_node(
            conn, node_id=UUID(node.id), run_id=None,
        )
        node.receipt = await _receipt_for_trace(conn, trace)


def _row_to_node(row: asyncpg.Record, *, depth: int) -> LineageNode:
    """Map a substrate row record to a ``LineageNode``. ``row`` must carry
    the canonical projected columns (see ``_projection``). The ``lineage_body``
    payload is present only when projected with ``with_body`` (the root)."""
    title = row["lineage_title"]
    return LineageNode(
        id=str(row["id"]),
        row_kind=row["lineage_kind"],
        title=str(title) if title is not None else None,
        produced_at=row["produced_at"],
        target_id=row["target_id"],
        analyst_id=row["analyst_id"],
        schema_uri=row["schema_uri"],
        depth=depth,
        body=_parse_body(row.get("lineage_body")),
        canonical_url=row["canonical_url"],
        media_ref=row["media_ref"],
        modality=row["modality"],
        mime_type=row["mime_type"],
    )


def _projection(t: _SubstrateTable, *, with_body: bool = False) -> str:
    """Per-table SELECT projection — same column shape across every table
    so the BFS doesn't have to branch on table identity downstream.

    ``with_body`` adds the report payload column (``lineage_body``) AND the
    producing ``run_id`` — both used ONLY for the root fetch so the heavy
    payload + the receipt-chain join travel for one row, not every node in the
    walk."""
    body_col = (
        f"{t.body_expr} AS lineage_body, {t.run_id_expr} AS run_id, "
        if with_body
        else ""
    )
    return (
        f"SELECT id, {t.kind_expr} AS lineage_kind, "
        f"{t.title_expr} AS lineage_title, "
        f"{t.produced_expr} AS produced_at, "
        f"{t.target_expr} AS target_id, "
        f"{t.analyst_expr} AS analyst_id, "
        f"{t.canonical_url_expr} AS canonical_url, "
        f"{t.media_ref_expr} AS media_ref, "
        f"{t.modality_expr} AS modality, "
        f"{t.mime_type_expr} AS mime_type, "
        f"{body_col}"
        f"schema_uri, derived_from "
        f"FROM {t.table} "
    )


async def _fetch_root(
    conn: asyncpg.Connection,
    row_kind: str,
    row_id: UUID,
) -> asyncpg.Record | None:
    """Look up the root row in its kind-specific table.

    For polymorphic ``analyst_outputs`` kinds, we filter on both id and
    the ``kind`` discriminator so a finding UUID doesn't accidentally
    resolve via the alert path (UUIDs are unique so this is belt-and-
    braces, but the discriminator also lets us reject a stale UUID with
    the wrong row_kind).
    """
    table, kind_filter = _TABLES_BY_KIND[row_kind]
    spec = next(t for t in _SUBSTRATE_TABLES if t.table == table)
    # Root carries the full report payload (lineage_body); walk children don't.
    sql = _projection(spec, with_body=True) + "WHERE id = $1"
    if kind_filter is not None:
        sql += " AND kind = $2"
        return await conn.fetchrow(sql, row_id, kind_filter)
    return await conn.fetchrow(sql, row_id)


async def _fetch_by_ids(
    conn: asyncpg.Connection,
    ids: list[UUID],
) -> list[asyncpg.Record]:
    """Fan out a batched ``id = ANY($1)`` query across every substrate
    table. UUIDs are globally unique so each id resolves in at most one
    table, but we don't know which a priori — issue all the queries and
    union the results.

    Returns one record per row found. Caller is responsible for
    de-duplicating against the visited set.
    """
    if not ids:
        return []
    out: list[asyncpg.Record] = []
    for spec in _SUBSTRATE_TABLES:
        rows = await conn.fetch(
            _projection(spec) + "WHERE id = ANY($1::uuid[])",
            ids,
        )
        out.extend(rows)
    return out


async def _fetch_children_of(
    conn: asyncpg.Connection,
    parent_ids: list[UUID],
) -> list[asyncpg.Record]:
    """Downstream fan-out: every row whose ``derived_from`` array contains
    any of ``parent_ids``. One query per substrate table; uses the GIN
    index on ``derived_from`` for the array containment predicate.

    Postgres ``&&`` between two UUID arrays is "have any element in
    common" and is GIN-indexable on the LHS.
    """
    if not parent_ids:
        return []
    out: list[asyncpg.Record] = []
    for spec in _SUBSTRATE_TABLES:
        rows = await conn.fetch(
            _projection(spec) + "WHERE derived_from && $1::uuid[]",
            parent_ids,
        )
        out.extend(rows)
    return out


def _dedupe_keep_first(
    records: Iterable[asyncpg.Record],
) -> list[asyncpg.Record]:
    """When a UUID somehow shows up in two tables, keep the first one. (Not
    expected — UUIDs are unique — but a fact-as-row-id collision shouldn't
    crash the walker.)"""
    seen: set[UUID] = set()
    out: list[asyncpg.Record] = []
    for r in records:
        rid = r["id"]
        if rid in seen:
            continue
        seen.add(rid)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# BFS walkers
# ---------------------------------------------------------------------------


async def _walk_upstream(
    conn: asyncpg.Connection,
    root: asyncpg.Record,
    max_depth: int,
) -> tuple[list[LineageNode], list[LineageEdge], bool]:
    """BFS over ``derived_from`` parents. Returns (non-root nodes, edges,
    truncated_flag).

    Cycle protection: visited set keyed by UUID. If a frontier row points
    back to an already-visited ancestor we still emit the edge (so the
    graph is consistent) but don't enqueue the ancestor again.
    """
    visited: set[UUID] = {root["id"]}
    nodes: list[LineageNode] = []
    edges: list[LineageEdge] = []

    # Frontier carries (row_record, depth-of-this-row). Initial frontier is
    # whatever the root's derived_from points at, at depth 1.
    frontier_ids: list[tuple[UUID, UUID]] = [
        # (parent_id, child_id) — the child here is the root.
        (pid, root["id"]) for pid in (root["derived_from"] or [])
    ]
    truncated = False
    depth = 1

    while frontier_ids:
        if depth > max_depth:
            # We still had frontier to walk — mark truncated.
            truncated = True
            break

        # Resolve every parent UUID across substrate tables in one fan-out.
        ids_to_fetch = [pid for pid, _ in frontier_ids if pid not in visited]
        # But also emit edges even for already-visited UUIDs (the graph
        # may legitimately re-converge).
        for pid, cid in frontier_ids:
            edges.append(LineageEdge(parent=str(pid), child=str(cid)))

        if not ids_to_fetch:
            break

        records = _dedupe_keep_first(
            await _fetch_by_ids(conn, ids_to_fetch),
        )

        next_frontier: list[tuple[UUID, UUID]] = []
        for r in records:
            rid: UUID = r["id"]
            if rid in visited:
                continue
            visited.add(rid)
            nodes.append(_row_to_node(r, depth=depth))
            for grand_parent in (r["derived_from"] or []):
                next_frontier.append((grand_parent, rid))

        frontier_ids = next_frontier
        depth += 1

    return nodes, edges, truncated


async def _walk_downstream(
    conn: asyncpg.Connection,
    root: asyncpg.Record,
    max_depth: int,
) -> tuple[list[LineageNode], list[LineageEdge], bool]:
    """BFS over ``derived_from`` children. Symmetric to ``_walk_upstream``
    but the fan-out is "rows whose derived_from array contains any of the
    frontier UUIDs"."""
    visited: set[UUID] = {root["id"]}
    nodes: list[LineageNode] = []
    edges: list[LineageEdge] = []

    # Initial frontier: parents-to-fetch-children-of. Starts as just the
    # root id.
    parent_layer_ids: list[UUID] = [root["id"]]
    truncated = False
    depth = 1

    while parent_layer_ids:
        if depth > max_depth:
            truncated = True
            break

        records = _dedupe_keep_first(
            await _fetch_children_of(conn, parent_layer_ids),
        )
        parent_set = set(parent_layer_ids)

        next_layer: list[UUID] = []
        for r in records:
            rid: UUID = r["id"]
            # Emit one edge per (parent ∈ parent_layer, child) so the graph
            # captures which specific frontier row produced this child.
            for pid in (r["derived_from"] or []):
                if pid in parent_set:
                    edges.append(
                        LineageEdge(parent=str(pid), child=str(rid)),
                    )
            if rid in visited:
                # Re-convergence — edge recorded above, but don't recurse.
                continue
            visited.add(rid)
            nodes.append(_row_to_node(r, depth=depth))
            next_layer.append(rid)

        parent_layer_ids = next_layer
        depth += 1

    return nodes, edges, truncated


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_lineage_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the lineage walk router bound to the registry deps.

    Mount under ``/api/v1`` so the path resolves at
    ``/api/v1/lineage/{row_kind}/{row_id}``.
    """
    router = APIRouter(tags=["lineage"])

    @router.get(
        "/lineage/{row_kind}/{row_id}",
        response_model=LineageReport,
    )
    async def walk_lineage(
        row_kind: str = Path(
            ...,
            description=(
                "Originating substrate row kind — one of: "
                + ", ".join(sorted(_TABLES_BY_KIND))
            ),
        ),
        row_id: UUID = Path(..., description="Substrate row UUID."),
        direction: Literal["upstream", "downstream", "both"] = Query(
            default="upstream",
            description=(
                "upstream walks parents (rows this row was derived FROM); "
                "downstream walks children (rows derived FROM this row); "
                "both walks both"
            ),
        ),
        depth: int = Query(
            default=_DEFAULT_DEPTH,
            ge=1,
            le=_MAX_DEPTH,
            description=(
                f"Max BFS depth. Default {_DEFAULT_DEPTH}, hard cap {_MAX_DEPTH}."
            ),
        ),
        _principal: str = Depends(require_bearer),
    ) -> LineageReport:
        if row_kind not in _TABLES_BY_KIND:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"unknown row_kind {row_kind!r}; expected one of "
                    f"{sorted(_TABLES_BY_KIND)}"
                ),
            )

        async with deps.descriptor_registry.pg.acquire() as conn:
            root_row = await _fetch_root(conn, row_kind, row_id)
            if root_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"no {row_kind!r} row found for id {row_id}"
                    ),
                )

            root_node = _row_to_node(root_row, depth=0)
            # Enrich the root with its producing run's receipt-chain receipt
            # (recomputed chain-consistency + honest single-node badge).
            root_node.receipt = await _fetch_receipt_for_root(conn, root_row)
            nodes: list[LineageNode] = []
            edges: list[LineageEdge] = []
            truncated = False

            if direction in ("upstream", "both"):
                up_nodes, up_edges, up_trunc = await _walk_upstream(
                    conn, root_row, depth,
                )
                nodes.extend(up_nodes)
                edges.extend(up_edges)
                truncated = truncated or up_trunc

            if direction in ("downstream", "both"):
                down_nodes, down_edges, down_trunc = await _walk_downstream(
                    conn, root_row, depth,
                )
                nodes.extend(down_nodes)
                edges.extend(down_edges)
                truncated = truncated or down_trunc

            # P1-T4: attach EACH walk node's own receipt (recomputed
            # chain_consistent + honest single-node badge) so the DAG can be
            # drilled one hop at a time. Signal / source-ingested nodes carry
            # receipt=None honestly (no producing analyst run to re-hash).
            await _attach_receipts_to_walk(conn, nodes)

        return LineageReport(
            root=root_node,
            nodes=nodes,
            edges=edges,
            truncated_at_depth=truncated,
        )

    return router


__all__ = [
    "LineageNode",
    "LineageEdge",
    "LineageReport",
    "ReceiptChainNode",
    "build_lineage_router",
]
