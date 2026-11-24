/**
 * Figure — the absence contract as a TYPE (UI-D1 §6.1–6.2).
 *
 * Two halves. The `@ts-expect-error` block is the NEGATIVE type test: each of
 * those lines must fail to type-check, and `tsc --noEmit` (part of the UI
 * check) fails with "Unused '@ts-expect-error' directive" the day one of them
 * starts compiling — i.e. the day a figure without an as-of, or an absent
 * figure carrying a 0, becomes expressible. The render half proves absence
 * prints a phrase from the closed vocabulary and never a 0, while a MEASURED
 * zero prints as 0.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import {
  Figure,
  FIGURE_ABSENT_STATES,
  figureText,
  figureValueText,
  asOfText,
  unitForCount,
  type FigureData,
} from './Figure'
import { ABSENCE_VOCABULARY } from './absenceVocabulary'

describe('Figure — what does not type-check', () => {
  it('a figure without an as-of, or with a null one, is a type error', () => {
    // @ts-expect-error — `asOf` is required: a figure without an as-of does not type-check
    const noAsOf: FigureData = { state: 'measured', value: 1, unit: 'n' }
    // @ts-expect-error — `null` is not an as-of; an unknown instant is `{absent: reason}`
    const nullAsOf: FigureData = { state: 'measured', value: 1, unit: 'n', asOf: null }
    // @ts-expect-error — `undefined` is not an as-of either
    const undefAsOf: FigureData = { state: 'measured', value: 1, unit: 'n', asOf: undefined }
    // @ts-expect-error — an absent as-of must say WHY
    const noReason: FigureData = { state: 'measured', value: 1, unit: 'n', asOf: {} }
    expect([noAsOf, nullAsOf, undefAsOf, noReason]).toHaveLength(4)
  })

  it('a measured figure must carry a number; an absent one must not', () => {
    // @ts-expect-error — `measured` with a null value: absence is a STATE, not a null number
    const measuredNull: FigureData = { state: 'measured', value: null, unit: 'n', asOf: 'x' }
    // @ts-expect-error — an absent figure cannot smuggle a 0 (`value={x ?? 0}`)
    const absentZero: FigureData = { state: 'unmeasured', value: 0, unit: 'n', asOf: 'x' }
    // @ts-expect-error — the unit is required
    const noUnit: FigureData = { state: 'measured', value: 3, asOf: 'x' }
    // @ts-expect-error — a state outside the figure's absence phrases
    const badState: FigureData = { state: 'zero', unit: 'n', asOf: 'x' }
    expect([measuredNull, absentZero, noUnit, badState]).toHaveLength(4)
  })

  it('the JSX form refuses a missing as-of too', () => {
    // @ts-expect-error — <Figure> without asOf
    const el = <Figure state="measured" value={2} unit="n" />
    expect(el).toBeTruthy()
  })

  it('the valid shapes compile', () => {
    const ok: FigureData[] = [
      { state: 'measured', value: 0, unit: 'typed', asOf: '2026-10-04 07:00Z' },
      { state: 'unmeasured', unit: 'firing', asOf: { absent: 'the route did not answer' } },
      { state: 'no_read_yet', value: null, unit: 'reads', asOf: { absent: 'no run in window' } },
    ]
    expect(ok).toHaveLength(3)
  })
})

describe('Figure — what it renders', () => {
  it('a MEASURED zero renders as 0, with its unit and as-of', () => {
    render(<Figure state="measured" value={0} unit="typed" asOf="2026-10-04 07:00Z" testId="f" />)
    expect(screen.getByTestId('f-value')).toHaveTextContent('0 typed')
    expect(screen.getByTestId('f-asof')).toHaveTextContent('as of 2026-10-04 07:00Z')
    expect(screen.getByTestId('f')).toHaveAttribute('data-state', 'measured')
  })

  it('an absent figure renders its phrase — never 0, never blank', () => {
    for (const state of FIGURE_ABSENT_STATES) {
      const { unmount } = render(
        <Figure state={state} unit="firing" asOf={{ absent: 'the route did not answer' }} testId="f" />,
      )
      const value = screen.getByTestId('f-value')
      expect(value).toHaveTextContent(ABSENCE_VOCABULARY[state].phrase)
      expect(value.textContent).not.toMatch(/\b0\b/)
      expect(value.textContent?.trim()).not.toBe('')
      expect(screen.getByTestId('f')).toHaveAttribute('data-state', state)
      unmount()
    }
  })

  it('the default absent rendering reads "unmeasured" (the footer word)', () => {
    render(<Figure state="unmeasured" unit="firing" asOf={{ absent: 'no route' }} testId="f" />)
    expect(screen.getByTestId('f-value')).toHaveTextContent(/^unmeasured$/)
  })

  it('an absent as-of states its reason in words', () => {
    render(<Figure state="measured" value={5} unit="n" asOf={{ absent: 'the route carries no timestamp' }} testId="f" />)
    expect(screen.getByTestId('f-asof')).toHaveTextContent('as of — (the route carries no timestamp)')
    expect(screen.getByTestId('f-asof')).toHaveAttribute('data-asof-absent')
  })

  it('a NaN that slipped through untyped JSON prints "unmeasured", not NaN or 0', () => {
    const fig = { state: 'measured', value: Number.NaN, unit: 'n', asOf: 'x' } as FigureData
    expect(figureValueText(fig)).toBe('unmeasured')
    render(<Figure {...fig} testId="f" />)
    expect(screen.getByTestId('f-value')).toHaveTextContent('unmeasured')
    expect(screen.getByTestId('f')).toHaveAttribute('data-state', 'unmeasured')
  })

  it('a % unit is glued to its number; a word unit is spaced; format is honoured', () => {
    expect(figureValueText({ state: 'measured', value: 0.5, unit: '%', asOf: 'x' }, (v) => (v * 100).toFixed(0))).toBe('50%')
    expect(figureValueText({ state: 'measured', value: 38, unit: 'firing', asOf: 'x' })).toBe('38 firing')
  })

  it('the whole-sentence form carries label, value, unit and as-of', () => {
    expect(figureText({ label: 'sources', state: 'measured', value: 38, unit: 'firing', asOf: '07:00Z' })).toBe(
      'sources: 38 firing, as of 07:00Z',
    )
    expect(figureText({ label: 'judge', state: 'unmeasured', unit: 'mean', asOf: { absent: 'no samples' } })).toBe(
      'judge: unmeasured (mean), as of — (no samples)',
    )
    expect(asOfText('t')).toBe('as of t')
  })

  it('every absent state the Figure accepts is a phrase in the closed vocabulary', () => {
    for (const s of FIGURE_ABSENT_STATES) expect(ABSENCE_VOCABULARY[s]).toBeDefined()
  })
})

describe('W8-F6 — a count noun agrees with its count', () => {
  it('prints "1 row", "2 rows", "0 rows"', () => {
    expect(figureValueText({ state: 'measured', value: 1, unit: 'rows', asOf: 'x' })).toBe('1 row')
    expect(figureValueText({ state: 'measured', value: 2, unit: 'rows', asOf: 'x' })).toBe('2 rows')
    expect(figureValueText({ state: 'measured', value: 0, unit: 'rows', asOf: 'x' })).toBe('0 rows')
    expect(figureValueText({ state: 'measured', value: 1, unit: 'entries', asOf: 'x' })).toBe('1 entry')
  })
  it('leaves symbols, short units and non-plural words alone', () => {
    expect(unitForCount(1, 'ms')).toBe('ms')
    expect(unitForCount(1, 'bps')).toBe('bps')
    expect(unitForCount(1, '%')).toBe('%')
    expect(unitForCount(1, 'firing')).toBe('firing')
    expect(unitForCount(1, 'class')).toBe('class')
    expect(unitForCount(1, 'Rows')).toBe('Rows')
  })
  it('the rendered value slot says "1 row"', () => {
    render(<Figure state="measured" value={1} unit="rows" asOf="09:30Z" testId="f1" />)
    expect(screen.getByTestId('f1-value').textContent).toBe('1 row')
    // data-unit keeps the declared unit verbatim.
    expect(screen.getByTestId('f1').getAttribute('data-unit')).toBe('rows')
  })
})
