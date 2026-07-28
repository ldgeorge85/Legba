/** P2-5 — goldsetModel: DOM-free worksheet logic (progress, local upsert,
 *  honest empty states, verdict vocabulary). */
import { describe, expect, it } from 'vitest'
import {
  applyLabel,
  emptyStateMessage,
  VERDICT_OPTIONS,
  worksheetProgress,
  type GoldsetLabelState,
  type GoldsetWorksheet,
  type GoldsetWorksheetItem,
} from './goldsetModel'

function item(id: string, label: GoldsetLabelState | null = null): GoldsetWorksheetItem {
  return {
    finding_id: id,
    unit: 'escalation',
    target_id: 'country_g20_us',
    title: `finding ${id}`,
    body: 'the read [1]',
    data: {},
    citations: [],
    faithfulness: 0.9,
    produced_at: '2026-07-20T00:00:00Z',
    superseded: false,
    label,
  }
}

function saved(id: string, label: GoldsetLabelState['label'] = 'correct'): GoldsetLabelState {
  return {
    id: `lbl-${id}`,
    finding_id: id,
    unit_analyst_id: 'escalation',
    target_id: 'country_g20_us',
    label,
    rationale: null,
    labeled_by: 'lewis',
    labeled_at: '2026-07-23T12:00:00Z',
    created_at: '2026-07-23T12:00:00Z',
  }
}

function ws(items: GoldsetWorksheetItem[]): GoldsetWorksheet {
  const labeled = items.filter((i) => i.label !== null).length
  return {
    week: '2026-W30',
    week_started_at: '2026-07-20T00:00:00Z',
    next_sample_at: '2026-07-27T00:00:00Z',
    sample_size: items.length,
    labeled_count: labeled,
    all_labeled: items.length > 0 && labeled === items.length,
    items,
  }
}

describe('worksheetProgress', () => {
  it('counts labeled items off the items themselves', () => {
    const p = worksheetProgress(ws([item('a'), item('b', saved('b'))]))
    expect(p).toEqual({ total: 2, labeled: 1, allLabeled: false })
  })

  it('an empty worksheet is never "all labeled"', () => {
    expect(worksheetProgress(ws([]))).toEqual({ total: 0, labeled: 0, allLabeled: false })
    expect(worksheetProgress(null)).toEqual({ total: 0, labeled: 0, allLabeled: false })
  })

  it('all items labeled → allLabeled', () => {
    const p = worksheetProgress(ws([item('a', saved('a')), item('b', saved('b'))]))
    expect(p.allLabeled).toBe(true)
  })
})

describe('applyLabel', () => {
  it('upserts the verdict into the matching item and recounts (immutably)', () => {
    const before = ws([item('a'), item('b')])
    const after = applyLabel(before, saved('a', 'incorrect'))
    expect(after.items[0].label?.label).toBe('incorrect')
    expect(after.labeled_count).toBe(1)
    expect(after.all_labeled).toBe(false)
    // Never mutates the input.
    expect(before.items[0].label).toBeNull()
    expect(before.labeled_count).toBe(0)
  })

  it('re-labeling replaces the verdict without double-counting', () => {
    const once = applyLabel(ws([item('a'), item('b', saved('b'))]), saved('a'))
    const twice = applyLabel(once, saved('a', 'partially_correct'))
    expect(twice.items[0].label?.label).toBe('partially_correct')
    expect(twice.labeled_count).toBe(2)
    expect(twice.all_labeled).toBe(true)
  })

  it('a verdict for a finding outside the sample is a no-op on the items', () => {
    const before = ws([item('a')])
    const after = applyLabel(before, saved('zz'))
    expect(after.items).toEqual(before.items)
    expect(after.labeled_count).toBe(0)
  })
})

describe('emptyStateMessage', () => {
  it('unlabeled work → no empty state', () => {
    expect(emptyStateMessage(ws([item('a')]))).toBeNull()
    expect(emptyStateMessage(null)).toBeNull()
  })

  it('exhausted week → "all labeled — next sample Monday"', () => {
    expect(emptyStateMessage(ws([item('a', saved('a'))]))).toBe(
      'all labeled — next sample Monday',
    )
  })

  it('no eligible candidates → says so honestly (not "all labeled")', () => {
    const msg = emptyStateMessage(ws([]))
    expect(msg).toContain('no verified findings eligible')
    expect(msg).toContain('next sample Monday')
  })
})

describe('VERDICT_OPTIONS', () => {
  it('carries exactly the closed server vocabulary, in display order', () => {
    expect(VERDICT_OPTIONS.map((o) => o.value)).toEqual([
      'correct',
      'partially_correct',
      'incorrect',
      'unresolvable',
    ])
  })

  it('every option has a label, hint, and tone for the buttons', () => {
    for (const o of VERDICT_OPTIONS) {
      expect(o.label.length).toBeGreaterThan(0)
      expect(o.hint.length).toBeGreaterThan(0)
      expect(['good', 'warn', 'bad', 'muted']).toContain(o.tone)
    }
  })
})
