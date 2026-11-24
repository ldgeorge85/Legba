/**
 * panelActions — THE ROUTER from an action to a view, through its contract
 * (U-6; `panel-registry/contracts.ts`).
 *
 *   applyPanelAction('subject.page:brazil', 'set_tab', { tab: 'evidence' })
 *
 * A click, a link and a Consult action all come here, by the view's id (or
 * an instance id, `<kind>:<record>`), and nothing reaches a view any other
 * way:
 *
 *   1. the contract is looked up (`contractOf`) — an unknown view is refused;
 *   2. the action must be one the view accepts (the common open / close /
 *      focus, or one it declares) — anything else is refused by name;
 *   3. the arguments are checked against the declared types;
 *   4. the frame's state is snapshotted for UNDO (every panel action is a
 *      view action; none of them writes anything);
 *   5. a handler the mounted view registered for this action runs
 *      ({@link usePanelActionHandler}); without one the DEFAULT runs — the
 *      frame port for open / close / focus / bind, and the stores every view
 *      already follows for the rest (the tab request, the one clock, the
 *      selection, the map's layers). So the map is driven without touching
 *      the map's code.
 *
 * The frame (`App.tsx`) registers a {@link FramePort}; this module never
 * touches Dockview. `drive` (`lib/applyAction.ts`) stays the executor of the
 * nine `case/1` actions, and its `open_panel` lands where the same contract
 * says (`lib/surfaces.ts::homeOf`).
 */
import { useEffect, useRef } from 'react'
import { create } from 'zustand'
import { contractOf } from '@/panel-registry/contractOf'
import { acceptedAction, checkArgs, viewIdOf, type PanelContract } from '@/panel-registry/contracts'
import { requestPanelTab } from '@/state/panelParams'
import { selectionKindOf, useSelection, type Selection } from '@/state/selection'
import { useWorldState, type WorldLayer } from '@/v4/world/worldState'
import { SPAN_PRESETS } from '@/lib/mapTime'
import type { StepOutcome } from '@/lib/applyAction'

// ---------------------------------------------------------------------------
// The frame port.
// ---------------------------------------------------------------------------

export interface OpenViewOptions {
  /** Where: the stage or the panel side. The contract's home when absent. */
  place?: 'stage' | 'panel'
  recordId?: string
  axis?: 'target' | 'analyst'
  tab?: string
  origin?: string
}

/** One view on screen, as the on-screen report lists it. */
export interface ViewOnScreen {
  /** The instance id (`v4.map`, `subject.page:brazil`). */
  id: string
  /** `stage`, or the panel region it sits in (`p1`, `p2`, … left to right). */
  at: string
  /** In front of its region (a tab behind another is listed with false). */
  front: boolean
}

export interface FramePort {
  surface: () => string
  openView: (viewId: string, opts: OpenViewOptions) => boolean
  closeView: (instanceId: string) => boolean
  focusView: (instanceId: string) => boolean
  /** Is this instance (or, for a view id, any instance of it) on screen? */
  findOpen: (id: string) => string | null
  views: () => ViewOnScreen[]
  chat: () => { placement: string; collapsed: boolean }
  snapshot: () => unknown
  restore: (snapshot: unknown) => void
}

let port: FramePort | null = null

export function registerFramePort(p: FramePort): () => void {
  port = p
  return () => {
    if (port === p) port = null
  }
}

export function getFramePort(): FramePort | null {
  return port
}

// ---------------------------------------------------------------------------
// Handlers a mounted view registers.
// ---------------------------------------------------------------------------

type Handler = (args: Record<string, unknown>) => boolean | void

const handlers = new Map<string, Handler>()
const hkey = (id: string, action: string) => `${id}\u0000${action}`

/**
 * A mounted view takes over one of ITS declared actions (a list that selects
 * its own row, a canvas that pans). `id` is the instance id or the view id.
 * Return false from the handler to fall through to the default.
 */
export function usePanelActionHandler(id: string, action: string, handler: Handler): void {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    const fn: Handler = (a) => ref.current(a)
    handlers.set(hkey(id, action), fn)
    return () => {
      if (handlers.get(hkey(id, action)) === fn) handlers.delete(hkey(id, action))
    }
  }, [id, action])
}

/** Non-React registration (tests, frame views without hooks). */
export function setPanelActionHandler(id: string, action: string, handler: Handler | null): void {
  if (handler) handlers.set(hkey(id, action), handler)
  else handlers.delete(hkey(id, action))
}

// ---------------------------------------------------------------------------
// Undo — every view action can be taken back.
// ---------------------------------------------------------------------------

interface ViewSnapshot {
  label: string
  frame: unknown
  world: { start: number; end: number; spanMs: number; layers: Record<WorldLayer, boolean> }
  selection: Selection | null
  /** What each registered view part held (the map's own layers and camera). */
  parts: Record<string, unknown>
}

/**
 * A VIEW PART (U-7): state a view keeps outside the frame, the clock and the
 * point — the map's layer rows and its camera — that an Undo must also take
 * back. A view registers a part once (module level, so it is kept while the
 * view is hidden); `take` snapshots it, `put` restores it.
 */
export interface UndoPart {
  take: () => unknown
  put: (snapshot: unknown) => void
}

const parts = new Map<string, UndoPart>()

export function registerUndoPart(name: string, part: UndoPart): void {
  parts.set(name, part)
}

interface UndoState {
  stack: ViewSnapshot[]
}

export const useViewUndo = create<UndoState>(() => ({ stack: [] }))

const UNDO_DEPTH = 20

/** Snapshot the view before a change, under a label ("open Map"). */
export function pushViewUndo(label: string): void {
  const w = useWorldState.getState()
  const snap: ViewSnapshot = {
    label,
    frame: port ? port.snapshot() : null,
    world: { start: w.windowStartMs, end: w.windowEndMs, spanMs: w.spanMs, layers: { ...w.layers } },
    selection: useSelection.getState().selection,
    parts: Object.fromEntries([...parts].map(([k, p]) => [k, p.take()])),
  }
  useViewUndo.setState((s) => ({ stack: [...s.stack, snap].slice(-UNDO_DEPTH) }))
}

/** Undo the last view change. Returns its label, or null when there is none. */
export function undoView(): string | null {
  const st = useViewUndo.getState().stack
  const last = st[st.length - 1]
  if (!last) return null
  useViewUndo.setState({ stack: st.slice(0, -1) })
  if (port && last.frame) port.restore(last.frame)
  useWorldState.setState({
    layers: { ...last.world.layers },
    spanMs: last.world.spanMs,
    windowStartMs: last.world.start,
    windowEndMs: last.world.end,
  })
  const sel = useSelection.getState()
  if (last.selection) sel.select(last.selection)
  else sel.clear()
  for (const [k, v] of Object.entries(last.parts)) parts.get(k)?.put(v)
  return last.label
}

export function clearViewUndo(): void {
  useViewUndo.setState({ stack: [] })
}

// ---------------------------------------------------------------------------
// The router.
// ---------------------------------------------------------------------------

export interface PanelActionOptions {
  /** Who: `operator` (a click, a link) or `case` (Consult). Stamped on what it writes. */
  origin?: string
  /** Skip the undo snapshot (the caller grouped several actions under one). */
  noUndo?: boolean
}

const EXECUTED: StepOutcome = { state: 'executed' }

function isBound(instanceId: string): boolean {
  return viewIdOf(instanceId) !== instanceId
}

/** The id a tab request rides: a bound instance's own id, else the view id. */
function tabKey(instanceId: string, viewId: string): string {
  return isBound(instanceId) ? instanceId : viewId
}

function ensureOpen(instanceId: string, contract: PanelContract, origin: string): boolean {
  if (!port) return false
  const live = port.findOpen(instanceId)
  if (live) return port.focusView(live)
  if (isBound(instanceId)) {
    const recordId = instanceId.slice(contract.id.length + 1)
    return port.openView(contract.id, { recordId, axis: contract.binds.includes('unit') ? 'analyst' : 'target', origin })
  }
  return port.openView(contract.id, { origin })
}

function runDefault(
  instanceId: string,
  contract: PanelContract,
  action: string,
  args: Record<string, unknown>,
  origin: string,
): StepOutcome {
  const viewId = contract.id
  switch (action) {
    case 'open': {
      if (!port) return { state: 'skipped', reason: 'no frame to open in' }
      const place = args.place as 'stage' | 'panel' | undefined
      if (isBound(instanceId)) {
        const recordId = instanceId.slice(viewId.length + 1)
        return port.openView(viewId, { place, recordId, axis: contract.binds.includes('unit') ? 'analyst' : 'target', origin })
          ? EXECUTED
          : { state: 'skipped', reason: `not opened: ${instanceId}` }
      }
      return port.openView(viewId, { place, origin }) ? EXECUTED : { state: 'skipped', reason: `not opened: ${viewId}` }
    }
    case 'close': {
      if (!port) return { state: 'skipped', reason: 'no frame' }
      const live = port.findOpen(instanceId)
      if (!live) return { state: 'skipped', reason: `not on screen: ${instanceId}` }
      return port.closeView(live) ? EXECUTED : { state: 'refused', reason: `cannot close: ${instanceId}` }
    }
    case 'focus':
      return ensureOpen(instanceId, contract, origin) ? EXECUTED : { state: 'skipped', reason: `not opened: ${instanceId}` }
    case 'bind': {
      if (!port) return { state: 'skipped', reason: 'no frame' }
      const ref = args.subject as { kind: string; id: string }
      return port.openView(viewId, { recordId: ref.id, axis: ref.kind === 'analyst' ? 'analyst' : 'target', origin })
        ? EXECUTED
        : { state: 'skipped', reason: `not bound: ${viewId}` }
    }
    case 'set_tab': {
      ensureOpen(instanceId, contract, origin)
      const live = port?.findOpen(instanceId) ?? instanceId
      requestPanelTab(tabKey(live, viewId), String(args.tab))
      return EXECUTED
    }
    case 'set_window': {
      const preset = SPAN_PRESETS.find((p) => p.id === args.span)
      if (!preset) return { state: 'refused', reason: `invalid_arg: span ${String(args.span)}` }
      useWorldState.getState().setSpan(preset.ms)
      return EXECUTED
    }
    case 'select':
    case 'fly_to': {
      const ref = args.ref as { kind: string; id: string; label?: string }
      useSelection.getState().select({ kind: selectionKindOf(ref.kind), id: ref.id, label: ref.label, origin })
      return EXECUTED
    }
    case 'layer': {
      const w = useWorldState.getState()
      const layer = args.layer as WorldLayer
      if (w.layers[layer] !== (args.on === true)) w.toggleLayer(layer)
      return EXECUTED
    }
    default:
      return { state: 'refused', reason: `no default for ${action} on ${viewId}; the view must handle it` }
  }
}

/**
 * Run one action on one view. Never throws: a handler that throws becomes a
 * `skipped` step with its message.
 */
export function applyPanelAction(
  id: string,
  action: string,
  args: Record<string, unknown> = {},
  opts: PanelActionOptions = {},
): StepOutcome {
  const origin = opts.origin ?? 'operator'
  const contract = contractOf(id)
  if (!contract) return { state: 'refused', reason: `unknown view: ${id}` }
  const spec = acceptedAction(contract, action)
  if (!spec) return { state: 'refused', reason: `action_not_accepted: ${action} on ${contract.id}` }
  const checked = checkArgs(spec, args)
  if (!checked.ok) return { state: 'refused', reason: `invalid_arg: ${checked.reason}` }
  try {
    if (!opts.noUndo) pushViewUndo(`${spec.label.toLowerCase()} · ${contract.name}`)
    const live = port?.findOpen(id) ?? id
    const own = handlers.get(hkey(live, action)) ?? handlers.get(hkey(contract.id, action))
    if (own && own(checked.args) !== false) return EXECUTED
    return runDefault(id, contract, action, checked.args, origin)
  } catch (err) {
    return { state: 'skipped', reason: err instanceof Error ? err.message : String(err) }
  }
}

/** A list of panel actions, under ONE undo step. */
export function drivePanels(
  steps: readonly { id: string; action: string; args?: Record<string, unknown> }[],
  opts: Omit<PanelActionOptions, 'noUndo'> & { label?: string } = {},
): StepOutcome[] {
  if (steps.length === 0) return []
  pushViewUndo(opts.label ?? `${steps.length} view change${steps.length === 1 ? '' : 's'}`)
  return steps.map((s) => applyPanelAction(s.id, s.action, s.args ?? {}, { ...opts, noUndo: true }))
}
