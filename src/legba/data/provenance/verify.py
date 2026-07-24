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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Hashable, Mapping, TypeVar
from uuid import UUID

import asyncpg

from ._core import is_valid_schema_uri

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

# Marker for a bare ``[3]`` citation (mirrors inline_target._CITATION_MARKER_RE).
_CLAIM_MARKER_RE = re.compile(r"\[(\d+)\]")

# M14 (2026-07-06) — a RANGE citation marker ``[1-92]`` / ``[1–92]`` cites the
# WHOLE enumerated corpus (the shape a survey / NULL-RESULT finding uses: "51 of
# the 92 signals concern floods/sports/trade [1-92]"). The bare-marker regex
# ``\[(\d+)\]`` NEVER matches a range (its first digit is followed by '-', not
# ']'), so a range-cited clause was read as ``no_citation`` and an honest survey
# floored to ~0. This expands a range to its integer members (hyphen / en- / em-
# dash) so the floor resolves the clause against the citation bridge like any
# multi-marker clause. Capped so a pathological ``[1-999999]`` can't fan out.
_CLAIM_RANGE_RE = re.compile(r"\[(\d+)\s*[-–—]\s*(\d+)\]")
_MAX_RANGE_WIDTH = 500

# M14 — an explicit ``[no citation]`` annotation is the assessor flagging a clause
# as DELIBERATELY un-citable (a synthesis / framing / corpus-survey line), NOT a
# fabricated fact. The floor treats it as floor-EXEMPT (see _is_fact_asserting);
# the JUDGE still grades it (a fabricated absence must not hide behind the marker).
_NO_CITATION_MARKER = "[no citation]"


def _range_markers(claim: str) -> set[int]:
    """Integer marker indices contributed by RANGE citations ``[lo-hi]`` (M14)."""
    out: set[int] = set()
    for m in _CLAIM_RANGE_RE.finditer(claim):
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo <= hi and (hi - lo) <= _MAX_RANGE_WIDTH:
            out.update(range(lo, hi + 1))
    return out

# Marker for a composition ``[[ref:N]]`` citation — a 1-BASED ORDINAL (small int)
# naming the Nth cited sub-claim in the rendered bundle. This is a LOCAL copy of
# meta_findings_synthesizer._REF_MARKER_RE (NOT imported — verify.py stays
# stdlib-only + slim-image-safe: it must not import the analysts package). The
# two marker regexes are PROVABLY DISJOINT — ``\[(\d+)\]`` requires a digit right
# after ``[`` and never matches ``[[ref:5]]`` (there the digit is preceded by
# ``:``); ``\[\[ref:`` never matches ``[N]`` — so the unit ([N]) path stays inert
# whenever the composition path is selected and vice-versa.
_REF_MARKER_RE = re.compile(r"\[\[ref:(\d+)\]\]")

# C1 (2026-07-03) — citation-marker drift normalization, applied to the body at
# the TOP of _segment_claims so BOTH the section-segmentation and the [N] matching
# that consumes the spans see ASCII markers. (a) full-width / CJK lenticular
# brackets the core plane (gpt-oss / Qwen) non-deterministically emits — mirrors
# inline_target._VARIANT_CITATION_RE; (b) a parenthesized comma-list of TWO OR
# MORE numbers, ``(57, 87)`` -> ``[57][87]``, while a single-number paren
# ``(2023)`` is LEFT ALONE (year false-positive). verify.py stays stdlib-only — no
# inline_target import.
_VARIANT_CITATION_RE = re.compile(r"[【［〔〖](\s*\d+\s*)[】］〕〗]")
_PAREN_CITATION_LIST_RE = re.compile(r"\((\s*\d+(?:\s*,\s*\d+)+\s*)\)")


def _normalize_verify_markers(text: str) -> str:
    """Rewrite citation-marker drift variants to ASCII ``[N]`` before parsing."""
    if not text:
        return text
    text = _VARIANT_CITATION_RE.sub(lambda m: f"[{m.group(1).strip()}]", text)
    text = _PAREN_CITATION_LIST_RE.sub(
        lambda m: "".join(f"[{n.strip()}]" for n in m.group(1).split(",")),
        text,
    )
    return text

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

# #116(b): a BOLDED label:value line — ``**Severity:** High``, ``**Confidence:**
# Moderate``, ``**Time horizon:** 3-6 months`` — is document SCAFFOLDING the
# assessor stamps onto a finding, NOT a first-order citable fact. The bold run is
# the reliable signal (a plain ``Foo: bar`` sentence stays a claim); the colon
# may sit INSIDE the bold (``**Severity:**``) or right after it (``**Severity**:``).
# FLOOR-ONLY exemption — the judge still grades these spans (H1: exemptions must
# not hide a claim from the judge).
_LABELED_SCAFFOLD_RE = re.compile(
    r"^(?:[-*>]\s+)*"                    # optional list / blockquote bullets
    r"\*\*[^*\n:]{1,48}"                # ** + a short label (no colon/star yet)
    r"(?::\s*\*\*|\*\*\s*:)",           # colon INSIDE the bold, or right AFTER it
    re.IGNORECASE,
)

# ABSENCE / negative-finding markers — a clause asserting something was NOT
# observed cannot cite a (non-existent) signal; you cite signals that EXIST, not
# their absence. This is the class that most crushed honest LOW-RISK reads.
_ABSENCE_MARKERS = (
    "no evidence",
    "no reports",
    "no report of",
    "no confirmed",
    "no indication",
    "no sign of",
    "no signs of",
    "no observable",
    "no observed",
    "no material",
    "no coordinated",
    "no discernible",
    "no credible",
    "no notable",
    "no data",
    "no direct",
    "no new",
    "no such",
    "not found",
    "none were",
    "were found in the signal",
    "absence of",
    "no relevant",
    "no significant",
    "nothing to suggest",
    "nothing indicating",
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

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "reason": self.reason, "markers": list(self.markers)}


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "faithfulness_score": round(self.faithfulness_score, 4),
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


def _marker_to_evidence(citations: Any) -> dict[int, str]:
    """Map each citation's ``[N]`` marker index → its EVIDENCE TEXT — the cited
    signal's authoritative source (+ title, + the analyst's working summary as
    labelled secondary context) — so the LLM judge can verify a claim against the
    signal's actual CONTENT rather than an opaque UUID.

    The unit judge previously received :func:`_marker_to_signal_id` (``{N ->
    signal_id}``), i.e. UUIDs; a judge handed only a UUID cannot verify anything
    and marks even properly-cited claims ``unsupported`` (the dominant unit-score
    crusher). This mirrors the composition path's ``_ordinal_evidence_map`` (which
    already supplies sub-claim text).

    FAITHFULNESS TRUST BOUNDARY: when the citation carries ``source_text`` (the RAW
    article the summarizer distilled from — NEVER the analyst-read ``distilled_body``
    summary), the judge is grounded on that SOURCE, LABELLED authoritative; the
    analyst's ``snippet`` (its working text, distilled-first) rides along as
    LABELLED secondary context ONLY when it is a genuinely DISTINCT summary (F4). A
    claim present only in the summary but absent from a COMPLETE source is thus
    UNSUPPORTED (a summarizer hallucination can't be rubber-stamped). When the
    source is an EXCERPT (``source_truncated`` / re-truncated here), the judge is
    told so and softens to "contradicted => unsupported" — a claim the analyst
    faithfully drew from deep in a long article is NOT false-demoted for being past
    the cut (F1). Entries with NO ``source_text`` (old data, non-signal path) keep
    the prior title/snippet/source/id fallback chain byte-for-byte, at the ORIGINAL
    600-char cap (F3). Never fabricates evidence; only entries carrying a resolvable
    ``signal_id`` (a real cited signal) contribute.
    """
    out: dict[int, str] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        sid = entry.get("signal_id")
        marker = entry.get("marker")
        if not (isinstance(sid, str) and sid) or not isinstance(marker, str):
            continue
        m = _CLAIM_MARKER_RE.search(marker)
        if not m:
            continue
        # (#116e) Feed the judge the cited signal's TITLE + evidence text, mirroring
        # the composition path's evidence_text — a title alone can be too terse for
        # the judge to confirm a specific claim, so a properly-cited clause gets
        # mis-graded DOWN. Fall back to title-only, then snippet-only, then the
        # source URL, then the id — never fabricated.
        title = entry.get("title")
        source = entry.get("source")
        source_text = entry.get("source_text")
        snippet = (
            entry.get("snippet")
            or entry.get("evidence_text")
            or entry.get("summary")
        )
        title_txt = title.strip() if isinstance(title, str) and title.strip() else ""
        snip_txt = snippet.strip() if isinstance(snippet, str) and snippet.strip() else ""
        src_full = (
            source_text.strip()
            if isinstance(source_text, str) and source_text.strip()
            else ""
        )
        src_txt = src_full[:_EVIDENCE_SOURCE_CHARS]
        # F1: the SOURCE is an EXCERPT if it was flagged truncated at build time
        # (cleaned raw exceeded the store cap) OR we re-truncate it here. Either way
        # the judge must NOT demote a cited claim merely for being absent from the
        # shown text — only for being CONTRADICTED by it.
        source_truncated = bool(entry.get("source_truncated")) or (
            len(src_full) > _EVIDENCE_SOURCE_CHARS
        )
        if src_txt:
            # TRUST BOUNDARY: ground the judge on the RAW authoritative SOURCE; the
            # analyst summary is LABELLED secondary context only (the judge prompt
            # says a fact present only in a COMPLETE source is UNSUPPORTED; for an
            # EXCERPT the summary shows what the fuller article covered). F4: skip the
            # summary line when the analyst read raw directly — i.e. snippet is the
            # same as, or a leading prefix of, the source (no distinct distilled_body).
            parts = []
            if title_txt:
                parts.append(title_txt)
            if source_truncated:
                parts.append(
                    "SOURCE (authoritative excerpt — the full article is longer "
                    f"than shown): {src_txt}"
                )
            else:
                parts.append(f"SOURCE (authoritative): {src_txt}")
            if snip_txt and snip_txt != src_txt and not src_txt.startswith(snip_txt):
                parts.append(f"Analyst summary: {snip_txt}")
            text = "\n".join(parts)
            cap = _EVIDENCE_TOTAL_CHARS
        elif title_txt and snip_txt:
            # Backward-compat (no source_text): the prior title + snippet evidence.
            text = f"{title_txt} — {snip_txt}"
            cap = _EVIDENCE_LEGACY_CHARS
        elif title_txt:
            text = title_txt
            cap = _EVIDENCE_LEGACY_CHARS
        elif snip_txt:
            text = snip_txt
            cap = _EVIDENCE_LEGACY_CHARS
        elif isinstance(source, str) and source.strip():
            text = source.strip()
            cap = _EVIDENCE_LEGACY_CHARS
        else:
            text = sid
            cap = _EVIDENCE_LEGACY_CHARS
        out[int(m.group(1))] = str(text)[:cap]
    return out


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


def _resolved_citation_ordinals(citations: Any) -> set[int]:
    """The set of resolved sub-claim ORDINALS in a composition's ``citations``.

    Skips any entry that carries no resolvable ordinal (never a fabricated one).
    """
    out: set[int] = set()
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if isinstance(entry, Mapping):
            n = _citation_ordinal(entry)
            if n is not None:
                out.add(n)
    return out


def _ordinal_evidence_map(citations: Any) -> dict[int, str]:
    """Map each cited sub-claim's ORDINAL → its evidence TEXT.

    The composition enriches each citation at synth time with the cited
    sub-claim's ``evidence_text`` (its body, captured point-in-time). Fallback
    chain: ``evidence_text`` → ``title`` → the ordinal itself. NEVER fabricates
    evidence — a citation with no captured text degrades to its ordinal string so
    the judge still has a stable label.
    """
    out: dict[int, str] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        n = _citation_ordinal(entry)
        if n is None:
            continue
        text = entry.get("evidence_text")
        if not (isinstance(text, str) and text):
            text = entry.get("title")
        if not (isinstance(text, str) and text):
            text = str(n)
        out[n] = str(text)
    return out


def _ordinal_effconf_map(citations: Any) -> dict[int, float]:
    """Map each cited sub-claim's ORDINAL → its ``effective_confidence``.

    Reads the ``effective_confidence`` the composition captured at synth time
    (already ``min(confidence, faithfulness)`` from the unit's own verify). A
    citation missing / with a non-numeric value is SKIPPED — its clause is then
    never hedge-flagged and never contributes to the cap (honest: no fabricated
    correlation/ceiling).
    """
    out: dict[int, float] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        n = _citation_ordinal(entry)
        if n is None:
            continue
        eff = entry.get("effective_confidence")
        if eff is None:
            continue
        try:
            out[n] = float(eff)
        except (TypeError, ValueError):
            continue
    return out


def _ordinal_derived_map(citations: Any) -> dict[int, set[str]]:
    """Map each cited sub-claim's ORDINAL → its ``derived_from`` id SET.

    Each cited sub-claim carries its own underlying lineage/signal ids (captured
    at synth time). Two cited sub-claims are CORRELATED iff these sets intersect
    (the shared-lineage detector — T7). A citation with no ``derived_from`` list
    is omitted → it forms its own singleton component (never falsely correlated).
    """
    out: dict[int, set[str]] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        n = _citation_ordinal(entry)
        if n is None:
            continue
        df = entry.get("derived_from")
        if isinstance(df, (list, tuple)):
            out[n] = {str(x) for x in df if x is not None and str(x)}
    return out


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
    out: dict[int, str] = {}
    if not isinstance(citations, (list, tuple)):
        return out
    for entry in citations:
        if not isinstance(entry, Mapping):
            continue
        n = _citation_ordinal(entry)
        if n is None:
            continue
        src = entry.get("source")
        if not (isinstance(src, str) and src):
            src = entry.get("analyst_id")
        if isinstance(src, str) and src:
            out[n] = src
    return out


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
    if _is_bold_heading(s):
        return False
    # P7-F1(5): a forward-looking watch/indicator bullet ('… would confirm …') is
    # a future non-occurrence the read is watching FOR — nothing exists to cite.
    if _is_forward_looking(low):
        return False
    # (#116b) A bolded label:value line (**Severity:** High) is scaffolding, not a
    # citable present-fact — FLOOR-ONLY (the judge still grades it via
    # _is_judgeable_claim). Matched on the ORIGINAL span so the leading ``**`` is
    # intact (the ``stripped`` form above has already lstripped the ``*`` bold).
    if _LABELED_SCAFFOLD_RE.match(s.strip()):
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
    if _is_bold_heading(s):
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


def _is_absence_claim(low: str) -> bool:
    """True when a lower-cased span is an ABSENCE / NEGATIVE assertion.

    Byte-identical to the FLOOR's absence exemption test in
    :func:`_is_fact_asserting` (the bare ``no ``/``none `` opener minus the
    positive-idiom guard, OR any ``_ABSENCE_MARKERS`` phrase) — extracted so the
    V3 classifier routes on the SAME calibrated lexical set the 10 in-window
    recalibrations tuned, guaranteeing the route and the floor exemption agree.
    """
    if (low.startswith("no ") or low.startswith("none ")) and not low.startswith(
        ("no fewer", "no less", "no longer", "no doubt", "no single", "no one")
    ):
        return True
    return any(marker in low for marker in _ABSENCE_MARKERS)


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
        or _LABELED_SCAFFOLD_RE.match(s.strip())
        or _CITATION_ONLY_RE.match(s)
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
    '"unsupported", ...]} with one verdict per claim, in order. Output only the '
    "JSON object."
)

# The versioned profile registry (design §2.2 / §5.2 step 2). ``absence`` and
# ``citation_support`` carry a prompt this train; the other three kinds are
# stubbed (judge_system=None → telemetry-only), stamped so a later train that
# gives them a prompt is a visible per-kind version bump. Bump a ``version`` on
# ANY prompt/floor-semantics change to that kind.
_JUDGE_PROFILES: dict[str, JudgeProfile] = {
    CLAIM_KIND_CITATION_SUPPORT: JudgeProfile(
        kind=CLAIM_KIND_CITATION_SUPPORT,
        version="citsupp.v3",
        judge_system=None,  # rides the existing unit/composition prompt in _run_judge
    ),
    CLAIM_KIND_ABSENCE: JudgeProfile(
        kind=CLAIM_KIND_ABSENCE,
        version="absence.v1",
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


def _segment_claims(body: str) -> list[str]:
    """Split a finding body into sentence-ish claim spans (deterministic)."""
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
        is_heading = line.lstrip().startswith("#") or bool(_BOLD_HEADING_RE.match(line))
        if is_heading:
            head = line.strip().strip("#*->: ").lower()
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
    merged: list[str] = []
    for span in spans:
        if merged and _CITATION_ONLY_RE.match(span):
            merged[-1] = f"{merged[-1]} {span}"
        else:
            merged.append(span)
    return merged


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

    claims = [c for c in _segment_claims(body) if _is_fact_asserting(c)]
    supported = 0
    spans: list[UnsupportedSpan] = []
    for claim in claims:
        # M14: fold RANGE markers ``[lo-hi]`` in alongside bare ``[N]`` markers so a
        # corpus-survey clause that cites the whole enumerated set resolves.
        markers = sorted(
            {int(m.group(1)) for m in _CLAIM_MARKER_RE.finditer(claim)}
            | _range_markers(claim)
        )
        if not markers:
            spans.append(UnsupportedSpan(text=claim, reason="no_citation"))
            continue
        # Supported when ANY marker resolves to a real cited signal_id.
        ok = any(marker_map.get(n) in resolved_ids for n in markers if marker_map.get(n))
        if ok:
            supported += 1
        else:
            spans.append(
                UnsupportedSpan(
                    text=claim, reason="unresolved_citation", markers=markers
                )
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

    claims = [c for c in _segment_claims(body) if _is_fact_asserting(c)]
    supported = 0
    spans: list[UnsupportedSpan] = []
    for claim in claims:
        markers: list[int] = []
        for m in _REF_MARKER_RE.finditer(claim):
            n = int(m.group(1))
            if n not in markers:
                markers.append(n)
        if not markers:
            spans.append(UnsupportedSpan(text=claim, reason="no_citation"))
            continue
        resolved_markers = [n for n in markers if n in resolved_ords]
        if not resolved_markers:
            spans.append(
                UnsupportedSpan(
                    text=claim, reason="unresolved_citation", markers=list(markers)
                )
            )
            continue
        supported += 1
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
    )


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
        cites = entry.get("citations")
        # A citation is a real signal-marker INDEX (int); reject bool (a stray
        # True is not a citation index) and non-list shapes.
        has_citation = isinstance(cites, (list, tuple)) and any(
            isinstance(c, int) and not isinstance(c, bool) for c in cites
        )
        if has_citation:
            supported += 1
        else:
            stmt = entry.get("statement")
            ident = entry.get("id")
            label = (
                stmt if isinstance(stmt, str) and stmt.strip() else str(ident or "indicator")
            )
            spans.append(
                UnsupportedSpan(
                    text=f"triggered indicator without citation: {label}"[:500],
                    reason=_INDICATOR_UNCITED_TRIGGERED,
                )
            )
    return checkable, supported, spans


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


# A compact country gazetteer + desk-slug expansion, MIRRORING
# legba.runtime.grounding (_KNOWN_COUNTRY_TOKENS / _TARGET_SLUG_TO_NAMES) — a
# deliberate slim-safe local copy so verify.py imports nothing from runtime.
_TARGET_SLUG_TO_COUNTRY: dict[str, tuple[str, ...]] = {
    "us": ("united states", "america", "u.s.", "usa"),
    "cn": ("china",), "ru": ("russia",), "ir": ("iran",), "il": ("israel",),
    "in": ("india",), "id": ("indonesia",), "br": ("brazil",),
    "ar": ("argentina",), "mx": ("mexico",), "ca": ("canada",),
    "fr": ("france",), "de": ("germany",), "it": ("italy",),
    "gb": ("united kingdom", "britain", "uk"), "uk": ("united kingdom", "britain"),
    "jp": ("japan",), "kr": ("south korea", "korea"), "sa": ("saudi arabia",),
    "tr": ("turkey", "turkiye"), "au": ("australia",), "za": ("south africa",),
    "eu": ("european union",), "kp": ("north korea", "dprk"), "tw": ("taiwan",),
    "ua": ("ukraine",), "pk": ("pakistan",),
}
_COUNTRY_TOKENS: frozenset[str] = frozenset({
    "united states", "america", "u.s.", "usa",
    "china", "russia", "iran", "israel", "ukraine", "india", "indonesia",
    "brazil", "argentina", "mexico", "canada", "france", "germany", "italy",
    "spain", "united kingdom", "britain", "japan", "south korea", "north korea",
    "korea", "saudi arabia", "turkey", "turkiye", "australia", "south africa",
    "egypt", "pakistan", "afghanistan", "iraq", "syria", "lebanon", "yemen",
    "venezuela", "taiwan", "vietnam", "thailand", "philippines", "nigeria",
    "romania", "poland", "greece", "hungary", "bulgaria", "serbia", "croatia",
})


def _country_desk_slug(target_id: str | None) -> str | None:
    """The trailing ISO-2 slug of a ``country_*`` target id, else ``None``."""
    if not target_id or not isinstance(target_id, str):
        return None
    tid = target_id.strip().lower()
    if "country" not in tid:
        return None
    token = tid.rsplit("_", 1)[-1]
    return token if len(token) == 2 and token.isalpha() else None


def _mentions_country(name: str, haystack_lc: str) -> bool:
    """Whole-word (token-boundary) mention of ``name`` in a casefolded haystack."""
    nlc = name.casefold()
    return re.search(rf"(?<![a-z0-9]){re.escape(nlc)}(?![a-z0-9])", haystack_lc) is not None


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
    if any(_mentions_country(n, haystack_lc) for n in own if n):
        return None  # on-target — mentions its own geo somewhere
    others = {c for c in _COUNTRY_TOKENS if c not in own}
    named = sorted(c for c in others if _mentions_country(c, haystack_lc))
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
    )


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


async def _maybe_llm_judge(
    floor: FaithfulnessReport,
    *,
    body: str,
    citations: Any,
    judge_llm: Any | None,
) -> FaithfulnessReport:
    """Optionally refine the floor with an LLM judge (flag-gated, soft-fail).

    Engages ONLY when ``_llm_judge_enabled()`` AND a ``judge_llm`` handler is
    supplied.  The judge re-grades each claim "does this follow from its cited
    evidence?" → supported / unsupported / contradicted.  ANY error, an absent
    handler, or the flag being off → return the deterministic floor LABELLED
    ``judge-unavailable`` (``judge_status`` stays ``'deterministic'``).  NEVER
    fabricates a score.
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
        # floor.  (The live judge component points at the cross-family
        # self-hosted Llama-3.1-8B at deploy; that is a REGISTERED component,
        # not hardcoded here.)
        verdicts, branch_scores = await _run_judge(
            judge_llm, body=body, citations=citations
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail, never break the run
        logger.warning("verify.faithfulness.judge_failed err=%s", exc)
        floor.judge_unavailable_reason = "judge_error"
        return floor

    if not verdicts:
        floor.judge_unavailable_reason = "judge_empty"
        return floor

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
    for claim_text, verdict in verdicts:
        judged_texts.add(claim_text.strip())
        if verdict == "supported":
            supported += 1
        else:
            reason = (
                "judge_contradicted" if verdict == "contradicted" else "judge_unsupported"
            )
            judged_spans.append(UnsupportedSpan(text=claim_text, reason=reason))
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


async def _judge_claim_partition(
    judge_llm: Any,
    *,
    claims: list[str],
    evidence_prompt: str,
    system: str,
) -> list[str]:
    """Send ONE partition of claims to the judge; return its verdict list.

    Factored out of :func:`_run_judge` so the V3 absence partition AND the M14
    whole-finding survey call reuse the identical call + parse machinery with
    their OWN system prompts (design §3.5). A malformed / empty response yields
    ``[]``; a length mismatch raises :class:`_JudgeVerdictError` (the
    ONE-verdict-per-claim honesty contract from #116d). ``evidence_prompt`` is
    the per-branch user message already carrying the evidence map + numbered
    claim list.
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
    out: list[str] = []
    for verdict in raw:
        v = str(verdict).strip().lower()
        if v not in ("supported", "unsupported", "contradicted"):
            v = "unsupported"
        out.append(v)
    return out


async def _run_judge(
    judge_llm: Any,
    *,
    body: str,
    citations: Any,
) -> tuple[list[tuple[str, str]], dict[str, dict[str, int | float]]]:
    """Call the judge LLM; return ``([(claim_text, verdict), ...], branch_scores)``.

    ``verdict`` ∈ {supported, unsupported, contradicted}, in ORIGINAL claim order.
    ``branch_scores`` maps each claim-kind that was JUDGED to
    ``{"checkable", "supported", "score"}`` (design §2.3 telemetry).

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
    """
    import json

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
        shared_lead = (
            "For each numbered CLAIM, decide whether it is FAITHFUL to the cited "
            "SUB-CLAIMS (the N -> sub-claim text map below). A claim that cites a "
            "[[ref:N]] marker must FOLLOW FROM the sub-claim it names. A claim with "
            "NO [[ref:N]] marker is a synthesis / BLUF / framing / severity / "
            "absence statement — mark it SUPPORTED unless it asserts a SPECIFIC "
            "fact (an event, number, name, or place) that is absent from, or "
            "contradicted by, ALL of the sub-claims. Answer strict JSON only: "
            '{"verdicts": ["supported"|"unsupported"|"contradicted", ...]} with '
            "one verdict per claim, in order.\n\n"
            f"N -> sub-claim: {json.dumps({str(k): v for k, v in evidence.items()})}"
        )
        # The absence rubric is scoped to the SAME evidence map so the negative
        # judge sees exactly what the analyst searched (design §3.4 per-claim lead).
        absence_evidence_line = (
            "N -> sub-claim (the evidence the analyst searched): "
            f"{json.dumps({str(k): v for k, v in evidence.items()})}"
        )
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
            "a 'SOURCE' line (the actual source article) and an 'Analyst summary' line "
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
            '"contradicted", ...]} with one verdict per claim, in order.\n\n'
            f"[N] -> evidence: {json.dumps(cited)}"
        )
        absence_evidence_line = (
            f"[N] -> evidence (the evidence the analyst searched): {json.dumps(cited)}"
        )

    def _numbered(claim_list: list[str]) -> str:
        return "\n".join(f"{i}. {c}" for i, c in enumerate(claim_list, start=1))

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
        return list(zip(claims, survey_verdicts)), {}

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

    verdicts_by_idx: dict[int, str] = {}

    if shared_idx:
        shared_claims = [claims[i] for i in shared_idx]
        shared_prompt = shared_lead + "\n\nCLAIMS:\n" + _numbered(shared_claims)
        shared_verdicts = await _judge_claim_partition(
            judge_llm,
            claims=shared_claims,
            evidence_prompt=shared_prompt,
            system=_GENERIC_JUDGE_SYSTEM,
        )
        if not shared_verdicts:
            return [], {}  # judge_empty on the load-bearing partition → soft-fail
        for i, v in zip(shared_idx, shared_verdicts):
            verdicts_by_idx[i] = v

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
            system=absence_profile.judge_system or _GENERIC_JUDGE_SYSTEM,
        )
        if not absence_verdicts:
            return [], {}  # judge_empty on the absence partition → soft-fail
        for i, v in zip(absence_idx, absence_verdicts):
            verdicts_by_idx[i] = v

    # Re-zip in ORIGINAL span order + record per-branch sub-scores (design §2.3).
    out: list[tuple[str, str]] = []
    branch_scores: dict[str, dict[str, int | float]] = {}
    for i, claim in enumerate(claims):
        verdict = verdicts_by_idx[i]
        out.append((claim, verdict))
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


async def verify_finding_faithfulness(
    *,
    body: str,
    citations: Any,
    judge_llm: Any | None = None,
    finding_confidence: float | None = None,
    indicators: Any = None,
    title: str = "",
    target_id: str | None = None,
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
    """
    floor = _deterministic_floor(body, citations, finding_confidence)
    floor = _fold_indicators(floor, indicators)
    # M13/M15: cheap lexical world-knowledge + target guards (flag, never delete).
    floor = _fold_world_knowledge_guards(
        floor, title=title, body=body, target_id=target_id
    )
    return await _maybe_llm_judge(
        floor, body=body, citations=citations, judge_llm=judge_llm
    )


def build_faithfulness_critique_payload(
    report: FaithfulnessReport,
    *,
    analyzed_output_id: UUID,
    analyzed_analyst_id: str = "",
    analyzed_analyst_version: str = "",
    analyzed_model: str = "",
    judge_model: str = "",
) -> dict[str, Any]:
    """Build the ``CritiquePayload``-shaped dict for the faithfulness verdict.

    Returns a plain dict (the runtime validates it against ``CritiquePayload``
    on write).  ``overall_score = faithfulness_score`` so the existing finding↔
    critique gate folds it into ``effective_confidence``; the unsupported spans
    + judge status live in the payload's ``data`` so the findings API can
    surface a ``verification`` block naming WHY confidence was demoted.

    The shape is built here (not in the kind module) so the verify seam owns its
    own critique contract; ``analyzed_output_id`` is the finding's id and the
    critic is the verify pass itself (the caller stamps the analyst_ctx).

    ``overall_score`` (the gate JOIN key) folds the T7 evidence ceiling:
    ``overall_score = min(faithfulness_score, confidence_ceiling)`` when the
    composition supplied a ceiling, else ``= faithfulness_score`` (the unit path
    is byte-identical — ``confidence_ceiling`` is ``None`` there). So the gate's
    ``effective_confidence = min(confidence, overall_score)`` yields
    ``min(confidence, faithfulness, sub-claim ceiling)`` for a composition — a
    hedge-laundering clause over a weak sub-claim demotes to ≤ that sub-claim,
    and correlated sub-claims cannot inflate the ceiling.
    """
    score = report.faithfulness_score
    ceiling = report.confidence_ceiling
    # The gate score — capped by the double-count-corrected evidence ceiling.
    overall = score if ceiling is None else min(score, ceiling)
    # (#3) Advisory spans (double_counted / hedge_laundering) are structural
    # observations, NOT unsupported claims — exclude them so the tally reconciles
    # (supported + unsupported ≈ checkable) instead of over-counting.
    n_unsupported = sum(
        1 for s in report.unsupported_spans if s.reason not in _ADVISORY_REASONS
    )
    n_advisory = len(report.unsupported_spans) - n_unsupported
    judge_label = (
        report.judge_status
        if report.judge_unavailable_reason is None
        else f"judge-unavailable:{report.judge_unavailable_reason}"
    )
    body_lines = [
        f"Faithfulness verify of finding {analyzed_output_id}",
        f"  faithfulness_score={score:.2f}",
        f"  checkable_claims={report.checkable_claims} "
        f"supported={report.supported_claims} unsupported={n_unsupported}"
        + (f" advisory={n_advisory}" if n_advisory else ""),
        f"  judge={judge_label}",
    ]
    if ceiling is not None:
        body_lines.append(
            f"  confidence_ceiling={ceiling:.2f} (double-count-corrected) "
            f"→ overall_score={overall:.2f}"
        )
    for span in report.unsupported_spans[:20]:
        body_lines.append(f"  - [{span.reason}] {span.text[:200]}")
    # V3 (MP:DEC-E) — per-branch telemetry (design §2.3 / §2.2). ``branch_scores``
    # is the pooled sub-score per JUDGED claim-kind (empty on the deterministic
    # path AND on the M14 whole-finding survey path, so those runs + a pre-V3
    # reader are byte-identical). ``branch_versions`` stamps the profile VERSION
    # of each kind that ran, so a recalibration is a visible, greppable, per-kind
    # version bump. Both are ADDITIVE JSONB keys ignored by existing readers (the
    # gate reads only ``overall_score``, which is unchanged).
    branch_scores = report.branch_scores or {}
    branch_versions = {
        kind: _JUDGE_PROFILES[kind].version
        for kind in branch_scores
        if kind in _JUDGE_PROFILES
    }
    return {
        "title": f"Faithfulness verify (score {overall:.2f})",
        "body": "\n".join(body_lines)[:65536],
        # Low surfaced confidence on the critique ROW itself when faithfulness is
        # poor, so the verify product reads as what it is. The GATE uses
        # overall_score (below), not this — but keep them coherent.
        "confidence": overall,
        "tags": ["verify", "faithfulness", judge_label],
        "analyzed_output_id": analyzed_output_id,
        "analyzed_analyst_id": analyzed_analyst_id[:256],
        "analyzed_analyst_version": analyzed_analyst_version[:64],
        "analyzed_model": analyzed_model[:128],
        "judge_model": judge_model[:128],
        "scores": {"faithfulness": score},
        # The gate reads data->>'overall_score' off the analyst_outputs row; the
        # whole CritiquePayload is model_dumped into the data JSONB, so this
        # top-level field lands at data->>'overall_score' (the JOIN key).
        "overall_score": overall,
        # The verification detail the findings API surfaces (it reads
        # data->'data'->'verification').
        "data": {
            "verification": {
                "faithfulness_score": round(score, 4),
                "confidence_ceiling": (
                    round(ceiling, 4) if ceiling is not None else None
                ),
                "overall_score": round(overall, 4),
                "checkable_claims": report.checkable_claims,
                "supported_claims": report.supported_claims,
                "unsupported_spans": [s.as_dict() for s in report.unsupported_spans],
                "judge_status": report.judge_status,
                "judge_unavailable_reason": report.judge_unavailable_reason,
                # V3 per-branch telemetry (additive; {} when the judge did not run
                # or the M14 whole-finding survey rubric graded the entire list).
                "branch_scores": branch_scores,
                "branch_versions": branch_versions,
            }
        },
    }
