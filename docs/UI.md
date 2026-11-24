<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# The console

For people who read an instance through its web console, and for admins who build and serve it.
This page covers the console's pages, how to move around it, the phone shell, and how to build it.
A walk-through for a first visit is the [Tour](TOUR.md).

The console is `legba-ui-v3/`, a Vite, React and TypeScript single-page app served by the Caddy
edge. It has two sides: four **reader pages** for reading what the instance produced, and an
**operator side** for running the instance. A separate phone shell at `/m/` serves the reader pages
on a small screen.

## The frame

Every page has the same layout:

```
┌─────────────────────────────── top bar ────────────────────────────────┐
│ rail │  stage: the page's main view, or     │  panels: one to three     │
│      │  whatever you pulled up               │  columns, the map first   │
│      │ ──────────── divider ──────────────── │                           │
│      │  Consult, with the question box       │                           │
├──────┴──────────────────────────────────────┴───────────────────────────┤
│ time band · health line                                                 │
└─────────────────────────────────────────────────────────────────────────┘
```

- **Top bar.** The page tabs, a subject switcher, the command palette (`⌘K` / `Ctrl-K`), the `＋`
  view catalog, **Undo**, **Reset**, **Copy link**, and the **Operator** entry (on the operator
  side, **◂ Reader** leads back).
- **Rail.** On the reader side: subjects, your earlier Consult conversations, and saved views. On
  the operator side: reads, subjects, units, sources, recorded cases and the human queues. It
  collapses to icons and starts collapsed below 1600 px of width.
- **Stage.** The page's default view. Anything you open from the rail, the switcher or Consult
  lands here, with a breadcrumb back.
- **Consult.** The question box and the conversation. It sits under the stage by default.
- **Panels.** Up to three regions of views (built on [Dockview](https://dockview.dev)). Each view
  can be closed, moved to the stage with **show on the stage**, and reopened from `＋`.
- **Time band.** One shared time window. The map, the timeline and the scrubber all follow it.
- **Health line.** Four figures: sources firing, typed gaps on record for the current subject, the
  judge's mean faithfulness with its sample size, and how many subjects the correctness grader
  reached. Each opens its own panel when clicked.

The console uses the words your profile declares: the subject noun ("Desk" in the example
instance, "subject" by default), the read tiers, the map title. See [PROFILES.md](PROFILES.md).

## Reader pages

| Page | What it answers | Opens with | Key |
|---|---|---|---|
| **Board** | What changed, ranked | Today's read; the map and an at-a-glance panel | `Alt+1` |
| **Subject** | What is established about one subject | The subject's page; the map and its forecasts | `Alt+2` |
| **Ledger** | What was expected and what happened | Every sealed forecast; the track record and evaluations | `Alt+3` |
| **Watch** | Did anything happen that I asked to be told about | Alerts and watches; source health | `Alt+4` |

A subject's page has tabs: Brief, Evidence, Signals, Situations, Claims, Graph, Sources and
Runtime. The track record on the Ledger shows no score until five forecasts have resolved.

## Operator side

| Section | What it is for | Opens with | Key |
|---|---|---|---|
| **Engine** | Is the machine running | System Status; Registry, Flow, Model Stack Settings, Ledgers | `Alt+5` |
| **Evaluation** | How the checks are doing | Eval Boards; Read Scoreboard, Judge Stats, Layer Divergence, Contentions | `Alt+6` |
| **Gate** | The human queues | Journal Gate; forecast resolutions, Weekly Grading, Optimizer, Journal, Report Export | `Alt+7` |
| **Investigate** | Follow one thing to the bottom | Live Feed; Provenance, Register, Graph Walk, Entities, Timeline | `Alt+8` |

`Alt+backtick` cycles pages within a side. In the palette, `#` lists all eight.

## Checking a claim

Every `[N]` marker in a read is a citation chip. Click one and the **Record** sheet slides in over
the panels. It shows the statement as published, the record it maps to, the evidence with its
source, the fetch receipt (time, address, hash, stored copy) and the per-claim verdict from the
checking pass. From there the lineage walks back hop by hop to each source item. `Escape` or
`Ctrl/Cmd-I` closes the sheet.

Selecting any row, map point, graph node or id loads its detail in the **Inspector**, the same view
the Record sheet uses. `Alt+←` and `Alt+→` step back and forward through what you selected.

## Consult

Consult answers a question over the instance's own record, with its tool calls and citations. The
line above the box names what you are asking about: the current subject, the time window and any
records you pinned. **clear** drops that scope.

- The header buttons collapse Consult to a one-line ask bar, pop it out to the whole centre, or
  move it to a column on the left or right. The divider above it drags. The placement is kept per
  page.
- With **Drive the view** on, an answer can move the console: open views, select records, set the
  window. Each move is listed with **Stop** and one **Undo**. With it off (the default), an answer
  moves nothing.
- A change to the instance itself (a source, a descriptor setting, a fact) is never a view move.
  It waits for you to confirm it, and needs an admin when sign-in is on.
- A link of the form `#case=<session>:<turn>` replays a recorded answer's view moves.

## Moving around

- **Command palette** (`⌘K` / `Ctrl-K`). Searches subjects, views, pages, recorded cases, units and
  sources. `Enter` opens a subject's read; `⌘/Ctrl-Enter` only selects it. `>` limits the search to
  views and commands.
- **`＋` catalog.** Lists the views you can open, grouped, each with a one-line description. Each
  row can open its view on the stage or in the panels. Views that need a record to show (a single
  claim, one analyst's runs) are not listed; they open from that record.
- **Links.** The address holds the page, the subject, the selection and the time window
  (`#ws=`, `#scope=`, `#sel=`, `#win=`). **Copy link** copies it; **Save this view** on the rail
  keeps it in a list in your browser.
- **Undo** (`Ctrl/Cmd-Z` outside a text box) takes back the last view change. **Reset**
  (`Alt+Shift+R`) restores the page's default layout.

Layouts are stored per page in the browser. When a page's default changes in a new release, a
stored layout that no longer fits is replaced by the new default, and the top bar says so.

## Numbers that were not measured

A count that has not been read yet shows `—`. A read that failed shows `unavailable`. Neither is
ever shown as `0`; a `0` is a measured zero. Other phrases you will see:

| Phrase | Means |
|---|---|
| `unmeasured`, `not measured` | No measurement was taken. It says nothing about the value. |
| `no read yet` | The unit has produced no read for this subject. |
| `no typed absence is recorded` | The record was read and holds no gap. |
| `last known absence, not re-checked` | A recorded gap whose re-check is overdue. |
| `carried, ungraded` | A verdict carried from an earlier run, shown beside the averages and left out of them. |
| `absent, not contradicted` | A unit that said nothing, which is not the same as disagreeing. |

A composed read marks the unit reads it quoted that are older than three times their unit's
cadence (`LEGBA_STALE_READ_CADENCE_MULTIPLE`), and units whose status no new evidence has supported
for three cadence windows (`LEGBA_CARRY_EXPIRY_WINDOWS`).

## The map

The map (`v4.map`) draws the instance's subjects at the places their target descriptors declare,
and the layers the profile allows. It uses MapLibre, with Leaflet as a fallback where WebGL is
missing. The globe projection is a choice in the map's controls.

| Profile `map.geometry` | Result |
|---|---|
| `iso2` (the geopolitics example) | The map with every layer; subjects placed by their ISO 3166 codes. |
| `declared` | The map without the country shading and the co-mention arcs. |
| `none` (the sanctions skeleton) | No map. Opening one is refused with `no_map_surface`. |

Each layer has an `i` that states what it can see: the route and row cap, the source layers that
reach it, its time window, the subjects it reads and how a row gets a position. Rows with no date
of their own are not drawn on a time window.

## The Accuracy page

`system.eval_scorecard`, on the Ledger page's panel side, shows how well the instance's
reads hold up, in five sections:

1. **Cohort.** Which units and subjects are scored, over which window.
2. **Sealed ledgers.** The forecast ledger and the human-graded set, each stage with its count and
   denominator.
3. **Calibration.** Per-unit skill, band calibration and banded verdicts per subject.
4. **Graders.** Each grader's graded count, the population it drew from and what it left out.
5. **What this page does not publish.** Generated by `scripts/gen_not_published.py` from the
   measurement seams in [SEAMS.md](SEAMS.md) and the eval routes' own declarations.

## Sign-in and roles on screen

The console asks the registry who you are (`GET /api/v1/auth/me`) and shows one of these:

| Screen | When |
|---|---|
| The console | The registry names an identity: a signed-in person, the shared perimeter credential with sign-in off, or a service. Also when the check fails for any reason other than 401. |
| **Sign in** | The registry answers 401, or the address ends in `#signin`. |
| **Accept your invitation** | The address carries `#invite=<token>` from an admin's invite link. The password must be at least 12 characters. |

The chip at the right of the health line names who is signed in and their role, with **sign out**.

With sign-in on, the console hides what your role cannot do:

- A **reader** sees the four reader pages, the Record sheet, their own Consult sessions and the
  phone shell. The **Operator** entry says why it does not open. Grading, acknowledging alerts and
  every change control are replaced by *This needs a contributor or an admin*.
- A **contributor** can also grade, decide journal proposals and forecast outcomes, and acknowledge
  or snooze alerts.
- An **admin** can do everything.

The registry enforces every route whatever the console shows. Turning sign-in on is covered in
[Turning on sign-in](OPERATING_YOUR_INSTANCE.md#9-turning-on-sign-in); each route's minimum role is in
[API_ROLES.md](API_ROLES.md).

### Deployment modes

A build or a token can set a panel mode: `personal` (the default, every panel) or `cis` (a smaller
set). `?mode=` in the address overrides it. A mode only filters which panels are offered. It is
not a permission.

## The phone shell

`/m/` serves the reader side on a phone. A browser at most 480 px wide that opens the desktop
console is sent there; `?desktop=1` keeps the desktop for that tab.

| Tab or page | What it shows |
|---|---|
| **Changed** (landing) | What changed since you last looked, what paged, the ranked list and forecasts falling due. |
| **Watch** | The alert log, silences and source edits. A contributor or admin can acknowledge or snooze. |
| **Ledger** | Sealed forecasts, the track record and published evaluation runs. |
| **Consult** | The same Consult as the desktop, full screen. |
| **Subjects** | Search, recently opened, and the roster. A subject's page stacks its sections. |
| **Map** | A drawn map. The globe loads only when you ask for it. |
| **Record** | A short sheet for a citation, then the whole chain as a page. |
| **Settings** | Who is signed in, sign out, and a link to the desktop view. |

Links work the same as on the desktop: a `#rec=` link opens that record on the phone.

**Installing it.** With `LEGBA_AUTH_SIGNIN=1`, the edge serves the app manifest, the service worker
and the icons, and `/m/` can be installed from the browser (Android Chrome: the install prompt or
the ⋮ menu; iPhone Safari: Share, then Add to Home Screen). With sign-in off the phone shell works
in the browser but cannot be installed.

The installed app stores only its own code and icons. It never stores anything under `/api/`,
`/ws/` or `/tiles/`. Offline, it opens and says "No connection" with the time it last loaded
anything. A new version waits until you tap **Reload**. Signing out removes the worker and its
caches. There is no web push; alerts go to ntfy.

**Things to check on a real phone** before relying on the installed app: that it opens full screen
with nothing under the notch; that the session ends after the 12-hour idle limit; the offline
message; the **Update available** prompt; and that signing out leaves nothing behind. On iPhone,
check whether the app shows the perimeter password prompt. If it shows a blank page instead, run
with `LEGBA_EDGE_PERIMETER=0` so that sign-in is the only gate. iOS also clears an installed app's
data after about a week without use.

## The panel catalog

The views the console registers. Which side and mode each belongs to, and its release state, are
generated into [RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md).

| View | Kind | What it shows |
|---|---|---|
| Morning Read | `v4.morning_read` | Today's read: what changed, what was checked, gaps, per subject. |
| World Assessment | `v4.assessment` | A composed read, for the whole instance or one subject. |
| Map | `v4.map` | Subjects and record layers on a map. |
| The Wall | `system.wall` | At-a-glance counts, subject bands and movers since your last visit. |
| Subject page | `subject.page` | One subject's brief, evidence, signals, situations, claims, graph, sources and runtime. |
| Inspector | `system.inspector` | The detail of whatever is selected. |
| Consult | `system.consult` | Questions over the record. |
| Live Feed | `system.findings` | New findings and signals as they land. |
| Timeline | `system.timeline` | Events and validity windows on the shared window. |
| Alerts & Watches | `system.alerts_watches` | Standing watches, alert triggers and deliveries. |
| Source Health | `system.source_health` | Source quality, freshness and staleness. |
| Accuracy | `system.eval_scorecard` | The scored quality of the instance's reads. |
| Entities | `system.entities` | The entity roster, its relationship graph and notable structure. |
| Global Search | `system.search` | Search across signals, findings, situations and sources (client-side, preview). |
| Graph Walk | `system.graph_walk` | A step-by-step walk over typed relations, with the evidence for each edge. |
| Provenance | `system.provenance` | Lineage, receipts and source fan-out for one record. |
| Register | `system.register` | Situations with their trajectory, and contested narratives. |
| Layer Divergence | `system.layer_divergence` | How each source layer covers each subject. |
| Cross-framing | `analysis.cross_framing` | One claim against every unit that touches it. |
| Contentions | `analysis.contentions` | Records from the contrary-evidence pass. |
| Optimizer Candidates | `system.optimizer` | Prompt candidates with measured before and after scores, and their diff. |
| Journal | `system.journal` | The journal's entries. |
| Journal Gate | `system.journal_gate` | Accept or reject journal proposals. |
| Weekly Grading | `system.goldset` | Grade the week's sample of verified findings. |
| Report Export | `system.report_export` | Collect items and export them with citations. |
| Eval Boards | `system.eval_boards` | Subject baselines, band trajectory and run timing. |
| Judge Stats | `system.judge_stats` | The judge's verdict mix. |
| Read Scoreboard | `system.read_scoreboard` | What people read in the console. |
| System Status | `system.status` | Health per layer, actors, queues, failures and production. |
| Registry | `system.registry` | Subjects, units, sources, stack components and action packs, with editors. |
| Flow | `system.flow` | A read-only graph of sources, subjects, analysts and packs. |
| Ledgers | `system.ledgers` | The governor, audit and budget ledgers. |
| Model Stack Settings | `system.settings` | Model endpoint configuration. |
| Target Map, Target Timeline | `target.map`, `target.timeline` | One subject's signals and findings on a map or over time. |
| Analyst Runs, Outputs, Critiques | `analyst.runs`, `analyst.outputs`, `analyst.critiques` | One analyst's runs, findings and critique scores. |
| Cross-target Analyst | `analyst.cross_target` | Per-subject contributions for a cross-subject analyst. |

Older view ids (from saved layouts and links) still open: each resolves to the tab that replaced it
(`panel-registry/aliases.ts`).

## Building and running

Run these from `legba-ui-v3/` after `npm install`. The dev server proxies `/api/*` and `/ws/*` to
`VITE_API_BASE` (default `http://localhost:8501`).

| Command | Effect |
|---|---|
| `npm run dev` | Dev server on port 5174 |
| `npm run dev:mobile` | Dev server for the phone shell |
| `npm run build` | Type-check, then build `dist/` and `dist/m/` |
| `npm run preview` | Serve the built bundle |
| `npm run lint` | Type-check only |
| `npm run lint:tokens`, `npm run lint:absence` | Two ratchets: raw colour classes, and absent values rendered as `0`. Their ceilings may only go down. |
| `npm run test` | The unit tests (vitest) |

In a deployment, the `legba-ui-build` job builds into a named volume that the Caddy edge serves.
Only Caddy serves the console: a stack brought up without it (`deploy/deploy.sh --no-caddy`) has no
console.

To add a view: write the component, then register it with `registerPanel(contract, Component)`
(`panel-registry/`). The contract names the records it binds to, where it opens and the actions it
accepts. Run `python3 scripts/gen_release_state_matrix.py` afterwards; a test fails when the
matrix is out of date.

### The browser walk: every UI change is walked before it ships

`legba-ui-v3/tools/walk/` opens the console in headless Chromium as a first-time user and fails on
broken budgets: slow tasks, horizontal overflow, a cramped Consult box, a `0` shown before its
count was read, console errors, text under 12 px. Run it against the committed fixtures before a
change and against the live bundle after a deploy:

```bash
legba-ui-v3/tools/walk/run.sh fixture --build
LEGBA_TOKEN=... legba-ui-v3/tools/walk/run.sh live
```

The walk's [README](../legba-ui-v3/tools/walk/README.md) lists every step and budget.

## Declared gaps

The full list is [SEAMS.md](SEAMS.md). The ones you can see in the console:

- No panel for catch-up replay; it is a runtime operation with no registry route.
- The map's co-mention arcs stay empty while enrichment writes one country per signal.
- A subject cannot declare a point or polygon geometry yet (SEAMS #66).
- Accepted journal reflections do not flow back into the analysis.
- Some regions on the reader pages are placeholders (the Board's coverage strip, the Subject
  page's forecasts and series). Each says what it is waiting for.
- Exported citation endnotes carry the source host and URL but no publication date.
