/**
 * The Report/Journal Navigator — the workstation's front door.
 *
 * It navigates **products**, not panels. The panel catalog stays behind ⌘K and
 * the sidebar; this rail lists what the machine PUBLISHED today — the world
 * read and its Assessment voice, the regional rollups, the country desks, the
 * thematic lanes, the journal consolidation — so the first thing the operator
 * sees on the landing is the day's intelligence rather than a list of surfaces.
 *
 * ## No new endpoint
 *
 * Every section below is `GET /api/v1/findings?analyst_id=…&limit=…`, the exact
 * call `v4/read/MorningRead.tsx:94` and `v4/world/countryVerdicts.ts:114`
 * already make, plus `fetchJournalSummary()` for the journal section. A report
 * IS a `kind='finding'` row whose `analyst_id` is one of the composition
 * producers, so the front door costs zero backend work.
 *
 * ## Clicking a row performs exactly two writes and nothing else
 *
 *     useScope.getState().setScope(scopeFromRow(row))          // the world
 *     selectRow('report', row.id, title, { origin: 'navigator' })  // the record
 *
 * Everything downstream is SUBSCRIPTION — that is the whole point of the
 * scope/focus split (`state/scope.ts`). The Navigator knows nothing about the
 * feed, the map, the timeline, the graph or the consult; it moves two stores
 * and they follow. Adding a following panel never touches this file.
 *
 * ## Why scope AND focus on one click
 *
 * They are different lifetimes, and the click asserts both: "the wall is now
 * about this report" (scope, which survives every subsequent row click) and "I
 * am reading this record" (focus, which the Inspector renders and which the
 * next row click replaces). Writing only one of them reproduces exactly the
 * defect the design was written to remove.
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { apiGet, fetchJournalSummary, type JournalEntrySummary } from '@/lib/api'
import { projectAssembly, type ReadFindingRow } from '@/lib/assemblyModel'
import { countryNameForTargetId, humanizeId, thematicDeskName } from '@/lib/deskNames'
import { scopeFromJournalEntry, scopeFromRow } from '@/lib/scopeFromReport'
import { useScope } from '@/state/scope'
import { selectRow, useSelection } from '@/state/selection'
import { SeverityDot } from '@/components/SeverityBadge'
import type { Severity } from '@/v4/world/types'
import type { PanelProps } from '@/types'

/** The world spine and its fenced interpretive channel (D-4 / D-6). */
const SPINE_ANALYST = 'world_assessor'
const ASSESSMENT_ANALYST = 'world_assessment'
const REGION_ANALYST = 'region_composition'
const COUNTRY_ANALYST = 'country_composition'

/** Prior-run depth, matching `MorningRead.tsx`'s own history list. */
const RUN_LIMIT = 12
/** One page of desks is plenty for a rail; the roster is ~60 rows. */
const DESK_LIMIT = 200
const REGION_LIMIT = 40
const JOURNAL_LIMIT = 8

interface FindingsResponse {
  data: ReadFindingRow[]
}

function useProducer(analystId: string, limit: number) {
  return useQuery<FindingsResponse>({
    queryKey: ['navigator', analystId, limit],
    refetchInterval: 5 * 60_000,
    staleTime: 60_000,
    queryFn: () => apiGet<FindingsResponse>(`/findings?analyst_id=${analystId}&limit=${limit}`),
  })
}

/** Newest-first, and only the rows this producer actually wrote. */
function rowsOf(resp: FindingsResponse | undefined, analystId: string): ReadFindingRow[] {
  const rows = (resp?.data ?? []).filter((r) => r.analyst_id === analystId)
  return [...rows].sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))
}

/** Freshest row per desk — the roster view, not the run log. */
function freshestPerTarget(rows: ReadFindingRow[]): ReadFindingRow[] {
  const byTarget = new Map<string, ReadFindingRow>()
  for (const r of rows) {
    const key = r.target_id ?? r.id
    const prev = byTarget.get(key)
    if (prev && Date.parse(prev.produced_at) >= Date.parse(r.produced_at)) continue
    byTarget.set(key, r)
  }
  return [...byTarget.values()].sort((a, b) =>
    (deskLabel(a) ?? '').localeCompare(deskLabel(b) ?? ''),
  )
}

/** A desk's human name — country, thematic lane, or a humanized id. */
function deskLabel(row: ReadFindingRow): string {
  const t = row.target_id ?? ''
  return countryNameForTargetId(t) ?? thematicDeskName(t) ?? (t ? humanizeId(t) : '—')
}

/** A thematic lane is a supply-chain / flow desk, not a country desk. */
function isThematic(row: ReadFindingRow): boolean {
  const t = row.target_id ?? ''
  return /^(lane_|flow_)/.test(t) || (thematicDeskName(t) != null && countryNameForTargetId(t) == null)
}

/**
 * Compact age. Deliberately coarse: the rail answers "is today's read here
 * yet", not "how many minutes ago" — a precise number invites reading it as a
 * freshness guarantee the row does not carry.
 */
export function ageLabel(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '—'
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return '—'
  const h = (now - t) / 3600_000
  if (h < 1) return 'now'
  if (h < 24) return `${Math.floor(h)}h`
  const d = Math.floor(h / 24)
  return d < 30 ? `${d}d` : `${Math.floor(d / 30)}mo`
}

function asSeverity(s: string | null | undefined): Severity | null {
  return s === 'critical' || s === 'high' || s === 'medium' || s === 'low' ? s : null
}

interface RowProps {
  row: ReadFindingRow
  /** Section-local secondary label — the desk, the region, the tier. */
  sub?: string
  active: boolean
  onPick: (row: ReadFindingRow) => void
}

/**
 * One product row.
 *
 * `▸ N desks` comes off `blocks.length` — a count the record carries, never a
 * derived one. A row whose payload predates the assembly shows no count rather
 * than a zero, because a zero on this rail reads as "composed from nothing".
 */
function NavigatorRow({ row, sub, active, onPick }: RowProps) {
  const assembly = useMemo(() => projectAssembly(row), [row])
  const sev = asSeverity(row.severity)
  const blocks = assembly?.blocks.length ?? null
  const asOf = assembly?.as_of ?? row.produced_at
  return (
    <li>
      <button
        type="button"
        onClick={() => onPick(row)}
        aria-current={active ? 'true' : undefined}
        data-testid="navigator-row"
        data-row-id={row.id}
        className={`flex w-full items-baseline gap-2 rounded px-2 py-1 text-left text-xs ${
          active ? 'bg-surf-3 text-ink-1' : 'text-ink-2 hover:bg-surf-1'
        }`}
      >
        {sev ? (
          <SeverityDot severity={sev} className="h-2 w-2 shrink-0" />
        ) : (
          <span className="inline-block h-2 w-2 shrink-0" aria-hidden />
        )}
        <span className="min-w-0 flex-1 truncate">{sub ?? row.title ?? '(untitled)'}</span>
        {blocks != null && (
          <span className="shrink-0 font-mono text-[10px] text-ink-3" title={`${blocks} desks carried`}>
            ▸{blocks}
          </span>
        )}
        <span className="shrink-0 font-mono text-[10px] text-ink-3">{ageLabel(asOf)}</span>
      </button>
    </li>
  )
}

function Section({
  label,
  count,
  children,
  defaultOpen = true,
}: {
  label: string
  count?: number
  children: React.ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className="border-b border-line py-1" data-testid={`navigator-section-${label.toLowerCase()}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1 px-2 py-1 text-label uppercase tracking-wider text-ink-3 hover:text-ink-2"
      >
        <span className="w-3 shrink-0">{open ? '▾' : '▸'}</span>
        <span className="flex-1 text-left">{label}</span>
        {count != null && <span className="font-mono text-[10px]">{count}</span>}
      </button>
      {open && <ul className="space-y-0.5 pb-1">{children}</ul>}
    </section>
  )
}

export default function NavigatorPanel(_props: PanelProps) {
  void _props
  const scope = useScope((s) => s.scope)
  const setScope = useScope((s) => s.setScope)
  const selection = useSelection((s) => s.selection)

  const spineQ = useProducer(SPINE_ANALYST, RUN_LIMIT)
  const assessQ = useProducer(ASSESSMENT_ANALYST, RUN_LIMIT)
  const regionQ = useProducer(REGION_ANALYST, REGION_LIMIT)
  const deskQ = useProducer(COUNTRY_ANALYST, DESK_LIMIT)
  const journalQ = useQuery({
    queryKey: ['navigator-journal', JOURNAL_LIMIT],
    staleTime: 60_000,
    queryFn: () => fetchJournalSummary({ limit: JOURNAL_LIMIT }),
  })

  const spineRuns = useMemo(() => rowsOf(spineQ.data, SPINE_ANALYST), [spineQ.data])
  const assessRuns = useMemo(() => rowsOf(assessQ.data, ASSESSMENT_ANALYST), [assessQ.data])
  const regions = useMemo(
    () => freshestPerTarget(rowsOf(regionQ.data, REGION_ANALYST)),
    [regionQ.data],
  )
  const desks = useMemo(() => freshestPerTarget(rowsOf(deskQ.data, COUNTRY_ANALYST)), [deskQ.data])
  const countries = useMemo(() => desks.filter((r) => !isThematic(r)), [desks])
  const thematic = useMemo(() => desks.filter(isThematic), [desks])

  /**
   * THE two writes. Nothing else happens here — no navigation, no panel open,
   * no query invalidation. Everything the click does downstream is a
   * subscription to one of these two stores.
   */
  function pick(row: ReadFindingRow) {
    setScope(scopeFromRow(row, 'navigator'))
    selectRow('report', row.id, row.title ?? undefined, {
      origin: 'navigator',
      preview: {
        title: row.title ?? undefined,
        body: typeof row.body === 'string' ? row.body : undefined,
        severity: row.severity ?? null,
        analystId: row.analyst_id ?? null,
        targetId: row.target_id ?? null,
        ts: Date.parse(row.produced_at) || undefined,
      },
    })
  }

  function pickJournal(entry: JournalEntrySummary) {
    setScope(scopeFromJournalEntry(entry.id, entry.title, 'navigator'))
    selectRow('journal_entry', entry.id, entry.title, { origin: 'navigator' })
  }

  const activeId = scope?.id ?? selection?.id ?? null

  /**
   * Prior runs of whatever is scoped — the `RUN_LIMIT = 12` history the Morning
   * Read already keeps, lifted onto the rail so it applies to every product and
   * not only the world read.
   */
  const history = useMemo(() => {
    if (!scope || scope.kind !== 'report') return []
    const all = [...spineRuns, ...assessRuns, ...rowsOf(regionQ.data, REGION_ANALYST), ...rowsOf(deskQ.data, COUNTRY_ANALYST)]
    const current = all.find((r) => r.id === scope.id)
    if (!current) return []
    return all
      .filter(
        (r) =>
          r.id !== current.id &&
          r.analyst_id === current.analyst_id &&
          (r.target_id ?? null) === (current.target_id ?? null),
      )
      .slice(0, RUN_LIMIT)
  }, [scope, spineRuns, assessRuns, regionQ.data, deskQ.data])

  const loading =
    spineQ.isLoading && assessQ.isLoading && regionQ.isLoading && deskQ.isLoading

  return (
    <div className="flex h-full flex-col overflow-y-auto text-xs" data-testid="navigator">
      {loading && (
        <div className="px-2 py-3 text-ink-3" data-testid="navigator-loading">
          Loading today&rsquo;s reads…
        </div>
      )}

      <Section label="Today" count={spineRuns.length > 0 ? 1 : 0}>
        {spineRuns[0] ? (
          <NavigatorRow
            row={spineRuns[0]}
            sub={spineRuns[0].title ?? 'World read'}
            active={activeId === spineRuns[0].id}
            onPick={pick}
          />
        ) : (
          <li className="px-2 py-1 text-ink-3">No world read published yet.</li>
        )}
        {assessRuns[0] && (
          <NavigatorRow
            row={assessRuns[0]}
            sub={assessRuns[0].title ?? 'Assessment'}
            active={activeId === assessRuns[0].id}
            onPick={pick}
          />
        )}
      </Section>

      <Section label="Regions" count={regions.length}>
        {regions.length === 0 && <li className="px-2 py-1 text-ink-3">No regional rollups.</li>}
        {regions.map((r) => (
          <NavigatorRow
            key={r.id}
            row={r}
            sub={deskLabel(r)}
            active={activeId === r.id}
            onPick={pick}
          />
        ))}
      </Section>

      <Section label="Countries" count={countries.length} defaultOpen={false}>
        {countries.map((r) => (
          <NavigatorRow
            key={r.id}
            row={r}
            sub={deskLabel(r)}
            active={activeId === r.id}
            onPick={pick}
          />
        ))}
      </Section>

      <Section label="Thematic" count={thematic.length} defaultOpen={false}>
        {thematic.map((r) => (
          <NavigatorRow
            key={r.id}
            row={r}
            sub={deskLabel(r)}
            active={activeId === r.id}
            onPick={pick}
          />
        ))}
      </Section>

      <Section label="Journal" count={journalQ.data?.entries.length}>
        {journalQ.data?.consolidation && (
          <li>
            <button
              type="button"
              onClick={() => pickJournal(journalQ.data!.consolidation!)}
              aria-current={activeId === journalQ.data.consolidation.id ? 'true' : undefined}
              data-testid="navigator-journal-row"
              className={`flex w-full items-baseline gap-2 rounded px-2 py-1 text-left text-xs ${
                activeId === journalQ.data.consolidation.id
                  ? 'bg-surf-3 text-ink-1'
                  : 'text-ink-2 hover:bg-surf-1'
              }`}
            >
              <span className="min-w-0 flex-1 truncate">
                {journalQ.data.consolidation.title}
              </span>
              <span className="shrink-0 font-mono text-[10px] text-ink-3">
                {ageLabel(journalQ.data.consolidation.produced_at)}
              </span>
            </button>
          </li>
        )}
        {(journalQ.data?.entries ?? []).map((e) => (
          <li key={e.id}>
            <button
              type="button"
              onClick={() => pickJournal(e)}
              aria-current={activeId === e.id ? 'true' : undefined}
              data-testid="navigator-journal-row"
              className={`flex w-full items-baseline gap-2 rounded px-2 py-1 text-left text-xs ${
                activeId === e.id ? 'bg-surf-3 text-ink-1' : 'text-ink-2 hover:bg-surf-1'
              }`}
            >
              <span className="min-w-0 flex-1 truncate">{e.title}</span>
              <span className="shrink-0 font-mono text-[10px] text-ink-3">
                {ageLabel(e.produced_at)}
              </span>
            </button>
          </li>
        ))}
      </Section>

      {history.length > 0 && (
        <Section label="History" count={history.length} defaultOpen={false}>
          {history.map((r) => (
            <NavigatorRow
              key={r.id}
              row={r}
              sub={r.produced_at.slice(0, 16).replace('T', ' ')}
              active={activeId === r.id}
              onPick={pick}
            />
          ))}
        </Section>
      )}
    </div>
  )
}
