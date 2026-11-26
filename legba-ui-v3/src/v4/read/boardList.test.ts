/**
 * The Board's list as data: the four cuts, the trust words, the rail's
 * change counts and the "when" stamp.
 */
import { describe, expect, it } from 'vitest'
import type { JudgmentFindingRow, JudgmentSince } from '@/lib/morningReadBands'
import { changeCounts, changedRows, checkedRows, dueRows, findingTrust, onSubjectRows, whenText } from './boardList'

function sect<T>(items: T[]) {
  return { items, total: items.length, truncated: false }
}

function since(partial: Partial<JudgmentSince> = {}): JudgmentSince {
  return {
    cursor: '2026-11-23T06:00:00Z',
    server_now: '2026-11-24T19:00:00Z',
    counts: {},
    new_findings: sect([]),
    superseded: sect([]),
    band_changes: sect([]),
    situations: sect([]),
    alerts: sect([]),
    forecasts_due: sect([]),
    ...partial,
  } as JudgmentSince
}

const finding = (id: string, target: string, severity: string, at: string) => ({
  id,
  analyst_id: 'energy_security',
  target_id: target,
  title: `Title ${id}`,
  severity,
  confidence: 0.7,
  faithfulness_score: 0.9,
  effective_confidence: 0.63,
  produced_at: at,
})

function check(id: string, verification: Record<string, unknown> | null, sources: string[]): JudgmentFindingRow {
  return {
    id,
    kind: 'finding',
    title: `Title ${id}`,
    analyst_id: 'energy_security',
    analyst_version: null,
    target_id: 'country_a',
    target_version: null,
    produced_at: '2026-11-24T10:00:00Z',
    severity: 'medium',
    confidence: 0.7,
    schema_uri: 'x',
    verification,
    citations: sources.map((s, i) => ({ ordinal: i + 1, source: s, source_id: s, produced_at: null, single_source: false, wire_folded: false, marker_class: null })),
  }
}

describe('trust words', () => {
  it('at most two plain words, with their number', () => {
    expect(findingTrust(check('f', { unsupported_spans: [] }, ['a', 'b', 'a']))).toEqual([
      { text: 'Checked', title: 'The check ran and flagged nothing' },
      { text: '2 sources', title: '3 citations' },
    ])
    const flagged = findingTrust(check('f', { unsupported_spans: [{ text: 'x' }, { text: 'y' }] }, ['a']))
    expect(flagged.map((w) => w.text)).toEqual(['2 claims flagged', 'Single source'])
    expect(findingTrust(check('f', { score_state: 'pending' }, [])).map((w) => w.text)).toEqual(['Not yet checked', 'No source cited'])
  })

  it('without the check row, only a verified-finding alert says Checked; nothing is guessed', () => {
    expect(findingTrust(undefined)).toEqual([])
    expect(findingTrust(undefined, true).map((w) => w.text)).toEqual(['Checked'])
  })

  it('no row carries an engine word', () => {
    const words = [
      ...findingTrust(check('f', { unsupported_spans: [] }, ['a'])),
      ...findingTrust(check('f', { unsupported_spans: [{ text: 'x' }] }, [])),
      ...findingTrust(check('f', { score_state: 'absent' }, ['a', 'b'])),
    ].map((w) => w.text.toLowerCase())
    for (const w of words) expect(w).not.toMatch(/judge|grader|faithfulness|verify|unit|confidence/)
  })
})

describe('the Changed cut', () => {
  it('keeps the daily read ranking and names the subject, never its id', () => {
    const s = since({
      new_findings: sect([finding('f1', 'country_a', 'medium', '2026-11-24T10:00:00Z'), finding('f2', 'country_b', 'high', '2026-11-24T09:00:00Z')]),
      band_changes: sect([
        { target_id: 'country_a', dimension: 'leadership_transition', from_band: 'watch', to_band: 'elevated', direction: 'deterioration', severity: 'low', from_scorecard_row_id: 'a', to_scorecard_row_id: 'b', changed_at: '2026-11-24T08:00:00Z' },
      ]),
    })
    const rows = changedRows(s, new Map([['f2', check('f2', { unsupported_spans: [] }, ['x', 'y'])]]))
    expect(rows.map((r) => r.key)).toEqual(['finding:f2', 'finding:f1', 'band:b'])
    expect(rows[0].subject).toBe('Country B')
    expect(rows[0].trust.map((t) => t.text)).toEqual(['Checked', '2 sources'])
    expect(rows[1].trust).toEqual([])
    expect(rows[2].sentence).toBe('Leadership transition moved from watch to elevated')
    expect(rows[0].open).toEqual({ rowKind: 'finding', id: 'f2', label: 'Title f2' })
    expect(onSubjectRows(rows, 'country_a').map((r) => r.key)).toEqual(['finding:f1', 'band:b'])
  })

  it('an alert left as a row is said in words, without its machine prefix', () => {
    const s = since({
      alerts: sect([{ id: 'a1', severity: 'high', channel: 'verified_finding', summary: 'Verified high-severity finding: Strait closes on military_posture', target_id: 'country_a', produced_at: '2026-11-24T10:00:00Z' }]),
    })
    expect(changedRows(s, new Map())[0].sentence).toBe('Strait closes on military_posture')
  })
})

describe('the Checked cut', () => {
  it('flagged first (its first flagged claim is the sentence), then held, then not checked; exempt producers left out', () => {
    const rows = checkedRows([
      check('held', { unsupported_spans: [] }, ['a']),
      check('pending', { score_state: 'pending' }, []),
      check('flagged', { unsupported_spans: [{ text: '**BLUF:** the claim' }] }, ['a']),
    ])
    expect(rows.map((r) => r.key)).toEqual(['finding:flagged', 'finding:held', 'finding:pending'])
    expect(rows[0].sentence).toBe('**BLUF:** the claim')
    expect(rows[0].detail).toBe('Title flagged')
  })
})

describe('the Due cut', () => {
  it('longest overdue first, two plain words, and the frozen test as the detail', () => {
    const due = (id: string, days: number, mark: string) => ({
      id, region: 'country_a', event_class: 'border_clash', window_start: '', window_end: '2026-11-20T00:00:00Z', p: 0.3, p_base: 0.2,
      method: 'm', method_version: null, scale_version: null, resolution_test: `test ${id}`, resolved_by: null, issued_at: '', days_overdue: days, mark,
    })
    const rows = dueRows([due('d1', 1.2, 'in_grace'), due('d2', 4.7, 'expired')])
    expect(rows.map((r) => r.key)).toEqual(['due:d2', 'due:d1'])
    expect(rows[0].trust.map((t) => t.text)).toEqual(['Expired unanswered', '4 days past'])
    expect(rows[0].sentence).toBe('Border clash, called at 30 %: its window closed with no outcome recorded')
    expect(rows[0].detail).toBe('test d2')
    expect(rows[0].open).toBeNull()
  })
})

describe('the rail', () => {
  it('counts changes per subject; a row with no subject is not counted', () => {
    const s = since({
      new_findings: sect([
        finding('f1', 'country_a', 'medium', '2026-11-24T10:00:00Z'),
        finding('f2', 'country_a', 'low', '2026-11-24T09:00:00Z'),
        { ...finding('f3', 'country_b', 'low', '2026-11-24T09:00:00Z'), target_id: null },
      ]),
    })
    expect([...changeCounts(s)]).toEqual([['country_a', 2]])
  })
})

describe('when', () => {
  it('the hour today, the day before that, in UTC', () => {
    const now = Date.parse('2026-11-24T19:00:00Z')
    expect(whenText('2026-11-24T18:01:00Z', now)).toBe('18:01Z')
    expect(whenText('2026-11-23T18:01:00Z', now)).toBe('23 Nov')
    expect(whenText(null, now)).toBe('')
  })
})
