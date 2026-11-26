/**
 * THE PANEL CONTRACT (U-6) — every reader view declares what it is, what it
 * binds to, where it opens and what it accepts; the operator views without a
 * contract are a named list; the router refuses what a view does not accept.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import {
  COMMON_ACTIONS,
  OPERATOR_CONTRACTS,
  OPERATOR_KINDS_WITHOUT_CONTRACT,
  READER_CONTRACTS,
  acceptedAction,
  allContracts,
  checkArgs,
  declaredContract,
  registerPanel,
  viewIdOf,
  type PanelContract,
} from './contracts'
import { contractOf } from './contractOf'
import { READER_SURFACES, OPERATOR_SURFACES, pageViews, readerKinds } from '@/lib/surfaces'
import { CASE_VOCABULARY } from '@/lib/caseModel'
import { GLOBE_LAYERS } from '@/v4/globe/catalog'

describe('the reader side is fully declared', () => {
  it('every view a reader page shows, and every reader kind, has a declared contract', () => {
    const ids = new Set<string>([...readerKinds(), ...READER_SURFACES.flatMap((s) => pageViews(s))])
    for (const id of ids) {
      const c = declaredContract(id)
      expect(c, id).toBeDefined()
      expect(c!.side, id).toBe('reader')
      expect(c!.name.length, id).toBeGreaterThan(1)
      expect(c!.description.length, id).toBeGreaterThan(20)
      expect(c!.description, id).not.toContain('\n')
    }
  })

  it('the map has ONE contract, the globe\'s: its real layer ids, fly-to, home, the window, select', () => {
    const map = declaredContract('v4.map')!
    expect(map.actions.map((a) => a.id)).toEqual(['set_layer', 'fly_to', 'home', 'set_window', 'select'])
    expect(map.binds).toEqual(expect.arrayContaining(['place', 'subject', 'window']))
    // The layer enum is the globe's own rows that a route serves.
    const layer = map.actions[0].args.find((a) => a.name === 'layer')!
    expect(layer.values).toEqual(GLOBE_LAYERS.filter((l) => !l.unavailable).map((l) => l.id))
    // Every layer of the case vocabulary (a Consult `map` step) that the globe
    // draws is bound to one of those rows; `entities` has no globe row.
    const bound = new Set(GLOBE_LAYERS.flatMap((l) => l.world))
    for (const l of CASE_VOCABULARY.layers) if (l !== 'entities') expect(bound.has(l as never), l).toBe(true)
  })

  it('a set_tab action names only tabs the view has (the server vocabulary agrees)', () => {
    for (const c of READER_CONTRACTS) {
      const t = c.actions.find((a) => a.id === 'set_tab')
      if (!t) continue
      const vocab = CASE_VOCABULARY.tabs[c.id]
      if (vocab) expect([...(t.args[0].values ?? [])].sort(), c.id).toEqual([...vocab].sort())
    }
  })

  it('the operator views without a contract are a named list, each a real kind on an operator page', () => {
    const operatorViews = new Set(OPERATOR_SURFACES.flatMap((s) => pageViews(s)))
    for (const k of OPERATOR_KINDS_WITHOUT_CONTRACT) {
      expect(PANEL_REGISTRY[k as keyof typeof PANEL_REGISTRY], k).toBeDefined()
      expect(declaredContract(k), k).toBeUndefined()
      // …and they still get a contract: the common actions and a panel home.
      const c = contractOf(k)!
      expect(c.side).toBe('operator')
      expect(c.actions).toEqual([])
    }
    // Every other operator view is a declared operator contract (U-11's frame views).
    for (const v of operatorViews) {
      if (OPERATOR_KINDS_WITHOUT_CONTRACT.includes(v)) continue
      expect(OPERATOR_CONTRACTS.map((c) => c.id), v).toContain(v)
      expect(declaredContract(v)!.side, v).toBe('operator')
    }
  })
})

describe('the action specs are checked', () => {
  const subject = declaredContract('subject.page')!
  it('the three common actions are accepted by every view', () => {
    for (const c of allContracts()) for (const a of COMMON_ACTIONS) expect(acceptedAction(c, a.id), `${c.id} ${a.id}`).toBeDefined()
  })
  it('an action a view does not declare is not accepted', () => {
    expect(acceptedAction(subject, 'layer')).toBeUndefined()
  })
  it('enums, refs and required args are typed', () => {
    const tab = acceptedAction(subject, 'set_tab')!
    expect(checkArgs(tab, { tab: 'evidence' })).toEqual({ ok: true, args: { tab: 'evidence' } })
    expect(checkArgs(tab, { tab: 'nope' })).toMatchObject({ ok: false })
    expect(checkArgs(tab, {})).toMatchObject({ ok: false, reason: 'missing tab' })
    const bind = acceptedAction(subject, 'bind')!
    expect(checkArgs(bind, { subject: { kind: 'finding', id: 'f1' } })).toMatchObject({ ok: false })
    expect(checkArgs(bind, { subject: { kind: 'target', id: 't1' } })).toMatchObject({ ok: true })
    const layer = acceptedAction(declaredContract('v4.map')!, 'set_layer')!
    expect(checkArgs(layer, { layer: 'events', on: 'yes' })).toMatchObject({ ok: false })
    expect(checkArgs(layer, { layer: 'entities', on: true })).toMatchObject({ ok: false })
    const fly = acceptedAction(declaredContract('v4.map')!, 'fly_to')!
    expect(checkArgs(fly, { lon: 43.4, lat: 12.6 })).toMatchObject({ ok: true })
    expect(checkArgs(fly, { lon: '43' })).toMatchObject({ ok: false, reason: 'lon must be a number' })
  })
})

describe('registering a view is one call', () => {
  let undo: (() => void) | null = null
  afterEach(() => undo?.())
  it('a new contract is listed, found by id and by instance id, and can be taken back', () => {
    const c: PanelContract = {
      id: 'test.dummy',
      name: 'Dummy',
      description: 'A view registered by a test, to prove one call is enough.',
      group: 'Other',
      side: 'reader',
      binds: ['subject'],
      size: 'column',
      home: 'panel',
      actions: [],
    }
    undo = registerPanel(c, () => null)
    expect(allContracts().map((x) => x.id)).toContain('test.dummy')
    expect(declaredContract('test.dummy:t1')).toBe(c)
    expect(viewIdOf('test.dummy:t1')).toBe('test.dummy')
    expect(viewIdOf('view:forecasts')).toBe('view:forecasts')
    expect(viewIdOf('slot:coverage')).toBe('slot:coverage')
    undo()
    undo = null
    expect(declaredContract('test.dummy')).toBeUndefined()
  })
})
