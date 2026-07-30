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

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "reason": self.reason,
            "markers": list(self.markers),
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
_CLAIM_VERDICTS_CAP = 120
_CLAIM_VERDICT_TEXT_CHARS = 300

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

    @classmethod
    def supported(
        cls, text: str, markers: list[int | str] | None = None
    ) -> "ClaimVerdict":
        return cls(text=text, verdict=VERDICT_SUPPORTED, markers=list(markers or []))

    @classmethod
    def failed(
        cls, text: str, reason: str, markers: list[int | str] | None = None
    ) -> "ClaimVerdict":
        return cls(
            text=text,
            verdict=fail_class_for_reason(reason),
            reason=reason,
            markers=list(markers or []),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text[:_CLAIM_VERDICT_TEXT_CHARS],
            "markers": list(self.markers),
            "verdict": self.verdict,
            "reason": self.reason,
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

    def as_dict(self) -> dict[str, Any]:
        bounded, truncated = _bounded_claim_verdicts(self.claim_verdicts)
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
            # P2-4 additive: the size-bounded per-claim ledger + honest cut flag.
            "claim_verdicts": bounded,
            "claim_verdicts_truncated": truncated,
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

# Collection-scoping lexicon — language that bounds a negative to what was
# actually collected/searched, NOT the world. Deliberately GENEROUS (substring,
# lowercased): a scoped variant slipping through unflagged is the cheap error;
# flagging a genuinely scoped negative is the expensive one. Every phrasing the
# unit prompts now recommend ("in collected reporting", "this desk's sources",
# "the corpus searched", "the reviewed documents", ...) matches here.
_COLLECTION_SCOPE_MARKERS: tuple[str, ...] = (
    # desk / collection possessives
    "this desk",
    "the desk",
    "desk's",
    # collection / gathering stems (cover "collected signals", "the collection
    # window", "monitored sources", "reviewed reporting", "the corpus
    # searched", ...)
    "collected",
    "collection",
    "gathered",
    "ingested",
    "monitored",
    "sampled",
    "reviewed",
    "examined",
    "analyzed",
    "analysed",
    "searched",
    # bounded corpus / slice / window nouns
    "corpus",
    "working set",
    "signal set",
    "source set",
    "evidence set",
    "slice",
    "window",
    # available-evidence idioms
    "available reporting",
    "available sources",
    "available signals",
    "available evidence",
    "in the available",
    # bounded-referent signal/source idioms ("in the signals" names the slice;
    # bare "no signals report X" does not)
    "in the signals",
    "among the signals",
    "across the signals",
    "of the signals",
    "in these signals",
    "in the signal",
    "in the sources",
    "among the sources",
    "across the sources",
    "of the sources",
    "in these sources",
    "in our sources",
    "in the documents",
    "among the documents",
    "of the documents",
    "in the evidence",
    "in the cited",
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
        # Supported when ANY marker resolves to a real cited signal_id.
        ok = any(marker_map.get(n) in resolved_ids for n in markers if marker_map.get(n))
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


def _person_name_tokens(name: str) -> set[str]:
    """Diacritic-folded, casefolded name tokens (len ≥ 3) for tolerant person
    matching — 'Janša' matches 'Jansa', 'Donald J. Trump' matches 'Trump'."""
    norm = unicodedata.normalize("NFKD", name or "")
    norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
    return {t for t in re.split(r"[^\w'’\-]+", norm.casefold()) if len(t) >= 3}


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
    # P2-4 — the per-claim LEDGER on the judge path: the judge's verdicts are
    # authoritative for every prose span it graded (supported rows included —
    # previously recorded nowhere); floor ledger rows whose text the judge did
    # NOT grade carry over unchanged (structured-indicator rows, world-knowledge
    # guard hits — the same dedup-by-text rule the span reconciliation uses).
    # NOTE: carried SUPPORTED non-prose rows are provenance, not arithmetic —
    # the headline tallies above are untouched.
    subclaim = _uses_subclaim_convention(citations)
    judge_ledger: list[ClaimVerdict] = []
    for claim_text, verdict in verdicts:
        markers = _markers_in_claim(claim_text, subclaim=subclaim)
        if verdict == "supported":
            judge_ledger.append(ClaimVerdict.supported(claim_text, list(markers)))
        else:
            reason = (
                "judge_contradicted" if verdict == "contradicted" else "judge_unsupported"
            )
            judge_ledger.append(
                ClaimVerdict.failed(claim_text, reason, list(markers))
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
    judge_prompt_profile: str | None = None,
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
            "one verdict per claim, in order.\n\n"
            + tier_rubric
            + f"N -> sub-claim: {json.dumps({str(k): v for k, v in evidence.items()})}"
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
            system=generic_system,
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
            system=absence_profile.judge_system or generic_system,
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
    judge_prompt_profile: str | None = None,
    facts_conn: Any | None = None,
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
    """
    floor = _deterministic_floor(body, citations, finding_confidence)
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
    return await _maybe_llm_judge(
        floor,
        body=body,
        citations=citations,
        judge_llm=judge_llm,
        judge_prompt_profile=judge_prompt_profile,
    )


def build_faithfulness_critique_payload(
    report: FaithfulnessReport,
    *,
    analyzed_output_id: UUID,
    analyzed_analyst_id: str = "",
    analyzed_analyst_version: str = "",
    analyzed_model: str = "",
    judge_model: str = "",
    judge_llm_ref: str = "",
    judge_route: str = "",
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

    P2-4 (additive, labels + persistence only — scores/floors/gates untouched):

      * ``judge_llm_ref`` — the RESOLVED judge stack-ref (the P2-4 JudgeRoute
        component id) stamped top-level (CritiquePayload field) AND into
        ``data.verification`` so provenance records which model judged, forever.
        ``""`` = floor-only (no judge wired).
      * ``judge_route`` (W-3d, additive) — the judge-route CLASS the ladder
        resolved: ``configured`` (env override / ``method.llm.judge``) |
        ``fallback_verify`` (``method.llm.verify`` — today's live rung) |
        ``fallback_primary`` (terminal rung). Stamped top-level AND into
        ``data.verification`` (which the findings API projects wholesale) so
        the UI provenance badge can tell an explicitly-configured judge from a
        ladder fallback. ``""`` = floor-only / pre-W-3d rows (the block then
        carries ``None``, never a fabricated class).
      * ``data.verification.claim_verdicts`` — the size-bounded per-claim
        verdict LEDGER (supported + hard_fail + soft_fail rows) with an honest
        ``claim_verdicts_truncated`` flag; each ``unsupported_spans`` entry
        additionally carries its ``fail_class`` (via ``UnsupportedSpan.as_dict``).
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
    # P2-4: the size-bounded per-claim ledger + honest truncation flag.
    claim_verdicts, claim_verdicts_truncated = _bounded_claim_verdicts(
        report.claim_verdicts
    )
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
        # P2-4: the RESOLVED judge stack-ref (JudgeRoute component id) —
        # provenance for which model judged, stamped on the row forever.
        "judge_llm_ref": judge_llm_ref[:256],
        # W-3d: the judge-route CLASS (configured|fallback_verify|
        # fallback_primary) — the badge's configured-vs-fell-back signal.
        "judge_route": judge_route[:32],
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
                # P2-4 additive fields: the judge-route provenance stamp, and the
                # full per-claim verdict ledger (supported verdicts included —
                # previously recorded nowhere), size-bounded with an honest flag.
                "judge_llm_ref": judge_llm_ref[:256] or None,
                # W-3d: the route CLASS behind the ref — the UI reads this
                # block wholesale, so the badge gets it with no route change.
                "judge_route": judge_route[:32] or None,
                "claim_verdicts": claim_verdicts,
                "claim_verdicts_truncated": claim_verdicts_truncated,
            }
        },
    }


# ---------------------------------------------------------------------------
# C2b (P4-6) — the ``structural_claims`` verify PROFILE
# ---------------------------------------------------------------------------
#
# The honesty architecture's ONE documented exception (S-1-era / C2) is the
# deterministic STRUCTURAL analysts (graph_mining, geo_convergence_scan,
# indicator_tracker, thematic_proposal, …): they emit findings OUTSIDE the
# mandatory faithfulness verify pass at flat conf=1.0, shown with an
# ``unverified — structural`` badge (``STRUCTURAL_VERIFY_EXEMPT_ANALYSTS`` in
# provenance.kinds), because their product is a COUNT / AGGREGATE over substrate
# rows, not cited LLM prose the faithfulness judge can grade.
#
# But a structural finding that ASSERTS A CHECKABLE QUANTITY ("3 distinct source
# families converged in cell X", "currently_formed_bins = cell + country bins",
# "these N sources co-carry claim Y") CAN be verified — not by an LLM
# faithfulness judge (this is not cited prose), but by DETERMINISTIC
# RE-DERIVATION: recompute the asserted quantity from the constituent set the
# finding itself recorded (its ``derived_from`` rows / the per-bin breakdown
# captured in ``data``) and check the finding's number MATCHES. A mismatch flags
# a structural analyst that MISCOUNTS.
#
# CONTRACT — the finding declares its checkable claims in
# ``data['structural_claims']`` as a list of self-describing claim objects, so
# this module re-derives GENERICALLY (verify.py imports nothing analyst-specific
# and stays slim-image-safe; the analyst owns WHAT to claim, this seam owns HOW
# to check it):
#
#   {
#     "id": "families_cell_35_51",                     # optional label
#     "statement": "3 distinct source families in cell 35,51",  # human text
#     "op": "distinct_count" | "count" | "sum" | "equals",
#     "asserted": 3,                                   # the number the finding CLAIMS
#     "basis": ["news", "gis", "health"],              # the recorded constituent set
#     "field": "family",                               # optional dict-projection key
#   }
#
#   * count          — asserted == len(basis)
#   * distinct_count — asserted == len({project(b, field) for b in basis})
#   * sum            — asserted == sum(basis)   (basis = a list of numbers)
#   * equals         — asserted == basis        (a scalar identity; basis is the
#                                                recomputed expected value)
#   * basis == ``"@derived_from"`` (sentinel) — the re-derivation runs against
#     the finding's ACTUAL ``derived_from`` id list (passed in by the caller),
#     so a "N contributing rows" claim is checked against the real lineage,
#     not a number the analyst also typed.
#
# HONESTY. A claim whose op/basis can't be re-derived (malformed, unknown op, a
# non-list basis for a set op) is ``unverifiable_structural`` — NEVER a fake
# pass. A finding with NO structural_claims block is a NO-OP (the caller writes
# no critique; the row keeps its honest ``unverified — structural`` badge). The
# finding is stamped ``structural_verified`` only when EVERY declared claim
# re-derived AND matched (≥1 checkable, zero miscounts, zero unverifiable).
# ---------------------------------------------------------------------------

#: The finding-``data`` key carrying the declared structural claims.
STRUCTURAL_CLAIMS_DATA_KEY = "structural_claims"
#: A ``basis`` sentinel selecting the finding's actual ``derived_from`` id list.
STRUCTURAL_DERIVED_FROM_SENTINEL = "@derived_from"

#: The re-derivation ops this profile understands.
_STRUCTURAL_OPS = frozenset({"count", "distinct_count", "sum", "equals"})

#: Per-claim verdict labels.
STRUCTURAL_SUPPORTED = "supported"
STRUCTURAL_MISCOUNT = "structural_miscount"
STRUCTURAL_UNVERIFIABLE = "unverifiable_structural"

#: Bound on the persisted per-claim ledger (mirrors the faithfulness cap posture).
_STRUCTURAL_VERDICTS_CAP = 120
_STRUCTURAL_STATEMENT_CHARS = 300

# OFF-SAFE gate (C2b point 4). The structural critique is ALWAYS written and its
# verdict ALWAYS shown (the badge + the data.verification detail). Whether it
# DEMOTES effective_confidence (via the finding↔critique ``overall_score`` gate)
# is behind this flag, code DEFAULT OFF ("compute-and-show, do-not-gate"): when
# off the critique's ``overall_score`` is pinned to 1.0 so a miscount never
# lowers a structural finding's surfaced confidence; when on it carries the
# honest re-derivation fraction so a miscount demotes like any critic score.
_STRUCTURAL_VERIFY_GATE_ENV = "LEGBA_STRUCTURAL_VERIFY_GATE"


def structural_verify_gate_enabled() -> bool:
    """Whether a structural critique's score DEMOTES effective_confidence. Off
    by default (compute-and-show, not-yet-gate — C2b OFF-safe posture)."""
    raw = os.getenv(_STRUCTURAL_VERIFY_GATE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class StructuralClaimVerdict:
    """One re-derived structural claim's verdict.

    ``verdict`` ∈ {``supported`` (re-derived == asserted), ``structural_miscount``
    (re-derived != asserted — the finding misstates its own evidence),
    ``unverifiable_structural`` (the claim's op/basis could not be re-derived —
    NEVER a fake pass)}.
    """

    claim_id: str
    statement: str
    op: str
    asserted: Any
    rederived: Any
    verdict: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.claim_id[:120],
            "statement": self.statement[:_STRUCTURAL_STATEMENT_CHARS],
            "op": self.op,
            "asserted": self.asserted,
            "rederived": self.rederived,
            "verdict": self.verdict,
            "detail": self.detail[:200],
        }


@dataclass
class StructuralVerifyReport:
    """Result of the ``structural_claims`` re-derivation over ONE finding.

    ``had_claims`` is False when the finding carried no (non-empty)
    ``structural_claims`` block — the caller then writes NO critique (a no-op;
    the row keeps its honest ``unverified — structural`` badge). Otherwise the
    per-claim ``claim_verdicts`` carry each re-derivation outcome.
    """

    claim_verdicts: list[StructuralClaimVerdict] = field(default_factory=list)
    had_claims: bool = False

    @property
    def supported(self) -> int:
        return sum(1 for v in self.claim_verdicts if v.verdict == STRUCTURAL_SUPPORTED)

    @property
    def miscount(self) -> int:
        return sum(1 for v in self.claim_verdicts if v.verdict == STRUCTURAL_MISCOUNT)

    @property
    def unverifiable(self) -> int:
        return sum(
            1 for v in self.claim_verdicts if v.verdict == STRUCTURAL_UNVERIFIABLE
        )

    @property
    def checkable(self) -> int:
        """Claims that WERE re-derivable (supported + miscount)."""
        return self.supported + self.miscount

    @property
    def structural_verified(self) -> bool:
        """True only when EVERY declared claim re-derived AND matched (≥1
        checkable, zero miscounts, zero unverifiable) — the honest bar for the
        ``structural-verified`` badge. Any mismatch or any non-re-derivable
        claim keeps the finding UN-certified (honest ``unverified — structural``).
        """
        return (
            self.had_claims
            and self.checkable >= 1
            and self.miscount == 0
            and self.unverifiable == 0
        )

    @property
    def score(self) -> float:
        """Fraction of RE-DERIVABLE claims that matched; 1.0 when none is
        re-derivable (we never fabricate a demotion for an unverifiable claim —
        the badge stays honest via ``structural_verified``, not the score)."""
        c = self.checkable
        return 1.0 if c == 0 else self.supported / c


def _structural_project_distinct(basis: Any, field_name: Any) -> tuple[int | None, bool]:
    """``(distinct_count, ok)`` — the count of distinct projected members, or
    ``ok=False`` when the basis/field can't be projected (→ unverifiable)."""
    if not isinstance(basis, (list, tuple)):
        return None, False
    keys: list[Any] = []
    for item in basis:
        if field_name is not None:
            if not isinstance(item, Mapping) or field_name not in item:
                return None, False
            keys.append(item[field_name])
        else:
            keys.append(item)
    try:
        return len(set(keys)), True
    except TypeError:
        return None, False  # unhashable members → not re-derivable, honest


def _structural_sum(basis: Any) -> tuple[float | int | None, bool]:
    """``(sum, ok)`` over a list of numbers (bools rejected — a stray ``True``
    is not a number). Preserves int when every member is int."""
    if not isinstance(basis, (list, tuple)):
        return None, False
    total: float = 0.0
    all_int = True
    for item in basis:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None, False
        total += item
        if not isinstance(item, int):
            all_int = False
    return (int(total) if all_int else total), True


def _structural_values_match(asserted: Any, rederived: Any) -> bool:
    """Equality with float tolerance for numeric pairs (bools compared exactly)."""
    if isinstance(asserted, bool) or isinstance(rederived, bool):
        return asserted == rederived
    if isinstance(asserted, (int, float)) and isinstance(rederived, (int, float)):
        return abs(float(asserted) - float(rederived)) <= 1e-9
    return asserted == rederived


def _verify_one_structural_claim(
    raw: Any, idx: int, derived_from_ids: list[str]
) -> StructuralClaimVerdict:
    """Re-derive ONE declared claim and classify it. Never raises — a malformed
    claim is ``unverifiable_structural`` (honest), never a fabricated pass."""
    if not isinstance(raw, Mapping):
        return StructuralClaimVerdict(
            claim_id=f"claim_{idx}", statement="", op="", asserted=None,
            rederived=None, verdict=STRUCTURAL_UNVERIFIABLE,
            detail="claim is not an object",
        )
    claim_id = str(raw.get("id") or f"claim_{idx}")
    statement = str(raw.get("statement") or "")
    op = str(raw.get("op") or "")
    asserted = raw.get("asserted")
    field_name = raw.get("field")
    basis = raw.get("basis")
    # The ``@derived_from`` sentinel re-derives against the finding's ACTUAL
    # lineage ids (the substrate rows the finding derives from), not a number
    # the analyst also typed into its own payload.
    if basis == STRUCTURAL_DERIVED_FROM_SENTINEL:
        basis = list(derived_from_ids)

    def _unverifiable(detail: str) -> StructuralClaimVerdict:
        return StructuralClaimVerdict(
            claim_id=claim_id, statement=statement, op=op, asserted=asserted,
            rederived=None, verdict=STRUCTURAL_UNVERIFIABLE, detail=detail,
        )

    if op not in _STRUCTURAL_OPS:
        return _unverifiable(f"unknown op {op!r}")
    if asserted is None:
        return _unverifiable("no asserted value")

    if op == "count":
        if not isinstance(basis, (list, tuple)):
            return _unverifiable("basis is not a list")
        rederived: Any = len(basis)
    elif op == "distinct_count":
        rederived, ok = _structural_project_distinct(basis, field_name)
        if not ok:
            return _unverifiable("basis/field not projectable")
    elif op == "sum":
        rederived, ok = _structural_sum(basis)
        if not ok:
            return _unverifiable("basis is not a list of numbers")
    else:  # equals — basis IS the recomputed expected scalar
        if basis is None:
            return _unverifiable("no expected value")
        rederived = basis

    matched = _structural_values_match(asserted, rederived)
    return StructuralClaimVerdict(
        claim_id=claim_id,
        statement=statement,
        op=op,
        asserted=asserted,
        rederived=rederived,
        verdict=STRUCTURAL_SUPPORTED if matched else STRUCTURAL_MISCOUNT,
        detail="" if matched else f"asserted {asserted!r} != re-derived {rederived!r}",
    )


def verify_structural_claims(
    *,
    data: Any,
    derived_from: list[Any] | None = None,
) -> StructuralVerifyReport:
    """The ``structural_claims`` verify profile — DETERMINISTIC re-derivation of
    a structural finding's asserted quantities (C2b / P4-6).

    Reads ``data[STRUCTURAL_CLAIMS_DATA_KEY]`` (a list of self-describing claim
    objects), re-derives each asserted quantity from the constituent set the
    finding recorded, and returns a :class:`StructuralVerifyReport`. A claim that
    can't be re-derived is ``unverifiable_structural`` (never a fake pass). A
    finding carrying no claims block returns ``had_claims=False`` (the caller
    writes no critique). ``derived_from`` supplies the finding's actual lineage
    ids for the ``@derived_from`` basis sentinel; DB-free + pure so verify.py
    stays slim-image-safe.
    """
    claims = data.get(STRUCTURAL_CLAIMS_DATA_KEY) if isinstance(data, Mapping) else None
    if not isinstance(claims, (list, tuple)) or not claims:
        return StructuralVerifyReport(claim_verdicts=[], had_claims=False)
    df_ids = [str(x) for x in (derived_from or []) if x is not None and str(x)]
    verdicts = [
        _verify_one_structural_claim(raw, i, df_ids) for i, raw in enumerate(claims)
    ]
    return StructuralVerifyReport(claim_verdicts=verdicts, had_claims=True)


def build_structural_critique_payload(
    report: StructuralVerifyReport,
    *,
    analyzed_output_id: UUID,
    analyzed_analyst_id: str = "",
    analyzed_analyst_version: str = "",
    gate: bool | None = None,
) -> dict[str, Any]:
    """Build the ``CritiquePayload``-shaped dict for a structural verdict.

    Uses the EXISTING critique contract (``analyzed_output_id`` + top-level
    ``overall_score`` + ``data.verification``) so every faithfulness reader — the
    finding↔critique gate, the reads-API verification surface — works unchanged.

    OFF-safe (C2b point 4): ``overall_score`` is pinned to **1.0** unless the
    ``LEGBA_STRUCTURAL_VERIFY_GATE`` flag is on (``gate`` overrides the env for
    tests). Off ⇒ the critique is written + the verdict shown (badge +
    verification detail) but effective_confidence is NEVER demoted (min(conf,
    1.0) == conf) — compute-and-show, do-not-gate. On ⇒ ``overall_score`` is the
    honest re-derivation fraction, so a miscount demotes via the same
    ``effective_confidence = min(confidence, overall_score)`` gate as any critic.

    ``data.verification`` carries a ``structural_verify: true`` MARKER and the
    ``structural_verified`` boolean the reads-API reads to flip the badge from
    ``unverified — structural`` to ``structural-verified``, plus the per-claim
    ledger so the operator sees WHAT was re-derived (and any miscount).
    """
    gated = structural_verify_gate_enabled() if gate is None else gate
    honest_score = report.score
    overall = honest_score if gated else 1.0
    verified = report.structural_verified

    if report.miscount:
        headline = f"FLAGGED — {report.miscount} miscount(s)"
    elif verified:
        headline = "verified"
    else:
        headline = "unverifiable"

    body_lines = [
        f"Structural verify of finding {analyzed_output_id}",
        f"  structural_verified={verified}",
        f"  claims: checkable={report.checkable} supported={report.supported} "
        f"miscount={report.miscount} unverifiable={report.unverifiable}",
        f"  gate={'on' if gated else 'off (compute-and-show, not demoting)'}"
        f" overall_score={overall:.2f}",
    ]
    for v in report.claim_verdicts[:20]:
        body_lines.append(f"  - [{v.verdict}] {v.statement[:160] or v.claim_id}")

    ledger = [v.as_dict() for v in report.claim_verdicts[:_STRUCTURAL_VERDICTS_CAP]]
    ledger_truncated = len(report.claim_verdicts) > _STRUCTURAL_VERDICTS_CAP
    return {
        "title": f"Structural verify ({headline})",
        "body": "\n".join(body_lines)[:65536],
        "confidence": overall,
        "tags": ["verify", "structural", "structural_verified" if verified else "structural_unverified"],
        "analyzed_output_id": analyzed_output_id,
        "analyzed_analyst_id": analyzed_analyst_id[:256],
        "analyzed_analyst_version": analyzed_analyst_version[:64],
        "scores": {"structural": honest_score},
        # The gate JOIN key (data->>'overall_score'). Pinned to 1.0 when the gate
        # flag is off so no consumer (reads-API is title-pinned to Faithfulness
        # anyway; the query-port laterals are unpinned) can demote a structural
        # finding — the OFF-safe default.
        "overall_score": overall,
        "data": {
            "verification": {
                # MARKER — this is a structural re-derivation verdict, not a
                # faithfulness one. The reads-API badge derivation keys on it.
                "structural_verify": True,
                "structural_verified": verified,
                "checkable_claims": report.checkable,
                "supported_claims": report.supported,
                "miscount_claims": report.miscount,
                "unverifiable_claims": report.unverifiable,
                "overall_score": round(overall, 4),
                "structural_score": round(honest_score, 4),
                "gate": gated,
                "claim_verdicts": ledger,
                "claim_verdicts_truncated": ledger_truncated,
            }
        },
    }
