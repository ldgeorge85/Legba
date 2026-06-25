# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime + analyst telemetry endpoints for the P-7/P-9/P-10 panels.

Designed per `plans/legba_panels_redesign_2026_05_28.md`. Powers:

  * **P-7 Target Detail** (Overview, Sources, Signals tabs) — needs the
    actor's lifecycle, last-run state, and per-source cursors.
  * **P-9 Analyst Roster** — needs analyst-actor state plus a 7-day
    activity window aggregated from ``analyst_traces``.
  * **P-10 Analyst Detail** — needs per-analyst run history, output
    history, and critique history (the analyst as analysand, not judge).

All five endpoints are mounted on the same router so the registry server
can include them next to the v3 telemetry surface. To wire them into
the registry app, add the following to ``server.py`` next to the
existing v3 include::

    from .runtime_telemetry_api import build_runtime_telemetry_router
    app.include_router(
        build_runtime_telemetry_router(deps),
        prefix="/api/v1",
    )

(``server.py`` is intentionally **not** modified here; the integration
note will land in the parent session.)

Design notes:

  * Reads live substrate state via ``deps.descriptor_registry.pg`` —
    same pattern as ``v3_api.py``. No mocks, no synthesised metrics.

  * ``analyst_traces`` carries ``run_started_at`` / ``run_ended_at``
    (NOT ``started_at`` / ``finished_at``) and has no ``duration_ms``
    or ``token_cost`` column. We compute:

      - ``duration_ms`` from the two timestamps (null if either is null).
      - ``token_count`` by summing ``prompt_tokens + completion_tokens +
        reasoning_tokens`` across the ``llm_calls`` JSONB array.

    The roster's ``avg_token_count_7d`` is the average of that derived
    per-trace sum — not a dollar cost. We rename the field from the
    panel spec's ``avg_token_cost`` to ``avg_token_count_7d`` because
    converting tokens to USD requires a model-price lookup that isn't
    available row-by-row (``budget_ledger`` aggregates per day).

  * Cursor pagination is base64-encoded ``"{iso_ts}|{uuid}"`` — same
    shape as the E-1 paginated reads. Forward-only DESC ordering.

  * The /critiques endpoint joins ``analyst_critiques`` through
    ``analyst_traces`` so we can filter by ``analyzed analyst_id`` —
    that's the analyst whose trace was judged, **not** the judge. This
    matches the panel-spec's intent ("rows where this analyst is the
    analyzed analyst, NOT the critic").
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TargetSourceRuntime(BaseModel):
    """One source binding's runtime cursor state.

    Mirrors ``SourceCursor`` (``src/legba/runtime/state.py``) unpacked
    from ``actor_state.source_cursors`` JSONB. ``descriptor_kind`` and
    ``descriptor_enabled`` come from the corresponding ``SourceBinding``
    entry in the active descriptor body when one matches by id; null
    when the cursor exists but the active descriptor has dropped the
    binding (which can happen during a version-up).
    """

    source_id: str
    last_pulled_at: datetime | None
    rows_pulled: int
    last_error: str | None
    descriptor_kind: str | None = None
    descriptor_enabled: bool | None = None


class TargetActiveDescriptor(BaseModel):
    """Compact summary of the target's active descriptor head."""

    descriptor_id: str
    version: str
    schema_uri: str
    state: str
    name: str
    abstraction_level: str | None
    source_count: int


class TargetRuntimeRow(BaseModel):
    """Per-actor row for ``GET /targets/{id}/runtime``.

    Mirrors ``actor_state`` columns for the target actor identified by
    descriptor_id. Multiple rows can exist for the same descriptor_id
    if the target has been re-versioned (each version gets its own
    actor_id per ``runtime.reconcile._default_actor_id``); the response
    returns them all so the UI can show the version cut-over.
    """

    actor_id: str
    actor_kind: str
    descriptor_id: str
    descriptor_version: str
    lifecycle: str
    last_run_at: datetime | None
    last_outcome: str | None
    cooldown_until: datetime | None
    error_count: int
    last_error: str | None
    updated_at: datetime
    sources: list[TargetSourceRuntime] = Field(default_factory=list)


class TargetRuntimeOut(BaseModel):
    """Envelope for ``GET /targets/{id}/runtime``."""

    descriptor_id: str
    active_descriptor: TargetActiveDescriptor | None
    actors: list[TargetRuntimeRow]


class AnalystRuntimeRow(BaseModel):
    """Per-analyst row for ``GET /analysts/runtime``.

    Combines one ``actor_state`` row (analyst_kind='analyst') with a
    7-day window aggregate over ``analyst_traces``. When the analyst
    has no traces in the window the metrics are zero (not null) and
    the row is still emitted.
    """

    actor_id: str
    descriptor_id: str
    descriptor_version: str
    lifecycle: str
    last_run_at: datetime | None
    last_outcome: str | None
    cooldown_until: datetime | None
    error_count: int
    last_error: str | None
    updated_at: datetime
    # 7-day window aggregates from analyst_traces.
    runs_7d: int
    success_count_7d: int
    failed_count_7d: int
    avg_token_count_7d: float  # mean of llm_calls token-sum across the window
    last_trace_at: datetime | None


class AnalystRunRow(BaseModel):
    """One row of ``GET /analysts/{id}/runs``.

    Derived from ``analyst_traces``. ``duration_ms`` is computed from
    ``run_started_at`` + ``run_ended_at`` (null if either is null).
    ``token_count`` is summed over the ``llm_calls`` JSONB array.
    ``output_count`` is a COUNT subquery against ``analyst_outputs``.
    """

    run_id: str
    analyst_id: str
    analyst_version: str
    target_id: str | None
    cadence_trigger: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    token_count: int
    output_count: int


class AnalystOutputRow(BaseModel):
    """One row of ``GET /analysts/{id}/outputs``.

    Mirrors ``analyst_outputs`` columns. ``data`` carries the kind-
    specific payload (the writer fills it from each ``OutputKind`` shape).
    """

    id: str
    kind: str
    title: str
    body: str
    confidence: float
    severity: str | None
    target_id: str | None
    target_version: str | None
    analyst_id: str | None
    analyst_version: str | None
    run_id: str | None
    produced_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class AnalystCritiqueRow(BaseModel):
    """One row of ``GET /analysts/{id}/critiques`` — the *analyzed*
    analyst's critique history.

    Mirrors ``analyst_critiques`` plus the analyzed-analyst pointer
    pulled through ``analyst_traces`` (the table doesn't store it
    directly — it's the joined trace's ``analyst_id``).
    """

    id: str
    trace_id: str
    analyzed_analyst_id: str          # FROM analyst_traces.analyst_id
    analyzed_analyst_version: str     # FROM analyst_traces.analyst_version
    judge_analyst_id: str
    judge_analyst_version: str
    rubric_uri: str
    scores: dict[str, Any] = Field(default_factory=dict)
    overall_score: float | None
    revision_delta: dict[str, Any] | None
    produced_at: datetime


class CursorPage(BaseModel):
    """Pagination envelope for /runs, /outputs, /critiques."""

    items: list[Any]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _encode_cursor(ts: datetime, row_id: str) -> str:
    """Base64-encode ``(timestamp, id)`` for forward-only DESC pagination."""
    raw = f"{ts.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a cursor back into ``(timestamp, id)``.

    Raises ``HTTPException(400)`` on malformed input — same shape as the
    other registry endpoints' validation errors.
    """
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding).decode()
        ts_part, _, id_part = raw.partition("|")
        if not ts_part or not id_part:
            raise ValueError("missing timestamp or id segment")
        ts = datetime.fromisoformat(ts_part)
        return ts, id_part
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cursor: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# JSONB helpers
# ---------------------------------------------------------------------------


def _as_dict(raw: Any) -> dict[str, Any]:
    """Coerce a JSONB column value into a dict.

    asyncpg returns ``str`` for JSONB by default; codec setup may return
    ``dict``. We accept either.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _sum_llm_call_tokens(llm_calls_raw: Any) -> int:
    """Sum prompt + completion + reasoning tokens across an llm_calls JSONB.

    Each entry is expected to carry numeric ``prompt_tokens`` /
    ``completion_tokens`` / ``reasoning_tokens`` keys (matching what the
    deterministic + LLM-method analysts emit per the usage_dict pattern
    in `meta_findings_synthesizer.py` etc.). Missing keys → 0. Non-list
    or non-dict entries are ignored.
    """
    total = 0
    for entry in _as_list(llm_calls_raw):
        if not isinstance(entry, dict):
            continue
        # Walk the common shapes: usage dict nested under .usage, or
        # at the top level — analysts use both depending on whether the
        # llm response was serialized whole or just its usage block.
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else entry
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                total += int(value)
    return total


def _compute_duration_ms(
    started_at: datetime | None, ended_at: datetime | None,
) -> int | None:
    if started_at is None or ended_at is None:
        return None
    delta = ended_at - started_at
    return int(delta.total_seconds() * 1000)


def _source_cursor_entries(raw: Any) -> dict[str, dict[str, Any]]:
    """Unpack the actor_state.source_cursors JSONB into per-source dicts."""
    data = _as_dict(raw)
    out: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[key] = value
    return out


def _parse_iso_or_none(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_runtime_telemetry_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the runtime-telemetry router bound to the registry deps."""
    router = APIRouter(tags=["runtime-telemetry"])

    # ----------------------------------------------------------------------
    # /targets/{id}/runtime
    # ----------------------------------------------------------------------

    @router.get(
        "/targets/{target_id}/runtime",
        response_model=TargetRuntimeOut,
    )
    async def get_target_runtime(
        target_id: str,
        principal: str = Depends(require_bearer),
    ) -> TargetRuntimeOut:
        async with deps.descriptor_registry.pg.acquire() as conn:
            # All actor_state rows for this target descriptor.
            actor_rows = await conn.fetch(
                """
                SELECT actor_id, actor_kind, descriptor_id, descriptor_version,
                       lifecycle, last_run_at, last_outcome, cooldown_until,
                       error_count, last_error, source_cursors, updated_at
                  FROM public.actor_state
                 WHERE descriptor_id = $1
                   AND actor_kind = 'target'
                 ORDER BY updated_at DESC
                """,
                target_id,
            )

            # Active descriptor head — for the source-binding summary.
            head_row = await conn.fetchrow(
                """
                SELECT descriptor_id, version, schema_uri, state, name,
                       abstraction_level, body
                  FROM target_descriptors
                 WHERE descriptor_id = $1
                   AND is_head = TRUE
                """,
                target_id,
            )

        active: TargetActiveDescriptor | None = None
        binding_index: dict[str, dict[str, Any]] = {}
        if head_row is not None:
            body = _as_dict(head_row["body"])
            sources_list = body.get("sources") if isinstance(body, dict) else None
            sources_list = sources_list if isinstance(sources_list, list) else []
            for entry in sources_list:
                if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                    binding_index[entry["id"]] = entry
            active = TargetActiveDescriptor(
                descriptor_id=head_row["descriptor_id"],
                version=head_row["version"],
                schema_uri=head_row["schema_uri"],
                state=str(head_row["state"]),
                name=head_row["name"],
                abstraction_level=head_row["abstraction_level"],
                source_count=len(sources_list),
            )

        actors: list[TargetRuntimeRow] = []
        for ar in actor_rows:
            sources_raw = _source_cursor_entries(ar["source_cursors"])
            sources: list[TargetSourceRuntime] = []
            for source_id, cursor_entry in sources_raw.items():
                binding = binding_index.get(source_id)
                sources.append(TargetSourceRuntime(
                    source_id=source_id,
                    last_pulled_at=_parse_iso_or_none(
                        cursor_entry.get("last_pulled_at"),
                    ),
                    rows_pulled=int(cursor_entry.get("rows_pulled", 0) or 0),
                    last_error=cursor_entry.get("last_error"),
                    descriptor_kind=(
                        binding.get("kind") if isinstance(binding, dict) else None
                    ),
                    descriptor_enabled=(
                        bool(binding.get("enabled", True))
                        if isinstance(binding, dict) else None
                    ),
                ))
            sources.sort(key=lambda s: s.source_id)
            actors.append(TargetRuntimeRow(
                actor_id=ar["actor_id"],
                actor_kind=ar["actor_kind"],
                descriptor_id=ar["descriptor_id"],
                descriptor_version=ar["descriptor_version"],
                lifecycle=ar["lifecycle"],
                last_run_at=ar["last_run_at"],
                last_outcome=ar["last_outcome"],
                cooldown_until=ar["cooldown_until"],
                error_count=int(ar["error_count"]),
                last_error=ar["last_error"],
                updated_at=ar["updated_at"],
                sources=sources,
            ))

        return TargetRuntimeOut(
            descriptor_id=target_id,
            active_descriptor=active,
            actors=actors,
        )

    # ----------------------------------------------------------------------
    # /analysts/runtime
    # ----------------------------------------------------------------------

    @router.get(
        "/analysts/runtime",
        response_model=list[AnalystRuntimeRow],
    )
    async def list_analyst_runtime(
        principal: str = Depends(require_bearer),
    ) -> list[AnalystRuntimeRow]:
        window_start = datetime.now(tz=timezone.utc) - timedelta(days=7)

        async with deps.descriptor_registry.pg.acquire() as conn:
            # Pull actor_state rows for analyst actors plus, in the same
            # roundtrip, all in-window traces. We aggregate in Python
            # because the per-trace token sum requires unpacking the
            # llm_calls JSONB array (which we can do here without a
            # second-level CTE per analyst).
            actor_rows = await conn.fetch(
                """
                SELECT actor_id, descriptor_id, descriptor_version, lifecycle,
                       last_run_at, last_outcome, cooldown_until, error_count,
                       last_error, updated_at
                  FROM public.actor_state
                 WHERE actor_kind = 'analyst'
                 ORDER BY updated_at DESC
                """
            )
            trace_rows = await conn.fetch(
                """
                SELECT analyst_id, status, llm_calls, run_started_at
                  FROM analyst_traces
                 WHERE run_started_at >= $1
                """,
                window_start,
            )

        # Build per-analyst trace aggregates keyed by descriptor_id (which
        # is the same string analyst_id used in analyst_traces).
        agg: dict[str, dict[str, Any]] = {}
        for tr in trace_rows:
            aid = tr["analyst_id"]
            slot = agg.setdefault(aid, {
                "runs": 0,
                "success": 0,
                "failed": 0,
                "tokens_sum": 0,
                "last_trace_at": None,
            })
            slot["runs"] += 1
            if tr["status"] == "success":
                slot["success"] += 1
            else:
                slot["failed"] += 1
            slot["tokens_sum"] += _sum_llm_call_tokens(tr["llm_calls"])
            ts = tr["run_started_at"]
            if slot["last_trace_at"] is None or (
                ts is not None and ts > slot["last_trace_at"]
            ):
                slot["last_trace_at"] = ts

        out: list[AnalystRuntimeRow] = []
        for ar in actor_rows:
            slot = agg.get(ar["descriptor_id"])
            runs = int(slot["runs"]) if slot else 0
            success = int(slot["success"]) if slot else 0
            failed = int(slot["failed"]) if slot else 0
            tokens_sum = int(slot["tokens_sum"]) if slot else 0
            last_trace_at = slot["last_trace_at"] if slot else None
            avg_tokens = (tokens_sum / runs) if runs > 0 else 0.0
            out.append(AnalystRuntimeRow(
                actor_id=ar["actor_id"],
                descriptor_id=ar["descriptor_id"],
                descriptor_version=ar["descriptor_version"],
                lifecycle=ar["lifecycle"],
                last_run_at=ar["last_run_at"],
                last_outcome=ar["last_outcome"],
                cooldown_until=ar["cooldown_until"],
                error_count=int(ar["error_count"]),
                last_error=ar["last_error"],
                updated_at=ar["updated_at"],
                runs_7d=runs,
                success_count_7d=success,
                failed_count_7d=failed,
                avg_token_count_7d=float(avg_tokens),
                last_trace_at=last_trace_at,
            ))
        return out

    # ----------------------------------------------------------------------
    # /analysts/{id}/runs
    # ----------------------------------------------------------------------

    @router.get(
        "/analysts/{analyst_id}/runs",
        response_model=CursorPage,
    )
    async def list_analyst_runs(
        analyst_id: str,
        since: datetime | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> CursorPage:
        clauses = ["analyst_id = $1"]
        args: list[Any] = [analyst_id]
        if since is not None:
            args.append(since)
            clauses.append(f"run_started_at >= ${len(args)}")
        if cursor is not None:
            ts, cur_id = _decode_cursor(cursor)
            try:
                cur_uuid = UUID(cur_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="cursor id segment is not a UUID",
                ) from exc
            args.append(ts)
            ts_idx = len(args)
            args.append(cur_uuid)
            id_idx = len(args)
            clauses.append(
                f"(run_started_at, run_id) < (${ts_idx}, ${id_idx})"
            )

        args.append(limit + 1)
        limit_idx = len(args)
        where = " AND ".join(clauses)

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT t.run_id, t.analyst_id, t.analyst_version,
                       t.target_id, t.cadence_trigger, t.status,
                       t.run_started_at, t.run_ended_at, t.llm_calls,
                       (SELECT COUNT(*) FROM analyst_outputs ao
                          WHERE ao.run_id = t.run_id) AS output_count
                  FROM analyst_traces t
                 WHERE {where}
                 ORDER BY t.run_started_at DESC, t.run_id DESC
                 LIMIT ${limit_idx}
                """,
                *args,
            )

        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items: list[AnalystRunRow] = []
        for r in page_rows:
            items.append(AnalystRunRow(
                run_id=str(r["run_id"]),
                analyst_id=r["analyst_id"],
                analyst_version=r["analyst_version"],
                target_id=r["target_id"],
                cadence_trigger=r["cadence_trigger"],
                status=r["status"],
                started_at=r["run_started_at"],
                ended_at=r["run_ended_at"],
                duration_ms=_compute_duration_ms(
                    r["run_started_at"], r["run_ended_at"],
                ),
                token_count=_sum_llm_call_tokens(r["llm_calls"]),
                output_count=int(r["output_count"] or 0),
            ))
        next_cursor: str | None = None
        if has_more and items:
            last = page_rows[-1]
            next_cursor = _encode_cursor(
                last["run_started_at"], str(last["run_id"]),
            )
        return CursorPage(items=items, next_cursor=next_cursor)

    # ----------------------------------------------------------------------
    # /analysts/{id}/outputs
    # ----------------------------------------------------------------------

    @router.get(
        "/analysts/{analyst_id}/outputs",
        response_model=CursorPage,
    )
    async def list_analyst_outputs(
        analyst_id: str,
        since: datetime | None = Query(default=None),
        kind: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> CursorPage:
        clauses = ["analyst_id = $1"]
        args: list[Any] = [analyst_id]
        if since is not None:
            args.append(since)
            clauses.append(f"produced_at >= ${len(args)}")
        if kind is not None:
            args.append(kind)
            clauses.append(f"kind = ${len(args)}")
        if cursor is not None:
            ts, cur_id = _decode_cursor(cursor)
            try:
                cur_uuid = UUID(cur_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="cursor id segment is not a UUID",
                ) from exc
            args.append(ts)
            ts_idx = len(args)
            args.append(cur_uuid)
            id_idx = len(args)
            clauses.append(
                f"(produced_at, id) < (${ts_idx}, ${id_idx})"
            )

        args.append(limit + 1)
        limit_idx = len(args)
        where = " AND ".join(clauses)

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, kind, title, body, confidence, severity,
                       target_id, target_version, analyst_id, analyst_version,
                       run_id, produced_at, data
                  FROM analyst_outputs
                 WHERE {where}
                 ORDER BY produced_at DESC, id DESC
                 LIMIT ${limit_idx}
                """,
                *args,
            )

        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items: list[AnalystOutputRow] = []
        for r in page_rows:
            items.append(AnalystOutputRow(
                id=str(r["id"]),
                kind=r["kind"],
                title=r["title"],
                body=r["body"],
                confidence=float(r["confidence"]),
                severity=r["severity"],
                target_id=r["target_id"],
                target_version=r["target_version"],
                analyst_id=r["analyst_id"],
                analyst_version=r["analyst_version"],
                run_id=str(r["run_id"]) if r["run_id"] is not None else None,
                produced_at=r["produced_at"],
                data=_as_dict(r["data"]),
            ))
        next_cursor: str | None = None
        if has_more and items:
            last = page_rows[-1]
            next_cursor = _encode_cursor(
                last["produced_at"], str(last["id"]),
            )
        return CursorPage(items=items, next_cursor=next_cursor)

    # ----------------------------------------------------------------------
    # /analysts/{id}/critiques
    # ----------------------------------------------------------------------

    @router.get(
        "/analysts/{analyst_id}/critiques",
        response_model=CursorPage,
    )
    async def list_analyst_critiques(
        analyst_id: str,
        since: datetime | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> CursorPage:
        # Filter is on the *analyzed* analyst: join through traces so we
        # can pin analyst_traces.analyst_id == $1 (the run that was judged).
        clauses = ["t.analyst_id = $1"]
        args: list[Any] = [analyst_id]
        if since is not None:
            args.append(since)
            clauses.append(f"c.produced_at >= ${len(args)}")
        if cursor is not None:
            ts, cur_id = _decode_cursor(cursor)
            try:
                cur_uuid = UUID(cur_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="cursor id segment is not a UUID",
                ) from exc
            args.append(ts)
            ts_idx = len(args)
            args.append(cur_uuid)
            id_idx = len(args)
            clauses.append(
                f"(c.produced_at, c.id) < (${ts_idx}, ${id_idx})"
            )

        args.append(limit + 1)
        limit_idx = len(args)
        where = " AND ".join(clauses)

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT c.id, c.trace_id, c.judge_analyst_id,
                       c.judge_analyst_version, c.rubric_uri, c.scores,
                       c.overall_score, c.revision_delta, c.produced_at,
                       t.analyst_id     AS analyzed_analyst_id,
                       t.analyst_version AS analyzed_analyst_version
                  FROM analyst_critiques c
                  JOIN analyst_traces t ON t.run_id = c.trace_id
                 WHERE {where}
                 ORDER BY c.produced_at DESC, c.id DESC
                 LIMIT ${limit_idx}
                """,
                *args,
            )

        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items: list[AnalystCritiqueRow] = []
        for r in page_rows:
            revision_delta = r["revision_delta"]
            if revision_delta is not None:
                revision_delta = _as_dict(revision_delta)
            items.append(AnalystCritiqueRow(
                id=str(r["id"]),
                trace_id=str(r["trace_id"]),
                analyzed_analyst_id=r["analyzed_analyst_id"],
                analyzed_analyst_version=r["analyzed_analyst_version"],
                judge_analyst_id=r["judge_analyst_id"],
                judge_analyst_version=r["judge_analyst_version"],
                rubric_uri=r["rubric_uri"],
                scores=_as_dict(r["scores"]),
                overall_score=(
                    float(r["overall_score"])
                    if r["overall_score"] is not None else None
                ),
                revision_delta=revision_delta,
                produced_at=r["produced_at"],
            ))
        next_cursor: str | None = None
        if has_more and items:
            last = page_rows[-1]
            next_cursor = _encode_cursor(
                last["produced_at"], str(last["id"]),
            )
        return CursorPage(items=items, next_cursor=next_cursor)

    return router


__all__ = [
    "AnalystCritiqueRow",
    "AnalystOutputRow",
    "AnalystRunRow",
    "AnalystRuntimeRow",
    "CursorPage",
    "TargetActiveDescriptor",
    "TargetRuntimeOut",
    "TargetRuntimeRow",
    "TargetSourceRuntime",
    "build_runtime_telemetry_router",
]
