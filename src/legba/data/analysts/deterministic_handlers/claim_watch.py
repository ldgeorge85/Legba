# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``claim_watch`` sub-handler — flag-only new-evidence-vs-open-question matcher.

A deterministic META analyst on a ~30-minute cadence that matches NEW signals
(since a durable cursor watermark) against the standing OPEN-QUESTION set
(``hypotheses`` rows with ``status='open_question'``, optionally ``active``)
and side-writes append-only markers — NEVER content:

  * ``bearing_edges`` rows (migration 0107): "new evidence bears on old
    question", one per (signal, question) match above the fused threshold,
    with the contributing planes, the fused weight, ``provenance_class='live'``
    and this matcher's version — plus, when the bearing pipeline below is on,
    the semantic-judgment stamp in ``data`` (migration 0116). The UNIQUE
    (src_id, dst_id, edge_kind) constraint + ON CONFLICT DO NOTHING make
    re-runs idempotent.
  * ``review_flags`` rows (migration 0107) ONLY for matched questions that
    trace to LIVE products via a FORWARD walk over ``output_consumption``
    (migration 0106): each non-superseded consumer output reached gets one
    open flag per (consumer, question) pair (the partial unique index is the
    writer's idempotency). A consumer id with no ``analyst_outputs`` row
    (e.g. a journal entry) counts as live — supersession is only observable
    for ``analyst_outputs`` rows, and journal entries are never superseded.
  * ONE summary receipt per run (the ``alert_trigger_scan`` pattern):
    TRACE_ONLY — fully audited in ``analyst_traces``, no ``analyst_outputs``
    row, which also keeps this handler OUT of the FINDING-emitters set the
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS drift guard asserts equality against.
    The receipt carries the per-run match counts AND the ``staleness_debt``
    gauge: open review_flags whose flagged consumer is still a live
    (non-superseded) head.

No alerts, no writes to analysis outputs, no recomposition, no correction
content — detect-and-mark only (the standing arbiter discipline; the 0107
forbid-delete trigger enforces the flag side).

The BEARING PIPELINE (W-B1/W-B2) — the one LLM seam
---------------------------------------------------
The MATCHING above is and stays fully deterministic. What 3.3.0 adds is a
POST-MATCH filter: once the matcher has decided which edges it would write,
each candidate is put to a small self-hosted model as a plain question —
"does this signal bear on this thesis?" — and a NO means the edge is not
written. See :mod:`.bearing_gate` for the whole leg; the seam here is one
call between the matching pass and the write block, and every knob it reads
is read in :func:`handle` so the X-1 catalog stays the honest contract.

Why a model at all, in a handler whose whole point was determinism: two
rounds of deterministic levers (3.1 the measured vector floor; 3.2 the meta
exclusion, the global hub discount, the omnibus/duplicate dampers) hit their
ceiling at ~0.21 write precision on the K-4 gold population, and the residual
failures are pairs where every plane is honestly positive and the signal
still does not bear on the thesis. That distinction lives in neither the
entity set nor the embedding neighbourhood, so no re-weighting of those
planes can make it. The idle 8B answers it at specificity 0.900 on 242 gold
pairs with a naive prompt — i.e. it is measured to be good at exactly the
operation it is used for, REFUSING.

Three properties that keep this from being a new failure mode:

  * **Default OFF.** ``bearing_gate`` defaults to ``'off'`` in code, so a
    descriptor with no ``method.options`` block behaves byte-for-byte like
    3.2.0 and constructs no client at all. It is turned on with a descriptor
    PUT at deploy.
  * **The outage never silences the matcher.** An unreachable / timed-out /
    unparseable gate STAMPS AND WRITES (``data.bearing_gate='unavailable'``),
    as does an over-budget candidate (``'deferred'``). A gate that failed
    closed would convert one 8B outage into a silent hole in the bearing
    plane; consumers filter on the stamp instead.
  * **Everything is counted.** ``bearing_gated_out`` (the refusals),
    ``bearing_gate_errors``, ``bearing_gate_deferred`` and the confirm leg's
    four counters ride every receipt, and the gate's tallies ride the receipt
    TITLE whenever it is on.

A second, BATCHED judgment over the gate-YES edges only — the $0 core plane,
echo-bound by pair id — records ``data.bearing_confirm`` +
``bearing_confirm_reason`` on the edge. It never blocks an edge (by then the
edge is already written); it is a richer second reading for the consumers and
for the measurement loop.

Matching planes (fused into one weight)
---------------------------------------
1. **vector** — the signal's ALREADY-STORED embedding (signal_embedder →
   Qdrant ``legba_signals``; point id = signal id) against the question
   thesis embedding, cosine. Question texts are embedded ON FIRST SIGHT via
   the hosted embed client and cached in-process (the open set is bounded —
   hundreds — so steady-state embed traffic is near zero). PLANE REUSE, not
   an 18th bespoke stack: the store + embedder arrive on ``deps.extras``
   under the signal_embedder keys, wired by the SAME
   ``_wire_signal_embedder`` deps-builder hook (extended to this
   sub-handler); signal vectors are read back by id IN BOUNDED CHUNKS (one
   oversized retrieve must never take the whole plane down), never
   re-embedded.
   Degrade-not-break, but NEVER SILENTLY: an unwired plane, a retrieve
   failure, or a batch of signals the embedder sweep has not reached yet
   makes the vector plane contribute 0 for the affected pairs. The run
   still completes on the remaining planes — but it says so LOUDLY.
   ``signal_vectors_found`` / ``signal_vectors_missing`` /
   ``vector_plane_errors`` are counted separately, and
   ``vector_plane_starved`` goes true (with a WARNING) whenever the plane
   is wired yet covers NOTHING in a non-empty batch. The KW-3 first-live
   incident was exactly this shape: the plane read "wired" in the boot log
   while contributing to 0 of 591 edges, because the cursor had fallen back
   into the band of signals ``signal_embedder`` (newest-first) had not
   reached yet — the structural cause, now fixed under Cursor policy.
   Coverage is not left to luck either: the batch's newest rows, which the
   15-minute embedder sweep has typically not reached, are HELD one tick
   rather than matched blind (``held_for_embedding``, see the tail-hold
   under Cursor policy).
2. **entity** — canonical entity overlap. The signal side: its
   ``signal_entity_links`` entity ids folded through
   ``entity_profiles.merged_into`` to the elected canonical row (one-level
   fold — the same convergence the watchlist matcher applies to merge
   losers); for a NEW signal the resolution sweep has not linked yet, its
   NER surface names (``payload.entities``) are resolved through the
   existing election machinery (:func:`legba.data._entity_resolve.resolve_keeper`)
   and compared by canonical name. The question side: entity ids linked to
   its ``derived_from`` lineage signals (direct, through a fact's
   ``derived_from``, or through a finding's — bounded two-hop expansion,
   the watchlist lineage-walk shape), same canonical fold.
3. **geo** — the signal's ISO2 ``geo`` tags intersect the question's desk
   scope (``hypotheses.target_id`` → the head ``target_descriptors`` row's
   ``body.scope.geo``, the alert_trigger_scan desk-geo read).

Fusion::

    weight = (W_VECTOR*vector + entity_component(n) + W_GEO*geo) * age_factor

where ``n`` is the number of DISTINCT shared canonical entities, vector
counts only at/above :data:`VECTOR_SIM_FLOOR`, and the age factor dampens
OLDER questions (the fact-decay module's MISP retention curve,
:func:`legba.data.facts.decay.retention_factor`, floored at
:data:`AGE_FACTOR_FLOOR`).

The entity plane is GRADED, not boolean — that is the whole conservatism
of the model. ONE shared entity is desk co-membership, not evidence: on a
country desk "this signal mentions Iran and is geo-Iran" against "an open
question about Iran" is close to trivially true, and the KW-3 first-live
run proved it (a 0.45 threshold against a boolean 0.35*entity + 0.15*geo
= 0.50 made every desk co-member a match and saturated the per-run edge
cap). So the first shared entity buys :data:`W_ENTITY_FIRST`, each further
distinct one buys :data:`W_ENTITY_ADDITIONAL` up to
:data:`MAX_SHARED_ENTITIES_COUNTED`, and geo is a tie-breaker at
:data:`W_GEO` rather than a third of the budget.

...and each shared entity is weighted by its desk-relative SPECIFICITY,
because grading the COUNT was not enough. The first live 2.0.0 run wrote
185 edges of which 150 sat at exactly 0.560 — the 3-entity cap, entity
plane alone. That is the same desk co-membership the grading was meant to
exclude, wearing three names: a signal mentioning Iran, Khamenei and the
IRGC shares three entities with nearly every question on the Iran desk,
whose lineage entities are dominated by the desk's own headline names. So
each shared entity contributes :func:`entity_specificity` of its document
frequency across THAT DESK's questions in the scanned set — 1.0 up to
:data:`DF_UBIQUITY_KNEE`, ramping to :data:`ENTITY_SPECIFICITY_FLOOR` at
df = 1.0 — instead of a flat 1. Three entities each carried by ~90% of the
desk's questions then fuse to ≈0.32 with geo (no match) while two rare
co-mentions still fuse to 0.48 (match).

Deliberate properties of that rule:

  * It is computed from data ALREADY IN HAND — the question set this run
    loaded — so it costs no table, no query and no sweep.
  * It only ever LOWERS a weight (:func:`entity_component` is monotone),
    so every conservatism property above holds unchanged: nothing that did
    not match before can start matching because of specificity.
  * It is INERT where it cannot be estimated: a desk with fewer than
    :data:`MIN_DESK_QUESTIONS_FOR_SPECIFICITY` questions in the scanned set
    scores on raw counts, because df computed from one document says
    "everything is ubiquitous" and would silently mute a whole desk.
  * The floor is a floor, not a zero: the desk's headline name is weak
    evidence, not anti-evidence.

...and that desk-relative rule is only HALF the ubiquity problem, which the
K-4 gold set measured exactly (123 labeled edges): entity-only pairs scored
**0/54**. The df above is QUESTION-SIDE and DESK-LOCAL, so an entity that is
ubiquitous across the whole STREAM but carried by only a few of any one
desk's questions rides straight through it — "Trump", "United States",
"Iran", "Russia", bare demonyms and NER datelines bridged desks wholesale
(a US-strikes-Iran wire story matched a Canada-trade thesis on
``Donald Trump, Trump, United States`` alone).

So each entity carries a SECOND, SIGNAL-SIDE discount:
:func:`global_entity_specificity` of its document frequency across a recent
window of the signal stream (:data:`GLOBAL_DF_WINDOW_SIGNALS` newest signals,
denominator = the ATTRIBUTED signals in that window, i.e. those carrying any
entity at all — an unlinked signal is a missing observation, not evidence
that a name is rare). The two discounts COMPOSE multiplicatively and share
one floor (:func:`combined_specificity`), so a name can be discounted by its
desk, by the stream, or by both, and still never falls below
:data:`ENTITY_SPECIFICITY_FLOOR`.

The knee and saturation are SET FROM MEASUREMENT, not intuition (live DB,
10,000 newest signals ⇒ 5,576 attributed): United States 0.146, Russia
0.095, Iran 0.093, Ukraine 0.065, Trump 0.054 — against France 0.040,
China 0.029, Japan 0.026, and the overwhelming mass of entities below 0.01
(Mali 0.0025, Ebola 0.0011). :data:`GLOBAL_DF_UBIQUITY_KNEE` = 0.02 leaves
that mass untouched; :data:`GLOBAL_DF_SATURATION` = 0.10 puts the top of the
hub band at the floor and the rest of it within ~0.05 of the floor. Three
names all in that band no longer add up to a match even with desk geo on
top, which is precisely the 0/54 class. Why a QUERIED window rather than the
run's own signal batch: the
batch is too small to estimate this. At the measured ingest a tick's batch is
~70 signals of which ~45% are attributed, i.e. ~30 documents — a resolution
of 1/30 = 0.033, coarser than the entire 0.015→0.144 range the discount has
to discriminate, and one burst story would rank its subject above the United
States. Measured directly: over the 500 newest signals China read df 0.138
(≈ the US) purely because it was a China news day, while over 10,000 it read
0.030. The window query is one bounded aggregate (~200 ms, measured) per
30-minute tick.

Same deliberate properties as the desk-relative rule: computed not curated
(there is NO name stop-list — a name earns its discount from the stream),
monotone (it can only ever LOWER a weight), floored not zeroed, and INERT
below :data:`MIN_SIGNALS_FOR_GLOBAL_SPECIFICITY` attributed signals in the
window — the same honesty as the desk floor, for the same reason.

The resulting arithmetic (fresh question, age_factor 1.0, entities of full
specificity on BOTH surfaces; threshold :data:`DEFAULT_MATCH_THRESHOLD` =
0.45):

  ===================================== ====== =======
  evidence                              weight match?
  ===================================== ====== =======
  geo alone                              0.10  no
  1 shared entity alone                  0.20  no
  **1 shared entity + geo**              0.30  **no**
  2 shared entities                      0.38  no
  2 shared entities + geo                0.48  yes
  3 shared entities                      0.56  yes
  vector at the 0.45 floor + geo         0.325 no
  vector at the 0.45 floor + 1 entity    0.425 no
  vector at the 0.45 floor + 1 ent + geo 0.525 yes
  vector alone                          ≥0.45  only at cosine ≥0.90
  ===================================== ====== =======

so a match ALWAYS requires either semantic (vector) support or genuinely
strong entity evidence — never mere desk co-membership. A pair below
threshold writes NOTHING.

Age dampening is a real but SLOW brake, and the docstring used to overstate
it: with a 365-day lifetime and decay_speed 0.40 the curve is
``1 - (t/365)**2.5``, which still reads ≈1.0 for the first few months
(it takes ~145 days to fall to 0.9). It is therefore NOT what keeps weak
matches out — the graded entity plane is. What the curve does buy is that
at the :data:`AGE_FACTOR_FLOOR` even 3 shared entities + geo (0.66 raw →
0.396) no longer lands, so a long-standing question needs vector support
to be re-flagged.

Guards
------
* **Circularity** — a signal already in the question's own evidence (its
  ``derived_from`` lineage signals, ``supporting_signals`` or
  ``refuting_signals``) never matches: new-evidence edges must point
  OUTSIDE the question's own foundations.
* **Meta questions** — a question about the SYSTEM'S OWN analysis products
  is not answerable by a news signal, and the K-4 gold set measured the
  consequence: the harvested meta classes scored **2/58 = 0.034** while
  substantive world-state theses scored 32/65 = 0.492. So questions whose
  harvest class is in :data:`META_QUESTION_CLASSES` are SKIPPED AT MATCH
  TIME — counted as ``skipped_meta_questions``, never silently. They stay
  open questions, keep their lineage and stay visible to every read route;
  only THIS matcher ignores them, because a wire story cannot bear on
  "what sources would close this collection gap", "this finding failed the
  faithfulness floor", or "does this composition still hold given a
  superseded input". ``fact_contention`` questions ("which value of
  'border with' for 'madrid' is correct?") are deliberately NOT excluded —
  they are questions about the WORLD, which a news signal genuinely can
  bear on. The class is read from the durable ``diagnostic_evidence``
  marker ``scripts/harvest_open_questions.py`` stamps (see
  :func:`harvest_class`); unmarked questions (the agency faucet, unit
  payloads) are never meta.
* **Omnibus signals** — a live blog, day digest or press review carries a
  huge entity set and sprays it across the whole open-question set. Measured
  over the 11,195 live 3.x edges: 943 source signals, a MEDIAN of 15
  distinct questions per signal, p90 = p99 = max = 20 — i.e. the old
  20-edge per-signal cap only ever engaged in the top decile, and half of
  every run's signals were already spraying 15+. The cap is therefore
  :data:`MAX_QUESTIONS_PER_SIGNAL` distinct questions per signal per run
  (strongest kept, overflow counted as ``edges_dropped_per_signal_cap``,
  and the number of SIGNALS the cap engaged on counted separately as
  ``omnibus_capped`` — the omnibus population size is not derivable from
  the edge count). It is a BACKSTOP, not the primary lever: the meta
  exclusion and the global-ubiquity discount are what should collapse the
  fan-out; this bounds what they miss.
* **Same-URL duplicates** — one article ingested twice (two ``signals``
  rows, one ``canonical_url``) is double-sampled into double the edges. A
  batch keeps the NEWEST row per canonical url and counts the drops as
  ``signals_url_deduped`` (measured: 88 droppable rows in a 500-signal
  batch, worst url 40×). Because rows arrive oldest-first the keeper is
  always the LAST occurrence, so the batch's final row is never dropped and
  the cursor advance is untouched.
* **Caps** — bounded work per run: signal batch, question set, per-signal
  and per-run edge budgets, per-run flag budget, per-run embed budget.
  When the edge budget would be exceeded the remaining SIGNALS ARE
  DEFERRED (the cursor only advances past fully-processed signals, so
  nothing is lost WITHIN the freshness horizon below) and the receipt
  reports the deferral. ``cursor_falling_behind`` still goes true (with a
  WARNING) when a run both truncates its signal batch and defers work —
  the horizon bounds that condition's cost, it does not make it healthy.
  Raising ``edge_cap`` is not a fix for it; it buys one tick and hides the
  lag. The real containment is the fusion model above: a match rate low
  enough that the SIGNAL cap, which advances the cursor, binds rather than
  the EDGE cap, which strands it.
* **Idempotency** — the 0107 unique constraints do the dedup
  (ON CONFLICT DO NOTHING both tables); a re-run over the same window
  inserts zero new rows.

Statefulness — the cursor watermark
-----------------------------------
Rides the EXISTING ``alert_trigger_watermarks`` table (migration 0091) as a
new consumer class ``trigger_class='claim_watch'`` — the table's
(trigger_class, watermark_key, state jsonb) shape accommodates a cursor row
directly, so no new watermark table is minted (the anti-sprawl constraint).
One ``_cursor`` row fingerprints the last fully-processed signal
(fetched_at + id); the 0091 contract is mirrored exactly: the FIRST-EVER
run seeds the cursor at the current newest signal and writes NOTHING (a
bring-up never floods the backlog), and the cursor advances ONLY after the
run's edge/flag writes landed. Late-arriving signals stamped BEFORE the
cursor are not re-scanned (the standard watermark-plane limitation).

Cursor policy — keeping pace, and paying for it out loud
--------------------------------------------------------
A watermark that advances only over fully-processed signals CANNOT keep
pace with a stream faster than one run's throughput, and the shortfall
compounds. Measured on the first live 2.0.0 run: 39 signals processed, 461
deferred, ~70 ingested per 30-minute tick — a permanent loss of ground.
Because ``signal_embedder`` drains NEWEST-FIRST, a permanently lagging
cursor also sits permanently in the band the embedder has not reached, so
"the cursor cannot catch up" and "the vector plane contributes nothing"
are one defect, not two.

Two mechanisms, both counted:

1. **Freshness horizon (skip-ahead).** The cursor may never sit further
   behind the STREAM HEAD (the newest signal — not the wall clock, so an
   ingest outage never reads as lag and clock skew never manufactures one)
   than ``max_lag_seconds``
   (:data:`DEFAULT_MAX_CURSOR_LAG_SECONDS`, 6h). When it does, the run
   COUNTS the signals between the cursor and the horizon exactly, WARNs,
   persists the new cursor at the horizon, and abandons them. That is a
   real loss and it is reported as one — ``signals_skipped_ahead`` in the
   receipt counters AND in the receipt TITLE, ``cursor_skipped_ahead``
   true, plus ``skip_count_clipped`` if the (bounded) count probe
   saturated, so an abandonment can never read as "covered everything".
   The trade is deliberate and matches what this analyst is FOR: matching
   NEW evidence against standing questions. A day-old signal re-examined
   next week is worth far less than staying current, and the alternative
   on offer is not "process everything" — it is "process an ever-older
   sliver of everything, with no vector plane, forever".
   The horizon is sized so ordinary catch-up never trips it: at the
   measured ingest a 6h backlog is ~840 signals, and a healthy run drains
   up to ``signal_cap`` (500) against ~70 arriving, i.e. ~430/tick of net
   catch-up — under two ticks. ``max_lag_seconds <= 0`` disables skipping
   (grind the whole backlog, forfeiting the pace guarantee).
2. **Tail-hold.** In healthy steady state the matcher sits AT the head of
   the stream, where a large share of each batch is younger than the
   embedder's last 15-minute sweep. Matching those blind is the OTHER way
   the vector plane contributes nothing. So the batch's trailing
   not-yet-covered rows are HELD (the cursor stops short of them) and
   scored next tick WITH their vectors — held, never dropped, counted as
   ``held_for_embedding``. The hold is disarmed wherever it could wedge:
   only when the batch reached the head (an un-truncated batch), only
   while the plane covers something (otherwise the run is starved, which
   is a different and already-loud condition), only for rows younger than
   a grace age, and never for the whole batch.

Steady state under the two: the cursor tracks the head roughly one
embedder tick behind, every processed signal is one the embedder has
already covered, and the throughput the matcher must sustain is the ingest
rate — not the ingest rate plus an unpayable debt.

Registered via ``scripts/bringup_register_claim_watch.py`` — NOT inline
through a test fixture.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from legba.data._entity_resolve import resolve_keeper

from ...facts.decay import retention_factor
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from .bearing_gate import (
    DEFAULT_BEARING_CONFIRM_CAP,
    DEFAULT_BEARING_GATE,
    DEFAULT_BEARING_GATE_CAP,
    DEFAULT_BEARING_GATE_REF,
    EdgeCandidate,
    bearing_counter_defaults,
    gate_enabled,
    run_bearing_pipeline,
    signal_digest,
)
from .alert_trigger_scan import (
    _load_class_watermarks,
    _mark_seeded,
    _upsert_watermark,
)
from .signal_embedder import (
    EMBEDDER_DEPS_EXTRA_KEY,
    QDRANT_DEPS_EXTRA_KEY,
)

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "claim_watch"

#: The 0091 consumer class this handler owns (rides alert_trigger_watermarks).
TRIGGER_CLASS = "claim_watch"

#: The single cursor row's watermark_key within the class.
CURSOR_KEY = "_cursor"

#: Stamped into every bearing_edges row (reproducibility). MAJOR-BUMPED with
#: any change to the fusion model, because the stamp is the only thing that
#: tells an edge written under one weighting apart from an edge written under
#: another. 1.0.0 = the boolean entity plane (every row entity+geo at weight
#: 0.500 — desk co-membership, not evidence); 2.0.0 = the graded entity plane
#: (which measured 150 of 185 rows at exactly the 3-entity cap 0.560, i.e.
#: still co-membership, just three names of it); 3.0.0 = the same grading
#: weighted by desk-relative entity SPECIFICITY; 3.1.0 = the measured vector
#: floor (0.60 → 0.45); 3.2.0 = the three K-4 levers — meta-question
#: exclusion, the GLOBAL (signal-side) ubiquity discount composed onto the
#: desk-relative one, and the omnibus/duplicate signal dampers; 3.3.0 = the
#: BEARING PIPELINE seam — the fusion model is byte-for-byte 3.2.0, but a
#: post-match semantic gate may now REFUSE an edge the matcher would have
#: written (see :mod:`.bearing_gate`), so a 3.3.0 row is a row that survived a
#: filter a 3.2.0 row never faced. That is a difference in what the population
#: MEANS, which is exactly what this stamp exists to record — even though the
#: gate ships OFF and an off run writes the same rows 3.2.0 wrote.
#: Rows written under each model stay readable and stay attributable to the
#: model that made them.
MATCHER_VERSION = "claim_watch/3.3.0"

#: review_flags.reason for flags this matcher writes.
FLAG_REASON = "new_evidence_bears_on_open_question"

# ---------------------------------------------------------------------------
# Fusion model — weights, floors, thresholds (conservative by construction)
# ---------------------------------------------------------------------------

W_VECTOR = 0.50

#: The GRADED entity plane. The first shared canonical entity is desk
#: co-membership and deliberately CANNOT reach the threshold with geo
#: (0.20 + 0.10 = 0.30 < 0.45); the second and third each add real
#: discriminating power. Counting is capped so one pathologically
#: entity-dense signal cannot buy an unbounded weight.
W_ENTITY_FIRST = 0.20
W_ENTITY_ADDITIONAL = 0.18
MAX_SHARED_ENTITIES_COUNTED = 3

#: Geo is a TIE-BREAKER, not evidence. Desk scope is shared by every
#: question on the desk, so it may nudge an already-supported pair over the
#: line — it may never be a third of the fused budget.
W_GEO = 0.10

# -- entity SPECIFICITY (desk-relative IDF over the scanned question set) ----
#
# Counting shared entities treats every name as equally informative, and on a
# country desk that is false: the desk's own country and leader appear in the
# lineage of nearly every question on it, so "shares 3 entities" can still be
# pure co-membership. The first live 2.0.0 run measured exactly that — 150 of
# 185 edges at weight 0.560, the 3-entity cap, entity plane alone.
#
# The correction is an IDF-shaped weight computed from data ALREADY LOADED:
# the document frequency of an entity across the questions of its own desk in
# THIS run's scanned set. No new table, no new query, no new sweep.

#: Document frequency at/below which an entity counts as fully specific.
#: Half the desk's questions is already generous for "distinctive".
DF_UBIQUITY_KNEE = 0.50

#: What a desk-UBIQUITOUS entity (df = 1.0) is worth relative to a specific
#: one. FLOORED rather than zeroed: the desk's headline name is weak evidence,
#: not anti-evidence — it simply may not be three-quarters of a match.
ENTITY_SPECIFICITY_FLOOR = 0.25

#: Below this many questions on a desk the document frequency is NOT
#: ESTIMABLE (with one question every entity it carries reads df = 1.0), so
#: the rule stays INERT and that desk scores on raw distinct counts. A
#: statistic invented from one observation is worse than no statistic.
MIN_DESK_QUESTIONS_FOR_SPECIFICITY = 5

# -- GLOBAL entity ubiquity (signal-side df over a recent stream window) -----
#
# The desk-relative df above is QUESTION-SIDE: it asks "do this desk's own
# questions all carry this name". It cannot see the other half of the ubiquity
# problem, which the K-4 gold set measured at 0/54 on entity-only pairs — a
# name that is everywhere IN THE STREAM bridges unrelated desks even when only
# a couple of questions on any one desk carry it. So each entity carries a
# second, signal-side discount computed from its document frequency over the
# newest GLOBAL_DF_WINDOW_SIGNALS signals. Computed, never curated: there is
# no stop-list of names anywhere in this module.

#: Signals in the recent-stream df window. A COUNT window, not a time window,
#: so the work is bounded regardless of ingest rate. Sized from measurement:
#: over 10,000 newest signals the hub ranking is stable and event-independent,
#: while over 500 a single busy news day put China (0.138) level with the
#: United States (0.144). One bounded aggregate, ~200 ms measured, per tick.
GLOBAL_DF_WINDOW_SIGNALS = 10_000

#: Attributed signals (rows carrying ANY entity link) required in the window
#: before the global discount applies at all. Mirrors
#: :data:`MIN_DESK_QUESTIONS_FOR_SPECIFICITY`'s rationale: below this the df
#: is not an estimate. 200 documents give a finest resolution of 0.005, a
#: quarter of the knee — under that, one document is a sizeable fraction of
#: the knee and a single burst story manufactures a hub. INERT, not guessed.
MIN_SIGNALS_FOR_GLOBAL_SPECIFICITY = 200

#: Stream document frequency at/below which an entity is fully specific.
#: MEASURED (live DB, 10,000 newest signals ⇒ 5,673 attributed): the mass of
#: entities sits below 0.01 and ordinary subjects — France 0.040, China 0.030,
#: Japan 0.026 — sit an order of magnitude under the hubs. 0.02 leaves that
#: mass untouched.
GLOBAL_DF_UBIQUITY_KNEE = 0.02

#: Stream df at/above which an entity is worth only
#: :data:`ENTITY_SPECIFICITY_FLOOR`. MEASURED: the structural hubs the K-4
#: failure classes name cluster at the top of the distribution — United
#: States 0.146 sits PAST 0.10; Russia 0.095 and Iran 0.093 sit just under it
#: and land within 0.05 of the floor; Ukraine 0.065 and Trump 0.054 ramp
#: toward it — while ordinary desk subjects (China 0.029, Japan 0.026) keep
#: >0.85 of their worth and the long tail keeps all of it. Linear between
#: knee and saturation: continuous, monotone, and floored — a hub is weak
#: evidence, not anti-evidence.
#:
#: NOT tuned to make any individual measured row fall. The one K-4 hub row
#: that still clears — United States + Trump + Donald Trump — clears because
#: ``Trump`` and ``Donald Trump`` are two UNMERGED ``entity_profiles`` rows,
#: so one person is counted twice at two different document frequencies. That
#: is an entity-resolution defect and is left to the merge machinery; bending
#: this curve around it would mis-price every genuine entity to compensate.
GLOBAL_DF_SATURATION = 0.10

# -- meta questions (L1): unmatchable BY CONSTRUCTION ------------------------
#
# The harvest (scripts/harvest_open_questions.py) seeds open questions from
# five classes, and three of them are questions about the system's OWN
# analysis products rather than about the world. The K-4 gold set measured
# them at 2/58 = 0.034 pairwise precision against 32/65 = 0.492 for
# substantive theses. They are skipped AT MATCH TIME and stay open questions.

#: The marker key ``harvest_open_questions.py`` stamps into the durable
#: ``hypotheses.diagnostic_evidence`` array. Mirrored (not imported — scripts/
#: is not an importable package from the runtime) and drift-guarded by test.
HARVEST_MARKER_KEY = "open_question_origin"

#: Harvest classes this matcher will not score. ``collection_gap`` ("what
#: sources would close this gap"), ``below_floor`` ("this finding failed the
#: faithfulness floor"), and the two composition-validity shapes —
#: ``freshness_advisory`` ("does this composition still hold given a
#: superseded input") and ``scorecard_disagreement`` ("is this assessment
#: adequately evidenced given the scorecard excluded it"). Measured per class
#: on the K-4 gold set: collection_gap 1/40, below_floor 0/10,
#: freshness_advisory 0/5, scorecard_disagreement 1/3.
#:
#: ``fact_contention`` is deliberately ABSENT: "which value of 'border with'
#: for 'madrid' is correct?" is a question about the WORLD and new reporting
#: genuinely can bear on it.
META_QUESTION_CLASSES: frozenset[str] = frozenset(
    {
        "collection_gap",
        "below_floor",
        "freshness_advisory",
        "scorecard_disagreement",
    }
)

#: Cosine below this contributes NOTHING (the vector plane is not recorded).
#: 0.45 is set from measurement, not intuition (scripts/
#: measure_claim_watch_cosines.py, 14,280 live pairs / 4 desks): genuinely
#: related thesis-vs-body pairs cluster p50 0.36 / p90 0.43 / p99 0.50 while
#: random pairs sit p90 0.385 / p99 ~0.444 — the old 0.60 floor admitted only
#: ~0.2% of RELATED pairs (the plane was inert). 0.45 ≈ p90(random)+0.05
#: admits the top ~decile of related pairs as CORROBORATION; a lone weak echo
#: still cannot edge (0.5×0.45 = 0.225 < threshold), so the ~1% random tail
#: above 0.45 needs real entity/geo evidence before anything is written.
VECTOR_SIM_FLOOR = 0.45

#: Fused weight at/above this writes an edge; below writes NOTHING.
DEFAULT_MATCH_THRESHOLD = 0.45

#: Age dampening — the fact-decay MISP retention curve applied to the
#: QUESTION's age (days since produced_at). An old question matches colder:
#: at the floor even 3 shared entities + geo (0.66 raw ⇒ 0.396) no longer
#: clears the threshold, so a long-standing question needs vector support to
#: be re-flagged. Floored (never zero) so a standing open question is
#: dampened, not retired, by age alone. NOTE the curve is slow — see the
#: module docstring; it is not what keeps weak matches out.
QUESTION_LIFETIME_DAYS = 365.0
QUESTION_DECAY_SPEED = 0.40
AGE_FACTOR_FLOOR = 0.60

# ---------------------------------------------------------------------------
# Caps (bounded work per run; overrun is DEFERRED + reported, never silent)
# ---------------------------------------------------------------------------

DEFAULT_SIGNAL_CAP = 500
DEFAULT_QUESTION_CAP = 500
DEFAULT_EDGE_CAP = 200

# -- cursor policy: a bounded, COUNTED freshness horizon --------------------
#
# The oldest-first watermark alone cannot keep pace: it advances only over
# fully-processed signals, so any run whose edge budget binds advances by less
# than the stream moved and the lag compounds forever (measured live: 39
# processed, 461 deferred, ~70 ingested per tick). A permanently-lagging
# cursor is also a permanently STARVED vector plane, because signal_embedder
# drains newest-first — so the two failures are one failure.
#
# The fix is a horizon, not a bigger bucket. The cursor may never sit further
# behind the wall clock than DEFAULT_MAX_CURSOR_LAG_SECONDS; when it does, the
# run ABANDONS the intervening signals, COUNTS them exactly, WARNs, and
# restarts at the horizon. That is the honest trade this analyst is for:
# matching NEW evidence against old questions, where a day-old signal is worth
# far less than staying current — but never a silent one.

#: The freshness horizon. 6h = 12 ticks of the 30-minute cadence. Sized so
#: ordinary catch-up NEVER trips it: at the measured ~70 signals/tick ingest a
#: 6h backlog is ~840 signals, and a healthy run drains up to
#: ``signal_cap`` (500) per tick against those ~70, i.e. ~430/tick of net
#: catch-up — under two ticks of work. Only a STRUCTURAL stall reaches it.
#: ``<= 0`` disables skip-ahead entirely (a deliberate operator choice to
#: grind the whole backlog, at the cost of the pace guarantee).
DEFAULT_MAX_CURSOR_LAG_SECONDS = 6 * 3600.0

#: Upper bound on the skip-count probe, so measuring an abandonment can never
#: itself become an unbounded scan. Saturation is REPORTED
#: (``skip_count_clipped``) rather than rounded off — an under-count of
#: abandoned work would read as "we covered nearly everything".
_MAX_SKIP_COUNT_PROBE = 200_000

#: Tail-hold grace. The freshest rows in a batch are routinely not embedded
#: yet (signal_embedder runs on its own 15-minute tick), and processing them
#: blind is how the vector plane contributes nothing even when everything
#: works. They are HELD for one tick instead — but only while they are young
#: enough for the newest-first embedder to plausibly still be coming. Past
#: this age the embedder is not coming and the matcher processes them rather
#: than waiting forever.
_UNEMBEDDED_HOLD_MAX_AGE_SECONDS = 2 * 3600.0

#: OMNIBUS DAMPER — distinct questions one signal may bear on in one run
#: (one edge per question, so this is the per-signal edge cap too). Lowered
#: from 20 by measurement: over the 11,195 live 3.x edges, 943 source signals
#: produced a MEDIAN of 15 questions each with p90 = p99 = max = 20, so the
#: old bound engaged only in the top decile while half of every run was
#: already spraying. Deliberately a BACKSTOP above what the meta exclusion and
#: the global-ubiquity discount should leave — a focused wire story bearing on
#: more than 8 distinct standing questions is an omnibus artifact (live blog,
#: day digest, press review), not evidence. Strongest kept; the overflow is
#: counted (``edges_dropped_per_signal_cap``) and so is the number of signals
#: it engaged on (``omnibus_capped``).
MAX_QUESTIONS_PER_SIGNAL = 8
DEFAULT_FLAG_CAP = 200
#: Per-run budget of hosted embed calls (QUESTION texts only — signal vectors
#: are read back from Qdrant, never re-embedded here).
DEFAULT_EMBED_CAP = 64
#: Point ids per ``retrieve_vectors`` call. Bounds both the response size and
#: the blast radius of one transport failure (see :func:`_fetch_signal_vectors`).
_MAX_VECTOR_FETCH_IDS = 128
#: Per-run budget of resolve_keeper name resolutions (NER fallback only).
_MAX_RESOLVE_CALLS = 200
#: NER surface names considered per un-linked signal.
_MAX_NER_NAMES = 8
#: Forward consumption walk bounds.
FORWARD_WALK_MAX_DEPTH = 6
_MAX_CONSUMERS_PER_QUESTION = 50
#: Question thesis chars embedded (bounded input, the signal_embedder rule).
_MAX_QUESTION_EMBED_CHARS = 2000
#: In-process question-embedding cache bound (cleared wholesale on overflow —
#: the set is bounded by design; overflow means churn, and a re-embed of a few
#: hundred cached rows is cheaper than an eviction policy nobody audits).
_EMBED_CACHE_MAX = 2000
#: Per-embed wall-clock timeout (mirrors signal_embedder).
_EMBED_TIMEOUT_SECONDS = 30.0

#: Question statuses scanned by default (operator may add 'active' via
#: options.question_statuses).
DEFAULT_QUESTION_STATUSES = ("open_question",)

_EPOCH_ISO = "1970-01-01T00:00:00+00:00"
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"

#: Process-lifetime question-embedding cache: {hypothesis_id: (text_sha, vec)}.
_QUESTION_EMBED_CACHE: dict[str, tuple[str, list[float]]] = {}


# ---------------------------------------------------------------------------
# Pure helpers (testable with NO database)
# ---------------------------------------------------------------------------


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain cosine in [−1, 1]; 0.0 on any degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def question_age_factor(produced_at: Optional[datetime], now: datetime) -> float:
    """Age dampening for one question — the MISP retention curve over days
    since ``produced_at``, floored at :data:`AGE_FACTOR_FLOOR`.

    ``None`` (defensive; the column is NOT NULL) reads as fully aged →
    the floor, never a crash and never full freshness."""
    if produced_at is None:
        return AGE_FACTOR_FLOOR
    ref = produced_at if produced_at.tzinfo else produced_at.replace(tzinfo=timezone.utc)
    now_ref = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    elapsed_days = max(0.0, (now_ref - ref).total_seconds() / 86400.0)
    r = retention_factor(
        elapsed_days,
        lifetime_days=QUESTION_LIFETIME_DAYS,
        decay_speed=QUESTION_DECAY_SPEED,
    )
    return max(AGE_FACTOR_FLOOR, r)


def entity_component(shared_entities: float) -> float:
    """The GRADED entity contribution for ``n`` shared canonical entities —
    0 for none, :data:`W_ENTITY_FIRST` for the first, plus
    :data:`W_ENTITY_ADDITIONAL` per further one up to
    :data:`MAX_SHARED_ENTITIES_COUNTED`.

    Graded rather than boolean because a SINGLE shared entity on a country
    desk is co-membership, not evidence (see the module docstring's fusion
    table); the discriminating power is in sharing SEVERAL.

    ``n`` is a FLOAT, not a count: each shared entity contributes its
    desk-relative :func:`entity_specificity` rather than a flat 1, so three
    shared names that every question on the desk carries are not three names'
    worth of evidence. Whole numbers give exactly the integer-count values
    (0.20 / 0.38 / 0.56), and the sub-one interval ramps linearly from 0 so
    the function is continuous and MONOTONE — specificity can only ever LOWER
    a pair's weight, never raise one, which is what keeps every
    conservatism property of the model intact by construction."""
    n = min(max(0.0, float(shared_entities)), float(MAX_SHARED_ENTITIES_COUNTED))
    if n <= 0.0:
        return 0.0
    if n <= 1.0:
        return W_ENTITY_FIRST * n
    return W_ENTITY_FIRST + W_ENTITY_ADDITIONAL * (n - 1.0)


def entity_specificity(document_frequency: float) -> float:
    """What ONE shared entity is worth given how ubiquitous it is on its desk.

    ``document_frequency`` is the fraction of that desk's scanned questions
    whose lineage carries the entity. Fully specific (1.0) up to
    :data:`DF_UBIQUITY_KNEE`, then ramping down to
    :data:`ENTITY_SPECIFICITY_FLOOR` at df = 1.0 — the desk's own country and
    leader are nearly free, a rare co-mention is real evidence."""
    df = min(1.0, max(0.0, float(document_frequency)))
    if df <= DF_UBIQUITY_KNEE:
        return 1.0
    span = 1.0 - DF_UBIQUITY_KNEE
    if span <= 0.0:
        return ENTITY_SPECIFICITY_FLOOR
    return 1.0 - (1.0 - ENTITY_SPECIFICITY_FLOOR) * ((df - DF_UBIQUITY_KNEE) / span)


def global_entity_specificity(document_frequency: float) -> float:
    """What ONE shared entity is worth given how ubiquitous it is IN THE
    STREAM — the signal-side half of the ubiquity problem
    :func:`entity_specificity` cannot see.

    ``document_frequency`` is the fraction of the recent window's ATTRIBUTED
    signals (rows carrying any entity link) that carry this entity. Fully
    specific up to :data:`GLOBAL_DF_UBIQUITY_KNEE`, ramping linearly to
    :data:`ENTITY_SPECIFICITY_FLOOR` at :data:`GLOBAL_DF_SATURATION` and
    holding there. Continuous, monotone-decreasing, floored — and computed
    from the stream, so no name is ever hardcoded as a hub."""
    df = min(1.0, max(0.0, float(document_frequency)))
    if df <= GLOBAL_DF_UBIQUITY_KNEE:
        return 1.0
    span = GLOBAL_DF_SATURATION - GLOBAL_DF_UBIQUITY_KNEE
    if span <= 0.0 or df >= GLOBAL_DF_SATURATION:
        return ENTITY_SPECIFICITY_FLOOR
    return 1.0 - (1.0 - ENTITY_SPECIFICITY_FLOOR) * (
        (df - GLOBAL_DF_UBIQUITY_KNEE) / span
    )


def combined_specificity(desk_specificity: float, global_specificity: float) -> float:
    """The two ubiquity discounts composed onto ONE shared entity's worth.

    Multiplicative (a name may be discounted by its desk, by the stream, or
    by both) and clamped at the single :data:`ENTITY_SPECIFICITY_FLOOR`, so
    the "floored, not zeroed" property holds for the composition and not just
    for each part: a name that is BOTH desk furniture AND a stream hub is
    still weak evidence, not anti-evidence.

    Both inputs live in ``[ENTITY_SPECIFICITY_FLOOR, 1.0]``, so the result is
    non-decreasing in each and never exceeds either — the composition can
    only ever LOWER a weight, which is what keeps every conservatism property
    of the fusion model intact by construction."""
    a = min(1.0, max(0.0, float(desk_specificity)))
    b = min(1.0, max(0.0, float(global_specificity)))
    return max(ENTITY_SPECIFICITY_FLOOR, a * b)


def harvest_class(diagnostic_evidence: Any) -> Optional[str]:
    """The harvest class of one open question, or ``None``.

    Read from the durable ``hypotheses.diagnostic_evidence`` marker
    ``scripts/harvest_open_questions.py`` stamps::

        {"marker": "open_question_origin", "origin": "harvest",
         "harvest_class": "<class>", "source_id": "..."}

    Questions the agency faucet or a unit payload opened carry the same
    marker key with a different ``origin`` and NO ``harvest_class`` — those
    read ``None`` and are never meta."""
    data = _parse_jsonish(diagnostic_evidence)
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, Mapping):
            continue
        if item.get("marker") != HARVEST_MARKER_KEY:
            continue
        hc = item.get("harvest_class")
        if isinstance(hc, str) and hc.strip():
            return hc.strip()
    return None


def is_meta_question(
    diagnostic_evidence: Any,
    meta_classes: frozenset[str] | set[str] = META_QUESTION_CLASSES,
) -> bool:
    """True when a question is about the SYSTEM'S OWN analysis products
    rather than the world — see :data:`META_QUESTION_CLASSES`."""
    hc = harvest_class(diagnostic_evidence)
    return hc is not None and hc in meta_classes


def dedupe_by_canonical_url(rows: Sequence[Any]) -> tuple[list[Any], int]:
    """(kept rows, dropped count) — one article ingested twice is ONE
    document's worth of evidence, not two.

    Keeps the NEWEST row per non-empty ``canonical_url``. Rows arrive in
    ascending ``(fetched_at, id)`` order, so the newest occurrence is the
    LAST one — which is also why the batch's final row can never be dropped
    and the cursor advance is untouched by this filter. Rows with no
    canonical url are never deduped (an absent url is not a shared identity).
    """
    last_at: dict[str, int] = {}
    for i, r in enumerate(rows):
        url = str(r["canonical_url"] or "").strip()
        if url:
            last_at[url] = i
    kept: list[Any] = []
    for i, r in enumerate(rows):
        url = str(r["canonical_url"] or "").strip()
        if url and last_at[url] != i:
            continue  # an older row for the same article — counted, not kept
        kept.append(r)
    return kept, len(rows) - len(kept)


def build_entity_specificity(
    question_keys: Mapping[str, set[str]],
    desk_of: Mapping[str, str],
    *,
    min_questions: int = MIN_DESK_QUESTIONS_FOR_SPECIFICITY,
) -> dict[tuple[str, str], float]:
    """{(desk, entity key): specificity} for the entities this run's scanned
    questions carry — computed from ALREADY-LOADED data, no query.

    ``question_keys`` maps question id → its lineage entity keys (ids on one
    call, canonical names on another — the two surfaces are scored
    separately because they are compared separately). ``desk_of`` maps EVERY
    scanned question to its desk (``target_id``), including questions with no
    lineage entities: they are part of the denominator.

    Questions with no ``target_id`` share one bucket (``""``). An entity
    ubiquitous across THAT bucket is globally ubiquitous, which is the same
    finding by a weaker route, so the rule applies there too.

    Only DOWN-weighted entries are returned — a missing key reads 1.0, so a
    desk below ``min_questions`` (where df is not estimable) contributes
    nothing and scores on raw counts."""
    desk_questions: dict[str, int] = {}
    desk_entities: dict[str, dict[str, int]] = {}
    for qid, desk in desk_of.items():
        desk_questions[desk] = desk_questions.get(desk, 0) + 1
        counts = desk_entities.setdefault(desk, {})
        for key in question_keys.get(qid) or ():
            counts[key] = counts.get(key, 0) + 1

    out: dict[tuple[str, str], float] = {}
    for desk, n_questions in desk_questions.items():
        if n_questions < min_questions:
            continue
        for key, seen in desk_entities.get(desk, {}).items():
            spec = entity_specificity(seen / n_questions)
            if spec < 1.0:
                out[(desk, key)] = spec
    return out


def fuse_weight(
    *,
    vector_sim: Optional[float],
    shared_entities: float,
    geo_overlap: bool,
    age_factor: float,
) -> tuple[float, list[str]]:
    """(fused weight, contributing planes) for one (signal, question) pair.

    The vector plane contributes only at/above :data:`VECTOR_SIM_FLOOR`
    (a weak semantic echo is noise, not evidence); the entity plane is
    graded by :func:`entity_component`; geo is a tie-breaker. Planes list
    only the contributors — an edge's ``planes`` column must name what
    produced it.

    CONSERVATIVE BY CONSTRUCTION: no combination that lacks either vector
    support or two shared entities' worth of SPECIFIC overlap can reach
    :data:`DEFAULT_MATCH_THRESHOLD`. In particular one shared entity plus
    geo — the desk-co-membership case — fuses to 0.30 and writes nothing,
    and three entities that every question on the desk carries fuse LOWER
    than that (see :func:`entity_specificity`).
    """
    planes: list[str] = []
    raw = 0.0
    if vector_sim is not None and vector_sim >= VECTOR_SIM_FLOOR:
        raw += W_VECTOR * min(1.0, max(0.0, vector_sim))
        planes.append("vector")
    entity_raw = entity_component(shared_entities)
    if entity_raw > 0.0:
        raw += entity_raw
        planes.append("entity")
    if geo_overlap:
        raw += W_GEO
        planes.append("geo")
    return raw * age_factor, planes


def _parse_jsonish(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def _ner_names(payload: Any, cap: int = _MAX_NER_NAMES) -> list[str]:
    """Bounded NER surface names out of ``signals.payload.entities``
    (entries are strings or dicts with a name/text field)."""
    data = _parse_jsonish(payload)
    if not isinstance(data, Mapping):
        return []
    ents = data.get("entities")
    if not isinstance(ents, list):
        return []
    names: list[str] = []
    for e in ents:
        name = None
        if isinstance(e, str):
            name = e
        elif isinstance(e, Mapping):
            for k in ("name", "text", "entity"):
                v = e.get(k)
                if isinstance(v, str) and v.strip():
                    name = v
                    break
        if name and name.strip():
            names.append(name.strip())
        if len(names) >= cap:
            break
    return names


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_NEWEST_SIGNAL_SQL = """
    SELECT fetched_at, id
      FROM signals
     ORDER BY fetched_at DESC, id DESC
     LIMIT 1
"""

_NEW_SIGNALS_SQL = """
    SELECT id, fetched_at, geo, payload, canonical_url
      FROM signals
     WHERE (fetched_at, id) > ($1::timestamptz, $2::uuid)
     ORDER BY fetched_at ASC, id ASC
     LIMIT $3
"""

# EXACT count of the signals a skip-ahead abandons, over the half-open range
# (old cursor, horizon]. Bounded by a probe LIMIT so measuring an abandonment
# can never itself become an unbounded scan; saturation is reported, not
# rounded away.
_SKIPPED_SIGNAL_COUNT_SQL = """
    SELECT count(*)::bigint AS skipped
      FROM (
        SELECT 1
          FROM signals
         WHERE (fetched_at, id) > ($1::timestamptz, $2::uuid)
           AND (fetched_at, id) <= ($3::timestamptz, $4::uuid)
         LIMIT $5
      ) probe
"""

_OPEN_QUESTIONS_SQL = """
    SELECT id, thesis, status, target_id, produced_at, derived_from,
           supporting_signals, refuting_signals, diagnostic_evidence
      FROM hypotheses
     WHERE status = ANY($1::text[])
     ORDER BY produced_at DESC, id
     LIMIT $2
"""

# Per-question lineage expansion (bounded two-hop: hypothesis.derived_from →
# facts'/findings' derived_from → facts-cited-by-findings' derived_from) down
# to SIGNAL ids, plus the canonical entity ids linked to those signals
# (one-level merged_into fold — losers converge onto the elected row).
_QUESTION_LINEAGE_SQL = """
    WITH q AS (
        SELECT id, derived_from FROM hypotheses WHERE id = ANY($1::uuid[])
    ), l0 AS (
        SELECT q.id AS qid, unnest(q.derived_from) AS ref FROM q
    ), l1 AS (
        SELECT l0.qid, unnest(fx.derived_from) AS ref
          FROM facts fx JOIN l0 ON fx.id = l0.ref
        UNION
        SELECT l0.qid, unnest(ao.derived_from) AS ref
          FROM analyst_outputs ao JOIN l0 ON ao.id = l0.ref
    ), l2 AS (
        SELECT l1.qid, unnest(fx.derived_from) AS ref
          FROM facts fx JOIN l1 ON fx.id = l1.ref
    ), refs AS (
        SELECT qid, ref FROM l0
        UNION SELECT qid, ref FROM l1
        UNION SELECT qid, ref FROM l2
    ), qsig AS (
        SELECT DISTINCT r.qid, s.id AS sid
          FROM refs r JOIN signals s ON s.id = r.ref
    )
    SELECT qs.qid::text AS qid,
           array_agg(DISTINCT qs.sid) AS lineage_signal_ids,
           COALESCE(
             array_agg(DISTINCT COALESCE(ep.merged_into, sel.entity_id))
               FILTER (WHERE sel.entity_id IS NOT NULL),
             '{}'::uuid[]
           ) AS entity_ids
      FROM qsig qs
      LEFT JOIN signal_entity_links sel ON sel.signal_id = qs.sid
      LEFT JOIN entity_profiles ep ON ep.id = sel.entity_id
     GROUP BY qs.qid
"""

_SIGNAL_ENTITIES_SQL = """
    SELECT sel.signal_id::text AS sid,
           array_agg(DISTINCT COALESCE(ep.merged_into, sel.entity_id))
               AS entity_ids
      FROM signal_entity_links sel
      LEFT JOIN entity_profiles ep ON ep.id = sel.entity_id
     WHERE sel.signal_id = ANY($1::uuid[])
     GROUP BY sel.signal_id
"""

# GLOBAL (signal-side) entity document frequency over a recent stream window.
#
# Denominator = the ATTRIBUTED signals in the window (rows carrying ANY entity
# link), NOT every signal: an unlinked row is a MISSING OBSERVATION (the
# resolution sweep has not reached it, which is exactly the case this
# handler's NER fallback exists for), not evidence that a name is rare.
# Counting unlinked rows in the denominator deflates every df uniformly —
# measured on the live DB, only 224 of the 500 newest signals were attributed,
# so it would have understated every hub by a factor of ~2.2.
#
# Numerator is restricted to the entities that could POSSIBLY be shared (the
# question side's canonical set), so the result stays a few hundred rows
# regardless of how many entities the window mentions. The denominator is
# computed over the whole window either way — restricting it would be the
# same deflation in reverse.
_GLOBAL_ENTITY_DF_SQL = """
    WITH win AS (
        SELECT id FROM signals ORDER BY fetched_at DESC, id DESC LIMIT $1
    ), attributed AS (
        SELECT DISTINCT sel.signal_id
          FROM signal_entity_links sel
          JOIN win ON win.id = sel.signal_id
    ), df AS (
        SELECT COALESCE(ep.merged_into, sel.entity_id) AS eid,
               count(DISTINCT sel.signal_id)::bigint AS n
          FROM signal_entity_links sel
          JOIN attributed a ON a.signal_id = sel.signal_id
          LEFT JOIN entity_profiles ep ON ep.id = sel.entity_id
         WHERE COALESCE(ep.merged_into, sel.entity_id) = ANY($2::uuid[])
         GROUP BY 1
    )
    SELECT sample.attributed_signals,
           df.eid::text AS entity_id,
           df.n AS df
      FROM (SELECT count(*)::bigint AS attributed_signals FROM attributed) sample
      LEFT JOIN df ON TRUE
"""

_ENTITY_NAMES_SQL = """
    SELECT id, lower(btrim(canonical_name)) AS name
      FROM entity_profiles
     WHERE id = ANY($1::uuid[])
"""

_DESK_GEO_SQL = """
    SELECT descriptor_id, body -> 'scope' -> 'geo' AS geo
      FROM target_descriptors
     WHERE is_head = TRUE
       AND descriptor_id = ANY($1::text[])
"""

# FORWARD consumption walk from one question id to its live (non-superseded)
# consumer outputs. A consumer with no analyst_outputs row (journal entries)
# counts as live — supersession is only observable on analyst_outputs.
_FORWARD_WALK_SQL = """
    WITH RECURSIVE walk AS (
        SELECT oc.consumer_id, 1 AS depth
          FROM output_consumption oc
         WHERE oc.consumed_id = $1
        UNION
        SELECT oc.consumer_id, w.depth + 1
          FROM output_consumption oc
          JOIN walk w ON oc.consumed_id = w.consumer_id
         WHERE w.depth < $2
    )
    SELECT DISTINCT w.consumer_id
      FROM walk w
     WHERE NOT EXISTS (
           SELECT 1 FROM analyst_outputs ao
            WHERE ao.id = w.consumer_id
              AND ao.superseded_by IS NOT NULL
     )
     LIMIT $3
"""

# ``data`` (migration 0116) carries the bearing-gate stamp. With the gate OFF
# the writer binds '{}' — the column's own DEFAULT — so a gate-off run stores
# exactly the bytes 3.2.0 stored and the X-1 "absent option changes nothing"
# contract holds at the storage layer, not merely in the handler.
_INSERT_EDGE_SQL = """
    INSERT INTO bearing_edges
        (edge_kind, src_kind, src_id, src_as_of, dst_kind, dst_id, dst_as_of,
         weight, planes, provenance_class, matcher_version, data)
    VALUES ('bears_on', 'signal', $1, $2, 'hypothesis', $3, $4, $5,
            $6::text[], 'live', $7, $8::jsonb)
    ON CONFLICT (src_id, dst_id, edge_kind) DO NOTHING
"""

_INSERT_FLAG_SQL = """
    INSERT INTO review_flags (output_id, founded_on_id, moved_at, reason)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (output_id, founded_on_id) WHERE closed_at IS NULL DO NOTHING
"""

# The staleness-debt gauge: open flags whose flagged consumer is still a live
# (non-superseded) head. Closed-by-supersession flags are excluded by the
# closed_at test; flags whose consumer got superseded (but nobody closed the
# flag yet) are excluded by the liveness test — the debt only counts review
# work that still has a live product to re-review.
_STALENESS_DEBT_SQL = """
    SELECT count(*)::int AS debt
      FROM review_flags rf
     WHERE rf.closed_at IS NULL
       AND NOT EXISTS (
             SELECT 1 FROM analyst_outputs ao
              WHERE ao.id = rf.output_id
                AND ao.superseded_by IS NOT NULL
       )
"""


def _uuid_or_none(raw: Any) -> Optional[UUID]:
    try:
        return UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Vector plane (reuses the signal_embedder deps wiring — see module docstring)
# ---------------------------------------------------------------------------


def _resolve_vector_plane(deps: Any | None) -> tuple[Any | None, Any | None]:
    """(qdrant store, hosted embedder) off ``deps.extras`` under the
    signal_embedder keys — the SAME plane, not a second stack. Either absent
    → the vector plane degrades to 0 for every pair this run."""
    if deps is None:
        return None, None
    extras = getattr(deps, "extras", None)
    if not isinstance(extras, Mapping):
        return None, None
    return extras.get(QDRANT_DEPS_EXTRA_KEY), extras.get(EMBEDDER_DEPS_EXTRA_KEY)


async def _fetch_signal_vectors(
    store: Any, signal_ids: list[str]
) -> tuple[dict[str, list[float]], int]:
    """Stored signal vectors by point id, plus the count of FAILED chunks.

    CHUNKED (:data:`_MAX_VECTOR_FETCH_IDS` ids per retrieve): a signal batch
    is up to ``signal_cap`` ids and each vector is ~1k floats, so a single
    whole-batch retrieve is both a large response and a single point of
    failure — one transport error would zero the plane for the entire run.
    Chunking bounds each call and keeps a failure PARTIAL.

    Degrade-not-break per chunk, but the failure count is RETURNED (not
    swallowed) so the caller can tell "the embedder sweep has not reached
    these signals yet" apart from "the vector store is broken" — the two
    look identical in an empty dict, and conflating them is what let the
    KW-3 first-live starvation go unnoticed."""
    if not signal_ids:
        return {}, 0
    collection = getattr(getattr(store, "cfg", None), "signals_collection", None)
    collection = collection or "legba_signals"
    out: dict[str, list[float]] = {}
    errors = 0
    for start in range(0, len(signal_ids), _MAX_VECTOR_FETCH_IDS):
        chunk = signal_ids[start : start + _MAX_VECTOR_FETCH_IDS]
        try:
            got = await store.retrieve_vectors(collection, chunk)
        except Exception as exc:  # noqa: BLE001 — plane degrade, never a run kill
            errors += 1
            logger.warning(
                "claim_watch.signal_vectors_chunk_failed ids=%d err=%s — the "
                "vector plane contributes nothing for these signals",
                len(chunk),
                exc,
            )
            continue
        for k, v in (got or {}).items():
            if v:
                out[str(k)] = list(v)
    return out, errors


async def _embed_questions(
    embedder: Any,
    questions: list[Mapping[str, Any]],
    *,
    embed_cap: int,
) -> tuple[dict[str, list[float]], dict[str, int]]:
    """Question-thesis embeddings, embed-on-first-sight with the in-process
    cache. Returns ({question_id: vector}, stats). Bounded: at most
    ``embed_cap`` hosted calls per run — questions beyond the budget keep
    their entity/geo planes this run and warm the cache on a later one."""
    import asyncio

    stats = {"question_embeds": 0, "question_embed_cache_hits": 0,
             "question_embed_failures": 0}
    if len(_QUESTION_EMBED_CACHE) > _EMBED_CACHE_MAX:
        _QUESTION_EMBED_CACHE.clear()

    out: dict[str, list[float]] = {}
    for q in questions:
        qid = str(q["id"])
        text = str(q["thesis"] or "").strip()[:_MAX_QUESTION_EMBED_CHARS]
        if not text:
            continue
        sha = _text_sha(text)
        cached = _QUESTION_EMBED_CACHE.get(qid)
        if cached is not None and cached[0] == sha:
            out[qid] = cached[1]
            stats["question_embed_cache_hits"] += 1
            continue
        if stats["question_embeds"] + stats["question_embed_failures"] >= embed_cap:
            continue  # budget spent — this question keeps entity/geo only
        try:
            vec = await asyncio.wait_for(
                embedder.embed(text), timeout=_EMBED_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 — degrade the one question
            stats["question_embed_failures"] += 1
            logger.warning(
                "claim_watch.question_embed_failed question=%s err=%s", qid, exc
            )
            continue
        if not vec:
            stats["question_embed_failures"] += 1
            continue
        vec = list(vec)
        _QUESTION_EMBED_CACHE[qid] = (sha, vec)
        out[qid] = vec
        stats["question_embeds"] += 1
    return out, stats


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def _build_receipt(counters: Mapping[str, Any]) -> FindingPayload:
    title = (
        f"Claim watch: {counters.get('edges_written', 0)} bearing edge(s), "
        f"{counters.get('flags_written', 0)} review flag(s), "
        f"staleness_debt={counters.get('staleness_debt', 0)}"
    )
    if counters.get("seeded"):
        title = f"{title} (seeded — first scan writes nothing)"
    # An abandonment belongs in the TITLE, not only in the body: a receipt
    # that reads clean while the run gave up on 400 signals is the silent
    # truncation this project refuses.
    skipped = int(counters.get("signals_skipped_ahead", 0) or 0)
    if skipped:
        clipped = "≥" if counters.get("skip_count_clipped") else ""
        title = (
            f"{title} — SKIPPED {clipped}{skipped} signal(s) past the "
            f"freshness horizon"
        )
    # The v3.2 levers in the TITLE, not only the body, for the same reason the
    # abandonment is: a receipt that reads "3 edges" while the run refused
    # half the question set and damped two omnibus signals is not a receipt of
    # what happened. Each is only named when it actually engaged.
    damping: list[str] = []
    meta_skipped = int(counters.get("skipped_meta_questions", 0) or 0)
    if meta_skipped:
        damping.append(f"{meta_skipped} meta question(s) not scored")
    omnibus = int(counters.get("omnibus_capped", 0) or 0)
    if omnibus:
        damping.append(f"{omnibus} omnibus signal(s) capped")
    url_dupes = int(counters.get("signals_url_deduped", 0) or 0)
    if url_dupes:
        damping.append(f"{url_dupes} duplicate url(s) dropped")
    if damping:
        title = f"{title} [{'; '.join(damping)}]"
    # The BEARING GATE in the title whenever it is ON, for the same reason the
    # abandonment and the v3.2 levers are: a receipt reading "3 edges" while a
    # model refused 40 more — or while the 8B was down and 43 edges carry an
    # 'unavailable' stamp — is not a receipt of what happened.
    if counters.get("bearing_gate_mode") == "on":
        title = (
            f"{title} {{gate {counters.get('bearing_gate_yes', 0)} yes / "
            f"{counters.get('bearing_gated_out', 0)} refused / "
            f"{counters.get('bearing_gate_errors', 0)} unavailable / "
            f"{counters.get('bearing_gate_deferred', 0)} deferred}}"
        )
    body = "\n".join(f"{k}={counters[k]}" for k in sorted(counters))
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


def _result(counters: Mapping[str, Any]) -> AnalystMethodResult:
    return AnalystMethodResult(
        finding=_build_receipt(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


async def _staleness_debt(conn: Any) -> int:
    row = await conn.fetchrow(_STALENESS_DEBT_SQL)
    return int(row["debt"]) if row is not None else 0


def _cursor_state(fetched_at: datetime, signal_id: Any) -> dict[str, str]:
    ts = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc)
    return {"fetched_at": ts.isoformat(), "signal_id": str(signal_id)}


def _parse_cursor(state: Mapping[str, Any]) -> tuple[datetime, UUID] | None:
    try:
        ts = datetime.fromisoformat(str(state["fetched_at"]))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts, UUID(str(state["signal_id"]))
    except (KeyError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — one matcher pass (see module docstring).

    REFUSES LOUD on a missing pool (the alert_trigger_scan contract): a
    matcher that cannot read the substrate must error visibly, never report
    a quiet zero-match run."""
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "claim_watch requires a live deps.pg_pool — refusing to report a "
            "zero-match run without reading the substrate"
        )

    threshold = float(options.get("match_threshold", DEFAULT_MATCH_THRESHOLD))
    signal_cap = max(1, int(options.get("signal_cap", DEFAULT_SIGNAL_CAP)))
    question_cap = max(1, int(options.get("question_cap", DEFAULT_QUESTION_CAP)))
    edge_cap = max(1, int(options.get("edge_cap", DEFAULT_EDGE_CAP)))
    flag_cap = max(1, int(options.get("flag_cap", DEFAULT_FLAG_CAP)))
    embed_cap = max(0, int(options.get("embed_cap", DEFAULT_EMBED_CAP)))
    max_lag_seconds = float(
        options.get("max_lag_seconds", DEFAULT_MAX_CURSOR_LAG_SECONDS)
    )
    hold_max_age = float(
        options.get("unembedded_hold_max_age_seconds",
                    _UNEMBEDDED_HOLD_MAX_AGE_SECONDS)
    )
    if max_lag_seconds > 0.0:
        # The hold may never reach past the horizon, or a run would hold rows
        # the NEXT run then abandons — deliberately keeping work only to throw
        # it away. The defaults are already 2h against 6h; this bounds a
        # misconfiguration, not the normal case.
        hold_max_age = min(hold_max_age, max_lag_seconds)
    statuses = [
        str(s)
        for s in (options.get("question_statuses") or DEFAULT_QUESTION_STATUSES)
        if str(s).strip()
    ] or list(DEFAULT_QUESTION_STATUSES)
    # L1 — the meta classes this run refuses to score. Operator-overridable
    # (the class taxonomy is data-defined and can grow); an EXPLICIT empty
    # list disables the lever, and the receipt still reports a zero.
    raw_meta = options.get("meta_question_classes")
    meta_classes = (
        frozenset(str(c).strip() for c in raw_meta if str(c).strip())
        if raw_meta is not None
        else META_QUESTION_CLASSES
    )
    # L2 — the global-ubiquity window and its inertness floor.
    df_window = max(0, int(options.get("global_df_window", GLOBAL_DF_WINDOW_SIGNALS)))
    df_min_signals = max(
        0,
        int(
            options.get(
                "global_df_min_signals", MIN_SIGNALS_FOR_GLOBAL_SPECIFICITY
            )
        ),
    )
    # L3 — the omnibus damper.
    max_questions_per_signal = max(
        1,
        int(
            options.get("max_questions_per_signal", MAX_QUESTIONS_PER_SIGNAL)
        ),
    )
    # W-B1/W-B2 — THE BEARING PIPELINE (see :mod:`.bearing_gate`). Read HERE,
    # in the sub-handler's own module, so the X-1 catalog drift guard (which
    # sweeps ``options.get("...")`` call sites out of this file) holds every
    # knob to the catalog with no delegation exception; the values are passed
    # to the pipeline as explicit arguments. DEFAULT OFF in code: a descriptor
    # with no ``method.options`` block is byte-identical to 3.2.0, and turning
    # the pipeline on is a descriptor PUT at deploy, not a rebuild.
    bearing_gate_mode = options.get("bearing_gate", DEFAULT_BEARING_GATE)
    bearing_gate_ref = str(
        options.get("bearing_gate_ref", DEFAULT_BEARING_GATE_REF)
    ).strip() or DEFAULT_BEARING_GATE_REF
    bearing_gate_cap = max(
        0, int(options.get("bearing_gate_cap", DEFAULT_BEARING_GATE_CAP))
    )
    bearing_confirm_cap = max(
        0, int(options.get("bearing_confirm_cap", DEFAULT_BEARING_CONFIRM_CAP))
    )
    # Resolved once so the OFF path costs literally nothing: with the gate off
    # the per-signal text digest below is never even built.
    bearing_gate_on = gate_enabled(bearing_gate_mode)

    counters: dict[str, Any] = {
        "seeded": False,
        "examined_signals": 0,
        "deferred_signals": 0,
        "signal_batch_truncated": False,
        "cursor_falling_behind": False,
        "cursor_lag_seconds": 0.0,
        "cursor_skipped_ahead": False,
        "signals_skipped_ahead": 0,
        "skip_count_clipped": False,
        "held_for_embedding": 0,
        "signals_url_deduped": 0,
        "questions_scanned": 0,
        "skipped_meta_questions": 0,
        "questions_matchable": 0,
        "edges_written": 0,
        "edges_deduped": 0,
        "edges_dropped_per_signal_cap": 0,
        "omnibus_capped": 0,
        "edges_dropped_run_cap": 0,
        "flags_written": 0,
        "flags_deduped": 0,
        "flags_dropped_cap": 0,
        "matches_vector": 0,
        "matches_entity": 0,
        "matches_geo": 0,
        "vector_plane_wired": False,
        "vector_plane_starved": False,
        "vector_plane_errors": 0,
        "signal_vectors_found": 0,
        "signal_vectors_missing": 0,
        "question_embeds": 0,
        "question_embed_cache_hits": 0,
        "question_embed_failures": 0,
        "entity_specificity_desks": 0,
        "entity_specificity_downweighted": 0,
        "global_specificity_sample": 0,
        "global_specificity_inert": False,
        "global_specificity_downweighted": 0,
        "staleness_debt": 0,
        "match_threshold": threshold,
        "matcher_version": MATCHER_VERSION,
        # W-B1/W-B2 — seeded at their INERT values on every path (including
        # the seed run and the no-signals early return), so every receipt
        # carries the full set and "the gate wrote nothing" is readable
        # without knowing which build produced the row.
        **bearing_counter_defaults(),
    }

    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        seeded, wm = await _load_class_watermarks(conn, TRIGGER_CLASS)
        cursor = _parse_cursor(wm.get(CURSOR_KEY) or {})

        if not seeded or cursor is None:
            # First-ever run (or an unreadable cursor): seed at the newest
            # signal SILENTLY — the 0091 bring-up contract; the backlog is
            # never flooded with edges.
            newest = await conn.fetchrow(_NEWEST_SIGNAL_SQL)
            state = (
                _cursor_state(newest["fetched_at"], newest["id"])
                if newest is not None
                else {"fetched_at": _EPOCH_ISO, "signal_id": _ZERO_UUID}
            )
            await _upsert_watermark(
                conn, TRIGGER_CLASS, CURSOR_KEY, state, fired=False
            )
            await _mark_seeded(conn, TRIGGER_CLASS)
            counters["seeded"] = True
            counters["staleness_debt"] = await _staleness_debt(conn)
            logger.info("claim_watch.seeded cursor=%s", state)
            return _result(counters)

        cursor_ts, cursor_id = cursor

        # The horizon is measured against the STREAM HEAD, not the wall clock:
        # "how far behind the newest signal is this cursor". Falling behind is
        # a property of the stream, so an ingest outage (head stops moving)
        # must not read as lag, and DB/app clock skew must not manufacture one.
        head_row = await conn.fetchrow(_NEWEST_SIGNAL_SQL)
        stream_head = now
        if head_row is not None and head_row["fetched_at"] is not None:
            stream_head = head_row["fetched_at"]
            if stream_head.tzinfo is None:
                stream_head = stream_head.replace(tzinfo=timezone.utc)
        lag_seconds = max(0.0, (stream_head - cursor_ts).total_seconds())
        counters["cursor_lag_seconds"] = round(lag_seconds, 1)

        # ------------------------------------------------------------------
        # FRESHNESS HORIZON — the pace guarantee. A cursor further behind the
        # stream head than the horizon cannot be caught up by working harder
        # (the whole failure mode: the run advances by less than the stream
        # moved, forever), so the intervening signals are ABANDONED — counted
        # exactly, warned about, and reported in the run receipt. Skipping is
        # a loss; a silent skip would be a lie, and an un-skipped cursor is a
        # permanently starved vector plane. The abandonment is persisted
        # BEFORE any matching so it is durable and counted exactly once.
        # ------------------------------------------------------------------
        if max_lag_seconds > 0.0 and lag_seconds > max_lag_seconds:
            horizon = stream_head - timedelta(seconds=max_lag_seconds)
            horizon_id = UUID(_ZERO_UUID)
            skipped = int(
                await conn.fetchval(
                    _SKIPPED_SIGNAL_COUNT_SQL,
                    cursor_ts,
                    cursor_id,
                    horizon,
                    horizon_id,
                    _MAX_SKIP_COUNT_PROBE,
                )
                or 0
            )
            # A lag with NOTHING behind it (a quiet stream, an ingest outage)
            # is not a skip: the batch select would return the same rows
            # either way. Only move — and only warn — when signals are being
            # given up.
            if skipped > 0:
                counters["cursor_skipped_ahead"] = True
                counters["signals_skipped_ahead"] = skipped
                counters["skip_count_clipped"] = skipped >= _MAX_SKIP_COUNT_PROBE
                await _upsert_watermark(
                    conn,
                    TRIGGER_CLASS,
                    CURSOR_KEY,
                    _cursor_state(horizon, horizon_id),
                    fired=False,
                )
                cursor_ts, cursor_id = horizon, horizon_id
                logger.warning(
                    "claim_watch.cursor_skipped_ahead skipped=%d%s lag_hours=%.1f "
                    "horizon_hours=%.1f — the cursor was further behind the "
                    "stream than the freshness horizon, so these signals were "
                    "ABANDONED (never matched against the open-question set) "
                    "and the cursor restarted at the horizon. This is a real "
                    "loss, reported not hidden: the matcher's value is NEW "
                    "evidence against old questions, and a permanently lagging "
                    "cursor also reads only signals signal_embedder "
                    "(newest-first) has not embedded, so the vector plane "
                    "starves. If this repeats every tick the MATCH RATE is too "
                    "high for the edge budget — fix the match rate, not the cap",
                    skipped,
                    " (clipped at the probe bound — at least this many)"
                    if counters["skip_count_clipped"]
                    else "",
                    lag_seconds / 3600.0,
                    max_lag_seconds / 3600.0,
                )

        signal_rows = list(
            await conn.fetch(_NEW_SIGNALS_SQL, cursor_ts, cursor_id, signal_cap + 1)
        )
        if len(signal_rows) > signal_cap:
            # At least one more batch waits. Recorded as its OWN flag, never
            # folded into deferred_signals — that counter must stay a true
            # count of signals this run deferred, not a count plus a sentinel.
            counters["signal_batch_truncated"] = True
            signal_rows = signal_rows[:signal_cap]

        # L3a — SAME-ARTICLE DUPLICATES. One article ingested twice is one
        # document's worth of evidence; left standing it doubles every edge it
        # earns and double-samples any precision measurement taken off them.
        # Runs BEFORE the vector fetch and the entity resolution so the dropped
        # rows cost neither. Keeps the newest row per url, which (oldest-first
        # ordering) is never the batch's last row — so the cursor advance is
        # untouched and a dropped row is passed over, not stranded.
        signal_rows, url_dupes = dedupe_by_canonical_url(signal_rows)
        counters["signals_url_deduped"] = url_dupes
        if url_dupes:
            logger.info(
                "claim_watch.url_deduped dropped=%d kept=%d — older rows sharing "
                "a canonical_url with a newer row in the same batch",
                url_dupes,
                len(signal_rows),
            )
        counters["examined_signals"] = len(signal_rows)

        if not signal_rows:
            counters["staleness_debt"] = await _staleness_debt(conn)
            return _result(counters)

        all_question_rows = await conn.fetch(
            _OPEN_QUESTIONS_SQL, statuses, question_cap
        )
        counters["questions_scanned"] = len(all_question_rows)

        # L1 — META-QUESTION EXCLUSION. A question about the system's own
        # analysis products (a collection gap, a floored claim, a composition's
        # validity) cannot be answered by a wire story; the K-4 gold set
        # measured those classes at 2/58 while substantive theses scored
        # 32/65. They are dropped HERE, before the embed budget, the lineage
        # walk and the specificity build, so the exclusion buys work back
        # rather than just muting output. They remain open questions in every
        # other read path — only this matcher ignores them, and it SAYS SO.
        question_rows = [
            q
            for q in all_question_rows
            if not is_meta_question(q["diagnostic_evidence"], meta_classes)
        ]
        counters["skipped_meta_questions"] = len(all_question_rows) - len(
            question_rows
        )
        counters["questions_matchable"] = len(question_rows)
        if counters["skipped_meta_questions"]:
            logger.info(
                "claim_watch.skipped_meta_questions skipped=%d matchable=%d "
                "classes=%s — questions about the system's own analysis "
                "products are not answerable by a news signal (K-4: 2/58); "
                "they stay OPEN, this matcher just does not score them",
                counters["skipped_meta_questions"],
                len(question_rows),
                ",".join(sorted(meta_classes)),
            )

        if not question_rows:
            # Nothing to match against — advance past the batch (this plane is
            # forward-only; questions created later see only later signals).
            last = signal_rows[-1]
            await _upsert_watermark(
                conn,
                TRIGGER_CLASS,
                CURSOR_KEY,
                _cursor_state(last["fetched_at"], last["id"]),
                fired=False,
            )
            counters["staleness_debt"] = await _staleness_debt(conn)
            return _result(counters)

        # ------------------------------------------------------------------
        # Vector plane FIRST — stored signal vectors + cached question
        # embeddings. Read before the entity work because its coverage
        # decides the TAIL-HOLD below, and holding a row is cheaper than
        # resolving its entities and then holding it.
        # ------------------------------------------------------------------
        store, embedder = _resolve_vector_plane(deps)
        sig_vecs: dict[str, list[float]] = {}
        q_vecs: dict[str, list[float]] = {}
        if store is not None and embedder is not None:
            counters["vector_plane_wired"] = True
            sig_vecs, vec_errors = await _fetch_signal_vectors(
                store, [str(s["id"]) for s in signal_rows]
            )
            counters["vector_plane_errors"] = vec_errors
            q_vecs, embed_stats = await _embed_questions(
                embedder, question_rows, embed_cap=embed_cap
            )
            counters.update(embed_stats)
            # A WIRED plane that covers NOTHING is the KW-3 first-live failure
            # mode: matching silently collapses onto the entity plane while
            # every boot log still says the plane is up. Refuse to report that
            # quietly — it is a starved plane, not a plane that had no work.
            if not sig_vecs or not q_vecs:
                counters["vector_plane_starved"] = True
                logger.warning(
                    "claim_watch.vector_plane_starved signals=%d "
                    "signal_vectors=%d question_vectors=%d chunk_errors=%d — "
                    "the vector plane is WIRED but contributes to NOTHING this "
                    "run; matching rests on the entity plane alone. Likely "
                    "causes: the cursor has fallen behind into signals "
                    "signal_embedder (newest-first) has not embedded, or the "
                    "vector store is unreachable (see chunk_errors)",
                    len(signal_rows),
                    len(sig_vecs),
                    len(q_vecs),
                    vec_errors,
                )
        else:
            logger.info(
                "claim_watch.vector_plane_absent store=%s embedder=%s — "
                "matching rests on entity+geo this run",
                "wired" if store is not None else "absent",
                "wired" if embedder is not None else "absent",
            )

        # ------------------------------------------------------------------
        # TAIL-HOLD — the freshest rows the embedder has not covered YET.
        #
        # In healthy steady state the matcher sits AT the stream head, so a
        # large share of every batch is younger than signal_embedder's last
        # (newest-first, 15-minute) sweep. Matching those blind is how the
        # vector plane contributes nothing even when everything works. They
        # are held back one tick instead — the cursor simply does not advance
        # past them, so nothing is lost and the next run scores them WITH
        # their vectors.
        #
        # Three guards keep the hold from becoming a wedge:
        #   * only when the batch reached the stream HEAD (not truncated) —
        #     a truncated batch means we are behind the head, where uncovered
        #     means the newest-first embedder has not come BACK this far and
        #     never will on this tick;
        #   * only while the plane covers something — a plane covering nothing
        #     is starvation (already warned above), not a head sliver;
        #   * only for rows younger than the hold grace, and never the whole
        #     batch, so a dead embedder costs at most that grace and the run
        #     always makes forward progress.
        # ------------------------------------------------------------------
        if (
            counters["vector_plane_wired"]
            and sig_vecs
            and not counters["signal_batch_truncated"]
        ):
            hold_floor = stream_head - timedelta(seconds=max(0.0, hold_max_age))
            while len(signal_rows) > 1:
                tail = signal_rows[-1]
                if str(tail["id"]) in sig_vecs:
                    break
                tail_ts = tail["fetched_at"]
                if tail_ts.tzinfo is None:
                    tail_ts = tail_ts.replace(tzinfo=timezone.utc)
                if tail_ts <= hold_floor:
                    break
                signal_rows.pop()
                counters["held_for_embedding"] += 1
            if counters["held_for_embedding"]:
                counters["examined_signals"] = len(signal_rows)
                logger.info(
                    "claim_watch.held_for_embedding held=%d examined=%d — the "
                    "batch's newest rows are not embedded yet; the cursor "
                    "stops short of them so the next tick scores them WITH "
                    "the vector plane (held, never dropped)",
                    counters["held_for_embedding"],
                    len(signal_rows),
                )

        signal_ids = [s["id"] for s in signal_rows]
        # Coverage is reported over what the run actually MATCHES ON, after
        # the hold: a held row is not a coverage miss, it is a row deliberately
        # postponed until it IS covered. (An unwired plane has no coverage to
        # report — a shortfall counter there would invent a fault.)
        if counters["vector_plane_wired"]:
            retained = {str(s) for s in signal_ids}
            counters["signal_vectors_found"] = sum(
                1 for k in sig_vecs if k in retained
            )
            counters["signal_vectors_missing"] = (
                len(retained) - counters["signal_vectors_found"]
            )

        qids = [q["id"] for q in question_rows]
        lineage_rows = await conn.fetch(_QUESTION_LINEAGE_SQL, qids)
        q_lineage_signals: dict[str, set[str]] = {}
        q_entity_ids: dict[str, set[str]] = {}
        all_q_entity_ids: set[Any] = set()
        for r in lineage_rows:
            qid = str(r["qid"])
            q_lineage_signals[qid] = {
                str(s) for s in (r["lineage_signal_ids"] or [])
            }
            ents = {e for e in (r["entity_ids"] or []) if e is not None}
            q_entity_ids[qid] = {str(e) for e in ents}
            all_q_entity_ids.update(ents)

        # Canonical names of the questions' entities (the NER-name fallback's
        # comparison surface).
        q_entity_names: dict[str, str] = {}
        if all_q_entity_ids:
            name_rows = await conn.fetch(
                _ENTITY_NAMES_SQL, sorted(all_q_entity_ids, key=str)
            )
            q_entity_names = {
                str(r["id"]): str(r["name"] or "") for r in name_rows
            }
        q_name_sets: dict[str, set[str]] = {
            qid: {q_entity_names[e] for e in ents if q_entity_names.get(e)}
            for qid, ents in q_entity_ids.items()
        }

        # Desk geo scope per question target.
        target_ids = sorted(
            {str(q["target_id"]) for q in question_rows if q["target_id"]}
        )
        desk_geo: dict[str, set[str]] = {}
        if target_ids:
            for r in await conn.fetch(_DESK_GEO_SQL, target_ids):
                geo_raw = _parse_jsonish(r["geo"])
                if isinstance(geo_raw, list):
                    desk_geo[str(r["descriptor_id"])] = {
                        str(g).strip().upper() for g in geo_raw if str(g).strip()
                    }

        # Signal-side canonical entity ids (linked) + NER-name fallback for
        # signals the resolution sweep has not linked yet.
        sig_entity_ids: dict[str, set[str]] = {}
        for r in await conn.fetch(_SIGNAL_ENTITIES_SQL, signal_ids):
            sig_entity_ids[str(r["sid"])] = {
                str(e) for e in (r["entity_ids"] or []) if e is not None
            }
        sig_names: dict[str, set[str]] = {}
        resolve_cache: dict[str, str] = {}
        resolve_calls = 0
        any_q_names = any(q_name_sets.values())
        for s in signal_rows:
            sid = str(s["id"])
            if sig_entity_ids.get(sid) or not any_q_names:
                continue
            names = _ner_names(s["payload"])
            if not names or resolve_calls >= _MAX_RESOLVE_CALLS:
                continue
            resolved: set[str] = set()
            for name in names:
                if resolve_calls >= _MAX_RESOLVE_CALLS:
                    break
                resolve_calls += 1
                kept = await resolve_keeper(conn, name, cache=resolve_cache)
                if kept and kept.strip():
                    resolved.add(kept.strip().lower())
            if resolved:
                sig_names[sid] = resolved

        # ------------------------------------------------------------------
        # Desk-relative entity SPECIFICITY — an IDF-shaped weight over the
        # question set ALREADY loaded (no table, no query, no sweep). An
        # entity carried by most of a desk's questions is that desk's
        # furniture, not evidence that this signal bears on THIS question;
        # a rarely-shared one is. Built per surface (canonical ids, resolved
        # NER names) because the two are compared separately.
        # ------------------------------------------------------------------
        desk_of = {
            str(q["id"]): str(q["target_id"] or "") for q in question_rows
        }
        spec_ids = build_entity_specificity(q_entity_ids, desk_of)
        spec_names = build_entity_specificity(q_name_sets, desk_of)
        desk_question_counts: dict[str, int] = {}
        for desk in desk_of.values():
            desk_question_counts[desk] = desk_question_counts.get(desk, 0) + 1
        counters["entity_specificity_desks"] = sum(
            1
            for n in desk_question_counts.values()
            if n >= MIN_DESK_QUESTIONS_FOR_SPECIFICITY
        )
        counters["entity_specificity_downweighted"] = len(spec_ids) + len(
            spec_names
        )

        # ------------------------------------------------------------------
        # L2 — GLOBAL (signal-side) entity ubiquity. The desk-relative df
        # above is question-side and desk-local, so a name that is everywhere
        # in the STREAM rides straight through it and bridges unrelated desks
        # (K-4: entity-only pairs 0/54, on Trump / United States / Iran /
        # Russia / bare demonyms / NER datelines). One bounded aggregate over
        # the newest GLOBAL_DF_WINDOW_SIGNALS signals gives each candidate
        # entity its stream document frequency; the discount is computed, not
        # curated — no name is named anywhere.
        #
        # INERT below MIN_SIGNALS_FOR_GLOBAL_SPECIFICITY attributed signals,
        # for exactly MIN_DESK_QUESTIONS_FOR_SPECIFICITY's reason: a df from
        # too few documents is not an estimate, and one burst story would
        # manufacture a hub. Inertness is REPORTED, not assumed away.
        # ------------------------------------------------------------------
        global_spec_ids: dict[str, float] = {}
        global_spec_names: dict[str, float] = {}
        if df_window > 0 and all_q_entity_ids:
            df_rows = await conn.fetch(
                _GLOBAL_ENTITY_DF_SQL,
                df_window,
                sorted(all_q_entity_ids, key=str),
            )
            sample = int(df_rows[0]["attributed_signals"] or 0) if df_rows else 0
            counters["global_specificity_sample"] = sample
            if sample < df_min_signals:
                counters["global_specificity_inert"] = True
                logger.info(
                    "claim_watch.global_specificity_inert attributed=%d "
                    "required=%d window=%d — the stream window carries too few "
                    "entity-attributed signals for a document frequency to "
                    "mean anything, so the global ubiquity discount does "
                    "NOTHING this run (desk-relative specificity still "
                    "applies)",
                    sample,
                    df_min_signals,
                    df_window,
                )
            elif sample > 0:
                for r in df_rows:
                    eid = r["entity_id"]
                    if eid is None:
                        continue  # the sample-only row of an empty df join
                    spec = global_entity_specificity(
                        int(r["df"] or 0) / float(sample)
                    )
                    if spec < 1.0:
                        global_spec_ids[str(eid)] = spec
                # The NER-name surface is compared by canonical name, so the
                # id-keyed discount is folded onto names through the SAME
                # name map the comparison uses. Where two ids fold to one
                # name (a merge the election has not converged yet) the MOST
                # ubiquitous reading wins — the conservative one.
                for eid, spec in global_spec_ids.items():
                    name = q_entity_names.get(eid)
                    if not name:
                        continue
                    prev = global_spec_names.get(name)
                    if prev is None or spec < prev:
                        global_spec_names[name] = spec
        counters["global_specificity_downweighted"] = len(global_spec_ids)

    # ------------------------------------------------------------------
    # Matching (pure, in memory). Edges accumulate signal-by-signal in
    # cursor order; the edge budget defers WHOLE signals so the cursor
    # only ever advances past fully-processed work.
    # ------------------------------------------------------------------
    age_factors = {
        str(q["id"]): question_age_factor(q["produced_at"], now)
        for q in question_rows
    }
    edge_rows: list[EdgeCandidate] = []
    matched_questions: dict[str, datetime] = {}
    last_processed: Any | None = None
    processed_count = 0
    budget_stop = False

    for s in signal_rows:
        sid = str(s["id"])
        s_geo = {str(g).strip().upper() for g in (s["geo"] or []) if str(g).strip()}
        s_ents = sig_entity_ids.get(sid, set())
        s_names = sig_names.get(sid, set())
        s_vec = sig_vecs.get(sid)

        cands: list[tuple[float, list[str], Any]] = []
        for q in question_rows:
            qid = str(q["id"])
            # Circularity guard — the question's OWN evidence never matches:
            # lineage signals, supporting_signals, refuting_signals, and the
            # raw derived_from members themselves.
            own = q_lineage_signals.get(qid, set())
            if (
                sid in own
                or any(sid == str(x) for x in (q["derived_from"] or []))
                or any(sid == str(x) for x in (q["supporting_signals"] or []))
                or any(sid == str(x) for x in (q["refuting_signals"] or []))
            ):
                continue

            vector_sim = None
            q_vec = q_vecs.get(qid)
            if s_vec is not None and q_vec is not None:
                vector_sim = cosine_similarity(s_vec, q_vec)

            # DISTINCT shared canonical entities, each weighted by its
            # COMBINED specificity — a graded sum, not a boolean and not a
            # flat count: one shared entity is desk co-membership, and three
            # shared entities that every question on the desk carries are the
            # SAME co-membership under three names (the 2.0.0 live run: 150
            # of 185 edges at exactly the 3-entity cap). The two discounts
            # answer two different questions — "do this DESK's questions all
            # carry this name" (desk-relative) and "does the STREAM carry it
            # everywhere" (global, K-4's 0/54 entity-only class) — and
            # compose multiplicatively under one floor. An entity that is
            # neither desk furniture nor a stream hub weighs its full 1.0.
            # The id and NER-name surfaces are disjoint by construction
            # (names are resolved only for signals with NO links), so summing
            # them cannot double-count one entity.
            desk = desk_of.get(qid, "")
            shared_entities = sum(
                combined_specificity(
                    spec_ids.get((desk, e), 1.0), global_spec_ids.get(e, 1.0)
                )
                for e in (s_ents & q_entity_ids.get(qid, set()))
            ) + sum(
                combined_specificity(
                    spec_names.get((desk, n), 1.0), global_spec_names.get(n, 1.0)
                )
                for n in (s_names & q_name_sets.get(qid, set()))
            )
            scope = desk_geo.get(str(q["target_id"] or ""), set())
            geo_overlap = bool(s_geo and scope and s_geo & scope)

            weight, planes = fuse_weight(
                vector_sim=vector_sim,
                shared_entities=shared_entities,
                geo_overlap=geo_overlap,
                age_factor=age_factors[qid],
            )
            if weight >= threshold and planes:
                cands.append((round(weight, 4), planes, q))

        # L3b — OMNIBUS DAMPER. A per-signal cap on DISTINCT QUESTIONS (one
        # edge per question, so also the per-signal edge cap): keep the
        # strongest, count what that costs. Two counters because they are two
        # different facts — the edges lost, and the number of SIGNALS the cap
        # engaged on (the omnibus population, which no edge count reveals).
        cands.sort(key=lambda c: (-c[0], str(c[2]["id"])))
        if len(cands) > max_questions_per_signal:
            counters["edges_dropped_per_signal_cap"] += (
                len(cands) - max_questions_per_signal
            )
            counters["omnibus_capped"] += 1
            logger.info(
                "claim_watch.omnibus_capped signal=%s candidates=%d cap=%d — "
                "one signal bearing on that many distinct standing questions "
                "is an omnibus artifact (live blog, digest, press review); "
                "keeping the strongest %d",
                sid,
                len(cands),
                max_questions_per_signal,
                max_questions_per_signal,
            )
            cands = cands[:max_questions_per_signal]

        # Per-run edge budget: defer this signal (and the rest) when the
        # budget cannot hold it — unless NOTHING was processed yet, in which
        # case trim to the budget so the run always makes forward progress.
        if len(edge_rows) + len(cands) > edge_cap:
            if last_processed is not None:
                budget_stop = True
                break
            # Nothing processed yet, so deferral would stall the cursor
            # forever. Trim to the budget to guarantee forward progress —
            # and count the loss under its OWN name. These candidates ARE
            # dropped (the cursor moves past this signal), so folding them
            # into the per-signal-cap counter would misreport a run-cap loss
            # as a per-signal one.
            counters["edges_dropped_run_cap"] += len(cands) - edge_cap
            cands = cands[:edge_cap]

        # Only the gate reads this text, so an OFF run never pays for it.
        s_digest = signal_digest(s["payload"]) if (cands and bearing_gate_on) else ""
        for weight, planes, q in cands:
            edge_rows.append(
                EdgeCandidate(
                    signal_id=s["id"],
                    signal_as_of=s["fetched_at"],
                    signal_text=s_digest,
                    question_id=q["id"],
                    question_as_of=q["produced_at"],
                    question_thesis=str(q["thesis"] or ""),
                    weight=weight,
                    planes=planes,
                )
            )
            # matches_* count what the DETERMINISTIC matcher produced — the
            # gate's refusals are counted separately (bearing_gated_out), so
            # the two together give the gate's measured effect on this run.
            for p in planes:
                counters[f"matches_{p}"] += 1
        last_processed = s
        processed_count += 1

    if budget_stop:
        counters["deferred_signals"] = len(signal_rows) - processed_count
        counters["examined_signals"] = processed_count
        logger.info(
            "claim_watch.edge_budget_deferral processed=%d deferred=%d cap=%d",
            processed_count,
            counters["deferred_signals"],
            edge_cap,
        )

    # The deferral is loss-free but not free: a run that both filled its
    # signal batch AND deferred work advanced the cursor by less than one
    # batch while the stream kept moving, so the cursor is LOSING GROUND.
    # Sustained, that is what parks the matcher in the un-embedded band and
    # starves the vector plane — so it is a warning, not a silent counter.
    if counters["signal_batch_truncated"] and counters["deferred_signals"]:
        counters["cursor_falling_behind"] = True
        logger.warning(
            "claim_watch.cursor_falling_behind processed=%d deferred=%d "
            "batch_cap=%d edge_cap=%d — the run filled its signal batch AND "
            "deferred work, so the cursor advanced less than one batch while "
            "the stream moved on. Left standing this walks the matcher back "
            "into signals the embedder sweep has not reached (the vector "
            "plane then reads as wired but contributes nothing). Look at the "
            "MATCH RATE first; raising edge_cap only hides it",
            processed_count,
            counters["deferred_signals"],
            signal_cap,
            edge_cap,
        )

    # ------------------------------------------------------------------
    # W-B1/W-B2 — THE BEARING PIPELINE. The one seam between "the matcher
    # decided to write this edge" and "the edge is written".
    #
    # Placed HERE, after the whole matching pass and before any write, for
    # three reasons that are all about correctness rather than tidiness:
    #   * the confirm leg is BATCHED, so it needs the run's full candidate
    #     set, not one signal's slice;
    #   * the gate budget is PER RUN, so applying it inside the per-signal
    #     loop would need the same cross-signal state anyway;
    #   * ``matched_questions`` drives the review_flags leg, and a flag must
    #     only exist for a question some edge ACTUALLY reached. Rebuilding it
    #     from the SURVIVORS is the only correct construction — a gated-out
    #     pair must not flag a downstream product for re-review.
    # No pool connection is held across these calls (the matching block's
    # connection was released above), so a slow 8B never parks a connection.
    #
    # OFF by default: the call below returns the input list untouched and
    # constructs no client at all.
    # ------------------------------------------------------------------
    edge_rows = await run_bearing_pipeline(
        edge_rows,
        deps=deps,
        mode=bearing_gate_mode,
        gate_ref=bearing_gate_ref,
        gate_cap=bearing_gate_cap,
        confirm_cap=bearing_confirm_cap,
        counters=counters,
    )
    for cand in edge_rows:
        qid = str(cand.question_id)
        prev = matched_questions.get(qid)
        if prev is None or cand.signal_as_of > prev:
            matched_questions[qid] = cand.signal_as_of

    # ------------------------------------------------------------------
    # Writes — edges, then flags, then the cursor advance (the watermark
    # moves ONLY after the writes landed, the 0091 ordering discipline).
    # ------------------------------------------------------------------
    async with pool.acquire() as conn:
        for cand in edge_rows:
            tag = await conn.execute(
                _INSERT_EDGE_SQL,
                cand.signal_id,
                cand.signal_as_of,
                cand.question_id,
                cand.question_as_of,
                cand.weight,
                cand.planes,
                MATCHER_VERSION,
                json.dumps(cand.data_payload()),
            )
            if tag.endswith(" 1"):
                counters["edges_written"] += 1
            else:
                counters["edges_deduped"] += 1

        # Review flags — ONLY for matched questions that trace FORWARD to a
        # live product; one open flag per (consumer, question) pair.
        for qid, moved_at in sorted(matched_questions.items()):
            quuid = _uuid_or_none(qid)
            if quuid is None:
                continue
            consumers = await conn.fetch(
                _FORWARD_WALK_SQL,
                quuid,
                FORWARD_WALK_MAX_DEPTH,
                _MAX_CONSUMERS_PER_QUESTION,
            )
            for c in consumers:
                if counters["flags_written"] >= flag_cap:
                    counters["flags_dropped_cap"] += 1
                    continue
                tag = await conn.execute(
                    _INSERT_FLAG_SQL,
                    c["consumer_id"],
                    quuid,
                    moved_at,
                    FLAG_REASON,
                )
                if tag.endswith(" 1"):
                    counters["flags_written"] += 1
                else:
                    counters["flags_deduped"] += 1

        if last_processed is not None:
            await _upsert_watermark(
                conn,
                TRIGGER_CLASS,
                CURSOR_KEY,
                _cursor_state(last_processed["fetched_at"], last_processed["id"]),
                fired=counters["edges_written"] > 0,
            )
        counters["staleness_debt"] = await _staleness_debt(conn)

    if counters["edges_written"] or counters["flags_written"]:
        logger.info(
            "claim_watch.done signals=%d questions=%d/%d edges=%d (+%d dup) "
            "flags=%d (+%d dup) staleness_debt=%d lag_hours=%.1f skipped=%d "
            "held=%d vectors=%d/%d meta_skipped=%d omnibus_capped=%d "
            "url_deduped=%d global_df_sample=%d",
            counters["examined_signals"],
            counters["questions_matchable"],
            counters["questions_scanned"],
            counters["edges_written"],
            counters["edges_deduped"],
            counters["flags_written"],
            counters["flags_deduped"],
            counters["staleness_debt"],
            float(counters["cursor_lag_seconds"]) / 3600.0,
            counters["signals_skipped_ahead"],
            counters["held_for_embedding"],
            counters["signal_vectors_found"],
            counters["examined_signals"],
            counters["skipped_meta_questions"],
            counters["omnibus_capped"],
            counters["signals_url_deduped"],
            counters["global_specificity_sample"],
        )
    return _result(counters)


__all__ = [
    "AGE_FACTOR_FLOOR",
    "CURSOR_KEY",
    "DEFAULT_MATCH_THRESHOLD",
    "DEFAULT_MAX_CURSOR_LAG_SECONDS",
    "DF_UBIQUITY_KNEE",
    "ENTITY_SPECIFICITY_FLOOR",
    "FLAG_REASON",
    "GLOBAL_DF_SATURATION",
    "GLOBAL_DF_UBIQUITY_KNEE",
    "GLOBAL_DF_WINDOW_SIGNALS",
    "HARVEST_MARKER_KEY",
    "MATCHER_VERSION",
    "MAX_QUESTIONS_PER_SIGNAL",
    "MAX_SHARED_ENTITIES_COUNTED",
    "META_QUESTION_CLASSES",
    "MIN_DESK_QUESTIONS_FOR_SPECIFICITY",
    "MIN_SIGNALS_FOR_GLOBAL_SPECIFICITY",
    "SUB_HANDLER_NAME",
    "TRIGGER_CLASS",
    "VECTOR_SIM_FLOOR",
    "W_ENTITY_ADDITIONAL",
    "W_ENTITY_FIRST",
    "W_GEO",
    "W_VECTOR",
    "build_entity_specificity",
    "combined_specificity",
    "cosine_similarity",
    "dedupe_by_canonical_url",
    "entity_component",
    "entity_specificity",
    "fuse_weight",
    "global_entity_specificity",
    "handle",
    "harvest_class",
    "is_meta_question",
    "question_age_factor",
]
