/**
 * C-1b/ui — the executor. Each of the nine actions calls the right shell API
 * (spies on the shell port) or writes exactly its own store, and nothing else;
 * a non-allowlisted kind is refused client-side too.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { applyAction, registerCaseShell, getCaseShell, scopeForCase, type CaseShell } from './caseRunner'
import { parseCaseAction, type CaseAction } from './caseModel'
import { resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import { pendingPanelParams, resetPanelParams } from '@/state/panelParams'

function fakeShell(mode = 'personal'): CaseShell & Record<string, ReturnType<typeof vi.fn>> {
  return {
    mode,
    openSingleton: vi.fn(() => ({})),
    openBound: vi.fn(() => true),
    switchStance: vi.fn(),
    applyPreset: vi.fn(),
    runMission: vi.fn(),
    reopenConsult: vi.fn(),
    currentStance: vi.fn(() => 'morning_read'),
    serializeLayout: vi.fn(() => ({ grid: 'before' })),
    restoreLayout: vi.fn(),
  } as unknown as CaseShell & Record<string, ReturnType<typeof vi.fn>>
}

function action(name: string, args: Record<string, unknown>, seq = 1): CaseAction {
  const a = parseCaseAction({
    seq,
    action: name,
    args,
    at: '2026-10-03T12:00:00Z',
    cites: [{ kind: 'target', id: 't1' }],
    rests_on: [{ round: 1, tool: 'list_targets' }],
  })
  if (!a) throw new Error(`bad action ${name}`)
  return a
}

const effects = () => ({ resetCamera: vi.fn(), setTimelineAt: vi.fn() })

const WORLD0 = {
  windowStartMs: 1_000,
  windowEndMs: 2_000,
  spanMs: 1_000,
  layers: { signals: true, findings: true, situations: true, entities: false, events: true },
}

beforeEach(() => {
  resetScope()
  useSelection.getState().clear()
  useWorldState.setState({ ...WORLD0, layers: { ...WORLD0.layers } })
})

afterEach(() => {
  vi.restoreAllMocks()
})

/** The three stores a case may write, as one comparable value. */
function stores() {
  const w = useWorldState.getState()
  return {
    scope: useScope.getState().scope,
    selection: useSelection.getState().selection,
    window: [w.windowStartMs, w.windowEndMs],
    layers: { ...w.layers },
  }
}

describe('open_panel', () => {
  it('opens an allowlisted singleton through the singleton opener, and nothing else', () => {
    const sh = fakeShell()
    const before = stores()
    expect(applyAction(action('open_panel', { kind: 'system.findings', params: {} }), sh)).toEqual({ state: 'executed' })
    expect(sh.openSingleton).toHaveBeenCalledWith('system.findings', undefined)
    expect(sh.openBound).not.toHaveBeenCalled()
    expect(stores()).toEqual(before)
  })

  it('REFUSES a kind outside the allowlist without touching the shell (defence in depth)', () => {
    const sh = fakeShell()
    for (const kind of ['registry.stack', 'system.settings', 'source.subscription_builder', 'system.consult']) {
      const out = applyAction(action('open_panel', { kind, params: {} }), sh)
      expect(out).toEqual({ state: 'refused', reason: `kind_not_drivable: ${kind}` })
    }
    expect(sh.openSingleton).not.toHaveBeenCalled()
  })

  it('a bound kind opens on its subject through the bound opener', () => {
    const sh = fakeShell()
    applyAction(
      action('open_panel', { kind: 'subject.page', params: { subject: { kind: 'target', id: 't1' } } }),
      sh,
    )
    expect(sh.openBound).toHaveBeenCalledWith('subject.page', 't1', 'target')
    // UI-B3 — a case names the subject AND the tab; a retired id lands on its own tab.
    applyAction(
      action('open_panel', { kind: 'subject.page', params: { subject: { kind: 'target', id: 't1' }, tab: 'claims' } }),
      sh,
    )
    expect(sh.openBound).toHaveBeenLastCalledWith('subject.page', 't1', 'target', 'claims')
    applyAction(
      action('open_panel', { kind: 'target.findings', params: { subject: { kind: 'target', id: 't1' } } }),
      sh,
    )
    expect(sh.openBound).toHaveBeenLastCalledWith('subject.page', 't1', 'target', 'evidence')
    // A tab the vocabulary does not name for the kind is not honoured for a case.
    applyAction(
      action('open_panel', { kind: 'subject.page', params: { subject: { kind: 'target', id: 't1' }, tab: 'lineage' } }),
      sh,
    )
    expect(sh.openBound).toHaveBeenLastCalledWith('subject.page', 't1', 'target')
    applyAction(
      action('open_panel', { kind: 'analyst.runs', params: { subject: { kind: 'analyst', id: 'a1' } } }),
      sh,
    )
    expect(sh.openBound).toHaveBeenCalledWith('analyst.runs', 'a1', 'analyst')
    // No subject, or the wrong kind of subject: refused, never guessed.
    expect(applyAction(action('open_panel', { kind: 'target.overview', params: {} }), sh).state).toBe('refused')
    expect(
      applyAction(
        action('open_panel', { kind: 'target.overview', params: { subject: { kind: 'entity', id: 'x' } } }),
        sh,
      ).state,
    ).toBe('refused')
  })

  it('a tab is honoured only when the vocabulary names it for that kind', () => {
    const sh = fakeShell()
    applyAction(action('open_panel', { kind: 'system.timeline', params: { tab: 'validity' } }), sh)
    expect(sh.openSingleton).toHaveBeenLastCalledWith('system.timeline', 'validity')
    applyAction(action('open_panel', { kind: 'system.findings', params: { tab: 'validity' } }), sh)
    expect(sh.openSingleton).toHaveBeenLastCalledWith('system.findings', undefined)
  })

  it('a retired kind resolves through aliases.ts onto its survivor and tab', () => {
    const sh = fakeShell()
    expect(applyAction(action('open_panel', { kind: 'v4.timeline', params: {} }), sh)).toEqual({ state: 'executed' })
    expect(sh.openSingleton).toHaveBeenCalledWith('system.timeline', 'events')
  })

  it('a kind gated out of the active mode renders skipped, with the reason', () => {
    const sh = fakeShell('above_ai')
    const out = applyAction(action('open_panel', { kind: 'system.findings', params: {} }), sh)
    expect(out.state).toBe('skipped')
    expect(out).toMatchObject({ reason: expect.stringMatching(/^mode-gated/) })
    expect(sh.openSingleton).not.toHaveBeenCalled()
  })

  it('cross-framing opens through the singleton opener, its claim in the panel params (no park, no event)', () => {
    resetPanelParams()
    const sh = fakeShell()
    expect(
      applyAction(
        action('open_panel', { kind: 'analysis.cross_framing', params: { subject: { kind: 'target', id: 't9' } } }),
        sh,
      ),
    ).toEqual({ state: 'executed' })
    expect(sh.openSingleton).toHaveBeenCalledWith('analysis.cross_framing', undefined)
    expect(pendingPanelParams('analysis.cross_framing')?.params).toEqual({
      subject: { kind: 'target', id: 't9' },
      origin: 'case',
    })
  })

  it('a case may not open cross-framing without its desk; the operator may (it reads the wall scope)', () => {
    const sh = fakeShell()
    expect(applyAction(action('open_panel', { kind: 'analysis.cross_framing', params: {} }), sh).state).toBe('refused')
    expect(
      applyAction(action('open_panel', { kind: 'analysis.cross_framing', params: {} }), sh, { driver: 'operator' }).state,
    ).toBe('executed')
  })
})

describe('arrange', () => {
  it('a PRESET seeds through the preset opener and re-opens the presenting Consult tile', () => {
    const sh = fakeShell()
    applyAction(action('arrange', { layout: 'workspace', layout_type: 'preset' }), sh)
    expect(sh.applyPreset).toHaveBeenCalledWith('workspace')
    expect(sh.switchStance).not.toHaveBeenCalled()
    expect(sh.reopenConsult).toHaveBeenCalledTimes(1)
  })

  it('a STANCE goes through the ordinary switcher (and keeps the Consult tile)', () => {
    const sh = fakeShell()
    applyAction(action('arrange', { layout: 'investigate', layout_type: 'stance' }), sh)
    expect(sh.switchStance).toHaveBeenCalledWith('investigate')
    expect(sh.reopenConsult).toHaveBeenCalledTimes(1)
  })

  it('a layout the vocabulary does not offer is refused (trust/engine/gate)', () => {
    const sh = fakeShell()
    expect(applyAction(action('arrange', { layout: 'engine', layout_type: 'stance' }), sh).state).toBe('refused')
    expect(sh.switchStance).not.toHaveBeenCalled()
  })
})

describe('set_scope', () => {
  it('writes exactly the scope store, with members from the SHELL projector — never the action', () => {
    const before = stores()
    applyAction(
      action('set_scope', {
        kind: 'target',
        id: 't1',
        // A model-supplied member list must be ignored.
        members: { targetIds: ['t1', 't2', 't3'] },
      }),
      fakeShell(),
    )
    const scope = useScope.getState().scope!
    expect(scope).toEqual(scopeForCase('target', 't1'))
    expect(scope.members.targetIds).toEqual(['t1'])
    expect({ ...stores(), scope: before.scope }).toEqual(before)
  })

  it('a situation scope is degraded (empty members), the share-link contract', () => {
    applyAction(action('set_scope', { kind: 'situation', id: 'sit-1' }), fakeShell())
    expect(useScope.getState().scope?.members.targetIds).toEqual([])
    expect(useScope.getState().scope?.origin).toBe('case')
  })

  it('none clears the scope', () => {
    useScope.getState().setScope(scopeForCase('target', 't1'))
    applyAction(action('set_scope', { kind: 'none', id: null }), fakeShell())
    expect(useScope.getState().scope).toBeNull()
  })
})

describe('focus / map / timeline', () => {
  it('focus writes exactly the selection store', () => {
    const before = stores()
    applyAction(action('focus', { ref: { kind: 'finding', id: 'f1' } }), fakeShell())
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: 'f1', origin: 'case' })
    expect({ ...stores(), selection: before.selection }).toEqual(before)
  })

  it('map sets the layers WHOLESALE and fits through the selection-driven camera', () => {
    const fx = effects()
    applyAction(
      action('map', { view: 'fit_ref', ref: { kind: 'situation', id: 's1' }, layers: ['situations', 'events'] }),
      fakeShell(),
      { effects: fx },
    )
    expect(useWorldState.getState().layers).toEqual({
      signals: false,
      findings: false,
      situations: true,
      entities: false,
      events: true,
    })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'situation', id: 's1' })
    expect(fx.resetCamera).not.toHaveBeenCalled()
  })

  it('map reset moves only the camera; no layers named is no layer claim', () => {
    const fx = effects()
    const before = stores()
    applyAction(action('map', { view: 'reset', ref: null, layers: null }), fakeShell(), { effects: fx })
    expect(fx.resetCamera).toHaveBeenCalledTimes(1)
    expect(stores()).toEqual(before)
  })

  it('map with no map surface in the active mode is refused by name', () => {
    const out = applyAction(action('map', { view: 'reset', ref: null, layers: null }), fakeShell('above_ai'))
    expect(out).toEqual({ state: 'skipped', reason: 'no_map_surface' })
  })

  it('timeline writes the one shared window (map AND scrubber), absolute, and the at', () => {
    const fx = effects()
    const before = stores()
    applyAction(
      action('timeline', {
        start: '2026-09-26T12:00:00Z',
        end: '2026-10-03T12:00:00Z',
        span: '7d',
        at: '2026-10-01T00:00:00Z',
      }),
      fakeShell(),
      { effects: fx },
    )
    expect(useWorldState.getState().windowStartMs).toBe(Date.parse('2026-09-26T12:00:00Z'))
    expect(useWorldState.getState().windowEndMs).toBe(Date.parse('2026-10-03T12:00:00Z'))
    expect(fx.setTimelineAt).toHaveBeenCalledWith(Date.parse('2026-10-01T00:00:00Z'))
    expect({ ...stores(), window: before.window }).toEqual(before)
  })
})

describe('run_mission / narrate / done', () => {
  it('a mission runs through the shell chooser and keeps the Consult tile', () => {
    const sh = fakeShell()
    applyAction(action('run_mission', { name: 'crisis' }), sh)
    expect(sh.runMission).toHaveBeenCalledWith('crisis')
    expect(sh.reopenConsult).toHaveBeenCalled()
    expect(applyAction(action('run_mission', { name: 'release' }), sh).state).toBe('refused')
  })

  it('narrate and done write nothing', () => {
    const sh = fakeShell()
    const before = stores()
    expect(applyAction(action('narrate', { text: 'look here' }), sh).state).toBe('executed')
    expect(applyAction(action('done', { summary: null }), sh).state).toBe('executed')
    expect(stores()).toEqual(before)
    for (const fn of Object.values(sh)) if (typeof fn === 'function' && 'mock' in fn) {
      if (fn !== sh.currentStance && fn !== sh.serializeLayout) expect(fn).not.toHaveBeenCalled()
    }
  })

  it('a shell call that throws becomes a skipped step, never an abort', () => {
    const sh = fakeShell()
    vi.mocked(sh.applyPreset).mockImplementation(() => {
      throw new Error('dock torn down')
    })
    expect(applyAction(action('arrange', { layout: 'wall', layout_type: 'preset' }), sh)).toEqual({
      state: 'skipped',
      reason: 'dock torn down',
    })
  })
})

describe('the shell port', () => {
  it('registers and unregisters', () => {
    const sh = fakeShell()
    const off = registerCaseShell(sh)
    expect(getCaseShell()).toBe(sh)
    off()
    expect(getCaseShell()).toBeNull()
  })
})
