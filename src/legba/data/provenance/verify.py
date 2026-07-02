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

# Marker for a composition ``[[ref:N]]`` citation — a 1-BASED ORDINAL (small int)
# naming the Nth cited sub-claim in the rendered bundle. This is a LOCAL copy of
# meta_findings_synthesizer._REF_MARKER_RE (NOT imported — verify.py stays
# stdlib-only + slim-image-safe: it must not import the analysts package). The
# two marker regexes are PROVABLY DISJOINT — ``\[(\d+)\]`` requires a digit right
# after ``[`` and never matches ``[[ref:5]]`` (there the digit is preceded by
# ``:``); ``\[\[ref:`` never matches ``[N]`` — so the unit ([N]) path stays inert
# whenever the composition path is selected and vice-versa.
_REF_MARKER_RE = re.compile(r"\[\[ref:(\d+)\]\]")

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

# Section headings whose CONTENT is not a checkable factual assertion: the
# "indicators to watch" block is explicitly forward-looking ("would confirm /
# break this assessment"), so its lines are NOT scored as unsupported facts.
_NON_FACTUAL_HEADINGS = (
    "indicators to watch",
    "indicators",
    "watch",
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

# Advisory span reasons (#3): NOT unsupported claims — they are structural
# observations (a shared-lineage double-count note; a hedge-laundering cap
# trigger). Excluded from the unsupported tally so supported + unsupported
# reconciles against checkable instead of over-counting.
_ADVISORY_REASONS = frozenset({"double_counted", "hedge_laundering"})

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
    asserts more confidence than the sub-claim it rests on).
    """

    text: str
    # 'no_citation' | 'unresolved_citation' | 'judge_unsupported'
    # | 'judge_contradicted' | 'double_counted' | 'hedge_laundering'
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


def _marker_to_evidence(citations: Any) -> dict[int, str]:
    """Map each citation's ``[N]`` marker index → its EVIDENCE TEXT — the cited
    signal's title/headline — so the LLM judge can verify a claim against the
    signal's actual CONTENT rather than an opaque UUID.

    The unit judge previously received :func:`_marker_to_signal_id` (``{N ->
    signal_id}``), i.e. UUIDs; a judge handed only a UUID cannot verify anything
    and marks even properly-cited claims ``unsupported`` (the dominant unit-score
    crusher). This mirrors the composition path's ``_ordinal_evidence_map`` (which
    already supplies sub-claim text). Falls back to the source URL, then the
    signal_id, only when no title is present — never fabricates evidence. Only
    entries carrying a resolvable ``signal_id`` (a real cited signal) contribute.
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
        title = entry.get("title")
        source = entry.get("source")
        if isinstance(title, str) and title.strip():
            text = title.strip()
        elif isinstance(source, str) and source.strip():
            text = source.strip()
        else:
            text = sid
        out[int(m.group(1))] = str(text)[:400]
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


def _correlated_components(
    ids: list[_H],
    derived_map: Mapping[_H, set[str]],
) -> list[list[_H]]:
    """Connected components over ``ids`` (composition sub-claim ordinals — kept
    generic over any hashable key), joined when their ``derived_from`` sets
    intersect. Each component = ONE independent evidence unit (T7).

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
            if da and db and (da & db):
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
    # A markdown heading line is structure, not an assertion.
    if s.lstrip().startswith("#"):
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
    # positive "No fewer/less than" idioms.
    if (low.startswith("no ") or low.startswith("none ")) and not low.startswith(
        ("no fewer", "no less", "no longer", "no doubt", "no single", "no one")
    ):
        return False
    for marker in _ABSENCE_MARKERS:
        if marker in low:
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
    # Still skip the explicitly forward-looking watch section (not a present
    # claim by construction); _segment_claims already section-skips it, this
    # guards an inline heading-opener.
    for head in _NON_FACTUAL_HEADINGS:
        if low.startswith(head):
            return False
    if len(re.findall(r"[A-Za-z]{2,}", stripped)) < 2:
        return False
    return True


def _segment_claims(body: str) -> list[str]:
    """Split a finding body into sentence-ish claim spans (deterministic)."""
    if not body:
        return []
    # Drop everything from the 'indicators to watch' heading onward — that whole
    # section is forward-looking by construction (the assessor prompt defines it
    # as "developments that would confirm or break this assessment").
    lines = body.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        head = line.lstrip("#-*> ").strip().lower()
        if line.lstrip().startswith("#"):
            skipping = any(head.startswith(h) for h in _NON_FACTUAL_HEADINGS)
        if skipping:
            continue
        kept.append(line)
    text = "\n".join(kept)
    raw = _SENTENCE_SPLIT_RE.split(text)
    return [c.strip() for c in raw if c.strip()]


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
        markers = sorted({int(m.group(1)) for m in _CLAIM_MARKER_RE.finditer(claim)})
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
    components = _correlated_components(cited_ords, derived_map)
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
        verdicts = await _run_judge(judge_llm, body=body, citations=citations)
    except Exception as exc:  # noqa: BLE001 — soft-fail, never break the run
        logger.warning("verify.faithfulness.judge_failed err=%s", exc)
        floor.judge_unavailable_reason = "judge_error"
        return floor

    if not verdicts:
        floor.judge_unavailable_reason = "judge_empty"
        return floor

    # Refine: the judge's per-claim verdicts replace the floor's span set. A
    # claim the floor passed but the judge marks unsupported/contradicted is
    # ADDED as an unsupported span; the score becomes supported/checkable over
    # the judge's verdicts, but never drops below nor rises above what the
    # evidence supports — we take the MIN(floor, judge) score so the judge can
    # only tighten, never inflate, the deterministic floor.
    judged_spans: list[UnsupportedSpan] = []
    supported = 0
    for claim_text, verdict in verdicts:
        if verdict == "supported":
            supported += 1
        else:
            reason = (
                "judge_contradicted" if verdict == "contradicted" else "judge_unsupported"
            )
            judged_spans.append(UnsupportedSpan(text=claim_text, reason=reason))
    checkable = len(verdicts)
    judge_score = 1.0 if checkable == 0 else supported / checkable
    refined_score = min(floor.faithfulness_score, judge_score)
    return FaithfulnessReport(
        faithfulness_score=refined_score,
        checkable_claims=max(floor.checkable_claims, checkable),
        supported_claims=supported,
        # Surface BOTH the floor's structural spans and the judge's semantic ones.
        unsupported_spans=floor.unsupported_spans + judged_spans,
        judge_status="llm",
        # Carry the floor's T7 evidence ceiling through — the judge only refines
        # the faithfulness number, never the double-count-corrected cap.
        confidence_ceiling=floor.confidence_ceiling,
    )


async def _run_judge(
    judge_llm: Any,
    *,
    body: str,
    citations: Any,
) -> list[tuple[str, str]]:
    """Call the judge LLM; return ``[(claim_text, verdict), ...]``.

    ``verdict`` ∈ {supported, unsupported, contradicted}. Kept deliberately
    thin — the prompt asks for a strict-JSON list; a malformed / empty response
    yields ``[]`` (the caller then soft-fails to the floor). This is the ONLY
    place that talks to the judge model; tests mock ``judge_llm.chat_complete``.
    """
    import json

    # H1: the judge grades EVERY prose span — including the BLUF / synthesis /
    # absence claims the FLOOR exempts — so a fabricated uncited claim can't hide
    # in a vacuous checkable=0. The floor still exempts (see _is_fact_asserting);
    # the judge distinguishes faithful synthesis from invented fact via its prompt.
    claims = [c for c in _segment_claims(body) if _is_judgeable_claim(c)]
    if not claims:
        return []
    if _uses_subclaim_convention(citations):
        # COMPOSITION branch — evidence is the CITED SUB-CLAIM's own text, keyed
        # by the ordinal the clause cites via [[ref:N]].
        evidence = _ordinal_evidence_map(citations)
        prompt = (
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
            "\n\nCLAIMS:\n"
            + "\n".join(f"{i}. {c}" for i, c in enumerate(claims, start=1))
        )
    else:
        # UNIT branch. Feed the judge the cited signal's TEXT (title), not an
        # opaque signal_id — a judge handed only a UUID cannot verify a claim and
        # marks even properly-cited claims unsupported (the unit-score crusher).
        # Mirrors the composition path's _ordinal_evidence_map (sub-claim text).
        cited = _marker_to_evidence(citations)
        prompt = (
            "For each numbered CLAIM, decide whether it is FAITHFUL to the evidence "
            "(the [N] -> evidence map below). A claim that cites [N] markers must "
            "FOLLOW FROM the evidence those markers name. A claim with NO [N] marker "
            "is a synthesis / framing / severity / absence statement — mark it "
            "SUPPORTED unless it asserts a SPECIFIC fact (an event, number, name, or "
            "place) that is absent from, or contradicted by, ALL of the evidence. "
            'Answer strict JSON only: {"verdicts": ["supported"|"unsupported"|'
            '"contradicted", ...]} with one verdict per claim, in order.\n\n'
            f"[N] -> evidence: {json.dumps(cited)}\n\nCLAIMS:\n"
            + "\n".join(f"{i}. {c}" for i, c in enumerate(claims, start=1))
        )
    response = await judge_llm.chat_complete(
        [{"role": "user", "content": prompt}],
        # 2026-07-01: the faithfulness judge now runs on the SAME core reasoning
        # model as generation (llm.primary.openai_compat), because the 8B
        # cross-family judge proved too weak — harsh + mis-aimed (see the
        # composition shake-down). Matched to the main model's budget: a
        # reasoning-class model may emit thinking before the strict-JSON verdicts,
        # so a 512 cap would truncate the JSON → empty parse → soft-fail to floor.
        # NOTE: this removes cross-family independence — a DOCUMENTED LIMITATION,
        # pending a dedicated reasoning judge model (a self-verifying model shares
        # blind spots with the generator; the deterministic citation floor + the
        # provenance chain still backstop it).
        max_tokens=16384,
        temperature=0.0,
        system=(
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
        ),
    )
    content = getattr(response, "content", "") or ""
    try:
        parsed = json.loads(content.strip().strip("`"))
    except (json.JSONDecodeError, ValueError):
        return []
    raw = parsed.get("verdicts") if isinstance(parsed, Mapping) else None
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str]] = []
    for claim, verdict in zip(claims, raw):
        v = str(verdict).strip().lower()
        if v not in ("supported", "unsupported", "contradicted"):
            v = "unsupported"
        out.append((claim, v))
    return out


async def verify_finding_faithfulness(
    *,
    body: str,
    citations: Any,
    judge_llm: Any | None = None,
    finding_confidence: float | None = None,
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
    """
    floor = _deterministic_floor(body, citations, finding_confidence)
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
            }
        },
    }
