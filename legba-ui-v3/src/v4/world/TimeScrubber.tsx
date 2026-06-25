/**
 * TimeScrubber — compact bottom bar (~44px) that drives the World's time window.
 *
 * Reads the orchestrator-owned world store. A range slider over
 * [windowStartMs (≈now−24h), now] is bound to windowEndMs; the map renders
 * events up to time T = windowEndMs. Play advances T forward in wall-clock-ish
 * ticks until it reaches "now", then auto-stops at LIVE.
 */
import { useEffect, useRef } from 'react'
import { Play, Pause } from 'lucide-react'
import { format } from 'date-fns'
import { cn } from '@/lib/cn'
import { useWorldState } from './worldState'

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
