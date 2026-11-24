/**
 * TimeScrubber — the bottom bar of the World map that drives THE ONE CLOCK.
 *
 * Reads and writes the orchestrator-owned world store. Every write of the
 * window goes through `setWindow` (UI-D1 D6): the two thumbs, the playback
 * tick, and a span preset (whose `setSpan` records the outer span and then
 * calls `setWindow`). Nothing else on this bar moves "when".
 *
 * TWO CLOCKS UNDER THE THUMBS (W8 UI-B4/time; `lib/temporalLayers.ts`). Below
 * the range sit two density strips over the same outer span — VALID time
 * (when the world was so) and RECORD time (when the platform learned it) —
 * for every map layer that is on. Each strip's line names its temporal layers
 * with their row counts in the window, *no read yet* for a layer with none,
 * and the as-of, which is the latest RECORD time among the rows counted.
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
import {
  SPAN_PRESETS,
  clampWindow,
  isLiveWindow,
  sliderStep,
  windowFraction,
} from '@/lib/mapTime'
import { asOfText, figureText, figureValueText, type FigureData } from '@/lib/Figure'
import {
  AXIS_MEANING,
  TEMPORAL_AXES,
  densityBuckets,
  instantZ,
  latestRecordMs,
  layerFigure,
  rowsInLayer,
  sourcesOn,
  temporalLayers,
  type TemporalAxis,
  type TemporalLayer,
  type TemporalRow,
  type TemporalSource,
} from '@/lib/temporalLayers'
import { useTemporalRows } from '@/lib/useTemporalRows'

/** Window is "live" when its end is within ~1 minute of now. */
const LIVE_THRESHOLD_MS = 60_000
/** Each ~1s playback tick advances the window END by speed × 2 sim minutes. */
const TICK_INTERVAL_MS = 1_000
const TICK_BASE_MS = 120_000
const SPEEDS = [0.5, 1, 2, 4] as const
/** Density buckets per strip across the outer span. */
const BUCKETS = 96

/** Shared thumb styling so both overlaid range inputs match the dark chrome and
 *  their thumbs stay clickable while their tracks are click-through. */
const THUMB_CLASS = cn(
  'pointer-events-none absolute left-0 top-1/2 h-0 w-full -translate-y-1/2 appearance-none bg-transparent',
  'focus:outline-none',
  '[&::-webkit-slider-thumb]:pointer-events-auto [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5',
  '[&::-webkit-slider-thumb]:cursor-pointer [&::-webkit-slider-thumb]:appearance-none',
  '[&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:border-2',
  '[&::-webkit-slider-thumb]:border-accent-info [&::-webkit-slider-thumb]:bg-surface-100',
  '[&::-moz-range-thumb]:pointer-events-auto [&::-moz-range-thumb]:h-3.5 [&::-moz-range-thumb]:w-3.5',
  '[&::-moz-range-thumb]:cursor-pointer [&::-moz-range-thumb]:rounded-full',
  '[&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-accent-info [&::-moz-range-thumb]:bg-surface-100',
)

/** A failed read is an unread layer, not an empty one. */
const UNREAD: FigureData = {
  state: 'unmeasured',
  unit: 'rows',
  asOf: { absent: 'the route did not answer' },
}

/** One temporal layer's figure, honouring a source whose read failed. */
function figureFor(
  rows: readonly TemporalRow[],
  layer: TemporalLayer,
  failed: readonly TemporalSource[],
): FigureData {
  return failed.includes(layer.source) ? UNREAD : layerFigure(rows, layer)
}

/**
 * One clock's strip and line: the density of the on-layers' rows across the
 * outer span (the window's buckets lit, the rest dimmed), then each temporal
 * layer's figure and the as-of — the latest record time among the rows the
 * line counted.
 */
function AxisStrip({
  axis,
  rows,
  layers,
  failed,
  lo,
  hi,
  windowStartMs,
  windowEndMs,
}: {
  axis: TemporalAxis
  rows: readonly TemporalRow[]
  layers: readonly TemporalLayer[]
  failed: readonly TemporalSource[]
  lo: number
  hi: number
  windowStartMs: number
  windowEndMs: number
}) {
  const onRows = useMemo(() => {
    const on = new Set(layers.map((l) => l.source))
    return rows.filter((r) => on.has(r.source))
  }, [rows, layers])
  const buckets = useMemo(() => densityBuckets(onRows, axis, lo, hi, BUCKETS), [onRows, axis, lo, hi])
  const peak = Math.max(1, ...buckets)
  const bucketMs = (hi - lo) / BUCKETS
  const asOfMs = latestRecordMs(layers.flatMap((l) => rowsInLayer(rows, l)))
  const asOf = asOfMs === null ? { absent: 'no row in the window' } : instantZ(asOfMs)
  const figures = layers.map((l) => ({ layer: l, fig: figureFor(rows, l, failed) }))
  const lineTitle = figures
    .map(({ layer, fig }) => figureText({ ...fig, label: layer.source }))
    .join('\n')

  return (
    <>
      <span
        className="col-span-2 justify-self-end text-label uppercase tracking-wide text-ink-3"
        title={AXIS_MEANING[axis]}
      >
        {axis}
      </span>
      <div
        className="flex h-2.5 items-end gap-px"
        data-testid={`scrubber-axis-${axis}`}
        data-buckets={buckets.join(',')}
        aria-label={`${AXIS_MEANING[axis]} — row density across the span`}
        role="img"
      >
        {buckets.map((n, i) => {
          const at = lo + (i + 0.5) * bucketMs
          const inWindow = at >= windowStartMs && at <= windowEndMs
          return (
            <span
              key={i}
              className={cn('flex-1 rounded-sm', axis === 'valid' ? 'bg-accent-info' : 'bg-ink-2')}
              style={{
                height: n === 0 ? 0 : `${Math.max(15, (n / peak) * 100)}%`,
                opacity: inWindow ? 0.9 : 0.3,
              }}
            />
          )
        })}
      </div>
      <span
        className="col-span-2 truncate text-label text-ink-3"
        data-testid={`scrubber-axis-${axis}-line`}
        title={lineTitle}
      >
        {layers.length === 0 ? (
          'no time layer is on'
        ) : (
          <>
            {figures.map(({ layer, fig }, i) => (
              <span key={layer.id} data-testid={`scrubber-layer-${layer.id}`} data-state={fig.state}>
                {i > 0 ? ' · ' : ''}
                {layer.source}{' '}
                <span className={fig.state === 'measured' ? 'font-mono text-ink-2' : 'italic'}>
                  {figureValueText(fig)}
                </span>
              </span>
            ))}
            <span className="font-mono" data-testid={`scrubber-axis-${axis}-asof`}>
              {' · '}
              {asOfText(asOf)}
            </span>
          </>
        )}
      </span>
    </>
  )
}

/**
 * TimeScrubber — a genuine two-ended [t0, t1] window control over two clocks.
 *
 * A dual-thumb range over the outer span [now − spanMs, now] drives BOTH the
 * window start and end; the map filters signals / findings / situations to that
 * window (see MapLibreWorldMap `winSignals` etc.). Span presets (6h/24h/7d/30d)
 * re-anchor the outer span so widening actually reaches further back in the
 * data. Play sweeps the END forward to LIVE, revealing events up to time T.
 */
export default function TimeScrubber() {
  const windowStartMs = useWorldState((s) => s.windowStartMs)
  const windowEndMs = useWorldState((s) => s.windowEndMs)
  const spanMs = useWorldState((s) => s.spanMs)
  const playing = useWorldState((s) => s.playing)
  const speed = useWorldState((s) => s.speed)
  const layerToggles = useWorldState((s) => s.layers)
  const setWindow = useWorldState((s) => s.setWindow)
  const setSpan = useWorldState((s) => s.setSpan)
  const setPlaying = useWorldState((s) => s.setPlaying)
  const setSpeed = useWorldState((s) => s.setSpeed)
  const { rows, failed } = useTemporalRows()

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

  // Keep "now" fresh for the slider's upper bound across renders.
  const nowRef = useRef(Date.now())
  nowRef.current = Math.max(nowRef.current, Date.now(), windowEndMs)
  const now = nowRef.current
  // Outer bounds: the whole span sits inside [lo, hi]. `lo` follows the span
  // preset; a start thumb dragged inward raises windowStartMs above lo.
  const lo = Math.min(windowStartMs, now - spanMs)
  const hi = now
  const step = sliderStep(hi - lo)

  const isLive = isLiveWindow(windowEndMs, now, LIVE_THRESHOLD_MS)
  const startPct = windowFraction(windowStartMs, lo, hi) * 100
  const endPct = windowFraction(Math.min(windowEndMs, hi), lo, hi) * 100

  const onStart = (v: number) => {
    const w = clampWindow(v, windowEndMs, lo, hi)
    setWindow(w.startMs, w.endMs)
  }
  const onEnd = (v: number) => {
    const w = clampWindow(windowStartMs, v, lo, hi)
    setWindow(w.startMs, w.endMs)
  }

  const rangeLabel = `${format(windowStartMs, 'MMM d HH:mm')} – ${format(windowEndMs, 'HH:mm')}`

  // The temporal layers: both clocks for every map layer that is on.
  const layers = useMemo(
    () => temporalLayers(sourcesOn(layerToggles), { startMs: windowStartMs, endMs: windowEndMs }),
    [layerToggles, windowStartMs, windowEndMs],
  )

  return (
    <div
      className={cn(
        'grid w-full shrink-0 grid-cols-[auto_auto_minmax(0,1fr)_auto_auto] items-center gap-x-3 gap-y-0.5',
        'border-t border-line bg-surf-1 px-3 py-1.5 text-ink-2',
      )}
      data-testid="time-scrubber"
    >
      <button
        type="button"
        onClick={() => setPlaying(!playing)}
        aria-label={playing ? 'Pause playback' : 'Play playback'}
        title={playing ? 'Pause' : 'Play'}
        className={cn(
          'flex h-7 w-7 shrink-0 items-center justify-center rounded',
          'border border-line bg-surf-2 text-ink-1',
          'transition-colors hover:bg-surf-3',
          'focus:outline-none focus:ring-1 focus:ring-accent-info',
        )}
      >
        {playing ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
      </button>

      {/* Span presets — re-anchor the outer window to the last N. */}
      <div className="flex shrink-0 items-center gap-0.5" aria-label="Window span">
        {SPAN_PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => setSpan(p.ms)}
            aria-pressed={spanMs === p.ms}
            className={cn(
              'rounded px-1.5 py-0.5 text-xs tabular-nums transition-colors',
              'focus:outline-none focus:ring-1 focus:ring-accent-info',
              spanMs === p.ms
                ? 'bg-accent-info/20 font-medium text-accent-info'
                : 'text-ink-3 hover:text-ink-1',
            )}
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Dual-thumb window range — two overlaid inputs, click-through tracks. */}
      <div className="relative h-4" data-testid="time-scrubber-range">
        <div className="absolute left-0 top-1/2 h-1.5 w-full -translate-y-1/2 rounded-full bg-surf-3" />
        <div
          className="absolute top-1/2 h-1.5 -translate-y-1/2 rounded-full bg-accent-info/60"
          style={{ left: `${startPct}%`, width: `${Math.max(0, endPct - startPct)}%` }}
        />
        <input
          type="range"
          min={lo}
          max={hi}
          step={step}
          value={Math.min(Math.max(windowStartMs, lo), hi)}
          onChange={(e) => onStart(Number(e.target.value))}
          aria-label="Window start"
          aria-valuetext={format(windowStartMs, 'MMM d HH:mm')}
          className={THUMB_CLASS}
        />
        <input
          type="range"
          min={lo}
          max={hi}
          step={step}
          value={Math.min(Math.max(windowEndMs, lo), hi)}
          onChange={(e) => onEnd(Number(e.target.value))}
          aria-label="Window end"
          aria-valuetext={format(windowEndMs, 'MMM d HH:mm')}
          className={THUMB_CLASS}
        />
      </div>

      <div className="flex w-40 shrink-0 items-center justify-end gap-1 tabular-nums">
        {isLive ? (
          <span className="flex items-center gap-1 text-xs font-medium text-accent-ok">
            <span aria-hidden className="text-label leading-none">●</span>
            LIVE
          </span>
        ) : null}
        <span className="text-label text-ink-2" title="Selected window">
          {rangeLabel}
        </span>
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
                : 'text-ink-3 hover:text-ink-1',
            )}
          >
            {s}x
          </button>
        ))}
      </div>

      {TEMPORAL_AXES.map((axis) => (
        <AxisStrip
          key={axis}
          axis={axis}
          rows={rows}
          layers={layers.filter((l) => l.time === axis)}
          failed={failed}
          lo={lo}
          hi={hi}
          windowStartMs={windowStartMs}
          windowEndMs={windowEndMs}
        />
      ))}
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
