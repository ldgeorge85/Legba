<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# The operator workstation

`legba-ui-v3` is the console an operator reads Legba through: a single-page
Vite + React + TypeScript app whose workspace is a [Dockview](https://dockview.dev)
tiling surface. The operator opens **panels** into a draggable grid, each a
self-contained read (or authoring) surface over one slice of the substrate, the
registry, or the runtime.

The console is descriptor-driven in the same sense the rest of the platform is.
Two registries cooperate: a **bundle-time panel registry**
(`legba-ui-v3/src/panel-registry/registry.ts`), the static catalog of every panel
*kind* the app ships; and the **runtime descriptor registry**, the live list of
panel *instances* — one per bound target or analyst — fetched over REST and kept
current over a NATS-fed WebSocket. Adding a panel kind is a code change: one
registry row and one component. Adding a panel *instance* is a descriptor
change, and the binding shows up in the sidebar with no UI rebuild.

The console states what it measures. Legba grades **groundedness** — does each
claim follow from its cited evidence? — and, separately, **correctness** against
independently built references; the panels show a weak or unmeasured result as
weak or unmeasured, and an empty space is the truthful render for something the
platform has not earned a number for.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the planes, registry and runtime,
[ACQUISITION.md](ACQUISITION.md) for the source / signal / fan-out model the
panels read, and [ANALYSIS.md](ANALYSIS.md) for the analysts behind the products.

**Contents:** [The shell](#the-shell) · [Workspaces and presets](#workspaces-and-presets) ·
[Deployment modes](#deployment-modes) · [The panel catalog](#the-panel-catalog) ·
[The Inspector](#the-inspector) · [Registering a panel](#registering-a-panel) ·
[Retired ids](#retired-ids-and-hidden-panels) · [Auth chain](#data-access-and-the-auth-chain) ·
[Mobile](#the-mobile-surface) · [Building](#building-and-running)

## The shell

The root component (`src/App.tsx`) lays out four regions: the sidebar at left,
the workspace bar along the top, the Dockview workspace filling the middle, and
the status bar along the bottom.

**Sidebar** (`components/Sidebar.tsx`) — one grouped navigation tree: a search
launcher, a Layouts menu, then five verb-grouped sections the analyst scans top
to bottom, with the per-target and per-analyst instance groups nested inside the
last of them.

| Group | The verb it serves |
|---|---|
| **Awareness** | what is happening now — the live surfaces and the detail rail |
| **Investigation** | dig into the why — entities, provenance, search, the graph walk |
| **Analysis** | reason over it — the optimizer, the eval scorecard, the divergence map |
| **Products** | the finished intelligence — the reads, the journal, the export basket |
| **Engine Room** | the plumbing that runs it — registries, health, ledgers, operator queues |

All five start collapsed with a count on each header, so the sidebar opens as
five rows rather than a flat catalog; a saved collapse list wins over the
default. Within a group a handful of kinds sort in workflow order (`TASK_ORDER`,
`panel-registry/navGroups.ts`) and the rest alphabetically by title, so Awareness
reads Wall → Live Feed → World Map → Timeline → Alerts & Watches → Inspector →
Consult and not the alphabet. A panel's group comes from an explicit per-kind
override, then a prefix fallback on the leading segment of its kind (`registry.`
/ `source.` / `system.` → Engine Room, `v4.` → Investigation, `analysis.` →
Analysis), then an Engine Room catch-all — total over every kind, so a new panel is reachable before anyone
assigns it a group, and the overrides exist so the *intent* survives a change to
the fallback.

The Targets and Analysts instance groups render the runtime registry's rows
union a synthesized bound-panel set (`panel-registry/synthesize.ts`): no
descriptor declares `outputs.ui_panel`, so the live registration surface is empty
and the groups are minted from the live descriptor heads
(`GET /registry/descriptors?family=target|analyst&head_only=true`), one synthetic
registration per record × bound panel kind. Real registry rows stay
authoritative — a synthetic row an existing non-retired registration already
covers is dropped.

**Workspace** — a `DockviewReact` instance whose every tile resolves its bound
registration to a lazy-loaded component, so a panel's chart or map libraries load
only when its tile is first opened.

**StatusBar** (`components/StatusBar.tsx`) — the **honesty footer**: a standing,
unprompted statement of how much of the intelligence the reader is reading was
actually covered, in four figures, each over one of our own objects and each
sourced from one live route. Beside it sit the console's own small chrome: the
last registry refresh, the auth state (`auth: ok` / `auth: dev`), any
registry-load error, an export-basket chip that renders only when the basket is
non-empty, and the deployment mode under debug chrome.

| Figure | What it states | Route | One click opens |
|---|---|---|---|
| **sources** | firing over the wired roster, with silent / error / paused and the per-source freshness grades counted separately in the tooltip; as-of is the freshest signal | `/v3/system/source-firing` | `system.source_health` |
| **gaps** | typed absences on record for the **active scope**, how many are past their re-check, and any kind that could not be read for the desk at all | `/v3/absence?scope=` | `v4.morning_read` (the Gaps band) |
| **judge** | mean faithfulness with the `n` it was taken over, its window, and the pooled-graders caveat when the window straddles a pipeline change | `/v3/system/judge-stats` | `system.judge_stats` |
| **grader** | desks the independent reference reached over the roster, with correctness beside its coverage | `/v3/eval/grader_roster` | `system.eval_scorecard` |

Every figure carries its unit and its own as-of on the bar, and its whole
sentence — absolute as-of, every denominator, every caveat — in its tooltip. An
absent reading prints **`unmeasured`**, never `0`; a *measured* zero stays a
zero, so a desk read for typed absence and found clean shows `0 typed`. The
`/v3/absence` read only fires when the wall is scoped to exactly one desk,
because the route answers one desk at a time — a world scope states why it has
no count rather than showing one. Every query key is a key a panel already uses,
so the footer adds no request while that panel is open; the arithmetic and the
wording live in `lib/coverageFooter.ts`, DOM-free, and are argued with in a unit
test rather than inside a component. **The registered-panel count is gone** —
the footer's job is what the intelligence covered, not what the console loaded.

### The selection store

One selection store (`src/state/selection.ts`, `useSelection`) is the single
source of truth for what is selected across the whole app: click any row, map
dot, graph node or id anywhere and that record brushes every subscribing panel
and loads its full detail in the Inspector. A `row_kind → SelectionKind` bridge
maps substrate row kinds onto the cross-room vocabulary, so a lineage-walk click
is never a dead end, and `Alt+←` / `Alt+→` walk the drill trail (`Alt+Shift+` the
same arrows walk the coarser scope trail). The store is **capped at one selection
plus a drill breadcrumb**, because brushing-and-linking degrades past about three
linked surfaces — the design reason for a few rooms plus one Inspector rather
than many mutually-brushing panels.

### The command palette

`Ctrl/Cmd-K` opens the record-jump gateway (`components/CommandPalette.tsx`). One
fuzzy query resolves across five indexed families, ranked recents → favorites →
the rest: **records** (`Enter` opens the record's bound primary panel,
`⌘/Ctrl-Enter` selects it into the Inspector), **panels** (every singleton kind,
including the registered-but-hidden set, discoverable here and nowhere else),
**presets**, **workspaces**, and **actions** that open a record's bound analysis
grid. A leading sigil narrows the search: `#` to workspaces, `>` to panels and
presets.

Dockview keeps inactive tab content mounted but hidden, which blanks any surface
that measures itself at mount, so two shared hooks own that class of bug rather
than each panel re-solving it: `useDockviewTileRedraw` redraws and re-keys a tile
a frame after it becomes visible, and `useElementWidth` measures container width
through a callback ref so the observer follows the element rather than the
mount.

## Workspaces and presets

Two mechanisms re-arrange the dock, and they own separate storage keys so
neither writes the other's.

**Workspaces** (`components/WorkspaceBar.tsx`, `lib/workspaces.ts`) — six stances
above the dock, each a curated arrangement for one reason to open the app. A
workspace is an object you return to, not a reset: switching serializes the
outgoing stance into its own slot and restores the incoming one, seeding a
curated default only on first entry, and an autosave on unload means a reload
returns the arrangement you left.

| Workspace | The question it answers | Key |
|---|---|---|
| **Morning Read** | What happened, what moved, what does it mean? (the landing) | `Alt+1` |
| **Desk** | Everything about the one place or lane I have selected | `Alt+2` |
| **Investigate** | Follow this one thing to the bottom | `Alt+3` |
| **Trust** | Is what it produced any good? | `Alt+4` |
| **The Gate** | Work the human queues — journal proposals, grading, optimizer candidates | `Alt+5` |
| **Engine** | Is the machine running, and what is it configured as? | `Alt+6` |

`Alt+backtick` cycles, `Alt+Shift+R` resets the active stance to its curated
default, and `⌘K → #` lists the six — the guaranteed switcher if a keybinding is
contested. `#ws=<stance>` rides the same URL hash as `#sel=`, so a shared link
carries the stance as well as the record.

**Missions** (`lib/missions.ts`, `lib/applyMission.ts`) — a stance seeds PANELS,
which is half of what "put me in crisis mode" means. A **mission** is the whole
object: panel set **+** target scope **+** temporal window **+** map layers **+**
the Consult tile with its scope pin already set. Four ship, named for the
reader's job, chosen from the **Mission** button on the workspace bar:

| Mission | Stance it mounts | Scope | Window | Map layers |
|---|---|---|---|---|
| **Morning Read** | Morning Read | the whole roster (clears the scope) | 24h | signals, findings, situations, events |
| **Desk Watch** | Desk | the desk you have selected | 7d | signals, findings, events |
| **Crisis** | Investigate | the situation you have selected | 6h | signals, situations, events |
| **Release** | Trust | left as it is — the eval set is over the roster | 30d | left as you set them |

The chooser shows each mission's one-line description and a **"what this mission
shows"** line derived from the object itself — tile count, scope, window, layers
— so the promise and what choosing actually does cannot drift apart. Choosing
one switches the stance through the ordinary switcher (so the outgoing
arrangement is still serialised into its own slot), then sets the scope, moves
the map's and the scrubber's window in one write, sets the layers wholesale, and
opens the tiles that stance's seed does not carry. A mission is **not** a seventh
stance and has no storage slot of its own; it rides the hash as
`#mission=<id>`, and `#mission=` alone names the stance.

Nothing is invented: a mission *declares* what it wants to be about and
*resolves* that against what the reader has selected. Desk Watch with no desk
selected leaves the scope as it was and says so on the bar. A link carrying its
own `#scope=` keeps that scope — an address the sender chose beats a mission's
default aperture. Walking off a mission's stance drops the mission's name (it is
no longer true of the wall) without rolling back the posture it set. The Gate
and Engine carry no mission, and the chooser's footnote says why rather than
omitting them.

**Layout presets** (`lib/layoutPresets.ts`) — the Layouts menu clears the dock
and re-seeds it with a named arrangement. Seven ship:

| Preset | Panels |
|---|---|
| **Wall** | The Wall with the Inspector riding right |
| **Monitoring** | Live Feed · Inspector · Target Registry · Alerts & Watches |
| **Workspace** | Live Feed · Inspector · Consult · Provenance |
| **Investigation** | Live Feed · Inspector · Entities · Global Search |
| **Analysis** | Optimizer Candidates · Eval Scorecard · Consult · Layer Divergence (tabbed with Consult) |
| **Operations** | Actor Health · Dead-letter · Consumer-Lag · Governor Events |
| **Focus (Zen)** | Inspector alone, full canvas |

Presets seed through the same singleton opener the stances use, so a preset tile
is mode-gated identically and indistinguishable from a hand-opened one — a preset
naming an operator-only panel simply skips it in a narrower mode. **Save** and
**Restore** round-trip the live, hand-dragged layout through Dockview's own
serializer into `localStorage`, keyed per mode.

The landing seeds Morning Read: a glance strip on top, the Wall beneath it, the
Live Feed at left, and the day's read, alerts and Inspector at right over the
World Map. The operator reads the Wall for what moved, clicks a desk chip or a
feed row to brush it into the Inspector, follows a citation chip to the cited
signal, and walks the provenance trail from there. A placement whose reference
panel is mode-gated away is re-anchored rather than left pointing at a tile
Dockview never saw.

## Deployment modes

`auth/jwt.ts::currentMode()` resolves the active mode (`personal` | `above_ai` |
`cis`), highest priority first: the `?mode=` URL query param, the `mode` claim of
a stored bearer token, the `VITE_LEGBA_DEFAULT_MODE` build env, then `personal`.
Each panel kind declares the modes it ships in, and the sidebar, the command
palette and the layout opener all filter on the active mode: `personal` is the
single-operator daily driver with the widest panel set, `cis` a narrower lens.
The mode filters which panels show and nothing else — it is not a tenant
isolation boundary. Legba ships single-tenant, and the registry remains the
authority on what the bearer can read.

## The panel catalog

One row per registered panel kind. The group is its sidebar section; **bound**
panels (per target, per analyst) need a descriptor binding and open from the
sidebar's instance groups or the command palette. Panels marked *hidden* stay in
the bundle so saved layouts and deep links resolve, but take no sidebar row at
all — they are reached through the command palette, or through the panel that
opens them. Each kind's tier and modes are generated from `registry.ts` into
[RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md) §2 by
`scripts/gen_release_state_matrix.py`, with a drift test failing the suite if the
two disagree.

| Panel | Kind | Group | What it shows | What it reads |
|---|---|---|---|---|
| The Wall | `system.wall` | Awareness | The mission-control anchor: four quadrants — per-desk band grid, movers since last visit, newest high-severity verified findings, system health | `/v3/since`, `/v3/system/source-firing`, `/v3/system/analyst-cadence` |
| Live Feed | `system.findings` | Awareness | The one unified findings + signals feed; Live on/off, a Source filter, and findings-only situation clustering | `/findings`, `/signals`, plus the `analyst.*.finding` and `legba.signals.>` NATS tails |
| World Map | `v4.map` | Awareness | Banded-verdict choropleth over the country desks, with signal-density, co-mention arc, convergence-marker and watch-location layers, a dual-thumb time scrubber, and — beneath the draw layers — the six **source layers** with the per-country aperture declaration behind an `i` on each | the desk verdicts behind the Wall's band grid, `/signals` geo, `/v3/since`, `/v3/layers/divergence` |
| Timeline | `system.timeline` | Awareness | Events lanes and validity windows in one panel, mode-switched; ranged facts / situations / findings with supersession connectors | `/v3/timeline`, `/findings`, `/signals`, `/situations` |
| Alerts & Watches | `system.alerts_watches` | Awareness | Three tabs: server-side standing Watches, client-side alert Triggers, and Deliveries (did an escalation land) | `/v3/watchlist`, `/findings`, `/v3/system/escalations` |
| Inspector | `system.inspector` | Awareness | The selection-linked detail rail — see [The Inspector](#the-inspector) | the selected row's detail route, `/lineage/{kind}/{id}`, `/v3/watchlist` |
| Consult | `system.consult` | Awareness | The on-demand analyst workbench; a depth toggle switches between the inline ReAct answer and a detached deep-analysis workflow. Beside each settled answer's header, the server-composed **provenance census** prints what the answer rests on — *"cited: 6 live · 2 history · 0 web · model knowledge: 3 sentences"* — refs counted by their own `origin_class` column (never by which tool returned them) and sentences carrying no citation at all, the two halves labelled with their own units so they can never be read as one total. The panel prints what the route says and derives nothing; a class the server could not measure renders as *not measured*, never as 0, because "cites no live reporting" is a claim and absence is not | `/consult`, `/deep_consult` |
| At a Glance | `v4.kpi` | Awareness | Signal / finding / situation / source counts with band-change deltas | `/findings`, `/signals`, `/situations`, `/registry/sources` |
| Entities | `system.entities` | Investigation | The entity roster with search and class facets; tabs fold in the relationship graph and the ranked notable-structure shortlist | `/entities`, `/entities/graph`, `/graph/structure` |
| Global Search | `system.search` | Investigation | Cross-family search composed client-side — there is no backend search route; a family that 404s degrades rather than erroring | `/signals`, `/findings`, `/situations`, `/registry/sources` |
| Graph Walk | `system.graph_walk` | Investigation | An interactive walk over the reified edge store: anchor on an actor, take one indexed hop at a time, open an edge for its evidence. The three edge families draw as three kinds of line, and the disclosure strip always states how many edges the view is withholding | `/graph/ego`, `/graph/edge/{id}`, `/entities` |
| Provenance | `system.provenance` | Investigation | Five tabs over one subject: Why (the lineage DAG and provenance chips), Lineage (the hop-by-hop receipt walk), Flow (the live registry canvas over sources → targets → analysts → packs), Trajectory, and Narratives | `/lineage/{row_kind}/{row_id}`, `/findings`, `/signals` |
| Eval Scorecard | `system.eval_scorecard` | Analysis | Seven stacked sections, each over its own route key and never pooled: the honest skill scoreboard; band calibration (14d/28d persistence + reversal rates per horizon with their graded-outcome breakdown, split by direction and by dimension — ordinal band persistence, explicitly never a Brier score); **grader correctness, roster** — the nightly machine grader's whole fleet over a trailing 7-night window at ONE grain (desks by default, echoed on the response — a composition is graded against the same reference as the desks it composes over, so the two are never pooled), one row per desk sorted thinnest-coverage-first, each carrying correctness AND coverage with the denominator each rests on, a null share reading `unmeasured` and never 0% while a coverage of 0.0 still renders as the measured zero it is, and two labelled roster figures (mean of desks, one desk one vote; pooled claims, every claim once) because a correctness number read without its coverage beside it is the failure the section exists to prevent — never pooled with the gold-set axis directly below it; the operator gold-set correctness axis; standing external truth; the banded per-desk scorecard with per-band drill to the verified claims it rests on; and the per-analyst critic rollup Each card carries a scale stamp beside its generated-at; the scorecard's bands are a ladder rather than a measure, so the stamp reads method-only **Download CSV** on the banded section serialises the bands the page is holding — one row per (desk, dimension), insufficient dimensions included, the dimension's own stamp and the card's kept as separate as-of columns, an absent `method_version` an empty cell and never a zero — built client-side from those rows, so the file cannot disagree with the section; the filename names its scope and newest as-of (`legba_bands_<desk-or-all>_<asof>.csv`, `undated` when no row carries one) | `/v3/eval/calibration`, `/v3/eval/country_scorecard`, `/v3/eval/scorecard`, `/v3/eval/correctness`, `/v3/eval/grader_roster` |
| Layer Divergence | `system.layer_divergence` | Analysis | The source-layer divergence map: one card per desk with the six source layers as chips carrying their aperture declaration (present / absent / unmeasured / undeclared, each excluded one showing the operator's own reason), and the three layer-to-layer pairs as rows — state (fired · thin · below threshold · not evaluable · evaluated) with the `no_fire_reason` by name, a paired sparkline of the two layers' post-fold items/day under a z track with the firing threshold drawn, and a link into the finding when a pair fired. The header carries the unit's own sentence plus the method version, payload schema, window, baseline, threshold and thin floor; the SEAMS #60 un-audited-classification stamp rides every read. A quiet run is suppressed to trace-only, so this is the only surface on which the instrument is visible on the days it fired nothing | `/v3/layers/divergence` |
| Cross-framing | `analysis.cross_framing` | hidden (Analysis when shown) | ONE CLAIM against the units that touch it. The header carries the claim verbatim, its per-claim verify verdict and the count sentence ("carried by N of M units · K with no read") beside the record's scale stamp and as-of; then one row per unit on the RECORD's own coverage roster — how that unit's read frames the claim (its own sentence, with its own markers), what it rests on (cited signals, outlets, the record's fold chips, the block's faithfulness), and for a unit with no read the TYPED ABSENCE in the route's own words with its proof and its clock (a stale one reads "last known absence, not re-checked"; a unit with none reads "no typed absence is recorded" beside the record's own coverage status) — never a blank row. Below it: the six stance-typed leans' framings of the same evidence, each with its declared prior named and its own gate score (a lens that ran and cited nothing is SILENT, not dissenting); the desk's layer-divergence receipt pair by pair with the SEAMS #60 stamp, where no response · a failed read · no run ever · a desk the run did not resolve are four distinct answers; and WHAT NO UNIT SAYS — the units named by none ("absent, not contradicted"), the desk's off-roster typed absences, the kinds the route could not read, the record's drop ledger by why-class, and the tension pass's checked negative. **Ask about this claim** sets the AMBIENT consult scope pin (`lib/consultContext.SCOPE_PIN_ORIGIN`) to the claim, so the existing Consult session answers about it with its provenance census line intact. Adds NO route and computes no statistic; all derivation is pure in `lib/framingModel.ts`. Opened from the Claims panel's `cross-framing ▸` action, a situation row, or ⌘K | `/findings`, `/v3/contentions`, `/v3/absence`, `/v3/layers/divergence`, `/journal?kind=lens` |
| Optimizer Candidates | `system.optimizer` | Analysis | The GEPA candidate queue with its real paired before/after faithfulness delta, the method each row actually took, and the human promote action | `/v3/optimizer/candidates`, `POST …/{id}/review` |
| Prompt-Module Diff | `system.optimizer.diff` | hidden | Candidate-versus-parent prompt-module text diff before promotion; opened from Optimizer Candidates | `/v3/optimizer/candidates/{id}/diff` |
| Navigator | `system.navigator` | Products | The products index — the day's reads by tier, scoping the rest of the surface to whichever one the operator picks | `/findings` |
| Morning Read | `v4.morning_read` | Products | Opens on four judgment bands per desk, worst-moved first — **Changed** (what landed since the reader's last visit, or yesterday 06:00Z, each row cited and clickable), **Checked** (the day's claim verdicts: verified / flagged / unchecked counts, with every flagged sentence quoted beside the finding it came from — reads `/findings` at its `fields=judgment` weight, the verify verdict whole plus reduced citations, never the finding's `data`/`derived_from`/`body`, which cut a measured 200-row page from ~5.5 MB toward ~1 MB), **Gaps** (TYPED ABSENCE, read `/v3/absence?scope=` once per desk shown and cached for the page load: the eight kinds with the route's own meaning on hover, each item carrying its `as_of`, its `expires_at` and — where that has passed — "last known absence, not re-checked", with the proof's `ref_kind` offered as a control only where it resolves; a kind the route could not read for a desk says "not measured for this desk" in the route's own words, a kind it read and found nothing under is named as read, and a desk with nothing absent is named rather than dropped. The card-derived rows — dimensions banded `insufficient-evidence`, a desk whose composition is absent — follow beneath each desk as the rows no kind already names, with the de-duplicated count stated) and **Due** (pre-registered forecasts past their window with the resolution test frozen on the row, each call carrying the probability scale its `p` is read on and the moment it was issued) — above the unchanged assembly reader for the composed read. A reading-order rail carries the four counts and a read timer against the twenty-minute budget; the timer displays the read-telemetry session clock and emits nothing, so it cannot inflate the `brief_read` count the wager is graded on. | `/v3/since` (incl. its `forecasts_due` section), `/findings?fields=judgment`, `/v3/eval/country_scorecard`, `/v3/absence?scope=` (one per desk shown) |
| World Assessment | `v4.assessment` | Products | The reading surface for a composition: the world one-pager with nothing selected, the desk intelligence card with a desk selected (band and delta → BLUF → the verified country composition → the per-desk unit cards → related → history) | `/findings` |
| Journal | `system.journal` | Products | The reflective voice: kind-filtered entries grouped by cycle, the open consolidation above them, a reader pane with per-claim provenance chips, and an honesty banner keyed off the live calibration verdict Each card carries a kind badge (entry, consolidation, chronicle, lens, lens_diff, plus the stateful tier: inquiry and crossroads, which band with the synthesis reading because they read across cycles) and each cycle splits into a synthesis band above the diary band. | `/journal`, `/journal/{id}` |
| Report Export | `system.report_export` | Products | The collection basket — items gathered from the Inspector, feed rows and journal cards, composed server-side into markdown or JSON with citations resolved live. "Desk brief" (also on the Target Overview panel) is a one-click shortcut that fills the basket with the scoped desk's current composition, each unit's latest admitted read in the composition's declared order, and its open situations/tracked events, then exports it in one file — and asks the route for the desk's TYPED ABSENCE beside them (`appendix.absences` + `appendix.scope`; composed server-side so the markdown and the JSON carry the same block, all eight kinds with the stamp and proof rules, printed after the cited events). **Print / save as PDF** (also on the Target Overview desk brief) re-lays the composed markdown into its own print document — A4/Letter margins, a running header carrying the title and the export's own as-of, each section's citations lifted to numbered endnotes — every signal endnote carrying the cited source's date with the word that says which date it is (`published` when the source declared one, `fetched` when the platform's read stamp is all there is; a citation the route could not date states the absence in words) — a page break before the appendix and another before the typed-absence section, which is split off the appendix onto its own page — and prints it from a hidden iframe, so no workstation chrome reaches the page and no route is added; it reformats nothing, and is disabled until a markdown document has actually been composed | `POST /v3/export`, `/findings`, `/situations`, `/v3/events` |
| Action-Pack Grants | `registry.action_packs` | Engine Room | Action-pack descriptors — tools, channels, tags, governor caps — plus the effective-capability view of the analyst ∩ target ∩ pack intersection the governor gates | `/registry/action_packs`, `/registry/descriptors` |
| Actor Health | `system.actor_health` | Engine Room | The actor roster by kind and lifecycle with last run, last outcome, cooldown and error counts, and an expandable last-error inspector | `/v3/runtime/actors` |
| Analyst Registry | `registry.analysts` | Engine Room | Analyst descriptor heads with method kind and tool whitelist on the row, the guided builder, and the inline editor | `/registry/descriptors?family=analyst` |
| Audit-Chain Browser | `system.audit` | Engine Room | The descriptor audit log with each entry's signature re-verified inline, a chain-health banner and tamper highlighting | `/registry/audit` |
| Budget Ledger | `system.budget` | Engine Room | Per-analyst tokens, runs and cost; the global per-bucket envelope; budget-exhaustion demote events | `/budget/ledger`, `/budget/envelope`, `/budget/demotions` |
| Consumer-Lag Monitor | `system.stream_lag` | hidden | Per-consumer JetStream lag, redeliveries and unacked backlog; the rollup lives in System Status | `/v3/streams/consumer_lag` |
| Dead-letter Inspector | `system.dead_letter` | Engine Room | The dead-letter projection across its namespaces, with an inline-patch resubmit and a resolved/unresolved toggle | `/registry/dead_letter`, `POST …/{id}/resubmit` |
| Eval Boards | `system.eval_boards` | Engine Room | Three engine-level boards read on a different errand from the analyst scorecard: desk baselines, band trajectory, and per-analyst run timing. An expanded baseline row carries the scale stamp its counts and sigma-distance are read on, with that row's own computed-at — a sigma-distance is comparable across desks only within one scale era | `/v3/eval/desk_baselines`, `/v3/eval/band_trajectory`, `/v3/eval/analyst_runtime` |
| Fan-out Explorer | `source.fanout` | hidden | Walks source → signal → finding: a source's signals, then the findings whose `derived_from` holds one | `/signals?source_id=`, `/findings` |
| Governor Events | `system.governor` | Engine Room | The per-pack governor's allow/block decision stream, blocks emphasised | `/registry/governor_events` |
| Journal Gate | `system.journal_gate` | Engine Room | The human gate on `journal_proposals`: accept (an idempotent per-kind apply) or reject with a reason | `/journal_proposals`, `POST …/{id}/{accept,reject}` |
| Judge Stats | `system.judge_stats` | Engine Room | The judge's verdict mix by serving provider | `/v3/system/judge-stats` |
| Model Stack Settings | `system.settings` | Engine Room | Model-component configuration and the first-run wizard. The runtime authority for LLM / embedding / NLP endpoints is the stack-component registry, not the environment file, which only seeds those rows at bring-up | `/registry/config/status`, `/registry/stack`, `POST /registry/vault/secrets` |
| Movers Since Last Visit | `system.wall_movers` | hidden | The Wall's movers quadrant mounted standalone, kept for saved layouts that hold it | `/v3/since` |
| Production Gauge | `system.production_gauge` | Engine Room | The whole-engine production read with its integrity and metering checks; its paging flag is the same predicate the alert plane's production-deficit trigger fires on, so the panel and the operator's phone cannot disagree | `/v3/system/source-firing`, `/v3/system/analyst-cadence` |
| Read Scoreboard | `system.read_scoreboard` | Engine Room | The one panel whose subject is the operator: reads today, morning reads, drills, a per-kind table and a fourteen-day strip. An empty log renders as a stated finding, and the panel-open bias is disclosed in surface | `/read-events/rollup` |
| Source Detail | `source.detail` | Engine Room | Per-source identity, scope and output summary; a cursor and staleness readout derived from the most recent published signal against the descriptor's cadence; the projected output-stream shape; the recent signals | `/registry/sources/{id}`, `/signals` |
| Source Health | `system.source_health` | Engine Room | Source quality and staleness debt in one rollup, with a per-source drill and a sortable cadence-derived freshness-grade column (`empty`/`ungraded` render muted as absences, never as failing grades) | `/v3/source-quality`, `/v3/system/staleness-debt`, `/v3/sources/{id}/quality` |
| Source Registry | `registry.sources` | Engine Room | Every shared source descriptor with kind, acquisition, scope, subscription policy and output subject lifted onto the row; create, edit and drive the lifecycle transitions | `/registry/sources`, `POST /registry/descriptors/source/{id}/transition` |
| Stack Registry | `registry.stack` | Engine Room | Substrate stack components. Credentials are never returned — vault refs resolve at call time | `/registry/stack` |
| Subscription Builder | `source.subscription_builder` | hidden | Composes a target's source reference — an explicit source id or a selector predicate — plus a signal-level filter, previews what it would match, and emits copy-ready JSON | `/registry/sources`, `/signals` |
| Subscription Policy | `source.subscription_policy` | hidden | A source's subscription gate (open / allowlist / grant) and the per-source-and-target grant wirings | `/registry/sources/{id}`, `/registry/descriptors` |
| System Status | `system.status` | Engine Room | Per-layer health in one page — Acquisition (the per-source firing matrix), Analysis (per-analyst cadence, read from the run record rather than actor state), Queues (orphan-filtered consumer lag), Infra (substrate reachability) | `/v3/system/source-firing`, `/v3/system/analyst-cadence`, `/v3/streams/consumer_lag`, `/v3/runtime/actors` |
| Target Registry | `registry.targets` | Engine Room | Target descriptor heads with filter, expand-to-body, the guided builder, the starter clone-and-edit and the inline editor | `/registry/descriptors?family=target` |
| Weekly Grading | `system.goldset` | Engine Room | The week's server-pinned stratified sample of verified findings as a card list — read the cited prose, pick a verdict, optionally say why. Verdicts feed the eval scoreboard's operator segment and are never pooled with the deterministic leg | `/v3/eval/goldset/*` |
| Target Overview | `target.overview` | per target | Descriptor metadata, the runtime actor roster with source cursors and error counts, recent signals and findings, and three brief actions in the header: **"read as page"** opens the **Desk Brief** panel (`target.desk_brief_page`) on this desk — the same composed document, read in place instead of downloaded; "desk brief" downloads the desk's current composition + unit reads + open situations + the desk's typed absence as one markdown file; and "print / save as PDF" prints the brief just composed — the same print document the Report Export panel builds, from the bytes already downloaded rather than a second compose | `/targets/{id}/runtime`, `/signals`, `/findings`, `POST /v3/export`, `/situations`, `/v3/events` |
| Desk Brief | `target.desk_brief_page` | per target | The desk brief as a PAGE — what `POST /v3/export` already composes, read on one scrollable surface organised by the desk's bounded units instead of downloaded. The index card carries the composition record with its fenced country voice beside it in its own band, the scale/method stamp, the as-of, its faithfulness and — never pooled with it — its correctness badge with that badge's own coverage. Then one block per unit in the composition's **declared** order (the coverage register it was built over), each cited through the one prose renderer so every marker keeps its source tag and fold chips, with the temporal layer printed where a historical series is cited — valid time, record time and the producer's tense marker verbatim. Below them the **evidence table** runs over the desk's unit ROSTER, not the carried set: one row per bounded unit with an `Evidence state` drawn from our own vocabularies (the composition's coverage register — `in basis` / `below floor` / `unverified` / `no head in horizon` — then the scorecard's `insufficient evidence`, then the cadence check's `stale` / `no read yet`), the record that decided it, the unit's faithfulness and correctness coverage, and the [typed absences](GLOSSARY.md#analysis--methods) whose subject is that unit; beside it the desk's declared source-layer aperture (present / absent / unmeasured / undeclared, each in the operator's own sentence, and "no layer map is declared for this desk" when none names it). Then the endnotes, each with its masthead and the route's own date word (`published` / `fetched`) kept verbatim, an undated citation stating the absence in words. Finally **Reading limits** — the scale era, the composition as-of, the document's compose instant, the absence read instant, the judge scorecard's instant and the grader reference's window and state — and a GENERATED *what this page does not publish*, assembled from the absence route's own `not_measured` classes, its per-kind held-back counts, the coverage register's unfilled units, the export's own missing-id placeholders and the grader's reference state, never from hand-written prose. Header actions: markdown and print (the print path stays the document of record — it prints the route's markdown through the same print document), **JSON** (the structured export document exactly as served) and **PNG** (`html-to-image` over this page's own DOM). Opening the page sets the ambient consult scope pin to the desk; opening a unit block re-pins it to that unit's read, so the Consult tile follows without a consult panel bolted on. An unmeasured figure prints as `unmeasured`, never as `0` | `POST /v3/export` (`format:'json'`), `/findings`, `/situations`, `/v3/events`, `/v3/eval/country_scorecard`, `/units/{target_id}/correctness`, `/v3/layers/divergence` |
| Target Signals | `target.signals` | per target | The raw signal table for the target's geo scope, with source and language filters and geo / entity-class / tag chips | `/signals` |
| Target Findings | `target.findings` | per target | The target's findings, severity-badged and sortable, with topic-tag chips; above the list, a gap strip shows one cell per bounded unit — current / insufficient-evidence / stale / no-read, sourced from `/findings` and the banded scorecard. A green cell opens its read; every other cell drills into the [typed absence](GLOSSARY.md#analysis--methods) behind it (7b/k5), and the Inspector renders that absence's kind, its proof — what was checked, when, and the record of that look — and whether anyone has looked since | `/findings`, `/v3/eval/country_scorecard`, `/v3/absence?scope=` |
| Target Situations | `target.situations` | per target | Situations bucketed by lifecycle state, with intensity-derived severity and contributing-finding links. An expanded frame shows the scale its intensity is a reading on, so a 59 here is not silently compared with a 59 from before the August re-base | `/situations` |
| Target Claims | `target.claims` | per target | Claim-like findings carrying corroboration score and sources, confidence-sorted with an evidence chain | `/findings`, `/facts`, `/claims` |
| Target Map | `target.map` | per target | A clustered overlay of geocoded signals and severity-coloured finding markers, with layer toggles, a per-country breakdown and provenance on hover | `/signals`, `/findings`, `/entities` |
| Target Timeline | `target.timeline` | per target | Signals, findings and situations over time, with situation lifecycles drawn as spans | `/signals`, `/findings`, `/situations` |
| Target Graph | `target.graph` | per target | The per-target lineage graph with a depth slider, row-kind filter chips and click-to-re-root | `/lineage/{kind}/{id}?direction=both` |
| Target Sources | `target.sources` | per target | A per-source ingest-health rollup from the target's signals grouped by source, joined to the source registry for name and handler kind | `/signals`, `/registry/descriptors?family=source` |
| Analyst Runs | `analyst.runs` | per analyst | Runs, outputs and critiques in three independently paginated tabs | `/analysts/{id}/{runs,outputs,critiques}` |
| Analyst Outputs | `analyst.outputs` | per analyst | A live tail of this analyst's findings with a severity histogram and run count | `/findings?analyst_id=` plus the `analyst.*.finding` tail |
| Cross-target Analyst | `analyst.cross_target` | per analyst | Per-target contribution counts and a contradiction-first correlation surface, for the cross-target analyst kinds | `/findings` |
| Critic Scores | `analyst.critiques` | per analyst | The critique history of this analyst's outputs: per-rubric-axis bars, the overall trend, the revision delta | `/analysts/{id}/critiques` |

## The Inspector

The Inspector (`components/inspector/InspectorPanel.tsx`) is the console's
primary read surface and the keystone the rest of the panel set brushes into. It
is driven entirely by the selection store: click any row, map dot, graph node or
id anywhere and its full detail loads here, every referenced id rendered as a
link so the next selection is one click away and a breadcrumb trails behind.
With nothing selected it shows a call to action rather than an empty frame. It is
built on the existing panel chrome and descriptor view and reuses the provenance
trail — not a second rendering stack.

**The cited read card.** For a finding, the card at the top is the drillable
product read, built from the shared reading kit: `CitedProse` is the one prose
renderer — markdown rendered, `[N]` and `[[ref:N]]` markers tokenized into
interactive chips — and a chip scrolls to and flashes the matching row in the
evidence panel below, whose title links into the cited signal. A legacy or
uncited finding degrades honestly: its prose renders plainly under an explicit
uncited marker, with no fabricated anchors and no empty evidence panel.

**The verdict badge** (`components/VerdictBadge.tsx`, `lib/verdictModel.ts`) is
the one verification dialect, aligned to ICD-203 and keeping its two axes
separate exactly as ICD-203 does: **L**, the likelihood on the seven-point verbal
scale (`unstated` when none is recorded), and **C**, the analytic confidence
derived from the faithfulness pass, the judge status and citation breadth
(`unverified` when no verify block exists); a legend affordance opens the tables
in place. A deterministic structural analyst's finding reads
`unverified — structural` because it never enters the faithfulness pass, and one
whose asserted quantities were re-derived from its own lineage and matched is
classified apart, so a miscount becomes a flagged critique rather than a silent
pass.

**Per-claim verdicts.** Hovering, focusing or tapping a resolved citation chip
opens a card with the source, the cited passage (or an honest note that none was
recorded), the citation's credibility, and the per-claim verdict for that
ordinal, derived from the verify block's unsupported spans — the only per-claim
record the verify pass persists. When the judge ran and nothing names the chip
the card says so; it never claims a positive the platform did not record. The
shared citation vocabulary (`lib/citationsModel.ts`) is read by every consumer,
so a chip's label, kind attribute and drill target all come from the kind the row
actually carries, and an unknown kind renders as what it says it is rather than
being guessed into the nearest familiar one. Beside each claim the read card also
prints the cited source it rests on — masthead · date — and one chip per fold
reason the record states (`single-source`, `N wire-folded`, `unsupported`,
`insufficient evidence`, `contested by retrieval`, each tooltipped with that
reason in one sentence read off the row: `lib/claimFold.ts`), so the checking is
visible at the sentence rather than a click away, and a claim whose record
states no fold reason renders exactly as it did before.

**Contested by retrieval** is the one chip whose evidence is *outside* this
tower. Every other fold reason describes how the platform assembled the claim —
the wire fold, the single-source mark, the judge's own verdict — and all of them
are the tower checking itself. This one quotes a `claim_contentions` row: the
contrary-evidence pass formulated the claim's counter-query, ran it on the free
rung, and fetched a page that states the opposite (`narrowed by retrieval` when
the page only narrows it, read at a lower volume because a qualification does
not pull against a claim). The tooltip says a page this platform *holds* states
the opposite, names its host and the date the page itself states, and ends *Not
adjudicated* — because a stance describes the retrieval and never the claim, and
a chip that implied a verdict would undo the whole pass. So it is the one chip
that **drills**: it is a button, it selects the record through the same
`selectRow` idiom the absence rows use, and the Inspector renders the
counter-query, the rung, the page's URL, its stated date and the hash of the
bytes we hold (`GET /v3/contentions?claim_id=`). The drill also carries the four
fences that let the sentence exist at all (migration 0222): the decisive page's
host class and the date the date gate actually parsed, how much of the claim's
subject the matched sentence carried, `independent_pages` read against its bar
as *N of 2 independent pages* — one admissible page is a qualification, not a
contradiction — and every page the pass fetched with the fence that refused or
capped it, each reading printed as `not measured` rather than as 0 on a record
written before those columns existed. A chip that named external
evidence without offering it would be asking to be taken on trust, which is the
posture the checking layer exists to replace. A retrieval that settled nothing
chips nothing — that is the pass's expected common answer and it is not a
weakness of the claim — and an expired record chips as *expired, not
re-checked* rather than vanishing, because hiding it would make a pass that
stopped running look like a week with nothing to contend.

**Typed absence in the Inspector.** One selection kind names something the
substrate does not contain: `absence`, whose id is `scope|kind|subject` and
whose detail is resolved against `GET /v3/absence?scope=` — so the desk gap
strip's silent cells, which had no record to open and were therefore the one
state a reader could not interrogate, now drill like everything else. The panel
renders the absence's kind from the closed eight-kind vocabulary, its proof
(what was checked, when, and a link to the record of that look wherever the
route types the ref as one that resolves), and its clock: an absence whose
measurer was due to repeat and did not reads *last known absence, not
re-checked* rather than passing for a current one. Three outcomes, never a
blank — the absence and its proof, the route's own sentence saying the kind was
not measured for this desk, or an explicit "no typed absence is recorded",
because *not checked* and *nothing absent* are different answers. See
[typed absence](GLOSSARY.md#analysis--methods).

**A cited historical observation in the Inspector.** A citation chip whose
`ref_kind` is `observation` names one row of a curated **collection**,
not a signal — so it resolves *in the card* rather than by drilling: the
producer carries the whole row on the citation, and the hover card prints the
value with its unit, the **stale-tense marker** verbatim
(`(historical: valid YYYY..YYYY, recorded YYYY-MM)`), the provider, series and
subject, and the `sha256` of the provider file the number was read out of. No
panel is added and no drill is offered, because there is no observations record
to open and a link to one would be a click that 404s — while the reader still
gets the thing a drill was for, including inside an exported document, where
most citations are actually read. A cited observation whose producer wrote no
row block is still labelled *historical observation* and simply carries no
fields, never a guessed period. See
[Collections](COLLECTIONS.md#readers--how-a-holding-is-read-and-cited).

**The two eval badges** sit side by side and are never pooled, because they
measure different things: faithfulness asks whether the read stayed true to its
own citations, correctness whether it was right against a reference built blind
to the substrate (see [ANALYSIS.md](ANALYSIS.md) and
[CORRECTNESS_GRADER.md](CORRECTNESS_GRADER.md)). The unit eval badge
(`GET /eval/scores`) and the unit correctness badge
(`GET /units/{target_id}/correctness`) each render a server-composed line
verbatim — the no-invented-number contract lives on the server — and the
correctness badge expands on click into the per-claim ledger, contradictions
first. Both render **nothing at all** where there is no measurement: a dash, a
zero or an optimistic hundred per cent would each be a claim the platform has not
earned, so an empty space is the truthful render for an unmeasured unit.

**The provenance trail.** The Inspector reuses the receipt walk
(`GET /lineage/{kind}/{id}`) the Provenance panel renders in full. Each hop
carries a SHA-256 receipt hash and a chain-consistency boolean; a hop that
re-hashes to its stored value shows the badge `chain-consistent (single-node)` —
a hash-chained receipt, not a signature, and the wording says so. A signal hop
renders a modality reference out to the real source URL, so a walk always reaches
a clickable acquisition source.

**The scale stamp.** At the head of the record sits the chip that says which
SCALE its numbers are a reading on and which method revision computed them
(`components/ScaleStamp.tsx`, `lib/scaleStamp.ts`), with the record's own as-of
beside it. The two halves answer different questions and neither substitutes for
the other: the method version says whether the same code produced two numbers,
the scale version whether the two numbers mean the same thing — migration 0188
re-based every stored situation intensity in August 2026 with no code change at
all, so an intensity of 59 from July and 59 today are readings on two different
scales. The chip's tooltip says what that costs a comparison. A row written
before the stamps existed reads **"unstamped (pre-2026-09)"**, never a dash, a
zero, or the current version back-filled onto it; the same chip rides the
Situations frame, the Eval Scorecard card, the desk-baseline row and the Morning
Read's due forecasts, so one vocabulary covers every instrument on the reader
surface. `docs/ANALYSIS.md` §10.9.1 is the scale register.

Two selection-origin actions ride the header: add the selected finding to the
export basket, and create a server-side entity watch in one click with honest
watching / watched / failed states. Every number the Inspector and the boards
display carries a provenance badge — `live`, `fallback` or `absent` — rendered
with shape and label rather than colour alone; `fallback` is shown only on an
explicit backend signal, which no route carries today.

## Registering a panel

A panel kind is one registry row and one component.

1. Add the component under `src/panels/`, exporting a default that takes
   `PanelProps`.
2. Add a lazy import and a `def(...)` row to `panel-registry/registry.ts`,
   declaring the frontend **kind** (`system.foo`), the descriptor-facing **panel
   id** (`system_foo`), the **category** (`target` / `analyst` / `operator` /
   `system`), the **scope key** (`target_id`, `analyst_id`, or null for a
   singleton), the **default title**, whether it **requires a binding**, the
   **modes** it ships in, and an icon name. Tier defaults to `live`, with
   `PREVIEW_KINDS` promoting a guarded-preview surface and `HIDDEN_KINDS`
   dropping one from the sidebar, each in one place. Operator-category panels
   ship `personal`-only, which a registry test enforces.
3. Assign a nav group only if the prefix fallback would put it somewhere wrong,
   or to record the intent against a future change to that fallback: one line in
   `KIND_GROUP` (`panel-registry/navGroups.ts`).
4. Regenerate the matrix: `python3 scripts/gen_release_state_matrix.py`. A drift
   test fails the suite if the committed table and `registry.ts` disagree.

At runtime, `panel-registry/loader.ts` maps a registration to a mounted tile:
`resolvePanel` maps the descriptor panel id onto a bundle kind (an unknown id is
non-fatal and returns a placeholder rather than crashing the shell),
`extractScope` pulls the binding's scope id out, and `instanceId` builds a stable
Dockview panel id (`<kind>:<scope>`, or the bare kind for a singleton) so
re-opening a bound panel re-focuses its tile instead of duplicating it.
`useRegistry(mode)` owns the live instance list, fetching
`GET /api/v1/registry/ui_panels?mode=<mode>` on mount and refetching on any
`registry.>` event over the WebSocket — the SQL surface is authoritative and the
feed is a "something changed" nudge. Retired rows are filtered from the sidebar
but stay resolvable so deep links into them do not 404.

## Retired ids and hidden panels

A retired panel id keeps resolving forever, because saved layouts, palette deep
links, share hashes and registration rows all persist ids. The **alias table**
(`panel-registry/aliases.ts`) is one line per retired id naming its survivor and,
where the survivor is tabbed, the tab that *is* the retired surface. Three call
sites resolve through it: the panel resolver, both panel openers in `App.tsx`
(threading the tab through), and a pre-pass on both layout loaders that rewrites
stale ids, **collapses duplicates** (a layout holding three ids that now name one
tabbed panel becomes one tile) and drops the unresolvable before Dockview, which
has no per-panel fallback for an unknown component, can throw.

Aliasing is the retirement mechanism because hiding is expensive: a hidden kind
costs a registry row, a component import, bundle weight and a flag to remember,
where an alias costs one line of data. What stays registered-but-hidden is the
set an alias cannot describe because no shipped surface renders it yet — the
honest holding position until a merge gives it a survivor, since pointing an
alias at an approximation would silently lose the capability. Two rows in that
set are not retirements at all but DRILLS, hidden because they are opened from
the object they are about and a sidebar row would open them empty:
`system.optimizer.diff` (from the Optimizer queue) and `analysis.cross_framing`
(from a claim, a situation row, or ⌘K). Hiding a drill is the sidebar budget's
third option — earn / fold / hide — taken honestly, and it is why the 60th
registered kind cost the 26-row sidebar nothing. The current
aliases resolve `v4.timeline` onto Timeline (Events); `v4.why`, `system.lineage`,
`v4.flow`, `system.situations` and `system.narratives` onto Provenance;
`system.watchlist`, `system.alert_center` and `system.escalations` onto Alerts &
Watches; `system.deep_consult` onto Consult (Deep); and `system.entity_graph`
and `system.notable_structure` onto Entities.

Most cross-panel coordination flows through the selection store. Custom DOM
events survive only where an *open this panel* side effect is the point, so a
panel can drive another without knowing whether it is mounted:
`legba:open-lineage`, `legba:open-source-detail`, `legba:open-optimizer-diff` and
`legba:open-cross-framing`. The last two also PARK their subject on `window`
(`lib/crossFramingLink.ts` for cross-framing), which the target panel drains once on
mount — an event with no listener is lost, so the park is what makes a first click
survive the panel not being open yet.
The shell also listens for `legba:open-desk-brief` (which carries a `targetId` and opens the Desk Brief page
BOUND to that desk, through the same `addBound` opener the Investigate grids use,
so the tile dedupes with a sidebar or palette open of the same kind).

## Data access and the auth chain

**REST.** `lib/api.ts` is a thin client against `/api/v1`, attaching
`Authorization: Bearer <token>` only if a token exists in
`localStorage.legba_token` and surfacing errors as a typed `ApiError`; most
panels read through `@tanstack/react-query`. In the canonical production
deployment localStorage is empty and the app sends no `Authorization` header —
the perimeter supplies one.

**Live updates.** `lib/ws.ts::subscribeRegistryEvents` opens a WebSocket to
`/api/v1/registry/events?filter=<subject>`, the registry's event multiplexer,
with exponential reconnect backoff. The credential is never in the URL: when
localStorage holds a token the client offers it as a WebSocket **subprotocol**
(`legba.bearer.v1, <base64url token>`), which travels as a header, and the server
echoes back only the scheme name. A query-param token would be byte-identical to
the registry's admin credential and would land in every access log that records a
request line; the server still accepts one so a stale build does not hard-break
on deploy, and logs a deprecation event each time.

**The two-layer perimeter.** In production the app is served by `legba-caddy`
(`docker/Caddyfile`), which fronts the site at the public domain with automatic
TLS and proxies the registry upstream. Auth is two layers:

1. **Caddy `basic_auth`.** The browser prompts once per session and the password
   is checked against a bcrypt hash read from an environment variable, set in the
   gitignored environment file and never committed. It gates the app bundle
   itself — so the bundle is not publicly downloadable — and every API path
   except the event stream. Rotating it is one `caddy hash-password` away.
2. **Bearer injection.** After `basic_auth` validates, a `header_up` directive
   *replaces* the inbound `Authorization` with the registry's bearer for the
   upstream. The app never knows the registry token: it sends no `Authorization`
   header, the browser's cached Basic credential flows through, and Caddy swaps
   in the Bearer.

**The WebSocket exception.** The `new WebSocket()` API cannot attach an
`Authorization` header, so `basic_auth` on the event path would cause a 401 and a
re-prompt loop on every reconnect. The event path therefore routes through a
handle block that bypasses `basic_auth` and injects the Bearer directly; the
upgrade handler reads that injected header to authenticate. Security holds
because only requests through this Caddy get the registry token, so the registry
rejects an external direct hit.

Caddy polls the registry's unauthenticated readiness probe and drops a half-dead
upstream from rotation, so a registry that cannot reach its substrate fails
requests fast rather than hanging. If a stale token is left in localStorage, the
app sends a Bearer that collides with the Basic perimeter and triggers a
re-prompt loop; clearing `legba_token` and hard-refreshing fixes it. The status
bar shows `auth: ok` when a token is present and `auth: dev` otherwise. In
development, `npm run dev` proxies `/api` and `/ws` to the registry, so there is
no CORS and no Caddy in the loop, and the registry in dev accepts any token or
none.

## The mobile surface

A second, phone-first surface is served at `/m/` (`legba-ui-v3/src/mobile/`): a
navigator over the day's reads, a report reader, a journal view and a Consult
sheet. It is report-centric, with no map and no dock.

It is **its own bundle** (`vite.config.mobile.ts`, entry `m.html`, output
`dist/m/`) rather than a second entry in the workstation's build, because sharing
one Rollup graph re-partitions and re-hashes the workstation's chunks. Two
independent builds keep the workstation's output byte-identical, and
`npm run build` runs both, so one command still produces the whole tree. The cost
is that the bundles share no chunks, which a visitor would pay only by opening
both on one device.

One list request buys the session: the findings route returns each row's full
payload, and a read's payload already contains its blocks, their cited heads and
those heads' signals — so the request that paints the navigator also contains
every report body reachable from it, and opening a report costs no network.
Telemetry is the same instrument: the read-event vocabulary is closed at the
database and carries no surface column, so the honest carrier for "read on a
phone" is the free-text `workspace` dimension, set to `mobile`, and the
operator's morning reading counts once whichever screen they read it on.

Caddy serves it from the same volume behind the same perimeter, with its own SPA
fallback to `/m/index.html` rather than the workstation's, so a deep link like
`/m/?report=<id>` boots the phone bundle instead of silently serving a Dockview
app to a phone; a bare `/m` redirects to `/m/`. The mobile entry adds no new auth
path — it reads the same token and rides the same two-layer gate.

---

### Read telemetry

`lib/readTelemetry.ts` emits read events at seven chokepoint surfaces rather than
at call sites — panel opens, workspace switches, finding opens, citation drills,
provenance walks, consult, the morning read. It batches, flushes on page hide,
and is fail-silent in every path, because telemetry that can break the product it
measures is worse than no telemetry. A failed batch is dropped rather than
retried: a retry would backdate a week of reading into one minute and corrupt the
exact number the ledger exists to produce. The Read Scoreboard reads the rollup.

## Building and running

| Command | Effect |
|---|---|
| `npm run dev` | Vite dev server on `:5174`, proxying to the registry |
| `npm run dev:mobile` | The mobile surface's dev server |
| `npm run build` | `tsc -b && vite build && vite build --config vite.config.mobile.ts` → `dist/` and `dist/m/` |
| `npm run preview` | Serve the built bundle locally |
| `npm run lint` | `tsc --noEmit` type-check |
| `npm run test` | `vitest run` — panel logic that can be tested without a DOM lives under `lib/` by convention, so the tests do not need one |

In production, the `legba-ui-build` one-shot job builds into a named volume that
`legba-caddy` mounts read-only and serves with an SPA fallback. Key libraries are
`dockview` (workspace), `@tanstack/react-query` (data), `recharts` (charts),
`maplibre-gl` and `deck.gl` (maps — deck.gl dynamically imported only when a deck
layer is first switched on), `cytoscape` (graphs), `react-markdown` (reads and
consult) and Tailwind. See [RUNBOOK.md](RUNBOOK.md) for deploy and operations.

## Declared gaps

The authoritative list is [SEAMS.md](SEAMS.md). The console-relevant entries:
the catch-up replay is a runtime-plane operation with no registry proxy, so there
is no panel for it and nothing fakes one; the A2A skill surface is gated off by
default and fail-closed, with no panel; the provenance badge's `fallback` state
needs an explicit backend signal no route carries yet, so every displayed number
honestly reads `live` or `absent`; the map's co-mention arc layer is honest-empty
until multi-country geo lands upstream; and routing the journal's accepted
reflections back into the analysis spine is not built, though the Journal Gate
works the queue.

One console-side absence that is not a seam but is worth naming, because it is
visible on paper: the print document's citation endnotes carry a masthead — the
cited URL's own host — and the URL, but no publication date. `POST /v3/export`
resolves a cited signal to its title and `canonical_url` and nothing else, so
the endnote prints that slot as a stated absence rather than reading a date off
a headline or a URL path. An endnote with no masthead, date or URL at all (a
desk-grounding block, a sub-claim ref) says so in words.
