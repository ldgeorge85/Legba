# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collection export surface (A10) — basket of findings + journal entries →
one markdown or JSON document, composed server-side at FULL fidelity.

``POST /api/v1/v3/export`` takes the UI collection basket
(``{items: [{kind: finding|journal_entry, id}], format: markdown|json,
title?}``) and returns ONE document:

  * **finding** items (any ``analyst_outputs`` row — unit findings, meta/
    composition reports) carry: title, analyst, target, produced_at, the cited
    body with its ``[N]`` markers left intact, EVERY citation the row records
    — each declaring its ``citation_kind`` and the ``resolves_against`` set
    its ordinal indexes — with signal refs resolved LIVE to titles +
    canonical_urls (falling back to the stored citation title/source when the
    signal row is gone — resolution state is stated, never faked) and
    composition sub-claim refs + the five DESK GROUNDING block kinds carried
    through as structured entries rather than dropped (none carries a
    ``signal_id``; filtering on one used to strip them all, which made an
    exported composition report look uncited and printed an actively false
    "no resolved citations" line — see ``_citation_class``),
    the verify state (``faithfulness=<score>`` with hard/soft
    fail-flag counts when spans exist, or an explicit ``unverified — <reason>``
    — the structural verify-exemption included), confidence + the
    verify-folded ``effective_confidence`` (min(confidence, critic score) —
    the same fold ``substrate_reads_api`` surfaces), and the lineage receipt
    link (relative API path always; absolute when ``LEGBA_PUBLIC_BASE_URL``
    is set — via the ONE ``receipt_link`` helper the alert sinks use).

  * **journal_entry** items carry: the entry body, its tier label
    (``entry|consolidation|chronicle|lens|lens_diff``) with the VOICE framing
    stated explicitly (GLOSSARY's journal-entry language: off the
    fact/finding/nexus chain, an up-only reference, never a lineage edge —
    reflective voice, NOT the product chain), the per-claim cited spans with
    every ref resolved to ``(kind, title)`` (reusing ``journal_api``'s
    resolver so there is ONE definition), honesty flags, and the journal
    verify score when its faithfulness critique exists. NO receipt link — a
    lineage walk can never surface a journal node, so fabricating one here
    would lie about the provenance model.

Missing ids are NEVER silently dropped: a basket item that resolves to no
substrate row exports as an explicit ``not found in substrate`` section and is
counted in the header (``items: N (M not found)``).

Size-capped at ``EXPORT_MAX_ITEMS`` (50) with an honest 413 beyond — the cap
is stated in the error, not silently truncated.

Wiring convention mirrors ``goldset_api.py``: ``build_export_router(deps)``
mounted from ``server.py`` under ``/api/v1/v3``, the shared
``RegistryAPIDeps`` bundle, the same ``require_bearer`` gate, reads via
``deps.descriptor_registry.pg.acquire()``. Composition (``build_document`` /
``render_markdown``) is PURE — DB-free — so the markdown shape is
golden-testable without a substrate.

STIX is deliberately NOT built here (operator decision, program doc §A10 —
demoted to optional-later); print-PDF stays a client-side browser print of
the markdown view.

``appendix`` (A10/7b-iii, the Desk Brief) is an optional pre-composed
markdown string the client supplies and this route carries through verbatim,
printed after every basket item — see :class:`ExportRequest`.

``appendix`` also accepts an OBJECT (k5b) — ``{markdown?, absences: true,
scope}`` — which asks this route to compose the desk's TYPED ABSENCE section
itself (``export_absences.py``, off the same reader ``/v3/absence`` runs on).
It is server-side deliberately: composed by the client it would reach the JSON
format as a string and the markdown format as a section, and the two formats
of one export would then disagree about what the desk is missing. A caller
that does not ask gets byte-identical output — no document key, no section.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, model_validator

from .. import critic_fold
from ..alerts.sinks import receipt_link, unverified_state, verify_state_from_score
from ..archive import sha256_from_object_ref
from ..provenance.kinds import (
    GROUNDING_REF_KINDS,
    ROLLUP_LEAD_CARRIED,
    ROLLUP_PAYLOAD_KEY,
    is_deterministic_rollup,
    rollup_exempt_reason,
    verify_exempt_reason,
)
from ..provenance.verify import fail_class_for_reason
from .api import RegistryAPIDeps, require_bearer
from .export_absences import (
    APPENDIX_MAX_CHARS,
    ExportAppendixIn,
    load_absences_block,
    render_absences_markdown,
)
from .journal_api import (
    _ENTRY_COLS as _JOURNAL_ENTRY_COLS,
    _load_jsonb,
    _read_verify_results,
    _resolve_refs,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Hard cap on basket size — beyond this the route answers an honest 413
#: (the cap + the actual count in the detail), never a silent truncation.
EXPORT_MAX_ITEMS = 50

#: The header's honest provenance note — stamped on every export, both formats.
PROVENANCE_NOTE = (
    "machine-generated export; every claim carries its citations; "
    "verify states as recorded"
)

#: The VOICE framing for journal items — the exact GLOSSARY journal-entry
#: language (docs/GLOSSARY.md, "Journal (OutputKind)"): the journal is
#: reflective voice, explicitly OFF the product chain.
JOURNAL_VOICE_NOTE = (
    "reflective journal voice — off the fact/finding/nexus chain: an "
    "always-empty derived_from, excluded from the lineage catalog; citations "
    "live only in the row's claims / cited_substrate_refs, an up-only "
    "reference, not a lineage edge"
)

#: Human tier labels for the journal ``entry_kind`` vocabulary (the same set
#: ``journal_api._VALID_KINDS`` validates).
JOURNAL_TIER_LABELS: dict[str, str] = {
    "entry": "entry (12h diary tier)",
    "consolidation": "consolidation (daily forward-carried narrative)",
    "chronicle": "chronicle (weekly third-person tier)",
    "lens": "lens (weekly faculty lens)",
    "lens_diff": "lens_diff (weekly chorus diff)",
    "inquiry": "inquiry (standing investigation, carries a ledger)",
    "crossroads": "crossroads (cross-desk patterns, drifts, silences)",
}

# Non-ASCII citation brackets wrapping a bare integer (``【3】``/``［3］``/…)
# that some core-plane models emit instead of ASCII ``[3]``. Local mirror of
# ``inline_target._VARIANT_CITATION_RE`` (that module drags the whole analyst
# runtime in; the 2-line regex is the stable part) — normalize BEFORE the body
# is exported so the prose markers and the citation list key on the SAME
# ``[N]`` (the full-width-bracket trap, 2026-06-30).
_VARIANT_CITATION_RE = re.compile(r"[【［〔〖](\s*\d+\s*)[】］〕〗]")

# R-tail (2026-08-04) — the non-numeric half of the same drift, so an EXPORT
# never publishes a raw ``【none】`` / ``【ref:2】``. Same allowlist + rationale
# as ``inline_target._UNCITABLE_ANNOTATIONS`` (local mirror for the same reason
# the regex above is one).
_VARIANT_REF_CITATION_RE = re.compile(
    r"[【［〔〖]\s*\[?\s*ref:\s*(\d+)\s*\]?\s*[】］〕〗]", re.IGNORECASE
)
_VARIANT_RANGE_CITATION_RE = re.compile(
    r"[【［〔〖]\s*(\d+)\s*[-–—‑]\s*(\d+)\s*[】］〕〗]"
)
_VARIANT_ANNOTATION_RE = re.compile(r"[【［〔〖]([^】］〕〗]{1,40})[】］〕〗]")
_UNCITABLE_ANNOTATIONS: frozenset[str] = frozenset(
    {
        "none",
        "no citation",
        "not observed",
        "assessed",
        "assessment",
        "assessed situation",
        "assessed situations",
        "assessed structure",
        "system assessed",
        "derived structure",
        "authoritative current context",
    }
)


def _canonical_annotation(token: str) -> str | None:
    flat = re.sub(r"[_\-\s]+", " ", token).strip().casefold()
    return "[no citation]" if flat in _UNCITABLE_ANNOTATIONS else None


def _normalize_citation_markers(text: str) -> str:
    if not text:
        return text
    text = _VARIANT_CITATION_RE.sub(lambda m: f"[{m.group(1).strip()}]", text)
    text = _VARIANT_RANGE_CITATION_RE.sub(
        lambda m: f"[{m.group(1)}-{m.group(2)}]", text
    )
    text = _VARIANT_REF_CITATION_RE.sub(lambda m: f"[[ref:{m.group(1)}]]", text)
    return _VARIANT_ANNOTATION_RE.sub(
        lambda m: _canonical_annotation(m.group(1)) or m.group(0), text
    )


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class ExportItemIn(BaseModel):
    """One basket item — a substrate finding/report row or a journal entry."""

    kind: Literal["finding", "journal_entry"]
    id: UUID


class ExportRequest(BaseModel):
    """``POST /export`` body — the basket + format + optional document title.

    ``appendix`` (A10/7b-iii) is an OPTIONAL, pre-composed markdown block the
    caller supplies verbatim — carried through unread and unmodified, printed
    after every basket item. It exists for content this route has no query
    for (the Desk Brief's open-situations/tracked-events summary: neither
    table is an ``analyst_outputs`` row, so it cannot become a basket item),
    never for anything a caller could cite through the ordinary ``items``
    path instead — a citation, a verify state or a finding belongs there, not
    here.

    A STRING is that block and nothing else — the pre-k5b shape, unchanged
    and still the whole contract for every caller that sends one. An OBJECT
    (:class:`ExportAppendixIn`) is the same block plus the typed-absence ask.
    """

    items: list[ExportItemIn]
    format: Literal["markdown", "json"]
    title: str | None = None
    appendix: (
        Annotated[str, Field(max_length=APPENDIX_MAX_CHARS)]
        | ExportAppendixIn
        | None
    ) = None

    @property
    def appendix_markdown(self) -> str | None:
        """The caller's own markdown, whichever form ``appendix`` took."""
        if isinstance(self.appendix, ExportAppendixIn):
            return self.appendix.markdown
        return self.appendix

    @property
    def absence_scope(self) -> str | None:
        """The desk whose typed absence was asked for, or ``None``."""
        if isinstance(self.appendix, ExportAppendixIn) and self.appendix.absences:
            return (self.appendix.scope or "").strip() or None
        return None


# ---------------------------------------------------------------------------
# Fetch — findings (any analyst_outputs row) with verify + citations.
# ---------------------------------------------------------------------------


# The SAME faithfulness-critique lateral `/findings` uses (substrate_reads_api,
# S8-T2): pinned to `title LIKE 'Faithfulness verify%'` so a later generic
# critique can never win the produced_at race and mask the verify verdict.
_FINDING_SQL = f"""
    WITH f AS MATERIALIZED (
        SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity,
               f.data, f.target_id, f.analyst_id, f.analyst_version,
               f.produced_at, f.derived_from, f.superseded_by
          FROM analyst_outputs f
         WHERE f.id = ANY($1::uuid[])
    ), {critic_fold.latest_critique_cte(
        "c",
        "(cr.data->>'overall_score')::real AS critic_score,"
        " (cr.data->'data'->'verification') AS verification",
        "SELECT id::text FROM f",
    )}
    SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity,
           f.data, f.target_id, f.analyst_id, f.analyst_version,
           f.produced_at, f.derived_from, f.superseded_by,
           c.critic_score AS critic_score,
           c.verification AS verification
      FROM f
      LEFT JOIN c ON c.fid = f.id::text
"""


#: Marker class for a citation whose ordinal indexes a DESK GROUNDING block
#: rather than a slice row — the discriminator ``unit_grounding`` stamps.
_MARKER_CLASS_GROUNDING = "desk_grounding"

#: The three resolution targets an exported citation can name. These are the
#: SETS an ordinal is a position in, spelled as data so a reader of the
#: document never has to run a shape heuristic over ``signal_id`` / ``ref_id``
#: to learn what a marker points at.
_RESOLVES_SIGNALS = "signals"
_RESOLVES_OUTPUTS = "analyst_outputs"
_RESOLVES_IN_ROW = "data.citations"


def _citation_class(entry: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """Classify one stored citation → ``(citation_kind, resolves_against,
    marker_class)``, or ``None`` when the entry is genuinely unclassifiable.

    THE DEFECT THIS EXISTS TO CLOSE (2026-08-30): the previous reader kept only
    entries with a truthy ``signal_id``. Three shapes coexist in
    ``data['citations']`` and only ONE of them carries that key:

      * a UNIT SIGNAL ref — ``signal_id`` = a ``signals`` uuid;
      * a COMPOSITION SUB-CLAIM ref — ``ref_id`` = an ``analyst_outputs`` uuid
        under ``ref_kind='finding'``, NO ``signal_id``
        (``meta_findings_synthesizer``);
      * a DESK GROUNDING block — one of
        :data:`~legba.data.provenance.kinds.GROUNDING_REF_KINDS`, carrying
        ``marker_class='desk_grounding'`` and its own ``resolves_against``,
        with a ``ref_id`` only for the prior read (the other four are synthetic
        and deliberately carry NO id — ``unit_grounding.citation_for_block``).

    So the filter silently stripped every citation off a composition report and
    every grounding block off a unit read, and a finding whose citations were
    ALL of those printed the actively false "no resolved citations recorded on
    this row". The verify plane has scored these as SUPPORTED evidence since
    2026-07-31; the export was contradicting its own grader.

    Classification keys on ``marker_class`` / ``resolves_against`` FIRST — the
    structural marks the producer writes — and falls back to the canonical
    ``GROUNDING_REF_KINDS`` registry for rows written before those marks
    existed. It is deliberately NOT a ``signal_id``-presence heuristic, because
    that heuristic IS the bug.
    """
    if entry.get("signal_id"):
        return ("signal", _RESOLVES_SIGNALS, None)
    marker_class = entry.get("marker_class")
    ref_kind = entry.get("ref_kind")
    if marker_class == _MARKER_CLASS_GROUNDING or ref_kind in GROUNDING_REF_KINDS:
        kind = str(ref_kind or entry.get("grounding") or _MARKER_CLASS_GROUNDING)
        target = entry.get("resolves_against")
        return (
            kind,
            str(target) if isinstance(target, str) and target else _RESOLVES_IN_ROW,
            # PASSED THROUGH, never re-derived. A pre-stamp row carries no
            # mark, and stamping one into the export would claim the producer
            # wrote something it did not. `citation_kind` already tells the
            # reader this is a grounding block; `marker_class` answers the
            # different question of whether the ROW says so itself.
            str(marker_class) if isinstance(marker_class, str) and marker_class else None,
        )
    if ref_kind == "finding" and entry.get("ref_id"):
        # The composition sub-claim convention — the ordinal indexes another
        # analyst_outputs row, captured with its own evidence_text at write
        # time (verify._uses_subclaim_convention keys on this same token).
        return ("finding", _RESOLVES_OUTPUTS, None)
    return None


def _citation_list(data: Any) -> list[Any]:
    """The raw citation array out of an ``analyst_outputs.data`` column.

    THE NESTING, and why reading it wrong was silent: the column holds the
    WHOLE payload dump (``provenance.writes`` line ~810:
    ``payload.model_dump(mode='json')``), and ``FindingPayload`` itself has a
    field named ``data``. So a citation list written as ``finding.data
    ['citations']`` (``inline_target``) lands at
    ``analyst_outputs.data->'data'->'citations'`` — DOUBLE-nested. This
    module's own ``_FINDING_SQL`` already unwraps that extra level for the
    critique block (``cr.data->'data'->'verification'``); the citation reader
    did not, so it looked for ``data->'citations'``, found nothing on every
    real row, and every exported finding printed "no resolved citations
    recorded on this row" regardless of how many it carried. The test suite
    could not see it because its fixture inserted the FLAT shape by hand.

    Nested level FIRST (the live shape), flat as the fallback — the same
    defensive order the v3 UI's ``citationsModel.extractCitations`` uses, so a
    hand-composed or forward-shaped payload still reads.
    """
    payload = _load_jsonb(data) or {}
    if not isinstance(payload, dict):
        return []
    inner = payload.get("data")
    nested = inner.get("citations") if isinstance(inner, dict) else None
    if isinstance(nested, list):
        return nested
    flat = payload.get("citations")
    return flat if isinstance(flat, list) else []


def _rollup_aligned_citations(
    data: Any, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Re-map a ``region_rollup.v1`` row's citations onto its RENDERED order.

    THE HISTORY FIX, and it is READ-SIDE ONLY. Rows are append-only, so the 16
    rollup rows written between 2026-09-05 and the 2026-09-07 producer fix carry
    ``citations[]`` in the SLICE order while their body numbers its member
    sections in the ROSTER order — ``[[ref:1]]`` under "Argentina" resolving to
    the United States' head. Exporting that pairing unchanged would keep handing
    the operator a document whose labels are wrong about history.

    A PURE FUNCTION OF THE ROW, which is the only reason it is allowed to exist.
    The rendered order is not inferred and not re-derived from anything outside
    the row: ``rollup.members[]`` is the exact walk ``render_rollup_body``
    performs, and each carried member's ``assembly_id`` IS the citation's
    ``ref_id``. So the re-map is a permutation the row itself specifies. No DB
    read, no clock, no flag.

    TOTAL OR NOTHING. If the row is not a rollup, if the counts disagree, if two
    citations share a ``ref_id``, or if any carried member has no matching
    citation, the stored list is returned UNTOUCHED. A partial re-map would mix
    two orderings inside one ordinal space, which is the defect itself.

    A no-op on every row written after the producer fix, because there the two
    orders are already the same permutation — one code path, no flag, and it
    stays correct as the old rows age out.
    """
    payload = _load_jsonb(data) or {}
    if not isinstance(payload, dict) or not is_deterministic_rollup(payload):
        return entries
    inner = payload.get("data")
    rollup = inner.get(ROLLUP_PAYLOAD_KEY) if isinstance(inner, dict) else None
    if not isinstance(rollup, dict):
        return entries
    carried = [
        m for m in (rollup.get("members") or [])
        if isinstance(m, dict) and m.get("lead_source") == ROLLUP_LEAD_CARRIED
    ]
    by_ref: dict[str, dict[str, Any]] = {}
    for entry in entries:
        ref = str(entry.get("ref_id") or "")
        if ref:
            by_ref.setdefault(ref, entry)
    if not carried or len(carried) != len(entries) or len(by_ref) != len(entries):
        return entries
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for ordinal, member in enumerate(carried, start=1):
        ref = str(member.get("assembly_id") or "")
        entry = by_ref.get(ref)
        # Each stored citation must be consumed exactly ONCE. Anything else is
        # not a permutation of the stored list, and re-numbering a non-
        # permutation would invent a pairing rather than recover one.
        if entry is None or ref in used:
            return entries
        used.add(ref)
        out.append({**entry, "ordinal": ordinal, "marker": f"[[ref:{ordinal}]]"})
    return out


def _stored_citations(data: Any) -> list[dict[str, Any]]:
    """The finding's persisted citation entries — EVERY citation kind, not
    just the signal refs, read at the level they are actually written to.

    Three entry shapes coexist (see :func:`_citation_class`); an entry that
    matches none of them carries no resolvable target at all and is dropped,
    because exporting it would be a marker pointing at nothing.

    A ``region_rollup.v1`` row written before the 2026-09-07 producer fix has
    its citations re-mapped onto the order its body renders — see
    :func:`_rollup_aligned_citations`. Every other row passes through unchanged.
    """
    return _rollup_aligned_citations(data, [
        entry
        for entry in _citation_list(data)
        if isinstance(entry, dict) and _citation_class(entry) is not None
    ])


def _cited_signal_ids(entries: list[dict[str, Any]]) -> list[str]:
    """The ``signals`` ids among a citation list — the only kind that needs a
    live substrate round-trip. Grounding blocks resolve against the row's own
    citation record and sub-claim refs against ``analyst_outputs``; neither is
    a signal, and treating them as one is what stripped them before."""
    return [
        str(e["signal_id"])
        for e in entries
        if (_citation_class(e) or ("", "", None))[0] == "signal"
    ]


async def _resolve_citation_signals(
    conn: Any, signal_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Batch-resolve cited signal ids → ``{id: {title, canonical_url, date}}``
    from the LIVE signals table (one query over the whole basket's citation
    set).
    Ids that no longer resolve are simply absent — the caller states that
    honestly instead of fabricating a link."""
    out: dict[str, dict[str, Any]] = {}
    uniq: list[str] = []
    seen: set[str] = set()
    for raw in signal_ids:
        s = str(raw)
        if s in seen:
            continue
        seen.add(s)
        try:
            UUID(s)
        except (ValueError, TypeError, AttributeError):
            continue
        uniq.append(s)
    if not uniq:
        return out
    rows = await conn.fetch(
        "SELECT id, payload->>'title' AS title, canonical_url, object_ref, "
        "payload->>'published_at' AS published_at, "
        "payload->>'_published_at_dt' AS published_at_dt, fetched_at "
        "FROM signals WHERE id = ANY($1::uuid[])",
        uniq,
    )
    for row in rows:
        # o2 — the citation DATE, off the row this query already reads. Three
        # more projected columns on the same ``id = ANY(...)`` primary-key
        # lookup: no new query, no new join, no new index. See
        # ``export_citation_lines.citation_date`` for the precedence and for
        # why the label rides along with the value.
        stamped, label = citation_date(
            row["published_at"], row["published_at_dt"], row["fetched_at"],
        )
        out[str(row["id"])] = {
            "title": row["title"],
            "canonical_url": row["canonical_url"],
            "citation_date": stamped,
            "citation_date_label": label,
            # P2-1 evidence archival (additive): derived from the existing
            # signals.object_ref column (cas:sha256/<hex>) — the export can
            # state "evidence preserved" + the verifiable hash, never
            # fabricated for un-archived rows.
            "archived": row["object_ref"] is not None,
            "archive_sha256": sha256_from_object_ref(row["object_ref"]),
        }
    return out


def _fail_flag_counts(verification: dict[str, Any]) -> dict[str, int]:
    """Count hard/soft fail flags across the verify block's unsupported spans.

    Uses the span's P2-4 ``fail_class`` when persisted; derives it from the
    span ``reason`` via the ONE mapping table for legacy blocks written before
    the label existed. Empty dict when no spans — no fabricated zero-flags."""
    counts: dict[str, int] = {}
    spans = verification.get("unsupported_spans")
    if not isinstance(spans, list):
        return counts
    for span in spans:
        if not isinstance(span, dict):
            continue
        fail_class = span.get("fail_class") or fail_class_for_reason(
            str(span.get("reason") or "")
        )
        counts[fail_class] = counts.get(fail_class, 0) + 1
    return counts


def _finding_verify(
    verification: dict[str, Any] | None,
    analyst_id: str | None,
    data: Any = None,
) -> tuple[str, dict[str, int]]:
    """``(verify_state, fail_flags)`` for one finding — the alert-edge verify
    grammar (``faithfulness=<score>`` / ``unverified — <reason>``), with the
    structural verify-exemption stated as its own honest reason.

    D-5: a DETERMINISTIC ROLLUP row gets its own reason too. Without it a region
    rollup would export as *"unverified — no faithfulness verdict recorded for
    this finding"*, which is TRUE and MISLEADING — it reads as an oversight when
    it is a design: there is no prose on that row for a faithfulness judge to
    grade, and its numbers ARE verified, by deterministic re-derivation. The
    reason keys on the ROW's payload (``data``), so a legacy generative region
    read keeps the ordinary grammar unchanged."""
    if verification is not None:
        score = verification.get("faithfulness_score")
        # Q-1: an UNASSESSABLE block still returns here — the pass ran, and saying
        # "no faithfulness verdict recorded" about it would be false. The state
        # string carries the distinction; the fail-flag counts are unchanged.
        if verification.get("score_state") == "unassessable" or (
            isinstance(score, (int, float)) and not isinstance(score, bool)
        ):
            return (
                verify_state_from_score(
                    score,
                    score_state=verification.get("score_state"),
                    provisional=verification.get("provisional"),
                ),
                _fail_flag_counts(verification),
            )
    rollup = rollup_exempt_reason(analyst_id, data)
    if rollup is not None:
        # The row, not the analyst. This same analyst_id wrote graded LLM prose
        # last week, and a badge that said "deterministic analyst" about it
        # would be false in the other direction.
        return (
            unverified_state(
                f"{rollup} (no prose to grade; the arithmetic is "
                "structurally re-derived)"
            ),
            {},
        )
    exempt = verify_exempt_reason(analyst_id)
    if exempt is not None:
        return (
            unverified_state(f"{exempt} (verify-exempt deterministic analyst)"),
            {},
        )
    return (
        unverified_state("no faithfulness verdict recorded for this finding"),
        {},
    )


#: The spine-block hop's own row, fetched for W-3. Deliberately NARROW: this is
#: a LINEAGE hop, not a second copy of the cited read, so the document gains a
#: name and a receipt to walk to and never a duplicated body.
_SPINE_BLOCK_SQL = """
    SELECT id, title, analyst_id, target_id, produced_at, data
      FROM analyst_outputs
     WHERE id = ANY($1::uuid[])
"""


def _first_hop_citation(data: Any) -> dict[str, Any] | None:
    """The FIRST citation on a spine block's own head — the third hop, named.

    The Assessment's fence (D-6) means its citations can only ever name the
    spine; the spine block names a country/escalation read; that read names
    SIGNALS. This returns the first of those so a reader sees the chain
    terminate in the world rather than in another one of our own rows.

    The fields are the head's OWN STORED copy, never a live re-resolution
    (``resolution_source`` on this hop would be ``in_row`` in the vocabulary of
    :func:`_citation_export_entry`). Re-resolving one head's citations inside a
    different row's export would claim a freshness the document did not check —
    the signal-index round-trip is for the exported row's own signal refs.

    ``None`` when the head carries no classifiable citation — an absence stated
    as an absence, never a fabricated leaf.
    """
    for entry in _stored_citations(data):
        classified = _citation_class(entry)
        if classified is None:
            continue
        kind, resolves_against, _ = classified
        return {
            "marker": str(entry.get("marker") or ""),
            "citation_kind": kind,
            "resolves_against": resolves_against,
            "title": entry.get("title"),
            "signal_id": (
                str(entry["signal_id"]) if entry.get("signal_id") else None
            ),
            "canonical_url": entry.get("source"),
        }
    return None


async def _resolve_spine_blocks(
    conn: Any, block_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """W-3 — batch-resolve the SPINE BLOCK heads an Assessment's citations name.

    THE DEFECT (2026-09-06). By design the Assessment is fenced to its spine
    (D-6: ``derived_from == [spine_id]``), so every ``citations[].ref_id`` it
    writes is that one row id. The 12:15Z Assessment carried four citations and
    all four exported as the same UUID with the titles "Escalation composition"
    and "Country composition" — the fence rendered as thin lineage, because the
    export DROPPED the two keys the producer had already stamped:
    ``ordinal`` (which spine block the marker points at) and ``spine_block``
    (that block's OWN head — desk, target, finding id).

    Nothing about the fence needs to move to fix that, and nothing here does.
    The producer is byte-identical; what changes is that the export stops
    throwing away lineage it was handed, and adds one batched read so the hop
    carries a NAME and a RECEIPT rather than a bare uuid. One query over the
    whole basket, the ``_resolve_citation_signals`` pattern exactly.

    Ids that no longer resolve are simply absent, and the caller says so.
    """
    if not block_ids:
        return {}
    uniq = sorted({b for b in block_ids if b})
    if not uniq:
        return {}
    try:
        rows = await conn.fetch(_SPINE_BLOCK_SQL, uniq)
    except Exception:
        # A malformed id in the basket must not fail the whole export; the
        # unresolved hop degrades to `resolved: false` with the stored fields.
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[str(r["id"])] = {
            "title": r["title"],
            "analyst_id": r["analyst_id"],
            "target_id": r["target_id"],
            "produced_at": _iso(r["produced_at"]),
            "first_citation": _first_hop_citation(r["data"]),
        }
    return out


#: D-5 — the region tier's payload key. A ``region_composition`` row under the
#: rollup regime carries this INSTEAD of ``assembly``.
_ROLLUP_KEY = "rollup"
_ROLLUP_REGIME = "rollup"

#: The one sentence that stops a rollup being read as an independent finding.
#: W-2: the 2026-09-06 world read filed the five region rollups under
#: ``below_floor`` while this same export shipped them as findings, so a reader
#: had one document asserting both "these five failed verification" and "here
#: are five region findings". The ledger side is fixed at the producer; this is
#: the export's half — a rollup says what it is on its own face.
_ROLLUP_NOTE = (
    "derived carry (region_rollup.v1): this row states the arithmetic of its "
    "membership and carries its member country reads' lead blocks forward "
    "byte-identically. It is NOT an independent read, and it is not a candidate "
    "for the world surface — the member country reads are."
)


def _derivation(data: Any) -> dict[str, Any] | None:
    """The ``region_rollup.v1`` derivation block, or ``None`` for every other
    row — the W-2 export half.

    Keyed on the PAYLOAD (``data.data.rollup`` carrying ``regime == "rollup"``),
    never on ``analyst_id``: a legacy-regime ``region_composition`` row is a
    generative read and must keep exporting as one.
    """
    payload = _load_jsonb(data) or {}
    if not isinstance(payload, dict):
        return None
    inner = payload.get("data")
    rollup = inner.get(_ROLLUP_KEY) if isinstance(inner, dict) else None
    if not isinstance(rollup, dict):
        rollup = payload.get(_ROLLUP_KEY)
    if not isinstance(rollup, dict):
        return None
    if str(rollup.get("regime") or "") != _ROLLUP_REGIME:
        return None
    members = []
    for m in rollup.get("members") or []:
        if not isinstance(m, dict):
            continue
        path, url = receipt_link(m.get("assembly_id"), row_kind="finding")
        members.append({
            "target_id": m.get("target_id"),
            "target_name": m.get("target_name"),
            "carried": m.get("lead_source") == "carried",
            "lead_source": m.get("lead_source"),
            "lead_block_ordinal": m.get("lead_block_ordinal"),
            "member_read_id": m.get("assembly_id"),
            "receipt_path": path,
            "receipt_url": url,
        })
    return {
        "schema": rollup.get("schema"),
        "regime": _ROLLUP_REGIME,
        "tier": rollup.get("tier"),
        "note": _ROLLUP_NOTE,
        "frame_id": rollup.get("region_id"),
        "frame_name": rollup.get("region_name"),
        "member_count": rollup.get("member_count"),
        "members_with_head": rollup.get("members_with_head"),
        "members_carried": rollup.get("members_carried"),
        "members_missing": list(rollup.get("members_missing") or []),
        "members": members,
    }


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None


def _ordinal(value: Any) -> int | None:
    """A citation's spine ordinal, or ``None``. Never coerced from a string —
    an ordinal this reader had to guess is not an ordinal."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _spine_block_export(
    entry: dict[str, Any], spine_index: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """One citation's SPINE-BLOCK hop, resolved (W-3).

    The producer stamps ``spine_block`` = ``{finding_id, desk, target_id}`` on
    every Assessment citation and calls it, correctly, "NOT a ref_id: this
    channel did not read that head, the record did". The export used to drop it
    outright. It is kept here under its own key — so nothing can mistake it for
    a second ``ref_id`` — and enriched with the hop's title, its receipt, and
    the first citation that head itself carries, which is what turns four
    identical uuids into "Assessment → world read block 3 → country_g20_pk read
    → signals".
    """
    raw = entry.get("spine_block")
    if not isinstance(raw, dict):
        return None
    head_id = str(raw.get("finding_id") or "") or None
    live = spine_index.get(head_id or "")
    path, url = receipt_link(head_id, row_kind="finding")
    return {
        "finding_id": head_id,
        "desk": raw.get("desk") or None,
        "target_id": raw.get("target_id"),
        "title": (live or {}).get("title"),
        "produced_at": (live or {}).get("produced_at"),
        "receipt_path": path,
        "receipt_url": url,
        # The THIRD hop. `None` both when the head did not resolve and when it
        # resolved carrying no classifiable citation — the two are told apart by
        # `resolved`, never by guessing.
        "first_citation": (live or {}).get("first_citation"),
        "resolved": live is not None,
    }


def _citation_export_entry(
    entry: dict[str, Any],
    signal_index: dict[str, dict[str, Any]],
    spine_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One citation's export dict — the SAME key set for every kind, so the
    document is self-describing.

    ``citation_kind`` + ``resolves_against`` are the two keys a reader needs:
    the first says WHAT was cited (``signal`` / ``finding`` / one of the five
    grounding kinds), the second names the SET the ordinal indexes.
    ``marker_class`` passes the producer's structural mark through when the row
    carries it (``desk_grounding``), and is ``None`` on a signal or sub-claim
    ref and on grounding rows written before the mark existed — absence is not
    re-derived, it is reported.

    ``resolved``/``resolution_source`` stay honest per kind:

      * ``signal`` — re-resolved LIVE against ``signals``. Found →
        ``resolution_source='live'`` and the live title/url win over the stored copy;
        pruned → ``resolution_source='stored'``, ``resolved=False``, stored fields
        shown. Unchanged from the pre-2026-08-30 behaviour, byte for byte.
      * ``finding`` / grounding — ``resolution_source='in_row'``: the cited passage
        was captured into this row's own citation record at write time, so it
        is available to the reader of this document without a round-trip.
        These are NOT re-resolved (and never were), so claiming ``'live'``
        would be a fabrication and claiming ``'stored'``-with-``resolved=False``
        would read as "the target is gone" — which is not what was checked.
    """
    classified = _citation_class(entry)
    # _stored_citations only admits classifiable entries, so this is total.
    kind, resolves_against, marker_class = classified or (
        "signal", _RESOLVES_SIGNALS, None,
    )
    out: dict[str, Any] = {
        "marker": str(entry.get("marker") or ""),
        "citation_kind": kind,
        "resolves_against": resolves_against,
        "marker_class": marker_class,
        "signal_id": None,
        "ref_id": None,
        "title": entry.get("title"),
        "canonical_url": entry.get("source"),
        "resolved": True,
        "resolution_source": "in_row",
        "archived": False,
        "archive_sha256": None,
        # o2 — the cited source's own date and the label that says WHICH date
        # it is (``published`` / ``fetched``). In the uniform key set, both
        # ``None`` for every kind but ``signal`` and for a signal that resolves
        # to neither date, so the document stays self-describing and an absent
        # date is an absence rather than a missing key.
        "citation_date": None,
        "citation_date_label": None,
        # W-3 — two keys the producer stamps and the export used to drop. In the
        # uniform key set (both `None` off the Assessment channel) so the
        # document stays self-describing rather than growing a shape a reader
        # has to sniff for.
        "ordinal": _ordinal(entry.get("ordinal")),
        "spine_block": None,
        # 7g-2 — the cited observation's own fields; None for every kind but
        # ``observation`` (see the non-signal branch below).
        "observation": None,
    }
    if kind != "signal":
        ref_id = entry.get("ref_id")
        out["ref_id"] = str(ref_id) if ref_id else None
        out["spine_block"] = _spine_block_export(entry, spine_index or {})
        # 7g-2 — an OBSERVATION ref carries the row's own fields (the producer
        # stamped them at write time so a cited number stays legible without a
        # round trip). Passed through verbatim; absent on every other kind, so
        # the key set stays uniform by being explicitly None rather than by
        # appearing only sometimes.
        observation = entry.get("observation")
        out["observation"] = (
            dict(observation) if isinstance(observation, dict) else None
        )
        return out
    sid = str(entry["signal_id"])
    live = signal_index.get(sid)
    out["signal_id"] = sid
    # Live signal title/url first (full fidelity); stored citation fields as
    # the fallback for a pruned signal — and `resolved` states which one the
    # reader is looking at.
    out["title"] = (live or {}).get("title") or entry.get("title")
    out["canonical_url"] = (live or {}).get("canonical_url") or entry.get("source")
    out["resolved"] = live is not None
    out["resolution_source"] = "live" if live is not None else "stored"
    # P2-1: evidence-archive surface (False/None when un-archived or the
    # signal no longer resolves — never fabricated).
    out["archived"] = bool((live or {}).get("archived"))
    out["archive_sha256"] = (live or {}).get("archive_sha256")
    # o2: the date comes off the LIVE row only. A pruned signal keeps its
    # stored title and url and carries NO date, because the stored citation
    # record never held one — inventing it from the row that cited it would
    # date the source by when we wrote about it.
    out["citation_date"] = (live or {}).get("citation_date")
    out["citation_date_label"] = (live or {}).get("citation_date_label")
    return out


def _finding_export_item(
    row: Any,
    signal_index: dict[str, dict[str, Any]],
    spine_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One finding's full-fidelity export dict (pure given the fetched row +
    the resolved signal index)."""
    verification = row["verification"]
    if isinstance(verification, str):
        try:
            verification = json.loads(verification)
        except json.JSONDecodeError:
            verification = None
    if not isinstance(verification, dict):
        verification = None

    confidence = float(row["confidence"]) if row["confidence"] is not None else None
    critic_score = (
        float(row["critic_score"]) if row["critic_score"] is not None else None
    )
    effective = (
        min(confidence, critic_score)
        if confidence is not None and critic_score is not None
        else confidence
    )

    citations = [
        _citation_export_entry(entry, signal_index, spine_index)
        for entry in _stored_citations(row["data"])
    ]

    verify_state, fail_flags = _finding_verify(
        verification, row["analyst_id"], row["data"]
    )
    path, url = receipt_link(str(row["id"]), row_kind="finding")

    return {
        "kind": "finding",
        "id": str(row["id"]),
        "row_kind": row["kind"],
        "title": row["title"],
        "analyst_id": row["analyst_id"],
        "analyst_version": row["analyst_version"],
        "target_id": row["target_id"],
        "severity": row["severity"],
        "produced_at": _iso(row["produced_at"]),
        "superseded": row["superseded_by"] is not None,
        "body": _normalize_citation_markers(row["body"] or ""),
        # W-2 — `None` on every row that is not a rollup, so a reader can ask
        # "is this an independent read?" of the document rather than of us.
        "derivation": _derivation(row["data"]),
        "citations": citations,
        "verify_state": verify_state,
        "verify_flags": fail_flags,
        "confidence": confidence,
        "effective_confidence": effective,
        "receipt_path": path,
        "receipt_url": url,
    }


# ---------------------------------------------------------------------------
# Fetch — journal entries (reusing journal_api's resolvers).
# ---------------------------------------------------------------------------


def _journal_claims(row: Any, resolved: dict[str, Any]) -> list[dict[str, Any]]:
    """The entry's ``claims`` sidecar with every ref resolved to (kind, title)
    — the same hydration shape ``journal_api._hydrate_entry`` builds, reduced
    to plain dicts for the document."""
    out: list[dict[str, Any]] = []
    raw_claims = _load_jsonb(row["claims"]) or []
    if not isinstance(raw_claims, list):
        return out
    for c in raw_claims:
        if not isinstance(c, dict):
            continue
        span = c.get("text_span")
        if not isinstance(span, str) or not span:
            continue
        refs = []
        for rid in (c.get("refs") or []):
            r = resolved.get(str(rid))
            if r is not None:
                refs.append({"id": r.id, "kind": r.kind, "title": r.title})
        out.append(
            {
                "text_span": span,
                "kind": str(c.get("kind") or "fact"),
                "refs": refs,
            }
        )
    return out


def _journal_export_item(
    row: Any,
    resolved: dict[str, Any],
    verify: dict[str, Any],
) -> dict[str, Any]:
    """One journal entry's export dict — tier-labeled, VOICE-framed, claims +
    resolved refs, verify score when its faithfulness critique exists."""
    entry_kind = str(row["entry_kind"])
    vr = verify.get(str(row["id"]))
    cited = []
    for rid in (row["cited_substrate_refs"] or []):
        r = resolved.get(str(rid))
        if r is not None:
            cited.append({"id": r.id, "kind": r.kind, "title": r.title})
    return {
        "kind": "journal_entry",
        "id": str(row["id"]),
        "tier": entry_kind,
        "tier_label": JOURNAL_TIER_LABELS.get(entry_kind, entry_kind),
        "voice_note": JOURNAL_VOICE_NOTE,
        "title": row["title"],
        "analyst_id": row["analyst_id"],
        "analyst_version": row["analyst_version"],
        "period_start": _iso(row["period_start"]),
        "period_end": _iso(row["period_end"]),
        "produced_at": _iso(row["produced_at"]),
        "honesty_flags": list(row["honesty_flags"] or []),
        "body": _normalize_citation_markers(row["body"] or ""),
        "claims": _journal_claims(row, resolved),
        "cited_substrate_refs": cited,
        "verify_state": (
            verify_state_from_score(vr.score)
            if vr is not None
            else unverified_state(
                "no faithfulness verdict recorded for this journal entry"
            )
        ),
    }


def _missing_export_item(kind: str, item_id: str) -> dict[str, Any]:
    """The honest placeholder for a basket id that resolves to no row —
    exported, counted, never silently dropped."""
    return {
        "kind": kind,
        "id": item_id,
        "error": "not found in substrate",
    }


# ---------------------------------------------------------------------------
# Composition — PURE (DB-free), golden-testable.
# ---------------------------------------------------------------------------


def build_document(
    *,
    title: str | None,
    generated_at: datetime,
    items: list[dict[str, Any]],
    appendix: str | None = None,
    absences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ONE structured document both formats serialize — JSON returns it
    verbatim; markdown renders it. ``items`` is in basket order and may carry
    ``error`` placeholders for missing ids. ``appendix`` (A10/7b-iii) is
    carried through verbatim, un-parsed — ``None``/blank renders nothing.

    ``absences`` (k5b) is the server-composed typed-absence block. The key is
    ABSENT — not null — when the caller did not ask, so a document built
    without it is byte-identical to one built before this field existed.
    """
    missing = sum(1 for i in items if i.get("error"))
    doc = {
        "title": (title or "").strip() or "Legba export",
        "generated_at": generated_at.isoformat(),
        "item_count": len(items),
        "missing_count": missing,
        "provenance_note": PROVENANCE_NOTE,
        "items": items,
        # Whitespace-only collapses to `None` too — a blank appendix is
        # indistinguishable from an absent one on both the JSON and the
        # markdown surface.
        "appendix": (appendix or "").strip() or None,
    }
    if absences is not None:
        doc["absences"] = absences
    return doc


def _md_finding_section(n: int, item: dict[str, Any]) -> list[str]:
    lines = [f"## {n}. {item['title']}", ""]
    meta = [
        f"- kind: `{item['row_kind']}`",
        "- analyst: "
        + (item["analyst_id"] or "(none)")
        + (f" `{item['analyst_version']}`" if item["analyst_version"] else ""),
        f"- target: {item['target_id'] or '(global)'}",
        f"- produced_at: {item['produced_at']}",
    ]
    if item.get("severity"):
        meta.append(f"- severity: {item['severity']}")
    if item.get("confidence") is not None:
        eff = item.get("effective_confidence")
        conf = f"- confidence: {item['confidence']:.2f}"
        if eff is not None and eff != item["confidence"]:
            conf += f" (effective {eff:.2f} after verify fold)"
        meta.append(conf)
    verify_line = f"- verify: {item['verify_state']}"
    flags = item.get("verify_flags") or {}
    if flags:
        flag_bits = ", ".join(f"{v} {k}" for k, v in sorted(flags.items()))
        verify_line += f" · flags: {flag_bits}"
    meta.append(verify_line)
    if item.get("superseded"):
        meta.append("- note: this finding has been SUPERSEDED by a newer row")
    if item.get("receipt_url") or item.get("receipt_path"):
        meta.append(
            f"- receipt: {item.get('receipt_url') or item.get('receipt_path')}"
        )
    lines.extend(meta)
    lines.append("")
    if item.get("body"):
        lines.append(item["body"].rstrip())
        lines.append("")
    lines.extend(_md_derivation_section(item.get("derivation")))
    citations = item.get("citations") or []
    if citations:
        lines.append("### Citations")
        lines.append("")
        for c in citations:
            lines.append(_md_citation_line(c))
        lines.append("")
    else:
        lines.append("*(no citations recorded on this row)*")
        lines.append("")
    return lines


def _md_derivation_section(d: dict[str, Any] | None) -> list[str]:
    """The ``### Derived carry`` section — W-2's export half.

    Printed ONLY for a rollup row, and printed BEFORE the citations, because a
    reader who has just read a region body needs to know what kind of object it
    is before they weigh it against a world read that did not carry it.
    """
    if not d:
        return []
    lines = ["### Derived carry", "", f"- {d['note']}"]
    carried, total = d.get("members_carried"), d.get("member_count")
    if carried is not None and total is not None:
        lines.append(f"- membership: {carried} of {total} member reads carried")
    for m in d.get("members") or []:
        name = m.get("target_name") or m.get("target_id") or "(unnamed member)"
        state = (
            "carried" if m.get("carried")
            else str(m.get("lead_source") or "not carried")
        )
        entry = f"  - {name} — {state}"
        link = m.get("receipt_url") or m.get("receipt_path")
        if link:
            entry += f" — {link}"
        lines.append(entry)
    missing = [str(m) for m in (d.get("members_missing") or [])]
    if missing:
        lines.append(
            f"- no read inside the horizon: {', '.join(missing)}"
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# The ``### Citations`` bullets — extracted to ``export_citation_lines`` (7g-2)
#
# Moved VERBATIM to a sibling when the HISTORICAL OBSERVATION bullet brought
# this module past the module-size gate's 1,500-line entry threshold. The gate
# is honoured by splitting, never by pinning a ceiling. Imported back ONE WAY
# and re-exported, so every caller and test is unchanged.
# ---------------------------------------------------------------------------

from .export_citation_lines import (  # noqa: E402
    _md_citation_line,
    _md_observation_line,
    citation_date,
)

__all__ = ["_md_citation_line", "_md_observation_line", "citation_date"]


def _md_journal_section(n: int, item: dict[str, Any]) -> list[str]:
    lines = [f"## {n}. {item['title']}", ""]
    lines.extend(
        [
            f"- kind: journal entry · tier: {item['tier_label']}",
            f"- voice: {item['voice_note']}",
            "- analyst: "
            + (item["analyst_id"] or "(none)")
            + (f" `{item['analyst_version']}`" if item["analyst_version"] else ""),
            f"- period: {item['period_start']} → {item['period_end']}",
            f"- produced_at: {item['produced_at']}",
            f"- verify: {item['verify_state']}",
        ]
    )
    if item.get("honesty_flags"):
        lines.append(f"- honesty flags: {', '.join(item['honesty_flags'])}")
    lines.append("")
    if item.get("body"):
        lines.append(item["body"].rstrip())
        lines.append("")
    claims = item.get("claims") or []
    if claims:
        lines.append("### Claims & cited refs")
        lines.append("")
        for c in claims:
            refs = c.get("refs") or []
            ref_bits = (
                "; ".join(
                    f"{r['kind']}: {r['title'] or r['id']}" for r in refs
                )
                if refs
                else "(no refs — unverified perspective)"
            )
            lines.append(f"- [{c['kind']}] \"{c['text_span']}\" → {ref_bits}")
        lines.append("")
    return lines




def render_markdown(doc: dict[str, Any]) -> str:
    """Render the structured document as ONE markdown file: header block
    (generated-at, item count, the honest provenance note), then per-item
    sections in basket order. Deterministic given the document — the golden
    test pins this shape."""
    header_count = str(doc["item_count"])
    if doc.get("missing_count"):
        header_count += f" ({doc['missing_count']} not found)"
    lines: list[str] = [
        f"# {doc['title']}",
        "",
        f"> {doc['provenance_note']}",
        "",
        f"- generated_at: {doc['generated_at']}",
        f"- items: {header_count}",
        "",
    ]
    for n, item in enumerate(doc["items"], start=1):
        lines.append("---")
        lines.append("")
        if item.get("error"):
            lines.append(f"## {n}. ({item['kind']} {item['id']})")
            lines.append("")
            lines.append(f"**{item['error']}** — the basket referenced a row "
                         "this substrate does not hold (superseded/pruned or a "
                         "different environment).")
            lines.append("")
        elif item["kind"] == "journal_entry":
            lines.extend(_md_journal_section(n, item))
        else:
            lines.extend(_md_finding_section(n, item))
    appendix = doc.get("appendix")
    if appendix:
        # A10/7b-iii — rendered LAST, after every basket item, so the reading
        # order the Desk Brief composed (composition, then units, then this)
        # holds in the document too.
        lines.append("---")
        lines.append("")
        lines.append(str(appendix).rstrip())
        lines.append("")
    absence_lines = render_absences_markdown(doc.get("absences"))
    if absence_lines:
        # k5b — after the cited events, because what the desk HAS is what the
        # absences are read against. Its own `---` rule gives the print
        # document a section to break a page on (`printDocument.ts`).
        lines.append("---")
        lines.append("")
        lines.extend(absence_lines)
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_export_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the export router bound to the registry deps. Mount under
    ``/api/v1/v3`` so the path resolves at ``/api/v1/v3/export``."""
    router = APIRouter(tags=["export"])

    @router.post("/export")
    async def export_collection(
        req: ExportRequest,
        principal: str = Depends(require_bearer),
    ) -> Response:
        if not req.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="items must contain at least one basket entry",
            )
        if len(req.items) > EXPORT_MAX_ITEMS:
            # 413 — the constant was renamed CONTENT_TOO_LARGE in newer
            # Starlette; fall back to the legacy name on older pins.
            code_413 = getattr(
                status,
                "HTTP_413_CONTENT_TOO_LARGE",
                getattr(status, "HTTP_413_REQUEST_ENTITY_TOO_LARGE", 413),
            )
            raise HTTPException(
                status_code=code_413,
                detail=(
                    f"export is capped at {EXPORT_MAX_ITEMS} items; "
                    f"got {len(req.items)} — split the basket"
                ),
            )

        finding_ids = [str(i.id) for i in req.items if i.kind == "finding"]
        journal_ids = [str(i.id) for i in req.items if i.kind == "journal_entry"]

        findings_by_id: dict[str, dict[str, Any]] = {}
        journal_by_id: dict[str, dict[str, Any]] = {}
        # ONE instant stamps the whole document — the header, and the typed
        # absence read below. Taken before the reads, not between them.
        generated_at = datetime.now(tz=timezone.utc)
        absence_scope = req.absence_scope
        absences: dict[str, Any] | None = None

        async with deps.descriptor_registry.pg.acquire() as conn:
            if finding_ids:
                rows = await conn.fetch(_FINDING_SQL, finding_ids)
                # One batched signal resolution over the WHOLE basket's
                # citation set (no per-finding round-trips).
                all_signal_ids = [
                    sid
                    for r in rows
                    for sid in _cited_signal_ids(_stored_citations(r["data"]))
                ]
                signal_index = await _resolve_citation_signals(
                    conn, all_signal_ids
                )
                # W-3: the SAME one-batched-read discipline for the spine-block
                # hop — no per-citation round-trips.
                spine_index = await _resolve_spine_blocks(
                    conn,
                    [
                        str(sb["finding_id"])
                        for r in rows
                        for e in _stored_citations(r["data"])
                        if isinstance(sb := e.get("spine_block"), dict)
                        and sb.get("finding_id")
                    ],
                )
                for r in rows:
                    findings_by_id[str(r["id"])] = _finding_export_item(
                        r, signal_index, spine_index
                    )
            if journal_ids:
                jrows = await conn.fetch(
                    f"SELECT {_JOURNAL_ENTRY_COLS} FROM journal_entries "
                    "WHERE id = ANY($1::uuid[])",
                    journal_ids,
                )
                ref_ids = [
                    str(rid)
                    for r in jrows
                    for rid in (
                        list(r["cited_substrate_refs"] or [])
                        + [
                            ref
                            for c in (_load_jsonb(r["claims"]) or [])
                            if isinstance(c, dict)
                            for ref in (c.get("refs") or [])
                        ]
                    )
                ]
                resolved = await _resolve_refs(conn, ref_ids)
                verify = await _read_verify_results(
                    conn, [str(r["id"]) for r in jrows]
                )
                for r in jrows:
                    journal_by_id[str(r["id"])] = _journal_export_item(
                        r, resolved, verify
                    )
            if absence_scope:
                # k5b — the desk's typed absence, read on the SAME reader
                # `/v3/absence` runs on and stamped with this document's own
                # instant. It reads defensively kind by kind, so a desk whose
                # audit table is unreachable still exports the rest of its
                # absences with that kind named `not measured` — the export
                # never fails because a desk could not be fully read.
                absences = await load_absences_block(
                    conn, scope=absence_scope, now=generated_at
                )

        # Reassemble in basket order, missing ids as honest placeholders.
        items: list[dict[str, Any]] = []
        for item in req.items:
            key = str(item.id)
            found = (
                findings_by_id.get(key)
                if item.kind == "finding"
                else journal_by_id.get(key)
            )
            items.append(found or _missing_export_item(item.kind, key))

        doc = build_document(
            title=req.title,
            generated_at=generated_at,
            items=items,
            appendix=req.appendix_markdown,
            absences=absences,
        )

        stamp = generated_at.strftime("%Y%m%d")
        if req.format == "json":
            return JSONResponse(
                doc,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="legba-export-{stamp}.json"'
                    )
                },
            )
        return Response(
            content=render_markdown(doc),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="legba-export-{stamp}.md"'
                )
            },
        )

    return router
