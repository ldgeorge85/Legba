# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult audit-trail persistence — sessions + turns (0038).

The consult chat panel and Deep Consult panel hold their transcripts entirely
client-side; nothing was persisted server-side, so a closed tab lost the
conversation and there was no operator-visible audit trail. This module is the
thin write/read layer over the ``consult_sessions`` + ``consult_turns`` tables
(migration ``0038_consult_sessions.sql``):

  * ``create_session`` — open a session header (chat conversation OR deep task).
  * ``append_turn`` — append one user/assistant turn (with the ReAct steps,
    projected tool calls, cited refs, and optional finding id).
  * ``list_sessions`` / ``load_session`` — the read surface the history sidebar
    + continue-a-prior-session flow consume.

All functions take an asyncpg pool/connection-acquirer (the registry's
``descriptor_registry.pg``) so they slot straight into the consult routers,
which already own that handle. Writes are best-effort from the caller's
perspective — a persistence failure must NOT fail the consult request itself
(the answer is the product; the audit row is a side-effect), so the routers
wrap these in a try/except that logs and continues. The functions themselves do
the minimal validation and let asyncpg raise on a genuine DB error.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: History title is the first question, truncated. Keep it short for the sidebar.
_TITLE_MAX = 120


def _title_from_question(question: str) -> str:
    q = (question or "").strip().replace("\n", " ")
    if len(q) > _TITLE_MAX:
        return q[: _TITLE_MAX - 1].rstrip() + "…"
    return q or "(untitled consult)"


def _as_jsonb(value: Any) -> str:
    """Serialise a python value to a JSON string for a jsonb column.

    asyncpg binds jsonb params as text, so we hand it a JSON string. Defensive:
    a non-serialisable value degrades to an empty list/object rather than
    raising inside the audit write.
    """
    try:
        return json.dumps(value if value is not None else [])
    except (TypeError, ValueError):
        return "[]"


async def create_session(
    pg: Any,
    *,
    mode: str,
    question: str,
    principal: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
) -> str | None:
    """Insert a ``consult_sessions`` header; return its id (or None on failure).

    ``mode`` is ``'chat'`` | ``'deep'``. For deep tasks ``task_id`` / ``run_id``
    correlate the session with the detached workflow so the status poll can join
    back to it. Returns None (not raising) when the write fails so the caller's
    consult request is never blocked by an audit-trail outage.
    """
    safe_mode = mode if mode in ("chat", "deep") else "chat"
    try:
        async with pg.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO consult_sessions
                    (mode, title, principal, task_id, run_id)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                safe_mode,
                _title_from_question(question),
                principal,
                task_id,
                run_id,
            )
        return str(row["id"]) if row else None
    except Exception as exc:  # pragma: no cover — exercised only on DB outage
        logger.warning("consult_persistence.create_session failed: %s", exc)
        return None


async def append_turn(
    pg: Any,
    *,
    session_id: str,
    role: str,
    content: str,
    steps: Any = None,
    tool_calls: Any = None,
    cited_refs: Any = None,
    finding_id: str | None = None,
) -> str | None:
    """Append one ``consult_turns`` row and bump the session's ``updated_at``.

    ``role`` is ``'user'`` | ``'assistant'``. The jsonb projections default to
    empty lists. Returns the new turn id, or None on failure (best-effort — the
    audit write never fails the consult request).
    """
    if role not in ("user", "assistant"):
        logger.warning("consult_persistence.append_turn bad role=%r", role)
        return None
    if not session_id:
        return None
    try:
        async with pg.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO consult_turns
                        (session_id, role, content, steps, tool_calls,
                         cited_refs, finding_id)
                    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7)
                    RETURNING id
                    """,
                    session_id,
                    role,
                    content or "",
                    _as_jsonb(steps),
                    _as_jsonb(tool_calls),
                    _as_jsonb(cited_refs),
                    finding_id,
                )
                await conn.execute(
                    "UPDATE consult_sessions SET updated_at = now() WHERE id = $1",
                    session_id,
                )
        return str(row["id"]) if row else None
    except Exception as exc:  # pragma: no cover — exercised only on DB outage
        logger.warning("consult_persistence.append_turn failed: %s", exc)
        return None


async def record_deep_completion(
    pg: Any,
    *,
    task_id: str,
    answer: str,
    cited_refs: Any = None,
    finding_id: str | None = None,
) -> str | None:
    """Append the deep-consult ANSWER turn to its session, idempotently.

    The deep-consult status route is poll-driven — it can observe ``completed``
    many times — so this writes the assistant turn AT MOST ONCE per session: it
    finds the session by ``task_id`` and only appends when no assistant turn
    exists yet. Returns the turn id when newly written, None otherwise (already
    recorded, no session, or a DB error — never raises into the poll path).
    """
    if not task_id:
        return None
    try:
        async with pg.acquire() as conn:
            session = await conn.fetchrow(
                """
                SELECT id FROM consult_sessions
                 WHERE task_id = $1
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                task_id,
            )
            if session is None:
                return None
            session_id = str(session["id"])
            already = await conn.fetchval(
                """
                SELECT 1 FROM consult_turns
                 WHERE session_id = $1 AND role = 'assistant'
                 LIMIT 1
                """,
                session_id,
            )
            if already:
                return None
    except Exception as exc:  # pragma: no cover — exercised only on DB outage
        logger.warning("consult_persistence.record_deep_completion lookup failed: %s", exc)
        return None

    return await append_turn(
        pg,
        session_id=session_id,
        role="assistant",
        content=answer or "",
        cited_refs=cited_refs,
        finding_id=finding_id,
    )


async def list_sessions(
    pg: Any, *, limit: int = 50, mode: str | None = None,
) -> list[dict[str, Any]]:
    """List session headers, most-recently-active first (history sidebar).

    Optional ``mode`` filter ('chat' | 'deep'); ``limit`` clamps the page (1..200).
    Each row carries a ``turn_count`` so the sidebar can show conversation size
    without a second round-trip.
    """
    clamped = max(1, min(200, int(limit)))
    where = ""
    args: list[Any] = [clamped]
    if mode in ("chat", "deep"):
        where = "WHERE s.mode = $2"
        args.append(mode)
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT s.id, s.mode, s.title, s.task_id, s.run_id,
                   s.created_at, s.updated_at,
                   (SELECT count(*) FROM consult_turns t
                     WHERE t.session_id = s.id) AS turn_count
              FROM consult_sessions s
              {where}
             ORDER BY s.updated_at DESC
             LIMIT $1
            """,
            *args,
        )
    return [
        {
            "id": str(r["id"]),
            "mode": r["mode"],
            "title": r["title"],
            "task_id": r["task_id"],
            "run_id": r["run_id"],
            "turn_count": int(r["turn_count"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


def _load_jsonb(value: Any) -> Any:
    """asyncpg returns jsonb either pre-decoded or as a string; normalise."""
    if value is None:
        return []
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return []
    return []


async def load_session(pg: Any, session_id: str) -> dict[str, Any] | None:
    """Load one session header + its ordered turns (continue-a-prior-chat).

    Returns None when the session id is unknown. Turns are oldest-first so the
    client can re-seed its ``messages[]`` transcript verbatim.
    """
    async with pg.acquire() as conn:
        header = await conn.fetchrow(
            """
            SELECT id, mode, title, task_id, run_id, created_at, updated_at
              FROM consult_sessions
             WHERE id = $1
            """,
            session_id,
        )
        if header is None:
            return None
        turn_rows = await conn.fetch(
            """
            SELECT id, role, content, steps, tool_calls, cited_refs,
                   finding_id, created_at
              FROM consult_turns
             WHERE session_id = $1
             ORDER BY created_at ASC, id ASC
            """,
            session_id,
        )

    return {
        "id": str(header["id"]),
        "mode": header["mode"],
        "title": header["title"],
        "task_id": header["task_id"],
        "run_id": header["run_id"],
        "created_at": header["created_at"].isoformat() if header["created_at"] else None,
        "updated_at": header["updated_at"].isoformat() if header["updated_at"] else None,
        "turns": [
            {
                "id": str(t["id"]),
                "role": t["role"],
                "content": t["content"],
                "steps": _load_jsonb(t["steps"]),
                "tool_calls": _load_jsonb(t["tool_calls"]),
                "cited_refs": _load_jsonb(t["cited_refs"]),
                "finding_id": t["finding_id"],
                "created_at": t["created_at"].isoformat() if t["created_at"] else None,
            }
            for t in turn_rows
        ],
    }


__all__ = [
    "append_turn",
    "create_session",
    "list_sessions",
    "load_session",
    "record_deep_completion",
]
