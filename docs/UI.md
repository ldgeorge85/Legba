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

- **Sidebar** (`components/Sidebar.tsx`) — the ONE grouped navigation tree
  (S7-T2): a Search (⌘K) launcher, a compact **Layouts** menu (named presets +
  save/restore), then five collapsible **verb-grouped sections** the analyst
  scans top to bottom — **Awareness** (what's happening now) / **Investigation**
  (dig into the why) / **Analysis** (reason over it) / **Products** (the
  finished intelligence) / **Operations** (the plumbing) — followed by the
  per-**Target** and per-**Analyst** instance groups. Clicking a row
  opens/focuses that panel in the workspace. Operations and the registry-scale
  Targets/Analysts groups start collapsed so the first screenful stays the
  five-group tree.

  A panel's group comes from an explicit per-kind override, else a prefix
  fallback on its kind segment (`registry.`/`source.`/`system.` → Operations,
  `v4.` → Investigation), so a new panel kind auto-slots with no nav edit
  (`panel-registry/navGroups.ts`).

  The Targets/Analysts instance groups render the runtime registry's rows
  **UNION a synthesized bound-panel set** (P0-2f, `panel-registry/synthesize.ts`):
  the live `ui_panel_registrations` surface is empty (no descriptor declares
  `outputs.ui_panel`), so the groups are minted from the live descriptor heads
  (`GET /registry/descriptors?family=target|analyst&head_only=true`) — one
  synthetic registration per (record × bound panel kind). Real registry rows
  stay authoritative: a synthetic row whose panel instance is already covered by
  a live, non-retired registration is dropped (`mergeRegistrations`).
- **Workspace** — a `DockviewReact` instance (`dockview-theme-abyss`). Each
  tile is a `LegbaPanelComponent` frame that resolves its bound
  `PanelRegistration` (or a synthetic singleton registration) to a lazy-loaded
  React component.
- **StatusBar** (`components/StatusBar.tsx`) — footer showing the active
  deployment mode badge, the registered-panel count, the last registry refresh
  time, the auth state (`auth: ok` / `auth: dev`), any registry-load error, and
  (A10) the **export-basket chip** — a count of collected items that opens the
  Report Export panel; it only renders when the basket is non-empty.

Panel components are **code-split** (`React.lazy`) and suspend behind a
"Loading panel…" fallback, so the initial bundle stays small and a panel's
chart/map libraries load only when its tile is first opened.

Two shared hooks kill the blank-surface classes measured surfaces hit inside
Dockview (P0-2f):

- **`useDockviewTileRedraw`** (`components/useTileRedraw.ts`) — Dockview keeps
  inactive tab content mounted but hidden, so a WebGL map (maplibre) or a
  measured chart (recharts `ResponsiveContainer`) that mounts into a hidden
  tile initializes against a zero-size box and stays blank on activation. The
  hook subscribes to the tile's own panel api
  (`onDidVisibilityChange` / `onDidDimensionsChange`), calls the redraw
  callback (`map.resize()` / `fitView()`) a frame after the tile becomes
  visible, and bumps a tick usable as a `key` to remount mount-time-measuring
  components.
- **`useElementWidth`** (`lib/useElementWidth.ts`) — container-width
  measurement with the **callback-ref** pattern, so the ResizeObserver follows
  the element, not the mount: a panel that renders a loading empty-state first
  has nothing in the ref slot when a `[]`-deps effect runs, and the width would
  stick at 0 forever (the `system.timeline` first-mount blank). The callback
  ref re-observes on every attach/detach.

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

`Ctrl/Cmd-K` toggles the **command palette** (`components/CommandPalette.tsx`)
— the record-jump gateway. A single fuzzy query resolves across four indexed
families, ranked by recents → favorites → the rest:

1. **Records** — targets / analysts / sources from the live registry
   (`usePaletteRecords`). `Enter` opens the record's bound primary panel
   (target → Findings, analyst → Outputs, source → Detail);
   `⌘/Ctrl-Enter` selects it into the Inspector instead.
2. **Panels** — every singleton panel kind (including the hidden-but-registered
   set, which stays off the sidebar but is discoverable here).
3. **Presets** — the named layout workspaces, one fuzzy match away.
4. **Actions** — "Investigate · <target/analyst>" entries that open the bound
   analysis grid (the same machinery as the sidebar's instance groups).

Arrow keys move the selection, `Esc` (or a backdrop click) dismisses; a leading
star toggle persists favorites to localStorage.

### Boot layout

On first workspace-ready, in `personal` and `cis` modes the shell seeds the
**mission-control grid** (S7-T2 — the first screenful is the glance state plus
the product):

```
┌───────────────────────────────────────────────────────────┐
│  KPI STRIP (v4.kpi) — signals/findings/situations/sources │
├──────────────────┬───────────────────┬────────────────────┤
│  Live Feed       │  World Map        │  World Assessment  │
│  (system.        │  (v4.map)         │  (v4.assessment,   │
│   findings)      │  at real size     │   the REPORT)      │
├──────────────────┤                   │  + Inspector       │
│  Timeline lanes  │                   │    tabbed behind   │
│  (v4.timeline)   │                   │                    │
└──────────────────┴───────────────────┴────────────────────┘
```

The KPI glance strip spans the full width up top; the **Live Feed** anchors the
left column with the global **Timeline lanes** beneath it; the **World Map**
takes the center at real size; and the verified **World Assessment** report is a
first-class right panel with the **Inspector** tabbed behind it. Everything is
brushed by the one shared selection store, and `sizeMissionControl` pins the
proportions after seeding. The Live Feed and the Inspector are pinned
**non-closable anchors** (a close-button-less tab). `cis` boots the same grid —
every panel in the seed ships in both `personal` and `cis`.

(The Live Feed anchor is `system.findings` — the unified findings+signals feed
described in §3 *Daily driver*. The former separate `v4.feed` rail was deleted
in the #90 feed merge; `system.findings` subsumed it wholesale.)

### Layout presets & custom layouts

The sidebar's **Layouts** menu (`lib/layoutPresets.ts`) re-seeds the
workspace with a named arrangement. Seven presets ship (the redesign swapped the
former Lineage slot for the keystone Inspector and added a Zen focus mode; the
#90 redesign added the **Workspace** intel-desk preset; P1-7 added the optional
**Wall** preset — the default boot grid is unchanged, the operator opts in):

| Preset | Panels |
| --- | --- |
| **Wall** | The Wall (`system.wall`) with the Inspector riding right, so the Wall's finding / desk / situation rows have somewhere to land |
| **Monitoring** | Live Feed · Inspector · Target Registry · Alert Center |
| **Workspace** | Live Feed · Inspector · Consult · Why (the 2×2 intel desk, all brushed by the shared selection) |
| **Investigation** | Live Feed · Inspector · Entities · Global Search |
| **Analysis** | Optimizer · Eval Scorecard · Consult · Deep Consult |
| **Operations** | Actor Health · Dead-letter · Stream Lag · Governor Events |
| **Focus (Zen)** | Inspector alone, full canvas — an undistracted single-record read |

(Monitoring's third tile is the canonical **Target Registry** — the former
`system.targets.roster` was collapsed into it in #90 Wave A and deleted outright
in S7-T2, so the preset points at `registry.targets`.)

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
single-operator daily driver (the widest panel set); `cis` is an alternate,
narrower panel-set lens. **It is NOT a
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
resolvable so deep links into them don't 404. The hook also fetches the target /
analyst descriptor heads and **unions in the synthesized bound-panel
registrations** (P0-2f, `synthesize.ts` — see §1 Sidebar): the live
`ui_panel_registrations` surface is empty, so without synthesis every bound
panel kind was sidebar-unreachable; real rows stay authoritative, and a registry
event re-runs the descriptor fetch so the synthesized groups track it.

A handful of panel kinds are registered but **hidden** (`HIDDEN_KINDS` in
`registry.ts`): they stay in the bundle so saved layouts referencing them still
resolve, but don't surface in the sidebar (the ⌘K palette still lists them).
The old DROP/consolidation cohorts (Global Pulse, Users, NATS-tail,
wiring/mutations editors, dynamic dashboard, Discovery, Backfill, Targets
Roster, Casework, Tenant View, Runtime Actor Health) were **deleted outright**
in the S7-T2 shell reform — those kinds no longer exist. What remains hidden
today are live panels merged under a peer or reachable via ⌘K only — see
*Consolidated / hidden / deleted* below.

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
  `cis`, is tabbed into the boot grid's right rail (behind the World Assessment
  report), and rides the Wall / Monitoring / Workspace / Investigation / Focus
  presets.

  For a finding the Inspector renders the **cited read card**
  (`components/inspector/CitedAssessment.tsx`) at the top — this is the drillable
  product read. It is built from the shared **reading kit** — `CitedProse` (the
  ONE prose renderer: markdown always rendered, `[N]` / `[[ref:N]]` markers
  tokenized into interactive chips) and `VerdictBadge` (below). The report prose
  renders with its inline markers turned into clickable citation chips: a chip
  scrolls to and flashes the matching row in the **Evidence** panel below, whose
  title is itself a `RecordLink` into the cited signal (so a claim drills to its
  source). A header strip carries the citation count, the `VerdictBadge`, and
  the per-unit **eval badge** (`UnitEvalBadge`, below). A legacy / uncited
  finding degrades honestly: its prose renders plainly under an explicit
  "uncited (legacy finding)" marker with no fabricated anchors and no empty
  evidence panel.

  The **VerdictBadge** (`components/VerdictBadge.tsx` + `lib/verdictModel.ts`)
  is the ONE verification dialect, ICD-203 aligned: two muted chips kept
  separate exactly as ICD-203 keeps the axes separate — **L** (likelihood: the
  finding's probability mapped onto the seven-point verbal scale, `unstated`
  when none is recorded) and **C** (analytic confidence: Low / Moderate / High
  derived from the faithfulness-verify pass + judge status + citation breadth,
  `unverified` when no verify block exists). A `?` legend affordance opens the
  ICD-203 tables in place. Two honest verify-exempt states (P0-4 / C2b): a
  deterministic structural analyst's finding renders **`unverified —
  structural`** (it never enters the faithfulness pass — the client mirrors the
  server's `STRUCTURAL_VERIFY_EXEMPT_ANALYSTS` registry for live-tail rows),
  while a structural finding whose asserted quantities were deterministically
  **re-derived from its own lineage and matched** is stamped
  `verify_exempt: "structural-verified"` by the reads API and classified apart
  (`isStructuralVerified`) — a miscount becomes a flagged critique instead.

  **Citation-chip hover verdict card** (P1-8, `CitedProse.tsx` +
  `lib/claimVerdicts.ts`): hovering (or focusing / tapping) a resolved citation
  chip opens a card with the source, the **cited passage** (or an honest "cited
  passage not recorded"), the citation's credibility, and the **per-claim verify
  verdict** for that chip's ordinal, derived from the verify block's
  `unsupported_spans` — the only per-claim record the verify pass persists in
  the finding body. A flagged claim shows its honesty-vocabulary label
  (contradicted / unsupported / hedge-laundering / …) plus the flagged claim
  text; when the LLM judge ran and nothing names this chip the card says "not
  flagged" with the pooled `supported/checkable` context (a positive per-claim
  "supported" is not recorded per-chip, and the card never claims one); a
  floor-only or legacy row reads the explicit `claim-level verdict not
  recorded`. Nothing is fabricated.

  Two selection-origin affordances ride the Inspector header: **add to export**
  (A10) drops the selected finding into the collection basket
  (`@/state/exportBasket` — see Report Export), and **Watch this** (P5-6, shown
  for a selected entity) creates a server-side entity watch via
  `POST /v3/watchlist` in one click, with honest watching / watched / failed
  states — verified hits then alert through the shared dispatcher.

  The **UnitEvalBadge** (`components/inspector/UnitEvalBadge.tsx`, off
  `GET /api/v1/eval/scores`) shows a bounded reasoning unit's honest eval — a
  server-composed string like `verified | faithfulness 0.45 | unmeasured (0
  labels)` rendered verbatim (the "no invented number" contract lives on the
  server). It renders **nothing** when the analyst id is not a bounded unit, the
  scorer has never run, or the fetch fails — a non-unit finding gets no badge
  rather than a fabricated one.

### The Wall & the Timeline (awareness surfaces)

- **The Wall** (`system.wall`) — the mission-control anchor tile (P1-7,
  `panels/system/Wall.tsx`): one glanceable screen, four quadrants, each
  clipping and scrolling its own overflow. Opened from the sidebar (Awareness)
  or the optional **Wall** preset — the default boot grid is unchanged.
  - **World at a glance** — a compact per-desk band grid over the SAME data as
    the World Map's banded-verdict choropleth (`useCountryVerdicts` +
    `CONFIDENCE_FILL`), so grid chip and map band never disagree; a chip click
    selects the desk into the Inspector. Honest empty state: "no verified
    country compositions yet".
  - **Movers since last visit** — `GET /v3/since` (the "what changed since"
    diff API) with a **client-owned cursor** (`localStorage.legba_wall_cursor`;
    first-ever open = 24h lookback). Band changes first (direction-colored),
    then the superseded-reversal count, then situation lifecycle edges. The
    cursor resolves once per mount and advances to each response's
    `server_now` (never backwards), so the next open diffs from the last
    moment the Wall was live. All non-DOM logic in `lib/wallModel.ts`.
  - **Newest high-severity verified** — top 5 of the since-window's verified
    findings (verified-only server-side), severity-badged, each row selecting
    into the Inspector.
  - **System health** — a rollup over the System Status routes
    (`/v3/system/source-firing`, `/v3/system/analyst-cadence`): signal volume,
    sub-hour source liveness, stale analysts, source errors — each number
    stamped with a **ProvenanceBadge** (`live|fallback|absent`, below).

  Ships in `personal` and `cis`; Awareness nav group.
- **Timeline** (`system.timeline`) — the validity-window temporal view (P4-4,
  `panels/system/Timeline.tsx`): the temporal substrate (facts with
  `[valid_from, valid_until)`, situations with a lifecycle window, findings
  with `[produced_at, superseded_at)` + supersession chains) had no temporal
  view — this is it. Ranged items on three lanes (facts / situations /
  findings) over a brushable, zoomable time axis (ms → months), with
  **supersession-sequence connectors** between finding versions; an OPEN window
  (server `end=null`) draws live to the right edge with a dashed cap — never a
  fabricated close. Built as a lightweight custom SVG (not vis-timeline) with
  all shaping pure in `lib/timelineWindows.ts`; width measurement uses the
  shared `useElementWidth` callback-ref hook (the first-mount-blank fix,
  below) PLUS `useDockviewTileRedraw` (keying the measured div on its tick) —
  the callback ref alone still stuck at width 0 when the tile mounted hidden
  (a background tab, or a tile Dockview hadn't laid out yet) and its
  ResizeObserver missed the later hidden→visible transition; the tile-redraw
  tick forces a re-measurement once the tile actually becomes visible. Data:
  `GET /api/v1/v3/timeline?target_id=&days=` (added for this
  panel). Desk-scoped to the unified selection's target; click a bar →
  `selectRow` into the Inspector. Ships in `personal` and `cis`; pinned to the
  Investigation nav group. (Distinct from the `v4.timeline` KPI-strip lanes in
  the boot grid.)

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
  The panel (`panels/system/Journal.tsx`, the "Voices" cut) reads
  `GET /api/v1/journal` summary-weight for the list plus
  `GET /api/v1/journal/{id}` full-weight on row-select, and surfaces:
  - **A filter rail** — entry-kind chips (Journal / Consolidation / Chronicle,
    plus any lens kinds actually present) with live row counts and a
    verify-score pill; multi-select, all-selected by default.
  - **The latest consolidation** prominently above the list — "Legba's current
    inner landscape" (the single open `entry_kind='consolidation'` row). With
    none yet, an empty-state note explains the consolidation opens once enough
    entries accumulate.
  - **A grouped-by-cycle collapsed list** — rows bucketed by their `period_end`
    date, newest cycle expanded, synthesized rows (consolidation / chronicle /
    lenses) leading each group and the high-volume diary entries capped behind
    a reveal (group headers label the bucket date — daily buckets are no longer
    mislabeled "week of").
  - **A reader pane** — selecting a row fetches the full entry and renders it
    with the shared reading kit; when the row's verify body names contested
    spans (`[judge_contradicted]` / `[judge_unsupported]`) a compact per-claim
    **verdict block** renders them as flagged chips — the operator's window
    into what the verify pass actually disputed. Entry cards also carry the
    A10 **add to export** action into the collection basket.
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
  raises. Ships in `personal` and `cis`; Products nav group.

  The journal's outward changes — a correction, a `change`, or a `self_revision`
  (including to its own instructions) — go to the **human-gated
  `journal_proposals` queue**, never a live table; the backend serves
  `GET /api/v1/journal_proposals` plus
  `POST /api/v1/journal_proposals/{id}/{accept,reject}` (accept runs an
  idempotent per-kind apply; a `self_revision` touching a protected section
  auto-rejects). A dedicated operator **review surface** for that queue is **not
  yet wired in the console** (the former Mutations Queue panel was deleted in
  S7-T2); the journal-proposals review panel is a tracked follow-up.

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
  into the **Operations** section (`navGroups.ts`, `system.*` → Operations) with
  no nav edit. Ships in `personal`. (Panel registered at
  `panel-registry/registry.ts` as `system.status` → `SystemStatus`; routes in
  `src/legba/data/registry/v3_api.py`, `build_v3_router`.) The standalone
  **Consumer-Lag Monitor** (`system.stream_lag`) was rolled into this
  at-a-glance view and is hidden from the sidebar (still ⌘K-reachable).

### v4 visual workspace

The "three rooms" visual surface — World / Flow / Why — all selection-linked
through the same store. The boot anchor is no longer a `v4`-namespace feed: the
former `v4.feed` rail was **deleted** in the #90 feed merge and replaced by the
unified `system.findings` Live Feed (see §3 *Daily driver*). Every panel in
this group ships in `personal` and `cis`.

- **World Map** (`v4.map`) — the default is a **maplibre-gl banded-verdict
  choropleth** (`v4/world/MapLibreWorldMap` + `countryVerdicts`), which shades each
  country desk by its scorecard/verdict band; the **Leaflet** map
  (`v4/world/LeafletWorldMap`) is the fallback when `hasWebGL` is false. Both carry
  a layer panel, time scrubber, KPI strip, and a selection drawer; the boot grid
  gives it the center column. A map click selects into the store. The P4-3
  deepening, layered bottom → top:
  - **Choropleth hover + click** — hovering a desk shows a popup with its band,
    faithfulness, and windowed activity (top movers); clicking selects the desk
    into the Inspector. Popups are themed to the dark chrome (E-5).
  - **Signal density** — a light maplibre heatmap (*Heat*), or a richer
    **deck.gl `HexagonLayer`** (*Hex*) camera-synced over the basemap. deck.gl
    is **dynamically imported** only when a deck layer is first turned on — the
    map opens without paying for it (data shaping pure in `lib/mapLayers.ts`).
  - **Co-mention arcs** (deck.gl `ArcLayer`) — countries a single signal
    jointly references, from the `/signals` `geo[]` column. Honest empty: the
    geo backfill stamps single countries, so `countries.length >= 2` never
    holds on the live substrate today and the arc layer is **honest-empty** —
    arcs appear the moment multi-country geo lands upstream.
  - **Geo-convergence markers** — the A7 deterministic cross-stream
    correlator's active bins, fetched as recent `geo_convergence`-channel
    alerts through `GET /v3/since?channel=geo_convergence`
    (`v4/world/convergenceData.ts`, bounded 7-day lookback); a bin with no
    parseable placement gets **no marker**, never a fabricated one.
  - **Watch locations** — operator "watch here" points + radius rings
    (`lib/watchLocations.ts`: localStorage-persisted, haversine proximity,
    100–1000 km radius options, 24-point cap); proximate signals are haloed.
    Client-local — distinct from the server-side Watchlist panel.
  - **Time scrubber** (`v4/world/TimeScrubber.tsx` + `lib/mapTime.ts`) — a
    **dual-thumb range slider** over the window that actually filters what the
    map renders (both ends adjustable), with span presets and a play control
    that advances the window end until it reaches LIVE.
- **Flow Canvas** (`v4.flow`) — The Flow: the live registry canvas over
  sources → targets → analysts → packs (`v4/flow`), with NiFi-style live
  telemetry. The node highlight reads `useSelection` (its former local
  `selectedNodeId` was retired into the store). **Edge-density gate** (P0-2f,
  `v4/flow/flowState.ts`): the predicate fan-out (`analyst_target`, one edge
  per analyst × matched target) is default-hidden, and any edge kind past
  `DENSE_EDGE_COUNT_THRESHOLD` (400) starts hidden too — the layer toggles
  state the real per-kind counts, so nothing is silently thinned; the operator
  can switch any kind back on.
- **KPI Strip** (`v4.kpi`) — the boot grid's full-width top strip: signal /
  finding / situation / source counts with band-change deltas, the glance
  state the mission-control layout leads with.
- **Timeline** (`v4.timeline`) — the global recharts Timeline lanes seeded
  beneath the Live Feed in the boot grid (event dots per lane; distinct from
  the `system.timeline` validity-window view above).
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
  clickable acquisition source. Seeded by the Workspace preset (the boot grid's
  right rail is the World Assessment + Inspector instead).

- **World Assessment** (`v4.assessment`) — the reading surface for a
  composition, **un-hidden by S7-T2** and now the boot grid's right-rail REPORT
  panel (Products nav group; `v4/why/WorldAssessment.tsx`). WORLD mode (no
  selection): the `world_assessor` one-pager — the composed, verified world
  view — as a calm centered reading column. DESK mode (a country selected):
  the desk **Intelligence Card** (S7-T3), reading top-to-bottom as a finished
  product — banded score + delta → BLUF → the verified `country_composition`
  (expanded) → the per-desk bounded **unit cards** (`CountryUnitsAssessment`,
  each carrying its `UnitEvalBadge`) → related → history (older/superseded runs
  collapsed). Both modes render through the shared reading kit and offer a
  client-side Download (.md / print → PDF). The former **Casework Board**
  (`v4.case`) was deleted outright in S7-T2 (shelved — no pin entry points were
  ever wired).

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
  provenance into Lineage. Finding rows carry the A10 **add to export** context
  action (findings only) into the collection basket — the same basket the
  Inspector button and Journal cards feed, counted by the status-bar chip.
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
- **Subscription Policy** (`source.subscription_policy`) — the operator surface
  for a source's `subscription_policy` gate (`open` / `allowlist` / `grant`),
  enforced at subscription registration on the control plane; manages the
  per-(source, target) `subscription_grant` wirings.
- **Fan-out Explorer** (`source.fanout`) — walks `source → signal → finding`:
  hop 1 lists a source's signals (`GET /signals?source_id=`), hop 2 lists the
  findings whose `derived_from ⊇ {signal_id}` (`GET /findings`, joined
  client-side). Any node hands off to Lineage for the full bidirectional walk.

(The Subscription Builder, Subscription Policy, and Fan-out Explorer are in the
hidden set — niche source-config surfaces reachable via ⌘K, off the sidebar.)

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
- **Notable Structure** (`system.notable_structure`) — the ranked `interesting`
  shortlist the graph-analysis handlers (structural_balance + graph_mining)
  distil every run (`GET /graph/structure`): tense actors, brokers, new-hostile
  edges, sign-imbalanced triads, proxy chains, each with rationale + score.
  Selection-aware — a selected country/entity scopes and prioritises matching
  items.

### Registries (guided authoring)

Browse/search HEAD descriptors and author new ones. Two authoring surfaces sit
side by side: the **guided builder** (a form that produces a valid descriptor
body) and the **inline YAML editor** (the raw escape hatch). All operator
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
- **Action-Pack Grants** (`registry.action_packs`) — the action-pack descriptor
  family (`GET /registry/action_packs`): tools / channels / tags / governor
  caps per pack, plus the effective-capability view (the three-way
  `analyst ∩ target ∩ pack` intersection the governor gates).
- **Model Stack Settings** (`system.settings`) — model-component configuration
  + the first-run wizard. The runtime source of truth for LLM / embedding / NLP
  endpoints is the `stack_components` registry, NOT `.env` (which only seeds
  those rows at bring-up); this panel reads/writes those rows
  (`GET /registry/config/status`, `GET/POST/PUT /registry/stack`) and routes
  credentials to the vault (`POST /registry/vault/secrets`).

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
  `preview`; hidden from the sidebar — opened from Optimizer Candidates (or ⌘K).
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
    (distinct from a failed pilot). Displayed calibration numbers carry the
    P4-5 **ProvenanceBadge** (`live|fallback|absent`, below) so a real-live
    figure is never confused with an honest empty. (The route also carries the
    P2-3 additive `band_calibration` section — band transitions logged as
    resolvable claims and graded at 14/28-day horizons as persistence /
    reversal rates — which the panel does not yet render; a follow-up surface.)
  - **Banded per-country scorecard** (`GET /v3/eval/country_scorecard`) — one
    honest card per active desk tagged `g20` **or** `watch`, each written by the
    deterministic `scorecard_producer` (the 12th OutputKind, `scorecard`). The
    roster is the 19 G20 country desks plus a 13-country high-consequence
    **watch** tier (Israel, Iran, Ukraine, Taiwan, North Korea, Pakistan, plus
    the escalation-risk band Sudan, Mali, Burkina Faso, Niger, DR Congo,
    Myanmar, Haiti), **32 desks** in all; the bands span **14
    days**, so a card integrates over that window rather than a single tick.
    The thematic supply-chain desks are deliberately **not** carded here — the
    scorecard enumerates `g20`/`watch`-tagged targets only, so a lane desk has no
    row rather than an empty one.
    Adding a country is register-a-target — the coverage tag alone cards it, no
    code. Each card shows a
    **band per dimension** (the seven broad bounded units — the eighth,
    `proliferation_watch`, is deliberately NOT a fixed scorecard dimension,
    since a fixed dimension would mis-render `insufficient-evidence` on the
    non-nuclear desks it doesn't cover; its read still surfaces via the
    per-country composition's `other_analysts`) derived by high-precision
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
- **Correctness Gold Set** (`system.goldset`) — the weekly correctness labeling
  worksheet (P2-5, `panels/system/Goldset.tsx`). The correctness-vs-reference
  gold set only grows if labeling is cheap: the panel shows the week's
  deterministic, server-pinned stratified sample (~8 verified findings,
  stratified per unit + faithfulness band) as a card list — finding title →
  cited read (the same `CitedProse` reading kit) → four verdict buttons +
  optional rationale → saved state. Verdicts upsert via
  `POST /v3/eval/goldset/label` (the server snapshots what was judged) and flow
  into the eval scoreboard's additive per-unit `operator … (n=…)` segment —
  never pooled with the deterministic recall leg. Honest states: an exhausted
  week says "all labeled — next sample Monday"; a week with no verified
  candidates says so. Progress/verdict logic is DOM-free in `@/lib/goldsetModel`.
  `personal`-only; pinned to the Operations nav group (a weekly operator duty,
  not an analysis read).

  **Provenance badges on displayed numbers** (P4-5, shared components): the
  `live | fallback | absent` enum (`lib/provenance.ts`) stamps every number
  that *could* come from live data, a degraded fallback, or nothing —
  **ProvenanceBadge** (`components/ProvenanceBadge.tsx`) renders which, with
  shape + label (not color alone). Honesty: `fallback` is only ever shown on an
  **explicit** backend fallback signal; with no such signal (the common case
  today) a present value reads `live` and an empty one `absent` — a fallback
  state is never synthesized (the backend fallback flag is a documented seam,
  §7). **ProvenanceCard** (`components/ProvenanceCard.tsx` +
  `describeProvenance`) maps what the substrate already carries (lineage,
  verify state, source, produced/fetched-at, confidence) onto a purpose /
  source / freshness / confidence / limitations card — no fabricated fields.
  Wired into the Wall's health quadrant, the Eval Scorecard's calibration
  numbers, and the Timeline's provenance card.
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
  kind/lifecycle rollup and an expandable last-error inspector. (The duplicate
  **Runtime Actor Health** kind, `system.runtime`, was deleted in S7-T2.)
- **Consumer-Lag Monitor** (`system.stream_lag`) — per-consumer NATS JetStream
  lag (`GET /v3/streams/consumer_lag`): `num_pending` headline lag,
  redeliveries (poison messages), unacked backlog (slow consumers); polled
  every 5s. Hidden from the sidebar (rolled into System Status's Queues
  section; still ⌘K-reachable).

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
  Lineage. Client-only (`preview` tier) — it fires while the panel is open;
  contrast the Watchlist below.
- **Watchlist** (`system.watchlist`) — **server-side standing watches** (P5-6,
  `panels/system/Watchlist.tsx`). The operator names a watch — an **entity**
  ("Wagner Group", alias-resolved), a free-text **topic** ("Strait of Hormuz"),
  or a **place** (ISO2 country list, or a lat/lon point + radius) — and the
  `watchlist_hit` trigger class inside the server's `alert_trigger_scan` pages
  on any VERIFIED finding touching it, on its own cadence, whether or not any
  UI is open (alerts flow the shared dispatcher → ntfy). The rows live in the
  `watchlist` table over `GET/POST/PUT/DELETE /v3/watchlist` — this panel is
  MANAGEMENT (list + add + optional min-severity floor + per-watch 7-day hit
  count); the alerts themselves are the product. Deletes are **soft**
  (`active=false`), so a watch's no-refire watermark history survives and
  re-activating never re-pages already-seen hits. The Inspector's **Watch
  this** affordance creates an entity watch from the current selection. `live`
  tier; ships in `personal` and `cis`; Awareness nav group.
- **Escalation Deliveries** (`system.escalations`) — the human-visible alert
  edge: renders `alert_sink_deliveries` (`GET /v3/system/escalations`) so the
  operator can see whether each escalation actually **landed** (delivered) or
  went nowhere (failed / logged_only). `live` tier; `personal` and `cis`.
- **Report Export** (`system.report_export`) — the **collection basket** (A10).
  The operator collects findings / analyst reports / journal entries from
  wherever selection already flows — the Inspector's "add to export" button, a
  feed row's + action, a Journal entry card — into ONE persistent basket
  (`@/state/exportBasket`, localStorage-backed; the status-bar chip shows the
  count) and composes here: basket list (removable), title, markdown/JSON toggle,
  Export. Composition is **server-side** (`POST /api/v1/v3/export`,
  `export_api.py`): each finding carries its cited body (`[N]` markers intact),
  citations resolved LIVE to signal titles + canonical_urls, the verify state
  (faithfulness or an explicit `unverified — <reason>`), confidence + verify-folded
  effective_confidence, the lineage receipt link, and — where the P2-1 evidence
  archiver has captured a cited signal — its `archived` flag + `archive_sha256`
  content hash (derived from the `cas:sha256/<hex>` object ref; honestly
  `false`/absent for un-archived rows); each journal entry carries
  its tier label + the reflective off-product-chain VOICE framing, claims + refs.
  Markdown renders a preview pane + `window.print()` for PDF. Size-capped at 50
  items (honest 413 beyond). **STIX is demoted to optional-later** (operator
  decision, A10 — not built into this flow; the DOM-free `@/lib/reportModel`
  STIX machinery stays in the repo, unused here). `live` tier + unhidden since
  A10; Products nav group; ships in `personal` and `cis`.

### Consolidated / hidden / deleted

The S7-T2 shell reform **deleted** the earlier DROP/consolidation cohorts
outright — `system.pulse` / `system.eval` / `system.users` / `system.streams`,
`registry.wirings` / `registry.mutations` / `registry.discovery`,
`dashboard.dynamic`, `system.backfill`, `system.runtime`, `system.tenant_view`,
`system.targets.roster`, and `v4.case` no longer exist as panel kinds (the #90
feed merge had already deleted `v4.feed`, subsumed by `system.findings`; the
Targets Roster's function lives in **Target Registry**, `registry.targets`).
Tenant View's deletion is deliberate truth-in-labeling: multitenancy is not
product-baked (Legba ships single-tenant; see `docs/DIRECTION.md` §0).

What remains in `HIDDEN_KINDS` (`registry.ts`) today are **live** panels kept in
the bundle — saved layouts and ⌘K deep links still resolve them — but dropped
from the sidebar so the catalog stays ~25–30 good panels:

| Hidden kind | Why | Where the function lives now |
| --- | --- | --- |
| `system.optimizer.diff` | operator review aid folded under Optimizer | opened from **Optimizer Candidates** |
| `source.subscription_builder` | niche source-config | ⌘K |
| `source.subscription_policy` | niche source-config | ⌘K |
| `source.fanout` | niche explorer | ⌘K |
| `system.stream_lag` | rolled into the at-a-glance view | **System Status** (Queues section) |

(`system.report_export` left this set in A10 — no longer the Report panel's
download twin but the collection-basket export surface, unhidden and `live`.)

### Cross-panel events

Panels coordinate through custom DOM events rather than route coupling, so any
panel can drive another without knowing whether it's mounted:

| Event | Fired by | Consumed by |
| --- | --- | --- |
| `legba:open-lineage` | Findings, target/analyst panels, Search, Alerts, Fan-out | Provenance Lineage |
| `legba:open-source-detail` | Source Registry, Fan-out | Source Detail |
| `legba:open-optimizer-diff` | Optimizer Candidates | Prompt-Module Diff |
| `legba:open-entity-graph` | Entities | Entity Graph |

(`legba:set-tenant` died with the Tenant View deletion; most cross-panel
coordination now flows through the unified selection store instead — the events
above are the survivors where an *open-this-panel* side effect is the point.)

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
(timeline/charts), `maplibre-gl` (maps), `deck.gl` (map density/arc layers —
**lazy-loaded** only when a deck layer is first switched on), `cytoscape` +
`react-cytoscapejs` (graphs), `react-markdown` (consult/reports), Tailwind
(styling). See `RUNBOOK.md` for deploy and ops procedures.

---

## 6. Panel tiers

Every panel kind carries a `tier` in its registry definition
(`PanelKindDefinition.tier`, `panel-registry/registry.ts`), surfaced as a small
**Preview** badge in `PanelChrome`'s header (threaded via `PanelTierContext` so
panels don't each re-declare it):

- **`live`** (default) — a product surface backed by a real, wired route.
- **`preview`** — registered and usable, but either a guarded-preview /
  honest-pending backend or an operator-experimental surface. The current
  preview set (`PREVIEW_KINDS`, `registry.ts`) is three kinds:
  - **Prompt-Module Diff** (`system.optimizer.diff`) — operator review aid over
    the GEPA loop; the `/diff` route is wired (snapshot-based, no dspy).
  - **Global Search / Alert Center** — client-only product surfaces with no
    dedicated backend route yet (the Alert Center's server-side counterpart is
    the `live` **Watchlist**).

  **Report Export** left the `preview` cohort in A10: it now fronts the real
  `POST /api/v1/v3/export` collection-basket route (shipped on the same train)
  and is `live` + unhidden. The new panels on the same train — the Wall,
  Timeline, Correctness Gold Set, Watchlist, Escalation Deliveries — all ship
  `live` (each fronts a real route: `/v3/since`, `/v3/timeline`,
  `/v3/eval/goldset/*`, `/v3/watchlist`, `/v3/system/escalations`). (The former
  Backfill Replay preview panel was deleted with the S7-T2 DROP set — the
  catch-up replay itself remains a runtime-plane operation with no registry
  proxy; see §7.)

## 7. Future seams

The authoritative list of declared seams is `docs/SEAMS.md`. UI-relevant entries:

- **Backfill trigger** — the Backfill Replay panel was deleted in S7-T2; the
  P-12 catch-up replay (`Backfiller.catch_up_and_forward`,
  `runtime/subscription/backfill.py`) remains a runtime-plane operation not
  reachable through the registry API (no registry→runtime proxy). Nothing in
  the UI fakes it.
- **A2A skill surface** (SEAMS #15) — operator-gated OFF by default
  (fail-closed); when disabled the runtime answers `/a2a/skills` with a 503 +
  enable recipe (never a silent 404). No UI panel today.
- **Source health** — Source Detail derives staleness from the latest signal vs
  cadence; System Status's source-firing matrix is the at-a-glance rollup. The
  backend's per-source freshness grades (cadence-derived budgets) are not yet
  rendered as a grade column.
- **Backend fallback flag** (P4-5) — the ProvenanceBadge's `fallback` state is
  only ever shown on an explicit backend signal, and no route carries one yet:
  today every displayed number honestly reads `live` or `absent`. Threading a
  real degraded-source flag through the routes is the declared follow-up.
- **Band-calibration surface** (P2-3) — `/v3/eval/calibration` carries the
  additive `band_calibration` section (persistence / reversal rates at
  14/28-day horizons); the Eval Scorecard does not render it yet.
- **Co-mention arcs** — the map's ArcLayer is honest-empty until multi-country
  `geo[]` lands upstream (the geo backfill stamps single countries today).

Hidden-by-default kinds (see §3 *Consolidated / hidden / deleted*) stay
registered so saved layouts resolve, but are kept off the sidebar; the ⌘K
palette still reaches them.

---

See also: `ARCHITECTURE.md` (planes, registry, runtime) · `ACQUISITION.md`
(sources, signals, fan-out) · `ANALYSIS.md` (analysts, findings,
hypotheses) · `AI_MODELS.md` (consult engine, models) · `RUNBOOK.md`
(deploy + ops).
