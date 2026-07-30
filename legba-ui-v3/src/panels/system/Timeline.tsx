/**
 * The Timeline (`system.timeline`) — the validity-window temporal view (P4-4).
 *
 * The temporal substrate (facts with `[valid_from, valid_until)`, situations
 * with a lifecycle window, findings with `[produced_at, superseded_at)` +
 * supersession chains) had NO temporal view — this is it. RANGED items on three
 * lanes over a brushable, zoomable time axis (ms→months): situations as spans,
 * findings with their validity windows + supersession-sequence connectors, and
 * facts as validity bands. An OPEN window (server `end=null`) draws live to the
 * right edge with a dashed cap — never a fabricated close.
 *
 * Built on a lightweight custom SVG rather than vis-timeline: vis-timeline
 * manipulates the DOM imperatively, ships its own CSS (which fights the panel's
 * token palette) and a date lib, and isn't React-friendly — the custom SVG
 * renders ranged items cleanly, matches the chrome, and keeps ALL shaping in
 * `lib/timelineWindows` (pure + unit-tested).
 *
 * Data: `GET /api/v1/v3/timeline?target_id=&days=` (added for this panel — no
 * existing read carries validity windows or supersession edges). Desk-scoped to
 * the unified selection's target; click a bar → `selectRow` into the Inspector.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { HelpCircle, Maximize2, ZoomIn, ZoomOut } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { ProvenanceCard } from '@/components/ProvenanceCard'
import { useDockviewTileRedraw } from '@/components/useTileRedraw'
import { fetchTimeline } from '@/lib/api'
import { useElementWidth } from '@/lib/useElementWidth'
import { useSelection, selectRow } from '@/state/selection'
import type { ProvenanceFacts } from '@/lib/provenance'
import type { PanelProps } from '@/types'
import {
  KIND_COLOR,
  LANE_LABEL,
  LANE_ORDER,
  itemColor,
  kindTallies,
  panDomain,
  shapeItems,
  supersessionEdges,
  timeDomain,
  visibleItems,
  xOf,
  zoomDomain,
  type ShapedItem,
  type TimelineItemKind,
} from '@/lib/timelineWindows'

const DAY_OPTIONS = [7, 30, 90] as const
const LANE_H = 78 // px per lane
const GUTTER = 76 // px left label gutter
const AXIS_H = 22 // px bottom axis
const TOP_PAD = 8
const BAR_H = 16
const DRAG_THRESHOLD = 4 // px before a drag suppresses the click-select

// ---------------------------------------------------------------------------
// Axis tick formatting — adapts to the visible span (ms → months).
// ---------------------------------------------------------------------------

function fmtTick(ms: number, spanMs: number): string {
  const d = new Date(ms)
  const HOUR = 3_600_000
  const DAY = 86_400_000
  if (spanMs < 2 * HOUR) {
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  }
  if (spanMs < 3 * DAY) {
    return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString(
      undefined,
      { hour: '2-digit', minute: '2-digit' },
    )}`
  }
  if (spanMs < 120 * DAY) {
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  }
  return d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
}

function axisTicks(domain: [number, number], count = 6): number[] {
  const [lo, hi] = domain
  const step = (hi - lo) / count
  const out: number[] = []
  for (let i = 0; i <= count; i++) out.push(lo + step * i)
  return out
}

// The SVG needs a measured px width (no recharts here). `useElementWidth` is
// the shared CALLBACK-REF hook: the plot div mounts AFTER the loading
// empty-state has occupied a render or two, and a []-deps observer effect
// would observe nothing and pin the width at 0 forever (the first-mount
// blank-panel bug) — the callback ref re-observes whenever the element itself
// attaches.
//
// That callback-ref fix does NOT cover a SECOND, distinct class: a panel
// mounted into a Dockview tile that starts hidden (a background tab, or a
// tile Dockview hasn't laid out yet) measures a zero-size box the instant the
// plot div attaches, same as before — but this time the ResizeObserver it
// just subscribed can miss the later hidden→visible transition entirely
// (Dockview keeps the content mounted the whole time, so there is no
// guaranteed further resize delivery once the tile activates). The width
// then sticks at 0 forever: the subtitle honestly reports "N ranged items"
// (shaped from the fetched data, independent of measurement) while the
// canvas stays blank — exactly the gallery-2 "278 ranged items" + empty-plot
// bug. `useDockviewTileRedraw` is the shared fix for this class (already
// applied to `target/Map.tsx` + `target/Timeline.tsx`): it watches the
// tile's own visibility/dimension events and bumps a tick a frame after the
// tile becomes visible; keying the measured div on that tick forces the
// callback ref to detach/reattach, which re-seeds the width against the
// tile's now-real box.

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export default function TimelinePanel({ registration }: PanelProps) {
  const selection = useSelection((s) => s.selection)
  const targetId = selection?.kind === 'target' ? selection.id : undefined
  const targetLabel = selection?.kind === 'target' ? selection.label ?? selection.id : undefined
  const [days, setDays] = useState<number>(30)
  const [showProvenance, setShowProvenance] = useState(false)

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['system-timeline', targetId ?? 'all', days],
    queryFn: () => fetchTimeline({ target_id: targetId, days }),
    refetchInterval: 60_000,
  })

  // Fix "now" per data load so open windows resolve against a stable reference.
  const nowMs = useMemo(
    () => (data ? Date.parse(data.server_now) || Date.now() : Date.now()),
    [data],
  )
  const shaped = useMemo(() => shapeItems(data?.items ?? [], nowMs), [data, nowMs])
  const fitDomain = useMemo(() => timeDomain(shaped), [shaped])
  const edges = useMemo(() => supersessionEdges(shaped), [shaped])
  const tallies = useMemo(
    () => kindTallies(shaped, data?.counts ?? {}, data?.truncated ?? {}),
    [shaped, data],
  )

  // The visible (zoom/pan) domain — re-fit whenever the underlying fit changes
  // (new data / new scope / new window).
  const [domain, setDomain] = useState<[number, number] | undefined>(fitDomain)
  useEffect(() => {
    setDomain(fitDomain)
  }, [fitDomain])

  const [wrapRef, width] = useElementWidth<HTMLDivElement>()
  const plotW = Math.max(0, width - GUTTER)
  const svgH = TOP_PAD + LANE_ORDER.length * LANE_H + AXIS_H

  // Background/not-yet-laid-out Dockview tile fix (see the comment above):
  // remeasure once the tile actually becomes visible.
  const redrawTick = useDockviewTileRedraw()

  const vis = useMemo(
    () => (domain ? visibleItems(shaped, domain) : shaped),
    [shaped, domain],
  )

  // --- interaction: wheel-zoom about the cursor, drag-pan, click-select ---
  const dragRef = useRef<{ startX: number; startDomain: [number, number]; moved: boolean } | null>(null)

  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      if (!domain || plotW <= 0) return
      e.preventDefault()
      const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
      const px = e.clientX - rect.left - GUTTER
      const pivot = Math.max(0, Math.min(1, px / plotW))
      const factor = e.deltaY > 0 ? 1.25 : 1 / 1.25
      setDomain(zoomDomain(domain, factor, pivot))
    },
    [domain, plotW],
  )

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (!domain) return
      dragRef.current = { startX: e.clientX, startDomain: domain, moved: false }
    },
    [domain],
  )
  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      const d = dragRef.current
      if (!d || plotW <= 0) return
      const dx = e.clientX - d.startX
      if (Math.abs(dx) > DRAG_THRESHOLD) d.moved = true
      const span = d.startDomain[1] - d.startDomain[0]
      const deltaMs = -(dx / plotW) * span
      setDomain(panDomain(d.startDomain, deltaMs))
    },
    [plotW],
  )
  const endDrag = useCallback(() => {
    // Defer clearing so the click handler can read `.moved`.
    window.setTimeout(() => {
      dragRef.current = null
    }, 0)
  }, [])

  const onBarClick = useCallback((item: ShapedItem) => {
    if (dragRef.current?.moved) return // a pan, not a select
    selectRow(item.kind, item.id, item.label, { origin: 'timeline' })
  }, [])

  const zoomBtn = (factor: number) => {
    if (!domain) return
    setDomain(zoomDomain(domain, factor, 0.5))
  }

  const totalShown = shaped.length
  const span = domain ? domain[1] - domain[0] : 0
  const scopeText = targetId ? `desk ${targetLabel}` : 'all desks'

  // P4-5 — the reusable ProvenanceCard describing THIS panel's data lineage
  // (purpose / source / freshness / limitations), pulled from what the read
  // carries. No walker click: the grammar is surfaced in-panel.
  const provenanceFacts: ProvenanceFacts = {
    purpose:
      'Facts, situations and findings as validity-window spans — [valid_from, valid_until), situation lifecycle, [produced_at, superseded_at) — with supersession-chain edges.',
    source: 'GET /v3/timeline · facts + situations + analyst_outputs',
    freshnessAt: data?.server_now,
    limitations: [
      ...(tallies.some((t) => t.truncated) ? ['some lanes capped — see the per-kind tally'] : []),
      'open windows (dashed) are extended to now for layout; the substrate carries no close for them',
    ],
    state: data ? 'live' : 'absent',
  }

  const laneY = (lane: number) => TOP_PAD + lane * LANE_H

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${totalShown} ranged item${totalShown === 1 ? '' : 's'} · ${scopeText} · ${days}d`}
      onRefresh={() => refetch()}
      actions={
        <div className="flex items-center gap-1">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded border border-line bg-surf-1 px-1 py-0.5 text-label text-ink-2"
            title="window (days)"
            aria-label="timeline window in days"
            data-testid="timeline-days"
          >
            {DAY_OPTIONS.map((d) => (
              <option key={d} value={d}>
                {d}d
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => zoomBtn(1 / 1.5)}
            className="rounded border border-line p-1 text-ink-2 hover:border-line-strong hover:text-ink-1"
            title="zoom in"
            aria-label="zoom in"
            data-testid="timeline-zoom-in"
          >
            <ZoomIn className="h-3.5 w-3.5" aria-hidden />
          </button>
          <button
            type="button"
            onClick={() => zoomBtn(1.5)}
            className="rounded border border-line p-1 text-ink-2 hover:border-line-strong hover:text-ink-1"
            title="zoom out"
            aria-label="zoom out"
            data-testid="timeline-zoom-out"
          >
            <ZoomOut className="h-3.5 w-3.5" aria-hidden />
          </button>
          <button
            type="button"
            onClick={() => setDomain(fitDomain)}
            className="rounded border border-line p-1 text-ink-2 hover:border-line-strong hover:text-ink-1"
            title="fit to data"
            aria-label="fit timeline to data"
            data-testid="timeline-fit"
          >
            <Maximize2 className="h-3.5 w-3.5" aria-hidden />
          </button>
          <button
            type="button"
            onClick={() => setShowProvenance((v) => !v)}
            className={`rounded border border-line p-1 hover:border-line-strong hover:text-ink-1 ${
              showProvenance ? 'text-ink-1' : 'text-ink-2'
            }`}
            title="data provenance"
            aria-label="toggle data provenance"
            aria-pressed={showProvenance}
            data-testid="timeline-provenance-toggle"
          >
            <HelpCircle className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      }
    >
      <div className="flex h-full flex-col">
        {error instanceof Error && (
          <div className="py-2 text-label text-accent-critical">error: {error.message}</div>
        )}

        {showProvenance && (
          <div className="mb-2 shrink-0">
            <ProvenanceCard facts={provenanceFacts} title="Timeline provenance" />
          </div>
        )}

        {totalShown === 0 ? (
          <div
            className="flex flex-1 items-center justify-center text-body text-ink-3"
            data-testid="timeline-empty"
          >
            {isLoading
              ? 'loading timeline…'
              : `no facts, situations, or findings with a validity window in the last ${days}d${
                  targetId ? ` for ${scopeText}` : ''
                }`}
          </div>
        ) : (
          <div
            key={redrawTick}
            ref={wrapRef}
            className="min-h-0 flex-1 select-none overflow-hidden"
            data-testid="timeline-plot"
            onWheel={onWheel}
            onMouseDown={onMouseDown}
            onMouseMove={onMouseMove}
            onMouseUp={endDrag}
            onMouseLeave={endDrag}
            style={{ cursor: dragRef.current?.moved ? 'grabbing' : 'grab' }}
          >
            {domain && plotW > 0 && (
              <svg width={width} height={svgH} role="img" aria-label="validity-window timeline">
                {/* lane bands + labels */}
                {LANE_ORDER.map((kind, lane) => (
                  <g key={kind}>
                    <rect
                      x={0}
                      y={laneY(lane)}
                      width={width}
                      height={LANE_H}
                      fill={lane % 2 === 0 ? 'var(--surf-2)' : 'var(--surf-1)'}
                      opacity={0.5}
                    />
                    <line
                      x1={GUTTER}
                      x2={width}
                      y1={laneY(lane)}
                      y2={laneY(lane)}
                      stroke="var(--line-1)"
                      opacity={0.5}
                    />
                    <text
                      x={8}
                      y={laneY(lane) + LANE_H / 2}
                      fill="var(--ink-3)"
                      fontSize={11}
                      dominantBaseline="middle"
                    >
                      <tspan className="uppercase">{LANE_LABEL[kind]}</tspan>
                    </text>
                    {/* lane color key dot */}
                    <circle cx={64} cy={laneY(lane) + LANE_H / 2} r={3} fill={KIND_COLOR[kind]} />
                  </g>
                ))}

                {/* supersession connectors (drawn under the bars) */}
                {edges.map((e, i) => {
                  const fx = xOf(e.from.startMs, domain, plotW) + GUTTER
                  const tx = xOf(e.to.startMs, domain, plotW) + GUTTER
                  const fy = laneY(e.from.lane) + LANE_H / 2 + BAR_H / 2 + 3
                  const ty = laneY(e.to.lane) + LANE_H / 2 + BAR_H / 2 + 3
                  return (
                    <line
                      key={`edge-${i}`}
                      x1={fx}
                      y1={fy}
                      x2={tx}
                      y2={ty}
                      stroke="var(--ink-3)"
                      strokeWidth={1}
                      strokeDasharray="2 2"
                      opacity={0.7}
                    />
                  )
                })}

                {/* the ranged item bars */}
                {vis.map((it) => {
                  const x1 = xOf(it.startMs, domain, plotW) + GUTTER
                  const x2 = xOf(it.endMs, domain, plotW) + GUTTER
                  const w = Math.max(2, x2 - x1)
                  const y = laneY(it.lane) + (LANE_H - BAR_H) / 2
                  const color = itemColor(it)
                  return (
                    <g key={`${it.kind}-${it.id}`}>
                      <rect
                        x={x1}
                        y={y}
                        width={w}
                        height={BAR_H}
                        rx={3}
                        fill={color}
                        fillOpacity={it.open ? 0.55 : 0.85}
                        stroke={color}
                        strokeWidth={1}
                        strokeDasharray={it.open ? '3 2' : undefined}
                        style={{ cursor: 'pointer' }}
                        onClick={() => onBarClick(it)}
                        data-testid={`timeline-bar-${it.kind}`}
                      >
                        <title>
                          {it.label}
                          {'\n'}
                          {it.kind}
                          {it.status ? ` · ${it.status}` : ''}
                          {it.severity ? ` · ${it.severity}` : ''}
                          {'\n'}
                          {new Date(it.startMs).toLocaleString()} →{' '}
                          {it.open ? 'open (current)' : new Date(it.endMs).toLocaleString()}
                          {it.supersededBy ? `\nsuperseded by ${it.supersededBy}` : ''}
                        </title>
                      </rect>
                      {/* open-window chevron cap */}
                      {it.open && x2 >= GUTTER && (
                        <text
                          x={Math.min(x2, width) - 2}
                          y={y + BAR_H / 2}
                          fill={color}
                          fontSize={11}
                          textAnchor="end"
                          dominantBaseline="middle"
                          pointerEvents="none"
                        >
                          ▸
                        </text>
                      )}
                    </g>
                  )
                })}

                {/* time axis */}
                <g>
                  <line
                    x1={GUTTER}
                    x2={width}
                    y1={svgH - AXIS_H}
                    y2={svgH - AXIS_H}
                    stroke="var(--line-2)"
                  />
                  {axisTicks(domain).map((t, i) => {
                    const x = xOf(t, domain, plotW) + GUTTER
                    return (
                      <g key={`tick-${i}`}>
                        <line x1={x} x2={x} y1={TOP_PAD} y2={svgH - AXIS_H} stroke="var(--line-1)" opacity={0.3} />
                        <text
                          x={x}
                          y={svgH - AXIS_H + 14}
                          fill="var(--ink-3)"
                          fontSize={10}
                          textAnchor={i === 0 ? 'start' : 'middle'}
                        >
                          {fmtTick(t, span)}
                        </text>
                      </g>
                    )
                  })}
                </g>
              </svg>
            )}
          </div>
        )}

        {/* legend + honest per-kind tally (showing N of total; truncation) */}
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line pt-1.5 text-[10px] text-ink-3">
          {tallies.map((t) => (
            <span key={t.kind} className="inline-flex items-center gap-1" data-testid={`timeline-tally-${t.kind}`}>
              <span className="h-2 w-2 rounded-sm" style={{ background: KIND_COLOR[t.kind] }} aria-hidden />
              <span>{LANE_LABEL[t.kind as TimelineItemKind]}</span>
              <span className="font-mono text-ink-2">
                {t.shown}
                {t.truncated ? `/${t.total}` : ''}
              </span>
              {t.truncated && <span className="text-accent-warning">capped</span>}
            </span>
          ))}
          <span className="ml-auto opacity-70">
            dashed = open window · drag to pan · wheel/buttons to zoom · click a bar to inspect
          </span>
        </div>
      </div>
    </PanelChrome>
  )
}
