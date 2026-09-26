# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``inquiry_state`` pack — the inquiry kind's OWN continuity ledger.

Program 5 lane 1 (planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §3). An
inquiry is STATEFUL (§1): it carries hypotheses with a resolution test each,
questions it has dispatched into the platform's own machinery, and
observations it expects a desk to later carry forward. ``inquiry_ledger``
(migration 0215) is the one table; this pack is its ONLY read/write surface
for an analyst — three tools, every one scoped to the CALLING descriptor:

  * ``ledger_read(status?)`` — the calling inquiry's own rows, newest first.
  * ``ledger_write(kind, text, resolution_test?, resolves_by?, dispatched_to?)``
    — insert ONE open row under the calling descriptor's own id. A
    ``hypothesis`` with no ``resolution_test`` is refused (the H13
    sealed-ledger discipline: a test frozen at write, never softened later) —
    enforced BOTH here (a friendly ``failed`` ToolResult) AND by the DB CHECK
    ``inquiry_ledger_hypothesis_needs_test``, so the discipline holds even
    against a caller that bypasses this handler.
  * ``ledger_close(id, status, reason, cited_refs)`` — close ONE of the
    calling descriptor's own rows: stamps the terminal status + ``closed_at``,
    appends ``reason`` to the row's own ``text`` (the ledger carries no
    separate reason column — the close narrative rides the same append-only
    field the row was opened with), and sets ``cited_refs``. That is the
    writer of the CLOSING ``cited_refs`` — ``ledger_write`` may set the opening refs and this tool replaces them; ``ledger_write`` never
    takes it — so an entry's citations are always the CLOSE-time warrant, per
    §4's "an observation whose cited_refs a later finding cites".

THE SCOPE FENCE (inquiry_state.py:scope_fence). Every tool is scoped to
``ctx.writeback.analyst_ctx.analyst_id`` — the CALLING descriptor's own
per-run identity, the SAME channel :mod:`.journal_propose` stamps every
proposal with (``ctx = wb.analyst_ctx``; see its ``_insert_proposal``). Never
a caller-supplied argument:

  * ``ledger_read`` / ``ledger_close`` filter every statement on
    ``descriptor_id = $calling_id`` — a row that exists under a DIFFERENT
    inquiry's descriptor_id is invisible to (and un-closable by) this one, not
    by omission, by a WHERE clause on every statement.
  * ``ledger_write`` INSERTs with ``descriptor_id`` set to that SAME identity,
    never a value the caller could pass — an inquiry cannot mint a row under
    another inquiry's name either.

``ledger_close`` cannot tell a caller WHY an id was refused (unknown id vs.
already closed vs. belongs to another inquiry) — deliberately: distinguishing
those would leak whether another inquiry's row exists.

WHY A SEPARATE PACK (not folding into ``journal_propose`` / ``substrate_read``):
the same §7.6 grant invariant this tree already holds ``journal_propose`` to —
a pack's tools must all share ONE write surface, so granting it can never
become an implicit write-anything grant. ``inquiry_state`` writes ONLY
``inquiry_ledger``; it never touches facts / nexuses / hypotheses / journal
tables. Dispatch itself (raising an open question, filing a contention) is
NOT a tool of this pack — §3: "the inquiry uses the EXISTING journal_propose
shapes"; a ``question`` row here only RECORDS where (``dispatched_to``), so
this pack can never itself spend anything.

FOUR-SURFACE convergence (memory: consult-tools-must-be-pack-tools), the same
discipline :mod:`.journal_read` / :mod:`.journal_propose` already hold:

  1. the in-code TUPLE (``INQUIRY_STATE_TOOLS``) — below;
  2. the DESCRIPTOR (``descriptors/action_pack_inquiry_state.yaml``);
  3. the HANDLERS (``register_inquiry_state_tools``) — below;
  4. every tool a bound inquiry can dispatch ∈ this pack (lane 2's kind
     module reaches these only through :class:`legba.data.analysts.agency.Agency`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from ...schemas.action_pack import ActionPack
from .tools import ToolCall, ToolContext, ToolResult, WritebackContext

logger = logging.getLogger(__name__)

INQUIRY_STATE_PACK_ID = "inquiry_state"

# Keep this tuple == the descriptor's `tools` names == the handlers registered
# below (the per-pack drift guard the journal packs already carry — see
# tests/data_pkg/test_inquiry_state_pack.py).
INQUIRY_STATE_TOOLS = (
    "ledger_read",
    "ledger_write",
    "ledger_close",
)

#: migration 0215's `inquiry_ledger_kind_vocab` CHECK, mirrored so a bad kind
#: fails here with a readable message before it ever reaches the DB.
KIND_VOCAB: tuple[str, ...] = ("hypothesis", "question", "observation", "expectation")

#: migration 0215's `inquiry_ledger_status_vocab` CHECK, mirrored likewise.
STATUS_VOCAB: tuple[str, ...] = (
    "open", "confirmed", "refuted", "answered", "expired", "withdrawn",
)

#: The statuses `ledger_close` may SET. 'open' is the un-closed state — the
#: DB's own `inquiry_ledger_closed_at_matches_status` CHECK ties it 1:1 to a
#: NULL `closed_at`, so "closing" a row TO 'open' is refused here, before it
#: ever reaches that CHECK.
_CLOSED_STATUSES: tuple[str, ...] = tuple(s for s in STATUS_VOCAB if s != "open")

_MAX_TEXT_CHARS = 8192
_MAX_REASON_CHARS = 4096
#: A read is bounded so a runaway inquiry can never pull an unbounded ledger
#: into one tool result — mirrors the `_pos_int` caps the deterministic
#: catalog uses elsewhere.
_READ_LIMIT = 200


def _writeback(ctx: ToolContext) -> WritebackContext | None:
    """The per-run write surface a ledger tool needs — mirrors
    :func:`legba.data.analysts.agency.journal_propose._writeback`. Absent →
    a clean ``failed`` ToolResult (no silent no-op, no un-scoped write)."""
    wb = ctx.writeback
    if wb is None or wb.pg_pool is None or wb.analyst_ctx is None:
        return None
    return wb


def _calling_descriptor_id(wb: WritebackContext) -> str:
    """THE SCOPE FENCE (inquiry_state.py:scope_fence). Every ledger row is
    scoped to the descriptor that is ACTUALLY RUNNING this call — read off
    the per-run provenance identity the runtime stamps, never a
    caller-supplied argument — so one inquiry can never read, write under, or
    close another inquiry's rows."""
    return wb.analyst_ctx.analyst_id


def _coerce_cited_refs(raw: Any) -> tuple[list[str] | None, str | None]:
    """Parse the OPTIONAL ``cited_refs`` arg (``ledger_close`` only) into a
    JSON-safe list of strings. Accepts a list or a JSON-string-encoded list;
    absent/``None`` means "no refs" (an empty array, not a skipped column —
    ``ledger_write`` never sets this column at all, so ``ledger_close`` is
    the first and only write it ever gets)."""
    if raw is None:
        return [], None
    items = raw
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError as exc:
            return None, f"cited_refs is not valid JSON: {exc}"
    if not isinstance(items, (list, tuple)):
        return None, "cited_refs must be a list"
    out: list[str] = []
    for item in items:
        s = str(item).strip()
        if s:
            out.append(s)
    return out, None


def _coerce_resolves_by(raw: Any) -> tuple[datetime | None, str | None]:
    """Parse the OPTIONAL ``resolves_by`` arg into a timestamp. Absent/``None``
    → no deadline."""
    if raw is None:
        return None, None
    if isinstance(raw, datetime):
        return raw, None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")), None
    except ValueError as exc:
        return None, f"resolves_by is not a valid ISO-8601 timestamp: {exc}"


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Render one ``inquiry_ledger`` row (an ``asyncpg.Record``) into the
    JSON-safe shape a tool caller reads."""
    cited = row["cited_refs"]
    if isinstance(cited, str):  # asyncpg may hand back jsonb as text
        try:
            cited = json.loads(cited)
        except json.JSONDecodeError:
            cited = []
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "text": row["text"],
        "status": row["status"],
        "resolution_test": row["resolution_test"],
        "resolves_by": (
            row["resolves_by"].isoformat() if row["resolves_by"] else None
        ),
        "dispatched_to": row["dispatched_to"],
        "cited_refs": cited if isinstance(cited, list) else [],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
        "closed_by_entry": (
            str(row["closed_by_entry"]) if row["closed_by_entry"] else None
        ),
    }


_ROW_COLUMNS = (
    "id, kind, text, status, resolution_test, resolves_by, dispatched_to, "
    "cited_refs, created_at, updated_at, closed_at, closed_by_entry"
)


# ---------------------------------------------------------------------------
# ledger_read
# ---------------------------------------------------------------------------


async def ledger_read_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext,
) -> ToolResult:
    """The calling descriptor's OWN rows, newest first.

    ``args``:
      * ``status`` (optional) — one of :data:`STATUS_VOCAB`; omitted reads
        every status.
    """
    wb = _writeback(ctx)
    if wb is None:
        return ToolResult(
            status="failed",
            error="no writeback surface wired for ledger_read (ctx.writeback is None)",
        )
    descriptor_id = _calling_descriptor_id(wb)
    status = call.args.get("status")
    if status is not None and status not in STATUS_VOCAB:
        return ToolResult(
            status="failed",
            error=f"status must be one of {list(STATUS_VOCAB)}, got {status!r}",
        )

    try:
        async with wb.pg_pool.acquire() as conn:
            if status is None:
                rows = await conn.fetch(
                    f"""
                    SELECT {_ROW_COLUMNS}
                    FROM inquiry_ledger
                    WHERE descriptor_id = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    descriptor_id, _READ_LIMIT,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT {_ROW_COLUMNS}
                    FROM inquiry_ledger
                    WHERE descriptor_id = $1 AND status = $2
                    ORDER BY created_at DESC
                    LIMIT $3
                    """,
                    descriptor_id, status, _READ_LIMIT,
                )
    except Exception as exc:  # noqa: BLE001 — a read failure folds into the loop
        logger.warning(
            "inquiry_state.ledger_read.failed descriptor=%s err=%s",
            descriptor_id, exc,
        )
        return ToolResult(status="failed", error=f"ledger_read_failed: {exc!s}")

    entries = [_row_to_dict(r) for r in rows]
    return ToolResult(
        status="completed",
        output={"entries": entries, "count": len(entries)},
        units=1,
    )


# ---------------------------------------------------------------------------
# ledger_write
# ---------------------------------------------------------------------------


async def ledger_write_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext,
) -> ToolResult:
    """Insert ONE open row under the calling descriptor's own id.

    ``args``:
      * ``kind`` (required) — one of :data:`KIND_VOCAB`.
      * ``text`` (required) — the row's narrative (non-empty).
      * ``resolution_test`` (required IFF ``kind == 'hypothesis'``) — frozen
        at write, the H13 sealed-ledger discipline: a hypothesis with no test
        is refused, here AND by the DB CHECK
        ``inquiry_ledger_hypothesis_needs_test``.
      * ``resolves_by`` (optional) — an ISO-8601 timestamp.
      * ``dispatched_to`` (optional) — an open_question id / contention id /
        desk analyst_id this row was raised through (§3). Dispatch itself is
        NOT this tool's job — it rides the EXISTING journal_propose /
        substrate faucets; this only records WHERE.

      * ``cited_refs`` (optional) — the substrate uuids the row was opened ON
        (the inquiry kind sends the refs its entry already cites, so the yield
        instrument can later tell an "anticipated" observation from a guess).
        ``ledger_close`` may replace them with the closing warrant.
    """
    wb = _writeback(ctx)
    if wb is None:
        return ToolResult(
            status="failed",
            error="no writeback surface wired for ledger_write (ctx.writeback is None)",
        )
    descriptor_id = _calling_descriptor_id(wb)
    args = call.args

    kind = str(args.get("kind", "")).strip()
    if kind not in KIND_VOCAB:
        return ToolResult(
            status="failed",
            error=f"kind must be one of {list(KIND_VOCAB)}, got {kind!r}",
        )
    text = str(args.get("text", "")).strip()
    if not text:
        return ToolResult(
            status="failed", error="ledger_write requires non-empty 'text'",
        )
    text = text[:_MAX_TEXT_CHARS]

    resolution_test = args.get("resolution_test")
    if resolution_test is not None:
        resolution_test = str(resolution_test).strip() or None
    if kind == "hypothesis" and not resolution_test:
        return ToolResult(
            status="failed",
            error=(
                "a hypothesis needs a 'resolution_test' at write — the "
                "sealed-ledger discipline (H13): a test decided later is a "
                "test that can be softened to fit the outcome, so it is "
                "refused, not skipped"
            ),
        )

    resolves_by, rb_err = _coerce_resolves_by(args.get("resolves_by"))
    if rb_err is not None:
        return ToolResult(status="failed", error=rb_err)

    dispatched_to = args.get("dispatched_to")
    if dispatched_to is not None:
        dispatched_to = str(dispatched_to).strip() or None

    cited_refs, cr_err = _coerce_cited_refs(args.get("cited_refs"))
    if cr_err is not None:
        return ToolResult(status="failed", error=cr_err)
    try:
        async with wb.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO inquiry_ledger
                    (descriptor_id, kind, text, status, resolution_test,
                     resolves_by, dispatched_to, cited_refs)
                VALUES ($1, $2, $3, 'open', $4, $5, $6, $7::jsonb)
                RETURNING id
                """,
                descriptor_id, kind, text, resolution_test, resolves_by,
                dispatched_to, json.dumps(cited_refs or []),
            )
    except Exception as exc:  # noqa: BLE001 — a DB-side CHECK / insert failure folds into the loop
        logger.warning(
            "inquiry_state.ledger_write.insert_failed descriptor=%s kind=%s err=%s",
            descriptor_id, kind, exc,
        )
        return ToolResult(status="failed", error=f"ledger_write_failed: {exc!s}")

    entry_id = row["id"]
    logger.info(
        "inquiry_state.ledger_write.inserted descriptor=%s kind=%s id=%s",
        descriptor_id, kind, entry_id,
    )
    return ToolResult(
        status="completed",
        output={"id": str(entry_id), "kind": kind, "status": "open"},
        units=1,
    )


# ---------------------------------------------------------------------------
# ledger_close
# ---------------------------------------------------------------------------


async def ledger_close_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext,
) -> ToolResult:
    """Close ONE of the calling descriptor's own OPEN rows.

    ``args``:
      * ``id`` (required) — the ledger row's id (a UUID string).
      * ``status`` (required) — a terminal status: anything in
        :data:`STATUS_VOCAB` except ``'open'``.
      * ``reason`` (required) — appended to the row's ``text`` (the ledger
        carries no separate reason column — see the module docstring).
      * ``cited_refs`` (optional) — the substrate refs that warrant the
        close (§4: "an observation whose cited_refs a later finding
        cites"). The ONLY write this column ever receives.

    Scoped by ``id AND descriptor_id = <calling descriptor> AND status =
    'open'`` — a row that exists under another inquiry's descriptor_id, or
    is already closed, updates ZERO rows: the same fence
    :func:`ledger_read_tool` enforces on the read side, plus a guard against
    silently re-closing (and re-stamping ``closed_at`` on) a settled row.
    """
    wb = _writeback(ctx)
    if wb is None:
        return ToolResult(
            status="failed",
            error="no writeback surface wired for ledger_close (ctx.writeback is None)",
        )
    descriptor_id = _calling_descriptor_id(wb)
    args = call.args

    raw_id = args.get("id")
    try:
        entry_id = UUID(str(raw_id))
    except (TypeError, ValueError):
        return ToolResult(
            status="failed", error=f"id is not a valid UUID: {raw_id!r}",
        )

    status = str(args.get("status", "")).strip()
    if status not in _CLOSED_STATUSES:
        return ToolResult(
            status="failed",
            error=(
                f"status must be one of {list(_CLOSED_STATUSES)} "
                f"(ledger_close never sets 'open'), got {status!r}"
            ),
        )
    reason = str(args.get("reason", "")).strip()
    if not reason:
        return ToolResult(
            status="failed", error="ledger_close requires non-empty 'reason'",
        )
    reason = reason[:_MAX_REASON_CHARS]

    cited_refs, refs_err = _coerce_cited_refs(args.get("cited_refs"))
    if refs_err is not None:
        return ToolResult(status="failed", error=refs_err)

    # Built in Python, not SQL — the separator is a plain literal newline
    # pair, which is easier to read here than escaped inside the query text.
    close_note = f"\n\n[closed -> {status}] {reason}"

    try:
        async with wb.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE inquiry_ledger
                SET status = $3,
                    cited_refs = $4::jsonb,
                    text = text || $5,
                    closed_at = now(),
                    updated_at = now()
                WHERE id = $1 AND descriptor_id = $2 AND status = 'open'
                RETURNING id
                """,
                entry_id, descriptor_id, status, json.dumps(cited_refs), close_note,
            )
    except Exception as exc:  # noqa: BLE001 — a DB-side failure folds into the loop
        logger.warning(
            "inquiry_state.ledger_close.update_failed descriptor=%s id=%s err=%s",
            descriptor_id, entry_id, exc,
        )
        return ToolResult(status="failed", error=f"ledger_close_failed: {exc!s}")

    if row is None:
        # THE SCOPE FENCE, made visible: indistinguishable on purpose whether
        # the id is unknown, already closed, or belongs to ANOTHER inquiry —
        # a caller must not learn whether another inquiry's row even exists.
        return ToolResult(
            status="failed",
            error=(
                f"no open row {entry_id} owned by {descriptor_id!r} "
                "(unknown id, already closed, or belongs to another inquiry)"
            ),
        )

    logger.info(
        "inquiry_state.ledger_close.closed descriptor=%s id=%s status=%s",
        descriptor_id, entry_id, status,
    )
    return ToolResult(
        status="completed",
        output={"id": str(entry_id), "status": status},
        units=1,
    )


def register_inquiry_state_tools(registry: Any) -> None:
    """Register the ``inquiry_state`` pack's three handlers (called by
    ``default_tool_registry``). One tool name → one global handler; the pack
    is the GRANT/governance boundary, not a handler copy."""
    registry.register("ledger_read", ledger_read_tool)
    registry.register("ledger_write", ledger_write_tool)
    registry.register("ledger_close", ledger_close_tool)


__all__ = [
    "INQUIRY_STATE_PACK_ID",
    "INQUIRY_STATE_TOOLS",
    "KIND_VOCAB",
    "STATUS_VOCAB",
    "ledger_read_tool",
    "ledger_write_tool",
    "ledger_close_tool",
    "register_inquiry_state_tools",
]
