/**
 * THE ONE EXECUTOR (UI-B1/shell; UI-D1 §3, D5). `caseRunner.test.ts` proves the
 * CASE driver action by action (C-1b's contract, unchanged); this file proves
 * the OPERATOR driver and the split between the two:
 *
 *  - each action reaches exactly the right shell call or store;
 *  - the drive pack's kind refusal stays CLIENT-SIDE for a case
 *    (`kind_not_drivable`) and does not bind the operator;
 *  - `drive()` runs through the registered shell as the operator;
 *  - a drill's params reach the tile through `state/panelParams`, before the
 *    shell is asked (the first-open guarantee the window parks gave).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, applyAction, drive, isWorkstationAction, registerShell, type ShellPort } from './applyAction'
import { CASE_VOCABULARY } from './caseModel'
import { resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import { pendingPanelParams, resetPanelParams } from '@/state/panelParams'

function fakeShell(mode = 'personal') {
  return {
    mode,
    openSingleton: vi.fn(() => ({})),
    openBound: vi.fn(() => true),
    switchStance: vi.fn(),
    applyPreset: vi.fn(),
    runMission: vi.fn(),
    reopenConsult: vi.fn(),
    currentStance: vi.fn(() => 'morning_read'),
    serializeLayout: vi.fn(() => null),
    restoreLayout: vi.fn(),
  } satisfies ShellPort
}

const OP = { driver: 'operator' as const }

beforeEach(() => {
  resetScope()
  resetPanelParams()
  useSelection.getState().clear()
  useWorldState.setState({ windowStartMs: 1_000, windowEndMs: 2_000, spanMs: 1_000 })
})

afterEach(() => vi.restoreAllMocks())

describe('open_panel', () => {
  it('a case may only drive the allowlist; the operator may open any registered kind', () => {
    const sh = fakeShell()
    expect(CASE_VOCABULARY.drivableKinds).not.toContain('system.journal_gate')
    expect(applyAction(act('open_panel', { kind: 'system.journal_gate', params: {} }), sh)).toEqual({
      state: 'refused',
      reason: 'kind_not_drivable: system.journal_gate',
    })
    expect(sh.openSingleton).not.toHaveBeenCalled()
    expect(applyAction(act('open_panel', { kind: 'system.journal_gate', params: {} }), sh, OP)).toEqual({
      state: 'executed',
    })
    expect(sh.openSingleton).toHaveBeenCalledWith('system.journal_gate', undefined)
  })

  it('an unknown kind is skipped by name for the operator, never thrown', () => {
    expect(applyAction(act('open_panel', { kind: 'nope.kind', params: {} }), fakeShell(), OP)).toEqual({
      state: 'skipped',
      reason: 'unknown kind: nope.kind',
    })
  })

  it('a bound kind needs its subject, of the right kind, and opens through the bound opener', () => {
    const sh = fakeShell()
    expect(applyAction(act('open_panel', { kind: 'target.claims', params: {} }), sh, OP).state).toBe('refused')
    expect(
      applyAction(
        act('open_panel', { kind: 'analyst.runs', params: { subject: { kind: 'target', id: 't1' } } }),
        sh,
        OP,
      ).state,
    ).toBe('refused')
    applyAction(act('open_panel', { kind: 'analyst.runs', params: { subject: { kind: 'analyst', id: 'a1' } } }), sh, OP)
    expect(sh.openBound).toHaveBeenCalledWith('analyst.runs', 'a1', 'analyst')
  })

  it("an operator's tab and position pass through; a case's position never does", () => {
    const sh = fakeShell()
    const pos = { referencePanel: 'system.findings', direction: 'right' }
    applyAction(act('open_panel', { kind: 'system.entities', params: { tab: 'graph', position: pos } }), sh, OP)
    expect(sh.openSingleton).toHaveBeenLastCalledWith('system.entities', 'graph', pos)
    applyAction(act('open_panel', { kind: 'system.timeline', params: { tab: 'validity', position: pos } }), sh)
    expect(sh.openSingleton).toHaveBeenLastCalledWith('system.timeline', 'validity')
  })

  it("a drill's params land in panelParams BEFORE the shell is asked (first open with no shell)", () => {
    const out = applyAction(
      act('open_panel', { kind: 'system.optimizer.diff', params: { candidate_id: 'c-1' } }),
      null,
      OP,
    )
    expect(out).toEqual({ state: 'skipped', reason: 'no workstation shell to drive' })
    // The diff retired onto the Optimizer's Diff tab (UI-B1/merges): the
    // params land under the SURVIVOR, whose wrapper relays them to the diff.
    expect(pendingPanelParams('system.optimizer')?.params).toEqual({ candidate_id: 'c-1' })
  })

  it('mode gating holds for both drivers', () => {
    const sh = fakeShell('cis')
    const out = applyAction(act('open_panel', { kind: 'system.journal_gate', params: {} }), sh, OP)
    expect(out).toMatchObject({ state: 'skipped', reason: expect.stringMatching(/^mode-gated/) })
  })
})

describe('arrange / run_mission', () => {
  it('the operator may arrange any stance or preset; no Consult re-open (that is a case affordance)', () => {
    const sh = fakeShell()
    applyAction(act('arrange', { layout: 'engine', layout_type: 'stance' }), sh, OP)
    expect(sh.switchStance).toHaveBeenCalledWith('engine')
    applyAction(act('arrange', { layout: 'operations', layout_type: 'preset' }), sh, OP)
    expect(sh.applyPreset).toHaveBeenCalledWith('operations')
    expect(sh.reopenConsult).not.toHaveBeenCalled()
    expect(applyAction(act('arrange', { layout: 'nowhere' }), sh, OP).state).toBe('refused')
  })

  it('a case may not arrange outside its vocabulary (engine) and re-opens Consult when it may', () => {
    const sh = fakeShell()
    expect(applyAction(act('arrange', { layout: 'engine', layout_type: 'stance' }), sh).state).toBe('refused')
    applyAction(act('arrange', { layout: 'desk', layout_type: 'stance' }), sh)
    expect(sh.reopenConsult).toHaveBeenCalledTimes(1)
  })

  it('the operator may run any mission; a case only the vocabulary missions', () => {
    const sh = fakeShell()
    expect(applyAction(act('run_mission', { name: 'release' }), sh).state).toBe('refused')
    expect(applyAction(act('run_mission', { name: 'release' }), sh, OP).state).toBe('executed')
    expect(sh.runMission).toHaveBeenCalledWith('release')
  })
})

describe('set_scope / focus / timeline', () => {
  it('stamp the origin the producer names', () => {
    applyAction(act('set_scope', { kind: 'target', id: 'country_g20_br', label: 'Brazil' }), null, {
      ...OP,
      origin: 'share-link',
    })
    expect(useScope.getState().scope).toMatchObject({ id: 'country_g20_br', label: 'Brazil', origin: 'share-link' })
    applyAction(act('focus', { ref: { kind: 'finding', id: 'f1' }, label: 'F' }), null, { ...OP, origin: 'rail' })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: 'f1', label: 'F', origin: 'rail' })
  })

  it("an operator's SPAN re-anchors the scrubber's outer span with the window (the mission write)", () => {
    applyAction(
      act('timeline', { start: '2026-10-01T00:00:00Z', end: '2026-10-08T00:00:00Z', span: '7d' }),
      null,
      OP,
    )
    const w = useWorldState.getState()
    expect(w.spanMs).toBe(7 * 86_400_000)
    expect(w.windowEndMs).toBe(Date.parse('2026-10-08T00:00:00Z'))
  })

  it("a case's timeline writes the absolute window only", () => {
    applyAction(act('timeline', { start: '2026-10-01T00:00:00Z', end: '2026-10-08T00:00:00Z', span: '7d' }), null)
    const w = useWorldState.getState()
    expect(w.spanMs).toBe(1_000)
    expect(w.windowStartMs).toBe(Date.parse('2026-10-01T00:00:00Z'))
  })
})

describe('drive() — the operator entry point', () => {
  it('runs a list, in order, through the registered shell as the operator', () => {
    const sh = fakeShell()
    const off = registerShell(sh)
    try {
      const out = drive([
        act('focus', { ref: { kind: 'target', id: 't1' } }),
        act('open_panel', { kind: 'system.journal_gate', params: {} }),
      ])
      expect(out).toEqual([{ state: 'executed' }, { state: 'executed' }])
      expect(sh.openSingleton).toHaveBeenCalledWith('system.journal_gate', undefined)
      expect(useSelection.getState().selection?.origin).toBe('operator')
    } finally {
      off()
    }
  })

  it('isWorkstationAction accepts the nine names only', () => {
    expect(isWorkstationAction(act('done', { summary: null }))).toBe(true)
    expect(isWorkstationAction({ action: 'write_object', args: {} })).toBe(false)
    expect(isWorkstationAction({ action: 'focus' })).toBe(false)
  })
})
