/**
 * Figure — the one primitive for a displayed number (UI-D1 §6.1–6.2).
 *
 * Every figure the workstation shows carries its UNIT and its AS-OF, and an
 * absent reading renders as absence — a phrase from the closed vocabulary
 * (`lib/absenceVocabulary.ts`) — never as 0. A *measured* zero stays a zero.
 * The honesty footer (`lib/coverageFooter.ts`) is the reference
 * implementation of those rules; this primitive makes them a TYPE, so a new
 * surface cannot forget them:
 *
 *   - `asOf` is `string | {absent: reason}` and is REQUIRED: a figure without
 *     an as-of does not type-check, and "we have no instant for this" has to
 *     be said, with its reason, rather than left out.
 *   - `state: 'measured'` requires a `value: number`; every absent state
 *     forbids one (`value?: null`), so `value={x ?? 0}` on an absent reading
 *     is a type error rather than a silent zero.
 *
 *   <Figure state="measured" value={38} unit="firing" asOf="2026-10-04 07:00Z" />
 *   <Figure state="unmeasured" unit="firing" asOf={{ absent: 'the route did not answer' }} />
 *
 * The value renders in the mono face with tabular figures (§5.4 change 2/7),
 * so a polled number does not jitter.
 */
import { absenceMeaning, absencePhrase, type AbsencePhraseId } from '@/lib/absenceVocabulary'
import { cn } from '@/lib/cn'

/** When a figure was true — or, stated with its reason, that no instant is known. */
export type AsOf = string | { absent: string }

/** The absence phrases a single figure slot may print (a subset of the vocabulary). */
export type FigureAbsentState = Extract<AbsencePhraseId, 'unmeasured' | 'not_measured' | 'no_read_yet' | 'dash'>

export const FIGURE_ABSENT_STATES: readonly FigureAbsentState[] = [
  'unmeasured',
  'not_measured',
  'no_read_yet',
  'dash',
]

/** A measured figure: a number, its unit, its as-of. */
export interface MeasuredFigure {
  state: 'measured'
  value: number
  unit: string
  asOf: AsOf
}

/** An absent figure: no number — the phrase says why — but still a unit and an as-of. */
export interface AbsentFigure {
  state: FigureAbsentState
  value?: null
  unit: string
  asOf: AsOf
}

export type FigureData = MeasuredFigure | AbsentFigure

export type FigureProps = FigureData & {
  /** The short noun in front of the value ("sources"). Optional. */
  label?: string
  /** Number → text. Default: `String(value)` (no rounding, no locale). */
  format?: (value: number) => string
  className?: string
  testId?: string
}

/** True when the figure carries a renderable measured number (a finite one). */
export function isMeasured(fig: FigureData): boolean {
  return fig.state === 'measured' && Number.isFinite(fig.value)
}

/**
 * The absence phrase a figure prints, or `null` when it prints its number. A
 * `measured` figure whose value is not finite (a NaN that slipped through
 * untyped JSON) prints `unmeasured` — never a number.
 */
export function figureAbsence(fig: FigureData): FigureAbsentState | null {
  if (fig.state === 'measured') return Number.isFinite(fig.value) ? null : 'unmeasured'
  return fig.state
}

/**
 * A plural count noun agrees with its count (W8-F6: "1 rows" → "1 row").
 * Only a lowercase word of 4+ letters ending in `s` (not `ss`) is a count
 * noun here — `ms`, `s`, `bps`, `%` and every other symbol pass through.
 */
export function unitForCount(value: number, unit: string): string {
  if (value !== 1 || !/^[a-z]{3,}s$/.test(unit) || unit.endsWith('ss')) return unit
  return unit.endsWith('ies') ? `${unit.slice(0, -3)}y` : unit.slice(0, -1)
}

/** A unit glued to its number ("12%") or set after a space ("38 firing"). */
function joinUnit(text: string, unit: string, value?: number): string {
  if (!unit) return text
  const u = value === undefined ? unit : unitForCount(value, unit)
  return /^[%‰°]/.test(u) ? `${text}${u}` : `${text} ${u}`
}

/** The as-of, in words. */
export function asOfText(asOf: AsOf): string {
  return typeof asOf === 'string' ? `as of ${asOf}` : `as of — (${asOf.absent})`
}

/**
 * The figure's value slot as text: the formatted number with its unit, or the
 * absence phrase. A measured value that is not a finite number (a NaN that
 * slipped through untyped JSON) prints as `unmeasured`, never as a number.
 */
export function figureValueText(fig: FigureData, format: (v: number) => string = String): string {
  const absent = figureAbsence(fig)
  if (absent !== null || fig.state !== 'measured') return absencePhrase(absent ?? 'unmeasured')
  return joinUnit(format(fig.value), fig.unit, fig.value)
}

/** The whole figure as one sentence (a tooltip, an aria-label, a CSV cell). */
export function figureText(fig: FigureData & { label?: string }, format?: (v: number) => string): string {
  const value = figureValueText(fig, format)
  const unitNote = isMeasured(fig) || !fig.unit ? '' : ` (${fig.unit})`
  return `${fig.label ? `${fig.label}: ` : ''}${value}${unitNote}, ${asOfText(fig.asOf)}`
}

export function Figure(props: FigureProps) {
  const { label, format = String, className, testId = 'figure' } = props
  const absentId: AbsencePhraseId | null = figureAbsence(props)
  const measuredValue = absentId === null && props.state === 'measured' ? props.value : null
  return (
    <span
      className={cn('inline-flex items-baseline gap-1', className)}
      data-testid={testId}
      data-state={absentId ?? 'measured'}
      data-unit={props.unit}
      aria-label={figureText(props, format)}
    >
      {label && <span className="text-ink-3">{label}</span>}
      {measuredValue !== null ? (
        <span className="font-mono tabular-nums text-ink-1" data-testid={`${testId}-value`}>
          {joinUnit(format(measuredValue), props.unit, measuredValue)}
        </span>
      ) : (
        <span
          className="text-ink-3"
          data-testid={`${testId}-value`}
          title={absentId ? absenceMeaning(absentId) : undefined}
        >
          {absencePhrase(absentId ?? 'unmeasured')}
        </span>
      )}
      <span
        className={cn('text-ink-3', typeof props.asOf === 'string' && 'font-mono')}
        data-testid={`${testId}-asof`}
        data-asof-absent={typeof props.asOf === 'string' ? undefined : ''}
      >
        {asOfText(props.asOf)}
      </span>
    </span>
  )
}
