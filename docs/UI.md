# Legba UI — Operator Console

This document describes `legba-ui-v3`, the operator console for Legba: a
**composable panel workstation**. It is a single-page **Vite + React +
TypeScript** app whose workspace is a [Dockview](https://dockview.dev) tiling
surface — the operator opens **panels** into a draggable grid, and each panel
is a self-contained read (or authoring) surface over one slice of the
substrate, the registry, or the runtime. New here? Start with the
[README](../README.md) and the [Tour](TOUR.md).

The read/drill product surfaces — the **cited read card** in the Inspector, the
**banded per-country scorecard** and the **honest skill scoreboard** in Eval
Scorecard, and the **provenance lineage DAG** in The Why — are described in §3.
The system measures groundedness (does each claim follow from its cited
evidence?), not truth; the UI is written to state that plainly and to show weak
or unmeasured results honestly rather than hide them.

The console is **descriptor-driven**. Two registries cooperate:

- a **bundle-time panel registry** (`src/panel-registry/registry.ts`) — the
  static catalog of every panel *kind* the app ships, and
- the **runtime descriptor registry** (the backend) — the live list of panel
  *instances* (one per bound target/analyst), fetched as
  `PanelRegistration` rows over REST and kept current over a NATS-fed
  WebSocket.

Adding a new panel kind is a code change (one registry row + one component).
Adding a new panel *instance* — a new country target, a new analyst — is a
descriptor-only change: the new binding shows up in the sidebar with no UI
rebuild. See `ARCHITECTURE.md` for the descriptor registry and runtime, and
`ACQUISITION.md` for the source / signal / fan-out model the panels read.

**Contents:** [1. The shell](#1-the-shell) ·
[2. Panel registration & loading](#2-panel-registration--loading) ·
[3. The panel set](#3-the-panel-set) ·
[4. Data access & the auth chain](#4-data-access--the-auth-chain) ·
[5. Building & running](#5-building--running) ·
[6. Panel tiers](#6-panel-tiers) ·
[7. Future seams](#7-future-seams)

---

## 1. The shell

The root component (`src/App.tsx`) lays out three regions:

```
┌──────────────────────────────────────────────────────────┐
│ Sidebar │            Dockview workspace                   │
│ (panel  │            (per-panel tiles)                    │
│  tree)  │                                                 │
│         ├─────────────────────────────────────────────────│
│         │            StatusBar                            │
└──────────────────────────────────────────────────────────┘
```

- **Sidebar** (`components/Sidebar.tsx`) — a grouped panel tree: a layout-preset
  selector, the singleton panels grouped by operator task, and **Targets** /
  **Analysts** / **Dashboards (registered)** sections expanded from the live
  registry rows. Clicking a row opens that panel in the workspace.

  The #90 redesign splits the sidebar into two **top-level product sections** so
  the intelligence product leads and the plumbing is demoted (`Sidebar.tsx`):
  - **Intelligence** — the product the operator reads (findings, the world
    assessment, consult, lineage, entities), leading the rail.
  - **Operations** — the runtime/plumbing surfaces (actor health, dead-letter,
    stream lag, governor, budget, audit), which starts **collapsed** on first
    run so the product leads.

  Within those sections singleton panels auto-slot into named task groups
  (`panel-registry/navGroups.ts`): **Monitor** (what's happening now) /
  **Investigate** (dig into the why) / **Configure** (registries, tenancy) /
  **Operate** (runtime/plumbing) / **More** (the catch-all disclosure, collapsed
  by default). A panel's group comes from an explicit per-kind override, else a
  prefix fallback on its kind segment (`registry.`/`source.` → Configure,
  `system.` → Operate, …), so a new panel kind auto-slots with no nav edit.
- **Workspace** — a `DockviewReact` instance (`dockview-theme-abyss`). Each
  tile is a `LegbaPanelComponent` frame that resolves its bound
  `PanelRegistration` (or a synthetic singleton registration) to a lazy-loaded
  React component.
- **StatusBar** (`components/StatusBar.tsx`) — footer showing the active
  deployment mode badge, the registered-panel count, the last registry refresh
  time, the auth state (`auth: ok` / `auth: dev`), and any registry-load error.

Panel components are **code-split** (`React.lazy`) and suspend behind a
"Loading panel…" fallback, so the initial bundle stays small and a panel's
chart/map libraries load only when its tile is first opened.

The console now centers on a **unified selection store** (`src/state/selection.ts`,
`useSelection`): a single source of truth for "what is selected" across the whole
app, capped at one selection plus a drill breadcrumb. Click any row / map dot /
graph node / id anywhere and that record brushes every subscribing room and loads
its full detail in the **Inspector**. The store replaced three former selection
systems — the v4 cross-room store, Flow's local `selectedNodeId`, and the
`legba:open-*` window-event bus (the ~20 `legba:open-lineage` dispatchers now call
`selectRow()` and every listener subscribes to the store instead). A `row_kind →
SelectionKind` bridge maps substrate `row_kind` strings onto the cross-room
vocabulary so a lineage-walk click is never a dead-end.

### Command palette

`Ctrl/Cmd-K` toggles the **command palette** (`components/CommandPalette.tsx`):
a fuzzy quick-open modal over every non-binding singleton panel available in the
current mode. Arrow keys move the selection, `Enter` opens, `Esc` (or a backdrop
click) dismisses. Binding-required panels (per-target / per-analyst) are *not*
offered here — opening one unbound would only render a placeholder — so they
live in the sidebar's per-scope groups instead.

### Boot layout

On first workspace-ready, in `personal` and `cis` modes the shell seeds the
**rebalanced daily-driver grid** (redesign Move 6 — "the active task gets the
room"). The map no longer anchors the canvas (it used to eat ~70%); the **Live
Feed** is the anchor, the **Inspector** is a first-class right rail, and the
**World Map** is demoted to a bottom strip with **Why** tabbed within it:

```
┌──────────────────────────────────┬──────────────────┐
│  Live Feed (system.findings)     │  INSPECTOR       │
│  the active task — the surface   │  (system.        │
│  you scan; ~65% width            │   inspector)     │
├──────────────────────────────────┤  ~35% width      │
│  World Map (v4.map) — demoted    │  selection →     │
│  bottom strip ~28% tall          │  full detail     │
│  + Why (v4.why) tabbed within    │                  │
└──────────────────────────────────┴──────────────────┘
```

(The Live Feed anchor is `system.findings` — the unified findings+signals feed
described in §3 *Daily driver*. The former separate `v4.feed` rail was deleted
in the #90 feed merge; `system.findings` subsumed it wholesale.)

The seed order is Live Feed (anchor) → Inspector (right of the feed) → World Map
(below the feed) → Why (tabbed *within* the map group). After seeding,
`sizeWorkspace` pins the proportions so the split isn't a naive 50/50: the
Inspector to ≈35% of the canvas width and the map to a ≈28%-tall bottom strip,
leaving the feed the dominant surface. `cis` boots the same grid — every panel in
the seed ships in both `personal` and `cis`.

### Layout presets & custom layouts

The sidebar's **Layout preset** dropdown (`lib/layoutPresets.ts`) re-seeds the
workspace with a named arrangement. Six presets ship (the redesign swapped the
former Lineage slot for the keystone Inspector and added a Zen focus mode; the
#90 redesign added the **Workspace** intel-desk preset):

| Preset | Panels |
| --- | --- |
| **Monitoring** | Live Feed · Inspector · Target Registry · Alert Center |
| **Workspace** | Live Feed · Inspector · Consult · Why (the 2×2 intel desk, all brushed by the shared selection) |
| **Investigation** | Live Feed · Inspector · Entities · Global Search |
| **Analysis** | Optimizer · Eval Scorecard · Consult · Deep Consult |
| **Operations** | Actor Health · Dead-letter · Stream Lag · Governor Events |
| **Focus (Zen)** | Inspector alone, full canvas — an undistracted single-record read |

(Monitoring's third tile is the canonical **Target Registry** — the former
`system.targets.roster` was collapsed into it in #90 Wave A, so the preset points
at `registry.targets`, not the hidden roster dupe.)

Presets are seeded through the same singleton opener the boot grid uses, so
preset tiles are mode-gated identically and indistinguishable from
hand-opened ones (a preset that includes an operator-only panel simply skips it
in `cis`). The **Save** / **Restore** buttons round-trip the live, hand-dragged
layout through Dockview's own `toJSON()` / `fromJSON()` serializer into
`localStorage`, keyed per mode.

### Deployment modes

`auth/jwt.ts::currentMode()` resolves the active mode (`personal` |
`above_ai` | `cis`), highest priority first:

1. the `?mode=` URL query param (operator override for testing),
2. the `mode` claim of the stored bearer token (best-effort JWT decode),
3. the `VITE_LEGBA_DEFAULT_MODE` build env, then
4. `personal` (the daily-driver default).

Each panel kind declares the modes it ships in; the sidebar, the command
palette, and the layout opener all filter on the active mode. `personal` is the
single-operator daily driver (the widest panel set); `cis` is an alternate
panel-set lens that adds an owner-rollup convenience view. **It is NOT a
multi-tenant isolation surface** — Legba ships single-tenant (one operating
tenant; see `docs/DIRECTION.md` §0); the mode only filters which panels show.
The mode is a UI lens — the registry remains the source of truth for what data
the bearer can actually read, and there is no per-tenant access boundary behind
it.

---

## 2. Panel registration & loading

Each `PanelRegistration` row (mirroring
`src/legba/data/outputs/ui_panel.py::PanelRegistration`) carries a `panel_id`
(the descriptor-facing logical id, e.g. `target_overview`), a `binding`
(the scope JSON, e.g. `{ "target_id": "br_country" }`), a `title`, a `mode`,
and lifecycle fields. The flow (`panel-registry/loader.ts`):

1. `resolvePanel(reg)` maps the descriptor `panel_id` → the bundle `PanelKind`
   (e.g. `target_overview` → `target.overview`). An unknown `panel_id` is
   **non-fatal**: the loader returns an `UnboundPanelPlaceholder` marker so a
   renamed/removed kind never crashes the shell.
2. `extractScope(reg)` pulls `target_id` / `analyst_id` / `dashboard_id` out of
   the binding.
3. `instanceId(kind, scope)` builds a stable Dockview panel id
   (`<kind>:<scope_value>`, or the bare kind for singletons), so re-opening a
   bound panel re-focuses the existing tile instead of duplicating it.

`useRegistry(mode)` (`panel-registry/useRegistry.ts`) owns the live instance
list: it fetches `GET /api/v1/registry/ui_panels?mode=<mode>` on mount and
subscribes to the `registry.>` NATS subjects over the events WebSocket. Any
`registry.bindings.*` / `registry.targets.*` / `registry.analysts.*` event
triggers a focused **refetch** (the SQL surface is authoritative; the WS feed is
a "something changed" nudge). Retired rows are filtered from the sidebar but kept
resolvable so deep links into them don't 404.

A handful of panel kinds are registered but **hidden** (`HIDDEN_KINDS` in
`registry.ts`): they stay in the bundle so saved layouts referencing them still
resolve, but don't surface in the sidebar or palette. The set has two cohorts:
the original §6 DROP set (an empty Global-Pulse aggregate, the single-operator
Users panel, a NATS-tail stub, the empty wiring/mutations editors, the abstract
dynamic dashboard) and the **#90 Wave A consolidation** set (Discovery, Backfill,
Targets Roster, Casework, Tenant View, World Assessment, Runtime Actor Health) —
see *Consolidated / hidden by #90* below.

---

## 3. The panel set

Panels are grouped here by operator purpose. The frontend kind is in
`code font`.

### The Inspector (keystone)

The redesign's keystone is the **Inspector** — a single, persistent, docked-right
tile that headlines the whole panel set.

- **Inspector** (`system.inspector`) — the unified selection-linked detail
  surface (`components/inspector/InspectorPanel.tsx`) and the console's primary
  **read** surface. It is driven entirely by the unified selection store
  (`src/state/selection.ts`, `useSelection`): click any row / map dot / graph
  node / id anywhere and its full detail loads here, with every referenced id
  rendered as a `RecordLink` so the next selection is one click away and a
  breadcrumb (drill-through `history`) trails behind you. It is built atop the
  existing `PanelChrome` + `DescriptorView` and reuses the Why provenance trail
  (`GET /lineage/{kind}/{id}`) — not a new rendering stack. With nothing selected
  it shows a call-to-action empty state ("click a finding, signal, entity,
  target, or any id"); the world assessment is now just a FINDING and opens here
  like any other when selected. The store is **capped at one selection**
  (brushing-and-linking degrades past ~3 surfaces — which is the design reason
  for three rooms + one Inspector, not 82 panels). It ships in `personal` and
  `cis`, and is the right rail of the boot grid and the Monitoring / Investigation
  / Focus presets.

  For a finding the Inspector renders the **cited read card**
  (`components/inspector/CitedAssessment.tsx`) at the top — this is the drillable
  product read. The report prose renders with its inline `[N]` markers turned
  into clickable citation chips: a chip scrolls to and flashes the matching row
  in the **Evidence** panel below, whose title is itself a `RecordLink` into the
  cited signal (so a claim drills to its source). A header strip carries the
  citation count, a **faithfulness-verify label** (`ShieldCheck` + the
  `faithfulness_score` when the merged body carries the verify block, else an
  honest `unverified` label — never a fabricated score), and the per-unit
  **eval badge** (`UnitEvalBadge`, below). A legacy / uncited finding degrades
  honestly: its prose renders plainly under an explicit "uncited (legacy
  finding)" marker with no fabricated anchors and no empty evidence panel.

  The **UnitEvalBadge** (`components/inspector/UnitEvalBadge.tsx`, off
  `GET /api/v1/eval/scores`) shows a bounded reasoning unit's honest eval — a
  server-composed string like `verified | faithfulness 0.45 | unmeasured (0
  labels)` rendered verbatim (the "no invented number" contract lives on the
  server). It renders **nothing** when the analyst id is not a bounded unit, the
  scorer has never run, or the fetch fails — a non-unit finding gets no badge
  rather than a fabricated one.

### The Journal (reflective voice)

- **Journal** (`system.journal`) — the read surface over the `journal_assessor`,
  Legba's first-person reflective voice: the one analyst pointed at the whole
  organism (its own self / state / flow), narrating a coherent point of view
  *over* the rest of the system rather than cutting one slice of it. *"Poetry
  without evidence is noise. Evidence without perspective is just a log file."*
  The `journal_assessor` runs **on cadence** as an introspective instrument — a
  12h entry tier (`journal_assessor`) plus a daily `journal_consolidator` — and
  writes **only** `journal_entries`, off the fact / finding / nexus chain, so the
  reflective voice can never pollute product output. This panel reads the
  accruing entries live (routing those reflections back into the product via the
  human-gated proposal queue is a future item, not yet done).
  Renders `GET /api/v1/journal` (`panels/system/Journal.tsx`) with three stacked
  regions:
  - **The current inner landscape** — the single open
    `entry_kind='consolidation'` row rendered prominently at the top (the daily
    consolidation tier distils prior entries into one forward-carried narrative).
    With none yet, an empty-state note explains the consolidation opens once
    enough entries accumulate.
  - **Recent entries** — a scrollable stream of `entry_kind='entry'` cards below
    it, cursor-paginated via a "load more" button (`next_cursor`), each card
    showing its title, the period it reflects on, the markdown narrative (inline
    `[[ref:uuid]]` markers stripped — the binding lives in the claims sidecar),
    and per-entry honesty pills.
  - **Per-claim provenance chips** — every `claims[].refs` ref renders as a
    `ProvenanceChip` (the same chip The Why uses) bound to its specific cited
    span, not a footnote pile. A chip click calls the shared `selectRow` (origin
    `journal`) — opening the cited situation / assessment / nexus / fact in the
    Inspector and brushing the other rooms. The walk is **UP-only**, from the
    entry's in-payload refs; the journal is **off the lineage chain** (empty
    `derived_from`, excluded from the lineage catalog), so a chip is the *only*
    way to reach a journal row's citations and a downstream lineage walk never
    surfaces the journal itself. An unresolved ref (superseded / pruned) still
    renders as a slate `unknown` chip — the citation is never hidden.

  The **unverified-perspective style** is the panel's grounding-honesty surface,
  and the enforcement is the *visible distinction itself*, not an LLM stripper:
  a `[needs_citation]`-prefixed `text_span` (an uncited factual assertion that
  slipped the REFLECT flag) and a `kind="perspective"` claim (the voice / an
  inference, not a cited fact) both render in a distinct dashed amber style with
  an `uncited` / `perspective` badge — shown verbatim, never collapsed or hidden.
  Above the stream sits the **honesty banner**, keyed off the substrate-derived
  `calibration` verdict the route returns (the live metric, not a self-reported
  field) and cross-checked against the open consolidation's stored
  `honesty_flags`; it is never green-washed (the `forecast_unproven` /
  `calibration_thin` legs are stated plainly, with BSS and sample sizes), and it
  flags drift when the open consolidation omits a leg the live metric now
  raises. Ships in `personal` and `cis`. (It was tsc-green + fully wired but
  pending its first real in-browser render at the time of writing.)

  The journal's outward changes — a correction, a `change`, or a `self_revision`
  (including to its own instructions) — go to the **human-gated
  `journal_proposals` queue**, never a live table; the backend serves
  `GET /api/v1/journal_proposals` plus
  `POST /api/v1/journal_proposals/{id}/{accept,reject}` (accept runs an
  idempotent per-kind apply; a `self_revision` touching a protected section
  auto-rejects). A dedicated operator **review surface** for that queue is **not
  yet wired in the console** — the Mutations Queue panel (`registry.mutations`,
  itself hidden by #90) covers GEPA / entity-merge / nexus proposals but not the
  journal queue; the journal-proposals review panel is a tracked follow-up.

### System Status (per-layer health)

- **System Status** (`system.status`) — the at-a-glance per-component /
  per-layer health view the operator repeatedly asked for, answering "are all
  sources firing? how is the queue? which cadence triggers are stalled?" in one
  page instead of forcing a hunt across the plumbing panels. Renders
  (`panels/system/SystemStatus.tsx`) four colour-coded layer sections:
  - **Acquisition** — the per-source firing matrix off
    `GET /api/v1/v3/system/source-firing`: each source with its signals 24h/7d,
    last-seen age, last poll outcome, recent error count, and a
    firing / silent / error / paused badge. This is where a 403/429/5xx source or
    a silent (200-but-empty) feed shows up at a glance.
  - **Analysis** — the per-analyst cadence health off
    `GET /api/v1/v3/system/analyst-cadence`: last run, age, runs in the last
    1h/24h, last outcome, and a healthy / stale / silent badge. It reads
    **`analyst_traces`** (the actual run record) rather than
    `actor_state.last_run_at` — which is NULL — so it surfaces the cadence
    liveness the Actor Health panel structurally could not. The companion
    per-analyst run-timing route `GET /api/v1/v3/eval/analyst_runtime` reads the
    same `analyst_traces` for run count, avg/max wall-clock seconds, last run,
    and non-success count over a window.
  - **Queues** — consumer backpressure off the orphan-filtered
    `GET /api/v1/v3/streams/consumer_lag`: the `num_pending` headline lag with
    orphaned/deleted durables filtered out (the fix for the "tons of targets, all
    with 845 pending" phantom the raw lag view showed).
  - **Infra** — substrate component reachability (PG / NATS / registry
    readiness) rolled up from the existing health surfaces.

  A panel's nav group comes from its kind prefix, so `system.status` auto-slots
  into the **Operate** section (`navGroups.ts`, `system.*` → Operate) with no nav
  edit. Ships in `personal`. (Panel registered at
  `panel-registry/registry.ts` as `system.status` → `SystemStatus`; routes in
  `src/legba/data/registry/v3_api.py`, `build_v3_router`.) It is tsc-green with
  both new routes confirmed serving live data, but was pending its first real
  in-browser render at the time of writing.

### v4 visual workspace

The "three rooms" visual surface — World / Flow / Why — all selection-linked
through the same store. The boot anchor is no longer a `v4`-namespace feed: the
former `v4.feed` rail was **deleted** in the #90 feed merge and replaced by the
unified `system.findings` Live Feed (see §3 *Daily driver*). World Map / Flow /
Why ship in `personal` and `cis`.

- **World Map** (`v4.map`) — a **Leaflet** world map (`v4/world/LeafletWorldMap`)
  with a layer panel, time scrubber, KPI strip, and a selection drawer; demoted
  to the boot grid's bottom strip. A map click selects into the store.
- **Flow Canvas** (`v4.flow`) — The Flow: the live registry canvas over
  sources → targets → analysts → packs (`v4/flow`), with NiFi-style live
  telemetry. The node highlight reads `useSelection` (its former local
  `selectedNodeId` was retired into the store).
- **Why · Provenance** (`v4.why`) — The Why: selection-driven provenance and the
  **lineage DAG**. With nothing selected the room renders an in-panel **node
  picker** (recent findings / situations / entities) so it is useful on its own;
  select a finding / situation / signal anywhere and it renders the
  `ProvenanceTrail` chip chain (oldest → newest) plus the full `LineageGraph`
  (a Cytoscape render wrapped in a crash boundary), and an entity renders its
  relationship ego-graph. For a finding or a country a **Lineage / Lenses**
  toggle switches between the one-hop-at-a-time receipt DAG and the temporal /
  node-graph lenses. Each lineage hop carries its **SHA-256 receipt hash** and a
  `chain_consistent` boolean: a hop that re-hashes to its stored `receipt_hash`
  shows the honest badge `chain-consistent (single-node)` (**not** "signed" /
  "tamper-proof" — analyst-trace provenance is a hash-chained receipt, not an
  Ed25519 signature); a mismatched re-hash flags the hop. Signal hops render a
  `ModalityRef` link out to the real source URL, so a walk always reaches the
  clickable acquisition source. Tabbed within the map group in the boot grid.

Two former panels from this group were **hidden** by #90 Wave A (still in the
bundle, see *Consolidated / hidden by #90* below): **World Assessment**
(`v4.assessment`) and **Casework Board** (`v4.case`, an Excalidraw board shelved
because no pin entry points were wired). `v4.assessment` stays out of the default
nav, but its component (`v4/why/WorldAssessment.tsx`) has since been re-pointed:
for a **selected country** it renders the **bounded-unit read column**
(`CountryUnitsAssessment`) as the headline — one card per bounded reasoning unit
(**leadership_transition**, **energy_security**, **escalation**,
**narrative_coordination**), each carrying its `UnitEvalBadge` and linking its
latest cited read into the Inspector, with the **retired** `country_assessor`
one-pager demoted to a collapsible "legacy monolith" below it. That producer is
**stopped** — nothing in the spine reads it, and its ~1.2k historical findings
remain in the DB (unread), so the column surfaces whatever legacy one-pager
already exists but no new ones accrue; the bounded units + the per-country
composition supersede it. For the world view it renders the `world_assessor`
finding. Note: the world-view copy in this component and in the
`AssessmentBanner` strip still carries the earlier transitional framing ("one
producer's finding, not a global verdict") and has not been fully re-pointed to
the composed world view, so treat the world one-pager as one analyst's read until
that copy lands.

### Daily driver

The core of routine monitoring (the redesign demotes Lineage to the Inspector's
provenance trail and pairs Consult with Deep Consult).

- **Live Feed** (`system.findings`) — the single **unified findings + signals**
  feed and the console's landing/anchor surface (the #90 feed merge folded the
  former separate `v4.feed` rail into this one panel; `v4.feed` was deleted). It
  reads **both** `GET /findings` and `GET /signals` (the substrate-reads endpoint
  family) and folds in **two NATS live tails** — `analyst.*.finding` for findings
  and `legba.signals.>` for signals. Three controls drive it:
  - **Live** (on/off) — the only mode toggle. Live-ON tails new findings+signals
    in realtime (the pulse); Live-OFF tears down both WS subs and freezes the
    seeded history (browse).
  - **Source** (All / Findings / Signals) — gates which REST seed *and* which live
    tail are active at the data layer, so a Findings-only view pays zero cost for
    signals (and vice-versa).
  - **Cluster** (clustered ⇄ flat) — situation clustering, **findings-only**:
    near-duplicate re-assessments of one evolving situation collapse into a single
    canonical row with the superseded history one expander away; signals never
    join a cluster (enforced by the `clusterKeyOf` source guard in
    `lib/findingsViews.ts`) and render flat below the finding clusters.

  Plus localStorage **saved views**, a findings-only hourly sparkline, server-side
  target / analyst / severity filters, and #89 selection-follow — click a country
  anywhere and the feed re-seeds to that country's findings (a `target_id`
  filter, so the bounded-unit reads are included) *and* its geo signals. All
  grouping / sorting / view / row-mapping logic lives
  in `@/lib/findingsViews` so it is unit-tested without a DOM. A row click selects
  the row into the unified store (Inspector + Why follow); it also still deep-links
  provenance into Lineage.
- **Provenance Lineage** (`system.lineage`) — walks the `derived_from` DAG
  upstream or downstream from any substrate row via
  `GET /lineage/{row_kind}/{row_id}`. It listens for the
  `legba:open-lineage` cross-panel event, so a click in *any* panel
  (Findings, a target panel, Search, Alerts) deep-links here without route
  coupling. This is the spine of the console's provenance story — every output
  carries `derived_from`, and this panel renders it hop by hop down to the real
  source URL with zero dangling links (a lineage-integrity sweep prunes dangling
  `derived_from`). Each node carries a **SHA-256 `receipt_hash`** and a
  `chain_consistent` boolean; a re-hash-matched node shows the honest
  `chain-consistent (single-node)` badge (a hash-chained receipt, **not** an
  Ed25519 signature — do not read it as "signed" / "tamper-proof"). Signal nodes
  render a `ModalityRef` — a modality badge plus a `canonical_url`/`media_ref`
  link — so a walk reaches the clickable acquisition source. `ModalityRef` is
  driven by the `modality → renderer` registry (`lib/modalityRenderers.tsx`), the
  UI half of the modality → {extractor, renderer} registry (`DESIGN.md` §7.5).
- **Consult** (`system.consult`) — an on-demand ReAct analyst workbench. POSTs
  to `/api/v1/consult` (the registry-side proxy in
  `src/legba/data/registry/consult_api.py`), which invokes the
  `legba_consult_default` analyst actor through the Dapr sidecar, waits for the
  ReAct loop, and returns the structured answer plus its tool trace and
  citations (rendered as markdown). See `AI_MODELS.md` for the consult engine.
  #90 added **pin-to-context** (`components/system/Consult.tsx`): pin records from
  the shared selection into a sticky set that accumulates as you navigate, and
  every pin is injected into each turn's context — both as a `[Pinned …]` prefix
  and as a structured `pinned_context` field a backend can hydrate full record
  bodies from (the backend hydration is a non-blocking follow-up).
- **Deep Consult** (`system.deep_consult`) — the on-demand **deep** analysis
  surface, alongside the chat Consult panel. Unlike Consult (which answers inline
  with no durable row), Deep Consult submits a **detached staged Dapr Workflow**
  (plan → acquire → analyze → synthesize) that runs minutes → hours and produces
  a lineage-walkable **finding** (plus optional facts/hypotheses). Submit POSTs
  `/api/v1/deep_consult` and gets a `task_id` back immediately (no 180s block);
  the panel then polls `GET /api/v1/deep_consult/{task_id}` until the status
  flips to `completed` (the synthesize stage wrote the finding) or `failed`, and
  links the produced `finding_id` into the lineage walk. Backed by
  `src/legba/data/registry/deep_consult_api.py`. (`personal`-only.)

### Source-first

The acquisition plane — operator surfaces for shared sources, their signals,
and the predicate fan-out. (Operator-category, `personal`-only.)

- **Source Registry** (`registry.sources`) — every shared `SourceDescriptor`,
  with the source-specific surface (kind / acquisition / scope / subscription
  policy / output subject) lifted onto the row. Reads
  `GET /registry/sources`. Per-row: expand to the descriptor body, create/edit
  via the inline `DescriptorEditor` (family `source`), and drive lifecycle
  transitions (`draft → configured → active ⇄ paused`) via
  `POST /registry/descriptors/source/{id}/transition`. "Open detail" fires
  `legba:open-source-detail` for the Source Detail panel.
- **Source Detail** (`source.detail`) — the per-source operator view: descriptor
  identity/scope/output summary, a cursor/health readout derived from the most
  recent published signal vs the descriptor's cadence (last published /
  staleness), the projected JetStream output-stream shape plus observed publish
  rate, and the recent published signals (the fan-out's first hop).
- **Subscription Builder** (`source.subscription_builder`) — composes a target's
  `SourceRef` (`src/legba/data/schemas/source.py`): an **explicit** named
  `source_id` *or* a `SourceSelector` predicate over source scope
  (tags/geo/languages/kinds/tenant + a Starlark residual), plus a signal-level
  `Subscription` filter. It validates client-side against the pydantic patterns
  and the exactly-one-of `SourceRef` invariant, previews "what it'd match" by
  resolving sources and recent signals, and emits copy-ready `SourceRef` JSON to
  paste into a target descriptor's `sources: [...]`.
- **Fan-out Explorer** (`source.fanout`) — walks `source → signal → finding`:
  hop 1 lists a source's signals (`GET /signals?source_id=`), hop 2 lists the
  findings whose `derived_from ⊇ {signal_id}` (`GET /findings`, joined
  client-side). Any node hands off to Lineage for the full bidirectional walk.

### Analysis (per target / per analyst)

Bound panels — each needs a descriptor binding and opens from the sidebar's
per-scope groups. The per-target set (T-series) and per-analyst set (A-series)
all read the frozen substrate-read endpoints and deep-link rows to Lineage.

Per target:

- **Target Overview** (`target.overview`) — the per-target home: descriptor
  metadata, the runtime actor roster (source cursors, `last_pulled_at`, error
  counts) from `GET /targets/{id}/runtime`, plus recent signals and findings.
- **Target Signals** (`target.signals`) — the raw signal table for the target's
  geo scope (signals are target-agnostic; the `target_id` filter resolves the
  target's `scope.geo`). Source/language filters, geo + entity-class + tag chips.
- **Target Findings** (`target.findings`) — the target's findings, severity-
  badged (nullable → "unrated") and sortable, with topic-tag chips.
- **Target Situations** (`target.situations`) — situations bucketed by lifecycle
  state (escalating / active / resolved), with intensity-derived severity and
  contributing-finding links (`GET /situations`).
- **Target Claims** (`target.claims`) — claim-like findings carrying
  `corroboration_score` + `corroboration_sources` (the corroboration component
  of signal/fact composite confidence), confidence-sorted with an evidence
  chain.
- **Target Map** (`target.map`) — a clustered MapLibre overlay of geocoded
  signals and severity-colored finding markers, with independent layer toggles,
  a per-country breakdown, and provenance-on-hover.
- **Target Timeline** (`target.timeline`) — a recharts banded view of signals /
  findings / situations over time, with situation lifecycles drawn as spans.
- **Target Graph** (`target.graph`) — the per-target lineage graph
  (`GET /lineage/{kind}/{id}?direction=both&depth=4`) with a depth slider,
  row-kind filter chips (orphan-safe re-parenting), and click-to-re-root.
- **Target Sources** (`target.sources`) — a per-source ingest-health rollup
  computed from the target's signals grouped by `source_id`, left-joined with
  the source registry for friendly name + handler kind.

Per analyst:

- **Analyst Runs** (`analyst.runs`) — a Runs / Outputs / Critiques three-tab
  view over `GET /analysts/{id}/{runs,outputs,critiques}`, each independently
  cursor-paginated.
- **Analyst Outputs** (`analyst.outputs`) — a live tail of this analyst's
  findings (`GET /findings?analyst_id=` + the `analyst.*.finding` NATS subject),
  with a severity histogram and run count.
- **Cross-target Analyst** (`analyst.cross_target`) — for `cross_target_raw` /
  `cross_analyst_correlator` kinds: per-target contribution counts and a
  contradiction-first correlation surface (`data.correlation_type` +
  `referenced_analyst_ids`).
- **Critic Scores** (`analyst.critiques`) — the critique history *of* this
  analyst's outputs (`GET /analysts/{id}/critiques`): per-rubric-axis bars, the
  overall trend, and the revision delta.

### Entity knowledge-graph

The entity substrate kept current by the `entity_resolution` deterministic
analyst (`entity_profiles` / `signal_entity_links` / `proposed_edges`).

- **Entities** (`system.entities`) — the entity roster:
  `GET /entities` (nodes, mention counts, geo) with search + class facet, and
  expandable rows showing each entity's recent signals (lineage-clickable) and
  co-occurrence relationships.
- **Entity Graph** (`system.entity_graph`) — a Cytoscape rendering of
  `GET /entities/graph`: nodes colored by `entity_class`, sized by mention
  count, the densest subgraph by default, click-to-ego-center.

### Registries (guided authoring)

Browse/search HEAD descriptors and author new ones. Two authoring surfaces sit
side by side: the **guided builder** (a form that produces a valid descriptor
body) and the **inline YAML editor** (the raw escape hatch). All four operator
registries are `personal`-only.

- **Target Registry** (`registry.targets`) — target descriptors via
  `GET /registry/descriptors?family=target&head_only=true`, with filter,
  expand-to-body, the guided `DescriptorBuilder`, the `StarterPicker`
  clone-and-edit, and the inline `DescriptorEditor`.
- **Analyst Registry** (`registry.analysts`) — analyst descriptors, same shape,
  surfacing `method.kind` + tools whitelist in the row summary.
- **Source Registry** (`registry.sources`) — see *Source-first* above.
- **Stack Registry** (`registry.stack`) — substrate stack components via
  `GET /registry/stack` (seven kinds: `llm_provider` / `vector_store` /
  `embedding` / `nats` / `postgres` / `redis` / `proxy_pool` +
  `nlp_service`). Credentials are never returned (vault refs
  resolve at call time). Stack components use the starter-clone surface (a
  separate API path), not the form builder.

Authoring components:

- `DescriptorBuilder` (`components/DescriptorBuilder.tsx`) — a guided form whose
  fields are hinted from the pydantic schemas (`src/legba/data/schemas/*`):
  types, patterns, enums, required-ness. It seeds from the family's starter
  skeleton (so every structural block is present and valid), overlays the
  operator's edits by dotted path, previews the body, and POSTs to
  `POST /registry/descriptors/{family}` — the registry's 422
  (`{error, message}`) surfaces inline. Supported families: `target`, `source`,
  `analyst`, `action_pack`.
- `DescriptorEditor` (`components/DescriptorEditor.tsx`) — the inline YAML
  editor (`create` → POST, `update` → PUT), with the same pydantic-422 gate.
  The `identity.version` field accepts the all-zeros sentinel; the registry
  stamps the real content hash at write time.
- `StarterPicker` (`components/StarterPicker.tsx`) — clones a working starter
  descriptor body into the YAML editor pre-filled (the "less raw" win for
  families without a form builder).
- `ScopePicker` (`components/ScopePicker.tsx`) — a descriptor dropdown sourced
  from the live registry, used in place of free-text id boxes.

### Eval + ops

The evaluation and operations surfaces (mostly `personal`-only).

- **Optimizer Candidates** (`system.optimizer`) — the GEPA prompt-module
  candidate queue (`GET /v3/optimizer/candidates`), with a promote/reject review
  action (`POST .../{id}/review`) that mints a new analyst descriptor version.
  The self-optimizer returns as a **scoped, measured** experiment: the live
  `unit_optimizer` runs over ONE bounded unit (`leadership_transition`), and each
  candidate carries a **real before/after paired faithfulness delta** measured on
  the same faithfulness verify judge (currently the core model, not cross-family; a recent live run: parent 0.34 → candidate 0.29,
  delta −0.05). It stays `promotion_gate=human_gated` and can **never**
  auto-promote on a degenerate, absent, or non-positive delta — promotion is the
  operator's click. The old monolithic `country_optimizer` is **cadence-frozen**
  (its descriptor is still `state=active`, but its cadence is nulled — no
  reminder-flood regression). The loop runs as a **Dapr
  Workflow** on the daprd sidecar; each row flags the **real** method it took
  (`dspy_gepa` vs a fallback like `naive_best_of_n` — a worker-less deploy
  silently runs naive search), amber-badging a non-`dspy_gepa` method so
  operators don't assume every candidate came from the full loop.
- **Prompt-Module Diff** (`system.optimizer.diff`) — candidate-vs-current
  prompt-module text diff before promotion (`GET /v3/optimizer/candidates/{id}/diff`).
  `current_text` is the parent-prompt snapshot the candidate's eval delta was
  measured against (captured on the candidate row at compile time), preferring
  the analyst's live promoted prompt when one exists. The route is
  snapshot-based and **never imports dspy** (the registry process stays
  dspy-free); a 404 only fires for an unknown candidate id, and candidates
  emitted before the snapshot field existed render an empty current side. Tier:
  `preview`.
- **Eval Scorecard** (`system.eval_scorecard`) — the measurement surface, three
  stacked sections (`panels/system/EvalScorecard.tsx`), each written to publish a
  no-skill / insufficient-sample result rather than hide it. All grouping / band /
  gate logic lives in `@/lib/evalOps` so it is unit-tested without a DOM.
  - **Skill scoreboard** (`GET /v3/eval/calibration`) — the honest top-line. Two
    legs, each behind its own honesty gate: the **exogenous Brier** (shows a
    number only when the exogenous sample is sufficient, else the verbatim
    `INSUFFICIENT exogenous sample (n_exo=k/N)`), and the **acute-forecast BSS**
    with a `ready` / `accumulating` / `degenerate` tag. The BSS number is shown
    **only** when the pilot is ready, non-degenerate, and the skill score is
    positive; a degenerate pilot reads `degenerate — skill claim withheld` and a
    ready-but-non-positive pilot reads `ready — no positive skill yet` — never a
    bare positive number. The acute pilot currently reports **no proven skill**
    (it accumulates toward n=30 and abstains on a degenerate probability vector),
    which the panel states plainly. Before any calibration finding exists the
    whole strip reads "no forecast / calibration pilot has been computed yet"
    (distinct from a failed pilot).
  - **Banded per-country scorecard** (`GET /v3/eval/country_scorecard`) — one
    honest card per active desk tagged `g20` **or** `watch`, each written by the
    deterministic `scorecard_producer` (the 12th OutputKind, `scorecard`). The
    roster is the 19 G20 country desks plus a high-consequence **watch** tier
    (Israel, Iran, Ukraine, Taiwan, North Korea — descriptor ids
    `country_watch_{il,ir,ua,tw,kp}`), 24 desks in all; the bands span **14
    days**, so a card integrates over that window rather than a single tick.
    Adding a country is register-a-target — the coverage tag alone cards it, no
    code. Each card shows a
    **band per dimension** (the four bounded units) derived by high-precision
    rules over already-verified sub-claims; clicking a band expands its **basis**
    — the verified sub-claim finding ids the band rests on, each a `RecordLink`
    that drills into the Inspector's cited card + the lineage DAG. A dimension
    with no qualifying verified claim renders an explicit
    `insufficient — <reason>` state (a muted, non-severity tone, no number, no
    drill target) rather than a fabricated band; a per-dimension aggregate
    faithfulness below the floor flags `⚑ low faithfulness`. Each dimension also
    carries its effective-confidence and a faithfulness + correctness eval badge,
    and the card footer links the P3 per-country **composition** node (or "no
    verified composition"). An empty list is a first-class "no scorecard computed
    yet" state. The live board is honestly a **mix**: some countries band on
    several dimensions while others read all-`insufficient` (e.g. the US, whose
    unit faithfulness is genuinely low) — the card shows that rather than
    manufacturing a band.
  - **Per-analyst critic rollup** (`GET /v3/eval/scorecard`) — "is this analyst
    getting better?": per-analyst rubric scores over time, the critic-judge
    overall trend chart, per-axis rubric bars, and ground-truth backtest accuracy
    where present (`buildScorecards` aggregates the dual-sink critique rows,
    worst-scoring analysts first). A 404 while the cross-analyst rollup is unwired
    degrades to an honest "endpoint pending" note pointing at the per-analyst
    Critiques panel — never an error.
- **Budget Ledger** (`system.budget`) — per-analyst tokens/runs/cost
  (`/budget/ledger`), the global per-bucket envelope (`/budget/envelope`), and
  budget-exhaustion demote events (`/budget/demotions`).
- **Governor Events** (`system.governor`) — the per-pack governor's BLOCK/ALLOW
  decision stream, BLOCK rows emphasised. The caps (rate / invocation / source /
  daily-cost + the global token envelope) are **live-enforced fail-closed** at
  the agency entry point (`Agency.run_pack_tool`); every decision lands a
  `governor_events` row and a best-effort `governor.events.>` publish. Two
  built-in paths drive real decision volume in production (A-3, live since the
  2026-06-10 cutover): consult routes its ReAct tool calls through the governed
  `substrate_read` pack, and the actor run path fires the `escalate_finding`
  pack when a finding crosses its severity/confidence gate. The
  `GET /registry/governor_events` read route is wired (the panel reads live
  decision rows).
- **Audit-Chain Browser** (`system.audit`) — the descriptor audit log
  (`GET /registry/audit`); each register/update/promote is Ed25519-signed and
  re-verified inline, with a chain-health banner and tamper highlighting.
- **Dead-letter Inspector** (`system.dead_letter`) — the DLQ projection
  (`GET /registry/dead_letter`) across the `descriptor` / `output` / `stack` /
  `discovery_resync` namespaces, with an inline-patch **resubmit**
  (`POST .../{id}/resubmit`), a since-window filter, and a resolved/unresolved
  toggle.
- **Actor Health** (`system.actor_health`) — the `actor_state` roster from
  `GET /v3/runtime/actors` (lifecycle, `last_run_at`, `last_outcome`,
  `cooldown_until`, error counts; polled every 5s). It first-classes the `source`
  actor kind (SourceActor) alongside target/analyst/discovery/consult, with a
  kind/lifecycle rollup and an expandable last-error inspector. **Runtime Actor
  Health** (`system.runtime`) read the same endpoint and was **hidden by #90 Wave
  A** as a dup (still in the bundle).
- **Consumer-Lag Monitor** (`system.stream_lag`) — per-consumer NATS JetStream
  lag (`GET /v3/streams/consumer_lag`): `num_pending` headline lag,
  redeliveries (poison messages), unacked backlog (slow consumers); polled
  every 5s.

### Product

The customer-facing surfaces, available in `personal` and `cis`.

- **Global Search** (`system.search`) — composed client-side (there is no
  backend `/search`): fans out in parallel to `/signals`, `/findings`,
  `/situations`, `/registry/sources`, merges into a common `SearchHit` with
  client-side ranking + facets; endpoints that 404 degrade gracefully. A hit
  click fires `legba:open-lineage`.
- **Alert Center** (`system.alert_center`) — operator-local alert subscriptions
  (scope × min-severity), fired by **polling** `GET /findings` and diffing
  successive polls (the first poll seeds without firing; alerts de-dup on
  finding id). Subscriptions persist to localStorage; each alert deep-links to
  Lineage.
- **Report Export** (`system.report_export`) — pick a slice of findings (+
  situations) and export four ways: a **STIX 2.1** bundle (report + indicator
  SDOs, `derived_from` → relationship SDOs, TLP markings), raw JSON, a
  severity-grouped markdown brief, and print → PDF.
- **Tenant View** (`system.tenant_view`) — **hidden by #90 Wave A** (multitenancy
  is not product-baked; the build is ingestion-only single-tenant). Still in the
  bundle. An **operator-convenience** rollup that groups HEAD descriptors
  (target / source / analyst) by the descriptor
  `owner` field, drills into a selected owner's roster, and broadcasts the
  active owner via `legba:set-tenant` for any panel that opts into owner
  scoping. It is a **client-side UI grouping, not a multi-tenant isolation
  boundary** — there is no per-tenant access control behind it (Legba ships
  single-tenant; see `docs/DIRECTION.md` §0). Do not read it as enforced
  tenancy.

### Cross-target dashboard

- **Target Roster** (`system.targets.roster`) — the cross-target inventory: every
  registered target with its source-first scope (geo countries, domain, languages,
  entity-class footprint) surfaced inline, from
  `GET /registry/descriptors?family=target&head_only=true`. **Hidden by #90 Wave
  A** — collapsed into the **Target Registry** (`registry.targets`) so there is a
  single Targets panel; the registry row points the Monitoring preset at
  `registry.targets`. See *Consolidated / hidden by #90* below.

### Consolidated / hidden by #90

The #90 Wave A consolidation moved seven panel kinds into `HIDDEN_KINDS`
(`registry.ts`). They are **present in the bundle, not deleted** — saved layouts
and deep links that reference them still resolve — but they no longer appear in
the default nav, the command palette, or the singleton boot list. (The lone
genuine deletion in #90 was the `v4.feed` rail and its `LiveFeed.tsx` /
`FeedPanel.tsx` components, subsumed by `system.findings`.)

| Hidden kind | Why | Where the function lives now |
| --- | --- | --- |
| `registry.discovery` | dead — 0 descriptors carry a `discovery` block | — |
| `system.backfill` | hard-disabled honest-501 stub (also `preview`) | tracked SEAM — registry→runtime proxy |
| `system.targets.roster` | one Targets panel | **Target Registry** (`registry.targets`) |
| `v4.case` | Casework Board shelved — no pin entry points wired | — |
| `system.tenant_view` | multitenancy not product-baked (ingestion-only) | — |
| `v4.assessment` | world assessment is a FINDING (opens in the Inspector); its component was re-pointed to the bounded-unit read column but the panel stays out of the default nav | **Inspector** (world one-pager) · bounded-unit reads still rendered by `WorldAssessment.tsx` |
| `system.runtime` | same endpoint as Actor Health (a dup) | **Actor Health** (`system.actor_health`) |

### Cross-panel events

Panels coordinate through custom DOM events rather than route coupling, so any
panel can drive another without knowing whether it's mounted:

| Event | Fired by | Consumed by |
| --- | --- | --- |
| `legba:open-lineage` | Findings, target/analyst panels, Search, Alerts, Fan-out | Provenance Lineage |
| `legba:open-source-detail` | Source Registry, Fan-out | Source Detail |
| `legba:open-optimizer-diff` | Optimizer Candidates | Prompt-Module Diff |
| `legba:open-entity-graph` | Entities | Entity Graph |
| `legba:set-tenant` | Tenant View | tenant-scoped panels |

---

## 4. Data access & the auth chain

### REST client

`lib/api.ts` is a thin REST client against `/api/v1`. `apiGet` / `apiPost`
attach an `Authorization: Bearer <token>` header **only if** a token exists in
`localStorage.legba_token`; errors surface as `ApiError` carrying the status and
parsed body. Most panels read through `@tanstack/react-query` for caching and
refetch. In the canonical production deployment **localStorage is empty** and
the SPA sends no `Authorization` header — see the perimeter below.

### Live updates

`lib/ws.ts::subscribeRegistryEvents(filter, onEvent)` opens a WebSocket to
`/api/v1/registry/events?filter=<subject>&token=<t>` (the registry's NATS event
multiplexer), with 1→2→4→…→30s reconnect backoff. The registry hook and the
live-tail panels (Findings, Analyst Outputs, Governor Events) subscribe through
it. The WS resolves `ws://` vs `wss://` from the page protocol.

### The two-layer auth chain

In production the SPA is served by **`legba-caddy`** (`docker/Caddyfile`),
which fronts the site at `$LEGBA_PUBLIC_DOMAIN` (TLS via Let's Encrypt) and the
`legba-registry` upstream. Auth is two layers:

1. **Perimeter — Caddy `basic_auth`.** The browser prompts once per session;
   the password is checked against a bcrypt hash from `LEGBA_BASIC_AUTH_HASH`
   (read from the gitignored `.env`, never committed). This gates the SPA bundle
   itself and every API path except the WS event stream.
2. **Bearer injection.** After `basic_auth` validates, Caddy's
   `header_up Authorization "Bearer {$LEGBA_REGISTRY_API_TOKEN}"` *replaces* the
   inbound `Authorization` with the registry's bearer for the upstream. **The
   SPA never knows the registry token** — it sends no `Authorization` header, so
   the browser's cached Basic credential flows through cleanly and Caddy swaps in
   the Bearer.

**WebSocket exception.** The `new WebSocket()` API can't attach an
`Authorization` header, so a `basic_auth` gate on the WS path would cause a
401 → re-prompt loop on every reconnect. The Caddyfile therefore routes
`/api/v1/registry/events*` through a handle block that **bypasses
`basic_auth`** and injects the Bearer directly. `events_ws` reads that
Caddy-injected `Authorization: Bearer` header to authenticate the upgrade (§2.5;
it also still accepts a `?token=` query param for non-Caddy / direct access).
Security is preserved because only requests through this Caddy get the registry
token, so the registry rejects any external direct hit.

> **Gotcha.** If a stale `legba_token` is left in `localStorage` (e.g. from a
> dev session that pointed `auth/jwt.ts::setToken` at the registry's
> `LEGBA_REGISTRY_API_TOKEN`), the SPA will send `Authorization: Bearer <stale>`,
> which collides with Caddy's `basic_auth` (Basic vs Bearer mismatch) and
> triggers a re-prompt loop. Fix: `localStorage.removeItem('legba_token')` and
> hard-refresh. The StatusBar shows `auth: ok` when a token is present,
> `auth: dev` otherwise.

### Dev server

`npm run dev` (Vite, port 5174) proxies `/api` and `/ws` to the registry
(`VITE_API_BASE`, default `http://localhost:8501`) so there's no CORS and no
Caddy in the loop. The registry in dev accepts any token (or none).

---

## 5. Building & running

| Command | Effect |
| --- | --- |
| `npm run dev` | Vite dev server on `:5174`, proxying to the registry |
| `npm run build` | `tsc -b && vite build` → `dist/` |
| `npm run preview` | Serve the built bundle locally |
| `npm run lint` | `tsc --noEmit` type-check |
| `npm run test` | `vitest run` (the DOM-free model logic in `lib/*` is unit-tested) |

In production, the `legba-ui-build` one-shot job builds `dist/` into the
`legba_ui_dist` volume, which `legba-caddy` mounts read-only and serves with an
SPA fallback (`try_files {path} /index.html`). Key libraries: `dockview` /
`dockview-react` (workspace), `@tanstack/react-query` (data), `recharts`
(timeline/charts), `maplibre-gl` (maps), `cytoscape` + `react-cytoscapejs`
(graphs), `react-markdown` (consult/reports), Tailwind (styling). See
`RUNBOOK.md` for deploy and ops procedures.

---

## 6. Panel tiers

Every panel kind carries a `tier` in its registry definition
(`PanelKindDefinition.tier`, `panel-registry/registry.ts`), surfaced as a small
**Preview** badge in `PanelChrome`'s header (threaded via `PanelTierContext` so
panels don't each re-declare it):

- **`live`** (default) — a product surface backed by a real, wired route.
- **`preview`** — registered and usable, but either a guarded-preview /
  honest-pending backend or an operator-experimental surface. The current
  preview set:
  - **Backfill Replay** (`system.backfill`) — `preview` *and* hidden by #90 (it
    is no longer in the default nav). The trigger button is disabled
    ("backend not exposed"): the registry-side `POST /registry/targets/{id}/backfill`
    is an honest **501**, because the P-12 catch-up replay
    (`Backfiller.catch_up_and_forward`, `runtime/subscription/backfill.py`) is a
    runtime-plane operation not reachable through the registry API in this build
    (cross-plane loopback, no Caddy route). Wiring a registry→runtime proxy is a
    tracked follow-up.
  - **Prompt-Module Diff** (`system.optimizer.diff`) — operator review aid over
    the GEPA loop; the `/diff` route is wired (snapshot-based, no dspy).
  - **Global Search / Alert Center / Report Export** — client-only product
    surfaces with no dedicated backend route yet. (**Tenant View** is also in this
    `preview` cohort but is now hidden by #90 — see *Consolidated / hidden by #90*.)

## 7. Future seams

The authoritative list of declared seams is `docs/SEAMS.md`. UI-relevant entries:

- **Backfill trigger route** — see the Backfill preview note above (registry
  `POST .../backfill` is an honest 501; the button is disabled, not faked).
- **A2A skill surface** (SEAMS #15) — operator-gated OFF by default
  (fail-closed); when disabled the runtime answers `/a2a/skills` with a 503 +
  enable recipe (never a silent 404). No UI panel today.
- **Source health** — Source Detail derives staleness from the latest signal vs
  cadence; a dedicated source-health endpoint is a later add.

Hidden-by-default kinds (Global Pulse, Users, NATS-tail, wiring/mutations
editors, dynamic dashboard) stay registered so saved layouts resolve, but are
kept off the daily-driver surface until they carry real data.

---

See also: `ARCHITECTURE.md` (planes, registry, runtime) · `ACQUISITION.md`
(sources, signals, fan-out) · `ANALYSIS.md` (analysts, findings,
hypotheses) · `AI_MODELS.md` (consult engine, models) · `RUNBOOK.md`
(deploy + ops).
