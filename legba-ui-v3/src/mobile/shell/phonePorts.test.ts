/**
 * M-2 — the phone's frame port and shell port: panel actions, cross-links and
 * Consult reach the phone through the same router; the on-screen report reads
 * the phone's screen; what only the desktop does is refused by name.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { applyPanelAction, getFramePort, undoView, clearViewUndo } from '@/lib/panelActions'
import { applyAction, getShell, act } from '@/lib/applyAction'
import { describeOnScreen } from '@/lib/onScreen'
import { restoreSnapshot, takeSnapshot } from '@/lib/caseRunner'
import { openRecord, useRecordSheet } from '@/lib/recordSheet'
import { useSelection } from '@/state/selection'
import { registerPhoneFramePort, viewsOfScreen } from './phoneFramePort'
import { NOT_ON_THIS_DEVICE, registerPhoneShellPort } from './phoneShellPort'
import { usePhoneShell } from './phoneState'
import { phoneScreenOf } from './phoneAddress'

const here = () => phoneScreenOf(window.location.hash)

describe('the phone ports', () => {
  let off: Array<() => void> = []
  beforeEach(() => {
    window.history.replaceState(null, '', '/m/')
    usePhoneShell.setState({ watchSegment: 'alerts', lastScreen: null, asking: null })
    useRecordSheet.setState({ ref: null, cite: null, open: false })
    useSelection.getState().clear()
    clearViewUndo()
    off = [registerPhoneFramePort(), registerPhoneShellPort()]
  })
  afterEach(() => {
    for (const f of off) f()
  })

  it('registers both ports', () => {
    expect(getFramePort()?.chat()).toEqual({ placement: 'tab', collapsed: true })
    // M-3g: the phone's arrangement is its screen (the frame port's snapshot)
    expect(getShell()?.serializeLayout()).toMatchObject({ kind: 'phone-screen/1' })
  })

  it("a case's one Undo (the snapshot before step 1) puts the phone back on its screen", () => {
    window.history.replaceState(null, '', '/m/#screen=consult')
    const snap = takeSnapshot(getShell())
    expect(applyPanelAction('subject.page:country_x', 'open').state).toBe('executed')
    expect(applyPanelAction('v4.map', 'open').state).toBe('executed')
    expect(here().page).toEqual({ kind: 'map' })
    restoreSnapshot(snap, getShell())
    expect(here()).toMatchObject({ tab: 'consult', page: null })
  })

  it('a desktop arrangement handed to the phone is refused by name', () => {
    expect(() => getShell()!.restoreLayout('board', { dockview: {} })).toThrow(NOT_ON_THIS_DEVICE)
  })

  it('a Watch view opens the Watch tab on its segment; undo puts the screen back', () => {
    expect(applyPanelAction('view:watch_silences', 'open').state).toBe('executed')
    expect(here().tab).toBe('watch')
    expect(usePhoneShell.getState().watchSegment).toBe('silences')
    expect(undoView()).not.toBeNull()
    expect(here().tab).toBe('changed')
  })

  it('a bound subject page is pushed; the map is pushed; the proof page is pushed', () => {
    expect(applyPanelAction('subject.page:country_x', 'open').state).toBe('executed')
    expect(here().page).toEqual({ kind: 'subject', id: 'country_x' })
    expect(applyPanelAction('v4.map', 'open').state).toBe('executed')
    expect(here().page).toEqual({ kind: 'map' })
    expect(applyPanelAction('view:proof', 'open').state).toBe('executed')
    expect(here()).toMatchObject({ tab: 'ledger', page: { kind: 'proof' } })
  })

  it('opening a record shows its peek, and the on-screen report lists it at sheet', () => {
    openRecord({ kind: 'finding', id: 'ex_1' }, { origin: 'test' })
    const s = here()
    expect(s.record?.ref).toMatchObject({ kind: 'finding', id: 'ex_1' })
    expect(s.peek).toBe(true)
    const views = viewsOfScreen(s, 'alerts')
    expect(views.find((v) => v.id.startsWith('system.inspector'))?.at).toBe('sheet')
  })

  it('the on-screen report describes the phone screen; on Consult, the screen the person came from', () => {
    applyPanelAction('view:watch_alerts', 'open')
    let report = describeOnScreen()
    expect(report?.views.map((v) => v.id)).toContain('view:watch_alerts')
    expect(report?.views[0].at).toBe('screen')
    usePhoneShell.setState({ lastScreen: here() })
    window.history.replaceState(null, '', '/m/#screen=consult')
    report = describeOnScreen()
    expect(report?.chat).toEqual({ placement: 'tab', collapsed: false })
    expect(report?.views.map((v) => v.id)).toContain('view:watch_alerts')
  })

  it('a view the phone does not show is refused by the router, not dropped', () => {
    const r = applyPanelAction('system.flow', 'open')
    expect(r.state).not.toBe('executed')
  })

  it('arrange to a reader surface switches the tab; an operator surface, a preset, a mission are refused by name', () => {
    expect(applyAction(act('arrange', { layout: 'ledger', layout_type: 'stance' }), getShell(), { driver: 'operator' }).state).toBe('executed')
    expect(here().tab).toBe('ledger')
    const op = applyAction(act('arrange', { layout: 'engine', layout_type: 'stance' }), getShell(), { driver: 'operator' })
    expect(op.state).toBe('skipped')
    expect(op.state === 'skipped' && op.reason).toContain(NOT_ON_THIS_DEVICE)
  })
})
