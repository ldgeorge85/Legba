#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""sample_k4_match_worksheet.py — draw the K-4 match-gold labeling worksheet.

K-4 (``docs/SEAMS.md`` #49, ``planning/COHERENCE_WAVES_PLAN_2026-07-28.md``
§6) is the match-PRECISION gold loop that gates the ``claim_watch`` closer
(K-5): a human/frontier-model reviewer judges a STRATIFIED sample of
``bearing_edges`` rows (the "new evidence bears on old question" pointers
``claim_watch`` writes — see
:mod:`legba.data.analysts.deterministic_handlers.claim_watch`'s module
docstring for the fused vector+entity+geo matching model this measures) so
per-class precision can be computed against an agreed bar (DEC-K1,
recommendation on the table: pairwise precision >= 0.85).

THIS SCRIPT ONLY DRAWS THE SAMPLE AND RENDERS THE WORKSHEET. Labeling is
explicitly OUT OF PLANE (the plan's own words: "labeling prefers a Fable
session — out-of-plane frontier + live search, the W31 provenance pattern")
and is never performed here — every labeler column this script writes is
blank.

SAMPLING STRATA (stratified, not uniform — the fusion model's own docstring
table shows the interesting cases are NOT evenly distributed: entity-only
matches vastly outnumber vector-supported ones):

  * ``vector_bearing``  — ALL edges (any matcher_version) whose ``planes``
    contains ``'vector'``. Rare by construction (the module docstring's own
    measurement: a 0.45 vector floor still admits only the top ~decile of
    genuinely related pairs) and the highest-value stratum to confirm,
    because a false vector match would mean the semantic plane itself is
    unreliable, not just mistuned.
  * ``v3_0_0`` (~60 total) — non-vector ``claim_watch/3.0.0`` edges,
    stratified across the two plane shapes the fusion model can actually
    produce without vector support (``entity_only`` / ``entity_geo`` — see
    ``fuse_weight``'s docstring table; a bare ``geo`` match can never clear
    the 0.45 threshold, so these two are exhaustive) CROSSED with three
    weight bands (0.45-0.55 / 0.55-0.70 / 0.70+), random within each of the
    six cells (session-seeded ``setseed`` + ``ORDER BY random()`` — see
    ``_open_readonly_conn``). A cell with fewer live rows than the per-cell
    target simply yields fewer rows — never padded, never borrowed from
    another cell.
  * ``v3_1_0`` (up to ~40) — ALL non-vector ``claim_watch/3.1.0`` edges
    (the floor-change matcher version), newest-first up to the cap. This is
    the version the precision measurement is actually FOR: 3.0.0 is the
    baseline the fusion-model docstring already characterizes at length.

Overlap is impossible BY CONSTRUCTION, not by post-hoc dedup: the three
strata's SQL predicates partition on ``'vector' = ANY(planes)`` and
``matcher_version``, so no edge can satisfy two of them in the same run.
The ONLY dedup this script needs is against a PRIOR run's worksheet (see
APPEND below).

RENDERING (per sampled edge): edge id, matcher_version, weight, planes,
desk (the destination question's ``target_id``, resolved to its
``target_descriptors`` head name where one exists), the QUESTION thesis
text, the SIGNAL title + summary (HTML-stripped, truncated) + canonical_url
+ fetched_at, and a best-effort SHARED-ENTITY name list — the exact same
(Not every source shapes a ``title``/``summary`` pair — Telegram-sourced
signals carry only ``payload.text`` — so the summary falls back through
``summary`` / ``text`` / ``description`` / ``raw_body`` / ``content_text``
in that order, and a missing title is synthesized from the resolved body's
first ~100 chars rather than rendered blank.)
canonical-entity overlap ``claim_watch`` itself computes
(:data:`legba.data.analysts.deterministic_handlers.claim_watch._QUESTION_LINEAGE_SQL`
/ ``_SIGNAL_ENTITIES_SQL``, reused verbatim rather than re-derived, mirroring
``scripts/measure_claim_watch_cosines.py``'s precedent for cross-script SQL
reuse), MINUS the NER-surface-name fallback claim_watch also applies for
UNLINKED signals — that fallback calls a live entity-resolution helper
per name and is not "cheap" for an offline sampler, so a signal the
resolution sweep has not linked yet renders an empty shared-entity list here
even where claim_watch's own live run found a name-based match. This is a
sampler simplification, not a claim about the edge's correctness — the
labeler judges the edge from the thesis/title/summary text regardless.

WORKSHEET COLUMNS (leave-blank labeler columns come last):
  ``label`` — closed vocabulary, one of: correct_match / related_not_evidence
    / entity_confusion / temporal_stale / geo_mismatch / spurious. This IS
    the failure-class taxonomy (the task's own framing): grouping by this
    column is how per-class precision gets reported instead of one pooled
    average.
  ``failure_class`` — optional FREE-TEXT elaboration on why, when the
    closed-vocabulary judgment above needs more nuance than the six labels
    give (e.g. which entity got confused, how stale). Blank is fine,
    including for a correct_match.
  ``labeled_by`` — REQUIRED whenever ``label`` is filled (the plan's
    never-unlabeled-provenance rule, the W31 pattern): stamp who/what judged
    it, e.g. ``fable+websearch`` or an operator name.
  ``notes`` — anything else worth recording.

APPEND / RE-RUN CONTRACT: running this script again against an EXISTING
worksheet path never disturbs a row already in the file (including any
labels a human has since filled in) — existing rows are read back verbatim
and kept in place; only edges NOT already present (by ``edge_id``) are
newly sampled and appended. This is how newly-arriving 3.1.0 edges get
folded in over time without re-drawing (and re-numbering) the whole sheet.

SAFETY (read-only): the sampling connection is opened with
``default_transaction_read_only = on`` at the Postgres SESSION level
(server-enforced — mirrors ``scripts/measure_entity_merge_quality.py``'s
``_open_readonly_conn``) IN ADDITION to every statement here being a
SELECT. No migration, no schema change, no write path, no import of
anything that wires a write-capable client.

USAGE
-----
    python3 scripts/sample_k4_match_worksheet.py \\
        --out planning/K4_MATCH_GOLD_WORKSHEET.csv

Re-run later (same command) to append newly-arrived edges (e.g. more
3.1.0 rows) without disturbing existing labeled rows.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import html
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv optional
    pass

from legba.data.analysts.deterministic_handlers.claim_watch import (
    _parse_jsonish,
    _QUESTION_LINEAGE_SQL,
    _SIGNAL_ENTITIES_SQL,
)
from legba.data.config import PostgresConfig

WORKSHEET_SCHEMA_VERSION = "legba/k4-match-gold-worksheet/1-0-0"

#: Closed label vocabulary (docs/SEAMS.md #49 / DEC-K1). This taxonomy IS the
#: failure-class breakdown the K-4 precision measurement reports per-class,
#: not a single pooled accuracy number.
LABEL_VOCAB: tuple[str, ...] = (
    "correct_match",
    "related_not_evidence",
    "entity_confusion",
    "temporal_stale",
    "geo_mismatch",
    "spurious",
)

WORKSHEET_FIELDS = [
    "edge_id", "sample_stratum", "matcher_version", "weight", "planes",
    "desk", "question_id", "question_thesis", "question_produced_at",
    "signal_id", "signal_title", "signal_summary", "signal_canonical_url",
    "signal_fetched_at", "shared_entities", "edge_created_at",
    "label", "failure_class", "labeled_by", "notes",
]

_HEADER_COMMENT = (
    "# {schema}\n"
    "# K-4 MATCH-GOLD WORKSHEET - claim_watch precision measurement\n"
    "# (docs/SEAMS.md #49; DEC-K1 bar recommendation: pairwise precision >= 0.85)\n"
    "#\n"
    "# LABELING IS OUT-OF-PLANE (never performed by this sampler). Columns\n"
    "# below `edge_created_at` are the ONLY ones a labeler fills; everything\n"
    "# before that is sampler-populated context - read it, do not edit it.\n"
    "#\n"
    "#   label       - REQUIRED closed vocabulary, exactly one of:\n"
    "#                   correct_match         - the signal genuinely bears on the question\n"
    "#                   related_not_evidence  - same topic/desk but not real evidence for THIS question\n"
    "#                   entity_confusion      - matched on a shared name, not the same real-world entity\n"
    "#                   temporal_stale        - old news the question has already absorbed\n"
    "#                   geo_mismatch          - the geo overlap was spurious (wrong country/region)\n"
    "#                   spurious              - no discernible relationship at all\n"
    "#                 Grouping by this column is how PER-CLASS precision gets reported -\n"
    "#                 that is the point of this worksheet, not one pooled average.\n"
    "#   failure_class - optional free-text elaboration (which entity got confused, how\n"
    "#                 stale, etc). Blank is fine, including for a correct_match.\n"
    "#   labeled_by  - REQUIRED whenever `label` is filled (never-unlabeled-provenance\n"
    "#                 rule): stamp who/what judged it, e.g. 'fable+websearch' or an\n"
    "#                 operator name. A filled label with an empty labeled_by is a bug.\n"
    "#   notes       - anything else worth recording.\n"
)
_HEADER_COMMENT_LINE_COUNT = _HEADER_COMMENT.count("\n")


# ===========================================================================
# Formula-injection guard (CWE-1236) - text fields come from arbitrary
# ingested/model-produced content and this CSV is opened by a human in a
# spreadsheet app. Mirrors scripts/measure_entity_merge_quality.py verbatim.
# ===========================================================================

_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")
_FORMULA_GUARD_PREFIX = "'"


def _apply_formula_guard(value: str) -> str:
    if value and value[0] in _FORMULA_TRIGGER_CHARS:
        return _FORMULA_GUARD_PREFIX + value
    return value


def _strip_formula_guard(value: str) -> str:
    if (
        len(value) >= 2
        and value[0] == _FORMULA_GUARD_PREFIX
        and value[1] in _FORMULA_TRIGGER_CHARS
    ):
        return value[1:]
    return value


# ===========================================================================
# HTML clean (signal summary/title are often content:encoded HTML). Copied
# from signal_embedder._clean_html so this script stays self-contained (a
# sibling-private helper is not part of that module's public surface) -
# same convention that module itself documents using for the same reason.
# ===========================================================================

_HTML_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WS_RE = re.compile(r"\s+")
_MAX_SUMMARY_CHARS = 600


def _clean_html(text: Any) -> str:
    if not text or not isinstance(text, str):
        return ""
    text = _HTML_SCRIPT_STYLE_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _HTML_WS_RE.sub(" ", text).strip()


# ===========================================================================
# Worksheet row (pure dataclass - no I/O)
# ===========================================================================


@dataclass
class WorksheetRow:
    edge_id: str
    sample_stratum: str
    matcher_version: str
    weight: str
    planes: str
    desk: str
    question_id: str
    question_thesis: str
    question_produced_at: str
    signal_id: str
    signal_title: str
    signal_summary: str
    signal_canonical_url: str
    signal_fetched_at: str
    shared_entities: str
    edge_created_at: str
    label: str = ""
    failure_class: str = ""
    labeled_by: str = ""
    notes: str = ""

    def to_csv_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in WORKSHEET_FIELDS}


def write_worksheet(path: Path, rows: list[WorksheetRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(_HEADER_COMMENT.format(schema=WORKSHEET_SCHEMA_VERSION))
        writer = csv.DictWriter(f, fieldnames=WORKSHEET_FIELDS)
        writer.writeheader()
        for r in rows:
            guarded = {
                k: _apply_formula_guard(v) for k, v in r.to_csv_dict().items()
            }
            writer.writerow(guarded)


def read_worksheet(path: Path) -> list[WorksheetRow]:
    """Read a (possibly human-filled) worksheet back, skipping the fixed-size
    leading header-comment block by LINE COUNT (not by '#' content-filtering,
    which could wrongly strip a real record whose sampled text contains an
    embedded newline followed by '#') and reversing the formula guard."""
    with path.open("r", newline="", encoding="utf-8") as f:
        lines = f.readlines()[_HEADER_COMMENT_LINE_COUNT:]
    reader = csv.DictReader(lines)
    out: list[WorksheetRow] = []
    for row in reader:
        out.append(WorksheetRow(**{
            k: _strip_formula_guard(row.get(k) or "") for k in WORKSHEET_FIELDS
        }))
    return out


# ===========================================================================
# Read-only DB connection (session-level read-only + optional setseed).
# Mirrors scripts/measure_entity_merge_quality.py's _open_readonly_conn.
# ===========================================================================


def _seed_to_pg_setseed(seed: int) -> float:
    """Map an integer CLI seed to the [-1.0, 1.0) float ``setseed()`` needs.
    Pure - fixture-testable without a DB. Total for any int (Python's ``%``
    on a negative operand returns non-negative, so this stays in range for a
    negative or huge seed too)."""
    return ((seed % 2_000_000) / 1_000_000.0) - 1.0


async def _open_readonly_conn(cfg: PostgresConfig, *, seed: int | None):
    import asyncpg

    conn = await asyncpg.connect(
        host=cfg.host, port=cfg.port, user=cfg.user,
        password=cfg.password, database=cfg.database,
        server_settings={"default_transaction_read_only": "on"},
    )
    if seed is not None:
        await conn.execute("SELECT setseed($1)", _seed_to_pg_setseed(seed))
    return conn


# ===========================================================================
# Strata
# ===========================================================================

_BASE_EDGE_PREDICATE = (
    "e.edge_kind = 'bears_on' AND e.src_kind = 'signal' "
    "AND e.dst_kind = 'hypothesis' AND e.provenance_class = 'live'"
)

_PLANE_MIX_PREDICATES: tuple[tuple[str, str], ...] = (
    ("entity_only", "'entity' = ANY(e.planes) AND NOT ('geo' = ANY(e.planes))"),
    ("entity_geo", "'entity' = ANY(e.planes) AND 'geo' = ANY(e.planes)"),
)

_WEIGHT_BANDS: tuple[tuple[str, str], ...] = (
    ("0.45-0.55", "e.weight >= 0.45 AND e.weight < 0.55"),
    ("0.55-0.70", "e.weight >= 0.55 AND e.weight < 0.70"),
    ("0.70+", "e.weight >= 0.70"),
)

V30_MATCHER_VERSION = "claim_watch/3.0.0"
V31_MATCHER_VERSION = "claim_watch/3.1.0"
V32_MATCHER_VERSION = "claim_watch/3.2.0"


@dataclass(frozen=True)
class StratumSpec:
    key: str
    predicate: str  # extra SQL predicate (fixed internal strings only)
    order_sql: str
    limit: int


def _build_strata(*, vector_cap: int, v31_cap: int, v30_per_cell: int) -> list[StratumSpec]:
    strata = [
        StratumSpec(
            key="vector_bearing",
            predicate="'vector' = ANY(e.planes)",
            order_sql="e.created_at DESC",
            limit=vector_cap,
        ),
        StratumSpec(
            key="v3_1_0",
            predicate=(
                f"e.matcher_version = '{V31_MATCHER_VERSION}' "
                "AND NOT ('vector' = ANY(e.planes))"
            ),
            order_sql="e.created_at DESC",
            limit=v31_cap,
        ),
        # 3.2.0 — the post-tune crop (meta-exclusion + hub damping + omnibus
        # cap live). Same cap and shape as the 3.1.0 stratum: this is the
        # version each re-measure round is actually FOR.
        StratumSpec(
            key="v3_2_0",
            predicate=(
                f"e.matcher_version = '{V32_MATCHER_VERSION}' "
                "AND NOT ('vector' = ANY(e.planes))"
            ),
            order_sql="e.created_at DESC",
            limit=v31_cap,
        ),
    ]
    for plane_key, plane_pred in _PLANE_MIX_PREDICATES:
        for band_key, band_pred in _WEIGHT_BANDS:
            strata.append(StratumSpec(
                key=f"v3_0_0:{plane_key}:{band_key}",
                predicate=(
                    f"e.matcher_version = '{V30_MATCHER_VERSION}' "
                    f"AND NOT ('vector' = ANY(e.planes)) "
                    f"AND {plane_pred} AND {band_pred}"
                ),
                order_sql="random()",
                limit=v30_per_cell,
            ))
    return strata


_EDGE_COLUMNS = (
    "e.id, e.matcher_version, e.weight, e.planes, e.src_id, e.src_as_of, "
    "e.dst_id, e.dst_as_of, e.created_at"
)


async def _count_population(conn: Any, stratum: StratumSpec) -> int:
    row = await conn.fetchval(
        f"SELECT count(*) FROM bearing_edges e "
        f"WHERE {_BASE_EDGE_PREDICATE} AND {stratum.predicate}"
    )
    return int(row or 0)


async def _sample_stratum(
    conn: Any, stratum: StratumSpec, *, exclude_ids: Sequence[UUID]
) -> list[Any]:
    sql = (
        f"SELECT {_EDGE_COLUMNS} FROM bearing_edges e "
        f"WHERE {_BASE_EDGE_PREDICATE} AND {stratum.predicate} "
        f"AND NOT (e.id = ANY($1::uuid[])) "
        f"ORDER BY {stratum.order_sql} LIMIT $2"
    )
    return list(await conn.fetch(sql, list(exclude_ids), stratum.limit))


# ===========================================================================
# Context batch-fetch (hypotheses / signals / desk names / shared entities)
# ===========================================================================

_HYPOTHESES_BY_ID_SQL = """
    SELECT id, thesis, target_id, produced_at
      FROM hypotheses
     WHERE id = ANY($1::uuid[])
"""

_SIGNALS_BY_ID_SQL = """
    SELECT id, fetched_at, canonical_url, payload
      FROM signals
     WHERE id = ANY($1::uuid[])
"""

_DESK_NAMES_SQL = """
    SELECT descriptor_id, name
      FROM target_descriptors
     WHERE is_head = TRUE
       AND descriptor_id = ANY($1::text[])
"""

_ENTITY_NAMES_BY_ID_SQL = """
    SELECT id, canonical_name
      FROM entity_profiles
     WHERE id = ANY($1::uuid[])
"""


@dataclass
class Context:
    hypotheses: dict[str, Any]
    signals: dict[str, Any]
    desk_names: dict[str, str]
    q_entities: dict[str, set[str]]
    s_entities: dict[str, set[str]]
    entity_names: dict[str, str]


async def _load_context(
    conn: Any, question_ids: set[str], signal_ids: set[str]
) -> Context:
    q_uuids = [UUID(q) for q in question_ids]
    s_uuids = [UUID(s) for s in signal_ids]

    hyp_rows = await conn.fetch(_HYPOTHESES_BY_ID_SQL, q_uuids) if q_uuids else []
    hypotheses = {str(r["id"]): r for r in hyp_rows}

    sig_rows = await conn.fetch(_SIGNALS_BY_ID_SQL, s_uuids) if s_uuids else []
    signals = {str(r["id"]): r for r in sig_rows}

    target_ids = sorted({
        str(h["target_id"]) for h in hypotheses.values() if h["target_id"]
    })
    desk_rows = await conn.fetch(_DESK_NAMES_SQL, target_ids) if target_ids else []
    desk_names = {str(r["descriptor_id"]): str(r["name"] or "") for r in desk_rows}

    lineage_rows = (
        await conn.fetch(_QUESTION_LINEAGE_SQL, q_uuids) if q_uuids else []
    )
    q_entities: dict[str, set[str]] = {}
    for r in lineage_rows:
        q_entities[str(r["qid"])] = {
            str(e) for e in (r["entity_ids"] or []) if e is not None
        }

    sig_ent_rows = (
        await conn.fetch(_SIGNAL_ENTITIES_SQL, s_uuids) if s_uuids else []
    )
    s_entities: dict[str, set[str]] = {}
    for r in sig_ent_rows:
        s_entities[str(r["sid"])] = {
            str(e) for e in (r["entity_ids"] or []) if e is not None
        }

    all_entity_ids: set[str] = set()
    for ents in q_entities.values():
        all_entity_ids.update(ents)
    for ents in s_entities.values():
        all_entity_ids.update(ents)
    entity_names: dict[str, str] = {}
    if all_entity_ids:
        name_rows = await conn.fetch(
            _ENTITY_NAMES_BY_ID_SQL, [UUID(e) for e in all_entity_ids]
        )
        entity_names = {
            str(r["id"]): str(r["canonical_name"] or "") for r in name_rows
        }

    return Context(
        hypotheses=hypotheses, signals=signals, desk_names=desk_names,
        q_entities=q_entities, s_entities=s_entities, entity_names=entity_names,
    )


def _render_row(edge: Any, stratum_key: str, ctx: Context) -> WorksheetRow:
    qid = str(edge["dst_id"])
    sid = str(edge["src_id"])
    hyp = ctx.hypotheses.get(qid)
    sig = ctx.signals.get(sid)

    target_id = str(hyp["target_id"]) if hyp and hyp["target_id"] else ""
    desk_name = ctx.desk_names.get(target_id, "")
    desk = f"{desk_name} [{target_id}]" if desk_name else (target_id or "(no desk)")

    question_thesis = str(hyp["thesis"] or "") if hyp else "(question row not found)"
    question_produced_at = str(hyp["produced_at"]) if hyp and hyp["produced_at"] else ""

    # asyncpg on the raw (codec-less) read-only connection returns jsonb as a
    # JSON STRING, not a dict (PostgresStore's connect() would register a
    # codec, but this script deliberately opens its own bare, read-only-
    # enforced connection - see _open_readonly_conn) - _parse_jsonish (reused
    # from claim_watch, which hits the exact same shape) handles both.
    payload = _parse_jsonish(sig["payload"]) if sig else None
    if not isinstance(payload, dict):
        payload = {}
    signal_title = str(payload.get("title") or "").strip()
    # Not every source shapes a title/summary pair - a live sample turned up
    # Telegram-sourced signals (payload carries only `text`, no `title`/
    # `summary` key at all) that would otherwise render two blank context
    # cells. Fall back through the same body-field precedence
    # signal_embedder._BODY_FIELDS uses, and synthesize a title from the
    # resolved body when the source has none, so the labeler always has
    # SOME preview text to judge the match by.
    body_text = None
    for field in ("summary", "text", "description", "raw_body", "content_text"):
        val = payload.get(field)
        if isinstance(val, str) and val.strip():
            body_text = val
            break
    signal_summary = _clean_html(body_text)[:_MAX_SUMMARY_CHARS]
    if not signal_title and signal_summary:
        signal_title = signal_summary[:100].rstrip() + (
            "…" if len(signal_summary) > 100 else ""
        )
    signal_canonical_url = str(sig["canonical_url"] or "") if sig else ""
    signal_fetched_at = str(sig["fetched_at"]) if sig and sig["fetched_at"] else ""
    if sig is None:
        signal_title = signal_title or "(signal row not found)"

    shared_ids = ctx.q_entities.get(qid, set()) & ctx.s_entities.get(sid, set())
    shared_names = sorted(
        {ctx.entity_names.get(e, "") for e in shared_ids if ctx.entity_names.get(e)}
    )

    return WorksheetRow(
        edge_id=str(edge["id"]),
        sample_stratum=stratum_key,
        matcher_version=str(edge["matcher_version"]),
        weight=f"{float(edge['weight']):.4f}",
        planes=",".join(edge["planes"] or []),
        desk=desk,
        question_id=qid,
        question_thesis=question_thesis,
        question_produced_at=question_produced_at,
        signal_id=sid,
        signal_title=signal_title,
        signal_summary=signal_summary,
        signal_canonical_url=signal_canonical_url,
        signal_fetched_at=signal_fetched_at,
        shared_entities=",".join(shared_names),
        edge_created_at=str(edge["created_at"]),
    )


# ===========================================================================
# Orchestration
# ===========================================================================


@dataclass
class StratumReport:
    key: str
    population: int
    sampled_new: int
    already_present: int


async def build_new_rows(
    cfg: PostgresConfig,
    *,
    existing_ids: set[str],
    seed: int | None,
    vector_cap: int,
    v31_cap: int,
    v30_per_cell: int,
) -> tuple[list[WorksheetRow], list[StratumReport]]:
    conn = await _open_readonly_conn(cfg, seed=seed)
    try:
        strata = _build_strata(
            vector_cap=vector_cap, v31_cap=v31_cap, v30_per_cell=v30_per_cell
        )
        exclude_uuids = [UUID(i) for i in existing_ids]

        edge_batches: list[tuple[str, list[Any]]] = []
        reports: list[StratumReport] = []
        for stratum in strata:
            population = await _count_population(conn, stratum)
            rows = await _sample_stratum(conn, stratum, exclude_ids=exclude_uuids)
            edge_batches.append((stratum.key, rows))
            # already_present: population minus rows NOT excluded gives the
            # count of live rows in this stratum that exclude_ids removed -
            # i.e. how many of THIS stratum's candidates were already in the
            # worksheet from a prior run (bounded probe: only meaningful up
            # to `population`, exact via a second count with the exclusion).
            present_count = await conn.fetchval(
                f"SELECT count(*) FROM bearing_edges e "
                f"WHERE {_BASE_EDGE_PREDICATE} AND {stratum.predicate} "
                f"AND e.id = ANY($1::uuid[])",
                exclude_uuids,
            )
            reports.append(StratumReport(
                key=stratum.key,
                population=population,
                sampled_new=len(rows),
                already_present=int(present_count or 0),
            ))

        question_ids = {
            str(r["dst_id"]) for _, rows in edge_batches for r in rows
        }
        signal_ids = {
            str(r["src_id"]) for _, rows in edge_batches for r in rows
        }
        ctx = await _load_context(conn, question_ids, signal_ids)

        new_rows: list[WorksheetRow] = []
        for stratum_key, rows in edge_batches:
            for r in rows:
                new_rows.append(_render_row(r, stratum_key, ctx))
        return new_rows, reports
    finally:
        await conn.close()


async def run(args: argparse.Namespace) -> None:
    cfg = PostgresConfig.from_env()
    out_path = Path(args.out)

    existing_rows: list[WorksheetRow] = []
    if out_path.exists():
        existing_rows = read_worksheet(out_path)
        print(f"Existing worksheet found: {out_path} ({len(existing_rows)} row(s))")
    existing_ids = {r.edge_id for r in existing_rows}

    new_rows, reports = await build_new_rows(
        cfg,
        existing_ids=existing_ids,
        seed=args.seed,
        vector_cap=args.vector_cap,
        v31_cap=args.v31_cap,
        v30_per_cell=args.v30_per_cell,
    )

    final_rows = existing_rows + new_rows
    write_worksheet(out_path, final_rows)

    print(f"\nWrote {len(final_rows)} total row(s) to {out_path} "
          f"({len(existing_rows)} pre-existing + {len(new_rows)} newly sampled)\n")
    print(f"{'stratum':<28} {'population':>10} {'already_in_sheet':>16} {'newly_sampled':>14}")
    for rep in reports:
        print(f"{rep.key:<28} {rep.population:>10} {rep.already_present:>16} {rep.sampled_new:>14}")

    vector_reports = [r for r in reports if r.key == "vector_bearing"]
    v31_reports = [r for r in reports if r.key == "v3_1_0"]
    if vector_reports:
        vr = vector_reports[0]
        print(f"\nvector-bearing edges at run time: {vr.population} "
              f"(sampled {vr.sampled_new + vr.already_present} of them into the sheet)")
    if v31_reports:
        vr31 = v31_reports[0]
        print(f"claim_watch/3.1.0 edges at run time: {vr31.population} "
              f"(sampled {vr31.sampled_new + vr31.already_present} of them into the sheet, cap={args.v31_cap})")

    print(
        "\nNEXT STEP: labeling happens OUT-OF-PLANE (see the worksheet's own "
        "header comment for the label vocabulary + labeled_by requirement). "
        "This script does not label anything."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out", default="planning/K4_MATCH_GOLD_WORKSHEET.csv",
        help="Worksheet CSV path (read back + appended-to if it already exists).",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Postgres setseed() applied to the sampling session, so the "
             "random v3.0.0 draws are reproducible across runs against an "
             "unchanged table (default 42; pass a different value or a "
             "fresh one for a non-reproducible draw).",
    )
    parser.add_argument(
        "--vector-cap", type=int, default=500,
        help="Safety cap on the vector-bearing stratum (task expectation: "
             "rare, so ALL of them are drawn up to this bound). Default 500.",
    )
    parser.add_argument(
        "--v31-cap", type=int, default=40,
        help="Cap on the claim_watch/3.1.0 stratum (newest-first). Default 40.",
    )
    parser.add_argument(
        "--v30-per-cell", type=int, default=10,
        help="Target rows per (plane_mix x weight_band) cell for the "
             "claim_watch/3.0.0 stratum - 6 cells, so the default targets "
             "~60 total. Default 10.",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
