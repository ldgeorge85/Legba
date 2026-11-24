# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The workstation vocabulary — what a consult CASE may say (C-1b, design C-1a §2/§6).

A *case* is an ordered list of workstation actions one consult turn presents:
open a panel, arrange the wall, scope it, focus a record, point the map, set
the clock, run a mission, narrate, seal. This module is the ONE authority on
the words those actions may use — panel kinds, stances, presets, map layers,
missions, span presets, ref kinds — and on the run-scoped limits around them.

Two readers, one file:

* the backend — ``analysts/consult_case_schema.py`` builds the nine drive
  tools' closed JSON schemas from these lists, so ``validate_args`` refuses an
  unknown kind, layout, layer or mission by the existing ``invalid_arg``;
* the workstation — ``scripts/gen_workstation_vocabulary.py`` exports
  :func:`export_document` to ``legba-ui-v3/src/lib/workstationVocabulary.json``,
  which the UI imports at build time. A drift test fails the suite when an id
  named here is missing from its TypeScript source, and when the committed
  JSON is not what this module generates.

**A leaf.** Nothing from ``legba`` is imported here, so the registry (which
ships a slim image and may import leaf modules only) can stamp the vocabulary
hash on a case record without dragging the analyst package in.

**Platform, not exemplar.** The ids below are the workstation shell's ids,
named for what the surface IS (a map, a timeline, a findings feed). Two ids
are exemplar-shaped and are named so here rather than hidden: the ``desk``
stance and the ``desk_watch`` mission (a subject-family word from the
geopolitics instance), and ``v4.map`` whose camera resolves a subject through
a country table today. UI-D1 owns their rename or alias (C-1a §6.4/§6.5); a
rename goes through ``panel-registry/aliases.ts`` and recorded cases resolve
through it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "ARRANGEABLE_LAYOUTS",
    "CASE_BLOCK_CAUSES",
    "CASE_FRAME_KINDS",
    "CASE_PHASE",
    "CASE_RECORD_VERSION",
    "CASE_STATUSES",
    "DRIVABLE_KINDS",
    "DRIVE_TOOL_NAMES",
    "LIMITS",
    "MAP_LAYERS",
    "MAP_SURFACE_KIND",
    "MAP_VIEWS",
    "MISSIONS",
    "PANEL_TABS",
    "REF_KINDS",
    "SCOPE_KINDS",
    "SPAN_PRESETS",
    "VOCABULARY_VERSION",
    "WORKSTATION_DRIVE_PACK_ID",
    "export_document",
    "vocabulary_hash",
]

#: Bumped when a word is REMOVED or changes meaning. Adding a word is additive
#: and changes only :func:`vocabulary_hash`, which every record carries.
#: ``/2`` (UI-B1/merges, W8): ``system.navigator`` and ``v4.kpi`` left the
#: drivable kinds and Provenance's ``flow`` / ``trajectory`` / ``narratives``
#: tabs left it (they are the Registry's Canvas tab and the Register's tabs
#: now). A recorded case naming a retired id still replays: the UI resolves it
#: through ``panel-registry/aliases.ts``, never through this module.
#: ``/3`` (UI-B3, W11): the eight per-subject read kinds
#: (``target.{overview,signals,findings,claims,situations,sources,graph,
#: desk_brief_page}``) left the drivable kinds for ``subject.page`` — one bound
#: kind whose ``tab`` is the read (Brief · Evidence · Claims · Signals ·
#: Situations · Sources · Graph · Runtime) — and the ``desk`` stance re-seeds on
#: it (UI-D1 Table A's Subject stance). A recorded case naming a retired id
#: still replays through the alias table.
VOCABULARY_VERSION = "workstation_vocabulary/3"

#: The case record's own schema version (C-1a §3). ``case/2`` is UI-D1's.
CASE_RECORD_VERSION = "case/1"

#: The grant pack the nine tools live in (``descriptors/action_pack_workstation_drive.yaml``).
WORKSTATION_DRIVE_PACK_ID = "workstation_drive"

#: The nine tools, in the order the model is shown them. Closed: a tenth is a
#: vocabulary change, a schema change and a pack change, never one of the three.
DRIVE_TOOL_NAMES: tuple[str, ...] = (
    "open_panel",
    "arrange",
    "set_scope",
    "focus",
    "map",
    "timeline",
    "run_mission",
    "narrate",
    "done",
)

# ---------------------------------------------------------------------------
# Ref kinds — what a cite / focus / scope / subject may name
# ---------------------------------------------------------------------------

#: The ``{kind, id}`` kinds an action may cite or point at. Each one is a
#: workstation SELECTION kind (``state/selection.ts::SelectionKind``), so a
#: focused ref always has an Inspector to land in. Kinds the selection store
#: has but no read tool returns (``absence``, ``contention``) are deliberately
#: absent: a ref may only be used once a tool returned it (C-1a §2), and
#: nothing returns those, so offering them would offer a refusal.
#:
#: ``report`` and ``finding`` share one id space: a report IS a finding row
#: from a composition producer, so a finding the run read may be scoped as a
#: report by the same id (the read-set check folds the two).
REF_KINDS: tuple[str, ...] = (
    "target",
    "entity",
    "source",
    "analyst",
    "finding",
    "report",
    "situation",
    "signal",
    "event",
    "journal_entry",
)

#: ``set_scope`` kinds (``state/scope.ts::ScopeKind``). ``none`` clears the
#: scope (the whole roster) and takes no id.
SCOPE_KINDS: tuple[str, ...] = (
    "report",
    "target",
    "entity",
    "situation",
    "journal_entry",
    "none",
)

# ---------------------------------------------------------------------------
# Panels (decision D3 — a CLOSED allowlist)
# ---------------------------------------------------------------------------

#: The drivable panel kinds and what each needs. D3 (operator, 2026-10-04):
#: the Awareness, Investigation and Products nav groups, plus the per-subject
#: (``target.*``) and per-analyst (``analyst.*``) read kinds and the
#: cross-framing drill; NOT the Engine Room, NOT the Analysis group's
#: operator instruments, NOT the authoring surfaces (Model Stack Settings,
#: Subscription Builder). ``system.consult`` is not here: the presenting tile
#: is the shell's to keep open (an ``arrange`` re-opens it), never the case's.
#:
#: ``subject`` is the ref kind a BOUND kind requires (None = a singleton that
#: takes none). ``group`` is the nav group it is drawn from, recorded so the
#: allowlist's derivation is reviewable rather than asserted.
#:
#: UI-B1/merges (W8) retired ``system.navigator`` (onto the Morning Read; the
#: products index is the object rail's Reads section) and ``v4.kpi`` (the
#: Wall's header band), so neither is drivable; the survivors carry their
#: tabs below. Of the three kinds it registered, ``system.register`` is an
#: Investigation kind (the Situations / Narratives reads that were Provenance
#: tabs, and the Investigate stance seeds it) and is drivable by D3's own
#: derivation; ``system.registry`` and ``system.ledgers`` are Engine Room and
#: are not.
DRIVABLE_KINDS: dict[str, dict[str, Any]] = {
    # --- Awareness ---
    "system.wall": {"group": "awareness", "subject": None},
    "system.findings": {"group": "awareness", "subject": None},
    "system.inspector": {"group": "awareness", "subject": None},
    "v4.map": {"group": "awareness", "subject": None},
    "system.timeline": {"group": "awareness", "subject": None},
    "system.alerts_watches": {"group": "awareness", "subject": None},
    # --- Investigation ---
    "system.entities": {"group": "investigation", "subject": None},
    "system.search": {"group": "investigation", "subject": None},
    "system.graph_walk": {"group": "investigation", "subject": None},
    "system.provenance": {"group": "investigation", "subject": None},
    "system.register": {"group": "investigation", "subject": None},
    # --- Products ---
    "v4.morning_read": {"group": "products", "subject": None},
    "v4.assessment": {"group": "products", "subject": None},
    "system.journal": {"group": "products", "subject": None},
    "system.report_export": {"group": "products", "subject": None},
    # --- Per-subject read kinds (bound: a target ref is required) ---
    # UI-B3: the subject page — one kind, its reads are tabs (PANEL_TABS).
    "subject.page": {"group": "per_subject", "subject": "target"},
    "target.map": {"group": "per_subject", "subject": "target"},
    "target.timeline": {"group": "per_subject", "subject": "target"},
    # --- Per-analyst read kinds (bound: an analyst ref is required) ---
    "analyst.runs": {"group": "per_analyst", "subject": "analyst"},
    "analyst.outputs": {"group": "per_analyst", "subject": "analyst"},
    "analyst.cross_target": {"group": "per_analyst", "subject": "analyst"},
    "analyst.critiques": {"group": "per_analyst", "subject": "analyst"},
    # --- The cross-framing drill (the parked slot; its subject is the desk) ---
    "analysis.cross_framing": {"group": "drill", "subject": "target"},
}

#: Tabs a kind can be opened ON (``params.tab``). A kind absent here takes no
#: tab, and a tab named for it is refused. The ids are the panels' own
#: ``Tab``/``Mode`` unions (``panels/merged/*.tsx``). Only a DRIVABLE kind
#: carries tabs here: the Engine Room survivors' tabs (System Status,
#: Registry, Ledgers, Optimizer) are the shell's, not a case's.
PANEL_TABS: dict[str, tuple[str, ...]] = {
    "system.timeline": ("events", "validity"),
    "system.alerts_watches": ("watches", "triggers", "deliveries"),
    "system.provenance": ("why", "lineage", "fanout"),
    "system.register": ("situations", "narratives"),
    "system.wall": ("overview", "movers"),
    # UI-B3 — ``lib/subjectPage.ts::SubjectTab`` (UI-D1 §3: subject.page takes
    # a subject id and a tab).
    "subject.page": (
        "brief", "evidence", "claims", "signals", "situations", "sources", "graph", "runtime",
    ),
}

#: The map surface this vocabulary drives. ``None`` in a profile that declares
#: no map — every ``map`` action is then refused ``no_map_surface`` by name.
MAP_SURFACE_KIND: str | None = "v4.map"

#: ``v4/world/worldState.ts::WorldLayer``.
MAP_LAYERS: tuple[str, ...] = ("signals", "findings", "situations", "entities", "events")

#: ``fit_ref`` needs a read ref and moves the camera to the record's own
#: geometry; ``reset`` returns the camera home. No coordinates from the model.
MAP_VIEWS: tuple[str, ...] = ("fit_ref", "reset")

# ---------------------------------------------------------------------------
# Layouts and missions — arrangeable only when every tile they seed is drivable
# ---------------------------------------------------------------------------

#: ``arrange`` targets: stance ids (``lib/workspaces.ts``) and preset ids
#: (``lib/layoutPresets.ts``), with the tiles each seeds. A layout is offered
#: only when every tile it seeds is in :data:`DRIVABLE_KINDS` (or is the
#: Consult tile the shell keeps open) — so ``trust``/``gate``/``engine`` and
#: the ``monitoring``/``analysis``/``operations`` presets, which seed Engine
#: Room or operator instruments, are not arrangeable by a case (D3).
ARRANGEABLE_LAYOUTS: dict[str, dict[str, Any]] = {
    # X3/D2 — the five-tile landing: the read (the Wall one tab behind it),
    # Consult, the Live Feed and the map; everything else is in the catalog.
    "morning_read": {
        "type": "stance",
        "seeds": ("v4.morning_read", "system.wall", "system.consult", "system.findings", "v4.map"),
    },
    # UI-D1 Table A's Subject stance (id ``desk``): the subject page (seeded
    # unbound, it follows the selected subject), the feed, the clock, Consult.
    "desk": {
        "type": "stance",
        "seeds": ("system.findings", "system.inspector", "system.consult", "subject.page", "system.timeline"),
    },
    "investigate": {
        "type": "stance",
        "seeds": (
            "system.findings", "system.inspector", "system.entities",
            "system.provenance", "system.register", "system.graph_walk", "system.consult",
        ),
    },
    "wall": {"type": "preset", "seeds": ("system.wall", "system.inspector")},
    "workspace": {
        "type": "preset",
        "seeds": ("system.findings", "system.inspector", "system.consult", "system.provenance"),
    },
    "investigation": {
        "type": "preset",
        "seeds": ("system.findings", "system.inspector", "system.entities", "system.search"),
    },
    "focus": {"type": "preset", "seeds": ("system.inspector",)},
}

#: ``run_mission`` targets (``lib/missions.ts::MissionId``) with the stance
#: each mounts and the tiles it adds. ``release`` mounts the Trust stance
#: (Engine Room instruments) and is not offered.
MISSIONS: dict[str, dict[str, Any]] = {
    "morning_read": {"stance": "morning_read", "adds": ("system.consult",)},
    "desk_watch": {"stance": "desk", "adds": ("v4.map", "system.consult")},
    "crisis": {"stance": "investigate", "adds": ("v4.map", "system.timeline", "system.consult")},
}

# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------

#: ``timeline.window.span`` values — the scrubber's own outer-span presets
#: (``lib/mapTime.ts::SPAN_PRESETS``), in seconds. A span wider than the widest
#: preset has no scrubber to show it on, so it is not offered.
SPAN_PRESETS: dict[str, int] = {
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}

# ---------------------------------------------------------------------------
# Run-scoped limits, frames, statuses, causes
# ---------------------------------------------------------------------------

LIMITS: dict[str, int] = {
    #: ``cites`` per action (every action but ``done`` carries 1..8).
    "max_cites": 8,
    #: ``narrate.text`` characters (plain text).
    "narrate_max_chars": 600,
    #: ``done.summary`` characters.
    "done_summary_max_chars": 300,
    #: Admitted actions per case — the per-run cap (``PackGovernor.max_invocations_per_run``
    #: overrides it when the pack declares one).
    "case_cap_default": 30,
    #: Rounds a case turn defaults to (the 30-round ceiling is unchanged).
    "case_rounds_default": 20,
    #: How far past "now" an absolute window's ``end`` / ``at`` may reach.
    "future_skew_seconds": 300,
    #: The widest absolute window (``start``..``end``) a case may set.
    "max_window_seconds": 365 * 86400,
    #: Characters of an id inside a ref.
    "ref_id_max_chars": 200,
}

#: The ``phase`` every case frame carries on the step stream.
CASE_PHASE = "case"

#: Step-frame kinds (C-1a §4). ``case_action`` is one admitted action;
#: ``case_refused`` a refusal (trace only, never executed); ``case_sealed`` the
#: ``done``; ``case_stopped`` the operator's stop (published by the registry).
CASE_FRAME_KINDS: tuple[str, ...] = ("case_action", "case_refused", "case_sealed", "case_stopped")

#: ``sealed`` = the model called ``done``; ``unsealed`` = the run ended without
#: it (budget, cap, error — honest, nothing invented); ``stopped`` = the
#: operator stopped it.
CASE_STATUSES: tuple[str, ...] = ("sealed", "unsealed", "stopped")

#: The case gate's refusal vocabulary (C-1a §5), written to ``governor_events``
#: under ``record_governor_event`` like every other block. Closed: a cause not
#: here never reaches a governor event.
CASE_BLOCK_CAUSES: frozenset[str] = frozenset({
    "drive_not_armed",
    "ref_not_read",
    "no_citation",
    "case_cap_reached",
    "case_sealed",
    "case_stopped",
    "no_map_surface",
    "kind_not_drivable",
})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _vocabulary() -> dict[str, Any]:
    """The vocabulary as plain JSON-able data (tuples → lists, sets sorted)."""
    return {
        "version": VOCABULARY_VERSION,
        "case_record_version": CASE_RECORD_VERSION,
        "pack_id": WORKSTATION_DRIVE_PACK_ID,
        "tools": list(DRIVE_TOOL_NAMES),
        "ref_kinds": list(REF_KINDS),
        "scope_kinds": list(SCOPE_KINDS),
        "drivable_kinds": {
            k: {"group": v["group"], "subject": v["subject"], "tabs": list(PANEL_TABS.get(k, ()))}
            for k, v in DRIVABLE_KINDS.items()
        },
        "map_surface_kind": MAP_SURFACE_KIND,
        "map_layers": list(MAP_LAYERS),
        "map_views": list(MAP_VIEWS),
        "layouts": {
            k: {"type": v["type"], "seeds": list(v["seeds"])}
            for k, v in ARRANGEABLE_LAYOUTS.items()
        },
        "missions": {
            k: {"stance": v["stance"], "adds": list(v["adds"])}
            for k, v in MISSIONS.items()
        },
        "span_presets": dict(SPAN_PRESETS),
        "limits": dict(LIMITS),
        "case_phase": CASE_PHASE,
        "case_frame_kinds": list(CASE_FRAME_KINDS),
        "case_statuses": list(CASE_STATUSES),
        "case_block_causes": sorted(CASE_BLOCK_CAUSES),
    }


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def vocabulary_hash() -> str:
    """sha256 over the canonical vocabulary — stamped on every case record."""
    return hashlib.sha256(_canonical(_vocabulary()).encode("utf-8")).hexdigest()


def export_document(contract: dict[str, Any] | None = None) -> str:
    """The text of ``legba-ui-v3/src/lib/workstationVocabulary.json``.

    ``contract`` is the frozen tool/frame/record schema document
    (``consult_case_schema.contract_document()``), embedded under
    ``"contract"`` so the UI reads one file. Pretty-printed, keys sorted,
    trailing newline — deterministic bytes, which is what the drift test pins.
    """
    doc: dict[str, Any] = {"vocabulary": _vocabulary(), "vocabulary_hash": vocabulary_hash()}
    if contract is not None:
        doc["contract"] = contract
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
