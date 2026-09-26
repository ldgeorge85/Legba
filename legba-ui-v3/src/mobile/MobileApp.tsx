/**
 * MobileApp — the whole phone surface: a navigator, a report, and a Consult.
 *
 * ── ONE REQUEST BUYS THE WHOLE SESSION ────────────────────────────────────
 * `/api/v1/findings` returns each row's full `data` payload, and a read's
 * payload already contains its blocks, their cited heads and those heads'
 * signals. So the single list request that paints the navigator ALSO contains
 * every report body the operator can reach from it: opening a report costs no
 * network at all, which on a train is the difference between a surface that
 * works and one that spins.
 *
 * The cost of that bargain is an unavoidably fat list response — `/findings`
 * has no `fields=summary` weight the way `/journal` does. That gap is named
 * precisely in the build report rather than worked around with a guessed
 * parameter.
 *
 * ── TELEMETRY IS THE SAME INSTRUMENT ──────────────────────────────────────
 * The read-event vocabulary is closed (a Postgres CHECK behind migration 0189)
 * and `ReadEvent` has no `surface` column, so a `surface: 'mobile'` field would
 * be dropped by the client before it ever reached the wire. The honest carrier
 * is the one free-text dimension the schema already has: `workspace`, set to
 * `mobile` for this entry point. Every event this surface emits is otherwise
 * byte-identical to the workstation's, so the operator's morning reading counts
 * once, in one place, whichever screen they read it on.
 */

import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet, fetchJournalEntry, fetchJournalSummary } from '@/lib/api'
import { projectAssembly, type ReadFindingRow } from '@/lib/assemblyModel'
import {
  emitRead,
  installReadTelemetryLifecycle,
  setTelemetryWorkspace,
} from '@/lib/readTelemetry'
import {
  daysPresent,
  READ_ANALYST_CSV,
  TIER_LABEL,
  toReportRow,
  tierOf,
  type ReportRow,
} from './mobileModel'
import {
  cachedAtLabel,
  loadReport,
  loadReportsList,
  saveReport,
  saveReportsList,
} from './mobileCache'
import { useSelection, useSelectionActions } from './useUrlState'
import ReportsList from './components/ReportsList'
import ReportView from './components/ReportView'
import JournalView from './components/JournalView'
import ConsultSheet from './components/ConsultSheet'
import type { PinnedEntity, PinnedReport } from './mobileConsult'

/**
 * How many read rows the navigator asks for.
 *
 * A single day is roughly one world read, one assessment, five regions and up
 * to ~32 countries, so 80 covers about two days of every tier — enough for the
 * "yesterday" step without a second request, and the ceiling on how much a
 * cold open costs.
 */
const LIST_LIMIT = 80

interface FindingsResponse {
  data: ReadFindingRow[]
}

/** The surface's identity in the read-event stream. See the module header. */
const MOBILE_WORKSPACE = 'mobile'

export default function MobileApp() {
  const selection = useSelection()
  const { openReport, openJournal, closeReport, selectEntity, setDay } = useSelectionActions()

  useEffect(() => {
    setTelemetryWorkspace(MOBILE_WORKSPACE)
    emitRead('workspace_open', { workspace: MOBILE_WORKSPACE })
    return installReadTelemetryLifecycle()
  }, [])

  const reads = useQuery<FindingsResponse>({
    queryKey: ['mobile-reads'],
    refetchInterval: 5 * 60_000,
    queryFn: () =>
      apiGet<FindingsResponse>(
        `/findings?analyst_id_in=${encodeURIComponent(READ_ANALYST_CSV)}&limit=${LIST_LIMIT}`,
      ),
  })

  const journal = useQuery({
    queryKey: ['mobile-journal'],
    refetchInterval: 15 * 60_000,
    queryFn: () => fetchJournalSummary({ limit: 1, kind: ['consolidation'] }),
  })

  // The consolidation's body is fetched only when the operator opens it — the
  // summary weight is what the navigator row needs, and a journal body is long.
  const journalEntry = useQuery({
    queryKey: ['mobile-journal-entry', selection.journal],
    enabled: selection.journal != null,
    queryFn: () => fetchJournalEntry(selection.journal as string),
  })

  // ── the navigator rows, live or cached ────────────────────────────────────
  const liveRows: ReportRow[] | null = useMemo(() => {
    const data = reads.data?.data
    if (!data) return null
    return data
      .map((row) => toReportRow(row, projectAssembly(row)))
      .filter((r): r is ReportRow => r !== null)
      .sort((a, b) => b.producedAt.localeCompare(a.producedAt))
  }, [reads.data])

  useEffect(() => {
    if (liveRows && liveRows.length > 0) saveReportsList(liveRows)
  }, [liveRows])

  const cachedList = useMemo(() => (liveRows ? null : loadReportsList()), [liveRows])
  const rows = liveRows ?? cachedList?.rows ?? []
  const listCachedAt = liveRows ? null : cachedList ? cachedAtLabel(cachedList.savedAt) : null

  const days = useMemo(() => daysPresent(rows), [rows])
  const activeDay = selection.day ?? days[0] ?? null

  // ── the selected report, from the list response or the cache ──────────────
  const liveRow = useMemo(() => {
    if (!selection.report) return null
    return reads.data?.data.find((r) => r.id === selection.report) ?? null
  }, [reads.data, selection.report])

  useEffect(() => {
    if (liveRow) saveReport(liveRow)
  }, [liveRow])

  const cachedRow = useMemo(
    () => (selection.report && !liveRow ? loadReport(selection.report) : null),
    [selection.report, liveRow],
  )
  const activeRow = liveRow ?? cachedRow?.row ?? null
  const reportCachedAt = liveRow ? null : cachedRow ? cachedAtLabel(cachedRow.savedAt) : null

  // `brief_read` is the wager's own metric — it must fire for a report the
  // operator actually reached, and exactly once per report (the store's dedupe
  // window covers a StrictMode double-mount).
  useEffect(() => {
    if (activeRow) {
      emitRead('brief_read', {
        subjectKind: 'finding',
        subjectId: activeRow.id,
        workspace: MOBILE_WORKSPACE,
      })
    }
  }, [activeRow])

  // ── the pinned context handed to the Consult ──────────────────────────────
  const pinnedReport: PinnedReport | null = useMemo(() => {
    if (!activeRow) return null
    const listRow = rows.find((r) => r.id === activeRow.id)
    const tier = tierOf(activeRow)
    return {
      id: activeRow.id,
      title: activeRow.title ?? '',
      tier: tier ? TIER_LABEL[tier] : 'Read',
      target: listRow?.target ?? activeRow.target_id ?? 'World',
      bluf: listRow?.leadThread ?? '',
      producedAt: activeRow.produced_at,
    }
  }, [activeRow, rows])

  const pinnedEntity: PinnedEntity | null = useMemo(() => {
    if (!selection.entity || !activeRow) return null
    const assembly = projectAssembly(activeRow)
    const block = assembly?.blocks.find((b) => b.target_id === selection.entity)
    return { id: selection.entity, name: block?.target_name ?? selection.entity }
  }, [selection.entity, activeRow])

  const consolidation = journal.data?.consolidation
    ? {
        id: journal.data.consolidation.id,
        title: journal.data.consolidation.title,
        producedAt: journal.data.consolidation.produced_at,
      }
    : null

  return (
    <div className="m-app">
      {selection.journal ? (
        journalEntry.data ? (
          <JournalView entry={journalEntry.data} onBack={closeReport} />
        ) : (
          <div className="m-screen">
            <button type="button" className="m-back" onClick={closeReport}>
              ‹ Reports
            </button>
            <p className="m-empty">
              {journalEntry.isLoading ? 'Loading consolidation…' : 'Consolidation unavailable.'}
            </p>
          </div>
        )
      ) : selection.report && activeRow ? (
        <ReportView
          row={activeRow}
          entityId={selection.entity}
          onSelectEntity={selectEntity}
          onBack={closeReport}
          cachedAt={reportCachedAt}
        />
      ) : selection.report && !reads.isLoading ? (
        <div className="m-screen">
          <button type="button" className="m-back" onClick={closeReport}>
            ‹ Reports
          </button>
          <p className="m-empty" data-testid="report-missing">
            That report is not in the current window and is not cached.
          </p>
        </div>
      ) : reads.isLoading && rows.length === 0 ? (
        <div className="m-screen">
          <p className="m-empty">Loading reads…</p>
        </div>
      ) : (
        <ReportsList
          rows={rows}
          day={activeDay}
          days={days}
          onOpen={openReport}
          onDay={setDay}
          consolidation={consolidation}
          onOpenConsolidation={() => {
            if (consolidation) openJournal(consolidation.id)
          }}
          cachedAt={listCachedAt}
        />
      )}

      <ConsultSheet report={pinnedReport} entity={pinnedEntity} />
    </div>
  )
}
