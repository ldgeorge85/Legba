/**
 * L-204 — Legba UI v3 root.
 *
 * Layout:
 *  ┌────────────────────────────────────────────────────────────────┐
 *  │  Sidebar    │      Dockview workspace                          │
 *  │  (panels)   │      (per-panel tiles)                           │
 *  │             │                                                  │
 *  │             ├──────────────────────────────────────────────────│
 *  │             │      StatusBar                                   │
 *  └────────────────────────────────────────────────────────────────┘
 *
 * The Sidebar lists registered panels. Clicking one calls
 * `dockApi.addPanel`, which mounts the resolved React component via the
 * `LegbaPanelComponent` Dockview frame.
 */

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  DockviewReact,
  type DockviewReadyEvent,
  type DockviewApi,
  type IDockviewPanelProps,
  type IDockviewPanelHeaderProps,
} from 'dockview-react'
import { Sidebar } from '@/components/Sidebar'
import { StatusBar } from '@/components/StatusBar'
import { CommandPalette } from '@/components/CommandPalette'
import { UnboundPanelPlaceholder } from '@/components/PanelChrome'
import { PanelTierProvider } from '@/components/PanelTierContext'
import { PanelErrorBoundary } from '@/components/PanelErrorBoundary'
import { DockviewPanelApiProvider } from '@/components/DockviewPanelApiContext'
import { useRegistry } from '@/panel-registry/useRegistry'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { extractScope, instanceId, resolvePanel } from '@/panel-registry/loader'
import { resolveKind } from '@/panel-registry/aliases'
import type { PanelKind, PanelRegistration } from '@/types'
import { currentMode, getToken } from '@/auth/jwt'
import {
  applyPreset,
  findPreset,
  hasCustomLayout,
  loadCustomLayout,
  saveCustomLayout,
} from '@/lib/layoutPresets'
import {
  findWorkspace,
  isWorkspaceId,
  LANDING_WORKSPACE,
  loadWorkspaceLayout,
  migrateLegacyLayout,
  resetWorkspaceLayout,
  saveWorkspaceLayout,
  seedWorkspace,
  WORKSPACES,
  type WorkspaceDef,
  type WorkspaceId,
} from '@/lib/workspaces'
import { WorkspaceBar } from '@/components/WorkspaceBar'
import { applyInvestigateLayout, applyInvestigateAnalystLayout } from '@/lib/investigateLayout'
import { toggleDebugMode } from '@/lib/debugMode'
import { useSelection, type SelectionKind } from '@/state/selection'
import {
  emitRead,
  installReadTelemetryLifecycle,
  setTelemetryWorkspace,
} from '@/lib/readTelemetry'
import { readWorkspaceHash, useShareState, writeWorkspaceHash } from '@/lib/shareState'
import type { PaletteRecord } from '@/components/usePaletteRecords'

/**
 * One Dockview panel — looks up the bound `PanelRegistration` by panel id
 * and renders the resolved component.
 *
 * Dockview passes us a `params` object via `addPanel({ params })`; we
 * stash the `registration` there at open time so each tile gets its own
 * binding.
 */
function LegbaPanelComponent(props: IDockviewPanelProps<LegbaPanelParams>) {
  const params = props.params
  const registration = params?.registration

  // READ TELEMETRY (D2e) — THE panel-open chokepoint.
  //
  // Every opener in this file (`onOpenPanel` from the sidebar/palette,
  // `addSingleton` from workspace seeding and layout presets, `addBound` from
  // the investigate rails) and every restored saved layout mounts through
  // `component: 'default'`, which is this function. Dockview instantiates it
  // exactly once per `addPanel` — `dockviewMountLifecycle.probe.test.tsx`
  // pins that a tab switch does NOT remount — so one effect here counts every
  // panel open in the app and nothing else. Instrumenting the three openers
  // instead would have missed the restored-layout path entirely, which is
  // most of a normal morning.
  //
  // Computed before any early return so the hook order stays unconditional.
  const openedKind = registration
    ? (registration.layout_slot ?? registration.panel_id)
    : params?.singletonKind
      ? (resolveKind(params.singletonKind)?.kind ?? params.singletonKind)
      : null
  useEffect(() => {
    if (!openedKind) return
    emitRead('panel_open', { subjectKind: 'panel', subjectId: openedKind })
    // The operator-pull surface the premise review measured decaying
    // 64→53→18 gets its own kind, so "did giving them a worthy read revive
    // asking questions of it?" is answerable without a subject-string filter.
    if (openedKind === 'system.consult') emitRead('consult_open')
    // `lineage_walk` is NOT emitted here: opening the Provenance survivor is
    // not the same act as entering one of its walk surfaces, and the panel
    // owns that distinction. See panels/merged/Provenance.tsx.
  }, [openedKind])

  if (!registration) {
    // Singleton panel — no binding; render a synthetic registration.
    if (params?.singletonKind) {
      // A saved layout / deep-link may name a RETIRED kind: resolve it onto
      // its survivor and remember the tab that IS the retired surface
      // (panel-registry/aliases.ts, design §4.4 call site 2).
      const alias = resolveKind(params.singletonKind)
      const kind = alias?.kind ?? params.singletonKind
      const tab = params.tab ?? alias?.tab
      const entry = PANEL_REGISTRY[kind]
      if (!entry) {
        return (
          <UnboundPanelPlaceholder
            panelId={params.singletonKind}
            descriptorId="(no descriptor — singleton)"
          />
        )
      }
      const synthetic: PanelRegistration = {
        id: `singleton:${kind}`,
        panel_id: entry.definition.panelId,
        descriptor_id: '(singleton)',
        descriptor_version: '00000000',
        descriptor_family: 'target',
        analyst_id: null,
        title: entry.definition.defaultTitle,
        mode: params.mode,
        layout_slot: kind,
        data_query: {},
        binding: {},
        retired: false,
        created_at: new Date().toISOString(),
        retired_at: null,
      }
      const Component = entry.Component
      return (
        <DockviewPanelApiProvider value={props.api}>
          <PanelErrorBoundary label={synthetic.id}>
            <PanelTierProvider tier={entry.definition.tier ?? 'live'}>
              <Suspense fallback={<PanelLoading />}>
                <Component
                  registration={synthetic}
                  scope={{}}
                  mode={params.mode}
                  initialTab={tab}
                />
              </Suspense>
            </PanelTierProvider>
          </PanelErrorBoundary>
        </DockviewPanelApiProvider>
      )
    }
    return <UnboundPanelPlaceholder panelId="?" descriptorId="(no binding)" />
  }

  const resolved = resolvePanel(registration)
  if (!resolved.ok) {
    return (
      <UnboundPanelPlaceholder
        panelId={resolved.panel_id}
        descriptorId={registration.descriptor_id}
      />
    )
  }
  const Component = resolved.Component
  const scope = extractScope(registration)
  return (
    <DockviewPanelApiProvider value={props.api}>
      <PanelErrorBoundary label={registration.id}>
        <PanelTierProvider tier={resolved.definition.tier ?? 'live'}>
          <Suspense fallback={<PanelLoading />}>
            <Component
              registration={registration}
              scope={scope}
              mode={params!.mode}
              initialTab={params?.tab ?? resolved.tab}
            />
          </Suspense>
        </PanelTierProvider>
      </PanelErrorBoundary>
    </DockviewPanelApiProvider>
  )
}

function PanelLoading() {
  return (
    <div className="flex items-center justify-center h-full w-full text-slate-500 text-xs">
      Loading panel…
    </div>
  )
}

interface LegbaPanelParams {
  registration: PanelRegistration | null
  singletonKind?: PanelKind
  mode: ReturnType<typeof currentMode>
  /** Tab a tabbed survivor opens on (set when a retired kind was aliased here). */
  tab?: string
}

/**
 * The Dockview component map. EXPORTED for the read-telemetry runtime test
 * (`lib/readTelemetryRuntime.test.tsx`), which mounts a real `DockviewReact`
 * with THIS object rather than a stand-in — so if the panel-open chokepoint
 * ever stops being `LegbaPanelComponent`, the test follows the change instead
 * of quietly passing against a copy. Same reason `ANCHOR_KINDS` is exported.
 */
export const COMPONENTS = { default: LegbaPanelComponent }

/**
 * Anchor-panel tab (item 5): renders the title WITHOUT a close button, so a
 * pinned panel can't be closed out of the workspace. RETAINED but currently
 * UNUSED — see {@link ANCHOR_KINDS}. Any future kind added to that set picks
 * this tab up again with no other change.
 */
function AnchorTab(props: IDockviewPanelHeaderProps) {
  return (
    <div
      className="flex h-full select-none items-center px-3 text-[13px] whitespace-nowrap"
      title="Anchor panel — always docked"
      data-testid="anchor-tab"
    >
      <span className="truncate">{props.api.title}</span>
    </div>
  )
}

const TAB_COMPONENTS = { anchor: AnchorTab }

/**
 * Singletons pinned as non-closable anchors (item 5) — opened anywhere (boot
 * seed / sidebar / palette) they use the close-button-less {@link AnchorTab}.
 *
 * EMPTY as of the operator's call 2026-08-04. `system.findings` (Live Feed) and
 * `system.inspector` used to be pinned here; they are now ordinary closable
 * panels. Nothing is stranded by closing them — both still open by default at
 * boot (the seed is unchanged) and reopen from the sidebar or the command
 * palette — so pinning only cost the operator the ability to reclaim the space.
 *
 * The machinery is kept, not deleted: adding a kind back to this set is the
 * whole change needed to pin a future surface.
 */
export const ANCHOR_KINDS: ReadonlySet<PanelKind> = new Set<PanelKind>([])

export function App() {
  const mode = currentMode()
  const { registrations, isLoading, isError, error } = useRegistry(mode)
  const [dockApi, setDockApi] = useState<DockviewApi | null>(null)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [savedLayout, setSavedLayout] = useState(() => hasCustomLayout(mode))
  // The active stance (design §2). A shared link can carry it (`#ws=`), so the
  // initial value is read from the hash and falls back to the landing.
  const [workspace, setWorkspace] = useState<WorkspaceId>(() => {
    const fromHash = readWorkspaceHash()
    return fromHash && isWorkspaceId(fromHash) ? fromHash : LANDING_WORKSPACE
  })
  const seededRef = useRef(false)
  // The stance whose layout the dock currently holds — read by the unload
  // autosave and the switcher without re-binding either to React state.
  const workspaceRef = useRef<WorkspaceId>(workspace)
  workspaceRef.current = workspace

  // Shareable state — the selection ⇄ URL hash (addressability without a router).
  useShareState()

  // READ TELEMETRY (D2e) — the drain hooks. Queued events flush on a timer;
  // this makes sure the LAST batch of a morning is not the one that gets lost,
  // via `sendBeacon` on hide/pagehide/unload. Installed once, torn down with
  // the app. Everything inside fails silent by construction — see
  // lib/readTelemetry.ts rule 1.
  useEffect(() => installReadTelemetryLifecycle(), [])

  const onDockReady = useCallback((ev: DockviewReadyEvent) => {
    setDockApi(ev.api)
  }, [])

  // Global Ctrl/Cmd-K toggles the command palette (v2 parity); Ctrl/Cmd+Shift-D
  // toggles the developer "debug chrome" (item 5 — panel provenance stamp,
  // status-bar counters, density control), which defaults OFF.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault()
        setPaletteOpen((o) => !o)
        return
      }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'd' || e.key === 'D')) {
        e.preventDefault()
        toggleDebugMode()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Enter a workspace (UI_HOLISTIC_DESIGN_2026-08-24 §2 — the stance model).
  //
  // A workspace REMEMBERS ITSELF: if this stance has a stored layout for the
  // active mode we restore that (the operator's own arrangement), otherwise we
  // seed the curated default and pin its proportions. Nothing here clears
  // another stance's slot — the caller saves the outgoing layout first (see
  // `onSwitchWorkspace`), which is what makes a workspace an object you return
  // to rather than the reset the old `applyPreset` performed.
  const enterWorkspace = useCallback(
    (ws: WorkspaceId) => {
      if (!dockApi) return
      const def = findWorkspace(ws)
      if (!def) return
      seededRef.current = true

      // READ TELEMETRY (D2e) — the stance chokepoint. Boot, Alt+N, the
      // workspace bar, the `#ws=` deep-link and Alt+Shift+R all land here, so
      // one emit covers every way a stance can come up.
      //
      // The workspace is pushed into the telemetry module FIRST, so every
      // panel_open the seed walk is about to fire is tagged with the stance it
      // belongs to rather than the one being left.
      setTelemetryWorkspace(ws)
      emitRead('workspace_open', { workspace: ws })
      // THE HEADLINE METRIC. `brief_read` is emitted exactly here — the
      // Morning Read landing mounting — because that is the product the
      // oracle wager is about. "On how many of the last 90 days did the
      // operator open the morning read at all?" is the number that decides
      // Option 1 vs the pre-committed Option 2 fallback, and this is the only
      // line in the app that answers it.
      if (ws === LANDING_WORKSPACE) emitRead('brief_read', { workspace: ws })

      if (loadWorkspaceLayout(dockApi, ws, mode)) return
      dockApi.clear()
      seedWorkspace(def, (kind, position) => addSingleton(dockApi, kind, mode, position))
      // Default-active tabs (e.g. the World Assessment in front of the
      // Inspector on Morning Read) and the stance's pinned proportions —
      // the boot-only extras a generic seed walk doesn't know about.
      for (const kind of def.active ?? []) dockApi.getPanel(kind)?.api.setActive()
      sizeWorkspace(dockApi, def)
    },
    [dockApi, mode],
  )

  // THE LANDING (design §3.1) — first open is the Morning Read workspace,
  // seeded, in one paint: never a blank dock, never the panel tree, never a
  // tour. It answers "what happened, what moved, what does it mean?" —
  //
  //   +-------------------------------------------------------------------+
  //   |  AT A GLANCE (signals / findings / situations / sources + deltas)  |
  //   +-------------------------------------------------------------------+
  //   |  THE WALL — world at a glance · MOVERS SINCE LAST VISIT ·          |
  //   |             newest verified · health corner                       |
  //   +--------------------------------+----------------------------------+
  //   |  LIVE FEED                     |  WORLD ASSESSMENT | Alerts | Insp |
  //   |  (verified-first, facets)      +----------------------------------+
  //   |                                |  WORLD MAP                       |
  //   +--------------------------------+----------------------------------+
  //
  // This supersedes the S7-T2 mission-control boot grid. The one substantive
  // change to what mounts: the Wall replaces the standalone
  // `system.wall_movers` tile, because movers-since-last-visit is the Wall's
  // OWN quadrant and the old grid mounted it a second time beside its parent
  // (design §1.2) — U-4's "cold boot must answer what changed" acceptance is
  // kept, by the panel that owns the answer. The exact placement sequence
  // lives in `lib/workspaces.ts` so it is colocated with, and unit-tested the
  // same way as, the other five stances.
  useEffect(() => {
    if (!dockApi || seededRef.current) return
    if (mode !== 'personal' && mode !== 'cis') {
      seededRef.current = true
      return
    }
    // A pre-workspace saved layout becomes Morning Read's slot on the very
    // first boot after this train (design §6.2 rule 1) — copied, never moved,
    // so the sidebar's Save/Restore keeps its own slot untouched.
    migrateLegacyLayout(mode)
    enterWorkspace(workspaceRef.current)
  }, [dockApi, mode, enterWorkspace])

  // SWITCHING NEVER DESTROYS (design §2.5). The outgoing stance is serialized
  // into its own slot on the way out, so coming back returns the arrangement
  // you left — the single behavioural bug that made `LAYOUT_PRESETS` unusable
  // as workspaces was `applyPreset`'s `api.clear()` on the way IN.
  const onSwitchWorkspace = useCallback(
    (next: WorkspaceId) => {
      if (!dockApi || next === workspaceRef.current) return
      saveWorkspaceLayout(dockApi, workspaceRef.current, mode)
      workspaceRef.current = next
      setWorkspace(next)
      writeWorkspaceHash(next)
      enterWorkspace(next)
    },
    [dockApi, mode, enterWorkspace],
  )

  // Discard this stance's saved layout and re-seed its curated default —
  // the design's "Reset this workspace" (Alt+Shift+R). Scoped to ONE stance:
  // every other slot, and the sidebar's Save/Restore layout, are untouched.
  const onResetWorkspace = useCallback(() => {
    if (!dockApi) return
    resetWorkspaceLayout(workspaceRef.current, mode)
    enterWorkspace(workspaceRef.current)
  }, [dockApi, mode, enterWorkspace])

  // Alt+1…6 switch, Alt+` cycles, Alt+Shift+R resets (design §3.2 / §7 Q3 —
  // Ctrl+digit collides with browser tab switching on Windows/Linux and ⌘digit
  // on macOS; Alt+digit is free in both and preventDefault-able).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!e.altKey || e.ctrlKey || e.metaKey) return
      if (e.shiftKey) {
        if (e.key === 'R' || e.key === 'r') {
          e.preventDefault()
          onResetWorkspace()
        }
        return
      }
      if (e.key === '`') {
        e.preventDefault()
        const i = WORKSPACES.findIndex((w) => w.id === workspaceRef.current)
        onSwitchWorkspace(WORKSPACES[(i + 1) % WORKSPACES.length].id)
        return
      }
      const digit = Number(e.key)
      if (!Number.isInteger(digit)) return
      const target = WORKSPACES.find((w) => w.index === digit)
      if (!target) return
      e.preventDefault()
      onSwitchWorkspace(target.id)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onSwitchWorkspace, onResetWorkspace])

  // A stance that only persisted on SWITCH would lose a reload's worth of
  // arranging, so the live layout is also serialized on unload. Reset is the
  // escape hatch when an arrangement goes wrong.
  useEffect(() => {
    if (!dockApi) return
    function persist() {
      if (dockApi) saveWorkspaceLayout(dockApi, workspaceRef.current, mode)
    }
    window.addEventListener('beforeunload', persist)
    return () => window.removeEventListener('beforeunload', persist)
  }, [dockApi, mode])

  useEffect(() => {
    if (!isLoading) setLastRefresh(new Date())
  }, [isLoading, registrations])

  const onOpenPanel = useCallback(
    (requestedKind: PanelKind, registration: PanelRegistration | null) => {
      if (!dockApi) return
      // Retired kinds resolve onto their survivor + tab before anything is
      // opened, so a ⌘K deep-link or an event bridge minted before a merge
      // train still lands on a real surface (design §4.4 call site 2).
      const alias = resolveKind(requestedKind)
      const kind = alias?.kind ?? requestedKind
      if (!PANEL_REGISTRY[kind]) return
      const scope = registration ? extractScope(registration) : {}
      const id = registration ? instanceId(kind, scope) : kind
      const existing = dockApi.getPanel(id)
      if (existing) {
        // Dockview 4.x: DockviewApi.setActivePanel was removed — use
        // the panel's own api.setActive() instead.
        existing.api.setActive()
        return
      }
      const def = PANEL_REGISTRY[kind].definition
      const title = registration?.title ?? def.defaultTitle
      dockApi.addPanel({
        id,
        component: 'default',
        title,
        params: {
          registration,
          singletonKind: registration ? undefined : kind,
          mode,
          tab: alias?.tab,
        } satisfies LegbaPanelParams,
      })
    },
    [dockApi, mode],
  )

  // #90 Wave C — bridge the optimizer "view prompt-module diff" link to the
  // panel opener. Optimizer.tsx parks the candidate id in a durable window slot
  // AND fires `legba:open-optimizer-diff`; we open/focus the singleton diff
  // panel here so a first click works even when the panel isn't mounted yet
  // (the panel drains the parked slot on mount). Without this bridge the event
  // is lost when no listener exists yet — the original "doesn't open" bug.
  useEffect(() => {
    function onOpenDiff() {
      onOpenPanel('system.optimizer.diff', null)
    }
    window.addEventListener('legba:open-optimizer-diff', onOpenDiff)
    return () => window.removeEventListener('legba:open-optimizer-diff', onOpenDiff)
  }, [onOpenPanel])

  // Apply a named layout preset — clears the workspace and re-seeds it
  // through the same singleton opener the boot grid uses, so preset panels
  // are gated by mode identically and indistinguishable from hand-opened
  // ones. After seeding, the boot effect must not re-run.
  const onApplyPreset = useCallback(
    (presetId: string) => {
      if (!dockApi) return
      const preset = findPreset(presetId)
      if (!preset) return
      seededRef.current = true
      applyPreset(dockApi, preset, (kind, position) => addSingleton(dockApi, kind, mode, position))
    },
    [dockApi, mode],
  )

  // Open the target-scoped analysis grid (Overview/Map/Findings/Timeline) for a
  // chosen target. Surfaces the binding-scoped analysis panels that no preset or
  // boot grid reaches today (the Map et al.). See lib/investigateLayout.ts.
  const onInvestigateTarget = useCallback(
    (targetId: string) => {
      if (!dockApi) return
      seededRef.current = true
      applyInvestigateLayout(dockApi, targetId, (kind, position) =>
        addBound(dockApi, kind, targetId, mode, position),
      )
      // Move 6: the Inspector is a first-class right rail while investigating —
      // dock it right of the Findings anchor and pin it to ~35% so detail/drill
      // gets real estate (not a corner tab).
      const inspector = addSingleton(dockApi, 'system.inspector', mode, {
        referencePanel: `target.findings:${targetId}` as PanelKind,
        direction: 'right',
      })
      sizeInspectorRail(dockApi, inspector)
    },
    [dockApi, mode],
  )

  // Analyst analogue of onInvestigateTarget (redesign Move 3b — closes P-B2):
  // open the per-analyst grid (Outputs/Runs/Critiques/Cross-target) bound to a
  // chosen analyst. Same addBound machinery, keyed on analyst_id.
  const onInvestigateAnalyst = useCallback(
    (analystId: string) => {
      if (!dockApi) return
      seededRef.current = true
      applyInvestigateAnalystLayout(dockApi, analystId, (kind, position) =>
        addBound(dockApi, kind, analystId, mode, position, 'analyst'),
      )
      // Move 6: dock the Inspector as the ~35% right rail (mirror of the target
      // investigate grid) so analyst-output detail/drill gets the room.
      const inspector = addSingleton(dockApi, 'system.inspector', mode, {
        referencePanel: `analyst.outputs:${analystId}` as PanelKind,
        direction: 'right',
      })
      sizeInspectorRail(dockApi, inspector)
    },
    [dockApi, mode],
  )

  // Palette: open a record's bound primary panel (target→Findings, analyst→
  // Outputs). Sources have no binding-scoped panel, so the palette routes them
  // through onSelectRecord instead (handled in CommandPalette).
  const onOpenBoundRecord = useCallback(
    (kind: PanelKind, recordKind: PaletteRecord['recordKind'], id: string) => {
      if (!dockApi) return
      if (recordKind === 'source') {
        // No source-bound analysis panel; drop the source into the Inspector.
        useSelection.getState().select({ kind: 'source', id, origin: 'palette' })
        return
      }
      addBound(dockApi, kind, id, mode, undefined, recordKind === 'analyst' ? 'analyst' : 'target')
    },
    [dockApi, mode],
  )

  // Palette: select a record into the unified store → the Inspector renders it.
  const onSelectRecord = useCallback((recordKind: SelectionKind, id: string, label: string) => {
    useSelection.getState().select({ kind: recordKind, id, label, origin: 'palette' })
  }, [])

  // Save the live (hand-dragged) layout for this mode to localStorage.
  const onSaveLayout = useCallback(() => {
    if (!dockApi) return
    saveCustomLayout(dockApi, mode)
    setSavedLayout(true)
  }, [dockApi, mode])

  // Restore the previously-saved layout for this mode, if any.
  const onRestoreLayout = useCallback(() => {
    if (!dockApi) return
    seededRef.current = true
    loadCustomLayout(dockApi, mode)
  }, [dockApi, mode])

  const visibleRegistrations = useMemo(() => {
    return registrations.filter((r) => !r.retired)
  }, [registrations])

  const errorText = isError ? `registry load failed: ${error?.message ?? 'unknown'}` : null

  return (
    <div className="h-full w-full flex flex-col bg-surf-base">
      <div className="flex-1 flex min-h-0">
        <Sidebar
          registrations={visibleRegistrations}
          onOpen={onOpenPanel}
          onApplyPreset={onApplyPreset}
          onOpenPalette={() => setPaletteOpen(true)}
          onSaveLayout={onSaveLayout}
          onRestoreLayout={onRestoreLayout}
          canRestoreLayout={savedLayout}
        />
        {/* The workspace column: the stance bar over the dock. The sidebar to
            its left is unchanged — the bar is additive chrome, not a re-shell. */}
        <main className="flex-1 min-w-0 flex flex-col">
          <WorkspaceBar
            active={workspace}
            onSwitch={onSwitchWorkspace}
            onReset={onResetWorkspace}
          />
          <div className="min-h-0 flex-1">
            <DockviewReact
              components={COMPONENTS}
              tabComponents={TAB_COMPONENTS}
              onReady={onDockReady}
              className="dockview-theme-abyss h-full"
            />
          </div>
        </main>
      </div>
      <StatusBar
        mode={mode}
        panelCount={visibleRegistrations.length}
        authenticated={getToken() != null}
        lastRefresh={lastRefresh}
        errorText={errorText}
        onOpenExport={() => onOpenPanel('system.report_export', null)}
      />
      <CommandPalette
        open={paletteOpen}
        mode={mode}
        onClose={() => setPaletteOpen(false)}
        onOpen={onOpenPanel}
        onOpenBound={onOpenBoundRecord}
        onSelectRecord={onSelectRecord}
        onApplyPreset={onApplyPreset}
        onSwitchWorkspace={onSwitchWorkspace}
        onInvestigateTarget={onInvestigateTarget}
        onInvestigateAnalyst={onInvestigateAnalyst}
      />
    </div>
  )
}

function addSingleton(
  api: DockviewApi,
  requestedKind: PanelKind,
  mode: ReturnType<typeof currentMode>,
  position?: { referencePanel: PanelKind; direction: 'right' | 'left' | 'above' | 'below' | 'within' },
) {
  // Same alias resolution as `onOpenPanel` — the sidebar, ⌘K, workspace seeds
  // and layout presets all reach a panel through one of these two openers, so
  // resolving in both is resolving everywhere.
  const alias = resolveKind(requestedKind)
  const kind = alias?.kind ?? requestedKind
  const def = PANEL_REGISTRY[kind]?.definition
  if (!def) return undefined
  if (def.modes.length > 0 && !def.modes.includes(mode)) return undefined
  const existing = api.getPanel(kind)
  if (existing) {
    // Dockview 4.x: setActivePanel moved to the panel's api.
    existing.api.setActive()
    return existing
  }
  return api.addPanel({
    id: kind,
    component: 'default',
    title: def.defaultTitle,
    // Anchor singletons (Live Feed / Inspector) get the close-button-less tab.
    ...(ANCHOR_KINDS.has(kind) ? { tabComponent: 'anchor' } : {}),
    params: {
      registration: null,
      singletonKind: kind,
      mode,
      tab: alias?.tab,
    } satisfies LegbaPanelParams,
    // When position is supplied, Dockview splits the workspace relative
    // to the reference panel; otherwise the new panel joins the active
    // group as a tab.  The boot seed uses positions to materialise a
    // proper grid; sidebar opens stay tab-add (default behaviour).
    ...(position
      ? {
          position: {
            referencePanel: position.referencePanel,
            direction: position.direction,
          },
        }
      : {}),
  })
}

/**
 * Pin a workspace's proportions after its seed (the successor of S7-T2's
 * hardcoded `sizeMissionControl`). Dockview splits ~50/50 by default, so a
 * stance's drawn weights — a thin glance strip, a ~300px Wall band, a ~34%
 * report rail — have to be re-applied once, one frame after seeding.
 *
 * The hints are data on the workspace definition (`lib/workspaces.ts`), not
 * arithmetic in the shell, so a stance's shape is reviewable in one place.
 * Sizing is best-effort: a failed resize must never break the landing.
 */
function sizeWorkspace(api: DockviewApi, def: WorkspaceDef) {
  if (!def.sizes || def.sizes.length === 0) return
  // Defer one frame so Dockview has laid out the groups before we resize.
  requestAnimationFrame(() => {
    const width = api.width || 1280
    for (const hint of def.sizes ?? []) {
      const panel = api.getPanel(hint.kind)
      if (!panel) continue
      try {
        if (hint.height != null) panel.api.setSize({ height: hint.height })
        if (hint.widthFraction != null) {
          panel.api.setSize({ width: Math.round(width * hint.widthFraction) })
        }
      } catch {
        // setSize is best-effort; a failed resize must never break the seed.
      }
    }
  })
}

/**
 * Pin the Inspector to a ~35% right rail after an Investigate grid seeds — the
 * detail/drill pane the grid brushes into.
 */
function sizeInspectorRail(api: DockviewApi, inspector?: ReturnType<DockviewApi['addPanel']>) {
  requestAnimationFrame(() => {
    const width = api.width || 1280
    try {
      inspector?.api.setSize({ width: Math.round(width * 0.35) })
    } catch {
      // best-effort
    }
  })
}

/**
 * Open a binding-scoped panel against a synthesized per-record registration.
 *
 * Mirrors `addSingleton` but for `requiresBinding` panels: it mints a
 * `PanelRegistration` carrying the binding (`target_id` or `analyst_id` — the
 * shape the runtime registry would otherwise supply) so the analysis panels
 * render scoped to the record with no backend `ui_panel_registrations` row. The
 * panel id matches `instanceId(kind, scope)` (`<kind>:<record_id>`) so a later
 * sidebar open of the same panel dedups onto this tile.
 *
 * `axis` selects the binding key from the panel kind's category so the same
 * helper drives both the target grid and the analyst grid (Move 3b).
 */
function addBound(
  api: DockviewApi,
  kind: PanelKind,
  recordId: string,
  mode: ReturnType<typeof currentMode>,
  position?: { referencePanel: string; direction: 'right' | 'left' | 'above' | 'below' | 'within' },
  axis: 'target' | 'analyst' = 'target',
) {
  const def = PANEL_REGISTRY[kind]?.definition
  if (!def) return
  if (def.modes.length > 0 && !def.modes.includes(mode)) return
  const id = `${kind}:${recordId}`
  const existing = api.getPanel(id)
  if (existing) {
    existing.api.setActive()
    return
  }
  const binding = axis === 'analyst' ? { analyst_id: recordId } : { target_id: recordId }
  const registration: PanelRegistration = {
    id,
    panel_id: def.panelId,
    descriptor_id: recordId,
    descriptor_version: '00000000',
    descriptor_family: axis,
    analyst_id: axis === 'analyst' ? recordId : null,
    title: `${def.defaultTitle} · ${recordId}`,
    mode,
    layout_slot: kind,
    data_query: {},
    binding,
    retired: false,
    created_at: new Date().toISOString(),
    retired_at: null,
  }
  api.addPanel({
    id,
    component: 'default',
    title: registration.title,
    params: { registration, mode } satisfies LegbaPanelParams,
    ...(position
      ? { position: { referencePanel: position.referencePanel, direction: position.direction } }
      : {}),
  })
}
