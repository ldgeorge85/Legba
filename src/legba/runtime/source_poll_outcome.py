# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-poll provenance: the ``source_poll_outcomes`` writer and the outcome
rollup, in a leaf (DQ-H5b #88; success outcome — migration 0114; gated
outcome + ``keyword_filtered`` — migration 0226, W3-F8).

Moved out of ``source_actor.py`` (size-gated) on 2026-10-03 so the actor keeps
only the call sites; ``source_actor`` re-imports both names, so
``from legba.runtime.source_actor import write_poll_outcome`` still binds.

EVERY poll writes exactly one row. Migration 0046 originally logged only
NON-productive polls on the premise that "a productive poll is self-evidencing
via its signals rows" — true for a reader inspecting ONE poll, false for every
reader that walks a RUN of rows, because a run cannot observe an absence. With
success unrecorded, a repaired source still presented its historical `error`
rows as the LEADING run forever, and `entity_gc` operation 4 re-paused sources
that were actively ingesting (gdelt.files 2026-07-27; ukrinform / nasa.eonet
before it). A recorded success BREAKS the run at its first productive poll, for
every reader, with no operator intervention.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = ["classify_poll_outcome", "write_poll_outcome"]

_INSERT_POLL_OUTCOME = """
INSERT INTO public.source_poll_outcomes (
    source_id, source_version, owner_tenant,
    outcome, health_state, capped, signals_written, error, newest_entry_ts,
    reserve_unchanged, keyword_filtered
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

# N-L (migration 0245): optional per-poll JSONB columns — a non-document
# sink's counts, and the fetch protocol when it was not the silent default
# (an HTTP/1.1 fetch or a fallback). W11-F265 (migration 0254): the optional
# integer ``age_skipped`` — entries a FIRST poll skipped as older than the
# feed's first-poll age cap. Each is appended ONLY when present, so a
# document source's default poll writes the statement it always wrote.
# I-4 (migration 0256): the optional ``stream`` JSONB — what one window of a
# SUBSCRIBED source's held connection did (``runtime/stream_ingest.py``).
# C-2 (migration 0265): the optional ``claim_hygiene`` JSONB — what the
# claim-text rules did to this poll's CLAIM rows, as kinds and counts.
_OPTIONAL_JSON_COLUMNS = ("sink_counts", "fetch_protocol", "stream", "claim_hygiene")
_OPTIONAL_COLUMN_CAST = {"sink_counts": "jsonb", "fetch_protocol": "jsonb", "age_skipped": "integer",
                         "stream": "jsonb", "claim_hygiene": "jsonb"}


def _insert_with(extra: list[str]) -> str:
    if not extra:
        return _INSERT_POLL_OUTCOME
    cols = ", ".join(extra)
    params = ", ".join(
        f"${12 + i}::{_OPTIONAL_COLUMN_CAST.get(c, 'jsonb')}" for i, c in enumerate(extra)
    )
    return (_INSERT_POLL_OUTCOME
            .replace("reserve_unchanged, keyword_filtered\n)", f"reserve_unchanged, keyword_filtered, {cols}\n)")
            .replace("$10, $11)", f"$10, $11, {params})"))


# Cap the stored error string so a verbose traceback can't bloat the row.
_POLL_OUTCOME_ERROR_MAX = 2000


def classify_poll_outcome(
    *,
    signals_written: int,
    reserve_unchanged: int,
    errored: str | None,
    health_state: str | None,
    keyword_filtered: int | None,
) -> str:
    """The coarse outcome of ONE poll, in strict precedence (pure).

      * ``'success'`` — wrote >=1 signal or re-served >=1 duplicate (the
        source answered with real content). Productivity wins over the failure
        signals: a partial failure is still a success.
      * ``'error'`` — an escaped exception or a degraded/unhealthy handler.
      * ``'gated'`` (migration 0226) — none of the above, and the keyword gate
        dropped >=1 entry: the feed DELIVERED and everything it delivered was
        off-topic. Productive for the watchdog and the health reads (the fetch
        path works) but not a new item.
      * ``'empty'`` — polled fine, nothing arrived (``keyword_filtered`` is
        ``None`` for an ungated source, ``0`` for a gate that dropped nothing).
    """
    if max(0, int(signals_written)) > 0 or max(0, int(reserve_unchanged)) > 0:
        return "success"
    if errored is not None or health_state in ("degraded", "unhealthy"):
        return "error"
    if keyword_filtered is not None and keyword_filtered > 0:
        return "gated"
    return "empty"


async def write_poll_outcome(
    conn: Any,
    *,
    source_id: str,
    source_version: str | None,
    owner_tenant: str,
    outcome: str,                 # 'success' | 'gated' | 'empty' | 'error'
    health_state: str | None,     # 'healthy' | 'degraded' | 'unhealthy' | None
    capped: bool,
    signals_written: int,
    error: str | None,
    newest_entry_ts: datetime | None = None,
    reserve_unchanged: int = 0,
    keyword_filtered: int | None = None,
    sink_counts: dict[str, Any] | None = None,
    fetch_protocol: dict[str, Any] | None = None,
    age_skipped: int | None = None,
    stream: dict[str, Any] | None = None,
    claim_hygiene: dict[str, int] | None = None,
) -> None:
    """Append a provenance row for ONE poll (DQ-H5b; success — migration 0114).

    ``outcome`` is the coarse rollup every reader keys on (see
    :func:`classify_poll_outcome` for the precedence):

      * ``'success'`` — the poll was PRODUCTIVE: it wrote >=1 signal, or it
        collapsed >=1 intra-source duplicate (the source answered with real
        content). ``signals_written`` carries the count. A partial failure
        (some rows written, then an exception) is still a success — the source
        produced — but keeps its ``error`` / ``health_state`` detail on the
        row rather than discarding it.
      * ``'error'`` — the poll failed: an escaped exception OR a
        handler-swallowed 4xx / parse-fail / timeout surfaced via
        ``health_state``.
      * ``'gated'`` — migration 0226: 0 written, but the keyword gate dropped
        >=1 entry. The feed delivered; it was all off-topic.
      * ``'empty'`` — a genuine HTTP-200-but-0-items feed. UNCHANGED meaning:
        polled fine, nothing new. Deliberately NOT merged with 'success' —
        "polled and found nothing" and "polled and ingested 28 items" are
        different facts, and the empty-streak watchdog keys on the
        difference.

    ``newest_entry_ts`` (B0-12): the newest entry timestamp the handler
    OBSERVED upstream this poll (pre-since-filter, future-skew-clamped —
    recorded in the handler's health detail). The watchdog's empty-streak
    check compares it against the source's newest ingested signal to
    discriminate honest-quiet (no alert) from a cursor/filter fault
    (escalate). NULL when the handler records no such observation.

    ``reserve_unchanged`` (B-1, migration 0140): how many entries this poll
    re-served byte-identically (exact ``content_hash`` match on a row we already
    hold). ``signals_written = 0 AND reserve_unchanged > 0`` is the SERVE-STALE
    signature — the source is answering, the content is frozen. That is the
    state the 5 rsshub AP feeds sat in for six days while this ledger recorded
    nothing but healthy successes.

    ``keyword_filtered`` (migration 0226): how many entries the keyword gate
    dropped on this poll. ``None`` = the source is not gated (stored as NULL,
    never a 0); ``0`` = gated and it dropped nothing.

    ``age_skipped`` (W11-F265, migration 0254): how many entries a FIRST poll
    (no cursor) skipped as older than the feed's first-poll age cap
    (``runtime/source_first_poll_age.py``). The actor passes it only when
    >= 1 entry was skipped; ``None`` leaves the column out of the statement
    (NULL: nothing skipped, or the cap did not apply to the poll).

    ``stream`` (I-4, migration 0256): a SUBSCRIBED source writes one row per
    window of its held connection, and this carries the window (connection
    state, events, reconnects, stalls, drops, declared gaps, resume). ``None``
    (every poll) leaves the column out of the statement.

    ``claim_hygiene`` (C-2, migration 0265): for a poll that wrote CLAIM rows,
    ``{rows, <kind>: count}`` — URLs reduced to a host, onion links dropped,
    personal data redacted by kind, files / sample rows cut
    (``data/claim_hygiene.py``). Counts only, never a value. ``None`` (every
    other poll) leaves the column out of the statement.
    """
    present: list[tuple[str, Any]] = [
        (c, v) for c, v in (("sink_counts", sink_counts), ("fetch_protocol", fetch_protocol)) if v
    ]
    if age_skipped is not None:
        present.append(("age_skipped", max(0, int(age_skipped))))
    if stream:
        present.append(("stream", stream))
    if claim_hygiene:
        present.append(("claim_hygiene", claim_hygiene))
    extra: tuple[Any, ...] = ()
    sql = _insert_with([c for c, _ in present])
    if present:
        import json  # noqa: PLC0415 — only a non-default poll pays it

        extra = tuple(v if c == "age_skipped" else json.dumps(v) for c, v in present)
    await conn.execute(
        sql,
        source_id,
        source_version,
        owner_tenant or "default",
        outcome,
        health_state,
        capped,
        int(signals_written),
        error[:_POLL_OUTCOME_ERROR_MAX] if error else None,
        newest_entry_ts,
        max(0, int(reserve_unchanged)),
        None if keyword_filtered is None else max(0, int(keyword_filtered)),
        *extra,
    )
