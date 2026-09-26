# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The GATHER tool SURFACE — what the loop offers, and how it reads results.

EXTRACTED 2026-09-16 from ``inline_target.py``, which was at its module-size
ceiling (3915/3915) when the native-tool-rounds lane needed room. This is the
author's own seam, not an arbitrary cut: everything here answers one question —
*which tools exist, how are they described to the model, and which of their
results are citable* — and none of it touches the slice, the LLM, the citation
index or the finding. The GATHER LOOP stayed behind in ``inline_target``; only
its surface moved.

Every line below is the relocated original, byte for byte. The prompt strings
in particular are load-bearing beyond this file: ``_GATHER_SYSTEM_SUFFIX`` is
read by ``scripts/render_prompt_pack.py`` and is the text half of the very
protocol the native channel now replaces, so a "harmless" rewording here would
change live prompts. Change them deliberately or not at all.

WHY A TOOL NAME LIVES IN TWO PLACES. ``_GATHER_TOOLS`` says a name is
RECOGNIZED; ``_GATHER_READ_TOOLS`` says it routes through the read binding
rather than its own pack's. Membership in neither grants anything — the
three-way gate inside ``Agency.run_pack_tool`` decides every admission. The
native protocol offers the model exactly the union the loop can route, which is
what keeps the two channels describing the same surface.

``inline_target`` imports every name back and re-exports it, so
``from legba.data.analysts.inline_target import _GATHER_TOOLS`` (tests, the
journal assessor, the prompt-pack renderer) keeps working unchanged.
"""

from __future__ import annotations

from typing import Any, Mapping


_MAX_TITLE_CHARS = 200
# Bounds on the GATHER-gathered [N] evidence rendered into the synthesis prompt
# (Piece 1). A broad search_corpus can return 20+ rows; rendering them all as full
# [N] blocks balloons the prompt so large the CORE plane returns an EMPTY synthesis
# completion. Cap the citable/rendered gathered set + keep each block's preview
# short (the FULL raw source_text stays in the citation entry for the verify judge).
_GATHER_MAX_CITED_SIGNALS = 8
_GATHER_BLOCK_SNIPPET_CHARS = 500


# S5 — GATHER phase tuning.
#
# Default ONE round (vs consult's 6): the cadence assessors run under the P-1
# ~180s invoke timeout + a tight per-day token budget. ``_GATHER_ROUNDS_CEILING``
# is the hard clamp the runner applies regardless of what a descriptor requests,
# so a mis-set ``method.gather.max_rounds`` can never grind forever inside a
# cadence tick.
_GATHER_DEFAULT_ROUNDS = 1
_GATHER_ROUNDS_CEILING = 6

# Soft latency guard: when this much of the descriptor's invoke timeout has
# already elapsed, stop opening new GATHER rounds and go straight to synthesis
# so the run always lands a finding inside the P-1 window (degrade-not-drop).
_GATHER_TIMEOUT_BUDGET_FRACTION = 0.6

# The GATHER tool surface, appended to the assessor's system prompt only for the
# tool-call turns. Mirrors consult's tool catalogue (the same ``substrate_read``
# pack), but the loop protocol is GATHER-shaped: a tool call OR a single
# ``{"done": true}`` to proceed to synthesis. The final FINDING is produced by
# the existing one-shot REASON+ACT call (NOT here), so the assessor's finding
# schema and prompt stay unchanged — GATHER only enriches the context.
#
# SEAM #22: the read surface is ALWAYS described; the external (``web_access``)
# and write-back (``propose_facts``) tool guidance is spliced in ONLY when the
# running assessor is bound to those packs — see ``_gather_system_suffix``. The
# splice text is lifted verbatim from each pack descriptor's
# ``prompt_fragments`` + ``rules`` (the operator-authored tool-use guidance), so
# the in-run instruction tracks the descriptor, not a hardcoded copy that can
# drift from it.
_GATHER_SYSTEM_SUFFIX = (
    "\n\nBefore you write the finding you may FIRST query the substrate to "
    "ground your assessment. Each query must be a single strict-JSON object.\n"
    "Available tools:\n"
    "  - search_signals(query, [limit]) — full-text signal search (title + summary).\n"
    "  - search_corpus(query, [filters], [size]) — BM25 keyword search over the "
    "FULL raw body of every ingested signal (the whole corpus, not the recent "
    "slice); a row's id is the signal id. Use it to FIND source documents.\n"
    "  - read_document(doc_id) — the FULL stored body of ONE signal by its id "
    "(a search_corpus / search_signals row id) when you need the whole article.\n"
    "  - query_facts([subject], [predicate], [value], [limit], [as_of]) — fact "
    "store; as_of (ISO-8601) reads the facts that held on that date.\n"
    "  - inspect_entity(name) — entity profile + recent facts.\n"
    "  - query_nexuses([subject], [object], [rel_type], [polarity], [limit], "
    "[as_of]) — open signed/typed relationships.\n"
    "  - query_hypotheses([target_id], [status], [situation_id], [limit]) — "
    "competing-hypothesis (ACH) rows.\n"
    "  - get_timeline(subject, [limit], [since], [until]) — time-ordered facts "
    "∪ signals.\n"
    "  - compare_targets(target_ids) — side-by-side substrate rollup.\n"
    "  - query_paths(subject, object, [max_hops<=3], [polarity_product], "
    "[limit], [families], [as_of]) — ranked signed paths A->...->B over the "
    "open entity graph; families=['cooccurrence'] walks the co-mention cloud "
    "deliberately.\n"
    "  - find_proxy_chains(subject, object, [max_hops<=3], [polarity_product], "
    "[limit], [families], [as_of]) — INDIRECT links only (multi-hop chains + "
    "reified A->via->B cut-outs).\n"
    "  - query_brokers(camp_a, camp_b, [max_hops<=3], [limit], [families], "
    "[as_of]) — entities on paths between two entity sets, ranked by how many "
    "A->B paths run through them.\n"
    "  - list_findings([target_id], [analyst_id], [severity], [since_hours], "
    "[include_superseded], [believed_as_of], [limit]) — the platform's OWN prior LIVE "
    "assessments/findings (analyst products; superseded revisions are "
    "excluded unless include_superseded=true). Check these FIRST to build on "
    "and reconcile against earlier work; cite the output_id. "
    "effective_confidence already folds in the critic.\n"
    "  - list_situations([status], [target_id], [since_hours], [as_of], "
    "[limit]) — ongoing situation frames the platform has clustered (analysis-derived). "
    "Use a situation_id with query_hypotheses to pull its ACH rows.\n"
    "  - query_events([target_id], [geo], [category], [lifecycle_state], "
    "[entity], [situation_id], [since], [until], [as_of], [limit]) — "
    "bounded occurrences the platform has clustered (a thing that HAPPENED, "
    "where a situation is a thing being watched). lifecycle_state is one of "
    "emerging/developing/active/evolving/resolved; geo is an ISO2 code or "
    "list; entity is an actor-name substring; situation_id returns the "
    "events a situation tracks; since/until bound the occurrence span by "
    "overlap; as_of reads the events that held on that date.\n"
    "  - inspect_event(event_id) — the one-event dossier: ranked evidence "
    "signals, actors with roles, event edges, tracking situations, and the "
    "lifecycle ledger oldest→newest.\n"
    "  - series_history(series_id, subject, from, to, [as_of], "
    "[collection_id], [limit]) — one curated HISTORICAL series for one "
    "subject (ISO2) over a valid-time window. from/to are REQUIRED and bound "
    "the period the numbers are ABOUT; as_of bounds the provider's RECORD "
    "time (which revision had been published by then), which is a different "
    "question. Rows are HISTORY, never current reporting.\n"
    "  - series_compare(series_id, subjects, from, to, [as_of], "
    "[collection_id], [limit]) — the same historical series across several "
    "subjects in ONE call. A subject the holding does not carry returns no "
    "rows and is named in subjects_with_no_rows — never a zero.\n"
    "  - query_predictions([target_id], [status], [limit]) — the platform's "
    "event-volume forecasts (forecast_method='naive_mean' means no trend could "
    "be fit, low-confidence; 'auto_arima' means fitted). The feed is FROZEN "
    "(writer retired 2026-07-01) — rows are historical, never present one as "
    "a current forecast; cite the output_id.\n\n"
    "Protocol:\n"
    '  - To query, reply with strict JSON: {"tool": "<name>", "args": {...}}\n'
    '  - When you have gathered enough, reply with: {"done": true}\n'
    "  - Do not write the finding yet — you will be asked for it after gathering.\n"
    "  - The full-text source documents that search_corpus / read_document return "
    "are added to your context NUMBERED [N] (continuing after the input signals). "
    "In the finding you write next, cite each numbered source you rely on with [N] "
    "exactly like the input signals, and list that source's signal id in `evidence`."
)

# SEAM #22 — external + write tool guidance, spliced into the GATHER suffix only
# when the matching pack is EFFECTIVE for this (assessor, target) run. The
# descriptions name the tool signatures; the operator-authored tool-use rules
# (cite-the-URL / require derived_from / propose-not-assert) come from the pack
# descriptors via ``_gather_system_suffix``.
_WEB_TOOLS_SUFFIX = (
    "\n\nEXTERNAL EVIDENCE (web_access pack — egress is SSRF-guarded; a blocked "
    "host is a clean tool failure, not a crash):\n"
    "  - web_search(query, [limit]) — query the operator-pinned search endpoint; "
    "returns {title, url, snippet} results.\n"
    "  - web_fetch(url) — GET one absolute http(s) URL through the guarded "
    "transport; returns its (capped) text body.\n"
)
# R-A — the `research` pack's signature block. SEPARATE from the web_access
# block above because the two packs are granted independently: an assessor may
# hold either, both or neither, and describing a tool it cannot call is how a
# planner burns a GATHER round on a `tool_unbound` no-op.
_RESEARCH_TOOLS_SUFFIX = (
    "\n\nOUTBOUND RESEARCH (research pack — this tool WRITES: every hit it "
    "keeps is landed in the substrate as evidence, tagged with where it came "
    "from, and is permanent):\n"
    "  - web_evidence(query, [limit], [fetch], [hypothesis_id]) — search the "
    "open web and land what comes back. Returns `rows` (hits whose publisher "
    "licence cleared a full-text fetch: these are added to your context "
    "NUMBERED [N] exactly like a corpus document — cite them and list their "
    "signal ids in `evidence`) and `teaser_hits` (title/url/snippet only, NO "
    "body — name them in prose, NEVER cite one with [N]). Pass "
    "`hypothesis_id` when you are draining a standing question: it is what "
    "lets the evidence reach the desk that asked.\n"
)
_WRITE_TOOLS_SUFFIX = (
    "\n\nWRITE-BACK (propose_facts pack — these PROPOSE, they do NOT assert "
    "truth; every write REQUIRES derived_from lineage citing the substrate "
    "UUIDs it is grounded in):\n"
    "  - propose_fact(subject, predicate, value, derived_from=[uuid,...], "
    "[confidence]) — write one proposed-grade fact (source_type='proposed', "
    "confidence clamped).\n"
    "  - request_source(need, [rationale], derived_from=[uuid,...]) — record a "
    "coverage / evidence gap.\n"
    "  - open_question(question, [counter], derived_from=[uuid,...]) — record an "
    "unresolved analytical question.\n"
)

# GATHER read tools — the substrate_read pack's tool surface (S4).
_GATHER_READ_TOOLS = (
    "search_signals",
    # Stage 1 — OpenSearch full-text corpus readers. Both are in the
    # substrate_read pack (SUBSTRATE_READ_TOOLS); listing them here is what makes
    # the inline_target GATHER loop RECOGNIZE + read-route them, so a corpus-mining
    # analyst (corpus_researcher) can actually search + read the full-text corpus.
    # Their result signals are then numbered [N]-citable (see
    # ``_gathered_signals_from_result``).
    "search_corpus",
    "read_document",
    "query_facts",
    "inspect_entity",
    "vector_search",
    "query_nexuses",
    "query_hypotheses",
    "get_timeline",
    "compare_targets",
    # V3/P3 — the three graph walks, consult-only since P5 (a named defect:
    # no desk could reach the proxy-path / broker reads and E3's demand gauge
    # read it back as six lifetime invocations). Read-only, same pack, same
    # [N]-citable refs discipline as every other substrate read.
    "query_paths",
    "find_proxy_chains",
    "query_brokers",
    # Finished-intelligence reads — the platform's OWN prior products, so an
    # assessor can build on (and reconcile against) earlier assessments rather
    # than re-derive from the raw signal firehose every run.
    "list_findings",
    "list_situations",
    "query_predictions",
    # V3/P6 — the event surface readers (spec §6.1), same pack. Rows are
    # events, not signals — they stay UNnumbered prose summaries, never
    # [N]-citable (the _GATHER_SIGNAL_ROW_TOOLS guard above).
    "query_events",
    "inspect_event",
    # 7g-2 — the COLLECTION series reads, same pack. Rows are OBSERVATIONS,
    # not signals: they stay out of _GATHER_SIGNAL_ROW_TOOLS (their evidence
    # is the provider file behind the number, reached through the row's own
    # source_url + sha256, not a corpus body), and a research run cites one
    # through the `observation:<uuid>` ref the row carries.
    "series_history",
    "series_compare",
)

# SEAM #22 — external (web_access) + write-back (propose_facts) tool names. A
# GATHER round may invoke these ONLY when the runner is passed a per-tool
# binding for the owning pack (``options['gather_tool_bindings']``); the binding
# is built by the host iff the pack is EFFECTIVE (assessor grant ∩ target allow)
# and re-pointed per run by the actor. Read tools route through the default
# ``substrate_read`` binding; these route through their own pack's binding so
# ``Agency.run_pack_tool`` enforces tool↔pack ownership.
_GATHER_WEB_TOOLS = (
    "web_fetch",
    "web_search",
    # R-A: the `research` pack's one tool. Grouped with the web tools because
    # it routes the same way (its OWN pack's per-tool binding, never the
    # substrate_read one) and carries the same egress guarantees — but unlike
    # them it WRITES: each kept hit lands as a signals row, and the ids come
    # back in `rows`, which is what makes a fetched page [N]-citable
    # (_GATHER_SIGNAL_ROW_TOOLS below).
    "web_evidence",
)
_GATHER_WRITE_TOOLS = (
    "propose_fact",
    "request_source",
    "open_question",
)

# The full set the GATHER loop will dispatch. Membership here only means "a
# recognized tool name"; whether a call is actually admitted is decided by the
# three-way gate inside the routed binding's ``run_pack_tool``. A read tool with
# no write/web binding wired simply has no per-tool binding and falls back to the
# substrate_read binding; a write/web tool with no binding is reported as an
# unbound tool (a loud no-op folded back to the planner), never an ungoverned call.
_GATHER_TOOLS = _GATHER_READ_TOOLS + _GATHER_WEB_TOOLS + _GATHER_WRITE_TOOLS


# Piece 1 — GATHER tools whose result ROWS are substrate SIGNALS carrying a REAL
# corpus BODY (a resolvable signal id + doc fields incl raw_body), so each
# newly-seen result signal is numbered and becomes [N]-citable in the finding
# (see ``_gathered_signals_from_result``). ONLY ``search_corpus`` /
# ``read_document`` qualify — they are special-cased below because their doc
# fields (with raw_body) sit under ``source`` / ``document``.
#
# ``_GATHER_SIGNAL_ROW_TOOLS`` (the generic rows-with-id numbering path) is
# DELIBERATELY EMPTY. ``search_signals`` is EXCLUDED even though its rows carry a
# signal id: its Postgres-FTS projection has NO body field (only id/title/category/
# source_url/rank), so a numbered [N] citation to it would carry ``source_text=
# None`` → a TITLE-ONLY citation → a spurious faithfulness DEMOTION for the live
# agentic units that bind substrate_read + gather. So search_signals stays a
# prose-summary tool exactly as before this change (no regression); it already
# returns a ``refs`` list, so its rows still extend lineage the normal way. Every
# OTHER read tool (query_facts / query_nexuses / list_situations / list_findings /
# …) returns facts / relationships / products whose ids are NOT signal ids and
# likewise stay UNnumbered — numbering one would fabricate an ungroundable citation.
#
# R-A (2026-09-05) — the extension point's FIRST member: ``web_evidence``. It
# qualifies on exactly the terms the guard above sets out. Its rows are real
# ``signals`` rows it just LANDED (a resolvable signal id, not a search hit's
# transient handle), and it returns a row ONLY for a hit whose publisher licence
# cleared a full-text fetch — so every numbered [N] carries a real
# ``raw_body``, and a body-less TEASER hit is deliberately excluded from ``rows``
# and reaches the planner as prose in ``teaser_hits`` instead. That is the same
# distinction that keeps ``search_signals`` out, applied by the tool at its own
# source rather than by a special case here.
#
# WHAT THIS FIXES. Before it, a fetched page could not be cited at ALL: the web
# tools return no signal ids, so a [N] marker aimed at one resolved to nothing
# in the render-time index and ``_extract_citations`` COUNTED it and dropped it
# (never a fabricated ref — the drop is correct, the missing index entry was
# the bug). Landing the row and returning its id is what makes the marker
# resolve; no change to the citation parser was needed or made.
_GATHER_SIGNAL_ROW_TOOLS: tuple[str, ...] = ("web_evidence",)


def _render_gathered_block(
    n: int, entry: Mapping[str, Any], fields: Mapping[str, Any] | None,
) -> str:
    """Render ONE [N]-numbered GATHERED source document.

    V-N2 — THIS BLOCK USED TO CARRY NO DATE AT ALL. It rendered as
    ``[N] <title> — <snippet>`` while a slice signal rendered its
    ``ingested=`` / ``published=`` provenance line, and BOTH share one flat
    ``[N]`` numbering space. Two consequences, both real:

      * The D1 dated-claim rule (``_tradecraft._DATED_CLAIM_RULE``) requires
        every load-bearing claim to carry "the date of the reporting that
        supports it, taken from that source's OWN printed date". For a gathered
        document there WAS no printed date, so the rule was unfollowable for
        exactly the evidence a retrieval analyst leans on hardest. The only
        date visible was whatever the prose happened to mention — which is a
        date INSIDE the story, not the date OF the reporting.
      * A document pulled from the ~106k-doc corpus can be years old, and
        nothing distinguished it from a signal collected this morning. The
        model cannot weigh recency it cannot see.

    So the shape now mirrors :func:`_render_signal` field-for-field — same
    labels, same order, same honesty about ingestion-vs-publication — plus a
    RETRIEVED marker naming what this block IS: a document this run went and
    fetched, not part of the cadence slice. The corpus doc carries
    ``fetched_at`` / ``published_at`` at the top level of its OpenSearch
    ``_source`` (see ``data/opensearch.py``); an absent date renders as an
    absent field, never as a fabricated one.
    """
    title = str(entry.get("title") or "(untitled)")[:_MAX_TITLE_CHARS]
    snippet = str(entry.get("snippet") or "")[:_GATHER_BLOCK_SNIPPET_CHARS]
    fetched_at = published_at = None
    if isinstance(fields, Mapping):
        fetched_at = fields.get("fetched_at") or fields.get("produced_at")
        published_at = fields.get("published_at")
    parts = [f"[{n}] {title}", "    RETRIEVED (fetched by this run from the corpus"]
    if fetched_at:
        parts[1] += f"; collected {fetched_at}"
    parts[1] += ")"
    prov = ""
    if published_at:
        prov += f" published={published_at}"
    source = entry.get("source")
    if source:
        prov += f" source={source}"
    if prov:
        parts.append(f"   {prov}")
    parts.append(f"    snippet={snippet}")
    return "\n".join(parts)


def _gathered_signals_from_result(
    tool_name: str, tool_result: Mapping[str, Any],
) -> list[tuple[Any, Mapping[str, Any]]]:
    """Extract ``(raw_signal_id, doc_fields)`` pairs from a SIGNAL-bearing tool
    result, for numbering as [N]-citable gathered citations (Piece 1).

    Per-tool result shapes (ONLY the corpus readers, which carry a REAL body):
      * ``search_corpus`` — ``result['rows']``; each row is
        ``{id, score, source: {…doc fields incl raw_body…}}`` (OpenSearch ``_source``).
      * ``read_document`` — ``{status, doc_id, document: {…doc fields…}}``; mined
        ONLY when ``status == 'found'``.
      * the generic rows-with-id readers (:data:`_GATHER_SIGNAL_ROW_TOOLS`, now
        EMPTY) — reserved extension point; ``search_signals`` is DELIBERATELY not
        here (its FTS rows carry no body → a title-only citation would demote).

    A non-signal tool (query_facts / list_situations / search_signals / …), an
    errored result, or a corpus reader that returned no rows yields ``[]`` — it is
    never numbered (it stays a prose summary in the GATHER preamble).
    """
    if not isinstance(tool_result, Mapping) or "error" in tool_result:
        return []
    out: list[tuple[Any, Mapping[str, Any]]] = []
    if tool_name == "read_document":
        if tool_result.get("status") == "found":
            raw_id = tool_result.get("doc_id")
            doc = tool_result.get("document")
            if raw_id is not None:
                out.append((raw_id, doc if isinstance(doc, Mapping) else {}))
        return out
    if tool_name == "search_corpus":
        for row in tool_result.get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            raw_id = row.get("id")
            if raw_id is None:
                continue
            src = row.get("source")
            out.append((raw_id, src if isinstance(src, Mapping) else {}))
        return out
    if tool_name in _GATHER_SIGNAL_ROW_TOOLS:
        for row in tool_result.get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            raw_id = row.get("id")
            if raw_id is None:
                continue
            # Two row shapes are accepted, and the difference matters: a row
            # that nests its doc fields under ``source`` (``web_evidence``, and
            # ``search_corpus`` above) hands THOSE to the citation builder, so
            # ``raw_body`` is found and the [N] carries real source_text. A
            # FLAT row (the shape this generic path was written for) is passed
            # whole. Reading a nested row as flat would silently produce a
            # title-only citation — the demotion this whole extension point is
            # guarded against.
            src = row.get("source")
            out.append((raw_id, src if isinstance(src, Mapping) else row))
    return out


def _gather_system_suffix(
    *,
    web_fragments: list[str] | None = None,
    write_fragments: list[str] | None = None,
    bound_tools: tuple[str, ...] = (),
) -> str:
    """Build the GATHER system suffix, splicing in external/write guidance.

    The read surface is always present. ``web_fragments`` / ``write_fragments``
    are the owning pack descriptors' ``prompt_fragments`` + ``rules`` (operator
    authored), appended verbatim under the tool-signature block so the in-run
    instruction tracks the descriptor. Empty/None → that section is omitted
    (the pack is not bound for this run), keeping the read-only suffix
    byte-for-byte unchanged for a non-write assessor.
    """
    suffix = _GATHER_SYSTEM_SUFFIX
    if web_fragments is not None:
        # Describe only the egress tools this run can actually reach.
        # ``bound_tools=()`` (every caller that does not pass it — tests,
        # embedders, and the pre-R-A shape) keeps the web_access block
        # unconditional, so the suffix is byte-for-byte what it was.
        if not bound_tools or any(
            t in bound_tools for t in ("web_fetch", "web_search")
        ):
            suffix += _WEB_TOOLS_SUFFIX
        if "web_evidence" in bound_tools:
            suffix += _RESEARCH_TOOLS_SUFFIX
        for frag in web_fragments:
            frag = str(frag).strip()
            if frag:
                suffix += f"  {frag}\n"
    if write_fragments is not None:
        suffix += _WRITE_TOOLS_SUFFIX
        for frag in write_fragments:
            frag = str(frag).strip()
            if frag:
                suffix += f"  {frag}\n"
    return suffix
