/**
 * recordSheet (U-10) — the ONE command into the Record sheet, its address,
 * Consult's `open_record` panel action, and the on-screen entry.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  openRecord,
  readRecordAddress,
  recordSheetOnScreen,
  sheetRecordFor,
  useRecordSheet,
  writeRecordAddress,
} from '@/lib/recordSheet'
import { applyPanelAction, clearViewUndo, registerFramePort, type FramePort } from '@/lib/panelActions'
import { declaredContract } from '@/panel-registry/contracts'
import { useSelection } from '@/state/selection'

function fakePort(): FramePort & { opened: string[] } {
  const opened: string[] = []
  return {
    opened,
    surface: () => 'board',
    openView: (id) => {
      opened.push(id)
      return true
    },
    closeView: () => true,
    focusView: () => true,
    findOpen: () => null,
    views: () => [{ id: 'v4.morning_read', at: 'stage', front: true }],
    chat: () => ({ placement: 'bottom', collapsed: false }),
    snapshot: () => null,
    restore: () => {},
  }
}

let port: ReturnType<typeof fakePort>
let unregister: () => void

beforeEach(() => {
  window.history.replaceState(null, '', '/#ws=board')
  useSelection.getState().clear()
  useRecordSheet.setState({ ref: null, cite: null, open: false })
  clearViewUndo()
  port = fakePort()
  unregister = registerFramePort(port)
})

afterEach(() => unregister())

describe('openRecord', () => {
  it('sets the point, opens the sheet through the frame and writes the address', () => {
    expect(openRecord({ kind: 'finding', id: 'f 1', label: 'Cranes idle' }, { cite: 3, origin: 'read' })).toBe(true)
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: 'f 1', origin: 'read' })
    expect(useRecordSheet.getState()).toMatchObject({ ref: { kind: 'finding', id: 'f 1' }, cite: 3 })
    expect(port.opened).toEqual(['system.inspector'])
    const p = new URLSearchParams(window.location.hash.slice(1))
    expect(p.get('rec')).toBe('finding:f%201')
    expect(p.get('cite')).toBe('3')
    expect(p.get('ws')).toBe('board')
  })

  it('keeps a coerced kind (a fact) on the sheet while the point carries its nearest kind', () => {
    openRecord({ kind: 'fact', id: 'x1' })
    const sel = useSelection.getState().selection
    expect(sel?.instanceKey).toBe('fact')
    expect(sheetRecordFor(sel, useRecordSheet.getState())).toEqual({ ref: { kind: 'fact', id: 'x1', label: undefined }, cite: null })
  })

  it('follows the point when something else is selected, without the old sentence', () => {
    openRecord({ kind: 'finding', id: 'f1' }, { cite: 2 })
    useSelection.getState().select({ kind: 'signal', id: 's9', origin: 'map' })
    expect(sheetRecordFor(useSelection.getState().selection, useRecordSheet.getState())).toMatchObject({
      ref: { kind: 'signal', id: 's9' },
      cite: null,
    })
  })

  it('refuses an empty ref', () => {
    expect(openRecord({ kind: 'finding', id: '' })).toBe(false)
    expect(port.opened).toEqual([])
  })
})

describe('the address', () => {
  it('round-trips and ignores a kind the chain does not read', () => {
    writeRecordAddress({ kind: 'signal', id: 'a:b' }, 4)
    expect(readRecordAddress()).toEqual({ ref: { kind: 'signal', id: 'a:b' }, cite: 4 })
    expect(readRecordAddress('#rec=entity:e1')).toBeNull()
    expect(readRecordAddress('#rec=finding:f1&cite=0')).toEqual({ ref: { kind: 'finding', id: 'f1' }, cite: null })
    writeRecordAddress(null)
    expect(window.location.hash).toBe('#ws=board')
  })
})

describe("Consult's panel action", () => {
  it('open_record is declared on the Record and runs the one command', () => {
    expect(declaredContract('system.inspector')?.actions.map((a) => a.id)).toContain('open_record')
    const out = applyPanelAction('system.inspector', 'open_record', { ref: { kind: 'finding', id: 'f7' }, cite: 2 }, { origin: 'case' })
    expect(out.state).toBe('executed')
    expect(useRecordSheet.getState()).toMatchObject({ ref: { kind: 'finding', id: 'f7' }, cite: 2 })
    expect(port.opened).toEqual(['system.inspector'])
  })

  it('a kind the contract does not take, or a bad cite, is refused by name', () => {
    expect(applyPanelAction('system.inspector', 'open_record', { ref: { kind: 'entity', id: 'e' } }).state).toBe('refused')
    expect(applyPanelAction('system.inspector', 'open_record', { ref: { kind: 'finding', id: 'f' }, cite: '2' }).state).toBe('refused')
    expect(port.opened).toEqual([])
  })
})

describe('the on-screen entry', () => {
  it('lists the sheet at `sheet` only while it is open', () => {
    openRecord({ kind: 'finding', id: 'f1' })
    const sel = useSelection.getState().selection
    expect(recordSheetOnScreen(sel)).toBeNull()
    useRecordSheet.setState({ open: true })
    expect(recordSheetOnScreen(sel)).toEqual({ id: 'system.inspector:finding:f1', at: 'sheet', front: true })
  })

  it('the on-screen report Consult carries leads with it', async () => {
    const { describeOnScreen } = await import('@/lib/onScreen')
    openRecord({ kind: 'signal', id: 's1' })
    useRecordSheet.setState({ open: true })
    const report = describeOnScreen()!
    expect(report.views[0]).toEqual({ id: 'system.inspector:signal:s1', at: 'sheet', front: true })
    expect(report.views[1]).toEqual({ id: 'v4.morning_read', at: 'stage', front: true })
    vi.restoreAllMocks()
  })
})
