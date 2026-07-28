/**
 * Export collection basket (A10) — the cross-panel "add to export" store.
 *
 * The operator collects findings / analyst reports / journal entries from
 * wherever selection already flows (the Inspector's selected record, a feed
 * row's hover action, a Journal entry card) into ONE persistent basket, then
 * composes the export in the Report Export panel (`system.report_export`),
 * which POSTs the basket to `POST /api/v1/v3/export` for server-side
 * full-fidelity composition (markdown / JSON).
 *
 * Mirrors the consult panel's pin-to-context pattern (#90) — a sticky,
 * operator-curated set fed from the shared selection — but lifted into a
 * global zustand store (consult's pins are panel-local state) and persisted
 * to localStorage so the basket survives a reload mid-collection.
 *
 * The basket is capped at `BASKET_MAX_ITEMS` (mirrors the server's
 * `EXPORT_MAX_ITEMS` — the route answers an honest 413 beyond it); `add()`
 * returns false when the cap or a duplicate rejects the item so the caller's
 * affordance can say so instead of silently no-oping.
 *
 * DOM-free logic (parse/serialize/dedupe/cap) is exported for unit tests.
 */
import { create } from 'zustand'

/** The two exportable kinds the server route accepts. */
export type ExportItemKind = 'finding' | 'journal_entry'

export interface ExportBasketItem {
  kind: ExportItemKind
  id: string
  /** Human label for the basket list (falls back to the id when absent). */
  label?: string
}

/** Client cap — mirrors the server route's EXPORT_MAX_ITEMS (50). */
export const BASKET_MAX_ITEMS = 50

const STORAGE_KEY = 'legba_export_basket_v1'

/** Defensive parse of the persisted basket — malformed/unknown entries are
 *  dropped (never a crash on a stale localStorage shape), the cap enforced. */
export function parseBasket(raw: string | null): ExportBasketItem[] {
  if (!raw) return []
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return []
  }
  if (!Array.isArray(parsed)) return []
  const out: ExportBasketItem[] = []
  const seen = new Set<string>()
  for (const entry of parsed) {
    if (!entry || typeof entry !== 'object') continue
    const e = entry as Record<string, unknown>
    if (e.kind !== 'finding' && e.kind !== 'journal_entry') continue
    if (typeof e.id !== 'string' || !e.id) continue
    const key = `${e.kind}:${e.id}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push({
      kind: e.kind,
      id: e.id,
      label: typeof e.label === 'string' && e.label ? e.label : undefined,
    })
    if (out.length >= BASKET_MAX_ITEMS) break
  }
  return out
}

/** Pure add — dedupe on (kind, id), cap at BASKET_MAX_ITEMS. Returns the SAME
 *  array reference when the item was rejected so callers can detect it. */
export function addToBasket(
  items: ExportBasketItem[],
  item: ExportBasketItem,
): ExportBasketItem[] {
  if (items.some((i) => i.kind === item.kind && i.id === item.id)) return items
  if (items.length >= BASKET_MAX_ITEMS) return items
  return [...items, item]
}

export function removeFromBasket(
  items: ExportBasketItem[],
  kind: ExportItemKind,
  id: string,
): ExportBasketItem[] {
  return items.filter((i) => !(i.kind === kind && i.id === id))
}

function loadInitial(): ExportBasketItem[] {
  try {
    return parseBasket(localStorage.getItem(STORAGE_KEY))
  } catch {
    // localStorage unavailable (SSR / privacy mode) — start empty.
    return []
  }
}

function persist(items: ExportBasketItem[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items))
  } catch {
    // Best-effort persistence — the in-memory basket still works.
  }
}

interface ExportBasketState {
  items: ExportBasketItem[]
  /** Add one item; false when rejected (duplicate or basket full). */
  add: (item: ExportBasketItem) => boolean
  remove: (kind: ExportItemKind, id: string) => void
  clear: () => void
  has: (kind: ExportItemKind, id: string) => boolean
}

export const useExportBasket = create<ExportBasketState>((set, get) => ({
  items: loadInitial(),
  add: (item) => {
    const prev = get().items
    const next = addToBasket(prev, item)
    if (next === prev) return false
    persist(next)
    set({ items: next })
    return true
  },
  remove: (kind, id) => {
    const next = removeFromBasket(get().items, kind, id)
    persist(next)
    set({ items: next })
  },
  clear: () => {
    persist([])
    set({ items: [] })
  },
  has: (kind, id) => get().items.some((i) => i.kind === kind && i.id === id),
}))
