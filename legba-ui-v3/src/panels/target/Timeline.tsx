/**
 * T9. Target Timeline (`target.timeline`) — UI-3 (Tier B) real lifecycle view.
 *
 * Chronological view for the bound target, banded by row kind:
 *   - signals (source emissions)
 *   - findings (analyst-output emission marks)
 *   - situations (lifecycle: open → last-event, rendered as spans + marks)
 *
 * Built on recharts `ScatterChart` (banded Y, time on X) with situation
 * lifecycle drawn as `ReferenceArea` spans on the situation band, so the
 * operator can eyeball burst patterns AND see which window each situation
 * was live in relative to the signals/findings that fed it.
 *
 * All point/span derivation lives in `@/lib/timelinePoints` (pure +
 * unit-tested). Click a point → `legba:open-lineage`.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import {
  CartesianGrid,
  Cell,
  ReferenceArea,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import { useDockviewTileRedraw } from '@/components/useTileRedraw'
import type { PanelProps } from '@/types'
import { selectRow } from '@/state/selection'
import {
  BAND_LABELS,
  EVENT_LIFECYCLE_COLOR,
  KIND_COLOR,
  SEVERITY_COLOR,
  eventPoints,
  eventSpans,
  findingMarkColor,
  findingPoints,
  pointOpacity,
  signalPoints,
  situationPoints,
  situationSpans,
  spanOpacity,
  timeDomain,
  type TLEvent,
  type TLFinding,
  type TLSignal,
  type TLSituation,
  type TimelineKind,
  type TimelinePoint,
} from '@/lib/timelinePoints'

interface Page<T> {
  data: T[]
  next_cursor: string | null
}

function openLineage(kind: TimelineKind, id: string, title: string, ts?: number) {
  // Redesign Move 2: unified selection store → opens the Inspector + brushes
  // every room (was a legacy window event firing into the void).
  // `ts` (WORKSTATION_V2_FLOW_DESIGN §3 enabler 1) carries the record's own
  // instant so any OTHER timeline can center on it without a fetch.
  selectRow(kind, id, title, { origin: 'timeline', preview: { ts } })
}

function fmtTimeAxis(ms: number): string {
  const d = new Date(ms)
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString(
    undefined,
    { hour: '2-digit', minute: '2-digit' },
  )}`
}

function CustomTooltip({ active, payload }: { active?: boolean; payload?: unknown[] }) {
  if (!active || !payload || payload.length === 0) return null
  const p = (payload[0] as { payload: TimelinePoint }).payload
  return (
    <div className="bg-surface-100 border border-slate-700 rounded p-2 text-[11px] max-w-xs">
      <div className="text-slate-200 font-medium truncate">{p.title}</div>
      <div className="text-slate-500 mt-1">
        {p.kind} · {new Date(p.ts).toLocaleString()}
      </div>
      {p.subtitle && <div className="text-slate-400 mt-1 truncate">{p.subtitle}</div>}
      <div className="text-slate-600 font-mono mt-1 truncate">{p.id}</div>
    </div>
  )
}

export default function TargetTimelinePanel({ registration, scope }: PanelProps) {
  const target_id = scope.target_id ?? registration.descriptor_id

  // Background-tab fix (P0-2f): recharts' ResponsiveContainer measures a
  // zero-size box when the panel mounts into an inactive Dockview tab and
  // renders a blank chart on activation. Remount it (key=tick) each time the
  // tile becomes visible so it re-measures the real box.
  const redrawTick = useDockviewTileRedraw()

  const signalsQ = useQuery<Page<TLSignal>>({
    enabled: !!target_id,
    queryKey: ['target-timeline-signals', target_id],
    queryFn: () =>
      apiGet<Page<TLSignal>>(`/signals?target_id=${encodeURIComponent(target_id)}&limit=200`),
    refetchInterval: 60_000,
  })

  const findingsQ = useQuery<Page<TLFinding>>({
    enabled: !!target_id,
    queryKey: ['target-timeline-findings', target_id],
    queryFn: () =>
      apiGet<Page<TLFinding>>(`/findings?target_id=${encodeURIComponent(target_id)}&limit=100`),
    refetchInterval: 60_000,
  })

  const situationsQ = useQuery<Page<TLSituation>>({
    enabled: !!target_id,
    queryKey: ['target-timeline-situations', target_id],
    queryFn: async () => {
      try {
        return await apiGet<Page<TLSituation>>(
          `/situations?target_id=${encodeURIComponent(target_id)}&limit=100`,
        )
      } catch (e) {
        // Situations may 404 on older substrate — degrade to no lifecycle band.
        if (e instanceof ApiError && e.status === 404) return { data: [], next_cursor: null }
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  // V3/P6 — bounded occurrences on their own band (spans time_start→time_end,
  // lifecycle state as the badge). 404 = the route predates this deployment —
  // degrade to no event band exactly like the situations band.
  const eventsQ = useQuery<Page<TLEvent>>({
    enabled: !!target_id,
    queryKey: ['target-timeline-events', target_id],
    queryFn: async () => {
      try {
        return await apiGet<Page<TLEvent>>(
          `/v3/events?target_id=${encodeURIComponent(target_id)}&limit=100`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: [], next_cursor: null }
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  const nowMs = Date.now() // recency-fade reference for the dot bands
  const sigPts = useMemo(() => signalPoints(signalsQ.data?.data ?? []), [signalsQ.data])
  const findPts = useMemo(() => findingPoints(findingsQ.data?.data ?? []), [findingsQ.data])
  const sitPts = useMemo(() => situationPoints(situationsQ.data?.data ?? []), [situationsQ.data])
  const spans = useMemo(() => situationSpans(situationsQ.data?.data ?? []), [situationsQ.data])
  const evPts = useMemo(() => eventPoints(eventsQ.data?.data ?? []), [eventsQ.data])
  const evSpans = useMemo(
    () => eventSpans(eventsQ.data?.data ?? [], nowMs),
    [eventsQ.data, nowMs],
  )

  const xDomain = useMemo(
    () => timeDomain(
      [...sigPts, ...findPts, ...sitPts, ...evPts],
      [...spans, ...evSpans],
    ),
    [sigPts, findPts, sitPts, evPts, spans, evSpans],
  )

  const totalPoints = sigPts.length + findPts.length + sitPts.length + evPts.length
  const loading =
    signalsQ.isLoading || findingsQ.isLoading || situationsQ.isLoading || eventsQ.isLoading

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${sigPts.length} signals · ${findPts.length} findings · ${sitPts.length} situations · ${evPts.length} events · target ${target_id}`}
      onRefresh={() => {
        signalsQ.refetch()
        findingsQ.refetch()
        situationsQ.refetch()
        eventsQ.refetch()
      }}
    >
      <div className="flex-1 flex flex-col min-h-[280px]">
        {totalPoints === 0 ? (
          <div className="text-slate-500 text-sm py-4 text-center" data-testid="target-timeline-empty">
            {loading ? 'loading timeline…' : 'no signals, findings, situations, or events for this target yet'}
          </div>
        ) : (
          <div className="flex-1 min-h-[260px]" data-testid="target-timeline-chart">
            <ResponsiveContainer key={redrawTick} width="100%" height="100%">
              <ScatterChart margin={{ top: 12, right: 16, bottom: 28, left: 64 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.4} />
                <XAxis
                  type="number"
                  dataKey="ts"
                  domain={xDomain ?? ['auto', 'auto']}
                  tickFormatter={fmtTimeAxis}
                  stroke="#94a3b8"
                  fontSize={10}
                  scale="time"
                />
                <YAxis
                  type="number"
                  dataKey="band"
                  domain={[0.5, 4.5]}
                  ticks={[1, 2, 3, 4]}
                  tickFormatter={(v: number) => BAND_LABELS[v] ?? ''}
                  stroke="#94a3b8"
                  fontSize={10}
                  width={56}
                />
                <Tooltip content={<CustomTooltip />} cursor={{ stroke: '#475569', strokeDasharray: '3 3' }} />

                {/* Situation lifecycle spans on the situation band (3). The
                    span fades with the situation's decaying intensity + status
                    (active→dormant→closed) so it visibly "comes and goes". */}
                {spans.map((s) => {
                  const o = spanOpacity(s)
                  return (
                    <ReferenceArea
                      key={`span-${s.id}`}
                      x1={s.start}
                      x2={s.end}
                      y1={2.78}
                      y2={3.22}
                      fill={KIND_COLOR.situation}
                      fillOpacity={Math.round(o * 0.4 * 100) / 100}
                      stroke={KIND_COLOR.situation}
                      strokeOpacity={o}
                    />
                  )
                })}

                {/* V3/P6 — event occurrence spans on the event band (4):
                    time_start → time_end, colored by the lifecycle-state
                    badge. An open event (no time_end) resolves to now with a
                    dashed stroke — the ongoing bar, never a fabricated end. */}
                {evSpans.map((e) => {
                  const c = EVENT_LIFECYCLE_COLOR[e.lifecycle] ?? KIND_COLOR.event
                  return (
                    <ReferenceArea
                      key={`evspan-${e.id}`}
                      x1={e.start}
                      x2={e.end}
                      y1={3.78}
                      y2={4.22}
                      fill={c}
                      fillOpacity={e.open ? 0.25 : 0.4}
                      stroke={c}
                      strokeOpacity={e.open ? 0.6 : 0.9}
                      strokeDasharray={e.open ? '4 3' : undefined}
                    />
                  )
                })}

                <Scatter
                  name="signals"
                  data={sigPts}
                  fill={KIND_COLOR.signal}
                  onClick={(d: unknown) => {
                    const p = d as TimelinePoint
                    openLineage('signal', p.id, p.title, p.ts)
                  }}
                >
                  {/* Recency fade — older signal marks dim so the band reads as
                      events arriving + fading, not an accumulating wall. */}
                  {sigPts.map((p) => (
                    <Cell key={p.id} fillOpacity={pointOpacity(p.ts, nowMs)} />
                  ))}
                </Scatter>
                <Scatter
                  name="findings"
                  data={findPts}
                  fill={KIND_COLOR.finding}
                  onClick={(d: unknown) => {
                    const p = d as TimelinePoint
                    openLineage('finding', p.id, p.title, p.ts)
                  }}
                >
                  {/* Severity-coloured finding marks (analyst-output overlay),
                      recency-faded like the signal band. */}
                  {findPts.map((p) => (
                    <Cell
                      key={p.id}
                      fill={findingMarkColor(p.severity)}
                      fillOpacity={pointOpacity(p.ts, nowMs)}
                    />
                  ))}
                </Scatter>
                <Scatter
                  name="situations"
                  data={sitPts}
                  fill={KIND_COLOR.situation}
                  onClick={(d: unknown) => {
                    const p = d as TimelinePoint
                    openLineage('situation', p.id, p.title, p.ts)
                  }}
                />
                <Scatter
                  name="events"
                  data={evPts}
                  fill={KIND_COLOR.event}
                  onClick={(d: unknown) => {
                    const p = d as TimelinePoint
                    openLineage('event', p.id, p.title, p.ts)
                  }}
                >
                  {/* Lifecycle badge color per event mark (the same map the
                      span stroke uses). */}
                  {evPts.map((p) => (
                    <Cell
                      key={p.id}
                      fill={EVENT_LIFECYCLE_COLOR[p.subtitle] ?? KIND_COLOR.event}
                    />
                  ))}
                </Scatter>
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        )}

        <div className="text-[10px] text-slate-500 mt-2 flex gap-3 px-1 flex-wrap items-center">
          <Legend color={KIND_COLOR.signal} label="signal" />
          <span className="flex items-center gap-1">
            <span>finding</span>
            <Legend color={SEVERITY_COLOR.critical} label="crit" />
            <Legend color={SEVERITY_COLOR.high} label="high" />
            <Legend color={SEVERITY_COLOR.medium} label="med" />
            <Legend color={SEVERITY_COLOR.low} label="low" />
            <Legend color={KIND_COLOR.finding} label="n/a" />
          </span>
          <Legend color={KIND_COLOR.situation} label="situation (band = lifecycle span)" />
          <Legend color={KIND_COLOR.event} label="event (band = occurrence span · lifecycle badge)" />
          <span className="opacity-60">click a point to open lineage</span>
        </div>
      </div>
    </PanelChrome>
  )
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="w-2 h-2 rounded-full" style={{ background: color }} />
      {label}
    </span>
  )
}
