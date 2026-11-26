/**
 * The time band's module. The band itself lives in `TimeBand.tsx`; this file
 * keeps its default export (the frame mounts it from here) and the read's
 * temporal lens, which shares no state with the band.
 */
import { useMemo } from 'react'
import {
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { selectRow } from '@/state/selection'
import {
  BAND_LABELS,
  KIND_COLOR,
  findingMarkColor,
  pointOpacity,
  timeDomain,
  type TimelinePoint,
} from '@/lib/timelinePoints'

export { default } from './TimeBand'

// ---------------------------------------------------------------------------
// ReadTimelineLens — the TEMPORAL half of the read's lens.
//
// A self-contained banded scatter (signal / finding / situation bands on Y,
// event time on X) over ONE country/finding read's evidence — NOT the World
// room's playback window. The directly-cited evidence points are emphasised
// (full recency opacity); the rest of the country's activity is faded context.
// Clicking a point `selectRow`s the underlying row, brushing every room (and
// re-opening it in the Inspector). Built on the same pure `@/lib/timelinePoints`
// transforms the Target Timeline uses.
// ---------------------------------------------------------------------------

function fmtLensAxis(ms: number): string {
  const d = new Date(ms)
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString(
    undefined,
    { hour: '2-digit', minute: '2-digit' },
  )}`
}

function LensTooltip({ active, payload }: { active?: boolean; payload?: unknown[] }) {
  if (!active || !payload || payload.length === 0) return null
  const p = (payload[0] as { payload: TimelinePoint }).payload
  return (
    <div className="max-w-xs rounded border border-line bg-surface-100 p-2 text-label">
      <div className="truncate font-medium text-ink-1">{p.title}</div>
      <div className="mt-1 text-ink-3">
        {p.kind} · {new Date(p.ts).toLocaleString()}
      </div>
      {p.subtitle && <div className="mt-1 truncate text-ink-3">{p.subtitle}</div>}
    </div>
  )
}

export function ReadTimelineLens({
  points,
  evidenceIds,
  selectedId,
}: {
  points: TimelinePoint[]
  evidenceIds: Set<string>
  selectedId?: string | null
}) {
  const nowMs = Date.now()
  const sigPts = useMemo(() => points.filter((p) => p.kind === 'signal'), [points])
  const findPts = useMemo(() => points.filter((p) => p.kind === 'finding'), [points])
  const sitPts = useMemo(() => points.filter((p) => p.kind === 'situation'), [points])
  const xDomain = useMemo(() => timeDomain(points), [points])

  // Emphasis: a cited-evidence point keeps its recency opacity; everything else
  // fades into context. A selected point gets a white ring.
  const opacityFor = (p: TimelinePoint) =>
    evidenceIds.has(p.id) ? pointOpacity(p.ts, nowMs) : 0.22
  const strokeFor = (p: TimelinePoint) => (p.id === selectedId ? '#ffffff' : undefined)
  const onPick = (d: unknown) => {
    const p = d as TimelinePoint
    selectRow(p.kind, p.id, p.title, { origin: 'read-timeline-lens' })
  }

  if (points.length === 0) {
    return (
      <div
        className="flex h-full w-full items-center justify-center bg-surface-300 px-4 text-center text-sm text-ink-3"
        data-testid="read-timeline-lens-empty"
      >
        No timeline evidence for this read.
      </div>
    )
  }

  return (
    <div className="h-full w-full bg-surface-300" data-testid="read-timeline-lens">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 10, right: 16, bottom: 22, left: 56 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.4} />
          <XAxis
            type="number"
            dataKey="ts"
            domain={xDomain ?? ['auto', 'auto']}
            tickFormatter={fmtLensAxis}
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
          <RTooltip content={<LensTooltip />} cursor={{ stroke: '#475569', strokeDasharray: '3 3' }} />
          <Scatter name="signals" data={sigPts} fill={KIND_COLOR.signal} onClick={onPick}>
            {sigPts.map((p) => (
              <Cell
                key={p.id}
                fillOpacity={opacityFor(p)}
                stroke={strokeFor(p)}
                strokeWidth={p.id === selectedId ? 2 : 0}
              />
            ))}
          </Scatter>
          <Scatter name="findings" data={findPts} fill={KIND_COLOR.finding} onClick={onPick}>
            {findPts.map((p) => (
              <Cell
                key={p.id}
                fill={findingMarkColor(p.severity)}
                fillOpacity={opacityFor(p)}
                stroke={strokeFor(p)}
                strokeWidth={p.id === selectedId ? 2 : 0}
              />
            ))}
          </Scatter>
          <Scatter name="situations" data={sitPts} fill={KIND_COLOR.situation} onClick={onPick}>
            {sitPts.map((p) => (
              <Cell
                key={p.id}
                fillOpacity={opacityFor(p)}
                stroke={strokeFor(p)}
                strokeWidth={p.id === selectedId ? 2 : 0}
              />
            ))}
          </Scatter>
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  )
}
