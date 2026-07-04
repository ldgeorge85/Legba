# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``indicator_tracker`` sub-handler — S3-T2 structured-I&W run-over-run diff.

The S3-T1 slice gave each bounded unit a machine-checkable MIRROR of its
forward-looking "Indicators to watch" prose: ``FindingPayload.data.indicators[]``
(stored at ``analyst_outputs.data->'data'->'indicators'``), each entry a stable
``id`` slug + a ``status`` (``triggered`` / ``not_observed`` / ``expired``). This
handler closes the loop: a DETERMINISTIC (no-LLM) META analyst that DIFFS those
indicators run-over-run per ``(target_id, source unit analyst_id)`` and emits an
honest summary FINDING whenever an indicator's status FLIPS — most importantly a
``not_observed`` → ``triggered`` activation (a pre-registered warning signpost
firing).

Why deterministic + META
-------------------------

Diffing a stable structured field between two of a unit's own runs is pure code
over already-materialized findings — no interpretation, no LLM. It reads the
whole indicator-bearing finding pool directly via ``deps.pg_pool`` (a single
global sweep per cadence tick — NO ``subscription.targets`` → one run, not a
per-target fan-out), joins the two MOST-RECENT runs of each unit-stream by the
indicator ``id`` slug, and reports the status transitions.

Honesty / idempotency (the findings-feed dedup lesson)
------------------------------------------------------

The summary finding is a per-run RECEIPT — it must reach the feed ONLY when
something actually flipped, or an idempotent re-run over the same two source
findings would repeat the row every cadence tick (the exact noise the
``force_trace_only`` seam — see ``inline_target.py`` — exists to kill):

  * NO flips this run → ``force_trace_only=True`` (trace + no feed row).
  * Flips found, but the flip set is BYTE-IDENTICAL to the last EMITTED
    indicator_tracker finding (same two source runs, re-swept) → likewise
    ``force_trace_only=True`` (live path only; degrade-to-emit on any dedup
    error). The body is deterministic from the flip set, so a body match == an
    unchanged flip set — the same dedup contract ``thematic_proposal`` uses.

A newly-INTRODUCED indicator id (present in the latest run, absent from the
prior) is NOT a flip — it has no prior status to transition from — so a unit
first populating its indicators never floods the feed. Only a genuine status
transition on an id present in BOTH runs counts.

``deps=None`` runs the synthetic (no-DB) path: ``inputs`` are pre-shaped finding
rows (``id`` / ``target_id`` / ``analyst_id`` / ``produced_at`` + the indicator
list, reachable as ``row['indicators']`` or the persisted
``row['data']['data']['indicators']`` shape) — used by the unit tests.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "indicator_tracker"

# The three I&W indicator statuses (schemas.analyst.IndicatorEntry.status).
_STATUSES = frozenset({"triggered", "not_observed", "expired"})

# Only findings produced no earlier than this many days ago are diffed — an
# indicator stream older than this is settled history, not a live watch. Generous
# default; override via options['lookback_days'].
_DEFAULT_LOOKBACK_DAYS = 30
# Per-run safety cap on how many flips ride inside the summary finding's data /
# body (the flip rows are the product; keep the JSONB + feed row bounded).
_MAX_FLIPS_IN_FINDING = 200


# ---------------------------------------------------------------------------
# Indicator extraction (shared by the live + synthetic paths)
# ---------------------------------------------------------------------------


def _parse_data(raw: Any) -> dict[str, Any]:
    """Normalize a finding row's ``data`` to a dict (tolerate a JSONB str)."""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _extract_indicators(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The indicator entries carried by one finding row.

    Reads, in priority order:
      1. a direct ``row['indicators']`` list (the convenience shape unit tests
         author);
      2. the persisted payload shape — ``analyst_outputs.data`` is the full
         ``FindingPayload`` model_dump, so a unit's structured indicators live at
         ``data->'data'->'indicators'`` (nested payload sub-dict); a top-level
         ``data->'indicators'`` is tolerated as a fallback.

    Returns only well-shaped entries (a dict carrying a non-empty ``id`` and a
    known ``status``); everything else is dropped (degrade-not-drop).
    """
    direct = row.get("indicators")
    if isinstance(direct, list):
        return _clean_indicators(direct)
    data = _parse_data(row.get("data"))
    inner = data.get("data")
    candidates = ((inner,) if isinstance(inner, Mapping) else ()) + (data,)
    for src in candidates:
        ind = src.get("indicators")
        if isinstance(ind, list):
            return _clean_indicators(ind)
    return []


def _clean_indicators(entries: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, Mapping):
            continue
        eid = e.get("id")
        status = e.get("status")
        if not eid or status not in _STATUSES:
            continue
        out.append(dict(e))
    return out


def _index_by_id(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map indicator ``id`` slug → entry (last wins on a duplicate id)."""
    idx: dict[str, dict[str, Any]] = {}
    for e in entries:
        idx[str(e["id"])] = e
    return idx


# DQ P6 — normalized-statement fuzzy-join fallback. Most units re-mint their
# indicator id slugs run-over-run (56% of consecutive-run pairs share ZERO ids),
# so the id-join is structurally blind to ~74% of indicators. When TWO runs of a
# unit-stream share NO ids at all, fall back to joining on the normalized
# STATEMENT text — the human-readable signpost is far more stable than the slug —
# so a status flip on a re-slugged-but-same signpost is still caught. The
# descriptor-side canonical-vocabulary fix makes ids stable going forward; this
# is the code safety net for the streams that still churn.
_WS_RE = re.compile(r"\s+")
_NONWORD_RE = re.compile(r"[^a-z0-9 ]+")


def _normalize_statement(statement: Any) -> str:
    """Lowercased, punctuation-stripped, whitespace-collapsed statement key."""
    s = str(statement or "").strip().lower()
    s = _NONWORD_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _index_by_statement(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map normalized-statement key → entry (last wins), dropping blank keys."""
    idx: dict[str, dict[str, Any]] = {}
    for e in entries:
        key = _normalize_statement(e.get("statement"))
        if key:
            idx[key] = e
    return idx


# ---------------------------------------------------------------------------
# Diff core
# ---------------------------------------------------------------------------


def _is_activation(from_status: str, to_status: str) -> bool:
    """A flip INTO ``triggered`` from a non-triggered state — a warning signpost
    firing (the most consequential I&W transition, esp. not_observed→triggered)."""
    return to_status == "triggered" and from_status != "triggered"


def _flips_between(
    prev: list[dict[str, Any]],
    curr: list[dict[str, Any]],
    *,
    target_id: str | None,
    analyst_id: str | None,
) -> list[dict[str, Any]]:
    """Status transitions on indicator ids present in BOTH runs.

    A newly-introduced id (in ``curr`` only) is NOT a flip (no prior status); a
    dropped id (in ``prev`` only) is not reported as a flip either.
    """
    p = _index_by_id(prev)
    c = _index_by_id(curr)
    # DQ P6 — join on the STABLE key. Prefer the id slug; but when the two runs
    # share NO ids at all (the id-join is empty — the re-slugging churn), fall
    # back to the normalized STATEMENT text so a flip on a re-slugged-but-same
    # signpost is still caught. ``match`` records which join produced each flip.
    if set(p) & set(c):
        pj, cj, match = p, c, "id"
    else:
        pj, cj, match = _index_by_statement(prev), _index_by_statement(curr), "statement"

    flips: list[dict[str, Any]] = []
    for key, ce in cj.items():
        pe = pj.get(key)
        if pe is None:
            continue
        from_status = str(pe.get("status"))
        to_status = str(ce.get("status"))
        if from_status == to_status:
            continue
        flips.append({
            "target_id": target_id,
            "source_analyst_id": analyst_id,
            # Report the CURRENT run's id slug (stable when matched by id; the
            # freshest slug when matched by statement).
            "indicator_id": str(ce.get("id")),
            "statement": str(ce.get("statement") or pe.get("statement") or "")[:2048],
            "from_status": from_status,
            "to_status": to_status,
            "activation": _is_activation(from_status, to_status),
            "match": match,
            "citations": ce.get("citations") if isinstance(ce.get("citations"), list) else [],
        })
    return flips


def _group_key(row: Mapping[str, Any]) -> tuple[Any, Any]:
    return (row.get("target_id"), row.get("analyst_id"))


def _run_sort_key(row: Mapping[str, Any]) -> tuple[Any, str]:
    # Newest run first: latest produced_at, tie-broken by largest id.
    return (row.get("produced_at"), str(row.get("id")))


def collect_flips(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Diff the two most-recent runs of each ``(target_id, analyst_id)`` stream.

    Returns ``(flips, groups_compared)`` — ``groups_compared`` counts streams that
    had >=2 indicator-bearing runs (a stream with a single run has no prior to
    diff against and is skipped). Flips are ordered deterministically: activations
    (→triggered) first, then by ``(target_id, analyst_id, indicator_id)`` so the
    finding body is stable run-to-run (needed for the byte-identical dedup).
    """
    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[_group_key(r)].append(r)

    flips: list[dict[str, Any]] = []
    groups_compared = 0
    for (target_id, analyst_id), members in groups.items():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=_run_sort_key, reverse=True)
        curr, prev = ordered[0], ordered[1]
        pair_flips = _flips_between(
            _extract_indicators(prev),
            _extract_indicators(curr),
            target_id=target_id,
            analyst_id=analyst_id,
        )
        if pair_flips:
            groups_compared += 1
            flips.extend(pair_flips)

    flips.sort(
        key=lambda f: (
            0 if f["activation"] else 1,
            str(f["target_id"]),
            str(f["source_analyst_id"]),
            str(f["indicator_id"]),
        )
    )
    return flips, groups_compared


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(flips: list[dict[str, Any]], groups_compared: int) -> FindingPayload:
    activations = [f for f in flips if f["activation"]]
    n = len(flips)
    a = len(activations)
    if n:
        title = (
            f"Indicator tracker: {n} indicator flip(s), {a} newly triggered, "
            f"across {groups_compared} unit-run(s)"
        )
        lines: list[str] = []
        for f in flips[:_MAX_FLIPS_IN_FINDING]:
            tgt = f["target_id"] or "global"
            mark = "!! " if f["activation"] else "- "
            lines.append(
                f"{mark}[{tgt}/{f['source_analyst_id']}] "
                f"{f['statement']}: {f['from_status']} -> {f['to_status']}"
            )
        body = "\n".join(lines)
    else:
        title = "Indicator tracker: no indicator status changes"
        body = "No indicator status flips across the diffed unit-runs this sweep."
    tags = ["deterministic", SUB_HANDLER_NAME]
    if a:
        tags.append("indicator_triggered")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "flip_count": n,
            "activation_count": a,
            "groups_compared": groups_compared,
            "flips": flips[:_MAX_FLIPS_IN_FINDING],
        },
    )


# ---------------------------------------------------------------------------
# Live-pool path (asyncpg)
# ---------------------------------------------------------------------------


async def _fetch_indicator_runs(
    conn: Any, *, lookback_days: int,
) -> list[dict[str, Any]]:
    """The two most-recent indicator-bearing findings per (target_id, analyst_id).

    A window function partitions the recent finding pool by unit-stream and keeps
    only the latest two runs (rn<=2) that actually carry a non-empty structured
    ``data->'data'->'indicators'`` array — the diff never needs more than two.
    ``lookback_days`` is int-coerced into the interval literal (injection-safe).
    """
    rows = await conn.fetch(
        f"""
        SELECT id, target_id, analyst_id, produced_at, data
        FROM (
            SELECT id, target_id, analyst_id, produced_at, data,
                   row_number() OVER (
                       PARTITION BY target_id, analyst_id
                       ORDER BY produced_at DESC, id DESC
                   ) AS rn
            FROM analyst_outputs
            WHERE kind = 'finding'
              AND produced_at > NOW() - INTERVAL '{int(lookback_days)} days'
              AND jsonb_typeof(data->'data'->'indicators') = 'array'
              AND jsonb_array_length(data->'data'->'indicators') > 0
        ) t
        WHERE t.rn <= 2
        """,
    )
    return [
        {
            "id": r["id"],
            "target_id": r["target_id"],
            "analyst_id": r["analyst_id"],
            "produced_at": r["produced_at"],
            "data": r["data"],
        }
        for r in rows
    ]


async def _last_emitted_body(pool: Any, analyst_id: str) -> str | None:
    """Body of the most recent FEED finding this analyst emitted (or None).

    Trace-only suppressed runs write no ``analyst_outputs`` row, so this is the
    last NON-suppressed summary — exactly what a re-swept identical flip set
    should be deduped against.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT body FROM analyst_outputs "
            "WHERE analyst_id = $1 AND kind = 'finding' "
            "ORDER BY produced_at DESC LIMIT 1",
            analyst_id,
        )
    return row["body"] if row else None


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see the module docstring.

    ``deps`` is the analyst pool bundle (``deps.pg_pool``); ``deps=None`` runs the
    synthetic path (diffs pre-shaped ``inputs`` rows, no DB) for unit tests.

    Options
    -------
    lookback_days:
        Only findings this recent are diffed (default 30).
    analyst_id:
        This analyst's own id (used to scope the last-emitted-body dedup lookup).
    """
    lookback_days = int(options.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await _fetch_indicator_runs(conn, lookback_days=lookback_days)
            flips, groups_compared = collect_flips(rows)
        except Exception as exc:  # noqa: BLE001 — degrade-not-drop
            logger.warning("indicator_tracker.pool_failed err=%s", exc)
            flips, groups_compared = [], 0
    else:
        flips, groups_compared = collect_flips([dict(r) for r in inputs])

    finding = _build_finding(flips, groups_compared)

    # Emit a FEED finding only when there is a flip to surface. A no-flip sweep is
    # suppressed (trace-only) so an idempotent re-run doesn't repeat 'no changes'
    # every cadence tick; and on the live path a flip set byte-identical to the
    # last EMITTED summary (the same two source runs, re-swept) is likewise
    # suppressed. Degrade to emit on any dedup-check failure.
    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    force_trace_only = not flips
    if flips and pool is not None:
        try:
            force_trace_only = (
                await _last_emitted_body(pool, analyst_id) == finding.body
            )
        except Exception as exc:  # noqa: BLE001 — degrade: emit rather than crash
            logger.warning("indicator_tracker.dedup_check_failed err=%s", exc)
            force_trace_only = False

    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        force_trace_only=force_trace_only,
    )


__all__ = ["handle", "collect_flips", "SUB_HANDLER_NAME"]
