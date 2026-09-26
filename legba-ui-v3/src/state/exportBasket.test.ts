/**
 * Unit tests for the export collection basket (A10) — the DOM-free logic
 * (parse/add/remove/cap) plus the zustand store's add/remove/clear/has
 * contract and its localStorage persistence round-trip.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  BASKET_MAX_ITEMS,
  addToBasket,
  buildDeskBrief,
  buildDeskBriefAppendix,
  composeDeskBrief,
  parseBasket,
  removeFromBasket,
  useExportBasket,
  type DeskBriefFetchers,
  type ExportBasketItem,
} from './exportBasket'
import type { ReadFindingRow } from '@/lib/assemblyModel'

const item = (id: string, kind: ExportBasketItem['kind'] = 'finding'): ExportBasketItem => ({
  kind,
  id,
  label: `label-${id}`,
})

describe('parseBasket', () => {
  it('parses a persisted list and drops malformed entries', () => {
    const raw = JSON.stringify([
      { kind: 'finding', id: 'f1', label: 'A' },
      { kind: 'journal_entry', id: 'j1' },
      { kind: 'situation', id: 'x' }, // unknown kind → dropped
      { kind: 'finding' }, // no id → dropped
      'garbage',
      { kind: 'finding', id: 'f1', label: 'dup' }, // dup → dropped
    ])
    expect(parseBasket(raw)).toEqual([
      { kind: 'finding', id: 'f1', label: 'A' },
      { kind: 'journal_entry', id: 'j1', label: undefined },
    ])
  })

  it('returns [] for null / invalid JSON / non-array shapes', () => {
    expect(parseBasket(null)).toEqual([])
    expect(parseBasket('not json')).toEqual([])
    expect(parseBasket('{"a":1}')).toEqual([])
  })

  it('caps a persisted overlong list at BASKET_MAX_ITEMS', () => {
    const raw = JSON.stringify(
      Array.from({ length: BASKET_MAX_ITEMS + 10 }, (_, i) => ({ kind: 'finding', id: `f${i}` })),
    )
    expect(parseBasket(raw)).toHaveLength(BASKET_MAX_ITEMS)
  })
})

describe('addToBasket / removeFromBasket', () => {
  it('adds, dedupes on (kind, id), and allows the same id across kinds', () => {
    let items: ExportBasketItem[] = []
    items = addToBasket(items, item('f1'))
    expect(items).toHaveLength(1)
    // Duplicate → SAME reference back (the rejection signal).
    expect(addToBasket(items, item('f1'))).toBe(items)
    // Same id, different kind → distinct item.
    items = addToBasket(items, item('f1', 'journal_entry'))
    expect(items).toHaveLength(2)
  })

  it('rejects adds beyond BASKET_MAX_ITEMS', () => {
    let items: ExportBasketItem[] = []
    for (let i = 0; i < BASKET_MAX_ITEMS; i++) items = addToBasket(items, item(`f${i}`))
    expect(items).toHaveLength(BASKET_MAX_ITEMS)
    expect(addToBasket(items, item('overflow'))).toBe(items)
  })

  it('removes by (kind, id)', () => {
    let items = [item('f1'), item('f2'), item('f1', 'journal_entry')]
    items = removeFromBasket(items, 'finding', 'f1')
    expect(items.map((i) => `${i.kind}:${i.id}`)).toEqual(['finding:f2', 'journal_entry:f1'])
  })
})

describe('useExportBasket store', () => {
  beforeEach(() => {
    localStorage.clear()
    useExportBasket.getState().clear()
  })

  it('add() returns true on success, false on duplicate, and persists', () => {
    const { add } = useExportBasket.getState()
    expect(add(item('f1'))).toBe(true)
    expect(add(item('f1'))).toBe(false)
    expect(useExportBasket.getState().items).toHaveLength(1)
    // Persistence round-trip through the same parser the store boots from.
    expect(parseBasket(localStorage.getItem('legba_export_basket_v1'))).toEqual([
      { kind: 'finding', id: 'f1', label: 'label-f1' },
    ])
  })

  it('has() / remove() / clear() work and persist', () => {
    const s = useExportBasket.getState()
    s.add(item('f1'))
    s.add(item('j1', 'journal_entry'))
    expect(useExportBasket.getState().has('finding', 'f1')).toBe(true)
    useExportBasket.getState().remove('finding', 'f1')
    expect(useExportBasket.getState().has('finding', 'f1')).toBe(false)
    expect(useExportBasket.getState().items).toHaveLength(1)
    useExportBasket.getState().clear()
    expect(useExportBasket.getState().items).toEqual([])
    expect(parseBasket(localStorage.getItem('legba_export_basket_v1'))).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Desk Brief (7b-iii)
// ---------------------------------------------------------------------------

/** A minimal valid `assembly.v1` block — one span with a real origin, so
 *  `projectAssembly`/`projectBlock` (assemblyModel.ts) accept it. */
function block(unit: string, findingId: string, ordinal: number) {
  return {
    ordinal,
    finding_id: findingId,
    desk: unit,
    target_id: 'country_g20_RR',
    target_name: 'Ruritania',
    question: `${unit} question`,
    spans: [
      {
        role: 'body',
        text: 'quoted sentence',
        origin: {
          head_id: findingId,
          start: 0,
          end: 10,
          body_sha256: 'a'.repeat(64),
          body_len: 20,
        },
        markers: [],
        scope_tokens: [],
      },
    ],
  }
}

function compositionRow(opts: {
  id: string
  title?: string
  coverage?: Array<{ unit: string; unit_name?: string | null; status: string }>
  blocks?: unknown[]
}): ReadFindingRow {
  return {
    id: opts.id,
    title: opts.title ?? 'Ruritania composition',
    produced_at: '2026-09-01T00:00:00Z',
    analyst_id: 'country_composition',
    target_id: 'country_g20_RR',
    data: {
      data: {
        assembly: {
          schema: 'assembly.v1',
          regime: 'assembly',
          tier: 'country',
          as_of: '2026-09-01T00:00:00Z',
          blocks: opts.blocks ?? [],
          coverage: opts.coverage ?? [],
        },
      },
    },
  }
}

describe('composeDeskBrief', () => {
  it('orders items by the composition\'s declared (coverage) order, reporting a unit with no admitted head as missing rather than dropping it', () => {
    const composition = compositionRow({
      id: 'comp-1',
      coverage: [
        { unit: 'unitA', unit_name: 'Unit A', status: 'in_basis' },
        { unit: 'unitB', unit_name: 'Unit B', status: 'in_basis' },
        { unit: 'unitC', unit_name: 'Unit C', status: 'below_floor' },
      ],
      // Blocks stored out of coverage order — the OUTPUT must follow
      // coverage, not block ordinal, order.
      blocks: [block('unitB', 'find-b', 1), block('unitA', 'find-a', 2)],
    })
    const result = composeDeskBrief('country_g20_RR', composition, [], [])
    expect(result.compositionId).toBe('comp-1')
    expect(result.items).toEqual([
      { kind: 'finding', id: 'comp-1', label: 'Ruritania composition' },
      { kind: 'finding', id: 'find-a', label: 'unitA question' },
      { kind: 'finding', id: 'find-b', label: 'unitB question' },
    ])
    expect(result.missingUnits).toEqual([
      { unit: 'unitC', unitName: 'Unit C', status: 'below_floor' },
    ])
  })

  it('falls back to block ordinal order when the row carries no coverage register', () => {
    const composition = compositionRow({
      id: 'comp-2',
      coverage: [],
      blocks: [block('unitB', 'find-b', 2), block('unitA', 'find-a', 1)],
    })
    const result = composeDeskBrief('country_g20_RR', composition, [], [])
    expect(result.items.map((i) => i.id)).toEqual(['comp-2', 'find-a', 'find-b'])
    expect(result.missingUnits).toEqual([])
  })

  it('is a fully empty, honest result for a desk with no composition and no open situations', () => {
    const result = composeDeskBrief('nowhere', null, [], [])
    expect(result).toEqual({
      targetId: 'nowhere',
      compositionId: null,
      items: [],
      missingUnits: [],
      situations: [],
      appendix: null,
    })
  })

  it('carries open situations with their tracked events, in the order given', () => {
    const situationRows = [
      {
        id: 's1',
        name: 'Border unrest',
        status: 'escalating',
        category: 'civil_unrest',
        intensity_score: 0.8,
        event_count: 2,
        last_event_at: null,
      },
    ]
    const events = [[{ id: 'e1', title: 'Clash reported', lifecycle_state: 'active' }]]
    const result = composeDeskBrief('country_g20_RR', null, situationRows, events)
    expect(result.situations).toEqual([
      {
        id: 's1',
        name: 'Border unrest',
        status: 'escalating',
        category: 'civil_unrest',
        intensityScore: 0.8,
        eventCount: 2,
        events: [{ id: 'e1', title: 'Clash reported', lifecycleState: 'active' }],
      },
    ])
    expect(result.appendix).toContain('Border unrest')
    expect(result.appendix).toContain('Clash reported')
  })
})

describe('buildDeskBriefAppendix', () => {
  it('returns null for an empty register — no empty section on a quiet desk', () => {
    expect(buildDeskBriefAppendix([])).toBeNull()
  })

  it('states an absence of tracked events honestly rather than omitting the line', () => {
    const appendix = buildDeskBriefAppendix([
      {
        id: 's1',
        name: 'Quiet frame',
        status: 'active',
        category: 'civil_unrest',
        intensityScore: 0.1,
        eventCount: 0,
        events: [],
      },
    ])
    expect(appendix).toContain('Quiet frame')
    expect(appendix).toContain('none linked yet')
  })
})

describe('buildDeskBrief', () => {
  beforeEach(() => {
    localStorage.clear()
    useExportBasket.getState().clear()
  })

  function fetchers(overrides: Partial<DeskBriefFetchers> = {}): DeskBriefFetchers {
    return {
      fetchComposition: async () =>
        compositionRow({
          id: 'comp-1',
          coverage: [{ unit: 'unitA', unit_name: 'Unit A', status: 'in_basis' }],
          blocks: [block('unitA', 'find-a', 1)],
        }),
      fetchOpenSituations: async () => [],
      fetchSituationEvents: async () => [],
      ...overrides,
    }
  }

  it('fills the basket in reading order (composition, then units) and returns the result', async () => {
    const result = await buildDeskBrief('country_g20_RR', fetchers())
    expect(result.items.map((i) => i.id)).toEqual(['comp-1', 'find-a'])
    expect(result.rejected).toEqual([])
    expect(useExportBasket.getState().items.map((i) => i.id)).toEqual(['comp-1', 'find-a'])
  })

  it('leaves an already-basketed item alone and does not report it as rejected', async () => {
    useExportBasket.getState().add({ kind: 'finding', id: 'comp-1', label: 'already here' })
    const result = await buildDeskBrief('country_g20_RR', fetchers())
    expect(result.rejected).toEqual([])
    expect(useExportBasket.getState().items.map((i) => i.id)).toEqual(['comp-1', 'find-a'])
    expect(useExportBasket.getState().items[0].label).toBe('already here')
  })

  it('reports a genuinely rejected item (a full basket) rather than silently dropping it', async () => {
    for (let i = 0; i < BASKET_MAX_ITEMS; i++) {
      useExportBasket.getState().add({ kind: 'finding', id: `filler-${i}` })
    }
    const result = await buildDeskBrief('country_g20_RR', fetchers())
    expect(result.rejected.map((i) => i.id)).toEqual(['comp-1', 'find-a'])
    expect(useExportBasket.getState().items).toHaveLength(BASKET_MAX_ITEMS)
  })

  it('is empty for a desk with nothing to brief', async () => {
    const result = await buildDeskBrief(
      'nowhere',
      fetchers({ fetchComposition: async () => null }),
    )
    expect(result.items).toEqual([])
    expect(result.appendix).toBeNull()
    expect(useExportBasket.getState().items).toEqual([])
  })
})
