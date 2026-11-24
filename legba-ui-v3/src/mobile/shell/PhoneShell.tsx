/**
 * PhoneShell — the phone's second shell at `/m/` (M-2; plan §2, §3.3).
 *
 *   top bar     Back (any drill-in), the screen's name, refresh, Settings
 *   the screen  the tab's root, or the page pushed over it, or the Record's
 *               full page; the Record's peek is a sheet over all of them
 *   bottom bar  Changed · Watch · Ledger · Consult · Subjects (56 px plus the
 *               bottom inset; a badge on Changed only, for what paged)
 *
 * The same contracts as the desktop: the phone registers its own frame port
 * and shell port (`phoneFramePort.ts`, `phoneShellPort.ts`), so cross-links
 * and Consult's actions drive it through `applyPanelAction` / `drive`; the
 * address is the desktop's hash grammar plus `#screen=` (`phoneAddress.ts`);
 * the point, the window and the record are the shared stores. Each tab's
 * root stays mounted once visited, so its scroll and its reads survive a
 * drill-in and Back.
 */
import { lazy, Suspense, useEffect, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Bell, BookOpen, ChevronLeft, MessageSquare, RotateCw, Settings, Shapes, Sparkles } from 'lucide-react'
import { drive } from '@/lib/applyAction'
import { parseAddress } from '@/lib/address'
import { useShareState } from '@/lib/shareState'
import { CHAIN_KINDS, openRecord, useRecordSheet } from '@/lib/recordSheet'
import { selectRow, useSelection } from '@/state/selection'
import { emitRead, installReadTelemetryLifecycle, setTelemetryWorkspace } from '@/lib/readTelemetry'
import { purgeStoredReads } from '@/mobile/mobileCache'
import { useWorldState } from '@/v4/world/worldState'
import ChangedScreen from '@/mobile/screens/changed/ChangedScreen'
import { badgeCount, phoneWindowOf, useChanged, useNeedsYouCount } from '@/mobile/screens/changed/changedModel'
import { registerPhoneFramePort } from './phoneFramePort'
import { registerPhoneShellPort } from './phoneShellPort'
import { usePhoneScreen, usePhoneShell } from './phoneState'
import { back, depthOf, navigate, PHONE_TABS, seedHistory, type PhoneScreen, type PhoneTab } from './phoneAddress'
import { Quiet } from './ui'

const ReadPage = lazy(() => import('@/mobile/screens/changed/ReadPage'))
const WatchScreen = lazy(() => import('@/mobile/screens/watch/WatchScreens').then((m) => ({ default: m.WatchScreen })))
const AlertPage = lazy(() => import('@/mobile/screens/watch/WatchScreens').then((m) => ({ default: m.AlertPage })))
const LedgerScreen = lazy(() => import('@/mobile/screens/ledger/LedgerScreens').then((m) => ({ default: m.LedgerScreen })))
const ProofPage = lazy(() => import('@/mobile/screens/ledger/LedgerScreens').then((m) => ({ default: m.ProofPage })))
const SubjectsScreen = lazy(() => import('@/mobile/screens/subjects/SubjectScreens').then((m) => ({ default: m.SubjectsScreen })))
const SubjectPage = lazy(() => import('@/mobile/screens/subjects/SubjectScreens').then((m) => ({ default: m.SubjectPage })))
const MapScreen = lazy(() => import('@/mobile/screens/map/MapScreen'))
const ConsultScreen = lazy(() => import('@/mobile/screens/consult/ConsultScreen'))
// the Undo toast for what a Consult answer opened (loaded once Consult has been visited)
const DriveToast = lazy(() => import('@/mobile/screens/consult/drive').then((m) => ({ default: m.DriveToast })))
const SettingsPage = lazy(() => import('@/mobile/screens/settings/SettingsPage'))
const RecordPeek = lazy(() => import('@/mobile/screens/record/RecordScreens').then((m) => ({ default: m.RecordPeek })))
const RecordPage = lazy(() => import('@/mobile/screens/record/RecordScreens').then((m) => ({ default: m.RecordPage })))

const TAB_LABEL: Record<PhoneTab, string> = {
  changed: 'Changed',
  watch: 'Watch',
  ledger: 'Ledger',
  consult: 'Consult',
  subjects: 'Subjects',
}

const TAB_ICON: Record<PhoneTab, typeof Bell> = {
  changed: Sparkles,
  watch: Bell,
  ledger: BookOpen,
  consult: MessageSquare,
  subjects: Shapes,
}

const PAGE_TITLE: Record<string, string> = {
  read: 'Read',
  alert: 'Alert',
  subject: 'Subject',
  map: 'Map',
  proof: 'Evaluations',
  settings: 'Settings',
}

/** The telemetry dimension this surface reads under (the old phone's, kept). */
const MOBILE_WORKSPACE = 'mobile'

/** Where the shell sends a record a tap selected (a citation, a map mark) — its peek. */
const PEEK_ORIGINS = new Set(['cited-prose', 'map'])

function titleOf(s: PhoneScreen): string {
  if (s.record) return 'Record'
  if (s.page) return PAGE_TITLE[s.page.kind] ?? ''
  return TAB_LABEL[s.tab]
}

function Fallback() {
  return <Quiet>Loading…</Quiet>
}

/** The viewport the keyboard leaves (Android resizes; iOS moves `visualViewport`). */
function useKeyboardViewport(): boolean {
  const [kb, setKb] = useState(false)
  useEffect(() => {
    const vv = window.visualViewport
    const apply = () => {
      const h = vv ? vv.height : window.innerHeight
      document.documentElement.style.setProperty('--ph-vvh', `${Math.round(h)}px`)
      setKb(window.innerHeight - h > 120)
    }
    apply()
    vv?.addEventListener('resize', apply)
    window.addEventListener('resize', apply)
    return () => {
      vv?.removeEventListener('resize', apply)
      window.removeEventListener('resize', apply)
    }
  }, [])
  return kb
}

/** Boot once: the ports, the address, the telemetry, the record a link names. */
function useBoot(): void {
  useEffect(() => {
    const unFrame = registerPhoneFramePort()
    const unShell = registerPhoneShellPort()
    setTelemetryWorkspace(MOBILE_WORKSPACE)
    emitRead('workspace_open', { workspace: MOBILE_WORKSPACE })
    // whatever an earlier build stored on this device goes (no bodies on the phone)
    purgeStoredReads()
    // a deep link gets its parents underneath, so Back walks up
    seedHistory()
    // the desktop grammar: the point, the scope, the window (the tab is the address itself)
    const parsed = parseAddress(window.location.hash, window.location.search)
    drive(parsed.actions.filter((a) => a.action !== 'arrange'), { origin: 'link' })
    const unTelemetry = installReadTelemetryLifecycle()
    // the page is the shell (phone.css: one column under the app's banners)
    document.documentElement.dataset.phShell = '1'
    return () => {
      unFrame()
      unShell()
      unTelemetry()
      delete document.documentElement.dataset.phShell
    }
  }, [])
}

/** A record a tap selected (a citation, a map mark) opens in the peek. */
function useSelectionOpensPeek(): void {
  useEffect(
    () =>
      useSelection.subscribe((s, prev) => {
        const sel = s.selection
        if (!sel || sel === prev.selection || !PEEK_ORIGINS.has(sel.origin ?? '')) return
        const kind = sel.instanceKey && CHAIN_KINDS.has(sel.instanceKey) ? sel.instanceKey : sel.kind === 'report' ? 'finding' : sel.kind
        if (!CHAIN_KINDS.has(kind)) return
        openRecord({ kind, id: sel.id, label: sel.label }, { origin: 'phone-peek' })
      }),
    [],
  )
}

/** The Record the address names is the record sheet's (a link opened cold included). */
function useRecordFollowsAddress(screen: PhoneScreen): void {
  const key = screen.record ? `${screen.record.ref.kind}:${screen.record.ref.id}:${screen.record.cite ?? ''}` : ''
  useEffect(() => {
    if (!screen.record) return
    const st = useRecordSheet.getState()
    const r = screen.record
    if (st.ref?.kind === r.ref.kind && st.ref.id === r.ref.id && st.cite === r.cite) return
    useRecordSheet.setState({ ref: r.ref, cite: r.cite })
    const sel = useSelection.getState().selection
    if (!sel || sel.id !== r.ref.id) selectRow(r.ref.kind, r.ref.id, r.ref.label, { origin: 'link' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
}

function Layer({ on, children, testId }: { on: boolean; children: ReactNode; testId: string }) {
  return (
    // React 18 has no `inert` prop type; the attribute is set as a string (a hidden layer takes no focus or tap).
    <div className="ph-layer" data-on={on ? '1' : '0'} aria-hidden={!on} {...(on ? {} : { inert: '' })} data-testid={testId}>
      <Suspense fallback={<Fallback />}>{children}</Suspense>
    </div>
  )
}

function Root({ tab }: { tab: PhoneTab }) {
  if (tab === 'changed') return <ChangedScreen />
  if (tab === 'watch') return <WatchScreen />
  if (tab === 'ledger') return <LedgerScreen />
  if (tab === 'consult') return <ConsultScreen />
  return <SubjectsScreen />
}

function Page({ screen }: { screen: PhoneScreen }) {
  const p = screen.page
  if (!p) return null
  if (p.kind === 'read') return <ReadPage key={p.id} id={p.id} />
  if (p.kind === 'alert') return <AlertPage key={p.id} id={p.id} />
  if (p.kind === 'subject') return <SubjectPage key={p.id} id={p.id} />
  if (p.kind === 'map') return <MapScreen />
  if (p.kind === 'proof') return <ProofPage />
  return <SettingsPage />
}

function BottomBar({ tab }: { tab: PhoneTab }) {
  const spanMs = useWorldState((s) => s.spanMs)
  const windowId = phoneWindowOf(spanMs).id
  const changed = useChanged(windowId)
  const counts = useNeedsYouCount(windowId)
  const paged = badgeCount(counts.data, changed.data)
  return (
    <nav className="ph-bar" aria-label="Destinations" data-testid="phone-bar">
      {PHONE_TABS.map((t) => {
        const Icon = TAB_ICON[t]
        return (
          <button
            key={t}
            type="button"
            className="ph-tab"
            aria-current={t === tab ? 'page' : undefined}
            data-testid={`tab-${t}`}
            onClick={() => navigate({ tab: t, page: null, record: null, peek: false }, 'replace')}
          >
            <span className="ph-tab-icon">
              <Icon size={22} aria-hidden />
              {t === 'changed' && paged > 0 ? (
                <span className="ph-badge" data-testid="tab-changed-badge" aria-label={`${paged} paged`}>
                  {paged > 99 ? '99+' : paged}
                </span>
              ) : null}
            </span>
            <span className="ph-tab-label">{TAB_LABEL[t]}</span>
          </button>
        )
      })}
    </nav>
  )
}

export default function PhoneShell() {
  useBoot()
  useShareState()
  useSelectionOpensPeek()
  const screen = usePhoneScreen()
  useRecordFollowsAddress(screen)
  const kb = useKeyboardViewport()
  const qc = useQueryClient()

  // Consult's "what's on screen" is the last screen that was not Consult.
  const screenKey = JSON.stringify(screen)
  useEffect(() => {
    if (screen.tab !== 'consult') usePhoneShell.setState({ lastScreen: screen })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [screenKey])

  const [visited, setVisited] = useState<ReadonlySet<PhoneTab>>(() => new Set([screen.tab]))
  useEffect(() => {
    if (!visited.has(screen.tab)) setVisited(new Set([...visited, screen.tab]))
  }, [screen.tab, visited])

  const depth = depthOf(screen)
  const fullRecord = screen.record && !screen.peek ? screen.record : null
  const peek = screen.record && screen.peek ? screen.record : null
  const pageOn = !!screen.page && !fullRecord

  return (
    <div className="ph-app" data-testid="phone-shell" data-tab={screen.tab} data-depth={depth} data-kb={kb ? '1' : '0'}>
      <header className="ph-top" data-testid="phone-top">
        <div className="ph-top-inner">
          {depth > 0 ? (
            <button type="button" className="ph-icon-btn" onClick={back} aria-label="Back" data-testid="phone-back">
              <ChevronLeft size={24} aria-hidden />
            </button>
          ) : (
            <span className="ph-brand" aria-hidden />
          )}
          <h1 className="ph-top-title" data-testid="phone-title">
            {titleOf(screen)}
          </h1>
          <button
            type="button"
            className="ph-icon-btn"
            aria-label="Refresh"
            data-testid="phone-refresh"
            onClick={() => void qc.refetchQueries({ type: 'active' })}
          >
            <RotateCw size={20} aria-hidden />
          </button>
          <button
            type="button"
            className="ph-icon-btn"
            aria-label="Settings"
            data-testid="phone-settings-open"
            onClick={() => navigate({ tab: screen.tab === 'consult' ? 'changed' : screen.tab, page: { kind: 'settings' }, record: null, peek: false }, 'push')}
          >
            <Settings size={20} aria-hidden />
          </button>
        </div>
      </header>

      <main className="ph-main" data-testid="phone-main">
        {PHONE_TABS.filter((t) => visited.has(t)).map((t) => (
          <Layer key={t} on={screen.tab === t && !screen.page && !fullRecord} testId={`phone-root-${t}`}>
            <Root tab={t} />
          </Layer>
        ))}
        {screen.page ? (
          <Layer on={pageOn} testId="phone-page">
            <Page screen={screen} />
          </Layer>
        ) : null}
        {fullRecord ? (
          <Layer on testId="phone-record-layer">
            <RecordPage record={fullRecord.ref} cite={fullRecord.cite} />
          </Layer>
        ) : null}
      </main>

      {peek ? (
        <Suspense fallback={null}>
          <RecordPeek key={`${peek.ref.kind}:${peek.ref.id}:${peek.cite ?? ''}`} record={peek.ref} cite={peek.cite} />
        </Suspense>
      ) : null}

      {visited.has('consult') ? (
        <Suspense fallback={null}>
          <DriveToast />
        </Suspense>
      ) : null}

      <BottomBar tab={screen.tab} />
    </div>
  )
}
