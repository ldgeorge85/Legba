/**
 * Unit tests for the export collection basket (A10) — the DOM-free logic
 * (parse/add/remove/cap) plus the zustand store's add/remove/clear/has
 * contract and its localStorage persistence round-trip.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  BASKET_MAX_ITEMS,
  addToBasket,
  parseBasket,
  removeFromBasket,
  useExportBasket,
  type ExportBasketItem,
} from './exportBasket'

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
