<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# The console

`legba-ui-v3` is the console Legba is read through: a single-page Vite + React +
TypeScript app with two sides — four READER surfaces (Board, Subject, Ledger,
Watch) and, behind one separate entry, the OPERATOR sections (Engine,
Evaluation, Gate, Investigate). A second shell for phones serves the reader
surfaces at `/m/` ([the phone shell](#the-phone-shell)). Who may use which side
depends on the person's role when sign-in is on ([the sign-in gate](#the-sign-in-gate)). Every page is the same FRAME: a slim rail, a
centre at a reading measure split into a **stage** (today's read, or whatever
is pulled up) over **Consult** (the chat, movable and collapsible), and
**panels** to the right in the remaining width — a constrained
[Dockview](https://dockview.dev) layout of one to three columns. Each panel
holds one or more **views**, each a self-contained read (or authoring) surface
over one slice of the substrate, the registry, or the runtime, and each view
declares a **contract**: what it binds to and the actions it accepts, so a
click, a link and Consult drive it the same way.

The console is descriptor-driven in the same sense the rest of the platform is.
Two registries cooperate: a **bundle-time panel registry**
(`legba-ui-v3/src/panel-registry/registry.ts`), the static catalog of every panel
*kind* the app ships; and the **runtime descriptor registry**, the live list of
panel *instances* — one per bound target or analyst — fetched over REST and kept
current over a NATS-fed WebSocket. Adding a panel kind is a code change: one
registry row and one component. Adding a panel *instance* is a descriptor
change, and the binding shows up in the object rail with no UI rebuild.

The console states what it measures. Legba grades **groundedness** — does each
claim follow from its cited evidence? — and, separately, **correctness** against
independently built references; the panels show a weak or unmeasured result as
weak or unmeasured, and an empty space is the truthful render for something the
platform has not earned a number for.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the planes, registry and runtime,
[ACQUISITION.md](ACQUISITION.md) for the source / signal / fan-out model the
panels read, and [ANALYSIS.md](ANALYSIS.md) for the analysts behind the products.

**Contents:** [The shell](#the-shell) · [Surfaces and the arrangement](#surfaces-and-the-arrangement) · [The frame](#the-frame-two-sides-eight-pages-one-frame) · [The panel contract](#the-panel-contract-what-every-view-is-and-what-consult-can-do-to-it) ·
[Deployment modes](#deployment-modes) · [The panel catalog](#the-panel-catalog) ·
[The map](#the-map-subject-geometry-and-layer-apertures) · [The Accuracy page](#the-accuracy-page) ·
[The Inspector](#the-inspector) · [Registering a panel](#registering-a-panel) ·
[Retired ids](#retired-ids-drills-and-the-kind-ratchets) · [Auth chain](#data-access-and-the-auth-chain) ·
[The phone shell](#the-phone-shell) · [Read telemetry](#read-telemetry) · [Building](#building-and-running) ·
[Declared gaps](#declared-gaps)

## The shell

The root component (`src/App.tsx`) is the FRAME every page uses:

```
┌──────────────────────────────── top bar ──────────────────────────────────┐
│ Legba │ Board Subject Ledger Watch │ [subject ▾] │ ⌘K ＋ ↶ ↺ 🔗 │ Operator ▸ │
├──────┬──────────────────────────────┬──────────────────────────────────────┤
│ rail │  STAGE — today's read; what  │  PANELS — Dockview, 1–3 columns by   │
│ subj │  was pulled up (breadcrumb)  │  width, the map leading; the Record  │
│ hist │ ══════ divider (drag) ═══════│  sheet slides over them              │
│ view │  CHAT — Consult, the ask box │                                      │
│      │  at its foot                 │                                      │
├──────┴──────────────────────────────┴──────────────────────────────────────┤
│ time band (reader side) — the one clock                                     │
│ health line — sources n/N firing · gaps · judge · grader                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

**The top bar** (`components/SurfaceBar.tsx`) — the mark, the four reader
surfaces as tabs, the **subject switcher** (the subject roster, grouped, the
same list the rail shows; the current point is named on the button), and at
the right ⌘K, the `＋` view catalog, **Undo** (the last view change),
**Reset**, **Copy link** and one separate
**Operator** entry. On the operator side the tabs are the four operator
sections and a **◂ Reader** button leads back. The arrangement model, the
surfaces and their keys are under
[Surfaces and the arrangement](#surfaces-and-the-arrangement).

**The `＋` catalog** (`components/CatalogPopover.tsx`) hangs from the top bar.
On the reader side it lists only the reader's views
(`surfaces.ts::readerKinds`, less Consult and the Record sheet, which are
frame); on the operator side every view that renders on its own in the active
mode — a DRILL, a kind whose params need a ref, is never listed; it opens from
its parent object. Rows are grouped (Read, Subjects, Map, Forecasts and proof,
Watch, Records, Operator), each prints its one-line description under its name,
the filter searches names, ids, descriptions and groups, and each row can put
the view on the stage or in the panels ([the panel contract](#the-panel-contract-what-every-view-is-and-what-consult-can-do-to-it)).

**The rail** (`components/FrameRail.tsx`) — every page has it, collapsible to
a strip of icons (it starts shut below 1600 px). On the READER side it holds
the **subjects** (the roster, grouped — a click reads the subject on the
stage), Consult's prior **conversations** (a click continues one in the chat),
and **saved views** (links: a page, its point and its window; *Save this view*
keeps the current one). On the OPERATOR side it is the object rail below.

**Object rail** (`components/ObjectRail.tsx`) — on the OPERATOR side, the
rail lists the instance's OBJECTS, never panels. Six
sections, in a fixed order, labelled from the profile (`lib/profile_ui.json`,
below):

| Section | What it lists | A click is |
|---|---|---|
| **Reads** | the read index by frame (tier) — the top floor's newest read first, then each tier's freshest read per subject. The tiers are the projection's `tiers` when the plane answers, else the packaged profile's frames; a refusal is named under the section | `set_scope` + `focus` on the read |
| **Subjects** (the profile's plural) | every subject, by its DECLARED name, grouped by the frame family's subject groups (the projection's `subject_groups`), else by the roster leg it declares, with the refusal named above the groups (`SubjectRoster`, which the reader side's switcher also renders) | the subject's READ (`lib/subjectRead.ts`): `set_scope`, `focus` and `open_panel subject.page` on the Brief tab — the Subject surface follows it; its `⋯` menu opens its bound reads |
| **Units** (the profile's `unit_noun`) | the profile's unit roster | `focus` on the unit; its `⋯` menu opens its bound reads |
| **Sources** | the source roster, with a filter | `focus` on the source |
| **Cases** | every consult turn in the store that presented a case | a replay in Consult |
| **Queues** | the human queues — journal proposals pending, the week's grading still to grade, optimizer candidates pending — each count with its as-of | `open_panel` on the queue's surface |

The two long rosters start folded. A count not read yet is `—`; a body that is
not the route's shape is `unavailable`; neither is ever `0`. The rail's old
LAYOUTS block (*Open a workspace…*, Save, Restore), its missions and the saved
missions are gone with the machinery they drove.

**One subject noun**. The thing an analyst studies is `subject`,
`target`, `desk` or `country` in code; on screen it is only the profile's
subject noun (`lib/subjectNoun.ts`: the exemplar's profile says *Desk*, the
platform default is *subject*) — the Subject surface's tab, the switcher, the
rail, ⌘K's rows, the Inspector's kind chip and the drill menu pass through it;
a subject's declared name is data and is never rewritten.

**The look** (`globals.css`, `tailwind.config.ts`; layout design §1). ONE dark
theme, by design, with no light theme, no theme
switch and no stored theme preference (a browser that stored `light` lands on
the one theme). A deep slate ground, never black; regions divided by hairline
rules, never boxed cards, with a fill only where the content differs in kind
(Consult, a popover, the Record sheet); one accent, chart magenta
(`--point`, Tailwind `point`), for the reader's point and Consult's actions and
nothing else; good, warning and critical as their own colours. Three faces,
self-hosted as woff2 in the bundle (SIL OFL 1.1, latin subset, licences in
`src/assets/fonts/`; no font host is contacted at runtime): **Source Serif 4**
for statements, **IBM Plex Sans** for the interface, **IBM Plex Mono** with
tabular figures for data. The type scale is 12 · 13 · 14 · 16 · 20 · 28 px
(`text-label` · `text-body` · `text-body-lg` · `text-heading` ·
`text-heading-lg` · `text-display`), and 12 px is the floor on the frame and
the reader surfaces' shell chrome. The scale is in rem and the root steps up on
big screens (16 px below 2200 px of width, 18 px from 2200, 24 px from 3000),
so on a 3840-wide screen every size is 1.5× (the floor becomes 18 px). The token names (`--surf-*`, `--ink-*`,
`--line-*`) are the ones every panel already read, so the re-skin is values,
not call sites.

**Labels come from the profile and the projection** (`lib/profileUi.ts`).
The console spells no instance's word: every label, tier, roster
leg and subject name is read, in this precedence —

1. **The packaged profile** — PROFILE-1's `scripts/gen_profile_ui.py` exports
   the profile's `ui` key (plus its roster tags and its projection `plane`)
   into `lib/profile_ui.json` (schema `legba/profile_ui/1`; see
   [PROFILES.md](PROFILES.md)). It is synchronous, so every label paints on
   the first frame and never waits on a route: the subject and unit nouns, the
   frames and their producers (the read tiers, top floor first), the map,
   read and overview titles (`v4.map`, `v4.assessment`, the Wall), the
   unscoped-subject label, the roster labels, and the subject noun the
   Subject surface is named by. A key the profile does not declare renders the PLATFORM noun
   ("subjects", "units", "Map", "Read"), never a blank and never the
   exemplar's word.
2. **The projection** — `useProjection(section)` reads `GET
   /registry/projection/{plane}/{section}`. A surface takes the
   section when the plane answers `ok` with rows: the Reads rail and the phone
   navigator take `tiers`, the Morning Read's bands take `bands` (by band id;
   the order and ids stay the platform's reading order), and the rail's
   Subjects take `subject_groups`. A refusal is a typed absence `{state,
   reason}` printed BY NAME where the section is read — `projection_disabled`,
   `projection_empty`, `projection_stale`, `plane_unknown`,
   `section_undeclared`, or the transport's `projection_unreachable` — and the
   surface keeps the packaged profile (tiers) or the platform's default
   (bands, the roster grouping): the rail's `rail-projection-*` lines and the
   Morning Read rail's `data-bands-source` / tooltip say which.
3. **The subject's own declaration** — a subject's display name and roster leg
   come from its target descriptor (`lib/subjectIndex.ts` + `lib/deskNames.ts`,
   over `GET /registry/descriptors?family=target`, read once by the shell):
   under an `iso2` profile a `scope.domain: geo` subject declaring one ISO code
   reads as the gazetteer's name; any other subject reads as its declared name
   (`<qualifier> — <name>` reads as the name); its roster leg is the first of
   the profile's `rosters.subject_tags` its `scope.tags` carries. No subject id
   is parsed anywhere (the geometry contract's rule).

The profile's `ui.stances` / `ui.missions` labels name the RETIRED workspace
and mission ids; the shell no longer renders them (the surfaces are named by
the shell, and the Subject surface by the profile's subject noun). The
geopolitics export reproduces today's labels byte for byte;
`tests/test_profile.py` asserts the golden equals the profile manifest and that
no TypeScript site spells a profile label; `profileNone.surfaces.test.tsx`
renders the sanctions skeleton's own words on every surface above and greps its
landing's DOM for the exemplar's (no *G20*, *Regions*, *Countries*, *World
Map*).

**The dock** — a `DockviewReact` instance whose every view resolves its bound
registration to a lazy-loaded component, so a panel's chart or map libraries load
only when its view is first opened.

**StatusBar** (`components/StatusBar.tsx`) — the **health line**, the honesty footer: a standing,
unprompted statement of how much of the intelligence the reader is reading was
actually covered, in four figures, each over one of our own objects and each
sourced from one live route. Beside it sit the console's own small chrome: the
last registry refresh, the identity chip (below), any
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
At its right end sits the **identity chip** (`auth/IdentityChip.tsx`): who
the registry says is at this console, read from `GET /api/v1/auth/me` —
`<name> · <role>` (and `· owner` for the owner) with **sign out** for a
signed-in person; `operator · perimeter` with **sign in as a user** when sign-in
is off and the edge's shared credential is in use; with sign-in on, a stored
service or admin token names itself (`admin token · break-glass`) beside
**sign in**; `dev · operator` for a dev registry; `identity —` before the read
lands and `identity unavailable` when it fails (never a made-up identity). The
role printed is the registry's: `reader`, `contributor` or `admin` with sign-in
on, the older classes `read` / `reader-own-rows` / `operator` with it off.

### The sign-in gate

`main.tsx` mounts the shell inside ONE function, `auth/SignInGate.tsx`'s
`SignInGate`. It renders exactly one of these, decided by the registry's
answer to `GET /api/v1/auth/me` and the address — never by a client guess:

| Screen | When | What it does |
|---|---|---|
| **the shell** | `/auth/me` names any identity (a signed-in person; the shared perimeter credential with sign-in off; a service; dev) — and ALSO when `/auth/me` fails with anything but a 401 | renders `<App/>` unchanged. The gate is the door, not the lock: the registry enforces every route, and an outage must not hide the surfaces that report it |
| **Sign in** (`auth/SignInScreen.tsx` · `SignInScreen`) | `/auth/me` answers **401** (no credential the registry accepts — a session that ended, a disabled account, no perimeter in front), or the address carries `#signin` (the chip's *sign in as a user*; a signed-in caller gets **Back to the workstation**) | email + password → `POST /api/v1/auth/sign-in`; the registry sets the HttpOnly session cookie and the shell reloads onto it. A refusal prints the registry's own `detail.message` (`email or password is wrong`, `the account … is disabled`, `too many failed sign-ins …`) |
| **Accept your invitation** (`auth/SignInScreen.tsx` · `InviteAcceptScreen`) | the address carries `#invite=<token>` — an admin's invite link (a URL fragment, so the token never reaches an access log) | an optional name + the chosen password twice (at least 12 characters; the count and a mismatch are shown as typed) → `POST /api/v1/auth/accept`, which activates the account and signs in. A used or expired link is named as such |
| *checking sign-in…* | while `/auth/me` is in flight | one line; nothing of the shell renders |

The person's identity, role and private consult history are the registry's
([API_ROLES.md](API_ROLES.md); the users table is migration 0236, the roles and
the owner flag 0255); this gate only decides which screen a browser sees first.

**What a role changes on screen** (`auth/roles.ts`). With sign-in on, the
console reads the role from the same `/auth/me` answer and keeps itself from
sending requests the role will be refused:

- A **reader** sees the four reader pages, the Record sheet, Consult (their own
  sessions only) and the phone shell. The **Operator** entry stays visible but
  says why it does not open; the command palette and the `＋` catalog list
  reader views only. Drift flags, a subject's runtime, alert deliveries and
  descriptor configuration show a plain reason instead of a refused request.
  Acknowledge and snooze on an alert, grading and every change control are
  absent, with the line *This needs a contributor or an admin*.
- A **contributor** also grades, decides journal proposals and forecast
  outcomes in the Gate, and acknowledges or snoozes alerts. Changes to
  configuration and Consult turns that write still need an admin (*This needs
  an admin*).
- An **admin** sees and does everything.

When the role is not known (sign-in off, or `/auth/me` unavailable) the console
behaves as it always did, and the registry still refuses what it refuses: the
gate is the door, not the lock. Groups and per-source permissions are not built
(SEAMS #83, #85), so every role reads the same shared record.

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

`Ctrl/Cmd-K` opens the command line (`components/CommandPalette.tsx`). One
fuzzy query resolves across every family, and every family compiles to drive
actions run by the one executor (below). Opened empty it is ordered by FAMILY
after the person's own recents and favorites — **subjects first, by
recent activity** (the newest read on record, newest first, each row carrying
its age; a subject with no read on record says so and follows), then panels,
surfaces, cases, commands, units, the Investigate actions,
sources, records that are not active (a draft subject), and **paused sources
last**, marked `source · paused` (a paused source also sinks below every other
match under a query). **Records**: a subject is named by its declared name and
the profile's noun, never `target`; its `Enter` opens its read exactly as a
rail click does and `⌘/Ctrl-Enter` only focuses it; a unit's or a source's
`Enter` → `focus`, `⌘/Ctrl-Enter` → `open_panel` on the record's bound
primary. **Panels** (the `＋` catalog — on the reader side a view that lives on
the operator side is still found, hinted *operator side*, and choosing it
crosses there), **surfaces** (`arrange`: the four reader surfaces and the four
operator sections), **cases** (every recorded case in the Consult store —
choosing one replays it), **actions** ("Investigate · X": `focus` plus the
record's reads) and **commands** (the shell family this shell carries). A
leading sigil narrows the search: `#` to surfaces, `>` to panels and commands.
The workspaces, missions and presets families are gone with the arrangement
cut.

Dockview keeps inactive tab content mounted but hidden, which blanks any surface
that measures itself at mount, so two shared hooks own that class of bug rather
than each panel re-solving it: `useDockviewTileRedraw` redraws and re-keys a tile
a frame after it becomes visible, and `useElementWidth` measures container width
through a callback ref so the observer follows the element rather than the
mount.

### Primitives and absence contracts

**Five primitives, copied in.** `src/ui/` holds the workstation's only
overlay and control primitives: **Dialog**, **Popover**, **Tooltip**, **Tabs**
and **Select**, each a shadcn/ui component copied in as source (the MIT notice
travels in each file) on its Radix package, `@radix-ui/react-{dialog,popover,
tooltip,tabs,select}`, and drawn on the tokens. The set is held at five on
purpose. Every overlay portals into one explicit host element beside the
Dockview workspace (`ui/portal.tsx`), never onto `<body>`, so it inherits the
user's density and is positioned against the viewport rather than inside a
tile's GPU layer. What runs on them: the command palette and the Flow wiring
modal are Dialogs (Escape and the backdrop close, Tab is trapped inside, focus
returns to whatever opened them); `PanelTabStrip`, the merged panels' tab bar,
is a Tabs list (one Tab stop, ←/→ and Home/End move and activate);
`ScopePicker` is a Select (Enter/Space/↑/↓ open it, typeahead, Escape closes);
`InfoTip` is a Tooltip that opens on hover, on keyboard focus and on a tap or
click, flips at the viewport edge, and keeps its explainer in the DOM as the
trigger's `aria-describedby` description. Popover is the surface the rail's `＋`
catalog builds on.

**Tokens.** `globals.css` is the token set: the surface ramp L0–L4
(`--surf-base` … `--surf-4`, the overlay level), ink, three line alphas
(`--line-0` row dividers, `--line-1` borders, `--line-2` interactive), the
three colour channels (severity, confidence, system state), `--accent` and
`--accent-emphasis`, `--row-h` (24 / 32 / 40 px by density), `--font-ui` and
`--font-mono` (ids, hashes, timestamps and ratios set in mono with tabular
figures), and two radii (`--radius-control`, `--radius-panel`). There is one
focus ring: `:focus-visible` only, in `--accent-emphasis`. The dock's look is a
Dockview theme **object** (`ui/dockviewTheme.ts`, class
`dockview-theme-legba`) whose stylesheet maps every `--dv-*` variable onto a
token; a region's header is a hairline over the ground, the visible view
marked by a 1 px underline. There is one theme (dark).

**Absence contracts.** A displayed number is a `Figure` (`lib/Figure.tsx`):
`{value, unit, asOf, state}`, where `asOf` is `string | {absent: reason}` and is
required, `state: 'measured'` requires a number and every absent state forbids
one — so a figure without an as-of, or an absent reading defaulted to `0`, does
not type-check. A measured zero prints `0`; an absent reading prints a phrase
from **the absence vocabulary** (`lib/absenceVocabulary.ts`), one closed list,
each phrase with its meaning:

| Phrase | Means |
|---|---|
| `not measured` / `unmeasured` | no measurement was taken; says nothing about the value |
| `carried, ungraded` | a memory-only verdict, counted beside the means and never in them |
| `floor honoured` / `kept by the floor` | every group that offered was carried / this block is in because the floor kept its group represented |
| `last known absence, not re-checked` | a typed absence whose re-check fell due and has not run |
| `no typed absence is recorded` | the record was read and holds none — a real answer |
| `absent, not contradicted` | silent units are not dissenting ones |
| `no read yet` | the unit has produced no read for this subject |
| `—` | nothing to show, and the label already says why |
| `writes: none` | a write-armed consult turn ended with no write entry |

Every panel kind answers to an absence contract — its Table A line in the
workstation design (`panel-registry/absenceContracts.ts`; a test walks the
registry). Two ratchets run with `npm run test`: `tools/lint-tokens.mjs`
(raw palette classes; ceiling in the `lint:tokens` script, the shell, the rail
and the primitives held at zero) and `tools/lint-absence.mjs` (`?? 0`, `|| 0`
and `toFixed` on a nullable in the render paths; ceiling in `lint:absence`).
Either number may only go down.

**Stale quoted reads.** A composition marks each unit read it quotes
against the unit's own cadence (`data/analysts/composition_stale_reads.py`):
a read older than the unit's cooldown × `LEGBA_STALE_READ_CADENCE_MULTIPLE`
(default 3) carries `stale: true` and its `cadence_h` on its `data.head_ages`
head and on its `assembly.coverage` row, and the record counts them in
`data.stale_unit_reads`. Two readers print it, one component each: the
composition reader's coverage line (`CoverageLine`, `v4/read/DropLedger.tsx`)
appends "Quoted stale (n): <unit> read 94 h old (cadence 11 h)", and the desk
brief's evidence table (`EvidenceTable`, `panels/target/DeskBriefSections.tsx`)
adds "quoted stale: read 94 h old" under the unit's last-read date. A read
with no known cadence carries no mark and prints nothing — never "fresh".

**Carried, unsupported.** A unit whose status none of its own
indicators re-evidenced inside N of its cadence windows
(`data/carried_state.py`, `LEGBA_CARRY_EXPIRY_WINDOWS`, default 3) is named on
the composition's coverage row with status `carried_unsupported`, its
`carried_status` and `last_evidence_age_h`. The same two components print it,
one line each: `CoverageLine` appends "Carried, unsupported (n): <unit> last
re-evidenced 336 h ago" ("not re-evidenced in the lookback" when there is no
such read), and the desk brief's `EvidenceTable` puts the unit in the
`carried, unsupported` evidence state with "carried, unsupported: status
'moderate' held; last re-evidenced 336 h ago" under its last-read date. On a
subject no composition reads (a flow or lane desk) the same verdict arrives as
the ninth typed-absence kind in the table's "Typed absence" column.

## Surfaces and the arrangement

### The command model — one registry, one executor

`lib/commands.ts` is the ONE command registry, in three families:

| Family | Members | Drivers | Replayable | Executes |
|---|---|---|---|---|
| `drive` | the nine `case/1` actions — `open_panel`, `arrange`, `set_scope`, `focus`, `map`, `timeline`, `run_mission`, `narrate`, `done` | a person (⌘K, the rail, the `＋` catalog, the top bar, the URL, a panel's cross-link) and consult (through the `workstation_drive` gate) | yes | `applyAction` |
| `shell` | reset the surface, copy a link to the view, print, PNG, density, close the opened view, palette search | a person only | never | a local handler the shell registers |
| `write` | Consult's write tools and a person's confirm / cancel / revert | consult emits, a person confirms | never | the registry — **the shell refuses every write by name** |

**One executor.** `lib/applyAction.ts::applyAction(action, opener)` is the only
code that moves the view; `App.tsx` registers the shell port it drives (the
home-region openers for singletons and bound reads, the surface switcher, the
retired-preset and retired-mission translations, serialize / restore). Two
drivers share it: `driver: 'case'` keeps the closed vocabulary's client-side
refusals (`kind_not_drivable`, a layout or mission the vocabulary does not
list); `driver: 'operator'` may open any registered kind and go to any surface.
`arrange` is the wire name for going to a surface (a surface id, or a retired
workspace or preset id); `run_mission` survives only for retired mission ids. A
drill's params (the claim for Cross-framing, the
candidate for the prompt-module diff) reach its tile through
`state/panelParams.ts`, written before the tile is opened, so a first open and
an already-open tile both land on the request and a remount never replays a
consumed one.

**The DOM bridges are retired.** `legba:open-cross-framing`,
`legba:open-optimizer-diff` and `legba:open-desk-brief`, and the `window` parks
behind the first two, are gone: each former call site drives one `open_panel`
(`bridgeActions`). A surface reachable only by an event was unaddressable; now
every open is the same action a case can record. A test fails the suite if any
source file dispatches a `legba:open-*` event or parks on `window` again.

**The URL is a saved view** (`lib/address.ts`). `#ws=` (a surface, or a
retired workspace id), `#scope=`, `#sel=`, `#win=` (the window, one of the
scrubber's spans) and a retired `#mission=` compile, in that order, to
`[arrange, set_scope, focus, timeline, run_mission]`, and an action list writes
back to the same address; the shell mirrors the surface, the scope, the
selection and the window into the hash as they change, so the address bar
always holds the current view. `#case=<session>:<turn>` (the phone reads
`?case=` the same way) opens that consult turn's recorded case as a REPLAY in
Consult — `<turn>` is the turn's id or its 0-based position; a turn
that presented no case, or a session that cannot be read, says so on the bar
and replays nothing.

### The clock: one window, two kinds of time

**One clock** (D6). `useWorldState`'s window is the only writer of *when*: the
map, the scrubber (the time band), a link's `#win=`, a retired mission's span
and a case's `timeline` step all write it, and every one of those writes lands in
`setWindow` (a span preset's `setSpan` records the outer span, then calls it).
Both modes of `system.timeline` follow it — the validity windows and the Events
lanes — centred on a case's `at` when a case drives the clock; in each, zoom,
pan, fit and the selection re-centre are a VIEW LENS that never writes the
shared window, a **re-centre** control drops the lens, and a new shared window
drops it too. The validity mode's day select is the fetch depth, widened
automatically to cover the shared window.

**Temporal layers: valid time and record time** (`lib/temporalLayers.ts`,
`lib/useTemporalRows.ts`). Every row on a clock has two instants, and they are
different clocks: **valid time**, when the world was so, and **record time**,
when the platform learned it. A temporal layer is one clock over one source in
one window — `{id, time: 'valid' | 'record', window, source}` — read off what
the routes already return:

| Source | Valid time | Record time |
|---|---|---|
| signals | the publisher's date (`data.published_at`) | `created_at` — the `/signals` route serves the row's fetch instant there, and on `event_timestamp` and `produced_at` too |
| findings | `produced_at` | `created_at` |
| situations | `last_event_at` | `created_at` |
| events | `valid_from`, else `time_start` | `produced_at` |

A row without an instant on a clock is absent from that clock — a signal whose
source printed no date has no valid time, and is never placed at its fetch time
instead. **The scrubber** draws both clocks under its thumbs: one density strip
per clock over the outer span (the window's stretch lit), and one line per
clock naming each on-layer's rows in the window, *no read yet* for a layer with
none, *unmeasured* for a route that did not answer, and the as-of — the latest
**record** time among the rows counted, because "as of" means "what we had
learned by then". **The Events lanes** plot either clock (the `valid time` /
`record time` switch, a view choice that never moves the window), print both in
every mark's tooltip, count the rows the plotted clock cannot place ("N without
a valid time — not drawn"), and give each lane a figure over the view with its
as-of, or *no read yet*. **A history citation keeps its tense marker
verbatim**: the producer's `(historical: valid YYYY..YYYY, recorded YYYY-MM)`
carries a cited figure's valid and record time in one string, and the citation
card and the desk brief print it byte for byte — `????` slots included — with
no surface re-deriving it (`lib/temporalLayers.tense.test.tsx`).

### The frame: two sides, eight pages, one frame

An earlier shell (three Dockview regions per surface and Consult as a fixed
380–480 px column on the right) placed Consult badly: a fixed side column could
not be resized or moved and left space unused. The frame below replaced it. Its
design goals: Consult sits in the centre, in a chat-like
layout, below the stage at about half height, so the conversation stays visible
with room for content above it; it can be resized and moved; the space is used
fully; and the frame leaves room for many more panels, for other pages, and for
Consult acting on panels. The model is `lib/surfaces.ts` (what a
page shows), `lib/frameGrid.ts` (the numbers), `state/frame.ts` (the chat, the
stage, the rail) and `lib/surfaceDock.ts` (the panel side in Dockview).

```
┌──────────────────────────────── top bar ────────────────────────────────────┐
│ rail │  STAGE — today's read; what was pulled │  PANELS — 1 to 3 columns     │
│      │  up, with a breadcrumb back            │  by width, the map leading   │
│      │ ═════════ divider (drag) ═══════════════│  (Dockview: resize, swap,    │
│      │  CHAT — Consult, the ask box at its foot│  close, ＋ to re-open)       │
├──────┴─────────────────────────────────────────┴──────────────────────────────┤
│ time band (reader side) · status line                                          │
└────────────────────────────────────────────────────────────────────────────────┘
```

**Two sides.** Infrastructure and technical views are kept apart from the
reading side of the workstation. The top bar carries the four READER pages
and, at its right end, one separate **Operator** entry; on the operator side
the bar carries the four operator sections and a **◂ Reader** button.

| Side | Page | Its one job | Stage (default) | Panels (lead first) | Chat | Key |
|---|---|---|---|---|---|---|
| reader | **Board** (the landing) | what changed, ranked | Today's read | the map (3) · At a glance, Coverage (a slot) (2) | open | `Alt+1` |
| reader | **Subject** (the profile's noun) | what is established about one subject | the subject page | the map (3) · Forecasts here (a slot) (2) | open | `Alt+2` |
| reader | **Ledger** | what was expected, what happened | Forecasts (every sealed forecast: its probability, rule, state and seal) | Track record (no score below five resolutions) · Evaluations, Accuracy | open | `Alt+3` |
| reader | **Watch** | did anything happen I asked about | Alerts and watches | Source health | open | `Alt+4` |
| operator | **Engine** | is the machine running | System Status | Registry, Flow, Model Stack Settings (3) · Ledgers (2) | ask bar | `Alt+5` |
| operator | **Evaluation** | how the checking machinery is doing | Eval Boards | Read Scoreboard, Judge Stats, Layer Divergence · Contentions | ask bar | `Alt+6` |
| operator | **Gate** | the human queues | Journal Gate | Forecast resolutions, Weekly Grading, Optimizer · Journal, Report Export | ask bar | `Alt+7` |
| operator | **Investigate** | follow one thing to the bottom | Live Feed | Provenance, Register, Graph Walk (3) · Entities, Timeline (2) | ask bar | `Alt+8` |

(The number after a region is its width weight when it has a column of its
own.) `Alt+backtick` cycles within the side, `Alt+Shift+R` resets, `⌘K → #`
lists all eight.

**The grid** (`lib/frameGrid.ts`; every measure is in rem, so one root size
scales the whole console). The root type size steps up on big screens — 16 px
below 2200 px of width, 18 px from 2200, 24 px from 3000 (the media queries in
`globals.css`) — so text at 3840 is 1.5× its 1920 size.

| Viewport | Root | Rail (open / shut) | Centre measure | Min panel column | Panel columns |
|---|---|---|---|---|---|
| 1366 | 16 | 240 / **48** (starts shut) | 576 | 480 | 1 |
| 1920 | 16 | **240** | 768 | 480 | 1 |
| 2560 | 18 | **270** | 864 | 540 | 2 |
| 3840 | 24 | **360** | 1152 | 720 | 3 |

The centre is `clamp(36rem, 40vw, 48rem)` — a reading width that grows
modestly and never runs unbounded; the panels take ALL the remaining width (no
dead band), in as many columns as the width affords at 30 rem each, capped at
three and at the number of regions the page seeds (a page with two regions uses
two columns, the lead wider by its weight; regions past the last column stack
under it, the lead keeping 60 % of the height). The rail starts open from 1600
px. The reader can drag the centre's edge (remembered per page).

**The stage** (`components/Stage.tsx`) opens on the page's default. What the
reader or Consult pulls up — a subject's page, a record, the forecasts, any
view — lands on the stage of the page they are on, and a breadcrumb (*◂ Back ·
Today's read › Brazil › Record · …*) always leads back; the first crumb is the
page's own. The stage shows any registered view (`components/ViewHost.tsx`):
**To the panels** sends the view in front to the panel side, and a region's
**show on the stage** button brings a panel to the stage. A record Consult
pulls up (a case's `focus` on a record) lands on the stage, as the Record; a
record the READER selects opens the Record sheet (below).

**What reads, and when** (`lib/queryLayer.tsx`): every
panel tile and slot renders in a tile region that reads only while it is the
visible tab of its group; the stage and the chat are always-visible regions;
the rail reads only while it is open, and the palette only while it is open.

**The panel side** is Dockview, constrained: at most three regions (a drag that
would split off a fourth is refused, `dropWouldExceedRegions`), each region a
group whose tabs are the views that can fill it. Every view is closable (its
tab's ×) and re-openable from the ＋ catalog; regions resize by their
splitters and pop out over the panels. A page SEEDS at most two regions.

**Opening never adds a column** (brief rule 5). Every open goes through the ONE
opener (`App.tsx::openViewAt`) to its HOME (`surfaces.ts::homeOf`): a view
already on screen comes to the front (on the asked tab); a page's own views
stay where the page puts them; a page-sized view (its contract's home is the
stage: a subject, the read, the forecasts, a record) lands on the stage with a
crumb; a panel-sized reader view joins this page's open region and REPLACES the
region's previous opened view (`surfaceDock.ts::openInRegion`); an operator
view opened from the reader side crosses to the operator section that shows it
(else Investigate). A subject's read (a rail or switcher click, a chip, ⌘K, a
case's `open_panel subject.page`, a retired `target.*` id) is the subject's
page, bound, on the current page's stage, on the tab asked for (the request
rides `state/panelParams` under the bound page's key).

**A saved view is a link.** Arrangement is never part of an address; the
address bar holds the page, the scope, the selection and the window (`#ws=`,
`#scope=`, `#sel=`, `#win=`); the bar's **Copy link** copies it, and the
rail's **Save this view** keeps it in a list (a per-viewer convenience).
**Reset** (↺, `Alt+Shift+R`, ⌘K) lays the page's default back down — its
panels, its stage and its chat — and says so. **Undo** (↶, `Ctrl/Cmd-Z`
outside a text box) takes back the last view change: an open, a move, a
pulled-up record (`lib/panelActions.ts::undoView`, twenty deep).

**Stored arrangements are versioned.** The panel side is stored per page and
mode (`legba_surfaces[<page>][<mode>]` = `{version, layout, closed}`); the
version is a hash of the page's stage, its regions and their views and its
chat default (`surfaceLayoutVersion`, prefix `s2-`), derived, never
hand-bumped. A stored arrangement is used only when (`readSurfaceLayout`) it
carries the current version, survives the alias pre-pass, still holds every
seeded view **the reader did not close** (`closed` lists the reader's own
closes), and holds at most three regions; otherwise it is DISCARDED for the
default and the bar says why in one line (*Board was reset to its new
default*, *… so no view stays hidden*). Every `s1-` slot the earlier shell wrote
is therefore stale and lands on the new default with that notice. The frame's
own state (`legba_frame`: per page the chat's placement, size and collapse,
the centre's width, and the rail) carries its own stamp (`f1`); a body with
another stamp is dropped. The stage is never stored: a page opens on its
default. The retired keys (`legba_ws`, `legba_ws_retired`,
`legba_layout_custom`, `legba_user_missions`, `legba_theme`) are never read and
never deleted. `App.shell.test.tsx` boots over exactly that state, including an
earlier shell's `s1-` Board slot.

**The retired ids still resolve.** `#ws=desk`, a recorded case's
`arrange {layout: 'workspace', layout_type: 'preset'}` and a `#mission=crisis`
link land on the nearest page (`resolveSurfaceId`); `lib/workspaces.ts`,
`lib/layoutPresets.ts` and `lib/missions.ts` are translation tables only.

**The Record sheet** (`components/RecordSheet.tsx`). The Record is a drill-in
behind a statement: the Inspector in a sheet that slides over the panel side
from the right, 40 rem wide (never wider than the main area), when the reader
selects a record — and closes with ×, `Escape` or `Ctrl/Cmd-I`. It never moves
a region, and the stage and the chat stay usable beside it.

**Cases** (`lib/caseModel.ts`, `lib/caseRunner.ts`, `state/casePlayer.ts`) — a
consult turn can PRESENT its answer: an ordered list of governed workstation
actions drawn from a closed vocabulary of nine — `open_panel`, `arrange`,
`set_scope`, `focus`, `map`, `timeline`, `run_mission`, `narrate`, `done`. Each
action except `done` cites the records it rests on, and the server's case gate
admits it only when every cite came back from a read in an EARLIER round (or is
one of the person's pins). The vocabulary is one file with two readers
(`src/legba/data/workstation_vocabulary.py` exports
`lib/workstationVocabulary.json`), and the client refuses a kind outside its
allowlist a second time (`kind_not_drivable`). Every action executes through
the shell port into the same opener a click uses. The **player** takes a
**snapshot** before step 1 — scope, focus, window, layers, page, the panel
side, the stage and the chat — and **Undo** puts all of it back. **STOP**
latches at once and only then tells the stop route; **Replay** re-runs a
reloaded turn's record with no consult POST.

### Consult in the frame: under the stage, movable, collapsible

Consult is a primary focus on every page (brief §2, §12;
`components/ConsultDock.tsx`). Its default home is the **lower half of the
centre**, under the stage, with the ask box at its foot. Its header:

- **▾ / ▴** collapse it to the **ask bar** (9 rem: the line naming the point,
  the box, the drive switch and Send) and restore it;
- **⤢** pops it to the **full centre** (the stage folds to a one-line *show
  the stage* bar);
- **◧ / ⬓ / ◨** move it to a column on the **left** of the centre, back
  **under the stage**, or to a column on the **right** of the panels (30 rem
  by default, its edge draggable).

The **divider** between the stage and the chat drags from the ask bar alone
(drag to the bottom collapses) to 92 % of the centre's height; it also steps by
keyboard (arrows, Home, End). Placement, size and collapse are remembered **per
page**; reader pages open with the chat open (half), operator sections with the
ask bar. Every move is undoable. It renders the same `system.consult` kind
against the same store slice (`singleton:system.consult`), so the conversation
survives every move, resize and page switch; the question box is the only
child of its own full-width row in every placement. The rail's
**Conversations** list continues a prior chat in place.

**A line naming the point above the box** (`components/ConsultPointLine.tsx`):
*Asking about: Burkina Faso · desk · window 24h · 1 pinned*, or *everything in
view*, with **clear** to drop the scope.

**Cited records are readable** (`components/CitedRecords.tsx`). An answer's
cited records collapse to *Records cited · 31 ▸ show*; expanded, each is its
title, its outlet (or source) and its date — the id only in the hover title and
the Record sheet a click opens. The words come only from what the client
already holds: the answer's own tool results (the rows the model read carry
`title`, `title_en`, `source_url`, `produced_at`), the cited ref's
`description`, and rows already in the page's query cache. A record none of
these describe is counted as such. There is no batched by-id lookup route
today; one would describe the rest (noted for the data layer).

**Drive the view is a mode** (`state/driveMode.ts`): when it is on, the chat
moves the view, with an undo; when it is off, it does not. **On**: every chat turn is
sent as a case and the view actions it presents run at once through the ONE
executor, listed with STOP and one Undo. **Off** (the default): a turn moves
nothing. A change to the system is never a view action and always waits for an
explicit Confirm.

**What Consult is given** (`lib/onScreen.ts`). A turn asked in the frame
carries `on_screen`: the page and side, the point, the window, the views on
screen with where each sits (`stage`, `p1`, `p2`, … left to right) and
whether it is in front, and the chat's placement — ids and places only, at
most twelve views, under 1.5 KB. The registry's consult request model ignores
fields it does not declare (pydantic's default), so today it changes neither
the answer nor what a turn costs; the core lane that widens Consult's context
(brief §4, item 4) declares it and hands it to the planner.

**The pin follows the reader.** Every object view calls one hook,
`lib/consultContext.ts::usePinScope(object, surface)`, with the object it is
reading — or with nothing, which releases its claim. The ambient pin is the
NEWEST live claim; the wall scope is one more claimant.

| View | Pins |
|---|---|
| the rail and the subject switcher | the subject, unit or source picked |
| a bound view on the stage or in a panel | the record it is bound to, while in front |
| a record on the stage, the Record sheet | the record |
| the subject page (`subject.page`) | its subject, re-asserted on every tab change |
| the map (`v4.map`) | the subject the camera fits |
| the Timeline (`system.timeline`) | the record it holds selected, or the subject the validity view reads |
| the Register (`system.register`) | the situation picked on the Situations tab |
| the Accuracy page (`system.eval_scorecard`) | the whole roster |
| Cross-framing | it sets the wall scope, which claims the pin |

**The case driver on the frame** is audited action by action
(`App.caseActions.test.tsx`), and a recorded real case replays end to end
through `#case=` (`App.caseReplay.test.tsx`).

### The panel contract: what every view is, and what Consult can do to it

`panel-registry/contracts.ts` declares, per view, in one place: a stable id
(the view id), a name and a one-line description, a catalog group, the object
types it binds to (subject, record, source, forecast, window, place, unit,
alert), a size class, its home (the stage or a panel) and the ACTIONS it
accepts, each with typed arguments (text, true/false, an enum of values, a
`{kind, id}` ref of named kinds). Every view accepts `open` (optionally
`place: stage | panel`), `close` and `focus`; a view that binds to a subject or
a unit accepts `bind`.

| View | Binds | Home | Its own actions |
|---|---|---|---|
| `v4.morning_read` Today's read | window | stage | `set_window {span}`, `select {ref}` |
| `subject.page` the subject | subject | stage | `bind {subject}`, `set_tab {tab}`, `select` |
| `view:forecasts` Forecasts | forecast, window | stage | `set_window` |
| `system.eval_scorecard` Accuracy | — | panel | — |
| `system.alerts_watches` Alerts and watches | alert | panel | `set_tab`, `select` |
| `system.source_health` Source health | source | panel | `select` |
| `system.wall` At a glance | window | panel | `set_tab`, `set_window` |
| `system.inspector` Record | record, source | stage | `select` |
| `v4.map` the map (contract only; no map code touched) | place, subject, window | panel | `layer {layer, on}`, `fly_to {ref}`, `set_window` |
| `system.consult` Consult | subject, record, window | (the chat) | — |
| the three slots | — | panel | — |

`lib/panelActions.ts::applyPanelAction(id, action, args)` is the router: a
click, a link and a Consult action reach a view by its id or instance id
(`subject.page:<id>`) only through it — the contract is looked up, the action
must be accepted, the arguments are checked, the view is snapshotted for Undo,
and a handler the mounted view registered (`usePanelActionHandler`) runs, or
the default does: the frame port for open / close / focus / bind, the tab
request for `set_tab`, the one clock for `set_window`, the selection for
`select` and `fly_to`, the map's layer store for `layer`. `drive`'s
`open_panel` lands in the same opener. **Adding a view is one call** —
`registerPanel(contract, Component)` — with no edit to the frame:
`App.panelContract.test.tsx` registers a dummy view, opens and binds it,
drives its own action, finds it in the on-screen report, sends it to the stage
and undoes it all. The operator views without a declared contract (they get
the three common actions and a panel home) are listed in
`OPERATOR_KINDS_WITHOUT_CONTRACT`; `contracts.test.ts` keeps that list honest.

**The ＋ catalog** lists views by group (Read, Subjects, Map, Forecasts and
proof, Watch, Records, Operator), each with its description; the filter
searches names, ids, both descriptions and the group, and each row can put the
view on the stage or in the panels.

## Deployment modes

`auth/jwt.ts::currentMode()` resolves the active mode (`personal` | `above_ai` |
`cis`), highest priority first: the `?mode=` URL query param, the `mode` claim of
a stored bearer token, the `VITE_LEGBA_DEFAULT_MODE` build env, then `personal`.
Each panel kind declares the modes it ships in, and the `＋` catalog, the
command palette, the rail's queues and the one executor all filter on the
active mode: `personal` has the widest panel set, `cis` a narrower lens.
The mode filters which panels show and nothing else: it is neither a role nor
a tenant boundary. Legba ships single-tenant, and the registry's route roles
remain the authority on what a caller can read.

## The panel catalog

One row per registered panel kind. The group is the verb family the kind
belongs to (the vocabulary's `group`; the rail no longer lists panels — every
kind that is not a drill is in the `＋` catalog and ⌘K `>`). **Bound** panels
(per target, per analyst) need a descriptor binding and open from their
subject's or unit's `⋯` menu on the rail, or the command palette. A **drill**
needs a ref to render and opens from the object it is about. Each kind's tier and modes are generated from `registry.ts` into
[RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md) §2 by
`scripts/gen_release_state_matrix.py`, with a drift test failing the suite if the
two disagree.

A row marked **↳** is not a registered kind: it is an id retired into a tab of a
survivor when panels were merged, kept here so its description is
not lost. Its
Kind column reads `retired.id` → `survivor#tab`; opening the id — from a saved
layout, a hash, ⌘K, a recorded case — lands on that tab (see
[Retired ids](#retired-ids-drills-and-the-kind-ratchets)). The registry holds
39 kinds.

| Panel | Kind | Group | What it shows | What it reads |
|---|---|---|---|---|
| The Wall | `system.wall` | Awareness | The mission-control anchor. The At-a-Glance counts strip rides above every tab as a header band; **Overview** is the four quadrants — per-desk band grid, movers since last visit, newest high-severity verified findings, system health — and **Movers** is the movers quadrant alone. "Since last visit" is ONE anchor per page load, shared by the Overview quadrant, the Movers tab and the standalone tile, so the two tabs can never answer differently; the stored cursor (`legba_wall_cursor`) advances only when the movers list has been SHOWN — its tile visible, the document visible — never on a fetch, so neither the page load nor a Wall in a background tab marks anything read (`lib/wallModel.ts` `wallVisitForPage` / `markWallRead`). `v4.kpi` and `system.wall_movers` alias here | `/v3/since`, `/v3/system/source-firing`, `/v3/system/analyst-cadence`, plus the strip's `/findings`, `/signals`, `/situations`, `/registry/sources` |
| Live Feed | `system.findings` | Awareness | The one unified findings + signals feed; Live on/off, a Source filter, and findings-only situation clustering. A row's headline owns its line — the **age** (*2h ago*, with the full timestamp on hover) rides the row's meta line, right-aligned, beside the desk and the producer (the badge shared the headline's line and cut headlines at ≈ 20 characters in the feed's column) | `/findings`, `/signals`, plus the `analyst.*.finding` and `legba.signals.>` NATS tails |
| Timeline | `system.timeline` | Awareness | Events lanes and validity windows in one panel, mode-switched, both following the one shared window with zoom and pan as a local lens; the Events lanes plot valid or record time and print both per mark; ranged facts / situations / findings with supersession connectors. An `open_panel` that asks for a mode switches an already-open Timeline too; the record it holds selected — a mark it places in time, or the subject the validity view reads — pins Consult ([Consult on the shell](#consult-in-the-frame-under-the-stage-movable-collapsible)) | `/v3/timeline`, `/findings`, `/signals`, `/situations`, `/v3/events` |
| World Map (the profile's `map.title`) | `v4.map` | Awareness | The subjects' DECLARED geometry and the layers the profile can draw — under the geopolitics profile the banded-verdict choropleth, signal-density, co-mention arc, convergence-marker and watch-location layers — each layer with its aperture `i`; a dual-thumb time scrubber over two clocks — valid time (when the world was so: a signal's `data.published_at`, a situation's newest member event) and record time (when the platform learned it) — with each layer's rows as-of the record time; and beneath the draw layers the six **source layers** with the per-subject aperture declaration behind an `i` on each. Absent under `map.geometry: none` (not seeded, not in the catalog, refused `no_map_surface`). The subject the camera fits pins Consult; one it cannot place pins nothing. See [The map](#the-map-subject-geometry-and-layer-apertures) | the composition verdicts, `/signals`, `/findings`, `/situations`, `/v3/events`, `/v3/since`, `/v3/layers/divergence`, and the target descriptors' `scope` for subject geometry |
| Alerts & Watches | `system.alerts_watches` | Awareness | Three tabs: server-side standing Watches, client-side alert Triggers, and Deliveries (did an escalation land). An `open_panel` that asks for a tab switches an already-open tile too | `/v3/watchlist`, `/findings`, `/v3/system/escalations` |
| Inspector | `system.inspector` | Awareness | The selection-linked detail rail — see [The Inspector](#the-inspector) | the selected row's detail route, `/lineage/{kind}/{id}`, `/v3/watchlist` |
| Consult | `system.consult` | Awareness | The on-demand analyst workbench; a depth toggle switches between the inline ReAct answer and a detached deep-analysis workflow. Beside each settled answer's header, the server-composed **provenance census** prints what the answer rests on — *"cited: 6 live · 2 history · 0 web · model knowledge: 3 sentences"* — refs counted by their own `origin_class` column (never by which tool returned them) and sentences carrying no citation at all, the two halves labelled with their own units so they can never be read as one total. The panel prints what the route says and derives nothing; a class the server could not measure renders as *not measured*, never as 0, because "cites no live reporting" is a claim and absence is not. Beside the four classes, and only when the server measured them, the line carries what the citations did NOT check out as — **unresolved** (the id matched a real record whose origin class the census does not read: a finding, a situation, a hypothesis, an event, a nexus), **fabricated** (a well-formed uuid matching no record at all) and **malformed** (a citation token that was never an id, e.g. `sit-001`) — with the by-kind breakdown and the malformed tokens themselves on the line's tooltip, so the 29 real refs the 2026-09-28 run filed as unresolved are named rather than invisible. When the server **held** the answer — not one cited reference resolved to live or held reporting and the prose still asserts — an amber frame sits above the answer body and the answer itself leads with the server's own banner naming the counts; a held answer is never discarded, only framed. When the server's **evidence floor** raised the model's own uncertainty (too few cited refs resolved to a real record), an amber stamp beside `uncertainty=` reads *"floored from 0.30 · 0 cited refs resolved"*; no stamp means no floor moved the number. Beside them, when the server minted the answer's watch candidates (`analysts.consult_watch_mint`), a line reads **N watches standing** (*· M not watched* when any were refused): each candidate that carried a future `by YYYY-MM-DD` horizon became an open question (`hypotheses`, `status='open_question'`) that `claim_watch` matches new reporting against, a repeat folding onto the watch already standing; the tooltip names every candidate with its horizon, and every refusal by name (`unmintable_no_test`, `hypothesis_refused_no_resolves_by`, `ungrounded_office`). No line means no mint was attempted — never "0 standing". Beside `uncertainty=` every answer's header carries the turn's **judged label** and its **usage** (X2 C1, `components/ConsultTurnStamp.tsx`): *judged: 0.83 (weak judge)* when the answer's cited claims went through the fleet's faithfulness judge (the score is the share of cited claims the judge found supported, 0–1; the tooltip names the claim counts, the judge model and route, and when it judged; *weak judge* = the in-house 8B rung), *unassessable · no cited claims* when there was nothing a judge could grade (never a number — never 0.5), or *not judged · judge off* / *· judge timed out* / *· answer incomplete* / *· recorded before judging* — the label is never blank. The usage reads *gpt-oss-120b · 41,230 in / 1,880 out tok · 37 s · cost: not priced* — the model the calls ran on, the tokens in and out, the run's own wall clock, and the cost only when the route prices its calls (*$0.42* on a hosted plane; *not priced* on the self-hosted plane, never *$0.00*); a field the run did not report reads *not measured*, a turn with no receipt *usage: not recorded*. Both come from the server (`consult_turns.usage`, `consult_turns.verification`, migration 0234) on the live answer and on a reload; `GET /consult/usage` sums them per day. All of it survives a reload: the turn's census, hold, floor stamp, uncertainty, model, watch candidates and watch outcome are kept on `consult_turns.response` (migration 0223) and lifted by `GET /consult/sessions/{id}`, so a session reopened from History — or re-synthesised from evidence — renders exactly as it did live; a turn recorded before 0223 reads *"cited: not measured"*, never zeros. Through a quiet stretch of a live run the last round reads **synthesising · Ns** (or **working · Ns** while the drilling has not visibly ended) — shown only while the stream's named `keepalive` event vouches for the silence, N being the seconds since the last frame; an unvouched or stale silence claims nothing (`lib/consultLiveness.ts`). The question box owns its own full-width row at every dock width; **Present as a case**, **Allow changes this turn** and Send sit in the row under it, which wraps rather than squeezing the box. **Present as a case** — a toggle beside Send, OFF by default and reset on every send — asks this one turn to present its answer as a [case](#the-frame-two-sides-eight-pages-one-frame): the request carries `present_case: true` (a plain turn's body carries no such key), and while the case plays a **case rail** sits in the pinned composer region with the step list, the current step highlighted, each step's census line (*rests on 2 reads + 1 pin*, or *no read* — never a blank), the narration with its cites as record chips, refusals listed in the trace and never executed, and a **STOP** that is always on screen while anything can still move the wall. Below 380 px the rail collapses to the step counter and STOP. A settled turn that presented one carries a line — *case · sealed · 7 steps · recorded …* — with **Replay**, which re-runs the persisted record without the model; once the case ends, **Restore** returns the wall to how it was before step 1; **Save as mission** on the rail keeps the case's posture as an operator mission. The ambient pin follows the reader — every object surface sets it — and a question asked in this page carries, beside its answer's census, the line *pinned: &lt;kind&gt; · &lt;name&gt; (&lt;surface&gt;)* or *pin: none* ([Consult on the shell](#consult-in-the-frame-under-the-stage-movable-collapsible)). A 409 `case_unavailable` (the drive pack not bound) lands as the turn's error, never as a silent plain answer A candidate that re-phrases a watch already standing on the same desk — horizon within ±3 days, the same claim (identical claim cores, or claim-core cosine ≥ 0.86 on the platform's own embedder; W4-F1, `analysts.consult_watch_fold`) — folds onto it and the line adds *· K folded*; its tooltip entry names the watch it folded onto and the cosine, and a candidate minted while the embedder was unavailable reads *(fold check unavailable)*. **Allow changes this turn** — a second toggle beside Send, OFF by default and reset on every send, on the workstation and the mobile sheet only — lets this one turn carry out a change the operator asks for (W5: the `source` class — cadence, scope tags, access class, existing fetch options, name; pause / activate / retire; create a draft; revert. W6: the `subject` class — name, scope geo, scope tags, scope entity classes; pause / activate / retire; create a draft; revert. A subject has no cadence of its own to change: the units that read it run on theirs. Also W6: **units** — a `method.options` knob the option catalog declares for the unit's kind, the slice window, the rubric, an inline prompt unless it is pinned — and **compositions** — their inputs, the input window, declared knobs; pause / retire; revert; and **rerun**, one forced run now, which cannot be undone and is offered only where the unit's LLM components are declared unbilled. Pausing a unit or composition, any prompt edit and any change to a composition's inputs are staged like a retire. Also W11: **facts** — *assert* a fact as the operator's own word (it grounds like curated; `supersede` none / differing — never a seed or curated row — or the ids an inspect of the pair returned) and *close* one; closing more than one open row is staged, and its amber card names the triple and lists each row it would close (`components/ConsultWritesFact.tsx`), while a fact receipt reads *Written · fact iran | leader of asserted "Jane Roe" (operator) · closed 1 · 2 open → 2 open · core · $0.00 · 14:21Z · read on the next query* — the triple and what closed, never a digest. A rerun's receipt reads *run pending* until its forced run is traced, then *run landed 14:22Z (success)* — or *run not landed* / *run failed: …*, never a fabricated landing). W11: **entities** — a record, not a head — edit the name, the class (a value of the `entity_class` family, else refused naming the family), aliases (add / remove) and geometry; merge one entity into another; unmerge it again until the lifecycle compacts the loser; revert. Every entity change is staged, and its amber card shows the record as the executor read it when it staged the change — *Merge entity Acme Corp (organization) into Acme (organization) · folds 2 aliases · 12 open facts name the loser (as of 14:00Z) · unmergeable until compaction*, *Edit entity Acme (organization) v4 · name → “Acme Holdings” · aliases +“Acme Group” · now: 1 alias* — a count the ledger row does not carry reading *not measured*, never 0; its receipt reads *Written · entity Acme Corp (organization) merged into Acme (organization) v1→v2 · core · $0.00 · 14:21Z · record write (no activation)* (a record has no head to activate, so no activation stamp is shown); an unmerge of a compacted loser, or of a merge that recorded no pre-image, is refused *kind_not_built (SEAMS #68)* (`components/consultWritesEntity.ts`). The request carries `allow_writes: true` (a plain turn's body carries no such key); the armed turn's header carries a **changes allowed** stamp, and every armed answer ENDS with the server's truth line *writes: executed n · staged n · refused n* (or *writes: none*) whatever the prose says. Under the answer, each change renders FROM THE LEDGER (`GET /consult/writes`), never from the prose: a staged change (activate a source, pause an active source or subject, retire) is an **amber card** — *Pause source X (head 3f2a…, active) · [Confirm] [Cancel] · or type confirm K7QD · expires 14:32Z*, retire adding *cannot be undone* — and nothing has changed until the operator confirms by the click or by sending the code as a whole message (matched at the front door before any model runs; the model has no tool that confirms, so *yes, go ahead* executes nothing); an executed change is a **receipt line** — *Written · source X updated 3f2a…→9c41… · core · $0.00 · 14:21Z · activation pending* — with **Revert** (absent where the change cannot be undone), the activation stamp reading *pending* until the server probed it, never assumed; a reverted receipt is **struck through** with *Reverted 14:40Z by w-…*; a refusal or expiry reads *Not written · stale_read: …*. A 409 `writes_unavailable` (the `consult_write` pack not bound) or a 503 (the operator turn could not be recorded) lands as the turn's error. Receipts outlive the session: the ledger has no foreign keys, and the Audit-Chain Browser shows each change as an `operator_via_consult` row signed by actor `consult_write:<id>`. | `/consult` (`present_case`, `allow_writes`; a whole-message `confirm XXXX` is the typed confirm; `usage` + `verification` on the body), `/consult/usage`, `/deep_consult`, `/consult/sessions/{id}` (`case`, `writes` on the bag), `/consult/stream/{request_id}` (`case_action` / `case_refused` / `case_sealed` / `case_stopped`, `write_executed` / `write_staged` / `write_refused` frames), `/consult/runs/{id}/stop` (`executed_through_seq`; staged writes expire), `/consult/writes` (GET the ledger; `/{id}/confirm`, `/{id}/cancel`, `/{id}/revert`) staged writes expire), `/consult/writes` (GET the ledger, scoped by `session_id` or `request_id` — the whole ledger is `?all=1` for an explicit principal only and never carries a confirm code; |
| ↳ At a Glance | `v4.kpi` → `system.wall` | alias | Now the header band of **The Wall**, shown on every tab. Signal / finding / situation / source counts with band-change deltas | `/findings`, `/signals`, `/situations`, `/registry/sources` |
| Entities | `system.entities` | Investigation | The entity roster with search and class facets; tabs fold in the relationship graph and the ranked notable-structure shortlist | `/entities`, `/entities/graph`, `/graph/structure` |
| Global Search | `system.search` | Investigation | Cross-family search composed client-side — there is no backend search route; a family that 404s degrades rather than erroring | `/signals`, `/findings`, `/situations`, `/registry/sources` |
| Graph Walk | `system.graph_walk` | Investigation | An interactive walk over the reified edge store: anchor on an actor, take one indexed hop at a time, open an edge for its evidence. The three edge families draw as three kinds of line, and the disclosure strip always states how many edges the view is withholding | `/graph/ego`, `/graph/edge/{id}`, `/entities` |
| Provenance | `system.provenance` | Investigation | Evidence, three tabs over one subject: **Why** (the lineage DAG and provenance chips), **Lineage** (the hop-by-hop receipt walk) and **Fan-out** (source → signal → finding, the former Fan-out Explorer). Since the panel merges the registry canvas is the Registry's Canvas tab and Trajectory / Narratives are the Register's tabs; a saved tab naming one of those opens on Why | `/lineage/{row_kind}/{row_id}`, `/findings`, `/signals`, `/signals?source_id=` |
| Register | `system.register` | Investigation | The frame register, two tabs: **Situations** (the situation register's frames and each frame's append-only trajectory ledger, every intensity read on its stated scale) and **Narratives** (reified contested-claim families and the source-echo graph). `system.situations` and `system.narratives` alias here (re-pointed off Provenance). On the Situations tab the situation picked pins Consult; the Narratives tab selects no object and pins nothing | `/situations`, `/v3/situations/{id}/trajectory`, `/v3/narratives` |
| Accuracy (the Eval Scorecard, retitled per Table A) | `system.eval_scorecard` | Analysis (the Trust stance) | The roster-wide Accuracy page over units × the profile's subject roster, in five sections — see [The Accuracy page](#the-accuracy-page): the generated cohort paragraph; the sealed ledgers as stage tables (entries → resolved → scored → voided → awaiting a judge, each stage a count with its denominator and as-of, a stage no route exposes reading *not measured* with the route it lacks); calibration (the skill scoreboard, band calibration with each horizon's own denominator — never a Brier score — and the banded verdicts per subject with their per-band drill and **Download CSV**); the graders (one table — per grader the graded count, the population, what was kept out and the as-of — then the machine grader's roster, the operator gold set, standing external truth and the per-analyst critic rollup, where a memory-only verdict counts as *N carried, ungraded* and never enters a mean); and a closing *what this page does not publish*, generated. A null share is *unmeasured*; a measured 0.0 stays 0.0. While on the wall it pins Consult to **the whole roster**, never one subject | `/v3/eval/calibration`, `/v3/eval/country_scorecard`, `/v3/eval/scorecard`, `/v3/eval/correctness`, `/v3/eval/grader_roster`, `/v3/since` (`forecasts_due` only) |
| Layer Divergence | `system.layer_divergence` | Analysis | The source-layer divergence map: one card per desk with the six source layers as chips carrying their aperture declaration (present / absent / unmeasured / undeclared, each excluded one showing the operator's own reason), and the three layer-to-layer pairs as rows — state (fired · thin · below threshold · not evaluable · evaluated) with the `no_fire_reason` by name, a paired sparkline of the two layers' post-fold items/day under a z track with the firing threshold drawn, and a link into the finding when a pair fired. The header carries the unit's own sentence plus the method version, payload schema, window, baseline, threshold and thin floor; the SEAMS #60 un-audited-classification stamp rides every read. A quiet run is suppressed to trace-only, so this is the only surface on which the instrument is visible on the days it fired nothing | `/v3/layers/divergence` |
| Cross-framing | `analysis.cross_framing` | drill (a claim) | ONE CLAIM against the units that touch it. The header carries the claim verbatim, its per-claim verify verdict and the count sentence ("carried by N of M units · K with no read") beside the record's scale stamp and as-of; then one row per unit on the RECORD's own coverage roster — how that unit's read frames the claim (its own sentence, with its own markers), what it rests on (cited signals, outlets, the record's fold chips, the block's faithfulness), and for a unit with no read the TYPED ABSENCE in the route's own words with its proof and its clock (a stale one reads "last known absence, not re-checked"; a unit with none reads "no typed absence is recorded" beside the record's own coverage status) — never a blank row. Below it: the six stance-typed leans' framings of the same evidence, each with its declared prior named ("prior: …" — the read's own `lens_prior` stamp when it carries one, else the persona roster the bundle mirrors, the hover says which) and its own gate score (a lens that ran and cited nothing is SILENT, not dissenting); the desk's layer-divergence receipt pair by pair with the SEAMS #60 stamp, where no response · a failed read · no run ever · a desk the run did not resolve are four distinct answers; and WHAT NO UNIT SAYS — the units named by none ("absent, not contradicted"), the desk's off-roster typed absences, the kinds the route could not read, the record's drop ledger by why-class, and the tension pass's checked negative. **Ask about this claim** sets the AMBIENT consult scope pin (`lib/consultContext.SCOPE_PIN_ORIGIN`) to the claim, so the existing Consult session answers about it with its provenance census line intact. Adds NO route and computes no statistic; all derivation is pure in `lib/framingModel.ts`. Opened from the Claims panel's `cross-framing ▸` action, a situation row, an inline citation chip's card (`cross-framing ▸` on the Inspector read and the desk brief's unit reads — the surfaces that name their desk; the claim is the sentence the chip closes), or ⌘K | `/findings`, `/v3/contentions`, `/v3/absence`, `/v3/layers/divergence`, `/journal?kind=lens` |
| Contentions | `analysis.contentions` | Analysis (the Trust stance) | The contrary-evidence pass's records as a LIST — fleet-wide and newest first by default ("what did the pass find last night"), or narrowed by the route's own filters: a target or desk key (the response's `scope_field` line says which handle matched, or that none did), one stance, live only. The route's contract note renders above the list verbatim (a stance is a retrieval outcome, never a verdict). Each row: the stance in words, the uncalibrated-negation marker where it applies, live vs "expired, not re-checked", the as-of, the target / desk, the claim verbatim and its hedged statement, then the four fences for the decisive page — host class, the page date the gate parsed, subject overlap, independent pages against the bar of 2 — each a reading or "not measured", never 0 — and the pages fetched with the decisive host. **open the read** selects the finding in the Inspector; **cross-framing ▸** (rows with a target) opens Cross-framing on the row's exact claim id. An empty list says it is not a claim that nothing is contested. Adds no route; helpers in `lib/contentionsModel.ts` | `/v3/contentions` |
| Optimizer Candidates | `system.optimizer` | Analysis | Two tabs. **Candidates**: the GEPA candidate queue with its real paired before/after faithfulness delta, the method each row actually took, and the human promote action. **Diff** (preview badge): the candidate-versus-parent prompt-module text diff, opened from a candidate row's "view prompt-module diff" — the row's `open_panel(system.optimizer.diff, {candidate_id})` resolves here, the candidate rides `state/panelParams` and the tile switches to Diff whether or not it was already open | `/v3/optimizer/candidates`, `POST …/{id}/review`, `/v3/optimizer/candidates/{id}/diff` |
| ↳ Prompt-Module Diff | `system.optimizer.diff` → `system.optimizer#diff` | alias | Now the **Diff** tab of **Optimizer Candidates**. Candidate-versus-parent prompt-module text diff before promotion; opened from Optimizer Candidates | `/v3/optimizer/candidates/{id}/diff` |
| ↳ Navigator | `system.navigator` → `v4.morning_read` | alias | Retired: the products index is the object rail's **Reads** section; the id opens the **Morning Read**. The products index — the day's reads by tier, scoping the rest of the surface to whichever one the operator picks | `/findings` |
| Morning Read | `v4.morning_read` | Products | Opens on four judgment bands per desk, worst-moved first — **Changed** (what landed since the reader's last visit, or yesterday 06:00Z, each row cited and clickable; a finding row without an effective confidence prints `—`, never throws), **Checked** (the day's claim verdicts: verified / flagged counts and the unverified ones split `judge pending` / `not judged` by the registry's own state, each unverified finding listed under its desk with its mark — a desk where nothing carries a verdict says so instead of "nothing flagged" — with every flagged sentence quoted beside the finding it came from — reads `/findings` at its `fields=judgment` weight, the verify verdict whole plus reduced citations, never the finding's `data`/`derived_from`/`body`, which cut a measured 200-row page from ~5.5 MB toward ~1 MB), **Gaps** (TYPED ABSENCE, read `/v3/absence?scope=` once per desk shown and cached for the page load: the nine kinds with the route's own meaning on hover, each item carrying its `as_of`, its `expires_at` and — where that has passed — "last known absence, not re-checked", with the proof's `ref_kind` offered as a control only where it resolves; a kind the route could not read for a desk says "not measured for this desk" in the route's own words, a kind it read and found nothing under is named as read, and a desk with nothing absent is named rather than dropped. The card-derived rows — dimensions banded `insufficient-evidence`, a desk whose composition is absent — follow beneath each desk as the rows no kind already names, with the de-duplicated count stated) and **Due** (pre-registered forecasts past their window with the resolution test frozen on the row, each call carrying the probability scale its `p` is read on and the moment it was issued) — above the unchanged assembly reader for the composed read. A reading-order rail carries the four counts — each `—` until its band's routes have answered (the GAPS count until every per-desk absence read has), `unavailable` when a route failed, never a `0` that was not read — (the bands' labels and questions are the projection's `bands` section when the plane answers, the platform's reading order otherwise — the rail's tooltip and `data-bands-source` name which; a band note counts the profile's subject noun) and a read timer against the twenty-minute budget; the timer displays the read-telemetry session clock and emits nothing, so it cannot inflate the `brief_read` count the wager is graded on. **A subject scope** (a rail click on a subject, ⌘K, a map click) narrows all four bands and their rail counts to that subject, says so above them ("Scoped to Brazil — every band below reads this desk only", with a *show every desk* control that clears the scope), and puts that subject's own read — the profile's per-subject frame's newest row on it — on the page below; a subject with no read on record says *No composition on record for this desk.* On a world record the ledger opens with the floor line — '{N} regions carried, floor honoured' — read off the persisted `assembly.floor`; a block the floor kept is chipped 'kept by the region floor' with its order rank and age, and a row it displaced says so. | `/v3/since` (incl. its `forecasts_due` section), `/findings?fields=judgment`, `/v3/eval/country_scorecard`, `/v3/absence?scope=` (one per desk shown) |
| World Assessment (the profile's `labels.read_title`) | `v4.assessment` | Products | The reading surface for a composition: the world one-pager with nothing selected, the desk intelligence card with a desk selected (band and delta → BLUF → the verified country composition → the per-desk unit cards → related → history) | `/findings` |
| Journal | `system.journal` | Products | The reflective voice: kind-filtered entries grouped by cycle, the open consolidation above them, a reader pane with per-claim provenance chips, and an honesty banner keyed off the live calibration verdict Each card carries a kind badge (entry, consolidation, chronicle, lens, lens_diff, plus the stateful tier: inquiry and crossroads, which band with the synthesis reading because they read across cycles) and each cycle splits into a synthesis band above the diary band. A lens card names its declared prior ("prior: …") from the row's own `lens_prior` stamp; a lens row written before the stamp shows no prior line. | `/journal`, `/journal/{id}` |
| Report Export | `system.report_export` | Products | The collection basket — items gathered from the Inspector, feed rows and journal cards, composed server-side into markdown or JSON with citations resolved live. "Desk brief" (also on the Target Overview panel) is a one-click shortcut that fills the basket with the scoped desk's current composition, each unit's latest admitted read in the composition's declared order, and its open situations/tracked events, then exports it in one file — and asks the route for the desk's TYPED ABSENCE beside them (`appendix.absences` + `appendix.scope`; composed server-side so the markdown and the JSON carry the same block, all nine kinds with the stamp and proof rules, printed after the cited events). **Print / save as PDF** (also on the Target Overview desk brief) re-lays the composed markdown into its own print document — A4/Letter margins, a running header carrying the title and the export's own as-of, each section's citations lifted to numbered endnotes — every signal endnote carrying the cited source's date with the word that says which date it is (`published` when the source declared one, `fetched` when the platform's read stamp is all there is; a citation the route could not date states the absence in words) — a page break before the appendix and another before the typed-absence section, which is split off the appendix onto its own page — and prints it from a hidden iframe, so no workstation chrome reaches the page and no route is added; it reformats nothing, and is disabled until a markdown document has actually been composed | `POST /v3/export`, `/findings`, `/situations`, `/v3/events` |
| ↳ Action-Pack Grants | `registry.action_packs` → `system.registry#packs` | alias | Now the **Packs** tab of **Registry**. Action-pack descriptors — tools, channels, tags, governor caps — plus the effective-capability view of the analyst ∩ target ∩ pack intersection the governor gates | `/registry/action_packs`, `/registry/descriptors` |
| ↳ Actor Health | `system.actor_health` → `system.status#actors` | alias | Now the **Actors** tab of **System Status**. The actor roster by kind and lifecycle with last run, last outcome, cooldown and error counts, and an expandable last-error inspector | `/v3/runtime/actors` |
| ↳ Analyst Registry | `registry.analysts` → `system.registry#units` | alias | Now the **Units** tab of **Registry**. Analyst descriptor heads with method kind and tool whitelist on the row, the guided builder, and the inline editor | `/registry/descriptors?family=analyst` |
| ↳ Audit-Chain Browser | `system.audit` → `system.ledgers#audit` | alias | Now the **Audit** tab of **Ledgers**. The descriptor audit log with each entry's signature re-verified inline, a chain-health banner and tamper highlighting; actors are hashed principal labels (`bearer:<12 hex>`), never credentials | `/registry/audit` |
| ↳ Budget Ledger | `system.budget` → `system.ledgers#budget` | alias | Now the **Budget** tab of **Ledgers**. Per-analyst tokens, runs and cost; the global per-bucket envelope; budget-exhaustion demote events | `/budget/ledger`, `/budget/envelope`, `/budget/demotions` |
| ↳ Consumer-Lag Monitor | `system.stream_lag` → `system.status#queues` | alias | Now the **Queues** tab of **System Status**. Per-consumer JetStream lag, redeliveries and unacked backlog; the rollup lives in System Status | `/v3/streams/consumer_lag` |
| ↳ Dead-letter Inspector | `system.dead_letter` → `system.status#failures` | alias | Now the **Failures** tab of **System Status**. The dead-letter projection across its namespaces, with an inline-patch resubmit and a resolved/unresolved toggle | `/registry/dead_letter`, `POST …/{id}/resubmit` |
| Eval Boards | `system.eval_boards` | Engine Room | Three engine-level boards read on a different errand from the analyst scorecard: desk baselines, band trajectory, and per-analyst run timing. An expanded baseline row carries the scale stamp its counts and sigma-distance are read on, with that row's own computed-at — a sigma-distance is comparable across desks only within one scale era | `/v3/eval/desk_baselines`, `/v3/eval/band_trajectory`, `/v3/eval/analyst_runtime` |
| ↳ Fan-out Explorer | `source.fanout` → `system.provenance#fanout` | alias | Now the **Fan-out** tab of **Provenance**. Walks source → signal → finding: a source's signals, then the findings whose `derived_from` holds one | `/signals?source_id=`, `/findings` |
| ↳ Governor Events | `system.governor` → `system.ledgers#governor` | alias | Now the **Governor** tab of **Ledgers**. The per-pack governor's allow/block decision stream, blocks emphasised | `/registry/governor_events` |
| Journal Gate | `system.journal_gate` | Engine Room | The human gate on `journal_proposals`: accept (an idempotent per-kind apply) or reject with a reason. A `merge_entities` correction is applied through the write plane's entity merge (the card says so, names `from` / `into`, and flags a half-filled one as incomplete); a `correct_situation` reads *NOT APPLIED* — a situation is a tower product — and accepting it archives the row | `/journal_proposals`, `POST …/{id}/{accept,reject}` |
| Judge Stats | `system.judge_stats` | Engine Room | The judge's verdict mix by serving provider | `/v3/system/judge-stats` |
| Model Stack Settings | `system.settings` | Engine Room | Model-component configuration and the first-run wizard. The runtime authority for LLM / embedding / NLP endpoints is the stack-component registry, not the environment file, which only seeds those rows at bring-up | `/registry/config/status`, `/registry/stack`, `POST /registry/vault/secrets` |
| ↳ Movers Since Last Visit | `system.wall_movers` → `system.wall#movers` | alias | Now the **Movers** tab of **The Wall**. The Wall's movers quadrant mounted standalone, kept for saved layouts that hold it | `/v3/since` |
| ↳ Production Gauge | `system.production_gauge` → `system.status#production` | alias | Now the **Production** tab of **System Status**. The whole-engine production read with its integrity and metering checks; its paging flag is the same predicate the alert plane's production-deficit trigger fires on, so the panel and the operator's phone cannot disagree | `/v3/system/source-firing`, `/v3/system/analyst-cadence` |
| Read Scoreboard | `system.read_scoreboard` | Engine Room | The one panel whose subject is the reader: reads today, morning reads, drills, a per-kind table and a fourteen-day strip. An empty log renders as a stated finding, and the panel-open bias is disclosed in surface | `/read-events/rollup` |
| ↳ Source Detail | `source.detail` → `system.registry#sources` | alias | Now the **Sources** tab of **Registry**. Per-source identity, scope and output summary; a cursor and staleness readout derived from the most recent published signal against the descriptor's cadence; the projected output-stream shape; the recent signals | `/registry/sources/{id}`, `/signals` |
| Source Health | `system.source_health` | Engine Room | Source quality and staleness debt in one rollup, with a per-source drill and a sortable cadence-derived freshness-grade column (`empty`/`ungraded` render muted as absences, never as failing grades), and under it the staleness-tripwire pill: `fresh`/`quiet`/`stale`/`dead` with the factor over the feed's expected interval and the last-new-item instant (`stale · 9.2× expected interval`, `last new item 2026-10-01 06:12Z`); `unknown` (never delivered / no cadence) and a non-active source (`— staleness not measured`) read muted, never a 0×; when the source's newest poll was `gated` (the feed delivered and the keyword gate dropped every entry, so nothing on-topic arrived) the pill's detail line adds `last poll gated` -- a clean poll, not a new item, and not the same fact as `empty` (nothing arrived); a muted line in the same COMPUTED cell says where the newest enriched poll's wall time went, per enrichment stage (`last poll: geocode 0.3 s/3 · ner 1.2 s/3 · 3h ago` = total seconds over calls, with the as-of; a stage with errors adds `(n err)`), and a source whose enrichment has never been timed reads `enrichment not measured`, never `0 s` | `/v3/source-quality`, `/v3/system/staleness-debt`, `/v3/sources/{id}/quality` |
| ↳ Source Registry | `registry.sources` → `system.registry#sources` | alias | Now the **Sources** tab of **Registry**. Every shared source descriptor with kind, acquisition, scope, subscription policy and output subject lifted onto the row; create, edit and drive the lifecycle transitions | `/registry/sources`, `POST /registry/descriptors/source/{id}/transition` |
| ↳ Stack Registry | `registry.stack` → `system.registry#stack` | alias | Now the **Stack** tab of **Registry**. Substrate stack components. Credentials are never returned — vault refs resolve at call time | `/registry/stack` |
| ↳ Subscription Builder | `source.subscription_builder` → `system.registry#builder` | alias | Now the **Builder** tab of **Registry**. Composes a target's source reference — an explicit source id or a selector predicate — plus a signal-level filter, previews what it would match, and emits copy-ready JSON | `/registry/sources`, `/signals` |
| ↳ Subscription Policy | `source.subscription_policy` → `system.registry#policy` | alias | Now the **Policy** tab of **Registry**. A source's subscription gate (open / allowlist / grant) and the per-source-and-target grant wirings | `/registry/sources/{id}`, `/registry/descriptors` |
| System Status | `system.status` | Engine Room | Five tabs. **Status**: per-layer health in one page — Acquisition (the per-source firing matrix), Analysis (per-analyst cadence, read from the run record rather than actor state), Queues (orphan-filtered consumer lag), Infra (substrate reachability, headed by the backup line: the last encrypted backup set and the last passing restore drill, "never recorded" when absent, amber past 36 h / 8 d). **Production** (the production gauge), **Failures** (the dead-letter inspector), **Actors** (actor health) and **Queues** (the consumer-lag monitor) mount the four retired kinds unmodified; their ids alias onto their tabs | `/v3/system/source-firing`, `/v3/system/analyst-cadence`, `/v3/streams/consumer_lag`, `/v3/runtime/actors`, `/v3/system/backup-status`, `/registry/dead_letter` |
| Registry | `system.registry` | Engine Room | The descriptor families as one kind with a family switch: **Subjects** and **Units** (labelled with the profile's nouns), **Sources** (the source registry with the per-source detail beneath it, which follows the selected source), **Stack**, **Packs**, **Builder** (the subscription builder), **Policy** (the subscription policy) and **Canvas** (the live registry canvas over sources → targets → analysts → packs; opening it counts a `lineage_walk` read, as Provenance's Flow tab did). Every retired `registry.*` / `source.*` authoring id and `v4.flow` alias onto their tab. Personal only | `/registry/descriptors`, `/registry/sources`, `/registry/stack`, `/registry/action_packs`, `/signals` |
| Flow | `system.flow` | Engine Room | A **read-only** canvas over the descriptor heads (React Flow + ELK, layout computed and never saved): sources → targets → analysts → packs, stores and sinks, every edge a relation a descriptor DECLARES, named by its basis on hover (`subscription.other_analysts[i]`, `subscription.targets.predicate`, `target.sources[i].source_selector`, `grounding.sources[i]`, `action_packs[i]`, `outputs[i]` …). Paused heads are dimmed. It opens as a bounded **overview**: one composite node per group (sources by kind, each analyst family, targets, packs, stores, sinks) with aggregated, counted edges (the 3,872 selector edges are one bundle per source kind); a group expands on click, and selector edges stay bundled outside a node's neighbourhood. No single view lays out or draws more than 150 nodes / 600 edges — above that the counts are shown and the reader narrows instead. Layout runs in a Web Worker (cancellable, memoised, a 5 s timeout falls back to columns and says so); the tile fetches nothing until it is shown. A scope narrows to one family, one target's chain or the selection's neighbourhood; search and a minimap find a node. Selecting one opens the inspector: every field, the prompt as declared and as last rendered (from the latest stored run, labelled), the output contract, sampling **configured vs sent**, recent runs (a run's steps, calls, sizes and gate verdicts overlay the node — *observed, not declared*), versions with a semantic and prompt diff, and an **Undeclared** list of what the registry cannot say (the stores an analyst writes, consumers that do not subscribe, what a handler reads). The report lists those relation kinds with live counts. No edit affordance. Seeded in the Engine stance beside the Registry as a background tab (a saved layout gains it inactive). Personal only | `/v3/flow/graph`, `/v3/flow/node/{id}`, `/v3/flow/version/{id}`, `/v3/flow/run/{run_id}`, `/v3/flow/run/{run_id}/body` |
| Ledgers | `system.ledgers` | Engine Room | The three append-only engine ledgers, one tab each: **Governor** (the allow/block decision stream), **Audit** (the signed descriptor audit chain) and **Budget** (per-analyst tokens / runs / cost and the envelope). Actors print as `bearer:<12 hex>` / `consult_write:<id>`, never a token. `system.governor`, `system.audit` and `system.budget` alias onto their tabs | `/registry/governor_events`, `/registry/audit`, `/budget/ledger`, `/budget/envelope`, `/budget/demotions` |
| ↳ Target Registry | `registry.targets` → `system.registry#subjects` | alias | Now the **Subjects** tab of **Registry**. Target descriptor heads with filter, expand-to-body, the guided builder, the starter clone-and-edit and the inline editor | `/registry/descriptors?family=target` |
| Weekly Grading | `system.goldset` | Engine Room | The week's server-pinned stratified sample of verified findings as a card list — read the cited prose, pick a verdict, optionally say why. Verdicts feed the eval scoreboard's operator segment and are never pooled with the deterministic leg | `/v3/eval/goldset/*` |
| Subject page (the profile's subject noun: "Desk") | `subject.page` | per target (bound; the Subject stance seeds it unbound, following the selected subject) | ONE page per subject of ANY frame kind — a country desk, a lane or flow, a region, a situation, a regime — tabbed **Brief · Evidence · Claims · Signals · Situations · Sources · Graph · Runtime**. Each tab is a file under `panels/subject/` mounting the retired per-subject read unmodified; the eight retired ids alias onto their tabs (the ↳ rows below). The **Brief** is the desk brief generalized (piece A's page shape): the composition record with its voice beside it — the newest row any of the profile's composing frames published on the subject (`/findings?analyst_id_in=<frame producers>`) — then one block per bounded unit in the composition's declared order with its read, its two never-pooled badges and its fold chips, the per-unit **evidence table over the subject's OWN roster** (`lib/subjectPage.ts::subjectRoster`: a scored subject carries the scored subjects' units; a subject on another leg only the units scoped to a tag it declares — `lane_hormuz` carries `disruption_status` alone; a profile that sets no leg apart fans every unscoped unit over every subject; a subject whose descriptor has not been read has no roster and says so), each unit's standing question from the composition or else the profile's question set, the endnotes, **Reading limits** naming the scale era, and markdown / print / JSON / PNG. A subject with no composition prints *No composition on record for this desk.* (the profile's noun) and reads its roster's own latest reads instead. The index card's headline and its *composition as of* are read off ONE row — the composition the page found — so the card can never headline "no composition" above a composition's date while the export document is still composing or was refused (`lib/deskBriefPage.compositionIndex`); the as-of prints `YYYY-MM-DD HH:MMZ` (the full instant stays in Reading limits). The voice's Assessment renders its markdown through the one prose renderer (`CitedProse`: `**BLUF:**` as bold, `## …` as a heading, `[[ref:N]]` as citation chips off the voice row's own citations), each unsupported span keeping its letter inline as a glyph with the notes list beneath. Every subject word on the page is the profile's noun. **Absence contract** (Table A): a unit with no read shows its typed absence or *no read yet*; no composition → "no composition on record for this subject"; correctness absent → *unmeasured* beside its coverage; a layer not declared → "no layer map is declared". The page pins its subject; a unit-block drill re-pins to that unit's read. Signals and Sources keep their kinds' personal-only mode | the eight tabs' reads (below) |
| ↳ Target Overview | `target.overview` → `subject.page#runtime` | alias | Now the **Runtime** tab of the **subject page**. Descriptor metadata, the runtime actor roster with source cursors and error counts, recent signals and findings, and three brief actions in the header: **"read as page"** opens the **Desk Brief** panel (`target.desk_brief_page`) on this desk — the same composed document, read in place instead of downloaded; "desk brief" downloads the desk's current composition + unit reads + open situations + the desk's typed absence as one markdown file; and "print / save as PDF" prints the brief just composed — the same print document the Report Export panel builds, from the bytes already downloaded rather than a second compose | `/targets/{id}/runtime`, `/signals`, `/findings`, `POST /v3/export`, `/situations`, `/v3/events` |
| ↳ Desk Brief | `target.desk_brief_page` → `subject.page#brief` | alias | Now the **Brief** tab of the **subject page**. The desk brief as a PAGE — what `POST /v3/export` already composes, read on one scrollable surface organised by the desk's bounded units instead of downloaded. The index card carries the composition record with its fenced country voice beside it in its own band, the scale/method stamp, the as-of, its faithfulness and — never pooled with it — its correctness badge with that badge's own coverage. Then one block per unit in the composition's **declared** order (the coverage register it was built over), each cited through the one prose renderer so every marker keeps its source tag and fold chips, with the temporal layer printed where a historical series is cited — valid time, record time and the producer's tense marker verbatim. Each unit block prints its read's verify line in the route's own words, so a memory-only read says *carried, ungraded* and never a faithfulness number. Below them the **evidence table** runs over the desk's unit ROSTER, not the carried set: one row per bounded unit with an `Evidence state` drawn from our own vocabularies (the composition's coverage register — `in basis` / `below floor` / `unverified` / `no head in horizon` — then the scorecard's `insufficient evidence`, then the cadence check's `stale` / `no read yet`), the record that decided it, the unit's faithfulness and correctness coverage, and the [typed absences](GLOSSARY.md#analysis--methods) whose subject is that unit; beside it the desk's declared source-layer aperture (present / absent / unmeasured / undeclared, each in the operator's own sentence, and "no layer map is declared for this desk" when none names it). Then the endnotes, each with its masthead and the route's own date word (`published` / `fetched`) kept verbatim, an undated citation stating the absence in words. Finally **Reading limits** — the scale era, the composition as-of, the document's compose instant, the absence read instant, the judge scorecard's instant and the grader reference's window and state — and a GENERATED *what this page does not publish*, assembled from the absence route's own `not_measured` classes, its per-kind held-back counts, the coverage register's unfilled units, the export's own missing-id placeholders and the grader's reference state, never from hand-written prose. Header actions: markdown and print (the print path stays the document of record — it prints the route's markdown through the same print document), **JSON** (the structured export document exactly as served) and **PNG** (`html-to-image` over this page's own DOM). The subject page around it pins the subject; opening a unit block re-pins Consult to that unit's read, so the Consult tile follows without a consult panel bolted on. Since the subject page the roster is the subject's own and every subject word is the profile's noun (the Subject page row above). An unmeasured figure prints as `unmeasured`, never as `0` | `POST /v3/export` (`format:'json'`), `/findings`, `/situations`, `/v3/events`, `/v3/eval/country_scorecard`, `/units/{target_id}/correctness`, `/v3/layers/divergence` |
| ↳ Target Signals | `target.signals` → `subject.page#signals` | alias | Now the **Signals** tab of the **subject page**. The raw signal table for the target's geo scope, with source and language filters and geo / entity-class / tag chips | `/signals` |
| ↳ Target Findings | `target.findings` → `subject.page#evidence` | alias | Now the **Evidence** tab of the **subject page**. The target's findings, severity-badged and sortable, with topic-tag chips; above the list, a gap strip shows one cell per bounded unit — current / insufficient-evidence / stale / no-read, sourced from `/findings` and the banded scorecard. A green cell opens its read; every other cell drills into the [typed absence](GLOSSARY.md#analysis--methods) behind it (7b/k5), and the Inspector renders that absence's kind, its proof — what was checked, when, and the record of that look — and whether anyone has looked since | `/findings`, `/v3/eval/country_scorecard`, `/v3/absence?scope=` |
| ↳ Target Situations | `target.situations` → `subject.page#situations` | alias | Now the **Situations** tab of the **subject page**. Situations bucketed by lifecycle state, with intensity-derived severity and contributing-finding links. An expanded frame shows the scale its intensity is a reading on, so a 59 here is not silently compared with a 59 from before the August re-base | `/situations` |
| ↳ Target Claims | `target.claims` → `subject.page#claims` | alias | Now the **Claims** tab of the **subject page**. Claim-like findings carrying corroboration score and sources, confidence-sorted with an evidence chain | `/findings`, `/facts`, `/claims` |
| ↳ Target Graph | `target.graph` → `subject.page#graph` | alias | Now the **Graph** tab of the **subject page**. The per-target lineage graph with a depth slider, row-kind filter chips and click-to-re-root | `/lineage/{kind}/{id}?direction=both` |
| ↳ Target Sources | `target.sources` → `subject.page#sources` | alias | Now the **Sources** tab of the **subject page**. A per-source ingest-health rollup from the target's signals grouped by source, joined to the source registry for name and handler kind | `/signals`, `/registry/descriptors?family=source` |
| Target Map | `target.map` | per target | A clustered overlay of geocoded signals and severity-coloured finding markers, with layer toggles, a per-country breakdown and provenance on hover | `/signals`, `/findings`, `/entities` |
| Target Timeline | `target.timeline` | per target | Signals, findings and situations over time, with situation lifecycles drawn as spans | `/signals`, `/findings`, `/situations` |
| Analyst Runs | `analyst.runs` | per analyst | Runs, outputs and critiques in three independently paginated tabs | `/analysts/{id}/{runs,outputs,critiques}` |
| Analyst Outputs | `analyst.outputs` | per analyst | A live tail of this analyst's findings with a severity histogram and run count | `/findings?analyst_id=` plus the `analyst.*.finding` tail |
| Cross-target Analyst | `analyst.cross_target` | per analyst | Per-target contribution counts and a contradiction-first correlation surface, for the cross-target analyst kinds | `/findings` |
| Critic Scores | `analyst.critiques` | per analyst | The critique history of this analyst's outputs: per-rubric-axis bars, the overall trend, the revision delta | `/analysts/{id}/critiques` |

## The map: subject geometry and layer apertures

The map (`v4.map`, `panels/v4/MapPanel.tsx`; two renderers, MapLibre by
default and Leaflet as the no-WebGL fallback) is a VIEW over the instance's
subjects and records, and what it can draw is the PROFILE's to say, not the
exemplar's. Three pieces carry that, and both renderers read the same ones.

**Whether there is a map at all** — `profile_ui.json`'s `map.geometry`:

| `map.geometry` | What the instance gets |
|---|---|
| `iso2` (geopolitics) | the map, with the layers below; subjects place through their declared ISO-3166 codes |
| `declared` | the map without the country choropleth or the co-mention arcs (both need ISO codes); subjects place through a declared point or polygon |
| `none` (the sanctions skeleton) | **no map surface.** No surface seeds it (the subject field's slot stands where the map was), the `＋` catalog and a subject's drills do not list it, `open_panel(map)` and `map(...)` are refused **`no_map_surface`** by the one executor for a case and for the operator alike (the Consult tile's case rail prints the refusal on the step), and a map tile restored from a layout saved under another profile says so instead of drawing |

**The subject geometry contract** (`lib/subjectGeometry.ts`). A subject's
place is what its target descriptor DECLARES, read as data from
`GET /registry/descriptors?family=target` (the Target Registry panel's read,
on the same query key): `scope.geo`'s codes, or a GeoJSON Point / Polygon /
MultiPolygon in `scope.geometry` where a descriptor carries one. **Never an
ISO code parsed out of a subject's id.** `geometryFor(subjectRef)` returns
`iso2 | point | polygon | none` — `none` always with its reason ("declares no
geometry in its scope", "the subject roster has not been read yet", …) — and
`cameraFor` turns it into a camera move: a point or a single code flies to
itself, a polygon or several codes fit their bounds, and `none` leaves the
camera where it is rather than flying to (0, 0). A case's `map fit_ref`
resolves through exactly this (it focuses the ref; the selection-driven
camera fits the declared geometry), and so does a finding that carries no
coordinate of its own: it places at its subject's geometry only when that
geometry names ONE place, and stays unplaced for a region. The target schema
does not admit a `scope.geometry` yet ([SEAMS.md](SEAMS.md) #66), so today
every declared geometry is a code list.

**The layer catalog** (`lib/layerCatalog.ts`). Every layer the map can draw is
declared once with the profile geometries it can draw under, the renderers
that draw it, whether it starts on, and its **aperture** — what the layer can
SEE, which is the `i` beside it (the `src/ui/tooltip.tsx` primitive: hover,
keyboard focus and a tap all open it, and the same sentence is the trigger's
accessible description):

| Aperture line | States |
|---|---|
| Reads | the route and its row cap |
| Source layers | which of the six declared source layers can reach the layer |
| Window | the shared window (the one clock), or the layer's own lookback, named as such |
| Subjects | the subject roster it reads, or that it reads none |
| Placement | how a row earns a position, and what is never drawn |

| Layer | Geometries | Renderers | Starts on |
|---|---|---|---|
| Verdict choropleth | `iso2` | MapLibre | yes |
| Signals · Findings · Situations · Events | `iso2`, `declared` | both | yes |
| Entities | `iso2`, `declared` (declared, never drawn) | none yet: the case vocabulary names it, no renderer draws it, and its row says so (`—`) | no |
| Signal density (heat / hex) | `iso2`, `declared` | MapLibre | no |
| Co-mention arcs | `iso2` | MapLibre | no |
| Convergence | `iso2`, `declared` | MapLibre | yes |
| Watch locations | `iso2`, `declared` | MapLibre | yes |

The default layer set is the profile's layers that start on, so the
geopolitics profile keeps exactly today's layers (a test snapshots the ids) and
no profile is forced onto a choropleth it cannot fill. The LayerPanel lists the
store's draw layers with their `i`; the MapLibre mode control carries the same
`i` on the choropleth, density, arcs, convergence and watch rows. **Absence:**
a layer whose read has not answered prints *no read yet*, never `0`; a read
that answered with nothing in the window prints a measured `0`; each count
is a `Figure` with its unit (`in window` / `on record`) and its as-of (the
window's end, or the newest row). A choropleth with no composition on record
says *choropleth: no read yet* on the collapsed control instead of tinting
nothing. A windowed layer is cut on its rows' VALID time
(the publisher's instant for a signal, the newest member event for a
situation); a row that carries no valid time is not drawn on the window — its
fetch time is never borrowed.

The six SOURCE layers' per-subject declaration (present / absent /
unmeasured / undeclared, from `/v3/layers/divergence`) is a different object
and keeps its own section beneath the draw layers (`lib/layerAperture.ts`).

## The Accuracy page

`system.eval_scorecard` is the roster-wide answer to the Trust stance's
question, *is what it produced any good?* (`unit.page#quality`
will read the same routes for one unit). It runs top to bottom in five
sections, one file each under `panels/system/EvalScorecard*.tsx`, every
derivation pure in `lib/accuracyPage.ts` and every number a `Figure`
(`{value, unit, asOf, state}`):

1. **Cohort.** One generated paragraph: the units the profile declares
   (`rosters.units`), the subject rosters it scores (`scored_subject_tags`,
   in the profile's labels), the grader's window and grain with its pair and
   subject counts, the critic page's span, the gold set's pinned weeks and
   the calibration as-of. The nouns are the profile's (`lib/profileUi.ts`);
   a profile that declares none gets the platform's — never "countries".
2. **Sealed ledgers.** Two stage tables, never pooled — the acute-forecast
   pilot and the operator gold set — each stage *entries → resolved →
   scored → voided → awaiting a judge* a count beside the denominator it is
   read against, both with an as-of. The forecast ledger's entries, resolved
   and voided stages read *not measured*: the `forecast_scoreboard` finding
   carries those totals and no read route projects them. Scored comes from
   `/v3/eval/calibration`; awaiting a judge from `/v3/since`'s
   cursor-independent `forecasts_due` section. The gold-set counts are read
   live, so their as-of is the client's read time, marked *(read live)*.
3. **Calibration.** The skill scoreboard, band calibration (ordinal
   persistence and reversal, each horizon with its own denominator) and the
   banded verdicts per subject that harness grades.
4. **Graders.** A table — per grader the graded count, the population it
   was drawn from, what was kept out (*unmeasured*, *carried, ungraded*,
   unresolvable, not found) and the as-of — and below it each grader's own
   block. A grader that graded nothing reads *unmeasured*, never a row of
   zeros. The machine grader's roster is the profile's CORRECTNESS roster,
   not its countries: every subject the profile's scored and
   `reference_subject_tags` legs cover — in the example instance the
   supply-chain lanes and flows sit beside the country desks, each row with its own
   denominators — and no panel code changed to show them.
5. **What this page does not publish.** Generated, never hand-written:
   `scripts/gen_not_published.py` writes `lib/not_published.json` from the
   measurement seams of [SEAMS.md](SEAMS.md) (the class is declared in the
   script as `MEASUREMENT_SEAMS`; a listed seam that is resolved or missing
   fails the generation) and from each eval route's own declaration of what
   it does not publish (a `Field` description, a marked model docstring
   paragraph or a module constant, read with `ast`). The page attaches each
   route class's live figure. `tests/test_gen_not_published.py` fails when
   the committed JSON is not a fresh generation; regenerate with
   `python3 scripts/gen_not_published.py` after editing either source.

## The Inspector

The Inspector (`components/inspector/InspectorPanel.tsx`) is the console's
primary read surface and the keystone the rest of the panel set brushes into. It
is driven entirely by the selection store: click any row, map dot, graph node or
id anywhere and its full detail loads here, every referenced id rendered as a
link so the next selection is one click away and a breadcrumb trails behind.
With nothing selected it shows a call to action rather than an empty frame. It is
built on the existing panel chrome and descriptor view and reuses the provenance
trail — not a second rendering stack.

**A subject opens on its read** (`components/inspector/SubjectReadState.tsx`).
A subject selection (a rail click, ⌘K, a map click) shows its READ STATE first:
the **last read** — the newest row any of the profile's composing frames
published on it (the subject page's own query), its producer, its as-of and its
verify line in the route's terms (faithfulness when judged, the
judge-unavailable line when not, *not judged* when the row carries no verify
block), or *No composition on record for this desk.* in the profile's noun —
with a button to the subject page; then the **typed absences** from
`/v3/absence?scope=` with the read's as-of: *carried, unsupported* on its own
line (a count of units, or *not measured*), every kind present with its count,
every kind the route could not read as *not measured*, and how many kinds were
read and found nothing. `—` while a read is in flight, *unavailable* when it
failed, never 0. The registry descriptor (packs, hop limit, version) and the
provenance trail sit folded under **Configuration**, one click further, never
first. The kind chip names the subject by the profile's noun.

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
pass. A read whose judge was asked and did not answer — every rung of the judge
fallback chain failed, so only the deterministic floor ran — carries a third chip,
`unverified since <UTC minute> (judge unavailable: <http status or class>)`, read
from the verify block's `unverified_reason` (a missing time renders `—`, never an
invented one); a verdict a later chain rung wrote names that judge in the
confidence chip's popover, and says so when it is the weak in-house rung.

**The unverified state** (`v4/read/UnverifiedMark.tsx`, `lib/unverifiedState.ts`;
X4 READPATH) is what a finding with NO verify verdict renders as, on every
findings surface — the Read's header, the Assessment band, the Morning Read's
Checked band and the Live Feed's badge line: `unverified · judge pending` while
the verify pass may still land, `unverified · not judged` once it will not —
never a plain current read, never a `0`. The registry says which
(`registry/verification_state.py`), the UI never infers it: `/findings?fields=judgment`
returns `verification: {score_state: "pending"|"absent", since, judge_window_s, reason}`
for such a row, and the full weight keeps `verification: null` and carries the
same block as `verification_state`. A row is `pending` for 15 minutes after its
`produced_at` (the verify pass runs after the finding is written and landed within
549 s at most over the 7 days measured); after that, or at once for a verify-exempt
producer, it is `absent`. The mark's tooltip names the meaning and the clock. A
live-tail row arrives ahead of its verify pass and reads `judge pending`; a row
from a registry that predates the block reads `not judged`. In the Live Feed a
verify-exempt row keeps only its `unverified — structural` badge.

**One request per `/findings` question** (`lib/queryCache.ts`; X4 READPATH):
`apiGet` sends identical `/findings` GETs once while one is in flight, and reuses
a successful answer for 30 s from its request's start (a poll on exactly 30 s is
always fresh); a failure is never cached, any write through the api layer drops
the cache, and every panel's Refresh button (`PanelChrome`) drops it before it refetches. Every other
route fetches as before. Shared answers are read-only — copy before sorting.

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
states no fold reason renders exactly as it did before. A chip may also read *carried from the prior read / window ledger, not re-sourced*: the claim asserts a date or a figure whose only support is this desk's own memory blocks, which cannot establish either — the judge counts it against the finding exactly like an uncited claim.

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
renders the absence's kind from the closed nine-kind vocabulary, its proof
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
zero, or the current version back-filled onto it; a stampless row written AFTER
they existed (a composition — its producer publishes on no declared scale) reads
**"no declared scale"**, never the pre-era label. The reader finds a stamp at the
top level, under `data`, or — on a `/findings` row, whose `data` is the whole
finding envelope — under `data.data`. The same chip rides the
Situations frame, the Accuracy page's band calibration, the desk-baseline row and the Morning
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
   `PREVIEW_KINDS` promoting a guarded-preview surface. Operator-category panels
   ship `personal`-only, which a registry test enforces. There is no hidden
   flag: a kind whose params need a ref is a drill — declare its parent in
   `SINGLETON_DRILLS` (`panel-registry/kindPlan.ts`) — and every other kind is
   in the `＋` catalog.
3. Give the kind an absence contract: one `KIND_SURVIVOR` row in
   `panel-registry/absenceContracts.ts` naming the surface whose contract it
   answers to (itself, with a new `ABSENCE_CONTRACTS` line, if it is a new
   surface). `tsc` and a registry test refuse a kind without one.
4. Give it a home (the SURFACE RULE, `kindPlan.test.ts`): a page's stage or
   panels (`lib/surfaces.ts` — and a page that seeds past what the brief allows
   is a design change, not a row), the alias table / Table B's schedule, or the
   drill list. A new kind also counts against the ≤ 33 ratchet below, and a new
   READER view against the reader-reach ratchet (`surfaces.test.ts`).
5. Declare its CONTRACT (`panel-registry/contracts.ts`): its name and
   one-line description, its catalog group, what it binds to, its size class,
   its home (the stage or a panel) and the actions it accepts with their typed
   arguments; a mounted view takes over an action with
   `usePanelActionHandler`. A reader view must declare one
   (`contracts.test.ts`); an operator view without one is listed in
   `OPERATOR_KINDS_WITHOUT_CONTRACT`. A view that is NOT a registry kind is
   registered whole with `registerPanel(contract, Component)` — no edit to the
   frame; `components/ViewHost.tsx` renders it on the stage and in the panels.
6. Regenerate the matrix: `python3 scripts/gen_release_state_matrix.py`. A drift
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
feed is a "something changed" nudge. A retired registration row never suppresses the synthesized bound read for its
instance (`panel-registry/synthesize.ts`), and its id stays resolvable so deep
links into it do not 404.

## Retired ids, drills and the kind ratchets

A retired panel id keeps resolving forever, because saved layouts, palette deep
links, share hashes, recorded cases and registration rows all persist ids. The
**alias table** (`panel-registry/aliases.ts`) is one line per retired id naming
its survivor and, where the survivor is tabbed, the tab that *is* the retired
surface. Four call sites resolve through it: the panel resolver, both panel
openers in `App.tsx` (threading the tab through), a pre-pass on both layout
loaders that rewrites stale ids, **collapses duplicates** (a layout holding
three ids that now name one tabbed panel becomes one tile) and drops the
unresolvable before Dockview, which has no per-panel fallback for an unknown
component, can throw — and the one executor, so a recorded case naming a
retired id still opens its survivor. When the survivor is ALREADY on the wall,
the singleton opener re-focuses it and posts the tab
(`state/panelParams.ts::requestPanelTab`); the merged wrapper follows it
(`panels/merged/useMergedTab.ts`), so a retired id lands on its tab whether or
not its survivor was open. The current aliases (40) resolve:

| Survivor | Retired id → tab |
|---|---|
| Subject page (bound) | `target.desk_brief_page` → Brief · `target.findings` → Evidence · `target.claims` → Claims · `target.signals` → Signals · `target.situations` → Situations · `target.sources` → Sources · `target.graph` → Graph · `target.overview` → Runtime. A bound tile `target.findings:<id>` becomes `subject.page:<id>` — the pre-pass re-points its registration (panel id, layout slot, title) and keeps its binding; one subject's several retired tiles collapse onto ONE page (the first tile's tab wins), another subject keeps its own. The bound opener follows the alias tab, and an open page switches tab on its own key (`subject.page:<id>`) |
| Timeline | `v4.timeline` → Events |
| Provenance | `v4.why` → Why · `system.lineage` → Lineage · `source.fanout` → Fan-out |
| Register | `system.situations` → Situations · `system.narratives` → Narratives (re-pointed off Provenance when panels were merged) |
| Alerts & Watches | `system.watchlist` → Watches · `system.alert_center` → Triggers · `system.escalations` → Deliveries |
| Consult | `system.deep_consult` → Deep |
| Entities | `system.entity_graph` → Graph · `system.notable_structure` → Structure |
| Registry | `registry.targets` → Subjects · `registry.analysts` → Units · `registry.sources` and `source.detail` → Sources (the one tab both land on: it mounts the list and the per-source detail together) · `registry.stack` → Stack · `registry.action_packs` → Packs · `source.subscription_builder` → Builder · `source.subscription_policy` → Policy · `v4.flow` → Canvas (re-pointed off Provenance) |
| System Status | `system.production_gauge` → Production · `system.dead_letter` → Failures · `system.actor_health` → Actors · `system.stream_lag` → Queues |
| Ledgers | `system.governor` → Governor · `system.audit` → Audit · `system.budget` → Budget |
| Optimizer Candidates | `system.optimizer.diff` → Diff (its `candidate_id` rides the survivor's params) |
| The Wall | `v4.kpi` → the header band (no tab: it is on every tab) · `system.wall_movers` → Movers |
| Morning Read | `system.navigator` (no tab: the products index is the object rail's Reads section) |

A saved panel id of either form resolves — the dotted kind, or the
descriptor-facing `panel_id` (`system_dead_letter`), whose table is derived
from the kind table so a retirement cannot be half-declared. A rewritten tile
takes its survivor's title.

**"Hidden" is gone.** `HIDDEN_KINDS` held eight kinds for the old sidebar
budget. A kind whose params need a ref is a DRILL — every bound kind, plus
`analysis.cross_framing` (opened from a claim) — derived in
`panel-registry/kindPlan.ts`, never listed, opened from its parent object.
`system.optimizer.diff` was the other singleton drill until it became the
Optimizer's Diff tab. Of the other former hidden kinds,
`analysis.contentions` is seated in the Trust stance and the rest retired into
survivors' tabs.

**The ratchets** replaced the 26-row sidebar budget in the commit that deleted
the tree (`panel-registry/kindPlan.test.ts`, `components/ObjectRail.test.tsx`),
so panel growth was never unguarded: panel rows in the rail **0**; registry
kinds OFF Table B's retirement schedule **≤ 33** (31 survivors + 2
headroom — 30 today, `subject.page` among them, plus `unit.page` still
planned); the total **≤ 38** (`REGISTRY_TOTAL_CEILING`: 62 → 45 by the panel merges,
which retired twenty kinds and registered `system.registry`, `system.ledgers`
and `system.register`; 45 → 38 by the subject page, which retired the eight
per-subject reads into `subject.page`'s tabs — the unit half, the map and clock
with a subject param, the A/B close and the palette's `/` family take it to
31); `registry.ts` exports no `HIDDEN_KINDS`;
and **the surface rule** — every kind appears on a page (its stage or its
panels), is frame (Consult, the Record sheet), is in the alias table (including Table
B's scheduled rows) or is in the drill list with a declared parent. **The
arrangement ratchets** (`lib/surfaces.test.ts`): named arrangements 17
(six workspaces, four missions, seven presets) → **8 surfaces** (four reader,
four operator), plus user missions and the Save/Restore slot → none; SEEDED
panel regions per page ≤ **2** (was ≤ 3 regions plus a fixed Consult
column — the stage and the chat are frame, and the reader may add a third);
panel kinds reachable from the reader side ≤ **9** (the reader pages' stages
and panels plus Consult and the Record sheet, where every
catalog kind — 33 — used to be one click away); reader navigation rows on the
top bar = 4. Table B lives in `kindPlan.ts::RETIREMENT_SCHEDULE`, all 36
rows: a row a lane has landed is flagged `retired` and must match its
`aliases.ts` row (same survivor, same tab); a pending row must still name a
registered kind.

Most cross-panel coordination flows through the selection store. A panel that
opens another drives one `open_panel` through the one executor (see "The
command model"); the `legba:open-*` DOM events and their `window` parks are
retired.

## Data access and the auth chain

**REST.** `lib/api.ts` is a thin client against `/api/v1`, attaching
`Authorization: Bearer <token>` only if a token exists in
`localStorage.legba_token` and surfacing errors as a typed `ApiError`; most
panels read through `@tanstack/react-query`. In production localStorage is
empty and the app sends no `Authorization` header: the edge supplies the
credential, or the session cookie carries the person (below).

**Live updates.** `lib/ws.ts::subscribeRegistryEvents` opens a WebSocket to
`/api/v1/registry/events?filter=<subject>`, the registry's event multiplexer,
with exponential reconnect backoff. The credential is never in the URL: when
localStorage holds a token the client offers it as a WebSocket **subprotocol**
(`legba.bearer.v1, <base64url token>`), which travels as a header, and the server
echoes back only the scheme name. A query-param token would be byte-identical to
the registry's admin credential and would land in every access log that records a
request line; the server still accepts one so a stale build does not hard-break
on deploy, and logs a deprecation event each time.

**The edge.** In production the app is served by `legba-caddy`
(`docker/Caddyfile`), which fronts the site with automatic TLS and proxies the
registry. What it does with a browser's request depends on two switches in
`.env`, `LEGBA_AUTH_SIGNIN` (accounts, default off) and `LEGBA_EDGE_PERIMETER`
(the perimeter password, default on):

| Sign-in | Perimeter | A browser with no session cookie | A browser with a `legba_session` cookie |
|---|---|---|---|
| off (default) | on (default) | passes the perimeter password, then the edge replaces its `Authorization` header with the registry's admin bearer: every person is the same admin | not used |
| on | on | passes the perimeter password, then reaches the registry with no credential: `/auth/me` answers 401 and the sign-in screen shows | passes the perimeter password; the edge strips the Basic header and injects nothing, so the registry sees the person and their role |
| on | off | reaches the registry with no credential; the sign-in screen shows | the registry sees the person and their role; the registry's sign-in is the only gate |
| off | off | refused: the edge does not start on this pair, because it would hand the admin token to anyone | — |

**The perimeter password.** Caddy's `basic_auth`, checked against a bcrypt hash
read from `LEGBA_BASIC_AUTH_HASH` in the gitignored environment file and never
committed. Where it is on, it gates the app bundle itself and every API path,
the event stream included. Rotating it is one `caddy hash-password` away. The
ntfy site keeps its password whatever the console's switch says.

**Signed-in users.** A person signs in with their email and password
(`auth/SignInScreen.tsx`); the registry answers with a `legba_session` cookie —
`HttpOnly; Secure; SameSite=Lax; Path=/`, host-only, so the app never reads it
and the browser attaches it to every same-origin `fetch`, the events socket
and the Consult `EventSource`. A write sent from another origin with that
cookie is refused. A session ends after 12 idle hours (`LEGBA_SESSION_IDLE_HOURS`)
and after 72 hours at most. Sign out revokes the session and returns to the
sign-in screen.

**The event stream.** The `new WebSocket()` API cannot attach an
`Authorization` header, so the event path carries the browser's cached
perimeter credential (or its session cookie) on the upgrade like any other
request, and the edge applies the same rules as for `/api/*`. With sign-in off
the edge injects the admin bearer on the upgrade after the perimeter has
passed; with it on, the cookie rides the upgrade and nothing is injected.

Caddy polls the registry's unauthenticated readiness probe and drops a
half-dead upstream from rotation, so a registry that cannot reach its
substrate fails requests fast rather than hanging. If a stale token is left in
localStorage, the app sends a Bearer that collides with the Basic perimeter
and triggers a re-prompt loop; clearing `legba_token` and hard-refreshing fixes
it. The identity chip marks `stored token` when localStorage holds one. In
development, `npm run dev` proxies `/api` and `/ws` to the registry, so there
is no Caddy in the loop, and a registry in dev mode accepts any token or none.

## The phone shell

A second shell for phones is served at `/m/` (`legba-ui-v3/src/mobile/`,
`shell/PhoneShell.tsx`). It reads the same routes the reader pages read and
drives the same contracts; it is the reader side on a small screen, not a
separate product. There is no operator side on the phone.

```
┌ top bar: Back (after any drill-in) · the screen's name · refresh · Settings ┐
│                                                                              │
│  the screen: the tab's root, or a page pushed over it, or the Record page;  │
│  the Record's peek is a sheet over all of them                               │
│                                                                              │
└ bottom bar: Changed · Watch · Ledger · Consult · Subjects ───────────────────┘
```

| Tab or page | What it shows |
|---|---|
| **Changed** (the landing) | since you last looked, what paged, the Board's ranked list and the forecasts falling due, over one `/v3/since` read per window; a read opens as a page. The tab's badge counts only what paged. |
| **Watch** | the alert log, silences and source edits; an alert's page with its decision, state and subject, and **Acknowledge** and **Snooze** for a contributor or an admin (a reader is told who can, while the alert is open) |
| **Ledger** | the sealed forecasts and the track record, tables narrowed to the screen with the first column held; the published evaluation runs as a page |
| **Consult** | the same Consult as the desktop, full screen, through the shared request builder and turn hook; with drive mode on, an answer's panel actions navigate the phone with Undo |
| **Subjects** | search, recently opened, and the roster; a subject's page stacks its sections (latest reads, what changed, forecasts, the map) |
| **Map** | the drawn map at rest (no WebGL, no map library); the globe loads only when asked for, and pauses on a lost graphics context |
| **Record** | a peek sheet (the statement, the row it maps to, how many rows rest under it), then the whole chain as a page |
| **Settings** | who is signed in and sign out, that alerts go to ntfy, where the seen cursor is kept, and a link to the desktop view |

**The same contracts as the desktop.** The phone registers its own frame port
and shell port (`phoneFramePort.ts`, `phoneShellPort.ts`), so cross-links and
Consult's actions reach it through `applyPanelAction` and `drive`. Its address
is the desktop's hash grammar plus `#screen=` (`phoneAddress.ts`), with browser
history as the stack, so a `#rec=` link opens the Record on the phone as it
does on the desktop. Each tab's root stays mounted once visited, so its scroll
and its reads survive a drill-in and Back. Every control is at least 44 px in
both directions. Read telemetry carries `workspace = mobile`, so a read counts
once whichever screen it was read on.

**Its own bundle.** `vite.config.mobile.ts`, entry `m.html`, output `dist/m/`,
rather than a second entry in the desktop's build, because sharing one Rollup
graph re-partitions and re-hashes the desktop's chunks. `npm run build` runs
both builds. Caddy serves `/m/` from the same volume with its own SPA fallback
to `/m/index.html`, so a deep link boots the phone bundle; a bare `/m` redirects
to `/m/`.

**A phone is sent here.** A browser whose viewport is at most 480 px wide that
opens the desktop is redirected to `/m/` before anything mounts
(`lib/phoneSurface.ts`; an invite or `#signin` link keeps its fragment), and
`?desktop=1` (the *Desktop view* link in Settings) keeps the desktop for the
tab's session. `/m/` mounts behind the same `SignInGate` as the desktop: a 401
shows the sign-in screen, and a session that ends while the page is open shows
it again with the reason.

**Installable, with no app store.** `/m/` is a web app the browser can install
(Android Chrome: the install line or the ⋮ menu; iPhone Safari: Share → Add to
Home Screen). `public-m/manifest.webmanifest` gives `id`, `scope` and
`start_url` `/m/`, `display: standalone`, and icons at 192 and 512 px plus a
maskable pair and a 180 px `apple-touch-icon`, all drawn from the one
`favicon.svg` by `tools/make-phone-icons.mjs` (a test redraws them and compares
pixels). A hand-written service worker (`src/mobile/pwa/sw.template.js`,
emitted as `/m/sw.js` with its shell list filled from the bundle) controls
`/m/` only, never the desktop. It stores the shell (the page, the entry's
scripts and styles, the icons and the manifest) and nothing else; it never
answers or stores anything under `/api/`, `/ws/` or `/tiles/`, and that refusal
is in the worker's own code and tested, because the Cache API ignores
`Cache-Control`. Offline, the shell opens from the device and says "No
connection. Showing what was loaded at HH:MM UTC." Nothing of the record is
kept on the device, and coming back online refreshes the screen. A new version
waits until the person taps **Reload** on "Update available". Sign-out on the
phone drops the worker and its caches and asks the registry for
`Clear-Site-Data: "storage"`. There is no web push: alerts stay on ntfy.

The edge's part (the manifest, the worker script and the icons served without
the perimeter password, and the cache headers) applies only with
`LEGBA_AUTH_SIGNIN=1`: the `phone_app_*` snippets in `docker/Caddyfile`. With
sign-in off the phone shell still works in a phone's browser, behind the
perimeter, but is not installable. What can only be checked on a real phone is
listed below.

### Testing the phone app on a real phone

The phone reader at `/m/` can be installed from the browser as an app, with no
app store. The research behind it could not confirm some behaviour without a
real phone, so these checks are for you to run on your own device: Android
with Chrome first, then iPhone with Safari.

Each item says what to do and what should happen. Mark each one pass or fail.
For a fail, note the phone model, the OS version, and what you saw. A
screenshot helps.

**What the app keeps on the phone.** Only the app's own code and icons, so it
can open with no connection. It never stores reports, evidence, record text,
Consult answers or anything else from the server. Offline, it shows only what
was loaded since the app was last opened, and it says when that was. Alerts
still come through ntfy. The app has no push notifications, and no alert
passes through Apple or Google.

#### Before you start

- Sign-in is on (`LEGBA_AUTH_SIGNIN=1`), and you have a user account to sign
  in with.
- The edge runs a Caddyfile that has the `phone_app_*` snippets. With sign-in on, the
  manifest, the worker script and the icons are reachable without the
  perimeter password. The pages themselves still ask for it unless
  `LEGBA_EDGE_PERIMETER=0`; run the list once each way if you can.
- On the computer, have the console at `/` open in a browser, so you can see
  whether anything you do on the phone shows up there.

#### Android, Chrome

| # | Do this | This should happen |
|---|---|---|
| A1 | Open `https://<your domain>/m/` in Chrome. Enter the perimeter password if asked, then sign in. Use the app for about 30 seconds and tap something. | A line appears at the top: "Install Legba on this phone", with **Install** and **Not now**. You may also see Chrome's own install entry in the ⋮ menu ("Install app" or "Add to Home screen"). |
| A2 | Tap **Install**, then confirm. | Chrome's install sheet shows the Legba icon (dark square, grey cross, magenta dot) and the name "Legba". After installing, the icon is in the app drawer and on the home screen. The icon is not cropped, whatever icon shape your launcher uses. |
| A3 | Open the app from its icon. | It opens full screen with no address bar, on the phone reader. The status bar is dark. Nothing sits under the status bar or the navigation bar. |
| A4 | In the app, use it as normal: open a report, ask Consult something. | It works as it does in the browser. You stay signed in. |
| A5 | Close the app completely (swipe it away in recent apps) and open it again. Then repeat after 1 hour and after 13 hours. | After 1 hour: still signed in. After 13 hours (over the 12-hour idle limit): the sign-in screen, and after signing in everything works. Note whether Chrome asked for the perimeter password again, and whether that prompt appeared inside the app. |
| A6 | Turn on airplane mode. Open the app (or pull it back up and use **Try again**). | The app opens and draws its frame. The top line says "No connection." and gives the time it last loaded something, or says nothing has loaded yet. It shows no report text it did not already have on screen. |
| A7 | Turn airplane mode off. | Within a few seconds the "No connection" line goes away and the screen refreshes. |
| A8 | Leave the app open. On the server, deploy a new UI build. Bring the app back to the foreground, or wait up to an hour. | A line appears: "Update available." with **Reload**. The app does **not** reload by itself. |
| A9 | Tap **Reload**. | The app reloads once, and the line does not come back. |
| A10 | From another app (for example a chat), tap a link to `https://<your domain>/m/`. Then tap a link to `https://<your domain>/` (the desktop). | The `/m/` link opens in the installed app. The desktop link opens in Chrome, not in the app. |
| A11 | In the app, sign out (Settings → Sign out). If you can, connect the phone to a computer and open `chrome://inspect` > the app > Application. | The app shows the sign-in screen. Under Application, Local Storage and Cache Storage hold nothing from before the sign-out. The app may store its own code again straight away; that code holds no data. Using the app again needs a new sign-in. |
| A12 | Rotate the phone to landscape on a screen with a notch or punch-hole camera. | No text or control is under the notch or the rounded corners. |
| A13 | On the computer, open `/` (the desktop). Check DevTools > Application > Service Workers, or just use it normally. | The desktop is not controlled by the phone app's worker, and it behaves exactly as before. |

#### iPhone, Safari

| # | Do this | This should happen |
|---|---|---|
| I1 | Open `https://<your domain>/m/` in Safari. Enter the perimeter password if asked. | One line appears at the top: 'To install: tap Share, then "Add to Home Screen"', with **Got it**. After you tap **Got it** it never comes back on this phone. |
| I2 | Tap Share, then **Add to Home Screen**, then **Add**. | The Home Screen shows the Legba icon, which is the full dark square with no white border, and the name "Legba". |
| I3 | Open the app from the Home Screen. | It opens full screen with no Safari bars. The status bar area is the app's dark colour, and the clock and battery are readable. Nothing sits under the notch, the Dynamic Island or the home indicator. |
| I4 | Sign in inside the app. Then open Safari and go to `/m/`. | **Expected on iOS:** the app and Safari do not share sign-in. Signing in inside the app does not sign Safari in, and signing in through Safari does not sign the app in. Note what you see. |
| I5 | **The perimeter password inside the app.** With the perimeter on: on the first open, and again after the phone has restarted, see whether the app asks for the perimeter password. | Note exactly what happens: a password prompt, a blank page, or an error. **This is the most important iOS check.** If the app shows a blank page or an error instead of a prompt, run the phone with the perimeter off (`LEGBA_EDGE_PERIMETER=0`), where sign-in is the only gate. |
| I6 | Close the app (swipe it up out of the app switcher) and reopen it after 1 hour, 13 hours and 8 days. | 1 hour: still signed in. 13 hours: the sign-in screen (the idle limit). 8 days: note what happens. iOS clears a Home Screen app's site data after about 7 days without use. |
| I7 | Turn on airplane mode and open the app. | As on Android (A6): the frame draws, the "No connection" line shows, and no report text appears that was not already on screen. If you see a blank white page instead, note it. |
| I8 | Turn airplane mode off and tap **Try again**. | The line goes away and the screen refreshes. |
| I9 | Deploy a new UI build. Bring the app to the foreground. | "Update available." with **Reload**, and no reload by itself. Tap **Reload**: one reload, and the line does not come back. |
| I10 | Sign out in the app (Settings → Sign out). Then close and reopen it. | The sign-in screen shows, and nothing you saw before signing out comes back without a new sign-in. If you can, connect the phone to a Mac and use Safari > Develop > the app > Storage, and note whether anything from before the sign-out is still there. Whether iOS honours the server's "clear this site's storage" instruction inside an installed app has not been confirmed. |
| I11 | Rotate to landscape. | No text or control is under the notch or the Dynamic Island. |
| I12 | Open a Consult question and type in it. | The question box stays above the keyboard. |

#### What to send back

The item numbers that failed, the phone and OS version for each, what you
saw, and whether the perimeter was on. The results of I4, I5, I6 and I10 decide
whether an instance that serves phones can keep the perimeter password in front
of sign-in or should run with `LEGBA_EDGE_PERIMETER=0` and sign-in alone.

## Read telemetry

`lib/readTelemetry.ts` emits read events at seven chokepoint surfaces rather than
at call sites — panel opens, workspace switches, finding opens, citation drills,
provenance walks, consult, the morning read. It batches, flushes on page hide,
and is fail-silent in every path, because telemetry that can break the product it
measures is worse than no telemetry. A failed batch is dropped rather than
retried: a retry would backdate a week of reading into one minute and corrupt the
exact number the ledger exists to produce. The Read Scoreboard reads the rollup.

## Building and running

Run these from `legba-ui-v3/` after `npm install`. The dev server proxies `/api/*`
and `/ws/*` to `VITE_API_BASE` (default `http://localhost:8501`, a
`legba-registry` running locally); override it as
`VITE_API_BASE=http://<host>:<port> npm run dev`.

| Command | Effect |
|---|---|
| `npm run dev` | Vite dev server on `:5174`, proxying to the registry |
| `npm run dev:mobile` | The mobile surface's dev server |
| `npm run build` | `tsc -b && vite build && vite build --config vite.config.mobile.ts` → `dist/` and `dist/m/` |
| `npm run preview` | Serve the built bundle locally |
| `npm run lint` | `tsc --noEmit` type-check |
| `npm run lint:tokens` / `npm run lint:absence` | The two ratchets on their own (both also run inside `npm run test`) |
| `npm run test` | `vitest run` — panel logic that can be tested without a DOM lives under `lib/` by convention, so the tests do not need one |
| `tools/walk/run.sh fixture\|live` | The first-time-user browser walk (below) |

In production, the `legba-ui-build` one-shot job builds into a named volume that
`legba-caddy` mounts read-only and serves with an SPA fallback. Key libraries are
`dockview` (workspace), `@tanstack/react-query` (data), `recharts` (charts),
`maplibre-gl` and `deck.gl` (maps — deck.gl dynamically imported only when a deck
layer is first switched on), `cytoscape` (graphs), `react-markdown` (reads and
consult), Tailwind, and the five Radix packages under the copied-in primitives
(`src/ui/`). See [RUNBOOK.md](RUNBOOK.md) for deploy and operations.

### The browser walk: every UI change is walked before it ships

Two UI changes shipped untested by a browser and hurt: a Consult box 34 px
wide, and a Flow panel that locked the browser on a 336-node graph. So every UI
change is walked in a real (headless) Chromium as a first-time user before it
ships, by `legba-ui-v3/tools/walk/` (its README has the details):

| Mode | Command | Bundle and data | When |
|---|---|---|---|
| fixture | `legba-ui-v3/tools/walk/run.sh fixture --build` | a bundle built from this tree + the committed synthetic fixtures (`tools/walk/fixtures/api.json`), no network | inside the deploy gate (`scripts/run_targeted_gate.sh`, the `walk` phase, whenever UI files changed); any time locally |
| live | `LEGBA_TOKEN=… legba-ui-v3/tools/walk/run.sh live`, or with sign-in on `LEGBA_WALK_EMAIL=… LEGBA_WALK_PASSWORD_FILE=… legba-ui-v3/tools/walk/run.sh live` (signs in through the real sign-in screen as a named account; the password is read from the file and never printed) | the live bundle (volume `legba_ui_dist`) + the live registry, GET-only apart from sign-in | **after every UI deploy**, by whoever rolled it; the report is the record |

The walk visits the sign-in screen without a token, the landing (also with
every API answer held, to catch a count printed before it is read), the frame
(the top bar, the rail, the stage, Consult, the panels), the four reader
surfaces, one subject opened from the switcher, the Consult composer, the
Record sheet over the Board, the operator side's four sections, the Flow panel
(default, one expansion, one expansion the budget must refuse), the landing
and each reader page at **1366×768, 1920×1080, 2560×1440 and 3840×2160**, the
frame's composability at 1920 (the chat divider, the centre's edge and a panel
splitter dragged and checked after a reload; Consult moved to a right column
and back, collapsed to the ask bar and restored, popped to the full centre and
back), and the phone shell at 360, 390 and 430 px wide and in landscape. It is GET-only: every write is answered
locally, consult and model routes are refused, outside origins are blocked. It
fails on: a main-thread task over 1,000 ms; a landing not ready in time (8 s on
fixtures, 20 s live); horizontal page overflow at any of those widths or 390; a
Consult question box under 300 px; a "0" shown before its count was read; any
page or console error; a machine id as a title on the phone; a walk step that
did not complete; reader text under the 12 px floor on the reader pages, the
Record sheet and the phone (`--report-text-floor` reports it without failing);
and the frame budgets — a full-height strip of the main area with nothing
in it wider than 6 % of the viewport, body text (the median visible size)
under 13 px at 1366–1920, 14 px at 2560 or 18 px at 3840, the chat's reading
width outside 30–52 rem, a splitter that does not move or is not kept across a
reload, and a Consult move that leaves the box unusable or the page broken. It records per surface the tile and text element counts and
any engine words it saw.

## Declared gaps

The authoritative list is [SEAMS.md](SEAMS.md). The console-relevant entries:
the catch-up replay is a runtime-plane operation with no registry proxy, so there
is no panel for it and nothing fakes one; the A2A skill surface is gated off by
default and fail-closed, with no panel; the provenance badge's `fallback` state
needs an explicit backend signal no route carries yet, so every displayed number
honestly reads `live` or `absent`; the map's co-mention arc layer is honest-empty
until multi-country geo lands upstream; a subject cannot declare a point or
polygon geometry until the target schema admits one (#66); and routing the journal's accepted
reflections back into the analysis spine is not built, though the Journal Gate
works the queue. The reader surfaces seed honest empty regions where their
first-class views do not exist yet — the Ledger's forecasts, the Board's
coverage strip, the Subject's forecasts and series — each saying what it waits
on (`lib/surfaces.ts::SLOTS`). The consult vocabulary the server offers
(`workstationVocabulary.json`) still names the retired workspace, preset and
mission ids; the shell translates them onto surfaces, and re-cutting the
vocabulary to the surfaces is a server-side change not made here.

One console-side absence that is not a seam but is worth naming, because it is
visible on paper: the print document's citation endnotes carry a masthead — the
cited URL's own host — and the URL, but no publication date. `POST /v3/export`
resolves a cited signal to its title and `canonical_url` and nothing else, so
the endnote prints that slot as a stated absence rather than reading a date off
a headline or a URL path. An endnote with no masthead, date or URL at all (a
desk-grounding block, a sub-claim ref) says so in words.
