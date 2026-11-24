# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``known_at`` record-time cut — the CLASSIFICATION (lanes E0, E2).

An evaluation arm must read the substrate as it stood at an instant K: nothing
that entered the record at or after K may reach the model. Run 1b had no such
cut ("No corpus freeze or as-of filter on the raw readers", ``report_1b.md``),
and its open arms cited post-T rows in 16–20 of 30 answers. E0 cut the RAW
readers (arm B's pack); E2 cuts the RECORD readers (the record-only arm's
tools, ``scripts/bakeoff/arm_open.py`` ``RECORD_TOOLS``) and ``live_series``.

``known_at`` is a RECORD-time cut (when a row entered the substrate), not the
validity-time ``as_of`` of :func:`legba.data.provenance.origin.as_of_gate_sql`
and not the decision-time ``believed_as_of``. A different parameter name for a
different clock, as the temporal leaf requires. Where a record reader already
has a clock of its own, the cut also CLAMPS that clock to K (a planner asking
for an ``as_of`` after K reads K), so the reader cannot be steered past it.

Every tool of each evaluated surface is classified here, EXACTLY ONCE, as:

``CUT``     the reader takes ``known_at`` and applies it (how: :data:`KNOWN_AT_CUTS`);
``EXEMPT``  the reader carries no record rows; why is written down.

Three tables, one per surface, merged into :data:`KNOWN_AT_CUTS`:
:data:`RAW_KNOWN_AT_CUTS` (arm B's raw pack), :data:`RECORD_KNOWN_AT_CUTS`
(the record-only arm) and :data:`LIVE_KNOWN_AT_CUTS` (``live_series``, a
product tool no frozen arm loads). The dispatcher
(:func:`legba.data.analysts.consult_tool_routing._dispatch_tool`) REFUSES,
under a ``known_at``, any tool not classified here — so a new reader cannot
silently leak post-K rows into an arm. Tests pin each surface's tool set to
its table (``tests/data_pkg/test_origin_gate_inventory.py`` part 4), so the
classification cannot go stale.

Nothing in the product passes ``known_at``: only the evaluation harness does
(``scripts/bakeoff/arm_open.py``: ``B-open-asof`` and the record arms
``C-record`` / ``C-record-2``). With it absent every reader's SQL and result
are byte-identical to before (sha-pinned in the tests).

LIMITS OF THE CUT (declared, not hidden). It is a ROW cut on record entry.

* A row that entered before K but was EDITED in place after K is read with
  today's values: the evidence archiver's full-text re-fetch (raw readers); an
  event's title, summary, severity, confidence, geo and locations (the event
  aggregator rewrites them); an entity profile's name, type and geo; a fact's
  value, confidence and contention flags; a situation's name, status and
  intensity inside an event dossier. Timestamps after K on a returned row are
  masked to ``None`` and counted, and the clocks that have an append-only
  ledger are rebuilt at K: an event's lifecycle state (``event_lifecycle_events``),
  its signal and source counts (``signal_event_links``), an entity's version
  (``entity_profile_versions``).
* Supersession is read at K where the table dates it: a finding superseded
  after K is a live head at K (``superseded_at``); a fact or an event closed
  after K holds at K (``valid_until``). A close recorded after K but dated
  BEFORE K (a back-dated ``valid_until``) reads as closed — today's knowledge
  of a past close; how often that happens is not measured. An entity merged
  after K is hidden (``merged_into`` carries no date). An event edge is
  admitted on its record time and ``valid_until``; its ``superseded_by`` is
  undated and ignored under the cut.
* Navigation and approval state is today's: the target roster
  (``list_targets``, exempt), the source roster and cadence profiles
  (``list_sources``), which history collections are ``loaded`` (series reads),
  and a target's member desks (``list_findings``' empty-target widening).
* Verdicts fold as of the read's instant (``produced_at``);
  ``fold_verdicts='latest'`` (today's verdict) is refused under a cut, and so
  is ``list_findings``' survey mode (it reads today's live heads only).

Pure: no imports beyond the standard library (read by the data-layer dispatcher
and by the runtime readers alike).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CUT",
    "EXEMPT",
    "KNOWN_AT_CUTS",
    "KNOWN_AT_VERSION",
    "KnownAtCut",
    "LIVE_KNOWN_AT_CUTS",
    "RAW_KNOWN_AT_CUTS",
    "RECORD_KNOWN_AT_CUTS",
    "cut_tools",
]

#: Stamped on every harness record that ran under the cut.
KNOWN_AT_VERSION = "2026-11/e2.1"

CUT = "cut"
EXEMPT = "exempt"


@dataclass(frozen=True)
class KnownAtCut:
    """How one reader honours ``known_at`` (or why it need not)."""

    kind: str
    how: str


_LIST_TARGETS = KnownAtCut(
    EXEMPT,
    "Navigation roster (target descriptor heads: id, name, geo, tags). "
    "It returns no corpus row and no ref; a target opened after K is "
    "visible by name only.",
)

#: The record-entry anchor every cut confirms against: a row is IN THE RECORD
#: at K when its ``created_at`` is strictly before K (the half-open window of
#: ``substrate_temporal.anchor_window_clause``). ``signals`` for corpus rows,
#: ``analyst_outputs`` for a finding id ``read_document`` falls back to.
RAW_KNOWN_AT_CUTS: dict[str, KnownAtCut] = {
    "search_signals": KnownAtCut(
        CUT,
        "SQL: signals.created_at < K in the WHERE (anchor_window_clause).",
    ),
    "search_corpus": KnownAtCut(
        CUT,
        "Index range fetched_at < K in the OpenSearch query, then every hit's "
        "id is confirmed against signals.created_at < K; a hit that fails or "
        "has no record row is dropped and counted.",
    ),
    "read_document": KnownAtCut(
        CUT,
        "The id (and any canonical twin the read resolves to) must be a "
        "signals or analyst_outputs row with created_at < K, checked before "
        "and after the read; otherwise no body is returned.",
    ),
    "vector_search": KnownAtCut(
        CUT,
        "Qdrant carries no record time, so the search over-fetches and every "
        "hit is confirmed against signals.created_at < K; the first `limit` "
        "survivors are returned and the drop is counted.",
    ),
    "list_sources": KnownAtCut(
        CUT,
        "SQL: newest signal per source over signals.created_at < K, poll "
        "outcomes with occurred_at < K, staleness judged at K. The source "
        "roster and learned cadence profiles are today's (declared).",
    ),
    "list_targets": _LIST_TARGETS,
}

#: The record-only arm's readers (E2). Each anchors on the record-entry column
#: of the table it hands ids out of (``created_at``; ``loaded_at`` for
#: ``observations``) and clamps its own clock, if it has one, to K. The
#: machinery is :mod:`legba.runtime.substrate_record_known_at`.
RECORD_KNOWN_AT_CUTS: dict[str, KnownAtCut] = {
    "list_findings": KnownAtCut(
        CUT,
        "SQL: analyst_outputs.created_at < K. Without include_superseded the "
        "live-head gate becomes the decision predicate at min(believed_as_of, "
        "K) (the heads live at K, including ones superseded since); with it, "
        "every finding recorded before K. The critic fold is dated to the same "
        "instant, since_hours counts back from K, survey mode is refused.",
    ),
    "belief_as_of": KnownAtCut(
        CUT,
        "SQL: analyst_outputs.created_at < K, and as_of clamped to "
        "min(as_of, K) for the decision predicate and the verdict fold. "
        "fold_verdicts='latest' (today's verdict) is refused.",
    ),
    "query_facts": KnownAtCut(
        CUT,
        "SQL: facts.created_at < K. The open read becomes the validity read at "
        "min(as_of, K) (facts that held at K, including ones closed since), "
        "with include_origin as on the as_of path.",
    ),
    "inspect_entity": KnownAtCut(
        CUT,
        "SQL: the profile must have created_at < K (else not found); versions, "
        "signal mentions (link and signal) and facts (validity at K) recorded "
        "before K; version rebuilt from the newest version row before K, the "
        "profile's other moving fields masked.",
    ),
    "query_events": KnownAtCut(
        CUT,
        "SQL: events.created_at < K; the open read becomes the validity read "
        "at min(as_of, K). Lifecycle state (filter and value) is the newest "
        "ledger row recorded before K; signal/source counts are recounted over "
        "links recorded before K; timestamps after K are masked.",
    ),
    "inspect_event": KnownAtCut(
        CUT,
        "SQL: the event must have created_at < K (else not found); every "
        "section (signals: link and signal; actors; edges; situations; ledger) "
        "is recorded before K; the event's lifecycle and counts are rebuilt at "
        "K and its timestamps after K masked.",
    ),
    "series_history": KnownAtCut(
        CUT,
        "SQL: observations.loaded_at < K, and the RECORD-time as_of clamped to "
        "min(as_of, K) (the provider revision published by K).",
    ),
    "series_compare": KnownAtCut(
        CUT,
        "As series_history: observations.loaded_at < K and as_of clamped to "
        "min(as_of, K).",
    ),
    "list_targets": _LIST_TARGETS,
}

#: Product readers outside the frozen arms that honour the cut as well.
LIVE_KNOWN_AT_CUTS: dict[str, KnownAtCut] = {
    "live_series": KnownAtCut(
        CUT,
        "SQL: observations.loaded_at < K and record_time <= K on both legs "
        "(the catalogue and the window read): a revision, or a series, first "
        "recorded after K is not returned; the catalogue's newest-observation "
        "time is the newest one before K.",
    ),
}

#: Every classified tool. A tool on two surfaces carries ONE classification
#: (the same object), so the merge cannot disagree with itself.
KNOWN_AT_CUTS: dict[str, KnownAtCut] = {
    **RAW_KNOWN_AT_CUTS, **RECORD_KNOWN_AT_CUTS, **LIVE_KNOWN_AT_CUTS,
}


def cut_tools() -> frozenset[str]:
    """The tools that take ``known_at`` (classified ``CUT``)."""
    return frozenset(t for t, c in KNOWN_AT_CUTS.items() if c.kind == CUT)
