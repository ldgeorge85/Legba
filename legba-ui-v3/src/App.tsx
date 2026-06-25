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
} from 'dockview-react'
import { Sidebar } from '@/components/Sidebar'
import { StatusBar } from '@/components/StatusBar'
import { CommandPalette } from '@/components/CommandPalette'
import { UnboundPanelPlaceholder } from '@/components/PanelChrome'
import { PanelTierProvider } from '@/components/PanelTierContext'
import { PanelErrorBoundary } from '@/components/PanelErrorBoundary'
import { useRegistry } from '@/panel-registry/useRegistry'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { extractScope, instanceId, resolvePanel } from '@/panel-registry/loader'
import type { PanelKind, PanelRegistration } from '@/types'
import { currentMode, getToken } from '@/auth/jwt'
import {
  applyPreset,
  findPreset,
  hasCustomLayout,
  loadCustomLayout,
  saveCustomLayout,
} from '@/lib/layoutPresets'
import { applyInvestigateLayout, applyInvestigateAnalystLayout } from '@/lib/investigateLayout'
import { useSelection, type SelectionKind } from '@/state/selection'
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
  if (!registration) {
    // Singleton panel — no binding; render a synthetic registration.
    if (params?.singletonKind) {
      const kind = params.singletonKind
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
        <PanelErrorBoundary label={synthetic.id}>
          <PanelTierProvider tier={entry.definition.tier ?? 'live'}>
            <Suspense fallback={<PanelLoading />}>
              <Component registration={synthetic} scope={{}} mode={params.mode} />
            </Suspense>
          </PanelTierProvider>
        </PanelErrorBoundary>
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
    <PanelErrorBoundary label={registration.id}>
      <PanelTierProvider tier={resolved.definition.tier ?? 'live'}>
        <Suspense fallback={<PanelLoading />}>
          <Component registration={registration} scope={scope} mode={params!.mode} />
        </Suspense>
      </PanelTierProvider>
    </PanelErrorBoundary>
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
}

const COMPONENTS = { default: LegbaPanelComponent }

export function App() {
  const mode = currentMode()
  const { registrations, isLoading, isError, error } = useRegistry(mode)
  const [dockApi, setDockApi] = useState<DockviewApi | null>(null)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [savedLayout, setSavedLayout] = useState(() => hasCustomLayout(mode))
  const seededRef = useRef(false)

  const onDockReady = useCallback((ev: DockviewReadyEvent) => {
    setDockApi(ev.api)
  }, [])

  // Global Ctrl/Cmd-K toggles the command palette (v2 parity).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault()
        setPaletteOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Seed the dockview with the rebalanced daily-driver grid on first ready
  // (redesign Move 6 — "the active task gets the room").
  //
  //   +----------------------------------+------------------+
  //   |  Live feed (active task)         |   INSPECTOR      |
  //   |  ~65% width — the surface you    |   ~35% width     |
  //   |  scan; the room it deserves      |   (detail/drill) |
  //   +----------------------------------+   selection →    |
  //   |  World map (demoted strip ~28%)  |   detail         |
  //   |  + Why tabbed — brushed by sel   |                  |
  //   +----------------------------------+------------------+
  //
  // The map no longer anchors the canvas (it ate ~70% before); the live feed
  // is the anchor and the Inspector is a first-class ~35% right rail. The map
  // is demoted to a bottom strip — situational context, not the whole screen.
  // Group sizes are pinned after seeding so the split isn't a naive 50/50.
  useEffect(() => {
    if (!dockApi || seededRef.current) return
    seededRef.current = true
    if (mode !== 'personal' && mode !== 'cis') return

    // #90 feed merge — the boot anchor is the single "Live Feed"
    // (system.findings): one unified findings+signals feed with a Live on/off
    // button and a Source (All/Findings/Signals) filter (subsumed v4.feed).
    const feed = addSingleton(dockApi, 'system.findings', mode)
    const inspector = addSingleton(dockApi, 'system.inspector', mode, {
      referencePanel: 'system.findings',
      direction: 'right',
    })
    const map = addSingleton(dockApi, 'v4.map', mode, {
      referencePanel: 'system.findings',
      direction: 'below',
    })
    addSingleton(dockApi, 'v4.why', mode, {
      referencePanel: 'v4.map',
      direction: 'within',
    })

    // Pin the rebalanced proportions (Dockview defaults to ~50/50 per split):
    // Inspector ≈ 35% width (the research target for a detail pane), the map a
    // demoted ≈ 28%-tall bottom strip, leaving the feed the dominant surface.
    void feed // anchor; sized implicitly by the inspector/map weights below
    sizeWorkspace(dockApi, { inspector, map })
  }, [dockApi, mode])

  useEffect(() => {
    if (!isLoading) setLastRefresh(new Date())
  }, [isLoading, registrations])

  const onOpenPanel = useCallback(
    (kind: PanelKind, registration: PanelRegistration | null) => {
      if (!dockApi) return
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
      sizeWorkspace(dockApi, { inspector })
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
      sizeWorkspace(dockApi, { inspector })
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
          onInvestigateTarget={onInvestigateTarget}
          onInvestigateAnalyst={onInvestigateAnalyst}
          onOpenPalette={() => setPaletteOpen(true)}
          onSaveLayout={onSaveLayout}
          onRestoreLayout={onRestoreLayout}
          canRestoreLayout={savedLayout}
        />
        <main className="flex-1 min-w-0">
          <DockviewReact
            components={COMPONENTS}
            onReady={onDockReady}
            className="dockview-theme-abyss h-full"
          />
        </main>
      </div>
      <StatusBar
        mode={mode}
        panelCount={visibleRegistrations.length}
        authenticated={getToken() != null}
        lastRefresh={lastRefresh}
        errorText={errorText}
      />
      <CommandPalette
        open={paletteOpen}
        mode={mode}
        onClose={() => setPaletteOpen(false)}
        onOpen={onOpenPanel}
        onOpenBound={onOpenBoundRecord}
        onSelectRecord={onSelectRecord}
        onApplyPreset={onApplyPreset}
        onInvestigateTarget={onInvestigateTarget}
        onInvestigateAnalyst={onInvestigateAnalyst}
      />
    </div>
  )
}

function addSingleton(
  api: DockviewApi,
  kind: PanelKind,
  mode: ReturnType<typeof currentMode>,
  position?: { referencePanel: PanelKind; direction: 'right' | 'left' | 'above' | 'below' | 'within' },
) {
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
    params: { registration: null, singletonKind: kind, mode } satisfies LegbaPanelParams,
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
 * Pin the rebalanced boot proportions (redesign Move 6). Dockview splits
 * ~50/50 by default; this nudges the groups to the research-backed weights:
 * the Inspector to ~35% of the canvas width and the map to a demoted bottom
 * strip (~28% tall), leaving the live feed the dominant surface.
 *
 * Sizing is best-effort: it reads the live canvas dimensions off the root
 * element and asks each group to take a fraction. If the panels didn't seed
 * (mode gating) the calls are simply skipped — the layout still works, just at
 * Dockview's default proportions.
 */
function sizeWorkspace(
  api: DockviewApi,
  panels: {
    inspector?: ReturnType<DockviewApi['addPanel']>
    map?: ReturnType<DockviewApi['addPanel']>
  },
) {
  // Defer one frame so Dockview has laid out the groups before we resize.
  requestAnimationFrame(() => {
    const width = api.width || 1280
    const height = api.height || 800
    try {
      panels.inspector?.api.setSize({ width: Math.round(width * 0.35) })
      panels.map?.api.setSize({ height: Math.round(height * 0.28) })
    } catch {
      // setSize is best-effort; a failed resize must never break boot.
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
