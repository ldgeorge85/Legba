/**
 * The SCOPE store — the channel that makes "a panel FILTERS by scope and
 * HIGHLIGHTS by focus" expressible (WORKSTATION_V2_FLOW_DESIGN §3).
 *
 * The property every one of these tests is circling is the one `feedView.ts`
 * could not obtain with a single `{kind, id}` slot: a scope must SURVIVE the
 * row clicks made under it. That is asserted directly (`installDeskScopeBridge`
 * ignores every kind but `target`) rather than left to each panel's discipline.
 */
import { beforeEach, describe, expect, it } from 'vitest'

import {
  emptyMembers,
  inScope,
  resetScope,
  scopeParams,
  useScope,
  type Scope,
} from './scope'
import { selectRow, useSelection } from './selection'
import { installDeskScopeBridge, scopeFromTarget } from '@/lib/scopeFromReport'

function scope(id: string, over: Partial<Scope> = {}): Scope {
  return {
    kind: 'report',
    id,
    label: id,
    members: emptyMembers(),
    origin: 'test',
    ...over,
  }
}

beforeEach(() => {
  resetScope()
  useSelection.getState().clear()
})

describe('set / clear', () => {
  it('starts empty, and an empty scope filters nothing', () => {
    expect(useScope.getState().scope).toBeNull()
    expect(scopeParams(null)).toEqual({})
    expect(inScope(null, { id: 'anything' })).toBe(true)
  })

  it('setScope replaces the current scope', () => {
    useScope.getState().setScope(scope('a'))
    useScope.getState().setScope(scope('b'))
    expect(useScope.getState().scope?.id).toBe('b')
  })

  it('clear drops the scope AND both halves of the trail', () => {
    useScope.getState().setScope(scope('a'))
    useScope.getState().setScope(scope('b'))
    useScope.getState().back()
    useScope.getState().clear()
    const s = useScope.getState()
    expect(s.scope).toBeNull()
    expect(s.history).toEqual([])
    expect(s.future).toEqual([])
  })

  it('a no-op re-scope of the same record does not grow the trail', () => {
    useScope.getState().setScope(scope('a'))
    useScope.getState().setScope(scope('a', { label: 'renamed' }))
    expect(useScope.getState().history).toEqual([])
    expect(useScope.getState().scope?.label).toBe('renamed')
  })
})

describe('history — back AND forward, as v2 had', () => {
  it('back walks the trail and forward walks it again', () => {
    useScope.getState().setScope(scope('a'))
    useScope.getState().setScope(scope('b'))
    useScope.getState().setScope(scope('c'))

    useScope.getState().back()
    expect(useScope.getState().scope?.id).toBe('b')
    useScope.getState().back()
    expect(useScope.getState().scope?.id).toBe('a')

    useScope.getState().forward()
    expect(useScope.getState().scope?.id).toBe('b')
    useScope.getState().forward()
    expect(useScope.getState().scope?.id).toBe('c')
  })

  it('back and forward are no-ops at the ends of the trail', () => {
    useScope.getState().setScope(scope('a'))
    useScope.getState().back()
    useScope.getState().back()
    expect(useScope.getState().scope?.id).toBe('a')
    useScope.getState().forward()
    useScope.getState().forward()
    expect(useScope.getState().scope?.id).toBe('a')
  })

  it('a fresh scope after a back DROPS the forward stack (a new branch)', () => {
    useScope.getState().setScope(scope('a'))
    useScope.getState().setScope(scope('b'))
    useScope.getState().back()
    expect(useScope.getState().future.map((s) => s.id)).toEqual(['b'])

    useScope.getState().setScope(scope('c'))
    expect(useScope.getState().future).toEqual([])
    useScope.getState().forward()
    expect(useScope.getState().scope?.id).toBe('c')
  })
})

describe('scopeParams — what the server can actually express', () => {
  it('pushes target_id for a SINGLE-desk scope', () => {
    const s = scope('r', {
      members: { ...emptyMembers(), targetIds: ['country_g20_br'] },
    })
    expect(scopeParams(s).target_id).toBe('country_g20_br')
  })

  it('pushes NO target_id for a multi-desk scope', () => {
    // `/findings` takes one target, not a set. Sending the first of fourteen
    // would show the operator a fourteenth of their scope and call it the
    // scope; the client-side `inScope` carries the rest.
    const s = scope('r', {
      members: { ...emptyMembers(), targetIds: ['a', 'b', 'c'] },
    })
    expect(scopeParams(s).target_id).toBeUndefined()
  })

  it('derives `since` from as_of − windowHours', () => {
    const s = scope('r', {
      members: { ...emptyMembers(), asOf: '2026-09-03T12:00:00Z', windowHours: 12 },
    })
    expect(scopeParams(s).since).toBe('2026-09-03T00:00:00.000Z')
  })

  it('pushes no `since` when the record carries no window', () => {
    const s = scope('r', { members: { ...emptyMembers(), asOf: '2026-09-03T12:00:00Z' } })
    expect(scopeParams(s).since).toBeUndefined()
  })
})

describe('inScope — a silent axis matches EVERYTHING', () => {
  it('filters by desk when the scope names desks', () => {
    const s = scope('r', { members: { ...emptyMembers(), targetIds: ['br'] } })
    expect(inScope(s, { id: 'x', target_id: 'br' })).toBe(true)
    expect(inScope(s, { id: 'x', target_id: 'ar' })).toBe(false)
  })

  it('filters by id when the scope names findings and the row has no desk', () => {
    const s = scope('r', { members: { ...emptyMembers(), findingIds: ['f1'] } })
    expect(inScope(s, { id: 'f1' })).toBe(true)
    expect(inScope(s, { id: 'f2' })).toBe(false)
  })

  it('a scope with no members matches everything rather than nothing', () => {
    // An under-populated payload must not blank every following panel — that
    // reads as a broken link, which is the defect this train removes.
    expect(inScope(scope('r'), { id: 'anything', target_id: 'anywhere' })).toBe(true)
  })
})

describe('the desk bridge — scope survives every row click', () => {
  it('a target selection sets the scope', () => {
    const off = installDeskScopeBridge()
    selectRow('target', 'country_g20_br', 'Brazil', { origin: 'desks' })
    expect(useScope.getState().scope?.kind).toBe('target')
    expect(useScope.getState().scope?.id).toBe('country_g20_br')
    off()
  })

  it('NO other kind touches the scope — the whole point of the split', () => {
    const off = installDeskScopeBridge()
    selectRow('target', 'country_g20_br', 'Brazil')
    for (const kind of ['finding', 'signal', 'situation', 'entity', 'source']) {
      selectRow(kind, `${kind}-1`, kind)
      expect(useScope.getState().scope?.id, `${kind} moved the scope`).toBe('country_g20_br')
    }
    // …and the focus DID move, every time.
    expect(useSelection.getState().selection?.kind).toBe('source')
    off()
  })

  it('re-selecting the same desk never churns the store', () => {
    const off = installDeskScopeBridge()
    selectRow('target', 'br', 'Brazil')
    selectRow('finding', 'f1')
    selectRow('target', 'br', 'Brazil')
    expect(useScope.getState().history).toEqual([])
    off()
  })

  it('adopts a desk already selected when the shell mounts (the deep-link case)', () => {
    selectRow('target', 'br', 'Brazil', { origin: 'share-link' })
    resetScope()
    const off = installDeskScopeBridge()
    expect(useScope.getState().scope?.id).toBe('br')
    off()
  })

  it('unsubscribing stops the mirroring', () => {
    const off = installDeskScopeBridge()
    off()
    selectRow('target', 'ar', 'Argentina')
    expect(useScope.getState().scope).toBeNull()
  })
})

describe('scopeFromTarget', () => {
  it('names the desk as its only member and labels it for the chip', () => {
    const s = scopeFromTarget('country_g20_br', 'Brazil')
    expect(s.kind).toBe('target')
    expect(s.members.targetIds).toEqual(['country_g20_br'])
    expect(s.label).toBe('Brazil')
  })

  it('falls back to the id when there is no label — never a blank chip', () => {
    expect(scopeFromTarget('country_g20_br', '   ').label).toBe('country_g20_br')
  })
})
