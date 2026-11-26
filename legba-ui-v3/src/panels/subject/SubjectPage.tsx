/**
 * The subject page (`subject.page`) — UI-B3; UI-D1 §2.1 (the subject row) and
 * Table A ("survivor of `target.*` ×9 + `target.desk_brief_page`").
 *
 * ONE bound page per SUBJECT of any frame kind — a country desk, a lane or a
 * flow, a region, a situation, a regime — tabbed:
 *
 *   Brief · Evidence · Claims · Signals · Situations · Sources · Graph · Runtime
 *
 * Each tab is its own file under `panels/subject/` and mounts the retired
 * `target.*` component UNMODIFIED (the UI-B1/merges idiom; the Brief tab's
 * component was generalized in place — `panels/target/DeskBriefPage.tsx`).
 * The eight retired ids alias onto their tabs (`panel-registry/aliases.ts`):
 * `target.overview` → `#runtime`, `target.signals` → `#signals`,
 * `target.findings` → `#evidence`, `target.claims` → `#claims`,
 * `target.situations` → `#situations`, `target.sources` → `#sources`,
 * `target.graph` → `#graph`, `target.desk_brief_page` → `#brief`.
 *
 * ## Two ways onto the wall
 *
 *   * **Bound** (`subject.page:<id>`) — opened on a subject: the rail's
 *     Subjects, ⌘K `@`, any subject chip, a recorded case's
 *     `open_panel {subject, tab}`. The page is about that subject for its
 *     whole life and PINS it (UI-B6: Consult follows the page).
 *   * **Following** (`subject.page`, no binding) — what the Subject stance
 *     (id `desk`) seeds: the page reads the subject the reader selected (the
 *     selection, else the wall scope), keeps the last one when the reader
 *     drills off it (a unit's read, a finding), and states its absence when
 *     nothing has been selected yet. (`kindPlan.ts::SUBJECT_FOLLOWING_KINDS`.)
 *
 * ## The pin
 *
 * The page pins its subject through UI-B6's hook (`usePinScope`) and
 * re-asserts it on every tab change; the Brief tab's unit-block drill re-pins
 * to THAT unit's read (a newer claim), and leaving the tab releases it.
 *
 * ## Absence
 *
 * Table A's contract: a unit with no read shows its typed absence or *no read
 * yet*; no composition → "no composition on record for this subject";
 * correctness absent → *unmeasured* beside its coverage; a layer not declared
 * → "no layer map is declared" — all on the Brief tab, every subject word the
 * profile's noun.
 */
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import type { ComponentType, LazyExoticComponent } from 'react'
import type { PanelProps, PanelRegistration } from '@/types'
import { PanelTabStrip } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import { useMergedTab } from '@/panels/merged/useMergedTab'
import { readLastSubject, usePointSubject } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useActiveSurface } from '@/state/activeSurface'
import { mostChanged } from '@/v4/read/boardList'
import { useChangeCounts } from '@/v4/read/boardData'
import { usePinScope } from '@/lib/consultContext'
import { humanizeId, subjectName, useSubjectIndex } from '@/lib/deskNames'
import { PROFILE_UI } from '@/lib/profileUi'
import { useBelowAdminPassive as useBelowAdmin } from '@/auth/roles'
import {
  SUBJECT_DEFAULT_TAB,
  subjectNoun,
  subjectPageKey,
  subjectTabsFor,
  type SubjectTab,
} from '@/lib/subjectPage'

/** One file per tab, each loaded when first shown (Graph carries cytoscape). */
export const SUBJECT_TAB_COMPONENTS: Record<SubjectTab, LazyExoticComponent<ComponentType<PanelProps>>> = {
  brief: lazy(() => import('./BriefTab')),
  evidence: lazy(() => import('./EvidenceTab')),
  claims: lazy(() => import('./ClaimsTab')),
  signals: lazy(() => import('./SignalsTab')),
  situations: lazy(() => import('./SituationsTab')),
  sources: lazy(() => import('./SourcesTab')),
  graph: lazy(() => import('./GraphTab')),
  runtime: lazy(() => import('./RuntimeTab')),
}

/** Tabs only an admin is offered. */
const OPERATOR_TABS: ReadonlySet<SubjectTab> = new Set<SubjectTab>(['runtime'])

/** The subject a BOUND page was opened on, or null for the following page. */
export function boundSubjectOf(props: Pick<PanelProps, 'scope' | 'registration'>): string | null {
  const fromScope = props.scope?.target_id
  if (fromScope) return fromScope
  const bound = props.registration?.binding?.target_id
  return typeof bound === 'string' && bound ? bound : null
}

/**
 * The subject an UNBOUND page follows: THE POINT (`state/scope.ts`), the one
 * subject the top bar names and every region filters by. With no point it
 * keeps showing the last subject it followed, else the last one the reader
 * looked at, else the subject with the most changes since the last visit —
 * the page is never empty — and says that it is not the point.
 */
function useFollowedSubject(enabled: boolean): { id: string | null; isPoint: boolean; why: 'point' | 'kept' | 'last' | 'most' | null } {
  const point = usePointSubject()?.id ?? null
  const [kept, setKept] = useState<string | null>(point)
  useEffect(() => {
    if (enabled && point) setKept(point)
  }, [enabled, point])
  const counts = useChangeCounts(enabled && !point && !kept)
  const last = useMemo(() => (enabled ? readLastSubject() : null), [enabled])
  if (!enabled) return { id: null, isPoint: false, why: null }
  if (point) return { id: point, isPoint: true, why: 'point' }
  if (kept) return { id: kept, isPoint: false, why: 'kept' }
  if (last) return { id: last, isPoint: false, why: 'last' }
  const most = mostChanged(counts)
  return most ? { id: most, isPoint: false, why: 'most' } : { id: null, isPoint: false, why: null }
}

/**
 * On the subject tab, a page that has never shown a subject makes its
 * default (the last one looked at, else the most changed) the point, so the
 * top bar, Consult and the page name the same subject from the first look.
 * After the reader clears the point the page keeps what it showed and does
 * not take the point back.
 */
function useAdoptDefault(followed: ReturnType<typeof useFollowedSubject>): void {
  const onSubjectTab = useActiveSurface((s) => s.surface === 'subject')
  const adopted = useRef(false)
  useEffect(() => {
    if (adopted.current || !onSubjectTab || followed.isPoint) {
      if (followed.isPoint) adopted.current = true
      return
    }
    if ((followed.why === 'last' || followed.why === 'most') && followed.id) {
      adopted.current = true
      useSelection.getState().select({ kind: 'target', id: followed.id, origin: 'shell' })
    }
  }, [onSubjectTab, followed.isPoint, followed.why, followed.id])
}

function capitalised(word: string): string {
  return word ? word.charAt(0).toUpperCase() + word.slice(1) : word
}

export default function SubjectPage(props: PanelProps) {
  const bound = boundSubjectOf(props)
  const followed = useFollowedSubject(bound === null)
  useAdoptDefault(followed)
  const subject = bound ?? followed.id
  const index = useSubjectIndex()
  const name = useMemo(() => (subject ? subjectName(subject, index) : null), [subject, index])

  // A tab the mode does not ship (Signals, Sources in `cis`) is not offered,
  // and a request for one falls back to the Brief, never an empty surface.
  // The Runtime tab is the engine's view of the subject (its producers'
  // runs): an admin's, not a reader's.
  const belowAdmin = useBelowAdmin()
  const tabs = useMemo(
    () => subjectTabsFor(props.mode).filter((t) => !belowAdmin || !OPERATOR_TABS.has(t.id as SubjectTab)),
    [props.mode, belowAdmin],
  )
  const [tab, setTab] = useMergedTab<SubjectTab>(subjectPageKey(bound), props.initialTab, tabs, SUBJECT_DEFAULT_TAB)

  // THE PIN (UI-B6): the page pins its subject, re-asserted on every tab
  // change so a drill on one tab never outlives a move to another.
  const tabRev = useRef(0)
  const lastTab = useRef(tab)
  if (lastTab.current !== tab) {
    lastTab.current = tab
    tabRev.current += 1
  }
  // A page showing a subject that is not the point (the last one looked at,
  // after the point was cleared) does not pin it: Consult asks about the
  // point, never about something the top bar does not name.
  const pins = bound !== null || followed.isPoint
  usePinScope(subject && pins ? { kind: 'target', id: subject, label: name ?? subject } : null, 'subject page', tabRev.current)

  const childProps = useMemo<PanelProps | null>(() => {
    if (!subject) return null
    const registration: PanelRegistration = {
      ...props.registration,
      descriptor_id: subject,
      binding: { ...(props.registration?.binding ?? {}), target_id: subject },
    }
    return { ...props, registration, scope: { ...props.scope, target_id: subject }, initialTab: undefined }
  }, [props, subject])

  const noun = subjectNoun()
  const Tab = SUBJECT_TAB_COMPONENTS[tab]

  return (
    <div className="flex h-full w-full flex-col bg-surf-2" data-testid="subject-page" data-subject={subject ?? ''}>
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">{capitalised(noun)}</span>
        {subject ? (
          <span className="flex min-w-0 items-baseline gap-1.5" data-testid="subject-page-name">
            <span className="truncate text-body text-ink-1">{name ?? humanizeId(subject)}</span>
            {!belowAdmin && name && name !== subject && (
              <span className="truncate font-mono text-label text-ink-3">{subject}</span>
            )}
            {!bound && followed.isPoint && (
              <span className="text-label italic text-ink-3" data-testid="subject-page-following">
                following the selected {noun}
              </span>
            )}
            {!bound && !followed.isPoint && (
              <span className="text-label italic text-ink-3" data-testid="subject-page-not-point" data-why={followed.why ?? ''}>
                {followed.why === 'most' ? `the ${noun} with the most changes` : `the last ${noun} you looked at`} — not the point
              </span>
            )}
          </span>
        ) : null}
        <PanelTabStrip
          tabs={tabs}
          active={tab}
          onChange={(id) => setTab(id as SubjectTab)}
          ariaLabel={`${capitalised(noun)} page`}
          testIdPrefix="subject-tab"
        />
      </div>
      <div className="min-h-0 flex-1">
        {childProps ? (
          <PanelEmbedProvider>
            <Suspense fallback={<div className="p-3 text-xs text-ink-3">Loading tab…</div>}>
              <Tab key={`${subject}#${tab}`} {...childProps} />
            </Suspense>
          </PanelEmbedProvider>
        ) : (
          <p className="p-4 text-xs text-ink-3" data-testid="subject-page-no-subject">
            No {noun} is selected. Choose one from the rail&rsquo;s {PROFILE_UI.subject.plural}, from
            ⌘K <span className="font-mono">@</span>, or from any {noun} chip — this page follows it.
          </p>
        )}
      </div>
    </div>
  )
}
