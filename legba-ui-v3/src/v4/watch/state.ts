/**
 * The Watch surface's own state (U-9) — the filters, the open tab, the focused
 * alert and the opened flag — kept OUTSIDE the views (a module store) so that
 * a Consult action made while a view is hidden is kept and applied when it is
 * shown, and so that an Undo can put it back (a view part, like the map's).
 *
 * The actions the three views declare (`panel-registry/contracts.ts`) land
 * here through handlers registered at module load:
 *
 *   view:watch_alerts    filter_alerts {group?, severity?, paged?, state?}
 *                        select_alert {alert}   (its subject becomes the point)
 *                        set_window {span}      (the one clock; the default)
 *   view:watch_silences  filter_silences {side}
 *                        select {ref}           (the default: the point)
 *   view:watch_edits     set_tab {tab}          (flags | record)
 *                        open_flag {flag}
 */
import { create } from 'zustand'
import { registerUndoPart, setPanelActionHandler } from '@/lib/panelActions'
import { useSelection } from '@/state/selection'
import { DEFAULT_ALERT_FILTER, type AlertFilter, type QuietSide, type SubjectRef } from './model'

export const WATCH_ALERTS_VIEW = 'view:watch_alerts'
export const WATCH_SILENCES_VIEW = 'view:watch_silences'
export const WATCH_EDITS_VIEW = 'view:watch_edits'
export const WATCH_VIEWS = [WATCH_ALERTS_VIEW, WATCH_SILENCES_VIEW, WATCH_EDITS_VIEW] as const

export type EditsTab = 'flags' | 'record'

export interface WatchState {
  alertFilter: AlertFilter
  silenceSide: 'any' | QuietSide
  editsTab: EditsTab
  /** The alert row in focus (its subject is the point when it has one). */
  focusedAlert: string | null
  /** The flag whose passages are open. */
  openFlag: string | null
  /** What the alert log shows now (the on-screen report's figure). */
  shownAlerts: number | null
  /** The subject of each alert on screen, so `select_alert` can make it the point. */
  alertSubjects: Record<string, SubjectRef>
}

export const INITIAL_WATCH: WatchState = {
  alertFilter: DEFAULT_ALERT_FILTER,
  silenceSide: 'any',
  editsTab: 'flags',
  focusedAlert: null,
  openFlag: null,
  shownAlerts: null,
  alertSubjects: {},
}

export const useWatch = create<WatchState>(() => ({ ...INITIAL_WATCH }))

export function resetWatch(): void {
  useWatch.setState({ ...INITIAL_WATCH })
}

/** Focus an alert row; its subject (when it is a record) becomes the frame's point. */
export function focusAlert(id: string, subject: SubjectRef | null, origin = 'watch'): void {
  useWatch.setState({ focusedAlert: id })
  if (subject?.kind) useSelection.getState().select({ kind: subject.kind, id: subject.id, label: subject.label, origin })
}

const pick = <T extends string>(v: unknown, fallback: T): T => (typeof v === 'string' && v ? (v as T) : fallback)

let installed = false

/** Register the three views' handlers and the undo part (idempotent). */
export function installWatchContract(): void {
  if (installed) return
  installed = true
  setPanelActionHandler(WATCH_ALERTS_VIEW, 'filter_alerts', (a) => {
    const f = useWatch.getState().alertFilter
    useWatch.setState({
      alertFilter: {
        group: pick(a.group, f.group),
        severity: pick(a.severity, f.severity),
        paged: pick(a.paged, f.paged),
        state: pick(a.state, f.state),
      },
    })
    return true
  })
  setPanelActionHandler(WATCH_ALERTS_VIEW, 'select_alert', (a) => {
    const id = String(a.alert)
    const subject = useWatch.getState().alertSubjects[id]
    if (!subject) throw new Error(`no alert ${id} on screen`)
    focusAlert(id, subject, 'case')
    return true
  })
  setPanelActionHandler(WATCH_SILENCES_VIEW, 'filter_silences', (a) => {
    useWatch.setState({ silenceSide: pick(a.side, 'any') })
    return true
  })
  setPanelActionHandler(WATCH_EDITS_VIEW, 'set_tab', (a) => {
    useWatch.setState({ editsTab: a.tab === 'record' ? 'record' : 'flags' })
    return true
  })
  setPanelActionHandler(WATCH_EDITS_VIEW, 'open_flag', (a) => {
    useWatch.setState({ editsTab: 'flags', openFlag: String(a.flag) })
    return true
  })
  registerUndoPart('watch', {
    take: () => {
      const s = useWatch.getState()
      return { alertFilter: { ...s.alertFilter }, silenceSide: s.silenceSide, editsTab: s.editsTab, focusedAlert: s.focusedAlert, openFlag: s.openFlag }
    },
    put: (snap) => useWatch.setState({ ...(snap as Partial<WatchState>) }),
  })
}

/**
 * What the on-screen report says about Watch while one of its views is on the
 * page: the filter in force, how many alerts it shows, and the focused alert.
 * Small (ids and words only).
 */
export function watchOnScreen(): { filter: string; alerts: number | null; focused: string | null; edits: EditsTab } {
  const s = useWatch.getState()
  const f = s.alertFilter
  return {
    filter: `group=${f.group} severity=${f.severity} paged=${f.paged} state=${f.state} side=${s.silenceSide}`,
    alerts: s.shownAlerts,
    focused: s.focusedAlert,
    edits: s.editsTab,
  }
}

installWatchContract()
