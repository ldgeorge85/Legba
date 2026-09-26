# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Three per-feature ``OptionSpec`` blocks the 09-05 merge wave added to the
X-1 catalog: external grading width (W-9), the ``desk_reference`` sub-handler
(A-1), and ``research_measurement`` (R-D).

Extracted from :mod:`legba.data.analysts.handler_options` for the
module-size gate: that merge wave pushed the catalog to 1,699 lines against
its 1,705 ceiling, flagged as a follow-up extraction (see
``tests/test_module_size_gate.py``). Each of the three blocks below belongs
to exactly one feature landed the same week and has no cross-reference to
the rest of the catalog, which is what makes it a cohesive, self-contained
unit to move.

``handler_options`` imports the three constants below ONE WAY and splices
them into ``HANDLER_OPTIONS`` at the keys they always occupied —
``desk_reference`` and ``research_measurement`` verbatim, and
``standing_auditor``'s tuple as the pre-existing D5 entry (five specs, added
2026-08-29) followed by ``EXTERNAL_GRADING_WIDTH_OPTIONS`` (six specs added
2026-09-05, a seventh — ``window_grace_hours`` — added 2026-09-06, an eighth
— ``window_basis`` — added 2026-09-07) — so
``HANDLER_OPTIONS``, ``known_option_names`` and every
``tests/data_pkg/test_handler_options_x1.py`` reachability check resolve
unchanged: same keys, same specs, same order.

Later per-feature blocks join the same file for the same reason — one feature's
knobs, no cross-reference to the rest of the catalog, spliced back into
``HANDLER_OPTIONS`` at the key they belong to. ``LAYER_DIVERGENCE_OPTIONS``
(Program 6 L2, the layer-divergence unit) is the most recent.

This module imports ``OptionSpec`` and the three terse constructor helpers
(``_pos_int`` / ``_window_hours`` / ``_window_days``) from
``handler_options_base`` — the LEAF module both this file and
``handler_options`` depend on. Earlier this imported those names back from
``handler_options`` itself, which made the two modules mutually dependent: it
worked only because ``handler_options`` always happened to be imported
first, and importing THIS module first in a fresh interpreter raised
``ImportError: cannot import name 'DESK_REFERENCE_OPTIONS' from partially
initialized module`` — order-dependent, not actually safe. Importing from
the leaf instead makes both import orders work unconditionally: neither this
module nor ``handler_options`` imports the other, so there is no cycle left
to be careful about. Do not import ``handler_options`` from here — that
would reintroduce exactly the cycle this split removes.
"""

from __future__ import annotations

import re

from .handler_options_base import (
    OptionSpec,
    _MAX_WINDOW_HOURS,
    _pos_int,
    _window_days,
    _window_hours,
)

__all__ = [
    "CONTRARY_EVIDENCE_OPTIONS",
    "CORRECTNESS_GRADER_OPTIONS",
    "PROGRAM_CATALOG",
    "REFERENCE_BUILDER_OPTIONS",
    "DESK_REFERENCE_OPTIONS",
    "EXTERNAL_GRADING_WIDTH_OPTIONS",
    "LAYER_DIVERGENCE_OPTIONS",
    "NARRATIVE_SPREAD_BLOCK_OPTIONS",
    "RESEARCH_MEASUREMENT_OPTIONS",
    "INQUIRY_KIND_OPTIONS",
    "RELATIONSHIP_REIFIER_OPTIONS",
]


# R-D research measurement. `min_n` is gate G9 (spec §6.3) — the floor below
# which a rate publishes as null with a reason instead of as a number; it is
# settable because regime-1 yield is the single number the operator may want
# to move (F-10) and a code edit is the wrong place for that decision.
RESEARCH_MEASUREMENT_OPTIONS: tuple[OptionSpec, ...] = (
    _window_days("window_days", "Research-signal window each counter reads."),
    _window_days(
        "maturation_days", "Forward window CORROBORATION waits out."
    ),
    _pos_int(
        "corroboration_lookback_hours",
        "How far BEFORE a research signal a non-research row still counts.",
    ),
    _pos_int("min_n", "Gate G9 floor: below this a rate publishes null."),
    _pos_int(
        "weak_entity_overlap",
        "Shared entities the WEAK corroboration arm requires.",
    ),
    _pos_int("row_cap", "Research signals read per run."),
    _pos_int("head_cap", "Desk heads read for the R4 stratification."),
)


# Lane narrative (Program 7 piece 7e) — narrative_coordination's SPREAD BLOCK
# (spread_block.build_spread_block), spliced onto the tail of the
# ANALYST_KIND_OPTIONS["inline_target"] tuple in handler_options.py. Extracted
# straight here rather than added in-line there: this module-size gate ratchet
# is the established precedent for a small, self-contained knob (the same move
# the 09-05 merge wave made for width/desk_reference/research_measurement, per
# this module's own docstring).
NARRATIVE_SPREAD_BLOCK_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "spread_block",
        "bool",
        "Compute + render the deterministic SPREAD BLOCK (near-verbatim "
        "cross-source reuse, distinct-source count, class mix, time spread "
        "in hours) into this unit's slice header, keyed off the block's own "
        "ordinals. See spread_block.py. Absent/false on every other "
        "inline_target unit: byte-identical header.",
    ),
)


# W-9 EXTERNAL GRADING WIDTH (LEGBA_EXTERNAL_GRADING_WIDTH). Every one of
# these bounds a COST that is now hourly rather than daily, so an operator
# can widen or narrow external grading with a descriptor PUT and no code
# edit. They are read by the width tick (``_external_audit_width``), which
# the X-1 reachability sweep follows as a declared delegate off
# ``standing_auditor``. Spliced onto the tail of the pre-existing D5
# ``standing_auditor`` tuple in ``handler_options.HANDLER_OPTIONS`` — see
# that dict's own comment at the ``standing_auditor`` key.
#
# FAT-FINGER CEILING on ``max_claims_per_tick`` (2026-09-20) — deliberately
# NOT the governor. It used to be ``40``: a SECOND copy of the same stale
# ``120 // 3`` arithmetic the queue module hard-coded. Two copies meant the
# starvation could not be fixed by descriptor PUT at all — a widened value
# does not clamp here, it is REJECTED as dead config (dropped from
# ``accepted``, the module default standing in its place), so the operator's
# knob would have looked set and done nothing. The real ceiling is the LIVE
# pack governor, resolved at drain time by
# ``_external_audit_queue.governor_max_claims_per_tick``; this number only
# stops a typo — a stray zero — from being accepted as a cap.
_MAX_CLAIMS_PER_TICK_SCHEMA_CEILING = 2000

EXTERNAL_GRADING_WIDTH_OPTIONS: tuple[OptionSpec, ...] = (
    _pos_int(
        "max_claims_per_tick",
        "Claims drained per hourly tick. CLAMPED IN CODE to the web_access "
        "pack's LIVE governor (max_invocations_per_hour divided by the 3 "
        "egress calls one claim can spend), resolved off the binding rather "
        "than copied: a descriptor may lower this and can never raise it past "
        "the governor, because exceeding it does not slow a tick, it BLOCKS "
        "one.",
        maximum=_MAX_CLAIMS_PER_TICK_SCHEMA_CEILING,
    ),
    _pos_int(
        "max_claims_per_day",
        "Hard daily claim ceiling. On exhaustion the loop degrades to a "
        "hash-gated SAMPLED mode and stamps the fraction on the "
        "heartbeat, every ledger row and every published number — it "
        "never fabricates and never silently shrinks n.",
    ),
    _pos_int(
        "max_serp_per_day",
        "Hard daily outbound-search ceiling, counted across the primary "
        "query, the one reformulation and the span-verification fetch.",
    ),
    _pos_int(
        "max_queue_depth",
        "Safety valve on the durable claim queue. An overflowing day "
        "drops its LOWEST-priority claims (never its newest) and the "
        "drop is counted on the heartbeat.",
    ),
    _pos_int(
        "max_write_attempts",
        "A claim whose ledger write fails this many times running is "
        "dead-lettered rather than re-drained forever.",
    ),
    OptionSpec(
        "serp_provider_order", "str_list",
        "The SERP ladder, in the order rungs are tried. Today it has one "
        "rung ('searxng') and that is a statement about what exists: "
        "SearXNG cannot verify its own emptiness at any volume, so "
        "16-41% of every read is ungradable until a rung that can is "
        "appended here.",
    ),
    # 2026-09-06 follow-up — G-3's grace, made operator-settable. Mirrors
    # LEGBA_EXTERNAL_AUDIT_WINDOW_GRACE_HOURS (env supplies the base, this
    # option wins — the house `_coerce` idiom `_coverage_floor_scan.
    # CoverageFloorConfig.from_options` uses). Read by
    # `_external_audit_width._window_grace_hours`, which the X-1 reachability
    # sweep follows via the `standing_auditor` -> `_external_audit_width`
    # delegate declared above this tuple's splice point.
    OptionSpec(
        "window_grace_hours", "float",
        "G-3's admissible-window grace: a decisive source published up to "
        "this many hours BEFORE the read's own evidence_window.oldest is "
        "still admitted (the AFTER bound never moves). Default 0 == "
        "today's exact rule, byte-identical. FLIPPING THIS ABOVE 0 IN "
        "PRODUCTION IS AN INSTRUMENT CHANGE, not a retune — see "
        "_external_audit_width.py's WINDOW_GRACE_HOURS_ENV banner for the "
        "EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH stamp-lineage entry the "
        "flip must add.",
        minimum=0, maximum=_MAX_WINDOW_HOURS,
    ),
    # 2026-09-07 — G-3's window BASIS. Mirrors
    # LEGBA_EXTERNAL_AUDIT_WINDOW_BASIS (env supplies the base, this option
    # wins), read by `_external_audit_width._window_basis` off the same
    # `standing_auditor` -> `_external_audit_width` delegate. `choices` is
    # what makes a typo a REFUSAL at registration instead of a silent
    # not-heads that would stamp rows 2026-09-07/1 for no reason.
    OptionSpec(
        "window_basis", "str",
        "G-3's admissible-window basis. 'heads' (default, byte-identical to "
        "today): oldest == the oldest consumed head's produced_at, as the "
        "read stamped it. 'evidence': oldest == the oldest signals.fetched_at "
        "under those heads' own derived_from lineage — the read's real "
        "grounding window, which measured ~13 days against a 3.19-hour heads "
        "spread on the 2026-09-06 world read. The AFTER bound never moves "
        "under either basis. THIS IS AN INSTRUMENT CHANGE, not a retune: "
        "'evidence' stamps rows EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH "
        "(2026-09-07/1) instead of the heads stamp (2026-09-06/1).",
        choices=("heads", "evidence"),
    ),
)


# A-1 ATTENTION MEASUREMENT — every knob here bounds a COST: paid
# out-of-plane calls, external searches, and the size of the graded sample.
# An operator widens the instrument from v1 (the 5-target read set, 38
# pairs/day) to v2 (a rotation over all 238) with a descriptor PUT and no
# code edit — which is the whole reason the population is an option and not
# a constant. `reference_targets` is deliberately a plain str_list rather
# than a tier read: design F-5 measured the per-target `analyst.cadence`
# field to be a vestige the bounded units do not read.
DESK_REFERENCE_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "reference_targets",
        "str_list",
        "Target descriptor ids referenced daily (default: the read set).",
    ),
    _pos_int(
        "max_pairs_per_run",
        "(target, unit) pairs referenced per sweep — one paid out-of-plane "
        "call and one external search each.",
    ),
    _pos_int(
        "max_items_per_reference",
        "Items the reference model may return per (target, unit).",
    ),
    _pos_int("search_limit", "Results requested per web_search call."),
    _window_hours(
        "window_hours", "The window one reference covers (24h by default)."
    ),
    _window_days(
        "census_days",
        "Window the (target, unit) production census reads.",
    ),
)


# G1 CORRECTNESS GRADER (LEDGER_RESET_2026-09-16 §3, Program 2). Every knob
# here bounds a COST — LLM calls, DB reads, the size of the graded sample — so
# an operator widens or narrows the instrument with a PUT and no code edit.
#
# THE ONE KNOB THAT IS NOT HERE is the spend ceiling. It is an ENV var
# (LEGBA_GRADER_DAILY_CEILING_USD, default $0) and deliberately not a
# descriptor option: a descriptor PUT is an API call any holder of the registry
# token can make, and raising the amount of the operator's money this job may
# spend is not that kind of decision. It takes an env change and a recreate,
# which is a deploy step with a human at the other end.
#: An ISO-8601 instant with an explicit offset. Anchored and bounded so a
#: mistyped stamp is refused at the descriptor/invocation boundary rather than
#: silently falling back to now() inside the handler.
_ISO_INSTANT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-]\d{2}:\d{2})$"
)


# R2 REFERENCE BUILDER (LEDGER_RESET_2026-09-16 §3, Program 2). Every knob
# below bounds either the SIZE of one build (tool calls, page text, the
# development target) or the SHAPE of the schedule (cadence, window, how many
# targets a tick may take). None of them can move a FENCE: the fetched-URL
# manifest, the date gate, the tier allowlist and the span check are not
# settable from a descriptor and never will be, because a descriptor PUT is an
# API call any holder of the registry token can make, and lowering the bar a
# reference must clear is not that kind of decision. `thin_below` is the one
# boundary case and it is settable UPWARD as well as down — it names what
# "thin" means on the row, which a reader can see, rather than silently
# admitting evidence that would not otherwise qualify.
REFERENCE_BUILDER_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "as_of", "str",
        "The ONE stamped instant the built window ENDS at, ISO-8601 with an "
        "explicit offset. Absent (the shipped default) means now. This is the "
        "SAME key the correctness grader reads for the same concept, so an "
        "operator replaying a fortnight passes one value to both jobs and the "
        "reference and the grading of it pin to one instant. `t0` is accepted "
        "as an alias because that is what the reference JSON header calls it.",
        pattern=_ISO_INSTANT_RE,
    ),
    OptionSpec(
        "t0", "str",
        "ALIAS for `as_of`, accepted because that is what the reference JSON "
        "header and the R1/R3 harnesses call the same instant — a reader "
        "holding a ref_*.json should not have to translate to replay it. "
        "`as_of` wins when both are given. Declared rather than silently "
        "accepted: an undeclared key is REJECTED by the runtime's option merge, "
        "so an alias that is not in this catalog is an alias that does not work.",
        pattern=_ISO_INSTANT_RE,
    ),
    OptionSpec(
        "reference_targets", "str_list",
        "Country targets to build for. EMPTY (the shipped default) means the "
        "SINGLE most overdue target on the live roster — which is the whole "
        "stagger: one reference at a time, oldest first, no stagger table. "
        "Naming targets here bypasses the cadence and builds exactly those, "
        "which is what the on-demand script does.",
    ),
    _window_days(
        "window_days",
        "The window one reference covers. Equal to `cadence_days` by default "
        "and deliberately: consecutive references then TILE the timeline, so "
        "every as-of stamp the grader asks about falls inside exactly one "
        "reference's window rather than in a gap or an overlap.",
    ),
    _window_days(
        "cadence_days",
        "How stale a target's newest reference must be before it is due "
        "again. Raising it stretches the roster's cycle and lowers ai1 load; "
        "lowering it below `window_days` makes consecutive references overlap, "
        "which the grader resolves by newest build wins.",
    ),
    _pos_int(
        "max_targets_per_run",
        "Targets one tick may build. SHIPPED AT 1, and that is the stagger: "
        "R1 measured one reference at ~1-1.7M core-plane prompt tokens and ~8 "
        "minutes of ai1, so a 33-target roster is affordable only if at most "
        "one runs at a time. Raising this puts several builds on the box at "
        "once.",
    ),
    _pos_int(
        "tool_call_cap",
        "Search + fetch calls one build may spend. R1's measured budget is 80, "
        "which produced 19 developments across 8 dimensions at 78.9% span "
        "verification. This is the single largest lever on how long a build "
        "occupies the core plane.",
    ),
    _pos_int(
        "page_chars_to_model",
        "Characters of each fetched page shown to the model. R1 measured the "
        "token shape at 2.57M prompt against 73k completion: a tool loop over "
        "fetched pages is almost entirely prompt-side, so this is the second "
        "largest lever on core-plane time. Lowering it costs recall of long "
        "articles; raising it costs time on every page.",
    ),
    _pos_int(
        "min_developments",
        "Developments the model must have NOTED before it may commit. Below "
        "this the loop pushes back and tells it which dimensions are thin. "
        "The target the instruction states is this number to 35.",
    ),
    _pos_int(
        "thin_below",
        "A dimension with fewer than this many SURVIVING verified "
        "developments is written to `thin_dimensions` — the column the "
        "correctness grader reads. Two is the shipped value and the one G1's "
        "loader uses; a dimension resting on a single development is an "
        "anecdote, and every claim graded against it is structurally likelier "
        "to read `silent`.",
    ),
    _window_days(
        "roster_window_days",
        "How recently a dimension desk must have produced for its target to "
        "count as on the live roster. The roster is DERIVED from what the "
        "fleet actually reads rather than listed anywhere, so a country that "
        "joins the read set joins this lane with no edit.",
    ),
    OptionSpec(
        "reference_dry_run", "bool",
        "Build and FENCE the reference but do NOT write it to "
        "`unit_references`; the complete reference travels on the receipt's "
        "`data.per_target[].reference` instead. For proving the path, or for "
        "reviewing what a target would produce before it counts against a "
        "published number. Costs the same core-plane time as a real build.",
    ),
    # -- D3/D4, the two walls (2026-09-17) --------------------------------
    # Both bound what ONE build may spend, and both exist because the 00:43Z
    # autonomous tick spent 927.9 s and 4.5 M tokens and committed nothing:
    # the loop had no exit toward a commit except the tool cap running out,
    # and 4.5 M tokens exhausted the descriptor's own budget_tokens_per_day,
    # which stamped a one-hour global cooldown on the actor. A build is a
    # bounded thing or it is a liability to the plane that runs it. At ~70% of
    # either the loop stops offering tools and takes what the model has.
    _pos_int(
        "build_max_seconds",
        "HARD wall-clock cap on one build, seconds. Past it the loop stops, "
        "committed or not, and the receipt says `no_commit`. 420 s is 7 "
        "minutes: above the 294 s the one good live build needed, and below "
        "BOTH this descriptor's `cadence.cooldown_seconds` and its hourly tick "
        "period, so a build can never still be holding the actor's turn when "
        "the next reminder fires. Env override "
        "LEGBA_REFERENCE_BUILD_MAX_SECONDS; this knob wins over both.",
        maximum=3600,
    ),
    _pos_int(
        "build_max_tokens",
        "HARD per-build token ceiling. THE lever on the failure that actually "
        "stopped the lane: one build may not eat the analyst's whole day "
        "bucket. At 1.2 M against `budget_tokens_per_day: 4000000` three "
        "builds fit a day with headroom and no single build can exhaust it — "
        "an exhausted bucket is a BUDGET_THROTTLED cooldown, not a slow tick. "
        "Env override LEGBA_REFERENCE_BUILD_MAX_TOKENS; this knob wins.",
    ),
    # -- R2-FIX(2), the retry fence (2026-09-17) --------------------------
    # The ordering key became "last ATTEMPT" rather than "last success"
    # because a failed build writes no unit_references row, so a target that
    # CANNOT be built stayed permanently the most overdue one. Argentina held
    # the front of a 32-country queue through two whole builds. This knob is
    # the second half of that fix: a failure buys the target a rest, so the
    # hour goes to the next target instead.
    OptionSpec(
        "retry_backoff_hours", "int",
        "Hours a target stays INELIGIBLE after a build that committed nothing "
        "(`no_commit`) or had everything fenced out (`all_rejected`). It is "
        "never dropped and never hidden: it stays in the receipt's due queue "
        "carrying reason `retry_backoff` and the instant it returns. 24 h "
        "against a 7-day cadence means a chronically failing country costs the "
        "lane one build a day rather than one an hour. 0 DISABLES the fence, "
        "which is the only way to measure what it is doing. Env override "
        "LEGBA_REFERENCE_RETRY_BACKOFF_HOURS, and the ENV WINS over this knob "
        "— the inverse of the two build walls, deliberately: this is the lever "
        "an operator reaches for while the lane is stuck, when a descriptor "
        "PUT plus a re-register is the ceremony they cannot afford.",
        minimum=0, maximum=_MAX_WINDOW_HOURS,
    ),
)

CORRECTNESS_GRADER_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "as_of", "str",
        "The ONE stamped instant the sweep measures at, ISO-8601 with an "
        "explicit offset. Absent (the shipped default) means now. Set it for a "
        "REPLAY — re-grading a past stamp — which the idempotence key "
        "(analyst_id, target_id, head_id, reference_id, rubric_sha) makes a "
        "no-op if that measurement already exists, so a replay costs nothing "
        "the second time.",
        pattern=_ISO_INSTANT_RE,
    ),
    OptionSpec(
        "grader_targets", "str_list",
        "Country targets to grade. EMPTY (the shipped default) means every "
        "target carrying a unit_references row whose window contains the "
        "as-of stamp — so the sweep widens on its own as the reference "
        "builder (track R2) lands references, and grades nothing at all while "
        "the table is empty. Naming targets here bounds it to those.",
    ),
    _pos_int(
        "max_targets_per_run",
        "Targets graded per tick. The population is references, not the "
        "roster: a target with no reference costs one indexed SELECT.",
    ),
    _pos_int(
        "max_claims_per_unit",
        "Claims graded per bounded unit. When it bites, the claims kept are "
        "the first by id (a content-derived order, never the read's) and the "
        "receipt records how many were dropped — a share is always published "
        "beside the n it actually rests on.",
    ),
    _pos_int(
        "max_claims_per_run",
        "Hard ceiling on claims graded in one tick, across every target.",
    ),
    _window_days(
        "head_window_days",
        "How far back a desk head may have been produced and still count as "
        "the current read of the window the reference covers. Carried from "
        "R4's WINDOW_DAYS (14); moving it changes which read is measured.",
    ),
    OptionSpec(
        "reference_grace_days", "int",
        "How long past its window_end a reference stays the CURRENT reference "
        "for its target (default 7). A reference's window ends at its own T0 "
        "and every read it grades lands after that, so a grace of 0 is strict "
        "containment and makes the nightly sweep ungradable by construction — "
        "the defect the first live run found. LEGBA_GRADER_REFERENCE_GRACE_"
        "DAYS OVERRIDES this: how long a published number may rest on an "
        "ageing reference is a measurement-integrity decision, and the env is "
        "a deploy step with a human at the other end where a descriptor PUT "
        "is an API call. The age carried is written on every row "
        "(reference_age_days) and printed on the receipt; past the grace the "
        "target is named `reference_stale`, not `no_reference`.",
        # ZERO IS LEGAL and means strict containment — the pre-fix behaviour,
        # kept reachable so an operator can pin the old semantics without a
        # code edit. `_window_days` would floor it at 1, which is why this
        # spec is written out rather than borrowed.
        minimum=0, maximum=365,
    ),
)


# ---------------------------------------------------------------------------
# Program 6 L2 — layer_divergence (the divergence-baseline unit)
# ---------------------------------------------------------------------------
#
# The instrument turns a loaded layer map (`source_layers` / `desk_apertures`,
# migration 0214) into per-layer daily counts, a rolling baseline per layer
# pair, and a finding on a two-day move. The knobs below split cleanly in
# three, and the split is worth stating because it is what an operator tuning
# this in a proof round needs to understand:
#
#   * THE STATISTIC — `window_days`, `baseline_days`, `z_threshold`,
#     `mad_floor`. Moving any of these changes what the numbers MEAN, so a
#     change here belongs beside a `METHOD_VERSION` bump.
#   * THE HONESTY KNOBS — `thin_min_per_day`, `confidence_full_n`. These never
#     change whether a finding fires; they change what it CONFESSES about the
#     counts it rests on.
#   * THE BOUNDS — `max_signal_rows`, `max_fold_rows`, `max_citations_per_layer`
#     and the target list. Cost control only.
#
# Two rules are deliberately NOT here. The two-day consecutive rule
# (`_CONSECUTIVE_DAYS`) is the measure's definition of a real move, and a knob
# that could set it to 1 would silently turn the instrument into the
# single-day noise detector the program exists to avoid. The log-ratio's
# continuity constant (`_ALPHA`) defines the statistic itself. Both are
# in-source constants covered by METHOD_VERSION.

#: ``YYYY-MM-DD`` — a DATE, not an instant. The unit measures whole UTC days,
#: so an instant would imply a precision the buckets do not have.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: ``<target_id>`` or ``<target_id>:<CC>``. The optional country half is the
#: escape hatch for a desk whose aperture has not been loaded — see
#: ``_layer_divergence_reads.resolve_desks``.
_DIVERGENCE_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+(?::[A-Za-z]{2})?$")

LAYER_DIVERGENCE_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "divergence_targets", "str_list",
        "Desks to measure, as '<target_id>' or '<target_id>:<CC>'. EMPTY (the "
        "shipped default) means every desk whose loaded aperture shares a "
        "map_version with a loaded layer table — so the sweep widens on its "
        "own as maps are curated and measures nothing at all while the tables "
        "are empty. The ':<CC>' form is the escape hatch for a desk whose "
        "aperture half has not been loaded yet: the layer table is what the "
        "counts need, and the missing aperture then reads as six UNDECLARED "
        "layers, which excludes every pair and says so on the receipt.",
        pattern=_DIVERGENCE_TARGET_RE,
    ),
    _window_days(
        "window_days",
        "How many UTC days of signals the run counts (default 28). The "
        "EVALUABLE days are window_days - baseline_days, because a day needs a "
        "baseline built from the days before it — 28 and 14 give the proof "
        "round its two weeks.",
    ),
    _window_days(
        "baseline_days",
        "The rolling baseline width in days (default 14): the trailing "
        "non-blank days whose median and MAD a day's log-ratio is measured "
        "against. Widening it makes the instrument slower to accept a new "
        "normal; narrowing it lets a sustained move re-base itself into "
        "invisibility.",
    ),
    OptionSpec(
        "z_threshold", "float",
        "How many MAD-scaled deviations a day's log-ratio must sit from its "
        "baseline to count as a move (default 2.0). Required on TWO "
        "consecutive days with the same sign before anything fires — that "
        "second condition is not settable.",
        minimum=0.5, maximum=20.0,
    ),
    OptionSpec(
        "mad_floor", "float",
        "Floor on the MAD-derived scale, in log2 units (default 0.2, about a "
        "15% ratio move). Without it a pair whose ratio sat at EXACTLY one "
        "value across the baseline has MAD 0 and fires on any move at all, "
        "which is the noise the two-day rule exists to refuse. ZERO IS LEGAL "
        "and means no floor — every flat pair becomes infinitely sensitive, "
        "which is occasionally what a diagnostic run wants and never what a "
        "scheduled one does.",
        minimum=0.0, maximum=10.0,
    ),
    OptionSpec(
        "thin_min_per_day", "int",
        "Below this many FOLDED items on the day, a layer is 'thin' and every "
        "finding resting on it says so (default 5). It does not suppress the "
        "finding — a regime going silent is exactly a thin official layer, and "
        "hiding that would delete the measurement. ZERO IS LEGAL and means "
        "nothing is ever marked thin.",
        minimum=0, maximum=10_000,
    ),
    _pos_int(
        "confidence_full_n",
        "Folded items on the SMALLER side of a pair at which a divergence "
        "earns confidence 1.0 (default 20); below it confidence scales down "
        "linearly to a 0.1 floor. The thinner layer governs, because a ratio "
        "is only as well-evidenced as its smaller half.",
    ),
    OptionSpec(
        "day_basis", "str",
        "Which timestamp column buckets a UTC day. 'fetched_at' (the shipped "
        "default) is the INDEXED column; 'created_at' carries no index at all, "
        "so bucketing by it turns a bounded scan into a sequential one over "
        "the whole signals table — set it only for a diagnostic run you are "
        "watching. CHOICE-LOCKED because the value is interpolated into the "
        "scan SQL (a column name cannot be a bind parameter).",
        choices=("fetched_at", "created_at"),
    ),
    OptionSpec(
        "as_of", "str",
        "The ONE UTC day the run measures at, YYYY-MM-DD. Absent (the shipped "
        "default) means today. Set it for a REPLAY of a past day: the whole "
        "run is a pure function of its window, so a replay over an unchanged "
        "window reproduces the same finding body byte for byte — which is what "
        "makes the proof round's two weeks re-derivable rather than "
        "re-remembered.",
        pattern=_ISO_DATE_RE,
    ),
    _pos_int(
        "max_signal_rows",
        "Ceiling on signal rows pulled per desk per run (default 20,000). The "
        "read probes one row past it, so a truncated window is REPORTED on the "
        "receipt (rows_truncated) rather than silently counted as a complete "
        "one.",
        maximum=1_000_000,
    ),
    _pos_int(
        "max_fold_rows",
        "Bound on the O(n^2) declared-wire-pair walk inside ONE (layer, day) "
        "bucket (default 60). Rows past it still COUNT, they simply do not "
        "fold — which over-counts a layer and can never under-count it, the "
        "direction every guard in source_independence resolves toward.",
        maximum=5_000,
    ),
    _pos_int(
        "max_citations_per_layer",
        "How many of the day's top folded items each side of a fired pair "
        "cites (default 3). 'Top' is the fold's own order — newest distinct "
        "dispatch first — because this instrument has no relevance score and "
        "inventing one would be a claim it cannot back.",
        maximum=50,
    ),
)


# Program 7a CONTRARY-EVIDENCE PASS (`contrary_evidence_pass`). Every knob here
# bounds a COST — claims contended, searches issued, pages fetched — or the
# SHELF LIFE of a record, so an operator can widen, narrow or quieten the pass
# with a descriptor PUT and no code edit.
#
# `paid_rung` IS THE ONE THAT IS NOT A CAP, and it is the only knob in this
# tuple that can cause a charge. It defaults to OFF and the handler additionally
# TRUNCATES `serp_provider_order` to rung 0 while it is off, so a PUT that adds
# a metered rung to the ladder still cannot spend anything on its own. With it
# on, a metered query is still refused unless the web_access pack declares
# `max_cost_usd_per_day` — the platform's standing rule that a paid path ships
# with a cost ceiling — and every refusal is counted on the heartbeat rather
# than folded into a quiet zero.
#
# `serp_provider_order` is NOT re-documented here: it is the SAME ladder the
# standing auditor declares, read through the same `_external_audit_queue.
# serp_provider_order` normaliser, and a second description of one ladder is how
# two surfaces come to disagree about which rung is free.
CONTRARY_EVIDENCE_OPTIONS: tuple[OptionSpec, ...] = (
    _pos_int(
        "claims_per_run",
        "Material claims contended per run. The cap binds AFTER the audit's "
        "own severity-then-lead priority sort, so it drops the least material "
        "claims rather than the last ones by name.",
        maximum=1000,
    ),
    _pos_int(
        "max_refs_per_claim",
        "Pages FETCHED per claim through the auditor's fences. One search plus "
        "this many fetches is the whole per-claim egress budget; a search "
        "result that is never fetched never becomes a ref.",
        maximum=10,
    ),
    _pos_int(
        "search_limit",
        "Results requested per counter-query web_search call.",
        maximum=50,
    ),
    _window_hours(
        "contention_ttl_hours",
        "Shelf life of a contention record: how long after `as_of` the "
        "composition tension rule still admits it and the route still calls it "
        "live. The web moves; a record older than this is history, not a live "
        "disagreement.",
    ),
    _pos_int(
        "read_fetch_cap",
        "Safety valve on the top-layer read pre-sort. Not a window — reaching "
        "it truncates the OLDEST reads inside the window, and the truncation is "
        "counted on the receipt rather than being silent.",
        maximum=2000,
    ),
    OptionSpec(
        "paid_rung", "bool",
        "Allow ONE metered escalation for a claim whose free-rung search "
        "answered EMPTY. Default false, and false additionally truncates the "
        "ladder to rung 0 in code. A metered query is still refused without a "
        "declared daily cost cap on the web_access pack, and every escalation "
        "and every refusal is counted on the heartbeat.",
    ),
    OptionSpec(
        "serp_provider_order", "str_list",
        "The SERP ladder, in the order rungs are tried — the same ladder the "
        "standing auditor declares. Rung 0 is free and carries every "
        "counter-query; rungs after it are metered and unreachable while "
        "`paid_rung` is false.",
    ),
)

# ---------------------------------------------------------------------------
# The per-program splice table
# ---------------------------------------------------------------------------
#
# Every block above whose catalog entry is a WHOLE sub-handler (rather than a
# tail spliced onto an in-file tuple, as W-9's is onto `standing_auditor`'s)
# lives here, keyed exactly as it is keyed in `HANDLER_OPTIONS`. The parent
# splices the mapping in ONE line — `**_PROGRAM_CATALOG` — which is the
# `**_EVENTS_CATALOG` idiom `handler_options_events` already established.
#
# WHY THIS EXISTS. The parent module is pinned by `tests/test_module_size_gate
# .py`, and before this table each new program block cost it three to five
# lines of import alias, splice and comment — so the capped file grew a little
# every time a feature landed, which is exactly the regrowth the gate was
# written to stop. With the table, a new program block costs it ZERO lines: it
# is declared beside its own OptionSpecs and added to the dict below. Same
# keys, same specs, same `known_option_names` answers; only the line they are
# written on moved.

#: R-D (`research_measurement`) — the research program's counters. Every knob
#: bounds a COST or a floor on an honest-null; none can move a measured rate.
#:
#: A-1 ATTENTION MEASUREMENT (`desk_reference`) and G1 CORRECTNESS MEASUREMENT
#: (`correctness_grader`) are its two siblings. The grader's SPEND CEILING is
#: deliberately NOT among its knobs — it is an env var, not a descriptor PUT,
#: because how much a measurement may cost is a deploy decision with a human
#: at the other end.
#:
#: R2 (`reference_builder`) — the reference unit. Knobs bound the SIZE of a
#: build and the SHAPE of the schedule; no fence is settable from here.
#:
#: Program 6 L2 (`layer_divergence`) — the divergence-baseline unit. The
#: statistic / honesty / bounds split, and the two rules that are deliberately
#: NOT knobs, are documented on the block itself above.
#:
#: Program 7a (`contrary_evidence_pass`) — the contrary-evidence unit. Caps, a
#: record shelf life, and the one knob that can cost money (`paid_rung`, OFF by
#: default and fenced twice). What is deliberately NOT a knob: the stance
#: vocabulary, the paraphrase gate, and the rule that only a polarity-derived
#: contradiction reaches a composition. Those are the instrument.
PROGRAM_CATALOG: dict[str, tuple[OptionSpec, ...]] = {
    "research_measurement": RESEARCH_MEASUREMENT_OPTIONS,
    "desk_reference": DESK_REFERENCE_OPTIONS,
    "correctness_grader": CORRECTNESS_GRADER_OPTIONS,
    "reference_builder": REFERENCE_BUILDER_OPTIONS,
    "layer_divergence": LAYER_DIVERGENCE_OPTIONS,
    "contrary_evidence_pass": CONTRARY_EVIDENCE_OPTIONS,
}


# Program 5 (planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §2) — the `inquiry`
# analyst KIND's three knobs. They ride this module rather than the catalog for
# the module-size-gate reason its docstring gives: `handler_options.py` sat at
# 1,660 against a 1,660 ceiling when this landed, and the ratchet is honoured by
# splitting, never by raising the number.
#
# These are KIND knobs (ANALYST_KIND_OPTIONS), not sub-handler knobs: they are
# read by `legba.data.analysts.inquiry.run_method` itself — `brief` and
# `target_scope` at PLAN through `resolve_brief` / `resolve_target_scope`,
# `pre_pass_module` at PLAN through `resolve_pre_pass_block`. The whole point of
# putting the BRIEF here is that it is then a registry row: an operator re-aims a
# standing investigation with `PUT /api/v1/descriptors/analyst/{id}` — no code
# edit, no image rebuild, and the change carries the registry's own versioning,
# content hash and audit chain.

#: The design's 2,000-character brief ceiling, expressed where the catalog can
#: enforce it. `OptionSpec` has no length field (it carries TYPE and RANGE, never
#: a default), so the bound rides the pattern guard — DOTALL, because a brief is
#: prose and routinely spans lines.
_BRIEF_PATTERN = re.compile(r"(?s)\A.{1,2000}\Z")

#: One target id in `target_scope` — the registry's descriptor-id alphabet
#: (country_watch_ua, country_g20_de, lane_black_sea, …).
_DESCRIPTOR_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: A `module:function` reference — the COLON form, refused as a dotted path on
#: purpose: a dotted path is ambiguous between a module and an attribute, and
#: that ambiguity is what lets a typo resolve to something plausible.
_MODULE_ATTR_PATTERN = re.compile(
    r"^[A-Za-z_][\w.]{0,127}:[A-Za-z_]\w{0,63}$"
)

INQUIRY_KIND_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "brief",
        "str",
        "WHAT THIS INQUIRY IS LOOKING INTO — free text, <= 2,000 characters, "
        "rendered into the PLAN prompt VERBATIM as the operator wrote it (the "
        "copy an audit points at as 'the mandate this run actually saw'). It "
        "is the whole of the inquiry's mandate: the persona is explicit that "
        "wandering off the brief is not thoroughness. ABSENT is a real state, "
        "not an error — the run says so in its entry and confines itself to "
        "following its open ledger rows rather than inventing a mandate.",
        pattern=_BRIEF_PATTERN,
    ),
    OptionSpec(
        "target_scope",
        "str_list",
        "Target ids (e.g. country_watch_ua) this inquiry is ACCOUNTABLE for, "
        "rendered into the prompt as context. Deliberately NOT a query filter: "
        "the inquiry reads the whole substrate through its granted packs, "
        "because fencing its READS to a scope would make 'the record is silent "
        "outside my desks' unfalsifiable. Absent => no scope line.",
        pattern=_DESCRIPTOR_ID_PATTERN,
    ),
    OptionSpec(
        "pre_pass_module",
        "str",
        "A `module:function` reference (the SAME colon form method.prompt_"
        "module uses) to a deterministic PRE-PASS, called as "
        "`pre_pass_block(options, deps) -> str | None` at PLAN. A non-empty "
        "return is rendered ABOVE the ledger state as a code-computed block, "
        "so the model narrates numbers it was HANDED rather than numbers it "
        "derived (the 7e spread-block pattern). This is the hook the "
        "crossroads mandate's four detectors (patterns / drifts / "
        "contradictions / silences) arrive through. Degrade-not-drop at every "
        "step — an unresolvable reference or a raising callable renders "
        "nothing and never fails the run. Absent => no import and no call: "
        "byte-identical to a descriptor written before the hook existed.",
        pattern=_MODULE_ATTR_PATTERN,
    ),
)


# K-G2 — the relationship_reifier KIND's throughput and quality dials, EXTRACTED
# verbatim from the `ANALYST_KIND_OPTIONS["relationship_reifier"]` tuple in
# handler_options.py to make room for INQUIRY_KIND_OPTIONS above without touching
# that module's 1,660-line ceiling (the gate is honoured by splitting, never by
# raising the number). Same specs, same order, same key — `known_kind_option_
# names("relationship_reifier")` and every X-1 reachability check resolve
# unchanged.
#
# THROUGHPUT is max_candidates x batch_size; QUALITY is qualification_bar x
# min_independent_sources. They are separate levers on purpose — the bar controls
# WHICH edges may enter the graph, the cap controls HOW MANY get typed, and the
# bake-off is explicit that the bar is not a yield optimiser and must not be sold
# as one (docs/TYPING_BAKEOFF_2026-08-03.md §6.5).
RELATIONSHIP_REIFIER_OPTIONS: tuple[OptionSpec, ...] = (
    _pos_int(
        "max_candidates",
        "Candidates typed per run (the cadence is twice daily, so the daily "
        "figure is 2x this). Bounds the per-run LLM spend regardless of how "
        "deep the qualifying queue is. See the arithmetic in "
        "descriptors/analyst_relationship_reifier.yaml.",
        maximum=5000,
    ),
    _pos_int(
        "batch_size",
        "Candidates per LLM typing call. 12 is MEASURED (17/17 clean calls "
        "over 200 candidates, zero truncation) and cuts prompt tokens per "
        "candidate from 1,462 to 297. 1 restores the one-call-per-candidate "
        "shape. Above 24 is unevidenced and showed a possible judgement "
        "shift.",
        maximum=40,
    ),
    OptionSpec(
        "qualification_bar",
        "float",
        "Weighted qualification score a candidate must clear to earn a "
        "typing call. 0.42 is the recommended setting (~12,000 qualifying "
        "of ~176,000 pending). LOWERING it widens the queue but can never "
        "re-admit single-sourced candidates — min_independent_sources is a "
        "separate hard floor for exactly that reason.",
        minimum=0.0,
        maximum=1.0,
    ),
    _pos_int(
        "min_independent_sources",
        "Hard floor on distinct INDEPENDENT sources behind a candidate, "
        "counted after collapsing syndicated content. Not expressible as a "
        "weight: a single-sourced pair with huge salience would otherwise "
        "buy its way in. 92.1% of the live pending pool fails this floor, "
        "and that is the sludge the graph exists to exclude.",
        maximum=10,
    ),
)
