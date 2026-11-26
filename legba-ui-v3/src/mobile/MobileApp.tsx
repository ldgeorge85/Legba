/**
 * MobileApp — the September phone surface: a navigator and a report. NOT
 * MOUNTED since the phone shell; slated for deletion. Its Consult sheet
 * went away (Consult is the shell's Consult tab, `screens/consult/`).
 *
 * ── THE LIST IS LIGHT; A READ IS FETCHED WHEN IT IS OPENED ─────────
 * Until 2026-11-14 the navigator asked `/findings` for eighty rows at FULL
 * weight — every read's blocks, quoted spans and cited signals — so that
 * opening a report cost no network. Live, that one response was 9.2 MB on
 * every cold open. The navigator now reads the `fields=summary` projection
 * (scalars only: subject, producer, instant, title) and a report is fetched at
 * full weight only when it is opened (`fullReadQuery`: one row, usually), and
 * an Assessment's underlying world read beside it.
 *
 * ── NOTHING OF THE RECORD IS KEPT ON THE DEVICE ───────────────────────────
 * Offline, the phone shows what it loaded IN MEMORY this session, with the
 * time it was loaded ("No connection. Showing what was loaded at 14:52 UTC.");
 * a read never opened before the connection dropped says so. Earlier builds
 * stored the list and up to eight bodies in `localStorage`; those keys are
 * deleted on load (`mobileCache.purgeStoredReads`).
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

import { useCallback, useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApiError, apiGet, fetchJournalEntry, fetchJournalSummary } from '@/lib/api'
import { SignInScreen } from '@/auth/SignInScreen'
import { projectAssessment, type ReadFindingRow } from '@/lib/assemblyModel'
import {
  emitRead,
  installReadTelemetryLifecycle,
  setTelemetryWorkspace,
} from '@/lib/readTelemetry'
import {
  daysPresent,
  fullReadQuery,
  readAnalystCsv,
  toReportRow,
  type ReportRow,
} from './mobileModel'
import { useReadTiers } from '@/lib/profileUi'
import { subjectName, useSubjectIndex } from '@/lib/deskNames'
import { feedPreview } from '@/lib/proseText'
import { loadedAtLabel, purgeStoredReads } from './mobileCache'
import { useSelection, useSelectionActions } from './useUrlState'
import ReportsList from './components/ReportsList'
import ReportView from './components/ReportView'
import JournalView from './components/JournalView'

/**
 * How many read rows the navigator asks for.
 *
 * A single day is roughly one world read, one assessment, five regions and up
 * to ~32 countries, so 80 covers about two days of every tier — enough for the
 * "yesterday" step without a second request. At summary weight that is a few
 * hundred bytes a row.
 */
export const LIST_LIMIT = 80

/** The navigator's read: the summary projection of every read producer's newest rows. */
export function readsListPath(csv: string): string {
  return `/findings?analyst_id_in=${encodeURIComponent(csv)}&limit=${LIST_LIMIT}&fields=summary`
}

interface FindingsResponse {
  data: ReadFindingRow[]
}

/** The surface's identity in the read-event stream. See the module header. */
const MOBILE_WORKSPACE = 'mobile'

/** One read at full weight, by id (`GET /findings/{id}`, P-8). */
function useFullRead(id: string | null, rows: readonly ReportRow[]) {
  const path = id ? fullReadQuery(id, rows) : null
  const q = useQuery<ReadFindingRow>({
    queryKey: ['mobile-read', id, path],
    enabled: id != null && path != null,
    queryFn: () => apiGet<ReadFindingRow>(path as string),
  })
  const row = useMemo(() => (id && q.data && q.data.id === id ? q.data : null), [q.data, id])
  return { path, row, query: q }
}

export default function MobileApp() {
  const selection = useSelection()
  const { openReport, openJournal, closeReport, selectEntity, setDay } = useSelectionActions()

  useEffect(() => {
    setTelemetryWorkspace(MOBILE_WORKSPACE)
    emitRead('workspace_open', { workspace: MOBILE_WORKSPACE })
    // M-0a — whatever an earlier build stored on this device goes.
    purgeStoredReads()
    return installReadTelemetryLifecycle()
  }, [])

  // The read tiers: the projection's when the plane answers, the packaged
  // profile's otherwise (first paint never waits on the route). The subject
  // roster names a read's subject and its roster leg.
  const { tiers } = useReadTiers()
  const subjects = useSubjectIndex()
  const csv = readAnalystCsv(tiers)

  const reads = useQuery<FindingsResponse>({
    queryKey: ['mobile-reads', csv],
    refetchInterval: 5 * 60_000,
    queryFn: () => apiGet<FindingsResponse>(readsListPath(csv)),
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

  // ── the navigator rows (summary weight) ───────────────────────────────────
  const rows: ReportRow[] = useMemo(() => {
    const data = reads.data?.data
    if (!data) return []
    return data
      .map((row) => toReportRow(row, null, tiers, (id) => subjectName(id, subjects)))
      .filter((r): r is ReportRow => r !== null)
      .sort((a, b) => b.producedAt.localeCompare(a.producedAt))
  }, [reads.data, tiers, subjects])

  // Offline: a refetch failed and what is on screen is what loaded earlier.
  const listOffline = reads.isError && reads.data ? loadedAtLabel(reads.dataUpdatedAt) : null

  const days = useMemo(() => daysPresent(rows), [rows])
  const activeDay = selection.day ?? days[0] ?? null

  // ── the selected report, fetched at full weight when it is opened ────────
  const full = useFullRead(selection.report, rows)
  const activeRow = full.row
  const reportOffline =
    full.query.isError && full.query.data ? loadedAtLabel(full.query.dataUpdatedAt) : null

  // An Assessment is a voice over a spine; the spine carries the signals the
  // Assessment's own citations lack. It is fetched beside it when it is in the
  // loaded window.
  const spineId = activeRow ? projectAssessment(activeRow)?.spine_id ?? null : null
  const spine = useFullRead(spineId, rows)
  const spineRow = spine.row
  const nameOf = useCallback((id: string) => subjectName(id, subjects), [subjects])

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

  const consolidation = journal.data?.consolidation
    ? {
        id: journal.data.consolidation.id,
        // U-1 / M-0a — a list line is plain prose: no raw `**` from the title.
        title: feedPreview(journal.data.consolidation.title),
        producedAt: journal.data.consolidation.produced_at,
      }
    : null

  // A session that ENDS while the page is open: the next read answers 401. The
  // gate asked once at load; this is the same door, reached from inside.
  if (reads.error instanceof ApiError && reads.error.status === 401) {
    return <SignInScreen reason="Your session has ended. Sign in to read again." />
  }

  // Why an opened report is not on screen, in the reader's words.
  const reportGap = (() => {
    if (!selection.report || activeRow) return null
    if (reads.isLoading || full.query.isLoading) return { loading: true, text: 'Opening the read…' }
    if (reads.isError && !reads.data) return { loading: false, text: 'No connection. This read was not loaded before the connection dropped.' }
    if (full.query.isError && !full.query.data) return { loading: false, text: 'No connection. This read was not loaded before the connection dropped.' }
    return { loading: false, text: 'That read is not in the loaded window.' }
  })()

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
          offlineNote={reportOffline}
          spineRow={spineRow}
          nameOf={nameOf}
        />
      ) : selection.report && reportGap ? (
        <div className="m-screen">
          <button type="button" className="m-back" onClick={closeReport}>
            ‹ Reports
          </button>
          <p className="m-empty" data-testid={reportGap.loading ? 'report-loading' : 'report-missing'}>
            {reportGap.text}
          </p>
        </div>
      ) : reads.isLoading ? (
        <div className="m-screen">
          <p className="m-empty">Loading reads…</p>
        </div>
      ) : reads.isError && !reads.data ? (
        <div className="m-screen">
          <p className="m-empty" data-testid="reports-offline">
            No connection. Nothing has been loaded yet.
          </p>
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
          offlineNote={listOffline}
          tiers={tiers}
          subjects={subjects}
        />
      )}

    </div>
  )
}
