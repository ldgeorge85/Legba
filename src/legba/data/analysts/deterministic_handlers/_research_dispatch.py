# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-B — THE COVERAGE-FLOOR DISPATCH: an alert becomes a STANDING QUESTION.

``planning/RESEARCH_PROGRAM_SPEC_2026-09-05.md`` §2.2 / §0.9 rules that
dispatch for the outbound-research program is **a ``hypotheses`` row, not a
queue**: ``_coverage_floor_scan``'s alert converts to ``status='open_question'``
with ``harvest_class='coverage_floor'``, and the corpus_researcher's EXISTING
``grounding.sources: [open_questions]`` drain (``runtime.grounding
.resolve_open_questions``) is the dispatch. Zero new tables, zero NATS
subjects, zero trigger classes, zero ``dapr_actors.py`` lines.

WHY A QUESTION AND NOT A QUEUE — the four mechanical reasons, restated so the
next reader does not re-derive them:

  1. **The drain exists and is ranked.** ``resolve_open_questions`` already
     runs one recursive-CTE round trip computing ``live_reach`` fused with
     ``desk_salience``, ranks in pure Python by ``open_question_priority_key``
     and hard-caps at 8. A queue would be a second, worse copy of that.
  2. **The class slot exists.** ``_HARVEST_CLASS_PRIORITY`` was written to be
     extended (an unknown class ranks last rather than crashing).
     ``coverage_floor`` slots in at priority 0 — FIRST, ahead of every
     harvested class (2026-09-06 tune; originally priority 1, immediately
     after ``below_floor`` — moved after a live run showed a month-old
     ``unit_payload`` backlog row outranking a same-week dispatched gap, see
     ``open_question_priority_key``'s AGE DECAY note) — on the ruled grounds
     that a coverage gap is both *provably* unanswerable from our own corpus
     (the only class for which that is true) and DISPATCHED rather than
     merely harvested, so it is the only class that genuinely needs the web
     AND the only one an alert already named as needing an answer now.
  3. **The researcher's own cadence tick IS the requeue.** The backlog is
     re-read and priority-ordered every tick anyway.
  4. **A question is never silently closed.** A dispatched question the web
     cannot answer stays open and re-ranks. That is the honest state, and it
     is why this module never writes anything but an INSERT.

     REFINED 2026-09-07, without weakening it. The 03:37Z live run showed the
     other half of "never closed": a question nothing ever moves is also a
     question the next tick re-assigns forever. So the RESEARCH RUN — not this
     writer — now CLAIMS the question it was assigned (``open_question`` ->
     ``in_progress``, stamped with the run id) and the claim settles to
     ``answered`` only against a durable ``bearing_edges`` pointer, or lapses
     back to ``open_question`` if the run never produced one
     (``runtime.dispatched_question``). This module still only ever INSERTs;
     what changed is that its dedup probe now also treats a CLAIMED row as
     still open (``_EXISTS_SQL``), so a claim can never mint a duplicate.

WHERE THE CONVERSION RUNS, AND WHY IT IS POST-PERSIST
-----------------------------------------------------
Inside ``alert_trigger_scan.handle``'s alert-row write loop, immediately after
``_write_alert_row`` returns a row id — the K-2b precedent
(``open_question_conversion.convert_open_questions``, which likewise converts
AFTER the producing row lands, on the same connection, wrapped in a degrade
guard). Two reasons it cannot run earlier:

  * The dispatched question must carry **the dispatching alert's id**, and that
    id does not exist until the row is written.
  * A candidate that fails to write must NOT dispatch: the alert machinery
    deliberately leaves the watermark un-advanced so the transition retries
    next scan, and a question minted against a transition that never landed
    would be a research assignment for an event the record does not contain.

The scan therefore only BUILDS the dispatch payload (``AlertCandidate
.research_dispatch``); this module WRITES it. The payload rides
``apply_desk_cap``'s rollup merge exactly as ``watermarks`` does, so a
coverage-floor candidate folded into a per-desk rollup still dispatches — with
the rollup row as its dispatching alert. Without that merge, a gap suppressed
by the per-desk cap would advance its watermark (never re-firing) while having
dispatched nothing: a silent, permanent loss.

IDEMPOTENCY — WHAT "RE-DETECTION" ACTUALLY MEANS HERE
------------------------------------------------------
Two mechanisms stack, and each covers a case the other cannot:

  * **The rising edge.** ``_coverage_floor_scan`` pages a breach exactly once:
    a STANDING breach is refreshed silently and never re-enters
    ``new_clusters``. So the common "the gap is still there" case dispatches
    nothing, for free, with no probe at all.
  * **A durable containment probe** on ``diagnostic_evidence``, the same
    ``open_question_origin`` marker + jsonb-containment shape the K-2a harvest
    and the K-2b converter use (the ``hypotheses`` table has no ``data``
    column — ``writes._insert_hypothesis`` drops payload extras — so the marker
    must live in ``diagnostic_evidence``, which persists and is queryable).

The probe's ``source_id`` is ``<target_id>|<entity_fold>|<UTC date of the
rising edge>``. The date component is what makes a **resolved-then-re-breached**
gap mint a genuinely new question (the spec's requirement) while a persisting
breach cannot: a persisting breach has exactly ONE rising edge, so exactly one
key. The deliberate exception is a gap that resolves AND re-breaches inside the
same UTC day — at most four scans apart, given the 6h scan interval — which
collapses to one question. That is the correct reading: a coverage gap that
flickers within 24 hours is noise, not a second research need, and minting a
duplicate assignment for it would put two identical rows into a backlog that
already holds 1,795.

DEGRADE-NOT-BREAK
-----------------
Nothing in here may fail the alert scan. The caller wraps the call, and each
cluster is additionally isolated so one bad cluster never sinks its siblings —
the K-2b contract, verbatim. A failed dispatch is logged and lost; the alert
row (the durable product) is already written and the operator can still see the
gap. The alternative — letting a research side-write break the alert plane —
would trade a detector we rely on for a dispatch we do not yet.

THE FLAG
--------
Gated on ``research_regime.research_evidence_enabled()``
(``LEGBA_RESEARCH_EVIDENCE`` != ``off``). At ``off`` the scan builds no payload,
runs no extra query, and this module is never entered — the detector's output
is byte-identical to its pre-program self, which
``tests/data_pkg/test_coverage_floor_scan.py`` proves by passing UNCHANGED.
Dispatching into a backlog whose researcher has no web leg would be a question
nothing can answer.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from ..research_regime import research_evidence_enabled

logger = logging.getLogger(__name__)

#: The harvest class the COVERAGE-FLOOR dispatch stamps. Ruled priority 0 in
#: ``runtime.grounding._HARVEST_CLASS_PRIORITY`` — ahead of every harvested
#: class — because a coverage gap is provably unanswerable from our own
#: corpus AND was dispatched rather than merely harvested.
HARVEST_CLASS: str = "coverage_floor"

#: The harvest class the A-4 REFERENCE-GAP dispatch stamps
#: (``_reference_gap_dispatch``). Priority 1 — immediately after
#: ``coverage_floor`` and ahead of every harvested class, for the same two
#: reasons and with a thinner outside signal (see that table's own note).
#:
#: It lives HERE, beside :data:`HARVEST_CLASS`, and not in the A-4 module,
#: because the class vocabulary is what ``grounding.harvest_class_of`` reads
#: and what the containment probe keys on: ONE vocabulary with two producers
#: is the whole point of :func:`dispatch_open_questions` taking the class as a
#: parameter rather than A-4 forking a second writer.
REFERENCE_GAP_HARVEST_CLASS: str = "reference_gap"

#: The shared marker key every harvested / converted open-question row stamps
#: (``scripts/harvest_open_questions.MARKER_KEY``,
#: ``open_question_conversion.OPEN_QUESTION_MARKER_KEY``,
#: ``runtime.grounding._OPEN_QUESTION_MARKER_KEY``). One vocabulary, three
#: producers, one containment query.
MARKER_KEY: str = "open_question_origin"

#: ``origin`` must read ``harvest`` — that is the ONLY value for which
#: ``grounding.harvest_class_of`` reads an explicit ``harvest_class`` off the
#: marker. A third origin value would silently label every dispatched question
#: ``unknown`` and rank it LAST, which is the exact opposite of the ruling.
MARKER_ORIGIN: str = "harvest"

#: ``question_source`` — the R-B marker the spec asks for, distinct from
#: ``harvest_class`` because the class is a RANKING key (a vocabulary
#: ``grounding`` owns) while this is a PROVENANCE key naming which producer
#: minted the row. R-D's counters and any replay group on it.
QUESTION_SOURCE: str = "coverage_floor"

#: Provenance identity stamped on every dispatched row (functional + queryable,
#: the ``open_question_harvest`` precedent). NOT a registered analyst: no
#: descriptor, no cadence, no LLM — this is a side-write by the alert scan,
#: and inventing a descriptor for it would claim an organ that does not exist.
DISPATCH_ANALYST_ID: str = "research_dispatch"
DISPATCH_ANALYST_VERSION: str = "1"

#: Cap on ``derived_from`` refs per dispatched row — the cluster's exemplar
#: signals are lineage, not a roster. Mirrors the harvest script's own bound.
_MAX_DERIVED_REFS: int = 16

#: ``hypotheses.thesis`` is ``max_length=4096`` (``HypothesisPayload``); the
#: rendered question is trimmed to fit rather than rejected by pydantic.
THESIS_CHAR_CAP: int = 4096
_THESIS_CHAR_CAP: int = THESIS_CHAR_CAP

#: The four keys :func:`dispatch_marker`'s ``extra`` may never overwrite —
#: the SHARED vocabulary ``grounding.harvest_class_of`` and the containment
#: probe both read.
_RESERVED_MARKER_KEYS: frozenset[str] = frozenset(
    {"marker", "origin", "harvest_class", "source_id"}
)

#: Per-scan cap on dispatched questions. A single scan that somehow found a
#: hundred new gaps must not put a hundred rows into the backlog in one tick;
#: the rest re-detect on the next rising edge they earn.
MAX_DISPATCH_PER_SCAN: int = 8


# ---------------------------------------------------------------------------
# The hottest desk (F-6) — which desk's bounded question the gap carries
# ---------------------------------------------------------------------------
#
# The coverage-floor alert is per TARGET; a bounded question is per DESK. §8
# F-6 rules the question target-scoped and desk-AGNOSTIC ("this target's
# evidence names X and no open frame does"), carrying the HOTTEST desk's
# ``method.bounded_question`` as CONTEXT — chosen deterministically as the desk
# owning the target's most intense open situation.
#
# The desk is read off ``situations.situation_signature``, whose migration-0188
# form is ``sig:<target_id>#dim:<desk_analyst_id>`` — NOT off
# ``situations.analyst_id``, which names the PRODUCER (``situation_clustering``
# on every live row, verified 2026-09-05) and would resolve every target to the
# same non-desk. The distinction is the whole selector; getting it wrong would
# make F-6 pick nothing, silently.
_HOTTEST_DESK_SQL = """
    SELECT target_id,
           split_part(situation_signature, '#dim:', 2) AS desk_id,
           intensity_score
      FROM situations
     WHERE target_id = ANY($1::text[])
       AND status <> 'closed'
       AND (valid_until IS NULL OR valid_until > now())
       AND situation_signature LIKE '%#dim:%'
     ORDER BY target_id, intensity_score DESC, situation_signature
"""

# The desks' bounded questions, from the REGISTRY (``analyst_descriptors``) and
# not from ``descriptors/*.yaml``: the tree is in neither container image and
# is not volume-mounted (``scripts/gen_descriptor_prompt_manifest.py``'s own
# note), so a tree read would work in tests and return nothing in production —
# the worst possible failure shape for a field whose absence is invisible.
_BOUNDED_QUESTIONS_SQL = """
    SELECT descriptor_id, body -> 'method' ->> 'bounded_question' AS q
      FROM analyst_descriptors
     WHERE is_head = TRUE
       AND COALESCE(state, 'active') <> 'retired'
       AND body -> 'method' ->> 'bounded_question' IS NOT NULL
"""

# The dedup probe: a gap already on the backlog is never re-dispatched.
# ``in_progress`` (2026-09-07) is the CLAIM a research run holds while it
# answers a dispatched question (``runtime.dispatched_question.CLAIMED_STATUS``)
# — the row has left ``open_question`` but is emphatically still open, so it
# must keep deduping. Without this clause a claim would look like a closed
# question for the ~12h it stands and the next scan would mint a duplicate of
# the very gap a run is mid-way through answering.
_EXISTS_SQL = (
    "SELECT 1 FROM hypotheses "
    "WHERE status IN ('open_question', 'in_progress') "
    "AND diagnostic_evidence @> $1::jsonb LIMIT 1"
)


async def resolve_dispatch_context(
    conn: Any, target_ids: Sequence[str]
) -> dict[str, dict[str, str]]:
    """``{target_id: {"desk_id": …, "bounded_question": …}}`` for F-6.

    TWO reads, both bounded, both executed ONLY when the program flag is on
    (the caller gates). A target with no open dimension-signed situation, or
    whose hottest desk declares no ``bounded_question`` (the three composition
    tiers deliberately declare none), resolves to an ABSENT entry — and the
    rendered question then carries the gap alone, which §8 F-6 names as the
    defensible cheaper option. Never a fabricated question.
    """
    if not target_ids:
        return {}
    hottest: dict[str, str] = {}
    for row in await conn.fetch(_HOTTEST_DESK_SQL, list(dict.fromkeys(target_ids))):
        tid = str(row["target_id"] or "")
        desk = str(row["desk_id"] or "").strip()
        # ORDER BY target_id, intensity_score DESC — the FIRST row per target
        # is its hottest desk; ties break on the signature for determinism.
        if tid and desk and tid not in hottest:
            hottest[tid] = desk
    if not hottest:
        return {}
    questions: dict[str, str] = {}
    for row in await conn.fetch(_BOUNDED_QUESTIONS_SQL):
        q = str(row["q"] or "").strip()
        if q:
            questions[str(row["descriptor_id"])] = q
    out: dict[str, dict[str, str]] = {}
    for tid, desk in hottest.items():
        q = questions.get(desk)
        if not q:
            continue
        out[tid] = {"desk_id": desk, "bounded_question": q}
    return out


# ---------------------------------------------------------------------------
# Pure builders (no DB — unit-testable directly)
# ---------------------------------------------------------------------------


def dispatch_source_id(target_id: str, entity_fold: str, rising_edge: datetime) -> str:
    """The containment-probe key: ``<target>|<fold>|<UTC date of rising edge>``.

    See the module note on idempotency for why the date is in the key and what
    the same-day collapse deliberately buys.
    """
    ref = rising_edge if rising_edge.tzinfo else rising_edge.replace(tzinfo=timezone.utc)
    day = ref.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return f"{target_id}|{entity_fold}|{day}"


def dispatch_marker(
    *,
    target_id: str,
    geo: Sequence[str],
    entity_fold: str,
    gap_name: str,
    source_id: str,
    alert_output_id: Any,
    desk_id: str | None,
    bounded_question: str | None,
    run_id: Any,
    harvest_class: str = HARVEST_CLASS,
    question_source: str = QUESTION_SOURCE,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ONE durable marker object stored in ``diagnostic_evidence``.

    Carries, per the R-B contract: the target id + its ``geo`` (§1.3 — geo is
    the ONLY reachability key, so a dispatched question that does not carry it
    dispatches research that reaches no desk), the named gap, the desk's
    bounded question per F-6, the dispatching alert id, and the
    ``question_source`` marker.

    ``marker`` / ``origin`` / ``harvest_class`` / ``source_id`` are the four
    keys the SHARED vocabulary owns (``grounding.harvest_class_of`` reads the
    first three; the containment probe reads the first, third and fourth).
    Everything else on this object is R-B's own carry and is read by
    ``grounding.dispatch_scope_of``.

    ``harvest_class`` / ``question_source`` are PARAMETERS, defaulted to the
    coverage-floor values, so A-4's reference-gap dispatch stamps the same
    four-key vocabulary through the same builder instead of minting a second
    marker shape that ``harvest_class_of`` would have to learn twice.
    ``origin`` is deliberately NOT a parameter: ``harvest`` is the only value
    for which ``harvest_class_of`` reads an explicit class off the marker, so
    a producer that could override it could silently rank itself LAST.
    ``extra`` carries a producer's own non-vocabulary keys (A-4's citing
    reference row, its unit, its item) and can never overwrite the four.
    """
    marker: dict[str, Any] = {
        "marker": MARKER_KEY,
        "origin": MARKER_ORIGIN,
        "harvest_class": harvest_class,
        "source_id": source_id,
        "question_source": question_source,
        "target_id": target_id,
        # §1.3 — the reachability key, carried so the researcher's META run can
        # scope the evidence it fetches to the desk that asked (F-8).
        "geo": [str(g) for g in geo if str(g).strip()],
        "entity_fold": entity_fold,
        "gap_name": gap_name,
        "dispatched_by": str(alert_output_id) if alert_output_id else None,
        "run_id": str(run_id) if run_id else None,
    }
    if desk_id:
        marker["desk_id"] = desk_id
    if bounded_question:
        marker["bounded_question"] = bounded_question
    for key, value in dict(extra or {}).items():
        # The four vocabulary keys are the contract, not a suggestion: a
        # producer's own carry may ADD to the marker and may never redefine
        # what the shared reader keys on.
        if key not in _RESERVED_MARKER_KEYS:
            marker[key] = value
    return marker


def build_thesis(
    *,
    target_id: str,
    gap_name: str,
    cluster: Mapping[str, Any],
    open_frame_count: int,
    desk_id: str | None,
    bounded_question: str | None,
) -> str:
    """Render the standing question a coverage-floor breach becomes.

    Target-scoped and desk-AGNOSTIC (F-6): the sentence is about the TARGET's
    evidence and its frame set, because a coverage gap is a COLLECTION gap, not
    a desk's gap. The hottest desk's bounded question rides as explicitly
    labelled CONTEXT — named as such so a reader can never mistake it for a
    claim that the gap belongs to that desk.

    Every number in the rendered text is one the alert already measured. None
    is recomputed here and none is invented: a question that overstates its own
    evidence is a worse assignment than one that states it plainly.
    """
    n_signals = cluster.get("n_signals")
    n_days = cluster.get("n_days")
    share = cluster.get("slice_share")
    bits: list[str] = []
    if isinstance(n_signals, int):
        bits.append(f"{n_signals} signals")
    if isinstance(n_days, int):
        bits.append(f"over {n_days} days")
    if isinstance(share, (int, float)):
        bits.append(f"{float(share) * 100:.1f}% of its entity-bearing slice")
    measured = f" ({', '.join(bits)})" if bits else ""
    frames = (
        f"none of its {open_frame_count} open frames name it"
        if open_frame_count
        else "no open frame names it"
    )
    head = (
        f"COVERAGE GAP — {target_id}'s own evidence keeps naming {gap_name}"
        f"{measured}, and {frames}. Does {gap_name} bear on {target_id}, and if "
        f"so how? Our corpus is the collection that missed it, so answering this "
        f"is expected to require reaching OUTSIDE it."
    )
    if desk_id and bounded_question:
        head += (
            f" Context — the bounded question of this target's hottest open "
            f"desk ({desk_id}): \"{bounded_question}\""
        )
    return head[:_THESIS_CHAR_CAP]


def dispatch_payload_for_cluster(
    *,
    target_id: str,
    geo: Sequence[str],
    cluster: Mapping[str, Any],
    open_frame_count: int,
    rising_edge: datetime,
    context: Mapping[str, str] | None,
) -> dict[str, Any]:
    """One serialisable dispatch descriptor, built by the SCAN and written by
    :func:`dispatch_open_questions` after the alert row lands.

    Deliberately a plain dict of JSON-safe values: it rides on the
    ``AlertCandidate`` through ``apply_desk_cap``'s rollup merge, and a
    candidate's fields must survive being carried by a different candidate.
    """
    fold = str(cluster.get("entity_fold") or "")
    ctx = dict(context or {})
    return {
        "target_id": target_id,
        "geo": [str(g) for g in geo if str(g).strip()],
        "entity_fold": fold,
        "gap_name": str(cluster.get("name") or fold),
        "source_id": dispatch_source_id(target_id, fold, rising_edge),
        "open_frame_count": int(open_frame_count),
        "desk_id": ctx.get("desk_id"),
        "bounded_question": ctx.get("bounded_question"),
        "exemplar_signal_ids": [
            str(s) for s in (cluster.get("exemplar_signal_ids") or [])
        ][:_MAX_DERIVED_REFS],
        "cluster": {
            k: cluster.get(k)
            for k in ("n_signals", "n_days", "mean_magnitude", "slice_share")
        },
    }


# ---------------------------------------------------------------------------
# The write (post-persist, degrade-not-break)
# ---------------------------------------------------------------------------


def _uuid_or_none(value: Any) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


async def dispatch_open_questions(
    conn: Any,
    dispatches: Sequence[Mapping[str, Any]],
    *,
    alert_output_id: Any,
    run_id: Any = None,
    harvest_class: str = HARVEST_CLASS,
    question_source: str = QUESTION_SOURCE,
    analyst_id: str = DISPATCH_ANALYST_ID,
    max_dispatch: int = MAX_DISPATCH_PER_SCAN,
    stats: dict[str, int] | None = None,
) -> int:
    """Write ONE ``open_question`` ``hypotheses`` row per dispatch descriptor.

    Returns how many rows landed (0 when the flag is off, the list is empty,
    every entry already exists, or every entry degraded). NEVER raises — the
    caller's alert row is already durable and a research side-write may not
    cost the operator a detector.

    ``harvest_class`` / ``question_source`` / ``analyst_id`` / ``max_dispatch``
    default to the coverage-floor (R-B) values. A-4's reference-gap dispatch
    passes its own so that BOTH producers share this one write path, this one
    containment probe and this one marker vocabulary — the class is what
    ``grounding.harvest_class_of`` ranks on, and two writers would be two
    chances to drift out of it.

    An entry may carry its OWN rendered ``thesis``; absent one, the
    coverage-floor renderer (:func:`build_thesis`) is used. ``stats``, when
    given, is filled with ``existing`` / ``failed`` counts — the caller's
    receipt needs to tell "already open" apart from "could not write", which a
    single written-count cannot.
    """
    if stats is not None:
        stats.setdefault("existing", 0)
        stats.setdefault("failed", 0)
    if not dispatches or not research_evidence_enabled():
        return 0

    from ...provenance import AnalystContext, HypothesisPayload, write_hypothesis

    ctx = AnalystContext(
        analyst_id=analyst_id,
        analyst_version=DISPATCH_ANALYST_VERSION,
        run_id=_uuid_or_none(run_id) or uuid4(),
    )
    written = 0
    for entry in list(dispatches)[:max_dispatch]:
        try:
            if not isinstance(entry, Mapping):
                continue
            target_id = str(entry.get("target_id") or "").strip()
            source_id = str(entry.get("source_id") or "").strip()
            if not target_id or not source_id:
                continue
            probe = json.dumps([{
                "marker": MARKER_KEY,
                "harvest_class": harvest_class,
                "source_id": source_id,
            }])
            if await conn.fetchval(_EXISTS_SQL, probe) is not None:
                if stats is not None:
                    stats["existing"] += 1
                continue
            marker = dispatch_marker(
                target_id=target_id,
                geo=entry.get("geo") or (),
                entity_fold=str(entry.get("entity_fold") or ""),
                gap_name=str(entry.get("gap_name") or ""),
                source_id=source_id,
                alert_output_id=alert_output_id,
                desk_id=entry.get("desk_id"),
                bounded_question=entry.get("bounded_question"),
                run_id=run_id,
                harvest_class=harvest_class,
                question_source=question_source,
                extra=entry.get("marker_extra"),
            )
            thesis = str(entry.get("thesis") or "").strip()[
                :_THESIS_CHAR_CAP
            ] or build_thesis(
                target_id=target_id,
                gap_name=str(entry.get("gap_name") or ""),
                cluster=entry.get("cluster") or {},
                open_frame_count=int(entry.get("open_frame_count") or 0),
                desk_id=entry.get("desk_id"),
                bounded_question=entry.get("bounded_question"),
            )
            # Lineage: the cluster's exemplar signals — the very rows whose
            # recurrence the frame set failed to name. The dispatching ALERT is
            # named on the marker (``dispatched_by``) rather than here: it is an
            # ``analyst_outputs`` row, and mixing product ids into a question's
            # signal lineage would make the forward walk read a detector's alert
            # as evidence for the question.
            derived: list[UUID] = []
            for raw in entry.get("exemplar_signal_ids") or ():
                sid = _uuid_or_none(raw)
                if sid is not None and sid not in derived:
                    derived.append(sid)
            # target_id is stamped on the ROW (a hypotheses column the backlog
            # SQL joins ``desk_salience`` on) as well as on the marker — the
            # column is what ranks the question; the marker is what carries the
            # geo the column cannot.
            row, _dlq = await write_hypothesis(
                conn,
                analyst_ctx=AnalystContext(
                    analyst_id=ctx.analyst_id,
                    analyst_version=ctx.analyst_version,
                    run_id=ctx.run_id,
                    target_id=target_id,
                ),
                payload=HypothesisPayload(
                    thesis=thesis,
                    status="open_question",
                    diagnostic_evidence=[marker],
                ),
                derived_from=derived[:_MAX_DERIVED_REFS],
            )
            if row is not None:
                written += 1
            elif stats is not None:
                stats["failed"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad entry never sinks siblings
            if stats is not None:
                stats["failed"] += 1
            logger.warning(
                "research_dispatch.entry_failed alert=%s target=%s err=%s",
                alert_output_id, (entry or {}).get("target_id"), exc,
            )
            continue
    if written:
        logger.info(
            "research_dispatch.written n=%d alert=%s", written, alert_output_id
        )
    return written


__all__ = [
    "DISPATCH_ANALYST_ID",
    "DISPATCH_ANALYST_VERSION",
    "HARVEST_CLASS",
    "MARKER_KEY",
    "MARKER_ORIGIN",
    "MAX_DISPATCH_PER_SCAN",
    "QUESTION_SOURCE",
    "REFERENCE_GAP_HARVEST_CLASS",
    "THESIS_CHAR_CAP",
    "build_thesis",
    "dispatch_marker",
    "dispatch_open_questions",
    "dispatch_payload_for_cluster",
    "dispatch_source_id",
    "resolve_dispatch_context",
]
