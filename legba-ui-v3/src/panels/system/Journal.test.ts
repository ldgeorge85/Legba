/**
 * Tests for the Journal panel's pure helpers — `journalRefLabel` (T1.3),
 * plus `journalKindBadge` / `cycleBand` / `splitCycleBands` (h7).
 *
 * `journalRefLabel` — the T1.3 fix
 * (planning/JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09.md §6): a resolved
 * journal ref must never render the bare word "unknown", and a resolved
 * SIGNAL ref must name which source it came from rather than showing a
 * kind-less title/id.
 *
 * Pure-function tests — no DOM, no fetch — mirroring `journalGate.test.ts`'s
 * pattern for this panel's sibling.
 */

import { describe, it, expect } from 'vitest'
import { journalRefLabel, journalKindBadge, cycleBand, splitCycleBands } from './Journal'
import type { JournalEntrySummary, JournalRef } from '@/lib/api'

describe('journalRefLabel', () => {
  it('an unresolved ref (kind="unknown") reads "unresolved · <uuid8>" — never "unknown"', () => {
    const ref: JournalRef = {
      id: '233bd806-5bee-4c3c-b0c3-1c9e794c52ee',
      kind: 'unknown',
      title: null,
    }
    const label = journalRefLabel(ref)
    expect(label).toBe('unresolved · 233bd806')
    expect(label).not.toMatch(/unknown/i)
  })

  it('a resolved signal ref names its source: "signal · <short source> · <title>"', () => {
    const ref: JournalRef = {
      id: 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa',
      kind: 'signal',
      title: 'PRISON: coerce in Kansas, United States',
      source_id: 'source.gdelt.files',
    }
    expect(journalRefLabel(ref)).toBe('signal · gdelt.files · PRISON: coerce in Kansas, United States')
  })

  it('a signal ref with no source_id still says "signal" rather than dropping the kind', () => {
    const ref: JournalRef = {
      id: 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb',
      kind: 'signal',
      title: 'a headline',
    }
    expect(journalRefLabel(ref)).toBe('signal · signal · a headline')
  })

  it('a resolved analyst-output ref reads "<kind> · <title>", the findings convention', () => {
    const ref: JournalRef = {
      id: 'cccccccc-3333-4333-8333-cccccccccccc',
      kind: 'finding',
      title: 'Escalation risk rises in the Gulf',
    }
    expect(journalRefLabel(ref)).toBe('finding · Escalation risk rises in the Gulf')
  })

  it('a resolved non-signal ref with no title falls back to the id, never blank', () => {
    const ref: JournalRef = {
      id: 'dddddddd-4444-4444-8444-dddddddddddd',
      kind: 'situation',
      title: null,
    }
    expect(journalRefLabel(ref)).toBe('situation · dddddddd-4444-4444-8444-dddddddddddd')
  })

  it('a long title is truncated with an ellipsis, never blowing out the chip', () => {
    const longTitle = 'x'.repeat(120)
    const ref: JournalRef = {
      id: 'eeeeeeee-5555-4555-8555-eeeeeeeeeeee',
      kind: 'signal',
      title: longTitle,
      source_id: 'source.middleeasteye.news',
    }
    const label = journalRefLabel(ref)
    expect(label.startsWith('signal · middleeasteye.news · ')).toBe(true)
    expect(label.length).toBeLessThan(longTitle.length)
    expect(label.endsWith('…')).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// h7 — the per-kind badge and the synthesis/diary band split.
//
// The panel showed five entry kinds through one uniformly-grey label and gave
// the cards no kind marker at all, so the operator could not tell a `lens`
// row from an `entry` row while scanning. These cover the two halves of the
// fix: the badge's text+colour per kind, and the band grouping order.
// ---------------------------------------------------------------------------

/** A minimal summary row — only `entry_kind` and `id` matter to the banding. */
function summaryRow(id: string, entryKind: string): JournalEntrySummary {
  return {
    id,
    entry_kind: entryKind,
    title: `${entryKind} ${id}`,
    honesty_flags: [],
    period_start: '2026-09-22T00:00:00Z',
    period_end: '2026-09-23T00:00:00Z',
    produced_at: '2026-09-23T01:00:00Z',
    analyst_id: null,
    analyst_version: null,
    verify_score: null,
  }
}

const ALL_KINDS = [
  'entry',
  'consolidation',
  'chronicle',
  'lens',
  'lens_diff',
  // Program 5 — the stateful tier (the `inquiry` analyst kind).
  'inquiry',
  'crossroads',
]

describe('journalKindBadge', () => {
  it('labels every kind in the operator-facing language, not the raw column value', () => {
    expect(journalKindBadge('entry').label).toBe('Journal')
    expect(journalKindBadge('consolidation').label).toBe('Consolidation')
    expect(journalKindBadge('chronicle').label).toBe('Chronicle')
    expect(journalKindBadge('lens').label).toBe('Lens')
    expect(journalKindBadge('lens_diff').label).toBe('Lens diff')
    expect(journalKindBadge('inquiry').label).toBe('Inquiry')
    expect(journalKindBadge('crossroads').label).toBe('Crossroads')
  })

  it('gives each kind its OWN colour — every kind a distinct class string', () => {
    const classes = ALL_KINDS.map((k) => journalKindBadge(k).className)
    expect(new Set(classes).size).toBe(ALL_KINDS.length)
  })

  it('never paints a kind amber or rose — here those mean unverified and contradicted', () => {
    for (const kind of ALL_KINDS) {
      const { className } = journalKindBadge(kind)
      expect(className).not.toMatch(/amber|rose/)
    }
  })

  it('keeps consolidation on the emerald it already wears as the current inner landscape', () => {
    expect(journalKindBadge('consolidation').className).toMatch(/emerald/)
  })

  it('an unforeseen kind still gets a badge: its raw id, on the neutral token class', () => {
    const badge = journalKindBadge('reverie')
    expect(badge.label).toBe('reverie')
    expect(badge.className).toBe(journalKindBadge('entry').className)
    expect(badge.label).not.toBe('')
  })

  it('gives the Program 5 tier its own colours, not the lens pair reused', () => {
    const inquiry = journalKindBadge('inquiry').className
    const crossroads = journalKindBadge('crossroads').className
    expect(inquiry).not.toBe(journalKindBadge('lens').className)
    expect(crossroads).not.toBe(journalKindBadge('lens_diff').className)
    // and neither falls through to the unforeseen-kind fallback
    expect(inquiry).not.toBe(journalKindBadge('entry').className)
    expect(crossroads).not.toBe(journalKindBadge('entry').className)
  })
})

describe('cycleBand / splitCycleBands', () => {
  it('bands the cross-cycle reading as synthesis', () => {
    expect(cycleBand('chronicle')).toBe('synthesis')
    expect(cycleBand('lens')).toBe('synthesis')
    expect(cycleBand('lens_diff')).toBe('synthesis')
    // Program 5 reads ACROSS cycles by construction, so it bands with the
    // synthesis reading rather than under the diary's reveal cap.
    expect(cycleBand('inquiry')).toBe('synthesis')
    expect(cycleBand('crossroads')).toBe('synthesis')
  })

  it('bands the running self-account as diary — consolidation included', () => {
    expect(cycleBand('entry')).toBe('diary')
    expect(cycleBand('consolidation')).toBe('diary')
  })

  it('bands an unforeseen kind as synthesis, so it leads and is never reveal-capped', () => {
    expect(cycleBand('reverie')).toBe('synthesis')
  })

  it('splits a mixed cycle into the two bands, preserving the incoming order in each', () => {
    // The order groupByCycle hands over: kindPriority, synthesized kinds first.
    const rows = [
      summaryRow('c1', 'consolidation'),
      summaryRow('ch1', 'chronicle'),
      summaryRow('ld1', 'lens_diff'),
      summaryRow('l1', 'lens'),
      summaryRow('e1', 'entry'),
      summaryRow('e2', 'entry'),
    ]
    const { synthesis, diary } = splitCycleBands(rows)
    expect(synthesis.map((r) => r.id)).toEqual(['ch1', 'ld1', 'l1'])
    expect(diary.map((r) => r.id)).toEqual(['c1', 'e1', 'e2'])
  })

  it('puts every row in exactly one band — nothing is dropped', () => {
    const rows = ALL_KINDS.map((k, i) => summaryRow(`r${i}`, k))
    const { synthesis, diary } = splitCycleBands(rows)
    expect(synthesis.length + diary.length).toBe(rows.length)
    const ids = [...synthesis, ...diary].map((r) => r.id).sort()
    expect(ids).toEqual(rows.map((r) => r.id).sort())
  })

  it('an empty cycle yields two empty bands rather than throwing', () => {
    expect(splitCycleBands([])).toEqual({ synthesis: [], diary: [] })
  })
})
