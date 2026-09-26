/**
 * Unit tests for the unified selection store (redesign Move 2).
 *
 * Covers: the single-owner select/clear contract, the breadcrumb history
 * stack + back(), the row_kind→SelectionKind bridge, and the `selectRow`
 * drop-in that replaced the legacy `legba:open-lineage` dispatch.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  useSelection,
  selectionKindOf,
  selectRow,
  onSelectionChange,
  type Selection,
} from './selection'

function reset() {
  useSelection.getState().clear()
}

describe('selection store', () => {
  beforeEach(reset)

  it('select() sets the single current selection', () => {
    useSelection.getState().select({ kind: 'finding', id: 'f1', label: 'Finding 1' })
    const sel = useSelection.getState().selection
    expect(sel).toMatchObject({ kind: 'finding', id: 'f1', label: 'Finding 1' })
  })

  it('clear() empties the selection and history', () => {
    const { select, clear } = useSelection.getState()
    select({ kind: 'finding', id: 'f1' })
    select({ kind: 'signal', id: 's1' })
    clear()
    expect(useSelection.getState().selection).toBeNull()
    expect(useSelection.getState().history).toEqual([])
  })

  it('pushes the prior selection onto the breadcrumb on drill', () => {
    const { select } = useSelection.getState()
    select({ kind: 'finding', id: 'f1', label: 'A' })
    select({ kind: 'signal', id: 's1', label: 'B' })
    const { history, selection } = useSelection.getState()
    expect(selection?.id).toBe('s1')
    expect(history.map((h) => h.id)).toEqual(['f1'])
  })

  it('back() pops to the previous selection', () => {
    const { select, back } = useSelection.getState()
    select({ kind: 'finding', id: 'f1' })
    select({ kind: 'signal', id: 's1' })
    back()
    expect(useSelection.getState().selection?.id).toBe('f1')
    expect(useSelection.getState().history).toEqual([])
  })

  it('re-selecting the same record does not grow history', () => {
    const { select } = useSelection.getState()
    select({ kind: 'finding', id: 'f1' })
    select({ kind: 'finding', id: 'f1', label: 'relabelled' })
    expect(useSelection.getState().history).toEqual([])
  })

  it('caps history at MAX_HISTORY (50)', () => {
    // RAISED 12 → 50 (WORKSTATION_V2_FLOW_DESIGN §3 enabler 3). 12 was a cap on
    // a store with one consumer inside the Inspector; with `Alt+←`/`Alt+→`
    // bound in the shell the trail is NAVIGATION, and a morning's drilling
    // routinely exceeds a dozen hops. 50 is what v2 kept.
    const { select } = useSelection.getState()
    for (let i = 0; i < 20; i++) select({ kind: 'finding', id: `f${i}` })
    expect(useSelection.getState().history.length).toBe(19)
    for (let i = 20; i < 80; i++) select({ kind: 'finding', id: `f${i}` })
    expect(useSelection.getState().history.length).toBe(50)
    // The cap drops the OLDEST hops, never the most recent ones.
    expect(useSelection.getState().history.at(-1)?.id).toBe('f78')
  })

  it('forward re-enters a selection `back()` stepped out of', () => {
    const { select } = useSelection.getState()
    select({ kind: 'finding', id: 'a' })
    select({ kind: 'finding', id: 'b' })
    select({ kind: 'finding', id: 'c' })

    useSelection.getState().back()
    expect(useSelection.getState().selection?.id).toBe('b')
    useSelection.getState().forward()
    expect(useSelection.getState().selection?.id).toBe('c')
  })

  it('a fresh select DROPS the forward stack — a new branch, as a browser does', () => {
    const { select } = useSelection.getState()
    select({ kind: 'finding', id: 'a' })
    select({ kind: 'finding', id: 'b' })
    useSelection.getState().back()
    expect(useSelection.getState().future.map((s) => s.id)).toEqual(['b'])
    select({ kind: 'finding', id: 'c' })
    expect(useSelection.getState().future).toEqual([])
  })

  it('the vocabulary has a word for a report and for a journal entry', () => {
    // Design §0: a report IS a `finding` row, and a journal entry could not be
    // selected at all — so "click a report and everything follows it" was
    // inexpressible rather than merely unimplemented.
    expect(selectionKindOf('report')).toBe('report')
    expect(selectionKindOf('journal_entry')).toBe('journal_entry')
    expect(selectionKindOf('journal')).toBe('journal_entry')
  })

  it('carries the record’s own instant on the preview when the caller has it', () => {
    selectRow('finding', 'f1', 'F1', { origin: 'timeline', preview: { ts: 1_700_000 } })
    expect(useSelection.getState().selection?.preview?.ts).toBe(1_700_000)
  })

  it('onSelectionChange fires on every change', () => {
    const seen: Array<Selection | null> = []
    const unsub = onSelectionChange((s) => seen.push(s))
    useSelection.getState().select({ kind: 'finding', id: 'f1' })
    useSelection.getState().clear()
    unsub()
    expect(seen.length).toBeGreaterThanOrEqual(2)
    expect(seen.at(-1)).toBeNull()
  })
})

describe('selectionKindOf', () => {
  it('passes through first-class kinds', () => {
    expect(selectionKindOf('finding')).toBe('finding')
    expect(selectionKindOf('situation')).toBe('situation')
    expect(selectionKindOf('signal')).toBe('signal')
    expect(selectionKindOf('entity')).toBe('entity')
    expect(selectionKindOf('source')).toBe('source')
    expect(selectionKindOf('target')).toBe('target')
    expect(selectionKindOf('analyst')).toBe('analyst')
  })

  it('coerces walkable-but-not-first-class kinds to finding', () => {
    expect(selectionKindOf('hypothesis')).toBe('finding')
    expect(selectionKindOf('prediction')).toBe('finding')
    expect(selectionKindOf('critique')).toBe('finding')
    expect(selectionKindOf('meta_finding')).toBe('finding')
  })

  it('falls back to finding for unknown kinds (never throws)', () => {
    expect(selectionKindOf('trace')).toBe('finding')
    expect(selectionKindOf('whatever')).toBe('finding')
  })
})

describe('selectRow (legacy legba:open-lineage replacement)', () => {
  beforeEach(reset)

  it('drives the store from a row_kind/row_id/title triple', () => {
    selectRow('finding', 'f1', 'Port closure')
    expect(useSelection.getState().selection).toMatchObject({
      kind: 'finding',
      id: 'f1',
      label: 'Port closure',
    })
  })

  it('stashes the true substrate kind on instanceKey when coerced', () => {
    selectRow('hypothesis', 'h1', 'a hypothesis')
    const sel = useSelection.getState().selection
    expect(sel?.kind).toBe('finding')
    expect(sel?.instanceKey).toBe('hypothesis')
  })

  it('does not set instanceKey for a non-coerced kind', () => {
    selectRow('finding', 'f1')
    expect(useSelection.getState().selection?.instanceKey).toBeUndefined()
  })

  it('tags origin for breadcrumb provenance', () => {
    selectRow('signal', 's1', undefined, { origin: 'feed' })
    expect(useSelection.getState().selection?.origin).toBe('feed')
  })
})
