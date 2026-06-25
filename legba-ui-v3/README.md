# legba-ui-v3 — Lewis's daily-driver intelligence workstation

Replaces `legba-ui/` (27-panel legacy set, retired). Fresh build per the L-204
brief and the L-092 design (`plans/design/legba_ui_panels_v2.md`). Descriptor-
driven: panel instances materialize from the L-192 `ui_panel_registrations`
table via the `GET /api/v1/registry/ui_panels?mode=<mode>` REST surface and
the existing `/api/v1/registry/events` WebSocket.

## Stack

| Layer        | Choice                 | Notes |
|--------------|------------------------|-------|
| Build        | Vite 6                 | same as legacy |
| Language     | TypeScript ~5.7        | strict + noUnused* |
| UI runtime   | React 18.3             | StrictMode |
| Workspace    | Dockview 4.3           | dark `abyss` theme |
| Server state | @tanstack/react-query 5 | per-panel fetches |
| Styling      | Tailwind 3.4           | dark default, `surface-*` palette |
| Testing      | Vitest 2 + @testing-library/react | jsdom env |

## Quick start

```sh
cd legba-ui-v3
npm install
# point at a running legba-registry (default 8501); override:
#   VITE_API_BASE=http://localhost:8501 npm run dev
npm run dev      # http://localhost:5174
npm run build    # tsc -b && vite build → dist/
npm test         # Vitest one-shot
npm run test:watch
```

The dev server proxies `/api/*` and `/ws/*` to `VITE_API_BASE`. The default
expects a `legba-registry` console-script running on localhost:8501.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  ┌────────────┐   ┌──────────────────────────────────────────────┐  │
│  │  Sidebar   │   │  Dockview workspace (per-panel tiles)        │  │
│  │  groups    │   │   ┌────────────┐ ┌──────────────────────┐    │  │
│  │ by         │   │   │ Target     │ │ Findings             │    │  │
│  │ - target   │   │   │ Overview   │ │ (per-target)         │    │  │
│  │ - analyst  │   │   └────────────┘ └──────────────────────┘    │  │
│  │ - dashbrd  │   │                                              │  │
│  └────────────┘   └──────────────────────────────────────────────┘  │
│                   ┌──────────────────────────────────────────────┐  │
│                   │  StatusBar  (mode + panel count + auth)      │  │
│                   └──────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

Two registries cooperate (L-108 §1):

1. **Bundle-time registry** — `src/panel-registry/registry.ts`. Static map
   from `PanelKind` to React component + metadata. Adding a new panel kind
   = adding a row here + a `panels/...tsx` file.
2. **Runtime descriptor registry** — `src/panel-registry/useRegistry.ts`.
   Reactive React hook that fetches `PanelRegistration` rows from the
   backend and re-fetches on `registry.bindings.*` NATS events.

The panel-loader at `src/panel-registry/loader.ts` walks
`panel_id` → `PanelKind` and returns either a render bundle or an
`UnboundPanelPlaceholder` marker.

## Panels shipped (vs. deferred)

Implemented with real data binding (8 core, per L-204 priority):

| ID  | Kind                       | Source |
|-----|----------------------------|--------|
| O-Consult | `system.consult`     | Daily-driver: POSTs to A2A `consult_on_demand` skill (L-178) |
| D1  | `system.targets.roster`    | `GET /api/v1/registry/descriptors?family=target` |
| T1  | `target.overview`          | `GET /api/v3/targets/{id}/state` (gracefully 404-tolerant) |
| T3  | `target.findings`          | `GET /api/v3/substrate/findings?target_id=...` |
| T4  | `target.situations`        | `GET /api/v3/substrate/situations?target_id=...` |
| A1  | `analyst.runs`             | `GET /api/v3/analysts/{id}/runs` |
| S4  | `system.optimizer`         | `GET /api/v3/optimizer/candidates?state=pending` (L-176) |
| S6  | `system.runtime`           | `GET /api/v3/runtime/actors` (5s polling) |
| A5  | `analyst.critiques`        | `GET /api/v3/analysts/{id}/critiques` (L-175) |
| D3  | `dashboard.dynamic`        | Schema-driven shell (widget catalog stub) |

Deferred (registered + descriptor-loadable, body is `_DeferredStub`):

```
T2  target.signals     T7  target.map          A2  analyst.outputs
T5  target.hypotheses  T8  target.graph        A3  analyst.cross_target
T6  target.sources     T9  target.timeline     A4  analyst.forecasts
T10 target.claims      D2  system.pulse        S1  system.lineage
S2  system.budget      S3  system.eval         S5  system.dead_letter
S7  system.streams     S8  system.users        O1  registry.targets
O2  registry.analysts  O3  registry.stack      O4  registry.wirings
O5  registry.mutations
```

Each deferred panel renders chrome + a "spec: …" pointer + the registration
shape, so the registry → loader → render pipeline is end-to-end exercised
even before the body lands.

## Mode handling

`src/auth/jwt.ts::currentMode()` resolves the active mode with priority:

1. `?mode=` URL query (operator override for testing)
2. JWT `mode` claim (when a real bearer is set)
3. `VITE_LEGBA_DEFAULT_MODE` build-time env
4. `'personal'` (fallback)

Each panel kind declares its `modes:` array in the static registry. The
default landing layout (`App.tsx::addSingleton`) only adds singletons that
the current mode allows. Above-AI mode strips all Legba panels (the bundle
is small + the registry returns no rows).

## Talking to the backend

The frontend talks to one HTTP base:

- `/api/v1/registry/*` — descriptor + stack + dlq + audit + vocab + **ui_panels**
- `/api/v1/registry/events` (WebSocket) — registry events with subject filter
- `/api/v3/...` — substrate read endpoints (some land with later L-tasks)
- `/a2a/skills/consult_on_demand` — daily-driver consult tool (legba-runtime)

The bearer token lives in `localStorage.legba_token`. In dev mode the
registry accepts any (or no) token.

## Adding a new panel kind

1. Pick a `PanelKind` slug.
2. Drop a component at `src/panels/<dir>/<Name>.tsx` (props: `PanelProps`).
3. Register it in `src/panel-registry/registry.ts`.
4. Add unit coverage in `src/panel-registry/loader.test.ts`.
5. Ship a backend descriptor declaring `outputs: [{ kind: ui_panel, config: { panel: panels.<id>, mode: ..., layout_slot: ... } }]`.

No descriptor → no panel instance, even if the kind is bundled. That's
load-bearing (L-108 §1).

## Test layout

- `**/*.test.ts(x)` — Vitest unit + component tests under `jsdom`.
- Backend route tests: `tests/data_pkg/test_registry_ui_panels_route.py`.

Test counts after this commit:
- `panel-registry/loader.test.ts` — 8 cases
- `panel-registry/registry.test.ts` — 7 cases
- `auth/jwt.test.ts` — 7 cases
- `panels/system/Consult.test.tsx` — 4 cases (incl. error path)
- `panels/target/Overview.test.tsx` — 2 cases
- `panels/system/TargetsRoster.test.tsx` — 1 case

Backend integration: 6 cases in `test_registry_ui_panels_route.py`.

## What's needed before driving it in anger

1. Run a `legba-registry` instance: `legba-registry serve` (default 8501).
2. Register at least one target descriptor with `outputs.ui_panel` entries
   declaring `panels.target_overview` + friends (see L-092 §3.1 templates).
3. Optionally set `LEGBA_REGISTRY_API_TOKEN` and store the same token in
   `localStorage.legba_token` in the browser dev console.
4. `npm run dev` — open `http://localhost:5174/`. Default landing tiles:
   Target Roster + Global Pulse + Runtime Health + Consult.

The Consult panel works against the running `legba-runtime` A2A skill
router (post-L-002a). If `legba-runtime` isn't up, the panel surfaces the
error inline and preserves the question.
