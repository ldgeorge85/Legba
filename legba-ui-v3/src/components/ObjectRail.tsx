/**
 * ObjectRail — the left rail is OBJECTS, not panels (UI-B1/shell; UI-D1 §2.2, D2).
 *
 * The verb-grouped panel tree (`navGroups.ts`, 26 structural rows) is gone.
 * The rail carries the instance's OBJECTS in six sections, in a fixed order,
 * labelled from the profile (`lib/profileUi.ts`):
 *
 *   1 Reads            — the read index by frame (tier), top floor first (the
 *                        projection's `tiers`, else the packaged profile's)
 *   2 Subjects         — grouped by the frame family's subject groups (the
 *                        projection's `subject_groups`), else by the roster
 *                        leg each subject DECLARES (`groupSubjects`)
 *   3 Units            — the profile's unit roster
 *   4 Sources          — the source roster
 *   5 Cases            — the recorded consult cases (replayable)
 *   6 Queues           — the human queues, with their counts
 *
 * U-2 SHELL: the rail is the OPERATOR side's rail. The reader side has no rail
 * (its subjects are the top bar's switcher, which renders this module's
 * {@link SubjectRoster}). The LAYOUTS block ("Open a workspace…", Save,
 * Restore), the missions and the saved missions are gone with the arrangement
 * machinery they drove (02_UI_TARGET_BRIEF §3, option A).
 *
 * NO PANEL ROWS. The catalog is the top bar's `＋` popover (`CatalogPopover`)
 * and ⌘K `>`. Every click here is an OBJECT action compiled to drive actions
 * (`lib/commands.ts::railActions`) and run through the ONE executor
 * (`drive` → `applyAction`): a subject OPENS ITS READ (X3/D3: the wall scopes
 * to it, it is focused, and its page opens on the brief — `lib/subjectRead.ts`;
 * the descriptor is one click further, the Inspector's "Configuration"), a
 * unit is FOCUSED, a read SCOPES the wall, a mission RUNS, a queue OPENS its
 * surface, a case REPLAYS. An object's drills (its bound reads) open from the
 * object's own `⋯` menu.
 *
 * ONE subject noun on screen: the profile's (`PROFILE_UI.subject`); `target`,
 * `desk` and `country` are code-only aliases (`lib/subjectRead.ts::nounTitle`
 * rewrites a registry title that still spells one).
 *
 * Absence renders as absence: a section whose read failed says
 * "unavailable", a count not yet read is "—", never 0; every count carries its
 * unit and its as-of.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, MoreHorizontal } from 'lucide-react'
import type { Mode, PanelKind } from '@/types'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { drillsOf } from '@/panel-registry/kindPlan'
import { apiGet, fetchJournalProposals } from '@/lib/api'
import { drive } from '@/lib/applyAction'
import { railActions } from '@/lib/commands'
import {
  PLATFORM_NOUNS,
  PROFILE_UI,
  projectionAbsenceText,
  rosterLabel,
  subjectGroupsToList,
  subjectTierIds,
  useProjection,
  useReadTiers,
  type ProfileFrame,
  type ProjectionRead,
} from '@/lib/profileUi'
import { recordedCases, replayRecordedCase } from '@/lib/caseReplay'
import { fetchGoldsetWorksheet, type GoldsetWorksheet } from '@/lib/goldsetModel'
import { humanizeId, rosterTagOf, useSubjectIndex } from '@/lib/deskNames'
import { humanizeAnalystId } from '@/lib/analystNames'
import { relativeTime } from '@/lib/findingsViews'
import type { ReadFindingRow } from '@/lib/assemblyModel'
import type { ConfidenceLevel } from '@/lib/verdictModel'
import { cn } from '@/lib/cn'
import { CONFIDENCE_FILL, useCountryVerdicts } from '@/v4/world/countryVerdicts'
import { useSupplyChainDesks } from '@/v4/world/supplyChainDesks'
import { usePaletteRecords } from '@/components/usePaletteRecords'
import { useConsultSessions } from '@/state/consultSession'
import { pinForSelection, RAIL_PIN_KINDS, usePinScope } from '@/lib/consultContext'
import { useSelection } from '@/state/selection'
import { subjectReadActions } from '@/lib/subjectRead'
import { nounTitle, SUBJECT_NOUN } from '@/lib/subjectNoun'

// ---------------------------------------------------------------------------
// The six sections — fixed order (UI-D1 §2.2).
// ---------------------------------------------------------------------------

export type RailSectionId = 'reads' | 'subjects' | 'units' | 'sources' | 'cases' | 'queues'

/** The order is the contract; the labels come from the profile where it names them. */
export const RAIL_SECTIONS: readonly { id: RailSectionId; label: string }[] = [
  { id: 'reads', label: 'Reads' },
  { id: 'subjects', label: PROFILE_UI.subject.plural },
  { id: 'units', label: PROFILE_UI.unit_noun.plural },
  { id: 'sources', label: 'Sources' },
  { id: 'cases', label: 'Cases' },
  { id: 'queues', label: 'Queues' },
]

const COLLAPSE_KEY = 'legba_rail_collapsed'
/** The two long rosters start folded; the four short sections start open. */
const DEFAULT_COLLAPSED: RailSectionId[] = ['units', 'sources']

function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSE_KEY)
    if (raw == null) return new Set(DEFAULT_COLLAPSED)
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? new Set(arr.filter((x) => typeof x === 'string')) : new Set(DEFAULT_COLLAPSED)
  } catch {
    return new Set(DEFAULT_COLLAPSED)
  }
}

function persistCollapsed(ids: Set<string>) {
  try {
    localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...ids]))
  } catch {
    // a convenience only
  }
}

const CONFIDENCE_LABEL: Record<ConfidenceLevel, string> = {
  high: 'High',
  moderate: 'Moderate',
  low: 'Low',
  unassessed: 'Unverified',
}

function clockAt(ms: number | undefined): string {
  if (!ms) return '—'
  const d = new Date(ms)
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}Z`
}

// ---------------------------------------------------------------------------
// The rail.
// ---------------------------------------------------------------------------

export interface ObjectRailProps {
  mode: Mode
  /**
   * Retired with the rail's own search row (U-2: ⌘K and the ＋ catalog live
   * in the top bar on both sides). Accepted and ignored.
   */
  onOpenPalette?: () => void
}

export function ObjectRail({ mode }: ObjectRailProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => loadCollapsed())
  const toggle = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      persistCollapsed(next)
      return next
    })
  // UI-B6 — the pin hook: a subject, unit or source picked from the rail is
  // what the reader is on, so Consult's ambient pin follows it; any other
  // selection is not a rail object and the rail releases its claim.
  const selection = useSelection((s) => s.selection)
  usePinScope(pinForSelection(selection, RAIL_PIN_KINDS), 'rail')

  const body: Record<RailSectionId, React.ReactElement> = {
    reads: <ReadsSection />,
    subjects: <SubjectRoster />,
    units: <UnitsSection open={!collapsed.has('units')} />,
    sources: <SourcesSection open={!collapsed.has('sources')} />,
    cases: <CasesSection />,
    queues: <QueuesSection mode={mode} />,
  }

  return (
    <aside
      className="w-full bg-surf-base text-ink-1"
      data-testid="object-rail"
    >
      <div className="px-3 py-2 border-b border-line">
        <div className="text-label uppercase tracking-[0.06em] text-ink-3">Operator</div>
        <div className="text-label text-ink-3">objects, rosters and queues</div>
      </div>

      {RAIL_SECTIONS.map((s) => (
        <RailSection
          key={s.id}
          id={s.id}
          title={s.label}
          collapsed={collapsed.has(s.id)}
          onToggle={() => toggle(s.id)}
        >
          {body[s.id]}
        </RailSection>
      ))}
    </aside>
  )
}

// ---------------------------------------------------------------------------
// Chrome.
// ---------------------------------------------------------------------------

function RailSection({
  id,
  title,
  collapsed,
  onToggle,
  children,
}: {
  id: RailSectionId
  title: string
  collapsed: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <section className="py-1" data-testid={`rail-section-${id}`} data-rail-section={id}>
      <button
        type="button"
        data-testid={`rail-section-toggle-${id}`}
        aria-expanded={!collapsed}
        onClick={onToggle}
        className="w-full flex items-center gap-1 px-2 py-1 text-label uppercase tracking-wider text-ink-2 hover:text-ink-1"
      >
        <ChevronRight size={12} className={cn('flex-shrink-0 transition-transform', !collapsed && 'rotate-90')} />
        <span className="flex-1 text-left" data-testid={`rail-section-title-${id}`}>
          {title}
        </span>
      </button>
      {!collapsed && <div className="space-y-px mt-0.5">{children}</div>}
    </section>
  )
}

/** One object row. `object` names WHAT the row is — never a panel. */
function RailRow({
  object,
  testId,
  label,
  hint,
  title,
  swatch,
  onClick,
  menu,
}: {
  object: 'read' | 'subject' | 'unit' | 'source' | 'case' | 'queue'
  testId: string
  label: string
  hint?: string
  title?: string
  swatch?: string
  onClick: () => void
  menu?: React.ReactNode
}) {
  return (
    <li className="group relative flex items-center">
      <button
        type="button"
        data-testid={testId}
        data-rail-row={object}
        onClick={onClick}
        title={title ?? label}
        className="flex-1 min-w-0 flex items-center gap-2 text-left px-3 row-density text-body hover:bg-surf-2 text-ink-2"
      >
        {swatch && <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: swatch }} aria-hidden />}
        <span className="flex-1 truncate">{label}</span>
        {hint && <span className="shrink-0 text-label text-ink-3">{hint}</span>}
      </button>
      {menu}
    </li>
  )
}

function Muted({ children, testId }: { children: React.ReactNode; testId?: string }) {
  return (
    <div className="px-3 py-1 text-label text-ink-3" data-testid={testId}>
      {children}
    </div>
  )
}

/**
 * A drill's menu label in the profile's words: the subject page is "<Noun>
 * page"; any other title has its alias nouns rewritten ("Target Map" →
 * "Desk Map", "Cross-target Analyst" → "Cross-desk Analyst").
 */
export function drillLabel(kind: PanelKind): string {
  if (kind === 'subject.page') return `${SUBJECT_NOUN} page`
  return nounTitle(PANEL_REGISTRY[kind].definition.defaultTitle)
}

/**
 * An object's DRILLS — its bound reads, opened on it. A drill is never in the
 * `＋` catalog (it needs this object to render); this menu is its parent.
 */
function DrillMenu({ object, id, label }: { object: 'subject' | 'unit'; id: string; label: string }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    window.addEventListener('mousedown', onDown)
    return () => window.removeEventListener('mousedown', onDown)
  }, [open])
  const kinds = drillsOf(object)
  const axis = object === 'subject' ? 'target' : 'analyst'
  return (
    <div ref={ref} className="relative shrink-0">
      <button
        type="button"
        data-testid={`rail-drills-${id}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`open a read of ${label}`}
        title={`Open a read of ${label}`}
        onClick={() => setOpen((o) => !o)}
        className="px-1.5 text-ink-3 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-ink-1"
      >
        <MoreHorizontal size={13} aria-hidden />
      </button>
      {open && (
        <ul
          role="menu"
          className="absolute right-0 top-5 z-40 w-48 rounded border border-line bg-surf-1 py-1 shadow-xl"
          data-testid={`rail-drill-menu-${id}`}
        >
          {kinds.map((k) => (
            <li key={k}>
              <button
                type="button"
                role="menuitem"
                data-testid={`rail-drill-${k}`}
                onClick={() => {
                  setOpen(false)
                  drive(railActions.drill(k, { kind: axis, id }))
                }}
                className="w-full px-3 py-1 text-left text-body text-ink-2 hover:bg-surf-2 hover:text-ink-1"
              >
                {drillLabel(k)}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 1 · Reads
// ---------------------------------------------------------------------------

/**
 * How deep each tier's read goes (the Navigator's own depths, so the two share
 * one cache entry) — by the tier's SHAPE, never its producer id: the top floor
 * keeps its last dozen reads; a per-subject tier (a nested frame nothing hangs
 * under — one read per roster subject) reads deep enough to cover the roster;
 * any other tier reads 40.
 */
function tierLimit(tier: ProfileFrame, tiers: readonly ProfileFrame[]): number {
  if (tier.producer === PROFILE_UI.top_floor) return 12
  return subjectTierIds(tiers).has(tier.id) ? 200 : 40
}

function tierLabel(row: ReadFindingRow, top: boolean, frameLabel: string): string {
  if (top) return row.title ?? frameLabel
  const t = row.target_id ?? ''
  return t ? humanizeId(t) : row.title ?? row.id
}

/** A projection section's refusal, named, where a rail section reads it (never a blank). */
function ProjectionNote({ read, what, testId }: { read: ProjectionRead; what: string; testId: string }) {
  const text = projectionAbsenceText(read)
  if (!text) return null
  return (
    <div
      className="px-3 py-0.5 text-label text-ink-3"
      data-testid={testId}
      data-projection-state={read.state === 'absent' ? read.name : read.state}
      title={text}
    >
      {what} · {read.state === 'absent' ? read.name : 'projection not read yet'}
    </div>
  )
}

function freshest(rows: ReadFindingRow[], producer: string, perTarget: boolean): ReadFindingRow[] {
  const mine = rows
    .filter((r) => r.analyst_id === producer)
    .sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))
  if (!perTarget) return mine.slice(0, 1)
  const by = new Map<string, ReadFindingRow>()
  for (const r of mine) {
    const k = r.target_id ?? r.id
    if (!by.has(k)) by.set(k, r)
  }
  return [...by.values()].sort((a, b) => tierLabel(a, false, '').localeCompare(tierLabel(b, false, '')))
}

function ReadsSection() {
  // The tiers: the projection's when the plane answers; the packaged profile's
  // otherwise (first paint never waits) — and the refusal is named.
  const { tiers, read } = useReadTiers()
  return (
    <div>
      {!PROFILE_UI.top_floor && <Muted testId="rail-reads-no-top-floor">{PLATFORM_NOUNS.noTopFloor}</Muted>}
      {tiers.map((t) => (
        // A frame label that spells an alias noun (the exemplar's country frame) reads
        // in the profile's noun — one subject word on screen.
        <ReadTier key={t.id} producer={t.producer!} label={nounTitle(t.label)} limit={tierLimit(t, tiers)} />
      ))}
      <ProjectionNote read={read} what="tiers from the profile export" testId="rail-projection-tiers" />
    </div>
  )
}

function ReadTier({ producer, label, limit }: { producer: string; label: string; limit: number }) {
  const top = PROFILE_UI.top_floor === producer
  const q = useQuery<{ data: ReadFindingRow[] }>({
    queryKey: ['navigator', producer, limit],
    queryFn: () => apiGet<{ data: ReadFindingRow[] }>(`/findings?analyst_id=${producer}&limit=${limit}&fields=summary`),
    refetchInterval: 5 * 60_000,
    staleTime: 60_000,
  })
  const [open, setOpen] = useState(top)
  const rows = useMemo(
    () => freshest(Array.isArray(q.data?.data) ? q.data!.data : [], producer, !top),
    [q.data, producer, top],
  )
  const count = q.data ? `${rows.length} read${rows.length === 1 ? '' : 's'}` : q.isError ? 'unavailable' : '—'
  return (
    <div data-testid={`rail-read-tier-${producer}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="w-full flex items-center gap-1 px-3 row-density text-label text-ink-3 hover:text-ink-1"
      >
        <ChevronRight size={10} className={cn('shrink-0 transition-transform', open && 'rotate-90')} />
        <span className="flex-1 text-left">{label}</span>
        <span data-testid={`rail-read-count-${producer}`}>{count}</span>
      </button>
      {open && (
        <ul className="space-y-px">
          {q.isError && <Muted>reads unavailable — {(q.error as Error)?.message ?? 'error'}</Muted>}
          {q.data && rows.length === 0 && (
            <Muted testId={`rail-read-empty-${producer}`}>no {label.toLowerCase()} read on record</Muted>
          )}
          {rows.map((r) => {
            const name = tierLabel(r, top, label)
            return (
              <RailRow
                key={r.id}
                object="read"
                testId={`rail-read-${r.id}`}
                label={name}
                hint={relativeTime(r.produced_at)}
                title={`${name} — produced ${r.produced_at}`}
                onClick={() => drive(railActions.read(r.id, r.title ?? name))}
              />
            )
          })}
        </ul>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 2 · Subjects — grouped by the profile's frame family
// ---------------------------------------------------------------------------

interface SubjectRow {
  id: string
  /** The subject roster tag it belongs to (`g20`, `watch`, `supply_chain` …). */
  tag: string | null
  name: string
  confidence: ConfidenceLevel | null
  /** Recency stamp for a subject with no composition tier (never a fabricated band). */
  latestAt: string | null
}

/** One heading of the Subjects section and the subjects under it. */
export interface SubjectRailGroup {
  id: string
  label: string
  rows: SubjectRow[]
  /** True for a frame-family group (the rows keep their roster leg in the tooltip). */
  frame: boolean
}

const UNTAGGED = '_untagged'

/** A subject row's tooltip: its name, its band, and — under a frame group — its roster leg. */
function subjectTitle(r: SubjectRow, frame: boolean): string {
  const base = r.confidence ? `${r.name} — ${CONFIDENCE_LABEL[r.confidence]} confidence` : r.name
  return frame && r.tag ? `${base} · ${rosterLabel(r.tag)}` : base
}

/**
 * THE SUBJECTS GROUPING (UI-B2; UI-D1 §2.2). When the projection answers, the
 * subjects group by the FRAME FAMILY's subject groups (its `subject_groups`
 * section: each frame with the target ids it resolves to — the exemplar reads
 * Desks → region → its desks), each group in the plane's order under the
 * frame's own label, and a subject keeps its roster leg's label in its
 * tooltip. Only a frame with its OWN member rule heads a group
 * (`subjectGroupsToList`): a kind-of-kinds (`region`, whose members are its
 * whole subtree) and a frame that resolves no subject (a thematic lane is an
 * analyst) head nothing; a subject is listed once, under the first frame that
 * resolves it. A subject in no
 * frame — and EVERY subject when the plane refuses — groups by the roster leg
 * it declares (`rosters.subject_tags`, the profile's order, the profile's
 * label); a subject on no declared leg is listed under the plural noun alone —
 * never dropped.
 */
export function groupSubjects(rows: SubjectRow[], projected: ProjectionRead): SubjectRailGroup[] {
  const byName = (a: SubjectRow, b: SubjectRow) => a.name.localeCompare(b.name)
  const out: SubjectRailGroup[] = []
  const placed = new Set<string>()
  if (projected.state === 'ok') {
    for (const g of subjectGroupsToList(projected.rows)) {
      const members = new Set(g.members)
      // a subject is listed once, under the first frame that resolves it
      const inGroup = rows.filter((r) => members.has(r.id) && !placed.has(r.id))
      if (inGroup.length === 0) continue
      inGroup.forEach((r) => placed.add(r.id))
      out.push({ id: g.id, label: g.label, rows: [...inGroup].sort(byName), frame: true })
    }
  }
  const order = PROFILE_UI.rosters.subject_tags
  const byTag = new Map<string, SubjectRow[]>()
  for (const r of rows) {
    if (placed.has(r.id)) continue
    const k = r.tag ?? UNTAGGED
    byTag.set(k, [...(byTag.get(k) ?? []), r])
  }
  const keys = [...byTag.keys()].sort((a, b) => {
    const ia = order.indexOf(a)
    const ib = order.indexOf(b)
    return (ia < 0 ? order.length : ia) - (ib < 0 ? order.length : ib) || a.localeCompare(b)
  })
  for (const k of keys) {
    out.push({
      id: k,
      label: k === UNTAGGED ? PROFILE_UI.subject.plural : rosterLabel(k),
      rows: [...byTag.get(k)!].sort(byName),
      frame: false,
    })
  }
  return out
}

/**
 * THE SUBJECT ROSTER — grouped, with each subject's band — exported so the
 * reader side's top-bar switcher shows the same list the rail does. A click
 * opens the subject's read (the Subject surface follows it); `onPicked` lets a
 * popover close behind the click.
 */
export function SubjectRoster({ onPicked }: { onPicked?: () => void } = {}) {
  const { verdicts, isLoading: vLoading } = useCountryVerdicts()
  const { desks, isLoading: dLoading } = useSupplyChainDesks()
  // The subject roster: a subject's declared name and its roster leg.
  const subjects = useSubjectIndex()
  const projected = useProjection('subject_groups')
  const rows: SubjectRow[] = useMemo(() => {
    const out: SubjectRow[] = []
    for (const v of verdicts.values()) {
      out.push({
        id: v.targetId,
        tag: rosterTagOf(v.targetId, subjects),
        name: humanizeId(v.targetId),
        confidence: v.verdict.confidence,
        latestAt: null,
      })
    }
    for (const d of desks) {
      out.push({
        id: d.targetId,
        tag: rosterTagOf(d.targetId, subjects),
        name: humanizeId(d.targetId),
        confidence: null,
        latestAt: d.latestFindingAt,
      })
    }
    return out
  }, [verdicts, desks, subjects])

  const groups = useMemo(() => groupSubjects(rows, projected), [rows, projected])

  const loading = (vLoading || dLoading) && rows.length === 0
  if (loading) return <Muted testId="rail-subjects-loading">loading {PROFILE_UI.subject.plural.toLowerCase()}…</Muted>
  if (rows.length === 0) {
    return <Muted testId="rail-subjects-empty">no assessed {PROFILE_UI.subject.plural.toLowerCase()} yet</Muted>
  }
  return (
    <div>
      <ProjectionNote read={projected} what="grouped by roster" testId="rail-projection-subject_groups" />
      {groups.map((g) => (
        <div key={g.id} data-testid={`rail-frame-${g.id}`}>
          <div className="mt-1 px-3 py-0.5 text-label uppercase tracking-wider text-ink-3">
            {g.label} <span className="normal-case">({g.rows.length})</span>
          </div>
          <ul className="space-y-px">
            {g.rows.map((r) => (
              <RailRow
                key={r.id}
                object="subject"
                testId={`rail-subject-${r.id}`}
                label={r.name}
                swatch={r.confidence ? CONFIDENCE_FILL[r.confidence] : undefined}
                hint={r.confidence ? CONFIDENCE_LABEL[r.confidence] : r.latestAt ? relativeTime(r.latestAt) : undefined}
                title={subjectTitle(r, g.frame)}
                onClick={() => {
                  drive(subjectReadActions(r.id, r.name))
                  onPicked?.()
                }}
                menu={<DrillMenu object="subject" id={r.id} label={r.name} />}
              />
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 3 · Units  ·  4 · Sources — the registry rosters
// ---------------------------------------------------------------------------

function FilteredRoster({
  object,
  open,
  recordKind,
  noun,
  render,
}: {
  object: 'unit' | 'source'
  open: boolean
  recordKind: 'analyst' | 'source'
  noun: string
  render: (id: string, label: string) => React.ReactElement
}) {
  const { records, isLoading } = usePaletteRecords(open)
  const [filter, setFilter] = useState('')
  const mine = useMemo(
    () => records.filter((r) => r.recordKind === recordKind).sort((a, b) => a.id.localeCompare(b.id)),
    [records, recordKind],
  )
  const q = filter.trim().toLowerCase()
  const visible = q ? mine.filter((r) => `${r.id} ${r.label}`.toLowerCase().includes(q)) : mine
  if (isLoading && mine.length === 0) return <Muted>loading {noun}…</Muted>
  if (mine.length === 0) return <Muted testId={`rail-${object}-empty`}>no {noun} on record</Muted>
  return (
    <div>
      {mine.length > 8 && (
        <div className="px-3 pb-1">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder={`filter ${noun}…`}
            spellCheck={false}
            data-testid={`rail-filter-${object}`}
            className="w-full rounded border border-line bg-surf-2 px-2 py-1 text-body text-ink-2 placeholder:text-ink-3 outline-none focus:border-line-strong"
          />
        </div>
      )}
      {visible.length === 0 && <Muted>no {noun} match</Muted>}
      <ul className="space-y-px">{visible.map((r) => render(r.id, r.label))}</ul>
    </div>
  )
}

function UnitsSection({ open }: { open: boolean }) {
  const noun = PROFILE_UI.unit_noun.plural.toLowerCase()
  const roster = PROFILE_UI.rosters.units
  if (roster) {
    // The profile's own unit roster — no fetch, and no composition producers
    // dressed up as units.
    return (
      <ul className="space-y-px" data-testid="rail-unit-roster">
        {roster.map((id) => {
          const name = humanizeAnalystId(id)
          return (
            <RailRow
              key={id}
              object="unit"
              testId={`rail-unit-${id}`}
              label={name}
              title={`${name} · ${id}`}
              onClick={() => drive(railActions.unit(id, name))}
              menu={<DrillMenu object="unit" id={id} label={name} />}
            />
          )
        })}
      </ul>
    )
  }
  // No roster declared: the registry's analysts, said as such.
  return (
    <FilteredRoster
      object="unit"
      open={open}
      recordKind="analyst"
      noun={noun}
      render={(id) => {
        const name = humanizeAnalystId(id)
        return (
          <RailRow
            key={id}
            object="unit"
            testId={`rail-unit-${id}`}
            label={name}
            title={`${name} · ${id}`}
            onClick={() => drive(railActions.unit(id, name))}
            menu={<DrillMenu object="unit" id={id} label={name} />}
          />
        )
      }}
    />
  )
}

function SourcesSection({ open }: { open: boolean }) {
  return (
    <FilteredRoster
      object="source"
      open={open}
      recordKind="source"
      noun="sources"
      render={(id, label) => (
        <RailRow
          key={id}
          object="source"
          testId={`rail-source-${id}`}
          label={label === id ? id : label}
          title={`${label} · ${id}`}
          onClick={() => drive(railActions.source(id, label))}
        />
      )}
    />
  )
}

// ---------------------------------------------------------------------------
// 5 · Cases
// ---------------------------------------------------------------------------

function CasesSection() {
  const panels = useConsultSessions((s) => s.panels)
  const cases = useMemo(() => recordedCases(panels), [panels])
  if (cases.length === 0) {
    return <Muted testId="rail-cases-empty">no recorded case in this session&rsquo;s consult turns</Muted>
  }
  return (
    <ul className="space-y-px">
      {cases.map((c) => (
        <RailRow
          key={`${c.panelId}:${c.turnIndex}`}
          object="case"
          testId={`rail-case-${c.turnIndex}`}
          label={c.label}
          hint={`${c.record.actions.length} step${c.record.actions.length === 1 ? '' : 's'}`}
          title={`Replay — recorded ${c.record.as_of ?? '(as-of not recorded)'}`}
          onClick={() => replayRecordedCase(c)}
        />
      ))}
    </ul>
  )
}

// ---------------------------------------------------------------------------
// 6 · Queues — the human queues, counted
// ---------------------------------------------------------------------------

function modeShips(kind: PanelKind, mode: Mode): boolean {
  const def = PANEL_REGISTRY[kind]?.definition
  return !!def && (def.modes.length === 0 || def.modes.includes(mode))
}

function QueuesSection({ mode }: { mode: Mode }) {
  // The same query keys and functions the queue panels use — one cache entry.
  const proposals = useQuery({
    queryKey: ['journal_proposals', 'pending'],
    queryFn: () => fetchJournalProposals({ status: 'pending', limit: 100 }),
    refetchInterval: 60_000,
    enabled: modeShips('system.journal_gate', mode),
  })
  const grading = useQuery<GoldsetWorksheet>({
    queryKey: ['goldset-worksheet'],
    queryFn: fetchGoldsetWorksheet,
    refetchInterval: 300_000,
    enabled: modeShips('system.goldset', mode),
  })
  const candidates = useQuery<unknown[]>({
    queryKey: ['optimizer-candidates', 'pending'],
    queryFn: () => apiGet<unknown[]>(`/v3/optimizer/candidates?state=pending`),
    refetchInterval: 30_000,
    enabled: modeShips('system.optimizer', mode),
  })

  const queues: { kind: PanelKind; label: string; count: string; asOf: string }[] = []
  if (modeShips('system.journal_gate', mode)) {
    // Defensive on the envelope: a body that is not the route's shape is
    // "unavailable", never a count.
    const list = proposals.data?.proposals
    const n = Array.isArray(list) ? list.length : null
    queues.push({
      kind: 'system.journal_gate',
      label: 'Journal proposals',
      count:
        n == null
          ? proposals.isError || proposals.data
            ? 'unavailable'
            : '—'
          : `${n >= 100 ? '100+' : n} pending`,
      asOf: clockAt(proposals.dataUpdatedAt),
    })
  }
  if (modeShips('system.goldset', mode)) {
    const w = grading.data
    const measured = !!w && typeof w.sample_size === 'number' && typeof w.labeled_count === 'number'
    queues.push({
      kind: 'system.goldset',
      label: 'Weekly grading',
      count: measured
        ? `${Math.max(0, w!.sample_size - w!.labeled_count)} to grade`
        : grading.isError || w
          ? 'unavailable'
          : '—',
      asOf: measured && typeof w!.week === 'string' ? `week ${w!.week}` : clockAt(grading.dataUpdatedAt),
    })
  }
  if (modeShips('system.optimizer', mode)) {
    const n = Array.isArray(candidates.data) ? candidates.data.length : null
    queues.push({
      kind: 'system.optimizer',
      label: 'Optimizer candidates',
      count: n == null ? (candidates.isError || candidates.data ? 'unavailable' : '—') : `${n} pending`,
      asOf: clockAt(candidates.dataUpdatedAt),
    })
  }
  if (queues.length === 0) return <Muted testId="rail-queues-none">no human queue ships in the {mode} mode</Muted>
  return (
    <ul className="space-y-px">
      {queues.map((q) => (
        <RailRow
          key={q.kind}
          object="queue"
          testId={`rail-queue-${q.kind}`}
          label={q.label}
          hint={q.count}
          title={`${q.label}: ${q.count} · as of ${q.asOf}`}
          onClick={() => drive(railActions.queue(q.kind))}
        />
      ))}
    </ul>
  )
}
