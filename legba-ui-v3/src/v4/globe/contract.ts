/**
 * The map's contract, wired (U-7) — the one place where the actions the frame
 * declared for `v4.map` (`panel-registry/contracts.ts`) meet the globe's own
 * actions (`actions.ts::globeActions`).
 *
 *   set_layer {layer, on}            globeActions.setLayer — the globe's row ids
 *   fly_to {ref | lon,lat[,zoom] |   a ref becomes the point (globeActions.select;
 *          west,south,east,north}    a subject also asks the camera to fit it);
 *                                    a position or an area asks the camera
 *   home                             globeActions.home
 *   set_window {span | from,to}      a span: the one clock ending now; from/to:
 *                                    globeActions.setWindow
 *   select {ref}                     globeActions.select
 *
 * Everything goes through stores, so a request made while the map is hidden
 * (another page, a tab behind) is applied when it is shown. The handlers are
 * registered at module load, not by the mounted map, for the same reason.
 *
 * UNDO: the router snapshots the frame, the clock, the world layers and the
 * point before each action; this module adds the globe's own rows and its
 * camera as a view part, so an Undo puts the layers back and flies the camera
 * back to where it rested.
 */
import { registerUndoPart, setPanelActionHandler } from '@/lib/panelActions'
import { SPAN_PRESETS } from '@/lib/mapTime'
import { selectionKindOf } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import { globeActions, useGlobeCamera, type CameraView } from './actions'
import { GLOBE_LAYERS, globeLayer, type GlobeLayerId } from './catalog'
import { useGlobeLayers } from './layerState'
import { placeOfId } from './chokepoints'

export const MAP_VIEW_ID = 'v4.map'

type Ref = { kind: string; id: string; label?: string }
const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function setLayer(args: Record<string, unknown>): boolean {
  const id = String(args.layer) as GlobeLayerId
  if (!GLOBE_LAYERS.some((l) => l.id === id)) throw new Error(`unknown map layer: ${id}`)
  const def = globeLayer(id)
  if (def.unavailable) throw new Error(`${id} is not available: needs ${def.unavailable}`)
  globeActions.setLayer(id, args.on === true)
  return true
}

function selectRef(ref: Ref): void {
  const kind = ref.kind === 'place' ? 'place' : selectionKindOf(ref.kind)
  const label = ref.label ?? (kind === 'place' ? placeOfId(ref.id)?.name : undefined)
  globeActions.select({ kind, id: ref.id, label })
}

function flyTo(args: Record<string, unknown>): boolean {
  const ref = args.ref as Ref | undefined
  if (ref) {
    if (ref.kind === 'place' && !placeOfId(ref.id)) throw new Error(`unknown place: ${ref.id}`)
    selectRef(ref)
    // a subject already the point would not move the camera again: ask it too
    if (ref.kind === 'target') globeActions.flyTo({ kind: 'subject', id: ref.id })
    return true
  }
  if (num(args.west) && num(args.south) && num(args.east) && num(args.north)) {
    globeActions.flyTo({ kind: 'bounds', west: args.west, south: args.south, east: args.east, north: args.north })
    return true
  }
  if (num(args.lon) && num(args.lat)) {
    if (Math.abs(args.lat) > 90 || Math.abs(args.lon) > 180) throw new Error('invalid_arg: lon/lat out of range')
    globeActions.flyTo({ kind: 'place', lon: args.lon, lat: args.lat, zoom: num(args.zoom) ? args.zoom : undefined })
    return true
  }
  throw new Error('invalid_arg: fly_to needs a ref, a lon and lat, or west, south, east and north')
}

function setWindow(args: Record<string, unknown>): boolean {
  if (typeof args.from === 'string' && typeof args.to === 'string') {
    const a = Date.parse(args.from)
    const b = Date.parse(args.to)
    if (!Number.isFinite(a) || !Number.isFinite(b) || b <= a) throw new Error('invalid_arg: from/to must be ISO times, from before to')
    globeActions.setWindow(a, b)
    return true
  }
  const preset = SPAN_PRESETS.find((p) => p.id === args.span)
  if (!preset) throw new Error('invalid_arg: set_window needs a span or from and to')
  useWorldState.getState().setSpan(preset.ms)
  return true
}

let installed = false

/** Register the map's handlers and its undo part (idempotent). */
export function installMapContract(): void {
  if (installed) return
  installed = true
  setPanelActionHandler(MAP_VIEW_ID, 'set_layer', setLayer)
  setPanelActionHandler(MAP_VIEW_ID, 'fly_to', flyTo)
  setPanelActionHandler(MAP_VIEW_ID, 'home', () => (globeActions.home(), true))
  setPanelActionHandler(MAP_VIEW_ID, 'set_window', setWindow)
  setPanelActionHandler(MAP_VIEW_ID, 'select', (a) => (selectRef(a.ref as Ref), true))
  registerUndoPart('map', {
    take: () => ({ on: { ...useGlobeLayers.getState().on }, view: useGlobeCamera.getState().view }),
    put: (snap) => {
      const s = snap as { on: Record<GlobeLayerId, boolean>; view: CameraView | null }
      useGlobeLayers.setState({ on: { ...s.on } })
      if (s.view) globeActions.flyTo({ kind: 'place', lon: s.view.lon, lat: s.view.lat, zoom: s.view.zoom })
    },
  })
}

/** What the on-screen report says about the map: its rows on and where it rests. Small. */
export function mapOnScreen(): { layers: string[]; centre: string | null } {
  const on = useGlobeLayers.getState().on
  const v = useGlobeCamera.getState().view
  return {
    layers: GLOBE_LAYERS.filter((l) => on[l.id]).map((l) => l.id),
    centre: v ? `${v.lat.toFixed(1)},${v.lon.toFixed(1)} z${v.zoom.toFixed(1)} ${v.projection}` : null,
  }
}

installMapContract()
