# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``list_findings`` survey mode and paging (W11-F150).

The 2026-11-05 consult dig found the platform had a live desk read for all 44
targets, while the consult saw only about a dozen of them. With no
``target_id``, ``list_findings`` returns the newest live findings across the
whole platform, newest first. Each row carries its full ~4 KB body, and
``since_hours`` is only a floor, so widening it returns the same first page.
Thirty rows came to 123 KB, the 40 KB round budget kept about twelve, and
every target ranked past that looked unread.

This module holds two fixes:

  * **Survey mode** (``mode="survey"``) returns ONE row per target: that
    target's newest LIVE finding (not superseded), ordered by target id. The
    row has a short ``digest`` (at most :data:`SURVEY_DIGEST_CHARS`
    characters from the top of the body, slice preamble dropped) and the
    body's length, never the body itself. The
    full body is still available through ``list_findings(target_id=…)`` or
    ``read_document(doc_id=<finding id>)``. 44 targets at about 700 B each fit
    in two default pages.
  * **Paging** for both modes: ``offset`` plus ``rows_total``, the true count of
    matching rows, and ``next_offset`` while more rows remain. The page read
    without an ``offset`` is the same query as before.

Bounded scans. The survey reads within a window (``since_hours``, default
:data:`SURVEY_DEFAULT_WINDOW_HOURS`), so its scan is the
``idx_analyst_outputs_kind (kind, produced_at DESC)`` range and never the whole
table. The page count walks the same index newest first and stops at
:data:`ROWS_TOTAL_CAP`; a count that reaches the cap is reported as
``rows_total_capped``. A plain ``count(*)`` over every live finding
seq-scans ``analyst_outputs`` (measured 2026-11-05).

The port method :meth:`PostgresQdrantSubstrateQueryPort.list_findings` owns the
contract. This sibling owns the SQL and the survey body, so the port stays
under its module-size ceiling, as :mod:`.substrate_frame_reads` does.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from ..data import critic_fold as _critic_fold
from .target_resolution import stamp_target_resolution, widen_to_member_targets

#: The two shapes ``list_findings`` answers in. ``page`` is the historical
#: newest-first page; ``survey`` is one newest live read per target.
LIST_FINDINGS_MODES: tuple[str, ...] = ("page", "survey")

#: How much of a finding's body a survey row carries (the cap includes the
#: "..." a cut digest ends with).
SURVEY_DIGEST_CHARS = 240
#: How much of the body the survey SQL hands up for the digest. Desk bodies
#: open with an italic "*As of …; slice covers …*" line, which
#: :func:`digest` drops, so the head is wider than the digest.
SURVEY_HEAD_CHARS = 600
#: A survey title is cut here, so one long title cannot crowd out the page.
SURVEY_TITLE_CHARS = 160
#: The survey's window when the caller names none. Every desk writes on an 11 h
#: cadence, so a week sees every live target with room to spare.
SURVEY_DEFAULT_WINDOW_HOURS = 168
#: One survey page when the caller names no ``limit``. A row is about 700 B,
#: so 25 rows (~18 KB) fit one call's fair share of a shared 40 KB consult
#: round; the 43 live targets of 2026-11-05 are two pages, and ``next_offset``
#: names the second. A caller with the round to itself can ask for 50.
SURVEY_DEFAULT_LIMIT = 25
#: The page mode's ``limit`` when none is named (unchanged).
PAGE_DEFAULT_LIMIT = 20
#: Stop counting here. ``rows_total`` is exact below the cap; at the cap the
#: answer is "at least this many" and says so.
ROWS_TOTAL_CAP = 2000
#: The deepest ``offset`` a page may ask for; paging past the count cap is
#: never useful to a reader.
MAX_OFFSET = ROWS_TOTAL_CAP

SURVEY_NOTE = (
    "survey: one row per target, its newest LIVE finding, digest only (at "
    "most {n} characters from the top of the body). For the full body call "
    "list_findings(target_id=...) or read_document(doc_id=<finding id>)."
)


def normalize_mode(mode: Any) -> str | None:
    """``mode`` as one of :data:`LIST_FINDINGS_MODES`, or ``None`` if unknown.

    ``None`` and an empty string mean ``page``, and ``survey=true`` spellings
    are folded in by the caller.
    """
    m = str(mode if mode is not None else "page").strip().lower() or "page"
    return m if m in LIST_FINDINGS_MODES else None


def clamp_offset(offset: Any) -> int:
    try:
        o = int(offset or 0)
    except (TypeError, ValueError):
        o = 0
    return max(0, min(o, MAX_OFFSET))


def target_list(
    target_id: str | None, target_ids: Sequence[Any] | str | None,
) -> list[str]:
    """``target_id`` and ``target_ids`` as one de-duplicated list, in order.

    A bare string in ``target_ids`` is one id, the tolerance every pack
    handler already extends to a single value where a list is declared.
    """
    raw: list[Any] = [target_id] if target_id is not None else []
    if isinstance(target_ids, str):
        raw.append(target_ids)
    elif target_ids:
        raw.extend(target_ids)
    out: list[str] = []
    for t in raw:
        s = str(t).strip()
        if s and s not in out:
            out.append(s)
    return out


#: A whole line in *italics*: the slice preamble every desk body opens with.
_PREAMBLE_LINE = re.compile(r"^\s*\*[^*\n][^\n]*\*\s*$")


def digest(head: str | None) -> tuple[str, bool]:
    """A one-paragraph digest of a body's head, and whether it was cut.

    Drops leading blank and all-italic lines (the slice preamble), drops
    markdown bold markers, collapses whitespace, and cuts at a word boundary
    so the digest, with its "...", is at most :data:`SURVEY_DIGEST_CHARS`.
    A body that is nothing but preamble keeps it rather than going blank.
    """
    lines = (head or "").splitlines()
    kept = list(lines)
    while kept and (not kept[0].strip() or _PREAMBLE_LINE.match(kept[0])):
        kept.pop(0)
    flat = " ".join(" ".join(kept or lines).replace("**", "").split())
    if len(flat) <= SURVEY_DIGEST_CHARS:
        return flat, False
    cut = flat[: SURVEY_DIGEST_CHARS - 3]
    space = cut.rfind(" ")
    if space > SURVEY_DIGEST_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip() + "...", True


def _r3(x: Any) -> float | None:
    return round(float(x), 3) if x is not None else None


def effective_confidence(
    confidence: float | None, critic_score: float | None,
) -> float | None:
    """``min(confidence, critic_score)``, or ``confidence`` when either is missing."""
    if confidence is not None and critic_score is not None:
        return min(confidence, critic_score)
    return confidence


def rows_total_sql(where: str, cap_param: int) -> str:
    """Count the rows a page read matches, walking newest first up to a cap.

    The ``ORDER BY … LIMIT`` keeps the plan on
    ``idx_analyst_outputs_kind (kind, produced_at DESC)``. An uncapped
    ``count(*)`` over every live finding plans a parallel seq scan.
    """
    return (
        "SELECT count(*) FROM ( "
        "  SELECT 1 FROM analyst_outputs f "
        f" WHERE {where} "
        "  ORDER BY f.produced_at DESC "
        f" LIMIT ${cap_param} "
        ") n"
    )


def findings_survey_sql(where: str, limit_param: int, offset_param: int) -> str:
    """The newest live finding per target, one page of targets, critic-folded.

    * ``s``: ``DISTINCT ON (target_id)`` over the window, newest first within
      each target (``produced_at DESC, id DESC``, the page read's tiebreak),
      so exactly one row per target wins. The superseded gate is part of
      ``where``, so a superseded read can never win.
    * ``p``: one page of ``s`` ordered by target id. The body's head (for
      :func:`digest`) and its length are computed here, on at most ``limit``
      rows, not on every row in the window.
    * ``c``: the latest scored critique per page id, through the shared
      set-based fold (:func:`legba.data.critic_fold.latest_critique_cte`),
      with the same knobs as ``critic_folded_findings_sql``.

    The count joins from ``(SELECT count(*) FROM s)``, so an empty page (an
    offset past the end) still returns ``rows_total``, on one row with a
    NULL ``id``.
    """
    fold = _critic_fold.latest_critique_cte(
        "c",
        "(cr.data->>'overall_score')::real AS critic_score",
        "SELECT id::text FROM p",
        title_like=None,
        ids_as_array=False,
    )
    return (
        "WITH s AS MATERIALIZED ( "
        "  SELECT DISTINCT ON (f.target_id) f.id, f.title, f.body, "
        "         f.confidence, f.severity, f.target_id, f.analyst_id, "
        "         f.produced_at "
        "  FROM analyst_outputs f "
        f" WHERE {where} AND f.target_id IS NOT NULL "
        "  ORDER BY f.target_id, f.produced_at DESC, f.id DESC "
        "), p AS MATERIALIZED ( "
        f"  SELECT s.id, left(s.title, {SURVEY_TITLE_CHARS}) AS title, "
        f"         left(s.body, {SURVEY_HEAD_CHARS}) AS head, "
        "         length(s.body) AS body_chars, s.confidence, s.severity, "
        "         s.target_id, s.analyst_id, s.produced_at "
        "  FROM s ORDER BY s.target_id "
        f" LIMIT ${limit_param} OFFSET ${offset_param} "
        f"), {fold} "
        "SELECT t.n AS rows_total, p.id, p.title, p.head, p.body_chars, "
        "       p.confidence, p.severity, p.target_id, p.analyst_id, "
        "       p.produced_at, c.critic_score AS critic_score "
        "FROM (SELECT count(*) AS n FROM s) t "
        "LEFT JOIN p ON TRUE "
        "LEFT JOIN c ON c.fid = p.id::text "
        "ORDER BY p.target_id"
    )


def paging_fields(
    *, offset: int, count: int, rows_total: int, capped: bool = False,
) -> dict[str, Any]:
    """``rows_total`` (and ``rows_total_capped``), plus ``offset`` and
    ``next_offset`` when they say something."""
    out: dict[str, Any] = {"rows_total": rows_total}
    if capped:
        out["rows_total_capped"] = True
    if offset:
        out["offset"] = offset
    if offset + count < rows_total:
        out["next_offset"] = offset + count
    return out


async def page_rows_total(
    conn: Any, where: str, where_params: list[Any], *,
    offset: int, count: int, limit: int,
    count_sql: str | None = None, count_tail: Sequence[Any] = (),
) -> dict[str, Any]:
    """Paging fields for a page read, counting only when the page is full.

    A short first page is its own total; that answer needs no second query.
    ``count_sql`` replaces the plain count for a read whose filter is not a
    predicate on ``f`` (the verified floor). It is the page SQL itself, taking
    ``where_params``, then the cap as its ``LIMIT``, then ``count_tail``.
    """
    if offset == 0 and count < limit:
        return paging_fields(offset=0, count=count, rows_total=count)
    sql = (
        f"SELECT count(*) FROM ({count_sql}) n" if count_sql is not None
        else rows_total_sql(where, len(where_params) + 1)
    )
    n = int(await conn.fetchval(
        sql, *where_params, ROWS_TOTAL_CAP, *count_tail,
    ) or 0)
    # The page itself is ground truth when the count raced a fresh write.
    n = max(n, offset + count)
    return paging_fields(
        offset=offset, count=count, rows_total=n, capped=n >= ROWS_TOTAL_CAP,
    )


async def survey_findings(
    pool: Any,
    *,
    target_id: str | None,
    target_ids: Sequence[Any] | str | None,
    analyst_id: str | None,
    severity: str | None,
    since_hours: int | None,
    limit: int | None,
    offset: Any,
    max_row_limit: int,
) -> dict[str, Any]:
    """The survey body; the port method's docstring owns the contract."""
    window = int(since_hours) if since_hours is not None else SURVEY_DEFAULT_WINDOW_HOURS
    if window <= 0:
        return {"rows": [], "refs": [], "count": 0, "mode": "survey",
                "error": "since_hours must be a positive number of hours"}
    clamped = max(1, min(int(limit if limit is not None else SURVEY_DEFAULT_LIMIT),
                         max_row_limit))
    off = clamp_offset(offset)
    params: list[Any] = [datetime.now(timezone.utc) - timedelta(hours=window)]
    clauses = [
        "f.kind = 'finding'", "f.superseded_by IS NULL", "f.produced_at >= $1",
    ]
    targets = target_list(target_id, target_ids)
    target_param: int | None = None
    if targets:
        params.append(targets)
        target_param = len(params)
        clauses.append(f"f.target_id = ANY(${target_param}::text[])")
    if analyst_id is not None:
        params.append(analyst_id)
        clauses.append(f"f.analyst_id = ${len(params)}")
    if severity is not None:
        params.append(severity)
        clauses.append(f"f.severity = ${len(params)}")
    params.extend([clamped, off])
    sql = findings_survey_sql(" AND ".join(clauses), len(params) - 1, len(params))

    resolution: dict[str, Any] | None = None
    async with pool.acquire() as conn:
        records = list(await conn.fetch(sql, *params))
        total = int(records[0]["rows_total"]) if records else 0
        # A single frame id that matches no desk widens to its member desks,
        # exactly as the page read does: a survey of region_mena is the
        # newest read of each desk in the region.
        if total == 0 and target_param is not None and len(targets) == 1:
            records, resolution = await widen_to_member_targets(
                conn, sql, params, target_param, targets[0],
            )
            total = int(records[0]["rows_total"]) if records else 0

    rows: list[dict[str, Any]] = []
    for r in records:
        if r["id"] is None:
            continue
        conf, cs = _r3(r["confidence"]), _r3(r["critic_score"])
        body_chars = int(r["body_chars"] or 0)
        text, cut = digest(r["head"])
        rows.append({
            "id": str(r["id"]),
            "target_id": r["target_id"],
            "analyst_id": r["analyst_id"],
            "produced_at": r["produced_at"].isoformat()
                if isinstance(r["produced_at"], datetime) else None,
            "severity": r["severity"],
            "confidence": conf,
            "critic_score": cs,
            "effective_confidence": effective_confidence(conf, cs),
            "title": r["title"],
            "digest": text,
            "body_chars": body_chars,
            "body_truncated": cut or body_chars > SURVEY_HEAD_CHARS,
        })
    out: dict[str, Any] = {
        "mode": "survey",
        "rows": rows,
        "refs": [r["id"] for r in rows],
        "count": len(rows),
        **paging_fields(offset=off, count=len(rows), rows_total=total),
        "window_hours": window,
        "note": SURVEY_NOTE.format(n=SURVEY_DIGEST_CHARS),
    }
    if len(targets) == 1:
        stamp_target_resolution(out, targets[0], resolution)
    return out
