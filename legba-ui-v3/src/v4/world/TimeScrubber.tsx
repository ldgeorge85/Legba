/**
 * TimeScrubber — compact bottom bar (~44px) that drives the World's time window.
 *
 * Reads the orchestrator-owned world store. A range slider over
 * [windowStartMs (≈now−24h), now] is bound to windowEndMs; the map renders
 * events up to time T = windowEndMs. Play advances T forward in wall-clock-ish
 * ticks until it reaches "now", then auto-stops at LIVE.
 */
import { useEffect, useMemo, useRef } from 'react'
import { Play, Pause } from 'lucide-react'
import { format } from 'date-fns'
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
import { cn } from '@/lib/cn'
import { useWorldState } from './worldState'
import { selectRow } from '@/state/selection'
import {
  BAND_LABELS,
  KIND_COLOR,
  findingMarkColor,
  pointOpacity,
  timeDomain,
  type TimelinePoint,
} from '@/lib/timelinePoints'

/** Window is "live" when its end is within ~1 minute of now. */
const LIVE_THRESHOLD_MS = 60_000
/** Each ~1s playback tick advances the window by speed × 2 simulated minutes. */
const TICK_INTERVAL_MS = 1_000
const TICK_BASE_MS = 120_000
const SPEEDS = [0.5, 1, 2, 4] as const

export default function TimeScrubber() {
  const windowStartMs = useWorldState((s) => s.windowStartMs)
  const windowEndMs = useWorldState((s) => s.windowEndMs)
  const playing = useWorldState((s) => s.playing)
  const speed = useWorldState((s) => s.speed)
  const setWindow = useWorldState((s) => s.setWindow)
  const setPlaying = useWorldState((s) => s.setPlaying)
  const setSpeed = useWorldState((s) => s.setSpeed)

  // Playback loop. Reads live store values inside the tick so it doesn't
  // re-subscribe (and reset its interval) on every window change.
  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => {
      const s = useWorldState.getState()
      const now = Date.now()
      const next = s.windowEndMs + s.speed * TICK_BASE_MS
      if (next >= now) {
        s.setWindow(s.windowStartMs, now)
        s.setPlaying(false)
      } else {
        s.setWindow(s.windowStartMs, next)
      }
    }, TICK_INTERVAL_MS)
    return () => clearInterval(id)
  }, [playing])

  // Keep "now" fresh for the slider's max bound across renders.
  const nowRef = useRef(Date.now())
  nowRef.current = Math.max(nowRef.current, Date.now(), windowEndMs)
  const now = nowRef.current

  const isLive = now - windowEndMs <= LIVE_THRESHOLD_MS

  return (
    <div
      className={cn(
        'flex h-11 w-full items-center gap-3 border-t border-slate-800',
        'bg-surface-200 px-3 text-slate-300',
      )}
    >
      <button
        type="button"
        onClick={() => setPlaying(!playing)}
        aria-label={playing ? 'Pause playback' : 'Play playback'}
        title={playing ? 'Pause' : 'Play'}
        className={cn(
          'flex h-7 w-7 shrink-0 items-center justify-center rounded',
          'border border-slate-800 bg-surface-100 text-slate-200',
          'transition-colors hover:bg-surface-50 hover:text-white',
          'focus:outline-none focus:ring-1 focus:ring-accent-info',
        )}
      >
        {playing ? (
          <Pause className="h-3.5 w-3.5" />
        ) : (
          <Play className="h-3.5 w-3.5" />
        )}
      </button>

      <input
        type="range"
        min={windowStartMs}
        max={now}
        step={60_000}
        value={Math.min(windowEndMs, now)}
        onChange={(e) => setWindow(windowStartMs, Number(e.target.value))}
        aria-label="Scrub the map time window"
        aria-valuetext={format(windowEndMs, 'MMM d HH:mm')}
        className={cn(
          'h-1.5 flex-1 cursor-pointer appearance-none rounded-full',
          'bg-surface-50 accent-accent-info',
          'focus:outline-none focus:ring-1 focus:ring-accent-info',
        )}
      />

      <div className="flex w-28 shrink-0 items-center justify-end gap-1 tabular-nums">
        {isLive ? (
          <span className="flex items-center gap-1 text-xs font-medium text-accent-ok">
            <span aria-hidden className="text-[10px] leading-none">
              ●
            </span>
            LIVE
          </span>
        ) : (
          <span className="text-xs text-slate-400">
            {format(windowEndMs, 'MMM d HH:mm')}
          </span>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-1" aria-label="Playback speed">
        {SPEEDS.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setSpeed(s)}
            aria-pressed={speed === s}
            className={cn(
              'rounded px-1.5 py-0.5 text-xs tabular-nums transition-colors',
              'focus:outline-none focus:ring-1 focus:ring-accent-info',
              speed === s
                ? 'bg-accent-info/20 font-medium text-accent-info'
                : 'text-slate-500 hover:text-slate-300',
            )}
          >
            {s}x
          </button>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// ReadTimelineLens (P1-T7) — the TEMPORAL half of the read's lens.
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
    <div className="max-w-xs rounded border border-slate-700 bg-surface-100 p-2 text-[11px]">
      <div className="truncate font-medium text-slate-200">{p.title}</div>
      <div className="mt-1 text-slate-500">
        {p.kind} · {new Date(p.ts).toLocaleString()}
      </div>
      {p.subtitle && <div className="mt-1 truncate text-slate-400">{p.subtitle}</div>}
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
        className="flex h-full w-full items-center justify-center bg-surface-300 px-4 text-center text-sm text-slate-500"
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
