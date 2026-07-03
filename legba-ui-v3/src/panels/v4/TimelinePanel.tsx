/**
 * v4 panel — the global Timeline lanes (S7-T2).
 *
 * A cross-desk banded timeline over the live substrate: signals / findings /
 * situations on the Y bands, event time on X, signal marks colored by CATEGORY
 * (conflict / political / economic / disaster / health / technology …) so the
 * lanes read like the original mission-control timeline. Brushable via the
 * recharts Brush (T6 upgrades to aggregate-then-zoom category lanes). Clicking a
 * mark drives the shared selection store, brushing every other panel.
 *
 * Self-fetching (like the KPI strip) so it can dock anywhere without a binding.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Brush,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { PanelBoundary } from './PanelBoundary'
import { apiGet, ApiError } from '@/lib/api'
import { selectRow } from '@/state/selection'
import {
  BAND,
  BAND_LABELS,
  KIND_COLOR,
  findingMarkColor,
  pointOpacity,
  timeDomain,
  type TimelinePoint,
} from '@/lib/timelinePoints'

/** Signal-category → lane color (matches the original mission-control legend). */
const CATEGORY_COLOR: Record<string, string> = {
  conflict: '#ef4444',
  political: '#f59e0b',
  economic: '#10b981',
  disaster: '#a855f7',
  health: '#ec4899',
  technology: '#38bdf8',
  environment: '#22c55e',
  social: '#eab308',
}

interface Page<T> {
  data: T[]
}
interface SignalRow {
  id: string
  title?: string | null
  category?: string | null
  produced_at?: string | null
  event_timestamp?: string | null
  data?: { title?: string | null } | null
}
interface FindingRow {
  id: string
  title?: string | null
  severity?: string | null
  produced_at: string
}
interface SituationRow {
  id: string
  title: string
  state?: string | null
  produced_at?: string | null
  created_at?: string | null
}

async function getPage<T>(path: string): Promise<T[]> {
  try {
    const res = await apiGet<Page<T>>(path)
    return res.data ?? []
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return []
    throw e
  }
}
async function getList<T>(path: string): Promise<T[]> {
  try {
    const res = await apiGet<T[]>(path)
    return Array.isArray(res) ? res : []
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return []
    throw e
  }
}

function tsOf(...iso: Array<string | null | undefined>): number {
  for (const v of iso) {
    if (!v) continue
    const t = Date.parse(v)
    if (!Number.isNaN(t)) return t
  }
  return NaN
}

/** Extend a TimelinePoint with the signal category for lane coloring. */
interface LanePoint extends TimelinePoint {
  category?: string
}

export default function TimelinePanel() {
  const signals = useQuery({
    queryKey: ['timeline-signals'],
    queryFn: () => getPage<SignalRow>('/signals?limit=500'),
    refetchInterval: 60_000,
  })
  const findings = useQuery({
    queryKey: ['timeline-findings'],
    queryFn: () => getPage<FindingRow>('/findings?limit=200'),
    refetchInterval: 60_000,
  })
  const situations = useQuery({
    queryKey: ['timeline-situations'],
    queryFn: () => getList<SituationRow>('/situations'),
    refetchInterval: 60_000,
  })

  const points = useMemo<LanePoint[]>(() => {
    const out: LanePoint[] = []
    for (const s of signals.data ?? []) {
      const ts = tsOf(s.event_timestamp, s.produced_at)
      if (!Number.isFinite(ts)) continue
      const category = (s.category ?? '').toLowerCase()
      out.push({
        id: s.id,
        title: s.title ?? s.data?.title ?? '(untitled)',
        ts,
        band: BAND.signal,
        kind: 'signal',
        subtitle: category,
        category,
      })
    }
    for (const f of findings.data ?? []) {
      const ts = tsOf(f.produced_at)
      if (!Number.isFinite(ts)) continue
      out.push({
        id: f.id,
        title: f.title ?? '(untitled)',
        ts,
        band: BAND.finding,
        kind: 'finding',
        subtitle: f.severity ?? '',
        severity: f.severity,
      })
    }
    for (const s of situations.data ?? []) {
      const ts = tsOf(s.produced_at, s.created_at)
      if (!Number.isFinite(ts)) continue
      out.push({
        id: s.id,
        title: s.title,
        ts,
        band: BAND.situation,
        kind: 'situation',
        subtitle: s.state ?? 'active',
      })
    }
    return out
  }, [signals.data, findings.data, situations.data])

  const sigPts = useMemo(() => points.filter((p) => p.kind === 'signal'), [points])
  const findPts = useMemo(() => points.filter((p) => p.kind === 'finding'), [points])
  const sitPts = useMemo(() => points.filter((p) => p.kind === 'situation'), [points])
  const xDomain = useMemo(() => timeDomain(points), [points])
  const nowMs = Date.now()

  const laneColor = (p: LanePoint) =>
    (p.category && CATEGORY_COLOR[p.category]) || KIND_COLOR.signal
  const onPick = (d: unknown) => {
    const p = d as LanePoint
    selectRow(p.kind, p.id, p.title, { origin: 'timeline' })
  }

  const isLoading = signals.isLoading || findings.isLoading || situations.isLoading

  return (
    <PanelBoundary>
      <div className="flex h-full w-full flex-col bg-surface-300" data-testid="global-timeline">
        <div className="flex shrink-0 items-center justify-between border-b border-slate-800 px-3 py-1.5 text-[11px] text-slate-400">
          <span className="font-medium text-slate-300">Timeline</span>
          <span className="flex items-center gap-3">
            <LegendDot color={KIND_COLOR.signal} label="signals" />
            <LegendDot color={KIND_COLOR.finding} label="findings" />
            <LegendDot color={KIND_COLOR.situation} label="situations" />
            <span className="tabular-nums text-slate-500">{points.length} events</span>
          </span>
        </div>
        {points.length === 0 ? (
          <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
            {isLoading ? 'Loading timeline…' : 'No recent timeline events.'}
          </div>
        ) : (
          <div className="min-h-0 flex-1">
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 10, right: 18, bottom: 4, left: 56 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.35} />
                <XAxis
                  type="number"
                  dataKey="ts"
                  domain={xDomain ?? ['auto', 'auto']}
                  tickFormatter={fmtAxis}
                  stroke="#94a3b8"
                  fontSize={10}
                  scale="time"
                />
                <YAxis
                  type="number"
                  dataKey="band"
                  domain={[0.5, 3.5]}
                  ticks={[1, 2, 3]}
                  tickFormatter={(v: number) => BAND_LABELS[v] ?? ''}
                  stroke="#94a3b8"
                  fontSize={10}
                  width={52}
                />
                <RTooltip content={<TLTooltip />} cursor={{ stroke: '#475569', strokeDasharray: '3 3' }} />
                <Scatter name="signals" data={sigPts} onClick={onPick}>
                  {sigPts.map((p) => (
                    <Cell key={p.id} fill={laneColor(p)} fillOpacity={pointOpacity(p.ts, nowMs)} />
                  ))}
                </Scatter>
                <Scatter name="findings" data={findPts} onClick={onPick}>
                  {findPts.map((p) => (
                    <Cell key={p.id} fill={findingMarkColor(p.severity)} fillOpacity={pointOpacity(p.ts, nowMs)} />
                  ))}
                </Scatter>
                <Scatter name="situations" data={sitPts} onClick={onPick}>
                  {sitPts.map((p) => (
                    <Cell key={p.id} fill={KIND_COLOR.situation} fillOpacity={pointOpacity(p.ts, nowMs)} />
                  ))}
                </Scatter>
                <Brush dataKey="ts" height={16} travellerWidth={8} stroke="#475569" fill="#1e293b" tickFormatter={fmtAxis} />
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </PanelBoundary>
  )
}

function fmtAxis(ms: number): string {
  const d = new Date(ms)
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString(
    undefined,
    { hour: '2-digit', minute: '2-digit' },
  )}`
}

function TLTooltip({ active, payload }: { active?: boolean; payload?: unknown[] }) {
  if (!active || !payload || payload.length === 0) return null
  const p = (payload[0] as { payload: LanePoint }).payload
  return (
    <div className="max-w-xs rounded border border-slate-700 bg-surface-100 p-2 text-[11px]">
      <div className="truncate font-medium text-slate-200">{p.title}</div>
      <div className="mt-1 text-slate-500">
        {p.kind}
        {p.subtitle ? ` · ${p.subtitle}` : ''} · {new Date(p.ts).toLocaleString()}
      </div>
    </div>
  )
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1 text-slate-500">
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      {label}
    </span>
  )
}
