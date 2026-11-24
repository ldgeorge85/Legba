/**
 * The Events lanes of `system.timeline` (S7-T2; the Events mode of the merged
 * Timeline, `panels/merged/Timeline.tsx`).
 *
 * A cross-desk banded timeline over the live substrate: signals / findings /
 * situations / events on the Y bands, time on X, signal marks colored by
 * CATEGORY. Clicking a mark drives the shared selection store, brushing every
 * other panel.
 *
 * TWO CLOCKS (W8 UI-B4/time; `lib/temporalLayers.ts`). Every mark has a VALID
 * time (when the world was so) and a RECORD time (when the platform learned
 * it). The axis plots one of them — the `valid` / `record` switch — and the
 * tooltip prints both; a row with no instant on the plotted clock is not
 * drawn at a guessed place but counted as "N without a valid time". A lane
 * with no row in view prints *no read yet*; a lane's figure is AS OF the
 * latest record time among its rows.
 *
 * ONE CLOCK (UI-D1 D6; closes the W7 gap). The axis FOLLOWS the shared window
 * — `useWorldState`'s, the map's and the scrubber's — centred on a case's `at`
 * while a case drives the clock, exactly as the validity Timeline does. Zoom,
 * pan, fit and the selection re-centre are a VIEW LENS over it: they move this
 * axis only and never write the shared window; the re-centre control drops
 * the lens, and a new shared window drops it too.
 *
 * Self-fetching through `useTemporalRows` (shared with the scrubber) so it can
 * dock anywhere without a binding.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
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
import { ChevronLeft, ChevronRight, Crosshair, Maximize2, ZoomIn, ZoomOut } from 'lucide-react'
import { PanelBoundary } from './PanelBoundary'
import { selectRow, useSelection } from '@/state/selection'
import { useCasePlayer } from '@/state/casePlayer'
import { useWorldState } from '@/v4/world/worldState'
import { caseTimelineDomain } from '@/lib/caseModel'
import { panDomain, zoomDomain } from '@/lib/timelineWindows'
import {
  BAND,
  BAND_LABELS,
  KIND_COLOR,
  findingMarkColor,
  pointOpacity,
  type TimelinePoint,
} from '@/lib/timelinePoints'
import {
  AXIS_MEANING,
  SOURCE_KIND,
  TEMPORAL_AXES,
  TEMPORAL_SOURCES,
  axisMs,
  instantZ,
  layerFigure,
  rowsWithoutAxis,
  type TemporalAxis,
  type TemporalRow,
} from '@/lib/temporalLayers'
import { useTemporalRows } from '@/lib/useTemporalRows'
import { figureText, figureValueText, type FigureData } from '@/lib/Figure'

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

/** One mark: a temporal row placed on the plotted clock. */
interface LanePoint extends TimelinePoint {
  category?: string
  validMs: number | null
  recordMs: number | null
}

const NOT_RECORDED = 'not recorded'

function at(ms: number | null): string {
  return ms === null ? NOT_RECORDED : instantZ(ms)
}

function toPoint(r: TemporalRow, axis: TemporalAxis): LanePoint | null {
  const ts = axisMs(r, axis)
  if (ts === null) return null
  return {
    id: r.id,
    title: r.title,
    ts,
    band: BAND[SOURCE_KIND[r.source]],
    kind: SOURCE_KIND[r.source],
    subtitle: r.subtitle,
    severity: r.severity,
    category: r.category,
    validMs: r.validMs,
    recordMs: r.recordMs,
  }
}

export default function TimelinePanel() {
  const { rows, isLoading, failed } = useTemporalRows()
  const [axis, setAxis] = useState<TemporalAxis>('valid')

  // THE ONE CLOCK — the shared window, centred on a case's `at` while a case
  // drives the clock (the same domain the validity Timeline follows).
  const clockDrive = useCasePlayer((s) => s.clockDrive)
  const worldStart = useWorldState((s) => s.windowStartMs)
  const worldEnd = useWorldState((s) => s.windowEndMs)
  const sharedDomain = useMemo<[number, number]>(
    () => caseTimelineDomain(worldStart, worldEnd, clockDrive?.atMs ?? null),
    [worldStart, worldEnd, clockDrive],
  )

  // THE VIEW LENS. Null = the axis IS the shared window. Local state; nothing
  // in this panel writes `useWorldState`.
  const [lens, setLens] = useState<[number, number] | null>(null)
  const domain: [number, number] = lens ?? sharedDomain
  useEffect(() => {
    setLens(null)
  }, [sharedDomain])

  const points = useMemo(() => {
    const out: LanePoint[] = []
    for (const r of rows) {
      const p = toPoint(r, axis)
      if (p) out.push(p)
    }
    return out
  }, [rows, axis])

  // FOCUS centres the lens on a selection that falls outside the view, the
  // validity Timeline's rule: a row of ours is placed on the plotted clock; a
  // record we never fetched uses the instant its selection carries.
  const selection = useSelection((s) => s.selection)
  useEffect(() => {
    if (!selection) return
    const own = points.find((p) => p.id === selection.id)
    const centre = own ? own.ts : (selection.preview?.ts ?? null)
    if (centre == null || !Number.isFinite(centre)) return
    setLens((cur) => {
      const base = cur ?? sharedDomain
      const span = base[1] - base[0]
      const pad = span * 0.1
      if (centre >= base[0] + pad && centre <= base[1] - pad) return cur
      return [centre - span / 2, centre + span / 2]
    })
  }, [selection, points, sharedDomain])

  const inView = useMemo(
    () => points.filter((p) => p.ts >= domain[0] && p.ts <= domain[1]),
    [points, domain],
  )
  const byKind = useMemo(() => {
    const m: Record<string, LanePoint[]> = { signal: [], finding: [], situation: [], event: [] }
    for (const p of inView) m[p.kind].push(p)
    return m
  }, [inView])

  // Per-lane figures over the view, as of the latest record time in it.
  const laneFigures = useMemo(() => {
    const window = { startMs: domain[0], endMs: domain[1] }
    return TEMPORAL_SOURCES.map((source) => {
      const fig: FigureData = failed.includes(source)
        ? { state: 'unmeasured', unit: 'rows', asOf: { absent: 'the route did not answer' } }
        : layerFigure(rows, { id: `${source}:${axis}`, time: axis, window, source })
      return { source, fig }
    })
  }, [rows, axis, domain, failed])
  const missing = rowsWithoutAxis(rows, axis)

  // Category-lane v2 (S7-T6): the signal-lane color legend, restricted to the
  // categories actually in view, most-frequent first.
  const categoriesPresent = useMemo(() => {
    const counts = new Map<string, number>()
    for (const p of byKind.signal) {
      const c = p.category
      if (c && CATEGORY_COLOR[c]) counts.set(c, (counts.get(c) ?? 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([c]) => c)
  }, [byKind])
  const nowMs = Date.now()

  const laneColor = (p: LanePoint) =>
    (p.category && CATEGORY_COLOR[p.category]) || KIND_COLOR.signal
  const onPick = (d: unknown) => {
    const p = d as LanePoint
    // `ts` — the record's own instant (design §3 enabler 1), so every other
    // timeline can center on this selection without refetching the record.
    selectRow(p.kind, p.id, p.title, { origin: 'timeline', preview: { ts: p.ts } })
  }

  const zoom = (factor: number) => setLens(zoomDomain(domain, factor, 0.5))
  const pan = (dir: -1 | 1) => setLens(panDomain(domain, dir * 0.25 * (domain[1] - domain[0])))
  const fit = () => {
    if (points.length === 0) return
    let lo = Infinity
    let hi = -Infinity
    for (const p of points) {
      if (p.ts < lo) lo = p.ts
      if (p.ts > hi) hi = p.ts
    }
    const pad = Math.max(60_000, (hi - lo) * 0.05)
    setLens([lo - pad, hi + pad])
  }
  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      setLens(zoomDomain(domain, e.deltaY > 0 ? 1.25 : 1 / 1.25, 0.5))
    },
    [domain],
  )

  const btn =
    'rounded border border-line p-0.5 text-ink-2 hover:border-line-strong hover:text-ink-1 disabled:text-ink-3'

  return (
    <PanelBoundary>
      <div className="flex h-full w-full flex-col bg-surf-base" data-testid="global-timeline">
        <div className="shrink-0 border-b border-line px-3 py-1.5 text-[11px] text-ink-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="flex items-center gap-2">
              <span className="font-medium text-ink-1">Timeline</span>
              <span className="flex items-center gap-0.5" role="group" aria-label="Plotted clock">
                {TEMPORAL_AXES.map((a) => (
                  <button
                    key={a}
                    type="button"
                    onClick={() => setAxis(a)}
                    aria-pressed={axis === a}
                    title={AXIS_MEANING[a]}
                    data-testid={`events-timeline-axis-${a}`}
                    className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                      axis === a ? 'bg-accent-info/20 text-accent-info' : 'text-ink-3 hover:text-ink-1'
                    }`}
                  >
                    {a} time
                  </button>
                ))}
              </span>
            </span>
            <span className="flex items-center gap-1">
              <button type="button" className={btn} onClick={() => pan(-1)} aria-label="pan earlier" title="pan earlier (a view lens)" data-testid="events-timeline-pan-earlier">
                <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
              </button>
              <button type="button" className={btn} onClick={() => pan(1)} aria-label="pan later" title="pan later (a view lens)" data-testid="events-timeline-pan-later">
                <ChevronRight className="h-3.5 w-3.5" aria-hidden />
              </button>
              <button type="button" className={btn} onClick={() => zoom(1 / 1.5)} aria-label="zoom in" title="zoom in (a view lens)" data-testid="events-timeline-zoom-in">
                <ZoomIn className="h-3.5 w-3.5" aria-hidden />
              </button>
              <button type="button" className={btn} onClick={() => zoom(1.5)} aria-label="zoom out" title="zoom out (a view lens)" data-testid="events-timeline-zoom-out">
                <ZoomOut className="h-3.5 w-3.5" aria-hidden />
              </button>
              <button
                type="button"
                className={btn}
                onClick={fit}
                disabled={points.length === 0}
                aria-label="fit to data"
                title="fit to data (a view lens — the shared window does not move)"
                data-testid="events-timeline-fit"
              >
                <Maximize2 className="h-3.5 w-3.5" aria-hidden />
              </button>
              <button
                type="button"
                onClick={() => setLens(null)}
                disabled={lens === null}
                className={`flex items-center gap-1 rounded border border-line px-1 py-0.5 ${
                  lens === null ? 'text-ink-3' : 'text-ink-1 hover:border-line-strong'
                }`}
                title="re-centre on the shared window (the map's and the scrubber's)"
                aria-label="re-centre on the shared window"
                data-testid="events-timeline-recentre"
                data-lens={lens === null ? 'off' : 'on'}
              >
                <Crosshair className="h-3.5 w-3.5" aria-hidden />
                {lens === null ? 'shared window' : 're-centre'}
              </button>
            </span>
          </div>
          {/* One figure per lane over the view: rows on the plotted clock, as of
              the latest record time among them, or no read yet. */}
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5" data-testid="events-timeline-lanes">
            {laneFigures.map(({ source, fig }) => (
              <span
                key={source}
                className="flex items-center gap-1"
                data-testid={`events-timeline-lane-${source}`}
                data-state={fig.state}
                title={figureText({ ...fig, label: source })}
              >
                <span className="h-2 w-2 rounded-full" style={{ background: KIND_COLOR[SOURCE_KIND[source]] }} />
                {source}
                <span className={fig.state === 'measured' ? 'font-mono text-ink-2' : 'italic'}>
                  {figureValueText(fig)}
                </span>
              </span>
            ))}
            {missing > 0 && (
              <span className="text-ink-3" data-testid="events-timeline-missing">
                · {missing} without a {axis} time (not drawn)
              </span>
            )}
          </div>
          {/* category-lane v2 key — the signal-lane color coding, in-view only */}
          {categoriesPresent.length > 0 && (
            <div
              className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-ink-3"
              data-testid="timeline-category-legend"
            >
              <span className="uppercase tracking-wide text-[10px] text-ink-3">signal lane</span>
              {categoriesPresent.map((c) => (
                <LegendDot key={c} color={CATEGORY_COLOR[c]} label={c} />
              ))}
            </div>
          )}
        </div>
        <div
          className="relative min-h-0 flex-1"
          data-testid="events-timeline-plot"
          data-domain={`${Math.round(domain[0])}:${Math.round(domain[1])}`}
          data-axis={axis}
          onWheel={onWheel}
        >
          {inView.length === 0 ? (
            <div className="flex h-full items-center justify-center text-sm text-ink-3" data-testid="events-timeline-empty">
              {isLoading ? 'Loading timeline…' : `no read yet — no row has a ${axis} time in this window`}
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 10, right: 18, bottom: 4, left: 56 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--line-1)" opacity={0.35} />
                <XAxis
                  type="number"
                  dataKey="ts"
                  domain={domain}
                  allowDataOverflow
                  tickFormatter={fmtAxis}
                  stroke="var(--ink-3)"
                  fontSize={10}
                  scale="time"
                />
                <YAxis
                  type="number"
                  dataKey="band"
                  domain={[0.5, 4.5]}
                  ticks={[1, 2, 3, 4]}
                  tickFormatter={(v: number) => BAND_LABELS[v] ?? ''}
                  stroke="var(--ink-3)"
                  fontSize={10}
                  width={52}
                />
                <RTooltip content={<TLTooltip />} cursor={{ stroke: 'var(--line-2)', strokeDasharray: '3 3' }} />
                <Scatter name="signals" data={byKind.signal} onClick={onPick}>
                  {byKind.signal.map((p) => (
                    <Cell key={p.id} fill={laneColor(p)} fillOpacity={pointOpacity(p.ts, nowMs)} />
                  ))}
                </Scatter>
                <Scatter name="findings" data={byKind.finding} onClick={onPick}>
                  {byKind.finding.map((p) => (
                    <Cell key={p.id} fill={findingMarkColor(p.severity)} fillOpacity={pointOpacity(p.ts, nowMs)} />
                  ))}
                </Scatter>
                <Scatter name="situations" data={byKind.situation} onClick={onPick}>
                  {byKind.situation.map((p) => (
                    <Cell key={p.id} fill={KIND_COLOR.situation} fillOpacity={pointOpacity(p.ts, nowMs)} />
                  ))}
                </Scatter>
                <Scatter name="events" data={byKind.event} onClick={onPick}>
                  {byKind.event.map((p) => (
                    <Cell key={p.id} fill={KIND_COLOR.event} fillOpacity={pointOpacity(p.ts, nowMs)} />
                  ))}
                </Scatter>
              </ScatterChart>
            </ResponsiveContainer>
          )}
        </div>
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

/** Both clocks, always: the plotted one is only where the mark sits. */
function TLTooltip({ active, payload }: { active?: boolean; payload?: unknown[] }) {
  if (!active || !payload || payload.length === 0) return null
  const p = (payload[0] as { payload: LanePoint }).payload
  return (
    <div className="max-w-xs rounded border border-line-strong bg-surf-2 p-2 text-[11px]">
      <div className="truncate font-medium text-ink-1">{p.title}</div>
      <div className="mt-1 text-ink-3">
        {p.kind}
        {p.subtitle ? ` · ${p.subtitle}` : ''}
      </div>
      <div className="mt-0.5 font-mono text-ink-2" data-testid="events-timeline-tooltip-clocks">
        valid {at(p.validMs)} · recorded {at(p.recordMs)}
      </div>
    </div>
  )
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1 text-ink-3">
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      {label}
    </span>
  )
}
