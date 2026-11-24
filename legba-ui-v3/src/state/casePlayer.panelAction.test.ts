/**
 * U-8 / Q-1 — the TENTH drive tool, `panel_action`, on the console.
 *
 * A consult case's `panel_action` step (`{view, action, args}`, the server's
 * `workstation_drive` canonical args) rides the same case stream and the same
 * player as the nine, and the one executor (`applyAction`) routes it to the
 * view's own contract (`applyPanelAction`). The map's `set_layer` and `home`
 * reach the globe's stores; an unknown view or an action the view does not
 * accept is REFUSED on the rail with its reason — never dropped silently.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { casePlayer, resetCasePlayer, useCasePlayer } from './casePlayer'
import { registerCaseShell, type CaseShell } from '@/lib/caseRunner'
import { applyAction } from '@/lib/applyAction'
import { actionLabel, parseCaseAction, parseCaseRecord } from '@/lib/caseModel'
import { clearViewUndo } from '@/lib/panelActions'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { resetGlobeLayers, useGlobeLayers } from '@/v4/globe/layerState'
import { useGlobeCamera } from '@/v4/globe/actions'
import { installMapContract } from '@/v4/globe/contract'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  closed = false
  constructor() {
    FakeEventSource.instances.push(this)
  }
  addEventListener() {}
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent)
  }
  close() {
    this.closed = true
  }
}

function panelFrame(seq: number, view: string, action: string, args: Record<string, unknown> = {}) {
  return {
    type: 'step',
    phase: 'case',
    kind: 'case_action',
    seq,
    action: 'panel_action',
    args: { view, action, args },
    cites: [{ kind: 'target', id: 't1' }],
    rests_on: [{ round: 1, tool: 'list_targets' }],
    at: '2026-11-14T09:00:00Z',
    round: 2,
  }
}

let unregister: () => void

beforeEach(() => {
  resetCasePlayer()
  resetScope()
  useSelection.getState().clear()
  resetGlobeLayers()
  clearViewUndo()
  useGlobeCamera.setState({ request: null, nonce: 0, view: null })
  installMapContract()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })))
  const shell: CaseShell = {
    mode: 'personal',
    openSingleton: vi.fn(() => ({})),
    openBound: vi.fn(),
    switchStance: vi.fn(),
    applyPreset: vi.fn(),
    runMission: vi.fn(),
    reopenConsult: vi.fn(),
    currentStance: vi.fn(() => 'board'),
    serializeLayout: vi.fn(() => ({})),
    restoreLayout: vi.fn(),
  }
  unregister = registerCaseShell(shell)
})

afterEach(() => {
  unregister()
  resetCasePlayer()
  vi.unstubAllGlobals()
})

describe('panel_action is a readable drive step', () => {
  it('parses as an action (outside the nine) and labels itself', () => {
    const a = parseCaseAction(panelFrame(1, 'v4.map', 'set_layer', { layer: 'events', on: true }))
    expect(a).not.toBeNull()
    expect(a!.action).toBe('panel_action')
    expect(actionLabel(a!)).toBe('set_layer v4.map events on')
    // still closed: an unknown tool name is not an action
    expect(parseCaseAction({ ...panelFrame(1, 'v4.map', 'home'), action: 'paint_wall' })).toBeNull()
  })

  it('a persisted record with a panel_action step keeps it (no false gap on replay)', () => {
    const f = panelFrame(1, 'v4.map', 'home')
    const rec = parseCaseRecord({ version: 'case/1', status: 'sealed', complete: true, actions: [f] })!
    expect(rec.actions).toHaveLength(1)
    expect(rec.complete).toBe(true)
  })
})

describe('the executor routes panel_action through the view contract', () => {
  const run = (view: string, action: string, args: Record<string, unknown> = {}) =>
    applyAction({ action: 'panel_action', args: { view, action, args } }, null, { driver: 'case' })

  it('set_layer and home drive the map (the catalogue’s own action and argument names)', () => {
    expect(useGlobeLayers.getState().on.events).toBe(false)
    expect(run('v4.map', 'set_layer', { layer: 'events', on: true })).toEqual({ state: 'executed' })
    expect(useGlobeLayers.getState().on.events).toBe(true)
    expect(run('v4.map', 'home')).toEqual({ state: 'executed' })
    expect(useGlobeCamera.getState().request).toEqual({ kind: 'home' })
  })

  it('an unknown view, an unaccepted action and a bad argument are refused by name', () => {
    expect(run('v4.nowhere', 'open')).toMatchObject({ state: 'refused', reason: expect.stringMatching(/unknown view: v4\.nowhere/) })
    expect(run('v4.map', 'explode')).toMatchObject({ state: 'refused', reason: expect.stringMatching(/action_not_accepted: explode on v4\.map/) })
    expect(run('v4.map', 'set_layer', { layer: 'drift', on: true })).toMatchObject({ state: 'refused' })
    expect(applyAction({ action: 'panel_action', args: { action: 'home' } }, null)).toMatchObject({
      state: 'refused',
      reason: expect.stringMatching(/needs a view and an action/),
    })
  })
})

describe('a live case runs panel_action steps and shows a refusal on the rail', () => {
  it('executes a known one and marks an unknown one refused, with its reason', () => {
    casePlayer().beginLive('req-1', 'p1')
    casePlayer().openStream('/api/v1/consult/stream/req-1?token=', { onStep: vi.fn(), onTerminal: vi.fn(), onActorFinal: vi.fn() })
    const es = FakeEventSource.instances.at(-1)!
    es.emit(panelFrame(1, 'v4.map', 'set_layer', { layer: 'at_sea', on: true }))
    es.emit(panelFrame(2, 'v4.nowhere', 'open'))
    const p = useCasePlayer.getState().player!
    expect(useGlobeLayers.getState().on.at_sea).toBe(true)
    expect(p.steps.map((s) => s.state)).toEqual(['executed', 'refused'])
    expect(p.steps[1].reason).toMatch(/unknown view: v4\.nowhere/)
    expect(p.steps[1].label).toBe('open v4.nowhere')
  })
})
