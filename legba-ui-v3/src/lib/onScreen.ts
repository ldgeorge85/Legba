/**
 * onScreen — "what is on screen now", as data (U-6; brief §4 "Consult knows
 * the point. Every question carries the current point, the window and the ids
 * of what is in view").
 *
 *   {
 *     "v": 1, "page": "board", "side": "reader",
 *     "point": { "kind": "target", "id": "…", "label": "…" } | null,
 *     "window": { "span": "24h" | null, "from": "…Z", "to": "…Z" },
 *     "views": [ { "id": "v4.morning_read", "at": "stage", "front": true },
 *                { "id": "v4.map", "at": "p1", "front": true }, … ],
 *     "chat": { "placement": "bottom", "collapsed": false },
 *     "map": { "layers": ["change", "blanks", "series"], "centre": "20.0,30.0 z1.4 globe" }
 *   }
 *
 * `map` (U-7) is there only while the map is on the page: the globe's layer
 * ids that are on, and where its camera rests (lat,lon, zoom, projection).
 *
 * It is SMALL by construction — ids and places only, never a view's content,
 * at most {@link MAX_VIEWS} views — so it costs a turn almost nothing to carry
 * (`onScreen.test.ts` holds it under {@link MAX_BYTES} bytes).
 *
 * Consult sends it as the `on_screen` field of a turn. The registry's consult
 * request model ignores fields it does not declare (pydantic's default), so
 * today it changes neither the answer nor what a turn costs; the core lane
 * that widens Consult's context (brief §4, item 4) declares the field and
 * hands it to the planner.
 */
import { getFramePort, type ViewOnScreen } from '@/lib/panelActions'
import { findSurface } from '@/lib/surfaces'
import { useAmbientPins, resolveAmbientPin } from '@/lib/consultContext'
import { useScope } from '@/state/scope'
import { useWorldState } from '@/v4/world/worldState'
import { spanPresetFor } from '@/lib/mapTime'
import { mapOnScreen, MAP_VIEW_ID } from '@/v4/globe/contract'
import { watchOnScreen, WATCH_VIEWS } from '@/v4/watch/state'
import { recordSheetOnScreen } from '@/lib/recordSheet'
import { useSelection } from '@/state/selection'
import { ledgerOnScreen } from '@/panels/ledger/ledgerView'

export const MAX_VIEWS = 12
export const MAX_BYTES = 1500

export interface OnScreen {
  v: 1
  page: string
  side: 'reader' | 'operator'
  point: { kind: string; id: string; label: string | null } | null
  window: { span: string | null; from: string; to: string }
  views: ViewOnScreen[]
  chat: { placement: string; collapsed: boolean }
  map?: { layers: string[]; centre: string | null }
  /** U-9: the Watch filters in force, the alerts shown and the focused one, while a Watch view is on the page. */
  watch?: ReturnType<typeof watchOnScreen>
  /** U-11: while a Ledger view is on screen — the forecast in focus, the list's filter, the open evaluation run. */
  ledger?: { forecast: string | null; filter: string; run: string | null }
}

const LEDGER_VIEW_IDS = ['view:forecasts', 'view:track_record', 'view:proof', 'view:resolutions']

/** The on-screen report, or null when no frame is mounted (a bare panel in a test). */
export function describeOnScreen(): OnScreen | null {
  const port = getFramePort()
  if (!port) return null
  const page = port.surface()
  const ambient = resolveAmbientPin(useAmbientPins.getState().claims, useScope.getState().scope)
  const w = useWorldState.getState()
  const iso = (ms: number) => {
    const d = new Date(ms)
    return Number.isFinite(d.getTime()) ? d.toISOString().slice(0, 16) + 'Z' : ''
  }
  // U-10: the Record sheet, while open, is the first view listed (`at: 'sheet'`).
  const sheet = recordSheetOnScreen(useSelection.getState().selection)
  const views = [...(sheet ? [sheet] : []), ...port.views()].slice(0, MAX_VIEWS)
  const out: OnScreen = {
    v: 1,
    page,
    side: findSurface(page)?.side ?? 'reader',
    point: ambient
      ? {
          kind: ambient.pin.kind,
          id: ambient.pin.id,
          label: ambient.pin.label && ambient.pin.label !== ambient.pin.id ? ambient.pin.label.slice(0, 80) : null,
        }
      : null,
    window: { span: spanPresetFor(w.spanMs)?.id ?? null, from: iso(w.windowStartMs), to: iso(w.windowEndMs) },
    views,
    chat: port.chat(),
  }
  if (views.some((v) => v.id === MAP_VIEW_ID)) out.map = mapOnScreen()
  if (views.some((v) => (WATCH_VIEWS as readonly string[]).includes(v.id))) out.watch = watchOnScreen()
  if (views.some((v) => LEDGER_VIEW_IDS.includes(v.id))) out.ledger = ledgerOnScreen()
  return out
}
