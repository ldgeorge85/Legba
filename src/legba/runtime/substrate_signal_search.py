# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``search_signals`` — Postgres full-text search over ``signals``.

Extracted from :mod:`legba.runtime.substrate_query_port` when the
raw readers gained the ``known_at`` record-time cut and the port sat at its
module-size ceiling. The port's method delegates here; the SQL is byte-identical
to before when ``known_at`` is None.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..data.signal_slice_scope import CANONICAL_ONLY_SQL, FTS_SUMMARY_SQL
from . import substrate_known_at as _known_at

#: The port's logger name, so log lines and caplog filters are unchanged.
logger = logging.getLogger("legba.runtime.substrate_query_port")

#: SQLSTATE 54000 program_limit_exceeded - what Postgres raises for "string is
#: too long for tsvector".
_PROGRAM_LIMIT_SQLSTATE = "54000"

__all__ = ["search_signals"]


def _tsquery_lexemes(rendered: Any) -> list[str]:
    """The lexemes in a rendered ``tsquery``, in order.

    ``plainto_tsquery('simple', 'Iran drone strikes')::text`` renders as
    ``'iran' & 'drone' & 'strikes'``; a literal quote inside a lexeme is
    doubled. Reading the rendering back is how ``search_signals`` can tell a
    caller which terms its empty result actually required WITHOUT
    re-implementing the tokenizer and naming terms that are nearly right.
    """
    out: list[str] = []
    text = str(rendered or "")
    i = 0
    while i < len(text):
        if text[i] != "'":
            i += 1
            continue
        i += 1
        buf: list[str] = []
        while i < len(text):
            if text[i] == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                i += 1
                break
            buf.append(text[i])
            i += 1
        out.append("".join(buf))
    return out


async def search_signals(
    pool: Any,
    *,
    query: str,
    limit: int = 20,
    scope_predicate: str | None = None,
    max_limit: int = 200,
    known_at: datetime | None = None,
) -> dict[str, Any]:
    """Full-text search over ``signals`` via Postgres ``to_tsvector``.

    Returns rows ranked by ``ts_rank`` against
    ``plainto_tsquery('simple', $1)``.  When ``query`` is
    empty / whitespace we return an empty result (rather than
    running an unbounded scan ranked at zero).

    The old ``category`` filter parameter was removed (W2-T5
    residual): no live signal carries the payload key, so the filter
    could only turn every query into an honest-looking empty result.

    ``known_at`` (evaluation harness only): only rows whose
    ``created_at`` is before that instant; the result says so in
    ``known_at_cut``. Absent, the statement is unchanged.
    """
    clamped_limit = max(1, min(int(limit), max_limit))
    q = (query or "").strip()
    if not q:
        return {
            "rows": [],
            "refs": [],
            "count": 0,
            "query": query,
            "scope_predicate_applied": False,
            "backing": "postgres_fts",
            "note": "empty_query",
        }

    # The summary leg is capped (FTS_SUMMARY_MAX_CHARS) so no
    # row's tsvector can pass Postgres' 1 MB limit.
    fts_doc = (
        "to_tsvector('simple', coalesce(payload->>'title','') || ' ' || "
        f"{FTS_SUMMARY_SQL})"
    )
    sql_parts = [
        "SELECT id, payload->>'title' AS title, "
        "payload->>'title_en' AS title_en, "
        "payload->>'category' AS category, canonical_url, fetched_at,",
        f"       ts_rank({fts_doc}, plainto_tsquery('simple', $1)) AS rank",
        "FROM signals",
        f"WHERE {fts_doc}",
        "      @@ plainto_tsquery('simple', $1)",
        # W8-F2: a re-polled alias never surfaces as a second citation.
        f"  AND {CANONICAL_ONLY_SQL}",
    ]
    params: list[Any] = [q]
    if known_at is not None:
        # E0: the record-time cut — only rows that entered the record before K.
        params.append(known_at)
        sql_parts.append("  AND " + _known_at.recorded_before_sql(
            _known_at.RECORD_ANCHOR, len(params)))
    sql_parts.append("ORDER BY rank DESC, fetched_at DESC")
    sql_parts.append(f"LIMIT ${len(params) + 1}")
    params.append(clamped_limit)
    sql = "\n".join(sql_parts)

    lexemes: list[str] = []
    async with pool.acquire() as conn:
        try:
            records = await conn.fetch(sql, *params)
        except Exception as exc:  # noqa: BLE001 - narrowed by SQLSTATE below
            # A tsvector over Postgres' limit (SQLSTATE 54000,
            # "string is too long for tsvector") must not fail the whole
            # call. The left() cap makes this unreachable for the summary
            # leg; if it still fires (a pathological title, a future
            # column) degrade loudly with the standard empty-rows +
            # ``error`` shape rather than raising into the tool loop.
            if getattr(exc, "sqlstate", None) != _PROGRAM_LIMIT_SQLSTATE:
                raise
            logger.warning(
                "substrate_query_port.search_signals.fts_limit err=%s", exc,
            )
            return {
                "rows": [],
                "refs": [],
                "count": 0,
                "query": query,
                "backing": "postgres_fts",
                "scope_predicate_applied": False,
                "error": (
                    "full-text search hit a Postgres limit on an "
                    f"oversize signal ({exc}); this is a data defect, not "
                    "an empty result. Use search_corpus for this query."
                ),
            }
        if not records:
            # Ask Postgres what it actually required, rather than
            # re-implementing its tokenizer in Python and naming lexemes
            # that are nearly right. One cheap call, on the empty path
            # only.
            lexemes = _tsquery_lexemes(
                await conn.fetchval(
                    "SELECT plainto_tsquery('simple', $1)::text", q,
                )
            )

    rows: list[dict[str, Any]] = []
    refs: list[str] = []
    for r in records:
        rid = r["id"]
        refs.append(str(rid))
        rows.append({
            "id": str(rid),
            "title": r["title"],
            # T-1b (M13): the stored English title, when the signal went
            # through the translate route; absent otherwise. Readers prefer it.
            "title_en": r["title_en"],
            "category": r["category"],
            "source_url": r["canonical_url"],
            "produced_at": r["fetched_at"].isoformat()
                if isinstance(r["fetched_at"], datetime) else None,
            "rank": float(r["rank"]) if r["rank"] is not None else 0.0,
        })

    result = {
        "rows": rows,
        "refs": refs,
        "count": len(rows),
        "query": query,
        "backing": "postgres_fts",
        "scope_predicate_applied": False,
    }
    if not rows:
        # A QUIET empty. ``plainto_tsquery('simple', …)`` ANDs every
        # lexeme it finds, and the 'simple' configuration has no stopword
        # list and no stemming, so every word in the question — "the",
        # "of", a plural — becomes a term the title+summary must ALL
        # carry. Live, 2026-09-28: a five-term natural-language query came
        # back with zero rows and was read as "no coverage", when the
        # corpus had plenty and the conjunction was the whole story.
        # Never let this read look like an answer about the world.
        required = ", ".join(repr(t) for t in lexemes) or (
            "none — the query produced no searchable term at all"
        )
        result["lexemes_required"] = lexemes
        result["why_empty"] = (
            "No signal's title or summary contains ALL of these terms. "
            "This search requires EVERY lexeme to be present (AND, not "
            "OR) and matches them literally — no stemming and no "
            "stopwords, so short words count as terms too. Required "
            f"here: {required}. Drop terms and search again, or use "
            "search_corpus, which is BM25 over the full body and ranks "
            "partial matches instead of requiring all of them — that is "
            "the right tool for a natural-language question."
        )
    if scope_predicate:
        # We accept the argument so the kind can pass it through, but
        # we don't evaluate Starlark here yet — surface the deferral
        # instead of silently filtering.  See module docstring.
        result["scope_predicate_note"] = (
            "scope_predicate received but not evaluated — Starlark "
            "row-level evaluation is a known follow-up."
        )
    if known_at is not None:
        result["known_at_cut"] = _known_at.cut_stamp(
            known_at, dropped=0, how="sql: signals.created_at < known_at",
        )
    return result
