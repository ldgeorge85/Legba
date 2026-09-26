/**
 * useUrlState — the Selection Store, expressed as the URL.
 *
 * The v2 Crossroads kept the current selection in a Zustand store that every
 * panel subscribed to. On a phone the same job is better done by the address
 * bar: it survives a reload and a browser restore, it makes the Back button
 * mean "un-select" for free (which is the gesture a phone user will reach for
 * whatever we do), and it makes a report shareable by copying the URL.
 *
 * `?report=<id>` is the context for every section on the page; `?entity=<id>`
 * narrows all of them at once. One selection, everything follows.
 */

import { useCallback, useSyncExternalStore } from 'react'

const listeners = new Set<() => void>()

function emit(): void {
  for (const l of listeners) l()
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  window.addEventListener('popstate', cb)
  return () => {
    listeners.delete(cb)
    window.removeEventListener('popstate', cb)
  }
}

function snapshot(): string {
  return typeof window === 'undefined' ? '' : window.location.search
}

export interface MobileSelection {
  report: string | null
  entity: string | null
  day: string | null
  /** The journal consolidation, which is a journal row rather than a read. */
  journal: string | null
}

function parse(search: string): MobileSelection {
  const p = new URLSearchParams(search)
  return {
    report: p.get('report'),
    entity: p.get('entity'),
    day: p.get('day'),
    journal: p.get('journal'),
  }
}

/**
 * Write the selection into the URL.
 *
 * A report change PUSHES (so Back returns to the navigator — the phone's
 * expected "up"); an entity or day change REPLACES, because narrowing inside a
 * report is a filter, not a place, and stacking twelve filter states into the
 * history would make Back useless exactly when the operator needs it.
 */
export function setSelection(next: Partial<MobileSelection>, mode: 'push' | 'replace'): void {
  const cur = parse(window.location.search)
  const merged = { ...cur, ...next }
  const p = new URLSearchParams()
  if (merged.report) p.set('report', merged.report)
  if (merged.entity) p.set('entity', merged.entity)
  if (merged.day) p.set('day', merged.day)
  if (merged.journal) p.set('journal', merged.journal)
  const qs = p.toString()
  const url = `${window.location.pathname}${qs ? `?${qs}` : ''}`
  if (mode === 'push') window.history.pushState(null, '', url)
  else window.history.replaceState(null, '', url)
  emit()
}

export function useSelection(): MobileSelection {
  const search = useSyncExternalStore(subscribe, snapshot, () => '')
  return parse(search)
}

/**
 * Opening a record starts you at its top.
 *
 * Guarded because `window.scrollTo` is unimplemented in jsdom and a navigation
 * must never fail on a cosmetic scroll — the same reasoning that makes
 * `readFormat.scrollToAnchor` defensive.
 */
function scrollToTop(): void {
  try {
    window.scrollTo(0, 0)
  } catch {
    /* the navigation still happened; only the scroll was refused */
  }
}

/** The actions the surface needs, stable across renders. */
export function useSelectionActions(): {
  openReport: (id: string) => void
  openJournal: (id: string) => void
  closeReport: () => void
  selectEntity: (id: string | null) => void
  setDay: (day: string | null) => void
} {
  const openReport = useCallback((id: string) => {
    // Opening a report always clears any entity filter carried in from the
    // previous one — an entity id from report A means nothing inside report B.
    setSelection({ report: id, entity: null, journal: null }, 'push')
    scrollToTop()
  }, [])
  const openJournal = useCallback((id: string) => {
    setSelection({ journal: id, report: null, entity: null }, 'push')
    scrollToTop()
  }, [])
  const closeReport = useCallback(() => {
    setSelection({ report: null, entity: null, journal: null }, 'push')
  }, [])
  const selectEntity = useCallback((id: string | null) => {
    setSelection({ entity: id }, 'replace')
  }, [])
  const setDay = useCallback((day: string | null) => {
    setSelection({ day, report: null, entity: null, journal: null }, 'replace')
  }, [])
  return { openReport, openJournal, closeReport, selectEntity, setDay }
}
