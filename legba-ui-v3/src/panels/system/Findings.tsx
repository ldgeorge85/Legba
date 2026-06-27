/**
 * Live Feed (`system.findings`) — the single unified feed (#90 feed merge).
 *
 * ONE feed of findings AND signals. Reads `GET /api/v1/findings` and
 * `GET /api/v1/signals` (substrate-reads endpoint family) and folds in two NATS
 * live tails (`analyst.*.finding` + `legba.signals.>`). The former separate
 * "pulse" rail (v4.feed) and "browse" workbench are collapsed into orthogonal
 * controls on this one panel:
 *
 *  - **Live** (on/off button) — the only "mode" control. Live-ON tails new
 *    findings+signals in realtime (the pulse); Live-OFF tears down both WS subs,
 *    clears the live buffers, and leaves a stable, paginated read (the browse).
 *  - **Source** (All / Findings / Signals) — gates which REST seed AND which
 *    live tail are active at the DATA layer (Findings pays zero cost for signals
 *    and vice-versa; at most two subscriptions, same as the old pulse rail).
 *  - **Cluster** (clustered ⇄ flat) — situation clustering, FINDINGS-ONLY:
 *    near-dup re-assessments of one situation collapse to the latest, with the
 *    superseded history one click away. Signals are atomic events and never
 *    cluster (enforced by the `clusterKeyOf` source guard in findingsViews);
 *    they render flat below the finding clusters (Cluster ON) or interleaved by
 *    recency (Cluster OFF).
 *
 *  Plus: saved views, a findings-only hourly sparkline, server filters
 *  (target/analyst/severity) + client text/sort, and the keystone
 *  selection-follow (#89: click a country → its findings AND its geo signals).
 *
 * All grouping / sorting / view / row-mapping logic lives in
 * `@/lib/findingsViews` so it is unit-tested without a DOM. Row click drives the
 * shared selection store (opens the Inspector).
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Area,
  AreaChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { ChevronDown, ChevronRight, SlidersHorizontal } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { SeverityBadge } from '@/components/SeverityBadge'
import type { Severity } from '@/v4/world/types'
import { apiGet } from '@/lib/api'
import { useLiveTail } from '@/lib/useLiveTail'
import { useBatchedTail } from '@/lib/liveTail'
import type { PanelProps } from '@/types'
import { selectRow, useSelection } from '@/state/selection'
import {
  DEFAULT_FILTER,
  FINDINGS_TAIL_FILTER,
  SIGNALS_TAIL_FILTER,
  buildSupersessionIndex,
  clusterBySituation,
  loadSavedViews,
  mapTailEnvelope,
  persistSavedViews,
  relativeTime,
  removeView,
  rowDedupKey,
  signalRestToRow,
  signalTailToRow,
  sortFindings,
  upsertView,
  type FindingsCluster,
  type FindingsFilter,
  type SavedView,
  type SignalRestRow,
  type SortMode,
  type UnifiedRow,
} from '@/lib/findingsViews'

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

type SourceFilter = 'all' | 'findings' | 'signals'

const SEVERITY_OPTIONS = ['all', 'low', 'medium', 'high', 'critical'] as const

/**
 * The left at-a-glance severity rail colour (the old numbered feed's key scan
 * channel): critical=red, high=amber, medium=yellow, low/none=slate. Returns a
 * `border-l` colour class applied to the 3px rail on every FeedCard. Signals
 * carry no severity → slate.
 */
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
      return 'border-l-slate-700'
  }
}

/** Stamp a REST findings row as a unified finding row. */
function stampFinding(r: FindingRestRow): UnifiedRow {
  return { ...r, source: 'finding' }
}

export default function FindingsFeedPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [filter, setFilter] = useState<FindingsFilter>(DEFAULT_FILTER)
  // #90 feed merge — the two orthogonal controls that replaced the Pulse/Browse
  // tabs: `source` gates which kinds stream; `live` gates whether the tails run.
  const [source, setSource] = useState<SourceFilter>('all')
  const [live, setLive] = useState(true)

  // KEYSTONE (#89): the feed FOLLOWS the global selection so "click a country →
  // see exactly what its country_assessor recorded (+ its geo signals)" works.
  // One-way only (selection → filter); the manual inputs stay editable.
  const selection = useSelection((s) => s.selection)
  useEffect(() => {
    if (selection?.kind === 'target') {
      setFilter((f) => (f.target_id === selection.id ? f : { ...f, target_id: selection.id }))
    } else if (selection?.kind === 'analyst') {
      setFilter((f) => (f.analyst_id === selection.id ? f : { ...f, analyst_id: selection.id }))
    }
  }, [selection?.kind, selection?.id])

  // Findings page + load-more + live tail buffers.
  const [appended, setAppended] = useState<FindingRestRow[]>([])
  const [liveFindings, setLiveFindings] = useState<UnifiedRow[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  // Signals page + load-more + live tail buffers.
  const [signalsAppended, setSignalsAppended] = useState<SignalRestRow[]>([])
  const [liveSignals, setLiveSignals] = useState<UnifiedRow[]>([])
  const [signalsCursor, setSignalsCursor] = useState<string | null>(null)

  const [groupBySituation, setGroupBySituation] = useState(true)
  const [textFilter, setTextFilter] = useState('')
  const [views, setViews] = useState<SavedView[]>(() => loadSavedViews())
  // Advanced filters / saved-views are tucked behind a disclosure so the list
  // reclaims the vertical room the old multi-row filter bar + tall sparkline
  // ate. The primary controls (source, search, live) stay always-visible.
  const [showAdvanced, setShowAdvanced] = useState(false)

  // Live OFF clears the live buffers so a paused feed never shows stale
  // `live`-badged rows; the REST seed + 30s poll is the stable surface.
  useEffect(() => {
    if (!live) {
      setLiveFindings([])
      setLiveSignals([])
    }
  }, [live])

  // ---- Findings REST query (gated off when source = Signals) ----
  const findingParams = useMemo(() => {
    const p = new URLSearchParams({ limit: '50' })
    if (filter.target_id) p.set('target_id', filter.target_id)
    if (filter.analyst_id) p.set('analyst_id', filter.analyst_id)
    if (filter.severity !== 'all') p.set('severity', filter.severity)
    return p
  }, [filter.target_id, filter.analyst_id, filter.severity])

  const findingsQ = useQuery<FindingsResponse>({
    queryKey: ['findings-feed', findingParams.toString()],
    enabled: source !== 'signals',
    queryFn: async () => {
      const r = await apiGet<FindingsResponse>(`/findings?${findingParams.toString()}`)
      // NB: do NOT clear the live buffer here — queryFn is re-run by the 30s
      // refetchInterval, and wiping liveFindings/liveSignals on a background
      // poll permanently drops id-less ephemeral live signals. Live buffers are
      // cleared only on a real filter change / Live-off / manual refresh below.
      setAppended([])
      setNextCursor(r.next_cursor ?? null)
      return r
    },
    refetchInterval: 30_000, // poll backstop; live-tail handles real-time
  })

  // ---- Signals REST query (gated off when source = Findings) ----
  // Signals are target-agnostic + carry no analyst_id/severity; only target_id
  // (resolved server-side to scope.geo) applies.
  const signalParams = useMemo(() => {
    const p = new URLSearchParams({ limit: '50' })
    if (filter.target_id) p.set('target_id', filter.target_id)
    return p
  }, [filter.target_id])

  const signalsQ = useQuery<SignalsResponse>({
    queryKey: ['signals-feed', signalParams.toString()],
    enabled: source !== 'findings',
    queryFn: async () => {
      const r = await apiGet<SignalsResponse>(`/signals?${signalParams.toString()}`)
      // (see findings queryFn) — never clear liveSignals on the 30s poll.
      setSignalsAppended([])
      setSignalsCursor(r.next_cursor ?? null)
      return r
    },
    refetchInterval: 30_000,
  })

  // Clear the live buffers when the FILTER actually changes (params are memoized,
  // so these fire only on a real filter change — never on the 30s poll). Stops
  // stale-filter live rows lingering without the poll-wipes-live data loss.
  useEffect(() => {
    setLiveFindings([])
  }, [findingParams])
  useEffect(() => {
    setLiveSignals([])
  }, [signalParams])

  // -------- NATS live tails (both gated by the single Live button + Source) --------
  const filterRef = useRef(filter)
  filterRef.current = filter
  const findingsTail = useLiveTail(
    FINDINGS_TAIL_FILTER,
    (ev) => {
      if (ev.type !== 'event') return
      const row = mapTailEnvelope(ev.payload)
      if (!row) return
      const f = filterRef.current
      if (f.target_id && row.target_id !== f.target_id) return
      if (f.analyst_id && row.analyst_id !== f.analyst_id) return
      if (f.severity !== 'all' && row.severity !== f.severity) return
      setLiveFindings((prev) => {
        if (prev.some((r) => r.id === row.id)) return prev
        return [row, ...prev].slice(0, 200)
      })
    },
    live && source !== 'signals',
  )
  const signalsTail = useBatchedTail(
    SIGNALS_TAIL_FILTER,
    (events) => {
      const f = filterRef.current
      const mapped = events
        .map(signalTailToRow)
        .filter((r): r is UnifiedRow => r !== null)
        // signals respect only the target_id filter (no analyst/severity on signals)
        .filter((r) => !f.target_id || r.target_id === f.target_id)
      if (mapped.length === 0) return
      setLiveSignals((prev) => {
        const have = new Set(prev.map(rowDedupKey))
        const fresh = mapped.filter((r) => !have.has(rowDedupKey(r)))
        if (fresh.length === 0) return prev
        return [...fresh, ...prev].slice(0, 200)
      })
    },
    { intervalMs: 5000, enabled: live && source !== 'findings' },
  )
  const connected = findingsTail.connected || signalsTail.connected

  // ---- Merge → dedup → source-filter → text-filter → sort ----
  const rows = useMemo(() => {
    const findingsPage = source !== 'signals' ? (findingsQ.data?.data ?? []).map(stampFinding) : []
    const findingsAppendedRows = source !== 'signals' ? appended.map(stampFinding) : []
    const signalsPage = source !== 'findings' ? (signalsQ.data?.data ?? []).map(signalRestToRow) : []
    const signalsAppendedRows =
      source !== 'findings' ? signalsAppended.map(signalRestToRow) : []

    const seen = new Set<string>()
    const merged: UnifiedRow[] = []
    for (const r of [
      ...liveSignals,
      ...liveFindings,
      ...findingsPage,
      ...signalsPage,
      ...findingsAppendedRows,
      ...signalsAppendedRows,
    ]) {
      // Belt-and-suspenders: a disabled query can hold stale data + the live
      // buffers may outlive a Source flip, so re-assert the Source filter here.
      if (source === 'findings' && r.source !== 'finding') continue
      if (source === 'signals' && r.source !== 'signal') continue
      const k = rowDedupKey(r)
      if (seen.has(k)) continue
      seen.add(k)
      merged.push(r)
    }
    const q = textFilter.trim().toLowerCase()
    const filtered = q
      ? merged.filter((r) =>
          [r.title, r.body, r.target_id, r.analyst_id, r.source_id]
            .filter((v): v is string => typeof v === 'string')
            .some((v) => v.toLowerCase().includes(q)),
        )
      : merged
    return sortFindings(filtered, filter.sort)
  }, [
    findingsQ.data,
    signalsQ.data,
    appended,
    signalsAppended,
    liveFindings,
    liveSignals,
    source,
    filter.sort,
    textFilter,
  ])

  const findingRows = useMemo(() => rows.filter((r) => r.source === 'finding'), [rows])
  const signalRows = useMemo(() => rows.filter((r) => r.source === 'signal'), [rows])

  // Situation clustering (P-FS) — findings only. Cluster ON groups the findings
  // and renders signals as a flat block after; Cluster OFF passes the whole
  // merged list to a single flat pseudo-cluster (signals interleave by sort).
  const supersessionIndex = useMemo(() => buildSupersessionIndex(findingRows), [findingRows])
  const clusters = useMemo(
    () =>
      groupBySituation
        ? clusterBySituation(findingRows, true, supersessionIndex)
        : clusterBySituation(rows, false),
    [rows, findingRows, groupBySituation, supersessionIndex],
  )
  const clustered = clusters.some((c) => !c.flat)
  const collapsed = useMemo(
    () => clusters.reduce((n, c) => n + (c.history?.length ?? 0), 0),
    [clusters],
  )

  /** Volume sparkline: FINDINGS per hour over the last 24h (signals run ~100×
   *  findings volume and would swamp the rhythm). */
  const hourly = useMemo(() => {
    const now = Date.now()
    const buckets: Array<{ hour: number; label: string; count: number }> = []
    for (let i = 23; i >= 0; i--) {
      const start = new Date(now - i * 3600_000)
      start.setMinutes(0, 0, 0)
      buckets.push({
        hour: start.getTime(),
        label: `${start.getHours().toString().padStart(2, '0')}:00`,
        count: 0,
      })
    }
    const cutoff = now - 24 * 3600_000
    for (const r of findingRows) {
      const t = Date.parse(r.produced_at)
      if (!Number.isFinite(t) || t < cutoff) continue
      const idx = 23 - Math.floor((now - t) / 3600_000)
      if (idx >= 0 && idx < buckets.length) buckets[idx].count += 1
    }
    return buckets
  }, [findingRows])

  const hasMore =
    (!!nextCursor && source !== 'signals') || (!!signalsCursor && source !== 'findings')

  async function loadMore() {
    if (nextCursor && source !== 'signals') {
      const p = new URLSearchParams(findingParams)
      p.set('cursor', nextCursor)
      const next = await apiGet<FindingsResponse>(`/findings?${p.toString()}`)
      setAppended((prev) => [...prev, ...next.data])
      setNextCursor(next.next_cursor ?? null)
    }
    if (signalsCursor && source !== 'findings') {
      const p = new URLSearchParams(signalParams)
      p.set('cursor', signalsCursor)
      const next = await apiGet<SignalsResponse>(`/signals?${p.toString()}`)
      setSignalsAppended((prev) => [...prev, ...next.data])
      setSignalsCursor(next.next_cursor ?? null)
    }
  }

  function openRow(row: UnifiedRow) {
    selectRow(row.source === 'signal' ? 'signal' : 'finding', row.id, row.title ?? undefined, {
      origin: 'findings',
    })
  }

  // Situation-level provenance deep-link (findings clusters only).
  function openSituationLineage(cluster: FindingsCluster<UnifiedRow>) {
    if (cluster.situation_id.startsWith('sit:')) {
      const sid = cluster.situation_id.slice(4)
      selectRow('situation', sid, `situation ${sid}`, { origin: 'findings' })
    } else if (cluster.latest) {
      openRow(cluster.latest)
    }
  }

  // -------- saved views --------
  function saveCurrentView() {
    const name = window.prompt('Save view as:')?.trim()
    if (!name) return
    const next = upsertView(views, { ...filter, name })
    setViews(next)
    persistSavedViews(next)
  }
  function applyView(name: string) {
    const v = views.find((x) => x.name === name)
    if (!v) return
    setFilter({
      target_id: v.target_id,
      analyst_id: v.analyst_id,
      severity: v.severity,
      sort: v.sort,
    })
  }
  function deleteView(name: string) {
    const next = removeView(views, name)
    setViews(next)
    persistSavedViews(next)
  }

  const isLoading =
    (source !== 'signals' && findingsQ.isLoading) ||
    (source !== 'findings' && signalsQ.isLoading)
  const isFetching = findingsQ.isFetching || signalsQ.isFetching
  const error =
    (findingsQ.error instanceof Error ? findingsQ.error : null) ||
    (signalsQ.error instanceof Error ? signalsQ.error : null)
  const liveCount = liveFindings.length + liveSignals.length
  const sevDisabled = source === 'signals'

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${findingRows.length} findings${
        signalRows.length ? ` · ${signalRows.length} signals` : ''
      }${hasMore ? ' · more' : ''}${liveCount ? ` · ${liveCount} live` : ''}${
        clustered && collapsed ? ` · ${collapsed} superseded collapsed` : ''
      }`}
      actions={
        <button
          onClick={() => setLive((v) => !v)}
          className={`text-label px-2 py-0.5 rounded border ${
            live
              ? connected
                ? 'border-accent-ok text-accent-ok'
                : 'border-amber-700 text-amber-400'
              : 'border-slate-700 text-slate-500'
          }`}
          title={
            live
              ? connected
                ? 'Live tail connected — click to pause (stable read)'
                : 'Live tail connecting…'
              : 'Live tail off — click to resume realtime'
          }
          data-testid="findings-tail-toggle"
        >
          {live ? (connected ? '● live' : '● connecting') : '○ live off'}
        </button>
      }
      onRefresh={() => {
        setAppended([])
        setSignalsAppended([])
        setLiveFindings([])
        setLiveSignals([])
        setNextCursor(null)
        setSignalsCursor(null)
        qc.invalidateQueries({ queryKey: ['findings-feed'] })
        qc.invalidateQueries({ queryKey: ['signals-feed'] })
        findingsQ.refetch()
        signalsQ.refetch()
      }}
    >
      {/* Compact toolbar — the old tall sparkline + multi-row filter bar fold
          into ONE thin row so the list gets the room. Primary controls (source,
          search, clustered/flat, live) stay visible; everything else (sparkline,
          target/analyst/severity/sort, saved views) tucks behind the disclosure. */}
      <div className="flex items-center gap-2 mb-2 text-label flex-wrap">
        {/* Source — gates which kinds stream (data-layer). */}
        <div
          className="inline-flex rounded border border-slate-700 overflow-hidden"
          role="group"
          aria-label="feed source"
          data-testid="findings-source-toggle"
        >
          {(['all', 'findings', 'signals'] as const).map((s, i) => (
            <button
              key={s}
              onClick={() => setSource(s)}
              className={`px-2 py-0.5 ${i > 0 ? 'border-l border-slate-700' : ''} ${
                source === s ? 'bg-surface-300 text-slate-200' : 'text-slate-500 hover:text-slate-300'
              }`}
              data-testid={`findings-source-${s}`}
              title={
                s === 'all'
                  ? 'findings + signals'
                  : s === 'findings'
                    ? 'analyst findings only'
                    : 'raw signals only'
              }
            >
              {s}
            </button>
          ))}
        </div>
        <input
          className="flex-1 min-w-[140px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="search title/body/source…"
          value={textFilter}
          onChange={(e) => setTextFilter(e.target.value)}
          data-testid="findings-text-filter"
        />
        {/* flat ⇄ clustered toggle — findings-only; "latest per situation" default */}
        <div
          className="inline-flex rounded border border-slate-700 overflow-hidden"
          role="group"
          aria-label="findings grouping"
        >
          <button
            className={`px-1.5 py-0.5 ${
              groupBySituation ? 'bg-surface-300 text-slate-200' : 'text-slate-500'
            }`}
            onClick={() => setGroupBySituation(true)}
            data-testid="findings-mode-clustered"
            title="Latest per situation — near-dup findings collapse under their situation"
          >
            clustered
          </button>
          <button
            className={`px-1.5 py-0.5 border-l border-slate-700 ${
              !groupBySituation ? 'bg-surface-300 text-slate-200' : 'text-slate-500'
            }`}
            onClick={() => setGroupBySituation(false)}
            data-testid="findings-mode-flat"
            title="Flat — every row, no situation grouping"
          >
            flat
          </button>
        </div>
        {/* keep the canonical checkbox toggle (test + a11y) — visually hidden, still wired */}
        <label className="sr-only">
          <input
            type="checkbox"
            checked={groupBySituation}
            onChange={(e) => setGroupBySituation(e.target.checked)}
            data-testid="findings-group-toggle"
          />
          latest per situation
        </label>
        {/* advanced-filters disclosure — sparkline, target/analyst/severity/sort, saved views */}
        <button
          className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border ${
            showAdvanced
              ? 'border-slate-600 text-slate-200 bg-surface-300'
              : 'border-slate-700 text-slate-400 hover:text-slate-200'
          }`}
          onClick={() => setShowAdvanced((v) => !v)}
          data-testid="findings-advanced-toggle"
          aria-expanded={showAdvanced}
          title="advanced filters, sort & saved views"
        >
          <SlidersHorizontal className="h-3 w-3" />
          filters
          {showAdvanced ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        </button>
        {isFetching && <span className="text-slate-500">loading…</span>}
      </div>

      {/* Always mounted (controls stay reachable + queryable) but visually
          collapsed via `hidden` until the disclosure is opened — so the list
          reclaims the room without losing a11y/test reach of the controls. */}
      <div className={showAdvanced ? '' : 'hidden'}>
        <div
          className="mb-2 rounded border border-slate-800 bg-surf-2 p-2 space-y-2"
          data-testid="findings-advanced"
        >
          {/* hourly volume sparkline (findings) — denser, inside the disclosure */}
          <div className="h-[44px]" data-testid="findings-sparkline">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={hourly} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="findings-spark" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#34d399" stopOpacity={0.6} />
                    <stop offset="100%" stopColor="#34d399" stopOpacity={0.05} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="label" hide />
                <YAxis hide allowDecimals={false} />
                <Tooltip
                  contentStyle={{
                    background: '#1e293b',
                    border: '1px solid #334155',
                    borderRadius: 4,
                    fontSize: 11,
                  }}
                  labelStyle={{ color: '#cbd5e1' }}
                  formatter={(value: unknown) => [String(value), 'findings']}
                />
                <Area
                  type="monotone"
                  dataKey="count"
                  stroke="#34d399"
                  strokeWidth={1.5}
                  fill="url(#findings-spark)"
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          {/* target / analyst / severity / sort filters */}
          <div className="flex items-center gap-2 text-xs flex-wrap">
            <input
              className="flex-1 min-w-[120px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
              placeholder="target_id filter…"
              value={filter.target_id}
              onChange={(e) => setFilter((f) => ({ ...f, target_id: e.target.value }))}
              data-testid="findings-target-filter"
            />
            <input
              className="flex-1 min-w-[120px] bg-surface-200 border border-slate-700 rounded p-1 px-2 disabled:opacity-40"
              placeholder="analyst_id filter…"
              value={filter.analyst_id}
              disabled={sevDisabled}
              title={sevDisabled ? 'signals carry no analyst' : undefined}
              onChange={(e) => setFilter((f) => ({ ...f, analyst_id: e.target.value }))}
              data-testid="findings-analyst-filter"
            />
            <select
              className="bg-surface-200 border border-slate-700 rounded p-1 px-2 disabled:opacity-40"
              value={filter.severity}
              disabled={sevDisabled}
              title={sevDisabled ? 'signals carry no severity' : undefined}
              onChange={(e) => setFilter((f) => ({ ...f, severity: e.target.value }))}
              data-testid="findings-severity-filter"
            >
              {SEVERITY_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  severity: {s}
                </option>
              ))}
            </select>
            <select
              className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
              value={filter.sort}
              onChange={(e) => setFilter((f) => ({ ...f, sort: e.target.value as SortMode }))}
              data-testid="findings-sort"
            >
              <option value="recency">sort: recency</option>
              <option value="severity">sort: severity</option>
            </select>
          </div>

          {/* saved views */}
          <div className="flex items-center gap-2 text-label flex-wrap">
            <span className="text-slate-500">views:</span>
            {views.length === 0 && <span className="text-slate-600">none saved</span>}
            {views.map((v) => (
              <span
                key={v.name}
                className="inline-flex items-center gap-1 bg-surface-200 border border-slate-700 rounded px-1.5 py-0.5"
                data-testid={`findings-view-${v.name}`}
              >
                <button
                  className="text-slate-300 hover:text-slate-100"
                  onClick={() => applyView(v.name)}
                  data-testid={`findings-view-apply-${v.name}`}
                >
                  {v.name}
                </button>
                <button
                  className="text-slate-600 hover:text-rose-400"
                  onClick={() => deleteView(v.name)}
                  title="delete view"
                  data-testid={`findings-view-delete-${v.name}`}
                >
                  ×
                </button>
              </span>
            ))}
            <button
              className="text-slate-400 hover:text-slate-200 underline"
              onClick={saveCurrentView}
              data-testid="findings-save-view"
            >
              + save view
            </button>
            {source === 'signals' && (
              <span
                className="text-slate-600 ml-auto"
                title="Clustering groups findings; signals are atomic"
              >
                (clustering applies to findings)
              </span>
            )}
            {source !== 'signals' && !clustered && groupBySituation && findingRows.length > 0 && (
              <span
                className="text-slate-600 ml-auto"
                title="No situation-bearing findings on this page"
              >
                (flat — no clustering data)
              </span>
            )}
          </div>
        </div>
      </div>

      {isLoading && <div className="text-slate-500 text-sm">loading feed…</div>}
      {error && <div className="text-rose-400 text-sm">error: {error.message}</div>}

      <div className="flex-1 overflow-auto space-y-1 text-xs" data-testid="findings-list">
        {(() => {
          // Running 1-based index for the tight numbered feed (the old at-a-glance
          // row number). Counts every rendered row top-to-bottom — flat rows, each
          // cluster's canonical row, then the signals tail — so the numbers read
          // as one continuous list regardless of clustering.
          let rowNo = 0
          return clusters.map((cluster) =>
            cluster.flat ? (
              <div
                key={cluster.situation_id}
                data-testid={`findings-cluster-${cluster.situation_id}`}
                className="space-y-1"
              >
                {cluster.rows.map((row) => (
                  <FeedCard
                    key={rowDedupKey(row)}
                    row={row}
                    index={++rowNo}
                    onOpen={() => openRow(row)}
                  />
                ))}
              </div>
            ) : (
              <ClusterBlock
                key={cluster.situation_id}
                cluster={cluster}
                index={++rowNo}
                onOpenRow={openRow}
                onOpenSituation={openSituationLineage}
              />
            ),
          )
        })()}

        {/* signals flat block — only when clustering findings (Cluster ON);
            when flat, signals already interleave in the single flat cluster. */}
        {groupBySituation && signalRows.length > 0 && (
          <div className="space-y-1" data-testid="findings-signals-flat">
            <div className="text-label text-ink-3 uppercase tracking-wide pt-1">
              signals ({signalRows.length})
            </div>
            {signalRows.map((row, i) => (
              <FeedCard
                key={rowDedupKey(row)}
                row={row}
                index={i + 1}
                onOpen={() => openRow(row)}
              />
            ))}
          </div>
        )}

        {rows.length === 0 && !isLoading && (
          <div className="text-slate-500 text-sm py-4 text-center">no items match filters</div>
        )}
      </div>

      {hasMore && (
        <div className="border-t border-slate-800 pt-2 mt-2">
          <button
            onClick={loadMore}
            className="w-full bg-surface-200 hover:bg-surface-300 border border-slate-700 rounded p-1 text-xs"
            data-testid="findings-load-more"
          >
            load more
          </button>
        </div>
      )}
    </PanelChrome>
  )
}

/**
 * One situation cluster: the canonical/latest finding shown, with the
 * superseded near-dups collapsed under a per-cluster expander.
 */
function ClusterBlock({
  cluster,
  index,
  onOpenRow,
  onOpenSituation,
}: {
  cluster: FindingsCluster<UnifiedRow>
  index?: number
  onOpenRow: (row: UnifiedRow) => void
  onOpenSituation: (cluster: FindingsCluster<UnifiedRow>) => void
}) {
  const [open, setOpen] = useState(false)
  const history = cluster.history ?? []
  const latest = cluster.latest ?? cluster.rows[0]
  const explicit = cluster.situation_id.startsWith('sit:')
  const label = explicit
    ? cluster.situation_id.slice(4)
    : cluster.situation_id.replace(/^sig:/, '')

  // Lightened: a thin left accent (coloured by the canonical row's severity) +
  // a compact one-line header — no nested bordered card (no boxes-in-boxes).
  return (
    <div
      data-testid={`findings-cluster-${cluster.situation_id}`}
      className={`border-l-2 ${severityRailClass(latest.severity)} pl-2`}
    >
      <div className="flex items-center gap-2 text-label pb-0.5">
        <button
          className="text-accent-info hover:text-blue-300 font-mono truncate max-w-[40%] text-left"
          onClick={() => onOpenSituation(cluster)}
          title="open situation provenance"
          data-testid={`findings-cluster-header-${cluster.situation_id}`}
        >
          {explicit ? '◆' : '≈'} {label}
        </button>
        {cluster.confirmed ? (
          <span
            className="rounded px-1 bg-emerald-950 text-emerald-300"
            title={`P-FS confirmed${cluster.score != null ? ` · score ${cluster.score.toFixed(2)}` : ''}${
              cluster.reason ? ` · ${cluster.reason}` : ''
            }`}
            data-testid={`findings-cluster-confirmed-${cluster.situation_id}`}
          >
            superseded ✓
          </span>
        ) : (
          <span
            className="rounded px-1 bg-slate-800 text-slate-400"
            title="grouped client-side by shared situation signature (P-FS summary not on this page)"
          >
            grouped
          </span>
        )}
        <span className="text-slate-500 ml-auto">latest of {cluster.rows.length}</span>
      </div>

      <FeedCard row={latest} index={index} onOpen={() => onOpenRow(latest)} />

      {history.length > 0 && (
        <div className="pt-0.5">
          <button
            className="w-full text-left text-label text-ink-2 hover:text-ink-1 py-0.5"
            onClick={() => setOpen((v) => !v)}
            data-testid={`findings-cluster-history-toggle-${cluster.situation_id}`}
            aria-expanded={open}
          >
            {open ? '▾' : '▸'} {history.length} superseded finding
            {history.length === 1 ? '' : 's'} (history)
          </button>
          {open && (
            <div
              className="space-y-1 pl-3 border-l border-slate-800 mt-1 opacity-80"
              data-testid={`findings-cluster-history-${cluster.situation_id}`}
            >
              {history.map((row) => (
                <FeedCard key={rowDedupKey(row)} row={row} superseded onOpen={() => onOpenRow(row)} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * One feed row — a finding OR a signal. Branches on `row.source`: a signal is a
 * slim variant (SIGNAL tag, source + geo/tag chips, no analyst, confidence only
 * when present, no severity badge). A finding keeps the full card.
 */
function FeedCard({
  row,
  index,
  onOpen,
  superseded = false,
}: {
  row: UnifiedRow
  index?: number
  onOpen: () => void
  superseded?: boolean
}) {
  const isSignal = row.source === 'signal'
  // The tight numbered-row layout: a left severity colour rail + muted row index,
  // a bold title line that carries the at-a-glance scan, and ONE muted meta line
  // (target/analyst or source/geo) with a relative-time stamp on the right.
  return (
    <button
      onClick={onOpen}
      className={`group w-full text-left bg-surf-2 hover:bg-surf-1 border-l-2 ${severityRailClass(
        row.severity,
      )} pl-2 pr-2 py-1 cursor-pointer block text-body`}
      data-testid={isSignal ? `signal-${row.id}` : `finding-${row.id}`}
    >
      {/* title row: index · badges · bold title … relative time */}
      <div className="flex items-baseline gap-2">
        {index != null && (
          <span className="shrink-0 text-ink-3 text-label tabular-nums w-6 text-right">
            {index}.
          </span>
        )}
        {row.live && (
          <span
            className="shrink-0 self-center rounded px-1 text-label bg-emerald-900 text-emerald-200"
            data-testid={`${isSignal ? 'signal' : 'finding'}-live-${row.id}`}
          >
            live
          </span>
        )}
        {isSignal && (
          <span
            className="shrink-0 self-center rounded px-1 text-label bg-sky-950 text-sky-300 uppercase tracking-wide"
            data-testid={`signal-badge-${row.id}`}
          >
            signal
          </span>
        )}
        {superseded && (
          <span
            className="shrink-0 self-center rounded px-1 text-label bg-surf-1 text-ink-3"
            title="superseded by a newer finding for this situation"
            data-testid={`finding-superseded-${row.id}`}
          >
            superseded
          </span>
        )}
        <span
          className={`min-w-0 flex-1 truncate font-semibold ${
            superseded ? 'text-ink-2 line-through' : 'text-ink-1'
          }`}
          title={row.title ?? ''}
        >
          {row.title}
        </span>
        {row.severity && (
          <SeverityBadge severity={row.severity as Severity} className="shrink-0 self-center" />
        )}
        <span
          className="shrink-0 self-center text-ink-3 text-label"
          title={new Date(row.produced_at).toLocaleString()}
        >
          {relativeTime(row.produced_at)}
        </span>
      </div>

      {/* ONE muted meta line: target/analyst (findings) or source/geo (signals) */}
      <div className="mt-0.5 flex items-center gap-2 text-label text-ink-3 overflow-hidden whitespace-nowrap">
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
            {row.confidence !== null && <span className="shrink-0">· c={row.confidence.toFixed(2)}</span>}
            {row.derived_from.length > 0 && (
              <span className="shrink-0">
                · ←{row.derived_from.length} input{row.derived_from.length === 1 ? '' : 's'}
              </span>
            )}
          </>
        )}
      </div>

      {row.body && <div className="mt-0.5 text-ink-2 line-clamp-2">{row.body}</div>}
    </button>
  )
}
