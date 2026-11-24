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
import { useSelection } from '@/state/selection'
import { useScope } from '@/state/scope'
import { usePinScope } from '@/lib/consultContext'
import { subjectName, useSubjectIndex } from '@/lib/deskNames'
import { PROFILE_UI } from '@/lib/profileUi'
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

/** The subject a BOUND page was opened on, or null for the following page. */
export function boundSubjectOf(props: Pick<PanelProps, 'scope' | 'registration'>): string | null {
  const fromScope = props.scope?.target_id
  if (fromScope) return fromScope
  const bound = props.registration?.binding?.target_id
  return typeof bound === 'string' && bound ? bound : null
}

/**
 * The subject an UNBOUND page follows: the selected subject, else the wall
 * scope's; the last one is kept when the reader drills off it.
 */
function useFollowedSubject(enabled: boolean): string | null {
  const selection = useSelection((s) => s.selection)
  const scope = useScope((s) => s.scope)
  const candidate =
    selection?.kind === 'target' ? selection.id : scope?.kind === 'target' ? scope.id : null
  const [kept, setKept] = useState<string | null>(candidate)
  useEffect(() => {
    if (enabled && candidate) setKept(candidate)
  }, [enabled, candidate])
  return enabled ? (candidate ?? kept) : null
}

function capitalised(word: string): string {
  return word ? word.charAt(0).toUpperCase() + word.slice(1) : word
}

export default function SubjectPage(props: PanelProps) {
  const bound = boundSubjectOf(props)
  const followed = useFollowedSubject(bound === null)
  const subject = bound ?? followed
  const index = useSubjectIndex()
  const name = useMemo(() => (subject ? subjectName(subject, index) : null), [subject, index])

  // A tab the mode does not ship (Signals, Sources in `cis`) is not offered,
  // and a request for one falls back to the Brief, never an empty surface.
  const tabs = useMemo(() => subjectTabsFor(props.mode), [props.mode])
  const [tab, setTab] = useMergedTab<SubjectTab>(subjectPageKey(bound), props.initialTab, tabs, SUBJECT_DEFAULT_TAB)

  // THE PIN (UI-B6): the page pins its subject, re-asserted on every tab
  // change so a drill on one tab never outlives a move to another.
  const tabRev = useRef(0)
  const lastTab = useRef(tab)
  if (lastTab.current !== tab) {
    lastTab.current = tab
    tabRev.current += 1
  }
  usePinScope(subject ? { kind: 'target', id: subject, label: name ?? subject } : null, 'subject page', tabRev.current)

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
            <span className="truncate text-body text-ink-1">{name ?? subject}</span>
            {name && name !== subject && (
              <span className="truncate font-mono text-label text-ink-3">{subject}</span>
            )}
            {!bound && (
              <span className="text-label italic text-ink-3" data-testid="subject-page-following">
                following the selected {noun}
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
