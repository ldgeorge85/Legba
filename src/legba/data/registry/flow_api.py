# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/api/v1/v3/flow/*`` — the read-only routes behind the Flow canvas (N-J v0).

* ``GET /flow/graph?scope=…&depth=…`` — the semantic graph derived from the
  active and paused descriptor heads (:mod:`.flow_graph`). No layout.
* ``GET /flow/node/{node_id}`` — one node: every field, its edges, versions,
  the prompt (declared + as last rendered), the output contract, sampling
  configured vs sent (:mod:`.flow_params`), recent runs and the steps the
  latest run recorded ("observed, not declared").
* ``GET /flow/version/{node_id}?version=…`` — one stored version's body, for
  the compare view (the diff is computed client-side over two of these).
* ``GET /flow/run/{run_id}`` — one run's steps, calls and row refs: sizes and
  ids, never payloads (:mod:`.flow_runs`).
* ``GET /flow/run/{run_id}/body?part=…&index=…`` — one body, capped.

SELECT-only, bearer-gated like its v3 siblings. It writes nothing, renders
nothing with a model and starts nothing.

THE PROMPT. The resolved prompt for an analyst is assembled at run time by the
runtime (live slices, grounding preamble, prior reads) — producing it fresh
needs the runtime and its data, which this route will not touch. So the node
route returns the descriptor's declared prompt (``method.system_prompt`` or the
``method.prompt_module`` reference) AND the prompt as last actually rendered:
``analyst_traces.prompt_rendered`` of the most recent run that recorded one,
which is the LAST LLM call's prompt before the trace write
(``data/run_accounting.current_prompt_rendered``), head-capped at 32,000 chars,
with the final 4,000 in ``input_payload.prompt_tail``. Both are labelled.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + the three flow leaf
modules. The subprocess guard in ``tests/data_pkg/test_flow_api.py`` poisons
the runtime-only packages and imports this module.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .api import RegistryAPIDeps, require_bearer
from .flow_graph import FAMILY_TO_KIND, apply_scope, derive_graph
from .flow_params import sampling_params
from .flow_runs import (
    BODY_PARTS,
    body_part,
    observed_calls,
    run_detail,
    run_summary,
    step_summaries,
)

#: family → table. Only these tables are ever read for descriptors.
FAMILY_TABLES: dict[str, str] = {
    "source": "source_descriptors",
    "target": "target_descriptors",
    "analyst": "analyst_descriptors",
    "action_pack": "action_pack_descriptors",
    "collection": "collection_descriptors",
}
KIND_TO_FAMILY = {v: k for k, v in FAMILY_TO_KIND.items()}

#: Families whose table carries a ``kind`` column.
_KIND_COLUMN = {"source", "analyst"}

DEFAULT_RUNS = 20
MAX_RUNS = 100


def _graph_sql() -> str:
    parts = []
    for fam, table in FAMILY_TABLES.items():
        kind = "kind" if fam in _KIND_COLUMN else "NULL::text"
        parts.append(
            f"SELECT '{fam}'::text AS family, descriptor_id, version, {kind} AS kind, state, "
            f"name, body FROM {table} WHERE is_head AND state IN ('active','paused')"
        )
    return "/* flow:graph_rows */ " + " UNION ALL ".join(parts)


GRAPH_ROWS_SQL = _graph_sql()

# Only the model name, cap and timeout leave this query — never a credential
# field of the component config.
STACK_SQL = """/* flow:stack */
SELECT component_id, state,
       body->'config'->'model_name'->>'raw' AS model,
       body->'config'->'max_tokens'->>'raw' AS max_tokens,
       body->'config'->'timeout_seconds'->>'raw' AS timeout_seconds
  FROM stack_components
 WHERE is_head AND kind = 'llm_provider'"""

RECENT_RUNS_SQL = """/* flow:recent_runs */
SELECT run_id, analyst_version, target_id, cadence_trigger, status, run_started_at, run_ended_at,
       llm_calls, jsonb_array_length(COALESCE(tool_calls,'[]'::jsonb)) AS n_tool_calls
  FROM analyst_traces
 WHERE analyst_id = $1
 ORDER BY run_started_at DESC
 LIMIT $2"""

LATEST_PROMPT_SQL = """/* flow:latest_prompt */
SELECT run_id, run_started_at, analyst_version, prompt_rendered, prompt_sha256,
       input_payload->'prompt_tail' AS prompt_tail
  FROM analyst_traces
 WHERE analyst_id = $1 AND prompt_rendered IS NOT NULL
 ORDER BY run_started_at DESC
 LIMIT 1"""

LATEST_STEPS_SQL = """/* flow:latest_steps */
SELECT run_id, run_started_at, status, intermediate_steps, output_row_refs
  FROM analyst_traces
 WHERE analyst_id = $1
 ORDER BY run_started_at DESC
 LIMIT 1"""

RUN_SQL = """/* flow:run */
SELECT run_id, analyst_id, analyst_version, target_id, cadence_trigger, status,
       run_started_at, run_ended_at, receipt_hash, prev_receipt_hash, prompt_sha256,
       intermediate_steps, llm_calls, tool_calls, input_row_refs, output_row_refs,
       prompt_rendered, input_payload, error_payload,
       octet_length(input_payload::text) AS input_payload_bytes,
       octet_length(output_payload::text) AS output_payload_bytes
  FROM analyst_traces WHERE run_id = $1"""

RUN_BODY_SQL = """/* flow:run_body */
SELECT run_id, intermediate_steps, llm_calls, tool_calls, prompt_rendered,
       input_payload, output_payload, error_payload
  FROM analyst_traces WHERE run_id = $1"""

OUTPUT_ROWS_SQL = """/* flow:output_rows */
SELECT 'analyst_outputs' AS "table", id, kind, title, schema_uri, produced_at, data,
       NULL::text[] AS honesty_flags, octet_length(data::text) AS bytes
  FROM analyst_outputs WHERE id = ANY($1::uuid[])
UNION ALL
SELECT 'journal_entries', id, entry_kind, title, schema_uri, produced_at, data,
       honesty_flags, octet_length(COALESCE(data::text,'') || COALESCE(body,''))
  FROM journal_entries WHERE id = ANY($1::uuid[])"""

CRITIQUES_SQL = """/* flow:critiques */
SELECT id, produced_at, data, data->>'analyzed_output_id' AS analyzed_output_id
  FROM analyst_outputs
 WHERE kind = 'critique' AND (data->>'analyzed_output_id') = ANY($1::text[])"""


def _versions_sql(table: str) -> str:
    return (f"/* flow:versions */ SELECT version, state, is_head, created_at FROM {table} "
            "WHERE descriptor_id = $1 ORDER BY created_at DESC LIMIT 50")


def _version_body_sql(table: str) -> str:
    return (f"/* flow:version_body */ SELECT descriptor_id, version, state, is_head, created_at, body "
            f"FROM {table} WHERE descriptor_id = $1 AND version = $2")


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def load_graph(conn: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(graph, stack_models)`` from the live heads."""
    rows = [dict(r) for r in await conn.fetch(GRAPH_ROWS_SQL)]
    stack = {
        str(r["component_id"]): {
            "model": r["model"], "state": r["state"],
            "max_tokens": _int_or_none(r["max_tokens"]),
            "timeout_seconds": _int_or_none(r["timeout_seconds"]),
        }
        for r in await conn.fetch(STACK_SQL)
    }
    return derive_graph(rows, stack_models=stack), stack


def _split_node_id(node_id: str) -> tuple[str | None, str]:
    kind, sep, rest = node_id.partition(":")
    if not sep:
        return None, node_id
    return KIND_TO_FAMILY.get(kind), rest


def _prompt_block(body: Mapping[str, Any], latest: Mapping[str, Any] | None) -> dict[str, Any]:
    method = body.get("method") if isinstance(body.get("method"), Mapping) else {}
    rendered = None
    if latest:
        text = latest.get("prompt_rendered")
        tail = latest.get("prompt_tail")
        if isinstance(tail, str):
            try:
                tail = json.loads(tail)
            except ValueError:
                tail = None
        rendered = {
            "run_id": str(latest.get("run_id")),
            "run_started_at": latest["run_started_at"].isoformat()
            if isinstance(latest.get("run_started_at"), datetime) else latest.get("run_started_at"),
            "analyst_version": latest.get("analyst_version"),
            "text": text,
            "chars_stored": len(text) if isinstance(text, str) else 0,
            "truncated": isinstance(text, str) and "...[TRUNCATED:" in text,
            "tail": tail.get("text") if isinstance(tail, Mapping) else None,
            "total_chars": tail.get("total_chars") if isinstance(tail, Mapping) else (
                len(text) if isinstance(text, str) else None),
            "sha256": latest.get("prompt_sha256"),
            "basis": ("analyst_traces.prompt_rendered of the most recent run that recorded one: the "
                      "LAST LLM call's prompt before the trace write, code-injected preambles included "
                      "(data/run_accounting.current_prompt_rendered). Head-capped at 32,000 chars; the "
                      "last 4,000 are input_payload.prompt_tail; sha256 is over the full text."),
        }
    return {
        "declared": {
            "system_prompt": method.get("system_prompt"),
            "prompt_module": method.get("prompt_module"),
            "bounded_question": method.get("bounded_question"),
            "note": ("method.system_prompt is the descriptor's own text; method.prompt_module names "
                     "code that holds it. Either is the template, before the runtime adds the house "
                     "preamble, grounding and the slice."),
        },
        "rendered": rendered,
        "fresh_render": ("not produced: a fresh render needs the runtime (live slice, grounding, prior "
                         "reads). scripts/render_prompt_pack.py replays one offline."),
    }


def _output_contract(body: Mapping[str, Any], observed: list[dict]) -> dict[str, Any]:
    ident = body.get("identity") if isinstance(body.get("identity"), Mapping) else {}
    return {
        "type_signature": ident.get("type_signature"),
        "outputs": body.get("outputs"),
        "observed_rows": observed,
        "note": ("type_signature names Python types; the row payload model lives in code "
                 "(data/provenance/kinds.py by kind). observed_rows are the latest run's "
                 "output rows: table, kind and schema_uri as written."),
    }


def build_flow_router(deps: RegistryAPIDeps) -> APIRouter:
    """Read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["flow"])

    def _pg() -> Any:
        return deps.descriptor_registry.pg

    @router.get("/flow/graph")
    async def flow_graph(
        scope: str = Query("all", max_length=300),
        depth: int = Query(1, ge=1, le=3),
        fields: bool = Query(True, description="false omits each node's full body (the node route has it)"),
        principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        async with _pg().acquire() as conn:
            graph, _ = await load_graph(conn)
        t1 = time.perf_counter()
        try:
            scoped = apply_scope(graph, scope, depth=depth)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if not fields:
            scoped["nodes"] = [{**n, "fields": None} for n in scoped["nodes"]]
        return {
            **scoped,
            "fields_included": fields,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "timings_ms": {"load_and_derive": round((t1 - t0) * 1000, 1),
                           "total": round((time.perf_counter() - t0) * 1000, 1)},
        }

    @router.get("/flow/node/{node_id:path}")
    async def flow_node(
        node_id: str,
        runs: int = Query(DEFAULT_RUNS, ge=1, le=MAX_RUNS),
        principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        async with _pg().acquire() as conn:
            graph, stack = await load_graph(conn)
            node = next((n for n in graph["nodes"] if n["id"] == node_id), None)
            if node is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"no node {node_id!r} in the graph")
            out: dict[str, Any] = {
                "node": node,
                "edges_in": [e for e in graph["edges"] if e["target"] == node_id],
                "edges_out": [e for e in graph["edges"] if e["source"] == node_id],
            }
            family, did = _split_node_id(node_id)
            if family in FAMILY_TABLES and node.get("descriptor_id"):
                out["versions"] = [
                    {"version": r["version"], "state": r["state"], "is_head": r["is_head"],
                     "created_at": r["created_at"].isoformat()
                     if isinstance(r["created_at"], datetime) else r["created_at"]}
                    for r in await conn.fetch(_versions_sql(FAMILY_TABLES[family]), did)
                ]
            if family == "analyst":
                body = node["fields"] if isinstance(node["fields"], Mapping) else {}
                latest = await conn.fetchrow(LATEST_PROMPT_SQL, did)
                recent = [dict(r) for r in await conn.fetch(RECENT_RUNS_SQL, did, runs)]
                steps_row = await conn.fetchrow(LATEST_STEPS_SQL, did)
                observed_rows: list[dict] = []
                if steps_row and steps_row["output_row_refs"]:
                    for r in await conn.fetch(OUTPUT_ROWS_SQL, list(steps_row["output_row_refs"])):
                        observed_rows.append({"table": r["table"], "id": str(r["id"]),
                                              "kind": r["kind"], "schema_uri": r["schema_uri"]})
                out["prompt"] = _prompt_block(body, dict(latest) if latest else None)
                out["output_contract"] = _output_contract(body, observed_rows)
                out["params"] = sampling_params(str(did), body, stack)
                out["params"]["observed"] = {
                    "label": "observed in the last runs' analyst_traces.llm_calls",
                    "calls": observed_calls(recent),
                }
                out["recent_runs"] = [run_summary(r) for r in recent]
                out["observed_steps"] = (
                    {
                        "label": "observed, not declared",
                        "run_id": str(steps_row["run_id"]),
                        "status": steps_row["status"],
                        "steps": step_summaries(steps_row["intermediate_steps"]),
                    }
                    if steps_row else None
                )
        return out

    @router.get("/flow/version/{node_id:path}")
    async def flow_version(
        node_id: str,
        version: str = Query(..., min_length=4, max_length=128),
        principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        family, did = _split_node_id(node_id)
        if family not in FAMILY_TABLES:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{node_id!r} is not a descriptor node")
        async with _pg().acquire() as conn:
            row = await conn.fetchrow(_version_body_sql(FAMILY_TABLES[family]), did, version)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no version {version!r} of {node_id!r}")
        body = row["body"]
        if isinstance(body, str):
            body = json.loads(body)
        return {
            "node_id": node_id, "version": row["version"], "state": row["state"],
            "is_head": row["is_head"],
            "created_at": row["created_at"].isoformat()
            if isinstance(row["created_at"], datetime) else row["created_at"],
            "fields": body,
        }

    def _uuid(run_id: str) -> UUID:
        try:
            return UUID(run_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "run_id is not a uuid") from exc

    @router.get("/flow/run/{run_id}")
    async def flow_run(run_id: str, principal: str = Depends(require_bearer)) -> dict[str, Any]:
        rid = _uuid(run_id)
        async with _pg().acquire() as conn:
            row = await conn.fetchrow(RUN_SQL, rid)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"no run {run_id}")
            row = dict(row)
            refs = list(row.get("output_row_refs") or [])
            outputs = [dict(r) for r in await conn.fetch(OUTPUT_ROWS_SQL, refs)] if refs else []
            crit = ([dict(r) for r in await conn.fetch(CRITIQUES_SQL, [str(x) for x in refs])]
                    if refs else [])
        return run_detail(row, outputs, crit)

    @router.get("/flow/run/{run_id}/body")
    async def flow_run_body(
        run_id: str,
        part: str = Query(...),
        index: int | None = Query(None, ge=0),
        principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        rid = _uuid(run_id)
        if part not in BODY_PARTS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"part must be one of {', '.join(BODY_PARTS)}")
        async with _pg().acquire() as conn:
            row = await conn.fetchrow(RUN_BODY_SQL, rid)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no run {run_id}")
        try:
            return body_part(dict(row), part, index)
        except IndexError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no {part} at index {index}") from exc

    return router


__all__ = ["FAMILY_TABLES", "build_flow_router", "load_graph"]
