/**
 * Live Feed (`system.findings`) — the unified, reformed feed (S7-T4).
 *
 * ONE feed component, driven by TanStack Table (sorting / row model) + TanStack
 * Virtual (windowing) instead of the ≥10 bespoke lists it replaces, with a
 * single typed-facet FilterBar and HARD stream separation:
 *
 *  - **Two hard-separated streams.** Intelligence (analyst findings — the
 *    finished product, DEFAULT) and Signals (raw intake) NEVER interleave; a
 *    stream toggle switches between them and only the active stream fetches +
 *    tails. This is the S7-T4 fix for the old `source=all` mode that mixed raw
 *    signals into finished compositions.
 *  - **One FilterBar** (`FeedFilterBar`) — typed `key:value` chips
 *    (severity/verified/confidence/country/kind/analyst/last) + free text, with
 *    verification as a FIRST-CLASS facet on the ICD-203 verdict vocabulary. The
 *    whole filter + stream + sort serialize to a saved view AND to the `#view=`
 *    URL hash (addressable, no router).
 *  - **Review-first + live-tail with pause-on-scroll.** The stable paginated
 *    read is primary; the live tail is a toggle. Scrolling DOWN pauses the live
 *    prepend (so a read isn't yanked) and buffers new rows behind a "N new"
 *    banner; scrolling back to the top — or clicking the banner — resumes.
 *  - **Latest-per-situation.** Superseded near-dups are hidden by default
 *    (P-FS supersession index); a toggle reveals them. Signals are atomic and
 *    never cluster.
 *
 * The pure logic lives in `@/lib/feedFilters` (filter model, verdict, view
 * serialization) and `@/lib/findingsViews` (row mapping, supersession, live-tail
 * envelopes) so it is unit-tested without a DOM. Row click drives the shared
 * selection store (opens the Inspector).
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState, type UIEvent } from 'react'
import {
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from '@tanstack/react-table'
import { useVirtualizer } from '@tanstack/react-virtual'
import { ArrowDownToLine, EyeOff, Eye, FilePlus2 } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { SeverityBadge } from '@/components/SeverityBadge'
import { VerdictBadge } from '@/components/VerdictBadge'
import CitedProse from '@/components/CitedProse'
import { FeedFilterBar } from '@/components/FeedFilterBar'
import { extractCitations } from '@/lib/citationsModel'
import type { Severity } from '@/v4/world/types'
import { apiGet } from '@/lib/api'
import { feedPreview } from '@/lib/proseText'
import { useLiveTail } from '@/lib/useLiveTail'
import { useBatchedTail } from '@/lib/liveTail'
import type { PanelProps } from '@/types'
import { selectRow, useSelection } from '@/state/selection'
import { useExportBasket } from '@/state/exportBasket'
import {
  FINDINGS_TAIL_FILTER,
  SIGNALS_TAIL_FILTER,
  buildSupersessionIndex,
  mapTailEnvelope,
  relativeTime,
  rowDedupKey,
  severityRank,
  signalRestToRow,
  signalTailToRow,
  surfacedConfidence,
  type SignalRestRow,
  type UnifiedRow,
} from '@/lib/findingsViews'
import {
  DEFAULT_VIEW,
  deriveRowVerdict,
  loadFeedViews,
  matchesFilter,
  parseFilterInput,
  persistFeedViews,
  readViewHash,
  removeFeedView,
  serializeFilter,
  upsertFeedView,
  writeViewHash,
  type FeedSavedView,
  type FeedSort,
  type FeedStream,
  type ParsedFilter,
} from '@/lib/feedFilters'

/** A REST `/findings` row — every UnifiedRow field except the `source` stamp. */
interface FindingRestRow extends Omit<UnifiedRow, 'source'> {
  data?: Record<string, unknown> | null
}
interface FindingsResponse {
  data: FindingRestRow[]
  next_cursor: string | null
}
interface SignalsResponse {
  data: SignalRestRow[]
  next_cursor: string | null
}

/** One materialized feed row + its derived reading fields (computed once, reused
 *  by the filter, the sort, the VerdictBadge and the cited preview). */
interface RowItem {
  row: UnifiedRow
  citations: ReturnType<typeof extractCitations>
  verdict: ReturnType<typeof deriveRowVerdict>
}

/** Scrolling past this many px from the top pauses the live prepend. */
const SCROLL_PAUSE_PX = 24
/** Above this row count we virtualize; below it a plain list is cheaper (and
 *  keeps small feeds — and jsdom component tests — rendering every row). */
const VIRTUALIZE_THRESHOLD = 40

/** The left severity rail colour (the at-a-glance scan channel). */
function severityRailClass(severity: string | null | undefined): string {
  switch (severity) {
    case 'critical':
      return 'border-l-accent-critical'
    case 'high':
      return 'border-l-accent-warning'
    case 'medium':
      return 'border-l-severity-medium'
    case 'low':
      return 'border-l-accent-ok'
    default:
      return 'border-l-line'
  }
}

function stampFinding(r: FindingRestRow): UnifiedRow {
  return { ...r, source: 'finding' }
}

/** Prepend-dedupe a live row into a buffer, capped. */
function prependDedup(prev: UnifiedRow[], row: UnifiedRow, cap = 300): UnifiedRow[] {
  if (prev.some((r) => rowDedupKey(r) === rowDedupKey(row))) return prev
  return [row, ...prev].slice(0, cap)
}

export default function FindingsFeedPanel({ registration }: PanelProps) {
  const qc = useQueryClient()

  // ---- view state (stream · sort · filter), hydrated from #view= once ----
  const initialView = useMemo(() => readViewHash() ?? DEFAULT_VIEW, [])
  const [stream, setStream] = useState<FeedStream>(initialView.stream)
  const [sort, setSort] = useState<FeedSort>(initialView.sort)
  const [filter, setFilter] = useState<ParsedFilter>(() => parseFilterInput(initialView.query))
  const [hideSuperseded, setHideSuperseded] = useState(true)
  const [live, setLive] = useState(true)
  const [views, setViews] = useState<FeedSavedView[]>(() => loadFeedViews())

  // Serialize stream+sort+filter back into the #view= hash on every change.
  useEffect(() => {
    writeViewHash({ stream, sort, query: serializeFilter(filter) })
  }, [stream, sort, filter])

  // KEYSTONE (#89): the feed follows the global target selection so "click a
  // country → see its findings (+ its geo signals)" works. One-way (selection →
  // server target filter); the FilterBar chips stay independently editable.
  const selection = useSelection((s) => s.selection)
  const serverTargetId = selection?.kind === 'target' ? selection.id : ''

  // ---- REST paging + live buffers (per active stream) ----
  const [appended, setAppended] = useState<UnifiedRow[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [liveShown, setLiveShown] = useState<UnifiedRow[]>([])
  const [pending, setPending] = useState<UnifiedRow[]>([])

  // Pause-on-scroll: manual pause OR scrolled away from the top holds the tail.
  const [manualPaused, setManualPaused] = useState(false)
  const [scrolledAway, setScrolledAway] = useState(false)
  const isPaused = manualPaused || scrolledAway
  const isPausedRef = useRef(isPaused)
  isPausedRef.current = isPaused

  // Reset paging + live buffers whenever the stream or the server target flips.
  useEffect(() => {
    setAppended([])
    setNextCursor(null)
    setLiveShown([])
    setPending([])
  }, [stream, serverTargetId])

  // Live OFF clears the live buffers (a paused feed never shows stale live rows).
  useEffect(() => {
    if (!live) {
      setLiveShown([])
      setPending([])
    }
  }, [live])

  // When we un-pause (scrolled back to top, tail on), drain the buffer in.
  useEffect(() => {
    if (!isPaused && pending.length > 0) {
      setLiveShown((prev) => {
        let next = prev
        for (const r of [...pending].reverse()) next = prependDedup(next, r)
        return next
      })
      setPending([])
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPaused])

  // ---- REST queries — only the ACTIVE stream fetches (hard separation) ----
  const findingsParams = useMemo(() => {
    const p = new URLSearchParams({ limit: '50' })
    if (serverTargetId) p.set('target_id', serverTargetId)
    const sev = filter.chips.find((c) => c.key === 'severity')?.value
    if (sev) p.set('severity', sev) // exact-match facet — safe to push server-side
    return p
  }, [serverTargetId, filter.chips])

  const findingsQ = useQuery<FindingsResponse>({
    queryKey: ['feed-findings', findingsParams.toString()],
    enabled: stream === 'intelligence',
    queryFn: async () => {
      const r = await apiGet<FindingsResponse>(`/findings?${findingsParams.toString()}`)
      setAppended([])
      setNextCursor(r.next_cursor ?? null)
      return r
    },
    refetchInterval: 30_000,
  })

  const signalsParams = useMemo(() => {
    const p = new URLSearchParams({ limit: '50' })
    if (serverTargetId) p.set('target_id', serverTargetId)
    return p
  }, [serverTargetId])

  const signalsQ = useQuery<SignalsResponse>({
    queryKey: ['feed-signals', signalsParams.toString()],
    enabled: stream === 'signals',
    queryFn: async () => {
      const r = await apiGet<SignalsResponse>(`/signals?${signalsParams.toString()}`)
      setAppended([])
      setNextCursor(r.next_cursor ?? null)
      return r
    },
    refetchInterval: 30_000,
  })

  // -------- live tails (only the active stream tails) --------
  const filterRef = useRef(filter)
  filterRef.current = filter
  const findingsTail = useLiveTail(
    FINDINGS_TAIL_FILTER,
    (ev) => {
      if (ev.type !== 'event') return
      const row = mapTailEnvelope(ev.payload)
      if (!row) return
      // Respect the active facets on the tail so a paused-and-resumed feed stays
      // consistent with the filter (verdict recomputed per row).
      const cites = row.data ? extractCitations(row.data) : []
      if (!matchesFilter(row, deriveRowVerdict(row, cites.length), filterRef.current)) return
      if (isPausedRef.current) setPending((prev) => prependDedup(prev, row))
      else setLiveShown((prev) => prependDedup(prev, row))
    },
    live && stream === 'intelligence',
  )
  const signalsTail = useBatchedTail(
    SIGNALS_TAIL_FILTER,
    (events) => {
      const mapped = events
        .map(signalTailToRow)
        .filter((r): r is UnifiedRow => r !== null)
        .filter((r) => matchesFilter(r, deriveRowVerdict(r, 0), filterRef.current))
      if (mapped.length === 0) return
      if (isPausedRef.current) setPending((prev) => mapped.reduce((acc, r) => prependDedup(acc, r), prev))
      else setLiveShown((prev) => mapped.reduce((acc, r) => prependDedup(acc, r), prev))
    },
    { intervalMs: 5000, enabled: live && stream === 'signals' },
  )
  const connected = stream === 'signals' ? signalsTail.connected : findingsTail.connected

  // ---- merge → dedup → filter → supersession-hide ----
  const restRows = useMemo<UnifiedRow[]>(() => {
    if (stream === 'intelligence') return (findingsQ.data?.data ?? []).map(stampFinding)
    return (signalsQ.data?.data ?? []).map(signalRestToRow)
  }, [stream, findingsQ.data, signalsQ.data])

  const { rows, supersededHidden } = useMemo<{ rows: RowItem[]; supersededHidden: number }>(() => {
    const now = Date.now()
    const seen = new Set<string>()
    const merged: UnifiedRow[] = []
    for (const r of [...liveShown, ...restRows, ...appended]) {
      // Belt-and-suspenders: the buffers can outlive a stream flip.
      if (stream === 'intelligence' && r.source !== 'finding') continue
      if (stream === 'signals' && r.source !== 'signal') continue
      const k = rowDedupKey(r)
      if (seen.has(k)) continue
      seen.add(k)
      merged.push(r)
    }
    // Verdict + citation count once per row (reused by filter + the badge).
    const withVerdict: RowItem[] = merged.map((r) => {
      const citations = r.source === 'finding' && r.data ? extractCitations(r.data) : []
      return { row: r, citations, verdict: deriveRowVerdict(r, citations.length) }
    })
    const filtered = withVerdict.filter((x) => matchesFilter(x.row, x.verdict, filter, now))

    // Latest-per-situation: drop superseded finding ids (P-FS index) unless the
    // reveal toggle is off. Signals never carry supersession.
    let hidden = 0
    let visible = filtered
    if (hideSuperseded && stream === 'intelligence') {
      const idx = buildSupersessionIndex(filtered.map((x) => x.row))
      visible = filtered.filter((x) => {
        if (idx.superseded.has(x.row.id)) {
          hidden += 1
          return false
        }
        return true
      })
    }
    return { rows: visible, supersededHidden: hidden }
  }, [restRows, appended, liveShown, stream, filter, hideSuperseded])

  // ---- TanStack Table (sorting + row model) ----
  const columns = useMemo<ColumnDef<RowItem>[]>(
    () => [
      { id: 'produced_at', accessorFn: (x) => Date.parse(x.row.produced_at) || 0 },
      { id: 'severity', accessorFn: (x) => severityRank(x.row.severity) },
      { id: 'confidence', accessorFn: (x) => surfacedConfidence(x.row) ?? -1 },
    ],
    [],
  )
  const sorting: SortingState = useMemo(() => {
    if (sort === 'severity') return [{ id: 'severity', desc: true }, { id: 'produced_at', desc: true }]
    if (sort === 'confidence') return [{ id: 'confidence', desc: true }, { id: 'produced_at', desc: true }]
    return [{ id: 'produced_at', desc: true }]
  }, [sort])

  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getRowId: (x) => rowDedupKey(x.row),
  })
  const sortedRows = table.getRowModel().rows

  // ---- virtualization ----
  const parentRef = useRef<HTMLDivElement>(null)
  const virtualize = sortedRows.length > VIRTUALIZE_THRESHOLD
  const virtualizer = useVirtualizer({
    count: sortedRows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 70,
    overscan: 10,
  })

  function onScroll(e: UIEvent<HTMLDivElement>) {
    setScrolledAway(e.currentTarget.scrollTop > SCROLL_PAUSE_PX)
  }
  function resumeLive() {
    setManualPaused(false)
    if (parentRef.current) parentRef.current.scrollTop = 0
    setScrolledAway(false)
  }

  // ---- load more ----
  const hasMore = !!nextCursor
  async function loadMore() {
    if (!nextCursor) return
    const params = stream === 'intelligence' ? findingsParams : signalsParams
    const p = new URLSearchParams(params)
    p.set('cursor', nextCursor)
    if (stream === 'intelligence') {
      const next = await apiGet<FindingsResponse>(`/findings?${p.toString()}`)
      setAppended((prev) => [...prev, ...next.data.map(stampFinding)])
      setNextCursor(next.next_cursor ?? null)
    } else {
      const next = await apiGet<SignalsResponse>(`/signals?${p.toString()}`)
      setAppended((prev) => [...prev, ...next.data.map(signalRestToRow)])
      setNextCursor(next.next_cursor ?? null)
    }
  }

  function openRow(row: UnifiedRow) {
    const preview =
      row.source === 'finding' && row.body
        ? {
            title: row.title ?? undefined,
            body: row.body,
            severity: row.severity,
            analystId: row.analyst_id,
            targetId: row.target_id,
          }
        : undefined
    selectRow(row.source === 'signal' ? 'signal' : 'finding', row.id, row.title ?? undefined, {
      origin: 'feed',
      preview,
    })
  }

  // ---- saved views ----
  function saveCurrentView() {
    const name = window.prompt('Save view as:')?.trim()
    if (!name) return
    const next = upsertFeedView(views, { name, stream, sort, query: serializeFilter(filter) })
    setViews(next)
    persistFeedViews(next)
  }
  function applyView(v: FeedSavedView) {
    setStream(v.stream)
    setSort(v.sort)
    setFilter(parseFilterInput(v.query))
  }
  function deleteView(name: string) {
    const next = removeFeedView(views, name)
    setViews(next)
    persistFeedViews(next)
  }

  const isLoading = stream === 'intelligence' ? findingsQ.isLoading : signalsQ.isLoading
  const isFetching = stream === 'intelligence' ? findingsQ.isFetching : signalsQ.isFetching
  const error =
    (findingsQ.error instanceof Error ? findingsQ.error : null) ||
    (signalsQ.error instanceof Error ? signalsQ.error : null)
  const pendingCount = pending.length

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${sortedRows.length} ${stream === 'signals' ? 'signals' : 'findings'}${
        hasMore ? ' · more' : ''
      }${supersededHidden ? ` · ${supersededHidden} superseded hidden` : ''}${
        liveShown.length ? ` · ${liveShown.length} live` : ''
      }`}
      actions={
        <div className="flex items-center gap-1.5">
          {/* stream toggle — HARD separation: findings never mix with signals */}
          <div
            className="inline-flex overflow-hidden rounded border border-line text-label"
            role="group"
            aria-label="feed stream"
            data-testid="feed-stream-toggle"
          >
            <button
              className={`px-2 py-0.5 ${stream === 'intelligence' ? 'bg-surf-3 text-ink-1' : 'text-ink-3 hover:text-ink-1'}`}
              onClick={() => setStream('intelligence')}
              data-testid="feed-stream-intelligence"
              title="Finished intelligence — analyst findings & compositions (default)"
            >
              intelligence
            </button>
            <button
              className={`border-l border-line px-2 py-0.5 ${stream === 'signals' ? 'bg-surf-3 text-ink-1' : 'text-ink-3 hover:text-ink-1'}`}
              onClick={() => setStream('signals')}
              data-testid="feed-stream-signals"
              title="Raw intake — signals, never interleaved with finished intelligence"
            >
              signals
            </button>
          </div>
          <select
            className="rounded border border-line bg-surf-2 px-1 py-0.5 text-label text-ink-2"
            value={sort}
            onChange={(e) => setSort(e.target.value as FeedSort)}
            data-testid="feed-sort"
            title="sort order"
          >
            <option value="recency">recency</option>
            <option value="severity">severity</option>
            <option value="confidence">confidence</option>
          </select>
          <button
            onClick={() => setLive((v) => !v)}
            className={`rounded border px-2 py-0.5 text-label ${
              live
                ? connected
                  ? 'border-accent-ok text-accent-ok'
                  : 'border-accent-warning text-accent-warning'
                : 'border-line text-ink-3'
            }`}
            title={
              live
                ? connected
                  ? 'Live tail connected — click to pause (stable read)'
                  : 'Live tail connecting…'
                : 'Live tail off — click to resume realtime'
            }
            data-testid="feed-live-toggle"
          >
            {live ? (connected ? '● live' : '● connecting') : '○ live off'}
          </button>
        </div>
      }
      onRefresh={() => {
        setAppended([])
        setLiveShown([])
        setPending([])
        setNextCursor(null)
        qc.invalidateQueries({ queryKey: ['feed-findings'] })
        qc.invalidateQueries({ queryKey: ['feed-signals'] })
        if (stream === 'intelligence') findingsQ.refetch()
        else signalsQ.refetch()
      }}
    >
      <FeedFilterBar
        parsed={filter}
        onChange={setFilter}
        views={views}
        onApplyView={applyView}
        onSaveView={saveCurrentView}
        onDeleteView={deleteView}
        severityDisabled={stream === 'signals'}
      />

      {/* secondary controls: supersession reveal + fetching hint */}
      <div className="mb-1 flex items-center gap-2 text-label text-ink-3">
        {stream === 'intelligence' && (
          <button
            className="inline-flex items-center gap-1 hover:text-ink-1"
            onClick={() => setHideSuperseded((v) => !v)}
            data-testid="feed-superseded-toggle"
            title="Latest-per-situation: hide near-dup findings a newer run superseded"
          >
            {hideSuperseded ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
            {hideSuperseded ? 'latest per situation' : 'showing superseded'}
          </button>
        )}
        {/* keep a checkbox mirror for a11y/tests */}
        <label className="sr-only">
          <input
            type="checkbox"
            checked={hideSuperseded}
            onChange={(e) => setHideSuperseded(e.target.checked)}
            data-testid="feed-superseded-checkbox"
          />
          latest per situation
        </label>
        {isFetching && <span>loading…</span>}
      </div>

      {/* pause-on-scroll banner — buffered live rows waiting behind a scroll */}
      {live && pendingCount > 0 && (
        <button
          onClick={resumeLive}
          className="mb-1 flex w-full items-center justify-center gap-1.5 rounded border border-accent-ok/50 bg-accent-ok/10 py-1 text-label text-accent-ok hover:bg-accent-ok/20"
          data-testid="feed-resume-live"
        >
          <ArrowDownToLine className="h-3 w-3" />
          {pendingCount} new {stream === 'signals' ? 'signal' : 'finding'}
          {pendingCount === 1 ? '' : 's'} — resume live
        </button>
      )}

      {isLoading && <div className="text-ink-3 text-body">loading feed…</div>}
      {error && <div className="text-accent-critical text-body">error: {error.message}</div>}

      <div
        ref={parentRef}
        onScroll={onScroll}
        className="flex-1 overflow-auto text-xs"
        data-testid="feed-list"
      >
        {sortedRows.length === 0 && !isLoading ? (
          <div className="py-4 text-center text-body text-ink-3">no items match the filter</div>
        ) : virtualize ? (
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative', width: '100%' }}>
            {virtualizer.getVirtualItems().map((vi) => {
              const item = sortedRows[vi.index].original
              return (
                <div
                  key={sortedRows[vi.index].id}
                  data-index={vi.index}
                  ref={virtualizer.measureElement}
                  style={{
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    width: '100%',
                    transform: `translateY(${vi.start}px)`,
                    paddingBottom: 4,
                  }}
                >
                  <FeedCard
                    row={item.row}
                    citations={item.citations}
                    verdict={item.verdict}
                    index={vi.index + 1}
                    onOpen={() => openRow(item.row)}
                  />
                </div>
              )
            })}
          </div>
        ) : (
          <div className="space-y-1">
            {sortedRows.map((r, i) => (
              <FeedCard
                key={r.id}
                row={r.original.row}
                citations={r.original.citations}
                verdict={r.original.verdict}
                index={i + 1}
                onOpen={() => openRow(r.original.row)}
              />
            ))}
          </div>
        )}
      </div>

      {hasMore && (
        <div className="mt-2 border-t border-line pt-2">
          <button
            onClick={loadMore}
            className="w-full rounded border border-line bg-surf-2 p-1 text-xs hover:bg-surf-3"
            data-testid="feed-load-more"
          >
            load more
          </button>
        </div>
      )}
    </PanelChrome>
  )
}

/**
 * A10 — the feed row's "add to export" context action (findings only),
 * mirroring consult's pin-to-context: one click drops the row into the
 * persistent export basket. Rendered as a keyboard-operable span because the
 * whole FeedCard is itself a <button> (no nested interactive elements);
 * click/keys stop propagation so adding never also opens the Inspector.
 */
function FeedAddToExport({ id, title }: { id: string; title: string | null }) {
  const add = useExportBasket((s) => s.add)
  const inBasket = useExportBasket((s) => s.items.some((i) => i.kind === 'finding' && i.id === id))
  return (
    <span
      role="button"
      tabIndex={0}
      aria-disabled={inBasket}
      onClick={(e) => {
        e.stopPropagation()
        if (!inBasket) add({ kind: 'finding', id, label: title ?? undefined })
      }}
      onKeyDown={(e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return
        e.stopPropagation()
        e.preventDefault()
        if (!inBasket) add({ kind: 'finding', id, label: title ?? undefined })
      }}
      className={`shrink-0 self-center rounded border border-line px-1 py-0.5 text-label ${
        inBasket
          ? 'text-accent-ok'
          : 'text-ink-3 opacity-0 hover:text-ink-1 focus:opacity-100 group-hover:opacity-100'
      }`}
      title={inBasket ? 'in the export basket' : 'add to export basket'}
      data-testid={`feed-add-export-${id}`}
    >
      <FilePlus2 className="inline h-3 w-3" aria-hidden />
    </span>
  )
}

/**
 * One feed row — a finding OR a signal. A finding carries the full card + a
 * muted VerdictBadge (ICD-203); a signal is the slim raw-intake variant.
 */
function FeedCard({
  row,
  citations,
  verdict,
  index,
  onOpen,
}: {
  row: UnifiedRow
  citations: ReturnType<typeof extractCitations>
  verdict: ReturnType<typeof deriveRowVerdict>
  index?: number
  onOpen: () => void
}) {
  const isSignal = row.source === 'signal'
  const hasPreview = feedPreview(row.body).length > 0
  return (
    <button
      onClick={onOpen}
      className={`group block w-full cursor-pointer border-l-2 text-left text-body ${severityRailClass(
        row.severity,
      )} bg-surf-2 py-1 pl-2 pr-2 hover:bg-surf-1`}
      data-testid={isSignal ? `signal-${row.id}` : `finding-${row.id}`}
    >
      {/* title row */}
      <div className="flex items-baseline gap-2">
        {index != null && (
          <span className="w-6 shrink-0 text-right text-label tabular-nums text-ink-3">{index}.</span>
        )}
        {row.live && (
          <span
            className="shrink-0 self-center rounded bg-accent-ok/20 px-1 text-label text-accent-ok"
            data-testid={`${isSignal ? 'signal' : 'finding'}-live-${row.id}`}
          >
            live
          </span>
        )}
        {isSignal && (
          <span
            className="shrink-0 self-center rounded bg-accent-info/15 px-1 text-label uppercase tracking-wide text-accent-info"
            data-testid={`signal-badge-${row.id}`}
          >
            signal
          </span>
        )}
        <span className="min-w-0 flex-1 truncate font-semibold text-ink-1" title={row.title ?? ''}>
          {row.title}
        </span>
        {row.severity && (
          <SeverityBadge severity={row.severity as Severity} className="shrink-0 self-center" />
        )}
        <span
          className="shrink-0 self-center text-label text-ink-3"
          title={new Date(row.produced_at).toLocaleString()}
        >
          {relativeTime(row.produced_at)}
        </span>
        {!isSignal && <FeedAddToExport id={row.id} title={row.title} />}
      </div>

      {/* meta line */}
      <div className="mt-0.5 flex items-center gap-2 overflow-hidden whitespace-nowrap text-label text-ink-3">
        {isSignal ? (
          <>
            <span className="truncate" title={row.source_id ?? ''}>
              {row.source_id ?? '(source)'}
            </span>
            {(row.geo ?? []).slice(0, 3).map((g) => (
              <span key={g} className="shrink-0">
                · {g}
              </span>
            ))}
            {row.confidence !== null && <span className="shrink-0">· c={row.confidence.toFixed(2)}</span>}
            {(row.tags ?? []).length > 0 && (
              <span className="shrink-0 truncate">· {(row.tags ?? []).slice(0, 4).join(' ')}</span>
            )}
          </>
        ) : (
          <>
            <span className="truncate">{row.target_id ?? '(no target)'}</span>
            <span className="shrink-0">·</span>
            <span className="truncate">{row.analyst_id ?? '(no analyst)'}</span>
            {row.derived_from.length > 0 && (
              <span className="shrink-0">
                · ←{row.derived_from.length} input{row.derived_from.length === 1 ? '' : 's'}
              </span>
            )}
          </>
        )}
      </div>

      {/* verdict — findings only (signals are raw intake, not verify-assessed) */}
      {!isSignal && (
        <div className="mt-1">
          <VerdictBadge verdict={verdict} />
        </div>
      )}

      {hasPreview && (
        <div className="mt-1 line-clamp-2 text-ink-2">
          <CitedProse variant="inline" text={row.body ?? ''} citations={citations} />
        </div>
      )}
    </button>
  )
}
