/**
 * TimeBand — the band along the bottom of every reader surface: the one
 * control that sets the time window every region reads.
 *
 *   - The bar is a chart: how many items were dated to each slice of time,
 *     for the subject in focus or for everything when none is.
 *   - The window is a brush over that chart. Drag it to move it, drag an edge
 *     to resize it, click the chart to centre it there. With the brush
 *     focused, the arrow keys move it and shift + arrows resize it.
 *   - Right of "now" the future side is drawn, with a tick for every open
 *     forecast question at its due date; a tick opens the question on the
 *     Ledger.
 *   - Two times are named apart: "When it happened" (the date the item
 *     carries) is the chart; "When recorded" (when the system wrote it down)
 *     is the lane under it.
 *   - The lane carries the "as recorded" marker, at now by default. Dragged
 *     back, the console shows what the system had recorded by that instant
 *     (`lib/asOf.ts`, `?at=` in the address), and the band says so in the
 *     accent colour until it is returned to now.
 *
 * Every window write goes through `worldState.setWindow` (or `setSpan`, which
 * calls it), so the band stays the window's one writer.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import { Pause, Play } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useWorldState } from './worldState'
import { useTemporalRows } from '@/lib/useTemporalRows'
import { usePointSubject } from '@/state/scope'
import { pointLabel } from '@/components/PointFilter'
import { useQuery } from '@tanstack/react-query'
import { useLedgerForecasts } from '@/panels/ledger/ledgerQueries'
import { apiGet } from '@/lib/api'
import { LEDGER_ROUTE, type LedgerForecastsResponse } from '@/lib/ledgerModel'
import { asOfWords, withAt } from '@/lib/asOf'
import { useLedgerView } from '@/panels/ledger/ledgerView'
import { applyPanelAction } from '@/lib/panelActions'
import type { TimeWindow } from '@/lib/mapTime'
import {
  BAND_PRESETS,
  FUTURE_HORIZON_MS,
  FUTURE_SHARE,
  HISTOGRAM_BUCKETS,
  activePreset,
  activityHistogram,
  centreWindow,
  dayOnly,
  durationWords,
  endsNow,
  forecastTicks,
  keyStepMs,
  moveWindow,
  pastDomain,
  playStep,
  resizeWindow,
  windowWords,
  type ForecastTick,
} from '@/lib/timeBand'

/** One play step a second; each step slides the window a day later. */
const PLAY_INTERVAL_MS = 1_000

type DragMode = 'move' | 'start' | 'end'

/** Within this of now, the marker snaps back to now. */
const NOW_SNAP_MS = 120_000

/** The rows the chart can show as recorded at an instant: those whose row is
 *  written once. A situation's or an event's dates move after it is written,
 *  so neither is counted on a past record. */
const AS_OF_SOURCES = new Set(['signals', 'findings'])

interface Drag {
  mode: DragMode
  x0: number
  w0: TimeWindow
  lo: number
  hi: number
  msPerPx: number
}

/** Now, refreshed every minute so the band's right edge keeps up. */
function useMinuteNow(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 60_000)
    return () => clearInterval(id)
  }, [])
  return now
}

/** Open a forecast question on the Ledger: it becomes the page's point, then the page opens. */
function openForecast(slug: string): void {
  useLedgerView.getState().set({ forecast: slug, filter: 'all' })
  applyPanelAction('view:forecasts', 'open', {}, { origin: 'time-band' })
  applyPanelAction('view:forecasts', 'show_forecast', { forecast: slug }, { origin: 'time-band', noUndo: true })
}

export default function TimeBand() {
  const windowStartMs = useWorldState((s) => s.windowStartMs)
  const windowEndMs = useWorldState((s) => s.windowEndMs)
  const spanMs = useWorldState((s) => s.spanMs)
  const playing = useWorldState((s) => s.playing)
  const setWindow = useWorldState((s) => s.setWindow)
  const setSpan = useWorldState((s) => s.setSpan)
  const setPlaying = useWorldState((s) => s.setPlaying)

  const minuteNow = useMinuteNow()
  const now = Math.max(minuteNow, windowEndMs)
  const win: TimeWindow = { startMs: windowStartMs, endMs: windowEndMs }

  // THE POINT (`state/scope.ts`) narrows the chart and the ticks; none means
  // everything. The point, not whichever surface last claimed Consult's pin:
  // moving between tabs never re-reads the band.
  const point = usePointSubject()
  const subject = point?.id ?? null
  const subjectName = point ? pointLabel(point) : null
  const { rows: liveRows, failed, isLoading } = useTemporalRows({ targetId: subject })
  const asOfMs = useWorldState((s) => s.asOfMs)
  const setAsOf = useWorldState((s) => s.setAsOf)
  const rows = useMemo(
    () =>
      asOfMs === null
        ? liveRows
        : liveRows.filter((r) => AS_OF_SOURCES.has(r.source) && r.recordMs !== null && r.recordMs <= asOfMs),
    [liveRows, asOfMs],
  )
  const liveForecasts = useLedgerForecasts()
  // On a past record the ticks are the questions as the ledger stood then.
  const pastForecasts = useQuery<LedgerForecastsResponse>({
    queryKey: ['ledger-forecasts', 'at', asOfMs],
    queryFn: () => apiGet<LedgerForecastsResponse>(withAt(LEDGER_ROUTE, asOfMs)),
    enabled: asOfMs !== null,
    staleTime: Infinity,
  })
  const forecasts = asOfMs === null ? liveForecasts : pastForecasts

  // The past side's extent holds still while a drag is under way, so the
  // brush does not chase a domain that grows under it.
  const drag = useRef<Drag | null>(null)
  // The past side also always holds the as-recorded instant.
  const live = pastDomain(spanMs, Math.min(windowStartMs, asOfMs ?? windowStartMs), now)
  const { lo, hi } = drag.current ? { lo: drag.current.lo, hi: drag.current.hi } : live
  const span = hi - lo
  const frac = (ms: number) => (span > 0 ? Math.min(1, Math.max(0, (ms - lo) / span)) : 0)

  const happened = useMemo(() => activityHistogram(rows, lo, hi, 'valid'), [rows, lo, hi])
  const recorded = useMemo(() => activityHistogram(rows, lo, hi, 'record'), [rows, lo, hi])
  const peak = Math.max(1, ...happened)
  const recordPeak = Math.max(1, ...recorded)
  const counted = happened.reduce((a, b) => a + b, 0)
  const undated = rows.filter((r) => r.validMs === null).length

  const { ticks, later } = useMemo(
    () => forecastTicks(forecasts.data?.forecasts ?? [], now, { subject }),
    [forecasts.data, now, subject],
  )
  const [shownTick, setShownTick] = useState<ForecastTick | null>(null)

  // Playback: one day later per step, stopping at now.
  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => {
      const s = useWorldState.getState()
      const step = playStep({ startMs: s.windowStartMs, endMs: s.windowEndMs }, Date.now())
      s.setWindow(step.window.startMs, step.window.endMs)
      if (step.done) s.setPlaying(false)
    }, PLAY_INTERVAL_MS)
    return () => clearInterval(id)
  }, [playing])

  const onPlay = () => {
    if (playing) return setPlaying(false)
    // From a window that already ends now, play replays from the band's start.
    if (endsNow(win, now)) {
      const width = win.endMs - win.startMs
      setWindow(lo, lo + width)
    }
    setPlaying(true)
  }

  // Writes are coalesced to one per frame while dragging.
  const pending = useRef<TimeWindow | null>(null)
  const frame = useRef<number | null>(null)
  const write = useCallback(
    (w: TimeWindow) => {
      pending.current = w
      if (frame.current !== null) return
      const flush = () => {
        frame.current = null
        const p = pending.current
        pending.current = null
        if (p) setWindow(p.startMs, p.endMs)
      }
      if (typeof requestAnimationFrame === 'function') frame.current = requestAnimationFrame(flush)
      else flush()
    },
    [setWindow],
  )
  useEffect(
    () => () => {
      if (frame.current !== null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(frame.current)
    },
    [],
  )

  const pastRef = useRef<HTMLDivElement>(null)
  const beginDrag = (mode: DragMode) => (e: PointerEvent<HTMLElement>) => {
    e.stopPropagation()
    e.preventDefault()
    const width = pastRef.current?.getBoundingClientRect().width ?? 0
    if (!(width > 0)) return
    drag.current = { mode, x0: e.clientX, w0: win, lo, hi, msPerPx: span / width }
    setPlaying(false)
    try {
      e.currentTarget.setPointerCapture(e.pointerId)
    } catch {
      // capture is a nicety; a drag still works while the pointer stays on the band
    }
  }
  const onDragMove = (e: PointerEvent<HTMLElement>) => {
    const d = drag.current
    if (!d) return
    const delta = (e.clientX - d.x0) * d.msPerPx
    write(d.mode === 'move' ? moveWindow(d.w0, delta, d.lo, d.hi) : resizeWindow(d.w0, delta, d.lo, d.hi, d.mode))
  }
  const endDrag = () => {
    drag.current = null
  }
  const onChartClick = (e: PointerEvent<HTMLDivElement>) => {
    const rect = pastRef.current?.getBoundingClientRect()
    if (!rect || !(rect.width > 0)) return
    const at = lo + ((e.clientX - rect.left) / rect.width) * span
    setPlaying(false)
    const w = centreWindow(win, at, lo, hi)
    setWindow(w.startMs, w.endMs)
  }

  // The as-recorded marker: dragged along the lane, snapping to now at the end.
  const laneRef = useRef<HTMLDivElement>(null)
  const markerDrag = useRef(false)
  const asOfAt = (clientX: number) => {
    const rect = laneRef.current?.getBoundingClientRect()
    if (!rect || !(rect.width > 0)) return
    const t = lo + Math.min(1, Math.max(0, (clientX - rect.left) / rect.width)) * span
    setAsOf(hi - t <= NOW_SNAP_MS ? null : Math.round(t))
  }
  const onMarkerDown = (e: PointerEvent<HTMLElement>) => {
    e.preventDefault()
    markerDrag.current = true
    setPlaying(false)
    try {
      e.currentTarget.setPointerCapture(e.pointerId)
    } catch {
      // capture is a nicety
    }
  }
  const onMarkerMove = (e: PointerEvent<HTMLElement>) => {
    if (markerDrag.current) asOfAt(e.clientX)
  }
  const onMarkerUp = () => {
    markerDrag.current = false
  }

  const stepMs = keyStepMs(lo, hi)
  const onMarkerKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const cur = asOfMs ?? hi
    let next: number | null | undefined
    if (e.key === 'ArrowLeft') next = Math.max(lo, cur - stepMs)
    else if (e.key === 'ArrowRight') next = cur + stepMs >= hi - NOW_SNAP_MS ? null : cur + stepMs
    else if (e.key === 'Home') next = lo
    else if (e.key === 'End' || e.key === 'Escape') next = null
    if (next === undefined) return
    e.preventDefault()
    setAsOf(next)
  }

  const onBrushKey = (e: KeyboardEvent<HTMLDivElement>) => {
    let next: TimeWindow | null = null
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      const d = e.key === 'ArrowLeft' ? -stepMs : stepMs
      next = e.shiftKey ? resizeWindow(win, d, lo, hi) : moveWindow(win, d, lo, hi)
    } else if (e.key === 'Home') {
      next = moveWindow(win, lo - win.startMs, lo, hi)
    } else if (e.key === 'End') {
      next = moveWindow(win, hi - win.endMs, lo, hi)
    }
    if (!next) return
    e.preventDefault()
    setPlaying(false)
    setWindow(next.startMs, next.endMs)
  }

  const words = windowWords(win, now)
  const preset = activePreset(win, now)
  const startPct = frac(windowStartMs) * 100
  const endPct = frac(windowEndMs) * 100
  const pastPct = (1 - FUTURE_SHARE) * 100
  const chartTitle = isLoading
    ? 'reading…'
    : `${counted} items dated in this stretch${undated > 0 ? `; ${undated} carry no date of their own and are not drawn` : ''}`

  return (
    <div
      className="grid w-full shrink-0 grid-cols-[8.5rem_minmax(0,1fr)] items-center gap-x-3 gap-y-0.5 border-t border-line bg-surf-1 px-3 py-1.5 text-label text-ink-3"
      data-testid="time-scrubber"
      // the one window, as instants: Consult shows and sends exactly this
      data-window-start={windowStartMs}
      data-window-end={windowEndMs}
    >
      {/* Row 1: the window in words, the spans, play, what is counted. */}
      <span className="text-ink-2" title="The time window every region reads">
        Window
      </span>
      <div className="flex min-w-0 items-center gap-3">
        <span className="shrink-0 font-mono text-ink-1" data-testid="time-band-window">
          {words}
        </span>
        <div className="flex shrink-0 items-center gap-0.5" role="group" aria-label="Window span">
          {BAND_PRESETS.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => {
                setPlaying(false)
                setSpan(p.ms)
              }}
              aria-pressed={preset?.id === p.id}
              className={cn(
                'rounded px-1.5 py-0.5 tabular-nums transition-colors',
                'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent-emphasis',
                preset?.id === p.id ? 'bg-surf-3 text-ink-1' : 'text-ink-3 hover:text-ink-1',
              )}
            >
              {p.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={onPlay}
          aria-label={playing ? 'Pause' : 'Play the window forward a day at a time'}
          title={playing ? 'Pause' : 'Play: the window moves a day later each second, and stops at now'}
          className={cn(
            'flex h-5 w-5 shrink-0 items-center justify-center rounded text-ink-2 hover:text-ink-1',
            'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent-emphasis',
          )}
          data-testid="time-band-play"
        >
          {playing ? <Pause className="h-3 w-3" /> : <Play className="h-3 w-3" />}
        </button>
        <span className="min-w-0 truncate" data-testid="time-band-counting">
          Counting: <span className="text-ink-2">{subjectName ?? 'everything'}</span>
          {failed.length > 0 ? <span className="italic"> · {failed.join(', ')} not read</span> : null}
          {asOfMs !== null ? <span> · signals and findings recorded by then</span> : null}
        </span>
        {asOfMs === null ? (
          <span className="shrink-0" data-testid="time-band-as-of">
            As recorded: <span className="text-ink-2">now</span>
          </span>
        ) : (
          <span className="flex shrink-0 items-baseline gap-2 text-point" data-testid="time-band-as-of">
            <span>As recorded {asOfWords(asOfMs)}</span>
            <button
              type="button"
              className="underline underline-offset-2 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent-emphasis"
              onClick={() => setAsOf(null)}
              data-testid="time-band-as-of-return"
            >
              return to now
            </button>
          </span>
        )}
        <span className="ml-auto min-w-0 truncate text-right" data-testid="time-band-tick-detail" aria-live="polite">
          {shownTick ? (
            <>
              <span className="font-mono">due {dayOnly(shownTick.dueMs)}</span>
              <span className="text-ink-1"> · {shownTick.question}</span>
            </>
          ) : null}
        </span>
      </div>

      {/* Row 2: when it happened — the chart, the brush, and the future side. */}
      <span className="text-ink-2" title="When it happened: the date the item carries (published, occurred, assessed)">
        When it happened
      </span>
      <div className="flex h-10 min-w-0 items-stretch">
        <div
          ref={pastRef}
          className="relative h-full cursor-crosshair"
          style={{ width: `${pastPct}%` }}
          onPointerDown={onChartClick}
          data-testid="time-band-chart"
          title={chartTitle}
        >
          <div
            className="absolute inset-0 flex items-end gap-px"
            role="img"
            aria-label={`Items per slice of time, by when they happened. ${chartTitle}`}
            data-testid="time-band-histogram"
            data-buckets={happened.join(',')}
          >
            {happened.map((n, i) => {
              const at = lo + ((i + 0.5) * span) / HISTOGRAM_BUCKETS
              const inWin = at >= windowStartMs && at <= windowEndMs
              return (
                <span
                  key={i}
                  className="flex-1 rounded-t-sm bg-ink-3"
                  style={{ height: n === 0 ? 0 : `${Math.max(8, (n / peak) * 100)}%`, opacity: inWin ? 0.95 : 0.35 }}
                />
              )
            })}
          </div>
          <div
            role="slider"
            tabIndex={0}
            aria-label="Time window"
            aria-valuemin={lo}
            aria-valuemax={hi}
            aria-valuenow={windowStartMs}
            aria-valuetext={words}
            aria-keyshortcuts="ArrowLeft ArrowRight Shift+ArrowLeft Shift+ArrowRight Home End"
            title={`${words}. Drag to move, drag an edge to resize; arrow keys move, shift + arrows resize.`}
            className={cn(
              'absolute inset-y-0 cursor-grab touch-none border-x-2 border-ink-1 active:cursor-grabbing',
              'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent-emphasis',
            )}
            style={{ left: `${startPct}%`, width: `${Math.max(0.4, endPct - startPct)}%` }}
            onPointerDown={beginDrag('move')}
            onPointerMove={onDragMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onKeyDown={onBrushKey}
            data-testid="time-band-brush"
          >
            <span aria-hidden className="pointer-events-none absolute inset-0 bg-ink-1 opacity-10" />
            <span
              aria-hidden
              className="absolute inset-y-0 -left-1.5 w-3 cursor-ew-resize"
              onPointerDown={beginDrag('start')}
              data-testid="time-band-brush-start"
            />
            <span
              aria-hidden
              className="absolute inset-y-0 -right-1.5 w-3 cursor-ew-resize"
              onPointerDown={beginDrag('end')}
              data-testid="time-band-brush-end"
            />
          </div>
        </div>
        <div className="w-px shrink-0 bg-ink-2" aria-hidden title="now" />
        <div
          className="relative h-full bg-surf-2"
          style={{ width: `${100 - pastPct}%` }}
          data-testid="time-band-future"
          title={`The next ${durationWords(FUTURE_HORIZON_MS)}: open forecast questions at their due dates`}
        >
          {ticks.map((t) => (
            <button
              key={t.slug}
              type="button"
              className={cn(
                'absolute inset-y-1 w-1 -translate-x-1/2 rounded-sm bg-ink-2 hover:bg-ink-1',
                'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent-emphasis',
              )}
              style={{ left: `${t.frac * 100}%` }}
              aria-label={`Forecast due ${dayOnly(t.dueMs)}: ${t.question}`}
              title={`Due ${dayOnly(t.dueMs)}: ${t.question}`}
              onMouseEnter={() => setShownTick(t)}
              onMouseLeave={() => setShownTick(null)}
              onFocus={() => setShownTick(t)}
              onBlur={() => setShownTick(null)}
              onClick={() => openForecast(t.slug)}
              data-testid={`time-band-forecast-${t.slug}`}
            />
          ))}
        </div>
      </div>

      {/* Row 3: when recorded — when the system wrote the same items down. */}
      <span className="text-ink-2" title="When recorded: when the system wrote the item down, which can be later than when it happened">
        When recorded
      </span>
      <div className="flex h-4 min-w-0 items-stretch">
        <div ref={laneRef} className="relative h-full" style={{ width: `${pastPct}%` }}>
          <div
            className="absolute inset-0 flex items-end gap-px"
            role="img"
            aria-label="Items per slice of time, by when the system recorded them"
            data-testid="time-band-recorded"
            data-buckets={recorded.join(',')}
          >
            {recorded.map((n, i) => (
              <span
                key={i}
                className="flex-1 bg-ink-3"
                style={{ height: n === 0 ? 0 : `${Math.max(20, (n / recordPeak) * 100)}%`, opacity: 0.5 }}
              />
            ))}
          </div>
          {asOfMs !== null ? (
            <div
              aria-hidden
              className="pointer-events-none absolute inset-y-0 right-0 bg-surf-base opacity-70"
              style={{ left: `${frac(asOfMs) * 100}%` }}
            />
          ) : null}
          <div
            role="slider"
            tabIndex={0}
            aria-label="As recorded"
            aria-valuemin={lo}
            aria-valuemax={hi}
            aria-valuenow={asOfMs ?? hi}
            aria-valuetext={asOfMs === null ? 'now' : `the record as of ${asOfWords(asOfMs)}`}
            title="Drag back to see what the system had recorded by then; End or Escape returns to now"
            className={cn(
              'absolute -inset-y-0.5 w-1.5 -translate-x-1/2 cursor-ew-resize touch-none rounded-sm',
              'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent-emphasis',
              asOfMs === null ? 'bg-ink-2' : 'bg-point',
            )}
            style={{ left: `${frac(asOfMs ?? hi) * 100}%` }}
            onPointerDown={onMarkerDown}
            onPointerMove={onMarkerMove}
            onPointerUp={onMarkerUp}
            onPointerCancel={onMarkerUp}
            onKeyDown={onMarkerKey}
            data-testid="time-band-as-of-marker"
          />
        </div>
        <div className="w-px shrink-0 bg-ink-2" aria-hidden />
        <div className="h-full" style={{ width: `${100 - pastPct}%` }} />
      </div>

      {/* Row 4: the axis. */}
      <span />
      <div className="flex min-w-0 font-mono" aria-hidden>
        <div className="flex justify-between" style={{ width: `${pastPct}%` }}>
          <span>{dayOnly(lo)}</span>
          <span>{dayOnly(lo + span / 2)}</span>
          <span className="pr-1">now</span>
        </div>
        <div className="flex justify-between pl-1" style={{ width: `${100 - pastPct}%` }}>
          <span>{ticks.length > 0 ? `${ticks.length} due` : 'none due'}</span>
          <span>{later > 0 ? `+${durationWords(FUTURE_HORIZON_MS)} (${later} later)` : `+${durationWords(FUTURE_HORIZON_MS)}`}</span>
        </div>
      </div>
    </div>
  )
}
