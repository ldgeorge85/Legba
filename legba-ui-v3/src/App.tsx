/**
 * Legba UI v3 root — THE FRAME (U-6; 02_UI_TARGET_BRIEF §12, which replaced
 * the U-2 shell's fixed Consult column after the owner saw it live).
 *
 *  ┌──────────────────────────────── top bar ──────────────────────────────────┐
 *  │ Legba │ Board Subject Ledger Watch │ [subject ▾] │ ⌘K ＋ ↶ ↺ 🔗 │ Operator ▸ │
 *  ├──────┬──────────────────────────────┬──────────────────────────────────────┤
 *  │ rail │  STAGE (today's read; what   │  PANELS — Dockview, 1–3 columns by    │
 *  │ subj │  was pulled up; breadcrumb)  │  width (`lib/frameGrid.ts`), the map  │
 *  │ hist │ ═══════ divider (drag) ══════│  leading; resizable, swappable,       │
 *  │ view │  CHAT — Consult, ask box at  │  closable, re-openable from ＋.        │
 *  │      │  its foot (move · collapse)  │  The Record sheet slides over here.   │
 *  ├──────┴──────────────────────────────┴──────────────────────────────────────┤
 *  │ time band (reader side)                                                     │
 *  │ status line — sources n/N firing · gaps · judge · grader                    │
 *  └─────────────────────────────────────────────────────────────────────────────┘
 *
 * Every page is this frame; a page declares its stage, its panels and whether
 * the chat starts open (`lib/surfaces.ts`). Consult is back in the layout: the
 * divider resizes it, its header collapses it to the ask bar, pops it to the
 * full centre or moves it to a column on the left or the right
 * (`components/ConsultDock.tsx`, remembered per page in `state/frame.ts`).
 *
 * ONE EXECUTOR. Every producer — the bar, ⌘K, the ＋ catalog, the rail, the
 * URL, the panels' cross-links and Consult's cases — compiles to the drive
 * actions (`lib/applyAction.ts`) or to a panel action (`lib/panelActions.ts`),
 * and both land in ONE opener here ({@link openViewAt}): the stage for a
 * page-sized view, else the view's home region, replacing that region's
 * previous opened view — never a new column. The frame reports what is on
 * screen (`lib/onScreen.ts`) through the same port.
 */

import { Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  DockviewReact,
  type DockviewReadyEvent,
  type DockviewApi,
  type IDockviewPanel,
  type IDockviewPanelProps,
  type IDockviewHeaderActionsProps,
  type SerializedDockview,
} from 'dockview-react'
import { ExternalLink, PanelTop, PanelLeftOpen } from 'lucide-react'
import { StatusBar } from '@/components/StatusBar'
import { CommandPalette } from '@/components/CommandPalette'
import { SurfaceBar } from '@/components/SurfaceBar'
import { ConsultDock } from '@/components/ConsultDock'
import { RecordSheet } from '@/components/RecordSheet'
import { FrameRail } from '@/components/FrameRail'
import { Stage } from '@/components/Stage'
import { Splitter } from '@/components/Splitter'
import { ViewHost } from '@/components/ViewHost'
import { PanelLoading } from '@/components/SingletonView'
import { SingletonView } from '@/components/SingletonView'
import { PortalContainerProvider } from '@/ui/portal'
import { legbaDark } from '@/ui/dockviewTheme'
import { UnboundPanelPlaceholder } from '@/components/PanelChrome'
import { PanelTierProvider } from '@/components/PanelTierContext'
import { PanelErrorBoundary } from '@/components/PanelErrorBoundary'
import { DockviewPanelApiProvider } from '@/components/DockviewPanelApiContext'
import { QueryRegion, TileQueryRegion } from '@/lib/queryLayer'
import TimeScrubber from '@/v4/world/TimeScrubber'
import { useRegistry } from '@/panel-registry/useRegistry'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { extractScope, resolvePanel } from '@/panel-registry/loader'
import { resolveKind, rewriteSerializedLayout } from '@/panel-registry/aliases'
import { contractOf } from '@/panel-registry/contractOf'
import { frameViewComponent, viewIdOf } from '@/panel-registry/contracts'
import { drive, registerShell } from '@/lib/applyAction'
import { boundPinFor, usePinScope } from '@/lib/consultContext'
import { requestPanelTab } from '@/state/panelParams'
import { goToSurface, openPanel, registerShellCommands } from '@/lib/commands'
import { actionOf, parseAddress } from '@/lib/address'
import { openCaseAddress } from '@/lib/caseReplay'
import { useSubjectIndex, humanizeId } from '@/lib/deskNames'
import { boundTileTitle } from '@/lib/subjectTitle'
import { boundRegistration } from '@/lib/boundRegistration'
import { usePreferences, type Density } from '@/components/density/PreferencesProvider'
import type { PanelKind, PanelRegistration } from '@/types'
import { currentMode, getToken } from '@/auth/jwt'
import { OPERATOR_SIDE_REASON, useBelowContributor } from '@/auth/roles'
import {
  LANDING_SURFACE,
  MAX_REGIONS,
  SURFACES,
  findSurface,
  frameViewTitle,
  homeOf,
  isFrameView,
  isSlotId,
  isMountedViewId,
  layoutCensus,
  readSurfaceLayout,
  resetSurfaceLayout,
  resolveSurfaceId,
  saveSurfaceLayout,
  surfaceViews,
  type RegionId,
  type SurfaceDef,
  type SurfaceId,
  type ViewId,
} from '@/lib/surfaces'
import {
  TRANSIENT_PARAM,
  dropWouldExceedRegions,
  openInRegion,
  seedSurface,
  sizeSurface,
  type ViewPosition,
} from '@/lib/surfaceDock'
import { columnsFor, frameGrid, rootPxFor, ASK_BAR_REM, CHAT_MAX_SHARE, PANEL_COLUMN_MIN_REM } from '@/lib/frameGrid'
import { registerFramePort, pushViewUndo, undoView, useViewUndo, type ViewOnScreen } from '@/lib/panelActions'
import { legacyPresetSurface } from '@/lib/layoutPresets'
import { legacyMission } from '@/lib/missions'
import { toggleDebugMode } from '@/lib/debugMode'
import { useSelection } from '@/state/selection'
import { emitRead, installReadTelemetryLifecycle, setTelemetryWorkspace } from '@/lib/readTelemetry'
import { useShareState, writeMissionHash, writeWorkspaceHash } from '@/lib/shareState'
import { installDeskScopeBridge } from '@/lib/scopeFromReport'
import { useScope } from '@/state/scope'
import { useWorldState } from '@/v4/world/worldState'
import { isCasePlaying, useCasePlayer } from '@/state/casePlayer'
import {
  currentStage,
  stageInstanceId,
  useFrame,
  type ChatPlacement,
  type PageFrame,
  type StageEntry,
  type StageState,
} from '@/state/frame'
import { useActiveSurface } from '@/state/activeSurface'
// The map's contract wired to the globe's actions (U-7), kept while the map is hidden.
import '@/v4/globe/contract'
import '@/v4/watch/register'

type Mode = ReturnType<typeof currentMode>

const INSPECTOR_KIND = 'system.inspector'
const CONSULT_KIND = 'system.consult'
/** Where the last subject the reader looked at is kept, so Subject never opens empty. */
export const LAST_SUBJECT_KEY = 'legba_last_subject'

interface LegbaPanelParams {
  registration: PanelRegistration | null
  singletonKind?: PanelKind
  mode: Mode
  /** Tab a tabbed survivor opens on (set when a retired kind was aliased here). */
  tab?: string
  /** The region's ONE opened view (the next open there replaces it). */
  transient?: boolean
}

function SuspenseBoundary({ children }: { children: ReactNode }) {
  return <Suspense fallback={<PanelLoading />}>{children}</Suspense>
}

/**
 * One Dockview panel — a singleton through `SingletonView`, or a bound read
 * against its synthesized registration.
 */
function LegbaPanelComponent(props: IDockviewPanelProps<LegbaPanelParams>) {
  // U-5: a tile's reads wait until the tile is the visible one in its group
  // (`lib/queryLayer.ts`) — Dockview keeps a hidden tab mounted.
  return (
    <DockviewPanelApiProvider value={props.api}>
      <TileQueryRegion>
        <LegbaPanelBody {...props} />
      </TileQueryRegion>
    </DockviewPanelApiProvider>
  )
}

function LegbaPanelBody(props: IDockviewPanelProps<LegbaPanelParams>) {
  const params = props.params
  const registration = params?.registration

  // READ TELEMETRY (D2e) — the dock's panel-open chokepoint: every view a page
  // seeds, restores or opens in a region mounts through `component:
  // 'default'`, which is this function. (The stage's views count themselves
  // in `ViewHost`; Consult's open is emitted where the frame mounts it.)
  const openedKind = registration
    ? (registration.layout_slot ?? registration.panel_id)
    : params?.singletonKind
      ? (resolveKind(params.singletonKind)?.kind ?? params.singletonKind)
      : null
  useEffect(() => {
    if (!openedKind) return
    emitRead('panel_open', { subjectKind: 'panel', subjectId: openedKind })
  }, [openedKind])

  // THE PIN HOOK on a bound view (UI-B6): a per-subject or per-unit read pins
  // Consult to the record it is bound to while it is in front.
  const boundPin = boundPinFor(registration?.binding, registration?.descriptor_id ?? null)
  const [frontRev, setFrontRev] = useState(0)
  useEffect(() => {
    if (!boundPin || typeof props.api?.onDidActiveChange !== 'function') return
    const d = props.api.onDidActiveChange((e) => {
      if (e.isActive) setFrontRev((r) => r + 1)
    })
    return () => d.dispose()
  }, [props.api, !!boundPin]) // eslint-disable-line react-hooks/exhaustive-deps
  usePinScope(boundPin, 'tile', frontRev)

  if (!registration) {
    if (params?.singletonKind) {
      return <SingletonView kind={params.singletonKind} mode={params.mode} tab={params.tab} api={props.api} />
    }
    return <UnboundPanelPlaceholder panelId="?" descriptorId="(no binding)" />
  }

  const resolved = resolvePanel(registration)
  if (!resolved.ok) {
    return <UnboundPanelPlaceholder panelId={resolved.panel_id} descriptorId={registration.descriptor_id} />
  }
  const Component = resolved.Component
  const scope = extractScope(registration)
  return (
    <DockviewPanelApiProvider value={props.api}>
      <PanelErrorBoundary label={registration.id}>
        <PanelTierProvider tier={resolved.definition.tier ?? 'live'}>
          <SuspenseBoundary>
            <Component
              registration={registration}
              scope={scope}
              mode={params!.mode}
              initialTab={params?.tab ?? resolved.tab}
            />
          </SuspenseBoundary>
        </PanelTierProvider>
      </PanelErrorBoundary>
    </DockviewPanelApiProvider>
  )
}

/**
 * A region view that is not a panel kind: a mounted view (the forecast
 * ledger), a slot (a view that does not exist yet) or a view registered with
 * its own component (`panel-registry/contracts.ts::registerPanel`).
 */
function SlotPanel(props: IDockviewPanelProps<{ slot: string; recordId?: string }>) {
  const mode = currentMode()
  return (
    <DockviewPanelApiProvider value={props.api}>
      <TileQueryRegion>
        <ViewHost view={props.params.slot} recordId={props.params.recordId} mode={mode} instanceId={props.api.id} pinSurface="tile" />
      </TileQueryRegion>
    </DockviewPanelApiProvider>
  )
}

/**
 * The Dockview component map. EXPORTED for the read-telemetry runtime test,
 * which mounts a real `DockviewReact` with THIS object.
 */
export const COMPONENTS = { default: LegbaPanelComponent, slot: SlotPanel }

/**
 * A region's header actions: SEND TO THE STAGE (the view in front becomes the
 * stage's), and POP OUT (a floating group over the panels) or DOCK BACK.
 */
function RegionActions(props: IDockviewHeaderActionsProps) {
  const floating = props.api.location.type === 'floating'
  return (
    <div className="flex h-full items-center gap-0.5 pr-1">
      <button
        type="button"
        data-testid="region-to-stage"
        title="Show this on the stage"
        aria-label="Show this on the stage"
        onClick={() => {
          const p = props.group.activePanel
          if (p) window.dispatchEvent(new CustomEvent('legba:frame-to-stage', { detail: p.id }))
        }}
        className="flex h-6 w-6 items-center justify-center rounded-control text-ink-3 hover:bg-surf-3 hover:text-ink-1"
      >
        <PanelLeftOpen size={13} aria-hidden />
      </button>
      <button
        type="button"
        data-testid={floating ? 'region-dock' : 'region-popout'}
        title={floating ? 'Dock this region back' : 'Pop this region out over the panels'}
        aria-label={floating ? 'Dock this region back' : 'Pop this region out'}
        onClick={() => {
          try {
            if (floating) {
              const target = props.containerApi.groups.find((g) => g.api.location.type === 'grid')
              if (target) props.api.moveTo({ group: target, position: 'center' })
            } else {
              props.containerApi.addFloatingGroup(props.group, { width: 640, height: 440 })
            }
          } catch {
            // a refused move leaves the region where it was
          }
        }}
        className="flex h-6 w-6 items-center justify-center rounded-control text-ink-3 hover:bg-surf-3 hover:text-ink-1"
      >
        {floating ? <PanelTop size={13} aria-hidden /> : <ExternalLink size={13} aria-hidden />}
      </button>
    </div>
  )
}

/** Every opener resolves retired ids through the alias table. */
function addSingleton(
  api: DockviewApi,
  requestedKind: PanelKind,
  mode: Mode,
  position?: ViewPosition,
  tab?: string,
  opts: { seeded?: boolean; inactive?: boolean } = {},
): IDockviewPanel | undefined {
  const alias = resolveKind(requestedKind)
  const kind = alias?.kind ?? requestedKind
  const def = PANEL_REGISTRY[kind]?.definition
  if (!def) return undefined
  if (def.modes.length > 0 && !def.modes.includes(mode)) return undefined
  const existing = api.getPanel(kind)
  if (existing) {
    existing.api.setActive()
    const wantTab = tab ?? alias?.tab
    if (wantTab) requestPanelTab(kind, wantTab)
    return existing
  }
  if (position && !api.getPanel(position.referencePanel)) position = undefined
  return api.addPanel({
    id: kind,
    component: 'default',
    title: contractOf(kind)?.name ?? def.defaultTitle,
    params: {
      registration: null,
      singletonKind: kind,
      mode,
      tab: tab ?? alias?.tab,
      ...(opts.seeded ? {} : { [TRANSIENT_PARAM]: true }),
    } satisfies LegbaPanelParams,
    ...(opts.inactive ? { inactive: true } : {}),
    ...(position ? { position: { referencePanel: position.referencePanel, direction: position.direction } } : {}),
  })
}

/** The title a non-kind view carries (a slot, the forecast ledger, a registered view). */
function frameTitle(view: string): string {
  if (isSlotId(view) || isMountedViewId(view)) return frameViewTitle(view)
  return contractOf(view)?.name ?? view
}

/** A slot, a mounted view, or a registered frame view, in the dock. */
function addSlot(
  api: DockviewApi,
  view: string,
  position?: ViewPosition,
  inactive?: boolean,
  opts: { seeded?: boolean; recordId?: string } = {},
) {
  const id = opts.recordId ? `${view}:${opts.recordId}` : view
  const existing = api.getPanel(id)
  if (existing) return existing
  if (position && !api.getPanel(position.referencePanel)) position = undefined
  return api.addPanel({
    id,
    component: 'slot',
    title: frameTitle(view),
    params: { slot: view, ...(opts.recordId ? { recordId: opts.recordId } : {}), ...(opts.seeded ? {} : { [TRANSIENT_PARAM]: true }) },
    ...(inactive ? { inactive: true } : {}),
    ...(position ? { position: { referencePanel: position.referencePanel, direction: position.direction } } : {}),
  })
}

/** A bound read (`<kind>:<record>`) against a synthesized registration. */
function addBound(
  api: DockviewApi,
  kind: PanelKind,
  recordId: string,
  mode: Mode,
  axis: 'target' | 'analyst',
  position: ViewPosition | undefined,
  tab?: string,
): IDockviewPanel | undefined {
  const alias = resolveKind(kind)
  if (alias) kind = alias.kind
  const wantTab = tab ?? alias?.tab
  const def = PANEL_REGISTRY[kind]?.definition
  if (!def) return undefined
  if (def.modes.length > 0 && !def.modes.includes(mode)) return undefined
  const id = `${kind}:${recordId}`
  const existing = api.getPanel(id)
  if (existing) {
    existing.api.setActive()
    if (wantTab) requestPanelTab(id, wantTab)
    return existing
  }
  const registration = boundRegistration(kind, recordId, mode, axis)!
  return api.addPanel({
    id,
    component: 'default',
    title: registration.title,
    params: {
      registration,
      mode,
      [TRANSIENT_PARAM]: true,
      ...(wantTab ? { tab: wantTab } : {}),
    } satisfies LegbaPanelParams,
    ...(position ? { position: { referencePanel: position.referencePanel, direction: position.direction } } : {}),
  })
}

function readLastSubject(): string | null {
  try {
    return localStorage.getItem(LAST_SUBJECT_KEY)
  } catch {
    return null
  }
}

/** The stage entry a page opens on. */
function defaultStageEntry(def: SurfaceDef): StageEntry {
  return { view: def.stage, title: contractOf(def.stage)?.name ?? frameTitle(def.stage), origin: 'page' }
}

/** The viewport's width, live. */
function useViewportWidth(): number {
  const [w, setW] = useState(() => (typeof window === 'undefined' ? 1680 : window.innerWidth || 1680))
  useEffect(() => {
    const on = () => setW(window.innerWidth || 1680)
    window.addEventListener('resize', on)
    return () => window.removeEventListener('resize', on)
  }, [])
  return w
}

/** An element's height, live. */
function useHeight<T extends HTMLElement>(): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null)
  const [h, setH] = useState(0)
  useLayoutEffect(() => {
    if (!el) return
    setH(el.getBoundingClientRect().height)
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setH(el.getBoundingClientRect().height))
    ro.observe(el)
    return () => ro.disconnect()
  }, [el])
  return [setEl, h]
}

/**
 * The FRAME SNAPSHOT an undo or a case's Restore puts back: the panel side's
 * Dockview arrangement (its own keys, so a reader of `panels` / `grid` sees
 * the dock as before) plus the page, the stage and the chat.
 */
type FrameSnapshot = Partial<SerializedDockview> & {
  __frame: 1
  __surface: SurfaceId
  __stage: StageState | null
  __page: PageFrame
}

function isFrameSnapshot(v: unknown): v is FrameSnapshot {
  return !!v && typeof v === 'object' && (v as { __frame?: unknown }).__frame === 1
}

function dockOf(s: FrameSnapshot): SerializedDockview | null {
  const { __frame: _f, __surface: _s, __stage: _st, __page: _p, ...dock } = s
  return dock.grid && dock.panels ? (dock as SerializedDockview) : null
}

export function App() {
  const mode = currentMode()
  const { registrations, isLoading, isError, error } = useRegistry(mode)
  // UI-B2: read the subject roster once, so every surface names a subject by
  // what it DECLARES (lib/deskNames.ts), never by parsing its id.
  useSubjectIndex()
  const [dockApi, setDockApi] = useState<DockviewApi | null>(null)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  // The overlay host every `src/ui/` primitive portals into (`ui/portal.tsx`).
  const [portalHost, setPortalHost] = useState<HTMLElement | null>(null)
  const prefs = usePreferences()
  // THE BOOT ADDRESS — a saved view is a link (`lib/address.ts`).
  const bootAddress = useMemo(
    () => parseAddress(window.location.hash, window.location.search),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  )
  // RA-1: a reader (below contributor) stays on the reader side — the
  // operator sections are the engine's own panels and the contributors' work
  // queues, every read of which the registry refuses a reader.
  const readerOnly = useBelowContributor()
  const readerOnlyRef = useRef(readerOnly)
  readerOnlyRef.current = readerOnly
  const [surface, setSurface] = useState<SurfaceId>(() => {
    const allowed = (id: SurfaceId | null): SurfaceId | null =>
      id && readerOnly && findSurface(id)?.side === 'operator' ? LANDING_SURFACE : id
    const layout = actionOf(bootAddress.actions, 'arrange')?.args.layout
    const fromWs = typeof layout === 'string' ? resolveSurfaceId(layout) : null
    if (fromWs) return allowed(fromWs) ?? LANDING_SURFACE
    const mission = actionOf(bootAddress.actions, 'run_mission')?.args.name
    const fromMission = typeof mission === 'string' ? resolveSurfaceId(mission) : null
    return allowed(fromMission) ?? LANDING_SURFACE
  })
  const surfaceRef = useRef<SurfaceId>(surface)
  surfaceRef.current = surface
  // The views that dress by surface (the map: a globe on the Board, an inset on Subject).
  useEffect(() => useActiveSurface.getState().setSurface(surface), [surface])
  const seededRef = useRef(false)
  /** True while the shell itself is laying the dock down (closes then are not the reader's). */
  const layingRef = useRef(false)
  /** Seeded views the reader closed on the current page. */
  const closedRef = useRef<Set<string>>(new Set())
  const def = findSurface(surface)
  const side = def?.side ?? 'reader'

  // THE FRAME'S OWN STATE — the chat, per page; the rail.
  const hydrate = useFrame((s) => s.hydrate)
  useState(() => {
    hydrate()
    return null
  })
  const pageFrame = useFrame((s) => s.pages[surface]) ?? useFrame.getState().page(surface, def?.chat)
  const railPref = useFrame((s) => s.railOpen)
  const vw = useViewportWidth()
  const grid = frameGrid(vw, railPref ?? undefined)
  const railOpen = railPref ?? vw >= 1600
  const rootPx = rootPxFor(vw)
  const setPage = useCallback(
    (patch: Partial<PageFrame>) => useFrame.getState().setPage(surfaceRef.current, patch, findSurface(surfaceRef.current)?.chat),
    [],
  )

  // Shareable state — selection, scope and window ⇄ the URL hash.
  useShareState()
  // SCOPE ≠ FOCUS: deliberate subject selections are mirrored into the scope.
  useEffect(() => installDeskScopeBridge(), [])
  // READ TELEMETRY (D2e) — the drain hooks.
  useEffect(() => installReadTelemetryLifecycle(), [])

  const onDockReady = useCallback((ev: DockviewReadyEvent) => {
    setDockApi(ev.api)
  }, [])

  // THE REGION BUDGET: a drop that would split off a region past the panel
  // side's ceiling is refused; joining a region (a tab) and popping out are not.
  useEffect(() => {
    if (!dockApi) return
    const d = dockApi.onWillShowOverlay((e) => {
      if (dropWouldExceedRegions(dockApi, e.kind, e.position)) e.preventDefault()
    })
    return () => d.dispose()
  }, [dockApi])

  // A seeded view the READER closes stays closed in the stored arrangement
  // (and is one ＋ away); any other missing view resets the page.
  useEffect(() => {
    if (!dockApi) return
    const d = dockApi.onDidRemovePanel((p) => {
      if (layingRef.current) return
      const sdef = findSurface(surfaceRef.current)
      if (sdef && surfaceViews(sdef).includes(p.id as ViewId)) closedRef.current.add(p.id)
    })
    const a = dockApi.onDidAddPanel((p) => {
      closedRef.current.delete(p.id)
    })
    return () => {
      d.dispose()
      a.dispose()
    }
  }, [dockApi])

  /** The view opener the seed shares with every open. */
  const openView = useCallback(
    (
      api: DockviewApi,
      view: ViewId,
      position: ViewPosition | undefined,
      opts: { seeded: boolean; inactive?: boolean },
    ): IDockviewPanel | undefined =>
      isFrameView(view) || frameViewComponent(view)
        ? addSlot(api, view, position, opts.inactive, { seeded: opts.seeded })
        : addSingleton(api, view as PanelKind, mode, position, undefined, opts),
    [mode],
  )

  /** How many panel columns the panel side affords right now. */
  const columnsNow = useCallback((api: DockviewApi) => {
    const w = api.width || Math.max(0, (window.innerWidth || 1680) - frameGrid(window.innerWidth || 1680).railPx - frameGrid(window.innerWidth || 1680).centrePx)
    return columnsFor(w, rootPxFor(window.innerWidth || 1680))
  }, [])

  const seed = useCallback(
    (api: DockviewApi, sdef: SurfaceDef) => {
      layingRef.current = true
      try {
        const cols = columnsNow(api)
        seedSurface(api, sdef, (view, position, opts) => openView(api, view, position, opts), cols)
        requestAnimationFrame(() => sizeSurface(api, sdef, columnsNow(api)))
      } finally {
        layingRef.current = false
      }
      closedRef.current = new Set()
    },
    [openView, columnsNow],
  )

  /** Lay a page down: its stage on its default; its panels stored when still valid, else default. */
  const enterSurface = useCallback(
    (next: SurfaceId) => {
      if (!dockApi) return
      const sdef = findSurface(next)
      if (!sdef) return
      seededRef.current = true
      setTelemetryWorkspace(next)
      emitRead('workspace_open', { workspace: next })
      if (next === LANDING_SURFACE) emitRead('brief_read', { workspace: next })
      const st = useFrame.getState().stages[next]
      if (!st || st.stack.length === 0) useFrame.getState().seedStage(next, defaultStageEntry(sdef))
      const stored = readSurfaceLayout(next, mode)
      if (stored.ok) {
        layingRef.current = true
        try {
          dockApi.fromJSON(stored.layout)
          closedRef.current = new Set(stored.closed)
          return
        } catch {
          resetSurfaceLayout(next, mode)
          setNotice(`${sdef.label}'s stored arrangement could not be laid out — back to its default`)
        } finally {
          layingRef.current = false
        }
      } else if (stored.reason !== 'none') {
        setNotice(stored.note)
      }
      seed(dockApi, sdef)
    },
    [dockApi, mode, seed],
  )

  const saveCurrent = useCallback(() => {
    if (!dockApi || !seededRef.current) return
    try {
      saveSurfaceLayout(surfaceRef.current, mode, dockApi.toJSON(), [...closedRef.current])
    } catch {
      // a half-torn dock may not serialize; losing one autosave is fine
    }
  }, [dockApi, mode])

  /** Go to a page: the outgoing arrangement is stored first (switching never destroys). */
  const switchSurface = useCallback(
    (next: SurfaceId) => {
      if (!dockApi) return
      if (readerOnlyRef.current && findSurface(next)?.side === 'operator') {
        // never laid down: its panels would only ask and be refused
        setNotice(OPERATOR_SIDE_REASON)
        return
      }
      if (next === surfaceRef.current && seededRef.current) return
      if (seededRef.current) saveCurrent()
      surfaceRef.current = next
      useActiveSurface.getState().setSurface(next)
      setSurface(next)
      setNotice(null)
      writeWorkspaceHash(next)
      writeMissionHash(null)
      enterSurface(next)
    },
    [dockApi, enterSurface, saveCurrent],
  )

  // THE LANDING — the boot page, restored or seeded, in one paint.
  useEffect(() => {
    if (!dockApi || seededRef.current) return
    if (mode !== 'personal' && mode !== 'cis') {
      seededRef.current = true
      return
    }
    enterSurface(surfaceRef.current)
    // Consult is open on every page: its open is the landing's, tagged with
    // the page the reader landed on.
    emitRead('panel_open', { subjectKind: 'panel', subjectId: CONSULT_KIND })
    emitRead('consult_open')
    // A link's window (`#win=`) and a retired `#mission=` apply once, after
    // the page is down, through the one executor.
    const extra = bootAddress.actions.filter((a) => a.action === 'timeline' || a.action === 'run_mission')
    if (extra.length > 0) drive(extra, { origin: 'share-link' })
  }, [dockApi, mode, enterSurface, bootAddress])

  // Reset: discard this page's stored arrangement and frame, lay its default down.
  const onReset = useCallback(() => {
    if (!dockApi) return
    resetSurfaceLayout(surfaceRef.current, mode)
    useFrame.getState().resetPage(surfaceRef.current)
    const sdef = findSurface(surfaceRef.current)
    if (sdef) {
      useFrame.getState().seedStage(sdef.id, defaultStageEntry(sdef))
      seed(dockApi, sdef)
    }
    setNotice(`${sdef?.label ?? 'This page'} is back to its default`)
  }, [dockApi, mode, seed])

  // A saved view is a link: the address bar already holds it; copy it.
  const onCopyLink = useCallback(() => {
    writeWorkspaceHash(surfaceRef.current)
    const href = window.location.href
    const done = (ok: boolean) =>
      setNotice(ok ? 'Link to this view copied' : 'The address bar holds the link to this view')
    try {
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(href).then(
          () => done(true),
          () => done(false),
        )
      } else done(false)
    } catch {
      done(false)
    }
  }, [])

  // Persist the live arrangement on unload, and on every layout change (a
  // drag of a splitter is remembered without waiting for a page switch).
  useEffect(() => {
    if (!dockApi) return
    window.addEventListener('beforeunload', saveCurrent)
    let t: ReturnType<typeof setTimeout> | null = null
    const d = dockApi.onDidLayoutChange(() => {
      if (layingRef.current || !seededRef.current) return
      if (t) clearTimeout(t)
      t = setTimeout(saveCurrent, 300)
    })
    return () => {
      window.removeEventListener('beforeunload', saveCurrent)
      d.dispose()
      if (t) clearTimeout(t)
    }
  }, [dockApi, saveCurrent])

  useEffect(() => {
    if (!isLoading) setLastRefresh(new Date())
  }, [isLoading, registrations])

  // THE RECORD SHEET follows the selection: a record selected by the reader
  // opens it; a subject is the POINT (not a record), and a record Consult or
  // the stage put on the stage is read there, not in the sheet.
  useEffect(() => {
    // A link that names a record (`#rec=`) is opened by the sheet's own
    // effect, which runs BEFORE this one (a child's effects run first): its
    // selection landed before this subscription existed and was never seen,
    // so a shared Record link opened nothing (roll 87, found behind the live
    // walk's Board-row fallback). Open the sheet for it here.
    if (useSelection.getState().selection?.origin === 'link') setSheetOpen(true)
    return useSelection.subscribe((s, prev) => {
      if (s.selection === prev.selection || !s.selection) return
      if (s.selection.kind === 'target') {
        try {
          localStorage.setItem(LAST_SUBJECT_KEY, s.selection.id)
        } catch {
          // per-viewer convenience
        }
        return
      }
      if (s.selection.origin === 'stage' || s.selection.origin === 'case') return
      setSheetOpen(true)
    })
  }, [])

  // SUBJECT NEVER OPENS EMPTY (brief §2): standing on Subject with no subject
  // in view, the last one the reader looked at becomes the point.
  useEffect(() => {
    if (surface !== 'subject') return
    const sel = useSelection.getState().selection
    const scope = useScope.getState().scope
    if (sel?.kind === 'target' || scope?.kind === 'target') return
    const last = readLastSubject()
    if (last) useSelection.getState().select({ kind: 'target', id: last, origin: 'shell' })
  }, [surface])

  // ── THE ONE OPENER ─────────────────────────────────────────────────────
  /** Where an instance is on screen: `stage`, a dock id, or null. */
  const findOpen = useCallback(
    (id: string): string | null => {
      const cur = currentStage(useFrame.getState().stages[surfaceRef.current])
      if (cur && (stageInstanceId(cur) === id || (!cur.recordId && cur.view === id))) return 'stage'
      if (!dockApi) return null
      if (dockApi.getPanel(id)) return id
      const bound = dockApi.panels.find((p) => viewIdOf(p.id) === id && p.id !== id)
      return bound ? bound.id : null
    },
    [dockApi],
  )

  const caseOrigin = () => (isCasePlaying(useCasePlayer.getState().player) ? 'case' : 'operator')

  /**
   * Open a view where it belongs: an instance already on screen comes to the
   * front (on its tab); a page-sized view lands on the stage (with a crumb
   * back); anything else lands in its home region, replacing that region's
   * previous opened view. Returns the panel / true when shown.
   */
  const openViewAt = useCallback(
    (
      requested: string,
      opts: { place?: 'stage' | 'panel'; recordId?: string; axis?: 'target' | 'analyst'; tab?: string; origin?: string } = {},
    ): unknown => {
      if (!dockApi) return undefined
      const alias = resolveKind(requested)
      const view = (alias?.kind ?? requested) as string
      const tab = opts.tab ?? alias?.tab
      if (view === CONSULT_KIND) {
        const pf = useFrame.getState().page(surfaceRef.current, findSurface(surfaceRef.current)?.chat)
        if (pf.collapsed) setPage({ collapsed: false })
        return true
      }
      if (view === INSPECTOR_KIND && opts.place !== 'stage') {
        setSheetOpen(true)
        return true
      }
      const kindDef = PANEL_REGISTRY[view as PanelKind]?.definition
      const frameView = isFrameView(view) || !!frameViewComponent(view)
      if (!kindDef && !frameView) return undefined
      if (kindDef && kindDef.modes.length > 0 && !kindDef.modes.includes(mode)) return undefined
      const instance = opts.recordId ? `${view}:${opts.recordId}` : view
      const home = homeOf(view, surfaceRef.current)
      // RA-1: an operator view never opens for a reader (on any page): its
      // reads are the engine's and the registry refuses them a reader.
      if (readerOnlyRef.current && findSurface(home.surface)?.side === 'operator') {
        setNotice(OPERATOR_SIDE_REASON)
        return undefined
      }
      if (home.surface !== surfaceRef.current) switchSurface(home.surface)
      const here = surfaceRef.current
      const sdef = findSurface(here)!
      const origin = opts.origin ?? caseOrigin()

      // Already on screen: bring it forward, on the asked tab.
      const live = opts.place ? null : findOpen(instance)
      if (live === 'stage') {
        if (tab) requestPanelTab(instance, tab)
        return true
      }
      if (live) {
        const p = dockApi.getPanel(live)
        p?.api.setActive()
        if (tab) requestPanelTab(live, tab)
        return p
      }
      const toStage = opts.place ? opts.place === 'stage' : home.place === 'stage'
      if (toStage) {
        const title = opts.recordId
          ? kindDef
            ? boundTileTitle(view as PanelKind, kindDef.defaultTitle, opts.recordId, opts.axis ?? 'target')
            : `${frameTitle(view)} · ${opts.recordId}`
          : (contractOf(view)?.name ?? frameTitle(view))
        // The same view somewhere in the dock leaves it (one place per view).
        const docked = dockApi.getPanel(instance)
        if (docked) docked.api.close()
        // A crumb already in the trail is gone back to (and asked for its tab);
        // a new one mounts on its tab.
        const known = (useFrame.getState().stages[here]?.stack ?? []).some((e) => stageInstanceId(e) === instance)
        useFrame.getState().pushStage(here, {
          view,
          recordId: opts.recordId,
          axis: opts.axis,
          tab,
          title,
          origin,
        })
        if (tab && known) requestPanelTab(instance, tab)
        return true
      }
      const region: RegionId = home.place === 'stage' ? sdef.openRegion : home.place
      const add = (position: ViewPosition | undefined) =>
        opts.recordId && kindDef
          ? addBound(dockApi, view as PanelKind, opts.recordId, mode, opts.axis ?? 'target', position, tab)
          : frameView
            ? addSlot(dockApi, view, position, false, { recordId: opts.recordId })
            : addSingleton(dockApi, view as PanelKind, mode, position, tab, { seeded: home.seeded })
      // The stage's current entry is this view: it leaves the stage.
      const st = useFrame.getState().stages[here]
      const cur = currentStage(st)
      if (cur && stageInstanceId(cur) === instance && st && st.index > 0) useFrame.getState().goStage(here, st.index - 1)
      return openInRegion(dockApi, sdef, region, add, instance)
    },
    [dockApi, mode, switchSurface, findOpen, setPage],
  )

  // A region's "show on the stage" button.
  useEffect(() => {
    const on = (e: Event) => {
      const id = String((e as CustomEvent).detail ?? '')
      if (!id || !dockApi) return
      const p = dockApi.getPanel(id)
      const params = (p?.params ?? {}) as { slot?: string; recordId?: string; registration?: PanelRegistration | null; singletonKind?: string }
      const view = params.slot ?? params.singletonKind ?? params.registration?.layout_slot ?? viewIdOf(id)
      const recordId = params.recordId ?? (params.registration ? params.registration.descriptor_id : undefined)
      const axis = params.registration?.descriptor_family === 'analyst' ? 'analyst' : 'target'
      pushViewUndo(`show ${p?.title ?? view} on the stage`)
      openViewAt(view, { place: 'stage', recordId, axis, origin: 'operator' })
    }
    window.addEventListener('legba:frame-to-stage', on)
    return () => window.removeEventListener('legba:frame-to-stage', on)
  }, [dockApi, openViewAt])

  // ── THE SNAPSHOT (undo, a case's Restore) ─────────────────────────────
  const snapshot = useCallback((): FrameSnapshot => {
    let dock: SerializedDockview | null = null
    try {
      dock = dockApi ? dockApi.toJSON() : null
    } catch {
      dock = null
    }
    const s = surfaceRef.current
    const st = useFrame.getState().stages[s]
    return {
      ...(dock ?? {}),
      __frame: 1,
      __surface: s,
      __stage: st ? { stack: st.stack.slice(), index: st.index } : null,
      __page: { ...useFrame.getState().page(s, findSurface(s)?.chat) },
    }
  }, [dockApi])

  const restore = useCallback(
    (snap: FrameSnapshot) => {
      if (!dockApi) return
      const target = snap.__surface
      if (target !== surfaceRef.current && findSurface(target)) {
        surfaceRef.current = target
        setSurface(target)
        writeWorkspaceHash(target)
        enterSurface(target)
      }
      if (snap.__stage) useFrame.getState().setStage(target, snap.__stage)
      useFrame.getState().setPage(target, snap.__page, findSurface(target)?.chat)
      const dock = dockOf(snap)
      if (!dock) return
      try {
        const rewrite = rewriteSerializedLayout(dock)
        if (layoutCensus(rewrite.layout).regions <= MAX_REGIONS) {
          layingRef.current = true
          dockApi.fromJSON(rewrite.layout)
        }
      } catch {
        // a layout Dockview refuses leaves the panels as they are
      } finally {
        layingRef.current = false
      }
    },
    [dockApi, enterSurface],
  )

  // Keys: Ctrl/Cmd-K the palette, Ctrl/Cmd-I the Record sheet, Ctrl/Cmd+Shift-D
  // debug chrome, Ctrl/Cmd-Z undo a view change (outside a text box), Escape
  // closes the sheet.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault()
        setPaletteOpen((o) => !o)
        return
      }
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && (e.key === 'i' || e.key === 'I')) {
        e.preventDefault()
        setSheetOpen((o) => !o)
        return
      }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'd' || e.key === 'D')) {
        e.preventDefault()
        toggleDebugMode()
        return
      }
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && (e.key === 'z' || e.key === 'Z')) {
        const t = e.target as HTMLElement | null
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
        const label = undoView()
        if (label) {
          e.preventDefault()
          setNotice(`Undone: ${label}`)
        }
        return
      }
      if (e.key === 'Escape' && !paletteOpen) setSheetOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [paletteOpen])

  // Alt+1…8 switch pages (1–4 reader, 5–8 operator), Alt+` cycles within the
  // side, Alt+Shift+R resets; Alt+←/→ walk the focus trail, Alt+Shift+←/→ the
  // scope trail.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!e.altKey || e.ctrlKey || e.metaKey) return
      if (e.shiftKey) {
        if (e.key === 'R' || e.key === 'r') {
          e.preventDefault()
          onReset()
        }
        if (e.key === 'ArrowLeft') {
          e.preventDefault()
          useScope.getState().back()
        }
        if (e.key === 'ArrowRight') {
          e.preventDefault()
          useScope.getState().forward()
        }
        return
      }
      if (e.key === '`') {
        e.preventDefault()
        const sd = findSurface(surfaceRef.current)?.side ?? 'reader'
        const list = SURFACES.filter((s) => s.side === sd)
        const i = list.findIndex((s) => s.id === surfaceRef.current)
        drive(goToSurface(list[(i + 1) % list.length].id))
        return
      }
      if (e.key === 'ArrowLeft') {
        e.preventDefault()
        useSelection.getState().back()
        return
      }
      if (e.key === 'ArrowRight') {
        e.preventDefault()
        useSelection.getState().forward()
        return
      }
      const digit = Number(e.key)
      if (!Number.isInteger(digit)) return
      const target = SURFACES.find((s) => s.key === digit)
      if (!target) return
      e.preventDefault()
      drive(goToSurface(target.id))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onReset])

  // THE ONE EXECUTOR'S SHELL PORT. Every drive action reaches the frame
  // through exactly these calls (`lib/applyAction.ts`).
  useEffect(() => {
    if (!dockApi) return
    return registerShell({
      mode,
      openSingleton: (kind, tab) => openViewAt(kind, { tab }),
      openBound: (kind, recordId, axis, tab) => {
        seededRef.current = true
        return openViewAt(kind, { recordId, axis, tab })
      },
      showRecord: (ref) => {
        useFrame.getState().pushStage(surfaceRef.current, {
          view: INSPECTOR_KIND,
          record: { kind: ref.kind, id: ref.id, label: ref.label },
          title: ref.label ?? (ref.kind === 'target' ? humanizeId(ref.id) : `${ref.kind} record`),
          origin: 'case',
        })
      },
      switchStance: (id) => {
        const s = resolveSurfaceId(id)
        if (s) switchSurface(s)
      },
      applyPreset: (id) => {
        const s = legacyPresetSurface(id)
        if (s) switchSurface(s)
        if (id === 'focus') setSheetOpen(true)
      },
      runMission: (id) => {
        const m = legacyMission(id)
        const s = resolveSurfaceId(id)
        if (s) switchSurface(s)
        if (m) useWorldState.getState().setSpan(m.timeWindow.spanMs)
      },
      // Consult is in the frame; an arrangement never takes it away. A case
      // that moves the view keeps the conversation (and its STOP) on screen.
      reopenConsult: () => {
        const pf = useFrame.getState().page(surfaceRef.current, findSurface(surfaceRef.current)?.chat)
        if (pf.collapsed) setPage({ collapsed: false })
      },
      currentStance: () => surfaceRef.current,
      serializeLayout: () => snapshot(),
      // Undo of a case (D2): back onto the page it started from, with the
      // panels, stage and chat it had — without storing the case's arrangement.
      restoreLayout: (stance, layout) => {
        if (isFrameSnapshot(layout)) {
          restore(layout)
          return
        }
        const s = stance ? resolveSurfaceId(stance) : null
        if (s && s !== surfaceRef.current) {
          surfaceRef.current = s
          setSurface(s)
          writeWorkspaceHash(s)
          enterSurface(s)
        }
        if (!layout) return
        try {
          const rewrite = rewriteSerializedLayout(layout as SerializedDockview)
          if (!rewrite.empty && layoutCensus(rewrite.layout).regions <= MAX_REGIONS) dockApi.fromJSON(rewrite.layout)
        } catch {
          // a layout Dockview refuses leaves the page as the case left it
        }
      },
    })
  }, [dockApi, mode, switchSurface, enterSurface, openViewAt, snapshot, restore, setPage])

  // THE FRAME PORT — the panel contract's router and the on-screen report.
  useEffect(() => {
    if (!dockApi) return
    return registerFramePort({
      surface: () => surfaceRef.current,
      openView: (viewId, o) => !!openViewAt(viewId, o),
      closeView: (instanceId) => {
        const st = useFrame.getState().stages[surfaceRef.current]
        const cur = currentStage(st)
        if (cur && stageInstanceId(cur) === instanceId) {
          // Off the stage = back one crumb; the page's own read never closes.
          if (!st || st.index === 0) return false
          useFrame.getState().goStage(surfaceRef.current, st.index - 1)
          return true
        }
        const p = dockApi.getPanel(instanceId)
        if (!p) return false
        p.api.close()
        return true
      },
      focusView: (instanceId) => {
        const cur = currentStage(useFrame.getState().stages[surfaceRef.current])
        if (cur && stageInstanceId(cur) === instanceId) return true
        const p = dockApi.getPanel(instanceId)
        if (!p) return false
        p.api.setActive()
        return true
      },
      // The instance id where it is on screen: the stage's, or the dock's.
      findOpen: (id) => {
        const where = findOpen(id)
        if (where !== 'stage') return where
        const cur = currentStage(useFrame.getState().stages[surfaceRef.current])
        return cur ? stageInstanceId(cur) : null
      },
      views: () => {
        const out: ViewOnScreen[] = []
        const cur = currentStage(useFrame.getState().stages[surfaceRef.current])
        if (cur) out.push({ id: stageInstanceId(cur), at: 'stage', front: true })
        const groups = [...dockApi.groups]
          .filter((g) => g.api.location.type === 'grid')
          .sort((a, b) => {
            const ra = a.element.getBoundingClientRect()
            const rb = b.element.getBoundingClientRect()
            return ra.left - rb.left || ra.top - rb.top
          })
        groups.forEach((g, i) => {
          for (const p of g.panels) out.push({ id: p.id, at: `p${i + 1}`, front: g.activePanel?.id === p.id })
        })
        return out
      },
      chat: () => {
        const pf = useFrame.getState().page(surfaceRef.current, findSurface(surfaceRef.current)?.chat)
        return { placement: pf.placement, collapsed: pf.collapsed }
      },
      snapshot: () => snapshot(),
      restore: (s) => {
        if (isFrameSnapshot(s)) restore(s)
      },
    })
  }, [dockApi, openViewAt, findOpen, snapshot, restore])

  // THE SHELL COMMAND FAMILY (`lib/commands.ts`) — operator-only, never recorded.
  useEffect(() => {
    const densities: Density[] = ['tight', 'cozy', 'comfortable']
    return registerShellCommands({
      'view.reset': onReset,
      'view.link': onCopyLink,
      'palette.search': () => setPaletteOpen(true),
      'tile.close': () => {
        const active = dockApi?.activePanel
        if (active) active.api.close()
      },
      'density.cycle': () =>
        prefs.setDensity(densities[(densities.indexOf(prefs.density) + 1) % densities.length]),
    })
  }, [dockApi, onReset, onCopyLink, prefs])

  // `#case=<session>:<turn>` — replay a recorded case on boot.
  const caseBootRef = useRef(false)
  useEffect(() => {
    if (!dockApi || caseBootRef.current || !bootAddress.case) return
    caseBootRef.current = true
    void openCaseAddress(bootAddress.case).then((r) => {
      if (!r.ok) setNotice(`case link — ${r.reason}`)
    })
  }, [dockApi, bootAddress])

  // ── THE CENTRE'S GEOMETRY ─────────────────────────────────────────────
  const [centreRef, centreH] = useHeight<HTMLDivElement>()
  const centreBox = useRef<HTMLDivElement | null>(null)
  const sideBox = useRef<HTMLDivElement | null>(null)
  const [dragShare, setDragShare] = useState<number | null>(null)
  const [dragCentre, setDragCentre] = useState<number | null>(null)
  const [dragSide, setDragSide] = useState<number | null>(null)
  const placement: ChatPlacement = pageFrame.placement
  const collapsed = pageFrame.collapsed && placement === 'bottom'
  const askBarPx = ASK_BAR_REM * rootPx
  const sideColPx = (dragSide ?? pageFrame.sideRem) * rootPx
  const minPanelsPx = Math.min(PANEL_COLUMN_MIN_REM, 20) * rootPx
  const roomForCentre = vw - grid.railPx - (placement === 'left' || placement === 'right' ? sideColPx : 0) - minPanelsPx
  const centreWanted = (dragCentre ?? pageFrame.centreRem ?? grid.centrePx / rootPx) * rootPx
  const centrePx = Math.round(Math.max(24 * rootPx, Math.min(centreWanted, roomForCentre)))
  const share = dragShare ?? pageFrame.share
  const chatPx = collapsed
    ? askBarPx
    : Math.round(Math.min(CHAT_MAX_SHARE * (centreH || 800), Math.max(askBarPx, share * (centreH || 800))))

  const onDividerMove = useCallback(
    (clientY: number) => {
      const el = centreBox.current
      if (!el) return
      const r = el.getBoundingClientRect()
      if (r.height <= 0) return
      const px = r.bottom - clientY
      setDragShare(Math.min(CHAT_MAX_SHARE, Math.max(0, px / r.height)))
    },
    [],
  )
  const onDividerEnd = useCallback(() => {
    setDragShare((s) => {
      if (s == null) return null
      const h = centreBox.current?.getBoundingClientRect().height ?? 800
      if (s * h <= askBarPx + 12) setPage({ collapsed: true })
      else setPage({ collapsed: false, share: s })
      return null
    })
  }, [askBarPx, setPage])
  const onDividerKey = useCallback(
    (step: number) => {
      if (step === -Infinity) return setPage({ collapsed: false, share: CHAT_MAX_SHARE })
      if (step === Infinity) return setPage({ collapsed: true })
      const s = collapsed ? askBarPx / (centreH || 800) : pageFrame.share
      setPage({ collapsed: false, share: Math.min(CHAT_MAX_SHARE, Math.max(askBarPx / (centreH || 800), s - step * 0.04)) })
    },
    [setPage, collapsed, askBarPx, centreH, pageFrame.share],
  )
  const onCentreEdgeMove = useCallback(
    (clientX: number) => {
      const el = centreBox.current
      if (!el) return
      const left = el.getBoundingClientRect().left
      setDragCentre(Math.max(24, (clientX - left) / rootPx))
    },
    [rootPx],
  )
  const onCentreEdgeEnd = useCallback(() => {
    setDragCentre((c) => {
      if (c != null) setPage({ centreRem: Math.round(Math.min(c, roomForCentre / rootPx) * 10) / 10 })
      return null
    })
  }, [setPage, roomForCentre, rootPx])
  const onSideMove = useCallback(
    (clientX: number) => {
      const el = sideBox.current
      if (!el) return
      const r = el.getBoundingClientRect()
      const px = placement === 'left' ? clientX - r.left : r.right - clientX
      setDragSide(Math.min(80, Math.max(20, px / rootPx)))
    },
    [placement, rootPx],
  )
  const onSideEnd = useCallback(() => {
    setDragSide((s) => {
      if (s != null) setPage({ sideRem: Math.round(s * 10) / 10 })
      return null
    })
  }, [setPage])

  // The dock re-lays its columns when the centre changes size.
  useEffect(() => {
    if (!dockApi) return
    const id = requestAnimationFrame(() => {
      try {
        const el = sideBox.current?.parentElement?.querySelector('[data-testid="panel-side"]') as HTMLElement | null
        const r = el?.getBoundingClientRect()
        if (r && r.width > 0) dockApi.layout(r.width, r.height)
      } catch {
        // layout is best-effort
      }
    })
    return () => cancelAnimationFrame(id)
  }, [dockApi, centrePx, placement, sideColPx, railOpen, vw])

  const moveChat = useCallback(
    (p: ChatPlacement) => {
      pushViewUndo('move Consult')
      setPage({ placement: p, collapsed: false })
    },
    [setPage],
  )
  const collapseChat = useCallback((c: boolean) => setPage({ collapsed: c }), [setPage])

  const undoTop = useViewUndo((s) => s.stack[s.stack.length - 1]?.label ?? null)
  const onUndo = useCallback(() => {
    const label = undoView()
    if (label) setNotice(`Undone: ${label}`)
  }, [])

  const errorText = isError ? `registry load failed: ${error?.message ?? 'unknown'}` : null
  const dock = (
    <QueryRegion id="consult" active>
      <ConsultDock mode={mode} placement={placement} collapsed={pageFrame.collapsed} onCollapse={collapseChat} onMove={moveChat} />
    </QueryRegion>
  )

  return (
    <PortalContainerProvider container={portalHost}>
      <div
        className="h-full w-full flex flex-col bg-surf-base"
        data-testid="frame"
        data-surface={surface}
        data-side={side}
        data-chat={placement}
        style={
          {
            '--rail-open': `${15}rem`,
            '--rail-shut': `${3}rem`,
          } as React.CSSProperties
        }
      >
        <SurfaceBar
          mode={mode}
          active={surface}
          onSwitch={(id) => drive(goToSurface(id))}
          onReset={onReset}
          onCopyLink={onCopyLink}
          onOpenPalette={() => setPaletteOpen(true)}
          onUndo={onUndo}
          undoLabel={undoTop}
          notice={notice}
        />
        <div className="flex-1 flex min-h-0">
          {/* U-5: a folded rail reads nothing */}
          <QueryRegion id="rail" active={railOpen}>
            <FrameRail
              mode={mode}
              side={side}
              open={railOpen}
              onToggle={(o) => useFrame.getState().setRail(o)}
              pageLabel={def?.label ?? ''}
              onOpenPalette={() => setPaletteOpen(true)}
            />
          </QueryRegion>
          <main className="relative flex-1 min-w-0 flex" data-testid="surface-main">
            {placement === 'left' && (
              <>
                <div ref={sideBox} className="h-full min-h-0 shrink-0" style={{ width: sideColPx }} data-testid="consult-side-left">
                  {dock}
                </div>
                <Splitter orientation="vertical" label="Consult's width" testId="consult-side-splitter" onMove={onSideMove} onEnd={onSideEnd} />
              </>
            )}
            <div
              ref={(el) => {
                centreBox.current = el
                centreRef(el)
              }}
              className="flex h-full min-h-0 shrink-0 flex-col"
              style={{ width: centrePx }}
              data-testid="centre"
            >
              {placement !== 'full' && (
                <div className="min-h-0 flex-1" data-testid="stage-slot">
                  {/* U-5: the stage is always shown while it is mounted */}
                  <QueryRegion id="stage" active>
                    <Stage surface={surface} mode={mode} />
                  </QueryRegion>
                </div>
              )}
              {placement === 'full' && (
                <button
                  type="button"
                  onClick={() => moveChat('bottom')}
                  className="h-7 shrink-0 border-b border-line bg-surf-1 px-3 text-left text-label text-ink-3 hover:text-ink-1"
                  data-testid="stage-peek"
                  title="Show the stage again, with Consult under it"
                >
                  ▴ {currentStage(useFrame.getState().stages[surface])?.title ?? 'The stage'} — show the stage
                </button>
              )}
              {placement === 'bottom' && (
                <Splitter
                  orientation="horizontal"
                  label="Divide the stage and Consult"
                  testId="chat-divider"
                  value={Math.round((chatPx / (centreH || 800)) * 100)}
                  onMove={onDividerMove}
                  onEnd={onDividerEnd}
                  onKeyStep={onDividerKey}
                />
              )}
              {(placement === 'bottom' || placement === 'full') && (
                <div
                  className="min-h-0 shrink-0"
                  style={placement === 'full' ? { flex: '1 1 0' } : { height: chatPx }}
                  data-testid="chat-slot"
                >
                  {dock}
                </div>
              )}
            </div>
            <Splitter
              orientation="vertical"
              label="Width of the centre"
              testId="centre-edge"
              onMove={onCentreEdgeMove}
              onEnd={onCentreEdgeEnd}
            />
            <div className="relative min-h-0 min-w-0 flex-1" data-testid="panel-side">
              <DockviewReact
                components={COMPONENTS}
                rightHeaderActionsComponent={RegionActions}
                onReady={onDockReady}
                theme={legbaDark}
                className="h-full"
              />
            </div>
            {placement === 'right' && (
              <>
                <Splitter orientation="vertical" label="Consult's width" testId="consult-side-splitter" onMove={onSideMove} onEnd={onSideEnd} />
                <div ref={sideBox} className="h-full min-h-0 shrink-0" style={{ width: sideColPx }} data-testid="consult-side-right">
                  {dock}
                </div>
              </>
            )}
            <RecordSheet open={sheetOpen} mode={mode} onClose={() => setSheetOpen(false)} />
            <div
              ref={setPortalHost}
              data-ui-portal-host=""
              data-testid="ui-portal-host"
              className="legba-portal-host"
            />
          </main>
        </div>
        {side === 'reader' && (
          <div data-testid="time-band" className="shrink-0">
            <TimeScrubber />
          </div>
        )}
        <StatusBar
          mode={mode}
          authenticated={getToken() != null}
          lastRefresh={lastRefresh}
          errorText={errorText}
          onOpenExport={() => drive(openPanel('system.report_export'))}
          onOpenPanel={(kind) => drive(openPanel(kind))}
        />
        {/* U-5: the palette stays mounted while closed; its reads wait for it to open */}
        <QueryRegion id="palette" active={paletteOpen}>
          <CommandPalette open={paletteOpen} mode={mode} side={side} onClose={() => setPaletteOpen(false)} />
        </QueryRegion>
      </div>
    </PortalContainerProvider>
  )
}
