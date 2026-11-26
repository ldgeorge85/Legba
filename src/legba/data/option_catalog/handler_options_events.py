# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3 option specs — P1 event handlers and the P4b graph projector.

Split from ``handler_options.py`` only for the module-size gate; the catalog
still owns these entries and imports this leaf under private aliases, matching
the ``handler_options_programs.py`` split.
"""

from __future__ import annotations

from .handler_options_base import (
    OptionSpec,
    _MAX_WINDOW_HOURS,
    _pos_int,
    _window_days,
    _window_hours,
)
import re


#: Same conservative identifier alphabet ``handler_options`` uses for a
#: collection-name option; kept local so this leaf never imports its catalog.
_EVENT_IDENT_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


EVENT_CLUSTERING_OPTIONS = (
    _window_hours("lookback_hours", "Canonical signal slice clustered per run."),
    _pos_int("max_signals", "Signals examined per run."),
    _pos_int("max_open_events", "Existing event identities considered per run."),
    OptionSpec(
        "match_threshold",
        "float",
        "Four-feature event-match threshold.",
        minimum=0.0,
        maximum=1.0,
    ),
    OptionSpec(
        "embedding_cosine_min",
        "float",
        "Qdrant cosine threshold for the separately measured embedding gate.",
        minimum=0.0,
        maximum=1.0,
    ),
    OptionSpec(
        "qdrant_collection",
        "str",
        "Vector collection queried for the embedding gate.",
        pattern=_EVENT_IDENT_PATTERN,
    ),
    _window_days("tower_window_days", "Bounded tower-candidate window."),
    OptionSpec(
        "tower_floor",
        "float",
        "Effective-confidence floor for tower findings.",
        minimum=0.0,
        maximum=1.0,
    ),
    _pos_int("max_tower_candidates", "Tower candidates examined per run."),
    OptionSpec(
        "include_tower", "bool", "Whether the bounded tower-candidate leg runs."
    ),
    _pos_int("max_lifecycle_events", "Open events lifecycle-evaluated per run."),
    OptionSpec(
        "pass_budget_seconds",
        "float",
        "Per-tick wall-clock ceiling, mirroring "
        "LEGBA_EVENT_CLUSTERING_PASS_BUDGET_SECONDS; 0 disables.",
        minimum=0.0,
    ),
    # --- P1c: where the turn's seconds go ---------------------------------
    OptionSpec(
        "pair_block_window_hours",
        "float",
        "Pair BLOCKING window: a pair with no shared entity, no shared fact "
        "subject and no embedding cosine is scored only when the two signals "
        "sit this many hours apart or less. 0 leaves entity/fact/embedding as "
        "the only admitting keys. At the measured live density (300 signals "
        "span under 3 h) the 6 h default admits every pair — lower it to "
        "about 1 h to hold a 300-signal pass inside the turn budget.",
        minimum=0.0,
        maximum=_MAX_WINDOW_HOURS,
    ),
    OptionSpec(
        "tower_budget_share",
        "float",
        "Share of the pass budget held back for the tower leg while the "
        "slice, cluster, match and write phases run; 0 lets those phases "
        "spend the whole budget (the earlier shape).",
        minimum=0.0,
        maximum=1.0,
    ),
    OptionSpec(
        "tower_candidate_max_seconds",
        "float",
        "Per-candidate wall on the tower leg: a candidate is never started "
        "without this much budget left, and one that runs past it is "
        "abandoned and counted as tower_candidates_over_wall. 0 disables "
        "the wall.",
        minimum=0.0,
    ),
    _pos_int(
        "max_tower_members",
        "Representative members (newest first, then distinct source) fed to "
        "the tower leg's fuzzy event-match COMPARE; the full member list "
        "still lands every signal_event_links row on the write, this only "
        "bounds the walk over open events. A candidate reduced below this "
        "cap counts as tower_candidates_capped.",
        maximum=500,
    ),
    # --- 2026-11-09: the signal slice's progress contract ---------------------
    _pos_int(
        "min_signals_per_run",
        "Floor of the adaptive signal page: a tick that makes no slice "
        "progress halves the next page down to this; a tick that consumes a "
        "full page grows it by a quarter, up to max_signals.",
    ),
    OptionSpec(
        "catch_up_floor_hours",
        "float",
        "One-time catch-up floor: on the first tick under a given value, a "
        "signal_slice watermark older than now minus this many hours jumps "
        "to that floor, and the skipped span (from, to, signal count) is "
        "logged and recorded on the watermark and the receipt. Re-arms only "
        "when the value changes; 0 disables (the backlog then drains "
        "oldest-first at the adaptive page).",
        minimum=0.0,
        maximum=_MAX_WINDOW_HOURS,
    ),
    # --- 2026-11-14 (N-Z): catch-up split, multi-page passes, lag -------------
    OptionSpec(
        "catch_up_lag_minutes",
        "float",
        "When the signal_slice cursor is more than this many minutes behind, "
        "the head jumps to now minus catch_up_head_minutes and the gap behind "
        "it becomes a backfill segment drained oldest-first after the head "
        "each pass (nothing is skipped). 0 disables the split.",
        minimum=0.0,
        maximum=_MAX_WINDOW_HOURS * 60.0,
    ),
    OptionSpec(
        "catch_up_head_minutes",
        "float",
        "How far back from now the head restarts when a catch-up split opens.",
        minimum=1.0,
        maximum=_MAX_WINDOW_HOURS * 60.0,
    ),
    _pos_int(
        "max_pages_per_pass",
        "Pages (head then backfill) one pass may read while the front budget "
        "lasts; a page is read only after the previous one was full and fully "
        "processed.",
        maximum=100,
    ),
)


EVENT_RECONCILER_OPTIONS = (
    _pos_int("max_events", "Event rows reconciled per run."),
    _pos_int("evolves_min_actors", "Shared resolved actors required for evolves_from."),
    _pos_int(
        "evolves_max_gap_hours",
        "Maximum evidence-time gap for temporally adjacent evolves_from.",
    ),
    OptionSpec(
        "pass_budget_seconds",
        "float",
        "Per-tick wall-clock ceiling, mirroring "
        "LEGBA_EVENT_RECONCILER_PASS_BUDGET_SECONDS; 0 disables.",
        minimum=0.0,
    ),
)


#: P4b graph projector — whole-rebuild; the first-activation probe plus the
#: P-7 cost knobs (unlogged build, arc diet, skip-when-unchanged).
GRAPH_PROJECTOR_OPTIONS = (
    OptionSpec(
        "dry_run",
        "bool",
        "Build graph_arcs_new and report the counts without swapping it "
        "into place or writing graph_arcs_meta.",
    ),
    OptionSpec(
        "include_contention_arcs",
        "bool",
        "Also project fact -> fact contends_with arcs between contested "
        "value clusters (default off: no graph reader uses them).",
    ),
    OptionSpec(
        "include_lineage_copies",
        "bool",
        "Also project the citation lineage copied from derived_from, "
        "citations[], output_consumption and situations.derived_from "
        "(cites / member_of; default off: no graph reader traverses them).",
    ),
    OptionSpec(
        "unlogged",
        "bool",
        "Build the projection UNLOGGED (default on: no write-ahead log; "
        "emptied by crash recovery and rebuilt on the next tick).",
    ),
    OptionSpec(
        "skip_when_unchanged",
        "bool",
        "Skip the rebuild when no source table changed since the last build "
        "(default on; the skip is recorded as checked_at).",
    ),
    OptionSpec(
        "max_skip_seconds",
        "float",
        "Rebuild anyway once the serving build is older than this, even over "
        "unchanged inputs (default 86400; 0 disables the ceiling).",
        minimum=0.0,
    ),
)

#: W9 VIEW-1 profile projector — whole rebuild per plane; the one knob is the
#: first-activation probe (build every plane's rows, report, write nothing).
PROFILE_PROJECTOR_OPTIONS = (
    OptionSpec(
        "dry_run",
        "bool",
        "Build every declared plane's rows and report the per-plane counts "
        "without writing profile_projection or its _meta receipt.",
    ),
)


__all__ = [
    "EVENT_CLUSTERING_OPTIONS",
    "EVENT_RECONCILER_OPTIONS",
    "GRAPH_PROJECTOR_OPTIONS",
    "PROFILE_PROJECTOR_OPTIONS",
]

#: H13 (2026-09-24) — receipt_anchor: the daily Merkle-root anchor over the
#: per-analyst receipt heads. Pure SQL + two calendar POSTs; it exposes no
#: operator knob, and the X-1 catalog requires the explicit empty declaration
#: so an undeclared knob can never be read silently.
RECEIPT_ANCHOR_OPTIONS: tuple = ()

#: The event-plane slice of the X-1 catalog, spread into ``HANDLER_OPTIONS`` by
#: ``handler_options.py`` as one line so the catalog file stays under its
#: size ceiling (it sat exactly at 1,660 lines after P4b). Keys are the
#: ``deterministic.SUB_HANDLERS`` names.
EVENTS_CATALOG: dict[str, tuple] = {
    "event_clustering": EVENT_CLUSTERING_OPTIONS,
    "event_reconciler": EVENT_RECONCILER_OPTIONS,
    "graph_projector": GRAPH_PROJECTOR_OPTIONS,  # V3/P4b + P-7 cost knobs
    "profile_projector": PROFILE_PROJECTOR_OPTIONS,  # W9 VIEW-1 — dry_run is the only knob
    "receipt_anchor": RECEIPT_ANCHOR_OPTIONS,    # H13 — no knobs; explicit empty tuple
}

