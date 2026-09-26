import { cn } from '@/lib/cn'
import {
  readScaleStamp,
  scaleStampLabel,
  scaleStampTooltip,
  type ScaleStamp as Stamp,
} from '@/lib/scaleStamp'

/**
 * ScaleStamp — the chip that says which scale an instrument number is on (K3).
 *
 * Sits beside every published number on a reader surface: the situation
 * intensity, the scorecard bands, the due forecasts' `p`, the desk baselines.
 * It renders `scale · method` — two stamps answering two different questions
 * (does this number MEAN the same thing / was it produced by the same code) —
 * and its tooltip says what a scale change costs a comparison, because a
 * reader meeting a version string has no reason to guess.
 *
 * The rule the chip exists to hold: **an unstamped row renders as unstamped.**
 * `—` and `0` and a back-filled current version would each be a claim the
 * platform has not earned; "unstamped (pre-2026-09)" is the one truthful
 * render, and it is muted so it reads as a caveat rather than a value.
 *
 * Takes EITHER a raw row (`row` — the stamps read off the top level or out of
 * `data`) or an already-read `stamp`, so a call site that has the halves as
 * separate projected fields does not have to fake a row shape.
 */
export function ScaleStamp({
  row,
  stamp,
  showUnstamped = true,
  className,
  testId,
}: {
  /** An instrument row; the stamps are read off it defensively. */
  row?: unknown
  /** A pre-read stamp, when the call site already has one. */
  stamp?: Stamp
  /** False hides the chip entirely on an unstamped row — for a dense list
   *  where a per-row caveat would drown the rows. The default SHOWS it: the
   *  absence of a scale is information, not noise. */
  showUnstamped?: boolean
  className?: string
  testId?: string
}) {
  const s = stamp ?? readScaleStamp(row)
  if (s.state === 'unstamped' && !showUnstamped) return null
  const unstamped = s.state === 'unstamped'
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center rounded border border-line bg-surf-2',
        'px-1.5 py-0.5 font-mono text-label',
        unstamped ? 'italic text-ink-3' : 'text-ink-2',
        className,
      )}
      title={scaleStampTooltip(s)}
      data-testid={testId ?? 'scale-stamp'}
      data-stamp-state={s.state}
    >
      {scaleStampLabel(s)}
    </span>
  )
}
