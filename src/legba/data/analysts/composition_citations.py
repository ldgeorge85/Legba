# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The composition CITE step — the model's markers become resolved citations.

Extracted from ``meta_findings_synthesizer`` (2026-09-06) at the seam that
module's own size-gate entry named on its way out of the PROMPT-ASSEMBLY train:
*"the CITE resolution (``_extract_ref_markers`` + the ordinal→citation walk +
the continuity / window-ledger / register citation shapes), ~200 lines that read
``finding.body`` and the ordinal index and nothing else."* Three merges landed
on the synthesizer the same night — the rollup citation-ORDER fix, the
carry-by-mass fix and the world-read consistency fix — and together they put the
file 32 lines over its ceiling. The house rule on that ratchet is to take the
named seam, not the number.

What lives here
~~~~~~~~~~~~~~~

* The two MARKER GRAMMARS the model writes and this step reads —
  :data:`_REF_MARKER_RE` (``[[ref:N]]``, a 1-based ordinal) and
  :data:`_CONTESTED_MARKER_RE` (``[[contested:<uuid>]]``) — with the two
  resolvers that hold their shared honesty contract: an out-of-range or
  unlisted marker is DROPPED and COUNTED, never emitted
  (:func:`_extract_ref_markers`, :func:`_extract_contested_markers`).
* :func:`_build_composition_citation` — the ONE citation shape, shared by the
  resolved-marker loop and the A2 unmarked-basis fallback — and the two
  constants that bound it: :data:`MAX_EVIDENCE_TEXT_CHARS` (how wide a window of
  the cited body the verify pass is handed) and
  :data:`_FALLBACK_BASIS_CITATIONS_CAP`.
* :func:`resolve_composition_citations` — ``_run``'s whole ``--- CITE ---``
  block: the ordinal INDEX (basis → periphery → continuity, or the rollup's own
  roster order), the walk that turns each resolved ordinal into a citation
  through the window-ledger / situation-register / prior-read shapes, the A2
  fallback and the ``cite`` trace step.
* :func:`_coerce_uuid` — a zero-dependency leaf whose two heaviest readers are
  the resolvers above. It moved WITH them rather than being injected, because
  that is what keeps this module's imports strictly one-directional; it is
  re-exported, so all twenty of the synthesizer's own call sites and every test
  reaching for ``synth._coerce_uuid`` resolve unchanged.

Everything here is imported ONE WAY by ``meta_findings_synthesizer`` and
RE-EXPORTED from it, so ``synth._extract_ref_markers``,
``synth._build_composition_citation``, ``synth.MAX_EVIDENCE_TEXT_CHARS`` and
every other historical name resolves unchanged for every importer and every
test. ``__all__`` on the synthesizer is byte-identical across the move.

Why the register renderer is INJECTED
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``_render_situation_register_lines`` is the one name in the moved block that a
test patches ON THE SYNTHESIZER (``test_composer_prompt_block_equivalence``
corrupts it via ``monkeypatch.setattr(synth, ...)`` to prove the register never
reaches a composer prompt). Resolving it in THIS module's globals would make
that patch invisible to the walk below — the exact failure the PROMPT-ASSEMBLY
extraction paid for with its ``PromptRenderers`` bundle, and the one D-2 paid
for the assemblers' basis gather with ``basis_reader``. It arrives as a
parameter that ``_run`` fills from ITS OWN namespace at the call site, so the
lookup happens where it always happened.

No log line moved with this unit, so there is deliberately no ``getLogger``
here. If one is ever added, it must be pinned to the SYNTHESIZER's logger name
(``logging.getLogger("legba.data.analysts.meta_findings_synthesizer")``, the
call ``composition_coercion`` makes) — a moved log line that silently
re-attributes itself breaks every filter pointed at it.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from ..provenance.event_citations import (
    event_citations_enabled,
)
# V3/P2 — the [[event:<uuid>]] grammar + expansion moved to a leaf when the
# additions pushed this module past its ceiling; imported back ONE WAY.
from .composition_event_citations import _expand_event_markers
from . import region_rollup as _rollup
from .composition_window import PERIPHERY_TIER, _EVIDENCE_TIER_KEY
# 7d — the independence count behind a quoted claim (the ``single_source`` /
# ``wire_folded`` stamps below). Leaf, stdlib-only, one way.
from .source_independence import head_citations, independence_of
from .window_ledger import (
    CONTINUITY_CITATION_KEY,
    CONTINUITY_PRIOR,
    CONTINUITY_ROW_KEY,
    CONTINUITY_SITUATIONS,
    CONTINUITY_WINDOW_LEDGER,
    SITUATION_REGISTER_EVIDENCE_CHARS,
    SITUATION_REGISTER_REF_KIND,
    _iso_text,
    window_ledger_citation,
)


# ---------------------------------------------------------------------------
# Row-id coercion (moved from ``meta_findings_synthesizer``'s "Helpers — input
# shaping" section 2026-09-06 with the two readers below; re-exported there)
# ---------------------------------------------------------------------------


def _coerce_uuid(raw: Any) -> UUID | None:
    """Best-effort coerce of a row id into a UUID, swallowing malformed ids."""
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


# P3-T3/T7 — how much of a cited sub-claim's body to capture on its citation as
# ``evidence_text`` at synth time, so the composition faithfulness VERIFY (run in
# a LATER actor step) checks each composed clause against the EXACT point-in-time
# evidence the model saw — no verify-time re-fetch (which could read a superseded
# sub-claim).
#
# F-D (2026-08-03): raised 600 -> 3600, matching the UNIT judge's whole-evidence
# bound (``verify._EVIDENCE_TOTAL_CHARS``). At 600 the cap BOUND on essentially
# every composition citation in production — measured read-only on the live
# substrate, country_composition citations averaged 567 of 600 chars against
# cited bodies averaging 2,352 — so the judge graded the whole composition tower
# against roughly the first quarter of each sub-claim, and a composed clause
# resting on anything the cited finding said after its BLUF read as ungrounded.
# The composition citation now carries the same evidence window a unit citation
# does. (The 08-03 panel reported this as "0 of 551 citations carry resolvable
# text"; that measurement read ``source_text``/``snippet``/``body``, which is the
# UNIT citation shape. Composition citations carry ``evidence_text`` and 542 of
# 542 had it — the defect was never absence, it was the width of the window.)
#
# P0c (2026-09-12 pre-round, INSTRUMENT_PLAN B1): raised 3600 -> 4000, EQUAL to
# :data:`MAX_FULL_BODY_CHARS`. The renderer showed up to 4000 chars of a body
# but this capture stopped at 3600 — a body in that 400-char gap was SHOWN and
# not GRADED, so a span cut from the gap passed construction (D-1 §3.6 checks
# the true, untruncated body) but hard-failed D-3's DB-free ARM 1 audit as
# ``quote_origin_truncated`` (see ``assembly_arms.py``'s P0c banner). Quote
# fidelity is an invariant (G2): one false positive there is a MISS. Live
# exposure: 2 of ~6,500 desk bodies / 14 days (max 3,649; 0 over 4,000) — the
# fix raises the capture rather than lowering the render, so no live body
# shrinks. Pinned equal to ``MAX_FULL_BODY_CHARS`` (and transitively to
# ``verify._EVIDENCE_TOTAL_CHARS``) by ``test_composition_evidence_window.py``.
MAX_EVIDENCE_TEXT_CHARS: int = 4000

# A ``[[ref:N]]`` marker — a 1-BASED ORDINAL (small int) naming the position of
# the cited sub-claim in the rendered bundle. The composition prompt asks the
# model to cite each factual clause with one of these, using EXACTLY the small
# integer N stamped at the START of the sub-claim block it rests on. An ordinal is
# a 1-2 digit int the model copies RELIABLY (mirroring the unit ``[N]`` → Nth
# signal contract) — whereas a raw 36-char uuid was copied UNRELIABLY (the world
# run fabricated all 10, scoring the composition 0.0). Post-generation we keep only
# markers whose N is in ``[1, len(sliced)]`` and DROP (never emit) any out-of-range
# (fabricated) one — honesty by construction. Wrapped ``[[ref:...]]`` so verify's
# syntax discriminator still tells a composition marker from a unit ``[N]`` (the
# two regexes are provably disjoint — ``\[(\d+)\]`` never matches ``[[ref:5]]`` and
# ``\[\[ref:`` never matches ``[5]``).
_REF_MARKER_RE = re.compile(r"\[\[ref:(\d+)\]\]")

# A ``[[contested:<uuid>]]`` marker (T4, world composition only) — the
# contention_id of an open public.fact_contention dispute the model was shown in
# the CONTESTED FACTS block. Post-generation we keep only markers whose id is in
# the assembled group-id set and DROP (never emit) any fabricated/unlisted one,
# so the world read can never surface a "contested group" it was not fed. The
# real contention_id lets the UI resolve it through the existing
# GET /api/v1/contention?group=<id> read (substrate_reads_api._hydrate_contention).
_CONTESTED_MARKER_RE = re.compile(r"\[\[contested:([0-9a-fA-F-]{36})\]\]")


def _extract_ref_markers(
    body: str,
    num_subclaims: int,
) -> tuple[list[int], int]:
    """Resolve the ``[[ref:N]]`` ordinal markers in ``body`` against the slice RANGE.

    Returns ``(resolved_ordinals, dropped_count)``:

      * ``resolved_ordinals`` — the DISTINCT 1-based ordinals ``N`` that appear as
        ``[[ref:N]]`` markers AND lie in ``[1, num_subclaims]`` (i.e. point at a
        real sub-claim block in the rendered bundle), in first-appearance order.
      * ``dropped_count`` — the number of DISTINCT markers whose ``N`` is OUT OF
        RANGE (``< 1`` or ``> num_subclaims``) — a fabricated handle. These are
        counted for observability and NEVER emitted — the composition never
        surfaces a citation it cannot ground in a rendered sub-claim. Copying a
        1-2 digit int is reliable, so a dropped ordinal is far rarer than the raw
        uuid it replaced, but the drop-and-count honesty contract is preserved.

    ``N`` is the ordinal position in the (already ORIENTed + trimmed) ``sliced``
    list — the SAME ``enumerate(sliced, start=1)`` index the render stamps and the
    CITE block re-derives, so ``N`` ⇒ ``sliced[N-1]`` with no drift.
    """
    resolved: list[int] = []
    seen: set[int] = set()
    dropped = 0
    for match in _REF_MARKER_RE.finditer(body or ""):
        n = int(match.group(1))
        if n in seen:
            continue
        seen.add(n)
        if 1 <= n <= num_subclaims:
            resolved.append(n)
        else:
            dropped += 1
    return resolved, dropped


def _extract_contested_markers(
    body: str,
    allowed_ids: set[str],
) -> tuple[list[str], int]:
    """Resolve ``[[contested:<uuid>]]`` markers in ``body`` against ``allowed_ids``.

    Same honesty contract as :func:`_extract_ref_markers` (DISTINCT, canonical,
    first-appearance order; fabricated/unlisted markers DROPPED + counted, never
    emitted). ``allowed_ids`` is the set of contention_ids the model was shown in
    the CONTESTED FACTS block — so the world read can only mark a dispute the
    arbiter actually surfaced, and its ``[[contested:<id>]]`` always resolves
    through the existing /api/v1/contention read.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for match in _CONTESTED_MARKER_RE.finditer(body or ""):
        raw = match.group(1)
        canon = _coerce_uuid(raw)
        key = str(canon) if canon is not None else raw
        if key in seen:
            continue
        seen.add(key)
        if canon is not None and str(canon) in allowed_ids:
            resolved.append(str(canon))
        else:
            dropped += 1
    return resolved, dropped


# A2 (verify-path structural fix, 2026-07-31) — bound on the UNMARKED-BASIS
# citation fallback (see ``_run``'s CITE block): when the model's ``[[ref:N]]``
# prose resolves to NOTHING despite a real basis, cite the basis directly rather
# than shipping an empty citations array. Capped so the rare fallback can't
# balloon the payload with the per-citation ``evidence_text``.
_FALLBACK_BASIS_CITATIONS_CAP = 25


def _build_composition_citation(
    n: int, src_row: Mapping[str, Any],
) -> dict[str, Any] | None:
    """One resolved composition-citation entry for basis/periphery row
    ``src_row`` at ordinal ``n``.

    Shared by the resolved-``[[ref:N]]`` loop and the A2 unmarked-basis fallback
    in :func:`_run`'s CITE block — the SAME shape either way (only the caller
    decides whether to stamp ``"resolution": "fallback_basis"``). Returns
    ``None`` when the row carries no resolvable drill-target id (never a
    fabricated ref, mirroring the unit path's malformed-id handling).
    """
    uid = _coerce_uuid(src_row.get("id"))
    if uid is None:
        return None
    citation: dict[str, Any] = {
        "marker": f"[[ref:{n}]]",
        "ordinal": n,
        "ref_id": str(uid),
        "ref_kind": "finding",
    }
    # C-TIER: a citation resolving into the PERIPHERY section carries its tier
    # so the verify pass can require hedged attribution on any clause resting
    # only on it. Basis citations are byte-identical (no key).
    if src_row.get(_EVIDENCE_TIER_KEY) == PERIPHERY_TIER:
        citation["tier"] = PERIPHERY_TIER
    src = src_row.get("analyst_id")
    if src:
        citation["source"] = str(src)
    # S2-T4: the cited head's DESK (target_id) — names which desk a clause
    # rests on (the thematic prompt cites by desk) and keys the cross-desk
    # correlation guard's audit. Additive; absent on a target-less block.
    tgt = src_row.get("target_id")
    if tgt is not None:
        citation["target_id"] = str(tgt)
    title = src_row.get("title")
    if title is not None:
        citation["title"] = str(title)
    # D-2b — the origin head's TIMESTAMP, which ARM 3 compares against the block's
    # own. Guarded like ``title``: absent it, the arm declines the date field
    # (``assembly_attribution_date_unverifiable``) rather than passing an invention.
    if produced_at := _iso_text(src_row.get("produced_at")):
        citation["produced_at"] = produced_at
    # P3-T3/T7 — capture the sub-claim's EVIDENCE the verifier needs, point-in-
    # time, so the composition verify runs DB-free. ``data`` is open JSONB so
    # all three keys are additive.
    #   * evidence_text     — the cited sub-claim's body (judge evidence).
    #   * effective_confidence — the verify-floored min(conf, faithful) the
    #     reader surfaced (the T7 hedge/cap ceiling). Guarded: a row with no
    #     eff score is simply omitted → never falsely capped.
    #   * derived_from      — the sub-claim's underlying lineage/signal ids
    #     (the T7 shared-lineage / double-count detector).
    citation["evidence_text"] = str(src_row.get("body") or "")[
        :MAX_EVIDENCE_TEXT_CHARS
    ]
    eff = src_row.get("effective_confidence")
    if eff is not None:
        try:
            citation["effective_confidence"] = float(eff)
        except (TypeError, ValueError):
            pass
    citation["derived_from"] = [
        str(u) for u in (src_row.get("derived_from") or [])
    ]
    # 7d — WHAT IS ACTUALLY BEHIND THIS QUOTED CLAIM. Computed off the cited
    # head's OWN cited signals, the identical list ``assembly_payload``'s
    # corroboration block reads, so the citation and the rendered record
    # cannot disagree about a block. Both keys are written only when TRUE:
    # a claim on two independent sources is byte-for-byte the citation that
    # shipped before this, and a composition origin (which cites findings, not
    # signals) is UNKNOWN and therefore unmarked rather than falsely cleared.
    independence = independence_of(head_citations(src_row))
    if independence.single_source:
        citation["single_source"] = True
    if independence.folded:
        citation["wire_folded"] = True
    return citation


async def resolve_composition_citations(
    *,
    finding: Any,
    steps: list[dict[str, Any]],
    sliced: Sequence[Mapping[str, Any]],
    periphery_sel: Sequence[Mapping[str, Any]],
    prior_row: Mapping[str, Any] | None,
    ledger_row: Mapping[str, Any] | None,
    ledger_entries: Sequence[Mapping[str, Any]],
    register_row: Mapping[str, Any] | None,
    register_situations: Sequence[Mapping[str, Any]],
    rollup_payload: Mapping[str, Any] | None,
    render_situation_register_lines: Callable[
        [Sequence[Mapping[str, Any]], int], Sequence[str]
    ],
    # V3/P2 — a substrate connection (pool-or-conn, duck-typed .fetch) so
    # [[event:<uuid>]] markers can expand through signal_event_links.
    # None (every pre-P2 carrier) → the tokens stay ordinary prose.
    conn: Any = None,
) -> tuple[list[dict[str, Any]], list[int]]:
    """``_run``'s CITE phase: resolve ``[[ref:N]]`` against the rendered slice.

    Moved verbatim from ``meta_findings_synthesizer._run`` (2026-09-06), one
    indent level out of its ``if is_composition:`` guard, which STAYS at the
    call site — this function is only ever entered on the composition path.

    Resolve the model's inline ``[[ref:N]]`` ORDINAL markers against the rendered
    slice RANGE: ``N`` maps to ``sliced[N-1]`` (the SAME ``enumerate(sliced,
    start=1)`` order the render stamped, so ordinal N == the Nth sub-claim ==
    the Nth ``derived_from`` entry — no drift). Only in-range ordinals become
    citations — an out-of-range (fabricated) handle is DROPPED (counted, never
    emitted). Each citation carries ``ref_id`` (the cited FINDING uuid — the
    correct drill target) + ``ref_kind='finding'`` (the kind-aware discriminator)
    + ``ordinal`` (the deterministic resolution key) so a LATER stage can run a
    faithfulness verify over the composition itself.

    ``finding`` and ``steps`` are MUTATED exactly as the inline block mutated
    them (``finding.data['citations']`` is set; one ``cite`` step is appended).
    Returns ``(citations, resolved_ordinals)`` — the two values the sections
    below the block read: the SALIENCE CHECK wants the resolved ordinals, the
    CORRELATION GUARD wants the citations.
    """
    # C-TIER: the ordinal space spans basis THEN periphery (the same order
    # the render stamped), so a periphery citation resolves like any other
    # — the ``tier`` stamp below is what tells the verify pass apart.
    # CONTINUITY extends that ONE space by up to three more blocks, in the
    # SAME order ``_render_continuity_block`` emitted them (prior read, then
    # window ledger, then register), so ordinal N still means "the Nth
    # rendered block" with no drift. This sequence and that render are the
    # one place the memory order is written down; they must move together.
    continuity_seq: list[Mapping[str, Any]] = []
    if prior_row is not None:
        continuity_seq.append(prior_row)
    if ledger_entries and ledger_row is not None:
        continuity_seq.append(ledger_row)
    if register_situations and register_row is not None:
        continuity_seq.append(register_row)
    num_subclaims = len(sliced) + len(periphery_sel) + len(continuity_seq)
    index_by_ordinal: dict[int, Mapping[str, Any]] = {
        n: row
        for n, row in enumerate(
            (*sliced, *periphery_sel, *continuity_seq), start=1
        )
    }
    # D-5 FIX (2026-09-07) — THE ROLLUP RENDERS IN ROSTER ORDER, NOT SLICE
    # ORDER, so its ordinal space is its own. Every other tier mints
    # ``blocks[i].ordinal = i+1`` off ``carried = sliced[:BLOCK_CAP]``, which
    # is why ``sliced[N-1]`` is the right resolution there and stays
    # untouched below. ``render_rollup_body`` instead walks the region's
    # ROSTER (the denominator it reports against) while the slice arrives
    # salience-ordered: resolving its markers against ``sliced`` labelled
    # Argentina's carried read with the United States' head for every rollup
    # row written between 2026-09-05 and this fix. ONE ordering, owned by the
    # module that renders it (``region_rollup.rollup_ordinal_index``), and
    # the citation content is still built by the SHARED helper off the SAME
    # row objects — only which ordinal names which row changes.
    if rollup_payload is not None:
        index_by_ordinal = _rollup.rollup_ordinal_index(
            rollup_payload, sliced
        )
        num_subclaims = int(rollup_payload.get("members_carried") or 0) + int(
            rollup_payload.get("extra_carried") or 0
        )
    resolved_ords, dropped_refs = _extract_ref_markers(
        finding.body, num_subclaims
    )
    citations: list[dict[str, Any]] = []
    for n in resolved_ords:
        src_row = index_by_ordinal.get(n)
        if src_row is None:
            # No row behind the ordinal. Unreachable on the slice path (the
            # index is dense over 1..num_subclaims by construction) and
            # reachable on the rollup path only if a carried member's row
            # could not be recovered by id — count it, never resolve it to a
            # neighbour, which is the whole failure this fix exists to end.
            dropped_refs += 1
            continue
        # FRAME-2 — the WINDOW LEDGER takes the same honest shape as the
        # register for the same reason (a synthetic multi-row block with no
        # single drill target), built by its own module so the unit layer
        # and this one cite it identically.
        if src_row.get(CONTINUITY_ROW_KEY) == CONTINUITY_WINDOW_LEDGER:
            citations.append(window_ledger_citation(ledger_entries, n))
            continue
        # CONTINUITY — the open-situation REGISTER is not an
        # ``analyst_outputs`` row and has NO single substrate id, so it gets
        # its own citation shape: ``ref_kind='situation_register'`` with the
        # REAL ``situations`` uuids on ``situation_ids`` and NO ``ref_id``.
        # Minting a ``ref_id`` (say, the top situation's) purely so a drill
        # link resolves would be a fabricated anchor — the one thing the
        # citation contract forbids. ``evidence_text`` carries the rendered
        # register, so the verify pass grades a register-backed clause
        # against exactly what the model was shown, with no verify change.
        if src_row.get(CONTINUITY_ROW_KEY) == CONTINUITY_SITUATIONS:
            citations.append(
                {
                    "marker": f"[[ref:{n}]]",
                    "ordinal": n,
                    "ref_kind": SITUATION_REGISTER_REF_KIND,
                    CONTINUITY_CITATION_KEY: CONTINUITY_SITUATIONS,
                    "title": (
                        f"Open-situation register ({len(register_situations)} "
                        "open frame(s))"
                    ),
                    "situation_ids": [
                        str(s.get("situation_id"))
                        for s in register_situations
                        if s.get("situation_id")
                    ],
                    "evidence_text": "\n".join(
                        render_situation_register_lines(register_situations, n)
                    )[:SITUATION_REGISTER_EVIDENCE_CHARS],
                }
            )
            continue
        citation = _build_composition_citation(n, src_row)
        if citation is None:
            # No drill target on the cited sub-claim → count, never fabricate
            # a ref (mirrors the unit path's malformed-id handling).
            dropped_refs += 1
            continue
        # CONTINUITY — the PRIOR READ is a real finding, so it keeps the
        # ordinary ``ref_id``/``ref_kind='finding'`` shape (its drill target
        # is exactly right: the previous read). What it deliberately does NOT
        # carry is the T7 pair, and the omission is the point: the prior read
        # is MEMORY, not corroboration. Feeding its ``effective_confidence``
        # into the correlation guard would let last cycle's own conclusion
        # raise this cycle's de-duplicated evidence ceiling — a composition
        # bootstrapping its confidence off itself — and feeding its
        # ``derived_from`` would fold it into a shared-lineage component with
        # a current sub-claim, conflating "what we said before" with "what we
        # see now". The shared helper stamps both — and, since D-2b, the
        # ``produced_at`` this block used to re-stamp. Strip the PAIR only.
        if src_row.get(CONTINUITY_ROW_KEY) == CONTINUITY_PRIOR:
            citation.pop("effective_confidence", None)
            citation.pop("derived_from", None)
            citation[CONTINUITY_CITATION_KEY] = CONTINUITY_PRIOR
        citations.append(citation)
    # A2 (verify-path structural fix, 2026-07-31): the model cited via
    # [[ref:N]] ZERO times (or every marker it used fell out of range)
    # despite a REAL basis — ``sliced``/``derived_from`` is non-empty. Never
    # leave ``citations`` empty when the compose rests on real evidence: fall
    # back to citing the BASIS directly (bounded; periphery excluded — a
    # periphery clause needs the model's own hedge, not an auto-attribution),
    # each entry flagged so a reader can tell an unmarked compose from a
    # marker-resolved one. Still never fabricates a clause-to-source mapping
    # — it states plainly which basis rows this compose was built from.
    # No-op (byte-identical) when the model DID cite (the common case).
    citations_fallback = False
    if not citations and sliced:
        fallback_n = min(len(sliced), _FALLBACK_BASIS_CITATIONS_CAP)
        for n in range(1, fallback_n + 1):
            fallback_row = index_by_ordinal.get(n)
            if fallback_row is None:
                continue
            citation = _build_composition_citation(n, fallback_row)
            if citation is None:
                continue
            citation["resolution"] = "fallback_basis"
            citations.append(citation)
        citations_fallback = bool(citations)
    # V3/P2 — the ``[[event:<uuid>]]`` ref kind (spec §2.5), gated on
    # ``LEGBA_EVENT_CITATIONS`` + a wired conn: each marker expands through
    # ``signal_event_links`` into ordinary per-signal entries numbered
    # onto ordinals past the slice's own — the floor's ``[[ref:K]]`` path
    # resolves them unchanged, the event's own summary is never emitted.
    # Flag off ⇒ the tokens are ordinary text, byte-identical to today.
    event_expanded = 0
    event_stats: dict[str, int] = {}
    if conn is not None and event_citations_enabled():
        event_expanded = await _expand_event_markers(
            conn, finding, citations, start_ordinal=num_subclaims + 1,
            stats=event_stats,
        )
    finding.data["citations"] = citations
    steps.append({
        "phase": "cite",
        "kind": "resolve_refs",
        "citations": len(citations),
        "refs_dropped": dropped_refs,
        "citations_fallback": citations_fallback,
        # V3/P2 — event-ref expansions this run (flag off ⇒ 0), and the two
        # degrade counts the 09-23 review asked for before the flag goes on:
        # an expansion that RAISED, and one that resolved to no member.
        "event_citations": event_expanded,
        "event_expand_failed": event_stats.get("event_expand_failed", 0),
        "event_unresolved": event_stats.get("event_unresolved", 0),
        # How many of the resolved citations landed on a CONTINUITY block —
        # i.e. did the model actually USE its memory, or was the block shown
        # and ignored? Separately countable from the refs OFFERED
        # (``continuity_receipts``), because "offered but never cited" is the
        # failure mode a continuity clause is supposed to make impossible.
        "continuity_cited": sum(
            1 for c in citations if c.get(CONTINUITY_CITATION_KEY)
        ),
        # 7d — the INDEPENDENCE receipt. How many of this read's quoted claims
        # rest on one source, and how many had syndicated copies folded out of
        # their count. A checked zero, like every counter beside it.
        "claims_single_source": sum(
            1 for c in citations if c.get("single_source")
        ),
        "claims_wire_folded": sum(1 for c in citations if c.get("wire_folded")),
    })
    return citations, resolved_ords


__all__ = [
    "MAX_EVIDENCE_TEXT_CHARS",
    "_CONTESTED_MARKER_RE",
    "_FALLBACK_BASIS_CITATIONS_CAP",
    "_REF_MARKER_RE",
    "_build_composition_citation",
    "_coerce_uuid",
    "_extract_contested_markers",
    "_extract_ref_markers",
    "resolve_composition_citations",
]
