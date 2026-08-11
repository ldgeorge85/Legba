# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verification machinery — operator + test-time sanity checks.

Entry points:

  * ``verify_provenance_complete(conn, table, row_id)`` — does the row carry
    all required provenance fields? Returns a structured ``ProvenanceReport``
    rather than raising, so callers can decide policy (fail-fast in tests,
    surface in UI for operators).

  * ``validate_lineage(conn, table, row_id, max_depth=20)`` — walks the
    ``derived_from`` graph backward; detects cycles, dangling refs, missing
    ancestors; returns ``LineageReport``.

  * ``verify_finding_faithfulness(...)`` — the P0-T2 MANDATORY faithfulness
    pass.  A DETERMINISTIC FLOOR (always on) checks every fact-asserting claim
    in a cited finding's prose against the resolved ``data['citations']``
    bridge: a claim that asserts a fact with NO ``[N]`` marker, or whose marker
    resolves to no real signal_id, is an UNSUPPORTED span; the score is the
    fraction of checkable claims that are supported.  An OPTIONAL LLM judge
    (flag-gated, soft-fail) refines per-claim verdicts; when the flag is off or
    the judge is unreachable the result degrades to the floor and is LABELLED
    ``judge-unavailable`` — it never fabricates a number.  The verdict is
    persisted as a ``critique`` so the existing critic-actuation gate
    (``effective_confidence = min(confidence, overall_score)``) consumes it.

The provenance helpers route through the existing L-001 ``query_ancestors``
recursive CTE where possible and use direct fetches for single-row checks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Hashable, Mapping, TypeVar
from uuid import UUID

import asyncpg

from ._core import is_valid_schema_uri
# QW1-B — the DESK GROUNDING citation vocabulary lives in ``kinds`` (already
# imported by both producer and consumer), NOT in the producing analyst module:
# importing ``data.analysts.inline_target`` from here would close an import cycle
# through ``runtime.analyst_method``.
from .kinds import is_grounding_citation
# K-1 (2026-08-05) — CITATION MARKERS are the judge subsystem's fifth brick: the
# canonical marker regexes plus every drift VARIANT the core plane emits, which
# is a list that grows one acceptance panel at a time. Imported ONE WAY and
# RE-EXPORTED, so ``verify._CLAIM_MARKER_RE`` / ``verify._normalize_verify_markers``
# resolve exactly as before.
from .citation_markers import (  # noqa: F401 — re-exported verify surface
    _ASCII_NO_CITATION_VARIANT_RE,
    _CLAIM_MARKER_RE,
    _CLAIM_RANGE_RE,
    _COMPOUND_REF_CITATION_RE,
    _MAX_RANGE_WIDTH,
    _NO_CITATION_MARKER,
    _PAREN_CITATION_LIST_RE,
    _PAREN_REF_CITATION_RE,
    _REF_MARKER_RE,
    _UNCITABLE_ANNOTATIONS,
    _VARIANT_ANNOTATION_RE,
    _VARIANT_CITATION_RE,
    _VARIANT_DAGGER_CITATION_RE,
    _VARIANT_RANGE_CITATION_RE,
    _VARIANT_REF_CITATION_RE,
    _canonical_annotation,
    _expand_ref_list,
    _normalize_verify_markers,
    _range_markers,
)
# K-1 (2026-08-03) — the V-B ABSENCE-SLICE subsystem lives in a sibling module
# (the seam the module-size gate named by name). Imported ONE WAY and RE-EXPORTED
# here, so every existing reference — ``verify._ABSENCE_MARKERS``,
# ``verify.SliceRow``, ``verify.load_absence_slice_rows`` — resolves exactly as
# before. The ORCHESTRATION (``_fold_absence_slice`` / ``_absence_slice_stage2``)
# stays below: it manipulates the report + ledger types this module owns.
from .absence_slice import (  # noqa: F401 — re-exported verify surface
    _ABSENCE_MARKERS,
    _ABSENCE_SCOPE_QUALIFIERS,
    _ABSENCE_SCREEN_STOPWORDS,
    _ABSENCE_SLICE_BODY_CHARS,
    _ABSENCE_SLICE_CANDIDATE_CAP,
    _ABSENCE_SLICE_CONTRADICTED,
    _ABSENCE_SLICE_JUDGE_SYSTEM,
    _ABSENCE_SLICE_MIN_TERMS,
    _ABSENCE_SLICE_THIN_ROWS,
    _ABSENCE_SLICE_TITLE_CAP,
    _ABSENCE_SLICE_UBIQUITY,
    _CAMEO_TITLE_RE,
    _COLLECTION_SCOPE_MARKERS,
    _COUNTRY_TOKENS,
    _IRREGULAR_DEMONYMS,
    _MACHINE_STRUCTURED_PROVENANCE_KINDS,
    _MACHINE_STRUCTURED_SOURCE_IDS,
    _SCALE_QUALIFIERS,
    _SLICE_DESK_COUNTRIES,
    _SLICE_GEO_COUNTRIES,
    _TARGET_SLUG_TO_ABBREV,
    _TARGET_SLUG_TO_COUNTRY,
    SliceRow,
    _absence_carve_outs,
    _absence_content_terms,
    _absence_route_exclusion,
    _absence_slice_candidates,
    _country_desk_slug,
    _first_absence_marker_pos,
    _is_absence_claim,
    _is_machine_structured_row,
    _mentions_abbrev,
    _mentions_country,
    _mentions_own_country,
    _names_country,
    _row_field,
    _row_in_claim_scope,
    _scale_undershoots_claim,
    _slice_scope_countries,
    absence_scope_qualifier,
    denied_enumeration,
    load_absence_slice_rows,
    load_absence_slice_titles,
    quote_misses_the_denied_scope,
    row_restates_the_negative,
)
# K-1 (2026-08-05) — the QUOTE RULES are the judge subsystem's fourth brick, and
# the extraction seam the module-size gate named by name ("prompt registry +
# ``_run_judge`` + the quote/severity rules"). Every deterministic test for
# whether a contradiction EARNED the hard class — V-D resolves, W2 refutes, V-G1
# signals-only, V-G3 carve-outs — lives there now. Imported ONE WAY and
# RE-EXPORTED, so ``verify._quote_refutes`` / ``verify._VERDICT_*`` resolve
# exactly as before; the severity DECISION stays here, beside the fail-class
# table it has to agree with.
from .judge_quote_rules import (  # noqa: F401 — re-exported verify surface
    _CARVE_OUT_QUOTE_MIN_SHARED_TERMS,
    _CLAIM_CITES_PRIOR_READ_RE,
    _JUDGE_CONTRADICTED_MACHINE_ROW,
    _JUDGE_CONTRADICTED_OFF_SCOPE,
    _JUDGE_CONTRADICTED_ROUTE_EXCLUDED,
    _JUDGE_CONTRADICTED_UNQUOTED,
    _JUDGE_CONTRADICTED_UNREFUTED,
    _JUDGE_PRIOR_READ_CONFLICT,
    _JUDGE_QUALIFIER_RULE,
    _JUDGE_QUOTE_MIN_CHARS,
    _JUDGE_QUOTE_RULE,
    _PRIOR_READ_MARKER,
    _JUDGE_QUOTE_CONFIRMS,
    _VERDICT_CONTRADICTED_MACHINE_ROW,
    _VERDICT_CONTRADICTED_OFF_SCOPE,
    _VERDICT_CONTRADICTED_UNQUOTED,
    _VERDICT_CONTRADICTED_UNREFUTED,
    _VERDICT_PRIOR_READ_CONFLICT,
    _VERDICT_QUOTE_CONFIRMS,
    _VERDICT_ROUTE_EXCLUDED,
    _judge_claim_block,
    _normalize_quote_text,
    _numeral_fingerprint,
    _quote_hits_a_carve_out,
    _quote_is_refutable_evidence,
    _quote_refutes,
    _quote_resolves,
    _quote_restates_claim,
    claim_is_routed_out,
    quote_confirms_the_claim,
)
# V-G7 (2026-08-03) — the STRUCTURAL-CLAIMS verify profile is the SECOND,
# deterministic critique path (see the sibling module's header for why it is a
# different KIND rather than a gap in this one). Imported ONE WAY and RE-EXPORTED
# so ``verify.verify_structural_claims`` and friends resolve exactly as before.
from .structural_claims import (  # noqa: F401 — re-exported verify surface
    STRUCTURAL_CLAIMS_DATA_KEY,
    STRUCTURAL_DERIVED_FROM_SENTINEL,
    STRUCTURAL_MISCOUNT,
    STRUCTURAL_PIPELINE_VERSION,
    STRUCTURAL_SUPPORTED,
    STRUCTURAL_UNVERIFIABLE,
    StructuralClaimVerdict,
    StructuralVerifyReport,
    build_structural_critique_payload,
    structural_verify_gate_enabled,
    verify_structural_claims,
)

logger = logging.getLogger(__name__)

# Generic hashable component key (composition sub-claim ordinals — ``int`` — or
# any other hashable id the union-find is asked to cluster).
_H = TypeVar("_H", bound=Hashable)


_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _safe_table_name(name: str) -> bool:
    return bool(_TABLE_RE.match(name))


# ---------------------------------------------------------------------------
# verify_provenance_complete
# ---------------------------------------------------------------------------


@dataclass
class ProvenanceReport:
    row_id: UUID
    table: str
    ok: bool
    missing: list[str] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def issues(self) -> list[str]:
        return self.missing + self.malformed


# Required per row kind. A row is either source-kind (analyst_id NULL,
# run_id NULL) or analyst-kind (analyst_id NOT NULL, run_id NOT NULL).
# Both kinds require: schema_uri, produced_at, derived_from (may be empty),
# target_id+target_version (sources always carry these; analyst rows may
# carry NULL if the output is genuinely cross-target / target-less).
_ALWAYS_REQUIRED = ("schema_uri", "produced_at")


async def verify_provenance_complete(
    conn: asyncpg.Connection,
    table: str,
    row_id: UUID,
) -> ProvenanceReport:
    """Fetch the row and check its universal-provenance fields."""
    if not _safe_table_name(table):
        raise ValueError(f"unsafe table name: {table!r}")

    row = await conn.fetchrow(
        f"""
        SELECT id, target_id, target_version, analyst_id, analyst_version,
               produced_at, derived_from, schema_uri, run_id
        FROM {table}
        WHERE id = $1
        """,
        row_id,
    )
    if row is None:
        return ProvenanceReport(
            row_id=row_id,
            table=table,
            ok=False,
            missing=["row_not_found"],
        )

    raw = dict(row)
    missing: list[str] = []
    malformed: list[str] = []

    for col in _ALWAYS_REQUIRED:
        if raw.get(col) in (None, ""):
            missing.append(col)

    if raw.get("derived_from") is None:
        missing.append("derived_from")

    schema_uri = raw.get("schema_uri")
    if schema_uri and not is_valid_schema_uri(schema_uri):
        malformed.append(f"schema_uri:{schema_uri!r}")

    # Distinguish row kind. Analyst rows MUST carry analyst_version + run_id.
    analyst_id = raw.get("analyst_id")
    if analyst_id is not None:
        if not raw.get("analyst_version"):
            missing.append("analyst_version")
        if not raw.get("run_id"):
            missing.append("run_id")
    else:
        # Source-kind row: target_id + target_version must be present
        # (legacy back-tagged rows carry sentinel per DM-3, still non-null).
        if not raw.get("target_id"):
            missing.append("target_id")
        if not raw.get("target_version"):
            missing.append("target_version")

    produced_at = raw.get("produced_at")
    if produced_at is not None and not isinstance(produced_at, datetime):
        malformed.append("produced_at:not_datetime")

    derived_from = raw.get("derived_from")
    if derived_from is not None:
        if not isinstance(derived_from, (list, tuple)):
            malformed.append("derived_from:not_array")
        else:
            for i, item in enumerate(derived_from):
                if not isinstance(item, UUID):
                    malformed.append(f"derived_from[{i}]:not_uuid")
                    break

    return ProvenanceReport(
        row_id=row_id,
        table=table,
        ok=not (missing or malformed),
        missing=missing,
        malformed=malformed,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# validate_lineage
# ---------------------------------------------------------------------------


@dataclass
class LineageNode:
    row_id: UUID
    depth: int
    target_id: str | None
    analyst_id: str | None
    derived_from: list[UUID]


@dataclass
class LineageReport:
    root_id: UUID
    table: str
    nodes: list[LineageNode] = field(default_factory=list)
    cycles: list[list[UUID]] = field(default_factory=list)
    dangling: list[UUID] = field(default_factory=list)
    depth_exhausted: bool = False
    max_depth: int = 20

    @property
    def ok(self) -> bool:
        return not (self.cycles or self.dangling or self.depth_exhausted)


async def validate_lineage(
    conn: asyncpg.Connection,
    table: str,
    row_id: UUID,
    *,
    max_depth: int = 20,
) -> LineageReport:
    """Walk ``derived_from`` from row_id; detect cycles, missing ancestors.

    Uses an iterative BFS so cycle detection is structural rather than
    SQL-recursion-limit-based. Each row is fetched once; missing ancestors
    (derived_from references a non-existent id) become ``dangling``.
    """
    if not _safe_table_name(table):
        raise ValueError(f"unsafe table name: {table!r}")

    report = LineageReport(root_id=row_id, table=table, max_depth=max_depth)
    visited: set[UUID] = set()
    on_path: dict[UUID, list[UUID]] = {}     # row → path from root
    queue: list[tuple[UUID, int, list[UUID]]] = [(row_id, 0, [row_id])]

    while queue:
        current, depth, path = queue.pop(0)

        if depth > max_depth:
            report.depth_exhausted = True
            continue

        if current in visited:
            continue
        visited.add(current)

        row = await conn.fetchrow(
            f"""
            SELECT id, target_id, analyst_id, derived_from
            FROM {table}
            WHERE id = $1
            """,
            current,
        )
        if row is None:
            report.dangling.append(current)
            continue

        derived_from = list(row["derived_from"] or [])
        report.nodes.append(
            LineageNode(
                row_id=current,
                depth=depth,
                target_id=row["target_id"],
                analyst_id=row["analyst_id"],
                derived_from=derived_from,
            )
        )

        for ancestor in derived_from:
            if ancestor in path:
                # Cycle — record the offending path slice.
                cycle = path[path.index(ancestor):] + [ancestor]
                report.cycles.append(cycle)
                continue
            queue.append((ancestor, depth + 1, path + [ancestor]))

    return report


# ---------------------------------------------------------------------------
# P0-T2 — faithfulness verify (deterministic floor + optional LLM judge)
# ---------------------------------------------------------------------------
#
# The system's MANDATORY verify pass.  After an inline_target finding is
# emitted with its prose ``[N]`` markers resolved into ``data['citations']``
# (P0-T1), this pass scores how faithful the synthesis is to its cited evidence
# and persists the score as a ``critique`` the existing gate consumes.
#
# DESIGN (faithfulness_score):
#   We segment the finding body into CLAIMS (sentence-ish spans) and keep only
#   the FACT-ASSERTING ones (a heuristic that skips headings, the "indicators to
#   watch" forward-looking section, and bullet scaffolding).  Each checkable
#   claim is SUPPORTED iff it carries at least one ``[N]`` marker that resolves
#   to a real signal_id present in ``citations`` (the P0-T1 bridge).  A claim
#   with NO marker, or whose every marker is unresolved/absent from the bridge,
#   is an UNSUPPORTED span.
#
#     faithfulness_score = supported_claims / checkable_claims
#
#   When there are NO checkable fact-asserting claims (e.g. a body that is all
#   forward-looking watch-items, or an unstructured fallback finding) the score
#   is 1.0 — we do not punish a finding for asserting nothing checkable, and we
#   record ``checkable_claims=0`` so the operator sees the pass was vacuous.

# Hedge-laundering tolerance: a composed clause is flagged only when its finding
# confidence exceeds its cited sub-claim's ceiling by MORE than this (float noise
# guard; 0.9-over-0.5 is far past it, 0.5-over-0.5 is not).
_HEDGE_EPSILON: float = 1e-6

# Sentence-ish split: break on sentence terminators OR newlines so each bullet /
# line is its own claim span.  Deliberately simple + deterministic (no NLP dep).
# Abbreviation-aware (#2): do NOT split after a single-letter abbreviation period
# — the ".X." signature of "U.S." / "U.K." / "U.N." — so a citation-bearing
# sentence is not fractured into an uncited fragment (the shake-down's jp/kr FP).
# Everything else splits on a sentence terminator + whitespace, or a newline.
_SENTENCE_SPLIT_RE = re.compile(r"(?<!\.[A-Z]\.)(?<=[.!?])\s+|\n+")

# C1 (2026-07-03) — a whole-line BOLD run (``**Indicators to watch**`` or
# ``- **Indicators to watch:**``): a line that is ONLY a bold run + optional
# leading bullets + optional trailing colon. The inner text is CAPTURED so the
# gate-side heading test (:func:`_is_bold_heading`) can tell a short section LABEL
# from a bold factual SENTENCE. A ``**Severity:** High`` scaffold line (content
# AFTER the bold close) deliberately does NOT match — it stays a label:value
# scaffold, not a section heading. Used so _segment_claims skips the forward-
# looking watch section under the bold heading style, not just ``#``.
_BOLD_HEADING_RE = re.compile(r"^\s*(?:[-*>]\s+)*\*\*([^*\n]+)\*\*\s*:?\s*$")

# P7 r2 — an independent FINITE verb (a copula / auxiliary, or a clearly-verbal
# past tense) signals a present-fact MAIN clause. Two uses: (1) keep a bold FACTUAL
# sentence (``**Tehran resumed enrichment**``) OUT of the heading exemption, and
# (2) anchor :func:`_is_forward_looking` so a ``present-fact, which would …`` clause
# is NOT mistaken for a pure prediction. Biased to unambiguous verbs (few noun
# homographs) — the PRIMARY forward-looking anchor is the comma; this is the
# secondary no-comma guard.
_PRESENT_FACT_VERB_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|has|have|had|"
    r"remains?|appears?|continues?|holds?|maintains?|stands?|"
    r"resumed|seized|deployed|launched|killed|announced|began|fired|"
    r"mobili[sz]ed|halted|suspended|restored|captured|invaded|shelled|"
    r"bombed|declared|imposed|signed|breached|assassinated|detained|"
    r"arrested|ousted|toppled|erupted|escalated|"
    # P7 FU1 — common present-tense (3rd-person -s) EVENT verbs. A present fact
    # with a conditional tail but NO comma and a verb OUTSIDE the past-tense list
    # above ("Beijing conducts drills that would confirm intent", "Iran enriches
    # uranium which would confirm breakout") was wrongly read as pure prediction
    # and hidden from BOTH floor and judge (H1 residual). ONLY the unambiguous -s
    # form is listed (never the bare stem), so a genuine modal prediction ("Iran
    # WOULD enrich uranium if talks fail") still reads forward-looking — the -s is
    # the disambiguator. Noun-homograph -s forms a prediction legitimately uses as
    # a subject (strikes / fires / shells / tests / sanctions / launches / signs /
    # reports / hits / halts / resumes / captures / annexes) are DELIBERATELY
    # excluded so "air strikes would confirm …" stays a forward-looking prediction.
    r"conducts|enriches|deploys|seizes|imposes|expands|announces|"
    r"suspends|mobili[sz]es|expels|ratifies|withdraws|invades|"
    r"unveils|escalates|besieges|bombards|occupies|"
    r"detains|ousts)\b"
)


def _is_bold_heading(line: str) -> bool:
    """A whole-line bold run that is HEADING-SHAPED — a short section LABEL
    (``**Key points**``, ``**Indicators to watch**``), NOT a bold factual SENTENCE
    (``**Tehran resumed enrichment**``).

    A genuine heading is judge-exempt AND floor-exempt (pure structure); a bold
    factual sentence is exempt from NEITHER — the judge must grade it and the floor
    must count it (H1: an exemption must not hide a present fact from the judge).
    """
    m = _BOLD_HEADING_RE.match(line)
    if not m:
        return False
    content = m.group(1).strip()
    if not content:
        return False
    # A heading is short + titley: no sentence-terminal punctuation and no
    # independent finite verb (which would make it a factual assertion).
    if len(content) > 48 or len(content.split()) > 7:
        return False
    if content.endswith((".", "!", "?")):
        return False
    if _PRESENT_FACT_VERB_RE.search(content.lower()):
        return False
    return True


def _is_backtick_heading(line: str) -> bool:
    """A whole-line CODE-SPAN run that is HEADING-SHAPED (W4).

    The third section-label style the assessors emit (`` `Indicators to watch` ``)
    alongside ``#`` and ``**bold**``. Same shape test as :func:`_is_bold_heading`
    — short, titley, no terminal punctuation, no independent finite verb — so a
    backticked identifier inside a factual sentence is never mistaken for one.
    """
    m = _BACKTICK_HEADING_RE.match(line)
    if not m:
        return False
    content = m.group(1).strip()
    if not content or len(content) > 48 or len(content.split()) > 7:
        return False
    if content.endswith((".", "!", "?")):
        return False
    return not _PRESENT_FACT_VERB_RE.search(content.lower())


# V-H2 (2026-08-04) — the FOURTH section-label style, and the one the assessors
# use most: an UNDECORATED line ending in a colon ("Key points:", "Assessment:",
# "Indicators to watch:"). See :func:`_is_plain_heading`.
_PLAIN_HEADING_RE = re.compile(r"^\s*([A-Za-z][^\n:]{0,63}):\s*$")

#: A leading list bullet. A bullet is CONTENT, never a section label.
_PROSE_BULLET_LEAD_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")


def _is_plain_heading(line: str) -> bool:
    """A whole-line UNDECORATED section label — ``Indicators to watch:`` (V-H2).

    The producer side has always treated this form as a heading
    (``inline_target._is_watch_heading`` strips ``#``/``*``/`` ` ``/``:`` and then
    matches the label, so an undecorated line qualifies there and its bullets are
    mined as forward-looking ``IndicatorEntry`` rows). The VERIFY side required
    markdown, so the same section was graded as present-fact prose — the two
    halves of the system disagreed about what a heading is, and verify was the
    stricter one.

    The measured consequence is the 08-03 panel's ``soft_fail#4``: an energy desk
    whose whole watch list ("Escalated interdiction risk on a maritime chokepoint
    carrying … energy imports", and three siblings) reached the ledger graded on
    citation support, which a watch item can never have — you cite what happened,
    not what would.

    The TRAILING COLON is load-bearing and required. Without it a prose sentence
    opening "Watch for a second tanker strike" would be read as a section label
    and the section skip would swallow the whole rest of the finding — the
    expensive error. With it, plus the same shape guard the bold and backtick
    heading tests use (short, titley, no terminal punctuation, no independent
    finite verb), the form is unambiguous. A LIST BULLET is content, never a
    heading — the same rule the producer applies.
    """
    if _PROSE_BULLET_LEAD_RE.match(line):
        return False
    m = _PLAIN_HEADING_RE.match(line)
    if not m:
        return False
    content = m.group(1).strip()
    if not content or len(content) > 48 or len(content.split()) > 7:
        return False
    if content.endswith((".", "!", "?")):
        return False
    return not _PRESENT_FACT_VERB_RE.search(content.lower())


# C1 (2026-07-03) — a span that is ONLY citation markers (``[21][26]`` or
# ``[[ref:3]]``), the orphan the sentence splitter severs off a claim whose
# citation trails the period. Re-attached to the preceding span in _segment_claims.
_CITATION_ONLY_RE = re.compile(
    r"^\s*(?:\[\d+\]|\[\[ref:\d+\]\])(?:\s*(?:\[\d+\]|\[\[ref:\d+\]\]))*\s*$"
)

# P7-F1(2) — a citation marker that sits AFTER a sentence terminator
# (``…fighters. [[ref:1]]`` / ``…zones.\n[21]``) is severed from the claim it
# supports by _SENTENCE_SPLIT_RE, orphaning the clause as ``no_citation`` even
# though it IS cited. This pulls a run of trailing markers back INSIDE the
# sentence (before the terminator) so the split keeps the marker on the claim.
# It also handles the marker-leads-the-next-line style (the marker glues to the
# PRECEDING clause, which is the one it grounds). Applied at the top of
# _segment_claims, after marker-drift normalization and before the split.
_TRAILING_MARKER_PULL_RE = re.compile(
    r"([.!?])(\s+)((?:\[\d+\]|\[\[ref:\d+\]\])(?:\s*(?:\[\d+\]|\[\[ref:\d+\]\]))*)"
)

# W4 (2026-08-02) — an INLINE markdown heading, welded to the preceding sentence
# with no newline ("…coercive measures on Russia.## Key points"). 2+ hashes and a
# following space are required, so a ``#hashtag`` in prose never matches.
_INLINE_HEADING_RE = re.compile(r"(?<=[^\n])(#{2,6}\s+\S)")

# W4 — a whole-line BACKTICK heading (`Indicators to watch`), the third section-
# label style the assessors emit alongside ``#`` and ``**bold**``.
_BACKTICK_HEADING_RE = re.compile(r"^\s*(?:[-*>]\s+)*`([^`\n]{1,64})`\s*:?\s*$")

# Phase-V D1 (2026-08-04) — the AS-OF LINE. Every unit finding and every
# composition now opens with one, in the exact form the shared as-of clause
# mandates: a whole line, wrapped in single asterisks, beginning "As of", giving
# the run's own coordinates — ``*As of 3 August 2026; slice covers the trailing
# 72h to that date; 26 signals.*``
#
# It is STRUCTURE, not a claim, and must be exempt at BOTH gates:
#
#   * The FLOOR requires an ``[N]``. There is no signal that says what the run
#     date is — the line is copied from the rendered slice header — so it can
#     never carry one. Left graded, one uncited span would be added to EVERY
#     finding in the tower the day this ships, dragging the citation-presence
#     score down fleet-wide for prompt compliance.
#   * The JUDGE grades against the CITED EVIDENCE, which is the signal set. Run
#     metadata is not in it, so the judge cannot check this line and could only
#     mark it unfaithful. That is a false negative manufactured by the exemption
#     being absent, not a fabrication caught by it being present.
#
# The H1 rule ("an exemption must never hide a present fact from the judge")
# holds because this exemption is NARROW by four independent conditions, the
# last two mirroring the discipline ``_is_bold_heading`` already applies to tell
# a section LABEL from a bold factual SENTENCE:
#
#   1. the span is the WHOLE line, wrapped in single asterisks — the shape the
#      prompt mandates, and one a factual sentence does not take;
#   2. it opens with "as of";
#   3. it carries NO citation marker. A claim cites; the as-of line cannot,
#      because nothing in the evidence states the run date. A marker is
#      therefore positive evidence that the span is a claim, so it is graded.
#   4. it carries no independent present-fact verb. ``*As of 2 August, Iran
#      resumed enrichment at Fordow.*`` is a real assertion that happens to be
#      italicised, and it stays floored and judged.
#   5. it names at least one RUN COORDINATE the as-of form mandates — the signal
#      count, the slice/window, or the "composed from N ... reads" phrase. This
#      is what makes the test match the prompt's FORM rather than merely its
#      opening words, so an italicised sentence that starts "As of" but says
#      something about the world ("*As of 2 August, Brazil recalled its
#      ambassador.*") is a claim and is graded.
#
# A real dated claim — "As of 2 August, transits fell to 11/day [74]." — fails
# (1), (3), (4) and (5) and is untouched.
_AS_OF_LINE_RE = re.compile(r"^\s*\*\s*as of\b[^*\n]{0,240}\*\s*$", re.IGNORECASE)

#: The run coordinates the as-of form mandates — at least one must be present.
_AS_OF_COORDINATE_RE = re.compile(
    r"\b(signals?|slice|window|trailing|composed from|reads?\b|blocks?\b)",
    re.IGNORECASE,
)


def _is_as_of_line(claim: str) -> bool:
    """True for the Phase-V structural as-of line (see :data:`_AS_OF_LINE_RE`)."""
    s = claim.strip()
    if not _AS_OF_LINE_RE.match(s):
        return False
    if _CLAIM_MARKER_RE.search(s) or _REF_MARKER_RE.search(s):
        return False
    if not _AS_OF_COORDINATE_RE.search(s):
        return False
    return not _PRESENT_FACT_VERB_RE.search(s.lower())

# W4 — a NON-TERMINAL abbreviation whose period the sentence splitter mistakes
# for a sentence end, severing a claim into a dangling fragment plus a subjectless
# remainder ("… = 101 vs." + "84 last window"). Anchored at end-of-span, so it
# only fires on the fragment the split actually produced.
#
# DELIBERATELY SHORT. Only abbreviations that essentially NEVER end an English
# sentence qualify, because a false positive MERGES two real claims into one
# ledger row. "etc." ("…drones, radars, etc.") , "Jr."/"Sr." ("…John Smith Jr."),
# "no."/"nos.", "govt."/"dept."/"univ." and "et al." all routinely DO end one and
# are excluded for that reason.
_NON_TERMINAL_ABBREV_RE = re.compile(
    r"(?<![\w-])(?:vs|cf|e\.g|i\.e|approx|fig|figs|mr|mrs|ms|dr|prof)\.$",
    re.IGNORECASE,
)

# Section headings whose CONTENT is not a checkable factual assertion: the
# "indicators to watch" block is explicitly forward-looking ("would confirm /
# break this assessment"), so its lines are NOT scored as unsupported facts.
_NON_FACTUAL_HEADINGS = (
    "indicators to watch",
    "indicators",
    "watch",
    # #116(b): broadened — the forward-looking "watch" block surfaces under many
    # heading phrasings the assessors emit. All are forward-looking by
    # construction (developments that WOULD confirm/break the read), so none is a
    # present-fact assertion for EITHER the floor or the judge.
    "what to watch",
    "watch items",
    "watch for",
    "watchlist",
    "watch list",
    "signposts",
    "leading indicators",
    "key indicators",
    "indicators & triggers",
    "indicators and triggers",
    "triggers to watch",
    "developments to watch",
    "things to watch",
    "indicators to monitor",
    "what would confirm",
    "what would break",
    # Phase-V D4 (2026-08-04): the ONE unit body shape names this section
    # ``## What would change this read`` on every desk, so it goes from a
    # phrasing some models happened to pick to a section every unit finding
    # carries. It is forward-looking by construction — the prompt defines it as
    # "the ONE observation that would most move your verdict" plus the class of
    # reporting that would carry it and whether this desk collects it — so its
    # contents are a non-occurrence and a statement about our own collection,
    # neither of which can cite a signal. Left out of this tuple it would be
    # segmented as present-fact claims with no [N], and the citation-presence
    # floor would mark every unit finding down for following its own prompt.
    # ``what changed`` deliberately does NOT match (startswith, and the strings
    # diverge at "would"): ``## What changed`` is the CITED section and must
    # stay graded.
    "what would change",
)

# CALIBRATION (#1, 2026-07-01). The citation-presence floor was crushing HONEST
# LOW-RISK reads: a unit that correctly reports "nothing concerning found, low
# risk" is mostly summary + absence + synthesis, none of which can carry an [N]
# signal citation — yet each was flagged no_citation, dragging faithfulness down
# and propagating up to the composition. These classes are NOT citation defects.
# Applied at claim segmentation, so they correct BOTH the deterministic floor AND
# the LLM judge (both consume _segment_claims/_is_fact_asserting).

# Synthesis OPENERS — a clause that opens with one of these
# is the analyst's derived conclusion over the cited evidence, carrying no [N] by
# design (the unit prompts prefix inferred judgements with "Assessed:").
_SYNTHESIS_PREFIXES = (
    "bluf",
    "assessed:",
    "assessment:",
    "judgment:",
    "judgement:",
    # P7-F1(3): the composition assessors emit bare ``JUDGMENT`` / ``ASSUMPTION``
    # synthesis lines (no colon) as their derived-read scaffolding — a conclusion
    # over the cited sub-claims, not a new first-order citable fact. FLOOR-ONLY
    # (via _is_fact_asserting); the judge still grades them (_is_judgeable_claim).
    "judgment",
    "judgement",
    "assumption:",
    "assumption",
    "consequently",
    "collectively",
    "on balance",
    "in sum",
    "in summary",
    "in short",
    "overall",
    "taken together",
    "the most plausible",
    "the dominant",
    "net,",
    "net:",
)

# Q-1 (2026-08-05): the labeled-scaffold rule and the score-state policy moved to
# judge_assessability.py (judge-subsystem brick 2) — together, because the measured
# defect ran through both halves. ``_LABELED_SCAFFOLD_RE`` is re-exported for the
# callers that still name the SHAPE; every EXEMPTION decision now goes through
# ``is_labeled_scaffold``, which reads what follows the label instead of stopping
# at it. See that module's header for the 11-critique measurement.
from .judge_assessability import (  # noqa: F401 — re-exported verify surface
    DENOMINATOR_COVERAGE_STATEMENT,
    DENOMINATOR_TRIGGERED_INDICATOR,
    PROVISIONAL_SCORE_CEILING,
    SCORE_STATE_SCORED,
    SCORE_STATE_UNASSESSABLE,
    UNASSESSABLE_GATE_SCORE,
    _LABELED_SCAFFOLD_RE,
    denominator_caveat_counters,
    gate_score,
    is_assessment_scaffold,
    is_coverage_statement,
    is_json_syntax_claim,
    is_labeled_scaffold,
    is_provisional,
    resolve_score_state,
)


# P7-F1(5) — FORWARD-LOOKING watch/indicator bullet markers. A bullet phrased as a
# future conditional ("Official announcements of fuel rationing WOULD CONFIRM a
# supply crisis", "a border incident WOULD SIGNAL escalation") is a signpost the
# read is watching FOR — it describes a non-occurrence that, by construction,
# cannot cite an existing signal (you cite what happened, not what would). The
# floor already drops the 'Indicators to watch' SECTION wholesale; this catches a
# forward-looking bullet that leaked OUTSIDE a recognized heading (the high-
# severity 10535403 crush: the judge graded its 'Official announcements of …'
# watch bullets as uncited present facts). Narrow by design — it matches the
# future-conditional 'would confirm/break/signal' idiom, NOT a present-tense
# absence read ('No evidence of X'), so the judge STILL grades present absence
# claims (H1: the judge, not the floor, catches a fabricated absence).
_FORWARD_LOOKING_MARKERS = (
    "would confirm",
    "would break",
    "would signal",
    "would indicate",
    "would mark",
    "would suggest escalation",
    "would point to",
    "to watch for",
    "watch for",
)


def _is_forward_looking(low: str) -> bool:
    """True when a clause is a PURE future-conditional watch/indicator bullet.

    ANCHORED (P7 r2, H1): the conditional idiom must GOVERN THE WHOLE clause — the
    clause OPENS with the idiom, or is a bare ``X would/could Y`` whose subject (the
    text before the modal) carries NO independent present-fact assertion. A clause
    that leads with a present-fact main clause and only trails a conditional
    (``Tehran resumed enrichment, which would confirm a breakout``) is NOT
    forward-looking — the floor must count and the judge must grade the present
    fact. The round-1 unanchored substring match wrongly swallowed such facts.
    """
    marker_pos = min(
        (low.find(m) for m in _FORWARD_LOOKING_MARKERS if m in low),
        default=-1,
    )
    if marker_pos < 0:
        return False
    prefix = low[:marker_pos].strip()
    # Opens with the conditional idiom -> whole-clause prediction.
    if not prefix:
        return True
    # A comma-joined lead clause -> present-fact main clause + conditional tail.
    if "," in prefix:
        return False
    # An independent finite verb in the subject -> a present-fact main clause.
    if _PRESENT_FACT_VERB_RE.search(prefix):
        return False
    return True


# Advisory span reasons (#3): NOT unsupported claims — they are structural
# observations (a shared-lineage double-count note; a hedge-laundering cap
# trigger). Excluded from the unsupported tally so supported + unsupported
# reconciles against checkable instead of over-counting.
_ADVISORY_REASONS = frozenset({"double_counted", "hedge_laundering"})

# ---------------------------------------------------------------------------
# C-TIER (2026-07) — two-tier composition evidence: the PERIPHERY hedge rule.
#
# The composition synth (meta_findings_synthesizer, flag
# LEGBA_COMPOSITION_TIERED_EVIDENCE) renders below-floor / unverified
# sub-claims in an explicit PERIPHERY section and stamps ``tier='periphery'``
# on each citation that resolves into it. The verify contract for that tier:
# a fact-asserting clause whose resolved citations are ALL periphery-tier is
# SUPPORTED only when the clause itself is hedged/attributed ("weakly-supported
# reporting suggests ..."); asserted bald, it overclaims verification status
# its evidence does not carry — the ``unhedged_periphery_citation`` reason
# (soft, the overclaim/hedge-laundering family), COUNTED on the deterministic
# floor (the composition's always-on verify layer). A clause with >=1 BASIS
# citation is supported by that basis leg (periphery is corroborating color
# there, not the load-bearing evidence). Citations with no ``tier`` key —
# every pre-C-TIER composition — leave this rule inert, byte-for-byte.
#
# Scope note: the deterministic FLOOR rule above is always-on for tiered
# compositions. The optional LLM judge is ALSO tier-aware (the former SEAMS
# §45, resolved 2026-07): when the citation list carries periphery-tier stamps,
# the composition judge prompt gains the ADDITIVE EVIDENCE-TIERS rubric block
# (:func:`_judge_periphery_rubric`) naming the periphery ordinals and requiring
# hedged/attributed use — treating a periphery item as established fact is a
# failure. Untiered citation lists (every pre-C-TIER composition) leave the
# judge prompt byte-identical; the C1 no-co-veto decision is unchanged (the
# judge stays authoritative over the prose it graded, and the floor rule still
# counts an unhedged periphery claim regardless).
# ---------------------------------------------------------------------------

_PERIPHERY_TIER = "periphery"
_UNHEDGED_PERIPHERY = "unhedged_periphery_citation"

# Hedge/attribution lexicon for the periphery rule — deliberately GENEROUS
# (substring, lowercased): the periphery prompt section dictates the canonical
# phrasing ("weakly-supported reporting suggests ..."), so the happy path is
# guaranteed to match; the breadth here only makes false FLAGS rarer (a hedged
# variant slipping through unflagged is the cheap error; flagging a genuinely
# hedged clause is the expensive one). Trailing spaces on the modal forms keep
# "may " from matching "mayor" etc.
_HEDGE_ATTRIBUTION_MARKERS: tuple[str, ...] = (
    "weakly-supported",
    "weakly supported",
    "unverified",
    "unconfirmed",
    "uncorroborated",
    "not corroborated",
    "below the verification floor",
    "reportedly",
    "reporting suggests",
    "reports suggest",
    "suggests",
    "suggesting",
    "suggest that",
    "may ",
    "might ",
    "could ",
    "appears to",
    "appear to",
    "apparently",
    "possibly",
    "potentially",
    "alleged",
    "purported",
    "rumored",
    "rumoured",
    "claims that",
    "claimed",
    "according to",
    "single-source",
    "single source",
    "if confirmed",
    "tentative",
)


def _is_hedged_attributed(claim: str) -> bool:
    """True iff the clause carries hedge/attribution language (the periphery
    remedy). Lowercased substring match over the lexicon above — deterministic,
    lenient by design (see the lexicon note)."""
    low = claim.lower()
    return any(m in low for m in _HEDGE_ATTRIBUTION_MARKERS)


def _periphery_ordinals(citations: Any) -> set[int]:
    """The set of cited sub-claim ORDINALS the composition stamped
    ``tier='periphery'``. Empty for every pre-C-TIER citation list (no ``tier``
    key) — the periphery rule is then inert.

    Shares :func:`_build_ordinal_map` (defined below — resolved at call time, as
    this function already did for :func:`_citation_ordinal`); the tier filter
    rides the projector as a SKIP.
    """
    return set(
        _build_ordinal_map(
            citations,
            lambda entry, n: n if entry.get("tier") == _PERIPHERY_TIER else _ORDINAL_SKIP,
        )
    )


def _judge_periphery_rubric(citations: Any) -> str:
    """The ADDITIVE tier-aware judge rubric block (former SEAMS §45), or ``""``.

    Rendered into the COMPOSITION judge prompt lead ONLY when the citation list
    carries periphery-tier stamps (a tiered C-TIER composition); every untiered
    citation list — every pre-C-TIER composition and every unit — yields the
    empty string, leaving the judge prompt byte-identical. Mirrors the
    deterministic floor's periphery hedge contract so the two graders share ONE
    tier semantics: periphery = below-floor / unverified, may only inform
    hedged, attributed context; treated as established fact ⇒ a failure.
    """
    ords = _periphery_ordinals(citations)
    if not ords:
        return ""
    ord_list = ", ".join(str(n) for n in sorted(ords))
    return (
        "EVIDENCE TIERS: sub-claim(s) "
        f"[{ord_list}] are PERIPHERY tier — signals that scored BELOW the "
        "verification floor or were never verified. They are weak leads, NOT "
        "established findings. A claim resting ONLY on periphery sub-claims is "
        "SUPPORTED only when it is hedged AND attributed to weak reporting "
        "(e.g. 'weakly-supported reporting suggests ...'); a claim that treats "
        "a periphery sub-claim as ESTABLISHED FACT (asserted bald, with no "
        "hedge/attribution) is UNSUPPORTED even when the periphery text "
        "contains it. A claim citing at least one non-periphery (basis) "
        "sub-claim is graded normally.\n\n"
    )

# ---------------------------------------------------------------------------
# P2-4 — hard/soft verdict labels (the Primer taxonomy). LABELS ONLY: nothing
# below changes a score, a floor exemption, or pass/fail semantics — each span
# reason is additively classified so the report (and the UI hover) can say HOW
# BAD a flag is, and so a future gate can weigh hard vs soft without re-deriving
# the taxonomy from reason strings.
#
#   hard_fail — the finding misstates its evidence: an entity scramble, a claim
#     its own cited source contradicts, or a fabricated citation (a marker that
#     resolves to NO real cited evidence).
#   soft_fail — the finding outruns its evidence: an unsupported inference, a
#     hedge laundered into confidence, an overclaim / uncited assertion.
#
# THE mapping table lives HERE and only here (drift-guard test:
# tests/data_pkg/test_verify_claim_ledger.py scans this module for every emitted
# ``reason`` and asserts it is mapped) — a new span reason MUST be added to this
# table or the guard fails the suite.
# ---------------------------------------------------------------------------

FAIL_CLASS_HARD = "hard_fail"
FAIL_CLASS_SOFT = "soft_fail"

_FAIL_CLASS_BY_REASON: dict[str, str] = {
    # -- hard: entity scramble / contradicted-by-source / fabricated citation --
    # cites marker(s) that resolve to NO real cited evidence — fabricated citation
    "unresolved_citation": FAIL_CLASS_HARD,
    # the judge found the claim contradicted by its OWN cited source
    "judge_contradicted": FAIL_CLASS_HARD,
    # M13 — stale-cutoff current-officeholder reference (entity scramble)
    "stale_leader": FAIL_CLASS_HARD,
    # E-1 (2026-07-27) — officeholder claim reconciled against the CURRENT
    # facts-table officeholder row and found mismatched. Same hard entity-
    # scramble class as the M13 heuristic, but a DISTINCT reason so calibration
    # can tell the facts-backed flag from the curated-regex one — and because
    # the seed facts can THEMSELVES be stale (known: the DRC PM upstream), this
    # only ever DEMOTES/flags, never auto-corrects.
    "stale_leader_vs_facts": FAIL_CLASS_HARD,
    # M15 — per-country finding naming only OTHER countries (entity scramble)
    "cross_target_leak": FAIL_CLASS_HARD,
    # -- soft: unsupported inference / hedge laundering / overclaim -----------
    # a fact-asserting claim with no citation at all — overclaim/uncited
    "no_citation": FAIL_CLASS_SOFT,
    # the judge could not ground the claim in the cited evidence — unsupported
    # inference (distinct from contradicted: the evidence is silent, not opposed)
    "judge_unsupported": FAIL_CLASS_SOFT,
    # a composed clause asserts more confidence than its cited sub-claim carries
    "hedge_laundering": FAIL_CLASS_SOFT,
    # C-TIER — a composed clause resting ONLY on periphery-tier (below-floor /
    # unverified) sub-claims asserted WITHOUT hedged attribution — the same
    # overclaim family: the clause claims verification status its evidence
    # does not carry. COUNTED on the deterministic floor (not advisory).
    _UNHEDGED_PERIPHERY: FAIL_CLASS_SOFT,
    # two cited sub-claims share underlying lineage — evidence overclaim (advisory)
    "double_counted": FAIL_CLASS_SOFT,
    # S3-T1 — a 'triggered' structured indicator with no citation — uncited claim
    "indicator_uncited_triggered": FAIL_CLASS_SOFT,
    # W31 — an absence claim stated as a WORLD-fact with no collection-scoping
    # language ("no outages were reported" on a thin-collection desk). An
    # honesty-PHRASING defect (overclaim family), NOT fabrication — soft.
    "unscoped_absence_claim": FAIL_CLASS_SOFT,
    # V-B (2026-07-31) — a SCOPED-ABSENCE claim ("no NEW / large-scale X") that a
    # row of the analyst's OWN retained input slice reports. HARD by the same
    # earned-severity rule as V-D: the verdict must NAME the violating slice
    # title, resolved against the candidate set — an unresolvable answer decides
    # nothing and leaves today's verdict.
    "absence_slice_contradicted": FAIL_CLASS_HARD,
    # V-D (2026-07-31) — the judge called the claim CONTRADICTED but could not
    # point at the refutation: no ``quotes`` entry, or one that is not a verbatim
    # run of the evidence it was shown. The claim still FAILS; the unearned
    # hard-fail severity is what the demotion removes (readout finding #4 — half
    # the Cerebras and three quarters of the same-model hard-fails were false).
    "judge_contradicted_unquoted": FAIL_CLASS_SOFT,
    # W2 (2026-08-02) — the judge DID point at verbatim evidence, but the span
    # RESOLVES the claim's subject instead of REFUTING it: it restates the claim,
    # or it is lifted from the PRIOR READ block the claim explicitly diffs
    # against. Three unearned hard fails in the 08-02 panel had exactly that
    # shape and satisfied D1 mechanically. Soft, and DISTINCT from
    # ``judge_contradicted_unquoted`` — the defect is a bad refutation, not a
    # missing one, and calibration must be able to tell the two apart.
    "judge_contradicted_unrefuted": FAIL_CLASS_SOFT,
    # V-G1 (2026-08-03) — the judge's refuting quote is verbatim and real, but it
    # lives in an ANALYST FINDING the claim never cited: overwhelmingly this
    # desk's own superseded PRIOR READ (13 of 23 traced quotes on the 08-03
    # panel), sometimes a sibling desk's assessment. The desk did not misstate
    # its evidence — it CHANGED ITS MIND on fresh reporting, which is the
    # behaviour the platform exists to produce. Soft, and its own class: the
    # disagreement is real information a calibration read should be able to
    # count, and it is emphatically not fabrication.
    "judge_prior_read_conflict": FAIL_CLASS_SOFT,
    # V-H4 (2026-08-04) — the claim ENUMERATED what it denies ("FX-reserve
    # depletion, currency crises, SWIFT bans, or sovereign default pressures")
    # and the judge's real, verbatim, signal-backed quote names none of those
    # things in full ("business and mortgage defaults"). It evidences something
    # the claim never denied — often, as in the 08-03 panel's `hard_fail#8`, the
    # very thing the claim's OTHER clause asserts. Soft, and distinct from
    # ``judge_contradicted_unrefuted`` so the two demotion mechanisms stay
    # separable in the counters (V-G8's fidelity rule).
    "judge_contradicted_off_scope": FAIL_CLASS_SOFT,
    # V-I1 (2026-08-05) — the judge's "refuting" quote states the claim's OWN
    # numbers back to it once numerals, units and word-numbers are normalized on
    # both sides: "16 people were killed, and another 36 were injured" was used
    # to hard-fail "sixteen lives and thirty-six injuries". Soft, and its own
    # class because it is the only demotion that says the judge got the
    # DIRECTION wrong rather than the evidence, the scope or the provenance.
    "judge_quote_confirms_claim": FAIL_CLASS_SOFT,
    # V-I4 (2026-08-05) — the refuting quote resolves ONLY inside a GDELT/CAMEO
    # machine-coded event record ("STUDENT <-> PAPUA: protest in Jakarta"). A
    # coding is a machine's reading of an article, not the article; the V-B
    # route has excluded the class 2,109 times a day since W1(c) while the judge
    # path never had the filter. Soft: the quote is real, it just is not
    # testimony — and the article underneath H13's coding SUPPORTED the claim.
    "judge_contradicted_machine_row": FAIL_CLASS_SOFT,
    # V-I5 (2026-08-05) — the V-B continuity router had already routed this claim
    # OUT of slice checking, and the judge hard-failed it anyway. One claim, two
    # authorities. Soft: the routing decision is the platform's answer to what
    # KIND of claim this is, and the hard class is not available for it.
    "judge_contradicted_route_excluded": FAIL_CLASS_SOFT,
    # V-G5 (2026-08-03) — a claim with NO citation marker resting on a
    # HISTORICAL / STRUCTURAL BASELINE about the world ("Argentina's historical
    # propensity for coups", "the base rate for the United States"). The judge is
    # instructed to pass markerless prose as synthesis, so this class rode
    # through as SUPPORTED on both acceptance runs — the pass-side miss. Soft and
    # distinct from ``no_citation``: the defect is not a missing marker on a
    # reported fact, it is a premise whose truthmaker no cited row contains.
    "uncited_world_knowledge": FAIL_CLASS_SOFT,
    # V-C (2026-07-31) — prose MISQUOTING the platform's own metadata: it states
    # an effective_confidence / tier the cited output's captured column
    # contradicts. Newly VISIBLE (previously the judge could not check it at
    # all). Soft: an overclaim about provenance, not a fabricated world fact —
    # and the ONE hard-fail rule (V-D) is a quoted EVIDENCE span, which a column
    # lookup is not.
    "metadata_mismatch": FAIL_CLASS_SOFT,
    # R3 (2026-08-05) — the composition LED on an input materially less
    # consequential than its top one (``_build_salience_check``, gap > 0.30).
    # Soft: everything said was true, the ORDER was wrong — and order is what a
    # reader takes from a report first. Emitted by judge_input_checks.
    "buried_lead_salience": FAIL_CLASS_SOFT,
    # R2 (2026-08-05) — the input set asserted P and ¬P about one subject and the
    # composition composed them into agreement without naming the disagreement.
    # Soft by the same rule (no fabricated fact) and the most consequential soft
    # class in the table: it is how a chokepoint was simultaneously shut and open.
    "unsurfaced_input_contradiction": FAIL_CLASS_SOFT,
}


def fail_class_for_reason(reason: str) -> str:
    """The hard/soft fail class for a span reason. UNKNOWN reasons classify
    ``soft_fail`` (conservative: never escalate an unrecognized label to hard;
    the drift-guard test keeps the table total so this fallback stays unused).
    """
    return _FAIL_CLASS_BY_REASON.get(reason, FAIL_CLASS_SOFT)

# S3-T1 — a structured I&W indicator whose status is 'triggered' asserts an
# OBSERVED development and so must name the signal(s) that fired it. An uncited
# 'triggered' entry is a REAL unsupported span (NOT advisory — it counts against
# the tally + demotes faithfulness), exactly like an uncited prose fact. The
# forward-looking 'not_observed' / 'expired' statuses are exempt (nothing exists
# to cite for a non-occurrence — mirroring the prose watch-section exemption).
_INDICATOR_UNCITED_TRIGGERED = "indicator_uncited_triggered"

# Env flag (code DEFAULT OFF) gating the optional LLM judge. When unset / falsey
# the pass is the deterministic floor labelled 'judge-unavailable'.
_VERIFY_LLM_JUDGE_ENV = "LEGBA_VERIFY_LLM_JUDGE"


def _llm_judge_enabled() -> bool:
    """Whether the optional LLM judge is flag-enabled. Code default OFF."""
    raw = os.getenv(_VERIFY_LLM_JUDGE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# P2-4 — judge PROMPT PROFILE (independence posture), STAGED NOT LIVE.
#
# ``current`` (the DEFAULT, and the only live behavior) keeps the calibrated
# generic judge system prompt byte-for-byte. ``independent`` swaps in the
# adversarial-reviewer variant (_INDEPENDENT_JUDGE_SYSTEM below): the judge is
# instructed as an INDEPENDENT reviewer of ANOTHER analyst's claims — never
# "check your own work" — for the day the second judge model lands (and for the
# same-model A/B first: scripts/temp_ab_replay.py --judge-profile). The profile
# swaps ONLY the generic system prompt; the specialized rubrics (M14 null-result
# survey, V3 absence) are already tightly constrained and stay profile-invariant
# so an A/B isolates the posture variable. Selection: explicit call-site arg →
# ``LEGBA_JUDGE_PROMPT_PROFILE`` env → ``current``. Flipping the default is a
# measured, operator-gated step — nothing here changes a live prompt.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# THE JUDGE PIPELINE VERSION (2026-07-31) — the population SPLIT key.
#
# The verify gate is the product's keystone, so every structural change to it
# ships behind ONE version stamp on the critique, the MATCHER_VERSION idiom.
# Band calibration, the gold-set loop, the correctness scorer and the scorecard
# all read faithfulness history; without a split key they would POOL critiques
# graded under different pipelines and read the change as a quality movement.
#
# The 2026-07-31 train (V-F claim-splitter hygiene, V-C metadata lookup, V-D
# earned hard-fail severity, V-B slice-scoped absence, A3's counter) is expected
# to shift mean faithfulness UPWARD. That shift is a MEASUREMENT CORRECTION —
# the readout established that both judges over-fail, so the prior mean
# UNDERSTATED true faithfulness — and must never be reported as findings getting
# better. Splitting on this stamp is what makes that statement checkable rather
# than asserted.
#
# ONE bump per train. Format ``<train date>/<n>``; a later structural change to
# the verify path bumps it again, in the same commit as the change.
#
# 2026-08-02/1 — the F-A PRECISION train, off the 08-02 acceptance readout (all
# three pre-declared gates failed at 2026-07-31/1: 70% agreement vs 85%, 60%
# failure precision vs 75%, one pass-side miss vs zero). W1 makes the
# contradicted branch earn its hard fail (target-scope, composition-body,
# machine-row and carve-out filters, a tighter route, slice-size honesty); W2
# makes a hard fail auditable, correctly labelled in the ledger, and actually
# refuting; W3 splits the citationless shapes; W4 lands the four small checkers.
#
# DIRECTION OF THE EXPECTED SHIFT IS NOT ONE-WAY, and pooling would hide that.
# Hard-fail COUNT should fall sharply (20 of the 27 live contradicted verdicts
# are removed by W1's deterministic filters alone). Mean faithfulness may fall
# SLIGHTLY: W1(e) withdraws ~11% of V-B's supported overrides — claims where a
# subordinate negative was certifying a forecast or a two-read comparison it did
# not cover — and those claims go back to carrying the grader's own verdict.
# Fewer false hard fails AND fewer unearned passes is the intended shape; only
# the split key makes it legible as that rather than as a quality movement.
#
# 2026-08-03/1 — the V-G train, off the 08-03 acceptance RE-RUN (all three gates
# failed again, and agreement REGRESSED 70% -> 63%). F-A's filters worked — zero
# cross-target and zero CAMEO failures in the sample, contradicted 27 -> 15 — and
# in clearing them it exposed what they had been hiding: the judge was refuting
# findings with FINDINGS. 14 of 24 hard fails rested on a quote from an analyst
# output, 13 of them the desk's OWN superseded prior read.
#
#   V-G1  a hard fail's quote must resolve to SOURCE reporting, or to evidence
#         the claim itself cites; anything else demotes to the new soft class
#         judge_prior_read_conflict. Retires the whole anti-update class.
#   V-G2  continuity claims ("no material change since the prior read") leave the
#         V-B slice route — a diff between two assessments is not decidable from
#         a row describing the current state.
#   V-G3  the claim's carve-outs and its SCALE word reach the judge prompt, and
#         a quote landing on an exemption no longer earns the hard class.
#   V-G5  a markerless claim resting on an uncited world BASELINE stops passing
#         by default (the pass-side miss, twice running, on the same shape).
#   F-D   composition citations carry the unit judge's whole-evidence window, and
#         the synthesizer packs against the shared input-token budget.
#
# DIRECTION OF THE EXPECTED SHIFT, again not one-way. Hard-fail COUNT should fall
# again and further: V-G1 alone reaches 14 of 24, V-G2 removes 6 of the 15
# surviving absence hard fails (measured read-only on the stamped day). Mean
# faithfulness should move only slightly, and can move DOWN — V-G5 converts 19 of
# 5,338 silent passes into soft fails, and V-G2 hands 81 verified absences back
# to the judge to grade on citation support. Soft-fail count should FALL where
# F-D's wider evidence window lets a composed clause resolve against the body of
# what it cited instead of its first quarter. Three effects, opposite signs, one
# population: pooling this with 2026-08-02/1 would make every one of them
# invisible.
#
# 2026-08-04/1 — the V-H train, the RESIDUALS the 08-03 adjudication itemized and
# V-G did not reach. Smaller than its predecessors by design: V-G took the classes
# that moved volume, and what is left is four narrow defects and one honest
# refusal.
#
#   V-H1  the judge's citation view carries the OUTLET (`signals.source_id`). An
#         attribution claim — "near-identical framing across CBC, NPR and the
#         BBC" — was unverifiable BY CONSTRUCTION; the panel checked all six
#         outlets by hand and the judge still graded it unsupported.
#   V-H2  the UNDECORATED "Indicators to watch:" label is a heading. The producer
#         has always read it as one and mines its bullets as forward-looking;
#         verify required markdown, so it graded them on citation support a watch
#         item can never carry.
#   V-H3  _metadata_dominant opens a SECOND, evidence-bearing arm: the residual
#         passes when the CITED text covers it and agrees on polarity. The
#         anti-laundering arm is untouched.
#   V-H4  a hard fail whose quote names none of an ENUMERATED denial's listed
#         things demotes to the new soft class judge_contradicted_off_scope.
#   V-H5  a scoped negative is not violated by a slice row whose own leading
#         assertion is a negative about the same subject.
#
# DIRECTION OF THE EXPECTED SHIFT — mostly UP, and small, which is itself the
# reason to split. Every one of these five removes a FALSE failure and none adds
# a new failure class, so mean faithfulness should rise slightly and hard-fail
# count should fall slightly. Measured read-only on the 08-02/1 stamp: V-H4 fires
# on 1 of 24 quoted judge hard fails, V-H5 on 1 of 44 absence hard fails, V-H2
# withdraws roughly 4 graded claims from each of 27 findings a day, and V-H1 and
# V-H3 change what the judge can SEE rather than what it decides — so their
# effect is the one that cannot be predicted from here and is exactly what panel
# 3 is for. Two of the five (V-H1, V-H2) alter the population's claim SET, not
# just its verdicts, which is on its own sufficient reason never to pool this
# stamp with 2026-08-03/1.
#
# 2026-08-05/1 — the R train, the PRECISION batch. Unlike V-G and V-H, which
# corrected how claims were GRADED, this one corrects which claims EXIST and what
# a tally is entitled to be called. Two of its four parts change the population's
# claim SET and one changes the published NUMBER, so pooling it with any earlier
# stamp would make all three invisible at once.
#
#   Q-1a  the labeled-scaffold exemption reads PAST the label. It keyed on the
#         bold run and never looked at what followed, so every
#         `- **Heat-wave alerts:** <cited fact>` bullet was floor-exempt and
#         whole bodies segmented to ZERO claims. Measured: 11 critiques in 7 days
#         with no verdicts at all, 10 of them over 1,026-2,091 characters of
#         substantive cited analysis, every one scored 1.0. The LABELED spelling
#         of a derived read joins the synthesis exemption in the same change, so
#         the fix does not trade a false 1.0 for a false no_citation.
#   Q-1b  zero (or near-zero on a substantive body) checkable claims publishes
#         `unassessable` — a NON-score with its own title, tag, body line and
#         counter — instead of borrowing the top of the scale.
#   Q-1c  a judge_status != 'llm' verdict publishes PROVISIONAL under a ceiling.
#   Q-1d  literal JSON syntax is dropped from the claim stream, counted.
#   R2    a detected P/not-P pair in the composition's INPUT set that the body
#         never surfaced is a counted soft failure.
#   R3    a lead buried under a higher-consequence input (the salience check,
#         advisory since it was written) is a counted soft failure.
#
# DIRECTION OF THE EXPECTED SHIFT — mixed, large, and in both directions at once,
# which is the whole reason for the split key.
#
#   * CLAIM COUNT rises sharply on the affected population. Bodies that produced
#     zero claims now produce several; the Italy energy read replayed at 0 -> 3.
#     Every ratio computed over claim counts moves for that reason alone.
#   * MEAN FAITHFULNESS falls. Roughly a third of critiques scored >= 0.999, and
#     some of that was earned on nothing; those become real scores over real
#     denominators, and the two new soft classes add failures that did not exist.
#   * PUBLISHED overall_score falls further and separately, because ~23% of
#     critiques are floor-only and now cap at the provisional ceiling. That is a
#     LABELLING change, not a grading one: the raw tally is unchanged on the row.
#
# The honest summary is that this stamp measures the same fleet more accurately
# and will therefore look worse than its predecessor. Any comparison across the
# boundary is a comparison of two instruments, not of two fleets.
#
# 2026-08-09/1 — the round-5 pair: one regression fix, one honesty fix.
#
#   V-I1 guard 5  the numeral fingerprint is ENDPOINT-AWARE. Round 5 scored
#         V-I1 0-for-1 on live fires — its one absorption (critique b14bf715)
#         demoted a fully-earned hard fail because "issued 6 Aug 06:00, expires
#         8 Aug 08:00" and "issued August 6 at 7:25AM until August 6 at 8:00AM"
#         flatten to the same magnitude set. Every clock-time / month-day
#         endpoint the claim pins must now match the quote AS an endpoint, or
#         the quote does not confirm. One-directional (can only WITHDRAW a
#         confirmation); the 61-pair replay under 2026-08-05/1 flips only
#         b14bf715.
#   rec #8 (2/2)  an unassessable row publishes faithfulness_score = NULL on
#         the critique's verification block and the trace envelope, instead of
#         a raw 1.0 that entered the population mean and read as a perfect
#         pass. ``overall_score`` stays the real capped float (the lateral /
#         gate key); the raw tally on the report object is unchanged.
#
# DIRECTION OF THE EXPECTED SHIFT — small and honest-side. Hard-fail count may
# rise by the b14bf715 class (a suppression withdrawn is a hard fail restored);
# mean faithfulness computed over ``faithfulness_score`` falls slightly because
# unassessable rows leave the numerator instead of contributing 1.0 — which is
# a denominator correction, not a fleet movement. Pooling across this boundary
# would read both as quality changes; the split key is what makes them legible
# as the measurement corrections they are.
#
# 2026-08-10/1 — V-I1 guard 6: the confirmation fingerprint reads PROSE
#   DIRECTION (round-5 §10-5; judge_quote_rules.py's guard-6 banner carries the
#   mechanism). A claim taking one side of a direction axis whose "confirming"
#   quote takes the OPPOSITE side about the same subject was never confirmed —
#   the suppression withdraws. Withdraw-only like guard 5; the 69-pair replay
#   flips only 037f769f. EXPECTED SHIFT: hard-fail count rises by this class.
# ---------------------------------------------------------------------------

#: Stamped into every faithfulness critique's ``data.verification`` block.
JUDGE_PIPELINE_VERSION = "2026-08-10/1"

JUDGE_PROFILE_CURRENT = "current"
JUDGE_PROFILE_INDEPENDENT = "independent"
_JUDGE_PROFILES_ALLOWED = (JUDGE_PROFILE_CURRENT, JUDGE_PROFILE_INDEPENDENT)
_JUDGE_PROMPT_PROFILE_ENV = "LEGBA_JUDGE_PROMPT_PROFILE"


def _judge_prompt_profile(explicit: str | None = None) -> str:
    """Resolve the judge prompt profile: explicit arg → env → ``current``.

    An unrecognized value degrades to ``current`` (never a crashed judge over a
    typo'd profile name; the degradation is deterministic + visible in tests).
    """
    raw = (explicit or os.getenv(_JUDGE_PROMPT_PROFILE_ENV) or "").strip().lower()
    return raw if raw in _JUDGE_PROFILES_ALLOWED else JUDGE_PROFILE_CURRENT


@dataclass
class UnsupportedSpan:
    """One fact-asserting claim the verify pass could not tie to cited evidence.

    ``reason`` ∈ {no_citation, unresolved_citation, judge_unsupported,
    judge_contradicted} for BOTH conventions, plus the composition-only
    ``double_counted`` (two cited sub-claims share underlying lineage → they are
    one independent evidence unit) and ``hedge_laundering`` (a composed clause
    asserts more confidence than the sub-claim it rests on), plus the S3-T1
    ``indicator_uncited_triggered`` (a structured ``data.indicators[]`` entry with
    status ``triggered`` that carries no citation), plus the write/verify-time
    world-knowledge guards ``stale_leader`` (M13 — a stale-cutoff current-office-
    holder reference, e.g. calling the sitting president "former") and
    ``cross_target_leak`` (M15 — a per-country finding naming only OTHER countries
    than its desk target).
    """

    text: str
    # 'no_citation' | 'unresolved_citation' | 'judge_unsupported'
    # | 'judge_contradicted' | 'double_counted' | 'hedge_laundering'
    # | 'indicator_uncited_triggered'
    reason: str
    # The markers the claim DID carry. The unit ([N]) path fills INT signal
    # indices; the composition ([[ref:N]]) path fills INT sub-claim ORDINALS —
    # lets the operator see "it cited [[ref:9]] but only 3 sub-claims exist" /
    # "these two cited ordinals are the same underlying signal". Kept ``int|str``
    # for tolerance (a legacy uuid-marker span could still surface a str).
    markers: list[int | str] = field(default_factory=list)
    # 2026-07-31 — OPTIONAL free-form evidence for the verdict, when the checker
    # that emitted it HAS one: the real column value behind a ``metadata_mismatch``
    # ("prose says 0.68, the column says 0.41"), the violating slice title behind
    # an ``absence_slice_contradicted``. The ``reason`` stays a bare mapped LABEL
    # (the ONE fail-class table + its drift guard depend on that); the human
    # WHY lives here. ``None`` for every pre-existing emitter.
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "reason": self.reason,
            "markers": list(self.markers),
            "detail": self.detail,
            # P2-4 — ADDITIVE hard/soft label (the Primer taxonomy). Derived from
            # the ONE mapping table above; existing readers ignore the extra key.
            "fail_class": fail_class_for_reason(self.reason),
        }


# P2-4 — per-claim ledger bounds. The ``claim_verdicts`` ledger persists EVERY
# graded claim (supported included — previously recorded NOWHERE, the citation-
# hover finding), so it is size-bounded at the persist boundary: at most
# ``_CLAIM_VERDICTS_CAP`` entries, each text at most ``_CLAIM_VERDICT_TEXT_CHARS``
# chars, with an HONEST ``claim_verdicts_truncated`` flag when the cap cut
# anything (never a silently partial ledger presented as complete).
#
# 2026-08-02 — the text cap was 300 and that was too tight to READ a verdict by.
# A truncated claim mislabelled an adjudication: an Americas row lost the word
# "Argentina" past char 300, so the ledger row named a different dispute than the
# claim the judge actually graded. The cap is a SIZE bound on the persisted
# block, not a display budget — 1200 covers the long-tail claim sentence while
# still bounding a 120-row ledger, and a claim long enough to be cut here is
# already pathological rather than merely wordy.
_CLAIM_VERDICTS_CAP = 120
_CLAIM_VERDICT_TEXT_CHARS = 1200

#: ClaimVerdict verdict labels: ``supported`` or a fail class from the table.
VERDICT_SUPPORTED = "supported"


@dataclass
class ClaimVerdict:
    """One row of the per-claim verdict LEDGER (P2-4 deliverable 2b).

    The verify pass previously persisted per-claim data ONLY for failures
    (``unsupported_spans``); a SUPPORTED claim's verdict was recorded nowhere —
    the UI had to say "claim-level verdict not recorded". This ledger carries the
    FULL per-claim record: every checkable/graded claim with its text, the
    citation markers it carried, and its verdict (``supported`` | ``hard_fail`` |
    ``soft_fail``, with the underlying ``reason`` for failures).

    LABELS + PERSISTENCE ONLY: the ledger is derived alongside the existing
    tallies and never feeds a score. ADVISORY spans (double_counted /
    hedge_laundering) are NOT ledger rows — they annotate a claim that is itself
    recorded (typically as supported); they stay in ``unsupported_spans``.
    """

    text: str
    # 'supported' | 'hard_fail' | 'soft_fail' (fail class from the ONE table).
    verdict: str
    # None for supported; the span reason (no_citation / judge_contradicted / …)
    # for failures — so the ledger row explains itself without a span join.
    reason: str | None = None
    markers: list[int | str] = field(default_factory=list)
    # 2026-07-31 — the same OPTIONAL free-form WHY as ``UnsupportedSpan.detail``,
    # carried on the ledger row so a verdict explains itself without a span join
    # (and so a deterministically-VERIFIED supported row can say what it checked).
    detail: str | None = None

    @classmethod
    def supported(
        cls, text: str, markers: list[int | str] | None = None
    ) -> "ClaimVerdict":
        return cls(text=text, verdict=VERDICT_SUPPORTED, markers=list(markers or []))

    @classmethod
    def failed(
        cls,
        text: str,
        reason: str,
        markers: list[int | str] | None = None,
        detail: str | None = None,
    ) -> "ClaimVerdict":
        return cls(
            text=text,
            verdict=fail_class_for_reason(reason),
            reason=reason,
            markers=list(markers or []),
            detail=detail,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text[:_CLAIM_VERDICT_TEXT_CHARS],
            "markers": list(self.markers),
            "verdict": self.verdict,
            "reason": self.reason,
            "detail": self.detail,
        }


def _bounded_claim_verdicts(
    verdicts: list["ClaimVerdict"],
) -> tuple[list[dict[str, Any]], bool]:
    """``(bounded ledger dicts, truncated?)`` — the persist-boundary cap."""
    bounded = [v.as_dict() for v in verdicts[:_CLAIM_VERDICTS_CAP]]
    return bounded, len(verdicts) > _CLAIM_VERDICTS_CAP


@dataclass
class FaithfulnessReport:
    """Result of the faithfulness verify pass over ONE finding's prose.

    ``faithfulness_score`` is the fraction of CHECKABLE fact-asserting claims
    that are supported by a resolving citation (1.0 when there are none).
    ``judge_status`` is ``'deterministic'`` when only the floor ran (the flag
    was off → labelled, NOT a fabricated judge number) or the judge degraded,
    and ``'llm'`` when the LLM judge ran and refined the verdict.
    """

    faithfulness_score: float
    checkable_claims: int
    supported_claims: int
    unsupported_spans: list[UnsupportedSpan] = field(default_factory=list)
    # 'deterministic' (floor only — flag off or judge degraded) | 'llm'.
    judge_status: str = "deterministic"
    # When the judge was MEANT to run (flag on) but couldn't: the soft-fail
    # label so the operator sees WHY the score is the floor, never a guess.
    judge_unavailable_reason: str | None = None
    # COMPOSITION-ONLY (T7): the evidence ceiling — a double-count-corrected
    # noisy-OR/max over the effective_confidence of the INDEPENDENT cited
    # sub-claim components. ``None`` for the unit path (no sub-claim evidence)
    # and for a composition whose citations carry no effective_confidence (we
    # never fabricate a cap). The critique payload folds
    # ``overall_score = min(faithfulness_score, confidence_ceiling)`` so a
    # composition can be at most as confident as its strongest INDEPENDENT
    # cited sub-claim.
    confidence_ceiling: float | None = None
    # V3 (MP:DEC-E) — per-claim-KIND sub-scores from the LLM judge, recorded and
    # never hidden (design §2.3). Maps each judged kind →
    # ``{"checkable", "supported", "score"}``. Empty ``{}`` on the deterministic
    # (judge-off / judge-unavailable) path, for the unit/composition floor, AND
    # on the M14 whole-finding survey path (ONE rubric grades the whole claim
    # list there — per-branch attribution would be fabricated). The headline
    # ``faithfulness_score`` stays the POOLED ratio across ALL kinds, so no
    # per-branch weighting can launder a bad kind behind a good one.
    branch_scores: dict[str, dict[str, Any]] = field(default_factory=dict)
    # P2-4 — the FULL per-claim verdict ledger (supported + hard_fail +
    # soft_fail rows; see :class:`ClaimVerdict`). ADDITIVE: default empty so
    # every existing constructor/reader is byte-identical; the ledger never
    # feeds a score. On the floor path it reconciles exactly (one row per
    # checkable claim); on the judge path it carries the judge-graded prose
    # PLUS the non-prose floor rows the judge could not grade (structured
    # indicators, world-knowledge guards) — the ledger is provenance, the
    # tallies stay the score.
    claim_verdicts: list[ClaimVerdict] = field(default_factory=list)
    # 2026-07-31 verify-path structural fixes — the RECEIPTS counters. Every
    # structural fix in that train ships with a counter here (name → count) so
    # the class it addresses can be measured, and can never silently regrow:
    # ``claims_dropped_nonpropositional`` (V-F), ``metadata_verified`` /
    # ``metadata_mismatch`` / ``metadata_unverifiable`` (V-C),
    # ``hardfail_demoted_no_quote`` (V-D), ``absence_slice_verified`` /
    # ``absence_slice_contradicted`` / ``absence_slice_unavailable`` (V-B),
    # ``citationless_graded`` (A3). ADDITIVE + sparse: a counter appears ONLY
    # when it fired, so a run that trips nothing is byte-identical to pre-train.
    counters: dict[str, int] = field(default_factory=dict)
    # INTERNAL arithmetic provenance (never persisted): the denominator the
    # ``faithfulness_score`` above was computed over. The floor path's is
    # ``checkable_claims``; the judge path's is the reconciled
    # ``effective_checkable`` (which can differ from ``checkable_claims``). The
    # deterministic post-judge overrides (V-C/V-B) rescore against THIS number so
    # they can never silently re-base the score onto a different denominator.
    score_denominator: int | None = None
    # Q-1 (2026-08-05) — the SCORE STATE. ``scored`` when the tallies are a real
    # measurement over a real claim list; ``unassessable`` when they are not and
    # the number must therefore not be published as a faithfulness score at all
    # (see judge_assessability.resolve_score_state). Stamped ONCE, at the end of
    # verify_finding_faithfulness, after every fold has settled the final
    # ``checkable_claims`` — so no intermediate constructor can disagree with the
    # verdict that ships. Defaults to ``scored`` so a directly-constructed report
    # (tests, gepa) is byte-identical until it is stamped.
    score_state: str = SCORE_STATE_SCORED
    score_state_reason: str | None = None

    @property
    def provisional(self) -> bool:
        """Was this verdict produced WITHOUT the LLM judge?

        Q-1(c). A floor-only verdict detects missing citations; it structurally
        cannot confirm that a cited claim is faithful to what it cites. Publishing
        it beside an adjudicated one, with no mark, is what let a 26-hour judge
        outage write 611 scored critiques and drop fleet mean faithfulness 0.21
        with nothing on any surface saying the grader was gone.
        """
        return is_provisional(self.judge_status)

    def bump(self, name: str, n: int = 1) -> None:
        """Increment a receipts counter (sparse — absent means never fired)."""
        if n:
            self.counters[name] = self.counters.get(name, 0) + n

    def as_dict(self) -> dict[str, Any]:
        bounded, truncated = _bounded_claim_verdicts(self.claim_verdicts)
        return {
            # Q-1 rec #8, second half (2026-08-09): an UNASSESSABLE report has no
            # measurement to publish under this key — its raw tally (1.0 over an
            # empty claim list) read as a perfect pass to every consumer that did
            # not also read ``score_state`` (the trajectory ledger admitted delta
            # claims on it; round 5 §9b measured it). NULL says "never checked";
            # the published gate number below stays a REAL float, because
            # ``overall_score`` is the key the gates and the thirteen laterals
            # fold on. The raw tally itself is untouched on the report object.
            "faithfulness_score": (
                None
                if self.score_state == SCORE_STATE_UNASSESSABLE
                else round(self.faithfulness_score, 4)
            ),
            "checkable_claims": self.checkable_claims,
            "supported_claims": self.supported_claims,
            "unsupported_spans": [s.as_dict() for s in self.unsupported_spans],
            "judge_status": self.judge_status,
            "judge_unavailable_reason": self.judge_unavailable_reason,
            "confidence_ceiling": (
                round(self.confidence_ceiling, 4)
                if self.confidence_ceiling is not None
                else None
            ),
            "branch_scores": self.branch_scores,
            # P2-4 additive: the size-bounded per-claim ledger + honest cut flag.
            "claim_verdicts": bounded,
            "claim_verdicts_truncated": truncated,
            # 2026-07-31 additive: the structural-fix receipts counters (sparse)
            # + the population SPLIT key, so the trace envelope records which
            # verify pipeline produced this number.
            "counters": dict(self.counters),
            # Q-1 additive: the honesty pair. ``score_state`` says whether the
            # number above is a measurement at all; ``provisional`` says whether
            # a grader adjudicated it.
            "score_state": self.score_state,
            "score_state_reason": self.score_state_reason,
            "provisional": self.provisional,
            # The PUBLISHED gate number, computed by the one policy function, so
            # every consumer of this block caps on the same value the critique row
            # carries instead of re-deriving it (or, as the escalation gate did,
            # capping on the raw tally — which for a zero-claim finding was 1.0,
            # i.e. no demotion at all on precisely the findings we had not checked).
            "overall_score": round(
                gate_score(
                    score=self.faithfulness_score,
                    ceiling=self.confidence_ceiling,
                    score_state=self.score_state,
                    provisional=self.provisional,
                ),
                4,
            ),
            "judge_pipeline_version": JUDGE_PIPELINE_VERSION,
        }


def _resolved_citation_ids(citations: Any) -> set[str]:
    """The set of resolved signal_ids in a finding's ``data['citations']``.

    Tolerates the P0-T1 shape (``[{"marker": "[N]", "signal_id": ...}, ...]``)
    and skips any entry without a non-empty signal_id (never a fabricated id).
    """
    out: set[str] = set()
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if isinstance(entry, Mapping):
            sid = entry.get("signal_id")
            if isinstance(sid, str) and sid:
                out.add(sid)
    return out


def _marker_to_signal_id(citations: Any) -> dict[int, str]:
    """Map each citation's ``[N]`` marker index → its resolved signal_id."""
    out: dict[int, str] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        sid = entry.get("signal_id")
        marker = entry.get("marker")
        if not (isinstance(sid, str) and sid):
            continue
        if isinstance(marker, str):
            m = _CLAIM_MARKER_RE.search(marker)
            if m:
                out[int(m.group(1))] = sid
    return out


def _signal_backed_ordinals(citations: Any) -> set[int]:
    """The evidence ordinals whose text is a ``signals`` row — SOURCE reporting.

    V-G1: the discriminator behind the signals-only refutation rule. An ordinal
    qualifies ONLY when its citation entry carries a resolvable ``signal_id``,
    which is exactly the branch of :func:`_marker_to_evidence` that renders a
    cited article's SOURCE text. Everything else in a judge's evidence view is
    ANALYST PROSE wearing an ordinal:

      * the desk GROUNDING blocks (``is_grounding_citation`` — prior read,
        situation register, desk baseline, open questions) folded in at the tail
        of :func:`_marker_to_evidence`;
      * every COMPOSITION sub-claim (``ref_kind='finding'``), whose
        ``evidence_text`` is the cited finding's own body.

    Works on BOTH citation conventions and never fabricates: an entry with no
    resolvable ordinal, or no signal_id, simply does not appear.
    """
    def _project(entry: Mapping[str, Any], n: int) -> Any:
        sid = entry.get("signal_id")
        return n if isinstance(sid, str) and sid else _ORDINAL_SKIP

    # The unit convention keys off the ``[N]`` marker; the subclaim convention
    # off ``ordinal`` / ``[[ref:N]]``. Union both so one function answers for
    # either citation shape — and route the ordinal traversal through THE ONE
    # builder (C-4), never a hand-rolled copy of it.
    return set(_marker_to_signal_id(citations)) | set(
        _build_ordinal_map(citations, _project)
    )


# FAITHFULNESS TRUST BOUNDARY (2026-07): when a citation carries ``source_text``
# (the RAW authoritative article — see inline_target._build_citation_index) the
# judge grounds against IT, not against the analyst's ``snippet`` (which may be a
# ``distilled_body`` LLM summary). The source portion is bounded generously (the
# article is fuller than the summary, so a faithful summary point still traces to
# it) and the whole evidence string is capped so the judge prompt can't bloat.
# When the SOURCE is re-truncated here (or was already an excerpt at build time),
# the judge is told so and softens "absent => unsupported" to "contradicted =>
# unsupported" (F1) — otherwise a claim the analyst faithfully drew from deep in a
# long article would be false-demoted for being past the excerpt cut.
_EVIDENCE_SOURCE_CHARS = 3000
_EVIDENCE_TOTAL_CHARS = 3600
# BACKWARD-COMPAT (F3): entries with NO ``source_text`` (old data / non-signal
# path) keep the ORIGINAL 600-char total cap so the C1 verify-floor calibration on
# pre-existing findings is byte-unchanged. The larger caps above apply ONLY to the
# new source_text-grounded branch.
_EVIDENCE_LEGACY_CHARS = 600
# QW1-B: cap for a DESK GROUNDING block's evidence. Sized to the whole rendered
# block (the producer captures at 2400 chars — ``unit_grounding.
# EVIDENCE_TEXT_CHARS``), NOT the 600-char single-signal legacy cap: a 600-char
# cut would silently hide the tail of a 6-frame situation register and
# false-demote a faithful claim about a frame the model was actually shown. The
# same reasoning ``SITUATION_REGISTER_EVIDENCE_CHARS`` carries on the composition
# side.
_EVIDENCE_GROUNDING_CHARS = 2400


def _grounding_ordinals(citations: Any) -> dict[int, str]:
    """``{ordinal -> evidence_text}`` for the DESK GROUNDING blocks (QW1-B).

    A unit's grounding blocks (prior read / situation register / desk baseline /
    standing questions) are CITABLE in the same flat ``[N]`` space as its signals
    but are NOT ``signals`` rows, so they carry no ``signal_id``. They ARE real,
    rendered, captured evidence — ``evidence_text`` holds the exact bytes the
    model was shown — so this map is what lets the verify path grade a clause
    resting on one instead of scoring it as an unresolved citation.

    EMPTY for every pre-QW1-B finding and every composition (whose entries carry
    ``signal_id`` or the ``ref_kind='finding'`` sub-claim shape), which is what
    keeps both existing paths byte-identical.
    """
    out: dict[int, str] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not is_grounding_citation(entry):
            continue
        marker = entry.get("marker")
        n = None
        if isinstance(marker, str):
            m = _CLAIM_MARKER_RE.search(marker)
            if m:
                n = int(m.group(1))
        if n is None:
            raw_ordinal = entry.get("ordinal")
            if isinstance(raw_ordinal, int) and not isinstance(raw_ordinal, bool):
                n = raw_ordinal
        if n is None:
            continue
        title = entry.get("title")
        text = str(entry["evidence_text"])
        if isinstance(title, str) and title.strip() and title.strip() not in text:
            text = f"{title.strip()}\n{text}"
        out[n] = text[:_EVIDENCE_GROUNDING_CHARS]
    return out


# _marker_to_evidence extracted to judge_evidence.py (2026-08-05) — the first
# brick of the judge-subsystem seam. Re-exported: all callers unchanged.
from .judge_evidence import (  # noqa: F401
    _marker_to_evidence,
    machine_coded_ordinals,
)

# R2/R3 (2026-08-05) — the INPUT checks, judge-subsystem brick 3. Imported ONE
# WAY; the module late-binds back through ``_verify()`` at call time.
from .judge_input_checks import (  # noqa: F401,E402 — re-exported verify surface
    BURIED_LEAD_SALIENCE,
    UNSURFACED_CONTRADICTION,
    fold_input_checks,
)
def _canon_ref(raw: Any) -> str | None:
    """Canonicalize a uuid-ish value into ``str(UUID(...))`` or ``None``.

    Canonicalizes case + formatting so an UPPERCASE ``[[ref:UUID]]`` marker can't
    false-miss against a lowercase cited id (both collapse to the canonical
    lowercase 8-4-4-4-12 form). Non-uuid input → ``None`` (never fabricated).
    """
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return str(raw)
    try:
        return str(UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        return None


def _uses_subclaim_convention(citations: Any) -> bool:
    """Discriminator: is this a COMPOSITION (sub-claim) bridge, not a unit signal one?

    ``True`` iff ANY citation carries ``ref_kind == "finding"`` (the new kind-aware
    entry) OR any ``marker`` string starts with ``"[[ref:"`` (covers new ordinal
    ``[[ref:N]]`` entries AND legacy stored ``[[ref:<uuid>]]`` entries, so both
    still route to the sub-claim floor rather than the unit ``[N]`` floor). The
    ``"[[ref:"`` prefix test can never alias a unit ``[N]`` marker. Defaults
    ``False`` → the unit ``[N]`` → signal convention (selected unless a composition
    signal is positively present — a zero-regression default).
    """
    if not isinstance(citations, (list, tuple)):
        return False
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("ref_kind") == "finding":
            return True
        marker = entry.get("marker")
        if isinstance(marker, str) and marker.startswith("[[ref:"):
            return True
    return False


def _citation_ordinal(entry: Mapping[str, Any]) -> int | None:
    """The 1-based ORDINAL a composition citation resolves to, or ``None``.

    Prefers the explicit ``ordinal`` field (the CITE block stamps an int); falls
    back to parsing ``N`` out of the entry's ``[[ref:N]]`` ``marker``. A legacy
    stored ``[[ref:<uuid>]]`` marker carries no digit ordinal → ``None`` (so a
    legacy composition floors low rather than mis-resolving — the documented
    boundary; new rows carry the field). Never fabricates.
    """
    raw = entry.get("ordinal")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, (str, float)):
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    marker = entry.get("marker")
    if isinstance(marker, str):
        m = _REF_MARKER_RE.search(marker)
        if m:
            return int(m.group(1))
    return None


# --- THE ONE ordinal-map builder (C-4) -------------------------------------
# Every per-ordinal projection below shared ONE traversal, copy-pasted five
# times: tolerate a non-list ``citations``; skip non-Mapping entries; resolve
# the entry's sub-claim ORDINAL via :func:`_citation_ordinal`; skip entries with
# no resolvable ordinal; project the entry to a value. Collapsed here so the
# traversal has a single definition and a per-map change cannot drift.
#
# SKIP SEMANTICS (load-bearing — this is why the projector returns a sentinel
# rather than ``None``): a projector returning :data:`_ORDINAL_SKIP` contributes
# NOTHING for that entry, which on a DUPLICATE ordinal leaves any previously
# projected value INTACT. That is exactly the ``continue`` each hand-rolled loop
# used, and it differs observably from writing a ``None``. ``None`` itself is a
# legitimate projected value and is stored.
_ORDINAL_SKIP: Any = object()


def _build_ordinal_map(
    citations: Any,
    project: Callable[[Mapping[str, Any], int], Any],
) -> dict[int, Any]:
    """Traverse ``citations`` once, keyed by resolved sub-claim ORDINAL.

    ``project(entry, ordinal)`` returns the value to store, or
    :data:`_ORDINAL_SKIP` to contribute nothing for that entry. Later entries
    overwrite earlier ones for the same ordinal (last-wins), except that a
    SKIPPED entry never clears an earlier value.
    """
    out: dict[int, Any] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        n = _citation_ordinal(entry)
        if n is None:
            continue
        value = project(entry, n)
        if value is _ORDINAL_SKIP:
            continue
        out[n] = value
    return out


def _resolved_citation_ordinals(citations: Any) -> set[int]:
    """The set of resolved sub-claim ORDINALS in a composition's ``citations``.

    Skips any entry that carries no resolvable ordinal (never a fabricated one).
    """
    return set(_build_ordinal_map(citations, lambda _entry, n: n))


def _ordinal_evidence_map(citations: Any) -> dict[int, str]:
    """Map each cited sub-claim's ORDINAL → its evidence TEXT.

    The composition enriches each citation at synth time with the cited
    sub-claim's ``evidence_text`` (its body, captured point-in-time). Fallback
    chain: ``evidence_text`` → ``title`` → the ordinal itself. NEVER fabricates
    evidence — a citation with no captured text degrades to its ordinal string so
    the judge still has a stable label.
    """

    def _project(entry: Mapping[str, Any], n: int) -> str:
        text = entry.get("evidence_text")
        if not (isinstance(text, str) and text):
            text = entry.get("title")
        if not (isinstance(text, str) and text):
            text = str(n)
        return str(text)

    return _build_ordinal_map(citations, _project)


def _ordinal_effconf_map(citations: Any) -> dict[int, float]:
    """Map each cited sub-claim's ORDINAL → its ``effective_confidence``.

    Reads the ``effective_confidence`` the composition captured at synth time
    (already ``min(confidence, faithfulness)`` from the unit's own verify). A
    citation missing / with a non-numeric value is SKIPPED — its clause is then
    never hedge-flagged and never contributes to the cap (honest: no fabricated
    correlation/ceiling).
    """

    def _project(entry: Mapping[str, Any], _n: int) -> Any:
        eff = entry.get("effective_confidence")
        if eff is None:
            return _ORDINAL_SKIP
        try:
            return float(eff)
        except (TypeError, ValueError):
            return _ORDINAL_SKIP

    return _build_ordinal_map(citations, _project)


def _ordinal_derived_map(citations: Any) -> dict[int, set[str]]:
    """Map each cited sub-claim's ORDINAL → its ``derived_from`` id SET.

    Each cited sub-claim carries its own underlying lineage/signal ids (captured
    at synth time). Two cited sub-claims are CORRELATED iff these sets intersect
    (the shared-lineage detector — T7). A citation with no ``derived_from`` list
    is omitted → it forms its own singleton component (never falsely correlated).
    """

    def _project(entry: Mapping[str, Any], _n: int) -> Any:
        df = entry.get("derived_from")
        if not isinstance(df, (list, tuple)):
            return _ORDINAL_SKIP
        return {str(x) for x in df if x is not None and str(x)}

    return _build_ordinal_map(citations, _project)


def _ordinal_source_map(citations: Any) -> dict[int, str]:
    """Map each cited sub-claim's ORDINAL → its SOURCE producer (``source`` /
    ``analyst_id``), the discriminator for P7-F1(6).

    A country_composition cites the SEVEN bounded UNITS of one desk: they share
    the desk's wire lineage by construction (all read the same signal slice) yet
    each answers a DIFFERENT bounded question — so shared lineage across two
    DIFFERENT-source units is NOT double-counting. A thematic / region / world
    composition cites many blocks of the SAME source (``escalation`` desks,
    ``country_composition`` reads) where shared lineage DOES mean two views of one
    incident. Keying the correlation on shared-lineage AND same-source flags the
    real double-count and stops falsely collapsing a desk's 7 independent units.
    A citation with no source is omitted → it never blocks a union (back-compat).
    """

    def _project(entry: Mapping[str, Any], _n: int) -> Any:
        src = entry.get("source")
        if not (isinstance(src, str) and src):
            src = entry.get("analyst_id")
        if isinstance(src, str) and src:
            return src
        return _ORDINAL_SKIP

    return _build_ordinal_map(citations, _project)


def _correlated_components(
    ids: list[_H],
    derived_map: Mapping[_H, set[str]],
    group_map: Mapping[_H, str] | None = None,
) -> list[list[_H]]:
    """Connected components over ``ids`` (composition sub-claim ordinals — kept
    generic over any hashable key), joined when their ``derived_from`` sets
    intersect. Each component = ONE independent evidence unit (T7).

    P7-F1(6): when ``group_map`` is supplied, two ids are joined only when they
    share lineage AND belong to the SAME group (same producing source). Two ids
    that share lineage but come from DIFFERENT sources are NOT double-counting —
    they are different bounded questions over a shared wire slice (the
    country_composition case: 7 units, one desk). An id missing from ``group_map``
    imposes NO constraint (falls back to lineage-only) so the legacy behaviour is
    byte-identical when no source is present.

    Pure stdlib union-find; O(n^2) pairwise which is fine at composition scale
    (≤4 units × a couple fires each).
    """
    parent: dict[_H, _H] = {i: i for i in ids}

    def find(x: _H) -> _H:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: _H, b: _H) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            da = derived_map.get(ids[i]) or set()
            db = derived_map.get(ids[j]) or set()
            if not (da and db and (da & db)):
                continue
            if group_map is not None:
                gi = group_map.get(ids[i])
                gj = group_map.get(ids[j])
                # Distinct KNOWN sources sharing lineage = different bounded
                # questions, NOT one double-counted source → do not union.
                if gi is not None and gj is not None and gi != gj:
                    continue
            union(ids[i], ids[j])

    comps: dict[_H, list[_H]] = {}
    for i in ids:
        comps.setdefault(find(i), []).append(i)
    return list(comps.values())


def _is_fact_asserting(claim: str) -> bool:
    """Heuristic: is this span a CHECKABLE factual assertion?

    Skips empty spans, bare markdown scaffolding (headings, list bullets with no
    prose, separators), and the forward-looking 'indicators to watch' lines. A
    span carrying real prose words is treated as a factual assertion that ought
    to be grounded — the conservative direction (more claims checked, not fewer).
    """
    s = claim.strip()
    if not s:
        return False
    # Strip leading list/heading scaffolding for the content test.
    stripped = s.lstrip("#-*> ").strip()
    if not stripped:
        return False
    low = stripped.lower()
    # M14: an explicit ``[no citation]`` annotation marks a DELIBERATELY un-citable
    # synthesis / survey clause — floor-exempt (not an uncited-fact defect). The
    # judge still grades it (via _is_judgeable_claim), so a fabricated absence
    # dressed up with the marker is still caught semantically.
    if _NO_CITATION_MARKER in low:
        return False
    # A markdown heading line is structure, not an assertion.
    if s.lstrip().startswith("#"):
        return False
    # P7-F1(1): a whole-line BOLD HEADING (``**Key points**``, ``- **Drivers**``)
    # is a section label, not a fact — extend the ``#`` heading drop to the bold
    # style the composition assessors emit (the AU energy finding floored 0/4 with
    # ``**Key points**`` itself counted as an uncited claim). P7 r2: only a
    # HEADING-SHAPED bold line drops — a bold FACTUAL sentence
    # (``**Tehran resumed enrichment**``) is a real claim, still counted. A
    # ``**Severity:** High`` scaffold line does NOT match (_BOLD_HEADING_RE requires
    # the line be ONLY the bold run) — it stays handled by _LABELED_SCAFFOLD_RE.
    if _is_bold_heading(s) or _is_backtick_heading(s):
        return False
    # Phase-V D1: the structural as-of line carries the run's own coordinates,
    # copied from the rendered slice header. No signal states them, so no [N]
    # can ground it — floor-exempt as structure (see _AS_OF_LINE_RE).
    if _is_as_of_line(s):
        return False
    # P7-F1(5): a forward-looking watch/indicator bullet ('… would confirm …') is
    # a future non-occurrence the read is watching FOR — nothing exists to cite.
    if _is_forward_looking(low):
        return False
    # (#116b) A bolded label:value line (**Severity:** High) is scaffolding, not a
    # citable present-fact — FLOOR-ONLY (the judge still grades it via
    # _is_judgeable_claim). Matched on the ORIGINAL span so the leading ``**`` is
    # intact (the ``stripped`` form above has already lstripped the ``*`` bold).
    #
    # Q-1: the test is ``is_labeled_scaffold``, not the bare regex. The regex
    # keys on the LABEL and never looked past it, so ``- **Heat-wave alerts:**
    # Red alerts have been issued for 25-27 of Italy's major cities …[6][42]``
    # — a fully-formed cited fact wearing a signpost — was floor-exempt for
    # having a bold run in front of it. Whole bodies are built this way, so
    # whole bodies produced ZERO checkable claims. The position of this test in
    # the ladder is unchanged: a ``**BLUF:** …`` line is not a short-value stamp,
    # falls through, and still lands on the synthesis exemption below.
    if is_labeled_scaffold(s):
        return False
    # The forward-looking watch section is explicitly NOT a present-fact claim.
    for head in _NON_FACTUAL_HEADINGS:
        if low.startswith(head):
            return False
    # (#1) A BLUF / Assessment / synthesis OPENER is the analyst's derived read
    # over the cited evidence, not a new first-order citable fact.
    for pref in _SYNTHESIS_PREFIXES:
        if low.startswith(pref):
            return False
    # Q-1: the LABELED spelling of the same thing. ``**Severity:** elevated — no
    # nationwide blackout has been reported`` and ``Assessed: elevated — no
    # nationwide blackout has been reported`` are one sentence in two costumes;
    # before this the first was floor-exempt only by accident (the blanket
    # scaffold rule) and would have become an uncited FACT the moment that rule
    # started reading prose. FLOOR-ONLY — the judge grades it either way.
    if is_assessment_scaffold(s):
        return False
    # (#1) An ABSENCE / negative finding cannot cite a signal that does not exist
    # — flagging it no_citation crushed honest low-risk reads. A clause OPENING
    # with a bare "No " is a non-occurrence assertion ("No election … is reported",
    # "No military unrest … appears") — nothing exists to cite; guard only the rare
    # positive "No fewer/less than" idioms. V3: this test is factored into
    # ``_is_absence_claim`` so the floor exemption and the classifier's ``absence``
    # route share ONE definition and cannot drift apart.
    if _is_absence_claim(low):
        return False
    # Require at least a few word characters so a stray "—" / "..." isn't a claim.
    if len(re.findall(r"[A-Za-z]{2,}", stripped)) < 2:
        return False
    return True


def _is_judgeable_claim(claim: str) -> bool:
    """Is this span PROSE the LLM judge should grade?

    Broader than :func:`_is_fact_asserting`: it drops the same pure scaffolding
    (blank lines, markdown headings, the forward-looking watch section, stray
    punctuation) but does NOT apply the floor's BLUF / synthesis / absence
    EXEMPTIONS. Those exemptions correctly stop the deterministic no-citation
    FLOOR from crushing an honest, un-citable synthesis or absence claim — but
    they must NOT hide the claim from the judge, or a FABRICATED zero-citation
    BLUF body scores a vacuous 1.0 (every claim exempt → checkable=0; P4 pre-push
    review H1). The judge, unlike the mechanical floor, CAN tell a faithful
    synthesis from an invented fact (its calibrated fabrication-vs-analysis
    prompt), so it grades EVERY prose span; only the floor exempts.
    """
    s = claim.strip()
    if not s:
        return False
    stripped = s.lstrip("#-*> ").strip()
    if not stripped:
        return False
    low = stripped.lower()
    if s.lstrip().startswith("#"):
        return False
    # P7-F1(1): a whole-line BOLD HEADING is structure for the JUDGE too — a bold
    # section label carries no fact for the judge to grade. P7 r2: only a genuine
    # HEADING-SHAPED bold line is exempt — a bold FACTUAL sentence
    # (``**Tehran resumed enrichment**``) is a real claim the judge MUST grade
    # (H1). (Unlike the floor's BLUF/synthesis/absence exemptions, a genuine bold
    # heading is a true NON-claim, so exempting it does not hide a fabricated fact.)
    if _is_bold_heading(s) or _is_backtick_heading(s):
        return False
    # Phase-V D1: the structural as-of line states run metadata (date, window,
    # signal count), which is not in the cited evidence set the judge grades
    # against — so the judge cannot check it and could only mark it unfaithful.
    # A true NON-claim, exempt like a heading (see _AS_OF_LINE_RE for why this
    # does not violate H1).
    if _is_as_of_line(s):
        return False
    # Still skip the explicitly forward-looking watch section (not a present
    # claim by construction); _segment_claims already section-skips it, this
    # guards an inline heading-opener.
    for head in _NON_FACTUAL_HEADINGS:
        if low.startswith(head):
            return False
    # P7 r2 (H1): the JUDGE is exempt ONLY from a PURE forward-looking prediction —
    # one whose conditional idiom governs the WHOLE clause (a bare 'X would confirm
    # Y' signpost that leaked outside a recognized watch heading). _is_forward_looking
    # is now ANCHORED, so a present-fact main clause carrying a conditional TAIL
    # ('Tehran resumed enrichment, which would confirm a breakout') is NOT forward-
    # looking and the judge STILL grades it — the round-1 unanchored substring match
    # wrongly hid such present facts. A present-tense absence read ('No evidence of
    # X') is likewise judge-graded (the floor exempts absence, the judge does not).
    if _is_forward_looking(low):
        return False
    if len(re.findall(r"[A-Za-z]{2,}", stripped)) < 2:
        return False
    return True


# ---------------------------------------------------------------------------
# V3 (2026-07-16, MP:DEC-E) — per-CLAIM-KIND classifier + versioned judge
# profiles. The absence branch is the ONLY branch DEC-E approves building; the
# scaffolding (classifier + profile registry + per-branch telemetry) is the
# shared substrate every later branch (synthesis / forward_looking / stance /
# V5 support-hardening) plugs into WITHOUT touching the core (design §2.4).
#
# The classifier is DETERMINISTIC + pure-lexical (verify.py imports nothing from
# the analysts package): it is the thing that STOPS the measured 0.0/0.2/1.0
# variance on identical absence prose, because the variance came from handing an
# absence sentence to the free-latitude generic judge prompt — a deterministic
# route to a constrained per-kind prompt removes that latitude. Every span is
# assigned EXACTLY ONE kind (first-match-wins priority order → total + stable);
# the ``narrative`` register is a MODIFIER carried from the analyst kind at the
# call site, NOT a sixth kind, so it can never mis-partition the routing.
#
# COEXISTENCE with M14 (the whole-finding null-result survey below): M14 is a
# WHOLE-FINDING route (≤1 positive claim ⇒ the survey rubric grades the entire
# claim list as one corpus survey); the V3 absence branch is the PER-CLAIM route
# for the embedded-absence-in-a-fact-rich-finding case M14 misses (design §3.2
# #4 / §3.5). ``_run_judge`` checks M14 FIRST and only partitions when the
# finding is NOT a whole-finding null — the two never compete.
# ---------------------------------------------------------------------------

# Claim-kind labels (design §2.1). ``citation_support`` is the residual — every
# fact-asserting span that is none of the four structural/derived kinds.
CLAIM_KIND_STRUCTURE = "structure"
CLAIM_KIND_FORWARD_LOOKING = "forward_looking"
CLAIM_KIND_ABSENCE = "absence"
CLAIM_KIND_SYNTHESIS = "synthesis"
CLAIM_KIND_CITATION_SUPPORT = "citation_support"


def _claim_kind(claim: str) -> str:
    """Assign a segmented span EXACTLY ONE claim kind (design §2.1).

    First-match-wins PRIORITY ORDER — structure > forward_looking > absence >
    synthesis > citation_support — so the function is TOTAL (always returns a
    kind) and STABLE (deterministic, pure-lexical). Reuses the already-live
    lexical anchors (``_is_bold_heading``, ``_is_forward_looking``,
    ``_LABELED_SCAFFOLD_RE``, ``_ABSENCE_MARKERS``, ``_SYNTHESIS_PREFIXES``,
    ``_NO_CITATION_MARKER``) that the historical recalibrations calibrated, so a
    span classified ``absence`` here is the SAME span the floor exempts as
    absence — the route and the floor's exemptions cannot drift apart.

    NOTE (V3 scope): only the ``absence`` route is consumed by a dedicated judge
    profile in this train; the other kinds are classified for TELEMETRY
    (``branch_scores``) and route to the existing prompt, per DEC-E.
    """
    s = claim.strip()
    stripped = s.lstrip("#-*> ").strip()
    low = stripped.lower()
    # structure — a markdown/bold heading, a labeled ``**Severity:**`` scaffold,
    # or a pure-marker span (matched on the ORIGINAL span so ``**``/``[N]`` are
    # intact). This is the highest priority: a heading is never a claim of any
    # other kind.
    if (
        s.lstrip().startswith("#")
        or _is_bold_heading(s)
        # Q-1: a SHORT-value stamp only (see _is_fact_asserting). A labeled span
        # carrying prose now falls through to the kind its prose actually is —
        # ``**BLUF:** Italy faces …`` routes to ``synthesis`` (the rubric written
        # for it) instead of being filed as furniture and never graded.
        or is_labeled_scaffold(s)
        or _CITATION_ONLY_RE.match(s)
        or _is_backtick_heading(s)
        # Phase-V D1 — the as-of line is a machine-shaped coordinate line, so it
        # routes to ``structure`` for the branch_scores telemetry too. Kept in
        # lockstep with the two gate exemptions above it: a span the gates treat
        # as structure must not be counted as some other kind here.
        or _is_as_of_line(s)
    ):
        return CLAIM_KIND_STRUCTURE
    for head in _NON_FACTUAL_HEADINGS:
        if low.startswith(head):
            return CLAIM_KIND_STRUCTURE
    # forward_looking — a future-conditional idiom governs the whole clause. Runs
    # BEFORE absence so a ``would confirm``/``to watch for`` signpost stays a
    # prediction, never mis-routed to the absence prompt (design §3.6 test).
    if _is_forward_looking(low):
        return CLAIM_KIND_FORWARD_LOOKING
    # absence — the negative-finding class this branch owns.
    if _is_absence_claim(low):
        return CLAIM_KIND_ABSENCE
    # synthesis — a BLUF / Assessed / JUDGMENT derived-read opener, or a span the
    # assessor explicitly flagged ``[no citation]`` (M14: a deliberately
    # un-citable synthesis / framing line — design §2.1 table). Ordered AFTER
    # absence so a marked absence still routes to the absence rubric.
    if _NO_CITATION_MARKER in low:
        return CLAIM_KIND_SYNTHESIS
    for pref in _SYNTHESIS_PREFIXES:
        if low.startswith(pref):
            return CLAIM_KIND_SYNTHESIS
    # Q-1: the labeled derived-read (``**Severity:** …``) routes to the SYNTHESIS
    # rubric, in lockstep with the floor exemption above it — the design rule that
    # a span the floor exempts as synthesis is classified synthesis here, so the
    # route and the exemption can never drift apart.
    if is_assessment_scaffold(s):
        return CLAIM_KIND_SYNTHESIS
    # citation_support — everything else (the residual fact-asserting span).
    return CLAIM_KIND_CITATION_SUPPORT


@dataclass(frozen=True)
class JudgeProfile:
    """A versioned per-claim-kind judge profile (design §2.2).

    The profiles are CODE, versioned by a string stamp — NOT a descriptor, NOT a
    migration. A critique records ``data.verification.branch_versions = {kind:
    version, ...}`` for the profiles that RAN, so a recalibration becomes a
    visible, greppable, per-kind version bump (the plan's "versioned judge
    profiles" / "5-recalibrations-in-8-days class") instead of an invisible
    edit buried among flat-file commits.

    ``judge_system is None`` ⇒ the kind is classified for TELEMETRY only and is
    NOT sent to a dedicated judge call — it rides the existing shared prompt (the
    DEC-E scope boundary: only ``absence`` and ``citation_support`` carry a
    prompt in this train).
    """

    kind: str
    version: str
    # The system prompt for this kind's dedicated judge call. ``None`` ⇒ no
    # dedicated call (telemetry-only kind; rides the shared prompt).
    judge_system: str | None = None



# The absence-branch judge system prompt (design §3.4). A NEGATIVE-specific
# rubric: the free-latitude "is this cited?" framing that produced the 0.0/0.2/
# 1.0 spread is replaced with an explicit supported/contradicted/unsupported
# rubric for absence claims, scoped to the searched evidence set. Output is the
# SAME flat ``{"verdicts": [...]}`` shape the shared judge emits (deterministic
# parse; no nested schema — nested crashed the pipeline twice).
_ABSENCE_JUDGE_SYSTEM = (
    "You are a faithfulness judge grading ABSENCE / NEGATIVE claims — statements "
    "that something did NOT occur, was NOT observed, or is NOT evidenced. You are "
    "given the evidence set the analyst searched (the [N] -> evidence map below) "
    "and a list of absence claims. For each absence claim decide EXACTLY ONE "
    "verdict:\n"
    "- supported: the evidence set genuinely does NOT contain the thing the claim "
    "says is absent, AND the claim's scope matches the evidence searched (a claim "
    "scoped to 'the reviewed signals' / a named country / a stated corpus is "
    "judged against THAT scope, not the whole world).\n"
    "- contradicted: the evidence set plainly SHOWS the very thing the claim says "
    "is absent (e.g. the claim says 'no strikes reported' but a cited item reports "
    "a strike). A contradicted absence is the highest-severity error.\n"
    "- unsupported: the claim asserts an absence that is UNBOUNDED or unscoped "
    "('nothing is happening', 'there is no risk anywhere') that the searched "
    "evidence cannot possibly establish, OR names a specific missing "
    "event/number/place with a scope the evidence set does not cover.\n"
    "Do NOT mark a scoped, evidence-consistent absence 'unsupported' merely "
    "because a negative has no citation — a correctly-scoped negative over a "
    "searched set is the normal, faithful shape of an honest low-risk read. "
    'Output strict JSON only: {"verdicts": ["supported"|"contradicted"|'
    '"unsupported", ...]} with one verdict per claim, in order.'
    + _JUDGE_QUOTE_RULE
    + _JUDGE_QUALIFIER_RULE
    + " Output only the JSON object."
)

# The versioned profile registry (design §2.2 / §5.2 step 2). ``absence`` and
# ``citation_support`` carry a prompt this train; the other three kinds are
# stubbed (judge_system=None → telemetry-only), stamped so a later train that
# gives them a prompt is a visible per-kind version bump. Bump a ``version`` on
# ANY prompt/floor-semantics change to that kind.
#
# V-H (2026-08-04) bumps BOTH prompted kinds. The rubrics are untouched; the
# EVIDENCE is not. Every unit citation now renders an ``OUTLET:`` line (V-H1),
# which both the citation_support lead and the absence rubric's evidence line
# carry, so a judge on this stamp is answering from a different evidence view
# than one on 2026-08-03/1. The three stubbed kinds have no prompt and no floor
# semantics of their own, so they do not move.
_JUDGE_PROFILES: dict[str, JudgeProfile] = {
    CLAIM_KIND_CITATION_SUPPORT: JudgeProfile(
        kind=CLAIM_KIND_CITATION_SUPPORT,
        version="citsupp.v5",
        judge_system=None,  # rides the existing unit/composition prompt in _run_judge
    ),
    CLAIM_KIND_ABSENCE: JudgeProfile(
        kind=CLAIM_KIND_ABSENCE,
        version="absence.v3",
        judge_system=_ABSENCE_JUDGE_SYSTEM,
    ),
    CLAIM_KIND_SYNTHESIS: JudgeProfile(
        kind=CLAIM_KIND_SYNTHESIS, version="synthesis.v0", judge_system=None
    ),
    CLAIM_KIND_FORWARD_LOOKING: JudgeProfile(
        kind=CLAIM_KIND_FORWARD_LOOKING, version="fwd.v0", judge_system=None
    ),
    CLAIM_KIND_STRUCTURE: JudgeProfile(
        kind=CLAIM_KIND_STRUCTURE, version="structure.v0", judge_system=None
    ),
}


# M14 (2026-07-06) — the corpus-SURVEY shape a NULL-RESULT finding uses to
# characterize what the enumerated signals DO contain ("51 of the 92 signals
# focus on floods/sports/trade", "none of the 78 signals reference …"). Paired
# with the absence markers to detect the null-result finding shape below.
_SURVEY_SHAPE_RE = re.compile(
    r"\b\d+\s+signals?\b"
    r"|\bsignals?\s+(?:focus|concern|relate|reference|are about|center|centre|"
    r"revolve|pertain|discuss|cover)\b"
    r"|\bnone\s+of\s+the\s+\d+\b"
    r"|\bacross\s+(?:the\s+)?(?:examined|reviewed|available|analy[sz]ed)\b",
    re.IGNORECASE,
)


def _is_null_result_finding(body: str) -> bool:
    """True for an honest NULL-RESULT / corpus-survey finding (M14).

    The class the faithfulness judge most crushes: a unit that correctly reports
    "nothing concerning found — the N signals are about floods/sports/trade".
    Its content is a corpus-scoped NEGATIVE plus a survey of what the signals DO
    show; almost nothing is individually citable. The judge grading each clause
    "is this cited?" scores such honest nulls like fabrications (observed
    0.0/0.2/1.0 across runs). When this fires, :func:`_run_judge` routes the
    finding to a SURVEY rubric ("is this a faithful survey of the enumerated
    evidence?") instead of per-clause citation.

    Conservative: requires an ABSENCE or SURVEY signal AND that the finding asserts
    at most one individually-citable positive fact (``_is_fact_asserting`` already
    exempts absence / BLUF / synthesis, so a genuine null nets ~0 positive claims).
    A finding rich in positive cited facts is NOT a null-result and keeps the
    standard per-clause rubric.
    """
    if not body:
        return False
    low = body.lower()
    has_absence = any(m in low for m in _ABSENCE_MARKERS) or bool(
        re.search(r"(?:^|[\n.:;–—-])\s*(?:no|none)\b", low)
    )
    has_survey = bool(_SURVEY_SHAPE_RE.search(body))
    if not (has_absence or has_survey):
        return False
    positive = [c for c in _segment_claims(body) if _is_fact_asserting(c)]
    return len(positive) <= 1


# M14 — the NULL-RESULT judge rubric. A survey-scoped grade for an honest
# corpus-negative, so an un-citable NEGATIVE is not marked "unsupported" merely
# for lacking a per-clause ``[N]``.
_NULL_RESULT_JUDGE_SYSTEM = (
    "You are a faithfulness judge grading a NULL-RESULT / corpus-survey finding. "
    "Its core claim is a NEGATIVE ('no X observed / reported') together with a "
    "survey of what the enumerated signals DO contain. Grade each claim as a "
    "FAITHFUL SURVEY of the cited/enumerated evidence — NOT by whether each clause "
    "carries its own citation. A stated ABSENCE is SUPPORTED unless the evidence "
    "actually SHOWS the thing said to be absent; a survey characterization is "
    "SUPPORTED unless it misdescribes the corpus; reasonable framing / severity / "
    "risk judgement is SUPPORTED. Mark UNSUPPORTED only when the finding claims the "
    "absence of something the evidence plainly contains, or asserts a SPECIFIC "
    "event/number/name/place absent from all the evidence; mark CONTRADICTED only "
    "when the evidence directly refutes it. Output only the JSON object."
)
_NULL_RESULT_PROMPT_PREFIX = (
    "This is a NULL-RESULT / corpus-survey finding: judge whether it is a FAITHFUL "
    "SURVEY of the evidence below (does the evidence set genuinely LACK what the "
    "finding says is absent, and does it accurately characterize what the signals "
    "contain?), rather than whether each clause is individually cited.\n\n"
)


# ---------------------------------------------------------------------------
# V-F (2026-07-31) — CLAIM-SPLITTER HYGIENE: drop NON-PROPOSITIONAL spans.
#
# The live artifact (judge readout 2026-07-31 §6): a claim whose ENTIRE text was
# the literal ``(not_observed)`` reached the verdict ledger as a ``no_citation``
# soft-fail on a Mexico leadership_transition finding. It is a structured-status
# TOKEN the assessor emitted inline, not a proposition — there is nothing for
# either the floor or the judge to grade, and grading it manufactures a defect.
#
# It slipped through because ``_is_fact_asserting`` only requires two 2+-letter
# runs, and ``not_observed`` supplies two ("not", "observed") inside ONE token.
# The rule below is deliberately the narrowest one that catches the artifact: a
# span whose content, once scaffolding / emphasis / citation markers / wrapping
# punctuation are stripped, is a SINGLE whitespace-free token (``not_observed``,
# ``N/A``, ``TBD``, ``—``). A token-with-underscores is an identifier, never a
# sentence; anything with a space is left alone (a real fragment like "not
# observed" keeps today's handling — under-dropping is the cheap error here,
# since a dropped span silently RAISES the score).
#
# Dropped at SPLIT time so BOTH the floor and the judge see the same claim list
# (they share ``_segment_claims``), counted as ``claims_dropped_nonpropositional``
# in the verification block so the class can never silently regrow.
# ---------------------------------------------------------------------------

#: Punctuation / wrapper characters stripped before the single-token test.
_NONPROP_STRIP = "()[]{}<>\"'“”‘’*_`~ \t.,;:!?-–—"


def _is_propositional(claim: str) -> bool:
    """False when a segmented span carries NO proposition to grade (V-F).

    Conservative by construction: only a NON-HEADING span that reduces to ONE
    whitespace-free token is rejected. HEADING-shaped spans (``### Assessment``,
    ``**Drivers**``) are left in the stream untouched — they are already inert for
    both the floor (``_is_fact_asserting``) and the judge (``_is_judgeable_claim``),
    and they carry the preceding-span CONTEXT the W31 unscoped-absence backstop
    reads, so removing them here would move a live detector for no gain.
    Never raises; an empty / whitespace-only span is also non-propositional.
    """
    s = claim.strip()
    if not s:
        return False
    # Q-1(d): literal JSON syntax is never a proposition. A producer with a broken
    # output contract (cross_doc_corroborator ships raw tool preamble + unparsed
    # JSON as a finding body) otherwise puts ``"verdict": "supported",`` into the
    # verdict ledger as a graded CLAIM. Dropped here so the floor and the judge
    # agree, and COUNTED at the call site under its own name.
    if is_json_syntax_claim(s):
        return False
    if (
        s.lstrip().startswith("#")
        or _is_bold_heading(s)
        or _is_backtick_heading(s)
        or _LABELED_SCAFFOLD_RE.match(s)
    ):
        return True
    # Strip list scaffolding, then every citation marker (a marker-only span is
    # already re-attached upstream; a ``token [3]`` span reduces here).
    core = s.lstrip("-*> ").strip()
    core = _REF_MARKER_RE.sub(" ", _CLAIM_MARKER_RE.sub(" ", core)).strip()
    # W4: a span that is STILL a dangling comparison fragment ("Change vs.") —
    # the splitter's abbreviation merge re-joins these wherever a following span
    # exists, so what reaches here is the END-OF-BODY residue, which asserts
    # nothing. Tested BEFORE the punctuation strip (which would eat the trailing
    # period the abbreviation is recognized by) and bounded to short spans, so a
    # real sentence that happens to end "…radars, etc." is never dropped.
    if _NON_TERMINAL_ABBREV_RE.search(core) and len(core.split()) <= 4:
        return False
    core = core.strip(_NONPROP_STRIP)
    if not core:
        return False
    # A single whitespace-free token is a status TOKEN / label, not a sentence.
    return bool(re.search(r"\s", core))


def _segment_claims_with_drops(body: str) -> tuple[list[str], list[str]]:
    """``(claim spans, non-propositional spans dropped)`` — see
    :func:`_segment_claims`. The drop list feeds the V-F receipts counter; it is
    NEVER graded, scored, or persisted as a verdict row."""
    spans = _segment_claims_raw(body)
    kept = [c for c in spans if _is_propositional(c)]
    dropped = [c for c in spans if not _is_propositional(c)]
    return kept, dropped


def _segment_claims(body: str) -> list[str]:
    """Split a finding body into sentence-ish claim spans (deterministic).

    V-F: NON-PROPOSITIONAL spans (a bare ``(not_observed)`` status token) are
    dropped here, so the floor and the judge grade the identical claim list.
    """
    return _segment_claims_with_drops(body)[0]


def _segment_claims_raw(body: str) -> list[str]:
    """The pre-V-F segmentation — every sentence-ish span, hygiene NOT applied."""
    if not body:
        return []
    # C1 (2026-07-03): normalize citation-marker drift (full-width 【N】,
    # parenthesized (57, 87) lists) up front so BOTH the segmentation below and the
    # [N] matching that consumes these spans see ASCII markers.
    body = _normalize_verify_markers(body)
    # P7-F1(2): pull a citation marker that trails a sentence terminator back
    # inside the sentence (``…fighters. [[ref:1]]`` -> ``…fighters [[ref:1]].``)
    # so the split below keeps the marker on the claim it supports instead of
    # orphaning a ``no_citation`` fragment.
    body = _TRAILING_MARKER_PULL_RE.sub(
        lambda m: f" {m.group(3)}{m.group(1)}{m.group(2)}", body
    )
    # W4 (2026-08-02): break out an INLINE markdown heading. The assessors emit
    # ``…measures on Russia.## Key points`` with NO newline, which defeats BOTH
    # the line-based section skip below AND the sentence split (which needs
    # whitespace after the terminator). Two measured consequences: a claim shipped
    # to the ledger with a heading welded to its tail, and — far worse — an
    # ``## Indicators to watch`` heading in that form left its whole
    # forward-looking section UN-SKIPPED, so watch bullets were graded as uncited
    # present facts. Requires 2+ hashes and a following space, so a ``#hashtag``
    # in prose is untouched.
    body = _INLINE_HEADING_RE.sub(r"\n\1", body)
    # Drop everything from a 'watch'-family heading onward — that whole section is
    # forward-looking by construction (the assessor prompt defines it as
    # "developments that would confirm or break this assessment"). C1: recognize a
    # BOLD-only heading (**Indicators to watch**) as well as a '#' markdown heading
    # — the units emit BOTH styles, and the bold form was silently un-skipped, so
    # its forward-looking bullets were scored as uncited present-fact claims.
    lines = body.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        is_heading = (
            line.lstrip().startswith("#")
            or bool(_BOLD_HEADING_RE.match(line))
            # W4: the backtick-wrapped heading style (`Indicators to watch`) —
            # a whole-line code span the assessors also emit as a section label.
            or bool(_BACKTICK_HEADING_RE.match(line))
            # V-H2: the UNDECORATED label ("Indicators to watch:") — the style the
            # assessors actually emit most, and the one the producer half of the
            # system has always recognized.
            or _is_plain_heading(line)
        )
        if is_heading:
            head = line.strip().strip("#*->:` ").lower()
            skipping = any(head.startswith(h) for h in _NON_FACTUAL_HEADINGS)
        if skipping:
            continue
        kept.append(line)
    text = "\n".join(kept)
    raw = _SENTENCE_SPLIT_RE.split(text)
    spans = [c.strip() for c in raw if c.strip()]
    # C1: re-attach a citation-ONLY span to the sentence it trails — the splitter
    # severs "…zones. [21][26]" into "…zones." + "[21][26]", orphaning the markers
    # from the claim they support (the exact style the unit prompts mandate). Merge
    # each pure-marker span back onto the preceding span.
    #
    # W4: the same treatment for a span the splitter severed mid-sentence at a
    # NON-TERMINAL abbreviation — "signal_volume_24h = 101 vs." + "84 last
    # window" was two ledger rows, the first a bare fragment and the second a
    # claim missing its subject. Re-joining is what makes the fragment stop
    # existing; DROPPING it would have thrown away the assertion it leads
    # ("Internal political factions are debating war strategy — ultra-hardliners
    # vs." carries a real, checkable claim).
    merged: list[str] = []
    for span in spans:
        if merged and _CITATION_ONLY_RE.match(span):
            merged[-1] = f"{merged[-1]} {span}"
        elif merged and _NON_TERMINAL_ABBREV_RE.search(merged[-1]):
            merged[-1] = f"{merged[-1]} {span}"
        else:
            merged.append(span)
    return merged


def _markers_in_claim(claim: str, *, subclaim: bool) -> list[int]:
    """The citation markers a claim span carries, per convention.

    ``subclaim=False`` — unit ``[N]`` markers + M14 range markers, sorted +
    deduped (byte-identical to the unit floor's inline computation it replaces).
    ``subclaim=True`` — composition ``[[ref:N]]`` ordinals in first-seen order,
    deduped (byte-identical to the composition floor's loop it replaces).
    Shared by the floors and the P2-4 claim-verdict ledger so the recorded
    markers can never drift from what the floor resolved.
    """
    if subclaim:
        markers: list[int] = []
        for m in _REF_MARKER_RE.finditer(claim):
            n = int(m.group(1))
            if n not in markers:
                markers.append(n)
        return markers
    return sorted(
        {int(m.group(1)) for m in _CLAIM_MARKER_RE.finditer(claim)}
        | _range_markers(claim)
    )


# ---------------------------------------------------------------------------
# W31 (2026-07-28) — the UNSCOPED-ABSENCE backstop (denominator honesty for
# negative claims).
#
# The measured 2026-W31 gold-set class: 5 of 8 sampled downgrades were an
# ABSENCE claim stated as a WORLD claim on a thin-collection desk ("no
# weaponized commodity embargoes" while a fuel embargo was extensively reported
# — just never carried by the desk's sources; "no outages were reported"; "no
# signals report health issues"; "no coordinated narrative is evident";
# "large-scale exercise: not observed"). The findings were FAITHFUL to their
# inputs and wrong about the world, because the prose claimed world scope the
# collection cannot support. The floor deliberately EXEMPTS absence claims from
# citation-presence (the #1 honest-low-risk calibration), so this defect class
# previously carried ZERO deterministic penalty.
#
# The backstop is CONSERVATIVE by construction (a false flag poisons
# faithfulness scores; a missed flag is the cheap error):
#   * only floor-EXEMPT spans are eligible (``_is_fact_asserting`` False) — a
#     counted span ("There is no fuel embargo." → no_citation) is never
#     double-counted;
#   * the strong-absence match must be the claim's MAIN assertion: an explicit
#     strong opener at claim start ("no evidence of", "no observable", "there
#     is no ..."), a bare "No/None ..." opener whose predicate is an absence
#     verb ("... was reported", "... is evident", "... occurred"), or a
#     label-colon verdict tail ("...: not observed");
#   * hedged forms pass ("we assess", "likely", "appears", ...), cited spans
#     pass (evidence-anchored), forward-looking signposts pass, and any
#     collection-scoping language in the claim OR the immediately preceding
#     span passes ("in collected reporting", "this desk's sources", "among
#     monitored ...", the M14 survey shapes, ...). When in doubt, do NOT flag.
#
# Emitted as the SOFT reason ``unscoped_absence_claim`` (an honesty-PHRASING
# defect — the overclaim family — not fabrication), folded into the floor via
# ``_fold_guard_spans`` so a hit is one checkable-but-unsupported claim in the
# pooled score. The span text is the RAW segmented claim, so on the judge path
# the #116c text-dedup keeps the judge authoritative over prose it graded (the
# V3 absence rubric already treats an unbounded/unscoped absence as
# unsupported) — the deterministic backstop bites on the judge-off default.
# The prompt-side fix is the paired commit: every inline unit now carries a
# collection-scoped ABSENCE-CLAIM rule whose recommended phrasings are all in
# the scope lexicon below, so a compliant unit is never flagged.
# ---------------------------------------------------------------------------

_UNSCOPED_ABSENCE = "unscoped_absence_claim"

# Strong absence OPENERS — the claim must START with one (after stripping list
# bullets, bold markers, and a leading BLUF label). Deliberately the assertive
# world-negative shapes only; softer forms ("nothing to suggest", "absence of")
# stay unflagged (conservative direction).
_ABSENCE_STRONG_OPENERS: tuple[str, ...] = (
    "no evidence of",
    "no evidence that",
    "no evidence has",
    "no observable",
    "no observed",
    "no indication",
    "no sign of",
    "no signs of",
    "no signal ",
    "no signals",
    "no report of",
    "no reports of",
    "there is no ",
    "there are no ",
    "there was no ",
    "there were no ",
)

# The bare "No/None ..." opener flags ONLY when its predicate is an absence
# verb — the "no X was reported / is evident / occurred" main-assertion shape.
# A verbless "No confirmed movement of armor." stays unflagged (fragmentary —
# doubt → no flag).
_ABSENCE_ASSERTION_VERB_RE = re.compile(
    r"\b(?:is|are|was|were|has\s+been|have\s+been|had\s+been)\s+"
    r"(?:reported|observed|detected|identified|recorded|documented|noted|found"
    r"|seen|confirmed|corroborated|announced|evident|apparent|visible|underway)\b"
    r"|\b(?:occurred|took\s+place|materiali[sz]ed|emerged|surfaced|exists?)\b"
)

# The label-colon verdict tail ("Large-scale exercise: not observed") — the
# bold-scaffold signpost shape the floor exempts via _LABELED_SCAFFOLD_RE.
# Terminal-anchored so a scoped tail ("...: not observed in collected
# reporting") never matches.
_ABSENCE_VERDICT_TAIL_RE = re.compile(
    r"(?:^|:\s*)(?:not\s+(?:observed|detected|evident|identified|reported)"
    r"|none\s+(?:observed|detected|evident|identified|reported|found))\s*\.?\s*$"
)

# Hedged-assessment markers — a hedged negative is the analyst's calibrated
# inference, not a bald world-fact; it passes (the task's "we assess no" /
# "likely no" class). Substring, lowercased; trailing spaces keep "may " from
# matching "mayor".
_ABSENCE_HEDGE_MARKERS: tuple[str, ...] = (
    "we assess",
    "assessed",
    "assessment",
    "likely",
    "unlikely",
    "appears",
    "appear to",
    "seems",
    "seem to",
    "may ",
    "might ",
    "could ",
    "possibly",
    "probably",
    "if confirmed",
    "cannot be ruled out",
)


_BLUF_LEAD_RE = re.compile(r"^bluf\s*[:—-]\s*")


def _has_collection_scope(text_low: str) -> bool:
    """True when lowercased text carries collection-scoping language — the
    scope lexicon or the M14 survey shape ('none of the 78 signals ...',
    'across the examined ...'), which is corpus-scoped by construction."""
    if any(m in text_low for m in _COLLECTION_SCOPE_MARKERS):
        return True
    return bool(_SURVEY_SHAPE_RE.search(text_low))


def _is_strong_absence_assertion(low: str) -> bool:
    """True when a flattened, lowercased span's MAIN assertion is a strong
    absence claim (see the three shapes in the block comment above)."""
    if low.startswith(_ABSENCE_STRONG_OPENERS):
        return True
    if (low.startswith("no ") or low.startswith("none ")) and not low.startswith(
        ("no fewer", "no less", "no longer", "no doubt", "no single", "no one")
    ):
        return bool(_ABSENCE_ASSERTION_VERB_RE.search(low))
    return bool(_ABSENCE_VERDICT_TAIL_RE.search(low))


def unscoped_absence_spans(body: str) -> list[UnsupportedSpan]:
    """FLAG world-scoped absence claims that carry no collection scoping (W31).

    One soft ``unscoped_absence_claim`` span per hit, text = the RAW segmented
    claim (load-bearing: the judge-path #116c dedup matches on it). Only
    floor-EXEMPT spans are eligible — a span the floor already counts can never
    be double-counted here. Never raises.
    """
    if not body:
        return []
    spans: list[UnsupportedSpan] = []
    prev_low = ""
    for claim in _segment_claims(body):
        stripped = claim.strip().lstrip("#-*> ").strip()
        if not stripped:
            prev_low = ""
            continue
        # Flatten markdown emphasis so "**BLUF:** No ..." / "**X:** not
        # observed" anchor-match like their plain forms.
        low = re.sub(r"[*_`]+", "", stripped).strip().lower()
        this_low = low
        # A floor-counted span (fact-asserting) is already graded — skip.
        if _is_fact_asserting(claim):
            prev_low = this_low
            continue
        # Forward-looking signposts / cited (evidence-anchored) spans pass.
        if _is_forward_looking(low):
            prev_low = this_low
            continue
        if _markers_in_claim(claim, subclaim=False) or _markers_in_claim(
            claim, subclaim=True
        ):
            prev_low = this_low
            continue
        # A BLUF restates the verdict — strip the label so the absence shape
        # anchors; every OTHER synthesis opener ("Assessed:", ...) is hedged
        # by convention and drops via the hedge lexicon below.
        lead = _BLUF_LEAD_RE.sub("", low)
        if not _is_strong_absence_assertion(lead):
            prev_low = this_low
            continue
        if any(m in low for m in _ABSENCE_HEDGE_MARKERS):
            prev_low = this_low
            continue
        # Scoping language in the claim OR the immediately preceding span
        # ("Collection here is thin; no embargo activity is evident.").
        if _has_collection_scope(low) or (prev_low and _has_collection_scope(prev_low)):
            prev_low = this_low
            continue
        spans.append(UnsupportedSpan(text=claim, reason=_UNSCOPED_ABSENCE))
        prev_low = this_low
    return spans


def _deterministic_floor(
    body: str,
    citations: Any,
    finding_confidence: float | None = None,
) -> FaithfulnessReport:
    """The ALWAYS-ON deterministic citation-presence floor.

    A fact-asserting claim is SUPPORTED iff it carries a ``[N]`` marker that
    resolves to a real signal_id in ``citations``; otherwise it is an
    UNSUPPORTED span (``no_citation`` when it carries no marker at all,
    ``unresolved_citation`` when its markers don't resolve to a cited id).

    SELF-DETECTING BRIDGE: when the citations use the composition
    ``[[ref:N]]`` convention (a sub-claim bridge, not a signal bridge) the
    floor delegates to :func:`_deterministic_floor_subclaim`. The ``[N]`` body
    below is left BYTE-FOR-BYTE — ``\\[(\\d+)\\]`` provably never matches
    ``[[ref:5]]`` so the unit path stays inert on a composition and this
    guard is the ONLY change on the load-bearing unit floor.
    """
    if _uses_subclaim_convention(citations):
        return _deterministic_floor_subclaim(body, citations, finding_confidence)

    resolved_ids = _resolved_citation_ids(citations)
    marker_map = _marker_to_signal_id(citations)
    # QW1-B — the ordinals backed by a DESK GROUNDING block rather than a signal.
    # A clause citing one IS resting on real, rendered, captured evidence (the
    # unit's own prior read, the desk's open frames, its baseline band, its
    # standing questions), so it must not score as an ``unresolved_citation``.
    # EMPTY for every pre-QW1-B finding ⇒ this floor stays byte-identical.
    grounding_ords = set(_grounding_ordinals(citations))

    claims = [c for c in _segment_claims(body) if _is_fact_asserting(c)]
    supported = 0
    spans: list[UnsupportedSpan] = []
    verdicts: list[ClaimVerdict] = []
    for claim in claims:
        # M14: fold RANGE markers ``[lo-hi]`` in alongside bare ``[N]`` markers so a
        # corpus-survey clause that cites the whole enumerated set resolves.
        markers = _markers_in_claim(claim, subclaim=False)
        if not markers:
            spans.append(UnsupportedSpan(text=claim, reason="no_citation"))
            verdicts.append(ClaimVerdict.failed(claim, "no_citation"))
            continue
        # Supported when ANY marker resolves to a real cited signal_id — or, from
        # QW1-B, to a cited DESK GROUNDING block (real captured evidence with no
        # signal row behind it).
        ok = any(
            marker_map.get(n) in resolved_ids for n in markers if marker_map.get(n)
        ) or any(n in grounding_ords for n in markers)
        if ok:
            supported += 1
            verdicts.append(ClaimVerdict.supported(claim, list(markers)))
        else:
            spans.append(
                UnsupportedSpan(
                    text=claim, reason="unresolved_citation", markers=markers
                )
            )
            verdicts.append(
                ClaimVerdict.failed(claim, "unresolved_citation", list(markers))
            )

    checkable = len(claims)
    # No checkable factual claims → vacuously faithful (we never invent a defect).
    score = 1.0 if checkable == 0 else supported / checkable
    return FaithfulnessReport(
        faithfulness_score=score,
        checkable_claims=checkable,
        supported_claims=supported,
        unsupported_spans=spans,
        judge_status="deterministic",
        claim_verdicts=verdicts,
        score_denominator=checkable,
    )


def _deterministic_floor_subclaim(
    body: str,
    citations: Any,
    finding_confidence: float | None = None,
) -> FaithfulnessReport:
    """The composition ([[ref:N]] → sub-claim) deterministic floor + T7 guards.

    Mirrors :func:`_deterministic_floor` via the SHARED ``_segment_claims`` /
    ``_is_fact_asserting`` so the honesty is identical: a fact-asserting clause is
    SUPPORTED iff it carries a ``[[ref:N]]`` marker whose ordinal ``N`` is in the
    cited sub-claim set; ``no_citation`` when it carries no marker; an ordinal not
    in the cited set is ``unresolved_citation``; ``faithfulness_score =
    supported/checkable`` (checkable==0 → 1.0 vacuous, same as the unit path).

    T7 (both DB-free, over data captured on the citations at synth time):

      * DOUBLE-COUNTING — cited sub-claims whose ``derived_from`` sets intersect
        are one component (shared underlying lineage/signal). A component with
        >1 member emits an advisory ``double_counted`` span; the evidence ceiling
        is a max over COMPONENTS (each = the max effective_confidence within it),
        so two sub-claims on the same signal count as ONE source, never a sum.

      * HEDGE-LAUNDERING — a fact-asserting clause whose finding confidence
        exceeds the max effective_confidence of the sub-claim(s) it cites (by
        more than ``_HEDGE_EPSILON``) is flagged ``hedge_laundering``. The cap is
        enforced numerically via ``confidence_ceiling`` (the payload folds
        ``overall_score = min(faithfulness_score, confidence_ceiling)``).

    C-TIER (two-tier composition evidence): a fact-asserting clause whose
    resolved citations are ALL ``tier='periphery'`` (below-floor / unverified
    sub-claims the synth quarantined) is SUPPORTED only when the clause itself
    is hedged/attributed (:func:`_is_hedged_attributed`); asserted bald it is a
    COUNTED ``unhedged_periphery_citation`` defect. Citation lists with no
    ``tier`` keys (every pre-C-TIER composition) leave the rule inert.

    HONEST: a citation missing effective_confidence/derived_from is never
    fabricated into a correlation or a cap; a composition citing no resolvable
    sub-claim still floors low (every clause ``no_citation``/``unresolved``),
    never a faked pass.
    """
    # The cited sub-claim ORDINAL set (the deterministic resolution key — an
    # ordinal N ⇒ the Nth rendered sub-claim). Maps keyed by the same ordinal.
    resolved_ords = _resolved_citation_ordinals(citations)
    eff_map = _ordinal_effconf_map(citations)
    derived_map = _ordinal_derived_map(citations)
    source_map = _ordinal_source_map(citations)
    # C-TIER: the periphery-tier ordinals (empty ⇒ the rule below is inert).
    periphery_ords = _periphery_ordinals(citations)

    claims = [c for c in _segment_claims(body) if _is_fact_asserting(c)]
    supported = 0
    spans: list[UnsupportedSpan] = []
    verdicts: list[ClaimVerdict] = []
    for claim in claims:
        markers = _markers_in_claim(claim, subclaim=True)
        if not markers:
            spans.append(UnsupportedSpan(text=claim, reason="no_citation"))
            verdicts.append(ClaimVerdict.failed(claim, "no_citation"))
            continue
        resolved_markers = [n for n in markers if n in resolved_ords]
        if not resolved_markers:
            spans.append(
                UnsupportedSpan(
                    text=claim, reason="unresolved_citation", markers=list(markers)
                )
            )
            verdicts.append(
                ClaimVerdict.failed(claim, "unresolved_citation", list(markers))
            )
            continue
        # C-TIER — a clause resting ONLY on periphery-tier citations (below-
        # floor / unverified sub-claims) is SUPPORTED only when hedged/
        # attributed; asserted bald it is a COUNTED ``unhedged_periphery_
        # citation`` defect (overclaim family). Either way the numeric
        # hedge-laundering comparison below is SKIPPED for such a clause:
        # hedged text IS the tier's remedy (comparing the composition's
        # confidence to a by-definition-below-floor eff would advisory-flag
        # every legal hedged use), and the unhedged case already carries its
        # dedicated, counted reason. A clause with >=1 BASIS citation takes
        # the ordinary path — the basis leg supports it.
        if periphery_ords and all(n in periphery_ords for n in resolved_markers):
            if _is_hedged_attributed(claim):
                supported += 1
                verdicts.append(
                    ClaimVerdict.supported(claim, list(resolved_markers))
                )
            else:
                spans.append(
                    UnsupportedSpan(
                        text=claim,
                        reason=_UNHEDGED_PERIPHERY,
                        markers=list(resolved_markers),
                    )
                )
                verdicts.append(
                    ClaimVerdict.failed(
                        claim, _UNHEDGED_PERIPHERY, list(resolved_markers)
                    )
                )
            continue
        supported += 1
        # P2-4 ledger: the CLAIM is supported; a hedge-laundering hit below stays
        # an ADVISORY span annotating it, never a second ledger row.
        verdicts.append(ClaimVerdict.supported(claim, list(resolved_markers)))
        # HEDGE-LAUNDERING — the clause asserts more than its cited sub-claim(s).
        if finding_confidence is not None:
            cited_effs = [eff_map[n] for n in resolved_markers if n in eff_map]
            if cited_effs and finding_confidence > max(cited_effs) + _HEDGE_EPSILON:
                spans.append(
                    UnsupportedSpan(
                        text=claim,
                        reason="hedge_laundering",
                        markers=list(resolved_markers),
                    )
                )

    checkable = len(claims)
    score = 1.0 if checkable == 0 else supported / checkable

    # DOUBLE-COUNTING + the evidence ceiling over the CITED sub-claims (ordinals).
    cited_ords = sorted(resolved_ords)
    components = _correlated_components(cited_ords, derived_map, source_map)
    rep_effs: list[float] = []
    for comp in components:
        comp_effs = [eff_map[n] for n in comp if n in eff_map]
        if comp_effs:
            rep_effs.append(max(comp_effs))
        if len(comp) > 1:
            spans.append(
                UnsupportedSpan(
                    text="correlated evidence (shared lineage): "
                    + ", ".join(str(n) for n in sorted(comp)),
                    reason="double_counted",
                    markers=sorted(comp),
                )
            )
    # Ceiling = max over INDEPENDENT components (each already the max within it).
    # None when no citation carried effective_confidence — we never invent a cap.
    confidence_ceiling = max(rep_effs) if rep_effs else None

    return FaithfulnessReport(
        faithfulness_score=score,
        checkable_claims=checkable,
        supported_claims=supported,
        unsupported_spans=spans,
        judge_status="deterministic",
        confidence_ceiling=confidence_ceiling,
        claim_verdicts=verdicts,
        score_denominator=checkable,
    )


def _indicator_citation_markers(entry: Mapping[str, Any]) -> list[int]:
    """The real signal-marker INDEXES a structured indicator entry cites.

    A citation is an int index; bool is rejected (a stray ``True`` is not a
    citation index) and non-list shapes contribute nothing — the SAME predicate
    the S3-T1 tally uses, factored so the P2-4 ledger can never drift from it.
    """
    cites = entry.get("citations")
    if not isinstance(cites, (list, tuple)):
        return []
    return [c for c in cites if isinstance(c, int) and not isinstance(c, bool)]


def _indicator_label(entry: Mapping[str, Any]) -> str:
    """The human-facing label for an indicator entry (statement → id → stub)."""
    stmt = entry.get("statement")
    ident = entry.get("id")
    return stmt if isinstance(stmt, str) and stmt.strip() else str(ident or "indicator")


def _indicator_spans(indicators: Any) -> tuple[int, int, list[UnsupportedSpan]]:
    """Faithfulness contribution of the structured ``data.indicators[]`` block (S3-T1).

    Returns ``(checkable, supported, spans)``. Each ``triggered`` indicator is a
    CHECKABLE claim — SUPPORTED iff it carries >=1 citation index, else an
    ``indicator_uncited_triggered`` unsupported span. ``not_observed`` / ``expired``
    are forward-looking and contribute NOTHING (exempt — the same honesty as the
    prose watch-section drop). A malformed / absent block contributes
    ``(0, 0, [])`` — never a fabricated defect.
    """
    checkable = 0
    supported = 0
    spans: list[UnsupportedSpan] = []
    if not isinstance(indicators, (list, tuple)):
        return checkable, supported, spans
    for entry in indicators:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("status") != "triggered":
            continue  # not_observed / expired are forward-looking → exempt
        checkable += 1
        if _indicator_citation_markers(entry):
            supported += 1
        else:
            label = _indicator_label(entry)
            spans.append(
                UnsupportedSpan(
                    text=f"triggered indicator without citation: {label}"[:500],
                    reason=_INDICATOR_UNCITED_TRIGGERED,
                )
            )
    return checkable, supported, spans


def _indicator_claim_verdicts(indicators: Any) -> list[ClaimVerdict]:
    """P2-4 ledger rows for the structured indicator block — one per ``triggered``
    entry, mirroring :func:`_indicator_spans` exactly (same predicate helpers): a
    cited triggered indicator is a SUPPORTED ledger row (previously recorded
    nowhere), an uncited one a soft_fail row whose text matches its span.
    """
    verdicts: list[ClaimVerdict] = []
    if not isinstance(indicators, (list, tuple)):
        return verdicts
    for entry in indicators:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("status") != "triggered":
            continue
        markers = _indicator_citation_markers(entry)
        label = _indicator_label(entry)
        if markers:
            verdicts.append(
                ClaimVerdict.supported(f"triggered indicator: {label}"[:500], list(markers))
            )
        else:
            verdicts.append(
                ClaimVerdict.failed(
                    f"triggered indicator without citation: {label}"[:500],
                    _INDICATOR_UNCITED_TRIGGERED,
                )
            )
    return verdicts


def _fold_indicators(
    floor: FaithfulnessReport, indicators: Any
) -> FaithfulnessReport:
    """Fold the structured-indicator check (S3-T1) into a deterministic floor.

    Applied to the floor BEFORE the optional LLM judge so the judge's
    ``min(floor, judge)`` refinement PRESERVES any indicator demotion (the judge
    only grades prose; it can only tighten, never inflate). When there are no
    ``triggered`` indicators the floor is returned UNCHANGED — byte-identical for
    every finding without a structured indicators block, and for a composition
    (whose payload carries no ``indicators``). The evidence ceiling / judge labels
    carry through untouched.
    """
    ind_checkable, ind_supported, ind_spans = _indicator_spans(indicators)
    if ind_checkable == 0:
        return floor
    checkable = floor.checkable_claims + ind_checkable
    supported = floor.supported_claims + ind_supported
    score = 1.0 if checkable == 0 else supported / checkable
    return FaithfulnessReport(
        faithfulness_score=score,
        checkable_claims=checkable,
        supported_claims=supported,
        unsupported_spans=floor.unsupported_spans + ind_spans,
        judge_status=floor.judge_status,
        judge_unavailable_reason=floor.judge_unavailable_reason,
        confidence_ceiling=floor.confidence_ceiling,
        # P2-4: the indicator rows join the per-claim ledger (supported entries
        # were previously recorded nowhere; failures mirror their spans).
        claim_verdicts=floor.claim_verdicts + _indicator_claim_verdicts(indicators),
        counters=dict(floor.counters),
        score_denominator=checkable,
    )


# ---------------------------------------------------------------------------
# M13 / M15 (2026-07-06) — write/verify-time world-knowledge + target guards
# ---------------------------------------------------------------------------
#
# The faithfulness judge grades CITATION-support, not world-knowledge, so two
# defect classes both the citation floor AND the judge miss:
#
#   M13 STALE-CUTOFF LEADER — an assessor back-fills a current officeholder from a
#     pre-cutoff training prior ("renewed cooperation via FORMER President Trump"
#     while the cited signals establish Trump as the SITTING president).
#   M15 CROSS-TARGET LEAK — a per-country UNIT finding whose named subject-country
#     is the WRONG one (a Turkey desk head titled/bodied entirely "Romania").
#
# Both are cheap LEXICAL backstops that FLAG (add an unsupported span → demote
# effective_confidence via the min(confidence, faithfulness) gate), NEVER delete.
# Kept LOCAL + stdlib-only so verify.py stays slim-image-safe (no runtime import);
# the curated maps deliberately MIRROR their runtime counterparts (the
# legba.runtime.grounding current-officeholder anchor / finding_is_off_target
# gazetteer) — minimal by design (US president only; a small country-token set).

_STALE_LEADER_REASON = "stale_leader"
_CROSS_TARGET_REASON = "cross_target_leak"

# Curated CURRENT officeholders (US president ONLY — the one clear live stale-
# cutoff error; extend only for a NEW confirmed live error). Two stale shapes:
#   * a "former/ex/past ... <current holder>" reference — calling the SITTING
#     holder "former" is always a temporal error;
#   * a predecessor asserted as the CURRENT / sitting holder.
# The qualifier→title separator is ``[-\s]+`` so the HYPHENATED "ex-President
# Trump" matches (``ex`` + ``-`` + ``President``) as well as the spaced forms
# ("former President Trump" / "past President Trump").
_STALE_TRUMP_FORMER_RE = re.compile(
    r"\b(?:former|ex|past|previous)[-\s]+(?:u\.?\s?s\.?\s+)?presidents?\s+"
    r"(?:donald\s+(?:j\.?\s+)?)?trump\b"
    r"|\btrump\b\s*,?\s+(?:the\s+)?(?:former|ex|past|previous)[-\s]+"
    r"(?:u\.?\s?s\.?\s+)?president\b",
    re.IGNORECASE,
)
# A predecessor asserted AS THE CURRENT holder — ONLY explicit current-frame
# shapes. The bare "now/today within N chars" proximity is DELIBERATELY dropped:
# it false-flagged "President Biden, NOW a private citizen" (which correctly says
# Biden is out of office). Two accepted shapes: an explicit "current/sitting/
# incumbent (US) president … Biden", or "President Biden {remains in office | is
# the current/sitting president}".
_STALE_WRONG_POTUS_RE = re.compile(
    r"\b(?:current|sitting|incumbent)\s+(?:u\.?\s?s\.?\s+)?president[^.\n;]{0,32}"
    r"\b(?:joe\s+)?biden\b"
    r"|\bpresident\s+(?:joe\s+)?biden\b[^.\n;]{0,24}"
    r"\b(?:remains?\s+in\s+office|is\s+(?:the\s+)?(?:current|sitting|incumbent)(?:\s+president)?)\b",
    re.IGNORECASE,
)
_STALE_LEADER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_STALE_TRUMP_FORMER_RE,
     "the current US president is Donald Trump, not a 'former' one"),
    (_STALE_WRONG_POTUS_RE,
     "the current US president is Donald Trump, not Biden"),
)


def stale_leader_spans(text: str) -> list[UnsupportedSpan]:
    """FLAG stale-cutoff current-leader errors in ``text`` (M13).

    Curated + US-only + conservative — at most one span per pattern. Never raises.
    """
    if not text:
        return []
    spans: list[UnsupportedSpan] = []
    for regex, label in _STALE_LEADER_PATTERNS:
        m = regex.search(text)
        if m:
            frag = text[max(0, m.start() - 12): m.end() + 12].strip()
            spans.append(
                UnsupportedSpan(
                    text=f"stale current-leader reference — {label} (…{frag}…)"[:400],
                    reason=_STALE_LEADER_REASON,
                )
            )
    return spans


# ---------------------------------------------------------------------------
# E-1 (2026-07-27 sweep rec #2) — the FACTS-RECONCILED officeholder guard.
#
# The M13 heuristic above works off a curated regex pair (model-internal world
# knowledge, US-only). This guard is its data-backed sibling: when a finding
# names a person in an officeholder ROLE for a country ("DRC Prime Minister
# <name>", "President <name> of Venezuela"), probe the CURRENT officeholder
# facts (predicate in the head-of-state / head-of-government / leader-of
# family, superseded_by IS NULL AND valid_until IS NULL) and FLAG a mismatch.
#
# HONESTY CONSTRAINTS (load-bearing):
#   * the seed facts can THEMSELVES be stale (known live: the DRC PM row is
#     wrong upstream) — a mismatch DEMOTES/flags via the existing unsupported-
#     span path, NEVER auto-corrects either side;
#   * the reason is ``stale_leader_vs_facts`` (distinct from the heuristic's
#     ``stale_leader``) so calibration can score the two evidence bases apart;
#   * fail-OPEN everywhere: no current fact row for the claimed office → no
#     flag; a claimed person matching ANY current family officeholder (role
#     confusion, co-office) → no flag; a facts read failure → degrade, no flag.
# ---------------------------------------------------------------------------

_STALE_LEADER_VS_FACTS_REASON = "stale_leader_vs_facts"

# Country alias groups: every surface form the extractor recognizes AND every
# candidate ``facts.subject`` spelling probed (lower()-compared). One tuple per
# country so a match on any surface probes all its spellings. Conservative +
# minimal by design (the seeded-world scope), like the M13/M15 maps above.
_OFFICEHOLDER_COUNTRY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("united states", "united states of america", "america", "usa"),
    ("united kingdom", "great britain", "britain"),
    ("russia", "russian federation"),
    ("china", "people's republic of china"),
    ("india",), ("france",), ("germany",), ("italy",), ("japan",),
    ("canada",), ("mexico",), ("brazil",), ("argentina",), ("australia",),
    ("turkey", "turkiye"), ("iran",), ("israel",), ("ukraine",),
    ("saudi arabia",), ("south korea",), ("north korea",), ("south africa",),
    ("indonesia",), ("pakistan",), ("venezuela",), ("slovenia",),
    ("somalia",), ("sudan",), ("egypt",), ("nigeria",), ("poland",),
    ("spain",), ("netherlands",),
    ("democratic republic of the congo", "dr congo", "drc", "congo-kinshasa"),
)
# Uppercase-only acronym surfaces (NEVER matched case-insensitively — "us" /
# "it" / "in" are ordinary English words) → their alias group.
_OFFICEHOLDER_ACRONYMS: dict[str, tuple[str, ...]] = {
    "US": _OFFICEHOLDER_COUNTRY_GROUPS[0],
    "U.S.": _OFFICEHOLDER_COUNTRY_GROUPS[0],
    "USA": _OFFICEHOLDER_COUNTRY_GROUPS[0],
    "UK": _OFFICEHOLDER_COUNTRY_GROUPS[1],
    "DRC": _OFFICEHOLDER_COUNTRY_GROUPS[-1],
}

# Role surface → the facts predicates whose CURRENT row is the reconciliation
# BASIS (canonical lowercase-spaced forms — vocabulary.PREDICATE_CANONICAL).
# "president" maps to BOTH office predicates: seeds store an executive
# president under 'head of government' where a separate head of state exists
# (e.g. Iran), and under 'head of state' elsewhere.
_OFFICEHOLDER_ROLE_PREDICATES: dict[str, tuple[str, ...]] = {
    "president": ("head of state", "head of government"),
    "prime minister": ("head of government",),
    "chancellor": ("head of government",),
    "premier": ("head of government",),
}
_OFFICEHOLDER_FAMILY_PREDICATES: tuple[str, ...] = (
    "head of state", "head of government",
)
_LEADER_OF_PREDICATE = "leader of"

# A qualifier immediately before the match that makes the phrase NOT a
# current-officeholder claim ("former President X" is correct prose about a
# predecessor; "Vice President X" is a different office).
_OFFICEHOLDER_SKIP_QUALIFIER_RE = re.compile(
    r"(?:former|ex|past|previous|then|outgoing|incoming|late|deputy|vice|"
    r"acting|interim)[-\s]+$",
    re.IGNORECASE,
)


def _officeholder_country_alternation() -> str:
    """The country alternation for the extractor regexes: case-insensitive full
    names (longest-first so 'united states of america' beats 'united states')
    plus the uppercase-only acronym branch."""
    names = sorted(
        {n for grp in _OFFICEHOLDER_COUNTRY_GROUPS for n in grp},
        key=len, reverse=True,
    )
    ci = "|".join(re.escape(n) for n in names)
    acro = "|".join(re.escape(a) for a in _OFFICEHOLDER_ACRONYMS)
    return f"(?:(?i:{ci})|(?:{acro}))"


_OFFICEHOLDER_NAME_RE = r"[A-Z][\w'’.\-]+(?:\s+[A-Z][\w'’.\-]+){0,3}"
_OFFICEHOLDER_ROLE_RE = r"(?i:prime\s+minister|president|chancellor|premier)"
_COUNTRY_ALT = _officeholder_country_alternation()

# "<Country>['s] [current|sitting|incumbent|new] <Role> <Name>"
_OFFICEHOLDER_COUNTRY_FIRST_RE = re.compile(
    rf"\b(?P<country>{_COUNTRY_ALT})(?:['’]s)?\s+"
    rf"(?:(?i:current|sitting|incumbent|new)\s+)?"
    rf"(?P<role>{_OFFICEHOLDER_ROLE_RE})\s+"
    rf"(?P<name>{_OFFICEHOLDER_NAME_RE})"
)
# "<Role> <Name> of [the] <Country>"
_OFFICEHOLDER_ROLE_FIRST_RE = re.compile(
    rf"\b(?P<role>{_OFFICEHOLDER_ROLE_RE})\s+"
    rf"(?P<name>{_OFFICEHOLDER_NAME_RE})\s+of\s+(?:(?i:the)\s+)?"
    rf"(?P<country>{_COUNTRY_ALT})(?![a-z0-9])"
)


@dataclass
class OfficeholderClaim:
    """One extracted "<person> holds <role> for <country>" claim."""

    role: str                       # normalized role key (lowercase, spaced)
    person: str                     # the claimed officeholder, as written
    country_surface: str            # the country as written in the prose
    country_aliases: tuple[str, ...]  # candidate facts.subject spellings (lower)


def _country_aliases_for(surface: str) -> tuple[str, ...]:
    if surface in _OFFICEHOLDER_ACRONYMS:
        return _OFFICEHOLDER_ACRONYMS[surface]
    s = surface.casefold()
    for grp in _OFFICEHOLDER_COUNTRY_GROUPS:
        if s in grp:
            return grp
    return (s,)


def extract_officeholder_claims(text: str) -> list[OfficeholderClaim]:
    """PURE lexical extraction of current-officeholder claims (no DB, no LLM).

    Conservative: only the two explicit shapes; a preceding former/ex/vice/…
    qualifier disqualifies the match (correct prose about a predecessor or a
    different office must never enter the probe). De-duplicated on
    (role, country, person). Never raises."""
    if not text:
        return []
    out: list[OfficeholderClaim] = []
    seen: set[tuple[str, str, str]] = set()
    for regex in (_OFFICEHOLDER_COUNTRY_FIRST_RE, _OFFICEHOLDER_ROLE_FIRST_RE):
        for m in regex.finditer(text):
            window = text[max(0, m.start() - 24):m.start()]
            if _OFFICEHOLDER_SKIP_QUALIFIER_RE.search(window):
                continue
            role = re.sub(r"\s+", " ", m.group("role")).strip().casefold()
            if role not in _OFFICEHOLDER_ROLE_PREDICATES:
                continue  # defensive — the alternation and the map must agree
            surface = m.group("country").strip()
            person = m.group("name").strip()
            aliases = _country_aliases_for(surface)
            key = (role, aliases[0], person.casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append(OfficeholderClaim(
                role=role,
                person=person,
                country_surface=surface,
                country_aliases=aliases,
            ))
    return out


#: Honorifics / particles that carry no identity and must never be the ONLY
#: token two names share ("President Lee" vs "President Kim").
_PERSON_NAME_NOISE: frozenset[str] = frozenset(
    {
        "mr", "mrs", "ms", "dr", "sir", "the", "his", "her", "their",
        "president", "prime", "minister", "chancellor", "premier", "excellency",
        "hon", "rt", "van", "von", "der", "den", "del", "della", "bin", "ibn",
        "abu", "al", "el", "de", "da", "dos", "das", "jr", "sr",
    }
)


def _person_name_tokens(name: str) -> set[str]:
    """Diacritic-folded, casefolded name tokens (len ≥ 3) for tolerant person
    matching — 'Janša' matches 'Jansa', 'Donald J. Trump' matches 'Trump'.

    W4 (2026-08-02): POSSESSIVES are stripped, so "Trump's" matches
    "Donald Trump" — the live ``stale_leader_vs_facts`` false positive was
    exactly that, a genuine officeholder reference in the genitive scoring as a
    stale-leader mismatch because ``'`` is a word character to the splitter.
    Honorifics and name particles are dropped for the same reason a shared
    "president" must not make two different people match.
    """
    norm = unicodedata.normalize("NFKD", name or "")
    norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
    out: set[str] = set()
    for raw in re.split(r"[^\w'’\-]+", norm.casefold()):
        # Genitive forms: "trump's" / "trump’s" / a plural-possessive "harris'".
        token = re.sub(r"['’]s\b", "", raw).strip("'’-")
        if len(token) >= 3 and token not in _PERSON_NAME_NOISE:
            out.add(token)
    return out


_CURRENT_OFFICEHOLDER_SQL = """
    SELECT lower(predicate) AS predicate, subject, value
      FROM facts
     WHERE superseded_by IS NULL
       AND valid_until IS NULL
       AND (
             (lower(subject) = ANY($1::text[])
              AND lower(predicate) = ANY($2::text[]))
          OR (lower(predicate) = $3 AND lower(value) = ANY($1::text[]))
           )
"""

#: At most this many facts-reconciled spans per finding (bounded, skimmable).
_STALE_VS_FACTS_MAX_SPANS = 4
#: At most this many extracted claims probed per finding (bounds the queries).
_STALE_VS_FACTS_MAX_CLAIMS = 8


async def stale_leader_vs_facts_spans(
    conn: Any, text: str,
) -> list[UnsupportedSpan]:
    """FLAG officeholder claims that contradict the CURRENT facts-table row.

    For each extracted claim, reads the country's OPEN officeholder facts
    (country-subject 'head of state'/'head of government' rows + person-subject
    'leader of' rows; ``superseded_by IS NULL AND valid_until IS NULL``) and
    emits a ``stale_leader_vs_facts`` span when the claimed office has a
    current fact naming someone else AND the claimed person matches NO current
    family officeholder. Fail-open + degrade-not-drop throughout; never raises.
    """
    try:
        claims = extract_officeholder_claims(text)
    except Exception as exc:  # pragma: no cover — pure path, defensive only
        logger.warning("verify.stale_leader_vs_facts.extract_failed err=%s", exc)
        return []
    spans: list[UnsupportedSpan] = []
    for claim in claims[:_STALE_VS_FACTS_MAX_CLAIMS]:
        try:
            rows = await conn.fetch(
                _CURRENT_OFFICEHOLDER_SQL,
                list(claim.country_aliases),
                list(_OFFICEHOLDER_FAMILY_PREDICATES),
                _LEADER_OF_PREDICATE,
            )
        except Exception as exc:  # degrade-not-drop — facts read must not block
            logger.warning(
                "verify.stale_leader_vs_facts.read_failed country=%s err=%s",
                claim.country_surface, exc,
            )
            return spans
        role_preds = set(_OFFICEHOLDER_ROLE_PREDICATES[claim.role])
        basis = sorted({
            str(r["value"]) for r in rows
            if r["predicate"] in role_preds and r["value"]
        })
        if not basis:
            continue  # no CURRENT fact for the claimed office — fail-open
        holders = [
            str(r["value"]) for r in rows
            if r["predicate"] in _OFFICEHOLDER_FAMILY_PREDICATES and r["value"]
        ] + [
            str(r["subject"]) for r in rows
            if r["predicate"] == _LEADER_OF_PREDICATE and r["subject"]
        ]
        claimed_tokens = _person_name_tokens(claim.person)
        if not claimed_tokens:
            continue
        if any(claimed_tokens & _person_name_tokens(h) for h in holders):
            continue  # matches a current family officeholder — consistent
        spans.append(UnsupportedSpan(
            text=(
                f"officeholder mismatch vs facts — the finding names "
                f"{claim.person!r} as {claim.role} of {claim.country_surface}, "
                f"but the current open officeholder fact(s) name "
                f"{', '.join(basis[:3])}. Flag-only: the seed facts can "
                f"themselves be stale — never auto-corrected"
            )[:400],
            reason=_STALE_LEADER_VS_FACTS_REASON,
        ))
        if len(spans) >= _STALE_VS_FACTS_MAX_SPANS:
            break
    return spans


def cross_target_leak_span(
    *, title: str, body: str, target_id: str | None,
) -> UnsupportedSpan | None:
    """FLAG a per-country finding whose named subject-country contradicts its desk
    (M15): it names a DIFFERENT country and NEVER its own target geo.

    Conservative fail-OPEN (mirrors :func:`grounding.finding_is_off_target`): a
    finding that mentions its own country anywhere, or that names no country at
    all, is NOT flagged. Non-country / unmapped desks are never flagged."""
    slug = _country_desk_slug(target_id)
    if slug is None:
        return None
    # Build the own-mention set from ONLY the country NAME tokens — NEVER the bare
    # ISO-2 slug. A slug such as 'in' (India), 'it' (Italy), 'us' (US), 'id'
    # (Indonesia) is a common English word that _mentions_country would match in
    # normal prose, firing the on-target early-return on EVERY finding and silently
    # disabling the guard for those desks. Fail-OPEN when the desk has no country-
    # NAME mapping (an unmapped slug): we cannot tell its own country → never flag.
    own = {n.casefold() for n in _TARGET_SLUG_TO_COUNTRY.get(slug, ())}
    if not own:
        return None
    haystack_lc = f"{title}\n{body}".casefold()
    # V-I2 (2026-08-05): three surfaces, not one. The name, its DEMONYM (a US
    # desk writing "American outlets" names its own country), and the
    # CASE-SENSITIVE abbreviations a casefolded set cannot hold — bare "US" is
    # the pronoun "us" once you lower it. 08-04 rec #4, and 100% of the 08-05
    # `cross_target_leak` class was a finding that said "US" six times.
    if _mentions_own_country(slug, own, title, body):
        return None  # on-target — mentions its own geo somewhere
    others = {c for c in _COUNTRY_TOKENS if c not in own}
    # SYMMETRY: the other-country arm reads demonyms too, so the tolerance
    # cannot skew the on-target / off-target decision in one direction only.
    named = sorted(c for c in others if _names_country(c, haystack_lc))
    if not named:
        return None  # names no country at all — generic/thin, not off-target
    return UnsupportedSpan(
        text=(
            f"cross-target leak — desk target '{target_id}' but the finding names "
            f"only other countries ({', '.join(named[:5])}) and never its own"
        )[:400],
        reason=_CROSS_TARGET_REASON,
    )


def _fold_world_knowledge_guards(
    floor: FaithfulnessReport,
    *,
    title: str,
    body: str,
    target_id: str | None,
) -> FaithfulnessReport:
    """Fold the M13 stale-leader + M15 cross-target guards into a floor report.

    Each guard hit is an extra CHECKABLE-but-UNSUPPORTED span (demotes
    faithfulness). Applied to the floor BEFORE the optional judge so the judge's
    reconciliation carries the demotion through (the spans are non-prose,
    non-advisory → counted as residual unsupported). No hit → the floor is
    returned UNCHANGED (byte-identical for callers passing no title/target_id).
    """
    guard_spans = stale_leader_spans(f"{title}\n{body}")
    leak = cross_target_leak_span(title=title, body=body, target_id=target_id)
    if leak is not None:
        guard_spans = guard_spans + [leak]
    return _fold_guard_spans(floor, guard_spans)


def _fold_guard_spans(
    floor: FaithfulnessReport, guard_spans: list[UnsupportedSpan],
) -> FaithfulnessReport:
    """Fold guard-emitted spans (M13/M15/E-1/W31) into a floor report: each is
    an extra CHECKABLE-but-UNSUPPORTED span (demotes faithfulness) plus a failed
    ledger row. Empty spans → the floor is returned UNCHANGED (byte-identical).
    """
    if not guard_spans:
        return floor
    checkable = floor.checkable_claims + len(guard_spans)
    supported = floor.supported_claims
    score = 1.0 if checkable == 0 else supported / checkable
    return FaithfulnessReport(
        faithfulness_score=score,
        checkable_claims=checkable,
        supported_claims=supported,
        unsupported_spans=floor.unsupported_spans + guard_spans,
        judge_status=floor.judge_status,
        judge_unavailable_reason=floor.judge_unavailable_reason,
        confidence_ceiling=floor.confidence_ceiling,
        # P2-4: each guard hit is a checkable-but-failed ledger row (class from
        # the ONE _FAIL_CLASS_BY_REASON table), text mirroring its span.
        claim_verdicts=floor.claim_verdicts
        + [ClaimVerdict.failed(s.text, s.reason, list(s.markers)) for s in guard_spans],
        counters=dict(floor.counters),
        score_denominator=checkable,
    )


# ---------------------------------------------------------------------------
# 2026-07-31 — the DETERMINISTIC CLAIM-OVERRIDE seam (shared by V-C + V-B).
#
# Both structural fixes in this train do the same thing: a claim the citation-
# support branch cannot possibly grade (its truthmaker is a DB COLUMN, or the
# whole INPUT SLICE) is decided DETERMINISTICALLY and its verdict replaced —
# whichever grader produced the original. One mechanism, so the arithmetic is
# written and tested once.
#
# ARITHMETIC CONTRACT (the reason ``score_denominator`` exists):
#   * an override on a claim ALREADY in the ledger moves it between the
#     supported and failed columns and RESCORES over the SAME denominator the
#     path used (floor: ``checkable_claims``; judge: the reconciled
#     ``effective_checkable``) — it never re-bases the score;
#   * an override on a claim the pass did NOT grade (a floor-exempt absence
#     claim on the judge-off path) only ADDS work when it FAILS — it folds in as
#     one new checkable-but-unsupported claim, exactly like ``_fold_guard_spans``.
#     A SUPPORTED verdict on an ungraded claim records its counter and changes
#     NOTHING: we never manufacture a supported claim to lift a score.
#   * ADVISORY spans (double_counted / hedge_laundering) are never dropped — they
#     annotate a claim rather than grade it.
#
# PRECEDENCE CONTRACT (V-G8, 2026-08-03) — WRITE THIS DOWN, because the 08-03
# counter audit spent a section rediscovering it from the data.
#
# An override REPLACES a verdict; it does not relabel one. When a deterministic
# check lands on a claim some earlier stage already decided, the earlier verdict's
# ledger row STOPS EXISTING. That is the intended design — deterministic evidence
# beats an under-evidenced LLM contradiction attempt, and the last override on a
# claim wins — but it has a consequence nobody had stated:
#
#   THE COUNTERS AND THE LEDGER ANSWER DIFFERENT QUESTIONS. A counter fires when
#   a stage ATTEMPTS something. The ledger holds what SURVIVED. Where an override
#   erased an earlier verdict, the two differ BY DESIGN.
#
# The audit measured exactly this: 9 of the 11 rows where ``hardfail_demoted_no_quote``
# fired showed counter-count > surviving-label-count, every one of them a row that
# also carried ``absence_slice_verified`` — the deterministic absence check
# independently verifying the SAME claim the judge had tried and failed to
# hard-fail without a quote, overriding the demoted soft-fail to ``supported``
# outright. Only ~40% of demotion attempts (6 of 15 combined) left a visible
# trace, so a calibration read that mistook the counter for a survival count
# would have been wrong by 60% and had no way to notice.
#
# So the erase now emits its OWN receipt, ``override_erased_<reason>``, and the
# arithmetic closes for every reason R:
#
#     <R's attempt counter>  ==  surviving R rows in claim_verdicts
#                            +   override_erased_R
#
# ANNOTATE-ONLY overrides erase nothing and emit nothing — they carry a finding
# onto an existing row without moving its verdict, which is the whole point of
# that mode.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClaimOverride:
    """One deterministic verdict replacement for a claim, by exact span text."""

    text: str
    # True → VERDICT_SUPPORTED; False → a failure with ``reason``.
    supported: bool
    # The receipts counter this override bumps (always, even when it is a no-op).
    counter: str
    # None for supported; a reason from the ONE _FAIL_CLASS_BY_REASON table.
    reason: str | None = None
    # The human WHY (the real column value / the violating slice title).
    detail: str | None = None
    # W4 (2026-08-02) — ANNOTATE-ONLY: record ``detail`` on the claim's EXISTING
    # ledger row and change nothing else — not the verdict, not the score, not
    # the span set. The V-C anti-laundering gate deliberately refuses to let a
    # matching metadata value certify the prose around it, which left 36 of 38
    # metadata checks invisible: they ran, they matched, and no reader could
    # tell. This carries the finding without carrying the certification.
    annotate_only: bool = False


def _apply_claim_overrides(
    report: FaithfulnessReport, overrides: list[_ClaimOverride]
) -> FaithfulnessReport:
    """Replace the verdicts of the named claims + rescore (see the block above).

    Empty ``overrides`` → the report is returned UNCHANGED (byte-identical), so
    every finding that trips no deterministic check is untouched.

    PRECEDENCE (V-G8, and the block comment above states the full contract): this
    REPLACES a verdict rather than relabelling it, so an earlier stage's ledger
    row stops existing while the counter that recorded its attempt has already
    fired. Each such erase emits ``override_erased_<prior reason>``, which is what
    lets a calibration read reconcile an attempt counter against surviving rows
    instead of silently under-reading the survival rate by ~60%.
    """
    if not overrides:
        return report
    by_text: dict[str, _ClaimOverride] = {o.text.strip(): o for o in overrides}
    counters = dict(report.counters)
    for ov in overrides:
        counters[ov.counter] = counters.get(ov.counter, 0) + 1

    supported = report.supported_claims
    checkable = report.checkable_claims
    denom = (
        report.score_denominator
        if report.score_denominator is not None
        else report.checkable_claims
    )
    applied: dict[str, _ClaimOverride] = {}
    ledger: list[ClaimVerdict] = []
    for cv in report.claim_verdicts:
        key = cv.text.strip()
        ov = by_text.get(key)
        if ov is None:
            ledger.append(cv)
            continue
        if ov.annotate_only:
            # W4: the finding rides on the row, the verdict does not move. Never
            # entered in ``applied``, so the span set is untouched too.
            ledger.append(
                ClaimVerdict(
                    text=cv.text,
                    verdict=cv.verdict,
                    reason=cv.reason,
                    markers=list(cv.markers),
                    detail=ov.detail or cv.detail,
                )
            )
            continue
        applied[key] = ov
        was_supported = cv.verdict == VERDICT_SUPPORTED
        # V-G8 — the ERASE receipt. An override does not relabel a prior verdict,
        # it REPLACES it, so the prior reason's ledger row simply stops existing
        # while the counter that recorded it has already fired. The counter and
        # the ledger then answer two different questions and their arithmetic
        # cannot close (see the PRECEDENCE contract above). Recording the erase
        # closes it: for any reason R,
        #     <R's attempt counter> == surviving R rows + override_erased_R
        if cv.reason and (ov.supported or ov.reason != cv.reason):
            counters[f"override_erased_{cv.reason}"] = (
                counters.get(f"override_erased_{cv.reason}", 0) + 1
            )
        if ov.supported:
            if not was_supported:
                supported += 1
            ledger.append(
                ClaimVerdict(
                    text=cv.text,
                    verdict=VERDICT_SUPPORTED,
                    reason=None,
                    markers=list(cv.markers),
                    detail=ov.detail,
                )
            )
        else:
            if was_supported:
                supported -= 1
            ledger.append(
                ClaimVerdict(
                    text=cv.text,
                    verdict=fail_class_for_reason(str(ov.reason)),
                    reason=ov.reason,
                    markers=list(cv.markers),
                    detail=ov.detail,
                )
            )

    # Spans: drop the stale verdict spans for every claim an override TOUCHED
    # (advisory notes survive — they annotate, they do not grade), then re-emit
    # the failures the overrides decided.
    spans = [
        s
        for s in report.unsupported_spans
        if s.reason in _ADVISORY_REASONS or s.text.strip() not in applied
    ]
    for key, ov in applied.items():
        if ov.supported:
            continue
        markers = next(
            (list(cv.markers) for cv in ledger if cv.text.strip() == key), []
        )
        spans.append(
            UnsupportedSpan(
                text=ov.text,
                reason=str(ov.reason),
                markers=markers,
                detail=ov.detail,
            )
        )

    # Overrides on claims the pass never graded: a FAILURE folds in as new work
    # (one checkable-but-unsupported claim); a SUPPORTED verdict changes nothing.
    for key, ov in by_text.items():
        if key in applied or ov.supported or ov.annotate_only:
            continue
        checkable += 1
        denom += 1
        spans.append(
            UnsupportedSpan(text=ov.text, reason=str(ov.reason), detail=ov.detail)
        )
        ledger.append(
            ClaimVerdict(
                text=ov.text,
                verdict=fail_class_for_reason(str(ov.reason)),
                reason=ov.reason,
                detail=ov.detail,
            )
        )

    score = 1.0 if denom <= 0 else supported / denom
    return FaithfulnessReport(
        faithfulness_score=score,
        checkable_claims=checkable,
        supported_claims=supported,
        unsupported_spans=spans,
        judge_status=report.judge_status,
        judge_unavailable_reason=report.judge_unavailable_reason,
        confidence_ceiling=report.confidence_ceiling,
        branch_scores=report.branch_scores,
        claim_verdicts=ledger,
        counters=counters,
        score_denominator=denom,
    )


# ---------------------------------------------------------------------------
# V-C (2026-07-31) — METADATA CLAIMS: verify by LOOKUP, not exempt blind.
#
# The readout's structural finding #3: a claim quoting the platform's OWN
# metadata — "the unit's effective confidence of 0.68", "these indications are
# below verification thresholds" — is UNJUDGEABLE from evidence text. Its
# truthmaker is a COLUMN the judge never sees, so the judge marks it unsupported
# (or, worse, CONTRADICTED) whether it is right or wrong. Both live artifacts in
# the readout dossier are of exactly this shape.
#
# Exempting the class would hide it. Instead the composition CAPTURED those
# columns on each citation at synth time (``effective_confidence``, C-TIER
# ``tier``) precisely so the verify pass can run DB-free — so we LOOK THEM UP:
#
#   * MATCH  → the claim is SUPPORTED (``metadata_verified``), detail naming the
#              real value it was checked against.
#   * MISMATCH → a soft ``metadata_mismatch`` with the REAL value in the detail.
#              This is a defect class we previously could not see at all: prose
#              MISQUOTING its own numbers. It is emitted wherever it occurs.
#   * columns absent (unit citations name signals, which carry no confidence; a
#              pre-C-TIER composition carries no tier stamps) → today's path,
#              counted ``metadata_unverifiable``. Never a fabricated pass.
#
# ANTI-LAUNDERING (the one asymmetry, deliberate): a matching metadata value only
# lifts the claim to SUPPORTED when the metadata assertion IS the claim — a
# "metadata-dominant" span (see :func:`_metadata_dominant`). A mixed clause that
# also asserts a first-order world fact ("… hint at an absence of observable
# sanctions … but these indications are below verification thresholds") gets its
# metadata leg checked and COUNTED (``metadata_verified_not_dominant``) but keeps
# the grader's verdict — a checkable number must never certify the prose around
# it. Mismatch is NOT gated this way: a misquote is a misquote wherever it sits.
# ---------------------------------------------------------------------------

_METADATA_MISMATCH = "metadata_mismatch"

# "effective confidence of 0.68" / "effective_confidence=0.68" / "confidence: 0.7"
# — the metadata NOUN followed, within a short non-numeric run, by the value. The
# intervening run is CAPTURED (W4) so a COMPARATOR in it is read rather than
# ignored: "confidence >=0.80" was compared for EQUALITY against columns that
# were all >= 0.80 and flagged as a misquote.
_METADATA_CONFIDENCE_RE = re.compile(
    r"\b(?:effective[\s_\-]*)?confidence\b([^0-9\n]{0,24}?)(\d?\.\d{1,3})\b",
    re.IGNORECASE,
)

# W4 (2026-08-02) — the comparators a metadata claim states, longest phrase
# first so "no less than" wins over "less than". The AMBIGUOUS English forms
# ("above" / "over" / "below" / "under") map to the INCLUSIVE comparison on
# purpose: in analyst prose "confidence above 0.7" over a set of 0.70 values is
# the intended reading, and a false MISMATCH here manufactures a soft fail — the
# expensive error. The SYMBOLS stay strict, because a writer who typed ">" meant
# it.
_METADATA_COMPARATORS: tuple[tuple[str, str], ...] = (
    ("no less than", "ge"), ("no lower than", "ge"), ("not less than", "ge"),
    ("no more than", "le"), ("no higher than", "le"), ("not more than", "le"),
    ("greater than or equal to", "ge"), ("less than or equal to", "le"),
    ("at least", "ge"), ("at most", "le"),
    ("or higher", "ge"), ("or above", "ge"), ("or greater", "ge"),
    ("or lower", "le"), ("or below", "le"), ("or less", "le"),
    ("greater than", "gt"), ("less than", "lt"),
    ("exceeding", "gt"), ("exceeds", "gt"),
    ("above", "ge"), ("over", "ge"), ("below", "le"), ("under", "le"),
    (">=", "ge"), ("≥", "ge"), ("<=", "le"), ("≤", "le"),
    (">", "gt"), ("<", "lt"),
)


def _metadata_comparator(between: str) -> str | None:
    """The comparator stated between the metadata noun and its value, or ``None``.

    ``None`` means a bare equality assertion ("effective confidence of 0.68") and
    keeps the pre-W4 exact-match semantics byte-for-byte.
    """
    low = (between or "").lower()
    for phrase, op in _METADATA_COMPARATORS:
        if phrase in low:
            return op
    return None


def _metadata_comparison_holds(op: str, value: float, asserted: float) -> bool:
    """Does one captured column value satisfy the claim's stated comparison?"""
    if op == "ge":
        return value >= asserted
    if op == "gt":
        return value > asserted
    if op == "le":
        return value <= asserted
    if op == "lt":
        return value < asserted
    return False


#: Human rendering of a comparator, for the verdict detail.
_METADATA_COMPARATOR_TEXT = {"ge": ">=", "gt": ">", "le": "<=", "lt": "<"}

# Below-the-verification-floor phrasing + the two C-TIER tier NAMES. All assert
# the SAME column: the cited sub-claim's ``tier`` stamp.
_METADATA_BELOW_FLOOR_RE = re.compile(
    r"\bbelow\s+(?:the\s+)?(?:verification\s+|verify\s+)?"
    r"(?:threshold|thresholds|floor|bar|cut-?off)s?\b"
    r"|\bbelow[-\s]floor\b"
    r"|\bperiphery[-\s]tier\b|\btier\s*[:=]\s*periphery\b"
    r"|\bunverified\s+tier\b",
    re.IGNORECASE,
)
_METADATA_BASIS_TIER_RE = re.compile(
    r"\bbasis[-\s]tier\b|\btier\s*[:=]\s*basis\b", re.IGNORECASE,
)

# Framing / hedge / scaffold words that do NOT count as first-order content when
# testing whether a metadata assertion is the claim's whole point.
_METADATA_FRAME_WORDS: frozenset[str] = frozenset(
    """
    a an the this that these those its it their our unit units finding findings
    read reads claim claims sub subclaim subclaims block blocks value values
    score scores number numbers figure figures level levels of for from with at
    on in to is are was were be been being has have had and or but so as than
    indicates indicate indicating indicated suggests suggest suggesting reflects
    reflect means meaning implies imply carries carry carried reported stated
    shows show shown remains remain stays stay sits sit likely unlikely possible
    probable certain uncertain tentative provisional weak weakly strong strongly
    moderate moderately high low conclusion conclusions confidence effective
    verification verify threshold thresholds floor tier periphery basis
    unverified below above not no only merely just treated treat should would
    which while whereas therefore thus hence given
    """.split()
)


def _marker_metadata_map(
    citations: Any, *, subclaim: bool
) -> dict[int, Mapping[str, Any]]:
    """Map the marker a claim would cite → that citation ENTRY, per convention.

    ``subclaim=True`` keys by the composition sub-claim ORDINAL; ``False`` keys by
    the unit ``[N]`` marker index. The entry is returned whole so the metadata
    lookup reads whichever captured column it needs without a second traversal.
    """
    if subclaim:
        return {
            n: entry
            for n, entry in _build_ordinal_map(
                citations, lambda entry, _n: entry
            ).items()
            if isinstance(entry, Mapping)
        }
    out: dict[int, Mapping[str, Any]] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        marker = entry.get("marker")
        if not isinstance(marker, str):
            continue
        m = _CLAIM_MARKER_RE.search(marker)
        if m:
            out[int(m.group(1))] = entry
    return out


#: The negation vocabulary the V-H3 polarity guard reads. Bag-of-words coverage
#: is blind to negation, and "South Korea IS the target" would be "covered" by a
#: title reading "South Korea — NEITHER target NOR wielder" on words alone.
_NEGATION_TOKENS: frozenset[str] = frozenset(
    {"no", "not", "none", "neither", "nor", "without", "absent", "never", "lacks"}
)


def _has_negation(text: str) -> bool:
    return any(w in _NEGATION_TOKENS for w in re.findall(r"[a-z']+", text.lower()))


def _metadata_dominant(
    claim: str, matched: str, *, residual_evidence: str = ""
) -> bool:
    """Is the matched metadata phrase essentially the WHOLE claim? (anti-laundering)

    Strips scaffolding, citation markers and the matched phrase, then counts the
    words left that are not framing / hedge / metadata vocabulary. Few (≤3) → the
    claim is ABOUT its own metadata and a verified value decides it; more → the
    claim also asserts first-order content the column cannot certify.

    V-H3 (2026-08-04) — the SECOND way a claim can be decided. The gate was
    measured too tight on the stamped day (``metadata_verified_not_dominant``=18
    against ``metadata_verified``=7), and the 08-03 panel's ``soft_fail#6`` shows
    what the residual usually is: the claim "South Korea is neither target nor
    wielder of coercive economic pressure (effective_confidence 0.68)" carries a
    verified metadata leg AND first-order content that is the CITED OUTPUT'S OWN
    TITLE — "South Korea – No coercive economic pressure – neither target nor
    wielder". Refusing that claim is not anti-laundering; the column certified the
    number and the citation certified the rest, which is the whole contract.

    So when ``residual_evidence`` (the text of the citations in scope) COVERS
    every residual word, the claim is decided. Laundering stays barred: coverage
    is bag-of-words and therefore blind to negation, so the residual and its
    covering evidence must also AGREE ON POLARITY — otherwise a claim asserting
    the exact OPPOSITE of its cited title would be "covered" by it word for word.
    With no ``residual_evidence`` the function is byte-identical to the pre-V-H3
    rule, which is what every other caller and the ≤3 fast path still get.
    """
    core = claim.strip().lstrip("#-*> ").strip()
    core = _REF_MARKER_RE.sub(" ", _CLAIM_MARKER_RE.sub(" ", core))
    core = core.replace(matched, " ")
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", core.lower()) if len(w) > 1]
    residual = [w for w in words if w not in _METADATA_FRAME_WORDS]
    if len(residual) <= 3:
        return True
    if not residual_evidence:
        return False
    covering = residual_evidence.lower()
    if not all(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", covering) for w in residual):
        return False
    return _has_negation(core) == _has_negation(residual_evidence)


def _cited_surface(
    entries: Mapping[int, Mapping[str, Any]], scope: list[int]
) -> str:
    """The TEXT of the citations a metadata claim is scoped to (V-H3).

    Title + captured evidence text, concatenated. Never fabricates: an entry with
    neither contributes nothing, which leaves the pre-V-H3 dominance rule in
    force for that claim.
    """
    parts: list[str] = []
    for n in scope:
        entry = entries.get(n) or {}
        for key in ("title", "evidence_text", "snippet"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return " \n ".join(parts)


def _metadata_claim_override(
    claim: str, citations: Any, *, subclaim: bool
) -> _ClaimOverride | None:
    """The V-C verdict for ONE claim, or ``None`` when it asserts no metadata.

    Deterministic + DB-free: everything checked here was captured onto the
    citation at synth time. Never raises.
    """
    entries = _marker_metadata_map(citations, subclaim=subclaim)
    if not entries:
        return None
    markers = _markers_in_claim(claim, subclaim=subclaim)
    # The claim's OWN markers when it carries any; otherwise the whole cited set
    # (a metadata clause routinely states the number without re-citing it).
    scope = [n for n in markers if n in entries] or sorted(entries)

    conf = _METADATA_CONFIDENCE_RE.search(claim)
    if conf is not None:
        try:
            asserted = float(conf.group(2))
        except (TypeError, ValueError):
            asserted = None
        if asserted is None or not (0.0 <= asserted <= 1.0):
            return _ClaimOverride(
                text=claim, supported=True, counter="metadata_unverifiable"
            )
        digits = len(conf.group(2).split(".")[-1])
        op = _metadata_comparator(conf.group(1))
        real: list[float] = []
        for n in scope:
            raw = entries[n].get("effective_confidence")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                real.append(float(raw))
        if not real:
            # The cited outputs carry no confidence column (unit citations name
            # SIGNALS; a legacy composition captured none) — today's path.
            return _ClaimOverride(
                text=claim, supported=True, counter="metadata_unverifiable"
            )
        # W4: an INEQUALITY is a UNIVERSAL claim about the cited set ("these
        # sub-claims carry confidence >= 0.80"), so EVERY captured value must
        # satisfy it; a bare equality keeps the pre-W4 "any value matches" rule.
        if op is None:
            holds = any(round(v, digits) == asserted for v in real)
            asserted_text = f"effective_confidence={asserted:g}"
        else:
            holds = all(
                _metadata_comparison_holds(op, round(v, digits), asserted)
                for v in real
            )
            asserted_text = (
                f"effective_confidence{_METADATA_COMPARATOR_TEXT[op]}{asserted:g}"
            )
        if holds:
            counter = (
                "metadata_verified"
                if _metadata_dominant(
                    claim,
                    conf.group(0),
                    residual_evidence=_cited_surface(entries, scope),
                )
                else "metadata_verified_not_dominant"
            )
            return _ClaimOverride(
                text=claim,
                supported=True,
                counter=counter,
                detail=(
                    f"{asserted_text} holds for the cited output(s) "
                    f"{sorted(scope)} (" + ", ".join(f"{v:g}" for v in real) + ")"
                ),
            )
        return _ClaimOverride(
            text=claim,
            supported=False,
            counter=_METADATA_MISMATCH,
            reason=_METADATA_MISMATCH,
            detail=(
                f"prose asserts {asserted_text}; the cited "
                f"output(s) {sorted(scope)} carry "
                + ", ".join(f"{v:g}" for v in real)
            ),
        )

    below = _METADATA_BELOW_FLOOR_RE.search(claim)
    basis = None if below is not None else _METADATA_BASIS_TIER_RE.search(claim)
    if below is None and basis is None:
        return None
    tiered = [n for n in scope if "tier" in entries[n]]
    if not any("tier" in e for e in entries.values()):
        # No tier stamps anywhere (pre-C-TIER composition / unit path) — we
        # cannot know, so we do not decide.
        return _ClaimOverride(
            text=claim, supported=True, counter="metadata_unverifiable"
        )
    match = below is not None
    asserted_tier = _PERIPHERY_TIER if match else "basis"
    wrong = [
        n
        for n in scope
        if (entries[n].get("tier") == _PERIPHERY_TIER) is not match
    ]
    matched_text = (below or basis).group(0)  # type: ignore[union-attr]
    if not wrong:
        counter = (
            "metadata_verified"
            if _metadata_dominant(
                claim, matched_text, residual_evidence=_cited_surface(entries, scope)
            )
            else "metadata_verified_not_dominant"
        )
        return _ClaimOverride(
            text=claim,
            supported=True,
            counter=counter,
            detail=(
                f"tier={asserted_tier} matches the cited output(s) "
                f"{sorted(scope)}"
            ),
        )
    if not tiered:
        return _ClaimOverride(
            text=claim, supported=True, counter="metadata_unverifiable"
        )
    return _ClaimOverride(
        text=claim,
        supported=False,
        counter=_METADATA_MISMATCH,
        reason=_METADATA_MISMATCH,
        detail=(
            f"prose asserts tier={asserted_tier}; the cited output(s) "
            + ", ".join(
                f"{n}={entries[n].get('tier') or 'basis'}" for n in sorted(wrong)
            )
        ),
    )


def _fold_metadata_claims(
    report: FaithfulnessReport, *, body: str, citations: Any
) -> FaithfulnessReport:
    """Fold the V-C metadata LOOKUP verdicts into a report (both grader paths).

    A finding whose prose asserts no platform metadata produces no overrides →
    the report is returned UNCHANGED.
    """
    if not body:
        return report
    subclaim = _uses_subclaim_convention(citations)
    overrides: list[_ClaimOverride] = []
    for claim in _segment_claims(body):
        if not _is_judgeable_claim(claim):
            continue
        try:
            ov = _metadata_claim_override(claim, citations, subclaim=subclaim)
        except Exception as exc:  # noqa: BLE001 — a lookup must never break verify
            logger.warning("verify.metadata_claim.failed err=%s", exc)
            continue
        if ov is None:
            continue
        # An UNVERIFIABLE metadata claim keeps today's verdict: record only the
        # counter, never a manufactured pass.
        if ov.counter == "metadata_unverifiable":
            report.bump(ov.counter)
            continue
        # W4 — a VERIFIED-but-NOT-DOMINANT metadata leg. The anti-laundering gate
        # stands (a checkable number must never certify the prose around it), so
        # the verdict does not move; the finding is recorded ON the claim's row
        # instead of being counted into invisibility. Reviewed 2026-08-02 against
        # the readout's n=2 note and deliberately NOT loosened: the live split
        # (verified=1 / mismatch=1 / not_dominant=36) says the gate is doing
        # exactly what it was built to do, and every one of those 36 is a mixed
        # clause whose first-order content a column cannot certify.
        if ov.counter == "metadata_verified_not_dominant":
            # NB: the counter is bumped by _apply_claim_overrides, once, for
            # every override it is handed — annotate-only included.
            overrides.append(
                _ClaimOverride(
                    text=ov.text,
                    supported=True,
                    counter=ov.counter,
                    detail=(
                        f"metadata leg checked and holds ({ov.detail}) — the "
                        "verdict is unchanged because the claim also asserts "
                        "first-order content this column cannot certify"
                    ),
                    annotate_only=True,
                )
            )
            continue
        overrides.append(ov)
    return _apply_claim_overrides(report, overrides)


# ---------------------------------------------------------------------------
# V-G5 (2026-08-03) — the MARKERLESS-UNCITED guard: the recurring pass-side miss.
#
# The one gate that is supposed to be ZERO has failed on both acceptance runs,
# and both misses share a shape the judge is instructed to wave through. The
# unit and composition leads both say, in as many words: "A claim with NO [N]
# marker is a synthesis / framing / severity / absence statement — mark it
# SUPPORTED unless it asserts a SPECIFIC fact." That instruction is right for the
# 2,374 markerless claims a stamped day produces, and it is a hole for one class
# inside them.
#
# ``supported#7`` (08-03, internal_stability / country_g20_ar), ``markers=[]``:
#
#     "Given ARGENTINA'S HISTORICAL PROPENSITY FOR COUPS and its ONGOING
#      ECONOMIC CHALLENGES, the combination of elite discord and nascent protest
#      activity pushes the near-term trajectory toward destabilizing."
#
# Neither load-bearing premise appears in any cited row — the citations are a
# Milei/Villarruel rupture, an indigenous land-bill protest, and a week-in-review
# digest. Both are uncited WORLD KNOWLEDGE injected as a premise: the
# uncited-prior-leak shape that forced the world_context RAG rollback. It passed
# clean, and 13 hours earlier the SAME judge soft-failed the byte-similar
# Indonesian shape ("Indonesia's historical low coup incidence") on the SAME
# analyst. The judge is not self-consistent on this claim shape, which is the
# 08-02 "Australia byte-identical shape" defect recurring for the third time.
#
# WHAT THIS IS NOT. It is emphatically NOT "markerless claims fail". Measured
# read-only over the stamped day's 5,338 claim verdicts, 2,374 are markerless and
# pass; 750 of those are fact-asserting citation-support prose, and failing them
# would trade one gate for another and undo the 07-01 fabrication-vs-
# interpretation calibration. The guard is the NARROW premise class: a markerless
# claim resting on a HISTORICAL / STRUCTURAL BASELINE about the world — "historical
# propensity", "base rate", "reference class", "longstanding institutions",
# "track record". That is a factual assertion about the world with a truthmaker
# no cited row contains, and the analyst has no licence to supply it from memory.
# 19 of 5,338 claims (0.36%) match; every sibling of the two adjudicated rows is
# among them, which is the point — the class is now decided the same way every
# time instead of coin-flipped per run.
#
# EXEMPT: a baseline scoped to the EVIDENCE SET rather than the world ("historical
# coup indicators are absent FROM THE EVIDENCE") is a statement about what was
# collected, which is the absence machinery's job and not a world claim at all.
#
# It runs as a deterministic OVERRIDE (the V-C / V-B seam), not a floor guard,
# because a floor span whose text the judge also graded is deduped away by the
# #116c reconciliation — which is exactly how this class has been getting erased.
# ---------------------------------------------------------------------------

#: Its span reason. SOFT and distinct from ``no_citation`` — the defect is not a
#: missing marker on a reported fact, it is a WORLD-KNOWLEDGE premise the cited
#: evidence cannot supply, and calibration must be able to tell them apart.
_UNCITED_WORLD_KNOWLEDGE = "uncited_world_knowledge"

#: Claim flags a V-B content pass must NOT erase, and the counter each one bumps
#: when V-B steps aside for it. Both are ORTHOGONAL to "does a slice row violate
#: this negative": W31 flags the claim's PHRASING (a world-scoped negative on a
#: thin desk), V-G5 its PREMISE (a baseline the analyst supplied from memory).
#: A content pass answers neither question, so it may not clear either flag.
_COEXISTING_FLAG_COUNTERS: dict[str, str] = {
    _UNSCOPED_ABSENCE: "absence_slice_scope_flagged",
    _UNCITED_WORLD_KNOWLEDGE: "absence_slice_premise_flagged",
}

#: A HISTORICAL / STRUCTURAL BASELINE about the world — the load-bearing premise
#: shape. Narrow idioms only; every one of these asserts a fact about how things
#: have been, which no row in a 24-hour signal slice reports.
_WORLD_BASELINE_RE = re.compile(
    r"\bhistorical(?:ly)?\b"
    r"|\blong[-\s]standing\b|\blongstanding\b"
    r"|\btraditionally\b"
    r"|\bchronic(?:ally)?\b"
    r"|\bpropensity\b"
    r"|\btrack\s+record\b"
    r"|\bhistory\s+of\b|\bhas\s+a\s+history\b"
    r"|\bwell[-\s]documented\b"
    r"|\bknown\s+for\b"
    r"|\bendemic\b|\bperennial\b"
    r"|\bpast\s+pattern"
    r"|\bbase\s+rate\b"
    r"|\breference\s+class\b",
    re.IGNORECASE,
)

#: The baseline is about the EVIDENCE SET, not the world — the absence
#: machinery's territory, and no world claim at all.
_EVIDENCE_REFERENT_RE = re.compile(
    r"\b(?:from|in|within|across|among)\s+(?:the\s+)?"
    r"(?:current\s+|available\s+|collected\s+|reviewed\s+|examined\s+)?"
    r"(?:evidence|signals?|reporting|corpus|documents?|sources?|record\s+set)\b",
    re.IGNORECASE,
)


def _is_uncited_world_baseline(claim: str) -> bool:
    """V-G5 — does this markerless claim rest on an UNCITED world baseline?

    Marker-agnostic by design: the caller supplies only claims that carry no
    citation marker at all, which is where the judge's "no marker ⇒ synthesis"
    licence applies and where the truthmaker therefore cannot be checked.
    """
    core = re.sub(r"[*_`]+", "", claim.strip().lstrip("#-*> ").strip())
    if not _WORLD_BASELINE_RE.search(core):
        return False
    # NOTE the exemption is deliberately NOT ``_has_collection_scope``. That
    # lexicon answers a DIFFERENT question — how the claim's NEGATIVE is scoped —
    # and it is generous by design (it matches bare "window"). A claim can be
    # perfectly scoped to the collection window and still open with a world
    # baseline the analyst supplied from memory, which is the shape under test.
    # Only a baseline that names the EVIDENCE SET as its referent is exempt.
    return not _EVIDENCE_REFERENT_RE.search(core)


def _fold_markerless_uncited(
    report: FaithfulnessReport, *, body: str, citations: Any
) -> FaithfulnessReport:
    """Fold the V-G5 markerless-uncited verdicts into a report (both graders).

    A finding whose prose carries no uncited world baseline produces no overrides
    → the report is returned byte-identically, counter map included.
    """
    if not body:
        return report
    subclaim = _uses_subclaim_convention(citations)
    overrides: list[_ClaimOverride] = []
    seen: set[str] = set()
    for claim in _segment_claims(body):
        if not _is_judgeable_claim(claim):
            continue
        if _markers_in_claim(claim, subclaim=subclaim):
            continue  # it cited something; the judge can grade THAT
        key = claim.strip()
        if key in seen or not _is_uncited_world_baseline(claim):
            continue
        seen.add(key)
        overrides.append(
            _ClaimOverride(
                text=claim,
                supported=False,
                counter="claim_markerless_uncited",
                reason=_UNCITED_WORLD_KNOWLEDGE,
                detail=(
                    "the claim carries no citation marker and rests on a "
                    "historical/structural BASELINE about the world — a premise "
                    "no cited row supplies and the analyst may not supply from "
                    "memory (the uncited-prior-leak shape)"
                ),
            )
        )
    if overrides:
        logger.info(
            "verify.markerless_uncited n=%d sample=%r",
            len(overrides), overrides[0].text[:160],
        )
    return _apply_claim_overrides(report, overrides)


class _JudgeVerdictError(RuntimeError):
    """The judge returned a structurally-invalid verdict set — a verdict count
    that does not match the graded claims. Raised so :func:`_maybe_llm_judge`
    fails to the deterministic floor labelled ``judge_error`` (#116d), rather than
    silently zip-truncating to a partial pass that hides ungraded claims."""


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    """Every balanced top-level ``{...}`` block in ``text`` that parses as a JSON
    dict, in order (#116d).

    Fence- and prose-tolerant: a ```` ```json ```` (or bare ```` ``` ````) fence is
    unwrapped first, and leading reasoning prose / trailing text around the object
    are ignored — a reasoning-class judge may emit thinking before the strict-JSON
    verdicts. Returns ``[]`` when nothing parses. The caller picks the object that
    actually carries ``verdicts`` (so a stray brace in prose can't shadow it).
    """
    if not text:
        return []
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*\n?(.*?)```", candidate, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
    objs: list[dict[str, Any]] = []
    i, n = 0, len(candidate)
    while i < n:
        if candidate[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        escaped = False
        end: int | None = None
        j = i
        while j < n:
            ch = candidate[j]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
            j += 1
        if end is None:
            break  # unbalanced tail — nothing complete left to extract
        try:
            obj = json.loads(candidate[i:end])
            if isinstance(obj, dict):
                objs.append(obj)
        except (json.JSONDecodeError, ValueError):
            pass
        i = end
    return objs


#: How much of an earned evidence quote is persisted onto the verdict.
_JUDGE_QUOTE_DETAIL_CHARS = 300

#: verdict -> the counter its hard→soft demotion bumps. THE table: the counter a
#: panel reads and the reason the ledger carries are decided in one place, so the
#: V-G8 fidelity rule holds by construction (a pooled counter that answers a
#: different question than the ledger is how the last one hid).
_DEMOTION_COUNTERS: dict[str, str] = {
    _VERDICT_CONTRADICTED_UNQUOTED: "hardfail_demoted_no_quote",
    _VERDICT_CONTRADICTED_UNREFUTED: "hardfail_demoted_not_refuting",
    _VERDICT_PRIOR_READ_CONFLICT: "hardfail_demoted_prior_read",
    _VERDICT_CONTRADICTED_OFF_SCOPE: "hardfail_demoted_off_denied_scope",
    _VERDICT_QUOTE_CONFIRMS: "hardfail_demoted_quote_confirms",
    _VERDICT_CONTRADICTED_MACHINE_ROW: "hardfail_demoted_machine_row",
    _VERDICT_ROUTE_EXCLUDED: "hardfail_demoted_route_excluded",
}


def _judge_reason(verdict: str) -> str:
    """The span/ledger REASON for one judge verdict — the ONE mapping.

    Shared by ``unsupported_spans`` and ``claim_verdicts`` so the two can never
    disagree about a claim's class again (W2: the ledger arm used to collapse
    both demotion labels back to ``judge_unsupported``).
    """
    if verdict == "contradicted":
        return "judge_contradicted"
    if verdict == _VERDICT_CONTRADICTED_UNQUOTED:
        return _JUDGE_CONTRADICTED_UNQUOTED
    if verdict == _VERDICT_CONTRADICTED_UNREFUTED:
        return _JUDGE_CONTRADICTED_UNREFUTED
    if verdict == _VERDICT_PRIOR_READ_CONFLICT:
        return _JUDGE_PRIOR_READ_CONFLICT
    if verdict == _VERDICT_CONTRADICTED_OFF_SCOPE:
        return _JUDGE_CONTRADICTED_OFF_SCOPE
    if verdict == _VERDICT_QUOTE_CONFIRMS:
        return _JUDGE_QUOTE_CONFIRMS
    if verdict == _VERDICT_CONTRADICTED_MACHINE_ROW:
        return _JUDGE_CONTRADICTED_MACHINE_ROW
    if verdict == _VERDICT_ROUTE_EXCLUDED:
        return _JUDGE_CONTRADICTED_ROUTE_EXCLUDED
    return "judge_unsupported"


def _judge_detail(verdict: str, quote: str) -> str | None:
    """The persisted WHY for a judge verdict — the earned evidence quote (W2).

    A hard fail that cannot show its refutation is a hard fail nobody can audit:
    the quote was computed, used for the severity decision, and thrown away.
    ``None`` for every verdict that carries no earned quote, so the ledger row is
    byte-identical for them.
    """
    if not quote:
        return None
    span = re.sub(r"\s+", " ", str(quote)).strip()[:_JUDGE_QUOTE_DETAIL_CHARS]
    if verdict == _VERDICT_CONTRADICTED_UNREFUTED:
        return (
            "the judge's evidence span RESOLVES the claim's subject without "
            f"refuting it, so the hard class was not earned: {span!r}"
        )
    if verdict == _VERDICT_PRIOR_READ_CONFLICT:
        return (
            "this read CONFLICTS with an analyst finding the claim does not cite "
            "(typically this desk's own prior read) rather than with source "
            f"reporting — an update, not a misstatement of evidence: {span!r}"
        )
    if verdict == _VERDICT_CONTRADICTED_OFF_SCOPE:
        return (
            "the claim ENUMERATED what it denies and the judge's evidence span "
            "names none of those things in full, so it evidences something the "
            f"claim never denied: {span!r}"
        )
    if verdict == _VERDICT_QUOTE_CONFIRMS:
        return (
            "the judge's evidence span states the claim's OWN numbers back to "
            "it under numeral/unit normalization ('16' for 'sixteen'), every "
            "pinned clock/date endpoint matching and no prose direction "
            f"opposing — it CONFIRMS and cannot be what refutes it: {span!r}"
        )
    if verdict == _VERDICT_CONTRADICTED_MACHINE_ROW:
        return (
            "the judge's evidence span resolves ONLY inside a GDELT/CAMEO "
            "machine-coded event record — a machine's reading of an article, "
            "not the article's own words, and the class the V-B route already "
            f"excludes: {span!r}"
        )
    if verdict == _VERDICT_ROUTE_EXCLUDED:
        return (
            "the V-B router had already routed this claim OUT of slice checking "
            "as a continuity / volume / trajectory read; one claim cannot have "
            f"two authorities, so the hard class was not available: {span!r}"
        )
    return f"contradicted by a verbatim evidence span: {span!r}"


async def _maybe_llm_judge(
    floor: FaithfulnessReport,
    *,
    body: str,
    citations: Any,
    judge_llm: Any | None,
    judge_prompt_profile: str | None = None,
) -> FaithfulnessReport:
    """Optionally refine the floor with an LLM judge (flag-gated, soft-fail).

    Engages ONLY when ``_llm_judge_enabled()`` AND a ``judge_llm`` handler is
    supplied.  The judge re-grades each claim "does this follow from its cited
    evidence?" → supported / unsupported / contradicted.  ANY error, an absent
    handler, or the flag being off → return the deterministic floor LABELLED
    ``judge-unavailable`` (``judge_status`` stays ``'deterministic'``).  NEVER
    fabricates a score. ``judge_prompt_profile`` (P2-4, default ``current`` via
    env fallback) selects the generic judge system prompt's independence
    posture; ``current`` is byte-identical to the pre-P2-4 prompt.
    """
    if not _llm_judge_enabled():
        floor.judge_unavailable_reason = "flag_off"
        return floor
    if judge_llm is None:
        floor.judge_unavailable_reason = "no_judge_component"
        return floor
    try:
        # The judge resolves through the EXISTING component machinery (the
        # caller passes a resolved ``LLMHandlerLike`` — same shape the critic
        # uses).  We keep the prompt here but the score refinement is the
        # caller's contract surface; on any transport error we soft-fail to the
        # floor.  (The judge component is whatever the P2-4 judge ROUTE
        # resolved at the call site — a REGISTERED component, not hardcoded
        # here; today the core producer plane.)
        verdicts, branch_scores = await _run_judge(
            judge_llm,
            body=body,
            citations=citations,
            judge_prompt_profile=judge_prompt_profile,
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail, never break the run
        logger.warning("verify.faithfulness.judge_failed err=%s", exc)
        floor.judge_unavailable_reason = "judge_error"
        return floor

    if not verdicts:
        floor.judge_unavailable_reason = "judge_empty"
        return floor

    # A3 (2026-07-31) — the CITATION-LESS GUARD. Readout structural finding #1:
    # findings with no resolvable citations array fail at 26.3% vs a 6.1% base
    # rate — a zero-citation finding's absence claims are structurally unpassable,
    # because the judge is handed an EMPTY evidence map and every claim reads as
    # ungrounded. Two producer classes ship 100% citation-less and ~5-12% of the
    # rest ship an empty array, some legitimately and some defectively. The
    # producer-side fixes are V-A; THIS is the guard that makes the class visible
    # so it can never silently regrow: whenever the judge grades a finding whose
    # citations resolve to nothing, log it and count ``citationless_graded``.
    # Counts + logs only — it changes no verdict.
    #
    # W3 (2026-08-02) — the guard was read as UNDER-FIRING ~12x for
    # economic_coercion / energy_security / proliferation_watch, on the strength
    # of a DB projection showing 2,752 rows with a bare-ordinal ``data.evidence``
    # array and zero ``data.citations``. Traced read-only against the live
    # substrate: that is a PROJECTION ARTIFACT, not a producer defect.
    # ``analyst_outputs.data`` stores the WHOLE serialized FindingPayload
    # (title/body/confidence/evidence/tags/data/kind_marker), so a finding's own
    # ``data['citations']`` sits one level down at ``data->'data'->'citations'``
    # — where it is populated on 94% of those very rows (1049/1112, 364/389,
    # 1171/1251). ``FindingPayload.evidence`` is the separate top-level
    # evidence-IDENTIFIER field, and the verify path never reads it: the caller
    # passes ``finding_payload.data['citations']``, the inner dict, so citations
    # resolve normally. The guard's measured 8.3% firing rate matches the REAL
    # no-resolvable-citation rate of 6.6% (181/2752). It is honest.
    #
    # So the fix is not to teach resolution a new shape — there is no new shape —
    # but to make the class diagnosable FROM RECEIPTS, so the next audit reads the
    # counters instead of guessing at a JSONB path.
    subclaim_convention = _uses_subclaim_convention(citations)
    _resolved = (
        _resolved_citation_ordinals(citations)
        if subclaim_convention
        else _resolved_citation_ids(citations)
    )
    if not _resolved:
        floor.bump("citationless_graded")
        # WHICH citationless shape: no citations array at all (the producer
        # emitted none) vs an array whose entries resolve to nothing (a bridge
        # that ran and produced no usable ids). Different defects, different
        # owners — and neither is legible from the pooled counter alone.
        has_entries = isinstance(citations, (list, tuple)) and any(
            isinstance(e, Mapping) for e in citations
        )
        shape = "citations_unresolvable" if has_entries else "citations_absent"
        floor.bump(shape)
        logger.warning(
            "verify.judge.citationless_graded claims=%d shape=%s convention=%s "
            "— the judge graded a finding with NO resolvable citations (its "
            "evidence map is empty; this class fails at ~4x the base rate)",
            len(verdicts),
            shape,
            "subclaim" if subclaim_convention else "marker",
        )

    # Refine: when the LLM judge RAN, its per-claim verdicts are AUTHORITATIVE.
    # The judge grades EVERY prose span (via _is_judgeable_claim — INCLUDING the
    # BLUF / synthesis / absence spans the mechanical floor exempts, per the H1
    # fix) against each cited signal's TITLE+SNIPPET with a calibrated
    # fabrication-vs-analysis prompt, so it is the better grader. C1 (2026-07-03):
    # we DO NOT min(floor, judge). The deterministic floor's citation-PRESENCE
    # heuristic mis-scores well-cited findings to 0 (it severs a citation placed
    # after the period, mis-reads a bold watch section), and min() then discarded a
    # healthy judge verdict — silently flooring ~1-in-8 findings to 0 and dropping
    # them from composition + alerting. The floor is the FALLBACK for the
    # judge-UNAVAILABLE path (returned early above), NOT a co-veto; a claim the
    # judge marks unsupported/contradicted is still added as an unsupported span.
    judged_spans: list[UnsupportedSpan] = []
    judged_texts: set[str] = set()
    supported = 0
    # V-I (2026-08-05): ONE table, one accumulator. The five demotion classes had
    # five int locals and five conditional dict-merges at the bottom of this
    # function, which is how the sixth would have been forgotten. Emitted only
    # when non-zero, exactly as before — every pre-V-I counter map is unchanged.
    demotions: dict[str, int] = {}
    for claim_text, verdict, quote in verdicts:
        judged_texts.add(claim_text.strip())
        if verdict == "supported":
            supported += 1
        else:
            reason = _judge_reason(verdict)
            counter = _DEMOTION_COUNTERS.get(verdict)
            if counter is not None:
                demotions[counter] = demotions.get(counter, 0) + 1
            judged_spans.append(
                UnsupportedSpan(
                    text=claim_text,
                    reason=reason,
                    detail=_judge_detail(verdict, quote),
                )
            )
    checkable = len(verdicts)
    judge_score = 1.0 if checkable == 0 else supported / checkable

    # (#116c) Reconcile the surfaced span set so supported + unsupported ≤
    # checkable on the 'llm' path:
    #   * ADVISORY floor spans (double_counted / hedge_laundering) are structural
    #     notes, NOT unsupported claims — surface them but keep them OUT of the
    #     checkable tally (a hedge span can even sit on a SUPPORTED clause, so
    #     counting it double-counts).
    #   * a floor span whose claim text the judge ALSO graded is the SAME clause
    #     seen twice (the judge's verdict is authoritative on this path) — DEDUP
    #     it by text so the concatenation can't push supported + unsupported past
    #     checkable.
    advisory_spans = [
        s for s in floor.unsupported_spans if s.reason in _ADVISORY_REASONS
    ]
    residual_floor_spans = [
        s
        for s in floor.unsupported_spans
        if s.reason not in _ADVISORY_REASONS and s.text.strip() not in judged_texts
    ]
    # C1 (2026-07-03): the judge is authoritative over the PROSE it graded (no
    # min() co-veto with the buggy citation-presence floor), BUT a residual
    # non-advisory floor span the judge could NOT grade is a real, judge-blind
    # defect that must still count — notably an uncited 'triggered' STRUCTURED
    # indicator (S3-T1), which is not prose. Fold each such span in as an extra
    # unsupported checkable item so a clean prose pass cannot erase it.
    effective_checkable = checkable + len(residual_floor_spans)
    refined_score = (
        1.0 if effective_checkable == 0 else supported / effective_checkable
    )
    # P2-4 — the per-claim LEDGER on the judge path: the judge's verdicts are
    # authoritative for every prose span it graded (supported rows included —
    # previously recorded nowhere); floor ledger rows whose text the judge did
    # NOT grade carry over unchanged (structured-indicator rows, world-knowledge
    # guard hits — the same dedup-by-text rule the span reconciliation uses).
    # NOTE: carried SUPPORTED non-prose rows are provenance, not arithmetic —
    # the headline tallies above are untouched.
    subclaim = _uses_subclaim_convention(citations)
    judge_ledger: list[ClaimVerdict] = []
    for claim_text, verdict, quote in verdicts:
        markers = _markers_in_claim(claim_text, subclaim=subclaim)
        if verdict == "supported":
            judge_ledger.append(ClaimVerdict.supported(claim_text, list(markers)))
        else:
            # W2: the SAME ``_judge_reason`` the span above uses. This branch
            # previously collapsed both demotion classes back to
            # ``judge_unsupported``, so the label survived in unsupported_spans
            # and vanished from claim_verdicts — the calibration loop reads the
            # LEDGER, and so could not split the class it was built to measure.
            judge_ledger.append(
                ClaimVerdict.failed(
                    claim_text,
                    _judge_reason(verdict),
                    list(markers),
                    detail=_judge_detail(verdict, quote),
                )
            )
    carried_ledger = [
        cv for cv in floor.claim_verdicts if cv.text.strip() not in judged_texts
    ]
    return FaithfulnessReport(
        faithfulness_score=refined_score,
        checkable_claims=max(floor.checkable_claims, effective_checkable),
        supported_claims=supported,
        # The judge's semantic spans + any floor structural span the judge did NOT
        # re-grade + the advisory (uncounted) notes.
        unsupported_spans=judged_spans + residual_floor_spans + advisory_spans,
        judge_status="llm",
        # Carry the floor's T7 evidence ceiling through — the judge only refines
        # the faithfulness number, never the double-count-corrected cap.
        confidence_ceiling=floor.confidence_ceiling,
        # V3: the per-claim-KIND sub-scores from this judge run (design §2.3),
        # recorded on the report so the payload can surface branch_scores /
        # branch_versions. Empty on the deterministic path (never reached here)
        # and on the M14 whole-finding survey path (one rubric, no partition).
        branch_scores=branch_scores,
        claim_verdicts=judge_ledger + carried_ledger,
        # The floor's receipts counters carry through the judge refinement (they
        # record deterministic work already done, not a judge verdict), plus the
        # V-D / W2 hard→soft demotions this judge run produced.
        counters={**floor.counters, **demotions},
        score_denominator=effective_checkable,
    )


_GENERIC_JUDGE_SYSTEM = (
    "You are a faithfulness judge distinguishing FABRICATION from analysis. "
    "A claim is SUPPORTED when its factual content is consistent with and "
    "grounded in the cited evidence — reasonable analytical interpretation, "
    "framing, aggregation, severity/risk judgement, and negative reads (e.g. "
    "'this is routine, not escalation') ARE permitted and count as supported. "
    "Mark UNSUPPORTED only when the claim asserts a SPECIFIC fact the cited "
    "evidence does not contain (an invented event, number, name, or place); "
    "mark CONTRADICTED only when the evidence directly refutes it. Do NOT "
    "penalize a claim merely for adding interpretation to a supported fact. "
    "Output only the JSON object."
)

# P2-4 — the INDEPENDENCE-POSTURE variant (profile ``independent``), STAGED NOT
# LIVE (default profile is ``current``; flipping is a measured, operator-gated
# step, first A/B'd via scripts/temp_ab_replay.py --judge-profile). Same
# calibrated verdict rubric as _GENERIC_JUDGE_SYSTEM — supported/unsupported/
# contradicted definitions are kept materially identical so an A/B isolates the
# POSTURE variable — but the framing is an adversarial INDEPENDENT reviewer of
# ANOTHER analyst's claims, never "check your own work": the judge is told it
# did not write the claims, owes them no deference, and must ground every
# verdict in the shown evidence rather than its own knowledge or charity.
_INDEPENDENT_JUDGE_SYSTEM = (
    "You are an INDEPENDENT faithfulness reviewer. The claims below were "
    "written by ANOTHER analyst — you did not write them, you know nothing "
    "about that analyst, and you owe their work no deference. You are the "
    "adversarial second reader: your job is to find where the analyst's prose "
    "outruns or misstates the cited evidence, and your ONLY ground truth is "
    "the evidence shown to you — never your own background knowledge, and "
    "never the assumption that the analyst 'must have had a reason'. "
    "A claim is SUPPORTED when its factual content is consistent with and "
    "grounded in the cited evidence — reasonable analytical interpretation, "
    "framing, aggregation, severity/risk judgement, and negative reads (e.g. "
    "'this is routine, not escalation') ARE permitted and count as supported. "
    "Mark UNSUPPORTED only when the claim asserts a SPECIFIC fact the cited "
    "evidence does not contain (an invented event, number, name, or place); "
    "mark CONTRADICTED only when the evidence directly refutes it. Do NOT "
    "penalize a claim merely for adding interpretation to a supported fact, "
    "and do NOT rescue an unsupported claim with facts you happen to know. "
    "Output only the JSON object."
)


def _generic_judge_system(profile: str | None = None) -> str:
    """The generic judge system prompt for a resolved prompt profile.

    ``current`` (default) → the calibrated live prompt, byte-identical;
    ``independent`` → the staged adversarial-reviewer variant. The specialized
    M14 survey / V3 absence rubrics are deliberately NOT profile-switched.
    """
    if _judge_prompt_profile(profile) == JUDGE_PROFILE_INDEPENDENT:
        return _INDEPENDENT_JUDGE_SYSTEM
    return _GENERIC_JUDGE_SYSTEM


async def _judge_claim_partition(
    judge_llm: Any,
    *,
    claims: list[str],
    evidence_prompt: str,
    system: str,
) -> list[tuple[str, str]]:
    """Send ONE partition of claims to the judge; return ``[(verdict, quote)]``.

    Factored out of :func:`_run_judge` so the V3 absence partition AND the M14
    whole-finding survey call reuse the identical call + parse machinery with
    their OWN system prompts (design §3.5). A malformed / empty response yields
    ``[]``; a length mismatch raises :class:`_JudgeVerdictError` (the
    ONE-verdict-per-claim honesty contract from #116d). ``evidence_prompt`` is
    the per-branch user message already carrying the evidence map + numbered
    claim list.

    V-D: ``quotes`` is the OPTIONAL parallel array of verbatim evidence spans —
    ``""`` for every entry a judge omits, a wrong-length array ignored wholesale
    (a misaligned quote is worse than none). Resolution against the shown
    evidence is the CALLER's job (it holds the evidence map).
    """
    response = await judge_llm.chat_complete(
        [{"role": "user", "content": evidence_prompt}],
        # 2026-07-01: the faithfulness judge runs on the SAME core reasoning model
        # as generation (llm.primary.openai_compat), because the 8B cross-family
        # judge proved too weak — harsh + mis-aimed (see the composition
        # shake-down). Matched to the main model's budget: a reasoning-class model
        # may emit thinking before the strict-JSON verdicts, so a 512 cap would
        # truncate the JSON → empty parse → soft-fail to floor. NOTE: same-family
        # removes cross-family independence — a DOCUMENTED LIMITATION, pending a
        # dedicated reasoning judge model (a self-verifying model shares blind
        # spots with the generator; the deterministic citation floor + the
        # provenance chain still backstop it).
        max_tokens=16384,
        temperature=0.0,
        system=system,
    )
    content = getattr(response, "content", "") or ""
    # (#116d) Fence/prose-tolerant parse: a reasoning-class judge may wrap the
    # verdicts in a ```json fence or emit thinking around them, so scan for the
    # object that actually carries ``verdicts`` instead of a fence-intolerant
    # ``strip('`')`` that fails on ``` ```json\n{...}\n``` ```.
    parsed = next(
        (o for o in _extract_json_objects(content) if "verdicts" in o), None
    )
    if parsed is None:
        return []  # no parseable verdict object → soft-fail to floor (judge_empty)
    raw = parsed.get("verdicts")
    if not isinstance(raw, list):
        return []
    # (#116d) HONEST length contract: the judge MUST return one verdict per graded
    # claim. A short/long list previously zip-truncated to the shorter, silently
    # passing the ungraded tail — fail to the floor labelled judge_error instead.
    if len(raw) != len(claims):
        raise _JudgeVerdictError(
            f"judge returned {len(raw)} verdicts for {len(claims)} claims"
        )
    # V-D: the parallel quote array. A judge that omits it, or returns a
    # misaligned one, yields empty quotes — every contradiction then demotes
    # rather than risking a quote attached to the wrong claim.
    quotes_raw = parsed.get("quotes")
    quotes: list[str] = (
        [q if isinstance(q, str) else "" for q in quotes_raw]
        if isinstance(quotes_raw, list) and len(quotes_raw) == len(claims)
        else [""] * len(claims)
    )
    out: list[tuple[str, str]] = []
    for verdict, quote in zip(raw, quotes):
        v = str(verdict).strip().lower()
        if v not in ("supported", "unsupported", "contradicted"):
            v = "unsupported"
        out.append((v, quote))
    return out


async def _run_judge(
    judge_llm: Any,
    *,
    body: str,
    citations: Any,
    judge_prompt_profile: str | None = None,
) -> tuple[list[tuple[str, str, str]], dict[str, dict[str, int | float]]]:
    """Call the judge LLM; return ``([(claim, verdict, quote), ...], branch_scores)``.

    ``verdict`` ∈ {supported, unsupported, contradicted, contradicted_unquoted,
    contradicted_unrefuted, prior_read_conflict}, in ORIGINAL claim order.
    ``branch_scores`` maps each claim-kind that was JUDGED to
    ``{"checkable", "supported", "score"}`` (design §2.3 telemetry).

    V-G1 (2026-08-03): ``prior_read_conflict`` is the third demotion class — the
    quote is verbatim evidence, but evidence that is another FINDING the claim
    never cited (see the SIGNALS-ONLY REFUTATION block). Only source reporting,
    or an entry the claim itself cites, can carry the hard class.

    V-D: ``contradicted_unquoted`` is a ``contradicted`` verdict whose evidence
    QUOTE did not resolve — the judge either omitted it or wrote something that is
    not a verbatim run of the evidence THIS partition was shown. Substituted here,
    at the only place that holds both the evidence map and the raw response; the
    caller maps it to the soft ``judge_contradicted_unquoted`` reason. The claim
    still FAILS either way — only the severity label moves.

    W2 (2026-08-02): ``contradicted_unrefuted`` is the sibling class — the quote
    RESOLVES but does not REFUTE (see :func:`_quote_refutes`) — and ``quote`` is
    the EARNED verbatim span, returned so the caller can persist it. It was
    previously used transiently and discarded, which left 25 of 54 surviving
    hard fails with no recorded proof of anything. ``""`` for every verdict that
    is not a resolvable contradiction.

    ROUTING (M14 first, then the V3 partition — the two never compete):

    * **M14 whole-finding survey** — a corpus-negative / survey finding
      (``_is_null_result_finding``: ≤1 positive claim) keeps the tip behaviour
      byte-for-byte: ONE call grading the WHOLE claim list under the survey
      rubric. ``branch_scores`` is ``{}`` there — one rubric graded everything,
      so per-branch attribution would be fabricated.
    * **V3 per-claim partition (MP:DEC-E)** — otherwise the graded claims are
      PARTITIONED by claim kind: the ``absence`` partition goes to a dedicated
      judge call with the negative-claim rubric (design §3.4) — a deterministic
      route to a constrained prompt removes the free-latitude that produced the
      0.0/0.2/1.0 variance on identical absence prose — and every OTHER kind
      rides the existing unit/composition prompt VERBATIM. Verdicts are re-zipped
      in the original span order. This is the embedded-absence-in-a-fact-rich-
      finding case M14 misses (design §3.2 #4 / §3.5). When a finding carries NO
      absence claim, EXACTLY ONE judge call is made with the byte-identical
      existing prompt — the headline arithmetic and the judge-call count are
      unchanged (the pooled-ratio invariant, design §2.3 point 3).

    Kept deliberately thin — a malformed / empty response yields ``[]`` (the
    caller then soft-fails to the floor). This is the ONLY place that talks to
    the judge model; tests mock ``judge_llm.chat_complete``.

    P2-4: ``judge_prompt_profile`` selects the GENERIC system prompt's
    independence posture (``current`` default = byte-identical live prompt;
    ``independent`` = the staged adversarial-reviewer variant). The M14 survey
    and V3 absence rubrics are profile-invariant by design.
    """
    import json

    generic_system = _generic_judge_system(judge_prompt_profile)

    # H1: the judge grades EVERY prose span — including the BLUF / synthesis /
    # absence claims the FLOOR exempts — so a fabricated uncited claim can't hide
    # in a vacuous checkable=0. The floor still exempts (see _is_fact_asserting);
    # the judge distinguishes faithful synthesis from invented fact via its prompt.
    claims = [c for c in _segment_claims(body) if _is_judgeable_claim(c)]
    if not claims:
        return [], {}
    # M14: a corpus-negative / survey finding is graded with the NULL-RESULT
    # rubric (survey faithfulness) rather than the per-clause citation rubric, so
    # an honest un-citable NEGATIVE isn't scored like a fabrication.
    null_result = _is_null_result_finding(body)
    if _uses_subclaim_convention(citations):
        # COMPOSITION branch — evidence is the CITED SUB-CLAIM's own text, keyed
        # by the ordinal the clause cites via [[ref:N]]. The lead is shared by
        # the M14 whole-finding call and the V3 citation_support partition; each
        # appends its own numbered claim list.
        evidence = _ordinal_evidence_map(citations)
        # C-TIER (former SEAMS §45): the ADDITIVE tier-aware rubric — non-empty
        # ONLY when the citation list carries periphery-tier stamps, so every
        # untiered composition's judge prompt stays byte-identical.
        tier_rubric = _judge_periphery_rubric(citations)
        shared_lead = (
            "For each numbered CLAIM, decide whether it is FAITHFUL to the cited "
            "SUB-CLAIMS (the N -> sub-claim text map below). A claim that cites a "
            "[[ref:N]] marker must FOLLOW FROM the sub-claim it names. A claim with "
            "NO [[ref:N]] marker is a synthesis / BLUF / framing / severity / "
            "absence statement — mark it SUPPORTED unless it asserts a SPECIFIC "
            "fact (an event, number, name, or place) that is absent from, or "
            "contradicted by, ALL of the sub-claims. Answer strict JSON only: "
            '{"verdicts": ["supported"|"unsupported"|"contradicted", ...]} with '
            "one verdict per claim, in order."
            + _JUDGE_QUOTE_RULE
            + _JUDGE_QUALIFIER_RULE
            + "\n\n"
            + tier_rubric
            + f"N -> sub-claim: {json.dumps({str(k): v for k, v in evidence.items()})}"
        )
        # The absence rubric is scoped to the SAME evidence map so the negative
        # judge sees exactly what the analyst searched (design §3.4 per-claim lead).
        absence_evidence_line = (
            "N -> sub-claim (the evidence the analyst searched): "
            f"{json.dumps({str(k): v for k, v in evidence.items()})}"
        )
        # V-D: the evidence THIS run showed the judge (see quote_corpus below).
        evidence_values: dict[Any, Any] = dict(evidence)
    else:
        # UNIT branch. Feed the judge the cited signal's TEXT (title / F-train
        # SOURCE+summary lines), not an opaque signal_id — a judge handed only a
        # UUID cannot verify a claim and marks even properly-cited claims
        # unsupported (the unit-score crusher). Mirrors the composition path's
        # _ordinal_evidence_map (sub-claim text).
        cited = _marker_to_evidence(citations)
        shared_lead = (
            "For each numbered CLAIM, decide whether it is FAITHFUL to the evidence "
            "(the [N] -> evidence map below). A claim that cites [N] markers must "
            "FOLLOW FROM the evidence those markers name. An evidence entry may carry "
            "an 'OUTLET' line (the publisher ref of the cited signal — use it to check a "
            "claim about WHO reported something), a 'SOURCE' line (the actual source "
            "article) and an 'Analyst summary' line "
            "(what the analyst worked from — it does NOT itself validate a claim). Two "
            "modes, driven by the SOURCE label: (a) 'SOURCE (authoritative)' is the "
            "COMPLETE article — a cited claim absent from it is UNSUPPORTED; (b) "
            "'SOURCE (authoritative excerpt ...)' is only the START of a longer article "
            "— do NOT mark a claim unsupported merely because it is absent from the "
            "excerpt (the Analyst summary shows what the fuller article covered); mark "
            "it unsupported ONLY if the SOURCE excerpt CONTRADICTS it. In BOTH modes, a "
            "fact in the Analyst summary that the SOURCE CONTRADICTS is UNSUPPORTED. A "
            "claim with NO [N] marker is a synthesis / framing / severity / absence "
            "statement — mark it SUPPORTED unless it asserts a SPECIFIC fact (an event, "
            "number, name, or place) that is absent from, or contradicted by, ALL of "
            "the evidence. "
            'Answer strict JSON only: {"verdicts": ["supported"|"unsupported"|'
            '"contradicted", ...]} with one verdict per claim, in order.'
            + _JUDGE_QUOTE_RULE
            + _JUDGE_QUALIFIER_RULE
            + "\n\n"
            f"[N] -> evidence: {json.dumps(cited)}"
        )
        absence_evidence_line = (
            f"[N] -> evidence (the evidence the analyst searched): {json.dumps(cited)}"
        )
        evidence_values = dict(cited)

    def _numbered(claim_list: list[str]) -> str:
        return "\n".join(
            _judge_claim_block(i, c) for i, c in enumerate(claim_list, start=1)
        )

    # V-D: the normalized EVIDENCE CORPUS a hard-fail quote must resolve against
    # — exactly the text this call showed the judge, so a "quote" lifted from the
    # CLAIM itself (or invented) cannot pass. Built once per run.
    quote_corpus = _normalize_quote_text(
        " \n ".join(str(v) for v in evidence_values.values())
    )
    # W2/R2: the PRIOR READ slice of that corpus — the continuity block a claim
    # diffs against is its SUBJECT, never its refutation.
    prior_read_corpus = _normalize_quote_text(
        " \n ".join(
            str(v)
            for v in evidence_values.values()
            if _PRIOR_READ_MARKER in str(v)[:200].lower()
        )
    )
    # V-G1: the SIGNALS-backed slice — the only evidence that can carry a hard
    # fail on its own. Everything else in the map is analyst prose (a grounding
    # block, a composition sub-claim) and needs the claim's own citation to count.
    signal_ordinals = _signal_backed_ordinals(citations)
    signal_corpus = _normalize_quote_text(
        " \n ".join(
            str(v) for n, v in evidence_values.items() if n in signal_ordinals
        )
    )
    # V-I4: the MACHINE-CODED slice, and its complement. A GDELT/CAMEO row is a
    # code label, not testimony — the V-B route has excluded the class 2,109
    # times a day since W1(c) while this path never had the filter. A quote that
    # resolves ONLY here is real, and is not evidence of what the claim says.
    machine_ordinals = machine_coded_ordinals(citations)
    machine_corpus = _normalize_quote_text(
        " \n ".join(
            str(v) for n, v in evidence_values.items() if n in machine_ordinals
        )
    )
    testimony_corpus = (
        _normalize_quote_text(
            " \n ".join(
                str(v)
                for n, v in evidence_values.items()
                if n not in machine_ordinals
            )
        )
        if machine_ordinals
        else quote_corpus
    )
    subclaim_markers = _uses_subclaim_convention(citations)

    def _self_cited_corpus(claim: str) -> str:
        """The normalized evidence THIS claim's own markers name (V-G1)."""
        markers = _markers_in_claim(claim, subclaim=subclaim_markers)
        if not markers:
            return ""
        return _normalize_quote_text(
            " \n ".join(
                str(evidence_values[n]) for n in markers if n in evidence_values
            )
        )

    def _severity(verdict: str, quote: str, claim: str) -> tuple[str, str]:
        """``(verdict, EARNED quote)`` — every test a hard fail has to survive.

        A contradiction stays HARD only when its quote RESOLVES against the shown
        evidence (V-D); is not a machine CODING rather than testimony (V-I4);
        resolves in evidence that MAY refute — source reporting, or something
        this claim itself cited (V-G1); REFUTES rather than merely resolves the
        claim (W2); does not land on a clause the claim already exempts (V-G3);
        names something the claim actually denied (V-H4); does not state the
        claim's OWN numbers back to it (V-I1); and lands on a claim the V-B
        router has not already taken off the slice route (V-I5).

        ORDERING is a contract, not an accident: every rule about what the
        EVIDENCE IS runs before every rule about what the QUOTE SAYS, which runs
        before the one rule about what the CLAIM IS — so the most specific
        available diagnosis wins the label, and a panel reading the counters can
        tell the eight demotion mechanisms apart.

        The earned quote rides back out so the caller can PERSIST it: a hard
        fail nobody can audit is a hard fail nobody can trust, and that holds
        for the demotions too.
        """
        if verdict != "contradicted":
            return verdict, ""
        if not _quote_resolves(quote, quote_corpus):
            return _VERDICT_CONTRADICTED_UNQUOTED, ""
        if machine_corpus and not _quote_resolves(quote, testimony_corpus):
            # V-I4: the quote lives ONLY in a GDELT/CAMEO event coding —
            # "STUDENT <-> PAPUA: protest in Jakarta". Real, resolvable, and not
            # testimony: it is a machine's reading of an article, and the
            # article underneath it often says the opposite of the coding. Runs
            # beside V-G1 because both ask what the evidence IS, and before it
            # because "not testimony at all" is the stronger statement.
            return _VERDICT_CONTRADICTED_MACHINE_ROW, str(quote)
        if not _quote_is_refutable_evidence(
            quote, signal_corpus, _self_cited_corpus(claim)
        ):
            # V-G1: real, verbatim — and lifted from a FINDING this claim never
            # cited. The desk disagrees with a prior/sibling read; that is an
            # update, not a misstatement of its evidence. Keep the quote.
            return _VERDICT_PRIOR_READ_CONFLICT, str(quote)
        if not _quote_refutes(quote, claim, quote_corpus, prior_read_corpus):
            # It DID point at real evidence — keep the quote so the demotion is
            # auditable too, and say plainly that it resolves without refuting.
            return _VERDICT_CONTRADICTED_UNREFUTED, str(quote)
        if _quote_hits_a_carve_out(quote, claim):
            # V-G3: the quote refutes a clause the claim ALREADY EXEMPTS. Same
            # class as W2 — it resolves the claim's subject without opposing its
            # assertion — and the persisted quote makes the call auditable.
            return _VERDICT_CONTRADICTED_UNREFUTED, str(quote)
        if quote_misses_the_denied_scope(quote, claim):
            # V-H4: the claim ENUMERATED what it denies and the quote names none
            # of it in full — business defaults against "sovereign default
            # pressures". Real evidence, of something else. After the older,
            # more specific diagnoses so they keep their labels, and carries its
            # OWN class so a panel can read the split off the counters.
            return _VERDICT_CONTRADICTED_OFF_SCOPE, str(quote)
        if quote_confirms_the_claim(quote, claim):
            # V-I1: the quote states the claim's OWN numbers back to it —
            # "16 people were killed, and another 36 were injured" against
            # "sixteen lives and thirty-six injuries". It CONFIRMS. Runs beside
            # V-H4 because it is the same seam (equivalence normalization on the
            # hard-fail path), and last for the same reason: the older, more
            # specific diagnoses keep their labels.
            return _VERDICT_QUOTE_CONFIRMS, str(quote)
        routed = claim_is_routed_out(claim)
        if routed is not None:
            # V-I5: the V-B router already took this claim off the slice route as
            # a continuity / volume / trajectory read, and 08-03 rec #2 shipped
            # to stop exactly this class from hard-failing. Routing it out of one
            # path and hard-failing it on the other is one claim with two
            # authorities. LAST in the chain, so every more specific diagnosis
            # about the QUOTE keeps its label; this one is about the CLAIM.
            logger.info(
                "verify.judge.route_excluded class=%s claim=%r — the router "
                "decided this is not a slice-checkable negative, so the hard "
                "class is not available on the judge path either",
                routed, claim[:120],
            )
            return _VERDICT_ROUTE_EXCLUDED, str(quote)
        return verdict, str(quote)

    # M14 whole-finding survey — retained as-is (the ≤1-positive special case):
    # ONE call, the WHOLE claim list, the survey rubric on both the system role
    # and the prompt lead. No V3 partition, no branch telemetry (one rubric).
    if null_result:
        survey_prompt = (
            _NULL_RESULT_PROMPT_PREFIX
            + shared_lead
            + "\n\nCLAIMS:\n"
            + _numbered(claims)
        )
        survey_verdicts = await _judge_claim_partition(
            judge_llm,
            claims=claims,
            evidence_prompt=survey_prompt,
            system=_NULL_RESULT_JUDGE_SYSTEM,
        )
        if not survey_verdicts:
            return [], {}
        return (
            [
                (c, *_severity(v, q, c))
                for c, (v, q) in zip(claims, survey_verdicts)
            ],
            {},
        )

    # V3 partition — split graded claims by kind, preserving each claim's
    # ORIGINAL index so verdicts can be re-zipped in span order. The absence
    # partition is the only one routed to a dedicated prompt this train (DEC-E);
    # everything else rides the shared prompt as the ``citation_support``
    # (residual) partition.
    absence_idx: list[int] = []
    shared_idx: list[int] = []
    kinds: list[str] = []
    for i, claim in enumerate(claims):
        kind = _claim_kind(claim)
        kinds.append(kind)
        if kind == CLAIM_KIND_ABSENCE:
            absence_idx.append(i)
        else:
            shared_idx.append(i)

    verdicts_by_idx: dict[int, tuple[str, str]] = {}

    if shared_idx:
        shared_claims = [claims[i] for i in shared_idx]
        shared_prompt = shared_lead + "\n\nCLAIMS:\n" + _numbered(shared_claims)
        shared_verdicts = await _judge_claim_partition(
            judge_llm,
            claims=shared_claims,
            evidence_prompt=shared_prompt,
            system=generic_system,
        )
        if not shared_verdicts:
            return [], {}  # judge_empty on the load-bearing partition → soft-fail
        for i, (v, q) in zip(shared_idx, shared_verdicts):
            verdicts_by_idx[i] = _severity(v, q, claims[i])

    if absence_idx:
        absence_claims = [claims[i] for i in absence_idx]
        absence_prompt = (
            "You are given the evidence the analyst searched and a list of ABSENCE "
            "claims to grade against it.\n\n"
            + absence_evidence_line
            + "\n\nABSENCE CLAIMS:\n"
            + _numbered(absence_claims)
        )
        absence_profile = _JUDGE_PROFILES[CLAIM_KIND_ABSENCE]
        absence_verdicts = await _judge_claim_partition(
            judge_llm,
            claims=absence_claims,
            evidence_prompt=absence_prompt,
            system=absence_profile.judge_system or generic_system,
        )
        if not absence_verdicts:
            return [], {}  # judge_empty on the absence partition → soft-fail
        for i, (v, q) in zip(absence_idx, absence_verdicts):
            verdicts_by_idx[i] = _severity(v, q, claims[i])

    # Re-zip in ORIGINAL span order + record per-branch sub-scores (design §2.3).
    out: list[tuple[str, str, str]] = []
    branch_scores: dict[str, dict[str, int | float]] = {}
    for i, claim in enumerate(claims):
        verdict, earned_quote = verdicts_by_idx[i]
        out.append((claim, verdict, earned_quote))
        kind = kinds[i]
        bucket = branch_scores.setdefault(
            kind, {"checkable": 0, "supported": 0, "score": 0.0}
        )
        bucket["checkable"] += 1
        if verdict == "supported":
            bucket["supported"] += 1
    for bucket in branch_scores.values():
        c = bucket["checkable"]
        bucket["score"] = 1.0 if c == 0 else round(bucket["supported"] / c, 4)
    return out, branch_scores


async def _fold_absence_slice(
    report: FaithfulnessReport,
    *,
    body: str,
    judge_llm: Any | None,
    slice_conn: Any | None,
    run_id: Any | None,
    target_id: str | None,
) -> FaithfulnessReport:
    """Fold the V-B slice-scoped verdicts for SCOPED-ABSENCE claims (see above).

    A finding with no scope-qualified absence claim returns the report UNCHANGED
    (byte-identical) and never touches the DB — the DB read happens only once a
    candidate claim exists.
    """
    # NOT WIRED (no slice_conn / run_id) is a NO-OP, not an unavailable slice:
    # every pre-V-B caller lands here and must be byte-identical, counter map
    # included. ``absence_slice_unavailable`` means the branch RAN and the slice
    # could not be read.
    if not body or slice_conn is None or run_id is None:
        return report
    candidates_by_claim: list[tuple[str, str]] = []
    # The claims already carrying an ORTHOGONAL flag a content check must never
    # clear (see the block comment). W31's phrasing backstop was the first; V-G5's
    # uncited world-knowledge premise is the second — a scoped negative can be
    # perfectly verified against the slice AND still rest on a baseline the
    # analyst supplied from memory, and letting the slice pass erase that would
    # silently retire the pass-side guard the same way it retired W31's.
    scope_flagged = {
        cv.text.strip(): _COEXISTING_FLAG_COUNTERS[cv.reason]
        for cv in report.claim_verdicts
        if cv.reason in _COEXISTING_FLAG_COUNTERS
    }
    for claim in _segment_claims(body):
        if not _is_judgeable_claim(claim):
            continue
        qual = absence_scope_qualifier(claim)
        if qual is None:
            continue
        # W1(e): a VOLUME / CONTINUITY / TRAJECTORY read carries the absence SHAPE
        # without being a slice-checkable negative — it keeps today's route.
        excluded = _absence_route_exclusion(claim)
        if excluded is not None:
            report.bump("absence_slice_route_excluded")
            # V-G2: the pooled counter said the router was working while 15.2% of
            # its traffic was the ONE class the prior readout had already asked it
            # to drop. Per-class receipts, so the next panel reads the split
            # instead of re-deriving it from claim text.
            report.bump(f"absence_slice_route_excluded_{excluded}")
            logger.info(
                "verify.absence_slice.route_excluded class=%s claim=%r",
                excluded, claim[:120],
            )
            continue
        candidates_by_claim.append((claim, qual))
    if not candidates_by_claim:
        return report

    scoped: list[tuple[str, str]] = []
    for claim, qual in candidates_by_claim:
        counter = scope_flagged.get(claim.strip())
        if counter is None:
            scoped.append((claim, qual))
        else:
            report.bump(counter)
    if not scoped:
        return report

    slice_rows = await load_absence_slice_rows(slice_conn, run_id)
    if slice_rows is None:
        report.bump("absence_slice_unavailable", len(scoped))
        logger.info(
            "verify.absence_slice.unavailable run_id=%s claims=%d "
            "— scoped negatives keep today's verdict", run_id, len(scoped),
        )
        return report

    # W1(c): a GDELT/CAMEO event coding is a machine reading of a wire report, not
    # reporting — it can neither verify nor violate a scoped negative, so it leaves
    # the eligible set entirely (it is not evidence either way).
    machine = [r for r in slice_rows if r.machine_structured]
    eligible = [r for r in slice_rows if not r.machine_structured]
    if machine:
        report.bump("absence_slice_machine_rows_excluded", len(machine))

    overrides: list[_ClaimOverride] = []
    stage2: list[_Stage2Claim] = []
    for claim, qual in scoped:
        # W1(a): only rows within the CLAIM's target scope can bear on it.
        scope = _slice_scope_countries(claim, target_id=target_id)
        in_scope = [r for r in eligible if _row_in_claim_scope(r.text, scope)]
        off_scope = len(eligible) - len(in_scope)
        if off_scope:
            report.bump("absence_slice_off_scope_rows_excluded", off_scope)
        texts = [r.text for r in in_scope]
        n = len(texts)
        if n == 0:
            # NOTHING eligible to check against — a genuinely empty slice, or one
            # the machine-row / scope filters emptied. A screen that ran over zero
            # rows finds no collision by construction, and reading that as a
            # verified absence is the exact B3 honesty failure ("never fabricate a
            # pass from a slice we could not consult"), one filter further down.
            report.bump("absence_slice_unresolved")
            report.bump("absence_slice_no_eligible_rows")
            continue
        thin = n < _ABSENCE_SLICE_THIN_ROWS
        # W1(f): a verification against a 1- or 2-row slice is a WEAK result and
        # the detail must read that way — it is still the honest verdict, but it
        # must never be quoted as strong verification.
        scale = f"a THIN {n}-row" if thin else f"the {n}-row"
        terms = _absence_content_terms(claim, target_id=target_id)
        if len(terms) < _ABSENCE_SLICE_MIN_TERMS:
            report.bump("absence_slice_unresolved")
            continue
        hits, discriminated = _absence_slice_candidates(terms, texts)
        if not hits and discriminated:
            if thin:
                report.bump("absence_slice_thin")
            overrides.append(
                _ClaimOverride(
                    text=claim,
                    supported=True,
                    counter="absence_slice_verified",
                    detail=(
                        f"scoped negative ('{qual}') checked against {scale} "
                        "in-scope input slice: no row is topically about the "
                        "thing said to be absent"
                        + (
                            " — too few rows for this to be strong verification"
                            if thin
                            else ""
                        )
                    ),
                )
            )
            continue
        if not hits:
            # Unscreenable (the claim's whole vocabulary saturates the slice) —
            # never read as verified; there is simply nothing to show stage 2.
            report.bump("absence_slice_unresolved")
            continue
        report.bump("absence_slice_candidates")
        # V-G3 carries the SCALE qualifier and the screen's own content terms into
        # stage 2 alongside W1(d)'s carve-outs — the prompt shows the qualifier,
        # the post-check enforces it.
        stage2.append(
            _Stage2Claim(
                text=claim,
                hits=hits,
                carve_outs=_absence_carve_outs(claim),
                qualifier=qual,
                terms=frozenset(terms),
            )
        )

    if stage2 and judge_llm is not None and _llm_judge_enabled():
        overrides.extend(await _absence_slice_stage2(report, stage2, judge_llm))
    elif stage2:
        report.bump("absence_slice_unresolved", len(stage2))

    return _apply_claim_overrides(report, overrides)


def _resolve_violating_row(quote: Any, shown: list[str]) -> str | None:
    """The SHOWN slice row a stage-2 "contradicted" quote names, or ``None``.

    Exact (normalized) match first — the pre-W1 behavior, which is what a TITLE
    row produces. W1(b) then admits CONTAINMENT, because a composed row is shown
    as a body EXCERPT and the judge quotes the violating sentence out of it
    rather than echoing 500 characters. The same
    :data:`_JUDGE_QUOTE_MIN_CHARS` floor V-D uses applies, so a 2-word fragment
    still resolves against nothing.
    """
    norm = _normalize_quote_text(quote)
    if not norm:
        return None
    for row in shown:
        if _normalize_quote_text(row) == norm:
            return row
    if len(norm.strip("\"'“”‘’ .…")) < _JUDGE_QUOTE_MIN_CHARS:
        return None
    cleaned = norm.strip("\"'“”‘’ .…")
    for row in shown:
        if cleaned in _normalize_quote_text(row):
            return row
    return None


@dataclass(frozen=True)
class _Stage2Claim:
    """One scoped negative carried into the V-B stage-2 call, with its screen.

    ``carve_outs`` is W1(d)'s exemption clauses; ``qualifier`` and ``terms`` are
    V-G3's additions — the claim's SCALE/KIND word and the stage-1 content terms,
    which the prompt shows and the post-check enforces.
    """

    text: str
    hits: list[str]
    carve_outs: list[str]
    qualifier: str | None = None
    terms: frozenset[str] = frozenset()


async def _absence_slice_stage2(
    report: FaithfulnessReport,
    stage2: list[_Stage2Claim],
    judge_llm: Any,
) -> list[_ClaimOverride]:
    """ONE bounded judge call over the term-colliding slice rows (see above).

    Returns the overrides it could decide. Any failure — transport error, empty
    or malformed response, a "contradicted" whose named row does not resolve
    against the candidate set — decides NOTHING and leaves today's verdict, so a
    stage-2 miss can never manufacture either a pass or a hard fail.

    W1(d): each claim's CARVE-OUT clauses ride into the prompt beneath it, so the
    thing a claim explicitly exempts cannot be read back as its violation.

    V-G3: its SCALE qualifier rides down too, and a named violator that reports
    the claim's own subject at a SMALLER scale than the qualifier decides NOTHING
    — the Morinville shape, where "DOZENS protest" was read as violating "no MASS
    protests" while "MASSIVE data centre" sat two words away as the decoy.
    """
    claims = [c.text for c in stage2]
    shown: list[str] = []
    for entry in stage2:
        for row in entry.hits:
            if row not in shown:
                shown.append(row)
    shown = shown[:_ABSENCE_SLICE_CANDIDATE_CAP]

    def _claim_block(i: int, entry: _Stage2Claim) -> str:
        block = f"{i}. {entry.text}"
        if entry.carve_outs:
            block += "\n   CARVE-OUTS this claim already exempts (a row reporting "
            block += "one of these does NOT violate it): "
            block += "; ".join(f"'{c}'" for c in entry.carve_outs)
        if entry.qualifier and entry.qualifier.lower() in _SCALE_QUALIFIERS:
            block += (
                f"\n   SCALE this claim denies: '{entry.qualifier}'. A row "
                "reporting the same kind of event at a SMALLER scale (dozens, a "
                "handful, one incident) SUPPORTS this claim — it does not "
                "violate it."
            )
        return block

    prompt = (
        "INPUT-SLICE ROWS (the rows the analyst actually read that share "
        "vocabulary with the claims below):\n"
        + "\n".join(f"- {t}" for t in shown)
        + "\n\nSCOPED NEGATIVE CLAIMS:\n"
        + "\n".join(
            _claim_block(i, entry) for i, entry in enumerate(stage2, start=1)
        )
    )
    try:
        verdicts = await _judge_claim_partition(
            judge_llm,
            claims=claims,
            evidence_prompt=prompt,
            system=_ABSENCE_SLICE_JUDGE_SYSTEM,
        )
    except Exception as exc:  # noqa: BLE001 — never break the verify pass
        logger.warning("verify.absence_slice.stage2_failed err=%s", exc)
        report.bump("absence_slice_unresolved", len(claims))
        return []
    if not verdicts:
        report.bump("absence_slice_unresolved", len(claims))
        return []

    out: list[_ClaimOverride] = []
    for entry, (verdict, quote) in zip(stage2, verdicts):
        claim = entry.text
        if verdict == "supported":
            out.append(
                _ClaimOverride(
                    text=claim,
                    supported=True,
                    counter="absence_slice_verified",
                    detail=(
                        f"scoped negative checked against the {len(shown)} "
                        "colliding in-scope input-slice rows: none reports the "
                        "thing said to be absent"
                    ),
                )
            )
            continue
        # A violation must NAME the violating row, resolved against the set we
        # showed — the same earned-severity rule V-D applies to evidence quotes.
        violating = _resolve_violating_row(quote, shown)
        if verdict == "contradicted" and violating:
            # V-G3: the named row reports the claim's own subject at a SMALLER
            # scale than the claim's qualifier denies. That row is the claim's
            # EVIDENCE, not its refutation — decide nothing rather than invert it.
            if _scale_undershoots_claim(entry.qualifier, violating, set(entry.terms)):
                report.bump("absence_slice_scale_undershoot")
                report.bump("absence_slice_unresolved")
                logger.info(
                    "verify.absence_slice.scale_undershoot qualifier=%r row=%r "
                    "— a smaller-scale report of the same subject supports a "
                    "scale-qualified negative",
                    entry.qualifier, violating[:160],
                )
                continue
            # V-H5: the named row's own LEADING assertion is a NEGATIVE about the
            # same subject — the composition's "no coordinated narrative" filed
            # as violated by a unit BLUF reading "No coordinated narrative is
            # evident". That row corroborates the claim; it cannot refute it.
            if row_restates_the_negative(violating, set(entry.terms)):
                report.bump("absence_slice_row_restates_claim")
                report.bump("absence_slice_unresolved")
                logger.info(
                    "verify.absence_slice.row_restates_claim row=%r — a negative "
                    "cannot be refuted by another negative about the same thing",
                    violating[:160],
                )
                continue
            out.append(
                _ClaimOverride(
                    text=claim,
                    supported=False,
                    counter=_ABSENCE_SLICE_CONTRADICTED,
                    reason=_ABSENCE_SLICE_CONTRADICTED,
                    detail=f"violated by an input-slice row: {violating[:200]}",
                )
            )
            continue
        report.bump("absence_slice_unresolved")
    return out


async def verify_finding_faithfulness(
    *,
    body: str,
    citations: Any,
    judge_llm: Any | None = None,
    finding_confidence: float | None = None,
    indicators: Any = None,
    title: str = "",
    target_id: str | None = None,
    judge_prompt_profile: str | None = None,
    facts_conn: Any | None = None,
    slice_conn: Any | None = None,
    run_id: Any | None = None,
    eval_block: Any = None,
) -> FaithfulnessReport:
    """MANDATORY faithfulness verify over ONE finding's cited prose.

    Always runs the deterministic citation-presence FLOOR; optionally refines it
    with the flag-gated LLM judge (soft-fail to the floor, labelled
    ``judge-unavailable``, when the flag is off or the judge errors). Returns a
    :class:`FaithfulnessReport`; the caller persists it as a ``critique`` so the
    gate folds ``overall_score = faithfulness_score`` into
    ``effective_confidence = min(confidence, overall_score)``.

    Parameters
    ----------
    body:
        The finding's prose (markdown, with ``[N]`` citation markers).
    citations:
        The P0-T1 ``data['citations']`` list (``[{"marker", "signal_id"}, ...]``).
        A finding with NO citations scores every fact-asserting claim as
        unsupported — that is the honest result, not a defect of the pass.
    judge_llm:
        Optional resolved ``LLMHandlerLike`` for the judge. ``None`` (or the
        flag off) → deterministic floor labelled ``judge-unavailable``.
    finding_confidence:
        OPTIONAL — the finding's own confidence. Default ``None`` so the unit
        caller + existing tests are byte-identical. The composition caller passes
        ``float(finding_payload.confidence)`` so the T7 hedge-laundering check can
        compare a composed clause's asserted confidence against its cited
        sub-claim's ceiling. Ignored on the unit ``[N]`` path.
    indicators:
        OPTIONAL — the finding's structured ``data['indicators']`` block (S3-T1).
        Default ``None`` → no-op (byte-identical for every finding without it).
        When present, each ``triggered`` indicator is a checkable claim that MUST
        carry a citation; an uncited ``triggered`` DEMOTES faithfulness while
        ``not_observed`` / ``expired`` stay forward-looking + exempt. The forward-
        looking 'Indicators to watch' PROSE section is UNAFFECTED — it stays
        dropped wholesale by ``_segment_claims``; this scores only the STRUCTURED
        mirror.
    title:
        OPTIONAL — the finding's title. Default ``""`` → the M13/M15 guards see
        only the body (byte-identical for callers that pass no title). The unit
        caller passes it so the guards catch a title-only leak (the M15 live case
        was a Turkey desk head TITLED "Romania").
    target_id:
        OPTIONAL — the finding's run target id (``country_g20_tr`` …). Default
        ``None`` → the M15 cross-target guard is a no-op (byte-identical). When a
        ``country_*`` desk id is passed, a finding that names ONLY other countries
        is FLAGGED (demotes effective_confidence), never deleted.
    judge_prompt_profile:
        OPTIONAL (P2-4) — the judge PROMPT PROFILE: ``'current'`` (default; the
        calibrated live prompt, byte-identical) or ``'independent'`` (the staged
        adversarial-reviewer posture — dormant until an operator flips it).
        ``None`` → the ``LEGBA_JUDGE_PROMPT_PROFILE`` env, then ``current``.
    facts_conn:
        OPTIONAL (E-1) — a live DB connection for the facts-reconciled
        officeholder guard (``stale_leader_vs_facts``). Default ``None`` →
        the guard is a no-op (byte-identical for every existing caller). When
        present, a claim naming a person in an officeholder role for a country
        is probed against the CURRENT facts-table officeholder and a mismatch
        FLAGS (demotes, never auto-corrects — the seed facts can themselves be
        stale); a facts read failure degrades to no flag.
    slice_conn / run_id:
        OPTIONAL (V-B) — a live DB connection + this run's id, which together let
        the pass read the RETAINED INPUT SLICE
        (``analyst_traces.input_row_refs``) and judge SCOPED-ABSENCE claims
        against their actual scope instead of the citation subset. Either one
        ``None`` → the branch is a no-op (byte-identical for every existing
        caller); an unreadable slice degrades to today's behavior, counted
        ``absence_slice_unavailable``, never a fabricated pass.
    """
    floor = _deterministic_floor(body, citations, finding_confidence)
    # V-F: record the NON-PROPOSITIONAL spans the splitter dropped (a bare
    # ``(not_observed)`` status token). Counted, never graded — the drop is what
    # keeps a non-claim out of the verdict ledger; the counter is what makes the
    # class visible if a producer starts emitting them again.
    _dropped = _segment_claims_with_drops(body)[1]
    if _dropped:
        # Q-1(d): partition the drops. A JSON-syntax span is a PRODUCER defect
        # (a broken output contract reaching the substrate), not the ordinary
        # non-propositional residue, and pooling the two hid it — so it gets its
        # own counter and a WARNING, because someone has to fix the producer.
        _json_drops = [d for d in _dropped if is_json_syntax_claim(d)]
        if _json_drops:
            floor.bump("claims_dropped_json_syntax", len(_json_drops))
            logger.warning(
                "verify.claims.dropped_json_syntax n=%d sample=%r — a producer is "
                "emitting raw JSON as finding prose",
                len(_json_drops), _json_drops[0][:120],
            )
        _n_other = len(_dropped) - len(_json_drops)
        if _n_other:
            floor.bump("claims_dropped_nonpropositional", _n_other)
            logger.info(
                "verify.claims.dropped_nonpropositional n=%d sample=%r",
                _n_other, next(d for d in _dropped if d not in _json_drops)[:120],
            )
    floor = _fold_indicators(floor, indicators)
    # M13/M15: cheap lexical world-knowledge + target guards (flag, never delete).
    floor = _fold_world_knowledge_guards(
        floor, title=title, body=body, target_id=target_id
    )
    # W31: the unscoped-absence backstop — a world-scoped negative with no
    # collection-scoping language is one checkable-but-unsupported soft claim.
    # Folded BEFORE the judge; span text = the raw claim, so the #116c dedup
    # keeps the judge authoritative over prose it graded (the V3 absence rubric
    # already covers unscoped absence there).
    floor = _fold_guard_spans(floor, unscoped_absence_spans(body))
    if facts_conn is not None:
        # E-1: the facts-reconciled officeholder guard (never raises — degrade-
        # not-drop lives inside stale_leader_vs_facts_spans).
        floor = _fold_guard_spans(
            floor,
            await stale_leader_vs_facts_spans(facts_conn, f"{title}\n{body}"),
        )
    report = await _maybe_llm_judge(
        floor,
        body=body,
        citations=citations,
        judge_llm=judge_llm,
        judge_prompt_profile=judge_prompt_profile,
    )
    # V-C: metadata claims are decided by LOOKUP against the columns the
    # citations captured — AFTER the judge, because the judge structurally
    # cannot grade them (their truthmaker is not in any evidence text) and this
    # check is the authority on them, on BOTH grader paths.
    report = _fold_metadata_claims(report, body=body, citations=citations)
    # V-G5: a MARKERLESS claim resting on an uncited world baseline. Also after
    # the judge, and for the same reason as V-C — the judge is TOLD to pass
    # markerless prose as synthesis, so this class can only be decided outside
    # it. Before V-B, whose coexistence rule then keeps a slice pass from erasing
    # the premise flag (the W31 pattern: the two defects are orthogonal).
    report = _fold_markerless_uncited(report, body=body, citations=citations)
    # V-B: SCOPED-ABSENCE claims are about the WHOLE input slice, which the judge
    # never saw — re-decide them against the retained slice. No-op without a
    # slice_conn/run_id, and never fabricates a pass from a missing slice.
    report = await _fold_absence_slice(
        report,
        body=body,
        judge_llm=judge_llm,
        slice_conn=slice_conn,
        run_id=run_id,
        target_id=target_id,
    )
    # R2/R3: the INPUT checks — the two deterministic verdicts the producer already
    # computed about what it was SHOWN (a buried lead, an unsurfaced contradiction)
    # and filed in ``data.eval`` where nothing read them. Folded LAST because they
    # grade the finished composition against its inputs, which is only knowable
    # once the prose exists. No-op without an eval block: every unit finding and
    # every pre-R2 composition is byte-identical.
    report = fold_input_checks(report, eval_block=eval_block, body=body)
    # V-I6: the round-4 pass-side CAVEATS, as counters. Two classes sit inside
    # the supported denominator without being propositions about the world — the
    # `triggered indicator:` scaffold rows (97 in the frozen population, all
    # supported, ~4.6% of it) and coverage statements about the platform's own
    # input set. Counted, NOT gated: removing them would move every pass-side
    # score in a train that also carries five severity changes, and nobody could
    # then say which change moved the number. Measuring is the precondition for
    # deciding the drop, not a substitute for it.
    for _counter, _n in denominator_caveat_counters(
        body=body, indicators=indicators
    ).items():
        report.bump(_counter, _n)
    # Q-1(b): stamp the SCORE STATE — last, once, after every fold has settled
    # ``checkable_claims``. Everything above still computes exactly the number it
    # always did; this decides whether that number is entitled to be published as
    # a faithfulness SCORE. The counter is what makes the class countable from the
    # critique row (previously six of eight instances carried no counter at all —
    # the claims were not dropped, they were never segmented, so nothing fired).
    report.score_state, report.score_state_reason = resolve_score_state(
        checkable_claims=report.checkable_claims, body=body
    )
    if report.score_state == SCORE_STATE_UNASSESSABLE:
        report.bump(f"unassessable_{report.score_state_reason}")
        logger.warning(
            "verify.faithfulness.unassessable reason=%s checkable=%d body_chars=%d "
            "— a score is NOT published for this finding",
            report.score_state_reason, report.checkable_claims, len(body or ""),
        )
    return report


# build_faithfulness_critique_payload extracted to judge_assessability.py
# (2026-08-05, Q-1) — the CRITIQUE CONTRACT moved next to the score-state
# policy it now enforces: the payload builder is where a tally becomes a
# published verdict, so the rule about what a tally may be CALLED and the
# code that CALLS it are one unit. Re-exported: all callers unchanged.
from .judge_assessability import (  # noqa: F401,E402 — re-exported verify surface
    build_faithfulness_critique_payload,
)
