/**
 * phoneFramePort — the phone's FRAME PORT (M-2; `lib/panelActions.ts`).
 *
 * The desktop frame (`App.tsx`) registers a port over Dockview; the phone
 * registers this one over its screen stack, so a click, a cross-link and a
 * Consult `panel_action` reach a phone view through the same router
 * (`applyPanelAction`) and the same contracts, and the on-screen report
 * (`lib/onScreen.describeOnScreen`) describes the phone's screen without a
 * line of phone-specific code there.
 *
 *   openView   maps a view's contract onto the stack: a view that is a tab's
 *              page (`v4.morning_read`, the Watch views, the Ledger's
 *              forecasts) switches to that tab (REPLACE); a drill-in
 *              (`subject.page:<id>`, `v4.map`, `view:proof`, the Record)
 *              is PUSHED, so Back returns
 *   views      the one screen, `at: 'screen'` (the Record's peek: `sheet`);
 *              while Consult is the tab, the screen the person came from
 *   chat       `{placement: 'tab', collapsed}` — Consult is a tab here
 *   snapshot / restore   the address: every Consult action is undoable
 *
 * A view the phone does not show (the operator side, the dock-only tools)
 * answers false, which the router reports as `not opened: <view>`.
 */
import { registerFramePort, type FramePort, type OpenViewOptions, type ViewOnScreen } from '@/lib/panelActions'
import { RECORD_VIEW_ID, useRecordSheet } from '@/lib/recordSheet'
import { WATCH_ALERTS_VIEW, WATCH_EDITS_VIEW, WATCH_SILENCES_VIEW } from '@/v4/watch/state'
import { MAP_VIEW_ID } from '@/v4/globe/contract'
import { currentScreen, usePhoneShell, type WatchSegment } from './phoneState'
import { navigate, PHONE_NAV_EVENT, TAB_SURFACE, type PhoneScreen, type PhoneTab } from './phoneAddress'

/** The view a tab's root shows (its on-screen report id). */
export const SEGMENT_VIEW: Record<WatchSegment, string> = {
  alerts: WATCH_ALERTS_VIEW,
  silences: WATCH_SILENCES_VIEW,
  edits: WATCH_EDITS_VIEW,
}

const READ_VIEW = 'v4.morning_read'
const SUBJECT_VIEW = 'subject.page'
const CONSULT_VIEW = 'system.consult'

/** The views a screen shows, as the on-screen report lists them. */
export function viewsOfScreen(s: PhoneScreen, segment: WatchSegment): ViewOnScreen[] {
  const out: ViewOnScreen[] = []
  const page = s.page
  if (page?.kind === 'map') out.push({ id: MAP_VIEW_ID, at: 'screen', front: !s.record })
  else if (page?.kind === 'subject') out.push({ id: `${SUBJECT_VIEW}:${page.id}`.slice(0, 120), at: 'screen', front: !s.record })
  else if (page?.kind === 'proof') out.push({ id: 'view:proof', at: 'screen', front: !s.record })
  else if (page?.kind === 'alert') out.push({ id: WATCH_ALERTS_VIEW, at: 'screen', front: !s.record })
  else if (page?.kind === 'read') out.push({ id: READ_VIEW, at: 'screen', front: !s.record })
  else if (page?.kind === 'settings') {
    // Settings is the shell's own page: not a view Consult can drive.
  } else if (s.tab === 'changed') out.push({ id: READ_VIEW, at: 'screen', front: !s.record })
  else if (s.tab === 'watch') out.push({ id: SEGMENT_VIEW[segment], at: 'screen', front: !s.record })
  else if (s.tab === 'ledger') {
    out.push({ id: 'view:forecasts', at: 'screen', front: !s.record })
    out.push({ id: 'view:track_record', at: 'screen', front: false })
  } else if (s.tab === 'subjects') {
    // the roster: a list, not a view with a contract
  }
  if (s.record) out.push({ id: `${RECORD_VIEW_ID}:${s.record.ref.kind}:${s.record.ref.id}`.slice(0, 120), at: s.peek ? 'sheet' : 'screen', front: true })
  return out
}

function go(next: PhoneScreen, how: 'push' | 'replace'): true {
  navigate(next, how)
  return true
}

function toTab(tab: PhoneTab): true {
  const cur = currentScreen()
  if (cur.tab === tab && !cur.page && !cur.record) return true
  return go({ tab, page: null, record: null, peek: false }, 'replace')
}

/** Open a view on the phone; false when the phone does not show it. */
export function openPhoneView(viewId: string, opts: OpenViewOptions = {}): boolean {
  const cur = currentScreen()
  if (viewId === RECORD_VIEW_ID) {
    const st = useRecordSheet.getState()
    if (!st.ref) return false
    // the peek first (plan §2.3: a medium sheet, then "Open the record" pushes the pages)
    if (cur.record && cur.record.ref.kind === st.ref.kind && cur.record.ref.id === st.ref.id && cur.record.cite === st.cite) return true
    return go({ ...cur, record: { ref: st.ref, cite: st.cite }, peek: true }, 'push')
  }
  if (viewId === READ_VIEW) return toTab('changed')
  if (viewId === CONSULT_VIEW) return toTab('consult')
  if (viewId === WATCH_ALERTS_VIEW || viewId === WATCH_SILENCES_VIEW || viewId === WATCH_EDITS_VIEW) {
    const seg = (Object.keys(SEGMENT_VIEW) as WatchSegment[]).find((k) => SEGMENT_VIEW[k] === viewId)!
    usePhoneShell.setState({ watchSegment: seg })
    return toTab('watch')
  }
  if (viewId === 'view:forecasts' || viewId === 'view:track_record') return toTab('ledger')
  if (viewId === 'view:proof') return go({ tab: 'ledger', page: { kind: 'proof' }, record: null, peek: false }, 'push')
  if (viewId === MAP_VIEW_ID) {
    if (cur.page?.kind === 'map' && !cur.record) return true
    return go({ tab: cur.tab === 'consult' ? 'changed' : cur.tab, page: { kind: 'map' }, record: null, peek: false }, 'push')
  }
  if (viewId === SUBJECT_VIEW) {
    if (!opts.recordId) return toTab('subjects')
    if (cur.page?.kind === 'subject' && cur.page.id === opts.recordId && !cur.record) return true
    return go({ tab: 'subjects', page: { kind: 'subject', id: opts.recordId }, record: null, peek: false }, 'push')
  }
  return false
}

/** Is this view (or an instance of it) on the phone's screen now? */
export function findPhoneView(id: string): string | null {
  const s = currentScreen()
  const shown = viewsOfScreen(s.tab === 'consult' ? usePhoneShell.getState().lastScreen ?? s : s, usePhoneShell.getState().watchSegment)
  for (const v of shown) {
    if (v.id === id) return v.id
    const bare = v.id.startsWith(`${id}:`) ? v.id : null
    if (bare) return bare
  }
  return null
}

export const phoneFramePort: FramePort = {
  surface: () => {
    const s = currentScreen()
    return TAB_SURFACE[s.tab] ?? 'consult'
  },
  openView: (viewId, opts) => openPhoneView(viewId, opts),
  closeView: (instanceId) => {
    const s = currentScreen()
    if (instanceId.startsWith(RECORD_VIEW_ID) && s.record) return go({ ...s, record: null, peek: false }, 'replace')
    const top = viewsOfScreen(s, usePhoneShell.getState().watchSegment).find((v) => v.front)
    if (top && top.id === instanceId && s.page) return go({ ...s, page: null }, 'replace')
    return false
  },
  focusView: (instanceId) => findPhoneView(instanceId) !== null,
  findOpen: (id) => findPhoneView(id),
  views: () => {
    const s = currentScreen()
    const st = usePhoneShell.getState()
    // While Consult is the tab, "what's on screen" is the screen the person came from.
    const shown = s.tab === 'consult' ? st.lastScreen : s
    return shown ? viewsOfScreen(shown, st.watchSegment) : []
  },
  chat: () => ({ placement: 'tab', collapsed: currentScreen().tab !== 'consult' }),
  snapshot: () => ({ hash: window.location.hash, segment: usePhoneShell.getState().watchSegment }),
  restore: (snap) => {
    const s = snap as { hash?: string; segment?: WatchSegment } | null
    if (!s || typeof s.hash !== 'string') return
    if (s.segment) usePhoneShell.setState({ watchSegment: s.segment })
    try {
      window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}${s.hash}`)
      window.dispatchEvent(new Event(PHONE_NAV_EVENT))
    } catch {
      // history unavailable
    }
  },
}

/** Register the port (the shell calls this once, in an effect). */
export function registerPhoneFramePort(): () => void {
  return registerFramePort(phoneFramePort)
}
