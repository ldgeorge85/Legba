/**
 * readLayout — the Morning Read's ordering flip, persisted.
 *
 * Operator ruling 3 (`DEMOTION_D1_SPEC_2026-09-04.md` §6, D-4): **Layout B is the
 * default** — evidence map → verified record → Assessment last — and "the flip is
 * cheap and must stay cheap (one attribute)".
 *
 * So the flip is exactly one attribute. `data-read-layout` on the page root, CSS
 * `order` on the four bands, no re-render of the bands themselves and no second
 * component tree. That is also why this is a module-level external store rather
 * than component state: the value survives a Dockview remount (which tab
 * switching performs freely), and a panel that remembers the operator's ordering
 * across a reload is the difference between an affordance and a toy.
 *
 * The `debugMode.ts` idiom — a `Set` of listeners plus `useSyncExternalStore` —
 * is used deliberately over zustand: one boolean-ish value with no actions does
 * not earn a store, and `useSyncExternalStore` is the React-18-correct way to
 * subscribe to something outside React that a concurrent render must not tear on.
 *
 * localStorage access is wrapped: private mode / storage-disabled degrades to an
 * in-memory preference for the session rather than throwing inside a render.
 */

import { useSyncExternalStore } from 'react'

export type ReadLayout = 'A' | 'B'

/** Ruling 3. B is what the operator reads unless they say otherwise. */
export const DEFAULT_READ_LAYOUT: ReadLayout = 'B'

const KEY = 'legba_read_layout'

const listeners = new Set<() => void>()
let current: ReadLayout | null = null

function isLayout(v: unknown): v is ReadLayout {
  return v === 'A' || v === 'B'
}

export function getReadLayout(): ReadLayout {
  if (current !== null) return current
  try {
    const raw = localStorage.getItem(KEY)
    current = isLayout(raw) ? raw : DEFAULT_READ_LAYOUT
  } catch {
    current = DEFAULT_READ_LAYOUT
  }
  return current
}

export function setReadLayout(next: ReadLayout): void {
  if (!isLayout(next) || next === getReadLayout()) return
  current = next
  try {
    localStorage.setItem(KEY, next)
  } catch {
    // In-memory only for this session — a preference is not worth an exception.
  }
  for (const fn of listeners) fn()
}

export function toggleReadLayout(): ReadLayout {
  const next: ReadLayout = getReadLayout() === 'B' ? 'A' : 'B'
  setReadLayout(next)
  return next
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/** Test seam — forget the cached value and the persisted one. */
export function __resetReadLayout(): void {
  current = null
  try {
    localStorage.removeItem(KEY)
  } catch {
    /* nothing persisted to forget */
  }
  for (const fn of listeners) fn()
}

/**
 * The two orderings, named for what they put first. The labels are the control's
 * own copy — the flip has to say what it does, because "A / B" says nothing.
 */
export const READ_LAYOUT_LABEL: Record<ReadLayout, string> = {
  B: 'Evidence first',
  A: 'Assessment first',
}

export const READ_LAYOUT_DESCRIPTION: Record<ReadLayout, string> = {
  B: 'evidence map, then the quoted record, then the Assessment last',
  A: 'the Assessment first, then the quoted record, then the evidence map',
}

export { subscribe as subscribeReadLayout }

/** The React binding. `getReadLayout` is the server snapshot too — SSR would
 *  read the default, which is the honest value before storage exists. */
export function useReadLayout(): ReadLayout {
  return useSyncExternalStore(subscribe, getReadLayout, () => DEFAULT_READ_LAYOUT)
}
