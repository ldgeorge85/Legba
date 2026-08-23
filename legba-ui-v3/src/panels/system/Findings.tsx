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
 *    (severity/verified/judge/confidence/target/kind/analyst/last/minconf) +
 *    free text, with verification as a FIRST-CLASS facet on the ICD-203 verdict
 *    vocabulary. The whole filter + stream + sort serialize to a saved view AND
 *    to the `#view=` URL hash (addressable, no router).
 *  - **Latest-per-situation.** Superseded near-dups are hidden by default
 *    (P-FS supersession index); a toggle reveals them. Signals are atomic and
 *    never cluster.
 *
 * The pure logic lives in `@/lib/feedFilters` (filter model, verdict, server
 * push, view serialization), `@/lib/feedProducers` (the producer taxonomy) and
 * `@/lib/findingsViews` (row mapping, supersession, live-tail envelopes) so it
 * is unit-tested without a DOM; the feed's own view state lives in
 * `@/state/feedView`.
 *
 * ---------------------------------------------------------------------------
 * THE THREE THINGS THIS PANEL GUARANTEES (they are what it kept getting wrong)
 * ---------------------------------------------------------------------------
 *
 * 1. **A refetch never moves the ground under a read.** The REST page is
 *    fetched with `placeholderData: keepPreviousData`, so a poll or a filter
 *    change never blanks the list, and its result is not rendered directly:
 *    it is COMMITTED into `committed` only when the operator is at the top of
 *    the feed. Scrolled away, the fresh page is STAGED behind an "N new" pill
 *    instead, so nothing is ever inserted above the row being read. Rows carry
 *    stable identities (`rowDedupKey`, via the table's `getRowId`), the loaded
 *    extra pages (`appended`) survive a background refetch, and the scroll
 *    offset is remembered in the feed store so it also survives the panel being
 *    unmounted by a Dockview tab switch.
 *
 * 2. **Every filter is the operator's to set.** The FilterBar carries desk,
 *    producer (units · compositions · other), output kind, severity,
 *    verification and an effective-confidence floor, all AND-combined, all
 *    shown as removable chips, all persisted for the session. Facets the REST
 *    routes can answer (`target_id`, `analyst_id`, `severity`, `verified`,
 *    `judge_status`, `since`) are pushed server-side by `serverFilterParams`
 *    so they reach past the loaded page — the GLASS-1 verification facet
 *    included, so the page fill and next_cursor count the FILTERED population
 *    instead of a client-side sieve over fetched pages; the rest filter
 *    client-side (and the same predicates still gate live-tail rows, which
 *    never pass through the REST filter). No new API routes.
 *
 * 3. **Selecting is not filtering.** The feed's desk filter used to be DERIVED
 *    from the global selection, so opening a row in the Inspector (selection →
 *    `kind: 'finding'`) silently wiped the desk filter and reset the feed. Now:
 *    a desk selection SEEDS an ordinary, visible, removable `target:` chip
 *    (see `state/feedView.ts`), and a row click ONLY moves the global selection
 *    — it never touches this panel's filters, pages, or scroll. The inspected
 *    row is highlighted in place.
 */

import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState, type UIEvent } from 'react'
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
import { FeedFilterBar, type DeskOption } from '@/components/FeedFilterBar'
import { extractCitations } from '@/lib/citationsModel'
import { humanizeAnalystId } from '@/lib/analystNames'
import { countryNameForTargetId, humanizeId, thematicDeskName } from '@/lib/deskNames'
import type { Severity } from '@/v4/world/types'
import { apiGet } from '@/lib/api'
import { feedPreview } from '@/lib/proseText'
import { useLiveTail } from '@/lib/useLiveTail'
import { useBatchedTail } from '@/lib/liveTail'
import type { PanelProps } from '@/types'
import { selectRow, useSelection } from '@/state/selection'
import { useExportBasket } from '@/state/exportBasket'
import { FEED_VIEW_RESTORED, useFeedView } from '@/state/feedView'
import { useCountryVerdicts } from '@/v4/world/countryVerdicts'
import { useSupplyChainDesks } from '@/v4/world/supplyChainDesks'
import { buildProducerOptions, exactProducerIds } from '@/lib/feedProducers'
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
  FINDINGS_SERVER_FACETS,
  SIGNALS_SERVER_FACETS,
  chipValue,
  deriveRowVerdict,
  loadFeedViews,
  matchesFilter,
  persistFeedViews,
  removeFeedView,
  serializeFilter,
  serverFilterParams,
  upsertFeedView,
  writeViewHash,
  type FeedSavedView,
  type FeedSort,
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

/** Scrolling past this many px from the top HOLDS new arrivals behind the pill. */
const SCROLL_HOLD_PX = 24
/** Above this row count we virtualize; below it a plain list is cheaper (and
 *  keeps small feeds — and jsdom component tests — rendering every row). */
const VIRTUALIZE_THRESHOLD = 40
/** Scroll is a high-frequency event; only persist the offset once it settles. */
const SCROLL_PERSIST_MS = 250

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

/**
 * Union new values into a monotonic "seen" list, returning the SAME array when
 * nothing was added so React bails out of the re-render. The facet dropdowns
 * are built from these lists: they only ever grow, so narrowing the feed to one
 * producer never collapses the menu you narrowed it with.
 */
function unionSeen(prev: string[], incoming: Iterable<string | null | undefined>): string[] {
  const seen = new Set(prev)
  let grew = false
  for (const raw of incoming) {
    const v = (raw ?? '').trim()
    if (!v || seen.has(v)) continue
    seen.add(v)
    grew = true
  }
  return grew ? [...seen] : prev
}

export default function FindingsFeedPanel({ registration }: PanelProps) {
  const qc = useQueryClient()

  // ---- view state (stream · sort · filter · toggles) — the feed's OWN store,
  // session-scoped so a Dockview tab switch never drops the operator's posture.
  const stream = useFeedView((s) => s.stream)
  const sort = useFeedView((s) => s.sort)
  const filter = useFeedView((s) => s.filter)
  const hideSuperseded = useFeedView((s) => s.hideSuperseded)
  const live = useFeedView((s) => s.live)
  const setStream = useFeedView((s) => s.setStream)
  const setSort = useFeedView((s) => s.setSort)
  const setFilter = useFeedView((s) => s.setFilter)
  const setHideSuperseded = useFeedView((s) => s.setHideSuperseded)
  const setLive = useFeedView((s) => s.setLive)
  const applyStoredView = useFeedView((s) => s.applyView)
  const [views, setViews] = useState<FeedSavedView[]>(() => loadFeedViews())

  // Serialize stream+sort+filter back into the #view= hash on every change.
  useEffect(() => {
    writeViewHash({ stream, sort, query: serializeFilter(filter) })
  }, [stream, sort, filter])

  // ---- selection ⇄ feed, with the two directions DELIBERATELY asymmetric ----
  //
  // IN  (desk → feed): a desk selection SEEDS the `target:` chip. Subscribed
  //     imperatively rather than through a render dependency so that re-picking
  //     the SAME desk after the operator cleared the chip by hand seeds it
  //     again (a fresh click is a fresh instruction), while a no-op re-render
  //     never rewrites a filter the operator has since changed.
  // OUT (feed → Inspector): `openRow` moves the global selection and NOTHING
  //     else. Selecting a finding does not filter the feed — that coupling is
  //     the bug this panel was carrying.
  useEffect(() => {
    const seed = (sel: ReturnType<typeof useSelection.getState>['selection']) => {
      if (sel?.kind !== 'target' || !sel.id) return
      const view = useFeedView.getState()
      // Already the active desk filter? Nothing to do — never churn the store.
      if (chipValue(view.filter.chips, 'target') === sel.id) return
      view.seedDeskFilter(sel.id)
    }
    // Adopt a target that was already selected when the panel mounted ONLY on a
    // pristine feed (fresh session, no `#view=`): that is the deep-link /
    // cold-boot case the keystone "pick a desk → see its findings" flow needs.
    // A session where the operator has been driving the filters is never
    // retro-seeded out from under them.
    if (!FEED_VIEW_RESTORED) seed(useSelection.getState().selection)
    return useSelection.subscribe((s) => seed(s.selection))
  }, [])

  const selection = useSelection((s) => s.selection)
  /** The row the Inspector is showing, so the feed can highlight it in place. */
  const selectedRowId =
    selection && (selection.kind === 'finding' || selection.kind === 'signal') ? selection.id : null

  // ---- REST paging + live buffers (per active stream) ----
  /** The committed REST page — what the list actually renders. */
  const [committed, setCommitted] = useState<UnifiedRow[]>([])
  const [pageCursor, setPageCursor] = useState<string | null>(null)
  /** A fresh REST page held back because the operator is mid-read (see `hold`). */
  const [staged, setStaged] = useState<{ rows: UnifiedRow[]; cursor: string | null } | null>(null)
  /** Extra pages pulled by "load more" — kept across background refetches. */
  const [appended, setAppended] = useState<UnifiedRow[]>([])
  const [appendCursor, setAppendCursor] = useState<string | null | undefined>(undefined)
  const [liveShown, setLiveShown] = useState<UnifiedRow[]>([])
  const [pendingTail, setPendingTail] = useState<UnifiedRow[]>([])

  /** Monotonic facet vocabularies, harvested from every page we have seen. */
  const [seenProducers, setSeenProducers] = useState<string[]>([])
  const [seenTargets, setSeenTargets] = useState<string[]>([])
  const [seenKinds, setSeenKinds] = useState<string[]>([])

  // HOLD — scrolled away from the top means a read is in progress, so nothing
  // new may be inserted above it. Everything queues behind the "N new" pill.
  const [scrolledAway, setScrolledAway] = useState(false)
  const holdRef = useRef(scrolledAway)
  holdRef.current = scrolledAway

  // The scroll container + the last offset seen on it (persisted on a debounce).
  const parentRef = useRef<HTMLDivElement>(null)
  const lastTopRef = useRef(0)

  // Ref mirrors so the drain can read the held buffers without a setState
  // updater reaching for another setState (updaters must stay pure).
  const stagedRef = useRef(staged)
  stagedRef.current = staged
  const pendingTailRef = useRef(pendingTail)
  pendingTailRef.current = pendingTail

  /** Merge everything held back into the visible list, in one pass. */
  const drain = useCallback(() => {
    const s = stagedRef.current
    if (s) {
      setCommitted(s.rows)
      setPageCursor(s.cursor)
      setStaged(null)
    }
    const buf = pendingTailRef.current
    if (buf.length > 0) {
      setLiveShown((prev) => [...buf].reverse().reduce((acc, r) => prependDedup(acc, r), prev))
      setPendingTail([])
    }
  }, [])

  /** The pill's action: merge the held rows AND go back to the top. */
  const showNew = useCallback(() => {
    drain()
    if (parentRef.current) parentRef.current.scrollTop = 0
    lastTopRef.current = 0
    setScrolledAway(false)
  }, [drain])

  // Scrolling back to the top is itself the "I'm done reading" signal — drain
  // whatever queued up while the operator was down the list.
  useEffect(() => {
    if (!scrolledAway) drain()
  }, [scrolledAway, drain])

  // ---- server-side facet push -------------------------------------------
  // The desk roster is shared cache with the Sidebar/map (same query keys), so
  // offering it here costs no extra request.
  const { verdicts } = useCountryVerdicts()
  const { desks: supplyChainDesks } = useSupplyChainDesks()

  const deskOptions = useMemo<DeskOption[]>(() => {
    const byId = new Map<string, string>()
    for (const v of verdicts.values()) {
      if (!v.targetId) continue
      byId.set(v.targetId, countryNameForTargetId(v.targetId) ?? humanizeId(v.targetId))
    }
    for (const d of supplyChainDesks) {
      byId.set(d.targetId, thematicDeskName(d.targetId) ?? humanizeId(d.targetId))
    }
    for (const t of seenTargets) if (!byId.has(t)) byId.set(t, humanizeId(t))
    return [...byId.entries()]
      .map(([id, label]) => ({ id, label }))
      .sort((a, b) => a.label.localeCompare(b.label))
  }, [verdicts, supplyChainDesks, seenTargets])

  const exactTargets = useMemo(() => new Set(deskOptions.map((d) => d.id)), [deskOptions])
  const exactAnalysts = useMemo(() => exactProducerIds(seenProducers), [seenProducers])
  const producerOptions = useMemo(() => buildProducerOptions(seenProducers), [seenProducers])

  const findingsParams = useMemo(() => {
    const p = new URLSearchParams({ limit: '50' })
    const pushed = serverFilterParams(filter, {
      supports: FINDINGS_SERVER_FACETS,
      exactTargets,
      exactAnalysts,
    })
    for (const [k, v] of Object.entries(pushed)) p.set(k, v)
    return p
  }, [filter, exactTargets, exactAnalysts])

  const signalsParams = useMemo(() => {
    const p = new URLSearchParams({ limit: '50' })
    const pushed = serverFilterParams(filter, { supports: SIGNALS_SERVER_FACETS, exactTargets })
    for (const [k, v] of Object.entries(pushed)) p.set(k, v)
    return p
  }, [filter, exactTargets])

  // ---- REST queries — only the ACTIVE stream fetches (hard separation) ----
  // `keepPreviousData` is load-bearing: a 30s poll (or a filter change) must
  // never blank the list. The queryFn is a PURE fetch — the old version reset
  // the paging state from inside it, which is exactly what made every poll
  // throw away the operator's loaded pages.
  const findingsQ = useQuery<FindingsResponse>({
    queryKey: ['feed-findings', findingsParams.toString()],
    enabled: stream === 'intelligence',
    queryFn: () => apiGet<FindingsResponse>(`/findings?${findingsParams.toString()}`),
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  })

  const signalsQ = useQuery<SignalsResponse>({
    queryKey: ['feed-signals', signalsParams.toString()],
    enabled: stream === 'signals',
    queryFn: () => apiGet<SignalsResponse>(`/signals?${signalsParams.toString()}`),
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  })

  const activeQ = stream === 'intelligence' ? findingsQ : signalsQ
  /** Identity of the page currently on screen — a change means a NEW question
   *  was asked (filter/stream), not just a re-poll of the same one. */
  const restKey = `${stream}:${(stream === 'intelligence' ? findingsParams : signalsParams).toString()}`
  const committedKeyRef = useRef<string | null>(null)

  const restRows = useMemo<UnifiedRow[]>(() => {
    if (stream === 'intelligence') {
      const page = findingsQ.data as FindingsResponse | undefined
      return (page?.data ?? []).map(stampFinding)
    }
    const page = signalsQ.data as SignalsResponse | undefined
    return (page?.data ?? []).map(signalRestToRow)
  }, [stream, findingsQ.data, signalsQ.data])

  const restCursor =
    (stream === 'intelligence' ? findingsQ.data?.next_cursor : signalsQ.data?.next_cursor) ?? null

  /**
   * Commit (or stage) each freshly-fetched page.
   *
   *  - a NEW question (`restKey` changed) always commits and resets the paging;
   *  - a re-poll of the SAME question commits only while the operator is at the
   *    top; scrolled away it is staged behind the pill so the list never
   *    reflows under a read. Loaded extra pages (`appended`) survive either way.
   */
  useEffect(() => {
    // Placeholder data belongs to the PREVIOUS query key — committing it would
    // mis-file the old page under the new question.
    if (activeQ.isPlaceholderData || !activeQ.data) return

    setSeenProducers((prev) => unionSeen(prev, restRows.map((r) => r.analyst_id)))
    setSeenTargets((prev) => unionSeen(prev, restRows.map((r) => r.target_id)))
    setSeenKinds((prev) => unionSeen(prev, restRows.map((r) => r.kind)))

    if (committedKeyRef.current !== restKey) {
      committedKeyRef.current = restKey
      setCommitted(restRows)
      setPageCursor(restCursor)
      setStaged(null)
      setAppended([])
      setAppendCursor(undefined)
      return
    }
    if (holdRef.current) setStaged({ rows: restRows, cursor: restCursor })
    else {
      setCommitted(restRows)
      setPageCursor(restCursor)
      setStaged(null)
    }
  }, [activeQ.data, activeQ.isPlaceholderData, restRows, restCursor, restKey])

  // A stream flip drops the live buffers immediately (they are per-stream), so a
  // signals tail can never leak into the intelligence list during the refetch.
  useEffect(() => {
    setLiveShown([])
    setPendingTail([])
  }, [stream])

  // Live OFF clears the live buffers (a paused feed never shows stale live rows).
  useEffect(() => {
    if (!live) {
      setLiveShown([])
      setPendingTail([])
    }
  }, [live])

  // ---- live tails (only the active stream tails) --------------------------
  const filterRef = useRef(filter)
  filterRef.current = filter
  const findingsTail = useLiveTail(
    FINDINGS_TAIL_FILTER,
    (ev) => {
      if (ev.type !== 'event') return
      const row = mapTailEnvelope(ev.payload)
      if (!row) return
      // Respect the active facets on the tail so a held-and-resumed feed stays
      // consistent with the filter (verdict recomputed per row).
      const cites = row.data ? extractCitations(row.data) : []
      if (!matchesFilter(row, deriveRowVerdict(row, cites.length), filterRef.current)) return
      if (holdRef.current) setPendingTail((prev) => prependDedup(prev, row))
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
      if (holdRef.current) setPendingTail((prev) => mapped.reduce((acc, r) => prependDedup(acc, r), prev))
      else setLiveShown((prev) => mapped.reduce((acc, r) => prependDedup(acc, r), prev))
    },
    { intervalMs: 5000, enabled: live && stream === 'signals' },
  )
  const connected = stream === 'signals' ? signalsTail.connected : findingsTail.connected

  // ---- merge → dedup → filter → supersession-hide ----
  const { rows, supersededHidden, shownKeys } = useMemo<{
    rows: RowItem[]
    supersededHidden: number
    shownKeys: Set<string>
  }>(() => {
    const now = Date.now()
    const seen = new Set<string>()
    const merged: UnifiedRow[] = []
    for (const r of [...liveShown, ...committed, ...appended]) {
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
    return { rows: visible, supersededHidden: hidden, shownKeys: seen }
  }, [committed, appended, liveShown, stream, filter, hideSuperseded])

  /**
   * What the "N new" pill is counting: staged REST rows the operator has not
   * seen AND that pass the active filter (an arrival the filter would hide is
   * not "new" to this reader), plus the buffered live-tail rows.
   */
  const newFromStaged = useMemo(() => {
    if (!staged) return 0
    const now = Date.now()
    let n = 0
    for (const r of staged.rows) {
      if (shownKeys.has(rowDedupKey(r))) continue
      const cites = r.source === 'finding' && r.data ? extractCitations(r.data) : []
      if (!matchesFilter(r, deriveRowVerdict(r, cites.length), filter, now)) continue
      n += 1
    }
    return n
  }, [staged, shownKeys, filter])
  const pendingCount = pendingTail.length + newFromStaged

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
    // STABLE ROW IDENTITY — the same row keeps the same React key across every
    // refetch, so a re-poll re-uses the DOM node instead of remounting the list.
    getRowId: (x) => rowDedupKey(x.row),
  })
  const sortedRows = table.getRowModel().rows

  // ---- virtualization + scroll continuity ----
  const virtualize = sortedRows.length > VIRTUALIZE_THRESHOLD
  const virtualizer = useVirtualizer({
    count: sortedRows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 70,
    overscan: 10,
  })

  // Remember the scroll offset (debounced — it fires every frame) and flush it
  // on unmount, so a Dockview tab switch returns to the same place in the feed.
  const scrollFlushRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (scrollFlushRef.current) clearTimeout(scrollFlushRef.current)
      useFeedView.getState().setScrollTop(lastTopRef.current)
    },
    [],
  )

  function onScroll(e: UIEvent<HTMLDivElement>) {
    const top = e.currentTarget.scrollTop
    lastTopRef.current = top
    setScrolledAway(top > SCROLL_HOLD_PX)
    if (scrollFlushRef.current) clearTimeout(scrollFlushRef.current)
    scrollFlushRef.current = setTimeout(() => useFeedView.getState().setScrollTop(top), SCROLL_PERSIST_MS)
  }

  // One-shot restore once there is something to scroll through.
  const restoredRef = useRef(false)
  useEffect(() => {
    if (restoredRef.current || sortedRows.length === 0) return
    const el = parentRef.current
    if (!el) return
    restoredRef.current = true
    const top = useFeedView.getState().scrollTop
    if (top > 0) {
      el.scrollTop = top
      lastTopRef.current = top
      setScrolledAway(top > SCROLL_HOLD_PX)
    }
  }, [sortedRows.length])

  // ---- load more ----
  const nextCursor = appendCursor === undefined ? pageCursor : appendCursor
  const hasMore = !!nextCursor
  async function loadMore() {
    if (!nextCursor) return
    const params = stream === 'intelligence' ? findingsParams : signalsParams
    const p = new URLSearchParams(params)
    p.set('cursor', nextCursor)
    if (stream === 'intelligence') {
      const next = await apiGet<FindingsResponse>(`/findings?${p.toString()}`)
      setAppended((prev) => [...prev, ...next.data.map(stampFinding)])
      setAppendCursor(next.next_cursor ?? null)
    } else {
      const next = await apiGet<SignalsResponse>(`/signals?${p.toString()}`)
      setAppended((prev) => [...prev, ...next.data.map(signalRestToRow)])
      setAppendCursor(next.next_cursor ?? null)
    }
  }

  /**
   * Open a row in the Inspector. Moves the GLOBAL selection and nothing else —
   * no filter write, no refetch, no scroll change. (Defect 3.)
   */
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
    applyStoredView({ stream: v.stream, sort: v.sort, query: v.query })
  }
  function deleteView(name: string) {
    const next = removeFeedView(views, name)
    setViews(next)
    persistFeedViews(next)
  }

  const isLoading = activeQ.isLoading
  const isFetching = activeQ.isFetching
  const error =
    (findingsQ.error instanceof Error ? findingsQ.error : null) ||
    (signalsQ.error instanceof Error ? signalsQ.error : null)

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
            onClick={() => setLive(!live)}
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
        // An explicit refresh is the one place a full reset is what was asked
        // for — drop the buffers, return to the top (so the incoming page
        // commits rather than queueing behind the pill) and re-ask the question
        // from page one.
        setAppended([])
        setAppendCursor(undefined)
        setLiveShown([])
        setPendingTail([])
        setStaged(null)
        if (parentRef.current) parentRef.current.scrollTop = 0
        lastTopRef.current = 0
        setScrolledAway(false)
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
        deskOptions={deskOptions}
        producerOptions={producerOptions}
        kindOptions={seenKinds}
      />

      {/* secondary controls: supersession reveal + fetching hint */}
      <div className="mb-1 flex items-center gap-2 text-label text-ink-3">
        {stream === 'intelligence' && (
          <button
            className="inline-flex items-center gap-1 hover:text-ink-1"
            onClick={() => setHideSuperseded(!hideSuperseded)}
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

      {/* The hold banner — everything that arrived while the operator was
          reading, waiting to be merged on THEIR say-so. */}
      {pendingCount > 0 && (
        <button
          onClick={showNew}
          className="mb-1 flex w-full items-center justify-center gap-1.5 rounded border border-accent-ok/50 bg-accent-ok/10 py-1 text-label text-accent-ok hover:bg-accent-ok/20"
          data-testid="feed-resume-live"
        >
          <ArrowDownToLine className="h-3 w-3" />
          {pendingCount} new {stream === 'signals' ? 'signal' : 'finding'}
          {pendingCount === 1 ? '' : 's'} — show
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
                    active={item.row.id === selectedRowId}
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
                active={r.original.row.id === selectedRowId}
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
 * `active` marks the row the Inspector is currently showing.
 */
function FeedCard({
  row,
  citations,
  verdict,
  index,
  active = false,
  onOpen,
}: {
  row: UnifiedRow
  citations: ReturnType<typeof extractCitations>
  verdict: ReturnType<typeof deriveRowVerdict>
  index?: number
  active?: boolean
  onOpen: () => void
}) {
  const isSignal = row.source === 'signal'
  const hasPreview = feedPreview(row.body).length > 0
  return (
    <button
      onClick={onOpen}
      aria-current={active ? 'true' : undefined}
      className={`group block w-full cursor-pointer border-l-2 text-left text-body ${severityRailClass(
        row.severity,
      )} py-1 pl-2 pr-2 ${active ? 'bg-surf-3 ring-1 ring-inset ring-accent-info/60' : 'bg-surf-2 hover:bg-surf-1'}`}
      data-testid={isSignal ? `signal-${row.id}` : `finding-${row.id}`}
      data-active={active ? 'true' : undefined}
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
            <span className="truncate" title={row.analyst_id ?? undefined}>
              {humanizeAnalystId(row.analyst_id, '(no analyst)')}
            </span>
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
          {/* This row is itself a <button onClick={onOpen}> — interactiveParent
              stops a tap on either InfoTip chip from also bubbling into
              onOpen (see InfoTip's interactive-parent mode doc). */}
          <VerdictBadge verdict={verdict} interactiveParent />
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
